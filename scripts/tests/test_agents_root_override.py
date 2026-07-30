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
Name-trust mirrors Python name resolution: the nearest scope that BINDS
the name renders the verdict, and scopes that never bind it are
transparent (a module-level `root = <env read>` blesses a closure or
class-body use; a parameter, comprehension target, or any local rebind is
judged where it lives). Within the binding scope the check is
flow-insensitive, so it accounts for the WHOLE binding set fail-closed:
only Assign/AnnAssign/NamedExpr/AugAssign to a bare Name are inspectable;
trust requires at least one binding containing the env read, with every
other binding either reading too or PRESERVING the read — deriving from
the name and from nothing foreign (`root = root.rstrip('/') if root else
None` preserves; `root = cfg.root or root` launders a foreign value
through a mention and voids). ANY other binding of the name — a
`for`/`with`/`except` target, tuple unpacking, `import ... as`, a match
capture, a nested `def`/`class` statement of that name, a type alias,
`del`, or a `global`/`nonlocal` write reaching in from a nested scope —
is uninspectable and voids outright. A parameter voids unless every
inspectable rebind both reads the env and derives from the param (the
`root = root or os.environ.get(ENV_VAR)` idiom).

Bare fallbacks embedded in string literals (python -c payloads, shell
command strings, bytes commands) are scanned too, since the code that
runs in a spawned child is exactly where the HOME swap bites. A payload
that parses as Python is recursed through this same checker, so a
genuinely guarded multi-line payload is blessed and a read on one line
cannot waive an unrelated bare fallback on another. Unparseable text
(shell, prose, f-string fragment joins — fragments are joined with opaque
placeholders standing in for interpolations) falls back to per-LINE
matching, where a line passes only if it itself spells a READ of the
override — a Python call spelling or a shell `$OURLIBERTY_AGENTS_ROOT` /
`${OURLIBERTY_AGENTS_ROOT...}` expansion (so the canonical guarded
`"${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}"` is quiet). Merely NAMING the
var proves nothing. Deliberately OUT OF SCOPE for the text channel: a
bare ``~/agents`` token (pervasive in docstring prose, so it cannot
discriminate payloads from documentation) and home-alias spellings (see
the ALLOWED_FILES / ALLOWED_SITES notes) — those rely on the AST channel
or the allowlists.
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
    # Pre-registered for the invisible cousin above: if a refactor ever
    # inlines the REAL_HOME alias, this is the segment that would surface.
    ("test_regression_check.py", "REAL_HOME / 'agents'"):
        "jail RO-bind target / tripwire — must point at the REAL home",
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
    r"|\bhome\(\)\s*\.\s*joinpath\(\s*['\"]agents['\"]"
    # optional closing quote: shell-correct quoting ("$HOME"/agents,
    # "${HOME}"/agents) is the same path.
    r"|\$HOME['\"]?/agents(?![\w.-])|\$\{HOME\}['\"]?/agents(?![\w.-])"
)

# A string line is presumed guarded only when it contains a READ spelling
# of the override — Python call spellings, or the shell expansions
# ($OURLIBERTY_AGENTS_ROOT / ${OURLIBERTY_AGENTS_ROOT...}, which cover the
# canonical guarded idiom "${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}").
# Merely naming the var (an export line, a docstring mentioning it) used
# to waive — that blessed any payload that set the var without reading it.
TEXT_READ = re.compile(
    r"(?:(?<!\w)getenv\s*\(|environ\.get\s*\(|environ\[)\s*['\"]"
    + re.escape(ENV_VAR)
    + r"['\"]"
    + r"|\$\{?" + re.escape(ENV_VAR) + r"\b"
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


# Namespaces whose LOCAL bindings are invisible outside them (and vice
# versa, except through the resolution chain below). Comprehensions are
# boundaries for USE-sites (their targets shadow enclosing names) but are
# NOT skipped when collecting an enclosing scope's bindings — a walrus
# inside a comprehension binds the ENCLOSING scope (PEP 572), and the
# comprehension's own targets are excluded by form, not by skipping.
_SCOPE_TYPES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
)
_OPAQUE_WALK_SKIP = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
)
_TYPE_ALIAS = getattr(ast, "TypeAlias", None)  # PEP 695, py3.12+


