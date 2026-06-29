#!/usr/bin/env python3
"""pulse_check_retrospective.py — weekly elevation-retrospective, Stage A.

Spec: agents/beacon/specs/alert-pipeline-rework.md, PHASE 3 (Stage A).

Stage A is the *deterministic* half of the weekly retrospective: it mines
everything that reached Larry over the trailing window, normalizes each
elevation to a stable root signature, buckets the buckets with a resolution
histogram + weeks-recurring recurrence + trend-vs-last-period, applies a
promotion-probation-style dedup, and emits ``retrospective-candidates.json``.

It is pure data-prep. The bounded LLM author (Stage B / ``p3b-retro-author``)
consumes the candidates artifact to classify (AUTOMATE-NOW / FIX-PERMANENTLY /
KEEP-ELEVATING), pre-draft fixes, and post proposed missions. Stage A NEVER
classifies, posts a mission, opens a PR, or edits config — its only writes are
the candidates artifact (canonical + per-ISO-week sentinel) and the persistent
retrospective ledger.

Data + join (spec § PHASE 3 "Data + join"):
  - Elevations = chain_events ``escalation``, ``larry_alert`` where
    ``payload.route == 'escalate'``, ``sentinel_alert``, ``approval_request``,
    ``needs_attention``.
  - Resolutions = chain_events ``larry_action``, ``clarify_response``.
  - Join key: ``larry_action.payload.source_event_id -> elevation.event_id``
    (yields per elevation: did Larry act, and how).
  - Recurrence from ``larry-alerts.jsonl`` + the persistent retrospective
    ledger; live-unresolved cross-check against ``for-larry-escalations.json``;
    fixture task_ids filtered via ``is_fixture_task_id``.

Bucketing (spec § PHASE 3 "Bucketing"):
  - Root signature = ``source`` + normalized subject (strip trailing
    ``-YYYY-MM-DD``, ``#<num>`` / PR ids; collapse ``:``-tail to family —
    reusing the ``alert-translations.json`` strip rules).
  - Each bucket: count this period, weeks-recurring, resolution histogram
    (approve / reject / silence / ack / none), examples.

Plumbing cloned from ``pulse_check_ix.py`` (heartbeat ``run_check`` wrapper,
``supabase_factory`` fetch, ``atomic_io`` artifact write, ``--fixture`` /
``--dry-run`` / ``--force`` flags, validated per-ISO-week artifact sentinel).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
REPO_ROOT = Path(__file__).resolve().parent.parent

ALERTS_FILE = AGENTS_ROOT / 'blackboard' / 'larry-alerts.jsonl'
FOR_LARRY_ESCALATIONS = AGENTS_ROOT / 'blackboard' / 'for-larry-escalations.json'
ARTIFACT_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-retrospective'
CANONICAL_ARTIFACT = AGENTS_ROOT / 'blackboard' / 'retrospective-candidates.json'
LEDGER_FILE = AGENTS_ROOT / 'state' / 'retrospective-ledger.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-retrospective.log'

# Trailing window for the period's elevations + resolutions (spec § Cadence
# runs weekly; the period the run reports on is the trailing week).
LOOKBACK_DAYS = 7
# How far back to mine larry-alerts.jsonl for the weeks-recurring count. Bounded
# so the recurrence number stays meaningful (and the file read stays cheap).
RECURRENCE_LOOKBACK_DAYS = 56  # 8 weeks

# R1 (spec § Phase-3 sub-decisions): a bucket is recurrence-actionable at
# >= 3 fires in the period OR >= 2 distinct weeks running. Stage A only
# COMPUTES the flag; Stage B's LLM applies the classification gate.
RECURRENCE_COUNT_THRESHOLD = 3
RECURRENCE_WEEKS_THRESHOLD = 2

# Promotion-probation dedup: a dismissed signature stays quiet until its
# period-count re-crosses the count it was dismissed at, by this margin
# (modeled on promote_alerts.py's "promote-once until it re-crosses" anchor).
DEDUP_REISSUE_MARGIN = 2

# ---- chain_events event-type vocabulary (spec § "Data + join") ----
EV_ESCALATION = 'escalation'
EV_LARRY_ALERT = 'larry_alert'
EV_SENTINEL_ALERT = 'sentinel_alert'
EV_APPROVAL_REQUEST = 'approval_request'
EV_NEEDS_ATTENTION = 'needs_attention'
ELEVATION_EVENT_TYPES = (
    EV_ESCALATION,
    EV_LARRY_ALERT,
    EV_SENTINEL_ALERT,
    EV_APPROVAL_REQUEST,
    EV_NEEDS_ATTENTION,
)

EV_LARRY_ACTION = 'larry_action'
EV_CLARIFY_RESPONSE = 'clarify_response'
RESOLUTION_EVENT_TYPES = (EV_LARRY_ACTION, EV_CLARIFY_RESPONSE)

# Resolution histogram buckets (spec § Bucketing).
RES_APPROVE = 'approve'
RES_REJECT = 'reject'
RES_SILENCE = 'silence'
RES_ACK = 'ack'
RES_NONE = 'none'
RESOLUTION_BUCKETS = (RES_APPROVE, RES_REJECT, RES_SILENCE, RES_ACK, RES_NONE)

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from atomic_io import atomic_write_json  # noqa: E402
from fixture_patterns import is_fixture_task_id  # noqa: E402
from log_ts import parse_log_ts  # noqa: E402  (shared log-ts parser)


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


def _parse_ts(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    return parse_log_ts(s)


# -------------------- data shapes --------------------


@dataclass
class Elevation:
    event_id: str
    event_type: str
    task_id: str
    ts: datetime
    source: str
    subject: str          # raw subject (kept for examples)
    route: Optional[str] = None


@dataclass
class Resolution:
    event_id: str
    event_type: str
    task_id: str
    ts: datetime
    source_event_id: str  # the elevation event_id this action resolves
    bucket: str           # one of RESOLUTION_BUCKETS minus RES_NONE


@dataclass
class Bucket:
    root_signature: str
    source: str
    normalized_subject: str
    count_this_period: int
    weeks_recurring: int
    resolution_histogram: dict[str, int]
    examples: list[dict[str, Any]]
    event_types: list[str]
    live_unresolved: bool
    trend: str                       # new | rising | falling | steady
    last_period_count: int
    dedup_status: str                # fresh | already-proposed | suppressed-dismissed
    recurrence_actionable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            'root_signature': self.root_signature,
            'source': self.source,
            'normalized_subject': self.normalized_subject,
            'count_this_period': self.count_this_period,
            'weeks_recurring': self.weeks_recurring,
            'resolution_histogram': self.resolution_histogram,
            'examples': self.examples,
            'event_types': self.event_types,
            'live_unresolved': self.live_unresolved,
            'trend': self.trend,
            'last_period_count': self.last_period_count,
            'dedup_status': self.dedup_status,
            'recurrence_actionable': self.recurrence_actionable,
        }


# -------------------- root-signature normalization --------------------


# Reuses the alert-translations.json strip rules (mirrored in
# alert_triage_state._ISO_DATE_SUFFIX_RE): a trailing rotating ISO-date suffix
# is anchored to a leading '-' and runs to end-of-string so it only fires on a
# genuine -YYYY-MM-DD[...] tail, collapsing date-rotating subjects (e.g.
# weekly-2026-06-15) onto a stable family key (weekly).
_ISO_DATE_SUFFIX_RE = re.compile(r'-\d{4}-\d{2}-\d{2}.*$')
# `#<num>` issue/PR refs and `PR-<num>` / `PR <num>` / `pr/<num>` shapes.
_HASH_NUM_RE = re.compile(r'#\d+')
_PR_ID_RE = re.compile(r'\bpr[\s/_-]?\d+\b', re.IGNORECASE)
# Standalone long digit runs (PR/issue numbers that slipped the above shapes).
_BARE_NUM_RE = re.compile(r'\b\d{2,}\b')
# Whitespace/underscore runs fold to a single hyphen so word-spacing variants
# ("build failed" / "build_failed" / "build-failed") share one signature; a
# second pass collapses any resulting hyphen run.
_WS_RUN_RE = re.compile(r'[\s_]+')
_HYPHEN_RUN_RE = re.compile(r'-{2,}')


def normalize_subject(subject: Optional[str]) -> str:
    """Collapse a subject to its stable root family.

    Order (mirrors the alert-translations lookup_rule, applied as a one-shot
    normalization rather than a retry loop):
      1. collapse the ``:``-tail to the family head (everything before the
         first ``:``);
      2. strip a trailing ``-YYYY-MM-DD`` ISO-date suffix;
      3. strip ``#<num>`` / PR-id / bare-number refs;
      4. tidy separator runs and lowercase.
    An empty/None subject yields ``''`` (the caller substitutes a fallback).
    """
    if not isinstance(subject, str):
        return ''
    s = subject.strip()
    if not s:
        return ''
    # 1. collapse ':'-tail to family head.
    s = s.split(':', 1)[0]
    # 2. strip trailing ISO date suffix.
    s = _ISO_DATE_SUFFIX_RE.sub('', s)
    # 3. strip PR / issue refs.
    s = _HASH_NUM_RE.sub('', s)
    s = _PR_ID_RE.sub('', s)
    s = _BARE_NUM_RE.sub('', s)
    # 4. tidy.
    s = _WS_RUN_RE.sub('-', s)
    s = _HYPHEN_RUN_RE.sub('-', s)
    s = s.strip(' -_:').lower()
    return s


def root_signature(source: Optional[str], subject: Optional[str]) -> str:
    """``<source>::<normalized-subject>`` — the bucket key.

    Falls back to a stable placeholder when either half is empty so unrelated
    empties never collapse into one mega-bucket."""
    src = (source or '').strip().lower() or 'unknown-source'
    subj = normalize_subject(subject) or 'unknown-subject'
    return f'{src}::{subj}'


# -------------------- elevation / resolution extraction --------------------


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    p = row.get('payload')
    return p if isinstance(p, dict) else {}


def _first_str(d: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ''


def is_elevation_row(row: dict[str, Any]) -> bool:
    """True if a chain_events row counts as an elevation (spec § Data + join).

    ``larry_alert`` only counts when its payload route is ``escalate`` — the
    digest/hold/closure lines never DM'd Larry, so they are not elevations."""
    et = row.get('event_type')
    if et not in ELEVATION_EVENT_TYPES:
        return False
    if et == EV_LARRY_ALERT:
        return _payload(row).get('route') == 'escalate'
    return True


