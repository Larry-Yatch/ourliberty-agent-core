#!/usr/bin/env python3
"""decision_outcome_ledger.py — append-only audit trail of resolved needs-Larry
decisions, for the Operator's govern loop.

WHY THIS EXISTS
---------------
The Operator's govern loop (Operator Feed Loop, slice 3) learns where Larry has
consistently approved low-risk work so it can PROPOSE widening autonomy in those
areas. To learn that, it needs a durable record of every decision Larry (or a
resolving surface acting for him) has made: what was decided, which way, and by
whom. This module is that record. It is deliberately built and wired in slice 1
— long before the assessor that reads it ships — so that by the time the
assessor turns on it has weeks of real approve/reject history to reason over
instead of starting cold.

It records ONLY the click (the decision). Joining the team's downstream build
result (merged-clean / reverted / gate-failed) onto a decision is a SEPARATE,
later step keyed on `decision_key`; this module does not reach for that here.

Storage: append-only JSONL at ~/agents/state/decision-outcome-ledger.jsonl.
One JSON object per line. Two row KINDS share the file, joined on decision_key:

  DECISION row (slice 1) — Larry's click, written by record_decision::

    {
      "ts": "<ISO 8601 UTC>",
      "kind": "decision",
      "decision_key": "<canonical_decision_key or ''>",
      "outcome": "approved|rejected|modified|expired",
      "actor": "<resolving surface: dashboard|telegram|heal_stale_approvals|...>",
      "cleared": <int — store-legs the resolution actually cleared>,
      "notes": "<optional short string>"
    }

  BUILD_OUTCOME row (slice 2) — how the authorized work turned out, written by
  record_build_outcome (from decision_outcome_reconcile, GitHub ground truth)::

    {
      "ts": "<ISO 8601 UTC>",
      "kind": "build_outcome",
      "decision_key": "<same key as the decision it joins>",
      "build_outcome": "merged|merged_regressed|closed_unmerged",
      "pr_number": <int, optional>,
      "actor": "reconcile",
      "notes": "<optional short string>"
    }

A pre-`kind` row (none exist in prod yet) is read as a DECISION row.

Discipline (mirrors scripts/medic_ledger.py — the proven ledger pattern here):

  - Stdlib only. Never raises on IO / JSON-decode error — the caller
    (decision_resolve) is fire-and-forget and must never have its resolution
    broken by the audit trail. WARN-logs on every fallback so misconfig surfaces
    in the ledger's own log.
  - `cleared` is the resolution's total cleared-legs count. The caller only
    records when that is > 0, so a healer re-driving an already-resolved
    decision (which clears nothing) does NOT append a duplicate — one genuine
    resolution yields exactly one record. `actor` preserves WHICH surface first
    resolved it, so a reconciler-driven first-resolution is distinguishable from
    a human click at read time.
  - `outcome` is normalized to a known value; an unrecognized value is coerced
    to 'expired' (the inert bucket) rather than dropped, so the row is preserved
    even when a caller miscoded it.

KNOWN LIMITATIONS (for the govern-loop assessor that will read this)
-------------------------------------------------------------------
  - EMPTY-KEY ROWS ARE TALLY-ONLY. A resolution with an underivable canonical
    key (P-leg-only: an acted-on pending entry with no derivable cross-store
    key) records with `decision_key=''`. Such rows still count in
    `outcome_counts()` but can NEVER be joined to a downstream build result.
    The assessor must treat '' as "count, don't join".
  - RARE CONCURRENT DOUBLE-COUNT. If two surfaces resolve the SAME key truly
    concurrently, the store-level idempotency guards can split the legs between
    the two calls so each sees cleared>0 and each appends a row — 2 records for
    1 decision. It cannot corrupt anything (append-only) and is narrow, but if
    the assessor needs an exact approval tally it should dedup on
    (decision_key, outcome) within a short window at READ time rather than
    trusting a raw row count.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
LEDGER_FILE = AGENTS_ROOT / 'state' / 'decision-outcome-ledger.jsonl'
LOG_FILE = AGENTS_ROOT / 'logs' / 'decision-outcome-ledger.log'

# Mirrors decision_resolve.VALID_OUTCOMES — kept as a literal here so the ledger
# has no import-time dependency on decision_resolve (which imports this module).
VALID_OUTCOMES = ('approved', 'rejected', 'modified', 'expired')

# Build outcomes joined onto a decision AFTER the fact (slice 2), by
# decision_outcome_reconcile.py from GitHub ground truth. 'pending' is never
# RECORDED (a non-terminal PR is simply re-checked next pass); it exists only as
# a classifier return value. A recorded build_outcome row is terminal.
VALID_BUILD_OUTCOMES = ('merged', 'merged_regressed', 'closed_unmerged')

# A `closed_unmerged` outcome is NOT permanently terminal: a CLOSED PR can be
# reopened and merged. So a closed_unmerged row younger than the settle period
# is RE-CHECKABLE — the reconciler re-probes it each pass and a later `merged`
# supersedes it (readers take the newest row per key). Past the settle window
# the abandonment is treated as final and the key stops being re-checked.
SETTLE_PERIOD_DAYS = 14


def _log(level: str, msg: str) -> None:
    """Append a structured line to the ledger's own log. Never raises; if the
    log directory is unwritable the line is dropped. Greppable shape matching
    the healer/ledger constellation."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] [{level}] decision_outcome_ledger: {msg}\n')
    except OSError:
        pass


