#!/usr/bin/env python3
"""Fixtures for PR-S4 — outbox_notifier build-sequence kickoff handler.

Covers the new `_handle_build_sequence_advancer_kickoff` branch added to
`scripts/outbox_notifier.py`. The handler fires when Beacon emits an
APPROVAL_REQUEST marker with `target_agent: build_sequence_advancer`
(after Larry's `approve sequence <seq-id>` shortcut), reads the sequence
file at `~/agents/blackboard/build-sequences/<seq-id>.json`, validates it
via `build_sequence_validator.validate_dag`, and transitions the
sequence's `status` from `pending` to `active` with a `kickoff-
acknowledged` audit_log entry. Step dispatch is left to the next advancer
tick (≤5 min) per spec § 5.2 — the handler is route-only.

Test isolation discipline (PR #137): every test reroutes
`outbox_notifier.AGENTS_ROOT` / `safe_write_inbox.INBOXES_ROOT` /
`larry_alerts.ALERTS_FILE` to a per-test tmpdir. NO writes ever reach
`~/agents/` (real droplet state).

Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_outbox_notifier_sequence_handlers
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import build_sequence_validator as bsv      # noqa: E402
import larry_alerts as la                   # noqa: E402
import outbox_notifier as on                # noqa: E402
import safe_write_inbox as swi              # noqa: E402


def _make_step(step_id, deps=None, status='pending', pr_url=None,
               dispatched_at=None, merged_at=None):
    """Match the shape the PR-S2 validator's REQUIRED_STEP_FIELDS expects."""
    return {
        'step_id': step_id,
        'label': f'Step {step_id}',
        'depends_on': deps or [],
        'dispatch_text': (
            f'Build {step_id} per spec § X. Review focus: Y.'
        ),
        'target_repo': 'ourliberty-agent-core',
        'task_type': 'feature-development',
        'status': status,
        'dispatched_at': dispatched_at,
        'merged_at': merged_at,
        'pr_url': pr_url,
        'current_actor': None,
        'failure_reason': None,
    }


def _make_sequence(seq_id='pulse-upgrade-001', status='pending',
                   steps=None, current_steps=None, audit_log=None):
    """Build a sequence dict that passes the PR-S2 validator."""
    if steps is None:
        steps = [
            _make_step('step1', deps=[]),
            _make_step('step2', deps=['step1']),
            _make_step('step3', deps=['step1']),
        ]
    return {
        'seq_id': seq_id,
        'label': f'Sequence {seq_id}',
        'spec_doc': 'agents/beacon/specs/build-sequence-orchestrator.md',
        'created_at': '2026-05-27T00:00:00+00:00',
        'created_by': 'beacon',
        'status': status,
        'current_steps': current_steps if current_steps is not None else [],
        'steps': steps,
        'audit_log': audit_log if audit_log is not None else [
            {'ts': '2026-05-27T00:00:00+00:00', 'event': 'sequence-created',
             'actor': 'beacon'},
        ],
    }


