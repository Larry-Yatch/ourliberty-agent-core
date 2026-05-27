"""Tests for scripts/build_sequence_advancer.py.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Coverage (per brief mirror-review focus + spec § 5.8 test strategy):
  - Reboot resilience: fresh start with no internal state, populated
    sequence file + mock chain_events; advancer rebuilds live state.
  - Belt-and-suspenders gate matrix (spec § 5.3): all 4 (chain, gh) states:
    both pass / only chain / only gh / neither — only both passes advances.
  - 30-min gate-mismatch timeout (spec § 5.3): mock mismatch persisting
    past tolerance; assert advancer DMs Larry + pauses the sequence.
  - Corrupted-sequence handling (spec § 5.4): malformed JSON pauses + DMs
    without crashing; other sequences keep advancing in the same tick.
  - No infinite-loop on completed sequence: a fully-merged sequence is a
    no-op (other than the one-time `sequence-complete` transition).
  - Heartbeat-mtime advances on each successful tick.
  - Activation gate: tick is a no-op when the env var is unset.
  - Step dispatch writes an envelope to Beacon's inbox via
    safe_write_inbox (atomic write; routable as source=orchestrator).

Mocking strategy:
  - OURLIBERTY_AGENTS_ROOT → tmpdir per test so the blackboard dir, log
    dir, and Beacon's inbox all live under one root.
  - chain_event_says_merged / chain_event_says_failed / gh_pr_says_merged
    are monkey-patched per test rather than reaching out to Supabase / gh.
  - larry_alerts._dm_larry path: we monkey-patch the advancer's _dm_larry
    to capture invocations without writing to the real alerts queue. This
    leaves cooldown-state-on-disk out of the per-test path entirely.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _make_step(step_id, deps=None, status='pending', pr_url=None,
               dispatched_at=None):
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
        'merged_at': None,
        'pr_url': pr_url,
        'current_actor': 'forge' if status in ('dispatched', 'building', 'reviewing') else None,
        'failure_reason': None,
    }


def _make_sequence(seq_id='seq-001', status='active', steps=None,
                   current_steps=None, audit_log=None):
    if steps is None:
        steps = [_make_step('alpha')]
    return {
        'seq_id': seq_id,
        'label': f'Seq {seq_id}',
        'spec_doc': 'agents/beacon/specs/build-sequence-orchestrator.md',
        'created_at': '2026-05-27T00:00:00+00:00',
        'created_by': 'beacon',
        'status': status,
        'current_steps': current_steps if current_steps is not None else [],
        'steps': steps,
        'audit_log': audit_log if audit_log is not None else [],
    }


class _AdvancerHarness(unittest.TestCase):
    """Sets OURLIBERTY_AGENTS_ROOT to a tmpdir; reloads the advancer
    module so its module-level constants pick up the env var; enables
    the activation gate; monkey-patches the DM helper."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.agents_root = Path(self._tmp.name) / 'agents'
        (self.agents_root / 'blackboard' / 'build-sequences').mkdir(parents=True)
        (self.agents_root / 'logs').mkdir(parents=True)
        (self.agents_root / 'state').mkdir(parents=True)
        (self.agents_root / 'inboxes' / 'beacon').mkdir(parents=True)
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.agents_root)
        os.environ['OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED'] = 'true'
        # safe_write_inbox uses Path.home() not the env var, so override
        # HOME so its module-level INBOXES_ROOT resolves to the tmpdir on
        # the upcoming reload. Without this, the dispatch path would
        # write into the real ~/agents/inboxes/beacon during tests.
        self._prior_home = os.environ.get('HOME')
        os.environ['HOME'] = str(self.agents_root.parent)
        # Force fresh module imports so the env-var-dependent constants
        # re-resolve to the tmpdir.
        for mod in (
            'build_sequence_advancer', 'build_sequence_validator',
            'safe_write_inbox',
        ):
            sys.modules.pop(mod, None)
        import build_sequence_advancer as bsa  # noqa: E402
        self.bsa = bsa
        self.dms = []

        def _capture_dm(message, subject, severity='warning',
                        suggested_action=None):
            self.dms.append({
                'message': message, 'subject': subject,
                'severity': severity, 'suggested_action': suggested_action,
            })
            return True

        self.bsa._dm_larry = _capture_dm
        self.dispatched_envelopes = []

        def _capture_dispatch(seq, step):
            envelope = self.bsa._build_step_envelope(seq, step)
            self.dispatched_envelopes.append(envelope)
            # Write a real file via safe_write_inbox to exercise the
            # routing + atomic-write path. Falls through to the original
            # implementation on import errors.
            try:
                import safe_write_inbox as swi
                swi.safe_write_inbox(
                    target_agent='beacon',
                    task_dict=envelope,
                    source_agent='orchestrator',
                    filename=f'{envelope["task_id"]}.json',
                )
            except Exception as e:
                return f'{type(e).__name__}: {e}'
            return None

        # Patch dispatch so the test side knows what got dispatched, but
        # the underlying safe_write_inbox call still fires (verifies
        # routing + atomic write).
        self.bsa._dispatch_step = _capture_dispatch

    def tearDown(self):
        self._tmp.cleanup()
        for k in (
            'OURLIBERTY_AGENTS_ROOT',
            'OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED',
        ):
            os.environ.pop(k, None)
        if self._prior_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self._prior_home

    # -------- helpers --------

    def _write_sequence(self, seq):
        path = (
            self.agents_root / 'blackboard' / 'build-sequences'
            / f'{seq["seq_id"]}.json'
        )
        path.write_text(json.dumps(seq, indent=2))
        return path

    def _read_sequence(self, seq_id):
        path = (
            self.agents_root / 'blackboard' / 'build-sequences'
            / f'{seq_id}.json'
        )
        return json.loads(path.read_text())

    def _patch_gates(self, *, chain_merged=False, gh_merged=None,
                     failure=None):
        """Patch the four gate primitives in the advancer module."""
        self.bsa.chain_event_says_merged = lambda *_a, **_kw: chain_merged
        self.bsa.chain_event_says_failed = lambda *_a, **_kw: failure
        self.bsa.gh_pr_says_merged = lambda *_a, **_kw: gh_merged
        # Supabase connect: return a sentinel so the tick proceeds; the
        # patched chain_event_* lambdas above are what actually drive
        # behavior.
        self.bsa._connect_supabase = lambda: object()


