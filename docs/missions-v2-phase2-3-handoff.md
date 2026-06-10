# Missions v2 — Phase 2 & 3 Handoff (continuation)

**Purpose:** start a fresh chat to build Phases 2–3 of the Missions-tab redesign without re-deriving context. Phases 0–1 are shipped and proven; this doc + the project memory + the canonical docs below are everything the next chat needs.
**Date:** 2026-06-10

---

## 1. Where we are (one screen)

The Missions tab is being rebuilt into the single **work-state surface** for everything in flight or parked — desktop chats, the droplet agent fleet, and captured ideas — so the operator never holds it in their head or babysits timers. Two pillars, **both now LIVE**:

- **P1 — live work visibility (Phase 0, SHIPPED+PROVEN):** desktop Claude Code chats emit `desktop_session_*` events via a token-auth droplet ingest endpoint → appear on the board. Hooks wired on the Mac; service-role key stays on the droplet.
- **P2 — durable capture (Phase 1, SHIPPED+PROVEN+LIVE):** one-gesture capture → durable `captures.json` → Parked lane; a GC healer retires stale cards + ages parked captures + batch-commits. The timers-and-fear workflow is retired.

**Phase 2 and 3 are extension on a working, proven foundation** — same hands-free orchestrator path, no new core risk.

## 2. Canonical docs (read these; don't duplicate them)

| Doc | What it holds |
|---|---|
| `docs/missions-redesign-design-pass-2026-06-09.md` | The vision, the 5 locked decisions (§7), the build/orchestration strategy (§8), the phasing |
| `agents/beacon/specs/missions-v2-phase0-desktop-session-feed.md` | Phase 0 spec + frozen `desktop_session` + `captures.json` schemas |
| `agents/beacon/specs/missions-v2-phase1-durable-capture.md` | Phase 1 spec (captures + GC + parked lane) |
| `docs/missions-tab-capabilities-handoff.md` | Self-contained capabilities/architecture (for the sibling graph project) |
| `docs/missions-scene-graph-interface-reply.md` | The cross-project interface boundary + the Phase 2 derive decision |
| **Auto-memory** `missions-redesign-project.md` | Running state log — loads automatically in the new chat |

## 3. Phase 2 — Resurfacing (the timer replacement)

