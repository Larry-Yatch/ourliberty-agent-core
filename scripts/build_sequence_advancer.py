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
import json
import logging
import os
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

ACTIVATION_ENV = 'OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED'

# 30-minute one-signal-only mismatch tolerance per spec § 5.3.
GATE_MISMATCH_TIMEOUT_SEC = 30 * 60

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
# reconciliation pass. 20 covers ~a week of typical velocity; bump if
# silent-misses go undetected past the lookback window.
RECONCILE_PR_LOOKBACK = 20

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
        from supabase import create_client  # type: ignore
        return create_client(url, key)
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


def chain_event_says_failed(client: Any, task_id: str) -> Optional[str]:
    """If chain_events shows a terminal-failure event for this step's PR,
    return the failure reason; otherwise None.

    Recognized failure event types per spec § 5.4 failure mode 1:
    `mirror_revision_exhausted`, `mirror_emergency_halt`, `forge_reject`.
    Returns the first match found. None on connect failure (failure-mode
    detection is best-effort; missing it just delays the pause-DM by one
    tick)."""
    if client is None or not task_id:
        return None
    failure_types = (
        'mirror_revision_exhausted', 'mirror_emergency_halt', 'forge_reject',
    )
    try:
        res = client.table('chain_events').select(
            'event_type,payload,ts'
        ).eq('task_id', task_id).in_(
            'event_type', list(failure_types)
        ).limit(10).execute()
    except Exception:
        return None
    rows = getattr(res, 'data', None) or []
    if not rows:
        return None
    row = rows[0]
    return f'{row.get("event_type")}: {(row.get("payload") or {}).get("reason", "no reason given")}'


def gh_pr_says_merged(pr_url: str) -> Optional[bool]:
    """Return True / False / None. None means "couldn't determine" (gh
    timeout, missing auth, network) — distinct from False ("PR is OPEN /
    CLOSED but not MERGED"). The gate treats None as a soft fail (waits
    rather than counting as one-leg mismatch)."""
    if not pr_url:
        return None
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', pr_url, '--json', 'state'],
            capture_output=True, text=True, timeout=GH_PR_VIEW_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return data.get('state') == 'MERGED'


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
        # Already off the active path; just log + DM (cooldown-gated).
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

    Sequence steps store a bare repo name (`ourliberty-agent-core`); gh
    rejects that with rc=1 before any network call. Prepend GITHUB_OWNER
    unless the value is already qualified (an owner-or-host '/' is
    present)."""
    if '/' in repo:
        return repo
    return f'{GITHUB_OWNER}/{repo}'


def _gh_list_merged_prs(
    repo: str, logger: Optional[logging.Logger] = None,
) -> Optional[list[dict[str, Any]]]:
    """Return the list of recently-merged PRs for `repo`, or None on
    failure (gh missing, auth missing, timeout, non-zero rc, bad JSON).

    On any failure, log a WARNING that includes the concrete reason
    (returncode + truncated stderr, or the exception) so a self-heal that
    can't query is diagnosable from the log rather than silently-ish
    skipping — three stranded incidents traced back to the bare
    'gh pr list failed' line that swallowed the underlying cause.

    None vs []: None means "couldn't query" (caller should skip
    reconciliation for this repo this tick); [] means "queried OK, no
    merged PRs in the lookback window." The two cases differ in whether
    we'd consider this a soft failure or a confident no-match."""
    if not repo:
        return None
    qualified = _qualify_repo(repo)

    def _warn(detail: str) -> None:
        if logger is not None:
            logger.warning(
                f'reconcile: gh pr list failed for repo={qualified} '
                f'({detail}); skipping reconciliation for steps in this '
                f'repo this tick'
            )

    try:
        proc = subprocess.run(
            [
                'gh', 'pr', 'list', '--repo', qualified,
                '--state', 'merged', '--limit', str(RECONCILE_PR_LOOKBACK),
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
) -> Optional[dict[str, Any]]:
    """Find a merged PR that identifies as this step. Precedence:
      1. exact pr_url match (only when the step has pr_url populated)
      2. headRefName == f'forge/{step_id}' (Forge worktree convention)
      3. step_id substring in title (kebab-case is unambiguous)

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
    for pr in merged_prs:
        title = pr.get('title') or ''
        if step_id in title:
            return pr
    return None


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
        if step.get('status') != 'dispatched':
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
        match = _match_pr_for_step(step_id, step.get('pr_url'), merged_prs)
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

    if reconciled > 0:
        logger.info(
            f'reconcile: sequence={seq_id} reconciled_steps={reconciled} '
            f'step_ids={matched_step_ids}'
        )
    return reconciled


# -------------------- tick --------------------


def tick(now: Optional[datetime] = None) -> int:
    """One advancer tick. Returns 0 on completion (always — failures are
    surfaced via DM, not via exit code, so systemd doesn't restart-loop
    on a single bad sequence file)."""
    logger = _setup_logging()
    if kill_switch_active():
        logger.info('KILL_SWITCH active; exiting tick')
        return 0
    if not activation_enabled():
        logger.info(
            f'{ACTIVATION_ENV}=false; exiting tick (set true in systemd '
            f'override to activate after PR-S3 + PR-S4 ship)'
        )
        return 0
    heartbeat()
    now = now or datetime.now(timezone.utc)
    supabase_client = _connect_supabase()
    if supabase_client is None:
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
        if seq.get('status') != 'active':
            continue
        # Active reconciliation pass: backstop the V6 notifier hook for
        # silent-misses where the auto-merge fired under a derivative
        # task_id (rebase / rescue / revision) that doesn't exact-match
        # the step. Runs BEFORE _process_active_sequence so apply_step_merged's
        # disk write is visible when the dispatch loop computes `merged_ids`
        # for downstream-dep satisfaction on the same tick.
        try:
            reconciled_here = _reconcile_dispatched_steps(seq, logger)
            reconciled_total += reconciled_here
            # apply_step_merged wrote to disk; re-read so the in-memory
            # copy matches before we hand it to _process_active_sequence.
            if reconciled_here > 0:
                refreshed, _ = _read_sequence(path)
                if refreshed is not None:
                    seq = refreshed
        except Exception as e:
            logger.error(
                f'reconcile: unexpected error for {path.name}: '
                f'{type(e).__name__}: {e}'
            )
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
        f'reconciled_steps={reconciled_total}'
    )
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
