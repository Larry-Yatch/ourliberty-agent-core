# projects-v3 P7 — Generalize + shelf + dashboard-wide

**Type:** Payoff phase for projects-v3 (the capability becomes reusable, not a Projects-tab feature).
**North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) §7 P7 + §8 (durability mandate) + §6 (autonomy dial).
**Depends:** P2–P6 + P6.1 — all shipped/live (funnel + pipeline + meaning layer + brainstorm + edit/converse).

> **Revision note (after P7.1 shipped + reading the live Approvals code):** the Approvals
> pending queue is already a mature, virtualized, severity-coded, task-grouped surface (the
> "approvals-queue-rework"). Swapping it onto the new `UniversalCard` would *regress* it, and
> approval items carry no Narrator briefing today to fill the meaning layer. So P7.2 is
> re-scoped **backend-first**: teach the Narrator to brief approval items, then surface that
> meaning **additively inside the existing queue** — never a card swap. (§6 P7.2a/P7.2b.)

---

## 0. Desired End State *(the destination)*

**The plain-language "universal card" is a reusable capability, and the meaning-layer grammar reaches a second surface — additively, where it helps.** Concretely, after this phase:

- The funnel→pipeline→brainstorm→spec→build→closeout capability is **cataloged as a shelf descriptor** (capability statement, contracts, reuse_mode, seams, invariants) so future products and the other tabs adopt it instead of rebuilding (North Star §8 durability mandate).
- The card's reusable spine — **the meaning layer (what/why/suggest + risk dial) + card chrome + a pluggable action set** — is a **surface-agnostic component** (`UniversalCard`), with the Missions funnel as its first consumer (behavior-preserving). **[SHIPPED — P7.1, dashboard #80.]**
- **Approvals gains the meaning-layer grammar:** the Narrator authors a plain-English **briefing (what/why/suggest) + risk dial** for each pending decision, and the Approvals queue renders it **additively inside the existing card** (on expand), so Larry gets plain-language context on *what he's being asked and why it matters* instead of only the raw agent prompt. The dense collapsible / severity-coded / task-grouped / virtualized queue and all its actions are **preserved** — the meaning layer is an enhancement, not a replacement.
- **The autonomy dial is visible (read-only):** each briefed item shows its risk-derived disposition ("SAFE — would auto-pass at the loose setting") off the existing `trust_policy`, so Larry can *see* the dial before it ever *acts*.

## 1. Why now

P1–P6.1 built the whole loop **on one surface** (Missions/Projects), and the North Star always framed that loop as the *reusable capability*, not a tab feature (§8). P7.1 already extracted the reusable card spine (`UniversalCard`) — proven on the funnel. The remaining payoff is **plain-language reach**: the Approvals queue today shows raw agent prompts; the Narrator that already briefs captures (what/why/suggest + risk) can brief approvals too, giving Larry the same plain-English read on his most decision-dense surface. The Narrator plumbing exists (see §3), so this is assembly, and the context is hot.

## 2. Scope & non-goals

**In (this cut):**
- **(agent-core)** Extend the Narrator to **author a briefing + risk for pending approval items** (`approval_request`, `clarify_request`; `escalation` only when it lacks its own summary), reusing the `author_meaning_layer` schema + `trust_policy` risk. Non-committer (single-writer discipline), idempotent, per-tick capped, with the deterministic fallback. Attach the briefing to the approval row and serve it inline on the approvals list.
- **(dashboard)** **Additively** render the briefing (BriefingBlock) + risk dial (RiskBadge) inside the existing `PendingCard` (on expand) **when present** — reusing the shared meaning-layer components. Absence is legal: no briefing → today's behavior, unchanged.
- **(dashboard, light)** **Autonomy dial — visibility only:** show each briefed item's risk-derived disposition + a read-only `trust-policy` summary.

