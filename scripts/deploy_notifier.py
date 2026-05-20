#!/usr/bin/env python3
"""deploy_notifier.py — Vercel preview URL + build-failure notifier (E2.2).

Every 2 min via systemd timer. Polls Vercel's `/v6/deployments?state=READY,ERROR`
endpoint, filters by the GitHub repos in `config/deploy_targets.json`, and DMs
Larry via the existing `larry_alerts.append_alert` queue when:

  - state=READY: a preview build is live (DM with the preview URL).
  - state=ERROR: a preview build failed (DM with the Vercel inspect link).

Skipped states: BUILDING, QUEUED, INITIALIZING, CANCELED (transient or
human-cancelled — nothing actionable to surface).

Design (mirrors `sync_deploy_targets.py` from E2.1):
  - `Type=oneshot` systemd unit; `--once` is the only real mode.
  - Stdlib only — `urllib.request` for HTTP, `subprocess` for `gh` fallback.
  - Default off: `OURLIBERTY_DEPLOY_NOTIFIER_ENABLED=true` required to
    actually DM. Otherwise dry-run logs would-DM lines and fires a one-time
    activation prompt when there's a real deployment to surface.
  - State-file dedup keyed by `<uid>:<state>` so a deployment that transitions
    READY → ERROR re-DMs (rare but real); plain re-runs on the same uid+state
    are silently skipped. State capped at most-recent 1000 entries (LRU prune).
  - 401/403 → `INFRASTRUCTURE_ALERT` critical DM, throttled to once per 24h.
  - 5xx / network errors → log, exit non-zero (systemd retries next tick).
  - Empty deploy_targets array → log "no targets configured" and exit 0.
  - Per-target `branch_filter` (null=match-all, glob via fnmatch) silently
    skips non-matching branches.
  - PR# resolution priority: `deployment.meta.githubPrId` → `gh pr list
    --head <branch> --repo <owner/repo>` fallback → `(unknown)` literal in
    body when both fail.

Path isolation: AGENTS_ROOT honors `OURLIBERTY_AGENTS_ROOT` env override so
unit tests can monkeypatch the root into a tmpdir without polluting the
production `~/agents/logs/deploy-notifier.log`. (The wider healer-test
isolation issue is tracked separately — for THIS script only.)

Adapted from `sync_deploy_targets.py` (E2.1). Differs in: cadence (2 min vs
12 h), API endpoint (`/v6/deployments` vs `/v9/projects`), what triggers a
DM (per-deployment state transition vs cross-side drift), and the
AGENTS_ROOT env override.

Stdlib only.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# ---- AGENTS_ROOT + derived paths (env-overridable for test isolation) ----

AGENTS_ROOT = Path(os.environ.get(
    'OURLIBERTY_AGENTS_ROOT', '/home/larry/agents',
))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'deploy-notifier.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'deploy-notifier.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'deploy-notifier.json'

REGISTRY_PATH = Path(__file__).resolve().parent.parent / 'config' / 'deploy_targets.json'
ENV_FILE = Path('/home/larry/credentials/.env.larry')

ENV_NOTIFIER_ENABLED = 'OURLIBERTY_DEPLOY_NOTIFIER_ENABLED'

VERCEL_API_BASE = 'https://api.vercel.com'
VERCEL_DEPLOYMENTS_PATH = '/v6/deployments'
VERCEL_TIMEOUT_S = 20
VERCEL_PAGE_LIMIT = 20
VERCEL_PAGE_CAP = 5  # never traverse more than 5 pages per tick

# 24h throttle for infra alerts (Vercel auth failures + registry-parse failures).
INFRA_ALERT_WINDOW = timedelta(hours=24)

# LRU cap on the notified-uid history kept in the state file.
NOTIFIED_HISTORY_CAP = 1000

# States Vercel reports; only these two trigger a DM.
NOTIFY_STATES = frozenset({'READY', 'ERROR'})


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


def notifier_enabled() -> bool:
    """True iff OURLIBERTY_DEPLOY_NOTIFIER_ENABLED=true (case-insensitive)."""
    return os.environ.get(ENV_NOTIFIER_ENABLED, '').strip().lower() == 'true'


# -------------------- state file --------------------

def load_state() -> dict[str, Any]:
    """Return state shape: {
        '$schema_version': 1,
        'notified': {<uid:state>: <iso-ts-of-first-dm>},
        'notified_order': [<uid:state>, ...],  # FIFO for LRU prune
        'last_polled': <iso>,
        '_meta': {'activation_alerted': bool,
                  'infra_alert_last_at': {<key>: <iso>}},
    }.
    """
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return _empty_state()
        data.setdefault('$schema_version', 1)
        data.setdefault('notified', {})
        data.setdefault('notified_order', [])
        data.setdefault('_meta', {})
        meta = data['_meta']
        if not isinstance(meta, dict):
            data['_meta'] = {}
        data['_meta'].setdefault('infra_alert_last_at', {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_state()


def _empty_state() -> dict[str, Any]:
    return {
        '$schema_version': 1,
        'notified': {},
        'notified_order': [],
        '_meta': {'infra_alert_last_at': {}},
    }


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _notify_key(uid: str, state: str) -> str:
    return f'{uid}:{state}'


def _record_notified(
    state: dict[str, Any], uid: str, vercel_state: str,
    now: Optional[datetime] = None,
) -> None:
    """Append to notified history; prune to NOTIFIED_HISTORY_CAP (LRU/FIFO)."""
    now = now or datetime.now(timezone.utc)
    key = _notify_key(uid, vercel_state)
    notified = state.setdefault('notified', {})
    order = state.setdefault('notified_order', [])
    if key in notified:
        return
    notified[key] = now.isoformat()
    order.append(key)
    # FIFO prune from the front when over cap.
    while len(order) > NOTIFIED_HISTORY_CAP:
        evicted = order.pop(0)
        notified.pop(evicted, None)


def _already_notified(state: dict[str, Any], uid: str, vercel_state: str) -> bool:
    return _notify_key(uid, vercel_state) in state.get('notified', {})


def _should_infra_alert(
    state: dict[str, Any], key: str,
    now: Optional[datetime] = None,
    window: timedelta = INFRA_ALERT_WINDOW,
) -> bool:
    now = now or datetime.now(timezone.utc)
    last = state.get('_meta', {}).get('infra_alert_last_at', {}).get(key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now - last_dt) >= window


def _record_infra_alert(
    state: dict[str, Any], key: str, now: Optional[datetime] = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    state.setdefault('_meta', {}).setdefault('infra_alert_last_at', {})[key] = now.isoformat()


# -------------------- DM --------------------

def dm_larry(
    message: str, subject: str, suggested_action: str = '',
    severity: str = 'warning',
) -> bool:
    """Send a DM via larry_alerts.append_alert. Returns True on append,
    False on cooldown/error. Never raises."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='deploy-notifier',
            severity=severity,
            message=message,
            subject=subject,
            suggested_action=suggested_action or None,
        )
    except Exception as e:
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- env-file token loader --------------------

