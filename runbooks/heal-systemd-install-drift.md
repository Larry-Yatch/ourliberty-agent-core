# Runbook: heal_systemd_install_drift

**Purpose.** Catch the gap where a unit file shipped in the repo's `systemd/`
directory never made it onto the droplet (or drifted out of sync after a unit
file changed), so a daemon/timer/healer is silently absent or running stale
config. Motivated by the E1.5 discovery that `heal-pr-auto-merge.{service,timer}`
shipped via PR #43 but were never installed.

**Cadence.** Every 12h via `ourliberty-heal-systemd-install-drift.timer`. Silent
unless drift is detected. Per-unit DMs deduplicated 12h.

**Script.** `scripts/heal_systemd_install_drift.py`.

**Safety gates (all three must pass before any privileged action).**
1. Kill-switch file: `~/agents/healers.disabled` absent.
2. Env flag: `OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED=true` (else dry-run —
   detects + sends one activation DM, never remediates).
3. Allowlist: the action class listed in
   `config/auto-remediation-allowlist.json`.

In dry-run, or when any gate is closed, the healer only *alerts*; the operator
runs the manual dance below.

---

## The three drift states

### 1. Missing install
The unit exists in `systemd/<unit>` but not in `/etc/systemd/system/`. Whatever
it does is not running on the droplet. Detected by `detect_drift()`.

### 2. Content drift
The unit exists in *both* places but the installed copy's contents differ from
the repo copy (a single trailing newline is normalized; drop-in overrides under
`<unit>.d/` are deliberately ignored). The droplet is running stale unit config.
Detected by `detect_content_drift()`.

### 3. Healed (auto-reconciled)
When the safety gates are open the healer fixes states 1 and 2 itself
(`cp` + `daemon-reload`, plus a class-specific activation step — see below) and
emits a **no-action** notification. Healed events carry a distinct subject
(`install-healed:`, `content-healed:`, `stuck-timer-healed:`) so they route to
the FYI digest, never the URGENT "run the install dance" copy. If you see a
healed notification, nothing is required beyond an optional
`systemctl status <unit>` spot-check.

(A fourth, independent pass — `detect_stuck_timers()` — restarts a `.timer`
whose next-fire anchor has gone to `infinity`. That is almost always a transient
false alarm at the top of a firing period; see the `stuck-timer` translation
entry.)

---

## The 3-way unit classification

The correct manual remediation depends on what *kind* of unit drifted. The
healer classifies each unit by parsing `Type=` from the **repo** copy of the
unit file (`_classify_unit`), into one of three classes:

| Class | What it is | Why it matters |
|-------|-----------|----------------|
| **`.timer`** | A timer unit. | `enable --now` (install) / `restart` (re-anchor) the timer. |
| **oneshot `.service`** | `Type=oneshot` — runs to completion, activated by a sibling timer. | A `daemon-reload` is enough: it re-execs with fresh content on its **next timer fire**. Restarting it would run it off-schedule. |
| **long-running `.service`** | `Type=simple`/`notify`/`forking`/`exec`/`idle`/`dbus`, or **no explicit `Type=`** (systemd defaults to `simple`). E.g. `ourliberty-inbox-watcher.service`. | A `daemon-reload` **NEVER restarts a resident daemon** — it keeps running the stale code. Remediation MUST `systemctl restart <unit>` (content drift) or `enable --now <unit>` (missing install). There is **no "next fire"** to re-exec it. |

> The canonical trap: `ourliberty-inbox-watcher.service` is `Type=simple` with
> `WantedBy=multi-user.target` and **no sibling timer**. Treating it like a
> timer-triggered oneshot — "daemon-reload, it'll re-exec on next fire" — leaves
> it on stale code indefinitely. This is the service-restart-after-merge gap.

---

## Manual remediation per class

Run from the repo root on the droplet (`ssh larry@134.209.44.80`,
`cd ~/agent-core`). Substitute `<unit>` (e.g. `ourliberty-inbox-watcher.service`).

### Missing install

**`.timer`:**
```bash
sudo cp systemd/<unit> /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now <unit>
```

**oneshot `.service`** (activated by its sibling timer — install + reload, let
the timer fire it):
```bash
sudo cp systemd/<unit> /etc/systemd/system/
sudo systemctl daemon-reload   # re-execs on its next timer fire
```

**long-running `.service`** (must actually start the daemon):
```bash
sudo cp systemd/<unit> /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now <unit>   # starts it now + enables at boot
```

### Content drift

**`.timer`** (re-anchor the schedule):
```bash
sudo cp systemd/<unit> /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart <unit>
```

**oneshot `.service`** (reload is enough — next fire re-execs with new content):
```bash
sudo cp systemd/<unit> /etc/systemd/system/
sudo systemctl daemon-reload   # oneshot re-execs on its next timer fire
```

**long-running `.service`** (reload alone keeps it on stale code — restart it):
```bash
sudo cp systemd/<unit> /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart <unit>
```

---

## Verify

After any remediation:
```bash
systemctl status <unit>
```

- A **timer** should show `active (waiting)` with a populated
  `Trigger:`/next-elapse line.
- A **oneshot service** typically shows `inactive (dead)` between fires — that
  is normal; confirm it is `enabled` and check its sibling timer's next fire.
- A **long-running service** should show `active (running)` with a recent
  start timestamp (proving the restart took).

---

## Activating auto-remediation

To let the healer fix drift itself (rather than just alert):
```bash
sudo systemctl edit ourliberty-heal-systemd-install-drift.service
# add under [Service]:
#   Environment="OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED=true"
sudo systemctl restart ourliberty-heal-systemd-install-drift.timer
```
The action class must also be present in
`config/auto-remediation-allowlist.json`.
