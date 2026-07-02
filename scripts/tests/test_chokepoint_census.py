#!/usr/bin/env python3
"""test_chokepoint_census.py — forward-guard meta-gate for the test-jail choke
points (Layer B).

WHY THIS GATE EXISTS
--------------------
PR-2 wraps every production side-effect that a test could reach un-mocked
(Supabase client build, ``claude`` spawn, Telegram ``sendMessage`` POST,
destructive ``gh`` write) in a call-time ``refuse_under_test()`` guard — or, for
Supabase, behind the single guarded ``supabase_factory.get_supabase_client``.
That closes today's leaks. But nothing stops a FUTURE commit from adding a NEW
unguarded sink in a fresh module and silently reopening the hole.

This gate is the structural backstop. It AST-scans every production module in
``scripts/`` (NOT the tests/ tree) for the four sink SHAPES and fails if one
appears in a file that is not on that channel's allowlist. The allowlist is the
small, reviewed set of modules that legitimately own the sink (each with a
runtime ``refuse_under_test`` guard at the call site) plus the one guarded
Supabase factory. Adding a sink anywhere else fails the suite — forcing the
author to either route through the guarded path or consciously extend the
allowlist (which surfaces in review).

The static scan is intentionally conservative: it matches the literal argv
shapes the codebase actually uses (``['claude', '-p', ...]`` / ``['gh', 'pr',
'merge', ...]`` / a ``create_client(`` call / a ``'/sendMessage'`` URL string).
AST parsing means docstrings and comments that merely MENTION these tokens are
ignored — only real code is flagged.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_chokepoint_census
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import ast
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent  # scripts/

# Per-channel allowlist: filenames permitted to contain the sink shape because
# they are the guarded owner (a runtime refuse_under_test() at the call site) or
# the single guarded factory. A NEW file with the shape fails the gate.
ALLOWLIST: dict[str, set[str]] = {
    'supabase': {
        # The ONE guarded entrypoint; every other module imports from here.
        'supabase_factory.py',
    },
    'claude': {
        'agent_runner.py',          # run_claude Popen + tier2 subprocess.run
        'ceo_digest_generator.py',  # generate_ceo_voice
        'dashboard_api.py',         # _cleanup_review_verify_uncertain
        'missions_narrator.py',     # generate_briefing_voice (Phase 4 Narrator)
        'pulse_check_retrospective_author.py',  # _claude_classify (bounded retro author, #784)
    },
    'telegram': {
        'agent_telegram_bot.py',    # telegram_send
        'beacon_telegram_bot.py',   # telegram_send + _send_alert_dm
    },
    'gh-write': {
        'heal_pr_auto_merge.py',    # add_stalled_label + merge_pr
        'outbox_notifier.py',       # _auto_merge_pr + mirror-review status POST
    },
}

_CLAUDE_PAID_FLAGS = {'-p', '--print'}
_GH_DESTRUCTIVE_TOKENS = {'--add-label', '--delete-branch', '--delete'}
_GH_API_FIELD_FLAGS = {'-f', '-F', '--field', '--raw-field'}
_GH_API_WRITE_METHODS = {'POST', 'PATCH', 'PUT', 'DELETE'}


def _str_const(node: ast.AST):
    """Return the str value if ``node`` is a string constant, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _list_consts(node: ast.List) -> list:
    """Per-element string constant (or None for a non-constant element)."""
    return [_str_const(e) for e in node.elts]


def _claude_spawn_detail(consts: list):
    """Detail string if ``consts`` is a paid ``claude`` argv list, else None.

    Matches ``['claude', ..., '-p'|'--print', ...]`` — the metered Claude-Code
    spawn. ``claude auth login/status`` (no -p/--print) is intentionally NOT
    census-tracked: it is guarded at runtime but is not the money/leak sink the
    census forward-guards."""
    if not consts or consts[0] != 'claude':
        return None
    if any(c in _CLAUDE_PAID_FLAGS for c in consts if c is not None):
        return "claude paid spawn (['claude', ..., '-p'/'--print'])"
    return None


