#!/usr/bin/env python3
"""test_conftest_init_parity.py — drift guard between the pytest and unittest
test-isolation bootstraps.

WHY THIS TEST EXISTS
--------------------
The regression gate (scripts/test_regression_check.py) runs the suite via
``python3 -m unittest discover``, NOT pytest. Under unittest, conftest.py's
``@pytest.fixture(autouse=True)`` protections DO NOT RUN — pytest never loads
conftest.py outside a pytest session. The unittest path gets its
test-isolation from scripts/tests/__init__.py instead, which executes once at
package import time.

That asymmetry is a latent prod-leak class: any protection added to conftest.py
as an autouse fixture is SILENTLY INERT under the gate unless an equivalent is
also wired into __init__.py. Two such protections exist today and both are
currently mirrored:

  * production-log redirection      — env OURLIBERTY_LOG_DIR
  * live chain_events write blocking — env OURLIBERTY_DISABLE_LIVE_EMIT

(The 2026-06-02 leak — 200+ real-*/prod-* fixture rows upserted into the live
chain_events table from a build worktree with injected SUPABASE_* creds — is
the failure mode this parity protects against under the unittest gate.)

This test fails loudly the moment a THIRD autouse fixture lands in conftest.py
without a registered unittest mirror, so the two runners cannot drift apart.
Doctrine: docs/doctrine-of-doctrine.md — every rule earns an enforcement
mechanism; prose alone does not hold.

HOW IT WORKS
------------
1. AST-parse conftest.py *as source text* — it is never imported here, because
   pytest may be absent under the gate (importing conftest would ImportError on
   ``import pytest``). Enumerate every function decorated with
   ``@pytest.fixture(autouse=True)``.
2. Assert that set equals MIRRORED_AUTOUSE_FIXTURES, the explicit registry
   below. A new autouse fixture → this test fails until the author both
   registers it here AND wires its mirror into __init__.py.
3. Ground each registry entry: assert the conftest fixture and __init__.py both
   reference the env var named in the registry.
4. Assert each mirror is actually live in *this* process (the unittest path):
   the env var is set after ``import scripts.tests``, and the chain-event guard
   makes chain_event_emit._get_client() return None.
5. Cover Gap A separately (MIRRORED_IMPORT_TIME_REDIRECTS): the OURLIBERTY_*_ROOT
   sandbox is an import-time, module-level redirect — NOT an autouse fixture — so
   AST-assert that conftest.py AND __init__.py each actually set those vars at
   import, and that OURLIBERTY_AGENTS_ROOT is live and non-~/agents in process.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_conftest_init_parity
"""
from __future__ import annotations

import ast
import os
import shutil
import sys
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_SCRIPTS = _TESTS_DIR.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

_CONFTEST = _TESTS_DIR / "conftest.py"
_INIT = _TESTS_DIR / "__init__.py"


# ---------------------------------------------------------------------------
# Registry — the single source of truth for "which conftest autouse fixture is
# mirrored by which __init__.py mechanism". To add a new autouse fixture to
# conftest.py you MUST add a row here AND wire the env var into __init__.py.
# ---------------------------------------------------------------------------
MIRRORED_AUTOUSE_FIXTURES: dict[str, dict[str, str]] = {
    "_isolate_production_logs": {
        "env_var": "OURLIBERTY_LOG_DIR",
        "purpose": "redirect production log writes into a tmp dir",
    },
    "_block_live_chain_event_emit": {
        "env_var": "OURLIBERTY_DISABLE_LIVE_EMIT",
        "purpose": "block live Supabase chain_events writes during tests",
    },
    "_production_write_runtime_tripwire": {
        "env_var": "OURLIBERTY_TEST_RUN_SENTINEL",
        "purpose": (
            "runtime backstop: stamp a run sentinel into production log() "
            "writes so a write that escapes the sandbox to the real ~/agents "
            "tree is caught. The __init__ mirror (_arm_runtime_tripwire) now "
            "runs the full session lifecycle under the unittest gate — instrument "
            "at package import, scan the real tree at process exit via atexit — "
            "the counterpart to the pytest fixture's teardown."
        ),
    },
}


