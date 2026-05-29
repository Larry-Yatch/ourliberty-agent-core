# Roadmap

Active projects and their status. Updated by Beacon (or with Beacon's awareness) as work progresses. Each project has a status, next step, owner, dependencies, and reference to source-of-truth dispatch IDs where applicable.

Status values: `proposed` → `in design` → `approved` → `in flight` → `blocked` → `done` (or `dropped`).

---

## Desired-state reconciler — bot liveness auto-recovery

- **Status:** in flight (approved 2026-05-28; Forge dispatched)
- **Next:** Forge build per brief → Mirror review → merge
- **Owner:** Claude (spec) → Forge (build) → Mirror (review)
- **Reference:** `docs/desired-state-reconciler-brief.md`
- **Why:** pulse-bot sat cleanly down ~1d 8h (2026-05-27/28) with no actor recovering it. Watchdog detected but did not actuate; pulse's tmux deployment was a recovery artifact with no supervisor. Closes the actuation gap (Option B: return pulse to systemd, recover all four uniformly from the privileged watchdog) and models intended-down via a `desired_state` field, retiring the mask-unit / kill-healer hack.
- **Notes:** retires the bespoke beacon-bot watchdog carve-out; bundles the pulse tmux→systemd cutover into the same PR. `desired_state` is the substrate for a future paused-on-rate-limit healer. Channel-heartbeat is the separate follow-up below.

## Channel-heartbeat Pulse Check — end-to-end Telegram liveness

- **Status:** proposed — folded into the Pulse cycle upgrade (2026-05-29)
- **Next:** ships within the Pulse cycle upgrade as Check X (PR-β/γ); spec lives in `docs/pulse-cycle-upgrade-design-pass-2026-05-26.md` § 12.6 — no longer a standalone dispatch
- **Owner:** Claude (spec) → Forge (build), as part of the Pulse cycle upgrade workstream
- **Depends on:** desired-state reconciler — MERGED (PR #178, 2026-05-28); this is the observation half, the reconciler is the actuation half
- **Why:** existence checks (`systemctl is-active`) cannot catch a bot whose process is alive but whose Telegram channel is wedged (2026-05-20 HTTP 502 storm; 2026-05-28 HTTP 409 double-poll). A periodic end-to-end probe (`getMe` + getUpdates-not-erroring + optional self-ping watermark) closes this. Scoped as a Pulse Check because it is observation/triage, not restart-actuation; bundled into the cycle upgrade so it travels with that work rather than orphaning as a one-off.

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

## E2 — Deploy layer (Vercel preview-first)

- **Status:** in flight (E2.0 done 2026-05-19; E2.1 design starts next)
- **Next:** E2.1 — `config/deploy_targets.json` schema design
- **Owner:** Claude (design) → Forge (build)
- **Reference:** `docs/phase-e-plan.md` Phase E2 section
- **Why:** Close the spec→PR→deploy gap; preview-first because most client work is prototyping (full prod deploys deferred to E6)

---

## Archive

### E1.5 — Credential rotation discipline — resolved 2026-05-19 (single session)

- **Closed by:** PR #45 (design) + PR #46 (implementation) + PR #47 (chat-ID registry follow-up) + PR #48 (task #17 headless Beacon handler) + the task-#19 narrowing fix follow-up
- **What shipped:** Full credential rotation system primitive — 10-entry registry across 4 storage locations, 2 drift healers (credential drift 6h + systemd install drift 12h), Pulse cycle rotation-window extension, log-parser-based scope-usage analyzer, 8 runbooks, Mirror-enforced 4-artifact convention, Beacon-owned Google Calendar events (4 scheduled audits + revocation-only entries), source-routing fix for headless-mode Mirror dispatches, headless Beacon APPROVAL_REQUEST handler, source-routing narrowing fix. Five architectural findings surfaced; four closed in-session; one (DM delivery delay) deferred to E6 polish.
- **Reference:** `docs/operating-manual.md` Part II 2026-05-19 entry; memory `project_phase_e1_5_complete`; `config/token-rotation-schedule.json`; `shared/credentials-discipline.md`

### Auto-merge gap fix (PR #16 surface) — resolved 2026-05-19 by E1

- **Closed by:** PR #43 (`scripts/heal_pr_auto_merge.py`) + E1.1 markers (PR #40) structurally preventing PR #16's failure class
- **Original task_id:** `auto-merge-gap-pr16-001`
- **What happened:** The original "auto-merge did not fire on PR #16's REVIEW_PASS" gap was closed by D3.5 5d's `_auto_merge_pr` (primary path) + E1.3's `heal_pr_auto_merge` healer (defense in depth). Hand-typed marker drift (the root cause of the PR #16 silent dead-letter) is now structurally impossible thanks to E1.1's `render_marker` helpers + drift tests.

### Pulse iter 23b closure — codify D3.5 active-set — resolved before E1

- **Closed by:** Pulse iters 35+ confirmed the decommissioned services are no longer flagged
- **Original task_id:** `pulse-iter23b-close-decommission-001` (never landed as a standalone PR; the cycle-prompt update was absorbed into other commits)

---

**Convention:** when a project changes status, update its entry here. When a project starts, add it. When it closes, move it below the Archive section with the resolution + date. Keep entries ordered by priority/recency — newest active work at the top, in-flight before in-design before proposed.
