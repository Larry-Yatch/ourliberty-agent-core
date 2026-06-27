# Spec: Regression-gate efficiency (make code reviews conclude under the ceiling)

**Status:** PR 1 (parent-baseline cache) implemented; PR 2/PR 3 deferred (below).
**Context:** The mandatory review gate `scripts/test_regression_check.py` runs the FULL ~580-test suite TWICE per review — once at the parent SHA (baseline, to filter the ~13 chronic pre-existing failures) and once at head — then diffs the failing sets. That double full-suite run is too slow to finish inside Mirror's 900s bounded-step ceiling, so the gate exits 2 (cannot-conclude) and Mirror escalates CLEAN-code PRs as "review failures" that a human hand-merges (#733; #736 only squeaked under by retrying 3× and bumping the per-SHA timeout). This is the real blocker on hands-free team merges of *code* PRs.

## PR 1 — Parent-baseline cache (SHIPPED in this change; the SAFE lever)

The parent baseline is a pure function of the parent SHA, so compute it once and reuse it.
- `scripts/regression_baseline_cache.py` — per-SHA cache (`load`/`store`/`gc`/`warm`/`show`) under `~/agents/blackboard/regression-baselines/<sha>.json`, atomic writes via `atomic_io`. Keyed by the 40-char SHA; any malformed/mismatched entry reads as a miss (the cache can only speed up, never corrupt a verdict).
- `test_regression_check.py` — before running the parent SHA, reuse the cached baseline if present (skips that full-suite pass); on a miss, run it and warm the cache. `--no-baseline-cache` forces the original two-run behavior. Adds `used_cached_baseline` to the JSON report.
- **Correctness:** a hit is identical to re-running the same SHA — zero verdict change. The only theoretical drift is a flaky test flipping between the cached and head runs; that same flakiness already produces false regressions in the un-cached two-run path, so caching does not worsen it. `--no-baseline-cache` is the escape hatch.
- **Why this is enough to unblock:** the demonstrated pain is *repeated* runs against the same parent SHA — gate retries (#736 ran it 3×) and re-review rounds (#733). With the cache, those reuse the parent baseline, so each retry runs the suite ONCE (head only, ~5min) and fits under the 900s step ceiling.

## PR 2 — Steady-state warmer (deferred; small)

Lazy caching warms on first use, but each PR's parent is often a distinct recent main commit, so the FIRST review of a new parent is still a cold two-run. Close that with a post-merge warmer: run `regression_baseline_cache.py warm` on each new main SHA (off the review critical path), so a PR branching off warmed main hits the cache on its first review.
- Add `systemd/ourliberty-regression-baseline-warm.{service,timer}` (or fold a `warm` call into the post-merge/sync path). Note: the unit needs the standard manual install (install-drift is alert-only).
- GC keeps the newest N (`DEFAULT_KEEP=40`).

## PR 3 — Graph-aware head selection (deferred; needs validation)

The further win (5min → ~1min): at head, run only the tests that transitively depend on the diff's changed modules, instead of the whole suite.
- Reverse-dependency engine already exists: `~/ourliberty-graph/pipeline/scan_deps.py::scan_python` returns a `dependents` map (recursive over `scripts/`, includes `scripts/tests/`, stdlib-only). Vendor its ~40-line reverse-dep logic into agent-core so the review gate carries no cross-repo path dependency.
- **Correctness guardrails (required):** keep the FULL suite as a post-merge backstop on main (so anything the static import graph misses — dynamic imports, subprocess-exercised modules — is caught on main within minutes and is revertable); always run `ABSOLUTE_INVARIANT_TESTS` and any changed test files; fall back to the FULL head suite when the diff touches a high-fan-in/core module (e.g. `outbox_notifier`, `agent_runner`, shared utils), where selection would be ~everything anyway and the risk is highest.
- This is intentionally a separate, carefully-reviewed PR — selection trades safety for speed and must not silently drop a real regression.

## Validation
PR 1: `scripts/tests/test_regression_baseline_cache.py` (load/store/gc/bad-sha/empty-set) + `MainBaselineCacheTest` in `test_test_regression_check.py` (hit skips the parent run; hit still detects a real regression; miss warms the cache; `--no-baseline-cache` forces two runs). 60 tests green.