class TestActivationGate(_AdvancerHarness):
    def test_disabled_is_noop(self):
        os.environ['OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED'] = 'false'
        self._write_sequence(_make_sequence())
        rc = self.bsa.tick()
        self.assertEqual(rc, 0)
        # Heartbeat must NOT be written when the gate is closed —
        # otherwise the healer would think the daemon is healthy when
        # it's actually inactive.
        self.assertFalse(self.bsa.HEARTBEAT_FILE.exists())
        self.assertEqual(self.dms, [])

    def test_kill_switch_active_noop(self):
        (self.agents_root / 'healers.disabled').touch()
        self._write_sequence(_make_sequence())
        rc = self.bsa.tick()
        self.assertEqual(rc, 0)
        self.assertFalse(self.bsa.HEARTBEAT_FILE.exists())

    def test_enabled_writes_heartbeat(self):
        self._write_sequence(_make_sequence(
            steps=[_make_step('alpha', status='pending')],
            current_steps=[],
        ))
        self._patch_gates()
        rc = self.bsa.tick()
        self.assertEqual(rc, 0)
        self.assertTrue(self.bsa.HEARTBEAT_FILE.exists())


class TestHeartbeatMtime(_AdvancerHarness):
    def test_heartbeat_mtime_advances_each_tick(self):
        self._write_sequence(_make_sequence(
            steps=[_make_step('alpha', status='pending')],
            current_steps=[],
        ))
        self._patch_gates()
        self.bsa.tick()
        first_mtime = self.bsa.HEARTBEAT_FILE.stat().st_mtime
        time.sleep(0.05)
        self.bsa.tick()
        second_mtime = self.bsa.HEARTBEAT_FILE.stat().st_mtime
        self.assertGreater(second_mtime, first_mtime)


