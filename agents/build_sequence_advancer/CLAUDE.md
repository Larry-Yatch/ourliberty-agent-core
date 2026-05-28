# Not applicable — service-agent

This agent is a Python daemon (`scripts/build_sequence_advancer.py` on a systemd timer). No `CLAUDE.md` instructions are loaded by any LLM invocation against this directory; no `claude -p` session is ever spawned for `target_agent: build_sequence_advancer`.

The kickoff envelope routed here is consumed by `scripts/outbox_notifier.py::_handle_build_sequence_advancer_kickoff`, which transitions the sequence file's `status: pending → active` and exits. The daemon's recurring tick reads `~/agents/blackboard/build-sequences/*.json` directly.

Read `SOUL.md` and `scripts/build_sequence_advancer.py` for operational logic. See `agents/beacon/specs/build-sequence-orchestrator.md` for the spec contract.
