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
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import atomic_io

SCHEMA_VERSION = 1
_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
# Keep the newest N baselines; older ones GC away (main advances, old parents
# stop being anyone's merge-base). Bounds the dir to a handful of small files.
DEFAULT_KEEP = 40

# Captured at import — mirrors test_regression_check.REAL_HOME's discipline:
# anchor the real-tree default to the process's home as of start, not a
# call-time Path.home() that an in-process HOME mutation could move out from
# under us. The OL_REGRESSION_BASELINE_DIR override (used by tests) still wins.
_DEFAULT_BASELINE_DIR = Path.home() / 'agents' / 'blackboard' / 'regression-baselines'


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


def _path_for(sha: str) -> Path:
    return baseline_dir() / f'{sha}.json'


def load(sha: str) -> Optional[set[str]]:
    """Return the cached failing-test-id set for ``sha``, or None on any miss.

    A miss (absent file, unreadable, malformed, schema/sha mismatch) is always
    None so the caller transparently falls back to computing it — the cache can
    never wedge or corrupt a verdict, only speed it up.
    """
    if not _SHA_RE.match(sha or ''):
        return None
    p = _path_for(sha)
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
    if obj.get('schema') != SCHEMA_VERSION or obj.get('sha') != sha:
        return None
    tests = obj.get('failing_tests')
    if not isinstance(tests, list) or not all(isinstance(t, str) for t in tests):
        return None
    return set(tests)


def store(sha: str, failing_tests: set[str]) -> Optional[Path]:
    """Atomically cache ``failing_tests`` as the baseline for ``sha``.

    Returns the written path, or None if ``sha`` is not a canonical 40-char SHA
    (we never cache an abbreviated/symbolic ref — the key must be exact).
    """
    if not _SHA_RE.match(sha or ''):
        return None
    p = _path_for(sha)
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        'schema': SCHEMA_VERSION,
        'sha': sha,
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

    try:
        canonical = trc.resolve_sha(sha or 'HEAD', repo_root)
    except trc.AnalysisError as exc:
        print(f'regression_baseline_cache: {exc}', file=sys.stderr)
        return 2
    if load(canonical) is not None:
        print(f'baseline already cached for {canonical[:12]}; nothing to do')
        return 0
    import tempfile
    with tempfile.TemporaryDirectory(prefix='regbaseline-warm-') as tmp:
        try:
            failures = trc.collect_failures_at_sha(
                canonical, repo_root, timeout_s, Path(tmp),
            )
        except trc.AnalysisError as exc:
            print(f'regression_baseline_cache: warm failed: {exc}',
                  file=sys.stderr)
            return 2
    store(canonical, failures)
    gc()
    print(f'cached baseline for {canonical[:12]} ({len(failures)} failing)')
    return 0


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
        try:
            canonical = trc.resolve_sha(args.sha, Path(args.repo_root).resolve())
        except trc.AnalysisError as exc:
            print(f'regression_baseline_cache: {exc}', file=sys.stderr)
            return 2
        hit = load(canonical)
        if hit is None:
            print(f'no cached baseline for {canonical[:12]}')
            return 1
        print(f'{canonical[:12]}: {len(hit)} failing test(s)')
        for t in sorted(hit):
            print(f'  {t}')
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
