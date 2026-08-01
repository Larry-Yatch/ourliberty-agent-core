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
  syntactically that same read (stdlib ``Path()``/``str()`` wrappers
  allowed) — and if the binding IS wrapped, the name is not usable as a
  truthiness guard, because ``Path('')`` and ``str(None)`` are both truthy.

That is the entire trust model. No scope chain, no dataflow, no "derives
from", no parameter special-cases.

THE ORGANIZING PRINCIPLE — DIRECTIONAL BIAS. Every predicate in this file
sits on one of two sides, and the two sides get OPPOSITE treatment:

  * DETECTION (what counts as a home, a segment, a composition, a scanned
    file). Being loose here produces MORE flags. A false flag costs one
    message. So detection unwraps permissively (``_unwrap_loose``), trusts
    the bare name ``HOME`` and any attribute ``.HOME`` by spelling alone,
    and enumerates every composition operator it can.

  * BLESSING (what counts as a read, a wrapper, an ``environ``, a waiver).
    Being loose here produces a SILENT FALSE CLEAN, which writes tier-2
    state into the wrong home. So blessing unwraps only provably-stdlib
    wrappers (``_unwrap_strict``), makes the ``environ`` receiver EARN its
    identity from its bindings rather than its spelling, does not unwrap at
    all inside ``_is_guard``, and refuses an ambiguous waiver outright.

One sentence: NAME-TRUST IS FREE ON THE FLAGGING SIDE AND MUST BE EARNED ON
THE BLESSING SIDE. When a change makes something quieter, it belongs on the
blessing side and needs binding evidence; when it makes something louder, a
spelling match is enough.

WHY SO BLUNT. Three review rounds of a cleverer rule — scope-aware name
resolution, derivation analysis, parameter softening, payload parsing —
produced 5, then 10, then 14 real defects, most of them created by the
previous round's own fix. The tell was directional: every fix made the
BLESSING logic smarter, and a bigger trusted surface is a bigger attack
surface. A blessing rule you cannot audit in one sitting is one you cannot
trust, however many tests pin it — tests only pin the bypasses someone
already thought of. So the rule is deliberately blunt, and it OVER-FLAGS
on purpose: a loud false alarm costs a developer one message (every
offender says why it fired, prints its own paste-ready waiver key, and
``_REMEDIATION`` says how to clear it), while a silent false clean writes
tier-2 state into the wrong home. When this fires on correct code, fix it
with remediation 1 or 4 — do NOT widen the trust rule.

