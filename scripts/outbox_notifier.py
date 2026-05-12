#!/usr/bin/env python3
"""outbox_notifier.py — back-channel routing for completed work + dead-letters.

Phase D3, commit 2 (`D3-notifier`). Long-running daemon. 5s poll. Two scans:

  1. **Outbox results.** For each completed `outboxes/<agent>/<task-id>.json`:
     - If the original `source` is another agent OR a `*-question` source,
       write a `notify-<stem>.json` to that agent's inbox via
       `safe_write_inbox`. Bare-agent sources notify back as `*-result`;
       `*-question` sources notify back as `*-clarification`. Depth-capped
       at 1 (matches upstream `orchestrator.process_outbox_notifications`
       line 1878).
     - Failed tasks (exit_code != 0) take the same path — the dispatcher
       learns about the failure too (Gap 4 dead-letter for in-flight
       failures). Prompt frames it as `FAILED` + error text.
     - System sources (`larry`, `telegram-webhook`, `cron`, etc.) and reply-leg
       sources (`*-result`, `*-clarification`, `*-answer`) are archive-only —
       no back-channel.
     - All processed outboxes archive to `outboxes/<agent>/.archive/`
       (audit trail; cheap; matches inbox watcher convention).

  2. **Dead-letter .invalid scan.** Validator-rejected tasks today land in
     `inboxes/<agent>/.invalid/<stem>.json` with a `.reason` sidecar and
     nobody is notified. This scan finds new .invalid entries since last
     run and writes a dead-letter notify back to the source agent. State
     persisted in `state/outbox-notifier-dead-letter.json` so we don't
     re-notify on every cycle. GC'd when underlying .invalid file goes away.

What this daemon does NOT do:
  - Send Telegram DMs to Larry — that's the per-agent bot's job (the bot
    reads its own outboxes and DMs the user). Commit 3 (D3-approval) extends
    Beacon's bot to route certain notifies as approval DMs.
  - Routing-validate fresh dispatches — that's handled at write time by
    `safe_write_inbox.safe_write_inbox` (commit 1).
  - Process clarification-related outbox shapes beyond routing them. The
    Forge preflight protocol that produces `forge-question` outboxes is
    commit 4 (`D3-forge`); this daemon just routes them when they arrive.

Adapted from GrowthMastery-ai/gm-agent-core
`orchestrator.process_outbox_notifications` lines 1869–1947, with GM-specific
ship-tracking + briefing-fallback + milestone bypass stripped (audit Section 4).

EMERGENCY_HALT honored — same flag as the inbox watcher; touching it stops
both daemons cleanly.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Import sibling scripts as modules.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import dispatch_validator         # noqa: E402
import forge_preflight_handler as fph  # noqa: E402
import safe_write_inbox             # noqa: E402

HOME = Path.home()
AGENTS_ROOT = HOME / 'agents'
INBOXES_ROOT = AGENTS_ROOT / 'inboxes'
OUTBOXES_ROOT = AGENTS_ROOT / 'outboxes'
BLACKBOARD = AGENTS_ROOT / 'blackboard'
LOG_FILE = AGENTS_ROOT / 'logs' / 'outbox-notifier.log'
DEAD_LETTER_STATE = AGENTS_ROOT / 'state' / 'outbox-notifier-dead-letter.json'
EMERGENCY_HALT_FLAG = BLACKBOARD / 'EMERGENCY_HALT'

AGENT_IDS = ['beacon', 'forge', 'mirror', 'pulse']

POLL_INTERVAL_SECONDS = 5
DEAD_LETTER_STATE_CAP = 1000
MAX_RESULT_TEXT_CHARS = 8000  # truncate huge claude outputs in notify prompts

# Maximum notify-cascade depth. Hop 0 = original dispatch; hop 1 = first notify
# back (the answer). Anything > 1 means we'd be writing notify-X-result-result,
# which is the double-nest upstream's 2026-04-15 incident caught.
#
# Exception: Forge preflight markers bypass this cap because the clarification
# protocol is intentionally multi-hop (Beacon → Forge → Beacon → Forge → ...
# until PROCEED/REJECT/budget-exhausted). The `max_clarifications` budget on
# the task envelope replaces the depth cap as the termination guard for
# marker-driven cascades.
MAX_NOTIFY_DEPTH = 1

# Cap on consecutive marker-error retries to Forge. Defense against a wedge
# loop where Forge keeps producing malformed markers (e.g., a CLAUDE.md bug
# or a session that's lost track of the grammar). When exceeded, the notifier
# dead-letters back to Beacon instead of retrying Forge again.
MAX_MARKER_ERROR_RETRIES = 3

# -------------------- notify-prompt template (D3-forge commit 4a) --------------------
# Refined notify framing — Option C hybrid: one skeleton, per-intent action
# block. Replaces the D3-notifier naked "Task result from <agent>: SUCCESS\n..."
# framing that caused the Pulse over-run during commit 2's smoke test (receiver
# agent interpreted the notify as new work and burned $0.59 of Sonnet tokens).
#
# Receivers' CLAUDE.md only needs one rule: "when you receive a notify, follow
# the inline action block." Centralizing the imperative in the notifier — not
# in each receiver's CLAUDE.md — prevents drift.

NOTIFY_TEMPLATE = (
    '[Inter-agent notify | intent={intent} | from={sender} | task={task_id} | status={status}]\n\n'
    'This is an automatic delivery of an outbox result, not a new task request.\n\n'
    '{intent_block}\n\n'
    "Sender's output:\n"
    '---\n'
    '{output}\n'
    '---\n'
)

# Intent vocabulary mirrors dispatch_validator.ALLOWED_INTENTS so notify tasks
# pass schema validation. D3-prep names + D3-forge additions; see validator.
INTENT_ACTION_BLOCKS = {
    'result-notification': (
        'Read the sender output. Journal it in your activity log if material. '
        'Do not generate new work unless the sender output explicitly asks you to.'
    ),
    'clarification-response': (
        'This is the answer to your earlier CLARIFY_REQUEST on task `{task_id}`. '
        'Re-read the original spec with this new context. Decide one of: PROCEED, '
        'CLARIFY_REQUEST (you have {remaining} clarification(s) left), or REJECT. '
        'Emit exactly one marker block.'
    ),
    'clarify': (
        'Forge has asked a clarification question on task `{task_id}` '
        '(clarification {next_count} of {max_count}). Decide: answer in-scope '
        '(your reply becomes the clarification-response delivered back to Forge '
        'with `--resume`), or escalate to Larry as a plan modification by '
        'emitting a new APPROVAL_REQUEST with the revised plan.'
    ),
    'ack-proceed': (
        'Forge has emitted PROCEED on task `{task_id}` preflight. The build '
        'phase will dispatch automatically once worktree + gh pr machinery '
        'lands (commit 4b). For now, journal the proceed and stand by.'
    ),
    'reject': (
        'Forge has REJECTED task `{task_id}` at preflight. Reason: {reason}. '
        'Journal the rejection. Do not retry without addressing the reason — '
        'either revise the spec and re-emit APPROVAL_REQUEST, or set this aside.'
    ),
    'clarification-exhausted': (
        'Forge ran out of clarification budget on task `{task_id}` ({max_count} '
        'clarifications used). Final question: {reason}. The dispatch is closed; '
        'either revise the spec to remove the ambiguity and re-emit APPROVAL_REQUEST, '
        'or set this aside.'
    ),
    'dead-letter': (
        'A dispatch you originated was rejected and could not be delivered. '
        'Reason: {reason}. Journal the failure. Do not retry without addressing '
        'the rejection cause.'
    ),
    'marker-error': (
        'Your previous output on task `{task_id}` could not be parsed as a valid '
        'preflight marker (retry {retry_count} of {max_retries}). Error: {reason}. '
        'Re-read your CLAUDE.md preflight section, then re-emit EXACTLY one of '
        'PROCEED / CLARIFY_REQUEST / REJECT with the required fields and matching '
        '`=== END_XXX ===` block. After {max_retries} retries the dispatch will '
        'be closed and dead-lettered back to Beacon.'
    ),
}

_running = True


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] [notifier] [{level}] {msg}\n'
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line)
    except OSError:
        pass
    sys.stderr.write(line)


def _emergency_halt_active() -> bool:
    return EMERGENCY_HALT_FLAG.exists()


def ensure_dirs() -> None:
    for agent in AGENT_IDS:
        (OUTBOXES_ROOT / agent / '.archive').mkdir(parents=True, exist_ok=True)
        (INBOXES_ROOT / agent).mkdir(parents=True, exist_ok=True)
        (INBOXES_ROOT / agent / '.invalid').mkdir(parents=True, exist_ok=True)
    (AGENTS_ROOT / 'state').mkdir(parents=True, exist_ok=True)
    (AGENTS_ROOT / 'logs').mkdir(parents=True, exist_ok=True)


def _primary_agent_id(source: str) -> Optional[str]:
    """Return the bare agent name from a source field, or None if not an agent.

    Examples:
      'pulse'                -> 'pulse'
      'forge-result'         -> 'forge'
      'forge-question'       -> 'forge'
      'beacon-clarification' -> 'beacon'
      'telegram-webhook'     -> None
      'cron'                 -> None
    """
    for agent in AGENT_IDS:
        if source == agent or source.startswith(f'{agent}-'):
            return agent
    return None


def _should_notify_back(source: str, processing_agent: str) -> bool:
    """Decide whether an outbox warrants a notify back to its source.

    True iff source is a bare agent name OR a `*-question` source, AND
    the source's primary agent is not the processing agent itself
    (self-dispatch loops don't notify).
    """
    primary = _primary_agent_id(source)
    if primary is None:
        return False
    if primary == processing_agent:
        return False
    is_bare = source == primary
    is_question = source.endswith('-question')
    return is_bare or is_question


def _notify_back_source(processing_agent: str, original_source: str) -> str:
    """Derive the source field on the notify task.

    `<agent>-clarification` for `*-question` originals (we answered a question),
    `<agent>-result` for everything else (we completed work).
    """
    if original_source.endswith('-question'):
        return f'{processing_agent}-clarification'
    return f'{processing_agent}-result'


def _current_notify_depth(outbox_data: dict[str, Any]) -> int:
    """Compute the current cascade depth for a result being processed.

    Two signals:
      - Explicit `_notify_depth` field (propagated through prior notify hops).
      - `source_task_file` filename starts with `notify-` (the inbox task that
        produced this outbox was itself a notify cascade).
    """
    depth = outbox_data.get('_notify_depth')
    if isinstance(depth, int):
        return depth
    src_task_file = outbox_data.get('source_task_file', '')
    if src_task_file and Path(src_task_file).stem.startswith('notify-'):
        return 1
    return 0


def _truncate(text: str, limit: int = MAX_RESULT_TEXT_CHARS) -> str:
    if not text:
        return ''
    if len(text) <= limit:
        return text
    return text[:limit] + f'\n\n[... truncated, {len(text) - limit} more chars]'


def build_notify_prompt(
    *,
    intent: str,
    sender: str,
    task_id: str,
    success: bool,
    output: str,
    error: str = '',
    intent_kwargs: Optional[dict[str, Any]] = None,
) -> str:
    """Render the notify prompt for a receiver agent.

    Uses NOTIFY_TEMPLATE (header + framing) + INTENT_ACTION_BLOCKS[intent]
    (the action verb the receiver should apply). Falls back to
    `result-notification` action block if the intent is unrecognized.

    Output is the sender's claude output (truncated to MAX_RESULT_TEXT_CHARS);
    error appended if present. Result is padded to dispatch_validator's
    MIN_PROMPT_LEN floor.
    """
    intent_kwargs = dict(intent_kwargs or {})
    intent_kwargs.setdefault('task_id', task_id)
    action_template = INTENT_ACTION_BLOCKS.get(
        intent, INTENT_ACTION_BLOCKS['result-notification'],
    )
    try:
        intent_block = action_template.format(**intent_kwargs)
    except (KeyError, IndexError):
        # Missing format key — degrade gracefully instead of crashing the daemon.
        intent_block = action_template
        log(
            f'notify-prompt intent_kwargs incomplete for intent={intent}; '
            f'rendered without substitution',
            'WARN',
        )

    body_output = _truncate(output or '')
    if error:
        body_output = (body_output + '\n\n' if body_output else '') + f'Error: {error}'

    status = 'SUCCESS' if success else 'FAILED'
    prompt = NOTIFY_TEMPLATE.format(
        intent=intent,
        sender=sender,
        task_id=task_id,
        status=status,
        intent_block=intent_block,
        output=body_output or '(no output captured)',
    )
    # dispatch_validator requires MIN_PROMPT_LEN chars. The template + intent
    # block usually clears it, but truncated/empty outputs can still fall short.
    if len(prompt.strip()) < dispatch_validator.MIN_PROMPT_LEN:
        prompt += '\n\n— end of notify (auto-padded to clear validator floor) —'
    return prompt


def _archive_outbox(outbox_file: Path) -> None:
    archive_dir = outbox_file.parent / '.archive'
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / outbox_file.name
    # Don't clobber prior archives; suffix with a counter on collision.
    counter = 1
    while target.exists():
        target = archive_dir / f'{outbox_file.stem}.{counter}{outbox_file.suffix}'
        counter += 1
    shutil.move(str(outbox_file), str(target))


def _classify_forge_marker(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Inspect a Forge outbox for a preflight marker. Returns routing decision or None.

    Returned dict shape (when a marker is found):
      {
        'marker_type': 'proceed' | 'clarify_request' | 'reject',
        'payload':     parsed marker JSON,
        'intent':      one of INTENT_ACTION_BLOCKS keys (after budget check),
        'notify_source': source field for the resulting notify task,
        'intent_kwargs': dict of fields for the intent action-block template,
        'next_clarification_count': int — what the next dispatch's count should be,
                                    or None if irrelevant for this marker type,
      }

    Raises `fph.MalformedForgeMarker` or `fph.MultipleForgeMarkers` if the
    marker block is present but unparseable. The caller dead-letters those
    back to Forge so she can re-emit cleanly.

    Returns None if the outbox has no marker (legacy / non-forge agents take
    the default routing path).
    """
    result_text = data.get('result', '')
    if not isinstance(result_text, str) or not result_text.strip():
        return None

    marker_type, payload, _narrative = fph.parse_forge_marker(result_text)
    if marker_type is None:
        return None

    agent = data.get('agent', 'forge')
    if marker_type == 'clarify_request':
        decision, next_count, max_count = fph.evaluate_clarification_budget(data)
        if decision == 'exhausted':
            # Budget exhausted — clarification-exhausted intent so Beacon
            # sees the specific termination reason (not a generic reject).
            # Source goes back via the reject channel (forge-result) so the
            # protocol terminates rather than continuing the question loop.
            reason = fph.build_exhausted_reason(payload, next_count, max_count)
            return {
                'marker_type': marker_type,
                'payload': payload,
                'intent': fph.derive_intent(marker_type, budget_decision=decision),
                'notify_source': fph.derive_notify_source('reject', agent),
                'intent_kwargs': {
                    'reason': reason,
                    'max_count': max_count,
                },
                'next_clarification_count': None,
            }
        return {
            'marker_type': marker_type,
            'payload': payload,
            'intent': fph.derive_intent(marker_type, budget_decision=decision),
            'notify_source': fph.derive_notify_source(marker_type, agent),
            'intent_kwargs': {
                'next_count': next_count,
                'max_count': max_count,
            },
            'next_clarification_count': next_count,
        }

    if marker_type == 'proceed':
        return {
            'marker_type': marker_type,
            'payload': payload,
            'intent': fph.derive_intent(marker_type),
            'notify_source': fph.derive_notify_source(marker_type, agent),
            'intent_kwargs': {},
            'next_clarification_count': None,
        }

    # marker_type == 'reject'
    return {
        'marker_type': marker_type,
        'payload': payload,
        'intent': fph.derive_intent(marker_type),
        'notify_source': fph.derive_notify_source(marker_type, agent),
        'intent_kwargs': {'reason': payload.get('reason', '(no reason given)')},
        'next_clarification_count': None,
    }


def _notify_forge_marker_error(data: dict[str, Any], err_msg: str) -> None:
    """Write a marker-error notify back to Forge so she can re-emit a clean marker.

    Carries forward two propagated fields from the source outbox so the
    recovered marker's routing target survives across the retry round-trip:

      - `original_source` — the dispatcher's source (`beacon` on first round,
        or itself on subsequent rounds). Lets the notifier route Forge's
        recovered output back to the right agent (Beacon, not a dead-end).
      - `marker_error_count` — incremented each retry. When > MAX_MARKER_ERROR_RETRIES
        the notifier dead-letters to Beacon instead of looping back to Forge.

    Best-effort — daemon must not crash on a failed notify-write here. The
    underlying outbox is archived by the caller regardless.
    """
    agent = data.get('agent', 'forge')
    task_id = data.get('task_id', 'unknown')

    # Trace original dispatcher through the cascade. On the first malformed-
    # marker round, `original_source` isn't set on the outbox — Forge's outbox
    # source IS the original dispatcher. On subsequent rounds, `original_source`
    # was propagated by the prior _notify_forge_marker_error call.
    original_source = data.get('original_source') or data.get('source') or 'beacon'

    prev_count = data.get('marker_error_count', 0)
    if not isinstance(prev_count, int) or prev_count < 0:
        prev_count = 0
    new_count = prev_count + 1

    if new_count > MAX_MARKER_ERROR_RETRIES:
        # Defense against runaway loop — dead-letter to the original dispatcher
        # so a human can intervene. Don't keep asking Forge to retry.
        log(
            f'marker-error retries exhausted ({new_count}/{MAX_MARKER_ERROR_RETRIES}) '
            f'for task {task_id}; dead-lettering to {original_source}',
            'WARN',
        )
        _dead_letter_marker_error_to_dispatcher(
            data, original_source, err_msg, new_count,
        )
        return

    prompt = build_notify_prompt(
        intent='marker-error',
        sender='outbox-notifier',
        task_id=task_id,
        success=False,
        output=(data.get('result') or '')[:1000],
        intent_kwargs={
            'reason': err_msg,
            'task_id': task_id,
            'retry_count': new_count,
            'max_retries': MAX_MARKER_ERROR_RETRIES,
        },
    )
    notify_task: dict[str, Any] = {
        'task_id': f'marker-error-{task_id}-{new_count}',
        'prompt': prompt,
        'source': 'outbox-notifier',
        'intent': 'marker-error',
        '_notify_depth': 1,
        'original_source': original_source,
        'marker_error_count': new_count,
    }
    # Propagate clarification budget too — a marker-error round shouldn't reset
    # the count (Forge might re-emit a CLARIFY_REQUEST after recovering, and
    # Beacon needs the right budget for the next round).
    if data.get('clarification_count') is not None:
        notify_task['clarification_count'] = data['clarification_count']
    if data.get('max_clarifications') is not None:
        notify_task['max_clarifications'] = data['max_clarifications']
    if data.get('claude_session_id'):
        notify_task['session_id'] = data['claude_session_id']

    try:
        safe_write_inbox.safe_write_inbox(
            target_agent=agent,
            task_dict=notify_task,
            source_agent='outbox-notifier',
            filename=f'marker-error-{task_id}-{new_count}.json',
        )
        log(
            f'marker-error notify written to {agent} for task {task_id} '
            f'(retry {new_count}/{MAX_MARKER_ERROR_RETRIES})'
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'marker-error notify failed for {agent}/{task_id}: '
            f'{type(e).__name__}: {e}',
            'WARN',
        )


def _dead_letter_marker_error_to_dispatcher(
    data: dict[str, Any],
    original_source: str,
    err_msg: str,
    error_count: int,
) -> None:
    """Final-stop dead-letter when marker-error retries are exhausted.

    Sends a `dead-letter` intent notify to the original dispatcher (Beacon
    in the normal case) so a human can decide what to do next. The dispatch
    is closed — Forge will NOT see another retry from the notifier.
    """
    target_agent = _primary_agent_id(original_source)
    if target_agent is None:
        # Original source was a system entity — log and stop. No human-routable
        # endpoint to dead-letter to.
        log(
            f'marker-error dead-letter has no routable target '
            f'(original_source={original_source}); giving up on task '
            f'{data.get("task_id", "?")}',
            'ERROR',
        )
        return

    task_id = data.get('task_id', 'unknown')
    reason = (
        f'Forge produced {error_count} consecutive malformed markers on task '
        f'`{task_id}` (cap={MAX_MARKER_ERROR_RETRIES}). Final parse error: {err_msg}. '
        f'The dispatch is closed; inspect Forge\'s outputs in '
        f'~/agents/outboxes/forge/.archive/ and either fix her CLAUDE.md '
        f'marker discipline or revise the spec and re-dispatch.'
    )
    prompt = build_notify_prompt(
        intent='dead-letter',
        sender='outbox-notifier',
        task_id=task_id,
        success=False,
        output=(data.get('result') or '')[:1000],
        intent_kwargs={'reason': reason},
    )
    notify_task: dict[str, Any] = {
        'task_id': f'dead-letter-marker-{task_id}',
        'prompt': prompt,
        'source': 'outbox-notifier',
        'intent': 'dead-letter',
        '_notify_depth': 1,
    }
    if data.get('reply_chat_id') is not None:
        notify_task['reply_chat_id'] = data['reply_chat_id']

    try:
        safe_write_inbox.safe_write_inbox(
            target_agent=target_agent,
            task_dict=notify_task,
            source_agent='outbox-notifier',
            filename=f'dead-letter-marker-{task_id}.json',
        )
        log(
            f'marker-error dead-letter written to {target_agent} for task {task_id}'
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'marker-error dead-letter failed for {target_agent}/{task_id}: '
            f'{type(e).__name__}: {e}',
            'ERROR',
        )


def process_outbox(outbox_file: Path) -> str:
    """Process one result outbox. Returns one of:
       'notified' | 'notified-marker' | 'archived-no-notify' | 'depth-cap' |
       'skip-self' | 'partial-json' | 'notify-failed' | 'marker-error'.
    """
    try:
        data = json.loads(outbox_file.read_text())
    except (OSError, json.JSONDecodeError):
        return 'partial-json'

    agent = data.get('agent', '')
    source = data.get('source', '')
    if not agent or not source:
        _archive_outbox(outbox_file)
        return 'archived-no-notify'

    # Forge preflight marker check. Markers override default routing rules
    # because the preflight protocol is intentionally multi-hop and the
    # clarification budget on the envelope guards termination.
    marker_decision: Optional[dict[str, Any]] = None
    if agent == 'forge':
        try:
            marker_decision = _classify_forge_marker(data)
        except (fph.MalformedForgeMarker, fph.MultipleForgeMarkers) as e:
            log(
                f'forge marker error in {outbox_file.name}: '
                f'{type(e).__name__}: {e}',
                'WARN',
            )
            _notify_forge_marker_error(data, str(e))
            _archive_outbox(outbox_file)
            return 'marker-error'

    if marker_decision is not None:
        # Marker-driven routing. Always targets the original dispatcher
        # (Beacon today). If this outbox came from a marker-error retry,
        # `source` is the infra source `outbox-notifier` which has no
        # primary_agent_id — fall back to the propagated `original_source`
        # so the recovered marker reaches the right agent.
        routing_source = data.get('original_source') or source
        target_agent = _primary_agent_id(routing_source)
        if target_agent is None or target_agent == agent:
            # Can't route back (system source with no original_source) or
            # self-loop — archive.
            log(
                f'marker present but no routable target '
                f'(source={source}, original_source={data.get("original_source")}, '
                f'agent={agent}); archiving',
                'WARN',
            )
            _archive_outbox(outbox_file)
            return 'archived-no-notify'

        task_id = data.get('task_id', outbox_file.stem)
        prompt = build_notify_prompt(
            intent=marker_decision['intent'],
            sender=agent,
            task_id=task_id,
            success=data.get('exit_code', 0) == 0,
            output=_marker_output_for_prompt(data, marker_decision),
            error=data.get('error') or '',
            intent_kwargs=marker_decision['intent_kwargs'],
        )
        notify_task: dict[str, Any] = {
            'task_id': f'notify-{task_id}',
            'prompt': prompt,
            'source': marker_decision['notify_source'],
            'intent': marker_decision['intent'],
            # Depth still tracked for telemetry; budget supersedes the cap.
            '_notify_depth': _current_notify_depth(data) + 1,
        }
        if data.get('reply_chat_id') is not None:
            notify_task['reply_chat_id'] = data['reply_chat_id']
        if data.get('claude_session_id'):
            notify_task['session_id'] = data['claude_session_id']
        # Propagate clarification budget so the next leg has the counter.
        if marker_decision['next_clarification_count'] is not None:
            notify_task['clarification_count'] = marker_decision['next_clarification_count']
        if data.get('max_clarifications') is not None:
            notify_task['max_clarifications'] = data['max_clarifications']

        notify_filename = f'notify-{outbox_file.stem}.json'
        try:
            dest = safe_write_inbox.safe_write_inbox(
                target_agent=target_agent,
                task_dict=notify_task,
                source_agent=marker_decision['notify_source'],
                filename=notify_filename,
            )
            log(
                f'marker-notified {target_agent} <- {agent} '
                f'({marker_decision["notify_source"]}, '
                f'intent={marker_decision["intent"]}, file={dest.name})'
            )
        except (
            safe_write_inbox.DispatchRejected,
            safe_write_inbox.RoutingDenied,
        ) as e:
            log(
                f'marker notify failed for {outbox_file.name}: '
                f'{type(e).__name__}: {e}',
                'WARN',
            )
            _archive_outbox(outbox_file)
            return 'notify-failed'
        _archive_outbox(outbox_file)
        return 'notified-marker'

    # ---- Default (non-marker) routing path — unchanged from D3-notifier ----

    if not _should_notify_back(source, agent):
        _archive_outbox(outbox_file)
        return 'archived-no-notify'

    if _primary_agent_id(source) == agent:
        _archive_outbox(outbox_file)
        return 'skip-self'

    current_depth = _current_notify_depth(data)
    next_depth = current_depth + 1
    # `-question` source = clarification cascade leg (the agent we're processing
    # is answering Forge's CLARIFY_REQUEST). The `max_clarifications` budget on
    # the envelope guards termination; the depth cap would block a legitimate
    # multi-hop clarification flow, so bypass it for this leg only.
    is_clarification_answer = source.endswith('-question')
    if next_depth > MAX_NOTIFY_DEPTH and not is_clarification_answer:
        log(
            f'NOTIFY_CASCADE_DEPTH_EXCEEDED: {outbox_file.name} '
            f'(depth={current_depth}); skipping notify, archiving',
            'WARN',
        )
        _archive_outbox(outbox_file)
        return 'depth-cap'

    target_agent = _primary_agent_id(source)
    if target_agent is None:
        _archive_outbox(outbox_file)
        return 'archived-no-notify'

    notify_source = _notify_back_source(agent, source)
    # Pick intent + kwargs for the action block.
    if source.endswith('-question'):
        intent = 'clarification-response'
        remaining = fph.clarifications_remaining(data)
        intent_kwargs = {'remaining': remaining}
    else:
        intent = 'result-notification'
        intent_kwargs = {}

    task_id = data.get('task_id', outbox_file.stem)
    prompt = build_notify_prompt(
        intent=intent,
        sender=agent,
        task_id=task_id,
        success=data.get('exit_code', 0) == 0,
        output=data.get('result', '') or '',
        error=data.get('error') or '',
        intent_kwargs=intent_kwargs,
    )
    # Notify tasks omit `timeout` — they're not fresh dispatches with a
    # work budget, they're the receiver picking up an update. Our
    # dispatch_validator's `timeout` check is range-bounded
    # [MIN_TIMEOUT, MAX_TIMEOUT] when present, no-op when absent. Upstream's
    # `timeout: 0` ("no timeout") would fail our validator's 60s floor.
    notify_task = {
        'task_id': f'notify-{task_id}',
        'prompt': prompt,
        'source': notify_source,
        'intent': intent,
        '_notify_depth': next_depth,
    }
    if data.get('reply_chat_id') is not None:
        notify_task['reply_chat_id'] = data['reply_chat_id']
    # Propagate session_id so clarification-response delivery can resume
    # the original Forge session (commit 4b wires the watcher to honor it).
    if data.get('claude_session_id'):
        notify_task['session_id'] = data['claude_session_id']
    # Carry clarification budget across the cascade so it reaches Forge with
    # the correct count on the resume leg.
    if data.get('clarification_count') is not None:
        notify_task['clarification_count'] = data['clarification_count']
    if data.get('max_clarifications') is not None:
        notify_task['max_clarifications'] = data['max_clarifications']

    notify_filename = f'notify-{outbox_file.stem}.json'
    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent=target_agent,
            task_dict=notify_task,
            source_agent=notify_source,
            filename=notify_filename,
        )
        log(
            f'notified {target_agent} <- {agent} ({notify_source}, '
            f'depth={next_depth}, file={dest.name})'
        )
    except (safe_write_inbox.DispatchRejected, safe_write_inbox.RoutingDenied) as e:
        log(
            f'notify failed for {outbox_file.name}: {type(e).__name__}: {e}',
            'WARN',
        )
        _archive_outbox(outbox_file)
        return 'notify-failed'

    _archive_outbox(outbox_file)
    return 'notified'