class _KickoffHandlerHarness(unittest.TestCase):
    """Reroute outbox_notifier + safe_write_inbox + larry_alerts to a
    per-test tmpdir so no test write reaches ~/agents/ on the droplet."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)

        # Save and override outbox_notifier module-level paths.
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

        # Save and override safe_write_inbox module-level paths.
        self._swi_originals = {
            'AGENTS_ROOT': swi.AGENTS_ROOT,
            'INBOXES_ROOT': swi.INBOXES_ROOT,
            'ROUTING_EVENTS_LOG': swi.ROUTING_EVENTS_LOG,
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'

        # Save and override larry_alerts ALERTS_FILE so test alerts land
        # in the tmpdir and can be inspected. larry_alerts.append_alert
        # also writes cooldown markers under COOLDOWN_ROOT; reroute that
        # too so a passing test doesn't suppress the next test's alert.
        self._la_originals = {
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
        }
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'

        on.ensure_dirs()
        (self._root / 'blackboard' / 'build-sequences').mkdir(
            parents=True, exist_ok=True,
        )

    def tearDown(self):
        for name, value in self._on_originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        self._tmp.cleanup()

    # ------------- helpers -------------

    def _write_sequence(self, seq):
        path = (
            on.AGENTS_ROOT / 'blackboard' / 'build-sequences'
            / f'{seq["seq_id"]}.json'
        )
        path.write_text(json.dumps(seq, indent=2))
        return path

    def _read_sequence(self, seq_id):
        path = (
            on.AGENTS_ROOT / 'blackboard' / 'build-sequences'
            / f'{seq_id}.json'
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

    def _make_marker(self, **overrides):
        """Canonical PR-S4 kickoff marker: target_agent build_sequence_advancer."""
        payload = {
            'task_id': 'kickoff-pulse-upgrade-001',
            'summary': 'Kick off the Pulse cycle upgrade sequence.',
            'target_agent': 'build_sequence_advancer',
            'prompt': 'kickoff pulse-upgrade-001',
        }
        payload.update(overrides)
        return (
            f'=== APPROVAL_REQUEST ===\n{json.dumps(payload)}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )

    def _make_envelope(self, result_text=None, task_id='dispatch-larry-001',
                       agent='beacon', source='larry', reply_chat_id=42):
        if result_text is None:
            result_text = (
                "Sequence file authored, DAG preflight PASSed.\n\n"
                + self._make_marker()
            )
        return {
            'task_id': task_id,
            'agent': agent,
            'source': source,
            'result': result_text,
            'reply_chat_id': reply_chat_id,
            'started_at': '2026-05-27T00:00:00Z',
            'completed_at': '2026-05-27T00:00:05Z',
            'duration_sec': 5,
            'exit_code': 0,
            'model': 'claude-opus-4-7',
        }


# ===================================================================
# Happy path — pending → active
# ===================================================================


class KickoffPendingToActive(_KickoffHandlerHarness):

    def test_kickoff_transitions_pending_to_active(self):
        seq = _make_sequence(seq_id='pulse-upgrade-001', status='pending')
        self._write_sequence(seq)
        result = on._handle_build_sequence_advancer_kickoff(
            self._make_envelope(), self._make_envelope()['result'],
        )
        self.assertIsNotNone(result, 'handler should claim the marker')

        on_disk = self._read_sequence('pulse-upgrade-001')
        self.assertEqual(on_disk['status'], 'active')

    def test_kickoff_appends_audit_log_entry(self):
        seq = _make_sequence(seq_id='pulse-upgrade-001', status='pending')
        initial_log_len = len(seq['audit_log'])
        self._write_sequence(seq)
        on._handle_build_sequence_advancer_kickoff(
            self._make_envelope(), self._make_envelope()['result'],
        )
        on_disk = self._read_sequence('pulse-upgrade-001')
        self.assertEqual(len(on_disk['audit_log']), initial_log_len + 1)
        last = on_disk['audit_log'][-1]
        self.assertEqual(last['event'], 'kickoff-acknowledged')
        # PR-S4 rectification (L4): actor is `outbox-notifier`, not
        # `advancer`. The notifier wrote the entry; calling it
        # `advancer` was misleading for ops debugging.
        self.assertEqual(last['actor'], 'outbox-notifier')
        self.assertIn('ts', last)
        self.assertIn('task_id', last)

    def test_kickoff_does_not_dispatch_first_step_inline(self):
        """Per preflight Q6 option a: kickoff handler is route-only.
        The next advancer tick dispatches steps via the daemon's logic;
        the notifier MUST NOT write to Beacon's inbox here."""
        seq = _make_sequence(seq_id='pulse-upgrade-001', status='pending')
        self._write_sequence(seq)
        on._handle_build_sequence_advancer_kickoff(
            self._make_envelope(), self._make_envelope()['result'],
        )
        # Beacon's inbox stays empty — no step dispatch happened.
        beacon_inbox = on.INBOXES_ROOT / 'beacon'
        if beacon_inbox.is_dir():
            self.assertEqual(
                list(beacon_inbox.glob('*.json')), [],
                'kickoff handler must not dispatch the first step inline; '
                'the advancer daemon owns step dispatch per spec § 5.2',
            )
        # And Forge's inbox stays empty too — the kickoff marker
        # targets the advancer, not Forge.
        forge_inbox = on.INBOXES_ROOT / 'forge'
        if forge_inbox.is_dir():
            self.assertEqual(list(forge_inbox.glob('*.json')), [])

    def test_kickoff_through_process_outbox(self):
        """End-to-end through process_outbox — the integration most
        likely to break if the routing collision check is misordered."""
        seq = _make_sequence(seq_id='pulse-upgrade-001', status='pending')
        self._write_sequence(seq)
        body = self._make_envelope()
        outbox = on.OUTBOXES_ROOT / 'beacon'
        outbox.mkdir(parents=True, exist_ok=True)
        f = outbox / 'larry-kickoff-001.json'
        f.write_text(json.dumps(body))
        status = on.process_outbox(f)
        self.assertEqual(status, 'sequence-kickoff-handled')
        on_disk = self._read_sequence('pulse-upgrade-001')
        self.assertEqual(on_disk['status'], 'active')


