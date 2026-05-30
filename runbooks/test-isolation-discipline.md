# Runbook — test isolation discipline

**Component:** `scripts/tests/test_no_production_path_leaks.py` (the CI regression gate that enforces this discipline) + this runbook.
**Scope:** every Python file under `scripts/tests/*.py`.
**Rule:** tests MUST NOT read from or write to production agent-state paths.

## Why the rule exists

Production daemons (`inbox_watcher`, `build_sequence_advancer`, `outbox_notifier`, healers, etc.) continuously poll directories under `~/agents/` and act on whatever they find there. When a unit test writes a fixture envelope into one of those directories — even by accident, even just once during a CI run — the daemon picks it up and treats it as a real dispatch. The cost shows up as a Claude credit burn (the daemon retry-loops on the fixture's fake session_id) and as operator confusion (the dashboard surfaces phantom in-flight work that no human kicked off).

**Two incidents on record:**

| Date | Burn | Source |
|---|---|---|
| 2026-05-13 | ~$0.65 | A smoke test wrote a result envelope into the agent's outbox path instead of an archive subdir. The outbox notifier picked it up and dispatched it. (memory: `feedback_smoke_test_archive_not_outbox`) |
| 2026-05-26 | ~$15-20 | ~30 fixture envelopes with fake session_ids (`sess-abc-123`, `sess-preflight-xyz`, `forge-build-sess-19`) landed in `~/agents/inboxes/forge/`. `inbox_watcher` retry-looped on each, burning Opus 4.7 budget before manual cleanup. |

Two strikes — same failure-mode class — is enough to warrant a permanent regression gate.

## The rule

Tests must redirect ALL production agent-state path access to a tmpdir, for the duration of the test, AND must roll back the redirection on teardown. The paths to redirect:

| Pattern | Why it's load-bearing |
|---|---|
| `~/agents/inboxes/*` | `inbox_watcher` polls every 60s and dispatches whatever it finds. |
| `~/agents/blackboard/*` | `build_sequence_advancer` polls `build-sequences/*.json` every 5 min and acts on them. Also home of heartbeat files several healers read. |
| `~/agents/state/*` | `in-flight/`, `beacon-pending-approvals.json`, `auto-merge-queue/`, and other live coordination state. Tests writing here corrupt the live state machines. |

These are the daemon-input paths. `~/agents/logs/*` and `~/agents/outboxes/*` are not in the gate's scope — they accept writes but don't dispatch on them — but the same tmpdir discipline applies in spirit (don't pollute prod log files; you'll confuse the next person reading them).

## The canonical pattern

The reference implementation lives in `scripts/tests/test_build_sequence_advancer.py` (`_AdvancerHarness.setUp`, lines 90–148). Both env-var-driven module constants AND the `Path.home()`-driven constants get redirected:

```python
class _MyHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.agents_root = Path(self._tmp.name) / 'agents'
        # Create every subdir the module-under-test will write to.
        (self.agents_root / 'blackboard' / 'build-sequences').mkdir(parents=True)
        (self.agents_root / 'inboxes' / 'beacon').mkdir(parents=True)
        (self.agents_root / 'state').mkdir(parents=True)
        (self.agents_root / 'logs').mkdir(parents=True)

        # Redirect the env-var-driven root.
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.agents_root)

        # ALSO redirect HOME — modules like safe_write_inbox use
        # Path.home() (computed at module-import time) rather than the
        # env var. Without this, the dispatch path writes into the real
        # ~/agents/inboxes/<agent>/ even when OURLIBERTY_AGENTS_ROOT is
        # set. This is the gap that caused the 2026-05-26 incident.
        self._prior_home = os.environ.get('HOME')
        os.environ['HOME'] = str(self.agents_root.parent)

        # Force a fresh module import so module-level constants
        # (HOME, AGENTS_ROOT, INBOXES_ROOT, ...) re-resolve under the
        # redirected env.
        for mod in ('module_under_test', 'safe_write_inbox', 'other_dep'):
            sys.modules.pop(mod, None)
        import module_under_test as mut
        self.mut = mut

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        if self._prior_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self._prior_home
```

**Two things to notice:**

1. **Both env vars get redirected** — `OURLIBERTY_AGENTS_ROOT` (the modern, opt-in override) AND `HOME` (the legacy default that `Path.home()` keys off of). Some modules pick up the env-var override; some don't. Redirect both and you cover every module-level constant.
2. **The module gets re-imported** under the redirected env. Module-level constants are resolved once at import; without a reload, the test still sees the prod path.

`tempfile.TemporaryDirectory` is the unittest equivalent of pytest's `tmp_path` fixture. The repo standardizes on unittest (pytest isn't installed on the droplet), but the gate's heuristic accepts either.

## The regression gate

`scripts/tests/test_no_production_path_leaks.py` runs as part of the normal test suite. It walks `scripts/tests/*.py` via AST, finds any string literal containing a production-path pattern, and fails if the literal isn't a docstring AND isn't on the explicit whitelist. Adding a new test that writes a literal like `~/agents/inboxes/forge/foo.json` will fail CI with file:line and a pointer back to this runbook.

### Whitelist policy

