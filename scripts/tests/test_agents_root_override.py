"""Guard: every agents/ state-root construction must honor the
OURLIBERTY_AGENTS_ROOT override, so a per-tier HOME swap (the Claude CLI
auth HOME) never blinds app-state path resolution.

Background: agent_runner swaps HOME to the active tier's account home so the
Claude CLI finds that tier's OAuth. Any module that resolves agents/ state via
a bare home-relative fallback (`Path.home() / 'agents'`, `HOME / 'agents'`,
`expanduser('~/agents')`, ...) therefore points at the wrong tree under Tier 2
— both reading blind and writing state to the fallback home. The fix pins
OURLIBERTY_AGENTS_ROOT to the real account home in the child env; this test
stops the gap reopening by requiring every fallback expression to sit behind
an actual read of that override.

The check is AST-based and dataflow-based, not per-line and not name-based.
The original per-line regex had a false positive (a correctly-overridden
expression wrapped across lines lost its allow-token) and a false-CLEAN
direction (any bare fallback on a line containing the word "root" was waved
through). A first AST rewrite closed the per-line holes but still trusted
identifier NAMES ('root'/'override' as a substring blessed the expression) —
which blessed one live site bound from the WRONG env var. This version
trusts only reads: a fallback node passes when it is structurally
subordinate to a genuine read of the override —

  * the default (positional or keyword) of ``os.environ.get(ENV_VAR, ...)``
    / ``os.getenv(ENV_VAR, ...)``,
  * a later operand of an ``or`` chain whose earlier operands read the env
    var — directly, or via a name ASSIGNED from that read in the enclosing
    scope, or
  * the ``else`` arm of a conditional whose test does the same.

A bare fallback assigned directly, or guarded only by a variable that is
never bound from the env read, is an offender — whatever anything is named.
Binding-trust is strict about WHERE and HOW OFTEN the name is bound,
because the check is flow-insensitive: EVERY binding of the name in the
enclosing scope must contain the env read (one rebind from anywhere else —
or a live wrong-var binding beside a dead-branch env read — voids the
trust), and nested function definitions are opaque (a function-local
binding cannot bless a module-level use, nor a nested def its outer
function).

Bare fallbacks embedded in string literals (python -c payloads, shell
command strings) are scanned textually, since the code that runs in a
spawned child is exactly where the HOME swap bites; f-string constant
fragments are joined before matching so a guard split across fragments is
seen. WAIVER: a string passes only when it contains a READ spelling of the
override (getenv(/environ.get(/environ[ with the quoted var) — merely
NAMING the var (an export line, prose) proves nothing about the fallback
beside it. Deliberately OUT OF SCOPE for the text channel: a bare
``~/agents`` token (pervasive in docstring prose, so it cannot
discriminate payloads from documentation) and home-alias spellings (see
the ALLOWED_FILES note) — those rely on the AST channel or the allowlist.
"""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import ast
import importlib
import os
import re
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
ENV_VAR = "OURLIBERTY_AGENTS_ROOT"

# Files allowed to build a bare real-home agents path, each with a reason.
# test_isolation_guard._real_agents_roots() enumerates the REAL trees the
# test-jail must never write to; honoring the override there would point the
# jail at the sandbox and defeat it. (This replaces the old blanket
# `test_*` skip, which silently exempted production infrastructure like
# test_regression_check.py — the Mirror merge gate.)
#
# Known-INVISIBLE cousins (not allowlisted because the scanner cannot see
# them today): test_regression_check.py's REAL_AGENTS (bound via the
# REAL_HOME = Path.home() alias) and test_isolation_wall.py's ro_targets()
# (os.path.join(home, sub) from a variable). Both are INTENTIONAL real-home
# paths — the jail's RO-bind targets and the outside-jail tripwire. If a
# refactor ever surfaces them to the scanner, the fix is an ALLOWED_FILES
# entry, NEVER wrapping them in the override: an override-honoring jail
# would RO-bind/tripwire the sandbox itself and defeat the test jail.
# (Alias tracking is deliberately absent — it would flag exactly these
# correct sites. And test_regression_check.py must not be whole-file
# allowlisted either: its AGENTS_ROOT genuinely honors the override and
# must stay in scope.)
ALLOWED_FILES = {"test_isolation_guard.py"}

