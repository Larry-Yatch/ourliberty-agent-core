#!/usr/bin/env python3
"""test_no_production_writes_runtime.py — runtime backstop for the
test->production write-isolation discipline.

Every other isolation gate is STATIC analysis:
  - test_no_production_path_leaks.py  — flags prod-path string literals.
  - test_no_production_log_writes.py  — flags prod-module imports from
    external test dirs.
Static gates are blind to a leak via a novel mechanism (a new daemon, a
constant frozen at import before the redirect, a write that bypasses
resolve_log_dir()). Each prior incident — 2026-05-27 (TIER_ONE_MARKER /
'401 Unauthorized' into beacon_telegram_bot.log) and 2026-06-02 (real-*/
prod-* rows) — slipped through exactly that blind spot.

This module adds the RUNTIME half: a session-scoped tripwire (the fixture
lives in conftest.py; the scan + stamp logic and its self-checks live here)
that verifies the running suite wrote NOTHING to the real ~/agents tree.

Mechanism — content-sentinel attribution (chosen over a raw size/mtime diff
because this suite runs on the LIVE droplet, where daemons mutate
~/agents/{logs,blackboard,state} every few seconds; a size diff would
false-positive on daemon churn, a sentinel cannot):

  1. At session start mint a unique run sentinel (UUID-based) and expose it
     via OURLIBERTY_TEST_RUN_SENTINEL.
  2. Stamp the sentinel into the known production log() write helpers
     (instrument_log_helpers) so any TEST-originated log line carries it.
     The per-test OURLIBERTY_LOG_DIR redirect still sends those writes to a
     tmp dir — the stamp only matters if a write escapes to the real tree.
  3. At session end scan the REAL ~/agents/{logs,inboxes,blackboard,state}
     — resolved from the ACTUAL home, deliberately NOT through the sandbox
     env vars — for the sentinel. Any hit = a write that bypassed the
     redirect = a genuine leak. Daemon churn never carries the token, so no
     false positive on the live host. Clean run = zero hits, on the live
     droplet and in quiescent CI identically.

Grain (accepted): sentinel attribution catches leaks flowing through the
instrumented write helpers — the actual incident shape (production log()
helpers writing fixture strings). It deliberately does NOT try to catch
arbitrary un-instrumented writes; that was the size-diff approach rejected
for its live-host false positives.

The GateSelfCheck tests below operate ONLY on synthetic temp trees (never
the real one), so they are host-independent and deterministic: they prove
the scan flags a synthetic production write and ignores an unchanged tree,
so a silent regression in the scan logic can't make the backstop permissive.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_no_production_writes_runtime
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

# Subdirs under the REAL ~/agents tree where a leaked test write would land.
PRODUCTION_SUBDIRS: tuple[str, ...] = ('logs', 'inboxes', 'blackboard', 'state')

# Chunked read size for scanning possibly-large active logs. Reading in
# bounded blocks (with a small overlap so a token straddling a boundary is
# still found) keeps memory flat even for a multi-MB live log.
_SCAN_CHUNK = 1 << 20  # 1 MiB

# Distinguishes this run's sentinel from any literal in source/fixtures.
SENTINEL_PREFIX = 'OL-TEST-RUN-SENTINEL-'


def mint_sentinel() -> str:
    """A fresh, collision-proof run sentinel. UUID4 hex means no file written
    before this run (residue, daemon output, a prior run) can contain it."""
    return SENTINEL_PREFIX + uuid.uuid4().hex


def production_roots() -> list[Path]:
    """The real production roots, resolved from the ACTUAL home directory and
    DELIBERATELY NOT through OURLIBERTY_AGENTS_ROOT / OURLIBERTY_LOG_DIR.

    The backstop's whole job is to detect a write that escaped the sandbox
    env redirects, so it must look at the true ~/agents tree even while those
    vars point at a tmp sandbox. Resolving via Path.home() (which ignores the
    sandbox vars) is the load-bearing detail — route this through the env and
    the tripwire would inspect the sandbox and never see a real leak."""
    base = Path.home() / 'agents'
    return [base / sub for sub in PRODUCTION_SUBDIRS]


def file_contains_token(path: Path, token: bytes) -> bool:
    """True if `token` (bytes) appears anywhere in `path`.

    Reads in bounded chunks, carrying the trailing len(token)-1 bytes across
    the boundary so a token split across two reads is still matched.
    Unreadable files (permissions, races, FIFOs, directories) are treated as
    no-match — the scan must never crash the session on an odd file."""
    if not token:
        return False
    overlap = len(token) - 1
    try:
        with open(path, 'rb') as fh:
            carry = b''
            while True:
                chunk = fh.read(_SCAN_CHUNK)
                if not chunk:
                    return False
                if token in carry + chunk:
                    return True
                carry = (carry + chunk)[-overlap:] if overlap else b''
    except OSError:
        return False


def scan_roots_for_sentinel(roots, sentinel: str,
                            since_mtime: float) -> list[dict]:
    """Return [{'path', 'mtime'}] for every regular file under `roots` modified
    at/after `since_mtime` AND whose bytes contain `sentinel`.

    The mtime pre-filter is the cheap first pass: files untouched during the
    session (rotated logs, the bulk of the tree) are skipped without a read.
    Only files that grew during the window are opened, so a daemon-churned
    file that does not carry the token costs one read and yields no hit — the
    reason a live host produces no false positive."""
    token = sentinel.encode('utf-8')
    hits: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for path in root.rglob('*'):
            try:
                if not path.is_file():
                    continue
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < since_mtime:
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if file_contains_token(path, token):
                hits.append({'path': key, 'mtime': st.st_mtime})
    return hits


# Defense-in-depth ledger (Gap-2 closure): atomic_io JSON/state writes do NOT
# carry the log-line sentinel, so the content-sentinel scan is structurally blind
# to a leaked state file (beacon-pending-approvals.json, for-larry-escalations
# .json, missions.json, ...). We close that by recording, at the atomic_io
# chokepoint, any write that ACTUALLY LANDS under the real ~/agents tree during a
# test — a path-based attribution that is content-independent. This sits BEHIND
# the #816 refuse_live_state_write guard (which raises BEFORE such a write), so
# in normal operation the ledger stays empty: zero overhead, zero false
# positives. If that guard is ever removed/weakened, or a state write reaches the
# real tree by some other atomic_io path, the session-end tripwire still
# attributes it. Process-global (each runner process has its own); reset every
# session end.
_ATOMIC_IO_REAL_TREE_WRITES: list[str] = []


def _instrument_atomic_io(sentinel: str) -> list:
    """Wrap ``atomic_io.atomic_write_bytes`` so a write that lands under the real
    ~/agents tree during a test is recorded to the ledger. Returns undo
    callables. Fail-open: if atomic_io / the guard can't be imported, the
    tripwire still runs (log-helper stamping unaffected).

    Recording is POST-write (only reached when the write succeeded and actually
    landed) and gated on the LIVE run sentinel — so ``allow()``, which clears the
    sentinel for its duration, exempts sanctioned real-tree writes, and a
    correctly-redirected sandbox write (not under a real root) is never
    recorded. atomic_write_text/json both funnel through atomic_write_bytes via
    the module global, so wrapping this one function covers all three."""
    undo: list = []
    try:
        import atomic_io
        import test_isolation_guard as guard
    except Exception:
        return undo

    _orig = atomic_io.atomic_write_bytes
    try:
        _roots = guard._real_agents_roots()
    except Exception:
        _roots = []

    def _wrapped(path, data, *args, _orig=_orig, _roots=_roots, **kwargs):
        result = _orig(path, data, *args, **kwargs)
        # Reached only when the write actually landed. Record iff we are inside a
        # test run (live sentinel set — NOT cleared by an allow() block) and the
        # final path resolves under a real agents root.
        if os.environ.get('OURLIBERTY_TEST_RUN_SENTINEL'):
            try:
                target = Path(result).resolve()
                for root in _roots:
                    if target == root or root in target.parents:
                        _ATOMIC_IO_REAL_TREE_WRITES.append(str(target))
                        break
            except (OSError, ValueError, RuntimeError):
                pass
        return result

    atomic_io.atomic_write_bytes = _wrapped
    undo.append(lambda: setattr(atomic_io, 'atomic_write_bytes', _orig))
    return undo


def _drain_atomic_io_ledger() -> list:
    """Return the recorded real-tree atomic_io writes as tripwire hit dicts and
    reset the ledger for the next session."""
    hits = [{'path': path, 'via': 'atomic_io'} for path in _ATOMIC_IO_REAL_TREE_WRITES]
    _ATOMIC_IO_REAL_TREE_WRITES.clear()
    return hits


def instrument_log_helpers(sentinel: str) -> list:
    """Monkeypatch the known production log() helpers so every test-originated
    log line carries `sentinel`. Returns a list of zero-arg undo callables the
    caller MUST invoke at teardown.

    Scope (the accepted grain): the documented file-write leak vectors,
    agent_runner.log and beacon_telegram_bot.log. agent_runner is safe to
    import eagerly; beacon_telegram_bot is patched ONLY if already imported,
    because importing it requires Telegram env vars and runs import-time side
    effects — we never force that for a run that doesn't touch the bot."""
    undo: list = []

    try:
        import agent_runner
        _orig_ar = agent_runner.log

        def _wrapped_ar(agent_id, message, level='INFO',
                        _orig=_orig_ar, _s=sentinel):
            return _orig(agent_id, f'{message} {_s}', level)

        agent_runner.log = _wrapped_ar
        undo.append(lambda: setattr(agent_runner, 'log', _orig_ar))
    except Exception:
        pass

    bot = sys.modules.get('beacon_telegram_bot')
    if bot is not None and hasattr(bot, 'log'):
        _orig_bot = bot.log

        def _wrapped_bot(msg, _orig=_orig_bot, _s=sentinel):
            return _orig(f'{msg} {_s}')

        bot.log = _wrapped_bot
        undo.append(lambda: setattr(bot, 'log', _orig_bot))

    # Also record atomic_io state writes that land in the real tree (Gap-2
    # defense-in-depth behind the #816 refuse_live_state_write guard).
    undo.extend(_instrument_atomic_io(sentinel))
    return undo


