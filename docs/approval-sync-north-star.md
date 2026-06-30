# North Star: Approval & Notification Sync (one truth for "what needs Larry")

**Status:** Draft v1 for review — 2026-06-30. *Skeleton + current-state map; Phase 0 done, Phase 1 built (this PR).*
**Owner / approver:** Larry
**Author:** Claude Code (desktop deep-dive session, 2026-06-30; five parallel subagent audits across agent-core, dashboard, and live droplet state; live dashboard UX dive)
**Born from:** the 2026-06-30 incident — *"the dashboard says nothing is waiting my approval, yet Beacon keeps DMing me '4 items need your call.'"* The deep dive found the four items had **already been approved on the dashboard** but never cleared from Beacon's queue — a one-directional sync gap — and that the same "needs-Larry" item is authored independently into up to four stores with no shared identity.
**Relationship to other docs:** sits **under** [system-awareness-north-star.md](system-awareness-north-star.md), which establishes the governing principle — *"Because Beacon and the dashboard read the same substrate, they cannot disagree."* This doc operationalizes that principle for the **decision/notification stream specifically**: the set of things that need Larry to know or act. Adjacent: [approval-tab-coverage-gap-brief.md](approval-tab-coverage-gap-brief.md), [missions-redesign-design-pass-2026-06-09.md](missions-redesign-design-pass-2026-06-09.md) (the board boundary), [projects-tab-v3-north-star.md](projects-tab-v3-north-star.md).
**Reuse mandate:** do **not** rebuild. Three load-bearing pieces already exist — (1) the **State Log** (`system-state-log.json` `waiting_on_larry`) is already a drift-free derived read view; (2) the dashboard's **Operator Action Queue** (`lib/operator-queries.ts` + `OperatorActionQueuePanel.tsx`) is a fully-built 6-source unified "what needs you" query that was **never mounted** — fold its sourcing into the Approvals tab rather than standing up a new panel; (3) `heal_stale_approvals` is a durable reconciliation spine. The work is to *unify and wire*, not invent.

> Living doc — keep current; tick the tracker (§9) as work lands. Start here when we pick this up.

---

## 0. Desired end state *(the destination)*

**There is one canonical answer to "what needs Larry," surfaced on the Approvals tab, and the DMs and Beacon read that same answer — so they cannot disagree, and resolving a decision anywhere clears it everywhere.**

- The **Approvals tab is the single source of truth** for everything that needs Larry's attention. Every other page *links to it* rather than re-deriving its own count.
- The headline number is **decisions only** (approve/reject — genuinely blocking). Parked backlog (review at leisure) lives in a **separate, calmer lane**, never inflating the decisions count.
- Larry sees the **same count and the same list** whether he glances at the dashboard, reads a DM, or asks Beacon.
- **Approving (or rejecting) once — on any surface — clears the item from all surfaces deterministically**, no reliance on a best-effort LLM session.
- A decision has **one identity** every store keys off; Beacon's DMs are **never phantom**.

## 1. The problem (why this mission exists)

The execution engine is mature; the **decision-notification layer accreted** one patch at a time and now has no single source of truth. The same logical "Larry needs to act" item is authored **independently into up to four stores at once**, each with its **own resolution mechanism** and **no shared identity key linking them.** Resolving it in one store does not clear the others. The read surfaces each aggregate a *different subset*, so "what needs Larry" has 4+ non-agreeing answers.

**The 2026-06-30 evidence (live):**
- Beacon queue (`beacon-pending-approvals.json`): PRs **#747, #763** + 2 healer items — *all already approved via the dashboard, none popped.*
- `for-larry-escalations.json`: PRs **#749, #751, #766** — three *different* unresolved PRs.
- `larry-alerts.jsonl`: **52** `approval_request` lines.
- State Log: reported `escalations: 0` while the escalations file held 3 open → **the aggregator itself undercounts.**

