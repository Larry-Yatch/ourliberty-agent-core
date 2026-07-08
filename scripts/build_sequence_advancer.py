#!/usr/bin/env python3
"""build_sequence_advancer.py — polling daemon that advances multi-step builds.

Phase E-orchestrator PR-S2. Spec: agents/beacon/specs/build-sequence-orchestrator.md
§ 5.1 (sequence-file schema), § 5.2 (advancer architecture), § 5.3 (belt-and-
suspenders gate), § 5.4 (failure handling), § 5.8 (data sources).

The daemon polls `~/agents/blackboard/build-sequences/*.json` every 5 minutes
(via systemd timer). For each `active` sequence:

  1. For each in-flight step (in `current_steps`): check both gate signals
     — a `chain_events` row with event_type='auto_merge' AND
     payload['outcome']='merged', AND `gh pr view <pr_url>` returning
     state=MERGED. Per spec § 5.3 belt-and-suspenders, both signals must
     agree before the step transitions to `merged`. A 30-minute one-signal-
     only mismatch tolerance covers the transient lag between merge and
     chain_events ingestion; longer than that, the advancer pauses the
     sequence and DMs Larry.
  2. For each `pending` step: when every `depends_on` has reached `merged`,
     transition to `dispatchable` then immediately dispatch (envelope to
     Beacon's inbox per spec § 5.8) and transition to `dispatched`.
  3. If all steps are `merged`, set sequence status to `complete` and DM
     Larry with a one-line summary.
  4. If any step's PR shows `mirror_revision_exhausted` / `mirror_emergency_halt`
     / `forge_reject` in chain_events, set sequence status to `paused` and
     DM Larry.

State discipline (spec § 5.4 failure modes + spec § 5.1 atomic-write rule):

  - The sequence file is the ONLY durable state the advancer holds. There
    is no internal cursor, no separate state cache. On reboot, the first
    tick re-reads every sequence file and queries chain_events from each
    in-flight step's `dispatched_at` forward — the daemon rebuilds live
    state without any side state file.
  - All writes to sequence files use tmp + os.replace for atomicity. A
    crash mid-write leaves either the pre-write or the post-write file
    intact; readers never see a partial JSON.
  - Schema validation (build_sequence_validator.validate_dag) runs on
    every read. Schema-invalid sequence files are paused (status=paused +
    audit_log entry) via atomic rewrite, and Larry is DMed. Files that
    won't even parse as JSON are skipped (cannot be re-serialized) and
    Larry is DMed; the daemon never crashes on a single bad file. Other
    sequences keep advancing.
  - Heartbeat at `~/agents/blackboard/build-sequence-advancer.heartbeat`
    (mtime-only text file, matching the codebase's 10 existing heartbeat
    healers). `heal_build_sequence_advancer_heartbeat.py` DMs Larry when
    the mtime is >15 min stale.

Concurrency (per spec § 5.2 decision A): exactly one live sequence at a
time. The validator's `validate_no_concurrent_active()` helper gates new
sequence creation (PR-S3 wires the call site). The advancer itself does
not enforce concurrency — it just processes whatever active sequences
exist; if two are present (a manual mistake), it advances both.

Activation gate: `OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=false` by
default in the systemd unit. The daemon checks the env var at the top of
each tick and exits cleanly when unset/false. Flip to `true` after PR-S3
+ PR-S4 land and Larry has verified the kickoff round-trip.

Operator interface:
  - default: run `tick()` once and exit (the systemd timer fires every 5 min)
  - --once: synonym for default; kept for explicitness
  - --dump-state <seq_id>: print the parsed sequence + computed state to
    stdout; no writes
  - OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=false → exit early (kill-switch)
  - ~/agents/healers.disabled → exit early (blanket kill-switch — applies
    to daemons too per the codebase convention)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
LOG_FILE = AGENTS_ROOT / 'logs' / 'build-sequence-advancer.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'build-sequence-advancer.heartbeat'
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
BLACKBOARD_DIR = AGENTS_ROOT / 'blackboard' / 'build-sequences'
BEACON_INBOX = AGENTS_ROOT / 'inboxes' / 'beacon'
# The advancer's OWN inbox. `approve sequence <id>` via the Telegram bot routes
# a kickoff APPROVAL_REQUEST (target_agent=build_sequence_advancer) here through
# beacon_approval_handler.dispatch_approved → safe_write_inbox. Nothing consumed
# this inbox before — the kickoff special-casing existed only on the file-outbox
# path (outbox_notifier._handle_build_sequence_advancer_kickoff), which the
# in-process chat bot does NOT use. So a chat-issued kickoff orphaned here and
# the sequence stalled at `pending` (observed live on seq `pulse-check-xii`,
# 2026-07-07). `_drain_kickoff_inbox` closes that gap source-agnostically.
ADVANCER_INBOX = AGENTS_ROOT / 'inboxes' / 'build_sequence_advancer'

# Per-sequence last-alerted validation-error signature for sequences that are
# ALREADY off the active path (status paused/failed/archived). Kept OUTSIDE the
# sequence file so re-validating a deliberately-parked, schema-invalid sequence
# every tick doesn't append to its audit_log (write amplification) nor re-DM
# Larry hourly (an expected-by-design, non-actionable alert). One small JSON
# ({sig, ts}) per sequence, written atomically. Module-level so tests can
# redirect it (it also follows OURLIBERTY_AGENTS_ROOT for free).
INVALID_ALERT_SIG_DIR = AGENTS_ROOT / 'state' / 'advancer-invalid-alert-sigs'

# Canonical kickoff wording per build-sequence-orchestrator spec § 5.5
# discipline 2 (`prompt: kickoff <seq-id>`). Same shape outbox_notifier's
# kickoff handler parses, so both kickoff paths accept exactly the same marker.
_KICKOFF_PROMPT_RE = re.compile(r'^\s*kickoff\s+([A-Za-z0-9._-]+)\s*$')
# A seq_id becomes a filename under BLACKBOARD_DIR, so it must be a bare stem.
# The prompt regex already enforces this charset; the `task_id` fallback path
# does NOT, so a `task_id: kickoff-../../beacon/foo` would otherwise resolve
# `BLACKBOARD_DIR / '../../beacon/foo.json'` and let the drain write `active`
# outside the sequences dir. Both extraction paths funnel through this guard.
_KICKOFF_SEQ_ID_RE = re.compile(r'^[A-Za-z0-9._-]+$')

ACTIVATION_ENV = 'OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED'

# 30-minute one-signal-only mismatch tolerance per spec § 5.3.
GATE_MISMATCH_TIMEOUT_SEC = 30 * 60

# Wall-clock backstop for a step that's `dispatched` but never makes progress
# (no PR, no gate signal) — the silent-wedge class the reconcile pass can't see
# (it only RETIRES merged work). Set generously beyond any real build so a
# legitimately-slow build is never paused; a step still pr_url-less past this is
# almost certainly stranded (the 2026-06-19 `ol-work` launch sat ~6h with no
# failure + no alert). The invalid-target_repo escalation below fires
# IMMEDIATELY — this timeout is only the catch-all for OTHER stall causes.
# Tunable; see spec launch-repo-validation-and-silent-wedge.md § 3 Layer C.
DISPATCH_STALL_TIMEOUT_SEC = 4 * 60 * 60

# Wall-clock backstop for the WITH-PR wedge (completeness-pr2 Face 2 /
# sequence-step-stall-recovery Fix B). A step that opened a PR and then wedged
# in review — Mirror never reviewed, or her REVISION dead-lettered (the #532
# shape) — otherwise has NO timeout ever: the no-PR stall guard above short-
# circuits on `and not step.get('pr_url')`, and the pre-check that records
# `pr_url` on detecting an open PR then permanently suppresses re-escalation.
# This closes that permanently-unmonitored state. Set generously (6h) vs. the
# ~1h typical review round so a healthy long review is never disturbed; a step
# still `dispatched` with a live-but-unmerged PR past this has a demonstrably
# dead review loop and its review is re-dispatched (recover-then-alert).
# Distinct from the 4h no-PR DISPATCH_STALL_TIMEOUT_SEC — different failure
# mode, different (longer) horizon. Tunable.
REVIEW_STALL_TIMEOUT_SEC = 6 * 60 * 60

# Bounded subprocess timeout for `gh pr view`. The CLI usually responds in
# under a second; 30s gives the network layer wide headroom without
# letting a wedged subprocess block the entire tick.
GH_PR_VIEW_TIMEOUT_SEC = 30

# Bounded supabase timeout. Same reasoning as above — give the network
# layer headroom but don't let one slow query stall the tick.
SUPABASE_TIMEOUT_SEC = 15

# Bounded `gh pr list` timeout for the active-reconciliation pass.
# Tighter than `gh pr view` because the list call queries every active
# sequence's repo on every tick; we'd rather skip reconciliation for one
# tick than wedge the daemon.
GH_PR_LIST_TIMEOUT_SEC = 10

# How many recently-merged PRs to scan per repo per tick for the
# reconciliation pass. Widened from the original 20 (spec §3.4b): the
# reconcile pass now runs flag-independent of the default-OFF advancer, so a
# silent-miss can sit unreconciled for far longer than a week before the pass
# is the thing that retires it — the lookback must cover that tail. 50 matches
# task_terminal_state.DEFAULT_PR_LOOKBACK so both probes see the same horizon.
RECONCILE_PR_LOOKBACK = 50

# Recency bound for the #609-follow-up pre-dispatch already-merged match
# (_already_merged_launch_match). A launch step sits `pending` only minutes
# before dispatch, and a genuine duplicate's PR merges around that window, so a
# PR merged longer ago than this is NOT evidence that a freshly-authored launch
# is redundant. Generous (7d) so a real recent duplicate is never rejected while
# a stale, unrelated forge/<phase_id> branch can never falsely mark a re-launch
# done.
PREDISPATCH_MERGE_LOOKBACK_SEC = 7 * 24 * 60 * 60

# GitHub owner for the sandbox repos. Sequence steps store a bare repo
# name (e.g. 'ourliberty-agent-core'), but `gh --repo` requires the
# OWNER/REPO form — passing the bare name fails with rc=1 ("expected the
# [HOST/]OWNER/REPO format") before any auth/network, which is why the
# reconciliation pass never queried anything. Matches the GH_REPO
# convention used across the rest of the scripts (catch_me_up, heal_*).
GITHUB_OWNER = os.environ.get('OURLIBERTY_GH_OWNER', 'Larry-Yatch')

# Repo scripts dir on sys.path so sibling imports (larry_alerts,
# build_sequence_validator) resolve cleanly when invoked by systemd.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import build_sequence_validator as bsv  # noqa: E402
import projects_status_writeback as psw  # noqa: E402 — phase status writeback (p3f)
import projects_store  # noqa: E402 — shared launch-sequence id resolution (p3f)
import task_terminal_state as tts  # noqa: E402 — shared terminal-state probe kernel
from id_match import id_matches  # noqa: E402


# -------------------- logging + heartbeat --------------------


def _setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger('build_sequence_advancer')
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
    """Touch the heartbeat file with the current ISO timestamp.

    Mirrors the chain_event_shipper pattern (mtime + ISO body for
    operator debugging). The healer reads mtime only — the body is for
    humans tailing the file."""
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def kill_switch_active() -> bool:
    """Blanket kill-switch — touching ~/agents/healers.disabled stops every
    daemon and healer that respects this convention."""
    return KILL_SWITCH.exists()


def activation_enabled() -> bool:
    """Default-off. Flip OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=true
    after PR-S3 (dashboard) + PR-S4 (Beacon wiring) land and the kickoff
    round-trip is verified end-to-end."""
    return os.environ.get(ACTIVATION_ENV, 'false').strip().lower() in (
        'true', '1', 'yes', 'on',
    )


# -------------------- DM helper --------------------


def _dm_larry(
    message: str,
    subject: str,
    severity: str = 'warning',
    suggested_action: Optional[str] = None,
) -> bool:
    """Fire larry_alerts.append_alert. Cooldown is enforced inside.

    Severity vocabulary is the existing {warning, critical} pair — no new
    levels introduced. Subject-specific cooldown means corrupted-sequence
    DMs for seq A and seq B each have their own bucket."""
    try:
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='build-sequence-advancer',
            severity=severity,
            message=message,
            subject=subject,
            suggested_action=suggested_action,
        )
    except Exception as e:
        _setup_logging().warning(
            f'dm_larry failed: {type(e).__name__}: {e}'
        )
        return False


# -------------------- atomic sequence-file I/O --------------------


def _read_sequence(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Return (parsed_dict, error). On success: (dict, None). On any failure:
    (None, error_string). Errors are JSON parse errors or read errors; schema
    validation is a separate downstream step."""
    try:
        text = path.read_text()
    except OSError as e:
        return None, f'read failed: {e}'
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f'invalid JSON: {e}'