# ---------------------------------------------------------------------------
# Registry — import-time (module-level) redirects. These are NOT autouse
# fixtures, so MIRRORED_AUTOUSE_FIXTURES above does not — and must not — cover
# them (adding them there would wrongly trip test_no_unregistered_autouse_
# fixtures). The Gap A OURLIBERTY_*_ROOT sandbox is established at MODULE IMPORT
# in conftest.py AND __init__.py because production modules bind
#   AGENTS_ROOT = os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents')
# AT IMPORT; a function-scoped fixture runs after collection has already frozen
# that constant to the live tree (the filesystem analog of the 2026-06-02
# chain_events leak). Each var below must be SET at import time by BOTH
# bootstraps, or the unittest gate can silently leak inbox/blackboard/state
# writes to the real ~/agents tree.
# ---------------------------------------------------------------------------
MIRRORED_IMPORT_TIME_REDIRECTS: dict[str, str] = {
    "OURLIBERTY_AGENTS_ROOT": (
        "sandbox the AGENTS_ROOT family so import-time-frozen path constants "
        "cannot write inbox/blackboard/state to the real ~/agents tree"
    ),
    "OURLIBERTY_WORKTREES_ROOT": (
        "sandbox the worktrees root alongside AGENTS_ROOT"
    ),
}


def _is_autouse_fixture_decorator(dec: ast.expr) -> bool:
    """True if ``dec`` is ``@pytest.fixture(autouse=True)`` or
    ``@fixture(autouse=True)`` (handles both import styles)."""
    if not isinstance(dec, ast.Call):
        return False
    func = dec.func
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        return False
    if name != "fixture":
        return False
    for kw in dec.keywords:
        if kw.arg == "autouse" and isinstance(kw.value, ast.Constant) \
                and kw.value.value is True:
            return True
    return False


def _autouse_fixtures_in_conftest() -> set[str]:
    """Parse conftest.py source and return the set of autouse fixture names.

    conftest.py is read as text and AST-parsed; it is never imported (it does
    ``import pytest`` at module top, which is not guaranteed present under the
    unittest gate)."""
    tree = ast.parse(_CONFTEST.read_text(), filename=str(_CONFTEST))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_is_autouse_fixture_decorator(d) for d in node.decorator_list):
                found.add(node.name)
    return found


def _environ_subscript_key(target: ast.expr) -> str | None:
    """If ``target`` is ``os.environ['<literal>']`` return the literal key."""
    if not isinstance(target, ast.Subscript):
        return None
    val = target.value
    if not (isinstance(val, ast.Attribute) and val.attr == "environ"
            and isinstance(val.value, ast.Name) and val.value.id == "os"):
        return None
    key = target.slice
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    return None


