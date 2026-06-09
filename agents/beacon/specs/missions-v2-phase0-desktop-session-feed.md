# Spec: Missions v2 — Phase 0: Desktop Session Feed (contract freeze)

**Status:** Draft — ready to build (decisions locked 2026-06-09)
**Author:** Claude Code (desktop session, 2026-06-09)
**Approver:** Larry
**Parent:** [docs/missions-redesign-design-pass-2026-06-09.md](../../../docs/missions-redesign-design-pass-2026-06-09.md) (§5 phasing, §8 build strategy)
**Phase:** 0 of the Missions v2 redesign — *prove the feed, freeze the contracts*

---

## 1. Purpose (why this phase is hand-built, not a sequence)

Phase 0 is deliberately **not** a build-sequence. Its job is to **discover and freeze the data contracts** that every later phase builds against — the `desktop_session` chain-event shape and the `captures.json` schema. DAGs execute a known plan; they're poor at *defining* an interface. So Phase 0 is one hand-built, reviewable PR (plus a Mac-side hook + a droplet secret), and its real deliverable is a **frozen contract proven on a live chat** — namely, *this* conversation appearing as a card on the Missions board.

**Success test:** after this lands, an active desktop Claude Code chat in a tracked repo shows up on the Missions tab (as an orphan card at minimum), and closing the chat retires it.

---

## 2. The core architectural decision (frozen)

The desktop **must not hold the Supabase service-role key.** Confirmed 2026-06-09: this Mac has zero Supabase creds (`~/credentials/` absent, no `.env.local`), and the service-role key is all-access — spreading it to a laptop is a security regression.

Therefore the desktop emits **indirectly** through a token-authed droplet ingest endpoint, which calls the existing canonical writer `chain_event_emit.emit_event(...)` server-side (the droplet already holds `SUPABASE_SERVICE_ROLE_KEY`).

```
Claude Code hook (Mac)
  → curl POST  https://api.ourliberty.dev/api/ingest/desktop-session   [X-Ingest-Token]
    → dashboard_api handler  (validates, pins agent + event_type prefix)
      → chain_event_emit.emit_event(...)        [service-role key stays on droplet]
        → chain_events (Supabase)  →  Missions tab join (existing, by task_id)
```

Rejected alternatives: **service key on the Mac** (security regression); **ssh-per-hook** (latency + fragile + runs remote Python from a hook). The HTTP ingest path needs only `curl` on the Mac — no Python, no supabase-py, no ssh.

---

## 3. Frozen contract A — the `desktop_session` chain-event

### 3.1 Event types (added to `KNOWN_EVENT_TYPES` in `scripts/chain_event_shipper.py`)
| event_type | When | Required in Phase 0 |
|---|---|---|
| `desktop_session_start` | A chat begins substantive work in a tracked repo (first `SessionStart` hook fire). | **Yes** |
| `desktop_session_done` | The chat ends (`SessionEnd`/`Stop`). Drives card GC. | **Yes** |
| `desktop_session_active` | Activity heartbeat — carries latest `last_activity_ts` + `blocked_on_larry`. | Optional (Phase 0.1) |

`agent` is **always** `desktop-claude` (new agent identity; the ingest endpoint pins this — a caller cannot spoof another agent). Dashboard color map: neutral/slate for `desktop-claude` in v0.

### 3.2 `task_id` derivation (the grouping key)
- **Default:** `desktop-<session_id_short>` (first 8 chars of the Claude Code session id) — unique per chat. With no matching mission, it renders in the **orphan lane** (acceptable; Phase 1 capture/registration links it).
- **Override (tagging):** if the session is tagged to a thread/mission, the hook uses that `task_id` verbatim so the card attaches under the mission via the existing join. Tag source (first found wins):
  1. env `OL_DESKTOP_TASK_ID`
  2. file `<repo>/.claude/desktop-session-tag` (one line: the task_id)
- `event_id = sha1(task_id, event_type, ts)` (existing `compute_event_id`) → idempotent; a retried emit upserts the same row.

### 3.3 Payload (frozen field set)
```jsonc
{
  "repo": "ourliberty-agent-core",          // basename of git toplevel; "" if not a git repo
  "branch": "feat/missions-phase0-desktop-feed",
  "cwd": "/Users/Larry/dev/ourliberty-agent-core.phase0",
  "title": "Missions v2 Phase 0",           // optional; from .claude/desktop-session-tag line 2, else ""
  "blocked_on_larry": false,                // false on start; true via desktop_session_active when awaiting Larry
  "host": "larrys-mac",                     // hostname, for multi-machine disambiguation
  "source": "startup",                      // hook source/reason (startup|resume|clear|…)
  "last_activity_ts": "2026-06-09T18:30:00Z" // (desktop_session_active only)
}
```
Payload runs through `sanitize_payload` server-side (defense-in-depth redaction) before insert — same as every other pushed event. **Note:** the raw `session_id` is deliberately NOT a payload field — `sanitize_payload` redacts any key containing `session_id`, so the short session ref is carried only inside `task_id` (`desktop-<session_id_short>`).

---

## 4. Frozen contract B — `captures.json` (frozen now; consumed in Phase 1)

Sibling to `agents/beacon/missions.json`, version-controlled. Frozen here so Phase 1 builds against a stable shape; **not implemented in Phase 0.**

