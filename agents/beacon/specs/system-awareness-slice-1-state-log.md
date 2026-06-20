# Build spec — System self-awareness, Slice 1: the work-in-flight State Log

**Mission:** System self-awareness (the "standing brain") — see [docs/system-awareness-north-star.md](../../../docs/system-awareness-north-star.md) + [docs/desired-end-state.md](../../../docs/desired-end-state.md).
**Slice:** 1 of N — "work in flight, narrated end-to-end."
**Status:** Draft v1 for build — 2026-06-19.
**Repo:** ourliberty-agent-core (+ one dashboard read endpoint, see D3).
**Author:** Claude Code (desktop). **Approver:** Larry.

> Grounded against the live code (Explore pass 2026-06-19). This generalizes the EXISTING per-card missions Narrator to whole-system scope. **Reuse, don't reinvent.**

---

## 0. Goal (one paragraph)

Stand up the first **State Log**: a continuously-updated, plain-English picture of *all work currently in flight* across the system — active missions and the build/PR status underneath them — written on a schedule by a generalized Narrator, and **readable by Beacon** so Larry can ask "where are we?" and get a current, trustworthy answer without summoning Claude. This is the substrate (the "how it is right now" teal-doc), proven on the cheapest, lowest-risk surface first.

## 1. Success bar (from the DES — this is the acceptance test)

For a sustained stretch, **Larry asks Beacon "where are we on work in flight?" and gets a current, accurate, plain-English answer** — what missions are active, what's building, what's in review, what's stuck, what's waiting on him — that matches ground truth, without pinging Claude. (Dashboard rendering is a fast-follow, not this slice — see §5.)

## 2. Reuse mandate (do this FIRST)

1. Run the build-loop consult: `python3 pipeline/build_check.py "whole-system work-in-flight status narrative state log" missions_narrator task_terminal_state` from `ourliberty-graph` (with `OL_AGENT_CORE`/`OL_DASHBOARD` set). Honor STRONG/POSSIBLE reuse hits.
2. **Reuse these existing parts (do not rewrite):**
   - `scripts/missions_narrator.py` — `generate_briefing_voice()` (claude CLI round-trip + tolerant JSON parse), `author_meaning_layer()` pattern, the deterministic raw-render fallback, `NARRATOR_MODEL`/`CLAUDE_TIMEOUT_SEC`. The new whole-system narrator is a sibling that reuses these helpers.
   - `scripts/task_terminal_state.py` — the per-task OPEN/MERGED/CLOSED/UNKNOWN probe. Use it for pipeline status; do not re-derive phase.
   - `atomic_io` (shelf component) — for the atomic write.
   - The dashboard_api reader pattern (e.g. `_reader_missions`/`_reader_captures`) — copy it for the new endpoint (D3).

## 3. Deliverables

### D1 — The aggregator + narrator (`scripts/system_state_log.py`)

A pure-ish module that builds a **structured snapshot** then authors a **narrative** over it.

- **Reads (droplet filesystem + modules, NO HTTP):**
  - `agents/beacon/missions.json` → active missions (phase ∈ {drafting, in_flight, ready}; exclude terminal/proposed-funnel).
  - For each active mission's `task_ids` → `task_terminal_state` probe → per-task pipeline status (building / in-review / merged / stuck / unknown).
  - `~/agents/state/in-flight/*.json` → count + list of tasks actively dispatched right now.
  - `~/agents/blackboard/build-sequences/*.json` → active/paused multi-step sequences (seq_id, current step N/M, status).
  - `agents/beacon/captures.json` (state==parked, optional) → count of parked items awaiting Larry (for the "what's waiting on you" line).
  - chain_events (best-effort, optional) → recent event texture per task (e.g. "review passed, awaiting merge"); tolerate Supabase-absent.
- **Builds** `structured_snapshot`:
  ```
  {
    as_of: <ISO ts>,
    missions_active: [{id, name, phase, repo, tasks:[{task_id, status}], rollup:"N building / M in-review / K merged"}],
    pipeline: {building:int, in_review:int, merged_recent:int, stuck:[{task_id, why}]},
    in_flight_now: int,
    sequences_active: [{seq_id, step:"N/M", status}],
    waiting_on_larry: {parked:int, ...},
    health: null   // reserved — NOT this slice (Slice C)
  }
  ```
