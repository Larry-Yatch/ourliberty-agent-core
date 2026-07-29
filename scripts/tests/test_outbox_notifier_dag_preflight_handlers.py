#!/usr/bin/env python3
"""Regression coverage for PR-S4 rectification HIGH fixes.

Covers:
  - H1: `_handle_mirror_dag_preflight_result` — PASS triggers pending →
    active kickoff path, REVISION self-heals by routing a
    `dag-preflight-revision` notify to Beacon (no Larry DM; records a
    `dag-preflight-revision-routed` audit entry), malformed fires
    `mirror-dag-malformed-result` alert.
  - H3: source=`orchestrator` envelopes reach the kickoff +
    headless-approval handlers (the advancer dispatches Beacon with
    source='orchestrator'; before H3 the handlers source-locked to
    `larry` and silently archived).
  - M4: defensive gate — Mirror DAG-preflight session with a STRAY
    REVIEW_PASS marker in the result text does NOT route through the
    regular `_classify_mirror_marker` auto-merge path.
  - L5: malformed kickoff prompt (no parseable seq_id) fires a
    `kickoff-malformed-prompt` alert.

Test isolation discipline (PR #137): every test reroutes
`outbox_notifier.AGENTS_ROOT` / `safe_write_inbox.INBOXES_ROOT` /
`larry_alerts.ALERTS_FILE` to a per-test tmpdir. NO writes ever reach
`~/agents/` (real droplet state). The AST-walk leak gate
(`test_no_production_path_leaks.py`) enforces this.

Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_outbox_notifier_dag_preflight_handlers
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
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import build_sequence_validator as bsv  # noqa: E402
import larry_alerts as la  # noqa: E402
import outbox_notifier as on  # noqa: E402
import safe_write_inbox as swi  # noqa: E402


def _make_step(step_id, deps=None, status='pending'):
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
        'dispatched_at': None,
        'merged_at': None,
        'pr_url': None,
        'current_actor': None,
        'failure_reason': None,
    }


def _make_sequence(seq_id='orchestrator-bootstrap-002', status='pending'):
    return {
        'seq_id': seq_id,
        'label': f'Sequence {seq_id}',
        'spec_doc': 'agents/beacon/specs/build-sequence-orchestrator.md',
        'created_at': '2026-05-27T00:00:00+00:00',
        'created_by': 'beacon',
        'status': status,
        'current_steps': [],
        'steps': [
            _make_step('s1', deps=[]),
            _make_step('s2', deps=['s1']),
        ],
        'audit_log': [
            {'ts': '2026-05-27T00:00:00+00:00', 'event': 'sequence-created',
             'actor': 'beacon'},
        ],
    }


class _DagHandlerHarness(unittest.TestCase):
    """Reroute outbox_notifier + safe_write_inbox + larry_alerts to a
    per-test tmpdir."""

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

    def _beacon_inbox_files(self):
        inbox = swi.INBOXES_ROOT / 'beacon'
        if not inbox.is_dir():
            return []
        return sorted(p for p in inbox.glob('*.json'))

    def _read_beacon_notifies(self):
        return [json.loads(p.read_text()) for p in self._beacon_inbox_files()]


# ============================================================================
# H1: Mirror DAG preflight result handler
# ============================================================================


class MirrorDagPreflightPass(_DagHandlerHarness):

    def test_pass_transitions_sequence_pending_to_active(self):
        seq = _make_sequence(seq_id='dag-pass-seq', status='pending')
        self._write_sequence(seq)
        envelope = {
            'agent': 'mirror',
            'source': 'beacon',
            'task_id': 'review-sequence-dag-dag-pass-seq',
            'prompt': 'review-sequence-dag dag-pass-seq',
            'result': (
                'DAG preflight checks complete.\n'
                '- ✅ No cycles\n'
                '- ✅ All depends_on resolve\n'
                '- ✅ No parallel-file overlap\n'
                '- ✅ All spec sections exist\n\n'
                'result: PASS\n\n'
                'DAG preflight PASS for sequence `dag-pass-seq`.'
            ),
        }
        result = on._handle_mirror_dag_preflight_result(envelope)
        self.assertIsNotNone(result)
        on_disk = self._read_sequence('dag-pass-seq')
        self.assertEqual(on_disk['status'], 'active')
        last = on_disk['audit_log'][-1]
        self.assertEqual(last['event'], 'dag-preflight-pass-kickoff')
        self.assertEqual(last['actor'], 'outbox-notifier')
        # PASS alert DMed Larry.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('PASS', alerts[0].get('message', ''))

    def test_pass_idempotent_on_already_active(self):
        seq = _make_sequence(seq_id='dag-active', status='active')
        self._write_sequence(seq)
        envelope = {
            'agent': 'mirror',
            'source': 'beacon',
            'task_id': 'review-sequence-dag-dag-active',
            'prompt': 'review-sequence-dag dag-active',
            'result': 'result: PASS',
        }
        result = on._handle_mirror_dag_preflight_result(envelope)
        # Handler claims the outbox (returns a sentinel), but no audit
        # entry appended on already-active sequence.
        self.assertIsNotNone(result)
        on_disk = self._read_sequence('dag-active')
        kickoff_entries = [
            e for e in on_disk['audit_log']
            if e.get('event') == 'dag-preflight-pass-kickoff'
        ]
        self.assertEqual(len(kickoff_entries), 0)


class MirrorDagPreflightRevision(_DagHandlerHarness):

    def _revision_envelope(self, seq_id='dag-rev-seq'):
        return {
            'agent': 'mirror',
            'source': 'beacon',
            'task_id': f'review-sequence-dag-{seq_id}',
            'prompt': f'review-sequence-dag {seq_id}',
            'result': (
                'DAG preflight found issues:\n'
                '- Check 3: steps `s1` and `s2` both write `foo.py` with no '
                'depends_on edge — serialize s2 behind s1.\n\n'
                'result: REVISION\n\n'
                f'DAG preflight REVISION for sequence `{seq_id}`.'
            ),
        }

    def test_revision_routes_beacon_notify_no_larry_dm(self):
        seq = _make_sequence(seq_id='dag-rev-seq', status='pending')
        self._write_sequence(seq)
        result = on._handle_mirror_dag_preflight_result(
            self._revision_envelope('dag-rev-seq'),
        )
        self.assertEqual(result, 'mirror-dag-preflight:revision:dag-rev-seq')

        # Status unchanged — REVISION does NOT activate the sequence.
        on_disk = self._read_sequence('dag-rev-seq')
        self.assertEqual(on_disk['status'], 'pending')

        # Self-heal: a dag-preflight-revision notify landed in Beacon's inbox.
        notifies = self._read_beacon_notifies()
        self.assertEqual(len(notifies), 1)
        notify = notifies[0]
        self.assertEqual(notify['intent'], 'dag-preflight-revision')
        self.assertEqual(notify['seq_id'], 'dag-rev-seq')
        self.assertEqual(notify['source'], 'mirror-result')
        self.assertIn('dag-rev-seq', notify['seq_path'])
        # Mirror's verdict body rides the notify prompt.
        self.assertIn('Check 3', notify['prompt'])
        # Deterministic filename keyed on seq_id.
        self.assertEqual(
            self._beacon_inbox_files()[0].name,
            'notify-dag-revision-dag-rev-seq.json',
        )

        # Actionable-only: NO warning-severity Larry DM on the happy path.
        self.assertEqual(self._read_alerts(), [])

        # Durable stall signal recorded for the heal backstop.
        rev_entries = [
            e for e in on_disk['audit_log']
            if e.get('event') == 'dag-preflight-revision-routed'
        ]
        self.assertEqual(len(rev_entries), 1)
        self.assertEqual(rev_entries[0]['actor'], 'outbox-notifier')
        self.assertEqual(
            rev_entries[0]['mirror_task_id'],
            'review-sequence-dag-dag-rev-seq',
        )

    def test_revision_reprocess_is_idempotent(self):
        seq = _make_sequence(seq_id='dag-rev-seq', status='pending')
        self._write_sequence(seq)
        env = self._revision_envelope('dag-rev-seq')
        on._handle_mirror_dag_preflight_result(env)
        on._handle_mirror_dag_preflight_result(env)  # replay same outbox

        # Deterministic filename → single notify (overwrite, not duplicate).
        self.assertEqual(len(self._beacon_inbox_files()), 1)
        # Idempotent on mirror_task_id → single audit entry.
        on_disk = self._read_sequence('dag-rev-seq')
        rev_entries = [
            e for e in on_disk['audit_log']
            if e.get('event') == 'dag-preflight-revision-routed'
        ]
        self.assertEqual(len(rev_entries), 1)
        # Still no Larry DM.
        self.assertEqual(self._read_alerts(), [])


class MirrorDagPreflightRevisionSyncLag(_DagHandlerHarness):
    """The `rsdpm-m14-001` class (2026-07-28): Mirror's REVISION is Check 0
    `BEHIND_ORIGIN` — a sync-lag whose remediation is OPERATIONAL (run the
    sync, re-dispatch), not an artifact edit. Routing it to Beacon's amend
    path hands her a revision with nothing to amend; she records
    `dag-preflight-revision-resolved` having changed nothing, and the Larry
    DM has already been suppressed on the assumption the autonomous path
    owns the outcome. It does not — so the notifier must NOT claim it does.
    """

    def _revision_envelope(self, seq_id):
        return {
            'agent': 'mirror',
            'source': 'beacon',
            'task_id': f'review-sequence-dag-{seq_id}',
            'prompt': f'review-sequence-dag {seq_id}',
            'result': (
                'Check 0 resolves to **BEHIND_ORIGIN (exit 3)** — a sync-lag, '
                'not a missing spec. Checks 1-4 deliberately not run.\n\n'
                'result: REVISION\n'
            ),
        }

    def _patch_behind_origin(self, behind_by=1):
        return mock.patch.object(
            on.bsv, 'check_spec_doc_presence',
            return_value=bsv.SpecDocPresence(
                status=bsv.SPEC_DOC_BEHIND_ORIGIN,
                spec_doc='specs/M14-workspace-boundary.md',
                behind_by=behind_by,
                message=(
                    'working copy is behind origin/main by 1 commit(s); '
                    'spec_doc EXISTS on origin/main but is not yet in this '
                    'checkout.'
                ),
            ),
        )

    def test_sync_lag_revision_is_not_routed_to_beacon(self):
        seq = _make_sequence(seq_id='lag-seq', status='pending')
        self._write_sequence(seq)
        with self._patch_behind_origin():
            result = on._handle_mirror_dag_preflight_result(
                self._revision_envelope('lag-seq'),
            )
        self.assertEqual(
            result, 'mirror-dag-preflight:revision-sync-lag:lag-seq',
        )
        # Nothing to amend → Beacon's amend path is NOT engaged.
        self.assertEqual(self._read_beacon_notifies(), [])
        # …and the amend-path stall signal is NOT written, because the amend
        # path never ran. A `dag-preflight-revision-routed` entry here would
        # arm the Check 9 backstop, whose only recovery is to re-route the
        # same un-amendable revision to Beacon again.
        on_disk = self._read_sequence('lag-seq')
        self.assertEqual(on_disk['status'], 'pending')
        events = [e.get('event') for e in on_disk['audit_log']]
        self.assertNotIn('dag-preflight-revision-routed', events)
        self.assertIn('dag-preflight-sync-lag-detected', events)
        entry = next(
            e for e in on_disk['audit_log']
            if e.get('event') == 'dag-preflight-sync-lag-detected'
        )
        self.assertEqual(entry['actor'], 'outbox-notifier')
        self.assertEqual(
            entry['mirror_task_id'], 'review-sequence-dag-lag-seq',
        )
        self.assertEqual(entry['spec_doc'], 'specs/M14-workspace-boundary.md')
        self.assertEqual(entry['behind_by'], 1)

    def test_sync_lag_audit_entry_is_idempotent_on_replay(self):
        seq = _make_sequence(seq_id='lag-seq', status='pending')
        self._write_sequence(seq)
        env = self._revision_envelope('lag-seq')
        with self._patch_behind_origin():
            on._handle_mirror_dag_preflight_result(env)
            on._handle_mirror_dag_preflight_result(env)
        on_disk = self._read_sequence('lag-seq')
        lag_entries = [
            e for e in on_disk['audit_log']
            if e.get('event') == 'dag-preflight-sync-lag-detected'
        ]
        self.assertEqual(len(lag_entries), 1)

    def test_non_sync_lag_revision_still_routes_to_beacon(self):
        """Regression guard: only BEHIND_ORIGIN diverts. A present spec_doc
        (the mechanical Check-3 case) keeps today's Beacon amend route."""
        seq = _make_sequence(seq_id='amendable-seq', status='pending')
        self._write_sequence(seq)
        with mock.patch.object(
            on.bsv, 'check_spec_doc_presence',
            return_value=bsv.SpecDocPresence(
                status=bsv.SPEC_DOC_PRESENT,
                spec_doc='agents/beacon/specs/build-sequence-orchestrator.md',
                message='present',
            ),
        ):
            result = on._handle_mirror_dag_preflight_result(
                self._revision_envelope('amendable-seq'),
            )
        self.assertEqual(
            result, 'mirror-dag-preflight:revision:amendable-seq',
        )
        self.assertEqual(len(self._read_beacon_notifies()), 1)
        events = [
            e.get('event')
            for e in self._read_sequence('amendable-seq')['audit_log']
        ]
        self.assertIn('dag-preflight-revision-routed', events)

    def test_lost_audit_entry_on_sync_lag_dms_larry(self):
        """The sync-lag branch writes NO Beacon notify by design, so the audit
        entry is the sole artifact. If it can't be written (EROFS/ENOSPC, or a
        torn read while the advancer rewrites the file concurrently) the
        REVISION would otherwise vanish entirely: no audit, no notify, no DM,
        and the outbox archived as handled. That is the silent stall this
        whole arc exists to end, so it must escalate."""
        seq = _make_sequence(seq_id='lost-seq', status='pending')
        self._write_sequence(seq)
        with self._patch_behind_origin(), mock.patch.object(
            on.os, 'replace',
            side_effect=OSError('EROFS: read-only file system'),
        ):
            result = on._handle_mirror_dag_preflight_result(
                self._revision_envelope('lost-seq'),
            )
        self.assertEqual(
            result,
            'mirror-dag-preflight:revision-sync-lag-unrecorded:lost-seq',
        )
        events = [
            e.get('event')
            for e in self._read_sequence('lost-seq')['audit_log']
        ]
        self.assertNotIn('dag-preflight-sync-lag-detected', events)
        self.assertNotIn('dag-preflight-revision-routed', events)
        self.assertEqual(self._read_beacon_notifies(), [])
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]['subject'], 'mirror-dag-audit-unrecorded:lost-seq',
        )
        self.assertIn('NOTHING is now watching', alerts[0]['message'])

    def test_lost_audit_entry_on_amend_route_also_dms_larry(self):
        """The amend route DOES leave a live Beacon notify, so this is not the
        total-silence case — but without the audit entry Check 9 cannot arm,
        so a Beacon no-op goes uncaught. Same class, one step removed."""
        seq = _make_sequence(seq_id='amend-lost', status='pending')
        self._write_sequence(seq)
        with mock.patch.object(
            on.bsv, 'check_spec_doc_presence',
            return_value=bsv.SpecDocPresence(
                status=bsv.SPEC_DOC_PRESENT, spec_doc='x.md', message='p',
            ),
        ), mock.patch.object(
            # Targeted, not `os.replace`: the notify write is also atomic, so
            # breaking os-level replace would fail the write this test needs
            # to SUCCEED. False is exactly what the helper returns on a real
            # OSError (covered end-to-end by the sync-lag test above).
            on, '_append_dag_revision_audit', return_value=False,
        ):
            result = on._handle_mirror_dag_preflight_result(
                self._revision_envelope('amend-lost'),
            )
        self.assertEqual(result, 'mirror-dag-preflight:revision:amend-lost')
        # The notify IS live — the autonomous path still has a chance.
        self.assertEqual(len(self._read_beacon_notifies()), 1)
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0]['subject'], 'mirror-dag-audit-unrecorded:amend-lost',
        )

    def test_classifier_failure_falls_back_to_beacon_route(self):
        """Fail-safe: an unreadable/raising classifier must not swallow the
        REVISION. Degrade to today's behaviour (route to Beacon), never to
        silence."""
        seq = _make_sequence(seq_id='boom-seq', status='pending')
        self._write_sequence(seq)
        with mock.patch.object(
            on.bsv, 'check_spec_doc_presence',
            side_effect=RuntimeError('git exploded'),
        ):
            result = on._handle_mirror_dag_preflight_result(
                self._revision_envelope('boom-seq'),
            )
        self.assertEqual(result, 'mirror-dag-preflight:revision:boom-seq')
        self.assertEqual(len(self._read_beacon_notifies()), 1)


