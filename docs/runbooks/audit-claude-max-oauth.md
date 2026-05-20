# Audit the Claude Max OAuth credential

**When to run this:** when Pulse DMs you that `CLAUDE_MAX_OAUTH` is within 60 days of `next_rotation_due` (registered as `2027-05-18`), or unscheduled if the credential is suspected leaked.

This is a **subscription + billing audit**, not a rotation. The access token auto-refreshes via the stored refresh token; the credential's rotation cadence exists so we re-check the subscription tier and credential-storage hygiene once a year.

**Severity if lapsed:** critical. Every agent on the droplet (Beacon, Forge, Mirror, Pulse) stops being able to run `claude` invocations — that's the entire agent OS dispatching layer.

**Time required:** ~15 minutes for the audit; ~30 minutes if a forced re-auth is needed.

---

## Audit checklist

### 1. Confirm the credential file is intact

```bash
ssh larry@134.209.44.80 "ls -la /home/larry/.claude/.credentials.json && \
  python3 -c \"
import json, pathlib
p = pathlib.Path('/home/larry/.claude/.credentials.json')
data = json.loads(p.read_text())
oauth = data.get('claudeAiOauth', {})
print(f'accessToken set: {bool(oauth.get(\\\"accessToken\\\"))}')
print(f'refreshToken set: {bool(oauth.get(\\\"refreshToken\\\"))}')
print(f'expiresAt: {oauth.get(\\\"expiresAt\\\")}')\""
```

Expected: both tokens present; `expiresAt` is a future epoch millis. If `expiresAt` is in the past, the next `claude` invocation should auto-refresh — confirm by running one and re-checking.

### 2. Verify a fresh agent invocation works

```bash
ssh larry@134.209.44.80 "claude --print 'reply with the literal string OK and nothing else'"
```

Expected: prints `OK`. Failure modes:
- `Authentication failed` → access token expired and refresh failed. Either the refresh token is rejected (subscription canceled, account locked) or the keychain file is corrupted. Force re-auth per Section 3.
- `Rate limited` → not an auth issue; throttle.

### 3. Audit the subscription

Open https://console.anthropic.com/settings/profile in a browser logged in as `agent.beacon.ourliberty@gmail.com`. Verify:

- **Subscription is active**: Max plan still showing, next-billing-date in the future.
- **Tier matches actual usage**: cross-reference recent cost data from `~/agents/blackboard/ledger/` (Pulse Check I weekly report). If you're consistently near a tier cap, consider upgrading. If you're consistently at <30% of the tier limit, consider downgrading.
- **Active sessions list**: a "Sign out everywhere" button is the revocation primitive — only click it if you want to force re-auth (Section 3 below).

### 4. Audit storage hygiene

- `/home/larry/.claude/.credentials.json` should be `0600` perms.
- The file should be owned by `larry:larry`.
- No `.bak` or `.old` copies should exist alongside it.

```bash
ssh larry@134.209.44.80 "stat -c '%a %U:%G' /home/larry/.claude/.credentials.json && \
  find /home/larry/.claude/ -name '*credentials*' -type f"
```

Expected: `600 larry:larry` and exactly one path output.

### 5. Update the registry

Edit `config/token-rotation-schedule.json` for `CLAUDE_MAX_OAUTH`:
- `last_rotated_at` → today (`YYYY-MM-DD`) (treated as "last audited" for `auto_refresh` entries)
- `next_rotation_due` → today + 365 days
- `notes` → append `Audited <YYYY-MM-DD>; subscription tier <Max/etc>; recent monthly spend ~$X.`

Open a PR titled `chore(creds): audit Claude Max OAuth <YYYY-MM-DD>` and push the matching calendar event out by 1 year.

---

## Forced re-auth (Section 3 — use only when needed)

If audit step 2 fails with `Authentication failed`, OR you intentionally clicked "Sign out everywhere" in the Anthropic console, the credential needs to be re-issued via the PTY orchestrator pattern from Phase E5 (the droplet has no browser; `claude auth login` is interactive).

The pattern (full detail in `agents/forge/memory/2026-05-18.md` from the E5 install):

1. ssh to the droplet.
2. Run `claude auth login --inherit-env` — this prints a device code URL.
3. Copy the URL to your laptop browser; sign in as `agent.beacon.ourliberty@gmail.com`; authorize.
4. The CLI completes the flow and writes to `/home/larry/.claude/.credentials.json`.
5. Re-verify per Section 2.

The re-auth path is preserved here for reference; rotating without a known auth failure is unnecessary — `auto_refresh` means the keychain self-maintains.

---

## Related

- Registry entry: `config/token-rotation-schedule.json` (search for `CLAUDE_MAX_OAUTH`)
- Convention: `shared/credentials-discipline.md`
- E5 install narrative: `agents/forge/memory/` for the PTY orchestrator pattern
- Anthropic console: https://console.anthropic.com/settings/profile
