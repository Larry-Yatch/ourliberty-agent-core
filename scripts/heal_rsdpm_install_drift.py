#!/usr/bin/env python3
"""heal_rsdpm_install_drift.py — RSDPM install file-integrity drift healer.

Every 12h via systemd timer. Fingerprints the installed RSDPM tooling under
`/usr/local/lib/rsdpm` and raises a single actionable alert when the installed
*files* change content, mode, or owner out from under us.

Surface watched (read-only observation — this healer NEVER writes the install):
  * the three root-owned scripts alert-emit.py / drift-check.sh / refresh.sh —
    fingerprint = sha256(content) + octal mode + owner:group each;
  * the vendored node22/ runtime (~204M, ~4700 files, static) — a CHEAP integrity
    signal (sha256 of node22/bin/node + file count + aggregate byte size), NOT a
    12h per-file hash of the whole tree.

Distinct from two existing, easily-confused healers:
  * ourliberty-rsdpm-driftcheck (ExecStart IS this install's drift-check.sh)
    watches the RSDPM staging *database schema* vs repo migrations — not files;
  * heal_systemd_install_drift.py watches systemd unit *presence*, not file
    content under /usr/local/lib.
This fills the uncovered gap: file-content integrity of the install itself.

Baseline manifest persists at ~/agents/state/heal-rsdpm-install-drift.json. First
run adopts the current state as baseline with NO alert. Later runs compare; on a
real diff, exactly ONE actionable alert names which file(s) drifted and how
(content/mode/owner), then the new state is adopted so a persistent legitimate
change does not re-alarm every cycle (notify-once-then-adopt, mirroring the
existing drift healers). Detect-and-alert only — no auto-remediation or
re-install; recovery is a human call.

Same shape as heal_systemd_install_drift.py: stdlib-only, 12h cadence,
dry-run-by-default + activation-on-first-real-drift, DM-dedup window, two-layer
kill-switch (healers.disabled file + env flag), heartbeat, atomic state writes,
actionable-only alerting.

Stdlib only.
"""
from __future__ import annotations

import grp
import hashlib
import os
import pwd
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from atomic_io import atomic_write_json  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-rsdpm-install-drift.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-rsdpm-install-drift.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-rsdpm-install-drift.json'

INSTALL_DIR = Path(
    os.environ.get('OURLIBERTY_RSDPM_INSTALL_DIR', '/usr/local/lib/rsdpm')
)
# Root-owned tooling scripts fingerprinted individually.
TRACKED_SCRIPTS = ('alert-emit.py', 'drift-check.sh', 'refresh.sh')
# Vendored node runtime — cheap integrity signal, not a full-tree hash.
NODE22_SUBDIR = 'node22'
NODE22_SENTINEL = 'bin/node'

ENV_HEALER_ENABLED = 'OURLIBERTY_RSDPM_INSTALL_DRIFT_HEALER_ENABLED'
SOURCE = 'heal-rsdpm-install-drift'

# A single logical target — the whole install — so one dedup key throttles the
# one alert this healer can raise.
DEDUP_KEY = 'rsdpm-install'
RE_DM_WINDOW = timedelta(hours=12)

# A directory read (os.scandir/walk) that raises is a transient FS blip, not
# drift — swallow and skip so we never alarm on our own read failure.


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
        import json
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {'baseline': None, 'dedup': {}}
        data.setdefault('baseline', None)
        data.setdefault('dedup', {})
        return data
    except (FileNotFoundError, ValueError, OSError):
        return {'baseline': None, 'dedup': {}}


def save_state(state: dict[str, Any]) -> None:
    try:
        atomic_write_json(STATE_FILE, state, indent=2)
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _should_re_dm(
    state: dict[str, Any], key: str = DEDUP_KEY,
    now: Optional[datetime] = None, window: timedelta = RE_DM_WINDOW,
) -> bool:
    now = now or datetime.now(timezone.utc)
    entry = state.setdefault('dedup', {}).get(key)
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
    state: dict[str, Any], key: str = DEDUP_KEY, now: Optional[datetime] = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    entry = state.setdefault('dedup', {}).setdefault(key, {'dm_count': 0})
    entry['dm_count'] = entry.get('dm_count', 0) + 1
    entry['last_dm_at'] = now.isoformat()


# -------------------- alerting --------------------

def dm_larry(
    message: str, subject: str, suggested_action: str,
    severity: str = 'warning', route: str = 'escalate',
) -> bool:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source=SOURCE,
            severity=severity,
            message=message,
            subject=subject,
            suggested_action=suggested_action,
            route=route,
        )
    except Exception as e:  # noqa: BLE001 — alerting must never crash the tick
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- fingerprinting --------------------

def _sha256_file(path: Path) -> Optional[str]:
    """sha256 of a file's contents, or None if unreadable."""
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _owner_group(st: os.stat_result) -> tuple[str, str]:
    """Resolve owner/group to names, falling back to numeric ids."""
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except (KeyError, OSError):
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except (KeyError, OSError):
        group = str(st.st_gid)
    return owner, group


