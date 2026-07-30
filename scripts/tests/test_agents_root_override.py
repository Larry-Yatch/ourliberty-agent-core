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

THE RULE, in full — it is meant to fit in your head:

  A fallback flags unless it is the ``else``/later-operand/default of an
  expression whose guard IS a read of the override, or IS a bare trusted
  name. A NAME is trusted only if EVERY binding of it in the whole file is
  syntactically that same read (``Path()``/``str()`` wrappers allowed).

That is the entire trust model. No scope chain, no dataflow, no "derives
from", no parameter special-cases.

WHY SO BLUNT. Three review rounds of a cleverer rule — scope-aware name
resolution, derivation analysis, parameter softening, payload parsing —
produced 5, then 10, then 14 real defects, most of them created by the
previous round's own fix. The tell was directional: every fix made the
BLESSING logic smarter, and a bigger trusted surface is a bigger attack
surface. A blessing rule you cannot audit in one sitting is one you cannot
trust, however many tests pin it — tests only pin the bypasses someone
already thought of. So the rule is now deliberately blunt, and it
OVER-FLAGS on purpose: a loud false alarm costs a developer one message
(every offender says why it fired, and ``_REMEDIATION`` says how to clear
it), while a silent false clean writes tier-2 state into the wrong home.
When this fires on correct code, fix it with remediation 1 or 4 — do NOT
widen the trust rule.

WHAT THIS CANNOT SEE, deliberately. Because there is no scope model, a
binding anywhere in the file can bless a use anywhere else. Every such
case is code that raises NameError before it can resolve a path (a
function-local binding "blessing" a module-level use, a class attribute
"blessing" a module global), so it cannot ship a wrong path — it cannot
run at all. Buying that detection back means reintroducing the scope model
that produced three rounds of bypasses. Refused on purpose; pinned by the
``test_scope_free_limit_*`` tests so the trade stays visible.

The string channel (payloads that run in a spawned child, where the HOME
swap bites hardest) does NOT parse code — parsing mis-channelled shell
commands that happened to be valid Python. It matches per line, in str and
bytes constants, joining f-string fragments with an opaque placeholder.
Its ONE waiver is the shell default-expansion ``${OURLIBERTY_AGENTS_ROOT:-
$HOME/agents}``, which binds read and fallback into a single unambiguous
construct; a read merely elsewhere on the line waives nothing (a line can
read the var for one purpose and use a bare path for another, and
``OURLIBERTY_AGENTS_ROOT=$HOME/agents ... $OURLIBERTY_AGENTS_ROOT`` even
stomps the override to the bare path and reads it back). Out of scope: a
bare ``~/agents`` token, which is pervasive in docstring prose and cannot
discriminate a payload from documentation.
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
# refactor ever surfaces them to the scanner, the fix is an ALLOWED_SITES
# entry below (per-site, so the rest of the file stays scanned — a
# whole-file ALLOWED_FILES entry would take test_regression_check.py's
# genuinely override-honoring AGENTS_ROOT out of scope), and NEVER
# wrapping them in the override: an override-honoring jail would
# RO-bind/tripwire the sandbox itself and defeat the test jail. (Alias
# tracking is deliberately absent — it would flag exactly these correct
# sites.)
ALLOWED_FILES = {"test_isolation_guard.py"}

# Per-SITE waivers: (filename, whitespace-normalized source segment) →
# reason. Finer-grained than ALLOWED_FILES: the named expression is
# exempt, everything else in the file stays in scope. Entries must name
# intentional real-home sites only.
ALLOWED_SITES = {
    # (filename, exact whitespace-normalized expression) -> why it is
    # deliberately a REAL-home path. Empty today: the two known
    # intentional sites (test_regression_check.py's REAL_AGENTS,
    # test_isolation_wall.py's ro_targets) reach the real home through a
    # variable, which this scanner does not follow, so neither is visible
    # to it. If a refactor ever surfaces one, add it HERE with its reason
    # rather than to ALLOWED_FILES — a whole-file entry would also take
    # test_regression_check.py's genuinely override-honoring AGENTS_ROOT
    # out of scope. (No entry is pre-registered: the exact expression text
    # a refactor would produce cannot be guessed, and a key that never
    # matches is worse than no key.)
}

