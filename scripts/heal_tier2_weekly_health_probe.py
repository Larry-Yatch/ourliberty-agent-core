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

_CONFIG_FILE = Path(__file__).resolve().parent.parent / 'config' / 'agent-models.json'

# Haiku is the cheapest viable Claude model — probe is intentionally
# minimal so it costs ~$0.001 per run. Pinned default in case config is
# missing the models.haiku entry; the registry is the source of truth
# when present.
DEFAULT_HAIKU_MODEL_ID = 'claude-haiku-4-5-20251001'

RUNBOOK_HINT = (
    'Run docs/runbooks/restore-larry-personal-claude-oauth-tier2.md to re-auth.'
)


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
    EXPECTED_TOKEN AND (if JSON-parseable) is_error is not true."""
    env = {**os.environ, 'HOME': TIER2_HOME}
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


def run() -> int:
    if KILL_SWITCH.exists():
        log('kill switch present — exiting', 'INFO')
        return 0
    heartbeat()
    model_id = load_haiku_model_id()
    log(f'starting probe model={model_id} home={TIER2_HOME}', 'INFO')
    ok, stdout, stderr, exit_code = run_probe(model_id)
    if ok:
        log(f'TIER2_WEEKLY_PROBE_OK model={model_id}', 'INFO')
        return 0
    log(f'TIER2_WEEKLY_PROBE_FAILED model={model_id} exit={exit_code} '
        f'stdout={stdout[:300]!r} stderr={stderr[:300]!r}', 'WARN')
    now = datetime.now(timezone.utc)
    state = load_state()
    if _in_dm_cooldown(state, now):
        log(f'probe failed but within DM cooldown '
            f'(last_dm_ts={state.get("last_dm_ts")}) — suppressing', 'INFO')
        return 0
    body = (
        f'Tier 2 weekly probe failed. Output: {stdout[:200]!r}. '
        f'{RUNBOOK_HINT}'
    )
    ok = larry_alerts.append_alert(
        source='heal-tier2-weekly-probe',
        severity='warning',
        message=body,
        subject='tier2_weekly_probe_failed',
        suggested_action=(
            f'Re-auth Tier 2: see '
            f'docs/runbooks/restore-larry-personal-claude-oauth-tier2.md. '
            f'Manual re-probe: '
            f'`HOME={TIER2_HOME} claude -p --model {model_id} \'{PROBE_PROMPT}\'`.'
        ),
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
