#!/usr/bin/env python3
"""Fixtures for kickoff-approve-routing-gap-001 — the CHAT/dashboard-approve
build-sequence kickoff path through `beacon_approval_handler.dispatch_approved`.

Before this fix, a chat-approved `approve sequence <seq-id>` kickoff (Beacon
emits an APPROVAL_REQUEST with target_agent=build_sequence_advancer, and on
approve the bot calls `dispatch_approved`) took the plain inbox write and
landed at `inboxes/build_sequence_advancer/<...>.json` — a directory with NO
consumer — so the sequence sat at status=pending forever. The advancer daemon
reads sequence files under `blackboard/build-sequences/`, not that inbox.

The fix routes a `target_agent == build_sequence_advancer` entry to the SAME
transition helper (`build_sequence_kickoff.apply_kickoff_transition`) the
autonomous outbox path uses, so the sequence flips pending->active with a
`kickoff-acknowledged` audit entry and NO file is written to the dead-end
inbox. This module covers that convergence: the transition, idempotency, and
the missing/malformed failure DMs.

Test isolation discipline: every test reroutes `beacon_approval_handler.
AGENTS_ROOT`, `safe_write_inbox.INBOXES_ROOT`, and `larry_alerts.ALERTS_FILE`/
`COOLDOWN_ROOT` to a per-test tmpdir. NO writes reach `~/agents/`.

Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_dispatch_approved_kickoff
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import beacon_approval_handler as ah      # noqa: E402
import build_sequence_kickoff as bsk      # noqa: E402
import larry_alerts as la                 # noqa: E402
import safe_write_inbox as swi            # noqa: E402


def _make_step(step_id, deps=None, status='pending'):
    """Match the shape the PR-S2 validator's REQUIRED_STEP_FIELDS expects."""
    return {
        'step_id': step_id,
        'label': f'Step {step_id}',
        'depends_on': deps or [],
        'dispatch_text': f'Build {step_id} per spec § X. Review focus: Y.',
        'target_repo': 'ourliberty-agent-core',
        'task_type': 'feature-development',
        'status': status,
        'dispatched_at': None,
        'merged_at': None,
        'pr_url': None,
        'current_actor': None,
        'failure_reason': None,
    }


def _make_sequence(seq_id='chat-seq-001', status='pending', steps=None,
                   audit_log=None):
    if steps is None:
        steps = [
            _make_step('step1', deps=[]),
            _make_step('step2', deps=['step1']),
        ]
    return {
        'seq_id': seq_id,
        'label': f'Sequence {seq_id}',
        'spec_doc': 'agents/beacon/specs/build-sequence-orchestrator.md',
        'created_at': '2026-07-07T00:00:00+00:00',
        'created_by': 'beacon',
        'status': status,
        'current_steps': [],
        'steps': steps,
        'audit_log': audit_log if audit_log is not None else [
            {'ts': '2026-07-07T00:00:00+00:00', 'event': 'sequence-created',
             'actor': 'beacon'},
        ],
    }


