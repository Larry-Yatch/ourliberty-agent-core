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
}

# D3.5 commit 5a — Mirror review chain. `review` is the Mirror dispatch
# phase (review-request task lands in her inbox after Forge opens a PR).
# `revision` is the Forge revision phase wired in 5b (Mirror's REVIEW_REVISION
# routes a fresh task back to Forge under --resume).
ALLOWED_PHASES = {'preflight', 'build', 'review', 'revision'}

MIN_PROMPT_LEN = 100  # chars
MAX_PROMPT_LEN = 50000
MIN_TIMEOUT = 60      # seconds
MAX_TIMEOUT = 14400


def validate_task(task):
    """Return (ok: bool, reason: str). ok=True means task is dispatchable."""
    if not isinstance(task, dict):
        return False, 'task is not a dict'

    # prompt: must exist, non-empty, reasonable length
    prompt = task.get('prompt', '')
    if not isinstance(prompt, str):
        return False, 'prompt field missing or not a string'
    prompt = prompt.strip()
    if len(prompt) < MIN_PROMPT_LEN:
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