# ===================================================================
# Idempotency battery (status != pending → WARN no-op)
# ===================================================================


class KickoffIdempotency(_KickoffHandlerHarness):

    def _assert_warn_noop(self, status):
        seq = _make_sequence(seq_id=f'seq-{status}', status=status)
        original_log_len = len(seq['audit_log'])
        original_kickoff_acks = [
            e for e in seq['audit_log']
            if isinstance(e, dict) and e.get('event') == 'kickoff-acknowledged'
        ]
        self._write_sequence(seq)
        result = on._handle_build_sequence_advancer_kickoff(
            self._make_envelope(
                result_text=(
                    "Re-emitting kickoff for an already-active sequence "
                    f"(idempotency test).\n\n"
                    + self._make_marker(
                        prompt=f'kickoff seq-{status}',
                        task_id=f'kickoff-seq-{status}',
                    )
                ),
            ),
            (
                "Re-emitting kickoff for an already-active sequence "
                f"(idempotency test).\n\n"
                + self._make_marker(
                    prompt=f'kickoff seq-{status}',
                    task_id=f'kickoff-seq-{status}',
                )
            ),
        )
        self.assertIsNotNone(result, 'handler claims the marker on no-op too')
        on_disk = self._read_sequence(f'seq-{status}')
        # PR-S4 rectification (M3): idempotent no-op now appends a
        # `kickoff-duplicate-suppressed` audit entry so the trail is
        # honest. The load-bearing invariant — no DUPLICATE
        # `kickoff-acknowledged` events, no status mutation, no DM — is
        # preserved.
        self.assertEqual(
            on_disk['status'], status,
            f'idempotent no-op for status={status} must NOT mutate status',
        )
        on_disk_kickoff_acks = [
            e for e in on_disk['audit_log']
            if isinstance(e, dict) and e.get('event') == 'kickoff-acknowledged'
        ]
        self.assertEqual(
            len(on_disk_kickoff_acks), len(original_kickoff_acks),
            f'idempotent no-op for status={status} must NOT append a '
            f'second kickoff-acknowledged event',
        )
        # The dedup-suppressed event WAS appended (one new entry).
        self.assertEqual(
            len(on_disk['audit_log']), original_log_len + 1,
            f'idempotent no-op for status={status} appends exactly one '
            f'kickoff-duplicate-suppressed audit entry',
        )
        last = on_disk['audit_log'][-1]
        self.assertEqual(last['event'], 'kickoff-duplicate-suppressed')
        self.assertEqual(last['actor'], 'outbox-notifier')
        self.assertEqual(last['duplicate_task_id'], 'dispatch-larry-001')
        self.assertEqual(last['status_at_suppression'], status)
        # No alert/DM either.
        self.assertEqual(
            self._read_alerts(), [],
            f'idempotent no-op for status={status} must NOT fire a DM',
        )

    def test_active_is_warn_noop(self):
        self._assert_warn_noop('active')

    def test_paused_is_warn_noop(self):
        self._assert_warn_noop('paused')

    def test_complete_is_warn_noop(self):
        self._assert_warn_noop('complete')

    def test_failed_is_warn_noop(self):
        self._assert_warn_noop('failed')

    def test_archived_is_warn_noop(self):
        self._assert_warn_noop('archived')

    def test_double_kickoff_does_not_duplicate_audit_log(self):
        """Beacon's self-check: re-emit kickoff marker on an active
        sequence → no duplicate audit_log entry."""
        seq = _make_sequence(seq_id='double-kick', status='pending')
        self._write_sequence(seq)
        # First kickoff — should transition to active.
        on._handle_build_sequence_advancer_kickoff(
            self._make_envelope(
                result_text=(
                    "First.\n\n"
                    + self._make_marker(prompt='kickoff double-kick',
                                        task_id='kickoff-double-kick')
                ),
            ),
            (
                "First.\n\n"
                + self._make_marker(prompt='kickoff double-kick',
                                    task_id='kickoff-double-kick')
            ),
        )
        after_first = self._read_sequence('double-kick')
        self.assertEqual(after_first['status'], 'active')
        kickoff_entries = [
            e for e in after_first['audit_log']
            if e.get('event') == 'kickoff-acknowledged'
        ]
        self.assertEqual(len(kickoff_entries), 1)
        # Second kickoff — should WARN no-op, no duplicate.
        on._handle_build_sequence_advancer_kickoff(
            self._make_envelope(
                result_text=(
                    "Second (duplicate).\n\n"
                    + self._make_marker(prompt='kickoff double-kick',
                                        task_id='kickoff-double-kick')
                ),
            ),
            (
                "Second (duplicate).\n\n"
                + self._make_marker(prompt='kickoff double-kick',
                                    task_id='kickoff-double-kick')
            ),
        )
        after_second = self._read_sequence('double-kick')
        kickoff_entries = [
            e for e in after_second['audit_log']
            if e.get('event') == 'kickoff-acknowledged'
        ]
        self.assertEqual(
            len(kickoff_entries), 1,
            'second kickoff on active sequence must not append a duplicate '
            'kickoff-acknowledged event',
        )


