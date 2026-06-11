#!/usr/bin/env python3
"""pulse_check_ix.py — weekly operator-friction signal analyzer.

Spec: agents/beacon/specs/pulse-check-ix-operator-friction.md.

Check IX fires weekly (Mondays, alongside Check VIII) and scans four
operator-friction signals against trailing-7d evidence. Each signal that
crosses its threshold registers a `phase: drafting` mission via the
existing `POST /api/system/missions/new` endpoint (E4.4f PR-A). Larry
reviews drafted missions on the kanban; promote-to-ready stays manual.

Signals (per spec § 2):

  - **catch-me-up-gap** — `> 3` status-probe messages in the beacon-bot
    log over the trailing 7d.
  - **time-to-action-gap** — `> 5` step-dispatched events whose
    time-to-action exceeds `2 * rolling-median` and have no operator
    action within the same window.
  - **alert-ignored** — same `subject` in `larry-alerts.jsonl` fires
    `>= 3` times in 7d with no commit reference and no operator
    shortcut in the same window.
  - **rescue-burden** — `>= 2` `intent=clarification-exhausted` events
    in the outbox-notifier log over the trailing 7d.

Idempotency (spec § 3): before POSTing, the analyzer queries
`GET /api/system/missions` and skips registration when an existing entry
whose `id` starts with `pulse-check-ix-<signal>-` is still in
`phase: drafting`. There is no mission-PATCH endpoint today, so first
cycle registers and later cycles silently skip (the cycle's journal
still notes the recurring signal).

The analyzer NEVER writes the spec, the brief PR, or the mission entry
directly — every registration goes through the dashboard-api so audit
trail (chain_events + the auto-generated PR) stays consistent with
Larry's manual `+ New mission` flow.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
REPO_ROOT = Path(__file__).resolve().parent.parent

BEACON_BOT_LOG = AGENTS_ROOT / 'logs' / 'beacon_telegram_bot.log'
OUTBOX_NOTIFIER_LOG = AGENTS_ROOT / 'logs' / 'outbox-notifier.log'
ALERTS_FILE = AGENTS_ROOT / 'blackboard' / 'larry-alerts.jsonl'
PROPOSALS_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-ix-proposals'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-ix.log'

LOOKBACK_DAYS = 7
TIME_TO_ACTION_MULTIPLIER = 2.0
TIME_TO_ACTION_MIN_FRICTION = 5

CATCH_ME_UP_THRESHOLD = 3      # `> N` per spec § 2.1
TIME_TO_ACTION_THRESHOLD = 5   # `> N` per spec § 2.2
ALERT_IGNORED_THRESHOLD = 3    # `>= N` per spec § 2.3
RESCUE_BURDEN_THRESHOLD = 2    # `>= N` per spec § 2.4

DEFAULT_API_BASE = 'http://127.0.0.1:8000'
DEFAULT_REPO = 'ourliberty-agent-core'

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


# -------------------- data shapes --------------------


SIGNAL_CATCH_ME_UP = 'catch-me-up-gap'
SIGNAL_TIME_TO_ACTION = 'time-to-action-gap'
SIGNAL_ALERT_IGNORED = 'alert-ignored'
SIGNAL_RESCUE_BURDEN = 'rescue-burden'

ALL_SIGNALS = (
    SIGNAL_CATCH_ME_UP,
    SIGNAL_TIME_TO_ACTION,
    SIGNAL_ALERT_IGNORED,
    SIGNAL_RESCUE_BURDEN,
)


@dataclass
class StatusProbe:
    ts: datetime
    text: str


@dataclass
class StepEvent:
    """A chain_events row reduced to (task_id, event_type, ts)."""
    task_id: str
    event_type: str
    ts: datetime


@dataclass
class FrictionEvent:
    task_id: str
    time_to_action_sec: float
    dispatched_ts: datetime


@dataclass
class AlertEntry:
    ts: datetime
    subject: str
    source: str


@dataclass
class ClarificationExhausted:
    ts: datetime
    task_id: str


@dataclass
class SignalFinding:
    signal: str                   # one of ALL_SIGNALS
    headline: str                 # one-line operator-readable name
    evidence_count: int
    evidence_summary: str         # `Evidence: N <unit> over trailing 7d`
    suggested_fix: str            # 1-sentence shape proposal
    detail: dict[str, Any] = field(default_factory=dict)

    def mission_name(self, cycle_date: date) -> str:
        # Kebab-cases predictably (per dashboard_api._kebab_case) to
        # `pulse-check-ix-<signal>-<YYYY-MM-DD>`.
        return f'Pulse Check IX {self.signal} {cycle_date.isoformat()}'

    def mission_brief(self) -> str:
        return (
            f'{self.headline} | {self.evidence_summary} | '
            f'Suggested fix shape: {self.suggested_fix}'
        )


# -------------------- timestamp parsing --------------------


_ISO_RE = re.compile(r'\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?')

# Beacon-bot log lines look like:
#   [2026-05-13T11:55:24-0600] <- 7998341473: 'status'
# Outbox-notifier log lines look like:
#   [2026-05-13 10:31:58] [notifier] [INFO] queued completion DM ...
_LOG_LINE_TS_RE = re.compile(r'^\[([0-9T:\- .+]+?)\]')


def _parse_iso_or_log_ts(s: str) -> Optional[datetime]:
    # Beacon-bot lines carry an aware `-0600` offset and the alert / chain_event
    # / row `ts` fields are aware UTC isoformat, while the outbox-notifier shape
    # ("2026-05-13 10:31:58") is naive host-local. parse_log_ts resolves each to
    # the correct UTC instant — reading a naive value in the system zone, not
    # pinning it to UTC (which skewed the trailing-window math ~6h).
    return parse_log_ts(s)


def _extract_line_ts(line: str) -> Optional[datetime]:
    m = _LOG_LINE_TS_RE.match(line)
    if not m:
        return None
    return _parse_iso_or_log_ts(m.group(1))


# -------------------- signal 2.1: catch-me-up-gap --------------------


_STATUS_PROBE_PATTERNS = (
    r'\bstatus\b',
    r"what.{0,3}is.{0,3}happen(?:ing)?",
    r"what['’]?s.{0,3}happen(?:ing)?",
    r"is.{0,3}everything.{0,3}continu(?:ing)?",
    r"any.{0,3}update",
    r"where.{0,3}are.{0,3}we",
    r"where.{0,3}are.{0,3}things",
)
_STATUS_PROBE_RE = re.compile(
    '|'.join(f'(?:{p})' for p in _STATUS_PROBE_PATTERNS),
    re.IGNORECASE,
)

# Beacon-bot logs Larry's incoming messages with a `<- <chat-id>:` prefix.
_BEACON_INCOMING_RE = re.compile(r'<-\s*\d+:\s*(.*)$')


def parse_beacon_bot_log(
    log_path: Path = BEACON_BOT_LOG,
    *,
    now: Optional[datetime] = None,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[StatusProbe]:
    """Scan the beacon-bot log for Larry-originated status-probe messages
    in the trailing window. Returns one `StatusProbe` per matching line."""
    if not log_path.exists():
        return []
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    out: list[StatusProbe] = []
    try:
        with open(log_path, errors='replace') as fh:
            for line in fh:
                ts = _extract_line_ts(line)
                if ts is None or ts < cutoff:
                    continue
                m = _BEACON_INCOMING_RE.search(line)
                if not m:
                    continue
                text = m.group(1).strip()
                # Strip outer quotes if present (bot logs use ' or ").
                if len(text) >= 2 and text[0] in ('"', "'") and text[-1] == text[0]:
                    text = text[1:-1]
                if _STATUS_PROBE_RE.search(text):
                    out.append(StatusProbe(ts=ts, text=text))
    except OSError as e:
        log(f'read {log_path} failed: {e}', 'WARN')
        return []
    return out


def signal_catch_me_up(
    probes: list[StatusProbe],
    *,
    threshold: int = CATCH_ME_UP_THRESHOLD,
) -> Optional[SignalFinding]:
    """Fires when probe count strictly exceeds threshold (spec § 2.1)."""
    n = len(probes)
    if n <= threshold:
        return None
    return SignalFinding(
        signal=SIGNAL_CATCH_ME_UP,
        headline='Operator status-probe rate above threshold',
        evidence_count=n,
        evidence_summary=f'Evidence: {n} status-probe events over trailing 7d',
        suggested_fix=(
            'Add a Telegram catch-me-up shortcut that summarizes in-flight '
            'PRs, paused sequences, and pending approvals without manual prompting.'
        ),
        detail={'threshold': threshold, 'examples': [p.text[:120] for p in probes[-3:]]},
    )


# -------------------- signal 2.2: time-to-action-gap --------------------


# Per spec § 2.2: pair `step-dispatched` events with subsequent
# `step-merged` or operator-shortcut events. Treat any of the following
# event_types as a "step-action" close-out for the dispatched event.
_STEP_DISPATCHED_TYPE = 'step-dispatched'
_STEP_ACTION_TYPES = frozenset({
    'step-merged',
    'step-skipped',
    'step-retried',
    'step-failed',
})


def pair_step_events(
    events: Iterable[StepEvent],
) -> list[tuple[StepEvent, StepEvent]]:
    """For each task_id with a step-dispatched, pair with the first
    later step-action. Returns `(dispatched, action)` pairs only when
    both halves exist."""
    by_task: dict[str, list[StepEvent]] = {}
    for ev in events:
        if not ev.task_id:
            continue
        if is_fixture_task_id(ev.task_id):
            continue
        by_task.setdefault(ev.task_id, []).append(ev)
    pairs: list[tuple[StepEvent, StepEvent]] = []
    for tid, evs in by_task.items():
        evs_sorted = sorted(evs, key=lambda e: e.ts)
        dispatched = next(
            (e for e in evs_sorted if e.event_type == _STEP_DISPATCHED_TYPE),
            None,
        )
        if dispatched is None:
            continue
        action = next(
            (e for e in evs_sorted
             if e.ts > dispatched.ts and e.event_type in _STEP_ACTION_TYPES),
            None,
        )
        if action is None:
            continue
        pairs.append((dispatched, action))
    return pairs


def compute_friction_events(
    pairs: list[tuple[StepEvent, StepEvent]],
    *,
    multiplier: float = TIME_TO_ACTION_MULTIPLIER,
    min_pairs_for_baseline: int = TIME_TO_ACTION_MIN_FRICTION,
) -> tuple[list[FrictionEvent], Optional[float]]:
    """Per spec § 2.2: friction iff time-to-action > `multiplier * median`.
    Median is the rolling median over all pairs in the window.

    Returns `(friction_events, median_seconds_or_None)`. When fewer than
    `min_pairs_for_baseline` pairs are present, no median is computable
    and no friction events are emitted (the signal needs a baseline to
    judge what "long" means)."""
    if len(pairs) < min_pairs_for_baseline:
        return [], None
    durations = [(a.ts - d.ts).total_seconds() for d, a in pairs]
    median = statistics.median(durations)
    threshold = multiplier * median
    out: list[FrictionEvent] = []
    for (dispatched, action), dur in zip(pairs, durations):
        if dur > threshold:
            out.append(FrictionEvent(
                task_id=dispatched.task_id,
                time_to_action_sec=dur,
                dispatched_ts=dispatched.ts,
            ))
    return out, median


def signal_time_to_action(
    friction: list[FrictionEvent],
    *,
    threshold: int = TIME_TO_ACTION_THRESHOLD,
    median_sec: Optional[float] = None,
) -> Optional[SignalFinding]:
    """Fires when friction count strictly exceeds threshold (spec § 2.2)."""
    n = len(friction)
    if n <= threshold:
        return None
    median_str = f'{median_sec / 60:.1f}min' if median_sec else 'n/a'
    return SignalFinding(
        signal=SIGNAL_TIME_TO_ACTION,
        headline='Multiple step dispatches stalled past the time-to-action baseline',
        evidence_count=n,
        evidence_summary=(
            f'Evidence: {n} friction events over trailing 7d '
            f'(median time-to-action {median_str})'
        ),
        suggested_fix=(
            'Surface a sequenced operator-action queue or reorder dispatches '
            'so steps that are silently waiting bubble up sooner.'
        ),
        detail={
            'threshold': threshold,
            'median_sec': median_sec,
            'sample_task_ids': [f.task_id for f in friction[:5]],
        },
    )


# -------------------- signal 2.3: alert-ignored --------------------


def load_alerts(
    alerts_path: Path = ALERTS_FILE,
    *,
    now: Optional[datetime] = None,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[AlertEntry]:
    if not alerts_path.exists():
        return []
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    out: list[AlertEntry] = []
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
                ts = _parse_iso_or_log_ts(rec.get('ts', ''))
                if ts is None or ts < cutoff:
                    continue
                subject = rec.get('subject') or ''
                if not isinstance(subject, str) or not subject:
                    continue
                out.append(AlertEntry(
                    ts=ts, subject=subject,
                    source=rec.get('source', '') if isinstance(rec.get('source'), str) else '',
                ))
    except OSError as e:
        log(f'read {alerts_path} failed: {e}', 'WARN')
        return []
    return out


def signal_alert_ignored(
    alerts: list[AlertEntry],
    *,
    threshold: int = ALERT_IGNORED_THRESHOLD,
) -> Optional[SignalFinding]:
    """Per spec § 2.3: same subject `>= N` times in 7d.

    The spec mentions "no commit reference and no operator shortcut in
    the same window" as a gate. There is no canonical structured ledger
    of operator shortcuts joinable to alert subjects today, so v1 fires
    on the raw repeat-count and lets Larry adjudicate on the kanban.
    Check III's threshold tuning (per spec § 8) will revise once the
    join is available."""
    by_subject: dict[str, int] = {}
    for a in alerts:
        by_subject[a.subject] = by_subject.get(a.subject, 0) + 1
    repeats = [(s, c) for s, c in by_subject.items() if c >= threshold]
    if not repeats:
        return None
    repeats.sort(key=lambda sc: -sc[1])
    top_subject, top_count = repeats[0]
    total = sum(c for _, c in repeats)
    return SignalFinding(
        signal=SIGNAL_ALERT_IGNORED,
        headline=f'Alert subject `{top_subject}` ignored repeatedly',
        evidence_count=total,
        evidence_summary=(
            f'Evidence: {top_count} fires of `{top_subject}` over trailing 7d '
            f'({len(repeats)} repeating subject(s) total)'
        ),
        suggested_fix=(
            'Tighten the alert (raise its threshold, reduce its cadence, or '
            'convert to a periodic digest) — or land the operator shortcut '
            'that would resolve it without manual intervention.'
        ),
        detail={'threshold': threshold, 'repeats': repeats},
    )


# -------------------- signal 2.4: rescue-burden --------------------


# Matches outbox-notifier log entries that include the literal token
# `intent=clarification-exhausted` (per spec § 2.4). The notifier emits
# both a "queued completion DM" line and intermediate state lines; we
# count distinct task_ids to avoid double-counting a single rescue.
_CLARIFY_EXHAUSTED_RE = re.compile(
    r'intent=clarification-exhausted.*?task=([^\s\)]+)',
)


def parse_outbox_notifier_log(
    log_path: Path = OUTBOX_NOTIFIER_LOG,
    *,
    now: Optional[datetime] = None,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[ClarificationExhausted]:
    if not log_path.exists():
        return []
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    seen: dict[str, ClarificationExhausted] = {}
    try:
        with open(log_path, errors='replace') as fh:
            for line in fh:
                ts = _extract_line_ts(line)
                if ts is None or ts < cutoff:
                    continue
                m = _CLARIFY_EXHAUSTED_RE.search(line)
                if not m:
                    continue
                task_id = m.group(1).strip()
                if is_fixture_task_id(task_id):
                    continue
                if task_id in seen:
                    continue
                seen[task_id] = ClarificationExhausted(ts=ts, task_id=task_id)
    except OSError as e:
        log(f'read {log_path} failed: {e}', 'WARN')
        return []
    return list(seen.values())


def signal_rescue_burden(
    events: list[ClarificationExhausted],
    *,
    threshold: int = RESCUE_BURDEN_THRESHOLD,
) -> Optional[SignalFinding]:
    """Per spec § 2.4: `>= N` clarification-exhausted events in 7d."""
    n = len(events)
    if n < threshold:
        return None
    return SignalFinding(
        signal=SIGNAL_RESCUE_BURDEN,
        headline='Out-of-chain rescue burden — Forge ran out of clarifications repeatedly',
        evidence_count=n,
        evidence_summary=(
            f'Evidence: {n} clarification-exhausted events over trailing 7d'
        ),
        suggested_fix=(
            'Tighten the spec-template or the dispatch shape upstream so '
            'Forge does not need to ask more than the budget allows.'
        ),
        detail={
            'threshold': threshold,
            'sample_task_ids': [e.task_id for e in events[:5]],
        },
    )


# -------------------- chain_events fetch --------------------


def fetch_step_events_from_supabase(
    client, *, lookback_days: int = LOOKBACK_DAYS,
) -> list[StepEvent]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_iso = cutoff.isoformat()
    out: list[StepEvent] = []
    event_types = [_STEP_DISPATCHED_TYPE] + sorted(_STEP_ACTION_TYPES)
    for event_type in event_types:
        page = 0
        page_size = 1000
        while True:
            res = (
                client.table('chain_events')
                      .select('task_id,event_type,ts')
                      .eq('event_type', event_type)
                      .gte('ts', cutoff_iso)
                      .order('ts')
                      .range(page * page_size, (page + 1) * page_size - 1)
                      .execute()
            )
            rows = getattr(res, 'data', None) or []
            for row in rows:
                tid = row.get('task_id')
                ts = _parse_iso_or_log_ts(row.get('ts', ''))
                if not tid or ts is None:
                    continue
                out.append(StepEvent(
                    task_id=tid, event_type=event_type, ts=ts,
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
            'cannot run Check IX time-to-action signal.'
        )
    from supabase_factory import get_supabase_client  # type: ignore
    return get_supabase_client(url, key)


# -------------------- mission registration --------------------


def _api_base() -> str:
    return os.environ.get('DASHBOARD_API_URL', DEFAULT_API_BASE).rstrip('/')


def _api_token() -> Optional[str]:
    tok = (os.environ.get('DASHBOARD_API_TOKEN') or '').strip()
    return tok or None


def _http_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    data: Optional[bytes] = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        hdrs.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace') if e.fp else ''
        status = e.code
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f'http {method} {url} failed: {e}') from e
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {'raw': raw}
    if not isinstance(payload, dict):
        payload = {'raw': payload}
    return status, payload


def list_existing_drafting_missions(
    *,
    api_base: Optional[str] = None,
    token: Optional[str] = None,
) -> list[dict[str, Any]]:
    """GET /api/system/missions and filter to entries still in
    `phase: drafting`. Used for idempotency (spec § 3)."""
    base = (api_base or _api_base()).rstrip('/')
    tok = token or _api_token()
    if not tok:
        log('DASHBOARD_API_TOKEN missing — cannot dedup missions', 'WARN')
        return []
    status, payload = _http_request(
        'GET', f'{base}/api/system/missions',
        headers={'X-Dashboard-Token': tok},
    )
    if status != 200:
        log(f'GET /api/system/missions returned {status}', 'WARN')
        return []
    missions = payload.get('missions') or []
    if not isinstance(missions, list):
        return []
    return [
        m for m in missions
        if isinstance(m, dict) and m.get('phase') == 'drafting'
    ]


def has_existing_drafting_for_signal(
    missions: list[dict[str, Any]],
    signal: str,
) -> bool:
    prefix = f'pulse-check-ix-{signal}-'
    for m in missions:
        mid = m.get('id') or ''
        if isinstance(mid, str) and mid.startswith(prefix):
            return True
    return False


def post_new_mission(
    finding: SignalFinding,
    cycle_date: date,
    *,
    api_base: Optional[str] = None,
    token: Optional[str] = None,
    repo: str = DEFAULT_REPO,
) -> tuple[int, dict[str, Any]]:
    """POST /api/system/missions/new — kebab-cased name yields
    `pulse-check-ix-<signal>-<YYYY-MM-DD>` per spec § 3."""
    base = (api_base or _api_base()).rstrip('/')
    tok = token or _api_token()
    if not tok:
        raise RuntimeError(
            'DASHBOARD_API_TOKEN missing — cannot register mission'
        )
    body = {
        'name': finding.mission_name(cycle_date),
        'brief': finding.mission_brief(),
        'repo': repo,
        'spec_docs': ['agents/beacon/specs/pulse-check-ix-operator-friction.md'],
    }
    return _http_request(
        'POST', f'{base}/api/system/missions/new',
        headers={'X-Dashboard-Token': tok},
        body=body,
    )


# -------------------- orchestration --------------------


@dataclass
class CycleResult:
    cycle_date: str                       # YYYY-MM-DD
    findings: list[SignalFinding]
    registered: list[dict[str, Any]]      # api responses for new missions
    skipped: list[dict[str, Any]]         # findings deduped via existing drafting
    errors: list[dict[str, Any]]


def iso_week_monday(d: date) -> str:
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def artifact_path_for_week(week_anchor: str) -> Path:
    return PROPOSALS_DIR / f'check-ix-{week_anchor}.json'


def build_artifact(result: CycleResult, *, as_of_iso: str) -> dict[str, Any]:
    return {
        'as_of': as_of_iso,
        'week_anchor': result.cycle_date,
        'findings': [
            {
                'signal': f.signal,
                'headline': f.headline,
                'evidence_count': f.evidence_count,
                'evidence_summary': f.evidence_summary,
                'suggested_fix': f.suggested_fix,
                'detail': f.detail,
            }
            for f in result.findings
        ],
        'registered': result.registered,
        'skipped': result.skipped,
        'errors': result.errors,
    }


def write_artifact(artifact: dict[str, Any]) -> Path:
    # Audit #7 (sibling of pulse_check_viii, missed by #366): this file doubles
    # as the same-week idempotency sentinel (the target_path.exists() gate in
    # main()). A non-atomic write_text left a truncated artifact on a mid-write
    # crash that still satisfied .exists(), permanently suppressing the check
    # for that ISO week. Write atomically so a crash leaves either the intact
    # prior artifact or the intact new one.
    path = artifact_path_for_week(artifact['week_anchor'])
    atomic_write_json(path, artifact, indent=2)
    return path


def _artifact_is_valid_sentinel(path: Path) -> bool:
    """True if ``path`` holds a parseable artifact (a real same-week sentinel).

    A zero-byte or truncated/corrupt file satisfies ``path.exists()`` but is not
    a valid completion marker; treat it as 'not yet run' so the check
    regenerates it instead of being suppressed for the week (audit #7). Mirrors
    pulse_check_viii's gate hardening from #366.
    """
    try:
        obj = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(obj, dict) and bool(obj.get('week_anchor'))


def run_signals(
    *,
    probes: list[StatusProbe],
    step_events: list[StepEvent],
    alerts: list[AlertEntry],
    rescues: list[ClarificationExhausted],
) -> list[SignalFinding]:
    """Pure entry point — run all 4 signal checks against pre-loaded
    inputs. Returns the list of fired findings (may be empty)."""
    findings: list[SignalFinding] = []

    f1 = signal_catch_me_up(probes)
    if f1:
        findings.append(f1)

    pairs = pair_step_events(step_events)
    friction, median_sec = compute_friction_events(pairs)
    f2 = signal_time_to_action(friction, median_sec=median_sec)
    if f2:
        findings.append(f2)

    f3 = signal_alert_ignored(alerts)
    if f3:
        findings.append(f3)

    f4 = signal_rescue_burden(rescues)
    if f4:
        findings.append(f4)

    return findings


def register_findings(
    findings: list[SignalFinding],
    cycle_date: date,
    *,
    existing_missions: Optional[list[dict[str, Any]]] = None,
    dry_run: bool = False,
    api_base: Optional[str] = None,
    token: Optional[str] = None,
    repo: str = DEFAULT_REPO,
) -> CycleResult:
    """Apply idempotency check + POST each new mission."""
    if existing_missions is None and not dry_run:
        existing_missions = list_existing_drafting_missions(
            api_base=api_base, token=token,
        )
    existing_missions = existing_missions or []
    registered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for finding in findings:
        if has_existing_drafting_for_signal(existing_missions, finding.signal):
            skipped.append({
                'signal': finding.signal,
                'reason': 'existing drafting mission for this signal — '
                          'spec § 3 idempotency',
            })
            log(f'skip {finding.signal}: existing drafting mission found')
            continue
        if dry_run:
            registered.append({
                'signal': finding.signal,
                'dry_run': True,
                'name': finding.mission_name(cycle_date),
                'brief': finding.mission_brief(),
            })
            continue
        try:
            status, payload = post_new_mission(
                finding, cycle_date,
                api_base=api_base, token=token, repo=repo,
            )
        except RuntimeError as e:
            errors.append({'signal': finding.signal, 'error': str(e)})
            log(f'register {finding.signal} failed: {e}', 'WARN')
            continue
        if status not in (200, 201):
            errors.append({
                'signal': finding.signal,
                'http_status': status,
                'response': payload,
            })
            log(
                f'register {finding.signal} returned {status}: {payload}',
                'WARN',
            )
            continue
        registered.append({
            'signal': finding.signal,
            'mission_id': payload.get('mission_id'),
            'pr_url': payload.get('pr_url'),
        })
        log(
            f'registered {finding.signal} mission: '
            f'{payload.get("mission_id")} (PR: {payload.get("pr_url")})'
        )

    return CycleResult(
        cycle_date=cycle_date.isoformat(),
        findings=findings,
        registered=registered,
        skipped=skipped,
        errors=errors,
    )


# -------------------- main --------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute findings + print artifact; do not POST '
                             'missions.')
    parser.add_argument('--force', action='store_true',
                        help='Bypass weekly idempotency on the local artifact '
                             '(the missions-API dedup still applies).')
    parser.add_argument('--fixture',
                        help='Read all 4 input streams from a single JSON '
                             'fixture file (test-only path).')
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    week_anchor = iso_week_monday(now.date())
    target_path = artifact_path_for_week(week_anchor)

    if (target_path.exists() and _artifact_is_valid_sentinel(target_path)
            and not args.force and not args.dry_run):
        log(f'Check IX already ran this week ({week_anchor}); '
            'skipping (use --force to re-run).')
        return 0

    if args.fixture:
        with open(args.fixture) as fh:
            raw = json.load(fh)
        probes = [
            StatusProbe(ts=_parse_iso_or_log_ts(p['ts']) or now,
                        text=p.get('text', ''))
            for p in raw.get('probes', [])
        ]
        step_events = [
            StepEvent(
                task_id=e['task_id'],
                event_type=e['event_type'],
                ts=_parse_iso_or_log_ts(e['ts']) or now,
            )
            for e in raw.get('step_events', [])
        ]
        alerts = [
            AlertEntry(
                ts=_parse_iso_or_log_ts(a['ts']) or now,
                subject=a.get('subject', ''),
                source=a.get('source', ''),
            )
            for a in raw.get('alerts', [])
        ]
        rescues = [
            ClarificationExhausted(
                ts=_parse_iso_or_log_ts(r['ts']) or now,
                task_id=r.get('task_id', ''),
            )
            for r in raw.get('rescues', [])
        ]
    else:
        probes = parse_beacon_bot_log(now=now)
        try:
            supa = _connect_supabase()
            step_events = fetch_step_events_from_supabase(supa)
        except (RuntimeError, ImportError) as e:
            log(f'step-event fetch unavailable ({e}); '
                'time-to-action signal skipped', 'WARN')
            step_events = []
        alerts = load_alerts(now=now)
        rescues = parse_outbox_notifier_log(now=now)

    findings = run_signals(
        probes=probes, step_events=step_events,
        alerts=alerts, rescues=rescues,
    )

    if not findings:
        log('Check IX complete: no signals fired this cycle')
        artifact = build_artifact(
            CycleResult(cycle_date=now.date().isoformat(),
                        findings=[], registered=[], skipped=[], errors=[]),
            as_of_iso=now.isoformat(),
        )
        if not args.dry_run:
            write_artifact(artifact)
        else:
            print(json.dumps(artifact, indent=2))
        return 0

    result = register_findings(
        findings, now.date(), dry_run=args.dry_run,
    )
    artifact = build_artifact(result, as_of_iso=now.isoformat())

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    write_artifact(artifact)
    log(
        f'Check IX complete: fired={len(findings)} '
        f'registered={len(result.registered)} '
        f'skipped={len(result.skipped)} errors={len(result.errors)}'
    )
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check
    sys.exit(run_check('ix', main, log_fn=log))