# Bare fallback spelled inside a string literal (a python -c payload, a
# shell command): the AST cannot parse embedded code, so match the text.
# Python spellings plus the shell ones ($HOME/agents, ${HOME}/agents). A
# bare `~/agents` token is deliberately NOT matched — it is pervasive in
# docstring prose, so it cannot discriminate a payload from documentation;
# tilde spellings are caught only where a call wrapper names them
# (expanduser('~/agents')).
TEXT_BARE = re.compile(
    r"(?:Path\.home\(\)|(?<!\w)HOME)\s*/\s*['\"]agents['\"]"
    r"|expanduser\(\s*['\"]~/agents"
    r"|\bhome\(\)\s*\.\s*joinpath\(\s*['\"]agents['\"]"
    r"|\$HOME/agents|\$\{HOME\}/agents"
)

# A string is presumed guarded only when it contains a READ spelling of the
# override. Merely naming the var (an export line, a docstring mentioning
# it) used to waive — that blessed any payload that set the var without
# reading it.
TEXT_READ = re.compile(
    r"(?:(?<!\w)getenv\s*\(|environ\.get\s*\(|environ\[)\s*['\"]"
    + re.escape(ENV_VAR)
    + r"['\"]"
)


def _is_home_base(node):
    """An expression resolving to the process home: `Path.home()`,
    `pathlib.Path.home()`, `HOME`, `cfg.HOME`."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        f = node.func
        return f.attr == "home" and (
            (isinstance(f.value, ast.Name) and f.value.id == "Path")
            or (isinstance(f.value, ast.Attribute) and f.value.attr == "Path")
        )
    if isinstance(node, ast.Name):
        return node.id == "HOME"
    return isinstance(node, ast.Attribute) and node.attr == "HOME"


def _is_tilde_agents(node):
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (node.value == "~/agents" or node.value.startswith("~/agents/"))
    )


def _is_agents_fallback(node):
    """True for any spelling of <home>/agents: `<home> / 'agents'`,
    `<home>.joinpath('agents', ...)`, `expanduser('~/agents')`,
    `Path('~/agents').expanduser()`."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return (
            isinstance(node.right, ast.Constant)
            and node.right.value == "agents"
            and _is_home_base(node.left)
        )
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    fname = (
        func.attr if isinstance(func, ast.Attribute)
        else func.id if isinstance(func, ast.Name)
        else None
    )
    if fname == "joinpath" and isinstance(func, ast.Attribute) and node.args:
        first = node.args[0]
        return (
            isinstance(first, ast.Constant)
            and first.value == "agents"
            and _is_home_base(func.value)
        )
    if fname == "expanduser":
        if node.args and _is_tilde_agents(node.args[0]):
            return True
        if isinstance(func, ast.Attribute):
            v = func.value  # Path('~/agents').expanduser()
            return isinstance(v, ast.Call) and v.args and _is_tilde_agents(v.args[0])
    return False


def _is_env_read(node):
    """A genuine read of the override: os.environ.get(ENV_VAR, ...),
    environ.get(...), os.getenv(ENV_VAR, ...), getenv(...). The receiver is
    checked — `somedict.get('OURLIBERTY_AGENTS_ROOT')` reads a snapshot, not
    the env, and does not count."""
    if not (isinstance(node, ast.Call) and node.args):
        return False
    first = node.args[0]
    if not (isinstance(first, ast.Constant) and first.value == ENV_VAR):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "getenv"
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "getenv":
        return isinstance(func.value, ast.Name) and func.value.id == "os"
    if func.attr == "get":
        v = func.value
        if isinstance(v, ast.Name):
            return v.id == "environ"
        return (
            isinstance(v, ast.Attribute)
            and v.attr == "environ"
            and isinstance(v.value, ast.Name)
            and v.value.id == "os"
        )
    return False


