# Test Jail — default-deny test isolation (spec)

**Status:** SPEC — approved direction, PR sequence below.
**Evidence base:** [test-isolation-audit-20260611.md](test-isolation-audit-20260611.md) (17 verified holes H1–H18, missed items M1–M9, jail design constraints 1–11).
**Goal:** a test process is structurally UNABLE to touch production — files, money, network, repo, fleet state — under **every** invocation shape, on **both** machines, and a breach fails **LOUD at build time** instead of paging Larry.

## Why per-test mocking can never be the answer (the structural law)

Every PR that adds a side effect to a code path old tests drive end-to-end silently converts those tests into leak sources (H9 — exactly how #438 became an evening of pages). Isolation that depends on each test enumerating what to fake is fail-silent and decays with every feature. The audit verified that all four existing guardrails have holes and **all fail silently**. The fix must therefore be: (a) default-deny, (b) attached to the code itself rather than to the runner (9+ invocation shapes exist — they cannot all be patched), and (c) loud on breach.

## Architecture — five layers

Defense in depth; each layer independently useful, ordered by leverage.

### Layer A — universal bootstrap (closes H1, H2, H3, H13, M8; both test trees)

New `scripts/tests/_bootstrap.py`, importable BOTH top-level (`import _bootstrap`, works under bare `discover -s scripts/tests` because the start dir is on sys.path) AND as a package member (dotted invocations). Idempotent. At import, BEFORE any production module loads:

1. **HOME swap** — set `HOME` to a fresh per-process jail dir (`tempfile.mkdtemp(prefix='ol-test-home-')`). This is the ONLY mechanism that catches the frozen-`Path.home()` class (H1 larry_alerts, H13 import-time mkdirs, H15 fleet-control files) — env vars are structurally insufficient (audit constraint 2). Seed the jail HOME: minimal `.gitconfig`, empty `.claude/projects/`, `agents/{blackboard,state,logs,inboxes,outboxes}` skeleton (constraint 3).
2. **OURLIBERTY_* pins** — AGENTS_ROOT / LOG_DIR / WORKTREES_ROOT / TMPDIR under the jail; `OURLIBERTY_DISABLE_LIVE_EMIT=1`; mint `OURLIBERTY_TEST_RUN_SENTINEL` (Layer B keys off it).
3. **Subtractive env** (closes M4/H12 for in-process code): `del` `SUPABASE_*`, `TELEGRAM_*`, `GH_TOKEN`, `GITHUB_TOKEN`, `ANTHROPIC_*`, `CLAUDE_CODE_OAUTH_*`; pin dashboard/pulse API URLs to a dead localhost port (constraint 6).
4. `PYTHONDONTWRITEBYTECODE=1`.

Wiring:
- `scripts/tests/__init__.py` and `conftest.py` become thin delegates to `_bootstrap` (single source of truth — ends the __init__/conftest drift class).
- **Every test file's first repo-local import must be the bootstrap** — mechanical wave across the 170 files (they all already carry sys.path boilerplate to replace) **plus the root `tests/` tree (M1)**, which gets the same bootstrap.
- Enforced by Layer D's AST gate, so a new test file cannot omit it.

Compatibility (verified by the audit): tests compare against *dynamic* `Path.home()` so they stay green under the swap; git-driving tests set local user.email/name; `.claude/projects` readers get the seeded empty dir.

### Layer B — production-side choke guards (closes H1 forever, H4, H6, H7, H14, H15; makes H9 structurally dead)

At the small set of production chokepoints, a call-time guard (no import-freeze problem):

```python
def _refuse_under_test(channel):
    if os.environ.get('OURLIBERTY_TEST_RUN_SENTINEL'):
        raise TestIsolationBreach(f'{channel} reached from a test process')
```

Guarded chokepoints:
- `larry_alerts.append_alert` / `append_notification` / `append_approval_request` / `resolve_alert` (H1 — the pager).
- `safe_write_inbox` (H4 — the money amplifier; a leaked envelope becomes autonomous paid Opus dispatch).
- `agent_runner.run_claude` subprocess spawn + every other `claude` spawn site (H6 — spend).
- Telegram send helpers (H6).
- **One guarded Supabase client factory** replacing the 10+ direct `create_client` sites (H7 — refactor `chain_event_shipper`, `heal_stale_approvals`, `supabase_chunk`, `chain_events_retention`, `build_sequence_advancer`, `heal_pipeline_stall`, `heal_chain_event_type_audit`, `migrate_mission_control`, `pulse_check_{iii,iv,ix,x}`, `dashboard_api` to it).
- gh write helper (or guard at the existing wrapper) for `pr merge` / branch delete / label / status writes (H14).
- `kill_switch.halt`, `concurrency_guard` writes, `dispatch_lease` kill path (H14/H15 — fleet halts and SIGKILLs).

Semantics: **raise** (fail loud), not divert — a test that legitimately needs the behavior mocks the function as today; the guard only fires when an *unmocked* call escapes, which is precisely the bug. Production is unaffected (sentinel never set there). This is why H9 dies: a future side effect either flows through a guarded chokepoint (refused under test) or adds a new unguarded sink (caught by Layer D's census gate).

### Layer C — gate hardening (closes M2, M3, M4, M9, H10-arming, H12-at-the-gate)

`scripts/test_regression_check.py`:
1. **Never run in a live checkout** — drop the `sha == HEAD` in-place fast path; always materialize a disposable worktree and chdir there (M2; prevents M3's auto-commit of untracked test residue onto origin/main and mid-suite branch yanks).
2. **Subtractive env in `build_sandbox_env`** — currently `dict(os.environ)` + additions, which pipes live service-role keys INTO the sandbox (M4). Strip the Layer-A credential list.
3. **Droplet hard wall:** wrap the discover subprocess in `bwrap` (or `unshare`) read-only-binding the real `/home/larry/agents` and `~/agent-worktrees` — an escaped write becomes a loud EROFS failure even if every other layer is bypassed. Per-process, never chmod/ACL on the real tree (constraint 1 — ~50 live daemons share the box). macOS has no bwrap: the Mac floor is Layers A+B (constraint 11).
4. **Tripwire runs OUTSIDE the jail** — the #428 sentinel scan of the REAL tree must execute in the parent process with absolute real paths, or the fake HOME makes it vacuously green forever (constraint 4 / H10).
5. Gate worktree naming must not match the healers' `wt-main-*` cwd-prefix kill pattern (M9).

### Layer D — forward guards (meta-tests; keeps A–C from rotting)

1. **AST first-import gate:** every file in `scripts/tests/` AND `tests/` imports `_bootstrap` before any repo-local module. (Pattern: existing `test_no_production_path_leaks.py`.)
2. **Chokepoint census gate:** static scan of `scripts/` for direct `create_client(` / Telegram POST / `claude` spawn / gh-write invocations outside the guarded helpers — a new unguarded sink fails the suite (H7 recurrence, H9 for new channels).
3. **Jail-engagement meta-test:** runs the EXACT production invocation in a subprocess and proves the sandbox engaged + a deliberate breach attempt is refused (pattern: `test_gate_sandbox_env_injection.py`). One per invocation shape: bare discover, dotted, direct-file, gate.

### Layer E — ops (one-time, alongside)

1. **Purge the Mac phantom tree** `/Users/Larry/agents` + `/Users/Larry/agent-worktrees/wt-mirror-rev` (M5 — contains fixture alerts with the real chat_id; archive like the droplet's `.fixture-cleanup-20260610`, don't delete).
2. **Docs sweep:** every prescription of bare `unittest discover` (agents/forge/CLAUDE.md:144, docs/d35-5c-kickoff-prompt.md:35, d3-commit-4-plan.md) and especially `docs/test-suite-green-brief.md`'s *source-.env.larry-then-test* instruction → replaced with the blessed entrypoint.
3. Leak-residue reconciliation runbook (H18): on any future breach, the purge checklist (ledger lines, cooldown/silence keys, heartbeats, inbox residue, offsets).

## PR sequence (build-sequence-orchestrator compatible)

| PR | Scope | Size | Risk burned down |
|----|-------|------|------------------|
| **PR-0** | Pin the 4 leaking-NOW tests (`test_beacon_tier2_fallback` quota-ledger write, `test_ceo_digest_generator` heartbeat freshen — masks a real liveness signal, `test_heal_unregistered_approval` + `test_heal_wedged_review_sessions` log appends); fix `test-suite-green-brief.md`'s sourced-creds instruction | S | active leaks |
| **PR-1** | `_bootstrap.py` + wire `__init__`/conftest through it + mechanical import wave (170 + 4 files) + AST first-import gate | L (mechanical) | H1/H2/H3/H13/M8/M1 |
| **PR-2** | Choke guards (`larry_alerts`, `safe_write_inbox`, `run_claude`, telegram, gh) + guarded Supabase factory refactor + census gate | M | H4/H6/H7/H9/H14/H15 |
| **PR-3** | Gate hardening: worktree-always, subtractive env, droplet bwrap wall, outside-jail tripwire, worktree naming | M | M2/M3/M4/M9/H10/H12 |
| **PR-4** | Ops: Mac purge, docs sweep, reconciliation runbook, end-to-end validation (deliberate-leak test must fail every invocation shape on the droplet) | S | M5/H18 + proof |

Sequencing notes:
- PR-1 will surface latent failures hiding behind today's ~79 mock sites (constraint 7's "wave"). Run the full suite under the jail in a scratch worktree BEFORE merging; fix surfaced tests inside PR-1.
- The gate's 300s/SHA budget tolerates jail setup (negligible); lock isolation (M7) comes free with the HOME swap (sidecar locks land in the jail).
- The dashboard repo (M6) is out of scope here; carded separately (vitest env-scrub + network-deny).

## Acceptance (the property that ends the recurring incident class)

A deliberately-leaking test (un-mocked `append_alert`, inbox write, `create_client`, claude spawn) must **fail the suite loudly** under ALL of: bare `discover -s scripts/tests`, dotted module run, direct `python3 scripts/tests/test_x.py`, the regression gate, on droplet AND Mac — with zero writes observed in the real `/home/larry/agents` tree (verified by the outside-jail tripwire) and zero Telegram/Supabase/gh/claude traffic. When that holds, a future #438-class change breaks the *build*, not Larry's evening.
