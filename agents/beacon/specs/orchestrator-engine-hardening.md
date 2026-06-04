# Spec: Orchestrator Engine Hardening — close the silent-failure class end-to-end

**Status:** Draft (awaiting Larry approval)
**Author:** Claude-as-Forge (written 2026-06-03, grounded in the 2026-05-29 → 2026-06-02 stranding incidents)
**Approver:** Larry (pending)
**Phase:** Phase E-orchestrator follow-on. Hardening of the shipped build-sequence orchestrator, not new orchestrator capability.
**Parent spec:** [agents/beacon/specs/build-sequence-orchestrator.md](build-sequence-orchestrator.md) (§ 5.2 advancer architecture, § 5.3 belt-and-suspenders gate, § 5.4 failure handling)
**Engine under hardening:** `scripts/build_sequence_advancer.py` (the advancer daemon) + `scripts/sequence_shortcut_helpers.py` (`apply_step_merged` / the V6 hook surface)
**Prior point-fixes this spec generalizes:** PR #187 (V6 silent-miss literal fix), PR #216 (active reconciliation backstop), PR #225 (cycle-timer infinity-trap fix — sibling fragility), and the in-flight `fix-advancer-reconcile-gh-failure` (the gh `--repo` qualifier fix)

> **This is a design spec, not an implementation.** It is the canonical reference for a follow-up implementation sequence Larry will approve separately. No engine code changes ship with this doc.

---

## 1. Purpose

The orchestrator's promise is simple: **a multi-step build sequence advances to completion without manual operator intervention.** When a sequence step's PR merges, the engine reconciles that step to `merged`, satisfies downstream dependencies, dispatches the next step, and — when all steps are merged — closes the sequence. The operator watches; the operator does not push.

Over four days (2026-05-29 → 2026-06-02) that promise broke **three times**. Each time a sequence step's PR had genuinely merged, yet the step stayed stranded in `dispatched`; each time an operator had to hand-edit the sequence file (`actor: beacon-manual-unstick`) to advance it. A self-heal backstop (PR #216's active reconciliation) had already merged — and still the strandings happened, because the backstop's own `gh` call failed silently on every tick.

The strandings are not three isolated bugs. They are a **class**: the engine has several independent single-points-of-silent-failure, each was patched reactively after it bit, and none of them is self-observable. The engine can be fully wedged — backstop non-functional, steps stranded, operator unaware — while its logs show nothing more alarming than `reconciled_steps=0` and a WARNING that omits the reason.

This spec inventories the class (§ 2), traces why point-fixes didn't generalize (§ 3), and proposes four structural fixes that make silent failure structurally impossible — or at minimum, immediately visible (§ 4). The throughline: **the engine must observe its own health, log every failure with its cause, exercise its backstop in CI, and keep ticking even if its timer wedges.**

---

## 2. Incident inventory

All four classes were observed in production logs and sequence-file audit logs. Evidence paths are concrete so a future reader can re-derive each row.

| # | Failure class | Date(s) | What happened | Evidence |
|---|---|---|---|---|
| 1 | **V6 silent-miss** | 2026-05-29, 2026-05-31, 2026-06-02 (3 incidents) | The notifier's `apply_step_merged` hook resolves "which step does this merge belong to" by **exact `task_id` match**. When work spans a rebase / rescue / revision dispatch, the auto-merge fires under a *derivative* `task_id` (e.g. `pr211-rebase-step-a-rotation` instead of `seq-…-step-a-rotation`); exact-match misses; the step strands in `dispatched` though its PR is MERGED. | `build_sequence_advancer.py` `_reconcile_dispatched_steps` docstring; `rate-limit-resilience-001.json` audit_log (`step-a-rotation` → PR #211, `actor: beacon-manual-unstick`); `operator-ux-rollout` (step-rescue-runbook, 2026-05-29); `approvals-queue-rework.json` (`step-autoclear`→#250, `step-alert-promotion`→#251, both `actor: beacon-manual-unstick`, 2026-06-02) |
| 2 | **Reconcile gh-call failure** | 2026-05-30 onward (continuous) | PR #216's active-reconciliation backstop ran every tick, but its `gh pr list` call passed a **bare repo name** (`ourliberty-agent-core`) where `gh --repo` requires `OWNER/REPO`. `gh` exits rc=1 before any network call. The backstop returned `None` ("couldn't query") for every step, every tick → `reconciled_steps=0` forever. **This is why the V6 backstop did not save us.** | `build-sequence-advancer.log` L1100–1105: `reconcile: gh pr list failed for repo=ourliberty-agent-core; skipping` paired with `tick: … reconciled_steps=0`, firing every 5 min |
| 3 | **Swallowed subprocess stderr** | 2026-05-30 → 2026-06-02 (undiagnosed for days) | The reconcile gh-failure (class 2) logged the bare line `gh pr list failed for repo=X; skipping` with **no returncode and no stderr**. The actual cause (`expected the [HOST/]OWNER/REPO format`) was right there in `gh`'s stderr — but swallowed. The failure was undiagnosable from logs; it took manual reproduction to find the `--repo` format error. | Same log lines (class 2) — note the absence of `rc=` / `stderr=` in the pre-fix WARNING shape |
| 4 | **advancer.timer stuck-timer cadence** | observed in the stuck-timer cluster | The advancer ticks only because `ourliberty-build-sequence-advancer.timer` (`OnCalendar=*:0/5`) fires. The advancer.timer appeared in the `heal-systemd-install-drift` stuck-timer cluster. **If the timer wedges, the engine stops ticking entirely** — no reconcile, no dispatch, no heartbeat — and every other fix in this spec is moot because the loop never runs. | `systemd/ourliberty-build-sequence-advancer.timer`; the `heal-systemd-install-drift` stuck-timer cluster; sibling fix PR #225 (cycle-timer infinity-trap) |

