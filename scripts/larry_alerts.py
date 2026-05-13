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


_NOTIFICATION_INTENT_EMOJI = {
    'review-pass': '✓',
    'review-revision': '⚠',
    'review-escalate': '⚠',
    'review-emergency-halt': '🛑',
    'reject': '✗',
    'clarification-exhausted': '✗',
}


def format_dm(record: dict) -> str:
    """Render an alert OR notification for Telegram DM. Stable format the bot uses.

    Notifications (kind=notification) render as `<emoji> <message>` with the
    emoji chosen by intent. Alerts render with source + subject + severity
    emoji + message + optional suggested-action.
    """
    if record.get('_malformed'):
        return f'⚠ Bad alert in queue (skipped): {record.get("raw", "")!r}'
    # Notification rendering (D3.5 5a-followup).
    if record.get('kind') == 'notification':
        intent = record.get('intent', '?')
        emoji = _NOTIFICATION_INTENT_EMOJI.get(intent, '📬')
        return f'{emoji} {record.get("message", "")}'
    # Alert rendering (existing — D3.5-prep).
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