**Corrected root-cause of the incident:** the dashboard was *right* (0 unread; Larry had approved all four). Beacon was *stale*. Dashboard-approve sets `read_at` on Supabase **and** drops a dispatch envelope into Beacon's inbox + emits a `larry_action` audit row, **but never deterministically pops the entry from `beacon-pending-approvals.json`** — the pop depends on a best-effort LLM Beacon session, and the dispatch envelope's id (`larry-approval-<event_id>`) doesn't even carry the original task_id. The doorbell reads the un-popped queue and re-nags (two items had pinged 6× each).

## 2. The model — one identity, one resolve, one substrate

Three moves, mirroring the parent doc's substrate/projection split:

1. **One decision identity.** Every needs-Larry item (approval · clarify · escalation) carries a **canonical decision key** (the task_id / PR coordinate), used as the join key across every store. This is the foundational fix — its absence is *the* root cause.
2. **One resolve, fanned out.** A single `resolve_decision(key, outcome)` that pops Beacon's queue, sets `read_at` on Supabase `chain_events`, flips `resolved` on the escalations file, and retracts the alert line — called by **both** the Telegram path and the dashboard path. No surface can resolve "half."
3. **One read substrate, one surface.** The **State Log `waiting_on_larry`** is *the* read model; the **Approvals tab** is the one surface that renders it, with decisions and parked split. The doorbell DM, catch-me-up, Beacon, and every other dashboard page read/link to that — projection, not re-derivation.

## 3. Surfaces — how Larry consumes it (and Beacon)

Per the parent principle, all projections read the **same substrate**, and the **Approvals tab is the home**:
- **Dashboard** — the **Approvals tab is the single source of truth** for everything needing Larry. One global "N need you" counter in the nav (none today) reads the same substrate. **Decisions** (approve/reject) are the headline; **parked backlog** is a separate calmer lane on the same tab. Live System / Where-are-we / Operations stop computing their own counts and **link into Approvals**.
- **DMs** — the doorbell + per-item approval DMs count off the same substrate; quiet the instant `waiting_on_larry` decisions drop to 0.
- **Beacon** — reads the substrate before answering (scoped read-only query against `waiting_on_larry` / `chain_events`). **Not a dashboard login** (see §7). This is the safe, correct version of "let Beacon see what I see."

## 4. Current-state reference map *(what exists today)*

**Three terminal channels:** (1) Telegram DM — single transport, Beacon bot drains `larry-alerts.jsonl`; (2) dashboard surfaces — read Supabase + JSON; (3) Missions Parked ingest.

**Stores (sources of truth):**

| Store | Role | Self-resolves by | Drift risk |
|---|---|---|---|
| `beacon-pending-approvals.json` (**P**) | approval queue | Telegram approve → `resolve()` pop | **dashboard approve never pops it** (Phase 1 fixes) |
| Supabase `chain_events` (**C**) | dashboard SoT | `read_at` (click / Telegram / healer) | low (atomic CAS, deterministic `event_id`) |
| `larry-alerts.jsonl` (**A**) | alert log + DM feed | silence / retract / triage | retraction doesn't clear shipped C row |
| `for-larry-escalations.json` (**E**) | escalation signal | `resolved:true` self-clear | holds different PRs than P |
| `system-state-log.json` (**S**) | derived read view (good) | recomputed each tick (~15min) | none — but escalations bucket undercounts |
| `missions/projects/captures.json` | board / Parked lane | promote/drop, phase flip | parked capture has no completion write-back |

**Reconcilers that exist:** A→P add (`heal_unregistered_approval`, 15m); P/H→C clear (`heal_stale_approvals` main tick, 10m); P→C on interactive resolve (`_clear_dashboard_pending`, best-effort); P-phantom→retire-on-MERGED (`heal_stale_approvals` terminal-reconcile, 10m); **C-resolved→P-pop (`heal_stale_approvals` resolved-in-supabase, 10m — NEW in Phase 1)**; P→S→doorbell (state log + doorbell, 30m).

