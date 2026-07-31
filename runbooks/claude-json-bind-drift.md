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

Each tick runs three phases **in this order**.

### 1. Reconcile outstanding restarts (`reconcile_pending`)
Every restart the healer issues writes a **pending-verification entry** to the
state file **before** the restart command is sent (so a SIGTERM mid-restart
still leaves something reconcilable — Python's `finally` does not run on
SIGTERM). A restart that has not demonstrably landed inside the tick stays in
that ledger and is resolved on a later tick:

| what the unit looks like now | outcome |
| --- | --- |
| `active` + **new `InvocationID`** + new-namespace probe **OK** | `rebound` (digest DM) |
| `active` + new `InvocationID` + new-namespace probe **EROFS** | `still-dangled` (escalate) |
| `active` + new `InvocationID` but the re-probe never concluded, grace spent | `verify-inconclusive` — no DM, unit returns to the ordinary probe path |
| still not started **300 s** (`RESTART_LANDING_GRACE_S`) after the restart | `repair-did-not-land` (escalate). The DM distinguishes **no new invocation** (the start half never ran) from **a new invocation that died** (it came back and did not stay up) — do not collapse them, they send you to different halves of the journal |
| `systemctl show` **unreadable** for that whole 300 s | `verify-unreadable` (escalate) — its OWN subject. A failed read is not absence: the unit may be fine and the healer blind, so this must never be reported as "the bot is down" |
| unit no longer exists in systemd | `pending-gc` — dropped, INFO log, no DM |
| none of the above yet | `awaiting-verify` — keep waiting |

Reconciliation **owns** a unit's narrative until its entry closes: a unit with an
open entry is skipped by phase 2/3 (`awaiting-verify`), so one repair can never
produce two DMs. It reads the unit's systemd facts **directly** — there is no
"only if `ActiveState == active`" filter, which is what previously made a restart
that settled `failed` invisible to every later tick.

### 2. Classify + probe every `ourliberty-*.service`

Classification asks **`Restart=`**, a property of the unit file:

- `Restart=always | on-failure | on-abnormal | on-success | on-watchdog | on-abort`
  → **MONITOR** (a daemon systemd keeps alive; its mount can dangle for hours).
- `Restart=no` (or empty) → **EPHEMERAL_JOB** — a run-to-completion job. Its next
  activation gets a fresh namespace bound to the current inode, so a dangle heals
  itself; restarting one only SIGTERMs the live run. `ourliberty-cycle` is the
  only one in scope today, and it is exactly what was killed mid-`/cycle` on
  2026-07-30 and then false-paged for being `inactive` (its correct resting
  state).
- `Type` not in simple/notify/exec/idle/dbus → `skip-oneshot`.
- `systemctl show` unreadable, or an unrecognised `Restart=` → `skip-unknown`
  (never silently descoped **and** never silently restarted).

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

**Coverage is observable.** Each tick logs a `coverage=<units>` line, plus a
**WARN naming any unit that LEFT coverage and why** (`ourliberty-x.service LEFT
coverage: EPHEMERAL_JOB (Restart=no)`) and an INFO for any unit that entered it.
A departure is a transition **event**; sitting outside coverage is a **state**,
so the `coverage=` line also carries a standing `departed=<units>` list on
**every** later tick until the unit comes back or leaves systemd altogether.
(`state['coverage']` missing means "no baseline yet"; `[]` means "the baseline is
empty" — reading both as absent is what let a total collapse to zero monitored
units print a reassuring "baseline recorded" line forever.) That is the runtime
detector for the one new way to be silently descoped: a genuinely persistent
daemon that omits `Restart=`.

The build-time half is a repo lint in
`scripts/tests/test_heal_claude_json_bind_drift.py`. **Known prospective gap:**
that lint reads the unit *file*, so it skips a unit that omits `Type=` entirely
even though systemd defaults such a unit to `Type=simple` and the runtime
classifier does see it. No unit in `systemd/` omits `Type=` today; the runtime
`departed=` list is what covers it meanwhile.

### 3. Repair the dangled MONITOR units — ordered and budgeted

`sudo -n systemctl restart --no-block <unit>`, then gather **evidence**. The
verdict is never a wall-clock guess:

| verdict | evidence | what happens |
| --- | --- | --- |
| **LANDED** | `InvocationID` **changed** (or `MainPID` changed, when the invocation was unreadable) **and** a started state | re-probe the NEW MainPID → `rebound` / `still-dangled` / `awaiting-verify` |
| **IN_PROGRESS** | a confirmed pending `Job=`; or `deactivating`/`activating`/`reloading`; or identity unreadable | `restart-in-progress` — **no DM**, obligation left in the ledger for phase 1 of a later tick |
| **NOT_ENQUEUED** | identity **unchanged**, no pending job, and the restart shellout itself hung | `repair-not-enqueued` (escalate). No cooldown burned — nothing was restarted, so the next tick retries |
| **REJECTED** | the restart command was refused up front (rc ≠ 0, or sudo/systemctl missing) | `repair-failed` (escalate). This is the **only** path to that subject |

