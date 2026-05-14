# D3.5 — Mirror review chain (plan)

> **Status (2026-05-14): SHIPPED, D3.5 CLOSED.** All four sub-commits live (5a `d908ca6` + 5a-followup `15d046e`, 5b `3f29cfa` + followups `f3f90f7` + `4c79450`, 5c `463c6d8` + followups `033ef1b`/`7f68377`/`0c58c3a`/`957228a`, 5d `8412f82` + ops-manual `ac1bd4a`). Loop fully live: Forge builds → opens PR → Mirror reviews → auto-merge on PASS / Beacon auto-replan on ESCALATE / Forge revision on REVISION / EMERGENCY_HALT trip on safety event. Cost-budget gate enforced at all four dispatch sites. Verification arc lives in `docs/operating-manual.md` Part II — see the 5a/5b/5c/5d entries. This planning doc is preserved as-is for historical context; for current behavior consult the operating manual + agent CLAUDE.md files.

**Status as this plan is written (2026-05-12, after commit 4b + followup-2 + the Pulse digest commit shipped):** D3 is essentially complete. Commit 5 (sentinel + watchdog install) is the last D3 piece and is its own short plan. D3.5 is the next phase — the **review** half of the dispatch chain. D3 made Forge actually write code (preflight → build → PR open); D3.5 makes Mirror actually review the code (review → revise / escalate / pass → auto-merge).

This is the second-biggest commit cluster of the D3 era by design. Realistic pacing: **3–4 sessions of focused design + code + verification**, almost certainly split into multiple sub-commits (likely 5a / 5b / 5c / 5d, see Sequencing below — note these are *D3.5* sub-commits, not D3 commit-5 sub-commits; named the same way because we already did 4a/4b).

---

## Prep work — lands BEFORE 5a

These items aren't D3.5 deliverables proper but they're prerequisites for the first D3.5 live test. Ship as a dedicated pre-D3.5 commit (call it `D3.5-prep`).

### watchdog.py adapter rewrite (deferred from D3 commit 5 per the B-option signoff 2026-05-12)

`scripts/watchdog.py` (610 lines, D2.5 era) is the broad 8-check health monitor with auto-recovery. D3 commit 5 enabled the narrow `dispatch_sentinel.py` timer but deferred enabling the watchdog because `watchdog.py` still carries GM-orchestrator hard-coding that doesn't translate to our 4-bot topology. Concretely:

- `RESTARTABLE_SERVICES = ['ourliberty-orchestrator', 'ourliberty-telegram-webhook']` — neither service exists in our fork. **Replace with:** `['ourliberty-inbox-watcher', 'ourliberty-outbox-notifier', 'ourliberty-beacon-bot', 'ourliberty-forge-bot', 'ourliberty-mirror-bot', 'ourliberty-pulse-bot']`.
- `EXPECTED_SERVICES = ['ourliberty-orchestrator', 'ourliberty-telegram-webhook', 'ourliberty-github-webhook', ...]` — same drop + replace.
- `check_orchestrator()` (D2.5 line ~79) — replace with `check_inbox_watcher()` + `check_outbox_notifier()`; both serve the orchestrator's role in our topology. Same `systemctl is-active` + auto-restart shape.
- `check_orchestrator_memory()` (D2.5 line ~108) — V2 RSS-via-MainPID logic translates directly to the inbox-watcher; replace the service name and the cgroup path. Watcher's `MemoryMax=2G` so the 1 GB hard threshold from upstream is reasonable.
- `check_telegram_webhook()` — drop. We have 4 individual bots, each with its own webhook handler; per-bot liveness already covered by EXPECTED_SERVICES.
- `check_github_webhook()` — drop. We don't have one (we use outbox-poll → notifier dispatch, not GitHub webhook).
- Log-growth check — replace `orchestrator.log` reference with `inbox_watcher.log`.
- The other 4 checks (disk, memory pressure, inbox stale-task detection, token manager status) translate directly.