**The compounding shape (incident 2026-06-02, `approvals-queue-rework`):** four steps stranded as V6 silent-misses (class 1). The backstop that should have caught them was itself broken (class 2), and its breakage was invisible (class 3). Operator hand-merged `step-autoclear` + `step-alert-promotion` (`beacon-manual-unstick`). Only *after* the gh-`--repo` fix landed did the backstop start working — visible in the **same** audit log, where `step-cleanup-review` (#259) and `step-retention` (#260) later merged via `actor: advancer-reconcile` with zero operator action. That contrast is the proof the fix works *and* the proof the engine had no way to tell us it was broken for the days in between.

---

## 3. Root-cause analysis

The four classes share one meta-cause: **the engine is a chain of independent shell-outs and hook calls, each of which can fail quietly, and the engine has no model of its own health.** Each fix below was correct in isolation and still failed to generalize.

### 3.1 V6 silent-miss (class 1)

**Cause:** identity resolution by exact `task_id` is brittle the moment a PR's lifecycle spawns a derivative task_id (rebase / rescue / revision). The merge is real; the join key drifts.

**Why the point-fix (PR #187) didn't generalize:** #187 fixed the *literal* miss it found, but exact-match remained the only identity path. PR #216 then added a *second* identity path (the reconcile backstop: match by `pr_url` → `headRefName == forge/<step_id>` → `step_id` substring in title). That is the right structural move — a second, fuzzier identity path that doesn't depend on the join key staying stable. **But the backstop's value is entirely conditional on its `gh` call working** — which it didn't (class 2). So the engine had a load-bearing backstop that never bore load.

### 3.2 Reconcile gh-call failure (class 2)

**Cause:** `gh pr list --repo ourliberty-agent-core …` — a bare repo name where `gh` requires `OWNER/REPO`. rc=1, no rows, `None` returned, reconcile skipped. The `fix-advancer-reconcile-gh-failure` work resolves this with a `_qualify_repo()` helper that prepends `GITHUB_OWNER` (`Larry-Yatch`) when the name is unqualified.

**Why it didn't generalize:** the fix is correct for *this* call. But `gh_pr_says_merged()` (the belt-and-suspenders gate's `gh pr view` leg) has the *same* failure shape — it returns `None` on any non-zero rc and logs nothing at all. And the supabase legs (`chain_event_says_merged` / `chain_event_says_failed`) swallow every exception and return `False`/`None`. **The engine has at least four shell-out / query sites that fail silently; we fixed one.** A fix that doesn't generalize to the other three is a fix waiting to be re-needed.

### 3.3 Swallowed subprocess stderr (class 3)

**Cause:** the pre-fix WARNING logged the fact of failure but not its content. `gh` had already told us exactly what was wrong on stderr; the log threw it away.

**Why it didn't generalize:** even after `_gh_list_merged_prs` was taught to log `rc=` + truncated `stderr=`, that discipline lives in *one* function. `gh_pr_says_merged` still returns `None` with no log line; the supabase `except Exception: return False` blocks still discard the exception type and message. Diagnosability is a property the whole engine needs, not one function.

### 3.4 advancer.timer stuck-timer cadence (class 4)

**Cause:** the engine is a oneshot (`Type=oneshot`, `ExecStart=… --once`) driven entirely by a systemd timer. The daemon is correctly stateless-per-tick — but it ticks **only** while the timer fires. A wedged timer (the failure mode the `heal-systemd-install-drift` cluster surfaced) silently stops the whole engine.

**Why it relates to the others:** classes 1–3 are about a tick that runs but fails quietly. Class 4 is about the tick *not running at all*. PR #225 fixed the sibling cycle-timer's infinity-trap (a `Type=oneshot` unit that should have been `Type=simple` for a long-running loop). The advancer's shape is the inverse — oneshot is *correct* for a `--once` invocation — so the fix here is not oneshot→simple but **timer-recovery resilience**: the timer must self-recover from a wedge, and a missed-tick must be visible.

**The synthesis:** four reactive point-fixes, four blind spots. The engine never asked "am I healthy?" — so a broken backstop, a silent gh failure, and a wedged timer all looked identical to a healthy idle engine: quiet logs, `reconciled_steps=0`, no DM.

---

## 4. Structural fixes (the doctrine)

Four fixes. Each is a rule; per the doctrine-drafting discipline (`agents/beacon/CLAUDE.md` → "Doctrine-drafting discipline — every rule earns enforcement"), each pairs with an `**Enforcement:**` line naming the hard mechanism that keeps it from drifting.

### 4a. No silent shell-outs

**Rule:** Every `subprocess.run` and every external query (`gh`, supabase) in the engine MUST, on failure, log a WARNING (or ERROR) that includes the failure *cause*: the returncode + truncated stderr for subprocesses, the exception type + message for library calls. No code path may discard a failure's cause. This generalizes the `_gh_list_merged_prs` rc+stderr fix to **every** shell-out and query site — specifically `gh_pr_says_merged` (`gh pr view`), `chain_event_says_merged`, `chain_event_says_failed`, and `_connect_supabase`.

**Why:** class 3 cost us *days* of undiagnosable strandings because one WARNING omitted the rc and stderr that named the exact bug. A failure the engine can't explain is a failure the operator can't fix.

**Enforcement:** a unit test (`scripts/tests/test_build_sequence_advancer.py`) that, for each shell-out/query function, injects a forced failure (non-zero rc with a stderr payload; a raised exception) and asserts the emitted log record contains a cause token (`rc=` or the exception class name) and the captured detail. A site that swallows its cause fails the test. CI runs the suite.

### 4b. Self-observable health (heartbeat carries health, not just liveness)

**Rule:** The engine MUST emit a periodic **health** signal — not just an mtime liveness touch — carrying at minimum: `reconciled_steps` this tick, per-repo gh-call success/fail counts, supabase-connect success/fail, and the age of the oldest step still in `dispatched` (stuck-step age). The today-heartbeat (`HEARTBEAT_FILE`, mtime-only, read by `heal_build_sequence_advancer_heartbeat.py`) proves the engine is *alive*; it does NOT prove the engine is *working*. A non-functional reconcile path (class 2) must be visible **within one tick**, not after three strandings.

**Why:** for the entire class-2 window the heartbeat was fresh — the engine *was* alive — while the backstop did nothing. Liveness without health is exactly the blind spot that let a broken backstop hide. A health signal turns "reconciled_steps=0 forever + gh-fail-count climbing" into a surfaceable, alertable fact.

**Enforcement:** the health signal is written to a JSON state file (e.g. `~/agents/blackboard/build-sequence-advancer-health.json`) on every tick via the existing atomic-write helper; a healer (extending the heartbeat-healer pattern) reads it and DMs Larry when `gh_fail_count > 0` AND `reconciled_steps == 0` across N consecutive ticks, OR when stuck-step age exceeds a threshold. A unit test asserts every `tick()` writes a schema-valid health record with all required keys populated from that tick's real counters.

### 4c. Backstop must be exercised

**Rule:** The reconcile path MUST have a smoke/regression test that runs the **real `gh` invocation shape** — the exact argv `_gh_list_merged_prs` builds, including the `_qualify_repo` qualification — against a faithful mock of the service environment, so a future env regression (a re-introduced bare-repo-name bug, an arg-order change, an auth-shape change) is caught in **CI, not prod**. A reconcile backstop that is never exercised is indistinguishable from one that doesn't exist — which is precisely what class 2 demonstrated for days.

**Why:** PR #216's backstop merged, looked correct, and was non-functional in production from the moment it shipped. Nothing exercised the actual `gh` call shape, so the `--repo` format bug sailed through review and ran broken until an operator reproduced it by hand. The test is the difference between "the backstop is wired" and "the backstop works."

**Enforcement:** a regression test (`scripts/tests/test_build_sequence_advancer.py`) that asserts the `gh pr list` argv `_gh_list_merged_prs` constructs is `OWNER/REPO`-qualified (never bare), and an end-to-end reconcile test that drives `_reconcile_dispatched_steps` against a mocked merged-PR list and asserts a stranded `dispatched` step reconciles to `merged` via `apply_step_merged`. CI gate: the suite must pass before merge.

### 4d. Timer-cadence resilience

**Rule:** The advancer's systemd timer MUST adopt the resilient-cadence pattern from the `harden-systemd-timer-recovery` work so a wedged or missed timer self-recovers and a stalled cadence is visible. Concretely: `Persistent=true` (already set) so a missed tick fires on resume; the timer participates in the `heal-systemd-install-drift` stuck-timer recovery; and a missed-tick / stale-cadence condition surfaces via the § 4b health signal (a heartbeat that has not advanced for > 2 timer intervals is a stuck-timer signal). The advancer stays `Type=oneshot` (correct for a `--once` invocation — this is the inverse of the PR #225 cycle-timer infinity-trap, where a long-running loop was wrongly `oneshot`); the fix is recovery + visibility, not a unit-type change.

**Why:** classes 1–3 fail *within* a tick; class 4 stops ticks from happening. Every other fix in this spec is load-bearing only while the loop runs. A wedged timer silently disables the entire engine, and the only current signal is the heartbeat-healer's >15-min staleness DM — which fires late and says only "stale," not "timer wedged."

**Enforcement:** cross-reference + reuse the `harden-systemd-timer-recovery` mechanism (don't re-invent); the stuck-timer recovery is the deny-block-equivalent here. Visibility is enforced by the § 4b health-signal staleness check (heartbeat age > 2× timer interval → stuck-timer DM). A systemd-unit lint/check in CI (matching the existing timer-unit conventions) asserts the advancer.timer keeps `Persistent=true` and is registered with the install-drift healer.

### 4e. Single chokepoint for step finalization (the load-bearing invariant)

**Rule:** A merged PR for a sequence step MUST **always** reconcile to `merged`, via **either** the V6 notifier hook (`apply_step_merged` on exact task_id) **or** the reconcile backstop (`_reconcile_dispatched_steps` via fuzzy identity). The two paths are not redundant safety — they are a **primary (best-effort) + backstop (load-bearing)** pair. The V6 hook is best-effort: it fires fast when the task_id is stable, and is allowed to miss. **The reconcile backstop is the load-bearing guarantee** — it is the path that MUST be correct, tested (§ 4c), and observable (§ 4b), because it is the one that catches everything V6 misses. `apply_step_merged` is idempotent, so both paths firing for the same step is harmless.

**Why:** the 2026-06-02 incident is the whole argument. V6 missed (best-effort, allowed). The backstop should have caught it — and would have, had it been correct/tested/observable. Naming which path is load-bearing tells future implementers where to spend their correctness budget: the backstop, not the hook.

**Enforcement:** the § 4c reconcile regression test *is* the enforcement of this invariant for the backstop leg; the test asserts that a step whose V6 hook never fired (simulated by leaving it `dispatched` with a matching merged PR) still reaches `merged` via the backstop within one tick. Documented as an invariant in the advancer module docstring so the "backstop is load-bearing" framing survives future edits.

---

## 5. Scope: in / out

**In scope (the four — really five — fixes above):**
- 4a — generalize rc+stderr / exception-cause logging to every shell-out and query in the engine.
- 4b — health-carrying heartbeat + healer that surfaces a non-functional reconcile path.
- 4c — backstop smoke/regression test exercising the real `gh` invocation shape.
- 4d — advancer.timer recovery resilience + missed-tick visibility.
- 4e — document + test the single-chokepoint finalization invariant (backstop is load-bearing).

**Out of scope:**
- **Redesigning the DAG model.** The `depends_on` / `current_steps` / step-status state machine stays as-is.
- **Changing the sequence-file schema.** No new fields; `build_sequence_validator.py`'s `REQUIRED_SEQ_FIELDS` / `VALID_*_STATUS` enums are unchanged. (The health signal is a *separate* state file, not a sequence-file field.)
- **Replacing `gh` with the GitHub REST API.** Noted as a **possible future**: a native API client would remove the `gh` subprocess class entirely (no argv-format bugs, structured errors instead of stderr-parsing) — but that is a larger migration with its own auth/rate-limit design, and is not justified by the current incident class. The § 4a discipline makes the `gh` path safe in the meantime.

---

## 6. Acceptance

The hardening is complete when ALL of the following hold:

- [ ] **Zero-touch reconciliation.** A merged PR for any sequence step reconciles to `merged` within one advancer tick (≤ 5 min) with **zero operator action** — no `beacon-manual-unstick` audit entries — including when the V6 hook missed (derivative task_id) and only the backstop fires.
- [ ] **Immediate health visibility.** The engine's per-tick health signal makes a non-functional reconcile path visible **immediately** — a tick with `gh_fail_count > 0` and `reconciled_steps == 0` is surfaceable/alertable within one tick, not discovered after three strandings.
- [ ] **No silent shell-outs.** Every shell-out / query failure in the engine logs its cause (rc + stderr, or exception type + message); the § 4a test passes for every site.
- [ ] **CI catches env regressions.** The reconcile regression test (§ 4c) runs the real `gh` argv shape and fails if the `--repo` qualifier (or equivalent env assumption) regresses — caught in CI, not prod.
- [ ] **Timer resilience.** A wedged/missed advancer.timer self-recovers (install-drift healer) and a stalled cadence surfaces via the health signal (heartbeat age > 2× interval).

---

## 7. Implementation sequence shape

Proposed DAG for the follow-up sequence. Dependencies reflect what each step genuinely needs from a prior step.

```
step-1 (no-silent-shell-outs)  ──┐
                                 ├──> step-3 (backstop regression test)
step-2 (self-observable health) ─┘
                                 
step-4 (timer-cadence resilience) ── depends_on: step-2
```

- **step-1 — No silent shell-outs (§ 4a).** Generalize rc+stderr / exception-cause logging across `gh_pr_says_merged`, the two supabase query helpers, and `_connect_supabase`. **`depends_on`: none.** *(See § 8 — if `fix-advancer-reconcile-gh-failure` has already merged, step-1 narrows to the remaining shell-outs, since the `_gh_list_merged_prs` site is already done.)*
- **step-2 — Self-observable health (§ 4b).** Add the health state file + per-tick population + the surfacing healer. **`depends_on`: none** (independent of step-1; can run in parallel).
- **step-3 — Backstop regression test (§ 4c) + finalization invariant (§ 4e).** The reconcile smoke/regression test and the load-bearing-invariant test/docstring. **`depends_on`: step-1, step-2** — the test asserts on the cause-logging (step-1) and reads the health counters (step-2), so it lands after both.
- **step-4 — Timer-cadence resilience (§ 4d).** Wire advancer.timer into `harden-systemd-timer-recovery` + the missed-tick health check. **`depends_on`: step-2** — the missed-tick visibility piece consumes the step-2 health signal.

step-1 and step-2 share no upstream state and touch different surfaces (logging vs a new state file + healer), so they parallelize cleanly. step-3 and step-4 both gate on their real dependencies, not on conservative ordering.

---

## 8. Relationship to in-flight work

The **`fix-advancer-reconcile-gh-failure`** dispatch (the `_qualify_repo` / `GITHUB_OWNER` fix for class 2) is the **first concrete fix** in this class, and this spec generalizes the pattern around it: that fix made *one* shell-out diagnosable and correct; § 4a makes *all* of them diagnosable, § 4c makes the backstop's correctness CI-enforced rather than incidentally-true, and § 4b makes a future regression of that same fix visible within one tick instead of after days.

**Ground-truth note (verified at authoring, 2026-06-03):** the `fix-advancer-reconcile-gh-failure` work appears to have **already merged to main** — the current `build_sequence_advancer.py` already carries `_qualify_repo()`, the `GITHUB_OWNER` default, and the rc+stderr WARNING in `_gh_list_merged_prs`; and the `approvals-queue-rework.json` audit log shows the backstop firing successfully (`actor: advancer-reconcile`, steps #259/#260) on 2026-06-03, *after* the earlier `beacon-manual-unstick` strandings. If that holds when this sequence kicks off, **step-1 narrows** to the remaining silent shell-outs (`gh_pr_says_merged` + the supabase legs + `_connect_supabase`); the `_gh_list_merged_prs` site is already compliant and serves as the reference pattern the other sites should match.
