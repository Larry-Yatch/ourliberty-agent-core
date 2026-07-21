# Orphaned Mirror claim — lease-proven fast path (skip the 45-min grace)

**Status:** spec, ready to build
**Owner:** heal_orphaned_mirror_claims.py
**Incident:** PR #971, 2026-07-21
**Related:** #854 (orphan class), #911 (the healer), #923 (round-aware conclusion)

## Summary

When a Mirror review claim is stranded under `.claimed/<slot>/`, recovery today
waits out a blunt 45-minute timer (`ORPHAN_CLAIM_GRACE_SEC`) before the healer
will touch it. In a large and cheaply-detectable subset of cases the claim is
**provably** not owned by any dispatch the moment the healer looks at it, and
the wait buys nothing. This spec adds a fast path for exactly that subset:
skip the age floor when the slot's dispatch lease is free.

Expected effect: stranded reviews recover on the next 10-minute tick instead of
at the 45–55 minute mark. On the #971 incident that is ~40 minutes of dead time
removed from a PR that was otherwise ready to merge.

## Incident that motivated it

PR #971's Forge review was never dispatched. `heal_undispatched_pr_review`
correctly caught the orphan and dispatched a backstop review at 06:45:43Z. The
claim file landed in `inboxes/mirror/.claimed/1/` and **never spawned**.

The owning `inbox_watcher` process died between the claim rename and the
terminal archive — journal shows pid `122269` serving the queue at 06:39Z and
pid `324553` at 07:00Z. This is the documented #854 watcher-death class, not a
new one.

What made it hard to see: the **replacement** watcher took over slot 1 and ran
two other reviews (`pr-ourliberty-dashboard-138`, `-139`) to completion. The
slot was never head-of-line blocked, so nothing looked wedged and no alarm
fired. The healer logged `scanned=N cleared=0 spared=N` throughout — `spared`
because the claim was inside the grace window, which reads identically to
"healthy, nothing to do."

Recovery was correct but slow: the claim would have been re-injected at the
45-minute mark. It was reinjected manually at ~37 minutes; Mirror then reviewed
clean and the PR merged.

## Why the grace can be skipped safely

The 45-minute floor exists because two of the healer's liveness guards read
false during the claim→spawn window: a slot claims a task by `os.rename`
**before** the claude process and the in-flight registry entry exist, and
`os.rename` preserves mtime, so a backlogged envelope claimed just now can
already look old. The grace is the blunt cover for that window.

But the slot's dispatch lease already covers it precisely. In
`scripts/inbox_watcher.py`:

- `1606` — `dispatch_lease.try_acquire(identity)` — the lease is acquired
  **before** any claim is made
- `1622` — `_claim_task(...)` — the claim rename happens while holding it
- `1667` — `dispatch_lease.release(identity, nonce)` — in `finally`, after
  `process_task` returns

This yields the invariant the fast path rests on:

> A claim file can only be created, and can only be legitimately in progress,
> while its slot's lease is held.

`dispatch_lease.is_held` is an exact liveness predicate — lease file present,
within TTL, holder pid alive on the current boot — and is strictly read-only
(never acquires, reclaims, or signals). So **claim present + lease not held**
means no dispatch owns that claim: not spawning, not running. The age floor is
protecting against a state that cannot exist.

Note this holds for the whole review, not just the spawn window: the lease is
held for the entire `process_task` call, so a long-running live review keeps
its slot lease and is never a fast-path candidate.

## Change

In `heal_orphaned_mirror_claims.scan_agent`, at the age check
(`heal_orphaned_mirror_claims.py:418`), allow a provably-orphaned claim past
the floor.

The slot-lease probe already runs once per slot at `:407` and *defers the whole
slot* when held. Reuse that same result rather than probing per claim: if the
healer reached the per-claim loop at all, the slot lease was free. Capture it
as a local (e.g. `slot_lease_free`) and pass it into the age test:

```python
# Age floor, UNLESS the claim is provably orphaned: the slot lease is free.
# The lease is acquired before _claim_task and released only after
# process_task returns (inbox_watcher.py:1606/1622/1667), so a claim with no
# held lease is owned by no dispatch — spawning or running. Requires leases
# to be ON: in 'off' mode is_held is vacuously False and would fast-path
# every fresh claim.
provably_orphaned = slot_lease_free and dispatch_lease_enabled()
if age < ORPHAN_CLAIM_GRACE_SEC and not provably_orphaned:
    spared += 1
    continue
```

Guards that **must not** change:

1. `live_worktree_process` and `has_live_in_flight` still run, and still run
   *before* any conclusion signal. The fast path only removes the age floor; it
   must never remove a liveness check.
2. A **non-numeric** slot dir (`slot is None`) cannot resolve a lease identity.
   The grace stays mandatory there — no lease result means no proof.
3. Round-aware conclusion, PR-state tri-state, and the spare-on-UNKNOWN
   behavior are untouched.

### The `mode() == 'off'` trap

`dispatch_lease.is_held` returns `False` unconditionally when
`GM_DEDUP_USE_LEASES=off`, because no lease files are written in that mode.
Without an explicit gate, the fast path would then treat *every* freshly
claimed task as provably orphaned and re-inject it out from under a live
dispatch — a paid duplicate Opus review on every claim. Default is `shadow`
(leases written), but the gate is not optional:

```python
def dispatch_lease_enabled() -> bool:
    """Fast path requires real leases. In 'off' mode is_held is vacuously
    False, which would make every fresh claim look orphaned."""
    try:
        import dispatch_lease
        return dispatch_lease.mode() != 'off'
    except Exception:
        return False
```

Fails closed: any import/probe error → no fast path → current 45-min behavior.

### Logging

Fast-path actions must be distinguishable in the log, since the whole point is
that they fire earlier than operators expect. Add a marker to the existing
`reinject_orphan` / `archive_orphan` lines, e.g. `via=lease-proven`, and keep
`age_min` so a 2-minute recovery is visibly a fast path and not a clock bug.

## Test plan

`scripts/tests/test_heal_orphaned_mirror_claims.py`, extending existing fixtures:

1. **Fast path fires** — fresh claim (age ≪ grace), lease free, no live
   process, no in-flight, PR OPEN, not concluded → re-injected this tick.
2. **Lease held blocks it** — same, but slot lease held → spared (already
   covered by the slot-defer path; assert it still short-circuits before the
   age test).
3. **Live process still wins** — fresh claim, lease free, but a live claude cwd
   in `wt-mirror-<task_id>` → spared. Guards ordered correctly.
4. **Live in-flight still wins** — same with a signalable pid in the registry.
5. **`mode()=='off'` disables the fast path** — leases off, fresh claim → spared
   until the grace expires. Regression test for the trap above.
6. **Non-numeric slot dir** — fresh claim under `.claimed/spare/` → spared;
   grace still enforced when no lease identity resolves.
7. **Aged claim unchanged** — every existing test still passes untouched; the
   fast path only adds a way past the floor, never a new action.

Full suite: `python3 -m unittest discover -s scripts/tests`.

## Blast radius / rollback

- One file (`heal_orphaned_mirror_claims.py`) plus tests. No systemd, config,
  or schema change; timer cadence unchanged at ~10 min.
- Actions taken are the same two that exist today (re-inject / archive) with
  the same preconditions — only the *timing* changes.
- Worst case if the invariant is wrong: a duplicate Opus review (~$0.60) on a
  claim that was actually live. The three surviving guards each independently
  prevent this; `mode()=='off'` is the one path that could defeat them all and
  is explicitly gated.
- Rollback: set `OL_ORPHAN_CLAIM_GRACE_SEC` high and revert the commit; no
  state migration.

## Out of scope

- The `spared=N` log line reading identically for "healthy" and "stranded but
  waiting." Worth a follow-up: `spared` could break out a `waiting_on_grace`
  count so a stranded claim is legible in the heartbeat instead of invisible.
- Why `heal_undispatched_pr_review`'s backstop dispatch orphaned in the first
  place (the watcher restart at ~06:45Z is unexplained here).
