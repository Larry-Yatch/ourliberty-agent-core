# Spec: Pulse Check IX — operator-friction signal

**Status:** Draft (awaiting design pass)
**Author:** Forge (drafted 2026-05-28 from operator-UX backlog dispatch)
**Approver:** Larry (pending)
**Predecessors:** Check I (optimization-mode digest), Check III (threshold self-tuning), Check VIII (burn-rate signal) — all live patterns Check IX mirrors.

---

## 1. Purpose

Larry's vision for the operator-UX backlog: Pulse cycles surface UX findings **automatically**, not just on-demand via Beacon conversation. Check IX scans operator-behavior signals during Pulse's weekly cycle, detects friction patterns, and registers each finding as a `phase: drafting` mission in `agents/beacon/missions.json` via the existing `POST /api/system/missions/new` endpoint (from E4.4f's Missions tab PR-A).

The check is **self-bootstrapping**: had Check IX existed before the bootstrap-003 verifier ran, it would have flagged the skip-window UX gap from the verifier's findings without Larry having to catch it manually. The check's own outputs (drafting missions) feed back into the kanban, where Larry's promotion-to-ready decision is the human gate.

---

## 2. Signals to scan

Each signal is a weekly aggregate computed from logs and ledgers, with a threshold above which a drafting mission is registered. Threshold values are starting points and become tunable per the Check III self-optimization pattern (see § 8).

### 2.1 Catch-me-up gap signal

- **Source:** `~/agents/logs/beacon_telegram_bot.log` (or whichever log file beacon-bot writes to in the current deployment).
- **Match patterns:** case-insensitive grep for variants of `status`, `what is happening`, `what's happening`, `is everything continuing`, `any update`, `where are we`, `where are things`. Each match is a "status-probe" event.
- **Threshold:** more than 3 status-probe events in the trailing 7 days → register a drafting mission proposing the catch-me-up shortcut (or, if `operator-ux-catch-me-up-shortcut` is still in `drafting`, append evidence to its brief rather than registering a duplicate).

### 2.2 Time-to-action gap signal

- **Source:** `chain_events` Supabase table — pair `step-dispatched` events with subsequent `step-merged` or operator-shortcut events (e.g. `skip sequence X step Y`).
- **Metric:** for each step, compute `time-to-action = (operator-shortcut-OR-step-merged) - step-dispatched`.
- **Friction event:** a step whose time-to-action exceeds `2 × rolling-median(time-to-action)` AND has no operator action within the same window.
- **Threshold:** more than 5 friction events in the trailing 7 days → drafting mission.

### 2.3 Alert-ignored signal

- **Source:** `larry_alerts` ledger.
- **Metric:** count alerts where the same `subject` fired N times within 7 days with no commit reference and no operator shortcut in the same window.
- **Threshold:** the same subject ignored at least 3 times in 7 days → drafting mission citing the ignored subject.

### 2.4 Out-of-chain-rescue burden signal

- **Source:** `~/agents/logs/outbox-notifier.log` (or whichever file the notifier writes to).
- **Match:** count `intent=clarification-exhausted` events per 7-day window.
- **Threshold:** 2 or more in the trailing 7 days → drafting mission proposing tightening on the dispatch pattern that drove the exhaustion (e.g. spec-template gaps).

---

## 3. Schema

Check IX writes drafting mission entries with this shape (consistent with the existing `missions.json` schema, schema_version: 1):

```json
{
  "id": "pulse-check-ix-<signal-shortname>-<YYYY-MM-DD>",
  "name": "<one-line headline for the friction>",
  "phase": "drafting",
  "brief": "<finding summary> | Evidence: <N events, period> | Suggested fix shape: <1-sentence>",
  "spec_docs": [],
  "task_ids": [],
  "repo": "ourliberty-agent-core",
  "created": "<cycle date>",
  "deferred_reason": null
}
```

- `id` is auto-generated and includes the cycle date for collision-avoidance.
- `signal-shortname` is one of: `catch-me-up-gap`, `time-to-action-gap`, `alert-ignored`, `rescue-burden`.
- `brief` is structured so a downstream operator can read it without re-running the check.
- Registration goes through `POST /api/system/missions/new` — the existing endpoint Larry already uses from the Missions tab + New modal.

**Idempotency:** before POSTing, Check IX queries `GET /api/system/missions` for an existing entry whose `id` starts with `pulse-check-ix-<signal-shortname>-` and whose `phase` is still `drafting`. If found, it appends evidence to that entry's `brief` (via a follow-up PATCH if available, or skips registration if PATCH isn't available — first-cycle behavior is registration, later cycles update). This prevents weekly cycles from spawning duplicate cards.

---

## 4. Cadence

- Runs **weekly**, gated alongside Check VIII (Mondays).
- Same cadence-gate pattern as Check I and Check VIII: the cycle prompt checks a sentinel timestamp and skips Check IX if it ran within the last 6 days.
- Like Check VIII, output is structured for operator review — Larry reviews drafted missions on the kanban, promotes to `ready` or rejects.

---

## 5. Acceptance

- A synthetic friction event (e.g. operator asks `status` 5× in a day) triggers a drafting mission entry next cycle.
- Existing Check I / Check III / Check VIII behavior is unchanged (Check IX is additive, gated independently).
- Idempotency holds: re-running Check IX within the same week does not register duplicate missions.
- Each registered mission carries the evidence count in its brief (e.g. `"Evidence: 5 status-probe events over trailing 7d"`).

---

## 6. Out of scope

- Auto-dispatching the proposed fix — Larry reviews drafted missions; the manual promote-to-ready step preserves the human-in-the-loop discipline.
- Cross-operator friction signals (single-operator system today).
- Synthetic-event injection for testing — covered by a separate test fixture, not a runtime behavior.
- Friction signals on agent-internal behavior (those go into Pulse's existing Checks A–H).

---

## 7. Cost estimate

Single weekly analyzer dispatch: ~$8–10. Mirror revisions expected 0–1 (the check is self-contained and Mirror's review focuses on signal logic + idempotency).

---

## 8. Followups

- Each Check IX signal becomes its own threshold-tunable per the Check III self-optimization pattern. Once the system has 8 cycles of Check IX data, Check III can propose threshold updates on the four signals above.
- Signal additions are easy: new signal = new entry in the cycle prompt with its own source, metric, threshold, and registration template. Document the signal taxonomy in `runbooks/pulse-check-ix.md` when it lands.
- If alert-ignored or rescue-burden signals consistently fire on the same root cause, Check III may propose deprecating the underlying alert/dispatch shape rather than tuning thresholds.

**Enforcement:** Mirror review checklist item — confirm § 3 idempotency check is implemented (not just documented) before approving, since weekly duplicate cards would clog the Missions kanban quickly.
