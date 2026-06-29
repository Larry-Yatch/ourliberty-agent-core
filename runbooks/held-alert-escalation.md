# Runbook — held-alert escalation (alert-pipeline-rework B5 + B6)

Spec: `agents/beacon/specs/alert-pipeline-rework.md` Part B.
Script: `scripts/held_alert_escalation.py`
Units:
- `systemd/ourliberty-held-alert-persistence.{service,timer}` (B5, every 10 min)
- `systemd/ourliberty-held-alert-backstop.{service,timer}` (B6, every 15 min)

## Background — the hybrid DM gate

The alert queue (`scripts/larry_alerts.py` → `~/agents/blackboard/larry-alerts.jsonl`)
gained a `hold` route (B1). A held alert lands on the dashboard (via the shipper)
but is NOT DM'd to Larry — the Beacon bot skip-and-advances past it, exactly as
it does for `digest`, gated by `severity != 'critical'`. A `hold` is a *pending
judgment*, deliberately held back from Larry's phone — not a silent drop and not
a self-healed closure.

Because the bot's offset cursor is forward-only, the ONLY way to turn a held line
into a DM is to APPEND a fresh `route='escalate'` line (B3,
`larry_alerts.append_promotion`). This module is what decides a held line has sat
long enough and appends that promotion.

## The two escalation paths (deliberately redundant)

| Path | Unit | Trigger | State | Survives Pulse death? |
|---|---|---|---|---|
| B5 persistence | `held-alert-persistence` (10 min) | fingerprint open ≥ `N_CYCLES_BEFORE_PROMOTE` (3) consecutive cycles (~30 min) | `held-alert-probation.json` cycle counter | No (cycle-driven) |
| B6 backstop | `held-alert-backstop` (15 min) | hold line's own `ts` older than `BACKSTOP_SEC` (30 min) | none (stateless) | **Yes** |

B6 is the safety net under B5: it reads each hold's own wall-clock age and needs
neither the state file nor a healthy Pulse, so it still fires if the persistence
timer or Pulse itself is dead. The two paths are timed to agree (~30 min) but
reach the decision by independent mechanisms.

## "Open hold" + promote-once

A fingerprint (`source:subject`) is **open** iff the most-recent queue event for
its key is a `hold` — i.e. no later `escalate` / `closure` / promotion line
superseded it — AND it is not durably silenced (`larry_alerts.is_silenced`).

Promote-once is queue-authoritative, NOT state-driven. A promotion appends an
escalate line carrying `promoted_from=<source>:<subject>`; on the next scan that
line is the most-recent event, so the fingerprint stops being "open" and neither
path re-promotes it. The B5 state `promoted` flag is belt-and-suspenders only.
The two timers are jittered apart; a truly-simultaneous double fire is bounded
at-least-once (a rare duplicate DM), matching the bot's existing delivery
semantics.

Resolution drops a fingerprint from probation (never promotes it): a `closure`
self-heal or any plain `escalate` for the same key means it was already dealt
with out of band; a silence means Medic confirmed it benign.

## A critical is never held

B2 forces `route='escalate'` for any `severity == 'critical'` at emit time
(`append_alert`), and the bot re-checks `severity != 'critical'` at read time
before skipping. So a critical never reaches this module — it DMs immediately.

## Install

```bash
sudo cp ~/agent-core/systemd/ourliberty-held-alert-persistence.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-held-alert-persistence.timer /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-held-alert-backstop.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-held-alert-backstop.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ourliberty-held-alert-persistence.timer
sudo systemctl enable --now ourliberty-held-alert-backstop.timer

# Verify install landed AND the timers are active (merged != installed).
systemctl is-active ourliberty-held-alert-persistence.timer
systemctl is-active ourliberty-held-alert-backstop.timer
systemctl list-timers 'ourliberty-held-alert-*'
```

Both units are auto-covered by the `systemd-install-drift` healer, so a missed
`cp` DMs Larry the exact install commands within one 12 h tick.

## Manual smoke

```bash
# One persistence cycle / one backstop sweep by hand.
python3 ~/agent-core/scripts/held_alert_escalation.py persistence
python3 ~/agent-core/scripts/held_alert_escalation.py backstop
# Or via systemd:
sudo systemctl start ourliberty-held-alert-persistence.service
journalctl -u ourliberty-held-alert-persistence.service -n 50 --no-pager
# Expect: `tick(persistence): open=N promoted=M waiting=K`
```

## Tuning

`N_CYCLES_BEFORE_PROMOTE` and `BACKSTOP_SEC` are module constants in
`scripts/held_alert_escalation.py`. Change + commit + restart-on-next-tick (no
reload). They are kept aligned (3 × 10 min ≈ 30 min) on purpose; if you change
one, reconsider the other so the two paths still agree on "stale."

## Kill switches

1. Touch `~/agents/healers.disabled` — blanket switch (this module honors it via
   `kill_switch_active`).
2. `sudo systemctl disable --now ourliberty-held-alert-persistence.timer`
   and/or `ourliberty-held-alert-backstop.timer`.

## Logs + state

- Logs: `journalctl -u ourliberty-held-alert-{persistence,backstop}.service` and
  `~/agents/logs/held-alert-escalation.log`.
- Heartbeat: `~/agents/blackboard/held-alert-escalation.heartbeat`.
- B5 state: `~/agents/state/held-alert-probation.json` (cycle counters; safe to
  delete — a deleted state file just restarts the cycle count, and B6 still
  backstops on wall-clock age).
