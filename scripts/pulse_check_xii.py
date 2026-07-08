#!/usr/bin/env python3
"""pulse_check_xii.py — delivery-effectiveness meter (Check XII, V1).

Spec: agents/beacon/specs/pulse-check-xii.md.

Check XII is the first *outcome-level* Pulse check: every other check meters
the machine (liveness, thresholds, markers, cost); XII meters the product —
how much shipped, how fast, how much rework, at what cost, split by whether
work was mission-linked (intentional delivery) or unlinked (self-maintenance).

V1 SCOPE — observe-only. This module ships:

  - the meter (this file), computing the §2 metric blocks over a trailing 4w
    window with a prior-4w rolling baseline (weeks 5-8), retro-computed on the
    first run from gh / chain_events / costs / missions;
  - the artifact + heartbeat;
  - the monthly-nominal digest DM + the source-dark warning DM;
  - a `rules` state block present but INERT (firing rules are V1.1, calibrated
    from the §7 backtest — this V1 never fires a rule).

Deterministic, stdlib-only core, no LLM. Four substrate sources
(github, chain_events, costs, missions) are read INDEPENDENTLY and each is
try/except'd: the artifact is ALWAYS written with a `sources` status block and
per-metric `insufficient_signal` where a source was dark. main() returns 0 on
EVERY partial-data and clean-skip path (so the heartbeat always fires and the
staleness watcher stays quiet); non-zero is reserved EXCLUSIVELY for "could not
write the artifact at all". A dark source is a 0-exit with sources.<name>=error,
never a page.

All chain_events reads are PAGINATED (lesson #795 — unpaginated selects
truncate at 1000 rows).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Optional

# ---- paths (honour the test-sandbox OURLIBERTY_AGENTS_ROOT redirect) --------
AGENTS_ROOT = Path(
    os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents'))
)
REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-xii'
COSTS_FILE = AGENTS_ROOT / 'blackboard' / 'costs.jsonl'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-xii.log'
AGENT_MODELS_CONFIG = REPO_ROOT / 'config' / 'agent-models.json'
# The single-committer canonical copy the dashboard reads (spec §2.2). Do NOT
# read the synced runtime copy ~/agents/agents/beacon/workspace/missions.json —
# it lags the committer.
MISSIONS_FILE = REPO_ROOT / 'agents' / 'beacon' / 'missions.json'

GH_OWNER = 'Larry-Yatch'
GH_TIMEOUT_S = 120

WINDOW_DAYS = 28              # trailing 4-week window
BASELINE_DAYS = 28           # prior 4-week window (weeks 5-8 back)
SURVIVAL_DAYS = 14           # §2.4 survival: merges untouched for 14 days
HOT_FILE_TOP_N = 20          # §2.4 hot-file denylist size
BASELINE_WEEKS_FOR_HOTFILE = 8

# Non-proposed mission phases whose task_ids count as "intentional delivery"
# for the §2.1 linkage split. A `proposed` mission is a garbage-collectable
# suggestion, not committed work.
NON_PROPOSED_PHASES = frozenset(
    {'drafting', 'building', 'shipped', 'deferred'}
)

# Factory-internal repos (the three repo_paths keys). Product repos (RSDPM etc.)
# are `product` repo_class — empty until the product repo exists, but the split
# is present so self-referentiality is visible the day it starts (§2.1).
FACTORY_REPO_CLASS = 'factory-internal'
PRODUCT_REPO_CLASS = 'product'

INSUFFICIENT = 'insufficient_signal'

# The four substrate sources, in artifact-report order.
SOURCES = ('github', 'chain_events', 'costs', 'missions')

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


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


# ============================ data records ============================


@dataclass
class Merge:
    """A merged PR (github fact, joined to a task_id via chain_events)."""
    pr_url: str
    number: Optional[int]
    repo: str                       # full GH repo name, e.g. ourliberty-agent-core
    merged_at: datetime
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    handsfree: bool = False         # merged via the forge/auto-merge pipeline
    task_id: Optional[str] = None   # resolved from chain_events; None if unlinked
    is_revert: bool = False
    is_fix_shaped: bool = False
    changed_files: list[str] = field(default_factory=list)


@dataclass
class ChainRow:
    """A generic chain_events row (event_type / ts / task_id / pr_url / agent)."""
    event_type: str
    ts: datetime
    task_id: str
    agent: str = ''
    pr_url: Optional[str] = None
    revision_count: Optional[int] = None


@dataclass
class CostRow:
    ts: datetime
    cost_usd: float
    task_id: str
    agent: str = ''
    model: str = ''


@dataclass
class Mission:
    id: str
    phase: str
    task_ids: list[str]
    created: Optional[date] = None
    shipped_at: Optional[datetime] = None


# ============================ pure helpers ============================


def _parse_ts(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Date-only precision (missions `created`, `shipped_at` whole-day).
        try:
            dt = datetime.strptime(s[:10], '%Y-%m-%d')
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_date(d: Any) -> Optional[date]:
    dt = _parse_ts(d)
    return dt.date() if dt is not None else None


def iso_week_monday(d: date) -> str:
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def artifact_path_for_date(as_of: datetime) -> Path:
    return ARTIFACT_DIR / f'check-xii-{as_of.date().isoformat()}.json'


def is_first_monday(d: date) -> bool:
    """True iff d is the first Monday of its month (monthly-digest gate)."""
    return d.weekday() == 0 and d.day <= 7


def _trend_pct(current: Optional[float], prior: Optional[float]) -> Any:
    """(current - prior) / prior * 100, or INSUFFICIENT when the prior window
    is not a usable (positive) baseline. A rolling trend off a zero/None prior
    is infinite / noise — report insufficient_signal, don't fabricate."""
    if not isinstance(current, (int, float)) or not isinstance(prior, (int, float)):
        return INSUFFICIENT
    if prior <= 0:
        return INSUFFICIENT
    return round((current - prior) / prior * 100.0, 1)


