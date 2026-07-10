#!/usr/bin/env python3
"""heal_pulse_check_staleness.py — watch the watchers.

A Pulse check (I-X) is the one fleet component with no liveness signal of its
own: when the Pulse agent's /cycle stops invoking it, it goes dark silently.
That is what hid the IX/X stall for ~2 days (cost-signal audit, 2026-06-03).

Each check now emits a success heartbeat via scripts/pulse_check_heartbeat.py
(blackboard/pulse-check-<id>.heartbeat). This watcher runs on an OnCalendar
timer and, for each check in config/pulse-check-cadence.json, alerts when the
last success signal is older than cadence_hours + grace_hours. Freshness-of-
success catches all three failure modes — errored, can't-run/missing-env, and
fully-silent timer death — where a bare try/except would miss the silent stop.

Routing: pulse-check-stale:<id> goes through the shared larry_alerts queue
(severity warning, default 'escalate' route) -> Beacon triage -> Larry as an
outcome/escalation. A dark check usually cannot self-resolve, so it will
legitimately reach Larry — as an outcome, not a routine ping.

Fail-closed: a check the watcher knows about (the canonical set, plus any
pulse-check-<id>.heartbeat present on disk) that has no cadence entry, or a
malformed entry, gets a pulse-check-no-cadence:<id> alert rather than being
silently unmonitored.

First-deploy quiet: the "never signalled" case (no heartbeat AND no artifact)
does NOT escalate during a check's first cadence_hours + grace_hours window.
Each check's monitoring_since (the epoch the watcher first observed it) is
persisted to blackboard/pulse-check-staleness-baseline.json; escalation of a
never-signalled check waits until now - monitoring_since exceeds that window.
This stops a first deploy (or any newly added check) from storming while
preserving fail-closed: a check that blows its whole first window with no signal
still escalates. The already-stale path (a signal exists but is older than
cadence+grace) is unaffected.

DETECTION ONLY. This does not fix WHY a scheduler stopped invoking a check —
that root-cause is a separate follow-up. Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Canonical check set. Independent of the config on purpose: a check present
# here but absent from the registry is a fail-closed alert, not a silent skip.
# (Check II was a deprecated draft that never shipped — the gap is by design.
# Check VII was retired 2026-07-07: the producer for its substrate
# ~/agents/state/pulse-cost-escalations.jsonl never shipped, so the check never
# ran once, and event_driven=true made this watcher skip it forever. Revive the
# script + cadence entry from git history if escalation-response logging ever
# ships.)
CANONICAL_CHECKS = ['i', 'iii', 'iv', 'v', 'vi', 'viii', 'ix', 'x']

# Bootstrap liveness fallback. Before the heartbeat-emitting check code has run
# even once (first deploy), use each check's existing dated proposal/audit
# artifact as the liveness signal. This is what lets the watcher flag the
# already-dark checks on first deploy (brief "Built-in validation") instead of
# false-alarming every check for lack of a brand-new heartbeat file. Once
# heartbeats exist they dominate (emitted every run, including clean skips).
#
# The checks are inconsistent about which directory they write to: most use
# pulse-check-<id>-proposals/, but Check I writes pulse-check-i/ and Check III
# writes pulse-check-iii/ (NOT -proposals). A hardcoded per-id list silently
# drifted on exactly that — the iii false alarm (2026-06-03) came from the
# fallback globbing pulse-check-iii-proposals/ for a check that writes
# pulse-check-iii/. So instead of a fragile per-id list, cover BOTH namings for
# every check id; the union can never miss whichever directory a check actually
# uses, and a newly-added check needs no edit here.
def artifact_globs_for(check_id: str) -> list[str]:
    return [
        f'pulse-check-{check_id}/check-{check_id}-*.json',
        f'pulse-check-{check_id}-proposals/check-{check_id}-*.json',
    ]

# A runner that fires on schedule but keeps skipping without running (e.g. the
# main-suite-guardian bounded-waiting on the regbaseline-warm single-flight lock
# and never acquiring it) emits a deferral signal instead of a success heartbeat.
# A FRESH deferral is 'alive but deferred' — the timer IS firing — so a single
# contended night must not read as gone-dark. But this many CONSECUTIVE deferred
# firings with zero completed runs is genuine starvation and still escalates,
# with an accurate lock-contention diagnosis rather than the scheduler-gap text.
STARVATION_DEFERRAL_THRESHOLD = 3

CONFIG_FILE = (
    Path(__file__).resolve().parent.parent / 'config' / 'pulse-check-cadence.json'
)


def agents_root() -> Path:
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def blackboard() -> Path:
    return agents_root() / 'blackboard'


def kill_switch() -> Path:
    return agents_root() / 'healers.disabled'


def healer_heartbeat() -> Path:
    return blackboard() / 'heal-pulse-check-staleness.heartbeat'


def log_file() -> Path:
    return agents_root() / 'logs' / 'heal-pulse-check-staleness.log'


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        lf = log_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        hb = healer_heartbeat()
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def load_cadence_config(path: Optional[Path] = None) -> dict:
    """Return {check_id: entry}. Raises on unreadable/invalid JSON so main()
    can fail loud rather than silently monitoring nothing."""
    cfg_path = path or CONFIG_FILE
    data = json.loads(cfg_path.read_text(encoding='utf-8'))
    checks = data.get('checks')
    if not isinstance(checks, dict):
        raise ValueError("cadence config missing a 'checks' object")
    return checks


def baseline_file(bb: Path) -> Path:
    return bb / 'pulse-check-staleness-baseline.json'


def load_baseline(bb: Path) -> dict[str, float]:
    """Return {check_id: monitoring_since_epoch}. Missing or corrupt => {} so a
    first run (or a clobbered file) simply re-baselines every check on this tick
    rather than crashing the watcher. A re-baseline is the safe direction: it
    grants a fresh warm-up window rather than false-firing."""
    try:
        data = json.loads(baseline_file(bb).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    since = data.get('monitoring_since') if isinstance(data, dict) else None
    if not isinstance(since, dict):
        return {}
    out: dict[str, float] = {}
    for cid, ts in since.items():
        if isinstance(cid, str) and isinstance(ts, (int, float)):
            out[cid] = float(ts)
    return out


def save_baseline(bb: Path, since: dict[str, float]) -> None:
    """Atomic tmp + replace of the monitoring_since map. Best-effort; never
    raises (a watcher must not die because it could not persist a baseline)."""
    payload = {
        '_schema': {
            'version': 1,
            'purpose': (
                'Per-check monitoring_since epoch: when this liveness watcher '
                'first observed each Pulse check. The "never signalled" branch '
                'suppresses escalation until now - monitoring_since exceeds '
                'cadence_hours + grace_hours, so a first deploy (or any newly '
                'added check) warms up quietly instead of storming. After that '
                'window with still no signal it escalates (fail-closed).'
            ),
        },
        'monitoring_since': since,
    }
    try:
        path = baseline_file(bb)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True),
                       encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        pass


def _read_heartbeat_ts(path: Path) -> Optional[float]:
    """Epoch seconds of the heartbeat's ts field; fall back to file mtime."""
    try:
        raw = path.read_text(encoding='utf-8').strip()
    except OSError:
        return None
    try:
        obj = json.loads(raw)
        ts = obj.get('ts') if isinstance(obj, dict) else None
        if ts:
            return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def latest_signal_ts(check_id: str, bb: Path) -> tuple[Optional[float], str]:
    """Newest liveness signal for a check: max of the heartbeat ts and the
    newest matching artifact mtime. Returns (epoch_or_None, source)."""
    candidates: list[tuple[float, str]] = []

    hb = bb / f'pulse-check-{check_id}.heartbeat'
    if hb.exists():
        ts = _read_heartbeat_ts(hb)
        if ts is not None:
            candidates.append((ts, 'heartbeat'))

    for pattern in artifact_globs_for(check_id):
        for artifact in bb.glob(pattern):
            try:
                candidates.append((artifact.stat().st_mtime, 'artifact'))
            except OSError:
                continue

    if not candidates:
        return None, 'none'
    ts, source = max(candidates, key=lambda c: c[0])
    return ts, source


