# Spec: Projects Tab v3 — P1: Funnel + Missions retirement (data layer)

**Status:** Ready to build
**Owner / approver:** Larry (P1 outcome approved 2026-06-16)
**Author:** Claude Code (desktop design session)
**Parent North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) (§7 P1)
**Builds on (shipped):** Phase 4 meaning layer · 4.1 Narrator schedule · Phase S two-way sync · delegate fix.
**Build path:** build-sequence orchestrator (single repo: `ourliberty-agent-core`).

> P1 is **data/plumbing only** — no visible UI redesign (that's P2). The derive change is **additive/backward-compatible** so the current board keeps working.

---

## 0. Desired End State
The tab's intake shows your parked items + the team's suggestions front-and-center, the dead orphan clutter is cleared, and the old inert "Missions" plumbing no longer drives anything — and **nothing in-flight breaks** when we stop relying on missions.

## 1. Why now
Foundation for the Projects-tab redesign: the funnel cards (P2) and the project/phase pipeline (P3) can't be built on the inert missions data model, and the stale board (151 missions, 135 proposed) needs draining first.

## 2. Scope & non-goals
**In (all agent-core, backend):**
- (a) **Migrate the `target_repo` fallback** off `missions.json` to the chain-events activity log.
- (b) **Auto-drop verifiably-dead orphans** (terminal: PR merged or closed only) + **archive the 9 legacy `drafting` missions**.
- (c) **Reframe the derive additively**: classify intake into **parked + suggested (primary)** vs **orphaned (secondary)**, without breaking existing response fields.

**Out:** the universal card UI + actions (P2), the project/phase pipeline (P3), closeout (P4), DAG-status mirroring (P5). No storage migration — `missions.json` is **relabeled in place**, not moved (Fork 2, decided).

## 3. Reuse & restock  *(consulted shelf + graph 2026-06-16)*
**Reuse (assembly, not greenfield):**
- **`task_resolution`** (`scripts/task_resolution.py`) — the fail-closed "is this task already resolved?" check (chain_events resolution event OR merged/closed PR matching the task). **This is the verifiably-dead criterion** for the orphan drop. Reuse `shipped_pr` / `pr_matches_task` + the chain-events check; do **not** write new terminal detection.
- **`supabase_chunk`** (`scripts/supabase_chunk.py`) — chunked batch UPDATE/clear by id-list with resume-on-partial-failure. **The batch-dismiss mechanism** (no per-item PR spam).
- **`heal_missions_board_drain.py`** — the existing drain scaffold (surfacing + close-promoted passes); extend it to also drop terminal orphans + archive legacy drafts.
- **`_orphan_label_and_location(events, task_id)`** in `dashboard_api.py` — already extracts `repo`/`branch` from a task's chain-event payloads. **Reuse this reader for the `target_repo` backfill** (the repo lives in the event payload).

**Restock (after build):** `scripts/chain_envelope.py` is **uncatalogued** (2 dependents) — add a shelf card for it once P1 touches it.

## 4. Contracts

### C1 — `target_repo` resolves from the activity log
`chain_envelope.backfill_target_repo(task_id)` no longer reads `missions.json`. It derives the repo from the task's `chain_events` payloads (reuse the `_orphan_label_and_location` extraction pattern). Recovery-only path (envelopes normally carry `target_repo`); must return the same answer the missions lookup did for in-flight tasks. **No missions.json read remains in `chain_envelope.py` after this.**

### C2 — Drop verifiably-dead orphans (batched, fail-safe)
Extend `heal_missions_board_drain.py`: for each orphan-derived `proposed` item, run the `task_resolution` verified-terminal check (PR merged **or** closed, both via the belt-and-suspenders gate). **Only verifiably-terminal items are dropped** (set `acknowledged`/retired); anything open, unmerged, or indeterminate is **kept** (fail-safe). Writes are **batched via `supabase_chunk`** and honor the single-committer invariant (the drain writes the delta atomically; `heal_missions_card_gc` commits). Never a clock-based drop.

### C3 — Archive the legacy `drafting` missions
The 9 hand-authored `drafting` missions (`proposed_by: None`, never advanced) are **archived** (moved to an archive store / flagged retired) — **not deleted**. Idempotent; re-runs are no-ops.

### C4 — Additive funnel derive
`/api/missions/derived` classifies intake into **primary** (parked captures + team-suggested) and **secondary** (orphaned, auto-filtered). **Additive only** — existing fields/sections stay so the current board doesn't break; P2 consumes the new grouping. Suggested-source tagging (Beacon/Medic/Pulse) is recorded where available.

## 5. Risks & guardrails
- **Migrate before remove:** C1 must resolve correctly for a real in-flight task *before* any missions read is deleted. Prove it.
- **No false drops:** C2 drops only on a real terminal signal; an unmerged/uncertain orphan is never dropped.
- **No data loss:** C3 archives, never deletes; `missions.json` is relabeled in place, not moved.
- **Single-committer:** all `missions.json`/`captures.json` writes go through the existing committer; no second writer.
- **No PR spam:** the drop is one batched operation, not N PRs.
- **Backward-compatible derive:** C4 is additive; the live board must keep rendering.

## 6. Done-gate
- `target_repo` resolves from chain_events for a known in-flight task (proven), and `chain_envelope.py` has no `missions.json` dependency.
- Dead orphans dropped; live/uncertain ones retained and still visible; the 9 legacy drafts archived (recoverable).
- The derive returns the additive funnel grouping; the existing board still renders.
- Tests cover C1–C4; nothing in-flight breaks.

## 7. Build sequence (recommended — finalize via DAG-preflight)
Single repo (`ourliberty-agent-core`). **Serialization hazard:** C2 and C3 both write `missions.json`/`captures.json` via the single committer — serialize them. C1 (`chain_envelope.py`) and C4 (`dashboard_api.py`) touch distinct files.

| Step | Contract | File(s) | depends_on |
|---|---|---|---|
| **p1-target-repo** | C1 | `scripts/chain_envelope.py` (+ reuse `_orphan_label_and_location`) | — |
| **p1-drain-archive** | C2 + C3 | `scripts/heal_missions_board_drain.py` (+ `task_resolution`, `supabase_chunk`) | — |
| **p1-funnel-derive** | C4 | `scripts/dashboard_api.py` | p1-drain-archive |

Each step ends at its done-gate (tests green + the contract demonstrably holds), not just compiling. Mirror's DAG-preflight finalizes ordering; serialize any file/commit overlap it flags.
