# Rotate a Telegram bot token

**When to run this:** when a bot token is suspected leaked (e.g. accidentally pasted into a chat/PR), or on unscheduled compromise response. Telegram bot tokens don't expire on a schedule — `rotation_type` is `revocation_only` for all four bots — so this runbook is the response, not a calendar event.

**Covers:** `TELEGRAM_BOT_TOKEN_BEACON`, `TELEGRAM_BOT_TOKEN_FORGE`, `TELEGRAM_BOT_TOKEN_MIRROR`, `TELEGRAM_BOT_TOKEN_PULSE`. Procedure is identical; just swap the bot username.

**Severity if lapsed:** high. Whichever bot's token is revoked stops replying until the new token is installed — Larry loses that DM channel.

**Time required:** ~5 minutes per bot.

---

## Steps

### 1. Open BotFather

In Telegram, message `@BotFather`. Send `/mybots` and pick the bot whose token you want to revoke (e.g. `@OLH_Beacon_bot`).

### 2. Revoke the current token

From the bot's menu, choose **API Token** → **Revoke current token**. BotFather confirms revocation and immediately issues a new token. **Copy the new token immediately** — BotFather shows it once in the message; if you lose it, repeat this step.

The new token shape is `<bot-id>:<35-char-suffix>`. The bot-id (first numeric segment) does NOT change across rotations.

### 3. Install the new token on the droplet

From your laptop's Terminal, with the new token on your clipboard, replace the matching `TELEGRAM_BOT_TOKEN_<NAME>` line in `.env.larry` atomically:

```bash
# Pick the right env-var name for the bot you rotated.
ENV_VAR=TELEGRAM_BOT_TOKEN_BEACON

pbpaste | ssh larry@134.209.44.80 "python3 -c \"
import sys, re, os, tempfile
p = '/home/larry/credentials/.env.larry'
token = sys.stdin.read().strip()
assert token and ':' in token and len(token) >= 30, 'token validation failed'
content = open(p).read()
new, n = re.subn(r'^${ENV_VAR}=.*\$', f'${ENV_VAR}={token}', content, flags=re.M)
assert n == 1, f'expected 1 replacement, got {n}'
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f'OK: ${ENV_VAR} updated ({len(token)} chars)')\""
```

Then clear your clipboard:

```bash
pbcopy < /dev/null
```

### 4. Restart the matching bot service

```bash
# Pick the right service for the bot you rotated.
SERVICE=ourliberty-beacon-bot.service

ssh larry@134.209.44.80 "sudo systemctl restart $SERVICE && \
  sleep 2 && systemctl is-active $SERVICE"
```

Expected output: `active`. If `failed`, run `journalctl -u $SERVICE -n 50` to see the cause — most often a paste error in the new token.

### 5. Verify the bot is back online

```bash
ssh larry@134.209.44.80 "source /home/larry/credentials/.env.larry && \
  curl -sS https://api.telegram.org/bot\$$ENV_VAR/getMe | python3 -m json.tool"
```

Expected: a JSON blob with `\"ok\": true` and the bot username inside `result.username`.

Cross-check: send a `/ping` (or any message) to the bot in Telegram. It should reply within a few seconds. Old token is now revoked by BotFather; only the new token reaches the bot.

### 6. Update the registry

No `last_rotated_at` push for `revocation_only` entries (the field stays at install date). But add a one-line PR note: `chore(creds): rotated <bot> token <YYYY-MM-DD> after <reason>` — keeps the audit trail.

If the rotation was triggered by suspected leak, also:
- Search the repo + chat logs for the old token's `<bot-id>:<suffix>` prefix to confirm no exposure remains.
- Note the incident in `agents/pulse/MEMORY.md` so future audits know what happened.

---

## Rollback

There is no rollback. Once BotFather issues a new token, the old one is dead — Telegram's API stops accepting it immediately. If you panic-revoked the wrong bot's token, just install the new token per Step 3 and restart per Step 4; the bot continues working under its new credential.

---

## Related

- Registry entries: `config/token-rotation-schedule.json` (search for `TELEGRAM_BOT_TOKEN_`)
- Convention: `shared/credentials-discipline.md`
- Drift healer: `scripts/heal_credential_registry_drift.py` (6h)
- BotFather docs: https://core.telegram.org/bots#botfather
