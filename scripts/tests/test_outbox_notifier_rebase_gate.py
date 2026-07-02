#!/usr/bin/env python3
"""Tests for the post-open auto-rebase gate (forge-post-open-mergeable-rebase-001).

Covers the four success criteria of the Layer-2 mechanical guarantee in
`outbox_notifier`:
  (1) a PR that opens CONFLICTING (main advanced) is auto-rebased and reaches
      Mirror with zero human action;
  (2) a rebase that conflicts surfaces a Beacon-routed blocker, never a stranded
      PR or half-rebased branch (the abort/blocker behavior is Forge-side; here
      we assert the notifier leaves the obligation OPEN and does not dispatch
      Mirror when no `PR updated:` comes back);
  (3) Mirror is never dispatched onto a CONFLICTING PR;
  (4) the notifier check + durable obligation fire even if Forge's in-session
      step does not.

stdlib unittest only — pytest is NOT installed in the droplet environment.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
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

import outbox_notifier as on        # noqa: E402
import rebase_obligation_ledger as rol  # noqa: E402
import routing_validator as rv      # noqa: E402
import safe_write_inbox as swi      # noqa: E402

try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook name
    # The integration tests below drive the inbox-write chokepoint against an
    # already-isolated tmp tree (per-class path reroutes + mocked gh/cost/
    # sequence helpers), so opt out of the Layer B guard the same way
    # test_outbox_notifier.py does. The #428 real-tree scan still runs at exit.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook name
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


PR = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/777'


def _build_outbox(**overrides):
    base = {
        'task_id': 'rebase-task-1',
        'agent': 'forge',
        'source': 'beacon',
        'phase': 'build',
        'target_repo': 'ourliberty-agent-core',
        'branch': 'forge/rebase-task-1',
        'reply_chat_id': None,
        'exit_code': 0,
        'claude_session_id': 'sess-build-xyz',
        'head_sha': 'deadbeef',
        'result': f'PR opened: {PR}\n\nBuilt the thing.',
        'cost_usd': 0.02,
    }
    base.update(overrides)
    return base


# --------------------------- poll helper (UNKNOWN) ---------------------------

class PollMergeableTest(unittest.TestCase):
    def test_settles_on_conflicting_after_unknown(self):
        slept = []
        with mock.patch.object(
            on, '_gh_pr_mergeable_status',
            side_effect=['unknown', 'unknown', 'conflicting'],
        ) as m:
            status = on._poll_pr_mergeable(
                'o/r', 1, max_polls=6, interval_s=0.0, sleep=slept.append)
        self.assertEqual(status, 'conflicting')
        self.assertEqual(m.call_count, 3)
        self.assertEqual(len(slept), 2)  # one sleep between each of the 3 calls

    def test_exhausts_to_unknown(self):
        with mock.patch.object(
            on, '_gh_pr_mergeable_status', return_value='unknown',
        ) as m:
            status = on._poll_pr_mergeable(
                'o/r', 1, max_polls=4, interval_s=0.0, sleep=lambda *_: None)
        self.assertEqual(status, 'unknown')
        self.assertEqual(m.call_count, 4)

    def test_settles_mergeable_no_extra_polls(self):
        slept = []
        with mock.patch.object(
            on, '_gh_pr_mergeable_status', return_value='mergeable',
        ) as m:
            status = on._poll_pr_mergeable(
                'o/r', 1, max_polls=6, interval_s=0.0, sleep=slept.append)
        self.assertEqual(status, 'mergeable')
        self.assertEqual(m.call_count, 1)
        self.assertEqual(slept, [])


# ----------------------------- gate decision -----------------------------

class GateDecisionTest(unittest.TestCase):
    """_handle_pr_mergeable_before_review decision logic, isolated from dispatch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_ledger = rol.LEDGER_FILE
        rol.LEDGER_FILE = Path(self._tmp.name) / 'rebase-obligation-ledger.json'
        self.addCleanup(setattr, rol, 'LEDGER_FILE', self._orig_ledger)

    def test_build_conflicting_dispatches_rebase_round1_skips_mirror(self):
        data = _build_outbox()
        with mock.patch.object(on, '_poll_pr_mergeable', return_value='conflicting'), \
             mock.patch.object(on, '_dispatch_rebase_to_forge') as disp:
            proceed = on._handle_pr_mergeable_before_review(data, PR)
        self.assertFalse(proceed)  # criterion 3: Mirror NOT dispatched
        disp.assert_called_once()
        self.assertEqual(disp.call_args.kwargs.get('round_num'), 1)

    def test_build_mergeable_proceeds_no_rebase(self):
        data = _build_outbox()
        with mock.patch.object(on, '_poll_pr_mergeable', return_value='mergeable'), \
             mock.patch.object(on, '_dispatch_rebase_to_forge') as disp:
            proceed = on._handle_pr_mergeable_before_review(data, PR)
        self.assertTrue(proceed)
        disp.assert_not_called()

    def test_unknown_after_poll_proceeds_optimistically(self):
        data = _build_outbox()
        with mock.patch.object(on, '_poll_pr_mergeable', return_value='unknown'), \
             mock.patch.object(on, '_dispatch_rebase_to_forge') as disp:
            proceed = on._handle_pr_mergeable_before_review(data, PR)
        self.assertTrue(proceed)
        disp.assert_not_called()

    def test_rebase_phase_mergeable_resolves_obligation_and_proceeds(self):
        data = _build_outbox(phase='rebase', result=f'PR updated: {PR}')
        rol.open_obligation('rebase-task-1', pr_url=PR, round_num=1)
        with mock.patch.object(on, '_poll_pr_mergeable', return_value='mergeable'):
            proceed = on._handle_pr_mergeable_before_review(
                data, PR, is_rebase_phase=True)
        self.assertTrue(proceed)
        row = rol.get_obligation('rebase-task-1')
        self.assertEqual(row['status'], rol.RESOLVED)

    def test_rebase_phase_conflicting_under_cap_redispatches_next_round(self):
        data = _build_outbox(phase='rebase', result=f'PR updated: {PR}')
        rol.open_obligation('rebase-task-1', pr_url=PR, round_num=1)
        with mock.patch.object(on, '_poll_pr_mergeable', return_value='conflicting'), \
             mock.patch.object(on, '_dispatch_rebase_to_forge') as disp:
            proceed = on._handle_pr_mergeable_before_review(
                data, PR, is_rebase_phase=True)
        self.assertFalse(proceed)
        disp.assert_called_once()
        self.assertEqual(disp.call_args.kwargs.get('round_num'), 2)

    def test_rebase_phase_conflicting_at_cap_stops_and_keeps_open(self):
        data = _build_outbox(phase='rebase', result=f'PR updated: {PR}')
        rol.open_obligation('rebase-task-1', pr_url=PR,
                            round_num=on._REBASE_MAX_ROUNDS)
        with mock.patch.object(on, '_poll_pr_mergeable', return_value='conflicting'), \
             mock.patch.object(on, '_dispatch_rebase_to_forge') as disp:
            proceed = on._handle_pr_mergeable_before_review(
                data, PR, is_rebase_phase=True)
        self.assertFalse(proceed)
        disp.assert_not_called()  # no unbounded re-dispatch
        # obligation stays OPEN for the healer
        row = rol.get_obligation('rebase-task-1')
        self.assertEqual(row['status'], rol.OPEN)