def _parse_ts(ts) -> Optional[datetime]:
    """Parse an ISO-8601 ledger `ts` to a tz-aware datetime, or None if it is
    missing/unparseable. A naive timestamp is assumed UTC (the writer always
    stamps tz-aware UTC; the fallback is defensive)."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _iter_records():
    """Yield each parsed record from the ledger file. Malformed lines are
    skipped with a WARN; an unreadable / missing file yields nothing. Never
    raises."""
    if not LEDGER_FILE.exists():
        return
    try:
        with open(LEDGER_FILE, encoding='utf-8') as f:
            for idx, raw in enumerate(f):
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    _log('WARN', f'ledger line {idx} malformed; skipping')
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError as e:
        _log('WARN', f'ledger read failed: {e}; treating as empty')
        return


def record_decision(
    decision_key: Optional[str],
    outcome: str,
    *,
    actor: Optional[str] = None,
    cleared: int = 0,
    notes: Optional[str] = None,
) -> bool:
    """Append one decision record. Returns True on success, False on any failure
    (full disk, bad permissions, ...). Never raises.

    `outcome` is normalized to a VALID_OUTCOMES value ('expired' if unknown).
    `decision_key` None/non-str collapses to '' (an underivable-key resolution is
    still a real decision worth recording; the empty key just means it cannot be
    joined to a build result later). `cleared` is stored as-is for audit — the
    caller is responsible for only recording genuine (cleared>0) resolutions.
    """
    out = outcome if outcome in VALID_OUTCOMES else 'expired'
    key = decision_key if isinstance(decision_key, str) else ''
    try:
        cleared_int = int(cleared)
    except (TypeError, ValueError):
        cleared_int = 0
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'kind': 'decision',
        'decision_key': key,
        'outcome': out,
        'actor': actor if isinstance(actor, str) and actor else '?',
        'cleared': cleared_int,
    }
    if notes:
        record['notes'] = str(notes)[:500]
    return _append(record, key)


def record_build_outcome(
    decision_key: str,
    build_outcome: str,
    *,
    pr_number: Optional[int] = None,
    actor: str = 'reconcile',
    notes: Optional[str] = None,
) -> bool:
    """Append one build-outcome row, joining a team build result onto the
    decision that authorized it (slice 2). Keyed by the SAME `decision_key` the
    decision row carries, so the govern loop can pair "Larry approved X" with
    "the team's build for X turned out <build_outcome>". Returns True on success,
    False on any failure. Never raises.

    `build_outcome` is normalized to a VALID_BUILD_OUTCOMES value; an unknown
    value is dropped (returns False) rather than coerced — unlike a decision
    row, a miscoded build result is worse than a missing one for the assessor,
    so we refuse it. An empty `decision_key` is refused for the same reason (a
    build outcome with no join target is useless). Callers gate on
    `has_build_outcome` first so a terminal PR is recorded at most once.
    """
    if not isinstance(decision_key, str) or not decision_key:
        _log('WARN', 'record_build_outcome refused: empty decision_key')
        return False
    if build_outcome not in VALID_BUILD_OUTCOMES:
        _log('WARN', f'record_build_outcome refused: unknown build_outcome='
                     f'{build_outcome!r} for key={decision_key!r}')
        return False
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'kind': 'build_outcome',
        'decision_key': decision_key,
        'build_outcome': build_outcome,
        'actor': actor if isinstance(actor, str) and actor else 'reconcile',
    }
    if isinstance(pr_number, int):
        record['pr_number'] = pr_number
    if notes:
        record['notes'] = str(notes)[:500]
    return _append(record, decision_key)


def _append(record: dict, key: str) -> bool:
    """Serialize one record to the ledger file. Returns True on success, False on
    any OSError. Never raises. Shared by record_decision / record_build_outcome
    so both go through one write path."""
    try:
        LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError as e:
        _log('WARN', f'ledger append failed for key={key!r}: {e}')
        return False
    return True


def has_build_outcome(decision_key: str) -> bool:
    """True if a build_outcome row already exists for this key. Empty key or
    missing/malformed file -> False. The reconciler's idempotency guard: a
    terminal PR's outcome is recorded at most once."""
    if not isinstance(decision_key, str) or not decision_key:
        return False
    for rec in _iter_records():
        if rec.get('kind') == 'build_outcome' \
                and rec.get('decision_key') == decision_key:
            return True
    return False


