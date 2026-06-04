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
    its module-level constants pick up the env var.

    Also unsets SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY for the duration
    of each test so the shared `_resolution_signal_present` helper takes
    its failsafe path (returns (False, None)) instead of hitting the real
    Supabase instance from the test process. Tests that want to exercise
    the helper itself set the env vars + mock `_get_chain_events_for_task`
    explicitly via `_PatchSupabaseMixin` below."""

    _SUPABASE_ENV_KEYS = ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY')

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.agents_root = Path(self._tmpdir.name) / 'agents'
        self.agents_root.mkdir()
        (self.agents_root / 'logs').mkdir()
        (self.agents_root / 'blackboard').mkdir()
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.agents_root)
        # Snapshot + clear Supabase env so tests don't make network calls.
        self._supabase_env_snapshot = {
            k: os.environ.pop(k, None) for k in self._SUPABASE_ENV_KEYS
        }
        # Force re-import.
        if 'heal_pipeline_stall' in sys.modules:
            del sys.modules['heal_pipeline_stall']
        import heal_pipeline_stall  # noqa: E402
        self.hps = heal_pipeline_stall

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        # Restore Supabase env (only re-set if previously present).
        for k, v in self._supabase_env_snapshot.items():
            if v is not None:
                os.environ[k] = v


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

    def test_skips_when_preflight_family_shipped_as_build_pr(self) -> None:
        """Preflight/clarify tasks don't open a PR themselves — they emit a
        PROCEED that spawns a separate build task with a fresh timestamp.
        Production 2026-06-04: preflight task
        `forge-queue-api-preflight-20260603T231401Z-clarify1` shipped as
        merged PR #294 on branch `forge/build-forge-queue-api-20260603T234656Z`.
        Branch/title match miss (different task_id); family match catches it.
        This was the 4-escalation false-fire (02:07/03:11/04:17/05:18Z)."""
        task = 'forge-queue-api-preflight-20260603T231401Z-clarify1'
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        merged_prs = [{
            'headRefName': 'forge/build-forge-queue-api-20260603T234656Z',
            'number': 294,
            'title': 'feat(dashboard): add read-only GET /api/system/agent-queue lifecycle endpoint',
            '_repo': 'Larry-Yatch/ourliberty-dashboard',
        }]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(alerts, [])

    def test_preflight_family_does_not_match_unrelated_build_pr(self) -> None:
        """Family reconciliation must not silence a genuine preflight stall:
        a build PR for a *different* family must leave the alert intact."""
        task = 'forge-queue-api-preflight-20260603T231401Z-clarify1'
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        merged_prs = [{
            'headRefName': 'forge/build-done-today-fix-20260604T045743Z',
            'number': 303, 'title': 'Fix done_today lane',
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(len(alerts), 1)

    def test_preflight_family_floor_blocks_short_stem(self) -> None:
        """A short family stem (< _PREFLIGHT_FAMILY_MIN_LEN) must not match
        a coincidental build branch."""
        self.assertIsNone(self.hps._preflight_family_shipped(
            'ab-preflight-x', [{'headRefName': 'forge/build-ab-x'}],
        ))

    def test_preflight_family_ignores_non_preflight_tasks(self) -> None:
        """Non-preflight task_ids fall through unchanged (None)."""
        self.assertIsNone(self.hps._preflight_family_shipped(
            'build-done-today-fix-20260604T045743Z',
            [{'headRefName': 'forge/build-done-today-fix-20260604T045743Z'}],
        ))

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

    def test_resolves_task_id_from_adjacent_done_line_and_suppresses(self) -> None:
        # The exhaustion line carries no inline `task=`; the real id lives
        # on adjacent structured lines in the same journal batch. The id
        # must be extracted from those, and a resolved/superseded real id
        # must then suppress via the resolution-signal path (no alert).
        fake_journal = (
            'Jun 03 09:00:00 host inbox_watcher: [forge] start task=phase-c-promo-002\n'
            'Jun 03 09:00:01 host inbox_watcher: [forge] [ERROR] All retries exhausted\n'
            'Jun 03 09:00:02 host inbox_watcher: [forge] done task=phase-c-promo-002 success=False\n'
        )
        seen: dict = {}

        def fake_resolution(task_id, since_ts, **kw):
            seen['task_id'] = task_id
            return (True, 'superseded_session')

        with patch('subprocess.run') as mock_sub, \
             patch.object(self.hps, '_resolution_signal_present',
                          side_effect=fake_resolution):
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = fake_journal
            mock_sub.return_value.stderr = ''
            alerts = self.hps.check_retry_exhausted({})
        self.assertEqual(seen.get('task_id'), 'phase-c-promo-002')
        self.assertEqual(alerts, [])

    def test_resolves_task_id_from_worktree_path_when_no_done_line(self) -> None:
        # Fallback: no done/start line, but a preceding traceback line
        # carries the worktree path `wt-forge-<id>`.
        fake_journal = (
            'Jun 03 09:00:00 host inbox_watcher: Exception in '
            '/home/larry/agent-worktrees/wt-forge-stuck-task-007/run.py\n'
            'Jun 03 09:00:01 host inbox_watcher: [forge] [ERROR] All retries exhausted\n'
        )
        seen: dict = {}

        def fake_resolution(task_id, since_ts, **kw):
            seen['task_id'] = task_id
            return (False, None)

        with patch('subprocess.run') as mock_sub, \
             patch.object(self.hps, '_resolution_signal_present',
                          side_effect=fake_resolution):
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = fake_journal
            mock_sub.return_value.stderr = ''
            alerts = self.hps.check_retry_exhausted({})
        self.assertEqual(seen.get('task_id'), 'stuck-task-007')
        self.assertEqual(len(alerts), 1)
        self.assertIn('stuck-task-007', alerts[0]['subject'])

    def test_unidentifiable_batch_emits_no_unknown_alert(self) -> None:
        # A truly unidentifiable exhaustion is un-actionable and
        # un-resolvable: it must be suppressed, never emitted as a literal
        # `unknown` subject, and the resolution-signal path is not reached.
        fake_journal = (
            'Jun 03 09:00:00 host inbox_watcher: [forge] unrelated chatter\n'
            'Jun 03 09:00:01 host inbox_watcher: [forge] [ERROR] All retries exhausted\n'
            'Jun 03 09:00:02 host inbox_watcher: [forge] more chatter no id\n'
        )
        captured: list[str] = []
        with patch('subprocess.run') as mock_sub, \
             patch.object(self.hps, '_resolution_signal_present') as mock_res, \
             patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO': captured.append(msg)):
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = fake_journal
            mock_sub.return_value.stderr = ''
            alerts = self.hps.check_retry_exhausted({})
        self.assertEqual(alerts, [])
        mock_res.assert_not_called()
        self.assertFalse(any('unknown' in a.get('subject', '') for a in alerts))
        skip = [m for m in captured
                if 'RETRY_EXHAUSTED_SKIP' in m and 'no_task_id' in m]
        self.assertEqual(len(skip), 1)


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


# ---------- Shared resolution-signal reconciliation (2026-05-27) ----------
#
# Family A — helper unit tests
# Family B — per-check skip-path fixtures for Checks 1, 2, 4, 5, 6, 7
# Family C — replay of the 2026-05-27 review-sequence-dag-orchestrator-
#            bootstrap-001 false-fire case
# Family D — regression guards: legitimate alerts still fire when no
#            resolution signal is present

def _row(event_type: str, minutes_ago: int = 1, **extra) -> dict:
    """Construct a fake chain_events row matching the helper's expected shape.
    The helper only reads `event_type` from each row; `ts` is included for
    readability + future-proofing."""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    out = {'event_type': event_type, 'ts': ts, 'actor': 'test'}
    out.update(extra)
    return out


class TestResolutionSignalHelper(_TempAgentsRootMixin, unittest.TestCase):
    """Family A — `_resolution_signal_present` unit tests.

    The helper short-circuits on missing env (failsafe path used by the
    test mixin's default setup). Each test here mocks
    `_get_chain_events_for_task` to inject specific row sets, exercising
    the precedence order and failsafe behavior."""

    def test_larry_action_returns_true(self) -> None:
        rows = [_row('larry_action', minutes_ago=5)]
        with patch.object(self.hps, '_get_chain_events_for_task',
                          return_value=rows):
            hit, reason = self.hps._resolution_signal_present(
                task_id='t-001',
                since_ts=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        self.assertTrue(hit)
        self.assertEqual(reason, 'larry_action')

    def test_session_start_returns_superseded_session(self) -> None:
        rows = [_row('session_start', minutes_ago=5)]
        with patch.object(self.hps, '_get_chain_events_for_task',
                          return_value=rows):
            hit, reason = self.hps._resolution_signal_present(
                task_id='t-002',
                since_ts=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        self.assertTrue(hit)
        self.assertEqual(reason, 'superseded_session')

    def test_larry_action_wins_over_session_start(self) -> None:
        """Order matters — larry_action is cheaper to detect and is the
        more decisive signal (Larry already handled it)."""
        rows = [
            _row('session_start', minutes_ago=5),
            _row('larry_action', minutes_ago=3),
        ]
        with patch.object(self.hps, '_get_chain_events_for_task',
                          return_value=rows):
            hit, reason = self.hps._resolution_signal_present(
                task_id='t-003',
                since_ts=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        self.assertTrue(hit)
        self.assertEqual(reason, 'larry_action')

    def test_pr_state_merged_returns_pr_closed(self) -> None:
        """When chain_events has nothing AND check_pr_state=True AND
        gh reports MERGED, helper returns 'pr_closed'."""
        with patch.object(self.hps, '_get_chain_events_for_task',
                          return_value=[]), \
             patch.object(self.hps, '_check_pr_closed_via_gh',
                          return_value='MERGED'):
            hit, reason = self.hps._resolution_signal_present(
                task_id='t-004',
                since_ts=datetime.now(timezone.utc) - timedelta(minutes=30),
                check_pr_state=True,
                pr_url='https://example/pr/1',
            )
        self.assertTrue(hit)
        self.assertEqual(reason, 'pr_closed')

    def test_pr_state_open_falls_through_to_false(self) -> None:
        """check_pr_state=True + PR.state=OPEN (gh helper returns None)
        + no chain_events signals → (False, None)."""
        with patch.object(self.hps, '_get_chain_events_for_task',
                          return_value=[]), \
             patch.object(self.hps, '_check_pr_closed_via_gh',
                          return_value=None):
            hit, reason = self.hps._resolution_signal_present(
                task_id='t-005',
                since_ts=datetime.now(timezone.utc) - timedelta(minutes=30),
                check_pr_state=True,
                pr_url='https://example/pr/1',
            )
        self.assertFalse(hit)
        self.assertIsNone(reason)

    def test_no_signals_returns_false(self) -> None:
        with patch.object(self.hps, '_get_chain_events_for_task',
                          return_value=[]):
            hit, reason = self.hps._resolution_signal_present(
                task_id='t-006',
                since_ts=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        self.assertFalse(hit)
        self.assertIsNone(reason)

    def test_chain_events_query_error_returns_false_failsafe(self) -> None:
        """Infrastructure failure (query returns None) → helper returns
        (False, None). Existing alert behavior is preserved as the
        failsafe. PR-state is NOT consulted when chain_events failed,
        because we want the alert to fire unmodified in that case."""
        with patch.object(self.hps, '_get_chain_events_for_task',
                          return_value=None):
            hit, reason = self.hps._resolution_signal_present(
                task_id='t-007',
                since_ts=datetime.now(timezone.utc) - timedelta(minutes=30),
                check_pr_state=True,
                pr_url='https://example/pr/1',
            )
        self.assertFalse(hit)
        self.assertIsNone(reason)

    def test_unknown_task_short_circuits(self) -> None:
        """Check 5 (retry_exhausted) can emit `task='unknown'` when the
        journalctl line lacks `task=`. No chain_events row could possibly
        match — short-circuit to False without querying."""
        with patch.object(self.hps, '_get_chain_events_for_task') as mock_q:
            hit, reason = self.hps._resolution_signal_present(
                task_id='unknown',
                since_ts=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        self.assertFalse(hit)
        self.assertIsNone(reason)
        mock_q.assert_not_called()

    def test_empty_task_short_circuits(self) -> None:
        with patch.object(self.hps, '_get_chain_events_for_task') as mock_q:
            hit, reason = self.hps._resolution_signal_present(
                task_id='',
                since_ts=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        self.assertFalse(hit)
        self.assertIsNone(reason)
        mock_q.assert_not_called()

    def test_get_chain_events_returns_none_when_env_missing(self) -> None:
        """The mixin clears SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY;
        verify the underlying helper returns None silently (no WARN
        spam on every healer cycle in test environments)."""
        rows = self.hps._get_chain_events_for_task(
            'any-task',
            datetime.now(timezone.utc) - timedelta(minutes=30),
        )
        self.assertIsNone(rows)


class TestPerCheckSkipPaths(_TempAgentsRootMixin, unittest.TestCase):
    """Family B — each of Checks 1, 2, 4, 5, 6, 7 must emit a
    `<CHECK_NAME>_SKIP reason=larry_action` log line and NOT add to its
    alerts list when a larry_action chain_event is present after the
    stall trigger ts."""

    def _patch_resolution(self, reason: str = 'larry_action'):
        """Patch `_resolution_signal_present` to return (True, reason).
        Returns the context manager so callers can wrap their check."""
        return patch.object(
            self.hps, '_resolution_signal_present',
            return_value=(True, reason),
        )

    def _capture_logs(self):
        """Patch the module-level `log` so we can assert specific
        SKIP-shape lines without coupling to other INFO output."""
        captured: list[str] = []
        cm = patch.object(
            self.hps, 'log',
            side_effect=lambda msg, level='INFO': captured.append(msg),
        )
        return cm, captured

    def test_check1_skips_on_larry_action(self) -> None:
        lines = [_watcher_forge_done_line('check1-resolved-001', minutes_ago=180)]
        cm, captured = self._capture_logs()
        with cm, self._patch_resolution('larry_action'):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])
        skip_lines = [m for m in captured if 'FORGE_NO_PR_SKIP' in m]
        self.assertEqual(len(skip_lines), 1)
        self.assertIn('reason=larry_action', skip_lines[0])
        self.assertIn('check1-resolved-001', skip_lines[0])

    def test_check2_skips_on_larry_action(self) -> None:
        pr = {
            'number': 600,
            'headRefName': 'forge/check2-resolved-001',
            'title': 'docs(x): something',
            'createdAt': (
                datetime.now(timezone.utc) - timedelta(minutes=45)
            ).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        cm, captured = self._capture_logs()
        with cm, self._patch_resolution('larry_action'):
            alerts = self.hps.check_pr_no_mirror_dispatch([], [pr], {})
        self.assertEqual(alerts, [])
        skip_lines = [m for m in captured if 'PR_NO_MIRROR_DISPATCH_SKIP' in m]
        self.assertEqual(len(skip_lines), 1)
        self.assertIn('reason=larry_action', skip_lines[0])
        self.assertIn('check2-resolved-001', skip_lines[0])

    def test_check4_skips_on_larry_action(self) -> None:
        lines = [
            f'[{_ts(45)}] [notifier] [INFO] notified beacon <- mirror '
            f'(mirror-result, depth=1, '
            f'file=notify-check4-resolved-001.json)'
        ]
        cm, captured = self._capture_logs()
        with cm, self._patch_resolution('larry_action'):
            alerts = self.hps.check_mirror_marker_invisible(lines, {})
        self.assertEqual(alerts, [])
        skip_lines = [m for m in captured if 'MIRROR_MARKER_INVISIBLE_SKIP' in m]
        self.assertEqual(len(skip_lines), 1)
        self.assertIn('reason=larry_action', skip_lines[0])
        self.assertIn('check4-resolved-001', skip_lines[0])

    def test_check5_skips_on_larry_action(self) -> None:
        fake_journal = (
            'May 27 09:01:00 host inbox-watcher: All retries exhausted '
            'for task=check5-resolved-001\n'
        )
        cm, captured = self._capture_logs()
        with patch('subprocess.run') as mock_sub, \
             cm, self._patch_resolution('larry_action'):
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = fake_journal
            mock_sub.return_value.stderr = ''
            alerts = self.hps.check_retry_exhausted({})
        self.assertEqual(alerts, [])
        skip_lines = [m for m in captured if 'RETRY_EXHAUSTED_SKIP' in m]
        self.assertEqual(len(skip_lines), 1)
        self.assertIn('reason=larry_action', skip_lines[0])
        self.assertIn('check5-resolved-001', skip_lines[0])

    def test_check6_skips_on_larry_action(self) -> None:
        lines = [
            f'[{_ts(45)}] [notifier] [WARN] REVIEW_REVISION on task '
            f'check6-resolved-001 has no forge_build_session_id '
            f"(routing_source='beacon', chat_id=None); skipping."
        ]
        cm, captured = self._capture_logs()
        with cm, self._patch_resolution('larry_action'):
            alerts = self.hps.check_revision_dispatched_with_no_session(
                lines, {},
            )
        self.assertEqual(alerts, [])
        skip_lines = [m for m in captured if 'NO_SESSION_REVISION_SKIP' in m]
        self.assertEqual(len(skip_lines), 1)
        self.assertIn('reason=larry_action', skip_lines[0])
        self.assertIn('check6-resolved-001', skip_lines[0])

    def test_check7_skips_on_larry_action(self) -> None:
        pr = {
            'number': 700,
            'headRefName': 'forge/check7-resolved-001',
            'title': 'feat: x',
            'createdAt': (
                datetime.now(timezone.utc) - timedelta(minutes=180)
            ).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        cm, captured = self._capture_logs()
        with cm, self._patch_resolution('larry_action'):
            alerts = self.hps.check_unrouted_open_prs([pr], [], {})
        self.assertEqual(alerts, [])
        skip_lines = [m for m in captured if 'UNROUTED_OPEN_PR_SKIP' in m]
        self.assertEqual(len(skip_lines), 1)
        self.assertIn('reason=larry_action', skip_lines[0])
        self.assertIn('check7-resolved-001', skip_lines[0])


class TestReviewSequenceDagOrchestratorReplay(_TempAgentsRootMixin,
                                              unittest.TestCase):
    """Family C — replay of the concrete 2026-05-27 false-fire case.
    `review-sequence-dag-orchestrator-bootstrap-001` was reviewed by
    Mirror at 16:24Z (DAG sessions emit `result:` not `REVIEW_*`
    markers; the classifier missed). Check 4 would have fired at
    23:39Z. But: Larry approved via Approvals tab at 16:38Z
    (larry_action chain_event); Mirror also re-ran the review at
    16:21-24Z (session_start). With this PR's helper, Check 4 emits
    MIRROR_MARKER_INVISIBLE_SKIP reason=larry_action."""

    TASK = 'review-sequence-dag-orchestrator-bootstrap-001'

    def test_check4_skips_with_larry_action_winning(self) -> None:
        # Generic mirror-result notify at "16:24Z" (45 min ago from now).
        notify_line = (
            f'[{_ts(45)}] [notifier] [INFO] notified beacon <- mirror '
            f'(mirror-result, depth=1, file=notify-{self.TASK}.json)'
        )
        # Chain_events fixture: larry_action 31 min after notify, plus
        # an even-earlier session_start (Mirror re-ran). larry_action
        # wins per cheap-to-expensive precedence.
        rows = [
            _row('session_start', minutes_ago=48),
            _row('larry_action', minutes_ago=31),
        ]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda m, level='INFO': captured.append(m)), \
             patch.object(self.hps, '_get_chain_events_for_task',
                          return_value=rows):
            alerts = self.hps.check_mirror_marker_invisible([notify_line], {})
        self.assertEqual(alerts, [])
        skip_lines = [m for m in captured if 'MIRROR_MARKER_INVISIBLE_SKIP' in m]
        self.assertEqual(len(skip_lines), 1)
        self.assertIn('reason=larry_action', skip_lines[0])
        self.assertIn(self.TASK, skip_lines[0])

    def test_check4_skips_with_only_session_start_when_larry_absent(self) -> None:
        """Defense in depth: same task, but only the session_start is
        present (no larry_action). Helper falls through to
        'superseded_session'."""
        notify_line = (
            f'[{_ts(45)}] [notifier] [INFO] notified beacon <- mirror '
            f'(mirror-result, depth=1, file=notify-{self.TASK}.json)'
        )
        rows = [_row('session_start', minutes_ago=42)]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda m, level='INFO': captured.append(m)), \
             patch.object(self.hps, '_get_chain_events_for_task',
                          return_value=rows):
            alerts = self.hps.check_mirror_marker_invisible([notify_line], {})
        self.assertEqual(alerts, [])
        skip_lines = [m for m in captured if 'MIRROR_MARKER_INVISIBLE_SKIP' in m]
        self.assertEqual(len(skip_lines), 1)
        self.assertIn('reason=superseded_session', skip_lines[0])


class TestResolutionSignalRegressionGuards(_TempAgentsRootMixin,
                                           unittest.TestCase):
    """Family D — when NO resolution signal is present, every wired check
    must still emit its legitimate alert. Verifies the helper doesn't
    suppress real stalls. Mixin's SUPABASE env-clear means
    `_get_chain_events_for_task` returns None (failsafe → no skip)."""

    def test_check1_fires_when_no_resolution_signal(self) -> None:
        lines = [_watcher_forge_done_line('legit-stall-1', minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('legit-stall-1', alerts[0]['message'])

    def test_check2_fires_when_no_resolution_signal(self) -> None:
        pr = {
            'number': 800, 'headRefName': 'forge/legit-stall-2',
            'title': 'docs: x',
            'createdAt': (
                datetime.now(timezone.utc) - timedelta(minutes=45)
            ).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        alerts = self.hps.check_pr_no_mirror_dispatch([], [pr], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('PR #800', alerts[0]['message'])

    def test_check4_fires_when_no_resolution_signal(self) -> None:
        lines = [
            f'[{_ts(45)}] [notifier] [INFO] notified beacon <- mirror '
            f'(mirror-result, depth=1, file=notify-legit-stall-4.json)'
        ]
        alerts = self.hps.check_mirror_marker_invisible(lines, {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('legit-stall-4', alerts[0]['message'])

    def test_check5_fires_when_no_resolution_signal(self) -> None:
        fake_journal = (
            'May 27 09:01:00 host inbox-watcher: All retries exhausted '
            'for task=legit-stall-5\n'
        )
        with patch('subprocess.run') as mock_sub:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = fake_journal
            mock_sub.return_value.stderr = ''
            alerts = self.hps.check_retry_exhausted({})
        self.assertEqual(len(alerts), 1)
        self.assertIn('legit-stall-5', alerts[0]['message'])

    def test_check6_fires_when_no_resolution_signal(self) -> None:
        lines = [
            f'[{_ts(45)}] [notifier] [WARN] REVIEW_REVISION on task '
            f'legit-stall-6 has no forge_build_session_id'
        ]
        alerts = self.hps.check_revision_dispatched_with_no_session(lines, {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('legit-stall-6', alerts[0]['message'])

    def test_check7_fires_when_no_resolution_signal(self) -> None:
        pr = {
            'number': 900, 'headRefName': 'larry/legit-stall-7',
            'title': 'feat: x',
            'createdAt': (
                datetime.now(timezone.utc) - timedelta(minutes=180)
            ).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        alerts = self.hps.check_unrouted_open_prs([pr], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('PR #900', alerts[0]['message'])

    def test_skip_path_does_not_trip_cooldown(self) -> None:
        """The SKIP path emits a log line + continues; it does NOT call
        `record_alert` (which is what tripping the cooldown counter
        means). Verify by inspecting state dict before/after: stays
        empty when every check skips."""
        lines = [_watcher_forge_done_line('cooldown-untouched-1', minutes_ago=180)]
        state: dict = {}
        with patch.object(self.hps, '_resolution_signal_present',
                          return_value=(True, 'larry_action')):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], state)
        self.assertEqual(alerts, [])
        self.assertEqual(state, {})


class TestFixtureSkipDurable(_TempAgentsRootMixin, unittest.TestCase):
    """2026-05-30 fixture-replay incident — durable guard: run() must not DM
    Larry about stalls whose task is a synthetic fixture, even when a stale
    fixture build record lingers in forge/.archive. Check functions still
    detect them; the skip is at emission."""

    def test_alert_is_fixture_helper(self) -> None:
        f = self.hps._alert_is_fixture
        self.assertTrue(f({'key': 'forge_built_no_pr:zz-fixture-loop'}))
        self.assertTrue(f({'key': 'forge_built_no_pr:t-no-preamble'}))
        self.assertTrue(f({'key': 'forge_built_no_pr:prod-mirror-retry'}))
        self.assertTrue(f({'key': 'mirror_pass_unmerged:zz-fixture-built'}))
        # legit tasks are NOT fixtures
        self.assertFalse(f({'key': 'forge_built_no_pr:add-real-prefix-fixture-allowlist'}))
        self.assertFalse(f({'key': 'forge_built_no_pr:chain-discipline-001'}))
        self.assertFalse(f({'key': 'tier2_fallback:beacon-bot:FAILED:rate_limit'}))
        self.assertFalse(f({'key': 'no-mirror-dispatch:PR#5'}))

    def test_check_detects_but_run_suppresses_fixture(self) -> None:
        task = 'real-phantom-stall-001'
        archive_dir = self.agents_root / 'outboxes' / 'forge' / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f'{task}.json').write_text(json.dumps({
            'task_id': task, 'phase': 'preflight',
            'result': '=== PROCEED ===\n{}\n=== END_PROCEED ===',
        }))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        # check function STILL detects it (detection logic unchanged)
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertTrue(self.hps._alert_is_fixture(alerts[0]))

        # but run() must NOT DM Larry about it
        def side_effect(log_path, hours):
            if 'inbox_watcher.log' in str(log_path):
                return lines
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
        fixture_dms = [c for c in mock_alert.call_args_list
                       if task in (c.kwargs.get('subject', '') or '')]
        self.assertEqual(fixture_dms, [])


def _tier2_line(outcome: str, reason: str, minutes_ago: int = 10) -> str:
    """Build an agent-runner-shape TIER2_FALLBACK_<outcome> log line that the
    Check 8 regex matches. Production sample shape:
        [2026-06-01 14:03:11] [forge] [WARN] TIER2_FALLBACK_SKIPPED reason=rate_limit session=--resume task=foo
    """
    return (f'[{_ts(minutes_ago)}] [forge] [WARN] '
            f'TIER2_FALLBACK_{outcome} reason={reason} task=foo')


class TestCheckTier2FallbackOutcomes(_TempAgentsRootMixin, unittest.TestCase):
    """Check 8: SKIPPED is by-design + auto-remediated → log-only (no DM).
    FAILED / UNAVAILABLE remain actionable → still produce alerts."""

    def _write_forge_log(self, *lines: str) -> None:
        (self.agents_root / 'logs' / 'forge.log').write_text('\n'.join(lines) + '\n')

    def test_tier2_skipped_is_log_only(self) -> None:
        self._write_forge_log(_tier2_line('SKIPPED', 'rate_limit'))
        with patch.object(self.hps, 'log', wraps=self.hps.log) as mock_log:
            alerts = self.hps.check_tier2_fallback_failures({})
        # No alert returned → run_once never DMs Larry about it.
        self.assertEqual(alerts, [])
        # An INFO log line was emitted instead.
        info_calls = [
            c for c in mock_log.call_args_list
            if 'tier2-fallback SKIPPED (by-design' in c.args[0]
            and (len(c.args) > 1 and c.args[1] == 'INFO')
        ]
        self.assertEqual(len(info_calls), 1)
        self.assertIn('agent=forge', info_calls[0].args[0])
        self.assertIn('reason=rate_limit', info_calls[0].args[0])
        # Cursor advanced → a second scan does not re-process the same line.
        cursor = self.hps.load_check8_cursor()
        self.assertIn('forge:SKIPPED:rate_limit', cursor)
        with patch.object(self.hps, 'log', wraps=self.hps.log) as mock_log2:
            alerts2 = self.hps.check_tier2_fallback_failures({})
        self.assertEqual(alerts2, [])
        reemit = [c for c in mock_log2.call_args_list
                  if 'tier2-fallback SKIPPED (by-design' in c.args[0]]
        self.assertEqual(reemit, [])

    def test_tier2_failed_still_alerts(self) -> None:
        self._write_forge_log(_tier2_line('FAILED', 'rate_limit'))
        alerts = self.hps.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]['subject'],
            'pipeline-stall:tier2-fallback-failed-rate_limit:forge',
        )

    def test_tier2_unavailable_still_alerts(self) -> None:
        self._write_forge_log(_tier2_line('UNAVAILABLE', 'auth_401'))
        alerts = self.hps.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]['subject'],
            'pipeline-stall:tier2-fallback-unavailable-auth_401:forge',
        )

    def test_tier2_failed_wording_not_lapsed_when_auth_ok(self) -> None:
        # 2026-06-02 false-alarm fix: FAILED still alerts, but when Tier 2 OAuth
        # verifies the message must NOT claim lapsed/expired credentials.
        self._write_forge_log(_tier2_line('FAILED', 'rate_limit'))
        with patch('active_tier.tier_auth_ok', return_value=True):
            alerts = self.hps.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]['subject'],
            'pipeline-stall:tier2-fallback-failed-rate_limit:forge',
        )
        self.assertNotIn('lapsed', alerts[0]['message'].lower())
        self.assertIn('verifies ok', alerts[0]['message'].lower())

    def test_tier2_failed_wording_lapsed_when_auth_fails(self) -> None:
        self._write_forge_log(_tier2_line('FAILED', 'rate_limit'))
        with patch('active_tier.tier_auth_ok', return_value=False):
            alerts = self.hps.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts), 1)
        self.assertIn('lapsed', alerts[0]['message'].lower())

    def test_tier2_mixed_outcomes(self) -> None:
        self._write_forge_log(
            _tier2_line('SKIPPED', 'rate_limit'),
            _tier2_line('FAILED', 'rate_limit'),
            _tier2_line('UNAVAILABLE', 'auth_401'),
        )
        alerts = self.hps.check_tier2_fallback_failures({})
        subjects = sorted(a['subject'] for a in alerts)
        self.assertEqual(subjects, [
            'pipeline-stall:tier2-fallback-failed-rate_limit:forge',
            'pipeline-stall:tier2-fallback-unavailable-auth_401:forge',
        ])
        self.assertFalse(any('skipped' in s for s in subjects))


if __name__ == '__main__':
    unittest.main()
