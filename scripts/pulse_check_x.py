#!/usr/bin/env python3
"""pulse_check_x.py — chain-quality regression watch.

Check X. Spec: docs/check-x-chain-quality-regression-brief.md.

Motivated by the Opus 4.8 cutover on the Forge/Mirror auto-merge chain
(PR #233, 2026-06-01), but built as a general chain-quality regression
detector: re-point `cutover_date` for any future model/prompt change. A
same-family model bump won't show in routine chat — it shows on hard
build/review tasks. This analyzer is the objective early-warning that the
chain's quality has regressed since the cutover.

Weekly cadence (Mondays, from /cycle alongside Checks I / IV / VIII).
Deterministic, stdlib-only core, no LLM calls, read-only. The supabase
SDK is lazy-imported inside `_connect_supabase` so the `--from-json` test
path never touches it.

Inputs (read-only):

  - Supabase `chain_events` rows with `event_type == 'clarify_request'`
    and `agent == 'forge'` over a span covering both comparison windows.
  - Supabase `chain_events` rows with `event_type` in
    {review_pass, review_revision, review_escalate} (Mirror verdicts) over
    the same span, for the verdict-mix thresholds.
  - Local `~/agents/blackboard/costs.jsonl` for the Forge build-work task
    universe and the revision-rounds proxy.

Comparison windows:

  - Baseline = the `window_days` immediately BEFORE `cutover_date`.
  - Trailing = the most recent `window_days` (ending now).

Metrics (per window):

  - clarify_rounds_per_task = clarify_request count / distinct Forge task.
  - revision_rounds_per_task = (Forge build-work cost rows - distinct
    Forge tasks) / distinct Forge tasks, i.e. mean dispatches-per-task
    beyond the first. PROXY.
  - Mirror verdict mix: pass_rate = review_pass / total verdicts;
    escalate_rate = review_escalate / total verdicts (per window).

VERDICT-MIX status (check-x-verdict-emission, 2026-06-01):

  The verdict-mix thresholds `escalate_rate_abs_increase` and
  `pass_rate_abs_drop` were DEFERRED in v1 because no writer emitted the
  Mirror outcome mix. They are now ACTIVE: outbox_notifier push-emits
  `review_pass` / `review_revision` / `review_escalate` chain_events at the
  verdict-classification site. They are DATA-GATED — a window with fewer than
  `min_tasks_per_window` Mirror verdicts is treated as insufficient verdict
  history and the thresholds report status `insufficient_signal` (never fire).

  HONEST LIMITATION: emission started at the merge of the check-x-verdict-
  emission PR, so the baseline window for the CURRENT 4.8 cutover (before
  2026-06-01) has NO verdict events. The verdict-mix thresholds therefore
  CANNOT read the 4.8 transition retroactively — for 4.8 they sit at
  insufficient_signal. They activate for FUTURE cutovers once verdict history
  exists on both sides of `cutover_date`. The clarify/revision thresholds
  remain the 4.8 read (they have pre-cutover history).

  clarify/revision are RELATIVE-increase detectors (trailing vs baseline),
  so any stable measurement bias cancels; the verdict-mix thresholds are
  ABSOLUTE-change detectors on the pass/escalate share. Only a genuine
  post-cutover shift trips a threshold.

Outputs:

  - Artifact `~/agents/blackboard/pulse-check-x-proposals/check-x-<monday>.json`
    (full metric table for both windows + outcome + breached thresholds).
    Doubles as the same-week sentinel (idempotent re-run within the ISO week).
  - DM via `larry_alerts.append_alert` with `source='pulse-check-x'`,
    severity `warning`, ONLY when outcome is `regression_suspected`.
    Plain-language, CEO-readable, explicitly correlational.

The analyzer NEVER edits config/agent-models.json. It proposes; Larry
disposes (review recent Forge/Mirror PRs, and if confirmed revert the
affected agent to claude-opus-4-7).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path(
    os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents'))
)
REPO_ROOT = Path(__file__).resolve().parent.parent
COSTS_FILE = AGENTS_ROOT / 'blackboard' / 'costs.jsonl'
PROPOSALS_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-x-proposals'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-x.log'
AGENT_MODELS_CONFIG = REPO_ROOT / 'config' / 'agent-models.json'

# Config defaults — mirror config/agent-models.json:check_x_regression so a
# missing/malformed config still produces a sane (conservative) run.
DEFAULT_CUTOVER_DATE = '2026-06-01'
DEFAULT_WINDOW_DAYS = 28
DEFAULT_MIN_TASKS_PER_WINDOW = 8
DEFAULT_CLARIFY_RATE_REL_INCREASE = 0.5
DEFAULT_REVISION_ROUNDS_REL_INCREASE = 0.5
DEFAULT_ESCALATE_RATE_ABS_INCREASE = 0.10
DEFAULT_PASS_RATE_ABS_DROP = 0.15

# Active firing thresholds. The two verdict-mix thresholds were reactivated by
# the check-x-verdict-emission PR once outbox_notifier began push-emitting the
# review_pass/review_revision/review_escalate chain_events.
ACTIVE_THRESHOLDS = (
    'clarify_rate_rel_increase',
    'revision_rounds_rel_increase',
    'escalate_rate_abs_increase',
    'pass_rate_abs_drop',
)
# No thresholds are deferred anymore. Kept (empty) so the artifact schema's
# `deferred_thresholds` key and any downstream consumers stay stable.
DEFERRED_THRESHOLDS: tuple[str, ...] = ()
# Verdict-mix thresholds carry their own data gate: a window with fewer than
# `min_tasks_per_window` Mirror verdicts is treated as insufficient verdict
# history (status `insufficient_signal`) and never fires. HONEST LIMITATION:
# emission started at the merge of the check-x-verdict-emission PR, so the
# 4.8-cutover baseline window (before 2026-06-01) has ZERO verdict events and
# these two thresholds CANNOT read the 4.8 transition retroactively — they
# activate for FUTURE cutovers once verdict history exists on both sides of
# cutover_date. The clarify/revision thresholds remain the 4.8 read.
VERDICT_THRESHOLDS = ('escalate_rate_abs_increase', 'pass_rate_abs_drop')

# costs.jsonl task_types that are NOT Forge build-work (inter-agent legs,
# retries, merges, fixtures-by-type). A Forge cost row whose task_type is
# NOT in this set counts toward the build-work task universe.
EXCLUDED_TASK_TYPES = frozenset({
    'notification', 'retry', 'auto-merge', 'dead-letter', 'smoke-test',
    'cycle',
})

CLARIFY_EVENT_TYPE = 'clarify_request'
FORGE_AGENT = 'forge'
MIRROR_AGENT = 'mirror'

# Mirror verdict chain_event types (emitted by outbox_notifier at the verdict-
# classification site) → verdict-mix bucket.
VERDICT_EVENT_TYPES = {
    'review_pass': 'pass',
    'review_revision': 'revision',
    'review_escalate': 'escalate',
}

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from fixture_patterns import is_fixture_task_id  # noqa: E402


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


# -------------------- data records --------------------


@dataclass
class ClarifyEvent:
    ts: datetime
    task_id: str
    agent: str = FORGE_AGENT


@dataclass
class VerdictEvent:
    """A Mirror review verdict (PASS/REVISION/ESCALATE) chain_event."""
    ts: datetime
    task_id: str
    verdict: str  # 'pass' | 'revision' | 'escalate'
    agent: str = MIRROR_AGENT


@dataclass
class CostRow:
    ts: datetime
    task_id: str
    agent: str
    task_type: str


@dataclass
class WindowMetrics:
    """Computed metrics for one comparison window."""
    label: str
    distinct_tasks: int
    clarify_count: int
    clarify_rounds_per_task: Optional[float]
    revision_rows: int
    revision_rounds_per_task: Optional[float]
    # Mirror verdict mix (check-x-verdict-emission). Rates are None when the
    # window has zero verdicts (can't divide); verdict_total gates firing.
    verdict_total: int = 0
    pass_count: int = 0
    revision_count: int = 0
    escalate_count: int = 0
    pass_rate: Optional[float] = None
    escalate_rate: Optional[float] = None


@dataclass
class CheckXResult:
    outcome: str                       # regression_suspected | none | insufficient_signal
    baseline: WindowMetrics
    trailing: WindowMetrics
    breaches: list[dict[str, Any]]     # active thresholds that fired
    deferred: list[dict[str, Any]]     # deferred thresholds (now always empty)
    rationale: str
    week_anchor: str
    as_of_iso: str
    # Per verdict-mix threshold status: fired | clean | insufficient_signal.
    verdict_thresholds: list[dict[str, Any]] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)


# -------------------- pure helpers --------------------


def _parse_ts(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_cutover(cutover_date: str) -> datetime:
    """Parse a YYYY-MM-DD cutover date to midnight UTC. Falls back to the
    default on any parse error."""
    dt = _parse_ts(cutover_date)
    if dt is not None:
        return dt
    dt = _parse_ts(DEFAULT_CUTOVER_DATE)
    assert dt is not None  # default is well-formed
    return dt


def iso_week_monday(d: date) -> str:
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def artifact_path_for_week(week_anchor: str) -> Path:
    return PROPOSALS_DIR / f'check-x-{week_anchor}.json'


def _is_forge_build_work(row: CostRow) -> bool:
    return (
        row.agent == FORGE_AGENT
        and row.task_type not in EXCLUDED_TASK_TYPES
        and not is_fixture_task_id(row.task_id)
    )


def _rel_increase(baseline: Optional[float], trailing: Optional[float]
                  ) -> Optional[float]:
    """Relative increase (trailing-baseline)/baseline. None when undefined
    (baseline is None or <= 0) — we never fire a rel-increase off a zero
    baseline (would be infinite / noise on thin data)."""
    if baseline is None or trailing is None or baseline <= 0:
        return None
    return (trailing - baseline) / baseline


def _eval_verdict_thresholds(
    baseline: WindowMetrics,
    trailing: WindowMetrics,
    *,
    escalate_thr: float,
    pass_thr: float,
    min_verdicts: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Evaluate the two verdict-mix thresholds. Returns (breaches, statuses).

    DATA GATE: a window with fewer than `min_verdicts` Mirror verdicts is
    insufficient verdict history — both thresholds report `insufficient_signal`
    and never fire. This is the mechanism that keeps the thresholds silent for
    the 4.8 cutover (its baseline has ZERO verdict events) and until enough
    history accrues for any future cutover.

    escalate_rate_abs_increase fires on an ABSOLUTE rise in escalate share
    (trailing - baseline >= threshold). pass_rate_abs_drop fires on an ABSOLUTE
    fall in pass share (baseline - trailing >= threshold).
    """
    gated = (baseline.verdict_total < min_verdicts
             or trailing.verdict_total < min_verdicts)

    def _status(threshold: str, metric: str,
                b_rate: Optional[float], t_rate: Optional[float],
                abs_change: Optional[float], configured_min: float,
                fired: bool) -> dict[str, Any]:
        return {
            'threshold': threshold,
            'metric': metric,
            'status': ('insufficient_signal' if gated
                       else 'fired' if fired else 'clean'),
            'baseline': b_rate,
            'trailing': t_rate,
            'abs_change': abs_change,
            'configured_min': configured_min,
            'baseline_verdicts': baseline.verdict_total,
            'trailing_verdicts': trailing.verdict_total,
        }

    breaches: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []

    # escalate_rate_abs_increase
    esc_change = (None if gated or baseline.escalate_rate is None
                  or trailing.escalate_rate is None
                  else trailing.escalate_rate - baseline.escalate_rate)
    esc_fired = esc_change is not None and esc_change >= escalate_thr
    statuses.append(_status(
        'escalate_rate_abs_increase', 'escalate_rate',
        baseline.escalate_rate, trailing.escalate_rate,
        esc_change, escalate_thr, esc_fired,
    ))

    # pass_rate_abs_drop
    pass_change = (None if gated or baseline.pass_rate is None
                   or trailing.pass_rate is None
                   else baseline.pass_rate - trailing.pass_rate)
    pass_fired = pass_change is not None and pass_change >= pass_thr
    statuses.append(_status(
        'pass_rate_abs_drop', 'pass_rate',
        baseline.pass_rate, trailing.pass_rate,
        pass_change, pass_thr, pass_fired,
    ))

    for st in statuses:
        if st['status'] == 'fired':
            breaches.append({
                'threshold': st['threshold'],
                'metric': st['metric'],
                'baseline': st['baseline'],
                'trailing': st['trailing'],
                'abs_change': st['abs_change'],
                'configured_min': st['configured_min'],
            })

    return breaches, statuses


