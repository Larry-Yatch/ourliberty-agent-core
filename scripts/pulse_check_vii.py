#!/usr/bin/env python3
"""pulse_check_vii.py — cost-ceiling escalation-threshold analyzer.

Spec: agents/beacon/specs/pulse-cycle-upgrade.md § 12.3 (Check VII) +
§ 4 Decision III (escalation-response log).

Event-driven (not periodic). Cadence: invoked after every new
escalation-response row arrives in
``~/agents/state/pulse-cost-escalations.jsonl``. β creates this file
shape (α₂ documents the producer side; this analyzer is the consumer).

Each escalation-response row schema::

    {
      "ts": "<iso8601>",
      "band": "low" | "high",          # $50-band or $100-band by default
      "threshold_usd": <float>,
      "spend_usd_at_dm": <float>,
      "larry_decision": "keep_going" | "throttle" | "silent",
      "decided_at": "<iso8601>"|null
    }

Three proposal-firing rules per spec § 12.3 Check VII (priority order:
remove > raise/lower):

  1. ``remove`` — Larry's pattern is fully consistent over the last
     ``REMOVE_MIN_CONSECUTIVE`` (20) escalations for a band (all the
     same decision) → propose removing the escalation entirely for that
     band; Pulse decides without DMing Larry.
  2. ``raise`` — Larry consistently approves ``keep_going`` at the
     low-band escalation over the last ``RAISE_MIN_CONSECUTIVE`` (10)
     events → propose raising first escalation to a higher threshold
     (default: midpoint between low and high bands).
  3. ``lower`` — Larry consistently says ``throttle`` at the low-band
     escalation over the last ``RAISE_MIN_CONSECUTIVE`` (10) events →
     propose lowering threshold OR auto-throttling at that level.

Artifact: ``~/agents/blackboard/pulse-check-vii-proposals/check-vii-<date>.json``.
Idempotency anchor is the ``ts`` of the most-recent escalation processed:
each run reads the entire log but only fires (writes artifact + DMs) on
the FIRST run where a given rule fires for a given band — subsequent
runs with the same window of last-N rows produce the same conclusion
and are skipped via the sentinel artifact path.

Stdlib only. No LLM.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path(
    os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents'))
)
ESCALATIONS_FILE = AGENTS_ROOT / 'state' / 'pulse-cost-escalations.jsonl'
PROPOSALS_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-vii-proposals'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-vii.log'

REMOVE_MIN_CONSECUTIVE = 20
RAISE_MIN_CONSECUTIVE = 10

DECISION_KEEP = 'keep_going'
DECISION_THROTTLE = 'throttle'
DECISION_SILENT = 'silent'

LOW_BAND = 'low'
HIGH_BAND = 'high'

# Default raise/lower deltas. The "raise" rule needs a target; we move
# 50% of the gap between the low-band threshold and the high-band
# threshold (i.e. halfway up). Lower rule drops 20%.
RAISE_GAP_FRACTION = 0.5
LOWER_FALLBACK_PCT = 0.20


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


@dataclass
class EscalationRow:
    ts: datetime
    band: str
    threshold_usd: float
    spend_usd_at_dm: float
    decision: str


@dataclass
class Proposal:
    rule: str                # 'remove' | 'raise' | 'lower'
    band: str
    current_threshold_usd: float
    proposed_threshold_usd: Optional[float]
    rationale: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckVIIResult:
    proposals: list[Proposal]
    as_of_iso: str
    anchor_date: str               # YYYY-MM-DD of the most recent row
    rows_total: int


def _read_log(path: Path = ESCALATIONS_FILE) -> list[EscalationRow]:
    if not path.exists():
        return []
    out: list[EscalationRow] = []
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
                if not isinstance(rec, dict):
                    continue
                ts = _parse_ts(rec.get('ts'))
                if ts is None:
                    continue
                band = rec.get('band')
                if band not in (LOW_BAND, HIGH_BAND):
                    continue
                try:
                    threshold = float(rec.get('threshold_usd') or 0.0)
                    spend = float(rec.get('spend_usd_at_dm') or 0.0)
                except (TypeError, ValueError):
                    continue
                decision = rec.get('larry_decision')
                if decision not in (DECISION_KEEP, DECISION_THROTTLE,
                                    DECISION_SILENT):
                    decision = DECISION_SILENT
                out.append(EscalationRow(
                    ts=ts, band=band, threshold_usd=threshold,
                    spend_usd_at_dm=spend, decision=decision,
                ))
    except OSError as e:
        log(f'read {path} failed: {e}', 'WARN')
        return []
    return out


def _by_band(rows: list[EscalationRow]) -> dict[str, list[EscalationRow]]:
    out: dict[str, list[EscalationRow]] = defaultdict(list)
    for r in rows:
        out[r.band].append(r)
    for band in out:
        out[band].sort(key=lambda r: r.ts)
    return dict(out)


def _last_decisions(rows: list[EscalationRow], n: int) -> list[str]:
    return [r.decision for r in rows[-n:]]


def _all_same(seq: list[str], target: str, min_count: int) -> bool:
    return len(seq) >= min_count and all(d == target for d in seq[-min_count:])


def _band_thresholds(rows: list[EscalationRow]) -> dict[str, float]:
    """Return the most-recent threshold observed for each band."""
    out: dict[str, float] = {}
    for r in rows:
        out[r.band] = r.threshold_usd
    return out


def run_check(
    *,
    rows: Optional[list[EscalationRow]] = None,
    now: Optional[datetime] = None,
) -> CheckVIIResult:
    if now is None:
        now = datetime.now(timezone.utc)
    rows = rows or []
    by_band = _by_band(rows)
    band_thresholds = _band_thresholds(rows)
    low_threshold = band_thresholds.get(LOW_BAND, 50.0)
    high_threshold = band_thresholds.get(HIGH_BAND, 100.0)

    proposals: list[Proposal] = []

    for band, band_rows in by_band.items():
        decisions = _last_decisions(band_rows, REMOVE_MIN_CONSECUTIVE)
        current_threshold = band_thresholds.get(band, 0.0)

        # Rule 1 (highest): remove — fully-consistent decision over the
        # last REMOVE_MIN_CONSECUTIVE events of that band.
        for target in (DECISION_KEEP, DECISION_THROTTLE):
            if _all_same(decisions, target, REMOVE_MIN_CONSECUTIVE):
                proposals.append(Proposal(
                    rule='remove',
                    band=band,
                    current_threshold_usd=current_threshold,
                    proposed_threshold_usd=None,
                    rationale=(
                        f'Larry decided `{target}` on the last '
                        f'{REMOVE_MIN_CONSECUTIVE} {band}-band escalations '
                        '— pattern is fully consistent. Propose removing '
                        f'the {band}-band escalation entirely; Pulse will '
                        f'apply the fully-predicted decision (`{target}`) '
                        'without DMing.'
                    ),
                    detail={
                        'consecutive_decisions': target,
                        'count': REMOVE_MIN_CONSECUTIVE,
                    },
                ))
                break
        else:
            # Rule 2/3 only apply to the low band per spec.
            if band == LOW_BAND:
                raise_window = _last_decisions(band_rows, RAISE_MIN_CONSECUTIVE)
                if _all_same(raise_window, DECISION_KEEP, RAISE_MIN_CONSECUTIVE):
                    if high_threshold > low_threshold:
                        proposed = low_threshold + RAISE_GAP_FRACTION * (
                            high_threshold - low_threshold)
                    else:
                        proposed = low_threshold * (1.0 + RAISE_GAP_FRACTION)
                    proposals.append(Proposal(
                        rule='raise',
                        band=band,
                        current_threshold_usd=current_threshold,
                        proposed_threshold_usd=round(proposed, 2),
                        rationale=(
                            f'Larry approved `keep_going` on the last '
                            f'{RAISE_MIN_CONSECUTIVE} {band}-band '
                            f'escalations — raise threshold '
                            f'${current_threshold:.2f} → ${proposed:.2f}.'
                        ),
                        detail={'consecutive_keep_going': RAISE_MIN_CONSECUTIVE},
                    ))
                elif _all_same(raise_window, DECISION_THROTTLE,
                               RAISE_MIN_CONSECUTIVE):
                    proposed = current_threshold * (1.0 - LOWER_FALLBACK_PCT)
                    proposals.append(Proposal(
                        rule='lower',
                        band=band,
                        current_threshold_usd=current_threshold,
                        proposed_threshold_usd=round(proposed, 2),
                        rationale=(
                            f'Larry chose `throttle` on the last '
                            f'{RAISE_MIN_CONSECUTIVE} {band}-band '
                            f'escalations — lower threshold '
                            f'${current_threshold:.2f} → ${proposed:.2f} '
                            'OR auto-throttle at that level.'
                        ),
                        detail={'consecutive_throttle': RAISE_MIN_CONSECUTIVE},
                    ))

    anchor_dt = rows[-1].ts if rows else now
    return CheckVIIResult(
        proposals=proposals,
        as_of_iso=now.isoformat(),
        anchor_date=anchor_dt.date().isoformat(),
        rows_total=len(rows),
    )


def artifact_path_for_date(date_str: str) -> Path:
    return PROPOSALS_DIR / f'check-vii-{date_str}.json'


def _persisted_proposal_keys(path: Path) -> set[tuple[Any, Any]]:
    """The (rule, band) keys already recorded in the artifact at ``path``.

    Used by the content-aware same-day idempotency gate (audit #30). A missing
    or corrupt artifact yields an empty set, so any fresh proposal counts as
    new and is (re-)written rather than lost.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    keys: set[tuple[Any, Any]] = set()
    for p in data.get('proposals', []):
        if isinstance(p, dict):
            keys.add((p.get('rule'), p.get('band')))
    return keys


def build_artifact(result: CheckVIIResult) -> dict[str, Any]:
    return {
        'as_of': result.as_of_iso,
        'anchor_date': result.anchor_date,
        'check': 'VII',
        'rows_total': result.rows_total,
        'proposals': [
            {
                'rule': p.rule,
                'band': p.band,
                'current_threshold_usd': p.current_threshold_usd,
                'proposed_threshold_usd': p.proposed_threshold_usd,
                'rationale': p.rationale,
                'detail': p.detail,
            }
            for p in result.proposals
        ],
        'applied': False,
    }


def write_artifact(artifact: dict[str, Any]) -> Path:
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = artifact_path_for_date(artifact['anchor_date'])
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(artifact, indent=2))
    tmp.replace(path)
    return path


