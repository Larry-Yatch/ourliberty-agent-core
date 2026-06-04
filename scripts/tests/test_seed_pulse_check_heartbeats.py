#!/usr/bin/env python3
"""Tests for seed_pulse_check_heartbeats (one-time liveness bootstrap).

The contract the seed script must honour:

  - It NEVER invokes a check in a side-effecting mode. Every check it runs is
    invoked with exactly ``['--dry-run']`` (the proven POST/DM/config-free path)
    via run_check. We prove this by injecting a spy run_check and asserting the
    argv of every call.
  - A check it cannot prove safe to run (no --dry-run => argparse SystemExit) is
    baseline-seeded, not run, and never gets a fake heartbeat.
  - Event-driven checks are skipped (the watcher does not track their staleness).
  - --plan / execute=False writes nothing at all.

No real pulse_check_<id> module is imported and no real alert queue is touched:
import_fn and run_check_fn are injected.

Run:
    cd ~/agent-core && python3 -m unittest \
        scripts.tests.test_seed_pulse_check_heartbeats
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_pulse_check_staleness as w  # noqa: E402
import seed_pulse_check_heartbeats as seed  # noqa: E402


NOW = 1_750_000_000.0


def _full_cadence() -> dict:
    """A cadence config covering every canonical check; vii event-driven."""
    cfg = {cid: {'cadence_hours': 168, 'grace_hours': 36}
           for cid in w.CANONICAL_CHECKS}
    cfg['vii'] = {'event_driven': True}
    return cfg


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
        # vii (event-driven) is skipped; every OTHER check is invoked, and ONLY
        # ever with --dry-run — the side-effect-free contract.
        self.assertTrue(seen_argv)
        for check_id, argv in seen_argv:
            self.assertEqual(argv, ['--dry-run'],
                             f'{check_id} invoked with {argv!r}, not --dry-run')
        self.assertNotIn('vii', {cid for cid, _ in seen_argv})
        # All non-event-driven checks ran cleanly.
        ran = {r['check'] for r in rows if r['action'] == 'ran'}
        self.assertEqual(ran, set(w.CANONICAL_CHECKS) - {'vii'})

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
        self.assertEqual(seeded, set(w.CANONICAL_CHECKS) - {'vii'})
        # No heartbeat files were written by the seed itself.
        self.assertEqual(list(self.bb.glob('*.heartbeat')), [])
        # The baseline now records monitoring_since for the seeded checks.
        baseline = w.load_baseline(self.bb)
        for cid in set(w.CANONICAL_CHECKS) - {'vii'}:
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
            if r['check'] == 'vii':
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
        vii = next(r for r in rows if r['check'] == 'vii')
        self.assertEqual(vii['action'], 'skipped')
        self.assertIn('event-driven', vii['detail'])


if __name__ == '__main__':
    unittest.main()