def _latest_build_outcome_records(records) -> dict:
    """{decision_key: newest build_outcome RECORD}. `records` is yielded
    oldest-first, so the last write per key wins — a later `merged` row
    supersedes an earlier `closed_unmerged` (readers take the newest row per
    key). Empty-key rows are ignored (a build outcome needs a join target)."""
    latest: dict = {}
    for rec in records:
        if rec.get('kind') != 'build_outcome':
            continue
        key = rec.get('decision_key')
        if isinstance(key, str) and key:
            latest[key] = rec
    return latest


def _is_recheckable(rec: Optional[dict], now: datetime) -> bool:
    """True if a build_outcome record is a `closed_unmerged` still inside the
    settle window (SETTLE_PERIOD_DAYS) — so a later reopen+merge can still
    supersede it. Any other outcome, or a closed_unmerged past the window, is
    final. An unparseable ts is treated as settled (don't churn on bad data)."""
    if not isinstance(rec, dict) or rec.get('build_outcome') != 'closed_unmerged':
        return False
    dt = _parse_ts(rec.get('ts'))
    if dt is None:
        return False
    return (now - dt) < timedelta(days=SETTLE_PERIOD_DAYS)


def latest_build_outcomes() -> dict:
    """{decision_key: newest build_outcome VALUE} across the whole ledger.
    Newest row wins per key (so a corrected `merged` is what reads back). The
    reconciler uses this to record only a SUPERSEDING verdict and never
    re-append an unchanged closed_unmerged row during its re-check window."""
    latest = _latest_build_outcome_records(_iter_records())
    return {k: r.get('build_outcome') for k, r in latest.items()}


def latest_build_outcome(decision_key: str) -> Optional[str]:
    """The newest build_outcome verdict for one key, or None. Newest row wins,
    so this returns the CORRECTED outcome after a supersede (not the earlier
    closed_unmerged)."""
    if not isinstance(decision_key, str) or not decision_key:
        return None
    return latest_build_outcomes().get(decision_key)


def is_recheckable_closed_unmerged(decision_key: str,
                                   now: Optional[datetime] = None) -> bool:
    """True if the key's NEWEST build_outcome is a `closed_unmerged` still
    inside the settle window — i.e. the reconciler should re-check it because a
    reopen+merge can still supersede. Backs the (b) re-check semantics: the
    'already has an outcome' skip must NOT block this correction."""
    if not isinstance(decision_key, str) or not decision_key:
        return False
    now = now or datetime.now(timezone.utc)
    rec = _latest_build_outcome_records(_iter_records()).get(decision_key)
    return _is_recheckable(rec, now)


