#!/usr/bin/env python3
"""chain_event_shipper.py — poll-based ingestion daemon for chain_events.

Phase E4.4d PR-B. Spec: agents/beacon/specs/e4-4d-system-tab.md § 5.1, § 5.2.

The daemon tails five sources and INSERTs parsed events into the Supabase
chain_events table (created by PR-A migration 0004). Each insert uses a
deterministic event_id (sha1 of task_id+event_type+ts) so the table's PK
absorbs double-inserts (cursor-replay after restart, push-instrumented
writer in a future PR — same dedup property regardless of writer).

Five sources, each with its own cursor for resume-after-restart:

  1. journalctl -fu ourliberty-inbox-watcher.service --output=json
     (session_start, session_done events)
  2. ~/agents/logs/outbox-notifier.log
     (marker_emit, auto_merge, marker_error, cost_budget, review_request,
      build_dispatched, preflight_*, healer_fire)
  3. ~/agents/blackboard/pulse-escalations.json (snapshot, overwritten)
     (event_type='escalation')
  4. ~/agents/blackboard/larry-alerts.jsonl (append-only)
     (event_type='larry_alert' or 'sentinel_alert' depending on source field)
  5. ~/agents/blackboard/sentinel-alerts.jsonl (append-only)
     (event_type='sentinel_alert')

Cursor shapes:
  - journalctl: --cursor-file at AGENTS_ROOT/state/chain-event-cursor.journal
  - log/jsonl files: (inode, byte_offset) tuple per file
  - pulse-escalations.json: (file_mtime, content_sha256)

Buffer: on Supabase write failure, events spill to
AGENTS_ROOT/state/chain-event-buffer.jsonl (cap 10,000 lines / ~5 MB).
On reconnect, buffer drains FIFO. If buffer fills under chronic outage,
oldest events drop with a WARN log entry (audit healer DMs Larry).

Health: writes a heartbeat to AGENTS_ROOT/blackboard/chain-event-shipper.heartbeat
every 30 seconds. heal_chain_event_shipper_heartbeat.py reads mtime and DMs
Larry if stale > 10 min.

Operator interface:
  - default: run as a daemon, loops until SIGTERM
  - --once: single drain pass then exit (for tests / debugging)
  - --no-backfill: skip backfill on first run (default behavior; flag is a
    no-op kept for explicitness)
  - OURLIBERTY_CHAIN_SHIPPER_ENABLED=false → exit early (kill-switch)
  - ~/agents/healers.disabled → exit early (blanket kill-switch)
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402  (shared durable atomic write, PR-E #366)
import event_briefing  # noqa: E402  (#5 author-at-emit alert meaning layer)
import file_lock  # noqa: E402  (shared advisory flock, PR-E2 #16)
from log_ts import parse_log_ts  # noqa: E402  (shared log-ts parser)

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
LOG_FILE = AGENTS_ROOT / 'logs' / 'chain-event-shipper.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'chain-event-shipper.heartbeat'
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'

JOURNAL_CURSOR_FILE = AGENTS_ROOT / 'state' / 'chain-event-cursor.journal'
LOG_CURSORS_FILE = AGENTS_ROOT / 'state' / 'chain-event-cursors.json'
PULSE_CURSOR_FILE = AGENTS_ROOT / 'state' / 'chain-event-cursor-pulse.json'
BUFFER_FILE = AGENTS_ROOT / 'state' / 'chain-event-buffer.jsonl'

OUTBOX_NOTIFIER_LOG = AGENTS_ROOT / 'logs' / 'outbox-notifier.log'
PULSE_ESCALATIONS_JSON = AGENTS_ROOT / 'blackboard' / 'pulse-escalations.json'
LARRY_ALERTS_JSONL = AGENTS_ROOT / 'blackboard' / 'larry-alerts.jsonl'
SENTINEL_ALERTS_JSONL = AGENTS_ROOT / 'blackboard' / 'sentinel-alerts.jsonl'

HEARTBEAT_INTERVAL_SEC = 30
DRAIN_INTERVAL_SEC = 30
PULSE_POLL_INTERVAL_SEC = 30
BUFFER_MAX_LINES = 10_000
BUFFER_MAX_BYTES = 5 * 1024 * 1024
SUPABASE_TIMEOUT_SEC = 15
JOURNALCTL_UNIT = 'ourliberty-inbox-watcher.service'

# Per spec § 5.1: event_type validation is application-side. Any value not in
# this set rejected with a WARN log + the audit healer (weekly) DMs Larry.
KNOWN_EVENT_TYPES: frozenset[str] = frozenset({
    'session_start',
    'session_done',
    'marker_emit',
    'auto_merge',
    'marker_error',
    'cost_budget',
    'review_request',
    'build_dispatched',
    'preflight_proceed',
    'preflight_clarify',
    'preflight_reject',
    'escalation',
    'larry_alert',
    'sentinel_alert',
    'healer_fire',
    # E4.4e PR-A: push-instrumented writers emit these directly to Supabase
    # via scripts/chain_event_emit.py. Listing them here keeps the audit
    # healer (heal_chain_event_type_audit.py) from flagging them as unknown
    # types when they land. The shipper itself never produces these rows.
    # `larry_action` is added now for PR-B forward-compat: PR-B's dashboard
    # POST endpoint writes those rows, but the audit gate needs to admit
    # them the moment PR-B ships.
    'approval_request',
    'clarify_request',
    'clarify_response',
    'larry_action',
    # check-x-verdict-emission: Mirror review verdicts, push-emitted by
    # outbox_notifier at the verdict-classification site (dedicated types
    # rather than reusing `auto_merge`/`escalation` so a PASS is recorded at
    # the verdict moment, not at actual merge — a PASS can sit in the auto-
    # merge queue behind a blocker). The shipper never produces these rows;
    # the audit healer (heal_chain_event_type_audit.py) reads this same
    # constant, so listing them here also admits them to the weekly audit.
    'review_pass',
    'review_revision',
    'review_escalate',
    # skip-mirror-review / out-of-band-merge reconcile: emitted by
    # heal_stale_in_review_reconcile.py to close a phantom in_review card
    # whose PR merged/closed out of band with no verdict recorded. Push-only
    # (the shipper never produces it); listed here so emit_event admits it and
    # the audit healer does not flag it as unknown.
    'review_obsolete',
    # mirror-two-slot-review §4 PR3: inbox_watcher.emit_review_queue_wait
    # push-emits one row per Mirror review at review-start, carrying the
    # PR-open → review-start queue_wait_sec and the review_slot. Feeds the
    # burst-latency success metric (§8) and the sibling gauge's "need slot 3?"
    # decision. Push-only (the shipper never produces it); listed here so
    # emit_event admits it and the weekly audit healer does not flag it.
    'review_queue_wait',
    # N4 promotion rule (approvals-queue-rework.md L6): scripts/promote_alerts.py
    # push-emits one of these for each escalation that crosses the needs-CEO-
    # attention bar. The dashboard's NeedsAttentionCard on /live queries this
    # type. The shipper itself never produces these rows; listing the type here
    # also admits it to the weekly chain-event-type audit (heal_chain_event_type_audit).
    'needs_attention',
    # N6 (approvals-queue-rework spec): the daily/weekly CEO digest generator
    # (scripts/ceo_digest_generator.py) push-emits one row per run. payload
    # carries period ('daily'|'weekly'), window bounds, the CEO-voice summary,
    # and a structured raw fallback the dashboard card renders if the voice is
    # absent. The shipper never produces these rows; listing the type here
    # admits it to the weekly audit (heal_chain_event_type_audit.py).
    'ceo_digest',
    # Missions v2 Phase 0 (missions-v2-phase0-desktop-session-feed.md): desktop
    # Claude Code sessions push these directly to Supabase via the droplet
    # ingest endpoint (POST /api/ingest/desktop-session), which calls
    # chain_event_emit.emit_event with agent='desktop-claude'. They make a live
    # desktop chat appear as a card on the Missions board (the board was
    # previously blind to desktop work). _start opens a card, _done retires it,
    # _active is an optional activity/blocked heartbeat. The shipper never
    # produces these rows; listing them here admits them to the weekly audit
    # (heal_chain_event_type_audit.py).
    'desktop_session_start',
    'desktop_session_active',
    'desktop_session_done',
    # Missions v2 Phase 4 step 1b (missions-v2-phase4-meaning-layer.md § 8):
    # the capture-scoped conversation thread. The dashboard's
    # POST /api/missions/captures/{id}/message push-emits one of these per
    # operator message (direction='larry_to_team', agent=actor email), and
    # Beacon push-emits the same type on her reply (direction='team_to_larry',
    # agent='beacon'). task_id is the capture_id so a thread is one query.
    # GET /api/missions/captures/{id}/thread reads these rows back. The
    # shipper never produces these rows; listing the type here admits it to
    # the weekly chain-event-type audit (heal_chain_event_type_audit.py).
    'card_message',
    # projects-v3 P4 Contract A (p4-complete-signal): when a build-sequence's
    # final step reaches verified-merged, outbox_notifier push-emits ONE of
    # these (agent='build_sequence_advancer', task_id=seq_id) alongside the
    # plain-language completion DM to Larry. Exactly-once: guarded by a
    # `sequence-complete-signaled` audit_log marker on the sequence file, so a
    # re-tick / notifier crash-resume never double-emits. The shipper never
    # produces these rows; listing the type here admits it to the weekly audit.
    'sequence_complete',
    # autonomy-visibility (2026-06-21): the durable record of ONE trust-policy
    # decision, push-emitted at each decision site (beacon chat path, pulse-
    # auto-dispatch route, replan route — board-delegate route lands in slice 2)
    # via
    # beacon_approval_handler.build_autonomy_decision_chain_event. payload carries
    # decision (auto_approve|force_ask|reject), dispatched (fired without Larry?),
    # source, target_agent/repo, task_type, matched_rule, summary. This is the
    # audit primitive behind BOTH the Automated Work feed (decision=auto_approve)
    # and the needs-Larry view (decision=force_ask). The shipper never produces
    # these rows; listing the type here admits it to the weekly chain-event-type
    # audit (heal_chain_event_type_audit.py).
    'autonomy_decision',
    # approval-sync Phase 3a (approval-sync-phase3-spec.md §3a.1): the two
    # "needs-you" stragglers that were projected into chain_events so the
    # dashboard could read ONE substrate.
    #
    # `parked_capture` — RETIRED 2026-07-01. The dashboard dropped the Parked
    # lane from the Needs-You surface (ourliberty-dashboard#101; the parked
    # backlog lives only on the Missions tab), so nothing consumes these rows
    # and heal_missions_card_gc no longer projects them. RETAINED in this
    # whitelist deliberately: residual rows already in chain_events (with a ts
    # inside the audit's 7-day lookback) plus the ones cleared once by
    # scripts/retire_parked_capture_rows.py must NOT trip the weekly
    # chain-event-type audit (heal_chain_event_type_audit.py) as an "unknown
    # type" false alarm. Safe to drop from this set once no parked_capture row's
    # ts falls within the audit lookback window.
    'parked_capture',
    # `sequence_needs_you` is push-emitted by build_sequence_advancer when a
    # sequence is paused or a step is stuck (agent='build_sequence_advancer',
    # task_id=`seq-<id>`, lane='steer'), and cleared when it resumes/clears.
    # Event-driven (emitted at the state transition, not on a poll tick). The
    # shipper never produces these rows; listing the type here admits it to the
    # weekly chain-event-type audit (heal_chain_event_type_audit.py).
    'sequence_needs_you',
    # spec-gauntlet (agents/beacon/specs/spec-gauntlet-gate.md §3.5): the
    # antagonistic spec-review gate push-emits ONE of these per gauntlet round
    # (agent='spec-review-runner', task_id=<spec task_id>) carrying
    # {round, blocking_count, advisory_count, resolved_count, lens_verdicts,
    # duration_s}, with per-round id_extra dedup. Registered here in the
    # foundations slice BEFORE the runner ships: emit_event silently drops any
    # unregistered type, which would void the gate's entire visibility surface
    # (§3.5) — so the type must be admitted the moment the runner starts
    # emitting. The shipper never produces these rows; listing the type here
    # also admits it to the weekly chain-event-type audit
    # (heal_chain_event_type_audit.py).
    'spec_review_round',
    # spec-gauntlet silent-failure gauge (spec-gauntlet-gate.md §3.5): the
    # trailing gauge (scripts/spec_review_silent_failure_gauge.py) push-emits ONE
    # of these (agent='spec-review', task_id=<tail gauntlet task_id>) when the
    # trailing run of gauntlets ending errored/incomplete crosses MIN_STREAK —
    # the fail-open blind spot where a persistently broken gate degrades into an
    # invisible permanent no-op. Info surface ONLY (no DM, no needs_attention),
    # per the alert default-deny north star; this chain_event IS the surface, so
    # the type must be admitted or emit_event would silently drop it and void the
    # gauge. The shipper never produces these rows; listing the type here also
    # admits it to the weekly chain-event-type audit (heal_chain_event_type_audit.py).
    'spec_review_silent_failure',
})

# PII / credential redaction. Any payload field key matching one of these
# patterns (case-insensitive substring) gets its value replaced with the
# string '<redacted>' before INSERT. Cheap line of defense — the upstream
# log writers SHOULD never embed credentials in markers, but defense in
# depth at the ingestion layer means a single source-side slip doesn't
# leak to Supabase + the public dashboard.
_REDACT_KEY_SUBSTRS = (
    'token', 'secret', 'password', 'passwd', 'api_key', 'apikey',
    'service_role', 'authorization', 'auth_header', 'bearer',
    'private_key', 'session_id',
)


# -------------------- logging + heartbeat --------------------

def _setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger('chain_event_shipper')
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S%z',
    )
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


# -------------------- dedup hash + payload sanitization --------------------

def compute_event_id(
    task_id: Optional[str], event_type: str, ts: str,
    extra: Optional[str] = None,
) -> str:
    """Deterministic sha1 of (task_id|<none>, event_type, ts[, extra]).

    Per spec § 5.1: any writer (poller now, push-instrumented in a future
    PR) computes the same event_id for the same logical event. The PK
    absorbs double-inserts via ON CONFLICT DO NOTHING.

    ``extra`` is an optional disambiguator appended only when truthy, so
    existing callers (which omit it) hash exactly as before. Audit #58: the
    dashboard's larry_action audit row keys on (task_id, 'larry_action', ts)
    only, so two distinct actions on different source events sharing a task_id
    that land in the same microsecond collide and one audit row is silently
    dropped by ignore_duplicates. Passing the source_event_id as ``extra``
    makes each action's audit id unique.
    """
    raw = f'{task_id or ""}|{event_type}|{ts}'
    if extra:
        raw += f'|{extra}'
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def sanitize_payload(payload: Any) -> Any:
    """Walk payload and redact values for keys matching credential patterns.

    Recursive on dicts + lists. Leaves scalars alone. Never raises.
    """
    if isinstance(payload, dict):
        out = {}
        for key, val in payload.items():
            kl = str(key).lower()
            if any(needle in kl for needle in _REDACT_KEY_SUBSTRS):
                out[key] = '<redacted>'
            else:
                out[key] = sanitize_payload(val)
        return out
    if isinstance(payload, list):
        return [sanitize_payload(v) for v in payload]
    return payload


# -------------------- cursor persistence --------------------

_FINGERPRINT_BYTES = 64


@dataclass
class FileCursor:
    """Per-file cursor for log/jsonl tail.

    Tuple of (inode, byte_offset, first_bytes_sha1). The first-bytes
    fingerprint catches the rotation-with-reused-inode-and-equal-size
    case that inode+offset alone can miss (logrotate's create-and-then-
    write pattern, manual delete+recreate, etc.).
    """
    inode: int = 0
    offset: int = 0
    fp_sha: str = ''

    def to_dict(self) -> dict[str, Any]:
        return {'inode': self.inode, 'offset': self.offset,
                'fp_sha': self.fp_sha}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'FileCursor':
        return cls(
            inode=int(d.get('inode', 0)),
            offset=int(d.get('offset', 0)),
            fp_sha=str(d.get('fp_sha', '')),
        )


def _file_fingerprint(path: Path) -> str:
    """Hex sha1 of the first _FINGERPRINT_BYTES of the file.

    Returns '' if the file is smaller than _FINGERPRINT_BYTES — the
    fingerprint must be computed on a stable prefix, and a partial read
    of a file that's still being written would change on the next
    append. For files smaller than the fingerprint threshold, the
    rotation check falls back to inode+offset+size-shrinkage only.
    """
    try:
        st = path.stat()
        if st.st_size < _FINGERPRINT_BYTES:
            return ''
        with open(path, 'rb') as fh:
            head = fh.read(_FINGERPRINT_BYTES)
    except OSError:
        return ''
    if len(head) < _FINGERPRINT_BYTES:
        return ''
    return hashlib.sha1(head).hexdigest()


@dataclass
class PulseCursor:
    """Cursor for pulse-escalations.json snapshot (overwritten on each Pulse cycle)."""
    mtime: float = 0.0
    sha256: str = ''

    def to_dict(self) -> dict[str, Any]:
        return {'mtime': self.mtime, 'sha256': self.sha256}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'PulseCursor':
        return cls(mtime=float(d.get('mtime', 0.0)), sha256=str(d.get('sha256', '')))


def load_log_cursors() -> dict[str, FileCursor]:
    if not LOG_CURSORS_FILE.exists():
        return {}
    try:
        data = json.loads(LOG_CURSORS_FILE.read_text())
        return {k: FileCursor.from_dict(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_log_cursors_locked(cursors: dict[str, FileCursor]) -> None:
    """Atomically persist `cursors`. Caller MUST already hold the cursors lock."""
    atomic_io.atomic_write_json(
        LOG_CURSORS_FILE,
        {k: c.to_dict() for k, c in cursors.items()},
    )


def save_log_cursors(cursors: dict[str, FileCursor]) -> None:
    """Durably persist the log cursors under the shared advisory lock (audit M3).

    Two hardenings over the old plain write_text:
      * Atomic write (PR-E #366 helper): a mid-write crash (OOM/SIGTERM) can no
        longer leave a truncated file. A torn file would parse-fail in
        load_log_cursors, which swallows the error and returns {} — silently
        resetting every cursor (outbox_log/larry_alerts/sentinel_alerts) to 0
        and triggering a full re-read + mass re-upsert storm.
      * Shared flock (PR-E2 #16 helper) on a dedicated sidecar serialises this
        with larry_alerts_retention._refresh_shipper_cursor — the only other
        writer of this file — so its read-modify-write can't clobber an offset
        this writer just advanced (the non-torn lost-update variant).
    """
    try:
        with file_lock.exclusive_lock(
            file_lock.sidecar_lock_path(LOG_CURSORS_FILE)
        ):
            _write_log_cursors_locked(cursors)
    except OSError:
        pass


@contextlib.contextmanager
def log_cursors_transaction() -> Iterator[dict[str, FileCursor]]:
    """Hold the shared cursors lock across a full read-modify-write.

    Yields the currently-persisted cursors; the caller mutates the mapping in
    place and it is written atomically when the block exits normally. Use this
    (not load_log_cursors + save_log_cursors) when mutating a SUBSET of keys, so
    a concurrent writer can't advance another key between the load and the save
    and have that advance clobbered by the stale snapshot (audit M3)."""
    with file_lock.exclusive_lock(file_lock.sidecar_lock_path(LOG_CURSORS_FILE)):
        cursors = load_log_cursors()
        yield cursors
        _write_log_cursors_locked(cursors)


def load_pulse_cursor() -> PulseCursor:
    if not PULSE_CURSOR_FILE.exists():
        return PulseCursor()
    try:
        return PulseCursor.from_dict(json.loads(PULSE_CURSOR_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return PulseCursor()


def save_pulse_cursor(cursor: PulseCursor) -> None:
    """Durably persist the pulse cursor (unique temp + fsync + os.replace).

    Single-writer crash-atomicity sibling of save_log_cursors (audit M3): only
    the shipper writes this file, so there is no lost-update/lock concern — but a
    plain write_text could still leave a truncated file on a mid-write crash
    (OOM/SIGTERM). load_pulse_cursor swallows the resulting JSONDecodeError and
    returns a default PulseCursor() (mtime=0), re-reading the pulse file from the
    start. The atomic write removes that torn-file window; no flock needed."""
    try:
        atomic_io.atomic_write_json(PULSE_CURSOR_FILE, cursor.to_dict())
    except OSError:
        pass


def load_journal_cursor(cursor_file: Path = JOURNAL_CURSOR_FILE) -> str:
    """Read the persisted journald cursor string, or '' if none yet.

    PR-E2 #18: we manage this cursor ourselves (instead of letting journalctl's
    --cursor-file advance it as it streams) so it only ever points at a record we
    actually consumed. '' means "no cursor yet" → drain from the start of the
    unit's journal (matching the old first-run --cursor-file behaviour)."""
    try:
        return cursor_file.read_text().strip()
    except OSError:
        return ''


def save_journal_cursor(cursor: str, cursor_file: Path = JOURNAL_CURSOR_FILE) -> None:
    """Durably persist the journald cursor (unique temp + fsync + os.replace).

    Saved only AFTER a drain consumes records, mirroring save_log_cursors: a
    crash before this leaves the old cursor, so the next drain re-reads the
    un-persisted records and the event_id PK dedup absorbs the duplicates — the
    failure mode is re-read, never skip."""
    if not cursor:
        return
    try:
        atomic_io.atomic_write_text(cursor_file, cursor)
    except OSError:
        pass


# -------------------- canonical event record --------------------

@dataclass
class ChainEvent:
    """In-memory shape of one row destined for chain_events."""
    event_id: str
    ts: str
    agent: str
    task_id: Optional[str]
    event_type: str
    pr_url: Optional[str] = None
    cost_usd: Optional[float] = None
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ''  # which input source produced this (journal/log/pulse/larry/sentinel)

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            'event_id': self.event_id,
            'ts': self.ts,
            'agent': self.agent,
            'event_type': self.event_type,
            'payload': sanitize_payload(self.payload),
        }
        if self.task_id:
            row['task_id'] = self.task_id
        if self.pr_url:
            row['pr_url'] = self.pr_url
        if self.cost_usd is not None:
            row['cost_usd'] = self.cost_usd
        return row