def _percentile(values: list[float], q: float) -> Optional[float]:
    """Nearest-rank percentile (q in [0,1]). None for an empty list."""
    if not values:
        return None
    if q <= 0:
        return min(values)
    ordered = sorted(values)
    if q >= 1:
        return ordered[-1]
    # nearest-rank
    rank = max(1, min(len(ordered), int(round(q * len(ordered) + 0.5))))
    return ordered[rank - 1]


def _block(current: Any, prior: Any, n: int, sources_ok: bool) -> dict[str, Any]:
    """The per-metric schema (§3): {current, prior, trend_pct, n, sources_ok}.

    trend_pct is computed only when both current and prior are scalar numbers
    AND the source(s) feeding the metric were ok; otherwise INSUFFICIENT (no
    schema migration needed in V1.1 — the shape is stable)."""
    if sources_ok and isinstance(current, (int, float)) \
            and isinstance(prior, (int, float)):
        trend = _trend_pct(current, prior)
    else:
        trend = INSUFFICIENT
    return {
        'current': current if sources_ok else INSUFFICIENT,
        'prior': prior if sources_ok else INSUFFICIENT,
        'trend_pct': trend,
        'n': n,
        'sources_ok': sources_ok,
    }


def _in_window(ts: datetime, start: datetime, end: datetime) -> bool:
    return start <= ts < end


# ============================ mission linkage ============================


def mission_task_ids(missions: list[Mission]) -> set[str]:
    """Union of task_ids across all NON-PROPOSED missions (§2.1). Membership
    of a merged PR's task_id in this set == intentional (mission-linked)
    delivery; absence == maintenance / healer churn."""
    out: set[str] = set()
    for m in missions:
        if m.phase in NON_PROPOSED_PHASES:
            out.update(t for t in m.task_ids if t)
    return out


# ============================ metric computation ============================


@dataclass
class Windows:
    trailing_start: datetime
    trailing_end: datetime
    prior_start: datetime
    prior_end: datetime

    @classmethod
    def ending(cls, now: datetime) -> 'Windows':
        t_end = now
        t_start = now - timedelta(days=WINDOW_DAYS)
        p_end = t_start
        p_start = t_start - timedelta(days=BASELINE_DAYS)
        return cls(t_start, t_end, p_start, p_end)


def _merges_in(merges: list[Merge], start: datetime, end: datetime) -> list[Merge]:
    return [m for m in merges if _in_window(m.merged_at, start, end)]


def throughput_metrics(
    merges: list[Merge], linked_ids: set[str], w: Windows, *,
    github_ok: bool, missions_ok: bool,
) -> dict[str, Any]:
    """§2.1 throughput with the substance split. Mission-linkage needs BOTH
    github (the merges) and missions (the linkage set); the size / handsfree /
    repo_class sub-metrics need only github."""
    trailing = _merges_in(merges, w.trailing_start, w.trailing_end)
    prior = _merges_in(merges, w.prior_start, w.prior_end)

    def _split(ms: list[Merge]) -> tuple[int, int]:
        linked = sum(1 for m in ms if m.task_id and m.task_id in linked_ids)
        return linked, len(ms) - linked

    t_linked, t_unlinked = _split(trailing)
    p_linked, p_unlinked = _split(prior)
    link_ok = github_ok and missions_ok

    def _repo_class(ms: list[Merge]) -> dict[str, int]:
        factory = sum(1 for m in ms if m.repo in _factory_repo_names())
        return {FACTORY_REPO_CLASS: factory,
                PRODUCT_REPO_CLASS: len(ms) - factory}

    def _size(ms: list[Merge]) -> dict[str, Any]:
        if not ms:
            return {'median_files': None, 'median_additions': None,
                    'median_deletions': None}
        return {
            'median_files': median(m.files_changed for m in ms),
            'median_additions': median(m.additions for m in ms),
            'median_deletions': median(m.deletions for m in ms),
        }

    def _handsfree(ms: list[Merge]) -> Optional[float]:
        if not ms:
            return None
        return round(sum(1 for m in ms if m.handsfree) / len(ms), 4)

    return {
        'merges_total': _block(len(trailing), len(prior), len(trailing),
                               github_ok),
        'merges_mission_linked': _block(t_linked, p_linked, t_linked, link_ok),
        'merges_unlinked': _block(t_unlinked, p_unlinked, t_unlinked, link_ok),
        'size_distribution': _block(_size(trailing), _size(prior),
                                    len(trailing), github_ok),
        'repo_class_split': _block(_repo_class(trailing), _repo_class(prior),
                                   len(trailing), github_ok),
        # Check XIII preview field (§2.1) — costs nothing to compute now.
        'handsfree_merge_share': _block(_handsfree(trailing),
                                        _handsfree(prior), len(trailing),
                                        github_ok),
    }


