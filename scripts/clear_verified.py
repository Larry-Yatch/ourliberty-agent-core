#!/usr/bin/env python3
"""Clear pending decision rows for an explicit, subagent-VERIFIED list of
completed task_ids. Backup-first, reversible. Clears only approval_request /
clarify_request rows with read_at IS NULL whose task_id is in VERIFIED_DONE.

Dry-run by default — pass --apply to actually write. All destructive logic
lives under main()/__main__ so that merely importing this module performs NO
Supabase connection or mutation (audit #64).
"""
import argparse
import json
import os
import sys
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supabase_chunk import chunked_clear, ChunkedClearError  # noqa: E402

VERIFIED_DONE = {
    "build-sequence-orchestrator-pr-s3a-droplet-api-endpoint",
    "build-sequence-orchestrator-pr-s4-shortcuts-routing-and-mirror-dag-verify",
    "claude-quota-fixes-v2-tier2-bugs-plus-alert-noise-plus-new-healers",
    "fix-sync-service-silent-failure-and-pulse-wrong-branch",
    "pulse-check-iii-tune-forge-stuck-threshold",
    "pulse-cycle-fixture-leak-fix",
    "droplet-drift-discipline",
    "test-isolation-v2",
    "e4-4f-missions-tab-v1",
    "fix-watchdog-tmux-liveness-check",
    "clarify-shipper-extend",
    "watchdog-bot-liveness-policy-001",
    "step-clarify-visibility",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true",
        help="Actually clear the matched rows. Without it, dry-run (no writes).")
    args = ap.parse_args()

    from supabase import create_client
    c = create_client(os.environ["SUPABASE_URL"],
                      os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    now = dt.datetime.now(dt.timezone.utc)

    rows = c.table("chain_events").select("event_id,event_type,task_id,ts,read_at") \
        .is_("read_at", "null").in_("event_type", ["approval_request", "clarify_request"]) \
        .in_("task_id", sorted(VERIFIED_DONE)).execute().data or []

    print(f"Matched {len(rows)} pending verified-done decision rows.")
    if not rows:
        return 0

    if not args.apply:
        for r in rows:
            print(f"  would clear: {r.get('task_id')} ({r.get('event_id')})")
        print("DRY RUN — no writes made. Re-run with --apply to clear.")
        return 0

    bdir = "/home/larry/agents/blackboard/backups"
    os.makedirs(bdir, exist_ok=True)
    bpath = os.path.join(bdir, f"clear-verified-{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    with open(bpath, "w") as f:
        json.dump(rows, f, indent=2, default=str)

    ids = [r["event_id"] for r in rows]
    try:
        cleared = chunked_clear(c, "chain_events", ids, {"read_at": now.isoformat()})
    except ChunkedClearError as e:
        # PARTIAL clear: earlier chunks are already written. The backup holds
        # ALL matched rows, so read_at -> NULL for the cleared subset is the
        # exact reversal; uncleared rows are still NULL (reversing them is a
        # harmless no-op).
        print(f"PARTIAL CLEAR: {e.cleared_count}/{e.total} rows cleared before "
              f"failure ({e.cause}).")
        print(f"Reverse the cleared rows from backup: {bpath}")
        return 1
    print(f"Cleared {len(cleared)} rows. Backup: {bpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
