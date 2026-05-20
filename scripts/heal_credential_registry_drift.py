#!/usr/bin/env python3
"""heal_credential_registry_drift.py — credential registry drift healer (E1.5.2).

Every 6h via systemd timer. Reconciles the registry at
`config/token-rotation-schedule.json` against the actual contents of each
known storage location:

  - MISSING_REGISTRY_ENTRY: a credential is present in the store but has no
    matching `name` field in the registry. Means someone (human or agent)
    added a credential without updating the registry — the 4-artifact
    discipline was violated.
  - MISSING_CREDENTIAL: a registry entry exists but the credential is not
    present in its store. Means the credential was deleted, file was edited
    by mistake, or the registry entry was added before the credential
    actually landed.

Per E1.5 design (Larry's Q2): fail-closed. DM every 6h until reconciled.
Annoying by design — the alternative is silent rotation lapse a year later.

Adapted from heal_pr_auto_merge.py (E1.3) for kill-switches, dry-run
defaults, activation-on-first-real-drift, state-file dedup, and DM body
shape.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-credential-registry-drift.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-credential-registry-drift.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-credential-registry-drift.json'

REGISTRY_PATH = Path(__file__).resolve().parent.parent / 'config' / 'token-rotation-schedule.json'

ENV_HEALER_ENABLED = 'OURLIBERTY_CREDENTIALS_HEALER_ENABLED'

# 6h cadence per E1.5 design. Re-DM the same drift only once per 6h.
RE_DM_WINDOW = timedelta(hours=6)

# gh CLI scope-line shape.
_GH_SCOPES_RE = re.compile(r"Token scopes:\s*(.+)$", re.MULTILINE)

GH_TIMEOUT_S = 15


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


# -------------------- kill-switch + activation --------------------

def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


def healer_enabled() -> bool:
    """True iff OURLIBERTY_CREDENTIALS_HEALER_ENABLED=true (case-insensitive)."""
    return os.environ.get(ENV_HEALER_ENABLED, '').strip().lower() == 'true'


# -------------------- state file (per-drift dedup) --------------------

def load_state() -> dict[str, Any]:
    """Return {'drifts': {drift_key: {'dm_count': N, 'last_dm_at': iso,
    'activation_alerted': bool}}}. drift_key = '<name>:<kind>'."""
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {'drifts': {}}
        data.setdefault('drifts', {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'drifts': {}}


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _drift_key(name: str, kind: str) -> str:
    return f'{name}:{kind}'


def _should_re_dm(
    state: dict[str, Any],
    name: str,
    kind: str,
    now: Optional[datetime] = None,
    window: timedelta = RE_DM_WINDOW,
) -> bool:
    """True iff this drift has never been DMed OR last DM was > window ago."""
    now = now or datetime.now(timezone.utc)
    entry = state['drifts'].get(_drift_key(name, kind))
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
    state: dict[str, Any],
    name: str,
    kind: str,
    now: Optional[datetime] = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    key = _drift_key(name, kind)
    entry = state['drifts'].setdefault(key, {'dm_count': 0})
    entry['dm_count'] = entry.get('dm_count', 0) + 1
    entry['last_dm_at'] = now.isoformat()


def _reconciled_keys(
    state: dict[str, Any], live_keys: set[str],
) -> list[str]:
    """Return drift keys present in state but not in the current live set —
    these can be garbage-collected since the drift has been reconciled."""
    return [k for k in list(state['drifts'].keys()) if k not in live_keys]


# -------------------- DM --------------------

def dm_larry(
    message: str, subject: str, suggested_action: str,
    severity: str = 'warning',
) -> bool:
    """Send a DM via larry_alerts. Returns True on append, False on
    cooldown/error. Never raises."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='heal-credential-registry-drift',
            severity=severity,
            message=message,
            subject=subject,
            suggested_action=suggested_action,
        )
    except Exception as e:
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- registry loader --------------------

