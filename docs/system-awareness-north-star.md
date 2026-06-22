# North Star: System Self-Awareness (the "standing brain")

**Status:** Draft v1 for review — 2026-06-19. *Skeleton — tear apart before we spec the phases.*
**Owner / approver:** Larry
**Author:** Claude Code (desktop brainstorm session, 2026-06-19)
**Born from:** the 2026-06-19 brainstorm — *"we're still heavily dependent on Claude in a laptop session for insight, status, and direction; the team on the droplet can execute but can't self-direct, and that isn't sustainable as we build more."*
**Relationship to other docs:** sits **above** [projects-tab-v3-north-star.md](projects-tab-v3-north-star.md) (the board). The board is the **projection + operator controls**; this mission is the **awareness substrate + the directing intelligence**. We **reuse and read** the board — we do not rebuild it. Parent "why" = [shared/NORTH-STAR.md](../shared/NORTH-STAR.md). The manual Plan we automate = [roadmap.md](roadmap.md).
**Reuse mandate:** build durable + shelf-able. Generalize machinery that already exists (the missions Narrator, two-way sync, `chain_events`) — do not reinvent.

> Living doc — keep current; tick the tracker (§9) as work lands. Start here when we pick this up.

> **Update 2026-06-21 — autonomy went live, and its mirror surfaced.** The earned-autonomy ladder (§6 item 4) reached **rung 1 in production**: Beacon→Forge `ourliberty-agent-core` code now auto-fires without Larry's pre-build click (trust-policy gate live; config/deploy/migrations/secrets still ask; Mirror still reviews everything). That makes Slice 2 ("what needs Larry") urgent **and** adds its mirror — **"Automated Work"**: what the team did *without* a click, so Larry can audit it and dial autonomy. Both are now powered by one new durable record, the **`autonomy_decision`** chain event (capture DONE — agent-core #623). See §5–§6, §9. Context: memory `two-team-feasibility-2026-06`, `autonomy-visibility-project`.

> **Update 2026-06-22 — Slice 2 + the keystone shipped end-to-end.** The full autonomy-visibility surface is **live**: (1) the **board-delegate keystone** (agent-core #626) — a `source='dashboard'` board delegation now reaches Forge through the trust gate (it dead-ended before); proven live by re-enabling the board-drain and watching a card go board→Beacon→Forge. (2) **Automated Work feed** (agent-core #632 + dashboard #78) — a plain-English feed on `/where-we-are` of what the team auto-fired, with the matched trust rule. (3) **"Waiting on you," itemized** (dashboard #79). (4) the **doorbell** (agent-core #633) — a cooldown-gated `🔔 N items need your call — check the board` DM when approvals/escalations accrue. Larry can now see both halves — what autonomy did, and what needs him — and the doorbell finds him without his looking. §6 item 2 + §9 ticked.

---

## 0. Desired end state  *(the destination — everything builds from here)*

**The system knows where it is, why it's working on what it's working on, and proposes what's next — so Larry directs by gating decisions, not by couriering insight, status, and direction between a laptop chat and the droplet team.**

- Larry can learn *where we are* in seconds, in his language, without summoning Claude — by glancing at the dashboard or asking Beacon, and getting the **same current answer** either way.
- The team holds a **standing, always-current picture of itself** (not a cold start every interaction) and **proposes direction** off it — escalating only the policy/outcome calls that are genuinely Larry's.
- "Why are we doing this right now" is **written down and kept live**, so a decision made on the laptop and a decision made by the droplet brain draw from the same context.

This is the operational half of the factory's **situational awareness** (Mission B / the "scene graph" in the graph SSOT) — so building it also advances the factory thesis and unblocks RSDPM.

## 1. The problem (why this mission exists)

The system is a mature **execution** engine — Forge builds, Mirror reviews, Medic/healers heal, Beacon is the voice. What it lacks is a **brain that holds the whole picture and decides what matters.** That brain is Claude-in-a-laptop-session, and **Larry is the cable** between it and the team.

The three things Larry keeps coming back to Claude for map to three missing organs:

| Larry comes back for… | Missing organ |
|---|---|
| **Insights** ("what does this mean / root cause") | a **diagnostician** that reasons across signals |
| **Status** ("where are we as a whole") | a **narrator of the whole** |
| **Direction** ("what's next, in what order, is this right") | a **planner** |

Beacon can research on demand but is **stateless** — no running memory — so she's *reactive-smart but always ignorant of what's been going on*. The one missing leg vs. how Claude stays aware: Claude has the plan docs (the team has those) and the live look (Beacon has that) **plus a continuously-updated memory** — which the team does not.

`roadmap.md` is the tell: it's a real Plan doc *"updated by Beacon (or with Beacon's awareness),"* and its entries have gone stale (May). Manually-maintained awareness rots.

