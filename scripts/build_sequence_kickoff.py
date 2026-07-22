#!/usr/bin/env python3
"""build_sequence_kickoff.py — shared build-sequence kickoff transition.

Single source of truth for the `status: pending -> active` transition that
arms a build sequence (plus the `kickoff-acknowledged` audit_log entry). Two
entry paths converge here so the transition is implemented exactly ONCE:

  1. The AUTONOMOUS path — Beacon drops a kickoff APPROVAL_REQUEST in her
     outbox, `outbox_notifier._handle_build_sequence_advancer_kickoff`
     extracts the marker and calls `apply_kickoff_transition`.
  2. The CHAT / DASHBOARD-approve path — Larry taps `approve sequence <id>`,
     the bot calls `beacon_approval_handler.dispatch_approved`, which (for a
     `target_agent == 'build_sequence_advancer'` entry) calls the SAME helper
     instead of writing to the dead-end `inboxes/build_sequence_advancer/`
     (a directory with no consumer — the advancer daemon reads sequence files
     under `blackboard/build-sequences/`, not that inbox).

This module lives on its own — NOT in `outbox_notifier`, NOT in
`beacon_approval_handler` — because `outbox_notifier` already imports
`beacon_approval_handler`; putting the shared helper in either would create a
circular import. It depends only on `larry_alerts` (leaf) and lazily on
`build_sequence_validator`.

Behavioral contract (preserved verbatim from the notifier's original inline
implementation): the pending->active transition, the idempotent WARN no-op
for a sequence past `pending`, the validator + spec-doc presence gates, the
atomic write, and the Larry-DM failure modes are all identical regardless of
entry path.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import larry_alerts  # noqa: E402

# The one target_agent value that routes a kickoff marker to this transition.
SEQUENCE_KICKOFF_TARGET_AGENT = 'build_sequence_advancer'

# Canonical kickoff-prompt shape per spec § 5.5 discipline 2: `kickoff <seq-id>`.
_KICKOFF_PROMPT_RE = re.compile(r'^\s*kickoff\s+([A-Za-z0-9._-]+)\s*$')


def _default_log(msg: str, level: str = 'INFO') -> None:
    """Fallback logger for callers without their own (e.g. the bot path)."""
    sys.stderr.write(f'[build_sequence_kickoff] [{level}] {msg}\n')


@dataclass
class KickoffOutcome:
    """Result of a kickoff transition attempt.

    Attributes:
        kind: machine-readable category — one of {'activated', 'no-seq-id',
            'missing', 'read-error', 'invalid-json', 'no-validator',
            'invalid', 'spec-behind-origin', 'spec-not-authored',
            'already-active', 'write-error'}.
        seq_id: the parsed sequence id (None only when it couldn't be parsed).
        seq_path: the sequence file path (None only for the no-seq-id case).
        sentinel: the EXACT string the outbox notifier's handler returns for
            this outcome — preserved verbatim so the caller can return it
            unchanged (existing tests assert on substrings of it).
    """

    kind: str
    seq_id: Optional[str]
    seq_path: Optional[Path]
    sentinel: str


def extract_seq_id(
    prompt: Optional[str], marker_task_id: Optional[str],
) -> Optional[str]:
    """Parse the seq_id from a kickoff marker's `prompt` / `task_id`.

    Canonical wording (spec § 5.5 discipline 2) is `kickoff <seq-id>`; the
    marker's `task_id` of shape `kickoff-<seq-id>` is accepted as a fallback
    so a session that puts the seq_id in either place still routes correctly.
    """
    if isinstance(prompt, str):
        m = _KICKOFF_PROMPT_RE.match(prompt)
        if m:
            return m.group(1)
    if isinstance(marker_task_id, str) and marker_task_id.startswith('kickoff-'):
        return marker_task_id[len('kickoff-'):].strip() or None
    return None


def apply_kickoff_transition(
    *,
    prompt: Optional[str],
    marker_task_id: Optional[str],
    dispatch_task_id: str,
    agents_root: Path,
    log: Callable[..., None] = _default_log,
) -> KickoffOutcome:
    """Transition the referenced sequence from `pending` to `active`.

    This is the single implementation both the autonomous outbox path and the
    chat/dashboard-approve path converge on. It:
      - parses the seq_id (DM Larry + return `no-seq-id` if unparseable),
      - locates + reads + JSON-parses the sequence file (DM on each failure),
      - validates the DAG (DM + audit-trail on invalid),
      - is idempotent: a sequence already past `pending` gets a
        `kickoff-duplicate-suppressed` audit entry and NO status change,
      - guards on spec_doc presence (behind-origin defer vs not-authored fail),
      - atomically flips status pending->active and appends a
        `kickoff-acknowledged` audit entry.

    Args:
        prompt: the marker's `prompt` field (canonical `kickoff <seq-id>`).
        marker_task_id: the marker payload's `task_id` (used for the seq_id
            fallback and enriched validation-failure alert).
        dispatch_task_id: the dispatch/envelope task_id — recorded in the
            `kickoff-acknowledged` / `kickoff-duplicate-suppressed` audit
            entries and in the sentinel/log lines.
        agents_root: the `~/agents` root (sequence files live under
            `<agents_root>/blackboard/build-sequences/`). Passed in so each
            caller supplies its own module-level root (test-patchable).
        log: a `log(msg, level='...')`-compatible callable; defaults to a
            stderr logger for callers without one.

    Returns:
        A `KickoffOutcome`. The caller decides how to adapt it (the notifier
        returns `outcome.sentinel`; `dispatch_approved` returns
        `outcome.seq_path`).
    """
    seq_id = extract_seq_id(prompt, marker_task_id)
    if not seq_id:
        log(
            f'sequence-kickoff marker on task {dispatch_task_id} has no '
            f'parseable seq_id (prompt={prompt!r}); skipping',
            'WARN',
        )
        # PR-S4 rectification (L5): loud failure beats silent. If the kickoff
        # marker is mis-emitted (e.g., `prompt: "approve <id>"` instead of
        # `kickoff <id>`), the handler used to archive silently and the
        # sequence stayed `pending` forever. DM Larry so it surfaces.
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=(
                f'Build-sequence kickoff marker on task `{dispatch_task_id}` '
                f'has no parseable seq_id (prompt={prompt!r}). The kickoff '
                f'was likely emitted with the wrong prompt shape; expected '
                f'`kickoff <seq-id>`. Sequence remains in its prior status; '
                f're-dispatch the kickoff with the correct prompt.'
            ),
            subject=f'kickoff-malformed-prompt:{dispatch_task_id}',
        )
        return KickoffOutcome(
            kind='no-seq-id',
            seq_id=None,
            seq_path=None,
            sentinel=f'sequence-kickoff:no-seq-id:{dispatch_task_id}',
        )

    seq_path = (
        agents_root / 'blackboard' / 'build-sequences' / f'{seq_id}.json'
    )
    if not seq_path.is_file():
        msg = (
            f'Sequence `{seq_id}` kickoff failed: sequence file missing at '
            f'{seq_path}. Author the sequence file (Beacon discipline 2) '
            f'before re-dispatching the kickoff.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED file-missing '
            f'task={dispatch_task_id}',
            'WARN',
        )
        return KickoffOutcome(
            kind='missing', seq_id=seq_id, seq_path=seq_path,
            sentinel=f'sequence-kickoff:missing:{seq_id}',
        )

    try:
        raw_text = seq_path.read_text()
    except OSError as e:
        msg = (
            f'Sequence `{seq_id}` kickoff failed: cannot read sequence file '
            f'at {seq_path} ({e}). Investigate filesystem/permissions.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED read-error '
            f'task={dispatch_task_id}: {e}',
            'WARN',
        )
        return KickoffOutcome(
            kind='read-error', seq_id=seq_id, seq_path=seq_path,
            sentinel=f'sequence-kickoff:read-error:{seq_id}',
        )

    try:
        seq = json.loads(raw_text)
    except json.JSONDecodeError as e:
        msg = (
            f'Sequence `{seq_id}` kickoff failed: sequence file is not valid '
            f'JSON ({e}). Fix the file before re-dispatching the kickoff.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED invalid-json '
            f'task={dispatch_task_id}: {e}',
            'WARN',
        )
        return KickoffOutcome(
            kind='invalid-json', seq_id=seq_id, seq_path=seq_path,
            sentinel=f'sequence-kickoff:invalid-json:{seq_id}',
        )

    # Lazy import: keeps build_sequence_validator out of the import-time graph
    # for environments that don't have it on sys.path (e.g. minimal test
    # fixtures exercising unrelated handlers). Resolve it the SAME way callers
    # historically did so that a test patching `check_spec_doc_presence` on
    # the resolved module is seen here.
    try:
        from scripts import build_sequence_validator as bsv  # type: ignore  # noqa: E402
    except ImportError:
        try:
            import build_sequence_validator as bsv  # type: ignore  # noqa: E402
        except ImportError as e:
            log(
                f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED validator-import '
                f'task={dispatch_task_id}: {e}',
                'WARN',
            )
            return KickoffOutcome(
                kind='no-validator', seq_id=seq_id, seq_path=seq_path,
                sentinel=f'sequence-kickoff:no-validator:{seq_id}',
            )

    result = bsv.validate_dag(seq)
    if not result.valid:
        # PR-S4 rectification (M2): enrich the alert and append a side-channel
        # ops audit trail so the failure is fully reconstructable. The original
        # alert dropped only the first error — load-bearing when validation
        # rejected for multiple reasons.
        errs = list(result.errors or [])
        first_three = errs[:3]
        more = max(0, len(errs) - 3)
        more_suffix = f' (+{more} more)' if more else ''
        formatted = (
            '\n'.join(f'  - {e}' for e in first_three)
            if first_three else '  - unspecified validator error'
        )
        msg = (
            f'Sequence `{seq_id}` kickoff failed: schema/DAG validation '
            f'failed. Marker task_id: `{marker_task_id}`. Sequence file: '
            f'`{seq_path}`.\n\nFirst validator errors{more_suffix}:\n'
            f'{formatted}\n\nRun `python3 scripts/build_sequence_validator.py '
            f'validate {seq_id}` to see all errors, then re-dispatch the '
            f'kickoff.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        # Side-channel ops audit trail. Append-only; one JSON line per
        # rejected kickoff. Survives Larry-DM history rotation.
        try:
            failures_path = (
                agents_root / 'blackboard' / 'build-sequences'
                / '.kickoff-failures.jsonl'
            )
            failures_path.parent.mkdir(parents=True, exist_ok=True)
            with failures_path.open('a', encoding='utf-8') as fail_f:
                fail_f.write(json.dumps({
                    'ts': datetime.now(timezone.utc).isoformat(),
                    'seq_id': seq_id,
                    'task_id': dispatch_task_id,
                    'marker_task_id': marker_task_id,
                    'sequence_path': str(seq_path),
                    'errors': errs,
                }) + '\n')
        except OSError as e:
            log(
                f'kickoff-failures.jsonl append failed for seq={seq_id} '
                f'task={dispatch_task_id}: {e}',
                'WARN',
            )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED validation '
            f'task={dispatch_task_id}: {errs[:3]}',
            'WARN',
        )
        return KickoffOutcome(
            kind='invalid', seq_id=seq_id, seq_path=seq_path,
            sentinel=f'sequence-kickoff:invalid:{seq_id}',
        )

    current_status = seq.get('status')
    if current_status != 'pending':
        # Idempotent no-op: re-emitting kickoff on a sequence past `pending`
        # must NOT duplicate the `kickoff-acknowledged` event or trigger a
        # second daemon-side dispatch.
        #
        # PR-S4 rectification (M3): append a `kickoff-duplicate-suppressed`
        # entry to keep the audit log honest (a DIFFERENT event from
        # `kickoff-acknowledged`, so the "no duplicate kickoff-acknowledged"
        # invariant still holds).
        original_kickoff = next(
            (
                e for e in (seq.get('audit_log') or [])
                if isinstance(e, dict)
                and e.get('event') == 'kickoff-acknowledged'
            ),
            None,
        )
        original_task_id = (
            original_kickoff.get('task_id')
            if isinstance(original_kickoff, dict) else None
        )
        dedup_entry = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': 'kickoff-duplicate-suppressed',
            'actor': 'outbox-notifier',
            'original_task_id': original_task_id,
            'duplicate_task_id': dispatch_task_id,
            'status_at_suppression': current_status,
        }
        if not isinstance(seq.get('audit_log'), list):
            seq['audit_log'] = []
        seq['audit_log'].append(dedup_entry)
        # Atomic-write the dedup entry. Errors are logged but not fatal — the
        # duplicate-suppression itself is the safety property; the audit trail
        # is a nice-to-have on top.
        dedup_tmp = seq_path.with_suffix(seq_path.suffix + '.tmp')
        try:
            with open(dedup_tmp, 'w', encoding='utf-8') as f:
                json.dump(seq, f, indent=2)
                f.write('\n')
            os.replace(dedup_tmp, seq_path)
        except OSError as e:
            try:
                dedup_tmp.unlink()
            except OSError:
                pass
            log(
                f'BUILD_SEQUENCE_KICKOFF seq={seq_id} dedup audit append '
                f'failed task={dispatch_task_id}: {e}',
                'WARN',
            )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} WARN already-kicked-off '
            f'status={current_status} task={dispatch_task_id}; no-op (dedup '
            f'audit entry appended)',
            'WARN',
        )
        return KickoffOutcome(
            kind='already-active', seq_id=seq_id, seq_path=seq_path,
            sentinel=f'sequence-kickoff:already-active:{seq_id}',
        )

    # spec_doc presence guard (incident 2026-06-10). status is `pending`
    # here — this is the one transition that arms the advancer. The sequence
    # file is local, so validate_dag passes even when the spec_doc it
    # references hasn't synced into this checkout yet. Placed AFTER the
    # idempotency no-op so a re-dispatched kickoff on an already-active
    # sequence still dedups silently rather than tripping this guard.
    #
    # Resolve the spec_doc against the sequence's target_repo checkout (via
    # resolve_spec_doc_repo_root) so a cross-repo sequence — e.g. rsdpm-v0-001,
    # whose steps target RSDPM and whose BUILD_PLAN.md lives in the RSDPM
    # checkout, not agent-core — no longer false-fails NOT_AUTHORED here. An
    # agent-core-targeted sequence resolves to None → REPO_ROOT, unchanged.
    presence = bsv.check_spec_doc_presence(
        seq.get('spec_doc'),
        repo_root=bsv.resolve_spec_doc_repo_root(seq),
    )
    if presence.status == bsv.SPEC_DOC_BEHIND_ORIGIN:
        msg = (
            f'Sequence `{seq_id}` kickoff deferred: this checkout is behind '
            f'origin/main, so its spec_doc is not yet readable here — but the '
            f'spec EXISTS on main. Do NOT re-author it. {presence.message} '
            f'Then re-dispatch the kickoff. Sequence file: `{seq_path}`.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} DEFERRED '
            f'spec-doc-behind-origin task={dispatch_task_id} '
            f'spec_doc={presence.spec_doc!r} behind_by={presence.behind_by}',
            'WARN',
        )
        return KickoffOutcome(
            kind='spec-behind-origin', seq_id=seq_id, seq_path=seq_path,
            sentinel=f'sequence-kickoff:spec-behind-origin:{seq_id}',
        )
    if presence.status == bsv.SPEC_DOC_NOT_AUTHORED:
        msg = (
            f'Sequence `{seq_id}` kickoff failed: {presence.message} '
            f'Sequence file: `{seq_path}`.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED '
            f'spec-doc-not-authored task={dispatch_task_id} '
            f'spec_doc={presence.spec_doc!r}',
            'WARN',
        )
        return KickoffOutcome(
            kind='spec-not-authored', seq_id=seq_id, seq_path=seq_path,
            sentinel=f'sequence-kickoff:spec-not-authored:{seq_id}',
        )
    # present / indeterminate → proceed. Indeterminate (origin/main doesn't
    # resolve) must not block kickoff: it's the authoring-on-Mac and
    # non-synced-checkout case, where hard-failing would be a false negative.

    # Transition pending → active and append audit_log entry. Use the
    # advancer's atomic-write convention (tmp + os.replace via stdlib).
    audit_entry = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'event': 'kickoff-acknowledged',
        'actor': 'outbox-notifier',
        'task_id': dispatch_task_id,
    }
    seq['status'] = 'active'
    if not isinstance(seq.get('audit_log'), list):
        # Validator guarantees this is a list, but defend against schema drift
        # in case a future validator change relaxes the rule.
        seq['audit_log'] = []
    seq['audit_log'].append(audit_entry)

    tmp_path = seq_path.with_suffix(seq_path.suffix + '.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(seq, f, indent=2)
            f.write('\n')
        os.replace(tmp_path, seq_path)
    except OSError as e:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        msg = (
            f'Sequence `{seq_id}` kickoff failed: cannot write sequence file '
            f'at {seq_path} ({e}). Investigate filesystem/permissions.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED write-error '
            f'task={dispatch_task_id}: {e}',
            'WARN',
        )
        return KickoffOutcome(
            kind='write-error', seq_id=seq_id, seq_path=seq_path,
            sentinel=f'sequence-kickoff:write-error:{seq_id}',
        )

    log(
        f'BUILD_SEQUENCE_KICKOFF seq={seq_id} status=pending->active '
        f'task={dispatch_task_id} (next advancer tick will dispatch the '
        f'first step)'
    )
    return KickoffOutcome(
        kind='activated', seq_id=seq_id, seq_path=seq_path,
        sentinel=str(seq_path),
    )
