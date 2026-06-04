# Brief: Pulse-check liveness watcher (watch the watchers)

**Type:** Forge dispatch (new healer + small per-check edits)
**Origin:** Cost-signal audit 2026-06-03. IX and X are WEEKLY (Monday-only) checks; their most recent
scheduled runs ERRORED unnoticed (IX on Mon Jun 1 hit stale `:8001` code before the droplet synced
#240's `:8000` fix; X hit a transient missing-Supabase-env) and were caught only by manual digging.
Both run clean now via `--dry-run`. The gap this closes: a check has no liveness signal of its own, so
a scheduled run that errors or is skipped is invisible until a human looks.

## Problem

Pulse checks (I–X) are the fleet's self-optimization layer, but a check is the one component with
**no heartbeat**. If a check errors on its scheduled run, or its invoker (the Pulse cycle) fails to
call it on the day it's due, it goes dark silently — exactly what happened to IX/X's last runs.
Cadences VARY (some run every iter, IX/X weekly Monday-only, Check III every other Sunday), so the
watcher must be cadence-aware with a per-check grace window — not assume a fixed daily beat.

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

Validate via tests that inject a stale/missing heartbeat (assert it alerts) and a fresh one (assert
silence). On a healthy fleet it correctly stays quiet until a check actually errors or misses its
scheduled run — e.g. it would have fired on IX's Mon Jun 1 errored run.

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

---

## Hardening (2026-06-03, follow-up to PR #289)

The watcher's first live run fired 8 raw escalations (i, iii, iv, v, vi, viii, ix, x). An audit
(`docs/pulse-check-liveness-hardening-brief.md`) found a glob bug, a first-deploy baseline gap, a
translation gap, and genuinely-dark checks. This build makes the watcher escalate ONLY a
genuinely-dark check, in plain language, once.

1. **Artifact-glob robustness.** The bootstrap-artifact fallback now globs BOTH
   `pulse-check-<id>/check-<id>-*.json` and `pulse-check-<id>-proposals/check-<id>-*.json` for every
   id, derived in `artifact_globs_for()` rather than a hardcoded per-id list. Check III writes to
   `pulse-check-iii/` (not `-proposals`); the old per-id list globbed the wrong dir and false-alarmed.
   Covering both namings for every id means a newly added check needs no edit here.

2. **Monitoring-since baseline (kills the first-deploy storm).** The "never signalled" branch no
   longer escalates during a check's first `cadence_hours + grace_hours` window. Each check's
   `monitoring_since` epoch (when the watcher first observed it) is persisted to
   `blackboard/pulse-check-staleness-baseline.json` (atomic). A never-signalled check escalates only
   once `now - monitoring_since > cadence + grace`; before that it is a quiet warm-up log, no DM.
   Fail-closed is preserved: a check that blows its entire first window with no signal still
   escalates. Any future newly added check now deploys quietly too. Event-driven (vii) stays skipped.
   The already-stale path (a signal exists but is older than cadence+grace) is unchanged.

3. **Heartbeat seed (`scripts/seed_pulse_check_heartbeats.py`).** A one-time bootstrap that gives the
   watcher an honest starting signal without waiting weeks for the monthly checks. For every check
   that exposes a side-effect-free `--dry-run` (all but I), it runs the check ONCE through
   `run_check` in dry-run mode: a clean exit self-seeds a genuine heartbeat, a non-zero exit emits a
   `pulse-check-failed:<id>` alert — which is how IX/X surface their status immediately. Safety is
   structural: the seed only ever invokes a check with `['--dry-run']` (proven POST/DM/config-free),
   and anything it cannot prove safe (Check I has no `--dry-run`) is baseline-seeded instead of run —
   never a fake heartbeat. `--plan` previews the per-check plan with zero writes.

4. **Translation + routing.** The `pulse-check-stale`, `pulse-check-no-cadence`,
   `pulse-check-config-unreadable`, and `pulse-check-failed` subjects already render via the
   trailing-`:`-strip lookup in `translate_alert` (the engine needed no change). The real gap was in
   Check 0 triage (`alert_triage_state.classify`): a translation match was Tier-3-SILENCED to digest,
   which would have muted a genuinely-dark check. These entries now carry `"never_silence": true`;
   `classify` honors it by skipping Tier 3 and letting the alert fall through to Tier 4
   (`route=escalate`) with its translation intact. Translate + surface, never mute.

5. **Heartbeat-emission enforcement.** `test_pulse_check_run_check_enforcement.py` fails if any
   `pulse_check_<id>.py` `__main__` does not wrap `main()` with `run_check` imported from
   `pulse_check_heartbeat` — so a new check cannot ship without a liveness heartbeat.

6. **Systemd install.** The watcher's `.service`/`.timer` are in the repo. `heal_systemd_install_drift`
   auto-INSTALLS missing units (cp + daemon-reload + `enable --now`), so after merge it will install
   AND enable the timer on its own — no manual step required, but only AFTER the glob + baseline fixes
   land so the watcher does not re-storm. See `systemd/INSTALL.md`.
