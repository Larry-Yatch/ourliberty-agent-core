#!/usr/bin/env python3
"""heal_systemd_install_drift.py — systemd install drift healer (E1.5.2).

Every 12h via systemd timer. Compares `systemd/` in the repo against
`/etc/systemd/system/` on the droplet. Any unit shipped in the repo but
not installed on the droplet is a drift — operator forgot to run the
`cp + daemon-reload + systemctl enable --now` dance after a merge.

Motivated by the E1.5 discovery that `heal-pr-auto-merge.{service,timer}`
shipped via PR #43 but were never installed — that's exactly the gap this
healer closes.

12h cadence is sufficient because install state changes only on PR merge
(not minute-by-minute). Same dry-run-by-default + activation-on-first-real-
drift pattern as the credential drift healer; same two-layer kill-switch.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-systemd-install-drift.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-systemd-install-drift.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-systemd-install-drift.json'

REPO_SYSTEMD_DIR = Path(__file__).resolve().parent.parent / 'systemd'
INSTALLED_SYSTEMD_DIR = Path('/etc/systemd/system')
ALLOWLIST_FILE = (
    Path(__file__).resolve().parent.parent
    / 'config' / 'auto-remediation-allowlist.json'
)

ENV_HEALER_ENABLED = 'OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED'

RE_DM_WINDOW = timedelta(hours=12)

SYSTEMCTL_TIMEOUT_S = 10
RESTART_TIMEOUT_S = 30


# -------------------- logging + heartbeat --------------------

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
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


def healer_enabled() -> bool:
    return os.environ.get(ENV_HEALER_ENABLED, '').strip().lower() == 'true'


# -------------------- state --------------------

def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {'units': {}, 'stuck_timers': {}}
        data.setdefault('units', {})
        data.setdefault('stuck_timers', {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'units': {}, 'stuck_timers': {}}


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _should_re_dm(
    state: dict[str, Any], unit: str,
    now: Optional[datetime] = None, window: timedelta = RE_DM_WINDOW,
    bucket: str = 'units',
) -> bool:
    now = now or datetime.now(timezone.utc)
    entry = state.setdefault(bucket, {}).get(unit)
    if not entry:
        return True
    last_iso = entry.get('last_dm_at')
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now - last) >= window


def _record_dm(
    state: dict[str, Any], unit: str, now: Optional[datetime] = None,
    bucket: str = 'units',
) -> None:
    now = now or datetime.now(timezone.utc)
    entry = state.setdefault(bucket, {}).setdefault(unit, {'dm_count': 0})
    entry['dm_count'] = entry.get('dm_count', 0) + 1
    entry['last_dm_at'] = now.isoformat()


# -------------------- DM --------------------

def dm_larry(
    message: str, subject: str, suggested_action: str,
    severity: str = 'warning',
) -> bool:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='heal-systemd-install-drift',
            severity=severity,
            message=message,
            subject=subject,
            suggested_action=suggested_action,
        )
    except Exception as e:
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- drift detection --------------------

def list_repo_units(repo_dir: Path = REPO_SYSTEMD_DIR) -> list[str]:
    """Return sorted unit filenames in the repo's systemd/ dir."""
    if not repo_dir.is_dir():
        return []
    return sorted(
        p.name for p in repo_dir.iterdir()
        if p.suffix in ('.service', '.timer') and p.is_file()
    )


def list_installed_units(
    installed_dir: Path = INSTALLED_SYSTEMD_DIR,
) -> set[str]:
    """Return set of installed unit names in /etc/systemd/system/."""
    if not installed_dir.is_dir():
        return set()
    try:
        return {
            p.name for p in installed_dir.iterdir()
            if p.suffix in ('.service', '.timer')
        }
    except OSError as e:
        log(f'list_installed_units error: {e}', 'WARN')
        return set()


def detect_drift(
    repo_dir: Path = REPO_SYSTEMD_DIR,
    installed_dir: Path = INSTALLED_SYSTEMD_DIR,
) -> list[str]:
    """Return sorted list of unit filenames shipped in repo but not installed.

    The reverse case (installed but not in repo) is intentionally ignored —
    that's a removal or a manual operator install, neither of which is a
    drift this healer should police.
    """
    repo_units = list_repo_units(repo_dir)
    installed = list_installed_units(installed_dir)
    return sorted(u for u in repo_units if u not in installed)


