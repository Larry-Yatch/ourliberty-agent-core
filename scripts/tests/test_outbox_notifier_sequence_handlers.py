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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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

# Resolve the validator module the SAME way the kickoff handler does at
# call time, so patching `check_spec_doc_presence` on it is actually seen
# by the handler (the handler may bind `scripts.build_sequence_validator`
# or top-level `build_sequence_validator` depending on sys.path).
try:
    from scripts import build_sequence_validator as bsv_handler_mod  # noqa: E402
except ImportError:
    bsv_handler_mod = bsv


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

    def test_spec_doc_behind_origin_defers_kickoff_with_sync_message(self):
        """Incident 2026-06-10: spec_doc merged to origin/main but missing
        from this checkout. The kickoff MUST NOT transition pending→active
        and MUST report 'run sync' — NOT 'spec never authored'. The
        spec-doc classifier is patched to simulate the behind-origin state
        (it can't be reproduced live: origin/main == the working copy)."""
        seq = _make_sequence(seq_id='pulse-upgrade-001', status='pending')
        self._write_sequence(seq)
        fake = bsv.SpecDocPresence(
            status=bsv.SPEC_DOC_BEHIND_ORIGIN,
            spec_doc='agents/beacon/specs/missions-v2-phase2.md',
            message=('working copy is behind origin/main by 1 commit(s); run '
                     'sync (`systemctl start ourliberty-sync.service`); do not '
                     're-author it.'),
            behind_by=1,
        )
        body = self._make_envelope()
        with mock.patch.object(
            bsv_handler_mod, 'check_spec_doc_presence', return_value=fake,
        ):
            result = on._handle_build_sequence_advancer_kickoff(
                body, body['result'],
            )
        self.assertIsNotNone(result)
        self.assertIn('spec-behind-origin', result)
        # Sequence stays pending — the spec isn't readable here yet.
        self.assertEqual(self._read_sequence('pulse-upgrade-001')['status'],
                         'pending')
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        msg = alerts[0]['message'].lower()
        self.assertIn('ourliberty-sync.service', msg)
        self.assertIn('do not re-author', msg)
        self.assertNotIn('never authored', msg)

    def test_behind_origin_on_active_sequence_still_dedups(self):
        """Regression: the spec_doc guard must sit AFTER the idempotency
        no-op. A re-dispatched kickoff on an already-active sequence whose
        spec is behind-origin must dedup silently (`already-active`), NOT
        trip the spec guard (`spec-behind-origin`)."""
        seq = _make_sequence(seq_id='pulse-upgrade-001', status='active')
        self._write_sequence(seq)
        fake = bsv.SpecDocPresence(
            status=bsv.SPEC_DOC_BEHIND_ORIGIN,
            spec_doc='agents/beacon/specs/x.md',
            message='behind origin; run sync',
            behind_by=1,
        )
        body = self._make_envelope()
        with mock.patch.object(
            bsv_handler_mod, 'check_spec_doc_presence', return_value=fake,
        ) as patched:
            result = on._handle_build_sequence_advancer_kickoff(
                body, body['result'],
            )
        self.assertIn('already-active', result)
        self.assertNotIn('spec-behind-origin', result)
        # The guard never ran — dedup short-circuited before it.
        patched.assert_not_called()
        # Dedup audit entry appended; status unchanged.
        on_disk = self._read_sequence('pulse-upgrade-001')
        self.assertEqual(on_disk['status'], 'active')
        self.assertEqual(on_disk['audit_log'][-1]['event'],
                         'kickoff-duplicate-suppressed')

    def test_spec_doc_not_authored_fails_kickoff_with_genuine_message(self):
        """The genuine missing-spec case (absent locally AND on origin/main)
        still reports the real 'author + merge it first' error. The spec-doc
        classifier is patched to simulate the not-authored state — the same
        hermetic pattern as the sibling behind-origin test above. It can't be
        reproduced live: when origin/main is transiently unresolvable on the
        droplet the real classifier returns SPEC_DOC_INDETERMINATE (not
        SPEC_DOC_NOT_AUTHORED), which flipped these assertions non-
        deterministically under the full-suite regression gate."""
        seq = _make_sequence(seq_id='pulse-upgrade-001', status='pending')
        seq['spec_doc'] = 'agents/beacon/specs/__never_authored_spec__.md'
        self._write_sequence(seq)
        fake = bsv.SpecDocPresence(
            status=bsv.SPEC_DOC_NOT_AUTHORED,
            spec_doc='agents/beacon/specs/__never_authored_spec__.md',
            message=('spec_doc is absent locally and on origin/main; author '
                     'it and merge to origin/main first, then re-dispatch the '
                     'kickoff.'),
        )
        body = self._make_envelope()
        with mock.patch.object(
            bsv_handler_mod, 'check_spec_doc_presence', return_value=fake,
        ):
            result = on._handle_build_sequence_advancer_kickoff(
                body, body['result'],
            )
        self.assertIsNotNone(result)
        self.assertIn('spec-not-authored', result)
        self.assertEqual(self._read_sequence('pulse-upgrade-001')['status'],
                         'pending')
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('author', alerts[0]['message'].lower())

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
        # Contract A (p4-complete-signal): the last-step merge now flips the
        # sequence to `complete`, which runs `_maybe_signal_sequence_complete`
        # and its belt-and-suspenders gh veto. Stub `_gh_pr_state` → 'MERGED'
        # so the single-step `test_idempotent_when_step_already_merged` (and
        # any future last-step case) never shells out to real `gh`.
        self._gh_state_patcher = mock.patch.object(
            on, '_gh_pr_state', return_value='MERGED',
        )
        self._gh_state_patcher.start()
        self.addCleanup(self._gh_state_patcher.stop)

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


