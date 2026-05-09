# Operating Manual — Larry's Agent OS

This is the day-to-day manual for running, using, and troubleshooting Larry's agent system. Read top-to-bottom on first pass. After that it's a reference — jump to whatever section you need.

**Last updated:** 2026-05-08 (Phase B — Beacon online via Telegram)

---

## 0. The 30-second mental model

```
   ┌────────────────┐                ┌──────────────────────┐
   │  Your phone    │  Telegram msg  │  Telegram's servers  │
   │  (Telegram)    ├───────────────>│  (api.telegram.org)  │
   └────────────────┘                └──────────┬───────────┘
                                                │ getUpdates poll
                                                ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Droplet: ourliberty-agents-01 @ 134.209.44.80             │
   │  ┌──────────────────────────────────────────────────────┐  │
   │  │  tmux session: beacon-bot                            │  │
   │  │  ┌────────────────────────────────────────────────┐  │  │
   │  │  │  beacon_telegram_bot.py                        │  │  │
   │  │  │   • polls Telegram for messages                │  │  │
   │  │  │   • spawns: claude --print --resume "<msg>"    │  │  │
   │  │  │     in ~/agent-core/agents/beacon/             │  │  │
   │  │  │   • posts reply back to Telegram               │  │  │
   │  │  └────────────────────────────────────────────────┘  │  │
   │  └──────────────────────────────────────────────────────┘  │
   │                                                            │
   │  ~/credentials/.env.larry  ← bot token, chat ID            │
   │  ~/agent-core/             ← source repo (cloned, kept     │
   │                              current via git pull)         │
   │  ~/agents/                 ← runtime: state, logs, memory  │
   └────────────────────────────────────────────────────────────┘
                                      │
                                      │ Anthropic API call
                                      ▼
                              ┌───────────────┐
                              │  Claude Opus  │
                              │  (Larry's Max)│
                              └───────────────┘
```

**In English:** You send a Telegram message. The bot (a Python script running on your droplet inside a tmux session) sees it via long-polling, runs Claude Code with Beacon's prompt files, gets the response, sends it back through Telegram.

**Key idea:** The tmux session keeps the bot alive even when you're not SSHed in. As long as the droplet is running, the bot is running.

---

## 1. The pieces, named

| Piece | What it is | Where it lives |
|---|---|---|
| **Droplet** | The Linux virtual machine that hosts everything. Always-on. | DigitalOcean, NYC3 region |
| **IP address** | The droplet's address on the internet. | `134.209.44.80` |
| **Domain** | A friendly name pointing to the droplet. | `agents.ourliberty.dev` (DNS A record in Cloudflare) |
| **SSH** | How you log into the droplet from your Mac. | `ssh larry@134.209.44.80` |
| **`larry` user** | Your account on the droplet. Has sudo (admin) access without password prompts. | `/home/larry/` on the droplet |
| **`~/agent-core/`** | The source code repo, cloned to the droplet. Updated via `git pull`. | `/home/larry/agent-core/` |
| **`~/agents/`** | Runtime state — logs, memory, agent-specific working files. **Never touched by `git pull`.** | `/home/larry/agents/` |
| **`~/credentials/`** | Where secrets live. Mode 700 (only you can read). | `/home/larry/credentials/.env.larry` |
| **tmux** | A terminal multiplexer. Keeps the bot running after you log out. | Ubuntu package, already installed |
| **`beacon-bot`** | The tmux session name where the bot runs. | `tmux ls` to see it |
| **`beacon_telegram_bot.py`** | The Python script that bridges Telegram and Beacon. | `~/agent-core/scripts/beacon_telegram_bot.py` |
| **`beacon_telegram_bot.sh`** | The launcher script that starts the Python bot inside a tmux session. | `~/agent-core/scripts/beacon_telegram_bot.sh` |
| **`.env.larry`** | Environment file with your secrets (bot token, chat ID, etc). | `~/credentials/.env.larry` |
| **Beacon** | The Strategy/Architect agent. A persona defined in 6 markdown files. | `~/agent-core/agents/beacon/*.md` |

---

## 2. Daily use — talking to Beacon

### From your phone, anywhere