def _gh_write_detail(consts: list):
    """Detail string if ``consts`` is a DESTRUCTIVE ``gh`` argv list, else None.

    Read-only ``gh`` (``pr view`` / ``pr list`` / ``auth token``) is exempt."""
    if not consts or consts[0] != 'gh':
        return None
    present = {c for c in consts if c is not None}
    if 'merge' in present:
        return 'gh ... merge (destructive)'
    hit = present & _GH_DESTRUCTIVE_TOKENS
    if hit:
        return f'gh ... {sorted(hit)[0]} (destructive)'
    if 'api' in present:
        if present & _GH_API_FIELD_FLAGS:
            return 'gh api -f/-F field write (POST)'
        for i, c in enumerate(consts):
            if c == '-X' and i + 1 < len(consts) and \
                    consts[i + 1] in _GH_API_WRITE_METHODS:
                return f'gh api -X {consts[i + 1]} (destructive)'
    return None


def _docstring_nodes(tree: ast.AST) -> set:
    """Object ids of every module/class/function docstring Constant node, so a
    docstring that merely MENTIONS a sink token is not flagged."""
    ids: set = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, 'body', None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def find_sinks(text: str) -> list[tuple[str, int, str]]:
    """Return (channel, lineno, detail) for every chokepoint sink shape in
    ``text``. AST-based, so docstrings/comments mentioning the tokens are
    ignored."""
    tree = ast.parse(text)
    docstrings = _docstring_nodes(tree)
    findings: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        # supabase — a direct create_client(...) call.
        if isinstance(node, ast.Call):
            fn = node.func
            name = None
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name == 'create_client':
                findings.append(
                    ('supabase', node.lineno, 'create_client(...) call'))
        # claude / gh-write — argv list literals.
        if isinstance(node, ast.List):
            consts = _list_consts(node)
            detail = _claude_spawn_detail(consts)
            if detail:
                findings.append(('claude', node.lineno, detail))
            detail = _gh_write_detail(consts)
            if detail:
                findings.append(('gh-write', node.lineno, detail))
        # telegram — a Telegram sendMessage endpoint URL string (matches the
        # constant part of f"{API}/sendMessage" too).
        sval = _str_const(node)
        if sval is not None and '/sendMessage' in sval \
                and id(node) not in docstrings:
            findings.append(('telegram', node.lineno, "'/sendMessage' URL"))
    return findings


def _production_modules() -> list[Path]:
    """Top-level scripts/*.py (production). Excludes the tests/ tree."""
    return sorted(_SCRIPTS_DIR.glob('*.py'))


class ChokepointCensusTest(unittest.TestCase):
    """Fails if a chokepoint sink shape appears in a production module that is
    not on that channel's allowlist — i.e. a new, un-reviewed unguarded sink."""

    def test_no_unallowlisted_chokepoint_sinks(self):
        violations: list[str] = []
        for path in _production_modules():
            try:
                findings = find_sinks(path.read_text())
            except SyntaxError as e:  # pragma: no cover - corrupt source
                violations.append(f'{path.name} — failed to parse: {e}')
                continue
            for channel, lineno, detail in findings:
                if path.name in ALLOWLIST.get(channel, set()):
                    continue
                violations.append(
                    f'{path.name}:{lineno} — [{channel}] {detail}')
        if violations:
            self.fail(
                'Chokepoint census violation — '
                f'{len(violations)} unguarded sink(s) found outside the '
                'allowlist:\n  - '
                + '\n  - '.join(sorted(violations))
                + '\n\nFix: route the call through the guarded path '
                '(supabase_factory.get_supabase_client / test_isolation_guard.'
                'gh_write) and add a refuse_under_test() guard at the call '
                'site, OR — if this is a legitimately new sink-owning module — '
                'add the filename to ALLOWLIST[<channel>] in this test (which '
                'surfaces the new sink in review).'
            )


