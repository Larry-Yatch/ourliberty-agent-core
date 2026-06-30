#!/usr/bin/env python3
"""Tests for the concurrency-guard log strings in ``agent_runner.run_claude``
(audit 2026-06-05 PR-L / finding #52).

The two human-facing concurrency log lines had drifted from the values the
guard actually enforces — the timeout WARN hardcoded ``Waited 120s.`` while
``wait_for_slot`` blocks up to 1800s, and the per-spawn ``Running`` line printed
``active=N/10`` while the enforced ceiling is 6 (``concurrency_guard.MAX_CONCURRENT``,
lowered by #348). These tests assert both strings are now derived from the real
constants (``ar.SLOT_WAIT_TIMEOUT`` and ``ar.MAX_CONCURRENT``) and never carry
the stale ``120s`` / ``/10`` literals.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_concurrency_log
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
import larry_alerts  # noqa: E402


def _ok_response(text='ok', session_id='sid-new'):
    return json.dumps({'result': text, 'session_id': session_id})


class _FakeProc:
    """Minimal stand-in for ``subprocess.Popen``'s returned object that exits
    immediately (mirrors the helper in test_agent_runner_active_tier)."""

    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.pid = 4242
        self.stdin = mock.MagicMock()
        self.stdout = mock.MagicMock()
        self.stdout.read.return_value = stdout
        self.stderr = mock.MagicMock()
        self.stderr.read.return_value = stderr

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def _pin_log_dir(tc, root):
    """Route agent_runner.log()'s write-time OURLIBERTY_LOG_DIR resolution
    into the test's tmp tree so un-mocked log() calls (incl. the #438
    TRANSCRIPT_NOT_PERSISTED ERROR) can't land in the real ~/agents/logs/.
    Guards the bare `python3 -m unittest` invocation — the discover gate
    (#436) pins this process-wide, but direct runs have no other layer.
    Restore registers via addCleanup, not tearDown: unittest skips tearDown
    when setUp raises (e.g. the mkdir below), but cleanups still run.
    """
    prev = os.environ.get('OURLIBERTY_LOG_DIR')

    def _restore():
        if prev is None:
            os.environ.pop('OURLIBERTY_LOG_DIR', None)
        else:
            os.environ['OURLIBERTY_LOG_DIR'] = prev

    tc.addCleanup(_restore)
    os.environ['OURLIBERTY_LOG_DIR'] = str(root / 'logs')
    (root / 'logs').mkdir(parents=True, exist_ok=True)


class _Guard:
    """Configurable concurrency-guard stand-in."""

    def __init__(self, grant=True, active=0):
        self._grant = grant
        self._active = active

    def wait_for_slot(self, *_a, **_kw):
        return self._grant

    def release(self, *_a, **_kw):
        pass

    def active_count(self):
        return self._active


class ConcurrencyTimeoutLogTest(unittest.TestCase):
    """The WARN logged when no slot frees up before the timeout must report the
    real ceiling and the real wait window — never the stale 120s / /10."""

    def _capture_timeout_warn(self, active):
        msgs = []

        with mock.patch.object(ar, 'get_guard',
                               return_value=_Guard(grant=False, active=active)), \
                mock.patch.object(ar, 'log',
                                  side_effect=lambda *a, **k: msgs.append(a)):
            result = ar.run_claude(agent_id='forge', prompt='hi')

        # run_claude returns the at-capacity sentinel without spawning.
        self.assertFalse(result[0])
        warn = next(a for a in msgs if 'Concurrency limit reached' in a[1])
        return warn[1]

    def test_timeout_warn_uses_real_timeout_not_120s(self):
        msg = self._capture_timeout_warn(active=ar.MAX_CONCURRENT)
        self.assertIn(f'Waited {ar.SLOT_WAIT_TIMEOUT}s', msg)
        self.assertNotIn('120s', msg)

    def test_timeout_warn_uses_real_ceiling_not_10(self):
        msg = self._capture_timeout_warn(active=ar.MAX_CONCURRENT)
        # active=ceiling shown as N/<ceiling>, e.g. '6/6', never '/10'.
        self.assertIn(f'/{ar.MAX_CONCURRENT} active', msg)
        self.assertNotIn('/10', msg)

    def test_wait_for_slot_called_with_named_timeout_constant(self):
        seen = {}

        def _record(self, agent_id, task_id='', timeout=None, **_kw):
            seen['timeout'] = timeout
            return False

        guard = _Guard(grant=False, active=0)
        with mock.patch.object(type(guard), 'wait_for_slot', _record), \
                mock.patch.object(ar, 'get_guard', return_value=guard), \
                mock.patch.object(ar, 'log'):
            ar.run_claude(agent_id='forge', prompt='hi')
        self.assertEqual(seen['timeout'], ar.SLOT_WAIT_TIMEOUT)


class ConcurrencyRunningLogTest(unittest.TestCase):
    """The per-spawn 'Running (...)' line must report active=N/<ceiling> using
    the enforced MAX_CONCURRENT, not the hardcoded /10."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        self.workdir = self.root / 'work'
        self.workdir.mkdir(parents=True, exist_ok=True)
        # Per-task dispatch: pin a tier via the operator override so
        # select_dispatch_tier returns a tier and run_claude actually spawns
        # (emitting the 'Running (...)' line under test) instead of TIER_HOLDing
        # because no tier is usable in the bare test env.
        (self.root / 'rotation.disabled').write_text('tier1')
        # Defense-in-depth: ar.log is mocked in today's tests, but a future
        # test that drives run_claude without the mock must not write the
        # real ~/agents/logs/forge.log.
        _pin_log_dir(self, self.root)

    def tearDown(self):
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        self.tmp.cleanup()

    def test_running_log_uses_real_ceiling_not_10(self):
        active = 3
        msgs = []

        patches = [
            mock.patch.object(ar, 'get_manager',
                              return_value=mock.Mock(
                                  get_token=lambda: ('tok', 'acct1'),
                                  check_for_rate_limit=lambda _o: False,
                                  detect_cap_in_output=lambda _o: False,
                                  report_rate_limit=lambda *a, **k: None,
                                  report_success=lambda *a, **k: None,
                              )),
            mock.patch.object(ar, 'get_guard',
                              return_value=_Guard(grant=True, active=active)),
            mock.patch('agent_runner.subprocess.Popen',
                       side_effect=lambda cmd, **kw: _FakeProc(
                           0, stdout=_ok_response())),
            mock.patch('agent_runner.subprocess.run',
                       side_effect=lambda cmd, **kw: mock.Mock(
                           returncode=0, stdout=_ok_response(), stderr='')),
            # #438 transcript check: success with a mock session id would
            # write a REAL critical alert to the live larry-alerts ledger
            mock.patch.object(larry_alerts, 'append_alert'),
            mock.patch.object(ar, 'quarantine_parent_claude_md_poison'),
            mock.patch.object(ar, 'scrub_tmp_identity_landmines'),
            mock.patch('agent_runner.time.sleep'),
            mock.patch.object(ar, 'log',
                              side_effect=lambda *a, **k: msgs.append(a)),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        ar.run_claude(agent_id='forge', prompt='hi',
                      working_dir=str(self.workdir), timeout=60)

        running = next(a for a in msgs
                       if len(a) > 1 and a[1].startswith('Running ('))
        self.assertIn(f'active={active}/{ar.MAX_CONCURRENT}', running[1])
        self.assertNotIn('/10', running[1])


if __name__ == '__main__':
    unittest.main()