**Goal:** parked items resurface *by context*, not by a clock. Two parts:
1. **Contextual resurfacing** — when the operator touches a repo/mission, related captures surface.
2. **Dashboard digest** — a daily + on-demand "parked & aging" summary rendered **on the dashboard** (Approvals-tab-summary style, built on the `ceo_digest_generator` pattern), NOT Telegram (decision #3). Revives the dormant `operator-ux-catch-me-up-shortcut` mission. Aging = `last_touched` > 5 business days (the GC healer already marks this).

**ANCHOR DECISION (settle first) — the relocated derive endpoint.** Lift the mission-phase **derive** (phase / orphan / mission-aggregate) from the dashboard TypeScript (`lib/mission-queries.ts`) to a **single droplet-side Python source-of-truth endpoint** that both the dashboard and the sibling scene-graph consumer read. Kills TS↔Python re-derive drift; makes derived work-state agent-consumable. This is the load-bearing Phase 2 architecture choice.

Design constraints (folded in from the graph chat's preliminary consumption spec — `ourliberty-graph/docs/scene-graph-missions-request.md`):

- **Filterable, on-demand, read-only:** `GET …?repo=<target_repo>&task_id=<optional>` → just the relevant slice, freshly derived, low-latency (an agent pulls it right before a build, to orient). No write-back from consumers.
- **Returns derived fields, not raw:** per active task `{ task_id, mission, phase, target_repo, blocked_on?, last_activity_ts }`; per parked item `{ capture_id, title, repo/area }`. Consumers read the derived `phase` / orphan / aggregate — they never re-derive.
- **Answers, for a build target:** collisions (what's active on the same repo/area), related parked work, mission context + phase, blocks/orphans, recent activity.
- **~80% of this is already what the dashboard needs** — the derive already computes phase/orphan/aggregate, and Phase 1's Parked lane already lists captures. Build that now + make it **filterable**.
- **Extensible, not frozen:** the scene-graph-only bits (file/area-level collision, a general `blocked_on`) — design the response **versioned/extensible to** them but don't build them yet. Their final field list arrives as a **written request at their T8**; the endpoint is ours to own.
- **SCOPE GUARD:** don't let the scene-graph's needs balloon Phase 2. Its primary deliverable is the operator-facing resurfacing + digest; the derive endpoint is the *shared enabler*. Build to the dashboard's need + cheap filterability + extensibility; finalize scene-graph specifics at T8. (See `docs/missions-scene-graph-interface-reply.md` for the boundary.)

## 4. Phase 3 — Write-back + autonomy ladder

- **Write-back actions** from the UI: promote / drop / snooze a capture; defer / resume / reprioritize a mission — PR-backed like the existing New-Mission modal.
- **Auto-registration:** auto-claim orphan `task_id`s into a proposed thread (retire the Orphans lane).
- **Earned-autonomy ladder:** capture promotion graduates from "I prep / Larry dispatches" toward auto-dispatch for low-risk classes via the existing `scripts/trust_policy.py` (rules in `config/trust-policy.json`; default = everything asks), with a Pulse check proposing widenings (Doctrine #48). The dial stays in Larry's hand.

## 5. Cross-project interface (don't break it)

The sibling **ourliberty-graph** project will add a **read-only, agent-facing consumer** of our work-state (the "scene graph" — agents read it before building). **We own** the Missions data layer; **they own** the consumer; cross-boundary changes are **written requests through Larry, never parallel edits**. Keep `task_id` stable and `KNOWN_EVENT_TYPES` extensible. The Phase 2 derive-relocation (§3) is what makes their read clean — design for it.

## 6. Operating playbook (how a phase ships)

The proven loop, per phase:
1. **Author** a contract-first spec → commit to agent-core (Forge builds against it; merge it before kickoff).
2. **Author the build-sequence file** (`~/agents/blackboard/build-sequences/<seq>.json`): single-repo-split steps (agent-core before dashboard), `depends_on` DAG, `dispatch_text` **≤500 chars**.
3. **Validate:** `python3 scripts/build_sequence_validator.py <path-to-file.json>` (1-arg form; `validate <seq-id>` resolves the blackboard path).
4. **Kickoff is Beacon-mediated — do NOT shortcut it.** Place the sequence `pending`; the operator tells Beacon (Telegram) to run the Mirror DAG-preflight + kickoff. On Mirror PASS the notifier auto-activates → advancer dispatches step 1 → hands-free. (The kickoff marker needs `--phase routing-signal` to pass the min-prompt check.)
5. **Wire-and-prove:** a merge ≠ live. New endpoints need a `dashboard-api` restart; new healers need a systemd timer install+enable (the droplet's install-drift + stale-daemon healers often auto-do this — verify). Then prove end-to-end on a live case.

**Gotchas learned (don't relearn):**
- The harness auto-mode classifier **correctly gates production-orchestration shortcuts** — it blocked (a) droplet SSH until Larry authorized, and (b) flipping a sequence to `active` to *skip* the Mirror preflight. Route kickoff through Beacon; don't bypass review gates.
- A merge ≠ deployed/proven (Phase 0 and 1 both needed an explicit light-up).
- This clone self-advances mid-session (automation moves HEAD); **build in an isolated `git worktree`** off `origin/main`, not the main checkout.
- Droplet access: `ssh larry@134.209.44.80` (creds at `~/credentials/.env.larry`; service-role key lives there, never on the Mac).
- Run tests with `python3 -m unittest`; dashboard suites need a fastapi+httpx venv.

## 7. Open items / parked

- **Capture gesture:** today it's conversational (Larry says "capture X" or desktop-Claude flags → run `~/.config/ourliberty/emit_capture.sh "<title>" ["<note>"]`). A Larry-facing button is Phase 3 write-back.
- **Parked capture to revisit:** `cap-bidirectional-missions-board` — "agents read the board to self-prioritize" — now has a concrete driver (the scene graph). Decide in Phase 2/3 whether to promote it.
- **S700 cleanup hygiene:** worktrees self-clean after merge if you `git worktree remove`; the dispatch-branch accumulation problem is separate and tracked elsewhere.

## 8. First move in the new chat

Read this doc + the design pass §7–8 + the scene-graph reply, confirm the **Phase 2 derive-relocation** approach with Larry, then author the Phase 2 spec (resurfacing + dashboard digest, built on the relocated derive). Ship it as one orchestrator sequence the same way Phase 1 ran.
