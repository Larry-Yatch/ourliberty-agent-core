#!/usr/bin/env python3
"""Tests for the `marker.py` CLI surface.

Covers the V3 (`orchestrator-rectification-v2`) `--phase` flag — the new
slot Beacon needs to mint DAG-preflight APPROVAL_REQUEST markers with
`"phase": "routing-signal"` without hand-editing the JSON payload.
Bootstrap-002 caught two consecutive Larry-approve rejections because
the field was absent and the prior CLI had no way to add it.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_marker
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import beacon_approval_handler as bah  # noqa: E402
import marker as marker_cli            # noqa: E402


def _run_cli(stdin_text: str, argv: list[str]) -> tuple[int, str, str]:
    """Invoke `marker.main(argv)` with mocked stdin/stdout/stderr.

    Returns `(exit_code, stdout, stderr)`. Patches sys.stdin/out/err for
    the duration of the call so we don't need a subprocess for argparse
    behavior tests.
    """
    fake_stdin = io.StringIO(stdin_text)
    fake_stdout = io.StringIO()
    fake_stderr = io.StringIO()
    with (
        mock.patch.object(sys, 'stdin', fake_stdin),
        mock.patch.object(sys, 'stdout', fake_stdout),
        mock.patch.object(sys, 'stderr', fake_stderr),
    ):
        rc = marker_cli.main(argv)
    return rc, fake_stdout.getvalue(), fake_stderr.getvalue()


class PhaseFlagRender(unittest.TestCase):
    """V3: --phase merges into payload and renders into the marker JSON."""

    def test_phase_flag_emits_field_in_approval_request(self):
        payload = {
            'task_id': 'dag-preflight-abc',
            'summary': 'DAG preflight for sequence abc',
            'target_agent': 'mirror',
            'prompt': 'review-sequence-dag abc',
        }
        rc, out, err = _run_cli(
            json.dumps(payload),
            ['render', 'beacon', 'approval_request',
             '--phase', 'routing-signal'],
        )
        self.assertEqual(rc, 0, err)
        # Parse the marker block — the renderer pretty-prints JSON between
        # `=== APPROVAL_REQUEST ===` and `=== END_APPROVAL_REQUEST ===`.
        parsed, _narrative = bah.extract_approval_request(out)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get('phase'), 'routing-signal')
        # The required fields are still there.
        for k, v in payload.items():
            self.assertEqual(parsed.get(k), v)

    def test_no_phase_flag_omits_field(self):
        """Backward-compat: existing callers that don't pass --phase get
        identical output to before V3."""
        payload = {
            'task_id': 'spec-abc',
            'summary': 'Adopt spec abc',
            'target_agent': 'forge',
            'prompt': 'land spec abc — see docs/specs/abc.md',
        }
        rc, out, err = _run_cli(
            json.dumps(payload),
            ['render', 'beacon', 'approval_request'],
        )
        self.assertEqual(rc, 0, err)
        parsed, _narrative = bah.extract_approval_request(out)
        self.assertIsNotNone(parsed)
        self.assertNotIn('phase', parsed)

    def test_phase_flag_conflict_with_stdin_phase_fails(self):
        """If the caller supplies `phase` both in the stdin payload AND
        on the CLI, the CLI errors out rather than silently overwriting."""
        payload = {
            'task_id': 'abc',
            'summary': 'x',
            'target_agent': 'forge',
            'prompt': 'y',
            'phase': 'something-else',
        }
        rc, out, err = _run_cli(
            json.dumps(payload),
            ['render', 'beacon', 'approval_request',
             '--phase', 'routing-signal'],
        )
        self.assertNotEqual(rc, 0)
        self.assertIn('phase', err)
        self.assertEqual(out, '')

    def test_phase_flag_on_forge_proceed_also_injects(self):
        """The CLI doesn't gate the flag by agent/type — every handler
        preserves extra fields, so the flag is generic. (Today's intended
        caller is Beacon, but constraining the CLI would cause silent
        breakage if a future handler needs a phase-like field.)"""
        payload = {
            'task_id': 't-001',
            'preflight_summary': 'looks buildable',
        }
        rc, out, err = _run_cli(
            json.dumps(payload),
            ['render', 'forge', 'proceed', '--phase', 'whatever'],
        )
        self.assertEqual(rc, 0, err)
        # Forge proceed parser returns a 3-tuple.
        import forge_preflight_handler as fph
        mtype, parsed, _ = fph.parse_forge_marker(out)
        self.assertEqual(mtype, 'proceed')
        self.assertEqual(parsed.get('phase'), 'whatever')


class CLISmoke(unittest.TestCase):
    """Sanity tests on the CLI surface that aren't V3-specific but are
    cheap to lock in alongside the V3 tests so we have a real
    `test_marker.py` file going forward (spec V3 calls for one)."""

    def test_types_subcommand_lists_approval_request(self):
        rc, out, err = _run_cli('', ['types', 'beacon'])
        self.assertEqual(rc, 0, err)
        self.assertIn("'approval_request'", out)

    def test_render_with_unknown_marker_type_errors(self):
        payload = {'task_id': 'x'}
        rc, out, err = _run_cli(
            json.dumps(payload),
            ['render', 'beacon', 'bogus_type'],
        )
        self.assertNotEqual(rc, 0)
        self.assertIn('unknown marker type', err)

    def test_render_with_missing_required_field_errors(self):
        # Beacon approval_request requires task_id, summary, target_agent,
        # prompt — omit summary.
        payload = {
            'task_id': 'x', 'target_agent': 'forge', 'prompt': 'y',
        }
        rc, out, err = _run_cli(
            json.dumps(payload),
            ['render', 'beacon', 'approval_request'],
        )
        self.assertNotEqual(rc, 0)
        self.assertIn('missing required fields', err)


if __name__ == '__main__':
    unittest.main()
