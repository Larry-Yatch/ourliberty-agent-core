#!/usr/bin/env python3
"""readiness_trip_wire.py — second-build-team readiness trip-wire.

PURPOSE
-------
A deterministic, self-firing oneshot (systemd timer, every ~15min) that DMs
Larry EXACTLY when standing up a second build team / Claude account would
actually pay off — and stays silent otherwise. The whole design point is to
NOT cry wolf: a second team only helps if real work is genuinely queued behind
a pipeline that is simultaneously SATURATED (every concurrency slot in use) and
QUOTA-WALLED (recent rate-limit hits). Any single soft signal — a brief backlog
spike, a momentary full slot table, one stray rate-limit — must NOT fire.

THE ALL-MUST-HOLD FIRING RULE (and why each signal is necessary)
----------------------------------------------------------------
Three signals are sampled per tick from the agents runtime root:

  (a) FORGE BACKLOG — depth of ~/agents/inboxes/forge (real *.json task
      envelopes) AND the age of the OLDEST queued item. Depth alone is noise
      (a burst drains in minutes); pairing it with oldest-wait proves work is
      STUCK, not just briefly buffered. Necessary because a second team helps
      ONLY if there is durably-queued work for it to take.

  (b) CONCURRENCY SATURATION — active claude slots in
      ~/agents/config/.concurrency-guard.json at/above the cap (clamped to the
      real RAM ceiling of 6). Necessary because if slots are free, the EXISTING
      team can already drain the backlog — a second team adds nothing. Only a
      pinned-full pipeline means we are out of throughput on THIS account.

  (c) RATE-LIMIT PRESSURE — count of failure_class=="rate_limit" rows in
      ~/agents/blackboard/anthropic-quota-events.jsonl within the recent window.
      Necessary because saturation that is NOT quota-walled is solved by more
      local concurrency (raise the cap / bigger VM), NOT by a second ACCOUNT.
      A second team/account specifically relieves the quota wall — so without
      recent rate-limit events, provisioning one buys nothing.

FIRE ONLY when, SUSTAINED across at least K consecutive ticks spanning ~2h:
    backlog_depth >= T_DEPTH AND oldest_wait_sec >= T_AGE
    AND concurrency_active >= cap AND rate_limit_count_recent >= 1.

Requiring all four jointly, sustained for hours, is the false-alarm guard: a
transient blip on any axis resets the streak. "Sustained" is judged from this
watcher's OWN rolling history of per-tick samples, not a single reading.

COOLDOWN
--------
On top of larry_alerts' built-in 60-min warning cooldown, this watcher keeps a
self-managed MULTI-DAY cooldown (default 3 days) via `last_fired_at` in its
state file, so a genuinely-saturated steady state pages Larry at most once every
few days rather than nagging every 15 minutes.

WARM-UP
-------
On first run the watcher records a `monitoring_since` baseline and suppresses
firing until at least K ticks of real history have accumulated. A cold/empty
droplet (no history, no signal files) therefore can never false-fire on boot.

READ-ONLY EXCEPT ITS OWN STATE FILE
-----------------------------------
This watcher only READS the three signal sources; the single file it writes is
its own state at ~/agents/state/readiness-trip-wire.json (rolling history +
monitoring_since + last_fired_at). It mutates nothing else. main() never raises
— it returns 0 fail-open on any unexpected error (logged to stderr) so a timer
tick can never wedge.

Modeled on heal_credential_registry_drift.py (state-file dedup, AGENTS_ROOT
handling, fail-open main), audit_cadence_signal.py (append_alert signature),
and heal_pulse_check_staleness.py (monitoring_since warm-up baseline). Stdlib
only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# -------------------- paths (resolved at call time, honor sandbox) --------------------

def agents_root() -> Path:
    """The agents runtime root. Honors OURLIBERTY_AGENTS_ROOT (set by the test
    bootstrap / sandbox); defaults to ~/agents. Resolved at call time — not
    frozen at import — so a test redirect lands."""
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def kill_switch() -> Path:
    return agents_root() / 'healers.disabled'


def state_file() -> Path:
    return agents_root() / 'state' / 'readiness-trip-wire.json'


def forge_inbox() -> Path:
    return agents_root() / 'inboxes' / 'forge'


def concurrency_guard_file() -> Path:
    return agents_root() / 'config' / '.concurrency-guard.json'


def rate_limit_ledger() -> Path:
    return agents_root() / 'blackboard' / 'anthropic-quota-events.jsonl'


def log_file() -> Path:
    return agents_root() / 'logs' / 'readiness-trip-wire.log'


# -------------------- thresholds (module constants, env-overridable) --------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log(f'{name}={raw!r} is not an integer; using default {default}', 'WARN')
        return default


# Number of consecutive sustained ticks (and a wall-clock span) required before
# firing. K=8 ticks at the 15-min timer cadence spans ~2h — long enough that no
# transient blip clears the bar. Env: OURLIBERTY_TRIPWIRE_K.
K_TICKS = _env_int('OURLIBERTY_TRIPWIRE_K', 8)

# Minimum wall-clock span (seconds) the K sustained ticks must cover, so a burst
# of rapid manual --once runs can't satisfy K in seconds. ~2h default.
MIN_SUSTAINED_SPAN_SEC = _env_int('OURLIBERTY_TRIPWIRE_MIN_SPAN_SEC', 2 * 3600)

# Forge backlog must be at least this deep. Env: OURLIBERTY_TRIPWIRE_DEPTH.
T_DEPTH = _env_int('OURLIBERTY_TRIPWIRE_DEPTH', 3)

# The OLDEST queued forge item must have waited at least this long (seconds) —
# proves the backlog is stuck, not briefly buffered. 1h default.
# Env: OURLIBERTY_TRIPWIRE_AGE_SEC.
T_AGE = _env_int('OURLIBERTY_TRIPWIRE_AGE_SEC', 3600)

# Lookback window (hours) for counting recent rate_limit events.
# Env: OURLIBERTY_TRIPWIRE_RATE_WINDOW_HOURS.
RATE_WINDOW_HOURS = _env_int('OURLIBERTY_TRIPWIRE_RATE_WINDOW_HOURS', 3)

# At least this many rate_limit events inside the window. Env:
# OURLIBERTY_TRIPWIRE_RATE_MIN.
T_RATE_MIN = _env_int('OURLIBERTY_TRIPWIRE_RATE_MIN', 1)

# Self-managed multi-day cooldown (seconds) between fires, on top of
# larry_alerts' 60-min warning cooldown. 3 days default. Env:
# OURLIBERTY_TRIPWIRE_COOLDOWN_SEC.
FIRE_COOLDOWN_SEC = _env_int('OURLIBERTY_TRIPWIRE_COOLDOWN_SEC', 3 * 24 * 3600)

# Keep the rolling history bounded so the state file never grows unbounded.
HISTORY_MAX = 50

# The real RAM ceiling for concurrent claude processes. Mirrors
# concurrency_guard._MAX_CONCURRENT_CEILING; imported when cleanly importable,
# else this fallback (kept in sync intentionally).
_MAX_CONCURRENT_CEILING_FALLBACK = 6

ALERT_SOURCE = 'readiness-trip-wire'
ALERT_SUBJECT = 'second-build-team-readiness'


# -------------------- logging --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, file=sys.stderr, flush=True)
    try:
        lf = log_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


# -------------------- kill switch --------------------

def kill_switch_active() -> bool:
    return kill_switch().exists()


# -------------------- state file --------------------

def load_state() -> dict[str, Any]:
    """Return the watcher state dict. Shape:
        {'history': [sample, ...], 'monitoring_since': iso, 'last_fired_at': iso}
    Missing / corrupt => a fresh empty state (safe direction: re-baseline +
    re-accumulate history rather than crash)."""
    try:
        data = json.loads(state_file().read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return {'history': []}
        data.setdefault('history', [])
        if not isinstance(data['history'], list):
            data['history'] = []
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'history': []}


def save_state(state: dict[str, Any]) -> None:
    try:
        import atomic_io
        state_file().parent.mkdir(parents=True, exist_ok=True)
        atomic_io.atomic_write_json(state_file(), state, indent=2)
    except Exception as e:  # noqa: BLE001 — a watcher must not die on a bad write
        log(f'save_state failed: {type(e).__name__}: {e}', 'WARN')


# -------------------- time helpers --------------------

def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime. Naive input is
    normalized to UTC (mirrors the costs-analysis convention). None on failure."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# -------------------- signal (a): forge backlog --------------------

def _scan_forge_backlog() -> tuple[int, float]:
    """Return (depth, oldest_wait_sec) for the forge inbox.

    Counts real *.json task envelopes using the SAME filter as
    inbox_watcher.scan_inbox (file, not dotfile, .json suffix — which also
    excludes the .archive/.invalid subdirs, since those are directories and the
    dotfile rule covers them). Tries to import scan_inbox for a single source of
    truth; replicates the exact filter if the import isn't clean.

    oldest_wait_sec = now - mtime of the OLDEST queued item (0.0 when empty)."""
    inbox = forge_inbox()
    paths: list[Path] = []
    try:
        import inbox_watcher
        # inbox_watcher resolves INBOXES_ROOT from OURLIBERTY_AGENTS_ROOT at
        # import; only trust its scan when it points at the SAME inbox we do.
        if getattr(inbox_watcher, 'INBOXES_ROOT', None) == inbox.parent:
            paths = list(inbox_watcher.scan_inbox('forge'))
        else:
            paths = _scan_inbox_local(inbox)
    except Exception:  # noqa: BLE001 — fall back to the replicated filter
        paths = _scan_inbox_local(inbox)

    depth = len(paths)
    if depth == 0:
        return 0, 0.0
    now = datetime.now(timezone.utc).timestamp()
    oldest_mtime = now
    for p in paths:
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if mt < oldest_mtime:
            oldest_mtime = mt
    return depth, max(0.0, now - oldest_mtime)


def _scan_inbox_local(inbox: Path) -> list[Path]:
    """Replicate inbox_watcher.scan_inbox's filter verbatim (file, not dotfile,
    .json suffix), independent of import. Returned newest-last by mtime."""
    if not inbox.exists():
        return []
    entries: list[tuple[float, Path]] = []
    try:
        for e in os.scandir(inbox):
            if not e.is_file() or e.name.startswith('.') or not e.name.endswith('.json'):
                continue
            try:
                entries.append((e.stat().st_mtime, Path(e.path)))
            except OSError:
                continue
    except OSError:
        return []
    entries.sort(key=lambda x: x[0])
    return [p for _, p in entries]


# -------------------- signal (b): concurrency saturation --------------------

def _concurrency_ceiling() -> int:
    """The real RAM ceiling for concurrent claude processes. Imported from
    concurrency_guard when clean, else the local fallback (kept in sync)."""
    try:
        import concurrency_guard
        ceiling = getattr(concurrency_guard, '_MAX_CONCURRENT_CEILING', None)
        if isinstance(ceiling, int) and ceiling > 0:
            return ceiling
    except Exception:  # noqa: BLE001
        pass
    return _MAX_CONCURRENT_CEILING_FALLBACK


def _scan_concurrency() -> tuple[int, int]:
    """Return (active_count, cap).

    active_count = number of slots currently recorded in the guard file (a
    direct read — NOT a liveness reap, which a read-only watcher must not do).
    cap = the guard file's own "max", CLAMPED to the real ceiling so a
    tampered/over-large max can't make saturation impossible to detect. An
    empty/corrupt/missing guard reads as (0, ceiling) — NOT saturated — the
    conservative direction (don't fire on an unreadable guard)."""
    ceiling = _concurrency_ceiling()
    path = concurrency_guard_file()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return 0, ceiling
    if not isinstance(data, dict):
        return 0, ceiling
    slots = data.get('slots')
    active = len(slots) if isinstance(slots, list) else 0
    raw_max = data.get('max')
    cap = raw_max if isinstance(raw_max, int) and raw_max > 0 else ceiling
    cap = min(cap, ceiling)
    return active, cap


# -------------------- signal (c): rate-limit pressure --------------------

def _count_recent_rate_limits(now: Optional[datetime] = None) -> int:
    """Count failure_class=="rate_limit" rows in the quota-events ledger whose
    `ts` is within the last RATE_WINDOW_HOURS. Naive ts normalized to UTC.
    Missing ledger / unparseable rows => not counted (read-only, fail-quiet)."""
    now = now or datetime.now(timezone.utc)
    path = rate_limit_ledger()
    if not path.exists():
        return 0
    cutoff = now.timestamp() - RATE_WINDOW_HOURS * 3600
    count = 0
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get('failure_class') != 'rate_limit':
                    continue
                ts = _parse_iso(rec.get('ts'))
                if ts is None:
                    continue
                if ts.timestamp() >= cutoff:
                    count += 1
    except OSError:
        return 0
    return count


# -------------------- sampling --------------------

def sample_signals(now: Optional[datetime] = None) -> dict[str, Any]:
    """Take one read-only sample of all three signals. Returns a sample dict
    (also the per-tick history record)."""
    now = now or datetime.now(timezone.utc)
    depth, oldest_wait = _scan_forge_backlog()
    active, cap = _scan_concurrency()
    rate_count = _count_recent_rate_limits(now=now)
    return {
        'ts': now.isoformat(),
        'backlog_depth': depth,
        'oldest_wait_sec': round(oldest_wait, 1),
        'concurrency_active': active,
        'cap': cap,
        'rate_limit_count': rate_count,
    }


def _sample_hot(sample: dict[str, Any]) -> bool:
    """True iff a single sample meets ALL four threshold conditions."""
    return (
        sample.get('backlog_depth', 0) >= T_DEPTH
        and sample.get('oldest_wait_sec', 0) >= T_AGE
        and sample.get('concurrency_active', 0) >= sample.get('cap', _MAX_CONCURRENT_CEILING_FALLBACK)
        and sample.get('rate_limit_count', 0) >= T_RATE_MIN
    )


# -------------------- decision --------------------

def evaluate(state: dict[str, Any], sample: dict[str, Any],
             now: Optional[datetime] = None) -> tuple[bool, str]:
    """Decide whether to fire, given the rolling history (with `sample` already
    the latest entry). Returns (should_fire, human_reason).

    Fires ONLY when the most recent K samples are ALL hot (every threshold met)
    AND those K samples span at least MIN_SUSTAINED_SPAN_SEC of wall-clock AND
    the watcher has been monitoring long enough (warm-up) AND we are outside the
    multi-day fire cooldown."""
    now = now or datetime.now(timezone.utc)
    history = state.get('history', [])

    # Warm-up: need at least K ticks of real history before any fire can occur.
    if len(history) < K_TICKS:
        return False, (
            f'warm-up: {len(history)}/{K_TICKS} ticks of history so far '
            f'(need {K_TICKS} before firing is possible)'
        )

    # Self-managed multi-day cooldown.
    last_fired = _parse_iso(state.get('last_fired_at'))
    if last_fired is not None:
        age = (now - last_fired).total_seconds()
        if age < FIRE_COOLDOWN_SEC:
            remaining_h = (FIRE_COOLDOWN_SEC - age) / 3600.0
            return False, (
                f'in fire cooldown: last fired {last_fired.isoformat()} '
                f'({remaining_h:.1f}h of {FIRE_COOLDOWN_SEC / 3600.0:.0f}h '
                f'cooldown remaining)'
            )

    recent = history[-K_TICKS:]
    hot = [_sample_hot(s) for s in recent]
    if not all(hot):
        cold_idx = [i for i, h in enumerate(hot) if not h]
        return False, (
            f'not sustained: {sum(hot)}/{K_TICKS} of the last {K_TICKS} ticks '
            f'met all signals (tick(s) {cold_idx} cold) — a single soft signal '
            f'resets the streak'
        )

    # Wall-clock span of the K hot ticks.
    first_ts = _parse_iso(recent[0].get('ts'))
    last_ts = _parse_iso(recent[-1].get('ts'))
    if first_ts is None or last_ts is None:
        return False, 'cannot determine sustained span (unparseable ts in history)'
    span = (last_ts - first_ts).total_seconds()
    if span < MIN_SUSTAINED_SPAN_SEC:
        return False, (
            f'sustained streak too short in wall-clock: {span / 3600.0:.2f}h '
            f'< required {MIN_SUSTAINED_SPAN_SEC / 3600.0:.2f}h '
            f'(guards against a burst of rapid manual ticks)'
        )

    return True, (
        f'FIRE: all {K_TICKS} of the last {K_TICKS} ticks met every signal, '
        f'sustained {span / 3600.0:.2f}h — backlog>= {T_DEPTH} & oldest-wait>= '
        f'{T_AGE}s & concurrency saturated & >= {T_RATE_MIN} rate-limit(s) in '
        f'the last {RATE_WINDOW_HOURS}h'
    )


# -------------------- alert rendering --------------------

def _render_alert(sample: dict[str, Any]) -> tuple[str, str]:
    """Build (message, suggested_action) — plain-English, numbers first."""
    depth = sample.get('backlog_depth', 0)
    wait_h = sample.get('oldest_wait_sec', 0) / 3600.0
    active = sample.get('concurrency_active', 0)
    cap = sample.get('cap', _MAX_CONCURRENT_CEILING_FALLBACK)
    rate = sample.get('rate_limit_count', 0)
    message = (
        f'The build pipeline has been saturated AND quota-walled for the last '
        f'~{MIN_SUSTAINED_SPAN_SEC / 3600.0:.0f}h straight — a second build '
        f'team / Claude account may now be worth provisioning.\n'
        f'\n'
        f'What is saturated (all sustained for {K_TICKS} ticks):\n'
        f'  - Forge backlog: {depth} task(s) queued, oldest waiting '
        f'{wait_h:.1f}h (work is genuinely stuck, not just briefly buffered).\n'
        f'  - Concurrency: {active}/{cap} slots in use — the pipeline is '
        f'pinned full, so the existing team cannot drain the queue faster.\n'
        f'  - Rate limits: {rate} hit(s) in the last {RATE_WINDOW_HOURS}h — '
        f'throughput is capped by the Claude quota, not just local concurrency.\n'
        f'\n'
        f'Because all three held together for hours, more local concurrency '
        f'alone would not help (the wall is the quota). A second team on a '
        f'distinct account is the lever that adds throughput here. This '
        f'trip-wire stays silent unless all of the above are jointly sustained, '
        f'and will not re-fire for '
        f'{FIRE_COOLDOWN_SEC / (24 * 3600.0):.0f} days.'
    )
    suggested = (
        'Decide whether to provision a second build team / Claude account. '
        'Context: account-rotation + concurrency sizing — see '
        'agents/beacon/specs/account-rotation.md and '
        'concurrency_guard._MAX_CONCURRENT_CEILING. No automatic action is '
        'taken; this is a readiness signal only.'
    )
    return message, suggested


def fire(sample: dict[str, Any]) -> bool:
    """Emit the readiness DM via larry_alerts. Returns True on append, False on
    cooldown/error. Never raises."""
    message, suggested = _render_alert(sample)
    try:
        import larry_alerts
        return larry_alerts.append_alert(
            source=ALERT_SOURCE,
            severity='warning',
            message=message,
            subject=ALERT_SUBJECT,
            suggested_action=suggested,
        )
    except Exception as e:  # noqa: BLE001
        log(f'fire: append_alert failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- tick orchestration --------------------

def run_once(now: Optional[datetime] = None, dry_run: bool = False,
             persist: bool = True) -> dict[str, Any]:
    """One tick: sample → append to history → evaluate → (maybe) fire.

    Args:
        now: time anchor (tests pass a fixed time).
        dry_run: compute + decide but NEVER emit and NEVER write last_fired_at.
            History IS still appended (a dry-run tick is a real observation).
        persist: write the state file at the end (tests may suppress).

    Returns a result dict: {sample, should_fire, reason, fired, dry_run}.
    """
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {
        'sample': None, 'should_fire': False, 'reason': '', 'fired': False,
        'dry_run': dry_run,
    }

    if kill_switch_active():
        result['reason'] = f'kill-switch active at {kill_switch()}; no-op'
        log(result['reason'])
        return result

    state = load_state()

    # Warm-up baseline: record the first-observed epoch once.
    if not state.get('monitoring_since'):
        state['monitoring_since'] = now.isoformat()

    sample = sample_signals(now=now)
    state.setdefault('history', []).append(sample)
    # Bound the rolling history.
    if len(state['history']) > HISTORY_MAX:
        state['history'] = state['history'][-HISTORY_MAX:]
    result['sample'] = sample

    should_fire, reason = evaluate(state, sample, now=now)
    result['should_fire'] = should_fire
    result['reason'] = reason

    if should_fire and not dry_run:
        if fire(sample):
            state['last_fired_at'] = now.isoformat()
            result['fired'] = True
            log(f'FIRED readiness trip-wire: {reason}')
        else:
            log('readiness trip-wire: append_alert suppressed (cooldown or '
                'write error); last_fired_at NOT advanced', 'WARN')
    else:
        log(f'tick: dry_run={dry_run} should_fire={should_fire} :: {reason}')

    if persist:
        save_state(state)
    return result


# -------------------- status / explain --------------------

def _status_text(now: Optional[datetime] = None) -> str:
    """Human-readable current-signals + would-it-fire report. Pure read — does
    NOT append to history or touch the state file."""
    now = now or datetime.now(timezone.utc)
    state = load_state()
    sample = sample_signals(now=now)
    # Evaluate against a hypothetical history with this sample as the latest,
    # WITHOUT mutating persisted state.
    hypo = dict(state)
    hypo_history = list(state.get('history', [])) + [sample]
    hypo['history'] = hypo_history
    should_fire, reason = evaluate(hypo, sample, now=now)
    lines = [
        'readiness-trip-wire status',
        f'  monitoring_since : {state.get("monitoring_since", "(not yet)")}',
        f'  history ticks    : {len(state.get("history", []))} '
        f'(need {K_TICKS} to arm)',
        f'  last_fired_at    : {state.get("last_fired_at", "(never)")}',
        '',
        'current sample:',
        f'  backlog_depth      : {sample["backlog_depth"]}  (threshold >= {T_DEPTH})',
        f'  oldest_wait_sec    : {sample["oldest_wait_sec"]}  '
        f'(threshold >= {T_AGE})',
        f'  concurrency_active : {sample["concurrency_active"]}  '
        f'(cap {sample["cap"]}; saturated when active >= cap)',
        f'  rate_limit_count   : {sample["rate_limit_count"]}  '
        f'(last {RATE_WINDOW_HOURS}h; threshold >= {T_RATE_MIN})',
        f'  this sample hot    : {_sample_hot(sample)}',
        '',
        f'would fire now? {should_fire}',
        f'why: {reason}',
    ]
    return '\n'.join(lines)


# -------------------- CLI --------------------

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='readiness_trip_wire',
        description=(
            'Second-build-team readiness trip-wire. Samples forge backlog, '
            'concurrency saturation, and recent rate-limit pressure; DMs Larry '
            'ONLY when all three are jointly sustained for hours (a second team '
            'would actually pay off), and stays silent otherwise. Read-only '
            'except its own state file.'
        ),
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument(
        '--once', action='store_true',
        help='Run one tick (sample + decide + maybe emit). This is the default.',
    )
    g.add_argument(
        '--dry-run', action='store_true',
        help='Compute the decision and print signals, but NEVER emit and NEVER '
             'write last_fired_at. The tick is still recorded in history (a '
             'dry-run is a real observation).',
    )
    g.add_argument(
        '--status', '--explain', dest='status', action='store_true',
        help='Print the current signals and why the trip-wire would or would '
             'not fire. Pure read — does not modify state or history.',
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    try:
        args = _parse_args(argv)
        if args.status:
            print(_status_text())
            return 0
        if args.dry_run:
            result = run_once(dry_run=True)
            print(f'[readiness-trip-wire] DRY-RUN should_fire='
                  f'{result["should_fire"]} :: {result["reason"]}')
            if result.get('sample'):
                print(f'  sample: {json.dumps(result["sample"])}')
            return 0
        # Default (--once or no flag): a real tick.
        run_once()
        return 0
    except Exception as e:  # noqa: BLE001 — fail-open: a timer tick must never wedge
        log(f'FATAL (fail-open): {type(e).__name__}: {e}', 'ERROR')
        return 0


if __name__ == '__main__':
    sys.exit(main())
