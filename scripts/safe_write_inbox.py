#!/usr/bin/env python3
"""safe_write_inbox.py — validated atomic write helper for inbox dispatches.

Phase D3, prep commit. Extracted from upstream `orchestrator.safe_write_inbox`
(lines 551–594) so any dispatcher in our fork (Beacon's bot, the outbox
notifier, future helpers) can write to an agent's inbox through the same
hardened path:

  1. Filename-length guard — ext4 NAME_MAX is 255 bytes; notify/requeue
     prefixes accumulate. Truncate overlong filenames preserving suffix
     + a short hash for traceability.
  2. Schema validation via `dispatch_validator.validate_task` (F24 guard,
     source whitelist, length bounds, task_id required).
  3. Routing validation via `routing_validator.validate_route` — hard
     topology check (raises RoutingDenied on fail) + soft IDENTITY.md
     reroute (returns a possibly-rerouted final_target).
  4. Atomic write — temp file in target dir, then rename. Filesystem
     guarantees readers never see a half-written task.
  5. Audit log line to `~/agents/logs/routing-events.jsonl`.

Returns the Path that was actually written. Raises `DispatchRejected` on
schema rejection or `RoutingDenied` on hard topology violation.

Adapted from GrowthMastery-ai/gm-agent-core orchestrator.py (lines 551–594)
for Larry-Yatch/ourliberty-agent-core (2026-05-11, Phase D3-prep).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow imports of sibling modules in scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatch_validator  # noqa: E402
import routing_validator   # noqa: E402

HOME = Path.home()
AGENTS_ROOT = HOME / 'agents'
INBOXES_ROOT = AGENTS_ROOT / 'inboxes'
ROUTING_EVENTS_LOG = AGENTS_ROOT / 'logs' / 'routing-events.jsonl'

MAX_FILENAME_BYTES = 200  # leaves margin under NAME_MAX (255)


class DispatchRejected(Exception):
    """Schema validation rejected the task."""


# Re-export so callers only need to import safe_write_inbox.
RoutingDenied = routing_validator.RoutingDenied


def _truncate_filename(filename: str) -> tuple[str, bool]:
    """Return (safe_filename, was_truncated)."""
    if len(filename.encode('utf-8')) <= MAX_FILENAME_BYTES:
        return filename, False
    stem, dot, ext = filename.rpartition('.')
    if not dot:
        stem, ext = filename, ''
    h = hashlib.sha1(filename.encode('utf-8')).hexdigest()[:10]
    safe_stem = stem[:100].rstrip('-')
    safe = f'{safe_stem}--h{h}'
    if ext:
        safe = f'{safe}.{ext}'
    return safe, True


def _atomic_write_json(dest: Path, task_dict: dict[str, Any]) -> None:
    """Write JSON via tempfile + os.replace for atomicity."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f'.{dest.name}.', suffix='.tmp', dir=str(dest.parent)
    )
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(task_dict, f, indent=2)
        os.replace(tmp_path, dest)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _log_routing_event(record: dict[str, Any]) -> None:
    """Append a JSON line to routing-events.jsonl. Best-effort."""
    try:
        ROUTING_EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        record_full = dict(record)
        record_full['timestamp'] = datetime.now(timezone.utc).isoformat()
        with open(ROUTING_EVENTS_LOG, 'a') as f:
            f.write(json.dumps(record_full) + '\n')
    except OSError:
        pass


