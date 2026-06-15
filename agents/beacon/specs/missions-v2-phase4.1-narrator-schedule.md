# Spec: Missions v2 — Phase 4.1: Schedule the Narrator (durability)

**Status:** Draft — ready to sequence
**Author:** Claude Code (desktop session, 2026-06-15)
**Approver:** Larry (approved autonomous build while away, 2026-06-14)
**Parent:** [docs/meaning-layer-roadmap.md](../../../docs/meaning-layer-roadmap.md) §4 Phase 4.1
**Predecessor:** [Phase 4 — operator meaning layer](missions-v2-phase4-meaning-layer.md) (shipped + validated; the Narrator authors briefings but nothing runs it)
**Build path:** build-sequence orchestrator, single-repo (agent-core only)

---

## 1. Purpose

Phase 4 shipped the Narrator (`scripts/missions_narrator.py`) but **no schedule runs it** — its first pass had to be kicked manually (2026-06-14), and new/re-stated parked cards sit at the "still writing up" placeholder forever. Phase 4.1 makes the meaning layer **standing**: the Narrator runs on a cadence, re-briefs on change, and is robust to a non-JSON model reply.

**Done-gate:** a newly-parked capture gets a briefing + risk **without anyone running anything by hand**, within one healer cadence; a capture whose state changes (promote/drop/snooze) gets re-briefed; and a non-JSON model reply no longer drops a card to the bare fallback when a valid briefing is extractable.

---

## 2. Design decision — single committer (read first)

`captures.json` is a machine-owned file with the dual-committer data-loss hazard (the #409→#413 class). The Narrator must **not** become a second independent writer process. So Phase 4.1 **folds the Narrator sweep into the existing `heal_missions_card_gc.py` tick** — the healer already owns the `captures.json` read→write→commit cycle on its ~10-min timer. One process, one writer, one batched commit. (Rejected: a sibling `missions-narrator.timer` — it adds a second independent writer of `captures.json` and a new install-drift surface, for no benefit since the cadences match.)

---

## 3. Contract A — scheduled authoring (folded into the GC healer)

In `heal_missions_card_gc.py`'s tick, after the existing session-retire + capture-aging steps and **before** the commit/push step:
- Call the Narrator's author path for every capture where `needs_briefing` is true (new, or state changed since last brief — §4). Reuse `missions_narrator`'s existing idempotent `needs_briefing` + author functions; do **not** duplicate logic.
- **Bound the work per tick** (`NARRATOR_MAX_PER_TICK`, default ~8) so an LLM-slow tick can't run unboundedly; the remainder briefs next tick (idempotent). Log how many briefed and how many deferred.
- The healer's existing single write + batched git commit carries the new `briefing`/`risk`/`recommended_action` fields — no second writer, no second commit path.
- Fail-safe: an author error on one capture is logged and skipped (never aborts the tick, never corrupts the file) — the deterministic fallback already guarantees a usable briefing.

## 4. Contract B — re-brief on state change

A briefing is stamped with `briefing_provenance.from_state`. A card is **stale** (needs re-brief) when its current `state` ≠ `from_state`, or it has no briefing. Implementation: extend `missions_narrator.needs_briefing` to treat a `state`/`from_state` mismatch as needing a brief. The capture-ingest (new capture) and capture-action (promote/drop/snooze) paths already write `captures.json`; they need **no new writer** — the next folded sweep re-briefs any card whose state moved. (This keeps the single-committer invariant: endpoints change state, the healer re-authors.)

## 5. Contract C — harden the LLM-JSON parse

The first pass logged *"claude result was not a JSON briefing; using raw briefing"* on 1/14 — the model returned a valid briefing wrapped in prose/fences that the strict parse rejected. Harden `missions_narrator`'s parse: strip code fences, extract the first balanced JSON object, tolerate leading/trailing prose; only fall to the deterministic raw briefing when no valid JSON object is extractable. Keep the raw fallback as the safety net. Add unit tests for fenced / prose-wrapped / trailing-comma / truly-unparseable inputs.

---

## 6. Build plan — 2 steps (single-repo)

| Step | Scope | depends_on |
|---|---|---|
| **1 — schedule + harden** | Fold the Narrator sweep into `heal_missions_card_gc.py` (Contract A) with `NARRATOR_MAX_PER_TICK`; harden the JSON parse (Contract C); tests | — |
| **2 — re-brief on change** | `needs_briefing` treats `state ≠ from_state` as stale (Contract B); tests for promote/drop/snooze re-brief | 1 |

Single-repo (agent-core); linear (2 depends on 1). No dashboard change, no new systemd unit (rides the existing GC healer timer → **no install-drift surface**).

## 7. Test / proof plan

- **1:** GC healer tick briefs un-briefed parked captures (mock the author to avoid LLM in tests); respects `NARRATOR_MAX_PER_TICK` and defers the rest; a per-capture author error is skipped, not fatal; parse-harden unit tests (fenced/prose/garbage).
- **2:** a capture promoted/dropped/snoozed is re-briefed on the next sweep (its `from_state` no longer matches); an unchanged briefed capture is left alone (idempotent).
- **End-to-end (post-merge, on the droplet):** park a fresh capture → within one GC tick it shows a briefing + risk on the board, no manual run.

## 8. Out of scope (later phases)

- Two-way sync / closed-loop status + auto-close + cost on the card → **Phase S** (roadmap §4).
- Card-specific risk notes, generalizing the Narrator out of the captures-only mold → Phase 4.2.
- Near-real-time chat → Phase 4b.