# -------------------- stuck-timer detection --------------------

_STUCK_TIMER_PROPS = (
    'ActiveState',
    'NextElapseUSecRealtime',
    'NextElapseUSecMonotonic',
    'LastTriggerUSec',
)


def _systemctl_show(unit: str) -> Optional[dict[str, str]]:
    """Return parsed KEY=VALUE map from `systemctl show <unit> --property=...`.

    Returns None on shell-out failure or parse error so the caller can skip
    the unit + log without raising.
    """
    try:
        result = subprocess.run(
            [
                'systemctl', 'show', unit,
                '--property=' + ','.join(_STUCK_TIMER_PROPS),
                '--no-pager',
            ],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(f'systemctl show {unit} raised {type(e).__name__}: {e}', 'INFO')
        return None
    if result.returncode != 0:
        log(
            f'systemctl show {unit} rc={result.returncode} '
            f'stderr={(result.stderr or "").strip()!r}',
            'INFO',
        )
        return None
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition('=')
        if not sep:
            continue
        props[key.strip()] = value.strip()
    return props


def detect_stuck_timers(
    installed_dir: Path = INSTALLED_SYSTEMD_DIR,
) -> list[dict[str, Optional[str]]]:
    """Return a list of `.timer` units in the infinity trap.

    Trap predicate: ActiveState=active AND NextElapseUSecRealtime is empty
    AND NextElapseUSecMonotonic=infinity. Both empty-realtime and infinity-
    monotonic are required — either alone is a normal transient state.

    Per-unit shell-out failures are logged INFO and the unit is skipped;
    the function never raises.
    """
    stuck: list[dict[str, Optional[str]]] = []
    for unit in sorted(list_installed_units(installed_dir)):
        if not unit.endswith('.timer'):
            continue
        props = _systemctl_show(unit)
        if props is None:
            continue
        if props.get('ActiveState') != 'active':
            continue
        if props.get('NextElapseUSecRealtime', '') != '':
            continue
        if props.get('NextElapseUSecMonotonic') != 'infinity':
            continue
        stuck.append({
            'unit': unit,
            'last_trigger': props.get('LastTriggerUSec') or None,
        })
    return stuck


def _heal_stuck_timer(unit: str) -> tuple[int, str]:
    """Inline daemon-reload + restart for a stuck timer.

    Mirrors `heal_stale_daemon_code.auto_restart_unit` shape but inlined
    here to avoid a cross-module import dependency between the two healers.
    Returns `(restart_rc, restart_stderr)`; a failed daemon-reload is
    logged WARN and we continue to the restart anyway.
    """
    try:
        reload_result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'daemon-reload'],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
        if reload_result.returncode != 0:
            log(
                f'daemon-reload before stuck-timer restart of {unit} failed '
                f'rc={reload_result.returncode} '
                f'stderr={(reload_result.stderr or "").strip()!r}; '
                f'proceeding with restart anyway',
                'WARN',
            )
    except subprocess.TimeoutExpired:
        log(
            f'daemon-reload before stuck-timer restart of {unit} timed out '
            f'after {RESTART_TIMEOUT_S}s; proceeding with restart anyway',
            'WARN',
        )
    except FileNotFoundError:
        log(
            f'daemon-reload before stuck-timer restart of {unit}: sudo or '
            f'systemctl not found in PATH; proceeding with restart anyway',
            'WARN',
        )

    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'restart', unit],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
        return result.returncode, (result.stderr or '').strip()
    except subprocess.TimeoutExpired:
        return -1, f'systemctl restart timed out after {RESTART_TIMEOUT_S}s'
    except FileNotFoundError:
        return -1, 'sudo or systemctl not found in PATH'


# -------------------- remediation allowlist --------------------

