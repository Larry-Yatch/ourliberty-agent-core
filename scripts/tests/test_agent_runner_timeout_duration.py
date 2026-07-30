#!/usr/bin/env python3
"""Wall-clock-timeout telemetry on ``run_claude``'s terminal timeout return
(delegate-died-surface-001, deep-review of PR #1068).

The 2026-07-29/30 incident envelope read ``exit_code=-1``,
``result='TIMEOUT after 600s'``, ``duration_sec=None`` — and a null duration on
an exit -1 envelope is two thirds of the spawn-failure shape the notifier
classifiers (``_prior_build_was_spawn_failure`` /
``_prior_dispatch_was_definitive_non_run``) read as 'the worker never started'.
A run that reached the ceiling ran for the FULL window; it is the opposite.

These tests pin BOTH halves of that at the seam that actually reaches the
envelope, because the two are easy to confuse:

  * ``duration_sec`` reaches the outbox from ``out_meta`` — the envelope field
    is ``meta.get('duration_sec')`` in ``inbox_watcher._build_outbox``. The
    timeout path had to start populating it; the success path already did.
  * the return tuple's THIRD element is ``new_session_id``, written to the
    envelope as ``claude_session_id`` — a ``str | None`` field that downstream
    slices (``[:12]``), globs into a session-log path, and propagates as a
    ``--resume`` id. A duration must never be returned there.

The last test drives the real ``_build_outbox`` so the assertion is about the
envelope Beacon's archive actually receives, not about an intermediate.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_timeout_duration
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import agent_runner as ar  # noqa: E402
import active_tier  # noqa: E402
import inbox_watcher as iw  # noqa: E402


class _NeverExitsProc:
    """``subprocess.Popen`` stand-in that never finishes, so run_claude's poll
    loop runs to the wall-clock ceiling instead of reaping a result."""

    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.stdin = mock.MagicMock()
        self.stdout = mock.MagicMock()
        self.stdout.read.return_value = ''
        self.stderr = mock.MagicMock()
        self.stderr.read.return_value = ''
        self.terminated = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        return -1

    def terminate(self):
        self.terminated = True

    def kill(self):
        pass


class _NoopGuard:
    def wait_for_slot(self, *_a, **_kw):
        return True

    def release(self, *_a, **_kw):
        pass

    def active_count(self):
        return 0


def _pin_log_dir(tc, root):
    """Keep un-mocked log() writes (the intentional 'Timeout after Ns' ERROR)
    inside the test's tmp tree rather than the real ~/agents/logs/."""
    prev = os.environ.get('OURLIBERTY_LOG_DIR')

    def _restore():
        if prev is None:
            os.environ.pop('OURLIBERTY_LOG_DIR', None)
        else:
            os.environ['OURLIBERTY_LOG_DIR'] = prev

    tc.addCleanup(_restore)
    os.environ['OURLIBERTY_LOG_DIR'] = str(root / 'logs')
    (root / 'logs').mkdir(parents=True, exist_ok=True)


class RunClaudeTimeoutTelemetryTest(unittest.TestCase):
    """Drive run_claude's real poll loop to the ceiling and inspect what it
    hands back to inbox_watcher."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        self.workdir = self.root / 'work'
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.home = self.root / 'home'
        self.home.mkdir(parents=True, exist_ok=True)
        state = self.root / 'blackboard' / 'active-tier.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({'tier': 'tier1'}))
        (self.root / 'rotation.disabled').write_text('tier1')
        _pin_log_dir(self, self.root)

    def tearDown(self):
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        self.tmp.cleanup()

    def _run_to_timeout(self):
        """Run to the wall on the first poll tick (time.sleep is mocked, and
        the caller timeout equals one CANCEL_POLL_INTERVAL). Returns
        (result_tuple, out_meta)."""
        proc = _NeverExitsProc()
        meta: dict = {}
        patches = [
            mock.patch.object(ar, 'get_manager',
                              return_value=mock.Mock(
                                  get_token=lambda: ('tok', 'acct1'),
                                  check_for_rate_limit=lambda _o: False,
                                  detect_cap_in_output=lambda _o: False,
                                  report_rate_limit=lambda *a, **k: None,
                                  report_success=lambda *a, **k: None,
                              )),
            mock.patch.object(ar, 'get_guard', return_value=_NoopGuard()),
            # The spawn IS mocked (below), so the isolation guard's refusal is
            # the false positive it exists to prevent — neutralize it for the
            # patched call only.
            mock.patch.object(ar, 'refuse_under_test'),
            mock.patch('agent_runner.subprocess.Popen',
                       side_effect=lambda cmd, **kw: proc),
            mock.patch.object(active_tier, 'current_home',
                              return_value=str(self.home)),
            mock.patch.object(active_tier, 'TIER1_HOME', str(self.home)),
            mock.patch.object(ar, 'quarantine_parent_claude_md_poison'),
            mock.patch.object(ar, 'scrub_tmp_identity_landmines'),
            mock.patch('agent_runner.time.sleep'),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        result = ar.run_claude(
            agent_id='beacon',
            prompt='scope this card',
            working_dir=str(self.workdir),
            timeout=ar.CANCEL_POLL_INTERVAL,
            out_meta=meta,
        )
        return result, meta, proc

    def test_timeout_records_real_duration_on_out_meta(self):
        # `duration_sec` reaches the envelope from out_meta, NOT the return
        # tuple. A null here is what let the incident envelope look like a
        # worker that never spawned.
        (_success, _out, _third), meta, _proc = self._run_to_timeout()
        self.assertIn('duration_sec', meta)
        self.assertIsInstance(meta['duration_sec'], float)
        self.assertGreaterEqual(meta['duration_sec'], 0.0)
        self.assertTrue(meta['timed_out'])
        self.assertEqual(meta['timeout_seconds'], ar.CANCEL_POLL_INTERVAL)

    def test_timeout_returns_terminal_shape(self):
        (success, out, third), _meta, proc = self._run_to_timeout()
        self.assertFalse(success)
        self.assertIn('TIMEOUT after', out)
        self.assertTrue(proc.terminated, 'the wall-clock kill must fire')

    def test_third_return_element_is_a_session_id_slot_not_a_duration(self):
        # Contract: run_claude returns (success, output_text, new_session_id).
        # The third element becomes the envelope's `claude_session_id`, a
        # str|None consumed by string slicing, session-log globbing, and
        # `--resume`. A number here corrupts that field while fixing nothing,
        # because duration_sec does not come from this slot.
        (_success, _out, third), _meta, _proc = self._run_to_timeout()
        self.assertIsNone(third)
        self.assertNotIsInstance(third, (int, float))

    def test_envelope_seam_carries_duration_and_null_session_id(self):
        # The assertion that matches the incident artifact: build the outbox
        # exactly as inbox_watcher does from what run_claude handed back.
        (success, out, third), meta, _proc = self._run_to_timeout()
        envelope = iw._build_outbox(
            'beacon', 'delegate-cap-demo-abcd', {}, Path('/tmp/t.json'),
            success, out, third, meta, error=out,
        )
        self.assertEqual(envelope['exit_code'], -1)
        self.assertTrue(envelope['timed_out'])
        self.assertIsInstance(envelope['duration_sec'], float)
        self.assertIsNone(envelope['claude_session_id'])


if __name__ == '__main__':
    unittest.main()