def read_deferral(check_id: str, bb: Path) -> tuple[Optional[float], int]:
    """Newest deferral signal for a check: (ts_epoch_or_None, consecutive_count).

    The deferral file (blackboard/pulse-check-<id>.deferred) is written by a
    runner that fired on schedule but skipped WITHOUT running (lock contention).
    ts falls back to file mtime; a missing/malformed file reads as (None, 0)."""
    path = bb / f'pulse-check-{check_id}.deferred'
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, 0
    if not isinstance(obj, dict):
        return None, 0
    count = obj.get('consecutive')
    count = count if isinstance(count, int) and count >= 0 else 0
    ts_raw = obj.get('ts')
    ts: Optional[float] = None
    if ts_raw:
        try:
            ts = datetime.fromisoformat(ts_raw).timestamp()
        except (ValueError, TypeError):
            ts = None
    if ts is None:
        try:
            ts = path.stat().st_mtime
        except OSError:
            ts = None
    return ts, count


def deferral_verdict(
    check_id: str,
    entry: dict,
    bb: Path,
    now: float,
    threshold: float,
) -> tuple[bool, Optional[dict]]:
    """Decide whether a fresh deferral explains a stale/absent success signal.

    Returns (applies, result):
      * applies=False           -> no fresh deferral; caller runs its normal
                                    gone-dark path.
      * applies=True, result=None -> alive-but-deferred (under the starvation
                                    threshold): suppress the gone-dark alarm.
      * applies=True, result=<alert> -> starvation escalation with an accurate
                                    lock-contention diagnosis.
    ``threshold`` is the check's cadence+grace window in seconds: a deferral
    older than that is itself stale and no longer vouches for liveness."""
    ts, consecutive = read_deferral(check_id, bb)
    if ts is None or (now - ts) > threshold:
        return False, None
    if consecutive >= STARVATION_DEFERRAL_THRESHOLD:
        label = entry.get('label', check_id)
        return True, {
            'key': f'pulse-check-stale:{check_id}',
            'subject': f'pulse-check-stale:{check_id}',
            'message': (
                f'Pulse check {check_id} ({label}) has been DEFERRED '
                f'{consecutive} consecutive firings with no completed run: it '
                'fires on schedule each night but cannot acquire the '
                'regbaseline-warm single-flight lock within its bounded wait, so '
                'it is starving on lock contention. The timer IS invoking it — '
                'the guardian just never gets to run its green-guard.'
            ),
            'suggested_action': (
                'This is lock contention, NOT a scheduler gap. Check whether the '
                'regression-baseline warmer is holding '
                '/home/larry/agents/state/ol-regbaseline-warm.lock across the '
                '03:30 UTC window (journalctl -u ourliberty-main-suite-guardian '
                'and the regbaseline warmer). Fix by staggering the warmer off '
                'the guardian window or widening LOCK_WAIT_SEC in '
                'scripts/run_main_suite_guardian.sh. Logs: '
                '~/agents/logs/main-suite-guardian.log.'
            ),
        }
    return True, None