# ===================================================================
# Routing collision regression — non-advancer markers fall through
# ===================================================================


class KickoffRouting(_KickoffHandlerHarness):

    def test_target_agent_forge_falls_through(self):
        """Marker with target_agent=forge → kickoff handler returns None,
        existing headless-approval handler picks it up."""
        body = self._make_envelope(
            result_text=(
                "Plan.\n\n"
                + self._make_marker(
                    target_agent='forge',
                    prompt='Build Z per spec.',
                    task_id='build-z-001',
                )
            ),
        )
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNone(
            result,
            'kickoff handler must NOT claim a target_agent=forge marker; '
            'existing headless-approval handler owns that path',
        )

    def test_target_agent_mirror_falls_through(self):
        body = self._make_envelope(
            result_text=(
                "Plan.\n\n"
                + self._make_marker(
                    target_agent='mirror',
                    prompt='Review PR #42.',
                    task_id='review-42',
                )
            ),
        )
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNone(result)

    def test_target_agent_pulse_falls_through(self):
        body = self._make_envelope(
            result_text=(
                "Plan.\n\n"
                + self._make_marker(
                    target_agent='pulse',
                    prompt='Run Check IV.',
                    task_id='check-iv-001',
                )
            ),
        )
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNone(result)

    def test_target_agent_beacon_falls_through(self):
        body = self._make_envelope(
            result_text=(
                "Plan.\n\n"
                + self._make_marker(
                    target_agent='beacon',
                    prompt='Self-dispatch.',
                    task_id='self-001',
                )
            ),
        )
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNone(result)

    def test_no_marker_falls_through(self):
        body = self._make_envelope(
            result_text="Just narrative, no marker block.",
        )
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNone(result)

    def test_non_beacon_agent_does_not_fire(self):
        body = self._make_envelope(agent='forge')
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNone(result)

    def test_non_larry_source_does_not_fire(self):
        """Chat-mode dispatches arrive with source != 'larry'. The
        kickoff handler must defer to the chat-mode path entirely."""
        body = self._make_envelope(source='beacon')
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNone(result)

    def test_malformed_marker_falls_through(self):
        """Unparseable marker → kickoff handler returns None, the
        existing headless-approval handler logs its own diagnostic."""
        body = self._make_envelope(
            result_text=(
                "=== APPROVAL_REQUEST ===\n"
                '{"task_id": "broken", "target_agent": "build_sequence_advancer"}\n'
                "=== END_APPROVAL_REQUEST ==="
                # Missing required fields (summary, prompt) → MalformedApprovalMarker.
            ),
        )
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNone(result)


