"""test_tier_state_write_guard.py — the corrected-L2 seal for the 2026-07-02
tier-bench outage (a unit test wrote LIVE ~/agents/blackboard/active-tier.json
and benched every tier ~30min).

Root cause: the sandbox root-redirect fails OPEN — when a child `unittest`
inherits a live-pinned OURLIBERTY_AGENTS_ROOT (agent_runner pins it), the
bootstrap/conftest setdefault refuses to override, so tier-state writers resolve
to the REAL tree. A destination-AWARE guard (`refuse_live_state_write`) closes
this regardless of WHY the path went live (env fail-open OR a hardcoded literal),
while still allowing the many legitimate tests that drive tier-state against a
correctly-redirected tmpdir.

These tests NEVER write the real tree: the live-tree cases monkeypatch
`_real_agents_roots` to a throwaway dir, so even a guard regression can only
touch a tmpdir, and in any case the guard raises BEFORE any mkdir/write.
"""
import _bootstrap  # noqa: F401  # arm the sandbox + run sentinel first
import importlib
import os
import unittest
from pathlib import Path

import test_isolation_guard as tig
from test_isolation_guard import TestIsolationBreach

active_tier = importlib.import_module('active_tier')
cycle_tier_state = importlib.import_module('cycle_tier_state')

_SENTINEL = 'OURLIBERTY_TEST_RUN_SENTINEL'


class RefuseLiveStateWriteUnitTest(unittest.TestCase):
    """The guard primitive itself: fires only under-test AND only for a path
    under the real agents tree."""

    def setUp(self):
        # Sanity: the ambient test run must have the sentinel armed, else these
        # assertions would be vacuous.
        self.assertTrue(os.environ.get(_SENTINEL),
                        'run sentinel must be armed for this suite')

    def test_production_passthrough_even_for_live_path(self):
        # Sentinel cleared == production: never raises, even for a real path.
        with tig.allow('unit'):
            self.assertIsNone(
                tig.refuse_live_state_write(
                    Path.home() / 'agents' / 'blackboard' / 'active-tier.json',
                    'unit'))

    def test_fires_on_live_tree_path(self):
        fake_real = Path(self.enterContext(_tmp())) / 'agents'
        _patch(self, tig, '_real_agents_roots', lambda: [fake_real.resolve()])
        with self.assertRaises(TestIsolationBreach):
            tig.refuse_live_state_write(
                fake_real / 'blackboard' / 'active-tier.json', 'unit')

    def test_allows_sandbox_tmpdir_path(self):
        fake_real = Path(self.enterContext(_tmp())) / 'agents'
        _patch(self, tig, '_real_agents_roots', lambda: [fake_real.resolve()])
        sandbox = Path(self.enterContext(_tmp())) / 'sandbox-agents'
        # Not under fake_real -> allowed (this is the non-invasive property).
        self.assertIsNone(
            tig.refuse_live_state_write(
                sandbox / 'blackboard' / 'active-tier.json', 'unit'))