The gate's `WHITELIST` dict (top of the test file) covers cases where the literal is NOT a write target — input fixtures passed to a parser-under-test, defensive `assert path_does_NOT_exist` checks, canonical-example strings in docstring-equivalents. Each entry has a one-line documented reason. **Prefer refactoring over whitelisting.** A whitelist entry should be a last resort, used only when refactoring would defeat the test's purpose.

A second sub-test (`test_whitelist_entries_still_exist`) prevents the whitelist from drifting: if a whitelisted file:line no longer contains a leak literal (because the file was refactored), the entry must be removed. Otherwise the whitelist silently swallows future regressions at that line number.

## Reserved fixture task-id namespace (`zz-fixture-`)

Path redirection (above) is the first line of defense: a well-behaved test
never writes to a daemon-input path, so its fixtures never reach a daemon.
The reserved task-id namespace is the second, independent line of defense:
if a fixture task_id *does* reach a classification surface — a leaked
envelope, an emission path that re-reads its own output, a Pulse /cycle
scanning recent task_ids — `fixture_patterns.is_fixture_task_id()` must be
able to say "this is a test artifact, not real work" with zero ambiguity.

**The convention:** every synthetic fixture whose `task_id` can flow through
dispatch, gating, or any `is_fixture_task_id()` surface MUST use a
`zz-fixture-<scenario>` task_id. Production task_ids NEVER use this prefix.
`zz-` sorts last in any listing (fixtures cluster at the bottom), and
`-fixture-` is self-documenting; `<scenario>` names the case under test
(e.g. `zz-fixture-mirror-bad-marker`).

**Why a reserved prefix and not just "any test id":** before this
convention, the same string spaces — `t-*`, `real-*` — were used for BOTH
synthetic leak-fixtures AND legitimate mock task names (the subjects of
routing/cost/revision unit tests). That collision is the deep root of the
2026-05-29/30 fixture-replay incident: a classifier cannot both gate a leak
named `t-fail` and pass a legit mock named `t-core` if they share a prefix.
Reserving `zz-fixture-` removes the ambiguity by construction — see
`feedback_reserve_namespace_for_test_fixtures` in agent memory.

**Per-occurrence disambiguation (when migrating existing tests):** the
invariant is *an id matches `is_fixture_task_id()` if and only if it is a
synthetic fixture that flows through a dispatch/gating surface.* Apply it
case by case — do NOT blanket-rename:

- A synthetic leak-fixture (an envelope/task_id that exists only to prove the
  gate catches it) -> rename to `zz-fixture-<scenario>`.
- A legitimate mock task name that flows through an emission/gate surface and
  must NOT be gated (e.g. it stands in for real work in a routing test) ->
  rename to a non-matching name (a `real-*` style id that is NOT in
  `FIXTURE_PATTERN_EXACT`).
- A legitimate mock that is passed directly to a helper and never reaches a
  classification surface -> leave it untouched; renaming is churn.

**Where it's enforced:** `scripts/fixture_patterns.py` is the single source
of truth (`FIXTURE_PATTERN_PREFIXES` lists `zz-fixture-` first). The prefix
list is mirrored into `runbooks/cycle-prompt.md` and `agents/pulse/CLAUDE.md`;
`AllowlistDriftTest` (in `scripts/tests/test_pulse_cycle_fixture_allowlist.py`)
fails loudly if any live mirror surface drifts from the source of truth.

## What if my test legitimately needs to test real inbox-watcher behavior?

That's an **integration test**, not a unit test. It goes in a separate test class (eventually a separate file) with explicit pause-the-watcher / cleanup-on-success / cleanup-on-failure discipline. The watcher must be paused before writes, fixtures must be tagged so the cleanup step can find them, and the cleanup step must run unconditionally. None of that exists today — there is no integration-test harness for the inbox flow. If you have a use case for one, file a spec proposal; out of scope for V1 of this discipline.

## Debugging a failing gate

```bash
# Re-run just the gate, with full violation list.
python3 -m unittest scripts.tests.test_no_production_path_leaks -v

# To see EVERY literal hit including in-docstring ones (for triage):
python3 -c "
import sys; sys.path.insert(0, 'scripts/tests')
from test_no_production_path_leaks import _scan_all_test_files
for h in _scan_all_test_files():
    flag = 'docstring' if h['in_docstring'] else 'CODE'
    print(f\"  [{flag}] {h['file']}:{h['line']} -- {h['literal']}\")
"
```

The fix is almost always to copy the `_AdvancerHarness.setUp` pattern above into your test's setUp and remove the production-path string from the test body.

## Out of scope (future work)

- Cleaning leaked fixtures from prior incidents — already done manually for both 2026-05-13 and 2026-05-26.
- Modifying `inbox_watcher` to ignore fake-session-id envelopes — the watcher's correct response to any well-formed envelope is to dispatch; the fix belongs at the source (no leak).
- Auto-detecting `Path.home() / 'agents/inboxes/...'` AST patterns — the current scanner catches string literals. A `Path.home() / 'agents/...'` BinOp would slip through unless the next path segment is a string literal that matches (which it usually is). Promote if a real-world miss emerges.
- An integration-test harness for the real inbox flow (see above).
