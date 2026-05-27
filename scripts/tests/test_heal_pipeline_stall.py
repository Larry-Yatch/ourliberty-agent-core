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

    # ----- Reconciliation cases (2026-05-26 false-fire fix) -----

    def test_skips_when_branch_truncated_prefix_matches(self) -> None:
        """Forge truncates long branch names. PR #116 in production had
        head `forge/chain-discipline-gap-beacon-plan-synthesis-stale-s`
        (49-char suffix) for task_id `...stale-state` (54-char full).
        Exact-equal match misses; truncated-prefix match catches it."""
        full_task = 'chain-discipline-gap-beacon-plan-synthesis-stale-state'
        truncated_branch = (
            'forge/chain-discipline-gap-beacon-plan-synthesis-stale-s'
        )
        lines = [_watcher_forge_done_line(full_task, minutes_ago=180)]
        merged_prs = [{
            'headRefName': truncated_branch,
            'number': 116,
            'title': 'feat(chain-discipline): something',
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(alerts, [])

    def test_skips_when_pr_title_contains_task_id(self) -> None:
        """Title-substring fallback: branch differs entirely but the
        task_id appears verbatim in the PR title (case-insensitive)."""
        task = 'some-rekeyed-task-001'
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        open_prs = [{
            'headRefName': 'forge/unrelated-branch-name',
            'number': 42,
            'title': f'fix(thing): handle Some-Rekeyed-Task-001 edge case',
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }]
        alerts = self.hps.check_forge_built_no_pr(lines, open_prs, [], {})
        self.assertEqual(alerts, [])

    def test_branch_truncation_min_length_floor(self) -> None:
        """A 5-char shared prefix must NOT match — only branch suffixes
        >= _BRANCH_TRUNCATION_MIN_LEN (30) chars long are trusted as
        truncated."""
        lines = [_watcher_forge_done_line(
            'build-something-very-specific-001', minutes_ago=180,
        )]
        # Short prefix `build` would coincidentally match many tasks —
        # the floor blocks it.
        open_prs = [{
            'headRefName': 'forge/build',
            'number': 11, 'title': 'wat',
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }]
        alerts = self.hps.check_forge_built_no_pr(lines, open_prs, [], {})
        self.assertEqual(len(alerts), 1)

    def test_skips_when_preflight_clarify_request_archived(self) -> None:
        """oauth-orchestrator-promotion-plus-auto-restart shape: Forge
        done success=True, but archived outbox carries CLARIFY_REQUEST
        in `result`. No PR was intended."""
        task = 'oauth-orchestrator-promotion-plus-auto-restart'
        archive_dir = self.agents_root / 'outboxes' / 'forge' / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f'{task}.json').write_text(json.dumps({
            'task_id': task,
            'phase': 'preflight',
            'result': (
                '=== CLARIFY_REQUEST ===\n'
                '{"task_id": "' + task + '", "question": "..."}\n'
                '=== END_CLARIFY_REQUEST ==='
            ),
        }))
        # FORGE_OUTBOX_ARCHIVE was set at module import time; the
        # _TempAgentsRootMixin re-imports the module each test so the
        # constant has already been recomputed against the temp root.
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])

    def test_skips_when_preflight_reject_request_archived(self) -> None:
        """Defense-in-depth: REJECT_REQUEST marker also signals
        no-PR-by-design. No instances exist in production today but the
        marker shape is part of the Forge agent's vocabulary."""
        task = 'task-that-forge-explicitly-rejected-001'
        archive_dir = self.agents_root / 'outboxes' / 'forge' / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f'{task}.json').write_text(json.dumps({
            'task_id': task,
            'phase': 'preflight',
            'result': (
                '=== REJECT_REQUEST ===\n'
                '{"reason": "tier violation"}\n'
                '=== END_REJECT_REQUEST ==='
            ),
        }))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])

    def test_fires_when_archive_contains_proceed_marker(self) -> None:
        """A PROCEED marker means Forge committed to building. If there
        is still no PR after FORGE_BUILT_NO_PR_MIN, that IS the stall
        Check 1 is meant to surface."""
        task = 'real-stall-after-proceed-001'
        archive_dir = self.agents_root / 'outboxes' / 'forge' / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f'{task}.json').write_text(json.dumps({
            'task_id': task,
            'phase': 'preflight',
            'result': '=== PROCEED ===\n{"preflight_summary": "..."}\n=== END_PROCEED ===',
        }))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn(task, alerts[0]['message'])

    def test_skips_when_pr_merged_on_other_tracked_repo(self) -> None:
        """A merged PR in the dashboard repo (not agent-core) still
        means Forge's work shipped — both tracked repos must be checked."""
        task = 'cross-repo-task-001'
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        merged_prs = [{
            'headRefName': f'forge/{task}',
            'number': 77,
            'title': 'feat: x',
            '_repo': 'Larry-Yatch/ourliberty-dashboard',
        }]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(alerts, [])

    def test_falls_back_to_alert_when_pr_list_empty_due_to_gh_failure(self) -> None:
        """When `gh pr list` fails, `_all_open_prs` + `_all_merged_prs_recent`
        return []. Check 1 must still alert on real stalls — better a
        false-alert than a false-skip during gh outages. Verified by
        passing empty PR lists (the shape the wrappers produce on failure)
        and no archive entry (so the preflight reconciliation also misses)."""
        task = 'task-during-gh-outage-001'
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        # Empty open + merged PR lists (gh outage shape).
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn(task, alerts[0]['message'])

    def test_pr_matches_task_helper_branch_exact(self) -> None:
        pr = {'headRefName': 'forge/exact-001', 'title': 'x'}
        self.assertEqual(self.hps._pr_matches_task(pr, 'exact-001'), 'branch')

    def test_pr_matches_task_helper_branch_truncated(self) -> None:
        # 30 chars in the suffix → just at the floor.
        long_task = 'a' * 30 + '-extra-suffix-here-001'
        truncated = 'forge/' + 'a' * 30
        pr = {'headRefName': truncated, 'title': 'x'}
        self.assertEqual(
            self.hps._pr_matches_task(pr, long_task),
            'branch_truncated',
        )

    def test_pr_matches_task_helper_title_fallback(self) -> None:
        pr = {
            'headRefName': 'forge/something-else',
            'title': 'fix: handle MY-TASK-001 case',
        }
        self.assertEqual(
            self.hps._pr_matches_task(pr, 'my-task-001'),
            'title',
        )

    def test_pr_matches_task_helper_returns_none(self) -> None:
        pr = {'headRefName': 'forge/unrelated', 'title': 'no match'}
        self.assertIsNone(self.hps._pr_matches_task(pr, 'lonely-task-001'))

    def test_forge_preflight_non_proceed_returns_none_for_missing_archive(self) -> None:
        self.assertIsNone(
            self.hps._forge_preflight_non_proceed('never-existed-task'),
        )

    def test_forge_preflight_non_proceed_returns_none_for_malformed_json(self) -> None:
        task = 'malformed-archive-001'
        archive_dir = self.agents_root / 'outboxes' / 'forge' / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f'{task}.json').write_text('{not valid json')
        self.assertIsNone(self.hps._forge_preflight_non_proceed(task))

    # ----- Clean-preflight-exit fallback (2026-05-26 build-sequence-
    # orchestrator-pr-s1 false-fire) -----

    def test_skips_when_preflight_clean_exit_no_marker_delimiter(self) -> None:
        """build-sequence-orchestrator-pr-s1 shape: phase='preflight',
        exit_code=0, attempts=1, and `result` is prose describing the
        CLARIFY marker without literal `=== CLARIFY_REQUEST ===`
        delimiters. The 2026-05-26 185-min false-fire case."""
        task = 'build-sequence-orchestrator-pr-s1-spec-adoption'
        archive_dir = self.agents_root / 'outboxes' / 'forge' / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f'{task}.json').write_text(json.dumps({
            'task_id': task,
            'phase': 'preflight',
            'exit_code': 0,
            'attempts': 1,
            'result': (
                'Preflight surfaced two scope-blocking ambiguities. '
                'Marker emitted verbatim above. No files modified. '
                'Awaiting Beacon resolution before PROCEED.'
            ),
        }))
        # _forge_preflight_non_proceed returns the generic signal.
        self.assertEqual(
            self.hps._forge_preflight_non_proceed(task),
            'PREFLIGHT_EXIT',
        )
        # Check 1 captures the skip with reason=preflight_exit in the log.
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO': captured.append(msg)):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])
        skip_lines = [m for m in captured if 'FORGE_NO_PR_SKIP' in m]
        self.assertEqual(len(skip_lines), 1)
        self.assertIn('reason=preflight_exit', skip_lines[0])
        self.assertIn(task, skip_lines[0])

    def test_skips_when_preflight_marker_delimiter_present_preserves_label(self) -> None:
        """Same outbox shape with phase=preflight + exit_code=0 + attempts=1,
        but `result` ALSO carries the literal `=== CLARIFY_REQUEST ===`
        delimiter. Marker scan wins; the specific marker label is
        preserved in the skip log (`reason=preflight_non_proceed
        marker='CLARIFY_REQUEST'`) for precise reporting."""
        task = 'precise-clarify-marker-archived-001'
        archive_dir = self.agents_root / 'outboxes' / 'forge' / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f'{task}.json').write_text(json.dumps({
            'task_id': task,
            'phase': 'preflight',
            'exit_code': 0,
            'attempts': 1,
            'result': (
                '=== CLARIFY_REQUEST ===\n'
                '{"task_id": "' + task + '", "question": "..."}\n'
                '=== END_CLARIFY_REQUEST ==='
            ),
        }))
        self.assertEqual(
            self.hps._forge_preflight_non_proceed(task),
            'CLARIFY_REQUEST',
        )
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO': captured.append(msg)):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])
        skip_lines = [m for m in captured if 'FORGE_NO_PR_SKIP' in m]
        self.assertEqual(len(skip_lines), 1)
        self.assertIn('reason=preflight_non_proceed', skip_lines[0])
        self.assertIn("marker='CLARIFY_REQUEST'", skip_lines[0])

    def test_alerts_on_build_phase_crash_no_preflight_skip(self) -> None:
        """Regression guard for legitimate stalls: a real build-phase
        crash (phase='build', exit_code != 0) must not be silenced by
        the new PREFLIGHT_EXIT fallback. The three-conditions guard
        (phase=preflight AND exit_code=0 AND attempts>=1) prevents it."""
        task = 'real-build-phase-crash-001'
        archive_dir = self.agents_root / 'outboxes' / 'forge' / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f'{task}.json').write_text(json.dumps({
            'task_id': task,
            'phase': 'build',
            'exit_code': 1,
            'attempts': 3,
            'result': 'Forge crashed during gh pr create; no output.',
        }))
        # Helper returns None — three-conditions guard fails on phase.
        self.assertIsNone(self.hps._forge_preflight_non_proceed(task))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn(task, alerts[0]['message'])

    def test_alerts_when_no_preflight_archive_at_all(self) -> None:
        """Regression guard: Forge completed build per inbox_watcher.log
        (the `[forge] done success=True` line is present) but neither a
        matching PR nor a preflight archive exists. Check 1 must alert
        — neither reconciliation path applies."""
        task = 'no-archive-no-pr-stall-001'
        # Deliberately do NOT write any archive file.
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn(task, alerts[0]['message'])


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


