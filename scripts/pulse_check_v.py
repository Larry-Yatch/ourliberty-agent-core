#!/usr/bin/env python3
"""pulse_check_v.py — Tier-1 action-template trust analyzer.

Spec: agents/beacon/specs/pulse-cycle-upgrade.md § 12.3 (Check V) +
§ 5.2 / § 5.3 (action-template guard list).

Monthly cadence (runs on the first Monday of the month). Data substrate:
``~/agents/blackboard/cycle-prime-ledger.jsonl`` — the PRIME DIRECTIVE
ledger written by ``cycle_prime_ledger.py``. The auto-fix log
``runbooks/cycle-actions.jsonl`` is NOT the substrate (OQ1 resolution).

Two proposal-firing rules per spec § 12.3 bullet for Check V:

  1. ``graduate`` — for each action-template with >= 10 dispatches in
     trailing 90d AND zero Larry-modifications, propose removing from
     the guard list (template has earned trust).
  2. ``add_to_guard`` — for each non-guarded template that triggered a
     Larry-correction within 30d, propose adding to the guard list.

A row's action-template name is read from ``intervention_id``'s prefix
before the first ``:`` (e.g. ``restart-daemon:beacon`` → template
``restart-daemon``). A row counts as a Larry-modification if
``payload.larry_modified`` is true on the ledger row, OR if a separate
``intervention`` row with the same ``intervention_id`` carries
``payload.kind == "larry_correction"``.

Artifact: ``~/agents/blackboard/pulse-check-v-proposals/check-v-<month>.json``
where ``<month>`` is the ISO YYYY-MM of the run. Stdlib only. No LLM.
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
PROPOSALS_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-v-proposals'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-v.log'
REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_LIST_CONFIG = REPO_ROOT / 'config' / 'action-template-guard-list.json'

GRADUATE_LOOKBACK_DAYS = 90
GRADUATE_MIN_DISPATCHES = 10
ADD_LOOKBACK_DAYS = 30

# Reserved templates that can NEVER graduate, regardless of track record. The
# ledger normalizes any untagged intervention/systemic-fix into the
# ``uncategorized`` bucket (cycle_prime_ledger.UNCATEGORIZED_TEMPLATE); it is a
# "classify me" placeholder, not an auto-fix action, so a graduate proposal for
# it would be meaningless. config/auto-fix-patterns.json also marks it
# permanent_guard=true; this is the enforcement at the Check V layer.
NEVER_GRADUATE_TEMPLATES = frozenset({'uncategorized'})


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
    return PROPOSALS_DIR / f'check-v-{anchor}.json'


@dataclass
class TemplateStats:
    template: str
    dispatch_count_90d: int = 0
    larry_modifications_90d: int = 0
    larry_corrections_30d: int = 0


@dataclass
class Proposal:
    template: str
    kind: str                            # 'graduate' or 'add_to_guard'
    rationale: str
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckVResult:
    proposals: list[Proposal]
    template_count: int
    as_of_iso: str
    month_anchor: str


def _template_of(row: dict[str, Any]) -> str:
    iid = row.get('intervention_id') or ''
    if not isinstance(iid, str) or not iid:
        return ''
    # Convention: "template[:detail]". Empty detail allowed; the prefix
    # before ":" is the template name.
    return iid.split(':', 1)[0]


def _is_larry_modification(row: dict[str, Any]) -> bool:
    """True iff this row was modified by Larry (set on the ledger row by
    Pulse when she observed the operator-side correction)."""
    payload = row.get('payload') if isinstance(row.get('payload'), dict) else row
    if not isinstance(payload, dict):
        return False
    return bool(payload.get('larry_modified'))


def _is_larry_correction(row: dict[str, Any]) -> bool:
    if row.get('kind') != 'intervention':
        return False
    payload = row.get('payload') if isinstance(row.get('payload'), dict) else row
    if not isinstance(payload, dict):
        return False
    return payload.get('kind') == 'larry_correction'


def load_guard_list(path: Path = GUARD_LIST_CONFIG) -> set[str]:
    """Optional — if the guard-list config file exists, return its
    contents; otherwise return an empty set so every template is treated
    as non-guarded. The config file is owned by Larry / approved
    proposals; β does not create it."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log(f'guard-list config unreadable; assuming empty', 'WARN')
        return set()
    if isinstance(data, list):
        return {str(x) for x in data if isinstance(x, str)}
    if isinstance(data, dict):
        templates = data.get('guarded_templates')
        if isinstance(templates, list):
            return {str(x) for x in templates if isinstance(x, str)}
    return set()


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