## 5. The drift seams / gaps (ranked)

1. **Dashboard-approve → pop P** — *was no deterministic reconciler.* The live bug. **CLOSED by Phase 1** (resolved-in-supabase reconciler).
2. **terminal-reconcile is prefix-blind** — `expand_variants` never stripped `mirror-review-`/`heal-`/`fix-`, so `mirror-review-*` ids never matched their own merged PR. **CLOSED by Phase 1** (prefix-strip).
3. **No reconciler keys off `read_at` / `larry_action`** — **CLOSED by Phase 1** (the new reconciler keys off `larry_action`).
4. **`for-larry-escalations` / `larry-alerts` / `beacon-pending` share no identity** — Phase 2 (canonical key).
5. **State Log escalations bucket undercount** — Phase 2.
6. **larry-alerts retraction doesn't clear the shipped C row; parked captures have no completion write-back** — Phase 2.

**Redundancy to fold in (Phase 3):** two doorbells; three promotion timers; two `for-larry` writer modules; `_primary_chat_id()` copy-pasted across three files.

## 6. Phased plan

- **Phase 0 — stop the bleeding** *(DONE 2026-06-30)*: popped the 4 already-approved phantoms from P → doorbell quiet. Backup `/tmp/beacon-pending-approvals.backup.20260630T192847Z.json`.
- **Phase 1 — close the load-bearing gap** *(BUILT — this PR)*: (a) `heal_stale_approvals.reconcile_resolved_in_supabase` — pops a pending entry when a `larry_action` approve/reject exists in `chain_events` for its task_id (deterministic, idempotent, additive to Telegram self-pop); (b) `task_terminal_state.expand_variants` strips the `mirror-review-`/`heal-`/`fix-` wrapper prefixes so terminal-reconcile matches a wrapped id to its merged PR. Healer-side only — no change to the live dashboard approve/dispatch path. *Outcome: dashboard approvals clear Beacon's queue within one 10-min tick.*
- **Phase 2 — converge the stores under one decision key** *(spec written → [approval-sync-phase2-spec.md](approval-sync-phase2-spec.md))*: canonical key across P/C/E/A; one `resolve_decision(key, outcome)` fan-out called by both surfaces; fix the State Log escalations-bucket undercount.
- **Phase 3 — make the Approvals tab the one surface** *(team spec)*: fold the dormant Operator Action Queue's 6-source sourcing into the Approvals tab; split decisions vs parked; add a single global "N need you" nav counter; point Live System / Where-are-we / Operations at it via links instead of independent counts.
- **Phase 4 — Beacon sees the truth** *(team spec)*: scoped read-only query for Beacon against the unified substrate (not a login).

**Ordering is load-bearing:** giving Beacon dashboard visibility *first* is premature — the single accurate surface for her to reconcile against doesn't exist until Phases 1–3 land.

## 7. Security note (the original "give Beacon dashboard access" question)

**Do not give Beacon a dashboard login.** (a) Dashboard auth is binary — allowlisted email = full read **and write** to ~19 mutation endpoints (approve, autonomy dial, project launch); there is no read-only role. (b) Beacon already runs as droplet user `larry` with `python3:*` and `gh pr merge:*` — *more* capability than the dashboard exposes — so a login adds attack surface and a stealable credential for zero capability gain. (c) It would hand the *requester* side of the approval boundary an *approver* surface (self-approval). The correct mechanism is a **scoped read-only query** against the unified substrate (Phase 4).

## 8. Open questions
- Canonical key: task_id vs PR-coordinate vs a new decision_id — which survives the session-less / healer-minted cases cleanly?
- Should `resolve_decision()` live on the droplet (and the dashboard call it via the existing `/api/larry/action` → droplet path)? (Prefer single droplet owner.)
- Do we retire `for-larry-escalations.json` as a separate store and fold it into the canonical ledger, or keep it as a feed?
- ~~Does the unified queue become the Approvals tab, or a superset above it?~~ **RESOLVED 2026-06-30: the Approvals tab IS the single source of truth; other pages link to it.**

