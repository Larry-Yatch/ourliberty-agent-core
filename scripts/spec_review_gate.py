#!/usr/bin/env python3
"""spec_review_gate.py — intercept + deferred-stamp pickup for the spec gauntlet.

Intercept slice of agents/beacon/specs/spec-gauntlet-gate.md (§3.1, §3.5). This
module owns the *generic, host-agnostic* machinery of the three cheap file-based
hops (§2); the actual chat/DM I/O and the legacy stamp path stay owned by the
host daemons (bot + notifier), because those differ per site (the bot sends
Telegram DMs directly; the notifier queues through larry_alerts).

Two halves:

1. **`intercept(payload, site)`** — called at each gated stamp site right before
   its `trust_decision`. When the gate is enabled AND `site` is gated, it writes
   the payload to a durable spool (``pending/<task_id>.json``) plus a routing
   sidecar (``routing/<task_id>.json`` — the chat routing the runner's conclusion
   file does NOT carry) and returns ``'spooled'``; the caller then sends the
   instant ack so Larry knows in real time a gauntlet started. A restart replay
   of the same (task_id, payload_hash) returns ``'duplicate'`` (AC-6 — no double
   spool, no double ack). Gate off / site not gated returns ``'disabled'`` and
   the caller runs the byte-identical legacy stamp path, labelling the card
   ``gauntlet: disabled`` (§3.5). Pure file writes — microseconds, no subprocess,
   safe inside the daemons' single-threaded loops.

2. **Deferred-stamp pickup** — ``collect_concluded(sites)`` lets each host's
   existing poll tick scan ``concluded/`` for gauntlets that finished for a site
   it owns and have not yet been stamped; the host runs its *unchanged* legacy
   stamp path on the FINAL (post-revision) payload with ``build_digest`` appended
   to the summary, then calls ``mark_stamped`` so the next poll leaves it alone.
   Trust evaluates the final payload (§3.1). Stamp-then-mark ordering: an
   interrupted pickup can re-stamp (rare) but never *loses* an approval — the
   spec's stated priority ("delay an approval, never lose one", §3.4).

The spool schema (``{payload, payload_hash, site, created_at}``) is exactly what
``spec_review_runner`` reads; the runner keys the gauntlet on the pending file's
stem, so the sanitized ``task_id`` here is the same id the conclusion lands under.

Fail-safe reads throughout: a missing/unreadable/malformed spool, routing, or
conclusion artifact is skipped, never raised — a corrupt file can never wedge a
daemon loop.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

import atomic_io
import spec_review_config as config
import spec_review_conclusion as conclusion

# All state lives under the same dir tree the runner + conclusion predicate use,
# so redirecting OURLIBERTY_AGENTS_ROOT (or the module attrs, in tests) moves the
# whole gauntlet consistently.
STATE_DIR = conclusion.STATE_DIR
PENDING_DIR = conclusion.PENDING_DIR
CONCLUDED_DIR = conclusion.CONCLUDED_DIR
ROUTING_DIR = STATE_DIR / 'routing'
STAMPED_DIR = STATE_DIR / 'stamped'

# The instant ack the host sends on the chat path when a gauntlet starts (§3.1).
ACK_MESSAGE = (
    '🔬 Spec entering antagonistic review — approval card follows.'
)
# The one label a card carries while the kill switch is off (§3.5): otherwise
# byte-identical legacy behaviour, but Larry can see the gate exists and is off.
DISABLED_LABEL = 'gauntlet: disabled'

# Mirrors safe_write_inbox.sanitize_component's intent (keep a task_id from
# escaping its dir via `/`, `..`, control bytes) without coupling this hot-loop
# module to that import chain. The normal id shape ([A-Za-z0-9._-]) is untouched.
_UNSAFE_RE = re.compile(r'[^A-Za-z0-9._-]')


def _safe_task_id(task_id: Any) -> str:
    if not isinstance(task_id, str) or not task_id:
        return 'unknown'
    cleaned = _UNSAFE_RE.sub('-', task_id)
    if cleaned.strip('.') == '':
        return 'unknown'
    return cleaned


def payload_hash(payload: Any) -> str:
    """Stable content hash of the payload — the dedup key (AC-6). A re-spool of
    the *same* body hashes identically (duplicate); a genuinely-changed spec
    hashes differently and is NOT masked by a stale conclusion."""
    try:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = repr(payload)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _ensure_dirs() -> None:
    for d in (PENDING_DIR, ROUTING_DIR, CONCLUDED_DIR, STAMPED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _already_seen(task_id: str, phash: str) -> bool:
    """True iff this exact (task_id, payload_hash) is already spooled OR already
    concluded — the host-restart replay guard (AC-6). A pending or concluded
    artifact for a *different* body does not count."""
    concluded = _read_json(CONCLUDED_DIR / f'{task_id}.json')
    if (concluded is not None
            and concluded.get('payload_hash') == phash
            and concluded.get('terminal_state') in conclusion.TERMINAL_STATES):
        return True
    pending = _read_json(PENDING_DIR / f'{task_id}.json')
    if pending is not None and pending.get('payload_hash') == phash:
        return True
    return False


def intercept(
    payload: dict[str, Any],
    site: str,
    *,
    chat_id: Optional[int] = None,
    reply_chat_id: Optional[int] = None,
    meta: Optional[dict[str, Any]] = None,
) -> str:
    """Spool the payload for antagonistic review. Returns one of:

      - ``'disabled'``  — gate off or ``site`` not gated; caller runs the legacy
        stamp path (labelled ``gauntlet: disabled``). Read FRESH per call, so the
        override flips with zero daemon restarts (§3.6, AC-3).
      - ``'duplicate'`` — this exact (task_id, payload_hash) is already spooled or
        concluded; caller acks nothing and returns (host-restart replay, AC-6).
      - ``'spooled'``   — written to ``pending/`` + ``routing/``; caller sends the
        instant chat ack and returns without stamping now.

    ``chat_id`` / ``reply_chat_id`` / ``meta`` are recorded in the routing sidecar
    so the deferred pickup can reconstruct the site's stamp call — the conclusion
    file the runner writes carries none of this routing context.
    """
    if not config.is_enabled() or site not in config.gated_sites():
        return 'disabled'

    task_id = _safe_task_id(payload.get('task_id'))
    phash = payload_hash(payload)
    if _already_seen(task_id, phash):
        return 'duplicate'

    _ensure_dirs()
    atomic_io.atomic_write_json(
        PENDING_DIR / f'{task_id}.json',
        {
            'payload': payload,
            'payload_hash': phash,
            'site': site,
            'created_at': time.time(),
        },
        indent=2,
    )
    atomic_io.atomic_write_json(
        ROUTING_DIR / f'{task_id}.json',
        {
            'task_id': task_id,
            'site': site,
            'chat_id': chat_id,
            'reply_chat_id': reply_chat_id,
            'meta': meta or {},
        },
        indent=2,
    )
    return 'spooled'


def _finding_lines(conc: dict[str, Any]) -> list[str]:
    """One line per contested/advisory finding for the digest (§3.5)."""
    lines: list[str] = []
    for kind, key in (('contested', 'contested_findings'),
                      ('advisory', 'advisory_findings')):
        for f in conc.get(key) or []:
            if not isinstance(f, dict):
                continue
            lens = f.get('lens', '?')
            claim = (f.get('claim') or f.get('suggested_change') or '').strip()
            lines.append(f'  · {kind} [{lens}] {claim}'.rstrip())
    return lines


def build_digest(conc: dict[str, Any]) -> str:
    """The challenge digest appended to the approval card summary (§3.5):

        Gauntlet: <state> · N rounds · X blocking → Y resolved, Z contested · W advisory

    followed by one line per contested/advisory finding. Every concluded card
    carries exactly one terminal state, so a missing digest is impossible and
    ``errored`` is legible rather than silent.
    """
    state = conc.get('terminal_state', 'errored')
    rounds = conc.get('rounds', 0)
    resolved = conc.get('blocking_resolved_count', 0) or 0
    contested = conc.get('contested_count')
    if contested is None:
        contested = len(conc.get('contested_findings') or [])
    advisory = len(conc.get('advisory_findings') or [])
    blocking = resolved + contested
    header = (
        f'Gauntlet: {state} · {rounds} rounds · '
        f'{blocking} blocking → {resolved} resolved, {contested} contested · '
        f'{advisory} advisory'
    )
    # Surface the failure reason inline so errored/incomplete is legible.
    reason = conc.get('reason') or conc.get('error')
    if state in ('errored', 'incomplete') and reason:
        header += f' ({reason})'
    return '\n'.join([header, *_finding_lines(conc)])


def with_digest(payload: dict[str, Any], digest: str) -> dict[str, Any]:
    """A copy of ``payload`` with the digest appended to its summary — the card
    Larry reads renders the summary (§3.5). Never mutates the caller's dict."""
    out = dict(payload)
    summary = out.get('summary') or ''
    out['summary'] = f'{summary}\n\n{digest}'.strip() if summary else digest
    return out


