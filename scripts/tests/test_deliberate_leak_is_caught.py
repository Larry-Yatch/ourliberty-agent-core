#!/usr/bin/env python3
"""test_deliberate_leak_is_caught.py — THE ACCEPTANCE PROOF (test-jail Layer E).

WHAT THIS PROVES
----------------
The property that ends the test→production leak class (docs/test-jail-spec.md,
"Acceptance"): a deliberately-leaking test must FAIL the suite LOUDLY under EVERY
invocation shape, on BOTH machines, with ZERO writes to the real ~/agents tree
and zero Telegram/Supabase/gh/claude traffic.

This meta-test drives the deliberately-leaking fixture
``scripts/tests/_leak_fixtures/leak_probe.py`` (which is NOT collected by the
normal suite — it lives in a leading-underscore dir and is not named test*.py)
under each shape × each guarded channel and asserts each run:
  1. exits NON-ZERO (loud failure), and
  2. carries a ``TestIsolationBreach`` signal (the Layer-B choke guard fired), and
  3. wrote ZERO bytes of the leak payload to a jailed stand-in for ~/agents
     (the unique ``LEAK_MARKER`` never lands on disk).

INVOCATION SHAPES (the audit's 9+ vectors, collapsed to the four the spec names)
  - bare:   ``python3 -m unittest discover`` pointed at a tmp dir holding only a
            thin shim that imports the fixture (discover's own tree is untouched,
            so the 17-baseline can never absorb the leak).
  - dotted: ``python3 -m unittest scripts.tests._leak_fixtures.leak_probe``.
  - direct: ``python3 scripts/tests/_leak_fixtures/leak_probe.py``.
  - gate:   the real ``test_regression_check`` building blocks — ``run_tests_in_dir``
            sees the leak as a failing test and ``compute_verdict`` returns BLOCK
            (the leak shows up as a regression). A separate test exercises the
            gate's outside-jail tripwire (``scan_real_tree_for_sentinel``).

GUARDED CHANNELS: larry_alerts.append_alert (pager), safe_write_inbox (paid
dispatch), supabase_factory.get_supabase_client (live DB), agent_runner.run_claude
(Opus spend; refused at the concurrency-guard chokepoint before the spawn).

HERMETICITY (this meta-test must leak NOTHING itself)
Every leak attempt runs in a SUBPROCESS under a jailed ``HOME`` + tmp agents root,
so even the frozen-``Path.home()`` modules (larry_alerts, concurrency_guard) that
ignore ``OURLIBERTY_AGENTS_ROOT`` resolve into the jail, never the real tree. The
meta-test asserts the jail is marker-free after each run.

MACHINE PARITY (droplet AND macOS)
On the droplet the floor is Layers A+B+C (the gate's hard wall arms when bwrap/
userns is available; today it warns + runs unwalled). On macOS there is NO hard
wall — the floor is Layers A+B. This proof asserts the ``TestIsolationBreach``
from the choke guard, which fires identically on BOTH machines, so it does NOT
depend on EROFS / the hard wall and holds on either platform.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import fnmatch
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

# scripts/tests/ -> [0]=tests, [1]=scripts, [2]=repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_DIR = REPO_ROOT / 'scripts' / 'tests' / '_leak_fixtures'
_FIXTURE_FILE = _FIXTURE_DIR / 'leak_probe.py'

# Mirror of leak_probe.LEAK_MARKER. Kept as a literal so this meta-test takes no
# import dependency on the (deliberately un-collected) fixture module; the
# fixture and this constant must stay in parity.
LEAK_MARKER = 'OL-DELIBERATE-LEAK-PROBE-MARKER-DO-NOT-SHIP'

CHANNELS = ('alert', 'inbox', 'supabase', 'claude')
BREACH_SIGNAL = 'TestIsolationBreach'

# Credential families a leaking process must never see (constraint 6 / M4/H12).
# The jail env is built subtractively from a minimal base, so these are simply
# never added — asserted absent by ``_assert_creds_stripped``.
_STRIP_PREFIXES = ('SUPABASE_', 'TELEGRAM_', 'ANTHROPIC_', 'CLAUDE_CODE_OAUTH_')
_STRIP_EXACT = ('GH_TOKEN', 'GITHUB_TOKEN')

_SUBPROCESS_TIMEOUT_S = 120


def _make_jail(case: unittest.TestCase) -> tuple[Path, Path]:
    """Create a jailed HOME with a minimal ~/agents skeleton and return
    ``(home, agents_root)``. Registered for cleanup on the case."""
    home = Path(tempfile.mkdtemp(prefix='ol-leakproof-home-'))
    case.addCleanup(_rmtree, home)
    agents = home / 'agents'
    for sub in ('logs', 'inboxes', 'blackboard', 'state', 'outboxes', 'config'):
        (agents / sub).mkdir(parents=True, exist_ok=True)
    # Seed the concurrency-guard file so ConcurrencyGuard.__init__ (whose write is
    # the one un-guarded pre-acquire step) finds it present and writes nothing.
    (agents / 'config' / '.concurrency-guard.json').write_text(
        json.dumps({'slots': [], 'max': 6})
    )
    (home / '.claude' / 'projects').mkdir(parents=True, exist_ok=True)
    (home / 'agent-worktrees').mkdir(parents=True, exist_ok=True)
    (home / '.gitconfig').write_text(
        '[user]\n\temail = leakproof@example.com\n\tname = Leak Proof\n'
    )
    return home, agents


def _rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _base_env(home: Path, agents: Path, channel: str) -> dict:
    """A minimal, credential-free environment for a jailed leak subprocess.

    Built from a small allowlist (NOT a copy of os.environ) so live SUPABASE_* /
    TELEGRAM_* / ANTHROPIC_* / gh tokens from the Forge/Mirror service env can
    never reach the leak process — the H12 amplifier, subtracted at the source."""
    env = {
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
        'HOME': str(home),
        'OURLIBERTY_AGENTS_ROOT': str(agents),
        'OURLIBERTY_WORKTREES_ROOT': str(home / 'agent-worktrees'),
        'OURLIBERTY_LOG_DIR': str(agents / 'logs'),
        'OL_LEAK_REPO_ROOT': str(REPO_ROOT),
        'OL_LEAK_CHANNEL': channel,
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': os.pathsep.join((
            str(REPO_ROOT / 'scripts' / 'tests' / '_leak_fixtures'),
            str(REPO_ROOT / 'scripts' / 'tests'),
            str(REPO_ROOT / 'scripts'),
        )),
    }
    if 'LANG' in os.environ:
        env['LANG'] = os.environ['LANG']
    return env


def _marker_hits(root: Path) -> list[Path]:
    """Every file under ``root`` whose bytes contain LEAK_MARKER. Empty list is
    the zero-write proof: the choke guard refused before any byte was written."""
    hits = []
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        try:
            if LEAK_MARKER.encode() in path.read_bytes():
                hits.append(path)
        except OSError:
            continue
    return hits


class _LeakProofMixin:
    def _assert_breach(self, channel, returncode, output, home: Path):
        # 1. Loud: non-zero exit.
        self.assertNotEqual(
            returncode, 0,
            f'[{channel}] leak run exited 0 — the leak was NOT caught.\n{output}',
        )
        # 2. The Layer-B choke guard fired.
        self.assertIn(
            BREACH_SIGNAL, output,
            f'[{channel}] no {BREACH_SIGNAL} in output — a guard did not fire.\n'
            f'{output}',
        )
        # 3. Zero bytes of the leak payload reached the jailed real-tree stand-in.
        hits = _marker_hits(home)
        self.assertEqual(
            hits, [],
            f'[{channel}] LEAK MARKER written to {len(hits)} file(s) in the '
            f'jail — a byte escaped the guard: {hits}',
        )

    def _run(self, argv, env, cwd):
        proc = subprocess.run(
            argv, cwd=str(cwd), env=env,
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
        )
        return proc.returncode, (proc.stdout or '') + '\n' + (proc.stderr or '')


class DeliberateLeakCaughtTest(_LeakProofMixin, unittest.TestCase):
    """The leak must fail loud under bare discover, dotted, and direct shapes —
    for every guarded channel, with zero writes to the jailed stand-in tree."""

    def test_bare_discover_catches_leak(self):
        """``unittest discover`` pointed at a tmp dir holding only a shim that
        imports the fixture — exercises the top-level-module loader (the exact
        shape that historically never armed __init__) without ever letting the
        fixture into the real ``scripts/tests`` discovery tree."""
        for channel in CHANNELS:
            with self.subTest(channel=channel):
                home, agents = _make_jail(self)
                disc = Path(tempfile.mkdtemp(prefix='ol-leakproof-disc-'))
                self.addCleanup(_rmtree, disc)
                (disc / 'test_leak_shim.py').write_text(_SHIM_SRC)
                env = _base_env(home, agents, channel)
                rc, out = self._run(
                    [sys.executable, '-m', 'unittest', 'discover',
                     '-s', str(disc), '-t', str(disc), '-v'],
                    env, disc,
                )
                self._assert_breach(channel, rc, out, home)

    def test_dotted_module_run_catches_leak(self):
        """``python3 -m unittest scripts.tests._leak_fixtures.leak_probe`` — the
        dotted loader that DOES run the package __init__ chain."""
        for channel in CHANNELS:
            with self.subTest(channel=channel):
                home, agents = _make_jail(self)
                env = _base_env(home, agents, channel)
                rc, out = self._run(
                    [sys.executable, '-m', 'unittest',
                     'scripts.tests._leak_fixtures.leak_probe', '-v'],
                    env, REPO_ROOT,
                )
                self._assert_breach(channel, rc, out, home)

    def test_direct_file_run_catches_leak(self):
        """``python3 scripts/tests/_leak_fixtures/leak_probe.py`` — the M8 ninth
        vector (``if __name__ == '__main__'``: raw env, no __init__, no conftest)."""
        for channel in CHANNELS:
            with self.subTest(channel=channel):
                home, agents = _make_jail(self)
                env = _base_env(home, agents, channel)
                rc, out = self._run(
                    [sys.executable, str(_FIXTURE_FILE), '-v'],
                    env, REPO_ROOT,
                )
                self._assert_breach(channel, rc, out, home)


class RegressionGateBlocksLeakTest(_LeakProofMixin, unittest.TestCase):
    """The gate shape: the real ``test_regression_check`` building blocks see the
    leak as a NEW failing test and return verdict=BLOCK; the outside-jail tripwire
    catches a real-tree escape."""

    def _gate_module(self):
        sys.path.insert(0, str(REPO_ROOT / 'scripts'))
        import test_regression_check as trc  # noqa: E402
        return trc

    def test_gate_blocks_leak_as_regression(self):
        trc = self._gate_module()
        for channel in CHANNELS:
            with self.subTest(channel=channel):
                home, agents = _make_jail(self)
                workdir = Path(tempfile.mkdtemp(prefix='ol-leakproof-gatewd-'))
                self.addCleanup(_rmtree, workdir)
                tests_dir = workdir / 'scripts' / 'tests'
                tests_dir.mkdir(parents=True, exist_ok=True)
                (tests_dir / 'test_gate_leak_probe.py').write_text(_SHIM_SRC)

                base = _base_env(home, agents, channel)
                env = trc.build_sandbox_env(
                    isolated_agents_root=agents, base_env=base,
                )
                env['HOME'] = str(home)  # keep the jail HOME through the strip
                env['OL_LEAK_REPO_ROOT'] = str(REPO_ROOT)
                env['OL_LEAK_CHANNEL'] = channel
                env['PYTHONPATH'] = base['PYTHONPATH']

                failures = trc.run_tests_in_dir(workdir, 60, env=env)
                self.assertTrue(
                    failures,
                    f'[{channel}] gate saw NO failing test — the leak was not '
                    f'detected as a regression.',
                )
                verdict = trc.compute_verdict(set(), failures)
                self.assertEqual(
                    verdict['verdict'], 'BLOCK',
                    f'[{channel}] gate verdict was {verdict["verdict"]}, '
                    f'expected BLOCK. failures={sorted(failures)}',
                )
                self.assertEqual(
                    _marker_hits(home), [],
                    f'[{channel}] gate run wrote the leak marker into the jail.',
                )

    def test_gate_outside_jail_tripwire_fires(self):
        """The parent-process tripwire (#475 / constraint 4) catches a sentinel-
        bearing file in the REAL tree even when every in-process layer is bypassed
        — proving the gate's last-line backstop fails the run (exit 2 = BLOCK)."""
        trc = self._gate_module()
        sentinel = 'OL-TEST-RUN-SENTINEL-' + uuid.uuid4().hex
        real_stub = Path(tempfile.mkdtemp(prefix='ol-leakproof-realstub-'))
        self.addCleanup(_rmtree, real_stub)
        (real_stub / 'blackboard').mkdir(parents=True, exist_ok=True)
        (real_stub / 'blackboard' / 'larry-alerts.jsonl').write_text(
            f'{{"leaked": "{sentinel}"}}\n'
        )
        saved = trc.REAL_AGENTS
        trc.REAL_AGENTS = real_stub
        try:
            with self.assertRaises(trc.AnalysisError):
                trc.scan_real_tree_for_sentinel(sentinel)
        finally:
            trc.REAL_AGENTS = saved


class FixtureIsolationInvariantsTest(unittest.TestCase):
    """Structural guards that keep the leak fixture OUT of the discovered suite —
    if these ever regress, the fixture would join the 17-baseline as a permanent
    failure."""

    def test_fixture_not_matched_by_discover_pattern(self):
        # unittest discover's default pattern is test*.py; the fixture must not
        # match (or the bare suite would collect + fail it forever).
        self.assertFalse(
            fnmatch.fnmatch(_FIXTURE_FILE.name, 'test*.py'),
            f'{_FIXTURE_FILE.name} matches discover pattern test*.py — it would '
            f'be collected by the normal suite and fail the baseline.',
        )

    def test_fixture_dir_is_underscore_prefixed(self):
        # A leading-underscore dir is not a conventional test package; even were
        # it a package, discover only collects test*.py within it.
        self.assertTrue(
            _FIXTURE_DIR.name.startswith('_'),
            f'{_FIXTURE_DIR.name} must be underscore-prefixed so discover does '
            f'not treat it as a normal test package.',
        )

    def test_fixture_and_marker_exist(self):
        self.assertTrue(_FIXTURE_FILE.is_file(), 'leak_probe.py missing')
        self.assertIn(
            LEAK_MARKER, _FIXTURE_FILE.read_text(),
            'LEAK_MARKER drifted out of parity with the fixture.',
        )


# The thin discovered-test shim used by the bare-discover and gate shapes. It
# lives in a tmp dir (never in the real scripts/tests tree) and simply imports
# the fixture so the loader collects ``DeliberateLeakProbe``. Path resolution is
# driven by OL_LEAK_REPO_ROOT (the fixture dir is not under the shim's tmp tree).
_SHIM_SRC = '''import os, sys
_root = os.environ["OL_LEAK_REPO_ROOT"]
for _p in (os.path.join(_root, "scripts", "tests", "_leak_fixtures"),
           os.path.join(_root, "scripts", "tests"),
           os.path.join(_root, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import _bootstrap  # noqa: F401  (engages the sandbox + mints the run sentinel)
from leak_probe import DeliberateLeakProbe  # noqa: F401  (collected by the loader)
'''


if __name__ == '__main__':
    unittest.main()
