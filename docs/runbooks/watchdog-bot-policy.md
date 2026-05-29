# Watchdog bot liveness policy

`config/bot-liveness-policy.json` declares the deployment mode for each
bot the watchdog supervises. The watchdog reads this file when checking
liveness so a tmux-deployed bot (pulse-bot after PR #161) does not
false-alarm against a `systemctl is-active` check it was never expected
to satisfy — and the recovery command suggested in the Larry-alert DM
matches the bot's actual deployment shape.

## Why this file exists

Before the policy file, the watchdog hardcoded `systemctl is-active` as
the liveness gate and `sudo systemctl restart <unit>` as the recovery
command. When pulse-bot moved to a tmux deployment (PR #161 closed the
iter-94 launcher gap), the watchdog began false-alarming, and the
suggested recovery — running `sudo systemctl restart
ourliberty-pulse-bot.service` — would have started a second pulse-bot
instance competing with the tmux-resident one for the Telegram
`getUpdates` long-poll. That's the destructive shape this policy
exists to prevent.

## Schema

```json
{
  "_schema": { "version": 2, "purpose": "...", "default_mode": "systemd" },
  "<agent>": {
    "mode": "systemd" | "tmux-or-systemd",
    "desired_state": "up" | "down",   // optional, defaults to "up"
    "systemd_unit": "<unit>.service",
    "tmux_session": "<session>",      // tmux-or-systemd only
    "launcher": "<repo-rel-path>"     // tmux-or-systemd only
  }
}
```

### desired_state semantics

Operator-declared intent, read by the watchdog desired-state reconciler.
Defaults to `"up"` when absent.

- **`up`** — reconciler restarts the bot if it goes down (subject to the
  M=3 attempts / 30 min flapping cap; on exhaustion it pages
  `bot-reconcile-flapping:<agent>` and stops actuating until the window
  resets).
- **`down`** — reconciler will NOT restart the bot, and the per-tick
  down-alert is suppressed. Use this to hold a bot off deliberately
  (replaces the mask-unit / kill-healer hack and the TERM-kill emergency
  stop). If the bot is alive when intent is `down`, the reconciler logs
  one INFO note about the divergence but does not enforce shutdown —
  recovery only, not policing.

### Mode semantics

- **`systemd`** — liveness is `systemctl is-active <systemd_unit>`.
  Recovery is `sudo systemctl restart <systemd_unit>`.
- **`tmux-or-systemd`** — liveness is satisfied if EITHER
  `tmux has-session -t <tmux_session>` OR `systemctl is-active
  <systemd_unit>` reports alive (tmux preferred, since it's the intended
  deployment). Recovery is `bash <launcher>` — NEVER `sudo systemctl
  restart`, which would create a competing instance.

### Default

A bot missing from the policy file defaults to mode=`systemd` with
conventional unit name `ourliberty-<agent>-bot.service`. This preserves
pre-policy behavior so a dropped entry does not crash the watchdog.

## How to add a new bot

1. Decide the deployment mode. systemd is the default; pick
   `tmux-or-systemd` only if there's a genuine reason the bot needs a
   tmux session (e.g., interactive `getUpdates` long-poll that fights
   restart-loop semantics, like pulse-bot).
2. Add an entry to `config/bot-liveness-policy.json` with the right
   required fields (see schema above).
3. Add a `bots:<agent>:down` entry under `watchdog` in
   `config/alert-translations.json` so the DM uses plain language. For
   tmux-or-systemd bots, also add a `bots:<agent>:tmux` INFO entry for
   the "running via tmux" surface.
4. Run `python3 -m unittest scripts.tests.test_bot_liveness_policy` to
   confirm the schema gate is happy.

All policy-declared bots are reconciled by `reconcile_bot_desired_state`
in `scripts/watchdog.py` — there is no longer a separate alert-only set
or a per-bot auto-restart carve-out. Setting `desired_state: "down"` is
the supported way to keep a bot off; everything else gets restarted on
detect-down (within the flapping cap).

## Rule + enforcement pairing

Per `docs/doctrine-of-doctrine.md` (PR #163), every rule earns an
enforcement mechanism.

- **Rule.** Each bot entry in `bot-liveness-policy.json` must declare
  `mode` in `{systemd, tmux-or-systemd}` and all required fields for
  that mode (`systemd_unit` for both modes; additionally
  `tmux_session` and `launcher` for `tmux-or-systemd`).
- **Mechanism.** `scripts/bot_liveness_policy.py:load_policy` raises
  `BotPolicyError` on schema violation. The watchdog calls
  `load_policy` at module import (deriving `ALERT_ONLY_BOTS`) and again
  inside `check_bots()`; a malformed policy makes the watchdog refuse
  to operate rather than silently regress the carve-out semantics this
  module exists to express.
- **Tests.** `scripts/tests/test_bot_liveness_policy.py::LoadPolicyTest`
  asserts each schema-violation shape raises `BotPolicyError`.

## Beacon carve-out — retired

beacon-bot used to be auto-restarted by a bespoke `check_beacon_bot()`
path while every other bot was alert-only. With the desired-state
reconciler that asymmetry is gone: beacon (and pulse, forge, mirror)
are all reconciled by the same `reconcile_bot_desired_state()` pass.
This avoids a double-restart race between the bespoke carve-out and the
reconciler. The original motivation (beacon is the alert delivery
channel, so an alert about beacon being down cannot reach Larry) is
preserved — beacon's `desired_state` is `up`, so the reconciler
restarts it the same way the carve-out used to. See
`docs/desired-state-reconciler-brief.md` §4d for context.
