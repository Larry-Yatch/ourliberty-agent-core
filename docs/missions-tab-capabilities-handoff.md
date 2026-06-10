# Missions Tab — Capabilities & Architecture (Cross-Project Handoff)

**Audience:** another project/chat that needs to understand the capabilities built in the OurLiberty agent-OS around the "Missions" work-state surface — so it can reuse the patterns or integrate with them.
**Status:** Phase 0 shipped & proven; Phase 1 building on the autonomous agent team; Phases 2–3 designed.
**Last updated:** 2026-06-10

---

## 1. What this is, in one paragraph

The **Missions tab** is a surface in the OurLiberty agent-OS dashboard (a read-only Next.js app on Vercel) that answers one question: **"What are we working on right now, and where does it stand?"** It is being rebuilt (**Missions v2**) into the *single* work-state surface that shows everything **in flight or parked** across three sources that were previously siloed:

1. **Desktop Claude Code chats** (the operator's Mac — interactive AI sessions),
2. **The autonomous agent team on the droplet** (a fleet of Claude agents that build/review/merge code unattended), and
3. **Captured follow-up ideas** (things to run down later).

The goal: the operator never has to hold work-state in their head or babysit timers to avoid losing it.

---

## 2. The problem it solves (why it exists)

When you run multiple AI chats and an autonomous agent fleet at once, work-state scatters across chat history, GitHub, spec docs, and memory. No single place answered "what's flowing, what's blocked on me, what's done, what did we say we'd come back to." Two distinct pains, two "pillars":

- **P1 — Live work visibility:** *"Where am I across all the work in flight?"* The board was originally blind to the operator's own desktop AI chats — only the droplet agents emitted telemetry. Every concurrent desktop chat was invisible.
- **P2 — Durable capture:** *"Don't lose the idea/hole we said we'd run down later."* The prior workaround was ad-hoc **timers** (set a reminder for later). Timers are a *push at a clock* — you must guess *when*, they fire whether the moment fits or not, and a missed one is gone. The redesign replaces timers with a **durable queue that resurfaces by context**.

---

## 3. Core mental model — the concepts and how they relate

| Concept | What it is | Where it lives | Who manages it |
|---|---|---|---|
| **Mission** | A tracked multi-PR initiative ("build feature X") | `missions.json` (version-controlled registry) | Beacon (the architect agent) |
| **Capture** | A parked follow-up/idea, one-gesture, low-ceremony | `captures.json` (sibling registry) | created by anyone; promoted to a Mission deliberately |
| **Desktop session** | A live interactive AI chat on the operator's machine | emitted as events into the event store | the desktop AI (via a session hook) |
| **Dispatch / Task** | One unit of agent work (one PR), identified by a stable `task_id` | event store + agent inboxes | the agent fleet |
| **Project** | Business-portfolio item (PM layer) | Supabase `projects` table | the operator (separate system) |

The unifying primitive is the **`task_id`**: a stable string that identifies a unit of work everywhere it appears (agent inbox, event store, mission registry, worktree name). Missions *group* task_ids; captures *promote into* missions; desktop sessions and dispatches *emit* events keyed by task_id.

### The "registry + derive" pattern (important)
State is **not** stored redundantly. A small, version-controlled JSON **registry** (`missions.json` / `captures.json`) holds the durable facts (name, brief, task_ids, spec links). Live **phase/status is *derived* on read** by joining the registry against an append-only **event store** + live PR state. This means:
- Cards "move themselves" based on real chain events — no manual status-keeping that can lie.
- Every registry edit is a git commit (free audit trail).
- Migrating to a database later is trivial if dynamic fields are needed.

---

## 4. Architecture & data flow

```
   ┌─────────────────────────┐         ┌──────────────────────────────┐
   │ Desktop Claude Code      │  hook   │  Droplet (agent OS host)     │
   │ (operator's Mac)         │ ──────► │  token-auth ingest endpoint  │
   │  • SessionStart/End hook │  HTTPS  │  POST /api/ingest/...         │
   │  • capture gesture       │  +token │   pins agent + event_type     │
   └─────────────────────────┘         │   → chain_event_emit()        │
                                        └───────────────┬──────────────┘
   ┌─────────────────────────┐                          │ service-role key
   │ Agent fleet (droplet)   │  push                    ▼ (stays server-side)
   │  Forge / Mirror / Beacon│ ───────────────►  ┌──────────────────────┐
   │  Pulse / healers        │   chain_events    │  chain_events (Supabase) │
   └─────────────────────────┘                   │  append-only event log   │
                                                  └───────────┬──────────┘
   ┌─────────────────────────┐   read (join)                  │
   │ Dashboard (Next.js)     │ ◄──────────────────────────────┘
   │  Missions tab           │   + missions.json / captures.json
   │  derives phase per card │   + live GitHub PR state
   └─────────────────────────┘
```

**The event spine is `chain_events`** (a Supabase table): an append-only log of typed events (`session_start`, `review_pass`, `auto_merge`, `desktop_session_start`, `capture`, etc.), each with a deterministic `event_id` (sha1 over `(task_id, event_type, ts)`) so retries/double-emits idempotently collapse. New event types are registered in a `KNOWN_EVENT_TYPES` allowlist (a weekly audit healer flags any unknown type that lands).

**Writes are server-side only.** The Supabase service-role key never leaves the droplet. The desktop holds only a **narrow, dedicated ingest token**; it POSTs events to a droplet endpoint that pins the `agent` identity and constrains the allowed `event_type`, so a leaked desktop token can only write its own card type — nothing else.

---

## 5. How the agents use it

The agent OS is a fleet of Claude agents, each with a role:

- **Beacon (architect/orchestrator):** owns the mission registry, authors specs, and dispatches work. She synthesizes a **build sequence** from operator intent (a spec + a DAG of steps), runs a Mirror DAG-preflight, and fires the kickoff. She's the only agent that edits the registries.
- **Forge (builder):** receives a dispatch (a `task_id` + a prompt + a spec reference) in its inbox, builds the change in an isolated git worktree, opens a PR.
- **Mirror (reviewer):** reviews Forge's PRs (and runs the **DAG-preflight** that verifies a sequence's structure before kickoff — checking for file-overlap between parallel steps). Emits PASS/REVISION verdicts as chain events.
- **Pulse (ops/self-tuning):** periodic operational audits; owns the "self-optimizing config" pattern — any hand-tuned constant earns a periodic check that proposes data-backed adjustments.
- **Healers (reconcilers):** timer-driven scripts that keep state honest (e.g., garbage-collect stale cards, retry wedged sessions, detect git drift). They *report and reconcile* rather than requiring human babysitting.

