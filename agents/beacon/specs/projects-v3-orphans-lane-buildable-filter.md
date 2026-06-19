# projects-v3 — Orphaned lane buildable-filter (P1 follow-up, § 4.8)

**Step id:** `orphans-lane-buildable-filter`
**Repo:** `ourliberty-agent-core`
**Status:** spec → build (direct fix, /code-review high gated)

## Desired end state

The Missions board's **Orphaned** secondary funnel lane (dashboard
`app/missions/components/OrphansLane.tsx`, fed by the derive's `orphans[]`)
surfaces only **verifiably-live BUILDABLE work** — the things a human could
actually accept onto the pipeline. Terminal orphans were already dropped (a real
merge/close signal). § 4.8 closes the remaining gap: the **non-buildable noise**
that is an orphan-in-the-graph-sense but never a buildable mission
(chain-incident / alert artifacts, desktop-capture hashes, sequence-step
proposals, dag-preflight runs, dated-digest / translation / stale-fixture ids)
no longer clutters the lane.

Before this fix the lane showed ~63 "live" orphans, most of them noise.

## Root cause

`detect_orphans` (in `scripts/dashboard_api.py`) is deliberately broad: it is the
everything-in-flight view (task_ids in `chain_events` not registered to a
mission, minus `is_infrastructure_task`). It does NOT apply the stricter
"is this a buildable initiative?" gate — by design, because
`heal_orphan_autoregister` calls `detect_orphans` directly and needs the broad
set for its own purposes.

The dashboard's Orphaned lane (`orphans[]` + `funnel.secondary`) was rendering
that broad set verbatim. There was already a strict predicate for exactly this
question — `is_proposable_initiative(task_id, agent)` — but it was only used by
the autoregister healer's proposal queue, never by the surfaced view.

## Changes (server-side, single point)

1. **`scripts/dashboard_api.py` — `_build_derived_response`.** Immediately after
   `orphans = detect_orphans(recent_events, registered_task_ids)`, filter the
   list to buildable initiatives:

   ```python
   orphans = [
       o for o in orphans
       if is_proposable_initiative(o.get('task_id'), o.get('agent'))
   ]
   ```

   This is the single assembly point feeding BOTH `orphans[]` (consumed by
   `OrphansLane.tsx`) and `funnel.secondary` (built below it off the same list),
   so one filter narrows both. It runs before PR-state resolution, so the
   GitHub-truth read is also restricted to the surfaced subset.

2. **`scripts/heal_orphan_autoregister.py`** — comment-only: the now-stale
   "Higher signal bar than the Orphans lane" note is corrected to "the same gate
   the Orphaned lane now applies."

## Invariants preserved

- **`detect_orphans` semantics UNCHANGED.** This is a view-level filter on the
  derive response only. `heal_orphan_autoregister` calls `detect_orphans`
  directly and applies its OWN `is_proposable_initiative` filter separately
  (`scripts/heal_orphan_autoregister.py`), so the autoregister decision queue is
  untouched. Both consumers now agree on the buildable bar, but via independent
  call sites — no shared mutation.
- **Conservative gate.** `is_proposable_initiative` errs toward keeping anything
  not matching a known noise shape, so genuine buildable orphans always survive.
- **No new data source, no new field.** Purely additive filtering of an existing
  list; the funnel stays a pure re-view.

## Verification

- `scripts/tests/test_dashboard_api_missions_derived.py` green (109 tests),
  including new explicit cases: a non-buildable id (`desktop-ab12cd34`, a pure
  desktop-capture hash) is excluded from both `orphans[]` and `funnel.secondary`,
  while buildable orphans (`orphan-stalled-old`, `orphan-building-now`,
  `orphan-inreview-now`) stay. The § 4 parity + Phase-2 fixtures were updated to
  drop that one now-filtered orphan (the gate is hand-derived from the rules, so
  the parity gate stays non-circular).
- Dashboard: on `dashboard.ourliberty.dev/missions` the Orphaned count drops from
  ~63 to the buildable subset after droplet sync + dashboard-api restart.