# ------------------- dispatch writes a phase=rebase envelope -------------------

class DispatchRebaseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._root = Path(self._tmp.name)
        self._orig = {n: getattr(on, n) for n in ('AGENTS_ROOT', 'INBOXES_ROOT', 'LOG_FILE')}
        on.AGENTS_ROOT = self._root
        on.INBOXES_ROOT = self._root / 'inboxes'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        self.addCleanup(lambda: [setattr(on, n, v) for n, v in self._orig.items()])
        self._swi = {'AGENTS_ROOT': swi.AGENTS_ROOT, 'INBOXES_ROOT': swi.INBOXES_ROOT,
                     'ROUTING_EVENTS_LOG': swi.ROUTING_EVENTS_LOG}
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'
        self.addCleanup(lambda: [setattr(swi, n, v) for n, v in self._swi.items()])
        self._orig_ledger = rol.LEDGER_FILE
        rol.LEDGER_FILE = self._root / 'state' / 'rebase-obligation-ledger.json'
        self.addCleanup(setattr, rol, 'LEDGER_FILE', self._orig_ledger)
        self._rv_root = rv.REPO_ROOT
        rv.REPO_ROOT = self._root / 'repo'
        rv.invalidate_cache()
        self.addCleanup(rv.invalidate_cache)
        self.addCleanup(setattr, rv, 'REPO_ROOT', self._rv_root)
        on.ensure_dirs()

    def test_writes_rebase_task_and_opens_obligation(self):
        data = _build_outbox()
        with mock.patch.object(on, '_enforce_cost_budget', return_value=True):
            ok = on._dispatch_rebase_to_forge(data, PR, round_num=1)
        self.assertTrue(ok)
        forge_inbox = on.INBOXES_ROOT / 'forge'
        files = list(forge_inbox.glob('rebase-rebase-task-1-1.json'))
        self.assertEqual(len(files), 1)
        env = json.loads(files[0].read_text())
        self.assertEqual(env['phase'], 'rebase')
        self.assertEqual(env['source'], 'beacon')
        self.assertEqual(env['session_id'], 'sess-build-xyz')  # --resume build
        self.assertIn('Rebase phase', env['prompt'])
        # durable obligation opened
        row = rol.get_obligation('rebase-task-1')
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], rol.OPEN)
        self.assertEqual(row['round'], 1)

    def test_no_target_repo_returns_false_no_write(self):
        data = _build_outbox(target_repo=None)
        with mock.patch.object(on, '_enforce_cost_budget', return_value=True), \
             mock.patch.object(on, 'backfill_target_repo', return_value=None):
            ok = on._dispatch_rebase_to_forge(data, PR, round_num=1)
        self.assertFalse(ok)


