#!/usr/bin/env python3
"""heal_claude_max_burn_rate.py — early-warning healer for Tier 1 quota burn.

Reads the rolling 5h Tier 1 token-volume proxy from
`~/agents/blackboard/costs.jsonl` and DMs Larry once per window when the
volume crosses 80% of the configurable threshold in
`config/agent-models.json:tier1_quota.max_5h_token_threshold`.

Why token volume, not dollars (2026-05-28 re-base)
--------------------------------------------------
We run OAuth Max; there is no per-token billing. The `cost_usd` field in
costs.jsonl is imputed (tokens x API list price), not money spent. The
real constraint is Anthropic's rolling-5h Tier 1 rate-limit window, which
is metered in usage/quota, not dollars. Denominating the warning in
dollars (a) misframes the signal as money we are spending when we aren't,
and (b) false-alarmed every ~15 min on 2026-05-27 while real account
usage sat at 31% session / 59% weekly. Re-basing onto a quota-relevant
token-volume proxy fixes both. See docs/burn-rate-healer-rebase-brief.md.

Design fork — why approach #2 (token-volume proxy)
--------------------------------------------------
Approach #1 in the brief was a programmatic usage-% feed if `claude auth
status` exposed one headlessly. It does not (only loggedIn, authMethod,
subscriptionType, orgId — no session/weekly percentage). So this healer
falls back to approach #2: a trailing-5h sum of quota-consuming token
fields from costs.jsonl. The signal is leading (predicts the wall before
we hit it) and requires no LLM/paid-API call.

Which token fields count for quota
----------------------------------
`input_tokens + output_tokens + cache_creation` — the quota-consuming
tokens. `cache_read` is excluded: cached re-reads are the largely-free
re-use case and do not represent real quota burn against the 5h window.
Including them would inflate the signal by 10-100x and reproduce the
false-alarm shape we just removed.

Self-protection
---------------
This healer makes ZERO LLM subprocess calls. Pure file read + arithmetic.
That's the point — a quota healer that consumes quota would defeat itself.
Verifiable by grepping this file for `claude` / `subprocess`: no invocations.

Calibration delegated to Check VIII
-----------------------------------
The seed threshold is a starting point, not a magic number. The seed
(10,000,000 tokens) sits above the documented 2026-05-27 false-alarm
period's peak (9.7M tokens rolling-5h input+output+cache_creation) and
below historical peak burn (~14M). `scripts/pulse_check_viii.py` runs
weekly against `anthropic-quota-events.jsonl` and proposes
raise/lower/deprecate adjustments via the doctrine #48 precision/recall
loop. Don't hand-tune the seed here; let Check VIII tune.

V1 limitation (CLARIFY #1 from 2026-05-27)
------------------------------------------
costs.jsonl records currently have no `tier` / `account` field, so this
healer sums ALL token entries in the 5h window — Tier 1 PLUS any Tier 2
fallback. In steady state Tier 2 is fallback-only and the inflation is
marginal (conservative: we'd alert slightly early rather than late).
TODO(claude-quota-fixes-v3): add tier/account field to cost-writer for
precise per-account filtering, then update the sum here to filter.

State
-----
* Cursor: `~/agents/state/heal-claude-max-burn-rate-state.json` — records
  last-DM-fired-at timestamp. Once we DM, suppress for the cooldown
  window so a sustained high-burn period produces one DM, not 20.
* Heartbeat: `~/agents/blackboard/heal-claude-max-burn-rate.heartbeat`.
* Kill switch: `~/agents/healers.disabled` (shared with other healers).
* Log: `~/agents/logs/heal-claude-max-burn-rate.log`.
"""

from __future__ import annotations

import json
import os
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
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-claude-max-burn-rate.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-claude-max-burn-rate.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-claude-max-burn-rate-state.json'
COSTS_FILE = AGENTS_ROOT / 'blackboard' / 'costs.jsonl'
# Check VIII PR-2a: ground-truth rate-limit ledger written by agent_runner.
# The DM body's trailing-2h count is read from here. Missing file -> 0
# (first run after PR-2a merges, before any 429 has fired).
RATE_LIMIT_LEDGER_FILE = AGENTS_ROOT / 'blackboard' / 'anthropic-quota-events.jsonl'
RATE_LIMIT_RECENT_HOURS = 2

