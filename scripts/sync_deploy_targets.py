#!/usr/bin/env python3
"""sync_deploy_targets.py — deploy_targets ↔ Vercel drift detector (E2.1).

Every 12h via systemd timer. Reconciles `config/deploy_targets.json`
against what the Vercel API actually reports under the account that owns
VERCEL_TOKEN:

  - MISSING_FROM_REGISTRY: a project exists on Vercel but no entry in
    deploy_targets.json matches its `id`. Means a project was created on
    Vercel without a registry update — the 4-artifact discipline
    (registry + env var keys + framework declaration + notes) was skipped.
  - MISSING_FROM_VERCEL: a registry entry's vercel_project_id returns 404
    on Vercel. Means the project was deleted or renamed; the registry now
    points at nothing and the deploy notifier (E2.2) would silently fail.
  - NAME_MISMATCH: both sides have the project (matched by id), but the
    human-readable Vercel name differs from the registry `name`. Low-
    severity — usually a rename that needs PR'ing into the registry.

Per E1.5 design: dry-run by default. Re-DM the same drift only once per
24h (matches the credential-drift healer's cadence-per-tick pattern: with
a 12h timer, 24h means ~2 ticks between repeated DMs).

Adapted from heal_credential_registry_drift.py (E1.5.2) for kill-switches,
dry-run defaults, activation-on-first-real-drift, state-file dedup, and
DM body shape. Differs in scanner strategy: instead of filesystem,
we call Vercel's REST API.

Stdlib only. Network I/O via urllib.request.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path('/home/larry/agents')
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'sync-deploy-targets.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'sync-deploy-targets.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'sync-deploy-targets.json'

REGISTRY_PATH = Path(__file__).resolve().parent.parent / 'config' / 'deploy_targets.json'
ENV_FILE = Path('/home/larry/credentials/.env.larry')

ENV_SYNC_ENABLED = 'OURLIBERTY_DEPLOY_TARGETS_SYNC_ENABLED'

# 24h re-DM cadence per E2.1 design (with 12h timer, 2 ticks per re-DM).
RE_DM_WINDOW = timedelta(hours=24)

VERCEL_API_BASE = 'https://api.vercel.com'
VERCEL_PROJECTS_PATH = '/v9/projects'
VERCEL_TIMEOUT_S = 20
VERCEL_PAGE_LIMIT = 100

DRIFT_KINDS = ('MISSING_FROM_REGISTRY', 'MISSING_FROM_VERCEL', 'NAME_MISMATCH')

_PRJ_ID_RE = re.compile(r'^prj_[A-Za-z0-9]+$')


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


def sync_enabled() -> bool:
    """True iff OURLIBERTY_DEPLOY_TARGETS_SYNC_ENABLED=true (case-insensitive)."""
    return os.environ.get(ENV_SYNC_ENABLED, '').strip().lower() == 'true'


# -------------------- state file (per-drift dedup) --------------------

def load_state() -> dict[str, Any]:
    """Return {'drifts': {drift_key: {'dm_count': N, 'last_dm_at': iso}},
    '_meta': {'activation_alerted': bool, 'infra_alert_last_at': iso}}.
    drift_key = '<identifier>:<kind>'."""
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {'drifts': {}, '_meta': {}}
        data.setdefault('drifts', {})
        data.setdefault('_meta', {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'drifts': {}, '_meta': {}}


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _drift_key(identifier: str, kind: str) -> str:
    return f'{identifier}:{kind}'


def _should_re_dm(
    state: dict[str, Any],
    identifier: str,
    kind: str,
    now: Optional[datetime] = None,
    window: timedelta = RE_DM_WINDOW,
) -> bool:
    """True iff this drift has never been DMed OR last DM was > window ago."""
    now = now or datetime.now(timezone.utc)
    entry = state['drifts'].get(_drift_key(identifier, kind))
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
    identifier: str,
    kind: str,
    now: Optional[datetime] = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    key = _drift_key(identifier, kind)
    entry = state['drifts'].setdefault(key, {'dm_count': 0})
    entry['dm_count'] = entry.get('dm_count', 0) + 1
    entry['last_dm_at'] = now.isoformat()


def _reconciled_keys(
    state: dict[str, Any], live_keys: set[str],
) -> list[str]:
    """Return drift keys present in state but not in the current live set."""
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
            source='sync-deploy-targets',
            severity=severity,
            message=message,
            subject=subject,
            suggested_action=suggested_action,
        )
    except Exception as e:
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- env-file token loader --------------------

def load_vercel_token(env_path: Path = ENV_FILE) -> Optional[str]:
    """Parse VERCEL_TOKEN out of an env-file. Returns None if absent / empty.

    Mirrors heal_credential_registry_drift.scan_env_file's parsing rules so
    quoting + comment handling stays consistent.
    """
    if not env_path.exists():
        return None
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            if key.strip() != 'VERCEL_TOKEN':
                continue
            unquoted = value.strip().strip("'").strip('"').strip()
            return unquoted or None
    except OSError as e:
        log(f'load_vercel_token read error on {env_path}: {e}', 'WARN')
    return None


# -------------------- registry loader --------------------

def load_registry(path: Path = REGISTRY_PATH) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log(f'load_registry failed for {path}: {type(e).__name__}: {e}', 'WARN')
        return None


# -------------------- Vercel API --------------------

class VercelAuthError(Exception):
    """Raised when Vercel returns 401/403 — credentials are bad."""


def _vercel_get(
    path: str, token: str, params: Optional[dict[str, Any]] = None,
    timeout: float = VERCEL_TIMEOUT_S,
) -> tuple[int, Any]:
    """GET a Vercel API endpoint. Returns (status_code, json_body_or_None).

    Raises VercelAuthError on 401/403; returns (status, None) for other
    non-2xx so callers can branch (e.g. 404 → MISSING_FROM_VERCEL drift).
    """
    url = VERCEL_API_BASE + path
    if params:
        url += '?' + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
    req = urllib.request.Request(
        url, headers={'Authorization': f'Bearer {token}'},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            try:
                return resp.status, json.loads(body) if body else None
            except json.JSONDecodeError as e:
                log(f'vercel GET {path}: JSON parse error: {e}', 'WARN')
                return resp.status, None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise VercelAuthError(f'{e.code} from Vercel on {path}')
        return e.code, None
    except (urllib.error.URLError, TimeoutError) as e:
        log(f'vercel GET {path} network error: {type(e).__name__}: {e}', 'WARN')
        return 0, None


def fetch_vercel_projects(token: str) -> list[dict[str, Any]]:
    """Page through /v9/projects. Returns the flat list of project objects.

    Personal account (Hobby) — no teamId query param. Raises VercelAuthError
    on 401/403. Returns [] on non-auth network errors (logged).
    """
    projects: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    page = 0
    while True:
        page += 1
        params: dict[str, Any] = {'limit': VERCEL_PAGE_LIMIT}
        if cursor:
            params['until'] = cursor
        status, body = _vercel_get(VERCEL_PROJECTS_PATH, token, params=params)
        if status != 200 or not isinstance(body, dict):
            log(f'fetch_vercel_projects page={page} status={status} '
                f'aborting pagination', 'WARN')
            break
        page_projects = body.get('projects') or []
        if not isinstance(page_projects, list):
            log(f'fetch_vercel_projects page={page}: projects field is not '
                f'a list, aborting', 'WARN')
            break
        projects.extend(p for p in page_projects if isinstance(p, dict))
        pagination = body.get('pagination') or {}
        next_cursor = pagination.get('next') if isinstance(pagination, dict) else None
        if not next_cursor or next_cursor == cursor:
            break
        cursor = str(next_cursor)
        if page >= 100:  # hard cap — Hobby tier won't approach this.
            log(f'fetch_vercel_projects hit page cap=100; stopping', 'WARN')
            break
    return projects


# -------------------- drift detection --------------------

def detect_drift(
    registry: dict[str, Any],
    vercel_projects: list[dict[str, Any]],
) -> tuple[list[tuple[str, str, dict[str, Any]]], set[str]]:
    """Compare registry deploy_targets against Vercel projects.

    Returns (drifts, live_keys). Drift tuple shape: (identifier, kind,
    payload). identifier is the registry `name` when known, else the
    Vercel project id (for MISSING_FROM_REGISTRY).
    """
    targets = registry.get('deploy_targets') or []

    by_vercel_id: dict[str, dict[str, Any]] = {}
    for entry in targets:
        if not isinstance(entry, dict):
            continue
        vid = entry.get('vercel_project_id')
        if isinstance(vid, str) and _PRJ_ID_RE.match(vid):
            by_vercel_id[vid] = entry

    by_project_id_vercel: dict[str, dict[str, Any]] = {}
    for p in vercel_projects:
        pid = p.get('id')
        if isinstance(pid, str):
            by_project_id_vercel[pid] = p

    drifts: list[tuple[str, str, dict[str, Any]]] = []
    live_keys: set[str] = set()

    # MISSING_FROM_REGISTRY: present on Vercel, absent from registry.
    for pid, project in by_project_id_vercel.items():
        if pid in by_vercel_id:
            continue
        vercel_name = project.get('name') or '(unnamed)'
        identifier = pid
        drifts.append((identifier, 'MISSING_FROM_REGISTRY', {
            'vercel_project_id': pid,
            'vercel_name': vercel_name,
        }))
        live_keys.add(_drift_key(identifier, 'MISSING_FROM_REGISTRY'))

    # MISSING_FROM_VERCEL and NAME_MISMATCH.
    for vid, entry in by_vercel_id.items():
        name = entry.get('name') or vid
        if vid not in by_project_id_vercel:
            drifts.append((name, 'MISSING_FROM_VERCEL', {
                'vercel_project_id': vid,
                'registry_name': name,
                'github_repo': entry.get('github_repo'),
            }))
            live_keys.add(_drift_key(name, 'MISSING_FROM_VERCEL'))
            continue
        vercel_project = by_project_id_vercel[vid]
        vercel_name = vercel_project.get('name')
        if isinstance(vercel_name, str) and vercel_name and vercel_name != name:
            drifts.append((name, 'NAME_MISMATCH', {
                'vercel_project_id': vid,
                'registry_name': name,
                'vercel_name': vercel_name,
            }))
            live_keys.add(_drift_key(name, 'NAME_MISMATCH'))

    return drifts, live_keys


# -------------------- DM rendering --------------------

def _render_missing_from_registry(identifier: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    pid = payload.get('vercel_project_id', identifier)
    vercel_name = payload.get('vercel_name', '(unnamed)')
    message = (
        f'Vercel project `{vercel_name}` ({pid}) is connected to the account '
        f'but has no matching entry in `config/deploy_targets.json`. The '
        f'deploy notifier (E2.2) will not route pushes to it until it is '
        f'registered.'
    )
    subject = f'deploy-targets-sync:MISSING_FROM_REGISTRY:{pid}'
    suggested = (
        f'Add a `deploy_targets[]` entry for `{vercel_name}` with '
        f'vercel_project_id={pid}, the matching github_repo, framework, '
        f'env_var_keys, and notes. Then `python3 scripts/validate_deploy_targets.py` '
        f'and PR through Mirror. If the project was created experimentally '
        f'and should be removed, delete it on the Vercel side instead.'
    )
    return message, subject, suggested


def _render_missing_from_vercel(identifier: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    pid = payload.get('vercel_project_id', '?')
    github_repo = payload.get('github_repo', '(no github_repo in entry)')
    message = (
        f'[deploy-targets-sync] registry entry `{identifier}` ({pid}) '
        f'returned 404 from Vercel. Project may have been deleted or '
        f'renamed (id changes). Source repo: `{github_repo}`. The deploy '
        f'notifier would silently no-op for pushes to this entry.'
    )
    subject = f'deploy-targets-sync:MISSING_FROM_VERCEL:{identifier}'
    suggested = (
        f'Either (a) restore / recreate the Vercel project and update '
        f'`config/deploy_targets.json` with the new vercel_project_id, or '
        f'(b) if `{identifier}` is intentionally retired, remove the entry '
        f'from `config/deploy_targets.json` in the same PR that retired '
        f'the project.'
    )
    return message, subject, suggested


def _render_name_mismatch(identifier: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    pid = payload.get('vercel_project_id', '?')
    registry_name = payload.get('registry_name', identifier)
    vercel_name = payload.get('vercel_name', '(unknown)')
    message = (
        f'[deploy-targets-sync] project {pid} has registry name '
        f'`{registry_name}` but Vercel reports `{vercel_name}`. Informational '
        f'— typically a Vercel-side rename that needs reflecting in the '
        f'registry. The deploy notifier still routes correctly (matched by '
        f'id), so this is non-blocking.'
    )
    subject = f'deploy-targets-sync:NAME_MISMATCH:{identifier}'
    suggested = (
        f'Either (a) update `config/deploy_targets.json`: change `name` '
        f'from `{registry_name}` to `{vercel_name}` (kebab-case if needed), '
        f'or (b) rename the Vercel project back to `{registry_name}` on '
        f'the Vercel dashboard.'
    )
    return message, subject, suggested


def _render_for_kind(name: str, kind: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    if kind == 'MISSING_FROM_REGISTRY':
        return _render_missing_from_registry(name, payload)
    if kind == 'MISSING_FROM_VERCEL':
        return _render_missing_from_vercel(name, payload)
    if kind == 'NAME_MISMATCH':
        return _render_name_mismatch(name, payload)
    raise ValueError(f'unknown drift kind: {kind!r}')


def _activation_message(drift_count: int) -> tuple[str, str, str]:
    """One-time activation prompt when dry-run AND there's real drift."""
    message = (
        f'I would have alerted on {drift_count} deploy_targets drift '
        f'item(s) — set `{ENV_SYNC_ENABLED}=true` to activate '
        f'`sync-deploy-targets` and receive per-drift DMs.'
    )
    subject = 'deploy-targets-sync: activate to receive drift alerts'
    suggested = (
        f'On the droplet (ssh larry@<droplet>): '
        f'`sudo systemctl edit ourliberty-sync-deploy-targets.service`, '
        f'add `Environment="{ENV_SYNC_ENABLED}=true"` under `[Service]`, '
        f'then `sudo systemctl restart ourliberty-sync-deploy-targets.timer`. '
        f'The next tick (within 12h) will DM each detected drift with '
        f'reconciliation instructions.'
    )
    return message, subject, suggested


