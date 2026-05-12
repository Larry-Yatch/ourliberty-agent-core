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

import dispatch_validator   # noqa: E402
import safe_write_inbox     # noqa: E402

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
MAX_NOTIFY_DEPTH = 1

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


def _build_notify_prompt(outbox_data: dict[str, Any]) -> str:
    """Frame the result/failure as a prompt the source agent can react to."""
    agent = outbox_data.get('agent', 'unknown')
    success = outbox_data.get('exit_code', 0) == 0
    summary = 'SUCCESS' if success else 'FAILED'
    result_text = _truncate(outbox_data.get('result', '') or '')
    error_text = outbox_data.get('error') or ''
    task_id = outbox_data.get('task_id', '?')

    body_parts = [f'Task result from {agent}: {summary}', f'task_id: {task_id}']
    if result_text:
        body_parts.append(result_text)
    if error_text:
        body_parts.append(f'Error: {error_text}')
    body = '\n\n'.join(body_parts)

    # dispatch_validator requires MIN_PROMPT_LEN=100 chars. Some results are
    # short ("ack."). Pad with a deterministic footer that downstream agents
    # can recognize and ignore.
    if len(body.strip()) < dispatch_validator.MIN_PROMPT_LEN:
        body += '\n\n— end of notify (auto-padded to clear validator floor) —'
    return body


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


def process_outbox(outbox_file: Path) -> str:
    """Process one result outbox. Returns one of:
       'notified' | 'archived-no-notify' | 'depth-cap' | 'skip-self' |
       'partial-json' | 'notify-failed'.
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

    if not _should_notify_back(source, agent):
        _archive_outbox(outbox_file)
        return 'archived-no-notify'

    if _primary_agent_id(source) == agent:
        _archive_outbox(outbox_file)
        return 'skip-self'

    current_depth = _current_notify_depth(data)
    next_depth = current_depth + 1
    if next_depth > MAX_NOTIFY_DEPTH:
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
    # Notify tasks omit `timeout` — they're not fresh dispatches with a
    # work budget, they're the receiver picking up an update. Our
    # dispatch_validator's `timeout` check is range-bounded
    # [MIN_TIMEOUT, MAX_TIMEOUT] when present, no-op when absent. Upstream's
    # `timeout: 0` ("no timeout") would fail our validator's 60s floor.
    notify_task: dict[str, Any] = {
        'task_id': f'notify-{data.get("task_id", outbox_file.stem)}',
        'prompt': _build_notify_prompt(data),
        'source': notify_source,
        '_notify_depth': next_depth,
    }
    if data.get('reply_chat_id') is not None:
        notify_task['reply_chat_id'] = data['reply_chat_id']
    # Propagate session_id so clarification-response delivery can resume
    # the original Forge session (commit 4 wires the watcher to honor it).
    if data.get('claude_session_id'):
        notify_task['session_id'] = data['claude_session_id']
    # If this was answering a *-question, tag the intent.
    if source.endswith('-question'):
        notify_task['intent'] = 'clarification-response'

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
            notify_task: dict[str, Any] = {
                'task_id': f'dead-letter-{invalid_file.stem}',
                'prompt': (
                    f'DEAD LETTER: your dispatch to {agent} was rejected by '
                    f'dispatch_validator and never reached the agent.\n\n'
                    f'Original filename: {invalid_file.name}\n'
                    f'Reason: {reason_text or "(no reason recorded)"}\n\n'
                    f'Original prompt (first 500 chars):\n{original_prompt_excerpt}\n\n'
                    f'No retry will happen automatically. Inspect the task, '
                    f'fix the issue (prompt length, schema, source enum, etc.), '
                    f'and re-dispatch if appropriate.'
                ),
                'source': f'{agent}-result',
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
    processed &= seen_now | processed  # no-op; below is the GC.
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
