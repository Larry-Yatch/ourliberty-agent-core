# Spec: Missions v2 — Phase 3: Write-back + Auto-registration (+ autonomy ladder)

**Status:** Draft — ready to sequence (builds on Phases 0–2, all shipped + proven/live)
**Author:** Claude Code (desktop session, 2026-06-10)
**Approver:** Larry
**Parent:** [docs/missions-redesign-design-pass-2026-06-09.md](../../../docs/missions-redesign-design-pass-2026-06-09.md) (§5 Phase 3, §6 autonomy ladder)
**Predecessors:** [Phase 1 — durable capture](missions-v2-phase1-durable-capture.md) · [Phase 2 — resurfacing + derive](missions-v2-phase2-resurfacing-and-derive.md)
**Build path:** build-sequence orchestrator, single-repo split (PR-S3), interface-first, one phase = one sequence

---

## 1. Purpose

Phases 0–2 made the Missions tab a **durable, readable, self-resurfacing** work-state surface — but it is still **read-only**. Every action (promote a capture, drop it, defer a mission, claim an orphan) happens *outside* the board, by hand. Phase 3 closes that loop: **act on the board, from the board** — PR-backed, with the autonomy dial staying in Larry's hand.

Three pillars, sequenced by value and by how much trust-data they need:

1. **Write-back actions** (this sequence, first) — promote / drop / snooze a capture; defer / resume / reprioritize a mission. Makes the Parked lane and the parked-&-aging digest **actionable** instead of catch-me-up-only.
2. **Auto-registration** (this sequence, second) — auto-claim orphan `task_id`s into *proposed* mission threads, shrinking the Orphans lane toward zero. The concrete home for the parked `cap-bidirectional-missions-board` idea.
3. **Earned-autonomy ladder** (FOLLOW-ON sequence, §8) — capture promotion graduates from "I prep / Larry dispatches" toward auto-dispatch for low-risk classes via `scripts/trust_policy.py`, with a Pulse check proposing widenings (Doctrine #48). Split out because it needs promotion-success data this sequence produces.

**Done-gate (this sequence):** From the Missions tab, Larry can promote / drop / snooze a parked capture and defer / resume / reprioritize a mission — each lands as a PR he merges (or trust-policy auto-merges for a carved-out low-risk class); and unregistered orphans auto-surface as *proposed* threads he accepts or dismisses, so the Orphans lane trends toward zero.

---

## 2. Scope guard

| In scope (this sequence) | Out of scope / deferred |
|---|---|
| Capture write-back: promote → mission, drop, snooze | The autonomy ladder itself (§8, follow-on sequence) |
| Mission write-back: defer, resume, reprioritize | Auto-DISPATCH of promoted work (stays "Larry dispatches" until §8) |
| Auto-registration: orphan → *proposed* thread (accept/dismiss) | Programs↔Missions unification, drag-drop, full PM tooling (out of arc) |
| PR-backed writes (New-Mission pattern) + optimistic UI | Bulk/multi-select actions (nice-to-have, later) |
| `snooze` field on captures + resurfacing honoring it | Write-back from Telegram (dashboard is the surface — decision #3) |

**Default stays "everything asks."** Every write-back opens a PR Larry merges. Auto-merge for any class is **off** until the §8 ladder explicitly carves it out via `trust_policy.py`. This sequence never auto-dispatches build work.

---

## 3. The write mechanism — reuse the New-Mission PR pattern (no new infra)

`POST /api/system/missions/new` already does exactly the shape Phase 3 needs ([`dashboard_api.py` `_handle_new_mission`](../../../scripts/dashboard_api.py)): read the registry locally, compute the mutated registry, then push it as a **branch + PR via the GitHub REST API** (GET main ref → create branch → PUT contents → POST PR) — **the local `missions.json`/`captures.json` are NOT mutated** (they update via `git pull` on merge, avoiding drift in the shared `~/agent-core` checkout that `heal-droplet-git-drift` would flag).

Phase 3 write-back reuses this verbatim, generalized to both registries:

- **Auth:** `_github_token()` — which (PR #427) now falls back to `gh auth token`, so this path is already un-blocked on the droplet (no `GITHUB_TOKEN` env var needed).
- **Idempotency/locking:** the existing `_NEW_MISSION_LOCK` pattern (in-process lock serializing concurrent writes) extends to a shared registry-write lock.
- **One PR per action**, titled + bodied so the diff is self-explanatory (e.g. `chore(missions): promote capture cap-… → mission …`).

**No new credential, no new service** — same data tier, same token path, same PR-review flow (Mirror reviews the registry-edit PR; trust-policy may auto-approve a `doc-only`/registry-only class once §8 lands).

---

## 4. Contract — capture write-back

New endpoint: `POST /api/missions/captures/{capture_id}/action`, body `{ "action": "promote" | "drop" | "snooze", ...args }`. Auth: `X-Dashboard-Token` + `X-Actor` (mirrors `/api/larry/action`). Each returns `{ pr_url, branch }` (or `{ applied: true }` for the local-only snooze write — see below).

### 4.1 `promote` — capture → mission
Creates a `missions.json` entry from the capture (the only moment heavyweight fields appear — design pass §3.1) and sets the capture's `promoted_to` + `state: "promoted"`. Args: optional `{ name?, brief?, repo?, spec_docs? }` overrides (defaults inferred from the capture's title/note/origin). **Opens ONE PR editing both files** (capture `promoted_to`/`state` + new mission entry) so they land atomically. The promoted mission starts at `phase: "drafting"` — **no dispatch** (Larry dispatches; §8 may automate low-risk classes later).

### 4.2 `drop` — retire a capture
Sets `state: "dropped"`. PR-backed (auditable — never a silent delete; the GC healer already moves dropped/promoted captures to a collapsed lane). Optional `{ reason? }` recorded in the capture.

### 4.3 `snooze` — defer resurfacing
**New capture field** `snoozed_until: <ISO date> | null`. Adds it to the frozen `captures.json` schema (one-line additive PR; `KNOWN_EVENT_TYPES`/schema is ours to extend). A snoozed capture:
- stays `parked` (not dropped — it's still in the holding tank),
- is **suppressed from the parked-&-aging digest and contextual resurfacing until `snoozed_until` passes** (the digest generator + the derive's `parked[]` honor it),
- the GC healer's aging clock pauses while snoozed (no `aging:true` flag during snooze).

**Snooze write path — DECIDED (Larry, 2026-06-10): direct via capture-ingest, NOT PR-backed.** Snooze is high-frequency + low-stakes (one reversible date field), so it routes through the existing `/api/ingest/capture` writer — already the **single committer** of `captures.json` ([[machine-owned-file-single-committer]]) — rather than opening a PR per snooze. The endpoint returns `{ applied: true, snoozed_until }`. Promote/drop stay PR-backed (they touch `missions.json` / are state-terminal). Implementation note: extend the capture-ingest writer with a `snooze` operation (set/clear `snoozed_until`), preserving its atomic-write + single-committer discipline; do NOT add a second writer of `captures.json`.

---

## 5. Contract — mission write-back

New endpoint: `POST /api/system/missions/{mission_id}/action`, body `{ "action": "defer" | "resume" | "reprioritize", ...args }`. PR-backed (missions.json is the curated registry — every change auditable). Returns `{ pr_url, branch }`.

- **`defer`** — set `phase: "deferred"` + `deferred_reason: <text>`. The derive's `aggregate_mission_phase` already treats `deferred` as an override (Phase 2 §3.4), so the board reflects it on merge with zero new derive logic.
- **`resume`** — clear `deferred`, restore to the derived phase (drop `deferred_reason`).
- **`reprioritize`** — **new optional `priority: int` field** on mission entries (additive; absent = default). Drives row ordering on the board (a thin sort; no new lane). One-line schema extension.

All three are single-field registry edits → small PRs Mirror waves through; a `registry-only` trust-policy class (§8) is the natural first auto-approve carve-out.

---

## 6. Contract — auto-registration (orphans → proposed threads)

Today the Orphans lane is a remediation surface that never empties itself (117 actionable orphans live as of 2026-06-10). Auto-registration makes it self-draining.

- **A healer** (sibling to `heal_missions_card_gc`, the GC pattern) periodically scans **non-terminal, non-infrastructure** orphans (the Phase 2 derive already classifies these — `terminal=false`, `is_infrastructure_task=false`) and emits a **proposed thread**: a lightweight `captures.json`-style proposal (or a `missions.json` entry with `phase: "proposed"`) carrying the orphan's `task_id`, derived label, repo/branch, and last activity.
- **Proposed-thread storage — DECIDED (technical): a `missions.json` entry with `phase: "proposed"`** (a new phase enum value), NOT a sibling `proposals.json`. Reuses the one registry + the derive + the board's existing join — one store, no drift. `phase: "proposed"` is treated as pre-`drafting` (ranks below it in `aggregate_mission_phase`; rendered in its own affordance, not the kanban). Revisit a sibling store only if proposed volume ever swamps the registry.
- **Proposed ≠ registered.** A proposed thread renders in a new **"Proposed" affordance** on the board with **accept** (→ flip `phase: proposed → drafting`, claiming the `task_id`) / **dismiss** (→ mark the orphan acknowledged so it stops re-proposing) buttons — both PR-backed via §4/§5.
- **Idempotent + fail-safe** (GC-healer posture): never re-propose an already-proposed/accepted/dismissed orphan; every indeterminate signal errs toward NOT proposing (no noise). Reuses the orphan-derive output, so there's one classification, no drift.
- **`cap-bidirectional-missions-board` — DECIDED (Larry, 2026-06-10): folded into this sequence.** Auto-registration (orphans → proposed threads the fleet can read) IS the concrete realization of "agents read the board to self-prioritize"; this sequence is its home, and the parked capture is retired (promoted) when the sequence kicks off.

**Scope guard:** auto-registration only *proposes* — it never auto-claims or auto-dispatches. Accept/dismiss is Larry's gesture (or a §8 trust-policy carve-out later).

---

## 7. Build sequence (single-repo split, interface-first)

One phase = one sequence (`missions-v2-phase3`), authored after this spec lands on `main`. Proposed DAG — agent-core (contracts/endpoints/healer) before dashboard (UI):

| Step | Repo | What | `depends_on` |
|---|---|---|---|
| `p3-capture-actions-api` | agent-core | `POST /api/missions/captures/{id}/action` (promote/drop/snooze) + `snoozed_until` schema + derive/digest honor snooze (§4) | — |
| `p3-mission-actions-api` | agent-core | `POST /api/system/missions/{id}/action` (defer/resume/reprioritize) + `priority` field (§5) | — |
| `p3-autoregister-healer` | agent-core | orphan→proposed-thread healer + proposed schema (§6) | `p3-capture-actions-api` |
| `p3-dashboard-writeback-ui` | dashboard | promote/drop/snooze + defer/resume/reprioritize controls (optimistic UI, PR-link toasts); digest card actions go live (§4–5) | `p3-capture-actions-api`, `p3-mission-actions-api` |
| `p3-dashboard-proposed-lane` | dashboard | "Proposed" affordance + accept/dismiss; Orphans lane shrinks (§6) | `p3-autoregister-healer`, `p3-dashboard-writeback-ui` |

- The two agent-core API steps are independent (parallel). UI steps gate on the frozen endpoints (interface-first).
- **Wire-and-prove (handoff §6.5):** new endpoints need a `dashboard-api` restart; the auto-register healer needs a systemd timer install+enable (verify — drift healers don't auto-install units, per the Phase 2 digest-timer lesson). Prove each on a live capture/mission/orphan.
- **Kickoff Beacon-mediated** (Mirror DAG-preflight, `--phase routing-signal`); Larry gates at the phase boundary.

---

## 8. Follow-on — the earned-autonomy ladder (separate, smaller sequence)

After write-back ships and **promotion-success data accrues**, a second sequence wires the autonomy dial (design pass §6, Doctrine #48):

- Promotion/accept actions consult **`scripts/trust_policy.py`** (`evaluate(task)` → `auto_approve` / `force_ask` / `reject`, first-match-wins, default `force_ask`). Larry adds rules to `config/trust-policy.json` so a **low-risk class auto-merges** (e.g. `registry-only` single-file edits: snooze, reprioritize, defer/resume) while everything else still asks.
- A **Pulse check** *proposes* widening the auto-approve rules from observed success rates (same self-optimizing pattern as Check III/VIII); Larry approves each widening. **Confidence is earned from data, not granted up front — the dial stays in his hand.**

Deferred to its own sequence because it must build on this sequence's real promotion/accept history; shipping it blind would be guessing at the rules.

---

## 9. Success criteria

1. Promote a parked capture → a PR opens creating the mission entry + setting `promoted_to`; on merge the capture leaves the Parked lane and the mission appears.
2. Drop a capture → PR sets `state: dropped`; GC moves it out of the active lane.
3. Snooze a capture until a date → it's suppressed from the digest + resurfacing until then, and not flagged `aging` while snoozed.
4. Defer / resume / reprioritize a mission → PR-backed; board reflects phase/order on merge.
5. The parked-&-aging digest card's promote/drop/snooze actions are live (Phase 2 shipped it read-only).
6. A non-terminal orphan auto-surfaces as a *proposed* thread; accept claims its `task_id` into a mission, dismiss stops re-proposing; the Orphans lane trends down.
7. No action auto-dispatches build work or auto-merges (until §8 carves out a class).

---

## 10. Decisions — ALL SETTLED 2026-06-10 (ready to author the sequence)

1. **Snooze write path (§4.3):** ✅ **direct via the capture-ingest committer** (not PR-backed) — avoids PR-spam for a trivial reversible field; one committer of `captures.json` preserved.
2. **Proposed-thread storage (§6):** ✅ **`missions.json` entry with `phase: "proposed"`** (one registry + derive, no sibling store).
3. **`cap-bidirectional-missions-board` (handoff §7):** ✅ **folded into this sequence** as the auto-registration driver; the parked capture is retired on kickoff.

No open decisions remain — the next step is to author + validate the `missions-v2-phase3` build-sequence file and run the Beacon-mediated kickoff (after this spec merges to `main`).

## 11. Cross-references

- Design pass: [docs/missions-redesign-design-pass-2026-06-09.md](../../../docs/missions-redesign-design-pass-2026-06-09.md) (§5 phasing, §6 autonomy ladder)
- Phase 2 spec (the derive + orphan-readability this builds on): `missions-v2-phase2-resurfacing-and-derive.md`
- Reuse anchors: `scripts/dashboard_api.py` (`_handle_new_mission` PR pattern, `_handle_larry_action`, `_handle_capture_ingest`), `scripts/trust_policy.py` (`config/trust-policy.json`), `scripts/heal_missions_card_gc.py` (healer pattern)
- Handoff: [docs/missions-v2-phase2-3-handoff.md](../../../docs/missions-v2-phase2-3-handoff.md) (§4 Phase 3, §7 open items)
