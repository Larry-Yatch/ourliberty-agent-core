#!/usr/bin/env python3
"""pulse_check_iii.py — 14-day stuck-threshold review analyzer.

Phase E4.4d PR-B. Spec: agents/beacon/specs/e4-4d-system-tab.md § 5.10.

Pulse runs Check III every other Sunday cycle. The analyzer:

  1. Queries chain_events for the last 30 days (session_start + session_done
     pairs per task_id) and computes per-(agent, task_type) duration
     statistics: median, p90, p99, sample size.
  2. Skips buckets with sample size < SAMPLE_SIZE_FLOOR (10) — insufficient
     signal to justify a tweak.
  3. Computes a proposed threshold (p90 by default, p95 if dataset is
     wider per spec). Compares against the current value from
     config/system_tab_thresholds.json.
  4. Flags bounded-delta changes (>BOUNDED_DELTA_RATIO, currently 50%) as
     'high-attention: regime-change-suspected' so Larry knows before
     approving.
  5. Detects rollback signals — if a recently-applied threshold change
     caused >ROLLBACK_FALSE_POSITIVE_THRESHOLD (3) false-positive
     stuck-detector alerts within ROLLBACK_WINDOW_DAYS (7d) of the apply,
     proposes un-tightening with rationale 'rollback'.
  6. Writes the proposal artifact to
     ~/agents/blackboard/pulse-threshold-proposals.json and queues a
     larry_alerts.append_alert digest (blue/warning severity).

The artifact shape lets Beacon's `approve threshold-update-<date>` flow
read it, apply the JSON-config edits, and flip `applied: true`.

This analyzer NEVER edits the config file. Pulse → proposal artifact →
Larry approves → Beacon → Claude-as-Forge → Mirror → merge. Same
discipline as the stuck-detector itself: surface signal, don't act.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
REPO_ROOT = Path(__file__).resolve().parent.parent
PROPOSALS_FILE = AGENTS_ROOT / 'blackboard' / 'pulse-threshold-proposals.json'
PROPOSALS_HISTORY_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-iii'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-iii.log'
THRESHOLDS_CONFIG = REPO_ROOT / 'config' / 'system_tab_thresholds.json'

LOOKBACK_DAYS = 30
SAMPLE_SIZE_FLOOR = 10

# 14-day Sunday-anchored cadence, self-enforced (2026-07-07 timer conversion):
# the systemd timer fires every Sunday and this gate skips the off week. It is
# anchored on the newest archived artifact's date — NOT the liveness heartbeat,
# which refreshes on any clean exit including --dry-run and the one-time
# heartbeat seeder, so heartbeat age can look fresh when no analysis ran.
# 13 (not 14) tolerates timer jitter on the due Sunday.
CADENCE_MIN_DAYS = 13
BOUNDED_DELTA_RATIO = 0.50           # >50% change → high-attention flag
ROLLBACK_WINDOW_DAYS = 7
ROLLBACK_FALSE_POSITIVE_THRESHOLD = 3

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from fixture_patterns import is_fixture_task_id  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


# -------------------- pure analysis core --------------------


@dataclass
class SessionDuration:
    """One observed session duration tied to an (agent, task_type) bucket.

    `task_id` is optional for back-compat with existing fixture files but
    is populated by the Supabase fetch path so the fixture-pattern filter
    in run_check can drop test artifacts before bucketing.
    """
    agent: str
    task_type: str
    duration_sec: float
    ts: str
    task_id: str = ''


@dataclass
class BucketStats:
    agent: str
    task_type: str
    sample_size: int
    median_sec: float
    p90_sec: float
    p99_sec: float


@dataclass
class Proposal:
    bucket: str                # human-readable "(agent, task_type)"
    agent: str
    task_type: str
    current_threshold_sec: Optional[int]
    proposed_threshold_sec: int
    sample_size: int
    p90_sec: float
    p99_sec: float
    median_sec: float
    rationale: str
    delta_ratio: float = 0.0
    high_attention: bool = False
    rollback: bool = False


def compute_percentile(values: list[float], percentile: float) -> float:
    """Closed-form percentile with linear interpolation. Empty → 0.0."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    sorted_values = sorted(values)
    k = (len(sorted_values) - 1) * percentile
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(sorted_values[int(k)])
    return (sorted_values[lo] * (hi - k) +
            sorted_values[hi] * (k - lo))