def compute_template_stats(
    rows: Iterable[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, TemplateStats]:
    """Aggregate rows by template within the relevant lookback windows."""
    cutoff_90 = now - timedelta(days=GRADUATE_LOOKBACK_DAYS)
    cutoff_30 = now - timedelta(days=ADD_LOOKBACK_DAYS)
    by_template: dict[str, TemplateStats] = defaultdict(lambda: TemplateStats(''))
    for row in rows:
        ts = _parse_ts(row.get('ts'))
        if ts is None:
            continue
        template = _template_of(row)
        if not template:
            continue
        stats = by_template[template]
        if not stats.template:
            stats.template = template
        if ts >= cutoff_90 and row.get('kind') in ('intervention', 'systemic_fix'):
            stats.dispatch_count_90d += 1
            if _is_larry_modification(row):
                stats.larry_modifications_90d += 1
        if ts >= cutoff_30 and _is_larry_correction(row):
            stats.larry_corrections_30d += 1
    return dict(by_template)


def run_check(
    *,
    rows: Optional[list[dict[str, Any]]] = None,
    guard_list: Optional[set[str]] = None,
    now: Optional[datetime] = None,
) -> CheckVResult:
    """Pure entry: given ledger rows + guard-list, return the proposals."""
    if now is None:
        now = datetime.now(timezone.utc)
    rows = rows or []
    guard_list = guard_list if guard_list is not None else set()

    by_template = compute_template_stats(rows, now=now)
    proposals: list[Proposal] = []

    for template, stats in sorted(by_template.items()):
        # Rule 1: graduate (template is guarded + trusted).
        if (
            template not in NEVER_GRADUATE_TEMPLATES
            and template in guard_list
            and stats.dispatch_count_90d >= GRADUATE_MIN_DISPATCHES
            and stats.larry_modifications_90d == 0
        ):
            proposals.append(Proposal(
                template=template,
                kind='graduate',
                rationale=(
                    f'{template} dispatched {stats.dispatch_count_90d}x '
                    f'in trailing {GRADUATE_LOOKBACK_DAYS}d with 0 Larry '
                    'modifications — propose removing from guard list.'
                ),
                stats={
                    'dispatch_count_90d': stats.dispatch_count_90d,
                    'larry_modifications_90d': stats.larry_modifications_90d,
                },
            ))
        # Rule 2: add_to_guard (template non-guarded + recent correction).
        if (
            template not in guard_list
            and stats.larry_corrections_30d > 0
        ):
            proposals.append(Proposal(
                template=template,
                kind='add_to_guard',
                rationale=(
                    f'{template} triggered '
                    f'{stats.larry_corrections_30d} Larry-correction(s) '
                    f'in trailing {ADD_LOOKBACK_DAYS}d — propose moving '
                    'INTO the guard list.'
                ),
                stats={
                    'larry_corrections_30d': stats.larry_corrections_30d,
                    'dispatch_count_90d': stats.dispatch_count_90d,
                },
            ))

    return CheckVResult(
        proposals=proposals,
        template_count=len(by_template),
        as_of_iso=now.isoformat(),
        month_anchor=month_anchor(now.date()),
    )


def build_artifact(result: CheckVResult) -> dict[str, Any]:
    return {
        'as_of': result.as_of_iso,
        'month_anchor': result.month_anchor,
        'check': 'V',
        'template_count': result.template_count,
        'graduate_lookback_days': GRADUATE_LOOKBACK_DAYS,
        'graduate_min_dispatches': GRADUATE_MIN_DISPATCHES,
        'add_lookback_days': ADD_LOOKBACK_DAYS,
        'proposals': [
            {
                'template': p.template,
                'kind': p.kind,
                'rationale': p.rationale,
                'stats': p.stats,
            }
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
    if not proposals:
        return (
            f'Check V ({date_str}) — no action-template trust changes '
            f'this month (templates evaluated: {artifact["template_count"]}).'
        )
    lines = [
        f'Check V ({date_str}) — '
        f'{len(proposals)} action-template proposal(s):',
        '',
    ]
    for p in proposals:
        lines.append(f'- {p["template"]} ({p["kind"]}): {p["rationale"]}')
    lines.append('')
    lines.append(
        f'Approve: reply `approve check-v-update-{date_str}` on '
        f'Telegram, or `reject check-v-update-{date_str} <reason>`.'
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
            source='pulse-check-v',
            severity='warning',
            message=body,
            subject=f'check-v-update:{date_str}',
            suggested_action=(
                f'Review proposals; reply `approve check-v-update-{date_str}` or '
                f'`reject check-v-update-{date_str} <reason>`.'
            ),
        )
    except Exception as e:
        log(f'dm_digest failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- main --------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',
                        help='Read ledger rows + guard-list from a JSON '
                             'fixture file instead of the live ledger.')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true',
                        help='Bypass monthly idempotency.')
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    anchor = month_anchor(now.date())
    target_path = artifact_path_for_month(anchor)

    if target_path.exists() and not args.force and not args.dry_run:
        log(f'Check V already ran this month ({anchor}); '
            'skipping (use --force to re-run).')
        return 0

    if args.fixture:
        with open(args.fixture) as fh:
            raw = json.load(fh)
        rows = raw.get('rows', [])
        guard_list = set(raw.get('guard_list', []))
    else:
        rows = _read_ledger()
        guard_list = load_guard_list()

    result = run_check(rows=rows, guard_list=guard_list, now=now)
    artifact = build_artifact(result)

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    write_artifact(artifact)
    dm_digest(artifact)
    log(f'Check V complete: proposals={len(result.proposals)} '
        f'templates_evaluated={result.template_count}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
