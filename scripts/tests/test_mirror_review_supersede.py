"""Tests for scripts/mirror_review_supersede.py and its two wiring sites.

Uses unittest (repo convention; pytest isn't installed on the droplet).

The behavior under test is a CANCEL, so every test here is written to fail if
the fix is reverted — the whole point is that an existence gate ("is there a
supersede call?") proves nothing. The mutation notes in
`docs/` are not a substitute; each test below names the specific mutation it
survives.

Covers:
- find_live_review_records: queued (age-independent) + claimed legs, the
  CLAIM-time aging that mtime alone gets wrong, the rev/replan round-name
  grammar, and the deliberate blindness to .archive/.invalid
- _archive_superseded: record leaves the inbox, envelope preserved, collisions
  disambiguated rather than clobbered
- await_cancel_ack: acks on marker deletion, and on timeout CLEARS the marker
  (the clobber trap — a stale marker would cancel the Forge task we dispatch)
- supersede_live_review: end-to-end per record state, no cancel marker for a
  queued-only task, never raises
- the wiring: outbox_notifier._dispatch_revision_to_forge supersedes before the
  Forge write, and beacon_approval_handler.dispatch_approved supersedes only
  for a card carrying a recheck_target
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import mirror_review_supersede as mrs  # noqa: E402
import task_cancel  # noqa: E402

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
TASK = 'pr-RSDPM-142'
STEM = 'review-pr-RSDPM-142'


class _RootCase(unittest.TestCase):
    """A throwaway agents root with Mirror's inbox laid out as production has
    it (`inboxes/mirror/{,.claimed/<slot>/,.archive/,.invalid/}`)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.inbox = self.root / 'inboxes' / 'mirror'
        for sub in ('', '.archive', '.invalid', '.claimed/0', '.claimed/1'):
            (self.inbox / sub).mkdir(parents=True, exist_ok=True)
        (self.root / 'blackboard').mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._tmp.cleanup)

    def write_record(self, where: Path, name: str, *,
                     age_minutes: float = 0.0,
                     claim_age_minutes: float | None = None) -> Path:
        """Write a review record. `age_minutes` sets mtime (= DISPATCH time,
        which a rename preserves); `claim_age_minutes` sets ctime by writing the
        file that long ago and renaming it into place, reproducing what a real
        `.claimed/` claim does to the two clocks."""
        p = where / name
        p.write_text(json.dumps({'task_id': TASK, 'phase': 'review'}))
        if claim_age_minutes is not None:
            # Real claim: mtime stays at DISPATCH, ctime bumps at CLAIM. We
            # cannot move ctime backwards, so emulate the inverse — an OLD
            # mtime with a NOW ctime — which is exactly the shape that fools a
            # bare-mtime reader.
            dispatch_ts = (NOW - timedelta(minutes=age_minutes)).timestamp()
            os.utime(p, (dispatch_ts, dispatch_ts))
        else:
            ts = (NOW - timedelta(minutes=age_minutes)).timestamp()
            os.utime(p, (ts, ts))
        return p