def make_event(
    *, agent: str, event_type: str, ts: str,
    task_id: Optional[str] = None,
    pr_url: Optional[str] = None,
    cost_usd: Optional[float] = None,
    payload: Optional[dict[str, Any]] = None,
    source: str = '',
    id_extra: Optional[str] = None,
) -> Optional[ChainEvent]:
    """Build a ChainEvent, validating event_type against KNOWN_EVENT_TYPES.

    Returns None if event_type is unknown — caller logs WARN; the row is
    never sent to Supabase. The weekly audit healer separately catches any
    unknown types that DO land (hot-patched code, drift, etc.).

    ``id_extra`` feeds compute_event_id's disambiguator: log timestamps have
    1-second resolution, so without it two distinct same-task lines in the
    same second (AUTO_MERGE outcome=failed + retry outcome=merged) collide
    on the PK and ON CONFLICT DO NOTHING silently drops the second — the
    advancer's merge gate would then never see the merged row. Callers with
    higher-resolution timestamps omit it and hash exactly as before.
    """
    if event_type not in KNOWN_EVENT_TYPES:
        return None
    event_id = compute_event_id(task_id, event_type, ts, extra=id_extra)
    return ChainEvent(
        event_id=event_id, ts=ts, agent=agent, task_id=task_id,
        event_type=event_type, pr_url=pr_url, cost_usd=cost_usd,
        payload=payload or {}, source=source,
    )