# ===================================================================
# Failure modes — sequence file missing / malformed / DAG invalid
# ===================================================================


class KickoffFailureModes(_KickoffHandlerHarness):

    def test_missing_sequence_file_dms_larry(self):
        # Don't write a sequence file; handler should DM + return sentinel.
        body = self._make_envelope()
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNotNone(result)
        self.assertIn('missing', result)
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1, 'one DM for missing-file failure')
        alert = alerts[0]
        self.assertEqual(alert['severity'], 'warning')
        self.assertIn('sequence-kickoff-pulse-upgrade-001', alert['subject'])
        self.assertIn('missing', alert['message'].lower())

    def test_malformed_json_dms_larry(self):
        # Write a file that's not valid JSON.
        seq_path = (
            on.AGENTS_ROOT / 'blackboard' / 'build-sequences'
            / 'pulse-upgrade-001.json'
        )
        seq_path.write_text('this is not json {')
        body = self._make_envelope()
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNotNone(result)
        self.assertIn('invalid-json', result)
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('JSON', alerts[0]['message'])

    def test_validator_invalid_dag_dms_larry(self):
        # Schema-valid file but with a cycle — validator catches it.
        seq = _make_sequence(
            seq_id='cycle-seq',
            status='pending',
            steps=[
                _make_step('a', deps=['b']),
                _make_step('b', deps=['a']),  # cycle!
            ],
        )
        self._write_sequence(seq)
        body = self._make_envelope(
            result_text=(
                "Plan.\n\n"
                + self._make_marker(
                    prompt='kickoff cycle-seq',
                    task_id='kickoff-cycle-seq',
                )
            ),
        )
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNotNone(result)
        self.assertIn('invalid', result)
        # Sequence file MUST NOT have been transitioned to active.
        on_disk = self._read_sequence('cycle-seq')
        self.assertEqual(on_disk['status'], 'pending')
        # One DM fired.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)

    def test_prompt_without_kickoff_verb_warns_and_archives(self):
        """target_agent=build_sequence_advancer but prompt isn't
        `kickoff <seq-id>` — handler claims the marker (returns
        non-None so the outbox archives without falling through to
        Forge), but no sequence-file write happens."""
        body = self._make_envelope(
            result_text=(
                "Plan.\n\n"
                + self._make_marker(
                    prompt='do something weird with the advancer',
                    task_id='unrelated-task-id',
                )
            ),
        )
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNotNone(result)
        self.assertIn('no-seq-id', result)

    def test_seq_id_from_task_id_fallback(self):
        """If prompt isn't parseable but task_id is `kickoff-<seq-id>`,
        the handler still routes correctly."""
        seq = _make_sequence(seq_id='fallback-seq', status='pending')
        self._write_sequence(seq)
        # Build a marker where prompt is something noisy but task_id
        # carries the canonical `kickoff-<seq-id>` shape.
        body = self._make_envelope(
            result_text=(
                "Plan.\n\n"
                + self._make_marker(
                    prompt='approved per Larry chat 2026-05-27',
                    task_id='kickoff-fallback-seq',
                )
            ),
        )
        result = on._handle_build_sequence_advancer_kickoff(
            body, body['result'],
        )
        self.assertIsNotNone(result)
        on_disk = self._read_sequence('fallback-seq')
        self.assertEqual(on_disk['status'], 'active')


# ===================================================================
# Shortcut-shape reference tests
# ===================================================================
# These tests document the EXPECTED sequence-file mutation shape for
# each of the 6 shortcuts Beacon's CLAUDE.md teaches her to handle.
# Beacon's parser is the Claude agent reading her CLAUDE.md, not a
# Python module — so these tests assert the LOCKED SCHEMA SHAPE the
# shortcuts must produce (per Mirror review focus: shortcut idempotency
# + no schema drift). If a future PR introduces a Python helper to
# encode these mutations, these reference fixtures become the contract
# the helper must satisfy.
#
# Each test builds the BEFORE state, applies the documented mutation
# manually (mirroring what Beacon would write), and asserts the AFTER
# state passes the PR-S2 validator. Idempotency is checked by
# re-applying the mutation against the AFTER state and asserting the
# AFTER-AFTER state is identical (no duplicate audit_log entries).