## 9. Tracker
- [x] Phase 0 — phantom cleanup (2026-06-30)
- [x] Phase 1 — deterministic C-resolved→P-pop reconciler + prefix-strip (this PR)
- [ ] Phase 2 — canonical key + resolve fan-out + escalations-bucket fix *(spec written 2026-06-30 → [approval-sync-phase2-spec.md](approval-sync-phase2-spec.md); awaiting build)*
- [ ] Phase 3 — Approvals tab as the one surface + decisions/parked split + nav counter *(team spec)*
- [ ] Phase 4 — Beacon scoped read *(team spec)*
- [x] UI/UX assessment appended (§10) — live dive 2026-06-30

## 10. UI/UX assessment *(live dashboard dive — 2026-06-30, signed in as larry@sealteamleaders.com)*

**Live finding — three surfaces gave three different answers to "what needs you" at the same moment**, minutes after Phase 0 popped the 4 phantoms:

| Surface | Showed | Source | Correct? |
|---|---|---|---|
| **Approvals tab** | "No decisions need you" · PENDING (0) | live Supabase query (`read_at IS NULL`) | ✅ correct |
| **CEO briefing card** (top of Approvals) | "You have **four items** waiting on your call" + a "**4 waiting on you**" tag | yesterday's 6am `ceo_digest` snapshot, never reconciled | ❌ stale |
| **Where-are-we → Waiting on You** | "**27 waiting** — 0 escalations · **4 approvals** · 23 parked" and **listed the exact 4 popped items** | State Log snapshot (~15min recompute lag) | ❌ stale window |

This is the fragmentation made visible: a correct surface, a frozen-at-6am surface, and a 15-minute-lagged surface, side by side. The narrator prose even names the fix in Larry's own words — *"the finished 'one list of what needs you' feed is built and reviewed but jammed on its final switch-on step"* (the dormant Operator Action Queue).

**Findings & recommendations:**

1. **No global "needs you" counter in the nav.** → **Add one nav badge** fed by the unified substrate.
2. **"Needs you" is computed by ≥3 surfaces with ≥3 freshnesses.** → **All read the one substrate**, and the substrate becomes **event-driven** (recompute on resolve, not only on a 15-min timer). The staleness window is itself a UX bug: Larry acts, then sees his own resolved items still listed elsewhere and doubts the action took.
3. **The CEO briefing's count *tags* masquerade as current.** → Render the chips from a **read-time live query**, or visibly **freeze + timestamp** them ("as of Mon 6am").
4. **"Waiting on You" conflates urgency tiers into one alarming number** (27 = 0 + 4 + 23). → **Decisions are the headline; parked backlog is a separate, calmer lane.** *(Confirmed 2026-06-30.)*
5. **The unified feed already exists — fold it in, don't rebuild.** `OperatorActionQueuePanel.tsx` + `lib/operator-queries.ts` is a built, reviewed, 6-source query never mounted. → Fold its sourcing into **the Approvals tab** (the single source of truth), with three fixes: (a) reconcile its approval filter (it queries `approval_request` only and drops `clarify_request`); (b) read the **unified substrate** rather than independently re-querying 6 sources; (c) present **decisions vs parked separately**.
6. **IA fragmentation — four names for facets of "things involving you":** Approvals · Where-are-we/Waiting-on-You · Live System/Needs-your-attention · Operations/Escalations. → **The Approvals tab is the one home**; the others **link into it** instead of re-deriving their own counts. Keep the defensible *conceptual* split (silent ops alerts vs blocking decisions) but on **one identity + one resolution**.

**Decision (2026-06-30):** the **Approvals tab becomes the single source of truth** for all actions needing Larry; every other page links to it. The dormant Operator Action Queue's sourcing is folded into the Approvals tab (Phase 3), not stood up as a separate panel.