def discover_heartbeat_ids(bb: Path) -> set[str]:
    """Check ids that have a heartbeat file on disk (catches a check added to
    the fleet but never registered in the cadence config)."""
    found: set[str] = set()
    try:
        for hb in bb.glob('pulse-check-*.heartbeat'):
            name = hb.name[len('pulse-check-'):-len('.heartbeat')]
            if name:
                found.add(name)
    except OSError:
        pass
    return found


def evaluate_check(
    check_id: str,
    entry: Optional[dict],
    bb: Path,
    now: float,
    monitoring_since: Optional[float] = None,
) -> Optional[dict]:
    """Pure: return an alert dict ({key, subject, message, suggested_action})
    for this check, or None if it is fresh / event-driven / still warming up.

    ``monitoring_since`` is the epoch the watcher first observed this check (see
    the baseline file). It only affects the never-signalled branch: a check with
    no signal stays quiet until it has been monitored for a full
    cadence_hours + grace_hours window, so a first deploy or a newly added check
    does not storm. ``None`` means "first observation this tick" (still warming
    up). Already-stale checks (a signal exists but is older than the threshold)
    ignore monitoring_since and escalate as before."""
    if entry is None:
        return {
            'key': f'pulse-check-no-cadence:{check_id}',
            'subject': f'pulse-check-no-cadence:{check_id}',
            'message': (
                f'Pulse check {check_id} has no entry in '
                'config/pulse-check-cadence.json, so its liveness is not being '
                'monitored. Fail-closed: alerting rather than leaving it dark.'
            ),
            'suggested_action': (
                'Add a cadence_hours + grace_hours (or event_driven) entry for '
                f'"{check_id}" to config/pulse-check-cadence.json.'
            ),
        }

    if entry.get('event_driven'):
        return None  # external trigger; time-based staleness does not apply.

    cadence_h = entry.get('cadence_hours')
    if not isinstance(cadence_h, (int, float)) or cadence_h <= 0:
        return {
            'key': f'pulse-check-no-cadence:{check_id}',
            'subject': f'pulse-check-no-cadence:{check_id}',
            'message': (
                f'Pulse check {check_id} has a malformed cadence entry '
                f'(cadence_hours={cadence_h!r}); cannot evaluate staleness. '
                'Fail-closed: alerting rather than leaving it dark.'
            ),
            'suggested_action': (
                'Fix the cadence_hours value for '
                f'"{check_id}" in config/pulse-check-cadence.json.'
            ),
        }

    grace_h = entry.get('grace_hours', 0) or 0
    threshold = (cadence_h + grace_h) * 3600.0
    label = entry.get('label', check_id)

    ts, source = latest_signal_ts(check_id, bb)
    if ts is None:
        # Never emitted a heartbeat and no artifact on disk. Distinguish "the
        # watcher only just started monitoring this check" from "the check is
        # actually dark": stay quiet during the first cadence+grace window after
        # monitoring_since (warm-up), then escalate. monitoring_since=None means
        # this is the very first observation, so it is by definition inside the
        # warm-up window. This is what keeps a first deploy (and any future
        # newly added check) from storming, while still firing a true positive
        # for a check that blows its entire first window with no signal.
        warming_up = (
            monitoring_since is None or (now - monitoring_since) <= threshold
        )
        if warming_up:
            return None
        # A fresh deferral means the runner IS firing (it just can't acquire its
        # single-flight lock), so the silence is not a dead check — suppress
        # unless it has starved for STARVATION_DEFERRAL_THRESHOLD firings.
        applies, deferred_alert = deferral_verdict(
            check_id, entry, bb, now, threshold)
        if applies:
            return deferred_alert
        return {
            'key': f'pulse-check-stale:{check_id}',
            'subject': f'pulse-check-stale:{check_id}',
            'message': (
                f'Pulse check {check_id} ({label}) has never emitted a success '
                'heartbeat and has no recent artifact through its entire first '
                f'{cadence_h:g}h cadence + {grace_h:g}h grace window — it has '
                'most likely never run, or stops before its first heartbeat.'
            ),
            'suggested_action': _timer_remediation(check_id, entry),
        }

    age = now - ts
    if age <= threshold:
        return None

    # Success signal is stale. Before declaring the runner dead, consult its
    # deferral signal: a runner that keeps firing but skipping on lock contention
    # is alive-but-deferred (no gone-dark alarm) until it truly starves.
    applies, deferred_alert = deferral_verdict(
        check_id, entry, bb, now, threshold)
    if applies:
        return deferred_alert

    age_h = age / 3600.0
    return {
        'key': f'pulse-check-stale:{check_id}',
        'subject': f'pulse-check-stale:{check_id}',
        'message': (
            f'Pulse check {check_id} ({label}) has gone stale: last success '
            f'signal ({source}) is {age_h:.1f}h old, past the '
            f'{cadence_h:g}h cadence + {grace_h:g}h grace. The check has '
            'silently stopped firing — its scheduler is most likely not '
            'invoking it.'
        ),
        'suggested_action': _timer_remediation(check_id, entry),
    }