# Bare fallback spelled inside a string literal (a python -c payload, a
# shell command): the AST cannot parse embedded code, so match the text.
# Python spellings plus the shell ones ($HOME/agents, ${HOME}/agents). A
# bare `~/agents` token is deliberately NOT matched — it is pervasive in
# docstring prose, so it cannot discriminate a payload from documentation;
# tilde spellings are caught only where a call wrapper names them
# (expanduser('~/agents')). Every alternative is TERMINATED at a path-
# segment boundary so sibling directories ($HOME/agents-archive,
# expanduser('~/agents-old')) — different trees entirely — do not match.
TEXT_BARE = re.compile(
    r"(?:Path\.home\(\)|(?<!\w)HOME)\s*/\s*['\"]agents['\"]"
    r"|expanduser\(\s*['\"]~/agents(?=['\"/])"
    # Path('~/agents').expanduser() — the wrapper the AST channel flags.
    r"|['\"]~/agents(?=['\"/])[^)]*\)\s*\.\s*expanduser"
    r"|\.\s*joinpath\(\s*['\"]agents['\"]"
    # optional closing quote: shell-correct quoting ("$HOME"/agents,
    # "${HOME}"/agents) is the same path.
    r"|\$HOME['\"]?/agents(?![\w.-])|\$\{HOME\}['\"]?/agents(?![\w.-])"
)

# The ONLY in-string guard the text channel recognizes: the shell
# default-expansion, which binds the read and the fallback into one
# unambiguous construct — `"${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}"`.
# A read merely somewhere else on the line does NOT waive: a line can
# read the var for one purpose and use a bare path for another, and
# `OURLIBERTY_AGENTS_ROOT=$HOME/agents ... $OURLIBERTY_AGENTS_ROOT` even
# stomps the override to the bare path and reads it back.
SHELL_DEFAULT = re.compile(r"\$\{" + re.escape(ENV_VAR) + r":[-=]")


def _inside_shell_default(line, pos):
    """Is the match at `pos` inside a `${OURLIBERTY_AGENTS_ROOT:-...}`
    expansion? Scans for the nearest opener before `pos` and checks its
    closing brace has not already passed."""
    for m in SHELL_DEFAULT.finditer(line):
        if m.start() > pos:
            break
        close = line.find("}", m.end())
        if close == -1 or close > pos:
            return True
    return False


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


def _is_environ(node):
    """The live env mapping: `environ` or `os.environ`. A snapshot dict is
    NOT the env — it can predate the pin — so the receiver is checked."""
    if isinstance(node, ast.Name):
        return node.id == "environ"
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_env_read(node):
    """A genuine read of the override: os.environ.get(ENV_VAR, ...),
    environ.get(...), os.getenv(ENV_VAR, ...), getenv(...), and the
    subscript forms os.environ[ENV_VAR] / environ[ENV_VAR]."""
    if isinstance(node, ast.Subscript):
        key = node.slice
        return (
            _is_environ(node.value)
            and isinstance(key, ast.Constant)
            and key.value == ENV_VAR
        )
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
        return _is_environ(func.value)
    return False


# ---------------------------------------------------------------------------
# THE TRUST RULE — deliberately dumb, and kept small enough to audit in one
# sitting. Three review rounds of a cleverer rule (scope chains, dataflow
# derivation, parameter special-cases) each closed one bypass and opened
# others: a blessing rule you cannot hold in your head is one you cannot
# trust, however many tests pin it. So:
#
#   A NAME blesses a fallback only if EVERY binding of that name anywhere
#   in the file is, syntactically, a read of the override (optionally
#   wrapped in Path()/str()/os.fspath()). One binding from anything else
#   — another env var, a parameter, a loop target, a rebind, a nested def
#   of the same name — and the name is not trusted, full stop.
#
#   A GUARD EXPRESSION blesses only if it IS a read of the override or IS
#   a bare trusted name (after unwrapping those same trivial calls). Not
#   "contains one somewhere": `cfg.get(root)` is not a guard.
#
# The rule over-flags on purpose. Every offender message says why it fired
# and how to clear it (see _REMEDIATION), because a loud false positive is
# cheap and a silent false clean is what actually bites.
# ---------------------------------------------------------------------------