class _ChatApproveKickoffHarness(unittest.TestCase):
    """Reroute beacon_approval_handler + safe_write_inbox + larry_alerts to a
    per-test tmpdir so no test write reaches ~/agents/ on the droplet."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)

        self._ah_root = ah.AGENTS_ROOT
        ah.AGENTS_ROOT = self._root

        self._swi_originals = {
            'AGENTS_ROOT': swi.AGENTS_ROOT,
            'INBOXES_ROOT': swi.INBOXES_ROOT,
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'

        self._la_originals = {
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
        }
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'

        (self._root / 'blackboard' / 'build-sequences').mkdir(
            parents=True, exist_ok=True,
        )

    def tearDown(self):
        ah.AGENTS_ROOT = self._ah_root
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        self._tmp.cleanup()

    # ------------- helpers -------------

    def _seq_path(self, seq_id):
        return (
            self._root / 'blackboard' / 'build-sequences' / f'{seq_id}.json'
        )

    def _write_sequence(self, seq):
        path = self._seq_path(seq['seq_id'])
        path.write_text(json.dumps(seq, indent=2))
        return path

    def _read_sequence(self, seq_id):
        return json.loads(self._seq_path(seq_id).read_text())

    def _read_alerts(self):
        if not la.ALERTS_FILE.exists():
            return []
        return [
            json.loads(line)
            for line in la.ALERTS_FILE.read_text().splitlines()
            if line.strip()
        ]

    def _kickoff_entry(self, seq_id='chat-seq-001', prompt=None,
                       task_id=None):
        return {
            'id': f'approval-{seq_id}',
            'target_agent': bsk.SEQUENCE_KICKOFF_TARGET_AGENT,
            'chat_id': 7998341473,
            'dispatch_payload': {
                'task_id': task_id or f'kickoff-{seq_id}',
                'summary': f'Kick off {seq_id}.',
                'target_agent': bsk.SEQUENCE_KICKOFF_TARGET_AGENT,
                'prompt': prompt if prompt is not None else f'kickoff {seq_id}',
            },
        }

    def _advancer_inbox_files(self):
        inbox = swi.INBOXES_ROOT / bsk.SEQUENCE_KICKOFF_TARGET_AGENT
        if not inbox.is_dir():
            return []
        return list(inbox.glob('*.json'))


class ChatApproveTransition(_ChatApproveKickoffHarness):

    def test_transitions_pending_to_active(self):
        self._write_sequence(_make_sequence(seq_id='chat-seq-001',
                                            status='pending'))
        dest = ah.dispatch_approved(self._kickoff_entry('chat-seq-001'))
        # Returns the sequence-file path (its Path contract), NOT an inbox path.
        self.assertEqual(dest, self._seq_path('chat-seq-001'))
        self.assertEqual(self._read_sequence('chat-seq-001')['status'],
                         'active')

    def test_appends_exactly_one_kickoff_acknowledged_entry(self):
        seq = _make_sequence(seq_id='chat-seq-001', status='pending')
        initial_len = len(seq['audit_log'])
        self._write_sequence(seq)
        ah.dispatch_approved(self._kickoff_entry('chat-seq-001'))
        on_disk = self._read_sequence('chat-seq-001')
        self.assertEqual(len(on_disk['audit_log']), initial_len + 1)
        acks = [e for e in on_disk['audit_log']
                if e.get('event') == 'kickoff-acknowledged']
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0]['actor'], 'outbox-notifier')
        self.assertEqual(acks[0]['task_id'], 'kickoff-chat-seq-001')

    def test_no_write_to_dead_end_advancer_inbox(self):
        self._write_sequence(_make_sequence(seq_id='chat-seq-001',
                                            status='pending'))
        ah.dispatch_approved(self._kickoff_entry('chat-seq-001'))
        self.assertEqual(
            self._advancer_inbox_files(), [],
            'chat-approved kickoff must NOT land in the dead-end '
            'inboxes/build_sequence_advancer/ directory',
        )

    def test_seq_id_from_task_id_fallback(self):
        """Prompt not `kickoff <id>` but task_id is `kickoff-<id>` — still
        routes correctly (mirrors the notifier handler's fallback)."""
        self._write_sequence(_make_sequence(seq_id='fallback-seq',
                                            status='pending'))
        entry = self._kickoff_entry(
            'fallback-seq',
            prompt='approved per Larry chat 2026-07-07',
            task_id='kickoff-fallback-seq',
        )
        ah.dispatch_approved(entry)
        self.assertEqual(self._read_sequence('fallback-seq')['status'],
                         'active')


class ChatApproveIdempotency(_ChatApproveKickoffHarness):

    def test_second_approve_is_warn_noop(self):
        self._write_sequence(_make_sequence(seq_id='chat-seq-001',
                                            status='pending'))
        # First approve — transitions to active.
        ah.dispatch_approved(self._kickoff_entry('chat-seq-001'))
        after_first = self._read_sequence('chat-seq-001')
        self.assertEqual(after_first['status'], 'active')
        first_acks = [e for e in after_first['audit_log']
                      if e.get('event') == 'kickoff-acknowledged']
        self.assertEqual(len(first_acks), 1)

        # Second approve — WARN no-op, no duplicate kickoff-acknowledged.
        dest = ah.dispatch_approved(self._kickoff_entry('chat-seq-001'))
        self.assertEqual(dest, self._seq_path('chat-seq-001'))
        after_second = self._read_sequence('chat-seq-001')
        self.assertEqual(after_second['status'], 'active')
        second_acks = [e for e in after_second['audit_log']
                       if e.get('event') == 'kickoff-acknowledged']
        self.assertEqual(
            len(second_acks), 1,
            'second approve on an active sequence must not append a duplicate '
            'kickoff-acknowledged event',
        )
        # A kickoff-duplicate-suppressed entry keeps the trail honest.
        self.assertEqual(after_second['audit_log'][-1]['event'],
                         'kickoff-duplicate-suppressed')


class ChatApproveFailureModes(_ChatApproveKickoffHarness):

    def test_missing_sequence_file_dms_larry_and_does_not_crash(self):
        # No sequence file written.
        dest = ah.dispatch_approved(self._kickoff_entry('nope-seq'))
        # Returns a Path (contract) and does not raise.
        self.assertIsInstance(dest, Path)
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1, 'one DM for missing-file failure')
        self.assertIn('missing', alerts[0]['message'].lower())
        # Nothing written to the dead-end inbox either.
        self.assertEqual(self._advancer_inbox_files(), [])

    def test_malformed_json_dms_larry_and_does_not_crash(self):
        self._seq_path('chat-seq-001').write_text('this is not json {')
        dest = ah.dispatch_approved(self._kickoff_entry('chat-seq-001'))
        self.assertIsInstance(dest, Path)
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('JSON', alerts[0]['message'])


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # The failure-mode tests drive a Layer B-guarded chokepoint (larry_alerts)
    # against already-isolated state, so the guard would breach before the
    # test's own mocks. Opt out for the module so the guard is a pass-through;
    # the #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    unittest.main()