def _timer_remediation(check_id: str, entry: dict) -> str:
    """Remediation text for a dark time-cadenced check. Every Pulse check now
    fires from its own systemd timer (agent /cycle invocation was retired
    2026-07-07), so the diagnosis points at the timer/service unit — NOT at 'the
    Pulse /cycle'. ``entry['firing']`` names the unit in the cadence config."""
    firing = entry.get('firing')
    unit_hint = (
        f'the systemd timer/service that fires it ({firing})' if firing
        else 'the systemd timer/service that fires it'
    )
    return (
        f'This check fires from a systemd timer (agent-invoked scheduling is '
        f'retired), so the failure is in the timer, not a missed invocation. '
        f'Confirm {unit_hint} is enabled and firing (systemctl status + '
        'journalctl -u the unit), and that its runner exits 0. Logs: '
        '~/agents/logs/ (pulse-check-*.log or the runner-specific log).'
    )


def evaluate_all(
    config: dict,
    bb: Path,
    now: Optional[float] = None,
    log_fn: Optional[callable] = None,
) -> list[dict]:
    """Alert dicts for every stale / unmonitored check.

    Persists each check's monitoring_since on first observation (atomic write to
    the baseline file under ``bb``) and threads it into evaluate_check so the
    never-signalled branch warms up quietly before escalating. ``log_fn`` (set by
    main()) gets a one-line warm-up note per suppressed-but-dark check; left None
    in unit tests so the pure evaluation never writes to the real log."""
    now = now if now is not None else time.time()
    ids = set(CANONICAL_CHECKS) | discover_heartbeat_ids(bb) | set(config)
    baseline = load_baseline(bb)
    baseline_changed = False
    alerts: list[dict] = []
    for check_id in sorted(ids):
        monitoring_since = baseline.get(check_id)
        if monitoring_since is None:
            monitoring_since = baseline[check_id] = now
            baseline_changed = True
        alert = evaluate_check(check_id, config.get(check_id), bb, now,
                               monitoring_since=monitoring_since)
        if alert is not None:
            alerts.append(alert)
        elif log_fn is not None and _is_warming_up(
                check_id, config.get(check_id), bb, now, monitoring_since):
            since_iso = datetime.fromtimestamp(
                monitoring_since, timezone.utc).isoformat()
            log_fn(f'warming up: {check_id} has no liveness signal yet but is '
                   f'inside its first cadence+grace window (monitoring since '
                   f'{since_iso}); no DM')
    if baseline_changed:
        save_baseline(bb, baseline)
    return alerts


