# Roadmap

Active projects and their status. Updated by Beacon (or with Beacon's awareness) as work progresses. Each project has a status, next step, owner, dependencies, and reference to source-of-truth dispatch IDs where applicable.

Status values: `proposed` → `in design` → `approved` → `in flight` → `blocked` → `done` (or `dropped`).

---

## Ledger — CFO agent

- **Status:** proposed
- **Next:** design spec (handoff from current Pulse Check I conversation once contract shape is locked)
- **Owner:** Beacon (spec) → Forge (build) — design conversation with Larry not yet started
- **Depends on:** Pulse Check I contract — Larry wants Ledger and Pulse to specialize; their pipeline shape needs to be agreed before either can be fully spec'd
- **Why:** dedicated cost-intelligence agent; carved out of Pulse Check I scope 2026-05-15 because cost-per-agent attribution and billing belong with a single financially-framed agent
- **Notes:** D3.5 5d already shipped the `cost_per_task_usd` budget gate in `scripts/outbox_notifier.py`; Ledger will build on that primitive, not replace it

## Pulse Check I — optimization mode

- **Status:** in design
- **Next:** resolve Ledger-Pulse pipeline shape (pipeline vs independent reads), then draft spec
- **Owner:** Beacon (spec) → Forge (build)
- **Depends on:** Ledger output contract — Pulse Check I's input format depends on what Ledger emits
- **Why:** Larry wants Pulse to surface optimization opportunities (efficiency, speed, patterns) on top of her existing health monitoring; current cycle-prompt only covers health/drift
- **Locked decisions (2026-05-15):** weekly Monday cadence + `/optimize` on-demand; heartbeat DM on empty weeks; specialization model where Pulse consumes Ledger findings and adds engineering interpretation + proposed fixes

## Auto-merge gap fix (PR #16 surface)

- **Status:** in flight
- **Next:** Forge preflight outcome
- **Owner:** Forge
- **Reference:** APPROVAL_REQUEST task_id `auto-merge-gap-pr16-001` (dispatched 2026-05-15)
- **Why:** Pulse iters 33+34 confirmed PR #16 received Mirror REVIEW_PASS but auto-merge did not fire; `outbox_notifier` processed the Mirror result correctly but the downstream `gh pr merge --auto` step did not execute

## Pulse iter 23b closure — codify D3.5 active-set

- **Status:** in flight
- **Next:** Forge preflight outcome
- **Owner:** Forge
- **Reference:** APPROVAL_REQUEST task_id `pulse-iter23b-close-decommission-001` (dispatched 2026-05-15)
- **Why:** 4 D3.5-decommissioned services (orchestrator, telegram-webhook, github-webhook, merge-watcher.timer) showed inactive on 12 consecutive cycles; Larry confirmed 2026-05-15 intentional; the PR updates `runbooks/cycle-prompt.md` Check C and closes the entry in `agents/pulse/MEMORY.md`

---

**Convention:** when a project changes status, update its entry here. When a project starts, add it. When it closes, move it below an Archive section (to be added when the first entry closes). Keep entries ordered by priority/recency — newest active work at the top, in-flight before in-design before proposed.
