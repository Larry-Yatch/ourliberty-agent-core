#!/usr/bin/env python3
"""Tests for projects-v3 P4 Contract A — one-time sequence-completion signal.

When a build-sequence's LAST step reaches verified-merged, the notifier must
emit EXACTLY ONE `sequence_complete` chain event + ONE plain-language
completion DM to Larry (what shipped, the PRs, a one-line summary). A re-tick
or notifier crash-resume that re-detects the same merged sequence must NEVER
double-DM.

Two layers under test:

  - `sequence_shortcut_helpers.claim_completion_signal` /
    `is_completion_signaled` — the durable, atomic exactly-once guard
    (audit_log `sequence-complete-signaled` marker).
  - `outbox_notifier._maybe_signal_sequence_complete` + the end-to-end path
    through `_signal_sequence_step_merged` — the gh veto, the claim-then-emit
    ordering, and the no-double-DM invariant.

Mirror review focus (spec § 5): exactly-once — no double-fire on re-tick. Every
test reroutes module-level paths to a per-test tmpdir; NO write reaches the
real ~/agents tree, and the gh veto is stubbed so nothing shells out.

Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_sequence_complete_signal
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import larry_alerts as la                # noqa: E402
import outbox_notifier as on             # noqa: E402
import safe_write_inbox as swi           # noqa: E402
import sequence_shortcut_helpers as ssh  # noqa: E402

try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout


_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # This module deliberately drives `larry_alerts.append_alert` (a Layer B
    # guarded chokepoint) against rerouted/isolated state, so opt the module
    # out of the call-time guard. The #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


def _make_step(step_id, deps=None, status='pending', pr_url=None,
               dispatched_at=None, merged_at=None):
    return {
        'step_id': step_id,
        'label': f'Step {step_id}',
        'depends_on': deps or [],
        'dispatch_text': f'Build {step_id} per spec § X. Review focus: Y.',
        'target_repo': 'ourliberty-agent-core',
        'task_type': 'feature-development',
        'status': status,
        'dispatched_at': dispatched_at,
        'merged_at': merged_at,
        'pr_url': pr_url,
        'current_actor': None,
        'failure_reason': None,
    }


def _make_sequence(seq_id='done-seq-001', status='complete', steps=None,
                   current_steps=None, audit_log=None):
    if steps is None:
        steps = [
            _make_step('step-a', deps=[], status='merged',
                       merged_at='2026-06-16T00:00:00Z',
                       pr_url='https://github.com/larry/r/pull/1'),
            _make_step('step-b', deps=['step-a'], status='merged',
                       merged_at='2026-06-16T01:00:00Z',
                       pr_url='https://github.com/larry/r/pull/2'),
        ]
    return {
        'seq_id': seq_id,
        'label': f'Sequence {seq_id}',
        'spec_doc': 'agents/beacon/specs/build-sequence-orchestrator.md',
        'created_at': '2026-06-16T00:00:00+00:00',
        'created_by': 'beacon',
        'status': status,
        'current_steps': current_steps if current_steps is not None else [],
        'steps': steps,
        'audit_log': audit_log if audit_log is not None else [
            {'ts': '2026-06-16T00:00:00+00:00', 'event': 'sequence-created',
             'actor': 'beacon'},
        ],
    }


class _Harness(unittest.TestCase):
    """Per-test tmpdir isolation for outbox_notifier + safe_write_inbox +
    larry_alerts + sequence_shortcut_helpers. Stubs the gh veto to MERGED by
    default so completion never shells out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)

        self._on_originals = {}
        for name in [
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT',
            'BLACKBOARD', 'LOG_FILE', 'DEAD_LETTER_STATE',
            'EMERGENCY_HALT_FLAG',
        ]:
            self._on_originals[name] = getattr(on, name)
        on.AGENTS_ROOT = self._root
        on.INBOXES_ROOT = self._root / 'inboxes'
        on.OUTBOXES_ROOT = self._root / 'outboxes'
        on.BLACKBOARD = self._root / 'blackboard'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.DEAD_LETTER_STATE = self._root / 'state' / 'dead-letter.json'
        on.EMERGENCY_HALT_FLAG = on.BLACKBOARD / 'EMERGENCY_HALT'

        self._swi_originals = {
            'AGENTS_ROOT': swi.AGENTS_ROOT,
            'INBOXES_ROOT': swi.INBOXES_ROOT,
            'ROUTING_EVENTS_LOG': swi.ROUTING_EVENTS_LOG,
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'

        self._la_originals = {
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
        }
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'

        self._ssh_original_root = ssh.AGENTS_ROOT
        ssh.AGENTS_ROOT = self._root

        on.ensure_dirs()
        (self._root / 'blackboard' / 'build-sequences').mkdir(
            parents=True, exist_ok=True,
        )

        # gh veto stub: PASS (MERGED) unless a test overrides it.
        self._gh_state_patcher = mock.patch.object(
            on, '_gh_pr_state', return_value='MERGED',
        )
        self._gh_state_mock = self._gh_state_patcher.start()
        self.addCleanup(self._gh_state_patcher.stop)

    def tearDown(self):
        ssh.AGENTS_ROOT = self._ssh_original_root
        for name, value in self._on_originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        self._tmp.cleanup()

    def _write_sequence(self, seq):
        path = (
            self._root / 'blackboard' / 'build-sequences'
            / f'{seq["seq_id"]}.json'
        )
        path.write_text(json.dumps(seq, indent=2))
        return path

    def _read_sequence(self, seq_id):
        path = (
            self._root / 'blackboard' / 'build-sequences' / f'{seq_id}.json'
        )
        return json.loads(path.read_text())

    def _read_alerts(self):
        if not la.ALERTS_FILE.exists():
            return []
        return [
            json.loads(line)
            for line in la.ALERTS_FILE.read_text().splitlines()
            if line.strip()
        ]

    def _signaled_markers(self, seq_id):
        seq = self._read_sequence(seq_id)
        return [
            e for e in seq.get('audit_log', [])
            if isinstance(e, dict)
            and e.get('event') == ssh.SEQUENCE_COMPLETE_SIGNALED_EVENT
        ]


# ===================================================================
# Layer 1 — claim_completion_signal / is_completion_signaled
# ===================================================================


class ClaimCompletionSignal(_Harness):

    def test_first_claim_on_complete_sequence_applies(self):
        self._write_sequence(_make_sequence(seq_id='c1', status='complete'))
        result = ssh.claim_completion_signal('c1')
        self.assertTrue(result.applied)
        self.assertFalse(result.error)
        # Marker durably written before return.
        self.assertEqual(len(self._signaled_markers('c1')), 1)
        marker = self._signaled_markers('c1')[0]
        self.assertEqual(marker['actor'], 'notifier')
        self.assertIn('ts', marker)

    def test_second_claim_is_benign_noop(self):
        self._write_sequence(_make_sequence(seq_id='c2', status='complete'))
        first = ssh.claim_completion_signal('c2')
        self.assertTrue(first.applied)
        second = ssh.claim_completion_signal('c2')
        self.assertFalse(second.applied)
        self.assertFalse(second.error)
        # Still exactly one marker — no duplicate appended.
        self.assertEqual(len(self._signaled_markers('c2')), 1)

    def test_not_complete_sequence_is_noop(self):
        self._write_sequence(_make_sequence(seq_id='c3', status='active'))
        result = ssh.claim_completion_signal('c3')
        self.assertFalse(result.applied)
        self.assertFalse(result.error)
        self.assertEqual(len(self._signaled_markers('c3')), 0)

    def test_missing_file_is_hard_error(self):
        result = ssh.claim_completion_signal('does-not-exist')
        self.assertFalse(result.applied)
        self.assertTrue(result.error)

    def test_is_completion_signaled_reflects_marker(self):
        seq = _make_sequence(seq_id='c4', status='complete')
        self.assertFalse(ssh.is_completion_signaled(seq))
        seq['audit_log'].append(
            {'ts': '2026-06-16T02:00:00Z',
             'event': ssh.SEQUENCE_COMPLETE_SIGNALED_EVENT, 'actor': 'x'},
        )
        self.assertTrue(ssh.is_completion_signaled(seq))


# ===================================================================
# Layer 2 — _maybe_signal_sequence_complete
# ===================================================================


class MaybeSignalSequenceComplete(_Harness):

    def test_complete_sequence_emits_event_and_dm_once(self):
        self._write_sequence(_make_sequence(seq_id='m1', status='complete'))
        with mock.patch.object(
            on.chain_event_emit, 'emit_event', return_value=True,
        ) as emit:
            on._maybe_signal_sequence_complete('m1')

        # Exactly one chain event of the right type.
        self.assertEqual(emit.call_count, 1)
        _, kwargs = emit.call_args
        self.assertEqual(kwargs['event_type'], 'sequence_complete')
        self.assertEqual(kwargs['agent'], 'build_sequence_advancer')
        self.assertEqual(kwargs['task_id'], 'm1')
        self.assertEqual(
            kwargs['payload']['pr_urls'],
            ['https://github.com/larry/r/pull/1',
             'https://github.com/larry/r/pull/2'],
        )

        # Exactly one DM with a plain-language body naming the PRs.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        body = alerts[0]['message']
        self.assertIn('✅', body)
        self.assertIn('https://github.com/larry/r/pull/1', body)
        self.assertIn('https://github.com/larry/r/pull/2', body)
        self.assertIn('Summary', body)
        self.assertEqual(alerts[0]['subject'], 'sequence-complete:m1')

        # Marker persisted.
        self.assertEqual(len(self._signaled_markers('m1')), 1)

    def test_re_detect_does_not_double_dm(self):
        self._write_sequence(_make_sequence(seq_id='m2', status='complete'))
        with mock.patch.object(
            on.chain_event_emit, 'emit_event', return_value=True,
        ) as emit:
            on._maybe_signal_sequence_complete('m2')
            on._maybe_signal_sequence_complete('m2')  # re-tick / crash-resume
            on._maybe_signal_sequence_complete('m2')

        # Event + DM fired exactly once across three detections.
        self.assertEqual(emit.call_count, 1)
        self.assertEqual(len(self._read_alerts()), 1)
        self.assertEqual(len(self._signaled_markers('m2')), 1)

    def test_gh_veto_suppresses_signal(self):
        # A step GitHub still reports as OPEN vetoes the completion DM — no
        # event, no DM, no marker — so a later real merge can still fire it.
        self._gh_state_mock.return_value = 'OPEN'
        self._write_sequence(_make_sequence(seq_id='m3', status='complete'))
        with mock.patch.object(
            on.chain_event_emit, 'emit_event', return_value=True,
        ) as emit:
            on._maybe_signal_sequence_complete('m3')

        self.assertEqual(emit.call_count, 0)
        self.assertEqual(self._read_alerts(), [])
        self.assertEqual(len(self._signaled_markers('m3')), 0)

    def test_not_complete_sequence_is_noop(self):
        self._write_sequence(_make_sequence(seq_id='m4', status='active'))
        with mock.patch.object(
            on.chain_event_emit, 'emit_event', return_value=True,
        ) as emit:
            on._maybe_signal_sequence_complete('m4')

        self.assertEqual(emit.call_count, 0)
        self.assertEqual(self._read_alerts(), [])
        # gh veto never runs on a non-complete sequence.
        self._gh_state_mock.assert_not_called()


# ===================================================================
# Layer 3 — end-to-end through _signal_sequence_step_merged
# ===================================================================


class EndToEndThroughStepMerged(_Harness):

    def _active_two_step(self, seq_id='e2e'):
        """Active sequence: step-a merged, step-b dispatched (the last step)."""
        return _make_sequence(
            seq_id=seq_id,
            status='active',
            steps=[
                _make_step('step-a', deps=[], status='merged',
                           merged_at='2026-06-16T00:00:00Z',
                           pr_url='https://github.com/larry/r/pull/1'),
                _make_step('step-b', deps=['step-a'], status='dispatched',
                           dispatched_at='2026-06-16T00:30:00Z'),
            ],
            current_steps=['step-b'],
        )

    def test_last_step_merge_fires_completion_once(self):
        self._write_sequence(self._active_two_step('e1'))
        with mock.patch.object(
            on.chain_event_emit, 'emit_event', return_value=True,
        ) as emit:
            seq_id = on._signal_sequence_step_merged(
                task_id='step-b',
                pr_url='https://github.com/larry/r/pull/2',
                merged_at_iso='2026-06-16T01:00:00Z',
            )
        self.assertEqual(seq_id, 'e1')
        on_disk = self._read_sequence('e1')
        self.assertEqual(on_disk['status'], 'complete')

        # One sequence_complete event + one DM.
        seq_complete_calls = [
            c for c in emit.call_args_list
            if c.kwargs.get('event_type') == 'sequence_complete'
        ]
        self.assertEqual(len(seq_complete_calls), 1)
        self.assertEqual(len(self._read_alerts()), 1)
        self.assertEqual(len(self._signaled_markers('e1')), 1)

    def test_re_fire_after_complete_does_not_double_dm(self):
        self._write_sequence(self._active_two_step('e2'))
        with mock.patch.object(
            on.chain_event_emit, 'emit_event', return_value=True,
        ):
            on._signal_sequence_step_merged(
                task_id='step-b',
                pr_url='https://github.com/larry/r/pull/2',
                merged_at_iso='2026-06-16T01:00:00Z',
            )
        self.assertEqual(len(self._read_alerts()), 1)

        # Crash-resume re-processes the same merged step.
        with mock.patch.object(
            on.chain_event_emit, 'emit_event', return_value=True,
        ) as emit2:
            on._signal_sequence_step_merged(
                task_id='step-b',
                pr_url='https://github.com/larry/r/pull/2',
                merged_at_iso='2026-06-16T02:00:00Z',
            )
        # No second DM, no second event.
        self.assertEqual(len(self._read_alerts()), 1)
        self.assertEqual(
            [c for c in emit2.call_args_list
             if c.kwargs.get('event_type') == 'sequence_complete'],
            [],
        )
        self.assertEqual(len(self._signaled_markers('e2')), 1)

    def test_non_final_step_merge_fires_no_completion(self):
        # Merging step-a (step-b still dispatched) must NOT complete or signal.
        seq = _make_sequence(
            seq_id='e3',
            status='active',
            steps=[
                _make_step('step-a', deps=[], status='dispatched',
                           dispatched_at='2026-06-16T00:00:00Z'),
                _make_step('step-b', deps=['step-a'], status='pending'),
            ],
            current_steps=['step-a'],
        )
        self._write_sequence(seq)
        with mock.patch.object(
            on.chain_event_emit, 'emit_event', return_value=True,
        ) as emit:
            on._signal_sequence_step_merged(
                task_id='step-a',
                pr_url='https://github.com/larry/r/pull/1',
                merged_at_iso='2026-06-16T00:30:00Z',
            )
        on_disk = self._read_sequence('e3')
        self.assertEqual(on_disk['status'], 'active')
        self.assertEqual(
            [c for c in emit.call_args_list
             if c.kwargs.get('event_type') == 'sequence_complete'],
            [],
        )
        self.assertEqual(self._read_alerts(), [])
        self.assertEqual(len(self._signaled_markers('e3')), 0)


if __name__ == '__main__':
    unittest.main()
