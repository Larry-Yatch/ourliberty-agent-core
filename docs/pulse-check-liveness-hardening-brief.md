# Pulse-check liveness watcher — hardening brief

Status: Larry-approved scope, 2026-06-03. One cohesive Forge build. Do it once.

## Why

The pulse-check liveness watcher (`scripts/heal_pulse_check_staleness.py`, shipped
PR #289 — "watch the watchers") fired 8 raw escalations on its first run
(2026-06-03 19:26 UTC) for checks i, iii, iv, v, vi, viii, ix, x. A live audit
found a mix of one real watcher bug, a first-deploy baseline gap, genuinely-dark
checks, and a missing translation. This build closes all of it so the watcher
only ever escalates a genuinely-dark check, in plain language, once.

Audit ground truth (verify against live `~/agents/blackboard`, do not assume):
- `i`   — writes `pulse-check-i/check-i-<date>.json`; fresh artifact today. Clean.
- `iii` — writes `pulse-check-iii/check-iii-<date>.json` (NOT `-proposals`); has
  `check-iii-2026-05-31.json`, within its 336h+48h cadence. FALSE ALARM caused by
  a glob bug (watcher reads `pulse-check-iii-proposals/`).
- `viii`— writes `pulse-check-viii-proposals/check-viii-<monday>.json`; has
  `check-viii-2026-06-01.json`. Clean in current code.
- `iv,v,vi,ix,x` — NO heartbeat and NO artifact on disk at all. ix/x errored on
  their 2026-06-01 Monday run (known IX/X stall; fixes dispatched 2026-06-03).
  v/vi are monthly (first Monday), next due ~2026-07-06. The watcher is CORRECT
  that these have no liveness signal — but it cannot tell "new heartbeat code,
  check has not fired yet" from "actually dead", and the timer runs every 6h
  with a 60-min alert cooldown, so each would DM Larry ~4x/day for up to a month.
- `vii`— event-driven; correctly skipped. Leave as-is.

## Goal

Make `heal_pulse_check_staleness.py` correct and quiet: escalate ONLY a check
that has genuinely gone past its cadence+grace with no liveness signal, render it
in plain language, and surface the true health of iv/v/vi/ix/x NOW where safe.

Read first: `docs/pulse-check-liveness-brief.md` (original design),
`config/pulse-check-cadence.json`, `scripts/pulse_check_heartbeat.py`,
`scripts/heal_pulse_check_staleness.py`, `scripts/larry_alerts.py`.

## Locked decisions (Larry approved — do not re-open)

1. ARTIFACT-GLOB ROBUSTNESS. The bootstrap-artifact fallback must match the REAL
   dir each check writes. For every id (i,iii,iv,v,vi,viii,ix,x) verify the write
   path in its `pulse_check_<id>.py` and make the fallback match BOTH
   `pulse-check-<id>/check-<id>-*.json` and
   `pulse-check-<id>-proposals/check-<id>-*.json`. Do not ship a fragile per-id
   list that silently drifts again — derive/verify from the scripts and cover
   both namings. This alone clears the iii false alarm.

2. MONITORING-SINCE BASELINE (kills the first-deploy storm). The "never emitted a
   heartbeat / no recent artifact" branch must NOT escalate during a check's
   first `cadence_hours + grace_hours` window after the watcher first starts
   monitoring it. Persist a per-check `monitoring_since` epoch in
   `blackboard/pulse-check-staleness-baseline.json` (atomic write; created on
   first observation of each id). Escalate the never-signalled case ONLY when
   `now - monitoring_since > cadence + grace`; before that, quiet log ("warming
   up"), no DM. Preserve fail-closed: a check that blows its first full window
   with still no signal DOES escalate (true positive). This also makes any
   future newly-added check deploy quietly. Event-driven (vii) stays skipped.
   The already-stale path (signal present but older than cadence+grace) is
   unchanged.

3. VALIDATE-NOW + HEARTBEAT SEED (the truth-surfacing half). Add
   `scripts/seed_pulse_check_heartbeats.py`. For each id i-x it must FIRST
   determine, by reading that check's weekday/sentinel gate, whether the script
   is provably side-effect-free when run off-cadence (NO mission POST, NO DM, NO
   config edit). For provably-safe checks: run the script once so a clean exit
   self-seeds a REAL heartbeat through the existing `run_check` wrapper, and
   capture/report any non-zero exit (this is what surfaces the ix/x 06-01 fix
   status immediately instead of waiting for Monday). For checks NOT provably
   safe off-cadence: seed `monitoring_since` only — DO NOT write a fake
   heartbeat. Emit a single summary (real-heartbeat / errored / baseline-only per
   id). HARD CONSTRAINT: this script must never POST a mission, send a DM, or
   edit config. If off-cadence safety cannot be proven for a script, treat it as
   unsafe and baseline-seed only.

4. TRANSLATION + ROUTING (full, do-it-once). Beacon flagged that `pulse-check-*`
   subjects (`pulse-check-stale:<id>`, `pulse-check-failed:<id>`,
   `pulse-check-no-cadence:<id>`) are not translated and that the matcher may
   "silently never match." Investigate `translate_alert` and the
   `significant_subjects` matcher in `scripts/larry_alerts.py` (note:
   `translate_alert` already strips trailing `:`-segments — confirm whether a gap
   truly exists before changing the engine). If a real gap exists, fix the
   matcher so ONE prefix entry covers all `pulse-check-*` subjects and any future
   check id (not a per-id list). Add the `config/alert-translations.json` entries
   so these render in plain language. Add a test proving `pulse-check-stale:iv`
   translates. Routing: keep `route=escalate` (a dark check is a real outcome)
   but make it Pulse-triageable — add a Check 0 known-pattern so Pulse triages it
   rather than raw-escalating. Translate + triage; do NOT mute genuinely-dark
   checks into silence.

5. HEARTBEAT-EMISSION ENFORCEMENT. Add a test that FAILS if any
   `pulse_check_<id>.py` does not wrap `main()` with `run_check` (every check
   must emit a heartbeat). Enforcement mechanism, not prose.

6. SYSTEMD INSTALL (scope note, mostly ops). The watcher `.service`/`.timer` are
   in the repo but not installed. Confirm whether `heal_systemd_install_drift.py`
   actually INSTALLS missing units or only alerts; if it only alerts, say so in
   the PR description — that is the real install gap. DO NOT enable the timer in
   this PR. Installation is a post-merge operator step, performed AFTER the glob
   + baseline fixes land, so the watcher does not re-storm before the fix is in.

## Acceptance

- Re-running the watcher against live `~/agents/blackboard` after the fix
  escalates NONE of i, iii, viii; stays quiet on iv/v/vi/ix/x within their first
  `monitoring_since` window; still escalates a check that blows cadence+grace
  with no signal.
- Check iii no longer false-alarms (glob fix), proven by a test.
- `pulse-check-stale:iv` renders a translated plain-language DM, proven by a test.
- A `pulse_check_<id>.py` missing `run_check` fails the new enforcement test.
- `seed_pulse_check_heartbeats.py` produces no mission POST / DM / config edit
  (assert in test), and reports per-id outcome.
- Stdlib only (match the existing watcher). Standard Forge flow:
  preflight -> build -> Mirror review -> PR. Conventional commits.

## Docs to update

`docs/pulse-check-liveness-brief.md` (add hardening section),
`runbooks/cycle-prompt.md` if check-invocation notes change,
`systemd/INSTALL.md` note for the watcher unit + the post-merge enable step.

## Out of scope (do not touch)

- The actual fix for WHY ix/x errored on 06-01 (already dispatched separately).
- Any change to check cadences in `pulse-check-cadence.json` values.
- `deploy-notifier` translation (Beacon owns that as a separate config PR).
