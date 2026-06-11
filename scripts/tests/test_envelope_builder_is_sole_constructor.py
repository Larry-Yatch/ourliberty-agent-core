#!/usr/bin/env python3
"""test_envelope_builder_is_sole_constructor.py — M1/S1 enforcement gate for
the Chain Context Durability spec (``agents/beacon/specs/chain-context-
durability.md`` §4 enforcement, §5 S1).

M1 centralizes inter-agent envelope construction behind the single sanctioned
constructor ``chain_envelope.build_chain_envelope`` so the drop-prone chain-
context whitelist (``forge_build_session_id``, ``reply_chat_id``, the loop
budgets, ``target_repo``/``pr_url``) is resolved in exactly one place. The two
production context-drops the spec was written for (the DAG-preflight REVISION
dead-end and the PR #412 no-session REVISION) both came from a hand-rolled
bare-dict envelope at one dispatch site silently forgetting a field.

This gate makes that regression class un-mergeable: every value handed to
``safe_write_inbox.safe_write_inbox(task_dict=...)`` in the scanned modules
MUST be a variable bound to a ``build_chain_envelope(...)`` result. A bare dict
literal — the historical hand-rolled form — fails the gate. The whitelist
itself lives in ``chain_envelope``; this test only enforces the *routing*, so
it stays green as the whitelist evolves.

Scope today is ``outbox_notifier.py`` (S1). The healers and ``inbox_watcher``
join ``SCANNED_MODULES`` in later steps of the sequence as they are migrated.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_envelope_builder_is_sole_constructor
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
_REPO_ROOT = _TESTS_DIR.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / 'scripts'

# The sanctioned constructor and the inbox-write sink it must feed.
_BUILDER_NAME = 'build_chain_envelope'
_WRITE_FUNC_NAME = 'safe_write_inbox'

# Modules whose dispatch/notify envelopes must route through the builder.
# S1 = outbox_notifier only; healers/inbox_watcher are added as later steps
# migrate them (each addition is the enforcement half of that step). S4 adds
# heal_pipeline_stall.py: its M4 recover-then-alert path now routes a
# no-session / DAG REVISION notify to Beacon, and that envelope is built via
# build_chain_envelope just like the notifier's.
SCANNED_MODULES: tuple[str, ...] = (
    'outbox_notifier.py',
    'heal_pipeline_stall.py',
)


def _is_write_call(node: ast.AST) -> bool:
    """True if ``node`` is a call to ``safe_write_inbox.safe_write_inbox(...)``
    (or a bare ``safe_write_inbox(...)`` if imported by name)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == _WRITE_FUNC_NAME
    if isinstance(func, ast.Name):
        return func.id == _WRITE_FUNC_NAME
    return False


def _is_builder_call(node: ast.AST) -> bool:
    """True if ``node`` is a call to ``build_chain_envelope(...)`` (by name or
    as ``chain_envelope.build_chain_envelope(...)``)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == _BUILDER_NAME
    if isinstance(func, ast.Attribute):
        return func.attr == _BUILDER_NAME
    return False


def _task_dict_arg(call: ast.Call) -> ast.AST | None:
    """Return the ``task_dict`` argument node of a write call, or None.

    Prefers the ``task_dict=`` keyword (the form every call site uses); falls
    back to the 2nd positional arg (target_agent, task_dict, ...) so a
    positional refactor can't slip past the gate.
    """
    for kw in call.keywords:
        if kw.arg == 'task_dict':
            return kw.value
    if len(call.args) >= 2:
        return call.args[1]
    return None


def _function_scopes(tree: ast.AST) -> list[ast.AST]:
    """Every function/module scope in the tree (module + each def). Used so a
    variable name like ``notify_task`` is resolved within its own function and
    can't be vouched for by a same-named build in an unrelated function."""
    scopes: list[ast.AST] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append(node)
    return scopes


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """Nodes belonging directly to ``scope`` — excludes anything inside a
    nested def so assignments/calls are attributed to the correct scope."""
    nested_ids: set[int] = set()
    for child in ast.walk(scope):
        if child is scope:
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(child):
                nested_ids.add(id(n))
    return [n for n in ast.walk(scope) if id(n) not in nested_ids]


def _assignments_in(nodes: list[ast.AST]) -> dict[str, list[ast.AST]]:
    """Map ``name -> [rhs value nodes]`` for simple ``x = ...`` / ``x: T = ...``
    assignments in ``nodes``."""
    out: dict[str, list[ast.AST]] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                out.setdefault(node.target.id, []).append(node.value)
    return out


