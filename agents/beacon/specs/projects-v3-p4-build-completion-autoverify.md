# Spec: Projects Tab v3 — P4 (pulled forward): Build-completion closeout & auto-verify

**Status:** Ready to build
**Owner / approver:** Larry (approved "P4 now, before P2" 2026-06-16, after P1's ~50-min "done-but-unnoticed" gap)
**Author:** Claude Code (desktop design session)
**Parent North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) (§7 P4 — closeout pass; this is the foundational build-orchestrator slice, pulled ahead of P2 because every remaining phase needs it)
**Builds on:** the build-sequence orchestrator + Phase S two-way sync.
**Build path:** build-sequence orchestrator (single repo: `ourliberty-agent-core`).

> Scope note: this is the **build-orchestrator completion engine** (a finishing build self-reports + self-verifies), NOT yet the missions-*card* closeout (that full P4 rides on this engine later).

---

## 0. Desired End State
The moment a build finishes, the team tells Larry in plain English — **"X is done, here's what shipped, and it's verified live"** — having already run the finish-steps itself (restart, one-time cleanup, checks). **No build ever sits done-but-unnoticed**, and no one has to remember a manual finish step. Anything genuinely risky waits for a **one-tap approval** instead of silence.

## 1. Why now
P1 finished at 17:44 and sat **~50 minutes done-but-unverified** because *both* notification paths failed: my desktop watcher died with the session, and the droplet emits **no whole-sequence-complete signal** (the per-step closing DM is suppressed on the `AUTO_MERGE_DEFERRED_UNKNOWN` merge path). Plus cleanups keep shipping as code that **doesn't auto-run** (Phase S + P1 both needed a manual `--apply`). Every remaining phase (P2–P7) hits this — fix the engine first so the rest of the build is hands-free and self-reporting.