**Scope estimate:** ~80–150 line edits in `watchdog.py`, plus a test file (`scripts/tests/test_watchdog.py` doesn't exist yet — write it from scratch with the same ephemeral-fixture pattern as `test_worktree_manager.py`). Independent code review pre-push (same pattern as 4a/4b: ~5–6 issues caught at ~$0 cost is wildly favorable).

**Pre-deploy verification:**
- Run `watchdog.py` once manually after the adapter rewrite. Should report all 6 of our services as active and pass all 8 checks (excluding the 2 dropped GM-specific ones, so 6 of 8 will run; document this).
- `systemctl enable --now ourliberty-watchdog.timer`. First fire on `Persistent=true` should be clean, same shape as the sentinel and cleanup-worktrees enables.
- Auto-recovery path test: synthetic stop one bot (`sudo systemctl stop ourliberty-mirror-bot`), wait 5 min for watchdog to fire, verify it restarts.

**Then:** clear to start D3.5 5a.

### Why prep, not part of 5a

The watchdog catches infra failures during D3.5's longer live tests. Without it, a stalled service mid-test would only surface via the sentinel (which detects stalls but doesn't restart). D3.5's revision/escalation loops are long enough that running them on an un-watchdog'd system means a single bot crash mid-test forces a manual restart and lost cost. Cheap insurance — half a session of work — for a phase that'll burn $5–7 in live tests.

---

## Scope reminder

Per the D3 design (Option C, signed off in 2026-05-08 design session): D3 ships the **dispatch** chain. D3.5 ships the **review** chain. They're peers, not parent/child. Once D3.5 lands, the loop is closed: Larry approves a plan → Beacon dispatches → Forge builds → Mirror reviews → either auto-merges or routes back for revision / replan → Pulse digest shows the outcome.

Without D3.5 the system is "Forge builds and opens PRs that pile up." With D3.5 the system is "Forge builds, Mirror reviews, things merge themselves, Larry only sees escalations and the digest."

**Wired in D3.5:**

- Mirror's review prompt template (her `CLAUDE.md`) — how to read a PR diff, what to look for, severity taxonomy, confidence rubric.
- The `REVIEW_*` marker grammar — parallel to Forge's preflight markers (`forge_preflight_handler.py` shape), with three normal outcomes + one safety valve.
- The Forge↔Mirror revision loop — Mirror's `REVIEW_REVISION` marker triggers a new Forge dispatch in the same worktree, on the same branch, under `--resume`. Forge applies the fix, pushes, Mirror re-reviews.
- The Mirror→Beacon escalation loop — `REVIEW_ESCALATE` routes to Beacon, who emits a revised `APPROVAL_REQUEST` (which goes through Larry per existing trust policy).
- Auto-merge on `REVIEW_PASS` — `gh pr merge --squash --delete-branch` fires after Mirror's PASS marker is classified.
- Emergency-halt safety valve — `REVIEW_EMERGENCY_HALT` trips `~/agents/blackboard/EMERGENCY_HALT` and DMs Larry immediately.
- Loop bounds: `max_revisions` (Forge↔Mirror), `max_replans` (Beacon↔Larry→Forge↔Mirror), cost budget per task.
- The preflight-discipline runtime gate deferred from 4b: notifier rejects `phase=preflight` outboxes that don't end with a Forge marker; dead-letters back to Forge.
- Pulse `/cycle` threshold update: open-Forge-PR escalation threshold drops from 24h → ~72h once Mirror's auto-merge is live (only blocked-on-Larry PRs surface).

**NOT in D3.5:**

