# Runbook — heal-stale-daemon-code

Forward-looking healer that detects systemd-managed daemons running pre-merge code. Chain-discipline-v2, 2026-05-25.

## What it checks

For every unit matching `ourliberty-*.service` (excluding `.timer` units):

1. Reads `ActiveEnterTimestamp` via `systemctl show <unit> --property=ActiveEnterTimestamp` — when the running process started.
2. Reads `FragmentPath` via `systemctl show <unit> --property=FragmentPath` — the .service file on disk.
3. Parses the `ExecStart=` line of the .service file to identify the script path (skipping interpreters like `python3`, `node`, `bash`, `env`).
4. Stats the script's `mtime` — when the disk artifact was last updated.
5. If `script_mtime > service_start` **AND** `(script_mtime - service_start) > 5 minutes` (race-avoidance), the running process is older than the script and likely executing stale code.

## What it does on detection

DM-only. The healer never restarts anything (same posture as the stuck-detector — surface, don't act). On detection it appends a `warning`-level alert to `~/agents/blackboard/larry-alerts.jsonl` via `larry_alerts.append_alert`, which the Beacon bot DMs to Larry on its next sweep.

The DM body includes:
- The unit name
- Service start timestamp + script mtime + gap in minutes
- Script path
- Suggested action: `sudo systemctl restart <unit>` to pick up the latest code

## What it does NOT do

- **No auto-restart.** Same posture as the stuck-detector. A restart bypasses Mirror's review for behavior changes and risks restarting during in-flight work.
- **No detection of stale Python imports inside a long-running daemon.** Out of scope; only the daemon-process-vs-script-file mtime check.
- **No retroactive alerts for historical stale-daemon-code incidents.** Forward-looking only.

## Cadence + cooldown

- Timer fires every 30 minutes (`ourliberty-heal-stale-daemon-code.timer`).
- Per-service cooldown is 6 hours, tracked in `~/agents/state/heal-stale-daemon-code-cooldowns.json`. The first stale-detection DMs Larry; subsequent ticks within 6 h log the staleness but suppress the DM.
- `larry_alerts.append_alert` adds its own 60-min internal cooldown on top of the healer's 6 h. The healer's window is the binding constraint.

## How to suppress alerts (intentional staleness)

If a daemon is intentionally running old code (e.g. holding off a behavior change until a maintenance window) and you want to silence the healer's DMs:

```bash
# Touch the cooldown file forward so the next 6h is quiet:
python3 - <<'PY'
import json, time
from pathlib import Path
p = Path.home() / 'agents' / 'state' / 'heal-stale-daemon-code-cooldowns.json'
state = json.loads(p.read_text()) if p.exists() else {'services': {}}
state['services']['ourliberty-foo.service'] = {'last_alert_ts': time.time()}
p.write_text(json.dumps(state, indent=2))
PY
```

To suppress globally for all healers (blanket kill-switch):

```bash
touch ~/agents/healers.disabled
```

The healer exits cleanly on the next tick when the kill-switch is present. Remove the file to re-enable.

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

There is no env-var activation gate — this healer is read-only (no `gh pr merge`, no restart calls), so it's safe to enable by default after install.

## When to investigate the source of a DM

The DM means: `<unit>` was started before the current script-on-disk was last modified by more than 5 minutes. Common root causes:

1. **A PR merged + sync ran, but no one restarted the daemon.** This is the canonical case (PR #103, 2026-05-25 — marker-parser fix landed but `ourliberty-outbox-notifier.service` kept running pre-merge code until manual restart). Fix: `sudo systemctl restart <unit>`.
2. **A direct commit to main edited the file on disk** (e.g. config-only touch-up per Forge's CLAUDE.md allowance), the daemon picked up the change only at next restart, and a restart hasn't happened. Same fix.
3. **`git pull` ran outside the deploy pipeline.** Maintain the discipline that daemons restart on deploy; if you `git pull` manually, also restart any daemons whose script files changed.

If the unit is one you intentionally do not want to restart (legacy behavior frozen pending a planned migration), suppress alerts using the cooldown trick above and add a comment in the unit's .service file documenting why.

## Where the code lives

- Script: `scripts/heal_stale_daemon_code.py`
- Tests: `scripts/tests/test_heal_stale_daemon_code.py`
- systemd unit: `systemd/ourliberty-heal-stale-daemon-code.{service,timer}`
- This runbook: `runbooks/heal-stale-daemon-code.md`
