#!/usr/bin/env python3
"""Meta-test: prove the test-gate sandbox actually engages under the EXACT
production invocation the Forge build runs use.

Root cause this guards (proven empirically, 2026-06-10): the regression gate
runs ``python3 -m unittest discover -s scripts/tests``. unittest discover
imports each test module as a TOP-LEVEL module and NEVER executes the
``scripts/tests`` package ``__init__.py``, so the #412 OURLIBERTY_*_ROOT /
LOG_DIR sandbox and the #428 OURLIBERTY_DISABLE_LIVE_EMIT guard + run sentinel
that __init__ arms are dead code under that invocation. The fix
(test_regression_check.build_sandbox_env) reproduces __init__'s env vars and
injects them into the discover subprocess, because env vars are process-wide
and immune to import semantics.

Structure (a self-spawning probe so the meta-test is fully self-contained):

  * ``GateSandboxEngagesTest`` (the OUTER meta-test) builds the gate's sandbox
    env via ``build_sandbox_env`` and runs the production invocation
    ``python3 -m unittest discover -s scripts/tests`` in a subprocess with a
    temp HOME, pattern-limited (-p) to THIS file so it runs in well under a
    second. It reads what the inner run observed and asserts the sandbox armed.

  * ``_GateSandboxProbe`` (the INNER probe) runs ONLY inside that spawned
    discover subprocess (skipUnless the probe-output env var is set). It records
    the OURLIBERTY_* env it sees, confirms a chain_event_emit burst hits NO real
    ledger (the live client is None), and writes the observations to a JSON file
    the outer test reads back.

The control test (``test_without_injection_sandbox_does_not_engage``) runs the
SAME discover with the un-injected env and shows the sandbox does NOT engage —
so the positive assertions are load-bearing: revert build_sandbox_env's
injection and the positive test fails.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Set in the spawned discover subprocess to (a) activate the inner probe and
# (b) tell it where to write its observations.
_PROBE_OUTPUT_ENV = 'OURLIBERTY_GATE_PROBE_OUTPUT'

# The env vars scripts/tests/__init__.py arms and that build_sandbox_env must
# reproduce in the discover subprocess.
_SANDBOX_VARS = (
    'OURLIBERTY_AGENTS_ROOT',
    'OURLIBERTY_WORKTREES_ROOT',
    'OURLIBERTY_LOG_DIR',
    'OURLIBERTY_DISABLE_LIVE_EMIT',
    'OURLIBERTY_TEST_RUN_SENTINEL',
)

_SENTINEL_PREFIX = 'OL-TEST-RUN-SENTINEL-'


@unittest.skipUnless(
    os.environ.get(_PROBE_OUTPUT_ENV),
    'inner probe — runs only inside the spawned discover subprocess',
)
class _GateSandboxProbe(unittest.TestCase):
    """Executed inside the discover subprocess the outer meta-test spawns.

    It is a real discovered test (so it proves the env is live in an actual
    ``discover`` run, not a synthetic import), recording the sandbox env and a
    real-ledger burst result for the outer test to assert on."""

    def test_record_sandbox_state(self):
        observed = {var: os.environ.get(var) for var in _SANDBOX_VARS}

        # A chain_event_emit burst: with the sandbox armed
        # (OURLIBERTY_DISABLE_LIVE_EMIT=1) _get_client() returns None, so every
        # emit drops to WARN+False and NOTHING reaches the live chain_events
        # ledger. We NEVER emit when a live client exists — a test must never
        # touch the real ledger — so a built client is recorded, not exercised.
        live_client_built = None
        burst_real_ledger_hits = 0
        try:
            import chain_event_emit as cee

            cee.reset_client_for_testing()
            live_client_built = cee._get_client() is not None
            if not live_client_built:
                for i in range(9):
                    ok = cee.emit_event(
                        event_type='approval_request',
                        agent='gate-sandbox-meta-probe',
                        task_id=f'gate-sandbox-probe-{i}',
                        payload={'probe': i},
                    )
                    if ok:
                        burst_real_ledger_hits += 1
        except Exception as exc:  # noqa: BLE001 — record, don't crash the probe
            live_client_built = f'error:{exc!r}'

        observed['_live_client_built'] = live_client_built
        observed['_burst_real_ledger_hits'] = burst_real_ledger_hits

        Path(os.environ[_PROBE_OUTPUT_ENV]).write_text(json.dumps(observed))


@unittest.skipIf(
    os.environ.get(_PROBE_OUTPUT_ENV),
    'outer meta-test — skipped inside the spawned probe run to avoid recursion',
)
class GateSandboxEngagesTest(unittest.TestCase):
    _PATTERN = 'test_gate_sandbox_env_injection.py'

    def _run_discover(self, env: dict, home: str) -> dict:
        """Run the EXACT production invocation in a subprocess, pattern-limited
        to this file, and return the probe's observations."""
        import test_regression_check as trc

        repo_root = Path(trc.__file__).resolve().parents[1]
        probe_dir = tempfile.mkdtemp(prefix='ol-gate-probe-out-')
        self.addCleanup(_rmtree, probe_dir)
        probe_out = Path(probe_dir) / 'probe.json'

        env = dict(env)
        env['HOME'] = home
        env[_PROBE_OUTPUT_ENV] = str(probe_out)

        res = subprocess.run(
            ['python3', '-m', 'unittest', 'discover',
             '-s', 'scripts/tests', '-p', self._PATTERN, '-v'],
            cwd=str(repo_root), env=env,
            capture_output=True, text=True, timeout=120,
        )
        self.assertTrue(
            probe_out.exists(),
            f'inner probe never ran (rc={res.returncode}); '
            f'stdout:\n{res.stdout}\nstderr:\n{res.stderr}',
        )
        return json.loads(probe_out.read_text())

    def _scrubbed_base_env(self, *, drop_supabase: bool) -> dict:
        """A base env with the sandbox vars removed so a removed injection is
        DETECTABLE. Optionally drop SUPABASE creds too (the control run has no
        DISABLE_LIVE_EMIT, so without this a live client could be built)."""
        base = {
            k: v for k, v in os.environ.items()
            if not k.startswith('OURLIBERTY_')
        }
        if drop_supabase:
            for k in list(base):
                if k.startswith('SUPABASE_'):
                    base.pop(k)
        return base

    def test_injected_env_arms_sandbox_in_discover(self):
        """build_sandbox_env's injection makes the #412/#428 sandbox engage in a
        real ``discover`` subprocess: every sandbox var is present, live emit is
        disabled, and a 9-event burst lands on NO real ledger."""
        import test_regression_check as trc

        home = tempfile.mkdtemp(prefix='ol-gate-home-')
        self.addCleanup(_rmtree, home)

        env = trc.build_sandbox_env(
            base_env=self._scrubbed_base_env(drop_supabase=False),
        )
        observed = self._run_discover(env, home)

        for var in _SANDBOX_VARS:
            self.assertTrue(
                observed.get(var),
                f'{var} missing in the discover subprocess — the #412/#428 '
                f'sandbox did NOT engage. observed={observed}',
            )
        self.assertEqual(observed['OURLIBERTY_DISABLE_LIVE_EMIT'], '1')
        self.assertTrue(
            observed['OURLIBERTY_TEST_RUN_SENTINEL'].startswith(_SENTINEL_PREFIX),
            f'sentinel not in canonical form: {observed["OURLIBERTY_TEST_RUN_SENTINEL"]!r}',
        )
        # Live emit disabled => no real chain_events client, zero ledger hits.
        self.assertIs(observed['_live_client_built'], False)
        self.assertEqual(observed['_burst_real_ledger_hits'], 0)
        # The sandbox roots are redirected AWAY from the real ~/agents tree.
        self.assertNotEqual(
            observed['OURLIBERTY_AGENTS_ROOT'], str(Path(home) / 'agents'),
        )
        self.assertNotEqual(
            observed['OURLIBERTY_AGENTS_ROOT'], str(Path.home() / 'agents'),
        )
        self.assertEqual(
            observed['OURLIBERTY_LOG_DIR'],
            str(Path(observed['OURLIBERTY_AGENTS_ROOT']) / 'logs'),
        )

    def test_without_injection_sandbox_does_not_engage(self):
        """Control: the SAME production invocation WITHOUT build_sandbox_env's
        injection leaves the sandbox vars unset — because discover never runs
        scripts/tests/__init__.py. This is the failing-state the positive test
        guards against; revert the injection and the positive test sees this and
        fails. SUPABASE creds are dropped here so the no-DISABLE_LIVE_EMIT run
        still can't build a live client — the control never risks the ledger."""
        home = tempfile.mkdtemp(prefix='ol-gate-home-noinject-')
        self.addCleanup(_rmtree, home)

        env = self._scrubbed_base_env(drop_supabase=True)
        observed = self._run_discover(env, home)

        # __init__ didn't run and nothing injected the vars => they're absent.
        self.assertIsNone(observed['OURLIBERTY_DISABLE_LIVE_EMIT'])
        self.assertIsNone(observed['OURLIBERTY_TEST_RUN_SENTINEL'])
        # And with creds dropped the control is itself ledger-safe.
        self.assertIs(observed['_live_client_built'], False)
        self.assertEqual(observed['_burst_real_ledger_hits'], 0)


def _rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