def format_digest(artifact: dict[str, Any]) -> str:
    proposals = artifact.get('proposals') or []
    date_str = artifact['as_of'][:10]
    if not proposals:
        return (
            f'Check VII ({date_str}) — escalation thresholds calibrated. '
            f'No proposal across {artifact["rows_total"]} log row(s).'
        )
    lines = [
        f'Check VII ({date_str}) — '
        f'{len(proposals)} cost-ceiling proposal(s):',
        '',
    ]
    for p in proposals:
        current = p['current_threshold_usd']
        proposed = p['proposed_threshold_usd']
        arrow = (f' (${current:.2f} → ${proposed:.2f})'
                 if proposed is not None else '')
        lines.append(f'- [{p["rule"]}] {p["band"]}-band{arrow}: {p["rationale"]}')
    lines.append('')
    lines.append(
        f'Approve: reply `approve check-vii-update-{date_str}` on '
        f'Telegram, or `reject check-vii-update-{date_str} <reason>`.'
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
            source='pulse-check-vii',
            severity='warning',
            message=body,
            subject=f'check-vii-update:{date_str}',
            suggested_action=(
                f'Review proposals; reply `approve check-vii-update-{date_str}` or '
                f'`reject check-vii-update-{date_str} <reason>`.'
            ),
        )
    except Exception as e:
        log(f'dm_digest failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- main --------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',
                        help='Read escalation rows from a JSON fixture '
                             'file (test path).')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true',
                        help='Bypass per-anchor-date idempotency.')
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)

    if args.fixture:
        with open(args.fixture) as fh:
            raw = json.load(fh)
        rows = []
        for r in raw.get('rows', []):
            ts = _parse_ts(r.get('ts')) or now
            try:
                threshold = float(r.get('threshold_usd') or 0.0)
                spend = float(r.get('spend_usd_at_dm') or 0.0)
            except (TypeError, ValueError):
                continue
            rows.append(EscalationRow(
                ts=ts, band=r.get('band', LOW_BAND),
                threshold_usd=threshold, spend_usd_at_dm=spend,
                decision=r.get('larry_decision', DECISION_SILENT),
            ))
    else:
        rows = _read_log()

    result = run_check(rows=rows, now=now)
    artifact = build_artifact(result)

    target_path = artifact_path_for_date(result.anchor_date)
    if (target_path.exists() and not args.force and not args.dry_run
            and result.proposals):
        # Audit #30: the per-date sentinel was content-blind, so a SECOND
        # qualifying signal the same calendar day (e.g. a high-band 'remove'
        # after a low-band 'raise' already wrote the artifact) was silently
        # skipped and never persisted/DM'd. Skip only when every freshly
        # computed proposal is already in the persisted artifact (keyed by
        # rule+band); if new ones appeared, fall through to re-write + DM.
        persisted = _persisted_proposal_keys(target_path)
        fresh = {(p.rule, p.band) for p in result.proposals}
        new_keys = fresh - persisted
        if not new_keys:
            log(f'Check VII: no new proposals vs the {result.anchor_date} '
                f'artifact; skipping (use --force to re-run).')
            return 0
        log(f'Check VII: {len(new_keys)} new proposal(s) since the '
            f'{result.anchor_date} artifact; re-writing + DMing.')

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    if result.proposals:
        write_artifact(artifact)
        dm_digest(artifact)
    log(f'Check VII complete: proposals={len(result.proposals)} '
        f'rows={result.rows_total}')
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check
    sys.exit(run_check('vii', main, log_fn=log))
