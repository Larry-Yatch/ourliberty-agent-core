#!/usr/bin/env python3
"""pulse_check_viii.py — weekly burn-rate-signal validity analyzer.

Check VIII PR-2b. Spec: docs/check-viii-burn-rate-signal-brief.md § 2 PR-2b.

PR-2a (#160) shipped the ground-truth ledger (anthropic-quota-events.jsonl)
plus the burn-rate DM body reframe. PR-2b adds this analyzer: weekly cadence,
fires from /cycle on Mondays alongside Check I, observes the ledger over a
trailing 4w window, and proposes threshold adjustments per doctrine #48
(self-optimizing config via Pulse Check pattern).

2026-05-28 re-base: the burn-rate healer now meters a quota-consuming
token-volume proxy (input+output+cache_creation) against
`tier1_quota.max_5h_token_threshold`, not a dollar sum against
`max_5h_spend_threshold_usd`. This analyzer parses the new DM body shape
and proposes token-denominated raise/lower thresholds.

Inputs (read-only, no LLM calls):

  - `~/agents/blackboard/larry-alerts.jsonl` entries where
    `source == 'heal-claude-max-burn-rate'` over the trailing 4w (28d).
  - `~/agents/blackboard/anthropic-quota-events.jsonl` events over the
    trailing 4w (28d) plus an 8w (56d) lookback for the deprecate rule.
  - `~/agents/blackboard/costs.jsonl` for rolling-5h token volume at
    FN-event timestamps (used to compute the LOWER proposal target).
  - `config/agent-models.json:tier1_quota.max_5h_token_threshold` as
    the current threshold.

Classification (per spec):

  - For each burn-rate DM: TP if a quota-event ts falls within 2h *after*
    the DM ts; FP otherwise.
  - For each quota-event: FN if no burn-rate DM in the 2h *before* the
    event ts.
  - Fixture-pattern task_ids on quota events are dropped via
    `fixture_patterns.is_fixture_task_id` BEFORE counting.
  - precision = TP / (TP + FP); recall = TP / (TP + FN).

Proposal-firing rules (priority: deprecate > defer > raise > lower):

  - **deprecate** — TP == 0 across trailing 8w with >= 5 quota-events in
    that 8w window. Signal has no observable predictive value; propose
    removing the token gate.
  - **defer** — precision < 0.4 (>= 5 DMs) AND recall < 0.6 (>= 3 events)
    both fire. DM Larry the metric tension; emit no proposal artifact.
  - **raise** — precision < 0.4 with >= 5 DMs. Propose raising threshold
    to 75th-percentile token usage at TP events, or +20% if no TPs.
  - **lower** — recall < 0.6 with >= 3 quota-events. Propose lowering
    threshold to 75th-percentile rolling-5h token volume at FN events, or
    -20% if no FNs.
  - **insufficient_signal** — DM_count < 5 OR event_count < 3 (4w window).
    No proposal, no DM.
  - **none** — none of the above; healer is well-calibrated this week.

Outputs (per spec § 2 PR-2b step 5-step pattern):

  - Artifact `~/agents/blackboard/pulse-check-viii-proposals/check-viii-<monday>.json`.
  - DM via `larry_alerts.append_alert` with `source='pulse-check-viii'`.
    Severity 'warning' for raise/lower/defer/insufficient; 'critical' for
    deprecate (matches doctrine — deprecate-class signals warrant the
    shorter cooldown bucket).

The analyzer NEVER edits config/agent-models.json. Approval flow:
artifact -> DM -> Larry `approve check-viii-update-<date>` -> Beacon
-> Claude-as-Forge PR -> Mirror -> merge -> Beacon flips `applied: true`.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
REPO_ROOT = Path(__file__).resolve().parent.parent
ALERTS_FILE = AGENTS_ROOT / 'blackboard' / 'larry-alerts.jsonl'
QUOTA_EVENTS_FILE = AGENTS_ROOT / 'blackboard' / 'anthropic-quota-events.jsonl'
COSTS_FILE = AGENTS_ROOT / 'blackboard' / 'costs.jsonl'
PROPOSALS_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-viii-proposals'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-viii.log'
AGENT_MODELS_CONFIG = REPO_ROOT / 'config' / 'agent-models.json'

LOOKBACK_DAYS = 28                  # trailing 4w main analysis window
DEPRECATE_LOOKBACK_DAYS = 56        # trailing 8w deprecate-rule window
CLASSIFICATION_WINDOW_HOURS = 2     # TP/FN proximity gate
COSTS_ROLLING_WINDOW_HOURS = 5      # matches healer's 5h rolling spend

MIN_DMS_FOR_PRECISION = 5
MIN_EVENTS_FOR_RECALL = 3
MIN_EVENTS_FOR_DEPRECATE = 5
PRECISION_FLOOR = 0.4
RECALL_FLOOR = 0.6
RAISE_FALLBACK_PCT = 0.20           # +20% if no TPs
LOWER_FALLBACK_PCT = 0.20           # -20% if no FNs

DEFAULT_THRESHOLD_TOKENS = 10_000_000  # fallback when config unreadable

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
class BurnRateDM:
    ts: datetime
    usage_tokens: int
    threshold_tokens: int


@dataclass
class QuotaEvent:
    ts: datetime
    task_id: str
    agent: str


@dataclass
class CostEntry:
    ts: datetime
    usage_tokens: int


# DM body shape from heal_claude_max_burn_rate.py (post 2026-05-28 re-base):
#   "...token gate (8,432,109 of 10,000,000 tokens; ...)"
# Commas are allowed in either number so the human-readable body parses cleanly.
_DM_USAGE_RE = re.compile(r'\(([\d,]+)\s+of\s+([\d,]+)\s+tokens')


def _parse_ts(ts_str: Any) -> Optional[datetime]:
    if not isinstance(ts_str, str) or not ts_str:
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


def parse_dm_body(body: str) -> Optional[tuple[int, int]]:
    """Extract (usage_tokens, threshold_tokens) from a burn-rate DM body.
    Returns None if the body doesn't match the expected shape (e.g. older
    pre-re-base record with dollar denomination)."""
    if not isinstance(body, str):
        return None
    m = _DM_USAGE_RE.search(body)
    if not m:
        return None
    try:
        usage = int(m.group(1).replace(',', ''))
        threshold = int(m.group(2).replace(',', ''))
        return usage, threshold
    except ValueError:
        return None


def load_burn_rate_dms(
    alerts_path: Path = ALERTS_FILE,
    *,
    now: Optional[datetime] = None,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[BurnRateDM]:
    """Read larry-alerts.jsonl, return parseable burn-rate-DM records in window."""
    if not alerts_path.exists():
        return []
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    out: list[BurnRateDM] = []
    try:
        with open(alerts_path, errors='replace') as fh:
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
                if rec.get('source') != 'heal-claude-max-burn-rate':
                    continue
                ts = _parse_ts(rec.get('ts'))
                if ts is None or ts < cutoff:
                    continue
                parsed = parse_dm_body(rec.get('message', ''))
                if parsed is None:
                    continue
                usage, threshold = parsed
                out.append(BurnRateDM(ts=ts, usage_tokens=usage,
                                      threshold_tokens=threshold))
    except OSError as e:
        log(f'read {alerts_path} failed: {e}', 'WARN')
        return []
    return out


def load_quota_events(
    events_path: Path = QUOTA_EVENTS_FILE,
    *,
    now: Optional[datetime] = None,
    lookback_days: int = DEPRECATE_LOOKBACK_DAYS,
) -> list[QuotaEvent]:
    """Read anthropic-quota-events.jsonl, return records in the deepest window
    (deprecate uses 8w; the 4w window is derived from this by ts filtering)."""
    if not events_path.exists():
        return []
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    out: list[QuotaEvent] = []
    try:
        with open(events_path, errors='replace') as fh:
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
                if ts is None or ts < cutoff:
                    continue
                task_id = rec.get('task_id') or ''
                if is_fixture_task_id(task_id):
                    continue
                out.append(QuotaEvent(
                    ts=ts,
                    task_id=task_id if isinstance(task_id, str) else '',
                    agent=rec.get('agent', '') if isinstance(rec.get('agent'), str) else '',
                ))
    except OSError as e:
        log(f'read {events_path} failed: {e}', 'WARN')
        return []
    return out


def load_cost_entries(
    costs_path: Path = COSTS_FILE,
    *,
    now: Optional[datetime] = None,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[CostEntry]:
    """Read costs.jsonl entries inside the trailing window (used to compute
    rolling-5h token volume at FN-event timestamps). Mirrors the healer's
    token-volume definition: input + output + cache_creation. cache_read
    is excluded — it's the largely-free cached re-read and is not the
    right quota signal."""
    if not costs_path.exists():
        return []
    if now is None:
        now = datetime.now(timezone.utc)
    # Pad cutoff by the 5h window so a FN event near the edge can still
    # see the tokens that contributed to its rolling sum.
    cutoff = now - timedelta(days=lookback_days, hours=COSTS_ROLLING_WINDOW_HOURS)
    out: list[CostEntry] = []
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
                if ts is None or ts < cutoff:
                    continue
                tokens = 0
                for key in ('input_tokens', 'output_tokens', 'cache_creation'):
                    v = rec.get(key, 0)
                    if isinstance(v, (int, float)) and v > 0:
                        tokens += int(v)
                if tokens == 0:
                    continue
                out.append(CostEntry(ts=ts, usage_tokens=tokens))
    except OSError as e:
        log(f'read {costs_path} failed: {e}', 'WARN')
        return []
    return out


def rolling_5h_tokens_at(
    target_ts: datetime,
    costs: Iterable[CostEntry],
    *,
    window_hours: int = COSTS_ROLLING_WINDOW_HOURS,
) -> int:
    """Sum quota-consuming tokens over [target_ts - window, target_ts]. Used
    to estimate the volume the healer would have seen at the moment of an
    FN event (no DM in the 2h before, so we read costs.jsonl directly to
    learn what usage the healer would have summed)."""
    window_start = target_ts - timedelta(hours=window_hours)
    return sum(
        c.usage_tokens for c in costs
        if window_start <= c.ts <= target_ts
    )


def compute_percentile(values: list[float], percentile: float) -> float:
    """Linear-interpolation percentile. Empty -> 0.0."""
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


def classify_dms(
    dms: list[BurnRateDM],
    events: list[QuotaEvent],
    *,
    window_hours: int = CLASSIFICATION_WINDOW_HOURS,
) -> tuple[list[BurnRateDM], list[BurnRateDM]]:
    """Partition DMs into (true_positives, false_positives).

    TP iff at least one quota-event ts falls within [dm.ts, dm.ts + window].
    """
    tps: list[BurnRateDM] = []
    fps: list[BurnRateDM] = []
    for dm in dms:
        upper = dm.ts + timedelta(hours=window_hours)
        if any(dm.ts <= ev.ts <= upper for ev in events):
            tps.append(dm)
        else:
            fps.append(dm)
    return tps, fps


def classify_events(
    dms: list[BurnRateDM],
    events: list[QuotaEvent],
    *,
    window_hours: int = CLASSIFICATION_WINDOW_HOURS,
) -> list[QuotaEvent]:
    """Return the subset of events that are false negatives (no DM in the
    window_hours BEFORE the event)."""
    fns: list[QuotaEvent] = []
    for ev in events:
        lower = ev.ts - timedelta(hours=window_hours)
        if not any(lower <= dm.ts <= ev.ts for dm in dms):
            fns.append(ev)
    return fns


# -------------------- proposal computation --------------------


@dataclass
class ProposalResult:
    """Output of run_check. `rule_fired` is one of:
      raise, lower, deprecate, defer, insufficient_signal, none.

    Threshold values are token counts (post 2026-05-28 re-base) — the field
    names stay unit-agnostic so the artifact schema doesn't churn.
    """
    rule_fired: str
    current_threshold_tokens: int
    proposed_threshold_tokens: Optional[int]   # None when no config change
    tp_count: int
    fp_count: int
    fn_count: int
    dm_count: int
    event_count: int
    precision: Optional[float]                 # None when undefined (TP+FP==0)
    recall: Optional[float]                    # None when undefined (TP+FN==0)
    rationale: str
    as_of_iso: str
    week_anchor: str                            # ISO Monday date YYYY-MM-DD
    # Diagnostic stats used by rules + tests.
    tp_count_8w: int = 0
    event_count_8w: int = 0


def _safe_ratio(numer: int, denom: int) -> Optional[float]:
    if denom <= 0:
        return None
    return numer / denom


def _filter_dms_window(
    dms: list[BurnRateDM], window_start: datetime,
) -> list[BurnRateDM]:
    return [d for d in dms if d.ts >= window_start]


def _filter_events_window(
    events: list[QuotaEvent], window_start: datetime,
) -> list[QuotaEvent]:
    return [e for e in events if e.ts >= window_start]


def iso_week_monday(d: date) -> str:
    """Return YYYY-MM-DD of the Monday of the ISO week containing d."""
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def run_check(
    *,
    dms: list[BurnRateDM],
    events: list[QuotaEvent],
    costs: Optional[list[CostEntry]] = None,
    current_threshold_tokens: int,
    gate_enabled: bool = True,
    now: Optional[datetime] = None,
    lookback_days: int = LOOKBACK_DAYS,
    deprecate_lookback_days: int = DEPRECATE_LOOKBACK_DAYS,
) -> ProposalResult:
    """Pure-function entry point — given inputs, returns a ProposalResult.

    Used directly by tests; CLI loads inputs from JSONL files + the
    agent-models.json config block.
    """
    costs = costs or []
    if now is None:
        now = datetime.now(timezone.utc)

    window_4w_start = now - timedelta(days=lookback_days)
    window_8w_start = now - timedelta(days=deprecate_lookback_days)

    dms_4w = _filter_dms_window(dms, window_4w_start)
    events_4w = _filter_events_window(events, window_4w_start)
    events_8w = _filter_events_window(events, window_8w_start)
    dms_8w = _filter_dms_window(dms, window_8w_start)

    # 4w classification drives precision/recall.
    tps, fps = classify_dms(dms_4w, events_4w)
    fns = classify_events(dms_4w, events_4w)
    tp_count = len(tps)
    fp_count = len(fps)
    fn_count = len(fns)
    dm_count = len(dms_4w)
    event_count = len(events_4w)

    precision = _safe_ratio(tp_count, tp_count + fp_count)
    recall = _safe_ratio(tp_count, tp_count + fn_count)

    # 8w stats for the deprecate rule.
    tps_8w, _ = classify_dms(dms_8w, events_8w)
    tp_count_8w = len(tps_8w)
    event_count_8w = len(events_8w)

    week_anchor = iso_week_monday(now.date())
    as_of_iso = now.isoformat()

    def _result(
        rule: str,
        *,
        proposed: Optional[int],
        rationale: str,
    ) -> ProposalResult:
        return ProposalResult(
            rule_fired=rule,
            current_threshold_tokens=current_threshold_tokens,
            proposed_threshold_tokens=proposed,
            tp_count=tp_count, fp_count=fp_count, fn_count=fn_count,
            dm_count=dm_count, event_count=event_count,
            precision=precision, recall=recall,
            rationale=rationale,
            as_of_iso=as_of_iso, week_anchor=week_anchor,
            tp_count_8w=tp_count_8w, event_count_8w=event_count_8w,
        )

    # 0. Already deprecated — precedes ALL rules.
    # A disabled gate emits no burn-rate DMs, so TP is structurally 0 forever
    # while quota-events keep flowing: the deprecate rule would re-fire every
    # cycle, re-proposing a terminal action that has already been applied.
    # With the gate off there is no live signal to tune (no DMs) and nothing
    # left to deprecate, so none of deprecate/insufficient_signal/defer/raise/
    # lower are meaningful. Short-circuit to a no-DM, no-proposal result.
    if gate_enabled is False:
        rationale = (
            'Token gate is disabled (tier1_quota.enabled=false): the healer '
            'emits no burn-rate DMs, so there is no live signal to tune and '
            'nothing to deprecate. No proposal this cycle.'
        )
        return _result('already_deprecated', proposed=None, rationale=rationale)

    # 1. Deprecate (highest priority).
    if tp_count_8w == 0 and event_count_8w >= MIN_EVENTS_FOR_DEPRECATE:
        rationale = (
            f'TP=0 across trailing 8w with {event_count_8w} quota-events: '
            'token gate has no observable predictive value. '
            'Propose deprecating the gate entirely.'
        )
        return _result('deprecate', proposed=None, rationale=rationale)

    # 2. Insufficient signal.
    if dm_count < MIN_DMS_FOR_PRECISION or event_count < MIN_EVENTS_FOR_RECALL:
        rationale = (
            f'Insufficient signal: DM_count={dm_count} '
            f'(floor {MIN_DMS_FOR_PRECISION}), '
            f'event_count={event_count} (floor {MIN_EVENTS_FOR_RECALL}). '
            'No proposal this cycle.'
        )
        return _result('insufficient_signal', proposed=None, rationale=rationale)

    # 3. Defer (raise + lower both fire — metric tension).
    raise_fires = (
        precision is not None and precision < PRECISION_FLOOR
        and dm_count >= MIN_DMS_FOR_PRECISION
    )
    lower_fires = (
        recall is not None and recall < RECALL_FLOOR
        and event_count >= MIN_EVENTS_FOR_RECALL
    )
    if raise_fires and lower_fires:
        rationale = (
            f'Metric tension: precision={precision:.2f} '
            f'(<{PRECISION_FLOOR}) AND recall={recall:.2f} '
            f'(<{RECALL_FLOOR}). DMs both alert on non-events AND miss '
            f'real events. TP={tp_count}, FP={fp_count}, FN={fn_count}. '
            'Defer — no automatic proposal; needs manual interpretation.'
        )
        return _result('defer', proposed=None, rationale=rationale)

    # 4. Raise.
    if raise_fires:
        tp_usages = [float(tp.usage_tokens) for tp in tps]
        if tp_usages:
            proposed = int(round(compute_percentile(tp_usages, 0.75)))
            target_source = f'75th-pct usage at {len(tp_usages)} TP DM(s)'
        else:
            proposed = int(round(current_threshold_tokens * (1 + RAISE_FALLBACK_PCT)))
            target_source = f'+{int(RAISE_FALLBACK_PCT * 100)}% (no TPs)'
        rationale = (
            f'precision={precision:.2f} (<{PRECISION_FLOOR}) over '
            f'{dm_count} DMs: too many FP alerts. Raise '
            f'{current_threshold_tokens:,} -> {proposed:,} tokens '
            f'({target_source}).'
        )
        return _result('raise', proposed=proposed, rationale=rationale)

    # 5. Lower.
    if lower_fires:
        fn_usages: list[float] = []
        for fn in fns:
            t = rolling_5h_tokens_at(fn.ts, costs)
            if t > 0:
                fn_usages.append(float(t))
        if fn_usages:
            proposed = int(round(compute_percentile(fn_usages, 0.75)))
            target_source = (
                f'75th-pct rolling-5h usage at {len(fn_usages)} '
                f'FN event(s) (of {fn_count})'
            )
        else:
            proposed = int(round(current_threshold_tokens * (1 - LOWER_FALLBACK_PCT)))
            target_source = (
                f'-{int(LOWER_FALLBACK_PCT * 100)}% '
                '(no usage data at FN events)'
            )
        rationale = (
            f'recall={recall:.2f} (<{RECALL_FLOOR}) over '
            f'{event_count} events: too many FN events slip past the gate. '
            f'Lower {current_threshold_tokens:,} -> {proposed:,} tokens '
            f'({target_source}).'
        )
        return _result('lower', proposed=proposed, rationale=rationale)

    # 6. None — healer is calibrated this week.
    rationale = (
        f'precision={precision:.2f}, recall={recall:.2f}, '
        f'TP={tp_count}, FP={fp_count}, FN={fn_count}. '
        'Within bounds — no proposal.'
    )
    return _result('none', proposed=None, rationale=rationale)


# -------------------- artifact + DM emission --------------------


def build_artifact(result: ProposalResult) -> dict[str, Any]:
    return {
        'as_of': result.as_of_iso,
        'week_anchor': result.week_anchor,
        'rule_fired': result.rule_fired,
        'unit': 'tokens',
        'current_threshold': result.current_threshold_tokens,
        'proposed_threshold': result.proposed_threshold_tokens,
        'sample_sizes': {
            'TP': result.tp_count,
            'FP': result.fp_count,
            'FN': result.fn_count,
            'DM_count': result.dm_count,
            'event_count': result.event_count,
            'TP_8w': result.tp_count_8w,
            'event_count_8w': result.event_count_8w,
        },
        'precision': (round(result.precision, 4)
                      if result.precision is not None else None),
        'recall': (round(result.recall, 4)
                   if result.recall is not None else None),
        'rationale': result.rationale,
        'applied': False,
    }


def artifact_path_for_week(week_anchor: str) -> Path:
    return PROPOSALS_DIR / f'check-viii-{week_anchor}.json'


def write_artifact(artifact: dict[str, Any]) -> Path:
    # Audit #7: this file doubles as the same-week idempotency sentinel (see
    # the target_path.exists() gate in main()). A non-atomic write_text left a
    # truncated artifact on a mid-write crash that still satisfied .exists(),
    # permanently suppressing Check VIII for that ISO week. Write atomically so
    # a crash leaves either the intact prior artifact or the intact new one.
    path = artifact_path_for_week(artifact['week_anchor'])
    atomic_write_json(path, artifact, indent=2)
    return path


def _artifact_is_valid_sentinel(path: Path) -> bool:
    """True if ``path`` holds a parseable artifact (a real same-week sentinel).

    A zero-byte or truncated/corrupt file satisfies ``path.exists()`` but is not
    a valid completion marker; treat it as 'not yet run' so the check
    regenerates it instead of being suppressed for the week (audit #7).
    """
    try:
        obj = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(obj, dict) and bool(obj.get('week_anchor'))


def format_digest(artifact: dict[str, Any]) -> str:
    rule = artifact['rule_fired']
    ss = artifact['sample_sizes']
    current = artifact.get('current_threshold')
    proposed = artifact.get('proposed_threshold')
    date_str = artifact['as_of'][:10]

    def _amt(x: Optional[float]) -> str:
        return f'{int(x):,} tokens' if isinstance(x, (int, float)) else '—'

    header_map = {
        'raise': 'Check VIII — propose RAISE burn-rate token gate',
        'lower': 'Check VIII — propose LOWER burn-rate token gate',
        'deprecate': 'Check VIII — propose DEPRECATE burn-rate token gate',
        'defer': 'Check VIII — metric tension (defer, no proposal)',
        'insufficient_signal': 'Check VIII — insufficient signal',
        'none': 'Check VIII — no proposal (healer calibrated)',
        'already_deprecated': 'Check VIII — gate already deprecated (no proposal)',
    }
    header = header_map.get(rule, f'Check VIII — {rule}')

    lines = [
        f'{header} ({date_str})',
        '',
        f'current: {_amt(current)}'
        + (f' -> proposed: {_amt(proposed)}'
           if proposed is not None else ''),
        f'samples: TP={ss["TP"]}, FP={ss["FP"]}, FN={ss["FN"]}, '
        f'DMs={ss["DM_count"]}, events={ss["event_count"]} (4w)',
    ]
    precision = artifact.get('precision')
    recall = artifact.get('recall')
    if precision is not None or recall is not None:
        lines.append(
            'precision=' + (f'{precision:.2f}' if precision is not None else '—')
            + ', recall=' + (f'{recall:.2f}' if recall is not None else '—')
        )
    lines.append('')
    lines.append(artifact['rationale'])
    if rule in ('raise', 'lower', 'deprecate'):
        lines.append('')
        lines.append(
            f'Approve: reply `approve check-viii-update-{date_str}` on '
            f'Telegram, or `reject check-viii-update-{date_str} <reason>`.'
        )
    return '\n'.join(lines)


def _severity_for_rule(rule: str) -> str:
    return 'critical' if rule == 'deprecate' else 'warning'


def dm_digest(artifact: dict[str, Any]) -> bool:
    rule = artifact['rule_fired']
    if rule in ('none', 'insufficient_signal', 'already_deprecated'):
        # No-op rules don't DM — they only land in the artifact file.
        return False
    body = format_digest(artifact)
    date_str = artifact['as_of'][:10]
    try:
        import larry_alerts as la  # noqa: E402
        if rule == 'defer':
            subject = f'check-viii-defer:{date_str}'
            action = (
                'Manual interpretation needed: precision AND recall both '
                'below floors this week. Read the artifact and decide '
                'whether to raise, lower, or hold.'
            )
        else:
            subject = f'check-viii-update:{date_str}'
            action = (
                f'Review proposal; reply `approve check-viii-update-'
                f'{date_str}` on Telegram, or `reject check-viii-update-'
                f'{date_str} <reason>`.'
            )
        return la.append_alert(
            source='pulse-check-viii',
            severity=_severity_for_rule(rule),
            message=body,
            subject=subject,
            suggested_action=action,
        )
    except Exception as e:
        log(f'dm_digest failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- config IO --------------------


def load_current_threshold(path: Path = AGENT_MODELS_CONFIG) -> int:
    """Read tier1_quota.max_5h_token_threshold. Fail safe to the
    DEFAULT_THRESHOLD_TOKENS on any read/parse error."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f'config read failed ({type(e).__name__}: {e}) — '
            f'using default {DEFAULT_THRESHOLD_TOKENS} tokens', 'WARN')
        return DEFAULT_THRESHOLD_TOKENS
    block = data.get('tier1_quota')
    if not isinstance(block, dict):
        log(f'config missing tier1_quota — using default '
            f'{DEFAULT_THRESHOLD_TOKENS} tokens', 'WARN')
        return DEFAULT_THRESHOLD_TOKENS
    raw = block.get('max_5h_token_threshold')
    if not isinstance(raw, (int, float)) or raw <= 0:
        log(f'config tier1_quota.max_5h_token_threshold invalid '
            f'({raw!r}) — using default {DEFAULT_THRESHOLD_TOKENS} tokens',
            'WARN')
        return DEFAULT_THRESHOLD_TOKENS
    return int(raw)


