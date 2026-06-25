# Spec: "In Motion" — the surface for work moving *without* Larry

**Status:** Draft (spec-first; the contracts below are NAMED, not implemented — a 2-step build is the fast-follow).
**Author:** Beacon (drafted via Forge dispatch `spec-in-motion-surface-001`), 2026-06-25
**Approver:** Larry (pending)
**Complements:** `agents/beacon/specs/operator-needs-you-feed.md` (the `/where-we-are` "Waiting on You" panel, PR #676). That surface answers *"what does the chain need from ME?"* This spec answers the inverse — *"what is the chain doing right now that I do NOT have to touch?"* The two are deliberately disjoint; see §1.

---

## 1. Purpose

The chain has many surfaces, but every one of them is organized around either **a thing Larry must act on** (`/where-we-are`, `/approvals`) or **a static catalog** (`/missions`, `/operations/build-sequences`, `/system`). None of them answers the question Larry actually asks when he opens the dashboard and *nothing* is waiting on him:

> "Okay — what's happening right now, on its own?"

Three classes of in-flight work are real, autonomous, and currently invisible-as-motion:

- **Delegated builds in flight.** A spec dispatched to Forge moves through preflight → build → Mirror review → auto-merge with no Larry action. Today that progression is scattered across `~/agents/state/in-flight/`, the build-sequence ladder, and the Telegram stream. There is no single card that says *"the X build is at Mirror review, opened 12m ago."*
- **Self-healing in flight.** When a PR needs an auto-rebase (the #685 / #687 class) or a cold-start revision, a healer opens a durable obligation and re-dispatches — autonomously. The repair is real work happening *right now*, but the only signal is a healer log line. Larry cannot watch self-repair while it happens; he only learns it happened (or didn't) after the fact.
- **The already-covered surfaces.** `/where-we-are`, `/missions`, the sequence ladder, `/approvals`, `/system` — each shows one slice. There is no hub that says "here is everything in motion, and here are the deep links."

This spec adds **one new top-level tab, `/in-motion`,** that surfaces these. It is **visibility-only** — it reads existing substrate and renders; it dispatches nothing and mutates nothing. It is the calm, "all moving smoothly" companion to the "needs-you" board: when `/where-we-are` is empty, `/in-motion` shows Larry the system is still working.

The key framing that keeps this from duplicating #676: **`/where-we-are` itemizes blocked-on-Larry work; `/in-motion` itemizes moving-without-Larry work.** An item is on exactly one of the two surfaces, never both — the moment a build blocks on a Larry decision it leaves `/in-motion` and appears on `/where-we-are`, and vice-versa. They partition the same underlying chain state by *who the next actor is.*

---

## 2. Locked decisions

| # | Decision | Rationale |
|---|---|---|
| a | A **new top-level tab `/in-motion`**, NOT an extension of `/where-we-are`. | The two surfaces partition by next-actor (§1). Folding "moving without you" into the "needs you" panel would re-merge what we're deliberately splitting and re-create the "is this blocked or just busy?" ambiguity. Distinct from `operator-needs-you-feed` decision (a), which correctly extended `/where-we-are` for *needs-you* classes — this is the other half. |
| b | **Visibility-only.** No buttons, no dispatch, no mutation. | This is a watch surface. Every actionable item already has a home with controls (`/where-we-are` has the steering buttons; `/operations/build-sequences` has the ladder). `/in-motion` links out to those; it does not duplicate their controls. Keeps the read/write boundary clean and the new tab's blast radius zero. |
| c | The **delegated card** reads existing in-flight substrate (`~/agents/state/in-flight/`, `~/agents/blackboard/build-sequences/`, chain_events) — it invents NO new producer. | The state already exists; what's missing is the *narrative join*. We reuse `system_state_log`'s readers (`load_in_flight`, `load_active_sequences`, `load_active_missions`) and the narrator (`missions_narrator.generate_briefing_voice`) rather than writing a parallel tracker. |
| d | The card's **milestone narrative auto-clears on ship.** | A delegated card describes a build's current milestone in plain English ("at Mirror review"); when the PR merges, the card retracts within one tick. Append-only would leave stale "in flight" cards for already-merged work — the same stale-row failure mode `operator-needs-you-feed` decision (d) guards against. Self-clearing is keyed on the merge/terminal signal already present in chain_events + the in-flight file's removal. |
| e | The **remediation-in-flight lane reads the healer ledgers directly** — `~/agents/state/rebase-obligation-ledger.json` and `~/agents/state/no-session-revision-ledger.json` — via their existing `list_open()` accessors. | These ledgers are the canonical, durable, self-clearing record of in-flight self-repair (each row carries `status: open|resolved` and a `resolution`). Reading them is how self-healing becomes watchable *while it happens* rather than only auditable after. No new state; the lane is a projection of `list_open()`. |
| f | The lane is **self-clearing by construction** — a remediation row is exactly an open ledger obligation; when the healer resolves the obligation (`status → resolved`), the row disappears on the next read. | The ledgers already own the open→resolved lifecycle (`resolve_obligation`); the lane piggybacks on it. No separate retraction logic, no timer. This is the §5.2 producer's existing guarantee, reused. |
| g | The **roll-up rail links the already-covered surfaces**; it does NOT re-render their contents. | `/in-motion` is a hub. The rail is a compact set of deep links (`/where-we-are`, `/missions`, `/operations/build-sequences`, `/approvals`, `/system`) with a live count badge each. Re-rendering their panels here would duplicate maintenance and split the source of truth. Links, not copies. |
| h | **Slice 2 (stuck-PR-reason + dispatch provenance) is a named fast-follow, not in this slice.** | Slice 1 establishes the surface + the two read-only feeds + the rail. Slice 2 enriches each card with *why* a PR is stuck (failing CI / merge conflict / awaiting review) and *where it came from* (which spec, which mission, who dispatched). That needs additional joins (CI status, dispatch provenance) worth their own spec pass. See §6. |
| i | One new **read-only API endpoint** (`GET /api/system/in-motion`) assembles the three pieces server-side; the dashboard renders the assembled shape. | Mirrors the existing `GET /api/system/state-log` / `/api/system/missions` pattern in `dashboard_api.py`. Server-side assembly keeps the client a pure renderer and the join logic testable in Python. |

---

## 3. Why now

- `/where-we-are` (#676) shipped the "needs you" half of the operator's mental model. The "moving without you" half is the obvious, symmetric gap — and it's the half that makes the system *legible* on a calm day.
- The substrate is already there: in-flight files, build-sequence files, chain_events, and — critically — the **healer ledgers** (`rebase_obligation_ledger`, `no_session_ledger`) that already record self-repair as durable, self-clearing obligations. The #685/#687 auto-rebases proved the chain self-heals; this surface makes that self-healing *visible while it runs* instead of buried in a healer log.
- It directly serves the alert-toil principle from the opposite direction: by giving Larry a trustworthy "everything's in motion" view, it reduces the urge to check Telegram just to confirm the chain is alive.

---

## 4. Acceptance

- A new `/in-motion` tab exists as a top-level route, reachable from the dashboard nav, distinct from `/where-we-are`.
- A delegated build that is mid-flight (in `~/agents/state/in-flight/` and/or an active build-sequence step) appears as **one card** with a plain-English milestone line ("at Mirror review", "building", "preflight") and an age.
- When that build's PR merges (or the build otherwise reaches a terminal state), its card disappears within one tick — no stale "in flight" cards.
- An open rebase obligation (`rebase-obligation-ledger.json`, `status: open`) appears in the **remediation-in-flight lane** as a row naming the PR, the round, and "self-healing: rebasing". A cold-start revision obligation (`no-session-revision-ledger.json`, `status: open`) appears likewise.
- When a healer resolves an obligation (`status → resolved`), its remediation row disappears on the next read.
- The **roll-up rail** shows a deep link + a live count badge for each covered surface (`/where-we-are`, `/missions`, `/operations/build-sequences`, `/approvals`, `/system`); clicking a rail item navigates to that surface.
- The all-empty state (nothing in flight, no remediation, all surfaces quiet) renders a calm "nothing in motion — all caught up" message, NOT a broken/empty grid.
- Nothing on `/in-motion` mutates state: there are no action buttons; every interaction is a navigation.
- An item that is blocked on a Larry decision appears on `/where-we-are`, NOT on `/in-motion` (the partition invariant from §1 holds).

**Enforcement:** the partition invariant (last bullet) and the self-clearing requirements (cards §4.3, lanes §4.5) are asserted by the §9 test matrix — a card or row that fails to retract when its underlying state goes terminal/resolved fails the suite, so a stale "in motion" item cannot ship. The visibility-only invariant (decision b) is enforced by the absence of any write path in the §5.4 endpoint (read-only handler, no POST) and a Mirror review check that the `/in-motion` client renders no action controls.

---

## 5. Per-item spec (contracts NAMED — implementation is the fast-follow build)

> This section *names* the contracts so the 2-step build (§8) can implement them. Per the dispatch, **implementing the contracts is out of scope for this doc.**

### 5.1 The delegated card — milestone narrative + auto-clear

- **Reader (no new producer — decision c):** a new assembler reads `system_state_log.load_in_flight()` (task stems in `~/agents/state/in-flight/`), `load_active_sequences()` (active build-sequence steps from `~/agents/blackboard/build-sequences/`), and `load_active_missions()`, then joins them to recent `chain_events` to determine each build's current milestone (preflight / building / at-review / merging).
- **Narrative:** the human-readable milestone line reuses the narrator already in the tree (`missions_narrator.generate_briefing_voice`, the same voice `system_state_log` uses for its prose) — one short sentence per card, not a paragraph.
- **Card shape:** `{task_id, title, milestone, milestone_detail, age, deep_link}` where `deep_link` points at the PR (if open) or the relevant `/operations/build-sequences/[seq_id]` ladder page.
- **Auto-clear (decision d):** the card derives entirely from live substrate, so it clears automatically — when the in-flight file is removed and/or a terminal `chain_event` (merged / closed / failed) for that `task_id` is observed, the next assembly omits the card. No retraction bookkeeping of its own.
- **Enforcement:** the assembler is a pure read + join; the §9 test asserts mid-flight task → card with the correct milestone, terminal task → no card, and that the assembler writes nothing.

### 5.2 The remediation-in-flight lane — reading the healer ledgers

- **Source (decision e):** `rebase_obligation_ledger.list_open()` → `~/agents/state/rebase-obligation-ledger.json` and `no_session_ledger.list_open()` → `~/agents/state/no-session-revision-ledger.json`. Each open row carries `{task_id, pr_url, branch, round, status: open, opened_at, last_dispatch_at}`.
- **Row shape:** `{kind: 'rebase'|'cold-start-revision', pr_url, round, why, age, deep_link}` where `why` is a fixed plain-English phrase per kind ("Self-healing: rebasing PR #N (round R)" / "Self-healing: cold-start revision on PR #N"), and `deep_link` is the PR URL.
- **Self-clearing (decision f):** a row IS an open obligation. When the healer calls `resolve_obligation(...)` (`status → resolved`, `resolution` set), `list_open()` stops returning it and the row vanishes on the next read. The lane owns no lifecycle of its own.
- **Why this matters:** this is the slice's headline capability — it makes autonomous self-repair (the #685/#687-class rebases) *watchable while it runs*. Today that motion is healer-log-only; here it's a live lane.
- **Enforcement:** the lane is a projection of `list_open()`; the §9 test asserts an open obligation → exactly one row of the right kind, a resolved obligation → no row, and that the projection performs no write.

### 5.3 The roll-up rail — linking the covered surfaces

- **Content (decision g):** a static list of the covered surfaces, each with a route and a live count sourced from that surface's existing reader: `/where-we-are` (waiting-on-Larry total via `load_for_larry_escalations` + waiting builders), `/missions` (active mission count), `/operations/build-sequences` (active sequence count via `load_active_sequences`), `/approvals` (pending-approval count via `load_pending_approvals`), `/system` (active-session count).
- **Rail item shape:** `{label, route, count}`. Rendering is a link with a badge; no panel contents are duplicated.
- **Enforcement:** counts are read-through to existing readers (no new aggregation); the §9 test asserts each rail count matches its source reader's length for a fixed fixture.

### 5.4 The assembly endpoint (the one new backend surface)

- **New:** `GET /api/system/in-motion` in `scripts/dashboard_api.py` — token-gated, read-only (mirrors the existing `get_system_state_log` / `get_system_missions` handlers). Returns `{delegated_cards: [...], remediation: [...], rollup: [...], generated_at}`.
- **No POST counterpart** (decision b). The handler calls the §5.1 assembler, the §5.2 lane projection, and the §5.3 rail builder, and serializes the result.
- **Enforcement:** the read-only invariant is structural — there is no write path in the handler; the §9 test asserts the endpoint returns the assembled shape for a populated fixture and the calm-empty shape for an empty one, and that no fixture file is modified by the call.

---

## 6. Slice 2 — named fast-follow (NOT in this slice)

A second slice enriches the surface; it is named here so the build order is clear, and specced in its own pass:

- **Stuck-PR-reason.** Annotate a delegated card (or a remediation row) whose PR has stopped moving with *why*: failing CI check, merge conflict, awaiting review past threshold, branch-protection block. Source: `gh pr view` / CI status joins. This turns "at review, 40m" into "at review, 40m — CI failing on `test_x`."
- **Dispatch provenance.** Annotate each card with *where it came from*: the spec section that authored it, the mission it belongs to (`missions.json`), and who dispatched it. Source: the dispatch envelope + mission registry join.

Both need joins (CI status, provenance) beyond Slice 1's pure in-flight + ledger reads, so they are deliberately deferred. Slice 1 must not block on them.

---

## 7. Out of scope

- **Implementing the contracts.** This is a spec-first doc; the §5 contracts are named, and the 2-step build (§8) implements them as a fast-follow.
- **Any write/steering control on `/in-motion`** (decision b). Steering lives on the surfaces the rail links to.
- **Slice 2** (§6) — stuck-PR-reason + dispatch provenance.
- **Re-rendering covered-surface contents** in the rail (decision g) — links + counts only.
- **A new healer or a new ledger.** The lane reads existing ledgers; it adds no remediation mechanism.
- **Real-time push.** The dashboard's existing poll cadence is sufficient; no websockets.

---

## 8. Build sequence (proposed DAG — the 2-step fast-follow)

- **Step 1 — agent-core, the assembly endpoint:** the §5.1 delegated-card assembler + the §5.2 remediation-lane projection + the §5.3 rail builder + the §5.4 `GET /api/system/in-motion` handler, with the §9 Python test matrix. No `depends_on`. Lands the read-only contract on the backend.
- **Step 2 — dashboard render:** the `/in-motion` top-level route that fetches the endpoint and renders the three pieces (delegated cards, remediation lane, roll-up rail) + the calm-empty state, with vitest coverage. `depends_on: [step-1]` (needs the endpoint shape).

Two steps, strictly ordered (render needs the contract). Slice 2 (§6) is a later, separate sequence.

---

## 9. Test matrix

- **Delegated card (§5.1):** mid-flight task → card with correct milestone; terminal task (merged/closed/failed event) → no card; assembler writes nothing.
- **Remediation lane (§5.2):** open rebase obligation → one `rebase` row; open cold-start obligation → one `cold-start-revision` row; resolved obligation → no row; empty ledgers → empty lane; projection writes nothing.
- **Roll-up rail (§5.3):** each rail count equals its source reader's length for a fixed fixture; a quiet fixture yields zero badges, not missing items.
- **Endpoint (§5.4):** populated fixture → full assembled shape; empty fixture → calm-empty shape; the call modifies no fixture file (read-only invariant).
- **Dashboard (vitest):** each section renders its items; the all-empty state renders the calm "nothing in motion" copy; no action controls render anywhere on the tab (visibility-only invariant).

---

## 10. Cost & migration

- **Cost:** 1 agent-core PR (endpoint + tests) + 1 dashboard PR (route + render). Modest — every reader (in-flight, sequences, missions, the two healer ledgers, pending-approvals) already exists; the work is join + render, not new mechanism.
- **Migration:** none. Additive and read-only — one new GET endpoint, one new route. No schema change, no new credential, no new producer or ledger.

---

## 11. Enforcement (doctrine-of-doctrine)

Every rule-bearing paragraph above pairs with a mechanism: the visibility-only invariant (decision b) is enforced by the *absence* of any write path in the §5.4 handler plus a Mirror check that the client renders no controls; the partition invariant (§1, §4) and the self-clearing requirements (decisions d, f) are enforced by the §9 retraction assertions — a card or row that doesn't disappear when its underlying state goes terminal/resolved fails the suite, so a stale "in motion" item cannot ship; and the "no new producer/ledger" constraint (decisions c, e, §7) is enforced by the test assertions that every assembler/projection writes nothing. The cross-cutting guarantee: because every item on `/in-motion` is a pure projection of substrate owned elsewhere, the surface cannot drift from reality — it has no state of its own to go stale.