```jsonc
{
  "schema_version": 1,
  "captures": [
    {
      "id": "cap-<kebab-slug>",             // stable id
      "title": "Resurface stale PR comments",
      "note": "Mirror sometimes drops review comments on rebase; run this down.",
      "state": "parked",                    // parked | promoted | dropped
      "origin": {
        "source": "desktop-chat",           // desktop-chat | telegram | agent | larry
        "session_id": "…",                  // the chat that birthed it (nullable)
        "repo": "ourliberty-agent-core",    // nullable
        "branch": "…",                      // nullable
        "captured_at": "2026-06-09T18:22:00Z"
      },
      "last_touched": "2026-06-09T18:22:00Z",
      "promoted_to": null                   // mission id once promoted (state=promoted)
    }
  ]
}
```

---

## 5. Frozen contract C — the ingest endpoint

- **Route:** `POST /api/ingest/desktop-session` (new, in `scripts/dashboard_api.py`).
- **Auth:** header `X-Ingest-Token` compared (constant-time) to `DESKTOP_INGEST_TOKEN` in the droplet env. **Dedicated token** — distinct from the dashboard read token; blast radius of a leak is limited to writing `desktop_session_*` events as `desktop-claude` and nothing else.
- **Request body:** `{ event_type, task_id, payload }`. Server **pins** `agent='desktop-claude'`, **rejects** any `event_type` not in `{desktop_session_start, desktop_session_active, desktop_session_done}`, caps `payload` at 16 KB (defense-in-depth vs. a leaked token), then calls `emit_event(...)`.
- **Responses:** `202 {ok:true, event_id}` on success; `401` bad/missing token (+ a WARN log if the token env is unset); `400` bad event_type/body; `413` payload over the cap; `502 {ok:false}` if `emit_event` returns False (Supabase down — best-effort, caller ignores).
- **Handler:** `_handle_desktop_session_ingest`; thread-safe (no shared mutable state; `emit_event` builds its own client).

---

## 6. The hook (Mac side)

`scripts/emit_desktop_session.sh` (stdlib `curl` + `git`; no Python dependency), wired into `~/.claude/settings.json`:

- **`SessionStart`** → derive repo/branch/cwd/host/session_id from the hook's stdin JSON + `git`, resolve `task_id` (§3.2), **gate on tracked repo** (cwd's git-toplevel basename ∈ tracked list: `ourliberty-agent-core`, `ourliberty-dashboard`, `ourliberty-graph`, and any `ourliberty-agent-core.*` worktree → normalized to `ourliberty-agent-core`), then `curl` `desktop_session_start`.
- **`SessionEnd`** (fallback `Stop`) → `curl` `desktop_session_done` with the same `task_id`.
- Reads the ingest token from `~/.config/ourliberty/ingest-token` (chmod 600). Fails silent (hook never blocks the session; a network error just means no card — acceptable, same best-effort contract as all pushed events).
- **Broad capture by design** (decision #2): any chat touching a tracked repo emits. Noise is controlled by GC (§7), not by a strict entry gate.

> Build-time detail to verify against live Claude Code: exact field names in the `SessionStart`/`SessionEnd` hook stdin JSON (`session_id`, `cwd`, `hook_event_name`, …). The frozen contract is the *emit payload + endpoint*; hook field-mapping is implementation and confirmed during the build.

---

## 7. Rendering + GC (minimal in Phase 0)

- **Render:** a `desktop_session_start` with no matching mission → **orphan lane** card (existing path), labeled with repo/branch/title and a `desktop-claude` chip. A tagged session whose `task_id` ∈ a mission's `task_ids` → attaches under that mission (existing join, zero new logic).
- **GC (Phase 0 minimum):** a session is "live" if it has a `desktop_session_start` with no later `desktop_session_done` for the same `task_id`. `desktop_session_done` retires the card. (Full lifecycle GC — branch-merge/idle/ repo-archive sweeps via a healer — is Phase 1's §3.3 cleanup system, not here.)

---

## 8. Files touched (Phase 0 build)

**agent-core (this PR, in the worktree):**
1. `scripts/chain_event_shipper.py` — add the three `desktop_session_*` types to `KNOWN_EVENT_TYPES`.
2. `scripts/dashboard_api.py` — `_handle_desktop_session_ingest` + route registration + `DESKTOP_INGEST_TOKEN` read.
3. `scripts/emit_desktop_session.sh` — the hook emitter (curl).
4. `scripts/tests/test_desktop_session_ingest.py` — endpoint auth/validation/pinning unit tests.
5. `docs/` / this spec.

**Environment (need Larry / droplet, tracked separately — NOT in the PR):**
6. Droplet: add `DESKTOP_INGEST_TOKEN` to `~/credentials/.env.larry`; `git pull` + restart `dashboard_api` so the new route is live.
7. Mac: write `~/.config/ourliberty/ingest-token`; add the two hook entries to `~/.claude/settings.json` (via the `update-config` skill).

---

## 9. Test / proof plan

1. **Unit:** ingest endpoint rejects missing/bad token (401), rejects non-`desktop_session_*` event_type (400), pins `agent='desktop-claude'` even if body says otherwise, returns 202 + event_id on success (mock `emit_event`).
2. **Integration (manual, on the droplet):** `curl` the live endpoint with a real token → row appears in `chain_events`.
3. **End-to-end proof:** tag *this* session (`OL_DESKTOP_TASK_ID=missions-phase0-desktop-feed`), fire the `SessionStart` hook, open the Missions tab → card visible. Close → card retires. **This is the Phase 0 done-gate.**

---

## 10. Out of scope (later phases)
- `captures.json` implementation + one-gesture capture (Phase 1).
- Full GC healer / branch-merge + repo-archive sweeps (Phase 1 §3.3).
- Dashboard "needs you" rail, lanes, dashboard digest (Phases 2 & 4).
- `trust_policy.py` promotion ladder (Phase 3).
- `desktop_session_active` heartbeat + `blocked_on_larry` surfacing (Phase 0.1 / 2).
