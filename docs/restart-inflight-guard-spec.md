# Don't restart an agent-hosting unit mid-session — in-flight guard for the restarters

**Status:** spec, ready to build
**Owner:** `heal_stale_daemon_code.py` (+ 3 latent peers)
**Incident:** PR #971, 2026-07-21
**Related:** #854 / #911 / #979 (the orphan-claim class this *generates*)

## Summary

`heal_stale_daemon_code` restarts services whose shared libraries are newer than
their active-since timestamp. It does not check whether the unit is currently
running paid agent work. When it restarts `ourliberty-inbox-watcher.service`, any
live Claude review dies on SIGTERM and its claim file is stranded under
`.claimed/<slot>/`.

This spec adds **cordon → drain → restart → uncordon**: stop the unit accepting
*new* work, let the running session finish on its own, restart in the resulting
quiet window, then release. Nothing is killed and no work is lost. A bounded
ceiling remains, demoted to a backstop for the case where work never stops.

> **Revision (2026-07-21, Larry).** The first draft deferred the restart on a
> timer and retried next tick — which only ever *reduces the odds* of landing on
> a live review, never eliminates them, and gets worse as the queue gets busier.
> Cordon-and-drain removes the collision instead of dodging it. The mechanism
> already exists in `inbox_watcher` (see below); this spec now reuses it.

## Incident

2026-07-21, PR #971:

- `06:45:48Z` — `[mirror] start task=route-ourliberty-graph-prs-to-mirror-001`.
  The review spawns and runs normally.
- `06:50:46Z` — `heal_stale_daemon_code` issues
  `systemctl restart --no-block ourliberty-inbox-watcher.service`, reason:
  *"shared lib routing_validator.py newer than active-since by 348.0min"*.
- Same second — `inbox_watcher: received signal 15; shutting down`.
  Claude children exit `143` (128+15).
- The claim is stranded: SIGTERM killed `process_task` before its terminal
  archive, so the claim never left `.claimed/1/`.

Net cost: one killed Opus review (~5 min in), a stranded claim, and — because
recovery waited out the 45-minute grace — a ~40-minute stall on a PR that was
otherwise ready to merge.

Note the staleness had already persisted **348 minutes**. Waiting one more
10-minute tick for the review to finish would have cost nothing.

## Scope — where else does this happen?

Audited every script that issues `systemctl restart` or signals processes
(2026-07-21). Scripts that target agent-hosting units
(`inbox-watcher` / `forge-bot` / `mirror-bot` / `beacon-bot`) and do **not**
consult `state/in-flight`:

| script | targets agent units | in-flight aware | fired in last 7d |
|---|---|---|---|
| `heal_stale_daemon_code.py` | yes | **no** | **17 restarts** |
| `heal_pipeline_stall.py` | yes | **no** | 0 |
| `medic_actions.py` | yes | **no** | 0 |
| `heal_systemd_install_drift.py` | yes | **no** | 0 |
| `heal_claude_json_bind_drift.py` | yes | **no** | 0 |
| `watchdog.py` | yes | yes — already guards | n/a |
| `agent_runner.py` | n/a (the dispatcher) | yes | n/a |

All 17 agent-unit restarts in the last 7 days came from `heal_stale_daemon_code`
(8 beacon-bot, 4 inbox-watcher, 3 mirror-bot, 3 forge-bot), and 4 Claude children
were killed with exit 143 in that window — roughly one killed session every other
day. The other four are latent: same blindness, they simply have not fired
recently.

**Therefore the guard belongs in a shared helper, not inlined in one healer.**

## The predicate already exists

`watchdog.py` solved this for its own stall detection and its reasoning is
battle-tested. Reuse it rather than reinventing:

- `_any_live_in_flight_session()` (`watchdog.py:410`) — any
  `state/in-flight/*.json` with a live pid.
- `_any_live_dispatch_lease()` (`watchdog.py:~444`) — any held+live
  `inbox:<agent>` lease.

Both are needed. The docstring at `watchdog.py:454` records why the in-flight
marker alone is **not** sufficient: a Mirror review reuses the Forge build's
`task_id` as its `task_stem`, so both write the same
`state/in-flight/<stem>.json`, and the build's `_unregister_in_flight` (keyed on
stem only) can delete the review's marker while the review is still live. The
per-agent lease is clobber-immune — keyed by agent, renewed on a 60s heartbeat,
counted only within TTL with a live holder pid.

For the #971 kill specifically, the `inbox:mirror` lease was held at 06:50:46Z,
so the lease signal alone would have caught it.

### Change

Extract both predicates into a new shared module — `agent_work_in_flight.py`,
matching the `mirror_review_conclusion` / `dispatch_lease` shared-predicate
pattern — exposing:

```python
def agent_work_in_flight() -> bool:
    """True iff any agent session is live: a live in-flight marker pid, OR a
    held+live inbox:<agent> dispatch lease. Both, because the marker is
    clobberable for Mirror reviews and the lease is absent in lease-'off' mode."""
```

`watchdog.py` then imports it in place of its two private copies (behavior
identical — this must be a pure refactor, asserted by watchdog's existing tests
continuing to pass untouched).

Restarters consult it before restarting an **agent-hosting** unit only. A
dashboard-api or deploy-notifier restart is unaffected; those host no agent
sessions and must stay immediate.

## Cordon-and-drain — the primary path

### The mechanism already exists

`inbox_watcher` already implements "stop taking new work, let in-flight finish"
for a different trigger: tier-pool exhaustion. `_rotation_gate_block_reason`
(`inbox_watcher.py:1525`, called at `:1061`) blocks NEW top-level dispatches
while explicitly passing continuations through, and its contract is exactly what
a cordon needs:

> *the task stays in the inbox; the next poll re-evaluates. No archive, no
> write_invalid — drain must NEVER drop work, only delay it.*

So the switch is not new machinery, it is a second reason to throw a switch that
is already trusted in production. That materially lowers the risk of this change.

### Sequence

1. **Cordon** — healer writes `state/restart-cordon/<unit>.json`
   (`{"unit", "reason", "created_at", "expires_at", "pid", "boot_id"}`).
2. **Watcher honors it** — the dispatch gate blocks NEW top-level tasks for the
   cordoned unit's agents. Continuations pass, exactly as under the rotation
   gate, so a `--resume`/revision round is never orphaned mid-chain.
3. **Drain** — healer polls `agent_work_in_flight()` (below) until it reads
   False, i.e. the running session ended by itself.
4. **Restart** — `systemctl restart` into a window where nothing is running and,
   because the cordon is still up, nothing new can start. **No race**: the
   ~5-second gap between one review ending and the next claim is not something
   we try to hit — the cordon holds it open.
5. **Uncordon** — remove the file. The new process resumes normally.

### Why the gap needs the cordon

Without step 1, this design would be a race we lose: the watcher polls every
`POLL_INTERVAL_SEC` (5s), so the idle window between sessions is seconds wide,
and a healer that wakes every 10 minutes cannot reliably land in it. The cordon
converts "hit a moving 5-second target" into "close the road, then work."

### Implementation note — where to block

The rotation gate blocks inside `process_task`, i.e. *after* `_claim_task`, so
the loop's `finally` un-claims and the task returns to the inbox each poll.
Correct but churny: a cordon held for minutes would claim/un-claim every 5s.
Prefer blocking **before** the claim (in `scan_inbox`'s filter or immediately
ahead of `_claim_task`) so a cordoned slot never touches the file at all. This
also keeps `.claimed/` quiet, which matters because a claim file appearing and
vanishing on a 5s cycle is exactly the noise that made #971 hard to read.

## The cordon must expire on its own (the new top risk)

Cordon-and-drain moves the danger rather than removing it. **If the healer dies
between cordon and uncordon — crash, SIGKILL, the very restart storm this healer
participates in — the cordon persists and the queue silently stops accepting
work.** That is a worse outage than the killed reviews we are fixing: total
dispatch stall, no alarm, looks like a quiet queue.

Non-negotiable mitigations, all three:

1. **TTL in the file.** `expires_at` = created + `CORDON_TTL_SEC` (default
   **600s**, comfortably past a normal drain). The watcher treats an expired
   cordon as absent — a dead cordon cannot outlive its TTL by even one poll.
2. **Holder liveness.** The cordon records `pid` + `boot_id`; the watcher ignores
   a cordon whose holder is not alive on the current boot. Same test
   `dispatch_lease.is_held` already applies to leases. This clears a dead
   cordon in seconds rather than waiting out the TTL.
3. **Unconditional release.** The healer writes the cordon in a `try` and removes
   it in `finally`, so every non-fatal path releases.

Fail-open in every direction: an unreadable, malformed, expired, or
dead-holder cordon means *not cordoned*, which degrades to exactly today's
behavior. A cordon must never be able to stop the queue through corruption.

## The ceiling, demoted to a backstop

With cordon-and-drain the normal case needs no timer — the restart happens as
soon as the current session ends, which may be two minutes or thirty.

`RESTART_DEFER_CEILING_SEC` (default **3600s**) now covers only the pathological
case: work that never stops, so the drain poll never reads False. Past it,
restart anyway and log loudly:

```
RESTART_FORCED_OVER_CEILING unit=<u> drained_for=<n>min reason=<staleness>
```

Rationale for 1h: a single review cannot exceed the 35-minute
`agent_runner.REVIEW_SESSION_CEILING_SECONDS`, so one session can never exhaust
it — only a genuinely continuous queue can, which is precisely when a human
should see a forced restart in the log. Because this is now a backstop rather
than the mechanism, the exact value matters much less than it did in the first
draft.