class GateSelfCheckTest(unittest.TestCase):
    """Self-checks proving the AST detector flags + accepts correctly, so a
    silent regression in the detector can't make the gate permissive."""

    def test_flags_create_client(self):
        src = 'def f():\n    return create_client(url, key)\n'
        found = find_sinks(src)
        self.assertTrue(any(c == 'supabase' for c, _, _ in found))

    def test_flags_supabase_attribute_call(self):
        src = 'def f():\n    return supabase.create_client(url, key)\n'
        found = find_sinks(src)
        self.assertTrue(any(c == 'supabase' for c, _, _ in found))

    def test_flags_paid_claude_spawn(self):
        src = "cmd = ['claude', '-p', '--model', m]\n"
        found = find_sinks(src)
        self.assertTrue(any(c == 'claude' for c, _, _ in found))

    def test_flags_print_claude_spawn(self):
        src = "cmd = ['claude', '--print', '--output-format', 'json']\n"
        found = find_sinks(src)
        self.assertTrue(any(c == 'claude' for c, _, _ in found))

    def test_ignores_claude_auth_spawn(self):
        src = "cmd = ['claude', 'auth', 'login', '--claudeai']\n"
        found = find_sinks(src)
        self.assertFalse(any(c == 'claude' for c, _, _ in found))

    def test_flags_gh_pr_merge(self):
        src = "cmd = ['gh', 'pr', 'merge', n, '--squash', '--delete-branch']\n"
        found = find_sinks(src)
        self.assertTrue(any(c == 'gh-write' for c, _, _ in found))

    def test_flags_gh_add_label(self):
        src = "cmd = ['gh', 'pr', 'edit', n, '--add-label', 'stalled']\n"
        found = find_sinks(src)
        self.assertTrue(any(c == 'gh-write' for c, _, _ in found))

    def test_flags_gh_api_field_write(self):
        src = "cmd = ['gh', 'api', path, '-f', 'state=x']\n"
        found = find_sinks(src)
        self.assertTrue(any(c == 'gh-write' for c, _, _ in found))

    def test_flags_gh_api_method_write(self):
        src = "cmd = ['gh', 'api', path, '-X', 'DELETE']\n"
        found = find_sinks(src)
        self.assertTrue(any(c == 'gh-write' for c, _, _ in found))

    def test_ignores_gh_read_only(self):
        src = "cmd = ['gh', 'pr', 'view', n, '--json', 'state']\n"
        found = find_sinks(src)
        self.assertFalse(any(c == 'gh-write' for c, _, _ in found))

    def test_ignores_gh_pr_list(self):
        src = "cmd = ['gh', 'pr', 'list', '--state', 'open']\n"
        found = find_sinks(src)
        self.assertFalse(any(c == 'gh-write' for c, _, _ in found))

    def test_flags_sendmessage_url(self):
        src = 'url = f"{API}/sendMessage"\n'
        found = find_sinks(src)
        self.assertTrue(any(c == 'telegram' for c, _, _ in found))

    def test_ignores_docstring_mentions(self):
        # A docstring that names every token must NOT be flagged (AST ignores it).
        src = (
            '"""mentions create_client and /sendMessage and gh pr merge."""\n'
            'x = 1\n'
        )
        found = find_sinks(src)
        self.assertEqual(found, [])

    def test_real_tree_passes(self):
        # The live scripts/ tree must currently be clean (every sink allowlisted).
        violations: list[str] = []
        for path in _production_modules():
            for channel, lineno, detail in find_sinks(path.read_text()):
                if path.name not in ALLOWLIST.get(channel, set()):
                    violations.append(f'{path.name}:{lineno} [{channel}] {detail}')
        self.assertEqual(violations, [], '\n'.join(violations))


if __name__ == '__main__':
    unittest.main()
