#!/usr/bin/env python3
"""test_held_alert_escalation.py — B5 persistence + B6 backstop promotion.

Spec: agents/beacon/specs/alert-pipeline-rework.md Part B (B5, B6).

Pins:
  - compute_open_holds: a fingerprint is OPEN iff its most-recent queue event is
    a `hold` (no later escalate/closure/promotion) AND not silenced.
  - run_cycle (B5): promote after N_CYCLES_BEFORE_PROMOTE cycles; promote-once;
    self-clear of resolved holds from probation state.
  - run_backstop (B6): promote on wall-clock age >= BACKSTOP_SEC; stateless;
    promote-once rides the queue (a promotion line makes the hold no longer open).

All IO seams are injected (records=, state=, now=, append_fn=, is_silenced_fn=)
so nothing touches the real queue / filesystem / network.

Run:
    python3 -m unittest scripts.tests.test_held_alert_escalation
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import held_alert_escalation as hae  # noqa: E402

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


def _hold(source, subject, *, severity='warning', minutes_ago=0, message='m'):
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {'ts': ts, 'source': source, 'severity': severity,
            'message': message, 'route': 'hold', 'subject': subject}


def _line(source, subject, route, **extra):
    rec = {'ts': NOW.isoformat(), 'source': source, 'severity': 'warning',
           'message': 'm', 'route': route, 'subject': subject}
    rec.update(extra)
    return rec


def _never_silenced(_key):
    return False


# Auto-merge held-alert subject shape emitted by outbox_notifier
# (`<alert-type>:<owner/repo>:<pr_number>`), keyed under the outbox-notifier
# source. A hold on one of these is what the live-PR-state gate acts on.
_AM_SUBJECT = 'auto-merge-queue-stale:Larry-Yatch/ourliberty-agent-core:843'


def _am_hold(subject=_AM_SUBJECT, *, minutes_ago=0):
    return _hold('outbox-notifier', subject, minutes_ago=minutes_ago)


def _pr_open(_ref):
    return 'OPEN'


def _pr_merged(_ref):
    return 'MERGED'


def _pr_closed(_ref):
    return 'CLOSED'


def _pr_unknown(_ref):
    return None  # gh couldn't determine → fail-open


class ComputeOpenHoldsTest(unittest.TestCase):
    def _open(self, records, is_silenced_fn=_never_silenced, pr_state_fn=_pr_open):
        # Default pr_state_fn is a hermetic OPEN stub so no test ever shells out
        # to real gh; the gate tests below inject MERGED/CLOSED/None explicitly.
        return hae.compute_open_holds(records, now=NOW,
                                      is_silenced_fn=is_silenced_fn,
                                      pr_state_fn=pr_state_fn)

    def test_lone_hold_is_open(self):
        holds = self._open([_hold('heal-x', 'a')])
        self.assertIn('heal-x:a', holds)

    def test_hold_then_escalate_resolves(self):
        holds = self._open([_hold('heal-x', 'a'), _line('heal-x', 'a', 'escalate')])
        self.assertNotIn('heal-x:a', holds)

    def test_hold_then_closure_resolves(self):
        holds = self._open([_hold('heal-x', 'a'), _line('heal-x', 'a', 'closure')])
        self.assertNotIn('heal-x:a', holds)

    def test_hold_then_promotion_resolves(self):
        promo = _line('heal-x', 'a::promoted', 'escalate',
                      promotion=True, promoted_from='heal-x:a')
        holds = self._open([_hold('heal-x', 'a'), promo])
        self.assertNotIn('heal-x:a', holds)

    def test_silenced_hold_not_open(self):
        holds = self._open([_hold('heal-x', 'a')],
                           is_silenced_fn=lambda k: k == 'heal-x:a')
        self.assertNotIn('heal-x:a', holds)

    def test_escalate_then_later_hold_is_open(self):
        # Last event wins: a NEW hold after an old resolution is open again.
        holds = self._open([_line('heal-x', 'a', 'escalate'), _hold('heal-x', 'a')])
        self.assertIn('heal-x:a', holds)

    def test_hold_without_subject_skipped(self):
        rec = {'ts': NOW.isoformat(), 'source': 'heal-x', 'severity': 'warning',
               'message': 'm', 'route': 'hold'}
        holds = self._open([rec])
        self.assertEqual(holds, {})

    def test_notification_record_ignored(self):
        rec = {'ts': NOW.isoformat(), 'kind': 'notification', 'route': 'hold',
               'source': 'heal-x', 'subject': 'a', 'message': 'm'}
        holds = self._open([rec])
        self.assertEqual(holds, {})

    def test_age_seconds_from_record_ts(self):
        holds = self._open([_hold('heal-x', 'a', minutes_ago=42)])
        self.assertAlmostEqual(holds['heal-x:a'].age_sec, 42 * 60, delta=1)

    # --- live-PR-state gate ---

    def test_merged_pr_hold_gated_out(self):
        key = f'outbox-notifier:{_AM_SUBJECT}'
        holds = self._open([_am_hold()], pr_state_fn=_pr_merged)
        self.assertNotIn(key, holds)

    def test_closed_pr_hold_gated_out(self):
        key = f'outbox-notifier:{_AM_SUBJECT}'
        holds = self._open([_am_hold()], pr_state_fn=_pr_closed)
        self.assertNotIn(key, holds)

    def test_open_pr_hold_kept(self):
        key = f'outbox-notifier:{_AM_SUBJECT}'
        holds = self._open([_am_hold()], pr_state_fn=_pr_open)
        self.assertIn(key, holds)

    def test_unknown_pr_state_fails_open(self):
        # gh couldn't determine (None) → the hold stays promotable.
        key = f'outbox-notifier:{_AM_SUBJECT}'
        holds = self._open([_am_hold()], pr_state_fn=_pr_unknown)
        self.assertIn(key, holds)

    def test_non_pr_subject_issues_no_gh_call(self):
        # A subject with no extractable PR ref must never invoke pr_state_fn.
        calls = []

        def _spy(ref):
            calls.append(ref)
            return 'MERGED'

        holds = self._open([_hold('heal-x', 'a')], pr_state_fn=_spy)
        self.assertIn('heal-x:a', holds)
        self.assertEqual(calls, [])


class ExtractPrRefTest(unittest.TestCase):
    def test_auto_merge_subject_tail(self):
        self.assertEqual(
            hae.extract_pr_ref(
                'auto-merge-conflict:Larry-Yatch/ourliberty-agent-core:764'),
            'Larry-Yatch/ourliberty-agent-core#764')

    def test_auto_merge_deep_review_hold(self):
        self.assertEqual(
            hae.extract_pr_ref(
                'auto-merge-deep-review-hold:Larry-Yatch/ourliberty-agent-core:830'),
            'Larry-Yatch/ourliberty-agent-core#830')

    def test_github_pr_url(self):
        self.assertEqual(
            hae.extract_pr_ref(
                'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/843'),
            'Larry-Yatch/ourliberty-agent-core#843')

    def test_non_pr_subject_returns_none(self):
        for subj in ('a', 'heal-x:a', 'sequence-paused:seq-42',
                     'auto-merge-queue-corrupt', '', None, 123):
            self.assertIsNone(hae.extract_pr_ref(subj))


class DefaultPrStateTest(unittest.TestCase):
    def _patch_gh_json(self, retval):
        orig = hae.tts.gh_json
        hae.tts.gh_json = lambda *a, **k: retval
        self.addCleanup(lambda: setattr(hae.tts, 'gh_json', orig))

    def test_open_state(self):
        self._patch_gh_json({'state': 'OPEN'})
        self.assertEqual(hae.default_pr_state('o/r#1'), 'OPEN')

    def test_merged_state(self):
        self._patch_gh_json({'state': 'MERGED'})
        self.assertEqual(hae.default_pr_state('o/r#1'), 'MERGED')

    def test_gh_failure_returns_none(self):
        # gh_json returns None on timeout / non-zero exit / parse failure.
        self._patch_gh_json(None)
        self.assertIsNone(hae.default_pr_state('o/r#1'))

    def test_unrecognized_state_returns_none(self):
        self._patch_gh_json({'state': 'WEIRD'})
        self.assertIsNone(hae.default_pr_state('o/r#1'))

    def test_empty_ref_short_circuits(self):
        self.assertIsNone(hae.default_pr_state(''))

    def test_gh_args_use_number_and_repo_flag(self):
        # Regression guard: gh reads a bare `owner/repo#number` positional as a
        # BRANCH and exits 1, fail-opening the gate on every hold. The number
        # must go positionally and the repo via `--repo`.
        captured = {}

        def _spy(args, **kw):
            captured['args'] = list(args)
            return {'state': 'MERGED'}

        orig = hae.tts.gh_json
        hae.tts.gh_json = _spy
        self.addCleanup(lambda: setattr(hae.tts, 'gh_json', orig))
        result = hae.default_pr_state('Larry-Yatch/ourliberty-agent-core#843')
        self.assertEqual(result, 'MERGED')
        args = captured['args']
        self.assertIn('843', args)
        self.assertIn('--repo', args)
        self.assertEqual(args[args.index('--repo') + 1],
                         'Larry-Yatch/ourliberty-agent-core')
        # The broken bare-ref form must NOT appear anywhere in the call.
        self.assertNotIn('Larry-Yatch/ourliberty-agent-core#843', args)

    def test_malformed_ref_no_gh_call(self):
        # A ref with no '#' never shells out (short-circuits to None fail-open).
        calls = []

        def _spy(args, **kw):
            calls.append(args)
            return {'state': 'MERGED'}

        orig = hae.tts.gh_json
        hae.tts.gh_json = _spy
        self.addCleanup(lambda: setattr(hae.tts, 'gh_json', orig))
        self.assertIsNone(hae.default_pr_state('owner/repo-no-hash'))
        self.assertEqual(calls, [])


class _Recorder:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, hold, reason):
        self.calls.append((hold.key, reason))
        return self.ok


class RunCyclePersistenceTest(unittest.TestCase):
    def test_no_promotion_before_n_cycles(self):
        rec = _Recorder()
        records = [_hold('heal-x', 'a')]
        state: dict = {}
        for _ in range(hae.N_CYCLES_BEFORE_PROMOTE - 1):
            state = _run_and_capture(records, state, rec)
        self.assertEqual(rec.calls, [])
        self.assertEqual(state['heal-x:a']['cycles'], hae.N_CYCLES_BEFORE_PROMOTE - 1)

    def test_full_promotion_flow(self):
        rec = _Recorder()
        records = [_hold('heal-x', 'a')]
        state: dict = {}
        promoted_cycle = None
        for cycle in range(1, hae.N_CYCLES_BEFORE_PROMOTE + 1):
            state = _run_and_capture(records, state, rec)
            if state['heal-x:a'].get('promoted'):
                promoted_cycle = cycle
                break
        self.assertEqual(promoted_cycle, hae.N_CYCLES_BEFORE_PROMOTE)
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0][0], 'heal-x:a')
        self.assertIn('persistence', rec.calls[0][1])

    def test_promote_once_does_not_refire(self):
        rec = _Recorder()
        records = [_hold('heal-x', 'a')]
        state: dict = {}
        for _ in range(hae.N_CYCLES_BEFORE_PROMOTE + 3):
            state = _run_and_capture(records, state, rec)
        self.assertEqual(len(rec.calls), 1)  # never re-promoted

    def test_resolved_hold_self_clears_from_state(self):
        rec = _Recorder()
        # First, accrue a cycle of probation.
        state = _run_and_capture([_hold('heal-x', 'a')], {}, rec)
        self.assertIn('heal-x:a', state)
        # Next cycle the hold is resolved (escalate appears) → dropped.
        resolved = [_hold('heal-x', 'a'), _line('heal-x', 'a', 'escalate')]
        state = _run_and_capture(resolved, state, rec)
        self.assertNotIn('heal-x:a', state)
        self.assertEqual(rec.calls, [])

    def test_append_failure_leaves_unpromoted_for_retry(self):
        rec = _Recorder(ok=False)
        records = [_hold('heal-x', 'a')]
        state: dict = {}
        for _ in range(hae.N_CYCLES_BEFORE_PROMOTE + 1):
            state = _run_and_capture(records, state, rec)
        # Append kept failing → still un-promoted, retried each cycle past N.
        self.assertFalse(state['heal-x:a']['promoted'])
        self.assertGreaterEqual(len(rec.calls), 2)


class RunBackstopTest(unittest.TestCase):
    def test_not_promoted_before_backstop_age(self):
        rec = _Recorder()
        summary = hae.run_backstop(
            records=[_hold('heal-x', 'a', minutes_ago=10)], now=NOW,
            append_fn=rec, is_silenced_fn=_never_silenced)
        self.assertEqual(rec.calls, [])
        self.assertEqual(summary['promoted'], [])

    def test_promoted_past_backstop_age(self):
        rec = _Recorder()
        summary = hae.run_backstop(
            records=[_hold('heal-x', 'a', minutes_ago=31)], now=NOW,
            append_fn=rec, is_silenced_fn=_never_silenced)
        self.assertEqual([c[0] for c in rec.calls], ['heal-x:a'])
        self.assertIn('backstop', rec.calls[0][1])
        self.assertEqual(summary['promoted'], ['heal-x:a'])

    def test_already_promoted_hold_not_refired(self):
        # A promotion line already exists → the hold is no longer open → no-op.
        rec = _Recorder()
        promo = _line('heal-x', 'a::promoted', 'escalate',
                      promotion=True, promoted_from='heal-x:a')
        hae.run_backstop(
            records=[_hold('heal-x', 'a', minutes_ago=99), promo], now=NOW,
            append_fn=rec, is_silenced_fn=_never_silenced)
        self.assertEqual(rec.calls, [])

    def test_silenced_hold_not_backstopped(self):
        rec = _Recorder()
        hae.run_backstop(
            records=[_hold('heal-x', 'a', minutes_ago=99)], now=NOW,
            append_fn=rec, is_silenced_fn=lambda k: True)
        self.assertEqual(rec.calls, [])

    def test_merged_pr_hold_not_backstopped(self):
        # B6 inherits the single-source gate: a merged-PR hold well past the
        # backstop age is excluded from open_holds and never promoted.
        rec = _Recorder()
        summary = hae.run_backstop(
            records=[_am_hold(minutes_ago=99)], now=NOW,
            append_fn=rec, is_silenced_fn=_never_silenced,
            pr_state_fn=_pr_merged)
        self.assertEqual(rec.calls, [])
        self.assertEqual(summary['promoted'], [])


class GatePropagationTest(unittest.TestCase):
    """Both promotion paths (B5 run_cycle, B6 run_backstop) inherit the gate."""

    def test_run_cycle_gates_merged_pr(self):
        rec = _Recorder()
        state: dict = {}
        # Even past N cycles, a merged-PR hold is never open, so never promoted.
        for _ in range(hae.N_CYCLES_BEFORE_PROMOTE + 2):
            captured = {}

            def _capture(holds, path=hae.STATE_FILE):
                captured.clear()
                captured.update(holds)

            orig = hae.write_state
            hae.write_state = _capture
            try:
                hae.run_cycle(records=[_am_hold()], state=state, now=NOW,
                              append_fn=rec, is_silenced_fn=_never_silenced,
                              pr_state_fn=_pr_merged, persist=True)
            finally:
                hae.write_state = orig
            state = captured
        self.assertEqual(rec.calls, [])
        self.assertEqual(state, {})

    def test_run_backstop_gates_merged_pr(self):
        rec = _Recorder()
        summary = hae.run_backstop(
            records=[_am_hold(minutes_ago=99)], now=NOW,
            append_fn=rec, is_silenced_fn=_never_silenced,
            pr_state_fn=_pr_merged)
        self.assertEqual(rec.calls, [])
        self.assertEqual(summary['promoted'], [])

    def test_run_backstop_open_pr_still_promotes(self):
        # Control: an OPEN-PR hold past backstop age still promotes (gate is a
        # no-op on OPEN — only MERGED/CLOSED suppress).
        rec = _Recorder()
        summary = hae.run_backstop(
            records=[_am_hold(minutes_ago=99)], now=NOW,
            append_fn=rec, is_silenced_fn=_never_silenced,
            pr_state_fn=_pr_open)
        self.assertEqual([c[0] for c in rec.calls],
                         [f'outbox-notifier:{_AM_SUBJECT}'])
        self.assertEqual(summary['promoted'], [f'outbox-notifier:{_AM_SUBJECT}'])


def _run_and_capture(records, prior_state, append_fn):
    """Run one persistence cycle without persistence and return the new_state by
    re-deriving it the same way run_cycle would (persist=False path skips the
    write, so we reconstruct via a second compute)."""
    # run_cycle with persist=False does the full decision but doesn't write; to
    # observe the next-cycle state we recompute the entry map exactly as run_cycle
    # builds it. Simpler: monkey-capture via a persist hook.
    captured = {}
    orig_write = hae.write_state

    def _capture(holds, path=hae.STATE_FILE):
        captured.clear()
        captured.update(holds)

    hae.write_state = _capture
    try:
        hae.run_cycle(records=records, state=prior_state, now=NOW,
                      append_fn=append_fn, is_silenced_fn=_never_silenced,
                      persist=True)
    finally:
        hae.write_state = orig_write
    return captured


if __name__ == '__main__':
    unittest.main()