# Calls that pass their argument through unchanged for our purposes, so
# `root = Path(os.environ.get(ENV_VAR))` still reads as the env read.
_TRIVIAL_WRAPPERS = {"Path", "PurePath", "PosixPath", "str", "fspath"}


def _unwrap(node):
    """Strip trivial single-argument wrapper calls."""
    while (
        isinstance(node, ast.Call)
        and len(node.args) == 1
        and not node.keywords
    ):
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else None
        )
        if name not in _TRIVIAL_WRAPPERS:
            break
        node = node.args[0]
    return node


def _binds_from_env_read(value):
    """Is this binding's value, syntactically, the override read?"""
    return value is not None and _is_env_read(_unwrap(value))


def _target_names(target):
    """Names bound by an assignment-style target, including tuple/list
    unpacking and starred targets."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _target_names(elt)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)


def _collect_bindings(tree, parents):
    """name -> list of bound values, ONE entry per binding occurrence
    anywhere in the file, in any scope. `None` means the value cannot be
    inspected (a parameter, loop target, import, `del`, `+=`, a nested def
    of that name, a `global` declaration, ...) — always disqualifying.

    Scope-free by design: no LEGB chain, no nested-scope bookkeeping, no
    parameter special-cases. Reusing a name for a second purpose anywhere
    in the file costs that name its trust, and the offender message says
    so. Missing a binding FORM here can only make a name look LESS bound,
    so the generic Store/Del sweep is the backstop for forms not named
    below."""
    bindings = {}

    def record(name, value):
        bindings.setdefault(name, []).append(value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            # Every name written or deleted by ANY construct: assignments,
            # loop targets, comprehension targets, walrus, del, `with as`,
            # type aliases. Only a plain assignment exposes a value.
            parent = parents.get(node)
            if isinstance(parent, ast.Assign) and node in parent.targets:
                record(node.id, parent.value)
            elif (
                isinstance(parent, (ast.AnnAssign, ast.NamedExpr))
                and node is parent.target
            ):
                record(node.id, parent.value)
            else:
                record(node.id, None)
        elif isinstance(node, ast.arg):
            record(node.arg, None)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            record(node.name, None)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            record(node.name, None)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                record(alias.asname or alias.name.split(".")[0], None)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                record(name, None)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            record(node.name, None)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            record(node.rest, None)
    return bindings


def _trusted_names(tree, parents):
    """Names every one of whose bindings is, syntactically, the override
    read. One binding from anything else disqualifies the name."""
    return {
        name
        for name, values in _collect_bindings(tree, parents).items()
        if values and all(_binds_from_env_read(v) for v in values)
    }


def _is_guard(expr, trusted):
    """Does this expression ITSELF read the override? It must BE the read
    or BE a bare trusted name (after unwrapping trivial calls) — not
    merely contain one somewhere inside."""
    expr = _unwrap(expr)
    if _is_env_read(expr):
        return True
    return isinstance(expr, ast.Name) and expr.id in trusted


def _guard_verdict(node, parents, trusted):
    """(guarded, offending_name). Climb ancestors: is this fallback
    structurally subordinate to a guard? offending_name names the guard
    variable that failed trust, for the offender message."""
    child = node
    parent = parents.get(child)
    failed_name = None
    while parent is not None:
        # Inside an env read the fallback can only be the default —
        # positional (args[1:]) or keyword — since args[0] is the var name.
        if _is_env_read(parent):
            return True, None
        candidates = ()
        if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.Or):
            candidates = parent.values[:parent.values.index(child)]
        elif isinstance(parent, ast.IfExp) and child is parent.orelse:
            candidates = (parent.test,)
        for cand in candidates:
            if _is_guard(cand, trusted):
                return True, None
            bare = _unwrap(cand)
            if isinstance(bare, ast.Name) and failed_name is None:
                failed_name = bare.id
        # Plain nesting (Call args like Path(...)/str(...), BinOps that
        # append segments, parens) keeps climbing.
        child, parent = parent, parents.get(parent)
    return False, failed_name


# Shown verbatim whenever the guard fires. The scanner over-flags on
# purpose, so it owes every developer it stops a way out.
_REMEDIATION = """
HOW TO CLEAR THIS (pick the first one that fits):

 1. The path SHOULD honor the override (almost always the case). Put the
    read in the expression itself, so no variable has to be trusted:
        AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT')
                           or Path.home() / 'agents')
    or use the env-get default:
        Path(os.environ.get('OURLIBERTY_AGENTS_ROOT',
                            str(Path.home() / 'agents')))

 2. You used the two-line idiom and it still fired. Then the guard
    variable is not trusted, and the reason is printed above:
        root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        return Path(root) if root else Path.home() / 'agents'
    A name is trusted ONLY if EVERY binding of it in the whole file is
    that same env read. Give the variable a name of its own (this file
    probably reuses it for another env var or another purpose), or fold
    the read into the expression as in 1. Note the guard must BE the
    variable or the read -- `cfg.get(root) or <fallback>` does not count.

 3. It is inside a string (a `python3 -c` payload, a shell command). The
    text channel does not parse code; spell the guard in the payload the
    shell way, which the scanner recognizes:
        "${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}"

 4. The bare REAL-home path is deliberate (the test jail's RO-bind
    targets and tripwire must point at the real tree, never the sandbox).
    Add a per-site waiver with a reason -- the rest of the file stays
    scanned:
        ALLOWED_SITES[('<file>.py', "<exact expression>")] = "<why>"
    Whole-file ALLOWED_FILES entries are a last resort: they switch this
    check off for everything in that file.

