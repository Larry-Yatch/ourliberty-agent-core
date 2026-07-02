#!/usr/bin/env python3
"""One-time cleanup: clear the retired `parked_capture` chain_events rows.

The `parked_capture` chain_events projection (approval-sync Phase 3a §3a.1) was
retired 2026-07-01: the dashboard dropped the Parked lane from the Needs-You
surface (ourliberty-dashboard#101 — the parked backlog lives only on the
Missions tab), so nothing reads these rows. heal_missions_card_gc no longer
projects them, which means the open rows that existed at the moment the healer
stopped reconciling would otherwise sit `read_at IS NULL` forever.

This script clears them ONCE by handing an EMPTY desired set to the same
`reconcile_open_events` primitive the projection used: every open
`parked_capture` row is `read_at`-stamped (cleared), and — because desired is
empty — nothing is emitted. It reuses the primitive's safety model verbatim:

  * If the open set can't be read (no client / Supabase error / over the scan
    cap), reconcile SKIPS rather than blind-clearing — an empty desired against
    an unknown open set is exactly the dangerous case the primitive guards.
  * Idempotent: re-running clears nothing more (all rows are already read).

RUN AFTER the healer is redeployed. Order matters: if this runs while the
droplet still runs the pre-retirement heal_missions_card_gc, that daemon
re-emits parked_capture rows on its next tick, leaving new orphans this one-shot
won't revisit. Deploy the removal + restart the healer FIRST, confirm no tick
still projects, THEN run this.

REVERSIBLE: before clearing, --apply writes the task_ids it is about to clear to
a timestamped JSON backup (mirrors scripts/approvals_cleanup.py). To undo, set
`read_at` back to NULL for `event_type='parked_capture'` AND those task_ids —
necessary because the projection's re-emit path upserts with
ignore_duplicates=True and would NOT re-open an already-cleared row. If the
backup can't be written the clear is ABORTED (never clear without a record).

DRY RUN by default (reports the open count, writes nothing). Pass --apply to
clear. Needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in the environment.

    python3 scripts/retire_parked_capture_rows.py                 # dry-run
    python3 scripts/retire_parked_capture_rows.py --apply         # clear
    python3 scripts/retire_parked_capture_rows.py --apply --no-backup
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chain_event_emit as cee  # noqa: E402

EVENT_TYPE = "parked_capture"
AGENT = "retire_parked_capture"
# Same backup home scripts/approvals_cleanup.py uses.
DEFAULT_BACKUP_DIR = "/home/larry/agents/blackboard/backups"


def _client():
    """Build a live Supabase client, or exit if the env isn't configured.

    Mirrors scripts/approvals_cleanup.py._client (goes through supabase_factory)
    rather than reaching into chain_event_emit's private _get_client — and,
    unlike that one, this returns a real client under a test env, so callers
    inject a fake in tests instead of building one here."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not in environment")
    from supabase_factory import get_supabase_client
    return get_supabase_client(url, key)


def _write_backup(task_ids: list[str], backup_dir: str,
                  logger: logging.Logger) -> str:
    """Write the to-be-cleared task_ids to a timestamped JSON. Returns the path.
    Raises on failure so the caller ABORTS the clear (never clear w/o a record)."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(backup_dir, f"retire-parked-capture-{stamp}.json")
    payload = {
        "event_type": EVENT_TYPE,
        "backed_up_at": stamp,
        "task_ids": sorted(task_ids),
        "restore": (
            "UPDATE chain_events SET read_at=NULL WHERE "
            f"event_type='{EVENT_TYPE}' AND task_id IN (task_ids)"
        ),
    }
    Path(path).write_text(json.dumps(payload, indent=2) + "\n")
    logger.info("backed up %d task_id(s) -> %s", len(task_ids), path)
    return path


def retire(
    *,
    apply: bool,
    client: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
    backup: bool = True,
    backup_dir: str = DEFAULT_BACKUP_DIR,
) -> dict:
    """Report (dry-run) or clear (apply) the open `parked_capture` rows.

    Returns a summary dict:
      * dry-run  -> {'open': <int|None>, 'applied': False}
        (`open` is None when the open set can't be read.)
      * apply    -> the reconcile_open_events {emitted, cleared, skipped} summary
        plus {'applied': True, 'backup': <path|None>}. `skipped` is True (and no
        clear happens) when the open set is unreadable OR the backup write fails.
    `client` is a test seam; production injects one via `_client()`.
    """
    log = logger or logging.getLogger("retire_parked_capture")
    open_ids = cee.list_open_event_task_ids(EVENT_TYPE, client=client, logger=log)
    if not apply:
        n = None if open_ids is None else len(open_ids)
        log.info(
            "DRY RUN: %s open %r rows (pass --apply to clear)",
            "unknown number of" if n is None else n, EVENT_TYPE,
        )
        return {"open": n, "applied": False}
    if open_ids is None:
        # Unreadable open set -> the empty-desired reconcile would blind-clear an
        # unknown set. Refuse (matches reconcile's own skip); no clear happens.
        log.warning("open set unreadable — skipping clear (no blind clear)")
        return {"emitted": 0, "cleared": 0, "skipped": True,
                "applied": True, "backup": None}
    backup_path = None
    if open_ids and backup:
        try:
            backup_path = _write_backup(sorted(open_ids), backup_dir, log)
        except OSError as e:
            log.error("backup write failed (%s: %s) — aborting clear",
                      type(e).__name__, e)
            return {"emitted": 0, "cleared": 0, "skipped": True,
                    "applied": True, "backup": None}
    # Empty desired -> clear every open row, emit nothing. reconcile SKIPS on an
    # unreadable open set, so a transient read failure never blind-clears.
    summary = cee.reconcile_open_events(
        EVENT_TYPE, {}, agent=AGENT, client=client, logger=log)
    log.info(
        "APPLY: cleared=%s emitted=%s skipped=%s",
        summary.get("cleared"), summary.get("emitted"), summary.get("skipped"),
    )
    return {**summary, "applied": True, "backup": backup_path}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clear the retired parked_capture chain_events rows.")
    parser.add_argument(
        "--apply", action="store_true",
        help="clear the open parked_capture rows (default: dry-run report only)")
    parser.add_argument(
        "--no-backup", action="store_true",
        help="skip the pre-clear task_id backup (default: back up first)")
    parser.add_argument(
        "--backup-dir", default=DEFAULT_BACKUP_DIR,
        help=f"where to write the backup JSON (default: {DEFAULT_BACKUP_DIR})")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="[%(levelname)s] %(message)s")
    log = logging.getLogger("retire_parked_capture")

    client = _client()  # exits if SUPABASE_* env is missing
    result = retire(apply=args.apply, client=client, logger=log,
                    backup=not args.no_backup, backup_dir=args.backup_dir)
    if not args.apply:
        return 0
    # An apply that SKIPPED cleared nothing (unreadable open set / backup
    # failure) — signal non-zero so an operator re-runs rather than assuming it
    # converged.
    return 1 if result.get("skipped") else 0


if __name__ == "__main__":
    raise SystemExit(main())