def build_elevation(row: dict[str, Any]) -> Optional[Elevation]:
    ts = _parse_ts(row.get('ts'))
    event_id = row.get('event_id')
    if ts is None or not isinstance(event_id, str) or not event_id:
        return None
    p = _payload(row)
    task_id = row.get('task_id') or ''
    if is_fixture_task_id(task_id):
        return None
    source = _first_str(p, 'source') or (row.get('agent') or '') or row.get('event_type', '')
    subject = _first_str(p, 'subject', 'headline', 'summary', 'message', 'reason')
    return Elevation(
        event_id=event_id,
        event_type=row.get('event_type', ''),
        task_id=task_id,
        ts=ts,
        source=source,
        subject=subject,
        route=p.get('route') if isinstance(p.get('route'), str) else None,
    )


# Maps a larry_action verb/decision to a histogram bucket. Unknown verbs land
# in `ack` ("Larry did something, but not a clear approve/reject/silence").
_RESOLUTION_VERB_MAP = {
    'approve': RES_APPROVE, 'approved': RES_APPROVE, 'accept': RES_APPROVE,
    'accepted': RES_APPROVE, 'yes': RES_APPROVE, 'go': RES_APPROVE,
    'ship': RES_APPROVE, 'proceed': RES_APPROVE,
    'reject': RES_REJECT, 'rejected': RES_REJECT, 'deny': RES_REJECT,
    'denied': RES_REJECT, 'no': RES_REJECT, 'dismiss': RES_REJECT,
    'dismissed': RES_REJECT, 'decline': RES_REJECT, 'declined': RES_REJECT,
    'silence': RES_SILENCE, 'silenced': RES_SILENCE, 'mute': RES_SILENCE,
    'muted': RES_SILENCE, 'snooze': RES_SILENCE, 'snoozed': RES_SILENCE,
    'ack': RES_ACK, 'acknowledge': RES_ACK, 'acknowledged': RES_ACK,
    'ok': RES_ACK, 'okay': RES_ACK, 'seen': RES_ACK, 'read': RES_ACK,
}


