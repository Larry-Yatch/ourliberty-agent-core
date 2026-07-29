"""Guard: every agents/ state-root construction must honor the
OURLIBERTY_AGENTS_ROOT override, so a per-tier HOME swap (the Claude CLI
auth HOME) never blinds app-state path resolution.

Background: agent_runner swaps HOME to the active tier's account home so the
Claude CLI finds that tier's OAuth. Any module that resolves agents/ state via
a bare `Path.home() / 'agents'` (or `HOME / 'agents'`) therefore points at the
wrong tree under Tier 2 — both reading blind and writing state to the fallback
home. The fix pins OURLIBERTY_AGENTS_ROOT to the real account home in the child
env; this test stops the gap reopening by requiring every fallback expression
to sit behind the override.

The check is AST-based, not per-line. The earlier per-line regex had a false
positive (a correctly-overridden expression wrapped across lines lost its
allow-token — agent_runner's identity-pin root) and, worse, a false-CLEAN
direction: any genuinely bare fallback written on a line that merely contained
the word "root" was waved through. Parsing the expression closes both. A
fallback node counts as guarded only when it is structurally subordinate to
the override:

  * the default of ``os.environ.get('OURLIBERTY_AGENTS_ROOT', ...)`` /
    ``os.getenv(...)``,
  * a later operand of an ``or`` chain whose earlier operands read the env
    var or name an override/root variable, or
  * the ``else`` arm of a conditional whose test does the same.

A bare fallback assigned directly — whatever the target is called — is an
offender.
"""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import ast
import os
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
ENV_VAR = "OURLIBERTY_AGENTS_ROOT"
NAME_TOKENS = ("root", "override")


def _is_agents_fallback(node):
    """True for `Path.home() / 'agents'` or `HOME / 'agents'`."""
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
        return False
    if not (isinstance(node.right, ast.Constant) and node.right.value == "agents"):
        return False
    left = node.left
    if isinstance(left, ast.Name) and left.id == "HOME":
        return True
    return (
        isinstance(left, ast.Call)
        and isinstance(left.func, ast.Attribute)
        and left.func.attr == "home"
        and isinstance(left.func.value, ast.Name)
        and left.func.value.id == "Path"
    )


def _is_env_read(node):
    """True for os.environ.get(ENV_VAR, ...) / os.getenv(ENV_VAR, ...)."""
    if not (isinstance(node, ast.Call) and node.args):
        return False
    first = node.args[0]
    if not (isinstance(first, ast.Constant) and first.value == ENV_VAR):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in ("get", "getenv")
    return isinstance(func, ast.Name) and func.id == "getenv"


def _mentions_override(expr):
    """True if the expression reads the env var or names an override/root
    variable — i.e. it is plausibly the preferred value the fallback yields to."""
    for sub in ast.walk(expr):
        if _is_env_read(sub):
            return True
        ident = None
        if isinstance(sub, ast.Name):
            ident = sub.id
        elif isinstance(sub, ast.Attribute):
            ident = sub.attr
        if ident and any(t in ident.lower() for t in NAME_TOKENS):
            return True
    return False


def _is_guarded(node, parents):
    """Walk ancestors: is this fallback structurally subordinate to the
    override? (env-get default / later `or` operand / `else` arm)."""
    child = node
    parent = parents.get(child)
    while parent is not None:
        if _is_env_read(parent) and child in parent.args[1:]:
            return True
        if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.Or):
            idx = parent.values.index(child) if child in parent.values else 0
            if idx > 0 and any(_mentions_override(v) for v in parent.values[:idx]):
                return True
        if isinstance(parent, ast.IfExp) and child is parent.orelse:
            if _mentions_override(parent.test):
                return True
        # keywords/starred/etc. are not expression contexts we bless through,
        # but plain nesting (Call args like Path(...)/str(...), BinOps that
        # append segments, parens) keeps climbing.
        child, parent = parent, parents.get(parent)
    return False


def find_bare_agents_roots(source, filename="<source>"):
    """Return ['file:line: segment', ...] for every unguarded fallback."""
    tree = ast.parse(source, filename=filename)
    parents = {}
    for node in ast.walk(tree):
        for kid in ast.iter_child_nodes(node):
            parents[kid] = node
    offenders = []
    for node in ast.walk(tree):
        if _is_agents_fallback(node) and not _is_guarded(node, parents):
            segment = ast.get_source_segment(source, node) or ""
            offenders.append(
                f"{filename}:{node.lineno}: {' '.join(segment.split())}"
            )
    return offenders