TWO CHANNELS, ONE SET OF FRAGMENTS. The AST channel judges real code in
``scripts/*.py``. The text channel judges string/bytes/f-string constants
in those files AND the raw text of ``scripts/*.sh`` — the shell runners are
production entry points that build the same state paths, and a payload that
moves from a Python string into a .sh file must not thereby turn the check
off. Both text uses run the SAME per-line matcher, and the matcher's home
and segment fragments are built once (``_TEXT_HOME``, ``_TEXT_SEGMENT``) so
the two channels cannot drift into disagreeing about what an agents path
looks like.

The text channel does NOT parse code — parsing mis-channelled shell
commands that happened to be valid Python. Its ONE waiver is the shell
default-expansion ``${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}``, which binds
read and fallback into a single unambiguous construct. That waiver is
FAIL-CLOSED: the expansion must be CLOSED on the SAME LINE, at or after the
match. An unterminated ``${VAR:-`` waives nothing (an f-string
interpolation can supply a closing brace this scanner cannot see, so a
``bash -n``-clean payload could otherwise carry a bare $HOME/agents
through). A read merely elsewhere on the line waives nothing either.

WHAT THIS CANNOT SEE, deliberately. Each label below is an entry in
``DECLARED_BLIND_SPOTS``; each has a fixture proving it is quiet, and
``test_declared_blind_spots_are_real_and_disclosed`` asserts this list and
that list stay in sync — so this section cannot drift back into claiming
coverage the code does not have.

  * scope-free trust — a binding anywhere in the file can bless a use
    anywhere else. Every such case is code that raises NameError before it
    can resolve a path, so it cannot ship a wrong path; buying it back
    means reintroducing the scope model that produced three rounds of
    bypasses. Pinned by the ``test_scope_free_limit_*`` tests.
  * alias-bound home — ``home = os.environ.get('HOME',''); Path(home) /
    'agents'``. Only the INLINE env-HOME spellings are recognized. Alias
    tracking would flag the test jail's intentional real-home RO-bind
    targets, which must NOT honor the override (see the ALLOWED_FILES
    note). The behavioral net (``test_modules_resolve_under_override``) is
    the second line of defense here.
  * alias hop through a second statement — ``AGENTS_ROOT =
    os.path.join(os.environ['HOME'], 'agents'); SUB =
    os.path.join(AGENTS_ROOT, 'blackboard')``. The first statement flags;
    the second is invisible for the same reason as above.
  * '/'.join([...]) composition — the receiver is a separator string, not
    a home, so there is nothing to key on without dataflow.
  * %-format through a dict — ``'%(h)s/agents' % {'h': home}``.
  * string.Template.substitute — same reason.
  * absolute '/home/<user>/agents' literal — substantive, not an
    oversight: an absolute path is immune to the HOME swap, so it is not
    the hazard this guard exists for (sync_agent_core.sh spells it that
    way on purpose).
  * invisible rebinds that are not star imports — ``globals().update(d)``,
    ``exec(...)``, ``setattr(sys.modules[__name__], ...)``,
    ``vars()[...] = ...``. A star import IS poisoned (see
    ``_collect_bindings``); these are absent from scripts/*.py today, and
    if a cheap poison is ever added it must be added the same way — a
    flag, not a model.
  * bare ~/agents token in prose — pervasive in docstrings across
    scripts/ (259 occurrences), so it cannot discriminate a payload from
    documentation. Tilde spellings are caught where a call wrapper names
    them (``expanduser('~/agents')``) and in the one shell spelling that
    cannot be prose: an UNQUOTED tilde welded to an ``=`` or ``:``
    (``LOG=~/agents/logs``), which is an executable assignment. A quoted
    ``"~/agents"`` does not tilde-expand in shell, so it is not a home
    path and stays quiet; ``AGENTS_ROOT = ~/agents`` with spaces, and
    ``echo tier1 > ~/agents/rotation.disabled``, are prose and stay quiet.
  * executable tilde not welded to = or : — the consequence of the entry
    above, stated in its own right because it is the one that costs
    something: ``cd ~/agents`` and ``mkdir -p ~/agents/state`` in a .sh
    runner really do expand, and stay quiet. The welded form is the only
    tilde spelling that cannot also be prose, so widening past it
    re-floods the offender list with docstrings.
  * literal override text in a non-expanding context — the waiver reads
    TEXT, not expansion. Inside a QUOTED heredoc (``<<'PYEOF'``) or a
    Python string literal, ``${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}``
    never expands, yet it still waives the line. Seeing this needs
    cross-line heredoc state the line-based channel does not carry. It is
    bounded by LOUDNESS rather than by the scanner: the unexpanded text
    is a nonsense relative path that fails immediately, not the silent
    wrong-home write this guard exists to prevent.
  * non-literal agents segment — every composition branch requires the
    ``'agents'`` segment to be a literal, so ``seg = 'agents'; Path.home()
    / seg`` is quiet. Same alias reasoning as the entries above.
"""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import ast
import collections
import importlib
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
ENV_VAR = "OURLIBERTY_AGENTS_ROOT"
HOME_VAR = "HOME"

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
# sites; see the "alias-bound home" blind spot in the module docstring.)
ALLOWED_FILES = {"test_isolation_guard.py"}

# Per-SITE waivers: (filename, whitespace-normalized waiver key) -> reason.
# Finer-grained than ALLOWED_FILES: ONE OCCURRENCE of the named expression
# is exempt, everything else in the file stays in scope.
#
# AMBIGUITY-INTOLERANT, on purpose. A key that matches two or more
# candidates in the same file exempts NEITHER — every match is reported
# with an ambiguity reason. A line number in the key would be the obvious
# alternative and is worse: an unrelated edit above the site would silently
# invalidate the waiver and fire the guard on correct code, trading one
# silent failure for another. Arity fails closed in both directions.
#
# Waivers must also EARN themselves: a key that matches nothing in the tree
# is dead decoration and fails test_no_dead_waivers.
ALLOWED_SITES = {
    # (filename, exact whitespace-normalized waiver key) -> why it is
    # deliberately a REAL-home path. Empty today: the two known
    # intentional sites (test_regression_check.py's REAL_AGENTS,
    # test_isolation_wall.py's ro_targets) reach the real home through a
    # variable, which this scanner does not follow, so neither is visible
    # to it. If a refactor ever surfaces one, add it HERE with its reason
    # rather than to ALLOWED_FILES. You do not have to guess the key: every
    # offender message PRINTS its own paste-ready key.
}

# --------------------------------------------------------------------------
# TEXT CHANNEL. Built from shared fragments so the AST channel and the text
# channel cannot drift about what an agents path looks like — the exact
# asymmetry that let `Path.home() / 'agents/state'` be a false clean while
# `$HOME/agents/state` flagged.
# --------------------------------------------------------------------------

# A quoted path segment rooted at agents/, with any sub-path: 'agents',
# "agents/state", 'agents/blackboard/EMERGENCY_HALT'. Terminated at a
# segment boundary, so sibling trees ('agents-archive', "agents_old") do
# not match.
_TEXT_SEGMENT = r"['\"]agents(?:/[^'\"]*)?['\"]"

# Every spelling of "the process home" the text channel knows. The env-HOME
# spellings matter because $HOME is the thing the tier swap actually
# mutates; `(?<!\w)HOME` alone cannot see `os.environ["HOME"]`. The
# trailing `\)*` lets a wrapper's closing parens sit between the home and
# the `/` (`Path(os.environ['HOME']) / 'agents'`).
_TEXT_HOME = (
    r"(?:Path\.home\(\)"
    r"|(?<!\w)HOME"
    r"|os\.environ\[\s*['\"]HOME['\"]\s*\]"
    r"|os\.environ\.get\(\s*['\"]HOME['\"][^)]*\)"
    r"|os\.getenv\(\s*['\"]HOME['\"][^)]*\)"
    r"|expanduser\(\s*['\"]~['\"]\s*\)"
    r")\s*\)*\s*"
)

# Bare fallback spelled inside a string literal (a python -c payload, a
# shell command) or a raw shell script: neither can be parsed as Python, so
# match the text. A bare `~/agents` TOKEN is deliberately NOT matched — it
# is pervasive in docstring prose (259 hits across scripts/*.py), so it
# cannot discriminate a payload from documentation; tilde spellings are
# caught where a call wrapper names them (expanduser('~/agents')) and in
# the one shell spelling that cannot be prose: an UNQUOTED tilde sitting
# immediately after `=` or `:` (`LOG=~/agents/logs`, `PATH=$PATH:~/agents`),
# which is an executable assignment, not a sentence. Requiring the tilde to
# touch the `=` is what keeps prose out — `AGENTS_ROOT = ~/agents` in a
# docstring, and `echo tier1 > ~/agents/rotation.disabled`, both stay quiet
# (measured: zero `=~/agents` occurrences in the tree today, so this costs
# nothing live). A QUOTED "~/agents" is not a home path in shell at all —
# there is no tilde expansion inside quotes — so it must stay quiet too,
# and the lookbehind gives that for free. Every alternative is TERMINATED
# at a path-segment boundary so sibling directories ($HOME/agents-archive,
# expanduser('~/agents-old'), LOG=~/agents-archive) — different trees
# entirely — do not match.
TEXT_BARE = re.compile(
    _TEXT_HOME + r"/\s*" + _TEXT_SEGMENT
    + r"|expanduser\(\s*['\"]~/agents(?=['\"/])"
    # Path('~/agents').expanduser() — the wrapper the AST channel flags.
    + r"|['\"]~/agents(?=['\"/])[^)]*\)\s*\.\s*expanduser"
    + r"|\.\s*joinpath\(\s*" + _TEXT_SEGMENT
    # optional closing quote: shell-correct quoting ("$HOME"/agents,
    # "${HOME}"/agents) is the same path. The shell spellings already
    # accept a sub-path via the segment-boundary terminator.
    + r"|\$HOME['\"]?/agents(?![\w.-])|\$\{HOME\}['\"]?/agents(?![\w.-])"
    # the executable tilde: unquoted, welded to an `=` or `:`.
    + r"|(?<=[=:])~/agents(?![\w.-])"
)

# The ONLY in-string guard the text channel recognizes: the shell
# default-expansion, which binds the read and the fallback into one
# unambiguous construct — `"${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}"`.
# A read merely somewhere else on the line does NOT waive: a line can
# read the var for one purpose and use a bare path for another, and
# `OURLIBERTY_AGENTS_ROOT=$HOME/agents ... $OURLIBERTY_AGENTS_ROOT` even
# stomps the override to the bare path and reads it back.
SHELL_DEFAULT = re.compile(r"\$\{" + re.escape(ENV_VAR) + r":[-=]")


def _expansion_close(line, open_end):
    """Index of the `}` that closes the `${` opened just before `open_end`,
    or -1 if this line never closes it.

    Brace-MATCHED, not first-brace. `line.find("}", ...)` answers "is there a
    closing brace anywhere later", which is a different question: any
    unrelated expansion further along the line (a `${PATH}`, a `${HOME}`)
    supplies a brace that closes nothing, and the caller then hands out a
    waiver for an opener that was never terminated. That silently restored
    the exact fail-OPEN branch _inside_shell_default's docstring claims to
    have removed — a scanner-wide posture reversal delivered by a one-token
    difference.

    Only `${` opens a level. A bare `{` does not: it is brace expansion or
    an awk/jq body, and counting it would make legitimate lines unclosable.
    """
    depth = 1
    i, n = open_end, len(line)
    while i < n:
        if line.startswith("${", i):
            depth += 1
            i += 2
        elif line[i] == "}":
            depth -= 1
            if depth == 0:
                return i
            i += 1
        else:
            i += 1
    return -1


def _inside_shell_default(line, pos):
    """Is the match at `pos` inside a CLOSED `${OURLIBERTY_AGENTS_ROOT:-...}`
    expansion on THIS line?

    FAIL-CLOSED. The waiver applies only when an opener before `pos` has its
    OWN matching close later on the same line, at or after `pos`. A missing
    closing brace waives NOTHING — it used to waive the whole rest of the
    line, which is a fail-OPEN branch inside a scanner whose entire posture
    is fail-closed. The case is not hypothetical: f-string interpolations
    are replaced by an opaque placeholder before this function runs, so a
    brace supplied by an interpolated value is invisible here, and a
    `bash -n`-clean payload can carry a genuinely bare $HOME/agents.

    The loop CONTINUES past an unterminated opener rather than returning,
    so a line carrying a typo'd opener AND a well-formed one still gets the
    legitimate waiver.

    KNOWN AND DECLARED: this asks whether the TEXT is a well-formed
    expansion, not whether the shell will ever EXPAND it. See
    DECLARED_BLIND_SPOTS "literal override text in a non-expanding context".
    """
    for m in SHELL_DEFAULT.finditer(line):
        if m.start() > pos:
            break
        close = _expansion_close(line, m.end())
        if close != -1 and close > pos:
            return True
    return False


# ---------------------------------------------------------------------------
# BINDING SWEEP. One walk, one record() call per binding occurrence, so there
# is a single place to audit "what can rebind a name in this file".
# ---------------------------------------------------------------------------

_ModuleImport = collections.namedtuple("_ModuleImport", "module")
_FromImport = collections.namedtuple("_FromImport", "module original")

# Match-statement capture nodes (PEP 634) exist only on py>=3.10, but the guard
# has to survive the interpreter it is INVOKED on, not the one the suite pins.
# `/usr/bin/python3` is 3.9 on this Mac, and a bare `ast.MatchAs` lookup there
# raises AttributeError out of the binding sweep — which makes the guard report
# ITSELF as broken across ~124 of its own tests. That is indistinguishable from
# a real regression in the thing being guarded, and it is the worst failure
# shape this file can have: it converts "your paths are wrong" into noise.
#
# Resolved once, defensively, instead of at every isinstance() call. Nothing is
# lost on 3.9: a `match` statement is a SyntaxError there, so these nodes can
# never appear in a tree that parsed. Compare the PEP 695 skipIf below — this
# file already degrades by version rather than crashing.
_MATCH_CAPTURE = tuple(
    node for node in (getattr(ast, name, None)
                      for name in ("MatchAs", "MatchStar"))
    if node is not None
)
_MATCH_MAPPING = getattr(ast, "MatchMapping", None)


def _collect_bindings(tree):
    """(bindings, star_import).

    bindings: name -> list of bound values, ONE entry per binding occurrence
    anywhere in the file, in any scope. `None` means the value cannot be
    inspected (a parameter, loop target, `del`, `+=`, a nested def of that
    name, a `global` declaration, ...) — always disqualifying. Imports
    record a `_ModuleImport`/`_FromImport` marker instead of None: also
    disqualifying for the override-read rule, but it lets the blessing side
    prove `os` really is the `os` module and `environ` really is
    `os.environ` (see `_is_unrebound_module`, `_environ_names`).

    star_import: True if the file contains `from X import *`. That is the
    ONE binding form which can make a name look MORE trusted than it is —
    it rebinds names this sweep never sees, and it emits no Name-in-Store
    node for the generic sweep to catch. So it POISONS every name-trust set
    in the file rather than being modelled. (This is the counterexample to
    the older claim that a missed binding form can only make a name look
    LESS bound.) Other invisible rebinds — globals().update, exec, setattr
    on the module — are declared blind spots; if a poison is ever added for
    them, add it the same way: a flag, not a model.

    Scope-free by design: no LEGB chain, no nested-scope bookkeeping, no
    parameter special-cases. Reusing a name for a second purpose anywhere
    in the file costs that name its trust, and the offender message says
    so.
    """
    bindings = {}
    star_import = False
    parents = _parent_map(tree)

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
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    record(alias.asname, _ModuleImport(alias.name))
                else:
                    top = alias.name.split(".")[0]
                    record(top, _ModuleImport(top))
        elif isinstance(node, ast.ImportFrom):
            module = None if node.level else node.module
            for alias in node.names:
                if alias.name == "*":
                    star_import = True
                    continue
                record(
                    alias.asname or alias.name,
                    _FromImport(module, alias.name),
                )
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                record(name, None)
        elif _MATCH_CAPTURE and isinstance(node, _MATCH_CAPTURE) and node.name:
            record(node.name, None)
        elif (_MATCH_MAPPING is not None
                and isinstance(node, _MATCH_MAPPING) and node.rest):
            record(node.rest, None)
    return bindings, star_import


def _parent_map(tree):
    parents = {}
    for node in ast.walk(tree):
        for kid in ast.iter_child_nodes(node):
            parents[kid] = node
    return parents


# ---------------------------------------------------------------------------
# RECEIVER PROVENANCE — shared by `_is_environ` and `_unwrap_strict`. Both
# are the same mistake waiting to happen ("this thing was trusted because of
# how its name was spelled"), so both go through ONE helper. Written twice,
# they drift, and the drift is invisible because each has its own tests.
# ---------------------------------------------------------------------------

_STDLIB_WRAPPER_MODULES = {"pathlib", "os", "os.path", "posixpath", "ntpath"}


def _is_unrebound_module(name, modname, bindings):
    """Is `name` provably the module `modname`? True when the file never
    binds `name` at all (nothing in this file rebound it) or binds it only
    by `import <modname>`. `os = shim` or `import myos as os` -> False."""
    values = bindings.get(name)
    if not values:
        return True
    return all(
        isinstance(v, _ModuleImport) and v.module == modname for v in values
    )


def _is_unrebound_stdlib_name(name, bindings):
    """Is the bare name `name` provably still the stdlib thing? True when
    the file never binds it, or binds it only via `from <stdlib> import
    <name>`. A local `def str(...)` or `Path = shim` -> False."""
    values = bindings.get(name)
    if not values:
        return True
    return all(
        isinstance(v, _FromImport)
        and v.module in _STDLIB_WRAPPER_MODULES
        and v.original == name
        for v in values
    )


class _Ctx:
    """Per-file analysis context. The construction ORDER is load-bearing:
    bindings -> star-import poison -> os provenance -> environ names ->
    trusted names. Any other order computes a name-trust set against an
    incomplete picture."""

    def __init__(self, tree):
        self.tree = tree
        self.parents = _parent_map(tree)
        self.bindings, self.star_import = _collect_bindings(tree)
        self.os_trusted = _is_unrebound_module("os", "os", self.bindings)
        # POISON: a star import can rebind `environ` exactly as easily as
        # `root`, so BOTH name-trust sets are cleared, not just the obvious
        # one. Any future name-trust set must be registered here too —
        # which is why they are all computed in this one place.
        if self.star_import:
            self.environ_names = frozenset()
            self.trusted = frozenset()
            self.wrapped_only = frozenset()
            return
        self.environ_names = _environ_names(self.bindings, self)
        self.trusted, self.wrapped_only = _classify_names(self.bindings, self)


def _binds_live_environ(value, ctx):
    """Is this binding provably the LIVE os.environ mapping?"""
    if isinstance(value, _FromImport):
        return value.module == "os" and value.original == "environ"
    return isinstance(value, ast.AST) and _is_os_environ_attribute(value, ctx)


def _is_os_environ_attribute(node, ctx):
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and ctx.os_trusted
    )


def _environ_names(bindings, ctx):
    """Names provably bound to the live env mapping — EVERY binding of the
    name must be `os.environ` or `from os import environ`. Trusting the
    spelling `environ` alone is a false clean: `environ = os.environ.copy()`
    (or a caller-supplied `env` dict, the live shape at
    build_sequence_validator.py:831) is a SNAPSHOT, and a snapshot taken
    before agent_runner pins the override reads None and takes the bare
    fallback under the tier-2 home."""
    return frozenset(
        name
        for name, values in bindings.items()
        if values and all(_binds_live_environ(v, ctx) for v in values)
    )


def _is_environ(node, ctx):
    """The live env mapping: a name that EARNED it, or `os.environ` where
    `os` is provably the module."""
    if isinstance(node, ast.Name):
        return node.id in ctx.environ_names
    return _is_os_environ_attribute(node, ctx)


def _is_env_read(node, ctx, var=ENV_VAR):
    """A genuine read of `var`: os.environ.get(var, ...), environ.get(...),
    os.getenv(var, ...), getenv(...), and the subscript forms
    os.environ[var] / environ[var]."""
    if isinstance(node, ast.Subscript):
        key = node.slice
        return (
            _is_environ(node.value, ctx)
            and isinstance(key, ast.Constant)
            and key.value == var
        )
    if not (isinstance(node, ast.Call) and node.args):
        return False
    first = node.args[0]
    if not (isinstance(first, ast.Constant) and first.value == var):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "getenv" and _is_unrebound_stdlib_name(
            "getenv", ctx.bindings
        )
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "getenv":
        return (
            isinstance(func.value, ast.Name)
            and func.value.id == "os"
            and ctx.os_trusted
        )
    if func.attr == "get":
        return _is_environ(func.value, ctx)
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
#   wrapped in a STDLIB Path()/str()/os.fspath()). One binding from
#   anything else — another env var, a parameter, a loop target, a rebind,
#   a nested def of the same name — and the name is not trusted, full stop.
#
#   A GUARD EXPRESSION blesses only if it IS a read of the override or IS
#   a bare trusted name. NO unwrapping here: `Path(root)` as a truthiness
#   test is ALWAYS true, so unwrapping it would turn a dead fallback into a
#   certified one. Not "contains one somewhere" either: `cfg.get(root)` is
#   not a guard, and neither is `cfg.str(root)`.
# ---------------------------------------------------------------------------

# Calls that pass their argument through unchanged for our purposes, so
# `root = Path(os.environ.get(ENV_VAR))` still reads as the env read.
_TRIVIAL_WRAPPERS = {"Path", "PurePath", "PosixPath", "str", "fspath"}


def _unwrap_loose(node):
    """DETECTION-side unwrap: strip ANY single-argument call. Loose here can
    only produce MORE flags (`cfg.Path(os.environ['HOME']) / 'agents'` must
    still be seen), which is the safe direction.

    Used whole by `_is_tilde_home_const`; `_is_home_base` inlines the same
    strip ONE LEVEL AT A TIME, because a blanket strip there would consume
    the env read itself (`os.environ.get('HOME')` is a single-argument
    call) and leave the bare constant 'HOME'."""
    while (
        isinstance(node, ast.Call)
        and len(node.args) == 1
        and not node.keywords
    ):
        node = node.args[0]
    return node


def _unwrap_strict(node, ctx):
    """BLESSING-side unwrap: strip only calls that are PROVABLY the stdlib
    wrapper — a bare name in `_TRIVIAL_WRAPPERS` that this file has not
    rebound, or an attribute of an un-rebound `os`/`pathlib`. `cfg.str(x)`
    and `helpers.Path(x)` are therefore opaque; keying on the method name
    alone was how an arbitrary call laundered a value past the trust rule
    just by picking one of five names."""
    while (
        isinstance(node, ast.Call)
        and len(node.args) == 1
        and not node.keywords
    ):
        func = node.func
        if isinstance(func, ast.Name):
            if func.id not in _TRIVIAL_WRAPPERS:
                break
            if not _is_unrebound_stdlib_name(func.id, ctx.bindings):
                break
        elif isinstance(func, ast.Attribute):
            if func.attr not in _TRIVIAL_WRAPPERS:
                break
            recv = func.value
            if not (
                isinstance(recv, ast.Name)
                and recv.id in ("os", "pathlib")
                and _is_unrebound_module(recv.id, recv.id, ctx.bindings)
            ):
                break
        else:
            break
        node = node.args[0]
    return node


def _binds_from_env_read(value, ctx):
    """Is this binding's value, syntactically, the override read?"""
    return (
        isinstance(value, ast.AST)
        and _is_env_read(_unwrap_strict(value, ctx), ctx)
    )


def _binding_wraps_the_read(value, ctx):
    """Is this binding a read that had to be UNWRAPPED to be recognized —
    `root = Path(os.environ.get(VAR))`? Such a name holds the override, but
    it is useless as a truthiness guard: `Path('')` is `Path('.')` and
    `str(None)` is `'None'`, both TRUTHY, so the fallback the guard
    certifies can never run."""
    return (
        isinstance(value, ast.AST)
        and not _is_env_read(value, ctx)
        and _is_env_read(_unwrap_strict(value, ctx), ctx)
    )


def _classify_names(bindings, ctx):
    """(trusted, wrapped_only). `trusted` = every binding is the read.
    `wrapped_only` = trusted, but at least one binding wraps the read, so
    the name is always truthy and cannot serve as a guard."""
    trusted, wrapped = set(), set()
    for name, values in bindings.items():
        if not values:
            continue
        if all(_binds_from_env_read(v, ctx) for v in values):
            trusted.add(name)
            if any(_binding_wraps_the_read(v, ctx) for v in values):
                wrapped.add(name)
    return frozenset(trusted), frozenset(wrapped)


def _is_guard(expr, ctx):
    """Does this expression ITSELF read the override? It must BE the read
    or BE a bare trusted name that is not always-truthy. No unwrapping: the
    rule is 'wrappers are opaque on the BINDING when the name is later used
    as a truthiness test', and `Path(root) if root else ...` (the live
    idiom — wrapper in the true-branch, bare name in the test) must stay
    quiet while `root = Path(read); root if root else ...` must not."""
    if _is_env_read(expr, ctx):
        return True
    return (
        isinstance(expr, ast.Name)
        and expr.id in ctx.trusted
        and expr.id not in ctx.wrapped_only
    )


# ---------------------------------------------------------------------------
# DETECTION — the composition table. Enumerated rather than hard-coded per
# operator, and every branch calls the SAME `_is_home_base` and
# `_is_agents_segment`, so a widening cannot land on one operator only.
# ---------------------------------------------------------------------------

_HOME_METHOD_CHAIN = {"resolve", "absolute", "expanduser"}
_PATH_CTORS = {"Path", "PurePath", "PosixPath", "PurePosixPath"}
_JOIN_RECEIVERS = {"posixpath", "ntpath", "path", "os"}
_MOD_AGENTS_RE = re.compile(r"%s/agents(?:/.*)?\Z", re.S)
_FORMAT_AGENTS_RE = re.compile(r"\{[^}]*\}/agents(?:/.*)?\Z", re.S)
_SLASHED_AGENTS_RE = re.compile(r"/agents(?:/.*)?\Z", re.S)


def _is_agents_segment(value, prefix="agents"):
    """The ONE segment rule, shared by every composition branch and mirrored
    by `_TEXT_SEGMENT`. True for 'agents' and any sub-path under it; False
    for sibling trees ('agents-archive', 'agents_old', 'agentsx') and for a
    leading slash ('/agents' is an absolute-path RESET in pathlib, not a
    home-relative fallback)."""
    return isinstance(value, str) and (
        value == prefix or value.startswith(prefix + "/")
    )


def _is_agents_segment_const(node, prefix="agents"):
    return isinstance(node, ast.Constant) and _is_agents_segment(
        node.value, prefix
    )


def _is_tilde_home_const(node):
    """`'~'`, or any single-arg wrapper around it (`Path('~')`)."""
    node = _unwrap_loose(node)
    return isinstance(node, ast.Constant) and node.value == "~"


def _callee_name(func):
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_home_base(node, ctx):
    """An expression resolving to the process home.

    Recognized: `Path.home()` / `pathlib.Path.home()`; a home-method chain
    (`.resolve()`, `.absolute()`, `.expanduser()`) over one of those; an
    INLINE read of $HOME in every form `_is_env_read` models
    (`os.environ['HOME']`, `os.environ.get('HOME', ...)`, `os.getenv(...)`);
    `os.path.expanduser('~')` and `Path('~').expanduser()`; the bare name
    `HOME` and any attribute `.HOME`.

    Two deliberate calls, both explained by the directional-bias principle:

      * `HOME` / `.HOME` are trusted BY SPELLING and that is fine — a
        wrongly-trusted name here only over-flags. It is EXACT, not a
        suffix match: `active_tier.TIER1_HOME` is the real account home,
        and agent_runner.py:1507 spells the override pin itself as
        `Path(active_tier.TIER1_HOME) / 'agents'` — a suffix match would
        flag the fix.
      * unwrapping is LOOSE, so `cfg.Path(os.environ['HOME'])` is still
        seen. Strict unwrapping belongs on the blessing side only.

    NOT recognized: an ALIASED home (`home = os.environ.get('HOME','')`).
    Declared blind spot; see the module docstring.
    """
    # Unwrap ONE level at a time, testing at each level. A blanket
    # `_unwrap_loose` first would strip the env read itself
    # (`os.environ.get('HOME')` is a single-argument call), turning the
    # very spelling this recognizes into the constant 'HOME'.
    depth = 0
    while True:
        if _is_home_base_direct(node, ctx):
            return True
        if (
            depth < 8
            and isinstance(node, ast.Call)
            and len(node.args) == 1
            and not node.keywords
        ):
            node, depth = node.args[0], depth + 1
            continue
        return False


def _is_home_base_direct(node, ctx):
    """`_is_home_base` without the unwrap loop — the shape checks only."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        f = node.func
        if f.attr == "home" and (
            (isinstance(f.value, ast.Name) and f.value.id == "Path")
            or (isinstance(f.value, ast.Attribute) and f.value.attr == "Path")
        ):
            return True
        if f.attr in _HOME_METHOD_CHAIN and _is_home_base(f.value, ctx):
            return True
        if f.attr == "expanduser" and _is_tilde_home_const(f.value):
            return True  # Path('~').expanduser()
    if isinstance(node, ast.Call) and _callee_name(node.func) == "expanduser":
        if node.args and _is_tilde_home_const(node.args[0]):
            return True  # os.path.expanduser('~')
    if _is_env_read(node, ctx, var=HOME_VAR):
        return True
    if isinstance(node, ast.Name):
        return node.id == "HOME"
    return isinstance(node, ast.Attribute) and node.attr == "HOME"


def _is_agents_fallback(node, ctx):
    """True for the enumerated compositions of <home> and an agents
    segment. The table (each branch sharing `_is_home_base` and
    `_is_agents_segment`):

      1. H / 'agents...'                      (Div)
      2. H.joinpath('agents...', ...)
      3. os.path.join(H, 'agents...', ...)
      4. Path(H, 'agents...', ...)            (multi-arg constructor)
      5. <str-ish H> + '/agents...'           (Add, home FIRST only)
      6. '%s/agents...' % H                   (Mod, home on the right)
      7. '{}/agents...'.format(H)
      8. f'{H}/agents...'                     (JoinedStr)
      9. expanduser('~/agents...'), Path('~/agents...').expanduser()

    Only home-FIRST orderings compose: `'/agents' + str(H)` and
    `os.path.join('agents', H)` do not produce a home-rooted path and must
    stay quiet. What is NOT in this table is in DECLARED_BLIND_SPOTS with
    its reason — that list, not this docstring, is the honest inventory.
    """
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Div):
            return _is_agents_segment_const(node.right) and _is_home_base(
                node.left, ctx
            )
        if isinstance(node.op, ast.Add):
            return (
                isinstance(node.right, ast.Constant)
                and isinstance(node.right.value, str)
                and _SLASHED_AGENTS_RE.fullmatch(node.right.value) is not None
                and _is_home_base(node.left, ctx)
            )
        if isinstance(node.op, ast.Mod):
            left = node.left
            if not (
                isinstance(left, ast.Constant)
                and isinstance(left.value, str)
                and _MOD_AGENTS_RE.fullmatch(left.value) is not None
            ):
                return False
            right = node.right
            if isinstance(right, ast.Tuple):
                return bool(right.elts) and _is_home_base(right.elts[0], ctx)
            return _is_home_base(right, ctx)
        return False

    if isinstance(node, ast.JoinedStr):
        values = node.values
        for i, part in enumerate(values[:-1]):
            nxt = values[i + 1]
            if (
                isinstance(part, ast.FormattedValue)
                and isinstance(nxt, ast.Constant)
                and isinstance(nxt.value, str)
                and _SLASHED_AGENTS_RE.match(nxt.value) is not None
                and _is_home_base(part.value, ctx)
            ):
                return True
        return False

    if not isinstance(node, ast.Call):
        return False
    func = node.func
    fname = _callee_name(func)

    if fname == "joinpath" and isinstance(func, ast.Attribute) and node.args:
        return _is_agents_segment_const(node.args[0]) and _is_home_base(
            func.value, ctx
        )

    if fname == "join" and isinstance(func, ast.Attribute) and len(node.args) >= 2:
        recv = func.value
        ok_recv = (
            isinstance(recv, ast.Attribute) and recv.attr == "path"
        ) or (isinstance(recv, ast.Name) and recv.id in _JOIN_RECEIVERS)
        return (
            ok_recv
            and _is_home_base(node.args[0], ctx)
            and _is_agents_segment_const(node.args[1])
        )

    if fname in _PATH_CTORS and len(node.args) >= 2:
        return _is_home_base(node.args[0], ctx) and _is_agents_segment_const(
            node.args[1]
        )

    if fname == "format" and isinstance(func, ast.Attribute) and node.args:
        tmpl = func.value
        return (
            isinstance(tmpl, ast.Constant)
            and isinstance(tmpl.value, str)
            and _FORMAT_AGENTS_RE.fullmatch(tmpl.value) is not None
            and _is_home_base(node.args[0], ctx)
        )

    if fname == "expanduser":
        if node.args and _is_agents_segment_const(node.args[0], "~/agents"):
            return True
        if isinstance(func, ast.Attribute):
            v = func.value  # Path('~/agents').expanduser()
            return (
                isinstance(v, ast.Call)
                and bool(v.args)
                and _is_agents_segment_const(v.args[0], "~/agents")
            )
    return False


def _guard_verdict(node, ctx):
    """(guarded, offending_name). Climb ancestors: is this fallback
    structurally subordinate to a guard? offending_name names the guard
    expression's variable, for the offender message."""
    parents = ctx.parents
    child = node
    parent = parents.get(child)
    failed_name = None
    while parent is not None:
        # Inside an env read the fallback can only be the default —
        # positional (args[1:]) or keyword — since args[0] is the var name.
        # This path consults no name set, so the star-import poison rightly
        # does not reach it: an inline default has no name to lie about,
        # which also makes it the exit a poisoned file should take.
        if _is_env_read(parent, ctx):
            return True, None
        candidates = ()
        if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.Or):
            candidates = parent.values[:parent.values.index(child)]
        elif isinstance(parent, ast.IfExp) and child is parent.orelse:
            candidates = (parent.test,)
        for cand in candidates:
            if _is_guard(cand, ctx):
                return True, None
            # Messaging only — loose enough to NAME the variable a
            # developer wrote, strict enough not to bless it.
            bare = _unwrap_strict(cand, ctx)
            if isinstance(bare, ast.Name) and failed_name is None:
                failed_name = bare.id
        # Plain nesting (Call args like Path(...)/str(...), BinOps that
        # append segments, parens) keeps climbing.
        child, parent = parent, parents.get(parent)
    return False, failed_name


def _failure_reason(failed_name, ctx):
    if failed_name is None:
        return "not inside any override-guarded expression"
    if ctx.star_import:
        return (
            f"guarded by {failed_name!r}, but this file contains a star"
            " import, so no name in it can be proven bound to the override;"
            " fold the read into the expression (remediation 1)"
        )
    if failed_name in ctx.wrapped_only:
        return (
            f"guard {failed_name!r} is always truthy (its binding wraps the"
            " read in Path()/str()), so this fallback is dead"
        )
    if failed_name in ctx.bindings:
        return (
            f"guarded by {failed_name!r}, which is NOT trusted: at least"
            f" one binding of {failed_name!r} in this file is something"
            f" other than a read of {ENV_VAR}"
        )
    return (
        f"guarded by {failed_name!r}, which this file never binds from"
        f" {ENV_VAR}"
    )


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
    variable or the read -- `cfg.get(root) or <fallback>` does not count,
    and neither does a variable whose BINDING wraps the read
    (`root = Path(os.environ.get(...))` is always truthy, so the fallback
    behind it is dead code).

 3. It is inside a string (a `python3 -c` payload, a shell command) or in
    a scripts/*.sh runner. The text channel does not parse code; spell the
    guard the shell way, which the scanner recognizes:
        "${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}"
    Keep the closing brace ON THE SAME LINE -- an unterminated expansion
    waives nothing, on purpose. `LOG=~/agents/logs` is the tilde spelling
    of the same bare path (unquoted, welded to the `=`, so the shell
    really does expand it) and takes the same fix.

 4. The bare REAL-home path is deliberate (the test jail's RO-bind
    targets and tripwire must point at the real tree, never the sandbox).
    Add a per-site waiver with a reason -- paste the waiver key printed
    with the offender above; you do not have to guess it:
        ALLOWED_SITES[('<file>', '<printed key>')] = "<why>"
    A waiver covers exactly ONE occurrence: if the same expression appears
    twice in the file, the waiver is refused and both are reported (rename
    or rewrite one of them). A waiver that matches nothing also fails.
    Whole-file ALLOWED_FILES entries are a last resort: they switch this
    check off for everything in that file.

This guard deliberately over-flags: a noisy false alarm costs you this
message, while a missed one silently writes tier-2 state into the wrong
home. Do NOT widen the trust rule to silence a single site -- use 1 or 4.
"""

_TEXT_REASON = (
    "bare fallback %s; the only guard recognized here is"
    ' "${%s:-...}", closed on the same line'
)

# ---------------------------------------------------------------------------
# TWO-PASS SCAN. Pass 1 collects candidates from ALL channels with an
# explicit, paste-ready waiver key. Pass 2 applies waivers uniformly. They
# are separated so the text channel cannot end up with a different waiver
# mechanism from the AST channel — which is how one channel got a per-site
# exit and the other got a remediation instruction that silently no-op'd.
# ---------------------------------------------------------------------------

_Candidate = collections.namedtuple(
    "_Candidate", "filename lineno key display reason"
)


def _normalize(text):
    return " ".join(text.split())


def _text_candidates(text, filename, kind, lineno=None):
    """Per-line TEXT_BARE scan — the ONE text matcher, used for string
    constants inside .py files and for the raw text of .sh files, so the
    same payload cannot get different verdicts depending on which file type
    it lives in.

    `lineno` pins every candidate to the enclosing constant's line (the .py
    channel); None uses the text's own line numbers (the .sh channel)."""
    out = []
    if "agents" not in text:  # cheap pre-filter; every spelling has it
        return out
    for i, line in enumerate(text.splitlines(), 1):
        for match in TEXT_BARE.finditer(line):
            if _inside_shell_default(line, match.start()):
                continue
            norm = _normalize(line)
            out.append(
                _Candidate(
                    filename=filename,
                    lineno=lineno if lineno is not None else i,
                    # KEY is the complete normalized line; DISPLAY is the
                    # truncated one. Keeping them distinct is the point: a
                    # key derived from the display string would be
                    # unguessable for any line over 80 chars.
                    key=norm,
                    display=f"[{kind}] {norm[:80]}",
                    reason=_TEXT_REASON % (kind, ENV_VAR),
                )
            )
            break
    return out


def collect_candidates(source, filename="<source>"):
    """Pass 1 for a Python source file: AST channel + text channel."""
    tree = ast.parse(source, filename=filename)
    ctx = _Ctx(tree)
    out = []
    for node in ast.walk(tree):
        if _is_agents_fallback(node, ctx):
            segment = _normalize(ast.get_source_segment(source, node) or "")
            guarded, failed_name = _guard_verdict(node, ctx)
            if not guarded:
                out.append(
                    _Candidate(
                        filename=filename,
                        lineno=node.lineno,
                        key=segment,
                        display=segment,
                        reason=_failure_reason(failed_name, ctx),
                    )
                )
        if isinstance(node, ast.JoinedStr):
            # An f-string is ONE string at runtime but many Constant
            # fragments in the AST. The COMPOSITION check above runs on the
            # structured node first (an interpolated home is invisible once
            # flattened); then join the fragments (interpolations become an
            # opaque placeholder, so fragments cannot create false
            # adjacency) and text-scan the result.
            out.extend(
                _text_candidates(
                    "".join(
                        v.value
                        if isinstance(v, ast.Constant)
                        and isinstance(v.value, str)
                        else "\x00"
                        for v in node.values
                    ),
                    filename,
                    "in string",
                    lineno=node.lineno,
                )
            )
        elif isinstance(node, ast.Constant) and not isinstance(
            ctx.parents.get(node), ast.JoinedStr
        ):
            if isinstance(node.value, str):
                out.extend(
                    _text_candidates(
                        node.value, filename, "in string", lineno=node.lineno
                    )
                )
            elif isinstance(node.value, bytes):
                # bytes commands are legal for shell=True subprocesses.
                out.extend(
                    _text_candidates(
                        node.value.decode("latin-1"),
                        filename,
                        "in bytes",
                        lineno=node.lineno,
                    )
                )
    return out


def collect_candidates_text(text, filename="<text>", kind="in shell"):
    """Pass 1 for a file that is not Python (the scripts/*.sh runners)."""
    return _text_candidates(text, filename, kind)


def _waiver_key_hint(candidate):
    return (
        f"waiver key: ALLOWED_SITES[({candidate.filename!r},"
        f" {candidate.key!r})] = \"<why>\""
    )


def apply_waivers(candidates, filename):
    """Pass 2. A waiver exempts a candidate only when its key matches
    EXACTLY ONE candidate in the file. Two or more matches exempt NONE of
    them: an ambiguous waiver silently covering a textual twin is the
    failure this rule exists to prevent, and refusing is the fail-closed
    answer in both directions (waiver-then-twin and twin-then-waiver)."""
    counts = collections.Counter(c.key for c in candidates)
    offenders = []
    for c in candidates:
        if (filename, c.key) in ALLOWED_SITES:
            if counts[c.key] == 1:
                continue
            reason = (
                "a per-site waiver covers exactly one occurrence; this file"
                f" has {counts[c.key]} textually identical occurrences of"
                " this expression -- disambiguate by renaming or rewriting"
                " one of them, or use ALLOWED_FILES"
            )
        else:
            reason = c.reason
        offenders.append(
            f"{c.filename}:{c.lineno}: {c.display} -- {reason}"
            f" | {_waiver_key_hint(c)}"
        )
    return offenders


def find_bare_agents_roots(source, filename="<source>"):
    """Return ['file:line: segment -- reason | waiver key: ...', ...] for
    every unguarded fallback in Python source. Each entry says WHY it fired
    and prints the exact key that would waive it; _REMEDIATION says what to
    do."""
    return apply_waivers(collect_candidates(source, filename), filename)


def find_bare_agents_roots_in_text(text, filename="<text>", kind="in shell"):
    """Same contract, for a file this scanner must NOT parse as Python —
    the shell runners. Same matcher, same waiver mechanism, same messages."""
    return apply_waivers(
        collect_candidates_text(text, filename, kind), filename
    )


# ---------------------------------------------------------------------------
# DECLARED BLIND SPOTS. The honest inventory: label -> (fixture, reason).
# Every label appears verbatim in the module docstring's "WHAT THIS CANNOT
# SEE" section, and every fixture is asserted quiet, by
# `test_declared_blind_spots_are_real_and_disclosed`. Adding a blind spot
# without disclosing it, or disclosing one the code actually catches, fails.
# ---------------------------------------------------------------------------

DECLARED_BLIND_SPOTS = (
    (
        "scope-free trust",
        "def f():\n"
        "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
        "    return root\n"
        "p = Path(root) if root else Path.home() / 'agents'\n",
        "NameError before it can resolve a path; buying it back means the"
        " scope model that produced three rounds of bypasses.",
    ),
    (
        "alias-bound home",
        "home = os.environ.get('HOME', '')\n"
        "p = Path(home) / 'agents'\n",
        "Alias tracking would flag the test jail's intentional real-home"
        " RO-bind targets.",
    ),
    (
        "alias hop through a second statement",
        "AGENTS_ROOT = os.path.join(repo_dir, 'agents')\n"
        "SUB = os.path.join(AGENTS_ROOT, 'blackboard')\n",
        "Same alias reason; the home-rooted FIRST statement does flag.",
    ),
    (
        "'/'.join([...]) composition",
        "p = '/'.join([str(Path.home()), 'agents'])\n",
        "The receiver is a separator, not a home.",
    ),
    (
        "%-format through a dict",
        "p = '%(h)s/agents' % {'h': Path.home()}\n",
        "The home is a dict value, not a positional operand.",
    ),
    (
        "string.Template.substitute",
        "p = Template('$h/agents').substitute(h=Path.home())\n",
        "Same reason as the dict %-format.",
    ),
    (
        "absolute '/home/<user>/agents' literal",
        "p = Path('/home/larry/agents')\n",
        "An absolute path is immune to the HOME swap, so it is not the"
        " hazard this guard exists for.",
    ),
    (
        "invisible rebinds that are not star imports",
        "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
        "globals().update(overrides)\n"
        "p = Path(root) if root else Path.home() / 'agents'\n",
        "Absent from scripts/*.py today; a poison would be a flag, not a"
        " model.",
    ),
    (
        "bare ~/agents token in prose",
        '"""State lives under ~/agents (see ~/agents/logs/).\n\n'
        "Rollback lever: echo tier1 > ~/agents/rotation.disabled\n"
        'The default is AGENTS_ROOT = ~/agents when nothing is pinned.\n'
        '"""\n'
        "x = 1\n",
        "Pervasive in docstrings; cannot discriminate payload from prose."
        " The executable spelling (unquoted `=~/agents`) IS caught.",
    ),
    (
        "executable tilde not welded to = or :",
        "cmd = 'cd ~/agents && ls'\n",
        "The .sh channel inherits the tilde-is-prose compromise above, so"
        " `cd ~/agents` and `mkdir -p ~/agents/state` are quiet even though"
        " the shell really does expand them. Narrowing to the welded form"
        " (`LOG=~/agents`) is what keeps the docstring/prose fixtures out of"
        " the offender list; widening to every `~/agents` re-floods it."
        " Bounded in practice: these spell a path but rarely WRITE tier-2"
        " state, and the `${...}` form is what review asks for.",
    ),
    (
        "literal override text in a non-expanding context",
        "payload = '''python3 - <<'PYEOF'\n"
        "p = \"${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}/state\"\nPYEOF'''\n",
        "_inside_shell_default asks whether the TEXT is a well-formed"
        " expansion, not whether the shell will EXPAND it — inside a QUOTED"
        " heredoc or a plain Python string literal nothing expands, so the"
        " spelling remediation #3 tells you to paste waives the check while"
        " doing nothing at runtime. Fixing it needs cross-line heredoc state"
        " the line-based text channel does not carry. Bounded by loudness:"
        " the unexpanded text is a nonsense relative path that fails at"
        " once, NOT the silent wrong-home write this guard exists for."
        " kick_govern_loop_assessor.sh shows the correct pattern — resolve"
        " in the shell, export, and read the env inside the payload.",
    ),
    (
        "non-literal agents segment",
        "seg = 'agents'\np = Path.home() / seg\n",
        "Every composition branch requires the agents segment to be a"
        " literal ast.Constant, so a variable, f-string or concatenation"
        " producing it is quiet. Same alias reason as the entries above:"
        " tracking it means the value model that produced three rounds of"
        " bypasses. Absent from scripts/*.py today.",
    ),
)


# ---------------------------------------------------------------------------
# THE CROSS-PRODUCT. Built ONCE and consumed by the home-spelling, segment
# and composition property tests together: each of those three widenings is
# an AXIS of the same predicate, and per-axis fixtures would each pass while
# the COMBINATION (os.path.join(os.environ['HOME'], 'agents/state')) stayed
# quiet.
# ---------------------------------------------------------------------------

HOME_SPELLINGS = (
    "Path.home()",
    "pathlib.Path.home()",
    "Path.home().resolve()",
    "HOME",
    "cfg.HOME",
    'Path(os.environ["HOME"])',
    'Path(os.environ.get("HOME"))',
    'Path(os.environ.get("HOME", "/home/larry"))',
    'Path(os.path.expanduser("~"))',
    'Path("~").expanduser()',
)

COMPOSITIONS = (
    ("div", lambda h, s: '%s / "%s"' % (h, s)),
    ("joinpath", lambda h, s: '%s.joinpath("%s")' % (h, s)),
    ("os.path.join", lambda h, s: 'os.path.join(%s, "%s")' % (h, s)),
    ("Path(h, s)", lambda h, s: 'Path(%s, "%s")' % (h, s)),
    ("f-string", lambda h, s: "f'{%s}/%s'" % (h, s)),
    ("concat", lambda h, s: 'str(%s) + "/%s"' % (h, s)),
    ("percent", lambda h, s: '"%%s/%s" %% %s' % (s, h)),
    (".format", lambda h, s: '"{}/%s".format(%s)' % (s, h)),
)

SEGMENTS = (
    "agents",
    "agents/state",
    "agents/state/dispatch.lease",
    "agents/logs",
    "agents/blackboard/EMERGENCY_HALT",
)

# Sibling trees and non-home-rooted shapes: widening the segment rule to
# sub-paths must not have swallowed these.
NEGATIVE_SEGMENTS = (
    "agents-archive",
    "agents_old",
    "agentsx",
    "agent",
    "/agents",
    "agent-core/agents",
)

# (home, composition, segment) triples the table deliberately does not
# cover. Empty today; the property test consults it so that a gap is
# DOCUMENTED rather than silently absent from the assertion.
UNCOVERED_COMBINATIONS = frozenset()


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
        # The shell runners are production entry points that build the same
        # state paths. They cannot be parsed as Python, so they go through
        # the text channel directly.
        for sh in sorted(SCRIPTS.glob("*.sh")):
            if sh.name in ALLOWED_FILES:
                continue
            offenders.extend(
                find_bare_agents_roots_in_text(
                    sh.read_text(encoding="utf-8"), filename=sh.name
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

    def test_shell_runners_are_actually_in_scope(self):
        """A glob that silently matches nothing is the false-clean twin of
        the bug that let the .sh runners go unscanned for three rounds. Pin
        the COUNT, so moving the runners into a subdirectory fails loudly
        instead of quietly scanning zero files."""
        shells = sorted(SCRIPTS.glob("*.sh"))
        self.assertGreaterEqual(
            len(shells),
            15,
            "the shell runners must stay directly under scripts/, where"
            f" this guard scans them; found {len(shells)}",
        )
        # And the one already-compliant template is still there and quiet.
        lib = SCRIPTS / "_lib_push_with_rebase.sh"
        self.assertTrue(lib.exists())
        self.assertEqual(
            find_bare_agents_roots_in_text(
                lib.read_text(encoding="utf-8"), filename=lib.name
            ),
            [],
        )

    def test_no_dead_waivers(self):
        """A waiver that matches nothing is decoration that rots into a
        false sense of coverage. Both dicts must keep earning themselves.
        Vacuous for ALLOWED_SITES today (it is empty) — which is exactly
        when to install the check, because the hole opens the day someone
        adds the first entry."""
        by_file = {}
        for py in sorted(SCRIPTS.glob("*.py")):
            by_file[py.name] = collect_candidates(
                py.read_text(encoding="utf-8"), filename=py.name
            )
        for sh in sorted(SCRIPTS.glob("*.sh")):
            by_file[sh.name] = collect_candidates_text(
                sh.read_text(encoding="utf-8"), filename=sh.name
            )
        for filename, key in ALLOWED_SITES:
            candidates = by_file.get(filename)
            self.assertIsNotNone(
                candidates, f"ALLOWED_SITES names a missing file {filename!r}"
            )
            self.assertTrue(
                any(c.key == key for c in candidates),
                f"dead waiver: ALLOWED_SITES[({filename!r}, {key!r})]"
                " matches nothing in the tree",
            )
        for filename in ALLOWED_FILES:
            candidates = by_file.get(filename)
            self.assertIsNotNone(
                candidates, f"ALLOWED_FILES names a missing file {filename!r}"
            )
            self.assertTrue(
                candidates,
                f"dead waiver: ALLOWED_FILES entry {filename!r} contains no"
                " bare fallback, so it exempts nothing and should be removed",
            )

    def test_modules_resolve_under_override(self):
        """Behavioral: with OURLIBERTY_AGENTS_ROOT set and HOME pointed at a
        bogus tier-2 home, lightweight modules resolve their root under the
        override, not under HOME.

        This is the SECOND LINE OF DEFENSE for the declared blind spots
        (aliases especially). It used to name five modules; the five
        `HOME = Path(os.environ.get('HOME', ...))` modules were outside it
        by omission, which is precisely the class the scanner also cannot
        see, so a miss in both was a miss everywhere."""
        override = "/tmp/ol-test-agents-root"
        mods = [
            ("larry_alerts", "AGENTS_ROOT"),
            ("concurrency_guard", "AGENTS_ROOT"),
            ("dispatch_lease", "AGENTS_ROOT"),
            ("kill_switch", "AGENTS_ROOT"),
            ("active_tier", "AGENTS_ROOT"),
            # The env-HOME family: each binds HOME from os.environ and then
            # derives its agents root on the very next line.
            ("cost_by_repo", "_AGENTS"),
            ("factory_utilization", "AGENTS_ROOT"),
            ("govern_loop_readiness", "AGENTS_ROOT"),
            ("ledger_weekly", "_AGENTS"),
            ("pulse_check_i", "_AGENTS"),
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
            # modules' AGENTS_ROOT (and every derived constant) stay
            # pinned to the bogus override for the rest of the process — the
            # same leak class ApprovalRootEnvTest's tearDown re-reload exists
            # to prevent (test_beacon_approval_root_env.py).
            for modname, _ in mods:
                importlib.reload(importlib.import_module(modname))

    def test_repaired_shell_runner_halts_under_the_override(self):
        """Behavioral proof of the SHELL repair, not of the scanner.

        run_ledger.sh reads EMERGENCY_HALT. Point HOME at a bogus tier-2
        home and OURLIBERTY_AGENTS_ROOT at the real root that holds the
        halt flag — exactly the child env agent_runner builds. Before the
        repair this invocation ran the work; now it halts. The writer and
        the reader agreeing under a swapped HOME is the actual fix."""
        script = SCRIPTS / "run_ledger.sh"
        self.assertTrue(script.exists(), script)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bogus_home = tmp / "tier2-home"
            real_root = tmp / "real" / "agents"
            (real_root / "blackboard").mkdir(parents=True)
            (real_root / "blackboard" / "EMERGENCY_HALT").write_text("halt\n")
            # The runner sources its push helper from $HOME/agent-core.
            helper_dir = bogus_home / "agent-core" / "scripts"
            helper_dir.mkdir(parents=True)
            (helper_dir / "_lib_push_with_rebase.sh").write_text(
                (SCRIPTS / "_lib_push_with_rebase.sh").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["HOME"] = str(bogus_home)
            env["OURLIBERTY_AGENTS_ROOT"] = str(real_root)
            proc = subprocess.run(
                ["bash", str(script)],
                env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertIn(
                "EMERGENCY_HALT present",
                proc.stdout + proc.stderr,
                f"rc={proc.returncode}\nstdout={proc.stdout}\n"
                f"stderr={proc.stderr}",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # And it honored the override for its state/logs, not $HOME.
            self.assertFalse(
                (bogus_home / "agents").exists(),
                "the runner still built state under the swapped HOME",
            )

    def test_repaired_shell_runner_falls_back_to_home_when_unset(self):
        """The OTHER arm of `${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}`.

        Every wrapper harness in the suite PINS the override (it has to —
        _bootstrap exports a sandbox root that would otherwise win over the
        swapped HOME). The consequence is that the default arm — the only
        arm the systemd-invoked runners ever take in production — had zero
        executable coverage: nothing ran a repaired runner with the variable
        UNSET. A repair that spelled the expansion `${OURLIBERTY_AGENTS_ROOT}`
        with no default would pass every other test in this file and resolve
        to `/blackboard` on the droplet.

        Same runner and same halt-flag signal as the override test above, so
        the two are a matched pair: that one proves the override WINS, this
        one proves $HOME still ANSWERS when there is no override.
        """
        script = SCRIPTS / "run_ledger.sh"
        self.assertTrue(script.exists(), script)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            home = tmp / "home"
            (home / "agents" / "blackboard").mkdir(parents=True)
            (home / "agents" / "blackboard" / "EMERGENCY_HALT").write_text(
                "halt\n"
            )
            helper_dir = home / "agent-core" / "scripts"
            helper_dir.mkdir(parents=True)
            (helper_dir / "_lib_push_with_rebase.sh").write_text(
                (SCRIPTS / "_lib_push_with_rebase.sh").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["HOME"] = str(home)
            # REMOVED, not blanked — this is the whole point of the test.
            env.pop(ENV_VAR, None)
            proc = subprocess.run(
                ["bash", str(script)],
                env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertIn(
                "EMERGENCY_HALT present",
                proc.stdout + proc.stderr,
                "with no override set, the runner must resolve the agents "
                f"root from $HOME.\nrc={proc.returncode}\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)


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

    # -- the cross-product properties ---------------------------------------

    def test_property_every_home_x_composition_x_segment_flags_once(self):
        """The three widenings are three AXES of one predicate. Per-axis
        fixtures would each pass while the COMBINATION stayed quiet, so the
        assertion is the whole cross-product."""
        checked = 0
        for home in HOME_SPELLINGS:
            for cname, build in COMPOSITIONS:
                for seg in SEGMENTS:
                    combo = (home, cname, seg)
                    src = "p = %s\n" % build(home, seg)
                    offenders = find_bare_agents_roots(src)
                    if combo in UNCOVERED_COMBINATIONS:
                        self.assertEqual(offenders, [], (combo, src))
                        continue
                    checked += 1
                    self.assertEqual(
                        len(offenders), 1,
                        f"{combo}: {src.strip()} -> {offenders}",
                    )
        self.assertEqual(
            checked,
            len(HOME_SPELLINGS) * len(COMPOSITIONS) * len(SEGMENTS)
            - len(UNCOVERED_COMBINATIONS),
        )

    def test_property_segment_boundary_discriminates(self):
        """The widening is a DISCRIMINATION, so it is asserted as one: over
        the same home x composition grid, a sub-path segment must flag and
        every sibling tree — plus an absolute '/agents', which is a pathlib
        reset rather than a home fallback — must stay quiet. Asserting only
        the quiet half would pass against a scanner that sees nothing at
        all, which is exactly the false clean this widening closes."""
        for home in HOME_SPELLINGS:
            for cname, build in COMPOSITIONS:
                covered = "p = %s\n" % build(home, "agents/state")
                self.assertEqual(
                    len(find_bare_agents_roots(covered)), 1,
                    f"{(home, cname, 'agents/state')}: {covered.strip()}",
                )
                for seg in NEGATIVE_SEGMENTS:
                    src = "p = %s\n" % build(home, seg)
                    self.assertEqual(
                        find_bare_agents_roots(src), [],
                        f"{(home, cname, seg)}: {src.strip()}",
                    )

    def test_property_operand_order_discriminates(self):
        """Only home-FIRST orderings produce a home-rooted path. Each
        asymmetric operator is asserted as a PAIR — the home-first form
        flags, the reversed twin does not — so neither a scanner that sees
        neither nor a symmetric one that flags both can pass."""
        for flags, quiet in (
            ("p = str(Path.home()) + '/agents'\n",
             "p = '/agents' + str(Path.home())\n"),
            ("p = os.path.join(Path.home(), 'agents')\n",
             "p = os.path.join('agents', Path.home())\n"),
            ("p = Path(Path.home(), 'agents')\n",
             "p = Path('agents', Path.home())\n"),
            ("p = '%s/agents' % Path.home()\n",
             "p = Path.home() % '%s/agents'\n"),
            ("p = Path.home() / 'agents'\n",
             "p = 'agents' / Path.home()\n"),
            ("p = Path.home() / 'agents'\n",
             "p = Path('agents') / Path.home()\n"),
            ("p = f'{Path.home()}/agents'\n",
             "p = f'/agents{Path.home()}'\n"),
        ):
            self.assertEqual(len(find_bare_agents_roots(flags)), 1, flags)
            self.assertEqual(find_bare_agents_roots(quiet), [], quiet)

    def test_property_home_spellings_are_all_recognized(self):
        """`_is_home_base` must accept every member of the shared set — the
        same set the composition property consumes, so the two cannot
        drift."""
        for home in HOME_SPELLINGS:
            src = "p = %s / 'agents'\n" % home
            self.assertEqual(len(find_bare_agents_roots(src)), 1, src)

    def test_property_home_base_discriminates(self):
        """Each non-home base is asserted against the home TWIN it differs
        from by one token, so the pair fails both ways: a scanner blind to
        the env-HOME spellings fails the flagging half, and a suffix match
        on names ENDING in HOME fails the quiet half. The load-bearing
        negatives are the TIER1_HOME ones: that is the REAL account home,
        and agent_runner.py:1507 spells the override pin itself as
        `Path(active_tier.TIER1_HOME) / 'agents'` — a suffix match would
        flag the fix with its own guard."""
        for quiet, flags in (
            ("p = Path(os.environ['XDG_DATA_HOME']) / 'agents'\n",
             "p = Path(os.environ['HOME']) / 'agents'\n"),
            ("p = Path(os.environ.get('OURLIBERTY_TIER2_HOME')) / 'agents'\n",
             "p = Path(os.environ.get('HOME')) / 'agents'\n"),
            ("p = active_tier.TIER1_HOME / 'agents'\n",
             "p = Path(os.environ.get('HOME', '/home/larry')) / 'agents'\n"),
            ("p = Path(active_tier.TIER1_HOME) / 'agents'\n",
             "p = Path(os.path.expanduser('~')) / 'agents'\n"),
            ("p = cfg.HOMEPAGE / 'agents'\n",
             "p = cfg.HOME / 'agents'\n"),
            ("p = Path.home / 'agents'\n",
             "p = Path.home().resolve() / 'agents'\n"),
            ("p = os.path.join(repo_dir, 'agents')\n",
             "p = os.path.join(os.environ['HOME'], 'agents')\n"),
            ("p = os.path.join(TIER1_HOME, '.config', 'gh')\n",
             "p = os.path.join(Path('~').expanduser(), 'agents')\n"),
            ("p = Path(base_dir, 'agents')\n",
             "p = Path(Path.home(), 'agents')\n"),
        ):
            self.assertEqual(find_bare_agents_roots(quiet), [], quiet)
            self.assertEqual(len(find_bare_agents_roots(flags)), 1, flags)

    def test_property_guard_climbs_over_every_composition(self):
        """Widening detection must not outrun `_guard_verdict`'s parent
        climb. Asserted as a pair per composition: the bare form flags and
        the SAME form wrapped in the blessed idiom is quiet — the quiet
        half alone would also pass against a scanner that never saw the
        composition in the first place."""
        for home in HOME_SPELLINGS:
            for cname, build in COMPOSITIONS:
                composed = build(home, "agents/state")
                bare = "p = %s\n" % composed
                guarded = (
                    "p = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT')"
                    " or %s)\n" % composed
                )
                self.assertEqual(
                    len(find_bare_agents_roots(bare)), 1,
                    f"{(home, cname)}: {bare}")
                self.assertEqual(
                    find_bare_agents_roots(guarded), [],
                    f"{(home, cname)}: {guarded}")

    def test_multi_segment_does_not_double_count(self):
        # The two-segment spelling already flagged on the INNER Div; after
        # widening the segment rule the one-node spelling must flag exactly
        # once and the two-segment one must not start double-counting (its
        # outer Div's right operand is 'state', which the segment rule
        # rejects).
        self.assert_flags("p = Path.home() / 'agents' / 'state'\n", n=1)
        self.assert_flags("p = Path.home() / 'agents/state'\n", n=1)

    def test_flags_live_multi_segment_shapes(self):
        # The exact shapes that were a false clean at HEAD.
        for src in (
            "LEASE = Path.home() / 'agents/state/dispatch.lease'\n",
            "p = Path.home().joinpath('agents/state')\n",
            "p = HOME / 'agents/state'\n",
            "def f():\n    return Path.home() / 'agents/logs'\n",
        ):
            self.assertEqual(len(find_bare_agents_roots(src)), 1, src)

    def test_text_channel_agrees_on_multi_segment_shapes(self):
        # The same four shapes carried in a str constant, a bytes constant
        # and an f-string fragment. The channels agree by construction (one
        # shared segment fragment); this pins that they still do.
        payloads = (
            "LEASE = Path.home() / 'agents/state/dispatch.lease'",
            "p = Path.home().joinpath('agents/state')",
            "p = HOME / 'agents/state'",
            "return Path.home() / 'agents/logs'",
        )
        for payload in payloads:
            for src in (
                'cmd = "%s"\n' % payload,
                'cmd = b"%s"\n' % payload,
                'cmd = f"{opt} %s"\n' % payload,
            ):
                self.assertEqual(len(find_bare_agents_roots(src)), 1, src)

    def test_flags_live_env_home_shapes(self):
        # $HOME is the thing the tier swap mutates; the DIRECT spellings of
        # reading it were invisible while the indirect Path.home() was not.
        for src in (
            "p = Path(os.environ['HOME']) / 'agents'\n",
            "p = Path(os.environ.get('HOME')) / 'agents'\n",
            "p = Path(os.environ.get('HOME', '/home/larry')) / 'agents'\n",
            "p = Path('~').expanduser() / 'agents'\n",
            "p = Path(os.path.expanduser('~')) / 'agents'\n",
            "p = Path.home().resolve() / 'agents'\n",
        ):
            self.assertEqual(len(find_bare_agents_roots(src)), 1, src)

    def test_flags_live_composition_shapes(self):
        # Every operator that was clean at HEAD.
        for src in (
            "p = os.path.join(os.environ['HOME'], 'agents')\n",
            "p = os.path.join(str(Path.home()), 'agents')\n",
            "p = Path(Path.home(), 'agents')\n",
            "p = home_dir + '/agents'\n".replace("home_dir", "str(HOME)"),
            'p = Path(f"{Path.home()}/agents")\n',
            "p = '%s/agents' % Path.home()\n",
        ):
            self.assertEqual(len(find_bare_agents_roots(src)), 1, src)

    def test_two_statement_healer_shape(self):
        # The first statement flags; the second is the declared alias hop.
        offenders = find_bare_agents_roots(
            "AGENTS_ROOT = os.path.join(os.environ['HOME'], 'agents')\n"
            "BLACKBOARD = os.path.join(AGENTS_ROOT, 'blackboard')\n"
        )
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("os.path.join(os.environ['HOME'], 'agents')",
                      offenders[0])
        self.assertNotIn("AGENTS_ROOT, 'blackboard'", offenders[0])

    # -- receiver provenance (blessing side) ---------------------------------

    def test_property_stdlib_wrappers_transparent_hostile_receivers_not(self):
        """The bug was that the wrapper check keyed on the METHOD NAME
        alone, so `cfg.str(...)` was as transparent as `str(...)`. One
        fixture would not have caught it — the property over the
        cross-product is the point."""
        wrappers = ("Path", "PurePath", "PosixPath", "str", "fspath")
        tail = (
            "    return Path(root) if root else Path.home() / 'agents'\n"
        )
        for w in wrappers:
            src = (
                "def f():\n"
                f"    root = {w}(os.environ.get('OURLIBERTY_AGENTS_ROOT'))\n"
                + tail
            )
            # Trusted, but WRAPPED: always truthy, so the fallback is dead.
            offenders = find_bare_agents_roots(src)
            self.assertEqual(len(offenders), 1, src)
            self.assertIn("always truthy", offenders[0])
        for w in ("pathlib.Path", "os.fspath"):
            src = (
                "def f():\n"
                f"    root = {w}(os.environ.get('OURLIBERTY_AGENTS_ROOT'))\n"
                + tail
            )
            offenders = find_bare_agents_roots(src)
            self.assertEqual(len(offenders), 1, src)
            self.assertIn("always truthy", offenders[0])
        for recv in ("cfg", "helpers", "self", "mod", "_shim"):
            for w in wrappers:
                src = (
                    "def f():\n"
                    f"    root = {recv}.{w}("
                    "os.environ.get('OURLIBERTY_AGENTS_ROOT'))\n" + tail
                )
                offenders = find_bare_agents_roots(src)
                self.assertEqual(len(offenders), 1, src)
                self.assertIn("NOT trusted", offenders[0])

    def test_property_guard_side_receiver_equivalence(self):
        """Same receiver, same arity, different method name must give the
        SAME verdict: `cfg.get(root)` was already flagged, `cfg.str(root)`
        was blessed purely because of the name."""
        for recv in ("cfg", "helpers", "self", "mod", "_shim"):
            for w in ("Path", "PurePath", "PosixPath", "str", "fspath", "get"):
                for shape in (
                    "p = %s.%s(root) or Path.home() / 'agents'\n",
                    "p = Path(root) if %s.%s(root)"
                    " else Path.home() / 'agents'\n",
                ):
                    src = (
                        "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
                        + shape % (recv, w)
                    )
                    self.assertEqual(len(find_bare_agents_roots(src)), 1, src)

    def test_wrapped_binding_makes_the_fallback_dead_code(self):
        # REVERSAL of a previously-pinned expectation. The old
        # test_allows_read_wrapped_in_path_constructor asserted these were
        # QUIET, on the theory that Path()/str() are value-preserving for
        # the trust rule. They are not TRUTHINESS-preserving, which is the
        # only property a guard uses.
        for src, needle in (
            (
                "def f():\n"
                "    root = Path(os.environ.get("
                "'OURLIBERTY_AGENTS_ROOT', ''))\n"
                "    return root if root else Path.home() / 'agents'\n",
                "always truthy",
            ),
            (
                "def f():\n"
                "    root = str(os.environ.get('OURLIBERTY_AGENTS_ROOT'))\n"
                "    return Path(root) if root else Path.home() / 'agents'\n",
                "always truthy",
            ),
        ):
            offenders = find_bare_agents_roots(src)
            self.assertEqual(len(offenders), 1, offenders)
            self.assertIn(needle, offenders[0])

    def test_dead_fallback_premise_is_true_at_runtime(self):
        """The rule is not arbitrary: prove its premise BY EXECUTION, and
        in the same breath assert the scanner acts on that premise. The
        runtime half alone proves nothing about this guard — it is true of
        every Python — so each truthiness fact is paired with the binding
        shape it condemns."""
        self.assertEqual(Path(""), Path("."))
        for label, value, binding in (
            ("Path('')", Path(""),
             "Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', ''))"),
            ("Path('.')", Path("."),
             "Path(os.environ.get('OURLIBERTY_AGENTS_ROOT'))"),
            ("str(None)", str(None),
             "str(os.environ.get('OURLIBERTY_AGENTS_ROOT'))"),
        ):
            self.assertTrue(bool(value), label)
            src = (
                "def f():\n"
                f"    root = {binding}\n"
                "    return root if root else Path.home() / 'agents'\n"
            )
            offenders = find_bare_agents_roots(src)
            self.assertEqual(len(offenders), 1, (label, offenders))
            self.assertIn("always truthy", offenders[0], label)

    def test_wrapper_position_decides_the_verdict(self):
        """The rule is not 'wrappers are opaque' — that would break the
        live idiom. It is 'a wrapper on the BINDING kills the truthiness
        guard'. So the two positions are asserted together: wrapper on the
        binding FLAGS, wrapper on the use (the shape ~25 live sites spell)
        stays QUIET."""
        self.assertIn(
            "always truthy",
            find_bare_agents_roots(
                "def f():\n"
                "    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT'))\n"
                "    return root if root else Path.home() / 'agents'\n")[0],
        )
        self.assert_quiet(
            "def f():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    # -- environ receiver provenance -----------------------------------------

    def test_property_environ_must_earn_its_identity(self):
        """`_is_environ` used to accept any name spelled `environ`, which
        is the one check whose own docstring said a snapshot is not the
        env. A snapshot taken before agent_runner pins the override reads
        None and takes the bare fallback."""
        tail = (
            "def f():\n"
            "    root = environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n"
        )
        for binding in (
            "environ = os.environ\n",
            "from os import environ\n",
        ):
            self.assert_quiet(binding + tail)
        for binding in (
            "environ = os.environ.copy()\n",
            "environ = dict(os.environ)\n",
            "environ = dict(SNAPSHOT)\n",
            "environ = {}\n",
            "environ = env if env is not None else os.environ\n",
            "from settings import environ\n",
        ):
            src = binding + tail
            self.assertEqual(len(find_bare_agents_roots(src)), 1, src)
        # Parameter form: the name is bound by a signature, not a value.
        self.assert_flags(
            "def f(environ):\n"
            "    root = environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_environ_verdict_follows_bindings_not_spelling(self):
        """Rename a qualifying receiver and it stays qualifying; rename a
        non-qualifying one to `environ` and it stays non-qualifying. That
        equivalence IS the finding, stated as a property."""
        tail = (
            "def f():\n"
            "    root = %s.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n"
        )
        self.assert_quiet("env_snapshot = os.environ\n" + tail % "env_snapshot")
        self.assert_flags(
            "environ = os.environ.copy()\n" + tail % "environ")

    def test_flags_snapshot_binding_from_the_live_tree(self):
        # build_sequence_validator.py:831's exact shape.
        self.assert_flags(
            "def f(env=None):\n"
            "    environ = env if env is not None else os.environ\n"
            "    root = environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_rebinding_os_disqualifies_the_module_path(self):
        for prelude in ("os = shim\n", "import myos as os\n"):
            src = (
                prelude
                + "def f():\n"
                "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
                "    return Path(root) if root else Path.home() / 'agents'\n"
            )
            self.assertEqual(len(find_bare_agents_roots(src)), 1, src)
        # ...and a plain `import os` does not.
        self.assert_quiet(
            "import os\n"
            "def f():\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")

    def test_tree_has_no_star_import_or_os_rebind(self):
        """The poison and the os-provenance check are free today. Assert it,
        so a future star import is a deliberate decision rather than a
        silent trust reset."""
        for py in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=py.name)
            bindings, star = _collect_bindings(tree)
            self.assertFalse(star, f"{py.name} contains a star import")
            self.assertTrue(
                _is_unrebound_module("os", "os", bindings),
                f"{py.name} rebinds the name `os`",
            )

    # -- star-import poison --------------------------------------------------

    def test_star_import_poisons_name_trust(self):
        offenders = find_bare_agents_roots(
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "from settings import *\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("star import", offenders[0])

    def test_property_star_import_poison_is_order_free(self):
        """Import-then-bind and bind-then-import both poison: the model is
        deliberately flow-free, and an 'optimization' that only poisoned
        bindings after the import would be a bypass."""
        read = "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
        use = "p = Path(root) if root else Path.home() / 'agents'\n"
        star = "from settings import *\n"
        for src in (
            star + read + use,
            read + star + use,
            read + use + star,
        ):
            self.assertEqual(len(find_bare_agents_roots(src)), 1, src)
        # The NAMED twin already flagged; both spellings must agree.
        self.assertEqual(
            len(find_bare_agents_roots(
                read + "from settings import root\n" + use)), 1)

    def test_star_import_poison_reaches_the_environ_set(self):
        """The missed-clear pair: a fix that only cleared `_trusted_names`
        would leave the second name-trust set blessed."""
        offenders = find_bare_agents_roots(
            "environ = os.environ\n"
            "from settings import *\n"
            "def f():\n"
            "    root = environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    return Path(root) if root else Path.home() / 'agents'\n")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("star import", offenders[0])

    def test_star_import_poison_spares_only_the_inline_env_default(self):
        """The poison must reach the NAME path and stop at the inline one.
        Both halves in one test: in the same star-importing file, a
        name-guarded fallback FLAGS while an inline env-get default stays
        quiet. The inline form has no name to lie about, and it is the exit
        (remediation 1) the poisoned offender message points at."""
        star = "from settings import *\n"
        offenders = find_bare_agents_roots(
            star
            + "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "p = Path(root) if root else Path.home() / 'agents'\n")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("star import", offenders[0])
        self.assert_quiet(
            star
            + "p = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT',"
            " str(Path.home() / 'agents')))\n")

    def test_star_import_case_runs_and_resolves_a_wrong_path(self):
        """Runtime truth: unlike the pinned scope-free limits, this code
        does NOT NameError — it resolves under the swapped HOME with the
        override set. That is what makes it a defect rather than a declared
        blind spot. The SAME source is fed to the scanner here, so the test
        proves the guard catches the thing it just watched go wrong."""
        victim = (
            "import os\n"
            "from pathlib import Path\n"
            "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "from settings import *\n"
            "resolved = Path(root) if root else Path.home() / 'agents'\n"
            "print(resolved)\n"
        )
        offenders = find_bare_agents_roots(victim, filename="victim.py")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("star import", offenders[0])
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "settings.py").write_text("root = ''\n")
            (tmp / "victim.py").write_text(victim)
            env = dict(os.environ)
            env["OURLIBERTY_AGENTS_ROOT"] = "/tmp/ol-real-agents"
            env["HOME"] = str(tmp / "bogus-tier2")
            proc = subprocess.run(
                [sys.executable, str(tmp / "victim.py")],
                cwd=str(tmp), env=env, capture_output=True, text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                proc.stdout.strip(),
                str(Path(env["HOME"]) / "agents"),
                proc.stdout,
            )

    # -- the shell-default waiver, fail-closed -------------------------------

    def test_property_shell_default_brace_topologies(self):
        """Only a same-line CLOSED expansion that actually encloses the
        match may waive. Every other topology flags."""
        bare = "cp -r $HOME/agents /x"
        cases = (
            # (label, line, expected offender count)
            ("closed-before-match", 'R=${%s:-a}; %s' % (ENV_VAR, bare), 1),
            ("closed-after-match", '${%s:-%s}' % (ENV_VAR, bare), 0),
            ("no-brace-on-line", 'a ${%s:-b; %s' % (ENV_VAR, bare), 1),
            (
                "two-openers-first-unterminated-second-closed",
                '${%s:-b ${%s:-%s}' % (ENV_VAR, ENV_VAR, bare),
                0,
            ),
            (
                "two-openers-first-closed-second-unterminated",
                '${%s:-a} ${%s:-b %s' % (ENV_VAR, ENV_VAR, bare),
                1,
            ),
            ("bare-before-any-opener", '%s ${%s:-x}' % (bare, ENV_VAR), 1),
        )
        for label, line, expected in cases:
            src = "cmd = %r\n" % line
            self.assertEqual(
                len(find_bare_agents_roots(src)), expected, (label, src))
            # And the raw .sh channel must give the same answer.
            self.assertEqual(
                len(find_bare_agents_roots_in_text(line + "\n")),
                expected,
                (label, line),
            )

    def test_flags_unterminated_expansion_repros(self):
        self.assert_flags(
            "cmd = 'a ${OURLIBERTY_AGENTS_ROOT:-b; cp -r $HOME/agents /x'\n")
        offenders = find_bare_agents_roots(
            'cmd = \'ROOT="${OURLIBERTY_AGENTS_ROOT:-$HOME/agents" ;'
            ' LOGS="$HOME/agents/logs"\'\n')
        # Both bare paths were silenced at HEAD; both must be represented.
        self.assertGreaterEqual(len(offenders), 1, offenders)
        self.assertIn("$HOME/agents", offenders[0])

    def test_brace_supplied_by_an_interpolation_is_not_a_waiver(self):
        """The strongest form: an f-string interpolation can supply the
        closing brace, so the runtime shell is well-formed while this
        scanner sees none. Byte-identical runtime shell must not get
        opposite answers, so the literal-brace twin is asserted alongside."""
        laundered = (
            'default_root = "/srv/agents-default}"\n'
            "cmd = f'ROOT=\"${{OURLIBERTY_AGENTS_ROOT:-{default_root}\" ;"
            ' cp -r $HOME/agents \\"$ROOT/backup\\"\'\n'
        )
        literal = (
            "cmd = f'ROOT=\"${{OURLIBERTY_AGENTS_ROOT:-/srv/agents-default}}\""
            ' ; cp -r $HOME/agents \\"$ROOT/backup\\"\'\n'
        )
        self.assertEqual(
            len(find_bare_agents_roots(laundered)),
            len(find_bare_agents_roots(literal)),
        )
        self.assertEqual(len(find_bare_agents_roots(laundered)), 1)

    def test_both_shell_default_operators_waive_only_when_closed(self):
        """Both operators SHELL_DEFAULT accepts must waive — and both must
        stop waiving when the brace is missing. Pinning only the quiet half
        would pass against the fail-OPEN branch this replaced."""
        for op in (":-", ":="):
            closed = (
                "cmd = 'ROOT=\"${OURLIBERTY_AGENTS_ROOT%s$HOME/agents}\"'\n"
                % op)
            unterminated = (
                "cmd = 'ROOT=\"${OURLIBERTY_AGENTS_ROOT%s$HOME/agents\"'\n"
                % op)
            self.assert_quiet(closed)
            self.assertEqual(
                len(find_bare_agents_roots(unterminated)), 1, unterminated)
            # ...and the raw .sh channel answers identically.
            self.assertEqual(
                find_bare_agents_roots_in_text(
                    'ROOT="${OURLIBERTY_AGENTS_ROOT%s$HOME/agents}"\n' % op),
                [], op)
            self.assertEqual(
                len(find_bare_agents_roots_in_text(
                    'ROOT="${OURLIBERTY_AGENTS_ROOT%s$HOME/agents"\n' % op)),
                1, op)

    def test_an_unrelated_later_expansion_does_not_close_the_opener(self):
        """A `}` belonging to some OTHER expansion is not this opener's close.

        The unterminated-opener case above is only fail-closed while the line
        has no later brace at all. Real lines are not that tidy: an export
        that also mentions ${PATH}, a log line that also mentions ${HOME}.
        Under first-brace matching those all hand back the waiver, so the
        fail-closed posture held only for the tidiest possible counterexample
        — which is the one a test author naturally writes.
        """
        for tail in ('${PATH}', '${HOME}', '${OURLIBERTY_LOG_DIR}'):
            line = ('ROOT="${OURLIBERTY_AGENTS_ROOT:-$HOME/agents" ; '
                    'echo "%s"\n' % tail)
            self.assertEqual(
                len(find_bare_agents_roots_in_text(line)), 1,
                f'unterminated opener waived by an unrelated {tail}: {line}')
            # The .py string channel must give the same answer.
            self.assertEqual(
                len(find_bare_agents_roots('cmd = %r\n' % line)), 1, line)

    def test_a_nested_expansion_inside_the_default_still_waives(self):
        """The other direction: brace MATCHING must not break the legitimate
        nested spelling, where the fallback itself is an expansion."""
        for src in (
            'ROOT="${OURLIBERTY_AGENTS_ROOT:-${HOME}/agents}"\n',
            'ROOT="${OURLIBERTY_AGENTS_ROOT:-${HOME}/agents}/state" ;'
            ' echo "${PATH}"\n',
        ):
            self.assertEqual(
                find_bare_agents_roots_in_text(src), [], src)

    # -- the .sh channel -----------------------------------------------------

    def test_property_shell_spelling_matrix(self):
        # Each home spelling, bare and wrapped, at each suffix depth. The
        # wrapped half uses the SAME spelling inside the expansion, so the
        # matrix proves the waiver tracks the construct rather than one
        # blessed string.
        homes = ("$HOME/agents", "${HOME}/agents",
                 '"$HOME"/agents', '"${HOME}"/agents')
        suffixes = ("", "/state", "/logs", "/blackboard/EMERGENCY_HALT")
        for home in homes:
            for suffix in suffixes:
                bare = 'LOCK=%s%s\n' % (home, suffix)
                wrapped = 'LOCK="${%s:-%s}%s"\n' % (ENV_VAR, home, suffix)
                self.assertEqual(
                    len(find_bare_agents_roots_in_text(bare)), 1, bare)
                self.assertEqual(
                    find_bare_agents_roots_in_text(wrapped), [], wrapped)
        # ...and the repaired production spelling, verbatim.
        self.assertEqual(
            find_bare_agents_roots_in_text(
                'LOCK_DIR="${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}/state"\n'),
            [],
        )

    def test_property_channels_agree_on_the_same_payload(self):
        """Moving a payload out of a Python string into a .sh file must not
        turn the check off — that is the hole, stated as an identity."""
        payloads = (
            'LOCK_DIR="${HOME}/agents/state"',
            'HALT="$HOME/agents/blackboard/EMERGENCY_HALT"',
            "mount --bind $HOME/agents /jail",
            'cd "${HOME}"/agents/logs',
        )
        for payload in payloads:
            as_py = find_bare_agents_roots("cmd = %r\n" % payload)
            as_sh = find_bare_agents_roots_in_text(payload + "\n")
            self.assertEqual(len(as_py), len(as_sh), payload)
            self.assertEqual(len(as_sh), 1, payload)
        for quiet in (
            'cd "${HOME}/agent-core/agents/pulse"',
            'ls "${HOME}/agents-archive"',
            'state_dir="${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}/state"',
        ):
            self.assertEqual(
                find_bare_agents_roots("cmd = %r\n" % quiet), [], quiet)
            self.assertEqual(
                find_bare_agents_roots_in_text(quiet + "\n"), [], quiet)

    def test_property_executable_tilde_assignment_flags_prose_does_not(self):
        """`LOG=~/agents/logs` in a .sh runner is a real tier-2 wrong-home
        write, and the '~/agents in prose' blind spot does NOT cover it: an
        executable assignment is not a sentence. The discriminator is that
        the tilde must be UNQUOTED and welded to an `=` or `:` — in shell
        there is no tilde expansion inside quotes, so `"~/agents"` is a
        directory literally named `~` and is not a home path at all.

        Both halves are asserted over the same shapes, because a matcher
        loose enough to catch the payload would light up 259 docstrings."""
        for payload in (
            "LOG=~/agents/logs",
            "STATE=~/agents",
            # NB: deeper prod sub-trees (~/agents/blackboard/, ~/agents/
            # state/) are banned literals in scripts/tests by the
            # production-path leak gate, so the depth case uses a leaf the
            # gate allows. The segment rule is depth-blind either way.
            "FLAG=~/agents/rotation.disabled",
            "PATH=$PATH:~/agents/bin",
            "python3 script.py --root=~/agents/state",
        ):
            self.assertEqual(
                len(find_bare_agents_roots_in_text(payload + "\n")), 1,
                payload)
            # ...and the channels agree: same payload, same verdict.
            self.assertEqual(
                len(find_bare_agents_roots("cmd = %r\n" % payload)), 1,
                payload)
        for quiet in (
            # quoted: no tilde expansion in shell, so not a home path
            'LOG="~/agents/logs"',
            "LOG='~/agents/logs'",
            # sibling tree
            "LOG=~/agents-archive",
            "LOG=~/agents_old",
            # prose spellings, which is why the token alone cannot match
            "# tail -f ~/agents/logs/pulse.log",
            'echo "Tail log: tail -f ~/agents/logs/x.log"',
            "# rollback: echo tier1 > ~/agents/rotation.disabled",
            "# the default is AGENTS_ROOT = ~/agents",
            # and the guarded spelling stays guarded
            'LOG="${OURLIBERTY_AGENTS_ROOT:-~/agents}/logs"',
        ):
            self.assertEqual(
                find_bare_agents_roots_in_text(quiet + "\n"), [], quiet)
            self.assertEqual(
                find_bare_agents_roots("cmd = %r\n" % quiet), [], quiet)

    # -- waiver mechanics ----------------------------------------------------

    def test_waiver_refuses_to_cover_a_textual_twin(self):
        src = ("REAL = Path.home() / 'agents'\n"
               "OOPS = Path.home() / 'agents'\n")
        with mock.patch.dict(
            ALLOWED_SITES,
            {("v.py", "Path.home() / 'agents'"): "jail target"},
        ):
            offenders = find_bare_agents_roots(src, filename="v.py")
        self.assertEqual(len(offenders), 2, offenders)
        for o in offenders:
            self.assertIn("textually identical occurrences", o)

    def test_property_waiver_arity(self):
        """Stated as a function of N, so a fix that special-cases N=2 is
        caught. N=0 is the dead waiver (covered by test_no_dead_waivers);
        N=1 exempts; N>=2 exempts nothing."""
        for n in (1, 2, 3):
            src = "".join(
                "x%d = Path.home() / 'agents'\n" % i for i in range(n)
            )
            with mock.patch.dict(
                ALLOWED_SITES,
                {("v.py", "Path.home() / 'agents'"): "test waiver"},
            ):
                offenders = find_bare_agents_roots(src, filename="v.py")
            self.assertEqual(len(offenders), 0 if n == 1 else n, (n, offenders))

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

    def test_waiver_key_normalization_is_pinned(self):
        """The waiver's blast radius depends entirely on this, and it was
        undocumented. `' '.join(seg.split())` collapses runs of whitespace
        but does NOT normalize spacing around the operator, so the two
        spellings are DIFFERENT keys."""
        spaced = "Path.home() / 'agents'"
        tight = "Path.home()/'agents'"
        self.assertNotEqual(_normalize(spaced), _normalize(tight))
        with mock.patch.dict(
            ALLOWED_SITES, {("v.py", spaced): "waiver"},
        ):
            offenders = find_bare_agents_roots(
                "a = %s\nb = %s\n" % (spaced, tight), filename="v.py")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(tight, offenders[0])

    def test_property_every_channel_prints_a_working_waiver_key(self):
        """(a) every offender prints a key, (b) registering exactly that
        printed key silences exactly that offender, (c) it silences nothing
        else in the file. Written as a loop over channels because the bug
        was that ONE channel had the lookup."""
        cases = {
            "ast node": "p = Path.home() / 'agents'\nq = HOME / 'agents'\n",
            "python str": (
                "cmd = 'mount --bind $HOME/agents /jail'\n"
                "other = HOME / 'agents'\n"
            ),
            "python bytes": (
                "cmd = b'mount --bind $HOME/agents /jail'\n"
                "other = HOME / 'agents'\n"
            ),
            "f-string": (
                'cmd = f"{opt} mount --bind $HOME/agents /jail"\n'
                "other = HOME / 'agents'\n"
            ),
        }
        for label, src in cases.items():
            candidates = collect_candidates(src, filename="v.py")
            self.assertEqual(len(candidates), 2, (label, candidates))
            target = candidates[0]
            offenders = find_bare_agents_roots(src, filename="v.py")
            self.assertEqual(len(offenders), 2, (label, offenders))
            self.assertTrue(
                any("waiver key: ALLOWED_SITES[" in o for o in offenders),
                (label, offenders),
            )
            with mock.patch.dict(
                ALLOWED_SITES, {("v.py", target.key): "test"},
            ):
                after = find_bare_agents_roots(src, filename="v.py")
            self.assertEqual(len(after), 1, (label, after))
            self.assertNotIn(target.key, after[0])

        # Same round-trip through the raw .sh channel.
        sh = 'mount --bind $HOME/agents /jail\nLOGS="$HOME/agents/logs"\n'
        candidates = collect_candidates_text(sh, filename="run.sh")
        self.assertEqual(len(candidates), 2, candidates)
        with mock.patch.dict(
            ALLOWED_SITES, {("run.sh", candidates[0].key): "test"},
        ):
            after = find_bare_agents_roots_in_text(sh, filename="run.sh")
        self.assertEqual(len(after), 1, after)

    def test_waiver_key_survives_display_truncation(self):
        """The display string is truncated to 80 chars; the key must not
        be, or it is unguessable for exactly the long lines that need it."""
        long_line = (
            "run_the_thing --with-a-very-long-flag=%s --and-another=%s"
            " --third=%s $HOME/agents/state"
            % ("x" * 30, "y" * 30, "z" * 30)
        )
        src = "cmd = %r\n" % long_line
        candidates = collect_candidates(src, filename="v.py")
        self.assertEqual(len(candidates), 1, candidates)
        self.assertGreater(len(candidates[0].key), 80)
        self.assertEqual(candidates[0].key, _normalize(long_line))
        offenders = find_bare_agents_roots(src, filename="v.py")
        self.assertIn(candidates[0].key, offenders[0])
        with mock.patch.dict(
            ALLOWED_SITES, {("v.py", candidates[0].key): "test"},
        ):
            self.assertEqual(
                find_bare_agents_roots(src, filename="v.py"), [])

    def test_text_channel_obeys_the_same_arity_rule(self):
        sh = ('LOGS="$HOME/agents/logs"\n'
              'LOGS="$HOME/agents/logs"\n')
        key = _normalize('LOGS="$HOME/agents/logs"')
        with mock.patch.dict(ALLOWED_SITES, {("run.sh", key): "test"}):
            offenders = find_bare_agents_roots_in_text(sh, filename="run.sh")
        self.assertEqual(len(offenders), 2, offenders)
        for o in offenders:
            self.assertIn("textually identical occurrences", o)

    # -- honesty -------------------------------------------------------------

    def test_declared_blind_spots_are_real_and_disclosed(self):
        """Every declared blind spot is (a) genuinely quiet and (b) named
        verbatim in the module docstring. This is what stops the docstring
        drifting back into claiming 'any spelling of <home>/agents'."""
        doc = sys.modules[__name__].__doc__
        self.assertIn("WHAT THIS CANNOT SEE", doc)
        for label, fixture, reason in DECLARED_BLIND_SPOTS:
            self.assertIn(label, doc, f"blind spot {label!r} is undisclosed")
            self.assertTrue(reason.strip(), label)
            self.assertEqual(
                find_bare_agents_roots(fixture), [],
                f"{label!r} is disclosed as a blind spot but the scanner"
                f" catches it: {fixture!r}",
            )

    def test_docstring_does_not_overclaim_coverage(self):
        doc = sys.modules[__name__].__doc__
        self.assertNotIn("any spelling of", doc)
        self.assertIn("DIRECTIONAL BIAS", doc)
        # The composition table must be enumerated where it is
        # implemented, not merely summarized here.
        self.assertIn("os.path.join", _is_agents_fallback.__doc__)
        self.assertIn("DECLARED_BLIND_SPOTS", _is_agents_fallback.__doc__)
        self.assertIn("blind spot", _is_home_base.__doc__)

    def test_reports_the_reason_it_fired(self):
        # The message must diagnose, not just accuse: an untrusted guard
        # name is named, the remediation text explains the exits, and the
        # offender carries a paste-ready waiver key.
        offenders = find_bare_agents_roots(
            "def f(a):\n"
            "    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
            "    root = a.root\n"
            "    return Path(root) if root else Path.home() / 'agents'\n",
            filename="victim.py")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("'root'", offenders[0])
        self.assertIn("NOT trusted", offenders[0])
        self.assertIn("waiver key: ALLOWED_SITES[", offenders[0])
        for cue in ("HOW TO CLEAR THIS", "ALLOWED_SITES", ENV_VAR,
                    "waiver key printed", "SAME LINE"):
            self.assertIn(cue, _REMEDIATION)

    def test_reports_file_and_line(self):
        offenders = find_bare_agents_roots(
            "x = 1\np = Path.home() / 'agents'\n", filename="victim.py")
        self.assertEqual(len(offenders), 1)
        self.assertTrue(offenders[0].startswith("victim.py:2:"), offenders)

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

    def test_import_rebind_spellings_agree(self):
        """Three ways to rebind a trusted name through an import. The named
        two already flagged; the STAR spelling was the bypass, because
        `_collect_bindings` filed it under the unusable key '*' and the
        Store/Del sweep sees no Name node for it. Asserting the three
        together is the point — a per-spelling fixture set is complete only
        by luck."""
        read = "root = os.environ.get('OURLIBERTY_AGENTS_ROOT')\n"
        use = "p = Path(root) if root else Path.home() / 'agents'\n"
        for rebind in (
            "import fallback_config as root\n",
            "from settings import root\n",
            "from settings import *\n",
        ):
            src = read + rebind + use
            self.assertEqual(len(find_bare_agents_roots(src)), 1, src)
        self.assertIn(
            "star import",
            find_bare_agents_roots(read + "from settings import *\n" + use)[0])

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

    @unittest.skipIf(
        sys.version_info < (3, 12), "PEP 695 `type X = ...` needs py3.12")
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
        # response to being surfaced is an ALLOWED_SITES entry, never the
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
