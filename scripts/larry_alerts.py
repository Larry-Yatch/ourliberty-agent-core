#!/usr/bin/env python3
"""larry_alerts.py — shared append-only alert queue for watchdog + sentinel.

Infra-monitoring scripts post alerts here when something should reach Larry's
phone. Beacon's Telegram bot polls the queue on its periodic sweep (alongside
the approval-reminder sweep) and DMs each new entry to every authorized chat.

Design notes:

  - Queue file: ~/agents/blackboard/larry-alerts.jsonl (append-only, UTF-8).
    One JSON object per line.
  - Per-subject cooldown gating: separate dirs for critical (10 min) and
    warning (60 min). The cooldown key is `source:subject` (subject-specific
    so e.g. "bots:mirror" and "bots:forge" each have their own bucket; M3
    fix from the design review).
  - The bot's offset file lives at ~/agents/state/beacon-alerts-offset.txt
    and is read/written by the bot — this module never advances it. Per-line
    ack on the bot side ensures at-least-once delivery (M2 fix).

Adapted from D3.5-prep design (2026-05-12).

Stdlib only.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

AGENTS_ROOT = Path.home() / 'agents'
ALERTS_FILE = AGENTS_ROOT / 'blackboard' / 'larry-alerts.jsonl'
COOLDOWN_ROOT = AGENTS_ROOT / 'state' / 'alert-cooldown'
OFFSET_FILE = AGENTS_ROOT / 'state' / 'beacon-alerts-offset.txt'

# Translation layer (stopgap until Pulse cycle upgrade ships healer-alert
# triage; see docs/operating-manual.md Part II #68). Lookup by (source,
# subject) at format_dm time; matched alerts get a plain-language layered
# render; unmatched alerts get the raw body + a "[no translation]" footer
# so silence-on-unmatched is impossible.
TRANSLATIONS_FILE = Path(__file__).resolve().parent.parent / 'config' / 'alert-translations.json'

CRITICAL_COOLDOWN_SEC = 10 * 60       # 10 min — terse and load-bearing
WARNING_COOLDOWN_SEC = 60 * 60        # 60 min — Larry's Dial 3 pick

VALID_SEVERITIES = ('warning', 'critical')


# ---------- cooldown machinery ----------


def _safe_key(key: str) -> str:
    """Filesystem-safe form of a cooldown key."""
    return ''.join(c if (c.isalnum() or c in '-._:') else '_' for c in key)


def _cooldown_path(severity: str, key: str) -> Path:
    return COOLDOWN_ROOT / severity / _safe_key(key)


def _cooldown_window(severity: str) -> int:
    return CRITICAL_COOLDOWN_SEC if severity == 'critical' else WARNING_COOLDOWN_SEC


def in_cooldown(severity: str, key: str, now: Optional[float] = None) -> bool:
    """Public-ish: True if the (severity, key) pair is still inside its window."""
    path = _cooldown_path(severity, key)
    if not path.exists():
        return False
    try:
        age = (now or time.time()) - path.stat().st_mtime
    except OSError:
        return False
    return age < _cooldown_window(severity)


def _mark_cooldown(severity: str, key: str) -> None:
    path = _cooldown_path(severity, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass


# ---------- writer side ----------


def append_alert(
    source: str,
    severity: str,
    message: str,
    subject: Optional[str] = None,
    suggested_action: Optional[str] = None,
) -> bool:
    """Append one alert if not in cooldown.

    Returns True if appended, False if suppressed (cooldown) or if the
    underlying append failed. Never raises — callers can fire-and-forget.

    Args:
        source: usually 'watchdog' or 'sentinel'.
        severity: 'warning' or 'critical'.
        message: short human-readable description for the DM body.
        subject: optional dedup-key suffix. Recommended — without it, all
            alerts from one source share a single cooldown bucket.
        suggested_action: optional shell command the operator can run.
    """
    if severity not in VALID_SEVERITIES:
        # Don't raise — surface as a no-op with a stderr hint.
        try:
            import sys as _sys
            _sys.stderr.write(
                f'[larry_alerts] invalid severity={severity!r}, dropping\n'
            )
        except Exception:
            pass
        return False
    key = f'{source}:{subject}' if subject else source
    if in_cooldown(severity, key):
        return False
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'source': source,
        'severity': severity,
        'message': message,
    }
    if subject:
        record['subject'] = subject
    if suggested_action:
        record['suggested_action'] = suggested_action
    try:
        ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND is atomic for writes <= PIPE_BUF (4096 on Linux); JSON
        # alerts are well under that. Open fresh each call so concurrent
        # writers (watchdog + sentinel) don't share a buffered handle.
        with open(ALERTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        return False
    _mark_cooldown(severity, key)
    return True


# ---------- notification writer (D3.5 5a-followup: chain-completion DMs) ----------


def append_notification(
    source: str,
    intent: str,
    message: str,
    chat_id: int,
    task_id: Optional[str] = None,
) -> bool:
    """Append one notification (closure DM for a chat-initiated task).

    Different shape from `append_alert` in three ways:

    1. **No cooldown gating.** Notifications are 1:1 with task completions
       (one PASS → one DM), not infra-noise. Repeating the same intent for
       different task_ids is normal; suppressing would lose closure DMs.
    2. **Targeted to a specific chat_id**, not broadcast to all authorized
       chats. The bot reads `chat_id` from the record and DMs only there.
       (Future-proofing for multi-user; today single-chat means same
       behavior either way.)
    3. **Carries `intent`**, not `severity`. The bot renders intent-specific
       emoji (✓ for review-pass, ⚠ for revision/escalate, 🛑 for emergency,
       ✗ for reject/clarification-exhausted) via `format_dm`.

    Records persist to the same `larry-alerts.jsonl` file as alerts, with
    `kind: "notification"` field distinguishing the two. The bot's reader
    side (`read_pending` + `read_offset` + `format_dm`) handles both.

    Returns True on successful append, False on failure. Never raises —
    callers fire-and-forget.
    """
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'source': source,
        'kind': 'notification',
        'intent': intent,
        'message': message,
        'chat_id': chat_id,
    }
    if task_id:
        record['task_id'] = task_id
    try:
        ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        return False
    return True


# ---------- approval-request writer (D3.5 5c: Beacon's auto-replan path) ----------


def append_approval_request(
    chat_id: int,
    approval_id: str,
    body: str,
    source: str = 'outbox-notifier',
) -> bool:
    """Append one approval-request record (Beacon-replan path).

    Different shape from `append_alert` and `append_notification`:

    1. **No cooldown gating.** Each replan is 1:1 with a task; suppressing
       would silently drop the auto-replan's approval prompt.
    2. **Targeted to a specific chat_id** (same as notifications) — the
       bot DMs only the originating thread.
    3. **Carries `approval_id` + `body`** — `approval_id` is the pending-
       approvals entry key (the bot looks it up to render the latest
       formatted prompt + reminder schedule + dispatch on approve); `body`
       is the pre-rendered fallback if the entry has been resolved before
       the bot polls (race protection — degrade to "stale approval-request
       record" rather than crashing the daemon).

    Used by `outbox_notifier._route_beacon_replan_approval` when the
    notifier extracts Beacon's auto-replan APPROVAL_REQUEST from her
    outbox and the trust policy says `force_ask`. The bot's existing
    chat-mode APPROVAL_REQUEST path (`_send_beacon_response` in
    `beacon_telegram_bot.py`) is unchanged — that handles markers Beacon
    emits in chat replies; this handles markers she emits via the inbox-
    watcher dispatch.

    Returns True on successful append, False on failure. Never raises —
    callers fire-and-forget.
    """
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'source': source,
        'kind': 'approval_request',
        'approval_id': approval_id,
        'chat_id': chat_id,
        'body': body,
    }
    try:
        ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        return False
    return True


# ---------- reader side (bot owns this) ----------


def read_pending(offset: int) -> list[tuple[int, dict]]:
    """Return [(line_index, parsed_dict)] for entries at or beyond offset.

    Malformed JSON or blank lines surface as {'_malformed': True, 'raw': ...}
    so the bot can advance past them rather than wedging forever.
    """
    if not ALERTS_FILE.exists():
        return []
    out: list[tuple[int, dict]] = []
    try:
        with open(ALERTS_FILE, encoding='utf-8') as f:
            for idx, raw_line in enumerate(f):
                if idx < offset:
                    continue
                line = raw_line.strip()
                if not line:
                    out.append((idx, {'_malformed': True, 'raw': ''}))
                    continue
                try:
                    out.append((idx, json.loads(line)))
                except json.JSONDecodeError:
                    out.append((idx, {'_malformed': True, 'raw': line[:200]}))
    except OSError:
        return []
    return out


def read_offset() -> int:
    """The bot's last-delivered line index + 1. 0 if file missing."""
    if not OFFSET_FILE.exists():
        return 0
    try:
        return int(OFFSET_FILE.read_text().strip() or '0')
    except (OSError, ValueError):
        return 0


def write_offset(offset: int) -> None:
    """Atomically persist the bot's offset (tmp + rename)."""
    try:
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OFFSET_FILE.with_suffix('.tmp')
        tmp.write_text(str(offset))
        tmp.rename(OFFSET_FILE)
    except OSError:
        # Best-effort. Worst case: at-least-once delivery → duplicate DM.
        pass


# ---------- translation layer (stopgap; see operating-manual.md #68) ----------


_TRANSLATIONS_CACHE: Optional[dict] = None


def _load_translations() -> dict:
    """Read config/alert-translations.json once per process. Returns the
    nested-by-source dict, or {} if the file is missing/malformed (the
    caller falls back to raw-body + [no translation] footer in that case)."""
    global _TRANSLATIONS_CACHE
    if _TRANSLATIONS_CACHE is not None:
        return _TRANSLATIONS_CACHE
    try:
        with open(TRANSLATIONS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    _TRANSLATIONS_CACHE = data
    return data


def translate_alert(source: str, subject: Optional[str]) -> Optional[dict]:
    """Look up a translation for (source, subject). Returns the translation
    entry (dict with severity / plain_language_summary / recommended_action)
    or None on miss.

    Lookup rule (per dispatch CLARIFY response):
      1. Exact match on the full subject.
      2. Longest-prefix match: strip trailing ':'-segments from subject one
         at a time, retrying after each strip until a key matches.

    Source must match exactly. Subject=None never matches (alerts without a
    subject can't be translated under V1 — there's no key shape for them)."""
    if not subject:
        return None
    translations = _load_translations()
    source_entries = translations.get(source)
    if not isinstance(source_entries, dict):
        return None
    if subject in source_entries:
        entry = source_entries[subject]
        return entry if isinstance(entry, dict) else None
    # Longest-prefix: strip trailing ':'-segments and retry.
    parts = subject.split(':')
    for trim in range(1, len(parts)):
        candidate = ':'.join(parts[:-trim])
        if candidate in source_entries:
            entry = source_entries[candidate]
            return entry if isinstance(entry, dict) else None
    return None


def _render_translated_alert(record: dict, translation: dict) -> str:
    """Render a matched alert with the new layered shape:

        <SEVERITY_WORD>                       (plain text, no emoji prefix)
        <plain-language summary>

        <recommended action>

        ---technical detail---
        <original raw header + body verbatim>

    The technical-detail block preserves the pre-translation render so the
    operator can still see source, subject, original message, and any
    suggested_action that the producer wrote."""
    severity_label = translation.get('severity', 'WARNING')
    summary = translation.get('plain_language_summary', '').strip()
    action = translation.get('recommended_action', '').strip()
    raw_body = _render_raw_alert_body(record)
    lines: list[str] = [severity_label]
    if summary:
        lines.append('')
        lines.append(summary)
    if action:
        lines.append('')
        lines.append(action)
    lines.append('')
    lines.append('---technical detail---')
    lines.append(raw_body)
    return '\n'.join(lines)


def _render_raw_alert_body(record: dict) -> str:
    """The original pre-translation render shape (severity emoji + source +
    subject + message + suggested_action). Kept verbatim for the technical-
    detail footer of matched alerts AND for the fallback render of unmatched
    alerts."""
    severity = record.get('severity', 'warning')
    emoji = '🚨' if severity == 'critical' else '⚠'
    source = record.get('source', '?')
    subject = record.get('subject')
    header = f'{emoji} {source}'
    if subject:
        header += f' [{subject}]'
    lines = [header, record.get('message', '')]
    sa = record.get('suggested_action')
    if sa:
        lines.append(f'Run: {sa}')
    return '\n'.join(line for line in lines if line)


_NO_TRANSLATION_FOOTER = (
    '[no translation; needs entry in config/alert-translations.json '
    'or Pulse triage scope]'
)


_NOTIFICATION_INTENT_EMOJI = {
    'review-pass': '✓',
    'review-revision': '⚠',
    'review-escalate': '⚠',
    'review-emergency-halt': '🛑',
    'reject': '✗',
    'clarification-exhausted': '✗',
}


def format_dm(record: dict) -> str:
    """Render an alert OR notification OR approval-request for Telegram DM.

    Three record shapes share this file:

    - Alerts (`kind: "alert"` or missing) — render with source + subject +
      severity emoji + message + optional suggested-action.
    - Notifications (`kind: "notification"`) — render as `<emoji> <message>`
      with the emoji chosen by intent.
    - Approval requests (`kind: "approval_request"`, D3.5 5c) — render is
      done by the BOT (it looks up the pending-approvals entry by
      `approval_id` and calls `approval.format_approval_dm`). This function
      returns the pre-rendered `body` as a fallback for the race where the
      entry has been resolved between append and read.
    """
    if record.get('_malformed'):
        return f'⚠ Bad alert in queue (skipped): {record.get("raw", "")!r}'
    # Notification rendering (D3.5 5a-followup).
    if record.get('kind') == 'notification':
        intent = record.get('intent', '?')
        emoji = _NOTIFICATION_INTENT_EMOJI.get(intent, '📬')
        return f'{emoji} {record.get("message", "")}'
    # Approval-request rendering (D3.5 5c). Bot reads `approval_id` to find
    # the pending entry and render via approval.format_approval_dm; this is
    # the degraded-fallback path (entry vanished between append and read).
    if record.get('kind') == 'approval_request':
        return record.get('body', '🪔 (approval request — entry not found)')
    # Alert rendering. First try the translation layer (stopgap; see
    # operating-manual.md #68): if (source, subject) matches an entry in
    # config/alert-translations.json, render the layered form with severity
    # word + plain-language summary + recommended action + technical-detail
    # footer. On miss, fall back to the original render shape with a
    # `[no translation]` footer so silence-on-unmatched is impossible.
    source = record.get('source', '?')
    subject = record.get('subject')
    translation = translate_alert(source, subject) if source != '?' else None
    if translation is not None:
        return _render_translated_alert(record, translation)
    raw = _render_raw_alert_body(record)
    return f'{raw}\n\n{_NO_TRANSLATION_FOOTER}'
