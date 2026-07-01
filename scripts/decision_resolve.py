#!/usr/bin/env python3
"""decision_resolve.py — the single resolve fan-out across the four needs-Larry
stores (Approval-Sync Phase 2, spec docs/approval-sync-phase2-spec.md §2 Change B).

The same logical "needs-Larry" item is authored into FOUR stores under the one
canonical key `decision_identity.canonical_decision_key` produces:

  P — beacon-pending-approvals.json   (the approval queue Beacon pops)
  C — Supabase `chain_events`          (the dashboard's read model)
  E — for-larry-escalations.json       (the `/where-we-are` "Waiting on You" feed)
  A — larry-alerts.jsonl               (the DM/alert queue)

Before Phase 2, resolving the item in ONE store did not fan out to the others,
so a dashboard-approve left the Telegram queue / escalation feed / alert line
reading "still waiting" until the 10-minute `heal_stale_approvals` healer
reconciled them after the fact. `resolve_decision(key, outcome)` makes the
resolution **synchronous and total**: one call clears the item from all four
stores. Both surfaces — the Telegram approve/reject path and the dashboard's
`/api/larry/action` droplet handler — call this same primitive, so there is one
resolution code path, not two divergent ones.

## Doctrine (spec §6)

- **Exact-key only.** Each leg matches the canonical key EXACTLY; a mis-join is
  worse than a missed join (a missed join is caught by the Phase 1 healer
  backstop; a mis-join silently drops an *unrelated* decision). No partial /
  prefix matching anywhere.
- **Best-effort per leg, total intent.** Every leg runs under its own failure
  isolation: one store being unreachable (Supabase down, a corrupt JSON file)
  logs and continues — it never aborts the other three. This mirrors Phase 1's
  `contextlib.suppress` + structured-log discipline. The Phase 1 healer remains
  the backstop for whatever a degraded leg could not clear.
- **Idempotent.** Calling twice on the same key is a no-op the second time:
  each store's clear is guarded (an already-resolved P entry is in history not
  pending; C's update has a `read_at IS NULL` guard; E's `list_open` only
  returns unresolved rows; A's lines are already gone). No exceptions, no
  double-write.

## Recursion note

The P-leg calls `beacon_approval_handler.resolve` — the resolution PRIMITIVE.
Therefore the *callers* of `bah.resolve` (the Telegram handler, the dashboard
droplet handler) must route through `resolve_decision`, NOT the other way
around — otherwise the fan-out would call itself. `bah.resolve` stays the
single-store primitive; `resolve_decision` is the multi-store orchestrator that
wraps it.

Stdlib + the four sibling store modules only. Pure-Python; the only I/O is each
leg's own store write.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from decision_identity import canonical_decision_key  # noqa: E402

_LOGGER = logging.getLogger('decision_resolve')

VALID_OUTCOMES = ('approved', 'rejected', 'modified', 'expired')


# -------------------- per-record key derivation --------------------

def _pending_entry_key(entry: dict[str, Any]) -> Optional[str]:
    """Canonical key for a beacon-pending-approvals entry: the stamped
    `decision_key` (Change A) if present, else derived from `id` + the
    dispatch_payload's `pr_url`."""
    stamped = entry.get('decision_key')
    if isinstance(stamped, str) and stamped:
        return stamped
    payload = entry.get('dispatch_payload')
    pr_url = payload.get('pr_url') if isinstance(payload, dict) else None
    return canonical_decision_key(entry.get('id'), pr_url)


def _escalation_row_key(row: dict[str, Any]) -> Optional[str]:
    """Canonical key for a for-larry-escalations row: the stamped `decision_key`
    if present, else derived from `id`/`task_id` + `pr_url`."""
    stamped = row.get('decision_key')
    if isinstance(stamped, str) and stamped:
        return stamped
    return canonical_decision_key(row.get('id') or row.get('task_id'), row.get('pr_url'))


# -------------------- the four legs --------------------

def _resolve_pending(key: str, outcome: str, note: str, log: logging.Logger) -> int:
    """P — beacon-pending-approvals.json. Resolve every pending entry whose
    canonical key matches, via the existing `bah.resolve` primitive (so the
    move-to-history + dashboard-pending clear + audit all happen the one
    blessed way). Returns the count resolved."""
    try:
        import beacon_approval_handler as bah
    except Exception as e:  # noqa: BLE001
        log.warning('resolve_decision P-leg import failed (key=%s): %s: %s',
                    key, type(e).__name__, e)
        return 0
    try:
        state = bah.load_state()
        ids = [
            entry.get('id')
            for entry in state.get('pending', [])
            if _pending_entry_key(entry) == key and entry.get('id')
        ]
    except Exception as e:  # noqa: BLE001
        log.warning('resolve_decision P-leg load failed (key=%s): %s: %s',
                    key, type(e).__name__, e)
        return 0
    resolved = 0
    for approval_id in ids:
        try:
            if bah.resolve(approval_id, outcome, note=note) is not None:
                resolved += 1
        except Exception as e:  # noqa: BLE001
            log.warning('resolve_decision P-leg resolve(%s) failed: %s: %s',
                        approval_id, type(e).__name__, e)
    return resolved