**Out (explicit boundary):**
- **No queue replacement / card swap.** `PendingCard`'s collapsible row, severity dots, source pills, markdown body, sticky action bar, task grouping, and `VirtualList` all stay. We do NOT render Approvals through `UniversalCard` (it would regress the queue and show an empty meaning layer). The meaning layer is *added inside* the existing card.
- **No new approvals action model.** Approve / Reject / Comment / Mark-done keep going through the existing `/api/approvals/action`; this cut changes presentation context, not the action path.
- **No autonomy *auto-fire*.** The dial is read-only here; a healer that auto-passes `safe` items is a deliberate later step (visibility before action).
- **No Operations / Alerts adoption.** Neither has a decision surface that benefits today; a follow-on once the meaning layer has proven out on Approvals.
- **No Programs↔Projects unification, no drag-drop** (North Star out-of-scope, unchanged).

## 3. Constraints & reuse *(assembly vs greenfield — verified in code)*

- **The card spine is already a shared component (DONE).** `UniversalCard` (`components/UniversalCard.tsx`) + the namespace-parameterized meaning layer (`app/missions/components/MeaningLayer.tsx`: `BriefingBlock`/`RiskBadge`/`RiskNote`) shipped in P7.1. The Approvals front-end reuses `BriefingBlock`/`RiskBadge` directly — it does NOT need `UniversalCard` (the queue keeps its own card shell).
- **The Narrator briefing is ASSEMBLY (verified).** `scripts/missions_narrator.py::author_meaning_layer` already authors `briefing {what,why,suggest}` + `risk` (safe|medium|careful) + `risk_note` + `recommended_action` + `briefing_provenance {by,model,at,from_state}` for captures, via a `claude` CLI call with a deterministic `render_raw_briefing` fallback, and a `needs_briefing` idempotency guard (re-author only on a state change; never re-author terminal items), capped at `NARRATOR_MAX_PER_TICK`. It is a **non-committer** — it mutates the in-memory registry and the owning healer does the sole atomic write. Briefing approvals = a sibling sweep that reuses this machinery over the approval rows.
- **Risk reuses `trust_policy` (verified, with a graceful caveat).** `derive_risk` maps `trust_policy.evaluate(capture_to_task(...))` → safe/medium/careful, with a careful-keyword escalator. An `approval_request` payload carries `proposing_agent`/`target_agent`/`prompt` but **not** the full dispatch task context (`task_type`/`target_repo`/`changed_files`), so policy matching is coarse. **Guardrail:** derive risk from what's available (target/source + the careful-keyword scan over the prompt) and **fall back to the event's `severity`** (critical→careful, warning→medium, info→safe) when policy can't classify — never block on missing task context.
- **The attach point is the approval row (single-writer).** The approval items are `chain_events` rows served to the dashboard's approvals list. The Narrator writes the new `briefing`/`risk`/`risk_note` fields onto the row (the ONLY writer of those fields; the emitter never touches them), and the approvals serve path returns them inline — the dashboard reads them exactly as it reads a capture's briefing. Idempotency on a `briefing_provenance`/`briefing_authored_at` stamp (approval rows are point-in-time, so author once per item, not per serve).
- **Plain-language + absence-is-legal preserved** (North Star §4.10): a briefed approval reads in Larry's terms; an un-briefed one degrades to exactly today's card (no empty block, no raw machine fields).

## 4. Risks & guardrails

- **Don't regress the queue (additive only).** The Approvals pending UX (collapse, severity dots, source pills, markdown body, sticky actions, task grouping, virtualization) must be untouched; the meaning layer is rendered *inside* the existing expanded card, gated on `briefing` presence. The existing Approvals test suite is the regression gate.
- **Narrator non-committer for the new field.** The approval briefing MUST go through the single-writer path (Narrator authors → the owning healer/store persists); the emitter and the dashboard never write it. No second committer.
- **Graceful absence + degraded risk.** No briefing → today's behavior. Risk that can't be policy-classified falls back to `severity`; it never blocks or guesses wildly. `risk_note` required for medium/careful, omitted for safe (same contract as captures).
- **Cost + cadence bound.** One LLM call per newly-briefed approval, shared with the captures sweep under the per-tick cap (tune a separate cap if approvals starve captures); deterministic fallback when the voice is unavailable. Author once per item (idempotency stamp), not per serve.
- **Dial is advisory-only.** The disposition reads "would auto-pass *if* the dial were loose" — never implies anything fired. No write path from the card.
- **Doorbell unchanged.** Approvals actions + the Telegram doorbell resolution are unchanged (presentation-only cut on the read side).