def load_registry(path: Path = REGISTRY_PATH) -> Optional[dict[str, Any]]:
    """Return parsed registry dict or None on read/parse error."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log(f'load_registry failed for {path}: {type(e).__name__}: {e}', 'WARN')
        return None


# -------------------- scanners --------------------

def scan_env_file(env_path: Path) -> set[str]:
    """Return the set of KEY names with non-empty values in an env file.

    Per registry's env_file scanner_strategy: parse KEY=value lines; ignore
    comments and blank lines; non-empty value = present.
    """
    if not env_path.exists():
        return set()
    names: set[str] = set()
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            # Strip surrounding quotes for emptiness check.
            unquoted = value.strip("'").strip('"').strip()
            if unquoted:
                names.add(key)
    except OSError as e:
        log(f'scan_env_file read error on {env_path}: {e}', 'WARN')
    return names


def scan_gh_cli(host_path: Path) -> set[str]:
    """Return {'GITHUB_GH_OAUTH_TOKEN'} iff `gh auth status` reports an
    authenticated host with non-empty scopes; else empty set.

    The actual token-bearer name is a convention — what we check is
    "gh CLI has an active credential for the host."
    """
    if not host_path.exists():
        return set()
    env = {**os.environ, 'PATH': '/usr/bin:/usr/local/bin:/snap/bin'}
    try:
        result = subprocess.run(
            ['gh', 'auth', 'status'],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S, env=env,
        )
        # `gh auth status` writes the human-readable summary to stderr.
        out = (result.stderr or '') + (result.stdout or '')
        if 'Logged in' in out or _GH_SCOPES_RE.search(out):
            return {'GITHUB_GH_OAUTH_TOKEN'}
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f'scan_gh_cli: gh auth status failed: {type(e).__name__}: {e}',
            'WARN')
    return set()


def scan_claude_cli(creds_path: Path) -> set[str]:
    """Return {'CLAUDE_MAX_OAUTH'} iff the Claude credentials JSON has a
    non-empty access token; else empty set."""
    if not creds_path.exists():
        return set()
    try:
        data = json.loads(creds_path.read_text())
        oauth = data.get('claudeAiOauth') if isinstance(data, dict) else None
        if isinstance(oauth, dict):
            token = oauth.get('accessToken')
            if isinstance(token, str) and token:
                return {'CLAUDE_MAX_OAUTH'}
    except (json.JSONDecodeError, OSError) as e:
        log(f'scan_claude_cli read error on {creds_path}: {e}', 'WARN')
    return set()


def scan_workspace_mcp(creds_dir: Path) -> set[str]:
    """Return {'GOOGLE_OAUTH_REFRESH_TOKEN'} iff the workspace-mcp
    credentials dir contains at least one non-empty .json file."""
    if not creds_dir.exists() or not creds_dir.is_dir():
        return set()
    try:
        for p in creds_dir.iterdir():
            if p.suffix == '.json' and p.is_file() and p.stat().st_size > 0:
                return {'GOOGLE_OAUTH_REFRESH_TOKEN'}
    except OSError as e:
        log(f'scan_workspace_mcp read error on {creds_dir}: {e}', 'WARN')
    return set()


def _scanner_for(prefix: str):
    if prefix.startswith('env_file:'):
        return scan_env_file
    if prefix.startswith('gh_cli:'):
        return scan_gh_cli
    if prefix.startswith('claude_cli:'):
        return scan_claude_cli
    if prefix.startswith('workspace_mcp:'):
        return scan_workspace_mcp
    return None


def _path_from_location(location: str) -> Path:
    """Strip the `<type>:` prefix and return a Path."""
    _, _, p = location.partition(':')
    return Path(p)


# -------------------- drift detection --------------------

def detect_drift(
    registry: dict[str, Any],
    scan_overrides: Optional[dict[str, set[str]]] = None,
) -> tuple[list[tuple[str, str, dict[str, Any]]], set[str]]:
    """Compare registry against live scans of each known storage location.

    Returns (drifts, live_keys) where:
      - drifts is a list of (name, kind, registry_entry_or_synthetic) tuples;
        kind is 'MISSING_REGISTRY_ENTRY' or 'MISSING_CREDENTIAL'.
      - live_keys is the set of `<name>:<kind>` strings currently in drift
        (used by the caller to GC reconciled state entries).

    `scan_overrides` is a test hook — when set, maps storage_location string
    to the set of names that scanner "would have" returned, bypassing
    real-world scans.
    """
    known_locations = registry.get('known_storage_locations') or {}
    creds = registry.get('credentials') or []

    # Build registry-known names per storage location.
    by_location: dict[str, set[str]] = {loc: set() for loc in known_locations}
    for entry in creds:
        loc = entry.get('storage_location')
        if not loc:
            continue
        # Match to the most specific known prefix.
        matched: Optional[str] = None
        if loc in by_location:
            matched = loc
        else:
            for known in known_locations:
                if known.startswith('workspace_mcp:') and loc.startswith(known):
                    matched = known
                    break
                if loc.startswith(known.rstrip('/') + '/'):
                    matched = known
                    break
        if matched is None:
            log(f'skipping entry {entry.get("name")!r}: storage_location '
                f'{loc!r} not in known_storage_locations', 'WARN')
            continue
        by_location.setdefault(matched, set()).add(entry['name'])

    drifts: list[tuple[str, str, dict[str, Any]]] = []
    live_keys: set[str] = set()

    for location, registered in by_location.items():
        if scan_overrides is not None:
            # Test mode: any location absent from the override is treated as
            # "scanner returned empty set" so tests don't pick up real
            # droplet state via unmocked scanner paths.
            live = scan_overrides.get(location, set())
        else:
            scanner = _scanner_for(location)
            if scanner is None:
                continue
            try:
                live = scanner(_path_from_location(location))
            except Exception as e:  # noqa: BLE001 — defensive
                log(f'scanner for {location} raised {type(e).__name__}: {e}; '
                    f'skipping', 'WARN')
                continue

        # Special case: gh_cli, claude_cli, workspace_mcp scanners return
        # generic credential names ('GITHUB_GH_OAUTH_TOKEN', etc.) — only
        # treat as MISSING_REGISTRY_ENTRY if those names aren't in the
        # registry at all. The env_file scanner returns actual key names.

        missing_in_registry = live - registered
        missing_in_store = registered - live

        for name in missing_in_registry:
            drifts.append((name, 'MISSING_REGISTRY_ENTRY', {
                'storage_location': location,
            }))
            live_keys.add(_drift_key(name, 'MISSING_REGISTRY_ENTRY'))

        for name in missing_in_store:
            entry = next(
                (e for e in creds if e.get('name') == name), {'name': name},
            )
            drifts.append((name, 'MISSING_CREDENTIAL', entry))
            live_keys.add(_drift_key(name, 'MISSING_CREDENTIAL'))

    return drifts, live_keys


# -------------------- DM rendering --------------------

def _render_missing_registry_entry(name: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    """Build (message, subject, suggested_action) for a MISSING_REGISTRY_ENTRY drift."""
    loc = payload.get('storage_location', '?')
    message = (
        f'Credential `{name}` is present in `{loc}` but has no entry in '
        f'`config/token-rotation-schedule.json`. The 4-artifact discipline '
        f'(credential + registry entry + runbook + calendar event) is '
        f'violated — without a registry entry, the rotation reminder '
        f'silently lapses.'
    )
    subject = f'credential-drift:MISSING_REGISTRY_ENTRY:{name}'
    suggested = (
        f'Add a registry entry for `{name}` per `shared/credentials-discipline.md`. '
        f'Required fields: name, storage_location, credential_type, purpose, '
        f'rotation_type, cadence_days, created_at, last_rotated_at, '
        f'next_rotation_due, calendar_event_url, runbook_path, '
        f'severity_if_lapsed, owner_role, scopes, notes. Then create the '
        f'matching runbook under `docs/runbooks/rotate-<name>.md` and '
        f'(if scheduled / scope_audit) the calendar event.'
    )
    return message, subject, suggested


def _render_missing_credential(name: str, entry: dict[str, Any]) -> tuple[str, str, str]:
    """Build (message, subject, suggested_action) for a MISSING_CREDENTIAL drift."""
    loc = entry.get('storage_location', '?')
    runbook = entry.get('runbook_path', '(no runbook in entry)')
    severity = entry.get('severity_if_lapsed', '?')
    message = (
        f'Registry entry for `{name}` exists but the credential is not '
        f'present in its store (`{loc}`). Either the credential was rotated '
        f'out by mistake, never landed, or the store path changed. '
        f'Severity-if-lapsed: {severity}.'
    )
    subject = f'credential-drift:MISSING_CREDENTIAL:{name}'
    suggested = (
        f'Either (a) install the credential at `{loc}` per `{runbook}`, or '
        f'(b) if the credential has been intentionally retired, remove the '
        f'entry from `config/token-rotation-schedule.json` in the same PR '
        f'that retired the credential.'
    )
    return message, subject, suggested


def _activation_message() -> tuple[str, str, str]:
    """One-time activation prompt — emitted when the healer is in dry-run
    AND has at least one real drift to report. Pattern matches
    heal_pr_auto_merge's E1.3 activation."""
    message = (
        f'Heal-credential-registry-drift is in dry-run mode '
        f'(`{ENV_HEALER_ENABLED}` is not set). It detected at least one '
        f'real credential drift but did not send the per-drift DM. To '
        f'activate the healer:'
    )
    subject = 'credential-drift-healer: activate to receive drift alerts'
    suggested = (
        f'On the droplet (ssh larry@134.209.44.80): '
        f'`sudo systemctl edit ourliberty-heal-credential-registry-drift.service`, '
        f'add `Environment="{ENV_HEALER_ENABLED}=true"` under `[Service]`, '
        f'then `sudo systemctl restart '
        f'ourliberty-heal-credential-registry-drift.timer`. The next tick '
        f'(within 6h) will DM each detected drift with reconciliation '
        f'instructions.'
    )
    return message, subject, suggested