Open Telegram. Find your bot (the username you gave BotFather). Send a message. Wait 5–30 seconds. Beacon replies.

That's it. **You don't need to be at your computer.** As long as the droplet and the bot are running, Telegram works.

### Conversation continuity

The bot uses `claude --resume` per chat. That means **all your messages to Beacon are in one continuing conversation**, even days apart. Beacon remembers what you discussed last week.

The session ID is stored at `~/agents/state/beacon_telegram_sessions.json` on the droplet (one entry per chat ID).

### When to start a new conversation

Almost never. Beacon's memory across messages is the whole point. If you ever need to truly start fresh (rare):

```bash
ssh larry@134.209.44.80
rm ~/agents/state/beacon_telegram_sessions.json
bash ~/agent-core/scripts/beacon_telegram_bot.sh   # restart bot
```

Next message will start a new session.

### What Beacon can and can't do

**Can:**
- Have a real conversation about ideas, design, architecture
- Ask you clarifying questions
- Produce structured specs (using the template in `agents/beacon/TOOLS.md`)
- Read any file in `~/agent-core/` and any of your GitHub repos
- Reference previous conversations and notes

**Can't (yet):**
- Write code (Forge does that — Forge doesn't exist yet, that's Phase C)
- Open PRs / merge code (also Forge / Mirror — Phase C)
- Watch the system itself (Pulse — Phase D)
- Send emails or interact with Google Workspace (Aide — Phase E)

If you ask Beacon to do something outside her scope, she'll tell you she can't and (usually) suggest the right next step.

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

## 4. Bot lifecycle

### Is the bot running?

```bash
tmux ls
```

Expected output:

```
beacon-bot: 1 windows (created Fri May  8 19:25:38 2026)
```

If you see `no server running on...` or `beacon-bot` is missing, the bot is NOT running. Start it.

### Starting the bot

```bash
bash ~/agent-core/scripts/beacon_telegram_bot.sh
```

The launcher will:
1. Verify `~/credentials/.env.larry` has `TELEGRAM_BOT_TOKEN_BEACON` and `TELEGRAM_ALLOWED_CHAT_IDS` set (refuses to start if missing)
2. Kill any existing `beacon-bot` tmux session (so it's safe to rerun)
3. Start a fresh tmux session called `beacon-bot` running the Python bot
4. Print confirmation

You should see:

```
Bot running in tmux session 'beacon-bot'.
View live:  tmux attach -t beacon-bot   (Ctrl-b d to detach)
Tail log:   tail -f ~/agents/logs/beacon_telegram_bot.log
Stop:       tmux kill-session -t beacon-bot
```

### Stopping the bot

```bash
tmux kill-session -t beacon-bot
```

That's it. The bot stops immediately. Telegram messages sent while the bot is down will be queued by Telegram and delivered when you start it again (within Telegram's retention window — usually 24 hours).

### Restarting the bot

After a code change or if the bot is acting weird:

```bash
cd ~/agent-core && git pull
bash ~/agent-core/scripts/beacon_telegram_bot.sh
```

The launcher kills the existing session and starts fresh, so you don't need to manually stop first.

### After a droplet reboot

**This is currently the biggest weakness.** The bot is running in tmux, which doesn't survive reboots. If the droplet reboots (rare, but unattended-upgrades occasionally triggers one), the bot is gone.

**Symptom:** You send a Telegram message and get no reply, even after a few minutes.

**Fix:**

```bash
ssh larry@134.209.44.80
tmux ls   # confirms beacon-bot is missing
bash ~/agent-core/scripts/beacon_telegram_bot.sh
```

**Permanent fix coming:** In Phase D, we'll convert the bot to a systemd service so it auto-starts on boot.

### How to know if the droplet is up at all

From your Mac:

```bash
ping -c 3 134.209.44.80
```

If you get replies, droplet is alive. If timeouts, the droplet is down — check the DigitalOcean web dashboard.

---

## 5. tmux — what it is, the 5 commands you actually need

**tmux** is a "terminal multiplexer." Think of it as a way to keep a terminal session running on the droplet even after you disconnect. The bot lives inside a tmux session so it doesn't die when you log out.