# Config defaults — overridden by config/agent-models.json:tier1_quota.
# Seed value: 10M quota-consuming tokens over 5h. Sits above the documented
# 2026-05-27 false-alarm period's peak rolling-5h volume (9.7M tokens) and
# below historical peak burn (~14M). Provisional — Check VIII tunes.
DEFAULT_MAX_5H_TOKEN_THRESHOLD = 10_000_000
WINDOW_HOURS = 5
ALERT_THRESHOLD_FRACTION = 0.80
# Cooldown between DMs. Aligned with the 5h window — once we fire on a
# burn period, we don't re-fire until that period has fully cycled. This
# is independent of (and longer than) larry_alerts.WARNING_COOLDOWN_SEC
# (1h) so a sustained burn produces one DM not five.
DM_COOLDOWN_HOURS = WINDOW_HOURS

_CONFIG_FILE = Path(__file__).resolve().parent.parent / 'config' / 'agent-models.json'

# Anthropic usage dashboard pointer — included in the DM body so Larry can
# verify the threshold seed against actual account state post-merge.
ANTHROPIC_USAGE_URL = 'https://console.anthropic.com/settings/usage'


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


def load_threshold() -> int:
    """Read the 5h token threshold from config/agent-models.json. Fail safe:
    on missing block / parse error / non-numeric value, return the default
    so the healer never wedges. WARN on every fallback so misconfig is
    visible in the log."""
    try:
        with open(_CONFIG_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f'config read failed ({type(e).__name__}: {e}) — '
            f'using default {DEFAULT_MAX_5H_TOKEN_THRESHOLD} tokens', 'WARN')
        return DEFAULT_MAX_5H_TOKEN_THRESHOLD
    block = data.get('tier1_quota')
    if not isinstance(block, dict):
        log(f'config missing tier1_quota block — using default '
            f'{DEFAULT_MAX_5H_TOKEN_THRESHOLD} tokens', 'WARN')
        return DEFAULT_MAX_5H_TOKEN_THRESHOLD
    raw = block.get('max_5h_token_threshold')
    if not isinstance(raw, (int, float)) or raw <= 0:
        log(f'config tier1_quota.max_5h_token_threshold invalid '
            f'({raw!r}) — using default '
            f'{DEFAULT_MAX_5H_TOKEN_THRESHOLD} tokens', 'WARN')
        return DEFAULT_MAX_5H_TOKEN_THRESHOLD
    return int(raw)


