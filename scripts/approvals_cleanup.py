#!/usr/bin/env python3
"""One-time cleanup for the dashboard Approvals queue (chain_events table).

The Approvals tab is an append-only firehose: a row leaves the pending list
ONLY when read_at is set (Larry clicks Approve/Reject/Mark-done). Nothing else
clears it, so noise (recurring healer alerts, escalations, sentinel chatter)
and old test/mock rows accumulate forever. This script clears the SAFE
categories by setting read_at, after backing up every affected row.

SAFETY MODEL
  - DRY RUN by default: reads + categorizes, writes nothing.
  - --apply: backs up every row it will touch to a timestamped JSON file
    FIRST, then sets read_at on those rows in batches.
  - NOISE types (larry_alert, sentinel_alert, escalation) -> cleared.
    These are informational/self-resolving; per Larry they move out of the
    Approvals tab entirely, so clearing the backlog is consistent.
  - TEST/MOCK rows (real-*, task-cascade, fixtures, xxxx-body) -> cleared,
    across all event types. These leaked from tests into the live queue.
  - DECISION types (approval_request, clarify_request) -> LEFT ALONE by
    default. These genuinely need Larry. Reported, never auto-cleared,
    unless --include-stale-decisions and older than --stale-days.

Restore: read_at can be set back to NULL for any event_id from the backup
file, so every clear is reversible.
"""
import argparse
import json
import os
import sys
import datetime as dt
from collections import Counter

NOISE_TYPES = {"larry_alert", "sentinel_alert", "escalation"}
DECISION_TYPES = {"approval_request", "clarify_request"}

# task_id / body markers that identify test+mock rows that leaked into prod.
TEST_TASKID_MARKERS = ("real-", "task-cascade", "zz-fixture", "real-pf")
TEST_BODY_MARKERS = ("xxxxxxxx",)


def _client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not in environment")
    from supabase import create_client
    return create_client(url, key)


def _is_test(row):
    tid = row.get("task_id") or ""
    if any(m in tid for m in TEST_TASKID_MARKERS):
        return True
    body = json.dumps(row.get("payload") or {})
    return any(m in body for m in TEST_BODY_MARKERS)


def _fetch_all_pending(client):
    """Page through every pending (read_at IS NULL) row."""
    rows, page, size = [], 0, 1000
    while True:
        resp = (
            client.table("chain_events")
            .select("event_id,event_type,agent,task_id,ts,read_at,payload")
            .is_("read_at", "null")
            .order("ts", desc=False)
            .range(page * size, page * size + size - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < size:
            break
        page += 1
    return rows


def _age_days(row, now):
    try:
        t = dt.datetime.fromisoformat((row["ts"]).replace("Z", "+00:00"))
        return (now - t).days
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually set read_at (default: dry run, no writes)")
    ap.add_argument("--include-stale-decisions", action="store_true",
                    help="also clear approval/clarify rows older than --stale-days")
    ap.add_argument("--stale-days", type=int, default=7)
    ap.add_argument("--backup-dir", default="/home/larry/agents/blackboard/backups")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    client = _client()
    rows = _fetch_all_pending(client)

    by_type = Counter(r.get("event_type") for r in rows)
    print(f"TOTAL pending (read_at IS NULL): {len(rows)}")
    print("by event_type:", dict(by_type))

    to_clear, test_rows, noise_rows, stale_dec, live_dec = [], [], [], [], []
    for r in rows:
        et = r.get("event_type")
        if _is_test(r):
            test_rows.append(r)
            to_clear.append(r)
        elif et in NOISE_TYPES:
            noise_rows.append(r)
            to_clear.append(r)
        elif et in DECISION_TYPES:
            age = _age_days(r, now)
            if args.include_stale_decisions and age is not None and age >= args.stale_days:
                stale_dec.append(r)
                to_clear.append(r)
            else:
                live_dec.append(r)

    print(f"\n  test/mock rows         : {len(test_rows)}")
    print(f"  noise (alert/escalation): {len(noise_rows)}")
    print(f"  stale decisions (>{args.stale_days}d): {len(stale_dec)}"
          + ("" if args.include_stale_decisions else "  [NOT cleared; pass --include-stale-decisions]"))
    print(f"  live decisions LEFT     : {len(live_dec)}")
    print(f"\n  -> WOULD CLEAR          : {len(to_clear)}")
    print(f"  -> REMAINING after clear: {len(rows) - len(to_clear)}")

    # Sample of the live decisions that will remain, for Larry to eyeball.
    print("\nLive decisions that will REMAIN (newest 15):")
    for r in sorted(live_dec, key=lambda x: x.get("ts") or "", reverse=True)[:15]:
        tid = r.get("task_id")
        age = _age_days(r, now)
        print(f"    [{r.get('event_type')}] {tid}  ({age}d old)")

    if not args.apply:
        print("\nDRY RUN — no writes made. Re-run with --apply to clear.")
        return

    if not to_clear:
        print("\nNothing to clear.")
        return

    # Backup FIRST.
    os.makedirs(args.backup_dir, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    backup_path = os.path.join(args.backup_dir, f"approvals-cleanup-{stamp}.json")
    with open(backup_path, "w") as f:
        json.dump(to_clear, f, indent=2, default=str)
    print(f"\nBacked up {len(to_clear)} rows -> {backup_path}")

    # Clear in batches.
    ids = [r["event_id"] for r in to_clear]
    cleared = 0
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        client.table("chain_events").update({"read_at": now.isoformat()}) \
            .in_("event_id", chunk).execute()
        cleared += len(chunk)
        print(f"  cleared {cleared}/{len(ids)}")
    print(f"\nDONE. Cleared {cleared}. Remaining pending: {len(rows) - cleared}.")
    print(f"Reversible from backup: {backup_path}")


if __name__ == "__main__":
    main()
