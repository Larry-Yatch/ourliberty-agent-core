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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _ts(minutes_ago: int) -> str:
    """Produce an outbox-notifier-shape log-line timestamp `minutes_ago` minutes
    before now. Format: `2026-05-26 13:08:39` (human-readable, NAIVE host-local).

    outbox_notifier.log() stamps `datetime.now()` — naive host-local time (the
    droplet runs America/Denver) — so this mirror uses `datetime.now()`, NOT
    `datetime.now(timezone.utc)`. heal_pipeline_stall._parse_ts interprets a
    naive stamp as host-local and converts to UTC, so the round-trip is only
    correct when this fixture and the parser agree on the zone — which is why
    every test class pins TZ to America/Denver (see _TempAgentsRootMixin).
    Used inside the `[<ts>] [notifier] [INFO] ...` line shape."""
    dt = datetime.now() - timedelta(minutes=minutes_ago)
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
        # Pin the process TZ to the production droplet's zone. outbox_notifier
        # writes naive host-local timestamps and _parse_ts interprets naive as
        # host-local, so the naive `_ts()` fixtures only round-trip when the
        # test's local zone is fixed — otherwise the suite is non-deterministic
        # across developer machines (a UTC box and an MDT box parse the same
        # fixture to instants 6h apart). America/Denver also gives the parser a
        # real non-zero UTC offset to exercise, so the naive->UTC conversion is
        # genuinely tested rather than trivially satisfied under TZ=UTC.
        self._tz_orig = os.environ.get('TZ')
        os.environ['TZ'] = 'America/Denver'
        time.tzset()
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
        # Restore the TZ the test inherited.
        if self._tz_orig is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self._tz_orig
        time.tzset()


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


class TestParseTs(_TempAgentsRootMixin, unittest.TestCase):
    """_parse_ts must read a NAIVE outbox_notifier timestamp as host-local and
    convert it to UTC, NOT stamp it as UTC. outbox_notifier.log() writes
    `datetime.now()` (naive, droplet-local America/Denver); reading it as UTC
    skewed every event ~6h into the past, so a recent event looked stale (false
    stall trigger) or scan windows clipped events they shouldn't. These cases
    fail under the prior `dt.replace(tzinfo=timezone.utc)` and pass under
    `dt.astimezone(timezone.utc)`. Mirrors heal_pr_auto_merge._to_utc /
    chain_event_shipper._normalize_iso_ts (the same 6h-skew incident).

    _TempAgentsRootMixin pins TZ to America/Denver, so naive stamps resolve at
    that zone's real offset (MDT -06:00 in summer, MST -07:00 in winter)."""

    def test_naive_summer_stamp_interpreted_as_local_then_utc(self) -> None:
        # 00:00:23 MDT (-06:00) == 06:00:23 UTC. Under the old code this was
        # mis-stamped as 00:00:23 UTC — the 6h skew.
        dt = self.hps._parse_ts('2026-06-11 00:00:23')
        self.assertIsNotNone(dt)
        self.assertEqual(dt.utcoffset(), timedelta(0))  # aware, normalized UTC
        self.assertEqual(dt.isoformat(), '2026-06-11T06:00:23+00:00')

    def test_naive_winter_stamp_uses_real_dst_offset(self) -> None:
        # 00:00:00 MST (-07:00, no DST in January) == 07:00:00 UTC. Proves the
        # conversion follows the zone's real DST rules, not a frozen offset.
        dt = self.hps._parse_ts('2026-01-15 00:00:00')
        self.assertEqual(dt.isoformat(), '2026-01-15T07:00:00+00:00')

    def test_naive_with_t_separator_and_microseconds(self) -> None:
        dt = self.hps._parse_ts('2026-06-11T00:00:23.500000')
        self.assertEqual(dt.isoformat(), '2026-06-11T06:00:23.500000+00:00')

    def test_aware_z_suffix_passes_through_to_same_instant(self) -> None:
        # An aware 'Z' timestamp denotes an absolute instant; it must NOT be
        # shifted by the local zone, only normalized to +00:00.
        dt = self.hps._parse_ts('2026-06-11T06:00:23Z')
        self.assertEqual(dt.isoformat(), '2026-06-11T06:00:23+00:00')

    def test_aware_explicit_offset_normalized_to_utc(self) -> None:
        # The inbox_watcher / gh shape carries an explicit offset; same instant.
        dt = self.hps._parse_ts('2026-06-11T00:00:23-06:00')
        self.assertEqual(dt.isoformat(), '2026-06-11T06:00:23+00:00')

    def test_compact_offset_normalized(self) -> None:
        # +HHMM (no colon) is normalized before parsing.
        dt = self.hps._parse_ts('2026-06-11T00:00:23-0600')
        self.assertEqual(dt.isoformat(), '2026-06-11T06:00:23+00:00')

    def test_unparseable_returns_none(self) -> None:
        self.assertIsNone(self.hps._parse_ts('not-a-timestamp'))
        self.assertIsNone(self.hps._parse_ts(''))