def _marker_output_for_prompt(
    data: dict[str, Any],
    decision: dict[str, Any],
) -> str:
    """Pick the output text to put in the notify prompt body for a marker case.

    For clarification-request: just the question text (Beacon doesn't need
    Forge's full preflight ramble, only the specific ask).

    For ack-proceed/reject: the marker payload's summary/reason, so Beacon
    sees the context behind the decision.
    """
    payload = decision['payload']
    marker_type = decision['marker_type']
    if marker_type == 'clarify_request' and decision['intent'] == 'clarify':
        return payload.get('question', '(no question text)')
    if marker_type == 'proceed':
        return payload.get('preflight_summary', '(no summary)')
    if marker_type == 'reject':
        return payload.get('reason', '(no reason)')
    # Budget-exhausted clarify converted to clarification-exhausted
    return decision['intent_kwargs'].get('reason', '(no reason)')


def _load_dead_letter_state() -> dict[str, Any]:
    if not DEAD_LETTER_STATE.exists():
        return {'processed': []}
    try:
        return json.loads(DEAD_LETTER_STATE.read_text())
    except (json.JSONDecodeError, OSError):
        return {'processed': []}


def _save_dead_letter_state(state: dict[str, Any]) -> None:
    DEAD_LETTER_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEAD_LETTER_STATE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    tmp.rename(DEAD_LETTER_STATE)


