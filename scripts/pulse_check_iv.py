#!/usr/bin/env python3
"""pulse_check_iv.py — marker-drift enforcement-strictness analyzer.

Spec: agents/beacon/specs/pulse-cycle-upgrade.md § 12.3 (Check IV row +
proposal-firing rule).

Weekly cadence (Mondays). Data substrate: ``chain_events`` rows whose
``event_type`` starts with ``mirror_marker_invisible:`` over the
trailing 4 weeks (28d). Proposal-firing rule: rate > 2/week (i.e. > 8
events across the 4w window) → propose enforcement tightening (Mirror
prompt re-emphasis OR a hard validator gate on marker shape).

Follows the pulse_check_viii.py shape: deterministic, stdlib-only,
sentinel-cum-artifact (the artifact file at
``~/agents/blackboard/pulse-check-iv-proposals/check-iv-<monday>.json``
gates re-execution within the same ISO week), idempotent on same-week
sentinel. DM via ``larry_alerts.append_alert`` only when the proposal
fires.

No LLM calls. ``subprocess`` is used solely by Supabase's SDK; we
import it lazily inside ``_connect_supabase`` so the test path never
touches it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

AGENTS_ROOT = Path(
    os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents'))
)
PROPOSALS_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-iv-proposals'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-iv.log'

LOOKBACK_DAYS = 28
RATE_PER_WEEK_FLOOR = 2.0     # > 2/week (strict greater than)
EVENT_PREFIX = 'mirror_marker_invisible:'


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


@dataclass
class MarkerEvent:
    ts: datetime
    event_type: str
    task_id: str = ''


@dataclass
class CheckIVResult:
    rule_fired: str                       # 'propose' or 'none'
    event_count: int
    rate_per_week: float
    rationale: str
    week_anchor: str
    as_of_iso: str
    examples: list[str]


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


def iso_week_monday(d: date) -> str:
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def artifact_path_for_week(week_anchor: str) -> Path:
    return PROPOSALS_DIR / f'check-iv-{week_anchor}.json'


def filter_window(events: Iterable[MarkerEvent], *, now: datetime,
                  lookback_days: int = LOOKBACK_DAYS) -> list[MarkerEvent]:
    cutoff = now - timedelta(days=lookback_days)
    return [e for e in events if e.ts >= cutoff]


def run_check(
    *,
    events: list[MarkerEvent],
    now: Optional[datetime] = None,
    lookback_days: int = LOOKBACK_DAYS,
    rate_per_week_floor: float = RATE_PER_WEEK_FLOOR,
) -> CheckIVResult:
    """Pure-function entry point. Returns a CheckIVResult."""
    if now is None:
        now = datetime.now(timezone.utc)
    in_window = filter_window(events, now=now, lookback_days=lookback_days)
    n = len(in_window)
    weeks = lookback_days / 7.0
    rate = (n / weeks) if weeks > 0 else 0.0
    week_anchor = iso_week_monday(now.date())
    examples = [e.event_type for e in in_window[-5:]]

    if rate > rate_per_week_floor:
        rationale = (
            f'{n} mirror_marker_invisible:* events over trailing '
            f'{lookback_days}d ({rate:.2f}/week > floor '
            f'{rate_per_week_floor:.1f}/week). Propose enforcement '
            'tightening: Mirror prompt re-emphasis OR a hard validator '
            'gate on marker shape.'
        )
        return CheckIVResult(
            rule_fired='propose', event_count=n, rate_per_week=rate,
            rationale=rationale, week_anchor=week_anchor,
            as_of_iso=now.isoformat(), examples=examples,
        )
    rationale = (
        f'{n} events over trailing {lookback_days}d '
        f'({rate:.2f}/week ≤ floor {rate_per_week_floor:.1f}/week). '
        'No proposal — enforcement is working as configured.'
    )
    return CheckIVResult(
        rule_fired='none', event_count=n, rate_per_week=rate,
        rationale=rationale, week_anchor=week_anchor,
        as_of_iso=now.isoformat(), examples=examples,
    )


def build_artifact(result: CheckIVResult) -> dict[str, Any]:
    return {
        'as_of': result.as_of_iso,
        'week_anchor': result.week_anchor,
        'check': 'IV',
        'rule_fired': result.rule_fired,
        'event_count': result.event_count,
        'rate_per_week': round(result.rate_per_week, 4),
        'rate_per_week_floor': RATE_PER_WEEK_FLOOR,
        'lookback_days': LOOKBACK_DAYS,
        'examples': result.examples,
        'rationale': result.rationale,
        'applied': False,
    }


def write_artifact(artifact: dict[str, Any]) -> Path:
    """Atomic write of the proposal artifact (tmp + replace)."""
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = artifact_path_for_week(artifact['week_anchor'])
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(artifact, indent=2))
    tmp.replace(path)
    return path


def format_digest(artifact: dict[str, Any]) -> str:
    rule = artifact['rule_fired']
    date_str = artifact['as_of'][:10]
    n = artifact['event_count']
    rate = artifact['rate_per_week']
    if rule == 'propose':
        header = ('Check IV — propose marker-enforcement TIGHTENING '
                  f'({date_str})')
    else:
        header = f'Check IV — no proposal (rate within bounds) ({date_str})'
    lines = [
        header,
        '',
        f'mirror_marker_invisible:* events (trailing '
        f'{artifact["lookback_days"]}d): {n} '
        f'({rate:.2f}/week vs floor '
        f'{artifact["rate_per_week_floor"]:.1f}/week)',
        '',
        artifact['rationale'],
    ]
    if rule == 'propose':
        lines.append('')
        lines.append(
            f'Approve: reply `approve check-iv-update-{date_str}` on '
            f'Telegram, or `reject check-iv-update-{date_str} <reason>`.'
        )
    return '\n'.join(lines)


def dm_digest(artifact: dict[str, Any]) -> bool:
    if artifact['rule_fired'] != 'propose':
        return False
    body = format_digest(artifact)
    date_str = artifact['as_of'][:10]
    try:
        import larry_alerts as la  # local import: tests can stub
        return la.append_alert(
            source='pulse-check-iv',
            severity='warning',
            message=body,
            subject=f'check-iv-update:{date_str}',
            suggested_action=(
                f'Review artifact at {artifact_path_for_week(artifact["week_anchor"])}; '
                f'reply `approve check-iv-update-{date_str}` or '
                f'`reject check-iv-update-{date_str} <reason>`.'
            ),
        )
    except Exception as e:
        log(f'dm_digest failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- substrate IO --------------------


def fetch_marker_events_from_supabase(
    client, *, lookback_days: int = LOOKBACK_DAYS,
) -> list[MarkerEvent]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_iso = cutoff.isoformat()
    out: list[MarkerEvent] = []
    page = 0
    page_size = 1000
    while True:
        res = (
            client.table('chain_events')
                  .select('event_type,ts,task_id')
                  .like('event_type', f'{EVENT_PREFIX}%')
                  .gte('ts', cutoff_iso)
                  .order('ts')
                  .range(page * page_size, (page + 1) * page_size - 1)
                  .execute()
        )
        rows = getattr(res, 'data', None) or []
        for row in rows:
            ts = _parse_ts(row.get('ts'))
            if ts is None:
                continue
            event_type = row.get('event_type') or ''
            if not isinstance(event_type, str):
                continue
            if not event_type.startswith(EVENT_PREFIX):
                continue
            out.append(MarkerEvent(
                ts=ts, event_type=event_type,
                task_id=row.get('task_id', '') or '',
            ))
        if len(rows) < page_size:
            break
        page += 1
    return out


def _connect_supabase():
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — '
            'cannot run Check IV.'
        )
    from supabase_factory import get_supabase_client  # type: ignore
    return get_supabase_client(url, key)


# -------------------- main --------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',
                        help='Read events from a JSON fixture file instead '
                             'of querying chain_events.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute + print but do not write artifact '
                             'or send DM.')
    parser.add_argument('--force', action='store_true',
                        help='Bypass weekly idempotency.')
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    week_anchor = iso_week_monday(now.date())
    target_path = artifact_path_for_week(week_anchor)

    # Sentinel-cum-artifact gate: same-week artifact -> skip (per spec
    # § 12.3 step-5 idempotency).
    if target_path.exists() and not args.force and not args.dry_run:
        log(f'Check IV already ran this week ({week_anchor}); '
            'skipping (use --force to re-run).')
        return 0

    if args.fixture:
        with open(args.fixture) as fh:
            raw = json.load(fh)
        events = [
            MarkerEvent(
                ts=_parse_ts(e['ts']) or now,
                event_type=e.get('event_type', EVENT_PREFIX + 'unknown'),
                task_id=e.get('task_id', ''),
            )
            for e in raw.get('events', [])
        ]
    else:
        try:
            client = _connect_supabase()
            events = fetch_marker_events_from_supabase(client)
        except Exception as e:
            log(f'cannot fetch events: {type(e).__name__}: {e}', 'ERROR')
            return 1

    result = run_check(events=events, now=now)
    artifact = build_artifact(result)

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    write_artifact(artifact)
    dm_digest(artifact)
    log(f'Check IV complete: rule={result.rule_fired} '
        f'events={result.event_count} rate={result.rate_per_week:.2f}/week')
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check as _hb_run_check
    sys.exit(_hb_run_check('iv', main, log_fn=log))