def lead_time_metrics(
    merges: list[Merge], chain: list[ChainRow], missions: list[Mission],
    w: Windows, *, github_ok: bool, chain_ok: bool, missions_ok: bool,
) -> dict[str, Any]:
    """§2.2 lead time. PR clock (dispatch->merge) is primary; the mission clock
    is a demoted diagnostic, honestly insufficient_signal most windows."""
    # earliest forge session_start per task_id (dispatch clock start).
    first_dispatch: dict[str, datetime] = {}
    for r in chain:
        if r.event_type == 'session_start' and r.agent == 'forge' and r.task_id:
            cur = first_dispatch.get(r.task_id)
            if cur is None or r.ts < cur:
                first_dispatch[r.task_id] = r.ts

    def _lead_hours(ms: list[Merge]) -> list[float]:
        out: list[float] = []
        for m in ms:
            if not m.task_id:
                continue
            start = first_dispatch.get(m.task_id)
            if start is None or m.merged_at < start:
                continue
            out.append((m.merged_at - start).total_seconds() / 3600.0)
        return out

    pr_ok = github_ok and chain_ok
    trailing_hours = _lead_hours(_merges_in(merges, w.trailing_start,
                                            w.trailing_end))
    prior_hours = _lead_hours(_merges_in(merges, w.prior_start, w.prior_end))

    def _p50(hours: list[float]) -> Any:
        return round(median(hours), 2) if len(hours) >= 5 else INSUFFICIENT

    def _p90(hours: list[float]) -> Any:
        # p90 from n<20 is just the sample max — report insufficient_n (§2.2).
        return (round(_percentile(hours, 0.90), 2) if len(hours) >= 20
                else 'insufficient_n')

    # Mission clock — created->shipped, whole-day, expected insufficient_signal.
    def _mission_lead_days(start: datetime, end: datetime) -> list[float]:
        out: list[float] = []
        for m in missions:
            if m.phase != 'shipped' or m.shipped_at is None or m.created is None:
                continue
            if not _in_window(m.shipped_at, start, end):
                continue
            days = (m.shipped_at.date() - m.created).days
            if days >= 0:
                out.append(float(days))
        return out

    mt = _mission_lead_days(w.trailing_start, w.trailing_end)
    mp = _mission_lead_days(w.prior_start, w.prior_end)
    mission_current = (round(median(mt), 1) if len(mt) >= 5 else INSUFFICIENT)
    mission_prior = (round(median(mp), 1) if len(mp) >= 5 else INSUFFICIENT)

    return {
        'dispatch_to_merge_p50_hours': _block(_p50(trailing_hours),
                                              _p50(prior_hours),
                                              len(trailing_hours), pr_ok),
        'dispatch_to_merge_p90_hours': _block(_p90(trailing_hours),
                                              _p90(prior_hours),
                                              len(trailing_hours), pr_ok),
        # Demoted diagnostic — includes operator queue dwell (dwell-subtraction
        # deferred in V1; labelled honestly per §2.2).
        'mission_wall_clock_days_incl_operator_queue': _block(
            mission_current, mission_prior, len(mt),
            missions_ok),
    }


def rework_metrics(
    merges: list[Merge], chain: list[ChainRow], w: Windows, *,
    chain_ok: bool, github_ok: bool,
) -> dict[str, Any]:
    """§2.3 rework, split so it doesn't punish good review.

    Firing-eligible (V1.1): Forge redispatch count = forge session_start rows
    per task_id minus 1. Diagnostic-only: Mirror review rounds = review_revision
    count per pr_url, reported PAIRED with escape/survival elsewhere."""
    def _redispatch(start: datetime, end: datetime) -> Optional[float]:
        starts: dict[str, int] = {}
        for r in chain:
            if (r.event_type == 'session_start' and r.agent == 'forge'
                    and r.task_id and _in_window(r.ts, start, end)):
                starts[r.task_id] = starts.get(r.task_id, 0) + 1
        if not starts:
            return None
        extra = sum(v - 1 for v in starts.values())
        return round(extra / len(starts), 4)

    def _review_rounds(ms: list[Merge]) -> Optional[float]:
        rounds = 0
        counted = 0
        by_pr: dict[str, int] = {}
        for r in chain:
            if r.event_type == 'review_revision' and r.pr_url:
                by_pr[r.pr_url] = by_pr.get(r.pr_url, 0) + 1
        for m in ms:
            if m.pr_url is None:
                continue
            counted += 1
            rounds += by_pr.get(m.pr_url, 0)
        return round(rounds / counted, 4) if counted else None

    t_redispatch = _redispatch(w.trailing_start, w.trailing_end)
    p_redispatch = _redispatch(w.prior_start, w.prior_end)
    t_rounds = _review_rounds(_merges_in(merges, w.trailing_start,
                                         w.trailing_end))
    p_rounds = _review_rounds(_merges_in(merges, w.prior_start, w.prior_end))

    return {
        'forge_redispatch_per_task': _block(t_redispatch, p_redispatch,
                                            0, chain_ok),
        'mirror_review_rounds_per_pr': _block(t_rounds, p_rounds, 0,
                                              chain_ok and github_ok),
    }


