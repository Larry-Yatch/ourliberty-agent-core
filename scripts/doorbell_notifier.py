#!/usr/bin/env python3
"""Doorbell notifier — a gentle, cooldown-gated nudge when items accrue that
need Larry's decision.

System self-awareness (the "standing brain"), the "poke me for the rest" half
of the autonomy-visibility ask: Beacon does the work it can and rings a
DOORBELL when force_ask approvals + escalations pile up. The DM is actionable —
it names WHAT needs Larry (the top blocking items, by title) and links WHERE to
go (a clickable board URL routed to the right surface), so a bare count is never
left dangling. Still a doorbell, never a wall (Larry's rule): the top few items
are named and capped at MAX_NAMED_ITEMS (then "+N more"); the FULL list + each
item's context lives on the board the link points to, not in the DM.

Reads the SAME substrate the dashboard renders — the State Log snapshot's
`waiting_on_larry` — so the doorbell and the board can never disagree. Counts
the BLOCKING items (pending_approvals + escalations); parked intake is excluded
(it's the standing funnel of promote/drop suggestions, not a decision that
blocks the team — and there are always ~20, which would make the doorbell nag).

DMs via larry_alerts.append_notification (intent='doorbell' → 🔔: a clean
`<emoji> <message>` render, targeted to Larry's primary chat). append_notification
has NO built-in cooldown, so THIS script owns the cadence:
  * ping promptly when the count INCREASES (a new thing needs Larry);
  * otherwise a gentle reminder at most once per REMINDER_WINDOW_HOURS while
    items remain (durable capture earns reduced frequency, NOT silence — the
    park-don't-decay rule);
  * go quiet (reset) when the count returns to 0, so the next new item rings
    promptly.

Fail-open everywhere: a missing/malformed snapshot or state file → no DM (never
nag on a broken read), and the tick never raises (a oneshot must not wedge its
timer).

Run: python3 scripts/doorbell_notifier.py   (systemd oneshot, every ~30 min)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_REPO_SCRIPTS = Path(__file__).resolve().parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import atomic_io  # noqa: E402
import larry_alerts  # noqa: E402

AGENTS_ROOT = Path(
    os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
# Mirror system_state_log.state_log_path() (the single source of truth for this
# path); replicated rather than imported to keep the doorbell's import graph
# light + test-isolated.
STATE_LOG_PATH = Path(
    os.environ.get(
        'OURLIBERTY_SYSTEM_STATE_LOG',
        str(AGENTS_ROOT / 'blackboard' / 'system-state-log.json'),
    )
)
STATE_FILE = AGENTS_ROOT / 'state' / 'doorbell-state.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'doorbell.log'

# The dashboard frontend base — the doorbell turns "the board" into an actual
# clickable destination (mirrors heal_missions_card_gc._DASHBOARD_BASE).
# Env-overridable so tests can assert the link without hard-coding the host.
DASHBOARD_BASE = os.environ.get(
    'OURLIBERTY_DASHBOARD_BASE', 'https://dashboard.ourliberty.dev')

# How many blocking items to NAME in the DM before collapsing to "+N more".
# A doorbell, never a wall (Larry's rule): enough to know WHAT without dumping
# the queue — the full list + context lives on the board the link points to.
MAX_NAMED_ITEMS = 3
# Trim a single item's title so one bullet stays one glanceable line.
TITLE_MAXLEN = 70

# A gentle reminder cadence while items remain (overridable). The timer ticks
# more often (~30 min) so an INCREASE is noticed promptly; this gates the
# steady-state reminder so a constant backlog isn't re-pinged every tick.
# Parse defensively: a malformed override must NOT raise at import (that would
# wedge the oneshot before main()'s guard could catch it) — fall back to 4h.
try:
    REMINDER_WINDOW_HOURS = float(
        os.environ.get('OURLIBERTY_DOORBELL_REMINDER_HOURS', '4'))
except ValueError:
    REMINDER_WINDOW_HOURS = 4.0


def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with LOG_FILE.open('a') as f:
            f.write(f'[{ts}] {msg}\n')
    except OSError:
        pass


def _primary_chat_id() -> Optional[int]:
    """Larry's primary Telegram chat — the lowest id in
    TELEGRAM_ALLOWED_CHAT_IDS (mirrors outbox_notifier._primary_chat_id /
    pulse_check). None when unset → the caller skips the DM rather than dropping
    it into a broadcast."""
    raw = os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS', '')
    ids = []
    for tok in raw.replace(',', ' ').split():
        try:
            ids.append(int(tok))
        except ValueError:
            continue
    return min(ids) if ids else None


def load_waiting() -> Optional[dict]:
    """Read `waiting_on_larry` from the State Log snapshot. None on any
    read/parse error — the caller treats None as 'no signal' and never nags on
    a broken/absent snapshot."""
    try:
        doc = json.loads(STATE_LOG_PATH.read_text())
        waiting = doc['structured_snapshot']['waiting_on_larry']
        return waiting if isinstance(waiting, dict) else None
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _needs_count(waiting: dict) -> tuple[int, int, int]:
    """(needs, approvals, escalations) — the BLOCKING items only. Parked intake
    is excluded by design (see module docstring)."""
    def _nonneg(v: object) -> int:
        return v if isinstance(v, int) and v > 0 else 0
    approvals = _nonneg(waiting.get('pending_approvals'))
    escalations = _nonneg(waiting.get('escalations'))
    return approvals + escalations, approvals, escalations


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    # Reuse the canonical durable atomic write (atomic_io); keep the fail-open
    # wrapper so a disk error logs rather than wedging the oneshot.
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_io.atomic_write_json(STATE_FILE, state, trailing_newline=True)
    except OSError as e:
        log(f'save_state failed: {e}')


def _parse_ts(s: object) -> Optional[datetime]:
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _board_link(approvals: int, escalations: int) -> str:
    """The clickable destination that replaces the dead words "check the board".
    Pure approvals land on the surface where you act on them (/approvals);
    anything with an escalation goes to the umbrella board (/where-we-are),
    which shows both kinds."""
    path = '/approvals' if escalations == 0 else '/where-we-are'
    return f'{DASHBOARD_BASE}{path}'


def _blocking_items(items: object) -> list[dict]:
    """The BLOCKING waiting-items (approvals + escalations) from the snapshot's
    `waiting_on_larry.items`, in the urgency order the State Log already sorted
    them. Parked intake is dropped — it's excluded from the doorbell count too
    (module docstring). Fail-soft: a missing/odd `items` yields []."""
    if not isinstance(items, list):
        return []
    return [
        it for it in items
        if isinstance(it, dict) and it.get('source') in ('approval', 'escalation')
    ]


def _item_line(item: dict) -> str:
    """One glanceable bullet: a kind tag + the item's own title, trimmed so the
    DM stays a doorbell (the full title + context lives on the board)."""
    tag = 'Approve' if item.get('source') == 'approval' else 'Escalation'
    # Collapse internal whitespace (titles can carry newlines) so one bullet
    # stays one line — a wrapped bullet reads like a second item.
    title = ' '.join(str(item.get('title') or '').split()) or '(no summary)'
    if len(title) > TITLE_MAXLEN:
        title = title[:TITLE_MAXLEN - 1].rstrip() + '…'
    return f'• {tag} — {title}'


def format_message(
    needs: int, approvals: int, escalations: int,
    items: object = None,
) -> str:
    """The doorbell DM body. Tells Larry WHAT needs him (the top blocking items,
    by name) and WHERE to go (a clickable board link) — not just a bare count.

    When the snapshot carries itemized `waiting_on_larry.items`, render a headline
    + up to MAX_NAMED_ITEMS named bullets (+"N more" when the count exceeds what
    we name) + the deep-link. When it doesn't (older snapshot / counts only),
    fall back to the calm one-liner with a light breakdown, still link-suffixed
    so "where to go" is never missing."""
    verb = 'needs' if needs == 1 else 'need'
    noun = 'item' if needs == 1 else 'items'
    link = _board_link(approvals, escalations)
    blocking = _blocking_items(items)

    if not blocking:
        # No itemized detail available — keep the old calm line, but make the
        # board a real link instead of dead words.
        breakdown = ''
        if approvals and escalations:
            breakdown = f' ({approvals} to approve, {escalations} escalated)'
        return f'{needs} {noun} {verb} your call.{breakdown}\n→ {link}'

    lines = [f'{needs} {noun} {verb} your call:']
    lines.extend(_item_line(it) for it in blocking[:MAX_NAMED_ITEMS])
    remaining = needs - min(len(blocking), MAX_NAMED_ITEMS)
    if remaining > 0:
        lines.append(f'• +{remaining} more')
    lines.append(f'→ {link}')
    return '\n'.join(lines)


def run(now: Optional[datetime] = None) -> bool:
    """One doorbell tick. Returns True iff a DM was sent. Never raises."""
    now = now or datetime.now(timezone.utc)
    waiting = load_waiting()
    if waiting is None:
        log('no readable State Log snapshot — skipping (no nag on a broken read)')
        return False
    needs, approvals, escalations = _needs_count(waiting)
    state = load_state()
    prev = state.get('last_count')
    prev_count = prev if isinstance(prev, int) else 0
    last_dm = _parse_ts(state.get('last_dm_ts'))

    if needs <= 0:
        # Caught up — reset so the next new item rings promptly.
        if prev_count != 0 or state.get('last_dm_ts') is not None:
            save_state({'last_count': 0})
        return False

    increased = needs > prev_count
    reminder_due = last_dm is None or (
        (now - last_dm) >= timedelta(hours=REMINDER_WINDOW_HOURS))

    if not (increased or reminder_due):
        # Quiet window, no new item — stay silent but track the current count so
        # a later increase is measured from this baseline (preserve last_dm_ts).
        save_state({'last_count': needs, 'last_dm_ts': state.get('last_dm_ts')})
        return False

    chat_id = _primary_chat_id()
    if chat_id is None:
        log('no TELEGRAM_ALLOWED_CHAT_IDS — cannot route doorbell DM; skipping')
        # Record the count but NOT a DM timestamp, so it retries once configured.
        save_state({'last_count': needs, 'last_dm_ts': state.get('last_dm_ts')})
        return False

    msg = format_message(needs, approvals, escalations, waiting.get('items'))
    sent = larry_alerts.append_notification(
        source='doorbell', intent='doorbell', message=msg, chat_id=chat_id,
    )
    if sent:
        reason = 'increase' if increased else 'reminder'
        log(f'doorbell DM sent ({reason}): needs={needs} '
            f'(approvals={approvals}, escalations={escalations})')
        save_state({'last_count': needs, 'last_dm_ts': now.isoformat()})
        return True
    log(f'append_notification returned False (disk?); needs={needs}; will retry')
    save_state({'last_count': needs, 'last_dm_ts': state.get('last_dm_ts')})
    return False


def main() -> int:
    try:
        run()
    except Exception as e:  # noqa: BLE001 — a oneshot must never wedge its timer
        log(f'unexpected error (fail-open): {type(e).__name__}: {e}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
