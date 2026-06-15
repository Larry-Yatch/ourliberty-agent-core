# Roadmap & Capability Plan: Operator Meaning Layer + Team Chat

**Status:** Living roadmap — updated as we build (do not let it go stale)
**Author:** Claude Code (desktop session, 2026-06-14)
**Owner / approver:** Larry
**Born from:** [Missions v2 Phase 4 — operator meaning layer](../agents/beacon/specs/missions-v2-phase4-meaning-layer.md) (proven on the Parked lane, validated 2026-06-14)
**Parent design:** [docs/missions-redesign-design-pass-2026-06-09.md](missions-redesign-design-pass-2026-06-09.md)

> Keep this doc current. Each phase has a **status** line; tick the tracker (§8) as work lands. This is the thread-keeper — when we pick the work back up, start here.

---

## 1. North star

The board (and eventually the whole dashboard) should be **decidable by Larry without decoding machine metadata.** Every card the team puts in front of him carries, in his terms: **what it is · why it matters · how careful to be · what to do** — plus a way to **act in one click** and **talk to the team** right there.

**Strategic frame (Larry, 2026-06-14):** this is **not** a missions-board feature. The conversation card + the Narrator that authors it are a **reusable platform capability** that many future builds will need (the dashboard's other tabs; the next products, e.g. RSDPM). So we build it **robustly and generically, and put it on the shelf** with a descriptor — reused, not reinvented. Worth building thoroughly even though the first instance is small.

**The bar:** when we're done with the core, a new surface should be able to adopt "team-authored briefing + risk + one-click delegate + two-way chat" by reusing a shelf component — the same way today's build reused `human-approval-gate`, `trust_policy`, and the CLARIFY rails.

---

## 2. The capability, defined

Three reusable pieces. Today they're wired to parked captures; the work of §4.2 is to lift them out of that mold.

### 2a. The conversation card (UI)
A card that renders a **team-authored briefing**, a **risk badge**, a **primary one-click action** (delegate to the team), and an **on-demand chat thread** — never raw machine fields (graceful "still being written up" state when not yet briefed).

### 2b. The Narrator (authoring)
A **Beacon-owned** pass that reads a work item + its context and writes the meaning layer: `briefing {what, why, suggest}`, `risk {safe|medium|careful}` (+ a card-specific note), and a `recommended_action`. Plain-English LLM voice with a deterministic fallback. Risk is derived from the `trust_policy` dial, so the risk badge and "what the team may do unattended" are the same setting seen from two sides.

### 2c. The doorbell (notification)
Risk-and-blocked-state gates how loudly an item reaches Larry: a quiet **FYI** for awareness, a loud **blocked-on-you** when the team is waiting. Telegram is demoted to a ping that deep-links to the card.

### Field contract (authored by the Narrator, all optional on the item)
```jsonc
"briefing": { "what": ..., "why": ..., "suggest": ... },
"risk": "safe" | "medium" | "careful",
"risk_note": "<one sentence; required for medium/careful>",
"recommended_action": "delegate" | "promote" | "drop" | "snooze",
"briefing_provenance": { "by": "beacon", "model": ..., "at": <iso>, "from_state": ... }
```

### Reuse boundary (generic vs. surface-specific)
- **Generic (shelf component):** the field contract, the Narrator's read→author→write loop, the risk derivation, the card layout, the chat thread, the doorbell routing.
- **Surface-specific (per adopter):** which registry/table the items live in, the available one-click actions, and the context the Narrator reads to author from.

---

## 3. What's done

The **Parked-lane proof** (Missions v2 Phase 4) — shipped and **validated** (Larry confirmed the briefed cards read plainly, 2026-06-14):

| Piece | PR | State |
|---|---|---|
| Spec | agent-core #499 | merged |
| 1a — meaning-layer fields + Narrator | agent-core #500 | merged |
| 1b — async chat thread + doorbell | agent-core #502 | merged |
| 2 — meaning-layer Parked card (UI) | dashboard #54 | merged |

**Live state:** the Narrator's first pass was run **manually** (no schedule shipped — see §4.1); it briefed all 14 parked cards in Beacon's voice; `dashboard-api` was restarted to serve the new fields. Reused: `human-approval-gate` (one-click + governance), `trust_policy` (risk dial), the CLARIFY thread UI/endpoints, the `ceo_digest` briefing pattern, `/api/missions/derived`, `larry_alerts` (doorbell).

**Gaps carried out of the proof** (fold into the phases below, not separate work):
- **No schedule** — the Narrator is a module nothing runs (→ §4.1).
- **LLM-parse fragility** — 1 of 14 fell back to the deterministic version on a non-JSON model result (→ harden in §4.1).
- **Generic risk notes** — the "medium" note is a per-level template, not card-specific (→ §4.2).
- **No re-brief on change** — a card's briefing should refresh when its state changes (→ §4.1).

---

## 4. The roadmap (phased, in priority order)

### Phase 4.1 — Durability: schedule the Narrator  ·  **NEXT**
Make today's win *standing* instead of a one-time manual kick.
- Run the Narrator on a schedule (own timer or folded into the missions-card GC healer timer — pick the cleaner; both ~10-min cadence).
- Re-brief on state change (event-driven), not only on the periodic sweep.
- Harden the LLM-output parsing (robust JSON extraction; deterministic fallback stays the safety net).
- Watch install-drift (a brand-new timer must actually be installed + enabled on the droplet).
- Captured as `cap-schedule-the-missions-narrator…` on the Parked lane.

### Phase 4.2 — Spread to the other lanes + **generalize for reuse**  ·  (Larry's pick for after 4.1)
Put the meaning layer on the **Orphans lane** (the ~97-item pile) and the **active missions**, so the *whole* board reads plainly — and **this is where the capability becomes a shelf component**:
- Lift the Narrator + card + field contract out of the captures-only mold into a generic unit (see §2 reuse boundary).
- Catalog a **descriptor on the [ourliberty-graph] shelf** (capability statement, contract, reuse_mode, seams, invariants) via the existing pipeline, so future adopters find it.
- Card-specific risk notes land here.

### Phase 4b — Live chat (the Beacon front desk)
The one genuinely-new build: a **dashboard-reachable, near-real-time Beacon responder** so the chat feels live — decide and **close the card in the moment** — instead of the async (CLARIFY-rails) version shipped for the proof. Honest target: seconds-to-a-minute per reply (each is a model turn), not instant typing.

### Phase 4.3 — The Proposed lane (team's front door)
The team surfaces work *it* found for Larry to **accept or dismiss** (e.g. a stalled orphan worth rescuing, a recurring nudge worth promoting). Accept/dismiss backend exists (dashboard #53 / agent-core #481); needs the lane UI + Beacon proposing into it.

### Phase 4.4 — Dashboard-wide chat layer
Adopt the shelf capability across the rest of the dashboard (Approvals, Operations, Alerts); Telegram becomes purely a doorbell everywhere. This is the payoff of building §4.2 generically.

### Phase 4.5 — Turn the autonomy dial
Let the team **auto-handle the safe (low-risk) items** by itself, lane by lane, via `trust_policy` `auto_approve` rules. Pulse proposes widenings from observed success; Larry approves each. Recommend-first → graduated autonomy. The risk badge already marks which items qualify.

---

## 5. Robustness & reuse bar (the "build it thoroughly" mandate)

Because this goes on the shelf and other products depend on it:
- **Shelf-able** — cataloged with a descriptor; reuse_mode and seams documented; discoverable via the librarian.
- **Hardened** — robust LLM parsing with a deterministic fallback; atomic, single-committer writes; idempotent re-runs; a graceful unbriefed state that never leaks raw metadata.
- **Generic** — decoupled from the captures schema so any lane/tab can carry it (the §4.2 lift).
- **One voice** — all author/chat output is Beacon's; agents stay behind it.
- **Tested to the real gate** — unit tests *and* the operator-decidability gate (a human can decide unaided), per phase.

---

## 6. Locked decisions (the contract)

From the design conversation (2026-06-13/14):
- **Meaning layer first** — plain briefing beats prettier metadata; it's the product, not polish.
- **Recommend-first** — the team proposes; nothing auto-fires; one click delegates the work *to* the team (you never open an editor).
- **3-level risk** = safe / medium / careful, and it **doubles as the autonomy dial**.
- **One voice = Beacon** — Larry's single POC and the team's manager.
- **Telegram → doorbell** — FYI quiet, blocked-on-you loud.
- **Chat feels live** — async on existing rails for the proof; the real-time front desk is Phase 4b.
- **Prove on Parked first** — done + validated.
- **Build as a reusable, shelf-able capability** — robust and generic, not a missions-only feature.

---

## 7. Open questions (to decide as we reach them)

- **4.1:** own Narrator timer vs. fold into the GC-healer timer? (Lean: whichever the team finds cleaner; both share cadence.)
- **4.2:** how generic is the component boundary on the first lift — Orphans + missions only, or design the contract for arbitrary registries now?
- **4b:** what latency is "live enough," and the responder's architecture (a held session vs. per-message spawn)?
- **Shelf:** where the descriptor lives and who keeps it current as the capability evolves.

---

## 8. Status tracker (living)

| Phase | What | Status | Refs |
|---|---|---|---|
| Proof | Parked-lane meaning-layer card | ✅ shipped + validated | #499, #500, #502, dash #54 |
| 4.1 | Schedule the Narrator (durability) | ⏭️ next | `cap-schedule-the-missions-narrator…` |
| 4.2 | Spread to Orphans + missions; shelf the component | ▫️ planned | — |
| 4b | Live chat (Beacon front desk) | ▫️ planned | — |
| 4.3 | Proposed lane | ▫️ planned | backend: dash #53 / #481 |
| 4.4 | Dashboard-wide chat layer | ▫️ planned | — |
| 4.5 | Autonomy dial (auto-handle safe) | ▫️ planned | `trust_policy.py` |

_Legend: ✅ done · ⏭️ next · 🚧 in flight · ▫️ planned_
