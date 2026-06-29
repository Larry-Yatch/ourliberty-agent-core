#!/usr/bin/env python3
"""Tests for the transcript-persistence check in ``agent_runner`` (2026-06-10
EROFS incident fix).

A session whose transcript ``.jsonl`` never landed on disk cannot be resumed —
the next phase's ``--resume <session_id>`` dies with 'No conversation found'.
``run_claude`` now verifies the transcript exists after a successful run and,
when it is absent, logs an ERROR + emits a larry-alert keyed
``subject=transcript-not-persisted:<tier>`` so the failure is LOUD at creation
time instead of silently stranding the next dispatch.

Two branches are covered:
  * transcript present  -> returns True, no ERROR, no alert
  * transcript missing  -> returns False, ERROR logged, alert emitted

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_transcript_persisted
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
import larry_alerts  # noqa: E402


def _ok_response(text='ok', session_id='sid-new'):
    return json.dumps({'result': text, 'session_id': session_id})


def _transcript_path(home: Path, cwd: str, session_id: str) -> Path:
    """Mirror the path scheme the check uses: Claude Code stores transcripts
    at <home>/.claude/projects/<cwd-slug>/<sid>.jsonl, slug = cwd with every
    '/' replaced by '-'."""
    slug = cwd.replace('/', '-')
    return home / '.claude' / 'projects' / slug / (session_id + '.jsonl')


def _pin_log_dir(tc, root):
    """Route agent_runner.log()'s write-time OURLIBERTY_LOG_DIR resolution
    into the test's tmp tree so un-mocked log() calls (incl. the intentional
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


class VerifyTranscriptPersistedUnitTest(unittest.TestCase):
    """Direct unit tests of ``_verify_transcript_persisted`` over a real
    temp filesystem (both branches)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / 'home'
        self.cwd = '/home/larry/agent-worktrees/wt-forge-demo'
        self.sid = 'sess-abc123def456'

    def tearDown(self):
        self.tmp.cleanup()

    def test_present_returns_true_no_error_no_alert(self):
        path = _transcript_path(self.home, self.cwd, self.sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"summary"}\n')

        log_lines = []
        with mock.patch.object(ar, 'log',
                               side_effect=lambda a, m, level='INFO':
                               log_lines.append((level, m))), \
                mock.patch.object(larry_alerts, 'append_alert') as alert:
            result = ar._verify_transcript_persisted(
                'forge', str(self.home), 'tier1', self.cwd, self.sid,
                task_stem='demo-task',
            )

        self.assertTrue(result)
        alert.assert_not_called()
        self.assertEqual(
            [l for l in log_lines if l[0] == 'ERROR'], [],
            'no ERROR should be logged when the transcript exists',
        )

    def test_absent_returns_false_logs_error_and_alerts(self):
        # No transcript written: the check must fail LOUD.
        log_lines = []
        with mock.patch.object(ar, 'log',
                               side_effect=lambda a, m, level='INFO':
                               log_lines.append((level, m))), \
                mock.patch.object(larry_alerts, 'append_alert') as alert:
            result = ar._verify_transcript_persisted(
                'forge', str(self.home), 'tier2', self.cwd, self.sid,
                task_stem='demo-task',
            )

        self.assertFalse(result)
        # ERROR logged
        error_lines = [l for l in log_lines if l[0] == 'ERROR']
        self.assertEqual(len(error_lines), 1)
        self.assertIn('TRANSCRIPT_NOT_PERSISTED', error_lines[0][1])
        # larry-alert emitted with the tier-keyed subject
        alert.assert_called_once()
        kwargs = alert.call_args.kwargs
        self.assertEqual(kwargs['subject'], 'transcript-not-persisted:tier2')
        self.assertEqual(kwargs['severity'], 'critical')

    def test_absent_subject_carries_active_tier(self):
        # The subject suffix must name the tier that actually ran, so distinct
        # tiers get distinct alert cooldown buckets.
        with mock.patch.object(ar, 'log'), \
                mock.patch.object(larry_alerts, 'append_alert') as alert:
            ar._verify_transcript_persisted(
                'mirror', str(self.home), 'tier1', self.cwd, self.sid,
            )
        self.assertEqual(
            alert.call_args.kwargs['subject'],
            'transcript-not-persisted:tier1',
        )


class _FakeProc:
    """Minimal stand-in for ``subprocess.Popen``'s returned object — returns
    its ``returncode`` on first ``poll()`` so run_claude exits without
    sleeping."""

    def __init__(self, returncode, stdout='', stderr=''):
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


class _NoopGuard:
    def wait_for_slot(self, *_a, **_kw):
        return True

    def release(self, *_a, **_kw):
        pass

    def active_count(self):
        return 0


class RunClaudeTranscriptCheckIntegrationTest(unittest.TestCase):
    """Prove the check is actually wired into ``run_claude``'s success path:
    a happy-path run whose transcript is missing must emit the alert; one
    whose transcript exists must not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        import os
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        self.workdir = self.root / 'work'
        self.workdir.mkdir(parents=True, exist_ok=True)
        # Per-test fake HOME so the check targets a writable temp path
        # instead of the real /home/larry.
        self.home = self.root / 'home'
        self.home.mkdir(parents=True, exist_ok=True)
        # active-tier state = tier1 so effective_tier resolves to 'tier1'.
        state = self.root / 'blackboard' / 'active-tier.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({'tier': 'tier1'}))
        # This class drives run_claude without mocking ar.log.
        _pin_log_dir(self, self.root)

    def tearDown(self):
        import os
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        self.tmp.cleanup()

    def _run(self, session_id):
        proc = _FakeProc(0, stdout=_ok_response(session_id=session_id))

        def fake_popen(cmd, **kwargs):
            return proc

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
            mock.patch('agent_runner.subprocess.Popen',
                       side_effect=fake_popen),
            # Drive the active HOME to our writable temp dir so the check
            # looks for the transcript somewhere the test controls.
            mock.patch.object(active_tier, 'current_home',
                              return_value=str(self.home)),
            # Root fix reads TIER1_HOME directly on the setup-token path —
            # patch it to the same temp home so the check targets it.
            mock.patch.object(active_tier, 'TIER1_HOME', str(self.home)),
            mock.patch.object(ar, 'quarantine_parent_claude_md_poison'),
            mock.patch.object(ar, 'scrub_tmp_identity_landmines'),
            mock.patch('agent_runner.time.sleep'),
        ]
        alert = mock.patch.object(larry_alerts, 'append_alert')
        patches.append(alert)
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in patches])
        result = ar.run_claude(
            agent_id='forge',
            prompt='hello',
            working_dir=str(self.workdir),
            timeout=60,
        )
        return result, started[-1]

    def test_missing_transcript_emits_alert(self):
        sid = 'sess-missing-001'
        (success, _out, ret_sid), alert = self._run(sid)
        self.assertTrue(success)
        self.assertEqual(ret_sid, sid)
        alert.assert_called_once()
        self.assertEqual(
            alert.call_args.kwargs['subject'],
            'transcript-not-persisted:tier1',
        )

    def test_present_transcript_no_alert(self):
        sid = 'sess-present-001'
        path = _transcript_path(self.home, str(self.workdir), sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"summary"}\n')
        (success, _out, ret_sid), alert = self._run(sid)
        self.assertTrue(success)
        self.assertEqual(ret_sid, sid)
        alert.assert_not_called()