def format_tripwire_failure(hits: list[dict], runner: str) -> str:
    """The canonical failure message for a detected escaped write. Shared so the
    pytest fixture and the unittest atexit hook can't drift in wording."""
    listing = '\n'.join(f'  - {h["path"]}' for h in hits)
    return (
        f'PRODUCTION-WRITE TRIPWIRE ({runner}): the test run left its run '
        f'sentinel in {len(hits)} file(s) under the real ~/agents tree — a '
        'write escaped the sandbox redirect:\n' + listing +
        '\nThis is the leak class the isolation hardening exists to catch. Find '
        'the test that wrote through an un-redirected production helper and route '
        'it through tmp_path / OURLIBERTY_LOG_DIR / the OURLIBERTY_*_ROOT sandbox.'
    )


def run_session_end_tripwire(sentinel: str, session_start: float,
                             undo: list, runner: str):
    """Shared session-end teardown for BOTH runners: undo the log-helper
    instrumentation, then scan the REAL ~/agents tree for the run sentinel.

    Returns ``(hits, message)`` — ``message`` is None on a clean run, else the
    formatted failure text. The CALLER decides how to fail: the pytest fixture
    raises AssertionError; the unittest atexit hook prints + os._exit(1). Undo is
    always attempted first (each callable guarded) so a scan error never leaves
    the production log() helpers monkeypatched."""
    for fn in undo:
        try:
            fn()
        except Exception:
            pass
    hits = scan_roots_for_sentinel(production_roots(), sentinel, session_start)
    # Merge the atomic_io real-tree-write ledger (content-independent Gap-2
    # attribution) into the content-sentinel scan hits.
    hits = hits + _drain_atomic_io_ledger()
    return hits, (format_tripwire_failure(hits, runner) if hits else None)


