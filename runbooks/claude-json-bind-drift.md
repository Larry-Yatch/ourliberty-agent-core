# Runbook — `/home/larry/.claude.json` bind-mount drift (EROFS `[claude exit 1]`)

**Healer:** `scripts/heal_claude_json_bind_drift.py`
**Units:** `ourliberty-heal-claude-json-bind-drift.{service,timer}` (2-min timer)
**Memory:** `claude-json-erofs-readwrite`  •  **Origin incidents:** PR #470, recurrence 2026-06-26

## Symptom
A telegram bridge (beacon / forge / mirror / pulse, or any persistent claude
unit) suddenly replies a bare **`[claude exit 1]`** and looks offline, while its
unit file and the `/home/larry/.claude.json` carve-out are both correct. The
journal shows:

```
API Error: EROFS: read-only file system, open '/home/larry/.claude.json'
```

## Root cause
The claude-invoking units run `ProtectHome=read-only` and carve
`/home/larry/.claude.json` back to read-write via `ReadWritePaths=` (PR #470).
`ReadWritePaths=` on a **file** is a per-FILE **bind-mount** — it pins that
file's **inode** into the unit's mount namespace at unit-start.

`claude` writes `~/.claude.json` **in place** (open + truncate; same inode), so
it never breaks its own mount. But when something **outside** the namespace
**atomically replaces** the file (write-temp + `rename`, producing a **new
inode**) the running unit's bind-mount still points at the **old** inode. Path
resolution to `/home/larry/.claude.json` inside the namespace then falls through
to the read-only `/home` mount → the next open-for-write is **EROFS**.

Who replaces it from outside the namespace? Anything on the **host** (where
`/home` is writable): an interactive `claude` session on the droplet, a
self-update, or claude's own occasional repair/backup path. **No agent-core code
writes `~/.claude.json`** — so we cannot fix it at the source.

The only fix is to **restart the unit**, which rebuilds the namespace and
re-binds the **current** inode.

## Why this is a healer, not a unit-file change
You cannot make a **file** carve-out replacement-proof from the unit file:

| Option | Why it doesn't durably fix it |
| --- | --- |
| `ReadWritePaths=` (today) | file bind-mount → pins an inode → dangles on atomic-replace |
| `BindPaths=<file>` | also a file bind-mount → same pinning → still dangles |
| `CLAUDE_CONFIG_DIR` → relocate `.claude.json` into the safe `.claude/` **dir** | whether `.claude.json` follows `CLAUDE_CONFIG_DIR` is undocumented/version-dependent (GitHub claude-code #25762) — a fragile bet on a specific binary build |
| carve a directory instead of the file | `.claude.json`'s parent dir **is** `/home/larry` — can't carve it without defeating `ProtectHome` |

So we respond to the **symptom** (the mount is no longer writable in the
namespace) regardless of which process or claude version replaced the file.
The OAuth token lives in `~/.claude/.credentials.json` (a **directory**
carve-out, already replacement-safe); `~/.claude.json` is only non-secret cache
/state, so a unit restart is always safe.

## What the healer does (every 2 min)

### 1. Classify + probe every `ourliberty-*.service`

Classification asks **`Restart=`**, a property of the unit file:

- `Restart=always | on-failure | on-abnormal | on-success | on-watchdog | on-abort`
  → **MONITOR** (a daemon systemd keeps alive; its mount can dangle for hours).
- `Restart=no` → **EPHEMERAL_JOB** — a run-to-completion job. Its next
  activation gets a fresh namespace bound to the current inode, so a dangle heals
  itself; restarting one only SIGTERMs the live run. `ourliberty-cycle` is the
  only one in scope today, and it is exactly what was killed mid-`/cycle` on
  2026-07-30 and then false-paged for being `inactive` (its correct resting
  state).
- `Restart=` **absent or empty** → `skip-unknown`, NOT ephemeral. systemd
  normalises an omitted `Restart=` to `no` and never reports it empty, so empty
  means the property dump was malformed — the same bad-read treatment as
  `ReadWritePaths=` below. If you are debugging an incident and a real daemon is
  sitting in `skip-unknown`, suspect a truncated `systemctl show`, not a
  classification decision: the healer is refusing to descope it on a bad read.
- `Type` not in simple/notify/exec/idle/dbus/**forking** → `skip-oneshot`.
  `forking` counts as persistent: a double-forking daemon lives exactly as long
  as a `simple` one and its `MainPID` holds the same pinned bind-mount.
- `systemctl show` unreadable, or an unrecognised `Restart=` → `skip-unknown`
  (never silently descoped **and** never silently restarted).

**Unreadable is not a class.** An **absent** `Type=` or `ReadWritePaths=` — a
truncated property dump under dbus pressure — is `skip-unknown`, never
`skip-oneshot` or `skip-nocarve`. Those two buckets assert a *positive fact*
systemd told us, and a partial read told us nothing; a real daemon that lands in
either one has silently left coverage alongside the ~95 healers that were never
in it. Absent and **empty** are different: an empty `ReadWritePaths=` really does
mean "this unit carves nothing" and stays `skip-nocarve`.

> **Why not `TriggeredBy=*.timer`?** Because it fails open *and* closed.
> `TriggeredBy` is a reverse dependency that exists only while the triggering
> `.timer` is **loaded** in the manager — `systemctl disable --now` or `mask` on
> the timer empties it (verified on the droplet), silently returning the service
> to the repair path. And from the other side, attaching any companion `.timer`
> to a real daemon would have removed it from coverage permanently. `Restart=`
> is read from the unit file and is invariant under all of that. `TriggeredBy`
> is still read and printed in the classification log line as corroboration —
> never as a verdict.

**Probe** (both classes, when the unit is `active`, has a `MainPID`, and carves
the file) — `sudo -n nsenter -m -t <MainPID> -- python3 -c "open(O_RDWR)"`.
Entering the unit's mount namespace and opening the file `O_RDWR` (no create, no
truncate, never written) is the ground-truth test: a read-only mount fails
`open(2)` with **EROFS even for root**, so the probe can't false-pass.

- exit 0 → `healthy`
- exit 2 → EROFS → **dangled**
- namespace already gone mid-probe → `skip-nsgone` (benign race, re-probe next tick)
- anything else → `probe-error` (sudo revoked / nsenter missing) — never a restart

**Detection and repair are separate.** An EPHEMERAL unit is still probed, via a
function that has no route to the restart path at all (structural, not an `if`):
a dangle in an in-flight `/cycle` is recorded as `ephemeral-dangled` — a WARN
line and a tick-line counter — with **no DM and no restart**. A restart would end
that run exactly as the EROFS does, and the next fire rebinds anyway, so paging
would be pure toil; but "we chose not to act" and "we could not see" are
different things, and this is the difference.

**Coverage is observable in the tick line.** Every skip reason is its own
counter, so a daemon that dropped OUT of coverage lands in `skip-ephemeral` and
can never be confused with the ~95 healers in `skip-oneshot` that were never in
it. That is the runtime detector for the one new way to be silently descoped: a
genuinely persistent daemon that omits `Restart=`.

The build-time half is a repo lint in
`scripts/tests/test_heal_claude_json_bind_drift.py` requiring every persistent
carve-out unit to declare `Restart=` or sit in a `KNOWN_EPHEMERAL_UNITS`
allowlist. **Known prospective gap:** that lint reads the unit *file*, so it
skips a unit that omits `Type=` entirely even though systemd defaults such a unit
to `Type=simple` and the runtime classifier does see it. No unit in `systemd/`
omits `Type=` today.

> A richer detector — a `coverage=<units>` line naming the monitored set, with
> departures carried across ticks — needs the fleet-wide detect pass that can
> compute it in one `systemctl show` per unit. `main()` here is one `check_unit`
> call per unit, so that detector ships with the pass that feeds it rather than
> being bolted onto a loop that would read every unit twice.

### 2. Repair the dangled MONITOR units

`sudo -n systemctl restart --no-block <unit>`, settle, verify `is-active`, then
**re-probe the new MainPID** to confirm the file is writable again. DM the
outcome (routine self-heal → digest; failure → escalate). Restarts run behind
`restart_guard`'s cordon-and-drain so rebinding a unit that hosts Claude sessions
doesn't SIGTERM one mid-review; if a peer restarter already holds the cordon lock
the repair is skipped (`repair-skipped-peer-active`), no cooldown is burned, and
the next tick re-evaluates.

### Tick-line outcomes
`journalctl -u ourliberty-heal-claude-json-bind-drift` prints one `tick: …` line
per run with a **distinct counter per reason** (a daemon leaving coverage must
never be byte-identical to a healer that was never in scope):

`skip-oneshot` · `skip-ephemeral` · `skip-inactive` · `skip-nocarve` ·
`skip-unknown` · `skip-nsgone` · `healthy` · `ephemeral-dangled` ·
`ephemeral-probe-error` · `probe-error` · `rebound` · `repair-failed` ·
`cooldown-dangled` · `repair-skipped-peer-active`

### Alert subjects
`rebound:` (digest — a routine self-heal) · `still-dangled:` · `repair-failed:` ·
`probe-blind:`. All are translated in `config/alert-translations.json`.

### Guardrails
- **Kill-switch:** `~/agents/healers.disabled` disables detection AND repair.
- **Per-unit restart cooldown:** 15 min — one repair attempt per unit per window.
  Still-dangled inside the cooldown → escalate (structural problem), don't loop.
- **Ephemeral units are never restarted** — by construction, not by a guard: the
  probe-only path holds no reference to `restart_guard` or `restart_unit`.
- **Probe never writes:** `O_RDWR` only, immediately closed — content + mtime intact.
- **`sudo -n`:** a revoked NOPASSWD contract fails fast and DMs `probe-blind`,
  never wedges.

## Deploy
Hands-off. On merge + sync, `sync_agent_core.sh` fires `heal_systemd_install_drift.py`,
which `cp`s the new `.service`/`.timer` to `/etc/systemd/system`, `daemon-reload`s,
and `enable --now`s the timer (it carries `[Install] WantedBy=timers.target`).

## Verify it's live
```
systemctl list-timers ourliberty-heal-claude-json-bind-drift.timer
journalctl -u ourliberty-heal-claude-json-bind-drift -n 40   # look for `tick: …`
cat ~/agents/logs/heal-claude-json-bind-drift.log
```

## Manual fallback (if the healer is disabled/blind)
This is exactly what the healer automates:
```
# Confirm the dangle for a unit (root open-probe inside its namespace):
sudo nsenter -m -t "$(systemctl show -p MainPID --value ourliberty-beacon-bot)" \
  -- python3 -c "open('/home/larry/.claude.json','r+')"   # EROFS == dangled
# Fix:
sudo systemctl restart ourliberty-beacon-bot
```

## Tuning
- Timer cadence: `OnUnitActiveSec=` in the `.timer` (default 2 min — the window
  is user-facing chat, so keep it short).
- A future upgrade could make detection **event-driven** (an inotify watch for a
  `rename`/`create` of `~/.claude.json`, ignoring benign in-place modifies) to
  shrink the window to near-zero; the 2-min poll is the simple, proven baseline.