def bucket_durations(
    durations: Iterable[SessionDuration],
) -> dict[tuple[str, str], list[float]]:
    out: dict[tuple[str, str], list[float]] = {}
    for d in durations:
        key = (d.agent, d.task_type or '_default')
        out.setdefault(key, []).append(d.duration_sec)
    return out


def compute_bucket_stats(
    durations: Iterable[SessionDuration],
) -> list[BucketStats]:
    """Return BucketStats for every (agent, task_type) bucket with ≥1 obs."""
    out: list[BucketStats] = []
    for (agent, task_type), values in bucket_durations(durations).items():
        out.append(BucketStats(
            agent=agent, task_type=task_type, sample_size=len(values),
            median_sec=float(statistics.median(values)),
            p90_sec=compute_percentile(values, 0.90),
            p99_sec=compute_percentile(values, 0.99),
        ))
    return sorted(out, key=lambda b: (b.agent, b.task_type))


def current_threshold_for_bucket(
    config: dict[str, Any], agent: str, task_type: str,
) -> Optional[int]:
    """Look up the current threshold for an (agent, task_type) pair.

    Schema follows config/system_tab_thresholds.json from PR-C. Mirror,
    Forge, Beacon, and Pulse each have task_type-keyed overrides maps;
    agents not in the override map fall through to
    session_duration_seconds_default.
    """
    if agent == 'mirror':
        overrides = config.get('mirror_review_overrides_seconds') or {}
        if task_type in overrides:
            return int(overrides[task_type])
        if '_default' in overrides:
            return int(overrides['_default'])
    elif agent == 'forge':
        overrides = config.get('forge_overrides_seconds') or {}
        if task_type in overrides:
            return int(overrides[task_type])
        if '_default' in overrides:
            return int(overrides['_default'])
    elif agent == 'beacon':
        overrides = config.get('beacon_overrides_seconds') or {}
        if task_type in overrides:
            return int(overrides[task_type])
        if '_default' in overrides:
            return int(overrides['_default'])
    elif agent == 'pulse':
        overrides = config.get('pulse_overrides_seconds') or {}
        if task_type in overrides:
            return int(overrides[task_type])
        if '_default' in overrides:
            return int(overrides['_default'])
    default = config.get('session_duration_seconds_default')
    if default is not None:
        return int(default)
    return None


def propose_threshold(
    stats: BucketStats,
    config: dict[str, Any],
    *,
    sample_size_floor: int = SAMPLE_SIZE_FLOOR,
    bounded_delta_ratio: float = BOUNDED_DELTA_RATIO,
) -> Optional[Proposal]:
    """Compute a proposal for one bucket, or None if floor not met.

    Per spec § 5.10: proposed threshold = p90 (or p95 if dataset wider).
    For MVP we always use p90; tuning the percentile choice is itself a
    future Check III iteration.
    """
    if stats.sample_size < sample_size_floor:
        return None
    proposed = int(round(stats.p90_sec))
    current = current_threshold_for_bucket(
        config, stats.agent, stats.task_type)
    delta_ratio = 0.0
    if current and current > 0:
        delta_ratio = abs(proposed - current) / current
    high_attention = delta_ratio > bounded_delta_ratio
    rationale_parts = [
        f'n={stats.sample_size}',
        f'median={int(stats.median_sec)}s',
        f'p90={int(stats.p90_sec)}s',
        f'p99={int(stats.p99_sec)}s',
    ]
    if current is None:
        rationale_parts.append('no current threshold (proposing initial)')
    elif proposed == current:
        rationale_parts.append('no change (±0)')
    elif high_attention:
        rationale_parts.append(
            f'high-attention: regime-change-suspected '
            f'(Δ={delta_ratio:.0%} from {current}s → {proposed}s)'
        )
    else:
        direction = 'tighten' if proposed < current else 'loosen'
        rationale_parts.append(
            f'{direction} {current}s → {proposed}s (Δ={delta_ratio:.0%})'
        )
    return Proposal(
        bucket=f'({stats.agent}, {stats.task_type})',
        agent=stats.agent,
        task_type=stats.task_type,
        current_threshold_sec=current,
        proposed_threshold_sec=proposed,
        sample_size=stats.sample_size,
        p90_sec=stats.p90_sec,
        p99_sec=stats.p99_sec,
        median_sec=stats.median_sec,
        rationale='; '.join(rationale_parts),
        delta_ratio=delta_ratio,
        high_attention=high_attention,
    )


