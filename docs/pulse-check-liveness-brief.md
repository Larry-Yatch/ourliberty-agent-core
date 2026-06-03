# Brief: Pulse-check liveness watcher (watch the watchers)

**Type:** Forge dispatch (new healer + small per-check edits)
**Origin:** Cost-signal audit 2026-06-03. Checks IX and X silently stopped firing ~June 1–2 and
nothing noticed for ~2 days. The checks themselves are healthy (confirmed via `--dry-run`, both
exit 0) — they have no liveness signal of their own, so a scheduler stall is invisible.

## Problem

Pulse checks (I–X) are the fleet's self-optimization layer, but a check is the one component with
**no heartbeat**. When its invoker (the Pulse cycle) stops calling it, it goes dark silently — the
exact failure that hid the IX/X stall for 48h.

## Fix

1. **Per-check success heartbeat.** On successful completion, each check (I–X) emits a heartbeat:
   `blackboard/pulse-check-<n>.heartbeat` (touch + write `{ "ts": ..., "check": "<n>" }`).
   Minimal, invocation-agnostic — works no matter *what* schedules the check.
2. **Per-check failure emission.** Wrap each check's top level so an uncaught exception emits a
   `pulse-check-failed:<n>` envelope (cheap complement to the heartbeat).
3. **New watcher** `scripts/heal_pulse_check_staleness.py` + systemd timer (**`OnCalendar`**, per the
   OnUnitActiveSec-dies lesson). Reads a cadence registry and, for each check, if the heartbeat is
   older than `cadence + grace`, emits a `pulse-check-stale:<n>` larry-alert envelope.
4. **Cadence registry** `config/pulse-check-cadence.json`: each check's expected cadence + grace.
   Missing-entry = fail-closed (alert), per "every rule earns enforcement."
5. **Routing:** all signals go through the existing `larry_alerts` envelope → Beacon triage →
   surface to Larry as an **outcome/escalation only** (fix-first/notify-on-outcome). A dark check
   usually can't self-resolve, so it will legitimately reach Larry — as an outcome, not a ping.

## Why this catches all three failure modes

Errored (IX), can't-run / missing-env (X), and fully-silent timer death — a freshness-of-success
check catches all three, where a pure try/except would miss the silent-stop case.

## Reuse (not greenfield)

Heartbeat-file pattern already exists (`heal-chain-event-shipper-heartbeat`); larry-alerts envelope
and Beacon triage already exist; OnCalendar timer pattern already documented.

## Built-in validation

On first deploy the watcher will **immediately flag the currently-stalled checks** — proves it works
and surfaces the real #1.

## Scope boundary

This dispatch makes silent death **detectable**. It does NOT fix *why* the scheduler stopped calling
IX/X — that scheduler diagnosis is a separate follow-up. State this explicitly so the agent doesn't
scope-creep into the Pulse-cycle invoker.

## PREFLIGHT MUST VERIFY

- Each check's real cadence and how Pulse decides to run it (checks are invoked by the Pulse agent,
  NOT `run_cycle.sh` — confirm the invocation path before assuming a fixed schedule).
- Existing heartbeat-file convention/path so the new heartbeats match it.
- The canonical `larry_alerts` envelope shape + that Beacon consumes the new subjects.
- CLARIFY if any check is intentionally infrequent (e.g. III every other Sunday) so its grace window
  doesn't false-alarm.