class ActiveTierWriteGuardTest(unittest.TestCase):
    """Integration: the real active_tier writers refuse a live-tree resolution
    and permit a sandboxed one — no allow() wrapper needed for the sandbox case.
    """

    def test_write_refuses_when_root_fails_open_to_live(self):
        tmp = Path(self.enterContext(_tmp()))
        fake_real = tmp / 'agents'  # stand-in for /home/larry/agents
        _patch(self, tig, '_real_agents_roots', lambda: [fake_real.resolve()])
        # Simulate the exact incident: OURLIBERTY_AGENTS_ROOT pinned to the live
        # tree (fail-open) while the run sentinel is armed.
        _patch_env(self, 'OURLIBERTY_AGENTS_ROOT', str(fake_real))
        with self.assertRaises(TestIsolationBreach):
            active_tier._write({'tier': 'tier2', 'benched': True})
        # And crucially: nothing was written to the "live" tree.
        self.assertFalse((fake_real / 'blackboard' / 'active-tier.json').exists())

    def test_write_allowed_against_sandbox_root(self):
        tmp = Path(self.enterContext(_tmp()))
        fake_real = tmp / 'home-agents'
        _patch(self, tig, '_real_agents_roots', lambda: [fake_real.resolve()])
        sandbox = tmp / 'sandbox-agents'  # NOT under fake_real
        _patch_env(self, 'OURLIBERTY_AGENTS_ROOT', str(sandbox))
        active_tier._write({'tier': 'tier1'})  # must NOT raise
        written = sandbox / 'blackboard' / 'active-tier.json'
        self.assertTrue(written.exists())
        self.assertIn('tier1', written.read_text())

    def test_atomic_write_json_refuses_live(self):
        tmp = Path(self.enterContext(_tmp()))
        fake_real = tmp / 'agents'
        _patch(self, tig, '_real_agents_roots', lambda: [fake_real.resolve()])
        with self.assertRaises(TestIsolationBreach):
            active_tier._atomic_write_json(
                fake_real / 'state' / 'tier-rr-counter', {'n': 1})

    def test_append_cost_row_refuses_live(self):
        tmp = Path(self.enterContext(_tmp()))
        fake_real = tmp / 'agents'
        _patch(self, tig, '_real_agents_roots', lambda: [fake_real.resolve()])
        _patch_env(self, 'OURLIBERTY_AGENTS_ROOT', str(fake_real))
        with self.assertRaises(TestIsolationBreach):
            active_tier.append_cost_row('tier1', model='x', cost_usd=0.0)
        self.assertFalse((fake_real / 'blackboard' / 'costs.jsonl').exists())


class CycleTierStateWriteGuardTest(unittest.TestCase):
    def test_atomic_write_refuses_live(self):
        tmp = Path(self.enterContext(_tmp()))
        fake_real = tmp / 'agents'
        _patch(self, tig, '_real_agents_roots', lambda: [fake_real.resolve()])
        _patch_env(self, 'OURLIBERTY_AGENTS_ROOT', str(fake_real))
        with self.assertRaises(TestIsolationBreach):
            cycle_tier_state._atomic_write({'tier': 1})
        self.assertFalse((fake_real / 'state' / 'cycle-tier.json').exists())

    def test_atomic_write_allowed_against_sandbox(self):
        tmp = Path(self.enterContext(_tmp()))
        fake_real = tmp / 'home-agents'
        _patch(self, tig, '_real_agents_roots', lambda: [fake_real.resolve()])
        sandbox = tmp / 'sandbox-agents'
        _patch_env(self, 'OURLIBERTY_AGENTS_ROOT', str(sandbox))
        cycle_tier_state._atomic_write({'tier': 2})  # must NOT raise
        self.assertTrue((sandbox / 'state' / 'cycle-tier.json').exists())

    def test_log_refuses_live(self):
        tmp = Path(self.enterContext(_tmp()))
        fake_real = tmp / 'agents'
        _patch(self, tig, '_real_agents_roots', lambda: [fake_real.resolve()])
        _patch_env(self, 'OURLIBERTY_AGENTS_ROOT', str(fake_real))
        with self.assertRaises(TestIsolationBreach):
            cycle_tier_state._log('corruption', 'WARN')
        self.assertFalse((fake_real / 'logs' / 'cycle-tier-state.log').exists())

    def test_read_tier_state_corrupt_refuses_live(self):
        # Reviewer-found gap: a corrupt LIVE state file makes read_tier_state
        # call _log (live log write) BEFORE the guarded _atomic_write.
        tmp = Path(self.enterContext(_tmp()))
        fake_real = tmp / 'agents'
        _patch(self, tig, '_real_agents_roots', lambda: [fake_real.resolve()])
        _patch_env(self, 'OURLIBERTY_AGENTS_ROOT', str(fake_real))
        sp = fake_real / 'state' / 'cycle-tier.json'
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text('{ not valid json')
        with self.assertRaises(TestIsolationBreach):
            cycle_tier_state.read_tier_state()


# ---- tiny helpers (stdlib only; no external deps) --------------------------

def _tmp():
    import tempfile
    return tempfile.TemporaryDirectory()


def _patch(tc, obj, attr, value):
    orig = getattr(obj, attr)
    setattr(obj, attr, value)
    tc.addCleanup(setattr, obj, attr, orig)


def _patch_env(tc, key, value):
    orig = os.environ.get(key)
    os.environ[key] = value

    def _restore():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    tc.addCleanup(_restore)


if __name__ == '__main__':
    unittest.main()
