# Green the test suite under full discover (brief)

## Why
`python3 -m unittest discover` over `scripts/tests` currently has 8 failures
(6 FAIL + 2 ERROR). They are TWO distinct root causes — handle each correctly,
do NOT blanket-skip/xfail/reorder to hide them. Goal: full discover green.

## Reproduce

**NEVER source `~/credentials/.env.larry` (or any live credentials) before
running tests.** Live `SUPABASE_*` / `TELEGRAM_*` / claude tokens in the test
process convert every isolation gap from "connection error" into a REAL write,
page, or paid dispatch (test-isolation audit 2026-06-11, holes H6/H7/H12/M4;
see docs/test-jail-spec.md). An earlier revision of this brief prescribed
sourcing the env — that instruction is retracted.

If a test needs `beacon_telegram_bot` to import (it sys.exits without
TELEGRAM_BOT_TOKEN_BEACON), give it DUMMY values:

    cd ~/agent-core \
      && AGENT=beacon TELEGRAM_BOT_TOKEN_BEACON=test-dummy \
         TELEGRAM_ALLOWED_CHAT_IDS=0 \
         python3 -m unittest discover -s scripts/tests

Tests that genuinely require a live credential are misdesigned — fix the test
(dummy env in setUp), never the invocation.

## Bucket A — genuine cross-module isolation/pollution (pass in isolation, fail under discover)
- `tests.test_deploy_notifier.PathIsolationTest.test_agents_root_inside_tmpdir`
- `tests.test_outbox_notifier` `setUpModule` (ERROR)

Root-cause class: a module-level path/env captured at IMPORT time, or a sibling
test module mutating global state (os.environ, cwd, AGENTS_ROOT/LOG_FILE,
sys.modules) without restoring it, leaking into these under a particular
discovery order. PREFLIGHT: identify the actual polluter. FIX via real isolation
— env-overridable + lazily-read paths, setUp/tearDown or addCleanup save/restore
of any mutated global. Each fixed test MUST pass BOTH in isolation AND under full
discover. Do NOT fix by reordering discovery or skipping.

## Bucket B — stale tests asserting intentionally-changed sync behavior (fail even in isolation)
- `tests.test_sync_agent_core_pulse_runtime`:
  `AllowlistOnlyDirtAutoCommitsTest.test_all_four_allowlist_files_dirty_together_auto_committed`,
  `AllowlistOnlyDirtAutoCommitsTest.test_journal_only_dirt_is_auto_committed_and_pushed`,
  `FixturePatternGuardTest.test_fixture_token_in_journal_blocks_auto_commit`,
  `MixedDirtStillBlocksTest.test_journal_plus_unrelated_file_still_blocks`,
  `PushFailureRollsBackTest.test_push_failure_resets_and_alerts`
- `tests.test_sync_agent_core_branch_alert.UncommittedChangesAlertTest.test_dirty_tree_on_main_emits_envelope_and_exits_nonzero`

Root cause: recent INTENTIONAL changes to `scripts/sync_agent_core.sh`:
- `afe9d07 fix(sync): remove fixture-token guard from Pulse-runtime auto-commit paths`
- `c0e238e fix(sync): add rebase fallback to sync_agent_core.sh push`
The tests assert pre-change behavior (e.g. `FixturePatternGuardTest` expects a
`sync-blocked:fixture-pattern-detected` subject that no longer fires because the
guard was removed; the auto-commit/rollback/branch-alert tests assert the old
push path).

PREFLIGHT MUST DETERMINE, per failing test, whether it is STALE (update/remove to
match the intentional new behavior, documenting WHICH commit changed it) vs a REAL
REGRESSION (the behavior change was a mistake).

SAFETY GATE (do not skip): for `FixturePatternGuardTest.test_fixture_token_in_journal_blocks_auto_commit`
specifically — removing the sync-path fixture guard is acceptable ONLY if the
fixture-replay protection still holds at the inbox-watcher dispatch boundary (the
fixture-allowlist gate, PR #170 — gate-at-emission). VERIFY that gate still
enforces before updating/removing this assertion. If the fixture protection is
genuinely gone everywhere, do NOT cement it by deleting the test — REJECT and flag
it as a real safety regression re-opening the 2026-05-29 fixture-replay incident
class.

## Acceptance
- Full `python3 -m unittest discover -s scripts/tests` (from repo root; dummy env
  only, never live credentials) is GREEN.
- Bucket A: real isolation fixes; each passes in isolation AND under discover.
- Bucket B: each test either updated with a one-line reason tying it to the
  specific commit, OR flagged (REJECT/CLARIFY) as a real regression. No silent
  deletion. The fixture-guard safety gate above is honored.
- No production code behavior change unless a Bucket B test reveals a real bug
  (then flag it, do not silently patch the code). No skip/xfail/reorder to mask.

## Constraints
- Test-isolation discipline: env-overridable paths, addCleanup, no module-level
  prod-path capture. Text-only (no emoji). Standard Forge flow: preflight -> build
  -> Mirror review -> PR.
