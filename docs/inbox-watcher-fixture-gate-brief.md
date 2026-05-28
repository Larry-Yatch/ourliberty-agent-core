# Brief: enforce fixture-pattern allowlist at the inbox-watcher dispatch boundary

## Problem (observed 2026-05-28)

The inbox-watcher dispatched fixture/test envelopes to Opus as if they were
real tasks, in a self-replicating loop:

- Cost: $33.44 across 3 hours, 104 fixture dispatches today, looping since
  ~09:48 MDT (`notify-t-pf`, `notify-q-1`, `dead-letter-*`, `marker-error-*`).
  (It was never stopped that morning and ran on to **$261.63 / 725 runs** before
  being halted by hand at ~15:02 MDT; archives held ~2,564 re-processings.)
- `scripts/inbox_watcher.py` has ZERO references to `is_fixture_task_id`.
  PR #147's allowlist only guards Pulse's `/cycle` hallucination class — it is
  NOT enforced at the watcher's dispatch boundary.
- The cascade self-replicates: processing a `notify-*` envelope re-emits another
  notify; doubled-prefix routing artifacts (`notify-dead-letter-notify-q-1.18`,
  `marker-error-notify-t-pf-1`) never match their terminator, so they regenerate
  every cycle (the `.18` seq counter climbing). Lineage: chain-routing gap #5,
  the `notify-notify-{task}` doubled-prefix bug (see inbox_watcher.py:373).

Three compounding gaps: (1) fixtures leaked into live `~/agents/inboxes/`
[test-isolation gap, ties to v2/#153]; (2) no fixture gate at dispatch; (3) the
notify/dead-letter cascade can't self-terminate.

Immediate bleed was stopped by hand on 2026-05-28 (stop watcher → drain →
restart). This brief is the durable defense-in-depth so it cannot recur.

## Fix

1. **Gate at the dispatch boundary.** At the point in `inbox_watcher.py` where a
   task is selected to run, call the fixture check on the task_id. On match: do
   NOT dispatch — archive the envelope to `.archive/` (or `.invalid/`) with a
   `fixture-suppressed` log line and `$0` cost, mirroring the Pulse `/cycle`
   suppression behavior. Reuse `scripts/fixture_patterns.py` (single source of
   truth — do not re-derive a regex).

2. **Peel routing wrappers before matching.** Self-replicating envelopes bury the
   fixture task_id behind wrapper prefixes (`notify-`, `dead-letter-`,
   `marker-error-`), so a raw `is_fixture_task_id` on the envelope name misses
   them. Add a centralized helper to `scripts/fixture_patterns.py`, e.g.
   `is_fixture_envelope_name(name)`, that strips the known routing wrappers
   (iteratively, with a cycle guard) and re-tests `is_fixture_task_id` at each
   layer. Tolerate trailing `.<seq>` suffixes. Centralize so the watcher, Pulse
   checks, and `outbox_notifier` all consume the same helper.

3. **Cascade non-termination (secondary).** Investigate why the notify/dead-letter
   cascade re-emits without terminating (doubled-prefix routing gap #5 lineage).
   The dispatch gate in (1) is the safety net even if this persists, but fixing
   the cascade removes the regeneration at the source.

## Acceptance criteria

- Regression test: a fixture envelope placed in an inbox — including the
  wrapper-prefixed forms `marker-error-notify-t-pf-1.json` and
  `notify-dead-letter-notify-q-1.18.json` — is archived with `fixture-suppressed`,
  NOT dispatched, and incurs $0 Opus cost.
- `is_fixture_envelope_name` covers: `t-*`, `notify-t-*`, `notify-q-*`,
  `marker-error-t-*`, plus the wrapped forms above.
- Prove no collision against real task_ids in `~/agents/outboxes/*/.archive/`
  (the discipline already required by `fixture_patterns.py`'s docstring).
- The gate IS the enforcement mechanism (per "every rule needs enforcement");
  the regression test is the gate that keeps it enforced.

## Open question for the implementer

`task-legacy` was observed in the loop (`notify-dead-letter-notify-notify-task-legacy.18`).
It pairs structurally with the allowlisted `task-001` and is almost certainly a
fixture, but it is NOT currently in `FIXTURE_PATTERN_EXACT`. Decide whether to add
it — and if so, prove no real-task collision first.

## Resolution (2026-05-28)

Implemented on branch `fix/inbox-watcher-fixture-gate`:

- `fixture_patterns.py`: added `matched_fixture_envelope` / `is_fixture_envelope_name`
  (wrapper-peeling + `.<seq>`-stripping, cycle-guarded, reusing the existing
  allowlist as the single source of truth), plus `ROUTING_WRAPPER_PREFIXES`.
- `inbox_watcher.process_task`: gate inserted immediately after the malformed-JSON
  guard, before the validator — fixtures are archived to `.archive/`, logged
  `fixture-suppressed`, given a `$0` cost row, and never dispatched.
- Open question resolved: `task-legacy` added to `FIXTURE_PATTERN_EXACT`. Collision
  proof over 3,968 distinct archived envelope stems found it ONLY ever wrapped as
  `dead-letter-notify-notify-task-legacy*` cascade artifacts — never a bare real
  task — so exact-match suppression cannot swallow real work. The same scan
  confirmed all 2,249 matched stems are genuine fixture artifacts (no real
  task_id collision).
- Regression gate: `scripts/tests/test_inbox_watcher_fixture_gate.py`.

The secondary cascade-non-termination fix (item 3) is left as follow-up; the
dispatch gate is the safety net regardless.
