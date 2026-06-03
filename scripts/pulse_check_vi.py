#!/usr/bin/env python3
"""pulse_check_vi.py — PRIME DIRECTIVE posture analyzer.

Spec: agents/beacon/specs/pulse-cycle-upgrade.md § 12.3 (Check VI) +
§ 4 (verification_pending semantics).

Monthly cadence. Data substrate: ``~/agents/blackboard/cycle-prime-ledger.jsonl``
(the new PRIME ledger; OQ1-resolved name). Analyses three indicators:

  - ``verification_pending_rate`` over trailing 30d: pending /
    (intervention + systemic_fix + verification_pending).
  - ``auto_promote_rate`` over trailing 30d: systemic_fixes from
    promote_verification_pending / total verification_pending rows
    that have been resolved one way or another (promoted or stuck).
  - ``stuck_forever_rate`` over trailing 30d: pending rows older than
    the 7d auto-promote window with no promotion / (pending rows older
    than 7d total).

Three proposal-firing rules (per spec § 12.3 Check VI):

  1. ``tighten_lenient`` — pending_rate > 0.40 AND auto_promote_rate
     > 0.80 → posture too lenient. Propose tightening (move toward
     Strict) OR shrinking the verification window.
  2. ``tighten_masking`` — pending_rate < 0.05 AND ratio NOT trending
     toward zero (trend not "improving") → Neutral is masking failures;
     propose tightening.
  3. ``stricter_unverifiable`` — stuck_forever_rate > 0.30 → discipline
     failing; propose stricter posture + re-examine fix-categories.

Multiple rules can fire in one cycle; each becomes its own entry in
``proposals`` so Larry sees them separately. Artifact:
``~/agents/blackboard/pulse-check-vi-proposals/check-vi-<month>.json``.

Stdlib only. No LLM.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

AGENTS_ROOT = Path(
    os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents'))
)
LEDGER_FILE = AGENTS_ROOT / 'blackboard' / 'cycle-prime-ledger.jsonl'
PROPOSALS_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-vi-proposals'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-vi.log'

LOOKBACK_DAYS = 30
PROMOTE_WINDOW_DAYS = 7
TREND_HALF_DAYS = 15
TREND_RATIO_DELTA = 0.10

PENDING_HIGH_RATE = 0.40
AUTO_PROMOTE_HIGH_RATE = 0.80
PENDING_LOW_RATE = 0.05
STUCK_FOREVER_FLOOR = 0.30


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


def month_anchor(d: date) -> str:
    return f'{d.year:04d}-{d.month:02d}'


def artifact_path_for_month(anchor: str) -> Path:
    return PROPOSALS_DIR / f'check-vi-{anchor}.json'


@dataclass
class PostureMetrics:
    pending_count: int
    intervention_count: int
    systemic_fix_count: int
    total_count: int
    auto_promote_count: int
    stuck_forever_count: int
    pending_rate: float
    auto_promote_rate: float
    stuck_forever_rate: float
    ratio_trend: str        # 'improving'|'flat'|'worsening' (per ledger.compute_ratio_30d)


@dataclass
class Proposal:
    rule: str
    rationale: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckVIResult:
    metrics: PostureMetrics
    proposals: list[Proposal]
    as_of_iso: str
    month_anchor: str


def _read_ledger(path: Path = LEDGER_FILE) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError as e:
        log(f'read {path} failed: {e}', 'WARN')
        return []
    return out


def _safe_rate(num: int, den: int) -> float:
    return (num / den) if den > 0 else 0.0


def _compute_trend(rows: list[dict[str, Any]], *, now: datetime,
                   lookback_days: int = LOOKBACK_DAYS,
                   half_days: int = TREND_HALF_DAYS) -> str:
    cutoff = now - timedelta(days=lookback_days)
    half = now - timedelta(days=half_days)

    def _half_ratio(start: datetime, end: datetime) -> Optional[float]:
        i = s = 0
        for r in rows:
            ts = _parse_ts(r.get('ts'))
            if ts is None:
                continue
            if not (start <= ts < end):
                continue
            kind = r.get('kind')
            if kind == 'intervention':
                i += 1
            elif kind == 'systemic_fix':
                s += 1
        return (i / s) if s > 0 else None

    rr = _half_ratio(half, now)
    or_ = _half_ratio(cutoff, half)
    if rr is None or or_ is None or or_ == 0:
        return 'flat'
    delta = (rr - or_) / or_
    if delta < -TREND_RATIO_DELTA:
        return 'improving'
    if delta > TREND_RATIO_DELTA:
        return 'worsening'
    return 'flat'


def compute_metrics(
    rows: list[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    lookback_days: int = LOOKBACK_DAYS,
    promote_window_days: int = PROMOTE_WINDOW_DAYS,
) -> PostureMetrics:
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    stuck_cutoff = now - timedelta(days=promote_window_days)

    pending_rows: list[dict[str, Any]] = []
    intervention = systemic = 0
    pending_count = 0
    promoted_ids: set[str] = set()
    pending_ids: dict[str, dict[str, Any]] = {}

    for r in rows:
        ts = _parse_ts(r.get('ts'))
        if ts is None or ts < cutoff:
            continue
        kind = r.get('kind')
        iid = r.get('intervention_id') or ''
        if kind == 'intervention':
            intervention += 1
        elif kind == 'systemic_fix':
            systemic += 1
            if r.get('verified_at') and iid in pending_ids:
                promoted_ids.add(iid)
        elif kind == 'verification_pending':
            pending_count += 1
            pending_rows.append(r)
            if iid:
                pending_ids[iid] = r

    total = intervention + systemic + pending_count
    pending_rate = _safe_rate(pending_count, total)

    # Auto-promote rate = promoted / resolved-pending. A pending row is
    # "resolved" if it was promoted OR aged past the window without
    # promotion (stuck).
    stuck_rows = [
        r for r in pending_rows
        if (_parse_ts(r.get('ts')) or now) < stuck_cutoff
        and (r.get('intervention_id') or '') not in promoted_ids
    ]
    resolved_pending = len(promoted_ids) + len(stuck_rows)
    auto_promote_rate = _safe_rate(len(promoted_ids), resolved_pending)
    aged_pending = [
        r for r in pending_rows
        if (_parse_ts(r.get('ts')) or now) < stuck_cutoff
    ]
    stuck_forever_rate = _safe_rate(len(stuck_rows), len(aged_pending))

    trend = _compute_trend(rows, now=now, lookback_days=lookback_days)

    return PostureMetrics(
        pending_count=pending_count,
        intervention_count=intervention,
        systemic_fix_count=systemic,
        total_count=total,
        auto_promote_count=len(promoted_ids),
        stuck_forever_count=len(stuck_rows),
        pending_rate=pending_rate,
        auto_promote_rate=auto_promote_rate,
        stuck_forever_rate=stuck_forever_rate,
        ratio_trend=trend,
    )


def run_check(
    *,
    rows: Optional[list[dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> CheckVIResult:
    if now is None:
        now = datetime.now(timezone.utc)
    rows = rows or []

    metrics = compute_metrics(rows, now=now)
    proposals: list[Proposal] = []

    # Rule 1: tighten_lenient.
    if (metrics.pending_rate > PENDING_HIGH_RATE
            and metrics.auto_promote_rate > AUTO_PROMOTE_HIGH_RATE):
        proposals.append(Proposal(
            rule='tighten_lenient',
            rationale=(
                f'verification_pending_rate={metrics.pending_rate:.2f} '
                f'(>{PENDING_HIGH_RATE}) AND '
                f'auto_promote_rate={metrics.auto_promote_rate:.2f} '
                f'(>{AUTO_PROMOTE_HIGH_RATE}) — posture is too lenient. '
                'Propose tightening (move toward Strict) OR shrink the '
                'verification window.'
            ),
            detail={
                'pending_rate': round(metrics.pending_rate, 4),
                'auto_promote_rate': round(metrics.auto_promote_rate, 4),
            },
        ))

    # Rule 2: tighten_masking.
    if (metrics.pending_rate < PENDING_LOW_RATE
            and metrics.ratio_trend != 'improving'):
        proposals.append(Proposal(
            rule='tighten_masking',
            rationale=(
                f'verification_pending_rate={metrics.pending_rate:.2f} '
                f'(<{PENDING_LOW_RATE}) AND ratio trend='
                f'{metrics.ratio_trend} (not improving). Neutral '
                'posture is masking failures; propose tightening.'
            ),
            detail={
                'pending_rate': round(metrics.pending_rate, 4),
                'ratio_trend': metrics.ratio_trend,
            },
        ))

    # Rule 3: stricter_unverifiable.
    if metrics.stuck_forever_rate > STUCK_FOREVER_FLOOR:
        proposals.append(Proposal(
            rule='stricter_unverifiable',
            rationale=(
                f'stuck_forever_rate={metrics.stuck_forever_rate:.2f} '
                f'(>{STUCK_FOREVER_FLOOR}) — discipline failing. '
                'Propose stricter posture + re-examine fix-categories '
                'that are systemically unverifiable.'
            ),
            detail={
                'stuck_forever_rate': round(metrics.stuck_forever_rate, 4),
                'stuck_count': metrics.stuck_forever_count,
            },
        ))

    return CheckVIResult(
        metrics=metrics,
        proposals=proposals,
        as_of_iso=now.isoformat(),
        month_anchor=month_anchor(now.date()),
    )


def build_artifact(result: CheckVIResult) -> dict[str, Any]:
    m = result.metrics
    return {
        'as_of': result.as_of_iso,
        'month_anchor': result.month_anchor,
        'check': 'VI',
        'lookback_days': LOOKBACK_DAYS,
        'metrics': {
            'pending_count': m.pending_count,
            'intervention_count': m.intervention_count,
            'systemic_fix_count': m.systemic_fix_count,
            'total_count': m.total_count,
            'auto_promote_count': m.auto_promote_count,
            'stuck_forever_count': m.stuck_forever_count,
            'pending_rate': round(m.pending_rate, 4),
            'auto_promote_rate': round(m.auto_promote_rate, 4),
            'stuck_forever_rate': round(m.stuck_forever_rate, 4),
            'ratio_trend': m.ratio_trend,
        },
        'proposals': [
            {'rule': p.rule, 'rationale': p.rationale, 'detail': p.detail}
            for p in result.proposals
        ],
        'applied': False,
    }


def write_artifact(artifact: dict[str, Any]) -> Path:
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = artifact_path_for_month(artifact['month_anchor'])
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(artifact, indent=2))
    tmp.replace(path)
    return path


def format_digest(artifact: dict[str, Any]) -> str:
    proposals = artifact.get('proposals') or []
    date_str = artifact['as_of'][:10]
    metrics = artifact['metrics']
    if not proposals:
        return (
            f'Check VI ({date_str}) — posture metrics within bounds. '
            f'pending={metrics["pending_rate"]:.2f}, '
            f'auto_promote={metrics["auto_promote_rate"]:.2f}, '
            f'stuck_forever={metrics["stuck_forever_rate"]:.2f}, '
            f'trend={metrics["ratio_trend"]}.'
        )
    lines = [
        f'Check VI ({date_str}) — '
        f'{len(proposals)} PRIME DIRECTIVE posture proposal(s):',
        '',
        f'pending_rate={metrics["pending_rate"]:.2f}, '
        f'auto_promote_rate={metrics["auto_promote_rate"]:.2f}, '
        f'stuck_forever_rate={metrics["stuck_forever_rate"]:.2f}, '
        f'trend={metrics["ratio_trend"]}',
        '',
    ]
    for p in proposals:
        lines.append(f'- [{p["rule"]}] {p["rationale"]}')
    lines.append('')
    lines.append(
        f'Approve: reply `approve check-vi-update-{date_str}` on '
        f'Telegram, or `reject check-vi-update-{date_str} <reason>`.'
    )
    return '\n'.join(lines)


def dm_digest(artifact: dict[str, Any]) -> bool:
    if not artifact.get('proposals'):
        return False
    body = format_digest(artifact)
    date_str = artifact['as_of'][:10]
    try:
        import larry_alerts as la
        return la.append_alert(
            source='pulse-check-vi',
            severity='warning',
            message=body,
            subject=f'check-vi-update:{date_str}',
            suggested_action=(
                f'Review proposals; reply `approve check-vi-update-{date_str}` or '
                f'`reject check-vi-update-{date_str} <reason>`.'
            ),
        )
    except Exception as e:
        log(f'dm_digest failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- main --------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',
                        help='Read ledger rows from a JSON fixture file.')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    anchor = month_anchor(now.date())
    target_path = artifact_path_for_month(anchor)

    if target_path.exists() and not args.force and not args.dry_run:
        log(f'Check VI already ran this month ({anchor}); '
            'skipping (use --force to re-run).')
        return 0

    if args.fixture:
        with open(args.fixture) as fh:
            raw = json.load(fh)
        rows = raw.get('rows', [])
    else:
        rows = _read_ledger()

    result = run_check(rows=rows, now=now)
    artifact = build_artifact(result)

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    write_artifact(artifact)
    dm_digest(artifact)
    log(f'Check VI complete: proposals={len(result.proposals)} '
        f'pending_rate={result.metrics.pending_rate:.2f} '
        f'trend={result.metrics.ratio_trend}')
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check
    sys.exit(run_check('vi', main, log_fn=log))
