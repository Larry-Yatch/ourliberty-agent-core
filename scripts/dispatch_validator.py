#!/usr/bin/env python3
"""dispatch_validator.py — Improvement #1: pre-dispatch validation.

Validates a task JSON file BEFORE it's submitted to an agent's inbox.
Kills failure modes F24 (empty prompt), ORIGIN_CHAT_UNKNOWN (missing
reply_chat_id), and a few related quality issues.

Usage:
  python3 dispatch_validator.py <task.json>
    Exit 0 = valid; exit 1 = rejected (with reason on stderr)

  Library mode:
    from dispatch_validator import validate_task
    ok, reason = validate_task(task_dict)
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
import json
import re
import sys
from pathlib import Path

# Known Telegram chat IDs are sourced at runtime from config; keep this set
# narrow until Larry wires up his own chat topology. Extend as needed.
KNOWN_CHAT_IDS = set()

ALLOWED_SOURCES = {
    # Humans
    'larry',
    # Live agents
    'beacon', 'forge', 'mirror', 'pulse',
    'beacon-result', 'forge-result', 'mirror-result', 'pulse-result',
    # Phase D3 clarification protocol
    'forge-question', 'mirror-question',
    'beacon-clarification',
    # Planned agents (placeholdered; safe to allow so dispatch tests don't fail)
    'aide', 'scout', 'compass',
    'aide-result', 'scout-result', 'compass-result',
    # Infrastructure / system sources
    'webhook', 'telegram-webhook', 'watchdog', 'auto-retry', 'auto-iterate',
    'blackboard', 'system-test', 'parallel-test', 'cron', 'unknown',
    'continuation', 'system-sweep', 'cycle-recovery', 'orchestrator',
    'ship_completion_watcher', 'backlog-promoter',
    # D3-forge (commit 4a): notifier-originated marker-error dispatches
    # back to Forge when her preflight marker is malformed. Treated as a
    # system source (not a dialogue leg) — see routing_validator.SYSTEM_SOURCES.
    'outbox-notifier',
    # Closed-loop step 5 (2026-05-24): Pulse Check I auto-dispatches
    # small-effort dollar-quantified proposals to Beacon's inbox. The
    # outbox_notifier branch added in step 4 (PR #82) keys off this exact
    # source on Beacon's outbox; the envelope source must match.
    'pulse-auto-dispatch',
    # E4.4 dashboard UI Approve/Reject actions. Pulse iter 97 + iter 97-notify
    # (2026-05-28) discovered that 'dashboard' was never in this allowlist;
    # 4 dashboard-sourced envelopes were silently dropped to beacon/.invalid
    # between 2026-05-27T17:55Z and 2026-05-28T05:30Z, breaking the UI as a
    # control surface. Adding here closes the gap. NOTE: do not auto-replay
    # the stale envelopes already in beacon/.invalid — they predate this fix.
    'dashboard',
}

# Phase D3 — clarification protocol metadata. Optional on dispatch.
#
# Names ack-proceed / clarify / clarification-response / clarification-exhausted /
# reject are the D3-prep preflight-protocol vocabulary used on Forge's outboxes
# and Beacon's clarification replies.
#
# D3-forge (commit 4a) adds three notifier-emitted intents that aren't
# preflight-protocol per se but ride the same envelope:
#   - result-notification: generic inter-agent result delivery (Pulse over-run
#     fix — naked notifies were getting misread as new work)
#   - dead-letter: dispatcher learns its task was validator-rejected
#   - marker-error: Forge learns her marker was unparseable (re-emit)
#
# D3.5 commit 5a adds the Mirror review marker vocabulary:
#   - review-pass: Mirror approved the PR; no findings ≥ medium, confidence high.
#   - review-revision: Mirror found fixable issues; Forge revises (wired in 5b).
#   - review-escalate: Mirror found issues that need Beacon replan (wired in 5c).
#   - review-emergency-halt: Mirror found a safety issue (credentials, destructive
#     migration, scope-of-trust breach); EMERGENCY_HALT trip wired in 5d.
#   - replan-request: Beacon-shaped intent on a Mirror escalation notify.
#
# PR-S4 follow-up adds the DAG-preflight self-heal vocabulary:
#   - dag-preflight-revision: Mirror returned REVISION on a build-sequence
#     DAG preflight; notify routed to Beacon for autonomous amend +
#     re-dispatch (symmetric with the PASS auto-activate path).
#
# chain-context-durability S2 (M2) adds the no-session code-review self-heal:
#   - code-review-revision-no-session: Mirror returned REVISION on a PR whose
#     envelope carries no forge_build_session_id (the PR #412 class); notify
#     routed to Beacon for autonomous fresh-task_id re-dispatch instead of a
#     broadcast manual-reconcile Larry alert.
ALLOWED_INTENTS = {
    'ack-proceed',
    'clarify',
    'clarification-response',
    'clarification-exhausted',
    'reject',
    'result-notification',
    'dead-letter',
    'marker-error',
    'review-pass',
    'review-revision',
    'review-escalate',
    'review-emergency-halt',
    'replan-request',
    'dag-preflight-revision',
    'code-review-revision-no-session',
}

# D3.5 commit 5a — Mirror review chain. `review` is the Mirror dispatch
# phase (review-request task lands in her inbox after Forge opens a PR).
# `revision` is the Forge revision phase wired in 5b (Mirror's REVIEW_REVISION
# routes a fresh task back to Forge under --resume).
# `rebase` (forge-post-open-mergeable-rebase-001) is the Forge rebase phase: the
# notifier dispatches it back to Forge under --resume when a freshly-opened PR is
# CONFLICTING because main advanced during the build, BEFORE Mirror is dispatched.
ALLOWED_PHASES = {'preflight', 'build', 'review', 'revision', 'rebase', 'routing-signal'}

# PR-S4 rectification (H2): target_agents whose envelopes are routing
# signals — short prompts like `kickoff <seq-id>` or
# `review-sequence-dag <seq-id>` — bypass MIN_PROMPT_LEN because the
# semantic surface is the seq-id, not the prose. The kickoff handler in
# outbox_notifier parses the prompt directly; min-length validation
# blanket-rejected these envelopes (~25-30 char prompts) and broke the
# orchestrator-bootstrap dispatch chain.
ROUTING_SIGNAL_TARGET_AGENTS = {'build_sequence_advancer'}

# bootstrap-002 gap V3 (2026-05-27): Beacon's marker.py render template
# for APPROVAL_REQUEST has no slot for the phase field, so DAG-preflight
# dispatches (target_agent=mirror, prompt=`review-sequence-dag <seq-id>`)
# cannot get phase=routing-signal set via the standard emission path —
# even when Beacon's CLAUDE.md mandates it. Defense-in-depth: also accept
# routing-signal exemption by canonical prompt prefix. Keeps the H2
# semantic safety net intact (these prefixes ARE routing signals by
# definition) without requiring Beacon-LLM adherence to the phase field.
ROUTING_SIGNAL_PROMPT_PREFIXES = (
    'kickoff ',
    'review-sequence-dag ',
)


def _is_routing_signal_prompt(prompt: str) -> bool:
    """True iff `prompt` is a canonical routing signal: it starts with a known
    prefix AND carries a non-empty argument (the seq-id) after it. The
    non-empty-argument check is what keeps a degenerate `kickoff ` (the F24
    empty-prompt bug coincidentally carrying the prefix) from bypassing the
    min-length guard — the whole point of the routing-signal carve-out is that
    the seq-id IS the semantic payload, so a prefix with no seq-id is not a
    routing signal at all."""
    for p in ROUTING_SIGNAL_PROMPT_PREFIXES:
        if prompt.startswith(p) and prompt[len(p):].strip():
            return True
    return False

MIN_PROMPT_LEN = 100  # chars
MAX_PROMPT_LEN = 50000
MIN_TIMEOUT = 60      # seconds
MAX_TIMEOUT = 14400

# Bytes that make a task_id unsafe as a single filesystem path component.
# task_id is interpolated into inbox filenames (f'{task_id}.json') and into
# worktree directory names; '/' and '\' are path separators and NUL / ASCII
# control bytes are filesystem-hostile and a log/JSONL-injection vector.
# PR-A's downstream sanitizers already keep those writes INSIDE the target
# directory, so this is defense-in-depth — but rejecting at the front door
# also closes a residual: the inbox sanitizer (safe_write_inbox.
# sanitize_component) REWRITES a '/'-bearing id before write while the
# idempotency readers (outbox_notifier / heal_pipeline_stall) reconstruct
# the RAW f'{task_id}.json' name, so the on-disk name and the rebuilt name
# diverge; worse, two distinct malformed ids can sanitize-COLLIDE onto one
# filename and os.replace (used by _atomic_write_json) silently overwrites
# the legit task's inbox file (audit #53). Rejecting here means only ids that
# round-trip cleanly ever reach the writer. Printable punctuation that real
# ids carry (':' '@' '#' '-' '_' '.' and spaces) stays valid — it is not
# path-structural, and worktree_manager's own aggressive sanitizer maps it
# away for the (derived, non-round-tripped) worktree name.
_TASK_ID_FORBIDDEN_RE = re.compile(r'[\x00-\x1f\x7f/\\]')


def _validate_task_id_chars(task_id):
    """Return (ok: bool, reason: str) for the path-safety of a task_id.

    Rejects a task_id that cannot serve as a single safe path component:
      - path separators ``/`` or ``\\``
      - NUL or any other ASCII control byte (0x00–0x1f, 0x7f)
      - a value consisting SOLELY of dots (``.``, ``..``, ``...``): that
        names a directory entry, not a file.

    A task_id with embedded dots (``v1.2.3``) is fine — only an all-dots id
    is rejected. ``:`` ``@`` ``#`` and spaces are allowed: they are not
    path-structural and real task ids carry them (e.g.
    ``medic-silence-cpu:high@web-01``). Kept as its own named check so the
    existing valid-id surface is unchanged and the rejection reason is
    self-describing.
    """
    if not isinstance(task_id, str):
        return False, 'task_id is not a string'
    m = _TASK_ID_FORBIDDEN_RE.search(task_id)
    if m:
        return False, (
            f'task_id contains an unsafe path character {m.group()!r} — '
            f'separators, NUL, and ASCII control bytes are rejected'
        )
    if task_id.strip('.') == '':
        return False, (
            f'task_id {task_id!r} is all dots — names a directory entry, '
            f'not a dispatchable id'
        )
    return True, 'ok'


def validate_task(task):
    """Return (ok: bool, reason: str). ok=True means task is dispatchable."""
    if not isinstance(task, dict):
        return False, 'task is not a dict'

    # prompt: must exist, non-empty, reasonable length
    prompt = task.get('prompt', '')
    if not isinstance(prompt, str):
        return False, 'prompt field missing or not a string'
    prompt = prompt.strip()
    # Routing-signal exemption (H2): short canonical prompts addressed to
    # routing-signal target agents OR carrying phase='routing-signal' skip
    # the min-length check. Either condition is sufficient.
    target_agent = task.get('target_agent')
    phase = task.get('phase')
    is_routing_signal = (
        target_agent in ROUTING_SIGNAL_TARGET_AGENTS
        or phase == 'routing-signal'
        or _is_routing_signal_prompt(prompt)
    )
    if not is_routing_signal and len(prompt) < MIN_PROMPT_LEN:
        return False, f'prompt too short ({len(prompt)} chars, min {MIN_PROMPT_LEN}) — likely F24 empty-prompt bug'
    if len(prompt) > MAX_PROMPT_LEN:
        return False, f'prompt too long ({len(prompt)} chars, max {MAX_PROMPT_LEN})'

    # source: must be in allowed list (orchestrator's whitelist)
    source = task.get('source', '')
    if source not in ALLOWED_SOURCES:
        return False, f'source "{source}" not in allowed list — would be DISPATCH_BLOCKED'

    # reply_chat_id: optional, but if present must be int + (preferably) known
    reply_chat_id = task.get('reply_chat_id')
    if reply_chat_id is not None:
        if not isinstance(reply_chat_id, int):
            return False, f'reply_chat_id must be int, got {type(reply_chat_id).__name__}'
        if reply_chat_id not in KNOWN_CHAT_IDS:
            # Warn but allow — new chat IDs are added over time
            pass

    # timeout: if set, must be sane
    timeout = task.get('timeout')
    if timeout is not None:
        if not isinstance(timeout, (int, float)) or timeout < MIN_TIMEOUT or timeout > MAX_TIMEOUT:
            return False, f'timeout {timeout} out of bounds [{MIN_TIMEOUT}, {MAX_TIMEOUT}]'

    # task_id: must exist + non-empty
    task_id = task.get('task_id', '')
    if not task_id or not isinstance(task_id, str):
        return False, 'task_id field missing or empty'

    # task_id path-safety (PR-A follow-up, audit #53 defense-in-depth). See
    # _TASK_ID_FORBIDDEN_RE for the rationale: reject ids that would either
    # sanitize-COLLIDE on write or diverge from the raw name the idempotency
    # readers reconstruct. Valid ids (including ':' '@' '#' and spaces) pass.
    id_ok, id_reason = _validate_task_id_chars(task_id)
    if not id_ok:
        return False, id_reason

    # priority: if set, must be a known value
    priority = task.get('priority')
    if priority is not None and priority not in ('founder-vision', 'normal', 'low', 'urgent'):
        return False, f'priority "{priority}" unknown'

    # Phase D3 — intent / phase / clarification counters. All optional.
    intent = task.get('intent')
    if intent is not None and intent not in ALLOWED_INTENTS:
        return False, f'intent "{intent}" not in allowed set {sorted(ALLOWED_INTENTS)}'

    phase = task.get('phase')
    if phase is not None and phase not in ALLOWED_PHASES:
        return False, f'phase "{phase}" not in allowed set {sorted(ALLOWED_PHASES)}'

    max_clar = task.get('max_clarifications')
    if max_clar is not None and (not isinstance(max_clar, int) or max_clar < 0):
        return False, f'max_clarifications must be non-negative int, got {max_clar!r}'

    clar_count = task.get('clarification_count')
    if clar_count is not None and (not isinstance(clar_count, int) or clar_count < 0):
        return False, f'clarification_count must be non-negative int, got {clar_count!r}'

    if max_clar is not None and clar_count is not None and clar_count > max_clar:
        return False, (
            f'clarification_count ({clar_count}) exceeds max_clarifications ({max_clar})'
        )

    return True, 'ok'


def main():
    if len(sys.argv) < 2:
        print('usage: dispatch_validator.py <task.json>', file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f'file not found: {path}', file=sys.stderr)
        return 2
    try:
        task = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f'invalid JSON: {e}', file=sys.stderr)
        return 1
    ok, reason = validate_task(task)
    if ok:
        print('OK')
        return 0
    print(f'REJECTED: {reason}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