# ============================================================================
# Regression: V6 hook fires from EVERY auto-merge chokepoint caller
# ============================================================================
#
# 2026-05-29 silent-miss on `operator-ux-rollout` step `step-rescue-runbook`
# (dashboard PR #21): `_auto_merge_pr` logged `outcome=merged` but no
# `SEQUENCE_STEP_MERGED` line followed. Root cause: V6 was wired only
# at the marker-routing call site in `process_outbox`, so PRs that
# merged via `_queue_release` (blocker resolves and re-attempts queued
# entries) or `_auto_merge_queue_sweep` (UNKNOWN-retry) silently
# skipped the sequence-state propagation. Fix: V6 hook lives inside
# `_attempt_auto_merge_with_gates` — the single chokepoint all three
# callers share. These tests lock that contract in.


class AutoMergeChokepointFiresV6Hook(SignalSequenceStepMergedHarness):
    """V6 must fire from `_attempt_auto_merge_with_gates` regardless of
    which caller drove it."""

    def setUp(self):
        super().setUp()
        # Reroute the auto-merge queue file so _queue_push / _queue_release
        # write to tmpdir, not the droplet's real state file.
        self._orig_queue_file = on.AUTO_MERGE_QUEUE_FILE
        on.AUTO_MERGE_QUEUE_FILE = (
            self._root / 'state' / 'auto-merge-queue.json'
        )
        (self._root / 'state').mkdir(parents=True, exist_ok=True)

        # Install the test-bypass: skip serializer gates, swap merge_fn
        # for a stub returning a clean 'merged' outcome. The bypass is
        # also a chokepoint caller (see _attempt_auto_merge_with_gates'
        # early-return branch), so V6 must fire from it too.
        self._orig_skip = on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST
        self._orig_override = on._AUTO_MERGE_FN_OVERRIDE
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = True
        on._AUTO_MERGE_FN_OVERRIDE = self._make_merge_stub('merged')

    def tearDown(self):
        on._AUTO_MERGE_QUEUE_FILE = self._orig_queue_file
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = self._orig_skip
        on._AUTO_MERGE_FN_OVERRIDE = self._orig_override
        super().tearDown()

    def _make_merge_stub(self, outcome):
        """Return a stand-in for `_auto_merge_pr` that records calls
        and returns a fixed outcome."""
        self._merge_calls = []

        def _stub(pr_url, task_id):
            self._merge_calls.append((pr_url, task_id))
            return {
                'merge_outcome': outcome,
                'merge_reason': f'stub: {outcome}',
                'pr_number': 21,
                'repo_coords': 'Larry-Yatch/ourliberty-dashboard',
            }
        return _stub

    def _push_queue_entry(self, pr_number, task_id, blocker_pr_number,
                          repo='Larry-Yatch/ourliberty-dashboard'):
        """Seed an auto-merge queue entry shaped like the real serializer
        writes (see `_attempt_auto_merge_with_gates` line ~4298)."""
        on._queue_push({
            'pr_number': pr_number,
            'task_id': task_id,
            'repo': repo,
            'pr_url': f'https://github.com/{repo}/pull/{pr_number}',
            'changed_files': ['some/file.py'],
            'queued_at': '2026-05-29T06:24:00Z',
            'blocker_pr_number': blocker_pr_number,
            'watchdog_dm_sent': False,
            'unknown_attempts': 0,
            'reply_chat_id': None,
            'summary': 'queued behind blocker',
        })

    # --- chokepoint-direct (marker-routing analogue) ---

    def test_direct_attempt_fires_v6_on_merged(self):
        # Equivalent to the marker-routing call site: a Mirror PASS that
        # reaches the gate, passes, and merges cleanly. V6 must fire.
        seq = self._make_active_sequence(
            seq_id='operator-ux-rollout',
            dispatched_step='step-rescue-runbook',
        )
        self._write_sequence(seq)

        result = on._attempt_auto_merge_with_gates(
            pr_url='https://github.com/Larry-Yatch/ourliberty-dashboard/pull/21',
            repo_coords='Larry-Yatch/ourliberty-dashboard',
            pr_number=21,
            task_id='step-rescue-runbook',
            summary='step body',
            chat_id=None,
            changed_files=['app/foo.py'],
        )
        self.assertEqual(result['merge_outcome'], 'merged')
        on_disk = self._read_sequence('operator-ux-rollout')
        step = next(
            s for s in on_disk['steps']
            if s['step_id'] == 'step-rescue-runbook'
        )
        self.assertEqual(step['status'], 'merged')
        self.assertEqual(
            step['pr_url'],
            'https://github.com/Larry-Yatch/ourliberty-dashboard/pull/21',
        )
        self.assertNotIn(
            'step-rescue-runbook', on_disk['current_steps'],
        )

    # --- _queue_release (the 2026-05-29 silent-miss path) ---

    def test_queue_release_fires_v6_on_merged(self):
        # The exact regression: step-rescue-runbook was queued behind
        # blocker PR #184; when #184 merged, _queue_release re-attempted
        # PR #21 via _attempt_auto_merge_with_gates -> merged -> the V6
        # hook MUST fire so the sequence file flips to 'merged' instead
        # of staying 'dispatched' for hours.
        seq = self._make_active_sequence(
            seq_id='operator-ux-rollout',
            dispatched_step='step-rescue-runbook',
        )
        self._write_sequence(seq)
        self._push_queue_entry(
            pr_number=21,
            task_id='step-rescue-runbook',
            blocker_pr_number=184,
        )

        on._queue_release(merged_pr_number=184,
                          repo_coords='Larry-Yatch/ourliberty-dashboard')

        # Merge stub was invoked exactly once for the released entry.
        self.assertEqual(len(self._merge_calls), 1)
        self.assertEqual(self._merge_calls[0][1], 'step-rescue-runbook')
        # The queue is now drained.
        self.assertEqual(on._load_auto_merge_queue(), [])
        # V6 hook fired — sequence file's step is now merged.
        on_disk = self._read_sequence('operator-ux-rollout')
        step = next(
            s for s in on_disk['steps']
            if s['step_id'] == 'step-rescue-runbook'
        )
        self.assertEqual(step['status'], 'merged')
        self.assertNotIn(
            'step-rescue-runbook', on_disk['current_steps'],
        )
        # An audit-log entry was appended by apply_step_merged.
        events = [e['event'] for e in on_disk['audit_log']]
        self.assertIn('step-merged', events)

    # --- _auto_merge_queue_sweep UNKNOWN-retry ---

    def test_queue_sweep_unknown_retry_fires_v6_on_merged(self):
        # Gate 2 deferred this PR one tick (UNKNOWN). The sweep retries
        # with second_attempt_on_unknown=True. The retry merges. V6 must
        # fire even on this path.
        seq = self._make_active_sequence(
            seq_id='operator-ux-rollout',
            dispatched_step='step-rescue-runbook',
        )
        self._write_sequence(seq)
        # UNKNOWN-deferred entries have blocker_pr_number=None and
        # unknown_attempts>=1 (see _attempt_auto_merge_with_gates ~4347).
        on._queue_push({
            'pr_number': 21,
            'task_id': 'step-rescue-runbook',
            'repo': 'Larry-Yatch/ourliberty-dashboard',
            'pr_url': (
                'https://github.com/Larry-Yatch/ourliberty-dashboard/pull/21'
            ),
            'changed_files': ['app/foo.py'],
            'queued_at': '2026-05-29T06:24:00Z',
            'blocker_pr_number': None,
            'watchdog_dm_sent': False,
            'unknown_attempts': 1,
            'reply_chat_id': None,
            'summary': 'deferred',
        })

        on._auto_merge_queue_sweep()

        on_disk = self._read_sequence('operator-ux-rollout')
        step = next(
            s for s in on_disk['steps']
            if s['step_id'] == 'step-rescue-runbook'
        )
        self.assertEqual(step['status'], 'merged')

    # --- idempotency: chokepoint fire once per merge, not double ---

    def test_chokepoint_fires_v6_exactly_once_per_merge(self):
        # The marker-routing block used to call V6 directly; the
        # chokepoint refactor removed that duplicate. Verify there's no
        # double-fire — apply_step_merged is invoked exactly once per
        # successful merge (audit_log gains exactly one 'step-merged'
        # entry).
        seq = self._make_active_sequence(
            seq_id='operator-ux-rollout',
            dispatched_step='step-rescue-runbook',
        )
        self._write_sequence(seq)

        on._attempt_auto_merge_with_gates(
            pr_url=(
                'https://github.com/Larry-Yatch/ourliberty-dashboard/pull/21'
            ),
            repo_coords='Larry-Yatch/ourliberty-dashboard',
            pr_number=21,
            task_id='step-rescue-runbook',
            summary='',
            chat_id=None,
            changed_files=['app/foo.py'],
        )

        on_disk = self._read_sequence('operator-ux-rollout')
        step_merged_events = [
            e for e in on_disk['audit_log']
            if e['event'] == 'step-merged'
        ]
        self.assertEqual(len(step_merged_events), 1)