def escape_metrics(
    merges: list[Merge], w: Windows, *, github_ok: bool,
) -> dict[str, Any]:
    """§2.4 defect escape — survival framing + a de-noised hotfix proxy.

    Survival share: % of window merges untouched for SURVIVAL_DAYS. Hotfix
    proxy + reverts are diagnostic-only in V1 (base rate reported before any
    rule keys on them)."""
    def _survival(ms: list[Merge], end: datetime) -> Optional[float]:
        # Only merges old enough to have had SURVIVAL_DAYS to be touched.
        eligible = [m for m in ms
                    if m.merged_at + timedelta(days=SURVIVAL_DAYS) <= end]
        if not eligible:
            return None
        hot = _hot_file_denylist(merges)
        survived = 0
        for m in eligible:
            follow = _later_touch(m, merges, hot)
            if not follow:
                survived += 1
        return round(survived / len(eligible), 4)

    def _revert_share(ms: list[Merge]) -> Optional[float]:
        if not ms:
            return None
        return round(sum(1 for m in ms if m.is_revert) / len(ms), 4)

    trailing = _merges_in(merges, w.trailing_start, w.trailing_end)
    prior = _merges_in(merges, w.prior_start, w.prior_end)

    return {
        'survival_share_14d': _block(_survival(trailing, w.trailing_end),
                                     _survival(prior, w.prior_end),
                                     len(trailing), github_ok),
        'revert_share': _block(_revert_share(trailing), _revert_share(prior),
                               len(trailing), github_ok),
    }


def _factory_repo_names() -> set[str]:
    return set(load_repo_names())


def _hot_file_denylist(merges: list[Merge]) -> set[str]:
    """§2.4 hot-file denylist = top-N most-touched files over the trailing 8w
    baseline, recomputed each run. Machine-owned/auto-committed files are also
    excluded via KNOWN_AUTO_COMMITTED."""
    counts: dict[str, int] = {}
    for m in merges:
        for f in m.changed_files:
            counts[f] = counts.get(f, 0) + 1
    top = sorted(counts, key=lambda f: counts[f], reverse=True)[:HOT_FILE_TOP_N]
    return set(top) | KNOWN_AUTO_COMMITTED


KNOWN_AUTO_COMMITTED = frozenset({
    'agents/beacon/missions.json',
    'config/pulse-check-cadence.json',
    'config/alert-translations.json',
    'blackboard/costs.jsonl',
})


def _later_touch(m: Merge, merges: list[Merge], hot: set[str]) -> bool:
    """True if a LATER, fix-shaped PR re-modified any of m's non-hot files
    within SURVIVAL_DAYS — the de-noised hotfix proxy (§2.4)."""
    window_end = m.merged_at + timedelta(days=SURVIVAL_DAYS)
    candidate_files = set(m.changed_files) - hot
    if not candidate_files:
        return False
    for other in merges:
        if other.pr_url == m.pr_url:
            continue
        if not (m.merged_at < other.merged_at <= window_end):
            continue
        if not (other.is_revert or other.is_fix_shaped):
            continue
        if candidate_files & set(other.changed_files):
            return True
    return False


def cost_metrics(
    merges: list[Merge], costs: list[CostRow], missions: list[Mission],
    w: Windows, *, costs_ok: bool, github_ok: bool, missions_ok: bool,
) -> dict[str, Any]:
    """§2.5 cost — per-mission-shipped is the headline (the PR is a gameable
    cost unit; the orchestrator splits features into N PRs by choice)."""
    def _spend(start: datetime, end: datetime) -> float:
        return round(sum(c.cost_usd for c in costs
                         if _in_window(c.ts, start, end)), 4)

    def _missions_shipped(start: datetime, end: datetime) -> int:
        return sum(1 for m in missions
                   if m.phase == 'shipped' and m.shipped_at is not None
                   and _in_window(m.shipped_at, start, end))

    def _per_mission(start: datetime, end: datetime) -> Optional[float]:
        shipped = _missions_shipped(start, end)
        if shipped <= 0:
            return None
        return round(_spend(start, end) / shipped, 2)

    def _per_merge(ms: list[Merge], start: datetime, end: datetime
                   ) -> Optional[float]:
        if not ms:
            return None
        return round(_spend(start, end) / len(ms), 2)

    t_trailing = _merges_in(merges, w.trailing_start, w.trailing_end)
    p_prior = _merges_in(merges, w.prior_start, w.prior_end)

    # overhead_share: cost tags do not carry a clean build/review vs
    # ops-overhead attribution, so report `unknown` rather than folding an
    # 85%-false healer storm into delivery cost (§2.5).
    return {
        'cost_per_mission_shipped_usd': _block(
            _per_mission(w.trailing_start, w.trailing_end),
            _per_mission(w.prior_start, w.prior_end),
            _missions_shipped(w.trailing_start, w.trailing_end),
            costs_ok and missions_ok),
        'cost_per_merge_usd': _block(
            _per_merge(t_trailing, w.trailing_start, w.trailing_end),
            _per_merge(p_prior, w.prior_start, w.prior_end),
            len(t_trailing), costs_ok and github_ok),
        'overhead_share': _block('unknown', 'unknown', 0, False),
    }