This guard deliberately over-flags: a noisy false alarm costs you this
message, while a missed one silently writes tier-2 state into the wrong
home. Do NOT widen the trust rule to silence a single site -- use 1 or 4.
"""


def find_bare_agents_roots(source, filename="<source>"):
    """Return ['file:line: segment -- reason', ...] for every unguarded
    fallback. Each entry says WHY it fired; _REMEDIATION says what to do."""
    tree = ast.parse(source, filename=filename)
    parents = {}
    for node in ast.walk(tree):
        for kid in ast.iter_child_nodes(node):
            parents[kid] = node
    trusted = _trusted_names(tree, parents)
    offenders = []

    def _scan_text(node, text, kind="in string"):
        """The text channel, for code that runs in a spawned child. It
        does NOT parse the payload — it matches per line, and the ONLY
        waiver is the shell default-expansion spelling, which puts the
        read and the fallback in one unambiguous construct. Anything else
        in a string is flagged; see _REMEDIATION items 3 and 4."""
        if "agents" not in text:  # cheap pre-filter; every spelling has it
            return
        for line in text.splitlines():
            for match in TEXT_BARE.finditer(line):
                if _inside_shell_default(line, match.start()):
                    continue
                offenders.append(
                    f"{filename}:{node.lineno}: [{kind}] "
                    + " ".join(line.split())[:80]
                    + " -- bare fallback in a string; the only in-string"
                    " guard recognized is \"${%s:-...}\"" % ENV_VAR
                )
                break

    for node in ast.walk(tree):
        if _is_agents_fallback(node):
            segment = " ".join(
                (ast.get_source_segment(source, node) or "").split()
            )
            if (filename, segment) in ALLOWED_SITES:
                continue
            guarded, failed_name = _guard_verdict(node, parents, trusted)
            if guarded:
                continue
            if failed_name is None:
                reason = (
                    "not inside any override-guarded expression"
                )
            elif failed_name in _collect_bindings(tree, parents):
                reason = (
                    f"guarded by {failed_name!r}, which is NOT trusted: at"
                    f" least one binding of {failed_name!r} in this file is"
                    f" something other than a read of {ENV_VAR}"
                )
            else:
                reason = (
                    f"guarded by {failed_name!r}, which this file never"
                    f" binds from {ENV_VAR}"
                )
            offenders.append(f"{filename}:{node.lineno}: {segment} -- {reason}")
        elif isinstance(node, ast.JoinedStr):
            # An f-string is ONE string at runtime but many Constant
            # fragments in the AST. Join them (interpolations become an
            # opaque placeholder, so fragments cannot create false
            # adjacency) and scan the result.
            _scan_text(node, "".join(
                v.value
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
                else "\x00"
                for v in node.values
            ))
        elif (
            isinstance(node, ast.Constant)
            and not isinstance(parents.get(node), ast.JoinedStr)
        ):
            if isinstance(node.value, str):
                _scan_text(node, node.value)
            elif isinstance(node.value, bytes):
                # bytes commands are legal for shell=True subprocesses.
                _scan_text(node, node.value.decode("latin-1"), "in bytes")
    return offenders


class TestAgentsRootOverride(unittest.TestCase):
    def test_no_bare_agents_root_without_override(self):
        offenders = []
        # Non-recursive BY DESIGN: scripts/tests/ holds this guard's own
        # attack fixtures (and the subprocess payloads in
        # test_log_dir_resolution.py), which would self-flag if the glob
        # were ever widened.
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
            "(a per-tier HOME swap would otherwise blind these).\n\n"
            "Offenders:\n  - "
            + "\n  - ".join(offenders)
            + "\n"
            + _REMEDIATION,
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

    def test_scope_free_limit_function_local_binding(self):
        # KNOWN, DELIBERATE LIMIT. The rule does not model scope, so a
        # binding anywhere in the file can bless a use anywhere else. Here
        # the module-level `root` is bound only inside a function, so this
        # code raises NameError the moment it runs — it cannot ship a
        # wrong path, it cannot run at all. Buying detection of
        # already-broken code back would mean reintroducing the scope
        # model that produced three rounds of bypasses; that trade is
        # refused on purpose. See the module docstring, "What this cannot
        # see".
        self.assert_quiet(
            "def f():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return root\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")

    def test_scope_free_limit_nested_def_binding(self):
        # Same limit one level down, and same reason it is harmless: the
        # outer function's `root` is unbound at runtime (NameError).
        self.assert_quiet(
            "def outer():\n"
            "    def inner():\n"
            "        root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "        return root\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_scope_free_limit_class_body_binding(self):
        # Same limit again (a class attribute is not a module global), and
        # again the code NameErrors rather than resolving a wrong path.
        self.assert_quiet(
            "class Cfg:\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_param_binding_with_dead_env_read(self):
        # The parameter spelling of the rebind attack: a parameter is a
        # binding whose value cannot be inspected, so it voids the trust
        # a dead-branch env read would otherwise earn.
        self.assert_flags(
            "def f(root):\n"
            "    if False:\n"
            "        root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_loop_target_rebind(self):
        self.assert_flags(
            "def f(items):\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    for root in items:\n"
            "        pass\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_tuple_unpack_rebind(self):
        self.assert_flags(
            "def f(pair):\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root, other = pair\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_with_as_rebind(self):
        self.assert_flags(
            "def f(cm):\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    with cm as root:\n"
            "        pass\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_except_as_rebind(self):
        self.assert_flags(
            "def f():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    try:\n"
            "        pass\n"
            "    except KeyError as root:\n"
            "        pass\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_import_as_rebind(self):
        self.assert_flags(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "import fallback_config as root\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_lambda_param_shadow(self):
        # The lambda spelling of the parameter attack: the name guarding
        # the fallback is the lambda's own param, not the module binding.
        self.assert_flags(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "f = lambda root: Path(root) if root else"
            " Path.home() / 'agents'\n")

    def test_flags_comprehension_target_shadow(self):
        # Comprehension targets are their own scope's bindings: inside
        # the element expression, `root` is the iterable's value.
        self.assert_flags(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "xs = [Path(root) if root else Path.home() / 'agents'"
            " for root in candidates]\n")

    def test_flags_derive_laundering_foreign_value(self):
        # A rebind that MENTIONS the name but mixes in a foreign source
        # is not normalization — the live value may be cfg.root.
        self.assert_flags(
            "def f(cfg):\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root = cfg.root or root\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_comprehension_shadowed_mention(self):
        # The `root` inside the genexp is a different variable; the
        # mention must not count as derivation.
        self.assert_flags(
            "def f(items):\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root = min(root for root in items)\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_global_rebind_through_nested_def(self):
        # A `global root` write inside a nested def rebinds the MODULE
        # name — the one enclosing-scope effect an opaque nested scope
        # can have.
        self.assert_flags(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "def clobber(v):\n"
            "    global root\n"
            "    root = v\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_def_statement_rebind(self):
        # `def root():` binds the name to a function object — an
        # uninspectable rebind, fail closed.
        self.assert_flags(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "def root():\n"
            "    pass\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_class_statement_rebind(self):
        self.assert_flags(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "class root:\n"
            "    pass\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_type_alias_rebind(self):
        # PEP 695 (py3.12): `type root = ...` is a binding too.
        self.assert_flags(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "type root = int\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_del_then_fallback(self):
        self.assert_flags(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "del root\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_quoted_shell_home_spelling(self):
        # Shell-correct quoting of the variable only: same path.
        self.assert_flags("cmd = 'ls \"$HOME\"/agents/state'\n")

    def test_flags_bytes_payload(self):
        # bytes commands are legal for shell=True subprocesses.
        self.assert_flags("cmd = b'ls $HOME/agents/state'\n")

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

    def test_flags_unrelated_bare_line_in_multiline_payload(self):
        # A read on ONE line of a payload must not silence a bare fallback
        # on ANOTHER — the waiver is per line, or joining f-string
        # fragments would have made one read whole-string.
        self.assert_flags(
            'script = """\n'
            "state = os.environ.get('OURLIBERTY_AGENTS_ROOT') or '/x'\n"
            "logs = Path.home() / 'agents' / 'logs'\n"
            '"""\n')

    def test_flags_docstring_spelling_the_fallback(self):
        # Pin the string-channel contract for prose: a docstring is a
        # string constant like any other. Round 1 waived prose, round 2
        # silently reversed that; this is the first test of either. A doc
        # that verbatim spells the bare fallback FLAGS — fix the doc, or
        # (for a true payload) guard it with a read spelling.
        self.assert_flags(
            '"""Reads Path.home() / \'agents\' at startup."""\n')

    def test_flags_read_used_only_as_an_argument(self):
        # "Contains the read somewhere" is not "is the read": paths.index()
        # returns an INT (0 is falsy, so the bare fallback fires).
        self.assert_flags(
            "def f(paths):\n"
            "    root = paths.index(os.environ.get('OURLIBERTY_AGENTS_ROOT'))\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_rebind_through_arbitrary_call(self):
        # `lookup(root)` returns whatever lookup returns.
        self.assert_flags(
            "def f():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root = lookup(root)\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_guard_that_merely_mentions_a_trusted_name(self):
        # The guard must BE the name or the read — `cfg.get(root)` is
        # cfg's value, not the override's.
        self.assert_flags(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "p = cfg.get(root) or Path.home() / 'agents'\n")

    def test_flags_walrus_in_def_header(self):
        # A walrus in a def header binds the ENCLOSING scope.
        self.assert_flags(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "def g(x=(root := '/attacker')):\n"
            "    pass\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_name_reused_for_a_different_env_var(self):
        # The live heal_phantom_dispatch_claim shape before its rename:
        # one name meaning several env vars. This is remediation 2.
        self.assert_flags(
            "def a():\n"
            "    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(override) if override else Path.home() / 'agents'\n"
            "def b():\n"
            "    override = os.environ.get('OURLIBERTY_LOG_DIR')\n"
            "    return Path(override)\n")

    def test_flags_shell_read_elsewhere_on_the_line(self):
        # A read for one purpose does not guard a bare path used for
        # another.
        self.assert_flags(
            "cmd = 'echo $OURLIBERTY_AGENTS_ROOT; rm -rf $HOME/agents'\n")

    def test_flags_shell_line_that_sets_the_var_from_the_bare_path(self):
        # Actively defeats the pin: stomps the override to the bare path,
        # then reads it back.
        self.assert_flags(
            "cmd = 'OURLIBERTY_AGENTS_ROOT=$HOME/agents run.sh"
            " --root $OURLIBERTY_AGENTS_ROOT'\n")

    def test_flags_shell_payload_that_parses_as_python(self):
        # `rf"` is a raw-f-string prefix, so this shell command is also
        # valid Python — which is why the text channel must not parse.
        self.assert_flags("cmd = 'rm -rf\"$HOME\"/agents'\n")

    def test_flags_expanduser_wrapper_spelling_in_payload(self):
        # The text channel must cover what the AST channel flags.
        self.assert_flags(
            "cmd = \"python3 -c 'print(Path(\\\"~/agents\\\").expanduser())'\"\n")

    def test_reports_the_reason_it_fired(self):
        # The message must diagnose, not just accuse: an untrusted guard
        # name is named, and the remediation text explains the exits.
        offenders = find_bare_agents_roots(
            "def f(a):\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root = a.root\n"
            "    return Path(root) if root else Path.home() / 'agents'\n",
            filename="victim.py")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("'root'", offenders[0])
        self.assertIn("NOT trusted", offenders[0])
        for cue in ("HOW TO CLEAR THIS", "ALLOWED_SITES", ENV_VAR):
            self.assertIn(cue, _REMEDIATION)

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

    def test_flags_payload_string_with_python_read(self):
        # DELIBERATE OVER-FLAG — see the f-string case above.
        self.assert_flags(
            "cmd = \"python3 -c 'import os; print(os.environ.get("
            "\\\"OURLIBERTY_AGENTS_ROOT\\\") or Path.home() / \\'agents\\')'\"\n")

    def test_ignores_comments_and_plain_prose(self):
        self.assert_quiet(
            "# was hardcoded to HOME/'agents' before the audit\n"
            "x = 1\n")

    def test_flags_fstring_payload_with_python_read(self):
        # DELIBERATE OVER-FLAG: a Python read spelling inside a string is
        # no longer a waiver (one read on a line waived unrelated bare
        # paths on it). Only the shell default-expansion is recognized.
        self.assert_flags(
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

    def test_flags_normalize_after_env_read(self):
        # DELIBERATE OVER-FLAG. Normalizing the read is safe in fact, but
        # "derives from the name" is the inference that kept springing
        # leaks (`root = cfg.root or root` launders a foreign value the
        # same way). The rule is now "every binding IS the read"; the fix
        # is to normalize inside the guarded expression. Remediation 1.
        self.assert_flags(
            "def f():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root = root.rstrip('/') if root else None\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_augmented_rebind(self):
        # DELIBERATE OVER-FLAG (same reason as normalization above): `+=`
        # is a second binding whose value is not the read.
        self.assert_flags(
            "def f():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root += '/sub'\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_name_reused_by_unrelated_class_attr(self):
        # DELIBERATE OVER-FLAG: the rule is scope-free, so reusing the
        # name anywhere in the file costs it its trust. The fix is to give
        # one of them its own name (remediation 2) — which is also what
        # made the live heal_phantom_dispatch_claim site honest.
        self.assert_flags(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "class Cfg:\n"
            "    root = 'unrelated-class-attr'\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")

    def test_allows_closure_read_of_module_guarded_name(self):
        # LEGB: a function with NO local binding of `root` reads the
        # module's trusted binding — flagging this pushes correct
        # refactors (hoist the env read to a module constant) into
        # ALLOWED_FILES entries.
        self.assert_quiet(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "def f():\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_allows_class_body_read_of_module_guarded_name(self):
        # Class bodies resolve module globals; the module binding is the
        # one guarding the fallback.
        self.assert_quiet(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "class Cfg:\n"
            "    path = Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_param_default_or_env_idiom(self):
        # DELIBERATE OVER-FLAG. Softening the parameter rule is exactly
        # what reopened the dead-branch attack: a dead copy of this same
        # rebind blessed a live foreign parameter. A parameter is now an
        # uninspectable binding, always. Fix per remediation 1.
        self.assert_flags(
            "def resolve(root=None):\n"
            "    root = root or os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_flags_wrapped_python_payload(self):
        # DELIBERATE OVER-FLAG. Parsing payloads as Python mis-channelled
        # shell commands that happen to parse (`rm -rf"$HOME"/agents`) and
        # let a TEXT_BARE pre-filter silently skip whole payloads. The
        # text channel no longer parses: use remediation 3 or 4.
        self.assert_flags(
            'script = """\n'
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT') or str(\n"
            "    Path.home() / 'agents')\n"
            '"""\n')

    def test_allows_guarded_shell_expansion_idiom(self):
        # The canonical guarded shell spelling reads the override via
        # default-expansion; punishing it would demand the fix and then
        # flag it.
        self.assert_quiet(
            "cmd = 'ROOT=\"${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}\"'\n")

    def test_allowed_sites_exempts_one_segment_not_the_file(self):
        # Per-site waiver granularity: the registered segment is exempt,
        # the rest of the same file stays in scope.
        src = ("a = Path.home() / 'agents'\n"
               "b = HOME / 'agents'\n")
        with mock.patch.dict(
            ALLOWED_SITES,
            {("victim.py", "Path.home() / 'agents'"): "test waiver"},
        ):
            offenders = find_bare_agents_roots(src, filename="victim.py")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("HOME / 'agents'", offenders[0])

    def test_allows_shell_default_expansion(self):
        # The one in-string guard: read and fallback in one construct.
        self.assert_quiet(
            "cmd = 'ROOT=\"${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}\"'\n")

    def test_allows_environ_subscript_read(self):
        # os.environ[VAR] is as real a read as environ.get(VAR).
        self.assert_quiet(
            "def f():\n"
            "    root = os.environ['OURLIBERTY_AGENTS_ROOT']\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_allows_read_wrapped_in_path_constructor(self):
        # Path()/str() wrappers are transparent for the trust rule.
        self.assert_quiet(
            "def f():\n"
            "    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT'))\n"
            "    return root if root else Path.home() / 'agents'\n")

    def test_allows_name_bound_twice_from_the_same_read(self):
        # "EVERY binding is the read" — not "exactly one binding".
        self.assert_quiet(
            "def a():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n"
            "def b():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_allows_shell_sibling_directory(self):
        # $HOME/agents-archive is a different tree entirely.
        self.assert_quiet('cmd = "ls $HOME/agents-archive"\n')

    def test_allows_expanduser_sibling_directory(self):
        self.assert_quiet(
            "cmd = \"python3 -c 'print(os.path.expanduser("
            "\\\"~/agents-old\\\"))'\"\n")

    def test_allows_tilde_agents_in_docstring_prose(self):
        # Bare ~/agents tokens are OUT OF SCOPE for the text channel: they
        # are pervasive in docstring prose across scripts/ and cannot
        # discriminate a payload from documentation.
        self.assert_quiet(
            '"""State lives under ~/agents (see ~/agents/logs/)."""\n'
            "x = 1\n")


if __name__ == "__main__":
    unittest.main()
