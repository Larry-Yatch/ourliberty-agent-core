"""Tests for scripts/pipeline_live_state.py — the canonical "is pipeline work
live RIGHT NOW?" primitive.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Hermetic + deterministic: every /proc read is monkeypatched (no real `pgrep`
or /proc dependence), the Mirror inbox points at a tmp dir, and the dispatch
lease state is controlled via a tmp LEASES_DIR / monkeypatched is_held — so the
suite is reproducible on any host and never touches the real ~/agents tree.

Coverage:
- pr_review_in_progress (port of #716's _mirror_session_active_for_pr):
  * live_pid exact match
  * boundary guards: 713 NOT matched by a -7130 / -7131 proc; 713 matched by
    -713, -713-rev2, -713-replan1, and the kernel `(deleted)` suffix
  * inbox_task_present: review-pr-<repo>-713.json and -713-rev1.json match;
    review-pr-<repo>-7130.json does NOT match 713
  * inactive → (False,'')
- agent_dispatch_live:
  * held + alive → (True,'lease_held')
  * not held → (False,'')
  * held but holder pid dead → (False,'')
  * leases off / error → (False,'')
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import pipeline_live_state as pls  # noqa: E402
import dispatch_lease  # noqa: E402

REPO = 'ourliberty-agent-core'


class _TempRootsMixin:
    """Redirect WORKTREES_ROOT + AGENTS_ROOT to a tmp dir for the duration of
    each test (pls resolves both from env at call time)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.worktrees_root = root / 'agent-worktrees'
        self.agents_root = root / 'agents'
        self.worktrees_root.mkdir()
        self.agents_root.mkdir()
        self.mirror_inbox = self.agents_root / 'inboxes' / 'mirror'
        self.mirror_inbox.mkdir(parents=True)
        self._env_snapshot = {
            'OURLIBERTY_WORKTREES_ROOT': os.environ.get('OURLIBERTY_WORKTREES_ROOT'),
            'OURLIBERTY_AGENTS_ROOT': os.environ.get('OURLIBERTY_AGENTS_ROOT'),
        }
        os.environ['OURLIBERTY_WORKTREES_ROOT'] = str(self.worktrees_root)
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.agents_root)

    def tearDown(self) -> None:
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _cwd_for(self, worktree_name: str) -> str:
        """A claude proc cwd that sits at the root of the named worktree."""
        return str(self.worktrees_root / worktree_name)


# ----------------------------- pr_review_in_progress -----------------------