def _remediation_allowed(class_name: str) -> bool:
    """Return True iff `class_name` is listed in the allowlist config.

    Fail safe: missing file, JSON parse error, or unexpected shape returns
    False and WARN-logs. Never raises. This is the third gate stacked on
    top of the kill-switch file and the OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED
    env flag — all three must pass before a class takes privileged action.
    """
    try:
        raw = ALLOWLIST_FILE.read_text()
    except FileNotFoundError:
        log(
            f'auto-remediation allowlist missing at {ALLOWLIST_FILE}; '
            f'class {class_name!r} not allowed',
            'WARN',
        )
        return False
    except OSError as e:
        log(
            f'auto-remediation allowlist read error: {type(e).__name__}: {e}; '
            f'class {class_name!r} not allowed',
            'WARN',
        )
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log(
            f'auto-remediation allowlist malformed JSON: {e}; '
            f'class {class_name!r} not allowed',
            'WARN',
        )
        return False
    if not isinstance(data, dict):
        log(
            f'auto-remediation allowlist top-level not an object; '
            f'class {class_name!r} not allowed',
            'WARN',
        )
        return False
    classes = data.get('classes')
    if not isinstance(classes, list) or not all(
        isinstance(c, str) for c in classes
    ):
        log(
            f'auto-remediation allowlist "classes" missing or not a list of '
            f'strings; class {class_name!r} not allowed',
            'WARN',
        )
        return False
    return class_name in classes


# -------------------- missing-install remediation --------------------

