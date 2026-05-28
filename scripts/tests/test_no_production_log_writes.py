#!/usr/bin/env python3
"""test_no_production_log_writes.py — regression gate for the indirect
log-write class that escaped PR #137's direct-write scanner.

Complementary to scripts/tests/test_no_production_path_leaks.py (PR #137).
That gate flags STRING LITERALS that look like production agent-state paths
in test files — closing the DIRECT-write surface. This gate flags MODULE
IMPORTS of production logging modules from test files that are not covered
by the autouse log-isolation fixture — closing the INDIRECT-write surface.

The 2026-05-27 11:56–11:57 MDT incident: tests calling production functions
(e.g., beacon_telegram_bot.call_beacon with a mocked subprocess) reached the
internal log() helper, which wrote sentinel strings (TIER_ONE_MARKER,
'tier2 stderr distinct token', '401 Unauthorized') into the live
~/agents/logs/beacon_telegram_bot.log file. PR #137's literal-pattern
scanner couldn't catch it — the test never named the production path,
the imported production module did.

Closure shape:
  - scripts/tests/conftest.py — autouse fixture sets OURLIBERTY_LOG_DIR
    per-test for everything under scripts/tests/. The 99% of cases.
  - This gate — flags test files OUTSIDE scripts/tests/ that import
    beacon_telegram_bot or agent_runner. Those test trees don't load the
    autouse fixture, so they'd silently regain the leak class.

Reviewer guidance for a flagged test outside scripts/tests/:
  - Move it under scripts/tests/ (preferred), OR
  - Mirror the conftest.py autouse fixture into the new test root's
    conftest.py, OR
  - If the test genuinely doesn't trigger log writes (rare — most reach
    log() via the bot's classify/retry paths), add the file to ALLOWLIST
    below with a one-line reason.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_no_production_log_writes
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent  # scripts/tests/
_REPO_ROOT = _TESTS_DIR.parent.parent

# Production modules whose log() helpers write to ~/agents/logs/ by
# default and whose tests must therefore run with OURLIBERTY_LOG_DIR
# redirected. Extend this set as new long-running daemons gain a
# resolve_log_dir() helper.
PROTECTED_MODULES: frozenset[str] = frozenset({
    'beacon_telegram_bot',
    'agent_runner',
})

# Test files outside scripts/tests/ that legitimately import a protected
# module without triggering log writes. Empty by default. Each addition
# must include a one-line justification readable by the next operator who
# runs this gate.
ALLOWLIST: dict[str, str] = {
    # 'tests/scripts/test_foo.py': 'imports beacon_telegram_bot only for
    #     a constant; no log() path reached',
}


def _is_protected_module_import(node: ast.AST) -> set[str]:
    """Return the set of PROTECTED_MODULES referenced by an AST import node.

    Handles `import X`, `import X as Y`, `from X import Y`, `from X.sub
    import Y`. Conditional imports inside function bodies are caught too
    because ast.walk visits every node.
    """
    hits: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            root = alias.name.split('.', 1)[0]
            if root in PROTECTED_MODULES:
                hits.add(root)
    elif isinstance(node, ast.ImportFrom):
        # `from X import Y` — X may be None for relative imports (`from .
        # import Y`); those aren't production modules so they're skipped.
        if node.module:
            root = node.module.split('.', 1)[0]
            if root in PROTECTED_MODULES:
                hits.add(root)
    return hits


def _scan_file_for_protected_imports(path: Path) -> set[str]:
    """Return the set of protected modules `path` imports, or an empty set."""
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except (OSError, SyntaxError):
        return set()
    hits: set[str] = set()
    for node in ast.walk(tree):
        hits |= _is_protected_module_import(node)
    return hits


def _discover_external_test_files() -> list[Path]:
    """Return every test_*.py under the repo that lives OUTSIDE
    scripts/tests/. We scan two well-known parents — `tests/` and
    `scripts/` — and avoid the `.git`, `node_modules`, and worktree
    directories by construction (we only descend into known test roots,
    not the whole repo)."""
    candidates: list[Path] = []
    # scripts/test_*.py (one-level — there's the legacy
    # scripts/test_regression_check.py shaped this way)
    scripts_dir = _REPO_ROOT / 'scripts'
    if scripts_dir.is_dir():
        for py in sorted(scripts_dir.glob('test_*.py')):
            candidates.append(py)
    # tests/**/test_*.py
    tests_dir = _REPO_ROOT / 'tests'
    if tests_dir.is_dir():
        for py in sorted(tests_dir.rglob('test_*.py')):
            candidates.append(py)
    return candidates


class ExternalTestProtectedImportTest(unittest.TestCase):
    """Fails when a test file OUTSIDE scripts/tests/ imports a production
    logging module. The autouse fixture in scripts/tests/conftest.py only
    redirects log writes for tests under that directory — any test tree
    sitting elsewhere reintroduces the indirect-write leak class."""

    def test_no_external_test_imports_protected_module(self):
        violations: list[str] = []
        for path in _discover_external_test_files():
            rel = path.relative_to(_REPO_ROOT)
            rel_str = str(rel)
            if rel_str in ALLOWLIST:
                continue
            hits = _scan_file_for_protected_imports(path)
            if hits:
                violations.append(
                    f'{rel_str} — imports {sorted(hits)!r} '
                    'without the scripts/tests/conftest.py autouse '
                    'log-isolation fixture. Options: (a) move the test '
                    'under scripts/tests/, (b) add a conftest.py mirroring '
                    'the autouse fixture to this test root, (c) add the '
                    'file to ALLOWLIST with a justification.'
                )
        if violations:
            self.fail(
                'Indirect-log-write isolation violation — '
                f'{len(violations)} external test(s) import production '
                'logging modules without log-isolation:\n'
                '  - ' + '\n  - '.join(violations)
            )


class GateSelfCheckTest(unittest.TestCase):
    """Self-checks: prove the AST scanner flags + skips correctly so a
    silent regression in the scanner can't make the gate accidentally
    permissive."""

    def test_scanner_flags_synthetic_import(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            synth = Path(td) / 'test_synthetic_indirect.py'
            synth.write_text(
                'import beacon_telegram_bot\n'
                'def test_x():\n'
                '    beacon_telegram_bot.log("hi")\n'
            )
            self.assertEqual(
                _scan_file_for_protected_imports(synth),
                {'beacon_telegram_bot'},
            )

    def test_scanner_flags_from_import(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            synth = Path(td) / 'test_synthetic_from.py'
            synth.write_text(
                'from agent_runner import log\n'
                'def test_x():\n'
                '    log("a", "b")\n'
            )
            self.assertEqual(
                _scan_file_for_protected_imports(synth),
                {'agent_runner'},
            )

    def test_scanner_flags_conditional_import_inside_function(self):
        # AST walk catches imports buried inside test bodies — a common
        # pattern for tests that want to defer the import until after
        # monkeypatching.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            synth = Path(td) / 'test_synthetic_nested.py'
            synth.write_text(
                'def test_x():\n'
                '    import agent_runner\n'
                '    agent_runner.classify_tier1_failure("", "")\n'
            )
            self.assertEqual(
                _scan_file_for_protected_imports(synth),
                {'agent_runner'},
            )

    def test_scanner_ignores_non_protected_import(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            synth = Path(td) / 'test_synthetic_ok.py'
            synth.write_text(
                'import json, os, sys\n'
                'from pathlib import Path\n'
                'def test_x():\n'
                '    assert True\n'
            )
            self.assertEqual(_scan_file_for_protected_imports(synth), set())

    def test_scanner_ignores_relative_import(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            synth = Path(td) / 'test_synthetic_rel.py'
            synth.write_text(
                'from . import helpers\n'
                'def test_x():\n'
                '    pass\n'
            )
            self.assertEqual(_scan_file_for_protected_imports(synth), set())


if __name__ == '__main__':
    unittest.main()
