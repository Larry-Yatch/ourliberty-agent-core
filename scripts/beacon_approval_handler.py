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

import safe_write_inbox  # noqa: E402
import trust_policy      # noqa: E402

HOME = Path.home()
AGENTS_ROOT = HOME / 'agents'
REPO_ROOT = _SCRIPT_DIR.parent
PENDING_APPROVALS_PATH = REPO_ROOT / 'agents' / 'beacon' / 'state' / 'pending-approvals.json'
APPROVALS_PAUSED_FLAG = AGENTS_ROOT / 'blackboard' / 'APPROVALS_PAUSED'

HISTORY_CAP = 1000

# Strict positive-confirmation whitelist. The user's *entire* trimmed
# message (case-insensitive) must match exactly one of these for approval
# to fire. Ambiguous text — "approve if it's safe", "yes, but..." — does
# NOT fire approval; it bounces back to Beacon for clarification.
APPROVE_TOKENS = {
    'approve', 'yes', 'go', 'ok', 'okay', 'ship', 'ship it',
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

# Reminder schedule (hours after creation).
REMINDER_HOURS = [6, 24, 72]


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
    # Validate the minimum required fields.
    required = {'task_id', 'summary', 'target_agent', 'prompt'}
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
) -> dict[str, Any]:
    """Add a pending-approval entry. Returns the entry dict written."""
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
    '  • approve / yes / go / ok / ship it\n\n'
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
        ' Use exactly one of: approve / yes / go / ok / ship it. '
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


# -------------------- dispatch step --------------------

def dispatch_approved(entry: dict[str, Any]) -> Path:
    """Call safe_write_inbox to land the task in target_agent's inbox.

    Returns the written path. Raises safe_write_inbox.DispatchRejected or
    safe_write_inbox.RoutingDenied — caller surfaces the error to Larry.
    """
    payload = entry['dispatch_payload']
    target = entry.get('target_agent') or payload.get('target_agent', 'forge')
    # Ensure source is 'beacon' on the envelope.
    task_dict = {**payload, 'source': 'beacon'}
    # Filename — use task_id stem.
    filename = f'{payload["task_id"]}.json'
    return safe_write_inbox.safe_write_inbox(
        target_agent=target,
        task_dict=task_dict,
        source_agent='beacon',
        filename=filename,
    )