# -------------------- main --------------------

def run_once(
    registry: Optional[dict[str, Any]] = None,
    state: Optional[dict[str, Any]] = None,
    scan_overrides: Optional[dict[str, set[str]]] = None,
    now: Optional[datetime] = None,
    dry_run_override: Optional[bool] = None,
) -> dict[str, int]:
    """Single-tick orchestration. Returns counts dict (also for tests).

    Args:
        registry: pre-loaded registry; None to load from disk.
        state: pre-loaded state; None to load from disk (with save on exit).
        scan_overrides: test hook — bypass live scanners for specific
            storage_locations.
        now: anchor for re-DM window dedup.
        dry_run_override: when set, overrides env-var detection.
    """
    counts = {
        'missing_registry_entry': 0,
        'missing_credential': 0,
        'dm_sent': 0,
        'dm_suppressed_dedup': 0,
        'reconciled_gc': 0,
    }
    if kill_switch_active():
        log(f'KILL_SWITCH active at {KILL_SWITCH}; exiting cleanly')
        return counts
    heartbeat()

    if registry is None:
        registry = load_registry()
        if registry is None:
            return counts

    dry_run = (
        dry_run_override
        if dry_run_override is not None
        else not healer_enabled()
    )

    state_was_none = state is None
    if state is None:
        state = load_state()

    drifts, live_keys = detect_drift(registry, scan_overrides=scan_overrides)

    for name, kind, payload in drifts:
        if kind == 'MISSING_REGISTRY_ENTRY':
            counts['missing_registry_entry'] += 1
            msg, subj, sug = _render_missing_registry_entry(name, payload)
        else:
            counts['missing_credential'] += 1
            msg, subj, sug = _render_missing_credential(name, payload)

        if not _should_re_dm(state, name, kind, now=now):
            counts['dm_suppressed_dedup'] += 1
            continue

        if dry_run:
            # Activation DM at most once total across drifts, then suppress
            # the per-drift DMs.
            state.setdefault('_meta', {})
            if not state['_meta'].get('activation_alerted'):
                a_msg, a_subj, a_sug = _activation_message()
                dm_larry(message=a_msg, subject=a_subj, suggested_action=a_sug)
                state['_meta']['activation_alerted'] = True
                counts['dm_sent'] += 1
            log(f'DRY-RUN drift: {kind} {name} (suppressed; activate via '
                f'{ENV_HEALER_ENABLED}=true)')
            continue

        ok = dm_larry(message=msg, subject=subj, suggested_action=sug,
                      severity='warning')
        if ok:
            counts['dm_sent'] += 1
            _record_dm(state, name, kind, now=now)
            log(f'DM sent for {kind} {name}')
        else:
            log(f'DM append suppressed (cooldown or write error) for '
                f'{kind} {name}', 'WARN')

    for gone_key in _reconciled_keys(state, live_keys):
        if gone_key.startswith('_'):
            continue
        state['drifts'].pop(gone_key, None)
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
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        return 1


if __name__ == '__main__':
    sys.exit(main())
