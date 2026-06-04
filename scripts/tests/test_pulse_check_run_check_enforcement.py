#!/usr/bin/env python3
"""Enforcement: every pulse_check_<id>.py must wrap main() in run_check.

The heartbeat/liveness layer only works if EVERY check actually emits a signal.
A check whose ``__main__`` calls ``sys.exit(main())`` directly — instead of
``sys.exit(run_check('<id>', main, ...))`` — would run fine but never touch its
heartbeat, so the staleness watcher would flag it dark forever (or, worse, a new
check would silently ship with no liveness at all). This test fails loudly if
any check drops the wrapper, so the guarantee can't regress.

It inspects the ``if __name__ == '__main__'`` block by AST (not text), and
specifically requires run_check to be imported from pulse_check_heartbeat there.
Several checks also define an unrelated *local* ``run_check`` domain function;
keying on the import is what tells the liveness wrapper apart from those.

Run:
    cd ~/agent-core && python3 -m unittest \
        scripts.tests.test_pulse_check_run_check_enforcement
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent


def _check_scripts() -> list[Path]:
    return sorted(
        p for p in _SCRIPTS.glob('pulse_check_*.py')
        if p.name != 'pulse_check_heartbeat.py'
    )


def _check_id(path: Path) -> str:
    return path.stem[len('pulse_check_'):]


def _main_block(tree: ast.Module) -> ast.If | None:
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        # __name__ == '__main__'
        if (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == '__name__'
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == '__main__'):
            return node
    return None


def _imports_run_check_from_heartbeat(block: ast.If) -> bool:
    for node in ast.walk(block):
        if isinstance(node, ast.ImportFrom) \
                and node.module == 'pulse_check_heartbeat':
            if any(alias.name == 'run_check' for alias in node.names):
                return True
    return False


def _run_check_call_id(block: ast.If) -> str | None:
    """First-positional-arg string of a run_check(...) call in the block."""
    for node in ast.walk(block):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == 'run_check' and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
    return None


class RunCheckEnforcementTest(unittest.TestCase):
    def test_scripts_present(self):
        scripts = _check_scripts()
        self.assertTrue(scripts, 'no pulse_check_<id>.py scripts found')

    def test_every_check_wraps_main_in_run_check(self):
        for path in _check_scripts():
            with self.subTest(check=_check_id(path)):
                tree = ast.parse(path.read_text(encoding='utf-8'))
                block = _main_block(tree)
                self.assertIsNotNone(
                    block, f'{path.name} has no `if __name__ == "__main__"` block',
                )
                self.assertTrue(
                    _imports_run_check_from_heartbeat(block),
                    f'{path.name} __main__ does not import run_check from '
                    'pulse_check_heartbeat — its liveness heartbeat will never '
                    'fire',
                )
                called_id = _run_check_call_id(block)
                self.assertIsNotNone(
                    called_id,
                    f'{path.name} __main__ does not call run_check(<id>, main, '
                    '...)',
                )
                self.assertEqual(
                    called_id, _check_id(path),
                    f'{path.name} wraps run_check with id {called_id!r}, '
                    f'expected {_check_id(path)!r}',
                )


if __name__ == '__main__':
    unittest.main()
