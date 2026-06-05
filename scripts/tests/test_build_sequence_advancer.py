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
        # Save prior env so tearDown restores rather than blindly popping.
        # Blindly popping OURLIBERTY_AGENTS_ROOT leaked across modules under
        # `unittest discover`: sibling modules that set it at import time
        # (e.g. test_deploy_notifier) found it gone when their tests ran.
        self._prior_agents_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        self._prior_advancer_enabled = os.environ.get(
            'OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED',
        )
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.agents_root)
        os.environ['OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED'] = 'true'
        # safe_write_inbox uses Path.home() not the env var, so override
        # HOME so its module-level INBOXES_ROOT resolves to the tmpdir on
        # the upcoming reload. Without this, the dispatch path would
        # write into the real ~/agents/inboxes/beacon during tests.
        self._prior_home = os.environ.get('HOME')
        os.environ['HOME'] = str(self.agents_root.parent)
        # Force fresh module imports so the env-var-dependent constants
        # re-resolve to the tmpdir. Snapshot the prior sys.modules entries so
        # tearDown can restore them: leaving safe_write_inbox absent leaked
        # under discover (test_outbox_notifier.setUpModule reloads it and
        # raised "module not in sys.modules").
        self._reloaded_mods = (
            'build_sequence_advancer', 'build_sequence_validator',
            'safe_write_inbox', 'sequence_shortcut_helpers',
        )
        self._prior_modules = {
            mod: sys.modules.get(mod) for mod in self._reloaded_mods
        }
        for mod in self._reloaded_mods:
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
        for k, prior in (
            ('OURLIBERTY_AGENTS_ROOT', self._prior_agents_root),
            ('OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED',
             self._prior_advancer_enabled),
        ):
            if prior is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prior
        if self._prior_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self._prior_home
        # Restore the sys.modules entries we popped so sibling modules that
        # reload these (test_outbox_notifier) still find them present.
        for mod, prior in self._prior_modules.items():
            if prior is None:
                sys.modules.pop(mod, None)
            else:
                sys.modules[mod] = prior

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
        # Default reconciler to "no merged PRs in any repo" so tests that
        # don't care about reconciliation don't fire real `gh` subprocess
        # calls. TestActiveReconciliation overrides this per-test.
        self.bsa._gh_list_merged_prs = lambda repo, logger=None: []


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