def demand_metrics(
    missions: list[Mission], w: Windows, *, missions_ok: bool,
) -> dict[str, Any]:
    """§2.6 demand — the actual binding constraint (spec throughput). Every
    other metric improves when Larry writes fewer specs, so a purpose meter
    that can't see starvation would reward it."""
    def _registered(start: datetime, end: datetime) -> int:
        s, e = start.date(), end.date()
        return sum(1 for m in missions
                   if m.created is not None and s <= m.created < e)

    # Backlog depth = proposed/drafting missions not yet building/shipped.
    backlog = sum(1 for m in missions
                  if m.phase in ('proposed', 'drafting'))

    return {
        'missions_registered': _block(
            _registered(w.trailing_start, w.trailing_end),
            _registered(w.prior_start, w.prior_end),
            0, missions_ok),
        'backlog_depth': _block(backlog if missions_ok else INSUFFICIENT,
                                INSUFFICIENT, backlog, missions_ok),
        # forge dispatch-slot idle share (idle-while-backlog-empty vs
        # idle-while-backlog-exists) needs the board-drain / dispatch-slot
        # substrate not wired in V1 — honestly insufficient_signal.
        'forge_idle_starvation_share': _block(INSUFFICIENT, INSUFFICIENT, 0,
                                              False),
    }


def _inert_rules_block() -> dict[str, Any]:
    """§3/§4 — the `rules` state block is present from V1 but INERT (firing
    rules are V1.1, calibrated from the §7 backtest). Shape is stable so V1.1
    needs no schema migration."""
    planned = ('rule_1_rework', 'rule_2_lead_time', 'rule_3_escape',
               'rule_4_cost_per_mission', 'rule_5_throughput_collapse',
               'rule_6_demand_starvation')
    return {
        r: {
            'last_fired_at': None,
            'suppressed_until_below': False,
            'last_value': None,
            'armed_since': None,
        }
        for r in planned
    }


# ============================ orchestration ============================


@dataclass
class Substrate:
    merges: list[Merge]
    chain: list[ChainRow]
    costs: list[CostRow]
    missions: list[Mission]
    sources: dict[str, str]        # name -> 'ok' | 'error'


def analyze(sub: Substrate, now: datetime) -> dict[str, Any]:
    """Pure analysis: substrate -> full metric-block dict. Every metric block
    is present regardless of source health; a dark source yields
    insufficient_signal blocks via the sources_ok flags."""
    w = Windows.ending(now)
    github_ok = sub.sources['github'] == 'ok'
    chain_ok = sub.sources['chain_events'] == 'ok'
    costs_ok = sub.sources['costs'] == 'ok'
    missions_ok = sub.sources['missions'] == 'ok'

    linked_ids = mission_task_ids(sub.missions) if missions_ok else set()

    return {
        'throughput': throughput_metrics(
            sub.merges, linked_ids, w,
            github_ok=github_ok, missions_ok=missions_ok),
        'lead_time': lead_time_metrics(
            sub.merges, sub.chain, sub.missions, w,
            github_ok=github_ok, chain_ok=chain_ok, missions_ok=missions_ok),
        'rework': rework_metrics(
            sub.merges, sub.chain, w,
            chain_ok=chain_ok, github_ok=github_ok),
        'escape': escape_metrics(sub.merges, w, github_ok=github_ok),
        'cost': cost_metrics(
            sub.merges, sub.costs, sub.missions, w,
            costs_ok=costs_ok, github_ok=github_ok, missions_ok=missions_ok),
        'demand': demand_metrics(sub.missions, w, missions_ok=missions_ok),
    }


def build_artifact(sub: Substrate, now: datetime) -> dict[str, Any]:
    w = Windows.ending(now)
    metrics = analyze(sub, now)
    return {
        # as_of written in UTC — the CEO digest converts to Denver for display.
        'as_of': now.isoformat(),
        'check': 'XII',
        'window': {
            'trailing_start': w.trailing_start.isoformat(),
            'trailing_end': w.trailing_end.isoformat(),
            'prior_start': w.prior_start.isoformat(),
            'prior_end': w.prior_end.isoformat(),
        },
        'sources': dict(sub.sources),
        'metrics': metrics,
        'rules': _inert_rules_block(),
    }


def write_artifact(artifact: dict[str, Any], now: datetime) -> Path:
    """Atomic write (tmp + replace). Raises on failure — the caller maps that
    to the ONLY non-zero exit (could not write the artifact at all)."""
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    path = artifact_path_for_date(now)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(artifact, indent=2))
    tmp.replace(path)
    return path


def prune_old_artifacts(keep_weeks: int = 26) -> None:
    """Retention: keep the most recent `keep_weeks` artifacts; the family has
    no reaper, so XII prunes its own dir (§3). Never raises."""
    try:
        artifacts = sorted(ARTIFACT_DIR.glob('check-xii-*.json'))
    except OSError:
        return
    for old in artifacts[:-keep_weeks] if len(artifacts) > keep_weeks else []:
        try:
            old.unlink()
        except OSError:
            pass


def _read_prior_artifact(now: datetime) -> Optional[dict[str, Any]]:
    """The newest artifact strictly before today's, for the source-dark
    2-consecutive-run escalation gate."""
    try:
        paths = sorted(ARTIFACT_DIR.glob('check-xii-*.json'))
    except OSError:
        return None
    today = artifact_path_for_date(now)
    for p in reversed(paths):
        if p.name == today.name:
            continue
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return None


# ============================ substrate IO ============================


def load_repo_names(path: Path = AGENT_MODELS_CONFIG) -> list[str]:
    """Repo list source = config/agent-models.json repo_paths keys, used
    verbatim as full GH names (owner Larry-Yatch). Fail-safe to the known set
    on any read/parse error (§2.1)."""
    default = ['ourliberty-agent-core', 'ourliberty-dashboard',
               'ourliberty-graph']
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default
    keys = list((data.get('repo_paths') or {}).keys())
    return keys or default