def classify_resolution(event_type: str, payload: dict[str, Any]) -> str:
    """Bucket a resolution event. ``clarify_response`` is always ``ack``
    (Larry answered a question); a ``larry_action`` reads its verb/decision."""
    if event_type == EV_CLARIFY_RESPONSE:
        return RES_ACK
    verb = _first_str(payload, 'action', 'decision', 'verb', 'response_type',
                      'outcome').lower()
    return _RESOLUTION_VERB_MAP.get(verb, RES_ACK)


def build_resolution(row: dict[str, Any]) -> Optional[Resolution]:
    ts = _parse_ts(row.get('ts'))
    event_id = row.get('event_id')
    if ts is None or not isinstance(event_id, str) or not event_id:
        return None
    p = _payload(row)
    source_event_id = _first_str(p, 'source_event_id')
    if not source_event_id:
        return None
    task_id = row.get('task_id') or ''
    if is_fixture_task_id(task_id):
        return None
    et = row.get('event_type', '')
    return Resolution(
        event_id=event_id,
        event_type=et,
        task_id=task_id,
        ts=ts,
        source_event_id=source_event_id,
        bucket=classify_resolution(et, p),
    )


def join_resolutions(
    resolutions: Iterable[Resolution],
) -> dict[str, list[Resolution]]:
    """Index resolutions by the elevation event_id they resolve."""
    by_eid: dict[str, list[Resolution]] = {}
    for r in resolutions:
        by_eid.setdefault(r.source_event_id, []).append(r)
    return by_eid