## 5. Done-gate *(BROWSER-CHECKABLE — run live on the dashboard)*

- [ ] **Missions unchanged:** the funnel still renders + acts identically on `UniversalCard` (P7.1 regression — already green).
- [ ] **Approvals briefed:** a pending `approval_request` (and a `clarify_request`) shows a plain-English what/why/suggest briefing + a risk dial inside its expanded card.
- [ ] **Queue intact:** the collapse/expand, severity dots, source pills, markdown body, sticky action bar, task grouping, and virtualization all still work; Approve / Reject / Comment / Mark-done still resolve the item.
- [ ] **Absence is legal:** an un-briefed approval renders exactly as today (no empty meaning block, no raw fields).
- [ ] **Risk degrades gracefully:** an approval with no policy match still shows a sane risk off its severity.
- [ ] **Dial visible:** a `safe` briefed item shows "would auto-pass (loose)"; a `careful` one "always needs you" — read-only.
- [ ] **Non-committer held:** the briefing is written once via the Narrator's single-writer path; no second committer; the dashboard stays read-only.
- [ ] **Shelf descriptor exists:** the projects-v3 capability descriptor is authored against the new schema and review-approved as the reuse template.

## 6. Breakdown (steps → DAG)

1. **p7-shelf-descriptor** *(ourliberty-agent-core)* — **End state:** *the projects-v3 capability is cataloged as a reusable shelf descriptor.* Define the descriptor schema (North Star §8 fields: capability statement, contracts, reuse_mode, seams, invariants) and author the first descriptor for the funnel→pipeline→brainstorm→spec→build→closeout capability, naming the generic vs surface-specific boundary. Documentation artifact — nothing reads it at runtime. *(no deps)*
2. **p7-universal-card** *(ourliberty-dashboard)* — **End state:** *the funnel card is a surface-agnostic component.* **SHIPPED (#80):** extracted `FunnelCard`'s reusable spine (card chrome + per-card error boundary + header + meaning layer) into `UniversalCard` with a pluggable children slot; `FunnelCard` is a thin wrapper. Behavior-preserving. *(no deps)*
3. **p7-brief-approvals** *(ourliberty-agent-core)* — **End state:** *pending approvals carry a plain-language briefing + risk.* Add a Narrator sweep that authors `briefing`/`risk`/`risk_note` for `approval_request` + `clarify_request` (and summary-less `escalation`) items, reusing `author_meaning_layer` + `trust_policy` risk (degrading to `severity` when task context is missing), non-committer, idempotent (author-once stamp), per-tick capped, deterministic fallback. Attach to the approval row + serve inline on the approvals list. Tests: schema, risk-degrade, idempotency, non-committer. *(no deps; the Narrator/serve plumbing exists)*
4. **p7-approvals-meaning-layer** *(ourliberty-dashboard)* — **End state:** *the Approvals queue shows the briefing additively.* Render `BriefingBlock` + `RiskBadge` inside the existing `PendingCard` (on expand) when the served approval carries a briefing; absence-is-legal (unchanged today otherwise). Reuse the shared meaning-layer components; **no** queue/card swap. Tests: briefed renders meaning layer, un-briefed unchanged, queue affordances intact. *(dep: 3)*
5. **p7-autonomy-dial-visibility** *(ourliberty-dashboard + light agent-core)* — **End state:** *Larry can see the dial.* Surface each briefed item's risk-derived disposition ("would auto-pass at loose" / "always needs you") off `trust_policy`, plus a read-only trust-policy summary. No auto-fire. *(dep: 4)*

**DAG:** {1} standalone (the durability artifact) ‖ {2 ✅} ‖ {3 → 4 → 5} (brief-approvals → additive surfacing → reveal the dial). **Recommended cut: ship 1 + 3 + 4** (give Approvals the plain-language grammar where it actually helps); review the dial (5) + the Operations/Alerts rollout + autonomy auto-fire as the next phase once briefed-Approvals has lived on the board. **Closeout MUST confirm every §5 item live in the browser, including the Approvals-queue-intact regression.**
