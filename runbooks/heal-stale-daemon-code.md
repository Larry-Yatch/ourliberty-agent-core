# Runbook — heal-stale-daemon-code

Detects systemd-managed daemons running pre-merge code and auto-restarts them. Chain-discipline-v2 (2026-05-25); auto-restart added 2026-05-26.

## What it checks

For every unit matching `ourliberty-*.service` (excluding `.timer` units):

1. Reads `ActiveEnterTimestamp` via `systemctl show <unit> --property=ActiveEnterTimestamp` — when the running process started.
2. Reads `FragmentPath` via `systemctl show <unit> --property=FragmentPath` — the .service file on disk.
3. Parses the `ExecStart=` line of the .service file to identify the script path (skipping interpreters like `python3`, `node`, `bash`, `env`).
4. Stats the script's `mtime` — when the disk artifact was last updated.
5. If `script_mtime > service_start` **AND** `(script_mtime - service_start) > 5 minutes` (race-avoidance), the running process is older than the script and likely executing stale code.

## What it does on detection

**Auto-restarts + DMs.** On stale-detection (and outside the 30-min per-unit restart cooldown), the healer runs:

```bash
sudo -n systemctl restart <unit>
```

The sudoers contract on the droplet is `(ALL) NOPASSWD: ALL`, so `-n` (non-interactive) succeeds. If a future sudoers change ever revokes passwordless access, `-n` errors fast and the failure path below fires instead of wedging the healer on a password prompt.

After the restart attempt, the healer DMs Larry with one of three outcomes:

- **Success** (`subject=auto-restarted:<unit>`): includes the unit name, script mtime, pre-restart `ActiveEnterTimestamp`, and a best-effort list of recent commits that touched the script (via `git log --since=<pre_restart_ts> -- <script_path>`). The PR list is omitted cleanly if `git log` returns empty or errors.
- **Failure** (`subject=auto-restart-failed:<unit>`): includes the systemctl return code, stderr, and the manual recovery command `sudo systemctl restart <unit>`. **The healer does NOT retry on failure** — a non-zero `systemctl restart` means the unit is fundamentally wedged (unit file syntax, missing binary, port collision) and hammering it adds noise without changing the outcome.
- **Still stale after the cooldown** (`subject=still-stale-after-restart:<unit>`): if the cooldown elapses (30 min after the previous restart attempt) and the unit is STILL stale, the healer DMs `<unit> still stale after auto-restart at <ts>; manual investigation needed` and does NOT attempt another restart. This is the loop-prevention surface for chronically-broken units.

## What it does NOT do

- **No retry on `systemctl restart` failure.** One attempt per cycle, max. The DM is the recovery surface.
- **No restart of units it hasn't detected as stale.** The mtime predicate is the gate; non-stale units are never touched.
- **No detection of stale Python imports inside a long-running daemon.** Out of scope; only the daemon-process-vs-script-file mtime check.
- **No retroactive alerts for historical stale-daemon-code incidents.** Forward-looking only.

## Cadence + cooldowns

- Timer fires every 30 minutes (`ourliberty-heal-stale-daemon-code.timer`).
- **Per-unit restart cooldown** is 30 minutes, tracked as `last_restart_ts` in `~/agents/state/heal-stale-daemon-code-cooldowns.json`. The same unit cannot restart twice in one healer tick window. The cooldown clock starts at the moment of the restart attempt regardless of outcome — a failed restart still gets the 30-min cooldown so a wedged unit isn't hammered between ticks.
- **Per-unit DM cooldown** for the `still-stale-after-restart` escalation is 6 hours, tracked as `last_alert_ts` in the same state file. A chronically-broken unit gets one escalation DM per 6h, even though the predicate trips on every 30-min tick.
- `larry_alerts.append_alert` adds its own 60-min internal cooldown per `subject` on top of the healer's gates. For `auto-restarted` and `auto-restart-failed` subjects (rare informational events), larry_alerts' window is the binding constraint; for `still-stale-after-restart`, the healer's 6h gate wins.

## How to suppress auto-restart (intentional staleness)

If a daemon is intentionally running old code (e.g. holding off a behavior change until a maintenance window) and you want to prevent the auto-restart AND the escalation DM, the simplest move is the blanket kill-switch (which now disables BOTH detection AND restart, since `main()` short-circuits at the top of the tick before any per-unit work):

```bash
touch ~/agents/healers.disabled
```

The healer exits cleanly on the next tick when the kill-switch is present. Remove the file to re-enable. This single file is the canonical disable; there is no env-var equivalent.

For per-unit suppression of the still-stale escalation DM (without stopping the restart attempts), bump the unit's cooldown timestamps forward:

```bash
# Mark BOTH cooldown clocks so the next 6h is quiet for this unit:
python3 - <<'PY'
import json, time
from pathlib import Path
p = Path.home() / 'agents' / 'state' / 'heal-stale-daemon-code-cooldowns.json'
state = json.loads(p.read_text()) if p.exists() else {'services': {}}
state['services']['ourliberty-foo.service'] = {
    'last_restart_ts': time.time(),
    'last_alert_ts': time.time(),
}
p.write_text(json.dumps(state, indent=2))
PY
```

## How to run manually

```bash
# Foreground, prints findings to stdout + writes ~/agents/logs/heal-stale-daemon-code.log:
python3 ~/agent-core/scripts/heal_stale_daemon_code.py

# Or via systemd one-shot (uses production env, writes journal):
sudo systemctl start ourliberty-heal-stale-daemon-code.service
journalctl -u ourliberty-heal-stale-daemon-code.service -n 80
```

## How to install / activate

Follow the standard healer activation in `systemd/INSTALL.md`. Specifically:

```bash
sudo cp ~/agent-core/systemd/ourliberty-heal-stale-daemon-code.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-heal-stale-daemon-code.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ourliberty-heal-stale-daemon-code.timer
```

There is no env-var activation gate — the healer's only "off" surface is the kill-switch file above. Auto-restart uses `sudo -n` per the sudoers contract (NOPASSWD), so installation just works once the timer is enabled.

## When to investigate the source of a DM

### Auto-restart success DM (`auto-restarted:<unit>`)
The healer detected staleness and restarted the unit successfully. The DM is informational — Larry verifies the restart was correct by reading the included unit name, mtime, pre-restart `ActiveEnterTimestamp`, and the optional list of recent commits. No action needed unless one of the listed commits should NOT have been live (in which case revert or roll back manually).

### Auto-restart failure DM (`auto-restart-failed:<unit>`)
The healer detected staleness and ran `sudo -n systemctl restart <unit>` but the command returned non-zero. The DM body includes the systemctl stderr and the manual recovery command. Common causes:

1. **Sudoers config changed.** `(ALL) NOPASSWD: ALL` is the contract; if that's been narrowed, `-n` fails immediately. Verify via `sudo -n -l`.
2. **Unit file is broken** (syntax error after a manual edit; missing binary at ExecStart). Verify via `systemctl status <unit>` + `journalctl -u <unit> -n 200`.
3. **Port collision / dependency failure.** Same investigation path; the unit may need `systemctl reset-failed` before the next restart attempt.

The healer will NOT retry — fix the underlying issue and run the manual recovery command in the DM.

### Still-stale-after-restart DM (`still-stale-after-restart:<unit>`)
The healer restarted the unit 30+ minutes ago, the unit reports a NEW `ActiveEnterTimestamp` (the restart succeeded), but the script's mtime is STILL ahead of it by more than 5 minutes. This is usually a sign that the deploy pipeline is regressing the script faster than the restart timer can catch up, OR systemd's `ActiveEnterTimestamp` is not advancing as expected (very unusual). Investigate via:

- `journalctl -u <unit> -n 200` for restart errors that left the unit in a degraded state.
- `git log -n 10 -- <script_path>` to see if the script keeps changing rapidly.
- `systemctl show <unit> --property=ActiveEnterTimestamp` and compare to the script's mtime via `stat <script_path>`.

### Common root causes for any of these
1. **A PR merged + sync ran, but no one restarted the daemon.** This is the canonical case (PR #103, 2026-05-25 — marker-parser fix landed but `ourliberty-outbox-notifier.service` kept running pre-merge code until manual restart). The auto-restart absorbs this category now.
2. **A direct commit to main edited the file on disk** (e.g. config-only touch-up per Forge's CLAUDE.md allowance), the daemon picked up the change only at next restart, and a restart hasn't happened. Auto-restart now covers this too.
3. **`git pull` ran outside the deploy pipeline.** Maintain the discipline that daemons restart on deploy; if you `git pull` manually, also restart any daemons whose script files changed (or wait up to 30 min for the healer tick).

If the unit is one you intentionally do not want to restart (legacy behavior frozen pending a planned migration), use the per-unit suppression block above and add a comment in the unit's .service file documenting why.

## Where the code lives

- Script: `scripts/heal_stale_daemon_code.py`
- Tests: `scripts/tests/test_heal_stale_daemon_code.py`
- systemd unit: `systemd/ourliberty-heal-stale-daemon-code.{service,timer}`
- This runbook: `runbooks/heal-stale-daemon-code.md`
