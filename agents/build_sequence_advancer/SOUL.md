# build_sequence_advancer (service-agent, no LLM)

I am not a Claude agent. I am a Python daemon — `scripts/build_sequence_advancer.py` on a 5-minute systemd timer — that owns the dispatch side of multi-step build sequences. No LLM is invoked against this directory; no SOUL prose I write here can ever shape a session's tone or judgment because no session is ever spawned for me.

## Why this file exists

`scripts/sync_agent_core.py`'s validator treats every directory under `agents/*/` as a Claude-agent directory and expects the per-agent convention: `IDENTITY.md` + `SOUL.md` + `CLAUDE.md`. Without these two stubs the validator fails on every tick with two "missing file" errors against `sync.service`'s log. Authoring stubs (rather than teaching the validator a service-agent exemption) is cheaper and self-documenting: the stub itself records "this is a daemon — look at the script, not here."

## What carries my actual behavior

- **Identity / routing:** `IDENTITY.md` (already present) — the routing anchor that `routing_validator.FRESH_DISPATCH_ROUTES` keys on so kickoff envelopes (`target_agent: build_sequence_advancer`) pass the safe-write gate.
- **Operational logic:** `scripts/build_sequence_advancer.py` — the dispatch loop, the gate evaluator, the per-tick step transitions.
- **Spec contract:** `agents/beacon/specs/build-sequence-orchestrator.md` — particularly § 5.2 (concurrency rule) and § 5.3 (gate semantics).
- **Kickoff hook:** `scripts/outbox_notifier.py::_handle_build_sequence_advancer_kickoff` — consumes the `kickoff <seq-id>` envelope and performs the `status: pending → active` transition on the sequence file.

## Why not auto-discover

The validator's per-agent file requirement guards against a real failure mode for Claude agents — a missing SOUL.md means an LLM agent will run without its operating posture loaded. For a Python daemon that posture is encoded in the script's code; the validator can't know which directories are LLM-driven vs daemon-driven without metadata it doesn't have today. Stubs let auto-discovery stay simple.
