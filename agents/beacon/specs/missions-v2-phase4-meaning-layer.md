# Spec: Missions v2 — Phase 4: Operator Meaning Layer (Parked-lane proof)

**Status:** Draft — ready to sequence (design refined with Larry, 2026-06-13/14)
**Author:** Claude Code (desktop session, 2026-06-14)
**Approver:** Larry
**Parent:** [docs/missions-redesign-design-pass-2026-06-09.md](../../../docs/missions-redesign-design-pass-2026-06-09.md)
**Predecessor:** [Phase 3 — write-back + auto-register](missions-v2-phase3-writeback-autoregister.md) (shipped + live; promote/drop/snooze exist) · [Phase 2 — relocated derive](missions-v2-phase2-resurfacing-and-derive.md) (shipped/live; `/api/missions/derived` is canonical)
**Build path:** build-sequence orchestrator, single-repo split (PR-S3 precedent), one phase = one sequence

---

## 1. Purpose

The board's cards are **technically correct and humanly unreadable**. They show machine metadata (task ids, branch names, agent names, phase codes) and make Larry the translator. He cannot make decisions on them.

Phase 4 adds a **team-authored meaning layer**: every card carries, in Larry's terms, **what it is · why it matters · how careful to be · what to do** — plus a way to **act in one click** (the work goes *to* the team, not onto Larry's plate) and **talk to the team** on the card. **Beacon is the single voice** — Larry's POC and the team's manager; the other agents work async behind it.

Proven first on the **Parked lane** (lowest-risk, highest-frequency triage). The architecture is built as a **reusable conversation card** so it later drops into Approvals/Operations/Alerts without a rebuild — but only Parked ships in this phase.

**Done-gate:** Larry looks at a parked card and, **without decoding anything**, understands what it is, why it matters, how careful to be, and acts in one click; and he can ask the team a question on the card and get an answer back. The real acceptance test (§10) is Larry triaging ~5 of his *actual* parked cards unaided.

---

## 2. Decisions locked with Larry (2026-06-13/14)

| # | Decision | How Phase 4 honors it |
|---|---|---|
| 1 | **Meaning-layer first**; live chat is a fast-follow | Ship briefing + risk + one-click now; conversation rides existing **async CLARIFY rails** this phase; near-live Beacon front desk → Phase 4b (§11) |
| 2 | **Recommend-first** posture, with **easy one-click into action** | Team proposes; nothing auto-fires; a click *delegates* to the team (reuses `human-approval-gate`) |
| 3 | **Clear risk signal** so Larry knows when to think harder | 3 levels (safe / medium / careful); doubles as the autonomy dial (§5) |
| 4 | **Live-feel** chat that lets him decide and **close the card** | Async for the proof; Phase 4b adds the dashboard-reachable Beacon responder for the real-time feel |
| 5 | **One voice** — Beacon as POC + manager | Beacon authors briefings, holds the chat, delegates, reports back; agents stay behind it |
| 6 | **Telegram → doorbell** (FYI quiet, blocked-on-you loud) | Risk + blocked-state gate notification loudness (§8) |

---

## 3. Reuse map (this is assembly, not greenfield)

Verified via the code graph + shelf (`ourliberty-graph`) on 2026-06-14:

| Card capability | Reuses (existing) | Mode |
|---|---|---|
| One-click hand-to-team + governance | `human-approval-gate` (shelf): AI proposes → human approves/modifies → dispatched → healers reconcile; dual-channel | unit+cluster |
| 3-level risk dial | `trust_policy.py` + `config/trust-policy.json` (default-ask, hot-reload) | direct |
| Beacon authors the plain-English briefing | `ceo_digest_generator` pattern: reads `chain_events` → structured brief | pattern |
| Where the briefing lives (server-authored) | `/api/missions/derived` on the droplet (Phase 2, canonical) | extend |
| Conversation thread (async) | CLARIFY rails: `ClarifyRoundDrawer` (thread), `ClarifyReplyBox` (reply), `GET /api/missions/clarify-rounds`, `POST .../action` (comment→inbox) | generalize |
| Doorbell routing + durable triage | `larry_alerts` + `alert_triage_state` | reuse |
| Card resilience / time / atomic state | `PanelErrorBoundary`, `RelativeTime`, `atomic_io` | direct |

---

## 4. Contract A — meaning-layer fields

New optional fields on a capture (and surfaced in the derived `parked[]` entry). Authored by the Narrator pass (§5), never computed dashboard-side.

```jsonc
{
  // ...existing capture fields (id, title, note, state, origin, last_touched)...
  "briefing": {
    "what":   "When a branch is rebased, the reviewer bot sometimes loses its comments.",
    "why":    "Real review feedback can silently vanish — you'd never know it was lost.",
    "suggest":"Have the team run it down and propose a fix."
  },
  "risk": "medium",                 // "safe" | "medium" | "careful"
  "risk_note": "A fix here changes how the reviewer bot behaves on every PR.",  // required for medium/careful
  "recommended_action": "delegate", // "delegate" | "promote" | "drop" | "snooze"
  "briefing_provenance": { "by": "beacon", "model": "claude-opus-4-8", "at": "<iso>", "from_state": "parked" }
}
```

- **Absence is legal.** A card with no `briefing` yet renders a neutral "briefing…" state — **never** raw machine fields.
- Fields regenerate when the card's state/context changes (idempotent; provenance stamps the source state).

## 5. Contract B — the Narrator pass (Beacon authors the meaning layer)

A Beacon-owned step (`scripts/missions_narrator.py`, reusing the `ceo_digest_generator` read pattern):