def _resolve_chain_events(key: str, client: Any, log: logging.Logger) -> int:
    """C — Supabase chain_events. Set `read_at` on all unread Larry-facing rows
    for this key (across event types, not just approval_request). Returns the
    count cleared."""
    try:
        import chain_event_emit as cee
    except Exception as e:  # noqa: BLE001
        log.warning('resolve_decision C-leg import failed (key=%s): %s: %s',
                    key, type(e).__name__, e)
        return 0
    try:
        return cee.clear_decision(key, client=client)
    except Exception as e:  # noqa: BLE001
        log.warning('resolve_decision C-leg failed (key=%s): %s: %s',
                    key, type(e).__name__, e)
        return 0


def _resolve_escalations(key: str, log: logging.Logger) -> int:
    """E — for-larry-escalations.json. Flip `resolved: true` on every OPEN
    record whose canonical key matches. Returns the count cleared."""
    try:
        import for_larry_escalations as fle
    except Exception as e:  # noqa: BLE001
        log.warning('resolve_decision E-leg import failed (key=%s): %s: %s',
                    key, type(e).__name__, e)
        return 0
    cleared = 0
    try:
        for row in fle.list_open():
            if _escalation_row_key(row) == key:
                rid = row.get('id')
                if rid and fle.clear(rid):
                    cleared += 1
    except Exception as e:  # noqa: BLE001
        log.warning('resolve_decision E-leg failed (key=%s): %s: %s',
                    key, type(e).__name__, e)
    return cleared


def _resolve_alerts(key: str, log: logging.Logger) -> int:
    """A — larry-alerts.jsonl. Retract escalate/approval-request lines stamped
    with this key. Returns the count retracted."""
    try:
        import larry_alerts as la
    except Exception as e:  # noqa: BLE001
        log.warning('resolve_decision A-leg import failed (key=%s): %s: %s',
                    key, type(e).__name__, e)
        return 0
    try:
        return la.resolve_alert_by_decision_key(key)
    except Exception as e:  # noqa: BLE001
        log.warning('resolve_decision A-leg failed (key=%s): %s: %s',
                    key, type(e).__name__, e)
        return 0


# -------------------- the fan-out --------------------

def resolve_decision(
    key: Optional[str],
    outcome: str,
    *,
    actor: Optional[str] = None,
    note: str = '',
    chain_client: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> dict[str, Any]:
    """Resolve one decision across all four needs-Larry stores (P/C/E/A).

    `key` is a `canonical_decision_key` value. `outcome` ∈ VALID_OUTCOMES.
    `actor`/`note` annotate the resolution (note is threaded into the P-leg's
    `bah.resolve`). `chain_client` is a test seam for the Supabase C-leg;
    production callers omit it and `chain_event_emit` builds its own.

    Each leg is best-effort and isolated — one failing never aborts the others
    (spec §6). Idempotent: a second call clears nothing. Never raises.

    Returns a result dict::

        {'key': <key>, 'outcome': <outcome>,
         'pending': n, 'chain_events': n, 'escalations': n, 'alerts': n,
         'total': n}

    A `None`/empty `key` is the underivable case (spec §1.1): no cross-store
    join is attempted — returns an all-zero result. A missed join here is the
    safe direction; the Phase 1 healer backstops it.
    """
    log = logger or _LOGGER
    result = {
        'key': key, 'outcome': outcome,
        'pending': 0, 'chain_events': 0, 'escalations': 0, 'alerts': 0,
        'total': 0,
    }
    if not key:
        log.debug('resolve_decision: no key — skipping cross-store fan-out')
        return result
    if outcome not in VALID_OUTCOMES:
        log.warning('resolve_decision: invalid outcome=%r for key=%s — '
                    'skipping', outcome, key)
        return result
    result['pending'] = _resolve_pending(key, outcome, note, log)
    result['chain_events'] = _resolve_chain_events(key, chain_client, log)
    result['escalations'] = _resolve_escalations(key, log)
    result['alerts'] = _resolve_alerts(key, log)
    result['total'] = (
        result['pending'] + result['chain_events']
        + result['escalations'] + result['alerts']
    )
    log.info(
        'resolve_decision key=%s outcome=%s actor=%s cleared P=%d C=%d E=%d A=%d',
        key, outcome, actor, result['pending'], result['chain_events'],
        result['escalations'], result['alerts'],
    )
    return result


def resolve_decision_for_entry(
    *,
    task_id: Optional[str] = None,
    pr_url: Optional[str] = None,
    outcome: str,
    actor: Optional[str] = None,
    note: str = '',
    chain_client: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> dict[str, Any]:
    """Convenience wrapper for callers that hold a `task_id`/`pr_url` rather than
    a precomputed key — derives the canonical key, then fans out. Used by the
    Telegram/dashboard call sites that resolve by the raw approval id."""
    key = canonical_decision_key(task_id, pr_url)
    return resolve_decision(
        key, outcome, actor=actor, note=note,
        chain_client=chain_client, logger=logger,
    )