The `Job=` probe is what keeps the `After=`-ordered peers off the pager:
`ourliberty-outbox-notifier` and `ourliberty-spec-review-runner` are both
ordered `After=ourliberty-inbox-watcher.service`, whose claude loop takes ~90 s
to SIGTERM-drain, so they read `inactive` **with a job enqueued** for a minute or
more. That is in progress, not failure. (Same discriminator as
`heal_stale_daemon_code`; a repo-wide lint,
`scripts/tests/test_restart_verify_invariants.py`, requires it of every
`restart --no-block` verifier.)

Repairs run **cheap pass-through peers first, the drain-paying
`ourliberty-inbox-watcher.service` last**, and only while the remaining tick
budget (`TICK_BUDGET_S`, 90 % of the unit's `TimeoutStartSec`) covers the repair
**in full**. A repair that does not fit is `deferred` to the next tick — the
drain ceiling is never shortened to make one fit, because a shortened ceiling
force-restarts a live Claude session early. Deferral is silent the first couple
of times and then escalates (`repair-deferred`) so chronic starvation is not
invisible.

### Tick-line outcomes
`journalctl -u ourliberty-heal-claude-json-bind-drift` prints one `tick: …` line
per run with a **distinct counter per reason** (a daemon leaving coverage must
never be byte-identical to a healer that was never in scope):

`skip-oneshot` · `skip-ephemeral` · `skip-inactive` · `skip-nocarve` ·
`skip-unknown` · `skip-nsgone` · `healthy` · `healed-by-peer` · `ephemeral-dangled` ·
`ephemeral-probe-error` · `probe-error` · `awaiting-verify` ·
`verify-inconclusive` · `restart-in-progress` · `rebound` · `still-dangled` ·
`repair-failed` · `repair-not-enqueued` · `repair-did-not-land` ·
`verify-unreadable` · `cooldown-dangled` · `repair-skipped-peer-active` · `deferred` · `pending-gc`

`deferred=N` is printed even when zero.

### Alert subjects
`rebound:` (digest — the only healed=True record, and only with a confirmed
new-namespace probe) · `still-dangled:` · `repair-failed:` ·
`repair-not-enqueued:` · `repair-did-not-land:` · `verify-unreadable:` ·
`repair-deferred:` · `probe-blind:`. All are translated in
`config/alert-translations.json`.

### Guardrails
- **Kill-switch:** `~/agents/healers.disabled` disables detection AND repair.
- **Per-unit restart cooldown:** 15 min — one repair attempt per unit per window.
  It RATE-LIMITS repair, it does not stop it: a mount that stays dangled is
  retried every 15 min indefinitely, because the mount is a live outage and a
  later restart may win. What is capped is the *telling* — every arm that can
  prove a dangle goes through `escalate_still_dangled`, which gates the DM on
  the 6 h `ESCALATION_COOLDOWN_SEC`. The `still-dangled` DM must therefore never
  claim the healer "stopped restarting"; it says it keeps retrying and that the
  message is what is rate-limited.
- **Pre-flight re-confirm:** detection runs across the whole fleet *before* the
  ordered repair pass and the agent-hosting unit is repaired LAST, so evidence
  can be minutes old by the time a restart fires. `reconfirm_before_restart`
  re-reads the unit first and, only if a peer restarted it since detection
  (new `InvocationID` → new namespace), re-probes before restarting again →
  `healed-by-peer`. An unchanged invocation is still dangled by construction, so
  that case pays no probe.
  A restart that was never enqueued does **not** burn it.
- **Probe never writes:** `O_RDWR` only, immediately closed — content + mtime intact.
- **`sudo -n`:** a revoked NOPASSWD contract fails fast and DMs `probe-blind`,
  never wedges.
- **No second lock.** Peer restarters (systemd's own `Restart=`, watchdog,
  heal_stale_daemon_code, medic) can restart these units concurrently;
  `restart_guard` serialises only `ourliberty-inbox-watcher.service`. That is
  handled with **evidence, not exclusion**: any new invocation counts as a
  landing (the mount is rebound either way) and the log says causation is
  unproven rather than claiming it.

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
The `tick:` line now carries **per-reason** skip counters and always ends with
`deferred=N` (see "Tick-line outcomes" above), so the old single `skip=98`
bucket is gone — that is the intended change, not a regression. The `coverage=`
line on the line above names the units currently monitored; a `LEFT coverage:`
WARN is the signal that a daemon dropped out of scope and why.

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
