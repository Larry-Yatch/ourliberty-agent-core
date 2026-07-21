#!/usr/bin/env python3
"""sort_once_tier4_cleanup.py — one-time grouped cleanup of stale tier-4 asks.

Larry decided 2026-07-07 (capture ``cap-run-sort-once-cleanup-of-352-stale-tier-4-asks``):
one grouped sort-once pass over the open tier-4 alert asks, run AFTER completeness
PR-1 landed so the decision-outcome ledger records the dispositions. One grouped
approval per class (keep / retire / park), not per-item.

Substrate reality (verified at build): the triage rows in ``alert-triage-state.json``
carry no subject / decision_key. The only per-row signals are a heterogeneous
``alert_id`` (a larry-alerts.jsonl line number, fully distinct across the open
rows — so NOT a recurrence key) plus ``tier`` / ``decision`` / ``route`` /
``rationale`` / ``triaged_at``. The emission-side join is dead (larry-alerts.jsonl
has no alert_id and is compacted), so this pass operates on the triage store alone
and keys recurrence off the row's own ``rationale`` signature, not ``alert_id``.

Per-row classification (pure):
  - keep   : ``rationale`` matches a known/recurring signature marker — a pattern
             worth keeping active regardless of age.
  - retire : a novel one-off at least ``retire_age_days`` old — the stale tail.
             Marked resolved (resolution='expired') and recorded in the
             decision-outcome ledger with outcome='expired'.
  - park   : a novel one-off younger than ``retire_age_days`` (or undatable) —
             too new to prove stale; left for a future pass.

Mirrors the Check VIII / Check III proposal-artifact + grouped-approve precedent:
``propose`` writes an artifact (``applied: false``) and DMs Larry one grouped
approve instruction; ``apply`` performs the retirements and flips
``applied: true`` (idempotent via the flag). A one-time operator action, not a
recurring pulse check.

Usage:
    python3 scripts/sort_once_tier4_cleanup.py propose            # artifact + DM
    python3 scripts/sort_once_tier4_cleanup.py apply              # retire + flip
    python3 scripts/sort_once_tier4_cleanup.py apply --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_RETIRE_AGE_DAYS = 30
# ``rationale`` substrings that mark a recurring / known signature to KEEP. The
# 'known never-silence pattern' class matched a known translation pattern, i.e.
# the underlying signature recurs — keep it active rather than retire it as a
# one-off.
RECURRING_SIGNATURE_MARKERS = ('known never-silence pattern',)
APPROVE_SLUG = 'sort-once-tier4-cleanup'


def _agents_root() -> Path:
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))


def _proposals_dir() -> Path:
    return _agents_root() / 'blackboard' / 'sort-once-tier4-cleanup'


def artifact_path() -> Path:
    return _proposals_dir() / 'sort-once-tier4-cleanup.json'


def _log_path() -> Path:
    return _agents_root() / 'logs' / 'sort-once-tier4-cleanup.log'


def _log(msg: str, level: str = 'INFO') -> None:
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] [{level}] sort_once_tier4_cleanup: {msg}\n')
    except OSError:
        pass


def _default_retire_age_days() -> int:
    try:
        return int(os.environ.get('OL_TIER4_RETIRE_AGE_DAYS', DEFAULT_RETIRE_AGE_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_RETIRE_AGE_DAYS


# -------------------- pure classification core --------------------


def _parse_ts(ts: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def is_target_row(row: Any) -> bool:
    """True for an OPEN tier-4 ask — the population this pass sorts.

    Union of the three signals my preflight verified against live data
    (tier==4 / status=='triaged-tier-4' / decision=='ask'), gated on the row
    being unresolved. A resolved row (or one carrying a ``resolved_at``) is
    already terminal and excluded.
    """
    if not isinstance(row, dict):
        return False
    if row.get('status') == 'resolved' or row.get('resolved_at'):
        return False
    return (
        row.get('tier') == 4
        or row.get('status') == 'triaged-tier-4'
        or row.get('decision') == 'ask'
    )


def _is_recurring(row: dict[str, Any]) -> bool:
    rationale = str(row.get('rationale') or '')
    return any(marker in rationale for marker in RECURRING_SIGNATURE_MARKERS)


def classify_row(row: dict[str, Any], now: datetime, retire_age_days: int) -> str:
    """Return 'keep' | 'retire' | 'park' for one target row.

    keep beats age: a recurring-signature row is kept even if old. A novel
    one-off retires only if we can PROVE it is at least ``retire_age_days`` old;
    an undatable ``triaged_at`` parks (conservative — never retire on an
    unprovable age)."""
    if _is_recurring(row):
        return 'keep'
    dt = _parse_ts(row.get('triaged_at'))
    if dt is None:
        return 'park'
    age_days = (now - dt).days
    return 'retire' if age_days >= retire_age_days else 'park'


def build_plan(rows_by_id: dict[str, dict[str, Any]], now: datetime,
               retire_age_days: int) -> dict[str, list[str]]:
    """Sort every target row into keep / retire / park, keyed by alert_id."""
    groups: dict[str, list[str]] = {'keep': [], 'retire': [], 'park': []}
    for alert_id, row in rows_by_id.items():
        if not is_target_row(row):
            continue
        groups[classify_row(row, now, retire_age_days)].append(alert_id)
    for key in groups:
        groups[key].sort()
    return groups


def build_artifact(groups: dict[str, list[str]], now: datetime,
                   retire_age_days: int) -> dict[str, Any]:
    return {
        'slug': APPROVE_SLUG,
        'as_of': now.isoformat(),
        'retire_age_days': retire_age_days,
        'counts': {key: len(groups[key]) for key in ('keep', 'retire', 'park')},
        'total_targets': sum(len(v) for v in groups.values()),
        'retire': list(groups['retire']),
        'keep': list(groups['keep']),
        'park': list(groups['park']),
        'recurring_signature_markers': list(RECURRING_SIGNATURE_MARKERS),
        'applied': False,
        'applied_at': None,
    }


# -------------------- digest + DM --------------------


def format_digest(artifact: dict[str, Any]) -> str:
    counts = artifact['counts']
    date_str = artifact['as_of'][:10]
    age = artifact['retire_age_days']
    return '\n'.join([
        f'Sort-once tier-4 cleanup — grouped disposition ({date_str})',
        '',
        f'{artifact["total_targets"]} open tier-4 asks, sorted once into three classes:',
        f'  retire : {counts["retire"]:>4}  novel one-offs >= {age}d old — mark resolved (expired) + ledger',
        f'  park   : {counts["park"]:>4}  novel one-offs < {age}d old — left for a future pass',
        f'  keep   : {counts["keep"]:>4}  recurring/known-signature rows — kept active',
        '',
        f'Approve the retire batch: reply `approve {APPROVE_SLUG}-{date_str}` on '
        f'Telegram, or `reject {APPROVE_SLUG}-{date_str} <reason>`. '
        f'keep/park need no action.',
    ])


def dm_digest(artifact: dict[str, Any]) -> bool:
    date_str = artifact['as_of'][:10]
    try:
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='sort-once-tier4-cleanup',
            severity='warning',
            message=format_digest(artifact),
            subject=f'{APPROVE_SLUG}:{date_str}',
            suggested_action=(
                f'Review the grouped disposition; reply '
                f'`approve {APPROVE_SLUG}-{date_str}` to retire the '
                f'{artifact["counts"]["retire"]} stale one-offs.'
            ),
            needs_larry=True,
        )
    except Exception as e:  # noqa: BLE001 — DM is best-effort, never fatal
        _log(f'dm_digest failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- artifact IO --------------------


def write_artifact(artifact: dict[str, Any]) -> Path:
    from atomic_io import atomic_write_json  # noqa: E402
    path = artifact_path()
    atomic_write_json(path, artifact, indent=2, sort_keys=True)
    return path


def read_artifact() -> Optional[dict[str, Any]]:
    path = artifact_path()
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


# -------------------- apply --------------------


def apply_cleanup(artifact: dict[str, Any], now: datetime, *,
                  ats_mod: Any = None, ledger_mod: Any = None) -> dict[str, Any]:
    """Retire the artifact's ``retire`` batch.

    For each retire alert_id: mark the triage row resolved (resolution='expired')
    and — only if that row actually existed — record an ``expired`` decision in
    the outcome ledger. Idempotent via ``artifact['applied']``: a second call is a
    no-op. Mutates ``artifact`` in place on the first successful pass
    (``applied=True``, ``applied_at`` stamped). Returns a summary dict."""
    if artifact.get('applied'):
        return {'already_applied': True, 'retired': 0,
                'ledger_recorded': 0, 'missing': []}
    if ats_mod is None:
        import alert_triage_state as ats_mod  # noqa: E402
    if ledger_mod is None:
        import decision_outcome_ledger as ledger_mod  # noqa: E402
    now_iso = now.isoformat()
    retired = 0
    recorded = 0
    missing: list[str] = []
    for alert_id in artifact.get('retire', []):
        if ats_mod.mark_resolved(alert_id, now_iso, 'expired'):
            retired += 1
            if ledger_mod.record_decision(
                f'tier4-ask-{alert_id}', 'expired',
                actor='sort-once-cleanup', cleared=1,
                notes='sort-once tier-4 stale one-off retirement',
            ):
                recorded += 1
        else:
            missing.append(alert_id)
    artifact['applied'] = True
    artifact['applied_at'] = now_iso
    return {'already_applied': False, 'retired': retired,
            'ledger_recorded': recorded, 'missing': missing}


# -------------------- CLI --------------------


def _read_state() -> dict[str, dict[str, Any]]:
    import alert_triage_state as ats  # noqa: E402
    return ats.read_state()


def cmd_propose(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    rows = _read_state()
    groups = build_plan(rows, now, args.retire_age_days)
    artifact = build_artifact(groups, now, args.retire_age_days)
    path = write_artifact(artifact)
    dmed = False if args.no_dm else dm_digest(artifact)
    print(format_digest(artifact))
    print(f'\nartifact: {path}  (dm={"sent" if dmed else "skipped"})')
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    artifact = read_artifact()
    if artifact is None:
        print('no artifact — run `propose` first', file=sys.stderr)
        return 2
    if args.dry_run:
        print(f'[dry-run] would retire {len(artifact.get("retire", []))} rows; '
              f'applied={artifact.get("applied")}')
        return 0
    summary = apply_cleanup(artifact, now)
    if not summary['already_applied']:
        write_artifact(artifact)
    print(f'retired={summary["retired"]} ledger={summary["ledger_recorded"]} '
          f'missing={len(summary["missing"])} '
          f'already_applied={summary["already_applied"]}')
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd')

    p_propose = sub.add_parser('propose', help='build artifact + DM Larry')
    p_propose.add_argument('--retire-age-days', type=int,
                           default=_default_retire_age_days())
    p_propose.add_argument('--no-dm', action='store_true',
                           help='write the artifact but do not DM')
    p_propose.set_defaults(func=cmd_propose)

    p_apply = sub.add_parser('apply', help='retire the approved batch')
    p_apply.add_argument('--dry-run', action='store_true')
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args(argv)
    if not getattr(args, 'cmd', None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == '__main__':
    _repo_scripts = str(Path(__file__).resolve().parent)
    if _repo_scripts not in sys.path:
        sys.path.insert(0, _repo_scripts)
    raise SystemExit(main())