class QualifyRepoTests(unittest.TestCase):
    """Bare repo name → OWNER/REPO coords for gh; qualified names pass through.
    Shared helper lives in sequence_shortcut_helpers (used by both seams)."""

    def test_bare_name_prefixes_owner(self):
        self.assertEqual(
            ssh.qualify_repo('ourliberty-agent-core'),
            'Larry-Yatch/ourliberty-agent-core',
        )

    def test_already_qualified_passes_through(self):
        self.assertEqual(
            ssh.qualify_repo('someone/other-repo'), 'someone/other-repo',
        )


class MaybeReconcileAlreadyMergedBuildTests(SignalSequenceStepMergedHarness):
    """fix 1b — a no-PR build whose work already merged flips its step terminal
    instead of stranding. `_make_active_sequence` gives step-a (dispatched) +
    step-b (pending), so reconciling step-a is NOT a last-step merge — no
    completion-signal machinery runs."""

    NO_DELTA_RESULT = (
        '**Already built and merged.** PR #602 merged to `main`; '
        '`git diff main..HEAD` is empty — no delta. Already merged via #602.'
    )
    PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/602'

    def _build_data(self, **overrides):
        data = {
            'agent': 'forge', 'phase': 'build', 'task_id': 'step-a',
            'target_repo': 'ourliberty-agent-core', 'exit_code': 0,
            'result': self.NO_DELTA_RESULT,
        }
        data.update(overrides)
        return data

    def test_already_merged_flips_step_to_merged(self):
        self._write_sequence(self._make_active_sequence())
        with mock.patch.object(
            ssh, 'gh_pr_merge_info',
            return_value=(self.PR_URL, '2026-06-20T01:10:37Z'),
        ):
            result = on._maybe_reconcile_already_merged_build(self._build_data())
        self.assertEqual(result, 'live-seq-001')
        on_disk = self._read_sequence('live-seq-001')
        step_a = next(s for s in on_disk['steps'] if s['step_id'] == 'step-a')
        self.assertEqual(step_a['status'], 'merged')
        self.assertEqual(step_a['pr_url'], self.PR_URL)
        self.assertNotIn('step-a', on_disk['current_steps'])

    def test_no_cue_does_not_reconcile(self):
        self._write_sequence(self._make_active_sequence())
        with mock.patch.object(ssh, 'gh_pr_merge_info') as gh:
            result = on._maybe_reconcile_already_merged_build(
                self._build_data(result='Opened nothing yet; still working on #602.'),
            )
        self.assertIsNone(result)
        gh.assert_not_called()  # no cue → never even shells out
        on_disk = self._read_sequence('live-seq-001')
        step_a = next(s for s in on_disk['steps'] if s['step_id'] == 'step-a')
        self.assertEqual(step_a['status'], 'dispatched')

    def test_gh_not_merged_does_not_reconcile(self):
        self._write_sequence(self._make_active_sequence())
        with mock.patch.object(ssh, 'gh_pr_merge_info', return_value=None):
            result = on._maybe_reconcile_already_merged_build(self._build_data())
        self.assertIsNone(result)
        on_disk = self._read_sequence('live-seq-001')
        step_a = next(s for s in on_disk['steps'] if s['step_id'] == 'step-a')
        self.assertEqual(step_a['status'], 'dispatched')

    def test_ambiguous_pr_refs_do_not_reconcile(self):
        self._write_sequence(self._make_active_sequence())
        with mock.patch.object(ssh, 'gh_pr_merge_info') as gh:
            result = on._maybe_reconcile_already_merged_build(self._build_data(
                result='Already merged via #602, superseding #595. No delta.',
            ))
        self.assertIsNone(result)
        gh.assert_not_called()

    def test_missing_target_repo_returns_none(self):
        self._write_sequence(self._make_active_sequence())
        result = on._maybe_reconcile_already_merged_build(
            self._build_data(target_repo=None),
        )
        self.assertIsNone(result)

    def test_non_clean_exit_does_not_reconcile(self):
        # A build that exited non-zero is a genuine failure, not an honest
        # no-delta refusal — never gh-verify or flip the step.
        self._write_sequence(self._make_active_sequence())
        with mock.patch.object(ssh, 'gh_pr_merge_info') as gh:
            result = on._maybe_reconcile_already_merged_build(
                self._build_data(exit_code=1),
            )
        self.assertIsNone(result)
        gh.assert_not_called()
        on_disk = self._read_sequence('live-seq-001')
        step_a = next(s for s in on_disk['steps'] if s['step_id'] == 'step-a')
        self.assertEqual(step_a['status'], 'dispatched')


