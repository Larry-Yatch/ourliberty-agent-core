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
For each `ourliberty-*.service` that is **persistent** (`Type` simple/notify/exec/idle),
**active** with a `MainPID`, and carves `/home/larry/.claude.json`:

1. **Probe** — `sudo -n nsenter -m -t <MainPID> -- python3 -c "open(O_RDWR)"`.
   Entering the unit's mount namespace and opening the file `O_RDWR` (no create,
   no truncate, never written) is the ground-truth test: a read-only mount fails
   `open(2)` with **EROFS even for root**, so the probe can't false-pass.
   - exit 0 → writable (healthy, no-op)
   - exit 2 → EROFS → **dangled**
   - anything else → probe-error (sudo revoked / nsenter missing) — never a restart
2. **Repair** (on EROFS) — `sudo -n systemctl restart --no-block <unit>`, settle,
   verify `is-active`, then **re-probe the new MainPID** to confirm the file is
   writable again. DM the outcome (routine self-heal → digest; failure → escalate).

Oneshot (timer-driven) units are **skipped** — each activation spawns a fresh
namespace bound to the current inode, so they can't carry a stale mount.

### Guardrails
- **Kill-switch:** `~/agents/healers.disabled` disables detection AND repair.
- **Per-unit restart cooldown:** 15 min — one repair attempt per unit per window.
  Still-dangled inside the cooldown → escalate (structural problem), don't loop.
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