class TestFindLiveReviewRecords(_RootCase):
    def test_queued_record_is_found_regardless_of_age(self):
        # A queued task has no execution ceiling — the watcher claims it
        # whenever a slot frees — so age must not expire it.
        self.write_record(self.inbox, f'{STEM}.json', age_minutes=6000)
        found = mrs.find_live_review_records(STEM, self.inbox, now=NOW)
        self.assertEqual([r.kind for r in found], ['queued'])

    def test_claimed_record_within_window_is_found(self):
        self.write_record(self.inbox / '.claimed' / '1', f'{STEM}.json')
        found = mrs.find_live_review_records(
            STEM, self.inbox, now=datetime.now(timezone.utc))
        self.assertEqual([r.kind for r in found], ['claimed'])

    def test_claimed_record_past_window_is_not_found(self):
        # Past the session ceiling heal_orphaned_mirror_claims owns it;
        # cancelling a ghost would be pure waste. Aged against the REAL clock:
        # ctime is the claim and no platform lets a test move it backwards, so
        # the only way to make a claim look old is to look at it from later.
        self.write_record(self.inbox / '.claimed' / '1', f'{STEM}.json')
        later = datetime.now(timezone.utc) + timedelta(
            minutes=mrs.CLAIM_LIVE_WINDOW_MINUTES + 10)
        self.assertEqual(
            mrs.find_live_review_records(STEM, self.inbox, now=later), [])

    def test_claim_is_aged_from_ctime_not_mtime(self):
        """MUTATION GUARD. Replace `max(st_mtime, st_ctime)` with `st_mtime`
        and this fails: a review that queued for longer than the window before
        a slot picked it up reads as expired at exactly the moment it STARTED
        executing — protected while merely queued, unprotected while running."""
        p = self.write_record(self.inbox / '.claimed' / '0', f'{STEM}.json')
        # mtime = DISPATCH, 90 min ago — older than the 45-min window. ctime is
        # the CLAIM, which just happened. This is the real shape: the review sat
        # queued under slot saturation and a slot picked it up seconds ago.
        real_now = datetime.now(timezone.utc)
        old = (real_now - timedelta(minutes=90)).timestamp()
        os.utime(p, (old, old))
        st = p.stat()
        self.assertLess(st.st_mtime, st.st_ctime,
                        'fixture precondition: ctime must be the newer clock')
        found = mrs.find_live_review_records(STEM, self.inbox, now=real_now)
        self.assertEqual(
            [r.kind for r in found], ['claimed'],
            'a bare-mtime reading stops protecting the review at exactly the '
            'moment it starts executing',
        )

    def test_matches_revision_and_replan_round_names(self):
        # The #865 drift: a scanner blind to the round suffixes sees nothing.
        for name in (f'{STEM}-rev2.json', f'{STEM}-replan1-rev1.json',
                     f'{STEM}.1.json'):
            self.write_record(self.inbox, name)
        found = mrs.find_live_review_records(STEM, self.inbox, now=NOW)
        self.assertEqual(len(found), 3)

    def test_ignores_a_different_task_with_a_shared_prefix(self):
        self.write_record(self.inbox, f'{STEM}-extra.json')
        self.assertEqual(mrs.find_live_review_records(STEM, self.inbox, now=NOW), [])

    def test_ignores_archived_and_invalid_records(self):
        """A record in .archive/.invalid is a review that CONCLUDED. Treating
        it as live would fire a cancel on every task ever reviewed."""
        self.write_record(self.inbox / '.archive', f'{STEM}.json')
        self.write_record(self.inbox / '.invalid', f'{STEM}.json')
        self.assertEqual(mrs.find_live_review_records(STEM, self.inbox, now=NOW), [])

    def test_missing_inbox_returns_empty_not_raises(self):
        self.assertEqual(
            mrs.find_live_review_records(STEM, self.root / 'nope', now=NOW), [])


class TestArchiveSuperseded(_RootCase):
    def test_record_leaves_the_inbox_and_envelope_is_preserved(self):
        p = self.write_record(self.inbox, f'{STEM}.json')
        rec = mrs.LiveReviewRecord(path=p, kind='queued')
        name = mrs._archive_superseded(rec, self.inbox, NOW)
        self.assertIsNotNone(name)
        self.assertFalse(p.exists())
        dest = self.inbox / '.archive' / name
        self.assertIn('.superseded-', name)
        self.assertEqual(json.loads(dest.read_text())['task_id'], TASK)

    def test_same_second_collision_does_not_clobber(self):
        # The archive is the only surviving copy of the envelope.
        first = self.write_record(self.inbox, f'{STEM}.json')
        n1 = mrs._archive_superseded(
            mrs.LiveReviewRecord(path=first, kind='queued'), self.inbox, NOW)
        second = self.write_record(self.inbox, f'{STEM}.json')
        n2 = mrs._archive_superseded(
            mrs.LiveReviewRecord(path=second, kind='queued'), self.inbox, NOW)
        self.assertNotEqual(n1, n2)
        self.assertEqual(len(list((self.inbox / '.archive').glob('*.json'))), 2)

    def test_vanished_record_reports_failure_not_raises(self):
        rec = mrs.LiveReviewRecord(path=self.inbox / 'gone.json', kind='queued')
        self.assertIsNone(mrs._archive_superseded(rec, self.inbox, NOW))