def detect_rollback_signal(
    *,
    last_applied_threshold: Optional[dict[str, Any]],
    recent_false_positives: int,
    window_days: int = ROLLBACK_WINDOW_DAYS,
    false_positive_threshold: int = ROLLBACK_FALSE_POSITIVE_THRESHOLD,
) -> bool:
    """Return True iff a recently-applied tightening produced too many false
    positives within the rollback window."""
    if not last_applied_threshold:
        return False
    if not last_applied_threshold.get('tightening'):
        return False
    applied_at_iso = last_applied_threshold.get('applied_at')
    if not applied_at_iso:
        return False
    try:
        applied_at = datetime.fromisoformat(
            applied_at_iso.replace('Z', '+00:00'))
    except ValueError:
        return False
    age_days = (datetime.now(timezone.utc) - applied_at).days
    if age_days > window_days:
        return False
    return recent_false_positives > false_positive_threshold


def annotate_rollbacks(
    proposals: list[Proposal],
    *,
    apply_history: dict[str, dict[str, Any]],
    false_positive_counts: dict[str, int],
) -> None:
    """In-place: flag any proposal that should be marked as a rollback.

    apply_history shape: { "<agent>:<task_type>": { 'tightening': bool,
        'applied_at': iso, 'prior_threshold_sec': int } }
    false_positive_counts: { "<agent>:<task_type>": int }
    """
    for prop in proposals:
        key = f'{prop.agent}:{prop.task_type}'
        last = apply_history.get(key)
        fps = false_positive_counts.get(key, 0)
        if detect_rollback_signal(
            last_applied_threshold=last,
            recent_false_positives=fps,
        ):
            prop.rollback = True
            prior = last.get('prior_threshold_sec') if last else None
            prop.rationale = (
                f'rollback: prior tightening produced {fps} false positives '
                f'within {ROLLBACK_WINDOW_DAYS}d; reverting toward '
                f'{prior}s. ' + prop.rationale
            )
            if prior is not None:
                prop.proposed_threshold_sec = int(prior)


def build_proposal_artifact(
    proposals: list[Proposal],
    *,
    as_of: Optional[datetime] = None,
) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc)
    return {
        'as_of': as_of.isoformat(),
        'lookback_days': LOOKBACK_DAYS,
        'sample_size_floor': SAMPLE_SIZE_FLOOR,
        'bounded_delta_ratio': BOUNDED_DELTA_RATIO,
        'applied': False,
        'proposals': [
            {
                'bucket': p.bucket,
                'agent': p.agent,
                'task_type': p.task_type,
                'current_threshold_sec': p.current_threshold_sec,
                'proposed_threshold_sec': p.proposed_threshold_sec,
                'sample_size': p.sample_size,
                'p90_sec': round(p.p90_sec, 2),
                'p99_sec': round(p.p99_sec, 2),
                'median_sec': round(p.median_sec, 2),
                'delta_ratio': round(p.delta_ratio, 4),
                'high_attention': p.high_attention,
                'rollback': p.rollback,
                'rationale': p.rationale,
            }
            for p in proposals
        ],
    }


def format_digest(artifact: dict[str, Any]) -> str:
    proposals = artifact.get('proposals') or []
    if not proposals:
        return (
            'Check III ran: no proposed threshold changes this cycle '
            '(all buckets within ±10% of current OR insufficient sample).'
        )
    lines = [
        f'Check III ({artifact["as_of"]}) — '
        f'{len(proposals)} proposed threshold update(s):',
        '',
    ]
    for p in proposals:
        flags = []
        if p['high_attention']:
            flags.append('high-attention')
        if p['rollback']:
            flags.append('rollback')
        flag_str = f' [{",".join(flags)}]' if flags else ''
        current_str = (f'{p["current_threshold_sec"]}s'
                       if p['current_threshold_sec'] is not None else '—')
        lines.append(
            f'- {p["bucket"]}: {current_str} → '
            f'{p["proposed_threshold_sec"]}s (n={p["sample_size"]}){flag_str}'
        )
        lines.append(f'    {p["rationale"]}')
    lines.append('')
    lines.append(
        'To approve: reply on Telegram with '
        f'`approve threshold-update-{artifact["as_of"][:10]}` '
        'or `reject threshold-update-<date> <reason>`.'
    )
    return '\n'.join(lines)