def _is_warming_up(
    check_id: str,
    entry: Optional[dict],
    bb: Path,
    now: float,
    monitoring_since: Optional[float],
) -> bool:
    """True iff this check produced no alert specifically because it is a
    time-cadenced check with no signal yet, still inside its first window. Used
    only for the quiet warm-up log — never for routing."""
    if not isinstance(entry, dict) or entry.get('event_driven'):
        return False
    cadence_h = entry.get('cadence_hours')
    if not isinstance(cadence_h, (int, float)) or cadence_h <= 0:
        return False
    ts, _ = latest_signal_ts(check_id, bb)
    if ts is not None:
        return False
    threshold = (cadence_h + (entry.get('grace_hours', 0) or 0)) * 3600.0
    return monitoring_since is None or (now - monitoring_since) <= threshold


def main() -> int:
    if kill_switch().exists():
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()

    try:
        config = load_cadence_config()
    except (OSError, ValueError, json.JSONDecodeError) as e:
        # The registry drives everything; an unreadable one is itself the
        # silent-death risk. Alert loudly and stop.
        log(f'cadence config unreadable: {type(e).__name__}: {e}', 'ERROR')
        _emit(
            subject='pulse-check-config-unreadable',
            message=(
                'config/pulse-check-cadence.json could not be loaded '
                f'({type(e).__name__}: {e}); the pulse-check liveness watcher '
                'is monitoring nothing until it is fixed.'
            ),
            suggested_action=(
                'Validate config/pulse-check-cadence.json (python3 -c '
                '"import json,sys; json.load(open(sys.argv[1]))" '
                'config/pulse-check-cadence.json).'
            ),
        )
        return 1

    alerts = evaluate_all(config, blackboard(), log_fn=log)
    if not alerts:
        log('tick: all pulse checks fresh (or warming up)')
        return 0

    fired = 0
    for alert in alerts:
        delivered = _emit(
            subject=alert['subject'],
            message=alert['message'],
            suggested_action=alert['suggested_action'],
        )
        log(f'{"alerted" if delivered else "suppressed"}: {alert["key"]}',
            'WARN' if delivered else 'INFO')
        fired += int(bool(delivered))
    log(f'done: {fired} alert(s) fired, {len(alerts) - fired} suppressed')
    return 0


def _emit(subject: str, message: str, suggested_action: str) -> bool:
    """Append a larry-alert. Cooldown + routing are enforced inside
    larry_alerts. Never raises."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402

        return la.append_alert(
            source='heal-pulse-check-staleness',
            severity='warning',
            message=message,
            subject=subject,
            suggested_action=suggested_action,
        )
    except Exception as e:  # noqa: BLE001
        log(f'emit failed for {subject}: {type(e).__name__}: {e}', 'WARN')
        return False


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