# -------------------- source parsers --------------------

# journalctl --output=json emits one JSON object per line. We care about
# session start/done events written by inbox_watcher. The expected shape
# from inbox_watcher's structured logging:
#
#   { "_SOURCE_REALTIME_TIMESTAMP": "...",
#     "MESSAGE": "inbox_watcher: [forge] start task=... model=...",
#     "OURLIBERTY_EVENT_TYPE": "session_start",
#     "OURLIBERTY_AGENT": "forge",
#     "OURLIBERTY_TASK_ID": "...",
#     "OURLIBERTY_MODEL": "claude-opus-4-7",
#     "OURLIBERTY_TASK_TYPE": "feature-development"
#   }
#
# Real-world inbox_watcher today doesn't emit those OURLIBERTY_* fields
# yet — the parser falls back to regex on MESSAGE for compatibility. The
# actual MESSAGE today looks like:
#
#   inbox_watcher: [forge] start task=<id> model=<m> timeout=<n>s resume=<sid>
#   inbox_watcher: [beacon] done task=<id> success=True duration=<N>s
#                  attempts=<n> cost=$<N>
#
# The regex below extracts agent, verb, task_id, and the optional fields
# we care about. Trailing fields we don't capture (timeout=, resume=,
# attempts=) are tolerated by not anchoring the tail.

