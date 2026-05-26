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
    """Produce an outbox-notifier-shape log-line timestamp `minutes_ago` minutes
    before now. Format: `2026-05-26 13:08:39` (human-readable). Used inside the
    `[<ts>] [notifier] [INFO] ...` line shape that outbox_notifier.py emits."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _watcher_ts(minutes_ago: int) -> str:
    """Produce an inbox_watcher-shape log-line timestamp `minutes_ago` minutes
    before now. Format: ISO-8601 with microseconds + UTC offset
    (e.g. `2026-05-26T04:46:20.823929+00:00`). Used inside the
    `[<ts>] inbox_watcher: [forge] done task=X success=True duration=...`
    line shape that inbox_watcher.py emits."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f+00:00')


def _watcher_forge_done_line(task: str, minutes_ago: int,
                             duration_sec: float = 125.0, cost_usd: float = 1.08) -> str:
    """Helper to construct a verbatim-shape inbox_watcher.log [forge] done line.
    Production sample (2026-05-26 inbox_watcher.log):
        [2026-05-26T04:46:20.823929+00:00] inbox_watcher: [forge] done task=chain-discipline-v2-marker-shape-and-stale-daemon-001 success=True duration=125.02s attempts=1 cost=$1.08064725
    """
    return (f'[{_watcher_ts(minutes_ago)}] inbox_watcher: [forge] done '
            f'task={task} success=True duration={duration_sec:.2f}s '
            f'attempts=1 cost=${cost_usd:.8f}')


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

    def test_forge_done_regex_matches_real_inbox_watcher_log_shape(self) -> None:
        """Verbatim production line from /home/larry/agents/logs/inbox_watcher.log
        (sampled 2026-05-26 morning). The `[forge] done task=X success=True` shape
        is emitted by inbox_watcher.py to its OWN log, NOT by outbox_notifier.py
        to outbox-notifier.log. Mirror PR #107 review caught the original
        regex-on-wrong-file bug; this fixture is verbatim production so a future
        log-format change is detected by test failure."""
        line = '[2026-05-26T04:46:20.823929+00:00] inbox_watcher: [forge] done task=chain-discipline-v2-marker-shape-and-stale-daemon-001 success=True duration=125.02s attempts=1 cost=$1.08064725'
        m = self.hps._FORGE_DONE_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group('task'), 'chain-discipline-v2-marker-shape-and-stale-daemon-001')

    def test_forge_done_regex_does_not_match_outbox_notifier_shape(self) -> None:
        """The notifier doesn't emit [forge] done lines (verified empirically
        via grep across 1814 lines of production outbox-notifier.log: zero
        matches). Defensive test: ensure we don't accidentally re-introduce
        a regex that matches an outbox-notifier-formatted line, which would
        indicate a regression to the original bug shape."""
        notifier_only_line = '[2026-05-26 06:54:41] [notifier] [INFO] review-request dispatched mirror <- beacon (task=build-X)'
        m = self.hps._FORGE_DONE_RE.search(notifier_only_line)
        self.assertIsNone(m)

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
        lines = [_watcher_forge_done_line('test-stalled-task-001', minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('test-stalled-task-001', alerts[0]['message'])
        self.assertEqual(alerts[0]['key'], 'forge_built_no_pr:test-stalled-task-001')

    def test_skips_when_open_pr_exists(self) -> None:
        lines = [_watcher_forge_done_line('test-task-002', minutes_ago=180)]
        open_prs = [{'headRefName': 'forge/test-task-002', 'number': 99, '_repo': 'x/y'}]
        alerts = self.hps.check_forge_built_no_pr(lines, open_prs, [], {})
        self.assertEqual(alerts, [])

    def test_respects_threshold(self) -> None:
        # 60 min < FORGE_BUILT_NO_PR_MIN (120)
        lines = [_watcher_forge_done_line('recent-task-001', minutes_ago=60)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])

    def test_accepts_merged_pr(self) -> None:
        """A merged PR for the task means Forge's work shipped — not a stall."""
        lines = [_watcher_forge_done_line('merged-task-001', minutes_ago=180)]
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
        # Satisfy Check 7 too: provide a routing event so the PR isn't
        # flagged as unrouted (chain discipline v3 GAP 3 added Check 7).
        routing_events = [{
            'source_agent': 'beacon',
            'target_agent_final': 'mirror',
            'phase': 'review',
            'task_id': 'end2end-stuck-001',
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }]
        with patch.object(self.hps, '_all_open_prs', return_value=[pr]), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=lines), \
             patch.object(self.hps, '_read_recent_routing_events', return_value=routing_events), \
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
        # Satisfy Check 7 too: routing events for both PRs.
        routing_events = [
            {
                'source_agent': 'beacon',
                'target_agent_final': 'mirror',
                'phase': 'review',
                'task_id': 'stall-a-001',
                'timestamp': datetime.now(timezone.utc).isoformat(),
            },
            {
                'source_agent': 'beacon',
                'target_agent_final': 'mirror',
                'phase': 'review',
                'task_id': 'stall-b-001',
                'timestamp': datetime.now(timezone.utc).isoformat(),
            },
        ]
        with patch.object(self.hps, '_all_open_prs', return_value=prs), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=lines), \
             patch.object(self.hps, '_read_recent_routing_events', return_value=routing_events), \
             patch('subprocess.run') as mock_sub, \
             patch.object(self.hps.larry_alerts, 'append_alert', return_value=True) as mock_alert:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            self.hps.run()
        self.assertEqual(mock_alert.call_count, 2)

    def test_check1_reads_from_inbox_watcher_log_not_outbox_notifier(self) -> None:
        """Defensive test: Check 1's `[forge] done` line must come from
        inbox_watcher.log (NOT outbox-notifier.log). The patched
        `_read_recent_log_lines` returns DIFFERENT lines depending on which
        file is passed — forge-done shape ONLY in the inbox_watcher.log
        slot. Regression check on the bug Mirror caught in PR #107 review."""

        def side_effect(log_path, hours):
            # log_path is a Path object; compare by name.
            if 'inbox_watcher.log' in str(log_path):
                return [_watcher_forge_done_line('e2e-watcher-source-001', minutes_ago=180)]
            # outbox-notifier.log gets nothing — production reality.
            return []

        with patch.object(self.hps, '_all_open_prs', return_value=[]), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', side_effect=side_effect), \
             patch('subprocess.run') as mock_sub, \
             patch.object(self.hps.larry_alerts, 'append_alert', return_value=True) as mock_alert:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            self.hps.run()
        # Exactly one alert — Check 1's forge_built_no_pr fires from the
        # inbox_watcher.log source.
        self.assertEqual(mock_alert.call_count, 1)
        call = mock_alert.call_args
        self.assertIn('forge-no-pr', call.kwargs['subject'])
        self.assertIn('e2e-watcher-source-001', call.kwargs['subject'])