def safe_write_inbox(
    target_agent: str,
    task_dict: dict[str, Any],
    source_agent: str,
    filename: str,
) -> Path:
    """Validated atomic write of `task_dict` to `inboxes/<final_target>/<filename>`.

    Raises:
        DispatchRejected: schema validation failed.
        RoutingDenied: hard topology check denied the (source, target) pair.

    Returns:
        Path to the written file (final_target may differ from target_agent
        if IDENTITY.md soft-reroute fired).
    """
    if not isinstance(task_dict, dict):
        raise DispatchRejected('task_dict must be a dict')
    if not target_agent or not isinstance(target_agent, str):
        raise DispatchRejected('target_agent must be a non-empty string')
    if not source_agent or not isinstance(source_agent, str):
        raise DispatchRejected('source_agent must be a non-empty string')
    if not filename or not isinstance(filename, str):
        raise DispatchRejected('filename must be a non-empty string')

    # Ensure source on the task envelope matches the caller-declared source.
    # If the task envelope doesn't carry source, set it; if it disagrees, reject.
    env_source = task_dict.get('source')
    if env_source is None:
        task_dict = {**task_dict, 'source': source_agent}
    elif env_source != source_agent:
        raise DispatchRejected(
            f'source mismatch: task envelope says "{env_source}" '
            f'but caller declared "{source_agent}"'
        )

    # 1. Schema validation
    ok, reason = dispatch_validator.validate_task(task_dict)
    if not ok:
        raise DispatchRejected(f'schema rejection: {reason}')

    # 2. Routing validation (raises RoutingDenied on hard fail)
    final_target, was_rerouted, reroute_reason = routing_validator.validate_route(
        target_agent, task_dict, source_agent=source_agent,
    )

    # 2b. Repo-scope check (Phase D3 commit 4b). target_repo must be in the
    #     target agent's allowed_repos when both are present. No-op for
    #     legacy tasks that don't carry target_repo, and for agents whose
    #     models config doesn't declare allowed_repos.
    repo_ok, repo_reason = routing_validator.check_target_repo(
        final_target, task_dict.get('target_repo'),
    )
    if not repo_ok:
        raise routing_validator.RoutingDenied(repo_reason or 'target_repo denied')

    # 3. Filename guard
    safe_name, was_truncated = _truncate_filename(filename)

    # 4. Atomic write
    dest = INBOXES_ROOT / final_target / safe_name
    _atomic_write_json(dest, task_dict)

    # 5. Audit log
    _log_routing_event({
        'action': 'reroute' if was_rerouted else 'write',
        'source_agent': source_agent,
        'target_agent_requested': target_agent,
        'target_agent_final': final_target,
        'filename_requested': filename,
        'filename_final': safe_name,
        'truncated': was_truncated,
        'rerouted': was_rerouted,
        'reroute_reason': reroute_reason,
        'task_id': task_dict.get('task_id'),
        'intent': task_dict.get('intent'),
        'phase': task_dict.get('phase'),
        'target_repo': task_dict.get('target_repo'),
    })

    return dest


def _self_test() -> int:
    """Smoke test runnable as `python3 safe_write_inbox.py`."""
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as td:
        global AGENTS_ROOT, INBOXES_ROOT, ROUTING_EVENTS_LOG
        original_agents = AGENTS_ROOT
        original_inboxes = INBOXES_ROOT
        original_log = ROUTING_EVENTS_LOG
        AGENTS_ROOT = Path(td)  # type: ignore
        INBOXES_ROOT = AGENTS_ROOT / 'inboxes'  # type: ignore
        ROUTING_EVENTS_LOG = AGENTS_ROOT / 'logs' / 'routing-events.jsonl'  # type: ignore
        try:
            # Happy path: beacon -> forge (allowed)
            task = {
                'task_id': 'test-001',
                'prompt': 'x' * 150,  # passes MIN_PROMPT_LEN
                'source': 'beacon',
            }
            dest = safe_write_inbox('forge', task, 'beacon', 'test-001.json')
            assert dest.exists(), dest
            assert dest.parent.name == 'forge'

            # Hard topology denial: pulse -> forge
            raised = False
            try:
                safe_write_inbox(
                    'forge',
                    {'task_id': 't2', 'prompt': 'x' * 150, 'source': 'pulse'},
                    'pulse',
                    't2.json',
                )
            except RoutingDenied:
                raised = True
            assert raised, 'expected RoutingDenied for pulse->forge'

            # Schema rejection: too-short prompt
            raised = False
            try:
                safe_write_inbox(
                    'forge',
                    {'task_id': 't3', 'prompt': 'short', 'source': 'beacon'},
                    'beacon',
                    't3.json',
                )
            except DispatchRejected as e:
                raised = True
                assert 'too short' in str(e), str(e)
            assert raised

            # Source mismatch
            raised = False
            try:
                safe_write_inbox(
                    'forge',
                    {'task_id': 't4', 'prompt': 'x' * 150, 'source': 'pulse'},
                    'beacon',  # caller says beacon, envelope says pulse
                    't4.json',
                )
            except DispatchRejected as e:
                raised = True
                assert 'source mismatch' in str(e), str(e)
            assert raised

            # Filename truncation
            long_name = 'a' * 250 + '.json'
            dest = safe_write_inbox(
                'forge',
                {'task_id': 't5', 'prompt': 'x' * 150, 'source': 'beacon'},
                'beacon',
                long_name,
            )
            assert len(dest.name.encode('utf-8')) <= MAX_FILENAME_BYTES + 20, dest.name
            assert dest.name.endswith('.json'), dest.name

            print('safe_write_inbox self-test: OK')
        finally:
            AGENTS_ROOT = original_agents  # type: ignore
            INBOXES_ROOT = original_inboxes  # type: ignore
            ROUTING_EVENTS_LOG = original_log  # type: ignore

    return 0


if __name__ == '__main__':
    sys.exit(_self_test())