def load_vercel_token(env_path: Path = ENV_FILE) -> Optional[str]:
    """Parse VERCEL_TOKEN out of an env-file (same rules as sync_deploy_targets)."""
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

    Raises VercelAuthError on 401/403. Returns (status, None) for other
    non-2xx so callers can branch.
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


def fetch_vercel_deployments(token: str) -> list[dict[str, Any]]:
    """Page through /v6/deployments?state=READY,ERROR. Returns flat list.

    Caps at VERCEL_PAGE_CAP pages per tick. Raises VercelAuthError on 401/403.
    """
    deployments: list[dict[str, Any]] = []
    until: Optional[int] = None
    page = 0
    while page < VERCEL_PAGE_CAP:
        page += 1
        params: dict[str, Any] = {
            'limit': VERCEL_PAGE_LIMIT,
            'state': 'READY,ERROR',
        }
        if until is not None:
            params['until'] = until
        status, body = _vercel_get(VERCEL_DEPLOYMENTS_PATH, token, params=params)
        if status != 200 or not isinstance(body, dict):
            log(f'fetch_vercel_deployments page={page} status={status} '
                f'aborting pagination', 'WARN')
            break
        page_deployments = body.get('deployments') or []
        if not isinstance(page_deployments, list):
            log(f'fetch_vercel_deployments page={page}: deployments field is '
                f'not a list, aborting', 'WARN')
            break
        deployments.extend(d for d in page_deployments if isinstance(d, dict))
        pagination = body.get('pagination') or {}
        next_cursor = pagination.get('next') if isinstance(pagination, dict) else None
        if not next_cursor or next_cursor == until:
            break
        until = next_cursor
    if page >= VERCEL_PAGE_CAP:
        log(f'fetch_vercel_deployments hit page cap={VERCEL_PAGE_CAP}; '
            f'remaining pages skipped this tick', 'INFO')
    return deployments