## 2. The model — why / what / how + the hybrid loop

We automate **the same five-step loop Claude runs in a session** — read plan + memory → take a live look → synthesize → update → direct — by assigning each step to something on the droplet, on a timer.

**The teal layer = three shared documents (the why/what/how triad):**

| Document | Answers | Changes | Maintained by |
|---|---|---|---|
| **Desired end state** | *Why* we're doing all this now | Rarely — shifts when big projects land | Larry, via structured check-ins + team proposals |
| **The plan** | *What* we're working on (now → future) | Medium pace | Orchestrator drafts; Larry gates the big calls |
| **State log** | *How* it is right now | Continuously | Narrator |

The hierarchy is load-bearing: the **why** justifies the **what**; the **what** is measured against the **how**. (Note: the desired-end-state doc is distinct from the existing "desired-state reconciler" healer — different concept, don't conflate.)

**Three actors:**
- **Narrator** (droplet, cheap, continuous): scrapes existing signals → writes the State Log. Generalizes the missions Narrator (Phase 4.1) from per-project cards to whole-system. Fully automated, read-only on the world → safe.
- **Orchestrator** (Opus, periodic + on-demand): reads the teal docs → diagnoses → updates the Plan → escalates policy to Larry → dispatches well-formed tasks to the team via Beacon. "Claude, scheduled." Semi-autonomous: proposes, Larry gates the big calls.
- **Beacon**: now reads the State Log before answering → never ignorant again. The mouth of the brain, not a stateless researcher.

**Laptop vs. droplet** is the only real difference: today the loop runs only when Larry summons Claude (awareness goes stale between sessions); on the droplet the Narrator keeps it fresh continuously and the Orchestrator runs the loop on a schedule → awareness becomes **ambient, not summoned.**

## 3. Surfaces — how Larry consumes it

The three teal docs are for the **machines** (Narrator writes; Orchestrator + Beacon read). Larry never reads them raw. He consumes the same substrate two ways:
- **Beacon** — ask, get a current answer (conversational).
- **Dashboard, three depths** — (1) an always-on briefing he glances at, (2) main points he skims, (3) one more click to the full story. *(Larry prefers a dashboard he skims over a document he reads.)*

Because Beacon and the dashboard read the **same substrate**, they cannot disagree — which is what stops Larry having to triangulate. So the build is two-sided: **substrate** (the teal docs) + **projection** (dashboard surfaces + Beacon).

## 4. Boundary with the board (Projects-tab-v3)

The board already targets *"Larry opens one tab and sees every piece of work in his world, each a plain-English card he can act on,"* with a funnel, a pipeline of Projects (each with its own North Star + phase cards), a Narrator, and two-way sync — and it's being dogfooded now.

- **Theirs (the board):** the surface, the per-project cards/North-Stars/phase-flow, the one-click operator controls, the funnel of suggestions.
- **Ours (the brain):** the whole-system **State Log** (health + pipeline + everything, beyond project cards), the **system-level why/what/how** above the per-project docs, **Beacon reading the memory**, and the **Orchestrator** that diagnoses + proposes direction (the board is operator-driven; the brain self-directs).
- **The seam:** we **feed and read** the board; we don't edit its files/schemas. If the board is owned by a separate work-stream, changes route **through Larry**, not parallel edits — same discipline as the graph↔missions seam.

## 5. What already exists that we build on

- `shared/NORTH-STAR.md` — the permanent parent "why" (the R&D-sandbox mission + handoff bar).
- `roadmap.md` — the manual Plan (the thing we make self-maintaining).
- The **missions Narrator (4.1)** + **two-way sync (Phase S)** — the seed machinery for our Narrator.
- `chain_events` spine + `missions.json` / `captures.json` + droplet endpoints (`/api/system/missions`, `/api/missions/captures`) — the work-state telemetry the State Log reads.
- Beacon (the voice), the dispatch path (Beacon-mediated), Forge/Mirror (the hands).
- **`autonomy_decision` chain event (agent-core #623, 2026-06-21)** — one durable record per trust-policy decision (`auto_approve` / `force_ask` / `reject` + the matched rule + task/repo). The audit substrate for **both** "Automated Work" (it auto-fired) and an enriched "what needs Larry" (it asks). The pre-existing `approval_request` event records only that an ask was *made*, never the resolution or the *why*.
- **The trust-policy gate** (`config/trust-policy.json`, live 2026-06-21) — the autonomy dial itself; surfacing its matched rule per action is the "dial-in" instrument Larry asked for.

## 6. Build sequence (prove-first)

Prove the **Narrator** on one narrow surface, run it days, widen, then add the **Orchestrator**. Don't build the brain until the memory it reads is trusted.

1. **Slice 1 — "work in flight," narrated end-to-end** *(A + D as one coherent surface).* Generalize the existing Narrator from per-project cards to whole-system work-in-flight: missions (the container) + build/PR pipeline (the execution detail), persisted as the **State Log** substrate and fed to the existing board. Narrator-only, read-only over existing telemetry. **Success bar:** for a week, Larry opens the dashboard (or asks Beacon) and knows where every active piece of work stands — mission *and* its PRs — without pinging Claude once.
2. **Slice 2 — "what needs Larry" + its mirror "Automated Work"** *(co-built — two views of one `autonomy_decision` record).* — ✅ **DELIVERED 2026-06-22.**
   - **What needs Larry:** a synthesized decision queue across sources (PRs at the merge-gate, missions awaiting promote/drop, **`force_ask` autonomy decisions**, policy escalations, pending chips) — substrate `…-slice-2a` ✅ on agent-core main; render `…-slice-2b` ✅ **dashboard #79** (itemized "Waiting on you" panel: source badges, plain-English why, age, action links). Plus a **doorbell** (Larry's ask 2026-06-21): Beacon does the work it can and **DMs Larry a short "N items need your call — check the board"** (a doorbell, never a wall) when `force_ask` approvals + escalations accrue — ✅ **agent-core #633** (`scripts/doorbell_notifier.py`, installed + live).
   - **Automated Work** (the mirror): a plain-language, cross-system feed of what the team did *on its own* (`autonomy_decision` that auto-fired), each row drilling to the matched trust rule. The **dial-in instrument** for autonomy. ✅ **agent-core #632 (endpoint) + dashboard #78 (panel on `/where-we-are`)**; spec `system-awareness-automated-work-feed.md`. (`/where-we-are` already existed from Slice 1b.)
   - **Keystone enabler:** the **board-delegate dispatch route** — completed `agents/beacon/specs/missions-v2-delegate-fix.md` (which built the dashboard endpoint but missed that a `source='dashboard'` Beacon proposal never reached Forge — `outbox_notifier._BEACON_TRUSTED_DISPATCH_SOURCES` trusted only `larry`/`orchestrator`). ✅ **agent-core #626** (reused `_route_beacon_pulse_auto_dispatch_approval` with `policy_source='beacon'`); proven live (the board-drain now produces real builds — it previously only scoped proposals that never built).
3. **HOLD — synthesized system health (C).** Highest eventual value, **highest risk** (false reassurance destroys trust). Earn it only after the pattern is proven.
4. **The Orchestrator.** The periodic reasoning brain on top of the trusted substrate: diagnose → propose Plan → escalate policy → dispatch. Climb the earned-autonomy ladder (advisory → proposes-with-gate → bounded auto).
5. **The why/what/how docs + their maintenance loop.** Stand up the Desired-End-State doc (structured Larry check-ins + team-proposed drift corrections) and make the Plan self-maintaining (Orchestrator-drafted, Larry-gated).

## 7. Constraints / invariants

- **Trust is the whole game.** A noisy/wrong narrative sends Larry back to Claude and we've built nothing. So *who writes the narrative matters* — likely the cheap tick collects raw signal and an Opus pass writes the prose (decide by test, §8).
- **One memory, one owner.** If both Claude-in-session and the Orchestrator write the same teal docs, they fork (the machine-owned-file dual-committer data-loss class). Each doc has exactly one committer from day one.
- **Plain-language-first** on every surface (Larry's standing rule). Lead with plain English + blast-radius; hold technical detail.
- **Reduce toil, don't add it.** The brain must subtract from Larry's load, not become a new noisy subsystem (the alert-toil doctrine).
- **Read the board, don't rebuild it** (§4).

## 8. Open questions (resolve as we refine)

- **Who writes the narrative** — cheap droplet model vs. Opus pass vs. hybrid? Decide by testing narrative quality on Slice 1, not by guessing.
- **Orchestrator cadence + triggers** — periodic only, or also event-triggered (e.g., a stall, a merge, a new mission)?
- **Where the teal docs physically live**, their single committer, and their agent-read path.
- **Orchestrator dispatch guardrails** — what it may dispatch unattended vs. what needs Larry; how it rides the existing Beacon-mediated path.
- **Desired-end-state cadence** — how often the structured Larry check-in happens, and how the team proposes drift corrections.
- **Convergence with the graph scene-graph (T7/T8)** — is this mission's State Log the same artifact the graph's scene-graph consumer wants, or a sibling? Coordinate so we build it once.

## 9. Tracker

| # | Item | Status |
|---|------|--------|
| 0 | This North Star drafted + boundary-with-board decided (own doc, above the board) | DONE 2026-06-19 |
| 1 | Slice 1 — work-in-flight State Log (generalize Narrator; A+D) → feed board | ✅ DONE 2026-06-20 (PR #602, live + closed-out) |
| 1.5 | **Autonomy gate LIVE** — earned-autonomy ladder rung 1 (Beacon→Forge agent-core auto-fires) + readiness trip-wire + board-drain | ✅ LIVE 2026-06-21 (agent-core #614/#615; activated on droplet, revert-ready) |
| 1.6 | `autonomy_decision` capture — the audit substrate for Slice 2 + Automated Work | ✅ DONE 2026-06-21 (agent-core #623) |
| 2 | Slice 2 — "what needs Larry" decision queue (+ doorbell DM) | ✅ DONE 2026-06-22 — 2a substrate already on agent-core main (`_build_waiting_on_larry` itemizes parked+approvals+escalations); render **2b dashboard #79**; **doorbell DM agent-core #633** (installed + live, ~30-min timer over `waiting_on_larry`, `intent='doorbell'` 🔔, prompt-on-increase + 4h reminder + reset-at-0) |
| 2′ | **Automated Work feed** — the mirror of Slice 2; the autonomy dial-in surface | ✅ DONE 2026-06-22 — endpoint **agent-core #632** (`/api/system/automated-work`, a chain_events reader; NOT through `system_state_log.py` — that's local-files-only) + render **dashboard #78** (panel on `/where-we-are`); spec `system-awareness-automated-work-feed.md`; live-verified showing the real auto-dispatches |
| 2″ | Board-delegate dispatch route (keystone: completes delegate-fix, un-blocks the board-drain) + re-enable drain | ✅ DONE + PROVEN LIVE 2026-06-21/22 — keystone **agent-core #626** (`source='dashboard'` → trust-gated route, `policy_source='beacon'`); drain re-enabled + a card watched board→Beacon→Forge end-to-end (the dispatch now reaches Forge; was dead-ending before) |
| 3 | Synthesized system health | HOLD (earn trust first) |
| 4 | Orchestrator (the directing brain) | TODO (after substrate is trusted) |
| 5 | Desired-end-state doc + Plan self-maintenance loop | DES v1 DRAFTED 2026-06-19 (`docs/desired-end-state.md`, why-now=self-direction-foundation, horizon=weeks); Plan self-maintenance TODO |

## 10. Related

Docs: [projects-tab-v3-north-star.md](projects-tab-v3-north-star.md), [shared/NORTH-STAR.md](../shared/NORTH-STAR.md), [roadmap.md](roadmap.md), [missions-tab-capabilities-handoff.md](missions-tab-capabilities-handoff.md). Graph SSOT (ourliberty-graph) Mission B / scene graph.
Memory: `system-self-awareness-layer`, `ourliberty-factory-vision-graph-layers`, `missions-redesign-project`, `larry-working-style-decisions`, `alert-toil-principle`, `plain-language-first-comms`, `machine-owned-file-single-committer`.