# ---------------------------------------------------------------------------
# Self-checks — synthetic temp trees only, never the real ~/agents. These run
# under both pytest and `unittest`, host-independent, and prove the scan/stamp
# primitives can't silently regress into permissiveness.
# ---------------------------------------------------------------------------
class ScannerSelfCheckTest(unittest.TestCase):
    def _mk_tree(self) -> Path:
        td = tempfile.mkdtemp(prefix='ol-runtime-selfcheck-')
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        root = Path(td)
        (root / 'logs').mkdir()
        return root

    def test_flags_recent_file_with_sentinel(self):
        """A synthetic production write (file carrying the sentinel, recent
        mtime) MUST be flagged — proves the backstop would fail on a leak."""
        sentinel = mint_sentinel()
        root = self._mk_tree()
        start = time.time() - 1
        leak = root / 'logs' / 'beacon_telegram_bot.log'
        leak.write_text(f'[ts] [beacon] [INFO] fixture line {sentinel}\n')
        hits = scan_roots_for_sentinel([root], sentinel, start)
        self.assertEqual([h['path'] for h in hits], [str(leak)])

    def test_ignores_clean_tree(self):
        """An unchanged tree with NO sentinel (e.g. ordinary daemon churn)
        MUST yield zero hits — proves the backstop is not trivially red."""
        sentinel = mint_sentinel()
        root = self._mk_tree()
        (root / 'logs' / 'daemon.log').write_text(
            'heartbeat 1\nheartbeat 2\n401 Unauthorized\n')
        hits = scan_roots_for_sentinel([root], sentinel, time.time() - 1)
        self.assertEqual(hits, [])

    def test_ignores_old_file_even_with_sentinel(self):
        """A file containing the sentinel but with mtime BEFORE session start
        is skipped by the pre-filter — models a pre-existing file the scan
        must not misattribute to this session."""
        sentinel = mint_sentinel()
        root = self._mk_tree()
        old = root / 'logs' / 'pre-existing.log'
        old.write_text(f'wrote {sentinel} long ago\n')
        old_ts = time.time() - 3600
        os.utime(old, (old_ts, old_ts))
        session_start = time.time()
        hits = scan_roots_for_sentinel([root], sentinel, session_start)
        self.assertEqual(hits, [])

    def test_survives_unreadable_and_binary_files(self):
        """Binary / odd files must not crash the scan or false-positive."""
        sentinel = mint_sentinel()
        root = self._mk_tree()
        (root / 'logs' / 'blob.bin').write_bytes(b'\x00\x01\x02\xff' * 256)
        hits = scan_roots_for_sentinel([root], sentinel, time.time() - 1)
        self.assertEqual(hits, [])

    def test_finds_token_spanning_chunk_boundary(self):
        """The chunked reader must catch a token straddling a 1 MiB read
        boundary — otherwise a leak into a large active log could hide."""
        sentinel = mint_sentinel()
        token = sentinel.encode('utf-8')
        root = self._mk_tree()
        big = root / 'logs' / 'big.log'
        head = b'a' * (_SCAN_CHUNK - len(token) // 2)
        big.write_bytes(head + token + b'b' * 32)
        hits = scan_roots_for_sentinel([root], sentinel, time.time() - 1)
        self.assertEqual([h['path'] for h in hits], [str(big)])

    def test_production_roots_ignore_sandbox_env(self):
        """production_roots() must resolve under the REAL home even when the
        sandbox env vars point elsewhere — else the scanner inspects the
        sandbox and is blind to real leaks."""
        prev = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        bogus = tempfile.mkdtemp(prefix='ol-bogus-agents-root-')
        self.addCleanup(shutil.rmtree, bogus, ignore_errors=True)
        os.environ['OURLIBERTY_AGENTS_ROOT'] = bogus
        try:
            roots = production_roots()
        finally:
            if prev is None:
                os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
            else:
                os.environ['OURLIBERTY_AGENTS_ROOT'] = prev
        home_agents = Path.home() / 'agents'
        for r in roots:
            self.assertEqual(r.parent, home_agents,
                             f'{r} is not under the real {home_agents}')
            self.assertFalse(str(r).startswith(bogus),
                             f'{r} leaked through the sandbox env var')
        self.assertEqual({r.name for r in roots}, set(PRODUCTION_SUBDIRS))


class StampSelfCheckTest(unittest.TestCase):
    """Prove the instrumentation half: a stamped production log() call writes
    the sentinel, and undo cleanly restores the original helper."""

    def test_instrument_stamps_agent_runner_log_and_undoes(self):
        import agent_runner
        sentinel = mint_sentinel()
        tmp = tempfile.mkdtemp(prefix='ol-runtime-stamp-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        log_dir = Path(tmp) / 'logs'
        log_dir.mkdir()

        before = agent_runner.log
        prev_env = os.environ.get('OURLIBERTY_LOG_DIR')
        os.environ['OURLIBERTY_LOG_DIR'] = str(log_dir)
        undo = instrument_log_helpers(sentinel)
        try:
            agent_runner.log('selfcheck-agent', 'hello world')
        finally:
            for fn in undo:
                fn()
            if prev_env is None:
                os.environ.pop('OURLIBERTY_LOG_DIR', None)
            else:
                os.environ['OURLIBERTY_LOG_DIR'] = prev_env

        written = (log_dir / 'selfcheck-agent.log').read_text()
        self.assertIn(sentinel, written)
        self.assertIn('hello world', written)
        # The stamped line, had it leaked, would carry the token the scan
        # looks for — end-to-end proof the two halves compose.
        hits = scan_roots_for_sentinel([Path(tmp)], sentinel, time.time() - 5)
        self.assertEqual([h['path'] for h in hits],
                         [str(log_dir / 'selfcheck-agent.log')])
        # undo restored the original callable.
        self.assertIs(agent_runner.log, before)


class UnittestGateExitSelfCheck(unittest.TestCase):
    """End-to-end proof that the unittest bootstrap's atexit tripwire actually
    FAILS the process on an escaped write — the wiring the ScannerSelfCheck
    above does not exercise (it tests scan_roots_for_sentinel in isolation, not
    the atexit -> os._exit(1) path armed by scripts/tests/__init__.py).

    Both cases run a child interpreter with HOME pointed at a temp dir, so
    production_roots() (which resolves Path.home()/agents, deliberately not
    through env) sees the sandbox, never the real ~/agents. The child imports
    scripts.tests (arming the tripwire), optionally plants a file carrying the
    armed run sentinel under HOME/agents/logs, then exits normally — letting
    atexit run the scan."""

    _REPO_ROOT = Path(__file__).resolve().parents[2]

    def _run_child(self, plant_leak: bool):
        home = tempfile.mkdtemp(prefix='ol-gate-exit-home-')
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        prog = (
            'import scripts.tests as pkg\n'
            'from pathlib import Path\n'
            's = pkg._RUNTIME_TRIPWIRE_SENTINEL\n'
            'assert pkg._RUNTIME_TRIPWIRE_ARMED and s, "tripwire not armed"\n'
        )
        if plant_leak:
            prog += (
                'd = Path.home() / "agents" / "logs"\n'
                'd.mkdir(parents=True, exist_ok=True)\n'
                '(d / "escaped.log").write_text("leaked write " + s + "\\n")\n'
            )
        env = dict(os.environ)
        env['HOME'] = home
        env.pop('OURLIBERTY_TEST_RUN_SENTINEL', None)  # let the child mint fresh
        import subprocess
        return subprocess.run(
            [sys.executable, '-c', prog],
            cwd=str(self._REPO_ROOT), env=env,
            capture_output=True, text=True, timeout=120,
        )

    def test_clean_run_exits_zero(self):
        """No escaped write -> the atexit scan finds nothing -> normal exit."""
        res = self._run_child(plant_leak=False)
        self.assertEqual(
            res.returncode, 0,
            f'clean child exited {res.returncode}; stderr:\n{res.stderr}',
        )
        self.assertNotIn('PRODUCTION-WRITE TRIPWIRE', res.stderr)

    def test_escaped_write_fails_the_gate(self):
        """A sentinel-bearing file under the real-tree path -> the atexit scan
        flags it -> os._exit(1). This is the regression gate going red on the
        exact leak class the hardening exists to catch."""
        res = self._run_child(plant_leak=True)
        self.assertEqual(
            res.returncode, 1,
            f'leak child exited {res.returncode} (expected 1); '
            f'stderr:\n{res.stderr}',
        )
        self.assertIn('PRODUCTION-WRITE TRIPWIRE', res.stderr)
        self.assertIn('escaped.log', res.stderr)


if __name__ == '__main__':
    unittest.main()
