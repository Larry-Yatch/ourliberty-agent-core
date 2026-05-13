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
import re
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

import beacon_approval_handler as approval  # noqa: E402
import dispatch_validator         # noqa: E402
import forge_preflight_handler as fph  # noqa: E402
import larry_alerts                # noqa: E402
import mirror_review_handler as mrh  # noqa: E402
import safe_write_inbox             # noqa: E402
import trust_policy                 # noqa: E402

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

# Cap on consecutive marker-error retries to Forge OR Mirror. Defense
# against a wedge loop where the agent keeps producing malformed markers
# (e.g., a CLAUDE.md bug or a session that's lost track of the grammar).
# When exceeded, the notifier dead-letters back to Beacon instead of
# retrying the agent again.
MAX_MARKER_ERROR_RETRIES = 3

# D3.5 commit 5c — inbound-intent values on a Beacon outbox that trigger
# the auto-replan extraction path. Today only `review-escalate` qualifies
# (Mirror flagged a finding that needs spec revision); future commits may
# add others (e.g., a Pulse digest-driven replan intent). The check is
# narrow on purpose — Beacon's chat-mode APPROVAL_REQUEST flow runs on
# her telegram bot and must NOT also fire here.
_BEACON_REPLAN_INBOUND_INTENTS = frozenset({'review-escalate'})

# D3.5 commit 5a — extract Forge's PR URL from a build-phase outbox result.
# Forge's CLAUDE.md mandates the build response START with `PR opened: <url>`
# (agents/forge/CLAUDE.md, Build phase protocol § "Ending your build response").
# Anchored to start-of-string (with optional leading whitespace) so a PR URL
# discussed in narrative ("I considered PR opened: <stale-url> from last week")
# doesn't false-match before the real URL. m-2 review fix.
_PR_URL_RE = re.compile(
    r'\A\s*PR opened:\s*(https://github\.com/[^\s]+/pull/\d+)',
)

# D3.5 commit 5b — extract Forge's revision-applied summary from a revision-
# phase outbox result. Forge's CLAUDE.md mandates revision responses START
# with `Revision N applied: <one-line summary>`. Strict per Larry's signoff:
# unlike the build-phase regex (which falls through to default routing when
# the prefix is missing, preserving the blocker-paragraph path), the revision
# phase has no documented blocker path — missing prefix triggers a
# marker-error dead-letter cascade back to Forge.
_REVISION_APPLIED_RE = re.compile(
    r'\A\s*Revision\s+(\d+)\s+applied:\s*(.+?)(?:\n|\Z)',
    re.IGNORECASE,
)

# D3.5 5b — read `loop_bounds.max_revisions` from config/agent-models.json so
# retuning the dial in config actually takes effect (M-4 review fix). Cached
# at module level since this fires on every Mirror review-request dispatch.
# Tests reset the cache via _invalidate_loop_bounds_cache.
_MODELS_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'agent-models.json'
_LOOP_BOUNDS_CACHE: dict[str, Any] = {}


def _invalidate_loop_bounds_cache() -> None:
    """Drop the cached loop_bounds. Tests use this between runs."""
    _LOOP_BOUNDS_CACHE.pop('config', None)


def _load_max_revisions_from_config() -> int:
    """Return `loop_bounds.max_revisions` from config/agent-models.json.

    Falls back to mrh.DEFAULT_MAX_REVISIONS when the config file is missing,
    malformed, or doesn't define the field. Cached — the file changes only
    when Larry retunes the dial + restarts the daemon (sync.timer pulls;
    daemon restart picks up the new value via this lazy load).

    D3.5 5b M-4 review fix: before this, _dispatch_mirror_review hardcoded
    `mrh.DEFAULT_MAX_REVISIONS` so config retuning had zero effect on actual
    behavior. The docs/tunables.md promise that the dial is tunable depends
    on this function reading the config file.
    """
    if 'config' not in _LOOP_BOUNDS_CACHE:
        try:
            cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
        _LOOP_BOUNDS_CACHE['config'] = cfg
    cfg = _LOOP_BOUNDS_CACHE['config']
    loop_bounds = cfg.get('loop_bounds') if isinstance(cfg, dict) else None
    if not isinstance(loop_bounds, dict):
        return mrh.DEFAULT_MAX_REVISIONS
    raw = loop_bounds.get('max_revisions')
    if not isinstance(raw, int) or raw < 0:
        return mrh.DEFAULT_MAX_REVISIONS
    return raw


def _load_max_replans_from_config() -> int:
    """Return `loop_bounds.max_replans` from config/agent-models.json.

    D3.5 5c. Same shape as `_load_max_revisions_from_config`. Falls back to
    `approval.DEFAULT_MAX_REPLANS` (=2) when the config file is missing,
    malformed, or doesn't define the field. Reads the same cached config so
    the two budgets stay synchronized to the same on-disk state.
    """
    if 'config' not in _LOOP_BOUNDS_CACHE:
        try:
            cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
        _LOOP_BOUNDS_CACHE['config'] = cfg
    cfg = _LOOP_BOUNDS_CACHE['config']
    loop_bounds = cfg.get('loop_bounds') if isinstance(cfg, dict) else None
    if not isinstance(loop_bounds, dict):
        return approval.DEFAULT_MAX_REPLANS
    raw = loop_bounds.get('max_replans')
    if not isinstance(raw, int) or raw < 0:
        return approval.DEFAULT_MAX_REPLANS
    return raw

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
        'marker (retry {retry_count} of {max_retries}). Error: {reason}. '
        'Re-read your CLAUDE.md marker-discipline section and re-emit EXACTLY one '
        'valid marker block with the required fields and matching '
        '`=== END_XXX ===` delimiter. After {max_retries} retries the dispatch '
        'will be closed and dead-lettered back to Beacon.'
    ),
    # D3.5 commit 5a — Mirror review marker intents. All four route to Beacon
    # as informational journal entries in 5a; the actual loop machinery
    # (revision dispatch to Forge in 5b, replan flow in 5c, auto-merge +
    # EMERGENCY_HALT trip in 5d) lands later.
    'review-pass': (
        'Mirror has APPROVED PR `{pr_url}` on task `{task_id}`. Summary: '
        '{summary}. Journal the approval. In 5a auto-merge is not yet wired, '
        'so Larry merges manually after seeing this notify. No further action '
        'from you.'
    ),
    'review-revision': (
        'Mirror has requested REVISION on PR `{pr_url}` for task `{task_id}` '
        '({finding_count} finding(s), severity={severity}, confidence={confidence}). '
        'The revision has been auto-dispatched to Forge — she will apply '
        'the findings in her existing build session, commit + push to the '
        'same branch (PR auto-updates), and Mirror will re-review. Journal '
        'the dispatch and await the re-review outcome. No manual action '
        'from you. (D3.5 5b: revision loop auto-wired; budget enforced by '
        'max_revisions in agent-models.json loop_bounds.)'
    ),
    'review-escalate': (
        'Mirror has ESCALATED PR `{pr_url}` on task `{task_id}` '
        '(severity={severity}, confidence={confidence}). Reason: {reason}. '
        'In 5a the replan flow is not yet wired — journal the escalation and '
        'decide manually: revise the spec (new APPROVAL_REQUEST) or push back '
        'with reasoning. 5c will wire the auto-replan loop.'
    ),
    'review-emergency-halt': (
        'Mirror has flagged EMERGENCY_HALT on PR `{pr_url}` for task `{task_id}`. '
        'Reason: {reason}. Evidence: {evidence}. In 5a the halt-file trip + '
        'priority DM are not yet wired — Larry will see this as a normal '
        'notify. Treat it as critical: review the PR immediately and decide '
        'whether to close it without merge. 5d will wire the automatic halt.'
    ),
    'replan-request': (
        'A downstream agent has requested a replan on task `{task_id}`. '
        'Reason: {reason}. Decide: revise the plan inline (new APPROVAL_REQUEST '
        'with adjusted scope) or push back to the requester. Bounded by '
        'max_replans on the envelope.'
    ),
}

# D3.5 5a-followup: per-intent DM templates for chain-completion DMs to Larry.
# Fires only for terminal-from-Larry's-perspective intents (his task ends here,
# either successfully or needing his attention). Mid-chain intents like
# ack-proceed / clarify / clarification-response do not DM because they're
# mechanics Larry doesn't need to see.
#
# Template fields drawn from the marker payload + envelope data (see
# `_render_dm_message`).
DM_TEMPLATES = {
    'review-pass': (
        'Mirror approved PR {pr_url} on task `{task_id}`.\n'
        'Summary: {summary}\n'
        'Ready for you to merge manually.'
    ),
    'review-escalate': (
        # m-6 review fix: lead line is cause-agnostic. The intent fires from
        # three scenarios — Mirror direct ESCALATE, low-confidence auto-
        # promote, budget exhausted — and the {reason} body carries the
        # specific cause. The old "Mirror escalated PR ..." lead was wrong
        # for the budget-exhaust case (Mirror said revision; system
        # downgraded). On phone, the lead is what Larry sees in the
        # notification preview.
        'Review escalated on PR {pr_url} (task `{task_id}`).\n'
        'Reason: {reason}\n'
        'Decide: revise the spec (new APPROVAL_REQUEST to Beacon) or push back.'
    ),
    'review-emergency-halt': (
        'EMERGENCY_HALT on PR {pr_url} (task `{task_id}`).\n'
        'Reason: {reason}\n'
        'Evidence: {evidence}\n'
        'Review the PR immediately and decide whether to close without merge.'
    ),
    'reject': (
        'Forge REJECTED task `{task_id}` at preflight.\n'
        'Reason: {reason}\n'
        'Either revise the spec and re-emit APPROVAL_REQUEST, or set aside.'
    ),
    'clarification-exhausted': (
        'Forge exhausted clarification budget on task `{task_id}`.\n'
        'Final question: {reason}\n'
        'Rewrite the spec more completely before re-dispatching.'
    ),
    # D3.5 5b-followup Bug C: dead-letter is now a terminal-from-Larry's-
    # perspective intent. The 5a-followup auto-DM pipe needs to surface
    # cascade-exhaust events (Forge/Mirror marker-error retries hit the
    # cap; dispatch closed; no PR opened) so the chat thread that initiated
    # the work gets a closing notification — not silence. Without this,
    # Larry approved-then-nothing for the failed 5b live test.
    'dead-letter': (
        'Dispatch on task `{task_id}` failed after {retry_count} '
        'marker-error retries. The dispatch is closed; no PR was opened.\n'
        'Reason: {reason}\n'
        'Either revise the spec (sharper instructions or a different '
        'approach) and re-emit APPROVAL_REQUEST to Beacon, or set this '
        'task aside.'
    ),
}