# --------------- process_outbox integration (criteria 1, 3, 4) ---------------

class ProcessOutboxRebaseIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._root = Path(self._tmp.name)
        self._orig = {n: getattr(on, n) for n in (
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT', 'BLACKBOARD',
            'LOG_FILE', 'DEAD_LETTER_STATE', 'EMERGENCY_HALT_FLAG')}
        on.AGENTS_ROOT = self._root
        on.INBOXES_ROOT = self._root / 'inboxes'
        on.OUTBOXES_ROOT = self._root / 'outboxes'
        on.BLACKBOARD = self._root / 'blackboard'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.DEAD_LETTER_STATE = self._root / 'state' / 'dead-letter.json'
        on.EMERGENCY_HALT_FLAG = on.BLACKBOARD / 'EMERGENCY_HALT'
        self.addCleanup(lambda: [setattr(on, n, v) for n, v in self._orig.items()])
        self._swi = {'AGENTS_ROOT': swi.AGENTS_ROOT, 'INBOXES_ROOT': swi.INBOXES_ROOT,
                     'ROUTING_EVENTS_LOG': swi.ROUTING_EVENTS_LOG}
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'
        self.addCleanup(lambda: [setattr(swi, n, v) for n, v in self._swi.items()])
        self._orig_ledger = rol.LEDGER_FILE
        rol.LEDGER_FILE = self._root / 'state' / 'rebase-obligation-ledger.json'
        self.addCleanup(setattr, rol, 'LEDGER_FILE', self._orig_ledger)
        self._rv_root = rv.REPO_ROOT
        rv.REPO_ROOT = self._root / 'repo'
        rv.invalidate_cache()
        self.addCleanup(rv.invalidate_cache)
        self.addCleanup(setattr, rv, 'REPO_ROOT', self._rv_root)
        on.ensure_dirs()

    def _write_outbox(self, body):
        d = on.OUTBOXES_ROOT / 'forge'
        d.mkdir(parents=True, exist_ok=True)
        f = d / f'{body["task_id"]}.json'
        f.write_text(json.dumps(body))
        return f

    def test_conflicting_build_dispatches_rebase_not_mirror(self):
        """Criteria 1+3+4: a CONFLICTING build opens a rebase + obligation, and
        does NOT dispatch Mirror onto the doomed PR."""
        f = self._write_outbox(_build_outbox())
        with mock.patch.object(on, '_poll_pr_mergeable', return_value='conflicting'), \
             mock.patch.object(on, '_enforce_cost_budget', return_value=True), \
             mock.patch.object(on, '_signal_sequence_step_pr_opened'):
            on.process_outbox(f)
        # rebase dispatched to Forge
        rebase_files = list((on.INBOXES_ROOT / 'forge').glob('rebase-*.json'))
        self.assertEqual(len(rebase_files), 1)
        # Mirror NOT dispatched (criterion 3)
        review_files = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(review_files, [])
        # durable obligation opened (criterion 4)
        self.assertEqual(rol.get_obligation('rebase-task-1')['status'], rol.OPEN)

    def test_mergeable_build_dispatches_mirror_no_rebase(self):
        f = self._write_outbox(_build_outbox())
        # The fixture PR url (.../pull/777) is a real MERGED PR; on the droplet
        # `gh` is installed, so the dispatch-time terminal guard would see
        # MERGED and skip the Mirror dispatch this test asserts. Force the
        # target non-terminal so the intended (open-PR) dispatch path runs.
        with mock.patch.object(on, '_poll_pr_mergeable', return_value='mergeable'), \
             mock.patch.object(on, '_mirror_review_target_is_terminal',
                                return_value=False), \
             mock.patch.object(on, '_enforce_cost_budget', return_value=True), \
             mock.patch.object(on, '_signal_sequence_step_pr_opened'):
            on.process_outbox(f)
        review_files = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(len(review_files), 1)
        rebase_files = list((on.INBOXES_ROOT / 'forge').glob('rebase-*.json'))
        self.assertEqual(rebase_files, [])

    def test_rebase_phase_mergeable_reaches_mirror_and_resolves(self):
        """Criterion 1: after the rebase lands MERGEABLE, Mirror is dispatched
        with zero human action and the obligation resolves."""
        rol.open_obligation('rebase-task-1', pr_url=PR, round_num=1)
        body = _build_outbox(phase='rebase', result=f'PR updated: {PR}')
        f = self._write_outbox(body)
        # See test_mergeable_build_dispatches_mirror_no_rebase: PR 777 is a real
        # MERGED PR, so the terminal guard would skip the Mirror dispatch this
        # test asserts. Force non-terminal to reach the intended dispatch path.
        with mock.patch.object(on, '_poll_pr_mergeable', return_value='mergeable'), \
             mock.patch.object(on, '_mirror_review_target_is_terminal',
                                return_value=False), \
             mock.patch.object(on, '_enforce_cost_budget', return_value=True), \
             mock.patch.object(on, '_signal_sequence_step_pr_opened'):
            on.process_outbox(f)
        review_files = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(len(review_files), 1)
        self.assertEqual(rol.get_obligation('rebase-task-1')['status'], rol.RESOLVED)

    def test_rebase_phase_blocker_no_pr_keeps_obligation_open(self):
        """Criterion 2: Forge aborted the rebase and emitted a blocker (no
        `PR updated:`). The obligation stays OPEN (healer backstop); Mirror is
        not dispatched."""
        rol.open_obligation('rebase-task-1', pr_url=PR, round_num=1)
        body = _build_outbox(
            phase='rebase',
            result='BLOCKER: rebase conflicted on scripts/foo.py; main moved '
                   'under commit abc123. Aborted. Needs a fresh build.')
        f = self._write_outbox(body)
        with mock.patch.object(on, '_poll_pr_mergeable') as poll, \
             mock.patch.object(on, '_signal_sequence_step_pr_opened'):
            on.process_outbox(f)
            poll.assert_not_called()  # no PR url → no mergeable check
        review_files = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(review_files, [])
        self.assertEqual(rol.get_obligation('rebase-task-1')['status'], rol.OPEN)


if __name__ == '__main__':
    unittest.main()