def scan_dead_letters() -> int:
    """Notify dispatchers when their tasks land in `.invalid/`. Returns count notified."""
    state = _load_dead_letter_state()
    processed = set(state.get('processed', []))
    notified = 0
    seen_now = set()

    for agent in AGENT_IDS:
        invalid_dir = INBOXES_ROOT / agent / '.invalid'
        if not invalid_dir.exists():
            continue
        for invalid_file in invalid_dir.glob('*.json'):
            key = f'{agent}:{invalid_file.name}'
            seen_now.add(key)
            if key in processed:
                continue

            try:
                task_data = json.loads(invalid_file.read_text())
            except (OSError, json.JSONDecodeError):
                processed.add(key)
                continue

            original_source = task_data.get('source', '')
            target_agent = _primary_agent_id(original_source)
            if target_agent is None or target_agent == agent:
                # No dispatcher to notify (system source) or self-dispatch.
                processed.add(key)
                continue

            reason_file = invalid_file.with_suffix('.reason')
            reason_text = ''
            if reason_file.exists():
                try:
                    reason_text = reason_file.read_text().strip()
                except OSError:
                    pass

            original_prompt_excerpt = (task_data.get('prompt', '') or '')[:500]
            dl_reason = (
                f'your dispatch to {agent} (filename {invalid_file.name}) was '
                f'rejected by dispatch_validator and never reached the agent. '
                f'Validator reason: {reason_text or "(no reason recorded)"}. '
                f'No retry will happen automatically — inspect the task, fix '
                f'the issue (prompt length, schema, source enum, etc.), and '
                f're-dispatch if appropriate.'
            )
            dl_prompt = build_notify_prompt(
                intent='dead-letter',
                sender=agent,
                task_id=task_data.get('task_id', invalid_file.stem),
                success=False,
                output=f'Original prompt (first 500 chars):\n{original_prompt_excerpt}',
                intent_kwargs={'reason': dl_reason},
            )
            notify_task: dict[str, Any] = {
                'task_id': f'dead-letter-{invalid_file.stem}',
                'prompt': dl_prompt,
                'source': f'{agent}-result',
                'intent': 'dead-letter',
                '_notify_depth': 1,  # this IS a depth-1 message; further loops cap
            }
            if task_data.get('reply_chat_id') is not None:
                notify_task['reply_chat_id'] = task_data['reply_chat_id']

            notify_filename = f'notify-dead-letter-{invalid_file.stem}.json'
            try:
                safe_write_inbox.safe_write_inbox(
                    target_agent=target_agent,
                    task_dict=notify_task,
                    source_agent=f'{agent}-result',
                    filename=notify_filename,
                )
                log(
                    f'dead-letter notified {target_agent} <- {agent} for '
                    f'{invalid_file.name} (reason: {reason_text[:80]})'
                )
                notified += 1
            except (safe_write_inbox.DispatchRejected, safe_write_inbox.RoutingDenied) as e:
                log(
                    f'dead-letter notify failed for {invalid_file.name}: '
                    f'{type(e).__name__}: {e}',
                    'WARN',
                )
            # Mark as processed regardless of notify outcome — we never want
            # to retry-notify and amplify a real bug.
            processed.add(key)

    # GC: drop processed keys whose underlying .invalid file no longer exists.
    processed = {k for k in processed if k in seen_now}
    # Cap state file size with FIFO truncation.
    processed_list = sorted(processed)[-DEAD_LETTER_STATE_CAP:]
    state['processed'] = processed_list
    state['last_run'] = datetime.now(timezone.utc).isoformat()
    state['last_notified_count'] = notified
    _save_dead_letter_state(state)
    return notified