# Subset of intents that produce a closing DM to the originating chat. Other
# intents are mid-chain mechanics that don't warrant Larry's attention.
TERMINAL_DM_INTENTS = frozenset(DM_TEMPLATES.keys())


def _render_dm_message(intent: str, decision: dict[str, Any]) -> Optional[str]:
    """Render the per-intent DM body. Returns None on missing template or
    unrenderable fields (degrades to silence rather than crashing the daemon)."""
    template = DM_TEMPLATES.get(intent)
    if template is None:
        return None
    payload = decision.get('payload') or {}
    intent_kwargs = decision.get('intent_kwargs') or {}
    # Merge envelope payload + intent_kwargs so the template can pull from
    # either. intent_kwargs wins on conflicts (the classifier already
    # normalized fields like pr_url + finding_count + reason).
    fields: dict[str, Any] = {
        'task_id': payload.get('task_id', '?'),
        'pr_url': payload.get('pr_url', '?'),
        'summary': payload.get('summary', '?'),
        'reason': payload.get('reason', '?'),
        'evidence': payload.get('evidence', '?'),
        'severity': payload.get('severity', '?'),
        'confidence': payload.get('confidence', '?'),
        'finding_count': 0,
    }
    findings = payload.get('findings')
    if isinstance(findings, list):
        fields['finding_count'] = len(findings)
    # intent_kwargs from classifier may have pre-rendered values (e.g.
    # auto-promote reason). Apply on top.
    fields.update({k: v for k, v in intent_kwargs.items() if v is not None})
    try:
        return template.format(**fields)
    except (KeyError, IndexError):
        # Missing field — degrade rather than crash.
        return None


