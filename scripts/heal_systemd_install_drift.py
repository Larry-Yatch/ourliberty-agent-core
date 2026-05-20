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
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path('/home/larry/agents')
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-systemd-install-drift.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-systemd-install-drift.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-systemd-install-drift.json'

REPO_SYSTEMD_DIR = Path(__file__).resolve().parent.parent / 'systemd'
INSTALLED_SYSTEMD_DIR = Path('/etc/systemd/system')

ENV_HEALER_ENABLED = 'OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED'

RE_DM_WINDOW = timedelta(hours=12)


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
            return {'units': {}}
        data.setdefault('units', {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'units': {}}


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _should_re_dm(
    state: dict[str, Any], unit: str,
    now: Optional[datetime] = None, window: timedelta = RE_DM_WINDOW,
) -> bool:
    now = now or datetime.now(timezone.utc)
    entry = state['units'].get(unit)
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
) -> None:
    now = now or datetime.now(timezone.utc)
    entry = state['units'].setdefault(unit, {'dm_count': 0})
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


# -------------------- DM rendering --------------------

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