def _infrastructure_alert_message(detail: str) -> tuple[str, str, str]:
    message = (
        f'[deploy-targets-sync] INFRASTRUCTURE ALERT — Vercel API '
        f'rejected our credentials. Detail: {detail}. The drift detector '
        f'cannot run until VERCEL_TOKEN is restored. Until then, registry-'
        f'vs-Vercel drift goes undetected.'
    )
    subject = 'deploy-targets-sync:INFRASTRUCTURE_ALERT:vercel-auth'
    suggested = (
        f'Rotate VERCEL_TOKEN per `docs/runbooks/rotate-vercel-token.md`. '
        f'Update `/home/larry/credentials/.env.larry` and restart the timer.'
    )
    return message, subject, suggested


# -------------------- main orchestration --------------------

def run_once(
    registry: Optional[dict[str, Any]] = None,
    state: Optional[dict[str, Any]] = None,
    vercel_projects: Optional[list[dict[str, Any]]] = None,
    vercel_token: Optional[str] = None,
    now: Optional[datetime] = None,
    dry_run_override: Optional[bool] = None,
    fetch_fn=None,
) -> dict[str, int]:
    """Single-tick orchestration. Returns counts dict (also for tests).

    Args:
        registry: pre-loaded registry; None to load from disk.
        state: pre-loaded state; None to load from disk (with save on exit).
        vercel_projects: pre-fetched project list; None to call Vercel API.
            Test hook — pass a list to bypass network I/O.
        vercel_token: pre-loaded token; None to parse from env file.
        now: anchor for re-DM dedup.
        dry_run_override: when set, overrides env-var detection.
        fetch_fn: test hook — alternative to fetch_vercel_projects.
    """
    counts = {
        'missing_from_registry': 0,
        'missing_from_vercel': 0,
        'name_mismatch': 0,
        'dm_sent': 0,
        'dm_suppressed_dedup': 0,
        'reconciled_gc': 0,
        'infra_alert': 0,
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
        else not sync_enabled()
    )

    state_was_none = state is None
    if state is None:
        state = load_state()
    state.setdefault('drifts', {})
    state.setdefault('_meta', {})

    # Fetch projects unless caller provided them.
    auth_error: Optional[VercelAuthError] = None
    if vercel_projects is None:
        token = vercel_token or load_vercel_token()
        if not token:
            log('VERCEL_TOKEN not found in env file; cannot reconcile', 'WARN')
            return counts
        try:
            fetcher = fetch_fn or fetch_vercel_projects
            vercel_projects = fetcher(token)
        except VercelAuthError as e:
            auth_error = e
            vercel_projects = []

    if auth_error is not None:
        counts['infra_alert'] = 1
        # Throttle infra alert by RE_DM_WINDOW too — keyed by 'vercel-auth'.
        if _should_re_dm(state, 'vercel-auth', 'INFRASTRUCTURE_ALERT', now=now):
            msg, subj, sug = _infrastructure_alert_message(str(auth_error))
            ok = dm_larry(message=msg, subject=subj, suggested_action=sug,
                          severity='critical')
            if ok:
                counts['dm_sent'] += 1
                _record_dm(state, 'vercel-auth', 'INFRASTRUCTURE_ALERT', now=now)
        if state_was_none:
            save_state(state)
        # Non-zero exit so systemd surfaces it.
        log(f'tick: dry_run={dry_run} infra_alert=1 (Vercel auth)')
        raise VercelAuthError(str(auth_error))

    drifts, live_keys = detect_drift(registry, vercel_projects)

    for identifier, kind, payload in drifts:
        if kind == 'MISSING_FROM_REGISTRY':
            counts['missing_from_registry'] += 1
        elif kind == 'MISSING_FROM_VERCEL':
            counts['missing_from_vercel'] += 1
        elif kind == 'NAME_MISMATCH':
            counts['name_mismatch'] += 1

        msg, subj, sug = _render_for_kind(identifier, kind, payload)

        if not _should_re_dm(state, identifier, kind, now=now):
            counts['dm_suppressed_dedup'] += 1
            continue

        if dry_run:
            if not state['_meta'].get('activation_alerted'):
                drift_count = len(drifts)
                a_msg, a_subj, a_sug = _activation_message(drift_count)
                if dm_larry(message=a_msg, subject=a_subj,
                            suggested_action=a_sug):
                    counts['dm_sent'] += 1
                state['_meta']['activation_alerted'] = True
            log(f'DRY-RUN drift: {kind} {identifier} (suppressed; activate '
                f'via {ENV_SYNC_ENABLED}=true)')
            continue

        ok = dm_larry(message=msg, subject=subj, suggested_action=sug,
                      severity='warning')
        if ok:
            counts['dm_sent'] += 1
            _record_dm(state, identifier, kind, now=now)
            log(f'DM sent for {kind} {identifier}')
        else:
            log(f'DM append suppressed (cooldown or write error) for '
                f'{kind} {identifier}', 'WARN')

    for gone_key in _reconciled_keys(state, live_keys):
        # 'vercel-auth:INFRASTRUCTURE_ALERT' is preserved — it's a transient
        # error class, not a drift item; let its own RE_DM_WINDOW gate it.
        if gone_key.endswith(':INFRASTRUCTURE_ALERT'):
            continue
        state['drifts'].pop(gone_key, None)
        counts['reconciled_gc'] += 1

    log(f'tick: dry_run={dry_run} '
        + ' '.join(f'{k}={v}' for k, v in counts.items() if v))

    if state_was_none:
        save_state(state)
    return counts


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='Reconcile config/deploy_targets.json against Vercel.',
    )
    p.add_argument('--once', action='store_true',
                   help='Single run (default; reserved for symmetry with '
                        'long-running daemons).')
    p.add_argument('--dry-run', action='store_true',
                   help=f'Force dry-run mode even if {ENV_SYNC_ENABLED} is '
                        f'set. Useful for diagnostics.')
    return p.parse_args(argv[1:])


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv or sys.argv)
    try:
        run_once(
            dry_run_override=True if args.dry_run else None,
        )
        return 0
    except VercelAuthError as e:
        log(f'EXIT non-zero on Vercel auth error: {e}', 'ERROR')
        return 2
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
