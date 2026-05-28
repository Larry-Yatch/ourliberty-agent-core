#!/usr/bin/env python3
"""heal_claude_max_burn_rate.py — early-warning healer for Tier 1 quota burn.

Reads the rolling 5h Tier 1 spend from `~/agents/blackboard/costs.jsonl` and
DMs Larry once per window when spend crosses 80% of the configurable
threshold in `config/agent-models.json:tier1_quota.max_5h_spend_threshold_usd`.

Motivation
----------
2026-05-26/27 incident: a fixture cascade burned Tier 1 quota silently
through the night. Larry only noticed when Beacon hit the rate-limit wall
the next morning. With this healer firing at the 80% mark, he'd have
gotten a DM 1-2h before the wall, giving him time to pause dispatches or
provision Tier 2.

Self-protection
---------------
This healer makes ZERO LLM subprocess calls. Pure file read + arithmetic.
That's the point — a quota healer that consumes quota would defeat itself.
Verifiable by grepping this file for `claude` / `subprocess`: no invocations.

V1 limitation (CLARIFY #1 from build-dispatch, 2026-05-27)
----------------------------------------------------------
costs.jsonl records currently have no `tier` / `account` / HOME field, so
this healer sums ALL cost entries in the 5h window — Tier 1 PLUS any
Tier 2 fallback spend. In steady state Tier 2 is fallback-only and the
inflation is marginal (and conservative: we'd alert slightly early rather
than late, which is the safer direction for a quota-burn warning).
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

Phase E4 followup, 2026-05-27 — claude-quota-fixes-v2 bundle.
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
# The DM body's trailing-2h count is read from here. Missing file → 0
# (first run after PR-2a merges, before any 429 has fired).
RATE_LIMIT_LEDGER_FILE = AGENTS_ROOT / 'blackboard' / 'anthropic-quota-events.jsonl'
RATE_LIMIT_RECENT_HOURS = 2

# Config defaults — overridden by config/agent-models.json:tier1_quota.
DEFAULT_MAX_5H_SPEND_THRESHOLD_USD = 60.0
WINDOW_HOURS = 5
ALERT_THRESHOLD_FRACTION = 0.80
# Cooldown between DMs. Aligned with the 5h window — once we fire on a
# burn period, we don't re-fire until that period has fully cycled. This
# is independent of (and longer than) larry_alerts.WARNING_COOLDOWN_SEC
# (1h) so a sustained burn produces one DM not five.
DM_COOLDOWN_HOURS = WINDOW_HOURS

_CONFIG_FILE = Path(__file__).resolve().parent.parent / 'config' / 'agent-models.json'

# Anthropic usage dashboard pointer — included in the DM body so Larry can
# verify the threshold default against actual account state post-merge.
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


def load_threshold() -> float:
    """Read the 5h threshold from config/agent-models.json. Fail safe: on
    missing block / parse error / non-numeric value, return the default
    so the healer never wedges. WARN on every fallback so misconfig is
    visible in the log."""
    try:
        with open(_CONFIG_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f'config read failed ({type(e).__name__}: {e}) — '
            f'using default ${DEFAULT_MAX_5H_SPEND_THRESHOLD_USD}', 'WARN')
        return DEFAULT_MAX_5H_SPEND_THRESHOLD_USD
    block = data.get('tier1_quota')
    if not isinstance(block, dict):
        log(f'config missing tier1_quota block — using default '
            f'${DEFAULT_MAX_5H_SPEND_THRESHOLD_USD}', 'WARN')
        return DEFAULT_MAX_5H_SPEND_THRESHOLD_USD
    raw = block.get('max_5h_spend_threshold_usd')
    if not isinstance(raw, (int, float)) or raw <= 0:
        log(f'config tier1_quota.max_5h_spend_threshold_usd invalid '
            f'({raw!r}) — using default '
            f'${DEFAULT_MAX_5H_SPEND_THRESHOLD_USD}', 'WARN')
        return DEFAULT_MAX_5H_SPEND_THRESHOLD_USD
    return float(raw)


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


def rolling_5h_spend(now: Optional[datetime] = None) -> float:
    """Sum `cost_usd` across all costs.jsonl entries whose ts is within the
    trailing WINDOW_HOURS of `now`. Tolerates missing file, malformed lines
    (skips them), and non-numeric cost values (skips them). See module
    docstring's V1 limitation block: this is account-agnostic — Tier 1
    + Tier 2 sum together because the record shape doesn't distinguish.
    """
    if not COSTS_FILE.exists():
        return 0.0
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    total = 0.0
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
                cost = rec.get('cost_usd')
                if not isinstance(cost, (int, float)):
                    continue
                total += float(cost)
    except OSError as e:
        log(f'read {COSTS_FILE} failed: {e}', 'WARN')
        return 0.0
    return total


def recent_rate_limit_event_count(now: Optional[datetime] = None) -> int:
    """Count rate-limit events from the ledger within the trailing
    RATE_LIMIT_RECENT_HOURS. Missing file or unreadable lines → 0 without
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
    now = datetime.now(timezone.utc)
    threshold = load_threshold()
    spend = rolling_5h_spend(now=now)
    pct = (spend / threshold) if threshold > 0 else 0.0
    log(f'rolling {WINDOW_HOURS}h spend=${spend:.2f} '
        f'threshold=${threshold:.2f} pct={pct * 100:.1f}%', 'INFO')
    if pct < ALERT_THRESHOLD_FRACTION:
        return 0
    state = load_state()
    if _in_dm_cooldown(state, now):
        log(f'spend at {pct * 100:.1f}% but within DM cooldown '
            f'(last_dm_ts={state.get("last_dm_ts")}) — suppressing', 'INFO')
        return 0
    recent_events = recent_rate_limit_event_count(now=now)
    body = (
        f'Trailing {WINDOW_HOURS}h LLM pace at {pct * 100:.0f}% of dollar gate '
        f'(${spend:.2f} of ${threshold:.2f}). '
        f'Pace indicator only — for actual quota state, check '
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
            f'Inspect recent cost entries: '
            f'`tail -50 ~/agents/blackboard/costs.jsonl | jq -r \'[.ts, .agent, .cost_usd] | @tsv\'`. '
            f'Verify against the Anthropic usage page. If sustained burn, '
            f'pause dispatches or wait for the rolling {WINDOW_HOURS}h '
            f'window to clear.'
        ),
    )
    if ok:
        state['last_dm_ts'] = now.isoformat()
        save_state(state)
        log(f'alerted: spend at {pct * 100:.1f}% of threshold', 'INFO')
    else:
        log('larry_alerts append failed (cooldown or write error)', 'WARN')
    return 0


if __name__ == '__main__':
    sys.exit(run())