class MirrorDagPreflightMalformed(_DagHandlerHarness):

    def test_no_verdict_fires_malformed_alert(self):
        seq = _make_sequence(seq_id='dag-bad-seq', status='pending')
        self._write_sequence(seq)
        envelope = {
            'agent': 'mirror',
            'source': 'beacon',
            'task_id': 'review-sequence-dag-dag-bad-seq',
            'prompt': 'review-sequence-dag dag-bad-seq',
            'result': 'I ran the checks and everything looks great.',
        }
        result = on._handle_mirror_dag_preflight_result(envelope)
        self.assertIsNotNone(result)
        # Status unchanged.
        self.assertEqual(
            self._read_sequence('dag-bad-seq')['status'], 'pending',
        )
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('malformed', alerts[0].get('message', '').lower())

    def test_non_dag_envelope_returns_none(self):
        """Envelope with a non-`review-sequence-dag` prompt falls
        through to the regular Mirror marker classifier."""
        envelope = {
            'agent': 'mirror',
            'source': 'beacon',
            'task_id': 'review-pr-123',
            'prompt': 'review the PR at https://github.com/x/y/pull/123',
            'result': '=== REVIEW_PASS ===\n{}\n=== END_REVIEW_PASS ===',
        }
        result = on._handle_mirror_dag_preflight_result(envelope)
        self.assertIsNone(result)


