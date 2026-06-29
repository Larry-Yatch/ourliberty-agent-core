# Spec: Regression-gate efficiency (make code reviews conclude under the ceiling)

**Status:** PR 1 (parent-baseline cache) implemented; PR 2 (steady-state warmer) implemented; PR 3 deferred (below).
**Context:** The mandatory review gate `scripts/test_regression_check.py` runs the FULL ~580-test suite TWICE per review — once at the parent SHA (baseline, to filter the ~13 chronic pre-existing failures) and once at head — then diffs the failing sets. That double full-suite run is too slow to finish inside Mirror's 900s bounded-step ceiling, so the gate exits 2 (cannot-conclude) and Mirror escalates CLEAN-code PRs as "review failures" that a human hand-merges (#733; #736 only squeaked under by retrying 3× and bumping the per-SHA timeout). This is the real blocker on hands-free team merges of *code* PRs.

## PR 1 — Parent-baseline cache (SHIPPED in this change; the SAFE lever)

The parent baseline is a pure function of the parent SHA, so compute it once and reuse it.
- `scripts/regression_baseline_cache.py` — per-SHA cache (`load`/`store`/`gc`/`warm`/`show`) under `~/agents/blackboard/regression-baselines/<sha>.json`, atomic writes via `atomic_io`. Keyed by the 40-char SHA; any malformed/mismatched entry reads as a miss (the cache can only speed up, never corrupt a verdict).
- `test_regression_check.py` — before running the parent SHA, reuse the cached baseline if present (skips that full-suite pass); on a miss, run it and warm the cache. `--no-baseline-cache` forces the original two-run behavior. Adds `used_cached_baseline` to the JSON report.
- **Correctness:** a hit is identical to re-running the same SHA — zero verdict change. The only theoretical drift is a flaky test flipping between the cached and head runs; that same flakiness already produces false regressions in the un-cached two-run path, so caching does not worsen it. `--no-baseline-cache` is the escape hatch.
- **Why this is enough to unblock:** the demonstrated pain is *repeated* runs against the same parent SHA — gate retries (#736 ran it 3×) and re-review rounds (#733). With the cache, those reuse the parent baseline, so each retry runs the suite ONCE (head only, ~5min) and fits under the 900s step ceiling.
- **Cache location / HOME note:** the default cache dir is `$HOME/agents/blackboard/regression-baselines`, captured at import (matching the gate's `REAL_HOME` discipline). The gate runs under Mirror's `TIER2_HOME`, so this is consistent across a review's retries/re-reviews (the PR-1 target). For PR 2's warmer to agree with the gate regardless of which HOME it runs under, set `OL_REGRESSION_BASELINE_DIR` to a fixed canonical path (e.g. `/home/larry/agents/blackboard/regression-baselines`) in the gate + warmer environment. Not required for PR 1.

## PR 2 — Steady-state warmer (IMPLEMENTED)

Lazy caching warms on first use, but each PR's parent is often a distinct recent main commit, so the FIRST review of a new parent is still a cold two-run. Closed with a post-merge warmer that runs `regression_baseline_cache.py warm` on each new main SHA (off the review critical path), so a PR branching off warmed main hits the cache on its first review.

**Mechanism (chosen): post-merge hook, NOT a systemd timer.** The spec's timer alternative was acceptable only at near-zero lag; the post-merge hook warms exactly the future parent SHA the instant it is created (zero lag), so it wins. After every successful auto-merge, `outbox_notifier._spawn_post_merge_baseline_warm` (called from both the `outcome=merged` and `outcome=already_merged` branches of `_auto_merge_pr`) spawns a **detached, fire-and-forget** subprocess that:
1. `git fetch --quiet origin main` (the squash-merge just advanced main) then
2. `regression_baseline_cache.py warm --sha FETCH_HEAD --repo-root /home/larry/agent-core --timeout-per-sha 900`.

FETCH_HEAD after that fetch is the squash-merge commit = the `baseRefOid` a future PR forks from = the gate's `--parent-sha`, so the warmed key is exactly what the next first-review looks up. The child does all network + CPU work (`start_new_session=True`, output discarded), so the notifier's poll loop never blocks; any spawn error is swallowed + logged, never propagated — the merge can never be blocked, delayed, or failed by the warmer.

**Canonical cache-dir pin (the correctness lever — the fix is inert without it).** The warmer and the Mirror review gate must read/write the SAME dir. `regression_baseline_cache.baseline_dir()` otherwise defaults to `$HOME/agents/blackboard/regression-baselines` captured at import; the gate runs under Mirror's tier HOME while the warmer runs under `/home/larry`, so the defaults diverge and the cache never hits. Both sides now pin `OL_REGRESSION_BASELINE_DIR=/home/larry/agents/blackboard/regression-baselines`:
- **Warmer side:** set in the detached subprocess env (`outbox_notifier.REGRESSION_BASELINE_CANONICAL_DIR`).
- **Gate side:** set in `agent_runner.run_claude`'s env for Mirror review dispatches only (`_is_mirror_review_dispatch(phase, expected_agent)` gate; `agent_runner.REGRESSION_BASELINE_CANONICAL_DIR`, kept identical to the warmer constant).

- GC keeps the newest N (`DEFAULT_KEEP=40`); PR 1 already calls `gc()` on the live store path and `warm()` calls `gc()` too, so the dir is self-bounding.
- `warm()` is idempotent on a cache HIT (no-op) but NOT under concurrency: two warmers for the same new main SHA both miss `load()` and both run the full suite (last write wins, identical content, no corruption — `atomic_write_json`). The wasted duplicate run is acceptable. A killed warm (e.g. cgroup memory pressure: the suite subprocess is the likely OOM victim, not the notifier) self-heals on the next merge, and PR 1's lazy warm-on-miss remains the backstop.

## PR 3 — Graph-aware head selection (deferred; needs validation)

The further win (5min → ~1min): at head, run only the tests that transitively depend on the diff's changed modules, instead of the whole suite.
- Reverse-dependency engine already exists: `~/ourliberty-graph/pipeline/scan_deps.py::scan_python` returns a `dependents` map (recursive over `scripts/`, includes `scripts/tests/`, stdlib-only). Vendor its ~40-line reverse-dep logic into agent-core so the review gate carries no cross-repo path dependency.
- **Correctness guardrails (required):** keep the FULL suite as a post-merge backstop on main (so anything the static import graph misses — dynamic imports, subprocess-exercised modules — is caught on main within minutes and is revertable); always run `ABSOLUTE_INVARIANT_TESTS` and any changed test files; fall back to the FULL head suite when the diff touches a high-fan-in/core module (e.g. `outbox_notifier`, `agent_runner`, shared utils), where selection would be ~everything anyway and the risk is highest.
- This is intentionally a separate, carefully-reviewed PR — selection trades safety for speed and must not silently drop a real regression.

## Validation
PR 1: `scripts/tests/test_regression_baseline_cache.py` (load/store/gc/bad-sha/empty-set) + `MainBaselineCacheTest` in `test_test_regression_check.py` (hit skips the parent run; hit still detects a real regression; miss warms the cache; `--no-baseline-cache` forces two runs). 60 tests green.

PR 2: `PostMergeBaselineWarmTest` in `scripts/tests/test_outbox_notifier.py` (the warm hook fires from both auto-merge success branches with the canonical `OL_REGRESSION_BASELINE_DIR` env + correct FETCH_HEAD/repo-root argv; it is non-blocking — `start_new_session=True`; a warmer spawn error is swallowed so the merge outcome is unchanged) + a canonical-dir agreement test asserting `outbox_notifier.REGRESSION_BASELINE_CANONICAL_DIR`, `agent_runner.REGRESSION_BASELINE_CANONICAL_DIR`, and `regression_baseline_cache.baseline_dir()` (under that env) all resolve the same path, plus `agent_runner` pinning the dir only for Mirror review dispatches.
