#!/usr/bin/env python3
"""ci_runner_watchdog.py — says so when RSDPM's pull-request CI runner (Larry's
spare Mac) drops off GitHub, and keeps pull-request checks running on the
droplet until it is back.

WHY THIS EXISTS
---------------
2026-10-26 (a Monday) the spare Mac shut itself down at 06:08 — a 2012
"shut down every Monday at 6:00 AM" schedule nobody knew it carried — and
stayed dark for eleven hours. Every pull-request check in that window queued
on the `proofs` / `vitest` labels that only that Mac carries, three PRs merged
on local test runs instead, and nothing told Larry: a queued check looks the
same as a slow one. He could have pressed the power button at any time.
The schedule is cancelled; this is the net for the next cause.

WHAT IT DOES, each tick (a 5-minute systemd timer)
--------------------------------------------------
  1. Reads the repo's self-hosted runners (`gh api …/actions/runners`).
  2. If WATCHED is offline for OFFLINE_TICKS_BEFORE_ACTION consecutive ticks
     (two — a reconnect blip never pages):
       a. moves each of its ROLE labels (FALLBACK) onto the named droplet
          runner — pull-request checks then run there, slower, instead of
          queuing — recording in STATE_FILE exactly which labels IT added;
       b. DMs Larry once, in plain words, with what to do at the Mac, and
          again every REMIND_SEC while it stays down.
  3. When WATCHED is online again: removes only the labels it added (a label
     a person put there by hand is never touched), DMs "back", clears state.

WHAT IT NEVER DOES
------------------
  * never changes the watched runner's own labels;
  * never removes a label it did not record adding;
  * never treats a failed GitHub read as "offline" — a read failure leaves
    state untouched, and after FETCH_FAILURES_BEFORE_ALERT consecutive
    failures DMs once that the watchdog itself is blind;
  * never pages on the first offline tick.

The droplet-down direction is not covered: this runs ON the droplet. The
workflow header (RSDPM .github/workflows/test.yml) carries that hand fallback.

Kill-switch: ~/agents/healers.disabled (blanket, same as every healer).
Stdlib + larry_alerts + atomic_io only.

Run by hand:  python3 scripts/ci_runner_watchdog.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import larry_alerts  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'ci-runner-watchdog.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'ci-runner-watchdog.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'ci-runner-watchdog.json'

REPO = 'Larry-Yatch/RSDPM'
SOURCE = 'ci-runner-watchdog'

# The runner we watch, and where each of its ROLE labels goes while it is away.
# Mirrors the "Mac down" lines of the workflow header in RSDPM
# .github/workflows/test.yml — change both or neither.
WATCHED = 'mac-2019'
FALLBACK = {'proofs': 'droplet-a', 'vitest': 'droplet-b'}

OFFLINE_TICKS_BEFORE_ACTION = 2    # two 5-min ticks: a reconnect blip never pages
REMIND_SEC = 6 * 3600              # while it stays down, say so again this often
FETCH_FAILURES_BEFORE_ALERT = 6    # 30 min of GitHub read failures = the watchdog is blind

# What Larry runs from his laptop when the Mac looks on but the runner is not.
MAC_SERVICE_CMD = (
    "ssh -6 -t Larry@Larrys-Mac-2021.local "
    "'sudo launchctl print system/com.ourliberty.actions-runner | grep state'"
)

# Injected IO, so `tick` is testable without GitHub or Telegram.
AddLabel = Callable[[int, str], bool]
RemoveLabel = Callable[[int, str], bool]
Alert = Callable[[str, str, Optional[float], float, list], bool]


# -------------------- plumbing --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat() + '\n')
    except OSError:
        pass


def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


def load_state() -> dict:
    """The state file, or {} — a missing or unreadable file is the zeroth run,
    never a reason to crash or to invent an outage."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log(f'state file unreadable ({type(e).__name__}: {e}); starting from empty', 'WARN')
        return {}
    if not isinstance(data, dict):
        log('state file is not an object; starting from empty', 'WARN')
        return {}
    return data


def save_state(state: dict) -> None:
    try:
        atomic_write_json(STATE_FILE, state, trailing_newline=True)
    except OSError as e:
        log(f'state save failed: {e}', 'WARN')