class TestCheckRevisionDispatchedWithNoSession(_TempAgentsRootMixin, unittest.TestCase):
    """Chain discipline v3 GAP 1 (2026-05-26). Healer-level defense:
    re-flag any `no forge_build_session_id` WARN line not already
    suppressed by the direct-fix alert's cooldown."""

    def test_fires_when_warn_line_present(self):
        line = (
            f'[{_ts(45)}] [notifier] [WARN] REVIEW_REVISION on task '
            f'feedback-claude-as-forge-001 has no forge_build_session_id '
            f"(routing_source='beacon', chat_id=None); revision dispatch "
            f'would have no session to --resume — skipping.'
        )
        alerts = self.hps.check_revision_dispatched_with_no_session([line], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('feedback-claude-as-forge-001', alerts[0]['message'])
        self.assertIn(
            'pipeline-stall:no-session-revision:feedback-claude-as-forge-001',
            alerts[0]['subject'],
        )

    def test_dedup_per_task(self):
        # Two WARN lines for the same task → one alert.
        lines = [
            f'[{_ts(45)}] [notifier] [WARN] REVIEW_REVISION on task '
            f'dup-task has no forge_build_session_id',
            f'[{_ts(40)}] [notifier] [WARN] REVIEW_REVISION on task '
            f'dup-task has no forge_build_session_id',
        ]
        alerts = self.hps.check_revision_dispatched_with_no_session(lines, {})
        self.assertEqual(len(alerts), 1)

    def test_silent_when_no_warn_lines(self):
        lines = [
            f'[{_ts(45)}] [notifier] [INFO] business as usual',
        ]
        alerts = self.hps.check_revision_dispatched_with_no_session(lines, {})
        self.assertEqual(alerts, [])

    def test_silent_against_pre_v3_warn_shape_is_still_caught(self):
        """The pre-v3 WARN line had a different trailing diagnostic
        ('propagation gap?'); the prefix is identical so the regex
        catches both shapes."""
        line = (
            f'[{_ts(45)}] [notifier] [WARN] REVIEW_REVISION on task '
            f'legacy-task has no forge_build_session_id (propagation gap?)'
        )
        alerts = self.hps.check_revision_dispatched_with_no_session([line], {})
        self.assertEqual(len(alerts), 1)


class TestCheckUnroutedOpenPrs(_TempAgentsRootMixin, unittest.TestCase):
    """Chain discipline v3 GAP 3 (2026-05-26). Externally-authored PRs
    that skip the notifier's review-request auto-dispatch must DM Larry
    with the manual dispatch text."""

    def _make_pr(self, number, branch, age_min, repo='Larry-Yatch/ourliberty-agent-core'):
        return {
            'number': number,
            'headRefName': branch,
            'title': 'feat: x',
            'createdAt': (
                datetime.now(timezone.utc) - timedelta(minutes=age_min)
            ).isoformat(),
            '_repo': repo,
        }

    def test_fires_for_pr_with_no_routing_event(self):
        prs = [self._make_pr(500, 'larry/manual-pr-001', age_min=180)]
        alerts = self.hps.check_unrouted_open_prs(prs, [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('PR #500', alerts[0]['message'])
        self.assertIn('routing-events.jsonl', alerts[0]['message'])

    def test_silent_when_routing_event_matches_task_id(self):
        prs = [self._make_pr(501, 'forge/routed-task-001', age_min=180)]
        events = [{
            'source_agent': 'beacon',
            'target_agent_final': 'mirror',
            'phase': 'review',
            'task_id': 'routed-task-001',
            'timestamp': '2026-05-26T10:00:00+00:00',
        }]
        alerts = self.hps.check_unrouted_open_prs(prs, events, {})
        self.assertEqual(alerts, [])

    def test_respects_min_age_threshold(self):
        # PR < 1h old: don't race with in-flight auto-dispatch.
        prs = [self._make_pr(502, 'larry/fresh-pr-001', age_min=30)]
        alerts = self.hps.check_unrouted_open_prs(prs, [], {})
        self.assertEqual(alerts, [])

    def test_silent_when_routing_event_matches_branch_substring(self):
        """Defensive matching: task_id appearing in the branch name (not
        the task suffix) still counts as routed."""
        prs = [self._make_pr(
            503,
            'feat/something-routed-task-002-extra',
            age_min=180,
        )]
        events = [{
            'source_agent': 'beacon',
            'target_agent_final': 'mirror',
            'phase': 'review',
            'task_id': 'routed-task-002',
            'timestamp': '2026-05-26T10:00:00+00:00',
        }]
        alerts = self.hps.check_unrouted_open_prs(prs, events, {})
        self.assertEqual(alerts, [])

    def test_non_review_routing_events_do_not_match(self):
        """A `marker-error` or `notify` routing event for the same task
        is not a review dispatch — don't suppress."""
        prs = [self._make_pr(504, 'forge/notify-only-001', age_min=180)]
        events = [{
            'source_agent': 'outbox-notifier',
            'target_agent_final': 'mirror',
            'phase': 'review',
            'task_id': 'notify-only-001',
            'intent': 'marker-error',
            'timestamp': '2026-05-26T10:00:00+00:00',
        }]
        alerts = self.hps.check_unrouted_open_prs(prs, events, {})
        # `source_agent='outbox-notifier'` is NOT 'beacon' → no match.
        self.assertEqual(len(alerts), 1)


class TestReadRecentRoutingEvents(_TempAgentsRootMixin, unittest.TestCase):
    """Chain discipline v3 GAP 3 — the routing-events.jsonl reader."""

    def test_returns_only_recent_records(self):
        path = self.agents_root / 'logs' / 'routing-events.jsonl'
        recent_ts = datetime.now(timezone.utc).isoformat()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        path.write_text(
            json.dumps({'task_id': 'recent', 'timestamp': recent_ts}) + '\n'
            + json.dumps({'task_id': 'old', 'timestamp': old_ts}) + '\n'
        )
        # Re-import after env change (TempAgentsRootMixin handles this).
        self.hps.ROUTING_EVENTS_LOG = path
        events = self.hps._read_recent_routing_events(hours=24 * 7)
        task_ids = {e.get('task_id') for e in events}
        self.assertIn('recent', task_ids)
        self.assertNotIn('old', task_ids)

    def test_tolerates_malformed_lines(self):
        path = self.agents_root / 'logs' / 'routing-events.jsonl'
        ts = datetime.now(timezone.utc).isoformat()
        path.write_text(
            '{"bad json missing comma"\n'
            + json.dumps({'task_id': 'good', 'timestamp': ts}) + '\n'
            + '\n'  # blank line
        )
        self.hps.ROUTING_EVENTS_LOG = path
        events = self.hps._read_recent_routing_events(hours=24)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['task_id'], 'good')

    def test_missing_file_returns_empty(self):
        # No file written → empty list, not exception.
        self.hps.ROUTING_EVENTS_LOG = self.agents_root / 'logs' / 'does-not-exist.jsonl'
        events = self.hps._read_recent_routing_events(hours=24)
        self.assertEqual(events, [])


if __name__ == '__main__':
    unittest.main()
