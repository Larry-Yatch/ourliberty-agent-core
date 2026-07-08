#!/usr/bin/env python3
"""
test_regression_check.py — Regression-only test gate (dial 3).

Compares the set of failing tests at a parent SHA vs a head SHA and decides
whether the head SHA introduces NEW failures. Mirror invokes this before
emitting REVIEW_PASS (task-26, since 2026-05-20).

The contract is dial 3 from Larry's 5-dial framework:
  - BLOCK iff the head SHA has at least one failing test that was NOT failing
    at the parent SHA (a *regression*).
  - Pre-existing failures (present at both SHAs) are tolerated for the gate
    verdict but reported in the JSON so reviewers can see them.
  - Tests the head SHA *fixes* (failing at parent, passing at head) are
    reported as a bonus.

Why a helper script rather than asking Mirror to run pytest twice and
parse output: deterministic, testable, doesn't burn LLM context on test
output parsing, reusable by future healers.

CLI:
    python3 scripts/test_regression_check.py \\
        --parent-sha <SHA> --head-sha <SHA> \\
        [--repo-root <PATH>] [--timeout-per-sha <SECS>] \\
        [--output json|text]

Exit codes:
    0 — verdict=PASS (no regressions, or both SHAs identical failure sets)
    1 — verdict=BLOCK (head introduces ≥1 new failure)
    2 — analysis failed (git/worktree error, test-runner crash, timeout,
        unresolvable SHA, malformed output). Mirror MUST NOT bypass — a
        failed analysis is itself a reason to request revision.

Output (JSON, default):
    {
      "parent_sha": "abc123",
      "head_sha":   "def456",
      "parent_failures": [<test ids>],
      "head_failures":   [<test ids>],
      "regressions":      [<head − parent>],
      "fixed":            [<parent − head>],
      "pre_existing_unaffected": [<intersection>],
      "verdict": "PASS" | "BLOCK",
      "summary": "<human sentence>"
    }

Path isolation: OURLIBERTY_AGENTS_ROOT (default /home/larry/agents). When
this script provisions tmp worktrees, the OURLIBERTY_AGENTS_ROOT env is
redirected to a tmp dir before invoking the test runner, so a Mirror
worktree never pollutes prod /home/larry/agents/state/* paths (per PR #53).

Run unit tests:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_test_regression_check
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import regression_baseline_cache as baseline_cache

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))

# Per-SHA suite-run cap. Raised 300→800 (2026-07-01): a full `unittest discover`
# pass was MEASURED at ~537s wall (one real sandboxed run; the suite is
# wait/IO-bound — CPU/wall ≈0.40 — not slow-because-loaded), so the old 300s
# default KILLED a healthy run mid-suite whenever the caller passed no explicit
# --timeout-per-sha. That mid-run kill surfaced as EXIT_ANALYSIS_FAIL → a false
# "regression gate inconclusive" ESCALATE on CLEAN code PRs (#747/#763/#790).
# 800s = the measured ~537s + ~50% margin for a slow/overlapped run, still well
# inside the run_review_step wrapper and the 2100s review-session ceiling. See
# memory regression-gate-cant-conclude (contention-hypothesis-refuted entry).
DEFAULT_TIMEOUT_PER_SHA_S = 800
TEST_DISCOVERY_TARGET = 'scripts.tests'

# Real (non-jailed) tree paths, captured from the gate's OWN process at module
# load. The gate parent never swaps HOME, so Path.home() here is the real HOME —
# unlike the per-process jail HOME PR-1's _bootstrap mints INSIDE the suite. Two
# consumers rely on these being the REAL paths: the droplet hard wall (item 3 —
# the RO-bind targets) and the outside-jail tripwire (item 4 — the sentinel scan
# of the production tree). Resolved as module globals so meta-tests can monkey-
# patch them to a tmp tree without touching the real one.
REAL_HOME = Path.home()
REAL_AGENTS = REAL_HOME / 'agents'
REAL_WORKTREES = REAL_HOME / 'agent-worktrees'

# Credential families stripped from the discover subprocess env (item 2 — fixes
# M4/H12). Under the Forge/Mirror systemd units (EnvironmentFile=.env.larry) the
# gate would otherwise pipe live SUPABASE service-role keys, Telegram bot tokens,
# durable claude tokens, and gh tokens INTO the "most sandboxed" vector. Composes
# with PR-2's choke guards: a stripped cred turns a guard-miss from a live write
# into a missing-auth error (and the choke guard turns it into a loud
# TestIsolationBreach first). This is the Layer-A credential list (constraint 6).
_STRIP_ENV_EXACT = frozenset({'GH_TOKEN', 'GITHUB_TOKEN'})
_STRIP_ENV_PREFIXES = (
    'SUPABASE_', 'TELEGRAM_', 'ANTHROPIC_', 'CLAUDE_CODE_OAUTH_',
)
# External-service URL vars pinned to a dead localhost port so an escaped
# prod-API POST connection-refuses instead of hitting live (constraint 6).
_DEAD_API_URL = 'http://127.0.0.1:1'
_DEAD_API_URL_VARS = ('OL_DASHBOARD_API_URL', 'DASHBOARD_API_URL')

# Subdirs of the REAL agents tree the outside-jail tripwire scans for a run's
# sentinel (item 4). INCLUDES outboxes/ — H10 verified the #428 runtime tripwire
# wrongly omits it, and outboxes/ was the 2026-05-13 burn path.
_TRIPWIRE_SUBDIRS = ('logs', 'inboxes', 'blackboard', 'state', 'outboxes')
# The tripwire only reads files modified within this margin before the suite
# started — the run sentinel is a fresh uuid minted at run start, so only files
# the run could have written can hold it. The margin absorbs filesystem mtime
# granularity and minor clock skew. Bounds the scan to a handful of fresh files
# instead of reading the whole ~/agents tree (~14k files / ~140MB) every run.
_TRIPWIRE_MTIME_MARGIN_S = 5.0

_WALL_UNAVAILABLE_WARNING = (
    'test_regression_check: WARNING — droplet hard wall unavailable: bwrap '
    'missing and unprivileged userns disabled; relying on Layers A+B + gate '
    'cred-strip. Install bubblewrap (sudo apt-get install bubblewrap) to arm '
    'the per-process read-only bind around the real ~/agents + ~/agent-worktrees.'
)

EXIT_PASS = 0
EXIT_BLOCK = 1
EXIT_ANALYSIS_FAIL = 2

# CI-delegated gate (JS/TS repos). This gate is Python/unittest-only and cannot
# run vitest on the droplet (a fresh sandboxed worktree has no node_modules and
# the npm cache is read-only under the isolation wall → EROFS). So for a repo
# whose tests run via npm — detected by a package.json `scripts.test` with no
# python `scripts/tests` discovery dir — we do NOT run the suite here. Instead we
# consume the repo's own GitHub Actions check (named CI_DELEGATED_CHECK_NAME),
# which runs `npm ci && vitest run` in a clean GitHub-hosted env. The check is
# absolute (green-at-head) rather than parent-vs-head; that subsumes
# no-new-regressions as long as main stays green, which the same workflow
# enforces on push:main. Keep the name in sync with the dashboard's
# .github/workflows/test.yml job name.
CI_DELEGATED_CHECK_NAME = 'vitest'
# Bounded, in-process, foreground wait for an in-flight CI check to conclude
# (the gate is usually invoked well after CI has finished, so this rarely
# sleeps). A wall-clock deadline — never a pgrep/proc poll loop (see
# _emit_foreground_warning for why those hung Mirror for ~100 min).
CI_CHECK_POLL_TIMEOUT_S = 600
CI_CHECK_POLL_INTERVAL_S = 20

# Matches unittest verbose FAIL/ERROR lines:
#   FAIL: test_method (scripts.tests.test_x.TestY)
#   ERROR: test_method (scripts.tests.test_x.TestY)
# Both shapes are treated as failures for gate purposes.
_FAILURE_LINE_RE = re.compile(
    r'^(?:FAIL|ERROR):\s+([\w_]+)\s+\(([\w\.]+)\)\s*$'
)

# Mirror of SENTINEL_PREFIX in
# scripts/tests/test_no_production_writes_runtime.py. Kept as a literal (not
# imported) so this production gate script never takes a dependency on a test
# module; the meta-test (test_gate_sandbox_env_injection.py) asserts the two
# stay in parity.
_TEST_RUN_SENTINEL_PREFIX = 'OL-TEST-RUN-SENTINEL-'

# Mirror of scripts/test_isolation_wall.WALL_ACTIVE_ENV. Set in the discover
# subprocess's env (via bwrap --setenv / unshare export) so that subprocess's
# _bootstrap sees it is ALREADY inside the wall and does NOT nest a second bwrap
# inside this one. Kept as a literal (not imported) so this production gate
# takes no import dependency on the wall module; parity is asserted by
# scripts/tests/test_gate_wall_parity.py.
_WALL_ACTIVE_ENV = 'OURLIBERTY_TEST_WALL_ACTIVE'


# Synthetic failure id for a suite that exited non-zero while printing a clean
# "Ran N tests" summary with no FAIL/ERROR lines — a session-level guard
# (the production-write tripwire's atexit os._exit, a teardown crash) that the
# line parser cannot see. Stable so the base-vs-head diff treats it like any
# other test id. The colon form cannot collide with a real dotted test id.
SUITE_EXITED_NONZERO_ID = 'scripts.tests:session-guard:suite-exited-nonzero'


# Absolute invariants (always-must-pass-at-head): test IDs that BLOCK the gate
# whenever they fail at HEAD, even if they were ALSO failing at the parent SHA —
# bypassing dial 3's pre-existing-failure tolerance for these IDs only. This
# closes the silent-accumulation hole the regression-only contract warned about:
# the bootstrap-first-import gate is a SINGLE test that fails if ANY test file
# drops its _bootstrap import. Once the first drift slipped in, that one ID
# entered the "pre-existing failures" set and every later drift was tolerated as
# not-new. The invariant set forces a BLOCK so a red defense-in-depth gate can
# never ride along at HEAD. IDs use the dotted module.Class.method form that
# parse_unittest_failures() produces.
ABSOLUTE_INVARIANT_TESTS = frozenset({
    'scripts.tests.test_bootstrap_first_import.'
    'BootstrapFirstImportTest.test_every_test_file_imports_bootstrap_first',
})


class AnalysisError(Exception):
    """Raised when the analysis itself can't complete (exit 2)."""