def _remediate_missing_install(unit: str) -> tuple[int, str]:
    """cp the repo unit file into /etc/systemd/system, daemon-reload, and
    enable --now if it's a timer. Returns (rc, stderr); never raises.

    For a .timer we verify after by re-reading systemctl show and confirming
    ActiveState=active + NextElapseUSecRealtime populated; verification
    failure flips rc non-zero so the caller falls back to the manual-dance
    alert. A .service is intentionally not enabled directly — it is
    activated by its sibling timer.
    """
    src = REPO_SYSTEMD_DIR / unit
    try:
        cp_result = subprocess.run(
            ['sudo', '-n', 'cp', str(src), str(INSTALLED_SYSTEMD_DIR) + '/'],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return -1, f'cp {unit} timed out after {RESTART_TIMEOUT_S}s'
    except FileNotFoundError:
        return -1, 'sudo or cp not found in PATH'
    if cp_result.returncode != 0:
        return cp_result.returncode, (
            f'cp failed: {(cp_result.stderr or "").strip()}'
        )

    try:
        reload_result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'daemon-reload'],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return -1, f'daemon-reload after cp of {unit} timed out'
    except FileNotFoundError:
        return -1, 'sudo or systemctl not found in PATH'
    if reload_result.returncode != 0:
        return reload_result.returncode, (
            f'daemon-reload failed: {(reload_result.stderr or "").strip()}'
        )

    if unit.endswith('.timer'):
        try:
            enable_result = subprocess.run(
                ['sudo', '-n', 'systemctl', 'enable', '--now', unit],
                capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return -1, f'enable --now {unit} timed out'
        except FileNotFoundError:
            return -1, 'sudo or systemctl not found in PATH'
        if enable_result.returncode != 0:
            return enable_result.returncode, (
                f'enable --now failed: '
                f'{(enable_result.stderr or "").strip()}'
            )
        # Verify the timer actually came up.
        post = _systemctl_show(unit) or {}
        if post.get('ActiveState') != 'active':
            return -1, (
                f'post-enable verify failed: ActiveState='
                f'{post.get("ActiveState")!r}'
            )
        if not post.get('NextElapseUSecRealtime'):
            return -1, (
                'post-enable verify failed: NextElapseUSecRealtime empty'
            )

    return 0, ''


# -------------------- DM rendering --------------------

def _render_stuck_timer_heal(unit: str, next_fire: str) -> tuple[str, str, str]:
    message = (
        f'Auto-healed stuck timer `{unit}`. Trap: `NextElapseUSecRealtime` '
        f'empty + `NextElapseUSecMonotonic=infinity` (timer never fires). '
        f'Recovery: daemon-reload + restart. Next fire now: {next_fire}.'
    )
    subject = f'stuck-timer:{unit}'
    suggested = (
        f'Verify on the droplet: '
        f'`systemctl show {unit} --property=NextElapseUSecRealtime`.'
    )
    return message, subject, suggested


def _render_stuck_timer_dry_run(unit: str) -> tuple[str, str, str]:
    message = (
        f'Stuck timer detected: `{unit}`. `NextElapseUSecRealtime` empty + '
        f'`NextElapseUSecMonotonic=infinity`. Manual recovery: '
        f'`sudo systemctl daemon-reload && sudo systemctl restart {unit}`.'
    )
    subject = f'stuck-timer:{unit}'
    suggested = (
        f'On the droplet (ssh larry@134.209.44.80):\n'
        f'  sudo systemctl daemon-reload\n'
        f'  sudo systemctl restart {unit}\n'
        f'Then verify: '
        f'`systemctl show {unit} --property=NextElapseUSecRealtime`.'
    )
    return message, subject, suggested


def _render_install_healed(unit: str, next_fire: str) -> tuple[str, str, str]:
    is_timer = unit.endswith('.timer')
    enable_phrase = (
        'enabled --now' if is_timer
        else 'left to its sibling timer to activate'
    )
    message = (
        f'Auto-installed `{unit}` — it was shipped in the repo but missing '
        f'from /etc/systemd/system/. Installed, daemon-reloaded, and '
        f'{enable_phrase}. Next fire: {next_fire}.'
    )
    subject = f'install-drift:{unit}'
    suggested = f'Verify on the droplet: `systemctl status {unit}`.'
    return message, subject, suggested


def _render_missing_install(unit: str) -> tuple[str, str, str]:
    repo_path = f'~/agent-core/systemd/{unit}'
    is_timer = unit.endswith('.timer')
    enable_line = (
        f'sudo systemctl enable --now {unit}'
        if is_timer
        else f'sudo systemctl daemon-reload  # service activated by its timer'
    )
    message = (
        f'Unit `{unit}` is shipped in the repo (`systemd/{unit}`) but is not '
        f'installed under `/etc/systemd/system/`. Likely cause: the PR that '
        f'added it merged without the operator running the install dance. '
        f'Without install, the timer never fires.'
    )
    subject = f'install-drift:{unit}'
    suggested = (
        f'On the droplet (ssh larry@134.209.44.80):\n'
        f'  sudo cp {repo_path} /etc/systemd/system/\n'
        f'  sudo systemctl daemon-reload\n'
        f'  {enable_line}\n'
        f'Then verify: `systemctl status {unit}`'
    )
    return message, subject, suggested


def _activation_message() -> tuple[str, str, str]:
    message = (
        f'Heal-systemd-install-drift is in dry-run mode '
        f'(`{ENV_HEALER_ENABLED}` is not set). It detected at least one '
        f'systemd unit shipped in the repo but not installed on the droplet, '
        f'but did not send per-unit DMs.'
    )
    subject = 'install-drift-healer: activate to receive missing-install alerts'
    suggested = (
        f'On the droplet (ssh larry@134.209.44.80): '
        f'`sudo systemctl edit ourliberty-heal-systemd-install-drift.service`, '
        f'add `Environment="{ENV_HEALER_ENABLED}=true"` under `[Service]`, '
        f'then `sudo systemctl restart '
        f'ourliberty-heal-systemd-install-drift.timer`.'
    )
    return message, subject, suggested


# -------------------- main --------------------

def run_once(
    repo_dir: Optional[Path] = None,
    installed_dir: Optional[Path] = None,
    state: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    dry_run_override: Optional[bool] = None,
) -> dict[str, int]:
    """Single-tick orchestration."""
    counts = {
        'missing_install': 0, 'dm_sent': 0,
        'dm_suppressed_dedup': 0, 'reconciled_gc': 0,
        'stuck_timer': 0, 'timer_healed': 0,
        'install_healed': 0,
    }
    if kill_switch_active():
        log(f'KILL_SWITCH active at {KILL_SWITCH}; exiting cleanly')
        return counts
    heartbeat()

    dry_run = (
        dry_run_override
        if dry_run_override is not None
        else not healer_enabled()
    )

    state_was_none = state is None
    if state is None:
        state = load_state()

    drifts = detect_drift(
        repo_dir or REPO_SYSTEMD_DIR,
        installed_dir or INSTALLED_SYSTEMD_DIR,
    )
    counts['missing_install'] = len(drifts)
    live_set = set(drifts)

    for unit in drifts:
        if not _should_re_dm(state, unit, now=now):
            counts['dm_suppressed_dedup'] += 1
            continue

        if dry_run:
            state.setdefault('_meta', {})
            if not state['_meta'].get('activation_alerted'):
                a_msg, a_subj, a_sug = _activation_message()
                dm_larry(message=a_msg, subject=a_subj, suggested_action=a_sug)
                state['_meta']['activation_alerted'] = True
                counts['dm_sent'] += 1
            log(f'DRY-RUN install drift: {unit} (suppressed; activate via '
                f'{ENV_HEALER_ENABLED}=true)')
            continue

        if _remediation_allowed('install-drift'):
            rc, stderr = _remediate_missing_install(unit)
            if rc == 0:
                post = _systemctl_show(unit) or {}
                next_fire = post.get('NextElapseUSecRealtime') or 'unknown'
                counts['install_healed'] += 1
                msg, subj, sug = _render_install_healed(unit, next_fire)
                ok = dm_larry(message=msg, subject=subj, suggested_action=sug)
                if ok:
                    counts['dm_sent'] += 1
                    _record_dm(state, unit, now=now)
                    log(f'auto-installed {unit} next_fire={next_fire!r}')
                else:
                    log(f'DM append suppressed for healed install {unit}', 'WARN')
                continue
            log(
                f'auto-install of {unit} failed rc={rc} stderr={stderr!r}; '
                f'falling back to manual-dance alert',
                'WARN',
            )

        msg, subj, sug = _render_missing_install(unit)
        ok = dm_larry(message=msg, subject=subj, suggested_action=sug)
        if ok:
            counts['dm_sent'] += 1
            _record_dm(state, unit, now=now)
            log(f'DM sent for missing install: {unit}')
        else:
            log(f'DM append suppressed for {unit}', 'WARN')

    # GC entries that have been reconciled.
    for gone in list(state['units'].keys()):
        if gone not in live_set:
            state['units'].pop(gone, None)
            counts['reconciled_gc'] += 1

    # Stuck-timer pass — independent of the missing-install loop. Cooldown
    # via the same _should_re_dm helper, but in a separate state bucket so
    # the two surfaces don't collide on a unit name.
    stuck = detect_stuck_timers(installed_dir or INSTALLED_SYSTEMD_DIR)
    counts['stuck_timer'] = len(stuck)
    stuck_live = set()
    for entry in stuck:
        unit = entry['unit']
        if not isinstance(unit, str):
            continue
        stuck_live.add(unit)
        if not _should_re_dm(state, unit, now=now, bucket='stuck_timers'):
            counts['dm_suppressed_dedup'] += 1
            continue

        if dry_run:
            msg, subj, sug = _render_stuck_timer_dry_run(unit)
            ok = dm_larry(message=msg, subject=subj, suggested_action=sug)
            if ok:
                counts['dm_sent'] += 1
                _record_dm(state, unit, now=now, bucket='stuck_timers')
                log(f'DRY-RUN stuck timer: {unit} (DM sent; no restart attempted)')
            else:
                log(f'DM append suppressed for stuck timer {unit}', 'WARN')
            continue

        rc, stderr = _heal_stuck_timer(unit)
        if rc != 0:
            log(
                f'stuck-timer restart of {unit} failed rc={rc} '
                f'stderr={stderr!r}',
                'WARN',
            )
            continue
        post = _systemctl_show(unit) or {}
        next_fire = post.get('NextElapseUSecRealtime') or 'unknown'
        counts['timer_healed'] += 1
        msg, subj, sug = _render_stuck_timer_heal(unit, next_fire)
        ok = dm_larry(message=msg, subject=subj, suggested_action=sug)
        if ok:
            counts['dm_sent'] += 1
            _record_dm(state, unit, now=now, bucket='stuck_timers')
            log(f'auto-healed stuck timer: {unit} next_fire={next_fire!r}')
        else:
            log(f'DM append suppressed for healed timer {unit}', 'WARN')

    # GC reconciled stuck-timer entries.
    for gone in list(state.get('stuck_timers', {}).keys()):
        if gone not in stuck_live:
            state['stuck_timers'].pop(gone, None)
            counts['reconciled_gc'] += 1

    log(f'tick: dry_run={dry_run} '
        + ' '.join(f'{k}={v}' for k, v in counts.items() if v))

    if state_was_none:
        save_state(state)
    return counts


def main() -> int:
    try:
        run_once()
        return 0
    except Exception as e:  # noqa: BLE001
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        return 1


if __name__ == '__main__':
    sys.exit(main())