# ============================================================================
# push-signal-and-substatus — _signal_sequence_step_failed
# ============================================================================


class SignalSequenceStepFailedTests(SignalSequenceStepMergedHarness):
    """Push-signal a non-merge terminal: step → failed + sequence paused +
    ONE Larry doorbell alert. Reuses the merged harness (ssh.AGENTS_ROOT +
    larry_alerts rerouted to the tmpdir)."""

    def test_active_step_fails_pauses_and_alerts(self):
        self._write_sequence(self._make_active_sequence())
        result = on._signal_sequence_step_failed(
            'step-a', 'Forge preflight REJECT (marker_type=reject)',
        )
        self.assertEqual(result, 'live-seq-001')
        on_disk = self._read_sequence('live-seq-001')
        step_a = next(s for s in on_disk['steps'] if s['step_id'] == 'step-a')
        self.assertEqual(step_a['status'], 'failed')
        self.assertIn('REJECT', step_a['failure_reason'])
        self.assertEqual(on_disk['status'], 'paused')
        # Failed step stays visible in current_steps.
        self.assertIn('step-a', on_disk['current_steps'])
        # Exactly one Larry doorbell alert about the pause.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'], 'sequence-paused:live-seq-001')

    def test_no_matching_step_returns_none_and_no_alert(self):
        self._write_sequence(self._make_active_sequence())
        result = on._signal_sequence_step_failed('unrelated', 'reason')
        self.assertIsNone(result)
        on_disk = self._read_sequence('live-seq-001')
        step_a = next(s for s in on_disk['steps'] if s['step_id'] == 'step-a')
        self.assertEqual(step_a['status'], 'dispatched')
        self.assertEqual(on_disk['status'], 'active')
        self.assertEqual(self._read_alerts(), [])

    def test_refire_on_paused_sequence_is_dropped_no_double_alert(self):
        # After the first failure the sequence is `paused`, which the active-
        # sequence scan skips (same convention as _signal_sequence_step_merged).
        # A re-fire of the same terminal therefore returns None and raises NO
        # second alert — the pause is the natural idempotency boundary.
        seq = _make_sequence(
            seq_id='already-failed', status='paused',
            steps=[_make_step('step-a', status='failed')],
            current_steps=['step-a'],
        )
        seq['steps'][0]['failure_reason'] = 'first reason'
        self._write_sequence(seq)
        result = on._signal_sequence_step_failed('step-a', 'second reason')
        self.assertIsNone(result)
        on_disk = self._read_sequence('already-failed')
        self.assertEqual(on_disk['steps'][0]['failure_reason'], 'first reason')
        self.assertEqual(self._read_alerts(), [])

    def test_merged_step_not_clobbered_by_late_failure(self):
        seq = _make_sequence(
            seq_id='merged-seq', status='active',
            steps=[_make_step('step-a', status='merged',
                              merged_at='2026-06-24T00:00:00Z',
                              pr_url='https://github.com/x/y/pull/1')],
        )
        self._write_sequence(seq)
        result = on._signal_sequence_step_failed('step-a', 'late failure')
        self.assertEqual(result, 'merged-seq')
        on_disk = self._read_sequence('merged-seq')
        self.assertEqual(on_disk['steps'][0]['status'], 'merged')
        self.assertEqual(on_disk['status'], 'active')
        self.assertEqual(self._read_alerts(), [])


