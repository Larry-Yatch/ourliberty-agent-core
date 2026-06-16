# Spec: Missions v2 — Phase S: Two-way sync (close the loop)

**Status:** Draft — **set up / staged** (spec ready; dispatch when Larry says go)
**Author:** Claude Code (desktop session, 2026-06-15)
**Approver:** Larry
**Parent:** [docs/meaning-layer-roadmap.md](../../../docs/meaning-layer-roadmap.md) §4 Phase S
**Predecessor:** [Phase 4.1 — schedule the Narrator](missions-v2-phase4.1-narrator-schedule.md) (live) · [Delegate fix](missions-v2-delegate-fix.md) (delegate must work first — Phase S tracks what delegate spawns)
**Build path:** build-sequence orchestrator, multi-step; sequence when prioritized

---

## 1. Purpose

The board **drifts behind the work** — proven twice on 2026-06-14 (team merged; dashboard showed nothing until a manual run + restart) and again on 2026-06-15 (the board is full of promoted drafts nobody drove, plus a Proposed lane with ~50 unattended items; Larry: "it seems very stale and I have not been using it"). The cause is structural: **the board accumulates work-items but has no mechanism to reflect their real state or to clear them.** Phase S closes the loop so a card always reflects the true state of the work it represents, and the board drains itself.

**Done-gate:** a delegated/promoted card shows its spawned work's live status (building → in review); on verified merge a **safe** card auto-closes (with a "shipped in PR #X" note) and a **risky** card gets a plain-language team closeout for Larry to review before closing; a failed/blocked job rings back loudly; and the board no longer shows merged-but-invisible state.

---

## 2. Locked decisions (from the roadmap, restated)

- **Closed-loop both directions** — you→team (delegate/chat, fixed in the delegate spec) AND team→card (this phase): in-flight status, completion, failure all sync back.
- **Auto-close safe / closeout risky** — safe-risk work auto-closes its card on verified merge; risky work posts a **team-authored plain-language closeout** for Larry to review before closing.
- **No stale board** — engineer out the lag (push-emit over poll; remove the needs-a-restart / batched-commit / hourly-sync gaps); covers both the card↔work loop and raw infra freshness.
- **Cost on the card** — estimate up front, actual on closeout; Larry gates spend.
- **In-flight overrules a late pause** — a pause/snooze/drop never interrupts work already in flight; it applies after the work safely completes.

---

## 3. Contracts

### S1 — Link a card to the work it spawns
When a card is delegated (→ Beacon proposal) or promoted (→ mission), **stamp the spawned identity back**: the capture/mission records the `task_id`/`pr_url` of the work it created (extend the capture dict + mission entry with a `spawned` ref; surface it in the derive). This is the join key everything else rides on.

### S2 — In-flight status rides the existing derive
The linked work's phase (building / in-review / merged / failed) comes from the **same `chain_events` derive** that already powers the missions/orphan lanes (`/api/missions/derived`) — no new state machine. The card reads its linked work's derived phase and renders it.

### S3 — Completion: auto-close safe / closeout risky
On the linked work reaching **verified-merged** (the advancer/GC **belt-and-suspenders** gate: chain_events `auto_merge` AND `gh pr view MERGED`):
- **safe**-risk card → **auto-close** (state → `done`/retired) with a "shipped in PR #X" note. Reuse the GC healer's retire-on-merge pattern (single committer).
- **medium/careful**-risk card → the **Narrator authors a closeout briefing** (what we did · the outcome · anything to know) posted to the card; the card moves to a "review & close" state awaiting Larry's ack (or his Delegate/close gesture). Narrator extends to closeout (reuse the Phase 4.1 fold).

### S4 — Failure / blocked rings back
Linked work that **fails or blocks** (Forge failure, Mirror block, CLARIFY-exhausted, gate-mismatch) surfaces back on the card as a loud **blocked-on-you** doorbell (reuse `larry_alerts` + `alert_triage_state`), with the plain-English reason.

### S5 — No stale board (freshness)
- **Push-emit** work-state changes to the board surface instead of waiting on the dashboard poll / hourly sync.
- Remove the **merged-but-not-visible** gaps: `dashboard_api` changes that need a restart, captures.json briefings that wait for the GC commit, the hourly droplet sync. Make the board reflect a merge within one short cycle (target: well under the current ~hour).

### S6 — Cost on the card
The card shows the **estimated** cost of its delegated/promoted work up front (from the build-sequence `expected_cost_usd`) and the **actual** on closeout. Larry gates spend.

### S7 — In-flight overrules a late pause
A `pause`/`snooze`/`drop` on a card whose linked work is **in flight** does not interrupt it; the action is recorded and applied once the work reaches a safe stop (then the card reports back).

### S8 — Drain the existing stale board (one-time + ongoing)
Beyond the ongoing loop: a **one-time reconciliation** that closes already-merged drafts and surfaces the ~50 unattended Proposed-lane items for a batch accept/dismiss, so the board starts clean — and S3's auto-close keeps it that way.

---

## 4. Build plan (sequence when prioritized — staged, not yet dispatched)

| Step | Repo | Scope | depends_on |
|---|---|---|---|
| **S-1 link + in-flight** | agent-core | S1 spawned-ref stamping + S2 derive surfaces linked-work phase; tests | — |
| **S-2 completion** | agent-core | S3 auto-close safe / Narrator closeout for risky (GC retire-on-merge + belt-and-suspenders); tests | S-1 |
| **S-3 failure + cost + pause** | agent-core | S4 failure-rings-back doorbell, S6 cost capture, S7 in-flight-overrules-pause; tests | S-1 |
| **S-4 freshness** | agent-core + dashboard | S5 push-emit + kill the restart/sync lag; tests | S-1 |
| **S-5 board UI** | dashboard | render linked-work status, closeout, cost, "review & close" on the card; tests | S-1..S-3 |
| **S-6 drain** | agent-core | S8 one-time reconcile of stale drafts + Proposed-lane batch surface; tests | S-2 |

Multi-step; sequence linearly or by the DAG above when Larry greenlights. Each step ends at the operator-decidability gate (a human can act on the card), not just green tests.

## 5. Out of scope / later
- The near-real-time chat front desk → Phase 4b.
- Spreading the meaning layer to other lanes / shelf-ing the component → Phase 4.2.
- Full autonomy (team auto-handles safe items) → Phase 4.5 (rides S3's risk gate).
