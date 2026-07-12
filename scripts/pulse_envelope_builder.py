#!/usr/bin/env python3
"""pulse_envelope_builder.py — Pulse-facing canonical dispatch-envelope builder.

WHY THIS EXISTS
---------------
Pulse-the-LLM hand-constructs `cycle-fix-<slug>.json` / `cycle-finding-<slug>.json`
dispatch envelopes during a /cycle session and raw-writes them to an agent's
inbox. The canonical field for the task body is `prompt` (what
`dispatch_validator.validate_task` reads), but across cycle iterations Pulse has
repeatedly written `body` instead. The validator ignores `body`, sees no
`prompt`, and rejects the envelope as "prompt too short" — the F24 empty-prompt
dead-letter class. Recoverable by hand, but it wastes Opus tokens every cycle.

This module makes the `body`-vs-`prompt` confusion STRUCTURALLY IMPOSSIBLE: the
caller passes the prompt TEXT as an argument and never names the key. The
builder owns canonical field naming.

TWO CONCERNS, KEPT SEPARATE
---------------------------
- ``build_envelope(...)`` is PURE: it constructs the canonical dict and
  fail-fast validates it via ``dispatch_validator.validate_task`` (the same F24
  gate the watcher enforces), raising ``EnvelopeValidationError`` on a bad
  envelope. No filesystem side-effect — so unit tests exercise construction +
  validation WITHOUT tripping the ``refuse_under_test('inbox-write')`` guard
  that fires inside ``safe_write_inbox``.
- ``write_envelope(...)`` builds, then delegates the write to
  ``safe_write_inbox`` — reusing its existing schema gate, routing-topology
  check, atomic write, and audit log. The route is enforced there: a Pulse
  ``source`` can only reach the targets allowed by
  ``routing_validator.FRESH_DISPATCH_ROUTES`` (``pulse -> beacon``); a denied
  route raises ``RoutingDenied`` loudly rather than dead-lettering silently.

CLI
---
Pulse invokes the CLI via Bash, piping the (long, multi-line) prompt on stdin to
avoid shell-escaping — mirroring the ``marker.py render ... <<'JSON'`` stdin
pattern. A validation failure aborts with a stderr diagnostic and a non-zero
exit BEFORE any inbox file is written, so a malformed envelope never reaches the
watcher::

    python3 scripts/pulse_envelope_builder.py beacon \\
        --task-id cycle-finding-foo-20260612T000000Z \\
        --dedup-identity cycle-finding:foo \\
        --timeout 3600 <<'PROMPT'
    Pulse observed <finding> in cycles <iter-list>. <evidence>. <why it matters>.
    <proposed fix shape>. <acceptance criteria>.
    PROMPT
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Optional

# Allow imports of sibling modules in scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatch_validator  # noqa: E402
import safe_write_inbox  # noqa: E402

DEFAULT_SOURCE = 'pulse'


class EnvelopeValidationError(Exception):
    """The constructed envelope failed ``dispatch_validator.validate_task``."""


def _primary_chat_id() -> Optional[int]:
    """Larry's primary Telegram chat — the lowest id in TELEGRAM_ALLOWED_CHAT_IDS
    (mirrors pulse_check_i / heal_unregistered_approval / outbox_notifier
    _primary_chat_id). None only when the allow-list is unset/empty."""
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    ids = []
    for tok in raw.replace(",", " ").split():
        try:
            ids.append(int(tok))
        except ValueError:
            continue
    return min(ids) if ids else None


def build_envelope(
    task_id: str,
    prompt: str,
    *,
    source: str = DEFAULT_SOURCE,
    dedup_identity: Optional[str] = None,
    timeout: Optional[int] = None,
) -> dict[str, Any]:
    """Construct and fail-fast-validate the canonical dispatch envelope.

    Returns the dict ``{task_id, source, prompt[, dedup_identity][, timeout]}``.
    The ``prompt`` key is owned here — the caller supplies the TEXT, never the
    key name, so a ``body`` typo is structurally impossible.

    Raises:
        EnvelopeValidationError: the envelope fails the F24 / schema gate
            (e.g. prompt < 100 chars, source not allow-listed, timeout out of
            bounds). Raised BEFORE any write, so a malformed envelope never
            reaches the inbox watcher.
    """
    envelope: dict[str, Any] = {
        'task_id': task_id,
        'source': source,
        'prompt': prompt,
    }
    if dedup_identity is not None:
        envelope['dedup_identity'] = dedup_identity
    if timeout is not None:
        envelope['timeout'] = timeout
    # Stamp the recipient at creation so the downstream APPROVAL_REQUEST marker
    # carries a real reply_chat_id (#933 null-chat pattern) — omit the key when
    # unresolvable so the notifier's fallback stays intact (never stamp null).
    reply_chat_id = _primary_chat_id()
    if reply_chat_id is not None:
        envelope['reply_chat_id'] = reply_chat_id

    ok, reason = dispatch_validator.validate_task(envelope)
    if not ok:
        raise EnvelopeValidationError(reason)
    return envelope


def write_envelope(
    target_agent: str,
    task_id: str,
    prompt: str,
    *,
    source: str = DEFAULT_SOURCE,
    dedup_identity: Optional[str] = None,
    timeout: Optional[int] = None,
) -> Path:
    """Build, validate, and atomically write the envelope to the target inbox.

    Delegates the write to ``safe_write_inbox`` (schema gate + routing-topology
    check + atomic write + audit log). The on-disk filename is ``<task_id>.json``.

    Raises:
        EnvelopeValidationError: construction-time schema failure (no write).
        safe_write_inbox.DispatchRejected: schema rejection at the write gate.
        safe_write_inbox.RoutingDenied: the (source, target) route is denied.

    Returns:
        Path to the written file.
    """
    envelope = build_envelope(
        task_id,
        prompt,
        source=source,
        dedup_identity=dedup_identity,
        timeout=timeout,
    )
    return safe_write_inbox.safe_write_inbox(
        target_agent, envelope, source, f'{task_id}.json',
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='pulse_envelope_builder.py',
        description=(
            'Build + write a canonical Pulse dispatch envelope. The prompt is '
            'read from stdin (use a heredoc). The prompt key is owned by the '
            'builder, so a body-vs-prompt typo is impossible.'
        ),
    )
    parser.add_argument(
        'target_agent',
        help='Inbox to dispatch to (e.g. beacon). Routing topology is enforced '
             'by safe_write_inbox; a pulse source can only reach beacon.',
    )
    parser.add_argument('--task-id', required=True, help='Unique task id; on-disk name is <task_id>.json.')
    parser.add_argument('--source', default=DEFAULT_SOURCE, help=f'Envelope source (default: {DEFAULT_SOURCE}).')
    parser.add_argument('--dedup-identity', default=None, help='Optional dedup_identity (e.g. cycle-finding:<slug>).')
    parser.add_argument('--timeout', type=int, default=None, help='Optional timeout in seconds (60..14400).')
    args = parser.parse_args(argv)

    prompt = sys.stdin.read()
    if not prompt.strip():
        sys.stderr.write(
            'pulse_envelope_builder.py: stdin is empty. Pipe the prompt TEXT, '
            "e.g.:\n  python3 scripts/pulse_envelope_builder.py beacon "
            "--task-id t <<'PROMPT'\n  <your prompt>\n  PROMPT\n"
        )
        return 2

    # Fail-fast: build_envelope validates BEFORE any write, so a malformed
    # envelope never reaches the watcher.
    try:
        envelope = build_envelope(
            args.task_id,
            prompt,
            source=args.source,
            dedup_identity=args.dedup_identity,
            timeout=args.timeout,
        )
    except EnvelopeValidationError as e:
        sys.stderr.write(f'pulse_envelope_builder.py: envelope rejected: {e}\n')
        return 1

    try:
        dest = safe_write_inbox.safe_write_inbox(
            args.target_agent, envelope, args.source, f'{args.task_id}.json',
        )
    except safe_write_inbox.DispatchRejected as e:
        sys.stderr.write(f'pulse_envelope_builder.py: dispatch rejected: {e}\n')
        return 1
    except safe_write_inbox.RoutingDenied as e:
        sys.stderr.write(f'pulse_envelope_builder.py: routing denied: {e}\n')
        return 1

    sys.stdout.write(f'{dest}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