_REVERT_KEYWORDS = ('revert', 'rollback', 'roll back')
_FIX_KEYWORDS = ('fix', 'hotfix', 'patch', 'bugfix')


def _title_is_revert(title: str) -> bool:
    t = (title or '').lower()
    return any(k in t for k in _REVERT_KEYWORDS)


def _title_is_fix(title: str) -> bool:
    t = (title or '').lower()
    return any(k in t for k in _FIX_KEYWORDS)


def fetch_merges_via_gh(repos: list[str], since: datetime) -> list[Merge]:
    """gh merged-PR search per repo (authoritative — chain_events auto_merge
    rows miss desktop merges). Authed in the timer env via HOME=/home/larry +
    ~/.config/gh/hosts.yml; no token in .env.larry. task_id is joined later
    from chain_events; here we capture the github facts.

    Raises on any transport/exit/parse failure so the caller marks
    sources.github='error' (a dark source, NOT a page)."""
    out: list[Merge] = []
    since_str = since.date().isoformat()
    for repo in repos:
        coords = f'{GH_OWNER}/{repo}'
        proc = subprocess.run(
            ['gh', 'pr', 'list', '--repo', coords, '--state', 'merged',
             '--search', f'merged:>={since_str}', '--limit', '500',
             '--json', 'number,url,mergedAt,files,additions,deletions,'
             'title,labels,headRefName'],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f'gh pr list ({coords}) rc={proc.returncode}: '
                f'{(proc.stderr or "").strip()[:200]}')
        payload = json.loads(proc.stdout or '[]')
        for item in payload:
            merged_at = _parse_ts(item.get('mergedAt'))
            if merged_at is None:
                continue
            files = item.get('files') or []
            title = item.get('title') or ''
            labels = {(lbl.get('name') or '').lower()
                      for lbl in (item.get('labels') or [])}
            branch = item.get('headRefName') or ''
            # handsfree = merged via the forge/auto-merge pipeline
            # (distinguishable from labels/branch prefix on the same PRs).
            handsfree = (branch.startswith('forge/')
                         or 'auto-merge' in labels)
            out.append(Merge(
                pr_url=item.get('url') or '',
                number=item.get('number'),
                repo=repo,
                merged_at=merged_at,
                files_changed=len(files),
                additions=int(item.get('additions') or 0),
                deletions=int(item.get('deletions') or 0),
                handsfree=handsfree,
                is_revert=_title_is_revert(title),
                is_fix_shaped=_title_is_fix(title),
                changed_files=[f.get('path') for f in files if f.get('path')],
            ))
    return out


# chain_event types we read: the join rows (auto_merge / review_request carry
# task_id + pr_url), forge session_starts (dispatch clock + redispatch), and
# review_revision (Mirror rounds).
_CHAIN_EVENT_TYPES = ('auto_merge', 'review_request', 'session_start',
                      'review_revision')