def compute_window_metrics(
    label: str,
    clarify_events: list[ClarifyEvent],
    cost_rows: list[CostRow],
    verdict_events: list[VerdictEvent],
    *,
    start: datetime,
    end: datetime,
) -> WindowMetrics:
    """Compute metrics over [start, end). Fixture task_ids already excluded
    by the loaders / _is_forge_build_work; we re-guard clarify + verdict
    events here."""
    build_rows = [
        r for r in cost_rows
        if _is_forge_build_work(r) and start <= r.ts < end
    ]
    task_ids = {r.task_id for r in build_rows}
    distinct_tasks = len(task_ids)

    clarifies = [
        e for e in clarify_events
        if e.agent == FORGE_AGENT
        and not is_fixture_task_id(e.task_id)
        and start <= e.ts < end
    ]
    clarify_count = len(clarifies)
    revision_rows = len(build_rows)

    if distinct_tasks > 0:
        clarify_rpt: Optional[float] = clarify_count / distinct_tasks
        # dispatches beyond the first per task, averaged.
        revision_rpt: Optional[float] = (
            (revision_rows - distinct_tasks) / distinct_tasks
        )
    else:
        clarify_rpt = None
        revision_rpt = None

    verdicts = [
        v for v in verdict_events
        if v.verdict in ('pass', 'revision', 'escalate')
        and not is_fixture_task_id(v.task_id)
        and start <= v.ts < end
    ]
    verdict_total = len(verdicts)
    pass_count = sum(1 for v in verdicts if v.verdict == 'pass')
    revision_count = sum(1 for v in verdicts if v.verdict == 'revision')
    escalate_count = sum(1 for v in verdicts if v.verdict == 'escalate')
    if verdict_total > 0:
        pass_rate: Optional[float] = pass_count / verdict_total
        escalate_rate: Optional[float] = escalate_count / verdict_total
    else:
        pass_rate = None
        escalate_rate = None

    return WindowMetrics(
        label=label,
        distinct_tasks=distinct_tasks,
        clarify_count=clarify_count,
        clarify_rounds_per_task=clarify_rpt,
        revision_rows=revision_rows,
        revision_rounds_per_task=revision_rpt,
        verdict_total=verdict_total,
        pass_count=pass_count,
        revision_count=revision_count,
        escalate_count=escalate_count,
        pass_rate=pass_rate,
        escalate_rate=escalate_rate,
    )