Agents **emit** chain events as a side effect of their normal work; they don't read the Missions board. The board is purely a **derived, operator-facing view**. The exception is the registries: Beacon writes them, and a GC healer batch-commits high-volume capture writes.

### The build-sequence orchestrator (how a "phase" becomes hands-free agent work)
This is the most reusable capability. A multi-PR initiative is expressed as a **sequence file** (a DAG of steps with `depends_on`, each targeting one repo). The lifecycle:

1. **Author + validate:** Beacon synthesizes the sequence; a validator checks the DAG (no cycles, valid refs, schema, per-step dispatch-text length).
2. **Preflight:** a Mirror review verifies the DAG (catches file-overlap between parallel steps).
3. **Kickoff (human-gated):** the operator triggers it; on Mirror PASS the sequence auto-activates.
4. **Hands-free advance:** a timer-driven **advancer** daemon polls the event store, dispatches each step whose dependencies have merged (parallel where safe, sequential where required), and advances on a **belt-and-suspenders gate** (both the event store *and* GitHub must confirm a merge). It DMs the operator only at key transitions (kickoff / each merge / completion / failure). Any failure **pauses the whole sequence and DMs the operator**, with `pause`/`resume`/`cancel` shortcuts.

The operator's only manual touch is the kickoff; the fleet builds, reviews, and merges the rest unattended.

### Earned-autonomy ladder
Promotion of work toward full autonomy is **data-driven, not granted up front**. A `trust_policy` evaluator (rules in a JSON file) decides per-task `auto_approve` / `force_ask` / `reject`; the default is "everything asks." As an agent proves reliable on a class of low-risk work, rules are widened (a Pulse check can *propose* widening based on observed success rates), and the operator approves the widening. The dial stays in the operator's hand.

---

## 6. Reusable capabilities (what another project can take from this)

These are the load-bearing patterns, independent of the Missions UI:

1. **Desktop AI-session telemetry feed.** Pipe interactive AI-assistant sessions (Claude Code on a laptop) into a shared, durable event store via a lightweight, non-blocking session hook → token-auth ingest endpoint → server-side writer. Keeps the privileged DB key off the client; the client holds a narrow write-only token. *Use when:* you want a fleet/board to "see" what humans+AI are doing locally, not just what runs on servers.