def _atomic_write_sequence(path: Path, seq: dict[str, Any]) -> None:
    """tmp + os.replace for atomicity. Raises OSError on failure (caller
    decides whether to DM Larry on durable I/O failure)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(seq, f, indent=2)
            f.write('\n')
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _audit_entry(event: str, **fields: Any) -> dict[str, Any]:
    """Build an audit_log entry. Per spec § 5.1 the audit_log is append-only;
    callers append to seq['audit_log'] before _atomic_write_sequence."""
    entry: dict[str, Any] = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'event': event,
        'actor': 'advancer',
    }
    entry.update(fields)
    return entry


def _iter_sequence_files() -> Iterable[Path]:
    """Yield every *.json file under the blackboard build-sequences dir,
    creating the dir on first call if missing. Hidden files + subdirs
    (including .archive/) are skipped."""
    BLACKBOARD_DIR.mkdir(parents=True, exist_ok=True)
    for entry in sorted(BLACKBOARD_DIR.iterdir()):
        if entry.is_file() and entry.suffix == '.json' and not entry.name.startswith('.'):
            yield entry


# -------------------- kickoff-inbox drain (chat-path routing gap fix) --------------------


def _seq_id_from_kickoff_envelope(env: dict[str, Any]) -> Optional[str]:
    """Extract the target seq_id from a kickoff envelope, or None if the
    envelope is not a kickoff marker.

    Mirrors outbox_notifier._handle_build_sequence_advancer_kickoff: prefer the
    canonical `prompt: kickoff <seq-id>`, fall back to a `task_id` of shape
    `kickoff-<seq-id>`. Returning None means "not a kickoff" — the caller
    leaves the file untouched rather than consuming an envelope it can't act on.

    Both extraction paths funnel through `_KICKOFF_SEQ_ID_RE` so the fallback
    can't smuggle a path-separator seq_id past the prompt regex's charset (the
    traversal guard — the seq_id is used to build a path under BLACKBOARD_DIR).
    """
    candidate: Optional[str] = None
    prompt = env.get('prompt')
    if isinstance(prompt, str):
        m = _KICKOFF_PROMPT_RE.match(prompt)
        if m:
            candidate = m.group(1)
    if candidate is None:
        task_id = env.get('task_id')
        if isinstance(task_id, str) and task_id.startswith('kickoff-'):
            candidate = task_id[len('kickoff-'):].strip() or None
    if candidate is None or not _KICKOFF_SEQ_ID_RE.match(candidate):
        return None
    return candidate


def _drain_kickoff_inbox(logger: logging.Logger) -> int:
    """Consume kickoff APPROVAL_REQUEST envelopes addressed to the advancer.

    Closes the chat-path routing gap: `approve sequence <id>` sent to Beacon
    via Telegram dispatches a kickoff marker to ADVANCER_INBOX (target_agent=
    build_sequence_advancer), but historically nothing read it, so a
    chat-issued kickoff stalled the sequence at `pending`. This drain performs
    the same `pending → active` transition the file-outbox path already does in
    outbox_notifier — source-agnostically, since any envelope landing in this
    inbox is by construction addressed to us.

    Idempotent and safe:
      - Only envelopes that parse as a kickoff (`kickoff <seq-id>`) are acted
        on; anything else is left in place (logged) rather than deleted.
      - A `pending` sequence is transitioned to `active`; a non-`pending`
        sequence is a no-op (already kicked / terminal) — either way the
        envelope is consumed so it never reprocesses.
      - The envelope is deleted ONLY after a successful transition-or-no-op.
        A failed sequence-file write leaves the envelope for the next tick.
      - Malformed envelopes / missing / invalid target sequences DM Larry once
        (subject-bucketed) and the envelope is dropped so it can't loop.

    Returns the number of sequences transitioned `pending → active` this tick.
    """
    if not ADVANCER_INBOX.is_dir():
        return 0
    kicked = 0
    for env_path in sorted(ADVANCER_INBOX.iterdir()):
        if not (env_path.is_file() and env_path.suffix == '.json'
                and not env_path.name.startswith('.')):
            continue
        try:
            env = json.loads(env_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f'kickoff-inbox: unreadable envelope {env_path.name}: {e}')
            _dm_larry(
                message=(
                    f'Build-sequence advancer got an unreadable kickoff envelope '
                    f'at `{env_path}` ({e}). Dropping it so the inbox does not '
                    f'loop; re-issue `approve sequence <id>` if a kickoff was '
                    f'intended.'
                ),
                subject=f'kickoff-inbox-unreadable:{env_path.name}',
            )
            _unlink_quietly(env_path, logger)
            continue
        if not isinstance(env, dict):
            logger.warning(f'kickoff-inbox: non-object envelope {env_path.name}; dropping')
            _unlink_quietly(env_path, logger)
            continue

        seq_id = _seq_id_from_kickoff_envelope(env)
        if seq_id is None:
            # Not a kickoff marker. Nothing else routes here today, but do not
            # delete an envelope we don't understand — surface it and move on.
            logger.warning(
                f'kickoff-inbox: envelope {env_path.name} is not a kickoff '
                f'marker (prompt={env.get("prompt")!r} task_id='
                f'{env.get("task_id")!r}); leaving in place'
            )
            continue

        seq_path = BLACKBOARD_DIR / f'{seq_id}.json'
        seq, err = _read_sequence(seq_path)
        if seq is None:
            reason = 'missing' if not seq_path.exists() else (err or 'unreadable')
            logger.warning(
                f'kickoff-inbox: kickoff for seq `{seq_id}` but sequence file '
                f'{reason} at {seq_path}; dropping envelope'
            )
            _dm_larry(
                message=(
                    f'`approve sequence {seq_id}` was received but its sequence '
                    f'file is {reason} at `{seq_path}`. No kickoff performed. '
                    f'Author the sequence file, then re-issue the approval.'
                ),
                subject=f'kickoff-inbox-seq-{reason}:{seq_id}',
            )
            _unlink_quietly(env_path, logger)
            continue

        validation = bsv.validate_dag(seq)
        if not validation.valid:
            first_errs = '; '.join(validation.errors[:3])
            logger.warning(
                f'kickoff-inbox: seq `{seq_id}` fails DAG validation '
                f'({first_errs}); dropping envelope, leaving sequence untouched'
            )
            _dm_larry(
                message=(
                    f'`approve sequence {seq_id}` was received but the sequence '
                    f'file fails validation: {first_errs}. No kickoff performed. '
                    f'Fix the file (see `python3 scripts/build_sequence_'
                    f'validator.py validate {seq_id}`), then re-issue.'
                ),
                subject=f'kickoff-inbox-seq-invalid:{seq_id}',
            )
            _unlink_quietly(env_path, logger)
            continue

        status = seq.get('status')
        if status != 'pending':
            # Idempotent: already kicked (active/paused) or terminal. Consume
            # the envelope without a second transition or a duplicate audit
            # entry — mirrors the outbox handler's WARN no-op contract.
            logger.info(
                f'kickoff-inbox: seq `{seq_id}` already status={status!r}; '
                f'no-op, consuming envelope'
            )
            _unlink_quietly(env_path, logger)
            continue

        seq['status'] = 'active'
        if not isinstance(seq.get('audit_log'), list):
            seq['audit_log'] = []
        seq['audit_log'].append(_audit_entry(
            'kickoff-acknowledged',
            note='chat-path kickoff consumed from advancer inbox',
            kickoff_task_id=env.get('task_id'),
        ))
        try:
            _atomic_write_sequence(seq_path, seq)
        except OSError as e:
            # Leave the envelope in place so the next tick retries the write.
            logger.warning(
                f'kickoff-inbox: seq `{seq_id}` write failed ({e}); leaving '
                f'envelope for retry'
            )
            _dm_larry(
                message=(
                    f'`approve sequence {seq_id}` could not be applied — writing '
                    f'the sequence file at `{seq_path}` failed ({e}). Will retry '
                    f'next tick.'
                ),
                subject=f'kickoff-inbox-write-error:{seq_id}',
            )
            continue

        _unlink_quietly(env_path, logger)
        kicked += 1
        logger.info(
            f'kickoff-inbox: seq `{seq_id}` transitioned pending → active '
            f'(chat-path kickoff); first step dispatches this tick'
        )
    return kicked


def _unlink_quietly(path: Path, logger: logging.Logger) -> None:
    """Best-effort unlink; a failed delete is logged, never raised."""
    try:
        path.unlink()
    except OSError as e:
        logger.warning(f'kickoff-inbox: could not unlink {path}: {e}')


# -------------------- gate-check primitives (spec § 5.3) --------------------


def _connect_supabase():
    """Return a Supabase client or raise on missing creds / library.

    The advancer treats supabase connectivity as a hard requirement — if
    we can't query chain_events, the belt-and-suspenders gate is a
    one-leg gate and the spec's premise fails. The tick handles connect
    failures by logging WARN + DMing Larry once per tick (cooldown-gated)
    + skipping the gate check for that tick. Steps stay in `dispatched`
    until supabase comes back up. Returns None on failure rather than
    raising so the caller can branch without try/except."""
    try:
        url = os.environ.get('SUPABASE_URL')
        key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
        if not url or not key:
            return None
        from supabase_factory import get_supabase_client  # type: ignore
        return get_supabase_client(url, key)
    except Exception:
        return None


def chain_event_says_merged(
    client: Any, task_id: str, dispatched_at: Optional[str],
) -> bool:
    """True iff chain_events has an `auto_merge` row for `task_id` with
    `payload['outcome'] == 'merged'` and `ts >= dispatched_at`.

    Per the 2026-05-27 CLARIFY resolution: the spec § 5.3 mentions an
    `auto_merge_success` event_type that does not exist in the codebase.
    The actual emitted type is `auto_merge` (per
    scripts/chain_event_shipper.py KNOWN_EVENT_TYPES line 96 + the log-
    line parser at line 495), with the merge outcome carried in the
    payload as `outcome=merged|already_merged|failed`. Both `merged` and
    `already_merged` count as a successful gate signal — `already_merged`
    is the resume-after-crash success path per outbox_notifier D3.5
    commit 5d. `failed` does NOT count (the auto-merge itself failed
    and is being retried by heal_pr_auto_merge)."""
    if client is None or not task_id:
        return False
    try:
        query = client.table('chain_events').select(
            'event_type,payload,ts'
        ).eq('task_id', task_id).eq('event_type', 'auto_merge')
        if dispatched_at:
            query = query.gte('ts', dispatched_at)
        res = query.limit(50).execute()
    except Exception:
        return False
    rows = getattr(res, 'data', None) or []
    for row in rows:
        payload = row.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        if payload.get('outcome') in ('merged', 'already_merged'):
            return True
    return False


# Terminal-failure chain_event types the advancer keys on. MUST be a subset of
# chain_event_shipper.KNOWN_EVENT_TYPES — a regression test pins this so the
# detector can never again drift onto names the shipper does not emit (the prior
# set `mirror_revision_exhausted`/`mirror_emergency_halt`/`forge_reject` were
# hypothetical orchestrator-spec names that no writer ever produced):
#   - preflight_reject: Forge rejected the spec at preflight (or a budget-
#     exhausted clarify routed as reject). Real payload keys: marker_type, intent
#     (NO `reason`).
#   - review_escalate: Mirror handed the PR back to Beacon — the shipper folds
#     ESCALATE, EMERGENCY_HALT, and revision-budget exhaustion into this one
#     type. Real payload keys: verdict, marker_type, auto_promoted,
#     budget_exhausted (NO `reason`).
# review_revision is deliberately ABSENT: a revision keeps the step in-flight
# (Forge fixes + Mirror re-reviews), it is NOT a terminal failure.
TERMINAL_FAILURE_EVENT_TYPES: tuple[str, ...] = (
    'preflight_reject', 'review_escalate',
)


def _failure_reason_from_event(event_type: str, payload: dict[str, Any]) -> str:
    """Human-readable failure reason derived from the REAL payload shape the
    shipper emits for each terminal type (neither carries a `reason` key)."""
    if not isinstance(payload, dict):
        payload = {}
    if event_type == 'preflight_reject':
        marker = payload.get('marker_type') or 'reject'
        if payload.get('intent') == 'clarification-exhausted':
            detail = 'Forge ran out of clarification budget at preflight'
        else:
            detail = 'Forge rejected the spec at preflight'
        return f'preflight_reject: {detail} (marker_type={marker})'
    if event_type == 'review_escalate':
        if payload.get('budget_exhausted'):
            cause = 'Mirror revision budget exhausted'
        elif payload.get('auto_promoted'):
            cause = 'Mirror low-confidence revision auto-promoted to escalate'
        elif payload.get('marker_type') == 'review_emergency_halt':
            cause = 'Mirror emergency halt'
        else:
            cause = 'Mirror escalated the PR to Beacon'
        return f'review_escalate: {cause}'
    return f'{event_type}: terminal failure'


def chain_event_says_failed(client: Any, task_id: str) -> Optional[str]:
    """If chain_events shows a terminal-failure event for this step, return the
    failure reason; otherwise None.

    Keys on the types the shipper ACTUALLY emits (TERMINAL_FAILURE_EVENT_TYPES:
    `preflight_reject`, `review_escalate`) and derives the reason from their real
    payload keys (marker_type/intent; verdict/marker_type/auto_promoted/
    budget_exhausted) — neither payload carries a `reason`. Returns the first
    match found. None on connect failure (failure-mode detection is best-effort;
    missing it just delays the pause-DM by one tick)."""
    if client is None or not task_id:
        return None
    try:
        res = client.table('chain_events').select(
            'event_type,payload,ts'
        ).eq('task_id', task_id).in_(
            'event_type', list(TERMINAL_FAILURE_EVENT_TYPES)
        ).limit(10).execute()
    except Exception:
        return None
    rows = getattr(res, 'data', None) or []
    if not rows:
        return None
    row = rows[0]
    return _failure_reason_from_event(
        row.get('event_type') or '', row.get('payload') or {},
    )


def gh_pr_says_merged(pr_url: str) -> Optional[bool]:
    """Return True / False / None. None means "couldn't determine" (gh
    timeout, missing auth, network) — distinct from False ("PR is OPEN /
    CLOSED but not MERGED"). The gate treats None as a soft fail (waits
    rather than counting as one-leg mismatch)."""
    if not pr_url:
        return None
    # Shared kernel (task_terminal_state.gh_json): bounded `gh`, None on any
    # error. Behavior is unchanged from the prior inline implementation — a gh
    # failure stays a soft None; only a positive MERGED state returns True.
    data = tts.gh_json(
        ['gh', 'pr', 'view', pr_url, '--json', 'state'],
        timeout=GH_PR_VIEW_TIMEOUT_SEC,
    )
    if data is None:
        return None
    return isinstance(data, dict) and data.get('state') == 'MERGED'


def _first_gate_mismatch_ts(
    audit_log: list[dict[str, Any]], step_id: str,
) -> Optional[datetime]:
    """Find the earliest gate-mismatch event for this step that has no
    subsequent `gate-clear`, `step-merged`, or `step-dispatched` event.

    Returns None if there is no open mismatch (a `gate-clear` or
    `step-merged` since the latest mismatch resets the clock; a fresh
    `step-dispatched` does the same because dispatch resets the gate-
    waiting window).

    Used by the 30-min mismatch-timeout check (spec § 5.3): on each tick,
    if a mismatch is observed AND the first observation is >30 min old,
    pause + DM."""
    # Walk forward and track the open-mismatch start.
    open_start: Optional[datetime] = None
    for entry in audit_log:
        if entry.get('step_id') != step_id:
            continue
        event = entry.get('event')
        ts_str = entry.get('ts')
        ts: Optional[datetime] = None
        if isinstance(ts_str, str):
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        if event == 'gate-mismatch':
            if open_start is None and ts is not None:
                open_start = ts
        elif event in ('gate-clear', 'step-merged', 'step-dispatched'):
            open_start = None
    return open_start


# -------------------- dispatch (advancer → Beacon's inbox) --------------------


def _build_step_envelope(
    seq: dict[str, Any], step: dict[str, Any],
) -> dict[str, Any]:
    """Construct the inbox envelope the advancer writes to Beacon's inbox
    per spec § 5.8 ("writes the next step's envelope here when advancing").

    The envelope's `prompt` is a self-contained instruction to Beacon: emit
    one standard APPROVAL_REQUEST marker for Forge whose `prompt` is the
    step's `dispatch_text` verbatim. PR-S4 will land Beacon CLAUDE.md
    coverage for this exact prompt shape (the 6 sequence shortcuts +
    target_agent: build_sequence_advancer routing); until then, Beacon's
    existing PLAN_SYNTHESIS_DISCIPLINE handles it (an APPROVAL_REQUEST is
    a regular marker, regardless of who triggered the synthesis).

    Source is `orchestrator` (already in routing_validator SYSTEM_SOURCES;
    accurately describes the actor). Topology check passes for
    orchestrator → beacon. The build-sequence-advancer source name is
    reserved for a future routing_validator.SYSTEM_SOURCES addition if
    operator observability calls for it."""
    seq_id = seq.get('seq_id', '<unknown>')
    step_id = step.get('step_id', '<unknown>')
    label = step.get('label', '')
    target_repo = step.get('target_repo', '')
    task_type = step.get('task_type', 'feature-development')
    dispatch_text = step.get('dispatch_text', '')
    spec_doc = seq.get('spec_doc', '')

    prompt = (
        f'GOAL: synthesize one standard APPROVAL_REQUEST marker for Forge '
        f'to build step `{step_id}` of build sequence `{seq_id}`.\n\n'
        f'CONTEXT: this envelope was dispatched by the '
        f'build_sequence_advancer daemon when step `{step_id}`\'s '
        f'`depends_on` dependencies all reached MERGED status. The '
        f'sequence file is at ~/agents/blackboard/build-sequences/'
        f'{seq_id}.json. Spec: '
        f'{spec_doc or "agents/beacon/specs/build-sequence-orchestrator.md"} '
        f'(advancer architecture lives in § 5.2; the sequence file schema '
        f'in § 5.1; the gate-check in § 5.3).\n\n'
        f'SEQUENCE: {seq_id}\n'
        f'STEP_ID: {step_id}\n'
        f'STEP_LABEL: {label}\n'
        f'TARGET_REPO: {target_repo}\n'
        f'TASK_TYPE: {task_type}\n\n'
        f'DISPATCH_TEXT (verbatim from sequence file step.dispatch_text — '
        f'use as the APPROVAL_REQUEST `prompt` field without modification):\n'
        f'---\n{dispatch_text}\n---\n\n'
        f'Action: render an APPROVAL_REQUEST marker via `python3 '
        f'~/agent-core/scripts/marker.py render beacon approval_request` '
        f'with target_agent=forge, prompt set to the DISPATCH_TEXT above '
        f'verbatim, task_id=`{step_id}`, target_repo=`{target_repo}`, '
        f'task_type=`{task_type}`, phase=preflight. Trust policy handles '
        f'auto-approve / require-Larry per existing rules.\n\n'
        f'After Forge\'s PR auto-merges (D3.5 5d) the chain_events row + '
        f'`gh pr view` will agree, the advancer\'s belt-and-suspenders '
        f'gate will pass, and the next step (or sequence-complete DM) '
        f'will fire on a later tick.\n\n'
        f'Preflight: this is a marker-synthesis task — emit the marker, '
        f'nothing else.'
    )
    return {
        'task_id': f'seq-{seq_id}-step-{step_id}',
        'source': 'orchestrator',
        'prompt': prompt,
        'target_repo': target_repo,
        'task_type': task_type,
        'phase': 'preflight',
        'reply_chat_id': None,
    }


def _dispatch_step(seq: dict[str, Any], step: dict[str, Any]) -> Optional[str]:
    """Write the step envelope to Beacon's inbox via safe_write_inbox.

    Returns None on success, error string on failure. Errors do not
    propagate to the caller as exceptions — the advancer logs + DMs Larry
    + leaves the step in `dispatchable` so the next tick retries."""
    envelope = _build_step_envelope(seq, step)
    filename = f'{envelope["task_id"]}.json'
    try:
        import safe_write_inbox as swi  # noqa: E402
        swi.safe_write_inbox(
            target_agent='beacon',
            task_dict=envelope,
            source_agent='orchestrator',
            filename=filename,
        )
    except Exception as e:
        return f'{type(e).__name__}: {e}'
    return None


# -------------------- per-sequence processing --------------------


def _launch_sequence_project_id(seq: dict[str, Any]) -> Optional[str]:
    """The project id a ``launch-<phase_id>`` sequence was authored for, read
    from the ``authored-by-launch-drain`` audit entry the drain wrote (it
    carries ``phase_id`` + ``project_id``). None if absent (e.g. an ordinary
    sequence, or one not authored by the launch drain). Delegates to the shared
    ``projects_store.launch_ids_from_sequence`` so the audit-entry parsing lives
    in exactly one place (the same source the done-stamp + closeout use)."""
    return projects_store.launch_ids_from_sequence(seq)[0]


def _maybe_stamp_phase_building(seq: dict[str, Any], step_id: str) -> None:
    """On dispatch of a launch sequence's step, stamp its phase ``building`` +
    pin the ``sequence_ref`` (p3f-status-writeback). A launch sequence is keyed
    ``launch-<phase_id>`` and its sole ``step_id`` IS the ``phase_id``; the
    project id comes from the drain's authoring audit entry. No-op for ordinary
    sequences and fail-safe (the writeback never raises into the tick)."""
    seq_id = seq.get('seq_id')
    if not isinstance(seq_id, str) or not seq_id.startswith('launch-'):
        return
    project_id = _launch_sequence_project_id(seq)
    if not project_id:
        logger = logging.getLogger('build_sequence_advancer')
        logger.warning(
            'launch sequence %s has no project_id in its audit log; '
            'skipping building-stamp', seq_id,
        )
        return
    try:
        psw.stamp_building(seq_id=seq_id, project_id=project_id, phase_id=step_id)
    except Exception as e:  # noqa: BLE001 — defensive; psw is already fail-safe
        logging.getLogger('build_sequence_advancer').warning(
            'building-stamp for %s/%s raised %s: %s',
            project_id, step_id, type(e).__name__, e,
        )


def _handle_unparseable_sequence(path: Path, error: str, logger: logging.Logger) -> None:
    """Per spec § 5.4 failure mode 'malformed sequence file': the daemon
    must DM Larry without crashing, and other sequences must keep
    advancing. Files that don't parse cannot be safely re-serialized as
    `status: paused`, so we just DM (cooldown-gated) + skip. Larry
    manually fixes the file or removes it."""
    logger.warning(f'unparseable sequence file {path.name}: {error}')
    _dm_larry(
        message=(
            f'Sequence file `{path.name}` could not be parsed and is '
            f'effectively paused (the advancer will not process it on '
            f'subsequent ticks until the JSON is repaired).\n\n'
            f'Error: {error}\n'
            f'Path: {path}'
        ),
        subject=f'sequence-unparseable:{path.name}',
        severity='warning',
        suggested_action=(
            f'Inspect the file: `cat {path}` — fix the JSON syntax or '
            f'`mv {path} {path}.broken` to remove it from the daemon\'s '
            f'view. Other sequences continue advancing normally.'
        ),
    )


def _invalid_alert_sig(errors: list[str]) -> str:
    """Stable, order-independent fingerprint of a validation-error set. Two
    ticks that surface the SAME problems hash identically; a genuinely
    different validation failure hashes differently (→ one fresh DM)."""
    return hashlib.sha256('\n'.join(sorted(errors)).encode('utf-8')).hexdigest()


def _safe_seq_key(seq_id: str) -> str:
    """Filesystem-safe form of a seq_id for use as a state-file name. Mirrors
    larry_alerts._safe_key: map every disallowed char to `_`, and append a
    short stable hash whenever sanitization changed the string OR it is
    over-length. This keeps a `/`-bearing id from escaping the dir and keeps
    the mapping injective (distinct ids can't collide onto one file)."""
    safe = ''.join(c if (c.isalnum() or c in '-._') else '_' for c in seq_id)
    if safe == seq_id and len(safe) <= 200:
        return safe
    digest = hashlib.sha1(seq_id.encode('utf-8')).hexdigest()[:10]
    if len(safe) > 200:
        safe = safe[:200]
    return f'{safe}.{digest}'


def _invalid_sig_path(seq_id: str) -> Path:
    return INVALID_ALERT_SIG_DIR / f'{_safe_seq_key(seq_id)}.json'


def _read_invalid_alert_sig(seq_id: str) -> Optional[str]:
    """Return the last-alerted error signature for `seq_id`, or None if none
    stored / unreadable. Fail-safe: a missing or corrupt file reads as None
    (→ we re-DM once and rewrite), never raises."""
    try:
        data = json.loads(_invalid_sig_path(seq_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    sig = data.get('sig') if isinstance(data, dict) else None
    return sig if isinstance(sig, str) else None


def _write_invalid_alert_sig(seq_id: str, sig: str) -> None:
    """Persist the just-alerted error signature atomically (tmp + os.replace).
    Raises OSError on durable write failure (caller logs + tolerates: a lost
    write just means one extra DM next tick, the safe direction)."""
    path = _invalid_sig_path(seq_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(
                {'sig': sig, 'ts': datetime.now(timezone.utc).isoformat()}, f,
            )
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _handle_invalid_sequence(
    path: Path, seq: dict[str, Any], errors: list[str], logger: logging.Logger,
) -> None:
    """Per spec § 5.4: schema-invalid (but parseable) sequence files get
    paused via atomic rewrite + DM. Different from unparseable in that we
    CAN safely set status: paused + append an audit_log entry — the
    underlying dict deserialized cleanly, only the contents are wrong."""
    seq_id = seq.get('seq_id', path.stem)
    logger.warning(f'invalid sequence {seq_id}: {errors}')
    # Don't downgrade a sequence that's already paused / failed / archived.
    current_status = seq.get('status')
    if current_status in ('paused', 'failed', 'archived'):
        # Already off the active path — make NO state change. But this handler
        # runs every tick for every sequence file, so a deliberately-parked,
        # schema-invalid sequence would re-DM Larry every WARNING cooldown
        # window (60 min) forever — an expected-by-design, non-actionable
        # alert. Suppress the repeat: DM only when the error signature is new
        # (first observation) or has genuinely changed; otherwise log + return.
        sig = _invalid_alert_sig(errors)
        if _read_invalid_alert_sig(seq_id) == sig:
            logger.info(
                f'invalid sequence {seq_id} still in status {current_status} '
                f'with unchanged error signature; suppressing repeat DM'
            )
            return
        _dm_larry(
            message=(
                f'Sequence `{seq_id}` failed schema validation but is '
                f'already in status `{current_status}`. No state change.\n\n'
                f'Validation errors:\n'
                + '\n'.join(f'  - {e}' for e in errors)
            ),
            subject=f'sequence-invalid:{seq_id}',
            severity='warning',
        )
        try:
            _write_invalid_alert_sig(seq_id, sig)
        except OSError as e:
            logger.warning(
                f'could not persist invalid-alert signature for {seq_id}: {e}'
            )
        return
    # Active / pending / complete → pause it. (complete is unusual; would
    # only happen if someone hand-edited a finalized file into invalidity.)
    new_seq = dict(seq)
    new_seq['status'] = 'paused'
    audit = list(new_seq.get('audit_log') or [])
    audit.append(_audit_entry(
        'sequence-paused-invalid', reason='schema_validation_failed',
        errors=errors[:5],  # cap to keep audit_log readable
    ))
    new_seq['audit_log'] = audit
    try:
        _atomic_write_sequence(path, new_seq)
    except OSError as e:
        logger.error(f'could not atomic-write paused state for {seq_id}: {e}')
        _dm_larry(
            message=(
                f'Sequence `{seq_id}` is schema-invalid AND the advancer '
                f'could not write the paused-state back to disk.\n\n'
                f'Validation errors:\n'
                + '\n'.join(f'  - {e2}' for e2 in errors)
                + f'\n\nWrite error: {e}'
            ),
            subject=f'sequence-invalid-write-failed:{seq_id}',
            severity='critical',
        )
        return
    _dm_larry(
        message=(
            f'Sequence `{seq_id}` paused because schema validation '
            f'failed. The daemon never advances invalid sequences.\n\n'
            f'Validation errors:\n'
            + '\n'.join(f'  - {e}' for e in errors)
            + f'\n\nAfter you fix the file, set "status": "active" again '
              f'and the next tick will resume processing.'
        ),
        subject=f'sequence-invalid:{seq_id}',
        severity='warning',
        suggested_action=f'Edit ~/agents/blackboard/build-sequences/{path.name}',
    )


def _check_in_flight_step(
    seq: dict[str, Any], step_id: str, supabase_client: Any,
    now: datetime, logger: logging.Logger,
) -> dict[str, Any]:
    """Compute the new state of one in-flight step. Returns a dict with:
        - 'transition': one of {'merged', 'failed', 'mismatch', 'mismatch_timeout', 'wait'}
        - 'reason': human-readable reason (failed / mismatch_timeout cases)
        - 'audit_events': list of audit_log entries to append (may be empty)

    Pure computation: no I/O side effects beyond the supabase + gh queries.
    The caller mutates the sequence dict + writes it atomically."""
    audit_events: list[dict[str, Any]] = []
    step = next(
        (s for s in seq.get('steps', []) if s.get('step_id') == step_id), None
    )
    if step is None:
        return {'transition': 'wait', 'reason': 'step missing', 'audit_events': []}
    pr_url = step.get('pr_url')
    task_id = f'seq-{seq.get("seq_id")}-step-{step_id}'
    dispatched_at = step.get('dispatched_at')

    # Failure-mode check first (per spec § 5.4 failure mode 1).
    failure_reason = chain_event_says_failed(supabase_client, task_id)
    if failure_reason:
        audit_events.append(_audit_entry(
            'step-failed', step_id=step_id, reason=failure_reason,
        ))
        return {
            'transition': 'failed', 'reason': failure_reason,
            'audit_events': audit_events,
        }

    chain_merged = chain_event_says_merged(
        supabase_client, task_id, dispatched_at,
    )
    gh_merged = gh_pr_says_merged(pr_url) if pr_url else None
    # The two gate signals must BOTH agree to advance (spec § 5.3).
    if chain_merged and gh_merged is True:
        audit_events.append(_audit_entry(
            'step-merged', step_id=step_id,
            pr_url=pr_url, task_id=task_id,
        ))
        return {
            'transition': 'merged', 'reason': 'both gates passed',
            'audit_events': audit_events,
        }
    # No gate signals at all → step is still being built / reviewed.
    if not chain_merged and gh_merged is not True:
        # If we had previously logged a gate-mismatch and now both signals
        # are clear (neither says merged), append gate-clear so the
        # mismatch clock resets.
        if _first_gate_mismatch_ts(seq.get('audit_log') or [], step_id) is not None:
            audit_events.append(_audit_entry(
                'gate-clear', step_id=step_id,
                reason='both gates report unmerged',
            ))
        return {
            'transition': 'wait', 'reason': 'neither gate confirms merged',
            'audit_events': audit_events,
        }
    # Exactly one signal → mismatch. Check the age.
    audit_log = list(seq.get('audit_log') or [])
    first_mismatch = _first_gate_mismatch_ts(audit_log, step_id)
    if first_mismatch is None:
        # First observation of mismatch — log it.
        audit_events.append(_audit_entry(
            'gate-mismatch', step_id=step_id,
            chain_merged=chain_merged,
            gh_merged=gh_merged,
        ))
        return {
            'transition': 'mismatch',
            'reason': f'chain_merged={chain_merged} gh_merged={gh_merged}',
            'audit_events': audit_events,
        }
    # Mismatch persists. Has it exceeded the timeout?
    age_sec = (now - first_mismatch).total_seconds()
    if age_sec >= GATE_MISMATCH_TIMEOUT_SEC:
        # gh is the authoritative merge source: the step's own recorded
        # `pr_url` showing MERGED means *that exact PR* merged (the URL was
        # captured for this task at pr-open time). So when the only laggard is
        # our chain_events log — gh_merged is True but chain_merged is still
        # False past the tolerance window — believe gh and COMPLETE, rather
        # than pausing + paging Larry for a self-resolving bookkeeping lag (the
        # recurring chain_events ingestion-lag class; observed on
        # `pulse-check-xii`, 2026-07-08). The belt-and-suspenders fast path is
        # unchanged: a normal merge still completes immediately when both gates
        # agree (above); this only changes the >30-min stalemate's resolution.
        # The opposite, genuinely-ambiguous direction (chain says merged but gh
        # does NOT confirm — e.g. closed-unmerged, or a chain false positive)
        # still pauses for manual verification.
        if gh_merged is True:
            audit_events.append(_audit_entry(
                'step-merged', step_id=step_id,
                pr_url=pr_url, task_id=task_id,
                gate_resolution='gh-authoritative',
                note=(
                    f'chain_events did not confirm merge within '
                    f'{GATE_MISMATCH_TIMEOUT_SEC}s (chain_merged=False); '
                    f'gh pr view=MERGED is authoritative — completing instead '
                    f'of pausing.'
                ),
            ))
            return {
                'transition': 'merged',
                'reason': (
                    f'gh_merged=True authoritative; chain_events lagged past '
                    f'{GATE_MISMATCH_TIMEOUT_SEC}s (chain_merged=False)'
                ),
                'audit_events': audit_events,
            }
        audit_events.append(_audit_entry(
            'gate-mismatch-timeout', step_id=step_id,
            age_sec=int(age_sec),
            chain_merged=chain_merged,
            gh_merged=gh_merged,
        ))
        return {
            'transition': 'mismatch_timeout',
            'reason': (
                f'chain_merged={chain_merged} gh_merged={gh_merged} '
                f'mismatch_age={int(age_sec)}s exceeds '
                f'{GATE_MISMATCH_TIMEOUT_SEC}s'
            ),
            'audit_events': audit_events,
        }
    # Mismatch still inside tolerance window; wait.
    return {
        'transition': 'mismatch',
        'reason': f'mismatch_age={int(age_sec)}s within tolerance',
        'audit_events': [],
    }


def _process_active_sequence(
    path: Path, seq: dict[str, Any], supabase_client: Any,
    now: datetime, logger: logging.Logger,
) -> None:
    """Advance one active sequence by one tick. Mutates `seq` in place
    and writes it atomically when there are state changes."""
    seq_id = seq.get('seq_id', path.stem)
    if seq.get('status') != 'active':
        return
    audit_log = list(seq.get('audit_log') or [])
    current_steps = list(seq.get('current_steps') or [])
    steps = seq.get('steps') or []
    state_changed = False

    # ---- 1. Check each in-flight step's gate status. ----
    new_current_steps: list[str] = []
    paused_with_reason: Optional[str] = None
    for step_id in current_steps:
        result = _check_in_flight_step(
            seq, step_id, supabase_client, now, logger,
        )
        audit_log.extend(result['audit_events'])
        if result['audit_events']:
            state_changed = True
        transition = result['transition']
        if transition == 'merged':
            step = next(
                (s for s in steps if s.get('step_id') == step_id), None
            )
            if step is not None:
                step['status'] = 'merged'
                step['merged_at'] = now.isoformat()
                step['current_actor'] = None
                state_changed = True
        elif transition == 'failed':
            step = next(
                (s for s in steps if s.get('step_id') == step_id), None
            )
            if step is not None:
                step['status'] = 'failed'
                step['failure_reason'] = result.get('reason', '')
                step['current_actor'] = None
                state_changed = True
            paused_with_reason = (
                f'Step `{step_id}` failed: {result.get("reason", "?")}'
            )
            new_current_steps.append(step_id)  # keep in current_steps for visibility
        elif transition == 'mismatch_timeout':
            paused_with_reason = (
                f'Step `{step_id}` gate-mismatch persisted >30 min: '
                f'{result.get("reason", "?")}'
            )
            new_current_steps.append(step_id)
        else:
            # 'wait' or 'mismatch' (within tolerance) → keep in current_steps.
            new_current_steps.append(step_id)

    # ---- 2. Dispatch any newly-ready pending steps. ----
    if paused_with_reason is None:
        merged_ids = {
            s.get('step_id') for s in steps
            if s.get('status') == 'merged' and isinstance(s.get('step_id'), str)
        }
        for step in steps:
            if step.get('status') != 'pending':
                continue
            deps = step.get('depends_on') or []
            if not all(d in merged_ids for d in deps):
                continue
            step_id = step.get('step_id')
            if not isinstance(step_id, str):
                continue
            # Dependencies met — dispatch.
            step['status'] = 'dispatchable'
            err = _dispatch_step(seq, step)
            if err is None:
                step['status'] = 'dispatched'
                step['dispatched_at'] = now.isoformat()
                step['current_actor'] = 'forge'
                new_current_steps.append(step_id)
                audit_log.append(_audit_entry(
                    'step-dispatched', step_id=step_id,
                ))
                # p3f-status-writeback: a launch-<phase_id> sequence dispatching
                # its step IS the phase's launch dispatching → stamp the phase
                # `building` + pin its sequence_ref (event-driven, idempotent,
                # non-committer). No-op for ordinary (non-launch) sequences.
                _maybe_stamp_phase_building(seq, step_id)
            else:
                # Dispatch failure — log + DM + RESET step to 'pending' so
                # the next tick's pending-only filter re-enters this branch
                # and retries. Leaving the step in 'dispatchable' would
                # silently strand it (the dispatch loop at line 756 only
                # iterates pending steps; the gate-check loop above only
                # iterates current_steps; nothing else picks 'dispatchable'
                # back up). Per spec § 5.4: never crash; pause + DM for
                # unrecoverable; here we treat as transient and retry.
                logger.error(
                    f'dispatch failed for {seq_id}/{step_id}: {err}'
                )
                step['status'] = 'pending'
                audit_log.append(_audit_entry(
                    'step-dispatch-failed', step_id=step_id, error=err,
                ))
                _dm_larry(
                    message=(
                        f'Sequence `{seq_id}` step `{step_id}` failed to '
                        f'dispatch to Beacon\'s inbox: {err}\n\n'
                        f'The advancer will retry on the next tick. If this '
                        f'persists, check beacon\'s inbox permissions and '
                        f'the routing_validator allowlist.'
                    ),
                    subject=f'sequence-dispatch-failed:{seq_id}:{step_id}',
                    severity='warning',
                )
            state_changed = True

    # ---- 3. Sequence-level transitions: complete / paused. ----
    if paused_with_reason is not None:
        seq['status'] = 'paused'
        audit_log.append(_audit_entry(
            'sequence-paused', reason=paused_with_reason,
        ))
        _dm_larry(
            message=(
                f'Sequence `{seq_id}` paused.\n\n{paused_with_reason}\n\n'
                f'Recovery shortcuts (PR-S4): `resume sequence {seq_id}` '
                f'/ `cancel sequence {seq_id}` / `retry sequence {seq_id} '
                f'step <step-id>`. Until PR-S4 ships, edit the sequence '
                f'file directly (set status back to active after fixing '
                f'the underlying issue).'
            ),
            subject=f'sequence-paused:{seq_id}',
            severity='warning',
        )
        state_changed = True
    elif (
        not new_current_steps
        and all(s.get('status') == 'merged' for s in steps)
    ):
        # All done.
        seq['status'] = 'complete'
        audit_log.append(_audit_entry('sequence-complete'))
        _dm_larry(
            message=(
                f'Sequence `{seq_id}` complete — all '
                f'{len(steps)} step(s) merged.'
            ),
            subject=f'sequence-complete:{seq_id}',
            severity='warning',  # `warning` is the existing low-severity bucket
        )
        state_changed = True

    # ---- 4. Persist if anything changed. ----
    if state_changed:
        seq['current_steps'] = new_current_steps
        seq['audit_log'] = audit_log
        try:
            _atomic_write_sequence(path, seq)
        except OSError as e:
            logger.error(f'atomic write failed for {seq_id}: {e}')
            _dm_larry(
                message=(
                    f'Sequence `{seq_id}` advanced in memory but the atomic '
                    f'write to {path} failed: {e}. The in-memory transitions '
                    f'will be lost; the next tick will re-process from the '
                    f'on-disk state.'
                ),
                subject=f'sequence-write-failed:{seq_id}',
                severity='critical',
            )


# -------------------- active reconciliation (V6 silent-miss backstop) --------------------


def _qualify_repo(repo: str) -> str:
    """Return `repo` in the OWNER/REPO form `gh --repo` requires.

    Sequence steps store a bare repo name (`ourliberty-agent-core`); gh rejects
    that with rc=1 before any network call. Delegates to the SINGLE shared
    resolver `sequence_shortcut_helpers.qualify_repo` so the owner default lives
    in one place (it reads the same `OURLIBERTY_GH_OWNER` / `Larry-Yatch` default
    as GITHUB_OWNER here). Imported lazily — same resilience posture as the
    reconcile pass's `ssh` import — but the only caller (`_gh_list_merged_prs`)
    runs INSIDE `_reconcile_dispatched_steps`, which already imported `ssh`
    successfully, so the import is warm and cannot fail at this point. The local
    fallback keeps a direct unit call (`bsa._qualify_repo(...)`) correct even if
    ssh were somehow unimportable."""
    try:
        from sequence_shortcut_helpers import qualify_repo
        return qualify_repo(repo)
    except Exception:
        if not repo or '/' in repo:
            return repo
        return f'{GITHUB_OWNER}/{repo}'


def _gh_list_merged_prs(
    repo: str, logger: Optional[logging.Logger] = None,
    state: str = 'merged',
) -> Optional[list[dict[str, Any]]]:
    """Return the list of recent PRs for `repo` in `state` (default
    'merged' — the reconcile pass's horizon), or None on failure (gh
    missing, auth missing, timeout, non-zero rc, bad JSON).

    `state` lets the stall-escalation pre-check ask for 'open' PRs with the
    same timeout / None-vs-[] / WARN-on-cause contract; existing callers
    omit it and keep the merged-PR reconcile behavior unchanged.

    On any failure, log a WARNING that includes the concrete reason
    (returncode + truncated stderr, or the exception) so a self-heal that
    can't query is diagnosable from the log rather than silently-ish
    skipping — three stranded incidents traced back to the bare
    'gh pr list failed' line that swallowed the underlying cause.

    None vs []: None means "couldn't query" (caller should skip its
    self-heal for this repo this tick); [] means "queried OK, no PRs in
    this state in the lookback window." The two cases differ in whether
    we'd consider this a soft failure or a confident no-match."""
    if not repo:
        return None
    qualified = _qualify_repo(repo)

    def _warn(detail: str) -> None:
        if logger is not None:
            logger.warning(
                f'gh pr list --state {state} failed for repo={qualified} '
                f'({detail}); skipping the dependent self-heal for steps in '
                f'this repo this tick'
            )

    try:
        proc = subprocess.run(
            [
                'gh', 'pr', 'list', '--repo', qualified,
                '--state', state, '--limit', str(RECONCILE_PR_LOOKBACK),
                '--json', 'number,url,title,headRefName,mergedAt',
            ],
            capture_output=True, text=True, timeout=GH_PR_LIST_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        _warn(f'timed out after {GH_PR_LIST_TIMEOUT_SEC}s')
        return None
    except (FileNotFoundError, OSError) as e:
        _warn(f'{type(e).__name__}: {e}')
        return None
    if proc.returncode != 0:
        stderr = ' '.join((proc.stderr or '').split())
        if len(stderr) > 500:
            stderr = stderr[:500] + '…'
        _warn(f'rc={proc.returncode} stderr={stderr!r}')
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        _warn(f'unparseable JSON: {e}')
        return None
    if not isinstance(data, list):
        _warn(f'unexpected JSON shape: {type(data).__name__}, expected list')
        return None
    return data


def _match_pr_for_step(
    step_id: str, pr_url: Optional[str], merged_prs: list[dict[str, Any]],
    logger: Optional[logging.Logger] = None,
) -> Optional[dict[str, Any]]:
    """Find a merged PR that identifies as this step. Precedence:
      1. exact pr_url match (only when the step has pr_url populated)
      2. headRefName == f'forge/{step_id}' (Forge worktree convention)
      3. step_id appears as a whole, boundary-delimited token in BOTH the
         PR title AND the PR headRefName (branch) — corroboration.

    Tier-3 corroboration (audit #9): the original tier-3 was a bare,
    unanchored `step_id in title` substring test. With no token boundary
    a short/common step_id ('api', 'auth', 'rotation') false-matched an
    unrelated merged PR (e.g. step_id='api' matching a PR titled
    'feat(api): unrelated change') and advanced the wrong sequence step.

    The shared id-match-discipline helper (id_match.id_matches) adds the
    missing boundary check, but its DEFAULT length floor (12) cannot be
    applied here: real step_ids ARE genuinely short ('a', 'b', 'api',
    'step-1'), so a floor would disable this tier entirely — including the
    designed derivative-branch case (a rebase/rescue/revision dispatch
    that auto-merges under a branch like `pr100-rebase-step-bar-001`, no
    pr_url, whose title contains the step_id). So instead of a length
    floor we require corroboration: the step_id must appear as a boundary-
    delimited token in BOTH the title AND the branch. In the legitimate
    derivative case the branch carries the step_id; in the audit's false
    case the unrelated PR's branch does not. Corroboration across two
    independent fields supplies the distinctiveness a length floor would,
    so we pass min_len=1 (boundary check only, no floor).

    A non-match fails safe — the step stays in `dispatched` for the belt-
    and-suspenders gate or a later tick / manual unstick — so tightening
    this tier can only ever err toward NOT advancing, never toward a
    silent wrong-advance.

    Returns the first match found at the highest-precedence tier, or
    None if no PR matches by any signal."""
    if pr_url:
        for pr in merged_prs:
            if pr.get('url') == pr_url:
                return pr
    expected_branch = f'forge/{step_id}'
    for pr in merged_prs:
        if pr.get('headRefName') == expected_branch:
            return pr
    # Remember the FIRST title-only near-miss (token in title, not in branch
    # — the audit #9 false-positive shape) but keep scanning: a later PR may
    # still corroborate. Only emit the WARN below if we fall through without
    # a match, so the log reflects an actual non-advance rather than firing
    # once per candidate PR (and never contradicting a same-tick advance).
    title_near_miss: Optional[tuple[str, str]] = None
    for pr in merged_prs:
        title = pr.get('title') or ''
        branch = pr.get('headRefName') or ''
        if not id_matches(step_id, title, min_len=1):
            continue
        if id_matches(step_id, branch, min_len=1):
            return pr
        if title_near_miss is None:
            title_near_miss = (title, branch)
    # No corroborated match. Surface a single diagnosable WARN if some PR
    # title token-matched but no branch corroborated — advancer changes are
    # flagged high-blast-radius in the remediation plan, so a legitimate
    # match this guard drops must stay visible in the log.
    if title_near_miss is not None and logger is not None:
        near_title, near_branch = title_near_miss
        logger.warning(
            f'reconcile: step_id={step_id!r} token-matched merged PR '
            f'title {near_title!r} but branch {near_branch!r} does not '
            f'corroborate; NOT advancing (audit #9 corroboration guard)'
        )
    return None


# -------------------- stranded-dispatch escalation (silent-wedge backstop) --------------------


def _valid_target_repos() -> frozenset[str]:
    """Buildable repo names from ``config/agent-models.json`` ``repo_paths`` —
    the same canonical block routing/dispatch validate against (config lives at
    ``<scripts>/../config/agent-models.json``).

    Best-effort: any read/parse error returns an EMPTY set, which the escalation
    pass treats as "can't validate repos this tick" and SKIPS the invalid-repo
    check (fail OPEN — never pause a sequence just because the config was briefly
    unreadable). A populated set enables the check."""
    cfg_path = _SCRIPTS_DIR.parent / 'config' / 'agent-models.json'
    try:
        data = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return frozenset()
    block = data.get('repo_paths') if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return frozenset()
    return frozenset(k for k in block if isinstance(k, str) and k)


def _dispatched_age_sec(dispatched_at: Any, now: datetime) -> Optional[float]:
    """Seconds since ``dispatched_at`` (ISO-8601), or None if it's absent /
    unparseable (caller then can't apply the stall timeout — fail safe)."""
    if not isinstance(dispatched_at, str) or not dispatched_at:
        return None
    # Normalize a trailing 'Z' to '+00:00' before fromisoformat: our own
    # producers stamp '+00:00', but 'Z'-suffixed timestamps circulate
    # elsewhere in the codebase and fromisoformat rejects 'Z' on Python <3.11
    # — without this a 'Z' step would silently skip the stall backstop.
    try:
        ts = datetime.fromisoformat(dispatched_at.replace('Z', '+00:00'))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds()


def _escalate_stranded_dispatched_steps(
    sequence: dict[str, Any], path: Path, now: datetime,
    logger: logging.Logger, valid_repos: frozenset[str],
    stall_enabled: bool = True, supabase_client: Any = None,
) -> bool:
    """Backstop for a `dispatched` step that can NEVER reach a terminal gate —
    the silent-wedge class the V6 reconcile pass cannot catch (it only RETIRES
    merged work via `gh pr list --state merged`; a step that produces no PR is
    invisible to it, and the gh call for an invalid repo just fails → None →
    no-op every tick). Two triggers, checked per still-`dispatched` step:

      1. INVALID TARGET_REPO — `target_repo` is not a buildable repo in
         `config/agent-models.json` `repo_paths`. Forge can't act on it, so the
         step would sit `dispatched` forever (the 2026-06-19 `ol-work` launch
         hung ~6h with no failure + no alert). Caught IMMEDIATELY — a bad repo
         never gets better — and FLAG-INDEPENDENT (a config error is wrong
         regardless of whether forward-dispatch is enabled).
      2. STALL — a valid-repo step `dispatched` longer than
         DISPATCH_STALL_TIMEOUT_SEC with no `pr_url` recorded. A generous
         wall-clock backstop for any OTHER cause that strands a dispatch (Forge
         never picked it up, a lost inbox envelope, ...). Gated on
         `stall_enabled` (the advancer's forward-dispatch flag): while
         forward-dispatch is intentionally OFF a step waiting is expected, not
         stalled, so the timing backstop only applies when dispatch is live.

    On the FIRST stranded step found: mark it `failed` (+ `failure_reason`),
    pause the SEQUENCE, append audit, atomic-write, and DM Larry (subject-keyed
    cooldown). Returns True iff it escalated (caller re-reads + skips
    forward-dispatch). Runs flag-INDEPENDENT (like the reconcile pass) so a
    wedge escalates even when forward-dispatch is gated off. Only the caller's
    `active`-status gate prevents re-firing on an already-paused sequence."""
    steps = sequence.get('steps') or []
    seq_id = sequence.get('seq_id', path.stem)
    for step in steps:
        if step.get('status') != 'dispatched':
            continue
        step_id = step.get('step_id')
        if not isinstance(step_id, str):
            continue
        target_repo = step.get('target_repo')
        reason: Optional[str] = None
        severity = 'warning'
        if valid_repos and (
            not isinstance(target_repo, str) or target_repo not in valid_repos
        ):
            reason = (
                f'target_repo {target_repo!r} is not a buildable repo '
                f'(config/agent-models.json repo_paths: {sorted(valid_repos)}). '
                f'Forge can never act on it, so the step would sit `dispatched` '
                f'indefinitely. Fix the phase/project repo and re-launch.'
            )
            severity = 'critical'
        elif stall_enabled:
            age = _dispatched_age_sec(step.get('dispatched_at'), now)
            if (
                age is not None
                and age >= DISPATCH_STALL_TIMEOUT_SEC
                and not step.get('pr_url')
            ):
                # Before stranding: a build that finished and opened a PR may
                # simply not have had its `pr_url` recorded yet (the merge-gate
                # / notifier hasn't advanced the step). Such a step is in review,
                # NOT stranded. The reconcile pass only ever queries `--state
                # merged`, so an OPEN-not-yet-merged PR is invisible to every
                # other recovery path — query for it here.
                open_prs = _gh_list_merged_prs(target_repo, logger, state='open')
                if open_prs is None:
                    # FAIL-SAFE: a transient gh outage must not resurrect the
                    # exact false-strand this guard removes. Defer escalation one
                    # tick — a genuinely-stranded step (truly no PR) isn't going
                    # anywhere and will alert next tick when gh is reachable.
                    logger.warning(
                        f'escalate: sequence={seq_id} step={step_id} past the '
                        f'{DISPATCH_STALL_TIMEOUT_SEC // 3600}h stall backstop, '
                        f'but the open-PR pre-check query failed this tick '
                        f'(cause logged above); deferring escalation, will retry '
                        f'next tick.'
                    )
                    continue
                open_match = _match_pr_for_step(
                    step_id, step.get('pr_url'), open_prs, logger,
                )
                if open_match is not None:
                    # The build shipped — record the PR and let the existing
                    # merge-gate / reconcile advance it on merge. Stays
                    # `dispatched` in `current_steps`; do NOT fail/pause/DM. The
                    # stall guard's `and not step.get('pr_url')` then suppresses
                    # any re-escalation on future ticks.
                    pr_url = open_match.get('url') or ''
                    step['pr_url'] = pr_url
                    audit = list(sequence.get('audit_log') or [])
                    audit.append(_audit_entry(
                        'step-pr-detected', step_id=step_id, actor='advancer',
                        pr_url=pr_url,
                    ))
                    sequence['audit_log'] = audit
                    try:
                        _atomic_write_sequence(path, sequence)
                    except OSError as e:
                        logger.error(
                            f'escalate: atomic write failed recording open PR '
                            f'for {seq_id} step={step_id}: {e}'
                        )
                    logger.info(
                        f'escalate: sequence={seq_id} step={step_id} past the '
                        f'stall backstop but has OPEN PR {pr_url}; recorded '
                        f'pr_url, NOT stranding (in review).'
                    )
                    continue
                # Before blaming "Forge never picked it up": a non-merge
                # terminal outcome (preflight_reject / review_escalate) often
                # DID occur but never propagated into sequence state — only the
                # merge path is push-wired, so every other terminal signal falls
                # through to this time-based backstop. Consult the durable
                # chain_events signal first and, when a terminal failure is
                # recorded, write the CORRECT attribution (rejected / escalated)
                # rather than the misleading stall guess.
                chain_task_id = f'seq-{seq_id}-step-{step_id}'
                terminal = chain_event_says_failed(
                    supabase_client, chain_task_id,
                )
                if terminal is not None:
                    reason = (
                        f'a terminal outcome was recorded in chain_events but '
                        f'never propagated into sequence state — {terminal}. The '
                        f'step sat `dispatched` past the '
                        f'{DISPATCH_STALL_TIMEOUT_SEC // 3600}h backstop because '
                        f'the non-merge terminal signal path is not wired; this '
                        f'is a genuine failure, not a missed dispatch.'
                    )
                else:
                    reason = (
                        f'dispatched ~{int(age // 3600)}h ago with no PR and no '
                        f'gate progress (exceeds the '
                        f'{DISPATCH_STALL_TIMEOUT_SEC // 3600}h stall backstop). '
                        f'Forge may never have picked it up. If this is a '
                        f'legitimately long build, `resume sequence {seq_id}`; '
                        f'otherwise investigate the dispatch.'
                    )
        if reason is None:
            continue
        # Escalate this step + pause the sequence (mirrors the failed/pause
        # handling in _process_active_sequence, but runs flag-independent).
        step['status'] = 'failed'
        step['failure_reason'] = reason
        step['current_actor'] = None
        audit = list(sequence.get('audit_log') or [])
        audit.append(_audit_entry('step-stranded', step_id=step_id, reason=reason))
        audit.append(_audit_entry(
            'sequence-paused', reason=f'Step `{step_id}` stranded: {reason}',
        ))
        sequence['audit_log'] = audit
        sequence['status'] = 'paused'
        try:
            _atomic_write_sequence(path, sequence)
        except OSError as e:
            # Still DM below so the wedge is visible even if the write failed;
            # the next tick re-processes from the on-disk (still active) state.
            logger.error(f'escalate: atomic write failed for {seq_id}: {e}')
        logger.warning(
            f'escalate: sequence={seq_id} step={step_id} stranded; paused. {reason}'
        )
        _dm_larry(
            message=(
                f'Sequence `{seq_id}` paused — step `{step_id}` is stranded and '
                f'can never complete on its own.\n\n{reason}'
            ),
            subject=f'sequence-stranded:{seq_id}:{step_id}',
            severity=severity,
            suggested_action=(
                f'Inspect ~/agents/blackboard/build-sequences/{path.name}; fix '
                f'the step `target_repo` (or the phase/project repo), then '
                f'`resume sequence {seq_id}` / re-launch.'
            ),
        )
        return True
    return False


# -------------------- with-PR review-stall recovery (Face 2 / Fix B) --------------------


def _review_stall_already_handled(
    sequence: dict[str, Any], step_id: str, dispatched_at: Any,
) -> bool:
    """True iff this wedged `(step_id, dispatched_at)` epoch was already
    recover-or-routed — a `step-review-redispatched` or `step-review-stall-
    alerted` audit entry names the SAME step_id AND the SAME dispatched_at.

    Keyed on dispatched_at so a fresh dispatch (a `retry`, which clears
    dispatched_at then re-stamps it) re-arms, while the same wedged dispatch is
    handled ONCE per epoch — not re-dispatched/alerted every tick."""
    for entry in sequence.get('audit_log') or []:
        if not isinstance(entry, dict):
            continue
        if entry.get('event') not in (
            'step-review-redispatched', 'step-review-stall-alerted',
        ):
            continue
        if entry.get('step_id') == step_id and \
                entry.get('dispatched_at') == dispatched_at:
            return True
    return False


def _gh_pr_open(pr_url: str) -> Optional[bool]:
    """True iff gh reports the PR `OPEN`; False for MERGED/CLOSED; None if the
    state couldn't be determined (gh timeout / auth / network). Distinct from
    `gh_pr_says_merged` because the review-stall pass must tell OPEN (act) apart
    from CLOSED-unmerged (skip — terminal, not our stall)."""
    if not pr_url:
        return None
    data = tts.gh_json(
        ['gh', 'pr', 'view', pr_url, '--json', 'state'],
        timeout=GH_PR_VIEW_TIMEOUT_SEC,
    )
    if not isinstance(data, dict) or data.get('state') is None:
        return None
    return data.get('state') == 'OPEN'


def _redispatch_review_for_wedged_step(
    step_id: str, target_repo: Any, pr_url: str, pr_title: Any,
    logger: logging.Logger,
) -> bool:
    """Fix B recovery (`sequence-step-stall-recovery.md` §4): re-dispatch a
    Mirror review of the wedged PR via the notifier's canonical, presence-gated
    `_dispatch_mirror_review`. Re-priming the review from durable PR state
    reconstructs the whole review→revision loop idempotently — Mirror re-reviews
    the (still-unfixed) PR and, on findings, the notifier's MECHANICAL
    `_dispatch_revision_to_forge` applies them to the EXISTING branch
    (`forge/<step_id>`), exactly the manual #532 recovery. A step's build
    task_id IS its `step_id` (advancer envelope), so the review-request file is
    `review-<step_id>.json`. Returns True iff a review request is present
    afterward (idempotent: the notifier's own presence-gate makes a duplicate
    call a safe no-op that still counts as landed)."""
    try:
        import outbox_notifier as notifier  # noqa: E402
        import safe_write_inbox as swi  # noqa: E402
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f'review-stall recover: import failed for {step_id}: '
            f'{type(e).__name__}: {e}'
        )
        return False
    data = {
        'task_id': step_id,
        'target_repo': (target_repo or '').split('/')[-1] if isinstance(
            target_repo, str) else '',
        'branch': f'forge/{step_id}',
        'pr_title': pr_title if isinstance(pr_title, str) else '',
        'dispatched_by': 'build-sequence-advancer',
    }
    try:
        notifier._dispatch_mirror_review(data, pr_url)
    except Exception as e:  # noqa: BLE001 — inner call swallows routing/cost denials
        logger.warning(
            f'review-stall recover: dispatch raised for {step_id}: '
            f'{type(e).__name__}: {e}'
        )
    try:
        fname = swi.canonical_inbox_name(f'review-{step_id}.json')
        landed = bool(notifier._review_request_already_dispatched(fname))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f'review-stall recover: verify failed for {step_id}: '
            f'{type(e).__name__}: {e}'
        )
        return False
    logger.info(f'review-stall recover: step={step_id} landed={landed}')
    return landed


def _recover_review_stalled_steps(
    sequence: dict[str, Any], path: Path, now: datetime,
    logger: logging.Logger,
) -> bool:
    """Face 2 (completeness-pr2 §1a) — recover-or-route a WITH-PR wedged step.

    Closes the permanently-unmonitored state the no-PR stall guard leaves: a
    step that opened a PR and then wedged in review (Mirror never reviewed, or
    her REVISION dead-lettered — the #532 shape) has no timeout otherwise,
    because `_escalate_stranded_dispatched_steps` short-circuits on `and not
    step.get('pr_url')` and its pr_url-recording pre-check then suppresses any
    future escalation for that step.

    Per-step, for a `dispatched` step carrying a `pr_url` past
    REVIEW_STALL_TIMEOUT_SEC and not yet advanced:
      * epoch dedup (`_review_stall_already_handled`) → skip if already handled;
      * probe the PR from durable state (`_gh_pr_open`): only act on a live
        (OPEN) PR — MERGED/CLOSED means the review resolved out-of-band
        (merge-gate / reconcile owns it), and an unverifiable state defers one
        tick (never act on unknown, per the ~85%-false-history anti-noise
        constraint);
      * recover-then-alert: re-dispatch the review (Fix B). On success, stamp
        `step-review-redispatched` and leave the sequence ACTIVE (the loop is
        being re-primed — do NOT pause). On failure, fire exactly ONE actionable
        Larry alert and stamp `step-review-stall-alerted` so it never loops.

    Detects from durable state only (sequence file + gh), never from logs.
    Returns True iff it acted on any step (recovered or alerted)."""
    steps = sequence.get('steps') or []
    seq_id = sequence.get('seq_id', path.stem)
    acted = False
    for step in steps:
        if not isinstance(step, dict) or step.get('status') != 'dispatched':
            continue
        pr_url = step.get('pr_url')
        if not pr_url or not isinstance(pr_url, str):
            continue  # no-PR wedge → Fix A / the 4h no-PR strand guard own it
        step_id = step.get('step_id')
        if not isinstance(step_id, str) or not step_id:
            continue
        dispatched_at = step.get('dispatched_at')
        age = _dispatched_age_sec(dispatched_at, now)
        if age is None or age < REVIEW_STALL_TIMEOUT_SEC:
            continue  # healthy / recently-dispatched review — leave it alone
        if _review_stall_already_handled(sequence, step_id, dispatched_at):
            continue  # already recover-or-routed this dispatch epoch
        is_open = _gh_pr_open(pr_url)
        if is_open is None:
            logger.warning(
                f'review-stall: sequence={seq_id} step={step_id} PR {pr_url} '
                f'state unverifiable this tick (gh); deferring one tick.'
            )
            continue
        if not is_open:
            logger.info(
                f'review-stall: sequence={seq_id} step={step_id} PR {pr_url} '
                f'no longer OPEN; skipping (merge-gate / reconcile owns it).'
            )
            continue
        recovered = _redispatch_review_for_wedged_step(
            step_id, step.get('target_repo'), pr_url, step.get('label'), logger,
        )
        audit = list(sequence.get('audit_log') or [])
        if recovered:
            audit.append(_audit_entry(
                'step-review-redispatched', step_id=step_id,
                dispatched_at=dispatched_at, pr_url=pr_url,
            ))
            sequence['audit_log'] = audit
            try:
                _atomic_write_sequence(path, sequence)
            except OSError as e:
                logger.error(
                    f'review-stall: atomic write failed recording re-dispatch '
                    f'for {seq_id} step={step_id}: {e}'
                )
            logger.info(
                f'review-stall: sequence={seq_id} step={step_id} review loop '
                f'wedged past {REVIEW_STALL_TIMEOUT_SEC // 3600}h; re-dispatched '
                f'Mirror review on {pr_url} (sequence stays active).'
            )
            acted = True
            continue
        # Recovery could not fire → one actionable alert, deduped by the stamp.
        reason = (
            f'Sequence `{seq_id}` step `{step_id}` has a PR ({pr_url}) wedged in '
            f'review for over {REVIEW_STALL_TIMEOUT_SEC // 3600}h and its review '
            f'loop is dead (Mirror never reviewed, or a revision dead-lettered — '
            f'the #532 shape). Auto-recovery (re-dispatch review) could not fire.'
        )
        audit.append(_audit_entry(
            'step-review-stall-alerted', step_id=step_id,
            dispatched_at=dispatched_at, pr_url=pr_url,
        ))
        sequence['audit_log'] = audit
        try:
            _atomic_write_sequence(path, sequence)
        except OSError as e:
            logger.error(
                f'review-stall: atomic write failed recording alert for '
                f'{seq_id} step={step_id}: {e}'
            )
        logger.warning(
            f'review-stall: sequence={seq_id} step={step_id} unrecoverable; '
            f'alerting Larry. {reason}'
        )
        _dm_larry(
            message=reason,
            subject=f'sequence-review-stall:{seq_id}:{step_id}',
            severity='warning',
            suggested_action=(
                f'Review the PR {pr_url}. Re-run Mirror\'s review by hand, or '
                f'`retry sequence {seq_id} step {step_id}` to re-dispatch the '
                f'build, or close the PR if the step is obsolete.'
            ),
        )
        acted = True
    return acted


# -------------------- already-merged build bridge (no-PR strand backstop) --------------------


# The escalate pass writes this signature into a stranded step's failure_reason
# (see `_escalate_stranded_dispatched_steps`). A `failed` step carrying it (and
# no pr_url) is the 4h-stall backstop's own work, NOT a genuine build failure or
# a human cancel — so reconcile may safely re-examine it (option c). Matched as a
# stable substring of the stall reason.
_STALL_FAILURE_SIGNATURE = 'stall backstop'


def _paused_by_stall_strand(sequence: dict[str, Any]) -> bool:
    """True iff the sequence's MOST RECENT `sequence-paused` audit entry was the
    stall backstop's own (its reason names a stranded/stall-backstop strand).

    Gates the reconcile auto-resume so it only un-pauses a sequence the escalate
    pass paused for THIS strand class — never an operator's intentional `pause`
    (or a pause for any other reason), even if a stranded-then-failed step
    happens to coexist."""
    for entry in reversed(sequence.get('audit_log') or []):
        if not isinstance(entry, dict) or entry.get('event') != 'sequence-paused':
            continue
        reason = entry.get('reason')
        return isinstance(reason, str) and (
            _STALL_FAILURE_SIGNATURE in reason or 'stranded' in reason
        )
    return False


def _is_advancer_stranded_failure(step: dict[str, Any]) -> bool:
    """True iff `step` is a `failed` step the advancer's stall backstop stranded
    (it has the stall signature in `failure_reason` and never recorded a PR).

    Distinguishes the auto-escalated-strand case — whose work may have shipped
    via another path — from a genuinely-failed build (Mirror reject, build error)
    or a human cancel, neither of which reconcile should resurrect."""
    if step.get('status') != 'failed':
        return False
    if step.get('pr_url'):
        return False
    reason = step.get('failure_reason')
    return isinstance(reason, str) and _STALL_FAILURE_SIGNATURE in reason


def _latest_forge_build_result(step_id: str) -> Optional[str]:
    """Return the result text of the most-recent Forge BUILD outbox for
    `step_id` (task_id == step_id), or None.

    Reads `~/agents/outboxes/forge/.archive/<step_id>(.N).json`. The filename is
    anchored so a step_id that is a prefix of another (`foo` vs `foo-v2`) can't
    cross-match. Only a CLEAN-EXIT `phase == 'build'` envelope counts
    (exit_code == 0 — an honest no-delta refusal is a successful turn, while a
    crash exits non-zero); the newest by completed_at (else mtime) wins.
    Fail-safe — any read/parse error → None."""
    try:
        archive_dir = AGENTS_ROOT / 'outboxes' / 'forge' / '.archive'
        if not archive_dir.is_dir():
            return None
        name_re = re.compile(rf'^{re.escape(step_id)}(\.\d+)?\.json$')
        best: Optional[tuple[float, str]] = None
        for f in archive_dir.glob('*.json'):
            if f.name.startswith('.') or not name_re.match(f.name):
                continue
            try:
                env = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(env, dict) or env.get('phase') != 'build':
                continue
            if env.get('exit_code') != 0:
                continue
            ts = env.get('completed_at')
            epoch: float
            if isinstance(ts, str) and ts:
                try:
                    epoch = datetime.fromisoformat(
                        ts.replace('Z', '+00:00')
                    ).timestamp()
                except ValueError:
                    epoch = f.stat().st_mtime
            else:
                try:
                    epoch = f.stat().st_mtime
                except OSError:
                    epoch = 0.0
            result = env.get('result')
            if not isinstance(result, str):
                result = ''
            if best is None or epoch > best[0]:
                best = (epoch, result)
        return best[1] if best is not None else None
    except Exception:
        return None


def _bridge_already_merged_pr(
    ssh, step_id: str, target_repo: str,
    merged_prs: Optional[list[dict[str, Any]]],
    logger: logging.Logger,
) -> Optional[dict[str, Any]]:
    """Bridge a step to the PR its work already merged under, when the PR's
    branch/title carry no step_id token so `_match_pr_for_step` can't.

    Reads the step's Forge build outbox; if it carries an already-merged /
    no-delta outcome naming a single gh-MERGED PR, returns a merged-PR dict
    `{'url', 'mergedAt'}` (the shape `apply_step_merged` consumes). The PR ref is
    resolved by `ssh.parse_already_merged_pr_ref`, which PREFERS Forge's canonical
    structured contract line `NO PR — already merged: #<N>` (durable against
    narration rewording) and falls back to the prose cue + single-PR heuristic for
    older results. Resolves the PR via the per-tick `merged_prs` list first
    (already fetched, no extra gh call); falls back to a direct `gh pr view`
    verify when the PR is older than the recent-merged window. Fail-safe — any
    miss/error → None.

    `ssh` is the lazily-imported `sequence_shortcut_helpers` module passed from
    the reconcile loop (the advancer imports it under an importability guard, so
    it is not a module-level name here)."""
    result_text = _latest_forge_build_result(step_id)
    if not result_text:
        return None
    pr_number = ssh.parse_already_merged_pr_ref(result_text)
    if pr_number is None:
        return None
    # Prefer the already-fetched recent-merged list (confirmed MERGED, carries
    # url + mergedAt) — no second gh call in the common case.
    if merged_prs:
        for pr in merged_prs:
            if pr.get('number') == pr_number:
                logger.info(
                    f'reconcile: step_id={step_id!r} bridged to already-merged '
                    f'PR #{pr_number} via build-outbox no-delta refusal '
                    f'(recent-merged list)'
                )
                return pr
    # Older than the lookback window — verify the named PR directly.
    info = ssh.gh_pr_merge_info(ssh.qualify_repo(target_repo), pr_number)
    if info is None:
        return None
    url, merged_at = info
    logger.info(
        f'reconcile: step_id={step_id!r} bridged to already-merged PR '
        f'#{pr_number} via build-outbox no-delta refusal (direct gh verify)'
    )
    return {'url': url, 'mergedAt': merged_at, 'number': pr_number}


def _reconcile_dispatched_steps(
    sequence: dict[str, Any], logger: logging.Logger,
    _repo_cache: Optional[dict[str, Optional[list[dict[str, Any]]]]] = None,
) -> int:
    """Active reconciliation pass — backstop for the V6 notifier hook.

    The notifier's apply_step_merged call looks up "which step does this
    task_id belong to" by exact task_id match. When work spans rebase /
    rescue / revision dispatches, the auto-merge fires under a derivative
    task_id (e.g. `pr211-rebase-step-a-rotation-001` instead of
    `seq-...-step-a-rotation`); exact-match fails; the sequence step
    strands in `dispatched`. Two incidents in 36 hours (2026-05-29
    operator-ux-rollout/step-rescue-runbook; 2026-05-31
    rate-limit-resilience-001/step-a-rotation) required manual operator
    unstick.

    This reconciler runs every tick BEFORE _process_active_sequence's
    dispatch logic. For each step still in `dispatched`, it queries
    `gh pr list --state merged` for the step's target_repo and identity-
    matches via pr_url → branch → title-substring. On a confident match,
    it fires apply_step_merged (which is idempotent) and counts the
    reconciliation. Returns the count for this sequence.

    no-PR already-merged backstop (2026-06-20): two extensions close the
    strand class where a build opens NO PR because its work already merged
    via another path (`git diff main..HEAD` empty; Forge refuses to fabricate
    a PR) AND the merged PR's branch/title carry no step_id token, so the
    identity match above can't bridge it (real incident: step
    `system-self-awareness-slice-1-state-log` → PR #602):
      • when the identity match misses for a step with NO recorded pr_url,
        fall back to `_bridge_already_merged_pr` — it reads the PR Forge named
        in her honest "already merged" build-outbox refusal and gh-verifies it;
      • reconcile ALSO considers steps the 4h stall backstop already escalated
        to `failed` (no pr_url + stall signature). Healing such a step on the
        paused sequence resumes it first (when the strand is the sole blocker)
        so the now-all-merged sequence finalizes — no manual operator unstick.

    Failure mode: gh missing / timeout / network → log + return 0. The
    rest of the tick continues normally (stale-but-running > stopped).

    Args:
        sequence: parsed sequence dict (one active sequence).
        logger: shared logger for INFO on reconcile fires + WARN on gh
            failures.
        _repo_cache: optional mapping `target_repo -> list-or-None`. When
            multiple steps in the same sequence target the same repo,
            cache the gh result so we don't issue N calls. Caller passes
            a dict that survives the per-sequence pass; if omitted the
            cache is per-call (still correct, just less efficient).
    """
    if _repo_cache is None:
        _repo_cache = {}
    seq_id = sequence.get('seq_id')
    if not isinstance(seq_id, str):
        return 0
    steps = sequence.get('steps') or []
    reconciled = 0
    matched_step_ids: list[str] = []
    matched_prs_in_pass: dict[str, str] = {}  # pr_url -> step_id (collision detect)

    try:
        import sequence_shortcut_helpers as ssh  # noqa: E402
    except Exception as e:
        logger.info(
            f'reconcile: sequence_shortcut_helpers unavailable '
            f'({type(e).__name__}: {e}); skipping pass'
        )
        return 0

    for step in steps:
        status = step.get('status')
        # Reconcile steps still `dispatched`, PLUS steps the 4h stall backstop
        # already escalated to `failed` whose work may have shipped via another
        # path (option c — without this a stranded-then-failed step could never
        # auto-heal, the exact dead end the 2026-06-20 incident hit). Every other
        # status (genuine failure, merged, pending, ...) is left alone.
        is_stranded_failed = _is_advancer_stranded_failure(step)
        if status != 'dispatched' and not is_stranded_failed:
            continue
        step_id = step.get('step_id')
        if not isinstance(step_id, str):
            continue
        target_repo = step.get('target_repo')
        if not isinstance(target_repo, str) or not target_repo:
            continue
        # Per-repo lookup with per-pass cache. _gh_list_merged_prs logs the
        # concrete failure reason (rc + stderr / exception) itself.
        if target_repo not in _repo_cache:
            merged_prs = _gh_list_merged_prs(target_repo, logger)
            _repo_cache[target_repo] = merged_prs
        merged_prs = _repo_cache[target_repo]
        if merged_prs is None:
            continue  # gh failure already logged; soft-fail.
        match = _match_pr_for_step(
            step_id, step.get('pr_url'), merged_prs, logger,
        )
        # No identity match by pr_url / branch / title. When the step recorded no
        # PR at all, the work may have shipped under a branch/title that carries
        # none of the step_id token (the #602 incident: branch
        # `forge/system-self-awareness-the-standing-brain`, title with no
        # step_id). Fall back to the build-outbox no-delta bridge — it reads the
        # PR Forge named in her honest "already merged" refusal and gh-verifies
        # it. Only for steps with no recorded pr_url (a recorded one already
        # matched tier-1 above, or is a genuine non-strand).
        if match is None and not step.get('pr_url'):
            match = _bridge_already_merged_pr(
                ssh, step_id, target_repo, merged_prs, logger,
            )
        if match is None:
            continue
        pr_url = match.get('url') or ''
        merged_at = match.get('mergedAt') or datetime.now(timezone.utc).isoformat()
        # Collision check: same merged PR shouldn't claim two steps in
        # the same sequence. Flag + skip the second; the first wins.
        prior_step = matched_prs_in_pass.get(pr_url)
        if prior_step is not None:
            logger.warning(
                f'reconcile: PR {pr_url} matched multiple steps in '
                f'{seq_id}: {prior_step} (kept) and {step_id} (skipped). '
                f'Inspect step identity signals.'
            )
            continue
        matched_prs_in_pass[pr_url] = step_id
        # Healing a stranded-`failed` step: the escalate pass also PAUSED the
        # sequence, and apply_step_merged's completion rollup no-ops while paused
        # (terminal statuses are operator-overriding). Resume FIRST so a now-all-
        # merged sequence finalizes — but ONLY when (a) this strand is the sole
        # blocker (no OTHER failed step), and (b) the pause was the stall
        # backstop's OWN (never auto-resume an operator's intentional pause). A
        # genuinely-failed sibling or a non-strand pause leaves the pause for
        # Larry; we still record this step's merge.
        if (
            is_stranded_failed
            and sequence.get('status') == 'paused'
            and _paused_by_stall_strand(sequence)
        ):
            other_failed = any(
                s is not step and s.get('status') == 'failed' for s in steps
            )
            if not other_failed:
                resume = ssh.apply_resume(seq_id, actor='advancer-reconcile')
                if getattr(resume, 'error', False):
                    logger.warning(
                        f'reconcile: could not resume paused sequence {seq_id} '
                        f'to heal stranded step {step_id}: {resume.reason}'
                    )
                else:
                    sequence['status'] = 'active'  # keep in-memory consistent
                    logger.info(
                        f'reconcile: resumed paused sequence {seq_id} to heal '
                        f'stranded-then-failed step {step_id} (work shipped via '
                        f'{pr_url})'
                    )
        try:
            result = ssh.apply_step_merged(
                seq_id=seq_id,
                step_id=step_id,
                pr_url=pr_url,
                merged_at=merged_at,
                actor='advancer-reconcile',
            )
        except Exception as e:
            logger.error(
                f'reconcile: apply_step_merged raised for '
                f'{seq_id}/{step_id}: {type(e).__name__}: {e}'
            )
            continue
        if getattr(result, 'applied', False):
            reconciled += 1
            matched_step_ids.append(step_id)
            # Keep the in-memory copy consistent so a later iteration's
            # `other_failed` / status checks in this same pass don't read stale
            # state (the on-disk file already reflects the merge).
            step['status'] = 'merged'
            step['pr_url'] = pr_url

    if reconciled > 0:
        logger.info(
            f'reconcile: sequence={seq_id} reconciled_steps={reconciled} '
            f'step_ids={matched_step_ids}'
        )
    return reconciled


# -------------------- pre-dispatch already-merged backstop (#609 follow-up) --------------------
#
# WHY (the 2026-06-20 incident, deeper fix). The board Launch of
# `system-self-awareness-slice-1-state-log` authored a build whose deliverables a
# SIBLING project's Forge build had ALREADY shipped (PR #602). The build still
# dispatched, found nothing to do (byte-identical to main), and stranded 4h /
# ~$1.5 before #610's post-build no-delta bridge reconciled it. PR #609 added a
# CHEAP author-time guard in the launch drain, but the drain is a pure-filesystem
# non-committer — it can only ADVISE/reversibly-hold. The REAL fix — "a recently-
# merged PR already delivered this phase → skip the build AND reconcile the phase
# straight to done" — belongs HERE in the advancer, which already shells to gh and
# writes the projects store. This pass catches the redundant build BEFORE it
# dispatches (the drain's hold may miss the window between author and dispatch),
# saving the whole Forge run rather than reconciling it after the fact.


def _pr_merged_recently(
    pr: dict[str, Any], now: datetime, max_age_sec: int,
) -> bool:
    """True iff ``pr['mergedAt']`` (ISO-8601, possibly 'Z'-suffixed) is within
    ``max_age_sec`` of ``now``. A missing / unparseable ``mergedAt`` returns
    False — we cannot confirm recency, so the conservative outcome is NOT a match
    (fail toward dispatching the build, never toward a false done-stamp)."""
    raw = pr.get('mergedAt')
    if not isinstance(raw, str) or not raw:
        return False
    try:
        ts = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return 0 <= (now - ts).total_seconds() <= max_age_sec


def _already_merged_launch_match(
    step_id: str,
    seq_id: str,
    merged_prs: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    now: datetime,
    max_age_sec: int,
) -> Optional[dict[str, Any]]:
    """Find the gh-MERGED PR that already delivered this launch phase's work, or
    None. CONSERVATIVE by construction — only two confident, exact signals, never
    a title-substring guess, and only against a RECENTLY-merged PR (so a phase is
    never marked done wrongly):

      1. OWN CONVENTION BRANCH — a merged PR whose ``headRefName`` is exactly
         ``forge/<step_id>`` or ``forge/<seq_id>`` (the phase's own build branch;
         a re-run / parallel dispatch of THIS phase that merged already).

      2. CLAIM + SIBLING-BRANCH CORROBORATION — a #609 deliverable claim names
         this phase (``claimed_task_id`` ∈ this phase's id forms) AND a merged PR
         corroborates it via the SIBLING's convention branch
         ``forge/<envelope_task_id>`` (the cross-identity case: the sibling built
         under its OWN envelope task_id but its marker claimed THIS phase's id —
         the exact 2026-06-20 shape, where #602 merged under
         ``forge/system-self-awareness-the-standing-brain``).

    RECENCY GATE (``_pr_merged_recently``): a launch step sits ``pending`` only
    minutes before dispatch (the advancer ticks every 5 min) and a genuine
    duplicate's PR merges around that same window, so a PR merged LONG ago is not
    evidence that a freshly-authored launch is redundant. Without it a stale,
    unrelated ``forge/<step_id>`` branch still inside the count-bounded (not
    date-bounded) lookback list would falsely mark a legitimate RE-launch done.
    ``max_age_sec`` is generous (7d) so a real recent duplicate is never rejected.

    The caller supplies ``claims`` already filtered to this phase
    (``launch_dedup_guard.find_matching_claims``), so tier 2 only needs the
    branch corroboration. Returns the first matching PR dict (carries ``url`` +
    ``mergedAt``)."""
    own_branches = {f'forge/{step_id}', f'forge/{seq_id}'}
    for pr in merged_prs:
        if (pr.get('headRefName') in own_branches
                and _pr_merged_recently(pr, now, max_age_sec)):
            return pr
    if claims:
        sibling_branches = {
            f'forge/{env}'
            for env in (
                c.get('envelope_task_id') for c in claims
            )
            if isinstance(env, str) and env
        }
        if sibling_branches:
            for pr in merged_prs:
                if (pr.get('headRefName') in sibling_branches
                        and _pr_merged_recently(pr, now, max_age_sec)):
                    return pr
    return None


def _reconcile_already_merged_launch_phase(
    sequence: dict[str, Any],
    logger: logging.Logger,
    now: datetime,
    _repo_cache: Optional[dict[str, Optional[list[dict[str, Any]]]]] = None,
) -> int:
    """Pre-dispatch already-merged backstop for a board-Launch sequence (#609
    follow-up). For a ``launch-<phase_id>`` sequence whose step is still
    ``pending`` (never dispatched) but whose deliverables ALREADY merged
    elsewhere, mark the step ``merged`` from the gh-confirmed PR INSTEAD of
    dispatching a redundant Forge build. Reuses the idempotent
    ``apply_step_merged`` rollup, so the single-step launch sequence finalizes to
    ``complete`` and the forward-dispatch leg (which runs AFTER reconcile in the
    same tick) then has a non-``active`` sequence and dispatches nothing.

    Scoped tight + flag-independent (like the rest of reconcile): only
    ``launch-`` sequences, only still-``pending`` steps (a ``dispatched`` /
    terminal step is handled by ``_reconcile_dispatched_steps`` / already done),
    and only an EXACT convention-branch match against a recently-merged PR
    (``_already_merged_launch_match``). Fail-safe: any gh / ssh / guard error
    returns 0 and leaves the step ``pending`` for the normal dispatch path (plus
    the post-build #610 no-delta bridge). Returns the count reconciled (0 or 1
    for a single-step launch).

    The per-tick ``_repo_cache`` is shared with ``_reconcile_dispatched_steps``
    so a launch sequence whose repo was already queried this tick issues no extra
    gh call."""
    seq_id = sequence.get('seq_id')
    if not isinstance(seq_id, str) or not seq_id.startswith('launch-'):
        return 0
    if _repo_cache is None:
        _repo_cache = {}
    pending = [
        s for s in (sequence.get('steps') or [])
        if isinstance(s, dict) and s.get('status') == 'pending'
    ]
    if not pending:
        return 0
    try:
        import sequence_shortcut_helpers as ssh  # noqa: E402
        import launch_dedup_guard as guard  # noqa: E402
    except Exception as e:  # noqa: BLE001
        logger.info(
            f'pre-dispatch reconcile: helpers unavailable '
            f'({type(e).__name__}: {e}); skipping for {seq_id}'
        )
        return 0
    reconciled = 0
    for step in pending:
        step_id = step.get('step_id')
        if not isinstance(step_id, str) or not step_id:
            continue
        target_repo = step.get('target_repo')
        if not isinstance(target_repo, str) or not target_repo:
            continue
        if target_repo not in _repo_cache:
            _repo_cache[target_repo] = _gh_list_merged_prs(target_repo, logger)
        merged_prs = _repo_cache[target_repo]
        # None (gh failure, already logged) or [] (queried OK, nothing merged) —
        # either way we cannot confidently confirm an already-merged delivery, so
        # leave the step pending for the normal dispatch path.
        if not merged_prs:
            continue
        # #609 deliverable claims naming this phase (the cross-identity case).
        # Derive the ledger from the advancer's own blackboard so a test pointing
        # AGENTS_ROOT at a tmpdir resolves the tmp ledger. Fail-safe → no claims.
        try:
            claims = guard.find_matching_claims(
                {'phase_id': step_id, 'seq_id': seq_id},
                sequences_dir=BLACKBOARD_DIR, now=now,
            )
        except Exception:  # noqa: BLE001 — claims are advisory corroboration
            claims = []
        match = _already_merged_launch_match(
            step_id, seq_id, merged_prs, claims, now,
            PREDISPATCH_MERGE_LOOKBACK_SEC,
        )
        if match is None:
            continue
        pr_url = match.get('url') or ''
        merged_at = match.get('mergedAt') or now.isoformat()
        try:
            result = ssh.apply_step_merged(
                seq_id=seq_id,
                step_id=step_id,
                pr_url=pr_url,
                merged_at=merged_at,
                actor='advancer-already-merged-predispatch',
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                f'pre-dispatch reconcile: apply_step_merged raised for '
                f'{seq_id}/{step_id}: {type(e).__name__}: {e}'
            )
            continue
        if getattr(result, 'applied', False):
            reconciled += 1
            step['status'] = 'merged'
            step['pr_url'] = pr_url
            logger.info(
                f'pre-dispatch reconcile: launch phase {step_id!r} already '
                f'delivered by merged PR {pr_url} (branch '
                f'{match.get("headRefName")!r}); marked merged + skipped a '
                f'redundant Forge build (seq={seq_id})'
            )
    return reconciled


def _signal_launch_phase_done(seq_id: Any, logger: logging.Logger) -> None:
    """Emit the one-time completion signal for a LAUNCH sequence the advancer's
    OWN reconcile pass drove to ``complete``. The outbox notifier owns the
    happy-path completion signal (SEQUENCE_COMPLETE → done-stamp + closeout + DM),
    but it only fires when it processes a merge outbox whose task_id matches a
    step — exactly the match that MISSES for the already-merged / cross-identity
    class the advancer reconcile exists to bridge. Without this, an advancer-
    reconciled launch phase stays ``building`` on the board with no closeout.

    Single-winner + idempotent: claims via the SAME
    ``ssh.claim_completion_signal`` gate the notifier uses (writes the
    ``sequence-complete-signaled`` marker before returning ``applied=True``), so a
    race with the notifier — or a re-tick — can never double-stamp or double-DM.
    On the (rare) happy path where the notifier already signaled, the claim
    returns ``applied=False`` and this is a clean no-op. Fail-safe: any error is
    logged and swallowed; the merge itself already landed on disk.

    Scoped to launch sequences (a non-launch sequence has no phase/closeout, and
    its completion DM stays the notifier's job). Mirrors the notifier's
    ``_maybe_signal_sequence_complete`` minus the post_merge executor + chain
    event (a launch sequence carries no ``post_merge`` block; the board-truth
    done-stamp + closeout + the doorbell DM are the load-bearing surface)."""
    if not isinstance(seq_id, str) or not seq_id.startswith('launch-'):
        return
    try:
        import sequence_shortcut_helpers as ssh  # noqa: E402
        seq, err = ssh._read_sequence(seq_id)
        if err is not None or not isinstance(seq, dict):
            return
        if seq.get('status') != 'complete' or ssh.is_completion_signaled(seq):
            return
        result = ssh.claim_completion_signal(
            seq_id, actor='advancer-already-merged-reconcile')
        if not getattr(result, 'applied', False):
            # Lost the race to the notifier, or already signaled — exactly-once
            # guard held; nothing more to do.
            return
        # We won the claim. Stamp the phase done (board truth) + author its
        # closeout, both NON-committer writes (heal_projects_store commits), then
        # ring Larry's doorbell. Each leg is independently guarded so a failure in
        # one never blocks the others or wedges the tick.
        try:
            if psw.stamp_done(seq=seq):
                logger.info(
                    f'pre-dispatch reconcile: stamped phase done for {seq_id}')
        except Exception as e:  # noqa: BLE001 — psw is already fail-safe
            logger.warning(
                f'pre-dispatch reconcile: done-stamp for {seq_id} raised '
                f'{type(e).__name__}: {e}; swallowing')
        closeout_outputs: Optional[dict[str, Any]] = None
        try:
            import projects_closeout_author as closeout  # noqa: E402
            closeout_outputs = closeout.run_closeout_for_sequence(seq)
        except Exception as e:  # noqa: BLE001 — closeout is non-load-bearing
            logger.warning(
                f'pre-dispatch reconcile: closeout for {seq_id} raised '
                f'{type(e).__name__}: {e}; swallowing')
        phase_title = (
            (closeout_outputs or {}).get('phase_title')
            or seq.get('label') or seq_id
        )
        summary = (closeout_outputs or {}).get('summary') or ''
        pr_urls = sorted({
            s.get('pr_url') for s in (seq.get('steps') or [])
            if isinstance(s, dict) and s.get('pr_url')
        })
        pr_line = f'\n\nDelivered by: {", ".join(pr_urls)}' if pr_urls else ''
        summary_line = f'\n\n{summary}' if summary else ''
        _dm_larry(
            message=(
                f'Phase **{phase_title}** (`{seq_id}`) reconciled to *done* — its '
                f'deliverables had ALREADY merged elsewhere, so the advancer '
                f'skipped a redundant Forge build and stamped the phase done + '
                f'authored its closeout.{pr_line}{summary_line}'
            ),
            subject=f'sequence-complete:{seq_id}',
            severity='warning',
        )
        logger.info(
            f'pre-dispatch reconcile: completion signaled for {seq_id} '
            f'(done + closeout + DM; advancer won the claim)'
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        logger.error(
            f'pre-dispatch reconcile: completion signal for {seq_id} raised '
            f'{type(e).__name__}: {e}; swallowing'
        )


# -------------------- tick --------------------


def _needs_you_task_id(item: dict[str, Any]) -> Optional[str]:
    """The chain_events task_id a waiting-sequence item projects under:
    `seq-<seq_id>` for a paused sequence, `seq-<seq_id>-step-<step_id>` for a
    stuck step. None if the item has no seq_id."""
    seq_id = item.get('seq_id')
    if not seq_id:
        return None
    step_id = item.get('step_id')
    return f'seq-{seq_id}-step-{step_id}' if step_id else f'seq-{seq_id}'


def _reconcile_sequence_needs_you(
    now: datetime, logger: logging.Logger,
) -> None:
    """Project the `sequence_needs_you` read-model rows (approval-sync Phase 3a
    §3a.1). Reuses `system_state_log.load_waiting_sequences` — the SAME logic the
    dashboard's waiting-on-you view derives — but at emit-time, so the dashboard
    reads one substrate (chain_events) instead of re-deriving from the sequence
    files. `reconcile_open_events` emits a row for each paused/stuck item and
    clears any row whose sequence resumed or whose step un-stuck.

    Runs every tick, flag-INDEPENDENT (like the terminal-state reconcile): the
    needs-you projection is a read-model concern, not forward-dispatch, and a
    paused sequence still needs its row cleared when resumed even while
    auto-dispatch is gated off. Best-effort — a projection failure never fails
    the tick. lane='steer' / needs_larry=False: a paused sequence is a
    reversible nudge, not a badge-driving decision."""
    try:
        import system_state_log as ssl  # noqa: PLC0415 — lazy, optional dep
        import chain_event_emit as cee  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — projection is optional
        logger.warning(
            'sequence_needs_you: seam unavailable: %s: %s',
            type(e).__name__, e)
        return
    try:
        # strict=True: a transient read failure RAISES (not fail-safe-to-[]) so
        # an unconfirmed-empty desired set never drives reconcile_open_events to
        # clear every still-open sequence_needs_you row. Because emit uses a
        # stable event_id, such a blind clear would be permanent — the re-emit is
        # absorbed by the PK. Skipping the tick leaves the open rows intact.
        waiting = ssl.load_waiting_sequences(now, strict=True)
    except Exception as e:  # noqa: BLE001 — fail-safe: skip projection this tick
        logger.warning(
            'sequence_needs_you: load_waiting_sequences raised: %s: %s',
            type(e).__name__, e)
        return
    desired: dict[str, Any] = {}
    for item in waiting:
        if not isinstance(item, dict):
            continue
        tid = _needs_you_task_id(item)
        if not tid:
            continue
        desired[tid] = {
            'ts': item.get('_ts') or now.isoformat(),
            'payload': {
                'seq_id': item.get('seq_id'),
                'step_id': item.get('step_id'),
                'why': item.get('why'),
                'actions': item.get('actions'),
                'lane': 'steer',
                'needs_larry': False,
            },
        }
    try:
        summary = cee.reconcile_open_events(
            'sequence_needs_you', desired, agent='build_sequence_advancer',
            logger=logger)
        if summary.get('emitted') or summary.get('cleared'):
            logger.info(
                'sequence_needs_you: emitted=%s cleared=%s',
                summary.get('emitted'), summary.get('cleared'))
    except Exception as e:  # noqa: BLE001 — projection must never fail the tick
        logger.warning(
            'sequence_needs_you: reconcile raised: %s: %s',
            type(e).__name__, e)


def tick(now: Optional[datetime] = None) -> int:
    """One advancer tick. Returns 0 on completion (always — failures are
    surfaced via DM, not via exit code, so systemd doesn't restart-loop
    on a single bad sequence file)."""
    logger = _setup_logging()
    if kill_switch_active():
        logger.info('KILL_SWITCH active; exiting tick')
        return 0
    now = now or datetime.now(timezone.utc)
    # The terminal-state reconciliation pass runs EVERY tick, independent of
    # the advancer activation flag (spec §3.4a). The flag gates only the
    # forward-dispatch logic in _process_active_sequence — a stranded
    # `dispatched` step whose PR has already merged must be retired even while
    # auto-dispatch is OFF, otherwise the in-flight record outlives its work's
    # terminal state (the invariant this whole spec locks). When the flag is
    # off we skip the supabase connect (dispatch-only) and the dispatch leg,
    # but still walk every sequence file for reconciliation.
    advancer_on = activation_enabled()
    if advancer_on:
        # Heartbeat only when forward-dispatch is live. The heartbeat healer
        # treats a fresh mtime as "daemon active"; writing it while the gate
        # is closed would mask an inactive advancer. The reconciliation pass
        # below still runs regardless, but it is a backstop, not the daemon's
        # primary liveness signal.
        heartbeat()
    else:
        logger.info(
            f'{ACTIVATION_ENV}=false; forward-dispatch gated OFF (set true in '
            f'systemd override to activate after PR-S3 + PR-S4 ship). The '
            f'terminal-state reconciliation pass still runs this tick.'
        )
    # Consume any chat-path kickoff markers BEFORE walking the sequence files,
    # so a sequence transitioned pending → active this tick gets its first step
    # dispatched in the same tick's forward-dispatch loop below. Gated on the
    # activation flag like forward-dispatch: with dispatch off, kicking a
    # sequence active would be inert, so leave the envelope queued until the
    # advancer is enabled (it persists in the inbox, idempotent when drained).
    if advancer_on:
        try:
            _drain_kickoff_inbox(logger)
        except Exception as e:  # never let a bad envelope abort the whole tick
            logger.warning(
                f'kickoff-inbox drain raised {type(e).__name__}: {e}; '
                f'continuing with sequence processing'
            )
    supabase_client = _connect_supabase() if advancer_on else None
    if advancer_on and supabase_client is None:
        logger.warning(
            'supabase client unavailable (creds missing or library not '
            'installed); gate check chain_events leg will return False for '
            'this tick — steps will stay in `dispatched` until supabase is '
            'reachable. The single-leg gh leg cannot advance by itself per '
            'spec § 5.3 belt-and-suspenders.'
        )
    seen_files = 0
    processed = 0
    reconciled_total = 0
    escalated_total = 0
    # Buildable repos, read once per tick (cheap) and shared across sequences;
    # empty if the config was unreadable (escalation skips the invalid-repo
    # check — fail open).
    valid_repos = _valid_target_repos()
    # Shared per-tick gh cache across all sequences (multiple sequences may
    # target the same repo); survives the whole loop, not just one sequence.
    repo_cache: dict[str, Optional[list[dict[str, Any]]]] = {}
    for path in _iter_sequence_files():
        seen_files += 1
        seq, err = _read_sequence(path)
        if seq is None:
            _handle_unparseable_sequence(path, err or 'unknown', logger)
            continue
        validation = bsv.validate_dag(seq)
        if not validation.valid:
            _handle_invalid_sequence(path, seq, validation.errors, logger)
            continue
        status = seq.get('status')
        # Reconciliation pass — flag-INDEPENDENT (§3.4a) and run for both
        # `active` and `paused` sequences (§3.4c). A paused sequence can still
        # hold `dispatched` steps whose PR merged after the pause; without
        # this scan those records would never retire. Runs BEFORE the
        # forward-dispatch leg so apply_step_merged's disk write is visible
        # when the dispatch loop computes `merged_ids` on the same tick.
        if status in ('active', 'paused'):
            try:
                reconciled_here = _reconcile_dispatched_steps(
                    seq, logger, repo_cache,
                )
                # #609 follow-up: also catch a still-PENDING launch phase whose
                # deliverables already merged elsewhere, BEFORE the forward-
                # dispatch leg below fires a redundant Forge build. Reuses the
                # same idempotent apply_step_merged rollup; shares the per-tick
                # gh cache so it issues no extra `gh pr list` for a repo already
                # queried above.
                reconciled_predispatch = _reconcile_already_merged_launch_phase(
                    seq, logger, now, repo_cache,
                )
                reconciled_here += reconciled_predispatch
                reconciled_total += reconciled_here
                # apply_step_merged wrote to disk; re-read so the in-memory
                # copy matches before we hand it to _process_active_sequence.
                if reconciled_here > 0:
                    refreshed, _ = _read_sequence(path)
                    if refreshed is not None:
                        seq = refreshed
                        status = seq.get('status')
                    # ONLY the pre-dispatch already-merged path owns the launch
                    # phase's done + closeout signal. A launch sequence is
                    # single-step, so it finalizes to `complete` on the same tick
                    # its step is reconciled. Completion of a launch sequence by
                    # the DISPATCHED-step reconcile (a build that actually RAN,
                    # then bridged via #610) stays the notifier's job, as before —
                    # claiming its signal here would wrongly DM Larry that a
                    # "redundant build was skipped" and steal the notifier's richer
                    # completion DM.
                    if reconciled_predispatch > 0 and status == 'complete':
                        _signal_launch_phase_done(seq.get('seq_id'), logger)
            except Exception as e:
                logger.error(
                    f'reconcile: unexpected error for {path.name}: '
                    f'{type(e).__name__}: {e}'
                )
        # Stranded-dispatch escalation — flag-INDEPENDENT (§3.4a-style backstop)
        # but `active`-only so it never re-fires on an already-paused sequence.
        # A `dispatched` step on an invalid target_repo (or stalled with no PR)
        # can't be reconciled and would otherwise hang forever; escalate it
        # (fail step + pause + DM) so a Launch can't silently wedge for hours.
        # Runs AFTER reconcile so a step whose PR did merge is retired first.
        if status == 'active':
            try:
                if _escalate_stranded_dispatched_steps(
                    seq, path, now, logger, valid_repos,
                    stall_enabled=advancer_on,
                    supabase_client=supabase_client,
                ):
                    escalated_total += 1
                    # The pass mutates `seq` in place (status → paused) AND
                    # atomic-writes it. Trust the in-memory copy — do NOT
                    # re-read from disk: if the write FAILED, the on-disk file
                    # is still `active`, and re-reading it would resurrect the
                    # stale status, letting the forward-dispatch leg act on the
                    # very step the pass just tried to pause. The write retries
                    # next tick from the still-active on-disk state.
                    status = seq.get('status')
            except Exception as e:
                logger.error(
                    f'escalate: unexpected error for {path.name}: '
                    f'{type(e).__name__}: {e}'
                )
        # With-PR review-stall recover-or-route (completeness-pr2 Face 2 / Fix B)
        # — flag-INDEPENDENT (a wedged review is wedged regardless of whether
        # forward-dispatch is on) but `active`-only: a sequence just paused by
        # the escalate pass above is skipped. Unlike escalate, this pass never
        # pauses — a re-dispatched review keeps the sequence live.
        if status == 'active':
            try:
                _recover_review_stalled_steps(seq, path, now, logger)
            except Exception as e:
                logger.error(
                    f'review-stall: unexpected error for {path.name}: '
                    f'{type(e).__name__}: {e}'
                )
        # Forward-dispatch leg — flag-GATED and active-only. Paused sequences
        # (incl. any just paused by escalation above) are never advanced here.
        if not advancer_on or status != 'active':
            continue
        try:
            _process_active_sequence(path, seq, supabase_client, now, logger)
            processed += 1
        except Exception as e:
            # Catch-all defense: a bug in per-sequence processing must not
            # poison the entire tick. Log + DM, then move on to the next
            # file. Other sequences keep advancing per spec § 5.4.
            logger.error(
                f'unexpected error processing {path.name}: '
                f'{type(e).__name__}: {e}'
            )
            _dm_larry(
                message=(
                    f'build_sequence_advancer hit an unexpected error '
                    f'processing `{path.name}`: '
                    f'{type(e).__name__}: {e}\n\n'
                    f'Other sequences in this tick continued normally. '
                    f'The next tick will retry this file. If this repeats, '
                    f'inspect the daemon log: journalctl -u '
                    f'ourliberty-build-sequence-advancer.service --since '
                    f'"30 min ago" | tail -100'
                ),
                subject=f'sequence-tick-error:{path.name}',
                severity='warning',
            )
    logger.info(
        f'tick: files={seen_files} processed={processed} '
        f'reconciled_steps={reconciled_total} escalated_seqs={escalated_total}'
    )
    # Project the sequence_needs_you read-model rows (approval-sync Phase 3a).
    # Flag-independent + best-effort: runs after the sequence walk so this tick's
    # pauses/resumes are reflected, and never fails the tick.
    _reconcile_sequence_needs_you(now, logger)
    return 0


# -------------------- CLI --------------------


def _dump_state(seq_id: str) -> int:
    """Dump a parsed sequence + computed transitions for one seq_id. Read-
    only; never writes. Useful for debugging without involving systemd."""
    target = BLACKBOARD_DIR / f'{seq_id}.json'
    if not target.is_file():
        sys.stderr.write(f'ERROR: not found: {target}\n')
        return 1
    seq, err = _read_sequence(target)
    if seq is None:
        sys.stderr.write(f'ERROR: {target}: {err}\n')
        return 1
    validation = bsv.validate_dag(seq)
    out = {
        'path': str(target),
        'seq_id': seq.get('seq_id'),
        'status': seq.get('status'),
        'current_steps': seq.get('current_steps'),
        'schema_valid': validation.valid,
        'schema_errors': validation.errors,
        'steps': [
            {
                'step_id': s.get('step_id'),
                'status': s.get('status'),
                'depends_on': s.get('depends_on'),
                'pr_url': s.get('pr_url'),
                'dispatched_at': s.get('dispatched_at'),
                'merged_at': s.get('merged_at'),
            }
            for s in (seq.get('steps') or [])
        ],
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write('\n')
    return 0


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description='Polling daemon that advances multi-step build sequences.',
    )
    parser.add_argument(
        '--once', action='store_true',
        help='Run one tick and exit (default; kept for explicitness).',
    )
    parser.add_argument(
        '--dump-state', metavar='SEQ_ID',
        help='Dump parsed state + validation for one sequence; no writes.',
    )
    args = parser.parse_args(argv)
    if args.dump_state:
        return _dump_state(args.dump_state)
    return tick()


if __name__ == '__main__':
    try:
        sys.exit(_main(sys.argv[1:]))
    except Exception as exc:
        _setup_logging().error(
            f'FATAL at tick boundary: {type(exc).__name__}: {exc}'
        )
        sys.exit(1)
