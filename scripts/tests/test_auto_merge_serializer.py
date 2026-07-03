#!/usr/bin/env python3
"""Tests for D3.5 5d-prime — AUTO_MERGE serializer.

Spec: an overlap-aware merge gate inserted before `gh pr merge` to
eliminate the 2026-05-26 incident class (two Mirror-passed PRs with
overlapping changed_files; the second's auto-merge guaranteed-to-fail;
Larry interrupted with a "Merge manually" DM).

Acceptance criteria covered:
  - Non-overlap baseline: D3.5 5d behavior is unchanged.
  - Gate 1: single-blocker hold; FIFO release on blocker-merge.
  - Gate 1: chained A < B < C blockers release in FIFO order.
  - Gate 1: blocker-rejected (closed without merge) release.
  - Gate 1: cross-repo isolation (PR in repo A never blocks repo B).
  - Gate 2: mergeable=CONFLICTING → DM rebase, NO merge call, queue clear.
  - Gate 2: mergeable=UNKNOWN → defer one sweep tick; second tick proceeds.
  - Watchdog DM after >watchdog_dm_hours, single-DM idempotency.
  - Corrupt queue file → fail-closed (no merges; DM Larry once).

Run:
    cd /home/larry/agent-core &&
    python3 -m unittest scripts.tests.test_auto_merge_serializer
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_AGENTS_ROOT_BACKUP = None
_AGENTS_ROOT_TMPDIR = None
_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    global _AGENTS_ROOT_BACKUP, _AGENTS_ROOT_TMPDIR, _CHOKEPOINT_SAVED_SENTINEL
    # Auto-merge drives gh-write (gh pr merge) through outbox_notifier against
    # the rerouted tmpdir tree; opt out of the Layer B guards so they pass
    # through to the test's mocks (the #428 real-tree scan still runs).
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()
    _AGENTS_ROOT_BACKUP = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    _AGENTS_ROOT_TMPDIR = tempfile.mkdtemp(prefix='auto-merge-serializer-test-')
    os.environ['OURLIBERTY_AGENTS_ROOT'] = _AGENTS_ROOT_TMPDIR
    for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
        Path(_AGENTS_ROOT_TMPDIR, sub).mkdir(exist_ok=True)
    import outbox_notifier
    importlib.reload(outbox_notifier)


def tearDownModule():  # noqa: N802
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)
    if _AGENTS_ROOT_BACKUP is None:
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
    else:
        os.environ['OURLIBERTY_AGENTS_ROOT'] = _AGENTS_ROOT_BACKUP
    if _AGENTS_ROOT_TMPDIR:
        shutil.rmtree(_AGENTS_ROOT_TMPDIR, ignore_errors=True)
    import outbox_notifier
    importlib.reload(outbox_notifier)


REPO = 'Larry-Yatch/ourliberty-agent-core'
DASHBOARD_REPO = 'Larry-Yatch/ourliberty-dashboard'


def _pr_url(repo: str, n: int) -> str:
    return f'https://github.com/{repo}/pull/{n}'


class _GhRouter:
    """Stand-in for `subprocess.run` that routes `gh` invocations to
    in-memory fixtures. Each test sets per-PR fixtures (files, mergeable,
    state) and per-repo open-PR lists.
    """

    def __init__(self):
        # (repo, pr_number) -> list[str] of changed files
        self.pr_files: dict[tuple[str, int], list[str]] = {}
        # (repo, pr_number) -> 'mergeable' | 'conflicting' | 'unknown' (raw)
        self.pr_mergeable: dict[tuple[str, int], str] = {}
        # (repo, pr_number) -> 'OPEN' | 'MERGED' | 'CLOSED'
        self.pr_state: dict[tuple[str, int], str] = {}
        # repo -> list[dict] of open PRs (number, createdAt, headRefName)
        self.open_prs: dict[str, list[dict]] = {}
        # fix-auto-merge-freshness-revalidation — baseRefOid (base-branch tip)
        # + headRefOid per PR, consumed by the release-path freshness gate.
        # Defaults are STABLE per repo/PR, so unless a test deliberately moves
        # the base between hold-time and release-time, base_moved == False and
        # the freshness gate is a no-op (preserves pre-feature release
        # behavior). A test simulates "blocker merged, main moved" by mutating
        # pr_base for the held PR before the release pass.
        self.pr_base: dict[tuple[str, int], str] = {}
        self.pr_head: dict[tuple[str, int], str] = {}
        # Recorded `gh pr merge` shell-outs
        self.merge_calls: list[tuple[str, int]] = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None,
                 **kwargs):
        # **kwargs absorbs cwd= (and any future kw) from non-gh shell-outs
        # like worktree teardown's `git -C ... worktree remove`, so they
        # route through the fixture (returncode=1, 'not gh') instead of
        # raising TypeError that the daemon-never-wedge guards swallow.
        # All callers pass a list[str] cmd starting with 'gh'.
        if not cmd or cmd[0] != 'gh':
            return _CompletedProc(stdout='', stderr='not gh', returncode=1)
        if cmd[1] == 'pr' and cmd[2] == 'view':
            pr_n = int(cmd[3])
            # --repo <coords>
            repo = cmd[cmd.index('--repo') + 1]
            json_fields = cmd[cmd.index('--json') + 1]
            payload: dict = {}
            if 'files' in json_fields.split(','):
                files = self.pr_files.get((repo, pr_n), [])
                payload['files'] = [{'path': p} for p in files]
            if 'mergeable' in json_fields.split(','):
                m = self.pr_mergeable.get((repo, pr_n), 'UNKNOWN')
                payload['mergeable'] = m
                payload['mergeStateStatus'] = (
                    'CLEAN' if m == 'MERGEABLE' else
                    'DIRTY' if m == 'CONFLICTING' else 'UNKNOWN'
                )
            if 'state' in json_fields.split(','):
                payload['state'] = self.pr_state.get((repo, pr_n), 'OPEN')
            if 'baseRefOid' in json_fields.split(','):
                payload['baseRefOid'] = self.pr_base.get(
                    (repo, pr_n), 'base000000000000',
                )
            if 'headRefOid' in json_fields.split(','):
                payload['headRefOid'] = self.pr_head.get(
                    (repo, pr_n), f'head{pr_n:012d}',
                )
            return _CompletedProc(
                stdout=json.dumps(payload), stderr='', returncode=0,
            )
        if cmd[1] == 'pr' and cmd[2] == 'list':
            repo = cmd[cmd.index('--repo') + 1]
            return _CompletedProc(
                stdout=json.dumps(self.open_prs.get(repo, [])),
                stderr='', returncode=0,
            )
        if cmd[1] == 'pr' and cmd[2] == 'merge':
            pr_n = int(cmd[3])
            repo = cmd[cmd.index('--repo') + 1]
            self.merge_calls.append((repo, pr_n))
            # Mark merged so post-merge state checks see MERGED.
            self.pr_state[(repo, pr_n)] = 'MERGED'
            return _CompletedProc(
                stdout='', stderr='', returncode=0,
            )
        return _CompletedProc(stdout='', stderr='unknown gh cmd', returncode=1)


class _CompletedProc:
    def __init__(self, stdout='', stderr='', returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class SerializerTestBase(unittest.TestCase):
    """Common setUp/tearDown: fresh tmpdir, mocked subprocess.run, reset
    module-level state. Each subclass test installs its own gh fixtures.
    """

    def setUp(self):
        import outbox_notifier as on
        import larry_alerts as la
        self.on = on
        self.la = la
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {
            'AGENTS_ROOT': on.AGENTS_ROOT,
            'AUTO_MERGE_QUEUE_FILE': on.AUTO_MERGE_QUEUE_FILE,
            'LOG_FILE': on.LOG_FILE,
            'BLACKBOARD': on.BLACKBOARD,
        }
        on.AGENTS_ROOT = self._root
        on.AUTO_MERGE_QUEUE_FILE = self._root / 'state' / 'auto-merge-queue.json'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.BLACKBOARD = self._root / 'blackboard'
        (self._root / 'state').mkdir(parents=True, exist_ok=True)
        (self._root / 'logs').mkdir(parents=True, exist_ok=True)
        (self._root / 'blackboard').mkdir(parents=True, exist_ok=True)
        # Reroute larry_alerts file targets so DMs don't escape to prod.
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
        # Mock subprocess.run for the gh CLI shell-outs.
        self.gh = _GhRouter()
        self._patcher = mock.patch(
            'outbox_notifier.subprocess.run', side_effect=self.gh,
        )
        self._patcher.start()
        on._reset_auto_merge_queue_state()
        on._invalidate_loop_bounds_cache()

    def tearDown(self):
        self._patcher.stop()
        on = self.on
        la = self.la
        for k, v in self._originals.items():
            setattr(on, k, v)
        for k, v in self._la_originals.items():
            setattr(la, k, v)
        on._reset_auto_merge_queue_state()
        on._invalidate_loop_bounds_cache()
        self._tmp.cleanup()

    def _read_alerts(self) -> list[dict]:
        path = self.la.ALERTS_FILE
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
        return out

    def _attempt(self, pr_n: int, repo: str = REPO, **kw) -> dict:
        return self.on._attempt_auto_merge_with_gates(
            pr_url=_pr_url(repo, pr_n),
            repo_coords=repo,
            pr_number=pr_n,
            task_id=kw.get('task_id', f'task-{pr_n}'),
            summary=kw.get('summary', f'summary for #{pr_n}'),
            chat_id=kw.get('chat_id', 999),
            changed_files=kw.get('changed_files'),
            second_attempt_on_unknown=kw.get('second_attempt_on_unknown', False),
        )


class NonOverlapBaselineTest(SerializerTestBase):
    """No-overlap PRs still merge in parallel — D3.5 5d behavior preserved."""

    def test_no_overlap_merges(self):
        self.gh.open_prs[REPO] = []  # no other in-flight PRs
        self.gh.pr_mergeable[(REPO, 42)] = 'MERGEABLE'
        result = self._attempt(42, changed_files=['scripts/foo.py'])
        self.assertEqual(result['merge_outcome'], 'merged')
        self.assertEqual(self.gh.merge_calls, [(REPO, 42)])
        # Queue remains empty.
        self.assertEqual(self.on._load_auto_merge_queue(), [])

    def test_two_non_overlapping_prs_both_merge(self):
        # Different files → no overlap → both merge.
        self.gh.open_prs[REPO] = [
            {'number': 100, 'createdAt': '2026-05-26T10:00:00Z',
             'headRefName': 'b1'},
        ]
        self.gh.pr_files[(REPO, 100)] = ['scripts/aaa.py']
        self.gh.pr_mergeable[(REPO, 100)] = 'MERGEABLE'
        self.gh.pr_mergeable[(REPO, 101)] = 'MERGEABLE'
        r1 = self._attempt(100, changed_files=['scripts/aaa.py'])
        r2 = self._attempt(101, changed_files=['scripts/bbb.py'])
        self.assertEqual(r1['merge_outcome'], 'merged')
        self.assertEqual(r2['merge_outcome'], 'merged')


class SingleBlockerHoldReleaseTest(SerializerTestBase):
    """Overlapping PRs serialize: second waits for first."""

    def test_overlap_holds_then_releases_on_blocker_merge(self):
        # PR-A is open, touches same file as PR-B. PR-A is the blocker.
        self.gh.open_prs[REPO] = [
            {'number': 109, 'createdAt': '2026-05-26T10:00:00Z',
             'headRefName': 'a'},
        ]
        self.gh.pr_files[(REPO, 109)] = ['docs/operating-manual.md']
        self.gh.pr_mergeable[(REPO, 109)] = 'MERGEABLE'
        self.gh.pr_mergeable[(REPO, 112)] = 'MERGEABLE'

        # PR-B's review-pass fires — should hold behind PR-A.
        r_b = self._attempt(112, changed_files=['docs/operating-manual.md'])
        self.assertEqual(r_b['merge_outcome'], 'held_for_blocker')
        self.assertEqual(r_b['blocker_pr_number'], 109)
        # PR-B is NOT merged.
        self.assertEqual(self.gh.merge_calls, [])
        queue = self.on._load_auto_merge_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]['pr_number'], 112)
        self.assertEqual(queue[0]['blocker_pr_number'], 109)

        # PR-A merges (via its own review-pass cycle).
        self.gh.open_prs[REPO] = []
        r_a = self._attempt(109, changed_files=['docs/operating-manual.md'])
        self.assertEqual(r_a['merge_outcome'], 'merged')

        # Post-merge release pass should have re-attempted PR-B; PR-B's
        # gh pr merge should have fired.
        self.assertIn((REPO, 112), self.gh.merge_calls)
        # Queue is now empty.
        self.assertEqual(self.on._load_auto_merge_queue(), [])


class StaleApprovalRevalidationTest(SerializerTestBase):
    """fix-auto-merge-freshness-revalidation — a held PR whose approval
    predates a base change must be re-validated against CURRENT main before
    its auto-merge fires.

    Models the 2026-06-11 PR #455 incident: #455 passed Mirror review, was
    HELD ~6h behind overlapping #454, then auto-fired 11s after #454 merged
    on its now-stale approval — landing a +13-test regression that
    mergeable=CLEAN never caught. The freshness gate re-runs the regression
    gate against the moved base and blocks the stale merge.
    """

    BLOCKER = 454
    HELD = 455
    FILE = 'scripts/agent_runner.py'

    def setUp(self):
        super().setUp()
        # Force the regression re-run ON regardless of the real repo config,
        # so the guard is exercised hermetically. tearDown's
        # _invalidate_loop_bounds_cache() clears this.
        self.on._LOOP_BOUNDS_CACHE['config'] = {
            'auto_merge_queue': {'revalidate_regression_on_release': True},
        }

    def _hold_behind_blocker(self, chat_id=777):
        """Drive HELD's review-pass so it holds behind BLOCKER on overlap.
        Returns the captured approval-time base SHA.
        """
        self.gh.pr_base[(REPO, self.HELD)] = 'baseAAAA00000000'
        self.gh.open_prs[REPO] = [
            {'number': self.BLOCKER, 'createdAt': '2026-06-11T02:00:00Z',
             'headRefName': 'fix-454'},
        ]
        self.gh.pr_files[(REPO, self.BLOCKER)] = [self.FILE]
        self.gh.pr_mergeable[(REPO, self.BLOCKER)] = 'MERGEABLE'
        self.gh.pr_mergeable[(REPO, self.HELD)] = 'MERGEABLE'
        r = self._attempt(self.HELD, changed_files=[self.FILE], chat_id=chat_id)
        self.assertEqual(r['merge_outcome'], 'held_for_blocker')
        self.assertEqual(r['blocker_pr_number'], self.BLOCKER)
        queue = self.on._load_auto_merge_queue()
        self.assertEqual(queue[0]['approved_base_sha'], 'baseAAAA00000000')
        return 'baseAAAA00000000'

    def _merge_blocker(self):
        """Merge BLOCKER (first-attempt path) — triggers the release pass."""
        self.gh.open_prs[REPO] = []
        r = self._attempt(self.BLOCKER, changed_files=[self.FILE])
        self.assertEqual(r['merge_outcome'], 'merged')

    def test_regression_on_moved_base_blocks_stale_auto_merge(self):
        """THE incident: held approval, base moves, regression gate now BLOCKs
        → the held PR must NOT auto-fire; route Larry back to re-review.
        """
        self._hold_behind_blocker(chat_id=777)
        # Blocker merges → main moves. HELD is still mergeable=CLEAN (the
        # regression is semantic, not a textual conflict) but a fresh
        # regression gate against the new main fails.
        self.gh.pr_base[(REPO, self.HELD)] = 'baseBBBB99999999'  # base moved
        self.on._RELEASE_REGRESSION_GATE_FN_OVERRIDE = (
            lambda repo, pr, base, head: 'block'
        )
        self._merge_blocker()

        # The stale approval must NOT have auto-merged HELD.
        self.assertNotIn((REPO, self.HELD), self.gh.merge_calls)
        # It is pulled from the queue (not silently stranded, not re-fired).
        self.assertEqual(self.on._load_auto_merge_queue(), [])
        # Larry is routed back to re-review/rebase.
        notifs = [a for a in self._read_alerts()
                  if a.get('intent') == 'auto_merge_stale_revalidation']
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0]['chat_id'], 777)
        self.assertIn('re-validation against current main failed',
                      notifs[0]['message'])

    def test_clean_revalidation_on_moved_base_merges(self):
        """Held approval, base moves, but the regression gate PASSes against
        current main → the merge proceeds (no over-block).
        """
        self._hold_behind_blocker()
        self.gh.pr_base[(REPO, self.HELD)] = 'baseBBBB99999999'  # base moved
        self.on._RELEASE_REGRESSION_GATE_FN_OVERRIDE = (
            lambda repo, pr, base, head: 'pass'
        )
        self._merge_blocker()
        self.assertIn((REPO, self.HELD), self.gh.merge_calls)
        self.assertEqual(self.on._load_auto_merge_queue(), [])

    def test_unchanged_base_skips_regression_gate_and_merges(self):
        """When the base did NOT move since approval, the (expensive)
        regression gate is not consulted at all — the pre-hold approval is
        still valid and the merge fires.
        """
        self._hold_behind_blocker()
        # Base stays 'baseAAAA...' (blocker closed without moving HELD's base,
        # or the merge didn't touch HELD's mergebase). Override would BLOCK if
        # consulted — assert it is NOT.
        calls = []
        self.on._RELEASE_REGRESSION_GATE_FN_OVERRIDE = (
            lambda repo, pr, base, head: calls.append((pr, base, head)) or 'block'
        )
        self._merge_blocker()
        self.assertEqual(calls, [], 'regression gate must be skipped when base unchanged')
        self.assertIn((REPO, self.HELD), self.gh.merge_calls)
        self.assertEqual(self.on._load_auto_merge_queue(), [])

    def test_unknown_mergeable_at_release_defers_then_revalidates_on_retry(self):
        """The ~11s-after-blocker-merge race: GitHub still reports
        mergeable=UNKNOWN at release. The PR must DEFER (re-queue behind its
        merged blocker) rather than merge blind — and the sweep retry must
        STILL re-validate (no bypass) before merging.
        """
        self._hold_behind_blocker()
        self.gh.pr_base[(REPO, self.HELD)] = 'baseBBBB99999999'  # base moved
        # GitHub hasn't recomputed mergeability yet.
        self.gh.pr_mergeable[(REPO, self.HELD)] = 'UNKNOWN'
        self.on._RELEASE_REGRESSION_GATE_FN_OVERRIDE = (
            lambda repo, pr, base, head: 'block'  # would block IF reached
        )
        self._merge_blocker()

        # Not merged; re-queued behind the (merged) blocker for a sweep retry.
        self.assertNotIn((REPO, self.HELD), self.gh.merge_calls)
        queue = self.on._load_auto_merge_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]['pr_number'], self.HELD)
        self.assertEqual(queue[0]['blocker_pr_number'], self.BLOCKER)

        # GitHub finishes recomputing (now MERGEABLE) and the code is in fact
        # clean against current main — the sweep retry re-validates and merges.
        self.gh.pr_mergeable[(REPO, self.HELD)] = 'MERGEABLE'
        self.on._RELEASE_REGRESSION_GATE_FN_OVERRIDE = (
            lambda repo, pr, base, head: 'pass'
        )
        self.on._auto_merge_queue_sweep()
        self.assertIn((REPO, self.HELD), self.gh.merge_calls)
        self.assertEqual(self.on._load_auto_merge_queue(), [])

    def test_release_defer_preserves_stale_queue_watchdog_clock(self):
        """A transient defer on release must NOT reset queued_at /
        watchdog_dm_sent — else a PR stuck in a persistent-UNKNOWN re-defer
        loop would reset its age every sweep and the stale-queue watchdog
        would NEVER fire (silent strand).
        """
        self._hold_behind_blocker()
        held = self.on._load_auto_merge_queue()[0]
        original_queued_at = held['queued_at']
        # Pretend the watchdog already DMed once for this held entry.
        self.on._queue_update_entry(self.HELD, REPO, {'watchdog_dm_sent': True})

        # Base moves, but GitHub keeps reporting UNKNOWN → release defers.
        self.gh.pr_base[(REPO, self.HELD)] = 'baseBBBB99999999'
        self.gh.pr_mergeable[(REPO, self.HELD)] = 'UNKNOWN'
        self._merge_blocker()

        self.assertNotIn((REPO, self.HELD), self.gh.merge_calls)
        q = self.on._load_auto_merge_queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]['pr_number'], self.HELD)
        # Watchdog clock survived the re-queue (NOT reset to now / False).
        self.assertEqual(q[0]['queued_at'], original_queued_at)
        self.assertTrue(q[0]['watchdog_dm_sent'])
        # And the merged blocker is preserved so the sweep re-releases it.
        self.assertEqual(q[0]['blocker_pr_number'], self.BLOCKER)

    def _revalidate(self, release_entry):
        return self.on._revalidate_held_merge_before_fire(
            pr_url=_pr_url(REPO, self.HELD),
            repo_coords=REPO,
            pr_number=self.HELD,
            task_id='t-held',
            summary='held summary',
            chat_id=777,
            changed_files=[self.FILE],
            release_entry=release_entry,
        )

    def test_release_gate_already_merged_returns_skip_outcome(self):
        """fix-auto-merge-already-merged-skip (a): the released PR is ALREADY
        MERGED (auto-merged via the blocker's base-move) and so permanently
        reports mergeable=UNKNOWN. The gate must return a release_already_merged
        outcome WITHOUT calling _defer_held_revalidation — the entry was already
        removed by _queue_release, so not re-queuing terminates the ~5s loop.
        """
        self.gh.pr_state[(REPO, self.HELD)] = 'MERGED'
        self.gh.pr_mergeable[(REPO, self.HELD)] = 'UNKNOWN'
        with mock.patch.object(self.on, '_defer_held_revalidation') as defer:
            result = self._revalidate(
                {'blocker_pr_number': self.BLOCKER,
                 'approved_base_sha': 'baseAAAA00000000'},
            )
        self.assertIsNotNone(result)
        self.assertEqual(result['merge_outcome'], 'release_already_merged')
        self.assertEqual(result['pr_number'], self.HELD)
        self.assertEqual(result['repo_coords'], REPO)
        # The loop-sustaining re-queue path must NOT be taken.
        defer.assert_not_called()
        # No merge shell-out (the PR is already merged).
        self.assertNotIn((REPO, self.HELD), self.gh.merge_calls)
        # Exactly one skip line; no defer line.
        log_text = self.on.LOG_FILE.read_text(encoding='utf-8')
        self.assertEqual(log_text.count('AUTO_MERGE_SKIP_ALREADY_MERGED'), 1)
        self.assertIn('state=MERGED', log_text)
        self.assertNotIn('AUTO_MERGE_RELEASE_DEFERRED', log_text)

    def test_release_gate_already_closed_returns_skip_outcome(self):
        """fix-auto-merge-already-merged-skip (b): a CLOSED released PR is
        likewise terminal — same skip outcome, no defer.
        """
        self.gh.pr_state[(REPO, self.HELD)] = 'CLOSED'
        self.gh.pr_mergeable[(REPO, self.HELD)] = 'UNKNOWN'
        with mock.patch.object(self.on, '_defer_held_revalidation') as defer:
            result = self._revalidate(
                {'blocker_pr_number': self.BLOCKER,
                 'approved_base_sha': 'baseAAAA00000000'},
            )
        self.assertEqual(result['merge_outcome'], 'release_already_merged')
        defer.assert_not_called()
        log_text = self.on.LOG_FILE.read_text(encoding='utf-8')
        self.assertIn('AUTO_MERGE_SKIP_ALREADY_MERGED', log_text)
        self.assertIn('state=CLOSED', log_text)

    def test_release_gate_open_unknown_still_defers(self):
        """fix-auto-merge-already-merged-skip (c) regression guard: a genuinely
        OPEN PR that hit a transient mergeable=UNKNOWN must STILL route to the
        defer/re-queue path — the terminal-state skip only fires on MERGED/CLOSED.
        """
        self.gh.pr_state[(REPO, self.HELD)] = 'OPEN'
        self.gh.pr_mergeable[(REPO, self.HELD)] = 'UNKNOWN'
        self.gh.pr_base[(REPO, self.HELD)] = 'baseBBBB99999999'
        with mock.patch.object(
            self.on, '_defer_held_revalidation',
            return_value={'merge_outcome': 'deferred_unknown'},
        ) as defer:
            result = self._revalidate(
                {'blocker_pr_number': self.BLOCKER,
                 'approved_base_sha': 'baseAAAA00000000'},
            )
        defer.assert_called_once()
        self.assertEqual(result['merge_outcome'], 'deferred_unknown')
        # The skip line must never fire for an OPEN PR. (defer is mocked, so the
        # log file may not exist — its absence is itself proof of no skip line.)
        log_text = (
            self.on.LOG_FILE.read_text(encoding='utf-8')
            if self.on.LOG_FILE.exists() else ''
        )
        self.assertNotIn('AUTO_MERGE_SKIP_ALREADY_MERGED', log_text)

    def test_already_merged_release_removes_entry_and_terminates_loop(self):
        """End-to-end: a blocker merge whose released PR is already MERGED logs
        one skip line and leaves the queue empty — no re-queue, no defer, and a
        follow-up sweep finds nothing to release (the loop terminates).
        """
        self._hold_behind_blocker()
        # Blocker merges, main moves, and GitHub auto-merged HELD via the
        # base-move so it is already MERGED + permanently mergeable=UNKNOWN.
        self.gh.pr_base[(REPO, self.HELD)] = 'baseBBBB99999999'
        self.gh.pr_mergeable[(REPO, self.HELD)] = 'UNKNOWN'
        self.gh.pr_state[(REPO, self.HELD)] = 'MERGED'
        self._merge_blocker()

        # Entry removed (not re-queued); HELD's merge never re-fired.
        self.assertEqual(self.on._load_auto_merge_queue(), [])
        self.assertNotIn((REPO, self.HELD), self.gh.merge_calls)
        log_text = self.on.LOG_FILE.read_text(encoding='utf-8')
        self.assertIn('AUTO_MERGE_SKIP_ALREADY_MERGED', log_text)
        self.assertNotIn('AUTO_MERGE_RELEASE_DEFERRED', log_text)

        # A follow-up sweep has nothing to release — the loop is broken.
        self.gh.merge_calls.clear()
        self.on._auto_merge_queue_sweep()
        self.assertEqual(self.gh.merge_calls, [])


class ChainedBlockersTest(SerializerTestBase):
    """A < B < C all overlap; A merges → B+C released; C may re-hold behind B."""

    def test_chain_releases_in_fifo_order(self):
        # Three PRs touching same file. A=109 (oldest), B=112, C=113.
        FILE = 'docs/operating-manual.md'
        self.gh.open_prs[REPO] = [
            {'number': 109, 'createdAt': '2026-05-26T10:00:00Z',
             'headRefName': 'a'},
        ]
        self.gh.pr_files[(REPO, 109)] = [FILE]
        self.gh.pr_mergeable.update({
            (REPO, 109): 'MERGEABLE',
            (REPO, 112): 'MERGEABLE',
            (REPO, 113): 'MERGEABLE',
        })

        # B comes through review-pass first (after A), holds behind A.
        r_b = self._attempt(112, changed_files=[FILE])
        self.assertEqual(r_b['merge_outcome'], 'held_for_blocker')
        self.assertEqual(r_b['blocker_pr_number'], 109)

        # C comes through after — finds both A (open) and B (queued)
        # as overlap candidates. FIFO blocker = lowest created_at = A.
        r_c = self._attempt(113, changed_files=[FILE])
        self.assertEqual(r_c['merge_outcome'], 'held_for_blocker')
        self.assertIn(r_c['blocker_pr_number'], (109, 112))

        # A merges.
        self.gh.open_prs[REPO] = []
        r_a = self._attempt(109, changed_files=[FILE])
        self.assertEqual(r_a['merge_outcome'], 'merged')

        # B should have released and merged. C should have either merged
        # OR re-held behind B (which then merged in the chained release).
        # Either way, all three end up merged.
        self.assertIn((REPO, 109), self.gh.merge_calls)
        self.assertIn((REPO, 112), self.gh.merge_calls)
        self.assertIn((REPO, 113), self.gh.merge_calls)
        # Queue empty.
        self.assertEqual(self.on._load_auto_merge_queue(), [])


class BlockerRejectedReleaseTest(SerializerTestBase):
    """Blocker closed without merge → queued PR retried via sweep."""

    def test_blocker_closed_releases_via_sweep(self):
        FILE = 'docs/operating-manual.md'
        self.gh.open_prs[REPO] = [
            {'number': 109, 'createdAt': '2026-05-26T10:00:00Z',
             'headRefName': 'a'},
        ]
        self.gh.pr_files[(REPO, 109)] = [FILE]
        self.gh.pr_mergeable[(REPO, 112)] = 'MERGEABLE'

        r_b = self._attempt(112, changed_files=[FILE])
        self.assertEqual(r_b['merge_outcome'], 'held_for_blocker')

        # Operator closes PR-A without merging.
        self.gh.open_prs[REPO] = []
        self.gh.pr_state[(REPO, 109)] = 'CLOSED'

        # Sweep detects blocker resolved → releases PR-B.
        self.on._auto_merge_queue_sweep()
        self.assertIn((REPO, 112), self.gh.merge_calls)
        self.assertEqual(self.on._load_auto_merge_queue(), [])


class ConflictingMergeableTest(SerializerTestBase):
    """Gate 2: mergeable=CONFLICTING → DM Larry, no merge, queue clear."""

    def test_conflicting_dms_and_skips_merge(self):
        self.gh.open_prs[REPO] = []
        self.gh.pr_mergeable[(REPO, 112)] = 'CONFLICTING'

        r = self._attempt(112, changed_files=['docs/operating-manual.md'],
                          chat_id=12345)
        self.assertEqual(r['merge_outcome'], 'held_conflict')
        # NO gh pr merge call.
        self.assertEqual(self.gh.merge_calls, [])
        # Queue clear.
        self.assertEqual(self.on._load_auto_merge_queue(), [])
        # DM emitted via append_notification.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['kind'], 'notification')
        self.assertEqual(alerts[0]['intent'], 'merge_conflict_manual_rebase')
        self.assertEqual(alerts[0]['chat_id'], 12345)
        self.assertIn('Rebase manually', alerts[0]['message'])


class UnknownMergeableDeferTest(SerializerTestBase):
    """Gate 2: mergeable=UNKNOWN defers one tick, then proceeds."""

    def test_unknown_defers_then_second_attempt_proceeds(self):
        self.gh.open_prs[REPO] = []
        self.gh.pr_mergeable[(REPO, 112)] = 'UNKNOWN'

        # First attempt: UNKNOWN → deferred.
        r1 = self._attempt(112, changed_files=['docs/operating-manual.md'])
        self.assertEqual(r1['merge_outcome'], 'deferred_unknown')
        # No merge yet.
        self.assertEqual(self.gh.merge_calls, [])
        queue = self.on._load_auto_merge_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]['unknown_attempts'], 1)
        self.assertIsNone(queue[0]['blocker_pr_number'])

        # Sweep retries with second_attempt_on_unknown=True. Even if gh
        # still says UNKNOWN, we proceed (let git be the authority).
        self.on._auto_merge_queue_sweep()
        self.assertIn((REPO, 112), self.gh.merge_calls)
        self.assertEqual(self.on._load_auto_merge_queue(), [])


class WatchdogDmTest(SerializerTestBase):
    """24h+ queue entry triggers one watchdog DM; not re-DMed on next sweep."""

    def test_watchdog_dm_fires_once(self):
        FILE = 'docs/operating-manual.md'
        # PR-A blocking — keep it open across the test.
        self.gh.open_prs[REPO] = [
            {'number': 109, 'createdAt': '2026-05-20T10:00:00Z',
             'headRefName': 'a'},
        ]
        self.gh.pr_files[(REPO, 109)] = [FILE]
        self.gh.pr_state[(REPO, 109)] = 'OPEN'
        self.gh.pr_mergeable[(REPO, 112)] = 'MERGEABLE'

        # PR-B held behind PR-A.
        r = self._attempt(112, changed_files=[FILE], chat_id=12345)
        self.assertEqual(r['merge_outcome'], 'held_for_blocker')

        # Backdate the entry's queued_at to 25h ago.
        entries = self.on._load_auto_merge_queue()
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        entries[0]['queued_at'] = old_ts
        self.on._save_auto_merge_queue(entries)

        # Sweep → fires watchdog DM.
        self.on._auto_merge_queue_sweep()
        alerts = self._read_alerts()
        watchdog_alerts = [
            a for a in alerts
            if a.get('intent') == 'auto_merge_queue_stale'
        ]
        self.assertEqual(len(watchdog_alerts), 1)
        # Flag on disk is set.
        entries = self.on._load_auto_merge_queue()
        self.assertTrue(entries[0]['watchdog_dm_sent'])

        # Second sweep → no additional watchdog DM.
        self.on._auto_merge_queue_sweep()
        alerts = self._read_alerts()
        watchdog_alerts = [
            a for a in alerts
            if a.get('intent') == 'auto_merge_queue_stale'
        ]
        self.assertEqual(len(watchdog_alerts), 1)


class CrossRepoIsolationTest(SerializerTestBase):
    """A PR in repo A never blocks a PR in repo B even with same filenames."""

    def test_cross_repo_does_not_block(self):
        FILE = 'docs/operating-manual.md'
        # Open PR in agent-core.
        self.gh.open_prs[REPO] = [
            {'number': 100, 'createdAt': '2026-05-26T10:00:00Z',
             'headRefName': 'a'},
        ]
        self.gh.pr_files[(REPO, 100)] = [FILE]
        # No open PRs in dashboard.
        self.gh.open_prs[DASHBOARD_REPO] = []
        self.gh.pr_mergeable[(DASHBOARD_REPO, 50)] = 'MERGEABLE'

        # Dashboard PR with the same file path — should NOT be blocked.
        r = self._attempt(50, repo=DASHBOARD_REPO, changed_files=[FILE])
        self.assertEqual(r['merge_outcome'], 'merged')
        self.assertEqual(self.gh.merge_calls, [(DASHBOARD_REPO, 50)])


class CorruptQueueFailClosedTest(SerializerTestBase):
    """Corrupt queue file → fail-closed; subsequent attempts all refused."""

    def test_corrupt_queue_refuses_all_merges(self):
        # Write garbage into the queue file.
        self.on.AUTO_MERGE_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.on.AUTO_MERGE_QUEUE_FILE.write_text('{not json{{{')

        # First attempt triggers fail-closed.
        self.gh.pr_mergeable[(REPO, 42)] = 'MERGEABLE'
        r1 = self._attempt(42, changed_files=['scripts/foo.py'])
        # Fail-closed is set after _load was called inside _find_overlap_blocker
        # (the first call); the gates surface 'held_fail_closed' on the next
        # entry into _attempt. The current attempt: gate 1 calls _load (sets
        # fail-closed), returns no blocker (queue is now treated as empty);
        # gate 2 runs and merges. Subsequent attempts get held_fail_closed.
        # (The DM Larry got is the one-shot critical broadcast.)
        self.assertTrue(self.on._AUTO_MERGE_QUEUE_FAIL_CLOSED)

        # Second attempt → held_fail_closed.
        r2 = self._attempt(43, changed_files=['scripts/bar.py'])
        self.assertEqual(r2['merge_outcome'], 'held_fail_closed')
        # No merge call for #43.
        self.assertNotIn((REPO, 43), self.gh.merge_calls)

        # DM Larry was fired once (broadcast via append_alert).
        alerts = self._read_alerts()
        corrupt_alerts = [
            a for a in alerts
            if a.get('subject') == 'auto-merge-queue-corrupt'
        ]
        self.assertGreaterEqual(len(corrupt_alerts), 1)
        self.assertEqual(corrupt_alerts[0].get('severity'), 'critical')


class MergeableStatusMappingTest(SerializerTestBase):
    """`_gh_pr_mergeable_status` maps the GitHub field to the gate tri-state."""

    def test_mergeable_mapping(self):
        self.gh.pr_mergeable[(REPO, 1)] = 'MERGEABLE'
        self.gh.pr_mergeable[(REPO, 2)] = 'CONFLICTING'
        self.gh.pr_mergeable[(REPO, 3)] = 'UNKNOWN'
        self.assertEqual(self.on._gh_pr_mergeable_status(REPO, 1), 'mergeable')
        self.assertEqual(self.on._gh_pr_mergeable_status(REPO, 2), 'conflicting')
        self.assertEqual(self.on._gh_pr_mergeable_status(REPO, 3), 'unknown')


class AtomicWriteTest(SerializerTestBase):
    """Queue file is written via temp + rename (no partial-write window)."""

    def test_atomic_write_via_tmp_rename(self):
        entries = [{'pr_number': 1, 'repo': REPO, 'changed_files': ['a.py']}]
        self.on._save_auto_merge_queue(entries)
        loaded = self.on._load_auto_merge_queue()
        self.assertEqual(loaded, entries)
        # No stray .tmp file left behind.
        tmp_path = self.on.AUTO_MERGE_QUEUE_FILE.with_suffix(
            self.on.AUTO_MERGE_QUEUE_FILE.suffix + '.tmp',
        )
        self.assertFalse(tmp_path.exists())


class FindOverlapBlockerTest(SerializerTestBase):
    """`_find_overlap_blocker` returns the lowest-createdAt overlapping PR."""

    def test_returns_oldest_overlap(self):
        FILE = 'docs/operating-manual.md'
        self.gh.open_prs[REPO] = [
            {'number': 200, 'createdAt': '2026-05-26T11:00:00Z',
             'headRefName': 'b'},
            {'number': 100, 'createdAt': '2026-05-26T09:00:00Z',
             'headRefName': 'a'},
        ]
        self.gh.pr_files[(REPO, 100)] = [FILE]
        self.gh.pr_files[(REPO, 200)] = [FILE]
        blocker = self.on._find_overlap_blocker(999, REPO, [FILE])
        self.assertEqual(blocker, 100)

    def test_returns_none_when_no_overlap(self):
        self.gh.open_prs[REPO] = [
            {'number': 100, 'createdAt': '2026-05-26T09:00:00Z',
             'headRefName': 'a'},
        ]
        self.gh.pr_files[(REPO, 100)] = ['scripts/aaa.py']
        blocker = self.on._find_overlap_blocker(999, REPO, ['scripts/bbb.py'])
        self.assertIsNone(blocker)

    def test_excludes_self(self):
        FILE = 'docs/operating-manual.md'
        self.gh.open_prs[REPO] = [
            {'number': 999, 'createdAt': '2026-05-26T09:00:00Z',
             'headRefName': 'a'},
        ]
        self.gh.pr_files[(REPO, 999)] = [FILE]
        blocker = self.on._find_overlap_blocker(999, REPO, [FILE])
        self.assertIsNone(blocker)


class ReleaseRegressionGateRunnerTest(unittest.TestCase):
    """fix-auto-merge-freshness-revalidation — the PRODUCTION regression
    runner `_run_release_regression_gate` (override OFF). Pins the
    exit-code→verdict mapping and the fail-closed / skip / error branches
    that the freshness guard's safety depends on.
    """

    def setUp(self):
        import outbox_notifier as on
        self.on = on
        on._reset_auto_merge_queue_state()  # ensure override is None
        self._orig_canon = on._canonical_repo_for_coords
        self._tmp = tempfile.TemporaryDirectory()
        self._repo = Path(self._tmp.name)

    def tearDown(self):
        self.on._canonical_repo_for_coords = self._orig_canon
        self.on._reset_auto_merge_queue_state()
        self._tmp.cleanup()

    def _set_repo(self, path):
        self.on._canonical_repo_for_coords = lambda coords: path

    def _make_python_repo(self):
        (self._repo / 'scripts' / 'tests').mkdir(parents=True, exist_ok=True)
        return self._repo

    def test_unresolvable_repo_is_error(self):
        self._set_repo(None)
        verdict = self.on._run_release_regression_gate(REPO, 5, 'b', 'h')
        self.assertEqual(verdict, 'error')

    def test_missing_sha_is_error(self):
        self._set_repo(self._make_python_repo())
        self.assertEqual(
            self.on._run_release_regression_gate(REPO, 5, None, 'h'), 'error')
        self.assertEqual(
            self.on._run_release_regression_gate(REPO, 5, 'b', None), 'error')

    def test_repo_without_scripts_tests_skips(self):
        # Repo exists but has no scripts/tests — gate N/A → skip (merge).
        self._set_repo(self._repo)
        verdict = self.on._run_release_regression_gate(REPO, 5, 'base', 'head')
        self.assertEqual(verdict, 'skip')

    def _run_with_returncode(self, rc):
        """Drive the runner with a python suite present, stubbing subprocess so
        `git fetch` succeeds and the test_regression_check shell-out returns
        exit code `rc`."""
        self._set_repo(self._make_python_repo())

        def _stub(cmd, capture_output=True, text=True, timeout=None):
            if cmd[:2] == ['git', '-C'] or (len(cmd) > 1 and cmd[1] == '-C'):
                return _CompletedProc(returncode=0)  # fetch: succeed
            return _CompletedProc(stdout='{}', returncode=rc)  # the gate run

        with mock.patch.object(self.on.subprocess, 'run', side_effect=_stub):
            return self.on._run_release_regression_gate(REPO, 5, 'base', 'head')

    def test_exit_zero_is_pass(self):
        self.assertEqual(self._run_with_returncode(0), 'pass')

    def test_exit_one_is_block(self):
        self.assertEqual(self._run_with_returncode(1), 'block')

    def test_exit_two_is_error(self):
        self.assertEqual(self._run_with_returncode(2), 'error')

    def test_runner_timeout_is_error(self):
        self._set_repo(self._make_python_repo())

        def _stub(cmd, capture_output=True, text=True, timeout=None):
            if len(cmd) > 1 and cmd[1] == '-C':
                return _CompletedProc(returncode=0)  # fetch ok
            raise subprocess.TimeoutExpired(cmd, timeout or 1)

        with mock.patch.object(self.on.subprocess, 'run', side_effect=_stub):
            verdict = self.on._run_release_regression_gate(
                REPO, 5, 'base', 'head')
        self.assertEqual(verdict, 'error')


class MergeReconcilesNoSessionDecisionTest(SerializerTestBase):
    """Fix 2 (PR #805 incident): a merged PR resolves any still-pending
    session-less decision approval to 'expired', wired at the auto-merge
    chokepoint so every merge path funnels through it."""

    def setUp(self):
        super().setUp()
        import beacon_approval_handler as approval
        self.approval = approval
        self._orig_pending = approval.PENDING_APPROVALS_PATH
        approval.PENDING_APPROVALS_PATH = (
            self._root / 'state' / 'beacon-pending-approvals.json'
        )
        self.addCleanup(
            setattr, approval, 'PENDING_APPROVALS_PATH', self._orig_pending)

    def _seed(self, approval_id):
        self.approval.add_pending(
            {'task_id': approval_id, 'summary': 's', 'target_agent': 'forge',
             'prompt': 'p'},
            chat_id=12345,
        )

    def test_merged_pr_expires_pending_decision(self):
        task_id = 'pr-ourliberty-agent-core-42'
        self._seed(f'mirror-review-{task_id}-deadbeef')
        self.gh.open_prs[REPO] = []
        self.gh.pr_mergeable[(REPO, 42)] = 'MERGEABLE'
        result = self._attempt(
            42, task_id=task_id, changed_files=['scripts/foo.py'])
        self.assertEqual(result['merge_outcome'], 'merged')
        s = self.approval.load_state()
        self.assertEqual(s.get('pending', []), [])
        hist = s.get('history', [])
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]['status'], 'expired')

    def test_merge_without_matching_approval_is_noop(self):
        # An unrelated pending approval must survive an unrelated PR merge.
        self._seed('mirror-review-pr-ourliberty-agent-core-999-cafe')
        self.gh.open_prs[REPO] = []
        self.gh.pr_mergeable[(REPO, 42)] = 'MERGEABLE'
        result = self._attempt(
            42, task_id='pr-ourliberty-agent-core-42',
            changed_files=['scripts/foo.py'])
        self.assertEqual(result['merge_outcome'], 'merged')
        self.assertEqual(len(self.approval.load_state().get('pending', [])), 1)


if __name__ == '__main__':
    unittest.main()