class TestReadRecentLogLines(_TempAgentsRootMixin, unittest.TestCase):
    """The coarse LOG_LOOKBACK_HOURS pre-filter must bound BOTH log shapes
    correctly: the notifier's naive host-local stamp AND inbox_watcher's aware
    UTC stamp (`...+00:00`). The capture regex must keep the offset — stripping
    it would make _parse_ts read the watcher's UTC digits as host-local (TZ is
    pinned to America/Denver here) and shift a >24h-old line ~6h forward, so it
    would slip back inside the window."""

    def _write_log(self, *lines: str) -> Path:
        p = self.agents_root / 'logs' / 'recent.log'
        p.write_text('\n'.join(lines) + '\n')
        return p

    def test_notifier_naive_lines_bounded_by_window(self) -> None:
        p = self._write_log(
            f'[{_ts(60)}] [notifier] [INFO] fresh',
            f'[{_ts(25 * 60)}] [notifier] [INFO] stale',
        )
        out = self.hps._read_recent_log_lines(p, self.hps.LOG_LOOKBACK_HOURS)
        self.assertEqual(len(out), 1)
        self.assertIn('fresh', out[0])

    def test_watcher_aware_lines_bounded_by_window(self) -> None:
        # 25h-ago aware line: correctly EXCLUDED. Under an offset-stripping
        # capture it would parse as 25h-ago-Denver -> +6h -> 19h -> wrongly kept.
        p = self._write_log(
            f'[{_watcher_ts(60)}] inbox_watcher: [forge] done task=fresh-001 success=True',
            f'[{_watcher_ts(25 * 60)}] inbox_watcher: [forge] done task=stale-001 success=True',
        )
        out = self.hps._read_recent_log_lines(p, self.hps.LOG_LOOKBACK_HOURS)
        self.assertEqual(len(out), 1)
        self.assertIn('fresh-001', out[0])


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

    def test_skips_when_pr_is_closed(self) -> None:
        """A CLOSED-not-merged PR for the task is a deliberate abandonment —
        not a stall. Fixes the forge-built-no-pr-closed-pr-fp recurrence
        (PR #712 CLOSED, refiring each 6h cooldown)."""
        lines = [_watcher_forge_done_line('closed-task-001', minutes_ago=180)]
        closed_prs = [{'headRefName': 'forge/closed-task-001', 'number': 712,
                       'state': 'CLOSED', '_repo': 'x/y'}]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {}, closed_prs=closed_prs)
        self.assertEqual(alerts, [])

    # ----- pr-<repo>-<num> task_id reconciliation (step 1a2) -----
    # The task is named directly after an existing PR number, so
    # `_pr_matches_task` (branch/title only) never correlates it; the named PR
    # is gh-resolved instead. Fixes the recurring `forge_built_no_pr` FP on
    # `pr-ourliberty-agent-core-712` (PR #712 CLOSED).

    _PR_TASK_REPOS = [
        'Larry-Yatch/ourliberty-agent-core',
        'Larry-Yatch/ourliberty-dashboard',
    ]

    def test_skips_when_pr_task_id_named_pr_is_closed(self) -> None:
        """`pr-<repo>-<num>` task whose named PR is gh-confirmed CLOSED is a
        valid resolution — zero alerts, one FORGE_NO_PR_SKIP."""
        lines = [_watcher_forge_done_line('pr-ourliberty-agent-core-712',
                                          minutes_ago=180)]
        with patch.object(self.hps, 'REPOS', self._PR_TASK_REPOS), \
                patch.object(self.hps, '_gh_pr_state', return_value='CLOSED'):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])

    def test_skips_when_pr_task_id_named_pr_is_merged(self) -> None:
        """Same shape, named PR gh-confirmed MERGED — also a valid resolution."""
        lines = [_watcher_forge_done_line('pr-ourliberty-agent-core-712',
                                          minutes_ago=180)]
        with patch.object(self.hps, 'REPOS', self._PR_TASK_REPOS), \
                patch.object(self.hps, '_gh_pr_state', return_value='MERGED'):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])

    def test_pr_task_id_open_not_suppressed(self) -> None:
        """An OPEN named PR is NOT a resolution — the new step must not suppress;
        the task falls through to the normal build-gap alert (fail-safe)."""
        lines = [_watcher_forge_done_line('pr-ourliberty-agent-core-712',
                                          minutes_ago=180)]
        with patch.object(self.hps, 'REPOS', self._PR_TASK_REPOS), \
                patch.object(self.hps, '_gh_pr_state', return_value='OPEN'):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['key'],
                         'forge_built_no_pr:pr-ourliberty-agent-core-712')

    def test_pr_task_id_gh_error_not_suppressed(self) -> None:
        """A gh transport/JSON error (`_gh_pr_state` -> None) must NOT suppress —
        an outage cannot masquerade as a positive skip signal."""
        lines = [_watcher_forge_done_line('pr-ourliberty-agent-core-712',
                                          minutes_ago=180)]
        with patch.object(self.hps, 'REPOS', self._PR_TASK_REPOS), \
                patch.object(self.hps, '_gh_pr_state', return_value=None):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['key'],
                         'forge_built_no_pr:pr-ourliberty-agent-core-712')

    def test_pr_task_id_helper_no_shellout_on_non_match(self) -> None:
        """A task_id that doesn't match `pr-<repo>-<num>` returns None without
        shelling out to gh."""
        with patch.object(self.hps, '_gh_pr_state') as mock_state:
            self.assertIsNone(
                self.hps._forge_pr_task_id_resolved('regular-task-001'))
            mock_state.assert_not_called()

    def test_pr_task_id_helper_no_shellout_on_unmappable_repo(self) -> None:
        """A `pr-<repo>-<num>` whose repo maps to no REPOS slug returns None
        (fail-safe) without shelling out."""
        with patch.object(self.hps, 'REPOS', self._PR_TASK_REPOS), \
                patch.object(self.hps, '_gh_pr_state') as mock_state:
            self.assertIsNone(
                self.hps._forge_pr_task_id_resolved('pr-some-other-repo-5'))
            mock_state.assert_not_called()

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

    def test_preflight_family_no_match_on_dash_prefixed_sibling_family(self) -> None:
        """Prefix-extension over-match guard: a preflight for family
        `add-user-auth` must NOT be silenced by the shipped build of the
        DISTINCT family `add-user-auth-v2`. The old `startswith(family + '-')`
        accepted the `-v2-<ts>` suffix; the timestamp anchor rejects it because
        the segment after `add-user-auth-` is `v2`, not a build timestamp."""
        self.assertIsNone(self.hps._preflight_family_shipped(
            'add-user-auth-preflight-20260603T231401Z-clarify1',
            [{'headRefName': 'forge/build-add-user-auth-v2-20260604T045743Z',
              'number': 1}],
        ))

    def test_preflight_family_matches_exact_family_with_timestamp(self) -> None:
        """The legitimate case still matches: the build branch for the SAME
        family is `forge/build-<family>-<ts>`."""
        pr = {'headRefName': 'forge/build-add-user-auth-20260604T045743Z',
              'number': 2}
        self.assertEqual(self.hps._preflight_family_shipped(
            'add-user-auth-preflight-20260603T231401Z-clarify1', [pr],
        ), pr)

    def test_preflight_family_matches_short_hhmm_timestamp(self) -> None:
        """Production build branches also use the shorter `YYYYMMDDTHHMM` stamp
        (no seconds, no Z), e.g. `forge/build-...-20260604T1528`. That must
        still register as the shipped build."""
        pr = {'headRefName': 'forge/build-agent-queue-generalize-20260604T1528',
              'number': 4}
        self.assertEqual(self.hps._preflight_family_shipped(
            'agent-queue-generalize-preflight-20260603T231401Z', [pr],
        ), pr)

    def test_preflight_family_requires_timestamp_after_family(self) -> None:
        """A build branch whose `<family>-` suffix is NOT a timestamp (e.g. an
        unrelated descriptive slug that happens to share the dash-prefix) must
        not match."""
        self.assertIsNone(self.hps._preflight_family_shipped(
            'forge-queue-api-preflight-20260603T231401Z',
            [{'headRefName': 'forge/build-forge-queue-api-extension-work',
              'number': 3}],
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
        # A realistic-length task_id carried verbatim in a human PR title still
        # matches via the title fallback (case-insensitively).
        pr = {
            'headRefName': 'forge/something-else',
            'title': 'fix: handle PIPELINE-STALL-RECOVERY-001 case',
        }
        self.assertEqual(
            self.hps._pr_matches_task(pr, 'pipeline-stall-recovery-001'),
            'title',
        )

    def test_pr_matches_task_title_short_id_below_floor(self) -> None:
        # audit #19 mirror: a short/common task_id must NOT match by title
        # substring (previously it did and stalled recovery).
        pr = {
            'headRefName': 'forge/something-else',
            'title': 'fix: handle MY-TASK-001 case',
        }
        self.assertIsNone(self.hps._pr_matches_task(pr, 'my-task-001'))

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

    def _write_archive(self, task: str, payload: dict) -> None:
        archive_dir = self.agents_root / 'outboxes' / 'forge' / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f'{task}.json').write_text(json.dumps(payload))

    def test_reclassifies_consumed_but_errored_preflight_no_sibling_pr(self) -> None:
        """Step 5: a preflight/clarify pointer with NO sibling PR whose
        archived outbox shows consumed-but-errored (the production shape:
        exit_code=-1, error='All retries exhausted') must NOT fire the
        build-gap alarm. It surfaces exactly once under the distinct
        `pipeline-stall:pr-create-inferred-failure:<task>` subject."""
        task = 'forge-queue-api-preflight-20260603T231401Z-clarify1'
        self._write_archive(task, {
            'task_id': task,
            'phase': 'preflight',
            'exit_code': -1,
            'attempts': None,
            'error': 'All retries exhausted',
            'result': 'All retries exhausted',
        })
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO': captured.append(msg)):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]['subject'],
            f'pipeline-stall:pr-create-inferred-failure:{task}',
        )
        # Build-gap subject must NOT be present.
        self.assertNotIn('forge-no-pr', alerts[0]['subject'])
        self.assertIn('All retries exhausted', alerts[0]['message'])
        reclassify = [m for m in captured if 'FORGE_NO_PR_RECLASSIFY' in m]
        self.assertEqual(len(reclassify), 1)
        self.assertIn('reason=pr_create_inferred_failure', reclassify[0])

    def test_reclassifies_on_auth_401_signal(self) -> None:
        """Step 5 also recognizes a gh-pr-create auth_401 signal (exit_code
        clean but the run carried an auth_401 error) and reclassifies."""
        task = 'dashboard-x-preflight-20260604T010000Z-clarify1'
        self._write_archive(task, {
            'task_id': task,
            'phase': 'preflight',
            'exit_code': 0,
            'error': 'gh pr create failed: auth_401 token expired',
        })
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]['subject'],
            f'pipeline-stall:pr-create-inferred-failure:{task}',
        )

    def test_step4_sibling_pr_wins_over_step5_reclassify(self) -> None:
        """Ordering invariant: when the sibling build already shipped
        (Step 4 / `_preflight_family_shipped`), a resolved task is fully
        suppressed and never reaches the Step 5 infra-failure branch —
        zero alerts, not a pr-create-inferred-failure alert. Uses the
        exact production ids (clarify task + merged PR #294) PLUS the
        errored archive so both paths could match; Step 4 must win."""
        task = 'forge-queue-api-preflight-20260603T231401Z-clarify1'
        self._write_archive(task, {
            'task_id': task,
            'phase': 'preflight',
            'exit_code': -1,
            'error': 'All retries exhausted',
        })
        merged_prs = [{
            'headRefName': 'forge/build-forge-queue-api-20260603T234656Z',
            'number': 294,
            'title': 'feat(dashboard): add read-only GET /api/system/agent-queue lifecycle endpoint',
            '_repo': 'Larry-Yatch/ourliberty-dashboard',
        }]
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(alerts, [])

    # ----- Reconciliation step 1c: already-merged bridge (#610 sibling) -----

    _ALREADY_MERGED_TASK = 'fix-645-alert-translation-001'
    _AGENT_CORE = 'Larry-Yatch/ourliberty-agent-core'

    def _already_merged_result(self, pr_number: int) -> str:
        """A Forge build result carrying the canonical no-delta contract line
        plus the prose that drove the real 2026-06-23 false stall."""
        return (
            f'NO PR — already merged: #{pr_number}\n'
            f'git diff main..HEAD is empty; the alert-translation entry was '
            f'already committed to the branch before this dispatch ran.'
        )

    def test_bridges_already_merged_pr_in_memory(self) -> None:
        """The #645 production case: a standalone build whose work shipped under
        a differently-named PR. The outbox names the merged PR via the canonical
        contract line; that PR is in the fetched merged list (no gh call). Skip,
        not a stall — logged as reason=already_merged_bridge."""
        task = self._ALREADY_MERGED_TASK
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'target_repo': self._AGENT_CORE,
            'result': self._already_merged_result(645),
        })
        merged_prs = [{
            'headRefName': 'fix/proposed-retirement-forge-matcher',
            'number': 645,
            'title': 'fix(missions): retire shipped proposed cards stuck on the lane',
            '_repo': self._AGENT_CORE,
        }]
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO': captured.append(msg)):
            alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(alerts, [])
        skip = [m for m in captured
                if 'FORGE_NO_PR_SKIP' in m and 'already_merged_bridge' in m]
        self.assertEqual(len(skip), 1)
        self.assertIn('pr=#645', skip[0])

    def test_bridges_already_merged_pr_via_direct_gh_verify(self) -> None:
        """A PR older than the 7-day merged-fetch window isn't in `merged_prs`;
        the bridge falls back to a direct gh-MERGED verify scoped to the
        outbox's target_repo. No in-memory match → exactly one gh call."""
        task = self._ALREADY_MERGED_TASK
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'target_repo': self._AGENT_CORE,
            'result': self._already_merged_result(421),
        })
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        with patch('sequence_shortcut_helpers.gh_pr_merge_info',
                   return_value=(
                       'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/421',
                       '2026-06-01T00:00:00Z')) as mock_gh:
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])
        mock_gh.assert_called_once_with(self._AGENT_CORE, 421)

    def test_bridge_alerts_when_named_pr_not_merged(self) -> None:
        """Safe direction: if gh cannot confirm the named PR is MERGED, the
        bridge returns None and the build-gap alert still fires."""
        task = self._ALREADY_MERGED_TASK
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'target_repo': self._AGENT_CORE,
            'result': self._already_merged_result(999),
        })
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        with patch('sequence_shortcut_helpers.gh_pr_merge_info', return_value=None):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'],
                         f'pipeline-stall:forge-no-pr:{task}')

    def test_bridge_refuses_ambiguous_number_without_target_repo(self) -> None:
        """No target_repo on the outbox + the named #N exists in BOTH tracked
        repos = ambiguous. Refuse to guess (no repo to scope or gh-verify) →
        the alert fires."""
        task = self._ALREADY_MERGED_TASK
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'result': self._already_merged_result(645),  # no target_repo
        })
        merged_prs = [
            {'number': 645, 'headRefName': 'a', 'title': 't',
             '_repo': 'Larry-Yatch/ourliberty-agent-core'},
            {'number': 645, 'headRefName': 'b', 'title': 't',
             '_repo': 'Larry-Yatch/ourliberty-dashboard'},
        ]
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(len(alerts), 1)

    def test_bridge_unique_number_without_target_repo_skips(self) -> None:
        """No target_repo but the named #N is unique across the fetched PRs →
        the unique cross-repo match is unambiguous, so skip."""
        task = self._ALREADY_MERGED_TASK
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'result': self._already_merged_result(645),
        })
        merged_prs = [{'number': 645, 'headRefName': 'a', 'title': 't',
                       '_repo': 'Larry-Yatch/ourliberty-agent-core'}]
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(alerts, [])

    def test_bridge_ignores_open_pr_with_same_number(self) -> None:
        """Contract guard: the bridge trusts only confirmed-MERGED PRs, never
        the OPEN-PR list. An OPEN PR sharing the named number — a cross-repo
        number collision, or the work genuinely still in review — must NOT
        suppress the stall: `merged_prs` is empty and the direct gh verify
        reports not-MERGED, so the build-gap alert still fires. (Passing the
        open+merged list here would have let the open PR mask a real stall.)"""
        task = self._ALREADY_MERGED_TASK
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'target_repo': self._AGENT_CORE,
            'result': self._already_merged_result(645),
        })
        open_prs = [{'number': 645, 'headRefName': 'feature/unrelated',
                     'title': 'something else', '_repo': self._AGENT_CORE}]
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        with patch('sequence_shortcut_helpers.gh_pr_merge_info', return_value=None):
            alerts = self.hps.check_forge_built_no_pr(lines, open_prs, [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'],
                         f'pipeline-stall:forge-no-pr:{task}')

    def test_bridge_inert_when_no_already_merged_cue(self) -> None:
        """A normal build outbox with no already-merged contract line / cue is
        untouched by the bridge — a genuine no-PR stall still fires."""
        task = 'genuine-no-pr-stall-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'target_repo': self._AGENT_CORE,
            'result': 'Built the feature and ran the suite. All green.',
        })
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'],
                         f'pipeline-stall:forge-no-pr:{task}')

    # ----- Reconciliation step 1d: sibling-pr_title shipped (2026-06-25) -----

    _SIBLING_PR_TITLE = (
        'chore(missions): reconcile orchestrator-terminal-signal-hardening '
        'to shipped (#672/#673)'
    )

    def test_sibling_pr_title_shipped_suppresses_stall(self) -> None:
        """The 2026-06-25 production case: task
        `reconcile-hardening-mission-shipped-001` honestly opened NO PR (its
        result is prose). The redispatch `-002` carried the IDENTICAL pr_title
        and shipped it under MERGED PR #688 on branch `forge/...-002`, whose
        branch + title carry the -002 token so `_pr_matches_task` can't
        correlate it and no `already merged: #N` line is in -001's result.
        The exact-pr_title sibling match suppresses the false stall —
        reason=sibling_pr_title_shipped, pr=#688."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'task_type': 'doc-only', 'pr_title': self._SIBLING_PR_TITLE,
            'result': 'Done. The JSON parses cleanly and the entry is reconciled.',
        })
        merged_prs = [{
            'headRefName': 'forge/reconcile-hardening-mission-shipped-002',
            'number': 688,
            'title': self._SIBLING_PR_TITLE,
            '_repo': self._AGENT_CORE,
        }]
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO': captured.append(msg)):
            alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(alerts, [])
        skip = [m for m in captured
                if 'FORGE_NO_PR_SKIP' in m and 'sibling_pr_title_shipped' in m]
        self.assertEqual(len(skip), 1)
        self.assertIn('pr=#688', skip[0])

    def test_sibling_pr_title_matches_open_pr_too(self) -> None:
        """The sibling PR need only be gh-truth (in the open+merged union); an
        OPEN sibling PR with the identical pr_title also suppresses the stall."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'pr_title': self._SIBLING_PR_TITLE, 'result': 'Done.',
        })
        open_prs = [{
            'headRefName': 'forge/reconcile-hardening-mission-shipped-002',
            'number': 688, 'title': self._SIBLING_PR_TITLE, '_repo': self._AGENT_CORE,
        }]
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, open_prs, [], {})
        self.assertEqual(alerts, [])

    def test_unique_pr_title_still_alerts(self) -> None:
        """No sibling carries this pr_title — a genuine no-PR stall must still
        fire unchanged. Guards against the step silencing real stalls."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'pr_title': self._SIBLING_PR_TITLE, 'result': 'Done.',
        })
        merged_prs = [{
            'headRefName': 'forge/some-unrelated-task-009',
            'number': 700,
            'title': 'feat(other): a completely different unit of work',
            '_repo': self._AGENT_CORE,
        }]
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'],
                         f'pipeline-stall:forge-no-pr:{task}')

    def test_sibling_helper_none_on_missing_pr_title(self) -> None:
        """No pr_title on the outbox = no signal to match on → helper returns
        None and the build-gap alert fires (fail-safe)."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0, 'result': 'Done.',
        })
        merged_prs = [{
            'headRefName': 'forge/reconcile-hardening-mission-shipped-002',
            'number': 688, 'title': self._SIBLING_PR_TITLE, '_repo': self._AGENT_CORE,
        }]
        self.assertIsNone(
            self.hps._forge_sibling_pr_title_shipped(task, merged_prs))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(len(alerts), 1)

    def test_sibling_helper_ignores_self_same_task_id(self) -> None:
        """A PR with the matching pr_title but the SAME branch task_id is self,
        not a sibling — the helper must not match its own (absent) PR. Here only
        a same-task_id PR exists, so the helper returns None."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'pr_title': self._SIBLING_PR_TITLE, 'result': 'Done.',
        })
        self_pr = [{
            'headRefName': f'forge/{task}',
            'number': 688, 'title': self._SIBLING_PR_TITLE, '_repo': self._AGENT_CORE,
        }]
        self.assertIsNone(
            self.hps._forge_sibling_pr_title_shipped(task, self_pr))

    def test_sibling_helper_none_on_ambiguous_two_siblings(self) -> None:
        """Two DISTINCT sibling PRs share the pr_title = ambiguous; refuse to
        guess (return None) so the alert still fires rather than bridging to an
        arbitrary one."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'pr_title': self._SIBLING_PR_TITLE, 'result': 'Done.',
        })
        prs = [
            {'headRefName': 'forge/reconcile-hardening-mission-shipped-002',
             'number': 688, 'title': self._SIBLING_PR_TITLE, '_repo': self._AGENT_CORE},
            {'headRefName': 'forge/reconcile-hardening-mission-shipped-003',
             'number': 689, 'title': self._SIBLING_PR_TITLE, '_repo': self._AGENT_CORE},
        ]
        self.assertIsNone(
            self.hps._forge_sibling_pr_title_shipped(task, prs))

    def test_sibling_helper_requires_exact_title_not_substring(self) -> None:
        """Substring is intentionally NOT accepted: a PR whose title merely
        CONTAINS the pr_title (with extra text) is not an exact match → None."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'pr_title': self._SIBLING_PR_TITLE, 'result': 'Done.',
        })
        prs = [{
            'headRefName': 'forge/reconcile-hardening-mission-shipped-002',
            'number': 688, 'title': self._SIBLING_PR_TITLE + ' and more',
            '_repo': self._AGENT_CORE,
        }]
        self.assertIsNone(
            self.hps._forge_sibling_pr_title_shipped(task, prs))

    # ----- Reconciliation step 1e: retry-redispatch PR exists (2026-06-25) ---

    def test_retry1_open_pr_suppresses_stall(self) -> None:
        """A1 (production case): task
        `reconcile-hardening-mission-shipped-001` honestly opened NO PR. Its
        SEPARATE redispatch `-001-retry1` opened OPEN PR #699 on branch
        `forge/reconcile-hardening-mission-shipped-001-retry1`. The retry-branch
        match suppresses the false stall — reason=retry_pr_exists, pr=#699."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'task_type': 'doc-only', 'result': 'Done. Reconciled.',
        })
        open_prs = [{
            'headRefName': 'forge/reconcile-hardening-mission-shipped-001-retry1',
            'number': 699,
            'title': 'chore: reconcile (retry)',
            '_repo': self._AGENT_CORE,
        }]
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO': captured.append(msg)):
            alerts = self.hps.check_forge_built_no_pr(lines, open_prs, [], {})
        self.assertEqual(alerts, [])
        skip = [m for m in captured
                if 'FORGE_NO_PR_SKIP' in m and 'retry_pr_exists' in m]
        self.assertEqual(len(skip), 1)
        self.assertIn('pr=#699', skip[0])

    def test_retry_merged_pr_suppresses_stall(self) -> None:
        """The retry PR need only be gh-truth (open OR merged). A MERGED
        `-retry2` PR also suppresses the original task's stall."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0, 'result': 'Done.',
        })
        merged_prs = [{
            'headRefName': 'forge/reconcile-hardening-mission-shipped-001-retry2',
            'number': 701, 'title': 'chore: reconcile (retry2)',
            '_repo': self._AGENT_CORE,
        }]
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(alerts, [])

    def test_retry_sibling_outbox_aged_out_suppresses_stall(self) -> None:
        """A2: the retry PR aged out of the live fetch window (absent from
        all_prs), but the archived retry-sibling outbox `<task>-retry1.json`
        carries proof a PR was opened (`PR opened:` preamble) → suppressed via
        the archive anchor. reason=retry_pr_exists, retry_outbox names the
        sibling file."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0, 'result': 'Done.',
        })
        self._write_archive(f'{task}-retry1', {
            'task_id': f'{task}-retry1', 'phase': 'build', 'exit_code': 0,
            'result': 'PR opened: https://github.com/x/y/pull/699',
        })
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO': captured.append(msg)):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])
        skip = [m for m in captured
                if 'FORGE_NO_PR_SKIP' in m and 'retry_pr_exists' in m]
        self.assertEqual(len(skip), 1)
        self.assertIn(f'{task}-retry1.json', skip[0])

    def test_no_retry_pr_still_alerts(self) -> None:
        """NEGATIVE (Pattern A): a genuine no-PR task with NO `-retry<N>` PR
        anywhere and no PR-bearing retry outbox must still fire unchanged."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0, 'result': 'Done.',
        })
        self.assertIsNone(self.hps._forge_retry_pr_exists(task, []))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'],
                         f'pipeline-stall:forge-no-pr:{task}')

    def test_retry_outbox_without_pr_proof_does_not_suppress(self) -> None:
        """A retry-sibling outbox that resolved cleanly but shows NO PR (e.g. a
        PROCEED that only dispatched a downstream build) is not proof of a PR —
        the helper returns None and the stall still fires (fail-safe)."""
        task = 'reconcile-hardening-mission-shipped-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0, 'result': 'Done.',
        })
        self._write_archive(f'{task}-retry1', {
            'task_id': f'{task}-retry1', 'phase': 'build', 'exit_code': 0,
            'result': 'Resolved with === PROCEED ===; dispatched downstream build.',
        })
        self.assertIsNone(self.hps._forge_retry_pr_exists(task, []))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)

    # ----- Reconciliation step 1f: rebase-class target shipped (2026-06-25) --

    def test_rebase_target_in_union_suppresses_stall(self) -> None:
        """B1 (production case): task `rebase-forge-post-open-mergeable-687-001`
        is a rebase-class task that opens no PR by design; its archived result
        names PR #687, present in the open+merged union (here MERGED). The
        stall is suppressed — reason=rebase_target_shipped, pr=#687."""
        task = 'rebase-forge-post-open-mergeable-687-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'result': ('PR #687 is now `MERGEABLE / CLEAN`. Done. Rebase '
                       'complete — PR #687 ready for Mirror.'),
        })
        merged_prs = [{
            'headRefName': 'forge/some-feature-687',
            'number': 687, 'title': 'feat: the rebased work',
            '_repo': self._AGENT_CORE,
        }]
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO': captured.append(msg)):
            alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(alerts, [])
        skip = [m for m in captured
                if 'FORGE_NO_PR_SKIP' in m and 'rebase_target_shipped' in m]
        self.assertEqual(len(skip), 1)
        self.assertIn('pr=#687', skip[0])

    def test_rebase_target_absent_from_union_still_alerts(self) -> None:
        """NEGATIVE (Pattern B): a `rebase-` task whose referenced PR is NOT in
        the open+merged union (a genuinely missing target) must still fire."""
        task = 'rebase-forge-post-open-mergeable-687-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'result': 'PR #687 is now MERGEABLE / CLEAN. Done.',
        })
        merged_prs = [{
            'headRefName': 'forge/unrelated-900', 'number': 900,
            'title': 'something else', '_repo': self._AGENT_CORE,
        }]
        self.assertIsNone(
            self.hps._forge_rebase_target_shipped(task, merged_prs))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'],
                         f'pipeline-stall:forge-no-pr:{task}')

    def test_rebase_helper_only_applies_to_rebase_task_ids(self) -> None:
        """Tight-scope guard: a NON-`rebase-` task whose result happens to name
        a PR present in the union is NOT a rebase candidate — the helper returns
        None so the normal build-gap decision is untouched."""
        task = 'normal-build-task-687-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'result': 'Referenced PR #687 in passing. Done.',
        })
        merged_prs = [{
            'headRefName': 'forge/some-feature-687', 'number': 687,
            'title': 'feat: the work', '_repo': self._AGENT_CORE,
        }]
        self.assertIsNone(
            self.hps._forge_rebase_target_shipped(task, merged_prs))

    def test_rebase_target_disambiguated_by_task_id_pr_number(self) -> None:
        """B1' (residual production case, 2026-06-25): the ORIGINAL task
        `rebase-forge-post-open-mergeable-687-001` rebased PR #687, but its
        archived result ALSO narrates its now-merged blocker #685 ('Its blocker
        (#685...) had merged'). Both #685 and #687 are in the merged union, so
        seen_numbers == {685, 687} (ambiguous). The task_id's own digit-groups
        {687, 1} disambiguate to the single target #687 — the helper returns the
        #687 PR dict and the stall is suppressed."""
        task = 'rebase-forge-post-open-mergeable-687-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'result': ('PR #687 is now `MERGEABLE / CLEAN`. Its blocker '
                       '(#685, the upstream fix) had merged, unblocking the '
                       'rebase. Done. PR #687 ready for Mirror.'),
        })
        merged_prs = [
            {'headRefName': 'forge/the-blocker-685', 'number': 685,
             'title': 'fix: upstream blocker', '_repo': self._AGENT_CORE},
            {'headRefName': 'forge/some-feature-687', 'number': 687,
             'title': 'feat: the rebased work', '_repo': self._AGENT_CORE},
        ]
        target = self.hps._forge_rebase_target_shipped(task, merged_prs)
        self.assertIsNotNone(target)
        self.assertEqual(target.get('number'), 687)
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO': captured.append(msg)):
            alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(alerts, [])
        skip = [m for m in captured
                if 'FORGE_NO_PR_SKIP' in m and 'rebase_target_shipped' in m]
        self.assertEqual(len(skip), 1)
        self.assertIn('pr=#687', skip[0])

    def test_rebase_ambiguous_no_task_id_match_still_alerts(self) -> None:
        """NEGATIVE fail-safe: the result names two PRs (#900, #901) both in the
        union, but NEITHER matches a task_id digit-group ({555, 1}). The
        intersection is empty, so the helper cannot disambiguate — it returns
        None and the stall still fires. Disambiguation can only REMOVE false
        stalls, never mask a real one."""
        task = 'rebase-forge-something-555-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'result': 'Rebased atop #900; superseded by #901. Done.',
        })
        merged_prs = [
            {'headRefName': 'forge/a-900', 'number': 900,
             'title': 'feat: a', '_repo': self._AGENT_CORE},
            {'headRefName': 'forge/b-901', 'number': 901,
             'title': 'feat: b', '_repo': self._AGENT_CORE},
        ]
        self.assertIsNone(
            self.hps._forge_rebase_target_shipped(task, merged_prs))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'],
                         f'pipeline-stall:forge-no-pr:{task}')

    def test_rebase_ambiguous_task_id_matches_two_still_alerts(self) -> None:
        """NEGATIVE fail-safe: the result names two PRs (#687, #1) both in the
        union AND both present as task_id digit-groups ({687, 1}). The
        intersection still has two members, so the helper cannot reduce to a
        single target — it returns None and the stall still fires."""
        task = 'rebase-forge-post-open-mergeable-687-001'
        self._write_archive(task, {
            'task_id': task, 'phase': 'build', 'exit_code': 0,
            'result': 'Rebased PR #687 over PR #1. Done.',
        })
        merged_prs = [
            {'headRefName': 'forge/the-root-1', 'number': 1,
             'title': 'feat: root', '_repo': self._AGENT_CORE},
            {'headRefName': 'forge/some-feature-687', 'number': 687,
             'title': 'feat: the rebased work', '_repo': self._AGENT_CORE},
        ]
        self.assertIsNone(
            self.hps._forge_rebase_target_shipped(task, merged_prs))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], merged_prs, {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'],
                         f'pipeline-stall:forge-no-pr:{task}')

    def test_step5_does_not_apply_to_non_preflight_errored_task(self) -> None:
        """Gating guard: a genuine build-phase crash (phase='build',
        exit_code=1) on a task_id WITHOUT `-preflight`/`-clarify` is not a
        Step 5 candidate — it still fires the original build-gap alert.
        This is the same shape as
        `test_alerts_on_build_phase_crash_no_preflight_skip`, asserted here
        from the Step 5 angle: the preflight/clarify gate is what keeps the
        regression green."""
        task = 'real-build-phase-crash-002'
        self._write_archive(task, {
            'task_id': task,
            'phase': 'build',
            'exit_code': 1,
            'attempts': 3,
            'error': 'Forge crashed during gh pr create; no output.',
        })
        self.assertFalse(self.hps._is_preflight_or_clarify_task(task))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]['subject'], f'pipeline-stall:forge-no-pr:{task}')

    def test_step5_skips_clean_preflight_exit(self) -> None:
        """A clean preflight (exit_code=0, no error) is handled by Step 2
        (`_forge_preflight_non_proceed` -> PREFLIGHT_EXIT) and must never
        reach Step 5; `_forge_pr_create_inferred_failure` returns None on a
        clean archive."""
        task = 'clean-preflight-preflight-20260604T010000Z-clarify1'
        self._write_archive(task, {
            'task_id': task,
            'phase': 'preflight',
            'exit_code': 0,
            'attempts': 1,
            'result': 'CLARIFY emitted; awaiting Beacon.',
        })
        self.assertIsNone(self.hps._forge_pr_create_inferred_failure(task))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])

    def _write_retry_archive(self, task: str, idx: int, payload: dict) -> None:
        archive_dir = self.agents_root / 'outboxes' / 'forge' / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f'{task}.{idx}.json').write_text(json.dumps(payload))

    def test_step5_suppressed_when_retry_sibling_opened_pr(self) -> None:
        """Genuine recovery: the first-attempt outbox errored (exit_code=-1,
        'All retries exhausted') but a marker-error retry sibling exited cleanly
        AND opened a PR (a `PR opened:` preamble in its result). Step 5 must
        consult the retry sibling and SKIP (reason=retry_recovered) instead of
        firing `pr-create-inferred-failure` — even with NO PR in the live view
        (the aged-out window case the sibling-outbox keying exists for)."""
        task = 'forge-queue-api-preflight-20260603T231401Z-clarify1'
        self._write_archive(task, {
            'task_id': task,
            'phase': 'preflight',
            'exit_code': -1,
            'error': 'All retries exhausted',
            'result': 'All retries exhausted',
        })
        self._write_retry_archive(task, 1, {
            'task_id': f'{task}.1',
            'phase': 'preflight',
            'exit_code': 0,
            'attempts': 1,
            'result': 'PR opened: https://github.com/Larry-Yatch/ourliberty-agent-core/pull/294',
        })
        self.assertEqual(
            self.hps._forge_retry_succeeded(task), f'{task}.1.json')
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        captured: list[str] = []
        with patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO': captured.append(msg)):
            alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(alerts, [])
        skips = [m for m in captured if 'reason=retry_recovered' in m]
        self.assertEqual(len(skips), 1)
        self.assertIn(f'{task}.1.json', skips[0])

    def test_step5_fires_when_retry_sibling_proceed_but_no_pr(self) -> None:
        """The false-positive guard: a retry sibling that exited cleanly with
        `=== PROCEED ===` only proves the clarify resolved and a SEPARATE
        downstream build was dispatched — NOT that any PR was opened. If that
        build then crashed without a PR (none in the live view), Step 5 must
        still fire `pr-create-inferred-failure` rather than silently treating
        PROCEED as recovery. The clarify-then-shipped-build case is handled
        upstream by Step 1b's family-PR cross-check, not by this PROCEED text."""
        task = 'forge-queue-api-preflight-20260603T231401Z-clarify1'
        self._write_archive(task, {
            'task_id': task, 'phase': 'preflight',
            'exit_code': -1, 'error': 'All retries exhausted',
        })
        self._write_retry_archive(task, 1, {
            'task_id': f'{task}.1', 'phase': 'preflight',
            'exit_code': 0, 'attempts': 1,
            'result': '=== PROCEED ===',  # clarify resolved, NO PR opened here
        })
        self.assertIsNone(self.hps._forge_retry_succeeded(task))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]['subject'],
            f'pipeline-stall:pr-create-inferred-failure:{task}')

    def test_step5_still_fires_when_retry_sibling_also_errored(self) -> None:
        """Guard: a retry sibling that ALSO errored is not a recovery — Step
        5 still surfaces the genuine infra/auth loss once."""
        task = 'forge-x-preflight-20260604T010000Z-clarify1'
        self._write_archive(task, {
            'task_id': task, 'phase': 'preflight',
            'exit_code': -1, 'error': 'All retries exhausted',
        })
        self._write_retry_archive(task, 1, {
            'task_id': f'{task}.1', 'phase': 'preflight',
            'exit_code': -1, 'error': 'All retries exhausted',
        })
        self.assertIsNone(self.hps._forge_retry_succeeded(task))
        lines = [_watcher_forge_done_line(task, minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]['subject'],
            f'pipeline-stall:pr-create-inferred-failure:{task}')

    def test_forge_retry_succeeded_none_when_no_siblings(self) -> None:
        """Only the first-attempt outbox exists (no `.N.json` retry) -> None.
        The glob must not match the base `<task>.json` itself."""
        task = 'forge-y-preflight-20260604T020000Z-clarify1'
        self._write_archive(task, {
            'task_id': task, 'phase': 'preflight',
            'exit_code': -1, 'error': 'All retries exhausted',
        })
        self.assertIsNone(self.hps._forge_retry_succeeded(task))


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

    def test_skips_deep_review_held_pr(self) -> None:
        """merge-gate-deep-review-hold: a PASS'd PR that the notifier
        INTENTIONALLY held for a human /code-review high must NOT be flagged
        as a stall — and (critically) must not be auto-merged out from under
        the review by this check's recovery. The held PR is mergeable, so
        without an exemption the recovery `gh pr merge` would succeed and
        defeat the gate."""
        lines = [
            f'[{_ts(45)}] [notifier] [INFO] marker-notified beacon <- mirror '
            f'(mirror-result, intent=review-pass, file=notify-crit-hold-001.json)',
            f'[{_ts(44)}] [notifier] [WARN] AUTO_MERGE_HELD_DEEP_REVIEW '
            f'task=crit-hold-001 pr=https://github.com/o/r/pull/303 '
            f'(critical-path change with no deep-review stamp; held for '
            f'/code-review high) agent=forge',
        ]
        pr = {
            'number': 303, 'headRefName': 'forge/crit-hold-001',
            'title': 'harden trust policy',
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
        # M4 recover-then-alert: Check 3 now attempts an auto-merge recovery
        # FIRST. Force that recovery to fail so the alert path (the behavior
        # this test asserts) is exercised. Without this stub the mocked
        # subprocess.run (returncode=0) would make merge_pr succeed and the
        # alert would be (correctly) suppressed.
        with patch.object(self.hps, '_all_open_prs', return_value=[pr]), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=lines), \
             patch.object(self.hps, '_read_recent_routing_events', return_value=routing_events), \
             patch.object(self.hps, '_recover_via_auto_merge', return_value=False), \
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

    def test_dry_run_is_a_true_no_op(self) -> None:
        """`run(dry_run=True)` detects the stall but performs ZERO
        side-effecting writes: no `larry_alerts.append_alert`, and the
        recovery primitive (`_recover_via_auto_merge`, which would run
        `gh pr merge` on a live PR) is never invoked. The companion
        `dry_run=False` block proves the SAME fixture genuinely triggers a
        stall — otherwise the no-op assertion would be vacuously true."""
        # Same Check-3 (Mirror-PASS-unmerged) fixture as
        # test_fires_alert_on_real_stall: a PR old enough to stall, with a
        # review-request line (satisfies Check 2), a review-pass marker
        # (trips Check 3), and a routing event (satisfies Check 7).
        pr = {
            'number': 999, 'headRefName': 'forge/end2end-stuck-001', 'title': 'fix: x',
            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        lines = [
            f'[{_ts(90)}] [notifier] [INFO] review-request dispatched mirror <- beacon (task=end2end-stuck-001, file=review-x.json, pr=https://example/999)',
            f'[{_ts(60)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-end2end-stuck-001.json)',
        ]
        routing_events = [{
            'source_agent': 'beacon',
            'target_agent_final': 'mirror',
            'phase': 'review',
            'task_id': 'end2end-stuck-001',
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }]

        # --- dry-run: no alert, no recovery dispatch ---
        with patch.object(self.hps, '_all_open_prs', return_value=[pr]), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=lines), \
             patch.object(self.hps, '_read_recent_routing_events', return_value=routing_events), \
             patch.object(self.hps, 'heartbeat') as mock_heartbeat, \
             patch.object(self.hps, 'save_state') as mock_save_state, \
             patch.object(self.hps, '_recover_via_auto_merge') as mock_recover, \
             patch('subprocess.run') as mock_sub, \
             patch.object(self.hps.larry_alerts, 'append_alert', return_value=True) as mock_alert:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            rc = self.hps.run(dry_run=True)
        self.assertEqual(rc, 0)
        mock_alert.assert_not_called()
        mock_recover.assert_not_called()
        mock_heartbeat.assert_not_called()
        mock_save_state.assert_not_called()

        # --- honesty check: SAME fixture, real run, DOES alert ---
        with patch.object(self.hps, '_all_open_prs', return_value=[pr]), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=lines), \
             patch.object(self.hps, '_read_recent_routing_events', return_value=routing_events), \
             patch.object(self.hps, '_recover_via_auto_merge', return_value=False), \
             patch('subprocess.run') as mock_sub, \
             patch.object(self.hps.larry_alerts, 'append_alert', return_value=True) as mock_alert:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            self.hps.run(dry_run=False)
        self.assertEqual(mock_alert.call_count, 1)

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
        # M4: both Check-3 stalls attempt auto-merge recovery first; force
        # both to fail so each falls through to its own DM (the per-key dedup
        # contract this test verifies).
        with patch.object(self.hps, '_all_open_prs', return_value=prs), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=lines), \
             patch.object(self.hps, '_read_recent_routing_events', return_value=routing_events), \
             patch.object(self.hps, '_recover_via_auto_merge', return_value=False), \
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


