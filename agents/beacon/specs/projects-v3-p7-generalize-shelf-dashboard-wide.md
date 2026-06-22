# projects-v3 P7 — Generalize + shelf + dashboard-wide

**Type:** Payoff phase for projects-v3 (the capability becomes reusable, not a Projects-tab feature).
**North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) §7 P7 + §8 (durability mandate) + §6 (autonomy dial).
**Depends:** P2–P6 + P6.1 — all shipped/live (funnel + pipeline + meaning layer + brainstorm + edit/converse).

---

## 0. Desired End State *(the destination)*

**The plain-language "universal card" is a reusable capability, and a second dashboard surface speaks it.** Concretely, after this phase:

- The funnel→pipeline→brainstorm→spec→build→closeout capability is **cataloged as a shelf descriptor** (capability statement, contracts, reuse_mode, seams, invariants) so future products and the other tabs adopt it instead of rebuilding (North Star §8 durability mandate).
- The card's reusable spine — **meaning layer (what/why/suggest + risk dial) + chat thread + a pluggable action set** — is a **surface-agnostic component**, with the Missions funnel as its first consumer (behavior-preserving).
- **Approvals is the first new surface to adopt it:** pending approval items render as the universal card (plain-English briefing + risk + the same chat affordance), with an Approve / Reject / Comment / Mark-done action set instead of the funnel's Delegate / Snooze / Drop / Promote. Telegram becomes just a doorbell for Approvals too.
- **The autonomy dial is visible (read-only):** each card shows its risk-derived autonomy disposition ("SAFE — would auto-pass at the loose setting") off the existing `trust_policy`, so Larry can *see* the dial before it ever *acts*.

Net: one capability, authored once, now rendered on two surfaces with the same plain-language grammar — proving the generalization and shelving it so the remaining tabs (Operations, Alerts) and future products are assembly, not rebuilds.

## 1. Why now

P1–P6.1 built the whole loop **on one surface** (Missions/Projects). The North Star always framed that loop as the *reusable capability*, not a tab feature (§8). The pieces are now mature and — crucially — the investigation confirms most of the spine is **already generic**: `MeaningLayer` (`BriefingBlock`/`RiskBadge`/`RiskNote`) is namespace-parameterized, `ClarifyRoundDrawer` + `ClarifyReplyBox` are pure and label-overridable (and `ClarifyReplyBox` *already lives under* `app/approvals/components/`), `CaptureActionBar` is action-agnostic, and the dashboard-api card-chat core (`_CARD_KIND_META` / `_post_card_message` / `_card_thread_messages`) is already kind-generic (kinds today: capture, mission, phase). So this is mostly **assembly + one greenfield lift (the shelf descriptor)** — the context is hot and the seams are proven.

## 2. Scope & non-goals

**In (this cut):**
- **(agent-core)** Define the **shelf-descriptor schema** + author the **first descriptor** cataloging the projects-v3 capability. Register an **`approval` card kind** in `_CARD_KIND_META` *only if* Approvals chat is in scope for the slice (see §6 P7.2 — optional v1).
- **(dashboard)** **Generalize** the funnel card into a surface-agnostic `UniversalCard` (meaning layer + thread + a pluggable action-set slot), Missions funnel migrated onto it with **no behavior change**. Then **Approvals adopts it**: an `approval → normalized-item` adapter + an Approve/Reject/Comment/Mark-done action set, reusing the meaning layer + the existing approvals action endpoint.
- **(dashboard, light)** **Autonomy dial — visibility only:** surface each card's risk-derived disposition + the current `trust-policy` summary. Read-only.