# PR-S4 rectification (L3): the aspirational ShortcutMutationShapes
# class that USED to live here was removed. It documented expected
# sequence-file mutation shapes for the 5 non-kickoff shortcuts but
# never executed the mutations (no helper functions existed). With
# `scripts/sequence_shortcut_helpers.py` shipped, real behavior tests
# live in `scripts/tests/test_sequence_shortcut_helpers.py` —
# happy-path + idempotency + atomic-write + audit_log shape for each
# of the 5 helpers, against the same PR-S2 validator. See that file.


# ============================================================================
# V6 (orchestrator-rectification-v2) — _signal_sequence_step_merged
# ============================================================================


import sequence_shortcut_helpers as ssh  # noqa: E402


class SignalSequenceStepMergedHarness(_KickoffHandlerHarness):
    """Shared setUp from _KickoffHandlerHarness — same per-test tmpdir
    isolation. Extra rerouting for `sequence_shortcut_helpers.AGENTS_ROOT`
    so the V6 helper writes into the same tmpdir as outbox_notifier."""

    def setUp(self):
        super().setUp()
        self._ssh_original_root = ssh.AGENTS_ROOT
        ssh.AGENTS_ROOT = self._root

    def tearDown(self):
        ssh.AGENTS_ROOT = self._ssh_original_root
        super().tearDown()

    def _make_active_sequence(
        self, seq_id='live-seq-001', dispatched_step='step-a',
    ):
        return _make_sequence(
            seq_id=seq_id,
            status='active',
            steps=[
                _make_step(
                    dispatched_step, deps=[], status='dispatched',
                    dispatched_at='2026-05-27T00:30:00Z',
                ),
                _make_step(
                    'step-b', deps=[dispatched_step], status='pending',
                ),
            ],
            current_steps=[dispatched_step],
        )


class SignalSequenceStepMergedHappyPath(SignalSequenceStepMergedHarness):

    def test_active_sequence_with_matching_step_id_flips_to_merged(self):
        seq = self._make_active_sequence()
        self._write_sequence(seq)
        result = on._signal_sequence_step_merged(
            task_id='step-a',
            pr_url='https://github.com/larry/r/pull/77',
            merged_at_iso='2026-05-27T01:00:00Z',
        )
        self.assertEqual(result, 'live-seq-001')
        on_disk = self._read_sequence('live-seq-001')
        step_a = next(
            s for s in on_disk['steps'] if s['step_id'] == 'step-a'
        )
        self.assertEqual(step_a['status'], 'merged')
        self.assertEqual(step_a['pr_url'],
                         'https://github.com/larry/r/pull/77')
        self.assertEqual(step_a['merged_at'], '2026-05-27T01:00:00Z')
        self.assertNotIn('step-a', on_disk['current_steps'])
        # An audit-log entry was appended.
        last = on_disk['audit_log'][-1]
        self.assertEqual(last['event'], 'step-merged')
        self.assertEqual(last['actor'], 'notifier')

    def test_no_matching_step_returns_none(self):
        seq = self._make_active_sequence()
        self._write_sequence(seq)
        result = on._signal_sequence_step_merged(
            task_id='unrelated-task',
            pr_url='https://github.com/x/y/pull/1',
            merged_at_iso='2026-05-27T01:00:00Z',
        )
        self.assertIsNone(result)
        # Sequence file unchanged.
        on_disk = self._read_sequence('live-seq-001')
        step_a = next(s for s in on_disk['steps'] if s['step_id'] == 'step-a')
        self.assertEqual(step_a['status'], 'dispatched')

    def test_terminal_sequence_is_not_mutated(self):
        # A completed or paused sequence with a matching task_id is
        # ignored — historical reconciliation isn't safe without operator
        # intent (a re-fire could re-open a closed sequence).
        for term_status in ('complete', 'failed', 'archived', 'paused'):
            with self.subTest(status=term_status):
                seq = _make_sequence(
                    seq_id=f'term-{term_status}',
                    status=term_status,
                    steps=[_make_step(
                        'x', status='merged',
                        merged_at='2026-05-26T00:00:00Z',
                        pr_url='https://github.com/x/y/pull/1',
                    )],
                )
                self._write_sequence(seq)
                result = on._signal_sequence_step_merged(
                    task_id='x',
                    pr_url='https://github.com/x/y/pull/99',
                    merged_at_iso='2026-05-27T01:00:00Z',
                )
                self.assertIsNone(result)
                # File unchanged — original pr_url preserved.
                on_disk = self._read_sequence(f'term-{term_status}')
                self.assertEqual(
                    on_disk['steps'][0]['pr_url'],
                    'https://github.com/x/y/pull/1',
                )

    def test_pending_sequence_with_matching_step_is_mutated(self):
        # `pending` is also considered active for V6 purposes — a step
        # somehow merging on a sequence that hasn't yet kicked off is an
        # edge case but we want the signal to land cleanly rather than
        # be silently dropped.
        seq = _make_sequence(
            seq_id='pending-seq',
            status='pending',
            steps=[_make_step('step-a', status='dispatched',
                              dispatched_at='2026-05-27T00:00:00Z')],
            current_steps=['step-a'],
        )
        self._write_sequence(seq)
        result = on._signal_sequence_step_merged(
            task_id='step-a',
            pr_url='https://github.com/x/y/pull/3',
            merged_at_iso='2026-05-27T01:00:00Z',
        )
        self.assertEqual(result, 'pending-seq')
        on_disk = self._read_sequence('pending-seq')
        self.assertEqual(on_disk['steps'][0]['status'], 'merged')


