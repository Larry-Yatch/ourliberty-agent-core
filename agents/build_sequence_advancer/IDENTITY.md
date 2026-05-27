# Identity

- **Name:** build_sequence_advancer
- **Role:** Multi-step build-sequence advancer daemon. Not a Claude agent — a stdlib Python daemon (`scripts/build_sequence_advancer.py`) on a 5-min systemd timer.
- **Emoji:** 🪜
- **Voice:** None. Daemon-only — no chat surface, no session log.
- **Avatar:** A ladder — rungs are sequence steps, climbed one at a time.

## What I am

A long-running daemon that owns the dispatch side of multi-step build sequences. Spec: `agents/beacon/specs/build-sequence-orchestrator.md` § 5.2 (concurrency rule) + § 5.3 (gate semantics).

On each tick I read every live sequence file in `~/agents/blackboard/build-sequences/<seq-id>.json`, evaluate which steps are dispatchable (deps merged, sequence active), and dispatch them to Beacon's inbox with `source: 'orchestrator'`. I also detect step completion via the two-source gate (chain_events + `gh pr view`) and transition step statuses pending → dispatchable → dispatched → merged.

## What I am NOT

- Not a Claude agent. My inbox at `~/agents/inboxes/build_sequence_advancer/` exists only as a routing terminus for the kickoff envelope (target_agent=build_sequence_advancer). The envelope is consumed by the outbox-notifier's kickoff handler (`_handle_build_sequence_advancer_kickoff`), which performs a status transition on the sequence file. No Claude session is ever spawned for this inbox.
- Not a step builder. I dispatch step envelopes to Beacon's inbox; Beacon then emits per-step APPROVAL_REQUEST markers; trust_policy auto-approves; Forge builds and opens PRs; Mirror reviews.
- Not the spec author. Beacon authors sequence files per Discipline 2 in `agents/beacon/CLAUDE.md`.

## Why this directory exists

`routing_validator.FRESH_DISPATCH_ROUTES` requires target_agents to be registered as real agents. The kickoff path (chat-mode `approve sequence <seq-id>` → Beacon bot → `dispatch_approved` → `safe_write_inbox` → `validate_task`) must write the envelope to a permitted Beacon target. PR-S4 rectification H4 added `build_sequence_advancer` to that allow-list and created this IDENTITY.md to keep the agent-directory convention symmetric. The daemon does not consult this file — it exists as a registration anchor only.
