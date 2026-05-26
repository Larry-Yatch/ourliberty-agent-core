"""Tests for scripts/heal_pipeline_stall.py.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Coverage:
- Each of the five checks against fixture log lines + mock gh output
- Regex patterns against real-shape log lines from outbox_notifier
- Kill switch
- Dedup state file persistence
- Branch-to-task extraction
- End-to-end run smoke (no stalls + clear stall)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _ts(minutes_ago: int) -> str:
    """Produce a log-line timestamp `minutes_ago` minutes before now."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


class _TempAgentsRootMixin:
    """Sets OURLIBERTY_AGENTS_ROOT to a temp dir + re-imports the module so
    its module-level constants pick up the env var."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.agents_root = Path(self._tmpdir.name) / 'agents'
        self.agents_root.mkdir()
        (self.agents_root / 'logs').mkdir()
        (self.agents_root / 'blackboard').mkdir()
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.agents_root)
        # Force re-import.
        if 'heal_pipeline_stall' in sys.modules:
            del sys.modules['heal_pipeline_stall']
        import heal_pipeline_stall  # noqa: E402
        self.hps = heal_pipeline_stall

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)


class TestRegexPatterns(_TempAgentsRootMixin, unittest.TestCase):
    """Verify regex patterns match real outbox_notifier log shapes."""

    def test_forge_done_regex_matches_real_shape(self) -> None:
        line = '[2026-05-25 13:08:39] [notifier] [INFO] [forge] done task=chain-discipline-v2-marker-shape-and-stale-daemon-001 success=True'
        m = self.hps._FORGE_DONE_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group('task'), 'chain-discipline-v2-marker-shape-and-stale-daemon-001')

    def test_marker_notified_mirror_regex_matches_pass(self) -> None:
        line = '[2026-05-25 13:16:54] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-chain-discipline-marker-parser-and-regression-check-001.json)'
        m = self.hps._MARKER_NOTIFIED_MIRROR_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group('intent'), 'review-pass')
        self.assertEqual(m.group('task'), 'chain-discipline-marker-parser-and-regression-check-001')

    def test_notified_mirror_generic_regex_matches_depth1(self) -> None:
        line = '[2026-05-25 19:08:53] [notifier] [INFO] notified beacon <- mirror (mirror-result, depth=1, file=notify-review-pr-104-e4-4d-system-tab-spec.json)'
        m = self.hps._NOTIFIED_MIRROR_GENERIC_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group('task'), 'review-pr-104-e4-4d-system-tab-spec')

    def test_review_request_dispatched_regex(self) -> None:
        line = '[2026-05-25 13:08:44] [notifier] [INFO] review-request dispatched mirror <- beacon (task=chain-discipline-marker-parser-and-regression-check-001, file=review-X.json, pr=https://github.com/Larry-Yatch/ourliberty-agent-core/pull/103)'
        m = self.hps._REVIEW_REQUEST_DISPATCHED_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group('task'), 'chain-discipline-marker-parser-and-regression-check-001')

    def test_auto_merge_regex_matches_outcome_merged(self) -> None:
        line = '[2026-05-25 13:16:57] [notifier] [INFO] AUTO_MERGE task=chain-discipline-marker-parser-and-regression-check-001 pr=https://github.com/Larry-Yatch/ourliberty-agent-core/pull/103 outcome=merged (--squash --delete-branch)'
        m = self.hps._AUTO_MERGE_MERGED_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group('task'), 'chain-discipline-marker-parser-and-regression-check-001')


class TestKillSwitch(_TempAgentsRootMixin, unittest.TestCase):

    def test_kill_switch_exits_immediately(self) -> None:
        (self.agents_root / 'healers.disabled').touch()
        with patch.object(self.hps.larry_alerts, 'append_alert') as mock_alert:
            self.hps.run()
        mock_alert.assert_not_called()


class TestHeartbeat(_TempAgentsRootMixin, unittest.TestCase):

    def test_heartbeat_writes_on_run(self) -> None:
        with patch.object(self.hps, '_all_open_prs', return_value=[]), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=[]), \
             patch('subprocess.run') as mock_sub:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            self.hps.run()
        self.assertTrue((self.agents_root / 'blackboard' / 'heal-pipeline-stall.heartbeat').exists())


class TestCheckForgeBuiltNoPr(_TempAgentsRootMixin, unittest.TestCase):

    def test_fires_when_no_matching_pr(self) -> None:
        lines = [f'[{_ts(180)}] [notifier] [INFO] [forge] done task=test-stalled-task-001 success=True']
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('test-stalled-task-001', alerts[0]['message'])
        self.assertEqual(alerts[0]['key'], 'forge_built_no_pr:test-stalled-task-001')

    def test_skips_when_open_pr_exists(self) -> None:
        lines = [f'[{_ts(180)}] [notifier] [INFO] [forge] done task=test-task-002 success=True']
        open_prs = [{'headRefName': 'forge/test-task-002', 'number': 99, '_repo': 'x/y'}]
        alerts = self.hps.check_forge_built_no_pr(lines, open_prs, [], {})
        self.assertEqual(alerts, [])

    def test_respects_threshold(self) -> None:
        # 60 min < FORGE_BUILT_NO_PR_MIN (120)
        lines = [f'[{_ts(60)}] [notifier] [INFO] [forge] done task=recent-task-001 success=True']
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])

    def test_accepts_merged_pr(self) -> None:
        """A merged PR for the task means Forge's work shipped — not a stall."""
        lines = [f'[{_ts(180)}] [notifier] [INFO] [forge] done task=merged-task-001 success=True']
        merged_prs = [{'headRefName': 'forge/merged-task-001', 'number': 50, '_repo': 'x/y'}]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(alerts, [])


