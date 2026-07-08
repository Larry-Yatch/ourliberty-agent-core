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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _recent_iso(minutes_ago=5):
    """A `dispatched_at` well inside the stranded-dispatch stall window
    (DISPATCH_STALL_TIMEOUT_SEC, default 4h). Reconcile tests that keep a step
    `dispatched` with no PR use this so the realistic 'just dispatched, no PR
    yet' scenario isn't also tripped by the stall backstop (the backstop only
    targets genuinely-old dispatches; the fixed 3-weeks-ago fixture timestamps
    were incidental, not the test's intent)."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


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

    def test_old_mismatch_gh_merged_completes_not_pauses(self):
        # The XII case: gh says MERGED but chain_events lagged past the 30-min
        # window. gh is authoritative, so the step COMPLETES — no pause, no
        # page. (Previously this false-paused the sequence and DMed Larry.)
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
                 'chain_merged': False, 'gh_merged': True},
            ],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=True)
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        self.assertEqual(seq2['steps'][0]['status'], 'merged')
        self.assertEqual(seq2['status'], 'complete')
        events = [e['event'] for e in seq2['audit_log']]
        self.assertNotIn('gate-mismatch-timeout', events)
        self.assertNotIn('sequence-paused', events)
        # The merge is recorded as gh-authoritative for the audit trail.
        merged = [e for e in seq2['audit_log'] if e['event'] == 'step-merged']
        self.assertTrue(
            any(e.get('gate_resolution') == 'gh-authoritative' for e in merged),
            'expected a gh-authoritative step-merged audit entry',
        )
        # No pause DM — the whole point of the fix.
        self.assertEqual([d for d in self.dms if 'paused' in d['subject']], [])

    def test_recent_mismatch_gh_merged_waits_for_timeout(self):
        # gh=merged but the mismatch is only 10 min old → do NOT trust gh yet;
        # give chain_events its grace window first (belt-and-suspenders fast
        # path intact — gh is only the tiebreaker at the >30-min stalemate).
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
                 'chain_merged': False, 'gh_merged': True},
            ],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=True)
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        self.assertEqual(seq2['steps'][0]['status'], 'dispatched')
        self.assertEqual(seq2['status'], 'active')

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


class TestPausedInvalidRealertSuppression(_AdvancerHarness):
    """Per advancer-suppress-paused-invalid-realert-001: a sequence already
    off the active path (paused/failed/archived) that stays schema-invalid
    must DM Larry AT MOST ONCE per distinct error signature — no hourly
    re-nag for a deliberately-parked file. A genuinely different error set
    fires one fresh DM. The active->pause transition alert is unaffected."""

    def _invalid_seq(self, seq_id='parked', status='paused'):
        seq = _make_sequence(seq_id=seq_id, status=status)
        # A depends_on pointing at a non-existent step → schema-invalid but
        # still parses cleanly as JSON (the off-path branch's precondition).
        seq['steps'].append(_make_step('beta', deps=['ghost']))
        return seq

    def _call(self, seq, errors):
        import logging
        path = (
            self.agents_root / 'blackboard' / 'build-sequences'
            / f'{seq["seq_id"]}.json'
        )
        self.bsa._handle_invalid_sequence(
            path, seq, errors, logging.getLogger('test-advancer'),
        )

    def _invalid_dms(self, seq_id):
        return [d for d in self.dms if d['subject'] == f'sequence-invalid:{seq_id}']

    def test_same_signature_dms_once(self):
        seq = self._invalid_seq()
        errors = ["step beta depends_on unknown step 'ghost'"]
        self._call(seq, errors)
        self._call(seq, errors)  # same parked seq, same errors, next tick
        self.assertEqual(len(self._invalid_dms('parked')), 1)

    def test_changed_signature_dms_again(self):
        seq = self._invalid_seq()
        self._call(seq, ["error A"])
        self._call(seq, ["error B — a genuinely different validation problem"])
        self.assertEqual(len(self._invalid_dms('parked')), 2)

    def test_signature_is_order_independent(self):
        seq = self._invalid_seq()
        self._call(seq, ["err one", "err two"])
        self._call(seq, ["err two", "err one"])  # same set, reordered
        self.assertEqual(len(self._invalid_dms('parked')), 1)

    def test_failed_and_archived_statuses_also_suppressed(self):
        for status in ('failed', 'archived'):
            with self.subTest(status=status):
                self.dms.clear()
                seq = self._invalid_seq(seq_id=f'parked-{status}', status=status)
                self._call(seq, ["boom"])
                self._call(seq, ["boom"])
                self.assertEqual(len(self._invalid_dms(f'parked-{status}')), 1)

    def test_sig_write_atomic_and_slash_safe(self):
        # A '/'-bearing seq_id must not escape the sig dir or create a subdir.
        seq = self._invalid_seq(seq_id='has/slash')
        self._call(seq, ["boom"])
        sig_dir = self.bsa.INVALID_ALERT_SIG_DIR
        entries = list(sig_dir.iterdir())
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].is_file())  # '/' did NOT create a subdir
        self.assertTrue(entries[0].name.startswith('has_slash'))
        self.assertEqual(list(sig_dir.glob('*.tmp')), [])  # no leftover temp
        data = json.loads(entries[0].read_text())
        self.assertIn('sig', data)
        self.assertIn('ts', data)

    def test_active_to_pause_transition_still_dms_and_audits(self):
        # The state-changing branch is UNCHANGED: still pauses, still writes
        # the sequence-paused-invalid audit entry, still DMs on transition.
        seq = self._invalid_seq(seq_id='fresh', status='active')
        self._call(seq, ["schema boom"])
        written = self._read_sequence('fresh')
        self.assertEqual(written['status'], 'paused')
        events = [e['event'] for e in written['audit_log']]
        self.assertIn('sequence-paused-invalid', events)
        self.assertEqual(len(self._invalid_dms('fresh')), 1)


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
    """Per spec § 5.4 failure mode 1: a terminal `review_escalate` (covers
    Mirror ESCALATE / EMERGENCY_HALT / revision-budget exhaustion) or
    `preflight_reject` in chain_events → pause + DM."""

    def test_review_escalate_pauses(self):
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
        self._patch_gates(
            failure='review_escalate: Mirror escalated the PR to Beacon')
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        self.assertEqual(seq2['status'], 'paused')
        alpha = next(s for s in seq2['steps'] if s['step_id'] == 'alpha')
        self.assertEqual(alpha['status'], 'failed')
        self.assertIn('escalate', (alpha['failure_reason'] or '').lower())
        paused_dms = [d for d in self.dms if 'paused' in d['subject']]
        self.assertEqual(len(paused_dms), 1)


class _FakeChainEvents:
    """Minimal stand-in for the Supabase client `chain_event_says_failed`
    queries. Supports the exact chain it builds —
    `.table().select().eq('task_id', ...).in_('event_type', [...]).limit().execute()`
    — and honors the two filters against an injected row list. `execute()`
    returns `SimpleNamespace(data=<filtered rows>)`, matching the real
    postgrest response shape the detector reads via `getattr(res, 'data', ...)`."""

    def __init__(self, rows):
        self._rows = rows
        self._task_id = None
        self._types = None

    def table(self, _name):
        return self

    def select(self, *_a, **_kw):
        return self

    def eq(self, col, val):
        if col == 'task_id':
            self._task_id = val
        return self

    def in_(self, col, vals):
        if col == 'event_type':
            self._types = list(vals)
        return self

    def limit(self, _n):
        return self

    def execute(self):
        rows = [
            r for r in self._rows
            if (self._task_id is None or r.get('task_id') == self._task_id)
            and (self._types is None or r.get('event_type') in self._types)
        ]
        return SimpleNamespace(data=rows)


class TestChainEventSaysFailed(_AdvancerHarness):
    """Unit tests for the realigned terminal-failure detector (spec § 2A / § 6).

    The prior detector keyed on event types the shipper NEVER emits
    (`mirror_revision_exhausted` / `mirror_emergency_halt` / `forge_reject`) and
    read a `reason` payload key that doesn't exist — so every non-merge terminal
    fell through to the 4h stall backstop. These pin it to the REAL emitted
    types + payload shapes, enforce the `KNOWN_EVENT_TYPES` subset invariant so
    it can't silently drift again, and assert `review_revision` is NOT failed."""

    def test_terminal_types_are_subset_of_known_event_types(self):
        # Enforcement (spec § 8): the queried set MUST be a subset of the types
        # the shipper actually produces, or the detector matches nothing.
        import chain_event_shipper
        queried = set(self.bsa.TERMINAL_FAILURE_EVENT_TYPES)
        known = set(chain_event_shipper.KNOWN_EVENT_TYPES)
        self.assertTrue(
            queried <= known,
            f'{queried - known} not in KNOWN_EVENT_TYPES — the shipper never '
            f'emits these, so the detector would silently never match (the '
            f'exact drift this test exists to catch).',
        )

    def test_preflight_reject_returns_reason_from_marker_type(self):
        client = _FakeChainEvents([
            {'event_type': 'preflight_reject', 'task_id': 'seq-x-step-a',
             'ts': '2026-06-24T00:00:00+00:00',
             'payload': {'agent': 'forge', 'task_id': 'seq-x-step-a',
                         'marker_type': 'reject', 'intent': 'reject'}},
        ])
        reason = self.bsa.chain_event_says_failed(client, 'seq-x-step-a')
        self.assertIsNotNone(reason)
        self.assertIn('preflight_reject', reason)
        self.assertIn('marker_type=reject', reason)
        # The payload carries NO `reason` key; the detector must not surface a
        # placeholder for a key it never reads.
        self.assertNotIn('None', reason)

    def test_preflight_reject_clarification_exhausted(self):
        client = _FakeChainEvents([
            {'event_type': 'preflight_reject', 'task_id': 'seq-x-step-a',
             'ts': '2026-06-24T00:00:00+00:00',
             'payload': {'marker_type': 'clarify',
                         'intent': 'clarification-exhausted'}},
        ])
        reason = self.bsa.chain_event_says_failed(client, 'seq-x-step-a')
        self.assertIn('clarification budget', reason.lower())

    def test_review_escalate_budget_exhausted(self):
        client = _FakeChainEvents([
            {'event_type': 'review_escalate', 'task_id': 'seq-x-step-a',
             'ts': '2026-06-24T00:00:00+00:00',
             'payload': {'verdict': 'escalate', 'budget_exhausted': True}},
        ])
        reason = self.bsa.chain_event_says_failed(client, 'seq-x-step-a')
        self.assertIn('review_escalate', reason)
        self.assertIn('budget', reason.lower())

    def test_review_revision_is_not_a_failure(self):
        # review_revision keeps the step in-flight (Forge fixes + Mirror
        # re-reviews); it is deliberately absent from the queried set, so the
        # `.in_` filter excludes it → None (no false-fail). This is the
        # Mirror-review focus: a revision must never pause a sequence.
        client = _FakeChainEvents([
            {'event_type': 'review_revision', 'task_id': 'seq-x-step-a',
             'ts': '2026-06-24T00:00:00+00:00',
             'payload': {'verdict': 'revision'}},
        ])
        self.assertIsNone(
            self.bsa.chain_event_says_failed(client, 'seq-x-step-a'))

    def test_no_terminal_event_returns_none(self):
        self.assertIsNone(
            self.bsa.chain_event_says_failed(_FakeChainEvents([]), 'seq-x-step-a'))

    def test_none_client_returns_none(self):
        self.assertIsNone(self.bsa.chain_event_says_failed(None, 'seq-x-step-a'))

    def test_empty_task_id_returns_none(self):
        self.assertIsNone(
            self.bsa.chain_event_says_failed(_FakeChainEvents([]), ''))


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
                    dispatched_at=_recent_iso(),
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

    def test_reconcile_near_miss_then_later_pr_corroborates(self):
        # Multi-PR ordering: an EARLIER merged PR token-matches the title
        # but its branch does NOT corroborate (the audit #9 near-miss), and
        # a LATER PR corroborates on both title and branch. The reconciler
        # must skip the near-miss, keep scanning, and advance via the LATER
        # PR — not strand the step and not stop at the first near-miss.
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
                    'number': 600,
                    # near-miss: 'api' token in title, NOT in branch.
                    'url': 'https://github.com/x/y/pull/600',
                    'title': 'feat(api): unrelated change',
                    'headRefName': 'feat/unrelated-change',
                    'mergedAt': '2026-05-30T00:50:00Z',
                },
                {
                    'number': 601,
                    # corroborator: 'api' token in BOTH title and branch.
                    'url': 'https://github.com/x/y/pull/601',
                    'title': 'fix(api): rotation rebuild',
                    'headRefName': 'pr601-rebase-step-api-001',
                    'mergedAt': '2026-05-30T01:00:00Z',
                },
            ],
        })
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == 'api')
        self.assertEqual(step['status'], 'merged')
        # It advanced via the CORROBORATING PR (601), not the near-miss (600).
        self.assertEqual(step['pr_url'], 'https://github.com/x/y/pull/601')

    def test_reconcile_no_match_no_op(self):
        seq = _make_sequence(
            steps=[
                _make_step(
                    'step-baz', status='dispatched',
                    pr_url=None,
                    dispatched_at=_recent_iso(),
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


class TestReconciliationConservativeGuard(_AdvancerHarness):
    """Spec §3.4 — the reconciliation pass must run (a) independent of the
    default-OFF advancer flag and (c) for `paused` sequences, while keeping
    the spec §6 conservative posture: a MERGED PR retires the in-flight
    step; an absent/unmatched (OPEN / UNKNOWN) PR keeps it."""

    _MERGED_PR = {
        'number': 77,
        'url': 'https://github.com/x/y/pull/77',
        'title': 'rebase under derivative task_id',
        'headRefName': 'forge/step-foo',
        'mergedAt': '2026-05-30T01:00:00Z',
    }

    def _patch_gh_pr_list(self, prs_by_repo):
        def _fake_list(repo, logger=None):
            return prs_by_repo.get(repo)
        self.bsa._gh_list_merged_prs = _fake_list

    def _dispatched_step_seq(self, status='active'):
        return _make_sequence(
            status=status,
            steps=[
                _make_step(
                    'step-foo', status='dispatched', pr_url=None,
                    dispatched_at='2026-05-30T00:00:00+00:00',
                ),
            ],
            current_steps=['step-foo'],
        )

    def test_flag_off_merged_step_reconciled(self):
        # Flag OFF, but a dispatched step's PR has merged → it must still be
        # retired (§3.4a). No heartbeat (gate closed) and no dispatch fire.
        os.environ['OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED'] = 'false'
        self._write_sequence(self._dispatched_step_seq())
        self._patch_gh_pr_list({'ourliberty-agent-core': [self._MERGED_PR]})
        rc = self.bsa.tick()
        self.assertEqual(rc, 0)
        seq = self._read_sequence('seq-001')
        step = next(s for s in seq['steps'] if s['step_id'] == 'step-foo')
        self.assertEqual(step['status'], 'merged')
        self.assertFalse(self.bsa.HEARTBEAT_FILE.exists())
        self.assertEqual(self.dispatched_envelopes, [])

    def test_flag_off_unmatched_step_kept(self):
        # Flag OFF and no merged PR matches (OPEN / UNKNOWN analog) → the
        # step stays dispatched. Conservative: never retire on absence.
        os.environ['OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED'] = 'false'
        self._write_sequence(self._dispatched_step_seq())
        self._patch_gh_pr_list({'ourliberty-agent-core': []})
        rc = self.bsa.tick()
        self.assertEqual(rc, 0)
        seq = self._read_sequence('seq-001')
        step = next(s for s in seq['steps'] if s['step_id'] == 'step-foo')
        self.assertEqual(step['status'], 'dispatched')
        self.assertFalse(self.bsa.HEARTBEAT_FILE.exists())

    def test_paused_sequence_merged_step_reconciled(self):
        # §3.4c: a `paused` sequence is never advanced, but its dispatched
        # steps must still be reconciled when their PR has merged.
        self._write_sequence(self._dispatched_step_seq(status='paused'))
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({'ourliberty-agent-core': [self._MERGED_PR]})
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        step = next(s for s in seq['steps'] if s['step_id'] == 'step-foo')
        self.assertEqual(step['status'], 'merged')
        # Paused → forward-dispatch leg skipped entirely.
        self.assertEqual(self.dispatched_envelopes, [])

    def test_paused_sequence_unmatched_step_kept(self):
        self._write_sequence(self._dispatched_step_seq(status='paused'))
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({'ourliberty-agent-core': []})
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        step = next(s for s in seq['steps'] if s['step_id'] == 'step-foo')
        self.assertEqual(step['status'], 'dispatched')


class TestStrandedDispatchEscalation(_AdvancerHarness):
    """Bug 2 backstop: a `dispatched` step that can NEVER reach a terminal gate
    (invalid target_repo, or stalled with no PR) is escalated — step `failed` +
    sequence `paused` + Larry DM — instead of silently hanging for hours (the
    2026-06-19 `ol-work` launch sat ~6h with no failure + no alert)."""

    def setUp(self):
        super().setUp()
        # Deterministic gates + a stand-in buildable-repo set (don't depend on
        # the live config). Reconcile finds no merged PR (the stranded steps
        # never produced one) — escalation is what must catch them; the lambda
        # also avoids a real `gh` subprocess for the invalid repo.
        self._patch_gates(chain_merged=False, gh_merged=None)
        # Accept the `state` kwarg the stall pre-check passes (state='open');
        # default mirrors the merged-PR reconcile behavior. Returns [] for
        # every state unless a test overrides — so the stall pre-check sees no
        # open PR and escalation proceeds as before.
        self.bsa._gh_list_merged_prs = (
            lambda repo, logger=None, state='merged': [])
        self.bsa._valid_target_repos = lambda: frozenset(
            {'ourliberty-agent-core', 'ourliberty-dashboard'})

    def _dispatched(self, *, repo, dispatched_at, pr_url=None, status='active'):
        seq = _make_sequence(
            status=status,
            steps=[_make_step('only-step', status='dispatched',
                              pr_url=pr_url, dispatched_at=dispatched_at)],
            current_steps=['only-step'],
        )
        seq['steps'][0]['target_repo'] = repo
        return seq

    def _stale_iso(self):
        return (datetime.now(timezone.utc) - timedelta(
            seconds=self.bsa.DISPATCH_STALL_TIMEOUT_SEC + 3600)).isoformat()

    def test_invalid_target_repo_escalates_within_one_tick(self):
        self._write_sequence(self._dispatched(
            repo='ol-work', dispatched_at=_recent_iso()))
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        step = seq['steps'][0]
        self.assertEqual(step['status'], 'failed')
        self.assertIn('ol-work', step['failure_reason'])
        self.assertIn('repo_paths', step['failure_reason'])
        self.assertEqual(seq['status'], 'paused')
        self.assertIsNone(step['current_actor'])
        subjects = [d['subject'] for d in self.dms]
        self.assertIn('sequence-stranded:seq-001:only-step', subjects)
        self.assertTrue(any(d['severity'] == 'critical' for d in self.dms))
        events = [e['event'] for e in seq['audit_log']]
        self.assertIn('step-stranded', events)
        self.assertIn('sequence-paused', events)

    def test_invalid_target_repo_escalates_even_with_flag_off(self):
        # A bad repo is a config error → escalation is FLAG-INDEPENDENT.
        os.environ['OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED'] = 'false'
        self._write_sequence(self._dispatched(
            repo='ol-work', dispatched_at=_recent_iso()))
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        self.assertEqual(seq['steps'][0]['status'], 'failed')
        self.assertEqual(seq['status'], 'paused')

    def test_stalled_valid_repo_escalates(self):
        self._write_sequence(self._dispatched(
            repo='ourliberty-agent-core', dispatched_at=self._stale_iso()))
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        step = seq['steps'][0]
        self.assertEqual(step['status'], 'failed')
        self.assertIn('stall', step['failure_reason'].lower())
        self.assertEqual(seq['status'], 'paused')
        self.assertTrue(any(d['severity'] == 'warning' for d in self.dms))

    def test_stall_reads_terminal_reject_attribution(self):
        # Spec § 2D / § 6: a stale `dispatched` step with no PR but a RECORDED
        # terminal reject in chain_events. The stall backstop must consult the
        # terminal result FIRST and write the correct attribution (a genuine
        # reject that never propagated into sequence state) rather than the
        # misleading "Forge may never have picked it up" stall guess.
        self.bsa.chain_event_says_failed = (
            lambda *_a, **_kw: 'preflight_reject: Forge rejected the spec at '
            'preflight (marker_type=reject)')
        self._write_sequence(self._dispatched(
            repo='ourliberty-agent-core', dispatched_at=self._stale_iso()))
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        step = seq['steps'][0]
        self.assertEqual(step['status'], 'failed')
        self.assertEqual(seq['status'], 'paused')
        reason = step['failure_reason'] or ''
        self.assertIn('terminal outcome', reason)
        self.assertIn('preflight_reject', reason)
        # The misleading stall guess must NOT be the attribution...
        self.assertNotIn('never picked it up', reason)
        # ...and the reason must NOT carry the stall-backstop signature, so the
        # reconcile auto-resume guard treats this as a genuine failure (not a
        # resumable strand).
        self.assertNotIn('stall backstop', reason)

    def test_stall_suppressed_when_flag_off(self):
        # Valid repo + forward-dispatch OFF → a waiting step is expected, not
        # stalled; the timing backstop does not fire.
        os.environ['OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED'] = 'false'
        self._write_sequence(self._dispatched(
            repo='ourliberty-agent-core', dispatched_at=self._stale_iso()))
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        self.assertEqual(seq['steps'][0]['status'], 'dispatched')
        self.assertEqual(seq['status'], 'active')

    def test_recent_valid_dispatch_not_escalated(self):
        self._write_sequence(self._dispatched(
            repo='ourliberty-agent-core', dispatched_at=_recent_iso()))
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        self.assertEqual(seq['steps'][0]['status'], 'dispatched')
        self.assertEqual(seq['status'], 'active')

    def test_unreadable_config_fails_open_on_invalid_repo(self):
        # Empty valid set (config unreadable) → invalid-repo check skipped so a
        # transient config miss never pauses a sequence. Recent dispatch → no
        # stall either, so the step is left untouched.
        self.bsa._valid_target_repos = lambda: frozenset()
        self._write_sequence(self._dispatched(
            repo='ol-work', dispatched_at=_recent_iso()))
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        self.assertEqual(seq['steps'][0]['status'], 'dispatched')
        self.assertEqual(seq['status'], 'active')

    def test_write_failure_does_not_resurrect_active_for_forward_dispatch(self):
        # If escalation's atomic write FAILS, the on-disk file stays `active`;
        # tick must NOT re-read it and run the forward-dispatch leg on the step
        # the pass just (in-memory) paused. Assert _process_active_sequence is
        # never invoked despite the write failure.
        def _boom(path, seq):
            raise OSError('disk full')
        self.bsa._atomic_write_sequence = _boom
        processed = []
        self.bsa._process_active_sequence = (
            lambda path, seq, *a, **k: processed.append(seq.get('seq_id')))
        self._write_sequence(self._dispatched(
            repo='ol-work', dispatched_at=_recent_iso()))
        self.bsa.tick()
        # The escalation DM still fired (wedge is visible)...
        self.assertTrue(any(
            d['subject'].startswith('sequence-stranded:') for d in self.dms))
        # ...and the forward-dispatch leg did NOT act on the paused-in-memory seq.
        self.assertEqual(processed, [])

    def test_dispatched_age_handles_z_suffix(self):
        # A 'Z'-suffixed timestamp must parse (not silently disable the stall
        # backstop on Python <3.11).
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
        age = self.bsa._dispatched_age_sec('2026-06-19T08:00:00Z', now)
        self.assertEqual(age, 4 * 3600)

    def _stub_open_prs(self, value):
        """Stub _gh_list_merged_prs so a state='open' query returns `value`
        (the open-PR pre-check's view) while the merged reconcile query keeps
        returning [] (no merged PR for the stranded step)."""
        def _fake(repo, logger=None, state='merged'):
            return value if state == 'open' else []
        self.bsa._gh_list_merged_prs = _fake

    def test_stalled_step_with_open_pr_is_not_stranded(self):
        # Criterion 1: a stalled `dispatched` step WITH a matching OPEN PR on
        # branch forge/<step_id> is recorded (pr_url populated) and left in
        # review — NOT failed, NOT paused, NOT DM'd.
        pr_url = 'https://github.com/Larry-Yatch/ourliberty-dashboard/pull/88'
        self._stub_open_prs([{
            'number': 88, 'url': pr_url, 'title': 'feat: live thread',
            'headRefName': 'forge/only-step', 'mergedAt': None,
        }])
        self._write_sequence(self._dispatched(
            repo='ourliberty-dashboard', dispatched_at=self._stale_iso()))
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        step = seq['steps'][0]
        # Step stays dispatched + in current_steps so the merge-gate advances it.
        self.assertEqual(step['status'], 'dispatched')
        self.assertEqual(step['pr_url'], pr_url)
        self.assertIn('only-step', seq['current_steps'])
        self.assertEqual(seq['status'], 'active')
        # No strand DM; no failure_reason set.
        self.assertFalse(any(
            d['subject'].startswith('sequence-stranded:') for d in self.dms))
        self.assertIsNone(step['failure_reason'])
        # Exactly the detection audit entry was appended (no strand/pause).
        events = [e['event'] for e in seq['audit_log']]
        self.assertIn('step-pr-detected', events)
        self.assertNotIn('step-stranded', events)
        self.assertNotIn('sequence-paused', events)
        detected = next(
            e for e in seq['audit_log'] if e['event'] == 'step-pr-detected')
        self.assertEqual(detected['actor'], 'advancer')
        self.assertEqual(detected['pr_url'], pr_url)

    def test_stalled_step_with_nonmatching_open_pr_still_escalates(self):
        # Criterion 2: a stalled step whose only open PR does NOT identify as it
        # (wrong branch, no token corroboration) escalates exactly as today.
        self._stub_open_prs([{
            'number': 5, 'url': 'https://github.com/x/y/pull/5',
            'title': 'unrelated change', 'headRefName': 'forge/something-else',
            'mergedAt': None,
        }])
        self._write_sequence(self._dispatched(
            repo='ourliberty-agent-core', dispatched_at=self._stale_iso()))
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        step = seq['steps'][0]
        self.assertEqual(step['status'], 'failed')
        self.assertIn('stall', step['failure_reason'].lower())
        self.assertEqual(seq['status'], 'paused')
        subjects = [d['subject'] for d in self.dms]
        self.assertIn('sequence-stranded:seq-001:only-step', subjects)

    def test_stall_open_pr_query_failure_defers_escalation(self):
        # Criterion 4: when the open-PR query can't be answered (None), the step
        # is NOT escalated this tick; a diagnosable WARN is logged; it's left
        # `dispatched` for retry next tick (a transient gh outage must not
        # resurrect the false strand this guard removes).
        self._stub_open_prs(None)
        self._write_sequence(self._dispatched(
            repo='ourliberty-agent-core', dispatched_at=self._stale_iso()))
        with self.assertLogs('build_sequence_advancer', level='WARNING') as cm:
            self.bsa.tick()
        seq = self._read_sequence('seq-001')
        step = seq['steps'][0]
        self.assertEqual(step['status'], 'dispatched')
        self.assertEqual(seq['status'], 'active')
        self.assertIsNone(step['failure_reason'])
        self.assertFalse(any(
            d['subject'].startswith('sequence-stranded:') for d in self.dms))
        events = [e['event'] for e in seq['audit_log']]
        self.assertNotIn('step-stranded', events)
        joined = '\n'.join(cm.output)
        self.assertIn('deferring escalation', joined)


class TestReviewStallRecovery(_AdvancerHarness):
    """Face 2 / Fix B (completeness-pr2 §1a): a WITH-PR step wedged in review
    past REVIEW_STALL_TIMEOUT_SEC is recover-or-routed — re-dispatch Mirror
    review (recover), else exactly one Larry alert; a healthy review is left
    alone. Closes the permanently-unmonitored state the no-PR stall guard's
    `and not step.get('pr_url')` short-circuit created (the #532 shape)."""

    def setUp(self):
        super().setUp()
        self.redispatch_calls = []
        self.bsa._gh_pr_open = lambda *_a, **_kw: True

        def _fake_redispatch(step_id, target_repo, pr_url, pr_title, logger):
            self.redispatch_calls.append({
                'step_id': step_id, 'target_repo': target_repo,
                'pr_url': pr_url, 'pr_title': pr_title,
            })
            return self._redispatch_result

        self._redispatch_result = True
        self.bsa._redispatch_review_for_wedged_step = _fake_redispatch

    def _stale_review_iso(self):
        return (datetime.now(timezone.utc) - timedelta(
            seconds=self.bsa.REVIEW_STALL_TIMEOUT_SEC + 3600)).isoformat()

    def _wedged_seq(self, *, dispatched_at=None,
                    pr_url='https://github.com/x/y/pull/7',
                    step_status='dispatched', status='active'):
        seq = _make_sequence(
            status=status,
            steps=[_make_step('only-step', status=step_status, pr_url=pr_url,
                              dispatched_at=dispatched_at
                              if dispatched_at is not None
                              else self._stale_review_iso())],
            current_steps=['only-step'],
        )
        return seq

    def _run_pass(self, seq):
        path = self._write_sequence(seq)
        now = datetime.now(timezone.utc)
        logger = self.bsa._setup_logging()
        result = self.bsa._recover_review_stalled_steps(seq, path, now, logger)
        return result, self._read_sequence(seq['seq_id'])

    def _events(self, seq):
        return [e['event'] for e in seq['audit_log']]

    def test_recovers_wedged_review(self):
        self._redispatch_result = True
        acted, seq = self._run_pass(self._wedged_seq())
        self.assertTrue(acted)
        self.assertIn('step-review-redispatched', self._events(seq))
        # Recovery re-primes the loop — the sequence stays ACTIVE, never paused.
        self.assertEqual(seq['status'], 'active')
        self.assertEqual(seq['steps'][0]['status'], 'dispatched')
        # No alert on a successful recovery.
        self.assertFalse(any(
            d['subject'].startswith('sequence-review-stall:') for d in self.dms))
        # Recovery targets the build task_id (== step_id) + its branch's PR.
        self.assertEqual(len(self.redispatch_calls), 1)
        self.assertEqual(self.redispatch_calls[0]['step_id'], 'only-step')
        self.assertEqual(self.redispatch_calls[0]['pr_url'],
                         'https://github.com/x/y/pull/7')

    def test_alerts_when_recovery_cannot_fire(self):
        self._redispatch_result = False
        acted, seq = self._run_pass(self._wedged_seq())
        self.assertTrue(acted)
        self.assertIn('step-review-stall-alerted', self._events(seq))
        self.assertNotIn('step-review-redispatched', self._events(seq))
        subjects = [d['subject'] for d in self.dms]
        self.assertIn('sequence-review-stall:seq-001:only-step', subjects)
        # Alert, not pause — the sequence is left active for a human to steer.
        self.assertEqual(seq['status'], 'active')

    def test_dedup_one_shot_per_dispatch_epoch(self):
        # First tick recovers + stamps; a second tick over the SAME dispatch
        # epoch is a no-op (no second re-dispatch, no duplicate audit entry).
        seq = self._wedged_seq()
        path = self._write_sequence(seq)
        now = datetime.now(timezone.utc)
        logger = self.bsa._setup_logging()
        self.assertTrue(
            self.bsa._recover_review_stalled_steps(seq, path, now, logger))
        second = self.bsa._recover_review_stalled_steps(seq, path, now, logger)
        self.assertFalse(second)
        stamps = [e for e in seq['audit_log']
                  if e['event'] == 'step-review-redispatched']
        self.assertEqual(len(stamps), 1)
        self.assertEqual(len(self.redispatch_calls), 1)

    def test_healthy_recent_review_untouched(self):
        # Under the 6h threshold → no probe, no recovery, no alert.
        probed = []
        self.bsa._gh_pr_open = lambda *a, **k: probed.append(1) or True
        acted, seq = self._run_pass(
            self._wedged_seq(dispatched_at=_recent_iso()))
        self.assertFalse(acted)
        self.assertEqual(probed, [])  # age check short-circuits before gh
        self.assertEqual(self.redispatch_calls, [])
        self.assertNotIn('step-review-redispatched', self._events(seq))
        self.assertNotIn('step-review-stall-alerted', self._events(seq))
        self.assertEqual(self.dms, [])

    def test_no_pr_step_skipped(self):
        # A no-PR wedge belongs to Fix A / the 4h no-PR strand guard, not here.
        acted, seq = self._run_pass(self._wedged_seq(pr_url=None))
        self.assertFalse(acted)
        self.assertEqual(self.redispatch_calls, [])
        self.assertEqual(self.dms, [])

    def test_merged_or_closed_pr_skipped(self):
        # PR resolved out-of-band → merge-gate / reconcile owns it; skip.
        self.bsa._gh_pr_open = lambda *a, **k: False
        acted, seq = self._run_pass(self._wedged_seq())
        self.assertFalse(acted)
        self.assertEqual(self.redispatch_calls, [])
        self.assertEqual(self.dms, [])
        self.assertNotIn('step-review-redispatched', self._events(seq))

    def test_gh_unverifiable_defers(self):
        # Unknown PR state must never trigger action (anti-noise: verify first).
        self.bsa._gh_pr_open = lambda *a, **k: None
        acted, seq = self._run_pass(self._wedged_seq())
        self.assertFalse(acted)
        self.assertEqual(self.redispatch_calls, [])
        self.assertEqual(self.dms, [])

    def test_non_dispatched_step_skipped(self):
        acted, seq = self._run_pass(self._wedged_seq(step_status='merged'))
        self.assertFalse(acted)
        self.assertEqual(self.redispatch_calls, [])

    def test_tick_wires_review_stall_pass(self):
        # Integration: the pass runs inside a real tick for an active sequence.
        self.bsa._valid_target_repos = lambda: frozenset(
            {'ourliberty-agent-core'})
        self._patch_gates(chain_merged=False, gh_merged=None)
        self.bsa._gh_pr_open = lambda *a, **k: True
        self._redispatch_result = True
        self._write_sequence(self._wedged_seq())
        self.bsa.tick()
        seq = self._read_sequence('seq-001')
        self.assertIn('step-review-redispatched', self._events(seq))
        self.assertEqual(len(self.redispatch_calls), 1)


class TestAlreadyMergedBridge(_AdvancerHarness):
    """no-PR already-merged backstop (2026-06-20 incident).

    A build that opens NO PR because its work already merged via another path
    strands until the 4h backstop. The merged PR's branch/title carry no
    step_id token, so the identity match can't bridge it — only the Forge
    build-outbox no-delta refusal (which names the PR) can. These tests cover
    that bridge plus the stranded-`failed` heal/resume (option c)."""

    STEP_ID = 'system-self-awareness-slice-1-state-log'
    PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/602'
    # Branch + title carry NONE of the step_id token — so _match_pr_for_step
    # cannot match; only the outbox bridge can. (Verbatim from the incident.)
    PR = {
        'number': 602,
        'url': PR_URL,
        'title': 'feat(system-awareness): work-in-flight State Log (Slice 1)',
        'headRefName': 'forge/system-self-awareness-the-standing-brain',
        'mergedAt': '2026-06-20T01:10:37Z',
    }
    RESULT = (
        '**This slice is already built and merged.** PR #602 merged to `main`; '
        '`git diff main..HEAD` is empty — no delta to commit. Already merged '
        'via #602.'
    )
    STALL_REASON = (
        'dispatched ~4h ago with no PR and no gate progress (exceeds the 4h '
        'stall backstop). Forge may never have picked it up.'
    )

    def _write_forge_build_outbox(self, step_id, result, phase='build'):
        d = self.agents_root / 'outboxes' / 'forge' / '.archive'
        d.mkdir(parents=True, exist_ok=True)
        (d / f'{step_id}.json').write_text(json.dumps({
            'agent': 'forge', 'phase': phase, 'task_id': step_id,
            'target_repo': 'ourliberty-agent-core', 'exit_code': 0,
            'duration_sec': 150.0, 'pr_url': None, 'result': result,
            'completed_at': '2026-06-20T01:27:00+00:00',
        }))

    def _patch_gh_pr_list(self, prs_by_repo):
        self.bsa._gh_list_merged_prs = (
            lambda repo, logger=None: prs_by_repo.get(repo)
        )

    def _failed_step(self, step_id, reason):
        step = _make_step(step_id, status='failed', pr_url=None)
        step['failure_reason'] = reason
        return step

    def _stall_pause_audit(self, step_id):
        # The audit trail the escalate pass writes when it strands + pauses.
        return [
            {'event': 'step-stranded', 'step_id': step_id,
             'reason': self.STALL_REASON},
            {'event': 'sequence-paused',
             'reason': f'Step `{step_id}` stranded: {self.STALL_REASON}'},
        ]

    def test_identity_match_cannot_bridge_this_pr(self):
        # The pre-existing matcher must MISS (branch/title lack the step_id
        # token) — the precondition that makes the bridge necessary.
        self.assertIsNone(
            self.bsa._match_pr_for_step(self.STEP_ID, None, [self.PR]),
        )

    def test_bridge_heals_stranded_dispatched_step(self):
        seq = _make_sequence(
            steps=[_make_step(self.STEP_ID, status='dispatched', pr_url=None,
                              dispatched_at=_recent_iso())],
            current_steps=[self.STEP_ID],
        )
        self._write_sequence(seq)
        self._write_forge_build_outbox(self.STEP_ID, self.RESULT)
        self._patch_gates()
        self._patch_gh_pr_list({'ourliberty-agent-core': [self.PR]})
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == self.STEP_ID)
        self.assertEqual(step['status'], 'merged')
        self.assertEqual(step['pr_url'], self.PR_URL)
        events = [(e['event'], e.get('actor')) for e in seq2['audit_log']]
        self.assertIn(('step-merged', 'advancer-reconcile'), events)

    def test_bridge_heals_stranded_failed_step_and_resumes_to_complete(self):
        seq = _make_sequence(
            status='paused',
            steps=[self._failed_step(self.STEP_ID, self.STALL_REASON)],
            current_steps=[self.STEP_ID],
            audit_log=self._stall_pause_audit(self.STEP_ID),
        )
        self._write_sequence(seq)
        self._write_forge_build_outbox(self.STEP_ID, self.RESULT)
        self._patch_gates()
        self._patch_gh_pr_list({'ourliberty-agent-core': [self.PR]})
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == self.STEP_ID)
        self.assertEqual(step['status'], 'merged')
        self.assertEqual(seq2['status'], 'complete')
        events = [(e['event'], e.get('actor')) for e in seq2['audit_log']]
        self.assertIn(('resumed', 'advancer-reconcile'), events)
        self.assertIn(('step-merged', 'advancer-reconcile'), events)
        self.assertTrue(any(e[0] == 'sequence-complete' for e in events))

    def test_does_not_resume_when_another_step_genuinely_failed(self):
        seq = _make_sequence(
            status='paused',
            steps=[
                self._failed_step(self.STEP_ID, self.STALL_REASON),
                self._failed_step('other-step',
                                  'Mirror REVIEW_ESCALATE: not buildable'),
            ],
            current_steps=[self.STEP_ID, 'other-step'],
            audit_log=self._stall_pause_audit(self.STEP_ID),
        )
        self._write_sequence(seq)
        self._write_forge_build_outbox(self.STEP_ID, self.RESULT)
        self._patch_gates()
        self._patch_gh_pr_list({'ourliberty-agent-core': [self.PR]})
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        stranded = next(s for s in seq2['steps'] if s['step_id'] == self.STEP_ID)
        genuine = next(s for s in seq2['steps'] if s['step_id'] == 'other-step')
        # The stranded step's merge is recorded, but a real failure remains so
        # the sequence stays paused for Larry and is NOT auto-resumed.
        self.assertEqual(stranded['status'], 'merged')
        self.assertEqual(genuine['status'], 'failed')
        self.assertEqual(seq2['status'], 'paused')
        self.assertNotIn('resumed', [e['event'] for e in seq2['audit_log']])

    def test_genuine_failure_without_stall_signature_is_left_untouched(self):
        # A `failed` step with no stall signature is a real failure — reconcile
        # must not resurrect it even when a merged PR is nameable.
        seq = _make_sequence(
            status='paused',
            steps=[self._failed_step(self.STEP_ID,
                                     'Mirror REVIEW_ESCALATE: design rejected')],
            current_steps=[self.STEP_ID],
        )
        self._write_sequence(seq)
        self._write_forge_build_outbox(self.STEP_ID, self.RESULT)
        self._patch_gates()
        self._patch_gh_pr_list({'ourliberty-agent-core': [self.PR]})
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == self.STEP_ID)
        self.assertEqual(step['status'], 'failed')
        self.assertEqual(seq2['status'], 'paused')

    def test_does_not_auto_resume_an_operator_pause(self):
        # A stranded-failed step whose work merged is recorded, but if the most
        # recent pause was an OPERATOR pause (event 'paused', not the stall
        # backstop's 'sequence-paused'), the sequence is NOT auto-resumed.
        seq = _make_sequence(
            status='paused',
            steps=[self._failed_step(self.STEP_ID, self.STALL_REASON)],
            current_steps=[self.STEP_ID],
            audit_log=[{'event': 'paused', 'actor': 'larry',
                        'reason': 'investigating something unrelated'}],
        )
        self._write_sequence(seq)
        self._write_forge_build_outbox(self.STEP_ID, self.RESULT)
        self._patch_gates()
        self._patch_gh_pr_list({'ourliberty-agent-core': [self.PR]})
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == self.STEP_ID)
        self.assertEqual(step['status'], 'merged')   # merge still recorded
        self.assertEqual(seq2['status'], 'paused')   # but NOT auto-resumed
        self.assertNotIn('resumed', [e['event'] for e in seq2['audit_log']])

    def test_non_clean_exit_build_outbox_is_not_bridged(self):
        # A build that exited non-zero is a genuine failure, not an honest
        # no-delta refusal — even if its result text names a merged PR, the
        # bridge must not flip the step.
        seq = _make_sequence(
            steps=[_make_step(self.STEP_ID, status='dispatched', pr_url=None,
                              dispatched_at=_recent_iso())],
            current_steps=[self.STEP_ID],
        )
        self._write_sequence(seq)
        d = self.agents_root / 'outboxes' / 'forge' / '.archive'
        d.mkdir(parents=True, exist_ok=True)
        (d / f'{self.STEP_ID}.json').write_text(json.dumps({
            'agent': 'forge', 'phase': 'build', 'task_id': self.STEP_ID,
            'target_repo': 'ourliberty-agent-core', 'exit_code': 1,
            'duration_sec': 150.0, 'pr_url': None, 'result': self.RESULT,
            'completed_at': '2026-06-20T01:27:00+00:00',
        }))
        self._patch_gates()
        self._patch_gh_pr_list({'ourliberty-agent-core': [self.PR]})
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == self.STEP_ID)
        self.assertEqual(step['status'], 'dispatched')

    def test_bridge_direct_verify_fallback_when_pr_not_in_recent_list(self):
        # PR older than the recent-merged lookback window — the bridge falls
        # back to a direct gh verify (ssh.gh_pr_merge_info).
        import sequence_shortcut_helpers as ssh_mod
        seq = _make_sequence(
            steps=[_make_step(self.STEP_ID, status='dispatched', pr_url=None,
                              dispatched_at=_recent_iso())],
            current_steps=[self.STEP_ID],
        )
        self._write_sequence(seq)
        self._write_forge_build_outbox(self.STEP_ID, self.RESULT)
        self._patch_gates()
        self._patch_gh_pr_list({'ourliberty-agent-core': []})  # #602 not listed
        with patch.object(
            ssh_mod, 'gh_pr_merge_info',
            lambda coords, num, timeout=10: (self.PR_URL, self.PR['mergedAt']),
        ):
            self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == self.STEP_ID)
        self.assertEqual(step['status'], 'merged')
        self.assertEqual(step['pr_url'], self.PR_URL)

    def test_no_outbox_no_bridge(self):
        # No Forge build outbox on disk → bridge finds nothing → step stays
        # dispatched (the bridge never invents a PR).
        seq = _make_sequence(
            steps=[_make_step(self.STEP_ID, status='dispatched', pr_url=None,
                              dispatched_at=_recent_iso())],
            current_steps=[self.STEP_ID],
        )
        self._write_sequence(seq)
        self._patch_gates()
        self._patch_gh_pr_list({'ourliberty-agent-core': [self.PR]})
        self.bsa.tick()
        seq2 = self._read_sequence('seq-001')
        step = next(s for s in seq2['steps'] if s['step_id'] == self.STEP_ID)
        self.assertEqual(step['status'], 'dispatched')


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


