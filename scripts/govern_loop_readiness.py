#!/usr/bin/env python3
"""govern_loop_readiness.py — the trip-wire that tells Larry WHEN slice 7 (the
govern-loop assessor) is worth building, and nudges until he kicks it.

WHY THIS EXISTS
---------------
The assessor spec (agents/beacon/specs/govern-loop-assessor.md, approved
2026-07-08 as decision govern-loop-assessor-spec-001) names its own blocking
risk: "rankings are not trustworthy until volume accrues. To resolve: soak.
Owner: time." Nothing owns "time" — without a watcher, kicking the build
depends on Larry (or a session) remembering to check the ledger. This module
gives "time" an owner: a daily pass over the decision-outcome ledger that
DMs Larry exactly once when the data is thick enough, then re-nudges weekly
until the build is kicked (park-don't-decay: nudges never self-suppress).

READINESS = all three, tunable via the module constants:
  - >= MIN_DECISIONS decision rows with a non-empty decision_key
    (empty keys can never join a build outcome; the spec excludes them)
  - >= MIN_JOINED distinct decision_keys carrying a build_outcome row
    (the assessor learns from the JOIN, not from raw clicks)
  - >= MIN_SPAN_DAYS between the oldest row and now (Larry's own prove-bar:
    ~2 weeks of history before trusting a judgment loop)

THE KICK is scripts/kick_govern_loop_assessor.sh — it dispatches the
pre-authored build envelope to Beacon (the sanctioned source=larry headless
path) and stamps `kicked` into this module's state file, which silences the
nudge permanently. The DM carries that exact command.

Writes ONLY its own state file (~/agents/state/govern-loop-kickoff.json,
atomic) + log; read-only over the ledger. The alert goes through
larry_alerts.append_alert (route=escalate -> Larry's chat + needs-you feed),
injected as a seam so tests never touch the real alert pipeline. Stdlib only;
never raises from run_once (malformed ledger -> not-ready, logged).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

HOME = Path(os.environ.get('HOME', '/home/larry'))
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
LEDGER_FILE = AGENTS_ROOT / 'state' / 'decision-outcome-ledger.jsonl'
STATE_FILE = AGENTS_ROOT / 'state' / 'govern-loop-kickoff.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'govern-loop-readiness.log'

# The assessor script itself, in this repo tree — the durable 'already built'
# signal (see _assessor_built).
ASSESSOR_SCRIPT = Path(__file__).resolve().parent / 'govern_loop_assessor.py'

# Readiness thresholds. Rationale: the assessor buckets by area and scores the
# decision->build_outcome JOIN; below ~15 joined keys every bucket is a
# coin-flip, and Larry's standing prove-bar for judgment loops is ~2 weeks of
# real history. Tune here, one place.
MIN_DECISIONS = 30
MIN_JOINED = 15
MIN_SPAN_DAYS = 14

# Nudge cadence once ready: first DM immediately, then weekly until kicked.
# Never auto-suppressed (park-don't-decay) — only the kick stamp silences it.
RENUDGE_SEC = 7 * 86400

KICK_COMMAND = ("ssh -i ~/.ssh/id_ed25519 larry@134.209.44.80 "
                "'bash ~/agent-core/scripts/kick_govern_loop_assessor.sh'")


def _log(level: str, msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {level} {msg}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        print(line, file=sys.stderr)


def _parse_ts(v: Any) -> Optional[float]:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def read_ledger_rows(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Every parseable JSONL row; bad lines skipped, missing file -> []."""
    rows: list[dict[str, Any]] = []
    try:
        with open(path or LEDGER_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def measure(rows: list[dict[str, Any]],
            now_ts: Optional[float] = None) -> dict[str, Any]:
    """The three readiness numbers + verdict, from raw ledger rows.

    Row taxonomy mirrors the ledger module: `kind` == 'build_outcome' marks a
    reconciler join; anything else (kind == 'decision' or the early rows
    written before `kind` existed) is a decision row.
    """
    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    decisions = 0
    joined_keys: set[str] = set()
    oldest: Optional[float] = None
    for row in rows:
        ts = _parse_ts(row.get('ts'))
        if ts is not None:
            oldest = ts if oldest is None else min(oldest, ts)
        key = row.get('decision_key')
        has_key = isinstance(key, str) and key != ''
        if row.get('kind') == 'build_outcome':
            if has_key:
                joined_keys.add(key)
        elif has_key:
            decisions += 1
    span_days = ((now - oldest) / 86400.0) if oldest is not None else 0.0
    ready = (decisions >= MIN_DECISIONS
             and len(joined_keys) >= MIN_JOINED
             and span_days >= MIN_SPAN_DAYS)
    return {
        'decisions': decisions,
        'joined': len(joined_keys),
        'span_days': round(span_days, 1),
        'thresholds': {'decisions': MIN_DECISIONS, 'joined': MIN_JOINED,
                       'span_days': MIN_SPAN_DAYS},
        'ready': ready,
    }


def _assessor_built() -> bool:
    """True once slice 7 (the assessor) is shipped into the repo tree.

    The nudge normally self-silences via the kick script's `kicked` stamp, but
    the assessor can ship OUTSIDE the kick path — PR #984 merged
    scripts/govern_loop_assessor.py directly, leaving no stamp — so the nudge
    would otherwise fire forever for a thing already built. The assessor
    script's presence is the repo-local 'already built' signal that closes that
    gap regardless of how it shipped. Seam for tests (monkeypatch this name).
    """
    return ASSESSOR_SCRIPT.exists()


def _read_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict[str, Any]) -> None:
    # Re-read right before writing: the kick script may have stamped `kicked`
    # into this same file between our read and now — a plain read-modify-write
    # would erase the stamp and the nudges would resume after the kick.
    # Preserve any stamp we don't already carry. (Tmp name is pid-unique so a
    # concurrent kick-script write can't cross-contaminate the temp file.)
    if not state.get('kicked'):
        fresh = _read_state()
        if fresh.get('kicked'):
            state = dict(state, kicked=fresh['kicked'],
                         kicked_by=fresh.get('kicked_by'))
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(f'{STATE_FILE}.tmp.{os.getpid()}')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=1)
        tmp.replace(STATE_FILE)
    except OSError as e:
        _log('WARN', f'could not write state file: {e}')