def gate_enabled() -> bool:
    """Whether the burn-rate gate is active. Reads tier1_quota.enabled from
    config/agent-models.json. ABSENT key -> True (enabled) for back-compat: an
    accidentally-deleted field must NOT silently blind monitoring. Only an
    EXPLICIT `false` deprecates the gate (Check VIII 2026-06-29: zero
    predictive value). Unreadable config / non-bool value -> True (fail safe,
    same intent as load_threshold)."""
    try:
        with open(_CONFIG_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return True
    block = data.get('tier1_quota')
    if not isinstance(block, dict):
        return True
    return block.get('enabled', True) is not False


def _parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse the costs.jsonl ts shape — ISO 8601 with optional Z/tz."""
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


def _record_tokens(rec: dict) -> int:
    """Quota-consuming token total for one costs.jsonl record.

    input_tokens + output_tokens + cache_creation. cache_read is excluded —
    it's the largely-free cached re-read and is not the right quota signal.
    Non-numeric fields are treated as 0 so malformed records don't crash
    the healer (degrade-gracefully — costs.jsonl is appended by many
    writers; the schema can drift).
    """
    total = 0
    for key in ('input_tokens', 'output_tokens', 'cache_creation'):
        v = rec.get(key, 0)
        if isinstance(v, (int, float)) and v > 0:
            total += int(v)
    return total


def rolling_5h_token_volume(now: Optional[datetime] = None) -> int:
    """Sum quota-consuming tokens (input + output + cache_creation) across
    costs.jsonl entries whose ts is within the trailing WINDOW_HOURS of
    `now`. Tolerates missing file, malformed lines (skips them), and
    non-numeric fields (treats as 0). Account-agnostic — Tier 1 + Tier 2
    sum together because the record shape doesn't distinguish (V1
    limitation, see module docstring).
    """
    if not COSTS_FILE.exists():
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    total = 0
    try:
        with open(COSTS_FILE, errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_ts(rec.get('ts', ''))
                if ts is None or ts < cutoff:
                    continue
                total += _record_tokens(rec)
    except OSError as e:
        log(f'read {COSTS_FILE} failed: {e}', 'WARN')
        return 0
    return total


def recent_rate_limit_event_count(now: Optional[datetime] = None) -> int:
    """Count rate-limit events from the ledger within the trailing
    RATE_LIMIT_RECENT_HOURS. Missing file or unreadable lines -> 0 without
    error; the ledger is purely observational and may not exist on first run.
    """
    if not RATE_LIMIT_LEDGER_FILE.exists():
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=RATE_LIMIT_RECENT_HOURS)
    count = 0
    try:
        with open(RATE_LIMIT_LEDGER_FILE, errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_ts(rec.get('ts', ''))
                if ts is None or ts < cutoff:
                    continue
                count += 1
    except OSError as e:
        log(f'read {RATE_LIMIT_LEDGER_FILE} failed: {e}', 'WARN')
        return 0
    return count


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
    """Atomic tmp+rename write."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix('.json.tmp')
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        tmp.rename(STATE_FILE)
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _in_dm_cooldown(state: dict, now: datetime) -> bool:
    """True if we DM'd within the last DM_COOLDOWN_HOURS — window-dedup
    layer (independent of larry_alerts' per-source 1h warning cooldown,
    which is shorter than our 5h window)."""
    last = state.get('last_dm_ts')
    if not isinstance(last, str):
        return False
    last_dt = _parse_ts(last)
    if last_dt is None:
        return False
    return (now - last_dt) < timedelta(hours=DM_COOLDOWN_HOURS)


def run() -> int:
    if KILL_SWITCH.exists():
        log('kill switch present — exiting', 'INFO')
        return 0
    heartbeat()
    if not gate_enabled():
        log('burn-rate gate disabled via config '
            '(tier1_quota.enabled=false) — skipping', 'INFO')
        return 0
    now = datetime.now(timezone.utc)
    threshold = load_threshold()
    usage = rolling_5h_token_volume(now=now)
    pct = (usage / threshold) if threshold > 0 else 0.0
    log(f'rolling {WINDOW_HOURS}h tokens={usage} '
        f'threshold={threshold} pct={pct * 100:.1f}%', 'INFO')
    if pct < ALERT_THRESHOLD_FRACTION:
        return 0
    state = load_state()
    if _in_dm_cooldown(state, now):
        log(f'usage at {pct * 100:.1f}% but within DM cooldown '
            f'(last_dm_ts={state.get("last_dm_ts")}) — suppressing', 'INFO')
        return 0
    recent_events = recent_rate_limit_event_count(now=now)
    body = (
        f'Trailing {WINDOW_HOURS}h quota pace at {pct * 100:.0f}% of token gate '
        f'({usage:,} of {threshold:,} tokens; input+output+cache_creation). '
        f'Pace indicator only — for actual session/weekly quota state, check '
        f'{ANTHROPIC_USAGE_URL}. '
        f'Recent rate-limit events (trailing {RATE_LIMIT_RECENT_HOURS}h): '
        f'{recent_events}.'
    )
    ok = larry_alerts.append_alert(
        source='heal-claude-max-burn-rate',
        severity='warning',
        message=body,
        subject='claude_max_5h_burn_threshold_breached',
        suggested_action=(
            f'Inspect recent token usage: '
            f'`tail -50 ~/agents/blackboard/costs.jsonl | jq -r \'[.ts, .agent, .input_tokens, .output_tokens, .cache_creation] | @tsv\'`. '
            f'Verify against the Anthropic usage page. If sustained burn, '
            f'pause dispatches or wait for the rolling {WINDOW_HOURS}h '
            f'window to clear.'
        ),
    )
    if ok:
        state['last_dm_ts'] = now.isoformat()
        save_state(state)
        log(f'alerted: usage at {pct * 100:.1f}% of threshold', 'INFO')
    else:
        log('larry_alerts append failed (cooldown or write error)', 'WARN')
    return 0


if __name__ == '__main__':
    sys.exit(run())