def _make_launch_sequence(phase_id, project_id='proj-x', status='active'):
    """A board-Launch build sequence: seq_id `launch-<phase_id>`, a single step
    whose step_id IS the phase_id, plus the `authored-by-launch-drain` audit entry
    the drain writes (carries phase_id + project_id, so the done-stamp/closeout can
    resolve the phase)."""
    seq_id = f'launch-{phase_id}'
    seq = _make_sequence(
        seq_id=seq_id,
        status=status,
        steps=[_make_step(phase_id, status='pending')],
        current_steps=[],
        audit_log=[{
            'ts': '2026-06-20T00:00:00+00:00',
            'event': 'authored-by-launch-drain',
            'actor': 'launch_queue_drain',
            'phase_id': phase_id,
            'project_id': project_id,
        }],
    )
    seq['created_by'] = 'launch_queue_drain'
    return seq


class TestPreDispatchAlreadyMergedLaunch(_AdvancerHarness):
    """#609 follow-up: the advancer catches a still-PENDING launch phase whose
    deliverables ALREADY merged elsewhere and reconciles it to done INSTEAD of
    dispatching a redundant Forge build (the 2026-06-20 cross-identity incident
    that stranded 4h / ~$1.5). Conservative — only an exact convention-branch
    match (own `forge/<phase|seq>` or a #609-claimed sibling's branch) against a
    gh-MERGED PR advances a phase; everything else dispatches normally."""

    def _patch_gh_pr_list(self, prs_by_repo):
        def _fake_list(repo, logger=None):
            return prs_by_repo.get(repo)
        self.bsa._gh_list_merged_prs = _fake_list

    def _write_claim(self, claimed_task_id, envelope_task_id):
        """Append one #609 deliverable claim (recent ts) to the tmp blackboard
        ledger the advancer derives from BLACKBOARD_DIR."""
        ledger = self.agents_root / 'blackboard' / 'deliverable-claims.jsonl'
        rec = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'claimed_task_id': claimed_task_id,
            'envelope_task_id': envelope_task_id,
            'agent': 'forge',
        }
        with ledger.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec) + '\n')

    def _completion_dms(self, seq_id):
        return [d for d in self.dms
                if d['subject'] == f'sequence-complete:{seq_id}']

    @staticmethod
    def _recent_merged_at():
        # tick() runs at real wall-clock now; the pre-dispatch match only counts
        # a PR merged within PREDISPATCH_MERGE_LOOKBACK_SEC (7d), so the matching
        # PRs must be recent RELATIVE TO NOW (a fixed past date would make these
        # tests rot once it falls outside the window).
        return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    def test_own_branch_match_skips_build_and_completes(self):
        seq = _make_launch_sequence('slice-1-state-log')
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({'ourliberty-agent-core': [{
            'number': 602,
            'url': 'https://github.com/x/y/pull/602',
            'title': 'unrelated framing',
            'headRefName': 'forge/slice-1-state-log',
            'mergedAt': self._recent_merged_at(),
        }]})
        self.bsa.tick()
        seq2 = self._read_sequence('launch-slice-1-state-log')
        step = seq2['steps'][0]
        self.assertEqual(step['status'], 'merged')
        self.assertEqual(step['pr_url'], 'https://github.com/x/y/pull/602')
        self.assertEqual(seq2['status'], 'complete')
        # No redundant build dispatched.
        self.assertEqual(self.dispatched_envelopes, [])
        # Completion signaled exactly once (done + closeout + DM), and the
        # single-winner marker is on the sequence so a re-tick can't double-fire.
        self.assertEqual(len(self._completion_dms('launch-slice-1-state-log')), 1)
        events = [e.get('event') for e in seq2['audit_log']]
        self.assertIn('sequence-complete-signaled', events)
        self.assertIn(('step-merged', 'advancer-already-merged-predispatch'),
                      [(e.get('event'), e.get('actor')) for e in seq2['audit_log']])

    def test_seq_branch_match(self):
        seq = _make_launch_sequence('phase-a')
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        # PR merged under the SEQUENCE branch convention (forge/<seq_id>).
        self._patch_gh_pr_list({'ourliberty-agent-core': [{
            'number': 7,
            'url': 'https://github.com/x/y/pull/7',
            'title': 'x',
            'headRefName': 'forge/launch-phase-a',
            'mergedAt': self._recent_merged_at(),
        }]})
        self.bsa.tick()
        seq2 = self._read_sequence('launch-phase-a')
        self.assertEqual(seq2['steps'][0]['status'], 'merged')
        self.assertEqual(seq2['status'], 'complete')
        self.assertEqual(self.dispatched_envelopes, [])

    def test_claim_corroboration_cross_identity(self):
        # The 2026-06-20 shape: a sibling built under its OWN envelope task_id
        # (`...the-standing-brain`) but its marker CLAIMED this phase's id; the
        # sibling's PR merged under `forge/<sibling-envelope>`. Branch-tier misses
        # (no forge/<phase|seq>), but claim + sibling-branch corroboration hits.
        seq = _make_launch_sequence('slice-1-state-log')
        self._write_sequence(seq)
        self._write_claim(
            claimed_task_id='slice-1-state-log',
            envelope_task_id='the-standing-brain',
        )
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({'ourliberty-agent-core': [{
            'number': 602,
            'url': 'https://github.com/x/y/pull/602',
            'title': 'feat(self-awareness): the standing brain',
            'headRefName': 'forge/the-standing-brain',
            'mergedAt': self._recent_merged_at(),
        }]})
        self.bsa.tick()
        seq2 = self._read_sequence('launch-slice-1-state-log')
        self.assertEqual(seq2['steps'][0]['status'], 'merged')
        self.assertEqual(seq2['steps'][0]['pr_url'],
                         'https://github.com/x/y/pull/602')
        self.assertEqual(seq2['status'], 'complete')
        self.assertEqual(self.dispatched_envelopes, [])

    def test_no_match_dispatches_normally(self):
        # No convention-branch match and no claim → the launch must build as
        # usual (the guard never blocks a legitimate launch).
        seq = _make_launch_sequence('phase-b')
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({'ourliberty-agent-core': [{
            'number': 9,
            'url': 'https://github.com/x/y/pull/9',
            'title': 'totally unrelated work',
            'headRefName': 'forge/something-else',
            'mergedAt': '2026-06-20T01:00:00Z',
        }]})
        self.bsa.tick()
        seq2 = self._read_sequence('launch-phase-b')
        self.assertEqual(seq2['steps'][0]['status'], 'dispatched')
        self.assertEqual(len(self.dispatched_envelopes), 1)
        self.assertEqual(self._completion_dms('launch-phase-b'), [])

    def test_title_only_does_not_match(self):
        # A merged PR whose TITLE contains the phase id but whose BRANCH does not
        # carry the convention must NOT mark the phase done (no title-substring
        # guesses — the conservative contract). It dispatches normally.
        seq = _make_launch_sequence('api')
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({'ourliberty-agent-core': [{
            'number': 11,
            'url': 'https://github.com/x/y/pull/11',
            'title': 'feat(api): unrelated change mentioning api',
            'headRefName': 'forge/unrelated',
            'mergedAt': '2026-06-20T01:00:00Z',
        }]})
        self.bsa.tick()
        seq2 = self._read_sequence('launch-api')
        self.assertEqual(seq2['steps'][0]['status'], 'dispatched')
        self.assertEqual(len(self.dispatched_envelopes), 1)

    def test_gh_failure_dispatches(self):
        # gh unavailable (None) → cannot confirm an already-merged delivery, so
        # fail-open: dispatch normally rather than block.
        seq = _make_launch_sequence('phase-c')
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({})  # repo missing → None
        self.bsa.tick()
        seq2 = self._read_sequence('launch-phase-c')
        self.assertEqual(seq2['steps'][0]['status'], 'dispatched')
        self.assertEqual(len(self.dispatched_envelopes), 1)

    def test_stale_branch_match_is_not_recent_so_dispatches(self):
        # A merged PR on the phase's own convention branch but merged LONG ago
        # (outside PREDISPATCH_MERGE_LOOKBACK_SEC) is NOT evidence the fresh
        # re-launch is redundant — it must build, not be falsely marked done.
        seq = _make_launch_sequence('state-log')
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        stale = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        self._patch_gh_pr_list({'ourliberty-agent-core': [{
            'number': 1,
            'url': 'https://github.com/x/y/pull/1',
            'title': 'old build of an earlier run',
            'headRefName': 'forge/state-log',
            'mergedAt': stale,
        }]})
        self.bsa.tick()
        seq2 = self._read_sequence('launch-state-log')
        self.assertEqual(seq2['steps'][0]['status'], 'dispatched')
        self.assertEqual(len(self.dispatched_envelopes), 1)
        self.assertEqual(self._completion_dms('launch-state-log'), [])

    def test_non_launch_sequence_untouched(self):
        # An ordinary (non-launch) sequence with a pending step is NEVER touched
        # by the pre-dispatch launch reconcile, even if a branch happens to match.
        seq = _make_sequence(
            seq_id='seq-ordinary',
            steps=[_make_step('phase-a', status='pending')],
        )
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({'ourliberty-agent-core': [{
            'number': 5,
            'url': 'https://github.com/x/y/pull/5',
            'title': 'x',
            'headRefName': 'forge/phase-a',
            'mergedAt': '2026-06-20T01:00:00Z',
        }]})
        self.bsa.tick()
        seq2 = self._read_sequence('seq-ordinary')
        # Dispatched normally; not marked merged by the launch-only pass.
        self.assertEqual(seq2['steps'][0]['status'], 'dispatched')
        self.assertEqual(len(self.dispatched_envelopes), 1)

    def test_idempotent_no_double_signal(self):
        seq = _make_launch_sequence('phase-d')
        self._write_sequence(seq)
        self._patch_gates(chain_merged=False, gh_merged=False)
        self._patch_gh_pr_list({'ourliberty-agent-core': [{
            'number': 12,
            'url': 'https://github.com/x/y/pull/12',
            'title': 'x',
            'headRefName': 'forge/phase-d',
            'mergedAt': self._recent_merged_at(),
        }]})
        self.bsa.tick()
        self.bsa.tick()  # re-tick: completed sequence is skipped; no double DM
        self.assertEqual(len(self._completion_dms('launch-phase-d')), 1)


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


