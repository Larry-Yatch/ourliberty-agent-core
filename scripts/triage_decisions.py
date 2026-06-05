#!/usr/bin/env python3
"""Triage the pending decision rows (approval_request / clarify_request) into
LIVE vs STALE vs UNCERTAIN using real resolution signals. READ-ONLY by default.

Signals (grounded in agent-core code):
  - approval_request: resolved iff its task_id is in beacon-pending-approvals.json
    `history` and NOT in `pending`. LIVE iff still in `pending`.
  - clarify_request: resolved iff a clarify_response chain_event exists for the
    same task_id with a later ts. LIVE iff none.
  - Anything we cannot map to a signal -> UNCERTAIN (hand to a subagent / git check).

Mocks (real-* / prod-clr / prod-fail / task-cascade) are flagged MOCK separately
so they are never counted as live decisions.

With --apply: backs up, then sets read_at ONLY on rows classified STALE or MOCK.
LIVE and UNCERTAIN are never touched.
"""
import argparse
import json
import os
import sys
import datetime as dt
from collections import defaultdict

PENDING_APPROVALS = "/home/larry/agents/state/beacon-pending-approvals.json"
MOCK_MARKERS = ("real-", "prod-clr", "prod-fail", "task-cascade", "real-pf")
# A marker identifies a mock task_id only as a whole leading component, not as a
# bare substring: 'prod-fail' tags 'prod-fail-001' but must NOT tag the real
# 'prod-failover-check', and 'real-' must NOT tag 'unreal-deploy-001' (audit
# #36). A marker matches when the id equals it or begins with it followed by a
# '-'/'_' component separator. Markers that already end in a separator ('real-')
# carry their own boundary.
_COMPONENT_SEP = ("-", "_")


def _client():
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


def _is_mock(tid):
    tid = tid or ""
    for m in MOCK_MARKERS:
        if tid == m:
            return True
        if tid.startswith(m) and (m.endswith(_COMPONENT_SEP)
                                  or tid[len(m):len(m) + 1] in _COMPONENT_SEP):
            return True
    return False


def _fetch(client, **filt):
    rows, page, size = [], 0, 1000
    while True:
        q = client.table("chain_events").select(
            "event_id,event_type,task_id,ts,read_at"
        ).is_("read_at", "null")
        for k, v in filt.items():
            q = q.eq(k, v)
        resp = q.range(page * size, page * size + size - 1).execute()
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < size:
            break
        page += 1
    return rows


def build_beacon_signals(beacon_approvals):
    """Derive the approval-resolution signals from parsed
    beacon-pending-approvals.json content.

    Returns (pending_ids, history_ids, hist_resolved_at) where
    hist_resolved_at maps task_id -> latest resolved_at stamp in history.
    """
    pending_ids = {e.get("id") or e.get("task_id")
                   for e in beacon_approvals.get("pending", [])}
    history_ids = {e.get("id") or e.get("task_id")
                   for e in beacon_approvals.get("history", [])}
    hist_resolved_at = {}
    for e in beacon_approvals.get("history", []):
        tid = e.get("id") or e.get("task_id")
        ra = e.get("resolved_at") or ""
        if tid and ra > hist_resolved_at.get(tid, ""):
            hist_resolved_at[tid] = ra
    return pending_ids, history_ids, hist_resolved_at


def fetch_clarify_response_ts(client):
    """task_id -> [clarify_response ts, ...] — the clarify resolution signal."""
    resp = client.table("chain_events").select("task_id,ts").eq(
        "event_type", "clarify_response").execute()
    cresp_ts = defaultdict(list)
    for r in (resp.data or []):
        if r.get("task_id"):
            cresp_ts[r["task_id"]].append(r.get("ts") or "")
    return cresp_ts


def classify_approval(row, pending_ids, history_ids):
    """LIVE/STALE/MOCK/UNCERTAIN for one approval_request row, + a reason."""
    tid = row.get("task_id")
    if _is_mock(tid):
        return "MOCK", "mock task_id"
    if tid in pending_ids:
        return "LIVE", "still pending in beacon-pending-approvals"
    if tid in history_ids:
        return "STALE", "resolved in beacon history"
    return "UNCERTAIN", "no entry in beacon pending or history"