class TestAgentsRootOverride(unittest.TestCase):
    def test_no_bare_agents_root_without_override(self):
        offenders = []
        for py in sorted(SCRIPTS.glob("*.py")):
            if py.name.startswith("test_"):
                continue
            offenders.extend(
                find_bare_agents_roots(
                    py.read_text(encoding="utf-8"), filename=py.name
                )
            )
        self.assertFalse(
            offenders,
            "agents/ state-root must honor OURLIBERTY_AGENTS_ROOT "
            "(a per-tier HOME swap would otherwise blind these). Offenders:\n  - "
            + "\n  - ".join(offenders),
        )

    def test_modules_resolve_under_override(self):
        """Behavioral: with OURLIBERTY_AGENTS_ROOT set and HOME pointed at a
        bogus tier-2 home, lightweight modules resolve their root under the
        override, not under HOME."""
        import importlib
        override = "/tmp/ol-test-agents-root"
        old = {k: os.environ.get(k) for k in ("OURLIBERTY_AGENTS_ROOT", "HOME")}
        os.environ["OURLIBERTY_AGENTS_ROOT"] = override
        os.environ["HOME"] = "/tmp/ol-bogus-tier2-home"
        try:
            for modname, attr in [
                ("larry_alerts", "AGENTS_ROOT"),
                ("concurrency_guard", "AGENTS_ROOT"),
                ("dispatch_lease", "AGENTS_ROOT"),
                ("kill_switch", "AGENTS_ROOT"),
                ("active_tier", "AGENTS_ROOT"),
            ]:
                mod = importlib.import_module(modname)
                importlib.reload(mod)
                val = str(getattr(mod, attr))
                self.assertTrue(
                    val.startswith(override),
                    f"{modname}.{attr} = {val!r} did not honor override {override!r}",
                )
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TestGuardScanner(unittest.TestCase):
    """Mutation-prove the scanner itself: it must flag every genuinely bare
    fallback (including ones the old per-line allow-tokens waved through) and
    stay quiet on every guarded idiom the tree actually uses. A guard that
    cannot fail is not evidence."""

    # -- must FLAG (a miss here is the false-CLEAN direction) ----------------

    def test_flags_plain_bare_fallback(self):
        self.assertEqual(len(find_bare_agents_roots(
            "p = Path.home() / 'agents'\n")), 1)

    def test_flags_bare_on_line_containing_root(self):
        # The old guard's worst hole: 'root' anywhere on the line passed it.
        offenders = find_bare_agents_roots(
            "agents_root = Path.home() / 'agents'\n")
        self.assertEqual(len(offenders), 1, offenders)

    def test_flags_bare_home_variant(self):
        self.assertEqual(len(find_bare_agents_roots(
            "AGENTS = HOME / 'agents'\n")), 1)

    def test_flags_fallback_as_first_or_operand(self):
        # Wrong precedence: the fallback wins, the override never reads.
        offenders = find_bare_agents_roots(
            "r = Path.home() / 'agents' or os.environ.get("
            "'OURLIBERTY_AGENTS_ROOT')\n")
        self.assertEqual(len(offenders), 1, offenders)

    def test_flags_or_guard_by_unrelated_name(self):
        offenders = find_bare_agents_roots(
            "r = Path(cfg_dir or Path.home() / 'agents')\n")
        self.assertEqual(len(offenders), 1, offenders)

    def test_flags_ifexp_with_unrelated_test(self):
        offenders = find_bare_agents_roots(
            "r = Path(x) if x else Path.home() / 'agents'\n")
        self.assertEqual(len(offenders), 1, offenders)

    def test_reports_file_and_line(self):
        offenders = find_bare_agents_roots(
            "x = 1\np = Path.home() / 'agents'\n", filename="victim.py")
        self.assertEqual(len(offenders), 1)
        self.assertTrue(offenders[0].startswith("victim.py:2:"), offenders)

    # -- must stay QUIET (each is a live idiom in scripts/) ------------------

    def test_allows_or_chain_env_read(self):
        self.assertEqual(find_bare_agents_roots(
            "AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT')"
            " or Path.home() / 'agents')\n"), [])

    def test_allows_env_get_default(self):
        self.assertEqual(find_bare_agents_roots(
            "root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT',"
            " str(Path.home() / 'agents')))\n"), [])

    def test_allows_wrapped_multiline_or_chain(self):
        # agent_runner.py's identity-pin root — the old guard's false positive.
        self.assertEqual(find_bare_agents_roots(
            "root = Path(agents_root or os.environ.get("
            "'OURLIBERTY_AGENTS_ROOT')\n"
            "            or Path.home() / 'agents')\n"), [])

    def test_allows_ifexp_override(self):
        self.assertEqual(find_bare_agents_roots(
            "return_ = Path(override) if override else"
            " Path.home() / 'agents'\n"), [])

    def test_allows_ifexp_with_trailing_segment(self):
        # beacon_telegram_bot's log-dir form: fallback nested one BinOp deeper.
        self.assertEqual(find_bare_agents_roots(
            "d = Path(override) if override else"
            " Path.home() / 'agents' / 'logs'\n"), [])

    def test_allows_home_variant_guarded(self):
        self.assertEqual(find_bare_agents_roots(
            "AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT')"
            " or HOME / 'agents')\n"), [])

    def test_ignores_comments_and_docstrings(self):
        self.assertEqual(find_bare_agents_roots(
            "# was hardcoded to HOME/'agents' before the audit\n"
            '"""mentions Path.home() / \'agents\' in prose"""\n'), [])


if __name__ == "__main__":
    unittest.main()
