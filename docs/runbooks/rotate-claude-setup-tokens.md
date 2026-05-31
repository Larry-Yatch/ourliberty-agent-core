# Rotate Claude Code setup-tokens (Tier 1 + Tier 2)

**Purpose.** Re-mint and install the long-lived Claude Code OAuth setup-tokens used by `scripts/agent_runner.py` as the **primary dispatch auth** for every agent invocation on the droplet. Per PR #210 (2026-05-30, auth_401-storm fix), `_apply_tier_auth` reads `CLAUDE_CODE_OAUTH_TOKEN_TIER1` and `CLAUDE_CODE_OAUTH_TOKEN_TIER2` from the process env and exports them as `CLAUDE_CODE_OAUTH_TOKEN` for each tier dispatch. These tokens are **NON-refreshing** and **EXPIRE in ~1 year** — unlike `~/.claude/.credentials.json` (`CLAUDE_MAX_OAUTH`), there is no auto-refresh. If they lapse unregistered, auth silently dies with no reminder.

**When to run this.**

- Pulse DMs that `CLAUDE_CODE_OAUTH_TOKEN_TIER1` or `CLAUDE_CODE_OAUTH_TOKEN_TIER2` is within 60 days of `next_rotation_due` (registered as `2027-05-30`).
- Healer alert `MISSING_CREDENTIAL:CLAUDE_CODE_OAUTH_TOKEN_TIER1` or `..._TIER2`.
- Observed `auth_401` failures attributed to the setup-token path (agent_runner log line includes `auth=setup_token` immediately before the 401).
- Unscheduled, on suspected leak of either token.

**Severity if lapsed.**

| Token | Severity | Why |
|-------|----------|-----|
| Tier 1 (`agent.beacon.ourliberty@gmail.com`, org `43441a1c-123f-4933-8f7f-55572925600f`) | critical | Primary dispatch auth for every agent. If it expires and Tier 1 falls back to `credentials.json` it races the auth_401 storm that PR #210 fixed. |
| Tier 2 (`larry@sealteamleaders.com`, org `848cafcc-765d-4a09-837d-eaf8d12b07cd`) | high | Fallback dispatch auth. Tier 1 continues working but the rate-limit / auth-401 fallback path is broken. |

**Time required:** ~10 minutes wall-clock per token; both can be done in the same window.

