# Rotate Vercel token

**When to run this:** when Pulse DMs you that `VERCEL_TOKEN` is within 60 days of `next_rotation_due` (registered as `2027-05-19`), or unscheduled if the token is suspected leaked.

**Severity if lapsed:** high. `scripts/deploy_notifier.py` (E2.2) stops surfacing Vercel preview URLs; Larry stops seeing client-demo links. Recovery is just rotating; no data loss.

**Time required:** ~5 minutes wall-clock.

---

## Steps

### 1. Generate a new token

1. Open https://vercel.com/account/tokens in a browser logged in as `larry-yatch`.
2. Click **Create Token**.
3. Name: `ourliberty-droplet-deploy-notifier-<YYYY-MM>` (date suffix prevents collision with the old token while both exist briefly).
4. Scope: `Full Account` (only option on Hobby tier).
5. Expiration: `1 year` — matches the registry `cadence_days: 365`.
6. Click **Create**.
7. **Copy the token immediately.** Vercel shows it exactly once. Starts with `vcp_...`.

### 2. Install on the droplet

From your laptop's Terminal, with the token on your clipboard:

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
token = sys.stdin.read().strip()
assert token and len(token) >= 20 and \" \" not in token and \"\n\" not in token, \"token validation failed\"
content = open(p).read()
new, n = re.subn(r\"^VERCEL_TOKEN=.*$\", f\"VERCEL_TOKEN={token}\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f\"OK: VERCEL_TOKEN updated ({len(token)} chars)\")"'
```

Then clear your clipboard:

```bash
pbcopy < /dev/null
```

### 3. Verify the new token works

```bash
ssh larry@134.209.44.80 'source /home/larry/credentials/.env.larry && \
  curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
    -H "Authorization: Bearer $VERCEL_TOKEN" \
    https://api.vercel.com/v2/user'
```

Expected output: `HTTP 200`. If `401`, the install didn't take — repeat step 2 with a fresh copy from Vercel (the original Show-once value can't be retrieved).

### 4. Revoke the old token

Once the new token is verified, return to https://vercel.com/account/tokens and **delete** the previous `ourliberty-droplet-deploy-notifier-<old-date>` entry. Leaving the old one valid extends the blast radius of any earlier leak.

### 5. Update the registry

Edit `config/token-rotation-schedule.json`:

- `last_rotated_at` → today's date (`YYYY-MM-DD`)
- `next_rotation_due` → today + 365 days
- Open a PR titled `chore(creds): rotate Vercel token <YYYY-MM-DD>` — the discipline expects the registry stays in sync with reality.

### 6. Push the calendar event out by 1 year

Either DM Beacon (`@OLH_Beacon_bot`): *"Push the Vercel rotation calendar event date forward by 1 year"* — she will move the event via her Google Calendar MCP. Or do it manually in your personal Calendar UI.

The next Pulse cycle (within 4 hours) will read the updated registry and confirm the upcoming-rotations DM count is now zero for Vercel.

---

## Rollback

If something goes wrong:

1. **You still have the old token** — it's not deleted from Vercel until step 4. Re-paste the old token from wherever you have it (1Password, etc.), or just leave the file alone and the old token continues working until step 4 happens. The system never stops working between step 1 and step 4.
2. **You completed step 4 with a broken new token** — regenerate at step 1 and re-run step 2. The old (now deleted) token cannot be recovered.

The window in step 4 is the only point of irreversibility. Verify step 3 returns HTTP 200 *before* clicking Delete in step 4.

---

## Related

- Registry entry: `config/token-rotation-schedule.json` (search for `"name": "VERCEL_TOKEN"`)
- Convention: `shared/credentials-discipline.md`
- Calendar event: see registry's `calendar_event_url` field
- Original install narrative: `docs/operating-manual.md` Part II, Phase E2.0 section (when written)