def records_for_key(decision_key: str) -> list[dict]:
    """All records for one canonical decision key, oldest first. Empty key or
    missing/malformed file -> []. Backs the later build-result join (match a
    team outcome to the decision that authorized it)."""
    if not isinstance(decision_key, str) or not decision_key:
        return []
    return [r for r in _iter_records() if r.get('decision_key') == decision_key]


def outcome_counts(actor: Optional[str] = None) -> dict:
    """Tally of records by outcome, optionally filtered to one `actor`. Missing /
    malformed file -> {}. This is the primary read the govern-loop assessor will
    lean on ("how often has Larry approved vs rejected in area X"). Returns a
    plain dict (not a Counter) so callers/tests compare cleanly."""
    counts: Counter = Counter()
    for rec in _iter_records():
        if rec.get('kind') == 'build_outcome':
            continue  # only decision rows carry an `outcome` tally
        if actor is not None and rec.get('actor') != actor:
            continue
        out = rec.get('outcome')
        if isinstance(out, str) and out:
            counts[out] += 1
    return dict(counts)


def decision_keys_without_outcome(limit: int = 500,
                                  now: Optional[datetime] = None) -> list[str]:
    """Distinct decision_keys (from DECISION rows — kind 'decision', or missing
    for forward-compat with pre-`kind` rows) that still need a reconcile check —
    the reconciler's work-list. A key qualifies when it has NO build_outcome
    row, OR its NEWEST build_outcome is a re-checkable `closed_unmerged` still
    inside the settle window (a closed PR can be reopened + merged, so an
    un-settled abandonment is not yet final). Keys whose newest outcome is
    terminal-and-settled (merged / merged_regressed / a settled closed_unmerged)
    are excluded. Empty keys are excluded (a build outcome needs a join target).

    WHOLE-FILE SCAN (not a windowed one): build_outcome rows share this file, so
    the resolved-set MUST be computed over the entire ledger — a window-scoped
    scan could re-list a key resolved long ago whose decision row happens to
    fall in the recent window. The scan is O(rows); `limit` caps the RETURNED
    work-list (bounding the gh calls a single reconcile tick makes), not the
    rows examined. Oldest-first among returned keys. Missing/malformed file ->
    []."""
    now = now or datetime.now(timezone.utc)
    records = list(_iter_records())
    if not records:
        return []
    latest = _latest_build_outcome_records(records)
    out: list[str] = []
    seen: set = set()
    for rec in records:
        if rec.get('kind') == 'build_outcome':
            continue
        key = rec.get('decision_key')
        if not isinstance(key, str) or not key or key in seen:
            continue
        seen.add(key)
        bo = latest.get(key)
        if bo is not None and not _is_recheckable(bo, now):
            continue  # terminal-and-settled -> resolved, skip
        out.append(key)
        if len(out) >= limit:
            break
    return out


def read_recent(limit: int = 50) -> list[dict]:
    """Up to `limit` most-recent records (oldest first among the returned
    slice). limit<=0 or missing/malformed file -> []. For observability + ad-hoc
    inspection."""
    if limit <= 0:
        return []
    all_recs: list[dict] = list(_iter_records())
    if not all_recs:
        return []
    return all_recs[-limit:]


if __name__ == '__main__':
    # Tiny CLI for ad-hoc inspection during development / ops.
    if len(sys.argv) >= 2 and sys.argv[1] == 'recent':
        n = int(sys.argv[2]) if len(sys.argv) >= 3 else 10
        for r in read_recent(n):
            print(json.dumps(r, ensure_ascii=False))
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == 'counts':
        who = sys.argv[2] if len(sys.argv) >= 3 else None
        print(json.dumps(outcome_counts(who), ensure_ascii=False))
        sys.exit(0)
    print('usage: decision_outcome_ledger.py {recent [N] | counts [actor]}',
          file=sys.stderr)
    sys.exit(2)
