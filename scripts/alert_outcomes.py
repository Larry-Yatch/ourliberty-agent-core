#!/usr/bin/env python3
"""alert_outcomes.py — append-only ledger of what happened to each alert.

XIV-b slice A (the data-capture half of the tier-4 alert write-back loop).

WHY THIS EXISTS
---------------
We send Larry alerts and never find out whether any of them were worth
sending. Without that signal, no one — human or loop — can tell a useful
alert source from noise, so the alert set only ever grows. This ledger is the
feedback channel: one row per alert outcome, keyed to the alert's chain_events
`event_id`, with `source` / `subject` / `tier` denormalized so the aggregate
("alerts from X get marked not-useful 9 times in 10") is a straight group-by.

BOUNDARY (deliberate)
---------------------
This is a SIBLING of `larry-alerts.jsonl`, never a mutation of it. The
emission queue is append-only and read by line-index cursors (beacon + medic);
rewriting it to record an outcome would shift those cursors and drop alerts.

WHAT IS NOT HERE (slice B)
--------------------------
The `lapsed` sweep — marking an alert Larry never engaged with — is
deliberately not built. Its window length is the one number that genuinely
needs observed data, and nothing has been observing. `lapsed` is reserved in
VALID_OUTCOMES so the schema does not change when the sweep lands.

Stdlib only. Every writer is fire-and-forget: never raises.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import file_lock
from test_isolation_guard import refuse_under_test

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT')
                   or Path.home() / 'agents')
OUTCOMES_FILE = AGENTS_ROOT / 'blackboard' / 'alert-outcomes.jsonl'

# Bounded wait for the sidecar flock, mirroring larry_alerts._locked_append.
_APPEND_LOCK_TIMEOUT_SEC = 5.0

# `acted`        — Larry clicked "Handled it": the alert prompted real work.
# `not_useful`   — Larry clicked "Not useful": this alert should not have been
#                  sent. THE signal the whole loop exists to collect.
# `auto_resolved`— the system healed the condition and retracted the alert
#                  before Larry engaged. Not a judgment on usefulness, and
#                  must not be counted as an ignore.
# `lapsed`       — reserved for slice B's sweep (see module docstring).
VALID_OUTCOMES = ('acted', 'not_useful', 'auto_resolved', 'lapsed')


def alert_event_id(record: dict) -> Optional[str]:
    """The chain_events `event_id` an alert row is shipped under.

    Delegates to `chain_event_shipper` rather than recomputing the hash, so
    this can never drift from the id the shipper actually wrote — the join
    key between this ledger and the alert is only worth anything if both
    sides derive it identically (the same discipline `alert_event_task_id`
    already enforces for the task_id half).

    Returns None when the shipper is unimportable or the record is malformed;
    the caller records the outcome without an event_id rather than dropping it.
    """
    try:
        import chain_event_shipper as ces
        ts = record.get('ts')
        if not ts:
            return None
        return ces.compute_event_id(
            ces.alert_event_task_id(record), 'larry_alert', ts,
        )
    except Exception:  # noqa: BLE001 — keying is best-effort, never fatal
        return None


def _locked_append(line: str) -> bool:
    """Append one serialized line under a sidecar flock. Never raises.

    Same shape as `larry_alerts._locked_append`: O_APPEND keeps the write from
    tearing, the flock excludes any future compaction pass, and a wedged lock
    degrades to a plain append rather than losing the row.
    """
    try:
        OUTCOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_path = file_lock.sidecar_lock_path(OUTCOMES_FILE)
        try:
            with file_lock.exclusive_lock(
                lock_path, timeout=_APPEND_LOCK_TIMEOUT_SEC,
            ):
                with open(OUTCOMES_FILE, 'a', encoding='utf-8') as f:
                    f.write(line)
        except file_lock.LockTimeout:
            with open(OUTCOMES_FILE, 'a', encoding='utf-8') as f:
                f.write(line)
    except OSError:
        return False
    return True


def record_outcome(
    outcome: str,
    *,
    event_id: Optional[str] = None,
    source: Optional[str] = None,
    subject: Optional[str] = None,
    tier: Optional[str] = None,
    tier_source: Optional[str] = None,
    task_id: Optional[str] = None,
    actor: str = 'larry',
    ts: Optional[str] = None,
) -> bool:
    """Append one outcome row. Returns True on write, False otherwise.

    Never raises — every caller is on a path (a dashboard click handler, an
    alert retraction) where a bookkeeping failure must not take down the
    action itself. An unrecognized `outcome` is refused rather than written,
    so a typo can't silently create a fourth bucket the aggregate ignores.

    Idempotency is the CALLER's: the dashboard path is serialized by the
    atomic `read_at` claim on the chain_events row (a second click 409s
    before reaching here), and the auto-resolve path fires once per retracted
    line, which is removed in the same locked pass.
    """
    refuse_under_test('alert-outcomes')
    if outcome not in VALID_OUTCOMES:
        return False
    record: dict[str, Any] = {
        'ts': ts or datetime.now(timezone.utc).isoformat(),
        'outcome': outcome,
        'actor': actor,
    }
    # Denormalized analytics columns — the aggregate groups on these, and the
    # alert row they came from may be trimmed by retention long before the
    # outcome is read. Omitted when unknown rather than written as null.
    for key, value in (
        ('event_id', event_id), ('source', source), ('subject', subject),
        ('tier', tier), ('tier_source', tier_source), ('task_id', task_id),
    ):
        if value:
            record[key] = value
    try:
        line = json.dumps(record, ensure_ascii=False) + '\n'
    except (TypeError, ValueError):
        return False
    return _locked_append(line)


def record_outcome_for_alert(
    alert: dict,
    outcome: str,
    *,
    actor: str = 'larry',
    event_id: Optional[str] = None,
) -> bool:
    """Record an outcome from a raw `larry-alerts.jsonl` record.

    Lifts source / subject / tier / tier_source / task_id off the alert so
    callers holding the row don't each re-derive them. `event_id` may be
    supplied by a caller that already has it (the dashboard knows the
    chain_events id it acted on); otherwise it is derived.
    """
    if not isinstance(alert, dict):
        return False
    return record_outcome(
        outcome,
        event_id=event_id or alert_event_id(alert),
        source=alert.get('source'),
        subject=alert.get('subject'),
        tier=alert.get('tier'),
        tier_source=alert.get('tier_source'),
        task_id=alert.get('task_id'),
        actor=actor,
    )


def read_outcomes(path: Optional[Path] = None) -> list:
    """Every parseable outcome row, oldest first. [] on any read error.

    Malformed lines are skipped, not fatal — the ledger is append-only from
    several processes and one torn line must not blind the aggregate.
    """
    target = path if path is not None else OUTCOMES_FILE
    out: list = []
    try:
        if not target.exists():
            return []
        with open(target, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


def aggregate(
    outcomes: Optional[list] = None,
    *,
    path: Optional[Path] = None,
) -> dict:
    """Reduce the ledger to per-(source, subject, tier) outcome counts.

    Shape: {'source::subject': {'source', 'subject', 'tier', 'total',
    'acted', 'not_useful', 'auto_resolved', 'lapsed'}}. The read-model the
    "is this alert worth sending" question is answered from; `auto_resolved`
    is reported alongside but is NOT a usefulness judgment (see
    VALID_OUTCOMES).
    """
    rows = outcomes if outcomes is not None else read_outcomes(path)
    agg: dict = {}
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        outcome = rec.get('outcome')
        if outcome not in VALID_OUTCOMES:
            continue
        source = rec.get('source') or '?'
        subject = rec.get('subject') or ''
        key = f'{source}::{subject}'
        entry = agg.get(key)
        if entry is None:
            entry = {
                'source': source, 'subject': subject,
                'tier': rec.get('tier'), 'total': 0,
            }
            for name in VALID_OUTCOMES:
                entry[name] = 0
            agg[key] = entry
        entry[outcome] += 1
        entry['total'] += 1
    return agg
