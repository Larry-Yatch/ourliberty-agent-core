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

## E1.5 — Credential rotation discipline

- **Status:** in flight (design PR opened 2026-05-19; Forge build dispatch next)
- **Next:** Larry signs off on the design PR → it merges → Forge dispatched for E1.5.2 implementation
- **Owner:** Claude (design) → Forge (build)
- **Reference:** `docs/phase-e-plan.md` Phase E1.5 section; `config/token-rotation-schedule.json`; `shared/credentials-discipline.md`
- **Why:** E2.0 Vercel install surfaced the gap — 8 active credentials across 4 storage locations with zero rotation tracking; DO's "rotate every 90 days" template comment had been silent since Phase A. Larry's framing: "We need to make that a part of the system."

## E2 — Deploy layer (Vercel preview-first)

- **Status:** in flight (E2.0 done 2026-05-19; E2.1 design starts after E1.5 closes)
- **Next:** E2.1 — `config/deploy_targets.json` schema design
- **Owner:** Claude (design) → Forge (build)
- **Reference:** `docs/phase-e-plan.md` Phase E2 section
- **Why:** Close the spec→PR→deploy gap; preview-first because most client work is prototyping (full prod deploys deferred to E6)

---

## Archive

### Auto-merge gap fix (PR #16 surface) — resolved 2026-05-19 by E1

- **Closed by:** PR #43 (`scripts/heal_pr_auto_merge.py`) + E1.1 markers (PR #40) structurally preventing PR #16's failure class
- **Original task_id:** `auto-merge-gap-pr16-001`
- **What happened:** The original "auto-merge did not fire on PR #16's REVIEW_PASS" gap was closed by D3.5 5d's `_auto_merge_pr` (primary path) + E1.3's `heal_pr_auto_merge` healer (defense in depth). Hand-typed marker drift (the root cause of the PR #16 silent dead-letter) is now structurally impossible thanks to E1.1's `render_marker` helpers + drift tests.

### Pulse iter 23b closure — codify D3.5 active-set — resolved before E1

- **Closed by:** Pulse iters 35+ confirmed the decommissioned services are no longer flagged
- **Original task_id:** `pulse-iter23b-close-decommission-001` (never landed as a standalone PR; the cycle-prompt update was absorbed into other commits)

---

**Convention:** when a project changes status, update its entry here. When a project starts, add it. When it closes, move it below the Archive section with the resolution + date. Keep entries ordered by priority/recency — newest active work at the top, in-flight before in-design before proposed.