def fetch_chain_events_via_supabase(client, since: datetime) -> list[ChainRow]:
    """Paginated chain_events read (lesson #795 — unpaginated selects truncate
    at 1000 rows). Copies the pagination pattern from pulse_check_x.py."""
    since_iso = since.isoformat()
    out: list[ChainRow] = []
    page = 0
    page_size = 1000
    while True:
        res = (
            client.table('chain_events')
                  .select('event_type,ts,task_id,agent,pr_url,payload')
                  .in_('event_type', list(_CHAIN_EVENT_TYPES))
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
            payload = row.get('payload') if isinstance(row.get('payload'),
                                                       dict) else {}
            # pr_url is a top-level column (§2.2); the shipper also mirrors it
            # into payload — fall back to that.
            pr_url = row.get('pr_url') or (payload or {}).get('pr_url')
            rev = (payload or {}).get('revision_count')
            out.append(ChainRow(
                event_type=row.get('event_type') or '',
                ts=ts,
                task_id=row.get('task_id') or '',
                agent=row.get('agent') or '',
                pr_url=pr_url,
                revision_count=rev if isinstance(rev, int) else None,
            ))
        if len(rows) < page_size:
            break
        page += 1
    return out


def join_task_ids(merges: list[Merge], chain: list[ChainRow]) -> None:
    """Resolve each merged PR's task_id from its chain_events auto_merge /
    review_request row via the top-level pr_url column (§2.2). Mutates merges
    in place; a PR with no join row stays task_id=None (counts as unlinked)."""
    by_pr: dict[str, str] = {}
    for r in chain:
        if r.event_type in ('auto_merge', 'review_request') and r.pr_url \
                and r.task_id:
            by_pr.setdefault(r.pr_url, r.task_id)
    for m in merges:
        if m.task_id is None and m.pr_url in by_pr:
            m.task_id = by_pr[m.pr_url]


def load_costs(path: Path, since: datetime) -> list[CostRow]:
    """costs.jsonl (§2.5): ts + cost_usd + agent/task_id/model. Raises on a
    read error so the caller marks sources.costs='error'."""
    out: list[CostRow] = []
    with open(path, errors='replace') as fh:
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
            try:
                cost = float(rec.get('cost_usd') or 0.0)
            except (TypeError, ValueError):
                cost = 0.0
            out.append(CostRow(
                ts=ts,
                cost_usd=cost,
                task_id=rec.get('task_id') or '',
                agent=rec.get('agent') or '',
                model=rec.get('model') or '',
            ))
    return out


def load_missions(path: Path) -> list[Mission]:
    """missions.json canonical committer copy (§2.2). Raises on read/parse
    error so the caller marks sources.missions='error'."""
    data = json.loads(path.read_text())
    raw = data['missions'] if isinstance(data, dict) else data
    out: list[Mission] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        out.append(Mission(
            id=m.get('id') or '',
            phase=m.get('phase') or '',
            task_ids=[t for t in (m.get('task_ids') or []) if t],
            created=_parse_date(m.get('created')),
            shipped_at=_parse_ts(m.get('shipped_at')),
        ))
    return out


def _connect_supabase():
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing.')
    from supabase_factory import get_supabase_client  # type: ignore
    return get_supabase_client(url, key)


def gather_substrate(now: datetime) -> Substrate:
    """Read all four sources INDEPENDENTLY, each try/except'd. Returns a
    Substrate whose `sources` block records per-source health; a dark source
    contributes empty records and an 'error' status (§5 partial-data)."""
    # Earliest ts either window needs = prior-window start, plus SURVIVAL_DAYS
    # of lookback so the hot-file denylist / survival proxy see enough history.
    since = (now - timedelta(days=WINDOW_DAYS + BASELINE_DAYS + SURVIVAL_DAYS))
    sources = {s: 'ok' for s in SOURCES}
    repos = load_repo_names()

    try:
        merges = fetch_merges_via_gh(repos, since)
    except Exception as e:  # noqa: BLE001 — a dark source is a 0-exit
        log(f'github source dark: {type(e).__name__}: {e}', 'WARN')
        merges = []
        sources['github'] = 'error'

    try:
        client = _connect_supabase()
        chain = fetch_chain_events_via_supabase(client, since)
    except Exception as e:  # noqa: BLE001
        log(f'chain_events source dark: {type(e).__name__}: {e}', 'WARN')
        chain = []
        sources['chain_events'] = 'error'

    if sources['github'] == 'ok' and sources['chain_events'] == 'ok':
        join_task_ids(merges, chain)

    try:
        costs = load_costs(COSTS_FILE, since)
    except Exception as e:  # noqa: BLE001
        log(f'costs source dark: {type(e).__name__}: {e}', 'WARN')
        costs = []
        sources['costs'] = 'error'

    try:
        missions = load_missions(MISSIONS_FILE)
    except Exception as e:  # noqa: BLE001
        log(f'missions source dark: {type(e).__name__}: {e}', 'WARN')
        missions = []
        sources['missions'] = 'error'

    return Substrate(merges=merges, chain=chain, costs=costs,
                     missions=missions, sources=sources)


# ============================ DM routing ============================


def _dm(source: str, severity: str, message: str, subject: str,
        route: str = 'escalate') -> bool:
    """Pinned append_alert routing (§6). Never raises."""
    try:
        import larry_alerts as la  # local import so tests can stub
        return la.append_alert(
            source=source, severity=severity, message=message,
            subject=subject, route=route,
        )
    except Exception as e:  # noqa: BLE001
        log(f'_dm failed: {type(e).__name__}: {e}', 'WARN')
        return False


def format_monthly_digest(artifact: dict[str, Any]) -> str:
    """Plain-language, CEO-readable monthly nominal digest body."""
    date_str = artifact['as_of'][:10]
    tp = artifact['metrics']['throughput']
    lt = artifact['metrics']['lead_time']
    cost = artifact['metrics']['cost']

    def _cur(block: dict[str, Any]) -> Any:
        return block.get('current')

    lines = [
        f'Check XII — delivery-effectiveness monthly digest ({date_str})',
        '',
        'How the factory delivered over the trailing 4 weeks (observe-only; '
        'no firing rules yet — this is the baseline that V1.1 will calibrate '
        'against):',
        '',
        f'  - Merges: {_cur(tp["merges_total"])} '
        f'(mission-linked {_cur(tp["merges_mission_linked"])}, '
        f'unlinked {_cur(tp["merges_unlinked"])})',
        f'  - Dispatch→merge p50: {_cur(lt["dispatch_to_merge_p50_hours"])} h',
        f'  - Cost per mission shipped: '
        f'${_cur(cost["cost_per_mission_shipped_usd"])}',
        '',
        'Full metric table (throughput split, lead time, rework, survival, '
        'cost, demand) is in the artifact. Dark sources this run: '
        + (', '.join(k for k, v in artifact['sources'].items()
                     if v != 'ok') or 'none') + '.',
    ]
    return '\n'.join(lines)


def maybe_dm(artifact: dict[str, Any], now: datetime,
             prior: Optional[dict[str, Any]]) -> list[str]:
    """Delivery cadence (§6): monthly nominal digest on the first Monday +
    source-dark warnings (only when a source is dark 2 consecutive runs).
    Silent otherwise. Returns the subjects DMed (for logging/tests)."""
    dmed: list[str] = []

    # Source-dark escalation: only when dark THIS run AND dark in the prior
    # artifact (2 consecutive weekly runs) — a single 05:00 gh/Supabase blip
    # is self-resolving and must not page (§5).
    prior_sources = (prior or {}).get('sources', {}) if prior else {}
    for name, status in artifact['sources'].items():
        if status == 'error' and prior_sources.get(name) == 'error':
            subject = f'pulse-check-xii-source-dark:{name}'
            if _dm('pulse-check-xii', 'warning',
                   f'Check XII source `{name}` has been dark for 2 consecutive '
                   f'weekly runs — the delivery metrics that depend on it are '
                   f'reporting insufficient_signal. Investigate before the next '
                   f'firing.', subject):
                dmed.append(subject)

    # Monthly nominal digest — first Monday only.
    if is_first_monday(now.date()):
        subject = 'pulse-check-xii-monthly-digest'
        if _dm('pulse-check-xii', 'info', format_monthly_digest(artifact),
               subject, route='escalate'):
            dmed.append(subject)

    return dmed


# ============================ fixture (test) path ============================


def _substrate_from_fixture(raw: dict[str, Any], now: datetime) -> Substrate:
    """Build a Substrate from a JSON fixture (deterministic tests / manual
    runs). Source health defaults to 'ok'; a fixture may force a source dark
    via `"sources": {"github": "error", ...}`."""
    sources = {s: 'ok' for s in SOURCES}
    for k, v in (raw.get('sources') or {}).items():
        if k in sources:
            sources[k] = v

    merges = [
        Merge(
            pr_url=m.get('pr_url', ''),
            number=m.get('number'),
            repo=m.get('repo', ''),
            merged_at=_parse_ts(m.get('merged_at')) or now,
            files_changed=int(m.get('files_changed', 0)),
            additions=int(m.get('additions', 0)),
            deletions=int(m.get('deletions', 0)),
            handsfree=bool(m.get('handsfree', False)),
            task_id=m.get('task_id'),
            is_revert=bool(m.get('is_revert', False)),
            is_fix_shaped=bool(m.get('is_fix_shaped', False)),
            changed_files=list(m.get('changed_files', [])),
        )
        for m in raw.get('merges', [])
    ] if sources['github'] == 'ok' else []

    chain = [
        ChainRow(
            event_type=c.get('event_type', ''),
            ts=_parse_ts(c.get('ts')) or now,
            task_id=c.get('task_id', ''),
            agent=c.get('agent', ''),
            pr_url=c.get('pr_url'),
            revision_count=c.get('revision_count'),
        )
        for c in raw.get('chain_events', [])
    ] if sources['chain_events'] == 'ok' else []

    costs = [
        CostRow(
            ts=_parse_ts(c.get('ts')) or now,
            cost_usd=float(c.get('cost_usd', 0.0)),
            task_id=c.get('task_id', ''),
            agent=c.get('agent', ''),
            model=c.get('model', ''),
        )
        for c in raw.get('costs', [])
    ] if sources['costs'] == 'ok' else []

    missions = [
        Mission(
            id=m.get('id', ''),
            phase=m.get('phase', ''),
            task_ids=[t for t in (m.get('task_ids') or []) if t],
            created=_parse_date(m.get('created')),
            shipped_at=_parse_ts(m.get('shipped_at')),
        )
        for m in raw.get('missions', [])
    ] if sources['missions'] == 'ok' else []

    if sources['github'] == 'ok' and sources['chain_events'] == 'ok':
        join_task_ids(merges, chain)

    return Substrate(merges=merges, chain=chain, costs=costs,
                     missions=missions, sources=sources)


# ============================ main ============================


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-json',
                        help='Read substrate from a JSON fixture instead of '
                             'querying gh / chain_events / costs / missions. '
                             'May carry "now" and a "sources" override for '
                             'deterministic tests.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute + print the artifact; do not write or DM.')
    parser.add_argument('--force', action='store_true',
                        help='Bypass the same-day idempotency skip.')
    args = parser.parse_args(argv)

    if args.from_json:
        with open(args.from_json) as fh:
            raw = json.load(fh)
        now = _parse_ts(raw.get('now')) or datetime.now(timezone.utc)
        sub = _substrate_from_fixture(raw, now)
    else:
        now = datetime.now(timezone.utc)

    target = artifact_path_for_date(now)
    # Same-day sentinel: a second run the same day is a clean no-op skip (still
    # a 0-exit so the heartbeat fires).
    if (target.exists() and not args.force and not args.dry_run
            and not args.from_json):
        log(f'Check XII already ran today ({now.date().isoformat()}); '
            'skipping (use --force to re-run).')
        return 0

    if not args.from_json:
        sub = gather_substrate(now)

    artifact = build_artifact(sub, now)

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    # The ONE non-zero exit: could not write the artifact at all. Every dark
    # source above is a 0-exit with sources.<name>='error' (§5 heartbeat/rc
    # invariant) — the natural "return non-zero on a failed source" instinct
    # would page Larry and is deliberately avoided.
    try:
        prior = _read_prior_artifact(now)
        path = write_artifact(artifact, now)
    except OSError as e:
        log(f'FATAL: could not write artifact: {type(e).__name__}: {e}',
            'ERROR')
        return 1

    prune_old_artifacts()
    dmed = maybe_dm(artifact, now, prior)
    dark = [k for k, v in artifact['sources'].items() if v != 'ok']
    log(f'Check XII complete: artifact={path.name} '
        f'dark_sources={dark or "none"} dmed={dmed or "none"}')
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check as _hb_run_check
    sys.exit(_hb_run_check('xii', main, log_fn=log))