class TestDispatchFailureRecovery(_AdvancerHarness):
    """Mirror revision 1: dispatch-failure must reset the step to
    'pending' so the next tick re-enters the dispatch loop (which only
    iterates pending steps). Leaving the step in 'dispatchable' would
    strand it forever — the gate-check loop only iterates current_steps;
    the dispatch loop only iterates pending; nothing picks 'dispatchable'
    back up."""

    def test_dispatch_failure_resets_and_recovers_on_next_tick(self):
        seq = _make_sequence(
            steps=[
                _make_step(
                    'alpha', status='merged',
                    pr_url='https://github.com/x/y/pull/1',
                    dispatched_at='2026-05-27T00:00:00+00:00',
                ),
                _make_step('beta', deps=['alpha'], status='pending'),
            ],
            current_steps=[],
        )
        seq['steps'][0]['merged_at'] = '2026-05-27T00:10:00+00:00'
        self._write_sequence(seq)
        self._patch_gates()

        # Fail dispatch on the first call, succeed on the second.
        attempts = {'n': 0}
        original_dispatch = self.bsa._dispatch_step

        def _fail_once_then_succeed(seq_arg, step_arg):
            attempts['n'] += 1
            if attempts['n'] == 1:
                # Don't call the original (no envelope written on failure).
                self.dispatched_envelopes.append(
                    self.bsa._build_step_envelope(seq_arg, step_arg)
                )
                return 'simulated transient dispatch failure'
            return original_dispatch(seq_arg, step_arg)

        self.bsa._dispatch_step = _fail_once_then_succeed

        # First tick: dispatch fails, step must be reset to 'pending'.
        self.bsa.tick()
        seq_after_t1 = self._read_sequence('seq-001')
        beta_t1 = next(
            s for s in seq_after_t1['steps'] if s['step_id'] == 'beta'
        )
        self.assertEqual(
            beta_t1['status'], 'pending',
            msg=(
                'After dispatch failure, step must be reset to pending so '
                'the next tick re-enters the dispatch loop (which filters '
                'on status==pending). Mirror revision 1.'
            ),
        )
        # Sequence still active (not paused — transient failure).
        self.assertEqual(seq_after_t1['status'], 'active')
        # Larry got a dispatch-failed DM.
        fail_dms = [
            d for d in self.dms if 'dispatch-failed' in d['subject']
        ]
        self.assertEqual(len(fail_dms), 1)
        # The audit_log records the failure.
        events_t1 = [e['event'] for e in seq_after_t1['audit_log']]
        self.assertIn('step-dispatch-failed', events_t1)

        # Second tick: dispatch succeeds; step transitions to dispatched.
        self.bsa.tick()
        seq_after_t2 = self._read_sequence('seq-001')
        beta_t2 = next(
            s for s in seq_after_t2['steps'] if s['step_id'] == 'beta'
        )
        self.assertEqual(
            beta_t2['status'], 'dispatched',
            msg=(
                'After a successful retry on tick 2, step must transition '
                'to dispatched. The reset-to-pending in tick 1 is what '
                'allows this re-entry; the test would fail with the pre-'
                'revision code that left the step at dispatchable.'
            ),
        )
        self.assertIn('beta', seq_after_t2['current_steps'])