# ============================================================================
# M4: defensive gate — stray REVIEW_* marker on DAG session
# ============================================================================


class StrayReviewMarkerOnDagSession(_DagHandlerHarness):

    def test_review_pass_marker_on_dag_session_does_not_classify(self):
        """A Mirror DAG-preflight session that accidentally emits a stray
        REVIEW_PASS marker (habit carryover from regular review work)
        MUST NOT route through `_classify_mirror_marker` — that path
        would trigger auto-merge against a fictional PR. The defensive
        gate checks the envelope's `prompt` prefix and returns None."""
        envelope = {
            'agent': 'mirror',
            'source': 'beacon',
            'task_id': 'review-sequence-dag-stray-001',
            'prompt': 'review-sequence-dag stray-001',
            'result': (
                'Looks good.\n\n'
                '=== REVIEW_PASS ===\n'
                '{"task_id": "review-sequence-dag-stray-001", '
                '"summary": "All checks pass"}\n'
                '=== END_REVIEW_PASS ==='
            ),
        }
        # The defensive short-circuit at the top of
        # `_classify_mirror_marker` should return None.
        decision = on._classify_mirror_marker(envelope)
        self.assertIsNone(
            decision,
            'stray REVIEW_PASS on a review-sequence-dag envelope MUST '
            'NOT be classified — the DAG-preflight handler owns this '
            'routing decision via the body\'s `result: PASS|REVISION` '
            'line',
        )