def _handle_sigterm(signum, frame):
    global _running
    _running = False
    log(f'received signal {signum}, exiting cleanly')


def main_loop() -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    ensure_dirs()
    log('outbox-notifier starting')
    while _running:
        if _emergency_halt_active():
            log('EMERGENCY_HALT active — exiting cleanly')
            return 0

        # Outbox results scan
        for agent in AGENT_IDS:
            outbox_dir = OUTBOXES_ROOT / agent
            if not outbox_dir.exists():
                continue
            for outbox_file in sorted(outbox_dir.glob('*.json')):
                if outbox_file.name.startswith('.'):
                    continue
                try:
                    process_outbox(outbox_file)
                except Exception as e:
                    log(
                        f'unexpected error processing {outbox_file.name}: '
                        f'{type(e).__name__}: {e}',
                        'ERROR',
                    )

        # Dead-letter scan
        try:
            scan_dead_letters()
        except Exception as e:
            log(f'dead-letter scan error: {type(e).__name__}: {e}', 'ERROR')

        # Sleep in short slices so SIGTERM is responsive.
        slept = 0.0
        while _running and slept < POLL_INTERVAL_SECONDS:
            time.sleep(0.5)
            slept += 0.5

    log('outbox-notifier exiting')
    return 0


if __name__ == '__main__':
    sys.exit(main_loop())