def _default_alert() -> Optional[Callable[..., bool]]:
    """larry_alerts.append_alert, or None (alert skipped, WARN-logged)."""
    try:
        import larry_alerts
        return larry_alerts.append_alert
    except Exception:  # import must never take the pass down
        return None


def _nudge_message(m: dict[str, Any]) -> str:
    return (
        "The decision-outcome ledger is now thick enough for the govern-loop "
        f"assessor (operator slice 7) to learn from: {m['decisions']} decisions "
        f"(need {m['thresholds']['decisions']}), {m['joined']} joined with a "
        f"build outcome (need {m['thresholds']['joined']}), "
        f"{m['span_days']} days of history (need {m['thresholds']['span_days']}). "
        "The spec is already drafted and approved "
        "(agents/beacon/specs/govern-loop-assessor.md); the build is read-only "
        "shadow mode — it ranks where your approvals pay off, launches nothing. "
        f"One command kicks it hands-free through the team: {KICK_COMMAND} "
        "— or tell Claude 'kick slice 7'. This nudge repeats weekly until kicked."
    )


def run_once(now: Optional[datetime] = None,
             alert_fn: Optional[Callable[..., bool]] = None) -> dict[str, Any]:
    now_dt = now or datetime.now(timezone.utc)
    now_ts = now_dt.timestamp()
    state = _read_state()

    if _assessor_built():
        # Slice 7 is already in the tree (e.g. PR #984 merged it outside the
        # kick path, so no `kicked` stamp exists). Nudging to build a thing that
        # already exists is a false signal — short-circuit before any emit.
        _log('INFO', 'assessor already shipped; nudge silenced')
        return state

    if state.get('kicked'):
        # The build was dispatched — this watcher's job is done, forever.
        _log('INFO', 'already kicked; nothing to do')
        return state

    m = measure(read_ledger_rows(), now_ts=now_ts)
    state['progress'] = m
    state['as_of'] = now_dt.isoformat()

    if not m['ready']:
        _log('INFO', f"not ready: {m['decisions']}/{MIN_DECISIONS} decisions, "
                     f"{m['joined']}/{MIN_JOINED} joined, "
                     f"{m['span_days']}/{MIN_SPAN_DAYS} days")
        _write_state(state)
        return state

    if state.get('first_ready_ts') is None:
        state['first_ready_ts'] = now_dt.isoformat()

    last = _parse_ts(state.get('last_nudge_ts'))
    if last is not None and (now_ts - last) < RENUDGE_SEC:
        _write_state(state)
        return state

    fn = alert_fn if alert_fn is not None else _default_alert()
    if fn is None:
        _log('WARN', 'ready but larry_alerts unavailable; will retry next pass')
        _write_state(state)
        return state
    try:
        sent = fn(
            source='govern_loop_readiness',
            severity='warning',
            subject='Operator slice 7 is ready to build',
            message=_nudge_message(m),
            suggested_action=KICK_COMMAND,
            route='escalate',
            decision_key='govern-loop-assessor-kickoff-001',
            needs_larry=True,
        )
    except Exception as e:  # the nudge must never crash the pass
        _log('WARN', f'append_alert raised: {type(e).__name__}: {e}')
        sent = False
    if sent:
        state['last_nudge_ts'] = now_dt.isoformat()
        state['nudge_count'] = int(state.get('nudge_count') or 0) + 1
        _log('INFO', f"nudged Larry (count={state['nudge_count']})")
    else:
        _log('INFO', 'alert suppressed (cooldown) or failed; will retry next pass')
    _write_state(state)
    return state


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == '--once':
        result = run_once()
        print(json.dumps(result.get('progress', {}), indent=1))
        sys.exit(0)
    print('usage: govern_loop_readiness.py --once', file=sys.stderr)
    sys.exit(2)