# ============================================================================
# H3: source=orchestrator reaches the headless-approval handler
# ============================================================================


class OrchestratorSourceReachesHandlers(_DagHandlerHarness):

    def _make_marker(self, **overrides):
        payload = {
            'task_id': 'seq-orch-001-step-s1',
            'summary': 'Build step s1.',
            'target_agent': 'forge',
            'prompt': (
                'Build the seq-orch-001 step s1 per the dispatch text. '
                'This dispatch text is intentionally over 100 characters '
                'long so the MIN_PROMPT_LEN check passes for the Forge '
                'target. The step is wired via the advancer daemon.'
            ),
            'target_repo': 'ourliberty-agent-core',
            'task_type': 'feature-development',
        }
        payload.update(overrides)
        return (
            f'=== APPROVAL_REQUEST ===\n{json.dumps(payload)}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )

    def test_orchestrator_source_reaches_headless_approval_handler(self):
        """Before H3 the source gate was `source != "larry": return None`
        — advancer-dispatched envelopes (source='orchestrator') silently
        archived. After H3 the gate is `source not in {larry, orchestrator}`
        and the handler dispatches Forge."""
        envelope = {
            'agent': 'beacon',
            'source': 'orchestrator',
            'task_id': 'seq-orch-001-step-s1',
            'result': self._make_marker(),
        }
        result = on._handle_beacon_headless_approval_request(
            envelope, envelope['result'],
        )
        # Handler claims the envelope — non-None return means Forge
        # task was written.
        self.assertIsNotNone(
            result,
            'headless-approval handler must accept source=orchestrator',
        )
        # Forge inbox has the file.
        forge_inbox = on.INBOXES_ROOT / 'forge'
        files = list(forge_inbox.glob('*.json'))
        self.assertEqual(len(files), 1)
        forge_task = json.loads(files[0].read_text())
        self.assertEqual(forge_task['target_agent'], 'forge')
        self.assertEqual(forge_task['phase'], 'preflight')

    def test_unauthorized_source_still_rejected(self):
        """The gate broadens to {larry, orchestrator} but no further —
        a random source like `pulse` still returns None."""
        envelope = {
            'agent': 'beacon',
            'source': 'pulse',
            'task_id': 'seq-x-step-y',
            'result': self._make_marker(),
        }
        result = on._handle_beacon_headless_approval_request(
            envelope, envelope['result'],
        )
        self.assertIsNone(result)


# ============================================================================
# L5: malformed kickoff prompt fires loud alert
# ============================================================================


class MalformedKickoffPromptAlert(_DagHandlerHarness):

    def _kickoff_marker(self, **overrides):
        payload = {
            'task_id': 'kickoff-bad-seq',
            'summary': 'Kick off the bad sequence.',
            'target_agent': 'build_sequence_advancer',
            # No `kickoff <seq-id>` shape; intentionally wrong.
            'prompt': 'approve bad-seq',
        }
        payload.update(overrides)
        return (
            f'=== APPROVAL_REQUEST ===\n{json.dumps(payload)}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )

    def test_malformed_prompt_fires_alert(self):
        envelope = {
            'agent': 'beacon',
            'source': 'larry',
            'task_id': 'larry-misemit-001',
            'result': self._kickoff_marker(
                # Also strip `task_id: kickoff-<id>` fallback so the
                # no-seq-id branch is forced.
                task_id='no-seqid-fallback',
            ),
        }
        result = on._handle_build_sequence_advancer_kickoff(
            envelope, envelope['result'],
        )
        self.assertIsNotNone(result, 'handler claims a malformed marker')
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        msg = alerts[0].get('message', '')
        self.assertIn('no parseable seq_id', msg)


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