class TestAwaitCancelAck(_RootCase):
    def test_returns_true_when_marker_disappears(self):
        task_cancel.request_cancel(self.root, TASK, reason='x')
        marker = task_cancel.cancel_marker_path(self.root, TASK)

        def _sleep(_s):  # agent_runner clears it on its first poll
            marker.unlink(missing_ok=True)

        clock = iter([0.0, 1.0, 2.0, 3.0, 4.0])
        self.assertTrue(mrs.await_cancel_ack(
            self.root, TASK, timeout_seconds=20.0,
            sleep=_sleep, now=lambda: next(clock)))

    def test_timeout_clears_the_marker_and_reports_false(self):
        """MUTATION GUARD. Drop the `clear_cancel` on the timeout path and this
        fails — and in production the leftover marker cancels the Forge task
        being dispatched (same task_stem), turning supersede into the strand
        the design forbids."""
        task_cancel.request_cancel(self.root, TASK, reason='x')
        marker = task_cancel.cancel_marker_path(self.root, TASK)
        clock = iter([0.0, 5.0, 25.0])
        acked = mrs.await_cancel_ack(
            self.root, TASK, timeout_seconds=20.0,
            sleep=lambda _s: None, now=lambda: next(clock))
        self.assertFalse(acked)
        self.assertFalse(marker.exists())

    def test_absent_marker_acks_immediately(self):
        self.assertTrue(mrs.await_cancel_ack(
            self.root, TASK, sleep=lambda _s: None, now=lambda: 0.0))