def classify_clarify(row, history_ids, hist_resolved_at, cresp_ts):
    """LIVE/STALE/MOCK/UNCERTAIN for one clarify_request row, + a reason."""
    tid = row.get("task_id")
    if _is_mock(tid):
        return "MOCK", "mock task_id"
    later = [t for t in cresp_ts.get(tid, []) if t > (row.get("ts") or "")]
    if later:
        return "STALE", f"clarify_response exists ({len(later)})"
    if cresp_ts.get(tid):
        return "STALE", "clarify_response exists (same/earlier ts)"
    # Progressed-past signal: same task had an approval resolved at/after
    # this clarify's ts -> the task moved on, clarify effectively answered.
    ra = hist_resolved_at.get(tid)
    if ra and ra >= (row.get("ts") or ""):
        return "STALE", "task approval resolved after clarify (progressed past)"
    if tid in history_ids:
        return "UNCERTAIN", "task in beacon history but resolved before clarify ts"
    return "LIVE", "no clarify_response and task not resolved"


def classify_rows(approvals, clarifies, pending_ids, history_ids,
                  hist_resolved_at, cresp_ts):
    """Classify every pending decision row.

    Returns a list of (cls, event_type, row, why) tuples. This is the
    single source of triage truth shared by the CLI (`main`) and the
    dashboard `/api/larry/cleanup-review` engine — no fork.
    """
    out = []
    for r in approvals:
        cls, why = classify_approval(r, pending_ids, history_ids)
        out.append((cls, "approval_request", r, why))
    for r in clarifies:
        cls, why = classify_clarify(r, history_ids, hist_resolved_at, cresp_ts)
        out.append((cls, "clarify_request", r, why))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-dir", default="/home/larry/agents/blackboard/backups")
    args = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    client = _client()

    # Decision rows still pending.
    approvals = _fetch(client, event_type="approval_request")
    clarifies = _fetch(client, event_type="clarify_request")

    # Beacon approval state.
    ba = json.load(open(PENDING_APPROVALS))
    pending_ids, history_ids, hist_resolved_at = build_beacon_signals(ba)

    # All clarify_response task_ids (resolution signal for clarify_request).
    cresp_ts = fetch_clarify_response_ts(client)

    rows_classified = classify_rows(
        approvals, clarifies,
        pending_ids, history_ids, hist_resolved_at, cresp_ts,
    )
    buckets = defaultdict(list)
    for cls, et, r, why in rows_classified:
        buckets[cls].append((et, r, why))

    total = len(approvals) + len(clarifies)
    print(f"DECISION ROWS PENDING: {total}  "
          f"(approval={len(approvals)}, clarify={len(clarifies)})")
    print(f"beacon-pending-approvals: pending={len(pending_ids)}, history={len(history_ids)}")
    for cls in ("LIVE", "UNCERTAIN", "STALE", "MOCK"):
        items = buckets.get(cls, [])
        print(f"\n=== {cls}: {len(items)} ===")
        for et, r, why in sorted(items, key=lambda x: x[1].get("ts") or ""):
            age = "?"
            try:
                t = dt.datetime.fromisoformat((r["ts"]).replace("Z", "+00:00"))
                age = f"{(now - t).days}d"
            except Exception:
                pass
            print(f"  [{et:17}] {r.get('task_id'):52} {age:>4}  {why}")

    if not args.apply:
        print("\nDRY RUN — no writes. STALE+MOCK would be cleared; LIVE+UNCERTAIN kept.")
        return

    to_clear = [r for cls, et, r, why in rows_classified if cls in ("STALE", "MOCK")]
    if not to_clear:
        print("\nNothing to clear.")
        return
    os.makedirs(args.backup_dir, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    bpath = os.path.join(args.backup_dir, f"triage-decisions-{stamp}.json")
    json.dump(to_clear, open(bpath, "w"), indent=2, default=str)
    ids = [r["event_id"] for r in to_clear]
    for i in range(0, len(ids), 200):
        client.table("chain_events").update({"read_at": now.isoformat()}) \
            .in_("event_id", ids[i:i + 200]).execute()
    print(f"\nCleared {len(ids)} STALE+MOCK rows. Backup: {bpath}")
    print(f"LEFT untouched: LIVE={len(buckets.get('LIVE', []))}, "
          f"UNCERTAIN={len(buckets.get('UNCERTAIN', []))}")


if __name__ == "__main__":
    main()
