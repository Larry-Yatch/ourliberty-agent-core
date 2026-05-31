#!/usr/bin/env python3
"""beacon_approval_handler.py — approval-gate library for Beacon's bot.

Phase D3 commit 3 (`D3-approval`). Pure logic — no Telegram, no claude
spawning, no daemons. The bot owns I/O; this module owns the state machine.

What this module provides:

  - **Marker extraction.** Beacon emits `=== APPROVAL_REQUEST ===` blocks
    in her responses when she has a plan ready. `extract_approval_request`
    parses the JSON payload + returns the stripped narrative.

  - **User-reply pattern detection.** `parse_user_reply` looks at a message
    from Larry and returns one of {approve, modify, reject, pause, resume,
    none}. Strict whitelist on positive confirmation — ambiguous text
    bounces back to Beacon for clarification (no inferred approvals).

  - **Pending-approvals state file CRUD.** Persistent JSON at
    `agents/beacon/state/pending-approvals.json` (per-agent, not in
    `~/agents/state/` — Beacon's state is Beacon's). Add / find / resolve.
    History kept for audit (capped at 1000 entries).

  - **Trust-policy consultation.** Wraps `trust_policy.evaluate` so the bot
    has one entry point for "should this dispatch require Larry's approval?"

  - **Reminder scheduling.** `due_reminders(now)` returns entries that need
    a nudge DM based on their `created_at` + already-sent reminder count.
    Schedule: 6h, 24h, 72h. No reminders during pause.

  - **Global pause flag ops.** Touch / rm
    `~/agents/blackboard/APPROVALS_PAUSED`. Parallels EMERGENCY_HALT shape.

  - **Approval DM formatter.** Builds the user-facing approval request
    message — plan summary + the strict-whitelist reminder footer.

The bot is the only caller. The bot's main loop:

    1. Before forwarding a user message to Beacon:
       - parse_user_reply -> dispatch action OR pass through to Beacon
       - approval/modify/reject -> resolve pending entry, dispatch via safe_write_inbox
       - pause/resume -> toggle global flag, surface backlog on resume
    2. After getting Beacon's response:
       - extract_approval_request -> if found:
           consult trust_policy
           if auto_approve: dispatch directly + DM Larry one-liner
           if force_ask: add pending entry + DM Larry formatted request
           if reject: DM Larry policy rejection
       - else: forward Beacon's response unchanged
    3. Every poll cycle:
       - due_reminders -> DM Larry nudges
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import chain_event_shipper as _ces  # noqa: E402  # imported read-only for compute_event_id
import safe_write_inbox  # noqa: E402
import trust_policy      # noqa: E402

HOME = Path.home()
AGENTS_ROOT = HOME / 'agents'
REPO_ROOT = _SCRIPT_DIR.parent
# State lives in the RUNTIME tree (~/agents/state/), not the repo.
# Runtime is writable by the bot's systemd unit; the repo is read-only.
# Matches the convention used by dispatch-sentinel.json + other runtime state.
PENDING_APPROVALS_PATH = AGENTS_ROOT / 'state' / 'beacon-pending-approvals.json'
APPROVALS_PAUSED_FLAG = AGENTS_ROOT / 'blackboard' / 'APPROVALS_PAUSED'

HISTORY_CAP = 1000

# Strict positive-confirmation whitelist. The user's *entire* trimmed
# message (case-insensitive) must match exactly one of these for approval
# to fire. Ambiguous text — "approve if it's safe", "yes, but..." — does
# NOT fire approval; it bounces back to Beacon for clarification.
APPROVE_TOKENS = {
    'approve', 'go', 'ok', 'okay', 'ship', 'ship it',
}

# Modify / reject use a prefix grammar — the rest of the message is the
# reason, captured verbatim.
MODIFY_PREFIX_RE = re.compile(r'^\s*modify\s*:\s*(.+)$', re.IGNORECASE | re.DOTALL)
REJECT_PREFIX_RE = re.compile(r'^\s*reject\s*:\s*(.+)$', re.IGNORECASE | re.DOTALL)

# Pause / resume commands.
PAUSE_RE = re.compile(r'^\s*/?pause\s*$', re.IGNORECASE)
RESUME_RE = re.compile(r'^\s*/?resume\s*$', re.IGNORECASE)

# Marker block in Beacon's response.
APPROVAL_MARKER_RE = re.compile(
    r'===\s*APPROVAL_REQUEST\s*===\s*(\{.*?\})\s*===\s*END_APPROVAL_REQUEST\s*===',
    re.DOTALL,
)

# E1.1: marker grammar registry, mirrors forge_preflight_handler and
# mirror_review_handler. Beacon has just one marker type today; the registry
# shape stays symmetric so render_marker has the same signature across all
# three handlers and the drift-detector test
# (scripts/tests/test_marker_drift.py) can iterate every handler uniformly.
MARKER_TYPES = ('approval_request',)

MARKER_KEYWORDS = {
    'approval_request': 'APPROVAL_REQUEST',
}

# Required fields per marker type. Kept aligned with extract_approval_request's
# inline check below and with agents/beacon/CLAUDE.md's marker example. Single
# source of truth — the parser reads from this; the renderer reads from this.
REQUIRED_FIELDS = {
    'approval_request': {'task_id', 'summary', 'target_agent', 'prompt'},
}

# Reminder schedule (hours after creation).
REMINDER_HOURS = [6, 24, 72]

# D3.5 commit 5c: replan budget default. Caps the number of times Beacon
# can auto-emit an APPROVAL_REQUEST in response to Mirror's REVIEW_ESCALATE
# notify on a single task_id. Read from `config/agent-models.json`
# `loop_bounds.max_replans` at dispatch time; the default here matches that
# file but is local to keep this module config-free. Symmetric with
# `mirror_review_handler.DEFAULT_MAX_REVISIONS`.
DEFAULT_MAX_REPLANS = 2

# D3.5 commit 5c: minimum number of >3-char tokens from Mirror's escalation
# reason that must appear in Beacon's replan APPROVAL_REQUEST summary for
# the discipline gate to pass. Dial-3 (medium) per Larry's 2026-05-13 5c
# sign-off — strict enough to catch "Beacon made up her own reason" but
# lenient enough that paraphrase survives.
REPLAN_REASON_MIN_TOKEN_OVERLAP = 2


# -------------------- exceptions --------------------

class ApprovalHandlerError(Exception):
    """Base for handler errors that the bot should surface to the user."""


class MalformedApprovalMarker(ApprovalHandlerError):
    """Beacon emitted a marker block whose JSON is unparseable or missing fields."""


# -------------------- user-reply parsing --------------------

def parse_user_reply(text: str) -> dict[str, Any]:
    """Return action + payload, or {'action': 'none'} if not an approval command.

    Possible actions:
      - 'approve' — strict whitelist hit; bot resolves the most recent pending entry
      - 'modify'  — `modify: <reason>`; bot opens a re-plan loop with Beacon
      - 'reject'  — `reject: <reason>`; bot resolves as rejected
      - 'pause'   — toggle approvals pause
      - 'resume'  — toggle approvals resume
      - 'none'    — not a recognized command; forward to Beacon
    """
    if not isinstance(text, str):
        return {'action': 'none'}
    stripped = text.strip()
    if not stripped:
        return {'action': 'none'}

    if PAUSE_RE.match(stripped):
        return {'action': 'pause'}
    if RESUME_RE.match(stripped):
        return {'action': 'resume'}

    m = MODIFY_PREFIX_RE.match(stripped)
    if m:
        return {'action': 'modify', 'reason': m.group(1).strip()}

    m = REJECT_PREFIX_RE.match(stripped)
    if m:
        return {'action': 'reject', 'reason': m.group(1).strip()}

    if stripped.lower() in APPROVE_TOKENS:
        return {'action': 'approve'}

    return {'action': 'none'}


# -------------------- marker rendering --------------------

def render_marker(marker_type: str, **payload: Any) -> str:
    """Build a canonical approval-marker block from a payload dict.

    Returns the exact text Beacon should paste into her response — block
    delimiters + pretty-printed JSON payload + trailing newline. Output is
    guaranteed parseable by `extract_approval_request`; the round-trip test
    `TestRenderMarker.test_render_then_parse_roundtrip` enforces that.

    Raises ValueError (NOT MalformedApprovalMarker — render-time errors are
    caller programming errors, not runtime parse failures) if:
      - marker_type is not in MARKER_TYPES (today: only 'approval_request')
      - payload is missing any field in REQUIRED_FIELDS[marker_type]

    Extra fields beyond REQUIRED_FIELDS are allowed and preserved (matches
    the existing optional-field pattern — e.g., approval_request supports
    `target_repo`, `task_type`, `changed_files`, `pr_title`,
    `pr_body_template`, `max_clarifications`, `phase`, `reply_chat_id`).
    """
    if marker_type not in MARKER_TYPES:
        raise ValueError(
            f'unknown marker type: {marker_type!r}. '
            f'Valid types: {sorted(MARKER_TYPES)}'
        )
    required = REQUIRED_FIELDS[marker_type]
    missing = required - set(payload.keys())
    if missing:
        raise ValueError(
            f'render_marker({marker_type!r}): missing required fields: '
            f'{sorted(missing)}. Required: {sorted(required)}'
        )
    keyword = MARKER_KEYWORDS[marker_type]
    json_body = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    return f'=== {keyword} ===\n{json_body}\n=== END_{keyword} ===\n'


# -------------------- Beacon marker extraction --------------------

def extract_approval_request(beacon_response: str) -> tuple[Optional[dict[str, Any]], str]:
    """Find the FIRST `=== APPROVAL_REQUEST === ... === END ===` block in
    Beacon's response. Return (parsed_payload | None, narrative_stripped_of_marker).

    Raises MalformedApprovalMarker if the block exists but the JSON is bad.

    The narrative returned has the marker block removed so the bot doesn't
    show Larry both Beacon's raw marker AND the formatted approval request.
    """
    if not beacon_response:
        return None, beacon_response or ''
    m = APPROVAL_MARKER_RE.search(beacon_response)
    if not m:
        return None, beacon_response
    raw_json = m.group(1)
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise MalformedApprovalMarker(
            f'approval marker has invalid JSON: {e}'
        ) from e
    if not isinstance(payload, dict):
        raise MalformedApprovalMarker(
            f'approval marker payload must be a JSON object, got {type(payload).__name__}'
        )
    # Validate the minimum required fields. Sourced from module-level
    # REQUIRED_FIELDS so the renderer and parser stay in lockstep.
    required = REQUIRED_FIELDS['approval_request']
    missing = required - set(payload.keys())
    if missing:
        raise MalformedApprovalMarker(
            f'approval marker missing required fields: {sorted(missing)}'
        )
    narrative = (beacon_response[:m.start()] + beacon_response[m.end():]).strip()
    return payload, narrative


# -------------------- pending-approvals state --------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    """Parse ISO-8601 robustly; accept 'Z' or offset, return aware UTC."""
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_state() -> dict[str, Any]:
    if not PENDING_APPROVALS_PATH.exists():
        return {'version': 1, 'pending': [], 'history': []}
    try:
        data = json.loads(PENDING_APPROVALS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {'version': 1, 'pending': [], 'history': []}
    if 'pending' not in data:
        data['pending'] = []
    if 'history' not in data:
        data['history'] = []
    return data


def save_state(state: dict[str, Any]) -> None:
    PENDING_APPROVALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Cap history.
    history = state.get('history', [])
    if len(history) > HISTORY_CAP:
        state['history'] = history[-HISTORY_CAP:]
    tmp = PENDING_APPROVALS_PATH.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    tmp.rename(PENDING_APPROVALS_PATH)


def add_pending(
    payload: dict[str, Any],
    chat_id: int,
    queued_during_pause: bool = False,
    state: Optional[dict[str, Any]] = None,
    replan_count: int = 0,
    max_replans: Optional[int] = None,
) -> dict[str, Any]:
    """Add a pending-approval entry. Returns the entry dict written.

    D3.5 commit 5c: `replan_count` and `max_replans` are SYSTEM-controlled
    fields, not Beacon-authored. Stored under underscore-prefixed keys
    (`_replan_count`, `_max_replans`) to make the system-vs-Beacon ownership
    visible at a glance. When non-zero, `dispatch_approved` writes them to
    the next task envelope so the loop budget propagates through the next
    Forge dispatch.
    """
    s = state if state is not None else load_state()
    entry = {
        'id': payload['task_id'],
        'created_at': _now_utc(),
        'chat_id': chat_id,
        'plan_summary': payload.get('summary', ''),
        'target_agent': payload.get('target_agent', 'forge'),
        'dispatch_payload': payload,
        'status': 'pending',
        'reminders_sent': [],
        'queued_during_pause': queued_during_pause,
    }
    # D3.5 5c — system-controlled replan counter. Only attach when this is
    # a replan (count > 0). Fresh approvals don't carry the field at all,
    # so dispatch_approved doesn't write replan_count=0 to the envelope
    # (which would be noise and could confuse downstream propagation).
    if replan_count and replan_count > 0:
        entry['_replan_count'] = replan_count
        entry['_max_replans'] = (
            max_replans if max_replans is not None else DEFAULT_MAX_REPLANS
        )
    s['pending'].append(entry)
    if state is None:
        save_state(s)
    return entry


def find_pending_by_id(approval_id: str, state: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    s = state if state is not None else load_state()
    for entry in s.get('pending', []):
        if entry.get('id') == approval_id:
            return entry
    return None


def find_by_id_any_state(
    approval_id: str, state: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Search BOTH pending and history for an entry with this id.

    D3.5 commit 5c (Med-X1 second-pass review fix). The 5c replan dedup
    gate needs to short-circuit replays where the entry has already been
    auto-approved + resolved (entry moved to history). `find_pending_by_id`
    alone misses this case: a notifier crash between `resolve` and outbox
    archive would let the duplicate outbox re-dispatch on the next poll,
    overwriting the already-dispatched Forge task file.

    Returns the entry from `pending` if present, else from `history` if
    present, else None. Caller treats either as "this approval_id is
    already known; skip the duplicate."
    """
    s = state if state is not None else load_state()
    for entry in s.get('pending', []):
        if entry.get('id') == approval_id:
            return entry
    for entry in s.get('history', []):
        if entry.get('id') == approval_id:
            return entry
    return None


