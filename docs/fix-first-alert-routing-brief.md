# Brief: fix-first / notify-on-outcome alert routing

## Problem (observed 2026-06-03)

Auto-healers fix a problem and then fire an alert whose copy tells Larry to go
fix it by hand. Two live examples the same night:

- `install-drift:ourliberty-ceo-digest-daily.service` — heal_systemd_install_drift
  had ALREADY installed + daemon-reloaded the unit (it ran fine at 06:01), yet the
  DM rendered "🔴 NOW URGENT — run `sudo cp systemd/<unit> /etc/systemd/system/`".
- `sync-blocked:auto-commit-push-failed` — the push retried and self-healed on the
  next sync tick, yet it paged as a 🟡 SOON warning.

Both should have been silent-or-digest, not a page. The render table
(config/alert-translations.json) was written assuming Larry is the fixer; for
auto-healed events that assumption is stale.

## Policy (Larry, verbatim)

"Fix first, then either tell me it's fixed or tell me you couldn't and I need an
action." Concretely, every alert resolves to ONE of three routes:

- **escalate** — DM Larry now. Used for: a heal that FAILED (couldn't fix it →
  must carry the specific action Larry takes), and any genuinely un-healable
  detection. This is the DEFAULT (back-compat / fail-loud: an un-migrated emitter
  still DMs rather than going silent).
- **closure** — DM Larry ONE line: "was broken, fixed it, no action needed."
  Used ONLY when a successful heal is SIGNIFICANT (see significance gate).
- **digest** — NOT DM'd. The daily CEO digest surfaces it as a "self-healed, no
  action" line. Used for routine successful heals (drift re-installs, transient
  sync push-retries, daemon restarts that recovered).

## Significance gate (Larry chose "only the ones that mattered")

A successful heal earns a `closure` DM only if its subject is SIGNIFICANT:
the issue would have stalled/broken the chain, touched money/credentials/secrets,
or was user-facing. Everything else routes `digest`. Escalations (failed heals)
ALWAYS DM regardless of significance.

Significance is a config table (config/alert-significance.json), subject-prefix
keyed, default = routine. This keeps it Pulse-tunable later without a code change.

## Design

1. `route` field on every alert record (escalate|closure|digest), default escalate.
2. Auto-healers emit on OUTCOME: success + significant → closure; success + routine
   → digest; failure → escalate (with the action).
3. The bot's DM loop SKIPS route=digest (advances offset, no DM). closure +
   escalate DM as today.
4. The render table speaks outcomes: a healed event NEVER renders a "go run X"
   imperative. failed events carry the recovery action.
5. The daily digest ingests route=digest heals from the jsonl as a count + lines.

## Out of scope

The broader Pulse triage of DETECTION-ONLY signals that have no auto-fix yet
(watchdog, dispatch_sentinel, pulse_check_*) — those still need a fix-or-escalate
path, but that is the Pulse cycle upgrade, a separate workstream. This PR covers
only the emitters that already auto-fix.