class TestScanWindow(_TempAgentsRootMixin, unittest.TestCase):
    """SCAN_WINDOW_SECONDS gates every Check's stall-trigger event
    timestamp. Events older than the window are skipped silently —
    historical record, not stalls. Verified per-Check + at the boundary."""

    def test_constant_is_24h(self) -> None:
        """Default is 24h = 86400s. Documented in the module docstring as
        the rationale for retiring resolved incidents."""
        self.assertEqual(self.hps.SCAN_WINDOW_SECONDS, 86400)

    def test_within_helper_inclusive_boundary(self) -> None:
        """Event exactly at `now - SCAN_WINDOW_SECONDS` is in-window
        (inclusive boundary, per spec)."""
        now = datetime.now(timezone.utc)
        at_boundary = now - timedelta(seconds=self.hps.SCAN_WINDOW_SECONDS)
        self.assertTrue(self.hps._within_scan_window(at_boundary, now=now))
        # Just outside is out.
        outside = now - timedelta(seconds=self.hps.SCAN_WINDOW_SECONDS + 1)
        self.assertFalse(self.hps._within_scan_window(outside, now=now))
        # None is out (un-parseable timestamps are not stall triggers).
        self.assertFalse(self.hps._within_scan_window(None, now=now))

    def test_check1_skips_event_older_than_window(self) -> None:
        """Inbox-watcher [forge] done line >24h ago must not alert.
        Mirrors the original 'three false positives from yesterday's
        already-resolved incidents' scenario."""
        # 25h ago — past the 24h SCAN_WINDOW_SECONDS.
        lines = [_watcher_forge_done_line('historical-task-001', minutes_ago=25 * 60)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])

    def test_check1_alerts_event_inside_window(self) -> None:
        """Same shape as the historical event, but 6h ago — well inside
        the window. Existing Check 1 logic still classifies it."""
        lines = [_watcher_forge_done_line('fresh-task-001', minutes_ago=6 * 60)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['key'], 'forge_built_no_pr:fresh-task-001')

    def test_check1_boundary_event_included(self) -> None:
        """Event exactly at `now - SCAN_WINDOW_SECONDS` is included.
        Spec: inclusive boundary."""
        # 24h ago to the second is the boundary — must include.
        # Use 24h minus 1s to avoid clock-drift flakiness on the boundary
        # arithmetic between this test's _watcher_ts and the production
        # _within_scan_window check.
        lines = [_watcher_forge_done_line(
            'boundary-task-001', minutes_ago=24 * 60 - 1,
        )]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['key'], 'forge_built_no_pr:boundary-task-001')

    def test_check3_skips_mirror_pass_older_than_window(self) -> None:
        """Mirror PASS marker line >24h ago. The PR could still be OPEN
        but at that age it's a long-standing situation, not a fresh
        stall — Check 3 must not re-DM."""
        lines = [f'[{_ts(25 * 60)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-historical-pass-001.json)']
        pr = {
            'number': 700, 'headRefName': 'forge/historical-pass-001', 'title': 'fix: x',
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        alerts = self.hps.check_mirror_pass_unmerged(lines, [pr], {})
        self.assertEqual(alerts, [])

    def test_check4_skips_generic_notify_older_than_window(self) -> None:
        """Generic mirror-result depth=1 line >24h ago: historical
        marker-shape drift, presumed-resolved. Skip."""
        lines = [f'[{_ts(25 * 60)}] [notifier] [INFO] notified beacon <- mirror (mirror-result, depth=1, file=notify-historical-drift-001.json)']
        alerts = self.hps.check_mirror_marker_invisible(lines, {})
        self.assertEqual(alerts, [])

    def test_check6_skips_no_session_warn_older_than_window(self) -> None:
        """REVIEW_REVISION no-session WARN line >24h ago: the direct-fix
        DM fired when the WARN was fresh. Re-alerting now is noise."""
        lines = [
            f'[{_ts(25 * 60)}] [notifier] [WARN] REVIEW_REVISION on task '
            f'historical-rev-001 has no forge_build_session_id'
        ]
        alerts = self.hps.check_revision_dispatched_with_no_session(lines, {})
        self.assertEqual(alerts, [])

    def test_check2_skips_pr_older_than_window(self) -> None:
        """PR opened >24h ago without Mirror dispatch: long-standing
        situation, not a fresh stall."""
        pr = {
            'number': 720, 'headRefName': 'forge/old-pr-001', 'title': 'docs: x',
            'createdAt': (
                datetime.now(timezone.utc) - timedelta(hours=25)
            ).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        alerts = self.hps.check_pr_no_mirror_dispatch([], [pr], {})
        self.assertEqual(alerts, [])

    def test_check7_skips_pr_older_than_window(self) -> None:
        """OPEN un-routed PR > 24h old: same logic. Long-standing, not
        a fresh stall trigger."""
        pr = {
            'number': 730, 'headRefName': 'larry/old-unrouted-001', 'title': 'feat: x',
            'createdAt': (
                datetime.now(timezone.utc) - timedelta(hours=25)
            ).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        alerts = self.hps.check_unrouted_open_prs([pr], [], {})
        self.assertEqual(alerts, [])


if __name__ == '__main__':
    unittest.main()