def most_recent_pending(state: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Return the most-recently-created pending entry, or None.

    Ignores entries that were queued during pause (they shouldn't be approved
    by a `/resume` follow-up unless the user picks them by id).
    """
    s = state if state is not None else load_state()
    pending = [e for e in s.get('pending', []) if not e.get('queued_during_pause')]
    if not pending:
        return None
    return max(pending, key=lambda e: e.get('created_at', ''))


def resolve(
    approval_id: str,
    new_status: str,
    note: str = '',
    state: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Mark an entry resolved (approved/rejected/modified) and move to history.
    Returns the moved entry (with the new status + resolution metadata)."""
    if new_status not in {'approved', 'rejected', 'modified', 'expired'}:
        raise ValueError(f'invalid status: {new_status}')
    s = state if state is not None else load_state()
    pending = s.get('pending', [])
    matched = None
    remaining = []
    for entry in pending:
        if entry.get('id') == approval_id and matched is None:
            matched = entry
        else:
            remaining.append(entry)
    if matched is None:
        return None
    matched['status'] = new_status
    matched['resolved_at'] = _now_utc()
    if note:
        matched['resolution_note'] = note
    s['pending'] = remaining
    s.setdefault('history', []).append(matched)
    if state is None:
        save_state(s)
    return matched


def pop_paused_backlog(state: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Mark all paused-during-creation entries as no-longer-paused, return them.

    Called on /resume so the bot can DM the backlog. The entries remain
    pending (still need approval); only the `queued_during_pause` flag flips.
    """
    s = state if state is not None else load_state()
    backlog = []
    for entry in s.get('pending', []):
        if entry.get('queued_during_pause'):
            entry['queued_during_pause'] = False
            entry['unpaused_at'] = _now_utc()
            backlog.append(entry)
    if state is None and backlog:
        save_state(s)
    return backlog


# -------------------- pause / resume --------------------

def is_paused() -> bool:
    return APPROVALS_PAUSED_FLAG.exists()


def set_paused(paused: bool) -> None:
    APPROVALS_PAUSED_FLAG.parent.mkdir(parents=True, exist_ok=True)
    if paused:
        APPROVALS_PAUSED_FLAG.touch(exist_ok=True)
    else:
        try:
            APPROVALS_PAUSED_FLAG.unlink()
        except FileNotFoundError:
            pass


# -------------------- trust policy bridge --------------------

def trust_decision(payload: dict[str, Any]) -> tuple[str, Optional[dict[str, Any]]]:
    """Consult trust_policy. Returns (action, matched_rule).

    Translates the marker payload into the trust_policy task shape (which
    expects `source`, `target_agent`, optionally `task_type`, `target_repo`,
    `changed_files`). Beacon always sources as 'beacon' since this gate
    fires on Beacon-initiated dispatches.
    """
    policy_task = {
        'source': 'beacon',
        'target_agent': payload.get('target_agent', 'forge'),
        'task_type': payload.get('task_type'),
        'target_repo': payload.get('target_repo'),
        'changed_files': payload.get('changed_files', []),
    }
    return trust_policy.evaluate(policy_task)


# -------------------- reminders --------------------

def due_reminders(
    now: Optional[datetime] = None,
    state: Optional[dict[str, Any]] = None,
) -> list[tuple[dict[str, Any], int]]:
    """Return [(entry, hours_threshold)] for entries that should be reminded.

    Skipped during pause. An entry is "due" when:
      - it isn't paused-during-creation
      - the current age exceeds a threshold from REMINDER_HOURS
      - that threshold hasn't already been recorded in reminders_sent
    """
    if is_paused():
        return []
    now = now or datetime.now(timezone.utc)
    s = state if state is not None else load_state()
    out = []
    for entry in s.get('pending', []):
        if entry.get('queued_during_pause'):
            continue
        try:
            created = _parse_iso(entry['created_at'])
        except (KeyError, ValueError):
            continue
        age_hours = (now - created).total_seconds() / 3600.0
        sent_set = set(entry.get('reminders_sent', []))
        for threshold in REMINDER_HOURS:
            if age_hours >= threshold and threshold not in sent_set:
                out.append((entry, threshold))
                break  # one reminder per cycle per entry
    return out


def record_reminder_sent(
    approval_id: str,
    threshold_hours: int,
    state: Optional[dict[str, Any]] = None,
) -> None:
    s = state if state is not None else load_state()
    for entry in s.get('pending', []):
        if entry.get('id') == approval_id:
            sent = entry.setdefault('reminders_sent', [])
            if threshold_hours not in sent:
                sent.append(threshold_hours)
            break
    if state is None:
        save_state(s)


# -------------------- DM formatters --------------------

APPROVAL_FOOTER = (
    '\n\n'
    'Reply to approve:\n'
    '  • approve / go / ok / ship it\n\n'
    'To adjust:\n'
    '  • modify: <what to change>\n'
    '  • reject: <why>\n\n'
    "Anything else, I'll bounce back to clarify."
)


def format_approval_dm(entry: dict[str, Any]) -> str:
    """Build the Telegram message body for a pending-approval request."""
    payload = entry.get('dispatch_payload', {})
    body = (
        f'🪔 Plan ready for approval — task {entry.get("id", "?")}\n\n'
        f'{entry.get("plan_summary", "(no summary)")}'
    )
    extras = []
    if payload.get('target_agent'):
        extras.append(f'Target: {payload["target_agent"]}')
    if payload.get('target_repo'):
        extras.append(f'Repo: {payload["target_repo"]}')
    if payload.get('task_type'):
        extras.append(f'Type: {payload["task_type"]}')
    if extras:
        body += '\n\n' + '    '.join(extras)
    body += APPROVAL_FOOTER
    return body


def format_reminder_dm(entry: dict[str, Any], hours: int) -> str:
    return (
        f'⏰ Reminder ({hours}h since creation): task {entry.get("id", "?")} '
        f'is still pending approval.\n\n'
        f'{entry.get("plan_summary", "(no summary)")}'
        f'{APPROVAL_FOOTER}'
    )


def format_dispatch_confirmation(entry: dict[str, Any]) -> str:
    payload = entry.get('dispatch_payload', {})
    return (
        f'✅ Approved + dispatched: {entry.get("id", "?")}\n'
        f'-> {payload.get("target_agent", "forge")} '
        f'(repo: {payload.get("target_repo", "—")})'
    )


def format_auto_approve_confirmation(entry: dict[str, Any], rule: dict[str, Any]) -> str:
    payload = entry.get('dispatch_payload', {})
    return (
        f'🤖 Auto-approved by trust policy + dispatched: {entry.get("id", "?")}\n'
        f'-> {payload.get("target_agent", "forge")} '
        f'(repo: {payload.get("target_repo", "—")})\n'
        f'Rule: {rule}'
    )


def format_policy_rejection(payload: dict[str, Any], rule: dict[str, Any]) -> str:
    return (
        f'🚫 Trust policy REJECTED dispatch — not sent.\n'
        f'task: {payload.get("task_id", "?")}\n'
        f'summary: {payload.get("summary", "(none)")}\n'
        f'Rule: {rule}'
    )


def format_bounce(reason: str = '') -> str:
    msg = "Reply didn't match the approval grammar."
    if reason:
        msg += f' ({reason})'
    msg += (
        ' Use exactly one of: approve / go / ok / ship it. '
        'For changes: modify: <what>. For rejection: reject: <why>.'
    )
    return msg


def format_pause_confirmation() -> str:
    return (
        '⏸ Approvals paused. New plans from Beacon will queue silently '
        'until /resume. Existing pending approvals remain pending. '
        'Reminders also paused.'
    )


def format_resume_confirmation(backlog_count: int) -> str:
    if backlog_count == 0:
        return '▶ Approvals resumed. No plans queued during pause.'
    return (
        f'▶ Approvals resumed. {backlog_count} plan(s) queued during pause '
        f'are about to be DMed individually.'
    )


# -------------------- chain_event payload builder (E4.4e PR-A) --------------------

# Concrete envelope shapes per spec § 7.1, with placeholders resolved at
# emit time. PR-B's dashboard POST endpoint reads suggested_envelope_for_*
# from the approval_request chain_event row and writes verbatim, only
# adding `actor` (the authed user's email). For reject, PR-B also appends
# the optional `Optional comment: <text>` line to the prompt when the
# user supplies one — that's why the reject prompt below ends with no
# comment placeholder.
_APPROVE_PROMPT_TEMPLATE = (
    'Larry approved the pending proposal via dashboard. '
    'Source event: {event_id}. '
    'Proceed per the approve-path that beacon_approval_handler.py describes '
    'for this approval_request type. '
    'Use the suggested_envelope_for_approve payload from the source event.'
)

_REJECT_PROMPT_TEMPLATE = (
    'Larry rejected the pending proposal via dashboard. '
    'Source event: {event_id}. '
    'Soft reject — archive the pending item, do not abort any in-flight work, '
    'route per the suggested_envelope_for_reject payload from the source event.'
)


def build_approval_request_chain_event(
    payload: dict[str, Any],
    *,
    ts: Optional[str] = None,
) -> dict[str, Any]:
    """Build kwargs for `chain_event_emit.emit_event` for an approval_request.

    Per spec § 4, the payload carries six fields:
      - proposing_agent: 'beacon' (this module's owner)
      - target_agent: who would receive the dispatched task on approve
      - prompt: the full marker prompt body
      - dedup_identity: the dispatch task_id (per PR-A dispatch task —
        NOT a timestamp/UUID that would defeat dedup on re-emit)
      - suggested_envelope_for_approve: concrete envelope for the dashboard
        POST endpoint to write when Larry clicks Approve
      - suggested_envelope_for_reject: concrete envelope for Reject

    Per spec § 11 open question 1 (resolved via PR-A preflight CLARIFY 2026-05-26):
    interpretation (A) — the suggested envelopes are CONCRETE dicts with
    placeholders already resolved against the chain_event's own event_id.
    `actor` is OMITTED; the dashboard fills it from the authed user.

    Returns kwargs ready to splat into `chain_event_emit.emit_event(**...)`.
    The caller (the bot, after add_pending) is responsible for the actual
    Supabase write; this builder is pure so test fixtures can assert payload
    shape without mocking supabase-py.
    """
    use_ts = ts or _now_utc()
    task_id = payload['task_id']
    event_id = _ces.compute_event_id(task_id, 'approval_request', use_ts)
    suggested_envelope_for_approve = {
        'task_id': f'larry-approval-{event_id}',
        'source': 'dashboard',
        'dedup_identity': f'larry-approval:{event_id}',
        'timeout': 600,
        'prompt': _APPROVE_PROMPT_TEMPLATE.format(event_id=event_id),
    }
    suggested_envelope_for_reject = {
        'task_id': f'larry-reject-{event_id}',
        'source': 'dashboard',
        'dedup_identity': f'larry-reject:{event_id}',
        'timeout': 600,
        'prompt': _REJECT_PROMPT_TEMPLATE.format(event_id=event_id),
    }
    chain_payload = {
        'proposing_agent': 'beacon',
        'target_agent': payload.get('target_agent', 'forge'),
        'prompt': payload.get('prompt', ''),
        'dedup_identity': task_id,
        'suggested_envelope_for_approve': suggested_envelope_for_approve,
        'suggested_envelope_for_reject': suggested_envelope_for_reject,
    }
    return {
        'event_type': 'approval_request',
        'agent': 'beacon',
        'task_id': task_id,
        'ts': use_ts,
        'payload': chain_payload,
    }


# -------------------- dispatch step --------------------

def dispatch_approved(entry: dict[str, Any]) -> Path:
    """Call safe_write_inbox to land the task in target_agent's inbox.

    Returns the written path. Raises safe_write_inbox.DispatchRejected or
    safe_write_inbox.RoutingDenied — caller surfaces the error to Larry.
    """
    payload = entry['dispatch_payload']
    target = entry.get('target_agent') or payload.get('target_agent', 'forge')
    # Ensure source is 'beacon' on the envelope. Propagate `chat_id` from the
    # stored entry into the dispatched task as `reply_chat_id` so the entire
    # downstream chain (preflight → build → review → review-pass notify) can
    # route a completion DM back to the chat thread that initiated the work.
    # Bug A fix (D3.5 5a-followup): without this, the original Forge inbox
    # task lands without reply_chat_id and the propagation chain has nothing
    # to carry, so Larry never gets a closing DM on his Telegram thread.
    task_dict = {**payload, 'source': 'beacon'}
    if entry.get('chat_id') is not None:
        task_dict['reply_chat_id'] = entry['chat_id']
    # D3.5 5c — replan_count + max_replans propagation. When this entry was
    # added by `_route_beacon_replan_approval` (notifier extracted Beacon's
    # auto-replan APPROVAL_REQUEST from her outbox after a review-escalate
    # notify), the entry carries `_replan_count` and `_max_replans` under
    # underscore-prefixed keys (system-controlled — Beacon doesn't author).
    # Surface them on the dispatched task envelope so the next Forge → Mirror
    # cycle's REVIEW_ESCALATE notify carries the incremented count forward,
    # and so Mirror's classifier can backstop the budget (defense in depth).
    replan_count = entry.get('_replan_count')
    if replan_count and replan_count > 0:
        task_dict['replan_count'] = replan_count
        task_dict['max_replans'] = (
            entry.get('_max_replans') or DEFAULT_MAX_REPLANS
        )
    # Filename — use task_id stem.
    filename = f'{payload["task_id"]}.json'
    return safe_write_inbox.safe_write_inbox(
        target_agent=target,
        task_dict=task_dict,
        source_agent='beacon',
        filename=filename,
    )


# -------------------- replan budget + discipline (D3.5 commit 5c) --------------------

def evaluate_replan_budget(
    task_envelope: dict[str, Any],
) -> tuple[str, int, int]:
    """Decide whether another replan round is permitted.

    Returns `(decision, next_count, max_count)` where:
      - decision ∈ {'allow', 'exhausted'}
      - next_count is the value `replan_count` would take after this round
      - max_count is the envelope's `max_replans` (or `DEFAULT_MAX_REPLANS`)

    Parallel to `mirror_review_handler.evaluate_revision_budget`. Called by
    `outbox_notifier._route_beacon_replan_approval` as the system-side
    backstop. Beacon's CLAUDE.md is the first line — she's instructed to
    check the envelope counters and NOT emit APPROVAL_REQUEST when at cap.
    This function catches it if she misses.

    Stateless: pass the envelope in, get a decision out.
    """
    current = task_envelope.get('replan_count', 0)
    if not isinstance(current, int) or current < 0:
        current = 0
    max_count = task_envelope.get('max_replans', DEFAULT_MAX_REPLANS)
    if not isinstance(max_count, int) or max_count < 0:
        max_count = DEFAULT_MAX_REPLANS
    next_count = current + 1
    if next_count > max_count:
        return 'exhausted', next_count, max_count
    return 'allow', next_count, max_count


# Tokenizer for the level-3 discipline gate on replan reason references.
# Strips punctuation, lowercases, keeps tokens > 3 chars to filter common
# words ("the", "a", "of") that don't carry semantic weight. Symmetric
# tokenization on both sides (Mirror's reason and Beacon's summary) means
# paraphrase survives ("addressed missing validation" passes when Mirror
# said "validation was missing"); pure fabrication fails ("scope creep
# concern" rejects when Mirror said "credentials in test file").
_REPLAN_TOKEN_RE = re.compile(r'[A-Za-z]{4,}')


def _replan_tokens(text: str) -> set[str]:
    """Lowercase >3-char word tokens. Empty set if text is non-string/empty."""
    if not isinstance(text, str) or not text:
        return set()
    return {tok.lower() for tok in _REPLAN_TOKEN_RE.findall(text)}


def validate_replan_discipline(
    payload: dict[str, Any],
    envelope: dict[str, Any],
    mirror_reason: str,
) -> tuple[bool, str]:
    """Apply the level-3 discipline gate to Beacon's auto-replan APPROVAL_REQUEST.

    Returns `(ok, error_msg)` where:
      - ok=True means the marker passes the gate and routing should proceed
      - ok=False means the marker fails; caller logs WARN + falls through to
        default routing (NO marker-error cascade — strict-5 dial behavior was
        not selected for 5c per Larry's 2026-05-13 sign-off)

    Level-3 (medium) gate checks:

      1. **task_id match** — payload['task_id'] must equal envelope['task_id'].
         Catches "Beacon authored a marker for the wrong task" (audit-trail
         confusion). Strict, not lenient.

      2. **Mirror-reason reference** — payload['summary'] must contain at
         least `REPLAN_REASON_MIN_TOKEN_OVERLAP` (=2) tokens from Mirror's
         escalation reason (>3-char words, case-insensitive). Catches
         "Beacon made up her own reason instead of addressing Mirror's
         finding." Lenient on phrasing — paraphrase passes; complete
         fabrication fails. The summary is what Larry sees on the approval
         DM; making it reference Mirror's actual concern is the value.

    Both checks are necessary; missing either fails. Empty Mirror reason
    (defensive fallback) auto-passes the second check — without a baseline
    to compare against, the gate has nothing to enforce.
    """
    # Check 1: task_id match
    # 5c-followup fix: strip the `notify-` prefix from envelope task_id
    # before comparing. The outbox-notifier's marker-routing block prefixes
    # `notify-` to task_id when writing the notify-task envelope to Beacon's
    # inbox (existing D3 convention for filename disambiguation), so Beacon's
    # outbox carries `task_id='notify-<orig>'`. Beacon (per her CLAUDE.md
    # Shape 8) correctly uses the ORIGINAL task_id in her APPROVAL_REQUEST
    # payload. Strict equality would reject every legitimate replan. Both
    # 5c review passes missed this because the test fixtures synthesized
    # envelopes with raw task_ids, not the production-shaped notify-prefixed
    # ones. Surfaced by Phase 2 live smoke 2026-05-14.
    envelope_task_id = envelope.get('task_id')
    if isinstance(envelope_task_id, str) and envelope_task_id.startswith('notify-'):
        envelope_task_id = envelope_task_id[len('notify-'):]
    payload_task_id = payload.get('task_id')
    if (
        envelope_task_id is not None
        and payload_task_id is not None
        and payload_task_id != envelope_task_id
    ):
        return False, (
            f'APPROVAL_REQUEST task_id ({payload_task_id!r}) does not match '
            f'envelope task_id ({envelope_task_id!r}); replan must reuse the '
            f'original task_id'
        )

    # Check 2: Mirror reason reference (skip if no reason to compare against)
    if not mirror_reason:
        return True, ''
    mirror_tokens = _replan_tokens(mirror_reason)
    summary = payload.get('summary', '')
    summary_tokens = _replan_tokens(summary)
    overlap = mirror_tokens & summary_tokens
    # Med-8 review fix: adapt threshold to len(mirror_tokens). If Mirror's
    # reason is terse ("Spec gap." → 1 long token), requiring 2 overlap is
    # unsatisfiable and Beacon's good-faith summary always fails. Keep the
    # Dial-3 strictness for verbose reasons; gracefully degrade when she's
    # terse. Symmetric with the "empty mirror reason auto-passes" case.
    required = min(REPLAN_REASON_MIN_TOKEN_OVERLAP, len(mirror_tokens))
    if len(overlap) < required:
        return False, (
            f'APPROVAL_REQUEST summary does not reference Mirror\'s escalation '
            f'reason (overlap={len(overlap)} tokens; need {required}). '
            f'Replan summary must address what Mirror flagged. '
            f'summary={summary[:120]!r}, mirror_reason={mirror_reason[:120]!r}'
        )
    return True, ''
