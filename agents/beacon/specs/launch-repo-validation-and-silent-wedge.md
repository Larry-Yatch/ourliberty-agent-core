# launch-repo-validation-and-silent-wedge — fix the bad-repo launch + the silent dispatch wedge

**Type:** Reliability fix (projects-v3 P3/P4 Launch→build path).
**Trigger:** 2026-06-19 dogfood of the Projects board Launch→build path.
**Depends:** projects-v3-p3-pipeline (shipped). Touches `dashboard_api.py`,
`launch_queue_drain.py`, `build_sequence_advancer.py`.

---

## 0. Desired End State

**A board Launch can never dispatch a build to a repo the factory can't build,
and if a step is ever dispatched somewhere unbuildable — or simply never makes
progress — it fails LOUDLY (failure_reason + Larry alert + paused sequence)
within a couple of ticks, instead of sitting `dispatched` for hours with no PR,
no failure, and no alert.**

## 1. What happened (the incident)

A one-off phase `pipeline-empty-state-hint` was promoted from a durable capture
and Launched through the board. The capture had been emitted from the local
working directory `/Users/Larry/dev/ol-work`, so its `origin.repo` was the
**directory name `ol-work`** — not a real repo. Promote copied that onto the
project; Launch copied it into the launch-queue entry; the drain authored the
build sequence with `target_repo: "ol-work"`; Mirror DAG-preflight passed (it
doesn't check repo buildability); and the advancer dispatched the step.

`ol-work` is not in `config/agent-models.json` `repo_paths`
(`ourliberty-agent-core`, `ourliberty-dashboard`, `ourliberty-graph`), so Forge
could not act. The step sat `status=dispatched`, `pr_url=null`,
`failure_reason=null` for ~6h. The advancer's reconciliation pass logged
`reconcile: gh pr list failed for repo=Larry-Yatch/ol-work (Could not resolve to
a Repository)` **every tick** and silently no-op'd. Nothing self-healed; a human
caught it.

## 2. The two bugs

### Bug 1 — bad repo derivation (no validation/repair at the source)
The repo flows capture.origin.repo → project.repo → launch-queue `repo` → step
`target_repo` with **no validation against `repo_paths`** anywhere. A
working-directory name like `ol-work` rides all the way to a dispatch.

### Bug 2 — silent wedge (no escalation for an unbuildable/stalled dispatch)
A `dispatched` step whose `target_repo` is invalid can never produce a PR, so
the V6 reconcile pass (which only *retires merged work* via `gh pr list
--state merged`) silently no-ops: the gh call fails → returns `None` → `continue`.
There is also no generic timeout for a step that is `dispatched` but never makes
progress. So a Launch can hang for hours with zero signal.

## 3. The fix (layered — each layer loud in its own medium)

### Layer A — dashboard validates / repairs / rejects (`dashboard_api.py`)
- **Promote** (`_create_project_from_funnel`): drop a non-buildable `repo`
  (store `None`) so a bogus value (e.g. `ol-work`) never reaches
  `projects.json`. The launch endpoint re-derives the real repo at build time.
- **Launch** (`_handle_launch_build`): gate the build repo — `phase.repo or
  project.repo`, if it's in `repo_paths` → use it; otherwise **reject 422**
  (`unbuildable target repo`) — a loud, user-visible error telling the user to
  set the real target repo, never a silent bad dispatch.
- **No spec-derivation.** We deliberately do NOT try to infer the repo from the
  spec's location: every spec lives in agent-core's `agents/beacon/specs`
  regardless of the build target (a `ourliberty-dashboard` build's `spec_doc`
  is an agent-core path), so spec location would mis-route every dashboard/graph
  build to agent-core. The phase/project `repo` is the only reliable signal;
  when it's missing/bogus the right answer is to ask (422), not guess.
- **Fail-open:** if `repo_paths` can't be read (transient), skip the check —
  never block a previously-fine launch over a config read miss.

### Layer B — drain belt-and-suspenders (`launch_queue_drain.py`)
Before authoring a *new* sequence, validate the entry's `repo` against
`repo_paths`; an invalid+unrepairable repo is **dead-lettered** (moved to
`.failed/`, warning logged) rather than authored into an unbuildable sequence.
Belt-and-suspenders for a legacy / hand-written queue file (the dashboard now
guarantees a valid repo). Fail-open on unreadable config.

### Layer C — advancer escalates a stranded dispatch (`build_sequence_advancer.py`)
A new **flag-independent** pass (`_escalate_stranded_dispatched_steps`), run on
`active` sequences right after the reconcile pass, escalates the FIRST stranded
`dispatched` step it finds:
- **Invalid target_repo** (`target_repo ∉ repo_paths`) → escalate **immediately**
  (a bad repo never gets better). Severity `critical`.
- **Stall** (valid repo, but `dispatched` longer than
  `DISPATCH_STALL_TIMEOUT_SEC` with no `pr_url` and no gate progress) →
  escalate. A generous wall-clock backstop (default 4h, well beyond any real
  build) for *any other* cause that strands a dispatch. Severity `warning`.

Escalation = mark the step `failed` + set `failure_reason`, **pause the
sequence**, append audit, atomic-write, and **DM Larry** (subject-keyed
cooldown). Fail-open: if `repo_paths` is unreadable, the invalid-repo check is
skipped (never pause a sequence over a transient config miss); the stall
backstop still applies.

## 4. Why these placements
- The dashboard is the user-facing gate → a 422 is the loudest, earliest signal.
- The drain is the author → it must not write an unbuildable sequence.
- The advancer is the runtime backstop → it catches anything that still slips
  through (or any already-wedged sequence on the next tick), flag-independent so
  it fires even when forward-dispatch is gated off. This closes the
  "healer-reports-success-while-broken" / stalled-sequence-backstop gap class.

## 5. Validation
Unit/integration tests (no production board mutation):
- promote with `repo='ol-work'` → project stored with `repo=None`;
- launch a spec-ready phase with a valid repo → passes through unchanged;
- launch with a bogus or missing repo → **422** `unbuildable target repo`;
- drain a queue entry with `repo='ol-work'` → **dead-lettered**, no sequence;
- advancer: a `dispatched` step with `target_repo='ol-work'` → step `failed` +
  sequence `paused` + Larry DM **within one tick**;
- advancer: a valid-repo step `dispatched` past the stall timeout with no PR →
  escalated as a stall.

## 6. Out of scope
- The capture *emitter* writing the cwd name as `origin.repo` (other sources
  exist; the fix belongs at the validating boundary, not one emitter).
- The separate Forge **preflight marker-discipline** issue surfaced during the
  live recovery (Forge acted instead of emitting a PROCEED marker) — its own
  follow-up.
