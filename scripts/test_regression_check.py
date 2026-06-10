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
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))

DEFAULT_TIMEOUT_PER_SHA_S = 300
TEST_DISCOVERY_TARGET = 'scripts.tests'

EXIT_PASS = 0
EXIT_BLOCK = 1
EXIT_ANALYSIS_FAIL = 2

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


# Synthetic failure id for a suite that exited non-zero while printing a clean
# "Ran N tests" summary with no FAIL/ERROR lines — a session-level guard
# (the production-write tripwire's atexit os._exit, a teardown crash) that the
# line parser cannot see. Stable so the base-vs-head diff treats it like any
# other test id. The colon form cannot collide with a real dotted test id.
SUITE_EXITED_NONZERO_ID = 'scripts.tests:session-guard:suite-exited-nonzero'


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

    if isolated_agents_root is None:
        sandbox_root = Path(tempfile.mkdtemp(prefix='ourliberty-gate-sandbox-'))
    else:
        sandbox_root = Path(isolated_agents_root)

    log_dir = sandbox_root / 'logs'
    worktrees_root = sandbox_root / 'worktrees'
    log_dir.mkdir(parents=True, exist_ok=True)
    worktrees_root.mkdir(parents=True, exist_ok=True)

    env['OURLIBERTY_AGENTS_ROOT'] = str(sandbox_root)
    env['OURLIBERTY_WORKTREES_ROOT'] = str(worktrees_root)
    env['OURLIBERTY_LOG_DIR'] = str(log_dir)
    env['OURLIBERTY_DISABLE_LIVE_EMIT'] = '1'
    env['OURLIBERTY_TEST_RUN_SENTINEL'] = (
        _TEST_RUN_SENTINEL_PREFIX + uuid.uuid4().hex
    )
    return env


def run_tests_in_dir(
    workdir: Path,
    timeout_s: int,
    isolated_agents_root: Optional[Path] = None,
) -> set[str]:
    """Invoke the unittest suite inside ``workdir`` and return the failure set.

    Raises AnalysisError on timeout or if the runner exits in a way that
    indicates it didn't complete (negative return code, no recognizable
    output). A non-zero exit code with parseable FAIL/ERROR lines is
    NOT an error — that's just "tests failed," which is the signal we want.

    The subprocess env is built by ``build_sandbox_env`` so the #412 sandbox
    and #428 live-emit guard engage even though ``unittest discover`` never runs
    ``scripts/tests/__init__.py``.
    """
    env = build_sandbox_env(isolated_agents_root)
    try:
        result = subprocess.run(
            ['python3', '-m', 'unittest', 'discover', '-s', 'scripts/tests', '-v'],
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
    """Best-effort cleanup. Never raises — cleanup must not mask a real error."""
    try:
        subprocess.run(
            ['git', '-C', str(repo_root), 'worktree', 'remove', '--force', str(dest)],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def current_head_sha(repo_root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ['git', '-C', str(repo_root), 'rev-parse', 'HEAD'],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def collect_failures_at_sha(
    sha: str,
    repo_root: Path,
    timeout_s: int,
    tmp_parent: Path,
) -> set[str]:
    """Run the suite at ``sha`` and return the failing-test-id set.

    Optimization: if ``sha`` equals the current HEAD of ``repo_root``, run
    in place (no tmp worktree). Otherwise materialize a detached worktree
    at ``sha`` inside ``tmp_parent`` and run there.

    In both cases, ``OURLIBERTY_AGENTS_ROOT`` is redirected to a per-SHA
    tmp directory so tests that touch agents-state paths don't pollute
    prod.
    """
    isolated_root = tmp_parent / f'agents-root-{sha[:12]}'
    for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
        (isolated_root / sub).mkdir(parents=True, exist_ok=True)

    head = current_head_sha(repo_root)
    if head and head == sha:
        return run_tests_in_dir(repo_root, timeout_s, isolated_root)

    worktree_path = tmp_parent / f'wt-{sha[:12]}'
    add_worktree(repo_root, sha, worktree_path)
    try:
        return run_tests_in_dir(worktree_path, timeout_s, isolated_root)
    finally:
        remove_worktree(repo_root, worktree_path)


def compute_verdict(parent: set[str], head: set[str]) -> dict:
    regressions = sorted(head - parent)
    fixed = sorted(parent - head)
    pre_existing_unaffected = sorted(parent & head)
    verdict = 'BLOCK' if regressions else 'PASS'

    parts: list[str] = []
    if regressions:
        parts.append(f'{len(regressions)} new failure(s) introduced by this PR')
    else:
        parts.append('no new failures introduced by this PR')
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
    _block('pre_existing_unaffected', report['pre_existing_unaffected'])
    _block('fixed', report['fixed'])
    return '\n'.join(lines).rstrip() + '\n'


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
        '  Do not background with & and poll for completion.\n'
        '  Poll loops re-deriving liveness via `pgrep` (self-match) or a\n'
        '  `/proc/<pid>` path test (empty pgrep -> /proc/ is always a dir) have\n'
        '  hung Mirror reviews 71 min (PR #101) and 102 min (PR #334).\n'
        '  The script has no completion flag file; the only completion signal\n'
        '  is the exit code returned synchronously. If you ever must wait on a\n'
        '  backgrounded PID elsewhere, use scripts/wait_for_pid.sh (captures the\n'
        '  PID once, gates on `kill -0`, has a wall-clock timeout).',
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
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()

    try:
        parent_canonical = resolve_sha(args.parent_sha, repo_root)
        head_canonical = resolve_sha(args.head_sha, repo_root)
    except AnalysisError as exc:
        print(f'test_regression_check: {exc}', file=sys.stderr)
        return EXIT_ANALYSIS_FAIL

    with tempfile.TemporaryDirectory(prefix='test-regression-check-') as tmp:
        tmp_parent = Path(tmp)
        try:
            parent_failures = collect_failures_at_sha(
                parent_canonical, repo_root, args.timeout_per_sha, tmp_parent,
            )
            head_failures = collect_failures_at_sha(
                head_canonical, repo_root, args.timeout_per_sha, tmp_parent,
            )
        except AnalysisError as exc:
            print(f'test_regression_check: {exc}', file=sys.stderr)
            return EXIT_ANALYSIS_FAIL

    verdict_block = compute_verdict(parent_failures, head_failures)
    report = {
        'parent_sha': parent_canonical,
        'head_sha': head_canonical,
        'parent_failures': sorted(parent_failures),
        'head_failures': sorted(head_failures),
        **verdict_block,
    }

    if args.output == 'json':
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report), end='')

    return EXIT_BLOCK if report['verdict'] == 'BLOCK' else EXIT_PASS


if __name__ == '__main__':
    sys.exit(main())