def _enclosing_scope(node, parents):
    n = parents.get(node)
    while n is not None and not isinstance(
        n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)
    ):
        n = parents.get(n)
    return n


def _scope_walk(scope):
    """Walk `scope` WITHOUT descending into nested function definitions:
    their local bindings are invisible to the enclosing scope, and trusting
    them blessed cross-scope (a function-local `root = <env read>` cleared
    an unrelated module-level `root`, and a nested def cleared its outer
    function)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(ast.iter_child_nodes(node))


def _binds_env_read(name, scope):
    """True if `name` is bound from an expression containing a genuine env
    read of the override in `scope` (the standard two-line idiom:
    `root = os.environ.get(ENV_VAR)` then `... if root else ...`).

    The check is flow-insensitive — it cannot see WHICH binding reaches the
    fallback — so it compensates by being strict about the binding set:
    EVERY binding of the name in the scope must contain the read. One
    rebind from anywhere else (`root = args.root` after the read), or a
    live wrong-var binding beside a dead `if False:` env read, voids the
    trust. Nested defs are excluded from the walk (see _scope_walk)."""
    if scope is None:
        return False
    bound_values = []
    for sub in _scope_walk(scope):
        value = None
        if isinstance(sub, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in sub.targets):
                value = sub.value
        elif isinstance(sub, (ast.AnnAssign, ast.NamedExpr)):
            if isinstance(sub.target, ast.Name) and sub.target.id == name:
                value = sub.value
        if value is not None:
            bound_values.append(value)
    return bool(bound_values) and all(
        any(_is_env_read(n) for n in ast.walk(v)) for v in bound_values
    )


def _reads_override(expr, scope):
    """True if the expression reads the override — directly, or through a
    name assigned from that read in the enclosing scope. Names are trusted
    only by their BINDING, never by what they are called."""
    for sub in ast.walk(expr):
        if _is_env_read(sub):
            return True
        if isinstance(sub, ast.Name) and _binds_env_read(sub.id, scope):
            return True
    return False


def _is_guarded(node, parents):
    """Walk ancestors: is this fallback structurally subordinate to a
    genuine override read? (env-get default / later `or` operand /
    `else` arm)."""
    child = node
    parent = parents.get(child)
    while parent is not None:
        # Inside an env read the fallback can only be the default —
        # positional (args[1:]) or keyword — since args[0] is the var name.
        if _is_env_read(parent):
            return True
        if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.Or):
            idx = parent.values.index(child)
            scope = _enclosing_scope(parent, parents)
            if any(_reads_override(v, scope) for v in parent.values[:idx]):
                return True
        if isinstance(parent, ast.IfExp) and child is parent.orelse:
            if _reads_override(parent.test, _enclosing_scope(parent, parents)):
                return True
        # Plain nesting (Call args like Path(...)/str(...), BinOps that
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

    def _flag_text(node, text):
        if TEXT_BARE.search(text) and not TEXT_READ.search(text):
            offenders.append(
                f"{filename}:{node.lineno}: [in string] "
                + " ".join(text.split())[:80]
            )

    for node in ast.walk(tree):
        if _is_agents_fallback(node) and not _is_guarded(node, parents):
            segment = ast.get_source_segment(source, node) or ""
            offenders.append(
                f"{filename}:{node.lineno}: {' '.join(segment.split())}"
            )
        elif isinstance(node, ast.JoinedStr):
            # An f-string is ONE string at runtime but many Constant
            # fragments in the AST: the env read can sit in a different
            # fragment from the fallback. Join the constant fragments
            # before matching — scanned per-fragment, the waiver could
            # never see the read, leaving a guarded payload no exit but
            # ALLOWED_FILES.
            _flag_text(node, "".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and not isinstance(parents.get(node), ast.JoinedStr)
        ):
            _flag_text(node, node.value)
    return offenders


class TestAgentsRootOverride(unittest.TestCase):
    def test_no_bare_agents_root_without_override(self):
        offenders = []
        for py in sorted(SCRIPTS.glob("*.py")):
            if py.name in ALLOWED_FILES:
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
        override = "/tmp/ol-test-agents-root"
        mods = [
            ("larry_alerts", "AGENTS_ROOT"),
            ("concurrency_guard", "AGENTS_ROOT"),
            ("dispatch_lease", "AGENTS_ROOT"),
            ("kill_switch", "AGENTS_ROOT"),
            ("active_tier", "AGENTS_ROOT"),
        ]
        try:
            with mock.patch.dict(os.environ, {
                "OURLIBERTY_AGENTS_ROOT": override,
                "HOME": "/tmp/ol-bogus-tier2-home",
            }):
                for modname, attr in mods:
                    mod = importlib.import_module(modname)
                    importlib.reload(mod)
                    val = str(getattr(mod, attr))
                    self.assertTrue(
                        val.startswith(override),
                        f"{modname}.{attr} = {val!r} did not honor override {override!r}",
                    )
        finally:
            # Re-freeze module state against the RESTORED env — this must run
            # AFTER the with-block has unwound the patch: without it, the
            # five modules' AGENTS_ROOT (and every derived constant) stay
            # pinned to the bogus override for the rest of the process — the
            # same leak class ApprovalRootEnvTest's tearDown re-reload exists
            # to prevent (test_beacon_approval_root_env.py).
            for modname, _ in mods:
                importlib.reload(importlib.import_module(modname))


class TestGuardScanner(unittest.TestCase):
    """Mutation-prove the scanner itself: it must flag every genuinely bare
    fallback (including ones an override-ish NAME would have talked past)
    and stay quiet on every guarded idiom the tree actually uses. A guard
    that cannot fail is not evidence."""

    def assert_flags(self, source, n=1):
        offenders = find_bare_agents_roots(source)
        self.assertEqual(len(offenders), n, offenders)

    def assert_quiet(self, source):
        self.assertEqual(find_bare_agents_roots(source), [])

    # -- must FLAG (a miss here is the false-CLEAN direction) ----------------

    def test_flags_plain_bare_fallback(self):
        self.assert_flags("p = Path.home() / 'agents'\n")

    def test_flags_bare_on_line_containing_root(self):
        # The per-line guard's worst hole: 'root' anywhere on the line.
        self.assert_flags("agents_root = Path.home() / 'agents'\n")

    def test_flags_bare_home_variant(self):
        self.assert_flags("AGENTS = HOME / 'agents'\n")

    def test_flags_attribute_home(self):
        self.assert_flags("p = cfg.HOME / 'agents'\n")

    def test_flags_pathlib_qualified_home(self):
        self.assert_flags("p = pathlib.Path.home() / 'agents'\n")

    def test_flags_joinpath_spelling(self):
        self.assert_flags("p = Path.home().joinpath('agents')\n")

    def test_flags_expanduser_spelling(self):
        self.assert_flags("p = os.path.expanduser('~/agents')\n")

    def test_flags_path_expanduser_spelling(self):
        self.assert_flags("p = Path('~/agents/state').expanduser()\n")

    def test_flags_fallback_inside_string_payload(self):
        # Embedded child code is where the HOME swap bites hardest.
        self.assert_flags(
            'cmd = "python3 -c \\"print(Path.home() / \'agents\')\\""\n')

    def test_flags_fallback_as_first_or_operand(self):
        # Wrong precedence: the fallback wins, the override never reads.
        self.assert_flags(
            "r = Path.home() / 'agents' or os.environ.get("
            "'OURLIBERTY_AGENTS_ROOT')\n")

    def test_flags_or_guard_by_unrelated_name(self):
        self.assert_flags("r = Path(cfg_dir or Path.home() / 'agents')\n")

    def test_flags_or_guard_by_rootish_but_unbound_name(self):
        # The name-token hole: 'repo_root' SOUNDS like an override but is
        # never bound from the env read — trusting it is trusting a name.
        self.assert_flags(
            "repo_root = Path(__file__).parent\n"
            "r = repo_root or Path.home() / 'agents'\n")

    def test_flags_ifexp_bound_from_wrong_env_var(self):
        # Right-shaped name, wrong binding (the beacon log-dir shape).
        self.assert_flags(
            "override = os.environ.get('OURLIBERTY_LOG_DIR')\n"
            "d = Path(override) if override else Path.home() / 'agents'\n")

    def test_flags_ifexp_with_unrelated_test(self):
        self.assert_flags("r = Path(x) if x else Path.home() / 'agents'\n")

    def test_flags_non_environ_receiver(self):
        # A dict snapshot is not the env: it can predate the pin.
        self.assert_flags(
            "r = stale_env.get('OURLIBERTY_AGENTS_ROOT',"
            " str(Path.home() / 'agents'))\n")

    def test_flags_rebind_after_env_read(self):
        # Flow-insensitivity compensated by the EVERY-binding rule: one
        # binding reads the env, but a rebind from anywhere else means the
        # name reaching the fallback may not be the override at all.
        self.assert_flags(
            "def f(args):\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root = args.root\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_dead_branch_env_read_with_wrong_var_live_binding(self):
        # A dead `if False:` env read must not bless the live binding from
        # the WRONG var sitting next to it.
        self.assert_flags(
            "def f():\n"
            "    if False:\n"
            "        root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root = os.environ.get('SOME_OTHER_VAR')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_module_use_with_only_function_local_binding(self):
        # Cross-scope blessing: a function-LOCAL `root = <env read>` is
        # invisible at module level — the module-level `root` guarding the
        # fallback is a different (unbound) name entirely.
        self.assert_flags(
            "def f():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return root\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_nested_def_binding_does_not_bless_outer(self):
        # Same rule one level down: a nested def's local binding cannot
        # clear its outer function's fallback.
        self.assert_flags(
            "def outer():\n"
            "    def inner():\n"
            "        root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "        return root\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_string_mentioning_var_without_reading_it(self):
        # The round-1 waiver blessed any string that NAMED the var. Setting
        # it is not reading it — the payload's fallback is still bare.
        self.assert_flags(
            "cmd = \"export OURLIBERTY_AGENTS_ROOT=/x; "
            "python3 -c 'print(Path.home() / \\\"agents\\\")'\"\n")

    def test_flags_bare_fallback_in_fstring_payload(self):
        # Joined-fragment scanning must still FLAG an f-string whose joined
        # text has the fallback and no read.
        self.assert_flags(
            "cmd = f\"HOME={home} python3 -c "
            "'print(Path.home() / \\\"agents\\\")'\"\n")

    def test_flags_shell_home_spelling_in_string(self):
        self.assert_flags('cmd = "ls $HOME/agents/state"\n')

    def test_flags_shell_braced_home_spelling_in_string(self):
        self.assert_flags('cmd = "cat ${HOME}/agents/rotation.disabled"\n')

    def test_flags_expanduser_spelling_in_string(self):
        self.assert_flags(
            "cmd = \"python3 -c 'import os; "
            "print(os.path.expanduser(\\\"~/agents\\\"))'\"\n")

    def test_flags_joinpath_spelling_in_string(self):
        self.assert_flags(
            "cmd = \"python3 -c 'from pathlib import Path; "
            "print(Path.home().joinpath(\\\"agents\\\"))'\"\n")

    def test_flags_docstring_spelling_the_fallback(self):
        # Pin the string-channel contract for prose: a docstring is a
        # string constant like any other. Round 1 waived prose, round 2
        # silently reversed that; this is the first test of either. A doc
        # that verbatim spells the bare fallback FLAGS — fix the doc, or
        # (for a true payload) guard it with a read spelling.
        self.assert_flags(
            '"""Reads Path.home() / \'agents\' at startup."""\n')

    def test_reports_file_and_line(self):
        offenders = find_bare_agents_roots(
            "x = 1\np = Path.home() / 'agents'\n", filename="victim.py")
        self.assertEqual(len(offenders), 1)
        self.assertTrue(offenders[0].startswith("victim.py:2:"), offenders)

    # -- must stay QUIET (each is a live idiom in scripts/) ------------------

    def test_allows_or_chain_env_read(self):
        self.assert_quiet(
            "AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT')"
            " or Path.home() / 'agents')\n")

    def test_allows_env_get_default(self):
        self.assert_quiet(
            "root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT',"
            " str(Path.home() / 'agents')))\n")

    def test_allows_env_get_keyword_default(self):
        # getenv/environ.get both accept default= in py3; the guard must not
        # punish the keyword spelling of its own first blessed idiom.
        self.assert_quiet(
            "p = Path(os.getenv('OURLIBERTY_AGENTS_ROOT',"
            " default=str(Path.home() / 'agents')))\n")

    def test_allows_env_get_default_expanduser(self):
        # audit_due_nudge / distill_detector's live spelling.
        self.assert_quiet(
            "AGENTS_ROOT = pathlib.Path(os.environ.get("
            "'OURLIBERTY_AGENTS_ROOT', os.path.expanduser('~/agents')))\n")

    def test_allows_wrapped_multiline_or_chain(self):
        # agent_runner.py's identity-pin root — the per-line guard's false
        # positive.
        self.assert_quiet(
            "root = Path(agents_root or os.environ.get("
            "'OURLIBERTY_AGENTS_ROOT')\n"
            "            or Path.home() / 'agents')\n")

    def test_allows_ifexp_bound_from_env_read(self):
        # The standard two-line idiom (heal_*, pulse_check_*): trusted by
        # BINDING, not by the name.
        self.assert_quiet(
            "def f():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_allows_two_step_override_with_trailing_segment(self):
        # beacon_telegram_bot's log-dir form after its fix: a foreign
        # override may take precedence, but the FALLBACK derives from a
        # genuine agents-root read.
        self.assert_quiet(
            "def g():\n"
            "    override = os.environ.get('OURLIBERTY_LOG_DIR')\n"
            "    if override:\n"
            "        return Path(override)\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return (Path(root) if root else Path.home() / 'agents')"
            " / 'logs'\n")

    def test_allows_home_variant_guarded(self):
        self.assert_quiet(
            "AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT')"
            " or HOME / 'agents')\n")

    def test_allows_payload_string_naming_the_env_var(self):
        self.assert_quiet(
            "cmd = \"python3 -c 'import os; print(os.environ.get("
            "\\\"OURLIBERTY_AGENTS_ROOT\\\") or Path.home() / \\'agents\\')'\"\n")

    def test_ignores_comments_and_plain_prose(self):
        self.assert_quiet(
            "# was hardcoded to HOME/'agents' before the audit\n"
            "x = 1\n")

    def test_allows_fstring_payload_guarded_across_fragments(self):
        # The read sits in one constant fragment, the fallback in another
        # (an interpolation between them splits the AST constants). Scanned
        # per-fragment this genuinely-guarded payload had no exit but
        # ALLOWED_FILES; joined, the waiver sees the read.
        self.assert_quiet(
            "cmd = f\"python3 -c 'print(os.environ.get("
            "\\\"OURLIBERTY_AGENTS_ROOT\\\") or {opt} "
            "or Path.home() / \\\"agents\\\")'\"\n")

    def test_allows_aliased_home_as_out_of_scope(self):
        # test_regression_check.py's REAL_AGENTS shape: Path.home() bound
        # to a name first. The scanner deliberately does NOT track aliases
        # — the live sites spelled this way are INTENTIONAL real-home paths
        # (the jail's RO-bind targets and tripwire), where the correct
        # response to being surfaced is an ALLOWED_FILES entry, never the
        # override (see the ALLOWED_FILES note).
        self.assert_quiet(
            "REAL_HOME = Path.home()\n"
            "REAL_AGENTS = REAL_HOME / 'agents'\n")

    def test_allows_tilde_agents_in_docstring_prose(self):
        # Bare ~/agents tokens are OUT OF SCOPE for the text channel: they
        # are pervasive in docstring prose across scripts/ and cannot
        # discriminate a payload from documentation.
        self.assert_quiet(
            '"""State lives in ~/agents/state/ (see ~/agents/logs/)."""\n'
            "x = 1\n")


if __name__ == "__main__":
    unittest.main()