def _enclosing_scope(node, parents):
    n = parents.get(node)
    while n is not None and not isinstance(n, _SCOPE_TYPES + (ast.Module,)):
        n = parents.get(n)
    return n


def _scope_walk(scope):
    """Walk `scope` WITHOUT descending into nested opaque namespaces
    (functions, lambdas, class bodies): their local bindings are invisible
    to the enclosing scope, and trusting them blessed cross-scope.
    Comprehensions ARE descended into (see _SCOPE_TYPES note)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _OPAQUE_WALK_SKIP):
            stack.extend(ast.iter_child_nodes(node))


def _target_names(target):
    """Names bound by an assignment-style target, including tuple/list
    unpacking and starred targets (attribute/subscript targets bind no
    name)."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _target_names(elt)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)


def _scope_params(scope):
    """Parameter names a function-like scope binds; comprehension targets
    for comprehension scopes; nothing for classes/modules."""
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = scope.args
        params = [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
        return {p.arg for p in params if p is not None}
    if isinstance(scope, (ast.ListComp, ast.SetComp, ast.DictComp,
                          ast.GeneratorExp)):
        names = set()
        for gen in scope.generators:
            names.update(_target_names(gen.target))
        return names
    return set()


def _value_names(expr, out):
    """Collect the Names in a binding value that speak to WHOSE data it
    is. Skips: env-read subtrees (the trusted source itself), nested-scope
    subtrees (their names are different variables — `min(root for root in
    items)` derives nothing from `root`), and a bare-Name call func
    (`str(root)`'s `str` is plumbing, not a data source; an Attribute
    receiver like `root.rstrip` still counts its Names)."""
    if _is_env_read(expr) or isinstance(expr, _SCOPE_TYPES):
        return
    if isinstance(expr, ast.Name):
        out.append(expr.id)
        return
    if isinstance(expr, ast.Call):
        if not isinstance(expr.func, ast.Name):
            _value_names(expr.func, out)
        for kid in ast.iter_child_nodes(expr):
            if kid is not expr.func:
                _value_names(kid, out)
        return
    for kid in ast.iter_child_nodes(expr):
        _value_names(kid, out)


def _binding_event(name, value, implicit_self=False):
    """(contains_read, trust_preserving) for one inspectable binding.
    Trust-preserving means the value derives from `name` and from nothing
    foreign: `root = root.rstrip('/') if root else None` preserves;
    `root = cfg.root or root` launders a foreign value through a mention
    and does NOT; `root = None` resets and does not."""
    read = any(_is_env_read(n) for n in ast.walk(value))
    names = []
    _value_names(value, names)
    preserves = (implicit_self or name in names) and all(
        n == name for n in names
    )
    return (read, preserves)


def _scope_binding_verdict(name, scope):
    """(bound, trusted) for `name` judged against `scope`'s OWN bindings.
    (False, False) means the scope never binds the name at all — callers
    then consult the enclosing scope, mirroring Python name resolution.

    The check is flow-insensitive — it cannot see WHICH binding reaches
    the fallback — so it compensates by accounting for the WHOLE binding
    set, fail-closed on anything it cannot inspect:

      * inspectable forms (Assign/AnnAssign/NamedExpr/AugAssign to a bare
        Name) contribute (read, preserving) events: trust requires at
        least one read and every event read or trust-preserving;
      * any OTHER binding of the name — a `for`/`with`/`except` target,
        tuple unpacking, `import ... as`, a match capture, a nested
        `def`/`class` STATEMENT named `name`, a PEP 695 type alias,
        `del`, a `global`/`nonlocal` declaration here or a
        `global`/`nonlocal` write reaching in from a nested scope — is
        uninspectable and voids the trust outright;
      * a parameter (or comprehension target) binding voids UNLESS every
        inspectable rebind both reads the env and derives from the name
        (`def resolve(root=None): root = root or os.environ.get(...)` is
        the guard's own or-chain in two-line form; `def f(root)` beside a
        dead `if False:` env read is not, and stays voided)."""
    param_bound = name in _scope_params(scope)
    events = []
    for sub in _scope_walk(scope):
        if isinstance(sub, ast.Assign):
            for t in sub.targets:
                if isinstance(t, ast.Name):
                    if t.id == name:
                        events.append(_binding_event(name, sub.value))
                elif name in _target_names(t):
                    return (True, False)  # unpacking: not attributable
        elif isinstance(sub, (ast.AnnAssign, ast.NamedExpr)):
            if (
                isinstance(sub.target, ast.Name)
                and sub.target.id == name
                and sub.value is not None
            ):
                events.append(_binding_event(name, sub.value))
        elif isinstance(sub, ast.AugAssign):
            if isinstance(sub.target, ast.Name) and sub.target.id == name:
                # x += v is x = x + v: derives from itself, plus v.
                events.append(_binding_event(name, sub.value,
                                             implicit_self=True))
        elif isinstance(sub, (ast.For, ast.AsyncFor)):
            if name in _target_names(sub.target):
                return (True, False)
        elif isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                if item.optional_vars and name in _target_names(
                    item.optional_vars
                ):
                    return (True, False)
        elif isinstance(sub, ast.ExceptHandler):
            if sub.name == name:
                return (True, False)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return (True, False)
        elif isinstance(sub, (ast.MatchAs, ast.MatchStar)):
            if sub.name == name:
                return (True, False)
        elif isinstance(sub, ast.MatchMapping):
            if sub.rest == name:
                return (True, False)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            if sub.name == name:
                return (True, False)  # def root(): / class root: rebind
            # A global/nonlocal write INSIDE the nested scope rebinds THIS
            # scope's name — the one enclosing-scope effect a nested def
            # can have, so it must void even though the def is opaque.
            for inner in ast.walk(sub):
                if isinstance(inner, (ast.Global, ast.Nonlocal)):
                    if name in inner.names:
                        return (True, False)
        elif isinstance(sub, (ast.Global, ast.Nonlocal)):
            if name in sub.names:
                return (True, False)  # this scope aliases another scope's
        elif isinstance(sub, ast.Delete):
            if any(isinstance(t, ast.Name) and t.id == name
                   for t in sub.targets):
                return (True, False)
        elif _TYPE_ALIAS is not None and isinstance(sub, _TYPE_ALIAS):
            if isinstance(sub.name, ast.Name) and sub.name.id == name:
                return (True, False)
    if param_bound:
        return (True, bool(events) and all(r and p for r, p in events))
    if not events:
        return (False, False)
    return (True, any(r for r, _ in events)
            and all(r or p for r, p in events))


def _trusted_name(name, scope, parents):
    """Resolve `name` from `scope` outward (LEGB): the nearest scope that
    binds it at all renders the verdict; scopes that never bind it are
    transparent, exactly like Python name resolution — so a module-level
    `root = <env read>` blesses a closure or class-body use, while any
    local binding (parameter, shadow, rebind) is judged where it lives."""
    s = scope
    while s is not None:
        bound, trusted = _scope_binding_verdict(name, s)
        if bound:
            return trusted
        if isinstance(s, ast.Module):
            return False
        s = _enclosing_scope(s, parents)
    return False


def _reads_override(expr, scope, parents):
    """True if the expression reads the override — directly, or through a
    name resolving (see _trusted_name) to a binding from that read. Names
    are trusted only by their BINDING, never by what they are called."""
    for sub in ast.walk(expr):
        if _is_env_read(sub):
            return True
        if isinstance(sub, ast.Name) and _trusted_name(sub.id, scope, parents):
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
            if any(_reads_override(v, scope, parents)
                   for v in parent.values[:idx]):
                return True
        if isinstance(parent, ast.IfExp) and child is parent.orelse:
            if _reads_override(parent.test, _enclosing_scope(parent, parents),
                               parents):
                return True
        # Plain nesting (Call args like Path(...)/str(...), BinOps that
        # append segments, parens) keeps climbing.
        child, parent = parent, parents.get(parent)
    return False


def find_bare_agents_roots(source, filename="<source>", _depth=0):
    """Return ['file:line: segment', ...] for every unguarded fallback."""
    tree = ast.parse(source, filename=filename)
    parents = {}
    for node in ast.walk(tree):
        for kid in ast.iter_child_nodes(node):
            parents[kid] = node
    offenders = []

    def _scan_payload(node, text):
        """The string channel. A payload that PARSES as Python gets the
        full AST checker recursively — so the standard two-line guarded
        idiom inside a python -c/heredoc payload is blessed, and a read on
        one line cannot waive an unrelated bare fallback on another.
        Unparseable text (shell, prose, f-string placeholder joins) falls
        back to per-LINE matching: a line flags when it spells a bare
        fallback and does not itself spell a read of the override."""
        if not TEXT_BARE.search(text):
            return  # hot path: nothing bare anywhere in the string
        if _depth < 2:
            try:
                sub_offenders = find_bare_agents_roots(
                    text,
                    filename=f"{filename}:{node.lineno}[payload]",
                    _depth=_depth + 1,
                )
            except (SyntaxError, ValueError):
                pass  # not Python (ValueError: \x00 placeholders)
            else:
                offenders.extend(sub_offenders)
                return
        for line in text.splitlines():
            if TEXT_BARE.search(line) and not TEXT_READ.search(line):
                offenders.append(
                    f"{filename}:{node.lineno}: [in string] "
                    + " ".join(line.split())[:80]
                )

    for node in ast.walk(tree):
        if _is_agents_fallback(node) and not _is_guarded(node, parents):
            segment = " ".join(
                (ast.get_source_segment(source, node) or "").split()
            )
            if (filename, segment) in ALLOWED_SITES:
                continue
            offenders.append(f"{filename}:{node.lineno}: {segment}")
        elif isinstance(node, ast.JoinedStr):
            # An f-string is ONE string at runtime but many Constant
            # fragments in the AST: the env read can sit in a different
            # fragment from the fallback. Join the constant fragments
            # before matching — scanned per-fragment, the waiver could
            # never see the read, leaving a guarded payload no exit but
            # ALLOWED_FILES. Each interpolation becomes an opaque
            # placeholder, so fragments cannot create false adjacency.
            _scan_payload(node, "".join(
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
                _scan_payload(node, node.value)
            elif isinstance(node.value, bytes):
                # bytes commands are legal for shell=True subprocesses;
                # scan them through the same channel.
                _scan_payload(node, node.value.decode("latin-1"))
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

    def test_flags_class_body_env_read_does_not_bless_module(self):
        # A class body is its own namespace: `Cfg.root` is not the
        # module-level `root` guarding the fallback.
        self.assert_flags(
            "class Cfg:\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
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

    def test_allows_normalize_after_env_read(self):
        # A rebind that DERIVES from the read (normalization) keeps the
        # trust — flagging it would leave correct code no exit but
        # ALLOWED_FILES.
        self.assert_quiet(
            "def f():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root = root.rstrip('/') if root else None\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_allows_augmented_self_derivation(self):
        # x += v is x = x + v: it starts from the read value.
        self.assert_quiet(
            "def f():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root += '/sub'\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_allows_module_trust_despite_unrelated_class_attr(self):
        # The mirror image of the class-body flag test: a class attribute
        # named `root` is not a module-level rebind and must not void a
        # correct module-level binding.
        self.assert_quiet(
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

    def test_allows_param_default_or_env_idiom(self):
        # `def resolve(root=None): root = root or <env read>` is the
        # guard's own or-chain in two-line form: the param must not void
        # when every rebind both reads the env and derives from it.
        self.assert_quiet(
            "def resolve(root=None):\n"
            "    root = root or os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_allows_wrapped_guarded_python_payload(self):
        # A payload that parses as Python is recursed through the full
        # AST checker: the standard guarded idiom wrapped across lines is
        # blessed — the wrapped-line false positive this module's own
        # history records must not return through the text channel.
        self.assert_quiet(
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