class TestCheckPrNoMirrorDispatch(_TempAgentsRootMixin, unittest.TestCase):

    def test_fires_for_doc_after_30min(self) -> None:
        pr = {
            'number': 200, 'headRefName': 'forge/doc-task-001', 'title': 'docs(e4): something',
            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        alerts = self.hps.check_pr_no_mirror_dispatch([], [pr], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('PR #200', alerts[0]['message'])

    def test_waits_longer_for_code(self) -> None:
        pr = {
            'number': 201, 'headRefName': 'forge/code-task-001', 'title': 'fix(chain): X',
            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        # 45 min < 60 min code threshold
        alerts = self.hps.check_pr_no_mirror_dispatch([], [pr], {})
        self.assertEqual(alerts, [])

    def test_skips_when_dispatched(self) -> None:
        pr = {
            'number': 202, 'headRefName': 'forge/dispatched-task-001', 'title': 'docs: x',
            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        lines = [f'[{_ts(40)}] [notifier] [INFO] review-request dispatched mirror <- beacon (task=dispatched-task-001, file=review-x.json, pr=https://example/1)']
        alerts = self.hps.check_pr_no_mirror_dispatch(lines, [pr], {})
        self.assertEqual(alerts, [])

    def test_ignores_non_forge_branches(self) -> None:
        """A `larry/` branch is human-authored — chain doesn't auto-review."""
        pr = {
            'number': 203, 'headRefName': 'larry/manual-doc', 'title': 'docs: x',
            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        alerts = self.hps.check_pr_no_mirror_dispatch([], [pr], {})
        self.assertEqual(alerts, [])


class TestCheckMirrorPassUnmerged(_TempAgentsRootMixin, unittest.TestCase):

    def test_fires_when_pr_still_open(self) -> None:
        lines = [f'[{_ts(45)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-pass-stuck-001.json)']
        pr = {
            'number': 300, 'headRefName': 'forge/pass-stuck-001', 'title': 'fix: x',
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        alerts = self.hps.check_mirror_pass_unmerged(lines, [pr], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('PR #300', alerts[0]['message'])
        self.assertIn('AUTO_MERGE never fired', alerts[0]['message'])

    def test_respects_threshold(self) -> None:
        lines = [f'[{_ts(15)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-fresh-pass-001.json)']
        pr = {
            'number': 301, 'headRefName': 'forge/fresh-pass-001', 'title': 'fix: x',
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        alerts = self.hps.check_mirror_pass_unmerged(lines, [pr], {})
        self.assertEqual(alerts, [])

    def test_ignores_revision_intent(self) -> None:
        """A REVIEW_REVISION marker shouldn't trigger this check."""
        lines = [f'[{_ts(45)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-revision, file=notify-rev-001.json)']
        pr = {
            'number': 302, 'headRefName': 'forge/rev-001', 'title': 'fix: x',
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        alerts = self.hps.check_mirror_pass_unmerged(lines, [pr], {})
        self.assertEqual(alerts, [])


class TestCheckMirrorMarkerInvisible(_TempAgentsRootMixin, unittest.TestCase):

    def test_fires_when_no_classify(self) -> None:
        lines = [f'[{_ts(45)}] [notifier] [INFO] notified beacon <- mirror (mirror-result, depth=1, file=notify-shape-drift-001.json)']
        alerts = self.hps.check_mirror_marker_invisible(lines, {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('shape-drift-001', alerts[0]['message'])

    def test_skips_when_classified(self) -> None:
        lines = [
            f'[{_ts(45)}] [notifier] [INFO] notified beacon <- mirror (mirror-result, depth=1, file=notify-good-001.json)',
            f'[{_ts(44)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-good-001.json)',
        ]
        alerts = self.hps.check_mirror_marker_invisible(lines, {})
        self.assertEqual(alerts, [])

    def test_skips_when_auto_merged(self) -> None:
        """If a PR merged after the generic notify, the marker was eventually
        classified via a follow-up. Don't false-alert."""
        lines = [
            f'[{_ts(60)}] [notifier] [INFO] notified beacon <- mirror (mirror-result, depth=1, file=notify-merged-001.json)',
            f'[{_ts(30)}] [notifier] [INFO] AUTO_MERGE task=merged-001 pr=https://example/x outcome=merged (--squash --delete-branch)',
        ]
        alerts = self.hps.check_mirror_marker_invisible(lines, {})
        self.assertEqual(alerts, [])


class TestCheckRetryExhausted(_TempAgentsRootMixin, unittest.TestCase):

    def test_parses_journal_line(self) -> None:
        fake_journal = 'May 26 08:01:00 host inbox-watcher: All retries exhausted for task=dead-task-001\n'
        with patch('subprocess.run') as mock_sub:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = fake_journal
            mock_sub.return_value.stderr = ''
            alerts = self.hps.check_retry_exhausted({})
        self.assertEqual(len(alerts), 1)
        self.assertIn('dead-task-001', alerts[0]['message'])

    def test_silent_when_no_match(self) -> None:
        with patch('subprocess.run') as mock_sub:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = 'May 26 08:01:00 host inbox-watcher: normal log line\n'
            mock_sub.return_value.stderr = ''
            alerts = self.hps.check_retry_exhausted({})
        self.assertEqual(alerts, [])


class TestDedup(_TempAgentsRootMixin, unittest.TestCase):

    def test_suppresses_repeat_within_cooldown(self) -> None:
        state: dict = {}
        key = 'test:dedup-key'
        self.assertTrue(self.hps.should_alert(state, key))
        self.hps.record_alert(state, key)
        self.assertFalse(self.hps.should_alert(state, key))

    def test_allows_after_cooldown(self) -> None:
        state = {'test:dedup-key': (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
        self.assertTrue(self.hps.should_alert(state, 'test:dedup-key'))

    def test_state_persists_to_disk(self) -> None:
        state = {'key1': '2026-05-26T10:00:00+00:00'}
        self.hps.save_state(state)
        loaded = self.hps.load_state()
        self.assertEqual(loaded, state)


class TestBranchToTask(_TempAgentsRootMixin, unittest.TestCase):

    def test_extracts_forge_prefix(self) -> None:
        self.assertEqual(self.hps._task_id_from_branch('forge/my-task-001'), 'my-task-001')

    def test_extracts_larry_prefix(self) -> None:
        self.assertEqual(self.hps._task_id_from_branch('larry/eod-docs-rollup'), 'eod-docs-rollup')

    def test_returns_none_for_unrecognized(self) -> None:
        self.assertIsNone(self.hps._task_id_from_branch('main'))
        self.assertIsNone(self.hps._task_id_from_branch('feature/x'))


class TestEndToEnd(_TempAgentsRootMixin, unittest.TestCase):

    def test_no_stalls_silent(self) -> None:
        """When everything is healthy, no alerts fire."""
        with patch.object(self.hps, '_all_open_prs', return_value=[]), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=[]), \
             patch('subprocess.run') as mock_sub, \
             patch.object(self.hps.larry_alerts, 'append_alert') as mock_alert:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            self.hps.run()
        mock_alert.assert_not_called()

    def test_fires_alert_on_real_stall(self) -> None:
        """A clear Mirror-PASS-unmerged stall produces a DM for that stall.

        The PR is old enough that Check 2 (PR no Mirror dispatch) would also
        fire — we provide a `review-request dispatched` log line so Check 2
        is satisfied and only Check 3 (Mirror PASS unmerged) trips. This
        isolates the end-to-end happy-path for one specific stall."""
        pr = {
            'number': 999, 'headRefName': 'forge/end2end-stuck-001', 'title': 'fix: x',
            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        lines = [
            # Satisfy Check 2: Mirror review WAS dispatched.
            f'[{_ts(90)}] [notifier] [INFO] review-request dispatched mirror <- beacon (task=end2end-stuck-001, file=review-x.json, pr=https://example/999)',
            # Trigger Check 3: Mirror PASSed >30 min ago but PR is still OPEN.
            f'[{_ts(60)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-end2end-stuck-001.json)',
        ]
        with patch.object(self.hps, '_all_open_prs', return_value=[pr]), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=lines), \
             patch('subprocess.run') as mock_sub, \
             patch.object(self.hps.larry_alerts, 'append_alert', return_value=True) as mock_alert:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            self.hps.run()
        self.assertEqual(mock_alert.call_count, 1)
        call = mock_alert.call_args
        self.assertEqual(call.kwargs['source'], 'heal-pipeline-stall')
        self.assertEqual(call.kwargs['severity'], 'warning')
        self.assertIn('PR #999', call.kwargs['message'])
        self.assertIn('AUTO_MERGE never fired', call.kwargs['message'])

    def test_multi_stall_fires_one_dm_per_unique_stall(self) -> None:
        """When multiple distinct stalls exist on different tasks, each fires
        its own DM (not collapsed). Verifies the dedup-by-key contract."""
        prs = [
            {'number': 800, 'headRefName': 'forge/stall-a-001', 'title': 'fix: a',
             'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat(),
             '_repo': 'Larry-Yatch/ourliberty-agent-core'},
            {'number': 801, 'headRefName': 'forge/stall-b-001', 'title': 'fix: b',
             'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat(),
             '_repo': 'Larry-Yatch/ourliberty-agent-core'},
        ]
        lines = [
            f'[{_ts(90)}] [notifier] [INFO] review-request dispatched mirror <- beacon (task=stall-a-001, file=r.json, pr=https://example/800)',
            f'[{_ts(60)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-stall-a-001.json)',
            f'[{_ts(90)}] [notifier] [INFO] review-request dispatched mirror <- beacon (task=stall-b-001, file=r.json, pr=https://example/801)',
            f'[{_ts(45)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-stall-b-001.json)',
        ]
        with patch.object(self.hps, '_all_open_prs', return_value=prs), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=lines), \
             patch('subprocess.run') as mock_sub, \
             patch.object(self.hps.larry_alerts, 'append_alert', return_value=True) as mock_alert:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            self.hps.run()
        self.assertEqual(mock_alert.call_count, 2)


if __name__ == '__main__':
    unittest.main()