class TestSupersedeLiveReview(_RootCase):
    def _supersede(self, **kw):
        # `cancel_timeout_seconds=0` unless a test overrides it: the ack wait
        # uses a real monotonic clock, so the default 20s bound would burn 20
        # real seconds per test on a fake session that can never ack.
        kw.setdefault('cancel_timeout_seconds', 0.0)
        # Real `now`: a `.claimed/` record is aged from ctime, which no test can
        # move, so a frozen NOW in the past would age every claim NEGATIVELY and
        # exercise a path production never takes.
        return mrs.supersede_live_review(
            TASK, STEM, reason='r', root=self.root,
            now=datetime.now(timezone.utc), sleep=lambda _s: None, **kw)

    def test_no_live_review_is_a_silent_no_op(self):
        out = self._supersede()
        self.assertFalse(out.attempted)
        self.assertEqual(out.archived, [])
        self.assertFalse(out.cancel_requested)
        self.assertFalse(
            task_cancel.cancel_marker_path(self.root, TASK).exists())

    def test_queued_review_is_archived_and_no_cancel_marker_is_written(self):
        """A queued record has spawned nothing, so a cancel marker would sit on
        disk with no reader — and would then cancel the Forge revision, which
        shares the task stem."""
        p = self.write_record(self.inbox, f'{STEM}.json')
        out = self._supersede()
        self.assertTrue(out.attempted)
        self.assertFalse(p.exists())
        self.assertEqual(len(out.archived), 1)
        self.assertFalse(out.cancel_requested)
        self.assertIsNone(out.cancel_acked)
        self.assertFalse(
            task_cancel.cancel_marker_path(self.root, TASK).exists())

    def test_claimed_review_is_archived_before_the_cancel_is_requested(self):
        """ORDER GUARD. The claim must stop being claimable BEFORE the session
        is asked to stop: the slot loop's `finally` re-queues a claimed file
        that still exists when process_task returns, and
        heal_orphaned_mirror_claims re-injects a not-concluded claim. Swap the
        order and the cancelled review comes straight back."""
        claim = self.write_record(
            self.inbox / '.claimed' / '1', f'{STEM}.json', age_minutes=3)
        seen: dict = {}
        real = task_cancel.request_cancel

        def _spy(root, stem, **kw):
            seen['claim_gone'] = not claim.exists()
            return real(root, stem, **kw)

        with mock.patch.object(mrs.task_cancel, 'request_cancel', _spy), \
                mock.patch.object(mrs, '_session_live',
                                  return_value=(True, 'live_worktree_proc')):
            out = self._supersede()
        self.assertTrue(out.cancel_requested)
        self.assertTrue(seen['claim_gone'])
        self.assertFalse(claim.exists())
        self.assertEqual(len(out.archived), 1)

    def test_claimed_review_marker_is_cleared_when_a_live_session_never_acks(self):
        self.write_record(self.inbox / '.claimed' / '0', f'{STEM}.json')
        with mock.patch.object(mrs, '_session_live',
                               return_value=(True, 'live_worktree_proc')):
            out = self._supersede(cancel_timeout_seconds=0.0)
        self.assertTrue(out.cancel_requested)
        self.assertIs(out.cancel_acked, False)
        self.assertFalse(
            task_cancel.cancel_marker_path(self.root, TASK).exists())

    def test_every_round_record_is_superseded_not_just_the_first(self):
        self.write_record(self.inbox, f'{STEM}.json')
        self.write_record(self.inbox / '.claimed' / '1', f'{STEM}-rev1.json',
                          age_minutes=2)
        out = self._supersede(cancel_timeout_seconds=0.0)
        self.assertEqual(len(out.archived), 2)
        self.assertEqual(
            list(self.inbox.glob('review-*.json')), [],
            'the queued round must not survive')

    def test_archive_failure_is_reported_and_does_not_raise(self):
        self.write_record(self.inbox, f'{STEM}.json')
        with mock.patch.object(mrs, '_archive_superseded', return_value=None):
            out = self._supersede()
        self.assertTrue(out.attempted)
        self.assertEqual(out.archived, [])
        self.assertTrue(any(f.startswith('archive-failed:') for f in out.failures))

    def test_never_raises_when_the_scan_blows_up(self):
        with mock.patch.object(
            mrs, 'find_live_review_records', side_effect=RuntimeError('boom'),
        ):
            out = self._supersede()
        self.assertEqual(out.failures, ['RuntimeError'])
        self.assertFalse(out.attempted)

    def test_log_line_is_content_free(self):
        self.write_record(self.inbox, f'{STEM}.json')
        out = self._supersede()
        line = out.log_line(TASK)
        self.assertIn('MIRROR_REVIEW_SUPERSEDED', line)
        self.assertIn(TASK, line)
        self.assertNotIn('phase', line)  # no envelope body ever reaches a log