### The 5 commands

| Command | What it does |
|---|---|
| `tmux ls` | List all running tmux sessions |
| `tmux attach -t beacon-bot` | Attach (jump into) the beacon-bot session — see what it's doing live |
| Press `Ctrl-b` then `d` (sequentially) | Detach from the session — leaves it running, returns you to your normal shell |
| `tmux kill-session -t beacon-bot` | Stop the bot |
| `tmux new -s scratch` | Start a new session called `scratch` (useful if you want to run something long-lived manually) |

### The one gotcha

When you're attached to the bot's tmux session, **don't press Ctrl-C** — that kills the bot. Press **Ctrl-b** then **d** to detach safely.

If you accidentally Ctrl-C'd, just relaunch with `bash ~/agent-core/scripts/beacon_telegram_bot.sh`.

---

## 6. Logs — where to look and what to expect

### The two log files

```
~/agents/logs/beacon_telegram_bot.log        # what the Python bot writes
~/agents/logs/beacon_telegram_bot.tmux.log   # captured tmux output
```

### Reading the log

```bash
# Last 50 lines
tail -50 ~/agents/logs/beacon_telegram_bot.log

# Live tail — watch as new lines come in (Ctrl-C to stop watching)
tail -f ~/agents/logs/beacon_telegram_bot.log

# Search for errors
grep -i error ~/agents/logs/beacon_telegram_bot.log | tail -20
```

### What "normal" looks like

```
[2026-05-08T19:25:39-0600] Beacon bot starting (cwd=/home/larry/agent-core/agents/beacon, allowed=[7998341473])
[2026-05-08T19:26:14-0600] <- 7998341473: 'Hello Beacon — read in.'
[2026-05-08T19:26:39-0600] -> 7998341473: 'Read in. Beacon, strategy/architect for Larry...'
```

- `<-` means an incoming message from you
- `->` means Beacon's reply going back

### What's NOT normal

- `[claude exit 1]` followed by error text → Claude Code failed to run (auth issue, quota issue, syntax issue in your message)
- `ignored unauthorized chat 1234567` → Someone OTHER than you tried to talk to the bot. Not an emergency, but worth knowing — your `TELEGRAM_ALLOWED_CHAT_IDS` correctly blocked them.
- `URL error... timed out` → Network blip with Telegram. Bot retries automatically.
- `[Beacon timed out after 10 min]` → Beacon spent 10+ min on a single message. Either it was a heavy task or something hung. Restart the bot if it persists.

### Log size

The log grows. Roughly 1 KB per message exchange. After months of use, run:

```bash
# Archive old log, start fresh
mv ~/agents/logs/beacon_telegram_bot.log ~/agents/logs/beacon_telegram_bot.log.$(date +%Y%m%d)
bash ~/agent-core/scripts/beacon_telegram_bot.sh   # restart picks up the new file
```

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

Shows the latest commit on your droplet. Compare to GitHub's `main` branch to see if you're behind.

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

1. **Is the bot running?** SSH in, `tmux ls`. If `beacon-bot` is missing → start it.
2. **Is the droplet up?** From your Mac: `ping 134.209.44.80`. If timeouts → check DO dashboard.
3. **Is your chat ID still allow-listed?** SSH in: `grep ALLOWED ~/credentials/.env.larry`. Should show your numeric ID.
4. **Did Claude Code's auth expire?** SSH in, `cd ~/agent-core/agents/beacon && claude --print "say ok"`. If it errors with auth issues → run `claude` interactively, log in again, then restart the bot.
5. **Tail the log live, send a fresh message, watch:**
   ```bash
   tail -f ~/agents/logs/beacon_telegram_bot.log
   # Then send a Telegram message from your phone
   ```
   You should see `<- ...` appear within 1–2 seconds. If not, the bot isn't seeing your message.

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

### "Bot crashed and won't start"