def gh_json(*args: str) -> Optional[Any]:
    """`gh api …` parsed, or None on any failure (logged). None is "could not
    read", which callers must never confuse with "offline"."""
    try:
        r = subprocess.run(['gh', 'api', *args], capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as e:
        log(f'gh api {args[0]}: {type(e).__name__}: {e}', 'WARN')
        return None
    if r.returncode != 0:
        log(f'gh api {args[0]}: rc={r.returncode} {r.stderr.strip()[:200]}', 'WARN')
        return None
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError as e:
        log(f'gh api {args[0]}: bad JSON: {e}', 'WARN')
        return None


def fetch_runners() -> Optional[dict]:
    """name → {id, status, busy, labels:set}, or None when GitHub could not be read."""
    data = gh_json(f'repos/{REPO}/actions/runners?per_page=100')
    if not isinstance(data, dict) or not isinstance(data.get('runners'), list):
        if data is not None:
            log('runners payload has no `runners` list', 'WARN')
        return None
    out = {}
    for r in data['runners']:
        try:
            out[r['name']] = {
                'id': int(r['id']),
                'status': r.get('status'),
                'busy': bool(r.get('busy')),
                'labels': {lbl['name'] for lbl in r.get('labels', [])},
            }
        except (KeyError, TypeError, ValueError):
            log(f'skipping malformed runner record: {str(r)[:120]}', 'WARN')
    return out


def add_label(runner_id: int, label: str) -> bool:
    return gh_json('-X', 'POST', f'repos/{REPO}/actions/runners/{runner_id}/labels',
                   '-f', f'labels[]={label}') is not None


def remove_label(runner_id: int, label: str) -> bool:
    """True once the label is not on the runner — a 404 (already gone) counts."""
    try:
        r = subprocess.run(
            ['gh', 'api', '-X', 'DELETE', f'repos/{REPO}/actions/runners/{runner_id}/labels/{label}'],
            capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as e:
        log(f'remove label {label} from {runner_id}: {type(e).__name__}: {e}', 'WARN')
        return False
    if r.returncode == 0 or 'HTTP 404' in r.stderr:
        return True
    log(f'remove label {label} from {runner_id}: rc={r.returncode} {r.stderr.strip()[:200]}', 'WARN')
    return False


# -------------------- the words Larry reads --------------------

def _when(ts: Optional[float]) -> str:
    if ts is None:
        return 'an unknown time'
    return datetime.fromtimestamp(ts).astimezone().strftime('%a %-I:%M %p %Z')


def _dur(since: Optional[float], now: float) -> str:
    if since is None:
        return 'a while'
    mins = int(max(0.0, now - since) // 60)
    if mins < 60:
        return f'{mins} min'
    h, m = divmod(mins, 60)
    return f'{h} h {m:02d} min'


def offline_message(name: str, since: Optional[float], now: float, fallback: list) -> str:
    moved = ', '.join(f'{label} → {runner}' for runner, label in fallback)
    parts = [
        f'The spare Mac (CI runner {name}) has been offline since {_when(since)} '
        f'({_dur(since, now)}). Pull-request checks that run there are queuing.',
    ]
    if fallback:
        parts.append(
            f'I moved them to the droplet for now ({moved}). It is slower, and its '
            'unit tests flake about one run in two there, so a red vitest on the '
            'droplet may be the host, not the code.')
    else:
        parts.append('The droplet fallback could not be applied yet; checks are waiting.')
    parts.append(
        'Walk to the Mac: if it is off, press the power button; if it shows a '
        'password screen, type it. If it looks on, run the command below. '
        'The checks move back to the Mac on their own when it reconnects.')
    return ' '.join(parts)


def back_message(name: str, since: Optional[float], now: float, removed: list) -> str:
    tail = '; the droplet fallback is removed.' if removed else '.'
    return (f'The spare Mac (CI runner {name}) is back online after {_dur(since, now)}. '
            f'Pull-request checks are on it again{tail}')


def send_alert(kind: str, name: str, since: Optional[float], now: float, pairs: list) -> bool:
    """One DM to Larry. Returns False when nothing was sent (cooldown, write
    failure) so the caller retries next tick instead of believing it told him."""
    if kind == 'offline':
        return larry_alerts.append_alert(
            SOURCE, 'warning', offline_message(name, since, now, pairs),
            subject=f'{name}-offline', suggested_action=MAC_SERVICE_CMD,
            route='escalate', needs_larry=True)
    if kind == 'back':
        return larry_alerts.append_alert(
            SOURCE, 'info', back_message(name, since, now, pairs),
            subject=f'{name}-back', route='escalate')
    if kind == 'blind':
        return larry_alerts.append_alert(
            SOURCE, 'warning',
            f'The CI runner watchdog could not read GitHub for {_dur(since, now)} '
            f'(gh api on the droplet keeps failing), so it cannot see whether the '
            f'spare Mac is up. Check the Mac yourself until this clears.',
            subject='blind', suggested_action='gh api repos/Larry-Yatch/RSDPM/actions/runners',
            route='escalate', needs_larry=True)
    log(f'unknown alert kind {kind!r}', 'WARN')
    return False


# -------------------- the decision --------------------

def tick(runners: dict, state: dict, now: float, *,
         add_label: AddLabel, remove_label: RemoveLabel, alert: Alert) -> dict:
    """One observation of the runner list → the new state. All IO is injected.

    state[WATCHED] = {
      offline_ticks: int,        consecutive offline observations
      offline_since: float|None, first offline observation of this outage
      alerted_at:    float|None, last 'offline' DM that was actually sent
      fallback:      [[runner, label], …] labels THIS watchdog added
      back_sent:     bool        the 'back' DM for this outage was sent
    }
    An entry exists only during an outage or its unfinished recovery.
    """
    state = dict(state)
    s = dict(state.get(WATCHED) or {})
    s.setdefault('offline_ticks', 0)
    s.setdefault('offline_since', None)
    s.setdefault('alerted_at', None)
    s.setdefault('fallback', [])
    s.setdefault('back_sent', False)

    watched = runners.get(WATCHED)
    online = watched is not None and watched.get('status') == 'online'

    if online:
        s['offline_ticks'] = 0
        nothing_to_undo = s['offline_since'] is None and not s['fallback']
        if nothing_to_undo:
            state.pop(WATCHED, None)
            return state
        # Recovery — first time, or a retry of a recovery that did not finish.
        still = []
        removed = []
        for runner, label in s['fallback']:
            rid = (runners.get(runner) or {}).get('id')
            if rid is not None and remove_label(rid, label):
                removed.append([runner, label])
                log(f'{label} removed from {runner} (back on {WATCHED})')
            else:
                still.append([runner, label])
                log(f'{label} still on {runner}; will retry', 'WARN')
        s['fallback'] = still
        was_announced = s['alerted_at'] is not None
        if was_announced and not s['back_sent']:
            s['back_sent'] = alert('back', WATCHED, s['offline_since'], now, removed)
        done = not s['fallback'] and (not was_announced or s['back_sent'])
        if done:
            log(f'{WATCHED} back online; outage of {_dur(s["offline_since"], now)} closed')
            state.pop(WATCHED, None)
        else:
            state[WATCHED] = s
        return state

    # Offline (or no longer registered — same thing for the labels' purpose).
    if s.get('back_sent'):
        # The previous outage was announced over; this is a new one.
        s = {'offline_ticks': 0, 'offline_since': None, 'alerted_at': None,
             'fallback': [], 'back_sent': False}
    s['offline_ticks'] += 1
    if s['offline_since'] is None:
        s['offline_since'] = now
    if s['offline_ticks'] < OFFLINE_TICKS_BEFORE_ACTION:
        log(f'{WATCHED} offline, tick {s["offline_ticks"]} of {OFFLINE_TICKS_BEFORE_ACTION}; waiting')
        state[WATCHED] = s
        return state

    watched_labels = set(watched['labels']) if watched else set(FALLBACK)
    for label, runner in FALLBACK.items():
        if label not in watched_labels:
            continue                       # the Mac does not carry this role today
        if [runner, label] in s['fallback']:
            continue                       # already moved by us
        target = runners.get(runner)
        if target is None or target.get('status') != 'online':
            log(f'{runner} is not online; cannot take {label}', 'WARN')
            continue
        if label in target['labels']:
            log(f'{label} already on {runner} (not ours); leaving it')
            continue
        if add_label(target['id'], label):
            s['fallback'].append([runner, label])
            log(f'{label} moved to {runner} while {WATCHED} is offline')
        else:
            log(f'could not add {label} to {runner}; will retry', 'WARN')

    due = s['alerted_at'] is None or now - s['alerted_at'] >= REMIND_SEC
    if due:
        ok = alert('offline', WATCHED, s['offline_since'], now, s['fallback'])
        if ok:
            s['alerted_at'] = now
        else:
            log('offline DM not sent (cooldown or write failure); will retry', 'WARN')
    state[WATCHED] = s
    return state


def note_fetch_result(state: dict, ok: bool, now: float, alert: Alert) -> dict:
    """Consecutive read failures are tracked so a blind watchdog says so once."""
    state = dict(state)
    f = dict(state.get('_fetch') or {})
    if ok:
        state.pop('_fetch', None)
        return state
    f['failures'] = int(f.get('failures', 0)) + 1
    f.setdefault('since', now)
    if f['failures'] >= FETCH_FAILURES_BEFORE_ALERT:
        last = f.get('alerted_at')
        if last is None or now - last >= REMIND_SEC:
            if alert('blind', WATCHED, f['since'], now, []):
                f['alerted_at'] = now
    state['_fetch'] = f
    return state


def main() -> int:
    if kill_switch_active():
        log('kill switch present; exiting')
        return 0
    heartbeat()
    state = load_state()
    now = time.time()
    runners = fetch_runners()
    if runners is None:
        state = note_fetch_result(state, False, now, send_alert)
        save_state(state)
        log('could not read runners; state left as it was', 'WARN')
        return 0
    state = note_fetch_result(state, True, now, send_alert)
    state = tick(runners, state, now, add_label=add_label, remove_label=remove_label, alert=send_alert)
    save_state(state)
    return 0


if __name__ == '__main__':
    sys.exit(main())