def find_violations(source: str, filename: str = '<source>') -> list[str]:
    """Return human-readable violations: write calls whose ``task_dict`` is not
    a variable bound (in the same scope) solely to a ``build_chain_envelope``
    result. A bare dict literal — or a name assigned one — is a violation."""
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []

    for scope in _function_scopes(tree):
        own = _own_nodes(scope)
        assigns = _assignments_in(own)
        for node in own:
            if not _is_write_call(node):
                continue
            arg = _task_dict_arg(node)
            line = getattr(node, 'lineno', '?')
            where = f'{filename}:{line}'
            if arg is None:
                violations.append(
                    f'{where} — safe_write_inbox call has no resolvable '
                    'task_dict argument; the gate cannot prove it routes '
                    'through build_chain_envelope.'
                )
                continue
            if _is_builder_call(arg):
                continue  # inline build — allowed
            if isinstance(arg, ast.Dict):
                violations.append(
                    f'{where} — task_dict is a bare dict literal. Build the '
                    'envelope via build_chain_envelope(base, source, '
                    'carry={...}) so the chain-context whitelist is resolved '
                    'in one place.'
                )
                continue
            if isinstance(arg, ast.Name):
                rhs = assigns.get(arg.id, [])
                has_builder = any(_is_builder_call(r) for r in rhs)
                has_dict_literal = any(isinstance(r, ast.Dict) for r in rhs)
                if has_builder and not has_dict_literal:
                    continue
                if not rhs:
                    violations.append(
                        f'{where} — task_dict `{arg.id}` is not assigned in '
                        'this scope; the gate cannot prove it came from '
                        'build_chain_envelope.'
                    )
                elif has_dict_literal:
                    violations.append(
                        f'{where} — task_dict `{arg.id}` is assigned a bare '
                        'dict literal. Rename the literal to `<name>_base` and '
                        'build the envelope with build_chain_envelope.'
                    )
                else:
                    violations.append(
                        f'{where} — task_dict `{arg.id}` is not bound to a '
                        'build_chain_envelope result.'
                    )
                continue
            violations.append(
                f'{where} — task_dict is an unrecognized expression '
                f'({type(arg).__name__}); route it through '
                'build_chain_envelope so the gate can verify it.'
            )

    return violations


class EnvelopeBuilderSoleConstructorTest(unittest.TestCase):
    """Every dispatch/notify envelope in the scanned modules must be built by
    build_chain_envelope — never a hand-rolled bare dict."""

    def test_scanned_modules_route_every_envelope_through_builder(self):
        all_violations: list[str] = []
        for mod_name in SCANNED_MODULES:
            path = _SCRIPTS_DIR / mod_name
            self.assertTrue(
                path.exists(),
                f'scanned module not found: {path}',
            )
            all_violations.extend(
                find_violations(path.read_text(encoding='utf-8'), str(path))
            )
        if all_violations:
            self.fail(
                'Bare-dict envelope(s) bypassing build_chain_envelope '
                f'({len(all_violations)} site(s)) — this is the silent '
                'context-drop class M1 closes:\n  - '
                + '\n  - '.join(all_violations)
            )

    def test_outbox_notifier_has_builder_calls(self):
        # Guard against the gate passing vacuously (e.g. if a refactor renamed
        # the write helper and the scanner stopped finding any calls).
        src = (_SCRIPTS_DIR / 'outbox_notifier.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        builder_calls = sum(
            1 for n in ast.walk(tree) if _is_builder_call(n)
        )
        write_calls = sum(
            1 for n in ast.walk(tree) if _is_write_call(n)
        )
        self.assertGreater(
            builder_calls, 0,
            'expected build_chain_envelope calls in outbox_notifier.py',
        )
        self.assertGreaterEqual(
            builder_calls, write_calls,
            'every safe_write_inbox dispatch should have a corresponding '
            f'build_chain_envelope (builds={builder_calls}, '
            f'writes={write_calls})',
        )


class GateSelfCheckTest(unittest.TestCase):
    """Prove the scanner FAILS on a bare-dict envelope and PASSES on a
    builder-routed one, so a silent scanner regression can't make the gate
    permissive."""

    _BARE = (
        'def f():\n'
        '    notify_task = {\n'
        "        'task_id': 'notify-x',\n"
        "        'target_agent': 'forge',\n"
        '    }\n'
        '    safe_write_inbox.safe_write_inbox(\n'
        "        target_agent='forge', task_dict=notify_task,\n"
        "        source_agent='beacon', filename='x.json')\n"
    )

    _BARE_INLINE = (
        'def f():\n'
        '    safe_write_inbox.safe_write_inbox(\n'
        "        target_agent='forge',\n"
        "        task_dict={'task_id': 'x', 'target_agent': 'forge'},\n"
        "        source_agent='beacon', filename='x.json')\n"
    )

    _GOOD = (
        'def f():\n'
        '    notify_base = {\n'
        "        'task_id': 'notify-x',\n"
        "        'target_agent': 'forge',\n"
        '    }\n'
        '    notify_task = build_chain_envelope(\n'
        '        notify_base, data, carry={})\n'
        '    safe_write_inbox.safe_write_inbox(\n'
        "        target_agent='forge', task_dict=notify_task,\n"
        "        source_agent='beacon', filename='x.json')\n"
    )

    def test_flags_bare_dict_via_variable(self):
        self.assertTrue(find_violations(self._BARE))

    def test_flags_bare_dict_inline(self):
        self.assertTrue(find_violations(self._BARE_INLINE))

    def test_passes_builder_routed(self):
        self.assertEqual(find_violations(self._GOOD), [])

    def test_per_scope_isolation(self):
        # A good build in one function must NOT vouch for a bare dict in
        # another function that happens to reuse the variable name.
        src = (
            'def good():\n'
            '    notify_task = build_chain_envelope(b, d, carry={})\n'
            '    safe_write_inbox.safe_write_inbox(\n'
            "        target_agent='forge', task_dict=notify_task,\n"
            "        source_agent='beacon', filename='a.json')\n"
            'def bad():\n'
            "    notify_task = {'task_id': 'x', 'target_agent': 'forge'}\n"
            '    safe_write_inbox.safe_write_inbox(\n'
            "        target_agent='forge', task_dict=notify_task,\n"
            "        source_agent='beacon', filename='b.json')\n"
        )
        violations = find_violations(src)
        self.assertEqual(len(violations), 1, violations)
        self.assertIn('notify_task', violations[0])


if __name__ == '__main__':
    unittest.main()