**Auth model recap.** These setup-tokens are the **race-proof** path that supersedes `credentials.json` as primary dispatch auth (per PR #210). `_apply_tier_auth` prefers the setup-token from the env when present; only falls back to the token-manager (`credentials.json`-derived) value when unset. The `CLAUDE_MAX_OAUTH` and `LARRY_PERSONAL_CLAUDE_MAX_OAUTH_TIER2` registry entries cover the `credentials.json` fallback path; this runbook covers the primary path.

---

## Pre-flight (BOTH tokens)

Before re-minting, confirm the existing tokens are actually installed where the watcher expects:

```bash
ssh larry@134.209.44.80 'grep -c "^CLAUDE_CODE_OAUTH_TOKEN_TIER[12]=" /home/larry/credentials/.env.larry'
# Expected: 2
```

If the count is < 2, the env file is missing one or both keys — fix that first (the systemd unit `ourliberty-inbox-watcher.service` consumes this file via `EnvironmentFile=`).

---

## Tier 1 rotation — `CLAUDE_CODE_OAUTH_TOKEN_TIER1`

### 1. Re-mint via `claude setup-token` on the agent account

On a machine with a browser (your laptop), in a session where `claude` is authenticated as `agent.beacon.ourliberty@gmail.com` (org `43441a1c-123f-4933-8f7f-55572925600f`):

```bash
claude setup-token
```

- The CLI prints a one-time URL. Open it in a browser **explicitly logged into the agent account** (incognito + paste credentials is the safest path — the wrong-account-in-browser gotcha from `restore-larry-personal-claude-oauth-tier2.md` STEP 3 applies here too).
- Approve the request.
- The CLI prints a token starting with `sk-ant-oat01-...`. **Copy it immediately** — it's shown once.

### 2. Install on the droplet (clipboard never enters an agent context)

From your laptop's Terminal, with the new token on your clipboard:

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
token = sys.stdin.read().strip()
assert token and len(token) >= 20 and \" \" not in token and \"\n\" not in token, \"token validation failed\"
content = open(p).read()
new, n = re.subn(r\"^CLAUDE_CODE_OAUTH_TOKEN_TIER1=.*$\", f\"CLAUDE_CODE_OAUTH_TOKEN_TIER1={token}\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f\"OK: CLAUDE_CODE_OAUTH_TOKEN_TIER1 updated ({len(token)} chars)\")"'
```

Then clear your clipboard:

```bash
pbcopy < /dev/null
```

### 3. Restart the watcher so `agent_runner` re-imports the env

```bash
ssh larry@134.209.44.80 'sudo systemctl restart ourliberty-inbox-watcher.service && \
  sleep 2 && systemctl is-active ourliberty-inbox-watcher.service'
```

Expected: `active`. The systemd unit reads `EnvironmentFile=/home/larry/credentials/.env.larry`, so the new token enters the watcher's process env on next start; subprocess dispatches via `agent_runner._apply_tier_auth` will then pick it up.

### 4. Verify (a single live dispatch should log `auth=setup_token` and produce zero `auth_401`)

Watch the watcher log for the next Tier 1 dispatch:

```bash
ssh larry@134.209.44.80 'tail -F /home/larry/agents/state/watcher.log | grep -E "auth=setup_token|auth_401|tier=tier1"'
```

Wait for any Tier 1 dispatch (Pulse cycles every ~4h; a beacon or forge dispatch is also fine), then confirm:

- One or more lines with `tier=tier1 ... auth=setup_token`.
- **Zero** `auth_401` lines following that dispatch.

If you see `auth=credentials_json` instead, the env var didn't reach the subprocess — the watcher wasn't restarted after the env edit, OR the env-file edit didn't land (re-run step 2 with `grep -c` to verify).

If you see `auth_401` following `auth=setup_token`, the new token is bad — re-mint per step 1.

### 5. Update the registry

Edit `config/token-rotation-schedule.json` for `CLAUDE_CODE_OAUTH_TOKEN_TIER1`:

- `last_rotated_at` → today's date (`YYYY-MM-DD`)
- `next_rotation_due` → today + 365 days
- Open a PR titled `chore(creds): rotate Claude setup-token Tier 1 <YYYY-MM-DD>`.

### 6. Push the calendar event out by 1 year

DM Beacon (`@OLH_Beacon_bot`): *"Push the Claude setup-token Tier 1 rotation calendar event forward by 1 year"* — she will move the event via her Google Calendar MCP. (The original event is whichever Beacon backfilled per PR creating these registry entries; `calendar_event_url` will be populated by then.)

---

## Tier 2 rotation — `CLAUDE_CODE_OAUTH_TOKEN_TIER2`

Same shape as Tier 1, but on Larry's personal Claude Max account:

### 1. Re-mint via `claude setup-token` on the personal account

On a machine with a browser, in a session where `claude` is authenticated as `larry@sealteamleaders.com` (org `848cafcc-765d-4a09-837d-eaf8d12b07cd`):

```bash
claude setup-token
```

- Open the URL in a browser **explicitly logged into the personal account** (incognito or explicit account-picker switch — wrong-account-in-browser gotcha applies).
- Approve, copy the `sk-ant-oat01-...` token.

### 2. Install on the droplet

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
token = sys.stdin.read().strip()
assert token and len(token) >= 20 and \" \" not in token and \"\n\" not in token, \"token validation failed\"
content = open(p).read()
new, n = re.subn(r\"^CLAUDE_CODE_OAUTH_TOKEN_TIER2=.*$\", f\"CLAUDE_CODE_OAUTH_TOKEN_TIER2={token}\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f\"OK: CLAUDE_CODE_OAUTH_TOKEN_TIER2 updated ({len(token)} chars)\")"'

pbcopy < /dev/null
```

### 3. Restart the watcher

Same command as Tier 1 step 3 — one restart covers both env-file edits if both tokens were updated in the same window.

### 4. Verify

Tier 2 only activates on Tier 1 fallback (rate-limit or auth_401), so a clean install may not exercise it organically. Two options:

- **Passive:** wait for an organic Tier 1 fallback (the watcher log will show `tier=tier2 ... auth=setup_token`).
- **Active mock:** temporarily `mv /home/larry/.claude/.credentials.json{,.bak}` and trigger a small beacon DM dispatch; the Tier 1 path should fail-auth and fall through to Tier 2 with `auth=setup_token`. **Restore the credentials.json immediately after** (`mv ...bak credentials.json`).

### 5. Update the registry + 6. Push the calendar event

Same as Tier 1, for `CLAUDE_CODE_OAUTH_TOKEN_TIER2`.

---

## Rollback

The window of irreversibility is narrow: the OLD setup-token is still valid for the rest of its ~1-year lifetime UNLESS you also click "Revoke" in the Anthropic console for the old token. Until then, if the new token install breaks, you can re-paste the OLD token into `.env.larry` and restart the watcher — auth resumes.

**Do not revoke the old token until verification (step 4) is green.** Both tokens valid in parallel is fine for the days/weeks between rotation and explicit revocation.

If you need to recover from a bad install (new token didn't land, new token rejected):

1. Re-run `claude setup-token` per step 1 — note the OLD token is recoverable only if you saved it (1Password, etc.); the Show-once display in the console can't be replayed.
2. Re-install per step 2.
3. Re-restart the watcher per step 3.
4. Re-verify per step 4.

If both tiers' setup-tokens are simultaneously broken, the agent OS falls through to the `credentials.json` (`CLAUDE_MAX_OAUTH`) path via the token-manager default — degraded but functional, with the auth_401-storm risk that PR #210 originally fixed. Restore as quickly as possible.

---

## Verification summary (used by Mirror's PR review for this rotation)

A successful rotation produces, in order:

1. `grep -c "^CLAUDE_CODE_OAUTH_TOKEN_TIER[12]=" /home/larry/credentials/.env.larry` → `2`.
2. `systemctl is-active ourliberty-inbox-watcher.service` → `active`.
3. Watcher log: at least one `tier=tier1 ... auth=setup_token` line and zero `auth_401` lines following it (Tier 1).
4. Registry timestamps moved forward by 365 days; `python3 scripts/validate_token_rotation_schedule.py` → exit 0.
5. Calendar event pushed +1 year (DM Beacon).

---

## Related

- Registry entries: `config/token-rotation-schedule.json` (search for `CLAUDE_CODE_OAUTH_TOKEN_TIER1`, `CLAUDE_CODE_OAUTH_TOKEN_TIER2`).
- Convention: `shared/credentials-discipline.md`.
- Primary-auth wiring: `scripts/agent_runner.py` — `_apply_tier_auth` (introduced in PR #210, 2026-05-30 auth_401-storm fix).
- Fallback-auth runbooks: `audit-claude-max-oauth.md` (Tier 1 `credentials.json` audit), `restore-larry-personal-claude-oauth-tier2.md` (Tier 2 `credentials.json` re-provision).
- Anthropic console: https://console.anthropic.com/settings/profile (subscription) and https://console.anthropic.com/settings/keys (active OAuth tokens).
