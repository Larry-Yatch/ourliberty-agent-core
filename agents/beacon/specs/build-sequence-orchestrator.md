# Spec: Multi-Step Build Sequence Orchestrator

**Status:** Draft (awaiting Larry approval — Phase E follow-on, prerequisite for Pulse cycle upgrade dispatches)
**Author:** Claude (written 2026-05-26, conversational design pass with Larry)
**Approver:** Larry (pending)
**Phase:** Phase E follow-on. Not part of the Pulse cycle upgrade per se; it is the **prerequisite capability** that makes the Pulse upgrade (PR-α₁/α₂/β/γ) and all future multi-PR builds hands-free.
**Predecessor:** PR #114 (AUTO_MERGE serializer + mergeable gate), E4.4d PR-A/B (chain_events table + ingestion daemon).
**Successor:** The Pulse cycle upgrade dispatches are the first real-use test of this orchestrator.
**Companion docs:** `agents/beacon/specs/pulse-cycle-upgrade.md` § 12.5 (the reciprocal cross-reference — that spec's PR-0 amendment hands the multi-step build orchestration scope off to this spec); `agents/beacon/CLAUDE.md` § "How you dispatch work to Forge" (extends with new multi-step authoring discipline, see also § "How you author multi-step build sequences" added by PR-S1).

---

## 1. Problem statement

Every multi-PR build today requires Larry to drop dispatches one at a time, watch for merge DMs, and paste the next dispatch. The Pulse cycle upgrade alone is a 5-PR sequence (PR-0 + PR-α₁/α₂/β/γ) and would cost Larry ~9 manual paste-touches if dispatched the old way. Future builds will be larger and many will have parallel sub-tasks Larry has to schedule by hand.

Larry's 2026-05-26 framing:

> We should also look at here is that the spec should live in a different document than they go to, as opposed to in the chat text. That keeps them going to a single source of truth, and we don't have to post long things in the chat.
>
> We should have something or someone assess what can be run in parallel versus what can be run individually or in sequence that's dependent... We should think about putting something in the operations tab that shows the multi-step process, almost like a ladder: what's running in parallel, what's running sequentially, and then a simple, visible tag on what state it's in and who's touching it.

The orchestrator turns a "kick off this 5-step build" intent into a hands-free sequence with visible state, automatic gate-watching, parallel-where-safe, sequential-where-required, and Larry-DM only at key transitions.

The Pulse cycle upgrade spec hands the multi-step orchestration scope off to this spec in its § 12.5 (post-2026-05-26 design pass) — both specs co-evolved during the same conversational design pass and reference each other deliberately. See `agents/beacon/specs/pulse-cycle-upgrade.md` § 12.5 for the reciprocal scope-handoff statement.

Joe's `gm-agent-core` does not have this capability; this is our innovation on top of his pattern. The Pulse cycle upgrade (which adapts Joe's `/cycle` doctrine) becomes natural successor consumer — post-Pulse-merge, Pulse layers LLM-judgment on top of the orchestrator's deterministic gate-watching.

---

## 2. Decisions locked (from 2026-05-26 design pass)

