#!/usr/bin/env python3
"""marker.py — CLI wrapper around the per-agent render_marker helpers.

Phase E1.1. Agents (Forge / Mirror / Beacon) call this from Bash to produce
canonical marker blocks instead of hand-typing the delimiters. Hand-typing
is how PR #16 stalled for 7+ hours: Mirror emitted bare `REVIEW_PASS` with
no delimiters, the parser missed it, the notifier dead-lettered silently.
The render helper makes that failure shape impossible at the source.

The actual rendering logic lives in each agent's handler module
(forge_preflight_handler, mirror_review_handler, beacon_approval_handler) —
this CLI is a thin dispatcher so agents don't have to import Python modules
from their working directory.

Usage:

  echo '{"task_id":"abc-001","preflight_summary":"..."}' \\
    | python3 ~/agent-core/scripts/marker.py render forge proceed

  echo '{"task_id":"abc-001","pr_url":"https://...","summary":"..."}' \\
    | python3 ~/agent-core/scripts/marker.py validate mirror review_pass

  python3 ~/agent-core/scripts/marker.py types beacon

Subcommands:
  render <agent> <type>    Read JSON payload from stdin, print marker to stdout.
                           Exits 0 on success, 1 with diagnostic to stderr on
                           validation error.
  validate <agent> <type>  Read JSON payload from stdin, exit 0 if valid for
                           that marker type, 1 with diagnostic to stderr if not.
                           Use this to pre-check a payload before committing
                           to the rendered form.
  types <agent>            Print available marker types for that agent and
                           their required fields. No stdin required.

Agents: forge, mirror, beacon.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import forge_preflight_handler as fph
import mirror_review_handler as mrh
import beacon_approval_handler as bah


# Map agent name → handler module. Single source of truth for the CLI surface.
HANDLERS = {
    'forge': fph,
    'mirror': mrh,
    'beacon': bah,
}


def _resolve_handler(agent: str):
    """Look up the handler module for an agent name, or exit with diagnostic."""
    if agent not in HANDLERS:
        sys.stderr.write(
            f'marker.py: unknown agent {agent!r}. '
            f'Valid agents: {sorted(HANDLERS)}\n'
        )
        sys.exit(2)
    return HANDLERS[agent]


def _read_stdin_json() -> dict[str, Any]:
    """Read a single JSON object from stdin. Exit 2 with diagnostic on failure."""
    raw = sys.stdin.read()
    if not raw.strip():
        sys.stderr.write(
            'marker.py: stdin is empty. Pipe a JSON payload object, e.g.:\n'
            "  echo '{\"task_id\":\"x\",\"preflight_summary\":\"y\"}' "
            '| python3 scripts/marker.py render forge proceed\n'
        )
        sys.exit(2)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f'marker.py: stdin is not valid JSON: {e}\n')
        sys.exit(2)
    if not isinstance(payload, dict):
        sys.stderr.write(
            f'marker.py: stdin JSON must be an object (dict), '
            f'got {type(payload).__name__}\n'
        )
        sys.exit(2)
    return payload


def cmd_render(args: argparse.Namespace) -> int:
    """render <agent> <type>: print canonical marker block to stdout."""
    handler = _resolve_handler(args.agent)
    payload = _read_stdin_json()
    # V3 (orchestrator-rectification-v2): --phase merges into the payload so
    # Beacon can mint approval_request markers with `"phase": "routing-signal"`
    # for DAG-preflight emissions without hand-editing JSON. The flag is
    # accepted on every (agent, type) — all three handlers preserve extra
    # fields. Backward-compatible: omit the flag and no `phase` field is
    # emitted. A `phase` already present in the stdin payload is rejected so
    # the CLI doesn't silently overwrite caller intent.
    if args.phase is not None:
        if 'phase' in payload:
            sys.stderr.write(
                'marker.py render: --phase conflicts with `phase` already '
                'present in the stdin payload. Provide it via exactly one path.\n'
            )
            return 1
        payload['phase'] = args.phase
    try:
        sys.stdout.write(handler.render_marker(args.type, **payload))
    except ValueError as e:
        sys.stderr.write(f'marker.py render: {e}\n')
        return 1
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """validate <agent> <type>: exit 0 if payload is valid, 1 otherwise."""
    handler = _resolve_handler(args.agent)
    payload = _read_stdin_json()
    try:
        # render_marker's validation IS the validation. Discard the output.
        handler.render_marker(args.type, **payload)
    except ValueError as e:
        sys.stderr.write(f'marker.py validate: {e}\n')
        return 1
    sys.stdout.write(f'OK: payload is valid for {args.agent}.{args.type}\n')
    return 0


def cmd_types(args: argparse.Namespace) -> int:
    """types <agent>: list marker types and their required fields."""
    handler = _resolve_handler(args.agent)
    print(f'Marker types for agent {args.agent!r}:')
    for mtype in handler.MARKER_TYPES:
        keyword = handler.MARKER_KEYWORDS[mtype]
        required = sorted(handler.REQUIRED_FIELDS[mtype])
        print(f'  {mtype!r:32s} keyword=={keyword:25s} required={required}')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='marker.py',
        description='Render / validate / introspect agent marker blocks.',
    )
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_render = sub.add_parser('render', help='Read JSON from stdin, print marker.')
    p_render.add_argument('agent', choices=sorted(HANDLERS))
    p_render.add_argument('type', help='Marker type (see `types <agent>`).')
    p_render.add_argument(
        '--phase',
        default=None,
        help=(
            'Optional `phase` field merged into the payload before rendering. '
            'Used by Beacon for `--phase routing-signal` on DAG-preflight '
            'APPROVAL_REQUEST markers. Omit to render without a phase field.'
        ),
    )
    p_render.set_defaults(func=cmd_render)

    p_validate = sub.add_parser('validate', help='Read JSON from stdin, check valid.')
    p_validate.add_argument('agent', choices=sorted(HANDLERS))
    p_validate.add_argument('type', help='Marker type (see `types <agent>`).')
    p_validate.set_defaults(func=cmd_validate)

    p_types = sub.add_parser('types', help='List marker types for an agent.')
    p_types.add_argument('agent', choices=sorted(HANDLERS))
    p_types.set_defaults(func=cmd_types)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