_JOURNAL_SESSION_RE = re.compile(
    r'inbox_watcher:\s+'
    r'\[(?P<agent>[^\]]+)\]\s+'
    r'(?P<verb>start|done)\s+'
    r'task=(?P<task_id>\S+)'
    r'(?P<tail>.*)$'
)

# Recognized kv tokens on the tail after `task=<id>`. Tokens we don't list
# (timeout=, resume=, attempts=) are silently tolerated.
_JOURNAL_TAIL_KV_RE = re.compile(
    r'\b(?P<key>model|success|duration|cost)='
    r'(?P<val>\$?[^\s]+)'
)


def parse_journal_record(record: dict[str, Any]) -> Optional[ChainEvent]:
    """Parse one journalctl JSON record. Returns None if not a chain event."""
    message = record.get('MESSAGE') or ''
    if isinstance(message, list):
        # journalctl wraps binary messages as a list of byte ints
        try:
            message = bytes(message).decode('utf-8', errors='replace')
        except Exception:
            return None
    ts = _journal_ts(record)
    structured_type = record.get('OURLIBERTY_EVENT_TYPE')
    if structured_type:
        agent = record.get('OURLIBERTY_AGENT') or 'watcher'
        task_id = record.get('OURLIBERTY_TASK_ID')
        payload = {
            'model': record.get('OURLIBERTY_MODEL'),
            'task_type': record.get('OURLIBERTY_TASK_TYPE'),
            'message': message,
        }
        cost = record.get('OURLIBERTY_COST_USD')
        return make_event(
            agent=agent, event_type=structured_type, ts=ts, task_id=task_id,
            cost_usd=float(cost) if cost else None,
            payload={k: v for k, v in payload.items() if v is not None},
            source='journal',
        )
    m = _JOURNAL_SESSION_RE.search(message)
    if not m:
        return None
    verb = m.group('verb')
    event_type = 'session_start' if verb == 'start' else 'session_done'
    agent = m.group('agent') or 'unknown'
    task_id = m.group('task_id')
    tail_kv = {kv.group('key'): kv.group('val')
               for kv in _JOURNAL_TAIL_KV_RE.finditer(m.group('tail') or '')}
    duration_raw = tail_kv.get('duration')
    if duration_raw and duration_raw.endswith('s'):
        duration_raw = duration_raw[:-1]
    cost_raw = tail_kv.get('cost')
    if cost_raw and cost_raw.startswith('$'):
        cost_raw = cost_raw[1:]
    payload = {
        'model': tail_kv.get('model'),
        'success': tail_kv.get('success'),
        'duration_sec': _maybe_float(duration_raw),
        'message': message,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    cost = _maybe_float(cost_raw)
    return make_event(
        agent=agent, event_type=event_type, ts=ts, task_id=task_id,
        cost_usd=cost, payload=payload, source='journal',
    )


def _journal_ts(record: dict[str, Any]) -> str:
    """Extract an ISO8601 ts from a journal record."""
    # journalctl's __REALTIME_TIMESTAMP is microseconds since epoch (as string)
    raw = (record.get('__REALTIME_TIMESTAMP') or
           record.get('_SOURCE_REALTIME_TIMESTAMP'))
    if raw:
        try:
            dt = datetime.fromtimestamp(int(raw) / 1_000_000, tz=timezone.utc)
            return dt.isoformat()
        except (ValueError, OSError):
            pass
    return datetime.now(timezone.utc).isoformat()


def _maybe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# outbox-notifier.log lines are written by outbox_notifier.log() — REAL shape
# (confirmed against the production log 2026-06-11):
#   [2026-06-11 00:00:23] [notifier] [INFO] AUTO_MERGE task=<id> pr=<url> outcome=merged (--squash --delete-branch) agent=forge
#   [2026-06-10 17:12:01] [notifier] [WARN] COST_BUDGET_EXHAUSTED task=<id> current=$5.12 cap=$5.00 dispatch=<label>; refusing dispatch agent=forge
# Two traps, both of which kept this source at zero shipped events from its
# birth until 2026-06-11:
#   - a `[notifier]` tag sits between the timestamp and the level. The tag
#     group below is OPTIONAL so the older `[ts] [LEVEL] KEYWORD ...` shape
#     (drain-test fixtures, possible future writers) still parses.
#   - the timestamp is NAIVE HOST-LOCAL time (`datetime.now()`), not UTC —
#     see _normalize_iso_ts.

_LOG_LINE_RE = re.compile(
    r'^\[(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\]]*)\]\s+'
    r'(?:\[(?P<tag>[\w-]+)\]\s+)?'
    r'\[(?P<level>\w+)\]\s+(?P<rest>.*)$'
)
_KV_RE = re.compile(r"(\w+)=('([^']*)'|\"([^\"]*)\"|(\S+))")

_LOG_EVENT_KEYWORDS = {
    'MARKER_EMIT': 'marker_emit',
    'AUTO_MERGE': 'auto_merge',
    'MARKER_ERROR': 'marker_error',
    # Deliberately NOT plain 'COST_BUDGET': those lines are per-dispatch
    # "(allowed)" trajectory logging, one per healthy dispatch. Shipping them
    # as cost_budget rows would make every healthy task look terminally
    # failed to dashboard_api._derive_done_today (cost_budget is in its
    # _DONE_FAILURE_EVENT_TYPES). Only the cap-fire sentinel is a chain event.
    'COST_BUDGET_EXHAUSTED': 'cost_budget',
    # NO 'REVIEW_REQUEST' keyword — deliberately absent (forge-queue-in-review-
    # lane): review_request is push-emitted by
    # outbox_notifier._emit_review_request_chain_event with agent='forge' at the
    # dispatch sites, and that push payload carries `origin_task_id`, the
    # load-bearing join key delegate-tracking Slice 2a joins review_request rows
    # by. A log-parsed copy would double-write the event — push ts != log ts, so
    # the deterministic event_id can't dedup the pair — with agent='notifier'
    # and NO origin_task_id, leaving a dashboard reader that joins on
    # origin_task_id in an inconsistent state. Never re-add this mapping.
    'BUILD_DISPATCHED': 'build_dispatched',
    'HEALER_FIRE': 'healer_fire',
    'PREFLIGHT_PROCEED': 'preflight_proceed',
    'PREFLIGHT_CLARIFY': 'preflight_clarify',
    'PREFLIGHT_REJECT': 'preflight_reject',
}


def _parse_kv_pairs(rest: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _KV_RE.finditer(rest):
        key = m.group(1)
        val = m.group(3) or m.group(4) or m.group(5) or ''
        out[key] = val
    return out


def parse_log_line(line: str) -> Optional[ChainEvent]:
    """Parse one outbox-notifier.log line. Returns None if not a chain event."""
    m = _LOG_LINE_RE.match(line.rstrip('\n'))
    if not m:
        return None
    rest = m.group('rest')
    keyword = None
    for kw in _LOG_EVENT_KEYWORDS:
        # Word-boundary match, not bare startswith: the log also carries
        # non-event lookalikes sharing these prefixes (AUTO_MERGE_HELD,
        # AUTO_MERGE_WORKTREE_TEARDOWN, AUTO_MERGE_QUEUE_*, COST_BUDGET,
        # COST_BUDGET_DM_WRITE_FAILED, ...) that must not ship.
        if rest == kw or rest.startswith(kw + ' '):
            keyword = kw
            break
    if not keyword:
        return None
    event_type = _LOG_EVENT_KEYWORDS[keyword]
    kv = _parse_kv_pairs(rest[len(keyword):])
    ts = _normalize_iso_ts(m.group('ts'))
    task_id = kv.get('task') or kv.get('task_id')
    agent = kv.get('agent') or 'notifier'
    pr_url = kv.get('pr')
    cost = _maybe_float(kv.get('cost_usd'))
    if cost is None:
        # COST_BUDGET_EXHAUSTED carries `current=$1.23` rather than cost_usd.
        cost = _maybe_float(kv.get('current', '').lstrip('$'))
    payload = {k: v for k, v in kv.items()
               if k not in ('task', 'task_id', 'agent', 'pr', 'cost_usd')}
    payload['raw_keyword'] = keyword
    # id_extra=rest: re-reading the same line after a cursor rewind still
    # dedups (identical rest → identical id), while two different lines for
    # the same task in the same second (1s log resolution) stay distinct.
    return make_event(
        agent=agent, event_type=event_type, ts=ts, task_id=task_id,
        pr_url=pr_url, cost_usd=cost, payload=payload, source='outbox_log',
        id_extra=rest,
    )


def _normalize_iso_ts(raw: str) -> str:
    """Normalize a log-line timestamp to an ISO8601 UTC string.

    Delegates the naive-host-local→UTC parse to the shared
    log_ts.parse_log_ts (see its module docstring for the 6h-skew history that
    made build_sequence_advancer.chain_event_says_merged drop fresh merges).
    On unparseable input we fall back to now() so a malformed line still ships
    with a sane ordering ts rather than vanishing.
    """
    dt = parse_log_ts(raw)
    if dt is None:
        return datetime.now(timezone.utc).isoformat()
    return dt.isoformat()


# pulse-escalations.json is a snapshot rewritten by Pulse each cycle. We
# don't tail it; we compare content_sha256 against the last cursor and if
# different, walk the array and emit one 'escalation' event per entry
# whose synthetic event_id has not been seen (dedup via PK).
def parse_pulse_escalations(content: str) -> list[ChainEvent]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    entries = data.get('escalations') if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    out: list[ChainEvent] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ts = entry.get('ts') or entry.get('timestamp') or \
            datetime.now(timezone.utc).isoformat()
        task_id = entry.get('task_id') or entry.get('headline') or entry.get('id')
        payload = {
            'severity': entry.get('severity'),
            'headline': entry.get('headline'),
            'needs_response': entry.get('needs_response'),
            'detail': entry.get('detail'),
            'source_finding': entry.get('source') or 'pulse',
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        ev = make_event(
            agent='pulse', event_type='escalation', ts=ts,
            task_id=task_id, payload=payload, source='pulse_escalations',
        )
        if ev:
            out.append(ev)
    return out


def alert_event_task_id(rec: dict) -> Optional[str]:
    """The task_id an alert/sentinel jsonl record is shipped under.

    THE single source of truth for the larry-alerts/sentinel-alerts keying:
    the explicit `task_id`, else the `subject` (or `intent`), else None. Shared
    by `parse_jsonl_line` (the poll shipper that STAMPS the row) and
    `larry_alerts._retract_shipped_alert_events` (which must CLEAR that same row
    by the identical key). Keeping the derivation in one place is load-bearing:
    if the two ever diverged, the retraction would clear a key the shipper never
    used, the read_at UPDATE would hit 0 rows, and auto-resolved alerts would
    render live on the dashboard forever — the exact §3a.2 bug the clear closes.
    """
    if not isinstance(rec, dict):
        return None
    subject = rec.get('subject') or rec.get('intent') or ''
    return rec.get('task_id') or subject or None


def parse_jsonl_line(line: str, *, source: str) -> Optional[ChainEvent]:
    """Parse one larry-alerts.jsonl or sentinel-alerts.jsonl entry."""
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(rec, dict):
        return None
    ts = rec.get('ts') or datetime.now(timezone.utc).isoformat()
    if source == 'larry_alerts':
        event_type = 'larry_alert'
        agent = rec.get('source') or 'beacon'
    elif source == 'sentinel_alerts':
        event_type = 'sentinel_alert'
        agent = rec.get('source') or 'sentinel'
    else:
        return None
    task_id = alert_event_task_id(rec)
    payload = {k: v for k, v in rec.items()
               if k not in ('ts', 'source', 'task_id')}
    # Author-at-emit (#5): bake the deterministic plain-language meaning layer
    # (briefing/risk/risk_note) into the alert payload so the dashboard's
    # Operations/Alerts panel renders it straight from the row — chain_events
    # payloads are immutable after insert, so the briefing must be present at
    # emit. Best-effort: alert_briefing returns None (and never raises) when
    # there is no translation to brief, leaving the raw-headline fallback intact.
    try:
        brief = event_briefing.alert_briefing(rec, event_type)
    except Exception:  # noqa: BLE001 — defense in depth: never let the meaning
        brief = None   # layer break the daemon's ingest path (alert_briefing is
        #              # itself fail-safe; this guards future edits to it too).
    if brief:
        payload.update(brief)
    return make_event(
        agent=agent, event_type=event_type, ts=ts, task_id=task_id,
        payload=payload, source=source,
    )


# -------------------- file tail iterators --------------------

def tail_file(
    path: Path, cursor: FileCursor, *, max_lines: int = 1000,
) -> Iterator[tuple[str, FileCursor]]:
    """Yield (line, updated_cursor) tuples since the cursor position.

    Detects log rotation via inode mismatch: closes the old fd, re-opens
    the new file from byte 0, and updates the cursor accordingly.
    Stops after max_lines per drain pass so the daemon doesn't starve
    other sources under a fast-writing log.
    """
    if not path.exists():
        return
    try:
        st = path.stat()
    except OSError:
        return
    current_inode = st.st_ino
    current_fp = _file_fingerprint(path)
    start_offset = cursor.offset
    rotated = (
        (cursor.inode and cursor.inode != current_inode) or
        (cursor.fp_sha and current_fp and cursor.fp_sha != current_fp) or
        (cursor.offset > st.st_size)
    )
    if rotated:
        start_offset = 0
    new_cursor = FileCursor(inode=current_inode, offset=start_offset,
                            fp_sha=current_fp)
    yielded = 0
    try:
        with open(path, 'rb') as fh:
            fh.seek(start_offset)
            while yielded < max_lines:
                raw = fh.readline()
                if not raw:
                    break
                if not raw.endswith(b'\n'):
                    # Partial line — leave offset before it for next drain.
                    break
                try:
                    line = raw.decode('utf-8', errors='replace')
                except Exception:
                    new_cursor.offset = fh.tell()
                    continue
                new_cursor.offset = fh.tell()
                yielded += 1
                yield line, FileCursor(inode=new_cursor.inode,
                                       offset=new_cursor.offset,
                                       fp_sha=new_cursor.fp_sha)
    except OSError:
        return


# -------------------- journalctl tail --------------------

def iter_journalctl(
    after_cursor: str = '',
    unit: str = JOURNALCTL_UNIT,
    once: bool = True,
    timeout_sec: float = 10.0,
) -> Iterator[tuple[dict[str, Any], str]]:
    """Yield ``(record, cursor)`` for each journalctl JSON record after
    ``after_cursor``.

    PR-E2 #18: we DON'T pass ``--cursor-file`` — letting journalctl own cursor
    advancement meant a timeout/SIGTERM could terminate it AFTER it had advanced
    the cursor past records that were streamed into the pipe but never consumed
    here, silently dropping them. Instead we resume with ``--after-cursor`` and
    surface each record's own ``__CURSOR`` so the CALLER advances the persisted
    cursor only as far as it actually consumed (mirroring the tail_file sources).

    ``after_cursor`` empty → no ``--after-cursor`` flag → drain from the start of
    the unit's journal (the old first-run behaviour). once=True drains then exits.
    """
    cmd = [
        'journalctl',
        '-u', unit,
        '--output=json',
        '--no-pager',
    ]
    if after_cursor:
        # --after-cursor is exclusive: it yields records STRICTLY after the given
        # cursor, so the last-consumed record is never re-emitted.
        cmd.append(f'--after-cursor={after_cursor}')
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except FileNotFoundError:
        return
    try:
        assert proc.stdout is not None
        deadline = time.monotonic() + timeout_sec if once else None
        for line in proc.stdout:
            if deadline and time.monotonic() > deadline:
                break
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            cursor = record.get('__CURSOR')
            if not isinstance(cursor, str) or not cursor:
                # No usable cursor on this record — yield it but advance to the
                # empty sentinel so the caller keeps its prior cursor rather than
                # persisting an un-resumable position.
                cursor = ''
            yield record, cursor
    finally:
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.wait(timeout=2)
        if proc.returncode is not None and proc.returncode != 0:
            stderr_text = ''
            if proc.stderr is not None:
                with contextlib.suppress(Exception):
                    stderr_text = proc.stderr.read() or ''
            logging.getLogger('chain_event_shipper').warning(
                'journalctl exited non-zero: returncode=%d stderr=%r',
                proc.returncode, stderr_text[:500],
            )


# -------------------- Supabase sink --------------------

def _resolve_client_options_cls():
    """Locate supabase-py's ClientOptions across version/layout drift.

    Recent supabase-py (2.x) re-exports ClientOptions from the top-level
    ``supabase`` package; older builds expose it only under
    ``supabase.lib.client_options``. Try the cheap top-level import first, then
    the lib path. Returns None if neither resolves (supabase-py absent, or a
    layout we don't recognise) so callers can degrade to an un-pinned client
    rather than crash the producer.
    """
    try:
        from supabase import ClientOptions  # type: ignore
        return ClientOptions
    except ImportError:
        pass
    try:
        from supabase.lib.client_options import ClientOptions  # type: ignore
        return ClientOptions
    except ImportError:
        pass
    return None


def build_client_options(timeout_sec: int = SUPABASE_TIMEOUT_SEC) -> Optional[Any]:
    """Build a supabase-py ClientOptions pinning the PostgREST request timeout.

    Without an explicit timeout, supabase-py's PostgREST client uses its own
    multi-tens-of-seconds default (~60s in 2.x). Every chain_events write in
    this codebase is synchronous: here in the shipper's drain loop and — more
    dangerously — at the push-emit sites that run inside the single-threaded
    outbox-notifier ``process_outbox`` loop (POLL_INTERVAL_SECONDS=5). There a
    single Supabase network black-hole blocks one emit for the whole default
    window and serializes EVERY agent's notifications behind it. Pinning the
    timeout to ``SUPABASE_TIMEOUT_SEC`` makes a black-hole fail fast so the
    best-effort writers fall through to their WARN+drop / buffer paths.

    Returns None when ClientOptions can't be located or doesn't accept the
    ``postgrest_client_timeout`` kwarg (version drift); the caller then builds
    the client without options, preserving prior behaviour rather than crashing.
    """
    options_cls = _resolve_client_options_cls()
    if options_cls is None:
        return None
    try:
        return options_cls(postgrest_client_timeout=timeout_sec)
    except TypeError:
        return None


def build_client(url: str, key: str,
                 timeout_sec: int = SUPABASE_TIMEOUT_SEC):
    """Build a supabase client with the PostgREST request timeout pinned.

    The actual client is built through ``supabase_factory.get_supabase_client``
    — the ONE guarded chokepoint (test-jail Layer B, H7), so an un-mocked build
    from a test process raises ``TestIsolationBreach`` instead of connecting to
    the live project. ``get_supabase_client`` raises ``ImportError`` if
    supabase-py is absent; callers keep their own ImportError policy (emit
    returns None, SupabaseSink raises RuntimeError).

    The timeout comes from ``build_client_options``. If this supabase-py build
    can't supply ClientOptions, OR its ``create_client`` rejects the ``options=``
    keyword (signature drift), fall back to an un-pinned client rather than let
    the failure escape: ``_get_client``'s callers treat a raised exception as a
    producer crash (``emit_event`` is documented best-effort/never-raises and
    invokes ``_get_client`` outside its try/except), so a TypeError here would
    wedge the very loop the timeout exists to protect. See build_client_options.
    """
    from supabase_factory import get_supabase_client  # type: ignore
    options = build_client_options(timeout_sec)
    if options is None:
        return get_supabase_client(url, key)
    try:
        return get_supabase_client(url, key, options=options)
    except TypeError:
        return get_supabase_client(url, key)


class SupabaseSink:
    """Wraps the supabase-py client with insert-or-buffer semantics.

    Constructed lazily so unit tests (which monkey-patch insert) don't
    need the supabase package installed.
    """

    def __init__(self, table: str = 'chain_events') -> None:
        self.table_name = table
        self._client = None

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        url = os.environ.get('SUPABASE_URL')
        key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
        if not url or not key:
            raise RuntimeError(
                'SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — '
                'cannot connect to Supabase.'
            )
        # Pin the PostgREST request timeout (SUPABASE_TIMEOUT_SEC) so a Supabase
        # network black-hole fails fast instead of blocking each drain for
        # supabase-py's multi-tens-of-seconds default — see build_client. The
        # client is built through the guarded supabase_factory chokepoint, which
        # raises ImportError when supabase-py is absent.
        try:
            self._client = build_client(url, key)
        except ImportError as exc:
            raise RuntimeError(
                'supabase-py is not installed. '
                'pip3 install --user --break-system-packages supabase'
            ) from exc

    def insert_rows(self, rows: list[dict[str, Any]]) -> None:
        """INSERT a batch with PK conflict treated as no-op.

        Uses upsert(ignore_duplicates=True, on_conflict='event_id') which
        translates to PostgREST's `Prefer: resolution=ignore-duplicates`,
        matching `INSERT ... ON CONFLICT (event_id) DO NOTHING`.
        """
        if not rows:
            return
        self._ensure_client()
        assert self._client is not None
        self._client.table(self.table_name).upsert(
            rows, on_conflict='event_id', ignore_duplicates=True,
        ).execute()


# -------------------- buffer (write-ahead spill on Supabase failure) --------------------

class EventBuffer:
    """Append-only spill file for events that couldn't reach Supabase.

    Bounded by line count + byte size. When over the cap, the OLDEST lines
    drop. This is intentional: under chronic Supabase outage we'd rather
    keep tailing fresh events than block waiting for replay capacity.
    """

    def __init__(self, path: Path = BUFFER_FILE,
                 max_lines: int = BUFFER_MAX_LINES,
                 max_bytes: int = BUFFER_MAX_BYTES) -> None:
        self.path = path
        self.max_lines = max_lines
        self.max_bytes = max_bytes

    def append(self, rows: list[dict[str, Any]]) -> int:
        """Append rows to the buffer. Returns count of rows dropped due to cap."""
        if not rows:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, 'a', encoding='utf-8') as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        return self._trim_if_overflowing()

    def _trim_if_overflowing(self) -> int:
        if not self.path.exists():
            return 0
        try:
            size = self.path.stat().st_size
        except OSError:
            return 0
        if size <= self.max_bytes:
            with open(self.path, 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
            if len(lines) <= self.max_lines:
                return 0
        with open(self.path, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()
        if len(lines) <= self.max_lines and \
                sum(len(line.encode('utf-8')) for line in lines) <= self.max_bytes:
            return 0
        # Drop oldest until under both caps.
        dropped = 0
        while lines and (
            len(lines) > self.max_lines or
            sum(len(line.encode('utf-8')) for line in lines) > self.max_bytes
        ):
            lines.pop(0)
            dropped += 1
        with open(self.path, 'w', encoding='utf-8') as fh:
            fh.writelines(lines)
        return dropped

    def drain(self) -> list[dict[str, Any]]:
        """Read all buffered rows; caller must clear() on successful flush."""
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(self.path, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


# -------------------- main daemon loop --------------------

@dataclass
class DrainStats:
    journal: int = 0
    outbox_log: int = 0
    pulse_escalations: int = 0
    larry_alerts: int = 0
    sentinel_alerts: int = 0
    dropped_unknown_type: int = 0
    dropped_buffer_overflow: int = 0
    inserted: int = 0
    buffered: int = 0
    flushed_from_buffer: int = 0


def drain_once(
    sink: SupabaseSink,
    buffer: EventBuffer,
    log_cursors: dict[str, FileCursor],
    pulse_cursor: PulseCursor,
    logger: logging.Logger,
    *,
    journal_cursor: str = '',
    journal_iter_fn=None,
    pulse_path: Path = PULSE_ESCALATIONS_JSON,
    outbox_log_path: Path = OUTBOX_NOTIFIER_LOG,
    larry_alerts_path: Path = LARRY_ALERTS_JSONL,
    sentinel_alerts_path: Path = SENTINEL_ALERTS_JSONL,
) -> tuple[DrainStats, PulseCursor, str]:
    """Walk all five sources, build event rows, INSERT or buffer.

    Pure-function-shaped for testing: pass mocked sink + buffer +
    cursors + journal_iter_fn and verify the resulting DrainStats.

    Returns ``(stats, new_pulse_cursor, new_journal_cursor)``. The journal cursor
    advances only as far as records are actually consumed here (PR-E2 #18), and
    the caller persists it AFTER the drain — so a deadline/SIGTERM mid-stream
    re-reads the un-consumed tail next time rather than skipping it.
    """
    stats = DrainStats()
    events: list[ChainEvent] = []
    unknown_types: list[str] = []

    # 1. journalctl
    new_journal_cursor = journal_cursor
    j_iter = journal_iter_fn() if journal_iter_fn else iter_journalctl(journal_cursor)
    for record, jcursor in j_iter:
        # Advance our in-memory cursor to every record we consume (even ones that
        # don't parse into an event — like tail_file advancing past a bad line).
        if jcursor:
            new_journal_cursor = jcursor
        ev = parse_journal_record(record)
        if ev:
            events.append(ev)
            stats.journal += 1
        else:
            # Detect unknown event_type so the WARN log is loud.
            structured_type = record.get('OURLIBERTY_EVENT_TYPE')
            if structured_type and structured_type not in KNOWN_EVENT_TYPES:
                unknown_types.append(structured_type)
                stats.dropped_unknown_type += 1

    # 2. outbox-notifier.log
    log_cursor = log_cursors.get('outbox_log', FileCursor())
    new_log_cursor = log_cursor
    for line, updated in tail_file(outbox_log_path, log_cursor):
        new_log_cursor = updated
        ev = parse_log_line(line)
        if ev:
            events.append(ev)
            stats.outbox_log += 1
    log_cursors['outbox_log'] = new_log_cursor

    # 3. pulse-escalations.json (snapshot)
    new_pulse_cursor = pulse_cursor
    if pulse_path.exists():
        try:
            stat = pulse_path.stat()
            content = pulse_path.read_text()
            content_sha = hashlib.sha256(content.encode('utf-8')).hexdigest()
            if content_sha != pulse_cursor.sha256:
                pulse_events = parse_pulse_escalations(content)
                events.extend(pulse_events)
                stats.pulse_escalations += len(pulse_events)
                new_pulse_cursor = PulseCursor(mtime=stat.st_mtime,
                                               sha256=content_sha)
        except OSError as e:
            logger.warning('pulse-escalations read failed: %s', e)

    # 4. larry-alerts.jsonl
    larry_cursor = log_cursors.get('larry_alerts', FileCursor())
    new_larry = larry_cursor
    for line, updated in tail_file(larry_alerts_path, larry_cursor):
        new_larry = updated
        ev = parse_jsonl_line(line, source='larry_alerts')
        if ev:
            events.append(ev)
            stats.larry_alerts += 1
    log_cursors['larry_alerts'] = new_larry

    # 5. sentinel-alerts.jsonl
    sentinel_cursor = log_cursors.get('sentinel_alerts', FileCursor())
    new_sentinel = sentinel_cursor
    for line, updated in tail_file(sentinel_alerts_path, sentinel_cursor):
        new_sentinel = updated
        ev = parse_jsonl_line(line, source='sentinel_alerts')
        if ev:
            events.append(ev)
            stats.sentinel_alerts += 1
    log_cursors['sentinel_alerts'] = new_sentinel

    for ut in unknown_types:
        logger.warning(
            'UNKNOWN_EVENT_TYPE event_type=%s — dropped (will be flagged by '
            'weekly audit healer if it lands via another writer)', ut,
        )

    rows = [ev.to_row() for ev in events]

    # Try to flush buffer FIRST so older events ship before new ones —
    # FIFO ordering matters for downstream dedupless consumers (none today
    # but the schema is queryable in time order).
    pending_buffer = buffer.drain()
    all_rows = pending_buffer + rows

    if not all_rows:
        return stats, new_pulse_cursor, new_journal_cursor

    try:
        sink.insert_rows(all_rows)
        stats.inserted = len(all_rows)
        stats.flushed_from_buffer = len(pending_buffer)
        buffer.clear()
    except Exception as e:
        logger.warning(
            'Supabase insert failed (%s); buffering %d rows',
            type(e).__name__, len(all_rows),
        )
        # Re-buffer the rows we just drained PLUS the fresh ones.
        # buffer.drain() didn't clear the file — but the rows are already
        # on disk. The cleanest semantic: clear, then append everything.
        buffer.clear()
        dropped = buffer.append(all_rows)
        stats.buffered = len(all_rows)
        stats.dropped_buffer_overflow = dropped
        if dropped:
            logger.warning(
                'BUFFER_OVERFLOW dropped=%d oldest events; chronic outage?', dropped,
            )

    return stats, new_pulse_cursor, new_journal_cursor


# -------------------- run / argparse --------------------

_should_stop = threading.Event()


def _sigterm_handler(signum, frame):  # noqa: ARG001
    _should_stop.set()


def kill_switch_active() -> bool:
    if KILL_SWITCH.exists():
        return True
    if os.environ.get('OURLIBERTY_CHAIN_SHIPPER_ENABLED', '').lower() == 'false':
        return True
    return False


def run_loop(logger: logging.Logger) -> int:
    """Daemon loop: drain → heartbeat → sleep, until SIGTERM or kill-switch."""
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    sink = SupabaseSink()
    buffer = EventBuffer()
    log_cursors = load_log_cursors()
    pulse_cursor = load_pulse_cursor()
    journal_cursor = load_journal_cursor()

    logger.info(
        'chain_event_shipper starting: drain_interval=%ds heartbeat=%ds',
        DRAIN_INTERVAL_SEC, HEARTBEAT_INTERVAL_SEC,
    )

    last_heartbeat = 0.0
    while not _should_stop.is_set():
        if kill_switch_active():
            logger.info('kill-switch active; exiting cleanly')
            return 0
        try:
            stats, pulse_cursor, journal_cursor = drain_once(
                sink, buffer, log_cursors, pulse_cursor, logger,
                journal_cursor=journal_cursor,
            )
            # Persist cursors only AFTER the drain (and its insert/buffer) — a
            # crash before this re-reads the un-persisted tail, never skips it.
            save_log_cursors(log_cursors)
            save_pulse_cursor(pulse_cursor)
            save_journal_cursor(journal_cursor)
            if any((stats.journal, stats.outbox_log, stats.pulse_escalations,
                    stats.larry_alerts, stats.sentinel_alerts,
                    stats.dropped_unknown_type, stats.dropped_buffer_overflow,
                    stats.flushed_from_buffer)):
                logger.info(
                    'drain: journal=%d log=%d pulse=%d larry=%d sentinel=%d '
                    'inserted=%d buffered=%d flushed=%d dropped_unknown=%d '
                    'dropped_overflow=%d',
                    stats.journal, stats.outbox_log, stats.pulse_escalations,
                    stats.larry_alerts, stats.sentinel_alerts,
                    stats.inserted, stats.buffered, stats.flushed_from_buffer,
                    stats.dropped_unknown_type, stats.dropped_buffer_overflow,
                )
        except Exception as e:
            logger.error('drain_once raised: %s: %s', type(e).__name__, e)

        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            heartbeat()
            last_heartbeat = now

        _should_stop.wait(timeout=DRAIN_INTERVAL_SEC)

    logger.info('chain_event_shipper stopping (SIGTERM)')
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true',
                        help='Single drain pass then exit (for tests).')
    parser.add_argument('--no-backfill', action='store_true',
                        help='No-op; backfill is never on by default.')
    parser.add_argument('--log-level', default='INFO')
    args = parser.parse_args(argv)

    logger = _setup_logging(getattr(logging, args.log_level.upper(), logging.INFO))

    if kill_switch_active():
        logger.info('kill-switch active at startup; exiting cleanly')
        return 0

    if args.once:
        sink = SupabaseSink()
        buffer = EventBuffer()
        log_cursors = load_log_cursors()
        pulse_cursor = load_pulse_cursor()
        journal_cursor = load_journal_cursor()
        try:
            stats, pulse_cursor, journal_cursor = drain_once(
                sink, buffer, log_cursors, pulse_cursor, logger,
                journal_cursor=journal_cursor,
            )
        finally:
            save_log_cursors(log_cursors)
            save_pulse_cursor(pulse_cursor)
            save_journal_cursor(journal_cursor)
            heartbeat()
        logger.info('--once drain complete: %s', stats)
        return 0

    return run_loop(logger)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        logging.getLogger('chain_event_shipper').error(
            'FATAL: %s: %s', type(exc).__name__, exc,
        )
        sys.exit(1)