# ============================================================================
# push-signal-and-substatus — _signal_sequence_step_pr_opened
# ============================================================================


class SignalSequenceStepPrOpenedTests(SignalSequenceStepMergedHarness):
    """Record pr_url + flip the step to `reviewing` at PR-open. No Larry
    alert — in-flight progress is not an operator event."""

    def test_open_records_pr_url_and_flips_reviewing(self):
        self._write_sequence(self._make_active_sequence())
        result = on._signal_sequence_step_pr_opened(
            'step-a', 'https://github.com/x/y/pull/42',
        )
        self.assertEqual(result, 'live-seq-001')
        on_disk = self._read_sequence('live-seq-001')
        step_a = next(s for s in on_disk['steps'] if s['step_id'] == 'step-a')
        self.assertEqual(step_a['status'], 'reviewing')
        self.assertEqual(step_a['pr_url'], 'https://github.com/x/y/pull/42')
        self.assertEqual(step_a['current_actor'], 'mirror')
        # Sequence stays active; no operator alert.
        self.assertEqual(on_disk['status'], 'active')
        self.assertEqual(self._read_alerts(), [])

    def test_no_matching_step_returns_none(self):
        self._write_sequence(self._make_active_sequence())
        result = on._signal_sequence_step_pr_opened(
            'unrelated', 'https://github.com/x/y/pull/1',
        )
        self.assertIsNone(result)
        on_disk = self._read_sequence('live-seq-001')
        step_a = next(s for s in on_disk['steps'] if s['step_id'] == 'step-a')
        self.assertEqual(step_a['status'], 'dispatched')

    def test_terminal_step_not_walked_backward(self):
        seq = _make_sequence(
            seq_id='term-seq', status='active',
            steps=[_make_step('step-a', status='merged',
                              merged_at='2026-06-24T00:00:00Z',
                              pr_url='https://github.com/x/y/pull/1')],
        )
        self._write_sequence(seq)
        result = on._signal_sequence_step_pr_opened(
            'step-a', 'https://github.com/x/y/pull/99',
        )
        self.assertEqual(result, 'term-seq')
        on_disk = self._read_sequence('term-seq')
        self.assertEqual(on_disk['steps'][0]['status'], 'merged')
        self.assertEqual(on_disk['steps'][0]['pr_url'],
                         'https://github.com/x/y/pull/1')


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # This module drives a Layer B-guarded chokepoint (larry_alerts / inbox /
    # gh-write / claude-spawn / concurrency) against already-isolated state, so
    # the guard would breach before the test's own mocks. Opt out for the module
    # so the guard is a pass-through; the #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    unittest.main()
