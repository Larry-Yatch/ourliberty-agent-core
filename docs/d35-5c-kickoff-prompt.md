# D3.5 commit 5c kickoff prompt (paste into next session)

Read your memory files first, then read these in order before doing anything else:

`docs/operating-manual.md` Part II — specifically the most recent three entries: **"Phase D3.5 commit 5b — Forge↔Mirror revision loop"**, **"Phase D3.5 5b-followup — second-pass review fixes + live-test cascade fixes"**, and the "Verification (live, 2026-05-13)" subsection inside the followup entry that captures the actual live test results (failed test → Bug E discovery → 5b-followup-2 ship → successful PR #6 verification). Together they capture the end-state of D3.5 5b through 5b-followup-2. The full revision loop is wired and live; closing-DM regression test passed at PR #6 with 3-min wall-clock + ~$0.50 cost.

`docs/d3-5-plan.md` — re-read the full plan, especially:
- "Scope reminder" section: what's wired in D3.5 vs what's NOT.
- "Component inventory" section: every file 5c will modify or create. 5c is the **Mirror→Beacon escalation flow** — Beacon's `CLAUDE.md` "How you handle Mirror's escalations" section is the big piece; code is minor.
- "Architectural calls that need verification before coding" section, items related to 5c:
  - Item 8: cost budget enforcement (cost_per_task_usd from loop_bounds) — does 5c activate it or defer to 5d? (Currently 5c-or-5d open.)
  - Item 5: max_replans default (1-5 dial) — same shape as max_revisions. Current default 2 (in loop_bounds).
- "Sequencing" section: 5c is the THIRD sub-commit. Ships Beacon's auto-revise-spec on ESCALATE. NOT yet shipping auto-merge (5d), EMERGENCY_HALT trip (5d), or branch protection (5d-design checkpoint).
- "Open questions to surface" section: 4 items not all yet decided. Walk through with Larry, classify per the feedback memory.

`scripts/outbox_notifier.py` — read the existing structure:
- `_classify_mirror_marker` (lines ~880-1010) — handles the 4 Mirror marker types. For ESCALATE specifically, currently routes to Beacon as `intent=review-escalate` with the reason field. 5c extends Beacon's handling, not the classifier itself.
- `INTENT_ACTION_BLOCKS['review-escalate']` (around line 230) — current template says "Decide manually: revise the spec or push back. 5c will wire the auto-replan loop." This is where 5c lights up.
- The marker-error cascade pattern (`_notify_forge_marker_error`, `_notify_mirror_marker_error`) — 5c may need a new `_notify_replan_error` if Beacon's replan response is malformed, OR may rely on existing patterns.
- `_maybe_dm_larry` + `DM_TEMPLATES['review-escalate']` — already in place from 5a-followup; 5c doesn't change this (Larry still gets DM on ESCALATE).

`agents/beacon/CLAUDE.md` — read Shape 8 (review-escalate) which already documents three trigger scenarios (direct Mirror escalate / auto-promote from low-confidence revision / budget-exhausted from 5b). 5c gives Beacon an automated handling path for these — when she can revise the spec inline, she emits a new APPROVAL_REQUEST automatically; when she can't, she still escalates to Larry. **The architectural call to surface: does Beacon's auto-replan need a budget counter (max_replans) parallel to max_revisions?** Per loop_bounds the default is 2. The question is whether Beacon emits a SECOND APPROVAL_REQUEST after one revised-spec round fails — or whether escalation back to Larry happens after one Beacon revision attempt.

`scripts/mirror_review_handler.py` — read the existing `evaluate_revision_budget` + `build_budget_exhausted_reason`. 5c may add a symmetric `evaluate_replan_budget` + `build_replan_exhausted_reason` if `max_replans` is enforced at the notifier level (rather than at Beacon's discretion).

`scripts/beacon_approval_handler.py` — Beacon's APPROVAL_REQUEST marker extraction + dispatch_approved. 5c may need to extend the entry shape to carry `replan_count` + `max_replans` through the chain so Beacon's auto-replan doesn't recurse forever.

`config/agent-models.json` `loop_bounds` — already has `max_replans: 2` from 5a. 5c activates it; no schema change.

`scripts/tests/test_outbox_notifier.py` — review the `RevisionLoopTest` + `RevisionFollowupFixesTest` patterns. 5c's new test class will mirror these shapes for Beacon's replan flow.

Verify droplet state:
- `ssh larry@134.209.44.80 'systemctl list-units "ourliberty-*" --type=service --all --no-pager | head -20 && echo --- && systemctl list-timers "ourliberty-*" --all --no-pager | head -15 && echo --- && git -C ~/agent-core log --oneline -10'` — should see HEAD past `4c79450` (5b-followup-2) + the merge commits for PRs #5 and #6 + any Pulse cycles that ran between sessions.
- `ssh larry@134.209.44.80 'ls ~/agents/state/in-flight/ && ls ~/agents/inboxes/*/ && tail -3 ~/agents/blackboard/larry-alerts.jsonl && echo offset: $(cat ~/agents/state/beacon-alerts-offset.txt)'` — should be empty in-flight + empty inboxes; queue and offset should match. System quiescent.
- `ssh larry@134.209.44.80 'cd ~/agent-core && python3 -m unittest discover scripts/tests/ 2>&1 | tail -3'` — should see "Ran 478 tests ... OK" (current baseline; 5c will add ~30 more).
- `ssh larry@134.209.44.80 'python3 ~/agent-core/scripts/watchdog.py 2>&1 | tail -3'` — should see overall=healthy.

Verify origin state:
- `gh pr list --repo Larry-Yatch/ourliberty-agent-core --state open` — should be empty (PR #6 merged by end of last session; if it's still open, that's the first item: `gh pr merge 6 --squash --delete-branch`).
- `git ls-remote --heads origin | grep -E "forge/|mirror/"` — should NOT show stale branches. If it does, branch cleanup is the second item: `git push origin --delete <branch...>`. Specifically the 4 `forge/marker-error-*` branches + the Mirror checkpoint branches from PR #5/#6.

Then summarize back to me:

1. **What state the system is in (1 paragraph).** Especially: did any Pulse cycles run between sessions? Did the test count match 478? Is the inbox clean? Are there outstanding PRs from the last session that need merging first?

2. **What 5c will ship** (bullet list of files to modify/create with line-range estimates). Cite specific upstream patterns (5b's `_dispatch_revision_to_forge`, Beacon's APPROVAL_REQUEST emission from 4a, the budget-exhaust downgrade pattern from 5b) the new code mirrors.

3. **Architectural decisions signed off in the d3-5-plan that need verifying haven't drifted:**
   - Beacon emits a new APPROVAL_REQUEST when she decides to revise (no new marker grammar — reuses 4a's existing APPROVAL_REQUEST shape).
   - Larry approves the revised plan via the existing Telegram flow.
   - `max_replans` from `loop_bounds` is the budget cap.

4. **Architectural decisions NOT pre-decided that need your input before coding (classify each as TECHNICAL / ARCHITECTURAL / VALUES per the feedback memory, surface the load-bearing ones as A/B or 1-5 dial):**
   - **Replan-count tracking shape.** Where does `replan_count` live in the chain? Options: (A) Beacon's APPROVAL_REQUEST marker payload carries it (envelope-local); (B) the bot's dispatch_approved propagates it through the new task envelope (system-controlled); (C) a per-task state file (e.g. `~/agents/state/replan-<task_id>.json`). Trade-offs around tracking durability vs Beacon-visible state.
   - **What triggers Beacon's auto-replan vs manual escalate?** Beacon receives ESCALATE notify with a reason. Options for her decision: (A) always try one auto-replan, escalate to Larry only if Mirror REVIEW_ESCALATEs again; (B) Beacon judges the reason text and decides; if it's "spec ambiguity," replan; if it's "approach is wrong," escalate to Larry; (C) Larry always sees the first ESCALATE, decides whether to authorize auto-replan. Currently 5a/5b's behavior is closest to (C) by-default (auto-replan not wired; Larry sees ESCALATE DM).
   - **Discipline gate on Beacon's replan emission.** Mirror's revision phase has a strict gate (preamble required). Should Beacon's auto-replan have a similar gate (e.g., the new APPROVAL_REQUEST must have a `replan_count` field; missing → marker-error)? Or trust Beacon's CLAUDE.md alone?
   - **Cost budget activation.** Does 5c also wire `cost_per_task_usd` (per d3-5-plan item 8) — sum cost across all dispatches by task_id, pause further dispatches at the cap? Or defer to 5d? Trade-off: 5c is the natural place to add it (more code paths to gate); 5d focuses on auto-merge.

5. **Any open questions you have before designing/coding.** Especially anything the plan describes that disagrees with what you read in the actual files — trust what you read.

Then we'll go through the open questions and the pre-deploy checklist together. Same shape as 5a/5b walkthrough: classify each architectural call as TECHNICAL / ARCHITECTURAL / VALUES, get sign-off, THEN write code. Do not start implementing until I sign off.

Five principles still in force (added one this session — item 5):
1. **Audit Joe's upstream first.** `/home/larry/gm-agent-core-upstream-mirror/scripts/` and `docs/upstream-audit.md` — does upstream have a Beacon-replan-equivalent? The `council_*.py` files have multi-phase choreography but the audit said don't transplant them.
2. **Build complete, not robust-by-half.** 5b's cascade brittleness (Bug B/C/E) was three layered bugs that each cost real money in live verification. Spend the design budget on every state-loss seam before declaring "tests pass, ship it." Trace every envelope field through every hop on paper before coding.
3. **Classify every architectural call.** Technical = decide-and-move with 2-3 sentence upstream reference. Architectural = brief input. Values = A/B or 1-5 dial with trade in Larry's terms (cost, observability, autonomy, time-to-revisit). The "feedback_plain_overview_before_questions" memory has an addendum from 2026-05-13 specifically about not letting options drift back into implementation framing as the session gets long — re-read it.
4. **Independent code review BEFORE push.** 4a/4b/5a/5a-followup/5b/5b-followup pattern: independent reviewer catches 5-6 issues per pass at ~$0. Don't skip; the cascade-recovery code paths are still under-tested as 5b-followup-2 showed.
5. **Verify reply_chat_id propagation through every hop after any chain change.** The 2026-05-13 Bug E showed that adding a new code path can break the closing-DM regression. Read the trace from `dispatch_approved` (bot) → Forge inbox → Forge outbox → notifier → Beacon notify → build dispatch → Forge build outbox → notifier → Mirror review-request → Mirror outbox → notifier → `_maybe_dm_larry`. Verify each hop carries it.

Specific to D3.5 commit 5c:

This is the THIRD sub-commit of D3.5. The plan estimates ~3 hours design + code + verify — smaller surface than 5b because Beacon's CLAUDE.md is the big piece and her code change is minor (the notifier already routes ESCALATE to her inbox per 5a; 5c gives her structured handling guidance).

Live test target (per the plan's verification budget):
- Unit tests pass on the new test class for replan flow.
- Independent code review caught + fixed issues before push.
- Synthetic Beacon ESCALATE notify → verify routing decisions.
- One end-to-end smoke: deliberately-ambiguous spec → Forge builds X → Mirror REVIEW_ESCALATEs saying spec was unclear → Beacon emits a new APPROVAL_REQUEST with the clarified spec → Larry approves → Forge re-runs → Mirror PASSes → closing DM. Expected cost ~$2.50.

Bonus scope I'd accept (not required):
- Branch cleanup: if there are still stale `forge/marker-error-*` branches on origin from the failed 5b live test (4 of them, plus any Mirror checkpoint branches), delete them as part of 5c's pre-deploy checklist.
- Operating manual Part I: if 5c surfaces a need to update Part I (the canonical "how the system behaves" reference) — e.g. add a "Beacon's replan handling" subsection — do it. Part II tracks build narrative; Part I is the spec.

Don't trust this brief — verify against `docs/d3-5-plan.md`, the operating manual's most recent entries, and the actual code state. If anything I've described here disagrees with what you read, trust what you read and flag the discrepancy.

Start with the read pass and state-summary. Then ask me your open questions before proposing any design changes.
