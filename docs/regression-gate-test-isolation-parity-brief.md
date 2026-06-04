# Regression-gate test-isolation parity (conftest ↔ \_\_init\_\_)

**Card:** regression-gate-conftest-init-parity-001
**Discovered:** 2026-06-04 (while fixing log-dir-test-isolation-leak-001)
**Dial:** regression dial 3 (block new failures, tolerate pre-existing)
**Status:** fix proposed — parity guard test + cross-reference comment

## Problem

The regression gate (`scripts/test_regression_check.py`) runs the suite via
`python3 -m unittest discover`, parsing unittest `-v` FAIL/ERROR lines. It does
**not** run pytest.

The test-isolation protections in `scripts/tests/conftest.py` are
`@pytest.fixture(autouse=True)` fixtures. pytest only loads `conftest.py` inside
a pytest session — so **under the gate, none of these autouse fixtures run.**
The unittest path instead relies on `scripts/tests/__init__.py`, which at package
import sets the same isolation env vars process-wide.

The two bootstraps are coupled by convention only. Any protection that lives in
`conftest.py` but is not mirrored in `__init__.py` is **silently inert under the
gate** — a latent prod-leak class. This was surfaced by
log-dir-test-isolation-leak-001 (a test that depended on the autouse fixture and
failed under the unittest gate); this card is the structural root cause behind
that symptom.

## Enumeration of conftest autouse protections, and their gate-path mirror

| conftest autouse fixture | Mechanism | `__init__.py` mirror (unittest path) | Status |
|---|---|---|---|
| `_isolate_production_logs` | sets `OURLIBERTY_LOG_DIR` → tmp `logs/` (per-test) | sets `OURLIBERTY_LOG_DIR` if unset (process-level) | ✅ mirrored |
| `_block_live_chain_event_emit` | sets `OURLIBERTY_DISABLE_LIVE_EMIT=1`; `reset_client_for_testing()`; monkeypatch `chain_event_emit._get_client → None` | sets `OURLIBERTY_DISABLE_LIVE_EMIT=1` | ✅ mirrored (see note) |

**No active leak today.** Both protections are currently mirrored. The
chain-event case deserves the cross-reference to the
"tests inherit live SUPABASE creds" history (2026-06-02 leak: 200+ `real-*`/
`prod-*` fixture rows upserted into the live `chain_events` table from a build
worktree with injected `SUPABASE_*` creds — PR #256 lineage):

- conftest's belt-and-suspenders includes a **direct `_get_client` attribute
  monkeypatch** that does **not** run under unittest.
- The unittest path is nonetheless protected because
  `chain_event_emit._get_client()` early-returns `None` whenever
  `OURLIBERTY_DISABLE_LIVE_EMIT` is set — and `__init__.py` sets it. The check
  sits **before** the cached-client return, so even a pre-cached `_CLIENT` is
  ignored under test. The env var, set process-wide at import, is the real
  common mechanism; the attribute patch is pytest-only defense-in-depth.

So the residual risk is **not** a current hole — it is **drift**: a future
autouse fixture (or a refactor that removes the env-var early-return from
`_get_client`, trusting conftest's attribute patch) would re-open the gap with
no signal.

## Decision

Two options were considered:

- **(a) Make the gate run pytest** so `conftest.py` applies uniformly. Rejected:
  - pytest is **not always installed** (it is absent in this dev clone and not
    guaranteed in Forge/Mirror build worktrees) — a hard new dependency on the
    gate's own viability; a missing pytest would make the gate exit 2 (analysis
    failed) and block every PR.
  - The gate's contract is unittest-shaped: it parses unittest `-v` output and
    emits dotted `module.Class.method` ids that `python3 -m unittest <id>`
    re-targets. Switching to pytest means rewriting the parser and changing the
    test-id format across the gate's consumers — a large, risky change to a
    load-bearing review gate.

- **(b) Mirror every protection in `__init__.py` + add a drift-catching parity
  test.** **Chosen.** Both protections are already mirrored, so no functional
  `__init__.py` change is needed; the durable part is a test that fails if the
  two bootstraps ever drift. This matches the repo doctrine
  (`docs/doctrine-of-doctrine.md`: *every rule earns an enforcement mechanism;
  prose alone does not hold*) and the established drift-guard pattern
  (`test_marker_drift.py`, `test_pulse_cycle_fixture_allowlist.py`'s
  `AllowlistDriftTest`).

## Fix

1. **`scripts/tests/test_conftest_init_parity.py`** (new) — the enforcement
   mechanism. It:
   - AST-parses `conftest.py` *as source* (never imports it — `import pytest`
     would fail under the gate) and enumerates every
     `@pytest.fixture(autouse=True)` function.
   - Asserts that set equals an explicit `MIRRORED_AUTOUSE_FIXTURES` registry. A
     new autouse fixture → the test fails until the author registers it **and**
     wires its env-var mirror into `__init__.py`.
   - Grounds each registry row (conftest + `__init__.py` both reference the
     named env var) and asserts the mirror is **live in-process**:
     `OURLIBERTY_LOG_DIR` set to a non-prod path, `OURLIBERTY_DISABLE_LIVE_EMIT=1`,
     and `chain_event_emit._get_client()` returns `None`.

2. **`scripts/tests/__init__.py`** — added a `PARITY IS ENFORCED` note pointing
   editors at the guard test and the registry, so the coupling is discoverable
   from the bootstrap itself.

### Verification (local, pre-dispatch)

- `python3 -m unittest scripts.tests.test_conftest_init_parity -v` → 5 tests OK.
- Drift simulation: injecting an unmirrored `_block_outbound_telegram` autouse
  fixture into a conftest copy is detected as `unregistered` → the test would
  fail with the offending fixture name.
- Sibling isolation tests (`test_log_dir_resolution`, `test_chain_event_emit`)
  still pass; pytest confirmed absent locally (reinforces rejecting option (a)).

## Maintenance contract

Adding an autouse fixture to `conftest.py` now requires, in the same change:
1. an env-var mirror in `scripts/tests/__init__.py`, and
2. a row in `MIRRORED_AUTOUSE_FIXTURES` in `test_conftest_init_parity.py`.

Omitting either fails the regression gate — the two runners cannot drift.

## Related

- log-dir-test-isolation-leak-001 — fixes the one symptomatic test; this card is
  the structural root cause behind it.
- stale-test-sweep-timezone-medic-001 — in-flight, adjacent test-hygiene sweep.
- PR #256 / "tests inherit live SUPABASE creds" — the live-DB leak history the
  chain-event mirror guards against.
