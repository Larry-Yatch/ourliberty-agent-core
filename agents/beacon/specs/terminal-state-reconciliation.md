# Spec: Terminal-State Reconciliation — no in-flight record outlives its work

**Status:** ready for build
**Author:** Beacon
**Date:** 2026-06-14
**Origin:** A system-wide scan (2026-06-14) for the "phantom-bookkeeping" hole that produced recurring stale approvals, a 128-entry proposed lane, a sequence that read "stuck," and a CEO digest that re-surfaced an already-fixed issue. Six instances of one shape were found.

## 1. The pattern + the invariant (locked)

The system keeps many **in-flight bookkeeping records** — pending approvals, dispatched alert-triage rows, mission phases, build-sequence steps, in-flight dispatch sentinels. Each is retired only by a **linking event**: a human action (Larry taps approve), an *exact* task_id match, or the trigger that starts the next step. None reconcile against the work's **terminal ground truth** (PR merged/closed, process exited). So when the linking event is missed — a recovery re-emit (`Go: <task_id>`), a re-dispatch under a variant id (`-002`), a `resolve()` that did not fire, a merge attributed to a different id — the record becomes a permanent **phantom**: it fires reminders/alerts forever, shows "blocking" on the dashboard, or floods a lane.

**Invariant:** No in-flight bookkeeping record may outlive its work's terminal state. For every store that tracks work as `pending`/`dispatched`/`active`/`proposed`/`in_flight`, there MUST be a reconciler that — past a grace window — checks the work's terminal ground truth and retires the record when terminal. **Conservative posture (non-negotiable):** an OPEN or UNKNOWN/indeterminate signal ⇒ KEEP. Never falsely retire live work. This backstop is ADDITIVE — it does not replace the happy-path linking event, it catches the cases the linking event missed.

**Enforcement:** §6.

## 2. The shared mechanism — `task_terminal_state` (DRY)

A proven tri-state `gh` probe already exists, duplicated across `heal_unregistered_approval.py` (`gh_ref_resolved`), `heal_recovery_already_merged.py` (`query_merged_pr`), and `build_sequence_advancer.py` (`gh_pr_says_merged`). Extract ONE shared helper (suggested `scripts/task_terminal_state.py`):

```
task_terminal_state(task_id: str, variants: list[str] = []) -> "MERGED" | "CLOSED" | "OPEN" | "UNKNOWN"
```

- Queries merged/closed/open PRs by `task_id` in branch/title PLUS known re-dispatch variants (`<id>-001`, `<id>-002`, `fix-<id>-revisions-*`, `<id>-redispatch-*`).
- Returns **UNKNOWN** on any `gh` error, timeout, or ambiguity — never guesses terminal.
- Stdlib + `gh` only; bounded timeout; no Supabase dependency.

All reconcilers in §3 consume this single helper. The three existing ad-hoc probes are refactored to call it (no behavior regression).

## 3. The six holes (each consumes §2)

### 3.1 Pending approvals
Extend `scripts/heal_stale_approvals.py`: for each entry in `beacon-pending-approvals.json` `pending[]` older than a grace (e.g. 2h), call `task_terminal_state(dispatch_payload.task_id, variants)`. If MERGED/CLOSED ⇒ `beacon_approval_handler.resolve(id, 'expired', note=...)` and clear the dashboard row. This adds the missing ground-truth check to the one reconciler that today is *forbidden* to touch `pending`.

### 3.2 Alert-triage rows stuck `action-dispatched`
New reconciler (or extend an existing healer): for each `~/agents/state/alert-triage.json` row in `status==action-dispatched` past a grace, probe `task_terminal_state(dispatch_task_id)`. If MERGED/CLOSED ⇒ `mark_resolved`. (Five rows are stuck 10+ days today.)

### 3.3 Missions in `drafting`/`in_flight`/`ready`
Extend `scripts/heal_missions_card_gc.py`: a mission in a non-`proposed`, non-terminal phase whose every `task_ids[]` entry is terminal ⇒ flip phase to `shipped` (audit-preserved). **Do NOT touch `proposed`** — that lane is owned by the in-flight `missions-proposed-lane-signal-hardening-001` PR.