class SignalSequenceStepMergedRobustness(SignalSequenceStepMergedHarness):
    """Defensive cases — the scan must NOT crash the daemon."""

    def test_no_sequences_directory_returns_none(self):
        # Wipe the build-sequences directory entirely; the scan should
        # short-circuit cleanly.
        import shutil
        shutil.rmtree(on.AGENTS_ROOT / 'blackboard' / 'build-sequences')
        result = on._signal_sequence_step_merged(
            task_id='x',
            pr_url='https://github.com/x/y/pull/1',
            merged_at_iso='2026-05-27T01:00:00Z',
        )
        self.assertIsNone(result)

    def test_malformed_sequence_file_is_skipped(self):
        # A broken JSON file in the sequences dir must not crash the
        # scan. We add a malformed file AND a valid matching one — the
        # scan should still apply the signal to the valid one.
        bad_path = (
            on.AGENTS_ROOT / 'blackboard' / 'build-sequences'
            / 'corrupt.json'
        )
        bad_path.write_text('{not valid json')
        seq = self._make_active_sequence(seq_id='good-seq')
        self._write_sequence(seq)
        result = on._signal_sequence_step_merged(
            task_id='step-a',
            pr_url='https://github.com/x/y/pull/4',
            merged_at_iso='2026-05-27T01:00:00Z',
        )
        self.assertEqual(result, 'good-seq')

    def test_idempotent_when_step_already_merged(self):
        # Outbox re-processing after a notifier crash: the second call
        # must not double-fire (apply_step_merged returns applied=False).
        seq = _make_sequence(
            seq_id='already-merged-seq',
            status='active',
            steps=[_make_step(
                'step-a', status='merged',
                merged_at='2026-05-27T00:00:00Z',
                pr_url='https://github.com/x/y/pull/1',
            )],
        )
        self._write_sequence(seq)
        result = on._signal_sequence_step_merged(
            task_id='step-a',
            pr_url='https://github.com/x/y/pull/99',  # different URL
            merged_at_iso='2026-05-28T00:00:00Z',
        )
        # Returns the matched seq_id (it WAS found) but no mutation.
        self.assertEqual(result, 'already-merged-seq')
        on_disk = self._read_sequence('already-merged-seq')
        self.assertEqual(
            on_disk['steps'][0]['pr_url'],
            'https://github.com/x/y/pull/1',
        )
        self.assertEqual(
            on_disk['steps'][0]['merged_at'],
            '2026-05-27T00:00:00Z',
        )


if __name__ == '__main__':
    unittest.main()
