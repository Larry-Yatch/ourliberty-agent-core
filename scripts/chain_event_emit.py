#!/usr/bin/env python3
"""chain_event_emit.py — push-instrumented chain_events writer (E4.4e PR-A).

The chain_event_shipper (scripts/chain_event_shipper.py) polls five sources
and pumps events into Supabase. For event types whose source data is already
persisted on disk as flat lines (journalctl messages, structured log lines,
JSONL alert files), that polling architecture fits cleanly.

Some new event types — `approval_request` (E4.4e PR-A), `clarify_request`
(E4.4e PR-A), and `larry_action` (E4.4e PR-B's dashboard endpoint) —
carry nested-dict payloads that don't fit the shipper's existing kv-log
parsing. These are emitted by push instead of poll, directly to Supabase
from the code site that produced them.

This module is the shared push helper. Callers build a payload dict and
call `emit_event(...)`. The function computes a deterministic event_id
via the shipper's `compute_event_id` (so the table's primary key absorbs
double-emits the same way poll-source dedup works), sanitizes the payload
through the shipper's `sanitize_payload` (defense-in-depth credential
redaction), and upserts via supabase-py with `on_conflict='event_id',
ignore_duplicates=True` — the same shape `SupabaseSink.insert_rows` uses.

## Failure mode

If Supabase is unreachable or the supabase-py client raises, `emit_event`
logs a WARN and returns False. The caller is fire-and-forget; chain_event
loss on outage is acceptable for these event types because:

  - `approval_request` is reconstructible from beacon-pending-approvals.json
    (the bot's authoritative state file at ~/agents/state/).
  - `clarify_request` is reconstructible from outbox-notifier.log lines
    (`marker-notified beacon <- forge ... intent=clarify ...`).
  - `larry_action` is reconstructible from routing-events.jsonl (every
    inbox write is journalled there with actor + envelope path).

The shipper's local-spill `EventBuffer` is deliberately NOT reused here:
its `_trim_if_overflowing` does a read-then-rewrite that races under
concurrent writers, and the chain_event_shipper.py module restriction
imposed by the PR-A dispatch task forbids extending the drain loop to
read a second spill file. The right home for a buffered-fallback push
writer is a follow-up PR that can also reshape EventBuffer for safe
concurrent use.

## Why not use the shipper's `SupabaseSink` class directly

`SupabaseSink` is a thin wrapper around the guarded supabase client's `.table(...)`.
Reusing it from this module would require sharing a process-scope sink
instance with the shipper daemon, which doesn't exist in the bot/notifier
processes. Cheaper to instantiate the client lazily here, same way the
shipper itself does, and import only the pure helpers
(`compute_event_id`, `sanitize_payload`) which are side-effect-free.

Stdlib + supabase-py only.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

# Allow imports of sibling modules in scripts/.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import chain_event_shipper as ces  # noqa: E402

_LOGGER = logging.getLogger('chain_event_emit')

_CLIENT = None  # process-scope supabase client (lazy)


def _get_client():
    """Lazily build a supabase client. Returns None if unconfigured.

    Mirrors `SupabaseSink._ensure_client` but returns None on missing env
    rather than raising — push writers should degrade to WARN+drop, not
    crash the producer (which is typically a long-lived daemon or bot).
    """
    global _CLIENT
    # Test-isolation guard (2026-06-02 live-DB leak): never return a live
    # client during a test run. pytest sets PYTEST_CURRENT_TEST for every
    # test; OURLIBERTY_DISABLE_LIVE_EMIT is the explicit opt-out the
    # conftest fixture + test runners set. Without this, handler tests that
    # transitively call emit_event upsert fixture rows (real-001, ...) into
    # the live chain_events table. Runs before the cache return so a
    # pre-cached client is also ignored under test.
    if os.environ.get('PYTEST_CURRENT_TEST') or os.environ.get(
            'OURLIBERTY_DISABLE_LIVE_EMIT'):
        return None
    if _CLIENT is not None:
        return _CLIENT
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        return None
    # Pin the PostgREST request timeout (shipper's SUPABASE_TIMEOUT_SEC) so a
    # Supabase network black-hole fails fast. Without it, every push-emit site
    # in outbox_notifier blocks this synchronous upsert for supabase-py's
    # multi-tens-of-seconds default — and because those emits run inside the
    # single-threaded notifier's process_outbox loop, one stall serializes ALL
    # agents' notifications behind it. ces.build_client falls back to an
    # un-pinned client (never raises into emit_event) on supabase-py drift, and
    # builds through the guarded supabase_factory chokepoint — which raises
    # ImportError when supabase-py is absent (degrade to None, no live emit).
    try:
        _CLIENT = ces.build_client(url, key)
    except ImportError:
        return None
    return _CLIENT


def reset_client_for_testing() -> None:
    """Drop the cached client. Tests use this to swap in a mock."""
    global _CLIENT
    _CLIENT = None


def emit_event(
    *,
    event_type: str,
    agent: str,
    task_id: Optional[str],
    payload: dict[str, Any],
    ts: Optional[str] = None,
    pr_url: Optional[str] = None,
    cost_usd: Optional[float] = None,
    table: str = 'chain_events',
    logger: Optional[logging.Logger] = None,
    client: Optional[Any] = None,
) -> bool:
    """Direct-write a single chain_event row. Best-effort; never raises.

    Returns True on successful upsert, False on any failure (missing
    Supabase config, supabase-py not installed, network error, server
    error). The caller treats failure as event-loss; see module docstring
    for why that's acceptable for current push-instrumented event types.

    `event_type` is validated against `chain_event_shipper.KNOWN_EVENT_TYPES`
    so a typo here doesn't write a row the audit healer will flag. Unknown
    event_type → WARN + return False, no insert attempted.

    `ts` defaults to now (UTC ISO-8601). The event_id is deterministic
    over (task_id, event_type, ts) so two emitters racing on the same
    semantic event upsert to the same row.

    `client` arg is for tests; production callers omit it and the module
    builds + caches its own client.
    """
    log = logger or _LOGGER
    if event_type not in ces.KNOWN_EVENT_TYPES:
        log.warning(
            'emit_event: unknown event_type=%r — dropping (not inserted). '
            'Add to KNOWN_EVENT_TYPES in chain_event_shipper.py first.',
            event_type,
        )
        return False
    use_ts = ts or ces.datetime.now(ces.timezone.utc).isoformat()
    event_id = ces.compute_event_id(task_id, event_type, use_ts)
    row: dict[str, Any] = {
        'event_id': event_id,
        'ts': use_ts,
        'agent': agent,
        'event_type': event_type,
        'payload': ces.sanitize_payload(payload or {}),
    }
    if task_id:
        row['task_id'] = task_id
    if pr_url:
        row['pr_url'] = pr_url
    if cost_usd is not None:
        row['cost_usd'] = cost_usd
    cli = client if client is not None else _get_client()
    if cli is None:
        log.warning(
            'emit_event: Supabase client unavailable (SUPABASE_URL/'
            'SUPABASE_SERVICE_ROLE_KEY unset or supabase-py missing); '
            'dropping event_type=%s task_id=%s', event_type, task_id,
        )
        return False
    try:
        cli.table(table).upsert(
            [row], on_conflict='event_id', ignore_duplicates=True,
        ).execute()
        return True
    except Exception as e:  # noqa: BLE001 — push writer must not crash producer
        log.warning(
            'emit_event upsert failed (event_type=%s task_id=%s): %s: %s',
            event_type, task_id, type(e).__name__, e,
        )
        return False


def clear_approval_request(
    task_id: Optional[str],
    *,
    ts: Optional[str] = None,
    client: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Retire the pending dashboard `approval_request` row(s) for `task_id`.

    The resolution-side counterpart to `emit_event`. When a Telegram (or any
    self-committed) approve/reject/modify retires an approval in the bot's
    authoritative state, the dashboard's matching pending row keeps reading as
    "pending / blocking the chain" until `heal_stale_approvals` reconciles it
    on its 10-minute timer. Calling this at resolution time closes that window
    immediately, the same way the dashboard's own Approve button sets `read_at`.

    Mirror of `emit_event`'s single keyed write, not the healer's heuristic
    sweep: one PostgREST UPDATE that sets `read_at` on the still-unread
    `approval_request` rows for this exact `task_id` (identical filter shape to
    the dashboard action's claim in `dashboard_api`, minus the event_id pin the
    bot doesn't carry). The `read_at IS NULL` guard makes it idempotent, and
    because the resolution is authoritative (not an inferred-staleness signal)
    it skips the reversibility backup `heal_stale_approvals` writes — the same
    way the emit side takes no backup.

    Best-effort, fire-and-forget — same contract as `emit_event`: returns False
    (never raises) when the resolution can't be mirrored (no Supabase creds,
    supabase-py absent, test mode, or a Supabase error). `heal_stale_approvals`
    stays the backstop, so a dropped clear self-heals within ≤10 min. A missing
    client is the *expected* degrade (logged at DEBUG to keep test/local runs
    quiet); a real Supabase failure is logged at WARN. Returns True iff ≥1 row
    was cleared. `client`/`ts` are for tests; production callers omit them.
    """
    log = logger or _LOGGER
    if not task_id:
        return False
    cli = client if client is not None else _get_client()
    if cli is None:
        log.debug(
            'clear_approval_request: Supabase client unavailable '
            '(creds unset / supabase-py missing / test mode); leaving '
            'task_id=%s for heal_stale_approvals to reconcile', task_id,
        )
        return False
    use_ts = ts or ces.datetime.now(ces.timezone.utc).isoformat()
    try:
        resp = (
            cli.table('chain_events')
            .update({'read_at': use_ts}, count='exact')
            .eq('event_type', 'approval_request')
            .eq('task_id', task_id)
            .is_('read_at', 'null')
            .execute()
        )
        rows = getattr(resp, 'data', None) or []
        count = getattr(resp, 'count', None)
        return bool(rows) or bool(count and count > 0)
    except Exception as e:  # noqa: BLE001 — push writer must not crash producer
        log.warning(
            'clear_approval_request failed (task_id=%s): %s: %s — '
            'heal_stale_approvals will reconcile on its next tick',
            task_id, type(e).__name__, e,
        )
        return False