| # | Decision | Locked value | Rationale |
|---|---|---|---|
| A | Concurrent sequences | **One active sequence at a time in V1.** Hotfix one-off PRs via the normal APPROVAL_REQUEST flow can still run alongside an in-flight sequence; only a SECOND multi-step sequence is blocked until the first finishes or is canceled. | Simpler state machine, fewer edge cases, no resource contention risk with Forge/Mirror serialization. Lifting to N is a small change in V2. |
| B | DM volume for progress | **Key transitions only.** DM on sequence kickoff, each PR merge (advance event), and on failure / completion. Skip individual Forge/Mirror state changes because the existing chain notification path already DMs those. | Per `feedback_pulse_triages_operational_signals`: Larry's attention is the scarcest resource. The sequence layer DMs only the meta-state to avoid double-DMing. |
| C | Ladder UI scope for V1 | **Full ladder visualization** from V1. Parallel branches side-by-side; sequential dependencies vertical; lines connecting dependent steps; who's-touching-it tag per node; color status (green merged, blue in-progress, yellow paused-for-approval, red failed). | "Ladder" is the load-bearing metaphor in Larry's framing. Minimal V1 risks shipping the right data with the wrong shape and forcing a V2 rebuild. |
| D | Sequencing of workstreams | **PR-0 of Pulse upgrade + orchestrator design run in parallel.** PR-0 dispatches manually now; orchestrator design proceeds concurrently. Pulse upgrade dispatches (PR-α₁/α₂/β/γ) fire through the orchestrator once it is live and hardened. | PR-0 is independent of the orchestrator; no value in serializing them. Pre-staging the Pulse dispatches before the orchestrator's authoring interface is final risks rework. |
| E | DAG declaration syntax | **JSON with explicit `depends_on` field per step.** No clever shorthand. Each step lists the step IDs it depends on; tooling validates the DAG (no cycles, no missing references). | Machine-readable, Beacon-synthesizable, no derivation ambiguity. Human authors can still read and edit it. |
| F | Parallelism assessment | **Author declares, Mirror preflight verifies.** Author (Beacon synthesizing from Larry's intent, or Pulse post-upgrade) declares `depends_on`. A Mirror preflight pass before the sequence is committed verifies the declared DAG by checking for file-overlap conflicts between steps marked parallel. | Authors know intent; tooling verifies. Mirror cost (~$2 + 5 min) is small relative to the value of catching missed dependencies before kickoff. |
| G | Advancer process model | **Separate daemon, not part of Beacon's bot process.** `build_sequence_advancer.service` polls `chain_events` every 5 min, advances state, writes the next step's envelope to Beacon's inbox via the existing headless-dispatch path. | Same separation as `chain_event_shipper.service` vs. the bot. Polling is deterministic infrastructure; bot is conversational. Decouples sequence-lifetime from bot-uptime. |
| H | Belt-and-suspenders gate check | **Both `chain_events` AND `gh pr view` must confirm merge** before the advancer advances to the next step. Neither alone is enough. | Defense in depth. A bug in chain_events ingestion (e.g., dropped events under buffer pressure) could otherwise cause premature advance; a bug in `gh` (e.g., stale cache) could otherwise stall the sequence forever. Requiring both closes both failure modes. |
| I | Failure-handling granularity | **Pause whole sequence on any step failure in V1.** All branches (including parallel ones) freeze. Larry sees a failure DM with the failure mode + a `resume <seq-id>` / `cancel <seq-id>` Beacon shortcut. | Parallel branches may have implicit dependencies the DAG doesn't capture (shared infrastructure, downstream PR cycles). Pausing all is the safe default. V2 can introduce branch-level pause. |
| J | Authoring path | **Beacon synthesizes the sequence file from Larry's intent.** Larry says "build X across multiple PRs"; Beacon's PLAN_SYNTHESIS_DISCIPLINE extends to producing a spec doc + sequence file + a single kickoff APPROVAL_REQUEST. Larry's role in authoring is approving Beacon's plan, not writing the sequence file. | Aligns with Larry's directive "we may have to retrain Beacon to write specs in this way because ultimately we're going to want the machines doing it, not external authors." |

---

## 3. Success criteria

The orchestrator is working when ALL of the following are true:

- Larry can kick off a multi-step sequence with one Beacon paste; the sequence runs hands-free until completion, failure, or his explicit pause.
- The Pulse cycle upgrade (PR-α₁/α₂/β/γ) runs through the orchestrator as its first real-use test and completes with at most 1 manual touch from Larry (the kickoff paste). Failure modes that fire pause-DMs are counted as expected behavior, not failures of the orchestrator itself.
- Operations tab shows the ladder view of the in-flight sequence with all four state colors visible at appropriate moments (green merged, blue in-progress, yellow paused, red failed) and the who's-touching-it tag updates in real-time.
- The advancer daemon survives a droplet reboot mid-sequence and resumes correctly from `~/agents/blackboard/build-sequences/<seq-id>.json` without reprocessing already-merged steps.
- Beacon's PLAN_SYNTHESIS_DISCIPLINE extension produces a syntactically valid sequence file + spec doc from Larry's "build X" intent on the first synthesis attempt for at least 4 out of 5 trial intents during the orchestrator's first month live.
- Belt-and-suspenders gate check prevents at least one false-positive advance during the orchestrator's first month live (verified via deliberate test: drop a synthetic AUTO_MERGE event into chain_events without a real PR merge; advancer must NOT advance).

---

## 4. Out of V1 scope (explicit deferrals)

- **Concurrent multi-step sequences.** V1 allows one active sequence at a time per decision A. V2 can lift this once the single-sequence path is hardened.
- **Branch-level pause-resume.** V1 pauses the whole sequence on any step failure per decision I. V2 can introduce per-branch pause when the failure is provably isolated.
- **Automatic parallelism inference from code analysis.** V1 requires authors to declare `depends_on` explicitly per decision F. V2 may add a static-analysis pre-pass that proposes additional parallelism based on file-overlap analysis.
- **Cross-repo sequences.** V1 assumes all steps in a sequence target the same `target_repo`. Multi-repo orchestration (e.g., a sequence that touches both `ourliberty-agent-core` and `ourliberty-dashboard`) is V2.
- **Pulse-as-driver.** V1 ships the advancer as a deterministic cron-driven daemon. Post-Pulse-cycle-upgrade, Pulse takes over advancement-with-judgment (see § 5.7) — that's a follow-on integration PR, not part of this spec's V1.
- **Sequence templates / replay.** V1 treats each sequence as one-shot. V2 may add a template-and-replay capability for recurring build patterns (e.g., "the standard 4-PR feature-flag rollout sequence").
- **External orchestration hooks.** V1 has no webhook or external-trigger surface; sequences are kicked off only via Beacon's APPROVAL_REQUEST flow.

---

## 5. Architecture

### 5.1 Sequence file schema

**Path:** `~/agents/blackboard/build-sequences/<seq-id>.json`

**Schema (V1):**

```json
{
  "seq_id": "pulse-upgrade-001",
  "label": "Pulse cycle upgrade — PR-α₁ through PR-γ",
  "spec_doc": "agents/beacon/specs/pulse-cycle-upgrade.md",
  "created_at": "2026-05-27T15:00:00-06:00",
  "created_by": "beacon",
  "status": "active",
  "current_steps": ["alpha-1"],
  "steps": [
    {
      "step_id": "alpha-1",
      "label": "PR-α₁ — cycle-prompt.md core doctrine",
      "depends_on": [],
      "dispatch_text": "Beacon, dispatch PR-α₁ per spec § 6 PR-α scope, part 1 (~1200 lines covering multi-tier cadence, 5 original checks, PRIME DIRECTIVE accounting, pipeline-driver, Phase 4 verification, WARN-vs-INFO, tier-state-machine). Mirror review per spec § 6.",
      "target_repo": "ourliberty-agent-core",
      "task_type": "feature-development",
      "expected_cost_usd": 6,
      "status": "pending",
      "dispatched_at": null,
      "merged_at": null,
      "pr_url": null,
      "current_actor": null,
      "failure_reason": null
    }
    // ... more steps
  ],
  "audit_log": [
    {"ts": "2026-05-27T15:00:00-06:00", "event": "sequence-created", "actor": "beacon"},
    {"ts": "2026-05-27T15:00:05-06:00", "event": "kickoff-dispatched", "step_id": "alpha-1", "actor": "advancer"}
  ]
}
```

**Field semantics:**

- `seq_id` — stable kebab-case identifier; must be unique across all sequences (active or archived).
- `spec_doc` — path to the canonical spec; the dispatch_text for each step references sections of this spec.
- `status` — one of `pending` (created but not started), `active` (kickoff fired), `paused` (Larry paused or step failed), `complete` (all steps merged), `failed` (canceled or unrecoverable failure), `archived` (post-complete after 30d).
- `current_steps` — array of step IDs currently in flight (multiple entries when parallel branches active).
- `steps[].depends_on` — array of step IDs that must reach `merged` before this step can be dispatched. Empty array means no dependencies (kickoff candidates).
- `steps[].status` — one of `pending` (waiting on deps), `dispatchable` (deps met, not yet sent to Beacon), `dispatched` (sent to Beacon's inbox), `building` (Forge active), `reviewing` (Mirror active), `merged` (AUTO_MERGE confirmed by both `chain_events` + `gh pr view`), `failed` (REVISION-exhausted, EMERGENCY_HALT, or REJECT).
- `steps[].current_actor` — one of `forge`, `mirror`, `auto_merge`, `larry` (when waiting on Larry approval), `null` (when status is `pending` or terminal).
- `audit_log` — append-only log of state transitions; never modified.

**Atomic writes.** All writes to the sequence file use `tmp + rename` for atomicity. Schema validated on every read; on corruption, the advancer pauses the sequence and DMs Larry rather than continuing on stale state.

**Daily rotation of completed sequences.** Sequences in status `complete` or `failed` for more than 30 days are moved to `~/agents/blackboard/build-sequences/.archive/YYYY-MM/<seq-id>.json`.

### 5.2 Advancer daemon

**Path:** `scripts/build_sequence_advancer.py`
**Systemd unit:** `systemd/ourliberty-build-sequence-advancer.timer` (every 5 min) + `.service`.

**Each tick:**

1. List all sequence files in `~/agents/blackboard/build-sequences/` with `status: active`.
2. For each active sequence:
   a. For each step in `current_steps`: check if it has reached `merged` per § 5.3 belt-and-suspenders gate check. If yes, update status to `merged`, append `step-merged` event to `audit_log`, remove from `current_steps`.
   b. For each step in `steps` with status `pending`: check if all `depends_on` have status `merged`. If yes, transition to `dispatchable`, then immediately dispatch (see § 5.5) and transition to `dispatched`. Add to `current_steps`.
   c. If `current_steps` is empty AND all steps are `merged`: set sequence `status: complete`, DM Larry with completion summary.
   d. If any step in `current_steps` has reached `failed` status (via chain_events showing EMERGENCY_HALT or REVISION-exhausted or REJECT for its PR): set sequence `status: paused`, DM Larry with failure summary + `resume <seq-id>` / `cancel <seq-id>` shortcuts.
3. Heartbeat: write current timestamp to `~/agents/state/build-sequence-advancer-heartbeat.json`.

**Resilience to droplet reboot.** All state lives in the sequence files; the advancer is stateless across reboots. First tick post-reboot re-reads all active sequences, queries chain_events from each step's `dispatched_at` forward, and rebuilds the live state.

**Concurrency.** Per decision A (one active sequence at a time), the advancer enforces this at sequence-creation time, not at tick time: when Beacon writes a new sequence file, the advancer checks for any existing `status: active` sequence and rejects the new one with a DM to Larry if found. PR-S2 ships the `validate_no_concurrent_active()` helper in `scripts/build_sequence_validator.py` (alongside the advancer itself, since both gate on the same state files).

### 5.3 Belt-and-suspenders gate check

Per decision H, advancing a step from `dispatched` (or `building` or `reviewing`) to `merged` requires BOTH:

1. **chain_events confirmation.** Query Supabase for `chain_events` rows where `task_id` matches this step's dispatch envelope `task_id` AND `event_type = 'auto_merge_success'` AND `ts >= dispatched_at`. At least one matching row required.
2. **gh pr view confirmation.** Run `gh pr view <pr_url> --json state` and confirm `state == "MERGED"`. PR URL is recorded in the step's `pr_url` field at PR-open time (extracted from the `pr-opened` chain_events row).

If only one confirms but not the other, the advancer logs a `gate-mismatch` event in audit_log and waits for the next tick. After 30 minutes of mismatch, the resolution is **asymmetric on which side confirms the merge** (added 2026-07-08 after `pulse-check-xii` false-paused on a clean merge):

- **gh confirms, chain_events lags (`gh_merged=True`, `chain_merged=False`).** gh is the authoritative merge source — the step's own recorded `pr_url` showing `MERGED` means *that exact PR* merged. The chain_events `auto_merge_success` row is a known-laggy secondary signal (the recurring ingestion-lag class). So the advancer **completes the step** (`step-merged`, `gate_resolution: gh-authoritative`) rather than pausing. No DM. This is *not* "trusting chain_events over gh" — it is trusting gh, the authoritative side, when the unreliable side is merely behind.
- **chain_events confirms, gh does NOT (`chain_merged=True`, `gh_merged` is `False`/`None`).** This is the genuinely ambiguous direction — gh is not vouching for the merge (the PR may be closed-unmerged, or chain_events emitted a false positive). The advancer **pauses** and DMs Larry: `Sequence <seq-id> step <step_id> gate-mismatch: chain_events says merged but gh pr view says <state>. Sequence paused. Manual verification needed.`

The belt-and-suspenders fast path is unchanged: a normal merge completes immediately the moment both gates agree; gh only becomes the tiebreaker at the >30-min stalemate.

### 5.4 Failure handling

Three failure modes are recognized:

1. **Step failure via Mirror.** Mirror emits REVISION-exhausted or EMERGENCY_HALT or REJECT for the step's PR. The advancer detects this via chain_events `event_type` rows (`mirror_revision_exhausted`, `mirror_emergency_halt`, `forge_reject`). On detection: set step status to `failed`, set sequence status to `paused`, DM Larry.
2. **Step failure via gate-mismatch timeout.** See § 5.3.
3. **Advancer daemon failure.** Detected via `heal_build_sequence_advancer_heartbeat.py` (new healer, ships with PR-S2): if heartbeat timestamp is more than 10 min stale, DM Larry.

**Larry's recovery options (via Beacon shortcuts):**

- `resume sequence <seq-id>` — unpause; advancer re-evaluates current step state. Used when the failure was transient (e.g., a retry succeeded out-of-band).
- `cancel sequence <seq-id>` — set sequence status to `failed`, log reason, stop advancing. New steps will not dispatch, and **any in-flight PR from this sequence will NOT auto-merge via the team's primary auto-merge path** — the gate in `outbox_notifier` checks the sequence's cancelled state before merging (board-abort-dispatched-build); the in-flight step PR stays open for manual close, and the Forge run already underway is not killed. (The default-OFF detective healer `heal_pr_auto_merge.py` is a separate merge path not yet covered by this gate — fast-follow.)
- `retry sequence <seq-id> step <step-id>` — re-dispatch a specific failed step (creates a new PR). Used when the failure was a fixable spec issue that Larry has already addressed.
- `skip sequence <seq-id> step <step-id>` — mark a step as `merged` without an actual PR (use sparingly; used when the step's work was done out-of-band). Logs a `step-skipped` event in audit_log.

### 5.5 Beacon CLAUDE.md additions

Adds a new top-level section `## How you author multi-step build sequences` to `agents/beacon/CLAUDE.md`. Key content:

**Discipline 1 — Spec-doc-first authoring.** When Larry says "build X across multiple PRs" or "implement the Y spec," do NOT include the build detail in the Telegram dispatch text. Instead:

1. Determine whether a canonical spec doc already exists at `agents/beacon/specs/<topic>.md`. If yes, amend it. If no, draft it.
2. The spec doc must be self-contained: someone who has not seen this Telegram conversation must be able to read the spec and understand what to build, why, and what success looks like.
3. Per the new authoring discipline, the spec doc is committed to `main` BEFORE the sequence kicks off (typically as a doc-only PR that Mirror reviews quickly via Claude-as-Forge). The sequence file references spec sections by anchor.

**Discipline 2 — Sequence file synthesis.** When Larry approves a multi-step build:

1. Write the sequence file to `~/agents/blackboard/build-sequences/<seq-id>.json` per § 5.1 schema.
2. Each step's `dispatch_text` must be ≤500 characters and consist of (a) a one-sentence statement of what to build, (b) a pointer to the spec section, (c) a brief Mirror-review-focus line. NO design detail inline; that lives in the spec.
3. Run `python3 scripts/build_sequence_validator.py validate <seq-id>` to verify DAG correctness before emitting the kickoff marker.
4. Emit a single APPROVAL_REQUEST with `task_id: kickoff-<seq-id>`, `target_agent: build_sequence_advancer`, `prompt: kickoff <seq-id>`. The bot routes this to the advancer rather than Forge.

**Discipline 3 — Mirror preflight DAG verification.** Per decision F, before the kickoff APPROVAL_REQUEST is emitted, Beacon dispatches a small Mirror review of the sequence file's DAG (a separate APPROVAL_REQUEST with `task_type: code-review`, `prompt: review-sequence-dag <seq-id>`). Mirror checks:

- No cycles in the DAG.
- All `depends_on` references resolve to valid step_ids.
- Steps declared parallel (i.e., no `depends_on` between them but both share an upstream parent) do not touch overlapping files based on a static analysis of their dispatch_texts and spec sections.
- All referenced spec sections exist in the spec_doc.

Mirror returns PASS or REVISION-with-reasons. On REVISION, Beacon amends the sequence file and re-dispatches the review. On PASS, the H1 handler (`_handle_mirror_dag_preflight_result` in `scripts/outbox_notifier.py`) auto-transitions the sequence file from `status: pending` → `status: active` and appends a `dag-preflight-passed` audit_log entry. The `build_sequence_advancer`'s next tick (≤5 min) dispatches the root step. No additional approval required — Larry already approved the sequence at author-time, and a second approval after the DAG-preflight PASS is friction without safety value.

*(Decision D in `agents/beacon/specs/orchestrator-rectification-v2.md` locks "implementation wins" — PR #145 H1 shipped the auto-transition; this spec § 5.5 prose is being aligned in orchestrator-rectification-v2 V5 to match what already ships. The `approve sequence <seq-id>` shortcut still exists for the legacy case where the author wants to defer kickoff past the PASS — it's idempotent and a no-op on a sequence the H1 handler already advanced.)*

**New shortcuts (added to Beacon CLAUDE.md):**

- `approve sequence <seq-id>` — confirms kickoff after Mirror preflight PASSes.
- `pause sequence <seq-id>` — Larry's manual pause.
- `resume sequence <seq-id>` — unpause.
- `cancel sequence <seq-id>` — terminate.
- `retry sequence <seq-id> step <step-id>` — re-dispatch a failed step.
- `skip sequence <seq-id> step <step-id>` — mark a step as merged without PR.

### 5.6 Dashboard ladder panel

**Path in repo:** `dashboard/app/operations/build-sequences/page.tsx` (Next.js) + supporting components.
**Droplet API endpoint (new):** `GET /api/system/build-sequences` returns a JSON list of all sequence files + their current state.

**Panel layout (V1 ladder per decision C):**

- Top section: list of active + recently-completed sequences (each row clickable).
- Click a sequence → detail page with the ladder.

**Ladder rendering:**

- Each step rendered as a node (box with label, status badge, current_actor tag, elapsed time, expected_cost).
- Steps with `depends_on: []` are roots, rendered at the top.
- Vertical line from each step to its dependents.
- Steps that share the same `depends_on` set are rendered side-by-side (parallel branches).
- Color coding per node background:
  - **green** — status `merged`
  - **blue** — status `dispatched` / `building` / `reviewing`
  - **yellow** — status `pending` (waiting on deps) OR sequence paused at this step
  - **red** — status `failed`
- Click a node → expandable detail with dispatch_text, PR link, chain_events history for the step.

**Real-time update.** Page polls `/api/system/build-sequences` every 10 seconds. No WebSocket in V1 (avoids droplet WebSocket plumbing).

### 5.7 Pulse takeover model (post-Pulse-cycle-upgrade)

Once the Pulse cycle upgrade ships, Pulse layers LLM-judgment on top of the deterministic advancer:

**What the advancer keeps doing:**

- Polling chain_events + gh pr view for gate-check.
- State-file updates and audit_log appends.
- DM emission on key transitions.

**What Pulse adds:**

- **Failure-DM triage.** When the advancer DMs Larry a sequence-paused notification, Pulse intercepts (per her healer-triage scope) and re-renders the DM with plain-language framing + a proposed recovery action: `Sequence pulse-upgrade-001 paused at PR-α₁ (Mirror REVISION-exhausted after 2 cycles). Likely cause: <Pulse's diagnostic>. Proposing: <recovery action>. Reply approve / modify / cancel.`
- **Mid-flight re-planning.** When Pulse detects that an upstream condition has changed mid-sequence (e.g., a hotfix PR merged that obsoletes step C's premise), she proposes a sequence amendment: `Sequence X step C is obsolete after the hotfix; proposing to skip + replan downstream. Reply approve / reject.`
- **Parallelism re-optimization.** Pulse periodically re-examines the in-flight DAG; if steps declared sequential could safely run in parallel based on observed file-touches in their already-completed analogues, she proposes a DAG amendment.

**Integration point:** Pulse's pipeline-driver layer (per `agents/beacon/specs/pulse-cycle-upgrade.md` § 5.4) reads sequence files as one of its data sources; her proposal artifacts route through the same Beacon-approval-shortcut machinery as Check III/IV/V/VI/VII.

**Backwards compatibility:** the orchestrator must keep functioning when Pulse is paused / offline. Pulse is an enhancement layer, not a dependency.

### 5.8 Data sources

| Source | What the advancer reads | When |
|---|---|---|
| `~/agents/blackboard/build-sequences/*.json` | All active sequence files | Every tick (5 min) |
| `chain_events` table (Supabase) | `auto_merge_success`, `mirror_revision_exhausted`, `mirror_emergency_halt`, `forge_reject`, `pr_opened` events scoped to each step's `task_id` | Every tick |
| `gh pr view <pr_url>` | Belt-and-suspenders MERGED-state confirmation | Every tick, per in-flight step |
| `~/agents/state/build-sequence-advancer-heartbeat.json` | Self-heartbeat | Every tick (write) |
| Beacon's inbox (`~/agents/inboxes/beacon/`) | Writes the next step's envelope here when advancing | When advancing |

---

## 6. Implementation staging (PR-S1 through PR-S4)

Each PR ships independently; later PRs gate on earlier ones.

### PR-S1 — Spec adoption + Beacon CLAUDE.md additions (this spec lands)

- **Files added/modified:**
  - `agents/beacon/specs/build-sequence-orchestrator.md` (THIS doc — adopted as canonical spec).
  - `agents/beacon/CLAUDE.md` — new section "How you author multi-step build sequences" per § 5.5.
  - `docs/operating-manual.md` Part II entry summarizing the orchestrator decision pass.
- **task_type:** `doc-only`. Eligible for Claude-as-Forge per `project_claude_as_forge_pattern` (pure docs + CLAUDE.md, no executable code).
- **Estimated cost:** ~$3 LLM. ~Half-day wall clock.
- **Mirror reviews for:** internal consistency of the spec, no contradictions with existing Beacon CLAUDE.md sections, correct cross-references to other specs (`pulse-cycle-upgrade.md`).
- **Acceptance criteria:** PR merges, Beacon's startup-context-load on next session picks up the new authoring discipline.

### PR-S2 — Advancer daemon + state schema + tests

- **Files added/modified:**
  - `scripts/build_sequence_advancer.py` (NEW) — the polling daemon per § 5.2.
  - `scripts/build_sequence_validator.py` (NEW) — DAG validator per § 5.5 discipline 2.
  - `scripts/heal_build_sequence_advancer_heartbeat.py` (NEW) — healer per § 5.4 failure mode 3.
  - `systemd/ourliberty-build-sequence-advancer.service` (NEW).
  - `systemd/ourliberty-build-sequence-advancer.timer` (NEW, every 5 min).
  - `systemd/ourliberty-heal-build-sequence-advancer-heartbeat.timer` (NEW, every 5 min).
  - `scripts/tests/test_build_sequence_advancer.py` (NEW).
  - `scripts/tests/test_build_sequence_validator.py` (NEW).
  - `~/agents/blackboard/build-sequences/.gitkeep` (NEW dir).
  - `runbooks/build-sequence-advancer.md` (NEW) — operator doc.
  - `systemd/INSTALL.md` — append new units to install table.
- **task_type:** `feature-development`.
- **Estimated cost:** ~$8 LLM. ~1.5 days wall clock.
- **Mirror reviews for:** state-file atomic-write correctness, belt-and-suspenders gate check (both checks required), reboot-resilience (advancer stateless across reboots), no infinite-loop on corrupted sequence files (pauses + DMs instead), DAG validator catches cycles and missing references.

### PR-S3 — Dashboard ladder panel + droplet API endpoint

**Split annotation (2026-05-27):** PR-S3 spans two repos (droplet `ourliberty-agent-core` for the API + `ourliberty-dashboard` for the UI). Per § 4's V1 single-repo discipline ("Cross-repo sequences. V1 assumes all steps in a sequence target the same `target_repo`"), this entry splits into two single-repo PRs that must both merge for the ladder feature to be operationally complete:

- **PR-S3a** (`ourliberty-agent-core`, this PR-S2 successor): droplet endpoint + tests + dashboard runbook + this annotation.
- **PR-S3b** (`ourliberty-dashboard`, follow-on): the Next.js ladder UI that consumes PR-S3a's endpoint.

Either repo's PR can land first in principle; PR-S3a is sequenced first because its endpoint locks the JSON contract PR-S3b consumes. Mirror's PR-S3a review covers the endpoint side; Mirror's PR-S3b review covers the UI side.

- **PR-S3a files added/modified (this PR):**
  - `scripts/dashboard_api.py` — new endpoint `GET /api/system/build-sequences` per § 5.6.
  - `scripts/tests/test_dashboard_api_build_sequences.py` (NEW).
  - `runbooks/build-sequence-dashboard.md` (NEW).
  - `agents/beacon/specs/build-sequence-orchestrator.md` — this split annotation + footer note.
- **PR-S3b files added/modified (follow-on, `ourliberty-dashboard`):**
  - `dashboard/app/operations/build-sequences/page.tsx` (NEW) — landing list page.
  - `dashboard/app/operations/build-sequences/[seq_id]/page.tsx` (NEW) — detail page with ladder.
  - `dashboard/components/build-sequence-ladder.tsx` (NEW) — the ladder component.
  - `dashboard/components/build-sequence-row.tsx` (NEW) — list-page row.
  - `dashboard/__tests__/build-sequence-ladder.test.tsx` (NEW).
- **task_type:** `feature-development` (both halves).
- **Estimated cost:** ~$7 LLM combined across PR-S3a + PR-S3b. ~1.5 days wall clock.
- **Mirror reviews PR-S3a for:** endpoint matches the § 5.6 contract; token-gating via existing `_require_token`; uncached re-read per request (no `lru_cache`); empty-state/missing-dir/corrupt-file graceful degradation (200, not 500); archive-layout discipline (only `YYYY-MM` subdirs recursed); path-safety + no env-var leak.
- **Mirror reviews PR-S3b for:** ladder rendering correctness across (a) all-sequential, (b) all-parallel, (c) mixed DAGs; color-coding matches spec § 5.6; polling cadence does not over-fetch (10s interval, no thundering-herd); accessibility tags on color-only-status nodes.

**Endpoint contract locked by PR-S3a's preflight CLARIFY (2026-05-27):** response shape is `{active: [...], archived: [...], parse_warnings: [...], as_of: <iso>}` with raw sequence-file dicts (no field projection). `active` = files with `status ∈ {pending, active, paused}` or unknown/missing status; `archived` = files with `status ∈ {complete, failed, archived}` plus anything under `.archive/YYYY-MM/*.json`. No pagination + no time-cutoff filter in V1 (the spec-§ 5.1 30-day archiver doesn't exist yet — `.archive/YYYY-MM/` is forward-compat scaffolding; `TODO(PR-S3c): pagination` breadcrumb lives in `_reader_build_sequences`).

### PR-S4 — Beacon shortcuts + Mirror preflight integration

- **Files added/modified:**
  - `agents/beacon/CLAUDE.md` — new shortcuts section per § 5.5 (the 6 sequence shortcuts: approve / pause / resume / cancel / retry / skip).
  - `scripts/outbox_notifier.py` — new handler for `target_agent: build_sequence_advancer` envelopes per § 5.5 discipline 2.
  - `agents/mirror/CLAUDE.md` — small addition teaching Mirror to recognize `prompt: review-sequence-dag <seq-id>` and dispatch the DAG verification per § 5.5 discipline 3.
  - `scripts/tests/test_outbox_notifier_sequence_handlers.py` (NEW).
  - `runbooks/build-sequence-shortcuts.md` (NEW).
- **task_type:** `feature-development`.
- **Estimated cost:** ~$5 LLM. ~1 day wall clock.
- **Mirror reviews for:** shortcut idempotency (re-running `approve sequence X` for an already-approved sequence is a WARN no-op), `target_agent: build_sequence_advancer` routing does not collide with existing handler paths, Mirror's DAG verification logic is concretely executable.

### Sequencing constraint

PR-S1 → PR-S2 → PR-S3 → PR-S4 strictly sequential. Reasons:

- PR-S2 implements what PR-S1's spec defines.
- PR-S3 reads the state files PR-S2 produces.
- PR-S4 wires the user-facing controls; testing it requires PR-S2's daemon live and PR-S3's UI visible.

**These four PRs are themselves a multi-step build sequence — and they ship via the OLD pattern (manual dispatch one at a time) because the orchestrator they're building does not exist yet.** This is the chicken-and-egg footnote: the orchestrator's own construction is the last manual sequence. Every subsequent multi-PR build can run through it.

---

## 7. Effort + cost estimate

| PR | LLM cost | Wall clock | Larry actions |
|---|---|---|---|
| PR-S1 spec + Beacon CLAUDE.md | ~$3 | ½ day | Paste dispatch text (1 touch); approve Mirror PASS DM (1 touch) |
| PR-S2 advancer + validator + tests | ~$8 | 1.5 days | Paste dispatch; approve Mirror PASS |
| PR-S3 dashboard panel + API | ~$7 | 1.5 days | Paste dispatch; approve Mirror PASS; quick visual review of the ladder on dashboard.ourliberty.dev |
| PR-S4 Beacon shortcuts + Mirror preflight wiring | ~$5 | 1 day | Paste dispatch; approve Mirror PASS |
| **Total** | **~$23** | **~4.5 days** | **~8 touches across 4 PRs** |

After PR-S4 merges: the Pulse cycle upgrade (PR-α₁/α₂/β/γ) runs through the orchestrator with Larry's manual touches dropping from ~9 to ~2 (one kickoff paste + one final completion DM ack).

**Ongoing cost.** The advancer daemon polls every 5 min — pure database + filesystem reads, no LLM cost. Steady-state cost is the LLM-driven sequences themselves, which would have happened with or without the orchestrator.

---

## 8. Risks + rollback

| Risk | Mitigation | Rollback |
|---|---|---|
| Advancer dispatches the next step prematurely (chain_events lies, `gh pr view` lies) | Belt-and-suspenders per § 5.3; 30-min mismatch timeout pauses sequence + DMs Larry | Stop the systemd timer; manual edit of sequence file `current_steps` |
| Advancer enters an infinite loop on a malformed sequence file | Schema validation on every read; corrupted files pause the sequence and DM Larry, do not crash the daemon | Manual fix or delete of the bad sequence file |
| Beacon's PLAN_SYNTHESIS_DISCIPLINE produces a syntactically invalid sequence file | `build_sequence_validator.py` runs at synthesis time per § 5.5 discipline 2; invalid sequences never reach the kickoff stage | Beacon re-synthesizes per validator's error output |
| Mirror's DAG preflight catches false-positive parallelism conflicts (e.g., two steps both touch a shared README) | Larry can override with `approve sequence <seq-id> --override-dag-warn` (Beacon shortcut adds the override flag if Larry's reply says so) | One-off; falls back to author-declared DAG |
| Sequence state file gets corrupted mid-write | Atomic `tmp+rename` writes; schema validation on read; corruption surfaces as a pause + DM, not silent data loss | Manual restore from `audit_log` (the log itself never overwrites, only appends, so it survives partial-write events) |
| Dashboard ladder UI breaks on a sequence with >10 parallel branches | V1 caps visible parallel branches at 6 (configurable); excess branches collapse to a "+N more" affordance | Tighten cap; Pulse can propose lowering the cap if dashboard load slows |
| Pulse takes over advancement (post-upgrade) and breaks something the deterministic advancer was getting right | Pulse layers on top; advancer stays as the canonical driver. Pulse's role is judgment + plain-language, not advancement-itself | Disable Pulse's sequence-related Check; advancer resumes deterministic operation |
| Larry kicks off a second sequence while one is active | Per decision A: validator rejects at creation; sequence file is never written; Beacon DMs Larry the active sequence's status | Educational error; no recovery needed |

---

## 9. Acceptance criteria (per-PR and full V1)

**Per-PR acceptance:**

- PR-S1: Mirror PASS, AUTO_MERGE fires, Beacon's next session-startup picks up the new CLAUDE.md authoring discipline.
- PR-S2: Mirror PASS + AUTO_MERGE; advancer systemd unit starts cleanly; heartbeat file appears; running `python3 scripts/build_sequence_validator.py validate-empty-sequence` exits 0.
- PR-S3: Mirror PASS + AUTO_MERGE; dashboard.ourliberty.dev/operations/build-sequences/ loads and renders an empty-state when no sequences exist; once PR-S2 + a synthetic sequence file exist, ladder renders correctly.
- PR-S4: Mirror PASS + AUTO_MERGE; Beacon's 6 new shortcuts respond correctly in dry-run mode (synthetic sequence file, no actual dispatches).

**Full V1 acceptance (after PR-S4 merges):**

- [ ] Synthetic end-to-end test: a 3-step sequence (one root + two parallel children) created via Beacon synthesis, kicked off, advanced through to completion in dry-run mode (PR URLs mocked).
- [ ] Belt-and-suspenders test: deliberately drop a fake `auto_merge_success` event in chain_events for a step whose `gh pr view` still shows OPEN; advancer must NOT advance + must log gate-mismatch.
- [ ] Reboot test: kick off a sequence, advance to step 2, restart the droplet, confirm the advancer resumes correctly without dispatching step 2 again.
- [ ] DM-volume test: complete a 5-step sequence; verify exactly 5 transition DMs + 1 completion DM fire (matching decision B's key-transition cadence).
- [ ] Pulse cycle upgrade kickoff: PR-α₁ through PR-γ run through the orchestrator with at most 2 Larry-touches across the entire 4-PR sequence.

---

## 10. Source notes

- Larry's 2026-05-26 conversational design pass: this entire spec emerged from one design conversation. Key framing quotes preserved in the audit log of the spec's first commit message.
- `feedback_pulse_triages_operational_signals` (memory) — the doctrine that informs decision B (key-transition DM volume) and § 5.7 (Pulse's failure-DM triage role).
- `feedback_self_optimizing_config_via_pulse_check_pattern` (memory) — informs that the orchestrator's tunable constants (5-min tick cadence, 30-min gate-mismatch timeout, 6-parallel-branch dashboard cap) are themselves candidates for future Pulse Check instances (Check VIII+).
- `feedback_beacon_dispatch_fits_one_telegram_bubble` (memory) — informs the §5.5 discipline 2 rule that each step's dispatch_text is ≤500 chars.
- `agents/beacon/specs/pulse-cycle-upgrade.md` — the first real consumer of this orchestrator; co-evolved during the same design pass.
- `agents/beacon/CLAUDE.md` § "How you dispatch work to Forge — the APPROVAL_REQUEST marker" — the existing dispatch infrastructure this spec extends without replacing.
- PR #114 (AUTO_MERGE serializer + mergeable gate, 2026-05-26) — the chain-discipline foundation that makes deterministic gate-watching reliable.
- E4.4d PR-A/B/C/D — the `chain_events` data layer that the advancer queries.

---

## 11. Open questions Larry may want to override before PR-S1 dispatches

These are values calls in the spec that Larry can override in approval-review:

1. **Decision B (DM volume).** Locked at key-transitions-only. If Larry wants per-step transition DMs too (every dispatch + every Mirror PASS, not just merges), amend § 5.4 + decision B.
2. **Decision C (ladder UI scope).** Locked at full ladder V1. If Larry wants a minimal V1 (list-only) to ship faster, amend § 5.6 + decision C; PR-S3 scope shrinks by ~½ day.
3. **Decision I (failure-handling granularity).** Locked at pause-whole-sequence. If Larry wants branch-level pause for parallel failures in V1, amend § 5.4 + decision I; PR-S2 scope grows by ~½ day.
4. **5-min tick cadence (§ 5.2).** Locked at 5 min. Tighter (1 min) reduces advance latency but burns more chain_events queries; looser (15 min) saves load but adds latency. Tunable via Pulse Check VIII later.

If any need amendment, paste the change as a Beacon `modify:` reply to the PR-S1 approval card.

---

*Amended 2026-05-27: § 6 PR-S3 split into PR-S3a (droplet API endpoint, this repo) + PR-S3b (dashboard UI, `ourliberty-dashboard`) per the § 4 single-repo discipline. Git history of this file is the canonical change log.*
