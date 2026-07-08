# Mirror Two-Slot Adversarial Review — spec

Status: CONFIRMED design, ready to build. The tier-pool prerequisite (§6) is
ALREADY MET — the dispatch wiring landed as #776/#778/#780/#784/#789/#793/#797
with cutover #802, and was verified live on the droplet 2026-07-08 (round-robin
alternating dispatch_tier=tier1/tier3, account-stamped costs, calibration timer
self-tuned from a real rate-limit wall). Code still lands inert (slots=1);
slot 2 turns on via the §6 rollout steps.

## 1. Problem (measured, 2026-07-08)

Mirror review is a strictly serial worker: one review at a time, enforced by the
single `inbox:mirror` lease (`scripts/dispatch_lease.py`, acquired in
`scripts/inbox_watcher.py:agent_loop`). A single review runs 15–75 min
(review-ceiling-fit digest: p99 2122–4576s over recent windows; the ceiling was
raised because 6 legitimate reviews were killed at 35 min).

With two generators (Larry desktop + droplet team) PRs arrive in bursts. Measured
2026-07-08: the overnight batch of 5 PRs (created 02:38–04:50Z) waited **3.5–11.5
hours** to merge; isolated afternoon PRs cleared in 10–45 min. This is
serialization latency, not throughput shortfall (~12–15 merges/day is well within
one slot's daily capacity — the queue-wait during bursts is the pain).

The Forge side is NOT the bottleneck: the second-team readiness trip-wire
(`ourliberty-readiness-trip-wire`) has read 0/8 signals on every tick — no
sustained build backlog, no concurrency saturation, no rate-limit pressure.

## 2. Non-goals

- **No second Forge lane.** Build capacity is fine (trip-wire cold). Adding it
  would only feed the review queue faster.
- **No cloned `mirror-2` agent identity.** A second agent id would duplicate the
  bot, inbox, routing-validator entries, and every healer that watches Mirror.
  This spec scales WORKERS within the one Mirror agent, not agent identities.
- **No regression-gate speedup here.** Cutting per-review service time (the gate
  is a large slice of the 35–75 min) is complementary, separate work.

## 3. Design (CONFIRMED)

N review slots consuming the SAME `~/agents/inboxes/mirror/` inbox, same agent
identity, same routing. N comes from config; default 1 (inert).

1. **Slot-indexed leases.** Generalize the lease identity from `inbox:mirror` to
   `inbox:mirror:<slot>` (`inbox:mirror:0`, `inbox:mirror:1`). `dispatch_lease`
   already keys everything off the identity string — TTL/heartbeat/PID-guard
   semantics are unchanged, one holder PER SLOT. Slot 0 keeps the legacy
   spelling `inbox:mirror` (compat: healers/tools that grep for it keep working
   during rollout; migrate them in the same PR — §4-PR2).
2. **Atomic task claim (the new primitive).** Today the single lease serializes
   the whole loop, so `scan_inbox` → `process_task` needs no claim step. With 2
   consumers both threads would scan the same `.json`. Claim = atomic
   `os.rename` of the task file into `~/agents/inboxes/mirror/.claimed/<slot>/`
   before processing; the loser's rename raises FileNotFoundError → it skips to
   the next task. Same-filesystem rename is atomic; no lock needed. On process
   death, orphaned `.claimed` files are re-queued by the existing
   startup/orphan-reap path (`reap_orphans_on_startup`) extended to sweep
   `.claimed/*` older than the session ceiling.
3. **Worker threads.** `inbox_watcher.agent_loop` gains a slot parameter;
   `main()` spawns `review_slots` threads for mirror (from
   `config/agent-models.json`, new per-agent key `"review_slots": 2`, absent =
   1). Other agents (forge, beacon, pulse) stay at 1 — the key is honored
   generically but only mirror sets it.
4. **Per-slot tier assignment.** Each concurrent review MUST run on a different
   credential tier or we halve each account's headroom instead of adding
   capacity. Slot dispatch calls `select_dispatch_tier()` (tier-dispatch spec
   W1) per task — round-robin across `{tier1, tier3}` naturally lands
   concurrent slots on different accounts. THIS IS WHY ACTIVATION IS GATED on
   the tier wiring (§6).
5. **Session/HOME isolation.** Two concurrent claude sessions under one HOME:
   transcripts are session-id-keyed (no collision), worktrees are task-keyed
   (`wt-mirror-<task_id>`, no collision). PR1 includes an audit item: grep the
   review path for any NON-session-keyed writable state under `$HOME`
   (`.claude.json` is the known offender class — see the EROFS/#470 history)
   and serialize or key any found.
6. **Ordering.** Mirror reviews are independent per PR; no cross-review ordering
   invariant exists (build-sequence ordering is enforced upstream by the
   advancer, not by review-queue position). `inbox_dispatch_order` stays as the
   scan order; slots may complete out of order — confirmed acceptable.

## 4. Work items (3 PRs)

- **PR1 — claim atomicity + slot plumbing (inert).** Rename-based claim dir +
  orphan sweep; slot-indexed lease identities; `review_slots` config key read by
  the watcher; threads spawned per slot. Ships with `review_slots` absent
  everywhere → 1 thread, behavior identical to today. Includes the $HOME
  shared-state audit (§3.5). Tests: two watcher threads against a fixture inbox
  never double-claim; orphaned claim re-queues; slot-0 lease spelling unchanged.
- **PR2 — make the watchers-of-Mirror slot-aware.** Enumerate and update
  everything that assumes ONE Mirror lease/session:
  `heal_wedged_review_sessions` (reap per-slot), the reaper/harvest path (the
  reap-before-harvest class — verify per-slot verdict harvest),
  `heal_review_ceiling_fit` (per-slot durations), `dispatch_sentinel` (its
  "single slot occupied" concept is Forge-scoped — verify and leave),
  duplicate-review dedup (#539 SHA-aware + #847 guard: two slots must never
  both review the same head SHA — the claim step covers the same-task case;
  add a same-PR-head guard at claim time for distinct task files targeting one
  head). Grep-driven: `grep -rn "inbox:mirror" scripts/` is the checklist seed.
- **PR3 — activation + observability.** Set `"review_slots": 2` for mirror;
  add `review_slot=<n>` and `dispatch_tier=<t>` to review start-lines; emit
  queue-wait (PR-open → review-start) per review into chain events; extend the
  readiness trip-wire (or a sibling gauge) with a Mirror queue-wait signal so
  the "do we need slot 3" decision self-fires instead of waiting for felt pain.
  PR3 does not merge until the §6 gate is met.

## 5. Hazards / interactions (address in the PR that touches them)

- **Droplet load.** Two concurrent regression gates ≈ 2× test-suite CPU. The
  box has had load incidents (gc overload class). PR3 verification includes a
  loaded-hour check; if load is a problem, the cheap valve is a
  max-one-concurrent-gate semaphore INSIDE the review (slots overlap on the
  LLM-bound phases, serialize the gate phase) — decide from measurement, not
  up front.
- **Quota.** Two concurrent Opus reviews ≈ 2× burn while overlapped. Covered by
  per-slot tier spread (§3.4) + the tier-pool proactive cap; do not enable
  slot 2 while the pool runs single-tier.
- **Lost-verdict class.** The .lost-result / reap-before-harvest fixes (#850,
  #857) are per-session and carry over per-slot; PR2 verifies the marker paths
  are slot-safe (no shared marker filename).
- **Global claude-process semaphore.** `scripts/concurrency_guard.py`
  (ConcurrencyGuard, shelf card `concurrency_guard`) caps TOTAL concurrent
  claude processes at 6, sized by RAM math (6×400MB on the 7.8GB VM). A second
  concurrent review permanently draws one more global slot at burst time,
  competing with Beacon/Forge/Pulse. PR3 verification: confirm guard headroom
  under 2-slot burst (guard `active_count` never pins at 6 / no acquire
  starvation for the bots). If tight, redo the RAM math before raising the
  ceiling — the guard deliberately refuses env-var raises.
  (Shelf consult also flagged `atomic_io` — reuse its write-temp-then-rename
  helper for the claim-dir sweep bookkeeping rather than hand-rolling.)

## 6. Activation gate + rollout

1. PR1, PR2 merge any time (inert; slot count still 1).
2. **Gate: MET (2026-07-08).** Tier-dispatch wiring W1–W4 landed
   (#776/#778/#780/#784, inbox gate #789, calibration #793, hardening #797,
   cutover #802) and verified live: rotation pin removed, round-robin
   alternating tier1/tier3 in same-day logs, session→tier map binding both
   tiers, costs.jsonl account-stamped, calibration self-tuned tier1's 5h budget
   from a real wall. Per-tier pool observability on GET /api/system/rotation is
   #876 (in review) — nice-to-have for the §5 quota watch, not blocking.
3. PR3 merges → `review_slots: 2`. First 48h: watch double-claim counter (must
   be 0), lost-verdict markers (0 new), droplet load, queue-wait trend.
4. Rollback = set `review_slots: 1` (or delete the key) + restart the watcher.
   One config line; no code revert needed.

## 7. Verification

1. Fixture: 6 queued reviews, 2 slots → both leases held concurrently, 6
   verdicts, 0 double-claims, 0 lost verdicts.
2. Live: burst of ≥3 PRs → observed review overlap (two `review_slot=` start
   lines with overlapping intervals, distinct `dispatch_tier`).
3. Queue-wait: burst p95 (PR-open → review-start) drops vs. the 2026-07-08
   baseline (3.5–11.5h → target <1.5h for a 5-PR burst).
4. Healer drill: kill one in-flight review → that slot's session reaped, task
   re-queued once (dedup honored), other slot undisturbed.

## 8. Success metric

Burst merge latency. Baseline 2026-07-08: 5-PR burst waited 3.5–11.5h. Target:
same-shape burst clears in <2h end-to-end, with per-PR review p99 unchanged
(we're removing queue-wait, not rushing reviews).