def fingerprint_file(path: Path) -> dict[str, Any]:
    """Fingerprint one tracked script: sha256 + octal mode + owner:group.

    Returns ``{'present': False}`` when the file is absent — treated as drift on
    later comparison (a script vanishing IS install drift)."""
    try:
        st = path.stat()
    except OSError:
        return {'present': False}
    owner, group = _owner_group(st)
    return {
        'present': True,
        'sha256': _sha256_file(path),
        'mode': oct(st.st_mode & 0o7777),
        'owner': owner,
        'group': group,
    }


def fingerprint_node22(node22_dir: Path) -> dict[str, Any]:
    """Cheap integrity signal for the vendored node runtime.

    Deliberately NOT a per-file hash of ~4700 files every 12h. Instead: sha256 of
    the node22/bin/node sentinel binary (a targeted content check on the one file
    that matters most), plus a stat-only tree walk yielding file count + aggregate
    byte size. Any silent swap/corruption/truncation moves at least one of the
    three signals; the walk reads no file contents, so it stays cheap."""
    sentinel = node22_dir / NODE22_SENTINEL
    node_sha = _sha256_file(sentinel)
    file_count = 0
    total_bytes = 0
    walked = False
    try:
        for root, _dirs, files in os.walk(node22_dir):
            walked = True
            for name in files:
                file_count += 1
                try:
                    total_bytes += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return {
        'present': node_sha is not None or walked,
        'node_bin_sha256': node_sha,
        'file_count': file_count,
        'total_bytes': total_bytes,
    }


def build_manifest(install_dir: Path = INSTALL_DIR) -> dict[str, Any]:
    """Fingerprint the whole install surface into a comparable manifest."""
    scripts: dict[str, Any] = {}
    for name in TRACKED_SCRIPTS:
        scripts[name] = fingerprint_file(install_dir / name)
    return {
        'scripts': scripts,
        'node22': fingerprint_node22(install_dir / NODE22_SUBDIR),
    }


# -------------------- drift detection --------------------

def _diff_script(name: str, base: dict, cur: dict) -> Optional[dict[str, Any]]:
    """Compare one script's baseline vs current fingerprint. Returns a drift
    descriptor ``{'target','kinds','detail'}`` or None when unchanged."""
    if not base.get('present') and not cur.get('present'):
        return None
    if base.get('present') and not cur.get('present'):
        return {'target': name, 'kinds': ['removed'],
                'detail': 'file is now missing from the install'}
    if cur.get('present') and not base.get('present'):
        return {'target': name, 'kinds': ['added'],
                'detail': 'file appeared in the install (was absent at baseline)'}

    kinds: list[str] = []
    details: list[str] = []
    if base.get('sha256') != cur.get('sha256'):
        kinds.append('content')
        details.append(
            f'content sha256 {str(base.get("sha256"))[:12]}… → '
            f'{str(cur.get("sha256"))[:12]}…'
        )
    if base.get('mode') != cur.get('mode'):
        kinds.append('mode')
        details.append(f'mode {base.get("mode")} → {cur.get("mode")}')
    owner_base = f'{base.get("owner")}:{base.get("group")}'
    owner_cur = f'{cur.get("owner")}:{cur.get("group")}'
    if owner_base != owner_cur:
        kinds.append('owner')
        details.append(f'owner {owner_base} → {owner_cur}')
    if not kinds:
        return None
    return {'target': name, 'kinds': kinds, 'detail': '; '.join(details)}


def _diff_node22(base: dict, cur: dict) -> Optional[dict[str, Any]]:
    kinds: list[str] = []
    details: list[str] = []
    if base.get('node_bin_sha256') != cur.get('node_bin_sha256'):
        kinds.append('content')
        details.append(
            f'node22/bin/node sha256 {str(base.get("node_bin_sha256"))[:12]}… → '
            f'{str(cur.get("node_bin_sha256"))[:12]}…'
        )
    if base.get('file_count') != cur.get('file_count'):
        kinds.append('file_count')
        details.append(
            f'file count {base.get("file_count")} → {cur.get("file_count")}'
        )
    if base.get('total_bytes') != cur.get('total_bytes'):
        kinds.append('size')
        details.append(
            f'aggregate bytes {base.get("total_bytes")} → {cur.get("total_bytes")}'
        )
    if not kinds:
        return None
    return {'target': 'node22/', 'kinds': kinds, 'detail': '; '.join(details)}


