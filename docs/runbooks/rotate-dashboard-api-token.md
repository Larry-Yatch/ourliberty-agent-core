# Rotate Dashboard API token

**When to run this:** when Pulse DMs you that `DASHBOARD_API_TOKEN` is within 60 days of `next_rotation_due` (registered as `2027-05-20`), or unscheduled if the token is suspected leaked.

**Severity if lapsed:** high. The Next.js dashboard at `https://dashboard.ourliberty.dev` (E3.2) stops being able to fetch from `https://api.ourliberty.dev` (E3.3 → `scripts/dashboard_api.py` E3.1) — every panel renders a 401 error. The droplet API itself keeps running; only the UI degrades. Recovery is just rotating; no data loss.

**Time required:** ~10 minutes wall-clock. Slightly longer than `rotate-vercel-token` because the token lives in two places that must change together.

**Critical:** the token is shared between the droplet AND the Vercel project env vars. Both must rotate in the same window or the dashboard 401s. Do not stop after step 2 — keep going to step 4 immediately.

---

## Steps

### 1. Generate a new token

On the droplet (or any machine with Python 3):

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Copy the 43-character output. Starts with arbitrary letters/digits/`-`/`_`; no `vcp_` or other prefix.

### 2. Install on the droplet

From your laptop's Terminal, with the new token on your clipboard:

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
token = sys.stdin.read().strip()
assert token and len(token) >= 32 and \" \" not in token and \"\n\" not in token, \"token validation failed\"
content = open(p).read()
new, n = re.subn(r\"^DASHBOARD_API_TOKEN=.*$\", f\"DASHBOARD_API_TOKEN={token}\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f\"OK: DASHBOARD_API_TOKEN updated ({len(token)} chars)\")"'
```

Then restart the droplet service so it picks up the new token from `EnvironmentFile=`:

```bash
ssh larry@134.209.44.80 'sudo systemctl restart ourliberty-dashboard-api.service && \
  sleep 2 && \
  sudo systemctl is-active ourliberty-dashboard-api.service'
```

Expected output: `active`. If `failed`, fall back to step 7 (rollback) immediately.

### 3. Update the Vercel project env vars

The Vercel-side env var is set on the `ourliberty-dashboard` project across Production, Preview, AND Development environments. Update all three so previews built off feature branches don't drift.

1. Open https://vercel.com/larry-yatch/ourliberty-dashboard/settings/environment-variables in a browser logged in as `larry-yatch`.
2. Find the `DASHBOARD_API_TOKEN` row.
3. Click the `⋯` menu → **Edit**.
4. Paste the new token value.
5. Verify all three environments (Production / Preview / Development) are checked.
6. Click **Save**.

### 4. Restart the droplet service

Already done in step 2 — but re-verify after the Vercel side is updated:

```bash
ssh larry@134.209.44.80 'sudo systemctl restart ourliberty-dashboard-api.service'
```

Why again: if you noticed a typo in step 2 and updated `.env.larry` between then and now, the second restart ensures the running uvicorn process holds the same token Vercel does.

### 5. Trigger a Vercel redeploy to pick up the new env

Env-var changes don't apply until the next build. Push a no-op commit or hit the redeploy button:

Option A (UI): Vercel → ourliberty-dashboard → Deployments → most recent Production → ⋯ menu → **Redeploy** → confirm.

Option B (CLI):

```bash
cd ~/dev/ourliberty-dashboard  # (your local clone)
git commit --allow-empty -m "chore: redeploy to pick up rotated DASHBOARD_API_TOKEN"
git push origin main
```

Wait ~30 s and confirm the new deployment shows `Ready` in Vercel.

### 6. Verify both ends with curl

From your laptop (the token is in your clipboard; if you cleared it, `ssh larry@... 'grep DASHBOARD /home/larry/credentials/.env.larry'`):

```bash
TOKEN='<paste-token-here>'

# Droplet side (via SSH tunnel — service is loopback-only at this phase).
ssh -L 8001:127.0.0.1:8000 larry@134.209.44.80 -N -f
curl -sS -H "X-Dashboard-Token: $TOKEN" http://127.0.0.1:8001/health
# expected: {"status":"ok",...}

# Vercel side (the dashboard makes a request via Nginx — once E3.3 lands).
# For now (pre-E3.3), the UI side is not externally reachable. Skip until
# api.ourliberty.dev is live; then:
#   curl -sS -H "X-Dashboard-Token: $TOKEN" https://api.ourliberty.dev/health
```

Expected output: `{"status":"ok",...}`. If `401`, the install didn't take on the droplet — repeat step 2. If `503` or hang, the service crashed on restart — check `journalctl -u ourliberty-dashboard-api.service -n 50`.

### 7. Revoke the old token

Once both sides verify with the new token:

```bash
# On the droplet — make sure no leftover comment or duplicate line holds
# the old value. Best done by re-reading the file to confirm the single
# DASHBOARD_API_TOKEN line is the new value:
ssh larry@134.209.44.80 'grep DASHBOARD_API_TOKEN /home/larry/credentials/.env.larry'
# Expected: a single line, with the new token.
```

The new token in `.env.larry` already overwrote the old in step 2, so this is a verification step rather than a separate revoke. The "old token" exists only in any backups or paste buffers — clear those:

```bash
pbcopy < /dev/null
# Also clear any 1Password drafts, Notes app jots, etc.
```

### 8. Update the registry

Edit `config/token-rotation-schedule.json`:

- `last_rotated_at` → today's date (`YYYY-MM-DD`)
- `next_rotation_due` → today + 365 days

Open a PR titled `chore(creds): rotate DASHBOARD_API_TOKEN <YYYY-MM-DD>`. Mirror's regression gate will run; merge once green.

### 9. Push the calendar event forward by 1 year

Either DM Beacon (`@OLH_Beacon_bot`): *"Push the DASHBOARD_API_TOKEN rotation calendar event date forward by 1 year"* — she'll move the event via her Google Calendar MCP. Or do it manually in your personal Calendar UI if you prefer.

The next Pulse cycle (within 4 hours) will read the updated registry and confirm the upcoming-rotations DM count is now zero for Dashboard.

---

## Rollback

The token lives in two places. The window of inconsistency is between step 2 (droplet has new token) and step 5 (Vercel redeploys with new token). During that window the dashboard 401s. To minimize: do step 2 → step 3 → step 5 in rapid succession.

If something goes wrong before step 5 completes:

1. **You still have the old token** — it's not deleted from anywhere external (Vercel keeps env-var history, droplet has the previous value in shell history if you used the inline-Python install). If the new token install was bad, re-paste the old token via step 2 with the old value, restart the service, and start over from step 1 with a fresh token.
2. **You've completed step 3 but the Vercel side seems to have not picked up** — verify by running step 5 (redeploy). Vercel env-var changes need a build to apply; until then the deployed UI uses the old token.
3. **You've completed all 5 steps but the dashboard 401s** — run step 6 to bisect: if droplet-side curl works, the Vercel build hasn't picked up the new env yet. Retry the redeploy. If droplet-side curl fails, the droplet hasn't picked up `.env.larry` — restart the service again.

There is no irreversible step. Both sides can be re-overwritten freely.

---

## Related

- Registry entry: `config/token-rotation-schedule.json` (search for `"name": "DASHBOARD_API_TOKEN"`)
- Convention: `shared/credentials-discipline.md`
- Calendar event: see registry's `calendar_event_url` field
- Sibling runbook (similar 2-location pattern): `docs/runbooks/rotate-vercel-token.md`
- Service file: `systemd/ourliberty-dashboard-api.service`
- API source: `scripts/dashboard_api.py`
- Spec: `agents/beacon/specs/dashboard-api-e3-1.md`