def load_gate_enabled(path: Path = AGENT_MODELS_CONFIG) -> bool:
    """Read tier1_quota.enabled. Fail SAFE to True on any read/parse error,
    a missing block, or an absent key.

    Defaulting True preserves the pre-existing behavior: it never accidentally
    SUPPRESSES a genuine deprecate on a config read error, and an
    accidentally-deleted `enabled` field can't silently blind the check. Only
    an explicit `enabled: false` disables the gate (mirrors
    heal_claude_max_burn_rate.gate_enabled)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f'gate-enabled config read failed ({type(e).__name__}: {e}) — '
            'assuming enabled', 'WARN')
        return True
    block = data.get('tier1_quota')
    if not isinstance(block, dict):
        return True
    return block.get('enabled', True) is not False


# -------------------- main --------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',
                        help='Read DMs/events/costs from a JSON fixture '
                             'file instead of the live blackboard.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute + print but do not write artifact or '
                             'send DM.')
    parser.add_argument('--force', action='store_true',
                        help='Bypass weekly idempotency (re-run for the '
                             'current ISO-week Monday).')
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    week_anchor = iso_week_monday(now.date())
    target_path = artifact_path_for_week(week_anchor)

    if (target_path.exists() and _artifact_is_valid_sentinel(target_path)
            and not args.force and not args.dry_run):
        log(f'Check VIII already ran this week ({week_anchor}); '
            'skipping (use --force to re-run).')
        return 0

    current_threshold = load_current_threshold()
    gate_enabled = load_gate_enabled()

    if args.fixture:
        with open(args.fixture) as fh:
            raw = json.load(fh)
        gate_enabled = bool(raw.get('gate_enabled', True))
        dms = [
            BurnRateDM(
                ts=_parse_ts(d['ts']) or now,
                usage_tokens=int(d['usage_tokens']),
                threshold_tokens=int(d.get('threshold_tokens',
                                           current_threshold)),
            )
            for d in raw.get('dms', [])
        ]
        events = [
            QuotaEvent(
                ts=_parse_ts(e['ts']) or now,
                task_id=e.get('task_id', ''),
                agent=e.get('agent', ''),
            )
            for e in raw.get('events', [])
            if not is_fixture_task_id(e.get('task_id', ''))
        ]
        costs = [
            CostEntry(ts=_parse_ts(c['ts']) or now,
                      usage_tokens=int(c['usage_tokens']))
            for c in raw.get('costs', [])
        ]
    else:
        dms = load_burn_rate_dms(now=now)
        events = load_quota_events(now=now)
        costs = load_cost_entries(now=now)

    result = run_check(
        dms=dms, events=events, costs=costs,
        current_threshold_tokens=current_threshold,
        gate_enabled=gate_enabled, now=now,
    )
    artifact = build_artifact(result)

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    write_artifact(artifact)
    dm_digest(artifact)
    log(f'Check VIII complete: rule={result.rule_fired} '
        f'TP={result.tp_count} FP={result.fp_count} FN={result.fn_count} '
        f'DMs={result.dm_count} events={result.event_count}')
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check as _hb_run_check
    sys.exit(_hb_run_check('viii', main, log_fn=log))