Deferral/drain state is advisory — missing or unparseable means "not currently
deferred", failing toward restart-now. It must never *block* a restart through
corruption.

## Logging

Every deferral logs at INFO with the reason, so a restart that visibly did not
happen is explicable:

```
RESTART_DEFERRED unit=<u> reason=agent-work-in-flight signal=<lease|marker>
    staleness=<what triggered it> deferred_for=<n>min/<ceiling>min
```

## Test plan

New `scripts/tests/test_agent_work_in_flight.py`:

1. live in-flight marker pid → True; dead pid → False
2. held+live `inbox:<agent>` lease → True; lease past TTL → False; lease with
   dead holder → False
3. marker absent but lease held (the Mirror-clobber shape) → True — the case
   that makes both signals necessary
4. lease-'off' mode with a live marker → True (degrades to marker alone)
5. neither → False; unreadable/half-written marker or lease → skipped, never
   counted

New `scripts/tests/test_restart_cordon.py` — the cordon's fail-open contract is
the highest-consequence surface in this change, so it gets its own file:

6. cordon present + live holder + unexpired → watcher blocks a NEW top-level
   task; the task **stays in the inbox** (not archived, not `.invalid`)
7. cordon present → a **continuation** (`--resume` / `phase=build|revision`)
   passes through, so a revision round is never orphaned mid-chain
8. cordon **expired** (`expires_at` in the past) → ignored, dispatch proceeds
9. cordon whose **holder pid is dead** (or a different boot) → ignored
10. cordon file unreadable / malformed / empty → ignored, dispatch proceeds
11. healer raises mid-drain → `finally` removes the cordon (no persistent stall)
12. cordoned slot never calls `_claim_task` — asserts the block sits ahead of the
    claim, so `.claimed/` stays quiet across a long cordon

`scripts/tests/test_heal_stale_daemon_code.py` (extend):

13. stale unit + agent work in flight → cordon written, **no** restart yet
14. work finishes → drain observes it, restart fires, cordon removed
15. stale unit + no agent work → restarts immediately (today's behavior for the
    common case, unchanged — no cordon, no drain wait)
16. stale **non-agent** unit (e.g. dashboard-api) + agent work in flight →
    restarts anyway; cordon-and-drain is scoped to agent-hosting units
17. drain never clears → past `RESTART_DEFER_CEILING_SEC` restarts and logs
    `RESTART_FORCED_OVER_CEILING`, cordon still removed afterward

`scripts/tests/test_watchdog.py`: existing tests pass **untouched** — proof the
extraction is a pure refactor.

Full suite, diffed as a set against the untouched parent (same names, not just
the same count).

## Rollout

Three PRs, smallest blast radius first:

1. **Shared predicate + watcher cordon support.** `agent_work_in_flight.py`
   extracted from `watchdog.py`, plus the watcher honoring a cordon file. No
   healer writes one yet, so this lands **inert** — the cordon path is exercised
   only by tests until step 2. Any bug here cannot stall the queue, because
   nothing creates a cordon.
2. **`heal_stale_daemon_code` cordons and drains.** The only restarter firing
   today, so it carries all the observed benefit and all the live risk.
3. **The four latent peers** (`heal_pipeline_stall`, `medic_actions`,
   `heal_systemd_install_drift`, `heal_claude_json_bind_drift`) after step 2 has
   soaked — so a bug in the predicate cannot take out five restart paths at once.

Splitting 1 from 2 matters specifically because of the stuck-cordon risk: the
consumer ships and soaks before anything can produce a cordon in production.

## Blast radius / rollback

- Predicate wrongly True → cordon + drain wait, then force through at the
  ceiling. Staleness persists at most 1h longer than today; logged.
- Predicate wrongly False → today's behavior exactly (restart now).
- **Cordon stuck on** → the one genuinely bad outcome: dispatch stalls. Bounded
  three ways (TTL, holder liveness, `finally`), each independently sufficient,
  and every ambiguity resolves to "not cordoned."
- Rollback without reverting code: `CORDON_TTL_SEC=0` makes every cordon
  instantly expired (watcher ignores all cordons → today's behavior);
  `RESTART_DEFER_CEILING_SEC=0` forces every restart through immediately.

## Out of scope

- Graceful drain on SIGTERM (the watcher finishing its session instead of dying).
  Cordon-and-drain makes this mostly moot for the *planned* restart path, but it
  would still help for unplanned kills — deploys, OOM, operator `systemctl`.
  Larger change to `inbox_watcher` signal handling; worth revisiting if kills
  keep showing up from paths this spec does not cover.
- Why `routing_validator.py` staleness sat 348 minutes before triggering.
- Cordoning for *operator* restarts (a human running `systemctl restart`). The
  same file would work — `scripts/cordon.sh <unit>` as a pre-flight — but that is
  a workflow change, not a healer fix.
