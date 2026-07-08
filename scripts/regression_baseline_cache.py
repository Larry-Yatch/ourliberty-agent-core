#!/usr/bin/env python3
"""regression_baseline_cache.py — per-SHA cache of the regression gate's
"failing tests at this commit" baseline.

WHY
---
The review regression gate (`scripts/test_regression_check.py`) decides
"did THIS diff introduce a new test failure?" by running the FULL suite at the
parent SHA (a baseline that filters the ~13 chronic pre-existing failures) AND
at the head SHA, then diffing the two failing-sets. Running the suite twice per
review is the dominant cost — and when a review is retried or re-reviewed
(PR #736 retried the gate 3×; #733 went to a second round), the IDENTICAL
parent-SHA baseline is recomputed every time. That repeated full-suite run is
what pushes a review past the 900s bounded-step ceiling, so the gate exits 2
(cannot-conclude) and Mirror escalates a CLEAN-code PR as a "review failure"
that a human then hand-merges.

The parent baseline is a pure function of the parent SHA (the tree is frozen at
that commit), so it is safe to compute once and reuse. This module is that
cache: keyed by the 40-char commit SHA, written atomically to the REAL agents
tree by the gate's PARENT process (never the jailed suite subprocess).

Correctness: a cache hit returns EXACTLY what re-running the same SHA would
produce — zero change to the verdict. The only theoretical drift is a flaky
test that flips between the cached run and the head run; that same flakiness
already produces false regressions in the un-cached two-run path, so caching
does not make it worse. `--no-baseline-cache` on the gate forces a fresh run.

This is the SAFE half of the gate-speedup. The further win — running only the
diff-affected tests at head (graph-aware selection) with a full-suite post-merge
backstop — is a deliberate, separately-validated follow-up (see
agents/beacon/specs/regression-gate-efficiency.md).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import atomic_io

# v2 (tree-content key, 2026-06-30): the cache key is no longer the raw commit
# SHA but a digest of the commit's TREE with the per-cycle Pulse journal paths
# removed (see content_key + _PULSE_JOURNAL_*). Bumping the schema makes every
# pre-existing v1 (SHA-keyed) entry a clean miss, so a stale SHA-keyed file can
# never be mistaken for a content-keyed one. WHY this matters: Pulse rewrites
# runbooks/cycle-journal.md + agents/pulse/MEMORY.md every ~5 min, so the parent
# SHA of almost every PR review was a brand-new commit the warmer had never seen
# — the SHA cache hit ~0% in practice (verified across every recent review), the
# gate fell back to the full two-run path, and the second run blew the 900s
# bounded-step ceiling. Keying on code-bearing tree content instead means a
# journal-only Pulse commit reuses the warmed baseline.
SCHEMA_VERSION = 2
_KEY_RE = re.compile(r'^[0-9a-f]{40}$')

# Per-cycle machine-journal paths excluded from the content key: a change to ANY
# of these cannot change a test outcome, so two commits that differ only here
# share a baseline. SOURCE OF TRUTH: PULSE_RUNTIME_PATHS in
# scripts/_lib_pulse_runtime.sh (the set Pulse auto-commits each cycle). Kept in
# lockstep by test_regression_baseline_cache.PulseDenylistSyncTest, which parses
# that shell array and fails if it drifts from this list. Verified test-safe: of
# the 10 test files that reference these paths, none assert on their churning
# CONTENT (all use tmp-dir fixtures or reference the paths as strings).
_PULSE_JOURNAL_FILES = frozenset({
    'runbooks/cycle-journal.md',
    'runbooks/cycle-actions.jsonl',
    'agents/pulse/MEMORY.md',
    'agents/pulse/.claude/settings.json',
})
_PULSE_JOURNAL_DIRS = ('agents/pulse/memory/', 'runbooks/journal-archive/')
# Keep the newest N baselines; older ones GC away (main advances, old parents
# stop being anyone's merge-base). Bounds the dir to a handful of small files.
DEFAULT_KEEP = 40

# Captured at import — mirrors test_regression_check.REAL_HOME's discipline:
# anchor the real-tree default to the process's home as of start, not a
# call-time Path.home() that an in-process HOME mutation could move out from
# under us. The OL_REGRESSION_BASELINE_DIR override (used by tests) still wins.
_DEFAULT_BASELINE_DIR = (
    Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or Path.home() / 'agents')
    / 'blackboard' / 'regression-baselines'
)


def baseline_dir() -> Path:
    """The cache directory on the REAL agents tree.

    Resolved from ``OL_REGRESSION_BASELINE_DIR`` when set (tests point this at a
    tmp dir); otherwise the import-time-captured ``~/agents/blackboard/
    regression-baselines``. Deliberately NOT derived from
    ``OURLIBERTY_AGENTS_ROOT`` — that env is the gate's per-run sandbox REDIRECT
    for the suite subprocess, whereas this cache is written by the gate's real
    parent process and must land on the real tree.
    """
    override = os.environ.get('OL_REGRESSION_BASELINE_DIR')
    if override:
        return Path(override)
    return _DEFAULT_BASELINE_DIR


def _is_journal_path(path: str) -> bool:
    """True iff ``path`` is a per-cycle Pulse machine-journal (excluded from the
    content key). Matches an exact file or anything under a journal directory."""
    if path in _PULSE_JOURNAL_FILES:
        return True
    return any(path.startswith(d) for d in _PULSE_JOURNAL_DIRS)


def content_key(sha: str, repo_root: Path) -> Optional[str]:
    """Digest of ``sha``'s tree with the Pulse journal paths removed — the cache
    key. Two commits with identical code-bearing trees (differing only in the
    per-cycle journals) map to the SAME key, so a journal-only Pulse commit
    reuses a warmed baseline instead of missing.

    Built from ``git ls-tree -r -z <sha>`` (one NUL-terminated record per blob:
    ``<mode> <type> <objectsha>\\t<path>``), dropping journal paths, then SHA-1
    over the remaining records joined by NUL. The object SHA in each record makes
    the digest content-sensitive: any real edit (or mode change) to a non-journal
    file changes the key and correctly invalidates the cache.

    ``-z`` (vs the default newline output) is what makes the digest STABLE ACROSS
    MACHINES: it emits paths verbatim — no ``core.quotepath`` octal-escaping of
    non-ASCII, and no ambiguity from paths containing newlines/tabs/quotes — so
    two checkouts with different git config still produce the same key for the
    same tree. ``git ls-tree`` output is already path-sorted and deterministic.

    Returns a 40-char hex digest, or None on ANY git failure — the caller then
    falls back to keying on the raw commit SHA (today's behavior), so a git
    hiccup degrades to "no extra hits", never to a wrong baseline.
    """
    if not _KEY_RE.match(sha or ''):
        return None
    try:
        proc = subprocess.run(
            ['git', '-C', str(repo_root), 'ls-tree', '-r', '-z', sha],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    kept: list[str] = []
    # `-z` separates records with NUL; the trailing NUL yields an empty tail we
    # skip. A path may now contain any byte except NUL (incl. tab/newline), so we
    # split off the path on the FIRST tab only and never rely on line breaks.
    for rec in proc.stdout.split('\0'):
        if not rec or '\t' not in rec:
            continue
        path = rec.split('\t', 1)[1]
        if _is_journal_path(path):
            continue
        kept.append(rec)
    digest = hashlib.sha1('\0'.join(kept).encode('utf-8')).hexdigest()
    return digest


def _path_for(key: str) -> Path:
    return baseline_dir() / f'{key}.json'


def load(key: str) -> Optional[set[str]]:
    """Return the cached failing-test-id set for ``key``, or None on any miss.

    A miss (absent file, unreadable, malformed, schema/key mismatch) is always
    None so the caller transparently falls back to computing it — the cache can
    never wedge or corrupt a verdict, only speed it up. ``key`` is a content_key
    digest (or, on the git-failure fallback, a raw commit SHA); both are 40-hex.
    """
    if not _KEY_RE.match(key or ''):
        return None
    p = _path_for(key)
    try:
        raw = p.read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get('schema') != SCHEMA_VERSION or obj.get('key') != key:
        return None
    tests = obj.get('failing_tests')
    if not isinstance(tests, list) or not all(isinstance(t, str) for t in tests):
        return None
    return set(tests)


def store(
    key: str, failing_tests: set[str], source_commit: Optional[str] = None,
) -> Optional[Path]:
    """Atomically cache ``failing_tests`` under ``key`` (a content_key digest).

    ``source_commit`` is the commit SHA the baseline was computed from, recorded
    for human debugging only (the key is the tree digest, so many commits can
    share one entry). Returns the written path, or None if ``key`` is not a
    canonical 40-char hex digest (we never cache under an abbreviated/symbolic
    key — it must be exact).
    """
    if not _KEY_RE.match(key or ''):
        return None
    p = _path_for(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        'schema': SCHEMA_VERSION,
        'key': key,
        'source_commit': source_commit,
        'failing_tests': sorted(failing_tests),
        'computed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    atomic_io.atomic_write_json(p, obj, indent=2)
    return p


def gc(keep: int = DEFAULT_KEEP) -> int:
    """Keep the ``keep`` most-recently-modified baselines; remove the rest.

    Returns the number removed. Never raises on a partial dir.
    """
    d = baseline_dir()
    try:
        files = [f for f in d.glob('*.json') if f.is_file()]
    except OSError:
        return 0
    if len(files) <= keep:
        return 0

    def _mtime(f: Path) -> float:
        # A concurrent gc/warm may unlink a file we just globbed; treat a
        # vanished/unreadable entry as oldest so the sort never raises (keeps
        # gc()'s "never raises on a partial dir" contract).
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    files.sort(key=_mtime, reverse=True)
    removed = 0
    for f in files[keep:]:
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def warm(repo_root: Path, sha: Optional[str], timeout_s: int) -> int:
    """Precompute + cache the baseline for ``sha`` (default: current main HEAD).

    Off the review critical path: run this post-merge / on a timer so a PR whose
    parent SHA was already warmed skips the parent run entirely at review time.
    Lazy-imports the gate to avoid a circular import at module load.
    """
    import test_regression_check as trc  # lazy: trc imports this module

    # Re-entrancy guard (regbaseline fork-bomb, 2026-06-29): warm() computes the
    # baseline by running the FULL `unittest discover -s scripts/tests` suite in
    # a worktree. build_sandbox_env copies this process env (minus credentials)
    # into that jailed suite, so REGBASELINE_WARMING reaches every test. If any
    # suite path reaches the post-merge warm spawn it would fork ANOTHER real
    # `warm --sha FETCH_HEAD --repo-root <real repo>`, which runs the suite again
    # -> unbounded. A nested warm CLI inherits the flag and refuses right here.
    if os.environ.get('REGBASELINE_WARMING') == '1':
        print('regression_baseline_cache: already warming in this process tree; '
              'refusing to nest (re-entrancy guard)')
        return 0

    # Defensive pre-sweep: the warmer is the most frequent creator of the
    # gate/warmer worktree leak (a SIGKILL mid `git worktree add` leaves a locked,
    # un-prunable .git/worktrees entry). Reap any such orphans from prior killed
    # runs before adding a fresh worktree, so the warmer self-heals on its own
    # cadence instead of waiting for the hourly cleanup timer. Best-effort and
    # strictly scoped to orphans (working dir absent), so it can never disturb a
    # live run; any failure is swallowed so it never blocks a warm.
    try:
        import cleanup_stale_worktrees as csw  # lazy: avoid config read at import
        csw.sweep_orphan_locked_worktrees(repo_root)
    except Exception as exc:  # pre-sweep must never fail the warm
        print(f'regression_baseline_cache: orphan pre-sweep skipped: {exc}',
              file=sys.stderr)

    try:
        canonical = trc.resolve_sha(sha or 'HEAD', repo_root)
    except trc.AnalysisError as exc:
        print(f'regression_baseline_cache: {exc}', file=sys.stderr)
        return 2
    # Key on the journal-stripped tree content, not the raw SHA, so the baseline
    # we warm here is reused by every later PR whose parent shares this code tree
    # (the Pulse-commit-churn that defeated the SHA key). Fall back to the SHA on
    # a git failure so a hiccup degrades to today's behavior, never a wrong key.
    key = content_key(canonical, repo_root) or canonical
    if load(key) is not None:
        print(f'baseline already cached for {canonical[:12]} '
              f'(key {key[:12]}); nothing to do')
        return 0

    # Single-flight (regbaseline fork-bomb, 2026-06-29): Pulse merges fire a
    # post-merge warm every ~7-9 min, but a cold full-suite warm can run longer,
    # so overlapping warms used to stack N concurrent suite runs and OOM a live
    # agent (the droplet has no swap). warm is idempotent + off the review
    # critical path, so a warm that cannot take the host lock simply skips; the
    # holder populates the cache for everyone. OL_REGBASELINE_LOCK_PATH lets
    # tests point the lock at a scratch file.
    lock_path = Path(
        os.environ.get('OL_REGBASELINE_LOCK_PATH')
        or (Path(tempfile.gettempdir()) / 'ol-regbaseline-warm.lock')
    )
    lock_fh = open(lock_path, 'w')
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print('regression_baseline_cache: another warm holds the host lock; '
                  'skipping (single-flight)')
            return 0
        # Re-check under the lock: the prior holder may have just cached this key.
        if load(key) is not None:
            print(f'baseline already cached for {canonical[:12]} '
                  f'(key {key[:12]}); nothing to do')
            return 0
        os.environ['REGBASELINE_WARMING'] = '1'
        try:
            with tempfile.TemporaryDirectory(prefix='regbaseline-warm-') as tmp:
                try:
                    failures = trc.collect_failures_at_sha(
                        canonical, repo_root, timeout_s, Path(tmp),
                    )
                except trc.AnalysisError as exc:
                    print(f'regression_baseline_cache: warm failed: {exc}',
                          file=sys.stderr)
                    return 2
            store(key, failures, source_commit=canonical)
            gc()
            print(f'cached baseline for {canonical[:12]} (key {key[:12]}, '
                  f'{len(failures)} failing)')
            return 0
        finally:
            os.environ.pop('REGBASELINE_WARMING', None)
    finally:
        lock_fh.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog='regression_baseline_cache')
    sub = parser.add_subparsers(dest='cmd', required=True)

    pw = sub.add_parser('warm', help='Compute+cache a SHA baseline (default HEAD).')
    pw.add_argument('--sha', default='HEAD')
    pw.add_argument('--repo-root', default='.')
    pw.add_argument('--timeout-per-sha', type=int, default=900)

    pg = sub.add_parser('gc', help='Prune old baselines.')
    pg.add_argument('--keep', type=int, default=DEFAULT_KEEP)

    ps = sub.add_parser('show', help='Show a cached baseline.')
    ps.add_argument('--sha', required=True)
    ps.add_argument('--repo-root', default='.')

    args = parser.parse_args(argv)

    if args.cmd == 'warm':
        return warm(Path(args.repo_root).resolve(), args.sha,
                    args.timeout_per_sha)
    if args.cmd == 'gc':
        print(f'removed {gc(args.keep)} old baseline(s)')
        return 0
    if args.cmd == 'show':
        import test_regression_check as trc
        repo_root = Path(args.repo_root).resolve()
        try:
            canonical = trc.resolve_sha(args.sha, repo_root)
        except trc.AnalysisError as exc:
            print(f'regression_baseline_cache: {exc}', file=sys.stderr)
            return 2
        key = content_key(canonical, repo_root) or canonical
        hit = load(key)
        if hit is None:
            print(f'no cached baseline for {canonical[:12]} (key {key[:12]})')
            return 1
        print(f'{canonical[:12]} (key {key[:12]}): {len(hit)} failing test(s)')
        for t in sorted(hit):
            print(f'  {t}')
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
