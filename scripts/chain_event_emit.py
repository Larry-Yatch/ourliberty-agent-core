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
from decision_identity import canonical_decision_key  # noqa: E402

_LOGGER = logging.getLogger('chain_event_emit')

_CLIENT = None  # process-scope supabase client (lazy)

# Pagination bounds for list_open_event_task_ids (projection reconcile, §3a.1).
# PostgREST caps a single select at ~1000 rows, so the open-set scan pages. The
# needs-you event types are low-volume (a handful of paused sequences / a parked
# backlog), so the cap is generous: hitting it signals runaway rows, not normal
# load, and the reader refuses (returns None) rather than truncate.
_OPEN_EVENT_PAGE = 1000
_OPEN_EVENT_SCAN_CAP = 10000

# Event types that represent a "needs-Larry" item on the dashboard's read model.
# Change A stamps the canonical decision key onto these at emit time, and the
# resolve fan-out's C-leg (clear_decision) retires all of them by that key.
_LARRY_FACING_DECISION_TYPES = (
    'approval_request',
    'clarify_request',
    'needs_attention',
    'larry_alert',
    'escalation',
)


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
    id_extra: Optional[str] = None,
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
    over (task_id, event_type, ts[, id_extra]) so two emitters racing on
    the same semantic event upsert to the same row.

    `id_extra` is the optional `compute_event_id` disambiguator. A push
    writer that ALSO ships the same event via the poll path (the shipper's
    log-tail) must pass the exact (normalized ts, id_extra) the shipper's
    `parse_log_line` will derive, so the deterministic event_id matches and
    the PK absorbs the later poll-source row instead of double-writing. See
    `outbox_notifier._emit_auto_merge_chain_event` for the auto_merge parity
    case. Pure push-only event types (no log line shipped) omit it.

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
    event_id = ces.compute_event_id(task_id, event_type, use_ts, extra=id_extra)
    # Change A: stamp the canonical decision key onto Larry-facing rows so the
    # resolve fan-out's C-leg can clear them by key. Copy first so we never
    # mutate the caller's dict; derive from task_id+pr_url when not preset.
    payload = dict(payload or {})
    if event_type in _LARRY_FACING_DECISION_TYPES and not payload.get('decision_key'):
        _dk = canonical_decision_key(task_id, pr_url)
        if _dk:
            payload['decision_key'] = _dk
    row: dict[str, Any] = {
        'event_id': event_id,
        'ts': use_ts,
        'agent': agent,
        'event_type': event_type,
        'payload': ces.sanitize_payload(payload),
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


def clear_larry_alert(
    key: str,
    *,
    by: str = 'task_id',
    ts: Optional[str] = None,
    client: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Clear the pending `larry_alert` row(s) for ONE shipped alert line.

    The retraction counterpart to `larry_alerts.resolve_alert` (approval-sync
    §3a.2). A SINGLE keyed `read_at` UPDATE scoped to `event_type='larry_alert'`,
    matching exactly ONE column — `task_id` (the shipper's key, via
    `chain_event_shipper.alert_event_task_id`) OR `payload->>decision_key` —
    never both.

    Deliberately narrower than `clear_decision`'s dual-leg, decision-oriented
    clear: resolving one alert authorizes clearing only ITS shipped row. Firing
    the task_id leg on a decision_key value (or vice-versa) could mis-join onto
    an unrelated row whose `task_id` happens to equal that key — PR-coordinate
    keys are reused as both task_ids and decision_keys — a mis-join the codebase
    treats as worse than a missed join, so the two are kept strictly separate.

    `by` picks the column: 'task_id' (default) or 'decision_key'. Best-effort,
    fire-and-forget — returns the rows cleared (0 on no-match / no-client /
    error), never raises. `read_at IS NULL` makes it idempotent. `client`/`ts`
    are for tests; production callers omit them."""
    log = logger or _LOGGER
    if not key:
        return 0
    column = 'payload->>decision_key' if by == 'decision_key' else 'task_id'
    cli = client if client is not None else _get_client()
    if cli is None:
        log.debug(
            'clear_larry_alert: Supabase client unavailable (creds unset / '
            'supabase-py missing / test mode); leaving %s=%s uncleared', by, key,
        )
        return 0
    use_ts = ts or ces.datetime.now(ces.timezone.utc).isoformat()
    try:
        resp = (
            cli.table('chain_events')
            .update({'read_at': use_ts}, count='exact')
            .eq('event_type', 'larry_alert')
            .eq(column, key)
            .is_('read_at', 'null')
            .execute()
        )
        rows = getattr(resp, 'data', None) or []
        count = getattr(resp, 'count', None)
        return count if isinstance(count, int) else len(rows)
    except Exception as e:  # noqa: BLE001 — push writer must not crash producer
        log.warning(
            'clear_larry_alert failed (%s=%s): %s: %s', by, key,
            type(e).__name__, e,
        )
        return 0


def clear_decision(
    key: str,
    *,
    event_types: tuple[str, ...] = _LARRY_FACING_DECISION_TYPES,
    ts: Optional[str] = None,
    client: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Retire EVERY unread Larry-facing chain_event row for one decision `key`.

    The C-leg of the resolve fan-out (decision_resolve.resolve_decision, spec
    §2 Change B step 2). Generalizes `clear_approval_request` from "by task_id,
    approval_request only" to "by canonical decision key, across all
    Larry-facing types" — so resolving an item also clears its already-shipped
    `larry_alert`/`escalation`/`clarify_request` rows, not just the
    `approval_request` (closes North Star §5 gap 6).

    A stamped row carries the key in `payload->>'decision_key'`; a pre-Change-A
    row does not, but its `task_id` (after the same normalization the key uses)
    may equal `key`. So two narrow keyed UPDATEs are issued — one matching the
    `decision_key` payload field, one matching the `task_id` column — both
    guarded by `read_at IS NULL` (idempotent: a re-resolve clears nothing). The
    `task_id`-match leg uses the key verbatim, which is correct for the common
    case where the key IS the (normalized) task_id; a PR-coordinate key only
    matches task_id rows already shaped `pr-<repo>-<num>`, never a mis-join onto
    an unrelated row (spec §6 — a mis-join is worse than a missed join).

    Best-effort, fire-and-forget — same contract as `emit_event`: returns the
    count of rows cleared (0 on no-match, missing client, or any error); never
    raises. `heal_stale_approvals` remains the backstop for the dropped-write
    case. `client`/`ts` are for tests; production callers omit them.
    """
    log = logger or _LOGGER
    if not key:
        return 0
    cli = client if client is not None else _get_client()
    if cli is None:
        log.debug(
            'clear_decision: Supabase client unavailable (creds unset / '
            'supabase-py missing / test mode); leaving key=%s for '
            'heal_stale_approvals to reconcile', key,
        )
        return 0
    use_ts = ts or ces.datetime.now(ces.timezone.utc).isoformat()
    types = list(event_types)
    total = 0
    for column, is_json in (('decision_key', True), ('task_id', False)):
        try:
            q = (
                cli.table('chain_events')
                .update({'read_at': use_ts}, count='exact')
                .in_('event_type', types)
                .is_('read_at', 'null')
            )
            if is_json:
                q = q.eq('payload->>decision_key', key)
            else:
                q = q.eq('task_id', key)
            resp = q.execute()
            rows = getattr(resp, 'data', None) or []
            count = getattr(resp, 'count', None)
            total += count if isinstance(count, int) else len(rows)
        except Exception as e:  # noqa: BLE001 — push writer must not crash producer
            log.warning(
                'clear_decision %s-match failed (key=%s): %s: %s — '
                'heal_stale_approvals will reconcile on its next tick',
                column, key, type(e).__name__, e,
            )
    return total


# ---------- projection reconcile (approval-sync Phase 3a §3a.1, PR-2) ----------
#
# The two "needs-you stragglers" (`parked_capture`, `sequence_needs_you`) aren't
# push-emitted at a single transition — their producers (heal_missions_card_gc's
# capture sweep, build_sequence_advancer's tick) already iterate the full
# desired set each cycle. So rather than diff transitions, they hand that set to
# `reconcile_open_events`, which makes chain_events match it: emit rows that are
# newly-desired, clear rows that are no longer desired. Emit is idempotent (a
# still-desired row is already open, so it is skipped), so re-running each tick
# is cheap and self-healing. This is the read-model twin of the resolve fan-out.


def list_open_event_task_ids(
    event_type: str,
    *,
    client: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[set]:
    """The task_ids of every UNREAD (`read_at IS NULL`) row of `event_type`.

    The read half of `reconcile_open_events`. Returns None (not an empty set)
    when the set can't be determined — no client, a Supabase error, OR an
    open set so large it exceeds the scan cap — so the caller SKIPS the
    reconcile rather than treat "unknown" as "nothing open" and blind-emit
    duplicates / wrongly clear the rows it couldn't see. Best-effort; never
    raises.

    PAGINATED (PostgREST caps a single select at ~1000 rows): an unpaginated
    read feeding a clear-derivation would silently truncate and strand every
    open row beyond the cap — the repo's known unpaginated-select class. Pages
    through the full open set; if it exceeds `_OPEN_EVENT_SCAN_CAP` (itself an
    anomaly for these low-volume needs-you types), that's surfaced as None +
    a WARN, never a silent truncation."""
    log = logger or _LOGGER
    cli = client if client is not None else _get_client()
    if cli is None:
        return None
    task_ids: set = set()
    start = 0
    try:
        while start < _OPEN_EVENT_SCAN_CAP:
            end = min(start + _OPEN_EVENT_PAGE - 1, _OPEN_EVENT_SCAN_CAP - 1)
            resp = (
                cli.table('chain_events')
                .select('task_id')
                .eq('event_type', event_type)
                .is_('read_at', 'null')
                .range(start, end)
                .execute()
            )
            rows = getattr(resp, 'data', None) or []
            task_ids |= {r.get('task_id') for r in rows
                         if isinstance(r, dict) and r.get('task_id')}
            if len(rows) <= (end - start):  # short page → last page reached
                return task_ids
            start = end + 1
        # Fell out of the loop without a short page → the open set is at/over
        # the cap. A truncated set can't safely drive clears, so refuse.
        log.warning(
            'list_open_event_task_ids: open %s set exceeds scan cap %d — '
            'skipping reconcile this cycle (investigate runaway open rows)',
            event_type, _OPEN_EVENT_SCAN_CAP,
        )
        return None
    except Exception as e:  # noqa: BLE001 — push reader must not crash producer
        log.warning(
            'list_open_event_task_ids failed (event_type=%s): %s: %s',
            event_type, type(e).__name__, e,
        )
        return None


def clear_event_by_task_id(
    task_id: str,
    *,
    event_type: str,
    ts: Optional[str] = None,
    client: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Clear (`read_at`) the pending row(s) for ONE task_id of ONE event_type.

    A SINGLE keyed UPDATE scoped to `event_type` + `task_id` + `read_at IS NULL`
    — the same narrow, single-column, no-mis-join shape as `clear_larry_alert`,
    generalized to any projected event_type (`parked_capture`,
    `sequence_needs_you`). Best-effort; returns rows cleared (0 on
    no-match/no-client/error); never raises. `client`/`ts` are for tests."""
    log = logger or _LOGGER
    if not task_id or not event_type:
        return 0
    cli = client if client is not None else _get_client()
    if cli is None:
        return 0
    use_ts = ts or ces.datetime.now(ces.timezone.utc).isoformat()
    try:
        resp = (
            cli.table('chain_events')
            .update({'read_at': use_ts}, count='exact')
            .eq('event_type', event_type)
            .eq('task_id', task_id)
            .is_('read_at', 'null')
            .execute()
        )
        rows = getattr(resp, 'data', None) or []
        count = getattr(resp, 'count', None)
        return count if isinstance(count, int) else len(rows)
    except Exception as e:  # noqa: BLE001 — push writer must not crash producer
        log.warning(
            'clear_event_by_task_id failed (event_type=%s task_id=%s): %s: %s',
            event_type, task_id, type(e).__name__, e,
        )
        return 0


def reconcile_open_events(
    event_type: str,
    desired: dict,
    *,
    agent: str,
    client: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> dict:
    """Make the open `event_type` rows match `desired` — emit new, clear gone.

    `desired` maps task_id -> {'ts': <stable iso or None>, 'payload': {...}}. The
    stable ts (a paused-at / captured-at timestamp) keeps the deterministic
    event_id fixed across ticks so a re-emit is absorbed by the PK; the
    open-set skip below is the primary dedup and the stable ts is the backstop.

    Fetches the currently-open task_ids ONCE, then:
      * emits every desired task_id NOT already open (idempotent: a still-open
        one is skipped, so steady state issues zero writes);
      * clears every open task_id NOT in `desired` (the item resolved).

    If the open set can't be read (no client / Supabase error) the whole
    reconcile is SKIPPED — never blind-emit against an unknown open set, which
    would duplicate rows. Best-effort; never raises. Returns a
    {emitted, cleared, skipped} summary. `client` is shared across the fetch,
    emits, and clears (one connection); tests pass a fake."""
    log = logger or _LOGGER
    cli = client if client is not None else _get_client()
    if cli is None:
        return {'emitted': 0, 'cleared': 0, 'skipped': True}
    open_ids = list_open_event_task_ids(event_type, client=cli, logger=logger)
    if open_ids is None:
        return {'emitted': 0, 'cleared': 0, 'skipped': True}
    desired = desired or {}
    emitted = 0
    for tid, spec in desired.items():
        if not tid or tid in open_ids:
            continue
        spec = spec or {}
        if emit_event(
            event_type=event_type, agent=agent, task_id=tid,
            payload=spec.get('payload') or {}, ts=spec.get('ts'),
            client=cli, logger=log,
        ):
            emitted += 1
    cleared = 0
    for tid in open_ids - set(desired):
        cleared += clear_event_by_task_id(
            tid, event_type=event_type, client=cli, logger=log)
    return {'emitted': emitted, 'cleared': cleared, 'skipped': False}