## 2. Scope & non-goals
**In (all agent-core):**
- (A) A **reliable sequence-COMPLETE signal + plain-language DM** to Larry when a build-sequence's last step merges.
- (B) A **declarative `post_merge` block** on a sequence (restart / cleanup / verify steps) + an **executor** that runs them on completion: **safe + fail-safe steps auto-run; risky/irreversible steps are proposed via `human-approval-gate`** (a one-tap), never auto-executed.
- (C) The completion DM **reports the verified go-live result** (what shipped, what was auto-run, what's awaiting your tap).

**Out:** the missions-*card* closeout briefing (Narrator-authored, per-card — full P4, later); the dashboard-wide rollout; changing how steps are built/reviewed.

## 3. Reuse & restock  *(consulted shelf + graph 2026-06-16)*
**Reuse:**
- **`outbox_notifier`** — owns the step-merge DMs and the **suppressed closing DM** (the bug site). Emit the sequence-COMPLETE DM here; un-suppress / re-fire the closing DM on the `AUTO_MERGE_DEFERRED_UNKNOWN` path. ⚠️ **10 dependents — surgical, well-tested change.**
- **`sequence_shortcut_helpers`** — the single library of legal build-sequence mutations; the "all steps merged → mark complete" transition + the `post_merge` state live here.
- **`human-approval-gate`** — the propose→approve mechanism for **risky** finish-steps (e.g., a prod restart, a non-fail-safe mutation). Safe steps skip it.
- **`larry_alerts`** — the doorbell sink for the completion DM (risk-gated loudness, per the meaning-layer doorbell model).
- **`task_resolution` / belt-and-suspenders verified-merge** — the "is the sequence truly complete (every step's PR merged)" check, reused so completion can't false-fire.

**Restock (after build):** `scripts/build_sequence_advancer.py` (uncatalogued, 5 dependents, reads chain_events) and `scripts/heal_missions_board_drain.py` (uncatalogued) — add shelf cards.

## 4. Contracts

### A — Reliable completion signal + plain-language DM
When the **last** step of a sequence reaches verified-merged (belt-and-suspenders: chain_events `auto_merge` AND `gh pr view MERGED`), emit a single **sequence-COMPLETE** event and a **plain-language DM** to Larry: *what the build was, the PRs that shipped, a one-line summary*. Exactly-once (idempotent — a re-detect never double-DMs). Fixes the gap where the closing DM is suppressed on the deferred-merge path and no whole-sequence signal exists.

### B — Declarative `post_merge` + executor (safe auto / risky gated)
A sequence may carry an optional `post_merge` block:
```jsonc
"post_merge": {
  "restart": ["ourliberty-dashboard-api.service"],   // services to restart (risky → gated)
  "run":     ["scripts/heal_missions_board_drain.py --apply"], // one-time cleanups
  "verify":  ["<read-only probe cmd>"]                // go-live checks (always safe)
}
```
On completion the executor runs them, classifying each: **`verify` (read-only) and fail-safe/idempotent `run` steps auto-execute; `restart` and any non-fail-safe step are proposed via `human-approval-gate`** for a one-tap. Each step's result is captured for the DM. A step author marks a `run` entry `safe: true` only when it is idempotent + fail-safe (e.g., the drain, which only touches verifiably-dead items).

### C — Verified go-live report in the DM
The completion DM states, in plain language: **done · shipped (PRs) · auto-ran (cleanups/restarts + result) · verified (checks passed/failed) · awaiting your tap (any gated step)**. If a verify check fails, the DM says so loudly (blocked-on-you doorbell).

### D — Cleanups persist via their owner (no sync jam)  *(added after the P1 drain jammed sync, 2026-06-16)*
An auto-run cleanup (B) that writes a machine-owned file (`missions.json`, `captures.json`) **must leave it committed by its sole owner** — it may NOT leave the working tree dirty. Today the GC healer (`heal_missions_card_gc`) commits `captures.json` and only commits `missions.json` when *it* ships a phase, so an externally-written `missions.json` delta sits uncommitted → `ourliberty-sync` refuses (dirty tree) → **all merges stop reaching the team** (exactly the P1 incident). Fix: **the owner (GC healer) commits ANY pending `missions.json` delta on its tick** (single committer preserved — no other writer commits it), so a cleanup's output is persisted within one tick. Defense-in-depth: **`sync_agent_core.sh` tolerates machine-owned-file dirt** (skip/stash the known machine files rather than hard-failing the whole sync) — a non-owner tolerating owner dirt, per the single-committer invariant. Either alone closes the jam; do both.

## 5. Risks & guardrails
- **Never auto-execute irreversible/prod-risky steps** — those go through `human-approval-gate` (honors the standing "explicit go for prod" rule); only fail-safe idempotent steps auto-run.
- **`outbox_notifier` is high-traffic (10 deps)** — the DM change must be surgical + unit-tested; do not regress existing step DMs.
- **Exactly-once completion DM** — guard against double-fire on re-detect / advancer re-tick.
- **Auto-run cleanups must be idempotent + fail-safe** — re-running on a re-detect is a no-op; the drain already meets this.
- **Don't block the build** — a verify/cleanup failure reports loudly but never corrupts the sequence record.
- **A cleanup must never jam sync** (Contract D) — its machine-file output is committed by the owner within one tick; sync tolerates machine-owned dirt. This is the P1 failure mode; it must not recur.
- **Single-committer preserved** — D centralizes the commit in the owner (GC healer); no second committer is introduced (no direct pushes of machine files).

## 6. Done-gate
- A sequence finishing emits one plain-language completion DM to Larry within a short cycle of the last merge (no more silent done-but-unnoticed).
- `post_merge` cleanups auto-run (proven: a build that ships a drain runs it automatically); risky steps surface as a one-tap, not silence.
- The DM reports verified go-live (shipped / auto-ran / verified / awaiting-tap).
- **A machine-file-writing cleanup leaves a clean tree within one tick and never jams sync** (Contract D) — provable by running the drain and confirming `ourliberty-sync` still succeeds.
- Tests cover A–D; the `outbox_notifier` change regresses nothing.
- Proven end-to-end on a real sequence completion.

## 7. Build sequence (recommended — finalize via DAG-preflight)
Single repo. **Serialization hazard:** these steps touch `outbox_notifier.py` / the advancer / `sequence_shortcut_helpers` / the GC healer — serialize them.

| Step | Contract | File(s) | depends_on |
|---|---|---|---|
| **p4-complete-signal** | A | `scripts/outbox_notifier.py`, `scripts/sequence_shortcut_helpers.py`, `scripts/larry_alerts.py` | — |
| **p4-cleanup-committer** | D | `scripts/heal_missions_card_gc.py`, `scripts/sync_agent_core.sh` | — |
| **p4-postmerge-exec** | B + C | `scripts/build_sequence_advancer.py`, `scripts/sequence_shortcut_helpers.py` (+ `human-approval-gate`) | p4-complete-signal, p4-cleanup-committer |

Each step ends at its done-gate (tests green + the contract demonstrably holds). Mirror's DAG-preflight finalizes ordering.

## 8. After P4 ships
**Re-run the P1 board drain** (`scripts/heal_missions_board_drain.py --apply`) to restore the cleanup discarded during the 2026-06-16 sync-jam incident — with Contract D live it now persists cleanly (the GC owner commits the `missions.json` delta, no jam). Expected: drop 4 terminal orphans + archive 9 legacy drafts (idempotent — identical to the dry-run). Ideally driven by P4's own `post_merge` auto-run as the first dogfood of the new machinery.
