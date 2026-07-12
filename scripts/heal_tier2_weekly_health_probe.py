#!/usr/bin/env python3
"""heal_tier2_weekly_health_probe.py — Tier 2 OAuth liveness + keep-alive probe.

Runs every 6h (00/06/12/18 UTC) per the systemd timer in
`ourliberty-heal-tier2-weekly-health-probe.timer`. Invokes a minimal
`claude -p 'say PROBE_OK'` against the Tier 2 account (HOME-overridden
to `/home/larry/.claude-larry-personal`) using the Haiku model — the
cheapest viable option so the probe costs ~$0.001 per run.

Filename / symbol names still carry the legacy ``weekly`` token because
the systemd unit, the alert subject (``tier2_weekly_probe_failed``), the
log token (``TIER2_WEEKLY_PROBE_OK``), and the alert-translations entry
key on it — renaming would force a coordinated change across all of them
plus shipped state files. The cadence is the only thing that changed.

Motivation
----------
2026-05-26/27 incident: Tier 2 was provisioned at a known-good state,
then silently rotted (credential expiry, OAuth refresh failure). The
failure mode only surfaced when Tier 1 hit its rate limit and Tier 2
fallback was attempted — at which point the incident had already
escalated. The original weekly probe caught credential rot but at
7-day cadence could NOT keep the Tier 2 OAuth token warm (~14h
lifetime). The 2026-05-30 step-A fix dropped the cadence to 6h so the
probe fires inside the token's lifetime and triggers the CLI's
auto-refresh path, keeping the refresh token exercised. Liveness +
keep-alive are now a single probe.

Success path: probe returns `PROBE_OK` with exit 0 → log
`TIER2_WEEKLY_PROBE_OK`, no DM. Silence is the success signal.

Failure paths (any of):
  * Non-zero exit from `claude`
  * `is_error: true` in the JSON output (--output-format json)
  * Output doesn't contain `PROBE_OK`
  * Subprocess timeout / claude binary missing
→ DM with severity=warning, intent=tier2_weekly_probe_failed, plus
the recovery hint pointing at the runbook.

State
-----
* Cooldown ledger: `~/agents/state/heal-tier2-weekly-probe-state.json` —
  records last-DM-fired-at timestamp. 6h cooldown so a sustained Tier 2
  outage doesn't spam (the next probe is 7 days out anyway; the cooldown
  matters for manual re-runs and systemd restart cycles).
* Heartbeat: `~/agents/blackboard/heal-tier2-weekly-probe.heartbeat`.
* Kill switch: `~/agents/healers.disabled`.
* Log: `~/agents/logs/heal-tier2-weekly-probe.log`.

Phase E4 followup, 2026-05-27 — claude-quota-fixes-v2 bundle.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import active_tier  # noqa: E402
import larry_alerts  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-tier2-weekly-probe.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-tier2-weekly-probe.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-tier2-weekly-probe-state.json'

TIER2_HOME = '/home/larry/.claude-larry-personal'
CLAUDE_BIN = os.environ.get('CLAUDE_BIN', '/usr/bin/claude')
PROBE_PROMPT = 'say PROBE_OK'
EXPECTED_TOKEN = 'PROBE_OK'
PROBE_TIMEOUT_SEC = 60
DM_COOLDOWN_HOURS = 6

# The OAuth-liveness alert this healer emits (source:subject). The retraction
# keys on this SAME source:subject so a recovered probe clears exactly its own
# stale red, nothing else. The sibling `tier2_provisioning_drift` subject is a
# set-diff/parity clear and is deliberately NOT retracted here (deferred).
PROBE_ALERT_SOURCE = 'heal-tier2-weekly-probe'
PROBE_ALERT_SUBJECT = 'tier2_weekly_probe_failed'

_CONFIG_FILE = Path(__file__).resolve().parent.parent / 'config' / 'agent-models.json'

# Haiku is the cheapest viable Claude model — probe is intentionally
# minimal so it costs ~$0.001 per run. Pinned default in case config is
# missing the models.haiku entry; the registry is the source of truth
# when present.
DEFAULT_HAIKU_MODEL_ID = 'claude-haiku-4-5-20251001'

# Failure-mode-specific recovery hints. The 2026-06-03 incident showed why a
# single generic "re-auth Tier 2" hint is actively misleading: the probe failed
# for 30h with TimeoutExpired (its own 256M cgroup cap starving a ~280M claude
# process) while the setup-token and real dispatch were perfectly healthy.
# Re-authing would have wasted a credential rotation and not fixed anything.
# So: route auth failures (401) to the re-auth runbooks, and route
# environment failures (timeout / OOM-kill / missing binary) to a "verify
# before you re-auth" hint that points at the probe's own resources.
_AUTH_HINT = (
    'Re-auth Tier 2 — the setup-token is the PRIMARY dispatch auth, so start '
    'with docs/runbooks/rotate-claude-setup-tokens.md (Tier 2); the legacy '
    'credentials.json fallback is docs/runbooks/'
    'restore-larry-personal-claude-oauth-tier2.md.'
)
_ENV_HINT = (
    'This is a probe-ENVIRONMENT failure, NOT necessarily an auth failure — '
    'the Tier 2 token may be fine. Verify before re-authing: '
    '`HOME=/home/larry/.claude-larry-personal '
    'CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN_TIER2 claude -p '
    "'say PROBE_OK'`. If that returns PROBE_OK, the token is healthy and the "
    "fault is the probe's systemd unit (claude needs ~280M RSS — check "
    'MemoryMax) or the host. Do NOT rotate credentials on a green token.'
)
_GENERIC_HINT = (
    'Unclassified probe failure. Verify the token with a live probe '
    '(`HOME=/home/larry/.claude-larry-personal '
    'CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN_TIER2 claude -p '
    "'say PROBE_OK'`) before deciding between re-auth "
    '(docs/runbooks/rotate-claude-setup-tokens.md) and a host/unit fix.'
)


def classify_failure(stdout, stderr, exit_code):
    """Map a failed probe run to ``(reason_code, detail, suggested_action)``.

    Lets the operator (and the alert) distinguish a genuine auth failure
    (token bad -> re-auth) from a probe-environment failure (process hung or
    OOM-killed -> token likely fine; re-auth is the wrong move). Pure string
    classification over the subprocess output; never raises."""
    err = stderr or ''
    blob = f'{err}\n{stdout or ""}'
    low = blob.lower()
    if 'TimeoutExpired' in err:
        return ('timeout',
                f'probe hung > {PROBE_TIMEOUT_SEC}s and was killed by timeout',
                _ENV_HINT)
    if exit_code in (-9, 137) or 'MemoryError' in blob or 'Killed' in err:
        return ('killed',
                'probe process was killed (out-of-memory likely)',
                _ENV_HINT)
    if ('FileNotFoundError' in err or 'No such file' in err
            or 'binary' in low):
        return ('binary_missing',
                'claude binary not found or not executable',
                _ENV_HINT)
    if ('401' in blob or 'invalid authentication' in low
            or 'failed to authenticate' in low):
        return ('auth_401', 'API rejected the credentials (HTTP 401)',
                _AUTH_HINT)
    return ('other', f'unclassified failure (exit={exit_code})', _GENERIC_HINT)


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
    except OSError as e:
        log(f'heartbeat write failed: {e}', 'WARN')


def load_haiku_model_id() -> str:
    """Read the Haiku model id from config/agent-models.json:models.haiku.
    Fail safe: on missing block / parse error, return the pinned default
    and WARN so misconfig is visible."""
    try:
        with open(_CONFIG_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f'config read failed ({type(e).__name__}: {e}) — '
            f'using default {DEFAULT_HAIKU_MODEL_ID}', 'WARN')
        return DEFAULT_HAIKU_MODEL_ID
    models = data.get('models')
    if not isinstance(models, dict):
        log(f'config missing models block — using default '
            f'{DEFAULT_HAIKU_MODEL_ID}', 'WARN')
        return DEFAULT_HAIKU_MODEL_ID
    haiku = models.get('haiku')
    if not isinstance(haiku, dict):
        log(f'config missing models.haiku entry — using default '
            f'{DEFAULT_HAIKU_MODEL_ID}', 'WARN')
        return DEFAULT_HAIKU_MODEL_ID
    model_id = haiku.get('model_id')
    if not isinstance(model_id, str) or not model_id:
        log(f'config models.haiku.model_id invalid ({model_id!r}) — '
            f'using default {DEFAULT_HAIKU_MODEL_ID}', 'WARN')
        return DEFAULT_HAIKU_MODEL_ID
    return model_id


def _parse_ts(ts_str: str) -> Optional[datetime]:
    if not isinstance(ts_str, str):
        return None
    s = ts_str.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f'state corrupted ({type(e).__name__}: {e}) — '
            f'treating as empty + writing fresh', 'WARN')
        save_state({})
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix('.json.tmp')
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        tmp.rename(STATE_FILE)
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _in_dm_cooldown(state: dict, now: datetime) -> bool:
    last = state.get('last_dm_ts')
    if not isinstance(last, str):
        return False
    last_dt = _parse_ts(last)
    if last_dt is None:
        return False
    return (now - last_dt) < timedelta(hours=DM_COOLDOWN_HOURS)


def run_probe(model_id: str) -> tuple[bool, str, str, int]:
    """Invoke `claude -p PROBE_PROMPT` against the Tier 2 account.
    Returns (success_bool, stdout, stderr, exit_code).

    success_bool is True iff: exit code 0 AND stdout contains
    EXPECTED_TOKEN AND (if JSON-parseable) is_error is not true.

    Auth mirrors the dispatch path (agent_runner._apply_tier_auth): when the
    Tier 2 long-lived setup-token is configured, authenticate via it so the
    probe exercises the SAME auth source a real dispatch uses. The HOME-swap
    to TIER2_HOME stays (session/project dirs live under HOME/.claude) but
    auth no longer depends on the auto-refreshing .credentials.json there —
    intentionally not refreshed once the setup-token became primary, so a
    HOME-swap-only probe would always 401 and emit a FALSE "Tier 2 down"
    signal. Only when no setup-token is configured do we fall back to the
    legacy HOME-swap->.credentials.json path. The token value is never
    logged."""
    env = {**os.environ, 'HOME': TIER2_HOME}
    setup_token = active_tier._setup_token_for_tier('tier2')
    if setup_token:
        env['CLAUDE_CODE_OAUTH_TOKEN'] = setup_token
    cmd = [
        CLAUDE_BIN, '--print', '--output-format', 'json',
        '--model', model_id, PROBE_PROMPT,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=PROBE_TIMEOUT_SEC, env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, '', f'subprocess failed: {type(e).__name__}: {e}', -1
    except Exception as e:  # pragma: no cover — defensive
        return False, '', f'unexpected: {type(e).__name__}: {e}', -1
    stdout = proc.stdout or ''
    stderr = proc.stderr or ''
    if proc.returncode != 0:
        return False, stdout, stderr, proc.returncode
    # JSON path — check is_error and extract `result` field.
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            if data.get('is_error') is True:
                return False, stdout, stderr, proc.returncode
            result_text = data.get('result') or data.get('text') or ''
            if EXPECTED_TOKEN in str(result_text):
                return True, stdout, stderr, proc.returncode
    except json.JSONDecodeError:
        pass
    # Plaintext fallback — token-in-stdout.
    if EXPECTED_TOKEN in stdout:
        return True, stdout, stderr, proc.returncode
    return False, stdout, stderr, proc.returncode


# -------------------- Tier-2 provisioning parity --------------------
# The OAuth probe above proves Tier-2 can authenticate. These read-only
# checks prove a Tier-2 *session* would have the same CAPABILITY surface as
# Tier-1 — the gap class that #755 (path resolution) and the 2026-06
# ReadWritePaths fix each closed one facet of. Silent drift here means a
# rotation to Tier-2 leaves an agent degraded (missing MCP tools, or
# transcripts hitting EROFS). See docs/runbooks/
# tier1-tier2-switch-capability-audit.md for the full surface map.
SESSION_UNITS = (
    'ourliberty-beacon-bot.service',
    'ourliberty-forge-bot.service',
    'ourliberty-mirror-bot.service',
    'ourliberty-pulse-bot.service',
    'ourliberty-inbox-watcher.service',
)
REPO_SYSTEMD = Path(__file__).resolve().parent.parent / 'systemd'


def _mcp_servers(home: str) -> set:
    """MCP server names defined in <home>/.claude.json. Best-effort: an
    unreadable/absent file yields an empty set (a false negative, never a
    false drift alarm)."""
    try:
        data = json.loads((Path(home) / '.claude.json').read_text())
        return set(data.get('mcpServers', {}).keys())
    except Exception:
        return set()


def check_provisioning_parity() -> list:
    """Return a list of human-readable Tier-2 capability-drift findings
    (empty list == at parity). Never raises — pure read-only inspection so a
    bug here can never break the OAuth probe.

    OBSOLETE UNDER THE PER-TASK TIER POOL (docs/specs/tier-dispatch-spec.md):
    when Tier-2 has a long-lived SETUP TOKEN, a Tier-2 dispatch authenticates
    via that token and — under the decoupled auth<->HOME model (#760) — runs
    with HOME = TIER1_HOME, NOT TIER2_HOME. So it uses Tier-1's ~/.claude.json
    (MCP servers), Tier-1's credentials, and the already-carved real home (no
    EROFS). Every facet this function checks (TIER2_HOME MCP parity, TIER2_HOME
    creds, TIER2_HOME in ReadWritePaths) only matters on the creds.json fallback
    path, which is unreachable while the token is configured. So skip the check
    — return no drift — when the token is present; that retires the weekly false
    SOON alert the time-sliced-rotation model produced. (Tier-2 credential
    health is still covered by the OAuth liveness probe above; a MISSING token
    is a credential-drift concern, not a provisioning-parity one.) The check
    still runs for the no-token creds-fallback case, where parity applies."""
    if active_tier._setup_token_for_tier('tier2'):
        return []
    drift = []
    # 1. Tier-2 OAuth credentials present at all.
    cred = Path(TIER2_HOME) / '.claude' / '.credentials.json'
    if not cred.exists():
        drift.append(f'Tier-2 OAuth credentials missing at {cred}')
    # 2. MCP parity — every Tier-1 MCP server must also be defined for Tier-2,
    #    else a Tier-2 session silently loses those tools (e.g. workspace-mcp).
    t1 = _mcp_servers(active_tier.TIER1_HOME)
    t2 = _mcp_servers(TIER2_HOME)
    missing = sorted(t1 - t2)
    if missing:
        drift.append(
            f'Tier-2 ~/.claude.json is missing MCP server(s) present on '
            f'Tier-1: {missing} — a Tier-2 session loses those tools')
    # 3. Sandbox regression guard — every session-running unit must keep
    #    TIER2_HOME in ReadWritePaths, or Tier-2 transcripts hit EROFS and
    #    --resume silently fails (the 2026-06 session-loss root cause).
    for unit in SESSION_UNITS:
        f = REPO_SYSTEMD / unit
        try:
            text = f.read_text()
        except Exception:
            drift.append(f'{unit}: unreadable in repo systemd/ ({f})')
            continue
        rwp = [ln for ln in text.splitlines()
               if ln.strip().startswith('ReadWritePaths=')]
        if not any(TIER2_HOME in ln for ln in rwp):
            drift.append(
                f'{unit}: TIER2_HOME not in ReadWritePaths — Tier-2 '
                f'transcripts would hit EROFS and --resume would fail')
    return drift


def alert_parity_drift(drift: list) -> None:
    """Emit a single deduped alert (own subject -> own cooldown) when Tier-2
    has drifted from Tier-1 capability parity."""
    larry_alerts.append_alert(
        source='heal-tier2-weekly-probe',
        severity='warning',
        message=(
            'Tier-2 is not at capability parity with Tier-1 — a session that '
            'rotates to Tier-2 would be degraded:\n- ' + '\n- '.join(drift)
        ),
        subject='tier2_provisioning_drift',
        suggested_action=(
            'Bring Tier-2 to parity (runbook: docs/runbooks/'
            'tier1-tier2-switch-capability-audit.md). MCP gap: define the '
            'server in a project .mcp.json or authenticate it under '
            'TIER2_HOME. ReadWritePaths gap: add TIER2_HOME to the unit and '
            'reinstall via the install-drift healer.'
        ),
    )


def _retract_probe_standdown() -> int:
    """Positive-clear retraction: the probe just returned PROBE_OK, so Tier-2
    OAuth liveness recovered. Retract any stale 🔴 this healer emitted for the
    probe-failed subject and emit one closure stand-down (auditable, never
    silent), keyed on this healer's own `source:subject`. Best-effort — swallow
    any error so it can never break the probe tick."""
    try:
        removed = larry_alerts.retract_with_standdown(
            f'{PROBE_ALERT_SOURCE}:{PROBE_ALERT_SUBJECT}',
            standdown_message=(
                'Tier-2 weekly probe returned PROBE_OK again — OAuth liveness '
                'has recovered. Standing down the earlier probe-failed alert.'
            ),
        )
        if removed:
            log(f'retracted {removed} stale tier2-probe alert line(s) '
                f'(probe healthy again)', 'INFO')
        return removed
    except Exception as e:  # noqa: BLE001
        log(f'_retract_probe_standdown failed: {type(e).__name__}: {e}', 'WARN')
        return 0


def run() -> int:
    if KILL_SWITCH.exists():
        log('kill switch present — exiting', 'INFO')
        return 0
    heartbeat()
    # Capability-parity check (cheap, read-only) runs every invocation,
    # independent of the OAuth probe's cooldown. Isolated so a bug here can
    # never break the OAuth probe.
    try:
        drift = check_provisioning_parity()
    except Exception as e:  # noqa: BLE001 — must never break the OAuth probe
        drift = []
        log(f'parity check errored (non-fatal): {type(e).__name__}: {e}',
            'WARN')
    if drift:
        log('TIER2_PROVISIONING_DRIFT: ' + ' | '.join(drift), 'WARN')
        alert_parity_drift(drift)
    else:
        log('TIER2_PROVISIONING_PARITY_OK', 'INFO')
    model_id = load_haiku_model_id()
    log(f'starting probe model={model_id} home={TIER2_HOME}', 'INFO')
    ok, stdout, stderr, exit_code = run_probe(model_id)
    if ok:
        log(f'TIER2_WEEKLY_PROBE_OK model={model_id}', 'INFO')
        # POSITIVE-CLEAR ONLY: retract our own probe-failed red solely on a
        # positively-successful probe (`ok is True` — PROBE_OK observed with
        # exit 0). Every degraded read (timeout / OOM / 401 / missing binary)
        # returns ok=False and falls through to the alert branch below, so a
        # degraded probe can never reach this retract.
        _retract_probe_standdown()
        return 0
    reason, detail, suggested = classify_failure(stdout, stderr, exit_code)
    log(f'TIER2_WEEKLY_PROBE_FAILED model={model_id} reason={reason} '
        f'exit={exit_code} stdout={stdout[:300]!r} stderr={stderr[:300]!r}',
        'WARN')
    now = datetime.now(timezone.utc)
    state = load_state()
    if _in_dm_cooldown(state, now):
        log(f'probe failed but within DM cooldown '
            f'(last_dm_ts={state.get("last_dm_ts")}) — suppressing', 'INFO')
        return 0
    # Lead with the classified reason so the operator sees timeout/OOM vs 401
    # at a glance — "Output: ''" alone sent the 2026-06-03 triage down a
    # credential-rotation path for a failure that was really the probe's
    # memory cap. Prefer stderr in the snippet (timeouts carry no stdout).
    snippet = (stderr or stdout or '')[:200]
    body = (
        f'Tier 2 weekly probe failed [{reason}]: {detail} '
        f'(exit={exit_code}). Detail: {snippet!r}. {suggested}'
    )
    ok = larry_alerts.append_alert(
        source=PROBE_ALERT_SOURCE,
        severity='warning',
        message=body,
        subject=PROBE_ALERT_SUBJECT,
        suggested_action=suggested,
    )
    if ok:
        state['last_dm_ts'] = now.isoformat()
        save_state(state)
        log('alerted: tier2_weekly_probe_failed', 'INFO')
    else:
        log('larry_alerts append failed (cooldown or write error)', 'WARN')
    return 0


if __name__ == '__main__':
    sys.exit(run())
