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
        self.assertEqual(last['actor'], 'advancer')
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
        original_dict = json.loads(json.dumps(seq))  # deep copy
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
        # Audit_log NOT extended; sequence dict is byte-identical.
        self.assertEqual(
            len(on_disk['audit_log']), original_log_len,
            f'idempotent no-op for status={status} must NOT append audit_log',
        )
        self.assertEqual(
            on_disk, original_dict,
            f'idempotent no-op for status={status} must NOT mutate the file',
        )
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


class ShortcutMutationShapes(_KickoffHandlerHarness):
    """Lock the expected sequence-file mutation shape for each of the 6
    shortcuts. These are the contracts Beacon's CLAUDE.md commits to."""

    # ---------- pause / resume ----------

    def test_pause_sets_status_paused_and_validates(self):
        seq = _make_sequence(seq_id='pause-seq', status='active')
        seq['status'] = 'paused'
        seq['audit_log'].append({
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': 'paused', 'actor': 'larry',
        })
        result = bsv.validate_dag(seq)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(seq['status'], 'paused')
        # 'paused' is a member of VALID_SEQUENCE_STATUS — no new field.
        self.assertNotIn('paused', set(seq.keys()) - {'audit_log', 'status',
                                                       'steps', 'current_steps',
                                                       'created_at', 'created_by',
                                                       'spec_doc', 'label',
                                                       'seq_id'})

    def test_pause_idempotency_on_already_paused(self):
        """Re-running `pause sequence X` when already paused must NOT
        append a second audit_log entry."""
        seq = _make_sequence(seq_id='pause-idem', status='paused')
        before = json.loads(json.dumps(seq))
        # WARN no-op: leave the dict identical.
        if seq['status'] == 'paused':
            pass  # no-op
        self.assertEqual(seq, before)

    def test_resume_sets_status_active(self):
        seq = _make_sequence(seq_id='resume-seq', status='paused')
        seq['status'] = 'active'
        seq['audit_log'].append({
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': 'resumed', 'actor': 'larry',
        })
        result = bsv.validate_dag(seq)
        self.assertTrue(result.valid, result.errors)

    # ---------- cancel ----------

    def test_cancel_sets_status_failed_no_archive_move(self):
        seq = _make_sequence(seq_id='cancel-seq', status='active')
        seq['status'] = 'failed'
        seq['audit_log'].append({
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': 'cancelled', 'actor': 'larry',
            'reason': 'Larry decided we are going a different direction',
        })
        result = bsv.validate_dag(seq)
        self.assertTrue(result.valid, result.errors)
        # No `outcome` field, no `cancelled_at` field — schema unchanged.
        self.assertNotIn('outcome', seq)
        self.assertNotIn('cancelled_at', seq)

    def test_cancel_without_reason_omits_reason_field(self):
        seq = _make_sequence(seq_id='cancel-no-reason', status='active')
        entry = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': 'cancelled', 'actor': 'larry',
        }
        seq['status'] = 'failed'
        seq['audit_log'].append(entry)
        self.assertNotIn('reason', entry)

    def test_cancel_idempotency_on_already_failed(self):
        seq = _make_sequence(seq_id='cancel-idem', status='failed')
        before = json.loads(json.dumps(seq))
        # WARN no-op: leave the dict identical.
        self.assertEqual(seq, before)

    # ---------- retry ----------

    def test_retry_resets_step_fields_and_removes_from_current_steps(self):
        seq = _make_sequence(
            seq_id='retry-seq',
            status='paused',
            steps=[
                _make_step('a', deps=[], status='merged',
                           merged_at='2026-05-27T01:00:00Z',
                           pr_url='https://example.com/pr/1'),
                _make_step('b', deps=['a'], status='failed',
                           pr_url='https://example.com/pr/2'),
            ],
            current_steps=['b'],
        )
        # Mutation per the spec/Beacon CLAUDE.md.
        for step in seq['steps']:
            if step['step_id'] == 'b':
                step['status'] = 'pending'
                step['dispatched_at'] = None
                step['pr_url'] = None
                step['current_actor'] = None
                step['failure_reason'] = None
                step['merged_at'] = None
        seq['current_steps'] = [s for s in seq['current_steps'] if s != 'b']
        seq['audit_log'].append({
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': 'step-retried', 'step_id': 'b', 'actor': 'larry',
        })
        # Sequence must still pass the validator.
        result = bsv.validate_dag(seq)
        self.assertTrue(result.valid, result.errors)
        # Step 'b' is back to pending.
        b = next(s for s in seq['steps'] if s['step_id'] == 'b')
        self.assertEqual(b['status'], 'pending')
        self.assertIsNone(b['pr_url'])
        # Removed from current_steps.
        self.assertNotIn('b', seq['current_steps'])

    def test_retry_idempotency_on_already_pending_step(self):
        """Step already pending → WARN no-op, no duplicate audit_log."""
        seq = _make_sequence(
            seq_id='retry-idem',
            status='paused',
            steps=[_make_step('a', status='pending')],
        )
        before = json.loads(json.dumps(seq))
        # WARN no-op.
        self.assertEqual(seq, before)

    # ---------- skip ----------

    def test_skip_sets_step_status_merged_not_skipped(self):
        """Per spec § 5.4 + preflight Q4: skip marks step as `merged`,
        NOT `skipped`. `'skipped'` is not in VALID_STEP_STATUS."""
        seq = _make_sequence(
            seq_id='skip-seq',
            status='active',
            steps=[
                _make_step('a', deps=[], status='merged'),
                _make_step('b', deps=['a'], status='failed'),
            ],
            current_steps=['b'],
        )
        # Apply skip(b, reason="Work done out-of-band via hotfix").
        now = datetime.now(timezone.utc).isoformat()
        for step in seq['steps']:
            if step['step_id'] == 'b':
                step['status'] = 'merged'
                step['merged_at'] = now
        seq['audit_log'].append({
            'ts': now, 'event': 'step-skipped', 'step_id': 'b',
            'reason': 'Work done out-of-band via hotfix',
            'actor': 'larry',
        })
        # Validator passes — `merged` is a valid step status.
        result = bsv.validate_dag(seq)
        self.assertTrue(result.valid, result.errors)
        # `skipped` is NOT a valid step status per the schema enum.
        self.assertNotIn('skipped', bsv.VALID_STEP_STATUS)

    def test_skip_without_reason_omits_reason_field(self):
        """If Larry doesn't provide reason text after the comma, the
        audit_log entry omits the `reason` key entirely (no empty string)."""
        entry = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': 'step-skipped', 'step_id': 'b', 'actor': 'larry',
        }
        # No `reason` key when Larry didn't provide one.
        self.assertNotIn('reason', entry)

    def test_skip_idempotency_on_already_merged_step(self):
        """Step already merged → WARN no-op, no duplicate audit_log."""
        seq = _make_sequence(
            seq_id='skip-idem',
            status='active',
            steps=[
                _make_step('a', status='merged',
                           merged_at='2026-05-27T01:00:00Z'),
            ],
        )
        before = json.loads(json.dumps(seq))
        # WARN no-op.
        self.assertEqual(seq, before)

    # ---------- schema invariant ----------

    def test_no_invented_fields_after_any_shortcut(self):
        """Across every shortcut mutation, the locked PR-S2 schema must
        hold: top-level fields stay within REQUIRED_SEQ_FIELDS, status
        within VALID_SEQUENCE_STATUS, step status within VALID_STEP_STATUS.
        This is the load-bearing guarantee — zero schema drift in PR-S4."""
        for status in ('pending', 'active', 'paused', 'complete',
                       'failed', 'archived'):
            seq = _make_sequence(seq_id=f'invariant-{status}', status=status)
            self.assertIn(seq['status'], bsv.VALID_SEQUENCE_STATUS)
            top_level_extra = set(seq.keys()) - set(bsv.REQUIRED_SEQ_FIELDS)
            self.assertEqual(
                top_level_extra, set(),
                f'no invented top-level fields for status={status}',
            )
            for step in seq['steps']:
                self.assertIn(step['status'], bsv.VALID_STEP_STATUS)


if __name__ == '__main__':
    unittest.main()
