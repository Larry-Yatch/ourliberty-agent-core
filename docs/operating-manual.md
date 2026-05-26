# Operating Manual — Larry's Agent OS

This is the day-to-day manual for running, using, and troubleshooting Larry's agent system. Read top-to-bottom on first pass. After that it's a reference — jump to whatever section you need.

**Last updated:** 2026-05-11 (Phase D2 — shared inbox watcher live; all 4 agents reachable via Telegram + JSON inbox dispatch; `/cycle` running every 4h with cost capture + auto-commit)

---

## How this document is organized

This file has two parts. They serve different readers, so they're kept distinct.

- **Part I — Operating Manual (§0 through Appendix C):** Reference for using the system today. Organized by topic. Updated in place each phase. Read this when you need to do something or diagnose something.
- **Part II — Build Narrative & Decisions Log:** Chronological story of how the system came to be. Phase-by-phase, append-only. Read this when you need to understand *why* something is the way it is, or what we ruled out along the way.

When we ship a new phase, both parts get updates: Part I gets new operational content woven in; Part II gets a new phase entry appended.

---

# Part I — Operating Manual

---

## 0. The 30-second mental model

```
   Two ways to dispatch work into the system:

   1. INTERACTIVE (Telegram) — for conversation with one agent
   ┌────────────────┐    Telegram     ┌──────────────────────┐
   │  Your phone    ├────────────────>│  Telegram's servers  │
   └────────────────┘                 └──────────┬───────────┘
                                                 │ getUpdates poll
                                                 ▼
                              ┌─────────────────────────────────┐
                              │  ourliberty-<agent>-bot.service │
                              │  (1 per agent: beacon/forge/    │
                              │   mirror/pulse — systemd-       │
                              │   managed, auto-restart)        │
                              │   spawns: claude --print        │
                              │   --resume in agent's CWD       │
                              └─────────────────────────────────┘

   2. SCHEDULED / AUTONOMOUS — for inter-agent and timed work
   ┌─────────────────────────────┐
   │ ourliberty-cycle.timer (4h) │ → Pulse runs /cycle Health Check
   │ ourliberty-sync.timer (1h)  │ → pulls origin/main into ~/agent-core
   │ ...health.timer (30m)       │ → enforces working-copy discipline
   └─────────────────────────────┘

   3. INTER-AGENT (D2) — for one agent to assign work to another
   ┌──────────────────────────────────────────────────────────────┐
   │  ~/agents/inboxes/<agent>/<task>.json                        │
   │                       │ polled every 5s                      │
   │                       ▼                                      │
   │  ourliberty-inbox-watcher.service                            │
   │   • validates task (dispatch_validator.py)                   │
   │   • acquires lease "inbox:<agent>" (one per agent in flight) │
   │   • spawns: claude --print --model <inbox_model> in CWD      │
   │   • writes ~/agents/outboxes/<agent>/<task>.json             │
   │   • appends ~/agents/blackboard/costs.jsonl                  │
   │   • archives task to inboxes/<agent>/.archive/               │
   └──────────────────────────────────────────────────────────────┘

   All three paths run on:
   Droplet: ourliberty-agents-01 @ 134.209.44.80
   ~/credentials/.env.larry  ← bot tokens (mode 600, never committed)
   ~/agent-core/             ← source repo (synced from origin/main)
   ~/agents/                 ← runtime state, logs, inboxes, outboxes
```

**In English:** Three ways work happens in the system.
1. You Telegram an agent → its bot runs Claude Code with that agent's prompts → you get a reply.
2. A timer fires → systemd starts a script → Claude Code runs `/cycle` or `sync` → result is journaled.
3. One agent (or you) drops a JSON file into another agent's inbox → the watcher picks it up within 5s → that agent runs the task → result lands in its outbox.

**Key idea:** Every agent has the same shape (prompt files, a Telegram bot, an inbox/outbox). What changes between agents is the persona (the markdown in `agents/<name>/`) and the model routing (`config/agent-models.json`). The infrastructure is uniform; the personalities are what make Beacon different from Forge.

---

## 1. The pieces, named

### Hosting & access

| Piece | What it is | Where it lives |
|---|---|---|
| **Droplet** | The Linux virtual machine that hosts everything. Always-on. | DigitalOcean, NYC3 region |
| **IP address** | The droplet's address on the internet. | `134.209.44.80` |
| **Domain** | A friendly name pointing to the droplet. | `agents.ourliberty.dev` (DNS A record in Cloudflare) |
| **SSH** | How you log into the droplet from your Mac. | `ssh larry@134.209.44.80` |
| **`larry` user** | Your account on the droplet. Has sudo (admin) access without password prompts. | `/home/larry/` on the droplet |

### Directories (on the droplet)

| Piece | What it is | Where it lives |
|---|---|---|
| **`~/agent-core/`** | The source code repo, cloned to the droplet. Synced from `origin/main` every hour. | `/home/larry/agent-core/` |
| **`~/agents/`** | Runtime state — logs, memory, inboxes, outboxes, blackboard. **Never touched by `git pull`.** | `/home/larry/agents/` |
| **`~/agents/inboxes/<agent>/`** | Task drop zone. Watcher polls every 5s. Subdirs: `.archive/` (consumed), `.invalid/` (rejected by validator + `.reason` sidecar). | `/home/larry/agents/inboxes/{beacon,forge,mirror,pulse}/` |
| **`~/agents/outboxes/<agent>/`** | Where the watcher writes the agent's reply + metadata after running a task. | `/home/larry/agents/outboxes/{beacon,forge,mirror,pulse}/` |
| **`~/agents/blackboard/`** | Shared inter-agent files. Most importantly: `costs.jsonl` (every Claude invocation), `pulse-escalations.json` (Pulse's open findings), `agent-core-sync.json` (sync timer status). | `/home/larry/agents/blackboard/` |
| **`~/agents/state/`** | Runtime state — bot session continuity (`*_telegram_sessions.json`), dispatch leases, the cycle lock. | `/home/larry/agents/state/` |
| **`~/agents/logs/`** | Per-bot Telegram bot logs, watcher log, cycle log. (systemd units also write to journalctl.) | `/home/larry/agents/logs/` |
| **`~/credentials/`** | Where secrets live. Mode 700 (only you can read). | `/home/larry/credentials/.env.larry` (mode 600) |

### Agents (personas + bots)

All four agents follow the same shape: a `~/agent-core/agents/<name>/` directory with 6 markdown prompt files (CLAUDE / IDENTITY / SOUL / TOOLS / USER / MEMORY) plus a systemd-managed Telegram bot.

| Agent | Role | Telegram model | Inbox model | Status |
|---|---|---|---|---|
| **Beacon** 🪔 | Strategy / Architect — drafts specs from your intent | Opus | Opus | Live |
| **Forge** ⚒️ | Builder — turns approved specs into code & PRs | Sonnet | Opus | Live (auto preflight → build → PR via inbox dispatch as of D3 commit 4b, 2026-05-12) |
| **Mirror** 🪞 | Adversarial Reviewer — gates merges, severity-tags findings | Sonnet | Opus | Live (PR-aware dispatch coming in D4) |
| **Pulse** 💓 | Self-healing Observer — runs `/cycle`, escalates, dispatches fixes | Sonnet | Sonnet | Live (auto-cycle every 4h) |

Future agents (per North Star / build plan): **Aide** (EA, Phase E), **Scout** (researcher, Phase 2), **Compass** (planner, Phase 2), **Ledger** (cost/CFO, Phase F+). Personas not yet authored.

### Infrastructure (D1–D3)

| Piece | What it is | Where it lives |
|---|---|---|
| **systemd bot services** | One service per agent: `ourliberty-{beacon,forge,mirror,pulse}-bot.service`. Auto-restart on crash, auto-start on boot. | `/etc/systemd/system/` (source in `~/agent-core/systemd/`) |
| **`/cycle`** | Pulse's iteration spec — what to check, what to fix, how to journal. Run by `ourliberty-cycle.timer` every 4h. | `~/agent-core/runbooks/cycle-prompt.md` |
| **`run_cycle.sh`** | Wraps `claude --print` for `/cycle`. Also (D2) captures cost to `costs.jsonl` and auto-commits Pulse's journal/MEMORY changes back to the repo. | `~/agent-core/scripts/run_cycle.sh` |
| **`sync_agent_core.sh`** | Atomic-swap sync from `origin/main` to live runtime. Run by `ourliberty-sync.timer` every 1h. | `~/agent-core/scripts/sync_agent_core.sh` |
| **`agent_core_health_check.py`** | Enforces working-copy discipline (always on `main`, always clean). Run by `ourliberty-agent-core-health.timer` every 30m. | `~/agent-core/scripts/agent_core_health_check.py` |
| **Inbox watcher (D2)** | `ourliberty-inbox-watcher.service`. One process, four threads (one per agent). Polls inboxes every 5s; one in-flight task per agent (lease-protected). Calls `agent_runner.run_claude` (D2.5) for the actual subprocess. | `~/agent-core/scripts/inbox_watcher.py` |
| **Outbox notifier (D3-2)** | `ourliberty-outbox-notifier.service`. Watches `~/agents/outboxes/*` and routes results back to the originating agent (back-channel for `pulse → beacon → pulse` dialogue, `forge → beacon` PR notifications, etc.). Also drives marker-driven routing for Forge's preflight markers (D3-4a) and the build-phase re-dispatch after PROCEED (D3-4b). | `~/agent-core/scripts/outbox_notifier.py` |
| **`safe_write_inbox.py`** (D3-prep) | Validated atomic write to an agent's inbox. Every dispatcher (Beacon's bot, the notifier, etc.) routes writes through this — filename guard, schema validation, routing validation, atomic write, audit log to `~/agents/logs/routing-events.jsonl`. | `~/agent-core/scripts/safe_write_inbox.py` |
| **`routing_validator.py`** (D3-prep, extended in 4b) | Two-layer route check: hard topology `(source, target)` allow-list + soft IDENTITY.md reroute. 4b added `check_target_repo` for the `allowed_repos` allow-list per agent. | `~/agent-core/scripts/routing_validator.py` |
| **`forge_preflight_handler.py`** (D3-4a) | Pure-logic marker library: parses Forge's `PROCEED` / `CLARIFY_REQUEST` / `REJECT` block, validates required fields, evaluates the clarification budget. Stateless — all envelope state rides on the task. | `~/agent-core/scripts/forge_preflight_handler.py` |
| **`worktree_manager.py`** (D3-4b) | Keyed-reuse worktree manager for Forge dispatches. Same task_id → same worktree path across all dispatches (preflight, CLARIFY round-trip, build). Worktrees live at `~/agent-worktrees/wt-<agent>-<task_id>/` (NOT `/tmp`, see Section 4 note on `PrivateTmp`). Idempotent branch checkpoint with empty WIP commit pushed to origin. | `~/agent-core/scripts/worktree_manager.py` |
| **`cleanup_stale_worktrees.py`** (D3-4b, ports upstream's Gap 10) | Daily sweep — removes `~/agent-worktrees/wt-*` directories older than 24h. Skips worktrees referenced by the in-flight registry (long Read-heavy builds don't get reaped mid-flight). Run by `ourliberty-cleanup-stale-worktrees.timer` every 24h. | `~/agent-core/scripts/cleanup_stale_worktrees.py` |
| **`dispatch_sentinel.py`** (D3 commit 5, ports upstream's Gap 2) | Stall detection — three scans per run: inbox stalls (> 3h unpicked), in-flight stalls (past per-model threshold), stale leases (heartbeat stopped). Disk-only alerts to `~/agents/blackboard/sentinel-alerts.jsonl` with dedup state in `state/dispatch-sentinel.json`. Does NOT auto-cancel; the cancel-marker pattern (`blackboard/cancel-task-<stem>.json`) is the explicit kill switch. Run by `ourliberty-dispatch-sentinel.timer` every 10m. | `~/agent-core/scripts/dispatch_sentinel.py` |
| **Self-healing healers (D2.5)** | Seven adopted from upstream's `gm-heal-*` family — abandoned inbox tasks, blocked inbox age, empty inbox files, recovery-already-merged, restart-dedup obsolete, silent-loop death, zombie main workers. Each is a `ourliberty-heal-*.{service,timer}` pair. | `~/agent-core/systemd/ourliberty-heal-*` |
| **`dispatch_validator.py`** | Pre-dispatch validation: `task_id` required, `prompt` ≥ 100 chars, `source` in allowed set, `phase` ∈ {`preflight`, `build`}, `intent` ∈ {`ack-proceed`, `clarify`, `clarification-response`, `clarification-exhausted`, `reject`, `result-notification`, `dead-letter`, `marker-error`}. Stricter than HANDSHAKE-SCHEMA. | `~/agent-core/scripts/dispatch_validator.py` |
| **`dispatch_lease.py`** | Restart-safe concurrency primitive (flock + nonce + TTL + boot-id PID-reuse guard). Used by the watcher to ensure one task per agent at a time. | `~/agent-core/scripts/dispatch_lease.py` |
| **`agent-models.json`** | Per-agent model routing. Tells the watcher which Claude model to use for each agent's inbox tasks. **4b** added per-agent `worktree_enabled` and `allowed_repos` fields (Forge has both set). | `~/agent-core/config/agent-models.json` |
| **Forge worktree directory** (D3-4b) | Persistent base for Forge's per-task isolated git worktrees. Auto-created on first use; daily cleanup script reaps entries > 24h old. | `~/agent-worktrees/wt-forge-<task_id>/` |
| **HANDSHAKE-SCHEMA** | JSON schema for inbox task files. Optional fields are documented here; required ones live in `dispatch_validator.py`. | `~/agent-core/shared/HANDSHAKE-SCHEMA.json` |
| **`.env.larry`** | Environment file with your secrets (4× bot tokens, allowed chat IDs). | `~/credentials/.env.larry` |

---

## 2. Daily use — Telegram chat with the agents

### From your phone, anywhere

Open Telegram. Each agent has its own bot — one for Beacon, Forge, Mirror, Pulse. Find the bot you want to talk to and send a message. Wait 5–30 seconds. Reply lands.

That's it. **You don't need to be at your computer.** As long as the droplet and the bot service are running (they're systemd-managed with `Restart=on-failure`), Telegram works.

### Conversation continuity (per bot)

Each bot uses `claude --resume` per chat. That means **all your messages to a given agent are in one continuing conversation**, even days apart. Each agent has her own continuity, separate from the others.

Session IDs live at `~/agents/state/<agent>_telegram_sessions.json` on the droplet (one entry per chat ID per agent).

### When to start a new conversation

Almost never. The continuity is the whole point. If you ever need to truly start fresh with one agent:

```bash
ssh larry@134.209.44.80
rm ~/agents/state/beacon_telegram_sessions.json   # or forge/mirror/pulse
sudo systemctl restart ourliberty-beacon-bot.service
```

Next message starts a new session.

### Who to talk to about what

| Question / intent | Talk to |
|---|---|
| "I have an idea — help me think through it / draft a spec" | **Beacon** 🪔 |
| "Implement this spec" (after Beacon has it drafted) | **Forge** ⚒️ |
| "Review this PR / does this match the spec?" | **Mirror** 🪞 |
| "How's the system doing? What did /cycle find?" | **Pulse** 💓 (or just read `runbooks/cycle-journal.md`) |
| Operational ops (restart a bot, check costs, ssh in) | Nobody — do it yourself or ask me in Claude Code |

If you ask the wrong agent (e.g. ask Forge for a strategic decision), they'll usually redirect you. The personas are written to know their lane.

### What's still done by humans (i.e., you) today

- **Approving a Beacon-drafted plan before Forge gets it.** D3 will automate the DM-Larry-for-approval flow; until then, you copy/paste from Beacon's chat into Forge's chat.
- **Dispatching a Pulse finding into Beacon's queue.** D2 (this phase) gives Pulse the *format* to dispatch via inbox JSON, but the auto-dispatch from `/cycle` output isn't wired yet — Pulse writes escalations to `~/agents/blackboard/pulse-escalations.json` and a human reads them.
- **Telling Mirror to review a Forge PR.** D4 will auto-trigger this; until then, you Telegram Mirror with the PR number.

---

## 3. SSH — getting onto the droplet

### The basic command

From any terminal on your Mac:

```bash
ssh larry@134.209.44.80
```

You should land in a Linux prompt that looks like:

```
larry@ourliberty-agents-01:~$
```

That `$` is your prompt — anything you type from there runs on the droplet, not your Mac. Type `exit` (or press Ctrl-D) to leave.

### First time on a new computer

If you SSH from a different Mac or a fresh OS install, SSH will prompt:

```
The authenticity of host '134.209.44.80' can't be established.
ED25519 key fingerprint is SHA256:...
Are you sure you want to continue connecting (yes/no)?
```

Type `yes`, press Enter. It saves the host key to `~/.ssh/known_hosts` so it never asks again from that computer.

### Surviving idle disconnects

By default, your SSH session may drop after a few minutes of no activity (router NAT timeout, etc.). To prevent that, on your Mac, edit (or create) `~/.ssh/config`:

```bash
# On your MAC (not the droplet):
mkdir -p ~/.ssh && chmod 700 ~/.ssh
cat >> ~/.ssh/config <<'EOF'

Host *
    ServerAliveInterval 60
    ServerAliveCountMax 5
EOF
chmod 600 ~/.ssh/config
```

Now SSH sends a keepalive every 60s, and gives up after 5 missed ones (5 minutes total). Future sessions stay alive while you're reading.

### What to do if SSH fails

| Error | What it means | Fix |
|---|---|---|
| `Connection refused` | The droplet's SSH daemon isn't running, or the firewall is blocking you | Check droplet status in DO dashboard. If droplet is up, `ssh` from a different network (your IP might've been blocked) |
| `Connection timed out` | Can't reach the droplet at all | Check `ping 134.209.44.80`. If ping fails, droplet might be off — check DO dashboard. |
| `Permission denied (publickey)` | SSH key isn't being recognized | Make sure you're using `larry@` not `root@`. Verify `~/.ssh/id_ed25519` exists on your Mac. |
| `Read from remote host... Connection reset by peer` | Idle timeout — your session was killed | Just SSH back in. Set up `ServerAliveInterval` (above) to prevent next time. |

---

## 4. Service lifecycle — all the systemd units

The system runs as a collection of systemd-managed services. They survive droplet reboots, auto-restart on crash, and are started/stopped/checked the same way.

### The cast

| Unit | What it does | Cadence |
|---|---|---|
| `ourliberty-beacon-bot.service` | Beacon Telegram bot | continuous |
| `ourliberty-forge-bot.service` | Forge Telegram bot | continuous |
| `ourliberty-mirror-bot.service` | Mirror Telegram bot | continuous |
| `ourliberty-pulse-bot.service` | Pulse Telegram bot | continuous |
| `ourliberty-inbox-watcher.service` | Shared inbox watcher (all 4 agents). Calls `worktree_manager.ensure_worktree_for_task` for agents with `worktree_enabled` (Forge). | continuous, polls 5s |
| `ourliberty-outbox-notifier.service` (D3-2 / 4a / 4b) | Back-channel router for outbox results. Drives Forge preflight markers + post-PROCEED build-phase re-dispatch + dead-letter cascade. | continuous, polls 5s |
| `ourliberty-cycle.timer` → `.service` | `/cycle` Health Check Suite (Pulse on Sonnet) | every 4h |
| `ourliberty-sync.timer` → `.service` | Pull `origin/main` into `~/agent-core/` | every 1h |
| `ourliberty-agent-core-health.timer` → `.service` | Working-copy discipline check | every 30m |
| `ourliberty-watchdog.timer` → `.service` | Broad system health monitor with auto-recovery *(disabled; D2.5 criterion met but the underlying `watchdog.py` still has GM-era service-name hard-coding — pending adapter rewrite, separate from D3 commit 5 per the B option Larry signed off 2026-05-12)* | every 5m |
| `ourliberty-heal-*.timer` → `.service` (×7) | Self-healing healers (D2.5) — abandoned-inbox-tasks, blocked-inbox-age, empty-inbox-files, recovery-already-merged, restart-dedup-obsolete, silent-loop-death, zombie-main-workers. | every 5–15 min each |
| `ourliberty-cleanup-stale-worktrees.timer` → `.service` (D3-4b) | Daily sweep of `~/agent-worktrees/wt-*` (24h grace; skips in-flight). | every 24h |
| `ourliberty-dispatch-sentinel.timer` → `.service` (D3 commit 5) | Stall detection — flags inbox tasks > 3h old, in-flight tasks past per-model threshold, leases with stale heartbeats. Disk-only alerts to `~/agents/blackboard/sentinel-alerts.jsonl`. Does NOT kill stalled tasks. | every 10m |
| `ourliberty-ledger.timer` → `.service` | Ledger weekly cost report (see §10.1). Pure-Python compute via `scripts/ledger_weekly.py`; no LLM in the loop. | weekly, Mon 07:00 UTC |

> **Note on `PrivateTmp`:** the inbox-watcher and outbox-notifier services run with `PrivateTmp=yes` (a default systemd hardening for these services). Each service gets a private `/tmp` namespace that's invisible to host shell + other services + cleanup. Forge's worktrees therefore live at `~/agent-worktrees/`, NOT `/tmp/wt-*` — persistent across service restarts, visible across services, and reachable by the cleanup timer. Both services have `~/agent-worktrees` in `ReadWritePaths`.

### What's running right now?

```bash
ssh larry@134.209.44.80
systemctl list-units 'ourliberty-*' --type=service
systemctl list-timers 'ourliberty-*'
```

### Is a specific service running?

```bash
systemctl status ourliberty-beacon-bot.service       # or forge/mirror/pulse/inbox-watcher/...
```

Look for `Active: active (running)` and a recent `Main PID:`.

### Start / stop / restart

```bash
sudo systemctl start ourliberty-beacon-bot.service
sudo systemctl stop ourliberty-beacon-bot.service
sudo systemctl restart ourliberty-beacon-bot.service

# After updating a unit file (e.g. you edited it in the repo):
sudo cp ~/agent-core/systemd/ourliberty-beacon-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ourliberty-beacon-bot.service
```

### Pulling new code and restarting

When code changes hit `origin/main`, the hourly sync timer picks them up automatically within ~60 min. If you want it sooner, or you want to restart a service after the pull:

```bash
ssh larry@134.209.44.80
cd ~/agent-core && git pull --ff-only
# If bot Python changed:
sudo systemctl restart 'ourliberty-*-bot.service'
# If inbox_watcher.py changed:
sudo systemctl restart ourliberty-inbox-watcher.service
# If only agent persona .md files changed: nothing — next invocation picks them up
```

### After a droplet reboot

Nothing for you to do. All services are `enabled` (auto-start on boot). Telegram messages sent during the boot window are queued by Telegram for ~24 hours and delivered when bots come back.

To confirm everything came up after a reboot:

```bash
ssh larry@134.209.44.80
systemctl list-units 'ourliberty-*' --type=service
# All should show "active (running)"
```

### How to know if the droplet itself is up

From your Mac:

```bash
ping -c 3 134.209.44.80
```

Replies → droplet alive. Timeouts → check the DigitalOcean web dashboard.

---

## 5. tmux — for ad-hoc droplet work

**tmux** is a "terminal multiplexer" — it keeps a terminal session running on the droplet even after you disconnect. The agent bots no longer use it (they're systemd services now), but tmux is still useful when you want to run a long manual task on the droplet without keeping your laptop tethered.

### The 5 commands

| Command | What it does |
|---|---|
| `tmux ls` | List all running tmux sessions |
| `tmux new -s scratch` | Start a new session called `scratch` |
| `tmux attach -t scratch` | Re-attach to it later |
| Press `Ctrl-b` then `d` (sequentially) | Detach (leaves it running, returns you to your normal shell) |
| `tmux kill-session -t scratch` | Stop the session |

Use case: starting a `tail -f journalctl -u ourliberty-*-bot` on the droplet so you can keep an eye on it while you do other things, or running a long Python interactive session for debugging.

---

## 6. Logs — where to look and what to expect

Two log surfaces:
1. **`journalctl`** — the systemd journal, the canonical log for every service. Persisted across reboots, queryable by unit + time.
2. **Per-service log files** in `~/agents/logs/` — what the Python scripts write directly. Same content as journalctl for bots; a useful tail target.

### The most-used commands

```bash
# Tail one service live
journalctl -u ourliberty-beacon-bot.service -f
journalctl -u ourliberty-inbox-watcher.service -f
journalctl -u ourliberty-cycle.service -f

# Last 100 lines from a service (no follow)
journalctl -u ourliberty-pulse-bot.service -n 100 --no-pager

# What's happened across ALL ourliberty services in the last hour
journalctl --since "1 hour ago" -u 'ourliberty-*' --no-pager

# Search for errors across everything
journalctl --since "1 day ago" -u 'ourliberty-*' --no-pager | grep -i error

# File-based tail (alternative)
tail -f ~/agents/logs/beacon_telegram_bot.log
tail -f ~/agents/logs/inbox_watcher.log
tail -f ~/agents/logs/cycle.log
```

### What "normal" looks like

**Bot:**
```
[2026-05-08T19:25:39-0600] Beacon bot starting (cwd=/home/larry/agent-core/agents/beacon, allowed=[7998341473])
[2026-05-08T19:26:14-0600] <- 7998341473: 'Hello Beacon — read in.'
[2026-05-08T19:26:39-0600] -> 7998341473: 'Read in. Beacon, strategy/architect for Larry...'
```
- `<-` incoming from you. `->` reply going back.

**Inbox watcher:**
```
[2026-05-11T22:00:23+00:00] inbox_watcher: [pulse] start task=d2-smoke-... model=claude-sonnet-4-6 file=d2-smoke-....json
[2026-05-11T22:00:27+00:00] inbox_watcher: [pulse] done task=d2-smoke-... success=True duration=3.92s cost=$0.033
```

**Cycle:**
```
[2026-05-11T...] run_cycle: Starting /cycle iteration; PULSE_DIR=...
[2026-05-11T...] run_cycle: /cycle iteration completed successfully
[2026-05-11T...] run_cycle: cost record appended to /home/larry/agents/blackboard/costs.jsonl
[2026-05-11T...] run_cycle: auto-commit: pushed to origin/main
```

### What's NOT normal

- `claude exit 1` followed by `No conversation found with session ID:` → known issue (cold-start bug, all 4 bots, watch-listed by Pulse). Bot auto-retries without `--resume` and recovers. Repeat occurrences (≥3 cycles) trigger a permanent fix dispatch.
- `validator rejected ...: prompt too short` in the inbox watcher log → a dispatcher wrote a bad task. The task is now in `.invalid/` with a `.reason` sidecar. Read the sidecar to see what was wrong.
- `ignored unauthorized chat 1234567` → Someone other than you tried to talk to a bot. Your `TELEGRAM_ALLOWED_CHAT_IDS` correctly blocked them.
- `URL error... timed out` → Network blip with Telegram. Bot retries automatically.
- `[<Agent> timed out after 10 min]` → A single message took 10+ min. Either it was heavy or something hung. Restart that bot if recurring.
- `status=226/NAMESPACE` in `journalctl` → systemd hardening blocked a path the service needs. Look for an offending `ReadWritePaths` line in the unit file. (We hit this once already with `~/.config/anthropic` — fixed.)

### Log rotation

systemd's journal rotates automatically. The file-based logs in `~/agents/logs/` don't yet. After months of use:

```bash
# Manually archive a noisy log
mv ~/agents/logs/beacon_telegram_bot.log ~/agents/logs/beacon_telegram_bot.log.$(date +%Y%m%d)
sudo systemctl restart ourliberty-beacon-bot.service   # picks up the new file
```

(A small follow-up is to add a `logrotate.d` config for `~/agents/logs/*.log`.)

---

## 7. Common operations

### Pulling new code from GitHub

When I push changes (new agents, bug fixes, new scripts), you pull them onto the droplet:

```bash
ssh larry@134.209.44.80
cd ~/agent-core
git pull
```

If the change touches the bot's Python code, restart the bot:

```bash
bash ~/agent-core/scripts/beacon_telegram_bot.sh
```

If the change only touches Beacon's prompt files (`agents/beacon/*.md`), **you don't need to restart**. The next time Beacon runs (next message), Claude Code reads the new prompt files automatically.

### Updating a credential in `.env.larry`

```bash
nano ~/credentials/.env.larry
```

Edit the file (use arrow keys, type to change values). Save with **Ctrl-O** then **Enter**. Exit with **Ctrl-X**.

After changing a bot token or `TELEGRAM_ALLOWED_CHAT_IDS`, restart the bot to pick up the new value:

```bash
bash ~/agent-core/scripts/beacon_telegram_bot.sh
```

### Adding a new authorized chat ID

For example, if you want a partner to also be able to talk to Beacon:

```bash
nano ~/credentials/.env.larry
# Find the line: TELEGRAM_ALLOWED_CHAT_IDS=7998341473
# Change to:    TELEGRAM_ALLOWED_CHAT_IDS=7998341473,123456789
# (Comma-separated, no spaces)
# Save: Ctrl-O, Enter, Ctrl-X
bash ~/agent-core/scripts/beacon_telegram_bot.sh
```

### Checking which version of the code is running

```bash
cd ~/agent-core
git log -1 --oneline
```

Shows the latest commit on your droplet. Compare to GitHub's `main` branch to see if you're behind. (The hourly sync timer should keep you within ~60 min of `origin/main`.)

### Dispatching a task to an agent via the inbox

When you want an agent to do something but you're not at Telegram (or you want the result in a JSON file, not a chat reply), drop a task into the agent's inbox. The watcher picks it up within 5s.

```bash
ssh larry@134.209.44.80
TASK_ID="my-task-$(date -u +%Y%m%dT%H%M%SZ)"
cat > ~/agents/inboxes/beacon/${TASK_ID}.json <<EOF
{
  "task_id": "${TASK_ID}",
  "source": "larry",
  "dedup_identity": "my-task-canonical-slug",
  "prompt": "<at least 100 chars of substantive context — what you want, why, success criteria>",
  "timeout": 3600
}
EOF
```

What happens next:
- Within 5s, the watcher validates the task and starts the agent.
- The agent runs in its own CWD with its persona loaded.
- The result lands at `~/agents/outboxes/<agent>/${TASK_ID}.json`.
- The original task is moved to `~/agents/inboxes/<agent>/.archive/`.
- A cost record is appended to `~/agents/blackboard/costs.jsonl`.

If you got the format wrong (prompt too short, `task_id` missing, etc.), the file lands in `~/agents/inboxes/<agent>/.invalid/` with a `.reason` sidecar explaining what was rejected.

Format reference lives in `runbooks/cycle-prompt.md` §8 ("Dispatch task format"). Validator rules are in `scripts/dispatch_validator.py`.

### Watching costs

Every Claude invocation that goes through the inbox watcher or `/cycle` appends one line to `~/agents/blackboard/costs.jsonl`:

```bash
# Today's spend by agent
jq -r 'select(.ts > "'$(date -u -d 'today 0:00' +%Y-%m-%dT00:00:00)'") | "\(.agent) \(.cost_usd)"' ~/agents/blackboard/costs.jsonl \
  | awk '{a[$1]+=$2} END {for (k in a) printf "%-10s $%.2f\n", k, a[k]}'

# Last 10 invocations
tail -10 ~/agents/blackboard/costs.jsonl | jq -c '{ts, agent, model, cost: .cost_usd, dur: .duration_sec}'

# Total since costs.jsonl was created
jq -s 'map(.cost_usd // 0) | add' ~/agents/blackboard/costs.jsonl
```

This file is the canonical cost source — the future Ledger agent (Phase F+) will mine it for weekly summaries.

---

## 8. Security operating principles

### Where secrets live (and don't)

| Secret | Lives at | Mode | Backed up? |
|---|---|---|---|
| Telegram bot tokens | `~/credentials/.env.larry` on droplet | 600 | No — kept only on droplet. Originals are on Telegram (re-getable from BotFather). |
| GitHub access | `~/.config/gh/hosts.yml` on droplet (managed by `gh auth login`) | 600 | No. If lost, re-run `gh auth login`. |
| Claude Code OAuth | `~/.config/anthropic/...` on droplet (managed by `claude` CLI) | 600 | No. If lost, re-run `claude` and re-authenticate. |
| DO API token | Your password manager (your Mac) | n/a | Yes — that's why we used a password manager. |
| Cloudflare login | Your Cloudflare account credentials | n/a | Account recovery via email |
| **Source code & prompts** | GitHub | n/a | Yes — that's GitHub's job |

**Rule of thumb:** If you can't restore a secret in under 5 minutes, you should write down where to get it.

### What goes in chat with me, what doesn't

| Type | OK to paste to me? |
|---|---|
| Numeric chat IDs (like `7998341473`) | Yes — not secret |
| Bot usernames | Yes — public info |
| Code, file contents, error messages | Yes |
| Bot tokens, API keys, passwords | **NEVER** — even if I ask. I'll always provide a way to keep secrets out of chat. |
| Your Telegram chat history with Beacon | Avoid — could include sensitive context. Summarize instead. |

### What to do if a token leaks

1. **Immediately revoke the leaked token** at the source:
   - Telegram bot token → BotFather: `/revoke` → pick the bot → confirm. Then `/token` to generate a new one.
   - GitHub PAT → GitHub Settings → Developer settings → Personal access tokens → revoke
   - DO API token → DO API → revoke
   - Anthropic API key → Anthropic console → revoke
2. Update the value in `~/credentials/.env.larry` and any other place it was stored.
3. Restart anything that was using it (e.g., the bot if a Telegram token).
4. **Tell me** so I can flag any code that might've logged the token.

### Why we never disable bracketed-paste

Bracketed paste is a terminal feature that tells the shell "these characters came from a paste, not typing." It's a security feature — it prevents pasted text from being interpreted as commands. The "tripping on paste" issue you saw earlier (`00~` and `01~` characters) was a clipboard-format problem, not bracketed paste itself. Leave it on.

---

## 9. Troubleshooting — symptom-driven

### "I send a Telegram message and get no reply"

1. **Is the bot service running?** SSH in: `systemctl status ourliberty-beacon-bot.service`. If `inactive (dead)` or `failed` → `sudo systemctl restart` and check `journalctl -u ourliberty-beacon-bot.service -n 50`.
2. **Is the droplet up?** From your Mac: `ping 134.209.44.80`. If timeouts → check DO dashboard.
3. **Is your chat ID still allow-listed?** SSH in: `grep ALLOWED ~/credentials/.env.larry`. Should show your numeric ID.
4. **Did Claude Code's auth expire?** SSH in: `cd ~/agent-core/agents/beacon && claude --print "say ok"`. If it errors with auth issues → run `claude` interactively, log in again, then `sudo systemctl restart 'ourliberty-*-bot.service'`.
5. **Tail the journal live, send a fresh message, watch:**
   ```bash
   journalctl -u ourliberty-beacon-bot.service -f
   # Then send a Telegram message from your phone
   ```
   You should see `<- ...` appear within 1–2 seconds. If not, the bot isn't seeing your message — Telegram polling issue.

### "I dropped a task in the inbox but nothing happened"

1. **Is the watcher running?** `systemctl status ourliberty-inbox-watcher.service`. If not → `sudo systemctl restart`.
2. **Did the task get rejected?** Look in `~/agents/inboxes/<agent>/.invalid/`. If your task is there, read the `.reason` sidecar.
3. **Is the agent already busy with an earlier task?** The watcher only runs one task per agent at a time (lease primitive). `ls ~/agents/state/dispatch-leases/` — if `inbox:<agent>.lease` exists, that agent is processing something.
4. **Watch the watcher log live:**
   ```bash
   journalctl -u ourliberty-inbox-watcher.service -f
   # Should see `[<agent>] start task=...` within 5s of dropping the file
   ```
5. **Did your file write atomically?** If you used a tool that writes-then-renames, fine. If you `>` redirected and the file was created in two writes, the watcher might have seen it mid-write. Re-drop with `scp` or `mv` from a temp file.

### "The /cycle journal isn't getting committed"

`run_cycle.sh` auto-commits four paths only: `runbooks/cycle-journal.md`, `runbooks/cycle-actions.jsonl`, `agents/pulse/MEMORY.md`, `agents/pulse/memory/`. If Pulse writes anywhere else, the tree stays dirty and `agent_core_health_check.py` will flag it.

To diagnose:
```bash
cd ~/agent-core && git status
# Anything outside the four paths above? That's the issue.
```
Either: (a) commit it by hand and tell me to add it to the auto-commit whitelist, or (b) restore it if it was unintentional.

### "Beacon's responses are weird, generic, off-character"

Most likely Beacon couldn't find her prompt files. Check:

```bash
ls ~/agent-core/agents/beacon/
# Should show: CLAUDE.md, IDENTITY.md, MEMORY.md, SOUL.md, TOOLS.md, USER.md
```

If files are missing: `cd ~/agent-core && git pull`.

If files are present but Beacon still sounds off, the issue is probably that she's not reading them on session start. Check `agents/beacon/CLAUDE.md` — the "Session startup" section instructs her to read the other files.

### "I see [claude exit 1] in the log"

Claude Code failed for some reason. Look at the next few lines for the error:

| Error contains | Likely cause | Fix |
|---|---|---|
| `Authentication` or `unauthorized` | OAuth expired | SSH in, run `claude` interactively, re-authenticate, restart bot |
| `quota` or `rate limit` | You hit Anthropic's rate limit | Wait an hour, or set up a dedicated agent Max account (Phase D follow-up) |
| `model` or `not found` | Model name mismatch | Should not happen unless we change `agent-models.json` — alert me |
| `timed out` | Beacon thought too long | One-off, no action. If recurring, scope conversations smaller. |

### "SSH disconnects after a few minutes"

Set up `ServerAliveInterval` on your Mac. See § 3.

### "A service won't start (status=failed)"

```bash
# What does systemd say?
systemctl status ourliberty-beacon-bot.service     # or whichever
journalctl -u ourliberty-beacon-bot.service -n 50 --no-pager

# Common failure: status=226/NAMESPACE → ReadWritePaths referenced a path that doesn't exist
# Look at the unit file and confirm every path in ReadWritePaths= exists

# Check for Python syntax errors
python3 -m py_compile ~/agent-core/scripts/agent_telegram_bot.py
python3 -m py_compile ~/agent-core/scripts/inbox_watcher.py

# Check env vars are set (without exposing values)
grep -E '^(TELEGRAM_BOT_TOKEN_|TELEGRAM_ALLOWED_CHAT_IDS)' ~/credentials/.env.larry | sed 's|=.*|=<set>|'

# Try running the bot in foreground to see errors directly
sudo systemctl stop ourliberty-beacon-bot.service
set -a; . ~/credentials/.env.larry; set +a
AGENT=beacon python3 ~/agent-core/scripts/agent_telegram_bot.py
# Ctrl-C to stop, then re-enable: sudo systemctl start ourliberty-beacon-bot.service
```

### "Telegram says my bot's token is invalid"

The token in `.env.larry` doesn't match what BotFather has. Either:
- The token got truncated when pasted → Re-run `bash ~/install_beacon_creds.sh`
- BotFather rotated the token (rare, only if you ran `/revoke` or `/token`) → Get the current token, install it again

---

## 10. Cost monitoring

### The canonical source: `~/agents/blackboard/costs.jsonl`

Every Claude invocation through the inbox watcher or `/cycle` appends one record here. Each line:

```json
{"ts":"...","agent":"pulse","task_id":"...","model":"...","cost_usd":0.034,"input_tokens":3,"output_tokens":46,"cache_read":12736,"cache_creation":7476,"duration_sec":3.92,"source":"inbox-watcher"}
```

Quick queries (jq one-liners) live in §7 "Watching costs".

The **Ledger** agent (see §10.1 below) rolls this up into a weekly summary every Monday morning, writes a markdown report + JSON sidecar to `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.{md,json}`, and DMs Larry a one-line headline.

### 10.1 The Ledger weekly report

Ledger runs every Monday morning at 07:00 UTC (00:00–01:00 MDT depending on DST) via `ourliberty-ledger.timer` → `ourliberty-ledger.service`. The service wraps `scripts/run_ledger.sh`, which (after a lock + `EMERGENCY_HALT` check) invokes `python3 scripts/ledger_weekly.py`. The Python module is fully deterministic — no LLM is in the loop for the weekly run.

**What he reads:**
- `~/agents/blackboard/costs.jsonl` — last 7 days of cost rows (window `[week_ending - 7d, week_ending)`).
- `~/agents/outboxes/<agent>/.archive/<task_id>.json` — per-row `task_type` attribution. Rows with no matching archive get `task_type = "unknown"`.
- The prior 4 weekly sidecars at `~/agents/blackboard/ledger/weekly-*.json` — for week-over-week delta + σ baselines (suspended until ≥4 weekly windows of history exist).

**What he writes:**
- `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.md` — human-readable report (total, by-agent, by-task_type, anomalies, top-5).
- `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.json` — machine-readable sidecar (schema documented in `runbooks/ledger-prompt.md` § "JSON sidecar schema"; consumed by Pulse Check I).
- `~/agents/blackboard/ledger/ledger-ready-YYYY-MM-DD` — sentinel file Pulse Check I polls before reading the JSON.
- `runbooks/ledger-journal.md` — one-line entry per run, auto-committed + pushed by `run_ledger.sh`.
- One alert via `larry_alerts.append_alert(source='ledger', severity='warning', subject='weekly-YYYY-MM-DD', ...)` — delivered to Larry's Telegram via the beacon-bot's 5-min alert sweep.

**Recovery from a missed run.** If the Monday timer was missed (e.g., droplet was off), `Persistent=true` causes the run to fire on next boot. For an ad-hoc rerun:

```bash
# Default: report for the current Monday
sudo systemctl start ourliberty-ledger.service

# Or a specific week:
bash ~/agent-core/scripts/run_ledger.sh
# (the wrapper passes --week-ending defaulted to current Monday;
# for arbitrary weeks invoke Python directly)
python3 ~/agent-core/scripts/ledger_weekly.py --week-ending 2026-05-18
```

The lock file `~/agents/state/.ledger.lock` prevents overlapping runs (30-min stale threshold). The `larry_alerts` cooldown bucket keyed by `(source, subject=weekly-YYYY-MM-DD)` prevents duplicate DMs even if the run fires twice for the same week.

**Verifying the timer:**

```bash
systemctl list-timers ourliberty-ledger.timer
systemd-analyze calendar 'Mon *-*-* 07:00:00 UTC'
journalctl -u ourliberty-ledger.service -n 50
tail -1 ~/agents/blackboard/ledger/weekly-$(date -u -d 'last monday' +%Y-%m-%d).json
```

### 10.2 Pulse Check I (weekly optimization digest)

Check I is Pulse's optimization-mode pass, additive to her existing 4h Health Check Suite (Checks A–H). It fires only on Monday cycles, **after** Ledger has landed his weekly report (sentinel-gated). The deterministic compute lives in `scripts/pulse_check_i.py`; no LLM is in the loop for the v1 baseline (Pulse-the-LLM running /cycle may layer an interpretation paragraph on top of the analyzer's output).

**When it fires:**
- Today is Monday (UTC weekday == 0), AND
- `~/agents/blackboard/EMERGENCY_HALT` not present, AND
- `~/agents/blackboard/ledger/ledger-ready-<this-Monday>` sentinel exists, AND
- `~/agents/blackboard/ledger/weekly-<this-Monday>.json` is ≤ 7 days old.

Off-Monday cycles skip Check I entirely; Pulse's A–H output is unchanged.

**What it reads:**
- Ledger's JSON sidecar at `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.json` (schema in `runbooks/ledger-prompt.md`).
- Outbox archives at `~/agents/outboxes/<agent>/.archive/*.json` — counts retry suffixes (`<task>.1.json`, `<task>.2.json`) per task_id to surface recurring shapes.

**What it writes:**
- A Telegram alert via `larry_alerts.append_alert(source='pulse', severity='warning', subject='check-i-YYYY-MM-DD', ...)`. The DM shape is either **heartbeat** ("chain shapes nominal — no proposed optimizations this week") when nothing actionable is found, or **digest** (Ledger headline + retry-overhead + numbered list of proposals) when proposals were synthesized.
- `~/agents/blackboard/pulse-check-i/check-i-YYYY-MM-DD.json` — structured audit record (mode, ledger_headline, engineering_signals, proposals).
- A `**Check I:**` block appended to `runbooks/cycle-journal.md` (additive to the standard A–H entry written by Pulse).

**How to interpret the DM:**
- **Heartbeat** (`Pulse Check I (week of X): chain shapes nominal — ...`): everything was within baseline; no action requested. Skim and move on.
- **Digest** (`Pulse Check I (week of X): Ledger total ..., N anomalies. Proposed optimizations: 1. ...`): the analyzer surfaced 1–3 proposals tagged with effort (`small`/`medium`/`large`) and impact (free-text USD or percent estimate). Each proposal is a *recommendation*, not an auto-dispatch — Beacon drafts the actual dispatch once you approve.
- **Skipped** (`Pulse Check I (week of X): skipped — Ledger sidecar unavailable`): Ledger's run didn't land for this week. Investigate Ledger first (§10.1), then re-run Check I manually.

**Manual trigger via Telegram `/optimize`:** the Pulse bot accepts `/optimize` for an ad-hoc Check I run regardless of the weekday. If Ledger's sidecar is >24h old, the bot refreshes Ledger first (runs `scripts/run_ledger.sh`), then invokes the analyzer with `--force` to bypass the Monday gate.

**Manual rerun (CLI):**

```bash
# Default: current Monday, scheduled cadence
python3 ~/agent-core/scripts/pulse_check_i.py

# Skip the Monday weekday gate (the `/optimize` path)
python3 ~/agent-core/scripts/pulse_check_i.py --force

# Specific week
python3 ~/agent-core/scripts/pulse_check_i.py --week-ending 2026-05-18

# Dry run — no DM, no journal append
python3 ~/agent-core/scripts/pulse_check_i.py --no-dm --no-journal
```

**Verifying Check I fired:**

```bash
# Did the audit record land for this week?
ls -la ~/agents/blackboard/pulse-check-i/check-i-$(date -u -d 'last monday' +%Y-%m-%d).json

# Did the journal pick up the Check I block?
grep -A 20 "^\*\*Check I" ~/agent-core/runbooks/cycle-journal.md | tail -25

# Did the DM queue?
tail -3 ~/agents/blackboard/larry-alerts.jsonl
```

**Empty-week behavior is the contract** — the heartbeat DM fires even with zero findings so silence never means "Pulse ran but didn't tell you." If Check I should have fired and *nothing* landed (no journal block, no audit record, no DM), check the cycle log (`~/agents/logs/cycle.log`) for invocation errors or the Monday-gate skip note.

### Anthropic (Claude Code / API)

- **Where:** [console.anthropic.com](https://console.anthropic.com) → Usage
- **Currently using:** Larry's personal Claude Max OAuth on the droplet. Quota is per Max account. Both the Telegram bots and the inbox watcher share this auth.
- **Cycle baseline:** ~$0.10 / cycle on Sonnet at the current 4h cadence → ~$0.60/day → ~$18/mo. Inbox-dispatched tasks add to this proportional to volume.
- **Open follow-up:** dedicated agent-only Max account so Larry's personal Claude Code doesn't share quota with the droplet bots.

### DigitalOcean

- **Where:** [cloud.digitalocean.com](https://cloud.digitalocean.com) → Billing
- **Current monthly:** ~$58 (droplet $48 + backups $9.60)
- **What to watch for:** Bandwidth overages (6 TB/mo budget — should never hit). Snapshot count growing.

### Cloudflare

- **Free** for the DNS service we use. Domain renewal: ~$12/yr in May 2027.

### Telegram

- Free.

### Total expected monthly: ~$58 droplet + ~$18 Anthropic (cycles) + whatever inbox dispatches cost. Currently ~$76/mo all-in.

---

## 11. Glossary

| Term | What it means |
|---|---|
| **SSH** | Secure Shell. The protocol you use to log into the droplet from your Mac. |
| **Droplet** | DigitalOcean's name for a virtual machine (VM). |
| **VM** | Virtual machine. A fake "computer" running on shared hardware. Acts like a real Linux box. |
| **Ubuntu** | A Linux distribution (operating system). The droplet runs Ubuntu 24.04. |
| **tmux** | Terminal multiplexer — keeps terminal sessions running after you log out. |
| **systemd** | Linux's service manager. We'll use it in Phase D to auto-start the bot on boot. |
| **UFW** | Ubuntu's firewall. Allows only SSH (22), HTTP (80), HTTPS (443) inbound. |
| **fail2ban** | Watches the SSH log for brute-force attacks; blocks the IPs. Already running. |
| **Cron** | Linux's scheduled task system. We'll use it in Phase A.12 (deferred) for the upstream mirror sync. |
| **Bracketed paste** | A terminal feature that distinguishes pasted text from typed text. |
| **DNS A record** | A mapping from a domain name (`agents.ourliberty.dev`) to an IPv4 address (`134.209.44.80`). |
| **Long-polling** | The bot keeps an HTTP request to Telegram open for ~30s; Telegram responds when there's a new message OR the timeout hits. Way more efficient than asking "any messages?" every second. |
| **OAuth** | The "log in with browser" flow used by `gh` and `claude`. Stores a long-lived token locally. |
| **`.env` file** | A text file with `KEY=value` lines. Convention for storing secrets and configuration outside the code. |
| **`sudo`** | "Superuser do" — runs a command with admin privileges. `larry` can sudo without a password (for automation). |
| **`tmux session`** | A named, long-running terminal that survives disconnects. Our bot lives inside one called `beacon-bot`. |
| **`git pull`** | Download new commits from GitHub into your local clone. |
| **Beacon** | Strategy/Architect agent. First agent in the system. Personality defined in `agents/beacon/*.md`. |
| **Forge / Mirror / Pulse** | Live agents (D1 activation). Personality in `agents/<name>/*.md`, bot under systemd. |
| **Ledger** | CFO / cost-intelligence agent (v1 shipped). Runs weekly Mon 07:00 UTC, writes report + JSON sidecar to `~/agents/blackboard/ledger/`, DMs Larry. See §10.1. |
| **Aide / Scout / Compass** | Planned future agents. Personas not yet authored. |
| **HANDSHAKE** | The JSON schema for tasks that flow between agents via inboxes. Lives at `shared/HANDSHAKE-SCHEMA.json`. Optional fields documented there; required ones in `scripts/dispatch_validator.py`. |
| **Inbox / Outbox** | Per-agent directories under `~/agents/{inboxes,outboxes}/<agent>/`. Dropping a JSON task in an inbox triggers the watcher to run that agent on it. The result lands in the outbox. |
| **Inbox watcher** | The shared daemon (`ourliberty-inbox-watcher.service`) that polls all four inboxes and runs `claude --print` on valid tasks. |
| **`/cycle`** | Pulse's self-healing iteration. Reads system state, classifies findings, takes safe auto-fixes, writes a journal entry. Runs every 4h via systemd timer. |
| **Cycle journal** | `runbooks/cycle-journal.md` — chronological record of every cycle iteration. Pulse appends; humans (or the auto-commit hook in `run_cycle.sh`) commit. |
| **Escalation** | A Pulse finding that wasn't auto-fixed and needs human attention. Lives in `~/agents/blackboard/pulse-escalations.json`. |
| **Lease** | A restart-safe "I'm working on this" claim. The watcher uses `inbox:<agent>` leases to ensure one task per agent at a time. Implementation in `scripts/dispatch_lease.py`. |
| **Dispatch validator** | The strict pre-flight check on inbox tasks (`scripts/dispatch_validator.py`). Stricter than HANDSHAKE-SCHEMA — designed to kill the F24 empty-prompt bug class. |
| **Working-copy discipline** | The rule that `~/agent-core/` is always on `main`, always clean, never has uncommitted changes. Enforced every 30 min by `agent_core_health_check.py`. |

---

## Appendix A — Quick reference card (print this)

```
SSH IN:                    ssh larry@134.209.44.80
ALL SERVICES STATUS:       systemctl list-units 'ourliberty-*' --type=service
ALL TIMERS STATUS:         systemctl list-timers 'ourliberty-*'
RESTART ONE BOT:           sudo systemctl restart ourliberty-beacon-bot.service   # or forge/mirror/pulse
RESTART ALL BOTS:          sudo systemctl restart 'ourliberty-*-bot.service'
RESTART WATCHER:           sudo systemctl restart ourliberty-inbox-watcher.service
RUN CYCLE NOW:             sudo systemctl start ourliberty-cycle.service
TAIL ONE SERVICE LIVE:     journalctl -u ourliberty-beacon-bot.service -f
TAIL CYCLE OUTPUT:         tail -f ~/agents/logs/cycle.log
TAIL WATCHER:              tail -f ~/agents/logs/inbox_watcher.log
PULL NEW CODE:             cd ~/agent-core && git pull --ff-only
EDIT CREDENTIALS:          nano ~/credentials/.env.larry   (Ctrl-O save, Ctrl-X exit)
TALK TO AGENT DIRECTLY:    cd ~/agent-core/agents/<agent> && claude
TODAY'S COST:              jq -r '.cost_usd' ~/agents/blackboard/costs.jsonl | awk '{s+=$1} END {printf "$%.2f\n", s}'
SEE PULSE ESCALATIONS:     cat ~/agents/blackboard/pulse-escalations.json | jq
SEE LATEST CYCLE:          tail -50 ~/agent-core/runbooks/cycle-journal.md
DROPLET STATUS:            (from Mac) ping -c 3 134.209.44.80
```

---

## Appendix B — Roadmap (what's still ahead)

| Phase | What | What changes for you |
|---|---|---|
| **D3** (next) | Beacon ↔ Pulse dialogue; Beacon → Larry approval gate via Telegram; Beacon → Forge dispatch | First end-to-end autonomous Build Loop: Pulse finds → Beacon plans → DMs you for approval → Forge implements → Mirror reviews. You stop being the message bus between agents. |
| **D4** | Mirror dispatch on PR open; Beacon → Larry completion summary | Mirror gets PRs without you having to Telegram her; you get a "shipped" DM from Beacon when a loop closes. |
| **D5** (probable stabilization) | Auto-commit whitelist tuning; cycle cost-attribution fix; logrotate for `~/agents/logs/` | Less ops drag. |
| **E** | Aide (Executive Assistant) added | New bot for Gmail/Calendar/inbox triage. Separate Telegram channel. |
| **F** | Mini Brains prototype shipped | First real prototype repo with a handoff package — RAG + meaning layer. |
| **F+** | Ledger agent (Accountant/CFO) added | Weekly cost summaries, anomaly alerts, optimization specs dispatched through the normal pipeline. Needs spend history (which we're now capturing in `costs.jsonl`). |
| **Phase 2** | Scout (research) + Compass (planner) added | Two more agents covering research and sequencing across multiple in-flight projects. |

Notes on what stays open even within D2:
- A.12 daily cron mirroring upstream `gm-agent-core` into the mirror repo
- Dedicated agent-only Claude Max account
- `ourliberty-watchdog.timer` enable (after ≥1 day of cycle observation)
- `agent_runner.py` path/sweep-ledger fixes (only matters when used in dispatch)
- Trim redundant glob patterns in `~/.claude/settings.json` once we know which one matches

## Appendix C — Re-bootstrapping from scratch (DR reference)

If you ever need to rebuild the system on a fresh droplet (e.g., DigitalOcean account migration, or the droplet is unrecoverable), the canonical sources are:

- **Infrastructure provisioning:** `systemd/INSTALL.md` in this repo — exact `sudo cp` + `daemon-reload` + `enable --now` sequence per unit.
- **Order of activation:** Part II of this document (the build narrative) — gives you the right sequence (Phase A foundations → Phase B Beacon → Phase C Forge/Mirror → Phase D Pulse+/cycle → Phase D2 inbox watcher).
- **Credentials to recreate:**
  - 4× Telegram bot tokens (BotFather: `/mybots` → token, or `/revoke` then `/token`)
  - GitHub: `gh auth login` on the new droplet
  - Claude Code: `claude` interactively
  - DigitalOcean: API token already in your password manager
- **Files that ARE NOT in the repo** (must be re-created or restored from backup):
  - `~/credentials/.env.larry` — bot tokens, allowed chat IDs
  - `~/agents/state/*_telegram_sessions.json` — session continuity (loss = fresh conversation, not catastrophic)
  - `~/agents/blackboard/pulse-escalations.json` — open findings (loss = Pulse re-discovers on next cycle)
  - `~/agents/blackboard/costs.jsonl` — cost history (loss = Ledger has less data to mine)

Backups: DO snapshots are enabled. They cover everything including `~/credentials/` and `~/agents/`.

---

# Part II — Build Narrative & Decisions Log

This section is the chronological story of how the system came to be. Each phase entry captures (a) what we built, (b) why, (c) the decisions made and what we rejected, and (d) how we verified it.

New phases append at the end. Earlier phases are not edited — if something changed later (e.g., Phase B was tmux, then migrated to systemd in D1), the change is described in the later phase's entry, not retroactively patched into the earlier one. This way the doc reads as a timeline of decisions rather than a revisionist snapshot.

---

## Phase A — Foundations (2026-05-08, ~3 hours)

**What we built:** A hardened DigitalOcean droplet (`ourliberty-agents-01`, 8GB/4vCPU, Ubuntu 24.04), a registered domain (`ourliberty.dev`) with DNS pointing at it, a foundation repo (`Larry-Yatch/ourliberty-agent-core`) bootstrapped with README + .gitignore + HANDSHAKE-SCHEMA + NORTH-STAR + REPO-GUARDRAILS + .env.example. Larry's `larry@sealteamleaders.com` account on the droplet has NOPASSWD sudo; root SSH disabled; UFW open on 22/80/443 only; fail2ban + unattended-upgrades running.

**Why:** Larry needed a sandbox separate from Marvin (Nick's system) and Pocket Agent (his Mac-only EA). The shape we picked: `gm-agent-core`-style on a Linux VM, optimized for the prototype-to-handoff loop. Everything else flows from those two choices.

**Decisions made:**
- **DigitalOcean over AWS/GCP:** simpler billing, simpler UI, one VM is all we need. No K8s.
- **Ubuntu 24.04 over Debian:** longer LTS window, friendlier `apt` ecosystem, what `gm-agent-core` upstream is tested on.
- **Cloudflare for DNS, not registrar lock-in:** keep portability.
- **Working-copy discipline as a rule from day 1:** `~/agent-core/` is always on `main`, always clean. Drift is the enemy of self-healing.

**Decisions deferred:**
- A.12 (daily cron mirroring upstream `gm-agent-core`) — easy, not blocking, still open.
- Dedicated agent-only Claude Max account — still uses Larry's personal Max.

**Verified:** SSH works, droplet stays up across reboots, DNS resolves, repo clones cleanly.

---

## Phase B — Beacon online (2026-05-08, same session)

**What we built:** Beacon (the Strategy/Architect agent) with her 6 prompt files (IDENTITY/SOUL/CLAUDE/TOOLS/USER/MEMORY, ~3.2k words total). A Telegram bot (`scripts/beacon_telegram_bot.py`) running in a tmux session named `beacon-bot` on the droplet, using `claude --resume` per-chat for continuity. Larry's chat ID (`7998341473`) is the only authorized chat ID (security gate; bot refuses to start with empty `TELEGRAM_ALLOWED_CHAT_IDS`).

**Why:** The bar Larry set was "talk to an agent on the new platform by end of session." Beacon is the right first agent because every other agent's output flows from her specs.

**Decisions made:**
- **Telegram over Slack/iMessage/web UI:** works from his phone, anywhere. No new app to learn. Free.
- **One bot per agent, not one shared bot:** lets each agent have its own personality + chat continuity. Costs us a BotFather setup per agent but pays off in mental model clarity.
- **`claude --resume` per chat, not per agent:** if Larry talks to Beacon from two devices, they share continuity. If two different humans talk to Beacon (future), they have separate sessions.
- **Allowlist-only chat IDs:** bot refuses to start with empty allowlist. Defense in depth — Telegram knowing the token isn't enough.
- **tmux for the bot (temporarily):** got us to "alive" in one session. Stated openly as a weakness; systemd migration scheduled for D1.

**Decisions deferred:**
- Migrate to systemd → done in D1.
- Dedicated agent-only Max → still open.

**Verified:** Beacon's first conversation: she read all 6 prompt files, accurately summarized her job + current state, named what doesn't exist yet, asked "What are we working on?" — terse, peer-level, no filler. This became the voice/quality bar future agents need to clear on their first conversation as a smoke test.

---

## Phase C / D pre-staging — autonomous authoring (2026-05-08, evening, ~5 hours)

**What we built:** Pre-staged the personas + infrastructure for Phase C (Forge + Mirror) and Phase D (Pulse + `/cycle`) so Larry's next activation sessions are ~15 min each rather than long authoring sessions.

Concretely (4 commits to `main`):
1. **Three agent personas** (Forge / Mirror / Pulse) — 18 files, ~12k words. Each follows Beacon's 6-file pattern.
   - Forge ⚒️ (Builder): pragmatic, action-first; Build Loop + PR template + handoff package requirements in `TOOLS.md`.
   - Mirror 🪞 (Reviewer): severity tags `[must-fix]` / `[should-fix]` / `[nit]`; Review Checklist baked in.
   - Pulse 💓 (Observer / `/cycle`): diagnostic calm voice; Health Check Suite (A–G); tiered auto-fix allow-list (always / ask-then-do / never-auto); teach-to-fish discipline.
2. **Adapted scripts from `gm-agent-core` upstream** (~20 files, ~5,400 lines): dispatch validator/lease/dedup-guard, watchdog/health-check, healers. Path translations applied (joe → larry); GM-specific topology dropped; stubs added with TODO markers for Phase D wiring.
3. **`/cycle` infrastructure:** `runbooks/cycle-prompt.md` (Pulse's iteration spec, ~10KB), starter files for journal + actions log, `scripts/run_cycle.sh` (concurrency-locked wrapper), `scripts/agent_telegram_bot.py` (generic successor to `beacon_telegram_bot.py`, parameterized by `AGENT=` env var), `config/agent-models.json` (per-agent model routing), `shared/REPO-GUARDRAILS.md` authority matrix.
4. **13 systemd unit files:** 4 bot services, 4 oneshot+timer pairs (cycle / sync / health-check / watchdog), all hardened (`User=larry`, `ProtectHome=read-only`, `MemoryMax=2G`, `NoNewPrivileges=true`).

**Why:** Larry asked "is there anything you can do now to start on C and D?" Yes — ~80% of the work is authoring + wiring, not Larry-specific. Pre-stage everything so activation is a series of short, surgical Larry-actions (BotFather, paste token, install unit) rather than long bouts of "wait while I write."

**Decisions made:**
- **Adopt Beacon's 6-file persona pattern across all agents.** Uniformity > customization at this stage.
- **Drop GM's 7-agent C-suite (Atlas/Sage/Luma/Nova/Prism/Ember/Mula).** Over-built for a solo sandbox. Target ~5 agents.
- **Keep `/cycle` self-healing as a first-class capability**, not an afterthought.
- **Direct commits to `main` on this config repo are allowed.** It's not a code repo; PRs are for `proto-*` repos.

**Subagent caveat:** the script-adaptation subagent triggered a "pushed to main" security warning. Expected per the working-copy discipline rule.

**Verified:** All files parse cleanly; systemd units pass `systemd-analyze verify` (later, on the droplet, during D1 activation).

---

## Phase D1 activation — agents go live (2026-05-09, single afternoon, ~5 hours)

**What we built:** Activated everything that was pre-staged. By end of session: all 4 agents under systemd with auto-restart, Pulse running `/cycle` autonomously every hour on Sonnet 4.6 at ~$0.84/cycle, sync + health-check timers running, real journal entries being written.

**Why:** The pre-staging in 2026-05-08 evening meant activation was "BotFather × 3, install tokens, install systemd units, fix bugs surfaced during activation, run first cycles, tune."

**Decisions made (these shape D2+):**
1. **Larry-approval gate before any Forge dispatch.** When Beacon completes a plan from a Pulse-dispatched task, she DMs Larry via Telegram with the plan and asks for approve/modify/reject. **Default = Medium autonomy** (proposed-then-confirmed). Loose mode (auto-approve carve-outs) added per-task-type later, never for code that touches T1 repos.
2. **Ledger agent (Accountant/CFO) planned as Phase F+ 8th agent.** Watches all agent costs and cloud bills. Codename matches the others' single-syllable + image style. **Deferred until cost surface stabilizes** so we don't rebuild her checks every phase.
3. **In D2 we add cost capture** so Ledger has a year of spend history when she arrives.
4. **Cycle cadence: 1h → 4h** the next day. Hourly was ~$600/mo paying $0.84 to journal nothing-changed; 4h is ~$150/mo and catches real issues within a tolerable window. Dial back up when Build Loop is active.

**Bugs surfaced and fixed during activation:**
- `systemd ReadWritePaths` referenced `~/.config/anthropic` (doesn't exist on Linux Claude Code; should be `~/.claude`) → all 4 bots + cycle failing with `status=226/NAMESPACE`. Fixed.
- Cycle ran on Opus 4.7 (1M-context) at $2.33/run → too expensive for hourly. Forced Sonnet via `--model claude-sonnet-4-6`. Per `agent-models.json`, this matches the design intent.
- `~/.claude/settings.json` permissions allow-list too narrow → expanded with explicit allows + targeted denies (credentials, /etc, force-push, rm -rf).

**Real findings from Pulse iter 1 (worth carrying forward):**
- 4-of-4 bots cold-start with `claude --resume <stale-session>` → fail-then-retry pattern. Real bug, watch-listed by Pulse for permanent-fix dispatch once it appears in 3+ cycles.
- Pulse's Beacon log-silence threshold (>30m → ask-then-do) false-positives on idle bot polling. Needs calibration.
- Cycle's auto-fix sandbox needed widening for normal Read/Write/Bash patterns.

**Verified:** All 4 bots responded on Telegram; first `/cycle` produced real findings; sync + health-check timers running clean.

**Open issues entering D2:**
- Pulse writes journal+MEMORY changes during cycle → uncommitted → `agent_core_health_check.py` flags dirty tree every 30 min. **D2 to add auto-commit hook in `run_cycle.sh`.**
- No mechanism yet for Pulse to actually dispatch a task to Beacon/Forge/Mirror's inbox; everything still routes through a human. **D2 to build the inbox watcher.**

---

## Phase D2 — Inbox watcher + cost capture + cycle auto-commit (2026-05-11, ~2 hours)

**What we built:**
1. **`scripts/inbox_watcher.py`** — shared multi-agent daemon. Polls `inboxes/{beacon,forge,mirror,pulse}/` on 5s, validates via `dispatch_validator`, holds `inbox:<agent>` lease via `dispatch_lease` while running, spawns `claude --print` in each agent's CWD with the agent's `inbox_model` from `agent-models.json`, writes `outboxes/<agent>/<task-id>.json` + appends a record to `blackboard/costs.jsonl`, archives consumed task to `inboxes/<agent>/.archive/`. One thread per agent → agents run in parallel; max one task per agent in flight. Malformed/rejected tasks move to `inboxes/<agent>/.invalid/` with a `.reason` sidecar. Requeue cap = 3.
2. **`systemd/ourliberty-inbox-watcher.service`** — `Type=simple`, `Restart=on-failure`, hardening parity with existing units. Single process for all four agents.
3. **`run_cycle.sh` cost capture** — `jq`-parse `cycle.last-output.json` → append a normalized record to `~/agents/blackboard/costs.jsonl`. Best-effort; jq absence is non-fatal.
4. **`run_cycle.sh` auto-commit** — if Pulse touched journal / actions / MEMORY, `git add` + commit + push. Closes the dirty-tree gap that was tripping `agent_core_health_check.py` every 30 min.
5. **`runbooks/cycle-prompt.md` §8 "Dispatch task format (reference)"** — spells out the validator-strict fields (`task_id` required, `prompt` ≥ 100 chars, `source` enum) with a copy-paste template. Pulse can now write valid dispatches.

**Why:** Phase D1 left the system *self-watching but not self-coordinating*. When Pulse found something, a human still drove Beacon → Forge → Mirror → merge through Telegram. D2 builds the layer that turns "Pulse writes JSON" into "Beacon actually receives it" — the foundation for D3's full autonomous Build Loop.

**Key architectural decisions:**

1. **One shared watcher process, four agent-threads, not one process per agent.**
   - *Considered:* 4 separate systemd units (one per agent). Cleaner failure isolation.
   - *Picked:* shared process. Less unit duplication, single place to evolve dispatch logic, subprocess isolation per task gives us the failure isolation we'd otherwise get from separate processes.
   - *Mitigation:* each `claude --print` invocation is a subprocess — one agent crashing doesn't touch the watcher's main loop.

2. **One task per agent in flight, agents run in parallel.**
   - *Considered:* fan out (N concurrent tasks per agent). Faster on bursty inboxes.
   - *Picked:* bounded. Lease identity is `inbox:<agent>`. Simpler, restart-safe, matches "agents are conversational beings, not workers." Easy to widen later by changing the lease identity to include a slot number.

3. **Reuse `dispatch_lease.py`, don't reinvent.**
   - The lease primitive already has flock + nonce + heartbeat + boot-id PID-reuse guard + SIGTERM→SIGKILL reclaim. Mature. Wasted effort to rebuild.

4. **5-second poll, stdlib only (no inotify dependency).**
   - At our task volume (single-digit per day initially), the difference between 5s and inotify is invisible. Stdlib keeps the watcher portable.

5. **Keep `dispatch_validator.py` strict (MIN_PROMPT_LEN=100, `task_id` required) — adapt Pulse instead.**
   - The strictness exists because of a real bug class (F24 empty-prompt). Weakening the validator would lose that protection. Teach Pulse to write substantive prompts via §8 of `cycle-prompt.md`.

6. **No outbox consumer in D2 — strict scope.**
   - The watcher writes outboxes; D3 wires the back-channel (Pulse's reply unblocking Beacon's session, etc.). Mixing concerns here would have ballooned D2.

7. **Bundle auto-commit + cost capture with run_cycle.sh changes.**
   - Both live in the same file, both are 30-min jobs, both unblock the next phase. Splitting into separate commits would have been process for its own sake.

**Verified end-to-end on the droplet (2026-05-11, ~$0.29 total):**
- **Smoke test (Pulse, Sonnet):** task picked up in 20s (5s poll + scp mtime skew), ran in 3.92s, $0.033. Outbox + costs.jsonl + archive all green. Pulse correctly identified herself from her CLAUDE.md.
- **Validator-rejection test:** 9-char prompt rejected with F24 error, file moved to `.invalid/`, `.reason` sidecar written. $0.
- **Parallelism test:** 4 tasks dropped simultaneously (one per agent). All 4 picked up within 1 second of each other (22:08:17 → 22:08:18), all 4 completed within 6 seconds of pickup. Wall-clock for the batch: 7s vs ~20s sequential. All 4 agents correctly identified themselves and obeyed the "no tools" instruction. Cost: $0.255 for all 4 (Beacon $0.072, Forge $0.076, Mirror $0.075, Pulse $0.033).

**Open issues entering D3:**
- `keys | first` in the cost-capture jq picks alphabetically — when a cycle uses both Haiku and Sonnet (via Claude Code's internal model routing), the cost record will say "haiku" even though most of the work was Sonnet. Cosmetic; flag for D5.
- `run_cycle.sh` auto-commit whitelist covers four paths. If Pulse ever writes outside them, health-check will flag dirty tree. Watch the first few post-D2 cycles to confirm whitelist matches observed behavior.
- The bot session-resume retry pattern is still open — natural first end-to-end Build Loop test in D3 once Pulse's pattern count crosses 3.

---

## Phase D2.5 — Upstream-audit response: hybrid watcher + healers (2026-05-11, ~5 hours)

**What we built:** Closed the gaps a comprehensive upstream audit found in D2. Five commits across one session:

1. **`docs/upstream-audit.md`** (652 lines) — file-by-file map of upstream `gm-agent-core` vs Larry's fork. Lists every script with adoption status, identifies 10 gaps prioritized by phase, calls out genuinely-GM-specific code with a defended skip reason, and explicitly answers the D2 thin-watcher question.
2. **`scripts/post_merge_verifier.py`** — forward-ported upstream `#241` (branch-prefix allowlist extension).
3. **D2.5-prep** — synced `shared/HANDSHAKE-SCHEMA.json` source enum from GM-era to Larry-era (Gap 7); pulled upstream's identity-test fixtures (Gap 9). One test (`RunClaudeInvokesScrub`) required a Popen-mock rewrite because our fork's `agent_runner.run_claude` uses `subprocess.Popen` for cancel-marker polling while upstream uses `subprocess.run` — captured as a separate fixup commit.
4. **D2.5-watcher** — the substantial migration (Gap 1 + Gap 5):
   - Extended `_StubTokenManager` in `agent_runner.py` with `check_for_rate_limit`/`detect_cap_in_output`/`report_rate_limit`/`report_success` no-ops (the previous stub only had `get_token`, so the first real call would have crashed). Fixed `get_token` to return a string `account_id` ('oauth') instead of `0` (int) which broke string concatenation.
   - Added optional `out_meta` dict parameter to `agent_runner.run_claude` so cost/usage/attempts/account_id can flow back to callers without breaking the existing return signature.
   - Rewrote `scripts/inbox_watcher.py` (~470 lines) to delete its local `run_claude` and call `agent_runner.run_claude` instead. Identity-assertion preamble (Phase D2.5 Call A: on by default) prepended via `build_expected_agent_assertion(agent)` where `expected_agent` is implicit from the inbox path (Call B). `task_stem=task_id` activates in-flight registry + cancel-marker support.
   - Added `reap_orphans_on_startup()` (Call C: adopt-if-alive, mark-failed-if-dead, never re-dispatch). On watcher restart, scans `state/in-flight/*.json`, writes forfeit outboxes with `exit_code=-3`, leaves operator to re-dispatch manually if needed.
   - Added `_emergency_halt_active()` poll at the top of `agent_loop()` and inside the per-task loop. `~/agents/blackboard/EMERGENCY_HALT` is sticky — exits cleanly, systemd doesn't auto-restart, requires manual `systemctl start` to resume.
   - Pre-create `~/agents/config/` in `ensure_dirs()` (fixup commit) — `concurrency_guard.py` writes its state there; missing directory caused the first hybrid dispatch to crash with `FileNotFoundError`.
5. **D2.5-healers** — 7 systemd `.service` + `.timer` pairs (Gap 8) wrapping the already-pulled healer scripts: `abandoned-inbox-tasks` (10m), `blocked-inbox-age` (15m), `empty-inbox-files` (15m), `recovery-already-merged` (5m), `restart-dedup-obsolete` (5m), `silent-loop-death` (10m), `zombie-main-workers` (5m). All oneshot, `Nice=10`, lean hardening (matches upstream's healer style).

**Why:** Larry caught that I'd started D3 design from scratch without auditing Joe's upstream. We paused, ran a 60-minute comprehensive audit, and the audit changed the trajectory: D2 had bypassed 600+ lines of upstream defense machinery (retry/backoff, identity-hardening, in-flight registry, cancel-markers, rate-limit detection). The "build complete, not fast" principle made the call obvious — adapt and use upstream's hardened path, not parallel it. Phase D2.5 isn't a new feature; it's "make D2 actually complete by the audit's measure" so D3 starts on a solid foundation.

**Key decisions (audit-grounded, not designed from scratch):**

1. **Hybrid migration, not full replacement.** Keep our thread-per-agent watcher structure (cleaner than upstream's ThreadPoolExecutor for our 4-agent fan-out). Swap only the `claude --print` spawning step to `agent_runner.run_claude`. The thin watcher's *structure* is Larry's improvement; the thin watcher's `run_claude` was undercooked and is correctly replaced.
2. **Identity-assertion preamble on by default.** Defense against the upstream 2026-04-16 `/tmp/CLAUDE.md` poisoning incident (Joe lost a full day to silent persona-swapping). Cost: ~500 tokens/task, invisible at our scale. `expected_agent` is implicit from inbox path — filesystem layout is the truth, dispatchers can't lie about it.
3. **Orphan policy: adopt-if-alive, mark-failed-if-dead, NEVER re-dispatch.** Re-dispatch automatically would risk double-billing if claude actually completed but the watcher restart lost its stdout pipe. The detached `start_new_session=True` subprocess outlives the watcher; we accept that output is forfeit on restart and surface it via outbox `exit_code=-3` with operator-actionable text.
4. **Three-commit sequencing for rollback isolation.** D2.5-prep (zero-risk additions) → D2.5-watcher (the one risky commit) → D2.5-healers (independent additive concern). Each commit independently revertable.
5. **Test-first, not test-after.** Pulled identity tests in D2.5-prep BEFORE the migration so we had a baseline. When the integration test (`RunClaudeInvokesScrub`) failed, the diagnostic took 30 minutes and surfaced a real test bug (Popen vs `run` API mismatch) rather than a real code defect — fixing it before the migration meant we knew the defense primitives were intact going in.
6. **Lean hardening on healers.** Upstream's healers use minimal hardening (no `ProtectHome`/`ProtectSystem`). They're short-lived oneshots; the calculus is different from long-running services.
7. **Schema enum sync without breaking the validator.** The previous `HANDSHAKE-SCHEMA.json` had GM-era sources (atlas/sage/luma/etc.) but `dispatch_validator.ALLOWED_SOURCES` had been rewritten to Larry-era (beacon/forge/mirror/pulse). The two had been disagreeing silently. Schema rewritten to match validator exactly; added a description note that they must stay in sync.

**Verified end-to-end on the droplet (~$0.29 testing cost — same as D2 baseline):**

- **Hybrid smoke test (Pulse, Sonnet):** 5.01s, attempts=1, $0.033. Outbox populated with all new meta fields (`account_id: 'oauth'`, `attempts: 1`, etc.). Pulse correctly read her CLAUDE.md.
- **Validator-rejection:** 9-char prompt rejected with the F24 error, file in `.invalid/` with `.reason`. $0.
- **Parallelism (4 agents simultaneous):** all 4 picked up within **18ms** of each other. Pulse done in 5.01s, three Opus agents done in 10.01s (5s `CANCEL_POLL_INTERVAL` granularity adds up to ~5s wait after claude finishes — trade-off for cancel-marker responsiveness, tunable later). All 4 returned exact ACK tokens + correct persona identification. $0.255.
- **EMERGENCY_HALT:** flag touched → Forge thread detected first → all 4 exited within milliseconds → systemd did NOT auto-restart (clean exit, sticky-until-investigated by design). Manual `systemctl start` resumed cleanly with new PID.
- **Healer timers:** all 7 unit pairs parsed cleanly via `systemd-analyze verify`, enabled successfully, first runs completed with `success`/`ExecMainStatus=0` within the same session.

**Open issues entering D3:**

- 5s `CANCEL_POLL_INTERVAL` adds up to ~5s wait on short tasks. Cosmetic; tunable.
- During this session, Pulse's 4h `/cycle` timer fired on the droplet and the D2 auto-commit hook successfully committed her journal (`aade9d4`) — passive in-the-wild confirmation that D2's auto-commit + cost-capture infrastructure works.
- D2.5 did NOT pull `dispatch_sentinel.py` (Gap 2), `routing_validator.py` (Gap 3), or the requeue/retry/dead-letter logic (Gap 4) — those directly support D3's three flows and land with D3.
- Upstream `#240` (`heal_pr_auto_merge`) is deferred to D5+ (only matters when Forge starts opening PRs).

**Next:** Phase D3 — the three flows (Beacon ↔ Pulse dialogue, Larry-approval gate, Beacon → Forge dispatch), now grounded in concrete upstream references the audit identified (`process_outbox_notifications` lines 1869-1947 for dialogue, `continuation registry` pattern for approval, `safe_write_inbox` for dispatch). Plus Gaps 2/3/4 lands with D3.

---

## Phase D3 (commits 1–3 of 5) — dispatch back-channel + approval gate (2026-05-11 evening through 05-12 early, ~5 hours, ~$1.12 verification)

**Status entering this entry:** 3 of 5 D3 commits shipped + verified live. Dispatch back-channel + Larry-approval gate work end-to-end. Commit 4 (Forge full flow + clarification protocol) and commit 5 (sentinel timer) are the remaining work; estimated 3–4 sessions of careful design + code across the two.

**Design pre-session** (covered in conversation; for the record): D3 was scoped as Option C — D3 ships dispatch chain, D3.5 ships Forge→Mirror→Beacon review chain. Twelve architectural calls were surfaced + signed off before any code: free-text strict-whitelist approval (Call 2), no-timeout-auto-reject with reminders (Call 3), trust-policy substrate ships empty rules (Call 4), standalone outbox-notifier daemon (Call 4 packaging), Forge full flow through PR-open (Call 5), planted real follow-up as live test target (Call 6, watchdog-doc-fix), per-agent worktree opt-in (Call 9), task envelope provides `pr_title` (Call 10), strict reminder schedule 6/24/72h (Call 11), watchdog-enable as live target (Call 12). Plus six follow-on calls (13–18) for Forge preflight + clarification protocol: Forge-judged trigger (Call 13), create worktree on preflight (Call 14), wire `session_id` resume into watcher (Call 15), max-3 clarifications + escalation (Call 16), Beacon's clarification-vs-modification fork (Call 17), generalize clarification protocol to all agents now (Call 18).

### D3-prep (`6392c03`) — substrate, zero-risk additive

**Shipped** (11 files, +2167 lines, 69 new tests):

1. **`scripts/routing_validator.py`** — two-layer role-boundary enforcement. Hard topology (`FRESH_DISPATCH_ROUTES` + dialogue-suffix bypass + system-source bypass) is Larry-shaped, added on top of upstream's pattern. Soft IDENTITY.md reroute adapted verbatim from upstream `scripts/routing_validator.py`. New `RoutingDenied` exception on hard-topology violation.
2. **`scripts/dispatch_sentinel.py`** — stall detection for inbox + in-flight registry + leases. Inbox + lease scans adapted from upstream `scripts/dispatch_sentinel.py` (~lines 80–260). The in-flight registry scan with per-model thresholds (Sonnet 30m, Opus 60m, Haiku 15m, default 30m) is the D3-specific addition; upstream did not have this. Wrapped in a systemd timer in commit 5.
3. **`scripts/safe_write_inbox.py`** — validated atomic-write helper. Combines `dispatch_validator` + `routing_validator` + filename-length guard + tempfile-rename atomic write + audit log to `~/agents/logs/routing-events.jsonl`. Extracted from upstream `orchestrator.safe_write_inbox` lines 551–594.
4. **`scripts/trust_policy.py`** + **`config/trust-policy.json`** — autonomy-tier policy substrate. First-match-wins rule list; default-deny fallback; glob-based repo + file matching; malformed-policy fails-closed to `force_ask`. Default shipped policy = empty rules → every dispatch `force_ask`.
5. **`shared/HANDSHAKE-SCHEMA.json`** extended: new source enums (`forge-question`, `mirror-question`, `beacon-clarification`); new optional fields (`task_id` formalized, `intent`, `phase`, `target_repo`, `task_type`, `pr_title`, `pr_body_template`, `max_clarifications`, `clarification_count`, `session_id`, `expected_agent`).
6. **`scripts/dispatch_validator.py`** extended: same source enum additions, `ALLOWED_INTENTS` + `ALLOWED_PHASES` sets, optional validation for intent / phase / max_clarifications / clarification_count fields.
7. Tests: `test_routing_validator.py` (14), `test_safe_write_inbox.py` (14), `test_trust_policy.py` (16), `test_dispatch_sentinel.py` (9).

**Verified:** 85 unit tests pass on Python 3.12 droplet; `validate_agent_core.py` passes; three module `_self_test()`s pass. Zero live impact — nothing wired to live paths.

### D3-notifier (`b2b5c1f`) — back-channel routing + dead-letter + watcher routing wire (LIVE)

**Shipped** (4 files, +941 lines, 21 new tests):

1. **`scripts/outbox_notifier.py`** — long-running daemon, 5s poll. Two scans per cycle:
   - **Outboxes:** every completed `outboxes/<agent>/*.json`. Bare-agent sources notify back as `<agent>-result`. `*-question` sources notify back as `<agent>-clarification` with `intent=clarification-response` and `session_id` propagated for `--resume` (the watcher wiring lands in commit 4). Reply-leg sources (`*-result`, `*-clarification`, `*-answer`) and system sources are archive-only. Self-dispatch skipped. Failed tasks get a FAILED-framed notify. Adapted from `orchestrator.process_outbox_notifications` lines 1869–1947.
   - **Dead-letter:** `.invalid/*.json` scan. Validator-rejected tasks today land in `.invalid/` with a `.reason` sidecar and nobody is notified. This scan finds new entries, writes a dead-letter notify to the source agent's inbox (Gap 4 closed). State persisted at `~/agents/state/outbox-notifier-dead-letter.json` (dedup + GC).
   - Depth limiter at 1 (matches upstream's cap, orchestrator line 1878). EMERGENCY_HALT honored.
2. **`systemd/ourliberty-outbox-notifier.service`** — `Type=simple`, hardening parity with the watcher, ordered `After=ourliberty-inbox-watcher.service`. **Live on droplet.**
3. **`scripts/inbox_watcher.py`** modified — D3 defense-in-depth: `routing_validator.check_hard_topology()` called after `dispatch_validator` on every pickup. Catches tasks that bypassed `safe_write_inbox`. Rejections land in `.invalid/`; the notifier then dead-letters back to the source.
4. **`scripts/tests/test_outbox_notifier.py`** — 21 tests covering routing decisions, depth cap, partial-JSON tolerance, short-prompt padding, dead-letter dedup + GC.

**Verified live on droplet (~$0.62):**
- Dropped a `source: pulse` task to Beacon's inbox → watcher picks up → Beacon ACKs (Sonnet, 5.02s, $0.031) → outbox archived → notifier sees `source=pulse` → writes `notify-*` to Pulse's inbox with `source=beacon-result` → Pulse's watcher picks up → Pulse processes (3.4 min, $0.59) → outbox archived → notifier sees `source=beacon-result` (reply leg) → archive-no-notify. Loop terminated by dialogue-suffix bypass. Full audit trail in `~/agents/logs/routing-events.jsonl`.
- Pulse's 3.4-min run was over-budget because the notify prompt arrived naked ("Task result from beacon: SUCCESS\n\n...") with no framing telling her "this is an inter-agent notify; archive it." **Lesson filed for commit 4's clarification-response prompt template.**

**Anomaly observed:** `archive failed for ... no such file or directory` warning on Pulse's inbox. A healer (likely `heal_abandoned_inbox_tasks` or `heal_blocked_inbox_age`) moved the inbox file mid-3.4-min-processing. Pre-existing watcher/healer race; not D3-introduced. Filed for D5.

### D3-approval (`dc3cb81` + fixup `4e4f34a`) — Larry-approval gate via Telegram (LIVE)

**Shipped** (4 + 2 files, +1336 / -38 lines, 39 new tests):

1. **`scripts/beacon_approval_handler.py`** — pure-logic library, no I/O coupling. Provides:
   - `parse_user_reply` — strict whitelist on positive confirmation (`approve` / `yes` / `go` / `ok` / `okay` / `ship` / `ship it`, exact match after case-fold + strip). Ambiguous text returns `none` → bot forwards to Beacon for clarification, never inferred approval. Modify / reject use prefix grammar (`modify:` / `reject:` followed by free-text reason). Pause / resume commands (`/pause` / `/resume`).
   - `extract_approval_request` — regex extraction of `=== APPROVAL_REQUEST === {json} === END_APPROVAL_REQUEST ===` blocks. Validates required fields (`task_id`, `summary`, `target_agent`, `prompt`). Raises `MalformedApprovalMarker` on missing fields or invalid JSON. Returns parsed payload + narrative-with-marker-stripped.
   - State file CRUD at `~/agents/state/beacon-pending-approvals.json` (runtime tree). Operations: `add_pending`, `find_pending_by_id`, `most_recent_pending`, `resolve` (status ∈ {approved, rejected, modified, expired}), `pop_paused_backlog`. History capped at 1000.
   - `trust_decision` bridge to `trust_policy.evaluate`.
   - `due_reminders` + `record_reminder_sent` — 6h / 24h / 72h schedule, deduped, suppressed during pause.
   - `is_paused` / `set_paused` — file flag at `~/agents/blackboard/APPROVALS_PAUSED`.
   - DM formatters and `dispatch_approved` (invokes `safe_write_inbox` with forced `source='beacon'`).
2. **`scripts/beacon_telegram_bot.py`** extended — two intercept points + reminder sweep:
   - **Before forwarding user message:** `parse_user_reply` → if recognized command, handle directly (no Beacon call). Approve dispatches via `dispatch_approved` + confirmation DM. Modify / reject resolve + send a structured relay note to Beacon. Pause / resume toggle the flag.
   - **After Beacon's response:** `_send_beacon_response` calls `extract_approval_request`. If marker present, consult `trust_decision`, route to one of three paths: auto-dispatch + one-liner, queue + formatted approval DM, or policy rejection. Marker stripped from narrative.
   - **Every ~5 min in the polling loop:** `_check_due_reminders` DMs nudges.
   - **Defensive per-update try/except** wraps `_process_update` — a single bad update never crashes the bot (prevents the replay loop documented below).
3. **`agents/beacon/CLAUDE.md`** — new section "How you dispatch work to Forge — the APPROVAL_REQUEST marker (Phase D3)". Documents the marker format (required + optional fields), the bot's behavior, the user-reply grammar Beacon should expect Larry to use, the pause / resume semantics, and a self-check before emitting. Replaces the old "Don't dispatch work to other agents" instruction.

**Verified live on droplet (~$0.50):**
- Larry messaged Beacon "*propose a tiny plan: fix the watchdog warning in operating-manual.md*". Beacon read her updated CLAUDE.md, emitted a clean marker with a detailed payload (her own preflight branching for Forge based on the actual `systemctl is-enabled` state, exact line numbers verified by grep, success criteria, out-of-scope list, "if you get stuck" guidance, `max_clarifications=2`). Bot extracted, persisted, DMed Larry the formatted approval request.
- Larry replied `reject: smoke test only`. Bot resolved the entry as rejected, archived to history with `resolution_note`, DMed confirmation, relayed to Beacon. Beacon acknowledged.

**Two bugs caught + fixed during the smoke test:**

1. **State file path in repo tree, not runtime tree.** Initial commit placed `pending-approvals.json` at `agent-core/agents/beacon/state/` (REPO). The beacon-bot service has `ProtectHome=read-only` + `ReadWritePaths` covering `~/agents` but NOT `~/agent-core`. First `add_pending` call failed with `Errno 30: Read-only file system`, crashing the bot. **Fix:** moved state to `~/agents/state/beacon-pending-approvals.json`. State isn't source-of-truth and shouldn't be in the repo anyway. (Commit `4e4f34a`.)
2. **Bot crash → systemd restart → message replay loop.** `Restart=on-failure` combined with in-memory `offset` (not persisted) means a crash mid-message replays the SAME update on restart. Beacon got Larry's prompt 7 times consecutively before we noticed, burning ~$0.40 of Sonnet tokens. **Fix:** defensive `try/except` per-update in the main loop. (Commit `4e4f34a`.)

**Bug + verification cost:** ~$0.40 burned during the replay loop, ~$0.10 for the successful retry. Both bugs would have been worse to hit in commit 4 when Forge consumes real Sonnet/Opus tokens.

**Codified conventions worth recalling:**

1. **State files in runtime tree, not repo** — the repo (`~/agent-core/`) is read-only for services with `ProtectHome=read-only`. Runtime (`~/agents/`) is the only writable location across all current + planned services. Any future state file must follow this.
2. **Strict positive-confirmation whitelist** — never infer approval from ambiguous text. Lesson borrowed from upstream's `INTERNAL_ACK_PREFIXES` filter (orchestrator lines 309–388).
3. **Marker convention with required-field validation** — the bot rejects malformed markers explicitly rather than silently mis-parsing. Beacon gets a warning DM and can re-emit.
4. **Notify task source override in `dispatch_approved`** — always sets `source='beacon'` on the envelope so a buggy marker payload cannot impersonate another agent.

**Open issues entering commit 4 (D3-forge):**

- **`ReadWritePaths` on `ourliberty-inbox-watcher.service` must include `~/agent-repos/`** before live worktree creation. Forge worktrees go to `~/agent-repos/<repo>/.worktrees/<task-id>/` which is NOT in any current `ReadWritePaths`. Pre-deployment blocker.
- **Notify prompt framing** — the over-budget Pulse run during D3-notifier smoke ($0.59) was the receiver agent interpreting a naked notify as new work. Commit 4 needs a refined notify-template that tells the receiver "this is an inter-agent notify of intent X; do Y with it" (especially for `clarification-response` and dead-letter cases).
- **`outbox_notifier.service` has `/home/larry/agent-core` in `ReadWritePaths` unnecessarily** — cosmetic security cleanup, file for D5.
- **Concurrent state writes between bot main loop and reminder sweep** — currently single-threaded so no race, but `load_state → modify → save_state` is not atomic if we ever multithread. File for D5.
- **Telegram rate limit on huge `/resume` backlogs** (>30 entries to one user) — edge case, file for D5.
- **`ourliberty-watchdog.timer` still disabled** — design Call 12 nominated this as commit 4's live test target. Beacon already produced a high-quality plan for it during the D3-approval smoke (now in history); commit 4 will re-run the flow with `approve` instead of `reject`.
- **Default trust policy ships empty** — every dispatch `force_ask`s today. Larry's dial; he edits `config/trust-policy.json` to add carve-outs as confidence grows. No code change needed.

**Next:** Commit 4 (D3-forge) — preflight protocol + worktree machinery + session_id resume in watcher + Forge's CLAUDE.md update + clarification routes wired + `gh pr create` plumbing + ReadWritePaths fix on watcher. Plus commit 5 (D3-sentinel) wrapping the sentinel script as a systemd timer. See `docs/d3-commit-4-plan.md` for the structured plan.

## Phase D3 (commit 4a of 5) — Forge preflight markers + intent-aware notify template (2026-05-12, single afternoon, ~$0.42 live smoke)

**Status entering this entry:** 4 of 5 D3 commits shipped + live + smoke-tested end-to-end. Forge's preflight marker pipeline operates against the real droplet daemons. 4b (worktree machinery + actual code writing + `gh pr create`) and commit 5 (sentinel systemd timer) remain.

**Design pre-session** (covered in conversation; for the record): D3 commit 4 was split into 4a + 4b per design Call (sequencing values dial). 4a ships the *marker pipeline* — preflight grammar, clarification budget cascade, intent-aware notify template. 4b ships the *worktree-and-PR machinery*. Splitting matches the "highest-risk single commit of D3" framing in the original plan; verification gets two smaller live tests instead of one big one. Seven architectural decisions were surfaced + signed off before any code: (1) `/tmp/wt-<agent>-<task_id>/` worktree path keyed by task_id, mirroring `merge_gates._ensure_pr_worktree` reuse pattern over `agent_runner.create_worktree_for_task` per-dispatch-fresh pattern — deferred to 4b; (2) two-invocation preflight→build with `--resume` (revisit collapse to one after 10+ successful runs); (3) `worktree_manager.py` as a new module called from `inbox_watcher.process_task`, not buried in `run_claude` — deferred to 4b; (4) `allowed_repos` enforcement via `routing_validator.check_target_repo` extension, parallel to `check_hard_topology` — deferred to 4b; (5) defer watchdog timer install — commit 5 ships it; (6) Option-C hybrid notify-prompt template (shared skeleton + per-intent action block); (7) Level-3 strict marker discipline (full block + required-field validation, mirror of Beacon's APPROVAL_REQUEST grammar). Plus a meta-decision Larry surfaced mid-session: classify every architectural call as TECHNICAL / ARCHITECTURAL / VALUES before asking — fake-asking on pattern-fit questions creates rubber-stamp validation that erodes signal on real values calls. Codified as `feedback_decision_classification.md` in the auto-memory.

### D3-forge-4a (`682e0ec`) — preflight markers + intent-aware notify template + clarification cascade (LIVE)

**Shipped** (9 files, +2015 / -46 lines, 58 new tests):

1. **`scripts/forge_preflight_handler.py`** (new, 280 lines) — pure-logic marker library mirroring `beacon_approval_handler` shape. `parse_forge_marker` extracts the first valid block (PROCEED / CLARIFY_REQUEST / REJECT) with strict block delimiters, required-field validation, and rejects multi-marker outputs (`MultipleForgeMarkers` raised for any two-or-more markers — homogeneous OR heterogeneous; the routing decision must be unambiguous). `evaluate_clarification_budget` returns `(decision, next_count, max_count)` with `allow` / `exhausted` tags. `clarifications_remaining`, `derive_notify_source`, `derive_intent`, `build_exhausted_reason` round out the library. Stateless — all budget state rides on the task envelope per HANDSHAKE schema extension from D3-prep.

2. **`scripts/outbox_notifier.py`** extended (+606 / -77) — marker-driven routing path layered on top of D3-notifier's default flow. `_classify_forge_marker` returns a routing decision dict (intent, notify_source, intent_kwargs, next_clarification_count). `process_outbox` branches: if `agent == 'forge'` and a marker is found, marker-driven routing fires — bypasses `_should_notify_back` filter (the protocol's multi-hop reply-leg traffic would otherwise archive) and bypasses `MAX_NOTIFY_DEPTH` (the `max_clarifications` budget on the envelope replaces it as the termination guard for the clarification cascade). Default path also bypasses depth cap for `*-question` sources (Beacon's clarification-answer leg is intentional multi-hop). `build_notify_prompt` (replaces D3-notifier's `_build_notify_prompt`) renders the Option-C hybrid template: `[Inter-agent notify | intent={intent} | from={sender} | task={task_id} | status={status}]` header + framing + per-intent action block from `INTENT_ACTION_BLOCKS` dispatch table + truncated sender output. Eight intents wired: `result-notification`, `clarification-response`, `clarify`, `ack-proceed`, `reject`, `clarification-exhausted`, `dead-letter`, `marker-error`. Marker-error cascade with retry counter capped at 3 retries before dead-lettering to the original dispatcher (`_dead_letter_marker_error_to_dispatcher`), and `original_source` field propagated through the marker-error envelope so a recovered marker's routing target survives the round-trip. `scan_dead_letters` switched from its own hardcoded prompt to `build_notify_prompt(intent='dead-letter', ...)` for consistency.

3. **`scripts/dispatch_validator.py`** extended (+18) — three new intents added to `ALLOWED_INTENTS` (`result-notification`, `dead-letter`, `marker-error`); existing D3-prep vocabulary preserved (`ack-proceed`, `clarify`, `clarification-response`, `clarification-exhausted`, `reject`). One new source: `outbox-notifier` (the notifier itself emitting marker-error retries back to Forge).

4. **`scripts/routing_validator.py`** extended (+2) — `outbox-notifier` added to `SYSTEM_SOURCES` (it's an infra source, not a dialogue leg; the initial `outbox-notifier-result` naming was a `-result`-suffix hack that the independent review caught and we corrected).

5. **`scripts/inbox_watcher.py`** modified (+10) — `_build_outbox` propagates seven envelope fields from the original inbox task to the outbox JSON so the notifier can read them without re-reading archived inbox files: `clarification_count`, `max_clarifications`, `phase`, `target_repo`, `task_type`, `original_source`, `marker_error_count`. The last two survive across the marker-error cascade.

6. **`agents/forge/CLAUDE.md`** extended (+60 lines) — new "Preflight discipline" section. Four-step protocol (read spec → read referenced files → probe environment → decide marker). Explicit marker formats with required-field documentation. Strict grammar rules (one marker per response; case-sensitive delimiters; JSON must parse; marker is the last meaningful thing). Clarification budget explanation. "Buildable" criteria. File-unreadable handling. Existing "Build Loop" section preserved (used in build phase, lands in 4b).

7. **`agents/beacon/CLAUDE.md`** extended (+76 lines) — new "How you handle Forge's preflight markers" section with five intent shapes (`clarify`, `ack-proceed`, `reject` / `clarification-exhausted`, `marker-error` notifies she won't see directly, `dead-letter` notifies). Each shape has its own decision-fork heuristic. Clarification-vs-modification fork explicit: "can I answer this question without changing what Forge is supposed to build? If yes, answer. If no, emit a new APPROVAL_REQUEST." All intent names match dispatch_validator's canonical vocab (the independent review caught initial naming drift — `preflight-proceed` / `preflight-rejection` / `clarification-request` were better names but inconsistent with the already-shipped D3-prep schema; we reverted to D3-prep names to keep the system uniform).

8. **`scripts/tests/test_forge_preflight_handler.py`** (new, 25 tests) — marker extraction (3 types + narrative stripping), malformed-JSON / missing-field rejections, multi-marker rejections (heterogeneous AND homogeneous — same marker type twice), budget evaluation (allow / exhausted at boundary; default-max-fallback for non-int / negative inputs), routing helpers.

9. **`scripts/tests/test_outbox_notifier.py`** extended (+501 lines, 33 new tests) — three test classes added: `BuildNotifyPromptTest` (10 tests covering all eight intent action blocks + the `KeyError, IndexError` graceful-degrade path + the validator-floor padding), `ForgeMarkerRoutingTest` (12 tests: each marker type's routing decision, budget-exhausted conversion to `clarification-exhausted` intent, depth-cap + should-notify-back bypasses for markers, marker-error cascade with `original_source` propagation and retry cap dead-letter, backward-compat for non-marker outboxes), `ClassifyForgeMarkerTest` (4 unit tests on the classifier helper). Plus the M5 integration test `test_full_cascade_three_forge_two_beacon` running the full Beacon→Forge→Beacon→Forge→Beacon cascade with count propagation asserted at each step.

**Independent code review caught (and fixed in same commit) before push** — Larry asked for a subagent assessment before deploy. Spawned a general-purpose reviewer with no shared context, instructed to surface issues. Found 5 real issues:

1. **CRITICAL — Marker-error recovered output was a routing black hole.** When Forge's first marker was malformed and the notifier dead-lettered back to Forge with `source=outbox-notifier`, Forge's NEXT outbox would have `source=outbox-notifier` which has no `_primary_agent_id` mapping — recovered marker had nowhere to route. Every malformed-first-marker dispatch would silently die after recovery. **Fix:** propagate `original_source` field through the marker-error envelope; the notifier reads it as a fallback for routing.

2. **CRITICAL — No retry cap on marker-error cascade.** Theoretical wedge loop if Forge persistently emits bad markers. **Fix:** `MAX_MARKER_ERROR_RETRIES = 3`; on exceeded, `_dead_letter_marker_error_to_dispatcher` writes a `dead-letter` notify to Beacon and stops asking Forge to retry.

3. **MAJOR — Two same-type markers parsed as one.** Original `parse_forge_marker` only counted distinct marker types; two CLARIFY_REQUEST blocks didn't raise. **Fix:** count all `regex.finditer` matches across all types; raise `MultipleForgeMarkers` on `>1` total.

4. **MAJOR — Beacon's CLAUDE.md used wrong intent names.** Initial draft used self-descriptive names (`preflight-proceed`, `clarification-request`, `preflight-rejection`); the actual emitted intents are D3-prep's `ack-proceed`, `clarify`, `reject`, `clarification-exhausted`. Beacon reading her CLAUDE.md would have looked for header tags that never appear. **Fix:** rewrote Beacon's CLAUDE.md to use the actual canonical names; preserved D3-prep's schema as the source of truth.

5. **MAJOR — `notify_task['intent']` not always set on envelope.** Default-path notify tasks only set `intent` for clarification-response; `result-notification` and `dead-letter` envelopes had no intent field. Receivers reading `task['intent']` would see undefined. **Fix:** always set on every notify_task.

Plus several minor refactors: `outbox-notifier-result` source renamed to `outbox-notifier` and moved to `SYSTEM_SOURCES` (semantically correct — infra source, not dialogue leg); `_classify_forge_marker` refactored to call `derive_intent` instead of hardcoded strings (centralizes the mapping); `scan_dead_letters` switched from inline-hardcoded prompt to `build_notify_prompt` for consistency; depth-cap bypass added for `*-question` source (clarification answer leg is intentional multi-hop, otherwise full cascade fails); a dead-code GC line in `scan_dead_letters` deleted; Forge CLAUDE.md updated to mention the 3-retry marker-error cap + handling of unreadable files.

**Verified live on droplet (~$0.42, 45 seconds end-to-end):**
- Synthetic Beacon→Forge preflight task dispatched via `safe_write_inbox` with `phase=preflight`, `max_clarifications=3`, `clarification_count=0`. Prompt: "Read `~/agent-core/scripts/outbox_notifier.py`, find the line number for `INTENT_ACTION_BLOCKS`, count the intent keys, emit one PROCEED marker. This is the live smoke; no file edits." Cost: $0.22, 15.01s, Opus.
- Forge emitted a clean PROCEED block: line 122, 8 distinct keys (matched the actual dict). Brief reasoning sentence above the marker; marker as the last meaningful content; all required fields present.
- Notifier picked up Forge's outbox, `_classify_forge_marker` returned `{intent: 'ack-proceed', notify_source: 'forge-result', ...}`, wrote `notify-smoke-4a-001.json` to Beacon's inbox. Log: `marker-notified beacon <- forge (forge-result, intent=ack-proceed, file=notify-smoke-4a-001.json)`.
- Beacon's watcher picked it up. Cost: $0.20, 25.01s, Opus. Beacon recognized `intent=ack-proceed` as Shape 2 in her new CLAUDE.md section, journaled to `memory/2026-05-12.md`, took no follow-up action — exactly as instructed.
- Beacon's outbox archived. `_should_notify_back('forge-result', 'beacon')` returns False (reply-leg suffix → archive-no-notify); cascade terminated correctly at depth 1.

**One minor observation:** Forge's marker used `task_id: "d3-forge-4a-smoke"` instead of the envelope's `task_id: "smoke-4a-001"`. Routing worked anyway (notifier uses outbox filename stem, not marker payload's task_id). Worth tightening in 4b — add a `marker_task_id == envelope_task_id` assertion in `_classify_forge_marker` so Forge can't drift on identifier discipline.

**Codified conventions worth recalling:**

1. **Intent vocabulary is owned by `dispatch_validator.ALLOWED_INTENTS`.** Don't invent new names in templates or docs that aren't in the canonical set. CLAUDE.md sections that reference intents must match the validator's strings exactly.
2. **Marker-driven routing bypasses default filters intentionally.** `_should_notify_back` and `MAX_NOTIFY_DEPTH` are good defaults for ad-hoc traffic but wrong for protocol traffic. The clarification budget on the envelope is the termination guard for marker cascades; the depth cap is the termination guard for ad-hoc cascades. Don't conflate.
3. **State that needs to survive a cascade rides on the envelope, not in a state file.** Clarification count, marker-error count, original source — all propagated outbox → notify → next inbox → next outbox. Watcher's `_build_outbox` is the propagation point.
4. **Strict block-delimited markers + required-field validation + multi-marker rejection** is the right discipline level. Level-3 of the 1–5 dial we surfaced during design. Catches drift loudly; revisit dialing down to level 2 only after Forge has 10+ successful runs of clean marker discipline.
5. **Independent code review before push is worth the time.** The 5 issues the subagent reviewer caught would all have manifested in live traffic — at least one (the marker-error black hole) on the very first malformed marker. The reviewer's $0 cost vs the cost of debugging a live regression is wildly favorable.

**Open issues entering commit 4b:**

- **Worktree creation, session_id resume in watcher dispatch path, `gh pr create`** — the headline 4b work. Per design Call 1, worktree path is `/tmp/wt-<agent>-<task_id>/` keyed by task_id with reuse-if-exists; cleanup via daily `cleanup_stale_worktrees.py` timer (Gap 10 in the audit, port from upstream).
- **Marker task_id discipline** — add `marker_task_id == envelope_task_id` check in `_classify_forge_marker`. Minor preflight discipline issue surfaced in the smoke; not blocking, easy to fix.
- **Watcher's `--resume` wiring** — `agent_runner.run_claude` already supports `session_id` param; `inbox_watcher.process_task` doesn't pass it through. ~1 line change once we want it.
- **Local `.claude/settings.local.json` not gitignored** — harness blocked the .gitignore append (counted as self-modification of agent settings). Larry to add manually.
- **Two notifier warnings during droplet test run** (`intent_kwargs incomplete for intent=marker-error / clarification-response`) — these are the graceful-degrade path firing during unit tests that deliberately omit kwargs. Expected behavior, not a regression. The warning message itself is good telemetry; consider lowering severity to INFO when triggered by the recognized test path.

**Next:** Commit 4b (D3-forge-build) — `worktree_manager.py` module, watcher worktree-wire-up, `routing_validator.check_target_repo`, `agent-models.json` `worktree_enabled: true` + `allowed_repos`, Forge CLAUDE.md build-phase content, `cleanup_stale_worktrees.py` + systemd timer, live test on `watchdog-doc-fix-001` (the doc cleanup task Beacon already planned during D3-approval smoke). Plus commit 5 (D3-sentinel) wrapping `dispatch_sentinel.py` as a systemd timer and enabling the watchdog timer as the same commit's live test.

## Phase D3 (commit 4b of 5) — worktree machinery + build-phase dispatch + cleanup timer (2026-05-12, single afternoon, ~$2.50 across two live smokes)

**Status entering this entry:** 5 of 5 D3 commits' worth of code in tree (4b + a 4b followup); commit 5 (sentinel timer install) remains. Forge now actually writes code to a real repo via the full preflight → PROCEED → build → `gh pr create` chain, in keyed-reuse worktrees, with `--resume` carrying the preflight session into build. Two real PRs opened against `ourliberty-agent-core` during the live smokes.

**Design pre-session** (covered in conversation; for the record): the 4b state-summary surfaced seven architectural calls that were already pre-decided in 4a's design session, plus five open questions that 4b needed to resolve. Larry signed off on the recommendations after a single round of classification per the feedback memory:

- **Q1 VALUES** — Where does the build-phase re-dispatch get written from? **Outbox notifier** (extends the ack-proceed handler), not Beacon (would add a hop), not the watcher (mixes concerns). Larry's autonomy-vs-ceremony dial: once Beacon's spec is approved upstream, Forge's PROCEED is self-attesting; no double-approval.
- **Q2 ARCHITECTURAL** — Canonical repo for `git worktree add`. **Treat `~/agent-core` itself as the canonical** rather than cloning into `~/agent-repos/<repo>/`. Single source of truth; worktrees go to `/tmp` so the canonical's working tree stays clean. Flagged the multi-repo expansion (when `allowed_repos` grows) as a future iteration.
- **Q3 TECHNICAL** — Branch hint source. **Read from `task['branch']` envelope field**, not from `Branch hint:` regex on the prompt. Envelope is the contract.
- **Q4 TECHNICAL** — `gh pr create` invocation locus. **Forge runs it herself** as the final step of her build phase; infra just ensures `gh` is in the worktree's env (already authed as `Larry-Yatch` with `repo` + `workflow` scopes).
- **Q5 TECHNICAL** — `worktree_manager.py` module surface. **New module purely additive**; existing `agent_runner.create_worktree_for_task` + `setup_branch_checkpoint` left alone (their `agent_id == 'main'` codepath is unused by Larry but kept). Don't refactor working code; minimize change risk.

### D3-forge-4b (`111b712`) — worktree machinery + build-phase dispatch + cleanup timer (LIVE)

**Shipped** (13 files, +1812 / -10 lines, 46 new tests):

**New (5 files):**

1. **`scripts/worktree_manager.py`** (new, 370 lines) — keyed-reuse worktree manager. Combines two upstream patterns: `agent_runner.create_worktree_for_task` (mechanics: `git worktree add --detach`, /tmp base, stem sanitization) + `merge_gates._ensure_pr_worktree` (reuse-if-exists keyed by stable identifier, tear-down-if-stale). Public API:
   - `ensure_worktree_for_task(agent_id, task_id, canonical_repo, branch=None, log_fn=None)` — high-level entry called from `inbox_watcher.process_task`. Returns `(worktree_path, branch_set_or_None)`. Idempotent on the same `task_id`.
   - `create_or_reuse_worktree_for_task` — handles four cases: both on-disk and registered (reuse); only on-disk (stale, remove + recreate); only registered (orphan, prune + recreate); neither (fetch + create).
   - `setup_branch_checkpoint` — `checkout -B → empty WIP commit → push -u origin` with `--force-with-lease` fallback. 4b review fix: skips the empty commit when HEAD subject already matches `[WIP][session-start] <task_id>` so re-dispatches don't stack empty commits.
   - `worktree_path_for(agent_id, task_id)` — deterministic path `/tmp/wt-<agent>-<task_id>/` (no timestamp; reuse-keyed by task_id per the merge_gates pattern, NOT the agent_runner per-dispatch-fresh pattern).
2. **`scripts/cleanup_stale_worktrees.py`** (new, 165 lines) — port from upstream's 123-line `gm-agent-core` script (Gap 10 closed). Adaptations: paths joe→larry, `CANONICAL_REPOS` list (so multi-repo expansion is one-line when `allowed_repos` grows). 4b review fix: skips worktrees referenced by `~/agents/state/in-flight/` so a long Read-heavy build (no mtime touch) isn't reaped mid-flight. Daily timer (`OnUnitActiveSec=1d`, `Persistent=true`), first run on `enable --now` was a clean no-op (`Removed: 0, Kept: 1` for the canonical itself).
3. **`scripts/tests/test_worktree_manager.py`** (new, 17 tests) — ephemeral git origin + canonical fixtures for real-op coverage. Covers: path sanitization, deterministic keying, fresh-create, reuse, stale-dir cleanup, orphan-registry pruning, branch checkpoint push (with `receive.denyCurrentBranch=updateInstead` on origin), bad-branch return, missing-worktree return, idempotent `ensure_worktree_for_task`.
4. **`systemd/ourliberty-cleanup-stale-worktrees.service` + `.timer`** (new) — installed at `/etc/systemd/system/`, enabled. `User=larry`, `Type=oneshot`, `EnvironmentFile=/home/larry/credentials/.env.larry`. `systemd-analyze verify` clean.

**Modified (8 files):**

5. **`scripts/inbox_watcher.py`** (+57 lines) — calls `worktree_manager.ensure_worktree_for_task` when `models_config[agent].worktree_enabled`. Threads `session_id` from envelope to `agent_runner.run_claude` for `--resume`, but **only when `task.phase == 'build'`** (intentional conservative gate — 4a's `notify_task['session_id']` propagation sets the SENDER's session on receiver tasks, which would resume the wrong agent's conversation; gating to `phase=build` matches the one supported continuation case). New `CANONICAL_REPO_PATHS` map (`ourliberty-agent-core` → `~/agent-core`). `check_target_repo` defense-in-depth call. Skips identity-assertion preamble on `--resume`. `_build_outbox` now propagates `branch` + `pr_title` + `pr_body` envelope fields (4b review fix — Beacon's PR title would otherwise drop at the preflight→outbox→build boundary).
6. **`scripts/outbox_notifier.py`** (+125 lines) — three extensions:
   - `_classify_forge_marker` asserts `marker.task_id == envelope.task_id`, raising `MalformedForgeMarker` on mismatch (the 4a smoke caught a drift here; tightens discipline).
   - New `_dispatch_build_phase(data)` helper — fires after the ack-proceed notify-to-Beacon when `marker_type == 'proceed'`. Writes `build-<task_id>.json` to Forge's inbox with `phase=build`, `source=beacon`, `session_id=<preflight session>`, `dispatched_by=outbox-notifier`. Propagates target_repo, branch, pr_title, pr_body, max_clarifications. Idempotent on existing file (4b review fix: guards against notifier-crash-restart double-dispatch).
   - `_notify_forge_marker_error` propagates `target_repo` + `branch` forward (4b review fix — same shape as the 4a marker-error black hole on a different code path: malformed-marker retry needs target_repo or the worktree gate refuses).
7. **`scripts/routing_validator.py`** (+90 lines) — new `check_target_repo(target_agent, target_repo)` function, parallel to `check_hard_topology`. Reads `agent-models.json:agents.<agent>.allowed_repos` via a lazy-loaded cached config. Fails open in two cases (back-compat): `target_repo` is None/empty, or `allowed_repos` is unconfigured/empty. Fails closed when both are set and `target_repo` isn't on the allow-list.
8. **`scripts/safe_write_inbox.py`** (+15 lines) — calls `check_target_repo` after `validate_route`; adds `target_repo` to the routing-events audit log.
9. **`config/agent-models.json`** — Forge gets `worktree_enabled: true` + `allowed_repos: ["ourliberty-agent-core"]`. `_history` entry appended.
10. **`agents/forge/CLAUDE.md`** (+100 lines) — new "Build phase protocol" section (worktree location, branch convention, conventional commits, `gh pr create` flow with envelope-driven `pr_title` + `pr_body` template, "start with `PR opened: <url>`" rule, "no marker block in build phase" rule). Preflight section cross-references the now-wired allow-list. Tier-rules line clarified: direct-commit-to-main only applies to ad-hoc Larry-chat work, never to inbox dispatch.
11. **`scripts/tests/test_routing_validator.py`** (+95 lines) — new `CheckTargetRepoTest` class (9 tests).
12. **`scripts/tests/test_outbox_notifier.py`** (+360 lines) — new `BuildPhaseDispatchTest` class (8 tests, seeded with realistic `agent-models.json` so the realistic allow-list shape is exercised, not the fail-open path). Marker task_id assertion tests (3). Marker-error propagation test (1).

**Independent code review caught (and fixed in same commit) before push** — Larry asked for the same subagent assessment as 4a's review. The reviewer spent 60 seconds reading + 4 minutes critiquing across worktree lifecycle, build re-dispatch correctness, session_id gate logic, check_target_repo enforcement, marker task_id assertion, gh pr create flow, cleanup race, CLAUDE.md instructions, test coverage gaps. Found 6 real issues:

1. **CRITICAL — Marker-error retry was a black hole on worktree_enabled agents.** Same shape as 4a's CRITICAL-1, different path: `_notify_forge_marker_error` wrote the retry task without `target_repo`. With Forge's `worktree_enabled: true`, the watcher rejects with `target_repo: no canonical path` and the malformed marker recovery silently dies. **Fix:** propagate `target_repo` + `branch` into the notify_task at `outbox_notifier._notify_forge_marker_error`.
2. **CRITICAL — Beacon's `branch` / `pr_title` were dropped at build dispatch.** `_build_outbox` only propagated `target_repo`/`task_type`/etc; `branch`/`pr_title`/`pr_body` were not propagated. `_dispatch_build_phase` then read None for branch and fell back to the default — any explicit branch from Beacon's spec was silently overwritten. **Fix:** extend `_build_outbox`'s envelope_field list to include those three.
3. **MAJOR — Empty WIP commits stacked on every dispatch.** `setup_branch_checkpoint` ran `git commit --allow-empty` unconditionally; reuse across preflight + build = 2+ empty commits before any real work. **Fix:** gate the empty commit on `git log -1 --pretty=%s` matching the WIP subject.
4. **MAJOR — Duplicate build dispatch on notifier crash-restart.** Notifier crashing between `safe_write_inbox(build_task)` and `_archive_outbox(preflight_outbox)` would re-process on restart, writing a second build task that resumes a terminated session. **Fix:** idempotency check on existing `build-<task_id>.json` in forge's inbox or archive before writing.
5. **MAJOR — `BuildPhaseDispatchTest` didn't seed agent-models.json.** Existing tests passed via the fail-open path; the realistic prod config (`allowed_repos: ["ourliberty-agent-core"]`) was unexercised. **Fix:** seed `MODELS_CONFIG_PATH` under `rv.REPO_ROOT` in setUp; added a negative test where build dispatch with a target_repo outside the allow-list is blocked by `safe_write_inbox`.
6. **MAJOR — Cleanup race during long Read-heavy builds.** `mtime > 24h` + daily timer + Read-heavy builds = potential mid-build worktree removal. **Fix:** cleanup loads `~/agents/state/in-flight/` task_stems and skips any worktree whose path contains a live stem.

Plus minor cleanups: CLAUDE.md tier-rules line scoped (direct-commit exception only outside inbox dispatch); dead-code branch in build prompt removed.

**Verified live on droplet — two smokes, total ~$2.50.**

**Live test 1 — `watchdog-doc-fix-001`** (95s, $0.78, **PR #1** opened):

Dispatched the same spec Beacon emitted during D3-approval smoke (archived as rejected in her pending-approvals history). Cascade ran clean through the 4b machinery:

- `safe_write_inbox` with `target_repo=ourliberty-agent-core` passed allowed_repos check ✓
- Watcher picked up; `worktree_manager.ensure_worktree_for_task` created `/tmp/wt-forge-watchdog-doc-fix-001` ✓
- `setup_branch_checkpoint` pushed `forge/watchdog-doc-fix-001` to origin with empty WIP checkpoint ✓
- Forge ran in `working_dir=/tmp/wt-forge-watchdog-doc-fix-001`, 95s on Opus ✓
- Forge made the doc edit, committed (conventional-commit msg), pushed, ran `gh pr create`, returned `PR opened: https://github.com/Larry-Yatch/ourliberty-agent-core/pull/1` ✓
- Notifier processed the outbox, wrote ack-back to Beacon's inbox ✓
- Beacon journaled the result ✓
- Final PR diff: 1 insertion, 1 deletion in `docs/operating-manual.md:258`; PR body has the systemctl ground-truth output and cites the D2.5 criterion. Title matches `pr_title` envelope field exactly.

**Caveat:** Forge **fast-pathed preflight → build in a single invocation**. Her output went through the *default* routing path (no marker emitted) — so the build-phase re-dispatch with `--resume` did NOT fire on test 1. The CLAUDE.md preflight section says "do NOT write code in preflight, decide one of three markers"; the task `prompt` itself said "REQUIRED STEPS: 1. Capture ground truth; 2. Pick the branch; 3. Edit; ..." which Forge read as build-phase instructions. End result correct but the marker pipeline wasn't exercised end-to-end. This is a discipline / prompt-strictness concern, NOT a 4b machinery concern — the build-phase dispatch IS covered by unit tests (`BuildPhaseDispatchTest`). Tightening the preflight-phase prompt template (or adding a hard runtime check that Forge's preflight output ends with a marker) is a follow-up for Beacon's spec-emission template.

**Live test 2 v1 — `pulse-cost-note-002`** (failed; cost $0.51):

Deliberately ambiguous spec ("update the Pulse cost line, but here are three candidate locations") designed to force CLARIFY_REQUEST and exercise the round-trip:

- Forge preflight emitted a clean `CLARIFY_REQUEST` marker ✓
- Notifier routed forge-question → Beacon's inbox ✓
- Beacon answered (15s, $0.11) ✓
- Notifier routed beacon-clarification → Forge's inbox ✓
- **Forge's watcher refused with `worktree_enabled but no canonical path for target_repo=None`** ✗
- Notifier dead-lettered the failure back to Beacon

**Root cause:** clarification cascade dropped `target_repo` at *both* notify hops. Same shape as the marker-error black hole (different code path):
- `outbox_notifier.process_outbox` marker-decision path wrote notify-to-Beacon without `target_repo`.
- Beacon's inbox task had no `target_repo`. `_build_outbox` (now propagating the field) had nothing to propagate.
- Default routing path wrote notify-to-Forge without `target_repo`. Forge's watcher gate refused.

**Fix (`b805578`, follow-up commit):** propagate `target_repo` + `branch` + `pr_title` + `pr_body` into the notify_task in both `process_outbox` paths (marker-decision + default). Two new tests (`test_clarify_notify_propagates_target_repo_and_branch`, `test_clarification_answer_leg_propagates_target_repo_and_branch`) cover both legs. 235 tests pass (was 233 after 4b).

**Live test 2 v2 — `pulse-cost-note-003`** (success; $1.21, **PR #2** opened):

After the follow-up fix, re-ran with a new task_id. This time Forge picked PROCEED directly (concluded Section 10 of Part I was the clear single target despite the candidate-list framing). So the round-trip-via-CLARIFY didn't fire, but the 4b NEW machinery did, end-to-end:

- Preflight (`950c0d77...`) emitted PROCEED marker ✓
- Notifier marker-decision: ack-proceed notify to Beacon ✓
- Notifier `_dispatch_build_phase`: build-phase task to Forge's inbox with `phase=build`, `session_id=950c0d77...` ✓
- Watcher picked up build task, `worktree_manager` **reused the existing worktree** (`reusing worktree /tmp/wt-forge-pulse-cost-note-003`) ✓ — the keyed-reuse pattern WORKS in live traffic
- Forge resumed under `--resume=950c0d77...`, ran 40s ✓ — `--resume` plumbing WORKS in live traffic
- Forge committed, pushed, ran `gh pr create`, returned `PR opened: https://github.com/Larry-Yatch/ourliberty-agent-core/pull/2` ✓
- Final PR diff: 2 insertions, 2 deletions in `docs/operating-manual.md` Section 10 (cost line + monthly total).

The clarification round-trip target_repo propagation is now covered by the new unit tests; live coverage of that specific path is still pending (Forge keeps deciding PROCEED on doc-only specs). A future spec genuinely needing clarification will exercise it.

**Discovered architectural concern — systemd `PrivateTmp` namespace.** Investigating why `/tmp/wt-forge-watchdog-doc-fix-001/` wasn't visible from host shell after test 1 surfaced this: `ourliberty-inbox-watcher.service` and `ourliberty-outbox-notifier.service` have `PrivateTmp=yes` (existing security hardening). Each service gets its own `/tmp` namespace via `systemd-private-<id>-<service>-<rand>`. Implications:

- The watcher creates `/tmp/wt-*` in its PRIVATE /tmp. Visible to its subprocesses (good — Forge can use the worktree). NOT visible to the cleanup-service (also has its own private /tmp) or to host shell.
- `cleanup_stale_worktrees.service` was installed with default `PrivateTmp=no` so it sees host /tmp — which never has the worktrees. **Today's cleanup script can't actually reach the worktrees it's supposed to clean.**
- On watcher *restart*, the old private /tmp gets destroyed by systemd. Any in-flight worktrees are *gone*. Cross-restart task continuity (a load-bearing assumption of `task_id`-keyed reuse) is broken in production.
- Private /tmp is tmpfs (memory-backed). Worktrees accumulate in RAM over the service lifetime. After hundreds of dispatches, multi-GB of RAM goes to stale worktrees.

**This is a real follow-up that should ship before scaling Forge dispatches.** The architectural decision "worktree path = `/tmp/wt-<agent>-<task_id>/`" was signed off pre-implementation; the `PrivateTmp` namespace effect wasn't surfaced in design review. Fix options:

- (A) Move worktree base from `/tmp` to `~/agent-worktrees/` (persistent, visible across services, no namespace isolation). Requires WORKTREE_BASE change in `worktree_manager.py` + `CANONICAL_REPOS` change in `cleanup_stale_worktrees.py` + Forge CLAUDE.md path mentions.
- (B) Disable `PrivateTmp` on the inbox-watcher and outbox-notifier services. Looser sandbox; the existing 4-agent OS already has wide filesystem access via `ReadWritePaths`, so the marginal hardening from PrivateTmp is small.
- (C) Configure `BindPaths=/tmp/wt-*` or similar so the worktree subtree is shared across the services + cleanup service. Finickier systemd config; bind paths are static (no wildcards) so this likely means a single dedicated dir like `/var/lib/ourliberty-worktrees/` shared via BindPaths.

Recommend (A) — cleanest, most explicit, and reading "worktrees live with the agents" matches the operating model. Defer to Larry's call (architectural).

**Codified conventions worth recalling:**

1. **Worktrees are keyed by task_id, not per-dispatch-fresh.** Multi-dispatch tasks (preflight → CLARIFY → answer → re-preflight → build) hit the same worktree under `--resume`. The mtime-touch on reuse + the in-flight registry are both needed to keep cleanup from racing the work.
2. **Envelope fields must propagate through every hop in a cascade.** target_repo, branch, pr_title, pr_body, clarification budget, marker_error_count, original_source — anything the downstream needs survives by being explicitly listed in `_build_outbox`'s propagation set AND in `notify_task` construction in `outbox_notifier`. The 4b review caught two of these; test 2 caught a third. Default is "drop", which means every new envelope field is a new opportunity for a black-hole bug. A unit test per propagation hop is cheap insurance.
3. **`session_id` propagation is dangerous.** A claude session belongs to ONE agent. Notify tasks carry `claude_session_id` for telemetry, but blindly threading it through to `agent_runner.run_claude` would resume the WRONG agent's conversation. Gate consumption on a phase-marker like `phase=build` and document the gate explicitly.
4. **Forge's preflight discipline is prompt-shaped, not code-shaped.** Strong "REQUIRED STEPS"-style imperatives in the task prompt can override the phase=preflight envelope flag. If we want hard discipline, either (a) tighten Beacon's spec-emission template so preflight prompts say "DECIDE, do not act", or (b) add a runtime check that Forge's preflight output ends with a marker (and dead-letter back if not). Today we have neither; relying on CLAUDE.md guidance + spec discipline.
5. **systemd PrivateTmp interacts with shared-state via /tmp.** When designing infrastructure that uses /tmp as a coordination point across services, audit the unit files for `PrivateTmp=yes` first. The 4a/4b reviewers didn't catch this; only the live smoke surfaced it.

**Open issues entering commit 5:**

- **systemd PrivateTmp / worktree location** — see "Discovered architectural concern" above. Architectural call needed before commit 5's live test.
- **Forge preflight discipline** — test 1's fast-path bypassed the marker pipeline; live coverage of preflight→build separation is via test 2 v2 only. Consider tightening Beacon's prompt template or adding a runtime marker-required check.
- **PR #1 + PR #2 are open against `ourliberty-agent-core` `main`.** Larry to review + merge (or close). They're the live-smoke output, both substantively correct, but no Mirror review yet (D3.5 hasn't landed). The watchdog PR is "doc-only" and aligned with the spec; the Pulse-cost PR has small arithmetic in the body that's worth eyeballing.
- **CLARIFY round-trip not exercised live.** Unit tests cover the propagation fix; live coverage waits for a spec that Forge actually finds ambiguous.
- **`DeprecationWarning` in `cleanup_stale_worktrees.py`** — uses `datetime.utcnow()` (deprecated in 3.12+). Inherited from upstream; trivial fix to `datetime.now(timezone.utc)`. Not blocking.
- **Commit 5 (D3-sentinel)** still ahead — `dispatch_sentinel.py` systemd timer + enabling the `ourliberty-watchdog.timer` as the same commit's live test. The watchdog unit isn't even installed yet (`is-enabled` returns `not-found`); commit 5 installs and enables it.

**Next:** Commit 5 (D3-sentinel) — install `dispatch_sentinel.py` as a systemd timer (every 10 min), install `ourliberty-watchdog.timer` so the system has a stall-detection layer + an inbox-age watchdog. The live test is the watchdog itself: drop a synthetic task into an inbox, kill the watcher, verify the watchdog catches the stall after the configured age. Plus the systemd PrivateTmp follow-up (depending on Larry's call).

## Phase D3 (4b followup-2) — worktree base relocated off /tmp; preflight-discipline note queued for D3.5 (2026-05-12, ~30 min)

Larry signed off on Option A from the 4b Part II entry's "Discovered architectural concern" section: move the Forge worktree base from `/tmp` to a persistent home-directory location so the `PrivateTmp=yes` namespace isolation on the watcher and notifier services stops sabotaging the cleanup timer and cross-restart task continuity.

### What shipped

- **`scripts/worktree_manager.py`** — `WORKTREE_BASE = Path.home() / 'agent-worktrees'`. Auto-mkdir on import. Header docstring rewritten to explain the PrivateTmp rationale so future readers don't try to "simplify" it back to `/tmp`.
- **`scripts/cleanup_stale_worktrees.py`** — `MANAGED_WORKTREE_PREFIX = '/home/larry/agent-worktrees/wt-'` (replaces the `/tmp/wt-` substring filter). Bundled the `datetime.utcnow()` → `datetime.now(timezone.utc)` deprecation fix since we were touching the file.
- **`systemd/ourliberty-inbox-watcher.service`** and **`ourliberty-outbox-notifier.service`** — `ReadWritePaths` extended to include `/home/larry/agent-worktrees`. Without this, `ProtectHome=read-only` would block worktree writes.
- **`agents/forge/CLAUDE.md`** — single path mention updated from `/tmp/wt-forge-<task_id>/` to `~/agent-worktrees/wt-forge-<task_id>/`. The Build phase protocol text otherwise unchanged.
- **`agents/beacon/CLAUDE.md`** — **preflight-prompt discipline** section added under "How you dispatch work to Forge." Captures the 4b test 1 fast-path observation as Beacon's responsibility: preflight prompts must read as *spec-to-evaluate*, not *plan-to-execute*. Specifically: lead with GOAL + CONTEXT, declarative EXACT LOCATIONS, avoid imperative verbs (do/execute/run/edit/commit/push), end with the one-line preflight reminder, and pre-fetch any droplet state Beacon could capture herself rather than asking Forge to probe in preflight. D3.5 is expected to add a runtime check (notifier rejects preflight outboxes that don't end with a marker); until then, this is prompt discipline.

### Verification

- Local + droplet test suites pass (235 tests). Worktree-manager tests already monkey-patch `WORKTREE_BASE` to a temp dir, so the location change is invisible to them.
- `systemd-analyze verify` clean on both modified service units.
- One dispatched test task (`worktree-relocation-smoke-001`, a no-op preflight) confirmed the new path: worktree created at `/home/larry/agent-worktrees/wt-forge-worktree-relocation-smoke-001/`, visible from host shell, visible from a separate `cleanup_stale_worktrees.py` run, branch checkpoint pushed cleanly.

### Operational implications captured in Part I

- Section 1 "Infrastructure (D1–D3)" inventory updated: new entries for `outbox_notifier.py`, `safe_write_inbox.py`, `routing_validator.py`, `forge_preflight_handler.py`, `worktree_manager.py`, `cleanup_stale_worktrees.py`, the 7 D2.5 healers, the Forge worktree directory itself, and the 4b additions to `agent-models.json` (`worktree_enabled`, `allowed_repos`).
- Section 4 "The cast" systemd-units table updated: added `ourliberty-outbox-notifier.service` (was missing), `ourliberty-heal-*.timer` (×7), `ourliberty-cleanup-stale-worktrees.timer`. Inline note on `PrivateTmp=yes` semantics so future operators know the worktrees aren't in `/tmp`.
- Forge's "Status" line in Section 1 updated: was "Live (Build Loop not yet auto-triggered)" — now "Live (auto preflight → build → PR via inbox dispatch as of D3 commit 4b, 2026-05-12)".

### What's still ahead

- **Open PRs review process** — PR #1 (watchdog-doc-fix) and PR #2 (pulse-cost-note) are still open against `ourliberty-agent-core` main. Need a workflow for surfacing open Forge PRs to Larry so they don't pile up. Three candidates surfaced (immediate Telegram notify on PR open; daily Pulse digest; hybrid). Awaiting Larry's pick before implementing.
- **Preflight discipline runtime check** — currently prompt-based (Beacon's CLAUDE.md note). D3.5 should add a runtime gate in the outbox notifier: if a `phase=preflight` outbox doesn't contain a forge marker, dead-letter back to Forge with "preflight must end with a marker block" instead of routing through default-path as if it were a normal result. Documented in Beacon's CLAUDE.md so the constraint is visible to her now.
- **Multi-repo expansion** — `CANONICAL_REPO_PATHS` (watcher) + `CANONICAL_REPOS` (cleanup) are still single-entry maps. When `allowed_repos` grows beyond `ourliberty-agent-core`, fold both into a top-level `repo_paths` block in `agent-models.json` so the logical-name → filesystem mapping lives in one place.

**Next:** Commit 5 (D3-sentinel) — `dispatch_sentinel.py` + `ourliberty-watchdog.timer` install + watchdog live test. The PR review workflow likely lands as a separate small commit between 4b's tail and commit 5 (it touches Beacon's CLAUDE.md and possibly Pulse's `/cycle` prompt; not on the commit-5 critical path but worth shipping before the next live test).

## Phase D3 (commit 5 of 5) — dispatch_sentinel timer live; watchdog adapter deferred (2026-05-12, ~30 min)

Closes the D3 commit list with the narrow stall-detector running on a 10-minute timer. The broader `watchdog.py` (8-check health monitor, D2.5 era) turned out to still carry GM-orchestrator hard-coding (`RESTARTABLE_SERVICES = ['ourliberty-orchestrator', 'ourliberty-telegram-webhook']` — neither exists in our topology), so per the B-option Larry signed off mid-session, the watchdog adaptation is deferred to its own commit. Commit 5 ships just the sentinel.

### What shipped

- **`systemd/ourliberty-dispatch-sentinel.{service,timer}`** (new) — installed at `/etc/systemd/system/`, enabled. `Type=oneshot`, `User=larry`, `OnBootSec=5min`, `OnUnitActiveSec=10min`, `Persistent=true`. Mirrors the `ourliberty-cleanup-stale-worktrees` shape.
- Operating manual Part I:
  - Section 1 Infrastructure inventory: new entry for `dispatch_sentinel.py` (3 scans, disk-only alerts, no auto-cancel — the cancel-marker pattern remains the explicit kill switch).
  - Section 4 "The cast" systemd-units table: new row for `ourliberty-dispatch-sentinel.timer`.
  - Section 4: the `ourliberty-watchdog.timer` annotation updated to reflect the actual deferral reason ("`watchdog.py` still has GM-era service-name hard-coding — pending adapter rewrite, separate from D3 commit 5"), superseding PR #1's earlier annotation update (which only noted the D2.5 criterion was met).

### What's NOT in commit 5 (and why)

- **`watchdog.py` adaptation + `ourliberty-watchdog.timer` enable.** Audit during commit 5 prep found that `scripts/watchdog.py` (610 lines, D2.5) still references `ourliberty-orchestrator`, `ourliberty-telegram-webhook`, `ourliberty-github-webhook` — none of which exist in our fork. Two of its 8 checks (`check_orchestrator`, `check_orchestrator_memory`) are GM-orchestrator-specific. Installing it as-is would alert every 5 minutes about missing services and try to restart things that don't exist. Three options surfaced (full adapter rewrite, defer, hybrid minimal-edit); Larry signed off on **defer** per the "build complete, not fast" principle. Scope decision: don't ship a half-adapted watchdog as a "live test surface" for the sentinel. Sentinel's own first-fire-on-enable is the live test.

### Verification

- `systemd-analyze verify` clean on both new units.
- `enable --now` triggered immediate first run (`Persistent=true`): `[sentinel] [INFO] sweep complete — 0 known stalls, 0 new`. Exit 0. Next fire scheduled 10 min later.
- No stuck tasks, no stale leases, no in-flight orphans — the system was quiescent at install time so the sweep had nothing to flag. This is the same kind of "clean first-fire" verification that worked for the cleanup-stale-worktrees timer.

### Open issues entering D3.5

- **`watchdog.py` adapter rewrite** — deferred from commit 5 by explicit decision. Will likely roll into D3.5's verification setup (the broader review chain needs a healthy infra baseline; rewriting watchdog as part of the D3.5 prep is a natural pairing).
- **The original 4b-tail concerns** (PR review workflow, preflight-discipline runtime gate, multi-repo expansion) — all rolled into D3.5's scope per `docs/d3-5-plan.md`.

**Next:** D3.5 — Mirror review chain. Plan doc shipped at `docs/d3-5-plan.md` (206 lines). Likely splits into 5a (review markers + preflight gate), 5b (revision loop), 5c (escalation), 5d (auto-merge + EMERGENCY_HALT) per the same multi-commit pattern as D3 commit 4 → 4a + 4b.

## Phase D3.5-prep — watchdog adapter + sentinel-DM wiring + beacon-bot alert sweep (2026-05-13, ~2 hours, $0)

Closes the last D3-era item — adapts the dormant `scripts/watchdog.py` (610-line upstream import, never adapted for our topology) to actually monitor our 6 services, wires `dispatch_sentinel` alerts to Larry's phone via a shared queue, and gives Beacon's bot a periodic alert-poll sweep so infra alerts land as Telegram DMs without claude in the loop.

The D3.5 plan doc reserved this as pre-5a work for one reason: D3.5's longer live tests need a safety net under them. Watchdog is that net. Without it, a single daemon crash mid-D3.5-test silently stalls everything until manual intervention.

### Architectural calls signed off before code (per the decision-classification feedback memory)

- **Q1 (VALUES, 1-5 dial) Auto-recovery scope: Dial 3.** Watchdog auto-restarts the 2 plumbing daemons (`inbox-watcher`, `outbox-notifier`) AND, per a design-review C1 carve-out, `beacon-bot` — because beacon-bot is the alert delivery channel; alerts about it being down can't reach Larry's phone if its consumer is also down. The other 3 bots (`forge-bot`, `mirror-bot`, `pulse-bot`) are alert-only with a `sudo systemctl restart ourliberty-<x>-bot` suggested-action in the DM. Their systemd `Restart=always` handles crashes; watchdog surfaces sustained outages where `StartLimitBurst` was exhausted.
- **Q2 (TECHNICAL) Memory threshold for inbox-watcher V2 RSS check: 1.5 GB.** MemoryMax=2G with 500 MB headroom for spawned claude subprocesses.
- **Q3 (TECHNICAL) Disk/system memory thresholds: 90% critical, 80% warning.** Upstream defaults.
- **Q4 (ARCHITECTURAL) Stale-task check: dropped from watchdog.** Sentinel owns task-flow stall detection (3 scans). Watchdog owns infra (processes, services, memory, disk). Single source of truth. The follow-up: sentinel-DM wiring (its pre-existing deferred TODO) closes the loop so Larry's phone actually receives stall alerts — sentinel previously only wrote to disk.
- **Q5 (TECHNICAL) Token-manager check: dropped.** Our token_manager is an inline stub in `agent_runner.py`, not a module — upstream's check would always pass uninterestingly. TODO comment left for Phase F+ when an agent-only Max OAuth pool gets wired.
- **Q6 (ARCHITECTURAL) cgroup-aggregate memory check: ADD.** The V2 RSS check measures only the watcher's MainPID. Real overrun shape is the watcher + spawned claude subprocesses together exceeding `MemoryMax=2G`. New `check_inbox_watcher_cgroup` reports both parent RSS and children-MB breakdown in the alert payload so the human reading the DM can tell which side is bloating.
- **Q7 (ARCHITECTURAL) Alert dispatch: shared queue + per-line ack delivery via Beacon's bot.** Watchdog and sentinel both `append_alert(...)` to `~/agents/blackboard/larry-alerts.jsonl`. Beacon's bot polls the queue on its existing reminder cadence (~5 min via REMINDER_INTERVAL_SEC=300) and DMs each new line with severity emoji + subject + message + suggested_action. Pulse-as-responder + twice-daily digest deferred to Commit 2 (separate values walkthrough on Pulse's autonomy).
- **Q8 (VALUES, 1-5 dial) Verification depth: Dial 3.** Full alert path test + outbox-notifier auto-restart + inbox-watcher auto-restart + (added by C1) beacon-bot auto-restart + mirror-bot alert-only path + sentinel DM test.

### Design-review issues caught before code (independent reviewer pass, $0)

Same pattern as 4a/4b — fresh-eyes review before any code:
1. **C1 CRITICAL — beacon-bot can't alert about itself.** Q1-Dial-3 said "bots alert-only" but beacon-bot is the alert channel; alerts pointing at itself wedge undelivered. **Fix:** carve-out — beacon-bot joins the auto-restart set.
2. **C2 CRITICAL — sentinel state corruption causes alert flood.** `load_state()` returned `{'alerted': {}}` on JSONDecodeError → every pre-existing stall would re-fire as new on next sweep. **Fix:** `load_state()` returns `(state, cold_start)` tuple; cold-start sweep records to disk but suppresses larry-alerts append, re-arming dedup silently.
3. **M1 MAJOR — watchdog `systemctl start` races systemd's own `Restart=on-failure`.** `auto-restart` SubState wasn't in the alive allow-list; calling `start` during systemd's pending restart would burn StartLimitBurst. **Fix:** `is_service_alive` treats `auto-restart` SubState as alive; explicit `StartLimitInterval=300/StartLimitBurst=10` added to both daemon units.
4. **M2 MAJOR — line-count offset advance loses or duplicates alerts on crash.** **Fix:** per-line ack — `_send_alert_dm` returns bool from HTTP 200+ok=True only; offset advances only after every authorized chat got a success. At-least-once delivery (duplicates beat silence).
5. **M3 MAJOR — cooldown key wasn't subject-specific.** If mirror-bot went down at 11pm and forge-bot at 11:30pm, the `bots` cooldown key suppressed the second alert. **Fix:** subject-specific keys (`bots:mirror`, `bots:forge`). Plus separate critical (10 min) and warning (60 min) cooldown windows (Larry's Dial 3 pick on the warning duration).
6. **M4 MAJOR — MemoryMax hardcoded 2G in cgroup check.** **Fix:** `_read_memory_max()` queries `systemctl show -p MemoryMax`; handles `infinity` by returning None and skipping the check.

### What shipped

- **`scripts/larry_alerts.py`** (new, 175 lines) — shared append-only queue + cooldown gating + offset helpers. Per-subject cooldown buckets (10 min critical, 60 min warning). Atomic-write offset via tmp+rename. Malformed lines surface as `{'_malformed': True, 'raw': ...}` so the bot can skip past them. Stdlib only.
- **`scripts/watchdog.py`** (rewrite, 610 → 558 lines) — 9 checks: inbox_watcher / outbox_notifier / beacon_bot (auto-restartable trio) + inbox_watcher_memory (V2 RSS, 1.5 GB threshold) + inbox_watcher_cgroup (NEW, 80%/95% of MemoryMax) + disk + system memory + log_growth (inbox_watcher.log) + bots (forge/mirror/pulse alert-only with subject-specific cooldown keys). `sudo -n systemctl start/restart` for write operations (watchdog runs as larry; NOPASSWD sudo enables silent escalation).
- **`scripts/dispatch_sentinel.py`** (+~50 lines) — `load_state()` now returns `(state, cold_start)` tuple; main loop calls `larry_alerts.append_alert(...)` for each new stall **unless** cold_start (C2 fix). New `_stall_dm_message()` renders human-readable summaries per stall kind.
- **`scripts/beacon_telegram_bot.py`** (+~64 lines) — new `_check_pending_alerts()` sweep called alongside `_check_due_reminders()` from main loop on the same 5-min cadence. `_send_alert_dm()` returns True only on full HTTP 200+ok=True success across all chunks. Per-line ack: offset advances only after delivery confirmed; on failure, break loop and retry on next sweep.
- **`systemd/ourliberty-inbox-watcher.service`** + **`ourliberty-outbox-notifier.service`** — explicit `StartLimitInterval=300/StartLimitBurst=10` documented so watchdog and operators know systemd's own retry envelope.
- **`systemd/ourliberty-watchdog.service`** — `NoNewPrivileges=true` REMOVED. Watchdog needs `sudo -n systemctl start/restart` to recover the daemons; NoNewPrivileges blocks setuid at the kernel level regardless of sudoers config. Remaining hardening (`PrivateTmp`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ProtectKernelTunables/Modules/ControlGroups`, `RestrictSUIDSGID`) preserved.
- **`scripts/tests/test_larry_alerts.py`** (new, ~210 lines) — cooldown gating, subject independence, severity-bucket independence, expiry, malformed-line handling, offset atomicity, format_dm rendering.
- **`scripts/tests/test_watchdog.py`** (new, ~440 lines) — every check's success + failure paths, M1 SubState=auto-restart treated as alive, M3 subject-specific keys, M4 MemoryMax-from-systemctl + parent/children breakdown, RunAllChecks integration. Uses path-scoped `mock.patch('builtins.open', side_effect=...)` so the cgroup-file mock doesn't swallow `larry_alerts`'s writes.
- **`scripts/tests/test_dispatch_sentinel.py`** (+~125 lines) — new `SentinelLarryAlertsTest` class covers C2 cold-start suppression, second-sweep new-stall append, dedup of already-alerted stalls. Existing tests extended to also redirect `larry_alerts.*` paths to the test root so they don't leak into real `~/agents/blackboard/larry-alerts.jsonl`.

### Verification (Q8 Dial 3, on droplet, live)

299/299 unit tests pass on droplet Python 3.12 (235 pre-existing + 64 new across D3.5-prep). $0 verification cost (no claude invocations).

Six live tests (all completed within ~35 min wall clock):

1. **Smoke alert end-to-end** — manually `append_alert(...)` then waited for beacon-bot's 5-min sweep. Larry's phone received `⚠ watchdog [d35-prep-smoke]` at ~00:10:30 (first sweep after bot restart).
2. **outbox-notifier auto-restart** — `sudo systemctl stop ourliberty-outbox-notifier` at 00:11:09. Watchdog timer at 00:15:40 detected DOWN, attempted start. *(First-fire path: `systemctl start` failed because the watchdog service had `NoNewPrivileges=true` blocking sudo at kernel level. Critical alert "auto-restart failed" appended to queue. ISSUE FOUND AND FIXED LIVE — see Issues Caught below.)* Post-fix manual trigger at 00:19:30 succeeded; "auto-restarted" warning appended. Both DMs delivered at 00:20:21 bot sweep.
3. **inbox-watcher auto-restart (load-bearing)** — `sudo systemctl stop ourliberty-inbox-watcher` at 00:21:08. Watchdog timer at 00:26:04 detected DOWN, sudo-start succeeded (post-NoNewPrivileges fix verified via timer-driven path). "Auto-restarted" warning appended. Larry's phone received the critical-then-warning DM pair at 00:30:21 sweep.
4. **beacon-bot C1 carve-out** — `sudo systemctl stop ourliberty-beacon-bot` at 00:31:21. Watchdog timer at 00:32:08 detected DOWN, sudo-start succeeded. Bot started at 00:32:08, did its first sweep BEFORE watchdog appended the recovery alert (queue race, expected — bot picks up its own recovery DM on its NEXT 5-min sweep). Recovery DM delivered at 00:37:08.
5. **mirror-bot alert-only path** — `sudo systemctl stop ourliberty-mirror-bot` at 00:33:04. Watchdog timer at 00:37:09 detected DOWN; logged `alert-only (systemd Restart=always handles crash recovery)`; appended critical alert with `subject: bots:mirror` and `suggested_action: sudo systemctl restart ourliberty-mirror-bot`. NO auto-restart attempted. DM delivered at 00:37:09 sweep. Larry ran the suggested command manually to restore (verified the workflow shape).
6. **sentinel DM test** — planted a 4h-old fake task at `~/agents/inboxes/beacon/d35-sentinel-smoke.json`. Triggered sentinel manually (skipping the 10-min timer wait). Sentinel detected stall, appended warning alert via `larry_alerts.append_alert(source='sentinel', ...)`. DM in queue for next sweep delivery. Inbox-watcher also quarantined the planted file to `.invalid/` (independent verification of D2.5's routing-validator path).

System returned to `overall=healthy`. All 6 services active. Inboxes empty. No in-flight tasks. Quiescent.

### Issues caught during live verification (would have shipped broken without it)

Two real bugs surfaced only during droplet execution:

1. **`sudo` required, not just plain `systemctl`.** Watchdog runs as `User=larry` per its service unit. `systemctl start/restart` on system units requires root. Local tests used mocked subprocess.run so the privilege requirement was invisible. **Fix:** prepend `sudo -n` (non-interactive; relies on Larry's NOPASSWD sudoers config) to all systemctl write operations. Test assertions updated to expect the `sudo -n systemctl ...` prefix.
2. **`NoNewPrivileges=true` blocks sudo at kernel level.** Even with NOPASSWD configured, the systemd hardening flag blocks the setuid path sudo needs regardless of sudoers. Surfaced because the post-sudo-fix MANUAL run (from shell, no NNP) worked, but the timer-driven run (under the service unit's NNP) still failed. **Fix:** remove `NoNewPrivileges=true` from `ourliberty-watchdog.service` with a comment block explaining why; preserve all other hardening flags.

Both fixes verified live before declaring the test sequence complete.

### Codified conventions worth recalling next session

1. **Watchdog needs privilege escalation; healers and observers do not.** The other 9 timer-driven scripts (7 healers, sentinel, cleanup-worktrees) only read or write within `~/agents`. Only watchdog calls `systemctl start/restart` on system units. If a future service needs the same shape, the same `sudo -n` + no-NoNewPrivileges pattern applies.
2. **Per-line ack is the right offset-advance shape for queue-based delivery.** Advance only after delivery confirmed; tolerate at-least-once duplicates. The alternative (advance-then-deliver) loses messages on crash.
3. **Subject-specific cooldown keys, not check-specific.** When multiple instances of the same class can fail (multiple bots, multiple disks if we ever had them), the cooldown key needs the specific subject embedded — otherwise the first failure suppresses alerts about the second.
4. **Independent code review catches integration bugs; live verification catches environment bugs.** This commit shipped 6 caught at review + 2 caught at live test. Both passes are load-bearing; the live verification cost $0 and ~35 min wall clock.
5. **Beacon-bot's reminder + alert sweep cadence is 5 min.** Worst-case alert-to-DM latency is ~5 min watchdog detection + ~5 min bot sweep = ~10 min. Acceptable for infra alerts; documented.

### Open items entering D3.5 commit 5a

- **Commit 2 of the original "Option B" split** — Pulse-as-responder behavior change + twice-daily digest. Separate values walkthrough on Pulse's autonomy (sudo-restart authority, escalation criteria, DM voice). Not blocking 5a but worth scheduling.
- **Beacon-bot self-recovery DM latency.** When watchdog restarts beacon-bot, the recovery alert is appended AFTER bot's first sweep (which started before the append). The alert delivers on the bot's NEXT sweep (~5 min later). Worst-case 10 min latency on beacon-bot recovery DMs specifically. Acceptable; deadman-style journal monitoring is a future enhancement.
- **Trim larry-alerts.jsonl history.** Append-only; will grow slowly (~200 bytes per alert × low frequency). Add logrotate or trim-by-age sweep in a future commit when growth becomes visible (probably 6+ months out).

**Next:** D3.5 commit 5a — Mirror review chain begins. Per `docs/d3-5-plan.md` Section "Sequencing": 5a ships Mirror's review marker pipeline + preflight-discipline runtime gate.

## Phase D3.5 commit 5a — Mirror review marker pipeline + preflight gate (2026-05-13, ~5 hours, [live cost TBD])

Lands the first sub-commit of D3.5. Mirror gains a strict marker grammar parallel to Forge's preflight markers — four block-delimited outcomes (REVIEW_PASS, REVIEW_REVISION, REVIEW_ESCALATE, REVIEW_EMERGENCY_HALT) — plus the outbox-notifier integration that auto-dispatches review-requests to her when Forge opens a PR. Also ships the long-deferred preflight-discipline runtime gate from D3 commit 4b's followup-2.

In 5a all four Mirror markers route to Beacon as informational notifies and stop there. Auto-merge on PASS (5d), Forge revision dispatch on REVISION (5b), Beacon replan flow on ESCALATE (5c), and EMERGENCY_HALT trip on the safety marker (5d) are all forward-compat-only — the marker grammar + classifier + routing exist so Mirror's outputs route deterministically, but the loop closures land in the named sub-commits.

### Architectural calls signed off before code (per the decision-classification feedback memory)

Surfaced to Larry before any code; classified per `feedback_decision_classification.md`:

- **(VALUES, Dial 3) Severity rubric:** four-tier `low / medium / high / critical`. PASS requires zero `medium+` findings AND `confidence: high`. Aligns with the d3-5-plan recommendation. Coexists with Mirror's existing `[must-fix]/[should-fix]/[nit]` comment-tag scheme via clean separation: comments are individual findings (chat-mode), markers are aggregate verdicts (dispatch-mode). Mapping documented in `agents/mirror/CLAUDE.md` § Severity rubric.
- **(TECHNICAL, decide-and-move) Required fields per marker:** REVIEW_PASS = `{task_id, pr_url, summary}`; REVIEW_REVISION = `{task_id, pr_url, findings, severity, confidence}` with `findings` as non-empty array of `{file, line_range, severity, description}` dicts; REVIEW_ESCALATE = `{task_id, pr_url, reason, severity, confidence}`; REVIEW_EMERGENCY_HALT = `{task_id, pr_url, reason, evidence}` (no severity/confidence — marker type encodes critical+safety, `evidence` quotes the artifact from the diff).
- **(TECHNICAL, decide-and-move) Review trigger envelope:** notifier writes `review-request` to Mirror's inbox when Forge's `phase=build` outbox carries `PR opened: <url>` in result. Envelope: `source=beacon`, `phase=review`, `pr_url`, `target_repo`, `branch`, `revision_count=0`, `max_revisions` from `loop_bounds`. Mirror's session is fresh (no `--resume`) because she reviews an artifact, not continues a conversation.
- **(VALUES) Trivial-PR carve-out (doc-only ≤10 lines auto-PASS without Mirror review):** **deferred** to post-5d. No carve-out in 5a — every PR gets reviewed. Auto-merge in 5d only fires on Mirror PASS regardless of size; carve-out only makes sense after Mirror has a track record showing zero false-passes on the trivial class.
- **(ARCHITECTURAL) Mirror's REVIEW_QUESTION marker (mid-review clarification leg):** **deferred** to 5b. 5a ships the 4 terminal outcomes only. If Mirror hits ambiguity, she falls back to ESCALATE. Once 5b has revision-loop infra, REVIEW_QUESTION becomes a natural addition (the `mirror-question` source is already in the vocab as forward-compat scaffolding).
- **(ARCHITECTURAL) Mirror's `worktree_enabled: true`:** confirmed. Lets her `gh pr checkout <N>` and run tests against the branch. Same worktree machinery as Forge; keyed by `task_id`. Creates a second worktree per task alongside Forge's; bumps disk + cleanup load slightly. Acceptable.
- **(VALUES) Preflight-discipline runtime gate STRICT (dead-letter), not warn:** confirmed. A `phase=preflight` outbox without a marker block raises `MalformedForgeMarker` → marker-error cascade → dead-letter back to Forge with sharper "decide, don't act" prompt. Soft warning would let Forge keep fast-pathing; strict costs one extra Forge invocation when she slips, cheap insurance.
- **(VALUES) Branch protection on `main`:** **deferred** to 5d. In 5a–5c there's no auto-merge so the question doesn't apply (Larry merges manually). At 5d-design time, decide whether branch protection adds a meaningful second gate over Mirror's marker. Logged as a 5d-design checkpoint; specifically, want at least 10 clean Mirror-reviewed manual merges in the 5a/5b/5c window before turning on auto-merge without branch protection.
- **(VALUES) Build-phase discipline gate (parallel to preflight — fail if Forge's build response doesn't start with "PR opened:"):** **deferred** to 5b. Forge's current build fallback is "default routing → Beacon journals the result" which is recoverable. The preflight gate exists because preflight has no natural fallback (no decision = no routing). Build phase has a natural fallback; adding a gate doubles the new code surface for marginal value. 5b can add it when revision-loop infra is being designed.

### Confidence-promote rule (load-bearing)

A REVIEW_REVISION with `confidence: low` is automatically routed as REVIEW_ESCALATE by the notifier (`intent=review-escalate`, `auto_promoted=True` flag in the routing decision for audit). Rationale: if Mirror is uncertain whether her finding is real, the auto-fix loop with Forge would burn $0.50+ on a false-positive revision. Kick to Beacon, who can decide whether to clarify the spec or push back. Mirror's CLAUDE.md tells her the rule so she sets confidence thoughtfully knowing low-confidence revisions will escalate.

### What shipped

- **`scripts/mirror_review_handler.py`** (new, 320 lines) — Pure-logic library mirroring `forge_preflight_handler.py` shape: 4 block-delimited regex (PASS / REVISION / ESCALATE / EMERGENCY_HALT) with strict closing delimiters; `parse_mirror_marker()` returns `(marker_type, payload, narrative)` with multi-marker rejection (homogeneous AND heterogeneous); `REQUIRED_FIELDS` table with severity/confidence vocab validation; empty-findings-on-REVISION rejection ("should have been REVIEW_PASS"); `should_auto_promote()` confidence-promote helper; `evaluate_revision_budget()` + `revisions_remaining()` for 5b forward-compat; `derive_intent()` + `derive_notify_source()` + `build_auto_promote_reason()` routing helpers.
- **`scripts/outbox_notifier.py`** (+~440 lines) — Five additions: (1) `_classify_mirror_marker(data)` parallel to `_classify_forge_marker` with envelope/marker task_id check + confidence-promote applied internally. (2) `_notify_mirror_marker_error(data, err_msg)` parallel to `_notify_forge_marker_error` with same MAX_MARKER_ERROR_RETRIES=3 cap, same envelope propagation. (3) `_extract_pr_url_from_build_result()` + `_PR_URL_RE` for parsing Forge's `PR opened: <url>` build-result preamble. (4) `_dispatch_mirror_review(data, pr_url)` parallel to `_dispatch_build_phase` with idempotency check, target_repo-required gate, max_revisions from handler default. (5) Preflight-discipline runtime gate inside `_classify_forge_marker`. Plus `process_outbox` integration: Forge build-phase + "PR opened:" trigger near top (additive — Beacon notify still fires); `if agent == 'mirror':` marker classification branch; auto-promote audit log; EMERGENCY_HALT WARN log. Updated `marker-error` template to be agent-agnostic. 5 new `INTENT_ACTION_BLOCKS` entries: `review-pass`, `review-revision`, `review-escalate`, `review-emergency-halt`, `replan-request`.
- **`scripts/dispatch_validator.py`** (+~15 lines) — `ALLOWED_INTENTS` expanded with the 5 new review-marker intents. `ALLOWED_PHASES` += `review`, `revision`.
- **`scripts/routing_validator.py`** (+3 lines) — `FRESH_DISPATCH_ROUTES['mirror']` extended to `{'beacon', 'forge'}` — forward-compat for 5b's revision dispatch. 5a doesn't dispatch on this route.
- **`config/agent-models.json`** — Mirror gains `worktree_enabled: true` + `allowed_repos: ["ourliberty-agent-core"]`. New top-level `loop_bounds` block: `max_revisions: 3`, `max_replans: 2`, `cost_per_task_usd: 5.0`. `_history` entry.
- **`agents/mirror/CLAUDE.md`** (+~150 lines) — Replaced "What you do — the Review Loop" with two-section structure: (1) Review protocol — every dispatched task (phase=review handling, 4 marker formats, marker discipline, severity rubric Dial 3 table, confidence rubric with auto-promote rule, what REVIEW_PASS requires, ESCALATE vs REVISION distinction). (2) Ad-hoc review loop preserves the legacy chat-mode `[must-fix]/[should-fix]/[nit]` comment loop for direct chat with Larry. Severity-vocab mapping between the two schemes documented.
- **`agents/beacon/CLAUDE.md`** (+~80 lines) — New section "How you handle Mirror's review markers (Phase D3.5 commit 5a)" after the Forge-markers section. Four new shapes (6: review-pass, 7: review-revision, 8: review-escalate with auto-promote handling, 9: review-emergency-halt). Each names manual-action in 5a + automation landing in 5b/5c/5d. Plus a "Sanity check before acting" closer.
- **`agents/forge/CLAUDE.md`** (+5 lines) — Preflight-discipline runtime gate note in the Marker discipline section, aligning prompt language with the strict runtime gate.
- **`docs/tunables.md`** (new, 140 lines) — Single source of truth for every numeric/boolean dial in the system, organized by subsystem. Each dial: location, current value, range, retune trigger. Periodic review cadence (quarterly walkthrough + monthly skim). Append-only change log. Eventual `tunables status` script flagged as Phase F+.
- **`scripts/tests/test_mirror_review_handler.py`** (new, 380 lines, 42 tests) — Mirrors `test_forge_preflight_handler.py` shape: `ParseMirrorMarkerTest`, `MultipleMarkerRejectionTest`, `AutoPromoteTest`, `EvaluateRevisionBudgetTest`, `RevisionsRemainingTest`, `DeriveIntentTest`, `DeriveNotifySourceTest`, `BuildAutoPromoteReasonTest`.
- **`scripts/tests/test_outbox_notifier.py`** (+~580 lines, 21 new tests across 4 classes) — `ClassifyMirrorMarkerTest` (9 cases), `MirrorMarkerRoutingTest` (7 cases including no-marker fallthrough), `PreflightDisciplineGateTest` (4 cases including build-phase-no-marker fallthrough verification), `MirrorReviewDispatchTest` (6 cases including idempotency on re-processing).
- **`scripts/tests/test_dispatch_validator.py`** (new, 130 lines, 16 tests) — Validator vocabulary smoke coverage. `CoreValidationTest`, `AllowedIntentsTest` (including new review-intents), `AllowedPhasesTest` (including new review/revision phases), `MirrorSourceVocabularyTest`, `ClarificationBudgetTest`.
- **`scripts/tests/test_routing_validator.py`** (+/- ~5 lines) — `mirror → forge` route flipped from disallowed to allowed.

**Total: +~2,250 / -~30 lines across 13 files. 79 new tests added (378 → 457 local suite). All green on local laptop modulo the 4 pre-existing macOS-only worktree_manager failures (unrelated; pass on droplet Linux).**

### Design-review issues caught before code (independent reviewer pass, $0)

[FILLED IN POST-REVIEW]

### Verification

[FILLED IN POST-LIVE-SMOKE]

**Next:** D3.5 commit 5b — Forge↔Mirror revision loop. Per `docs/d3-5-plan.md`: ships REVIEW_REVISION → `phase=revision` dispatch back to Forge under --resume, Forge's CLAUDE.md "Revision phase" section, `max_revisions` enforcement, REVIEW_QUESTION marker (deferred from 5a). Verification target: deliberately-flawed Forge build → Mirror REVISION → Forge fix → Mirror PASS. ~$1.50 live cost.

## Phase D3.5 5a-followup — Larry-DM-on-task-complete (2026-05-13, ~2 hours, $0.50 live re-test pending)

After Test 4's first real end-to-end dispatch (`tunables-first-review-date-001`, PR #3 merged) completed silently from Larry's perspective — Beacon's review-pass journal landed in her outbox but never reached his Telegram thread. Two distinct gaps surfaced:

**Bug A (pre-existing):** `beacon_approval_handler.dispatch_approved` builds the inbox task from the APPROVAL_REQUEST marker's payload but never copies the entry's stored `chat_id` into the task as `reply_chat_id`. So the original Forge inbox task landed with `reply_chat_id=None`, and every downstream hop's propagation block found `None` to propagate. This bug predated 5a — it's been silent because no flow ever closed back to an originating chat thread before.

**Gap B (5a scope):** Even with `reply_chat_id` propagating end-to-end, the outbox notifier wrote inter-agent notifies but had no mechanism to surface a closure DM to the originating Telegram chat. Beacon's claude invocation handling the PASS notify just journaled (correctly per her CLAUDE.md), but her response went to her outbox where the bot doesn't look.

### Architectural call signed off

**(VALUES, decided-and-moved) Notifier auto-DM vs Beacon-emits-marker.** Two options for closing the loop: (A) Beacon emits a `=== TELEGRAM_DM ===` marker in her response, bot intercepts and DMs Larry; (B) notifier auto-renders a per-intent DM template and queues it via the existing `larry-alerts.jsonl` pipe. Chose B — reliable (Beacon can't forget), matches the existing pattern where the notifier renders per-intent inter-agent action blocks, no new marker grammar. Trade-off: less flexibility for richer Beacon-authored DM content; acceptable for 5a, can extend with TELEGRAM_DM marker later if a real need surfaces.

### What shipped

- **`scripts/beacon_approval_handler.py`** (+3 lines) — `dispatch_approved` now copies `entry.get('chat_id')` into `task_dict['reply_chat_id']` when present. Bug A fix.
- **`scripts/larry_alerts.py`** (+~50 lines) — New `append_notification(source, intent, message, chat_id, task_id)`. No cooldown gating (notifications are 1:1 with task completions). Targeted delivery (record carries `chat_id`; bot reads it). Persists to the same `larry-alerts.jsonl` with `kind: "notification"` field distinguishing from alerts. `format_dm` extended with `_NOTIFICATION_INTENT_EMOJI` map (✓ pass, ⚠ revision/escalate, 🛑 emergency-halt, ✗ reject/clarification-exhausted, 📬 fallback). Regression-preserved alert rendering.
- **`scripts/outbox_notifier.py`** (+~140 lines) — New `DM_TEMPLATES` dict (6 per-intent templates), `TERMINAL_DM_INTENTS` frozenset, `_render_dm_message()`, `_maybe_dm_larry()` helper. Hook fires after marker-driven notify lands in `process_outbox`. Renders intent-specific message from `marker_decision['payload']` + `intent_kwargs`. Skips silently when reply_chat_id absent, intent non-terminal, or template unrenderable.
- **`scripts/beacon_telegram_bot.py`** (+~30 lines) — `_check_pending_alerts` extended to handle `kind: "notification"` records. Notifications target the specific `chat_id` field; alerts continue to broadcast to all authorized chats. Defense-in-depth check: unauthorized chat_ids get dropped (offset advances so we don't wedge).
- **`agents/beacon/CLAUDE.md`** (+~2 lines) — Note in Mirror-markers section: "Larry gets a closing Telegram DM automatically; you just journal."
- **Tests** (+24, total 425): 2 new in `DispatchApprovedTest` (chat_id propagation + omission); `AppendNotificationTest` (3 cases: basic, no-cooldown, no-task-id) + `FormatDmNotificationTest` (8 cases including legacy-alert regression); `MaybeDmLarryTest` (11 cases covering all 6 terminal intents fire DM, 3 non-terminal intents skip DM, missing chat_id skips, non-int chat_id skips, reject + clarification-exhausted render).

### Verification (pending live re-test)

Synthetic unit tests pass locally (24 new). Live verification: re-run Test 4 (Larry dispatches small spec → approval → Forge build → Mirror PASS → DM to Larry's Telegram). Expected ~$0.50 + DM appearing on Larry's phone within ~5 min of Mirror PASS (bot sweep cadence).

### Known cleanup gap (not in scope for this commit)

Test 4's first run + the synthetic test overflow created 5 stale branches on origin (`forge/marker-error-*` from cascade overflow, `forge/smoke-5a-prdispatch` from synthetic, `mirror/tunables-first-review-date-001` from Mirror's worktree checkpoint). `cleanup-stale-worktrees` timer prunes worktree DIRECTORIES but does not clean up associated remote branches. Need to either extend the cleanup timer to delete the branches or run `git push origin --delete <branch>` periodically. Tracked for a future commit.

**Next:** verify Test 4 re-run lands the DM, then D3.5 commit 5b (Forge↔Mirror revision loop) per the d3-5-plan sequencing.

## Phase D3.5 commit 5b — Forge↔Mirror revision loop (2026-05-13, ~3 hours, live verification TBD)

Status: Shipped 2026-05-13.

Closes the second sub-commit of D3.5. Mirror's REVIEW_REVISION marker now auto-dispatches a `phase=revision` task back to Forge under `--resume` against her build session, with findings serialized in the prompt. Forge applies the findings, commits + pushes to the same branch (PR auto-updates), emits a `Revision N applied:` preamble — the notifier detects it and auto-dispatches a fresh re-review to Mirror with `revision_count` incremented. Loop continues until Mirror emits PASS, or until `max_revisions` (currently 3 in `loop_bounds`) is exhausted — at which point Mirror's next REVIEW_REVISION downgrades to ESCALATE-shaped routing, Beacon journals, and Larry gets a Telegram DM via the 5a-followup auto-DM pipe.

In **5b** the full Forge↔Mirror chain is closed. Beacon's replan flow on ESCALATE (5c) and auto-merge on PASS (5d) remain forward-compat-only.

### Architectural calls signed off before code

Per `feedback_decision_classification.md`:

- **(ARCHITECTURAL) Session-id threading.** Forge's build `claude_session_id` propagates through Mirror's review-request envelope as `forge_build_session_id`; `_build_outbox` propagates forward; revision dispatch reads it as the resume target. *Alternative: per-task state file. Chose envelope-propagation — matches existing convention (clarification_count, marker_error_count, etc.).*
- **(ARCHITECTURAL) Forge revision response format.** Plain text with `Revision N applied: <summary>` preamble. *Alternative: structured `=== REVISION_APPLIED ===` marker. Chose plain text — matches build-phase pattern; the prefix is the trigger.*
- **(ARCHITECTURAL) Worktree continuity edge case.** Revision cycles > 24h could lose Forge's worktree (`cleanup-stale-worktrees` fires). Accept; document; worktree-manager recreates from origin if needed (only uncommitted edits lost). Logged in known-cleanup-gaps for future hardening.
- **(VALUES, A/B) Budget exhaust behavior.** Option A: downgrade to `intent=review-escalate` with budget-exhausted reason; reuses existing Beacon handler, no new vocabulary. *Signed off A.*
- **(VALUES, A/B) Push semantics on revision.** Option A: new commit on top of build, regular push, PR auto-updates. *Signed off A. Final merge `--squash` collapses build + revisions to one main-history commit.*
- **(VALUES, mixed) Discipline gate scope.** Build phase stays lenient (missing `PR opened:` prefix → default routing to Beacon, preserving blocker-paragraph path). Revision phase is strict (missing `Revision N applied:` prefix → marker-error cascade back to Forge). Asymmetric because build has documented blocker-narrative fallback while revision doesn't. *Signed off Option 3.*
- **(VALUES, 1-5 dial) `max_revisions` default.** Kept at 3 (in `loop_bounds`). Retune after live runs.
- **REVIEW_QUESTION marker:** deferred again from 5b per the existing memory `project_review_question_deferred.md`. Re-evaluate after ≥20 real reviews.

### What shipped

- **`scripts/inbox_watcher.py`** (+5 LOC) — Resume-session gate extended: `phase in ('build', 'revision')` consumes `session_id` for --resume. `_build_outbox` propagation list extended with `forge_build_session_id`, `revision_count`, `max_revisions`, `pr_url` (forward-compat for the cascade legs).
- **`scripts/mirror_review_handler.py`** (+~55 LOC) — `derive_intent` accepts `budget_exhausted` parameter (defaults False; True downgrades review_revision to review-escalate). New `build_budget_exhausted_reason()` helper renders the Beacon-facing escalate reason with finding context + budget figures.
- **`scripts/outbox_notifier.py`** (+~400 LOC) — `_extract_revision_summary_from_result()` parses Forge's `Revision N applied:` preamble (anchored to start, case-insensitive on keyword). `_dispatch_revision_to_forge(data, decision)` parallel to `_dispatch_build_phase`: reads `forge_build_session_id`, serializes Mirror's findings into structured prompt, writes `phase=revision` task keyed `revision-<task_id>-<N>.json` (idempotent across inbox + .archive + .invalid). `_dispatch_mirror_review_rerun(data, round_num, summary)` parallel to `_dispatch_mirror_review`: writes fresh review-request to Mirror with `revision_count` set + `forge_build_session_id` propagated forward (so next revision can resume Forge's session). `_classify_mirror_marker` extended: REVIEW_REVISION → evaluate `mrh.evaluate_revision_budget(data)`; budget_exhausted downgrades intent to review-escalate via the new `derive_intent` param. `process_outbox` integration: REVIEW_REVISION marker + budget-OK + high-confidence → auto-call `_dispatch_revision_to_forge`. New `agent == 'forge' and phase == 'revision'` branch with strict gate — missing preamble → marker-error cascade. `INTENT_ACTION_BLOCKS['review-revision']` rewritten for mid-chain semantics ("auto-dispatched to Forge; you just journal"). `review-revision` removed from `TERMINAL_DM_INTENTS` (mid-chain in 5b; only escalate variant DMs Larry). `_dispatch_mirror_review` (5a code) extended to propagate `forge_build_session_id` from `data.claude_session_id` into Mirror's review-request envelope.
- **`agents/forge/CLAUDE.md`** (+~80 lines) — New "Revision phase protocol" section parallel to Build phase protocol. Documents: same worktree + branch + PR; --resume Forge's build session; targeted edits per finding (no scope creep); conventional-commit revision message format; regular push (no force-push); strict `Revision N applied:` preamble required; missing → marker-error cascade with sharper "use the required preamble" prompt. Constraints listed explicitly.
- **`agents/mirror/CLAUDE.md`** (+5 lines) — Re-review context note in "Review steps" section: when `revision_count > 0`, approach diff fresh (prior session is closed); verify findings resolved + no new regressions introduced; bounded by `max_revisions` budget which will downgrade further REVISION to ESCALATE.
- **`agents/beacon/CLAUDE.md`** (+~15 lines) — Shape 7 (review-revision) rewritten: revision now auto-dispatched, journal only, no manual action. Shape 8 (review-escalate) expanded: 3 distinct trigger scenarios (direct REVIEW_ESCALATE, auto-promote from low-confidence REVISION, budget-exhaust downgrade). Larry gets DM in all three via terminal-intent pipe.
- **`scripts/tests/test_outbox_notifier.py`** (+~370 LOC, 18 new tests across 2 classes) — `RevisionLoopTest` (12 cases): revision dispatch on REVIEW_REVISION + budget-remaining + high-confidence; low-confidence does not dispatch; budget-exhausted does not dispatch; missing forge_session skips; idempotency on re-process; Forge revision outbox → Mirror re-review dispatch; missing preamble → marker-error dead-letter; re-review idempotency; round-2 extraction correctness; forge_build_session_id propagation through Mirror's review-request; build phase lenient (missing PR URL → default routing, NOT marker-error); budget-exhausted queues Larry DM via escalate intent. `ExtractRevisionSummaryTest` (6 cases): regex anchoring, case-insensitivity, missing returns None, buried-in-narrative returns None, multi-line summary captures first line only.
- **`scripts/tests/test_mirror_review_handler.py`** (+25 LOC, 4 new tests) — `derive_intent` budget_exhausted parameter (3 cases including both-flags case), `BuildBudgetExhaustedReasonTest` class (2 cases).
- **5a-followup test update** (-1 +1 LOC) — `test_review_revision_does_not_dm_in_5b` replaces the 5a-era test that expected REVIEW_REVISION to DM. Confirms the intent flipped from terminal to mid-chain.

### What's NOT in 5b

- **REVIEW_QUESTION marker.** Deferred again. See `project_review_question_deferred.md` memory for re-eval criteria.
- **Beacon replan flow on ESCALATE.** 5c will wire Beacon's auto-revise-spec + re-emit-APPROVAL_REQUEST when ESCALATE lands. 5b's ESCALATE still routes to Beacon for manual decision.
- **Auto-merge on REVIEW_PASS.** 5d.
- **EMERGENCY_HALT file trip.** 5d.
- **Branch protection on `main`.** Deferred to 5d-design checkpoint.

### Known limitations (logged for future hardening)

- **Worktree continuity > 24h.** If a Forge↔Mirror revision cycle exceeds 24h wall clock, `cleanup-stale-worktrees` may remove Forge's worktree directory mid-cycle. Worktree-manager recreates from origin on next dispatch (only uncommitted edits lost). Real-world: most revision cycles complete in minutes; the edge case is when a Mirror review takes hours (e.g., very large diff) and revisions stack up. Mitigation candidate for future commit: extend `cleanup-stale-worktrees` to skip worktrees with active dispatch leases.
- **claude session cache age.** Similar story for the session cache claude maintains for `--resume`. If 24h passes between Forge's build and her revision dispatch, the cache may be evicted and --resume could fail or produce a degraded continuation. Same mitigation shape.

### Verification (pending live re-test)

Synthetic unit tests pass locally (447 total, 4 pre-existing macOS-only worktree failures unrelated). 22 net new tests across the 5b additions.

Live verification target (~$1.50 cost): dispatch a spec Forge will get not-quite-right (so Mirror flags REVISION), observe Forge's revision dispatch + apply + push, observe Mirror's re-review + PASS, observe closing DM. Plus a targeted budget-exhaust test (dispatch a spec where Forge keeps not satisfying Mirror — synthetic marker injection if needed — to confirm ESCALATE downgrade fires after round 3).

**Next:** D3.5 commit 5c — Beacon replan flow. Wires Beacon's auto-revise-spec on ESCALATE intent (currently routes to manual decision). Smaller surface than 5b. After 5c → 5d (auto-merge + EMERGENCY_HALT). Then D3.5 closes.

## Phase D3.5 5b-followup — second-pass review fixes + live-test cascade fixes (2026-05-13, ~3 hours, $1.27 lost to the failed live test surfacing the bugs)

Status: Shipped 2026-05-13.

Two parallel review passes after 5b shipped: (1) a second-pass independent reviewer focused on multi-round data flow + prompt content (found 3 issues — M-7, M-8, m-9), (2) Larry's Path A live verification dispatch, which failed in a way that surfaced 3 more issues — Forge's marker-content discipline (Bug A), the 4a/4b task_id-wrapping cascade (Bug B), and the dead-letter Larry-DM gap (Bug C).

### The failed live test (2026-05-13 11:55–11:59 UTC −0600)

Larry dispatched a tiny doc-edit task (`opmanual-d35-5b-shipped-note-001`). Forge's preflight emitted:

```
[narrative bullets above]
=== PROCEED ===
Preflight passed. File and heading verified at docs/operating-manual.md:1536 via
the literal-substring grep. Plan for build phase: insert `Status: Shipped...`
=== END_PROCEED ===
```

**She wrote prose between the delimiters instead of the required `{"task_id":..., "preflight_summary":...}` JSON object.** The 5a preflight-discipline runtime gate (`parse_forge_marker` requires `\{...\}` between delimiters) returned None, raised `MalformedForgeMarker: none found`. The marker-error cascade then fired three retries, each rejected by the 4b task_id-mismatch check (because the cascade wraps envelope task_id as `marker-error-<orig>-<N>` but Forge correctly emits her marker with the original `<orig>`). Dead-letter to Beacon at retry 4. Total Opus cost: $1.27 (preflight + 3 retries + Beacon's dead-letter journal). Larry on his phone: "I approved, then nothing." No closing DM — `dead-letter` wasn't in `TERMINAL_DM_INTENTS`.

Three layered bugs caught:

- **Bug A (Forge discipline / parser diagnostic).** Forge sometimes emits prose between marker delimiters instead of strict JSON. The runtime gate's "none found" error didn't tell her that. Now: when delimiters are present but JSON is missing, the parser raises a diagnostic error explaining the JSON-only contract, AND her CLAUDE.md gets explicit wrong-vs-right example with the exact failure pattern.
- **Bug B (marker-error cascade brittleness).** The 4a/4b cascade wrapped envelope task_id (`marker-error-<orig>-<N>`) but Forge's marker contract required envelope-and-payload task_id match. Forge correctly used the ORIGINAL task_id (she could see the wrapper was metadata, not the real task); the mismatch check rejected every retry. Pre-existing bug since 4b; 5b's strict preflight gate made it visible by triggering more marker-error cascades.
- **Bug C (cascade exhaust silent to Larry).** `dead-letter` intent fired the inter-agent notify to Beacon's inbox but wasn't in `TERMINAL_DM_INTENTS`, so Larry's chat thread got no closing DM. Approval → silence.

### Second-pass reviewer findings (orthogonal to live test)

Same review pattern (4a/4b/5a/5a-followup → 5/6/6/6/6 issues each). This one caught 3:

- **M-7 MAJOR — `_notify_mirror_marker_error` drops `forge_build_session_id` and `phase`.** If Mirror's REVIEW_REVISION marker has bad JSON on the first try, her clean retry's outbox loses the field via `_build_outbox` propagation (only what's on the task gets propagated). Then `_dispatch_revision_to_forge` silently skips (no session to --resume). Same shape as the C-1 propagation gap, mirrored on Mirror's side.
- **M-8 MAJOR — Re-review prompt omits previous findings.** Mirror's CLAUDE.md told her to recover from "the PR's commit history or Beacon's journal" — but Forge's revision commit body explains what was fixed, not what the findings were, and "Beacon's journal" isn't a file Mirror has a documented tool to read. On round 2 she'd re-derive different findings, breaking loop coherence.
- **m-9 minor — `pr_url` default `'(unknown)'` poisons next dispatch.** Defensive fallback substituted a literal marker string into Mirror's re-review envelope; if she REVISIONs again, that string propagates to Forge's next revision prompt as `PR: (unknown)`. Now: missing pr_url skips dispatch with WARN (matches target_repo gate shape).

### What shipped

- **`scripts/forge_preflight_handler.py`** (+~30 LOC) — `_LOOSE_FORGE_DELIMITER_RE` for diagnostic error path; `parse_forge_marker` raises `MalformedForgeMarker` with sharp "delimiters found but no JSON inside, put narrative ABOVE the block / JSON INSIDE" message when the loose detector matches but strict doesn't.
- **`scripts/mirror_review_handler.py`** (+~30 LOC) — Symmetric `_LOOSE_MIRROR_DELIMITER_RE` + diagnostic raise in `parse_mirror_marker`.
- **`scripts/outbox_notifier.py`** (+~90 LOC across multiple sites):
  - Bug B: `_notify_forge_marker_error` and `_notify_mirror_marker_error` now keep envelope `task_id` as the ORIGINAL (filename + `marker_error_count` handle uniqueness). Forge's marker contract (task_id matches envelope) holds across retries.
  - Bug C: new `DM_TEMPLATES['dead-letter']`; `_dead_letter_marker_error_to_dispatcher` calls `_maybe_dm_larry` with a synthetic decision dict so the chat thread gets a closing notification.
  - M-7: `_notify_mirror_marker_error` propagates `forge_build_session_id` + `phase`.
  - M-8: `_dispatch_revision_to_forge` threads `previous_findings` into the revision envelope; `_dispatch_mirror_review_rerun` injects them into the re-review prompt.
  - m-9: `_dispatch_mirror_review_rerun` skips dispatch when `pr_url` is missing (matches `target_repo` gate shape).
- **`scripts/inbox_watcher.py`** (+1 LOC) — `_build_outbox` envelope_fields adds `previous_findings`.
- **`agents/forge/CLAUDE.md`** (+~22 lines) — Marker discipline section: explicit "JSON-only between delimiters" requirement with wrong-vs-right example using the exact failure pattern from the 2026-05-13 dispatch.
- **`agents/mirror/CLAUDE.md`** (+~17 lines) — Symmetric JSON-only marker discipline guidance.
- **Tests** (+~270 LOC, +13 new tests): loose-delimiter diagnostic tests in Forge + Mirror handler test files (4 cases); test class `RevisionFollowupFixesTest` gains tests for Bug B (3 cases — Forge initial retry, Forge retry 2, Mirror retry all keep original task_id), Bug C (3 cases — DM queued, no chat_id skips, template renders), plus the 5 second-pass tests for M-7/M-8/m-9 added earlier.

### Codified conventions worth recalling next session

1. **Marker payload is JSON-only; narrative goes above the block.** Universal — applies to Forge's preflight markers, Mirror's review markers, Beacon's APPROVAL_REQUEST. Forge's slip on 2026-05-13 was the first observed in real chain runs; the parser-level diagnostic + CLAUDE.md examples make this hard to repeat.
2. **Marker-error envelope task_id must equal the original task_id.** Filename + `marker_error_count` handle uniqueness; the envelope task_id is the agent's marker-contract anchor. Any cascade that wraps envelope task_id will fail the agent's marker validation.
3. **Every terminal-from-Larry's-perspective intent must DM Larry.** `dead-letter` joined the set; verify when adding any future intent (`review-question` in 5b's deferred scope, `pulse-digest` in Phase F+, etc.) — if Larry initiated the work, he gets a closing DM at every termination shape.
4. **Cascade brittleness is the failure mode to design against.** Forge's preflight slip cost $1.27 because three layers of bugs stacked: she made one prompt-discipline mistake; the marker-error retry mechanism couldn't recover because of B; the cascade exhaust didn't reach Larry because of C. Single-failure-mode design is dangerous; verify each layer's recovery path independently.

### Verification (live, 2026-05-13)

Synthetic unit tests pass locally (476 total, 4 pre-existing macOS-only worktree failures unrelated). 13 net new tests across the followup additions.

**Live test 1 (PR #5, `opmanual-d35-5b-shipped-note-001` — same task that originally failed):** chain ran successfully end-to-end. Forge slipped on JSON-vs-prose again on her first preflight attempt (`MalformedForgeMarker: phase=preflight requires ONE marker block ... — none found`); marker-error cascade fired retry 1; retry 1 succeeded (Bug B fix held — Forge's marker payload task_id matched the now-original envelope task_id); build phase dispatched; PR #5 opened at https://github.com/Larry-Yatch/ourliberty-agent-core/pull/5; Mirror reviewed + emitted REVIEW_PASS at 12:49:26. **Total cost: ~$1.31** ($0.29 + $0.31 + $0.23 + $0.46 + $0.30 across Beacon notify + Forge preflight retry + Beacon notify + Mirror review + Beacon journal). **But Larry got no closing DM** — `reply_chat_id` was None on Mirror's outbox, the auto-DM hook silently skipped.

Trace showed reply_chat_id dropped at HOP 3 — the marker-error retry task in Forge's inbox. Root cause: `_notify_forge_marker_error` was missing the `reply_chat_id` propagation block. The M-3 5a-followup fix had landed on `_notify_mirror_marker_error` (which I'd added later) but the symmetric fix on the Forge equivalent was overlooked — pre-existing gap from 4a/4b. Shipped as **5b-followup-2** (commit `4c79450`).

**Live test 2 (PR #6, `opmanual-d35-5b-followup-shipped-note-001`):** post-Bug-E-fix re-test. Chain ran 3 minutes wall-clock end-to-end. Forge nailed the JSON marker on first try (the strengthened CLAUDE.md guidance from Bug A landed). Build → PR #6 → Mirror REVIEW_PASS → **closing DM landed on Larry's Telegram at 13:07** with the full content (✓ prefix, PR URL, Mirror's verification summary including her grep evidence, "Ready for you to merge manually"). Full chain verified end-to-end including closing DM. Total cost: ~$0.50.

PRs #5 + #6 merged to main (commits `8756e4a` and the prior merge). Closing-DM regression test passed at PR #6 — every terminal-from-Larry's-perspective intent now reliably reaches the originating chat thread.

**Cascade-recovery paths still only unit-tested, not live:** Bug B cascade-recovery (envelope task_id stays original through retries), Bug C dead-letter Larry-DM, Bug E reply_chat_id propagation through marker-error. The unit tests are tight (16 new tests across the 5b-followup commits). Future live runs that happen to trigger marker-error cascades will exercise them naturally.

**Codified additions worth recalling next session:**

5. **The marker-error retry path is the most under-tested code in the dispatch chain.** Three production failures in 5b's verification window (Bug B, Bug C, Bug E) all lived in this path. Tests that exercise multi-hop chains with intentional preflight slips are essential before declaring future commits "live verified."
6. **Forge sometimes drifts on JSON-vs-prose discipline non-deterministically.** Same prompt; first attempt was prose, second attempt was clean JSON. Don't assume "she did it right last time" — verify each live run by reading her outbox content before judging the chain succeeded by-virtue-of-cost-records-existing.

**Next:** D3.5 commit 5c — Beacon replan flow (same as the next-up note in the 5b entry above).

## Phase D3.5 commit 5c — Beacon auto-replan on Mirror ESCALATE (2026-05-13, ~5–6 hours, $4.11)

Status: Shipped 2026-05-13.

Closes the third sub-commit of D3.5: when Mirror emits `REVIEW_ESCALATE` (direct, auto-promoted low-confidence, or revision-budget-exhausted), Beacon now emits a fresh `=== APPROVAL_REQUEST ===` in her inbox-watcher response; the outbox-notifier extracts the marker (impersonating the bot's chat-mode flow), consults `trust_policy`, adds a pending-approvals entry, and queues a `kind: approval_request` alert that the bot's alerts poll surfaces to Larry as a DM — no chat round-trip required. The loop is bounded by `max_replans=2` from `loop_bounds`, with `replan_count` propagated system-side through every dispatch hop. Live-verified end-to-end with Mirror PASS on the re-review; one cleanup gap (worktree_manager checkpoint behaviour during the live test attempt) deferred to a 5c-followup commit.

### The architectural calls

Five decisions signed off pre-code (per `feedback_decision_classification` — TECHNICAL/ARCHITECTURAL/VALUES, with VALUES on a 1–5 dial):

1. **Plumbing fork: Option A.** Notifier extracts Beacon's `=== APPROVAL_REQUEST ===` from her outbox and queues an alert; the bot does NOT poll outbox. Keeps the bot's responsibilities narrow (alerts + chat) and avoids a second outbox-reader race. Locked at design time; no Option B fallback shipped.
2. **Discipline gate: level 3 (medium).** `validate_replan_discipline` requires payload `task_id` to match envelope `task_id` AND payload summary to share ≥2 long-token (>3 chars) overlap with `mirror_escalate_reason`. Failure logs a WARN and falls through to default routing — no marker-error cascade, because the failure mode is upstream-of-Larry (Beacon authored a sloppy summary) rather than a contract violation Forge could re-attempt. Med-8 adaptive-threshold fix tightens this when the Mirror reason itself has fewer than 2 long tokens (`min(2, len(mirror_tokens))`).
3. **`cost_per_task_usd`: deferred to 5d.** 5c ships replan-count enforcement only; the cost-budget arm of the budget envelope lands in 5d alongside auto-merge.
4. **Budget-exhaust: A + B both.** Beacon's CLAUDE.md (Shape 8 rewrite) is the first line — she's told not to emit `=== APPROVAL_REQUEST ===` when `replan_count >= max_replans`. The notifier is the hard backstop — `evaluate_replan_budget` rejects the replan even if Beacon emits anyway, sending a reject-DM instead. A alone would be a single-point-of-failure if Beacon's prompt drifts; B alone would cost a wasted Beacon turn on every cap-violation. Belt + suspenders.
5. **`replan_count` is system-controlled.** Lives on the envelope; stored as `_replan_count` (underscore = system field) on the pending-approvals entry; propagated through every dispatch hop by the notifier. Beacon does not author it. Agents are stateless across dispatches, so anything the agent could observe-and-author is racy by construction — the monotonic counter belongs to the dispatcher.

### Two-pass independent review

Same pattern established in 4a/4b/5a/5a-followup/5b/5b-followup. **First pass** caught 11 issues; 10 fixed pre-push and 1 (Med-7 compound-word regex edge case) deferred for live-test surfacing:

- **C-1** — `replan_count` reset on every Forge→Mirror leg because `_dispatch_build_phase` + `_dispatch_mirror_review` didn't propagate it. Fixed.
- **C-2** — `mirror_escalate_reason` was procedural framing on 2 of 3 trigger paths (auto-promote + budget-exhaust), making the discipline gate unsatisfiable for Beacon's good-faith summary. Fixed by augmenting reason with findings text.
- **M-3/M-4/M-5/M-6** — sentinel-log on exhaust+missing-chat, bare-Exception coverage on `dispatch_approved`, `append_*` return-value check + sentinel on failure, `inbound_intent` propagation anti-contamination comment.
- **Med-8/9/10/11** — adaptive overlap threshold, asymmetric exception coverage, dedup-by-task_id on outbox replay, test-coverage gaps.

**Second pass** (run AFTER the first-pass fixes were applied — explicit "review the diff again with fresh eyes" budget) caught 2 more, shipped as followup commit `cdd56aa` inside PR #7:

- **C-X1 (CRITICAL)** — the C-1 fix propagated `replan_count`/`max_replans` through `_dispatch_build_phase` + `_dispatch_mirror_review` but missed the parallel revision-loop dispatches (`_dispatch_revision_to_forge` + `_dispatch_mirror_review_rerun`). A task that took any revision round before re-escalating would silently reset `replan_count` to 0 on the next ESCALATE notify, defeating `max_replans` entirely. And the revision loop is exactly where this matters — Mirror uses the revision budget BEFORE escalating, by design. Fixed; 2 regression tests added.
- **Med-X1** — Med-10's outbox-replay dedup only checked `state['pending']`. After `auto_approve` the entry moves to `state['history']`; a notifier crash between `resolve()` and outbox archive would let the replay run a fresh `add_pending` + `dispatch_approved`, overwriting the prior Forge task file. Fix: new helper `approval.find_by_id_any_state` searches both pending and history; the replan-approval router uses it for the dedup gate. 1 regression test added.

Two skipped from cdd56aa as low-value polish: Min-X1 (`dispatch_approved` error message conflates dispatch + resolve failures) and Min-X2 (silent drop of findings without description on degraded-Mirror-input).

### What shipped

- **`scripts/beacon_approval_handler.py`** (+~165 LOC) — `DEFAULT_MAX_REPLANS = 2`, `REPLAN_REASON_MIN_TOKEN_OVERLAP = 2`, `evaluate_replan_budget` (parallel to `mirror_review_handler.evaluate_revision_budget`), `validate_replan_discipline` (the level-3 gate with Med-8 adaptive threshold), `add_pending` extended with `replan_count`/`max_replans`, `dispatch_approved` propagates to the next Forge envelope, `find_by_id_any_state` helper (Med-X1).
- **`scripts/outbox_notifier.py`** (+~315 LOC core + ~98 LOC for C-X1 fix) — `_load_max_replans_from_config`, `_BEACON_REPLAN_INBOUND_INTENTS`, `_route_beacon_replan_approval` (the bot-impersonator path: extract → discipline gate → budget gate → trust policy → `add_pending` + queue alert OR auto-approve dispatch OR reject DM); `_classify_mirror_marker` propagates `replan_count`/`max_replans`/`mirror_escalate_reason` on escalate intents; **C-2 fix** augments reason with findings text on auto-promote + budget-exhaust paths; **M-3 + M-5** surface alert-write failures as load-bearing sentinels for watchdog scanning; **Med-9** bare-Exception coverage on `dispatch_approved`; **C-X1 fix** (in cdd56aa) propagates replan budget through `_dispatch_revision_to_forge` + `_dispatch_mirror_review_rerun`.
- **`scripts/inbox_watcher.py`** (+~12 LOC) — `_build_outbox` envelope_fields extended; `inbound_intent` propagated as a separate field with M-6 anti-contamination code comment.
- **`scripts/larry_alerts.py`** (+~52 LOC) — `append_approval_request` + `format_dm` extension for the `kind: approval_request` shape.
- **`scripts/beacon_telegram_bot.py`** (+~30 LOC) — alerts-poll handler recognizes `kind: approval_request` and renders the DM.
- **`agents/beacon/CLAUDE.md`** — Shape 8 rewritten with a structured decision tree (don't emit at cap; emit fresh `=== APPROVAL_REQUEST ===` when escalate intent is valid and `replan_count < max_replans`).
- **Tests** (+~770 LOC, +44 new tests across the two commits): `BeaconReplanLoopTest` in `test_outbox_notifier.py` (22 cases — happy path, discipline-gate fails, budget-exhaust, malformed marker, missing chat_id, idempotency-via-dedup, C-1/C-2 regressions, auto-approve/reject trust paths, inbound_intent propagation, non-escalate regression); `EvaluateReplanBudgetTest` (5), `ValidateReplanDisciplineTest` (8 incl. Med-8 adaptive-threshold), `AddPendingReplanCountTest` (3), `DispatchApprovedPropagatesReplanCountTest` (3) in `test_beacon_approval_handler.py`; plus the 3 second-pass regression tests (C-X1 ×2, Med-X1 ×1) in cdd56aa.
- **Test count:** 519 → 522 local + droplet both clean (4 pre-existing macOS-only worktree failures unrelated).

### Verification (live, 2026-05-13)

The smoke ran in two phases.

**Phase 1 (PR #8, $2.91).** Beacon emitted the 5c-shipped-section APPROVAL_REQUEST; Forge built; Mirror reviewed and approved. The cycle PASSed end-to-end at the chain level, BUT `replan_count=1` propagation was only exercised in unit tests at that point — Mirror's verdict on PR #8 was PASS, so the replan path didn't fire in the live chain. Phase 1 confirmed the no-escalate happy path; it did not exercise the auto-replan branch.

**Phase 2 round 1 ($0.60, surfaced the discipline-gate notify-prefix bug).** A second smoke specifically designed to drive Mirror to ESCALATE so the Beacon auto-replan would fire. Beacon's replan APPROVAL_REQUEST was emitted correctly, but the level-3 discipline gate silently skipped it: the notify-prefix Beacon prepends to her marker (the `[Inter-agent notify | intent=review-escalate | …]` block) was being counted as payload content during the ≥2-word match against `mirror_escalate_reason`, so legitimate replans whose actual summary referenced Mirror's reason were getting falsely rejected because the notify-prefix words dominated the comparison. Larry got the original ESCALATE auto-DM; no second DM with the replan plan; Forge never received the re-dispatch.

**Fix shipped as 5c-followup in PR #9 (commit `033ef1b`).** The notifier now strips the notify-prefix block before running the discipline-gate word-match, so payload summary is compared against `mirror_escalate_reason` cleanly.

**Phase 2 retry ($0.60).** Re-ran the ESCALATE-driving smoke after the PR #9 fix landed. Full 5c chain verified DM-to-DM: Forge build → Mirror review → Mirror ESCALATE → Larry auto-DM on ESCALATE → Beacon replan APPROVAL_REQUEST → discipline-gate PASS → Larry auto-DM with replan plan → approve → Forge re-dispatch → Mirror re-review → PASS → Larry closing DM. `replan_count=1` propagation verified end-to-end through every hop. Closing DM landed.

**Total smoke spend: $4.11** ($2.91 Phase 1 + $0.60 Phase 2 round 1 + $0.60 Phase 2 retry; PR #9 fix cost not counted here — it was its own commit + tests in the 5c-followup dispatch).

The two-pass smoke pattern (happy path first, then a designed-to-fail second pass) is the same shape established in 5b-followup. The Phase 2 bug surfacing on the first auto-replan attempt validates the practice — without it, the discipline-gate skip would have shipped silent and Larry would have seen an ESCALATE auto-DM with no follow-through across every future replan.

### Known cleanup gaps

- **`scripts/worktree_manager.py` — checkpoint behaviour during the PR #7 Mirror review attempt.** Surfaced during the live test; the exact symptom (suspected: an existing per-task checkpoint commit was overwritten / not preserved across a review re-run when the same branch was reused) is **(symptom to be captured in 5c-followup entry)** — not recoverable from PR #7 comments (PR has zero comments) or commit history in this worktree. The live chain still completed (Mirror PASS), so the bug did not block 5c shipping, but it needs a clean repro + fix before 5d's auto-merge lands and starts depending on stable checkpoint state. Tracked as `5c-followup`.

This `### Known cleanup gaps` subsection is new convention worth keeping: surface a forward pointer when a phase ships with a known-but-deferred issue, so future-Forge and future-Larry both have a paper trail without editing the prior shipped entry.

### Codified additions worth recalling next session

7. **Level-3 discipline gates that silently fall through (WARN + default routing) are the right shape when the failure is upstream-of-Larry.** Marker-error cascades only make sense when the misbehaving agent could re-attempt and succeed; when the misbehavior is "Beacon authored a sloppy `mirror_escalate_reason`" there's nothing for the cascade to fix. Fail closed (replan does not dispatch), but don't fan out alarms — just log a sentinel for watchdog scanning.
8. **System-controlled monotonic counters (`replan_count`, `revision_count`, future `cost_per_task_usd`) live on the envelope and are incremented by the notifier, not by the agent.** Agents are stateless across dispatches. Any counter the agent could observe-and-author is racy by construction; the dispatcher is the only place with both atomicity and the full history.
9. **The two-pass independent review pattern is now standard for D3.5 commits — budget for it.** Second-pass review of the post-fix diff has caught a CRITICAL on 4a, 5a-followup, 5b-followup, and 5c (C-X1 here). The "I already fixed the first 11; surely it's clean now" instinct is the failure mode the second pass exists to interrupt. Plan ~30 minutes + ~$0.20 per commit for it.

**Next:** D3.5 commit 5d — auto-merge on Mirror PASS + `cost_per_task_usd` budget enforcement. Closes D3.5.

## Phase D3.5 commit 5d — auto-merge + EMERGENCY_HALT trip + cost-budget gate (2026-05-14, ~6 hours, $1.77 live smokes)

Status: Shipped 2026-05-14. **D3.5 closes.**

The fourth and final sub-commit of D3.5. Mirror's `REVIEW_PASS` marker now triggers `gh pr merge <N> --squash --delete-branch` synchronously; her `REVIEW_EMERGENCY_HALT` marker now trips `~/agents/blackboard/EMERGENCY_HALT` automatically + broadcasts a priority `kind: alert` DM to all authorized chats (vs the targeted closing-DM channel used for routine intents); the `loop_bounds.cost_per_task_usd` config field (pre-staged in 5a but never read) is now enforced at all four dispatch sites in `outbox_notifier.py`. After 5d, the dispatch loop runs without a human in the merge step — Larry sees only the closing DM (per task) and, if EMERGENCY, a phone-priority broadcast.

### The architectural calls

Seven decisions signed off pre-code per `feedback_decision_classification` (TECHNICAL / ARCHITECTURAL / VALUES, with VALUES on 1–5 dial or A/B):

1. **Q1 — Auto-merge speed (VALUES, 1-5 dial).** Dial 1: merge immediately on PASS, no grace window. Trade-off: re-introduces "Larry watches the chat for 30 min after every approval" if grace window > 0. Rejected dial 5 (30-min grace) — the whole point of D3.5 is to remove Larry from the merge step. Recovery from a Mirror miscalibration is `git revert` (same cost in both worlds).
2. **Q2 — Auto-merge confidence gate (VALUES, A/B).** Option A: any-confidence PASS auto-merges. Mirror's CLAUDE.md already gates PASS on severity (no findings ≥ medium + no security/safety regardless of confidence) — `confidence` is informational, not a second gate. Rejected B (high-confidence-only) as belt-and-suspenders that costs the "Larry sees escalations only" promise.
3. **Q3 — Auto-merge failure routing (ARCHITECTURAL, A/B/C).** A: standard closing-DM channel via the 5a-followup `append_notification` pipe. Same render-then-queue path; just a different body variant (`failed`). Rejected B (priority broadcast for failures) and C (both) as premature for a not-yet-bitten path. Type-of-failure granularity (conflict vs auth-expired vs branch-protection) reserved for future iteration if a real conflict ever fires.
4. **Q5 — EMERGENCY_HALT scope (VALUES, A/B).** Option A: all four agents halt on next 5s poll. The existing `kill_switch.py` infrastructure already advertises this shape; the inbox-watcher and outbox-notifier both poll the halt file at the top of their main loops (pre-existing, audited from upstream). Rejected B (Beacon + Pulse stay up to support chat-with-Beacon-mid-halt) as a meaningful design change to battle-tested components; revisit if a real halt fires and Larry immediately wishes he could chat.
5. **Q7 — cost_per_task_usd activation (VALUES, 1-5 dial).** Dial 3: activate the gate at all four dispatch sites (`_dispatch_build_phase`, `_dispatch_revision_to_forge`, `_dispatch_mirror_review`, `_dispatch_mirror_review_rerun`). $5 is the hard ceiling per logical task. Rejected dial 1 (defer further) as the second-line bound 5c promised; rejected dial 5 (extend to Beacon's chat-mode dispatch) as overreach into Larry's own conversations.
6. **DM ordering (ARCHITECTURAL).** Merge → render DM body based on outcome → queue DM → archive outbox. Rejected the inverse (render DM first, then fire merge) because a DM saying "merged" followed by a merge failure produces a phantom-merge state that's actively misleading. The current ordering also enables clean crash recovery: if the daemon dies between merge and archive, the next process_outbox re-fires `gh pr merge`, which returns non-zero (already merged) but the `_gh_pr_state` recheck disambiguates that as `already_merged` → success-shaped DM body.
7. **EMERGENCY_HALT broadcast vs closing-DM (ARCHITECTURAL).** The halt's priority alert uses `larry_alerts.append_alert(severity='critical', subject='emergency-halt:<task>')` — broadcasts to all authorized chats, no cooldown across distinct task_ids. `review-emergency-halt` was REMOVED from `TERMINAL_DM_INTENTS` so the routine closing-DM pipe does NOT also fire — a single safety event produces one DM, not two with inconsistent guidance. (5d code-review fix #5.)

### What shipped

- **`scripts/outbox_notifier.py`** (+~820 LOC, the bulk of 5d):
  - `_auto_merge_pr(pr_url, task_id)` helper: parses repo coords + PR number from URL (anchored regex `^https?://github.com/...` per code-review fix #6 to prevent embedded-URL smuggling), shells out `gh pr merge <N> --repo <coords> --squash --delete-branch` with 30s timeout, distinguishes `merged` / `already_merged` (resume-after-crash success path via `gh pr view --json state` recheck) / `failed` outcomes. Handles `subprocess.TimeoutExpired`, `FileNotFoundError` (gh CLI missing), `OSError` defensively — daemon-never-wedge invariant preserved.
  - `_trip_emergency_halt(data, payload)` helper: writes `~/agents/blackboard/EMERGENCY_HALT` with a `kill_switch.halt()`-shaped JSON envelope (`activated_at`, `activated_by="mirror-marker"`, `reason`, `evidence`, `task_id`, `pr_url`) idempotently (no-op if file exists from a prior operator halt — but the priority alert still queues so new evidence reaches Larry). Per-task-id cooldown bucket on the alert subject so distinct task halts don't suppress each other.
  - `_check_cost_budget(task_id, cap_usd)` + `_enforce_cost_budget(task_id, dispatch_label, data)`: sums `cost_usd` field by `task_id` from `~/agents/blackboard/costs.jsonl` (field-name drift from upstream noted in `docs/upstream-audit.md` Pattern G — local uses `cost_usd`, upstream uses `total_cost_usd`); tolerates missing file, malformed lines, negative values, bool subclass-of-int. Per-task daemon-lifetime DM dedup via `_COST_BUDGET_DMED_TASKS` set (code-review fix #4) — first cap-fire DMs Larry; subsequent fires within the same daemon process log "refusing dispatch" only. Set cleared on daemon restart (intentional — Larry may have raised the cap).
  - `_load_cost_per_task_cap_usd_from_config()` reads `loop_bounds.cost_per_task_usd` via the shared `_LOOP_BOUNDS_CACHE` (same shape as `_load_max_revisions_from_config` + `_load_max_replans_from_config`). Defaults to `DEFAULT_COST_PER_TASK_USD_CAP = 5.0` on missing/malformed/bool/negative/string. Activates the 5a aspirational `_note` claim.
  - `process_outbox` marker-routing block extended: `review_emergency_halt` calls `_trip_emergency_halt` BEFORE the routine notify-to-Beacon (so the halt is live before any subsequent dispatch on the next poll); `review_pass` calls `_auto_merge_pr` AFTER `_classify_mirror_marker` but BEFORE `_maybe_dm_larry` (so the DM render reads `merge_result['merge_outcome']` and picks the outcome-aware variant from `_REVIEW_PASS_DM_VARIANTS`).
  - `main_loop` extended with two halt-checks (code-review fixes 2-#1 and 2-#2): one inside the per-file inner loop (so a mid-iteration halt trip stops the current poll's remaining file processing instead of leaking dispatches through the rest of mirror/pulse outboxes), one before the dead-letter scan (so halt fired on the last file of the last agent doesn't leak through dead-letter notify writes).
  - Cost-budget gate positioned AFTER the idempotency check at all four dispatch sites (second-pass code-review fix 2-#1) — without this, crash-resume re-processing of an outbox whose dispatch already landed would fire a false-alarm cost-DM for work that isn't being attempted. The dedup set then would consume the budget for the genuine later DM.
  - `INTENT_ACTION_BLOCKS['review-pass']` + `['review-escalate']` + `['review-emergency-halt']` rewritten to reflect 5b/5c/5d as live (no more "5d will wire" stubs). New `cost-budget-exhausted` action block + DM template.
  - `_REVIEW_PASS_DM_VARIANTS` dict with three outcomes (`merged` / `already_merged` / `failed`). The `already_merged` variant renders the same user-visible body as `merged` (PR is merged either way; resume distinction is in logs, not load-bearing for Larry).
  - `_AUTO_MERGE_FN_OVERRIDE` module-level test-mode hook: tests inject a synthetic outcome so existing integration tests (e.g. `MirrorMarkerRoutingTest`) don't shell out to real `gh` against real GitHub on PASS markers. Production keeps it `None`; the `try/except Exception` wrapper guarantees a buggy/raising override falls through to a `failed` outcome rather than wedging the daemon.

- **`agents/beacon/CLAUDE.md`**: Shape 6 (review-pass) + Shape 9 (review-emergency-halt) + the parent section heading + framing paragraph all updated to reflect 5a-through-5d as live. Shape 9 explicitly tells Beacon to journal-and-stand-down on emergency_halt (halt-file is already tripped + Larry is already priority-DMed by the time her notify arrives; her further dispatches would be useless because the halt is sticky).

- **`agents/pulse/TOOLS.md` line 103**: PR-stale threshold dropped from 24h to 72h. Rationale in the inline comment: clean+green PRs auto-merge themselves now; only Larry-blocked PRs (auto-merge failures or replan approvals) should still be open at 72h.

- **`config/agent-models.json`** `_history`: new 2026-05-14 entry documenting cost-budget gate activation + auto-merge + EMERGENCY_HALT trip + the fact that the previous `_note` on `loop_bounds` was aspirational pre-5d (true as of this commit).

- **`scripts/tests/test_outbox_notifier.py`** (+~1230 LOC, +68 new tests):
  - `ParsePrUrlTest` (10): URL parsing edge cases (trailing slash, query, fragment, /files suffix, non-PR URL, non-github, empty, None, zero PR number, anchored prefix).
  - `AutoMergePRTest` (10): success / gh command shape / failure-conflict / failure-branch-protection / failure-auth / timeout / gh-missing / OSError / malformed PR URL no-shellout / already-merged resume.
  - `AlreadyMergedResumeTest` (3): success-body shape / failure-body shape / merged-body shape for the outcome-aware render pipeline.
  - `EmergencyHaltTripTest` (8): halt envelope shape / idempotent re-trip / kind:alert queued / suggested_action carries recovery command (NOT duplicated in body per code-review fix #15) / per-task cooldown bucket / same-task cooldown suppresses / missing-payload-fields-don't-crash.
  - `CostBudgetGateTest` (16): missing ledger / below-cap / at-cap / over-cap / malformed-lines / negative-cost / bool-cost / enforce-allows-below-cap / enforce-refuses-at-cap-and-DMs / no-chat-id still refuses / config-loader-default / config-loader-valid / config-loader-bool-falls-back / config-loader-negative / config-loader-string / **dedup tests** (3 cases for the code-review-#4 daemon-lifetime DM dedup: same-task within lifetime, per-task not globally, reset after restart).
  - `MirrorMarkerRoutingAutoMergeTest` (9): auto-merge fires on PASS / skipped on revision / skipped on escalate / skipped on emergency / DM body reflects merged outcome / DM body reflects failed outcome / DM body reflects already_merged outcome / override exception renders failed / marker-error no auto-merge / **resume-after-crash end-to-end** (per principle 6 N+1 contract).
  - `CostBudgetGateAtDispatchSitesTest` (7): all four dispatch sites refuse at cap / DM queued on refusal / under-cap allows dispatch / **idempotent-skip does NOT fire false-alarm cost DM** (regression test for second-pass code-review fix 2-#1).
  - `CostDmTemplateTest` (3): renders with full fields / terminal intent registration / missing kwargs renders with `?` placeholders.

- **Existing tests updated:**
  - `test_review_emergency_renders_reason_and_evidence` → `test_review_emergency_does_not_dm_in_5d` (inversion: closing-DM pipe now skips review-emergency-halt; the broadcast handles it).
  - `test_alert_body_includes_recovery_command` → `test_alert_suggested_action_includes_recovery_command` (recovery command in `suggested_action` only, not duplicated in body).
  - `MirrorMarkerRoutingTest` setUp: injects `_AUTO_MERGE_FN_OVERRIDE` so PASS markers don't shell out to real gh against real GitHub.

### Two-pass independent review

Same pattern established in 4a/4b/5a/5a-followup/5b/5b-followup/5c.

**First pass:** 15 findings — 7 MAJOR (2 doc-drift on stale 5a-era stubs, 4 safety-event behavior, 1 stalled-state propagation) + 8 MINOR/NIT. Eight fixed in-commit (6 MAJOR + 2 MINOR); 3 deferred (cost-gate upstream propagation, costs.jsonl unbounded growth, CRITICAL-log-level vs watchdog vocabulary); 4 judged not worth shipping.

**Second pass** (run after the first-pass fixes were applied): 3 findings. **2 fixed** — both load-bearing:
- **2-#1 MAJOR** — cost-budget gate fired BEFORE the idempotency check at all four dispatch sites. On crash-resume re-process where the downstream dispatch already landed, the cost-DM would fire a false alarm for work that wasn't being attempted. Same multi-site shape as 5c's C-X1 (fix in N places but missed N+M). Repositioned the gate to fire only after the idempotency check confirms a genuine new dispatch attempt. Regression test (`test_idempotent_skip_does_not_fire_cost_dm`) locks the contract.
- **2-#2 MINOR** — `main_loop`'s inner-loop halt check (added by first-pass fix #3) didn't cover the dead-letter scan that runs after the outbox-for-agent block completes. A halt fired on the very last file of the very last agent would leak through dead-letter notify writes. Added a halt-check before the dead-letter scan.

1 NIT deferred (test classes that exercise `_dispatch_*` helpers but don't reset `_COST_BUDGET_DMED_TASKS` — pre-existing, no risk today; flagged for future test-isolation refactor).

### Verification (live, 2026-05-14)

Four smokes executed end-to-end. Total live spend: **$1.77** (well under the $5–7 plan budget).

**Smoke 1 — happy-path auto-merge ($0.87):** real Forge dispatch on `d35-5d-smoke-1-opmanual-5c-cost` (task: replace the 5c entry's H2 `$TBD` with `$4.11` — itself the deferred-from-5c cleanup that 5d was scoped to bundle). Forge built + opened PR #13 at 21:36:25Z; Mirror reviewed + PASSed at 21:38:06Z; **auto-merge fired at 21:38:09Z** (`outcome=merged --squash --delete-branch`); closing DM landed on Larry's phone with the outcome-aware body "Auto-merged + branch deleted." Wall clock: ~2 min from dispatch to merge. Branch deleted on origin (verified via gh API 404). Cost-budget gate logged `current=$0.37 cap=$5.00 (allowed)` at the mirror-review dispatch — proving the gate is active in production traffic, not just at the at-cap path.

**Smoke 4 — cost-budget gate ($0, synthetic):** injected 3 synthetic cost records summing $5.50 for a test task_id into the live droplet's `costs.jsonl`; verified `_check_cost_budget` returns `at_cap=True, current=$5.50`; verified `_enforce_cost_budget` refuses + DMs Larry with the correct body ("Cost-budget cap fired on task ... Refused dispatch: smoke-4-synthetic"); verified second call refuses without re-DMing (daemon-lifetime dedup per fix #4); cleanup restored the production ledger (107 → 104 lines, removed 3 synthetic).

**Smoke 3 — EMERGENCY_HALT trip + recovery ($0, synthetic):** dropped a synthetic Mirror `REVIEW_EMERGENCY_HALT` outbox into the live `outboxes/mirror/` (full notifier flow exercised). At 21:40:34Z, the notifier:
- wrote the halt-file envelope (verified contents match the `_trip_emergency_halt` shape — `activated_by: mirror-marker`, full reason + evidence + task_id + pr_url)
- queued the broadcast priority alert (`severity: critical`, `subject: emergency-halt:<task>`, body includes "All four agents halt on next 5s poll. The halt is sticky.", `suggested_action: python3 ~/agent-core/scripts/kill_switch.py resume`)
- logged the CRITICAL trip line and the **fix 2-#2 line** ("EMERGENCY_HALT detected before dead-letter scan — aborting remaining poll work; exiting cleanly")
- exited cleanly; on next poll the inbox-watcher exited cleanly too

Verified both daemons reported `inactive` post-trip (halt-honored). The four Telegram bots remained `active` (they're chat/alerts surface — not dispatchers — consistent with the broadcast model). Then: cleaned up Beacon's cascade notify (per `feedback_smoke_test_archive_not_outbox`), ran `kill_switch.py status` (correctly reads back the Mirror-shaped envelope), ran `kill_switch.py resume` (file removed), restarted the daemons via systemd, verified both `active` again. **Full halt → resume cycle live-verified.**

**Smoke 2 — round-1 PASS + auto-merge ($0.90):** real Forge dispatch on `d35-5d-smoke-2-cost-budget-docstring` (task: add a 2-line summary to `_check_cost_budget`'s docstring). The intent was to drive a revision round + auto-merge on round 2 (per principle 6's N+1 contract for loop-shaped features). Mirror PASSed on round 1 — the spec was tight enough that her instincts didn't flag scope creep. PR #14 auto-merged at 21:44:32Z. **The revision loop + auto-merge composition on round-N PASS was NOT live-exercised this commit.** Mitigation: (1) the 5b live tests already verified revision → re-review end-to-end; (2) the 5d code path doesn't branch on which round PASSes — `_auto_merge_pr` fires on any `review_pass` marker regardless of `revision_count`; (3) unit tests (`MirrorMarkerRoutingAutoMergeTest`, `test_resume_after_crash_renders_success_body`) exercise the auto-merge call shape on resume + reentry. Flagged as a partial-coverage live-test gap for future organic data.

### Known cleanup gaps (deferred from 5d)

1. **Cost-gate upstream propagation (first-pass review #7).** When `_enforce_cost_budget` refuses, the originating chain has no signal that the dispatch was stalled — Beacon's journal stays "in flight" while no work is happening. Suggested fix shapes (sentinel file in `~/agents/blackboard/state/cost-stalled/`, synthetic Beacon notify with `cost-budget-exhausted` intent) — design not clear yet; defer to organic data when a real cap-fire happens.
2. **`costs.jsonl` unbounded growth (first-pass review #13).** Pre-existing; 5d increases read frequency ~5× by adding gates at four dispatch sites. At current scale (~100 lines, weeks of operation) this is fine. Long-term: either compact periodically by task_id or cache per-daemon-lifetime.
3. **`CRITICAL` log-level vocabulary (first-pass review #16).** `_trip_emergency_halt` logs at `'CRITICAL'` level; sentinel/watchdog scans may key on `WARN`/`ERROR` only. Investigate and either align the level or extend the sentinel's level vocabulary.
4. **Revision-loop + auto-merge live N+1 contract (smoke 2 gap).** Smoke 2's spec was too tight to drive Mirror to REVISION on round 1; the round-2-PASS-then-auto-merge composition wasn't exercised live. Wait for organic data — first real revision cycle on a future task will exercise it.

### Codified additions worth recalling next session

10. **Order matters between irreversible actions and notification.** 5d's merge-then-render-DM ordering was the load-bearing decision call; the inverse (render-DM-first) creates phantom-merge state on failure. The general principle: **fire the irreversible action FIRST, then render a notification that accurately reflects what happened.** This is the inverse of what feels intuitive ("validate everything before acting") because the rendering itself is well-tested + has degraded-to-silence semantics, while the irreversible-action's outcome can only be known after the call.
11. **Multi-site fixes need uniform application across ALL sites.** The first-pass review's MAJOR finding on cost-gate ordering was an N+M pattern: fixed in N places (build-phase) but the same gap existed at three more dispatch sites. The 5c arc had the same shape (C-X1 caught on second pass). **Whenever a code-review finding involves "X happens before Y at site Z", grep for sister sites that should have the same X-before-Y discipline.**
12. **Test-mode override hooks for shell-out paths.** `_AUTO_MERGE_FN_OVERRIDE` lets the existing test suite continue to integration-test the marker-routing block without shelling out to real `gh` against the real GitHub repo. The pattern: module-level `Optional[Callable]` defaults to None; production reads `None or real_function`; tests monkey-patch to inject a synthetic outcome; the call site wraps the override in `try/except Exception` so a buggy/missing override falls through to the production-grade failure mode (daemon-never-wedge). Replicate when adding future shell-out primitives.
13. **EMERGENCY_HALT broadcast vs targeted DM.** `kind: alert` broadcasts to all authorized chats and was the right shape for halt events (safety > pure-Larry signal). `kind: notification` targets a single chat and is the right shape for chain-completion DMs. The decision call lives in *who needs to know*, not in *how loud*. Future ops events that cross the "everyone authorized" threshold belong on the broadcast channel.

**Next:** D3.5 closes. Phase E (Aide agent prototype) is next per `reference_droplet.md`'s deferred list. The 5d-deferred cost-gate-upstream-propagation + costs.jsonl-compaction items wait for organic data before redesign.

## Phase E5 — Google Suite via MCP (2026-05-18 + 2026-05-19, ~2 sessions)

Status: Shipped 2026-05-19. **E5 + E5.3 close.** PRs #37, #38, #39.

Beacon can now create, read, and edit Google Docs/Drive/Gmail/Calendar from the droplet, scoped to the agent's own Google account (`agent.beacon.ourliberty@gmail.com`), fully isolated from Larry's personal Google. Detailed wiring lives in memory `project_phase_e5_complete.md`; concise build narrative here.

**Identity stack locked:**
- Droplet Claude Code = separate Anthropic Max plan tied to the agent Google account (NOT Larry's personal account, NOT API-key auth — Larry preferred subscription billing).
- Google identity = same agent email. All Google MCP calls scoped to this account.
- Larry's personal Anthropic + personal Google = unchanged, used from his laptop only.

**Two MCP servers on droplet:**
- **workspace-mcp** (`uvx --from workspace-mcp ... --single-user --tools docs drive --transport stdio`) — primary Docs/Drive surface, full editing capability. OAuth token at `~/.google_workspace_mcp/credentials/<email>.json`. Required adding the agent email as a Test User in `beacon-agent` Google Cloud Console (Testing-mode OAuth client).
- **claude.ai connectors** (Drive/Gmail/Calendar) — Gmail/Calendar primary, Drive kept as week-1 fallback for workspace-mcp.

**Convention codified in `shared/google-workspace.md`:** workspace-mcp owns Docs/Drive primary; claude.ai owns Gmail/Calendar; everything Beacon creates must end up inside `Shared with Larry/<sub-folder>/` (sharing inheritance handles Larry's personal-account visibility). Non-negotiable two-step pattern for workspace-mcp: `create_doc` (lands at root) → `update_drive_file(add_parents=<folder>, remove_parents="root")`. End-to-end smoke verified 2026-05-19; Larry confirmed doc visibility from personal Drive.

**Beacon allowlist:** 57 entries (3 Bash + 25 claude.ai + 29 workspace-mcp). Intentionally excluded for escalation posture: delete-class tools (don't exist in `--tools docs drive` install anyway), `set_drive_file_permissions` + `manage_drive_access` (sharing changes escalate to Larry), `start_google_auth` (operator-only), `debug_*` (debug-only), `check_drive_file_public_access` (low value).

**E5.3 (PR #39) closed the workflow question:** Beacon now knows when to draft specs in a Google Doc vs reply inline in Telegram (heuristic: ~400 words / multi-section / Larry asks → Doc; else inline). The architectural rule: **Doc is the editing surface for Larry; APPROVAL_REQUEST marker `prompt` is the source of truth that reaches Forge.** Beacon reads the final Doc state via `get_doc_as_markdown` at dispatch time, embeds the full body in the marker prompt, includes the Doc URL for human traceability. Forge does NOT have Google MCP tools — marker stays self-contained. Preserves every D3/D3.5 gate. Long-conversation notes-doc summarization is offer-only (5a pattern, no auto-trigger).

**Key lessons / gotchas:**
- workspace-mcp credential dir is `~/.google_workspace_mcp/credentials/`, NOT `~/.config/workspace-mcp/` (the latter only holds `client_secret.json`). First orchestrator iteration watched the wrong directory for 10 min.
- OAuth client in Testing mode requires explicitly adding the agent email as a Test User in Cloud Console — even though the agent owns the project, it isn't auto-added.
- workspace-mcp's `create_doc` doesn't take a folder param. The `update_drive_file` two-step is non-negotiable; conventions doc spells it out.
- Headless OAuth via PTY orchestrator + SSH-tunneled localhost:8000 pattern (same shape as the `claude auth login` orchestrator) works cleanly. Pattern captured in `feedback_headless_oauth_orchestrator` memory; re-usable for Vercel/GitHub/AWS auth in E2.

**Next:** E1 hardening (same session, same day).

## Phase E1 — Hardening (2026-05-19, ~1 day actual vs ~3 days estimated, 4 PRs)

Status: Shipped 2026-05-19. **E1 closes.** PRs #40, #41, #42, #43.

Three structural cracks closed plus an opportunistic test-hygiene pass. Estimated 3 days; actual ~1 day because D2.5 had already done more than the phase-e-plan reflected when authored, and the framing-first pattern caught the redundancy before code was written.

**E1.1 — `render_marker` helpers + `marker.py` CLI + drift tests (PR #40).** Closed the silent-dead-letter class of bugs that took PR #16 7+ hours to surface. Each handler (`forge_preflight_handler`, `mirror_review_handler`, `beacon_approval_handler`) gained a `MARKER_KEYWORDS` registry + a `render_marker(type, **payload) -> str` function that validates type + required fields, JSON-encodes payload, wraps in canonical delimiters. `scripts/marker.py` exposes `render`/`validate`/`types` subcommands; agents pipe JSON via Bash and paste the output verbatim. New `test_marker_drift.py` cross-handler test catches any future marker type added without a sample payload + render coverage. Each agent's CLAUDE.md gained a "How to emit a marker safely" subsection documenting the CLI path. 167 marker-related tests, all green.

**E1 hygiene pass (PR #41).** While auditing for E1.2, discovered 13 stale test failures across three modules, no production-code touches:
- `test_worktree_manager` — macOS-only path comparison failures (`/var/folders/...` vs realpath `/private/var/folders/...`). Fixed by `.resolve()` in setUp.
- `test_outbox_notifier.CostBudgetGateTest` — D3.5 5d raised `loop_bounds.cost_per_task_usd` from $5 → $15; tests still wrote `cost_usd=10.0` expecting refusal. Bumped to $20.
- `test_ledger` — autonomous PRs #33/#34 changed task_type inference (`unknown` → `unclassified`/prefix-derived) and excluded `notify-*` from retry_overhead; tests weren't updated. Brought assertions into alignment with current contract; each fix carries an inline comment naming the originating PR. **Observation worth a future thought:** Mirror reviewed PRs #33/#34 but didn't catch the test drift — tests passed in isolation against old code, just not in the discover-all run after merge. Possible D3.5 retro item: should Mirror's checklist include "did any test assertion need updating to match the new contract?"

**E1.2 — `expected_agent` parameter + centralized preamble gating (PR #42).** Framing surfaced that D2.5 had already migrated `inbox_watcher` onto `agent_runner.run_claude` and wired the in-flight registry, parent-CLAUDE.md guard, /tmp landmine scrub, and EMERGENCY_HALT check. The phase-e-plan was authored 2026-05-18 with stale awareness. The one remaining piece was the identity-assertion preamble: the watcher built it manually with three gating conditions before passing the prompt. This PR moved that logic into `agent_runner._maybe_prepend_identity_assertion`, added an `expected_agent` kwarg to `run_claude`, and simplified the watcher (14 lines removed, 1 added). Now every `run_claude` caller gets the right behavior by passing one parameter; future callers can't forget the marker-already-present check. 6 new tests cover the four gating conditions plus idempotency under repeated calls. Note: `agent_runner.process_inbox` still has the old inline gating at line 1075–1092 — left intact since that codepath isn't exercised in our fork (cleanup for a future pass).

**E1.3 — `heal_pr_auto_merge.py` healer (PR #43).** D3.5 5d wired the primary auto-merge path in `outbox_notifier._auto_merge_pr` (fires on Mirror REVIEW_PASS). This healer is defense in depth for the residual gaps: notifier crashed before merge, `gh pr merge` returned non-zero (CI not green yet, network blip, transient API), Mirror PASS in an outbox the notifier never processed, post-conflict resolution with no re-trigger. **Mirror-PASS detection** scans `~/agents/logs/outbox-notifier.log` for `AUTO_MERGE ... outcome=failed` entries in the last 24h — the healer only acts on PRs the primary path already attempted, never auto-merges anything Mirror didn't review.

Adapted from upstream `gm-agent-core/scripts/heal_pr_auto_merge.py` (#240): pulled `is_mergeable` defensive predicate, CANCELLED-workflow rerun (GitHub auto-cancels in-flight CI when a healer pushes; rerun unblocks), `MAX_MERGES_PER_RUN=5` blast-radius cap, HOLD_PREFIXES + HOLD_LABELS lists, benign already-merged detection. Added on top: Mirror-PASS log filter, per-PR retry budget (3 attempts) with state file `~/agents/state/heal-pr-auto-merge.json`, Telegram DMs via `larry_alerts.append_alert` (activation DM when dry-run finds a candidate, stalled DM + GitHub `automerge-stalled` label when budget exhausts, brief success DM on merge), two-layer kill-switch (`healers.disabled` blanket + `OURLIBERTY_AUTOMERGE_ENABLED` env var per phase-e-plan's "default off until verified").

**Discoverability win:** Larry never has to remember to activate the healer — when the dry-run mode encounters its first real candidate, it DMs him with the exact `systemctl edit` + `restart` commands. 37 new tests (all gh shell-outs mocked). Systemd timer at 5-min cadence matches other healers. INSTALL.md updated for the 8-healer install.

**Decision pattern reinforced:** the probe-before-custom-build heuristic worked twice this session — E5 (re-enable existing workspace-mcp instead of building a Google Docs API wrapper) and E1.3 (clone upstream's healer for reference, write Larry-flavored on top of what we have, don't pull wholesale). Both saved 1–2 days vs the "build it ourselves" path. Pattern captured in `feedback_probe_before_custom_build` memory.

**E1 critical-path unblock:** E2 (Vercel deploy layer) can land safely on top of a hardened chain.

**Next:** E2 — Vercel preview-first deploy layer. ~3–4 days estimated. Different shape (new external system + multi-day, fresh-session work). E3 (read-only dashboard) follows E2 and dogfoods E2's preview URLs. Critical path: E1 → E2 → E3 → E4.

## Phase E2.0 + E1.5 — Vercel install + Credential Rotation Discipline (2026-05-19, single ~6-hour session)

Status: Shipped 2026-05-19. **E2.0 done. E1.5 + E1.5.2 closed. Task #17 + Task #19 follow-ups also landed same session.** Five PRs total: #45 (design), #46 (implementation), #47 (chat-ID registry follow-up), #48 (task #17 headless Beacon APPROVAL_REQUEST handler), #49 (task #19 source-routing narrowing fix).

Phase E2.0 was the small one-time Vercel signup + token install that unblocks E2.1-E2.3 (deploy layer). Phase E1.5 was a phase inserted MID-SESSION — surfaced organically by auditing `.env.larry` during the Vercel install. The audit revealed eight active credentials across four storage locations (`.env.larry` + gh CLI keychain + Claude Max OAuth + workspace-mcp OAuth), seventeen empty placeholder slots in `.env.larry` accumulated since Phase A, and zero tracking of rotation cadences. The DigitalOcean template comment "rotate every 90 days" had been silent for eleven days. Larry's framing: *"Can you make it so the calendar reminder is part of Beacon's calendar, and Beacon walks me through the process? We need to make that a part of the system."*

E1.5 became "credential rotation as a system primitive": a registry file, a Mirror-enforced 4-artifact convention, two drift healers (credential drift + systemd install drift), a Pulse cycle extension that DMs Larry on upcoming rotations, a log-parser-based scope-usage analyzer for the scope-audit cycles, and Beacon-owned Google Calendar events as the visible reminder layer. The phase ran across two PRs (#45 design + #46 implementation) plus a fix-on-first-dogfood follow-up (#47), plus two architectural-finding closeouts (#48 task #17, #49 task #19).

### Decisions locked during the session

- **Default rotation cadence: 365 days (Dial 4)** — matches the Vercel PAT max; cognitive load matches. DigitalOcean's 90-day vendor-mandated cadence overrides on a per-entry basis.
- **Drift healer posture: fail-closed, DM every 6h until reconciled** (Q2 option A). The failure mode of a missing entry is "your token silently lapses and your droplet stops deploying"; the cost exceeds the cost of repeated DMs. DMs stop the moment the entry is added.
- **Scope-audit DM body: (B) usage-analysis** — not just "list current scopes, ask Larry." Pulse runs `scripts/scope_usage_parser.py` on the credential's scope namespace and proposes specific drops in the DM. Log-parser approach (not call-site instrumentation) for the first cycle in 2027 — defer dedicated instrumentation unless log-parsing proves too fragile.
- **Empty-slot cleanup in `.env.larry`: remove the 17 placeholders, don't keep with comments** (Option B). Empty slots violate the 4-artifact discipline — they're "credentials without real existence." Re-add via the discipline when needed.
- **Registry covers all 4 storage locations, not just `.env.larry`** (Option B). Anything less misses the actual most-important credentials (gh-OAuth, Claude Max, Google OAuth refresh). Schema gained one field (`storage_location`); healer gained a small dispatch table mapping location-type → scanner.
- **Vercel account ownership: Larry's personal email** (Path A from E2.0 design). Vercel is your deploy infrastructure, not an agent capability surface. The agent only needs a token; it doesn't need to own the account.
- **Token rotation discipline as 4-artifact rule**: every new credential ships with credential + registry entry + runbook + Beacon calendar event, in ONE PR. Split = silent drift window. Mirror enforces; drift healer fails closed on any orphan.

### What shipped (highlights)

- `/home/larry/credentials/.env.larry` populated with `VERCEL_TOKEN=<60-char vcp_* PAT>` (Hobby tier, 1-year expiration). 17 empty placeholder lines removed post-PR-#46.
- `config/token-rotation-schedule.json` (194 lines after #47): 10-entry registry across 4 storage locations + `known_storage_locations` scanner-strategy map.
- `shared/credentials-discipline.md`: the Mirror-enforced 4-artifact rule.
- `docs/runbooks/` (new directory) with 8 runbooks: canonical Vercel rotation + telegram bot rotation + 3 scope-audit runbooks + 3 stubs for not-yet-wired credentials.
- `scripts/validate_token_rotation_schedule.py` (~310 LOC + ~380 LOC tests, 25 tests): schema + semantic validator.
- `scripts/heal_credential_registry_drift.py` (~566 LOC + ~415 LOC tests, ~30 tests): every-6h drift healer with fail-closed DMs + activation pattern matching E1.3.
- `scripts/heal_systemd_install_drift.py` (~316 LOC + ~240 LOC tests, ~15 tests): every-12h sibling healer for systemd unit install drift. Born from finding the E1.3 healer's missing install.
- `scripts/scope_usage_parser.py` (~224 LOC + ~200 LOC tests, ~15 tests): log-parser scope-usage analyzer (deliberately not call-site instrumentation; defer until logs prove too fragile).
- `scripts/outbox_notifier.py` modified across PR #46 (source-routing fix), PR #48 (headless Beacon APPROVAL_REQUEST handler), PR #49 (source-routing narrowing fix). Net ~+500 LOC across the three PRs + ~+600 LOC of tests.
- `runbooks/cycle-prompt.md` additive: Pulse credential rotation check section (60-day window, 14-day DM dedup).
- `systemd/` with two new unit+timer pairs for the new healers, installed operationally post-PR-#46.
- `agents/mirror/CLAUDE.md` + `agents/beacon/CLAUDE.md` additive sections for credential-discipline enforcement and headless-dispatch path.
- `INSTALL.md` updated (healer count 8 → 10) with install-drift-pattern callout.
- Four calendar events on Beacon's agent calendar (Vercel + 3 annual scope audits), Larry invited as guest on each. Dogfoods E5.3 Beacon-owns-Google-Workspace pattern.

### Architectural findings surfaced + closed

Three manual bridges were required during the session to keep the chain moving — each surfaced a real architectural gap in headless-mode operation. **All five findings were tracked; four were closed in code; one was deferred:**

1. **Source-routing gap** — `outbox_notifier` skipped auto-merge when source was `'larry'`. Surfaced on PR #45's Mirror dispatch. **FIXED in PR #46** via the source-routing addition. Validated on PR #47.
2. **Healer install drift** — E1.3's `heal-pr-auto-merge.{service,timer}` shipped to repo but never installed on droplet. **FIXED operationally** (cp + daemon-reload + enable+start) + **systemically** via new `heal_systemd_install_drift.py` healer in PR #46.
3. **DM delivery delay** — `larry-alerts.jsonl` queue → Telegram DM hop ran ~8 min. **Noted, deferred** to E6 polish.
4. **Headless Beacon APPROVAL_REQUEST handler** — Beacon's APPROVAL_REQUEST marker had no headless handler. Required a manual bridge during PR #46's chain (extract marker payload, write Forge preflight envelope directly). **CLOSED in PR #48 task #17** via `_handle_beacon_headless_approval_request`.
5. **PR #46 source-routing fix over-broad interception** — PR #46's fix intercepted ALL `source='larry'` markers (including Forge's PROCEED), DM'ing wrong template + skipping `_dispatch_build_phase`. Surfaced on task #17 dispatch (required a fifth manual bridge for build phase). **CLOSED in PR #49 task #19** via narrowing precondition + template fix.

### Verification (operational, on droplet)

- `python3 scripts/validate_token_rotation_schedule.py config/token-rotation-schedule.json` exits 0 (10 entries; all fields populated; all runbook_path refs resolve).
- Both new healers active: `systemctl is-active ourliberty-heal-credential-registry-drift.timer ourliberty-heal-systemd-install-drift.timer` → `active active`.
- Credential drift healer first real run reported `MISSING_REGISTRY_ENTRY:TELEGRAM_CHAT_ID_LARRY` + `MISSING_REGISTRY_ENTRY:TELEGRAM_ALLOWED_CHAT_IDS` — design oversight from PR #45. **The discipline caught its own designer's gap on first run.** Fixed in PR #47.
- After PR #47: drift healer re-run logged `reconciled_gc=2`, state file `{"drifts": {}}`. **Zero drift.**
- E1.3 healer first real activation fired during this session via a synthetic AUTO_MERGE-failure log line targeting PR #45. Activation DM landed on Telegram; Larry ran `systemctl edit` + `restart`; healer re-ran in live mode and merged PR #45 at 23:56:27Z. **First real activation of the E1.3 discoverability pattern.**
- Source-routing fix validated end-to-end on PR #47 (`source='larry'` Mirror dispatch → REVIEW_PASS → notifier routed correctly + auto-merge fired).
- PR #49 narrowing fix validated end-to-end (task #19's own chain ran fully autonomously after the manual build-phase bridge; subsequent dispatches on freshly-restarted notifier should be clean).

### Total spend + scope

- **5 PRs merged** (#45 #46 #47 #48 #49). All auto-merged via the chain (with manual bridges where the headless-mode gaps surfaced).
- **+~5000 LOC across the session** (#45 +448, #46 +3768, #47 +34, #48 ~+200, #49 ~+550).
- **Test suite: 733 → ~1050+** (~+110 in #46 + ~+8 in #48 + ~+30 in #49). All green throughout.
- **Total LLM spend: ~$25** across all dispatches.
- **Wall clock: ~6.5 hours** of focused engagement (interspersed with async build/review chains running in background).

### Codified additions worth recalling next session

14. **Three-to-five manual bridges per Claude-driven headless cycle was the friction discovered today.** Pattern documented in `feedback_headless_mode_chain_gaps` memory. After PR #46 + #48 + #49, the most common bridge paths are now closed; future headless cycles should run autonomously unless a new inter-agent handoff is introduced.
15. **The "build complete" instinct prevented a thin design** — Larry's choice of (B) usage-analysis over (A) just-list-scopes added implementation work but produced the durable shape. Annual scope audits will surface real drop-candidates with usage data, not just "review your scopes."
16. **The drift healer caught its designer's oversight on first run** — discipline working as designed. If a fail-closed healer doesn't fire on day-one, the registry isn't covering enough.
17. **Headless mode requires explicit handler entries for every inter-agent handoff** — agent OS was designed assuming Telegram bot interception. Claude-driven sessions are a second mode that needs first-class handler support. Whenever a NEW inter-agent handoff is added, also add the headless-mode handler.
18. **Calendar events as the visible reminder layer** — the system's discipline (registry + healer) is invisible to the operator. Beacon-owned calendar events on the operator's personal calendar are the visible interface. Pattern works.
19. **Bug fixes can introduce regressions even with tests** — PR #46's source-routing fix closed finding #1 but introduced finding #5. PR #49 narrowed it. The fix-introducing-a-regression shape is worth watching for; the dogfood-as-test pattern surfaced it within minutes of dispatch.

### Next

**E2.1 — `config/deploy_targets.json` schema design**. Resumption prompt for the next session is at `docs/next-phase-prompt-e2-1.md` (self-contained for a fresh Claude Code session).

---

## 2026-05-20 — E2.1 + E2.2 + 4 followups + Mirror regression gate (7 PRs, $44, one day)

The full deploy layer scaffolded plus four follow-on hardening PRs plus a Mirror gate strictening, all merged in a single working day via the headless agent OS chain. The day after PR #48 + #49 + the discipline lockdown of 2026-05-19, the chain delivered on its promise: 7 PRs in ~2 hours of chain-active time with one identified routing gap surfaced + closed inside the same session.

### What shipped

- **PR #51 — E2.1 deploy_targets registry + validator + 12h sync drift detector.** First task to run cleanly end-to-end through the post-#48/#49 headless chain. 19 min, $8.72. Per-target schema with `vercel_project_id`, `vercel_org_id` (nullable for personal accounts), `framework`, `env_var_keys`, `branch_filter`. Empty registry is a valid state — first real entry lands in E2.3.
- **PR #52 — E2.2 deploy_notifier.** 2-min systemd-oneshot poller for Vercel deployments; DM on READY + ERROR; PR-number resolution via `meta.githubPrId` → `gh pr list --head` fallback. 16 min, $6.87. Bakes the `OURLIBERTY_AGENTS_ROOT` env-var override pattern in from the start.
- **PR #53 — AGENTS_ROOT path-isolation fix (5 scripts).** Closes the bug where Forge's worktree pytest runs polluted prod `/home/larry/agents/logs/*.log` files (observed during E2.1's own build phase). 5 healer/sync scripts + 5 test files (`setUpModule` + `tempfile.mkdtemp` + `importlib.reload` per file, since tests are unittest, not pytest). 26 min, $8.32 — chain-routing chaos added ~$3-4 of overhead but the work landed.
- **PR #54 — Stale time-bound tests in heal_pr_auto_merge.** Three tests used literal `[2026-05-19T...]` timestamps that aged out of the 24h lookback window. Replaced with `datetime.now(timezone.utc) - timedelta(...)`. 17 min, ~$3.10. Pre-existing failures since ~2026-05-13.
- **PR #55 — Chain gap #5: headless clarification-response routing.** When Forge emits CLARIFY_REQUEST in headless mode, the response now resumes the ORIGINAL task session via `--resume` instead of cascading into a doubled-prefix branch (`forge/notify-notify-task-X-...`) and depth-multiplying awareness notifies. ~$8.50, 31 min — the chain bug fixed in this PR ALSO afflicted this PR's own dispatch (Forge's preflight CLARIFY_REQUEST about pytest-vs-unittest test framework choice fired the cascade live). Chain self-recovered via Forge's intelligence layer; real code landed on the wrong-named branch (`forge/notify-notify-task-22-...`); cosmetic branch name, correct merged code.
- **PR #56 — outbox_notifier path-isolation.** Extends PR #53 to the central daemon. Missed in PR #53 because the original grep used `AGENTS_ROOT *=` and outbox_notifier.py uses `AGENTS_ROOT = HOME / 'agents'` (no `Path(` literal). 33 min, ~$4.50.
- **PR #57 — Mirror regression gate (dial 3).** New `scripts/test_regression_check.py` helper runs the suite at both parent + head SHAs via git worktrees, diffs the failure sets, emits JSON verdict (`PASS` / `BLOCK`). Mirror invokes it before every REVIEW_PASS; blocks on PR-introduced regressions but tolerates pre-existing. Pre-existing failures listed in the review body for visibility. Decided as dial 3 of 5 (regression-only) after observing that Mirror approved PRs #52 + #53 despite the 3 pre-existing failures fixed in PR #54. 12 min, ~$3.67.

### Findings codified

5. **All 5 known headless chain gaps now closed.** Gap #1 (source-routing) PR #46. Gap #2 (healer install drift) PR #46. Gap #3 (DM delivery delay) deferred to E6. Gap #4 (headless APPROVAL_REQUEST) PR #48. Gap #5 (headless clarify-response) PR #55. The "expect 3 manual bridges per chain cycle" rule from 2026-05-19's session memory is now historical — today's 7-PR run had zero manual bridges. Future manual bridges are now a regression signal: if you find yourself needing one, a 6th gap has appeared.
6. **Chain self-recovery via Forge intelligence is real.** When task-22's chain went off-rails (chain gap #5 active), Forge's `--resume` carried original session context through the cascade. Forge emitted the right marker, did the right work, opened a PR with the right title — just on a doubled-prefix branch name. The intelligence layer compensates for routing-layer bugs when the original session survives. Cost of routing chaos was ~$3-4 of overhead per cascade-affected PR; product was correct.
7. **Same-day parallel dispatch works.** Tasks 21 + 22 dispatched 5 min apart; inbox-watcher serialized Forge (one preflight at a time) but Mirror overlapped with the next Forge. Later, tasks 23/24/25 ran with up to 3 concurrent Forge instances when conditions allowed (`active=N/10` cap accommodates it). Parallel dispatch is now a real lever for velocity.
8. **Spec lesson: check test style first.** I assumed pytest fixtures for the AGENTS_ROOT fix; Forge's preflight CLARIFY_REQUEST caught that the project uses `unittest.TestCase` throughout. Future specs touching `scripts/tests/`: check the existing test style (pytest vs unittest) and write fixtures accordingly. The clarification-response cascade we lived through was the cost of this miss.
9. **Mirror's gate had a blind spot.** It approved PRs despite pre-existing test failures because its workflow was reviewing the diff for correctness against the spec, not actually running the suite. PR #57's gate closes this; future Mirror reviews invoke `test_regression_check.py` and block on regressions.
10. **The drift healer caught its designer's oversight; the regression gate catches reviewer drift.** Same shape of insight, different layer. Both confirm the build-complete instinct: discipline mechanisms work as designed only when they're checking what they should check, and the "what they should check" set grows over time.

### Verification (operational, on droplet)

- All 4 new systemd units active: `ourliberty-deploy-notifier.timer` (2-min cadence, default-off via kill-switch env var), `ourliberty-sync-deploy-targets.timer` (12h cadence, default-off), both `heal-credential-registry-drift` + `heal-systemd-install-drift` from prior session still active.
- `python3 scripts/test_regression_check.py --parent-sha HEAD~1 --head-sha HEAD --output json` against current main: `{"verdict": "PASS", "regressions": 0, "pre_existing_unaffected": 0, "fixed": 0}`.
- `python3 -m unittest scripts.tests.test_heal_pr_auto_merge`: **OK** — 37 tests, 0 failures (was 2F + 1E before PR #54).
- `python3 scripts/deploy_notifier.py --once --dry-run`: exits 0 with `no targets configured; nothing to poll` — correct empty-registry behavior.
- Both daemons (outbox-notifier + inbox-watcher) restarted at session close after PRs #55 + #56 to load the new code in memory.
- All `scripts/heal_*.py` + `scripts/sync_deploy_targets.py` + `scripts/outbox_notifier.py` now use `Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', ...))`. Future test runs in any worktree write to tmp_path-redirected state.

### Total spend + scope

- **7 PRs merged** (#51 #52 #53 #54 #55 #56 #57). All auto-merged via the chain. Zero manual bridges (one chain-routing detour on PR #53 that self-recovered to a wrong-named-branch merge).
- **+~5500 LOC across the day** (#51 +1664 / #52 +1664 / #53 +210 / #54 minimal / #55 +665 / #56 +653 / #57 +746 — some test files double-counted in #52 vs #53 above; numbers from `git log --shortstat`).
- **Test suite: ~1050 → ~1200+** with the new tests in #51/#52/#53/#55/#56/#57.
- **Total LLM spend: ~$44** across all dispatches.
- **Wall clock: ~3 hours** of session-time engagement, ~2 hours of chain-active time.

### Codified additions worth recalling next session

20. **Headless chain is fully closed end-to-end.** All 5 routing gaps fixed. Future Claude-driven dispatches should run autonomously unless a NEW inter-agent handoff is introduced. The `[[feedback_headless_mode_chain_gaps]]` memory was updated to reflect the closed status; treat manual bridges as a regression signal.
21. **Mirror's regression gate is dial 3 (regression-only) by design.** Tolerates pre-existing failures by listing them in the review body for visibility but doesn't block. Trade was framed in 1-5 dial format: catching PR-introduced breakage vs work-stream coupling cost. Dial 4+ (strict) was rejected for entangling unrelated work. Memory at `[[project_mirror_gate_posture]]`. Revisit if pre-existing failures accumulate to "boy who cried wolf" levels.
22. **Spec lesson: bake path-isolation into NEW scripts from the start.** PR #52's `deploy_notifier.py` had the env-var override pattern from day one; the followup PR #56 had to extend it to `outbox_notifier.py` post-hoc. For any new script that touches `AGENTS_ROOT`, the env-var override + autouse test fixture goes in the initial spec.
23. **Audit greps need to cover both `=` patterns.** PR #53's initial scope-grep missed `outbox_notifier.py` because it used `AGENTS_ROOT *=` and the file uses `AGENTS_ROOT = HOME / 'agents'` (no `Path(` literal). Future audits: grep for the variable name itself, not the assignment pattern.
24. **Chain-active parallelism is now a real lever.** Three concurrent Forge tasks ran during the followup-PRs round. Wall clock for 3 PRs that would have been ~3h serial dropped to ~33min concurrent. Use it when tasks touch disjoint files.

### E2.3 closure (same session, 2026-05-20)

After the 7 PRs above (E2.1 + E2.2 + 4 followups + Mirror gate) merged + cooled, E2.3 ran as the closeout walkthrough — Larry-driven for the Vercel UI step, Claude-driven for the GitHub + config-update + activation steps.

**What happened (in order):**

1. **Scaffold the dashboard repo** (Claude, ~3 min). `npx create-next-app@latest ourliberty-dashboard --typescript --tailwind --app --eslint --no-src-dir --no-turbopack --import-alias "@/*" --use-npm --yes` → `gh repo create Larry-Yatch/ourliberty-dashboard --private --source=. --push`. Initial commit live at https://github.com/Larry-Yatch/ourliberty-dashboard.
2. **Vercel UI import** (Larry, ~5 min). Visited https://vercel.com/new, imported the new repo, accepted the Next.js auto-detected framework, deployed. First production deploy completed in ~30s.
3. **Capture the project ID** (Larry). From Vercel Settings → General: `prj_b1jhpIqS8VDyZfDQvIoyzm32Rf6b`. Personal Hobby account (slug `larry-yatch`) → `vercel_org_id` is `null` per the schema.
4. **Land the deploy_targets entry** (Claude-as-Forge, ~5 min, $0.57 LLM via Mirror). Branched `larry/e2-3-add-dashboard-deploy-target`, added the entry, validator passed, opened PR #58 manually (skipping Forge preflight + build phases since this was a trivial 15-line config edit). Dispatched a Mirror review envelope directly to `~/agents/inboxes/mirror/`. Mirror reviewed under the brand-new dial-3 regression gate (verdict: PASS, 0 regressions); auto-merge fired via the source-routing path (PR #46 + #49). PR #58 merged at 16:22:20 MDT.
5. **Activate both kill-switches on droplet** (Claude, ~30 sec). Wrote `/etc/systemd/system/ourliberty-deploy-notifier.service.d/override.conf` with `OURLIBERTY_DEPLOY_NOTIFIER_ENABLED=true`; same for `ourliberty-sync-deploy-targets.service.d/override.conf` with `OURLIBERTY_DEPLOY_TARGETS_SYNC_ENABLED=true`. `daemon-reload` + `restart` on both timers.
6. **Smoke test push** (Claude, ~30 sec). Created `test/e2-3-smoke` branch on `ourliberty-dashboard`, added a sentinel paragraph to README, pushed.
7. **Watch the chain fire** (~5 min). 16:22:20 PR #58 merged. 16:24:40 next deploy-notifier tick — found the now-registered project + 2 READY deployments (the smoke-test push from 16:18 + the original main-branch deploy Vercel auto-built on import). Both deployments DM'd to `larry-alerts.jsonl` queue with `source=deploy-notifier severity=warning` and a body like `Vercel preview live\nProject: ourliberty-dashboard\nPR #(unknown)\nBranch: test/e2-3-smoke\nURL: https://ourliberty-dashboard-5xejsz81g-larry-yatch.vercel.app`. 16:29:28-29 the beacon-bot relay delivered both alerts to Larry's Telegram thread. ~5-minute queue-to-Telegram lag (faster than the ~8-min lag observed on 2026-05-19 — the DM delivery delay finding from E1.5 may be settling).

**E2.3 success criterion (from phase-e-plan):** *"A push to `ourliberty-dashboard` triggers a Vercel preview build → URL appears in Beacon Telegram thread within ~2 min of CI green."* — **MET with caveat.** Vercel build was ~30s, deploy-notifier tick caught it within 2 min, but the `larry-alerts.jsonl` queue → Telegram bot relay added another ~3 min (the DM delivery delay). Total git-push-to-Telegram was ~5 min. Acceptable for E2; tighten in E6 if the lag becomes a real signal.

**Notable observations:**

- **`PR #(unknown)` in the DM body.** The smoke-test push was a bare branch push (no PR open against `ourliberty-dashboard`), so neither `deployment.meta.githubPrId` nor the `gh pr list --head` fallback resolved a PR number. Expected behavior; the spec called this out. When E3 starts pushing through Forge's PR-opening flow, PR numbers will populate.
- **Both preview URLs rendered identical content** because the smoke-test commit only edited `README.md`, which isn't part of the Next.js render tree. Visually meaningless distinction; technically valid smoke test (two distinct deployment UIDs, two distinct DMs). Future smoke tests that want a visible diff should touch `app/page.tsx`.
- **Mirror's new regression gate fired correctly on PR #58.** Verdict PASS, 0 regressions, 0 pre-existing failures (since PR #54 cleaned those up earlier in the session). The dial-3 contract is working as designed.

**Open follow-ups (none blocking):**

- DM delivery delay still ~3-5 min; deferred to E6 per [[project_phase_e1_5_complete]].
- `deploy_targets.json` only has one entry today; sync drift detector will gain real signal once we add more projects in future sessions.
- Vercel webhook delivery confirmation is implicit (we see the build, but don't see the webhook payload itself). Sufficient for the current trust level.

### Codified additions

25. **Claude-as-Forge for trivial config edits is a real velocity lever.** PR #58 was a 15-line JSON insert; full chain dispatch (Forge preflight + build + Mirror) would have spent ~$3-5 LLM + ~12 min wall clock. Claude-as-Forge (edit + branch + commit + push + PR + dispatch Mirror review only) spent ~$0.57 + ~5 min, and Mirror's regression gate still ran as the safety net. Use this pattern whenever the change is mechanical + the spec is in your head already.
26. **Phase E2 (Deploy Layer) is fully shipped.** E2.0 (Vercel signup), E2.1 (registry + validator + sync drift), E2.2 (notifier daemon), E2.3 (first real repo wired + smoke verified) — all DONE in <48 hours of session time. Next critical-path is E3 (the actual dashboard build on top of this scaffolding).

### Next

**E3 — Dashboard B (Read-Only).** Replace the default Next.js scaffold at https://ourliberty-dashboard-*.vercel.app with a real read-only dashboard rendering droplet state (agent status, recent tasks, costs, cycle-journal, healer status). Architecture: browser → Vercel (Next.js) → HTTPS → droplet FastAPI on `:8443` → reads `~/agents/blackboard/`, `~/agents/state/`, `~/agents/logs/`, `costs.jsonl`. ~3 days estimated. Open questions to surface at E3 kickoff: auth shape (shared-secret header vs OAuth vs IP allowlist), hosting domain (`dashboard.ourliberty.dev` is the natural pick if DNS is wired), initial endpoint set, and polling cadence vs SSE/websockets.

---

## Phase E3 — Dashboard B (Shipped 2026-05-21)

`https://dashboard.ourliberty.dev` is live with read-only droplet state. End-to-end stack: browser → Vercel (Next.js, Vercel-managed TLS) → server-side Next.js route handler that proxies to → Caddy (Let's Encrypt TLS, auto-renew) on droplet → uvicorn FastAPI on `127.0.0.1:8000` → reads `~/agents/blackboard/`, `~/agents/state/`, `~/agents/logs/`, `costs.jsonl`, `runbooks/cycle-journal.md`. SWR auto-refresh every 30 s with `keepPreviousData` for graceful API-down handling.

### Three sub-phases, ~50 min of chain wall-clock, ~$25 total LLM (including ~$5 cross-repo gap recovery)

**E3.1 — Droplet FastAPI (PR #62, $7.65 chain).**
- `scripts/dashboard_api.py` exposes 7 GET endpoints: `/health`, `/agents/status` (with `bot_model` discriminator added to disambiguate systemd-bot vs inbox-watcher agents), `/tasks/recent`, `/costs/today`, `/costs/week`, `/cycle-journal/recent`, `/healers/status`.
- Constant-time `secrets.compare_digest` on `X-Dashboard-Token`; CORS allows exactly `https://dashboard.ourliberty.dev`.
- `OURLIBERTY_AGENTS_ROOT` env override mirrors the E2.2 isolation pattern so tests don't pollute prod logs.
- 50 unit tests (≥30 target exceeded). systemd `Type=simple` long-running unit. Full E1.5 4-artifact credential discipline for `DASHBOARD_API_TOKEN` (PR #63 followed up the calendar URL into the registry).

**E3.2 — Next.js dashboard UI (PR #1 in ourliberty-dashboard, $9.94 chain incl. recovery).**
- App Router + TS strict + Tailwind 4 (CSS-first `@theme inline`, no `tailwind.config.js`) + SWR.
- **Server-side proxy pattern** at `app/api/proxy/[...path]/route.ts` is load-bearing. The browser only ever hits same-origin proxy routes. `X-Dashboard-Token` is added server-side from `process.env`. This sidesteps E3.1's intentionally-narrow CORS (preview deploys at `*-pr-N.vercel.app` would otherwise fail CORS) AND keeps the token out of the client bundle. Verified: `grep -r DASHBOARD_API_TOKEN .next/static` returns zero matches.
- 4 routes (`/`, `/tasks`, `/costs`, `/healers`), 9 components, typed `lib/api.ts` SWR hook, `lib/types.ts` mirroring all 7 E3.1 response shapes, `lib/env.ts` server-side loader, 20 Vitest tests.
- `README.md` documents an SSH-tunnel local-dev workflow (`ssh -L 8000:127.0.0.1:8000 -N larry@134.209.44.80` + `.env.local`).

**E3.3 — Caddy auto-TLS + DNS + Vercel custom domain (no PR, purely operational).**
- Pivoted from the originally-planned nginx + certbot to Caddy because Caddy was already installed on the droplet (port 80 conflict on nginx install caught this) and Caddy's auto-TLS via ACME is dramatically simpler. Wrote `/etc/caddy/Caddyfile` with `api.ourliberty.dev { reverse_proxy 127.0.0.1:8000 }` + a `:80, :443 { respond "Not Found" 404 }` fallback for non-matching hostnames.
- Cloudflare DNS (DNS-only / gray-cloud on both records): `A api → 134.209.44.80`, `CNAME dashboard → cname.vercel-dns.com`. Cloudflare propagation was near-instant.
- Caddy's ACME flow provisioned the Let's Encrypt cert via HTTP-01 challenge within ~1 second of DNS resolving. Auto-renews; no certbot maintenance needed.
- Vercel: added `dashboard.ourliberty.dev` as a custom domain (Vercel-managed TLS auto-provisioned) + `DASHBOARD_API_URL=https://api.ourliberty.dev` env var (Production + Preview). Redeploy triggered to bake in the new env var.

### The cross-repo dispatch gap fan — recovery PRs #65-#68

The first-ever Forge dispatch against a non-`ourliberty-agent-core` `target_repo` (task-29 → `ourliberty-dashboard`) surfaced 8 chain gaps in sequence. The agent OS implicitly assumed `target_repo == "ourliberty-agent-core"` in at least 8 places across 4 architectural layers. Each gap only became visible after the previous was closed:

- **PR #65 — chain gap #7** (`config/agent-models.json` allowed_repos): Beacon dead-lettered the dispatch cleanly + diagnosed the gap precisely (`agent-models.json:25` and `:35`). Trust-boundary edit shipped as Claude-as-Forge.
- **PR #66 — chain gap #8** (canonical filesystem paths): `scripts/inbox_watcher.py:72` `CANONICAL_REPO_PATHS` dict + `scripts/cleanup_stale_worktrees.py:39` `CANONICAL_REPOS` list. Both files have TODOs to move the mapping to `config/agent-models.json` under `repo_paths`; deferred because the refactor would introduce new code paths needing tests. Auto-merge failed on this PR because Mirror's marker hallucinated `lyatch-ourliberty` as the org name (one-off); manual merge unblocked it.
- **PR #67 — chain gaps #9-#13** (T0 tier classification): `shared/REPO-GUARDRAILS.md` T0 table, `agents/mirror/CLAUDE.md` tier list, `agents/forge/CLAUDE.md` tier list (line 27) + allowed_repos statement (line 125), `agents/forge/TOOLS.md` repo authority table, and the critical `scripts/heal_pr_auto_merge.py:77` `REPOS` list. **Without the REPOS update**, Mirror PASS on a dashboard PR would silently never auto-merge.
- **PR #68 — chain gap #14** (systemd ReadWritePaths sandbox): `systemd/ourliberty-inbox-watcher.service` + `systemd/ourliberty-outbox-notifier.service` `ReadWritePaths` allowlist excluded `/home/larry/ourliberty-dashboard`. `git worktree add` hit "fatal: Read-only file system" inside the systemd namespace.

After all 4 PRs landed, the droplet pulled main, the watcher + outbox-notifier were restarted to reload cached config, and the quarantined task-29 envelope was moved from `~/agents/inboxes/forge/.invalid/` back to the active inbox. Forge picked it up cleanly, built E3.2 in 16.75 min, opened PR #1 on ourliberty-dashboard, Mirror reviewed clean, auto-merge fired.

### Notable observations

- **Mirror's first-pass marker-error pattern.** PRs #63, #64, #65, #66, #68 all had Mirror's first review attempt emit `REVIEW_PASS` without `===` delimiters; the parser couldn't extract JSON. All self-recovered on second-pass retry. Cost: ~$0.30 wasted per first attempt. Pattern is consistent enough to be worth investigating — probably needs a CLAUDE.md emphasis tweak for short-PR contexts. Not blocking.
- **Caddy >> nginx for this use case.** Already-installed + auto-TLS-via-ACME meant E3.3 went from "~½ day Larry-driven" to "~10 min Claude-driven." If we'd known up front Caddy was installed, we'd have written E3.3 as a 1-line Caddyfile addition from the start. Lesson: always inventory the droplet before spec'ing infra.
- **Forge handled Next 16 + Tailwind 4 unfamiliarity cleanly.** This repo's `AGENTS.md` flagged the training-data gap up front; Forge read `node_modules/next/dist/docs/` during preflight and emitted clean code (single SWR v2 quirk caught at build time: `dataUpdatedAt` not exposed → wrote a small `useDataUpdatedAt(data)` hook). Zero CLARIFY_REQUESTs.
- **The token never entered Claude's chat context during the runtime install.** Generated on droplet via `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` piped directly into `.env.larry`; only the byte-length echoed for verification. Then sourced server-side for the smoke test. (Larry pasted it into Vercel manually via Chrome MCP — the screenshot incidentally captured the value, which prompted a transparency note in chat. Acceptable exposure given the token isn't yet load-bearing and rotation is one runbook step away.)

### Codified additions

27. **First cross-repo dispatch surfaces a gap fan.** When adding a new `target_repo` to the agent OS, pre-stage all 8 gap fixes in ONE bundled PR before any task-N dispatch attempt: `agent-models.json` allowed_repos (Forge + Mirror), `inbox_watcher.py` `CANONICAL_REPO_PATHS`, `cleanup_stale_worktrees.py` `CANONICAL_REPOS`, `REPO-GUARDRAILS.md` T0 row, both agent CLAUDE.mds + Forge TOOLS.md tier lists, `heal_pr_auto_merge.py` `REPOS`, and both watcher + notifier systemd `ReadWritePaths`. Then operationally: clone the repo at the canonical path on the droplet, pull on droplet post-merge, `sudo cp` updated units to `/etc/systemd/system/`, `daemon-reload`, restart both services. Doing this as a single bundled PR (vs. discovering gaps one-at-a-time) cuts recovery cost from ~$5 + 25-30 min to ~$1.50 + ~10 min for the next repo.
28. **Caddy is the default reverse-proxy on this droplet.** Don't install nginx. Caddy's auto-TLS-via-ACME makes new HTTPS subdomains a 3-line Caddyfile change + Cloudflare DNS record. Add new `api.*` style subdomains by appending a block to `/etc/caddy/Caddyfile` and `sudo systemctl reload caddy`.
29. **Phase E (Spec → Deployed Prototype) is fully shipped.** E1 + E1.5 + E2 + E3 + E5 all DONE across 2026-05-19 → 2026-05-21. The agent OS can now: dispatch a spec, build the code, review it, auto-merge it, deploy a preview, AND show real-time state in a public dashboard. Critical-path Phase E goals all met. Phase E4 (interactive dashboard) is the next critical-path item but trigger-gated on ≥1 week of E3 usage so the "I wish I could do X from here" list accumulates.

### Next

**Pause for usage.** No urgent next-phase work. Dashboard at `https://dashboard.ourliberty.dev` is live; spend a week using it. Things to watch for that would form E4's scope: when you find yourself opening a terminal to do something you wish you could click from the dashboard, jot it down (a `runbooks/e4-wish-list.md` or just a note). E4 dispatches when that list has 3-5 concrete items.

Optional small follow-ups (none blocking, dispatch when convenient):
- Move `CANONICAL_REPO_PATHS` to `config/agent-models.json` under `repo_paths` (TODO in both `inbox_watcher.py:70-71` and `cleanup_stale_worktrees.py:36-38`). Trigger: 3rd repo addition.
- Investigate Mirror's first-pass marker-error pattern on short PRs.

---

## Phase E4 — Unified PM Dashboard (in progress, started 2026-05-24)

Phase E4 reshaped from the original "interactive dashboard" sketch into a unified PM surface that hosts both Larry's personal projects (migrated from Marvin Mission Control) AND the agent OS's build initiatives in the same Programs > Projects > Tasks model, backed by Supabase as the system's first persistent DB primitive.

**Trigger:** ~6 hours of real E3 dashboard usage on 2026-05-21 surfaced the Telegram-as-database antipattern (chat conflating state-surface + comms-channel roles). Larry's adjacent need for unified personal PM (currently in Marvin Mission Control) closed the design loop.

### Design rounds (all 2026-05-24)

- **Round 1**: project entity (defined, not inferred), backend (hybrid droplet+Next.js), default Program for agent-OS work ("Agent OS Development"), type discriminator enum (`personal | agent_os_build | client | research`).
- **Round 2**: Plan-First sections (7 of Joe's 12), skip rule (trivial Claude-as-Forge only), deploy preview URL routing (Telegram-primary), active-project pinning (sticky per-chat).
- **Round 3**: dashboard landing (Programs grid + horizontal tabs), default project view (kanban by task status), first-of-day digest format, MC parallel-run length (≥1 week).
- **Round 4**: Direct UI CRUD locked into E4.4 scope (surfaced gap during E4.2 dispatch planning — Larry asked "do we have a system for creating new projects/tasks via the dashboard like Mission Control?").
- **Round 5**: E4.4 reordered before E4.3 (UI CRUD removed the dependency on `pm_writer`); E4.4 split into 3 sub-sub-phases (a/b/c) to fit within Forge dispatch reliability zone.

### Sub-phases shipped (same day, 2026-05-24)

Cost rollup: **~$60 LLM total** across ~14 PRs + 2 hotfix dispatches + 1 calibration bundle + 1 emergency memory-cap bump.

**E4.0 — Supabase activation** ($5.50 + Larry-actions ~25 min Chrome-MCP):
- PR #78 (agent-core): Supabase credential discipline — registry entries (URL + anon + service-role), `rotate-supabase-keys.md` expanded from stub, new `setup-supabase-pm-project.md` runbook, `systemd/INSTALL.md` Supabase Python section.
- PR #2 (ourliberty-dashboard): `@supabase/supabase-js` install, `lib/supabase-server.ts` server-side admin client helper, env loader extension, `.env.local.example` updates.
- Larry walked through Supabase project creation via Chrome MCP: project `ourliberty-pm-dashboard` in Our Liberty org under Larry-Yatch GitHub identity (Supabase has no Google SSO), us-east-1 region, Free tier, "Automatically expose new tables" UNCHECKED for defense-in-depth.
- 3 spec deviations bundled into calibration PR #84: GitHub ownership instead of agent.beacon.ourliberty Google, Supabase legacy JWT API keys (not new `sb_*` format), `.env.larry` slot drift (runbook claimed slots existed; they didn't — live-patched with an idempotent bootstrap script that appends missing slots).

**E4.1 — Schema v1 + 0002 GRANT + 0003 external_id** ($5 + 3× `supabase db push`):
- PR #3 (ourliberty-dashboard): 5 tables (programs/projects/tasks/events/decisions) + indexes + triggers + RLS-enabled-zero-policies + 6 seed Programs + GHA `supabase db lint` workflow + `supabase/SCHEMA.md`. **Note for future schemas: Postgres 17 is the Supabase default in 2026 (not 15 as the original spec assumed).**
- PR #4 (dashboard, $0.50 hotfix): `0002_grant_service_role.sql` — `GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role` + `ALTER DEFAULT PRIVILEGES`. Surfaced when post-push validation returned `42501: permission denied for table programs`. The "Automatically expose new tables: UNCHECKED" toggle blocks **all** Data API roles including service_role — not just `anon`/`authenticated` as the UI label suggests.
- PR #5 (dashboard, $0.50 hotfix): `0003_add_external_id_to_programs_projects.sql` — `external_id` was only on `tasks` in 0001 but E4.2 needed it on programs + projects for idempotency. Spec/schema inconsistency was Claude's error in the original E4.1 spec.
- PR #95 (agent-core): live-state note added to E4.1 spec acknowledging 0002 + 0003 are now in prod (so future Forge dispatches read the spec as authoritative-for-current-state, not just-as-shipped).

**E4.2 — Mission Control migration script** ($11 + ~$5 wasted on failed Forge attempts):
- task-34 first dispatch (agent-core): Forge preflight REJECTed correctly — caught the missing `external_id` columns above.
- task-34 retry got stuck: chain dedup blocked round-2 clarification-response because round-1 had already been answered earlier.
- Pivot: dispatched as `task-34b` with fresh task_id; merged as PR #96.
- `scripts/migrate_mission_control.py` (~250 LOC) + tests + runbook `docs/runbooks/migrate-mission-control.md`.
- Live migration applied: 1 new program ("The 'Thing'") + 28 projects + 13 tasks inserted into Supabase from Marvin Mission Control via Tailscale-HTTP-fetched JSON files (`/tmp/mc-export/` on Larry's Mac via `curl http://100.124.212.15:3002/api/{programs,projects,assignees}`).
- One UX wart: strict case-insensitive exact name match treated MC's "The 'Thing'" (curly quotes) and the 0001 seed "The Thing" (no quotes) as different programs → duplicate. Manually cleaned up via `DELETE FROM programs WHERE name = 'The Thing' AND external_id IS NULL` + `UPDATE programs SET name = 'The Thing' WHERE external_id = 'prog-002'`.

**E4 overview spec rounds 4 + 5** (PRs #90 + #98):
- PR #90 ($0.30 chain): added "Direct UI CRUD" subsection to overview § 5.4 locking + New Program/Project/Task/Decision buttons, inline-edit on fields, delete via overflow as REQUIRED in E4.4 scope.
- PR #98 ($0.50 chain, round-2 after Mirror REVIEW_REVISION): new E4.4 sub-spec at `agents/beacon/specs/e4-4-dashboard-ui-rebuild.md` + overview reorder (E4.4 before E4.3) + sub-sub-phase split (a/b/c). Mirror caught an orphan-bullet bug (the old E4.4 detail bullets were left under E4.3 after the section was reorganized) → 13-line cleanup commit, auto-merged.

**E4.4a — MVP read-only dashboard** (PR #6 in ourliberty-dashboard, **$11.08** — well over $6 estimate):
- 15 files: 5 page routes (`/`, `/programs/[id]`, `/projects/[id]`, `/tasks/[id]`, comms-inbox stub), 4 GET Route Handlers (`/api/pm/{programs,projects,tasks,events}`), 5 components (`<ProgramCard>`, `<ProjectListRow>`, `<TaskListRow>`, `<EventLogRow>`, `<TaskTimeline>`), `lib/types.ts` extension, `lib/pm-queries.ts`.
- Larry's real Mission Control data now visible at `dashboard.ourliberty.dev`: Programs grid → click into Program → Projects list → click into Project → Tasks list → click into Task → Events timeline.
- Existing E3 features (agent runtime cards / costs / healers) preserved — `app/api/proxy/[...path]/route.ts` untouched.
- Cost overrun ($11 vs $6) attributable to Next.js + Supabase TypeScript scaffolding being heavier than spec-estimated. Future estimates should account for ~1.5-2× the LOC count in tokens.

### Calibration + ops PRs (also same day)

| PR | Scope | Cost |
|---|---|---|
| #71 (morning) | inbox-watcher MemoryMax 2G→4G after watchdog warning | $0.30 |
| #84 | E4.0 post-setup calibration (GitHub ownership, legacy keys, slot drift) | $0.30 |
| #95 | E4.1 spec live-state note (0002 + 0003 in prod) | $0.30 |
| #97 | Mirror discipline tightening (marker.task_id-match, paste-stdout) + Supabase GRANT note in E4.0 spec | $0.30 |
| #99 (evening) | inbox-watcher MemoryMax 4G→8G after second watchdog CRITICAL alert | $0.30 |

### Mirror discipline gaps surfaced

Three patterns emerged across today's ~10 Mirror reviews:

1. **`task_id` in marker payload doesn't match envelope's `task_id`** — 3 occurrences (PR #5, PR #89 retry, PR #95). Notifier dead-letters with `MalformedMirrorMarker`; retry budget catches it (~$0.30 wasted per first attempt). Codified in `agents/mirror/CLAUDE.md` via PR #97.
2. **marker.py CLI invoked but stdout NOT pasted into response** — 1 occurrence (PR #89 first attempt). `output_tokens: 22` with meta-message "Marker already emitted; monitor timeout is moot" — but no actual marker block in response text. Notifier sees nothing; auto-merge never fires. Codified in `agents/mirror/CLAUDE.md` via PR #97.
3. **Cross-repo Mirror reviews require `source='beacon'`, not `source='larry'`** — 1 occurrence (dashboard PR #4 first attempt). `IDENTITY_MISMATCH: expected=mirror loaded=none` because the dashboard repo's `CLAUDE.md` is a Next.js project doc, not Mirror persona. Memory updated at `feedback_headless_mode_chain_gaps.md`; `source='larry'` strict-checks target repo CLAUDE.md, `source='beacon'` skips that check.

### Forge discipline gaps still pending (deferred dispatch)

Sibling issues to Mirror's that haven't been fixed via CLAUDE.md tightening yet:

1. **Preflight marker error on most short dispatches** — Forge invokes marker.py for PROCEED/CLARIFY/REJECT but stops short of pasting stdout. Notifier marker-errors; retry typically succeeds. Same shape as Mirror gap #2 above.
2. **IDENTITY_MISMATCH on re-dispatched envelopes** — surfaced when re-dropping a previously-dispatched envelope. Forge's session loaded Beacon's CLAUDE.md instead of its own (possibly stale state from prior session resume). Workaround: dispatch with fresh task_id (suffix like `-b` or `-take2`).
3. **Chain dedup blocking valid round-2 clarifies** — once Beacon answers a clarify, re-dispatching the same task hits `clarification-response continuation already dispatched for task X round 1; skipping duplicate write` and refuses to send a new round-2 response. Forced the `task-34` → `task-34b` workaround in E4.2.

Bundle for a future dispatch: "`agents/forge/CLAUDE.md` tightening + chain dedup gap closure."

### Child-process leak (real issue, deferred)

Inbox-watcher cgroup MemoryMax bumped twice today at doubling cadence — 2G→4G in morning (PR #71), 4G→8G at night (PR #99). Parent process RSS ~17 MB; children consume up to ~4.3 GB while only one Claude subprocess is actively running. The rest is accumulated dead-process memory not being garbage-collected.

Likely suspects: ProcessGroup not killed on parent exit, lingering pipes/file descriptors holding allocations, Node.js/Bun subprocesses spawned by Claude tool-use not being reaped. Real fix is engineering work (~1-2h with a healer-style dispatch examining ProcessGroup management + cgroup quota per-subprocess + reaper script for orphans). Deferred to a dedicated dispatch.

8G should last through E4.4b + E4.4c without watchdog firing again. If it bumps again before the leak is investigated, that's the trigger to prioritize the dispatch.

### Chain bugs observed (not fixed; flagged)

- **0-byte WIP placeholder PRs getting auto-merged** (PR #94 today): the watcher pushes an empty checkpoint commit when setting up a worktree branch; somehow it got promoted to a real PR and auto-merged. Zero-files changed, low blast radius, but the chain shouldn't open or merge an empty PR.
- **Mirror's 8-min cost for short docs PRs** (PR #95 was a 3-LOC diff and cost $1.33 / 8 min): Mirror's reading the whole spec context not just the diff. For trivial docs PRs this is wasteful. Possible CLAUDE.md tweak: "for docs-only diffs under 20 LOC, focus the review on the diff itself + immediate surrounding context, not the full document."

### What ships next

**E4.4b** (kanban + drag-drop) is the next dispatch per the E4.4 sub-spec plan. After Larry validates E4.4a's render quality + data fidelity, dispatch E4.4b. Then **E4.4c** (CRUD + forms). Then **E4.3** (`pm_writer` + Beacon CLAUDE.md updates). Then **E4.5** (Mission Control decommission) after ≥1 week of new-dashboard usage.

### Codified additions worth recalling next session

30. **`Automatically expose new tables` UNCHECKED also blocks `service_role` auto-grants on new tables.** Supabase's UI label suggests it only affects `anon`/`authenticated`, but the toggle is broader. Every schema migration that creates tables MUST include `GRANT ALL ... TO service_role` + `ALTER DEFAULT PRIVILEGES` (or a hotfix migration must follow). Codified in E4.0 sub-spec § 6.
31. **Forward-only migration discipline.** Don't edit shipped migrations (0001, 0002, etc.); patch via new migrations (0003, 0004, etc.). Specs that document the schema add a "live-state note" at the top of their SQL block pointing at follow-up migrations (E4.1 spec § 4.3 pattern).
32. **Cross-repo Mirror reviews require `source='beacon'`.** `source='larry'` triggers strict identity-assertion against the target repo's CLAUDE.md, which fails for any repo that isn't agent-core (their CLAUDE.md is project-specific, not agent persona). Memory updated.
33. **marker.py CLI is necessary BUT NOT SUFFICIENT.** Mirror + Forge must PASTE the stdout into their response text — the notifier parses the response, not Bash's stdout history. Codified in Mirror CLAUDE.md via PR #97; Forge's CLAUDE.md still pending.
34. **Marker payload `task_id` MUST match envelope `task_id` EXACTLY, including on retries.** Mismatch dead-letters with `MalformedMirrorMarker`. Codified.
35. **Memory cap bumps are a stopgap; investigate child-process leak.** Inbox-watcher cgroup bumped from 2G to 8G in one day at doubling cadence. Real fix is process-group / reaper investigation. Deferred dispatch.
36. **Re-dispatching after a REJECT requires a fresh `task_id`.** Chain dedup blocks otherwise; even valid round-2 clarification-responses get refused. Use suffix like `-b` or `-take2`.
37. **Supabase free-tier `Postgres 17` is the 2026 default** (not 15 as the original E4.1 spec assumed). Migration SQL is portable from 11+ so 0001 worked, but worth noting for future schema specs.
38. **Phase E now ships a real persistent data layer.** The agent OS has Supabase as a system primitive. Future per-product Supabases (TruPath, AI Co, etc.) follow this template — one project per product, owned by that product's billing identity, full E1.5 4-artifact credential discipline. Templates exist at `docs/runbooks/setup-supabase-pm-project.md` (initial setup) and `docs/runbooks/rotate-supabase-keys.md` (rotation).

## Phase E4 followup — PR #101 Mirror review hang: chain-discipline + parser fix (2026-05-25)

Closes a two-bug interaction surfaced when Mirror reviewed PR #101 (the pulse-check-i marker-error counter fix). Mirror approved the diff in her session output but the auto-merge never fired; her session stayed alive 71 min until Larry manually killed it and merged the PR by hand. The session cost ~$1.62 of LLM time waiting on a self-matching `pgrep` loop.

### Symptom

Mirror started reviewing PR #101 at 19:23 UTC, ran the regression gate, emitted REVIEW_PASS at 19:25 UTC, and then — instead of letting her session exit naturally — invented a backgrounded poll loop to "wait until the regression check finishes" *after* the check had already returned synchronously and *after* she had already emitted REVIEW_PASS. The loop never exited; her session stayed billable for 71 min until manually killed. When agent_runner subsequently woke her after the kill, she emitted a new final turn — *"Acknowledged — moot now"* — which became the outbox `result`. Auto-merge never fired despite REVIEW_PASS being on disk in her session log. PR #101 was manually merged via commit `f4dee2b`.

### Root cause #1 — self-matching `pgrep -f`

Mirror invented:

```bash
python3 scripts/test_regression_check.py ... &
until [ -f /tmp/regression-done ] || ! kill -0 $(pgrep -f test_regression_check.py | head -1); do sleep 3; done
```

`pgrep -f` matches the loop's own shell process via argv — the bash command line contains the literal pattern string `test_regression_check.py`, so `pgrep` returns the poll loop's own PID, `kill -0` always succeeds, and the loop never exits. Two structural fixes against repeating it:

1. **agents/mirror/CLAUDE.md** had the canonical regression-gate command + foreground-only instruction since dial-3 shipped (PR #87, 2026-05-20), but the self-matching `pgrep` warning was missing until hotfix `0fc8f92` (this same incident's first patch, also 2026-05-25). The fix added warning paragraphs at L227-L228 of that doc.
2. **scripts/test_regression_check.py** now prints a foreground-only WARNING to stderr at startup (this PR). Any future agent invoking the script — Mirror, a healer, a manual debug session — sees the warning regardless of whether their CLAUDE.md was up-to-date when their session loaded.

### Root cause #2 — marker parser blind to earlier turns

The outbox notifier's `_classify_mirror_marker` parsed only the final `claude -p` assistant turn from the outbox `result` field. When agent_runner woke Mirror after the kill and she emitted *"Acknowledged — moot now"* as the new final turn, that text overwrote the `result`. The existing multi-turn recovery (PR #80) was structured as a fallback: only fire if final-turn parsing returned None. *"Acknowledged — moot now"* contains no marker delimiters so it parsed as None — recovery should have fired and found her earlier REVIEW_PASS. The exact failure-mode classification (whether recovery did fire and find nothing, or fired and matched stale text, or was somehow gated) remains ambiguous from logs alone, but the structural fix is the same regardless:

The classifier now **always scans the session log first** and picks the LATEST assistant turn that parses as a valid marker. The outbox `result` is only consulted as a fallback when the session log is unavailable (no `claude_session_id`, missing file, no marker found in any turn). Applied symmetrically to `_classify_forge_marker` so PROCEED / CLARIFY_REQUEST / REJECT markers don't suffer the same failure mode.

The shared helper `_scan_session_log_for_latest_marker_text(session_id, parser, skip_exceptions)` is parser-agnostic; Mirror and Forge each get a thin wrapper binding it to their parser.

### Discipline addition — post-marker exit

Added `## Post-marker exit discipline` sections to `agents/forge/CLAUDE.md` + `agents/pulse/CLAUDE.md`, modeled on Mirror's L225-L230 regression-gate paragraphs. The discipline generalizes Mirror's regression-gate incident to ANY terminal marker (or terminal preamble): after emitting it, stop the session — don't spawn `&`-backgrounded work, don't write self-matching `pgrep -f` poll loops. The parser fix above closes one failure mode (later assistant turns no longer mask the marker), but the cost-amplification mode (a 71-min billable session waiting on a hung loop) is a CLAUDE.md discipline question, not a code bug.

### What shipped

- **scripts/outbox_notifier.py:** marker classification now always-scan-latest-wins across all assistant turns; `result_text` is a fallback path. Applies to both Mirror and Forge classifiers. New helper `_scan_session_log_for_latest_marker_text` (parser-agnostic); thin per-agent wrappers retained.
- **scripts/test_regression_check.py:** foreground-only WARNING printed to stderr at every invocation.
- **agents/forge/CLAUDE.md + agents/pulse/CLAUDE.md:** new "Post-marker exit discipline" sections.
- **scripts/tests/test_outbox_notifier.py:** 3 new tests covering Mirror's always-scan-latest-wins behavior + the existing `test_happy_path_does_not_consult_session_log` updated to assert the inverted precedence.

### Verification

Full `scripts/tests/` suite green post-change. The existing PR #80 multi-turn-recovery tests remain green because they exercise the same scan-the-log-and-pick-latest behavior — only the precedence ordering changed. The previously-named `test_happy_path_does_not_consult_session_log` was inverted to `test_happy_path_consults_session_log_first` to reflect the new contract.

### Recovery cost

$1.62 burned on the hung Mirror session; PR #101 manually merged via `f4dee2b` after Larry confirmed Mirror's REVIEW_PASS by reading the session JSONL directly.

### Codified additions worth recalling next session

39. **Marker classification is always-scan-latest-wins across all assistant turns** (chain-discipline-marker-parser-and-regression-check-001, 2026-05-25). The outbox `result` field is only the final `claude -p` turn; the session JSONL is the authoritative source. Mirror and Forge classifiers both use the session-log scan as the primary path now, with `result_text` as a fallback for when the log is unavailable. Earlier `parse_*_marker` returning None on a final-turn text no longer suppresses a valid earlier marker.
40. **`pgrep -f <pat>` self-matches its caller's argv.** The bash command line that runs `pgrep -f foo` contains the literal string `foo`, so `pgrep` returns the caller's own PID, `kill -0` succeeds forever, and any "wait until the child exits" poll loop hangs. Workaround when you genuinely need to poll a sibling process: `pgrep -f '[f]oo'` — the brackets are a one-char character class; the literal pattern in the caller's argv contains the brackets and won't match the bracketless regex. Default: don't poll; run subprocesses in the foreground.

## Phase E4 followup — page-cache investigation (Pulse off-cycle dispatch, 2026-05-25)

A misdiagnosed "memory leak" that turned out to be reclaimable page cache. Resolved via one code change (PR #102) plus one manual systemctl tweak. Worth recording because the bump-the-cap reflex would have masked the actual cause indefinitely.

### Symptom

The `ourliberty-inbox-watcher.service` MemoryMax setting was bumped twice on 2026-05-24: 2G → 4G in the morning (PR #71), 4G → 8G in the evening (PR #99). Both triggered by watchdog DMs as the cgroup approached the cap. Parent watcher process RSS was healthy (~17 MB); children consumed up to ~4.3 GB while only one Claude subprocess was actively running. The remaining was "accumulated dead-process memory" — or so the symptom suggested.

### Root cause

Pulse off-cycle investigation (Sonnet 4.6, $1.68) traced 82% of cgroup memory (3.26 GB of 3.97 GB) to **kernel page cache from large `node_modules` directories** in three dashboard worktrees (~720 MB each, 12-19 h old at sample time). Linux cgroup v2 attributes page cache to the cgroup that brought the page in. When Forge/Mirror ran `pnpm typecheck/build` in those worktrees, the kernel cached the read pages and accounted them to the watcher's cgroup. `memory.events.max=868` confirmed the kernel was reclaiming but new file reads refilled faster than the soft pressure could keep up.

Ruled out with evidence:
- Anonymous RSS leak: `memory_stat.anon=354MB` (healthy for 2 active Claude processes)
- Zombie processes: `zombie_processes=0`
- Orphan-process retention: KillMode=control-group already set; PrivateTmp=true already set
- Pipe-buffer growth: no swap, no kernel-side spillover

### What shipped

- **Fix A (PR #102):** `scripts/cleanup_stale_worktrees.py:83` — `MAX_AGE_SECONDS = 86400 → 14400` (24h → 4h). Tighter retention removes idle dashboard worktrees within 4h, releasing the page cache they were caching. In-flight detection (`load_active_task_stems`) already guards active worktrees, so the tighter mtime gate cannot collateral-remove anything in use.
- **Fix B (manual systemctl, deployed 2026-05-25 by Larry):** `sudo systemctl set-property ourliberty-inbox-watcher.service MemoryHigh=3221225472` (3 GB). Creates a new persistent drop-in at `/etc/systemd/system.control/ourliberty-inbox-watcher.service.d/50-MemoryHigh.conf`. The `MemoryHigh` setting triggers async background reclaim under soft pressure rather than waiting for synchronous direct-reclaim at the `MemoryMax` hard cap. `memory.current` dropped 3.67 GB → 3.12 GB within seconds of the property taking effect.
- `MemoryMax=8G` stayed unchanged pending ≥1 day of metrics under the new MemoryHigh-bounded regime.

### Codified additions worth recalling next session

41. **Memory pressure is not always a leak.** Before reaching for `systemctl set-property MemoryMax=<higher>`, decompose `memory.stat`: `anon` is application heap (real leak signal); `file` is page cache (reclaimable, usually not the problem); `slab_reclaimable` + `slab_unreclaimable` are kernel structures. If `file >> anon`, the cgroup is caching reads, not leaking. Look at WHAT'S being read (large node_modules trees, big data files), not at the parent process.
42. **`MemoryHigh` is the right escalation step before `MemoryMax`.** `MemoryMax` is a hard kill ceiling. `MemoryHigh` is a soft-pressure threshold that triggers async background reclaim. When the cgroup is mostly page cache (per #41), MemoryHigh forces the kernel to keep reclaiming proactively instead of waiting for a hard miss. `systemctl set-property <unit> MemoryHigh=<bytes>` is the live-tuning path; it creates a `50-MemoryHigh.conf` drop-in automatically.
43. **`systemctl set-property` for live cgroup quota changes** persists across reboots via the auto-generated drop-in file. Larry's pattern: bump live, verify with `systemctl show <unit> | grep -E 'Memory(High|Max)='`, never edit `/etc/systemd/system.control/<unit>.d/` directly (the file is regenerated). The pattern works for `TasksMax`, `CPUWeight`, and other cgroup-backed properties too.

## Phase E4.4d — System tab spec lands (2026-05-25)

A new E4 sub-phase. Captures the Operations tab + System view (chain telemetry MVP-2) — the dashboard surface Larry has been needing all session to make today's debugging self-service. Spec doc only; implementation queued as 4 PRs.

### What triggered it

Today's debugging burned ~3 hours of Larry's day on issues that the dashboard would have surfaced as a single glance: a 71-min Mirror review hang invisible to the chain (PR #101); a "memory leak" that was page cache (PR #102 above); an approved-but-unmerged PR (#101 again, due to the marker-parser bug PR #103 fixed). Every status answer required SSH forensics. Larry's framing: *"I have no clue if they're stuck, if they're working... Currently the live system dashboard seems to be useless."*

### What shipped

- **agents/beacon/specs/e4-4d-system-tab.md** (615 lines, PR #104). Full sub-spec with six locked decisions A–F, schema/API/UI architecture, threshold table, graceful-degradation matrix, 4-PR implementation staging, Pulse Check III self-optimizing loop, post-brief-additions provenance section.
- **agents/beacon/specs/e4-overview.md** (+18 lines) — decision-log row + new sub-phase entry under § 6 Phasing.

The six locked decisions (Beacon Telegram spec round 14:29–15:12 MDT):

| # | Decision |
|---|---|
| A | Hybrid ingestion (poll initial, push-compatible schema day 1) |
| B | Schema: `chain_events` table + `agent_sessions` Postgres VIEW + on-read PR pipeline route |
| C | Three new droplet API endpoints (`/api/system/{active-sessions,cgroup-stats,worktrees}`); stuck-detection lives dashboard-side, not droplet |
| D | Tighter task_type-gated thresholds in editable config file (D1=15min global, D2=10min, D3=2min, D4 Mirror task_type override) |
| E | Nested `Operations` parent tab with System as first child (future siblings: Cost, Memory, Worktrees) |
| F | Public for V1; "build then protect in separate project" later |

Four scope additions made mid-session with explicit Larry approval, documented in spec § 5.11 with verbatim quotes:

1. 5th view — Escalations + Alerts panel (`pulse-escalations.json` + `larry-alerts.jsonl` + `sentinel-alerts.jsonl`) added as a unified surface with `needs_response=true` pinning + per-event "Mark as read"
2. Pulse Check III — 14-day self-optimizing threshold loop with proposal artifact + Beacon Telegram approval shortcut + Claude-as-Forge PR delivery
3. Operating-manual doctrine entry for the self-optimizing-config pattern (this addition lands in the future PR-B build dispatch)
4. Migration 0005 + mark-as-read route handler (derived from #1)

### Authorship: Claude-as-Forge applied to a large doc

The spec was authored entirely in Larry's interactive Claude Code session, not through a Forge dispatch. Justified by the Claude-as-Forge pattern's core test: doc-only, no code execution change, low blast radius, full context already loaded. Mirror review applied at the merge gate, which is where the discipline matters.

### Mirror review marker-shape drift

Mirror's first two reviews of PR #104 emitted REVIEW_REVISION markers in a non-canonical `=== REVIEW_RESULT === { verdict: ... }` wrapper shape. Her third review (the eventual PASS) used yet a third shape: `REVIEW_PASS:` followed by fenced JSON. The always-scan parser shipped in PR #103 recognizes only the canonical `=== REVIEW_VERDICT === ... === END_REVIEW_VERDICT ===` shape, so all three reviews looked invisible to the chain (no `marker-notified beacon <- mirror (mirror-result, intent=review-X)` log line). Larry confirmed each verdict from the session JSONL directly and manually merged PR #104 at 22:22 MDT via `gh pr merge 104 --squash --delete-branch`.

This drift was the trigger for PR #105 (below).

### Recovery cost

~$5.50 LLM across three Mirror reviews + three Beacon notify cycles. Wall time ~5.5 hours, mostly the human + Claude-Code-side forensics between reviews; the chain compute itself was ~8 min combined.

### Codified additions worth recalling next session

44. **Claude-as-Forge scales to large doc artifacts.** The pattern's discipline boundary is "Mirror reviews at the merge gate," not "trivial change size." PR #104 was 615 lines of spec authored entirely in Larry's interactive Claude Code session; the same Mirror-at-merge-gate discipline kept the doc honest. The key extension to capture: a "Post-brief additions" provenance section with verbatim Larry quotes lets Mirror verify which scope expansions were explicitly approved during the Claude-as-Forge session vs. silent scope creep.
45. **The unified PM dashboard now has direct-write surfaces via Supabase REST.** Adding a Project row to a Program ahead of E4.4c (UI CRUD) is a one-line `curl -X POST` against the Supabase REST API using the service-role key from `.env.larry`. Pattern: source `.env.larry`, `curl -X POST "${SUPABASE_URL}/rest/v1/projects" -H "apikey: ${SUPABASE_SERVICE_ROLE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_ROLE_KEY}" -d @<json>`. Beware bash heredoc `$N` positional-arg expansion — use `USD` or escape if a literal dollar sign appears.

## Chain discipline v2 — marker.py mandate + stale-daemon healer (2026-05-25)

Closes two structural failure modes surfaced repeatedly across the day's PR cycle. Self-verifying: Mirror reviewed her own discipline fix in this PR, used the canonical marker shape, AUTO_MERGE fired cleanly.

### Symptom #1 — Mirror marker-shape drift

Three Mirror reviews of PR #104 emitted markers in three different shapes (canonical block, wrapper variant, inline JSON-fenced). The always-scan parser from PR #103 recognizes only the canonical shape. Every non-canonical emission produced silent chain failure (no `marker-notified` log line, no auto-merge, manual session-JSONL recovery required).

The marker.py CLI already exists and produces the canonical shape deterministically. Mirror used it correctly on PR #103's review (which auto-merged); she stopped using it on subsequent reviews. The fix: mandate marker.py in `agents/mirror/CLAUDE.md` so the canonical shape is non-negotiable.

### Symptom #2 — Stale daemon code

PR #103's marker-parser fix landed at 19:16 UTC. The fix didn't take effect because `ourliberty-outbox-notifier.service` had been running since 2026-05-20 (5 days before PR #103 existed); the new code sat on disk while the live process kept running the old version in memory. Larry manually restarted at 17:37 MDT once the regression was diagnosed. There was no automated detection that a daemon was running stale code.

### What shipped (PR #105)

- **agents/mirror/CLAUDE.md** — new non-negotiable section: *"EVERY review marker MUST be emitted via `python3 /home/larry/agent-core/scripts/marker.py render mirror <verdict>` and the stdout PASTED VERBATIM into your response. Do NOT type marker blocks by hand. Do NOT invent wrapper shapes like REVIEW_RESULT. Do NOT inline JSON with REVIEW_PASS: prefix."* Plus matching language in marker.py docstring confirming the CLI is the canonical render.
- **scripts/tests/test_mirror_marker_discipline.py** (new) — scans recent Mirror session JSONLs for non-canonical marker shapes. Reports historical violations (expected: PR #101 + PR #104 r1/r2/r3 sessions) as informational; fails forward if any session post-PR-#105-merge emits a non-canonical shape.
- **scripts/heal_stale_daemon_code.py** (new) — for each `ourliberty-*.service` unit, reads `ActiveEnterTimestamp`, parses `ExecStart` from the unit file, stats the script's mtime, DMs Larry via `larry_alerts.append_alert` if `script_mtime > service_start_time` with delta > 5 min. Cooldown 6h per service. Runs every 30 min via `ourliberty-heal-stale-daemon-code.timer`.
- **agents/forge/CLAUDE.md** + **agents/pulse/CLAUDE.md** — both now mandate marker.py for their respective markers (Forge: PROCEED / CLARIFY_REQUEST / REJECT; Pulse: any structured marker output). Aligns all four agents on canonical shape.
- **docs/operating-manual.md Part II** — this section.
- **runbooks/heal-stale-daemon-code.md** (new) — operational runbook covering when the alert fires + how to verify + how to restart the affected daemon.

### Self-verification

PR #105's own Mirror review (review #3 of this PR-chain, the eventual PASS) used the canonical marker shape. Notifier logged `classified mirror review_pass marker from session log scan` + `marker-notified beacon <- mirror (mirror-result, intent=review-pass)` + `AUTO_MERGE task=chain-discipline-v2-marker-shape-and-stale-daemon-001 ... outcome=merged`. Zero manual recovery needed. This is the test passing.

### Codified additions worth recalling next session

46. **`scripts/marker.py` is the ONLY canonical path for emitting structured markers.** All four agents (Beacon / Forge / Mirror / Pulse) now mandate it in their CLAUDE.md. The CLI produces the `=== REVIEW_VERDICT === ... === END_REVIEW_VERDICT ===` (or PROCEED / etc.) shape that the outbox-notifier parser recognizes. Hand-typed markers, wrapper shapes, and inline-JSON variants are silently dropped from the chain — no error, no DM, just invisible. If you're emitting a marker, you ARE calling marker.py.
47. **A daemon's running code is not its source file.** When a PR merges that modifies a daemon's Python source (`outbox_notifier.py`, `inbox_watcher.py`, healer scripts), the live process keeps the old code in memory until restarted. `heal_stale_daemon_code.py` catches this drift every 30 min by comparing `systemctl show <unit> --property=ActiveEnterTimestamp` against the script's mtime. If you see the alert, `sudo systemctl restart <unit>` is the fix; verify with `systemctl show <unit> --property=ActiveEnterTimestamp` to confirm the timestamp is post-merge.
48. **Self-optimizing-config-via-Pulse-Check is now a documented pattern.** Whenever an operational config surface is a hand-tuned constant (thresholds, retry budgets, cadences, caps, memory limits), the canonical mechanism is: Pulse periodic Check → proposal artifact at `~/agents/blackboard/pulse-<name>-proposals.json` → Telegram DM to Larry → Beacon `approve <name>-update-<date>` shortcut → Claude-as-Forge config-only PR → Mirror auto-merges. Guardrails: no auto-apply, bounded delta (>50% flagged as regime-change-suspected), sample-size floor (≥10 data points per bucket). First instance ships with E4.4d as Pulse Check III for stuck-detection thresholds. Backlog of 10 candidate surfaces in the project list at `dashboard.ourliberty.dev/projects/1b36f0d2-5f2c-4d45-9f13-6806a4c5fa42` (Operational Config Self-Optimization).

## Phase E4 followup — `heal_pipeline_stall.py` (Joe pipeline_watcher analog, 2026-05-26)

Codifies the manager-duty pattern Larry and Claude executed manually throughout the 2026-05-25 session: scan the pipeline state every 15 min, identify stalls, DM Larry with what stalled + recommended action. Adapted from `GrowthMastery-ai/gm-agent-core/scripts/pipeline_watcher.py` (Joe's "you never have to discover a stall on your own" doctrine, 2026-04-15).

### What shipped

- **scripts/heal_pipeline_stall.py** (new) — five concrete stall checks running every 15 min via systemd timer. Silent unless a stall is detected. Each alert dedup'd per unique stall key with 1-hour cooldown. Never acts on the stall — surface only, same posture as `dispatch_sentinel.py`.
- **scripts/tests/test_heal_pipeline_stall.py** (new) — pytest coverage of all five checks, regex patterns against real outbox-notifier log shapes, kill-switch behavior, dedup state file persistence, end-to-end run smoke.
- **systemd/ourliberty-heal-pipeline-stall.service + .timer** (new) — `Type=oneshot`, runs every 15 min with 2-min jitter to avoid lock-step with other healers. `MemoryMax=256M`, hardened sandbox.
- **runbooks/heal-pipeline-stall.md** (new) — operational runbook covering all five checks, the action recipes in each DM, kill switch, threshold tuning, and the manual-run path.

### The five stall checks

1. **Forge built but no PR opened** (>2h) — outbox-notifier shows `[forge] done success=True` but no PR with matching `forge/<task>` branch exists on either tracked repo.
2. **PR opened but no Mirror review-request dispatched** (30 min for doc PRs, 60 min for code PRs gated by title prefix heuristic) — PR exists on a `forge/` branch but no `review-request dispatched mirror` log line for the task.
3. **Mirror PASS but PR still OPEN** (>30 min) — `marker-notified ... intent=review-pass` line exists but the PR for that task is still OPEN. Catches the 2026-05-25 PR #101 and PR #104 stall shape.
4. **Mirror reviewed but no marker classified** (>30 min) — `notified beacon <- mirror (mirror-result, depth=1)` line exists but no follow-up `marker-notified beacon <- mirror` for the same task. Catches marker-shape drift (the bug PR #105 was supposed to prevent at the discipline layer; this healer catches the cases where the discipline mandate gets bypassed). Skipped if the PR auto-merged after the generic notify (a follow-up Mirror dispatch eventually classified).
5. **Retry-cap exhausted in last 30 min** — `All retries exhausted` log lines in the inbox-watcher journal recent window. Surfaces dead-lettered tasks.

### Why this fills a real gap

Throughout the 2026-05-25 session, Larry and Claude manually performed exactly these five checks via SSH each time a status answer was needed. Every "what's happening?" question routed through (a) journalctl inspection, (b) outbox-notifier.log grep, (c) `gh pr view`, (d) Mirror session JSONL forensics. The cost was real — multi-hour debugging sessions where the chain stalled silently and nobody discovered it until somebody asked. This healer makes the discovery automatic.

### Self-test plan after merge

- Trigger an artificial stall (drop a synthetic `[forge] done task=test-stall-X success=True` line with a ts >2h ago into a sandbox log file, point the healer at it) and verify the alert fires within 15 min.
- Verify dedup state file persists across runs (`~/agents/blackboard/heal-pipeline-stall-state.json`).
- Verify `heal_stale_daemon_code.py` (PR #105) consumes the heartbeat file when this healer is stopped.

### Codified additions worth recalling next session

49. **Pipeline-stall detection is a healer, not an agent.** Joe's `pipeline_watcher.py` model: zero-LLM Python script, N hard-coded checks, ONE Telegram DM per unique stall with 1-hour dedup. Our `heal_pipeline_stall.py` follows the same pattern. Manager duty (status answers, "where is everything?") gets split: zero-LLM healers for cheap deterministic checks (this script + `dispatch_sentinel.py` + `heal_pr_auto_merge.py` + `heal_stale_daemon_code.py`); LLM judgment (Pulse `/cycle` once upgraded per the deferred spec) for holistic interpretation when hard rules aren't enough. Avoids the cost of an always-on LLM "Maestro" agent — Joe explicitly chose this architecture; we follow.
50. **The `notified beacon <- mirror (mirror-result, depth=1)` log shape is the marker-invisibility signal.** When Mirror's marker is classified successfully, the notifier ALSO writes `marker-notified beacon <- mirror (mirror-result, intent=review-X, ...)`. The generic `notified ... depth=1` without a follow-up `marker-notified` means the parser failed to extract a verdict — the canonical signature of marker-shape drift (PR #105 mandates marker.py to prevent this at the source; the healer catches recurrences). Check 4 of `heal_pipeline_stall.py` looks for this exact pattern. When investigating "Mirror reviewed but the chain didn't react," this is the log signature to grep.
51. **Joe's pipeline_watcher.py is the canonical model for "watchdog scripts that DM."** Five concrete checks, ONE alert per unique key per hour, recommended action shipped INSIDE the alert text. No reasoning, no LLM, no judgment. Future watchdog candidates should mirror its shape: terse docstring naming the N checks, stable alert keys, dedup state file in blackboard, kill switch, heartbeat for stale-daemon healer to consume.

## E4.4d PR-B — Ingestion daemon + Pulse Check III + self-optimizing config doctrine (2026-05-26)

Second of four sequenced PRs implementing E4.4d (Operations tab + System view). PR-A shipped the `chain_events` migration; this PR ships the writer-side substrate that populates it plus the analyzer that makes thresholds self-optimizing. PR-C ships the droplet API endpoints; PR-D ships the UI.

### What shipped

- **`scripts/chain_event_shipper.py`** (NEW) — poll-based daemon tailing five sources (journalctl `ourliberty-inbox-watcher.service`, `~/agents/logs/outbox-notifier.log`, `~/agents/blackboard/pulse-escalations.json` snapshot, `~/agents/blackboard/larry-alerts.jsonl`, `~/agents/blackboard/sentinel-alerts.jsonl`). Each source has its own resumable cursor: journalctl `--cursor-file`; (inode, byte_offset, first-64-byte fingerprint) tuple per log/jsonl file; (mtime, sha256) for the Pulse snapshot. Writes to `chain_events` via supabase-py `upsert(on_conflict='event_id', ignore_duplicates=True)`, deterministic `event_id = sha1(task_id|event_type|ts)` per spec § 5.1. Bounded local spill buffer (10K lines / 5 MB) on Supabase failure; flushes FIFO on reconnect; drops oldest under chronic outage. Heartbeats every 30s.
- **`scripts/heal_chain_event_shipper_heartbeat.py`** + timer (every 5 min) — DMs Larry if the heartbeat is >10 min stale.
- **`scripts/heal_chain_event_type_audit.py`** + timer (weekly Sun 06:00) — queries `chain_events` for the past 7 days and DMs Larry if any `event_type` rows landed that aren't in the `KNOWN_EVENT_TYPES` allowlist. Belt-and-suspenders against hot-patched daemons, manual SQL, or push-instrumented writers that bypass the shipper's gate.
- **`scripts/pulse_check_iii.py`** + tests — Check III analyzer per spec § 5.10. Queries `chain_events` for the last 30 days of session_start/session_done pairs, computes p90/p99/median per (agent, task_type) bucket, enforces sample-size floor (≥10) + bounded-delta flagging (>50% Δ → `regime-change-suspected`), detects rollback signals (tightened threshold + >3 false-positive alerts within 7d → propose un-tightening). Writes proposal artifact to `~/agents/blackboard/pulse-threshold-proposals.json` + archived copy. Queues a `larry_alerts.append_alert` digest. **Never edits config.**
- **`systemd/ourliberty-chain-event-shipper.service`** — `Type=simple`, `Restart=on-failure`, `MemoryMax=512M / MemoryHigh=256M` per spec § 5.2. Activation gate (`OURLIBERTY_CHAIN_SHIPPER_ENABLED=false` by default).
- **`agents/pulse/CLAUDE.md`** — new "Check III — stuck-threshold review" section explaining the 14-day Sunday-anchored cadence, surface-only discipline, and the script invocation.
- **`agents/beacon/CLAUDE.md`** — new "Pulse Check III approvals" section teaching the `approve threshold-update-<date>` / `reject threshold-update-<date>` shortcuts. Idempotency enforced via `applied: true` flag in the archived artifact; re-running an approve for an already-applied date is a no-op WARN.
- **`runbooks/chain-event-shipper.md`** (NEW) — operational runbook (start/stop, cursor reset, buffer flush, adding a new event_type, Check III approval flow).
- **`systemd/INSTALL.md`** — append the three new units to the install table.
- This entry in operating-manual Part II.

### The self-optimizing-config-via-Pulse-Check doctrine (codified)

Entry #48 (PR #105) introduced the pattern; this PR ships the first instance (Pulse Check III) and codifies the doctrine as reusable agent-OS infrastructure. The five-step loop, generalizable to any operational config surface where hand-tuning eventually drifts away from reality:

1. **Pulse periodic Check** queries chain_events (or another data substrate) for the relevant signal — durations, costs, retry counts, memory peaks, cleanup ages, whatever the config tunes.
2. **Proposal artifact** at `~/agents/blackboard/pulse-<name>-proposals.json` with current vs. proposed values, sample sizes, one-line rationale, `applied: false` flag, and an `as_of` date.
3. **Telegram DM to Larry** via `larry_alerts.append_alert` carrying the digest.
4. **Beacon shortcut** `approve <name>-update-<date>` / `reject <name>-update-<date>` reads the dated artifact, dispatches a small Claude-as-Forge PR (config-only, `task_type: doc-only` so trust policy auto-approves).
5. **Mirror auto-merges** the PR. On merge, Beacon flips `applied: true` in the archived artifact, which makes future replays of the same shortcut a no-op WARN.

**Discipline boundaries (non-negotiable):**

- **No auto-apply.** Pulse proposes, Larry approves. Same posture as the stuck-detector: surface signal, never act on it.
- **Bounded delta.** Changes >50% from current ship with `high-attention: regime-change-suspected` so Larry knows before approving.
- **Sample-size floor.** Buckets with fewer than ~10 observations skip silently; better to wait than tune on noise.
- **Idempotent shortcuts.** Re-running the approve command for an already-applied date is a WARN, never a duplicate PR. The `applied: true` flag is the gate; without an archived artifact, the shortcut aborts and DMs Larry.
- **Rollback detection.** If a recently-applied tightening produced false positives within the rollback window, the next Check automatically proposes un-tightening with `rollback: true` in the rationale.

**Backlog of candidate surfaces** (10 total; current first instance in bold; full list tracked in PM project `dashboard.ourliberty.dev/projects/1b36f0d2-5f2c-4d45-9f13-6806a4c5fa42` "Operational Config Self-Optimization"):

- **Stuck-detection thresholds** (`config/system_tab_thresholds.json` — shipped this PR as Pulse Check III)
- Cost-budget cap per task_type (`config/agent-models.json loop_bounds.cost_per_task_usd`)
- Memory thresholds (systemd `MemoryMax` / `MemoryHigh` per service)
- Cleanup retention windows (worktree TTL, dedup-marker TTL, inbox-archive TTL)
- Healer retry budgets (`MAX_RETRIES_PER_PR`, `MAX_MERGES_PER_RUN`, etc.)
- Cycle cadences (`/cycle` frequency, Check I/II/III cadences themselves)
- Plus four more enumerated in the PM project record

### Reviewer focus (Mirror, this PR)

Dedup correctness (deterministic `event_id`; no double-INSERTs). Cursor resume after restart across all five sources. Buffer overflow behavior under sustained Supabase outage (bounded local buffer, no OOM, oldest drops with WARN). `KNOWN_EVENT_TYPES` discipline (unknown types route to a sentinel-rejected count, not silently dropped or mislabeled). No PII or credentials in log payloads (the `sanitize_payload` walker redacts on credential-shaped keys before INSERT). `pulse-escalations.json` snapshot-overwrite handling (cursor is `(mtime, content_sha256)`, daemon detects rewrites instead of assuming append-only). Check III analyzer correctness (sample-size floor enforced; bounded-delta flagging prevents wild swings; rollback-signal detection catches regressive tightenings). Beacon shortcut idempotency (re-applying `approve threshold-update-<date>` for an already-applied date is a no-op WARN, not a double-apply).

### Codified additions worth recalling next session

52. **Five-source poll-daemon pattern.** The shipper's cursor + buffer + sanitize machinery is genuinely reusable. Future event-source ingestion (e.g. healer-fire events, third-party webhooks captured to a file, dispatch-sentinel JSONL) follows the same shape: a parser per source, a per-source cursor, a fingerprint-aware tail iterator for rotation safety, an `upsert(ignore_duplicates=True)` sink with a bounded write-ahead spill. Spec § 5.1 + 5.2 cover the design; `scripts/chain_event_shipper.py` is the reference implementation.
53. **First-bytes file fingerprint catches reused-inode rotations.** Inode-equality + size-shrinkage detection alone misses the case where a file is deleted and recreated at the same size (Linux tmpfs/ext4 frequently reuses freed inodes). The fingerprint of the first 64 bytes (only computed when the file is ≥64 bytes — smaller files can't yield a stable prefix) closes that gap. Pattern is small enough to copy directly: `FileCursor` includes `fp_sha` and `tail_file` rewinds when `cursor.fp_sha != current_fp`.
54. **Pulse Check III is the first concrete instance of doctrine #48.** Entry #48 named the self-optimizing-config-via-Pulse-Check pattern; this PR codifies it as agent-OS doctrine with discipline boundaries (no auto-apply, bounded delta, sample-size floor, idempotent shortcut) and ships the first instance against stuck-detection thresholds. The 10-surface backlog in PM project `1b36f0d2-5f2c-4d45-9f13-6806a4c5fa42` is the canonical list of where to apply the pattern next.
55. **Application-side event-type validation, not DB-level.** Spec § 5.1 chose plain TEXT for `chain_events.event_type` over a Postgres ENUM. The trade-off: adding a new event_type is a single-line Python PR (Mirror catches typos at code review) instead of a migration; the cost is a belt-and-suspenders weekly audit healer to catch any drift. The audit healer is itself a reusable pattern: any allowlist gated in code (not schema) earns a weekly auditor that re-checks the data layer for drift.

## E4.4d PR-C — Droplet API endpoints + thresholds config

Third of four sequenced PRs implementing the Operations → System view (sub-spec at `agents/beacon/specs/e4-4d-system-tab.md`). PR-A landed the `chain_events` migration in `ourliberty-dashboard`; PR-B (parallel to this one) lands the ingestion daemon + Pulse Check III; PR-D wires the dashboard UI. This PR extends the existing droplet-side `dashboard-api.service` with three uncached read endpoints and ships the tunable thresholds config.

### What shipped (PR-C)

- **scripts/dashboard_api.py** — extended with three new `GET` endpoints under `/api/system/*`. All three are token-gated via the existing `_require_token` dependency, return JSON, and read filesystem state fresh on every request (no in-process caching).
  - `/api/system/active-sessions` — reads `cgroup.procs` for the `ourliberty-inbox-watcher.service` slice, cross-references each PID against the `~/agents/state/in-flight/*.json` registry. Returns raw per-session signals only: pid, agent, task_id, task_type, model (best-effort from `/proc/<pid>/cmdline`), started_at, duration_sec. **No `stuck` / `stuck_reason` booleans** — locked decision-C puts stuck-detection on the dashboard side. PIDs that die mid-read are silently omitted, not 500-ed.
  - `/api/system/cgroup-stats` — reads `memory.{current,peak,max,high,events}` and `cpu.stat` from the inbox-watcher slice. `memory.max` / `memory.high` literal-`max` sentinel surfaces as JSON `null` rather than a coercion error.
  - `/api/system/worktrees` — filesystem-only walk of `/home/larry/agent-worktrees/`, parses `wt-<agent>-<task_id>` dirnames into (agent, task_id, branch), stats mtime, cross-references in-flight registry. **Never shells out** to `git worktree list` — closes the no-user-input-near-subprocess reviewer concern.
- **scripts/tests/test_dashboard_api_system.py** (new) — 15 tests covering success paths, 401-on-missing/wrong-token paths, 503-with-structured-body degraded path (slice stopped → `{"error": "service-unavailable", "message": ...}`), PID-race tolerance, the literal-`max` sentinel, and an uncached-behavior assertion (mutating the underlying file between requests must change the response).
- **config/system_tab_thresholds.json** (new) — locked decision-D values verbatim: `session_duration_seconds_default=900`, `no_journal_output_seconds=600`, `envelope_not_picked_up_seconds=120`, plus Mirror `task_type` overrides (`doc-only=900`, `bug-investigation=1500`, `feature-development=2100`, `_default=1500`). Read by the dashboard's `/api/operations/stuck-sessions` Route Handler in PR-D.
- **runbooks/system-tab-thresholds.md** (new) — operator doc explaining each threshold's semantics, when to tune, and the edit-then-reload procedure (PR review path, no service restart required — the file is re-read on every request).
- **docs/operating-manual.md Part II** — this section.

### Design constraints honored

- **Uncached server-side.** No in-process cache, no TTL, no memoization. Every request re-reads the filesystem / `/proc` / cgroup files. The dashboard owns caching client-side. Test `UncachedTest` asserts mutating a file mid-flight changes the next response.
- **Raw signals only.** Per locked decision-C, stuck-detection lives in the dashboard's `/api/operations/stuck-sessions` Route Handler (PR-D). The droplet exposes duration_sec, started_at, memory bytes, etc.; the dashboard joins with thresholds + `chain_events` to compute booleans.
- **Structured error bodies.** Cgroup ENOENT (slice stopped) returns `503` with `{"error": "service-unavailable", "message": "<path>"}` via FastAPI's `HTTPException(detail=dict)`. No bare 500s.
- **No shell injection surface.** Worktree listing is filesystem walk, not `git worktree list --porcelain`. The three endpoints take zero user input regardless.
- **Test isolation.** Three new env overrides for tests: `OURLIBERTY_CGROUP_BASE`, `OURLIBERTY_WORKTREES_ROOT` (the existing `OURLIBERTY_AGENTS_ROOT` covers the in-flight registry). Same pattern as the existing E3.1 test suite.

### Coordination with PR-B

PR-B (ingestion daemon, parallel dispatch in `ourliberty-agent-core`) also appends a Part II section but uses a distinct title; merging order between PR-B and PR-C is reversible — each will rebase cleanly across the other.

### Followups deferred to PR-D

- The dashboard's stuck-detection Route Handler at `app/api/operations/stuck-sessions/route.ts`.
- Delivery of `config/system_tab_thresholds.json` into the dashboard repo (CI sync vs. a new `/api/system/thresholds` droplet endpoint — PR-D picks one).
- Migration 0005 `chain_events_read_flag.sql` for the "Mark as read" affordance.

---

*This doc lives at `docs/operating-manual.md` in `Larry-Yatch/ourliberty-agent-core`. Edit Part I in place when something changes about how the system behaves. Append a new section to Part II when a phase ships — don't edit earlier phase entries; capture the change in the new entry instead.*

