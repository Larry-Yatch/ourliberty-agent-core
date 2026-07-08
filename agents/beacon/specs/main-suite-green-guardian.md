# Build spec — Main-Suite Green Guardian

**Mission:** make "the full test suite at origin/main is green" an enforced,
self-healing invariant instead of an intention.
**Status:** Draft v2 for team build — 2026-07-08. v1 was torn down by a
three-lens antagonistic review (ops-toil, autonomy-safety, feasibility —
6 blockers, ~15 majors); every finding is folded in below. The reviews are
the design record; where a v2 rule looks oddly specific, it is a scar.
**Repo:** ourliberty-agent-core.
**Author:** Claude Code (desktop). **Approver:** Larry.
**Builds on:** #866 (gate fresh-parent re-verification — MERGED), #864/#865
(hermetic test fixes), regression_baseline_cache v2 (#774), Check-V headless
approval pattern (`pulse_check_v.py` — the working template for a timer that
files approvals and dispatches config-only PRs).
**Supersedes:** the never-built "weekly order-fragile test gauge" (#792/#799
era) — this guardian is that gauge, grown into a closed loop.

---

## 0. Goal (one paragraph)

A nightly droplet job runs the FULL suite at one pinned origin/main SHA,
classifies every red by isolation re-run (passes-alone → order/pollution
flake; fails-alone → genuine break or env rot), and drives the red count to
zero through the existing propose→approve→dispatch fabric: each run files at
most ONE approval decision whose approval drains fix tasks serially through
normal Forge→Mirror review. A ledger measures every proposal's full outcome
window; measured success earns staged autonomy (shadow → propose → auto-file
→ auto-merge), each dial-up a config-only PR Larry approves from a card. End
state: main genuinely green, the gate's baseline assumption becomes true,
and gate tightening is proposed (never auto-applied) with the evidence
attached.

## 1. Problem statement

The 2026-07-08 false-BLOCK batch (6 PRs escalated in one day) exposed that
main's suite was never truly green — it was **green-by-cancellation**. The
original two-run gate ran parent and head fresh in one invocation, so a
deterministic full-suite flake failed on BOTH sides and hid in the
"pre-existing, tolerated" bucket. The baseline cache (#774) broke that
symmetry and surfaced the flakes as false regressions. #866 fixed the gate's
verdict (on a would-be BLOCK against a cached baseline, re-run the parent
suite fresh and recompute — restoring the two-run symmetry on demand), but
nothing drives standing reds to zero:

* The gate TOLERATES pre-existing failures — debt accumulates silently, and
  every standing red is a blind spot (a real regression in an already-red
  test is invisible) plus a recurring fresh-parent re-verify cost (~9 min)
  on every PR that would-BLOCK.
* A GENUINE break on main surfaces only as collateral noise inside PR
  reviews — never as its own signal.
* Fix work happens only when a human notices (the 2026-07-08 fixes were
  manual archaeology).

## 2. Decisions locked

| # | Decision | Rationale / scar |
|---|----------|------------------|
| L1 | **Digest, never page — counted across ALL surfaces.** Per guardian run: ≤1 FYI signal card + ≤1 pending-approval decision. Guardian pending entries are stamped `bare_approvable: False` and `chat_id=0` (no Telegram reminders — guardian work is never urgent). The doorbell may ring once for a new decision entry; that is the ceiling. | v1 as-specced would have sent up to 30 reminder DMs/week via per-flake `add_pending` entries — the 1063-alert pattern rebuilt. |
| L2 | **Shadow-first.** Stage 0 detects + records only; ONE summary card at window end. | Absorbs the standing backlog without a day-1 card flood. |
| L3 | **Stage authority is guardian-read-only.** `config/suite-guardian.json` (repo file, added to `config/deep-review-paths.json`) is the ONLY stage source; graduation approval dispatches a **config-only PR** (Check-V pattern, `pulse_check_v.py:626`). The registry never stores stage; on unreadable config → Stage 0. Effective stage = **min(config stage, what the live trust-policy dial permits)** — the dial always wins. Dial→max-stage map (explicit, PR-3 tests assert it): `conservative→1`, `balanced→2`, `loose→3`. | v1 let the guardian write its own stage byte: self-promotion by one JSON write, and causally disconnected from any approval. |
| L4 | **Fix scope is `scripts/tests/**` only, MECHANICALLY enforced.** Guardian-lane fix PRs (identified by ledger `fix_task_id` join, not labels) pass an allow-list diff gate — every changed path must match `scripts/tests/**` — checked at dispatch AND immediately pre-merge against the current head SHA, fail-closed on fetch failure; the pre-merge check lives in the `outbox_notifier` merge-eligibility path (the same seam as `_deep_review_required`), keyed off the ledger `fix_task_id` join. Violation → block, ledger `scope-violation`, stage drop (L10), escalate. Production-side fixes always route to Larry regardless of stage. | v1's scope control was prompt-deep: `trust_policy` matches self-declared `changed_files`, and the deep-review deny-list covers ~9 files — nothing checked the actual diff. |
| L5 | **Four-way classification:** `order-flake` (fails in suite, passes alone), `env-fail` (fails alone with import/skip/venv/credential signature, or the canary preflight failed), `genuine-break` (fails alone, no env signature, AND previously green — defined as *absent from the red set of ≥1 prior completed guardian run*, derivable since every run is full-suite; a registry entry is NOT required, else long-green tests could never qualify), `infra-flake` (run-level: timeout/OOM/collection error after one retry). Never conflated; a red inherited at first-ever observation (= the guardian's first-ever completed run) is backlog debt, never a break. | v1's "fails alone = genuine break" would have paged Larry for droplet venv rot (~13 known env-dependent fails exist today). |
| L6 | **One test-isolation engine.** The single-test runner is NEW code (nothing like it exists — v1 falsely cited "verified" #866 primitives that never landed), added INTO `scripts/test_regression_check.py` as PR-1's refactor. Why isolation is valid here though #866 rejected it at the gate: the gate must attribute a failure to a *diff* (isolation can't distinguish pre-existing flake from diff-introduced pollution); the guardian runs at ONE SHA with no diff question — "passes alone vs fails alone" is exactly the classification sought. | Feasibility review B1 / safety review M4. State this in PR-1's description or Mirror will bounce it against the #866 design comment. |
| L7 | **Nightly 03:30 UTC, single-flighted with the suite-runner ecosystem.** The guardian acquires the regbaseline-warmer's single-flight lock for every suite-scale run — relocated in PR-1 from `tempfile.gettempdir()` to the **absolute** path `/home/larry/agents/state/ol-regbaseline-warm.lock` (NEVER `~`/`Path.home()` — a `$HOME`-relative path re-opens the #755 HOME-swap class: a tier-swapped warmer would flock a different file and void the single-flight); `OL_REGBASELINE_LOCK_PATH` override kept; warmer updated in the same PR; the guardian unit's `ReadWritePaths` must cover it. Can't acquire → skip the night, journal it. Orphan-worktree pre-sweep (`cleanup_stale_worktrees.sweep_orphan_locked_worktrees`) at start. Budgets: per-isolation-test timeout 120s; total wall cap 90 min (excess deferred, journaled); `TimeoutStartSec=10800`. | v1 claimed the lock was "shared with nothing" — inverting a documented droplet-OOM incident (no swap; stacked suite runs OOM'd a live agent). The template unit's `TimeoutStartSec=300` would have killed the first suite run (~537s measured). |
| L8 | **Gate tightening (end-state) is a proposal, and it tightens to *classified* strictness.** At 0 non-parked reds for 14 consecutive runs, file the one-time card proposing: a head failure BLOCKs only after surviving infra-retry + isolation classification as non-flake (or via `ABSOLUTE_INVARIANT_TESTS` expansion) — never raw any-failure. | v1's "BLOCK on ANY head failure" would have re-created the exact #866 false-BLOCK class (infra-flakes, PR-reshuffled discovery order) as the payoff step. |
| L9 | **Park, don't decay.** Larry-rejected proposals and classification flip-floppers (status `unstable`, ≥2 category flips) go to the Parked lane via the existing capture surface — never re-proposed, never silently dropped. L8's zero-count = `reds − parked == 0`, and the L8 card attaches the parked list so Larry re-decides them exactly once, when the decision is live. | v1's "reject → never re-proposed" made 0-reds unreachable forever after one rejection, silently killing the payoff. |
| L10 | **Downgrades are proportional and evidence resets.** `regressed` on an AUTO-MERGED fix or any scope-violation → Stage 0 + escalate (not decrement). Other `regressed` → stage−1. Re-promotion requires evidence accrued AFTER the downgrade; the graduation card must cite the downgrade cause. Before any `regressed` verdict, re-run the classifier to distinguish "same leak returned" (counts against fix) from "new polluter re-reddened the victim" (does not). Downgrade notices go to the briefings/journal channel — the Approvals tab carries decisions only, no FYI cards. | v1's downgrade-by-one after a bad Stage-3 auto-merge left the least-trusted state still auto-dispatching, and its FYI-card habit put unactionable noise on the action surface. |

## 3. Success bar (acceptance)

1. **Shadow proof:** after 7 shadow runs, the registry lists every standing
   red with a stable classification, matching a by-hand
   `collect_failures_at_sha` spot-check.
2. **Loop proof (Stage 1):** one real order-flake goes card → approve →
   Forge fix PR (with named poison-injection test) → Mirror PASS → merge →
   guardian observes green ≥2 runs + the named test present and passing →
   obligation `resolved` — zero manual glue.
3. **Genuine-break proof:** a deliberately-broken previously-green test (via
   `--test-sha` on a scratch SHA) classifies `genuine-break`, escalates
   exactly once (edge-triggered), and the card carries the last-green→now
   SHA range with the suspect commit list.
4. **Toil proof:** over 30 days, guardian-attributable Larry-visible events
   (cards + pending entries + doorbell rings + DMs, ALL surfaces) average
   ≤1/run, with zero reminder DMs and zero pages except a real
   genuine-break episode.
5. **Payoff proof:** `reds − parked == 0` holds ≥14 runs → the L8 card is
   filed with the evidence table and parked list attached.

## 4. Reuse map (corrected; every row re-verified against origin/main 2026-07-08)

| Need | Reuse | Where |
|------|-------|-------|
| Full-suite run at SHA (worktree + sandbox + sentinel tripwire) | `collect_failures_at_sha`, `build_sandbox_env`, `add_worktree`/`remove_worktree`, `parse_unittest_failures` | `scripts/test_regression_check.py` |
| Single-test isolation run | **NEW** `run_single_test_in_dir(worktree, test_id, env, timeout_s)` — `python3 -m unittest <id>` with cwd `<worktree>/scripts/tests` (bare-module ids only import from there), same sandbox/wall; fail-closed parse | PR-1 refactor INTO `test_regression_check.py` (L6) |
| Approvals-tab visibility | `add_pending` **+ MANDATORY** `chain_event_emit.emit_event(**build_approval_request_chain_event(payload))` — the tab is fed ONLY by `approval_request` chain_events (`heal_unregistered_approval.py`) | `beacon_approval_handler.py:967`, `chain_event_emit.py` |
| Pending-entry contract | `add_pending(payload, chat_id=0, …)`: payload REQUIRES `task_id`, `target_repo='ourliberty-agent-core'`, `task_type`, `prompt`; stamp `bare_approvable: False`; dedup via `find_by_id_any_state` before emit; resolution via `resolve(id, status, note)` (there is no `resolve_approval`) | `beacon_approval_handler.py:503,659` |
| FYI signal card | `for_larry_signal.upsert_record`/`resolve_record` with `needs_larry: False`; do NOT use `sync_prefix` (it auto-resolves absent keys — wrong for per-run cards) | `scripts/for_larry_signal.py` |
| Outcome ledger | clone `open_obligation/resolve_obligation/list_open` | `scripts/no_session_ledger.py` |
| Headless approval→config-PR loop | Check-V graduation pattern (chat_id=0, config-only PR, `find_*_pending` dedup) | `scripts/pulse_check_v.py:614-640` |
| Suite single-flight + orphan sweep | warmer lock (relocated per L7) + `sweep_orphan_locked_worktrees` | `regression_baseline_cache.py:318`, `cleanup_stale_worktrees.py` |
| Timer/wrapper template | `ourliberty-ledger.{service,timer}` + `run_ledger.sh` (lock, EMERGENCY_HALT) — with L7's TimeoutStartSec override and m1's log discipline | `systemd/`, `scripts/` |

## 5. Deliverables

### D1 — Detector + classifier + registry (PR-1)

`scripts/main_suite_guardian.py`, entrypoint `run_guardian(repo_root, *, mode)`:

1. Fetch; pin ONE resolved origin/main SHA for the whole cycle (detached
   worktree — the sync timer can't move code under it). **Skip when the SHA
   equals last run's, regardless of color** (identical code yields identical
   information; a red-main night must not re-burn the budget) — EXCEPT when
   the last run was inconclusive (`infra-flake`/canary-failed), which must
   retry or D2.5's consecutive-inconclusive logic can never advance. A
   skipped night inherits the prior run's result for all streak counters
   (green streaks, consecutive-red) — same identical-code reasoning.
2. **Canary preflight:** run 2 known-hermetic sentinel tests first; if they
   fail, the whole run is `infra-flake`/env — record, no per-test state
   mutation, no cards beyond digest aggregation (see D2.5).
3. `collect_failures_at_sha` → red set. Run-level failure → one retry →
   still bad = `infra-flake` run.
4. **Step-change branch:** if new reds > 5 in one run, SKIP per-test
   isolation; record a single `suite-event` keyed to the last-green→now SHA
   range; the run's decision entry proposes triage/revert of the suspect
   commits (the SHA diff answers what 40 isolation re-runs cannot). A
   `suite-event` ALSO routes through the same edge-triggered `escalate` path
   as genuine-break (once per episode) — a 40-red break must page at least
   as loudly as a 1-test break. Per-test bookkeeping resumes after the
   event resolves.
5. Otherwise, per red: `run_single_test_in_dir` → classify per L5 (env
   signature screen before genuine-break; genuine-break additionally
   requires previously-green-in-registry).
6. Registry `~/agents/state/main-suite-guardian.json` (atomic, keyed by test
   id): `{classification, first_seen, last_seen, consecutive_red_runs,
   consecutive_green_runs, flip_count, last_sha, history[≤20]}`.
   Green ≥2 consecutive runs → `recovered` (digest note, never carded).
   `flip_count ≥2` → `unstable` → Parked (L9).
7. On a green, canary-clean, collection-count-sane full run (rolling
   expectation ±10%): `regression_baseline_cache.store(content_key(main_sha,
   repo_root) or main_sha, ∅)`. This is hygiene, not a payoff — the
   post-merge warmer has usually warmed the key already; guardian-origin
   baselines stay subject to the gate's #866 fresh-parent re-verify.
8. Weekly (Sunday run), an additional **randomized-order suite pass** (seed
   journaled) purely for detection — order-flakes hide from a single fixed
   discovery order; findings feed the same registry, never the baseline.
9. Wrapper `run_main_suite_guardian.sh`: warmer lock (L7), stale-lock
   break (lock older than wall cap → reap + proceed), hard wall-clock kill
   at 2× cap, EMERGENCY_HALT, **raw suite output buffered in tmp until after
   `scan_real_tree_for_sentinel`** (teeing suite output into `~/agents/logs`
   pre-scan can self-trip the tripwire — only parsed test ids are logged),
   heartbeat registered in the pulse-check cadence config (~30h threshold)
   so the existing staleness healer owns guardian liveness.
10. Timer `ourliberty-main-suite-guardian.{service,timer}` (03:30 UTC,
    `RandomizedDelaySec=600`, `TimeoutStartSec=10800`), installed per the
    reinstall discipline. `--mode shadow|propose` (default read from
    `config/suite-guardian.json`); `--test-sha` for acceptance test 3.

### D2 — Proposal author + approvals wiring + ledger (PR-2)

1. **One decision per run, at most.** New actionable findings (new
   `order-flake`s, a `genuine-break` episode, a `suite-event`) batch into a
   single pending entry: `add_pending` + `approval_request` chain_event,
   `chat_id=0`, `bare_approvable: False`, `task_id`
   `suite-guardian-run-<date>`, deduped via `find_by_id_any_state`. PR-2
   also adds a falsy-skip (`if not chat_id: continue`) to
   `_check_due_reminders` in `beacon_telegram_bot.py` — today `chat_id=0`
   reminder suppression works only because Telegram rejects chat 0
   (accidental, not designed); two lines close the class.
   Approve = consent to dispatch the listed fixes **serially, ≤3 open fix
   obligations at any time** (guardian dispatches the next as one resolves).
   Reject = all listed items → Parked (L9).
2. **The backlog drains through the same valve.** The one-time
   Stage-1-entry backlog entry (count + top-3 named + registry pointer, not
   a 30-test wall) is standing consent: approval lets the guardian keep
   **dispatching** backlog items serially under the same ≤3-open cap until
   empty — no further per-item pending entries (that would be the doorbell
   drip L1 exists to prevent).
3. **FYI run card** (`for_larry_signal`, `needs_larry: False`,
   `upsert_record`/`resolve_record` only): the plain-language run digest —
   what changed, what recovered, what's parked, infra notes. One decision
   surface per decision: the pending entry is the decision; the card is
   context.
4. **Fix-task template:** reproduce the suite-order failure; find the
   shared-state read; make the victim hermetic (the
   `WaitingSourceReadersTest` pattern); **ADD a named, persistent
   poison-injection test** proving the leak reproduced-then-immunized (the
   name is recorded in the ledger at dispatch; Mirror's instruction becomes
   mechanical: FAIL if the named test is absent from the diff); scope
   `scripts/tests/**` only (L4 gate enforces); if a production seam is
   genuinely needed, STOP and report — that escalates to Larry.
5. **Escalation (the only page):** `genuine-break` at 2 consecutive reds,
   **edge-triggered once per (test, episode)** — a new episode starts only
   after the test returns to green. Severity `critical`, routed via the
   existing `escalate` DM path, carrying the SHA range + suspect commits.
   `env-fail` and `infra-flake` NEVER page: infra-flake cards at 2
   consecutive inconclusive runs, then doubling thresholds; ≥3 consecutive
   routes to the Medic/system-health surface (machine problem, machine
   layer).
6. **Ledger** `~/agents/state/suite-guardian-ledger.json` (obligation
   pattern): `proposed_at, decision (approved|parked), fix_task_id,
   poison_test_name, fix_pr, merged_at, green_streak_after_fix,
   window_closed (14 runs), regressed, regressed_attribution
   (same-leak|new-polluter)`. **Resolution keys off the observable:** test
   green ≥2 consecutive guardian runs AND the named poison test present +
   passing at main — regardless of merge provenance (out-of-band merges are
   a documented class). Approved-but-dead obligations (no PR activity 7
   days — the verdictless-death class) → terminal `abandoned`, re-eligible
   once, digest-noted.

### D3 — Autonomy stages + graduation (PR-3)

`config/suite-guardian.json` (repo; added to `config/deep-review-paths.json`
in this PR) holds `stage`; L3 governs authority and precedence.

| Stage | Behavior | Graduation evidence (only CLOSED 14-run windows count) |
|-------|----------|--------------------------------------------------------|
| 0 shadow | registry + digest only | 7 runs, no classification flip-flops |
| 1 propose | D2 in full; every dispatch consented via the run entry | ≥8 closed windows, 0 `regressed` (same-leak), 0 scope violations |
| 2 auto-file | fix tasks dispatch without prior consent; run entry becomes reject-to-kill | ≥10 auto-filed closed windows, 0 same-leak regressions. **Prerequisite PR:** a `source: 'suite-guardian'` + `file_patterns: ['scripts/tests/**']` auto_approve rule added to the trust-policy presets (`policy_for_level`) and `config/trust-policy.json` — deep-review-held. Under the `conservative` dial the guardian is pinned ≤ Stage 1 by construction; the graduation card must say "requires dial ≥ balanced." |
| 3 auto-merge | test-only fix PRs merge on Mirror PASS, **SHA-bound** (verdict bound to `headRefOid`, `LOCAL_REVIEW_PASS` semantics — the label-persistence force-push hole is documented) + the L4 diff gate re-checked at merge time | Larry's standing call; proposed by card only |

Graduation card → approval → **config-only PR** flipping `stage` (Check-V
pattern); the card cites the ledger evidence and any prior downgrade cause.
Downgrades per L10.

### D4 — L8 tightening card (inside PR-3, trivial)

Per L8/L9: at `reds − parked == 0` for 14 runs, file the one-time card with
the evidence table + parked list. Never auto-applied.

## 6. Explicitly OUT of scope

* Hand-fixing the backlog inside this build (the loop drains it — that IS
  the product).
* JS/vitest repos (gate already delegates to CI there); cross-repo
  generalization; new dashboard UI (existing Approvals/briefings surfaces
  render everything).
* Replacing Medic/Check-V — the guardian is a producer into the same
  fabric; Check-V unification remains its own mission.

## 7. Risks & mitigations (v2 — survived the antagonistic pass)

| Risk | Mitigation |
|------|------------|
| Guardian goes silently blind (wedged lock, hung run) | Wrapper hard kill + stale-lock break + pulse-check heartbeat → existing staleness healer pages the machine layer (D1.9). |
| Card/obligation leaks from verdictless Forge deaths or out-of-band merges | Observable-based resolution + 7-day `abandoned` age-out (D2.6). |
| Fixes that "fix" by weakening assertions | Named poison-test required in-diff (mechanically checkable), verified present+passing before `resolved`; same-leak regression attribution; L10 downgrades. |
| Guardian's green poisons the gate baseline | Store gated on canary-clean + collection-count sanity; #866 fresh-parent re-verify remains the gate's containment (D1.7). |
| Droplet OOM from stacked suite runs | Warmer single-flight lock around every suite-scale run; skip-night on contention (L7). |
| Evidence-bar gaming / rubber-stamp graduation | Only closed windows count; evidence resets on downgrade; scope violations mechanically detected (L4) so "0 violations" is measurable; randomized weekly pass makes green streaks carry information. |
| Mass-break smeared into multi-night dribble | Step-change branch: one suite-event, one card, SHA-range triage (D1.4). |

## 8. Build sequence handoff

Three PRs, strictly ordered, each Mirror-reviewed:

* **PR-1 (D1):** the `run_single_test_in_dir` refactor into
  `test_regression_check.py` + warmer-lock relocation + detector/classifier/
  registry + wrapper + timer, shipped in `shadow` mode with
  `config/suite-guardian.json` at stage 0. Tests: fake-invoker unit tests
  for classification (incl. env-signature screen, canary, step-change
  branch, SHA-skip), registry transitions, lock/timeout behavior.
* **PR-2 (D2):** proposal author + approvals wiring (chain_event + pending
  contract + bare_approvable) + ledger + escalation routing + Parked lane.
  Tests: one-decision-per-run batching, serial-drain cap, dedup, reject→
  parked, edge-triggered escalation, observable resolution, abandoned
  age-out — all against tmp state files.
* **PR-3 (D3+D4):** stage machine (config-authority + min-with-dial) +
  graduation/downgrade + trust-policy preset rule + L4 diff gate + L8 card.
  Tests: stage precedence, proportional downgrade + evidence reset,
  SHA-bound merge eligibility, diff-gate fail-closed.

Sequence activates only after this spec merges to main (spec_doc presence
gate). `dispatch_text` per task ≤500 chars per orchestrator contract.