# -------------------- target matching + filtering --------------------

def _target_for_deployment(
    deployment: dict[str, Any], targets: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Return the registry target matching this deployment, or None.

    Matches on vercel_project_id (deployment's `projectId` field — Vercel's
    deployment list responses include this).
    """
    pid = deployment.get('projectId')
    if not isinstance(pid, str):
        return None
    for t in targets:
        if isinstance(t, dict) and t.get('vercel_project_id') == pid:
            return t
    return None


def _deployment_branch(deployment: dict[str, Any]) -> Optional[str]:
    meta = deployment.get('meta') or {}
    if not isinstance(meta, dict):
        return None
    ref = meta.get('githubCommitRef')
    return ref if isinstance(ref, str) and ref else None


def _branch_matches(branch: Optional[str], filter_glob: Optional[str]) -> bool:
    """True if branch passes the target's branch_filter.

    - filter_glob=None → match all (None always wins).
    - filter_glob='main' → exact match.
    - filter_glob='forge/*' → fnmatch glob.
    - branch=None and filter_glob set → no match (we can't tell).
    """
    if filter_glob is None:
        return True
    if not branch:
        return False
    return fnmatch.fnmatch(branch, filter_glob)


# -------------------- PR# resolution --------------------

def _resolve_pr_number(
    deployment: dict[str, Any], target: dict[str, Any],
    gh_runner=None,
) -> tuple[Optional[int], str]:
    """Return (pr_number_or_None, resolution_source).

    Resolution_source is one of: 'meta', 'gh', 'unknown' — for logging.
    """
    meta = deployment.get('meta') or {}
    if isinstance(meta, dict):
        # Vercel encodes githubPrId as either an int or a string.
        raw = meta.get('githubPrId')
        if isinstance(raw, int) and raw > 0:
            return raw, 'meta'
        if isinstance(raw, str) and raw.isdigit():
            n = int(raw)
            if n > 0:
                return n, 'meta'

    branch = _deployment_branch(deployment)
    github_repo = target.get('github_repo')
    if not branch or not isinstance(github_repo, str) or '/' not in github_repo:
        return None, 'unknown'

    runner = gh_runner or _gh_pr_for_branch
    pr = runner(branch, github_repo)
    if isinstance(pr, int) and pr > 0:
        return pr, 'gh'
    return None, 'unknown'


def _gh_pr_for_branch(branch: str, github_repo: str) -> Optional[int]:
    """Call `gh pr list --head <branch> --repo <repo> --json number --limit 1`.

    Returns the PR number or None if not found / gh missing / error.
    """
    try:
        proc = subprocess.run(
            [
                'gh', 'pr', 'list',
                '--head', branch,
                '--repo', github_repo,
                '--json', 'number',
                '--limit', '1',
                '--state', 'open',
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f'gh pr list failed: {type(e).__name__}: {e}', 'WARN')
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or '[]')
    except json.JSONDecodeError:
        return None
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            n = first.get('number')
            if isinstance(n, int) and n > 0:
                return n
    return None


# -------------------- DM body rendering --------------------

def _pr_field(pr_number: Optional[int], pr_title: str = '') -> str:
    if pr_number is None:
        return 'PR #(unknown)'
    if pr_title:
        return f'PR #{pr_number} ({pr_title})'
    return f'PR #{pr_number}'


def _render_ready(
    target: dict[str, Any], deployment: dict[str, Any],
    pr_number: Optional[int],
) -> tuple[str, str, str]:
    project = target.get('name') or deployment.get('name') or '(unknown)'
    branch = _deployment_branch(deployment) or '(unknown)'
    url = deployment.get('url') or ''
    preview_url = f'https://{url}' if url and not url.startswith('http') else url
    pr = _pr_field(pr_number, _commit_message_first_line(deployment))
    body = (
        f'Vercel preview live\n'
        f'Project: {project}\n'
        f'{pr}\n'
        f'Branch: {branch}\n'
        f'URL: {preview_url}'
    )
    subject = f'deploy-notifier:READY:{deployment.get("uid", "?")}'
    return body, subject, ''


def _render_error(
    target: dict[str, Any], deployment: dict[str, Any],
    pr_number: Optional[int],
) -> tuple[str, str, str]:
    project = target.get('name') or deployment.get('name') or '(unknown)'
    branch = _deployment_branch(deployment) or '(unknown)'
    uid = deployment.get('uid', '?')
    # Inspect URL: vercel.com/<owner>/<project>/<uid>. The team/owner slug
    # isn't on the deployment object on personal accounts; fall back to the
    # registry's `vercel_org_id` mapping. For personal (null) accounts we
    # can't fabricate the slug, so omit the path component and link to the
    # dashboard root — Larry still recognizes the uid.
    inspect = f'https://vercel.com/dashboard/deployments/{uid}'
    pr = _pr_field(pr_number, _commit_message_first_line(deployment))
    body = (
        f'Vercel build FAILED\n'
        f'Project: {project}\n'
        f'{pr}\n'
        f'Branch: {branch}\n'
        f'Inspect: {inspect}'
    )
    subject = f'deploy-notifier:ERROR:{uid}'
    suggested = (
        f'Check the Vercel build log at the inspect URL above. If the '
        f'failure is from a missing env var, add it to the Vercel project '
        f'and re-deploy (push an empty commit or hit "Redeploy" in the UI).'
    )
    return body, subject, suggested


def _commit_message_first_line(deployment: dict[str, Any]) -> str:
    meta = deployment.get('meta') or {}
    if not isinstance(meta, dict):
        return ''
    msg = meta.get('githubCommitMessage')
    if not isinstance(msg, str) or not msg:
        return ''
    return msg.splitlines()[0].strip()[:80]


def _render_for_state(
    state_str: str, target: dict[str, Any], deployment: dict[str, Any],
    pr_number: Optional[int],
) -> tuple[str, str, str]:
    if state_str == 'READY':
        return _render_ready(target, deployment, pr_number)
    if state_str == 'ERROR':
        return _render_error(target, deployment, pr_number)
    raise ValueError(f'unsupported state for DM: {state_str!r}')


def _activation_message(pending_count: int) -> tuple[str, str, str]:
    message = (
        f'I would have alerted on {pending_count} Vercel deployment '
        f'event(s) — set `{ENV_NOTIFIER_ENABLED}=true` to activate '
        f'`deploy-notifier` and receive preview-URL + build-failure DMs.'
    )
    subject = 'deploy-notifier: activate to receive preview-URL DMs'
    suggested = (
        f'On the droplet: '
        f'`sudo systemctl edit ourliberty-deploy-notifier.service`, add '
        f'`Environment="{ENV_NOTIFIER_ENABLED}=true"` under `[Service]`, '
        f'then `sudo systemctl restart ourliberty-deploy-notifier.timer`. '
        f'The next tick (within ~2 min) will DM each new READY / ERROR '
        f'deployment.'
    )
    return message, subject, suggested


def _infrastructure_alert_message(detail: str) -> tuple[str, str, str]:
    message = (
        f'[deploy-notifier] INFRASTRUCTURE ALERT — Vercel API rejected our '
        f'credentials. Detail: {detail}. Preview URLs + build failures will '
        f'NOT be DMed until VERCEL_TOKEN is restored.'
    )
    subject = 'deploy-notifier:INFRASTRUCTURE_ALERT:vercel-auth'
    suggested = (
        f'Rotate VERCEL_TOKEN per `docs/runbooks/rotate-vercel-token.md`. '
        f'Update `/home/larry/credentials/.env.larry` and restart the timer.'
    )
    return message, subject, suggested


def _registry_parse_alert_message(detail: str) -> tuple[str, str, str]:
    message = (
        f'[deploy-notifier] `config/deploy_targets.json` failed to parse: '
        f'{detail}. Notifier is no-oping until fixed.'
    )
    subject = 'deploy-notifier:INFRASTRUCTURE_ALERT:registry-parse'
    suggested = (
        f'Run `python3 scripts/validate_deploy_targets.py` to surface the '
        f'parse error, fix it, and push through Mirror.'
    )
    return message, subject, suggested


# -------------------- main orchestration --------------------

def run_once(
    registry: Optional[dict[str, Any]] = None,
    state: Optional[dict[str, Any]] = None,
    vercel_deployments: Optional[list[dict[str, Any]]] = None,
    vercel_token: Optional[str] = None,
    now: Optional[datetime] = None,
    dry_run_override: Optional[bool] = None,
    fetch_fn=None,
    gh_runner=None,
) -> dict[str, int]:
    """Single-tick orchestration. Returns counts dict (also for tests).

    Args mirror sync_deploy_targets.run_once for symmetry. All deps are
    injectable for tests.
    """
    counts = {
        'ready': 0,
        'error': 0,
        'skipped_state': 0,
        'skipped_no_target': 0,
        'skipped_branch_filter': 0,
        'skipped_already_notified': 0,
        'dm_sent': 0,
        'infra_alert': 0,
        'registry_missing': 0,
    }
    if kill_switch_active():
        log(f'KILL_SWITCH active at {KILL_SWITCH}; exiting cleanly')
        return counts
    heartbeat()

    state_was_none = state is None
    if state is None:
        state = load_state()
    state.setdefault('notified', {})
    state.setdefault('notified_order', [])
    state.setdefault('_meta', {})
    state['_meta'].setdefault('infra_alert_last_at', {})

    # Load registry.
    registry_in = registry
    if registry_in is None:
        registry_in = load_registry()
    if registry_in is None:
        counts['registry_missing'] = 1
        if _should_infra_alert(state, 'registry-parse', now=now):
            msg, subj, sug = _registry_parse_alert_message(
                f'unreadable or invalid JSON at {REGISTRY_PATH}'
            )
            if dm_larry(message=msg, subject=subj, suggested_action=sug,
                        severity='critical'):
                counts['dm_sent'] += 1
                _record_infra_alert(state, 'registry-parse', now=now)
        if state_was_none:
            save_state(state)
        log(f'tick: registry_missing=1', 'WARN')
        # Non-zero exit signal: raise an exception caller can map.
        raise RuntimeError(f'deploy_targets registry unreadable at {REGISTRY_PATH}')

    targets = registry_in.get('deploy_targets') or []
    if not isinstance(targets, list):
        targets = []

    # Empty-registry short-circuit. No API call. No DM.
    if not targets:
        log('no targets configured; nothing to poll')
        state['last_polled'] = (now or datetime.now(timezone.utc)).isoformat()
        if state_was_none:
            save_state(state)
        return counts

    dry_run = (
        dry_run_override
        if dry_run_override is not None
        else not notifier_enabled()
    )

    # Fetch deployments unless caller provided them.
    auth_error: Optional[VercelAuthError] = None
    if vercel_deployments is None:
        token = vercel_token or load_vercel_token()
        if not token:
            log('VERCEL_TOKEN not found in env file; cannot poll', 'WARN')
            counts['infra_alert'] = 1
            if _should_infra_alert(state, 'vercel-token-missing', now=now):
                msg, subj, sug = _infrastructure_alert_message(
                    'VERCEL_TOKEN not present in /home/larry/credentials/.env.larry'
                )
                if dm_larry(message=msg, subject=subj, suggested_action=sug,
                            severity='critical'):
                    counts['dm_sent'] += 1
                    _record_infra_alert(state, 'vercel-token-missing', now=now)
            if state_was_none:
                save_state(state)
            raise RuntimeError('VERCEL_TOKEN missing')
        try:
            fetcher = fetch_fn or fetch_vercel_deployments
            vercel_deployments = fetcher(token)
        except VercelAuthError as e:
            auth_error = e
            vercel_deployments = []

    if auth_error is not None:
        counts['infra_alert'] = 1
        if _should_infra_alert(state, 'vercel-auth', now=now):
            msg, subj, sug = _infrastructure_alert_message(str(auth_error))
            if dm_larry(message=msg, subject=subj, suggested_action=sug,
                        severity='critical'):
                counts['dm_sent'] += 1
                _record_infra_alert(state, 'vercel-auth', now=now)
        if state_was_none:
            save_state(state)
        log(f'tick: dry_run={dry_run} infra_alert=1 (Vercel auth)')
        raise VercelAuthError(str(auth_error))

    # Score each deployment.
    pending_dms: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for d in vercel_deployments:
        vstate = d.get('state') or d.get('readyState')
        if vstate not in NOTIFY_STATES:
            counts['skipped_state'] += 1
            continue
        uid = d.get('uid')
        if not isinstance(uid, str) or not uid:
            counts['skipped_state'] += 1
            continue
        target = _target_for_deployment(d, targets)
        if target is None:
            counts['skipped_no_target'] += 1
            continue
        branch = _deployment_branch(d)
        if not _branch_matches(branch, target.get('branch_filter')):
            counts['skipped_branch_filter'] += 1
            continue
        if _already_notified(state, uid, vstate):
            counts['skipped_already_notified'] += 1
            continue
        pending_dms.append((d, target, vstate))
        if vstate == 'READY':
            counts['ready'] += 1
        else:
            counts['error'] += 1

    # Dry-run: emit one-time activation prompt if there's anything pending.
    if pending_dms and dry_run:
        if not state['_meta'].get('activation_alerted'):
            a_msg, a_subj, a_sug = _activation_message(len(pending_dms))
            if dm_larry(message=a_msg, subject=a_subj,
                        suggested_action=a_sug):
                counts['dm_sent'] += 1
            state['_meta']['activation_alerted'] = True
        for d, target, vstate in pending_dms:
            log(f'DRY-RUN deployment: {vstate} uid={d.get("uid")} '
                f'project={target.get("name")} branch={_deployment_branch(d)} '
                f'(suppressed; activate via {ENV_NOTIFIER_ENABLED}=true)')
        state['last_polled'] = (now or datetime.now(timezone.utc)).isoformat()
        log(f'tick: dry_run=True '
            + ' '.join(f'{k}={v}' for k, v in counts.items() if v))
        if state_was_none:
            save_state(state)
        return counts

    # Live mode: actually DM each pending deployment.
    for d, target, vstate in pending_dms:
        uid = d['uid']
        pr_number, source = _resolve_pr_number(d, target, gh_runner=gh_runner)
        log(f'PR resolution for uid={uid}: {source} -> {pr_number}')

        msg, subj, sug = _render_for_state(vstate, target, d, pr_number)
        severity = 'critical' if vstate == 'ERROR' else 'warning'
        ok = dm_larry(message=msg, subject=subj, suggested_action=sug,
                      severity=severity)
        if ok:
            counts['dm_sent'] += 1
            _record_notified(state, uid, vstate, now=now)
            log(f'DM sent for {vstate} uid={uid} project={target.get("name")}')
        else:
            log(f'DM append suppressed (cooldown or write error) for '
                f'{vstate} uid={uid}', 'WARN')

    state['last_polled'] = (now or datetime.now(timezone.utc)).isoformat()
    log(f'tick: dry_run={dry_run} '
        + ' '.join(f'{k}={v}' for k, v in counts.items() if v))

    if state_was_none:
        save_state(state)
    return counts


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='Poll Vercel for preview URLs + build failures and DM '
                    'Larry via larry_alerts.',
    )
    p.add_argument('--once', action='store_true',
                   help='Single run (default; reserved for symmetry with '
                        'long-running daemons).')
    p.add_argument('--dry-run', action='store_true',
                   help=f'Force dry-run mode even if {ENV_NOTIFIER_ENABLED} '
                        f'is set. Useful for diagnostics.')
    p.add_argument('--verbose', action='store_true',
                   help='Reserved; logging is always INFO-level.')
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
    except RuntimeError as e:
        log(f'EXIT non-zero on runtime error: {e}', 'ERROR')
        return 3
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