class TestMirrorReviewSessionLive(unittest.TestCase):
    """`pipeline_live_state.mirror_review_session_live` — the task_id-keyed
    liveness probe. `pr_review_in_progress` is keyed on PR coordinates and only
    ever matches `wt-mirror-pr-<repo>-<N>`, so a Forge-built PR's review
    (`wt-mirror-<task-slug>`) is invisible to it; that blindness is the whole
    reason this probe exists."""

    def setUp(self) -> None:
        import pipeline_live_state
        self.pls = pipeline_live_state
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'state' / 'in-flight').mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {'OURLIBERTY_AGENTS_ROOT': str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_live_claude_in_the_forge_task_worktree_is_detected(self):
        """The case `pr_review_in_progress` structurally cannot see."""
        import worktree_manager
        task = 'fix-thing-001'
        wt = str(worktree_manager.worktree_path_for('mirror', task))
        with mock.patch.object(self.pls, '_claude_pids', return_value=[42]), \
                mock.patch.object(self.pls, '_proc_cwd', return_value=wt):
            self.assertEqual(
                self.pls.mirror_review_session_live(task),
                (True, 'live_worktree_proc'))

    def test_sibling_worktree_prefix_does_not_match(self):
        import worktree_manager
        task = 'fix-thing-001'
        sibling = str(worktree_manager.worktree_path_for('mirror', task)) + '-2'
        with mock.patch.object(self.pls, '_claude_pids', return_value=[42]), \
                mock.patch.object(self.pls, '_proc_cwd', return_value=sibling):
            self.assertEqual(
                self.pls.mirror_review_session_live(task), (False, ''))

    def test_deleted_cwd_suffix_is_normalized_off(self):
        import worktree_manager
        task = 'fix-thing-001'
        wt = str(worktree_manager.worktree_path_for('mirror', task))
        with mock.patch.object(self.pls, '_claude_pids', return_value=[42]), \
                mock.patch.object(self.pls, '_proc_cwd',
                                  return_value=wt + ' (deleted)'):
            self.assertTrue(self.pls.mirror_review_session_live(task)[0])

    def test_live_in_flight_marker_is_detected(self):
        (self.root / 'state' / 'in-flight' / 'fix-thing-001.json').write_text(
            json.dumps({'pid': os.getpid(), 'agent_id': 'mirror'}))
        with mock.patch.object(self.pls, '_claude_pids', return_value=[]):
            self.assertEqual(
                self.pls.mirror_review_session_live('fix-thing-001'),
                (True, 'in_flight_marker'))

    def test_stale_in_flight_marker_with_a_dead_pid_is_not_live(self):
        """MUTATION GUARD. Drop the `_pid_alive` check and a stranded registry
        entry reads as a running review forever."""
        (self.root / 'state' / 'in-flight' / 'fix-thing-001.json').write_text(
            json.dumps({'pid': 999999999}))
        with mock.patch.object(self.pls, '_claude_pids', return_value=[]):
            self.assertEqual(
                self.pls.mirror_review_session_live('fix-thing-001'),
                (False, ''))

    def test_nonpositive_pid_is_not_live(self):
        # pid <= 0 targets a process GROUP / every process; os.kill would
        # succeed and read as "alive".
        (self.root / 'state' / 'in-flight' / 'fix-thing-001.json').write_text(
            json.dumps({'pid': 0}))
        with mock.patch.object(self.pls, '_claude_pids', return_value=[]):
            self.assertFalse(
                self.pls.mirror_review_session_live('fix-thing-001')[0])

    def test_nothing_running_is_not_live(self):
        with mock.patch.object(self.pls, '_claude_pids', return_value=[]):
            self.assertEqual(
                self.pls.mirror_review_session_live('fix-thing-001'), (False, ''))

    def test_empty_task_id_and_probe_errors_fail_safe(self):
        self.assertEqual(self.pls.mirror_review_session_live(''), (False, ''))
        with mock.patch.object(
            self.pls, '_claude_pids', side_effect=RuntimeError('boom'),
        ):
            self.assertEqual(
                self.pls.mirror_review_session_live('x'), (False, ''))


class TestSupersedeUsesLiveness(_RootCase):
    def test_live_session_gets_the_full_ack_wait(self):
        self.write_record(self.inbox / '.claimed' / '0', f'{STEM}.json')
        waited = []
        with mock.patch.object(
            mrs, '_session_live', return_value=(True, 'live_worktree_proc'),
        ), mock.patch.object(
            mrs, 'await_cancel_ack', side_effect=lambda *a, **k: waited.append(1) or True,
        ):
            out = mrs.supersede_live_review(
                TASK, STEM, reason='r', root=self.root,
                now=datetime.now(timezone.utc), sleep=lambda _s: None)
        self.assertEqual(len(waited), 1)
        self.assertIs(out.cancel_acked, True)
        self.assertEqual(out.session_reason, 'live_worktree_proc')

    def test_dead_session_skips_the_wait_and_clears_the_marker(self):
        """MUTATION GUARD. Wait unconditionally and this fails — and in
        production every revision that races `process_task`'s
        outbox-then-archive ordering stalls the notifier for the full ack
        window on a session that already exited."""
        self.write_record(self.inbox / '.claimed' / '0', f'{STEM}.json')
        with mock.patch.object(
            mrs, '_session_live', return_value=(False, ''),
        ), mock.patch.object(
            mrs, 'await_cancel_ack', side_effect=AssertionError('must not wait'),
        ):
            out = mrs.supersede_live_review(
                TASK, STEM, reason='r', root=self.root,
                now=datetime.now(timezone.utc), sleep=lambda _s: None)
        self.assertTrue(out.cancel_requested)
        self.assertIs(out.cancel_acked, False)
        self.assertEqual(
            out.session_reason, '',
            'an empty session_reason is what gates the UNACKED warning off for '
            'this benign shape; a value here would bury the real signal',
        )
        self.assertFalse(
            task_cancel.cancel_marker_path(self.root, TASK).exists(),
            'a marker left behind would cancel the Forge task being dispatched',
        )


class TestReviewStemSpellingIsSingleSourced(unittest.TestCase):
    """The notifier writes the record; two other modules go looking for it. If
    the three spellings drift, the supersede scans for a name that was never
    written and silently finds nothing — a false-clean, not a crash."""

    def test_notifier_and_approval_handler_agree(self):
        import beacon_approval_handler as bah
        import outbox_notifier as on
        for task_id in ('pr-RSDPM-142', 'fix-thing-001', 'weird:id@x',
                        'has space'):
            self.assertEqual(
                on.review_stem_for_task(task_id),
                bah._review_stem_for_task(task_id),
                f'stem spelling drifted for {task_id!r}',
            )

    def test_stem_matches_what_dispatch_mirror_review_writes(self):
        import outbox_notifier as on
        import safe_write_inbox
        task_id = 'pr-RSDPM-142'
        written = safe_write_inbox.canonical_inbox_name(f'review-{task_id}.json')
        self.assertTrue(
            on.review_record_name_re(on.review_stem_for_task(task_id))
            .fullmatch(written)
        )


class TestNotifierWiring(_RootCase):
    """`_dispatch_revision_to_forge` must supersede on the branch that writes a
    NEW Forge task, and must NOT on the idempotent re-process branch (which
    fires a moment after a legitimate re-review of the LANDED revision)."""

    def setUp(self) -> None:
        super().setUp()
        import outbox_notifier as on
        self.on = on

    def _run_dispatch(self, *, already_dispatched: bool):
        on = self.on
        forge_inbox = self.root / 'inboxes' / 'forge'
        forge_inbox.mkdir(parents=True, exist_ok=True)
        if already_dispatched:
            (forge_inbox / f'revision-{TASK}-1.json').write_text('{}')
        data = {
            'task_id': TASK, 'target_repo': 'Larry-Yatch/RSDPM',
            'branch': 'forge/x', 'pr_url': 'https://github.com/Larry-Yatch/RSDPM/pull/142',
            'forge_build_session_id': 'sess-abc', 'revision_count': 0,
        }
        decision = {'marker_type': 'review_revision', 'payload': {'findings': []}}
        calls = []
        with mock.patch.object(
            on.safe_write_inbox, 'INBOXES_ROOT', self.root / 'inboxes',
        ), mock.patch.object(
            on.safe_write_inbox, 'safe_write_inbox',
            side_effect=lambda **kw: forge_inbox / kw['filename'],
        ), mock.patch.object(
            on, '_enforce_cost_budget', return_value=True,
        ), mock.patch.object(
            on.revision_in_flight_ledger, 'mark_in_flight',
        ), mock.patch.object(
            on, 'log',
        ), mock.patch.object(
            on, '_supersede_live_review_before_forge',
            side_effect=lambda *a, **k: calls.append(a),
        ):
            on._dispatch_revision_to_forge(data, decision)
        return calls

    def test_supersedes_before_writing_a_new_forge_revision(self):
        """MUTATION GUARD. Delete the call in `_dispatch_revision_to_forge` and
        this fails."""
        calls = self._run_dispatch(already_dispatched=False)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], TASK)

    def test_does_not_supersede_on_the_idempotent_reprocess_branch(self):
        calls = self._run_dispatch(already_dispatched=True)
        self.assertEqual(calls, [])


