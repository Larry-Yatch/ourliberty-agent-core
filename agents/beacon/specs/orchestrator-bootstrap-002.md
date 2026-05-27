# Spec: orchestrator-bootstrap-002 — 14-gap rectification verifier

**Status:** Draft (awaiting Larry approval — synthetic verifier, not a feature)
**Author:** Claude-as-Forge (written 2026-05-27 evening)
**Approver:** Larry (pending)
**Parent:** [docs/pr-s4-rectification-v1-brief.md](../../docs/pr-s4-rectification-v1-brief.md) explicit follow-up
**Predecessor:** `orchestrator-bootstrap-001` paused 2026-05-27 pending PR-S4 rectification; rectification shipped as PR #145; this verifies it.

---

## 1. Purpose

PR #145 closed 14 wiring gaps in the build-sequence orchestrator. The rectification brief explicitly calls for *"a tiny `orchestrator-bootstrap-002` (2 steps) [that] will verify all gaps resolved."* This spec defines that verifier.

This is a **synthetic fixture**, not a feature delivery. The output (a tiny verification log file) is irrelevant; what matters is the *path* exercised: chat-mode kickoff → Mirror DAG preflight → advancer dispatch → Forge build → Mirror review → AUTO_MERGE → next step dependency-resolution → repeat → sequence completion.

---

## 2. Gaps this sequence exercises (subset of PR-S4 brief 14)

| Gap | How exercised |
|---|---|
| H1 — Mirror DAG-result handler | Beacon authors sequence → Mirror DAG-preflights → result must route correctly |
| H2 — MIN_PROMPT_LEN routing-signal exemption | Beacon emits `kickoff orchestrator-bootstrap-002` (~32 chars) |
| H3 — Headless approval source-gate accepts 'orchestrator' | Advancer dispatches each step with `source: 'orchestrator'`; Beacon emits APPROVAL_REQUEST; handler must accept |
| H4 — routing topology includes build_sequence_advancer | Chat-mode `approve sequence` writes inbox envelope with target_agent=build_sequence_advancer — must not fail topology validation |
| H5 — validator CLI `validate <seq-id>` subcommand | Beacon's own pre-author validation per CLAUDE.md uses the new subcommand form |
| M1 — sequence shortcut helpers (pause/resume/cancel/retry/skip) | NOT exercised in v002 happy path (no failures expected); a future v003 can exercise these |

Failure on any of H1-H5 produces a recognizable stall (no advance past kickoff, Mirror DAG result swallowed, headless dispatch dead-ends, etc.) — easy to diagnose against this minimal fixture.

---

## 3. The 2 steps

Both steps are doc-only, both target `ourliberty-agent-core`. Each emits one PR through the chain.

### Step 1: `step-verify-write`

- **dispatch_text:** `Create docs/orchestrator-bootstrap-002-verify.log (new file) with exactly one line: "step-verify-write completed at <UTC iso timestamp>". No other files; no other changes. This is a synthetic verifier per orchestrator-bootstrap-002 spec — keep it absolutely minimal.`
- **target_repo:** `ourliberty-agent-core`
- **task_type:** `doc-only`
- **depends_on:** `[]` (root step)

### Step 2: `step-verify-sequential`

- **dispatch_text:** `Append a second line to docs/orchestrator-bootstrap-002-verify.log: "step-verify-sequential completed at <UTC iso timestamp>". File already exists (created by step-verify-write). No other files; no other changes. Synthetic verifier per orchestrator-bootstrap-002 spec.`
- **target_repo:** `ourliberty-agent-core`
- **task_type:** `doc-only`
- **depends_on:** `["step-verify-write"]`

The dependency on step-1 is what exercises the advancer's dependency-resolution + the H3 source-gate path on the second iteration.

---

## 4. Operational flow (what Larry pastes into Beacon)

Two Beacon messages, in order:

### Message 1 — author the sequence file

```
Beacon — please author the orchestrator-bootstrap-002 sequence file
per agents/beacon/specs/orchestrator-bootstrap-002.md sections 3.
Write to ~/agents/blackboard/build-sequences/
orchestrator-bootstrap-002.json with status="pending" and the 2 steps
exactly as the spec defines. Then DAG-preflight it via Mirror
(`review-sequence-dag orchestrator-bootstrap-002`).
```

Beacon authors, Mirror DAG-preflights. If Mirror returns PASS, Beacon DMs Larry: *"Sequence ready for kickoff: `approve sequence orchestrator-bootstrap-002`."* If REVISION, Beacon DMs the verdict + reasons.

### Message 2 — kickoff (after Mirror PASS)

```
approve sequence orchestrator-bootstrap-002
```

Beacon's shortcut handler does the rest. The advancer ticks (every 5 min) and dispatches step-1. Once step-1's PR merges, the next tick dispatches step-2. Once step-2's PR merges, sequence transitions to `complete`.

---

## 5. Success criteria

- Sequence file at `~/agents/blackboard/build-sequences/orchestrator-bootstrap-002.json` ends with `status: "complete"` and both steps' `status: "merged"`.
- 2 PRs land cleanly on `ourliberty-agent-core` main, each adding a single line to `docs/orchestrator-bootstrap-002-verify.log`.
- No `pipeline-stall:*` alerts fired for either step or the sequence as a whole.
- Total wall-clock from `approve sequence` to `status: complete`: ≤30 min (5 min advancer cadence × 2 steps + Forge build + Mirror review per step).
- `chain_events` shows the expected sequence: kickoff approval_request → build_dispatched (step-1) → session_start/done (forge step-1) → review-request → session_start/done (mirror step-1) → auto_merge → build_dispatched (step-2) → … → final session_done for step-2 + AUTO_MERGE.

---

## 6. Out of scope

- Exercising the failure-recovery shortcuts (pause / cancel / retry / skip — M1 helpers). A later `bootstrap-003` can intentionally fail a step to exercise those.
- Exercising large step DAGs (>2 nodes) or parallel siblings — `bootstrap-001`'s 4-step DAG (root + a/b/c parallel) is the eventual target; v002 just verifies the linear-2 path works.
- Code-touching dispatch_texts — both steps are doc-only on purpose. Code-touching adds risk that doesn't help the verification goal.

---

## 7. Cleanup

After successful run, the verifier log file (`docs/orchestrator-bootstrap-002-verify.log`) stays in the repo as a permanent record. The sequence file at the blackboard path stays as `status: complete` for the dashboard's build-sequences ladder UI to render in the "Recently completed" section.

If we want to re-run for re-verification in the future, increment to `bootstrap-003` (new file, fresh log line) — never re-run the same seq_id.

---

## End of spec