def resolve_sha(sha: str, repo_root: Path) -> str:
    """Return the canonical 40-char SHA, or raise AnalysisError."""
    try:
        result = subprocess.run(
            ['git', '-C', str(repo_root), 'rev-parse', '--verify', f'{sha}^{{commit}}'],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise AnalysisError(f'git rev-parse for {sha!r} failed: {exc}') from exc
    if result.returncode != 0:
        raise AnalysisError(
            f'cannot resolve SHA {sha!r}: {result.stderr.strip() or "unknown error"}'
        )
    return result.stdout.strip()


def parse_unittest_failures(output: str) -> set[str]:
    """Extract test IDs from unittest -v output.

    A test ID is the dotted form ``module.ClassName.test_method``, which
    matches what ``python3 -m unittest <id>`` accepts as a target.
    """
    failures: set[str] = set()
    for line in output.splitlines():
        m = _FAILURE_LINE_RE.match(line.strip())
        if m:
            method, fqclass = m.group(1), m.group(2)
            failures.add(f'{fqclass}.{method}')
    return failures


def build_sandbox_env(
    isolated_agents_root: Optional[Path] = None,
    base_env: Optional[dict] = None,
) -> dict:
    """Return a copy of ``base_env`` with the test-sandbox env vars that
    ``scripts/tests/__init__.py`` would set were it executed.

    Why this exists (the #412/#428 dead-code bug): the gate invokes
    ``python3 -m unittest discover -s scripts/tests``. unittest discover imports
    each test module as a TOP-LEVEL module (top_level_dir defaults to the start
    dir) and NEVER executes the ``scripts/tests`` package ``__init__.py``. So
    everything __init__ arms — the #412 OURLIBERTY_*_ROOT / LOG_DIR sandbox and
    the #428 DISABLE_LIVE_EMIT guard + run sentinel — is dead code under the
    production gate invocation; a test that writes through a production helper
    before any test happens to ``import scripts.tests`` hits the REAL ~/agents
    tree / live chain_events ledger. Env vars are process-wide and immune to
    import semantics, so we reproduce __init__'s vars here and inject them into
    the discover subprocess instead of relying on __init__ running.

    ``isolated_agents_root`` (when provided by ``collect_failures_at_sha``)
    doubles as the sandbox root so the per-SHA OURLIBERTY_AGENTS_ROOT redirect
    and the log/worktrees sandbox all share one tmp tree; when omitted a fresh
    one is minted.
    """
    env = dict(os.environ if base_env is None else base_env)

    # Subtractive env (item 2 — M4/H12): drop live credential families so the
    # gate never pipes them into the test subprocess. Prefix-match the families,
    # exact-match the standalone token vars.
    for key in list(env):
        if key in _STRIP_ENV_EXACT or any(
            key.startswith(prefix) for prefix in _STRIP_ENV_PREFIXES
        ):
            del env[key]
    # Pin external-service API URLs to a dead port so an escaped POST refuses.
    for key in _DEAD_API_URL_VARS:
        env[key] = _DEAD_API_URL

    if isolated_agents_root is None:
        sandbox_root = Path(tempfile.mkdtemp(prefix='ourliberty-gate-sandbox-'))
    else:
        sandbox_root = Path(isolated_agents_root)

    log_dir = sandbox_root / 'logs'
    worktrees_root = sandbox_root / 'worktrees'
    log_dir.mkdir(parents=True, exist_ok=True)
    worktrees_root.mkdir(parents=True, exist_ok=True)

    env['OURLIBERTY_AGENTS_ROOT'] = str(sandbox_root)
    # Opt out of the bootstrap/conftest force-override: this sandbox root is a
    # coordinated tree (AGENTS_ROOT/LOG_DIR/WORKTREES all under it); a fresh
    # mkdtemp override would split-brain it. See _bootstrap.py force-override.
    env['OURLIBERTY_ALLOW_LIVE_AGENTS_ROOT'] = '1'
    env['OURLIBERTY_WORKTREES_ROOT'] = str(worktrees_root)
    env['OURLIBERTY_LOG_DIR'] = str(log_dir)
    env['OURLIBERTY_DISABLE_LIVE_EMIT'] = '1'
    # Defense-in-depth: route the regression-baseline cache into the sandbox so
    # the jailed suite-under-test can never read or write the gate parent's (or
    # the real tree's) baseline cache. The PARENT process writes/reads the real
    # cache off its own env before this sandbox env is ever built for a subprocess.
    env['OL_REGRESSION_BASELINE_DIR'] = str(sandbox_root / 'regression-baselines')
    env['OURLIBERTY_TEST_RUN_SENTINEL'] = (
        _TEST_RUN_SENTINEL_PREFIX + uuid.uuid4().hex
    )
    return env


def _probe(cmd: list[str]) -> bool:
    """Run ``cmd`` and return True iff it exits 0. Never raises — a probe that
    can't run is treated as "primitive unavailable" (fall through)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def _unshare_wall_prefix(ro_targets: list[Path]) -> list[str]:
    """Build an ``unshare`` argv prefix that enters a user+mount namespace,
    bind-then-remount-ro each real tree, then execs the wrapped command.

    Kept for kernels where unprivileged userns is enabled; on TODAY's droplet
    the probe fails (userns disabled) so this path is never taken."""
    mount_steps = []
    for target in ro_targets:
        quoted = shlex.quote(str(target))
        mount_steps.append(f'mount --bind {quoted} {quoted}')
        mount_steps.append(f'mount -o remount,ro,bind {quoted} {quoted}')
    _body = (' && '.join(mount_steps) + ' && exec "$@"') if mount_steps else 'exec "$@"'
    # Signal the wrapped test process it is already walled (no nested bwrap).
    inner = 'export ' + _WALL_ACTIVE_ENV + '=1; ' + _body
    return ['unshare', '--user', '--map-root-user', '--mount',
            'sh', '-c', inner, 'sh']


def _discover_wall_prefix(workdir: Path) -> list[str]:
    """Return an argv prefix that wraps the discover subprocess so the REAL
    ~/agents + ~/agent-worktrees are READ-ONLY for that process (item 3).

    Capability-detecting best-effort, tried in order: (a) bwrap, (b) unshare
    user+mount namespace, (c) fall through — log a loud warning and return []
    (run UNWALLED). The wall is additive defense-in-depth #4; it must NEVER
    block a PR because the sandbox primitive is absent, so every failure path
    degrades to () rather than raising.
    """
    ro_targets = [p for p in (REAL_AGENTS, REAL_WORKTREES) if p.exists()]

    # (a) bubblewrap: --dev-bind / / keeps the whole fs read-write (TMPDIR, the
    # tmp worktree, the parent .git, bytecode, python3/git/bash exec all work),
    # then the two --ro-bind overlays wall only the two real trees.
    bwrap = shutil.which('bwrap')
    if bwrap:
        bwrap_prefix = [bwrap, '--dev-bind', '/', '/', '--chdir', str(workdir)]
        for target in ro_targets:
            bwrap_prefix += ['--ro-bind', str(target), str(target)]
        # Signal the discover subprocess's _bootstrap that it is already walled
        # (prevents a nested bwrap inside this one).
        bwrap_prefix += ['--setenv', _WALL_ACTIVE_ENV, '1']
        if _probe(bwrap_prefix + ['--', '/bin/true']):
            return bwrap_prefix + ['--']

    # (b) unshare user+mount namespace (probe runs the SAME bind+remount-ro on
    # the real targets, so a passing probe guarantees the real mounts succeed —
    # protecting the green path from a half-built wall).
    unshare_prefix = _unshare_wall_prefix(ro_targets)
    if _probe(unshare_prefix + ['/bin/true']):
        return unshare_prefix

    # (c) no namespace primitive (today's droplet; always macOS): warn + unwalled.
    # Suppress the warning when THIS process is ALREADY inside the wall (e.g. the
    # gate-parity meta-test calls this from within the walled suite, where a
    # nested bwrap can't be created): bwrap isn't unavailable there, it just
    # can't nest, so the "install bubblewrap" message would be misleading noise.
    if os.environ.get(_WALL_ACTIVE_ENV) != '1':
        print(_WALL_UNAVAILABLE_WARNING, file=sys.stderr)
    return []


def run_tests_in_dir(
    workdir: Path,
    timeout_s: int,
    isolated_agents_root: Optional[Path] = None,
    env: Optional[dict] = None,
) -> set[str]:
    """Invoke the unittest suite inside ``workdir`` and return the failure set.

    Raises AnalysisError on timeout or if the runner exits in a way that
    indicates it didn't complete (negative return code, no recognizable
    output). A non-zero exit code with parseable FAIL/ERROR lines is
    NOT an error — that's just "tests failed," which is the signal we want.

    The subprocess env is built by ``build_sandbox_env`` so the #412 sandbox
    and #428 live-emit guard engage even though ``unittest discover`` never runs
    ``scripts/tests/__init__.py``. ``collect_failures_at_sha`` builds the env
    itself (so it can surface the run's sentinel for the outside-jail tripwire)
    and passes it in via ``env``; when omitted a fresh one is minted here.

    The discover subprocess is optionally wrapped by ``_discover_wall_prefix``
    (item 3) so an escaped write to the REAL agents trees becomes a loud EROFS
    failure; the wall degrades to a warning + unwalled run when no namespace
    primitive is available, so it never blocks a PR.
    """
    if env is None:
        env = build_sandbox_env(isolated_agents_root)
    wall_prefix = _discover_wall_prefix(workdir)
    discover_cmd = ['python3', '-m', 'unittest', 'discover',
                    '-s', 'scripts/tests', '-v']
    try:
        result = subprocess.run(
            wall_prefix + discover_cmd,
            cwd=str(workdir),
            capture_output=True, text=True, timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError(
            f'test suite timed out after {timeout_s}s in {workdir}'
        ) from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise AnalysisError(f'failed to invoke test runner in {workdir}: {exc}') from exc

    # unittest writes its summary to stderr; combine for parsing.
    combined = (result.stdout or '') + '\n' + (result.stderr or '')
    if result.returncode < 0:
        raise AnalysisError(
            f'test runner was killed by signal {-result.returncode} in {workdir}'
        )
    # Sanity-check that the runner actually ran. unittest verbose always
    # prints a summary like "Ran N tests in Xs" — if that's missing AND
    # there are no FAIL/ERROR lines, the runner aborted before reporting.
    failures = parse_unittest_failures(combined)
    if not failures and 'Ran ' not in combined:
        raise AnalysisError(
            f'test runner output looks malformed (no "Ran N tests" summary, '
            f'no FAIL/ERROR lines) in {workdir}; exit={result.returncode}'
        )
    # Non-zero exit + a clean "Ran N tests" summary + NO FAIL/ERROR lines means
    # the assertions all passed but a SESSION-LEVEL guard failed the process
    # after reporting — e.g. the production-write tripwire's atexit os._exit(1)
    # (scripts/tests/__init__.py), or a sys.exit/crash in teardown. The
    # FAIL/ERROR-line parser above cannot see it, so without this the non-zero
    # exit is silently swallowed and such a guard could never block a PR. Surface
    # it as a synthetic failure with a STABLE id so the base-vs-head diff blocks a
    # PR that NEWLY trips it (present at head, absent at parent) while tolerating
    # one already failing at parent — same regress-on-new-only contract as a real
    # test. Ordered after the malformed-output guard so a truly aborted run still
    # raises AnalysisError rather than masquerading as this clean-exit case.
    if result.returncode != 0 and not failures:
        failures.add(SUITE_EXITED_NONZERO_ID)
    return failures


def add_worktree(repo_root: Path, sha: str, dest: Path) -> None:
    try:
        result = subprocess.run(
            ['git', '-C', str(repo_root), 'worktree', 'add', '--detach',
             str(dest), sha],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise AnalysisError(f'git worktree add failed: {exc}') from exc
    if result.returncode != 0:
        raise AnalysisError(
            f'git worktree add for {sha!r} failed: '
            f'{result.stderr.strip() or "unknown error"}'
        )


def remove_worktree(repo_root: Path, dest: Path) -> None:
    """Best-effort cleanup. Never raises — cleanup must not mask a real error.

    Uses DOUBLE ``--force``. During ``git worktree add`` git writes a ``locked``
    file with reason 'initializing' and removes it only on successful completion;
    a single ``--force`` REFUSES a locked worktree, so on any path where the lock
    survived (a SIGKILL mid-init, or even a slow-completing add) the single-force
    remove silently no-ops and the worktree metadata leaks. ``--force --force``
    overrides the lock. The orphan reaper in cleanup_stale_worktrees.py is the
    backstop for the kill-mid-init case where this teardown never runs at all.
    """
    try:
        subprocess.run(
            ['git', '-C', str(repo_root), 'worktree', 'remove',
             '--force', '--force', str(dest)],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def scan_real_tree_for_sentinel(sentinel: str, since_mtime: float = 0.0) -> None:
    """Outside-jail tripwire (item 4 — H10). Scan the REAL agents tree for any
    file containing THIS run's sentinel and raise AnalysisError (exit 2) on a
    hit — a sentinel-bearing file in the real tree means a test process escaped
    every isolation layer and wrote production, which is a legitimate block.

    Runs in the gate's PARENT process (real HOME), so unlike the #428 runtime
    tripwire — which under PR-1's per-process HOME swap scans the JAIL home and
    is vacuously green forever — this sees the real tree. Scans absolute real
    paths from ``REAL_AGENTS`` (a module global so meta-tests can monkeypatch
    it to a tmp tree).

    ``since_mtime`` bounds the read to files modified at/after it. The run
    sentinel is a fresh uuid minted at run start, so a real-tree file written by
    THIS run necessarily has mtime >= run start; pre-existing residue (which by
    construction cannot contain this run's sentinel) is skipped without being
    read. The gate passes ``run-start − _TRIPWIRE_MTIME_MARGIN_S``; the default
    0.0 reads every file (used by the direct meta-tests). This keeps the scan to
    the handful of files the run actually touched instead of reading the whole
    ~/agents tree (~14k files / ~140MB on the droplet) on every gate run.

    Fail-open on scan-infra errors (missing dir, permission): a scan that can't
    complete logs a warning and does NOT block; only an actual sentinel hit
    blocks.
    """
    needle = sentinel.encode()
    for sub in _TRIPWIRE_SUBDIRS:
        root = REAL_AGENTS / sub
        if not root.exists():
            continue
        try:
            for path in root.rglob('*'):
                if not path.is_file():
                    continue
                try:
                    # Skip pre-existing residue: it can't hold this run's unique
                    # sentinel, so reading it is pure waste (the ~140MB win).
                    if path.stat().st_mtime < since_mtime:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                if needle in data:
                    raise AnalysisError(
                        f'PRODUCTION LEAK DETECTED: test-run sentinel {sentinel!r} '
                        f'found in real-tree file {path} — a test process wrote the '
                        f'real {REAL_AGENTS} tree despite every isolation layer. '
                        f'Failing the gate (exit 2); this is a confirmed leak.'
                    )
        except AnalysisError:
            raise
        except OSError as exc:
            print(
                f'test_regression_check: WARNING — outside-jail tripwire scan of '
                f'{root} failed ({exc}); skipping (fail-open on scan infra error).',
                file=sys.stderr,
            )


def collect_failures_at_sha(
    sha: str,
    repo_root: Path,
    timeout_s: int,
    tmp_parent: Path,
) -> set[str]:
    """Run the suite at ``sha`` and return the failing-test-id set.

    Always (item 1 — M2/M3) materializes a disposable detached worktree at
    ``sha`` inside ``tmp_parent`` and runs there — never in the live checkout,
    so cwd-relative test residue can't be auto-committed to origin/main by the
    pulse/sync timers and a mid-run ``checkout main``/ff-pull can't swap the
    code under the suite. ``OURLIBERTY_AGENTS_ROOT`` is redirected to a per-SHA
    tmp directory so tests that touch agents-state paths don't pollute prod.

    The sandbox env is built HERE (not inside run_tests_in_dir) so this parent
    process knows the run's exact sentinel and can scan the REAL tree for it
    after the suite (item 4 — the outside-jail tripwire).
    """
    isolated_root = tmp_parent / f'agents-root-{sha[:12]}'
    for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
        (isolated_root / sub).mkdir(parents=True, exist_ok=True)

    env = build_sandbox_env(isolated_root)
    sentinel = env['OURLIBERTY_TEST_RUN_SENTINEL']

    # Worktree dir prefixed ``gate-wt-`` and kept NESTED under the
    # ``test-regression-check-`` mkdtemp parent (item 5 — M9). Absolute path is
    # /tmp/test-regression-check-XXXX/gate-wt-<sha>, which can never start with
    # /tmp/wt-main- — the cwd-prefix the zombie/wedged-session healers SIGKILL.
    worktree_path = tmp_parent / f'gate-wt-{sha[:12]}'
    add_worktree(repo_root, sha, worktree_path)
    # Captured just before the suite runs so the outside-jail tripwire only
    # reads files the run could have written (minus the granularity margin).
    run_start = time.time()
    try:
        failures = run_tests_in_dir(
            worktree_path, timeout_s, isolated_root, env=env,
        )
    finally:
        remove_worktree(repo_root, worktree_path)

    scan_real_tree_for_sentinel(
        sentinel, since_mtime=run_start - _TRIPWIRE_MTIME_MARGIN_S,
    )
    return failures


# ---- flake re-verification: a would-be BLOCK against a CACHED parent baseline --
# ---- is re-checked against a freshly re-run parent before it is trusted. -------
#
# WHY: the parent baseline cache (regression_baseline_cache) freezes the parent
# failure-set. The ORIGINAL two-run gate ran BOTH SHAs fresh in ONE invocation,
# so a deterministic full-suite ORDERING/POLLUTION flake (a test that fails only
# after some earlier test pollutes shared state) failed at BOTH SHAs → landed in
# the tolerated intersection. The two fresh runs canceled it. Against a FROZEN
# cache that symmetry is gone: the same flake at head, absent from the cached
# parent set, looks like a brand-new regression → a false BLOCK on a clean PR
# (every escalation in the 2026-07-08 batch named `cache_hit=true`).
#
# The fix restores that symmetry ON DEMAND: when a CACHED baseline would BLOCK,
# re-run the PARENT full suite fresh and recompute the verdict against it. A
# deterministic full-suite flake fails in the fresh parent run too (same suite,
# same ordering) → back in the intersection → tolerated. A GENUINE regression
# still fails at head but NOT at the fresh parent → still BLOCKs. This is exactly
# the un-cached two-run comparison, paid ONLY on a would-be BLOCK (rare), so the
# green path and retries keep the cache speed. Chosen over re-running the
# candidates in ISOLATION because isolation cannot distinguish a pre-existing
# flake (passes alone) from a diff that INTRODUCES full-suite pollution (also
# passes alone) — the full-suite parent comparison can, so it never false-PASSes.


def reverify_verdict_with_fresh_parent(
    head_failures: set[str],
    cached_parent_failures: set[str],
    fresh_parent_failures: set[str],
) -> tuple[dict, list[str]]:
    """Recompute the verdict against a freshly re-run parent baseline.

    Returns ``(verdict_block, reverified_flakes)`` where ``reverified_flakes`` is
    the candidate regressions (head − cached_parent) that VANISH once the parent
    is honest (head − fresh_parent) — i.e. failures present in BOTH the fresh
    parent and head runs that the stale cache had missed, so they are full-suite
    flakes / pre-existing, not this PR's regressions. The verdict block is
    computed purely from the fresh parent, so a genuine head-only regression
    still BLOCKs.
    """
    cached_regressions = set(head_failures) - set(cached_parent_failures)
    fresh_regressions = set(head_failures) - set(fresh_parent_failures)
    reverified_flakes = sorted(cached_regressions - fresh_regressions)
    return compute_verdict(fresh_parent_failures, head_failures), reverified_flakes


# ---- diff-scoped skip: a non-code change cannot introduce a test regression --
#
# WHY: the gate runs the FULL suite at parent+head for EVERY review — even a
# pure-docs PR that changes zero importable code and zero file any test reads.
# That's wasted work that thrashes the bounded-step ceiling (the #770-#773 docs/
# spec PRs each burned a full double-run for nothing). If every changed file is
# provably unable to affect a test outcome, the head failure-set EQUALS the
# parent set, so the verdict is trivially PASS and we skip both suite runs.
#
# SAFETY — the inert set is tightly scoped by an empirical audit of what the
# suite actually reads off the real repo tree (REPO_ROOT-anchored reads + globs):
# the only real-tree inputs are scripts/, config/, agents/, runbooks/, systemd/.
# `docs/**` has ZERO real-tree reads (the docs paths that appear in tests are all
# fixture/mock string data, e.g. a changed_files list). The ONE carve-out is
# `docs/runbooks/**`: a config-validation test could assert a runbook_path
# exists, and that read is config-derived (not a literal the audit grep catches),
# so we treat it as non-inert out of caution. Net inert set = `docs/` minus
# `docs/runbooks/`. Conservative by construction: anything NOT proven inert runs
# the full gate. Guarded by DiffInertSkipTest + a comment pointer so the audit
# basis is re-checkable. Escape hatch: --no-skip-inert forces the full run.

_INERT_PREFIX = 'docs/'
# Subtrees UNDER docs/ that a test may existence-check via a config-derived path
# (so the read-audit grep can't see them) — treated as non-inert.
_NON_INERT_UNDER_DOCS = ('docs/runbooks/',)


def _is_test_inert_path(path: str) -> bool:
    """True iff a change to ``path`` cannot alter any test outcome (pure docs,
    excluding the existence-validated docs/runbooks/ subtree)."""
    if not path.startswith(_INERT_PREFIX):
        return False
    return not any(path.startswith(p) for p in _NON_INERT_UNDER_DOCS)


def diff_changed_files(
    parent: str, head: str, repo_root: Path,
) -> Optional[list[str]]:
    """Paths changed between ``parent`` and ``head``, or None on any git failure.

    None is the FAIL-SAFE signal: the caller then runs the full gate (never skips
    on uncertainty). Uses the two-dot range so only commits reachable from head
    but not parent are diffed — the PR's own changes.
    """
    try:
        proc = subprocess.run(
            ['git', '-C', str(repo_root), 'diff', '--name-only',
             f'{parent}..{head}'],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def diff_is_test_inert(changed_files: list[str]) -> bool:
    """True iff EVERY changed file is test-inert (so the suite can be skipped).

    An EMPTY diff returns False — there is nothing to safely skip on (and an
    empty parent..head usually means a degenerate/identical pair we'd rather run
    the gate on than silently pass)."""
    return bool(changed_files) and all(
        _is_test_inert_path(p) for p in changed_files
    )


def compute_verdict(parent: set[str], head: set[str]) -> dict:
    regressions = sorted(head - parent)
    fixed = sorted(parent - head)
    pre_existing_unaffected = sorted(parent & head)
    # Absolute invariants block on presence at HEAD regardless of parent state —
    # they bypass the pre-existing tolerance so a defense-in-depth gate that was
    # already red can't keep riding along while new drift accumulates behind it.
    invariant_failures = sorted(head & ABSOLUTE_INVARIANT_TESTS)
    verdict = 'BLOCK' if (regressions or invariant_failures) else 'PASS'

    parts: list[str] = []
    if regressions:
        parts.append(f'{len(regressions)} new failure(s) introduced by this PR')
    else:
        parts.append('no new failures introduced by this PR')
    if invariant_failures:
        parts.append(
            f'{len(invariant_failures)} absolute-invariant test(s) failing at '
            'head (always blocks, even if pre-existing)'
        )
    if pre_existing_unaffected:
        parts.append(
            f'{len(pre_existing_unaffected)} pre-existing failure(s) untouched'
        )
    if fixed:
        parts.append(f'{len(fixed)} previously-failing test(s) fixed')
    summary = '; '.join(parts) + '.'

    return {
        'regressions': regressions,
        'fixed': fixed,
        'pre_existing_unaffected': pre_existing_unaffected,
        'invariant_failures': invariant_failures,
        'verdict': verdict,
        'summary': summary,
    }


def render_text(report: dict) -> str:
    lines = [
        f'parent_sha: {report["parent_sha"]}',
        f'head_sha:   {report["head_sha"]}',
        f'verdict:    {report["verdict"]}',
        f'summary:    {report["summary"]}',
        '',
    ]
    def _block(label: str, items: list[str]) -> None:
        lines.append(f'{label} ({len(items)}):')
        for item in items:
            lines.append(f'  - {item}')
        if not items:
            lines.append('  (none)')
        lines.append('')
    _block('regressions', report['regressions'])
    _block('reverified_flakes', report.get('reverified_flakes', []))
    _block('invariant_failures', report['invariant_failures'])
    _block('pre_existing_unaffected', report['pre_existing_unaffected'])
    _block('fixed', report['fixed'])
    return '\n'.join(lines).rstrip() + '\n'


def is_ci_delegated_repo(repo_root: Path) -> bool:
    """True iff `repo_root` is a JS/TS repo whose tests are delegated to CI.

    Signal: a package.json that defines a `scripts.test`, AND the absence of the
    python unittest discovery dir (scripts/tests). The second clause keeps
    agent-core (which has scripts/tests and no package.json) firmly on the
    unittest path, and defends against a hybrid repo that carries a package.json
    but is really python-gated. A missing or malformed package.json is treated as
    not-delegated (fall through to the unittest gate) — fail-safe.
    """
    pkg = repo_root / 'package.json'
    if not pkg.is_file():
        return False
    if (repo_root / 'scripts' / 'tests').is_dir():
        return False
    try:
        data = json.loads(pkg.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    if not (isinstance(data, dict) and isinstance(data.get('scripts'), dict)):
        return False
    test_script = data['scripts'].get('test')
    # Only delegate for repos whose test runner is vitest — the check we consume
    # is named CI_DELEGATED_CHECK_NAME. A JS/polyglot repo with a non-vitest test
    # script (jest, a python service that also ships a JS tooling package.json,
    # …) must NOT be routed here: it would consult a 'vitest' check that never
    # exists and, worse, skip its real suite. Ties detection to the runner whose
    # check this gate reads.
    return isinstance(test_script, str) and 'vitest' in test_script


def _repo_slug(repo_root: Path) -> Optional[str]:
    """`owner/name` parsed from the origin remote; None on any failure."""
    try:
        url = subprocess.run(
            ['git', '-C', str(repo_root), 'remote', 'get-url', 'origin'],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    # git@github.com:owner/repo.git | https://github.com/owner/repo(.git)
    match = re.search(r'[:/]([^/:]+/[^/]+?)(?:\.git)?/?$', url)
    return match.group(1) if match else None


def _fetch_ci_check(slug: str, sha: str) -> Optional[dict]:
    """Return the newest check-run named CI_DELEGATED_CHECK_NAME for `sha`.

    None if the check is absent for that SHA or the `gh` call fails (both are
    handled by the caller as "cannot conclude" — a poll, then ANALYSIS_FAIL,
    never PASS). A re-run creates a fresh check-run for the same SHA, so the
    newest one wins.

    Reads the SHA-keyed check-runs REST endpoint rather than the PR-centric
    `statusCheckRollup` used by heal_pr_auto_merge / heal_pipeline_stall,
    because this gate's contract is SHA-based (--head-sha) and it is not always
    invoked with a PR number in hand. The vitest check is a GitHub Actions
    check-run (not a legacy commit status), so this endpoint sees it.
    """
    try:
        out = subprocess.run(
            ['gh', 'api', f'repos/{slug}/commits/{sha}/check-runs',
             '--paginate', '-q', '.check_runs[]'],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        # Distinguish a gh/infra failure from a genuinely-absent check so the
        # caller (and any human it re-pages) can tell flake from a missing
        # workflow. Both still fail safe — this only sharpens the diagnosis.
        detail = (getattr(exc, 'stderr', '') or '').strip().splitlines()
        print(
            f'test_regression_check: gh check-runs lookup failed for '
            f'{slug}@{sha[:12]}: {type(exc).__name__}'
            f'{" — " + detail[-1] if detail else ""}',
            file=sys.stderr,
        )
        return None
    runs: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get('name') != CI_DELEGATED_CHECK_NAME:
            continue
        # Defense-in-depth: trust only a check-run produced by GitHub Actions,
        # not an arbitrary third-party app that posted a same-named check. (A
        # same-repo workflow could still post one; the PR diff Mirror reviews is
        # the backstop against a maliciously-added workflow.)
        app_slug = (obj.get('app') or {}).get('slug')
        if app_slug not in (None, 'github-actions'):
            continue
        runs.append(obj)
    if not runs:
        return None
    # Newest re-run wins. Order by check-run id — monotonically increasing and
    # always present — NOT started_at, which can be null on a run and let a
    # stale green mask a newer red.
    runs.sort(key=lambda r: r.get('id') or 0, reverse=True)
    return runs[0]


def run_ci_delegated_gate(
    parent_sha: str,
    head_sha: str,
    repo_root: Path,
    output: str = 'json',
) -> int:
    """Consume the repo's GitHub `vitest` check instead of running the suite.

    Returns EXIT_PASS when the check is green, EXIT_BLOCK when it concluded
    not-green, and EXIT_ANALYSIS_FAIL when it cannot be concluded (no slug, the
    check never appears, or it is still running past CI_CHECK_POLL_TIMEOUT_S).
    Analysis-fail is the honest "gate could not conclude" signal — never a silent
    PASS (which would defeat the gate) nor a false BLOCK of correct code.
    """
    slug = _repo_slug(repo_root)
    if slug is None:
        print(
            'test_regression_check: ci-delegated gate could not determine the '
            'GitHub repo slug from the origin remote — cannot read the '
            f'{CI_DELEGATED_CHECK_NAME!r} check',
            file=sys.stderr,
        )
        return EXIT_ANALYSIS_FAIL

    deadline = time.time() + CI_CHECK_POLL_TIMEOUT_S
    check = _fetch_ci_check(slug, head_sha)
    while (check is None or check.get('status') != 'completed') and \
            time.time() < deadline:
        print(
            f'test_regression_check: ci-delegated gate waiting for '
            f'{CI_DELEGATED_CHECK_NAME!r} on {head_sha[:12]} '
            f'(status={(check or {}).get("status") or "absent"}) — '
            f'polling every {CI_CHECK_POLL_INTERVAL_S}s',
            file=sys.stderr,
        )
        time.sleep(CI_CHECK_POLL_INTERVAL_S)
        check = _fetch_ci_check(slug, head_sha)

    if check is None:
        print(
            f'test_regression_check: ci-delegated gate could not conclude — no '
            f'{CI_DELEGATED_CHECK_NAME!r} check found for {head_sha[:12]} on '
            f'{slug} within {CI_CHECK_POLL_TIMEOUT_S}s '
            '(is .github/workflows/test.yml present and triggering?)',
            file=sys.stderr,
        )
        return EXIT_ANALYSIS_FAIL
    if check.get('status') != 'completed':
        print(
            f'test_regression_check: ci-delegated gate could not conclude — '
            f'{CI_DELEGATED_CHECK_NAME!r} still {check.get("status")} for '
            f'{head_sha[:12]} after {CI_CHECK_POLL_TIMEOUT_S}s',
            file=sys.stderr,
        )
        return EXIT_ANALYSIS_FAIL

    conclusion = (check.get('conclusion') or '').lower()
    # Only an actual green run PASSes. 'skipped'/'neutral' mean the tests did not
    # assert green (a path-filtered or conditionally-skipped job, a neutral app
    # conclusion) — that is "cannot conclude", never a silent PASS. Everything
    # else on a completed run (failure/timed_out/cancelled/…) BLOCKs.
    if conclusion in ('skipped', 'neutral'):
        print(
            f'test_regression_check: ci-delegated gate could not conclude — '
            f'{CI_DELEGATED_CHECK_NAME!r} concluded {conclusion!r} (tests did '
            f'not run green) for {head_sha[:12]}',
            file=sys.stderr,
        )
        return EXIT_ANALYSIS_FAIL
    passing = conclusion == 'success'
    verdict = 'PASS' if passing else 'BLOCK'
    report = {
        'parent_sha': parent_sha,
        'head_sha': head_sha,
        'gate_mode': 'ci-delegated',
        'ci_check_name': CI_DELEGATED_CHECK_NAME,
        'ci_check_conclusion': conclusion or None,
        'ci_check_url': check.get('html_url'),
        'regressions': [] if passing
        else [f'ci:{CI_DELEGATED_CHECK_NAME}={conclusion or "unknown"}'],
        'fixed': [],
        'pre_existing_unaffected': [],
        'invariant_failures': [],
        'verdict': verdict,
        'summary': (
            f'CI-delegated gate: GitHub {CI_DELEGATED_CHECK_NAME!r} check '
            f'concluded {conclusion or "unknown"} — '
            + ('green, no regression.' if passing else 'not green, blocking.')
        ),
    }
    if output == 'json':
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report), end='')
    return EXIT_PASS if passing else EXIT_BLOCK


def _emit_foreground_warning() -> None:
    """Print a foreground-only-invocation warning to stderr at startup.

    chain-discipline-marker-parser-and-regression-check-001 (2026-05-25): on
    PR #101 Mirror invented a backgrounded `& ... until ! kill -0 $(pgrep -f
    test_regression_check.py | head -1) ...` poll loop AFTER emitting
    REVIEW_PASS. `pgrep -f` self-matched the loop's own argv, `kill -0`
    always succeeded, the loop never exited, and Mirror's session stayed
    alive 71 min until manually killed. Auto-merge then never fired because
    a post-marker assistant turn ("Acknowledged — moot now") masked her
    REVIEW_PASS from final-turn parsing. The parser bug is now fixed at the
    notifier layer (always-scan-latest-wins across all assistant turns); the
    warning here closes the other side of the loop by surfacing the
    foreground requirement on every invocation.

    PR #334 (2026-06-05): the "fix" for the self-match — the bracket trick
    `pgrep -f '[t]est_regression_check.py'` — spawned a NEW wedge:
    `until [ ! -d /proc/$(pgrep ... | head -1) ]`. Once this script finished,
    pgrep returned empty, `/proc/$()` collapsed to `/proc/` (always a dir),
    the `until` never exited, and Mirror hung 102 min blocking inbox-watcher.
    Same root cause: liveness re-derived each iteration + no timeout. The safe
    primitive for any unavoidable PID wait is scripts/wait_for_pid.sh.
    """
    print(
        'WARNING: test_regression_check.py must be run FOREGROUND.\n'
        '  Do not background it (shell & OR the Bash tool background mode) and\n'
        '  poll for completion. Poll loops have hung Mirror reviews: `pgrep`\n'
        '  self-match (PR #101, 71 min), empty pgrep -> /proc/ always-a-dir\n'
        '  (PR #334, 102 min), and a content sentinel that never arrived\n'
        '  (PR #717/#720, ~85-100 min). The script has no completion flag file;\n'
        '  the only completion signal is the exit code returned synchronously.\n'
        '  To bound the runtime, wrap it foreground in scripts/run_review_step.sh\n'
        '  (--timeout N; exits 124 + a REVIEW_STEP_TIMED_OUT banner on timeout).\n'
        '  Only if a process is ALREADY backgrounded with a shell & and cannot\n'
        '  run foreground, use scripts/wait_for_pid.sh (captures the PID once,\n'
        '  gates on `kill -0`, has a wall-clock timeout).',
        file=sys.stderr,
    )


def main(argv: Optional[list[str]] = None) -> int:
    _emit_foreground_warning()
    parser = argparse.ArgumentParser(
        prog='test_regression_check',
        description='Compare failing-test sets between two SHAs and emit a regression verdict.',
    )
    parser.add_argument('--parent-sha', required=True)
    parser.add_argument('--head-sha', required=True)
    parser.add_argument('--repo-root', default='.', help='Path to the git repo (default: cwd).')
    parser.add_argument(
        '--timeout-per-sha', type=int, default=DEFAULT_TIMEOUT_PER_SHA_S,
        help=f'Per-SHA test-suite timeout in seconds (default: {DEFAULT_TIMEOUT_PER_SHA_S}).',
    )
    parser.add_argument('--output', choices=('json', 'text'), default='json')
    parser.add_argument(
        '--no-baseline-cache', action='store_true',
        help='Force a fresh parent-SHA suite run instead of reusing the cached '
             'baseline (the default always-two-runs behavior).',
    )
    parser.add_argument(
        '--no-skip-inert', action='store_true',
        help='Force the full suite even when the diff touches only test-inert '
             'paths (docs/). The default short-circuits to PASS for such diffs.',
    )
    parser.add_argument(
        '--no-flake-reverify', action='store_true',
        help='Skip re-running the parent suite fresh before a cached-baseline '
             'BLOCK. The default re-verifies a would-be BLOCK against a freshly '
             'run parent so a full-suite flake the cache missed cannot false-BLOCK.',
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()

    try:
        parent_canonical = resolve_sha(args.parent_sha, repo_root)
        head_canonical = resolve_sha(args.head_sha, repo_root)
    except AnalysisError as exc:
        print(f'test_regression_check: {exc}', file=sys.stderr)
        return EXIT_ANALYSIS_FAIL

    # JS/TS repos delegate to their own GitHub Actions vitest check — the droplet
    # cannot run vitest under the isolation wall. This runs in the orchestrator
    # (before any walled subprocess), so `gh` uses the normal environment/auth.
    if is_ci_delegated_repo(repo_root):
        print(
            'test_regression_check: repo is JS/TS (package.json test script, no '
            'scripts/tests) — delegating to the GitHub '
            f'{CI_DELEGATED_CHECK_NAME!r} check instead of running unittest',
            file=sys.stderr,
        )
        return run_ci_delegated_gate(
            parent_canonical, head_canonical, repo_root, args.output,
        )

    # Diff-scoped skip: if every changed file is test-inert (pure docs/, excl
    # docs/runbooks/), the head failure-set is identical to the parent's, so the
    # verdict is trivially PASS — no need to run the suite at all (the #770-#773
    # docs-PR thrash). Fail-safe: a git error (changed is None) falls through to
    # the full gate; --no-skip-inert disables the short-circuit entirely.
    if not args.no_skip_inert:
        changed = diff_changed_files(parent_canonical, head_canonical, repo_root)
        if changed is not None and diff_is_test_inert(changed):
            report = {
                'parent_sha': parent_canonical,
                'head_sha': head_canonical,
                'baseline_key': None,
                'gate_skipped': True,
                'skip_reason': (
                    'diff touches only test-inert paths (docs/, excl '
                    'docs/runbooks/); a non-code change cannot introduce a test '
                    'regression — skipped both suite runs'
                ),
                'changed_files': sorted(changed),
                'parent_failures': [],
                'head_failures': [],
                'used_cached_baseline': False,
                'parent_reverified': False,
                'parent_run_secs': None,
                'head_run_secs': None,
                'reverified_flakes': [],
                # Synthesize a clean PASS verdict block (no parent/head failures).
                **compute_verdict(set(), set()),
            }
            print(
                f'test_regression_check: GATE_SKIPPED inert diff '
                f'({len(changed)} file(s): {", ".join(sorted(changed)[:5])}'
                f'{" …" if len(changed) > 5 else ""}) — verdict PASS, no suite run',
                file=sys.stderr,
            )
            if args.output == 'json':
                print(json.dumps(report, indent=2))
            else:
                print(render_text(report), end='')
            return EXIT_PASS

    used_cached_baseline = False
    parent_run_secs: Optional[float] = None
    head_run_secs: Optional[float] = None
    # Key the parent baseline on the journal-stripped TREE content, not the raw
    # SHA. Pulse rewrites the cycle journals every ~5 min, so a SHA key made the
    # parent of nearly every review a never-warmed commit (≈0% hit rate observed)
    # — the gate then ran the full suite twice and the second run blew the 900s
    # bounded-step ceiling. The content key lets a journal-only parent reuse the
    # warmed baseline. Fall back to the SHA on a git failure (today's behavior).
    parent_key = (
        baseline_cache.content_key(parent_canonical, repo_root)
        or parent_canonical
    )
    with tempfile.TemporaryDirectory(prefix='test-regression-check-') as tmp:
        tmp_parent = Path(tmp)
        try:
            # Parent baseline: reuse the cache when present. The baseline is a
            # pure function of the parent tree, so a hit is IDENTICAL to
            # re-running it — but skips a full-suite pass, which is what keeps a
            # retried/re-reviewed gate under the bounded-step ceiling. A miss runs
            # it and warms the cache for the next attempt. Cache writes are
            # best-effort: a cache failure never fails the gate. --no-baseline-cache
            # forces the original always-two-runs behavior.
            parent_failures: Optional[set[str]] = None
            if not args.no_baseline_cache:
                cached = baseline_cache.load(parent_key)
                if cached is not None:
                    parent_failures = cached
                    used_cached_baseline = True
                    print(
                        'test_regression_check: using cached parent baseline for '
                        f'{parent_canonical[:12]} (key {parent_key[:12]}, '
                        f'{len(cached)} failing) — skipping the parent suite run',
                        file=sys.stderr,
                    )
            if parent_failures is None:
                _t0 = time.time()
                parent_failures = collect_failures_at_sha(
                    parent_canonical, repo_root, args.timeout_per_sha, tmp_parent,
                )
                parent_run_secs = round(time.time() - _t0, 1)
                if not args.no_baseline_cache:
                    try:
                        baseline_cache.store(
                            parent_key, parent_failures,
                            source_commit=parent_canonical,
                        )
                        baseline_cache.gc()  # bound growth on the live path
                    except OSError:
                        pass  # best-effort; never fail the gate on a cache write
            _t1 = time.time()
            head_failures = collect_failures_at_sha(
                head_canonical, repo_root, args.timeout_per_sha, tmp_parent,
            )
            head_run_secs = round(time.time() - _t1, 1)
        except AnalysisError as exc:
            print(f'test_regression_check: {exc}', file=sys.stderr)
            return EXIT_ANALYSIS_FAIL

    # Cost telemetry (regression-gate-cant-conclude): surface the real per-run
    # suite duration + whether the content key hit, so the cache hit-rate and the
    # single-run margin under the bounded-step ceiling are measurable from the
    # gate output instead of guessed.
    print(
        f'test_regression_check: baseline_key={parent_key[:12]} '
        f'cache_hit={used_cached_baseline} '
        f'parent_run_secs={parent_run_secs} head_run_secs={head_run_secs}',
        file=sys.stderr,
    )
    verdict_block = compute_verdict(parent_failures, head_failures)
    reverified_flakes: list[str] = []
    parent_reverified = False
    # A would-be BLOCK computed against a CACHED parent baseline is re-checked
    # against a freshly re-run parent before it is trusted (see
    # reverify_verdict_with_fresh_parent). Only fires when there ARE regressions
    # AND the baseline was cached — a non-cached run already did the honest
    # two-run comparison, and a green run has nothing to re-verify — so the cache
    # speedup is preserved everywhere except the rare would-be-BLOCK-on-cache.
    if (verdict_block['regressions'] and used_cached_baseline
            and not args.no_flake_reverify):
        try:
            with tempfile.TemporaryDirectory(
                    prefix='test-regression-reverify-') as rv:
                _t2 = time.time()
                fresh_parent_failures = collect_failures_at_sha(
                    parent_canonical, repo_root, args.timeout_per_sha, Path(rv),
                )
                parent_run_secs = round(time.time() - _t2, 1)
            parent_reverified = True
            verdict_block, reverified_flakes = reverify_verdict_with_fresh_parent(
                head_failures, parent_failures, fresh_parent_failures,
            )
            parent_failures = fresh_parent_failures
            # Warm the cache with the accurate fresh baseline so the next review
            # off this parent tree doesn't repeat the false-BLOCK dance.
            if not args.no_baseline_cache:
                try:
                    baseline_cache.store(
                        parent_key, fresh_parent_failures,
                        source_commit=parent_canonical,
                    )
                except OSError:
                    pass
            if reverified_flakes:
                print(
                    'test_regression_check: fresh-parent re-verification cleared '
                    f'{len(reverified_flakes)} candidate regression(s) — they '
                    'fail in the fresh parent run too (full-suite flake / '
                    f'pre-existing, cache-missed): {", ".join(reverified_flakes)}',
                    file=sys.stderr,
                )
            else:
                print(
                    'test_regression_check: fresh-parent re-verification '
                    'confirmed the regression(s) are genuine — still BLOCK',
                    file=sys.stderr,
                )
        except AnalysisError as exc:
            # Could not re-run the parent — keep the cached-baseline verdict
            # (fail closed to today's behavior) rather than clear anything.
            print(
                f'test_regression_check: flake re-verification could not re-run '
                f'the parent ({exc}); keeping the cached-baseline verdict '
                '(fail closed)',
                file=sys.stderr,
            )

    report = {
        'parent_sha': parent_canonical,
        'head_sha': head_canonical,
        'baseline_key': parent_key,
        'parent_failures': sorted(parent_failures),
        'head_failures': sorted(head_failures),
        'used_cached_baseline': used_cached_baseline,
        'parent_reverified': parent_reverified,
        'parent_run_secs': parent_run_secs,
        'head_run_secs': head_run_secs,
        'reverified_flakes': reverified_flakes,
        **verdict_block,
    }

    if args.output == 'json':
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report), end='')

    return EXIT_BLOCK if report['verdict'] == 'BLOCK' else EXIT_PASS


if __name__ == '__main__':
    sys.exit(main())