- **Input:** a capture + its `chain_events` context (origin chat, repo, related missions/PRs).
- **Output:** `briefing` + `risk` (+ `risk_note`) + `recommended_action`, written back through the **single captures committer** (atomic; the Phase 1 GC healer batches the commit) — no second writer (honors the single-committer invariant).
- **Risk derivation:** `trust_policy.evaluate(task)` → map `auto_approve` ⇒ **safe**, `force_ask` (reversible) ⇒ **medium**, `force_ask` (irreversible/outward/spendy) or `reject`-class ⇒ **careful**. The risk note explains the catch in one sentence for medium/careful.
- **Trigger:** event-driven on capture create / state change, plus a periodic sweep (same cadence family as the GC healer) so nothing stays un-briefed.
- **Voice:** always Beacon (single voice, decision #5).

## 6. Contract C — risk signal = autonomy dial

The 3 levels are a *view* of `trust_policy`, not a parallel system. In this phase risk is **annotation + doorbell input only** — recommend-first means **nothing auto-fires**. The dial is wired so a later phase can let the team auto-handle **safe** items lane-by-lane by adding `auto_approve` rules; **careful** always reaches Larry. No new policy format.

## 7. Contract D — one-click delegation

The card's **primary** button hands the recommended work to the team:

- Clicking it emits an `human-approval-gate` proposal (`APPROVAL_REQUEST` grammar) for `recommended_action`. Under recommend-first it routes to Beacon and dispatches — **Larry never opens an editor**.
- **Secondary** actions reuse the Phase 3 write-back endpoints already live: `promote` / `drop` / `snooze` (`POST /api/missions/captures/{id}/action`).
- Busy/optimistic/error UI: reuse the `ApprovalActionBar` / `CaptureActionBar` pattern (parent owns the toast).

## 8. Contract E — the conversation thread (async this phase)

Generalize the CLARIFY rails from "Forge preflight question" to **"any card"**:

- **Event types:** extend the `clarify_request` / `clarify_response` chain-event family with a capture-scoped variant (keyed by `capture_id` instead of a preflight task), or add `card_message` of the same shape. Budgeting/exhaustion logic is **not** needed (open-ended operator chat).
- **Read:** `GET /api/missions/captures/{id}/thread` (mirrors `/api/missions/clarify-rounds`).
- **Write (Larry → team):** `POST /api/missions/captures/{id}/message` → droplet writes a resume/notify envelope into **Beacon's** inbox; Beacon answers on its next cycle (seconds-to-minutes) and the card updates.
- **UI:** reuse `ClarifyRoundDrawer` (thread render) + `ClarifyReplyBox` (input), mounted on the card; appears on demand (cards stay clean by default — decision: "chat appears").

## 9. Contract F — the doorbell (Telegram demoted, Parked-scoped)

- A card's notification loudness is gated by **risk + blocked-state**: **FYI** (quiet, awareness) for new/aging briefed cards; **blocked-on-you** (loud) when the team posted a question and is waiting.
- Reuse `larry_alerts` + `alert_triage_state` routing; the Telegram message becomes a **ping that deep-links to the card** (no decision-making in Telegram). Full dashboard-wide doorbell rework is out of scope; this phase covers Parked only.

---

## 10. Build plan — 3 steps (single-repo split)

| Step | Repo | Scope | depends_on |
|---|---|---|---|
| **1a — meaning-layer + Narrator** | agent-core | `briefing`/`risk`/`recommended_action` fields; `missions_narrator.py` (authoring + risk-via-trust_policy); surface fields in `/api/missions/derived` `parked[]`; tests | — |
| **1b — conversation + doorbell** | agent-core | capture-scoped thread event type; `GET .../thread` + `POST .../message` (→ Beacon inbox); doorbell loudness via `larry_alerts`/`alert_triage_state`; tests | 1a |
| **2 — meaning-layer Parked card** | dashboard | render briefing + risk badge + one-click delegate (+ Phase 3 secondary actions) + on-demand thread (reuse ClarifyRoundDrawer/ReplyBox/PanelErrorBoundary); minimal toast | 1a |

1b and 2 depend only on 1a (the field contract); the advancer may parallelize after 1a merges, linear fallback if Mirror flags overlap.

---

## 11. Test / proof plan

- **1a:** Narrator authors a deterministic `briefing`+`risk` for a seeded capture+events fixture; risk maps correctly through `trust_policy.evaluate`; absence renders the neutral state.
- **1b:** Larry posts a message on a card → envelope lands in Beacon's inbox → Beacon's reply appears in the thread; doorbell rings FYI vs loud per risk/blocked-state.
- **2:** card renders briefing + risk badge + one-click; clicking delegate queues a proposal (no editor); thread appears on demand.
- **End-to-end (the real gate):** Larry reviews **~5 of his actual parked cards** and confirms he can decide on each **without help**. That is the done-gate — not green unit tests.

## 12. Out of scope (Phase 4b / later / separate)

- **Near-live Beacon front desk** (dashboard-reachable low-latency responder) — the "feels truly live, decide-and-close-in-the-moment" upgrade → **Phase 4b**.
- **Dashboard-wide rollout** of the conversation card (Approvals / Operations / Alerts; Telegram→doorbell everywhere) → its own design pass (parked: `cap-make-team-two-way-chat-a-dashboard-wide-layer-no-adaf`).
- **Auto-handle** (team acts on safe items without asking) → later, lane-by-lane via `trust_policy` rules.
- **The Proposed lane** (the team's front door — work *it* surfaces for Larry to accept/dismiss; accept/dismiss backend exists from #481) → next after the meaning layer proves out.
- Multi-voice (talking to the specific agent vs Beacon), drag-drop, Programs↔Missions unification → out of arc.