def _maybe_dm_larry(
    data: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    """Append a chain-completion DM to larry-alerts.jsonl when the marker
    intent is terminal-from-Larry's-perspective AND the source envelope
    carries reply_chat_id.

    Phase D3.5 5a-followup. Fires AFTER the marker-driven inter-agent
    notify has been written; this is purely the Larry-facing side. Failure
    here is non-fatal (log + continue) — the inter-agent chain has already
    completed, the DM is the courtesy notification.

    The reply_chat_id propagation through every hop (per dispatch_build_phase
    + dispatch_mirror_review + marker-routing + default-routing blocks above)
    keeps the chat thread reachable from the terminal hop.
    """
    intent = decision.get('intent')
    if intent not in TERMINAL_DM_INTENTS:
        return
    chat_id = data.get('reply_chat_id')
    if chat_id is None:
        # No originating chat thread (autonomous Pulse-initiated runs, or
        # Bug A unfixed). Silent skip — no DM target.
        return
    if not isinstance(chat_id, int):
        log(
            f'reply_chat_id is not an int ({type(chat_id).__name__}) on task '
            f'{data.get("task_id", "?")}; skipping completion DM',
            'WARN',
        )
        return
    message = _render_dm_message(intent, decision)
    if message is None:
        log(
            f'DM template missing or unrenderable for intent={intent} on task '
            f'{data.get("task_id", "?")}; skipping completion DM',
            'WARN',
        )
        return
    task_id = data.get('task_id', 'unknown')
    ok = larry_alerts.append_notification(
        source='outbox-notifier',
        intent=intent,
        message=message,
        chat_id=chat_id,
        task_id=task_id,
    )
    if ok:
        log(
            f'queued completion DM to chat {chat_id} for intent={intent} '
            f'(task={task_id})',
        )
    else:
        log(
            f'completion DM append failed for chat {chat_id} (intent={intent}, '
            f'task={task_id}); inter-agent chain completed normally, DM dropped',
            'WARN',
        )


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
        # D3.5 commit 5a — preflight-discipline runtime gate (deferred from
        # 4b's followup-2 doc). A `phase=preflight` outbox MUST end with one
        # of PROCEED / CLARIFY_REQUEST / REJECT. If Forge fast-pathed past
        # the marker block (e.g., started writing code during preflight),
        # the dispatch has no decision recorded — dead-letter back via the
        # marker-error cascade with a sharper "decide, don't act" prompt.
        # 5a ships strict mode (per the d3-5-plan VALUES sign-off — soft
        # warning lets Forge keep fast-pathing; strict costs one extra
        # invocation, cheap insurance until Forge has 20+ disciplined runs).
        if data.get('phase') == 'preflight':
            raise fph.MalformedForgeMarker(
                'phase=preflight requires ONE marker block at end of response '
                '(PROCEED / CLARIFY_REQUEST / REJECT) — none found. Re-read '
                "agents/forge/CLAUDE.md 'Preflight discipline' — preflight "
                'decides, it does not act.'
            )
        return None

    # Phase D3 commit 4b: tighten marker discipline. Forge's marker payload
    # MUST carry the same task_id as the envelope. The 4a smoke surfaced a
    # drift where Forge emitted a non-matching task_id; routing happened to
    # work via the outbox filename stem, but Forge cannot rely on that path.
    # Raise as MalformedForgeMarker so the marker-error cascade fires and
    # Forge re-emits with the correct task_id.
    envelope_task_id = data.get('task_id')
    marker_task_id = (
        payload.get('task_id') if isinstance(payload, dict) else None
    )
    if (
        envelope_task_id is not None
        and marker_task_id is not None
        and marker_task_id != envelope_task_id
    ):
        raise fph.MalformedForgeMarker(
            f'marker task_id ({marker_task_id!r}) does not match envelope '
            f'task_id ({envelope_task_id!r})'
        )

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
    # D3.5 5b-followup Bug B: keep envelope task_id as the ORIGINAL task_id
    # across retries. The previous wrapped form (`marker-error-<orig>-<N>`)
    # broke the 4b task_id-mismatch check: Forge correctly emits her marker
    # with the original task_id (that's the actual semantic task), but the
    # envelope had the wrapper name, so the mismatch check rejected every
    # retry. Cascade never recovered from real preflight failures.
    # Retry tracking lives in `marker_error_count`; filename uses
    # `-{new_count}` suffix for uniqueness. Now Forge's marker contract
    # (task_id matches envelope) holds consistently across retries.
    notify_task: dict[str, Any] = {
        'task_id': task_id,
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
    # Phase D3 commit 4b: propagate target_repo + branch so the retry task
    # passes the watcher's worktree gate. Without these, an agent with
    # `worktree_enabled: true` (Forge) rejects the marker-error retry as
    # `target_repo: no canonical path` and the malformed-marker recovery
    # silently dies — same shape as the 4a marker-error black-hole bug.
    if data.get('target_repo'):
        notify_task['target_repo'] = data['target_repo']
    if data.get('branch'):
        notify_task['branch'] = data['branch']
    # D3.5 5b M-2 review fix: propagate revision-phase envelope fields so
    # a marker-error retry triggered by the revision-preamble strict gate
    # doesn't lose Forge's revision context. Without these the retry task
    # arrives with no `phase` (watcher resume gate refuses --resume),
    # no `forge_build_session_id` (next revision-dispatch can't thread),
    # no `revision_count` / `max_revisions` (budget eval starts fresh),
    # no `pr_url` (Forge has nothing to point Mirror at). Without these
    # the revision-phase marker-error path is a dead end.
    if data.get('phase'):
        notify_task['phase'] = data['phase']
    if data.get('forge_build_session_id'):
        notify_task['forge_build_session_id'] = data['forge_build_session_id']
    if data.get('revision_count') is not None:
        notify_task['revision_count'] = data['revision_count']
    if data.get('max_revisions') is not None:
        notify_task['max_revisions'] = data['max_revisions']
    if data.get('pr_url'):
        notify_task['pr_url'] = data['pr_url']
    # D3.5 5b-followup Bug E (live re-test): propagate reply_chat_id so a
    # marker-error retry doesn't drop the originating Telegram chat thread.
    # _notify_mirror_marker_error has this; _notify_forge_marker_error was
    # missing it — discovered when the 2026-05-13 re-test completed PR #5
    # successfully but Larry got no closing DM because reply_chat_id went
    # to None on the first marker-error retry and never recovered through
    # the chain.
    if data.get('reply_chat_id') is not None:
        notify_task['reply_chat_id'] = data['reply_chat_id']

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

    # D3.5 5b-followup Bug C: also DM Larry on cascade exhaust. The chat
    # thread that initiated the dispatch deserves a closing notification
    # — without this, "Larry approved, then silence" (the failed live
    # test 2026-05-13 was exactly this shape). Build a synthetic decision
    # dict so _maybe_dm_larry's render pipeline works.
    synthetic_decision = {
        'intent': 'dead-letter',
        'payload': {'task_id': task_id},
        'intent_kwargs': {
            'task_id': task_id,
            'reason': reason,
            'retry_count': error_count,
        },
    }
    _maybe_dm_larry(data, synthetic_decision)


def _dispatch_build_phase(data: dict[str, Any]) -> None:
    """Write a build-phase task to Forge's inbox after a PROCEED marker.

    Phase D3 commit 4b. The signed-off design is two invocations with
    ``--resume``: preflight first, then build. The notify-to-Beacon (above)
    is informational (Beacon journals "Forge is proceeding"); this is what
    actually triggers code work.

    ``source`` is ``beacon`` because Beacon is the logical dispatcher —
    her APPROVAL_REQUEST + Larry's approval authorized the work. The
    notifier acts as Beacon's mechanical extension. Audit field
    ``dispatched_by: 'outbox-notifier'`` records the actual writer.

    Failure to write the build-phase task is logged WARN and non-fatal.
    The notify-to-Beacon above has already informed her that Forge
    ack-proceeded; Larry sees the gap in the log and can manually
    re-dispatch.
    """
    task_id = data.get('task_id') or 'unknown'
    preflight_session = data.get('claude_session_id')
    if not preflight_session:
        log(
            f'PROCEED marker for task {task_id} has no claude_session_id; '
            f'build-phase dispatch would have nothing to --resume — '
            f'skipping. Larry should manually re-dispatch.',
            'WARN',
        )
        return

    target_repo = data.get('target_repo')
    branch = data.get('branch')
    pr_title = data.get('pr_title')
    pr_body = data.get('pr_body')
    max_clar = data.get('max_clarifications')

    # The build prompt is the next user turn in the resumed claude session;
    # the actual `target_repo` value is in the worktree's git config and
    # Forge already read it during preflight. Keep this terse — the
    # protocol details live in agents/forge/CLAUDE.md's Build phase section.
    build_prompt_lines = [
        'Build phase. Your preflight returned PROCEED; the plan you '
        'confirmed is the contract. Execute it now in this worktree.',
        '',
        f'Task: `{task_id}`',
    ]
    if branch:
        build_prompt_lines.append(f'Branch: `{branch}`')
    if pr_title:
        build_prompt_lines.append(f'PR title: `{pr_title}`')
    build_prompt_lines.extend([
        '',
        'Follow the Build phase protocol in your CLAUDE.md: implement '
        'changes → git add / git commit (conventional-commit style) → '
        'git push -u origin <branch> → gh pr create. Emit a single '
        'result block at the end starting with `PR opened: <url>`.',
    ])
    build_prompt = '\n'.join(build_prompt_lines)

    build_task: dict[str, Any] = {
        'task_id': task_id,
        'prompt': build_prompt,
        'source': 'beacon',
        'phase': 'build',
        'session_id': preflight_session,
        'dispatched_by': 'outbox-notifier',
    }
    if target_repo:
        build_task['target_repo'] = target_repo
    if branch:
        build_task['branch'] = branch
    if pr_title:
        build_task['pr_title'] = pr_title
    if pr_body:
        build_task['pr_body'] = pr_body
    if max_clar is not None:
        build_task['max_clarifications'] = max_clar
    if data.get('reply_chat_id') is not None:
        build_task['reply_chat_id'] = data['reply_chat_id']
    # D3.5 5c C-1 review fix: propagate replan_count + max_replans through
    # the preflight→build hop. Without this, an approval emitted by the 5c
    # replan path (which set replan_count > 0 on the original Forge task
    # envelope) would land in Forge's preflight outbox correctly but get
    # reset to 0 on the build dispatch — breaking the budget enforcement
    # on the next Mirror REVIEW_ESCALATE leg. Symmetric with how
    # revision_count rides through _dispatch_revision_to_forge.
    if data.get('replan_count') is not None:
        build_task['replan_count'] = data['replan_count']
    if data.get('max_replans') is not None:
        build_task['max_replans'] = data['max_replans']

    build_filename = f'build-{task_id}.json'
    # Idempotency check (4b review fix): if the build task is already
    # present in Forge's inbox OR was already archived, skip re-dispatch.
    # Guards against the notifier crashing between dispatch and archive
    # of the preflight outbox: on restart, re-processing the same outbox
    # would otherwise write a second build task that would resume an
    # already-terminated session against potentially-dirty worktree state.
    forge_inbox = safe_write_inbox.INBOXES_ROOT / 'forge'
    if (
        (forge_inbox / build_filename).exists()
        or (forge_inbox / '.archive' / build_filename).exists()
        # D3.5 5a M-1 review fix: also check .invalid/ — a prior dispatch that
        # was validator-rejected lives there, and we shouldn't re-dispatch a
        # duplicate that will hit the same rejection.
        or (forge_inbox / '.invalid' / build_filename).exists()
    ):
        log(
            f'build-phase already dispatched for task {task_id} '
            f'(file or archive or .invalid present); skipping duplicate write'
        )
        return

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='forge',
            task_dict=build_task,
            source_agent='beacon',
            filename=build_filename,
        )
        log(
            f'build-phase dispatched forge <- beacon '
            f'(task={task_id}, file={dest.name}, '
            f'resume={preflight_session[:12]}...)'
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'build-phase dispatch FAILED for task {task_id}: '
            f'{type(e).__name__}: {e}. Beacon was already notified of '
            f'PROCEED; Larry must manually re-dispatch build phase.',
            'WARN',
        )


def _classify_mirror_marker(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Inspect a Mirror outbox for a review marker. Returns routing decision or None.

    Phase D3.5 commit 5a. Parallel to `_classify_forge_marker` — same return
    shape, same envelope-vs-marker task_id discipline, same dead-letter
    cascade behavior on malformed markers.

    The confidence-promote rule is applied internally: a REVIEW_REVISION
    with `confidence: low` returns the routing decision shaped as an
    ESCALATE (intent=`review-escalate`) but with `auto_promoted=True` so
    downstream logging records that Mirror said revise + we routed escalate.

    Raises `mrh.MalformedMirrorMarker` or `mrh.MultipleMirrorMarkers` if the
    marker block is present but unparseable. The caller dead-letters those
    back to Mirror so she can re-emit cleanly.

    Returns None if the outbox has no marker (Mirror's chat-mode outputs
    take the default routing path).
    """
    result_text = data.get('result', '')
    if not isinstance(result_text, str) or not result_text.strip():
        return None

    marker_type, payload, _narrative = mrh.parse_mirror_marker(result_text)
    if marker_type is None:
        return None

    # Marker discipline: payload task_id MUST match envelope task_id
    # (same shape as Forge handler's 4b check). Drift here means Mirror
    # reviewed the wrong PR; route as marker-error so she re-emits.
    envelope_task_id = data.get('task_id')
    marker_task_id = (
        payload.get('task_id') if isinstance(payload, dict) else None
    )
    if (
        envelope_task_id is not None
        and marker_task_id is not None
        and marker_task_id != envelope_task_id
    ):
        raise mrh.MalformedMirrorMarker(
            f'marker task_id ({marker_task_id!r}) does not match envelope '
            f'task_id ({envelope_task_id!r})'
        )

    agent = data.get('agent', 'mirror')
    auto_promoted = mrh.should_auto_promote(marker_type, payload)

    # D3.5 5b: evaluate revision budget for REVIEW_REVISION markers. If the
    # next dispatch would exceed max_revisions, downgrade to ESCALATE-shaped
    # routing (Beacon's existing handler) with a budget-exhausted reason
    # field. No new intent vocabulary per Larry's 5b signoff (option A).
    # m-5 review fix: evaluate even when auto_promoted, so the combined
    # case (low confidence AND budget exhausted) renders the budget reason
    # (the stronger termination signal) rather than just the auto-promote
    # reason. derive_intent handles both flags identically (escalate); only
    # the reason text differs.
    budget_exhausted = False
    if marker_type == 'review_revision':
        decision_str, _next_count, _max_count = mrh.evaluate_revision_budget(data)
        if decision_str == 'exhausted':
            budget_exhausted = True

    # Per-marker intent_kwargs for INTENT_ACTION_BLOCKS rendering. Keep
    # aligned with the templates in INTENT_ACTION_BLOCKS — missing keys
    # degrade gracefully via build_notify_prompt's exception handler, but
    # cleaner to supply them upfront.
    pr_url = payload.get('pr_url', '(no PR URL)') if isinstance(payload, dict) else '(no PR URL)'

    if marker_type == 'review_pass':
        intent_kwargs = {
            'pr_url': pr_url,
            'summary': payload.get('summary', '(no summary)'),
        }
    elif marker_type == 'review_revision':
        # m-5 review fix: budget_exhausted is the stronger termination
        # signal — render its reason even when auto_promoted is also True
        # (low-confidence revision at round 3+ → user should see "loop
        # exhausted on round N", not "Mirror was uncertain"; the latter is
        # implied by the former).
        if budget_exhausted:
            current = data.get('revision_count', 0)
            if not isinstance(current, int) or current < 0:
                current = 0
            max_count = data.get('max_revisions', mrh.DEFAULT_MAX_REVISIONS)
            if not isinstance(max_count, int) or max_count < 0:
                max_count = mrh.DEFAULT_MAX_REVISIONS
            reason = mrh.build_budget_exhausted_reason(
                payload, current + 1, max_count,
            )
            if auto_promoted:
                # Append a note that the low-confidence promote ALSO would
                # have triggered escalate — preserves the audit trail.
                reason = (
                    reason
                    + ' (Mirror also flagged this REVISION with '
                    'confidence: low — the auto-promote rule would have '
                    'routed it to ESCALATE regardless of budget.)'
                )
            intent_kwargs = {
                'pr_url': pr_url,
                'severity': payload.get('severity', '?'),
                'confidence': payload.get('confidence', '?'),
                'reason': reason,
            }
        elif auto_promoted:
            intent_kwargs = {
                'pr_url': pr_url,
                'severity': payload.get('severity', '?'),
                'confidence': payload.get('confidence', '?'),
                'reason': mrh.build_auto_promote_reason(payload),
            }
        else:
            findings = payload.get('findings') or []
            finding_count = len(findings) if isinstance(findings, list) else 0
            intent_kwargs = {
                'pr_url': pr_url,
                'finding_count': finding_count,
                'severity': payload.get('severity', '?'),
                'confidence': payload.get('confidence', '?'),
            }
    elif marker_type == 'review_escalate':
        intent_kwargs = {
            'pr_url': pr_url,
            'severity': payload.get('severity', '?'),
            'confidence': payload.get('confidence', '?'),
            'reason': payload.get('reason', '(no reason)'),
        }
    elif marker_type == 'review_emergency_halt':
        intent_kwargs = {
            'pr_url': pr_url,
            'reason': payload.get('reason', '(no reason)'),
            'evidence': payload.get('evidence', '(no evidence)'),
        }
    else:
        # Defensive: future marker types should at minimum render the pr_url.
        intent_kwargs = {'pr_url': pr_url}

    return {
        'marker_type': marker_type,
        'payload': payload,
        'intent': mrh.derive_intent(
            marker_type,
            auto_promoted=auto_promoted,
            budget_exhausted=budget_exhausted,
        ),
        'notify_source': mrh.derive_notify_source(agent),
        'intent_kwargs': intent_kwargs,
        'auto_promoted': auto_promoted,
        'budget_exhausted': budget_exhausted,
        # Parallel field to Forge's clarification_count for cascade plumbing.
        'next_clarification_count': None,
    }


def _notify_mirror_marker_error(data: dict[str, Any], err_msg: str) -> None:
    """Write a marker-error notify back to Mirror so she can re-emit a clean marker.

    Phase D3.5 commit 5a. Parallel to `_notify_forge_marker_error` — same
    retry counter on the envelope (`marker_error_count`), same MAX cap,
    same dead-letter-to-original-dispatcher behavior on exhaust.

    The notify-prompt template (`marker-error` intent) is shared with Forge
    — agent-agnostic wording so both agents see the same retry shape. The
    receiver decides which marker grammar to re-emit by reading their own
    CLAUDE.md marker-discipline section.
    """
    agent = data.get('agent', 'mirror')
    task_id = data.get('task_id', 'unknown')

    # Trace original dispatcher (same pattern as Forge handler — on the
    # first malformed-marker round, source IS the original dispatcher; on
    # subsequent rounds it propagated via original_source).
    original_source = data.get('original_source') or data.get('source') or 'beacon'

    prev_count = data.get('marker_error_count', 0)
    if not isinstance(prev_count, int) or prev_count < 0:
        prev_count = 0
    new_count = prev_count + 1

    if new_count > MAX_MARKER_ERROR_RETRIES:
        log(
            f'marker-error retries exhausted ({new_count}/{MAX_MARKER_ERROR_RETRIES}) '
            f'for task {task_id} on agent {agent}; dead-lettering to {original_source}',
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
    # D3.5 5b-followup Bug B: same fix as Forge equivalent — keep envelope
    # task_id as the ORIGINAL task_id across retries (was wrapped before;
    # broke Mirror's marker contract). Retry tracking via marker_error_count;
    # filename uniqueness via the `-{new_count}` filename suffix.
    notify_task: dict[str, Any] = {
        'task_id': task_id,
        'prompt': prompt,
        'source': 'outbox-notifier',
        'intent': 'marker-error',
        '_notify_depth': 1,
        'original_source': original_source,
        'marker_error_count': new_count,
    }
    # Propagate envelope fields the agent needs to keep working on the same
    # task (session_id for --resume, target_repo + branch for worktree gating).
    # Same shape as the Forge marker-error path.
    if data.get('claude_session_id'):
        notify_task['session_id'] = data['claude_session_id']
    if data.get('target_repo'):
        notify_task['target_repo'] = data['target_repo']
    if data.get('branch'):
        notify_task['branch'] = data['branch']
    if data.get('pr_url'):
        notify_task['pr_url'] = data['pr_url']
    # Revision counters propagate too — a marker-error round shouldn't
    # reset the revision budget mid-review.
    if data.get('revision_count') is not None:
        notify_task['revision_count'] = data['revision_count']
    if data.get('max_revisions') is not None:
        notify_task['max_revisions'] = data['max_revisions']
    # D3.5 5a M-3 review fix: propagate reply_chat_id so a Telegram-initiated
    # review whose marker errors three-strikes still closes the chat thread
    # via the eventual dead-letter to Beacon. Without this the user-facing
    # DM thread silently ends.
    if data.get('reply_chat_id') is not None:
        notify_task['reply_chat_id'] = data['reply_chat_id']
    # D3.5 5b M-7 (second-pass review fix): propagate forge_build_session_id
    # and phase. Without these, a Mirror marker-error retry that emits a
    # clean REVIEW_REVISION on the second try would have no
    # forge_build_session_id on the envelope — `_build_outbox` propagates
    # only what's on the task — and `_dispatch_revision_to_forge` would
    # silently skip (no session to --resume). Same shape as the C-1
    # propagation gap, on Mirror's side. `phase` is also missing — without
    # it, Mirror's retry task arrives with no phase context.
    if data.get('forge_build_session_id'):
        notify_task['forge_build_session_id'] = data['forge_build_session_id']
    if data.get('phase'):
        notify_task['phase'] = data['phase']

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


def _extract_pr_url_from_build_result(result_text: str) -> Optional[str]:
    """Return the PR URL from a Forge build-phase outbox, or None if absent.

    Phase D3.5 commit 5a. Forge's CLAUDE.md mandates the build response
    START with `PR opened: <url>`. Returns the first match — narrative
    text below the URL is not searched. Returns None on empty/None input
    or no match.
    """
    if not isinstance(result_text, str) or not result_text:
        return None
    m = _PR_URL_RE.search(result_text)
    if not m:
        return None
    return m.group(1)


def _dispatch_mirror_review(data: dict[str, Any], pr_url: str) -> None:
    """Write a review-request task to Mirror's inbox after Forge opens a PR.

    Phase D3.5 commit 5a. Fires inside `process_outbox` when Forge's
    `phase=build` outbox carries `PR opened: <url>` in result. Same shape
    as `_dispatch_build_phase`: a single new dispatch keyed by the same
    task_id; the notify-to-Beacon from the default routing path still
    fires alongside (Beacon journals "Forge opened PR #N"; this dispatch
    starts Mirror's review).

    `source` is `beacon` because Beacon is the logical dispatcher (her
    APPROVAL_REQUEST authorized the work that produced the PR). The
    `dispatched_by: 'outbox-notifier'` audit field records the actual
    writer.

    Failure to write the review-request is logged WARN and non-fatal —
    the notify-to-Beacon above has already informed her of the PR; Larry
    sees the gap and can manually re-dispatch.
    """
    task_id = data.get('task_id') or 'unknown'
    target_repo = data.get('target_repo')
    if not target_repo:
        # Without target_repo, Mirror's worktree gate (now active per 5a's
        # agent-models.json change) rejects the review task as "no canonical
        # path." Surface the gap to Larry rather than silently dropping.
        log(
            f'PR opened on task {task_id} but no target_repo on envelope; '
            f'cannot dispatch review (Mirror requires target_repo for '
            f'worktree gating). Larry must manually re-dispatch.',
            'WARN',
        )
        return

    branch = data.get('branch')
    # max_revisions sourced from config/agent-models.json `loop_bounds` —
    # propagated through the dispatch envelope so the budget is consistent
    # across the full review/revision cascade. M-4 review fix: actually
    # reads the config file (was hardcoded to DEFAULT_MAX_REVISIONS in 5a).
    max_revisions = _load_max_revisions_from_config()

    review_prompt_lines = [
        f'Review phase. Forge has opened PR `{pr_url}` for task `{task_id}`. '
        f'Your job: verify this PR against the spec from the original '
        f'APPROVAL_REQUEST and emit one marker block (PASS / REVISION / '
        f'ESCALATE / EMERGENCY_HALT).',
        '',
        f'Task: `{task_id}`',
        f'PR: {pr_url}',
    ]
    if branch:
        review_prompt_lines.append(f'Branch: `{branch}`')
    if target_repo:
        review_prompt_lines.append(f'Target repo: `{target_repo}`')
    review_prompt_lines.extend([
        '',
        'Follow the Review protocol in your CLAUDE.md: read the spec from '
        'the dispatch context, fetch the PR diff (`gh pr diff <N>`), '
        'optionally `gh pr checkout <N>` and run tests in your worktree '
        'if you need to verify behavior. Emit a single marker block at '
        'the end (REVIEW_PASS / REVIEW_REVISION / REVIEW_ESCALATE / '
        f'REVIEW_EMERGENCY_HALT). You have {max_revisions} revision rounds '
        'budget — set confidence thoughtfully (low-confidence revisions '
        'auto-promote to escalate).',
    ])
    review_prompt = '\n'.join(review_prompt_lines)

    review_task: dict[str, Any] = {
        'task_id': task_id,
        'prompt': review_prompt,
        'source': 'beacon',
        'phase': 'review',
        'pr_url': pr_url,
        'target_repo': target_repo,
        'revision_count': 0,
        'max_revisions': max_revisions,
        'dispatched_by': 'outbox-notifier',
    }
    if branch:
        review_task['branch'] = branch
    if data.get('reply_chat_id') is not None:
        review_task['reply_chat_id'] = data['reply_chat_id']
    # D3.5 5a M-2 review fix: propagate the same envelope fields
    # _dispatch_build_phase does. Without these, a future Mirror REVIEW_QUESTION
    # round-trip (5b) loses the PR metadata when answering back to Beacon,
    # and the worktree gate rejects with "no canonical path" — same shape as
    # the 4a marker-error black hole.
    for f_name in ('pr_title', 'pr_body', 'max_clarifications'):
        if data.get(f_name) is not None:
            review_task[f_name] = data[f_name]
    # D3.5 5b: thread Forge's build session_id through Mirror's envelope as
    # `forge_build_session_id` so a downstream REVIEW_REVISION can resume
    # Forge's session for the revision dispatch. Without this, the revision
    # task can't --resume the right conversation and Forge starts fresh,
    # losing all the build context she'd already loaded.
    if data.get('claude_session_id'):
        review_task['forge_build_session_id'] = data['claude_session_id']
    # D3.5 5c C-1 review fix: propagate replan_count + max_replans through
    # the build→review hop. Without this, Mirror's REVIEW_ESCALATE outbox
    # would carry replan_count=0 on the second-loop iteration, defeating
    # the budget cap. Symmetric with the preflight→build propagation.
    if data.get('replan_count') is not None:
        review_task['replan_count'] = data['replan_count']
    if data.get('max_replans') is not None:
        review_task['max_replans'] = data['max_replans']

    review_filename = f'review-{task_id}.json'
    # Idempotency check (same pattern as _dispatch_build_phase): if the
    # review task is already in Mirror's inbox OR archived, skip. Guards
    # against the notifier crashing between dispatch and archive of the
    # build-phase outbox — re-processing would otherwise spawn a duplicate
    # Mirror review of a PR she's already started reviewing.
    mirror_inbox = safe_write_inbox.INBOXES_ROOT / 'mirror'
    if (
        (mirror_inbox / review_filename).exists()
        or (mirror_inbox / '.archive' / review_filename).exists()
        # D3.5 5a M-1 review fix: also check .invalid/ — a prior dispatch
        # that was validator-rejected lives there. Don't re-dispatch a
        # duplicate that will hit the same rejection.
        or (mirror_inbox / '.invalid' / review_filename).exists()
    ):
        log(
            f'review-request already dispatched for task {task_id} '
            f'(file or archive or .invalid present); skipping duplicate write'
        )
        return

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='mirror',
            task_dict=review_task,
            source_agent='beacon',
            filename=review_filename,
        )
        log(
            f'review-request dispatched mirror <- beacon '
            f'(task={task_id}, file={dest.name}, pr={pr_url})'
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'review-request dispatch FAILED for task {task_id}: '
            f'{type(e).__name__}: {e}. Beacon was already notified of '
            f'PR opened; Larry must manually re-dispatch review.',
            'WARN',
        )


def _extract_revision_summary_from_result(
    result_text: str,
) -> Optional[tuple[int, str]]:
    """Parse Forge's revision-phase outbox preamble. Returns (round_num, summary) or None.

    D3.5 commit 5b. Forge's CLAUDE.md mandates revision responses START with
    `Revision N applied: <summary>` (case-insensitive, N is integer round
    number). Anchored to start-of-string with leading whitespace tolerance,
    same shape as `_extract_pr_url_from_build_result`. Returns None when
    the prefix is missing OR malformed — the caller treats None as a
    discipline-violation and dead-letters via marker-error cascade (strict
    mode per Larry's 5b signoff).
    """
    if not isinstance(result_text, str) or not result_text:
        return None
    m = _REVISION_APPLIED_RE.search(result_text)
    if not m:
        return None
    try:
        round_num = int(m.group(1))
    except (TypeError, ValueError):
        return None
    summary = m.group(2).strip()
    return round_num, summary


def _dispatch_revision_to_forge(
    data: dict[str, Any], decision: dict[str, Any],
) -> None:
    """Write a revision-task to Forge's inbox after Mirror's REVIEW_REVISION.

    D3.5 commit 5b. Parallel to `_dispatch_build_phase`: same task_id, same
    branch, --resume against Forge's build session. The marker's findings
    serialize into the prompt so Forge has structured input on what to fix.

    Pulls `forge_build_session_id` from Mirror's outbox envelope (threaded
    through 5a's `_dispatch_mirror_review` → propagated via _build_outbox).
    Without it, the revision task can't --resume the right conversation;
    Forge starts fresh and loses her build context.

    Caller responsibility: only invoke when `decision['marker_type'] ==
    'review_revision'`, `not decision['auto_promoted']`, `not
    decision['budget_exhausted']`. The classifier handles the
    auto-promote/budget-exhaust downgrades to escalate routing.

    Failure to write is logged WARN and non-fatal — the notify to Beacon
    above has already informed her of the REVIEW_REVISION; Larry sees the
    gap and can manually re-dispatch.
    """
    task_id = data.get('task_id') or 'unknown'
    forge_session = data.get('forge_build_session_id')
    if not forge_session:
        log(
            f'REVIEW_REVISION on task {task_id} has no forge_build_session_id '
            f'(propagation gap?); revision dispatch would have no session to '
            f'--resume — skipping. Larry should manually re-dispatch.',
            'WARN',
        )
        return

    target_repo = data.get('target_repo')
    if not target_repo:
        log(
            f'REVIEW_REVISION on task {task_id} has no target_repo on envelope; '
            f'Forge worktree gate would reject revision dispatch — skipping.',
            'WARN',
        )
        return

    branch = data.get('branch')
    payload = decision.get('payload') or {}
    findings = payload.get('findings') or []

    # Increment revision counter for the dispatch envelope. The cousin in
    # Mirror's envelope is incremented when the re-review dispatches; this
    # one is the count of revision attempts Forge has made so far.
    current_count = data.get('revision_count', 0)
    if not isinstance(current_count, int) or current_count < 0:
        current_count = 0
    next_count = current_count + 1
    max_revisions = data.get('max_revisions', mrh.DEFAULT_MAX_REVISIONS)
    if not isinstance(max_revisions, int) or max_revisions < 0:
        max_revisions = mrh.DEFAULT_MAX_REVISIONS

    # Serialize findings into a human-readable block for the prompt.
    findings_lines = ['Mirror\'s findings on this PR:', '']
    if isinstance(findings, list):
        for i, f in enumerate(findings, 1):
            if not isinstance(f, dict):
                findings_lines.append(f'  {i}. {f}')
                continue
            sev = f.get('severity', '?')
            file_ref = f.get('file', '?')
            line_ref = f.get('line_range', '?')
            desc = f.get('description', '(no description)')
            findings_lines.append(
                f'  {i}. [{sev}] {file_ref} {line_ref} — {desc}'
            )
    else:
        findings_lines.append(f'  (raw findings: {findings})')
    findings_block = '\n'.join(findings_lines)

    revision_prompt_lines = [
        f'Revision phase. Mirror has reviewed your build on task `{task_id}` '
        f'and requested changes (revision {next_count} of {max_revisions}).',
        '',
        f'Task: `{task_id}`',
    ]
    if branch:
        revision_prompt_lines.append(f'Branch: `{branch}`')
    pr_url = data.get('pr_url') or payload.get('pr_url')
    if pr_url:
        revision_prompt_lines.append(f'PR: {pr_url}')
    revision_prompt_lines.extend([
        '',
        findings_block,
        '',
        'Follow the Revision phase protocol in your CLAUDE.md: apply each '
        'finding as a targeted edit (no scope creep), commit with a '
        'conventional-commit revision message, push to the same branch '
        '(PR auto-updates), and emit a one-line result starting with '
        f'`Revision {next_count} applied: <summary>` (strict per 5b — '
        'missing prefix dead-letters back to you).',
    ])
    revision_prompt = '\n'.join(revision_prompt_lines)

    revision_task: dict[str, Any] = {
        'task_id': task_id,
        'prompt': revision_prompt,
        'source': 'beacon',          # logical dispatcher (Beacon's spec authorized this)
        'phase': 'revision',
        'session_id': forge_session,  # --resume Forge's build session
        # C-1 review fix (D3.5 5b): also propagate forge_build_session_id
        # on the revision-task envelope itself. _build_outbox propagates
        # this onward to Forge's revision outbox, so round-2's
        # _dispatch_mirror_review_rerun → next REVIEW_REVISION →
        # _dispatch_revision_to_forge can still resolve the build session.
        # Without this, the loop stalls silently at round 2.
        'forge_build_session_id': forge_session,
        'target_repo': target_repo,
        'revision_count': next_count,
        'max_revisions': max_revisions,
        'dispatched_by': 'outbox-notifier',
        # D3.5 5b M-8 (second-pass review fix): thread Mirror's findings
        # forward so the re-review prompt can include them. Mirror's
        # re-review session is FRESH (no claude --resume); without these
        # findings on the envelope, her re-review prompt has to direct her
        # to reconstruct findings from sources that aren't reliably
        # available (Forge's commit message body, Beacon's journal). On
        # round 2 she'd re-derive different findings, breaking the loop's
        # coherence. Findings round-trip: here → _build_outbox propagates
        # → Forge's revision outbox carries → _dispatch_mirror_review_rerun
        # reads + injects into Mirror's re-review prompt.
        'previous_findings': findings if isinstance(findings, list) else [],
    }
    if branch:
        revision_task['branch'] = branch
    if pr_url:
        revision_task['pr_url'] = pr_url
    if data.get('reply_chat_id') is not None:
        revision_task['reply_chat_id'] = data['reply_chat_id']
    # Propagate the same envelope fields _dispatch_build_phase does so a
    # future REVIEW_QUESTION (deferred) round-trip would preserve PR
    # metadata. Same shape as 5a M-2 review fix.
    for f_name in ('pr_title', 'pr_body', 'max_clarifications'):
        if data.get(f_name) is not None:
            revision_task[f_name] = data[f_name]

    # Idempotency check: revision-task filename is keyed on round number so
    # multiple revision rounds for the same task_id don't collide. Re-process
    # on notifier crash skips if already in inbox/archive/invalid.
    revision_filename = f'revision-{task_id}-{next_count}.json'
    forge_inbox = safe_write_inbox.INBOXES_ROOT / 'forge'
    if (
        (forge_inbox / revision_filename).exists()
        or (forge_inbox / '.archive' / revision_filename).exists()
        or (forge_inbox / '.invalid' / revision_filename).exists()
    ):
        log(
            f'revision-{next_count} already dispatched for task {task_id} '
            f'(file or archive or .invalid present); skipping duplicate write'
        )
        return

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='forge',
            task_dict=revision_task,
            source_agent='beacon',
            filename=revision_filename,
        )
        log(
            f'revision-{next_count} dispatched forge <- beacon '
            f'(task={task_id}, file={dest.name}, '
            f'resume={forge_session[:12]}...)'
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'revision dispatch FAILED for task {task_id} round {next_count}: '
            f'{type(e).__name__}: {e}. Beacon already notified of REVISION; '
            f'Larry must manually re-dispatch.',
            'WARN',
        )


def _dispatch_mirror_review_rerun(
    data: dict[str, Any], round_num: int, summary: str,
) -> None:
    """Write a fresh review-request to Mirror after Forge's revision lands.

    D3.5 commit 5b. Parallel to `_dispatch_mirror_review` but for re-review
    rounds: Mirror's previous session is closed, so she starts fresh on the
    now-updated PR. The envelope carries `revision_count` (so her CLAUDE.md
    knows this is round N, not round 0).

    Triggered from `process_outbox` when Forge's `phase=revision` outbox
    arrives WITH a valid `Revision N applied:` preamble. Missing preamble
    is handled separately (strict gate → marker-error dead-letter).

    `forge_build_session_id` propagates forward unchanged — the next
    REVIEW_REVISION (if any) can dispatch another revision task to the
    same session.
    """
    task_id = data.get('task_id') or 'unknown'
    target_repo = data.get('target_repo')
    if not target_repo:
        log(
            f'Forge revision-{round_num} on task {task_id} has no target_repo; '
            f'cannot dispatch re-review — skipping.',
            'WARN',
        )
        return
    branch = data.get('branch')
    pr_url = data.get('pr_url')
    max_revisions = data.get('max_revisions', mrh.DEFAULT_MAX_REVISIONS)
    if not isinstance(max_revisions, int) or max_revisions < 0:
        max_revisions = mrh.DEFAULT_MAX_REVISIONS
    # D3.5 5b second-pass M-8 fix: pull previous_findings from envelope
    # (threaded through _dispatch_revision_to_forge → _build_outbox →
    # Forge's revision outbox → here). Mirror's re-review session is fresh
    # — without these findings her CLAUDE.md tells her to find them in
    # "the PR's commit history or Beacon's journal," neither of which is
    # reliably accessible. Inject them directly into the prompt instead.
    previous_findings = data.get('previous_findings')
    if not isinstance(previous_findings, list):
        previous_findings = []

    review_prompt_lines = [
        f'Re-review phase. Forge has applied revision {round_num} on task '
        f'`{task_id}` per your earlier REVIEW_REVISION findings.',
        '',
        f"Forge's revision summary: {summary}",
        '',
        f'Task: `{task_id}`',
    ]
    if pr_url:
        review_prompt_lines.append(f'PR: {pr_url}')
    if branch:
        review_prompt_lines.append(f'Branch: `{branch}`')
    if previous_findings:
        review_prompt_lines.extend(['', 'Your findings from the previous round:'])
        for i, f in enumerate(previous_findings, 1):
            if not isinstance(f, dict):
                review_prompt_lines.append(f'  {i}. {f}')
                continue
            sev = f.get('severity', '?')
            file_ref = f.get('file', '?')
            line_ref = f.get('line_range', '?')
            desc = f.get('description', '(no description)')
            review_prompt_lines.append(
                f'  {i}. [{sev}] {file_ref} {line_ref} — {desc}'
            )
    review_prompt_lines.extend([
        '',
        f'You are on revision round {round_num} of {max_revisions}. Read '
        'the updated PR diff (`gh pr diff <N>` — same PR, now with Forge\'s '
        'revision commit on top). Verify each finding above is resolved '
        'in the new diff AND no new issues were introduced. Emit one marker: '
        'REVIEW_PASS (if findings resolved cleanly), REVIEW_REVISION (if '
        'more changes needed; budget is round-aware — next would be round '
        f'{round_num + 1}), REVIEW_ESCALATE (if revision approach is wrong), '
        'or REVIEW_EMERGENCY_HALT (safety issue).',
    ])
    review_prompt = '\n'.join(review_prompt_lines)

    # D3.5 5b second-pass m-9 fix: don't substitute the literal string
    # '(unknown)' when pr_url is missing — it propagates as a marker value
    # into Forge's next revision prompt (`PR: (unknown)`). If pr_url is
    # missing, log + skip the dispatch; same shape as the target_repo gate.
    if not pr_url:
        log(
            f'Forge revision-{round_num} on task {task_id} has no pr_url '
            f'on envelope; cannot dispatch re-review — skipping. Larry '
            f'should manually re-dispatch.',
            'WARN',
        )
        return

    review_task: dict[str, Any] = {
        'task_id': task_id,
        'prompt': review_prompt,
        'source': 'beacon',
        'phase': 'review',
        'pr_url': pr_url,
        'target_repo': target_repo,
        'revision_count': round_num,
        'max_revisions': max_revisions,
        'dispatched_by': 'outbox-notifier',
    }
    if branch:
        review_task['branch'] = branch
    if data.get('reply_chat_id') is not None:
        review_task['reply_chat_id'] = data['reply_chat_id']
    # forge_build_session_id propagates forward unchanged so the NEXT
    # revision (if Mirror flags more findings) can resume Forge's session.
    if data.get('forge_build_session_id'):
        review_task['forge_build_session_id'] = data['forge_build_session_id']
    # M-8 second-pass fix: also propagate previous_findings forward so the
    # NEXT round's REVIEW_REVISION (if any) carries findings through.
    if isinstance(data.get('previous_findings'), list):
        review_task['previous_findings'] = data['previous_findings']
    for f_name in ('pr_title', 'pr_body', 'max_clarifications'):
        if data.get(f_name) is not None:
            review_task[f_name] = data[f_name]

    # Idempotency: keyed on round number so re-process on notifier crash
    # doesn't double-dispatch a re-review.
    review_filename = f'review-{task_id}-rev{round_num}.json'
    mirror_inbox = safe_write_inbox.INBOXES_ROOT / 'mirror'
    if (
        (mirror_inbox / review_filename).exists()
        or (mirror_inbox / '.archive' / review_filename).exists()
        or (mirror_inbox / '.invalid' / review_filename).exists()
    ):
        log(
            f're-review for revision {round_num} already dispatched for '
            f'task {task_id}; skipping duplicate write'
        )
        return

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='mirror',
            task_dict=review_task,
            source_agent='beacon',
            filename=review_filename,
        )
        log(
            f're-review dispatched mirror <- beacon (task={task_id}, '
            f'round={round_num}, file={dest.name})'
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f're-review dispatch FAILED for task {task_id} round {round_num}: '
            f'{type(e).__name__}: {e}. Forge already wrote her revision; '
            f'Larry must manually re-dispatch.',
            'WARN',
        )


def _route_beacon_replan_approval(data: dict[str, Any]) -> bool:
    """Handle Beacon's auto-replan APPROVAL_REQUEST emitted in response to a
    review-escalate inbox dispatch.

    D3.5 commit 5c. Bridges the gap between Beacon-via-inbox-watcher (her
    APPROVAL_REQUEST in result text) and the bot's existing chat-mode
    APPROVAL_REQUEST flow (which only fires on Telegram chat replies, not
    on outbox-derived markers). The notifier acts as the bot's impersonator
    here — calls `approval.extract_approval_request`, `trust_policy.evaluate`,
    `approval.add_pending`, and queues the formatted approval DM through the
    larry-alerts pipeline so the bot's existing alerts poll surfaces it to
    Larry without any chat round-trip.

    Returns:
      True if the replan path took over (caller should archive the outbox
        and NOT fall through to default routing).
      False if the path declined (no marker, or marker failed the discipline
        gate). Caller falls through to default routing so Beacon's narrative
        still reaches Mirror as informational result-notification.

    Decision tree (in order):

      1. No APPROVAL_REQUEST present in result → return False.
      2. Malformed APPROVAL_REQUEST (e.g., missing required fields) → log
         WARN + return False (fall through; level-3 discipline doesn't run
         marker-error cascade per 5c sign-off).
      3. Discipline gate fail (task_id mismatch OR insufficient Mirror-reason
         reference) → log WARN + return False.
      4. Budget exhausted (replan_count+1 > max_replans) → queue Larry-
         notification "loop exhausted" + return True (suppress dispatch).
      5. Trust policy:
           - reject → queue Larry-notification with policy rejection +
             return True.
           - auto_approve → add_pending(replan_count=next) +
             dispatch_approved + queue Larry-notification with auto-approve
             confirmation + resolve(approved) + return True.
           - force_ask → add_pending(replan_count=next) + queue
             approval-request alert + return True.

    No marker-error cascade on the Beacon side (per 5c sign-off, level-3
    discipline). If you want strict-5 dial behavior, the notifier would
    need a `_notify_beacon_marker_error` parallel to the Forge/Mirror
    paths plus a Beacon CLAUDE.md retry contract. Deferred.
    """
    task_id = data.get('task_id') or 'unknown'
    result_text = data.get('result') or ''
    if not isinstance(result_text, str) or not result_text:
        return False

    try:
        payload, _narrative = approval.extract_approval_request(result_text)
    except approval.MalformedApprovalMarker as e:
        log(
            f'beacon replan APPROVAL_REQUEST malformed for task {task_id}: '
            f'{e}; falling through to default routing',
            'WARN',
        )
        return False

    if payload is None:
        # Beacon chose to push back with prose only — no marker emitted.
        # Default routing will notify-back informationally.
        return False

    # Level-3 discipline gate. Failures log WARN and fall through; no
    # cascade per 5c sign-off.
    mirror_reason = data.get('mirror_escalate_reason', '') or ''
    ok, err = approval.validate_replan_discipline(
        payload, data, mirror_reason,
    )
    if not ok:
        log(
            f'beacon replan APPROVAL_REQUEST failed discipline gate for '
            f'task {task_id}: {err}; falling through to default routing',
            'WARN',
        )
        return False

    # Budget gate (system-side backstop — Beacon's CLAUDE.md is first line).
    decision, next_count, max_count = approval.evaluate_replan_budget(data)
    reply_chat_id = data.get('reply_chat_id')
    is_valid_chat = isinstance(reply_chat_id, int)

    if decision == 'exhausted':
        log(
            f'beacon replan budget exhausted for task {task_id} '
            f'(would be round {next_count} of {max_count}); '
            f'suppressing APPROVAL_REQUEST and DMing Larry',
            'WARN',
        )
        if is_valid_chat:
            msg = (
                f'Beacon replan loop exhausted on task `{task_id}` '
                f'(round {next_count - 1} of {max_count} used). '
                f'She emitted another revised plan but the budget cap fired. '
                f'Decide manually: chat with her to take a different '
                f'approach, or accept the prior PR as-is with caveats.'
            )
            if not larry_alerts.append_notification(
                source='outbox-notifier',
                intent='review-escalate',
                message=msg,
                chat_id=reply_chat_id,
                task_id=task_id,
            ):
                # M-5 review fix: surface alert-write failure so it's not
                # silent. Beacon's reminder timer at 6/24/72h will catch
                # the hung pending state, but for now log the gap loud.
                log(
                    f'BEACON_REPLAN_ALERT_WRITE_FAILED task={task_id} '
                    f'budget-exhausted DM did not queue (disk full?); '
                    f'manual intervention required',
                    'WARN',
                )
        else:
            # M-3 review fix: budget-exhausted with no chat_id is the
            # cascade-exhaust-silent-to-Larry failure mode. Without a
            # chat_id we can't DM, but we DO want a load-bearing sentinel
            # the watchdog can scan for. The dispatch is closed either
            # way; the silent loop is the part to avoid.
            log(
                f'BEACON_REPLAN_EXHAUSTED_NO_CHAT task={task_id} '
                f'(round {next_count - 1} of {max_count}); cannot DM '
                f'(no reply_chat_id on envelope); manual intervention '
                f'required',
                'WARN',
            )
        return True

    # Trust policy decision.
    try:
        action_str, rule = approval.trust_decision(payload)
    except Exception as e:  # noqa: BLE001 — defensive; never wedge daemon
        log(
            f'trust_policy.evaluate raised on beacon replan for task '
            f'{task_id}: {type(e).__name__}: {e}; falling through',
            'WARN',
        )
        return False

    # add_pending requires a chat_id (the bot uses it to route the DM).
    # Without one, we can't queue the approval-request alert because the
    # bot has no destination. Fall through so the narrative still reaches
    # Mirror via default routing.
    if not is_valid_chat:
        log(
            f'beacon replan APPROVAL_REQUEST for task {task_id} has no '
            f'valid reply_chat_id (got {reply_chat_id!r}); cannot route '
            f'approval DM, falling through',
            'WARN',
        )
        return False

    if action_str == 'reject':
        log(
            f'trust_policy rejected beacon replan for task {task_id} '
            f'(rule={rule}); DMing Larry the rejection',
        )
        msg = approval.format_policy_rejection(payload, rule or {})
        if not larry_alerts.append_notification(
            source='outbox-notifier',
            intent='reject',
            message=msg,
            chat_id=reply_chat_id,
            task_id=task_id,
        ):
            log(
                f'BEACON_REPLAN_ALERT_WRITE_FAILED task={task_id} '
                f'trust-policy reject DM did not queue (disk full?)',
                'WARN',
            )
        return True

    # Med-10 review fix: dedup by task_id against existing pending entries.
    # Replay scenarios (notifier processes a duplicate outbox; bot replays
    # after a crash; synthetic smoke test re-drops the same file) would
    # otherwise create N pending entries and queue N alerts, DMing Larry
    # N times for the same plan. Idempotency at the dispatch level
    # (filename = task_id) catches the auto_approve fork, but force_ask
    # has no analogous gate without this check.
    existing = approval.find_pending_by_id(payload['task_id'])
    if existing is not None and existing.get('status') == 'pending':
        log(
            f'beacon replan APPROVAL_REQUEST for task {task_id} already '
            f'has a pending entry (id={existing["id"]}, status=pending); '
            f'skipping duplicate add_pending + alert queue',
        )
        return True

    entry = approval.add_pending(
        payload,
        chat_id=reply_chat_id,
        replan_count=next_count,
        max_replans=max_count,
    )

    if action_str == 'auto_approve':
        # Med-9 review fix: bare-Exception coverage matches the trust_decision
        # block above. Either both narrow OR both wide — daemon-never-wedge is
        # the actual invariant. M-4 added OSError + JSONEncodeError to the
        # original DispatchRejected/RoutingDenied pair.
        try:
            approval.dispatch_approved(entry)
            approval.resolve(
                entry['id'], 'approved',
                note=f'auto_approved by rule (5c replan): {rule}',
            )
            log(
                f'beacon replan auto-approved + dispatched: task={task_id}, '
                f'replan_count={next_count}, rule={rule}',
            )
            msg = approval.format_auto_approve_confirmation(entry, rule or {})
            if not larry_alerts.append_notification(
                source='outbox-notifier',
                intent='review-pass',  # informational; reuse closest emoji
                message=msg,
                chat_id=reply_chat_id,
                task_id=task_id,
            ):
                log(
                    f'BEACON_REPLAN_ALERT_WRITE_FAILED task={task_id} '
                    f'auto-approve confirmation did not queue (disk full?)',
                    'WARN',
                )
        except Exception as e:  # noqa: BLE001 — daemon-never-wedge invariant
            log(
                f'beacon replan auto-approve dispatch FAILED for task '
                f'{task_id}: {type(e).__name__}: {e}',
                'WARN',
            )
            # Entry is still in pending — Larry can resolve manually.
            msg = (
                f'Beacon replan auto-approve dispatch failed for task '
                f'`{task_id}`: {type(e).__name__}: {e}. Entry remains '
                f'pending; reply `reject: ...` to clear or retry manually.'
            )
            if not larry_alerts.append_notification(
                source='outbox-notifier',
                intent='review-escalate',
                message=msg,
                chat_id=reply_chat_id,
                task_id=task_id,
            ):
                log(
                    f'BEACON_REPLAN_ALERT_WRITE_FAILED task={task_id} '
                    f'auto-approve failure DM did not queue (disk full?)',
                    'WARN',
                )
        return True

    # force_ask path — queue the approval-request alert; bot DMs the
    # formatted approval body on its next sweep.
    body = approval.format_approval_dm(entry)
    if not larry_alerts.append_approval_request(
        chat_id=reply_chat_id,
        approval_id=entry['id'],
        body=body,
    ):
        # M-5 review fix: if the alert queue write fails, the pending entry
        # still exists (durable) but Larry won't get the DM until the
        # reminder timer fires at 6h. Log loud for watchdog scanning.
        log(
            f'BEACON_REPLAN_ALERT_WRITE_FAILED task={task_id} '
            f'force_ask approval-request did not queue (disk full?); '
            f'pending entry exists; reminder timer will surface at 6h',
            'WARN',
        )
    log(
        f'beacon replan APPROVAL_REQUEST queued for force_ask: task={task_id}, '
        f'replan_count={next_count}, chat_id={reply_chat_id}',
    )
    return True


def process_outbox(outbox_file: Path) -> str:
    """Process one result outbox. Returns one of:
       'notified' | 'notified-marker' | 'archived-no-notify' | 'depth-cap' |
       'skip-self' | 'partial-json' | 'notify-failed' | 'marker-error' |
       'notified-replan'.
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

    # D3.5 commit 5a — Forge build-phase outbox carrying "PR opened: <url>"
    # triggers a `review-request` dispatch to Mirror. Fires BEFORE marker
    # classification so the dispatch happens even though Forge's build
    # response has no marker (markers are preflight-only per her CLAUDE.md
    # Build phase protocol). This is an additive dispatch — the notify to
    # Beacon below still fires via the default routing path, so Beacon
    # journals "PR opened" while Mirror starts her review.
    if agent == 'forge' and data.get('phase') == 'build':
        pr_url = _extract_pr_url_from_build_result(data.get('result', ''))
        if pr_url:
            _dispatch_mirror_review(data, pr_url)
        # No marker, so marker classification below returns None and the
        # default routing path takes over (Beacon notify with the full
        # build result narrative).

    # D3.5 commit 5b — Forge revision-phase outbox. Strict gate per Larry's
    # signoff (Option 3 — strict on revision, lenient on build):
    #   - "Revision N applied: <summary>" preamble found → dispatch a
    #     re-review to Mirror with revision_count incremented.
    #   - Preamble missing → raise MalformedForgeMarker so the marker-error
    #     cascade fires and Forge gets a sharp "use the required preamble"
    #     prompt back. Unlike build phase, revision phase has no documented
    #     blocker-paragraph alternative; the structure is mandatory.
    # The Beacon notify still fires via the default routing path below
    # (Beacon journals the revision narrative); the re-review dispatch is
    # additive.
    if agent == 'forge' and data.get('phase') == 'revision':
        parsed = _extract_revision_summary_from_result(data.get('result', ''))
        if parsed is None:
            # Strict gate: revision phase MUST start with the preamble.
            log(
                f'forge revision-phase outbox without "Revision N applied:" '
                f'preamble: {outbox_file.name}; treating as marker-error',
                'WARN',
            )
            _notify_forge_marker_error(
                data,
                'phase=revision requires response to START with '
                '"Revision N applied: <one-line summary>" preamble — none '
                'found. Re-read agents/forge/CLAUDE.md Revision phase '
                'protocol — the preamble is the structural signal that '
                'revision completed; the rest of the response is narrative '
                'underneath.',
            )
            _archive_outbox(outbox_file)
            return 'marker-error'
        round_num, summary = parsed
        # D3.5 5b M-3 review fix: validate round_num against the envelope's
        # expected revision_count. Forge writing "Revision 0 applied:" (no
        # such round exists; first revision is round 1), "Revision 99
        # applied:" (force-exhaust the budget via misreport), or repeating
        # "Revision 1 applied:" twice (round drift) all reach this gate.
        # Mismatch dead-letters via marker-error with a precise message
        # so Forge re-emits with the correct number from the envelope.
        envelope_round = data.get('revision_count')
        if round_num < 1:
            log(
                f'forge revision-phase outbox with non-positive round '
                f'(N={round_num}) in preamble: {outbox_file.name}; '
                f'treating as marker-error',
                'WARN',
            )
            _notify_forge_marker_error(
                data,
                f'Revision preamble round number must be ≥ 1 (you wrote '
                f'"Revision {round_num} applied:"). The first revision is '
                f'round 1; consecutive revisions increment. Read the '
                f'envelope\'s `revision_count` field and use that number.',
            )
            _archive_outbox(outbox_file)
            return 'marker-error'
        if isinstance(envelope_round, int) and round_num != envelope_round:
            log(
                f'forge revision-phase outbox round mismatch: preamble '
                f'says N={round_num}, envelope says revision_count='
                f'{envelope_round} ({outbox_file.name}); treating as '
                f'marker-error',
                'WARN',
            )
            _notify_forge_marker_error(
                data,
                f'Revision preamble round number ({round_num}) does not '
                f'match the envelope\'s `revision_count` ({envelope_round}). '
                f'Use the envelope number — that\'s the round the dispatch '
                f'is for. Re-emit "Revision {envelope_round} applied: ..." '
                f'with the same summary content.',
            )
            _archive_outbox(outbox_file)
            return 'marker-error'
        _dispatch_mirror_review_rerun(data, round_num, summary)

    # D3.5 commit 5c — Beacon auto-replan check. Beacon's outbox in response
    # to a review-escalate inbox dispatch may contain a `=== APPROVAL_REQUEST
    # ===` marker (her revised plan). The notifier impersonates the bot's
    # chat-mode approval flow (extract → trust policy → add_pending → queue
    # alert) so the bot's existing alerts poll surfaces the approval DM to
    # Larry. If no marker or marker fails discipline, falls through to
    # default routing — Beacon's narrative still reaches Mirror as
    # informational. The auto-DM Larry received on the Mirror→Beacon hop
    # (via 5a-followup's review-escalate DM_TEMPLATE) already gave him the
    # initial signal; this just adds the auto-replan approval prompt on
    # top when Beacon decides to revise.
    if (
        agent == 'beacon'
        and data.get('inbound_intent') in _BEACON_REPLAN_INBOUND_INTENTS
    ):
        if _route_beacon_replan_approval(data):
            _archive_outbox(outbox_file)
            return 'notified-replan'

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

    # D3.5 commit 5a — Mirror review marker check. Parallel to the Forge
    # branch above; same return shape from the classifier so the marker-
    # decision routing block below handles both transparently.
    if agent == 'mirror':
        try:
            marker_decision = _classify_mirror_marker(data)
        except (mrh.MalformedMirrorMarker, mrh.MultipleMirrorMarkers) as e:
            log(
                f'mirror marker error in {outbox_file.name}: '
                f'{type(e).__name__}: {e}',
                'WARN',
            )
            _notify_mirror_marker_error(data, str(e))
            _archive_outbox(outbox_file)
            return 'marker-error'
        # Auto-promoted REVIEW_REVISION (low confidence → escalate) — log
        # so the audit trail captures Mirror's original verdict alongside
        # the system's routing decision.
        if marker_decision and marker_decision.get('auto_promoted'):
            log(
                f'mirror REVIEW_REVISION auto-promoted to ESCALATE for task '
                f'{data.get("task_id", "?")} (confidence=low)',
            )
        # 5d ships the EMERGENCY_HALT trip; 5a just logs at WARN level so
        # the operator sees the marker fire even before the halt-file path
        # is wired.
        if marker_decision and marker_decision['marker_type'] == 'review_emergency_halt':
            log(
                f'mirror REVIEW_EMERGENCY_HALT on task '
                f'{data.get("task_id", "?")}: {marker_decision["payload"].get("reason", "(no reason)")} '
                f'— 5a journals to Beacon only; halt-file trip + priority DM in 5d',
                'WARN',
            )

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
        # Phase D3 commit 4b post-test-2 fix: propagate target_repo + branch
        # + pr_title/pr_body forward across the full clarification cascade
        # (forge→beacon question, then beacon→forge answer, then forge
        # re-preflight). Without these on the notify task, _build_outbox
        # on Beacon's side has nothing to propagate, the answer leg arrives
        # at Forge with target_repo=None, and the watcher's worktree gate
        # refuses with "no canonical path". Same shape as the marker-error
        # black hole the 4b review caught — different code path.
        for f_name in ('target_repo', 'branch', 'pr_title', 'pr_body',
                       'pr_url'):
            if data.get(f_name):
                notify_task[f_name] = data[f_name]
        # D3.5 5c — when the marker decision is review-escalate (any of three
        # sub-flavors: direct REVIEW_ESCALATE, auto-promoted from low-
        # confidence REVISION, or budget-exhausted REVISION), surface the
        # replan budget + Mirror's reason on the notify task so Beacon's
        # CLAUDE.md decision tree has the data it needs without re-reading
        # her inbox archive. `mirror_escalate_reason` rides forward through
        # _build_outbox propagation so the notifier can apply the level-3
        # discipline gate when Beacon emits her replan APPROVAL_REQUEST.
        if marker_decision['intent'] == 'review-escalate':
            notify_task['replan_count'] = data.get('replan_count', 0) or 0
            max_replans = data.get('max_replans')
            if not isinstance(max_replans, int) or max_replans < 0:
                max_replans = _load_max_replans_from_config()
            notify_task['max_replans'] = max_replans
            reason = marker_decision.get('intent_kwargs', {}).get('reason', '')
            # C-2 review fix: when the underlying marker was REVIEW_REVISION
            # (auto_promoted or budget_exhausted), the `reason` text built by
            # mrh.build_auto_promote_reason / build_budget_exhausted_reason is
            # PROCEDURAL framing ("Mirror emitted REVISION with low confidence
            # ... auto-promoted to ESCALATE"), not semantic finding content.
            # Beacon's level-3 discipline gate compares her summary tokens
            # against `mirror_escalate_reason` — without finding text, her
            # good-faith replan summary will always fail. Augment the reason
            # with finding descriptions so the gate has semantic signal.
            payload = marker_decision.get('payload') or {}
            if (
                (marker_decision.get('auto_promoted')
                 or marker_decision.get('budget_exhausted'))
                and isinstance(payload.get('findings'), list)
            ):
                finding_descs = []
                for f in payload['findings']:
                    if isinstance(f, dict) and f.get('description'):
                        finding_descs.append(str(f['description']))
                if finding_descs:
                    reason = (
                        f'{reason} Findings: ' + ' | '.join(finding_descs)
                    )
            if reason:
                notify_task['mirror_escalate_reason'] = reason

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

        # Phase D3 commit 4b: PROCEED → ALSO write a build-phase task to
        # Forge's inbox. The notify-to-Beacon above is informational
        # (Beacon journals "Forge is proceeding"); the build-phase dispatch
        # below is what actually triggers code work. Two-invocation
        # preflight→build with --resume per signed-off design.
        if marker_decision['marker_type'] == 'proceed' and agent == 'forge':
            _dispatch_build_phase(data)

        # D3.5 5b: REVIEW_REVISION with budget remaining + high confidence
        # → ALSO dispatch a revision task to Forge's inbox. Beacon's notify
        # above is informational (Shape 7, now mid-chain in 5b); the
        # revision dispatch is what actually triggers Forge's fix. Skipped
        # if auto_promoted (low confidence → escalate) or budget_exhausted
        # (over max_revisions → escalate); both downgrade to Beacon-only
        # routing via the intent override above.
        if (
            marker_decision['marker_type'] == 'review_revision'
            and agent == 'mirror'
            and not marker_decision.get('auto_promoted')
            and not marker_decision.get('budget_exhausted')
        ):
            _dispatch_revision_to_forge(data, marker_decision)

        # D3.5 5a-followup: chain-completion DM to the originating Telegram
        # thread. Fires only for terminal-from-Larry's-perspective intents
        # (review-pass/revision/escalate/emergency, plus Forge preflight
        # reject/clarification-exhausted) and only when reply_chat_id is
        # propagated through the chain. Non-fatal on failure.
        _maybe_dm_larry(data, marker_decision)

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
    # Phase D3 commit 4b post-test-2 fix: propagate target_repo + branch +
    # pr_title/pr_body so the clarification-answer leg back to Forge passes
    # the worktree gate. See the matching block in the marker-decision path
    # above for the full explanation.
    for f_name in ('target_repo', 'branch', 'pr_title', 'pr_body'):
        if data.get(f_name):
            notify_task[f_name] = data[f_name]

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

    D3.5 commit 5a — Mirror marker types. PASS body = summary; REVISION body
    = finding summary; ESCALATE body = reason; EMERGENCY_HALT body = reason +
    evidence. Without these branches the trailing fallback returns '(no
    reason)' for PASS and (non-auto-promoted) REVISION since their
    intent_kwargs carry no `reason` key — the C-1 review-pass-body-blank bug.
    """
    payload = decision['payload']
    marker_type = decision['marker_type']
    if marker_type == 'clarify_request' and decision['intent'] == 'clarify':
        return payload.get('question', '(no question text)')
    if marker_type == 'proceed':
        return payload.get('preflight_summary', '(no summary)')
    if marker_type == 'reject':
        return payload.get('reason', '(no reason)')
    if marker_type == 'review_pass':
        return payload.get('summary', '(no summary)')
    if marker_type == 'review_revision':
        findings = payload.get('findings') or []
        if isinstance(findings, list) and findings:
            first = findings[0]
            first_desc = (
                first.get('description', '(no description)')
                if isinstance(first, dict)
                else str(first)
            )
            return (
                f'{len(findings)} finding(s). First: {first_desc}'
            )
        return '(revision marker with no findings — should have been PASS)'
    if marker_type == 'review_escalate':
        return payload.get('reason', '(no reason)')
    if marker_type == 'review_emergency_halt':
        return (
            f'Reason: {payload.get("reason", "(no reason)")}\n'
            f'Evidence: {payload.get("evidence", "(no evidence)")}'
        )
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