- CI integration (Mirror reading CI status to factor into review). D3.5 ships without CI on the repo; Mirror reviews diff content only. Future.
- GitHub branch protection rules (the "require Mirror review marker before merge" pattern). Would be belt-and-suspenders against Mirror going rogue; defer unless a real incident motivates it.
- GitHub webhooks → droplet (push-driven Mirror trigger). D3.5 uses outbox-poll trigger (when Forge's build outbox has a "PR opened" line, notifier dispatches to Mirror). Webhook is faster but adds infra; defer.

---

## Component inventory

### New scripts

- **`scripts/mirror_review_handler.py`** — pure-logic marker library, mirrors `forge_preflight_handler.py` shape. Owns: `parse_mirror_marker` (extracts one of `REVIEW_PASS` / `REVIEW_REVISION` / `REVIEW_ESCALATE` / `REVIEW_EMERGENCY_HALT` block), `evaluate_revision_budget` (returns `allow` / `exhausted` like preflight clarification budget but counts revisions), `derive_intent`, `derive_notify_source`, severity/confidence helpers. Stateless. ~250 lines.

- **Mirror review trigger** — extends `outbox_notifier.py` (not a new file). When Forge's build outbox has `PR opened:` in `result`, classifier extracts the PR URL + task_id and writes a `review-request` task to Mirror's inbox with `source: forge`, `phase: review`, `target_repo`, `task_id`, the PR URL, the original spec prompt (so Mirror has full context). New helper `_dispatch_mirror_review(data, pr_url)`.

### Modified scripts

- **`scripts/outbox_notifier.py`** — three additions parallel to the Forge marker plumbing:
  1. `_classify_mirror_marker(data)` returning a routing decision for Mirror's outbox (PASS / REVISION / ESCALATE / EMERGENCY).
  2. `process_outbox` branches: if `agent == 'mirror'` and a marker is found, marker-driven routing fires. PASS → notify Beacon "PR shipped" + invoke `_auto_merge_pr(pr_url)`. REVISION → write revision task to Forge's inbox (similar shape to `_dispatch_build_phase`). ESCALATE → write replan-request task to Beacon's inbox. EMERGENCY → touch EMERGENCY_HALT + DM Larry via her bot's notify channel.
  3. The preflight-discipline runtime gate (deferred from 4b): in the Forge marker classifier, if `data.get('phase') == 'preflight'` and `_classify_forge_marker` returns `None` (no marker found in result), raise `MalformedForgeMarker('preflight outbox must end with a marker block')` so the marker-error cascade fires. Forge re-runs with a sharper "decide, don't act" prompt from the marker-error notify.

- **`scripts/auto_merge.py`** (new helper) OR extend `outbox_notifier.py` with `_auto_merge_pr(pr_url)`. Runs `gh pr merge <N> --squash --delete-branch` from within `~/agent-core/`. On failure (merge conflict, branch protection, etc.) writes a dead-letter back to Beacon and DMs Larry: "Mirror approved but auto-merge failed: <reason>." **Decision: extend `outbox_notifier.py`** — keeps the dispatch logic in one place, no new module.

- **`scripts/dispatch_validator.py`** — extend `ALLOWED_INTENTS` with `review-pass`, `review-revision`, `review-escalate`, `review-emergency-halt`, `replan-request`. Extend `ALLOWED_PHASES` with `review` and `revision` (alongside existing `preflight` / `build`). Extend `ALLOWED_SOURCES` with `mirror-question` (for Mirror clarifying with Beacon mid-review).

- **`scripts/routing_validator.py`** — extend `FRESH_DISPATCH_ROUTES`: `mirror`: {`forge`, `beacon`} (REVISION dispatches to Forge, ESCALATE dispatches to Beacon). Mirror's `worktree_enabled` decision goes in `agent-models.json`.

- **`scripts/inbox_watcher.py`** — minor: skip the worktree creation for Mirror IF her review can be done via `gh pr diff` alone (no checkout needed). OR enable her worktree for the cases she wants to run tests against the PR. **Decision needed (see Architectural calls below).** Default: enable worktree for Mirror so she can checkout the PR branch and run tests; same `worktree_enabled: true` shape as Forge.

- **`agents/mirror/CLAUDE.md`** — substantial. New sections:
  - **Review protocol** — when a `review-request` task arrives, read the spec from the envelope, read the PR diff (`gh pr diff <N>`), optionally check out the branch (`gh pr checkout <N>`) and run tests. Emit one marker.
  - **Marker formats** — `REVIEW_PASS`, `REVIEW_REVISION`, `REVIEW_ESCALATE`, `REVIEW_EMERGENCY_HALT`. Required fields per type (see Architectural calls).
  - **Severity rubric** — `low` / `medium` / `high` for each finding type (correctness bug, security issue, scope creep, style nit, missing test, etc.). Maps severity to marker outcome: low/medium → REVISION (fix in place), high → ESCALATE (replan needed), critical+security → EMERGENCY_HALT.
  - **Confidence rubric** — Mirror reports `confidence: high | medium | low` on REVISION and ESCALATE markers. Low confidence on a REVISION should escalate instead (don't trust auto-loops when uncertain).
  - **What "REVIEW_PASS" requires** — no findings above `low` severity, diff is scoped to `changed_files`, conventional-commit message, PR body has summary + test plan.

- **`agents/beacon/CLAUDE.md`** — new section "How you handle Mirror's escalations." Three shapes:
  - `replan-request` from Mirror with `severity: high` finding — read the finding, decide whether to revise the spec inline (new APPROVAL_REQUEST with adjusted plan) OR push back to Mirror with "actually that's intended" (clarify-back leg). Bounded by `max_replans` budget on the envelope.
  - `review-pass` notify (informational) — Beacon journals "PR #X shipped from task <id>." No further action.
  - `review-emergency-halt` notify — Beacon journals + DMs Larry. EMERGENCY_HALT is already tripped.

- **`agents/forge/CLAUDE.md`** — extend the Build phase protocol section with a new "Revision phase" subsection. When a `review-revision` task arrives (under `--resume` of her build session), apply Mirror's findings as targeted diffs, commit (revision-N message), push to the same branch. Same worktree, same task_id, same branch. Emit a plain-text "Fixes applied:" result, no marker (revision phase has no marker — Mirror's next review is the gate).

- **`config/agent-models.json`** — Mirror gets `worktree_enabled: true` + `allowed_repos: ["ourliberty-agent-core"]` (same as Forge). Plus a new top-level `loop_bounds` block:
  ```json
  "loop_bounds": {
    "max_revisions": 3,
    "max_replans": 2,
    "cost_per_task_usd": 5.0
  }
  ```

### New systemd / config

- No new systemd units. Mirror's review fires via the existing inbox-watcher when the notifier writes a review-request task to her inbox. Auto-merge fires synchronously inside the notifier.

### Tests

- **`scripts/tests/test_mirror_review_handler.py`** — marker extraction (4 types + narrative stripping), malformed-JSON / missing-field rejections, multi-marker rejections, severity/confidence parsing, revision-budget evaluation.
- **`scripts/tests/test_outbox_notifier.py`** extended — three new test classes: `ClassifyMirrorMarkerTest`, `MirrorMarkerRoutingTest` (covers each marker type's downstream effects: PASS → auto-merge call + Beacon notify; REVISION → Forge revision dispatch; ESCALATE → Beacon replan-request dispatch; EMERGENCY → halt flag + Larry DM), `AutoMergeTest` (mocks `gh pr merge`, covers success / conflict / branch-protection-deny / network-error paths).
- **`scripts/tests/test_routing_validator.py`** extended — Mirror's new fresh-dispatch routes.
- **`scripts/tests/test_dispatch_validator.py`** extended — new intents + phases.
- Integration test: synthetic Forge build outbox with "PR opened:" → notifier dispatches review-request → Mirror outbox with REVIEW_PASS → notifier calls (mocked) auto-merge + writes Beacon notify. End-to-end without claude.

---

## Architectural calls that need verification before coding

Most are already discussed in conversation; sign-off pre-implementation per the feedback memory.

1. **Marker grammar — 4 outcomes vs 3.** Three normal (PASS / REVISION / ESCALATE) + one safety valve (EMERGENCY_HALT). **VALUES — recommend 4.** Skipping EMERGENCY_HALT means a future security issue has no agent-level stop; Mirror would have to write a regular ESCALATE and Beacon would have to interpret. Better to have an explicit, machine-checked panic button.

2. **Severity judgment — Mirror decides vs Beacon decides.** **ARCHITECTURAL — recommend Mirror decides** (with `confidence` field; low-confidence REVISION auto-promotes to ESCALATE). Alternative: every Mirror finding goes to Beacon, who routes. Pro of Mirror-decides: fewer hops, faster loop. Con: Mirror might miscalibrate scope. The `confidence` field is the hedge — uncertain → escalate by default.

3. **Worktree reuse on replan.** **ARCHITECTURAL — recommend same `task_id`, same worktree, by default. Mirror's escalate marker can hint `worktree: fresh` if she's saying "different approach entirely."** Re-use is faster (branch already pushed) but diff history gets noisy. Fresh is cleaner but slower. Per-replan judgment Beacon makes; she's prompted with the choice when she emits the new APPROVAL_REQUEST.

4. **Mirror's `worktree_enabled` — yes or no?** **TECHNICAL — recommend yes.** Lets Mirror `gh pr checkout` and run tests against the PR branch as part of review. Without a worktree she can only read the diff text (`gh pr diff`) which is enough for simple reviews but blocks her from running the test suite or checking compile/lint. Cost: same as Forge — keyed by `task_id`, reuses across revision loops, persists across CLARIFY round-trips. Decide-and-move.

5. **Loop bounds defaults.** **VALUES — recommend `max_revisions: 3`, `max_replans: 2`, `cost_per_task_usd: 5.0`.** First number: Forge gets 3 attempts to satisfy Mirror before forced escalation. Second: Beacon gets 2 replans before the system surfaces "we're stuck, take a look" to Larry. Third: a single logical task (one Beacon APPROVAL_REQUEST and all downstream dispatches it spawned) can spend $5 before pausing. Today individual invocations cost $0.10–0.60, so $5 = ~10 invocations = a stuck loop. Revisit defaults after 10+ live runs. Numbers are 1–5 dial territory — Larry's gut.

6. **Auto-merge flag set.** **TECHNICAL — recommend `gh pr merge <N> --squash --delete-branch`.** Squash because Forge's history is `[WIP][session-start] + actual change + (optional revision commits)` and we want one clean commit on main. Delete-branch because the branch is task-keyed and useless after merge.

7. **Mirror review trigger.** **TECHNICAL — recommend outbox-poll (Forge's build outbox carrying `PR opened:` triggers a notifier-issued review-request to Mirror).** Matches existing patterns; no new infra. Webhook-driven trigger is faster (~seconds vs ~minutes for next watcher poll) but needs GitHub webhook → droplet which we don't have. Decide-and-move.

8. **Cost budget — what counts as "one task"?** **ARCHITECTURAL — recommend an envelope-bound concept: every dispatch keyed by the same `task_id` shares one budget. Sum across all dispatches (preflight + clarifications + build + revisions + mirror reviews + replans) per the cost ledger.** Pulse's existing `~/agents/blackboard/costs.jsonl` writes one record per claude invocation; notifier sums by `task_id` and gates further dispatches at the threshold.

9. **Preflight-discipline runtime gate — strict (dead-letter) or warning (log + proceed).** **VALUES — recommend strict.** Soft warning means Forge keeps fast-pathing and the discipline never gets enforced. Strict means a missed marker costs one extra Forge invocation; cheap. Revisit after Forge has been disciplined for 20+ runs.

---

## Sequencing (D3.5 will split into multiple commits)

Pacing realism: D3 commit 4 split into 4a + 4b mid-design when the worktree machinery turned out to be substantial enough to deserve its own design pass + verification window. D3.5 has at least as much surface, so expect splitting.

**Likely split:**

- **D3.5-mirror-review (a)** — review marker pipeline (parallel to 4a's preflight markers). Ships `mirror_review_handler.py`, the `_classify_mirror_marker` classifier, the `REVIEW_*` marker grammar, Mirror's CLAUDE.md review-protocol section, the preflight-discipline runtime gate (since we're already in the marker-error code path). Live test: synthetic Mirror outbox with each marker type; verify routing decisions; no auto-merge yet (PASS just logs a journal entry on Beacon's side, Larry merges manually for the test runs).
- **D3.5-revision-loop (b)** — Forge↔Mirror revision cycle. Ships the REVISION marker handler that writes revision-tasks to Forge, Forge's CLAUDE.md "Revision phase" section, the `max_revisions` budget. Live test: deliberately-flawed Forge build that Mirror flags; Forge fixes; Mirror passes.
- **D3.5-escalation (c)** — Mirror→Beacon ESCALATE chain. Ships the escalate marker handler, Beacon's CLAUDE.md "Handle Mirror escalations" section, the `max_replans` budget. Live test: spec ambiguity that Mirror catches; Beacon replans; Larry approves the revised plan; Forge re-runs; Mirror passes.
- **D3.5-auto-merge (d)** — `gh pr merge` integration + EMERGENCY_HALT valve. Ships the `_auto_merge_pr` helper, the EMERGENCY_HALT trip + DM, the Pulse digest threshold update (24h → 72h). Live test: the watchdog-doc-fix shape from D3-4b's smoke, but now end-to-end including auto-merge. Plus a synthetic EMERGENCY trigger.

**Don't merge 5a + 5b into one commit** — 5a's risk is the marker grammar (low: same shape as Forge's preflight markers, well-tested). 5b's risk is the actual revision loop running in Forge's worktree under `--resume` (higher: untested integration). Splitting gives each its own verification window.

---

## Verification plan

Same shape as D3 commit 4's plan:

**Per-commit unit tests:** all existing 236+ tests still pass; new tests for each component as listed under Tests above.

**Per-commit live smoke:** dedicated synthetic task that exercises the new behavior end-to-end. Cost estimates per smoke:

- 5a: ~$0.50 (one Forge build that closes with a marker + one Mirror review that emits a marker)
- 5b: ~$1.50 (forced REVISION cycle = one extra Forge revision + one extra Mirror re-review)
- 5c: ~$2.50 (forced ESCALATE cycle = Mirror review + Beacon replan + Larry approval round-trip + Forge re-run + Mirror re-review)
- 5d: ~$1.00 (auto-merge + EMERGENCY trip with a synthetic safety scenario)

**Total D3.5 live verification budget: ~$5–7.** Comparable to 4b's two smokes that totaled $2.50.

**Independent code review (subagent, same as 4a/4b pattern) before push on each sub-commit.** The 4a review caught 5 real issues; 4b review caught 6. Pattern-matched value at ~$0 review cost.

---

## Pre-deploy checklist (do not skip)

Before any sub-commit's `git push` + droplet sync + service restarts:

- [ ] **`D3.5-prep` shipped first** — `watchdog.py` adapted to our topology + `ourliberty-watchdog.timer` enabled + auto-recovery path verified. See Prep work section above.
- [ ] No in-flight tasks (`ls ~/agents/state/in-flight/`).
- [ ] Local + droplet test suites pass after sync.
- [ ] `gh auth status` shows `Larry-Yatch` with `repo` + `workflow` scopes (already verified pre-4b, but re-check pre-5a).
- [ ] `agent-models.json` has the new fields for the commit shipping: Mirror's `worktree_enabled` + `allowed_repos` (5a), `loop_bounds` block (5b), etc.
- [ ] For 5d specifically: branch-protection rules on `main` reviewed. Default is none — auto-merge from the droplet works directly. If we add branch protection later, the auto-merge path needs a corresponding bypass token.
- [ ] Independent code review run (subagent assessment) BEFORE push. Caught 5+6 real issues across 4a+4b; expect similar.

---

## Estimated depth

- **D3.5-prep (watchdog adapter):** ~3 hours. Lines edits in `watchdog.py` (~80–150) + new test file + manual verification + first-fire + auto-recovery path test. See Prep work section.
- **5a (Mirror review marker pipeline + preflight gate):** ~5 hours design + code + verification. Same shape as 4a; should go faster the second time.
- **5b (revision loop):** ~4 hours. New code is small; integration risk is the unknown.
- **5c (escalation loop):** ~3 hours. Beacon's CLAUDE.md is the big piece; code is minor.
- **5d (auto-merge + EMERGENCY):** ~4 hours. `gh pr merge` mechanics are simple; EMERGENCY_HALT plumbing needs careful thought about which services need to halt on what evidence.

**Total D3.5 (incl. prep): ~19 hours of focused work across 4–5 sessions.** Spread across a week or two of multi-session pacing.

---

## Risk flags

- **First time a PR auto-merges to main without human review.** Mitigation: strict severity threshold (Mirror PASS requires no findings ≥ medium); high-confidence-only PASS in 5d's initial deployment; revisit after 10+ clean runs.
- **Loop unboundedness.** A bug in `max_revisions` enforcement could let Forge↔Mirror cycle forever. The `cost_per_task_usd` budget is the second-line bound (10 invocations max even if loop count is broken).
- **EMERGENCY_HALT is broad.** Tripping it stops *all* agents on next poll. That's the right behavior for security/data-loss issues, but a false positive halts everything until Larry investigates. Mitigation: Mirror's CLAUDE.md gives narrow criteria for emitting EMERGENCY_HALT (credentials in diff, destructive migration, agent dispatching outside its repo allowlist) so it's hard to misfire.
- **Mirror miscalibrates and approves bad code.** No CI on the repo today means Mirror is the only review. If she has a blind spot for a class of bug, it ships unreviewed. Mitigation: start with PASS requiring high confidence + zero medium+ findings. Periodically audit a sample of merged PRs by hand for the first month.
- **Replan loops.** Beacon could emit revised plans that Mirror keeps rejecting. `max_replans: 2` caps this — third rejection forces surface-to-Larry with "we're stuck, here's what's happening."

---

## What to read first when picking up D3.5

1. **`docs/operating-manual.md` Part II, the most recent phase entry (4b followup-2).** Captures the full state of the dispatch chain at the start of D3.5 design.
2. **`docs/d3-commit-4-plan.md` (this doc's older sibling).** Same shape; D3.5 is essentially the review-chain twin of the dispatch-chain plan.
3. **`scripts/forge_preflight_handler.py`** + **`scripts/outbox_notifier.py` lines ~337–710** (marker classification + marker-driven routing in process_outbox). The Mirror review pipeline mirrors this shape.
4. **`scripts/worktree_manager.py`** + **`scripts/inbox_watcher.py` lines ~287–400** (process_task with worktree wiring). Mirror's review-with-worktree path follows the same shape.
5. **`agents/forge/CLAUDE.md` "Preflight discipline" + "Build phase protocol" sections.** Mirror's CLAUDE.md will be structured the same way: read the inputs, apply judgment, emit one marker.
6. **`agents/beacon/CLAUDE.md` "How you handle Forge's preflight markers" section.** Beacon's handling of Mirror's escalations follows the same shape (one section per shape, decision fork per shape).
7. **The 4b Part II entry's "Codified conventions worth recalling" list.** Items 1–3 (envelope-fields-propagate-through-every-hop, session_id-gating, marker-driven-routing-bypasses-default-filters) apply identically to D3.5.

---

## Open questions to surface in the design session

These aren't yet decided; flag them in the live walkthrough:

- **Does Mirror always review every Forge PR, or do trivial PRs get auto-merged without review?** Probably always-review for the first 30 days. After that, a "type=doc-only, diff ≤ 10 lines" carve-out might make sense — but only if Mirror's history shows zero false-passes on that class.
- **What's Mirror's escalation channel when her own review process fails (e.g., she can't read the diff, gh times out)?** Probably dead-letter to Beacon with `review-process-failure` intent. Beacon journals + DMs Larry.
- **Auto-merge + uncommitted local changes scenario.** If the droplet's working tree on `~/agent-core` has uncommitted changes (it shouldn't, per discipline), does auto-merge proceed anyway? `gh pr merge` works on the remote; local tree state is irrelevant. Confirmed safe but worth documenting.
- **Mirror's clarification-back leg.** Forge has a CLARIFY_REQUEST that goes back to Beacon (D3-4a). Should Mirror have a parallel — `REVIEW_QUESTION` that goes back to Beacon mid-review? Probably yes for parity. Bounded by the same `max_clarifications` shape. Scope decision for 5a vs 5b.
