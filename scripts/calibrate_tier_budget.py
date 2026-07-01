#!/usr/bin/env python3
"""calibrate_tier_budget.py — self-tuning per-tier 5h token budget (spec § 12b).

The per-task tier pool (docs/specs/tier-dispatch-spec.md) gates a primary on a
proactive cap keyed off ``max_5h_budget_tokens`` — an account's true 5h ceiling,
which is undocumented and plan-dependent. This periodic job LEARNS it from
reality instead of a hand-set constant:

  * Scan the rate-limit ledger (``blackboard/anthropic-quota-events.jsonl``) for
    ``failure_class == 'rate_limit'`` walls per tier over a rolling window
    (default 14d).
  * For each wall, compute that account's rolling-5h burn AT WALL-TIME from
    costs.jsonl (``account==t``-filtered via active_tier.rolling_5h_token_volume)
    — that burn ≈ the account's true 5h ceiling at that moment.
  * Set the budget = ``0.90 × the LOWEST observed wall-burn`` (conservative, so
    near_cap trips before the real ceiling), per tier, with a floor so a
    weekly-cap / anomalous wall can't drive it to near-zero.
  * Write tuned values to the RUNTIME OVERRIDE
    ``~/agents/state/tier-budget-calibration.json`` (atomic) that the selector
    reads at call time — so the pool adapts WITHOUT a code/config deploy.

Bootstrap: a tier with too few walls gets NO override (omitted from the file →
the selector falls back to the config default). When a previously-tightened
tier sees no walls over the window, its budget is LOOSENED slowly back toward
the config default. Idempotent, fail-safe, oneshot (systemd timer).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import active_tier  # noqa: E402 — the pool module owns the burn reader + paths

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'calibrate-tier-budget.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'calibrate-tier-budget.heartbeat'
RATE_LIMIT_LEDGER_FILE = AGENTS_ROOT / 'blackboard' / 'anthropic-quota-events.jsonl'

# Tuning knobs (spec § 12b defaults).
WINDOW_DAYS = 14
FRACTION = 0.90          # budget = 0.90 × lowest wall-burn
MIN_WALLS = 2            # bootstrap: need >= this many walls to (re)tighten a tier
LOOSEN_FACTOR = 1.05     # no-walls: raise the override slowly toward the default
# Safety floor: a rate-limit wall whose 5h burn is below this is almost
# certainly a weekly-cap / anomalous wall, not the 5h ceiling — never let it
# drive the budget to near-zero (which would hold the pool constantly).
MIN_BUDGET_FLOOR = 2_000_000


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


def _disabled() -> bool:
    """Honor the fleet-wide ``~/agents/healers.disabled`` kill switch."""
    return KILL_SWITCH.exists()


def _parse_ts(ts_str):
    """Parse an ISO-8601 ledger ts (optional trailing Z). None on failure."""
    if not isinstance(ts_str, str) or not ts_str:
        return None
    s = ts_str.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _calibrated_tiers():
    """The tiers to calibrate: the resolved pool primary + fallback (validity-
    filtered by active_tier._tier_pool_config)."""
    cfg = active_tier._tier_pool_config()
    seen = []
    for t in list(cfg['primary']) + list(cfg['fallback']):
        if t not in seen:
            seen.append(t)
    return seen, cfg


def read_walls(now=None):
    """Return ``{tier: [wall_ts_datetime, ...]}`` for ``failure_class ==
    'rate_limit'`` events within the trailing WINDOW_DAYS. Fail-open: missing /
    unreadable ledger or a malformed line yields no walls for that read."""
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)
    walls: dict[str, list] = {}
    if not RATE_LIMIT_LEDGER_FILE.exists():
        return walls
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
                if (rec.get('failure_class') or 'rate_limit') != 'rate_limit':
                    continue
                acct = rec.get('account')
                if not acct or acct in ('fixture', 'skipped'):
                    continue
                ts = _parse_ts(rec.get('ts', ''))
                if ts is None or ts < cutoff:
                    continue
                walls.setdefault(acct, []).append(ts)
    except OSError as e:
        log(f'read {RATE_LIMIT_LEDGER_FILE} failed: {e}', 'WARN')
        return {}
    return walls


def compute_budgets(now=None):
    """Compute the new calibration override ``{tier: budget_int}``.

    Per tier:
      * >= MIN_WALLS walls with positive wall-burns  -> TIGHTEN to
        ``max(FLOOR, FRACTION × min(wall_burns))``.
      * 0 walls AND a prior override below the config default -> LOOSEN
        (``prev × LOOSEN_FACTOR`` capped at the default; dropped once it
        reaches the default).
      * otherwise -> keep the prior override (if any); a never-calibrated tier
        stays omitted (bootstrap -> selector uses the config default).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    tiers, cfg = _calibrated_tiers()
    default = cfg.get('max_5h_budget_tokens')
    default = int(default) if isinstance(default, (int, float)) and default > 0 else None
    prev = _read_override()
    walls = read_walls(now=now)
    out: dict[str, int] = {}
    for t in tiers:
        tier_walls = walls.get(t, [])
        burns = [b for b in
                 (active_tier.rolling_5h_token_volume(account=t, now=w)
                  for w in tier_walls)
                 if b > 0]
        if len(burns) >= MIN_WALLS:
            budget = max(MIN_BUDGET_FLOOR, int(FRACTION * min(burns)))
            out[t] = budget
            log(f'{t}: tightened to {budget:,} from {len(burns)} wall(s) '
                f'(lowest 5h wall-burn {min(burns):,})')
        elif not tier_walls and t in prev:
            loosened = int(prev[t] * LOOSEN_FACTOR)
            if default is not None and loosened >= default:
                log(f'{t}: loosened back to config default (dropping override)')
                continue  # omit -> selector uses the config default
            out[t] = loosened
            log(f'{t}: loosened {prev[t]:,} -> {loosened:,} (no recent walls)')
        elif t in prev:
            out[t] = prev[t]  # too few walls to change -> hold
    return out


def _override_path():
    return active_tier._calibration_path()


def _read_override():
    try:
        data = json.loads(_override_path().read_text())
        if isinstance(data, dict):
            return {k: int(v) for k, v in data.items()
                    if isinstance(v, (int, float)) and v > 0}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {}


def write_override(budgets: dict) -> None:
    """Atomically write the calibration override the selector reads at call
    time. An empty dict clears the override (all tiers back to config default)."""
    path = _override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(budgets, indent=2))
    tmp.replace(path)


def main() -> int:
    if _disabled():
        log('healers.disabled present; skipping calibration')
        return 0
    heartbeat()
    try:
        budgets = compute_budgets()
    except Exception as e:  # noqa: BLE001 — a calibration error must never wedge
        log(f'calibration failed: {type(e).__name__}: {e}', 'WARN')
        return 0
    try:
        write_override(budgets)
    except OSError as e:
        log(f'override write failed: {e}', 'WARN')
        return 0
    if budgets:
        log('calibrated budgets: ' +
            ', '.join(f'{t}={b:,}' for t, b in sorted(budgets.items())))
    else:
        log('no per-tier calibration yet (insufficient wall data); '
            'selector uses config defaults')
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
