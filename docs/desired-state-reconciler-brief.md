# Desired-State Reconciler — bot liveness auto-recovery (scope 1 + 2)

**Status:** design brief, pending Larry approval
**Author:** Claude (Larry-chat design pass, 2026-05-28)
**Trigger:** pulse-bot sat cleanly down for ~1d 8h (stopped 2026-05-27 12:16 MDT) with no actor bringing it back. Investigation in this session found the recovery path is *documented but never executed*, and that pulse's tmux deployment is a recovery artifact rather than a design choice.

---

## 1. Problem

Two distinct holes, both surfaced by the pulse-bot outage:

**Hole 1 — the tmux deployment path has detection but no actuation.**
`config/bot-liveness-policy.json` declares pulse as `mode: tmux-or-systemd` and its `_schema` says *"Recovery defaults to the tmux launcher"* — but no code ever runs that launcher. beacon/forge/mirror run under systemd, so systemd (`Restart=always`) is their supervisor. pulse is the only bot that drifted off systemd onto tmux, and tmux does not auto-restart or survive reboots — so it had no supervisor at all.

**Why pulse was on tmux (root cause):** pulse-bot was emergency-killed 2026-05-27 for fixture-pattern hallucination (PR #147 fixed the root cause). Pulse's own /cycle then tried to self-relaunch via the `relaunch-missing-bot` always-fix, but Pulse is the *unprivileged Observer* agent — it has no `sudo`, so it could not use `systemctl restart`. tmux was the only no-sudo door, so PR #161 added `pulse_telegram_bot.sh` and pulse came back under tmux. The tmux deployment exists only because an unprivileged agent did the recovery.

**Hole 2 — "intended down" vs "unexpectedly down" is unmodeled.**
systemd `Restart=always` deliberately does **not** restart a clean `systemctl stop` — that is how an operator takes a bot down on purpose. So any auto-restart we add will fight deliberate stops unless we first model intent. Today the only way to hold a bot down is to mask the unit or stop the healer. That conflates "I want this off" with "this is broken."

Out of scope for this brief: **channel-level liveness** (process up but Telegram round-trip wedged — 2026-05-20 HTTP 502 storm, 2026-05-28 HTTP 409 double-poll). Tracked as a follow-up Pulse Check, see §7.

## 2. Chosen approach — Option B (return pulse to systemd; recover uniformly from the privileged watchdog)

The reconciler lives in the **watchdog**, which is privileged — it already runs `sudo -n systemctl restart` for inbox-watcher, outbox-notifier, and beacon-bot. So the watchdog can restart pulse's **systemd unit** exactly like the other three. That makes the tmux launcher unnecessary as a recovery path:

- **Return pulse to plain systemd.** Stop the tmux `pulse-bot` session, bring up `ourliberty-pulse-bot.service`, set policy `mode: systemd`.
- **Recover all four uniformly** via `sudo systemctl restart <unit>` from the watchdog. No per-bot tmux actuation branch to build or maintain.
- **Keep `pulse_telegram_bot.sh` only as a manual / break-glass fallback** (e.g. operator recovery when systemd itself is wedged). It is no longer a policy-declared recovery path.

This dissolves Hole 1 by removing the asymmetry rather than building machinery to support it. We intentionally give up pulse's *autonomous self-relaunch from its own cycle* — which we do not want anyway, since that cycle was the component hallucinating when it was halted. Recovery now comes from the independent, privileged watchdog.

## 3. Current state (what exists, do not rebuild)

- `config/bot-liveness-policy.json` — per-bot deployment mode + `systemd_unit` (+ tmux fields for pulse). `default_mode: systemd`.
- `scripts/bot_liveness_policy.py` — `load_policy()` (validates, raises `BotPolicyError`) + liveness resolution. `scripts/tests/test_bot_liveness_policy.py` is the existing gate.
- `scripts/watchdog.py` — oneshot on a 4-min timer. Already auto-restarts inbox-watcher, outbox-notifier, beacon-bot (with a restart cooldown marker). forge/mirror/pulse are alert-only today; `ALERT_ONLY_BOTS` is *derived* by subtracting the auto-restart set, so folding all bots into reconciliation is a subtraction, not a special case.
- All four bot units have `Restart=always` + `StartLimitInterval=300/StartLimitBurst=10`.
- `scripts/pulse_telegram_bot.sh` — tmux launcher; retained as manual fallback.

## 4. Design

**4a. Schema — add `desired_state` to each bot; move pulse to `mode: systemd`.**

```json
"pulse": {
  "mode": "systemd",
  "desired_state": "up",            // "up" | "down", default "up"
  "systemd_unit": "ourliberty-pulse-bot.service"
}
```

`desired_state` defaults to `"up"` when absent (preserves current intent for all bots). Setting `"down"` is the new, explicit way to hold a bot down — replacing the mask-unit / kill-healer hack, and replacing the TERM-kill used in the 2026-05-27 emergency halt.

**4b. Reconciler — extend watchdog, do not add a new daemon.**

Reconciliation generalizes the existing beacon-bot carve-out, so it belongs in watchdog (already owns bot liveness, already on a 4-min cadence, already has the cooldown-marker primitive). Add `reconcile_bot_desired_state()` invoked once per watchdog tick, after the existing checks:

For each bot in policy:
1. Resolve actual liveness via `bot_liveness_policy` (`systemctl is-active`).
2. Branch on (`desired_state`, actual):
   - `up` + alive -> no-op.
   - `up` + down -> `sudo -n systemctl restart <unit>` (subject to 4c cooldown). Treat systemd `auto-restart` SubState as alive so we never race systemd's own pending restart (existing M1 fix).
   - `down` + alive -> **leave it** (reconciler restores availability, it does not enforce shutdown). Emit one INFO note that intent/actual diverge.
   - `down` + down -> no-op, and **suppress the down alert** (intended state must not page).
3. The existing alert for an `up`+down bot stays, now reading "down, reconciler attempting recovery (attempt N/M)".

**4c. Restart-storm safety (reuse, don't invent).**
Reuse the watchdog cooldown-marker pattern: a per-bot marker under `~/agents/state/<bot>-reconcile-cooldown` + a rolling attempt counter. Cap at M attempts per window (propose M=3 / 30 min); on exhaustion stop actuating and escalate a `bot-reconcile-flapping:<bot>` alert. Mirrors systemd's `StartLimitBurst` philosophy.

**4d. Retire the beacon carve-out.**
beacon-bot is currently force-restarted by a bespoke watchdog path (`check_beacon_bot` / `_check_auto_restart`). The reconciler subsumes it. Remove the carve-out so beacon is restarted by exactly one mechanism (the reconciler) — otherwise two paths race to restart it. `ALERT_ONLY_BOTS` derivation collapses since all bots are now reconciled.

## 5. Per-bot applicability

| Bot | Today | After this change |
|---|---|---|
| pulse | tmux (no supervisor); systemd unit inactive | systemd; reconciler restarts on `desired_state: up` |
| beacon | systemd + bespoke watchdog carve-out | systemd; carve-out retired, reconciler owns it |
| forge | systemd + alert-only (clean stop stays down) | systemd; reconciler adds recovery-on-clean-stop |
| mirror | systemd + alert-only (clean stop stays down) | systemd; reconciler adds recovery-on-clean-stop |

EMERGENCY_HALT and APPROVALS_PAUSED are unaffected: they gate *work dispatch* in inbox-watcher, they do not stop bot processes, so the reconciler does not fight them. `desired_state` is also the natural substrate for a future paused-on-rate-limit healer (set `down` to pause a bot during a rate-limit window, `up` to resume).

## 6. Enforcement (doctrine #163 — every rule earns a mechanism)

The reconciler *is* the enforcement of the policy's recovery intent. Add to `scripts/tests/test_bot_liveness_policy.py`:
- every bot entry has a valid `desired_state` (`up`|`down` or absent->defaulted);
- every bot resolves a `systemd_unit` that exists on disk.

## 7. Cutover (bundled into this PR)

Land the policy change, reconciler, tests, and the pulse tmux->systemd cutover together so it is one reviewed change:
1. `tmux kill-session -t pulse-bot`.
2. `sudo systemctl start ourliberty-pulse-bot.service` (unit already `enabled`).
3. Confirm single instance (no HTTP 409), bot answers a test DM, systemd shows active.
4. Policy `pulse.mode` -> `systemd`.

## 8. Test plan

Unit (pytest, mock systemd probes):
- `up`+down -> restart invoked once; `up`+alive -> not invoked.
- `down`+down -> no restart, alert suppressed.
- `down`+alive -> no stop, divergence INFO emitted.
- flapping: M+1 consecutive down ticks -> actuation stops at M, flapping alert fires once.
- beacon: reconciled by the unified path, no double-restart (carve-out gone).

Live (on droplet, off-hours):
- pulse `desired_state: up`, `systemctl stop` -> reconciler restarts within one tick, bot answers a test DM.
- pulse `desired_state: down`, `systemctl stop` -> stays down across 3 ticks, no page.

## 9. Rollout

Real code touching watchdog -> through the chain (Forge build -> Mirror review), **not** Claude-as-Forge. Single PR; schema change + reconciler + carve-out retirement + cutover + tests land together so the enforcement gate is never green-without-teeth.

## 10. Follow-up (separate work, noted here for traceability)

**Channel-heartbeat Pulse Check** — existence checks (`is-active`) cannot catch a process that is alive but not round-tripping Telegram (2026-05-20 HTTP 502 storm; 2026-05-28 HTTP 409 double-poll). A periodic end-to-end probe (`getMe` success + getUpdates not erroring + optional self-ping watermark) would close this. Scoped as a Pulse Check rather than bundled into the reconciler because it is observation/triage, not actuation. Roadmap entry to be added.