def run_check(
    *,
    clarify_events: list[ClarifyEvent],
    cost_rows: list[CostRow],
    config: dict[str, Any],
    verdict_events: Optional[list[VerdictEvent]] = None,
    now: Optional[datetime] = None,
) -> CheckXResult:
    """Pure-function entry point. Given inputs + a resolved config dict,
    returns a CheckXResult. Used directly by tests.

    `verdict_events` defaults to empty: callers (and tests) that don't supply
    Mirror verdicts get the verdict-mix thresholds gated as insufficient_signal
    rather than an error — preserving the pre-emission behavior shape.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if verdict_events is None:
        verdict_events = []

    window_days = int(config.get('window_days', DEFAULT_WINDOW_DAYS))
    min_tasks = int(config.get('min_tasks_per_window',
                               DEFAULT_MIN_TASKS_PER_WINDOW))
    cutover = _parse_cutover(str(config.get('cutover_date',
                                            DEFAULT_CUTOVER_DATE)))
    clarify_thr = float(config.get('clarify_rate_rel_increase',
                                   DEFAULT_CLARIFY_RATE_REL_INCREASE))
    revision_thr = float(config.get('revision_rounds_rel_increase',
                                    DEFAULT_REVISION_ROUNDS_REL_INCREASE))
    escalate_thr = float(config.get('escalate_rate_abs_increase',
                                    DEFAULT_ESCALATE_RATE_ABS_INCREASE))
    pass_thr = float(config.get('pass_rate_abs_drop',
                                DEFAULT_PASS_RATE_ABS_DROP))

    span = timedelta(days=window_days)
    trailing = compute_window_metrics(
        'trailing', clarify_events, cost_rows, verdict_events,
        start=now - span, end=now,
    )
    baseline = compute_window_metrics(
        'baseline', clarify_events, cost_rows, verdict_events,
        start=cutover - span, end=cutover,
    )

    week_anchor = iso_week_monday(now.date())
    as_of_iso = now.isoformat()

    # No deferred thresholds remain (verdict-mix reactivated). Kept empty for
    # artifact-schema stability.
    deferred: list[dict[str, Any]] = []

    # Verdict-mix thresholds carry their own per-window verdict-count gate
    # (min_tasks_per_window), independent of the Forge cost-row volume gate
    # below. Computed up front so the status is reported even when the overall
    # check is insufficient_signal.
    verdict_breaches, verdict_statuses = _eval_verdict_thresholds(
        baseline, trailing,
        escalate_thr=escalate_thr, pass_thr=pass_thr,
        min_verdicts=min_tasks,
    )

    # Insufficient-signal gate: don't cry wolf on thin Forge build-work volume.
    if baseline.distinct_tasks < min_tasks or trailing.distinct_tasks < min_tasks:
        rationale = (
            f'Insufficient signal: distinct Forge build-work tasks '
            f'baseline={baseline.distinct_tasks}, '
            f'trailing={trailing.distinct_tasks} '
            f'(floor {min_tasks} per window). No proposal this cycle.'
        )
        return CheckXResult(
            outcome='insufficient_signal', baseline=baseline,
            trailing=trailing, breaches=[], deferred=deferred,
            rationale=rationale, week_anchor=week_anchor,
            as_of_iso=as_of_iso, verdict_thresholds=verdict_statuses,
            config=dict(config),
        )

    breaches: list[dict[str, Any]] = []

    clarify_rel = _rel_increase(baseline.clarify_rounds_per_task,
                                trailing.clarify_rounds_per_task)
    if clarify_rel is not None and clarify_rel >= clarify_thr:
        breaches.append({
            'threshold': 'clarify_rate_rel_increase',
            'metric': 'clarify_rounds_per_task',
            'baseline': baseline.clarify_rounds_per_task,
            'trailing': trailing.clarify_rounds_per_task,
            'rel_increase': clarify_rel,
            'configured_min': clarify_thr,
        })

    revision_rel = _rel_increase(baseline.revision_rounds_per_task,
                                 trailing.revision_rounds_per_task)
    if revision_rel is not None and revision_rel >= revision_thr:
        breaches.append({
            'threshold': 'revision_rounds_rel_increase',
            'metric': 'revision_rounds_per_task',
            'baseline': baseline.revision_rounds_per_task,
            'trailing': trailing.revision_rounds_per_task,
            'rel_increase': revision_rel,
            'configured_min': revision_thr,
        })

    # Verdict-mix breaches (gated above) join the active breach list.
    breaches.extend(verdict_breaches)

    if breaches:
        names = ', '.join(b['threshold'] for b in breaches)
        rationale = (
            f'regression_suspected: {len(breaches)} active threshold(s) '
            f'breached ({names}) with >= {min_tasks} tasks in both windows '
            f'(baseline={baseline.distinct_tasks}, '
            f'trailing={trailing.distinct_tasks}). CORRELATIONAL — the '
            f'cutover coincides with the change; cause is not proven.'
        )
        outcome = 'regression_suspected'
    else:
        rationale = (
            f'No active threshold breached. clarify_rounds_per_task '
            f'{baseline.clarify_rounds_per_task} -> '
            f'{trailing.clarify_rounds_per_task}; revision_rounds_per_task '
            f'{baseline.revision_rounds_per_task} -> '
            f'{trailing.revision_rounds_per_task}. Chain quality within '
            f'bounds this week.'
        )
        outcome = 'none'

    return CheckXResult(
        outcome=outcome, baseline=baseline, trailing=trailing,
        breaches=breaches, deferred=deferred, rationale=rationale,
        week_anchor=week_anchor, as_of_iso=as_of_iso,
        verdict_thresholds=verdict_statuses, config=dict(config),
    )


# -------------------- artifact + DM --------------------


def _window_dict(m: WindowMetrics) -> dict[str, Any]:
    def _r(x: Optional[float]) -> Optional[float]:
        return round(x, 4) if isinstance(x, (int, float)) else None
    return {
        'distinct_tasks': m.distinct_tasks,
        'clarify_count': m.clarify_count,
        'clarify_rounds_per_task': _r(m.clarify_rounds_per_task),
        'revision_rows': m.revision_rows,
        'revision_rounds_per_task': _r(m.revision_rounds_per_task),
        'verdict_total': m.verdict_total,
        'pass_count': m.pass_count,
        'revision_count': m.revision_count,
        'escalate_count': m.escalate_count,
        'pass_rate': _r(m.pass_rate),
        'escalate_rate': _r(m.escalate_rate),
    }


def build_artifact(result: CheckXResult) -> dict[str, Any]:
    return {
        'as_of': result.as_of_iso,
        'week_anchor': result.week_anchor,
        'check': 'X',
        'outcome': result.outcome,
        'cutover_date': result.config.get('cutover_date',
                                          DEFAULT_CUTOVER_DATE),
        'window_days': result.config.get('window_days', DEFAULT_WINDOW_DAYS),
        'min_tasks_per_window': result.config.get(
            'min_tasks_per_window', DEFAULT_MIN_TASKS_PER_WINDOW),
        'windows': {
            'baseline': _window_dict(result.baseline),
            'trailing': _window_dict(result.trailing),
        },
        'active_thresholds_breached': result.breaches,
        'deferred_thresholds': result.deferred,
        'verdict_thresholds': result.verdict_thresholds,
        'rationale': result.rationale,
        'applied': False,
    }


def write_artifact(artifact: dict[str, Any]) -> Path:
    """Atomic write (tmp + replace)."""
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = artifact_path_for_week(artifact['week_anchor'])
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(artifact, indent=2))
    tmp.replace(path)
    return path


def _fmt(x: Optional[float]) -> str:
    return f'{x:.2f}' if isinstance(x, (int, float)) else '-'


def format_digest(artifact: dict[str, Any]) -> str:
    date_str = artifact['as_of'][:10]
    b = artifact['windows']['baseline']
    t = artifact['windows']['trailing']
    lines = [
        f'Check X - chain-quality regression suspected ({date_str})',
        '',
        'Since the model/prompt cutover on '
        f'{artifact["cutover_date"]}, the Forge/Mirror build chain shows a '
        'measurable rise on at least one quality signal:',
        '',
    ]
    for br in artifact['active_thresholds_breached']:
        metric = br['metric'].replace('_', ' ')
        if 'rel_increase' in br:
            # Relative-increase metric (clarify rounds, revision rounds).
            pct = br['rel_increase'] * 100
            lines.append(
                f'  - {metric}: {_fmt(br["baseline"])} -> '
                f'{_fmt(br["trailing"])} (+{pct:.0f}%, '
                f'threshold +{br["configured_min"] * 100:.0f}%)'
            )
        else:
            # Absolute-change metric (verdict-mix escalate/pass rate). Shown in
            # percentage points; pass_rate_abs_drop is a fall, escalate a rise.
            pp = (br.get('abs_change') or 0.0) * 100
            arrow = 'drop' if br['threshold'] == 'pass_rate_abs_drop' else 'rise'
            lines.append(
                f'  - {metric}: {_fmt(br["baseline"])} -> '
                f'{_fmt(br["trailing"])} ({pp:.0f}pp {arrow}, '
                f'threshold {br["configured_min"] * 100:.0f}pp)'
            )
    lines += [
        '',
        f'Windows compared: the {artifact["window_days"]} days before the '
        f'cutover (baseline: {b["distinct_tasks"]} tasks) vs the most '
        f'recent {artifact["window_days"]} days '
        f'(trailing: {t["distinct_tasks"]} tasks).',
        '',
        'This is CORRELATIONAL, not proven cause - the cutover happens to '
        'coincide with the rise. The Mirror verdict-mix signals (pass / '
        'escalate rate) are evaluated only when both windows have enough '
        'verdict history; otherwise they are reported as insufficient_signal.',
    ]
    return '\n'.join(lines)


def dm_digest(artifact: dict[str, Any]) -> bool:
    if artifact['outcome'] != 'regression_suspected':
        return False
    body = format_digest(artifact)
    date_str = artifact['as_of'][:10]
    action = (
        'Review the recent Forge/Mirror PRs from the trailing window. If '
        'the regression is real, revert the affected agent to '
        'claude-opus-4-7 (flip its routing value in '
        'config/agent-models.json + restart the inbox-watcher, which caches '
        'config at startup - inbox_watcher.py:778). No auto-action taken.'
    )
    try:
        import larry_alerts as la  # local import: tests can stub
        return la.append_alert(
            source='pulse-check-x',
            severity='warning',
            message=body,
            subject=f'check-x-regression:{date_str}',
            suggested_action=action,
        )
    except Exception as e:  # noqa: BLE001
        log(f'dm_digest failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- config IO --------------------


def load_config(path: Path = AGENT_MODELS_CONFIG) -> dict[str, Any]:
    """Read the check_x_regression block. Fail safe to defaults on any
    read/parse error or missing block."""
    defaults = {
        'cutover_date': DEFAULT_CUTOVER_DATE,
        'window_days': DEFAULT_WINDOW_DAYS,
        'min_tasks_per_window': DEFAULT_MIN_TASKS_PER_WINDOW,
        'clarify_rate_rel_increase': DEFAULT_CLARIFY_RATE_REL_INCREASE,
        'revision_rounds_rel_increase': DEFAULT_REVISION_ROUNDS_REL_INCREASE,
        'escalate_rate_abs_increase': DEFAULT_ESCALATE_RATE_ABS_INCREASE,
        'pass_rate_abs_drop': DEFAULT_PASS_RATE_ABS_DROP,
    }
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f'config read failed ({type(e).__name__}: {e}) - using '
            'defaults', 'WARN')
        return defaults
    block = data.get('check_x_regression')
    if not isinstance(block, dict):
        log('config missing check_x_regression - using defaults', 'WARN')
        return defaults
    merged = dict(defaults)
    for k in defaults:
        if k in block:
            merged[k] = block[k]
    return merged


# -------------------- substrate IO --------------------


def fetch_clarify_events_from_supabase(
    client, *, since: datetime,
) -> list[ClarifyEvent]:
    since_iso = since.isoformat()
    out: list[ClarifyEvent] = []
    page = 0
    page_size = 1000
    while True:
        res = (
            client.table('chain_events')
                  .select('event_type,ts,task_id,agent')
                  .eq('event_type', CLARIFY_EVENT_TYPE)
                  .gte('ts', since_iso)
                  .order('ts')
                  .range(page * page_size, (page + 1) * page_size - 1)
                  .execute()
        )
        rows = getattr(res, 'data', None) or []
        for row in rows:
            ts = _parse_ts(row.get('ts'))
            if ts is None:
                continue
            if (row.get('event_type') or '') != CLARIFY_EVENT_TYPE:
                continue
            agent = row.get('agent') or ''
            out.append(ClarifyEvent(
                ts=ts,
                task_id=row.get('task_id') or '',
                agent=agent if isinstance(agent, str) else '',
            ))
        if len(rows) < page_size:
            break
        page += 1
    return out


def fetch_verdict_events_from_supabase(
    client, *, since: datetime,
) -> list[VerdictEvent]:
    """Fetch Mirror verdict chain_events (review_pass/revision/escalate).

    Symmetric to fetch_clarify_events_from_supabase. event_type -> verdict
    bucket via VERDICT_EVENT_TYPES; rows with an unmapped type are skipped.
    """
    since_iso = since.isoformat()
    out: list[VerdictEvent] = []
    page = 0
    page_size = 1000
    types = list(VERDICT_EVENT_TYPES.keys())
    while True:
        res = (
            client.table('chain_events')
                  .select('event_type,ts,task_id,agent')
                  .in_('event_type', types)
                  .gte('ts', since_iso)
                  .order('ts')
                  .range(page * page_size, (page + 1) * page_size - 1)
                  .execute()
        )
        rows = getattr(res, 'data', None) or []
        for row in rows:
            ts = _parse_ts(row.get('ts'))
            if ts is None:
                continue
            verdict = VERDICT_EVENT_TYPES.get(row.get('event_type') or '')
            if verdict is None:
                continue
            agent = row.get('agent') or ''
            out.append(VerdictEvent(
                ts=ts,
                task_id=row.get('task_id') or '',
                verdict=verdict,
                agent=agent if isinstance(agent, str) else '',
            ))
        if len(rows) < page_size:
            break
        page += 1
    return out


def load_cost_rows(
    costs_path: Path = COSTS_FILE, *, since: datetime,
) -> list[CostRow]:
    if not costs_path.exists():
        return []
    out: list[CostRow] = []
    try:
        with open(costs_path, errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_ts(rec.get('ts'))
                if ts is None or ts < since:
                    continue
                out.append(CostRow(
                    ts=ts,
                    task_id=rec.get('task_id') or '',
                    agent=rec.get('agent') or '',
                    task_type=rec.get('task_type') or '',
                ))
    except OSError as e:
        log(f'read {costs_path} failed: {e}', 'WARN')
        return []
    return out


def _connect_supabase():
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing - '
            'cannot run Check X.'
        )
    from supabase import create_client  # type: ignore
    return create_client(url, key)


# -------------------- fixture loading (test path) --------------------


def _events_from_fixture(raw: dict[str, Any], now: datetime
                         ) -> tuple[list[ClarifyEvent], list[CostRow]]:
    clarify_events = [
        ClarifyEvent(
            ts=_parse_ts(e.get('ts')) or now,
            task_id=e.get('task_id', ''),
            agent=e.get('agent', FORGE_AGENT),
        )
        for e in raw.get('clarify_events', [])
    ]
    cost_rows = [
        CostRow(
            ts=_parse_ts(c.get('ts')) or now,
            task_id=c.get('task_id', ''),
            agent=c.get('agent', ''),
            task_type=c.get('task_type', ''),
        )
        for c in raw.get('costs', [])
    ]
    return clarify_events, cost_rows


def _verdict_events_from_fixture(raw: dict[str, Any], now: datetime
                                 ) -> list[VerdictEvent]:
    """Parse the optional `verdict_events` fixture key into VerdictEvents.

    Kept separate from `_events_from_fixture` so the latter's 2-tuple return
    shape (and its callers/tests) stays stable. Each entry may carry either a
    `verdict` bucket ('pass'/'revision'/'escalate') or an `event_type`
    (review_pass/...) which is mapped via VERDICT_EVENT_TYPES.
    """
    out: list[VerdictEvent] = []
    for e in raw.get('verdict_events', []):
        verdict = e.get('verdict')
        if verdict is None:
            verdict = VERDICT_EVENT_TYPES.get(e.get('event_type') or '')
        if verdict not in ('pass', 'revision', 'escalate'):
            continue
        out.append(VerdictEvent(
            ts=_parse_ts(e.get('ts')) or now,
            task_id=e.get('task_id', ''),
            verdict=verdict,
            agent=e.get('agent', MIRROR_AGENT),
        ))
    return out


# -------------------- main --------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-json',
                        help='Read clarify_events + costs from a JSON '
                             'fixture instead of querying chain_events / '
                             'costs.jsonl. May also carry "now" and config '
                             'overrides for deterministic tests.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute + print but do not write artifact or '
                             'send DM.')
    parser.add_argument('--force', action='store_true',
                        help='Bypass weekly idempotency.')
    args = parser.parse_args(argv)

    if args.from_json:
        with open(args.from_json) as fh:
            raw = json.load(fh)
        now = _parse_ts(raw.get('now')) or datetime.now(timezone.utc)
        config = load_config()
        for k in (
            'cutover_date', 'window_days', 'min_tasks_per_window',
            'clarify_rate_rel_increase', 'revision_rounds_rel_increase',
            'escalate_rate_abs_increase', 'pass_rate_abs_drop',
        ):
            if k in raw:
                config[k] = raw[k]
        clarify_events, cost_rows = _events_from_fixture(raw, now)
        verdict_events = _verdict_events_from_fixture(raw, now)
    else:
        now = datetime.now(timezone.utc)
        config = load_config()

    week_anchor = iso_week_monday(now.date())
    target_path = artifact_path_for_week(week_anchor)

    # Sentinel-cum-artifact gate: same-week artifact -> skip.
    if (target_path.exists() and not args.force and not args.dry_run
            and not args.from_json):
        log(f'Check X already ran this week ({week_anchor}); skipping '
            '(use --force to re-run).')
        return 0

    if not args.from_json:
        window_days = int(config.get('window_days', DEFAULT_WINDOW_DAYS))
        cutover = _parse_cutover(str(config.get('cutover_date',
                                                DEFAULT_CUTOVER_DATE)))
        # Earliest ts either window can need = baseline window start.
        since = min(now - timedelta(days=window_days),
                    cutover - timedelta(days=window_days))
        try:
            client = _connect_supabase()
            clarify_events = fetch_clarify_events_from_supabase(
                client, since=since)
            verdict_events = fetch_verdict_events_from_supabase(
                client, since=since)
        except Exception as e:
            log(f'cannot fetch chain_events: '
                f'{type(e).__name__}: {e}', 'ERROR')
            return 1
        cost_rows = load_cost_rows(since=since)

    result = run_check(
        clarify_events=clarify_events, cost_rows=cost_rows,
        verdict_events=verdict_events, config=config, now=now,
    )
    artifact = build_artifact(result)

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    write_artifact(artifact)
    dm_digest(artifact)
    log(f'Check X complete: outcome={result.outcome} '
        f'baseline_tasks={result.baseline.distinct_tasks} '
        f'trailing_tasks={result.trailing.distinct_tasks} '
        f'breaches={len(result.breaches)}')
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check
    sys.exit(run_check('x', main, log_fn=log))
