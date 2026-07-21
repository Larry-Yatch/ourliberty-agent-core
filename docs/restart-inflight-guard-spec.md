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

This spec adds a pre-restart guard: if an agent-hosting unit has live work,
defer the restart to the next tick — up to a bounded ceiling, after which
staleness wins and the restart proceeds.

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

## The deferral ceiling (the real design tension)

An unbounded defer is its own outage: if reviews run back-to-back, a stale unit
never restarts and the staleness the healer exists to fix persists forever. The
guard therefore needs a ceiling.

Proposed: defer while work is in flight, up to `RESTART_DEFER_CEILING_SEC`
(default **3600s**, ~1h), tracked per unit in
`state/restart-deferrals/<unit>.json` (first-deferred timestamp + count). Past the
ceiling, restart anyway and log loudly:

```
RESTART_FORCED_OVER_CEILING unit=<u> deferred_for=<n>min reason=<staleness>
```

Rationale for 1h: the review session ceiling is 35 min
(`agent_runner.REVIEW_SESSION_CEILING_SECONDS`), so a single review can never
exhaust it; only a genuinely continuous queue can, and that is exactly the case
where a human should see the forced restart in the log.

Deferral state is advisory — a missing or unparseable file means "not currently
deferred", which fails toward the current (restart-now) behavior. It must never
be able to *block* a restart through corruption.

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

`scripts/tests/test_heal_stale_daemon_code.py` (extend):

6. stale unit + agent work in flight → **no** restart, deferral recorded, INFO
   logged
7. stale unit + no agent work → restarts (today's behavior, unchanged)
8. stale **non-agent** unit (e.g. dashboard-api) + agent work in flight →
   restarts anyway; the guard is scoped to agent-hosting units
9. deferred past `RESTART_DEFER_CEILING_SEC` → restarts, logs
   `RESTART_FORCED_OVER_CEILING`
10. corrupt/missing deferral-state file → treated as not-deferred, restart
    proceeds (fails toward current behavior)

`scripts/tests/test_watchdog.py`: existing tests pass **untouched** — proof the
extraction is a pure refactor.

Full suite, diffed as a set against the untouched parent (same names, not just
the same count).

## Rollout

Land the shared helper + `heal_stale_daemon_code` first — it is the only one
firing today, so it carries all the observed benefit and all the risk. The four
latent peers (`heal_pipeline_stall`, `medic_actions`,
`heal_systemd_install_drift`, `heal_claude_json_bind_drift`) follow in a second
PR once the guard has soaked, so a bug in the predicate cannot take out five
restart paths at once.

## Blast radius / rollback

- Worst case if the predicate wrongly returns True: restarts defer up to the
  ceiling, then force through. Staleness persists at most 1h longer than today,
  and the forced restart is logged.
- Worst case if it wrongly returns False: today's behavior exactly.
- Rollback: set `RESTART_DEFER_CEILING_SEC=0` (every deferral immediately past
  ceiling → restart now) without reverting code.

## Out of scope

- Graceful drain (letting the watcher finish the current session on SIGTERM
  instead of killing it). That is the more complete fix and a much larger change
  to `inbox_watcher`'s signal handling; deferring the restart gets most of the
  benefit for a fraction of the risk.
- Why `routing_validator.py` staleness sat 348 minutes before triggering.