class TestApprovalWiring(_RootCase):
    """Approving a `mirror-review-*` escalation card must supersede, and must
    still DISPATCH — suppressing the write would resolve the card and strand
    the PR (the approval-reject-strands-pr shape)."""

    def setUp(self) -> None:
        super().setUp()
        import beacon_approval_handler as bah
        self.bah = bah

    def _entry(self, *, with_target: bool):
        payload = {
            'task_id': f'mirror-review-{TASK}-abcd1234',
            'summary': 's', 'prompt': 'p', 'target_agent': 'forge',
        }
        if with_target:
            payload['recheck_target'] = {
                'task_id': TASK,
                'pr_url': 'https://github.com/Larry-Yatch/RSDPM/pull/142',
                'target_repo': 'Larry-Yatch/RSDPM',
                'head_sha': 'abcd1234' * 5, 'round': 1, 'replan_count': 0,
            }
        return {'id': payload['task_id'], 'dispatch_payload': payload,
                'target_agent': 'forge', 'chat_id': 7}

    def _dispatch(self, entry):
        bah = self.bah
        seen = []
        with mock.patch.object(
            bah.safe_write_inbox, 'safe_write_inbox',
            side_effect=lambda **kw: Path('/tmp') / kw['filename'],
        ), mock.patch.object(
            bah, '_supersede_live_review_for_approval',
            side_effect=lambda e, p: seen.append(p.get('recheck_target')),
        ):
            written = bah.dispatch_approved(entry)
        return seen, written

    def test_approve_supersedes_and_still_dispatches(self):
        """MUTATION GUARD, both directions. Remove the supersede call and the
        first assertion fails; turn the supersede into a suppressing guard that
        returns before the write and the second fails."""
        seen, written = self._dispatch(self._entry(with_target=True))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['task_id'], TASK)
        self.assertEqual(written.name, f'mirror-review-{TASK}-abcd1234.json')

    def test_supersede_is_scoped_to_cards_carrying_a_recheck_target(self):
        # Every non-escalation approval (kickoffs, graduations, direction asks)
        # must be untouched — they are not PR reviews.
        entry = self._entry(with_target=False)
        with mock.patch.object(
            self.bah, 'mirror_review_supersede', create=True,
        ):
            pass
        with mock.patch.object(mrs, 'supersede_live_review') as spy:
            self.bah._supersede_live_review_for_approval(
                entry, entry['dispatch_payload'])
        spy.assert_not_called()

    def test_escalation_card_reaches_the_supersede_helper(self):
        entry = self._entry(with_target=True)
        with mock.patch.object(
            mrs, 'supersede_live_review',
            return_value=mrs.SupersedeOutcome(),
        ) as spy:
            self.bah._supersede_live_review_for_approval(
                entry, entry['dispatch_payload'])
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[0], TASK)

    def test_helper_never_raises_when_supersede_blows_up(self):
        entry = self._entry(with_target=True)
        with mock.patch.object(
            mrs, 'supersede_live_review', side_effect=RuntimeError('boom'),
        ):
            self.bah._supersede_live_review_for_approval(
                entry, entry['dispatch_payload'])  # must not raise


if __name__ == '__main__':
    unittest.main()