class TestActiveReconciliation(_AdvancerHarness):
    """Active reconciliation pass — backstop for the V6 notifier hook.

    The notifier looks up "which step does this task_id belong to" by
    exact task_id match; under rebase / rescue / revision dispatches the
    auto-merge fires under a derivative task_id and the step strands in
    `dispatched`. This pass identity-matches dispatched steps against
    merged PRs (pr_url → branch → title-substring) and fires
    apply_step_merged on the match."""

    def _patch_gh_pr_list(self, prs_by_repo):
        """Stub _gh_list_merged_prs to return the given mapping (or None
        for repos where reconciliation should soft-fail)."""
        def _fake_list(repo, logger=None):
            return prs_by_repo.get(repo)
        self.bsa._gh_list_merged_prs = _fake_list

    def test_reconcile_matches_by_pr_url(self):
        seq = _make_sequence(
            steps=[
                _make_step(
                    'step-foo', status='dispatched',
                    pr_url='https://github.com/x/y/pull/42',
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
            ],
            current_steps=['step-foo'],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({
            'ourliberty-agent-core': [
                {
                    'number': 42,
                    'url': 'https://github.com/x/y/pull/42',
                    'title': 'unrelated title',
                    'headRefName': 'unrelated/branch',
                    'mergedAt': '2026-05-30T01:00:00Z',
                },
            ],
        })
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == 'step-foo')
        self.assertEqual(step['status'], 'merged')
        self.assertEqual(step['pr_url'], 'https://github.com/x/y/pull/42')
        events = [(e['event'], e.get('actor')) for e in seq2['audit_log']]
        self.assertIn(('step-merged', 'advancer-reconcile'), events)

    def test_reconcile_matches_by_branch_name(self):
        seq = _make_sequence(
            steps=[
                _make_step(
                    'step-foo', status='dispatched',
                    pr_url=None,
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
            ],
            current_steps=['step-foo'],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({
            'ourliberty-agent-core': [
                {
                    'number': 99,
                    'url': 'https://github.com/x/y/pull/99',
                    'title': 'rebase: thing',
                    'headRefName': 'forge/step-foo',
                    'mergedAt': '2026-05-30T01:00:00Z',
                },
            ],
        })
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == 'step-foo')
        self.assertEqual(step['status'], 'merged')
        self.assertEqual(step['pr_url'], 'https://github.com/x/y/pull/99')

    def test_reconcile_matches_by_title_substring(self):
        seq = _make_sequence(
            steps=[
                _make_step(
                    'step-bar', status='dispatched',
                    pr_url=None,
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
            ],
            current_steps=['step-bar'],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({
            'ourliberty-agent-core': [
                {
                    'number': 100,
                    'url': 'https://github.com/x/y/pull/100',
                    'title': 'fix(thing) — step-bar work',
                    'headRefName': 'pr100-rebase-step-bar-001',
                    'mergedAt': '2026-05-30T01:00:00Z',
                },
            ],
        })
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == 'step-bar')
        self.assertEqual(step['status'], 'merged')

    def test_reconcile_short_step_id_title_match_needs_branch_corroboration(self):
        # Audit #9 false-positive shape: a short/common step_id ('api')
        # appears as a token in an UNRELATED merged PR's title, but the
        # branch does NOT carry it. The pre-fix bare `step_id in title`
        # test advanced the wrong step here; the corroboration guard must
        # NOT advance — the step stays dispatched.
        seq = _make_sequence(
            steps=[
                _make_step(
                    'api', status='dispatched',
                    pr_url=None,
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
            ],
            current_steps=['api'],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({
            'ourliberty-agent-core': [
                {
                    'number': 500,
                    'url': 'https://github.com/x/y/pull/500',
                    # 'api' is a boundary token in the title but NOT in the
                    # branch — corroboration fails.
                    'title': 'feat(api): unrelated change',
                    'headRefName': 'feat/unrelated-change',
                    'mergedAt': '2026-05-30T01:00:00Z',
                },
            ],
        })
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == 'api')
        self.assertEqual(step['status'], 'dispatched')
        events = [e['event'] for e in seq2['audit_log']]
        self.assertNotIn('step-merged', events)

    def test_reconcile_short_step_id_advances_when_branch_corroborates(self):
        # The legitimate flip side of the guard: the SAME short step_id
        # ('api') DOES advance when both the title AND the branch carry it
        # as a token — the designed derivative-branch case (rebase/rescue
        # dispatch, no pr_url, auto-merged under a derivative branch). This
        # is the case a bare length floor (min_len=12) would wrongly kill.
        seq = _make_sequence(
            steps=[
                _make_step(
                    'api', status='dispatched',
                    pr_url=None,
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
            ],
            current_steps=['api'],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({
            'ourliberty-agent-core': [
                {
                    'number': 501,
                    'url': 'https://github.com/x/y/pull/501',
                    'title': 'fix(api): rotation rebuild — step api',
                    'headRefName': 'pr501-rebase-step-api-001',
                    'mergedAt': '2026-05-30T01:00:00Z',
                },
            ],
        })
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == 'api')
        self.assertEqual(step['status'], 'merged')

    def test_reconcile_no_match_no_op(self):
        seq = _make_sequence(
            steps=[
                _make_step(
                    'step-baz', status='dispatched',
                    pr_url=None,
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
            ],
            current_steps=['step-baz'],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({
            'ourliberty-agent-core': [
                {
                    'number': 101,
                    'url': 'https://github.com/x/y/pull/101',
                    'title': 'unrelated',
                    'headRefName': 'forge/something-else',
                    'mergedAt': '2026-05-30T01:00:00Z',
                },
            ],
        })
        rc = self.bsa.tick()
        self.assertEqual(rc, 0)
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == 'step-baz')
        self.assertEqual(step['status'], 'dispatched')
        events = [e['event'] for e in seq2['audit_log']]
        self.assertNotIn('step-merged', events)

    def test_reconcile_skips_already_merged_steps(self):
        # Reconciler iterates only `dispatched` steps; merged steps are
        # not even considered. Asserts the source-of-truth filter, not
        # apply_step_merged's own idempotence (which is also true).
        seq = _make_sequence(
            steps=[
                _make_step(
                    'step-foo', status='merged',
                    pr_url='https://github.com/x/y/pull/42',
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
            ],
            current_steps=[],
        )
        seq['steps'][0]['merged_at'] = '2026-05-30T00:30:00+00:00'
        # Sequence already had its completion event, mark as complete so
        # the tick is a true no-op (no sequence-complete DM either).
        seq['status'] = 'complete'
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        calls = {'n': 0}

        def _counting_list(repo, logger=None):
            calls['n'] += 1
            return [
                {
                    'number': 42,
                    'url': 'https://github.com/x/y/pull/42',
                    'title': 'step-foo',
                    'headRefName': 'forge/step-foo',
                    'mergedAt': '2026-05-30T00:30:00Z',
                },
            ]
        self.bsa._gh_list_merged_prs = _counting_list
        self.bsa.tick()
        # Sequence is `complete` so the tick skipped it before even
        # entering reconcile — gh_list shouldn't have been queried.
        self.assertEqual(calls['n'], 0)
        seq2 = self._read_sequence('seq-001')
        # No new step-merged audit entry (the only `step-merged` would be
        # from a re-fire of apply_step_merged, which the reconciler must
        # never trigger on a non-dispatched step).
        events = [e for e in seq2['audit_log'] if e.get('event') == 'step-merged']
        self.assertEqual(events, [])

    def test_reconcile_handles_gh_failure_gracefully(self):
        # When gh returns None (timeout / non-zero rc / missing), the
        # reconciler logs WARN and continues; the rest of the tick runs.
        seq = _make_sequence(
            steps=[
                _make_step(
                    'step-foo', status='dispatched',
                    pr_url='https://github.com/x/y/pull/42',
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
            ],
            current_steps=['step-foo'],
        )
        self._write_sequence(seq)
        # Gate path: chain says merged + gh says merged → existing flow
        # would advance. We need to confirm that even though gh-list
        # fails, the rest of the tick still works.
        self._patch_gates(chain_merged=True, gh_merged=True)
        self._patch_gh_pr_list({})  # repo missing → None
        rc = self.bsa.tick()
        self.assertEqual(rc, 0)
        seq2 = self._read_sequence('seq-001')
        # The existing belt-and-suspenders gate (not the reconciler)
        # advanced the step — proves the rest of the tick continued
        # despite the gh-list failure.
        step = next(s for s in seq2['steps'] if s['step_id'] == 'step-foo')
        self.assertEqual(step['status'], 'merged')

    def test_reconcile_then_dispatch_downstream_same_tick(self):
        # End-to-end: step-A is silent-missed (dispatched but merged
        # under a different task_id). Reconcile fires apply_step_merged
        # for step-A; THEN dispatch logic sees step-B's deps satisfied
        # and dispatches it on the SAME tick. This proves the
        # reconcile-before-dispatch ordering.
        seq = _make_sequence(
            steps=[
                _make_step(
                    'step-a', status='dispatched',
                    pr_url=None,
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
                _make_step('step-b', deps=['step-a'], status='pending'),
            ],
            current_steps=['step-a'],
        )
        self._write_sequence(seq)
        # Belt-and-suspenders gate would NOT advance step-a (chain
        # missing the task_id signal — the whole point of the silent-
        # miss shape). Reconciler is the ONLY path that can advance it.
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({
            'ourliberty-agent-core': [
                {
                    'number': 211,
                    'url': 'https://github.com/x/y/pull/211',
                    'title': 'rebase under different task_id',
                    'headRefName': 'forge/step-a',
                    'mergedAt': '2026-05-30T01:00:00Z',
                },
            ],
        })
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step_a = next(s for s in seq2['steps'] if s['step_id'] == 'step-a')
        step_b = next(s for s in seq2['steps'] if s['step_id'] == 'step-b')
        self.assertEqual(step_a['status'], 'merged')
        # The key assertion: step-b dispatches on the SAME tick because
        # the reconciler's apply_step_merged write to disk is visible to
        # _process_active_sequence's `merged_ids` calculation.
        self.assertEqual(step_b['status'], 'dispatched')
        self.assertIn('step-b', seq2['current_steps'])
        self.assertEqual(len(self.dispatched_envelopes), 1)
        self.assertEqual(
            self.dispatched_envelopes[0]['task_id'],
            'seq-seq-001-step-step-b',
        )

    def test_reconcile_caches_gh_call_per_repo(self):
        # Two dispatched steps in the same repo → one gh call, not two.
        seq = _make_sequence(
            steps=[
                _make_step(
                    'step-x', status='dispatched',
                    pr_url=None,
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
                _make_step(
                    'step-y', status='dispatched',
                    pr_url=None,
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
            ],
            current_steps=['step-x', 'step-y'],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        calls = {'n': 0}

        def _counting_list(repo, logger=None):
            calls['n'] += 1
            return []  # no merges → no reconcile fires, but still 1 call

        self.bsa._gh_list_merged_prs = _counting_list
        self.bsa.tick()
        self.assertEqual(calls['n'], 1)

    def test_reconcile_logs_gh_failure_detail(self):
        # Regression for the silent-ish failure that stranded three
        # incidents: a non-zero `gh` exit must surface its returncode +
        # stderr in the WARNING, not just a bare "gh pr list failed".
        class _Proc:
            returncode = 1
            stdout = ''
            stderr = 'auth error: not logged in to github.com'

        logger = _RecordingLogger()
        with patch.object(self.bsa.subprocess, 'run', return_value=_Proc()):
            result = self.bsa._gh_list_merged_prs(
                'ourliberty-agent-core', logger,
            )
        self.assertIsNone(result)
        joined = '\n'.join(logger.warnings)
        self.assertIn('rc=1', joined)
        self.assertIn('auth error', joined)

    def test_reconcile_gh_subprocess_inherits_env_and_qualifies_repo(self):
        # Two-in-one: (a) the gh subprocess must inherit the daemon's full
        # environment (no stripped `env=` that would drop HOME/PATH/auth —
        # matches the working notifier pattern), and (b) the bare repo name
        # must be owner-qualified to OWNER/REPO, which is the actual fix
        # for the rc=1 "expected [HOST/]OWNER/REPO format" failure.
        captured = {}

        class _Proc:
            returncode = 0
            stdout = '[]'
            stderr = ''

        def _fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            captured['kwargs'] = kwargs
            return _Proc()

        with patch.object(self.bsa.subprocess, 'run', _fake_run):
            result = self.bsa._gh_list_merged_prs('ourliberty-agent-core')
        self.assertEqual(result, [])
        # env not passed → subprocess inherits the full environment.
        self.assertNotIn('env', captured['kwargs'])
        # repo qualified with the owner; the bare name is never passed alone.
        repo_arg = captured['cmd'][captured['cmd'].index('--repo') + 1]
        self.assertEqual(repo_arg, 'Larry-Yatch/ourliberty-agent-core')

    def test_qualify_repo_leaves_owner_qualified_value_untouched(self):
        # Idempotent: an already-qualified value passes through unchanged.
        self.assertEqual(
            self.bsa._qualify_repo('Larry-Yatch/ourliberty-dashboard'),
            'Larry-Yatch/ourliberty-dashboard',
        )
        self.assertEqual(
            self.bsa._qualify_repo('ourliberty-dashboard'),
            'Larry-Yatch/ourliberty-dashboard',
        )


class _RecordingLogger:
    """Minimal logger stand-in capturing warning() message strings."""

    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *args, **kwargs):
        self.warnings.append(str(msg))

    def info(self, *a, **kw):
        pass

    def error(self, *a, **kw):
        pass


if __name__ == '__main__':
    unittest.main()