class TestRecoverableVsDetectiveClassification(_TempAgentsRootMixin,
                                                unittest.TestCase):
    """M4: recoverable checks attach a `recovery` callable to their alert
    dicts; detective-only checks do not. This is the static contract the
    run() loop relies on to decide recover-then-alert vs alert-only."""

    def test_mirror_pass_unmerged_alert_is_recoverable(self) -> None:
        lines = [f'[{_ts(45)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-pass-stuck-001.json)']
        pr = {'number': 300, 'headRefName': 'forge/pass-stuck-001',
              'title': 'fix: x', '_repo': 'Larry-Yatch/ourliberty-agent-core'}
        alerts = self.hps.check_mirror_pass_unmerged(lines, [pr], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('recovery', alerts[0])
        self.assertTrue(callable(alerts[0]['recovery']))

    def test_pr_no_mirror_dispatch_alert_is_recoverable(self) -> None:
        pr = {'number': 310, 'headRefName': 'forge/no-dispatch-001',
              'title': 'docs: x', '_repo': 'Larry-Yatch/ourliberty-agent-core',
              'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()}
        alerts = self.hps.check_pr_no_mirror_dispatch([], [pr], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('recovery', alerts[0])
        self.assertTrue(callable(alerts[0]['recovery']))

    def test_marker_invisible_alert_is_detective_only(self) -> None:
        lines = [f'[{_ts(45)}] [notifier] [INFO] notified beacon <- mirror (mirror-result, depth=1, file=notify-shape-drift-001.json)']
        alerts = self.hps.check_mirror_marker_invisible(lines, {})
        self.assertEqual(len(alerts), 1)
        self.assertNotIn('recovery', alerts[0])


class TestRecoverThenAlert(_TempAgentsRootMixin, unittest.TestCase):
    """M4 end-to-end: a recoverable stall attempts recovery before any alert;
    a failed recovery alerts exactly once; a successful recovery suppresses
    the alert and stamps the per-key cooldown so the next tick does not
    re-attempt. Vehicle: Check 3 (Mirror-PASS-unmerged) → auto-merge."""

    _PR = {
        'number': 999, 'headRefName': 'forge/recover-001', 'title': 'fix: x',
        '_repo': 'Larry-Yatch/ourliberty-agent-core',
    }
    _KEY = 'mirror_pass_unmerged:recover-001'
    _LINES = [
        # Satisfy Check 2 (review dispatched) so only Check 3 trips.
        f'[{_ts(90)}] [notifier] [INFO] review-request dispatched mirror <- beacon (task=recover-001, file=r.json, pr=https://example/999)',
        # Trigger Check 3.
        f'[{_ts(60)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-recover-001.json)',
    ]

    def _io_patches(self):
        """The shared I/O stubs (one fresh set per call)."""
        routing = [{'source_agent': 'beacon', 'target_agent_final': 'mirror',
                    'phase': 'review', 'task_id': 'recover-001',
                    'timestamp': datetime.now(timezone.utc).isoformat()}]
        return contextlib.ExitStack(), [
            patch.object(self.hps, '_all_open_prs', return_value=[self._PR]),
            patch.object(self.hps, '_all_merged_prs_recent', return_value=[]),
            patch.object(self.hps, '_read_recent_log_lines', return_value=list(self._LINES)),
            patch.object(self.hps, '_read_recent_routing_events', return_value=routing),
        ]

    def test_recovery_attempted_before_alert_and_suppresses_it(self) -> None:
        stack, io = self._io_patches()
        with stack:
            for p in io:
                stack.enter_context(p)
            mock_recover = stack.enter_context(
                patch.object(self.hps, '_recover_via_auto_merge', return_value=True))
            mock_sub = stack.enter_context(patch('subprocess.run'))
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            mock_alert = stack.enter_context(
                patch.object(self.hps.larry_alerts, 'append_alert', return_value=True))
            self.hps.run()
        # Recovery ran with the right PR coordinates …
        mock_recover.assert_called_once()
        args = mock_recover.call_args.args
        self.assertEqual(args[0], 'Larry-Yatch/ourliberty-agent-core')
        self.assertEqual(args[1], 999)
        # … and because it succeeded, NO Larry DM fired.
        mock_alert.assert_not_called()
        # The per-key cooldown was stamped so the next tick won't re-attempt.
        self.assertIn(self._KEY, self.hps.load_state())

    def test_failed_recovery_alerts_exactly_once_with_note(self) -> None:
        stack, io = self._io_patches()
        with stack:
            for p in io:
                stack.enter_context(p)
            mock_recover = stack.enter_context(
                patch.object(self.hps, '_recover_via_auto_merge', return_value=False))
            mock_sub = stack.enter_context(patch('subprocess.run'))
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            mock_alert = stack.enter_context(
                patch.object(self.hps.larry_alerts, 'append_alert', return_value=True))
            self.hps.run()
        mock_recover.assert_called_once()
        self.assertEqual(mock_alert.call_count, 1)
        msg = mock_alert.call_args.kwargs['message']
        self.assertIn('PR #999', msg)
        self.assertIn('attempted auto-recovery', msg)

    def test_recovery_exception_falls_through_to_single_alert(self) -> None:
        stack, io = self._io_patches()
        with stack:
            for p in io:
                stack.enter_context(p)
            stack.enter_context(
                patch.object(self.hps, '_recover_via_auto_merge',
                             side_effect=RuntimeError('boom')))
            mock_sub = stack.enter_context(patch('subprocess.run'))
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            mock_alert = stack.enter_context(
                patch.object(self.hps.larry_alerts, 'append_alert', return_value=True))
            self.hps.run()
        # A raising recovery is treated as failed — exactly one alert, no crash.
        self.assertEqual(mock_alert.call_count, 1)

    def test_no_double_alert_or_reattempt_after_success(self) -> None:
        stack, io = self._io_patches()
        with stack:
            for p in io:
                stack.enter_context(p)
            mock_recover = stack.enter_context(
                patch.object(self.hps, '_recover_via_auto_merge', return_value=True))
            mock_sub = stack.enter_context(patch('subprocess.run'))
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            mock_alert = stack.enter_context(
                patch.object(self.hps.larry_alerts, 'append_alert', return_value=True))
            self.hps.run()  # tick 1: recovers + stamps cooldown
            self.hps.run()  # tick 2: cooldown gate → no recovery, no alert
        mock_recover.assert_called_once()  # not re-attempted within cooldown
        mock_alert.assert_not_called()


class TestCheckRevisionDispatchedWithNoSession(_TempAgentsRootMixin, unittest.TestCase):
    """forge-cold-start-revision S3. The session-less REVISION backstop now
    reads the durable obligation ledger (no_session_ledger): a cold-start
    revision OPENS an obligation, a Mirror PASS / merge RESOLVES it, and an
    obligation still OPEN past NO_SESSION_STUCK_MIN fires a recover-then-alert.
    """

    def setUp(self):
        super().setUp()
        # Sandbox the ledger to this test's root (the mixin re-imports
        # heal_pipeline_stall but no_session_ledger keeps its import-time path).
        self._nsl = self.hps.no_session_ledger
        self._nsl_orig = self._nsl.LEDGER_FILE
        self._nsl.LEDGER_FILE = self.agents_root / 'state' / 'ns-ledger.json'

    def tearDown(self):
        self._nsl.LEDGER_FILE = self._nsl_orig
        super().tearDown()

    def _open(self, task, minutes_ago=60, pr_url='https://gh/o/r/pull/7', **kw):
        when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        self._nsl.open_obligation(task, pr_url=pr_url, now=when, **kw)

    def test_stuck_obligation_fires_alert(self):
        self._open('stuck-1', minutes_ago=60)
        alerts = self.hps.check_revision_dispatched_with_no_session({})
        self.assertEqual(len(alerts), 1)
        self.assertIn('stuck-1', alerts[0]['message'])
        self.assertEqual(
            alerts[0]['subject'],
            'pipeline-stall:no-session-revision:stuck-1',
        )
        self.assertIn('https://gh/o/r/pull/7', alerts[0]['message'])
        self.assertIn('recovery', alerts[0])

    def test_fresh_obligation_within_grace_no_alert(self):
        self._open('fresh-1', minutes_ago=5)  # < NO_SESSION_STUCK_MIN
        alerts = self.hps.check_revision_dispatched_with_no_session({})
        self.assertEqual(alerts, [])

    def test_human_authored_branch_suppresses_page(self):
        # A hand-opened PR (feat/* via the auto-review handoff) is session-less
        # BY DESIGN — Forge never built it, so the cold-start re-brief is the
        # expected path, not a stall. The stuck obligation must NOT page, but
        # the verify-only recovery still runs so the row can clear on merge.
        self._open('human-1', minutes_ago=90,
                   branch='feat/tier2-parity-monitor-and-audit')
        with patch.object(self.hps, '_gh_pr_state', return_value='OPEN') as gh:
            alerts = self.hps.check_revision_dispatched_with_no_session({})
        self.assertEqual(alerts, [])           # no loud page for the human case
        gh.assert_called()                     # recovery still verified PR state
        # PR still open → obligation stays OPEN for a later merge/PASS resolve.
        self.assertEqual(
            self._nsl.get_obligation('human-1')['status'], self._nsl.OPEN,
        )

    def test_human_branch_recovery_resolves_on_merge(self):
        # Same human branch, but the PR merged out-of-band: the inline
        # verify-only recovery clears the ledger row (no lingering obligation).
        self._open('human-merged-1', minutes_ago=90, branch='fix/manual-doc')
        with patch.object(self.hps, '_gh_pr_state', return_value='MERGED'):
            alerts = self.hps.check_revision_dispatched_with_no_session({})
        self.assertEqual(alerts, [])
        self.assertEqual(
            self._nsl.get_obligation('human-merged-1')['status'],
            self._nsl.RESOLVED,
        )

    def test_forge_branch_still_fires(self):
        # A `forge/` branch DID have a build session; a session-less cold start
        # on it is the genuine #412 regression and must still page loudly.
        self._open('forge-lost-1', minutes_ago=60,
                   branch='forge/some-build-task-001')
        alerts = self.hps.check_revision_dispatched_with_no_session({})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]['subject'],
            'pipeline-stall:no-session-revision:forge-lost-1',
        )

    def test_larry_branch_suppresses_page(self):
        # A `larry/` branch is hand-authored, NOT built by the Forge build path
        # (an extractable task_id != a resumable build session). A session-less
        # cold start on it is the expected re-brief, not the #412 regression —
        # it must NOT page, but the verify-only recovery still runs.
        self._open('larry-1', minutes_ago=90, branch='larry/some-hand-task')
        with patch.object(self.hps, '_gh_pr_state', return_value='OPEN') as gh:
            alerts = self.hps.check_revision_dispatched_with_no_session({})
        self.assertEqual(alerts, [])           # no loud page for the human case
        gh.assert_called()                     # recovery still verified PR state
        self.assertEqual(
            self._nsl.get_obligation('larry-1')['status'], self._nsl.OPEN,
        )

    def test_claude_branch_suppresses_page(self):
        # A `claude/` branch is laptop-authored, NOT built by the Forge build
        # path — same reasoning as larry/: suppress the page, run recovery only.
        self._open('claude-1', minutes_ago=90, branch='claude/laptop-task')
        with patch.object(self.hps, '_gh_pr_state', return_value='OPEN') as gh:
            alerts = self.hps.check_revision_dispatched_with_no_session({})
        self.assertEqual(alerts, [])           # no loud page for the laptop case
        gh.assert_called()                     # recovery still verified PR state
        self.assertEqual(
            self._nsl.get_obligation('claude-1')['status'], self._nsl.OPEN,
        )

    def test_resolved_obligation_no_alert(self):
        self._open('done-1', minutes_ago=60)
        self._nsl.resolve_obligation('done-1', resolution='review-pass')
        alerts = self.hps.check_revision_dispatched_with_no_session({})
        self.assertEqual(alerts, [])

    def test_one_alert_per_task(self):
        # The ledger holds one row per task_id, so a task yields exactly one
        # alert even after multiple cold-start rounds re-open it.
        self._open('dup-task', minutes_ago=90, round_num=1)
        self._open('dup-task', minutes_ago=60, round_num=2)
        alerts = self.hps.check_revision_dispatched_with_no_session({})
        self.assertEqual(len(alerts), 1)
        self.assertIn('round 2', alerts[0]['message'])

    def test_silent_when_ledger_empty(self):
        alerts = self.hps.check_revision_dispatched_with_no_session({})
        self.assertEqual(alerts, [])

    def test_recovery_resolves_on_merged_pr(self):
        # recover-then-alert: a stuck obligation whose PR merged out-of-band is
        # cleared and reported recovered (the framework suppresses the alert).
        self._open('merged-1', minutes_ago=60)
        with patch.object(self.hps, '_gh_pr_state', return_value='MERGED'):
            recovered = self.hps._recover_no_session_revision(
                'merged-1', 'https://gh/o/r/pull/7')
        self.assertTrue(recovered)
        ob = self._nsl.get_obligation('merged-1')
        self.assertEqual(ob['status'], self._nsl.RESOLVED)
        self.assertEqual(ob['resolution'], 'merged')

    def test_recovery_false_when_pr_confirmed_open(self):
        self._open('open-1', minutes_ago=60)
        with patch.object(self.hps, '_gh_pr_state', return_value='OPEN'):
            recovered = self.hps._recover_no_session_revision(
                'open-1', 'https://gh/o/r/pull/7')
        self.assertFalse(recovered)  # confirmed OPEN → stuck → loud alert fires
        self.assertEqual(
            self._nsl.get_obligation('open-1')['status'], self._nsl.OPEN,
        )

    def test_recovery_defers_when_gh_unreachable(self):
        # verify-before-alarm: gh unreachable (state None) must NOT fire a
        # false alert — suppress this round, leave the obligation OPEN to retry.
        self._open('unknown-1', minutes_ago=60)
        with patch.object(self.hps, '_gh_pr_state', return_value=None):
            recovered = self.hps._recover_no_session_revision(
                'unknown-1', 'https://gh/o/r/pull/7')
        self.assertTrue(recovered)  # suppress (defer), not alert
        self.assertEqual(
            self._nsl.get_obligation('unknown-1')['status'], self._nsl.OPEN,
        )


class TestCheckRebaseObligationStuck(_TempAgentsRootMixin, unittest.TestCase):
    """forge-post-open-mergeable-rebase-001 Check 10. The post-open auto-rebase
    backstop reads the durable obligation ledger (rebase_obligation_ledger): the
    notifier OPENS an obligation when it dispatches a phase=rebase to Forge (PR
    opened CONFLICTING because main advanced) and RESOLVES it when the rebased PR
    comes back MERGEABLE and Mirror is dispatched. An obligation still OPEN past
    REBASE_STUCK_MIN fires a recover-then-alert. Mirrors the sibling Check 6
    coverage (TestCheckRevisionDispatchedWithNoSession).
    """

    def setUp(self):
        super().setUp()
        # Sandbox the ledger to this test's root (the mixin re-imports
        # heal_pipeline_stall but rebase_obligation_ledger keeps its import-time
        # path).
        self._rol = self.hps.rebase_obligation_ledger
        self._rol_orig = self._rol.LEDGER_FILE
        self._rol.LEDGER_FILE = self.agents_root / 'state' / 'rebase-ledger.json'

    def tearDown(self):
        self._rol.LEDGER_FILE = self._rol_orig
        super().tearDown()

    def _open(self, task, minutes_ago=60, pr_url='https://gh/o/r/pull/7', **kw):
        when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        self._rol.open_obligation(task, pr_url=pr_url, now=when, **kw)

    def test_stuck_obligation_fires_alert(self):
        self._open('stuck-1', minutes_ago=60)
        alerts = self.hps.check_rebase_obligation_stuck({})
        self.assertEqual(len(alerts), 1)
        self.assertIn('stuck-1', alerts[0]['message'])
        self.assertEqual(
            alerts[0]['subject'],
            'pipeline-stall:rebase-obligation:stuck-1',
        )
        self.assertIn('https://gh/o/r/pull/7', alerts[0]['message'])
        self.assertIn('recovery', alerts[0])

    def test_fresh_obligation_within_grace_no_alert(self):
        self._open('fresh-1', minutes_ago=5)  # < REBASE_STUCK_MIN
        alerts = self.hps.check_rebase_obligation_stuck({})
        self.assertEqual(alerts, [])

    def test_resolved_obligation_no_alert(self):
        self._open('done-1', minutes_ago=60)
        self._rol.resolve_obligation('done-1', resolution='mergeable')
        alerts = self.hps.check_rebase_obligation_stuck({})
        self.assertEqual(alerts, [])

    def test_one_alert_per_task(self):
        # The ledger holds one row per task_id, so a task yields exactly one
        # alert even after multiple rebase rounds re-open it.
        self._open('dup-task', minutes_ago=90, round_num=1)
        self._open('dup-task', minutes_ago=60, round_num=2)
        alerts = self.hps.check_rebase_obligation_stuck({})
        self.assertEqual(len(alerts), 1)
        self.assertIn('round 2', alerts[0]['message'])

    def test_silent_when_ledger_empty(self):
        alerts = self.hps.check_rebase_obligation_stuck({})
        self.assertEqual(alerts, [])

    def test_recovery_resolves_on_merged_pr(self):
        # recover-then-alert: a stuck obligation whose PR merged out-of-band is
        # cleared and reported recovered (the framework suppresses the alert).
        self._open('merged-1', minutes_ago=60)
        with patch.object(self.hps, '_gh_pr_state', return_value='MERGED'):
            recovered = self.hps._recover_rebase_obligation(
                'merged-1', 'https://gh/o/r/pull/7')
        self.assertTrue(recovered)
        ob = self._rol.get_obligation('merged-1')
        self.assertEqual(ob['status'], self._rol.RESOLVED)
        self.assertEqual(ob['resolution'], 'merged')

    def test_recovery_resolves_on_closed_pr(self):
        # A PR closed out-of-band (e.g. superseded) also clears the obligation.
        self._open('closed-1', minutes_ago=60)
        with patch.object(self.hps, '_gh_pr_state', return_value='CLOSED'):
            recovered = self.hps._recover_rebase_obligation(
                'closed-1', 'https://gh/o/r/pull/7')
        self.assertTrue(recovered)
        ob = self._rol.get_obligation('closed-1')
        self.assertEqual(ob['status'], self._rol.RESOLVED)
        self.assertEqual(ob['resolution'], 'closed')

    def test_recovery_false_when_pr_confirmed_open(self):
        self._open('open-1', minutes_ago=60)
        with patch.object(self.hps, '_gh_pr_state', return_value='OPEN'):
            recovered = self.hps._recover_rebase_obligation(
                'open-1', 'https://gh/o/r/pull/7')
        self.assertFalse(recovered)  # confirmed OPEN → stuck → loud alert fires
        self.assertEqual(
            self._rol.get_obligation('open-1')['status'], self._rol.OPEN,
        )

    def test_recovery_defers_when_gh_unreachable(self):
        # verify-before-alarm: gh unreachable (state None) must NOT fire a
        # false alert — suppress this round, leave the obligation OPEN to retry.
        self._open('unknown-1', minutes_ago=60)
        with patch.object(self.hps, '_gh_pr_state', return_value=None):
            recovered = self.hps._recover_rebase_obligation(
                'unknown-1', 'https://gh/o/r/pull/7')
        self.assertTrue(recovered)  # suppress (defer), not alert
        self.assertEqual(
            self._rol.get_obligation('unknown-1')['status'], self._rol.OPEN,
        )

    def test_recovery_defers_when_pr_url_empty(self):
        # No pr_url to verify against → defer (suppress) rather than false-alert;
        # the obligation stays OPEN and re-checks next tick. _gh_pr_state must
        # not even be consulted.
        self._open('nopr-1', minutes_ago=60, pr_url='')
        with patch.object(self.hps, '_gh_pr_state') as mock_state:
            recovered = self.hps._recover_rebase_obligation('nopr-1', '')
        self.assertTrue(recovered)
        mock_state.assert_not_called()
        self.assertEqual(
            self._rol.get_obligation('nopr-1')['status'], self._rol.OPEN,
        )


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

    def test_silent_for_new_mission_pr(self):
        # Dashboard "+New mission" PRs are reconciled into missions.json + closed
        # by heal_orphan_autoregister, not Mirror-reviewed — so they must NOT page
        # here even with no routing event and well past the min age.
        prs = [self._make_pr(
            513, 'feat/new-mission-pulse-check-ix-alert-ignored-2026-06-15',
            age_min=600)]
        alerts = self.hps.check_unrouted_open_prs(prs, [], {})
        self.assertEqual(alerts, [])

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

    # ---- Active-Mirror suppression (G-rule unrouted-open-pr-active-mirror-
    # session-fp-001). The alert must not fire while a Mirror review session is
    # live (a `wt-mirror-pr-<repo>-<num>` claude proc) or a review task is
    # freshly dispatched (a `review-pr-<repo>-<num>*.json` inbox file). ----

    def _mirror_cwd(self, name: str) -> str:
        return str(self.hps.WORKTREES_ROOT / name)

    def test_suppressed_when_live_mirror_proc_for_pr(self):
        # PR #711: a live `claude` proc cwd'd in its review worktree → skip.
        prs = [self._make_pr(711, 'larry/manual-pr-001', age_min=180)]
        cwd = self._mirror_cwd('wt-mirror-pr-ourliberty-agent-core-711')
        # The probe moved to pipeline_live_state (#716 de-dup); patch it there.
        with patch('pipeline_live_state._claude_pids', return_value=[4242]), \
                patch('pipeline_live_state._proc_cwd', return_value=cwd):
            alerts = self.hps.check_unrouted_open_prs(prs, [], {})
        self.assertEqual(alerts, [])

    def test_suppressed_when_live_mirror_proc_revision_suffix(self):
        # A `-rev1` review worktree still suppresses; the kernel `(deleted)`
        # suffix on a torn-down cwd is tolerated too.
        prs = [self._make_pr(713, 'larry/manual-pr-002', age_min=180)]
        cwd = self._mirror_cwd(
            'wt-mirror-pr-ourliberty-agent-core-713-rev2') + ' (deleted)'
        with patch('pipeline_live_state._claude_pids', return_value=[99]), \
                patch('pipeline_live_state._proc_cwd', return_value=cwd):
            alerts = self.hps.check_unrouted_open_prs(prs, [], {})
        self.assertEqual(alerts, [])

    def test_not_suppressed_when_proc_pr_number_is_boundary_mismatch(self):
        # A live #713 review proc must NOT suppress the #13 PR (and there's no
        # inbox file), so the alert still fires. Boundary discipline.
        prs = [self._make_pr(13, 'larry/manual-pr-003', age_min=180)]
        cwd = self._mirror_cwd('wt-mirror-pr-ourliberty-agent-core-713')
        with patch('pipeline_live_state._claude_pids', return_value=[7]), \
                patch('pipeline_live_state._proc_cwd', return_value=cwd):
            alerts = self.hps.check_unrouted_open_prs(prs, [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('PR #13', alerts[0]['message'])

    def test_suppressed_when_review_inbox_task_present(self):
        # No live proc, but a dispatched review task file sits in Mirror's
        # inbox → still in flight, skip.
        mirror_inbox = self.agents_root / 'inboxes' / 'mirror'
        mirror_inbox.mkdir(parents=True)
        (mirror_inbox / 'review-pr-ourliberty-agent-core-711.json').write_text(
            '{}')
        prs = [self._make_pr(711, 'larry/manual-pr-004', age_min=180)]
        with patch('pipeline_live_state._claude_pids', return_value=[]):
            alerts = self.hps.check_unrouted_open_prs(prs, [], {})
        self.assertEqual(alerts, [])

    def test_inbox_task_boundary_does_not_oversuppress(self):
        # A `review-pr-...-7130.json` task must NOT suppress the #713 PR.
        mirror_inbox = self.agents_root / 'inboxes' / 'mirror'
        mirror_inbox.mkdir(parents=True)
        (mirror_inbox / 'review-pr-ourliberty-agent-core-7130.json').write_text(
            '{}')
        prs = [self._make_pr(713, 'larry/manual-pr-005', age_min=180)]
        with patch('pipeline_live_state._claude_pids', return_value=[]):
            alerts = self.hps.check_unrouted_open_prs(prs, [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('PR #713', alerts[0]['message'])

    def test_fires_when_no_mirror_session_active(self):
        # No live proc, no inbox task → genuinely unrouted, alert fires.
        prs = [self._make_pr(720, 'larry/manual-pr-006', age_min=180)]
        with patch('pipeline_live_state._claude_pids', return_value=[]):
            alerts = self.hps.check_unrouted_open_prs(prs, [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('PR #720', alerts[0]['message'])

    def test_mirror_session_helper_reasons(self):
        # Direct helper coverage: reason strings are the documented forensic
        # identifiers logged as MIRROR_ACTIVE_SKIP reason=<...>.
        cwd = self._mirror_cwd('wt-mirror-pr-ourliberty-agent-core-711')
        with patch('pipeline_live_state._claude_pids', return_value=[1]), \
                patch('pipeline_live_state._proc_cwd', return_value=cwd):
            active, reason = self.hps._mirror_session_active_for_pr(
                'ourliberty-agent-core', 711)
        self.assertTrue(active)
        self.assertEqual(reason, 'live_pid')

        mirror_inbox = self.agents_root / 'inboxes' / 'mirror'
        mirror_inbox.mkdir(parents=True)
        (mirror_inbox / 'review-pr-ourliberty-agent-core-711-rev1.json'
         ).write_text('{}')
        with patch('pipeline_live_state._claude_pids', return_value=[]):
            active, reason = self.hps._mirror_session_active_for_pr(
                'ourliberty-agent-core', 711)
        self.assertTrue(active)
        self.assertEqual(reason, 'inbox_task_present')

        with patch('pipeline_live_state._claude_pids', return_value=[]):
            active, reason = self.hps._mirror_session_active_for_pr(
                'ourliberty-agent-core', 999)
        self.assertFalse(active)
        self.assertEqual(reason, '')


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


class TestMergedPrFetchTruncation(_TempAgentsRootMixin, unittest.TestCase):
    """`_all_merged_prs_recent` must cover the full 7-day window, not just the
    most-recent N merges. Regression guard for the 2026-06-04 limit=30 bug
    where a ~22h-old sibling build PR was truncated off the tail, so
    `_preflight_family_shipped` never saw it."""

    @staticmethod
    def _merged(num: int, minutes_ago: int) -> dict:
        dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return {'number': num, 'title': f'pr {num}', 'state': 'MERGED',
                'mergedAt': dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'headRefName': f'forge/build-x-{num}'}

    def _run(self, prs: list) -> tuple[list, list]:
        captured: list[tuple[str, str]] = []
        with patch.object(self.hps, 'REPOS', ['x/y']), \
             patch.object(self.hps, 'gh_pr_list', return_value=prs), \
             patch.object(self.hps, 'log',
                          side_effect=lambda msg, level='INFO':
                          captured.append((level, msg))):
            out = self.hps._all_merged_prs_recent()
        warns = [m for lvl, m in captured if lvl == 'WARN' and 'cap' in m]
        return out, warns

    def test_warns_when_cap_hit_with_oldest_still_in_window(self) -> None:
        limit = self.hps._MERGED_PR_FETCH_LIMIT
        prs = [self._merged(i, minutes_ago=i % 60) for i in range(limit)]
        out, warns = self._run(prs)
        self.assertEqual(len(out), limit)
        self.assertEqual(len(warns), 1)

    def test_no_warn_when_below_cap(self) -> None:
        out, warns = self._run([self._merged(i, minutes_ago=i) for i in range(10)])
        self.assertEqual(len(out), 10)
        self.assertEqual(warns, [])

    def test_no_warn_when_cap_hit_but_oldest_outside_window(self) -> None:
        limit = self.hps._MERGED_PR_FETCH_LIMIT
        # `limit` results, but the oldest (last, newest-first order) merged 8
        # days ago — the 7-day window is fully covered, so no truncation risk.
        prs = [self._merged(i, minutes_ago=i) for i in range(limit - 1)]
        prs.append(self._merged(9999, minutes_ago=8 * 24 * 60))
        out, warns = self._run(prs)
        self.assertEqual(len(out), limit - 1)  # the 8-day-old PR is filtered out
        self.assertEqual(warns, [])


class TestClosedPrFetchFiltersMerged(_TempAgentsRootMixin, unittest.TestCase):
    """`_all_closed_prs_recent` fetches `--state closed`, which gh treats as a
    superset that INCLUDES merged PRs. The explicit `state == 'CLOSED'` guard
    must drop those merged entries (they're handled by `_all_merged_prs_recent`
    via the step-1 pr_exists path), keeping only genuinely closed-not-merged
    PRs inside the 7-day window."""

    @staticmethod
    def _pr(num: int, state: str, minutes_ago: int) -> dict:
        dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return {'number': num, 'title': f'pr {num}', 'state': state,
                'closedAt': dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'headRefName': f'forge/x-{num}'}

    def _run(self, prs: list) -> list:
        with patch.object(self.hps, 'REPOS', ['x/y']), \
             patch.object(self.hps, 'gh_pr_list', return_value=prs):
            return self.hps._all_closed_prs_recent()

    def test_drops_merged_keeps_closed(self) -> None:
        prs = [self._pr(1, 'CLOSED', minutes_ago=60),
               self._pr(2, 'MERGED', minutes_ago=60),
               self._pr(3, 'CLOSED', minutes_ago=120)]
        out = self._run(prs)
        self.assertEqual({pr['number'] for pr in out}, {1, 3})

    def test_filters_out_stale_closed(self) -> None:
        prs = [self._pr(1, 'CLOSED', minutes_ago=60),
               self._pr(2, 'CLOSED', minutes_ago=8 * 24 * 60)]
        out = self._run(prs)
        self.assertEqual({pr['number'] for pr in out}, {1})

    def test_fail_safe_empty_on_gh_failure(self) -> None:
        with patch.object(self.hps, 'REPOS', ['x/y']), \
             patch.object(self.hps, 'gh_pr_list', return_value=[]):
            self.assertEqual(self.hps._all_closed_prs_recent(), [])


def _iso(minutes_ago: int) -> str:
    """ISO-8601 timestamp `minutes_ago` minutes before now — the shape the
    notifier writes into build-sequence audit_log entries."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.isoformat()


class TestStalledPendingSequence(_TempAgentsRootMixin, unittest.TestCase):
    """Check 9 — sequence stuck pending after an unresolved DAG REVISION."""

    def setUp(self) -> None:
        super().setUp()
        self.seqdir = self.agents_root / 'blackboard' / 'build-sequences'
        self.seqdir.mkdir(parents=True, exist_ok=True)

    def _write_seq(self, seq_id, status='pending', audit=None):
        seq = {
            'seq_id': seq_id,
            'status': status,
            'steps': [],
            'audit_log': audit or [],
        }
        (self.seqdir / f'{seq_id}.json').write_text(json.dumps(seq, indent=2))

    @staticmethod
    def _rev(minutes_ago, task='review-sequence-dag-seq'):
        return {
            'ts': _iso(minutes_ago),
            'event': 'dag-preflight-revision-routed',
            'actor': 'outbox-notifier',
            'mirror_task_id': task,
        }

    @staticmethod
    def _pass(minutes_ago):
        return {
            'ts': _iso(minutes_ago),
            'event': 'dag-preflight-pass-kickoff',
            'actor': 'outbox-notifier',
        }

    def test_fires_for_pending_unresolved_revision_past_threshold(self) -> None:
        self._write_seq('stuck-seq', status='pending', audit=[self._rev(45)])
        alerts = self.hps.check_stalled_pending_sequence({})
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a['subject'], 'stalled-pending-sequence:stuck-seq')
        self.assertIn('stuck-seq', a['message'])
        self.assertTrue(a['key'].startswith('stalled_pending_sequence:stuck-seq:'))

    def test_silent_when_revision_recent(self) -> None:
        # Within the threshold — give Beacon's resume time to land.
        self._write_seq('fresh-seq', status='pending', audit=[self._rev(10)])
        self.assertEqual(self.hps.check_stalled_pending_sequence({}), [])

    def test_silent_when_pass_kickoff_newer_than_revision(self) -> None:
        # REVISION resolved: Mirror PASSed the amended sequence.
        self._write_seq(
            'resolved-seq', status='pending',
            audit=[self._rev(45), self._pass(5)],
        )
        self.assertEqual(self.hps.check_stalled_pending_sequence({}), [])

    def test_silent_for_active_sequence(self) -> None:
        self._write_seq('active-seq', status='active', audit=[self._rev(45)])
        self.assertEqual(self.hps.check_stalled_pending_sequence({}), [])

    def test_silent_for_pending_without_revision(self) -> None:
        # Healthy pending sequence awaiting its first DAG preflight.
        self._write_seq(
            'healthy-seq', status='pending',
            audit=[{'ts': _iso(120), 'event': 'sequence-created',
                    'actor': 'beacon'}],
        )
        self.assertEqual(self.hps.check_stalled_pending_sequence({}), [])

    def test_silent_when_revision_outside_scan_window(self) -> None:
        # 2 days old — historical record, not a live stall.
        self._write_seq(
            'abandoned-seq', status='pending',
            audit=[self._rev(2 * 24 * 60)],
        )
        self.assertEqual(self.hps.check_stalled_pending_sequence({}), [])

    def test_uses_latest_revision_when_multiple(self) -> None:
        # An old revision + a recent re-revision → keyed to the newest, and
        # since the newest is within the threshold, no alert yet.
        self._write_seq(
            'rerev-seq', status='pending',
            audit=[self._rev(200, task='t1'), self._rev(5, task='t2')],
        )
        self.assertEqual(self.hps.check_stalled_pending_sequence({}), [])

    def test_dedup_key_stable_across_runs(self) -> None:
        self._write_seq('stuck-seq', status='pending', audit=[self._rev(45)])
        a1 = self.hps.check_stalled_pending_sequence({})[0]
        a2 = self.hps.check_stalled_pending_sequence({})[0]
        self.assertEqual(a1['key'], a2['key'])
        # After recording, the shared cooldown suppresses a re-DM.
        state: dict = {}
        self.assertTrue(self.hps.should_alert(state, a1['key']))
        self.hps.record_alert(state, a1['key'])
        self.assertFalse(self.hps.should_alert(state, a1['key']))


class TestCheckRedMirrorStatusNoArtifact(_TempAgentsRootMixin, unittest.TestCase):
    """mirror-review-visibility Contract E (spec §8 / §10-E). The backstop for
    the #653 silent-red failure mode: a session-less PR sitting on a RED
    `mirror-review` commit status past threshold with no self-heal in progress
    and nothing on Larry's surfaces is promoted to its Contract C action surface
    (the for-Larry Waiting-on-You record) recover-FIRST, and alerts exactly once
    only if that promotion fails."""

    def setUp(self) -> None:
        super().setUp()
        # Sandbox the no_session_ledger (the _self_heal_in_progress guard reads
        # it) to this test's root.
        self._nsl = self.hps.no_session_ledger
        self._nsl_orig = self._nsl.LEDGER_FILE
        self._nsl.LEDGER_FILE = self.agents_root / 'state' / 'ns-ledger.json'
        # Sandbox the for-Larry feed to a temp file; feed_path() reads this env
        # live on every call.
        self._feed = self.agents_root / 'blackboard' / 'for-larry-escalations.json'
        self._feed_env_orig = os.environ.get('OURLIBERTY_FOR_LARRY_FEED_FILE')
        os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = str(self._feed)
        self._fle = self.hps.for_larry_escalations
        # Pin the Approvals-tab lookup to "nothing known" so _larry_artifact_exists
        # is driven purely by the sandboxed for-Larry feed, never the host's real
        # approvals state.
        patcher = patch('beacon_approval_handler.find_by_id_any_state',
                        return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._nsl.LEDGER_FILE = self._nsl_orig
        if self._feed_env_orig is None:
            os.environ.pop('OURLIBERTY_FOR_LARRY_FEED_FILE', None)
        else:
            os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = self._feed_env_orig
        super().tearDown()

    def _pr(self, number=7, branch='forge/silent-red', repo='owner/repo',
            created_min_ago=200):
        created = (datetime.now(timezone.utc)
                   - timedelta(minutes=created_min_ago))
        return {
            '_repo': repo,
            'number': number,
            'headRefName': branch,
            'createdAt': created.strftime('%Y-%m-%dT%H:%M:%SZ'),
        }

    def _status(self, state='failure', sha='deadbeefcafe', red_min_ago=120):
        red_since = (datetime.now(timezone.utc)
                     - timedelta(minutes=red_min_ago)) if red_min_ago else None
        return (state, sha, red_since)

    # ---- detection ----

    def test_red_status_past_threshold_no_artifact_alerts(self) -> None:
        with patch.object(self.hps, '_mirror_review_status',
                          return_value=self._status()):
            alerts = self.hps.check_red_mirror_status_no_artifact([self._pr()], {})
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a['key'], 'red_mirror_status:owner/repo:7')
        self.assertEqual(a['subject'], 'pipeline-stall:red-mirror-status:PR#7')
        self.assertIn('silent-red', a['message'])
        self.assertIn('recovery', a)

    def test_green_status_no_alert(self) -> None:
        with patch.object(self.hps, '_mirror_review_status',
                          return_value=self._status(state='success')):
            alerts = self.hps.check_red_mirror_status_no_artifact([self._pr()], {})
        self.assertEqual(alerts, [])

    def test_pending_status_no_alert(self) -> None:
        with patch.object(self.hps, '_mirror_review_status',
                          return_value=self._status(state='pending')):
            alerts = self.hps.check_red_mirror_status_no_artifact([self._pr()], {})
        self.assertEqual(alerts, [])

    def test_gh_unreachable_no_alert(self) -> None:
        # _mirror_review_status returns (None, None, None) on a gh outage — a
        # verify-before-alarm skip, not a stall.
        with patch.object(self.hps, '_mirror_review_status',
                          return_value=(None, None, None)):
            alerts = self.hps.check_red_mirror_status_no_artifact([self._pr()], {})
        self.assertEqual(alerts, [])

    def test_fresh_red_within_threshold_no_alert(self) -> None:
        # Old PR (passes the cheap createdAt pre-filter) but the status only just
        # went red — under RED_MIRROR_STATUS_MIN, so not yet a stall.
        with patch.object(self.hps, '_mirror_review_status',
                          return_value=self._status(red_min_ago=10)):
            alerts = self.hps.check_red_mirror_status_no_artifact([self._pr()], {})
        self.assertEqual(alerts, [])

    def test_young_pr_skipped_by_prefilter(self) -> None:
        # A PR younger than the threshold can't have a red status older than it;
        # the pre-filter skips the per-PR gh call entirely.
        called = []

        def _spy(repo, number):
            called.append((repo, number))
            return self._status()

        with patch.object(self.hps, '_mirror_review_status', side_effect=_spy):
            alerts = self.hps.check_red_mirror_status_no_artifact(
                [self._pr(created_min_ago=10)], {})
        self.assertEqual(alerts, [])
        self.assertEqual(called, [])  # gh never consulted

    def test_long_abandoned_red_outside_window_no_alert(self) -> None:
        with patch.object(self.hps, '_mirror_review_status',
                          return_value=self._status(red_min_ago=60 * 48)):
            alerts = self.hps.check_red_mirror_status_no_artifact(
                [self._pr(created_min_ago=60 * 48)], {})
        self.assertEqual(alerts, [])

    # ---- guards: no double-notify with step 2 ----

    def test_self_heal_in_progress_skips(self) -> None:
        # An OPEN no_session_ledger obligation means the mechanical cold-start
        # re-dispatch owns this task (Check 6) — the backstop must defer.
        self._nsl.open_obligation('silent-red', pr_url='https://x/pull/7')
        with patch.object(self.hps, '_mirror_review_status',
                          return_value=self._status()):
            alerts = self.hps.check_red_mirror_status_no_artifact([self._pr()], {})
        self.assertEqual(alerts, [])

    def test_existing_for_larry_record_skips(self) -> None:
        # Step 2 already surfaced this exact case — an OPEN Waiting-on-You record
        # under the SAME record_id — so the backstop must not double-notify.
        self._fle.upsert('mirror-review:silent-red', headline='h', context='c',
                         pr_url='https://github.com/owner/repo/pull/7',
                         head_sha='deadbeefcafe',
                         dedup_identity='https://github.com/owner/repo/pull/7@deadbeefcafe')
        with patch.object(self.hps, '_mirror_review_status',
                          return_value=self._status()):
            alerts = self.hps.check_red_mirror_status_no_artifact([self._pr()], {})
        self.assertEqual(alerts, [])

    def test_non_forge_branch_uses_pr_keyed_task_id(self) -> None:
        # An off-chain branch (the #653 shape) has no chain task_id; the canonical
        # key is pr-<repo>-<number>, matching step-2 routing exactly.
        with patch.object(self.hps, '_mirror_review_status',
                          return_value=self._status()):
            alerts = self.hps.check_red_mirror_status_no_artifact(
                [self._pr(branch='claude/auto-fix')], {})
        self.assertEqual(len(alerts), 1)
        rec = self._fle.get('mirror-review:pr-repo-7')
        self.assertIsNone(rec)  # detection only; recovery hasn't run yet
        self.assertIn('pr-repo-7', alerts[0]['suggested_action'])

    # ---- recovery: recover-then-route before any alert ----

    def _recovery_for(self, alerts):
        self.assertEqual(len(alerts), 1)
        return alerts[0]['recovery']

    def test_recovery_routes_to_for_larry_record(self) -> None:
        with patch.object(self.hps, '_mirror_review_status',
                          return_value=self._status()):
            alerts = self.hps.check_red_mirror_status_no_artifact([self._pr()], {})
        recovery = self._recovery_for(alerts)
        with patch.object(self.hps, '_gh_pr_state', return_value='OPEN'):
            recovered = recovery()
        self.assertTrue(recovered)  # routed → framework suppresses the alert
        rec = self._fle.get('mirror-review:silent-red')
        self.assertIsInstance(rec, dict)
        self.assertIs(rec['resolved'], False)
        self.assertEqual(rec['pr_url'], 'https://github.com/owner/repo/pull/7')

    def test_recovery_idempotent_no_duplicate_record(self) -> None:
        with patch.object(self.hps, '_mirror_review_status',
                          return_value=self._status()):
            alerts = self.hps.check_red_mirror_status_no_artifact([self._pr()], {})
        recovery = self._recovery_for(alerts)
        with patch.object(self.hps, '_gh_pr_state', return_value='OPEN'):
            self.assertTrue(recovery())
            self.assertTrue(recovery())  # same head → idempotent
        rows = self._fle.list_open()
        self.assertEqual(
            [r['id'] for r in rows], ['mirror-review:silent-red'])

    def test_recovery_clears_record_on_merged_pr(self) -> None:
        # PR merged out-of-band → the red status is moot; an existing record is
        # cleared and recovery reports success.
        self._fle.upsert('mirror-review:silent-red', headline='h', context='c',
                         pr_url='https://github.com/owner/repo/pull/7',
                         head_sha='deadbeefcafe', dedup_identity='x@y')
        with patch.object(self.hps, '_gh_pr_state', return_value='MERGED'):
            recovered = self.hps._recover_red_mirror_status(
                'silent-red', 'https://github.com/owner/repo/pull/7',
                'deadbeefcafe', 'mirror-review:silent-red')
        self.assertTrue(recovered)
        self.assertEqual(self._fle.list_open(), [])

    def test_recovery_defers_when_gh_unreachable(self) -> None:
        # gh unreachable (state None) with a known pr_url → defer, write nothing,
        # suppress the alert this round.
        with patch.object(self.hps, '_gh_pr_state', return_value=None):
            recovered = self.hps._recover_red_mirror_status(
                'silent-red', 'https://github.com/owner/repo/pull/7',
                'deadbeefcafe', 'mirror-review:silent-red')
        self.assertTrue(recovered)
        self.assertEqual(self._fle.list_open(), [])

    def test_recovery_false_when_write_fails_so_alert_fires(self) -> None:
        # If the durable promotion does not land (no open record afterward),
        # recovery returns False → the framework fires exactly one fallback alert.
        with patch.object(self.hps, '_gh_pr_state', return_value='OPEN'), \
                patch.object(self._fle, 'upsert', return_value=None), \
                patch.object(self._fle, 'get', return_value=None):
            recovered = self.hps._recover_red_mirror_status(
                'silent-red', 'https://github.com/owner/repo/pull/7',
                'deadbeefcafe', 'mirror-review:silent-red')
        self.assertFalse(recovered)

    def test_end_to_end_recover_then_route_suppresses_alert(self) -> None:
        # §10-E: the synthetic silent-red PR routes recover-then-alert — the
        # for-Larry record is written and NO Larry DM is appended.
        appended: list = []
        with patch.object(self.hps, '_all_open_prs', return_value=[self._pr()]), \
                patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
                patch.object(self.hps, '_mirror_review_status',
                             return_value=self._status()), \
                patch.object(self.hps, '_gh_pr_state', return_value='OPEN'), \
                patch.object(self.hps.larry_alerts, 'append_alert',
                             side_effect=lambda **kw: appended.append(kw) or True):
            rc = self.hps.run()
        self.assertEqual(rc, 0)
        # The silent-red stall recovered → its DM is suppressed. (Other checks
        # may DM on this bare fixture PR; we assert only that OUR subject never
        # paged.)
        red_dms = [a for a in appended
                   if a.get('subject') == 'pipeline-stall:red-mirror-status:PR#7']
        self.assertEqual(red_dms, [])
        rec = self._fle.get('mirror-review:silent-red')
        self.assertIsInstance(rec, dict)
        self.assertIs(rec['resolved'], False)


# ===================================================================
# Phase 2 #1 — heal_pipeline_stall noise reduction
# (episode-dedup + merge-truth gate + laptop-PR suppression)
# Spec: agents/beacon/specs/heal-pipeline-stall-noise-reduction.md
# ===================================================================


class TestEpisodeDedup(_TempAgentsRootMixin, unittest.TestCase):
    """Spec §1 / acceptance criterion 1. A slow-PR condition that persists
    across consecutive hourly ticks must alert ONCE per episode (re_dm_hours
    backstop), not once per legacy ALERT_DEDUP_HOURS — and a second alert only
    after the backstop elapses. A different PR alerts independently."""

    def _count_fires(self, window, *, n_ticks=8, interval_min=70):
        """Replay the run()-loop cooldown gate (`should_alert` →
        `record_alert`) across `n_ticks` ticks spaced `interval_min` apart, with
        a frozen clock advanced per tick. Returns how many ticks would DM.

        `interval_min=70` (slightly over an hour) mirrors the real cron cadence:
        under the legacy 1h window every tick clears the cooldown and re-DMs;
        under the 24h episode window only the onset tick fires."""
        state: dict = {}
        key = 'unrouted_open_pr:Larry-Yatch/ourliberty-agent-core:86'
        base = datetime(2026, 6, 23, 5, 0, tzinfo=timezone.utc)
        fires = 0
        fake = MagicMock(wraps=datetime)
        with patch.object(self.hps, 'datetime', fake):
            for i in range(n_ticks):
                fake.now.return_value = base + timedelta(minutes=i * interval_min)
                if self.hps.should_alert(state, key, window):
                    fires += 1
                    self.hps.record_alert(state, key)
        return fires

    def test_eight_hourly_ticks_episode_window_fires_once(self) -> None:
        # PR #86's live shape: 8 ticks, ~70 min apart. Episode (24h) → 1 DM.
        self.assertEqual(self._count_fires(24), 1)

    def test_eight_hourly_ticks_legacy_window_fires_every_tick(self) -> None:
        # Honesty companion: the SAME cadence under the flat 1h window re-DMs
        # every tick (the bug we're fixing — PR #86 fired 8x). Proves the test
        # cadence genuinely clears a 1h cooldown, so the single-fire result
        # above is the window's doing, not a too-tight cadence.
        self.assertEqual(self._count_fires(self.hps.ALERT_DEDUP_HOURS), 8)

    def test_second_alert_after_24h_backstop(self) -> None:
        # Onset fires; a tick just before 24h is silent; a tick past 24h re-DMs.
        state: dict = {}
        key = 'unrouted_open_pr:x:1'
        base = datetime(2026, 6, 23, 5, 0, tzinfo=timezone.utc)
        fake = MagicMock(wraps=datetime)
        with patch.object(self.hps, 'datetime', fake):
            fake.now.return_value = base
            self.assertTrue(self.hps.should_alert(state, key, 24))
            self.hps.record_alert(state, key)
            fake.now.return_value = base + timedelta(hours=23)
            self.assertFalse(self.hps.should_alert(state, key, 24))
            fake.now.return_value = base + timedelta(hours=25)
            self.assertTrue(self.hps.should_alert(state, key, 24))

    def test_different_pr_alerts_independently(self) -> None:
        # One PR already on cooldown must not suppress a different PR's onset.
        state = {
            'unrouted_open_pr:x:86': datetime.now(timezone.utc).isoformat(),
        }
        self.assertFalse(
            self.hps.should_alert(state, 'unrouted_open_pr:x:86', 24))
        self.assertTrue(
            self.hps.should_alert(state, 'unrouted_open_pr:x:99', 24))

    def test_slow_pr_checks_stamp_episode_window(self) -> None:
        # The four slow-PR checks must carry re_dm_hours so the gate applies the
        # episode window (criterion 1 is meaningless if the alert omits it).
        lines = [_watcher_forge_done_line('episode-task-001', minutes_ago=180)]
        alerts = self.hps.check_forge_built_no_pr(lines, [], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['re_dm_hours'], 24)


class TestMergeTruthGate(_TempAgentsRootMixin, unittest.TestCase):
    """Spec §2 / acceptance criterion 2. Before emitting a per-PR alert, the
    run() loop confirms the PR is non-terminal via `gh pr view`; a MERGED/CLOSED
    PR is suppressed. Degrades safe: an unreadable state stays alertable."""

    def _unrouted_pr(self, number=86, repo='Larry-Yatch/ourliberty-agent-core'):
        return {
            'number': number,
            'headRefName': f'larry/manual-pr-{number}',
            'title': 'feat: x',
            'createdAt': (
                datetime.now(timezone.utc) - timedelta(minutes=180)
            ).isoformat(),
            '_repo': repo,
        }

    # ---- helper-level: _pr_is_terminal truth table + cache ----

    def test_pr_is_terminal_merged_and_closed(self) -> None:
        for st in ('MERGED', 'CLOSED'):
            with patch.object(self.hps, '_gh_pr_state', return_value=st):
                self.assertTrue(self.hps._pr_is_terminal('u', {}))

    def test_pr_is_terminal_open_is_alertable(self) -> None:
        with patch.object(self.hps, '_gh_pr_state', return_value='OPEN'):
            self.assertFalse(self.hps._pr_is_terminal('u', {}))

    def test_pr_is_terminal_unreadable_degrades_safe(self) -> None:
        # gh outage → None → NOT terminal → still alertable (never a silent drop).
        with patch.object(self.hps, '_gh_pr_state', return_value=None):
            self.assertFalse(self.hps._pr_is_terminal('u', {}))

    def test_pr_is_terminal_caches_per_url(self) -> None:
        cache: dict = {}
        with patch.object(self.hps, '_gh_pr_state',
                          return_value='MERGED') as mock_gh:
            self.hps._pr_is_terminal('u', cache)
            self.hps._pr_is_terminal('u', cache)
        self.assertEqual(mock_gh.call_count, 1)  # one gh call for two checks

    # ---- run()-level: merged PR produces zero alerts ----

    def test_merged_pr_produces_zero_alerts(self) -> None:
        pr = self._unrouted_pr()
        with patch.object(self.hps, '_all_open_prs', return_value=[pr]), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=[]), \
             patch.object(self.hps, '_read_recent_routing_events', return_value=[]), \
             patch.object(self.hps, '_resolution_signal_present',
                          return_value=(False, None)), \
             patch.object(self.hps, '_mirror_session_active_for_pr',
                          return_value=(False, None)), \
             patch.object(self.hps, '_gh_pr_state', return_value='MERGED'), \
             patch.object(self.hps.larry_alerts, 'append_alert',
                          return_value=True) as mock_alert:
            self.hps.run()
        mock_alert.assert_not_called()

    def test_same_fixture_open_pr_still_alerts(self) -> None:
        # Honesty companion: the SAME unrouted fixture, gh-confirmed OPEN, DOES
        # alert — so the zero-alert result above is the gate, not a dead fixture.
        pr = self._unrouted_pr()
        with patch.object(self.hps, '_all_open_prs', return_value=[pr]), \
             patch.object(self.hps, '_all_merged_prs_recent', return_value=[]), \
             patch.object(self.hps, '_read_recent_log_lines', return_value=[]), \
             patch.object(self.hps, '_read_recent_routing_events', return_value=[]), \
             patch.object(self.hps, '_resolution_signal_present',
                          return_value=(False, None)), \
             patch.object(self.hps, '_mirror_session_active_for_pr',
                          return_value=(False, None)), \
             patch.object(self.hps, '_gh_pr_state', return_value='OPEN'), \
             patch.object(self.hps.larry_alerts, 'append_alert',
                          return_value=True) as mock_alert:
            self.hps.run()
        subjects = [c.kwargs.get('subject') for c in mock_alert.call_args_list]
        self.assertIn('pipeline-stall:unrouted-pr:PR#86', subjects)


class TestLaptopPrSuppression(_TempAgentsRootMixin, unittest.TestCase):
    """Spec §3 / acceptance criterion 3. A laptop-authored PR (`claude/` branch,
    or `fix|feat|chore`-shaped) whose review was dispatched under the synthetic
    `pr-<repo>-<num>` task_id must NOT false-alert as unrouted; the same PR with
    NO dispatch still alerts once (detection preserved)."""

    _REPO = 'Larry-Yatch/ourliberty-agent-core'

    def _pr(self, number, branch):
        return {
            'number': number,
            'headRefName': branch,
            'title': 'fix: laptop change',
            'createdAt': (
                datetime.now(timezone.utc) - timedelta(minutes=180)
            ).isoformat(),
            '_repo': self._REPO,
        }

    def _pr_dispatch_event(self, number):
        # The review-request a laptop PR's dispatch logs: task_id is the
        # synthetic pr-<repo>-<num>, not a forge/<task> branch token.
        return {
            'source_agent': 'beacon',
            'target_agent_final': 'mirror',
            'phase': 'review',
            'task_id': f'pr-ourliberty-agent-core-{number}',
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

    def _run_check(self, prs, events):
        with patch.object(self.hps, '_resolution_signal_present',
                          return_value=(False, None)), \
             patch.object(self.hps, '_mirror_session_active_for_pr',
                          return_value=(False, None)):
            return self.hps.check_unrouted_open_prs(prs, events, {})

    def test_claude_branch_with_pr_dispatch_suppressed(self) -> None:
        prs = [self._pr(640, 'claude/some-laptop-task')]
        alerts = self._run_check(prs, [self._pr_dispatch_event(640)])
        self.assertEqual(alerts, [])

    def test_fix_labeled_branch_with_pr_dispatch_suppressed(self) -> None:
        # A `fix/`-prefixed laptop branch yields no branch_task, but its
        # pr-<repo>-<num> dispatch still suppresses via _pr_dispatch_task_id.
        prs = [self._pr(641, 'fix/laptop-bug')]
        alerts = self._run_check(prs, [self._pr_dispatch_event(641)])
        self.assertEqual(alerts, [])

    def test_claude_branch_no_dispatch_still_alerts(self) -> None:
        # Detection preserved: no review dispatched → exactly one alert.
        prs = [self._pr(642, 'claude/undispatched-task')]
        alerts = self._run_check(prs, [])
        self.assertEqual(len(alerts), 1)
        self.assertIn('PR #642', alerts[0]['message'])

    def test_task_id_from_branch_recognizes_claude_prefix(self) -> None:
        self.assertEqual(
            self.hps._task_id_from_branch('claude/my-task-001'), 'my-task-001')

    def test_pr_dispatch_task_id_form(self) -> None:
        self.assertEqual(
            self.hps._pr_dispatch_task_id(
                {'_repo': self._REPO, 'number': 642}),
            'pr-ourliberty-agent-core-642')
        # Malformed PR → None (no crash).
        self.assertIsNone(self.hps._pr_dispatch_task_id({'number': 1}))
        self.assertIsNone(self.hps._pr_dispatch_task_id({'_repo': self._REPO}))


class TestNovelStallNoRegression(_TempAgentsRootMixin, unittest.TestCase):
    """Acceptance criterion 4. A genuinely novel stall — a forge/ PR, open,
    non-terminal, no dispatch, past the age floor — still alerts EXACTLY once.
    The noise-reduction changes suppress duplicates/terminals/in-progress, never
    a first genuine alert."""

    def test_novel_forge_stall_alerts_once(self) -> None:
        pr = {
            'number': 314,
            'headRefName': 'forge/novel-stall-001',
            'title': 'feat: novel',
            'createdAt': (
                datetime.now(timezone.utc) - timedelta(minutes=180)
            ).isoformat(),
            '_repo': 'Larry-Yatch/ourliberty-agent-core',
        }
        with patch.object(self.hps, '_resolution_signal_present',
                          return_value=(False, None)), \
             patch.object(self.hps, '_mirror_session_active_for_pr',
                          return_value=(False, None)):
            alerts = self.hps.check_unrouted_open_prs([pr], [], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn('PR #314', alerts[0]['message'])
        # The alert carries the episode window + the gate's pr_url, and stays
        # retraction-compatible on the stable key.
        self.assertEqual(alerts[0]['re_dm_hours'], 24)
        self.assertIn('pr_url', alerts[0])
        self.assertEqual(
            alerts[0]['key'],
            'unrouted_open_pr:Larry-Yatch/ourliberty-agent-core:314')


class TestStallRulesConfig(_TempAgentsRootMixin, unittest.TestCase):
    """Acceptance criterion 5. Config defaults load when the file is absent;
    per-check overrides are read when present; malformed/bad values fall back to
    the code defaults without raising."""

    @contextlib.contextmanager
    def _config_path(self, body=None):
        """Point PIPELINE_STALL_RULES_FILE at a temp path. `body=None` leaves
        the path absent; a str is written verbatim (to exercise malformed JSON);
        a dict is json-dumped."""
        d = tempfile.TemporaryDirectory()
        try:
            path = Path(d.name) / 'pipeline-stall-rules.json'
            if body is not None:
                path.write_text(body if isinstance(body, str)
                                else json.dumps(body))
            with patch.object(self.hps, 'PIPELINE_STALL_RULES_FILE', path):
                yield
        finally:
            d.cleanup()

    def test_defaults_when_file_absent(self) -> None:
        with self._config_path(None):
            self.assertEqual(self.hps.re_dm_hours_for('unrouted_open_pr'), 24)
            # Unnamed slow-PR check falls back to _default.
            self.assertEqual(self.hps.re_dm_hours_for('some_other_check'), 24)

    def test_override_when_present(self) -> None:
        body = {'re_dm_hours': {'_default': 12, 'unrouted_open_pr': 3}}
        with self._config_path(body):
            self.assertEqual(self.hps.re_dm_hours_for('unrouted_open_pr'), 3)
            # _default override applies to unnamed checks.
            self.assertEqual(self.hps.re_dm_hours_for('unnamed_check'), 12)
            # A check absent from the file keeps its code default.
            self.assertEqual(self.hps.re_dm_hours_for('forge_built_no_pr'), 24)

    def test_malformed_json_falls_back_to_defaults(self) -> None:
        with self._config_path('{not valid json'):
            self.assertEqual(self.hps.re_dm_hours_for('unrouted_open_pr'), 24)

    def test_non_object_top_level_falls_back(self) -> None:
        with self._config_path('[1, 2, 3]'):
            self.assertEqual(self.hps.re_dm_hours_for('unrouted_open_pr'), 24)

    def test_bad_values_keep_per_key_default(self) -> None:
        # Zero, negative, bool, and non-numeric values are each rejected; the
        # code default for that key survives, valid siblings still apply.
        body = {'re_dm_hours': {
            'unrouted_open_pr': 0,
            'forge_built_no_pr': -5,
            'mirror_pass_unmerged': True,
            'pr_no_mirror_dispatch': 'soon',
            '_default': 6,
        }}
        with self._config_path(body):
            self.assertEqual(self.hps.re_dm_hours_for('unrouted_open_pr'), 24)
            self.assertEqual(self.hps.re_dm_hours_for('forge_built_no_pr'), 24)
            self.assertEqual(self.hps.re_dm_hours_for('mirror_pass_unmerged'), 24)
            self.assertEqual(
                self.hps.re_dm_hours_for('pr_no_mirror_dispatch'), 24)
            # The valid _default override is still honored.
            self.assertEqual(self.hps.re_dm_hours_for('unnamed'), 6)

    def test_shipped_config_file_loads_cleanly(self) -> None:
        # The real config/pipeline-stall-rules.json shipped in this PR must parse
        # and yield the documented 24h windows (no patch — exercises the actual
        # file the healer reads in production).
        self.assertEqual(self.hps.re_dm_hours_for('unrouted_open_pr'), 24)
        self.assertEqual(self.hps.re_dm_hours_for('forge_built_no_pr'), 24)


if __name__ == '__main__':
    unittest.main()