def with_disabled_label(payload: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``payload`` with the kill-switch-off label (§3.5)."""
    return with_digest(payload, DISABLED_LABEL)


def collect_concluded(sites: list[str]) -> list[tuple[str, dict, dict]]:
    """Return ``(task_id, conclusion, routing)`` for every concluded gauntlet that
    (a) belongs to one of ``sites``, (b) has a valid terminal state, and (c) has
    not yet been stamped. The host's poll tick iterates these and runs its legacy
    stamp path on each. Fail-safe: an unreadable/malformed artifact is skipped."""
    if not CONCLUDED_DIR.exists():
        return []
    out: list[tuple[str, dict, dict]] = []
    for cf in sorted(CONCLUDED_DIR.glob('*.json')):
        task_id = cf.stem
        if (STAMPED_DIR / f'{task_id}.json').exists():
            continue
        conc = _read_json(cf)
        if conc is None or conc.get('terminal_state') not in conclusion.TERMINAL_STATES:
            continue
        routing = _read_json(ROUTING_DIR / f'{task_id}.json')
        if routing is None or routing.get('site') not in sites:
            continue
        out.append((task_id, conc, routing))
    return out


def mark_stamped(task_id: str) -> None:
    """Record that ``task_id``'s conclusion has been stamped so the next poll
    leaves it alone. Written AFTER the stamp so a crash re-stamps (rare) rather
    than dropping the approval."""
    _ensure_dirs()
    atomic_io.atomic_write_json(
        STAMPED_DIR / f'{task_id}.json',
        {'task_id': task_id, 'stamped_at': time.time()},
        indent=2,
    )