2. **Durable capture / anti-timer queue.** Replace "set a reminder" with "park a durable item that resurfaces by context." One-gesture create, never lost, surfaced when you're near related work or via a periodic digest. *Use when:* you keep losing follow-ups and dislike timers.

3. **Registry + derive.** Keep a tiny version-controlled registry of durable facts; derive live status by joining it against an append-only event log + live external state. Avoids status-fields that drift/lie; gives a free git audit trail. *Use when:* you need a status board that's always truthful with minimal maintenance.

4. **Build-sequence orchestrator hand-off.** Turn "build X across N PRs" into a validated DAG that an agent fleet executes hands-free — with preflight verification, dependency gating, parallel-where-safe, human-gated kickoff, and pause-on-failure. *Use when:* you want autonomous multi-step builds you can trust.

5. **Earned-autonomy ladder.** Data-as-config trust policy that graduates work from "always ask" to "auto" as reliability is proven, with the human approving each widening. *Use when:* you want to grow autonomy safely over time.

6. **GC/healer pattern.** Make *broad capture safe by cleanup, not by a strict entry gate*: let everything in, and run idempotent, timer-driven reconcilers that retire/age/clean stale items and report what they did. *Use when:* breadth would otherwise drown the signal.

7. **Human-gated safety boundaries.** Designed review gates (a preflight that can't be skipped) and spend gates (autonomous agent runs that cost money require explicit go). These are enforced points where a human must trigger; the system is built so shortcuts around them are caught.

8. **Idempotent event emission.** Deterministic `event_id` over `(task_id, event_type, ts)` + an allowlist of known event types + an audit healer for unknown types. *Use when:* multiple producers may emit the same semantic event and you need exactly-once-ish semantics cheaply.

---

## 7. Concrete contracts (for integration / reference)

**Desktop session event** (written to `chain_events` via the ingest endpoint; `agent` is pinned to `desktop-claude`):
```jsonc
event_type: "desktop_session_start" | "desktop_session_active" | "desktop_session_done"
task_id:    "<thread/mission id, or desktop-<session_short>>"
payload:    { repo, branch, cwd, title, blocked_on_larry, host, source, last_activity_ts }
```

**Ingest endpoint** `POST /api/ingest/<kind>`: header `X-Ingest-Token` (dedicated, not the dashboard read token); server pins `agent` + constrains `event_type`; payload size-capped; best-effort (the client treats failure as "no card", never blocks the session).

**Capture record** (`captures.json`, sibling to the mission registry):
```jsonc
{ id, title, note, state: "parked"|"promoted"|"dropped",
  origin: { source, session_id, repo, branch, captured_at },
  last_touched, promoted_to }
```
Capture writes happen on the server (atomic), and a GC healer **batch-commits** the registry to git (no PR per capture — durable immediately, version-controlled within one healer tick).

**Build-sequence file** (a DAG the orchestrator executes):
```jsonc
{ seq_id, label, spec_doc, status: "pending"|"active"|"paused"|"complete"|"failed",
  steps: [ { step_id, label, depends_on: [...], dispatch_text (≤500 chars),
             target_repo, task_type, status } ],
  audit_log: [...] }
```

---

## 8. Phasing / current status

- **Phase 0 — desktop feed (SHIPPED & PROVEN):** a live desktop chat now appears as a card on the board. Validated end-to-end (event written, card rendered).
- **Phase 1 — durable capture + GC (BUILDING on the agent fleet now):** `captures.json` + capture ingest + desktop capture gesture + the GC healer + a read-only "Parked" lane. Authored, validated, kicked off through the orchestrator; the fleet is building it hands-free as a 3-step sequence.
- **Phase 2 — resurfacing (DESIGNED):** contextual resurfacing + a dashboard daily/on-demand "parked & aging" digest (the timer replacement, consumed on the dashboard, not chat).
- **Phase 3 — write-back + autonomy (DESIGNED):** promote/drop/snooze actions from the UI + the trust-policy autonomy ladder for capture promotion.

Explicit non-goals (kept out): merging the business-portfolio (Projects) layer with the technical (Missions) layer; drag-drop; turning the board into a full PM tool.

---

## 9. One-line summary for the other project

> We built a **derived, always-truthful work-state board** fed by an **append-only event spine**, with **desktop AI sessions and an autonomous agent fleet both emitting into it**, a **durable capture queue that replaces timers**, and a **build-sequence orchestrator** that turns operator intent into hands-free multi-PR agent work behind human-gated kickoff and spend boundaries. The reusable parts are sections 6 and 7.