# -------------------- Supabase + config IO --------------------


def load_thresholds_config(path: Path = THRESHOLDS_CONFIG) -> dict[str, Any]:
    """Read the thresholds config file or return {} if it doesn't exist yet.

    PR-C creates this file; until that merges the analyzer can still run
    and propose absolute-value thresholds (no diff against current).
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log(f'thresholds config unreadable ({e}); proceeding without diff', 'WARN')
        return {}


def fetch_durations_from_supabase(
    client, *, lookback_days: int = LOOKBACK_DAYS,
) -> list[SessionDuration]:
    """Pair session_start/session_done events per task_id and produce durations.

    Strategy: pull both event types in the window, group by task_id, take
    the latest start + the earliest done after it. Skip task_ids that
    have only one half (still in flight, or orphaned).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_iso = cutoff.isoformat()
    # Audit #28: a task_id can legitimately recur in the window (retries reuse a
    # deterministic id). Pairing the EARLIEST start with the LATEST done spans
    # ALL sessions for that id — turning a real 5-min session into hours and
    # inflating the p90/p99 stuck-threshold proposal. Per the docstring intent,
    # keep the LATEST start, and collect ALL dones so we can pair it with the
    # EARLIEST done that lands at/after that start (one clean session span).
    starts: dict[str, dict[str, Any]] = {}
    dones_by_tid: dict[str, list[dict[str, Any]]] = {}

    for event_type in ('session_start', 'session_done'):
        page = 0
        page_size = 1000
        while True:
            res = (
                client.table('chain_events')
                      .select('task_id,agent,ts,payload')
                      .eq('event_type', event_type)
                      .gte('ts', cutoff_iso)
                      .order('ts')
                      .range(page * page_size, (page + 1) * page_size - 1)
                      .execute()
            )
            rows = getattr(res, 'data', None) or []
            for row in rows:
                tid = row.get('task_id')
                if not tid or 'ts' not in row:
                    continue
                if event_type == 'session_start':
                    if tid not in starts or row['ts'] > starts[tid]['ts']:
                        starts[tid] = row  # keep latest start
                else:
                    dones_by_tid.setdefault(tid, []).append(row)
            if len(rows) < page_size:
                break
            page += 1

    out: list[SessionDuration] = []
    for tid, start_row in starts.items():
        try:
            t0 = datetime.fromisoformat(start_row['ts'].replace('Z', '+00:00'))
        except (ValueError, KeyError):
            continue
        # Earliest done strictly after the latest start — the clean span.
        best_done_ts: Optional[datetime] = None
        for done_row in dones_by_tid.get(tid, ()):
            try:
                t1 = datetime.fromisoformat(done_row['ts'].replace('Z', '+00:00'))
            except (ValueError, KeyError):
                continue
            if t1 <= t0:
                continue
            if best_done_ts is None or t1 < best_done_ts:
                best_done_ts = t1
        if best_done_ts is None:
            continue
        duration = (best_done_ts - t0).total_seconds()
        payload = start_row.get('payload') or {}
        task_type = payload.get('task_type') or '_default'
        agent = start_row.get('agent') or 'unknown'
        out.append(SessionDuration(
            agent=agent, task_type=task_type,
            duration_sec=duration, ts=start_row['ts'],
            task_id=tid,
        ))
    return out


def _connect_supabase():
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — '
            'cannot run Check III.'
        )
    from supabase_factory import get_supabase_client  # type: ignore
    return get_supabase_client(url, key)


# -------------------- artifact + DM emission --------------------


def write_proposal_artifact(
    artifact: dict[str, Any], path: Path = PROPOSALS_FILE,
) -> None:
    # Audit #7 (companion to pulse_check_viii): atomic write so a mid-write
    # crash leaves the intact prior artifact, never a truncated one.
    atomic_write_json(path, artifact, indent=2)
    history_dir = PROPOSALS_HISTORY_DIR
    date_str = artifact['as_of'][:10]
    atomic_write_json(
        history_dir / f'check-iii-{date_str}.json', artifact, indent=2)