def _environ_setdefault_key(call: ast.Call) -> str | None:
    """If ``call`` is ``os.environ.setdefault('<literal>', ...)`` return key."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "setdefault"):
        return None
    obj = func.value
    if not (isinstance(obj, ast.Attribute) and obj.attr == "environ"
            and isinstance(obj.value, ast.Name) and obj.value.id == "os"):
        return None
    if call.args and isinstance(call.args[0], ast.Constant) \
            and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def _module_level_env_setters(path: Path) -> set[str]:
    """Env-var names the file SETS on ``os.environ`` at IMPORT time — via
    ``os.environ['X'] = ...`` or ``os.environ.setdefault('X', ...)`` — counting
    only statements that run at module import (excluding anything inside a
    function/fixture body, which does not run at import).

    Crucially this is an AST check, not a substring scan: a comment that merely
    *mentions* the var does NOT count, so deleting the real redirect while
    leaving the explanatory comment still trips the parity assertions."""
    tree = ast.parse(path.read_text(), filename=str(path))

    func_spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_spans.append(
                (node.lineno, getattr(node, "end_lineno", node.lineno))
            )

    def _runs_at_import(lineno: int) -> bool:
        return not any(lo <= lineno <= hi for lo, hi in func_spans)

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                name = _environ_subscript_key(tgt)
                if name is not None and _runs_at_import(node.lineno):
                    found.add(name)
        elif isinstance(node, ast.Call):
            name = _environ_setdefault_key(node)
            if name is not None and _runs_at_import(node.lineno):
                found.add(name)
    return found


class ConftestInitParityTest(unittest.TestCase):
    """Fails loudly if a conftest autouse protection has no __init__.py mirror."""

    def test_no_unregistered_autouse_fixtures(self):
        """Every autouse fixture in conftest.py must be in the registry — the
        forcing function that makes an author consciously mirror it into the
        unittest bootstrap."""
        actual = _autouse_fixtures_in_conftest()
        registered = set(MIRRORED_AUTOUSE_FIXTURES)
        unregistered = actual - registered
        self.assertFalse(
            unregistered,
            "conftest.py has autouse fixture(s) with NO registered unittest "
            f"mirror: {sorted(unregistered)}. Under the regression gate "
            "(python3 -m unittest) these fixtures DO NOT RUN. Add an equivalent "
            "protection to scripts/tests/__init__.py and register it in "
            "MIRRORED_AUTOUSE_FIXTURES, or the gate runs without this protection.",
        )
        stale = registered - actual
        self.assertFalse(
            stale,
            f"MIRRORED_AUTOUSE_FIXTURES names fixture(s) {sorted(stale)} that no "
            "longer exist in conftest.py. Remove the stale registry row(s).",
        )

    def test_conftest_fixture_references_its_env_var(self):
        """Ground each registry row: the conftest fixture source must mention
        the env var the registry claims it sets (catches a copy-paste row that
        points at the wrong mechanism)."""
        body = _CONFTEST.read_text()
        for fixture, meta in MIRRORED_AUTOUSE_FIXTURES.items():
            with self.subTest(fixture=fixture):
                self.assertIn(
                    meta["env_var"], body,
                    f"conftest.py does not reference {meta['env_var']!r}, which "
                    f"the registry maps to fixture {fixture!r}.",
                )

    def test_init_mirrors_each_env_var(self):
        """__init__.py (the unittest bootstrap) must set every mirrored env var."""
        body = _INIT.read_text()
        for fixture, meta in MIRRORED_AUTOUSE_FIXTURES.items():
            with self.subTest(fixture=fixture):
                self.assertIn(
                    meta["env_var"], body,
                    f"scripts/tests/__init__.py does not set {meta['env_var']!r}, "
                    f"the unittest mirror of conftest fixture {fixture!r}. The "
                    "gate would run without this protection.",
                )

    def test_mirror_env_vars_are_live_in_process(self):
        """Importing scripts.tests must leave every mirror env var set — proves
        the unittest bootstrap actually ran, not just that the source mentions
        the var."""
        import scripts.tests  # noqa: F401  (idempotent; runs __init__.py once)
        self.assertTrue(
            os.environ.get("OURLIBERTY_LOG_DIR"),
            "OURLIBERTY_LOG_DIR is unset; production log redirection is not live.",
        )
        log_dir = os.environ["OURLIBERTY_LOG_DIR"]
        self.assertNotIn(
            "agents/logs", log_dir,
            f"OURLIBERTY_LOG_DIR points at a live-looking path: {log_dir!r}.",
        )
        self.assertEqual(
            os.environ.get("OURLIBERTY_DISABLE_LIVE_EMIT"), "1",
            "OURLIBERTY_DISABLE_LIVE_EMIT != '1'; live chain_events writes are "
            "not blocked.",
        )

    def test_import_time_redirects_set_in_both_bootstraps(self):
        """Gap A's OURLIBERTY_*_ROOT redirect is the spec's PRIMARY mechanism,
        but it is import-time/module-level, not an autouse fixture — so the
        MIRRORED_AUTOUSE_FIXTURES checks above never touch it. Assert (via AST,
        not substring) that BOTH conftest.py and __init__.py actually SET each
        registered var at import time. Removing the redirect from either file
        fails here even if the explanatory comment is left behind."""
        conftest_setters = _module_level_env_setters(_CONFTEST)
        init_setters = _module_level_env_setters(_INIT)
        for env_var in MIRRORED_IMPORT_TIME_REDIRECTS:
            with self.subTest(env_var=env_var, file="conftest.py"):
                self.assertIn(
                    env_var, conftest_setters,
                    f"conftest.py no longer sets {env_var!r} at import time "
                    "(module-level os.environ assignment/setdefault). The Gap A "
                    "redirect is gone for the pytest path; an import-time-frozen "
                    "AGENTS_ROOT constant can leak inbox/blackboard/state writes "
                    "to the real ~/agents tree.",
                )
            with self.subTest(env_var=env_var, file="__init__.py"):
                self.assertIn(
                    env_var, init_setters,
                    f"scripts/tests/__init__.py no longer sets {env_var!r} at "
                    "import time. The Gap A redirect is gone for the unittest "
                    "gate (python3 -m unittest) — the exact path the regression "
                    "gate uses — so a frozen AGENTS_ROOT can leak writes to the "
                    "real ~/agents tree with nothing failing.",
                )

    def test_agents_root_redirect_is_live_and_sandboxed(self):
        """Runtime counterpart for Gap A (mirrors test_mirror_env_vars_are_live_
        in_process for OURLIBERTY_LOG_DIR): after ``import scripts.tests`` the
        unittest bootstrap must have left OURLIBERTY_AGENTS_ROOT pointing at a
        sandbox path, NOT the real ~/agents tree. Fails if the __init__.py
        redirect is removed (var unset) or resolves back into the live root."""
        import scripts.tests  # noqa: F401  (idempotent; runs __init__.py once)
        agents_root = os.environ.get("OURLIBERTY_AGENTS_ROOT")
        self.assertTrue(
            agents_root,
            "OURLIBERTY_AGENTS_ROOT is unset after importing scripts.tests; the "
            "Gap A import-time sandbox is not live, so import-time AGENTS_ROOT "
            "captures resolve to the production default /home/larry/agents.",
        )
        real_agents = str(Path.home() / "agents")
        resolved = str(Path(agents_root).resolve())
        self.assertFalse(
            resolved == real_agents or resolved.startswith(real_agents + os.sep),
            f"OURLIBERTY_AGENTS_ROOT resolves into the REAL agents tree "
            f"({resolved!r} under {real_agents!r}); test writes would hit "
            "production instead of the sandbox.",
        )

    def test_runtime_tripwire_armed_under_unittest_gate(self):
        """The whole point of the unittest mirror: under the bare unittest gate
        (the runner the regression check uses), importing scripts.tests must ARM
        the session-end scan — not merely set the env var. Asserts the bootstrap
        minted a sentinel and flagged itself armed. Under pytest the conftest
        session fixture owns the scan instead, so this assertion is skipped."""
        import scripts.tests as pkg  # noqa: F401  (idempotent; runs __init__)
        if "pytest" in sys.modules:
            self.skipTest("pytest path: scan owned by conftest session fixture")
        self.assertTrue(
            getattr(pkg, "_RUNTIME_TRIPWIRE_ARMED", False),
            "scripts.tests did not arm the runtime production-write tripwire "
            "under the unittest gate; a write escaping the sandbox to the real "
            "~/agents tree would go uncaught (the exact leak class that polluted "
            "~/agents/blackboard with fixture sentinels for days).",
        )
        sentinel = getattr(pkg, "_RUNTIME_TRIPWIRE_SENTINEL", None)
        self.assertTrue(
            sentinel and sentinel.startswith("OL-TEST-RUN-SENTINEL-"),
            f"armed but the run sentinel looks wrong: {sentinel!r}",
        )

    def test_runtime_tripwire_instrumentation_is_live(self):
        """Prove the stamp half is actually wired under this process: a
        production log() helper writes the run sentinel. Without the sys.path
        setup in _arm_runtime_tripwire, instrument_log_helpers' bare
        `import agent_runner` would fail and silently no-op the stamping,
        leaving the scan blind. Runner-agnostic: asserts the sentinel PREFIX,
        which both the unittest and pytest sentinels carry."""
        import tempfile
        from pathlib import Path

        import agent_runner

        tmp = tempfile.mkdtemp(prefix="ol-parity-stamp-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        prev = os.environ.get("OURLIBERTY_LOG_DIR")
        os.environ["OURLIBERTY_LOG_DIR"] = str(log_dir)
        try:
            agent_runner.log("parity-stamp-agent", "instrumentation check")
        finally:
            if prev is None:
                os.environ.pop("OURLIBERTY_LOG_DIR", None)
            else:
                os.environ["OURLIBERTY_LOG_DIR"] = prev
        written = (log_dir / "parity-stamp-agent.log").read_text()
        self.assertIn(
            "OL-TEST-RUN-SENTINEL-", written,
            "agent_runner.log did not carry the run sentinel — the production "
            "log() helpers are not instrumented, so the tripwire scan cannot "
            "attribute (and therefore cannot catch) an escaped write.",
        )

    def test_chain_event_client_is_blocked(self):
        """The end-to-end effect of the chain-event mirror: _get_client() must
        return None in this process, so no test can reach the live table —
        regardless of whether conftest's attribute monkeypatch ran."""
        import chain_event_emit as cee  # noqa: E402
        cee.reset_client_for_testing()
        self.assertIsNone(
            cee._get_client(),
            "chain_event_emit._get_client() returned a live client under the "
            "test bootstrap — the OURLIBERTY_DISABLE_LIVE_EMIT guard is not "
            "holding. Any test calling emit_event could write the prod table.",
        )


if __name__ == "__main__":
    unittest.main()