# -------------------- recurrence (larry-alerts.jsonl) --------------------


def _iso_week_of(dt: datetime) -> str:
    monday = dt.date() - timedelta(days=dt.date().weekday())
    return monday.isoformat()


def load_alert_signature_weeks(
    alerts_path: Path = ALERTS_FILE,
    *,
    now: Optional[datetime] = None,
    lookback_days: int = RECURRENCE_LOOKBACK_DAYS,
) -> dict[str, set[str]]:
    """Map each root signature to the set of ISO-week anchors it appeared in
    across larry-alerts.jsonl over the recurrence lookback window.

    This is the historical half of weeks-recurring; the ledger supplies the
    cross-run accumulation that survives log rotation."""
    out: dict[str, set[str]] = {}
    if not alerts_path.exists():
        return out
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
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
                ts = _parse_ts(rec.get('ts'))
                if ts is None or ts < cutoff:
                    continue
                sig = root_signature(rec.get('source'), rec.get('subject'))
                out.setdefault(sig, set()).add(_iso_week_of(ts))
    except OSError as e:
        log(f'read {alerts_path} failed: {e}', 'WARN')
    return out


# -------------------- live-unresolved cross-check --------------------


def load_live_unresolved_task_ids(
    path: Path = FOR_LARRY_ESCALATIONS,
) -> set[str]:
    """Task_ids of the currently-active for-Larry escalations.

    A bucket whose examples touch one of these is still live-unresolved — i.e.
    Larry has an open decision on it right now (spec § Data + join)."""
    out: set[str] = set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return out
    records = data.get('records') if isinstance(data, dict) else None
    if not isinstance(records, dict):
        return out
    for rec in records.values():
        if not isinstance(rec, dict):
            continue
        tid = rec.get('task_id')
        if isinstance(tid, str) and tid:
            out.add(tid)
    return out


