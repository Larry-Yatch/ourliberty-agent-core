# Spec: Missions v2 — Phase 1: Durable Capture + GC

**Status:** Draft — ready to sequence (builds on the locked decisions in the design pass)
**Author:** Claude Code (desktop session, 2026-06-09)
**Approver:** Larry
**Parent:** [docs/missions-redesign-design-pass-2026-06-09.md](../../../docs/missions-redesign-design-pass-2026-06-09.md)
**Predecessor:** [Phase 0 — desktop session feed](missions-v2-phase0-desktop-session-feed.md) (shipped #404; froze the `captures.json` schema in its §4)
**Build path:** build-sequence orchestrator, single-repo split (PR-S3 precedent), one phase = one sequence

---

## 1. Purpose

Phase 1 retires the **timers-and-fear** workflow. When Larry (or I) surface a follow-up/idea/hole mid-work, it becomes a **durable, one-gesture capture** that outlives the chat and resurfaces by context — never a timer Larry has to set. Capture is **broad** (decision #2: err toward keeping everything); a **GC healer** keeps the board clean so breadth doesn't drown it. This is the P2 pillar of the redesign.

**Done-gate:** I can capture a follow-up from a desktop chat in one gesture; it appears as a durable card in the Missions **Parked lane**; it survives an app restart; and stale desktop-session cards (Phase 0) auto-retire instead of piling up.

---

## 2. Builds on locked decisions

| # | Decision (design pass §7) | How Phase 1 honors it |
|---|---|---|
| 1 | Sibling `captures.json`, promotes into `missions.json` | New `agents/beacon/captures.json`; schema frozen in Phase 0 §4 |
| 2 | **Broad** capture, controlled by **GC** not a gate | Capture is one ungated gesture; `heal_missions_card_gc` does the cleanup |
| 4 | I prep / Larry dispatches | Capture is parked, never auto-dispatched; promotion stays Phase 3 |

---

## 3. Contract A — `captures.json` (frozen in Phase 0 §4, restated)

Sibling to `agents/beacon/missions.json`, version-controlled.

```jsonc
{ "schema_version": 1, "captures": [ {
  "id": "cap-<kebab-slug>",
  "title": "Resurface stale PR comments",
  "note": "Mirror sometimes drops review comments on rebase; run this down.",
  "state": "parked",                    // parked | promoted | dropped
  "origin": { "source": "desktop-chat", "session_id": null, "repo": "ourliberty-agent-core",
              "branch": null, "captured_at": "2026-06-09T18:22:00Z" },
  "last_touched": "2026-06-09T18:22:00Z",
  "promoted_to": null
} ] }
```

## 4. Contract B — capture ingest

A capture is created the same way a desktop session is: a token-authed POST to the droplet (the desktop holds no DB/git creds). **Reuses the Phase 0 `X-Ingest-Token`.**

- **Route:** `POST /api/ingest/capture` (new, `dashboard_api.py`). Body `{ title, note?, origin? }`. Server pins `source` from a fixed set, generates `id` (`cap-<kebab(title)>-<short-rand-by-count>`), sets `state="parked"`, `captured_at`/`last_touched` = now, appends to `captures.json` under a lock. Idempotency: collapse a re-POST with identical `(title, origin.session_id)` within a short window onto the same id (don't double-park).
- **Write mechanism (resolves the design's open question):** the endpoint writes `captures.json` **atomically on the droplet** (tmp+rename, same as other agent state) — it does **not** open a PR (a PR-per-capture is too heavy for a low-ceremony, multiple-per-day gesture). Durability + audit trail come from a **batched commit**: the GC/sync healer (§6) commits + pushes any `captures.json` delta to `main` on its timer, exactly like the existing droplet self-commit path. So a capture is durable on disk immediately and version-controlled within one healer tick.
- **Responses:** `202 {ok, capture_id}`; `401` bad/missing token; `400` bad body; `413` over a 4 KB cap.
- **Desktop gesture:** `scripts/emit_capture.sh` + `_impl.py` (stdlib, mirrors the Phase 0 emitter) — I invoke it when I flag a follow-up, or when Larry says "capture this." Derives `origin` (repo/branch/session) from context; reads the same ingest token file.

## 5. Contract C — dashboard Parked lane (render-only in Phase 1)

- New `GET /api/missions/captures` (droplet) returns `captures.json` + mtime; the dashboard renders a **Parked lane** under the Missions board: title, note, age, origin chat, an "aging" nudge when `last_touched` > 5 business days. **Read-only** — promote/drop/snooze actions are Phase 3 (write-back).
- The lane lists `state == "parked"` only; `promoted`/`dropped` are hidden (kept in the file for audit).

## 6. The GC healer — `scripts/heal_missions_card_gc.py` (the cleanup system, §3.3 of the design)

Timer healer (systemd timer, ~10 min), reconciles the board so **broad capture stays clean**:

1. **Retire stale desktop-session cards** (Phase 0 feed): a `desktop_session_start` with no `desktop_session_done` whose branch is merged/deleted, **or** whose repo dir is gone/archived, **or** that's been idle past a staleness window → emit a synthetic `desktop_session_done` (via the same ingest path) so the card drops off. Mirrors `cleanup_dispatch_branches.py`'s philosophy applied to cards.
2. **Age parked captures:** mark (never delete) captures with `last_touched` > 5 business days so the dashboard nudges; this is the contextual-resurfacing seed (full digest is Phase 2).
3. **Commit + push** any `captures.json` delta to `main` (the batched-durability half of §4).
4. **Report** what it retired/aged (healer audit line), never silently truncating.

Idempotent, atomic writes, fail-safe (a bad tick pauses + reports, never corrupts).

---

## 7. Build plan — 3 steps (single-repo split, PR-S3 precedent)

| Step | Repo | Scope | depends_on |
|---|---|---|---|
| **1a — captures core** | agent-core | `captures.json` seed + `POST /api/ingest/capture` (+ `GET /api/missions/captures`) + `emit_capture.sh`/`_impl.py` + tests | — |
| **1b — GC healer** | agent-core | `heal_missions_card_gc.py` + systemd timer + tests (session-card retire, capture aging, batched commit) | 1a |
| **2 — Parked lane** | dashboard | render the Parked lane from `GET /api/missions/captures`; aging nudge; hide promoted/dropped | 1a |

1b and 2 both depend only on 1a (the data contract), so the advancer may run them in parallel after 1a merges. Linear (1a→1b→2) is the safe fallback if Mirror preflight flags any file overlap.

---

## 8. Test / proof plan

- **1a:** endpoint auth/validation/idempotency unit tests (mirror `test_desktop_session_ingest.py`); `emit_capture` round-trip writes `captures.json`.
- **1b:** healer retires a synthetic stale session card; ages a parked capture; commits the delta; no-op on a clean board.
- **2:** Parked lane renders parked captures, hides promoted/dropped, shows the aging nudge.
- **End-to-end:** capture a follow-up from a live chat → card in the Parked lane; restart → still there; merge a branch → its desktop-session card retires on the next GC tick.

## 9. Out of scope (later phases)
- Promote/drop/snooze **write-back** actions + the `trust_policy.py` autonomy ladder → Phase 3.
- The dashboard **digest** (daily "parked & aging" / catch-me-up) → Phase 2.
- Telegram/agent capture sources → later (Phase 1 ships the desktop gesture; the endpoint's `source` field already admits them).
- Repo-archive **detection** beyond "repo dir gone" → Phase 3 (explicit archive signal).
