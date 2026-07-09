#!/usr/bin/env python3
"""gh_burn_analyzer.py — measure the GitHub GraphQL burn + auto-ping phase 2.

Reads the burn samples ``gh_burn_sampler.py`` accumulates in
``~/agents/logs/gh-api-burn.jsonl`` and, ONCE it has at least a full 24h collection
window, measures the real picture: peak GraphQL consumption per hour vs the 5000
ceiling, how many distinct hours hit exhaustion (remaining ~0), and the steady-state
headroom. It writes a dated proposal artifact under
``~/agents/blackboard/gh-burn-analysis/`` and — if exhaustion was actually observed —
emits exactly ONE ``approval_request`` pinging Larry to authorize PHASE 2 (the
durable shared cached-open-PR-snapshot fix), routed to Beacon on approve so she
authors the phase-2 spec against this collected data.

Same discipline as pulse_check_iii/viii: this analyzer surfaces signal, it never acts.
It writes the artifact + queues the approval; the phase-2 code is Beacon's to author
after Larry approves.

Idempotent: the phase-2 ping is emitted AT MOST ONCE. It gates on an ``emitted: true``
flag persisted in the artifact — once any prior artifact recorded the emit, the daily
timer is a no-op. Insufficient data (<24h) or a healthy budget (no exhaustion in the
window) writes the finding artifact and emits NO ping.

stdlib only (+ beacon_approval_handler / chain_event_emit lazily for the emit).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from atomic_io import atomic_write_json  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
BURN_LOG = AGENTS_ROOT / 'logs' / 'gh-api-burn.jsonl'
HISTORY_DIR = AGENTS_ROOT / 'blackboard' / 'gh-burn-analysis'
LOG_FILE = AGENTS_ROOT / 'logs' / 'gh-burn-analyzer.log'

SCHEMA = 'gh-burn-analysis-v1'
GRAPHQL_CEILING = 5000
# A full collection window before we trust the measurement enough to ask Larry.
MIN_WINDOW_HOURS = 24.0
# remaining at/under this in a window == that hour hit exhaustion.
EXHAUSTION_REMAINING = 50
# Stable task_id so the approval_request's chain-event dedup identity is stable
# across any re-emit attempt (the emitted-flag gate is the primary guard).
PHASE2_TASK_ID = 'gh-burn-phase2-durable-fix-authorize'
PHASE2_TARGET_REPO = 'ourliberty-agent-core'


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def read_samples(path: Path = BURN_LOG) -> list[dict]:
    """Parse the burn log into sample dicts, skipping malformed lines."""
    out: list[dict] = []
    try:
        with open(path, encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


def _parse_ts(sample: dict) -> Optional[datetime]:
    ts = sample.get('ts')
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _bucket_key(sample: dict) -> Optional[Any]:
    """Group samples into hourly budget windows. Prefer the GraphQL `reset`
    epoch (all samples in one reset window share it — the true hour boundary);
    fall back to the sample ts truncated to the hour."""
    reset = sample.get('graphql_reset')
    if isinstance(reset, int):
        return ('reset', reset)
    dt = _parse_ts(sample)
    if dt is None:
        return None
    return ('hour', dt.replace(minute=0, second=0, microsecond=0).isoformat())


def analyze(samples: list[dict], now: Optional[datetime] = None) -> dict[str, Any]:
    """Pure core: reduce raw samples to the measured-burn artifact.

    Buckets samples into hourly budget windows; per window the floor (min
    remaining) gives the hour's consumption (limit - floor) and whether it hit
    exhaustion. Returns the artifact with a `finding` and a `should_emit`
    decision (the ping is only warranted when exhaustion was actually observed
    across a full >=24h window)."""
    now = now or datetime.now(timezone.utc)

    times = [t for t in (_parse_ts(s) for s in samples) if t is not None]
    window_hours = 0.0
    if len(times) >= 2:
        window_hours = (max(times) - min(times)).total_seconds() / 3600.0

    valid = [
        s for s in samples
        if isinstance(s.get('graphql_remaining'), int)
        and isinstance(s.get('graphql_limit'), int)
    ]

    buckets: dict[Any, dict[str, int]] = {}
    for s in valid:
        key = _bucket_key(s)
        if key is None:
            continue
        rem = s['graphql_remaining']
        lim = s['graphql_limit']
        b = buckets.get(key)
        if b is None:
            buckets[key] = {'floor': rem, 'limit': lim}
        else:
            b['floor'] = min(b['floor'], rem)
            b['limit'] = max(b['limit'], lim)

    total_hours = len(buckets)
    peak_consumed: Optional[int] = None
    steady_state_headroom: Optional[int] = None
    exhausted_hours = 0
    if buckets:
        peak_consumed = max(b['limit'] - b['floor'] for b in buckets.values())
        steady_state_headroom = int(
            statistics.median(b['floor'] for b in buckets.values()))
        exhausted_hours = sum(
            1 for b in buckets.values() if b['floor'] <= EXHAUSTION_REMAINING)

    enough_data = window_hours >= MIN_WINDOW_HOURS and len(valid) >= 2

    if not enough_data:
        finding = 'insufficient_data'
        should_emit = False
    elif exhausted_hours > 0:
        finding = 'exhaustion_observed'
        should_emit = True
    else:
        finding = 'healthy'
        should_emit = False

    return {
        'schema': SCHEMA,
        'as_of': now.isoformat(),
        'sample_count': len(samples),
        'valid_sample_count': len(valid),
        'window_hours': round(window_hours, 2),
        'enough_data': enough_data,
        'graphql_ceiling': GRAPHQL_CEILING,
        'total_hours_observed': total_hours,
        'peak_consumed_per_hour': peak_consumed,
        'exhausted_hours': exhausted_hours,
        'steady_state_headroom': steady_state_headroom,
        'finding': finding,
        'should_emit': should_emit,
        'emitted': False,
    }


def _build_payload(artifact: dict) -> dict[str, Any]:
    peak = artifact['peak_consumed_per_hour']
    exhausted = artifact['exhausted_hours']
    total = artifact['total_hours_observed']
    headroom = artifact['steady_state_headroom']
    summary = (
        f'GitHub GraphQL burn measured over {artifact["window_hours"]:.0f}h: '
        f'peak {peak}/{GRAPHQL_CEILING} pts/hr, exhausted {exhausted} of '
        f'{total} hrs observed (steady-state headroom ~{headroom}). Approve to '
        'authorize Beacon to author + dispatch the phase-2 durable fix (a shared '
        'cached open-PR snapshot the ~dozen PR-polling healers read instead of '
        'each re-querying `gh pr list` every tick).'
    )
    prompt = (
        'Phase-2 durable fix for the GitHub GraphQL rate-limit burn is authorized. '
        'Author + dispatch a spec for a SHARED cached open-PR snapshot: a single '
        'periodically-refreshed cache of "what PRs are open?" per repo that the '
        'redundant pollers (heal_pipeline_stall, heal_pr_auto_merge, '
        'heal_undispatched_pr_review, build_sequence_advancer, and the '
        'task_terminal_state probe callers) read instead of each firing their own '
        '`gh pr list` GraphQL query every ~5 min. Base the design on the phase-1 '
        'measurement artifact under ~/agents/blackboard/gh-burn-analysis/: '
        f'peak {peak}/{GRAPHQL_CEILING} pts/hr, {exhausted} exhausted hrs observed. '
        'Phase-1 (this measurement + the backoff guard in scripts/gh_budget.py) is '
        'already shipped; phase-2 is the durable cache that removes the redundant '
        'polling so the backoff rarely needs to fire.'
    )
    return {
        'task_id': PHASE2_TASK_ID,
        'summary': summary,
        'prompt': prompt,
        'target_agent': 'beacon',
        'target_repo': PHASE2_TARGET_REPO,
        'kind': 'gh-burn-phase2',
        'bare_approvable': False,
    }


def _default_emit(payload: dict) -> bool:
    """Production emit wiring — mirrors main_suite_guardian's approval producer:
    register a pending-approval entry (bot DM + reminders) and write the
    approval_request chain-event (the Approvals-tab surface). Best-effort; the
    chain-event write is the essential surface, so its success is the return."""
    chat_id = 0
    try:
        chat_id = int(os.environ.get('LARRY_CHAT_ID', '0') or '0')
    except ValueError:
        chat_id = 0
    try:
        import beacon_approval_handler as ah
        import chain_event_emit as ce
    except Exception as e:  # noqa: BLE001
        log(f'emit deps unavailable ({type(e).__name__}); ping not sent', 'ERROR')
        return False
    try:
        ah.add_pending(payload, chat_id)
    except Exception as e:  # noqa: BLE001
        log(f'add_pending failed ({type(e).__name__}); continuing to chain-event '
            'emit', 'WARN')
    try:
        return bool(ce.emit_event(**ah.build_approval_request_chain_event(payload)))
    except Exception as e:  # noqa: BLE001
        log(f'chain-event emit failed ({type(e).__name__}); ping not sent', 'ERROR')
        return False


def already_emitted(history_dir: Path = HISTORY_DIR) -> bool:
    """True if any prior artifact recorded the phase-2 ping (emitted flag). This
    is the at-most-once gate — once set, the daily timer never re-pings."""
    try:
        for f in history_dir.glob('gh-burn-*.json'):
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and d.get('emitted') is True:
                return True
    except OSError:
        return False
    return False


def write_artifact(artifact: dict, now: datetime,
                   history_dir: Path = HISTORY_DIR) -> Path:
    """Write the dated artifact atomically. Never downgrades a same-day
    emitted=True record to emitted=False (protects the idempotency flag if the
    timer fires twice in one day after an emit)."""
    path = history_dir / f'gh-burn-{now.date().isoformat()}.json'
    if not artifact.get('emitted') and path.exists():
        try:
            prior = json.loads(path.read_text())
            if isinstance(prior, dict) and prior.get('emitted') is True:
                return path
        except (OSError, ValueError):
            pass
    atomic_write_json(path, artifact, indent=2)
    return path


def run(*, samples: Optional[list[dict]] = None,
        now: Optional[datetime] = None,
        emit_approval: Optional[Callable[[dict], bool]] = None,
        history_dir: Path = HISTORY_DIR) -> dict[str, Any]:
    """Read samples, measure, write the artifact, and emit the phase-2 ping at
    most once. ``emit_approval`` is injectable so tests assert the emit without
    touching Supabase/the bot."""
    now = now or datetime.now(timezone.utc)
    if samples is None:
        samples = read_samples()
    emit = emit_approval or _default_emit

    artifact = analyze(samples, now=now)

    if artifact['should_emit']:
        if already_emitted(history_dir):
            artifact['should_emit'] = False
            artifact['note'] = ('phase-2 ping already emitted in a prior run; '
                                 'idempotent no-op')
            log('exhaustion present but phase-2 ping already emitted earlier; '
                'no-op')
        else:
            payload = _build_payload(artifact)
            ok = emit(payload)
            artifact['emitted'] = bool(ok)
            log(f'phase-2 approval_request emit {"ok" if ok else "FAILED"}: '
                f'peak {artifact["peak_consumed_per_hour"]}/{GRAPHQL_CEILING}, '
                f'{artifact["exhausted_hours"]} exhausted hrs')
    else:
        log(f'finding={artifact["finding"]} '
            f'(window={artifact["window_hours"]}h, '
            f'exhausted_hrs={artifact["exhausted_hours"]}); no ping')

    write_artifact(artifact, now, history_dir)
    return artifact


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute + print the finding but write no artifact '
                             'and emit no ping.')
    args = parser.parse_args(argv)

    if args.dry_run:
        artifact = analyze(read_samples())
        print(json.dumps(artifact, indent=2))
        return 0

    run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