class TestKickoffInboxDrain(_AdvancerHarness):
    """The chat-path routing-gap fix: `approve sequence <id>` via the Telegram
    bot lands a kickoff APPROVAL_REQUEST in the advancer's OWN inbox
    (~/agents/inboxes/build_sequence_advancer/). The drain consumes it and
    transitions the named sequence pending → active so its first step
    dispatches — the transition the file-outbox path already does, but which
    the in-process chat bot never reached."""

    def _inbox_dir(self):
        return self.agents_root / 'inboxes' / 'build_sequence_advancer'

    def _write_kickoff_envelope(self, seq_id, *, task_id=None, prompt='__default__',
                                name=None, extra=None):
        d = self._inbox_dir()
        d.mkdir(parents=True, exist_ok=True)
        env = {
            'task_id': task_id if task_id is not None else f'kickoff-{seq_id}',
            'target_agent': 'build_sequence_advancer',
            'summary': f'Kick off {seq_id}',
            'source': 'beacon',
        }
        if prompt == '__default__':
            env['prompt'] = f'kickoff {seq_id}'
        elif prompt is not None:
            env['prompt'] = prompt
        if extra:
            env.update(extra)
        path = d / (name or f'{env["task_id"]}.json')
        path.write_text(json.dumps(env, indent=2))
        return path

    def _drain(self):
        return self.bsa._drain_kickoff_inbox(self.bsa._setup_logging())

    def test_pending_sequence_kicked_active_and_envelope_consumed(self):
        self._write_sequence(_make_sequence('seq-xii', status='pending'))
        env_path = self._write_kickoff_envelope('seq-xii')
        kicked = self._drain()
        self.assertEqual(kicked, 1)
        seq = self._read_sequence('seq-xii')
        self.assertEqual(seq['status'], 'active')
        events = [e['event'] for e in seq['audit_log']]
        self.assertIn('kickoff-acknowledged', events)
        self.assertFalse(env_path.exists(), 'envelope should be consumed')

    def test_tick_dispatches_first_step_after_chat_kickoff(self):
        # End-to-end: a pending sequence + an inbox kickoff → one tick both
        # activates it AND dispatches its first (dep-free) step.
        self._patch_gates()
        self._write_sequence(_make_sequence(
            'seq-xii', status='pending', steps=[_make_step('only')],
        ))
        self._write_kickoff_envelope('seq-xii')
        self.bsa.tick()
        seq = self._read_sequence('seq-xii')
        self.assertEqual(seq['status'], 'active')
        dispatched_steps = [e['task_id'] for e in self.dispatched_envelopes]
        self.assertTrue(dispatched_steps, 'first step should dispatch this tick')

    def test_task_id_fallback_when_prompt_absent(self):
        self._write_sequence(_make_sequence('seq-xii', status='pending'))
        # No prompt field; seq_id derived from `kickoff-<seq-id>` task_id.
        self._write_kickoff_envelope('seq-xii', prompt=None)
        self.assertEqual(self._drain(), 1)
        self.assertEqual(self._read_sequence('seq-xii')['status'], 'active')

    def test_already_active_is_idempotent_noop(self):
        seq = _make_sequence('seq-xii', status='active',
                             audit_log=[{'event': 'kickoff-acknowledged'}])
        self._write_sequence(seq)
        env_path = self._write_kickoff_envelope('seq-xii')
        kicked = self._drain()
        self.assertEqual(kicked, 0)
        after = self._read_sequence('seq-xii')
        # No SECOND kickoff-acknowledged entry appended.
        self.assertEqual(
            sum(1 for e in after['audit_log']
                if e.get('event') == 'kickoff-acknowledged'),
            1,
        )
        self.assertFalse(env_path.exists(), 'stale kickoff still consumed')

    def test_missing_sequence_dms_and_drops_envelope(self):
        env_path = self._write_kickoff_envelope('seq-ghost')
        kicked = self._drain()
        self.assertEqual(kicked, 0)
        self.assertFalse(env_path.exists())
        self.assertTrue(
            any('kickoff-inbox-seq-missing' in dm['subject'] for dm in self.dms),
            f'expected a missing-sequence DM, got {[d["subject"] for d in self.dms]}',
        )

    def test_non_kickoff_envelope_left_in_place(self):
        # An envelope that isn't a kickoff (no parseable seq_id) is NOT deleted
        # — we don't consume mail we can't act on.
        env_path = self._write_kickoff_envelope(
            'seq-xii', task_id='some-other-task', prompt='do something else',
        )
        self.assertEqual(self._drain(), 0)
        self.assertTrue(env_path.exists(), 'unknown envelope must be preserved')

    def test_invalid_dag_dms_and_does_not_activate(self):
        bad = _make_sequence('seq-bad', status='pending')
        bad['steps'] = []  # empty steps → validate_dag fails
        self._write_sequence(bad)
        env_path = self._write_kickoff_envelope('seq-bad')
        self.assertEqual(self._drain(), 0)
        self.assertEqual(self._read_sequence('seq-bad')['status'], 'pending')
        self.assertFalse(env_path.exists())
        self.assertTrue(
            any('kickoff-inbox-seq-invalid' in dm['subject'] for dm in self.dms),
        )

    def test_gated_off_leaves_envelope_queued(self):
        # Activation flag off → drain does not run in tick(); the envelope
        # persists for when the advancer is enabled.
        os.environ['OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED'] = 'false'
        self._write_sequence(_make_sequence('seq-xii', status='pending'))
        env_path = self._write_kickoff_envelope('seq-xii')
        self.bsa.tick()
        self.assertEqual(self._read_sequence('seq-xii')['status'], 'pending')
        self.assertTrue(env_path.exists(), 'envelope queued until advancer on')

    def test_no_inbox_dir_is_safe_noop(self):
        # Fresh systems have no advancer inbox until the first dispatch_approved.
        self.assertFalse(self._inbox_dir().exists())
        self.assertEqual(self._drain(), 0)

    def test_traversal_task_id_is_rejected(self):
        # The task_id fallback must not smuggle a path-separator seq_id past the
        # prompt regex's charset — else the drain could write `active` outside
        # the build-sequences dir. A traversal task_id parses to no seq_id, so
        # it's treated as a non-kickoff envelope (left in place, not acted on).
        outside = self.agents_root / 'inboxes' / 'beacon' / 'evil.json'
        outside.write_text(json.dumps(_make_sequence('evil', status='pending')))
        env_path = self._write_kickoff_envelope(
            'x', task_id='kickoff-../beacon/evil', prompt=None,
            name='kickoff-traversal.json',
        )
        self.assertEqual(self._drain(), 0)
        # The out-of-dir sequence was NOT flipped to active.
        self.assertEqual(json.loads(outside.read_text())['status'], 'pending')
        self.assertTrue(env_path.exists(), 'unparseable-seq envelope left in place')


if __name__ == '__main__':
    unittest.main()
