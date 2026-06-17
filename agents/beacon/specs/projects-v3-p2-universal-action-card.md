# Spec: Projects Tab v3 — P2: Universal action card on the funnel

**Status:** Ready to build
**Owner / approver:** Larry (approved 2026-06-16; Promote deferred to P3 by his call)
**Author:** Claude Code (desktop design session)
**Parent North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) (§7 P2)
**Builds on (shipped):** P1 (funnel data model + drain) · P4 (completion engine + Contract D) · Phase 4/4.1 (Narrator + Parked meaning-layer card) · the delegate-endpoint fix.
**Build path:** build-sequence orchestrator (multi-repo: `ourliberty-agent-core` + `ourliberty-dashboard`).

---

## 0. Desired End State
**Every item in the funnel — your parked notes, the team's suggestions, and the orphans — is the same plain-English card**: a briefing (what it is / why it matters / suggestion), a risk level, and the same one-click actions (**Delegate to team · Snooze · Drop · Talk-to-team**). No more bare "accept/dismiss" on cryptic orphans, no more raw machine names — and the team (Beacon/Medic/Pulse) can put a suggestion in front of you the same way.

## 1. Why now
P1 reframed the funnel data; P4 made completions self-report. Now make the funnel itself *usable*: today only Parked captures have the meaning layer + rich actions; orphan/suggested items are still raw machine names with only bare accept/dismiss. This is the operator-facing payoff — the board you'll actually use.

## 2. Scope & non-goals
**In:** (A) meaning layer on orphan + suggested cards; (B) the unified action set — Delegate/Snooze/Drop/Talk — on mission-backed funnel items; (C) multi-source suggestion intake (Beacon/Medic/Pulse → funnel); (D) the dashboard funnel card UI.
**Out:** **Promote** (deferred to P3 — its home is the project/phase pipeline; shipping a half-version now would re-create inert records). The pipeline itself (P3), closeout-on-card (full P4), dashboard-wide rollout (P7).

## 3. Reuse & restock  *(consulted shelf + graph 2026-06-16)*
**Reuse:**
- **`missions_narrator.py`** — the Narrator that briefs Parked captures (Phase 4.1). **Extend it to brief orphan + suggested items** (same `briefing`/`risk` field contract); it already runs on the GC tick. Reuse, don't reinvent. (`ceo-digest-briefing` is the underlying pattern.)
- **The capture-action machinery** in `dashboard_api.py` (`/api/missions/captures/{id}/action` + `/delegate`) + the **delegate endpoint** (shipped) — **generalize to mission-backed funnel items** so an orphan/suggested card gets the same Delegate/Snooze/Drop/Talk. ⚠️ `dashboard_api.py` has **22 dependents + writes chain_events** — surgical, well-tested changes only.
- **`inbox-dispatch`** — safe routing of a Delegate to the team's inbox (the delegate path already uses this shape).
- **CLARIFY rails / conversation card** — Talk-to-team.
- **The Parked meaning-layer card** (dashboard #54) — **generalize into one funnel card component** rendered for parked/suggested/orphaned (off P1's funnel derive). `dashboard-api-client` hooks for the data.

**Restock (after build):** `scripts/missions_narrator.py` and `scripts/heal_missions_card_gc.py` (both uncatalogued, 3 deps each) — add shelf cards.

## 4. Contracts

### A — Meaning layer on orphan + suggested cards
The Narrator authors `briefing {what, why, suggest}` + `risk {safe|medium|careful}` (+ a card-specific note) for **orphan-derived and suggested** funnel items, not only parked captures — on the same GC-tick schedule, single-committer safe, re-brief on state change. A never-yet-briefed card shows the graceful "still being written up" state, never raw machine fields.

### B — Unified action set on mission-backed funnel items
An orphan/suggested card (backed by a `missions.json` entry) exposes the **same actions as a Parked capture**: **Delegate** (→ Beacon proposal, the proven path), **Snooze** (hide until a date), **Drop** (acknowledge/hide — supersedes bare "dismiss"), **Talk-to-team** (conversation thread). Backend: generalize the capture-action handlers so they operate on a funnel item regardless of backing store (capture vs mission); reuse the existing envelopes. **Promote is intentionally absent (P3).** All writes honor the single-committer invariant (Contract D); no action leaves a dirty tree.

### C — Multi-source suggestion intake
**Beacon, Medic, and Pulse** can each propose a funnel card through **one shared interface** (a `suggested`-source entry written via the existing safe-inbox/ingest path), tagged with its source. The suggestion lands in the funnel's primary lane (suggested) and gets briefed by A. No bespoke per-agent path.

### D — The funnel card UI (dashboard)
One **funnel card component** renders the meaning layer + the unified action set, used for **parked + suggested (primary)** and **orphaned (secondary, collapsed)** lanes, off P1's funnel derive. Replaces the bare orphan accept/dismiss UI. Plain language always; technical detail on demand. Reuses/generalizes the Parked card (#54).

## 5. Risks & guardrails
- **`dashboard_api.py` is high-blast (22 deps, writes chain_events)** — generalize the action handlers surgically; unit-test; regress nothing existing (captures keep working).
- **Single-committer / no jam (Contract D)** — every action that writes `missions.json`/`captures.json` goes through the owner-committer; no action leaves a dirty tree (the P1 failure mode).
- **Don't lose the orphan decision-queue function** — Drop must still stop re-proposing (the dismiss/acknowledged semantics survive under the new label).
- **Briefing fail-safe** — robust LLM-JSON parse + deterministic fallback; never leak raw metadata to a card.
- **No Promote** — keep it out of P2 entirely so no inert records are created before the pipeline exists.

## 6. Done-gate
- Every funnel lane (parked/suggested/orphaned) renders the same plain-English card with briefing + risk + Delegate/Snooze/Drop/Talk; no raw machine names, no bare accept/dismiss.
- A real orphan card can be delegated (→ team), snoozed, dropped, and talked-to end-to-end (browser-verified).
- Beacon/Medic/Pulse can each land a suggested card via the shared interface; it briefs automatically.
- Captures keep working unchanged; no sync jam; tests cover A–D.

## 7. Build sequence (recommended — finalize via DAG-preflight)
Multi-repo. **Serialization hazard:** A touches the Narrator + GC healer; B touches `dashboard_api.py`; both write machine files via the owner — serialize anything that overlaps the GC healer / `dashboard_api.py` / `missions.json` committer.

| Step | Contract | Repo | File(s) | depends_on |
|---|---|---|---|---|
| **p2-meaning-layer** | A | agent-core | `scripts/missions_narrator.py`, `scripts/heal_missions_card_gc.py` | — |
| **p2-actions** | B | agent-core | `scripts/dashboard_api.py` (capture-action generalization) | — |
| **p2-suggest-intake** | C | agent-core | `scripts/heal_orphan_autoregister.py` / shared suggest interface | p2-actions |
| **p2-funnel-card-ui** | D | dashboard | funnel card component + proxy routes | p2-meaning-layer, p2-actions |

Each step ends at its done-gate (tests green + the contract demonstrably holds + the operator can act on the card). Mirror's DAG-preflight finalizes ordering/serialization.

## 8. Note
Once P2 is live, P3 (the project/phase pipeline) adds **Promote** ("admit into the pipeline") on top of this card — the one action intentionally held back here.