### 3.4 Sequence `dispatched` steps
Operationalize the existing V6 `_reconcile_dispatched_steps`: (a) ensure it runs independent of the default-OFF advancer flag (a dedicated reconcile pass/timer, or default the flag on with care), (b) widen the merged-PR lookback beyond the current 20, (c) scan `paused` sequences for terminal steps. Reuse §2.

### 3.5 In-flight steady-state
Give `scripts/dispatch_sentinel.py` (or a continuous twin of `inbox_watcher.reap_orphans`) a `pid`-alive + `task_terminal_state` check BEFORE age-alerting an `~/agents/state/in-flight/*.json` entry: dead pid OR merged PR ⇒ reconcile/forfeit instead of nag. Today pid is reconciled only at watcher boot.

### 3.6 CEO digest — don't re-surface already-fixed issues
The daily/weekly CEO digest (`ceo_digest_generator`) summarizes recent signal (alerts, G-rules, recurring problems) over a lookback window and presents "needs-your-call" items. It does NOT check whether an item's fix has already shipped, so a resolved issue re-surfaces as if open. Confirmed incident: on 2026-06-14 the digest surfaced the Check-0 watermark loss as a "recurring problem still needs your call" — but its fix had merged as PR #482 on 2026-06-12. Extend the generator so that, before emitting a problem/needs-call item, it derives the item's candidate fix task_id (or its stable signal key — e.g. the G-rule/alert subject) and probes `task_terminal_state`; if MERGED ⇒ suppress the item (or annotate "fix shipped — <PR>") rather than present it as current. **Conservative:** when no fix task_id can be derived, leave the item unchanged (never hide a genuinely-open problem). Reuse §2.

## 4. Out of scope

- The missions **proposed** lane — already being fixed (`missions-proposed-lane-signal-hardening-001`).
- Dedup/suppression ledgers that only grow (`heal-pipeline-stall-state.json`, unreviewed-merge dedup) — they suppress, they do not fire phantoms; unbounded-growth GC is a separate concern.
- Liveness/drift healers (heartbeat, install-drift, credential, git-drift) — a different class.
- Replacing the happy-path linking events (`resolve()`, the advancer exact-id match) — this spec adds a backstop, it does not remove the primary path.

## 5. Success criteria

- A pending approval whose PR merged is auto-expired within one reconciler cycle; no reminder fires after terminal.
- An alert-triage row whose dispatched work merged is auto-resolved; the five current phantoms clear.
- A shipped mission flips out of `drafting`/`in_flight`/`ready`.
- A sequence step merged under a variant id reconciles even when the advancer's primary path missed it.
- A dead-pid / merged in-flight entry is reconciled mid-run, not only at reboot.
- The CEO digest no longer surfaces a problem whose fix has merged (replaying the #482 watermark case → the item is suppressed/annotated, not presented as open).
- **Conservative guard holds:** a genuinely-live (OPEN/UNKNOWN) record is NEVER retired — tests assert MERGED⇒retire and OPEN/UNKNOWN⇒keep for every store.
- One shared `task_terminal_state`; the three existing ad-hoc probes refactored to call it with no behavior change.

## 6. Enforcement

- Each reconciler ships a test asserting terminal⇒retire AND live/indeterminate⇒keep (the conservative guard).
- The shared helper has unit tests for MERGED/CLOSED/OPEN/UNKNOWN including variant-id matching.
- A doctrine line added to the relevant CLAUDE.md: *"Every in-flight bookkeeping store MUST have a terminal-state reconciler that consumes `task_terminal_state`; indeterminate ⇒ keep."* **Enforcement:** Mirror's review checklist flags any new in-flight store introduced without a paired reconciler.

## 7. Build plan (sequence authored after this spec merges)

- **Step 1 — shared helper.** Extract `task_terminal_state` + refactor the three existing probes to use it + tests. (Foundation; §3 steps depend on it.)
- **Step 2 — approvals + alert-triage.** Apply §3.1 + §3.2. (agent-core) depends_on Step 1.
- **Step 3 — missions + sequence + in-flight + digest.** Apply §3.3 + §3.4 + §3.5 + §3.6. (agent-core) depends_on Step 1.

Steps 2 and 3 depend only on Step 1 and touch disjoint files; the advancer may parallelize them.