# -------------------- ledger (recurrence + probation dedup) --------------------


def load_ledger(path: Path = LEDGER_FILE) -> dict[str, dict[str, Any]]:
    """The persistent retrospective ledger, keyed by root signature.

    Serves both recurrence (accumulated ``weeks``) and promotion-probation
    dedup (``proposed`` / ``dismissed`` flags set by Stage B / the board)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    sigs = data.get('signatures')
    return sigs if isinstance(sigs, dict) else {}


def write_ledger(
    signatures: dict[str, dict[str, Any]], path: Path = LEDGER_FILE,
) -> Path:
    payload = {
        'version': 1,
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'signatures': signatures,
    }
    return atomic_write_json(path, payload, indent=2)


def _dedup_status(prev: Optional[dict[str, Any]], count_this_period: int) -> str:
    """Promotion-probation dedup decision for a signature.

    - dismissed and the period count has NOT re-crossed the dismiss-count by
      the reissue margin -> ``suppressed-dismissed`` (stays quiet);
    - already proposed (and not dismissed) -> ``already-proposed`` (Stage B
      won't re-propose the same open card);
    - otherwise -> ``fresh``.
    """
    if not prev:
        return 'fresh'
    if prev.get('dismissed'):
        dismissed_at = prev.get('dismissed_at_count')
        floor = (dismissed_at or 0) + DEDUP_REISSUE_MARGIN
        if count_this_period < floor:
            return 'suppressed-dismissed'
        return 'fresh'  # re-crossed by the margin — surface again
    if prev.get('proposed'):
        return 'already-proposed'
    return 'fresh'


def _trend(prev: Optional[dict[str, Any]], count_this_period: int) -> str:
    if not prev or not prev.get('weeks'):
        return 'new'
    last = prev.get('last_period_count')
    if not isinstance(last, int):
        return 'new'
    if count_this_period > last:
        return 'rising'
    if count_this_period < last:
        return 'falling'
    return 'steady'


# -------------------- bucketing --------------------


def bucket_elevations(
    elevations: list[Elevation],
    resolutions_by_eid: dict[str, list[Resolution]],
    *,
    alert_signature_weeks: dict[str, set[str]],
    ledger: dict[str, dict[str, Any]],
    live_unresolved_task_ids: set[str],
    week_anchor: str,
    max_examples: int = 5,
) -> list[Bucket]:
    """Collapse elevations into root-signature buckets with histograms,
    weeks-recurring, trend, dedup status, and examples."""
    grouped: dict[str, list[Elevation]] = {}
    for ev in elevations:
        grouped.setdefault(root_signature(ev.source, ev.subject), []).append(ev)

    buckets: list[Bucket] = []
    for sig, evs in grouped.items():
        evs_sorted = sorted(evs, key=lambda e: e.ts)
        count = len(evs_sorted)

        histogram = {b: 0 for b in RESOLUTION_BUCKETS}
        for ev in evs_sorted:
            res = resolutions_by_eid.get(ev.event_id) or []
            if not res:
                histogram[RES_NONE] += 1
                continue
            # An elevation can have several actions; count the latest decision.
            latest = max(res, key=lambda r: r.ts)
            histogram[latest.bucket] += 1

        prev = ledger.get(sig)
        weeks = set(alert_signature_weeks.get(sig, set()))
        if prev and isinstance(prev.get('weeks'), list):
            weeks.update(w for w in prev['weeks'] if isinstance(w, str))
        weeks.add(week_anchor)
        weeks_recurring = len(weeks)

        live = any(ev.task_id in live_unresolved_task_ids for ev in evs_sorted)

        examples = [
            {
                'event_id': ev.event_id,
                'task_id': ev.task_id,
                'event_type': ev.event_type,
                'subject': ev.subject,
                'ts': ev.ts.isoformat(),
            }
            for ev in evs_sorted[:max_examples]
        ]

        event_types = sorted({ev.event_type for ev in evs_sorted})

        buckets.append(Bucket(
            root_signature=sig,
            source=evs_sorted[0].source,
            normalized_subject=normalize_subject(evs_sorted[0].subject),
            count_this_period=count,
            weeks_recurring=weeks_recurring,
            resolution_histogram=histogram,
            examples=examples,
            event_types=event_types,
            live_unresolved=live,
            trend=_trend(prev, count),
            last_period_count=(
                prev.get('last_period_count')
                if prev and isinstance(prev.get('last_period_count'), int)
                else 0
            ),
            dedup_status=_dedup_status(prev, count),
            recurrence_actionable=(
                count >= RECURRENCE_COUNT_THRESHOLD
                or weeks_recurring >= RECURRENCE_WEEKS_THRESHOLD
            ),
        ))

    buckets.sort(key=lambda b: (-b.count_this_period, b.root_signature))
    return buckets


# -------------------- ledger update + resolved detection --------------------


def update_ledger(
    ledger: dict[str, dict[str, Any]],
    buckets: list[Bucket],
    *,
    alert_signature_weeks: dict[str, set[str]],
    week_anchor: str,
    now: datetime,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(new_ledger, resolved)``.

    ``new_ledger`` carries this period's weeks/count merged into every prior
    signature. ``resolved`` = signatures that were present last period but had
    zero elevations this period (the Phase-4 "echo volume dropped" signal)."""
    now_iso = now.isoformat()
    new_ledger: dict[str, dict[str, Any]] = {}
    seen_this_period: set[str] = set()

    for b in buckets:
        seen_this_period.add(b.root_signature)
        prev = ledger.get(b.root_signature) or {}
        weeks = set(alert_signature_weeks.get(b.root_signature, set()))
        if isinstance(prev.get('weeks'), list):
            weeks.update(w for w in prev['weeks'] if isinstance(w, str))
        weeks.add(week_anchor)
        new_ledger[b.root_signature] = {
            'first_seen_ts': prev.get('first_seen_ts') or now_iso,
            'last_seen_ts': now_iso,
            'last_period_count': b.count_this_period,
            'weeks': sorted(weeks),
            # dedup flags are owned by Stage B / the board; carry them forward.
            'proposed': bool(prev.get('proposed')),
            'proposed_ts': prev.get('proposed_ts'),
            'dismissed': bool(prev.get('dismissed')),
            'dismissed_ts': prev.get('dismissed_ts'),
            'dismissed_at_count': prev.get('dismissed_at_count'),
        }

    resolved: list[dict[str, Any]] = []
    for sig, prev in ledger.items():
        if sig in seen_this_period:
            continue
        # Carry the prior entry forward (zero this period) and record it as
        # resolved if it had elevations last period.
        carried = dict(prev)
        last = prev.get('last_period_count')
        carried['last_period_count'] = 0
        carried['last_seen_ts'] = prev.get('last_seen_ts')
        new_ledger[sig] = carried
        if isinstance(last, int) and last > 0:
            resolved.append({'root_signature': sig, 'last_period_count': last})

    resolved.sort(key=lambda r: (-r['last_period_count'], r['root_signature']))
    return new_ledger, resolved


# -------------------- artifact --------------------


def iso_week_monday(d: date) -> str:
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def sentinel_path_for_week(week_anchor: str) -> Path:
    return ARTIFACT_DIR / f'retrospective-candidates-{week_anchor}.json'


def _trend_line(buckets: list[Bucket], resolved: list[dict[str, Any]]) -> str:
    """One plain-language summary line (spec § Trend/closure — not per-bucket
    spam)."""
    rising = sum(1 for b in buckets if b.trend == 'rising')
    new = sum(1 for b in buckets if b.trend == 'new')
    falling = sum(1 for b in buckets if b.trend == 'falling')
    return (
        f'{len(buckets)} elevation bucket(s) this period: '
        f'{new} new, {rising} rising, {falling} falling, '
        f'{len(resolved)} resolved since last period.'
    )


def build_artifact(
    buckets: list[Bucket],
    resolved: list[dict[str, Any]],
    *,
    week_anchor: str,
    as_of_iso: str,
    total_elevations: int,
    lookback_days: int = LOOKBACK_DAYS,
) -> dict[str, Any]:
    fresh = [b for b in buckets if b.dedup_status == 'fresh']
    suppressed = [b for b in buckets if b.dedup_status == 'suppressed-dismissed']
    return {
        'as_of': as_of_iso,
        'week_anchor': week_anchor,
        'lookback_days': lookback_days,
        'summary': {
            'total_elevations': total_elevations,
            'total_buckets': len(buckets),
            'fresh_candidates': len(fresh),
            'suppressed': len(suppressed),
            'resolved_since_last': len(resolved),
            'trend_line': _trend_line(buckets, resolved),
        },
        'candidates': [b.to_dict() for b in buckets],
        'resolved': resolved,
    }


def write_artifact(artifact: dict[str, Any]) -> tuple[Path, Path]:
    """Write the per-ISO-week sentinel AND the canonical latest artifact.

    Both atomic (atomic_io): the per-week file doubles as the same-week
    idempotency sentinel (the validated ``main()`` gate), so a mid-write crash
    must leave either the intact prior or the intact new file — never a
    truncated one that satisfies ``.exists()`` and permanently suppresses the
    check (the audit-#7 failure mode that bit pulse_check_viii)."""
    week_anchor = artifact['week_anchor']
    sentinel = atomic_write_json(sentinel_path_for_week(week_anchor), artifact,
                                 indent=2)
    canonical = atomic_write_json(CANONICAL_ARTIFACT, artifact, indent=2)
    return sentinel, canonical


def _artifact_is_valid_sentinel(path: Path) -> bool:
    """True iff ``path`` holds a parseable artifact (a real same-week
    sentinel). A zero-byte/truncated file satisfies ``.exists()`` but is not a
    completion marker; treat it as 'not yet run' (audit #7 hardening)."""
    try:
        obj = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(obj, dict) and bool(obj.get('week_anchor'))


# -------------------- chain_events fetch --------------------


def _fetch_rows(client, event_types: Iterable[str], cutoff_iso: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event_type in event_types:
        page = 0
        page_size = 1000
        while True:
            res = (
                client.table('chain_events')
                      .select('event_id,event_type,task_id,agent,ts,payload')
                      .eq('event_type', event_type)
                      .gte('ts', cutoff_iso)
                      .order('ts')
                      .range(page * page_size, (page + 1) * page_size - 1)
                      .execute()
            )
            rows = getattr(res, 'data', None) or []
            out.extend(r for r in rows if isinstance(r, dict))
            if len(rows) < page_size:
                break
            page += 1
    return out


def fetch_from_supabase(
    client, *, lookback_days: int = LOOKBACK_DAYS,
) -> tuple[list[Elevation], list[Resolution]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_iso = cutoff.isoformat()
    elevation_rows = _fetch_rows(client, ELEVATION_EVENT_TYPES, cutoff_iso)
    resolution_rows = _fetch_rows(client, RESOLUTION_EVENT_TYPES, cutoff_iso)
    elevations = [
        e for e in (build_elevation(r) for r in elevation_rows if is_elevation_row(r))
        if e is not None
    ]
    resolutions = [
        r for r in (build_resolution(row) for row in resolution_rows)
        if r is not None
    ]
    return elevations, resolutions


def _connect_supabase():
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — '
            'cannot run the retrospective elevation gather.'
        )
    from supabase_factory import get_supabase_client  # type: ignore
    return get_supabase_client(url, key)


# -------------------- orchestration --------------------


def run_retrospective(
    *,
    elevations: list[Elevation],
    resolutions: list[Resolution],
    alert_signature_weeks: dict[str, set[str]],
    ledger: dict[str, dict[str, Any]],
    live_unresolved_task_ids: set[str],
    week_anchor: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Pure entry point — returns ``(artifact, new_ledger)`` from pre-loaded
    inputs. No IO; the whole flow is unit-testable without Supabase or disk."""
    resolutions_by_eid = join_resolutions(resolutions)
    buckets = bucket_elevations(
        elevations, resolutions_by_eid,
        alert_signature_weeks=alert_signature_weeks,
        ledger=ledger,
        live_unresolved_task_ids=live_unresolved_task_ids,
        week_anchor=week_anchor,
    )
    new_ledger, resolved = update_ledger(
        ledger, buckets,
        alert_signature_weeks=alert_signature_weeks,
        week_anchor=week_anchor, now=now,
    )
    artifact = build_artifact(
        buckets, resolved,
        week_anchor=week_anchor, as_of_iso=now.isoformat(),
        total_elevations=len(elevations),
    )
    return artifact, new_ledger


# -------------------- main --------------------


def _load_fixture(path: str, now: datetime) -> tuple[
    list[Elevation], list[Resolution], dict[str, set[str]],
    dict[str, dict[str, Any]], set[str],
]:
    with open(path) as fh:
        raw = json.load(fh)
    elevations = [
        e for e in (build_elevation(r) for r in raw.get('elevation_rows', [])
                    if is_elevation_row(r))
        if e is not None
    ]
    resolutions = [
        r for r in (build_resolution(row) for row in raw.get('resolution_rows', []))
        if r is not None
    ]
    alert_weeks = {
        sig: set(weeks) for sig, weeks in raw.get('alert_signature_weeks', {}).items()
    }
    ledger = raw.get('ledger', {}) if isinstance(raw.get('ledger'), dict) else {}
    live = set(raw.get('live_unresolved_task_ids', []))
    return elevations, resolutions, alert_weeks, ledger, live


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute + print the candidates artifact; do not '
                             'write the artifact or the ledger.')
    parser.add_argument('--force', action='store_true',
                        help='Bypass the weekly idempotency sentinel and re-run.')
    parser.add_argument('--fixture',
                        help='Read all inputs from a single JSON fixture file '
                             '(test-only path).')
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    week_anchor = iso_week_monday(now.date())
    sentinel = sentinel_path_for_week(week_anchor)

    if (sentinel.exists() and _artifact_is_valid_sentinel(sentinel)
            and not args.force and not args.dry_run):
        log(f'Retrospective already ran this week ({week_anchor}); '
            'skipping (use --force to re-run).')
        return 0

    if args.fixture:
        (elevations, resolutions, alert_weeks,
         ledger, live) = _load_fixture(args.fixture, now)
    else:
        try:
            supa = _connect_supabase()
            elevations, resolutions = fetch_from_supabase(supa)
        except (RuntimeError, ImportError) as e:
            log(f'elevation gather unavailable ({e}); cannot run', 'WARN')
            return 1
        alert_weeks = load_alert_signature_weeks(now=now)
        ledger = load_ledger()
        live = load_live_unresolved_task_ids()

    artifact, new_ledger = run_retrospective(
        elevations=elevations, resolutions=resolutions,
        alert_signature_weeks=alert_weeks, ledger=ledger,
        live_unresolved_task_ids=live, week_anchor=week_anchor, now=now,
    )

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    write_artifact(artifact)
    write_ledger(new_ledger)
    s = artifact['summary']
    log(
        f'Retrospective complete: elevations={s["total_elevations"]} '
        f'buckets={s["total_buckets"]} fresh={s["fresh_candidates"]} '
        f'suppressed={s["suppressed"]} resolved={s["resolved_since_last"]}'
    )
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check
    sys.exit(run_check('retrospective', main, log_fn=log))