class RealAppendAlertSeamTest(unittest.TestCase):
    """Drive the REAL larry_alerts.append_alert (paths patched into a tmp
    tree) through _verify_transcript_persisted and assert the ledger line
    lands.

    Every other run_claude suite mocks append_alert away, and production
    wraps the call in ``except Exception: pass`` — so without this test a
    signature drift in append_alert (renamed kwarg, new required arg) would
    pass every suite while silently killing the #438 critical alert in
    production. The path globals are patched as module attributes because
    larry_alerts freezes them from Path.home() at import; env vars cannot
    redirect them.
    """

    def test_missing_transcript_lands_real_ledger_line(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        alerts_file = root / 'blackboard' / 'larry-alerts.jsonl'

        with mock.patch.object(larry_alerts, 'ALERTS_FILE', alerts_file), \
                mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                                  root / 'state' / 'alert-cooldown'), \
                mock.patch.object(larry_alerts, 'SILENCE_ROOT',
                                  root / 'state' / 'alert-silenced'), \
                mock.patch.object(ar, 'log'):
            result = ar._verify_transcript_persisted(
                'forge', str(root / 'home'), 'tier2',
                '/home/larry/agent-worktrees/wt-forge-demo',
                'sess-gone-0123', task_stem='demo-task',
            )

        self.assertFalse(result)
        lines = alerts_file.read_text().strip().splitlines()
        self.assertEqual(
            len(lines), 1,
            'the real append_alert must land exactly one ledger line — '
            'zero means the call raised and was swallowed by the '
            'except-pass in _verify_transcript_persisted',
        )
        rec = json.loads(lines[0])
        self.assertEqual(rec['source'], 'agent-runner-forge')
        self.assertEqual(rec['severity'], 'critical')
        self.assertEqual(rec['subject'], 'transcript-not-persisted:tier2')
        self.assertEqual(rec['route'], 'escalate')


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # This module drives a Layer B-guarded chokepoint (larry_alerts / inbox /
    # gh-write / claude-spawn / concurrency) against already-isolated state, so
    # the guard would breach before the test's own mocks. Opt out for the module
    # so the guard is a pass-through; the #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    unittest.main()
