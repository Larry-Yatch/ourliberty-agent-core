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
import os
import subprocess
import sys
import tempfile
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


class AffixedForgeTaskIdWarning(unittest.TestCase):
    """marker-taskid-normalize-001: warn at author time when a Forge marker
    task_id carries a `forge-`/`forge/` affix. The envelope task_id is BARE;
    the affix is the recurring drift that dead-letters at the notifier. Warn to
    stderr but still render/exit-0 — a same-turn nudge, not a hard failure."""

    def test_render_warns_for_forge_dash_affixed_task_id(self):
        payload = {'task_id': 'forge-m4-pr2', 'preflight_summary': 'ok'}
        rc, out, err = _run_cli(
            json.dumps(payload), ['render', 'forge', 'proceed'],
        )
        self.assertEqual(rc, 0, err)
        self.assertIn('WARNING', err)
        self.assertIn('forge-m4-pr2', err)
        # Still renders the block.
        self.assertIn('=== PROCEED ===', out)

    def test_render_warns_for_forge_slash_affixed_task_id(self):
        payload = {'task_id': 'forge/m5-pr1', 'preflight_summary': 'ok'}
        rc, out, err = _run_cli(
            json.dumps(payload), ['render', 'forge', 'proceed'],
        )
        self.assertEqual(rc, 0, err)
        self.assertIn('WARNING', err)
        self.assertIn('forge/m5-pr1', err)

    def test_render_no_warning_for_bare_task_id(self):
        payload = {'task_id': 'm4-pr2', 'preflight_summary': 'ok'}
        rc, out, err = _run_cli(
            json.dumps(payload), ['render', 'forge', 'proceed'],
        )
        self.assertEqual(rc, 0, err)
        self.assertNotIn('WARNING', err)

    def test_validate_warns_for_affixed_task_id(self):
        payload = {'task_id': 'forge-m6-pr1', 'preflight_summary': 'ok'}
        rc, out, err = _run_cli(
            json.dumps(payload), ['validate', 'forge', 'proceed'],
        )
        self.assertEqual(rc, 0, err)
        self.assertIn('WARNING', err)

    def test_no_warning_for_non_forge_agent(self):
        # The affix pattern is Forge-specific; a Beacon task_id that happens to
        # start with `forge-` should not trip the warning.
        payload = {
            'task_id': 'forge-something',
            'summary': 'x',
            'target_agent': 'forge',
            'prompt': 'y',
        }
        rc, out, err = _run_cli(
            json.dumps(payload), ['render', 'beacon', 'approval_request'],
        )
        self.assertEqual(rc, 0, err)
        self.assertNotIn('WARNING', err)


class RenderLedger(unittest.TestCase):
    """lost-marker-render-emission-net-001 Part A: a SUCCESSFUL beacon
    approval_request render appends exactly one JSONL record to the render
    ledger; a render VALIDATION failure writes none; a ledger-write failure
    never fails the render. Ledger path is env-overridable for hermeticity."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ledger = Path(self._td.name) / 'marker-render-ledger.jsonl'
        patch = mock.patch.dict(os.environ, {
            'OURLIBERTY_MARKER_RENDER_LEDGER': str(self.ledger),
        })
        patch.start()
        self.addCleanup(patch.stop)

    def _read_ledger(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        return [json.loads(ln) for ln in
                self.ledger.read_text().splitlines() if ln.strip()]

    def _approval_payload(self, **extra) -> dict:
        payload = {
            'task_id': 'ledger-abc-001',
            'summary': 'ship the thing',
            'target_agent': 'forge',
            'prompt': 'do the work',
        }
        payload.update(extra)
        return payload

    def test_successful_render_writes_one_entry(self):
        rc, out, err = _run_cli(
            json.dumps(self._approval_payload()),
            ['render', 'beacon', 'approval_request'],
        )
        self.assertEqual(rc, 0, err)
        entries = self._read_ledger()
        self.assertEqual(len(entries), 1)
        rec = entries[0]
        self.assertEqual(rec['task_id'], 'ledger-abc-001')
        self.assertEqual(rec['agent'], 'beacon')
        self.assertEqual(rec['marker_type'], 'approval_request')
        self.assertEqual(rec['summary'], 'ship the thing')
        self.assertIn('rendered_at', rec)
        # rendered_at must be parseable ISO-8601.
        from datetime import datetime
        datetime.fromisoformat(rec['rendered_at'])

    def test_phase_flag_is_recorded(self):
        rc, out, err = _run_cli(
            json.dumps(self._approval_payload()),
            ['render', 'beacon', 'approval_request',
             '--phase', 'routing-signal'],
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._read_ledger()[0]['phase'], 'routing-signal')

    def test_validation_failure_writes_no_entry(self):
        # Missing 'summary' -> render validation error -> no ledger entry.
        payload = {'task_id': 'x', 'target_agent': 'forge', 'prompt': 'y'}
        rc, out, err = _run_cli(
            json.dumps(payload), ['render', 'beacon', 'approval_request'],
        )
        self.assertNotEqual(rc, 0)
        self.assertEqual(self._read_ledger(), [])

    def test_missing_task_id_writes_no_entry(self):
        # A payload with a summary/target/prompt but no task_id would fail
        # render validation anyway; assert nothing is written even if a caller
        # bypasses required fields — the ledger writer requires a task_id.
        marker_cli._record_render_to_ledger(
            'beacon', 'approval_request', {'summary': 's'})
        self.assertEqual(self._read_ledger(), [])

    def test_forge_render_writes_no_entry(self):
        # Scope is beacon approval_request only. A Forge proceed render must
        # not touch the ledger (its emission surface is the outbox).
        rc, out, err = _run_cli(
            json.dumps({'task_id': 't-1', 'preflight_summary': 'ok'}),
            ['render', 'forge', 'proceed'],
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._read_ledger(), [])

    def test_ledger_write_failure_never_fails_render(self):
        # Point the ledger at a path whose parent can't be created (a file used
        # as a directory component) so the append raises; the render must still
        # succeed and emit the block.
        blocker = Path(self._td.name) / 'blocker'
        blocker.write_text('i am a file, not a dir')
        with mock.patch.dict(os.environ, {
            'OURLIBERTY_MARKER_RENDER_LEDGER': str(blocker / 'nested' / 'l.jsonl'),
        }):
            rc, out, err = _run_cli(
                json.dumps(self._approval_payload()),
                ['render', 'beacon', 'approval_request'],
            )
        self.assertEqual(rc, 0, err)
        self.assertIn('=== APPROVAL_REQUEST ===', out)
        self.assertIn('render-ledger append failed', err)


if __name__ == '__main__':
    unittest.main()
