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

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_conftest_init_parity
"""
from __future__ import annotations

import ast
import os
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
            "tree is caught. The __init__ mirror sets the env var; the active "
            "session-end scan is the pytest fixture's teardown (the unittest "
            "bootstrap has no session-finish hook)."
        ),
    },
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