```bash
# Check for syntax errors
python3 -m py_compile ~/agent-core/scripts/beacon_telegram_bot.py
# If output is empty, syntax is fine. If errors, that's the issue.

# Check env vars are set (without exposing values)
grep -E '^(TELEGRAM_BOT_TOKEN_BEACON|TELEGRAM_ALLOWED_CHAT_IDS)=' ~/credentials/.env.larry | \
    sed 's|TELEGRAM_BOT_TOKEN_BEACON=.*|TELEGRAM_BOT_TOKEN_BEACON=<set>|'
# Should show both as <set>= or with a value

# Try running the bot in foreground (not tmux) to see errors directly
set -a; . ~/credentials/.env.larry; set +a
python3 ~/agent-core/scripts/beacon_telegram_bot.py
# Errors will print to your terminal. Ctrl-C to stop.
```

### "Telegram says my bot's token is invalid"

The token in `.env.larry` doesn't match what BotFather has. Either:
- The token got truncated when pasted → Re-run `bash ~/install_beacon_creds.sh`
- BotFather rotated the token (rare, only if you ran `/revoke` or `/token`) → Get the current token, install it again

---

## 10. Cost monitoring

### Anthropic (Claude Code / API)

- **Where:** [console.anthropic.com](https://console.anthropic.com) → Usage
- **Currently using:** Your personal Claude Max OAuth (no per-request charge — you pay the monthly Max sub). Quota is per Max account.
- **Future (Phase D):** A separate Anthropic API key with billing for orchestrator scripts (`/cycle`, watchdog). Those will appear as API charges.

### DigitalOcean

- **Where:** [cloud.digitalocean.com](https://cloud.digitalocean.com) → Billing
- **Current monthly:** ~$58 (droplet $48 + backups $9.60)
- **What to watch for:** Bandwidth overages (we have 6 TB/mo — should never hit). Snapshot count growing (each snapshot costs storage).

### Cloudflare

- **Free** for the DNS service we use. Domain renewal: ~$12/yr in May 2027.

### Telegram

- Free.

### Total expected monthly: ~$58 + Anthropic spend, currently ~$0 incremental from Beacon (within Max plan).

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
| **Forge / Mirror / Pulse / Aide / Scout / Compass** | Future agents — see README. Don't exist yet. |

---

## Appendix A — Quick reference card (print this)

```
SSH IN:                    ssh larry@134.209.44.80
START BOT:                 bash ~/agent-core/scripts/beacon_telegram_bot.sh
STOP BOT:                  tmux kill-session -t beacon-bot
IS BOT RUNNING?:           tmux ls
WATCH BOT LIVE:            tmux attach -t beacon-bot   (Ctrl-b d to detach)
TAIL LOG:                  tail -f ~/agents/logs/beacon_telegram_bot.log
PULL NEW CODE:             cd ~/agent-core && git pull
RESTART AFTER CODE PULL:   bash ~/agent-core/scripts/beacon_telegram_bot.sh
EDIT CREDENTIALS:          nano ~/credentials/.env.larry  (Ctrl-O save, Ctrl-X exit)
TALK TO BEACON DIRECTLY:   cd ~/agent-core/agents/beacon && claude
DROPLET STATUS:            (from Mac) ping -c 3 134.209.44.80
```

---

## Appendix B — Things that will come and what they mean

| When | What | What changes for you |
|---|---|---|
| Phase A.12 (small follow-up) | Daily cron mirroring upstream gm-agent-core into your mirror | Nothing visible day-to-day |
| Stabilization session | Bot becomes a systemd service | Bot survives droplet reboots automatically. Manual start commands above still work. |
| Stabilization session | Dedicated agent-only Claude Max | Beacon's quota stops competing with your personal Claude Code use |
| Phase C | Forge + Mirror agents added | Two more Telegram bots; build pipeline goes live; you can ask Beacon to draft a spec, then ask Forge to implement |
| Phase D | Pulse + `/cycle` self-healing | A new dashboard/log of what `/cycle` is finding and fixing automatically; rare DMs from Pulse when something needs you |
| Phase E | Aide (EA) added | A different bot that handles Gmail/Calendar work; separate Telegram channel |
| Phase F | Mini Brains prototype shipped | First real prototype repo with a handoff package |

---

*This doc lives at `docs/operating-manual.md` in `Larry-Yatch/ourliberty-agent-core`. Edit it directly when you learn something new about how the system behaves — that's how the manual gets better over time.*
