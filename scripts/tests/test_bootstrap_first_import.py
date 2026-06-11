#!/usr/bin/env python3
"""test_bootstrap_first_import.py — regression gate that keeps the per-module
test sandbox loader-agnostic.

WHY THIS GATE EXISTS
--------------------
scripts/tests/__init__.py only engages the sandbox when the ``scripts.tests``
PACKAGE is imported (the dotted / pytest loaders). But the regression gate and
every Forge/Mirror dev loop run

    python3 -m unittest discover -s scripts/tests

which imports each ``test_*.py`` as a TOP-LEVEL module and NEVER runs the
package ``__init__`` — so the sandbox never armed and production log/alert/state
writes leaked to the real ~/agents tree. That leak class survived PR #412, #428
and #436 (false ``transcript-not-persisted`` CRITICAL DMs citing mock /tmp
paths, fixture quota events, even ``../../../../etc/pwned`` path-traversal
fixture lines in the live inbox_watcher.log).

The fix makes every ``test_*.py`` import ``_bootstrap`` FIRST, so the sandbox
engages before any other import can freeze a path constant — under ANY loader.
This gate enforces that invariant so a newly added test file cannot silently
reintroduce the leak: it AST-parses every ``test_*.py`` and fails any whose
first import is not the ``_bootstrap`` import.

WHAT COUNTS AS THE BOOTSTRAP IMPORT
-----------------------------------
The loader-agnostic form the sweep installs:

    try:
        from . import _bootstrap  # noqa: F401
    except ImportError:
        import _bootstrap  # noqa: F401

(a ``Try`` whose body imports ``_bootstrap``). A bare ``import _bootstrap`` /
``from . import _bootstrap`` is also accepted. ``from __future__`` imports are
permitted to PRECEDE it: they are compiler directives that read no env/paths and
must syntactically come first, so they cannot defeat the sandbox.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_bootstrap_first_import
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import ast
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent  # scripts/tests/


def _is_docstring(node: ast.AST, index: int) -> bool:
    """True if ``node`` is the module docstring (first statement, a bare str)."""
    return (
        index == 0
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_future_import(node: ast.AST) -> bool:
    return isinstance(node, ast.ImportFrom) and node.module == "__future__"


def _is_bootstrap_import(node: ast.AST) -> bool:
    """True for ``import _bootstrap`` or ``from . import _bootstrap`` (the
    latter has module=None, level=1, names containing ``_bootstrap``)."""
    if isinstance(node, ast.Import):
        return any(a.name == "_bootstrap" for a in node.names)
    if isinstance(node, ast.ImportFrom) and node.module != "__future__":
        return any(a.name == "_bootstrap" for a in node.names)
    return False


def _is_bootstrap_try(node: ast.AST) -> bool:
    """True for the try/except wrapper whose body imports ``_bootstrap``."""
    if not isinstance(node, ast.Try):
        return False
    return any(_is_bootstrap_import(stmt) for stmt in node.body)


def first_import_is_bootstrap(text: str) -> tuple[bool, str]:
    """Return (ok, detail). ``ok`` is True iff the first non-docstring,
    non-``__future__`` top-level statement is the ``_bootstrap`` import (bare or
    try-wrapped). ``detail`` describes the offending first statement when not."""
    tree = ast.parse(text)
    for index, node in enumerate(tree.body):
        if _is_docstring(node, index) or _is_future_import(node):
            continue
        if _is_bootstrap_try(node) or _is_bootstrap_import(node):
            return True, ""
        kind = type(node).__name__
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ", ".join(
                a.name for a in node.names  # type: ignore[attr-defined]
            )
            kind = f"import ({names})"
        return False, (
            f"first non-docstring/non-__future__ statement is {kind} at "
            f"line {node.lineno}, not the _bootstrap import"
        )
    return False, "no statements after the docstring — _bootstrap import missing"


def _discover_test_files() -> list[Path]:
    return sorted(_TESTS_DIR.glob("test_*.py"))


class BootstrapFirstImportTest(unittest.TestCase):
    """Fails when any scripts/tests/test_*.py does not import _bootstrap first.
    Without that first import, the discover loader (``python3 -m unittest
    discover``) imports the test module top-level and the sandbox never
    engages — reintroducing the production-write leak class."""

    def test_every_test_file_imports_bootstrap_first(self):
        violations: list[str] = []
        for path in _discover_test_files():
            ok, detail = first_import_is_bootstrap(path.read_text())
            if not ok:
                violations.append(f"{path.name} — {detail}")
        if violations:
            self.fail(
                "Bootstrap-first-import violation — "
                f"{len(violations)} test file(s) do not import _bootstrap as "
                "their first import (the sandbox would not engage under "
                "`python3 -m unittest discover`):\n  - "
                + "\n  - ".join(violations)
                + "\n\nFix: add, as the first import (after the docstring and "
                "any `from __future__` imports):\n"
                "    try:\n"
                "        from . import _bootstrap  # noqa: F401\n"
                "    except ImportError:\n"
                "        import _bootstrap  # noqa: F401"
            )


class GateSelfCheckTest(unittest.TestCase):
    """Self-checks proving the AST detector flags + accepts correctly, so a
    silent regression in the detector can't make the gate permissive."""

    def test_accepts_try_wrapped_bootstrap_after_future(self):
        src = (
            '"""doc."""\n'
            "from __future__ import annotations\n"
            "try:\n"
            "    from . import _bootstrap  # noqa: F401\n"
            "except ImportError:\n"
            "    import _bootstrap  # noqa: F401\n"
            "import os\n"
        )
        ok, detail = first_import_is_bootstrap(src)
        self.assertTrue(ok, detail)

    def test_accepts_bare_bootstrap_import(self):
        src = '"""doc."""\nimport _bootstrap\nimport os\n'
        ok, _ = first_import_is_bootstrap(src)
        self.assertTrue(ok)

    def test_accepts_bootstrap_with_no_docstring(self):
        src = "import _bootstrap\nimport os\n"
        ok, _ = first_import_is_bootstrap(src)
        self.assertTrue(ok)

    def test_flags_missing_bootstrap(self):
        src = '"""doc."""\nfrom __future__ import annotations\nimport os\n'
        ok, detail = first_import_is_bootstrap(src)
        self.assertFalse(ok)
        self.assertIn("import", detail)

    def test_flags_production_import_before_bootstrap(self):
        src = (
            '"""doc."""\n'
            "import agent_runner\n"
            "import _bootstrap\n"
        )
        ok, detail = first_import_is_bootstrap(src)
        self.assertFalse(ok)
        self.assertIn("agent_runner", detail)

    def test_flags_empty_module(self):
        ok, _ = first_import_is_bootstrap('"""only a docstring."""\n')
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