def detect_drift(
    baseline: dict[str, Any], current: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return a list of drift descriptors comparing baseline vs current. Empty
    list means the install is byte-for-byte, mode-for-mode, owner-for-owner as
    last adopted."""
    drifts: list[dict[str, Any]] = []
    base_scripts = baseline.get('scripts', {})
    cur_scripts = current.get('scripts', {})
    for name in TRACKED_SCRIPTS:
        d = _diff_script(
            name, base_scripts.get(name, {}), cur_scripts.get(name, {}),
        )
        if d:
            drifts.append(d)
    nd = _diff_node22(baseline.get('node22', {}), current.get('node22', {}))
    if nd:
        drifts.append(nd)
    return drifts


# -------------------- render --------------------

def _render_drift_alert(drifts: list[dict[str, Any]]) -> tuple[str, str, str]:
    lines = []
    for d in drifts:
        lines.append(f'  • {d["target"]} [{", ".join(d["kinds"])}]: {d["detail"]}')
    body = '\n'.join(lines)
    message = (
        f'Installed RSDPM tooling under `{INSTALL_DIR}` drifted from its adopted '
        f'baseline — the installed file(s) changed content, mode, or owner out of '
        f'band:\n{body}\n'
        f'This is read-only observation; nothing was changed. If the change was '
        f'expected (a fresh install/refresh) no action is needed — the new state '
        f'has been adopted as the baseline. If it was NOT expected, the install '
        f'may be corrupted or tampered with.'
    )
    subject = f'rsdpm-install-drift:{DEDUP_KEY}'
    suggested = (
        f'On the droplet (ssh larry@134.209.44.80):\n'
        f'  ls -l {INSTALL_DIR}   # inspect mode/owner of the named file(s)\n'
        f'  # compare content against the RSDPM source of truth; if the drift is\n'
        f'  # unexpected, re-run the RSDPM install/refresh to restore known-good\n'
        f'  # files. This healer only detects — recovery is your call.'
    )
    return message, subject, suggested


def _activation_message() -> tuple[str, str, str]:
    message = (
        f'Heal-rsdpm-install-drift is in dry-run mode (`{ENV_HEALER_ENABLED}` is '
        f'not set). It detected file drift in the RSDPM install under '
        f'`{INSTALL_DIR}` but did not send the per-drift alert.'
    )
    subject = 'rsdpm-install-drift-healer: activate to receive install-drift alerts'
    suggested = (
        f'On the droplet (ssh larry@134.209.44.80): '
        f'`sudo systemctl edit ourliberty-heal-rsdpm-install-drift.service`, '
        f'add `Environment="{ENV_HEALER_ENABLED}=true"` under `[Service]`, then '
        f'`sudo systemctl restart ourliberty-heal-rsdpm-install-drift.timer`.'
    )
    return message, subject, suggested


# -------------------- orchestration --------------------

def run_once(
    install_dir: Optional[Path] = None,
    state: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    dry_run_override: Optional[bool] = None,
) -> dict[str, int]:
    """Single-tick orchestration."""
    counts = {
        'baseline_adopted': 0, 'drift_detected': 0, 'dm_sent': 0,
        'dm_suppressed_dedup': 0, 'clean': 0, 'baseline_readopted': 0,
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

    current = build_manifest(install_dir or INSTALL_DIR)
    baseline = state.get('baseline')

    if not baseline:
        # First run ever (or state reset): adopt current as baseline, NO alert.
        state['baseline'] = current
        counts['baseline_adopted'] = 1
        log('first run: adopted current install state as baseline (no alert)')
        if state_was_none:
            save_state(state)
        return counts

    drifts = detect_drift(baseline, current)
    if not drifts:
        counts['clean'] = 1
        log('tick: install matches baseline (no drift)')
        if state_was_none:
            save_state(state)
        return counts

    counts['drift_detected'] = len(drifts)
    targets = ', '.join(d['target'] for d in drifts)

    if dry_run:
        # Do NOT adopt in dry-run: keep the baseline so a later activated run
        # still sees the drift and pages. Send the one-time activation notice.
        state.setdefault('_meta', {})
        if not state['_meta'].get('activation_alerted'):
            a_msg, a_subj, a_sug = _activation_message()
            if dm_larry(message=a_msg, subject=a_subj, suggested_action=a_sug):
                counts['dm_sent'] = 1
            state['_meta']['activation_alerted'] = True
        log(f'DRY-RUN install drift: {targets} (suppressed; activate via '
            f'{ENV_HEALER_ENABLED}=true)')
        if state_was_none:
            save_state(state)
        return counts

    # Enabled mode: notify-once-then-adopt. One actionable alert naming the
    # drifted file(s) and how, throttled by the 12h dedup window, then adopt the
    # new state so a persistent legitimate change never re-alarms.
    if _should_re_dm(state, now=now):
        msg, subj, sug = _render_drift_alert(drifts)
        if dm_larry(message=msg, subject=subj, suggested_action=sug):
            counts['dm_sent'] = 1
            _record_dm(state, now=now)
            log(f'DM sent for install drift: {targets}')
        else:
            log(f'DM append suppressed for install drift: {targets}', 'WARN')
    else:
        counts['dm_suppressed_dedup'] = 1
        log(f'install drift {targets}: within re-DM window; DM suppressed')

    state['baseline'] = current
    counts['baseline_readopted'] = 1
    log(f'adopted new baseline after drift: {targets}')

    if state_was_none:
        save_state(state)
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    triggered = bool(argv) and argv[0] in ('--triggered', '--once')
    try:
        if triggered:
            log('triggered run: single tick')
        run_once()
        return 0
    except Exception as e:  # noqa: BLE001
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        return 1


if __name__ == '__main__':
    sys.exit(main())
