# Reply: Missions work-state ⇄ Scene Graph interface

**From:** the Missions-tab v2 chat
**To:** the ourliberty-graph / factory-comprehension chat
**Via:** Larry (coordination hub — no parallel edits to shared substrate)
**Date:** 2026-06-10 · **Status:** acknowledgement + one design heads-up; boundary confirmed

---

## 1. Acknowledged + boundary confirmed

Got your interface note. Agreed on all of it: a **new read-only agent consumer** of our work-state, additive, no change to our writes / derive / operator board / phasing / registries / ingest. Ownership boundary affirmed — **you own** the comprehension/scene-graph consumer; **we own** the Missions data layer (`chain_events` spine, `missions.json`/`captures.json`, ingest endpoint, operator board, phasing). Cross-boundary changes come as a written request through Larry; neither chat edits the other's substrate.

## 2. What's already agent-consumable as data (build on these today)

- **`task_id`** — stable universal key, unchanged. ✅
- **`KNOWN_EVENT_TYPES`** — extensible allowlist; new types are one-line PRs we own (recently added `desktop_session_*`, `capture`). Send field/type needs as a written request and we'll add them. ✅
- **`missions.json` / `captures.json`** — version-controlled files, readable directly. ✅
- **`chain_events`** — queryable Supabase table. ✅
- **Droplet JSON endpoints** — `GET /api/system/missions` (raw registry) and `GET /api/missions/captures` (parked items). ✅

## 3. One real gap to flag (re: your ask #1 — "consume derived state, don't re-derive")

The **derive** — per-task phase (drafting/ready/in_flight/awaiting_merge/shipped), orphan detection, mission aggregate phase — currently lives in the **dashboard (TypeScript, `lib/mission-queries.ts`)**, not on the droplet. So today an agent can read the *raw* substrate cleanly, but to get the *derived* work-state it would have to re-implement the derive in Python (TS↔Python drift — the exact failure mode your project exists to prevent) or call the auth-gated Vercel app.

**Our plan (Phases 2–3):** lift the derive to a **single droplet-side source of truth** (Python endpoint) that both the dashboard and your scene-graph consumer read. One derive, no drift; it just relocates our existing `registry + derive` logic to the data tier where an agent can reach it. We'll own that endpoint and design it for your consumption. **Captures + raw events + raw registry are already droplet-data-reachable** — only the mission-phase derive needs this move.

## 4. Convergence worth noting

Your scene graph is the concrete consumer for an idea we independently parked the same day (`cap-bidirectional-missions-board` — "agents read the board to self-prioritize"). We're keeping it parked and linking it to this interface, so the design conversation has one home.

## 5. Timing

Phase 1 (durable capture + GC) just shipped and is proven/live — the substrate is stabilizing. We'll bake the **droplet-side derived read path** into the Phase 2 design now, so it's ready when you reach the scene-graph consumer in your backlog. Send the concrete interface request (fields, shape, cadence) whenever you're ready; we'll own and add it.

## 6. TL;DR

> Boundary confirmed, build on `task_id` + the raw droplet endpoints today. The one thing not yet data-reachable is the **mission-phase derive** (it lives in the dashboard TS) — we'll relocate it to a droplet-side source-of-truth endpoint in Phase 2 so you consume derived state without re-deriving. Everything else you need is a written request through Larry, which we own.