**Out (explicit boundary):**
- **No autonomy *auto-fire*.** The dial is read-only here. A healer that auto-executes `safe`-rated items (auto-approve / auto-pass closeouts) is a deliberate **later** step — visibility first, action after Larry has watched it for a while (the same "see it before it acts" discipline as the rest of projects-v3).
- **No Operations / Alerts adoption yet.** Neither has a first-class *decision-card* model today (Operations is status monitoring; Alerts isn't a separate tab). Adopting them needs their own item model and is a follow-on once Approvals proves the pattern.
- **No Approvals data-pipeline rebuild.** `/api/approvals/list` + `/api/approvals/action` already serve the data and actions; this re-skins the surface, it doesn't re-plumb it.
- **No Programs↔Projects unification, no drag-drop** (North Star out-of-scope, unchanged).

## 3. Constraints & reuse *(assembly vs greenfield — verified in code)*

- **The card spine is already generic (ASSEMBLY).** `MeaningLayer.tsx` (`BriefingBlock`/`RiskBadge`/`RiskNote`) takes `namespace`+`id` so testids stay surface-scoped; `ClarifyRoundDrawer.tsx` takes `questionLabel`/`answerLabel` overrides; `ClarifyReplyBox` is already shared (Missions FunnelCard **and** Approvals PendingCard use it). The only Missions-locked piece is **`FunnelCard`'s fixed action set** (Delegate/Snooze/Drop/Talk/Promote) and its capture-bound thread endpoints. The generalization = lift `FunnelCard`'s reusable layout into `UniversalCard` and make the **action set a slot/prop**, then keep `FunnelCard` as a thin wrapper that passes the funnel action set (Missions stays green).
- **The adapter pattern is the surface seam (ASSEMBLY).** `lib/funnel.ts` already maps wire types → one `FunnelItem` view-model (`parkedToFunnelItem`/`suggestedToFunnelItem`/`orphanToFunnelItem`). A new surface plugs in by adding `approvalToFunnelItem(event) → FunnelItem` (+ a small superset type for the surface-specific bits). The generic card never learns about approvals; the adapter + the action set carry the difference.
- **The card-chat backend is already kind-generic (ASSEMBLY).** `dashboard_api._CARD_KIND_META` keys chat on a generic kind (`id_key`, `noun`, `thread_url`) over `_post_card_message`/`_card_thread_messages`; adding `approval` is one meta entry + the two thin routes (find/404 → delegate to the cores), exactly as the P6.1 `phase` kind was added. **Optional for the slice** — Approvals already has Comment via `/api/approvals/action`; a full Beacon-answers thread is only needed if we want two-way conversation on approval items.
- **The Narrator briefing is OPTIONAL for approvals (GRACEFUL).** Approval items are already plain-language structured requests (the agent's `prompt`/`question`/`message`), and the meaning-layer contract renders **absence as neutral** — so an approval card with no Narrator-authored briefing simply shows the request text + a neutral state, no garbage. We do **not** need to extend the Narrator to brief approvals in this cut; risk/recommended_action stay optional.
- **The autonomy substrate exists (ASSEMBLY for visibility).** `scripts/trust_policy.py` already evaluates a task → `auto_approve` / `force_ask` / `reject`, re-read on every call from `config/trust-policy.json`, and the Narrator already maps that to the `safe`/`medium`/`careful` risk dial. The dial-visibility step **reads** that, it doesn't invent a new policy engine.
- **The shelf descriptor is GREENFIELD (must define).** No descriptor schema or example exists in the repo today; the ourliberty-graph shelf is referenced but its descriptor format is undefined. P7 must define the schema (North Star §8 names the fields: capability statement, contracts, reuse_mode, seams, invariants) and author the first descriptor for the projects-v3 capability — that first descriptor becomes the template the other tabs/products mirror.
- **Plain-language + AI-draft framing preserved** (North Star §4.10): every surface keeps the same grammar — no raw machine fields, absence is legal, no bare accept/dismiss.

## 4. Risks & guardrails

- **Behavior-preserving generalization.** Lifting `FunnelCard` → `UniversalCard` must not change the Missions funnel at all: same testids, same actions, same thread. The Missions component test suite is the regression gate — it stays green untouched (the wrapper passes the funnel action set). Approvals is purely additive.
- **Don't over-abstract.** The card takes a normalized item + an action-set descriptor; it does NOT grow a config DSL. Surface differences live in the adapter + the action set, not in branching inside the card. If a surface needs a bespoke affordance the generic card can't express, that's a signal to keep that affordance surface-local (e.g. Approvals' event-type-gated Approve/Reject), not to bloat the card.
- **Approvals action parity.** Re-skinning Approvals must preserve every current affordance: Approve/Reject (+ optional comment) on `approval_request`, free-text reply on `clarify_request`, Mark-done on alerts, and the escalation `needs_response` conditional. The existing `PendingCard` affordance routing is the checklist; nothing regresses.
- **Autonomy dial is advisory-only here.** The disposition label MUST read as "would auto-pass *if* the dial were set to loose" — never imply anything auto-fired. No write path, no policy mutation from the card. (Editing `config/trust-policy.json` stays a deliberate, separate action.)
- **Single-committer + doorbell invariants unchanged.** Approvals writes still go through `/api/approvals/action` → the droplet; any chat (if added) goes through `_post_card_message`'s single-committer path. The dashboard stays a non-committer.
- **Descriptor is documentation, not a coupling.** The shelf descriptor catalogs the capability; nothing in the running system reads it at runtime in this cut. It must not become a load-bearing config — it's the durability artifact, kept honest by mirroring the real seams.

## 5. Done-gate *(BROWSER-CHECKABLE — run live on the dashboard)*

- [ ] **Missions unchanged:** the funnel still renders + acts identically after the `UniversalCard` extraction (Delegate/Snooze/Drop/Promote/Talk all work; testids intact; component suite green).
- [ ] **Approvals as cards:** the Approvals Pending bucket renders the universal card with plain-English headline/briefing (or neutral fallback) + risk dial, not the old bespoke `PendingCard`.
- [ ] **Approvals actions parity:** Approve, Reject (+ comment), Comment-reply on a clarify item, and Mark-done all work end-to-end against `/api/approvals/action` and the item resolves (moves to Acknowledged).
- [ ] **Dial visibility:** a `safe`-rated approval shows a "would auto-pass (loose)" disposition; a `careful` one shows "always needs you" — read-only, nothing fires.
- [ ] **Shelf descriptor exists:** the projects-v3 capability descriptor is authored against the new schema (capability statement, contracts, reuse_mode, seams, invariants) and review-approved as the reuse template.
- [ ] **Non-committer + doorbell held:** Approvals actions still single-commit via the droplet; no second committer; the Telegram doorbell still resolves on a dashboard action.

## 6. Breakdown (steps → DAG)

1. **p7-shelf-descriptor** *(ourliberty-agent-core)* — **End state:** *the projects-v3 capability is cataloged as a reusable shelf descriptor.* Define the descriptor schema (North Star §8 fields: capability statement, contracts, reuse_mode, seams, invariants) and author the first descriptor for the funnel→pipeline→brainstorm→spec→build→closeout capability, naming the generic vs surface-specific boundary. Documentation artifact — nothing reads it at runtime. *(no deps)*
2. **p7-universal-card** *(ourliberty-dashboard)* — **End state:** *the funnel card is a surface-agnostic component.* Extract `FunnelCard`'s reusable spine (meaning layer + thread slot + a pluggable action-set) into `UniversalCard`; `FunnelCard` becomes a thin wrapper passing the funnel action set. Behavior-preserving — the Missions funnel + its tests are unchanged. *(no deps)*
3. **p7-approvals-adopt** *(ourliberty-dashboard)* — **End state:** *Approvals speaks the universal card.* Add `approvalToFunnelItem` + an Approve/Reject/Comment/Mark-done action set; render the Approvals Pending bucket as `UniversalCard`, reusing the meaning layer + `ClarifyReplyBox` + the existing `/api/approvals/action`. Preserve every current affordance (event-type-gated). *Optional:* register an `approval` card kind in `_CARD_KIND_META` for a Beacon-answers thread (defer if Comment suffices). Tests: adapter mapping, affordance routing, action parity. *(dep: 2)*
4. **p7-autonomy-dial-visibility** *(ourliberty-dashboard + light agent-core)* — **End state:** *Larry can see the dial.* Surface each card's risk-derived disposition ("would auto-pass at loose" / "always needs you") off `trust_policy`, plus a read-only trust-policy summary. No auto-fire. *(dep: 3)*

**DAG:** {1} standalone (the durability artifact) ‖ {2 → 3 → 4} (the generalize→adopt→reveal chain). **Recommended first cut: ship 1 + 2 + 3** (prove the generalization on a second surface) and review the dial (4) + the Operations/Alerts rollout + autonomy auto-fire as the *next* phase once Approvals-as-cards has lived on the board. **Closeout MUST confirm every §5 item live in the browser, including Missions-unchanged regression.**
