#!/usr/bin/env python3
"""Tests for seed_pulse_check_heartbeats (one-time liveness bootstrap).

The contract the seed script must honour:

  - It NEVER invokes a check in a side-effecting mode. Every check it runs is
    invoked with exactly ``['--dry-run']`` (the proven POST/DM/config-free path)
    via run_check. We prove this by injecting a spy run_check and asserting the
    argv of every call.
  - A check it cannot prove safe to run (no --dry-run => argparse SystemExit) is
    baseline-seeded, not run, and never gets a fake heartbeat.
  - A check that fails ONLY because a required env var is unset is classified
    "unvalidated: env absent" — no heartbeat, no pulse-check-failed alert. A
    non-env failure still surfaces as a failure.
  - Event-driven checks are skipped (the watcher does not track their staleness).
  - --plan / execute=False writes nothing at all.

No real pulse_check_<id> module is imported and no real alert queue is touched:
import_fn and run_check_fn are injected.

Run:
    cd ~/agent-core && python3 -m unittest \
        scripts.tests.test_seed_pulse_check_heartbeats
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_pulse_check_staleness as w  # noqa: E402
import seed_pulse_check_heartbeats as seed  # noqa: E402


NOW = 1_750_000_000.0


def _full_cadence() -> dict:
    """A cadence config covering every canonical check; 'ev' event-driven.

    'ev' is a synthetic event-driven check (the shape the retired Check VII
    had) patched into CANONICAL_CHECKS by _patch_ev_canonical so the seed's
    event-driven skip path keeps real coverage.
    """
    cfg = {cid: {'cadence_hours': 168, 'grace_hours': 36}
           for cid in w.CANONICAL_CHECKS if cid != 'ev'}
    cfg['ev'] = {'event_driven': True}
    return cfg


def _patch_ev_canonical(test_case: unittest.TestCase) -> None:
    """Extend the canonical set with the synthetic event-driven 'ev' check for
    the duration of a test (seed_all iterates watcher.CANONICAL_CHECKS)."""
    patcher = mock.patch.object(
        w, 'CANONICAL_CHECKS', list(w.CANONICAL_CHECKS) + ['ev'])
    patcher.start()
    test_case.addCleanup(patcher.stop)


class _SpyModule:
    """Stand-in pulse_check_<id> module with a recordable main()."""

    def __init__(self, rc=0, raises=None):
        self.rc = rc
        self.raises = raises
        self.calls = []

    def main(self, argv=None):
        self.calls.append(argv)
        if self.raises is not None:
            raise self.raises
        return self.rc

    def log(self, msg, level='INFO'):
        pass


class SeedSafetyTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.bb = Path(self._td.name) / 'blackboard'
        self.bb.mkdir(parents=True)
        _patch_ev_canonical(self)

    def tearDown(self):
        self._td.cleanup()

    def test_every_run_uses_dry_run_only(self):
        seen_argv = []

        def spy_run_check(check_id, main_fn, *, argv=None, log_fn=None):
            seen_argv.append((check_id, argv))
            return 0  # pretend clean

        modules = {f'pulse_check_{c}': _SpyModule() for c in w.CANONICAL_CHECKS}

        rows = seed.seed_all(
            _full_cadence(), self.bb, NOW,
            run_check_fn=spy_run_check,
            import_fn=lambda name: modules[name],
        )
        # ev (event-driven) is skipped; every OTHER check is invoked, and ONLY
        # ever with --dry-run — the side-effect-free contract.
        self.assertTrue(seen_argv)
        for check_id, argv in seen_argv:
            self.assertEqual(argv, ['--dry-run'],
                             f'{check_id} invoked with {argv!r}, not --dry-run')
        self.assertNotIn('ev', {cid for cid, _ in seen_argv})
        # All non-event-driven checks ran cleanly.
        ran = {r['check'] for r in rows if r['action'] == 'ran'}
        self.assertEqual(ran, set(w.CANONICAL_CHECKS) - {'ev'})

    def test_no_dry_run_check_is_baseline_seeded_not_run(self):
        # A check whose main rejects --dry-run (argparse SystemExit) must be
        # baseline-seeded, never run, and must NOT receive a fake heartbeat.
        def spy_run_check(check_id, main_fn, *, argv=None, log_fn=None):
            raise SystemExit(2)  # argparse: unrecognized --dry-run

        modules = {f'pulse_check_{c}': _SpyModule() for c in w.CANONICAL_CHECKS}
        rows = seed.seed_all(
            _full_cadence(), self.bb, NOW,
            run_check_fn=spy_run_check,
            import_fn=lambda name: modules[name],
        )
        seeded = {r['check'] for r in rows if r['action'] == 'baseline-seeded'}
        self.assertEqual(seeded, set(w.CANONICAL_CHECKS) - {'ev'})
        # No heartbeat files were written by the seed itself.
        self.assertEqual(list(self.bb.glob('*.heartbeat')), [])
        # The baseline now records monitoring_since for the seeded checks.
        baseline = w.load_baseline(self.bb)
        for cid in set(w.CANONICAL_CHECKS) - {'ev'}:
            self.assertEqual(baseline[cid], NOW)

    def test_nonzero_run_surfaces_and_does_not_baseline_seed(self):
        # A check that runs but exits non-zero (ix/x style) is "ran" — run_check
        # surfaces the failure; the seed must NOT also baseline-seed it (that
        # would suppress the very escalation we want).
        def spy_run_check(check_id, main_fn, *, argv=None, log_fn=None):
            return 1

        modules = {f'pulse_check_{c}': _SpyModule(rc=1)
                   for c in w.CANONICAL_CHECKS}
        rows = seed.seed_all(
            _full_cadence(), self.bb, NOW,
            run_check_fn=spy_run_check,
            import_fn=lambda name: modules[name],
        )
        for r in rows:
            if r['check'] == 'ev':
                continue
            self.assertEqual(r['action'], 'ran')
            self.assertEqual(r['rc'], 1)
        # Nothing baseline-seeded => baseline file untouched.
        self.assertEqual(w.load_baseline(self.bb), {})

    def test_plan_writes_nothing(self):
        called = []

        def spy_run_check(*a, **k):
            called.append(a)
            return 0

        rows = seed.seed_all(
            _full_cadence(), self.bb, NOW,
            execute=False,
            run_check_fn=spy_run_check,
            import_fn=lambda name: (_ for _ in ()).throw(
                AssertionError('plan must not import a check')),
        )
        self.assertEqual(called, [], 'plan mode must not run any check')
        self.assertEqual(list(self.bb.glob('*')), [],
                         'plan mode must not write any file')
        self.assertTrue(all(r['action'] in ('would-run', 'skipped')
                            for r in rows))

    def test_event_driven_check_is_skipped(self):
        rows = seed.seed_all(
            _full_cadence(), self.bb, NOW,
            run_check_fn=lambda *a, **k: 0,
            import_fn=lambda name: _SpyModule(),
        )
        ev = next(r for r in rows if r['check'] == 'ev')
        self.assertEqual(ev['action'], 'skipped')
        self.assertIn('event-driven', ev['detail'])


class _FaithfulRunCheck:
    """Emulates pulse_check_heartbeat.run_check closely enough to test the seed's
    env-awareness: it actually calls main_fn, re-raises SystemExit untouched (so
    the seed's _EnvAbsent sentinel can propagate), turns any other exception into
    a recorded failure alert, and records a heartbeat on a clean rc==0 exit.
    """

    def __init__(self):
        self.alerts = []
        self.heartbeats = []

    def __call__(self, check_id, main_fn, *, argv=None, log_fn=None):
        try:
            rc = main_fn(argv)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 — mirrors run_check's guard
            self.alerts.append((check_id, f'{type(exc).__name__}: {exc}'))
            return 1
        if rc == 0:
            self.heartbeats.append(check_id)
        else:
            self.alerts.append((check_id, f'exited {rc}'))
        return rc


_ENV_GUARD_MSG = (
    'SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — cannot run Check III.'
)


class EnvAwarenessTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.bb = Path(self._td.name) / 'blackboard'
        self.bb.mkdir(parents=True)
        _patch_ev_canonical(self)

    def tearDown(self):
        self._td.cleanup()

    def test_env_absent_is_unvalidated_no_alert_no_heartbeat(self):
        # A check that fails solely because required env is unset must be
        # neutral: classified 'unvalidated', no heartbeat, no pulse-check-failed,
        # and no baseline write.
        runner = _FaithfulRunCheck()
        modules = {
            f'pulse_check_{c}': _SpyModule(raises=RuntimeError(_ENV_GUARD_MSG))
            for c in w.CANONICAL_CHECKS
        }
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SUPABASE_URL', None)
            os.environ.pop('SUPABASE_SERVICE_ROLE_KEY', None)
            rows = seed.seed_all(
                _full_cadence(), self.bb, NOW,
                run_check_fn=runner,
                import_fn=lambda name: modules[name],
            )
        non_ev = set(w.CANONICAL_CHECKS) - {'ev'}
        unvalidated = {r['check'] for r in rows if r['action'] == 'unvalidated'}
        self.assertEqual(unvalidated, non_ev)
        for r in rows:
            if r['check'] in non_ev:
                self.assertIsNone(r['rc'])
                self.assertFalse(r['baseline_changed'])
                self.assertIn('env absent', r['detail'])
        self.assertEqual(runner.alerts, [], 'env-absent must emit no alert')
        self.assertEqual(runner.heartbeats, [], 'env-absent writes no heartbeat')
        self.assertEqual(list(self.bb.glob('*.heartbeat')), [])
        self.assertEqual(w.load_baseline(self.bb), {})

    def test_nonenv_runtimeerror_still_surfaces_as_failure(self):
        # A genuine (non-env) failure must still surface exactly as today: run,
        # rc==1, pulse-check-failed alert emitted — NOT swallowed as unvalidated.
        runner = _FaithfulRunCheck()
        modules = {
            f'pulse_check_{c}': _SpyModule(raises=RuntimeError('boom'))
            for c in w.CANONICAL_CHECKS
        }
        rows = seed.seed_all(
            _full_cadence(), self.bb, NOW,
            run_check_fn=runner,
            import_fn=lambda name: modules[name],
        )
        non_ev = set(w.CANONICAL_CHECKS) - {'ev'}
        for r in rows:
            if r['check'] in non_ev:
                self.assertEqual(r['action'], 'ran')
                self.assertEqual(r['rc'], 1)
        self.assertEqual({c for c, _ in runner.alerts}, non_ev)
        self.assertEqual(runner.heartbeats, [])

    def test_missing_message_but_env_present_is_not_unvalidated(self):
        # A RuntimeError that mentions "missing" while the named vars ARE set is
        # a real failure, not env-absence — the os.environ check guards this.
        runner = _FaithfulRunCheck()
        modules = {
            f'pulse_check_{c}': _SpyModule(raises=RuntimeError(_ENV_GUARD_MSG))
            for c in w.CANONICAL_CHECKS
        }
        with mock.patch.dict(
            os.environ,
            {'SUPABASE_URL': 'https://x', 'SUPABASE_SERVICE_ROLE_KEY': 'k'},
        ):
            rows = seed.seed_all(
                _full_cadence(), self.bb, NOW,
                run_check_fn=runner,
                import_fn=lambda name: modules[name],
            )
        non_ev = set(w.CANONICAL_CHECKS) - {'ev'}
        for r in rows:
            if r['check'] in non_ev:
                self.assertEqual(r['action'], 'ran')
                self.assertEqual(r['rc'], 1)
        self.assertEqual({c for c, _ in runner.alerts}, non_ev)


class LoadEnvFileTest(unittest.TestCase):
    def test_loads_missing_vars_without_override(self):
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / '.env.larry'
            env_path.write_text(
                '# a comment\n'
                '\n'
                'NEW_SEED_VAR=fresh\n'
                "QUOTED_VAR='with-quotes'\n"
                'EXISTING_SEED_VAR=from-file\n'
                'not-a-kv-line\n'
            )
            with mock.patch.dict(
                os.environ, {'EXISTING_SEED_VAR': 'from-env'}, clear=False,
            ):
                os.environ.pop('NEW_SEED_VAR', None)
                os.environ.pop('QUOTED_VAR', None)
                loaded = seed.load_env_file(env_path)
                self.assertEqual(os.environ['NEW_SEED_VAR'], 'fresh')
                self.assertEqual(os.environ['QUOTED_VAR'], 'with-quotes')
                # Already-set var is never overridden.
                self.assertEqual(os.environ['EXISTING_SEED_VAR'], 'from-env')
                self.assertEqual(loaded, 2)

    def test_missing_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(seed.load_env_file(Path(td) / 'nope.env'), 0)


if __name__ == '__main__':
    unittest.main()
