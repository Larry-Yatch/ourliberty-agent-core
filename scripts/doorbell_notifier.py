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

STALE SUBSTRATE (2026-09-16 incident): fail-open covered a snapshot that is
missing or malformed, but not one that parses fine and is simply OLD. When the
snapshot's sole writer stopped, the doc froze with pending_approvals=4; those
four approvals expired ~3h43m later, but the doorbell kept reading 4 and rang
every ~4h for days. The self-silencing rule above ("go quiet when the count
returns to 0") could never fire, because a frozen count never reaches 0 — so the
gentle nudge became an unbounded, confidently-wrong nag. Hence: a snapshot older
than MAX_SNAPSHOT_AGE_HOURS (by its OWN `as_of`, not file mtime — mtime survives
a touch/copy that carries no new content) is NOT ringable. Staleness is itself
reported, but exactly ONCE per stale episode (re-armed only when a fresh snapshot
is seen), so a frozen writer yields one honest signal instead of an endless wrong
one. An absent/unparseable `as_of` counts as stale — fail toward silence, the
same posture as a broken read.

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

# A snapshot older than this is not ringable (see the STALE SUBSTRATE note in the
# module docstring). Sized against the writer's cadence: the State Log is written
# on the missions-card-GC tick (~10 min), so 1h leaves room for several skipped
# ticks before the doorbell distrusts the count. Deliberately looser than
# state_log_query.STALE_AFTER_SEC (25 min), which only has to caveat a chat reply
# with "this is N minutes old"; here the consequence is SUPPRESSING a real nudge,
# so the bar for calling a snapshot dead is higher. Parsed defensively for the
# same reason as REMINDER_WINDOW_HOURS above — a malformed override must not
# raise at import.
try:
    MAX_SNAPSHOT_AGE_HOURS = float(
        os.environ.get('OURLIBERTY_DOORBELL_MAX_SNAPSHOT_AGE_HOURS', '1'))
except ValueError:
    MAX_SNAPSHOT_AGE_HOURS = 1.0


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


def load_snapshot() -> Optional[dict]:
    """Read the whole State Log doc. None on any read/parse error.

    Split out from `load_waiting` so one tick reads the file ONCE and derives
    both the counts and the freshness stamp from the same bytes — re-reading
    would let the writer land between the two reads and pair a fresh `as_of`
    with a stale count (or vice versa)."""
    try:
        doc = json.loads(STATE_LOG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def load_waiting(doc: Optional[dict] = None) -> Optional[dict]:
    """Read `waiting_on_larry` from the State Log snapshot. None on any
    read/parse error — the caller treats None as 'no signal' and never nags on
    a broken/absent snapshot.

    `doc` lets a caller that already read the snapshot reuse those bytes. The
    no-argument form re-reads and is the PUBLIC contract other modules depend on
    (heal_unregistered_approval.doorbell_counts calls `load_waiting()` bare to
    measure the doorbell against its own source of truth) — keep it working."""
    if doc is None:
        doc = load_snapshot()
    if doc is None:
        return None
    try:
        waiting = doc['structured_snapshot']['waiting_on_larry']
    except (KeyError, TypeError):
        return None
    return waiting if isinstance(waiting, dict) else None


def snapshot_as_of(doc: dict) -> Optional[datetime]:
    """The snapshot's own timestamp: top-level `as_of`, falling back to
    `structured_snapshot.as_of`. None when both are absent or unparseable."""
    candidates = [doc.get('as_of')]
    structured = doc.get('structured_snapshot')
    if isinstance(structured, dict):
        candidates.append(structured.get('as_of'))
    for raw in candidates:
        parsed = _parse_ts(raw)
        if parsed is not None:
            return parsed
    return None


def is_stale(as_of: Optional[datetime], now: datetime) -> bool:
    """Is this snapshot too old to ring on? An unknown `as_of` is stale — we
    cannot vouch for a count we cannot date, so we fail toward silence."""
    if as_of is None:
        return True
    return (now - as_of) > timedelta(hours=MAX_SNAPSHOT_AGE_HOURS)


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


def _board_link() -> str:
    """The clickable destination that replaces the dead words "check the board".

    ONE destination: /approvals. The escalation branch used to send Larry to an
    "umbrella board" at /where-we-are, but the dashboard DELETED that page in
    a86aa6f (2026-07-03, "migrate Where-Are-We panels + retire the tab" — page,
    helpers, tests and nav entry all removed). Because the branch only fired when
    escalations > 0, the link worked on ordinary days and broke precisely on the
    days something extra was waiting, which is why it survived 132 DMs across 24
    days before Larry hit it.

    /approvals is the canonical waiting-on-you surface: the dashboard's own /live
    top glance routes its waiting-on-you count there, and an escalation reaches
    the dashboard only once heal_unregistered_approval promotes it into an
    approval card on that tab — so /approvals is where the actionable item
    actually is. The unpromoted escalation row has no dashboard page at all, and
    never did after 2026-07-03.
    """
    return f'{DASHBOARD_BASE}/approvals'


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
    link = _board_link()
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


def _humanize_age(delta: timedelta) -> str:
    """A coarse '2d 14h' / '3h 5m' / '12m' age — enough to convey how dead the
    substrate is without pretending to precision."""
    total_min = int(max(delta.total_seconds(), 0) // 60)
    days, rem_min = divmod(total_min, 24 * 60)
    hours, minutes = divmod(rem_min, 60)
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {minutes}m'
    return f'{minutes}m'


def format_stale_message(as_of: Optional[datetime], now: datetime) -> str:
    """The once-per-episode stale-substrate DM. Says the substrate is stale and
    names its `as_of`, so the silence is explained rather than merely absent."""
    if as_of is None:
        dated = 'it carries no readable `as_of` timestamp'
    else:
        dated = (
            f'it is dated {as_of.isoformat()} '
            f'({_humanize_age(now - as_of)} old)'
        )
    return (
        'Doorbell going quiet: the State Log snapshot it reads is stale — '
        f'{dated}. Any "N need your call" count from it would be unreliable, '
        'so no nudge until a fresh snapshot lands. This is the only message '
        'you will get for this episode.\n'
        f'→ {_board_link()}'
    )


def _report_stale(
    state: dict, as_of: Optional[datetime], now: datetime,
) -> bool:
    """Handle a stale-snapshot tick. Returns True iff the one-per-episode DM was
    sent on THIS tick.

    Already-reported episodes return silently WITHOUT rewriting state, so the
    count cooldown (`last_count` / `last_dm_ts`) is preserved untouched across
    the outage and the doorbell resumes from its real baseline once the writer
    recovers."""
    if state.get('stale_reported'):
        log(f'State Log snapshot still stale (as_of={as_of}) — already '
            f'reported this episode; staying quiet')
        return False

    chat_id = _primary_chat_id()
    if chat_id is None:
        log('no TELEGRAM_ALLOWED_CHAT_IDS — cannot route stale-substrate DM; '
            'skipping (episode stays unreported so it retries)')
        return False

    sent = larry_alerts.append_notification(
        source='doorbell', intent='doorbell',
        message=format_stale_message(as_of, now), chat_id=chat_id,
    )
    if not sent:
        # Leave the episode unflagged so the next tick retries, exactly as the
        # count path does on a failed append.
        log(f'append_notification returned False (disk?) for stale snapshot '
            f'(as_of={as_of}); will retry')
        return False
    log(f'stale-substrate DM sent: snapshot as_of={as_of} exceeds '
        f'{MAX_SNAPSHOT_AGE_HOURS}h — suppressing the count DM')
    new_state = dict(state)
    new_state['stale_reported'] = True
    save_state(new_state)
    return True


def run(now: Optional[datetime] = None) -> bool:
    """One doorbell tick. Returns True iff a DM was sent. Never raises."""
    now = now or datetime.now(timezone.utc)
    doc = load_snapshot()
    if doc is None:
        log('no readable State Log snapshot — skipping (no nag on a broken read)')
        return False
    waiting = load_waiting(doc)
    if waiting is None:
        log('State Log snapshot has no readable waiting_on_larry — skipping '
            '(no nag on a broken read)')
        return False

    state = load_state()

    # Freshness gate BEFORE any count is trusted: a stale snapshot's numbers are
    # unfalsifiable, so the count DM is suppressed entirely and the outage is
    # reported once (see the STALE SUBSTRATE note in the module docstring).
    as_of = snapshot_as_of(doc)
    if is_stale(as_of, now):
        return _report_stale(state, as_of, now)

    # Fresh snapshot — re-arm the stale episode so a LATER freeze reports again.
    # Popping is the whole reset: every save below writes an explicit dict that
    # omits the flag, so a cleared episode never survives to disk.
    rearmed = bool(state.pop('stale_reported', False))
    if rearmed:
        log(f'State Log snapshot is fresh again (as_of={as_of}) — stale episode '
            f'cleared; normal doorbell cadence resumes')

    needs, approvals, escalations = _needs_count(waiting)
    prev = state.get('last_count')
    prev_count = prev if isinstance(prev, int) else 0
    last_dm = _parse_ts(state.get('last_dm_ts'))

    if needs <= 0:
        # Caught up — reset so the next new item rings promptly. `rearmed` forces
        # the write in the otherwise-no-op case (count already 0, never DM'd) so
        # the cleared flag is actually persisted.
        if rearmed or prev_count != 0 or state.get('last_dm_ts') is not None:
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