class TestPrReviewInProgressLivePid(_TempRootsMixin, unittest.TestCase):
    """The live-proc signal + its boundary discipline."""

    def _probe_with_proc(self, cwd: str, repo: str, pr_number: int):
        with patch.object(pls, '_claude_pids', return_value=[4242]), \
                patch.object(pls, '_proc_cwd', return_value=cwd):
            return pls.pr_review_in_progress(repo, pr_number)

    def test_exact_match_is_live_pid(self):
        cwd = self._cwd_for(f'wt-mirror-pr-{REPO}-713')
        active, reason = self._probe_with_proc(cwd, REPO, 713)
        self.assertTrue(active)
        self.assertEqual(reason, 'live_pid')

    def test_rev_suffix_matches(self):
        cwd = self._cwd_for(f'wt-mirror-pr-{REPO}-713-rev2')
        active, reason = self._probe_with_proc(cwd, REPO, 713)
        self.assertTrue(active)
        self.assertEqual(reason, 'live_pid')

    def test_replan_suffix_matches(self):
        cwd = self._cwd_for(f'wt-mirror-pr-{REPO}-713-replan1')
        active, reason = self._probe_with_proc(cwd, REPO, 713)
        self.assertTrue(active)
        self.assertEqual(reason, 'live_pid')

    def test_deleted_cwd_suffix_tolerated(self):
        cwd = self._cwd_for(f'wt-mirror-pr-{REPO}-713') + ' (deleted)'
        active, reason = self._probe_with_proc(cwd, REPO, 713)
        self.assertTrue(active)
        self.assertEqual(reason, 'live_pid')

    def test_deleted_cwd_suffix_with_rev_tolerated(self):
        cwd = self._cwd_for(f'wt-mirror-pr-{REPO}-713-rev2') + ' (deleted)'
        active, reason = self._probe_with_proc(cwd, REPO, 713)
        self.assertTrue(active)
        self.assertEqual(reason, 'live_pid')

    def test_nested_cwd_under_worktree_matches(self):
        # A review session whose cwd descended into a subdir still matches on
        # the first path segment.
        cwd = self._cwd_for(f'wt-mirror-pr-{REPO}-713') + '/scripts'
        active, reason = self._probe_with_proc(cwd, REPO, 713)
        self.assertTrue(active)
        self.assertEqual(reason, 'live_pid')

    def test_boundary_7130_does_not_match_713(self):
        # A live #7130 review proc must NOT satisfy a #713 probe.
        cwd = self._cwd_for(f'wt-mirror-pr-{REPO}-7130')
        active, reason = self._probe_with_proc(cwd, REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_boundary_7131_does_not_match_713(self):
        cwd = self._cwd_for(f'wt-mirror-pr-{REPO}-7131')
        active, reason = self._probe_with_proc(cwd, REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_unexpected_trailing_garbage_does_not_match(self):
        # A suffix that isn't a clean -rev<N>/-replan<N> fails the anchored RE.
        cwd = self._cwd_for(f'wt-mirror-pr-{REPO}-713-wip')
        active, reason = self._probe_with_proc(cwd, REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_other_repo_proc_does_not_match(self):
        cwd = self._cwd_for('wt-mirror-pr-ourliberty-dashboard-713')
        active, reason = self._probe_with_proc(cwd, REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_non_mirror_worktree_ignored(self):
        cwd = self._cwd_for(f'wt-forge-{REPO}-713')
        active, reason = self._probe_with_proc(cwd, REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_cwd_outside_worktrees_root_ignored(self):
        with patch.object(pls, '_claude_pids', return_value=[1]), \
                patch.object(pls, '_proc_cwd', return_value='/tmp/somewhere'):
            active, reason = pls.pr_review_in_progress(REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_unreadable_proc_cwd_skipped(self):
        with patch.object(pls, '_claude_pids', return_value=[1]), \
                patch.object(pls, '_proc_cwd', return_value=None):
            active, reason = pls.pr_review_in_progress(REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_no_procs_no_inbox_is_inactive(self):
        with patch.object(pls, '_claude_pids', return_value=[]):
            active, reason = pls.pr_review_in_progress(REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')


class TestPrReviewInProgressInbox(_TempRootsMixin, unittest.TestCase):
    """The dispatched-inbox-task signal + its boundary discipline. No live
    procs in any of these (claude_pids patched empty) so only the inbox decides.
    """

    def _probe(self, repo: str, pr_number: int):
        with patch.object(pls, '_claude_pids', return_value=[]):
            return pls.pr_review_in_progress(repo, pr_number)

    def _write_inbox(self, name: str) -> None:
        (self.mirror_inbox / name).write_text('{}')

    def test_exact_json_task_matches(self):
        self._write_inbox(f'review-pr-{REPO}-713.json')
        active, reason = self._probe(REPO, 713)
        self.assertTrue(active)
        self.assertEqual(reason, 'inbox_task_present')

    def test_rev_suffixed_task_matches(self):
        self._write_inbox(f'review-pr-{REPO}-713-rev1.json')
        active, reason = self._probe(REPO, 713)
        self.assertTrue(active)
        self.assertEqual(reason, 'inbox_task_present')

    def test_dotted_extra_segment_task_matches(self):
        # The char after the number is the `.` extension dot → boundary OK.
        self._write_inbox(f'review-pr-{REPO}-713.attempt2.json')
        active, reason = self._probe(REPO, 713)
        self.assertTrue(active)
        self.assertEqual(reason, 'inbox_task_present')

    def test_boundary_7130_task_does_not_match_713(self):
        self._write_inbox(f'review-pr-{REPO}-7130.json')
        active, reason = self._probe(REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_other_repo_task_does_not_match(self):
        self._write_inbox('review-pr-ourliberty-dashboard-713.json')
        active, reason = self._probe(REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_empty_inbox_is_inactive(self):
        active, reason = self._probe(REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_missing_inbox_dir_is_inactive(self):
        # Remove the inbox entirely — the glob fails safe → not live.
        for f in self.mirror_inbox.iterdir():
            f.unlink()
        self.mirror_inbox.rmdir()
        (self.agents_root / 'inboxes').rmdir()
        active, reason = self._probe(REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')


class TestPrReviewInProgressFailSafe(_TempRootsMixin, unittest.TestCase):
    def test_exception_in_pid_scan_is_not_live(self):
        # A probe that itself raises must be read as "not live", never crash.
        def _boom():
            raise RuntimeError('proc scan blew up')

        with patch.object(pls, '_claude_pids', side_effect=_boom):
            active, reason = pls.pr_review_in_progress(REPO, 713)
        self.assertFalse(active)
        self.assertEqual(reason, '')


# ------------------------------ agent_dispatch_live ------------------------

class TestAgentDispatchLive(unittest.TestCase):
    AGENT = 'mirror'
    IDENTITY = 'inbox:mirror'

    def test_held_and_alive_is_live(self):
        with patch.object(dispatch_lease, 'is_held', return_value=True) as m:
            active, reason = pls.agent_dispatch_live(self.AGENT)
        m.assert_called_once_with(self.IDENTITY)
        self.assertTrue(active)
        self.assertEqual(reason, 'lease_held')

    def test_not_held_is_inactive(self):
        with patch.object(dispatch_lease, 'is_held', return_value=False):
            active, reason = pls.agent_dispatch_live(self.AGENT)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_held_but_holder_pid_dead_is_inactive(self):
        # is_held already folds the liveness check (TTL + _pid_alive_same_boot):
        # a lease whose holder pid is dead reads as not-held → not live. Exercise
        # the REAL is_held against a tmp lease whose holder pid is dead.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        leases_dir = Path(tmp.name) / 'dispatch-leases'
        leases_dir.mkdir()
        env_snap = os.environ.get('GM_DEDUP_USE_LEASES')
        os.environ['GM_DEDUP_USE_LEASES'] = 'authoritative'
        self.addCleanup(
            lambda: os.environ.__setitem__('GM_DEDUP_USE_LEASES', env_snap)
            if env_snap is not None
            else os.environ.pop('GM_DEDUP_USE_LEASES', None)
        )
        import time
        dead_pid = 2 ** 22  # a PID that is virtually certain not to exist
        lease_path = leases_dir / (
            dispatch_lease._safe_identity(self.IDENTITY) + '.lease')
        import json
        lease_path.write_text(json.dumps({
            'identity': self.IDENTITY,
            'holder_pid': dead_pid,
            'boot_id': 'some-boot',
            'nonce': 'n',
            'timestamp_created': time.time(),
            'timestamp_renewed': time.time(),
        }))
        with patch.object(dispatch_lease, 'LEASES_DIR', leases_dir):
            active, reason = pls.agent_dispatch_live(self.AGENT)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_leases_off_is_inactive(self):
        env_snap = os.environ.get('GM_DEDUP_USE_LEASES')
        os.environ['GM_DEDUP_USE_LEASES'] = 'off'
        self.addCleanup(
            lambda: os.environ.__setitem__('GM_DEDUP_USE_LEASES', env_snap)
            if env_snap is not None
            else os.environ.pop('GM_DEDUP_USE_LEASES', None)
        )
        active, reason = pls.agent_dispatch_live(self.AGENT)
        self.assertFalse(active)
        self.assertEqual(reason, '')

    def test_exception_is_inactive(self):
        with patch.object(dispatch_lease, 'is_held',
                          side_effect=RuntimeError('lease read blew up')):
            active, reason = pls.agent_dispatch_live(self.AGENT)
        self.assertFalse(active)
        self.assertEqual(reason, '')


if __name__ == '__main__':
    unittest.main()