def dm_digest(artifact: dict[str, Any]) -> bool:
    body = format_digest(artifact)
    date_str = artifact['as_of'][:10]
    try:
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='pulse',
            severity='warning',
            message=body,
            subject=f'threshold-proposal-{date_str}',
            suggested_action=(
                f'Review proposals; reply `approve threshold-update-'
                f'{date_str}` on Telegram, or `reject threshold-update-'
                f'{date_str} <reason>`.'
            ),
        )
    except Exception as e:
        log(f'dm_digest failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- main --------------------


def run_check(
    *,
    durations: Optional[list[SessionDuration]] = None,
    config: Optional[dict[str, Any]] = None,
    apply_history: Optional[dict[str, dict[str, Any]]] = None,
    false_positive_counts: Optional[dict[str, int]] = None,
    as_of: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pure-function entry point — given inputs, returns the artifact.

    Used directly by tests; CLI fetches inputs from Supabase + config file.
    """
    config = config or {}
    durations = durations or []
    apply_history = apply_history or {}
    false_positive_counts = false_positive_counts or {}

    # Fixture-pattern allowlist (2026-05-27): drop test-artifact task_ids
    # from the data substrate before bucketing so synthetic-noise durations
    # cannot pull a real (agent, task_type) bucket's p90 around. Durations
    # whose task_id is empty (legacy fixtures, --fixture CLI files without
    # the field) pass through — only positive matches are filtered.
    durations = [d for d in durations if not is_fixture_task_id(d.task_id)]

    stats = compute_bucket_stats(durations)
    proposals: list[Proposal] = []
    for s in stats:
        p = propose_threshold(s, config)
        if p is None:
            continue
        if p.current_threshold_sec is not None and \
                p.proposed_threshold_sec == p.current_threshold_sec:
            # No change vs current — skip per spec ("No proposed changes
            # this cycle" is a valid output).
            continue
        proposals.append(p)

    annotate_rollbacks(
        proposals,
        apply_history=apply_history,
        false_positive_counts=false_positive_counts,
    )
    return build_proposal_artifact(proposals, as_of=as_of)


def days_since_last_artifact(now: Optional[datetime] = None) -> Optional[float]:
    """Days since the newest archived check-iii-<date>.json, or None if none.

    The archive date is the cadence anchor: it moves only when a real analysis
    ran (write_proposal_artifact), never on --dry-run or heartbeat seeding.
    Unreadable dir or unparseable names count as "no artifact" (fail toward
    running — a missed off-week skip is cheaper than a silently dead check).
    """
    newest: Optional[datetime] = None
    try:
        for f in PROPOSALS_HISTORY_DIR.glob('check-iii-*.json'):
            try:
                d = datetime.strptime(
                    f.name[len('check-iii-'):-len('.json')], '%Y-%m-%d',
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if newest is None or d > newest:
                newest = d
    except OSError:
        return None
    if newest is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - newest).total_seconds() / 86400.0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',
                        help='Read durations from a JSON fixture file '
                             'instead of querying Supabase (for smoke tests).')
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute + print but do not write artifact or '
                             'send DM.')
    parser.add_argument('--force', action='store_true',
                        help='Skip the 14-day cadence gate (on-demand run).')
    args = parser.parse_args(argv)

    if not args.force and not args.dry_run:
        age = days_since_last_artifact()
        if age is not None and age < CADENCE_MIN_DAYS:
            log(f'newest Check III artifact is {age:.1f}d old '
                f'(< {CADENCE_MIN_DAYS}d) — inside the 14-day cadence; '
                'skipping (use --force to re-run).')
            return 0

    config = load_thresholds_config()

    if args.fixture:
        with open(args.fixture) as fh:
            raw = json.load(fh)
        durations = [SessionDuration(**d) for d in raw.get('durations', [])]
    else:
        try:
            client = _connect_supabase()
            durations = fetch_durations_from_supabase(client)
        except Exception as e:
            log(f'cannot fetch durations: {type(e).__name__}: {e}', 'ERROR')
            return 1

    artifact = run_check(durations=durations, config=config)

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    write_proposal_artifact(artifact)
    dm_digest(artifact)
    log(f'Check III complete: {len(artifact["proposals"])} proposal(s)')
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check as _hb_run_check
    sys.exit(_hb_run_check('iii', main, log_fn=log))
