# Chain Context Durability — eliminating the context-drop / human-terminal dead-end class

**Status:** draft for build
**Author:** Beacon
**Date:** 2026-06-10

## 1. Purpose

The Beacon→Forge→Mirror→merge automation chain (and the build-sequence preflight→activate→advance path) moves work forward by passing **context on the task envelope**: `forge_build_session_id` (to `--resume` a build), `routing_source`/`original_source`/`source` (who a fallback routes to), `reply_chat_id` (whose Telegram thread a DM lands in), and loop budgets (`replan_count`, `max_replans`, `revision_count`). Each step uses that context to fire the next step automatically.

Two failures of the **same class** have now occurred:

- **Instance 1 — DAG-preflight REVISION** (already addressed by `dag-preflight-revision-autonomy-001`): Mirror's DAG-preflight PASS auto-activated the sequence, but the REVISION branch only DM'd Larry the raw verdict and never tasked Beacon — so a staged sequence sat `pending` indefinitely.
- **Instance 2 — code-review REVISION with no session (PR #412):** a missed inline Mirror-review dispatch was auto-recovered by `heal_undispatched_pr_review.py`, which rebuilt the envelope from GitHub truth and necessarily dropped `claude_session_id`, `reply_chat_id`, and `routing_source`. Mirror then returned REVISION; `_dispatch_revision_to_forge` found no session to `--resume` and no `routing_source` to DM, and fell to a broadcast "reconcile manually" alert. The PR stalled until the operator noticed.

The shared shape: **the happy path carries context that lets the next step fire automatically; recovery/fallback paths drop that context; when context is missing the step cannot fire and the system dead-ends in a non-actionable "manually re-dispatch" alert that silently waits on a human.**

## 2. Root cause

**(a) Envelope construction is ad-hoc.** There is no central envelope builder. Every dispatch/notify handler in `scripts/outbox_notifier.py` hand-rolls a dict and hand-copies context with `if data.get(X): task[X] = data[X]`. Each handler is an independent copy-list, so each is a fresh chance to forget a field; every recovery/healer path that reconstructs an envelope from external truth (GitHub, the sequence file) starts from nothing and sets only what its author remembered.

**(b) Success auto-advances; failure dead-ends to a human.** A system-wide audit found **13 handler sites** where the success branch fires the next step automatically while the adjacent failure / missing-context branch terminates in a "Larry must manually re-dispatch" alert (or, worse, a silent log with no alert at all). Only one handler — the DAG-preflight REVISION path repaired in `dag-preflight-revision-autonomy-001` — routes its non-happy branch back to an agent for autonomous recovery. **That handler is the template this spec generalizes.**

## 3. Scope

**In scope:** the dispatch/handoff machinery in `scripts/outbox_notifier.py`; the context-propagation fields; the healer backstops in `scripts/heal_pipeline_stall.py` and the standalone recovery healers; the relevant agent `CLAUDE.md` handling shapes + doctrine entries.

**Out of scope:** the agents' internal build/review logic; the trust-policy / approval gate; **any change to what gets built.** This spec changes *how context survives handoffs and what happens when it does not* — never what work is performed.

## 4. The mechanisms

### M1 — Centralized chain-envelope builder (the root-cause fix)

Introduce one helper — `build_chain_envelope(base, source, *, carry)` in a shared module imported by `outbox_notifier.py`, the healers, and `inbox_watcher.py` — that is the **only** sanctioned way to construct a dispatch/notify envelope. It owns the canonical **context-field whitelist**: `forge_build_session_id`, `routing_source`/`original_source`/`source`, `reply_chat_id`, `replan_count`, `max_replans`, `revision_count`, `target_repo`, `pr_url`, and task-lineage fields. The builder forces each caller to resolve each whitelisted field explicitly — pass-through from `source`, an explicit value, or an explicit `None` — converting a silent drop into a visible, code-reviewed decision. Refactor all existing construction sites to route through it; no behavior change beyond closing drops.

**Enforcement:** an AST/grep regression test (`scripts/tests/test_envelope_builder_is_sole_constructor.py`) that fails CI if any dispatch/notify envelope in `outbox_notifier.py` / the healers is built as a bare dict literal (task_id + target) instead of via `build_chain_envelope`.

### M2 — Recover-or-route-to-agent: no revision/handoff dead-ends to a human

Generalize the DAG template to every revision/handoff step that today dead-ends. **Rule: when an auto-loop step cannot fire, it routes to the owning agent's inbox for autonomous re-dispatch — it never terminates in a "do it manually" Larry alert.** Concretely for the no-session code-review REVISION (the PR #412 class): when a REVISION arrives with no `forge_build_session_id`, instead of the broadcast "reconcile manually" alert, route it to Beacon's inbox (intent `code-review-revision-no-session`) so a Beacon session re-dispatches Forge with a **fresh task_id** — a new build session that applies Mirror's findings to the existing PR branch (exactly the manual recovery performed for PR #412). The human is pinged only for (i) a genuine scope/values decision, or (ii) the agent-route itself failing (caught by M4).

**Enforcement:** routing code in `outbox_notifier.py` + a backstop healer check + the actionable-only doctrine; a test asserting the no-session REVISION path emits a Beacon-inbox notify and NOT a warning-severity Larry DM.

### M3 — Backfill derivable context before alerting

Several dead-ends fire because a field is *missing*, not *unrecoverable*: `target_repo` is derivable from the mission registry; `pr_url` is derivable via `gh pr view` / the routing-events log. The envelope builder (M1) backfills these from their source of truth before any handler concludes it must dead-end. Only genuinely unrecoverable context (a reaped build session) falls through to M2's agent-route.

**Enforcement:** backfill logic lives in `build_chain_envelope`; tests cover `target_repo`←mission-registry and `pr_url`←gh backfill.

### M4 — Auto-recover-then-alert for healer checks

The `check_*` functions in `heal_pipeline_stall.py` are all alert-only. For every check whose condition is *recoverable*, attempt recovery first (re-dispatch / re-route through M1+M2) and alert only if recovery fails — per the actionable-only doctrine ("an alert fires only if auto-remediation fails"). Detective-only checks (e.g. unreviewed-merge) stay alert-only by design.

**Enforcement:** healer structure + tests that a recoverable stall triggers a recovery attempt before any alert, and that a failed recovery alerts exactly once.

## 5. Build sequence (multi-PR / DAG)

- **S1 — envelope builder + enforcement test (M1).** Foundational refactor of `outbox_notifier.py` dispatch sites + the AST gate. `depends_on: []`.
- **S2 — no-session REVISION → Beacon route (M2).** Builds on S1's builder + the `dag-preflight-revision-autonomy-001` template. `depends_on: [S1]`.
- **S3 — derivable-context backfill (M3).** Adds backfill to the builder. `depends_on: [S1]`.
- **S4 — healer auto-recover-then-alert (M4).** `depends_on: [S1, S2]`.
- **S5 — agent CLAUDE.md handling shapes + doctrine entries.** `depends_on: [S2, S4]`.

**File-overlap note (the Mirror Check-3 lesson):** S2, S3, S4 all touch `outbox_notifier.py` and/or `heal_pipeline_stall.py`. Serialize steps that touch the same file (a mostly-linear S1→S2→S3→S4→S5 chain) rather than declaring them parallel, to avoid a parallel-file-overlap DAG-preflight REVISION.

## 6. Success criteria

- No dispatch/notify envelope is constructed outside `build_chain_envelope` (test-enforced).
- A no-session code-review REVISION self-heals via Beacon re-dispatch with a fresh task_id; no warning-severity Larry DM for it.
- `target_repo` / `pr_url` missing-context dead-ends no longer occur (backfilled).
- Recoverable pipeline-stall checks attempt recovery before alerting.
- The PR #412 cascade, replayed, resolves end-to-end with zero human-terminal alerts.

## 7. Relationship to in-flight work

- `dag-preflight-revision-autonomy-001` (dispatched) is the template / increment-zero for M2; this sequence rebases on it.
- The PR #412 manual fix (`harden-test-prod-write-isolation-pr412-revision`) is the manual instance of M2; it stays as the immediate unstick and is **not** part of this sequence.