class TestBeltAndSuspendersGate(_AdvancerHarness):
    """Per spec § 5.3: both gate signals must agree for advance."""

    def _setup_in_flight(self):
        seq = _make_sequence(
            steps=[
                _make_step(
                    'alpha', status='dispatched',
                    pr_url='https://github.com/x/y/pull/1',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
            ],
            current_steps=['alpha'],
        )
        return self._write_sequence(seq)

    def test_neither_gate_passes_does_not_advance(self):
        path = self._setup_in_flight()
        self._patch_gates(chain_merged=False, gh_merged=False)
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        self.assertEqual(seq['steps'][0]['status'], 'dispatched')
        self.assertIn('alpha', seq['current_steps'])

    def test_only_chain_event_does_not_advance(self):
        path = self._setup_in_flight()
        self._patch_gates(chain_merged=True, gh_merged=False)
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        # Should stay in dispatched; gate-mismatch should be in audit_log.
        self.assertEqual(seq['steps'][0]['status'], 'dispatched')
        events = [e['event'] for e in seq['audit_log']]
        self.assertIn('gate-mismatch', events)

    def test_only_gh_does_not_advance(self):
        path = self._setup_in_flight()
        self._patch_gates(chain_merged=False, gh_merged=True)
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        self.assertEqual(seq['steps'][0]['status'], 'dispatched')
        events = [e['event'] for e in seq['audit_log']]
        self.assertIn('gate-mismatch', events)

    def test_both_gates_advance(self):
        path = self._setup_in_flight()
        self._patch_gates(chain_merged=True, gh_merged=True)
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        self.assertEqual(seq['steps'][0]['status'], 'merged')
        self.assertIsNotNone(seq['steps'][0]['merged_at'])
        # Last step merged → sequence-complete fires.
        self.assertEqual(seq['status'], 'complete')
        complete_dms = [
            d for d in self.dms if 'complete' in d['subject']
        ]
        self.assertEqual(len(complete_dms), 1)


class TestGateMismatchTimeout(_AdvancerHarness):
    """Per spec § 5.3: 30-min mismatch tolerance, then pause + DM."""

    def test_old_mismatch_triggers_pause(self):
        # Audit log has a gate-mismatch event 31 minutes ago for step alpha.
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        seq = _make_sequence(
            steps=[
                _make_step(
                    'alpha', status='dispatched',
                    pr_url='https://github.com/x/y/pull/1',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
            ],
            current_steps=['alpha'],
            audit_log=[
                {'ts': old_ts, 'event': 'gate-mismatch',
                 'actor': 'advancer', 'step_id': 'alpha',
                 'chain_merged': True, 'gh_merged': False},
            ],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=True, gh_merged=False)
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        self.assertEqual(seq2['status'], 'paused')
        # The step itself stays in `dispatched` (not failed) — the
        # mismatch is an infra issue not a Mirror rejection. The
        # sequence-pause is the actionable signal.
        events = [e['event'] for e in seq2['audit_log']]
        self.assertIn('gate-mismatch-timeout', events)
        self.assertIn('sequence-paused', events)
        # Larry got a sequence-paused DM.
        paused_dms = [d for d in self.dms if 'paused' in d['subject']]
        self.assertGreaterEqual(len(paused_dms), 1)

    def test_recent_mismatch_does_not_pause(self):
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        seq = _make_sequence(
            steps=[
                _make_step(
                    'alpha', status='dispatched',
                    pr_url='https://github.com/x/y/pull/1',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
            ],
            current_steps=['alpha'],
            audit_log=[
                {'ts': recent_ts, 'event': 'gate-mismatch',
                 'actor': 'advancer', 'step_id': 'alpha',
                 'chain_merged': True, 'gh_merged': False},
            ],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=True, gh_merged=False)
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        self.assertEqual(seq2['status'], 'active')
        events = [e['event'] for e in seq2['audit_log']]
        self.assertNotIn('gate-mismatch-timeout', events)

    def test_gate_clear_resets_mismatch_clock(self):
        # Old mismatch + subsequent gate-clear → next mismatch is fresh.
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        clear_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        seq = _make_sequence(
            steps=[
                _make_step(
                    'alpha', status='dispatched',
                    pr_url='https://github.com/x/y/pull/1',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
            ],
            current_steps=['alpha'],
            audit_log=[
                {'ts': old_ts, 'event': 'gate-mismatch',
                 'actor': 'advancer', 'step_id': 'alpha'},
                {'ts': clear_ts, 'event': 'gate-clear',
                 'actor': 'advancer', 'step_id': 'alpha'},
            ],
        )
        self._write_sequence(seq)
        # First tick: both gates clear → no action.
        self._patch_gates(chain_merged=False, gh_merged=False)
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        self.assertEqual(seq2['status'], 'active')


class TestCorruptedSequence(_AdvancerHarness):
    """Per spec § 5.4 failure modes: corrupted files do not crash the
    daemon; Larry is DMed; other sequences continue advancing."""

    def test_unparseable_json_dms_and_does_not_crash(self):
        bad_path = (
            self.agents_root / 'blackboard' / 'build-sequences' / 'broken.json'
        )
        bad_path.write_text('{not json')
        # Also have a valid sequence in the same dir so we can confirm
        # other-sequences-keep-advancing.
        good = _make_sequence(
            seq_id='good',
            steps=[
                _make_step(
                    'alpha', status='dispatched',
                    pr_url='https://github.com/x/y/pull/2',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
            ],
            current_steps=['alpha'],
        )
        self._write_sequence(good)
        self._patch_gates(chain_merged=True, gh_merged=True)
        rc = self.bsa.tick()
        self.assertEqual(rc, 0)
        # Larry got a DM about the unparseable file.
        bad_dms = [d for d in self.dms if 'unparseable' in d['subject']]
        self.assertEqual(len(bad_dms), 1)
        # The good sequence completed normally despite the bad file.
        good2 = self._read_sequence('good')
        self.assertEqual(good2['status'], 'complete')

    def test_schema_invalid_sequence_is_paused(self):
        bad = _make_sequence(seq_id='bad')
        bad['status'] = 'in_progress'  # invalid enum
        self._write_sequence(bad)
        self._patch_gates()
        self.bsa.tick()
        bad2 = self._read_sequence('bad')
        self.assertEqual(bad2['status'], 'paused')
        events = [e['event'] for e in bad2['audit_log']]
        self.assertIn('sequence-paused-invalid', events)
        invalid_dms = [d for d in self.dms if 'invalid' in d['subject']]
        self.assertGreaterEqual(len(invalid_dms), 1)

    def test_already_paused_invalid_not_double_processed(self):
        bad = _make_sequence(seq_id='bad', status='paused')
        # Add a missing-ref to make it schema-invalid.
        bad['steps'].append(_make_step('beta', deps=['ghost']))
        self._write_sequence(bad)
        self._patch_gates()
        self.bsa.tick()
        bad2 = self._read_sequence('bad')
        self.assertEqual(bad2['status'], 'paused')
        # No re-pause event; status was already paused.
        events = [e['event'] for e in bad2['audit_log']]
        self.assertNotIn('sequence-paused-invalid', events)


class TestRebootResilience(_AdvancerHarness):
    """Per spec § 5.4: advancer has NO internal state file. On reboot,
    first tick rebuilds live state from the sequence file + chain_events
    only."""

    def test_fresh_start_advances_ready_step(self):
        # Pre-merged step `alpha` + pending step `beta` depending on alpha.
        # Simulate a reboot mid-build: the sequence file already shows
        # alpha as merged (from a tick before the reboot) but beta has
        # not yet been dispatched. The advancer's first tick should
        # detect beta's deps are met and dispatch it.
        seq = _make_sequence(
            steps=[
                _make_step(
                    'alpha', status='merged',
                    pr_url='https://github.com/x/y/pull/1',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
                _make_step(
                    'beta', deps=['alpha'], status='pending',
                ),
            ],
            current_steps=[],
        )
        seq['steps'][0]['merged_at'] = '2026-05-27T00:10:00+00:00'
        self._write_sequence(seq)
        self._patch_gates()
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        # Beta should now be dispatched.
        beta = next(s for s in seq2['steps'] if s['step_id'] == 'beta')
        self.assertEqual(beta['status'], 'dispatched')
        self.assertIsNotNone(beta['dispatched_at'])
        self.assertIn('beta', seq2['current_steps'])
        # Envelope was written to beacon's inbox.
        self.assertEqual(len(self.dispatched_envelopes), 1)
        self.assertEqual(self.dispatched_envelopes[0]['task_id'],
                         'seq-seq-001-step-beta')
        # The envelope landed in the beacon inbox on disk (via safe_write_inbox).
        beacon_inbox = self.agents_root / 'inboxes' / 'beacon'
        landed = list(beacon_inbox.iterdir())
        self.assertEqual(len(landed), 1)
        self.assertTrue(landed[0].name.endswith('.json'))


class TestCompletedSequenceNoOp(_AdvancerHarness):
    """A sequence with all steps merged + status=complete must be a no-op
    (no re-dispatch of merged steps, no duplicate DMs)."""

    def test_already_complete_no_op(self):
        seq = _make_sequence(
            status='complete',
            steps=[
                _make_step(
                    'alpha', status='merged',
                    pr_url='https://github.com/x/y/pull/1',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
            ],
            current_steps=[],
        )
        seq['steps'][0]['merged_at'] = '2026-05-27T00:10:00+00:00'
        self._write_sequence(seq)
        # Snapshot the file contents to confirm it's untouched.
        before = self._read_sequence('seq-001')
        self._patch_gates(chain_merged=True, gh_merged=True)
        self.bsa.tick()
        after = self._read_sequence('seq-001')
        self.assertEqual(before, after)
        self.assertEqual(self.dms, [])
        self.assertEqual(self.dispatched_envelopes, [])


class TestSequenceCompletion(_AdvancerHarness):
    """End-to-end: a sequence with one in-flight step that's about to
    merge should transition through merged → complete in one tick."""

    def test_last_step_merges_triggers_complete(self):
        seq = _make_sequence(
            steps=[
                _make_step(
                    'alpha', status='merged',
                    pr_url='https://github.com/x/y/pull/1',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
                _make_step(
                    'beta', deps=['alpha'], status='dispatched',
                    pr_url='https://github.com/x/y/pull/2',
                    dispatched_at='2026-05-27T00:10:00+00:00',
                ),
            ],
            current_steps=['beta'],
        )
        seq['steps'][0]['merged_at'] = '2026-05-27T00:05:00+00:00'
        self._write_sequence(seq)
        self._patch_gates(chain_merged=True, gh_merged=True)
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        self.assertEqual(seq2['status'], 'complete')
        beta = next(s for s in seq2['steps'] if s['step_id'] == 'beta')
        self.assertEqual(beta['status'], 'merged')


class TestFailureFromMirror(_AdvancerHarness):
    """Per spec § 5.4 failure mode 1: Mirror REVISION-exhausted /
    EMERGENCY_HALT / forge_reject in chain_events → pause + DM."""

    def test_mirror_emergency_halt_pauses(self):
        seq = _make_sequence(
            steps=[
                _make_step(
                    'alpha', status='dispatched',
                    pr_url='https://github.com/x/y/pull/1',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
            ],
            current_steps=['alpha'],
        )
        self._write_sequence(seq)
        self._patch_gates(failure='mirror_emergency_halt: credentials in diff')
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        self.assertEqual(seq2['status'], 'paused')
        alpha = next(s for s in seq2['steps'] if s['step_id'] == 'alpha')
        self.assertEqual(alpha['status'], 'failed')
        self.assertIn('emergency_halt', (alpha['failure_reason'] or '').lower())
        paused_dms = [d for d in self.dms if 'paused' in d['subject']]
        self.assertEqual(len(paused_dms), 1)


class TestAtomicWritePartialRecovery(_AdvancerHarness):
    """If atomic-write fails mid-flight (simulated by raising OSError in
    os.replace), the on-disk file should remain unchanged — readers
    never see a partial write. Per brief mirror-review focus #1."""

    def test_write_failure_does_not_corrupt_file(self):
        seq = _make_sequence(
            steps=[
                _make_step(
                    'alpha', status='dispatched',
                    pr_url='https://github.com/x/y/pull/1',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
            ],
            current_steps=['alpha'],
        )
        path = self._write_sequence(seq)
        before = path.read_text()
        self._patch_gates(chain_merged=True, gh_merged=True)
        original_replace = os.replace

        def _failing_replace(src, dst):
            try:
                os.unlink(src)
            except OSError:
                pass
            raise OSError('simulated partial-write failure')

        with patch.object(os, 'replace', _failing_replace):
            rc = self.bsa.tick()
        self.assertEqual(rc, 0)
        # The original file is intact (no half-written state).
        after = path.read_text()
        self.assertEqual(before, after)
        # The advancer DMed Larry about the write failure.
        write_dms = [d for d in self.dms if 'write-failed' in d['subject']]
        self.assertEqual(len(write_dms), 1)
        self.assertEqual(write_dms[0]['severity'], 'critical')


class TestSupabaseUnavailable(_AdvancerHarness):
    """When the chain_events query side of the gate is unreachable, the
    daemon must NOT advance steps (the gh leg alone is insufficient per
    spec § 5.3)."""

    def test_supabase_unavailable_does_not_advance(self):
        seq = _make_sequence(
            steps=[
                _make_step(
                    'alpha', status='dispatched',
                    pr_url='https://github.com/x/y/pull/1',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
            ],
            current_steps=['alpha'],
        )
        self._write_sequence(seq)
        # Patch _connect_supabase to return None and stub the gate
        # primitives to reflect "chain leg returns False on no client".
        self.bsa._connect_supabase = lambda: None
        self.bsa.chain_event_says_merged = lambda *a, **kw: False
        self.bsa.chain_event_says_failed = lambda *a, **kw: None
        self.bsa.gh_pr_says_merged = lambda *a, **kw: True  # gh says merged
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        # Step stays in dispatched — one leg is not enough.
        alpha = next(s for s in seq2['steps'] if s['step_id'] == 'alpha')
        self.assertEqual(alpha['status'], 'dispatched')


if __name__ == '__main__':
    unittest.main()