- **Authors** `narrative_prose` via `generate_briefing_voice()` (reuse) over the snapshot — plain English, Larry's voice, blast-radius framing; **deterministic fallback** renders the snapshot as terse bullets when the LLM call fails (never empty, never crash).
- **Writes** `~/agents/blackboard/system-state-log.json` **atomically** (atomic_io), shape:
  ```
  { schema_version:1, as_of, narrative_prose, structured_snapshot, provenance:{by:"system-state-narrator", model, at, fallback:bool} }
  ```
- **Invariant:** this file is **droplet runtime state, NOT git-committed** (lives in `~/agents/blackboard/`, like the other blackboard files). The narrator is its **sole writer**. It NEVER writes missions.json/captures.json — read-only on those. (Preserves the single-committer rule.)
- **Bounded + fail-safe:** one LLM call per tick max; total work bounded; any read failure degrades that section, never aborts the whole log.

### D2 — Schedule it

Ride the existing GC healer tick (no new systemd unit, lowest ops surface): call `system_state_log.write_state_log()` once per `heal_missions_card_gc.py` tick (~10 min), AFTER the Narrator sweep so it sees fresh briefings. Fail-isolated: a State-Log error must not break the GC tick.
- *(Alternative if it needs its own cadence later: standalone `ourliberty-system-state-log.{service,timer}` — defer unless 10 min proves too coarse.)*

### D3 — Read path: `GET /api/system/state-log` (dashboard_api.py)

Serve `~/agents/blackboard/system-state-log.json` (+ mtime/staleness flag) via a reader copied from the existing `_reader_*` pattern. Auth: same gate as the other system endpoints. This is the one dashboard-side change in this slice; it's a pure read endpoint (no board UI — that's the board stream's, §5).

### D4 — Beacon reads it ("where are we")

In `scripts/beacon_telegram_bot.py`: add a handler so a "where are we" / "status" / `/status` query reads the State Log (via D3 or direct file read) and replies with `narrative_prose`, plus a one-line freshness note ("as of Xm ago"). If the log is stale (> ~25 min) or missing, say so honestly rather than answer from nothing.

## 4. Acceptance criteria

- [ ] `system_state_log.py` writes a valid `system-state-log.json` with a non-empty `narrative_prose` and a correct `structured_snapshot` (verified against ground truth: active missions, in-flight count, a known in-review PR).
- [ ] Deterministic fallback produces a usable log when the LLM call is forced to fail (no crash, no empty log).
- [ ] The narrator never mutates missions.json/captures.json (assert read-only).
- [ ] The GC tick writes/refreshes the log each cycle and a State-Log error does not break the GC tick.
- [ ] `GET /api/system/state-log` returns the log + a staleness indicator.
- [ ] Beacon answers "where are we?" from the State Log, with a freshness line, and degrades honestly when stale/missing.
- [ ] Unit tests for the snapshot builder (fixtures for missions/in-flight/sequences) + the fallback path. (`unittest`, touched suites only.)
- [ ] `/code-review high` clean before merge (the gate — no CI). Restock the shelf if a reusable part was added.

## 5. Out of scope (explicit)

- **Dashboard 3-depth rendering** of the State Log (glance/skim/full) — that's the **Projects-tab-v3 board stream's** surface; coordinate via Larry, don't build it here. This slice satisfies the DES bar through Beacon.
- **The Orchestrator** (diagnosis/direction) — later mission phase.
- **Synthesized system *health*** ("what's wrong vs benign") — that's Slice C, deliberately held (false-reassurance risk).
- The why/what/how doc maintenance loop.

## 6. Risks / notes

- **Trust is the whole game:** if the narrative is wrong, Larry stops trusting it and goes back to Claude. The structured_snapshot must be ground-truth-accurate (it's mechanical); the prose rides on top. Prefer "I don't know / stale" over confident-wrong.
- **Who writes the prose** (cheap vs Opus) is an open DES question — this slice uses the existing `NARRATOR_MODEL` (opus) on the 10-min tick; measure cost/quality and revisit.
- Keep the snapshot read-only and the file uncommitted — the single-committer scars (#409/#413 class) are why.
