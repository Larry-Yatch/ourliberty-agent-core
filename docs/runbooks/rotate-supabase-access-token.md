# Rotate Supabase access token

**When to run this:** when Pulse DMs you that `SUPABASE_ACCESS_TOKEN` is within 60 days of `next_rotation_due` (registered as `2027-05-26`, 365d cadence), or unscheduled if the token is suspected leaked.

**Severity if lapsed:** medium. `supabase` CLI on the droplet stops working for any Management API call (`supabase link`, `supabase db push`, `supabase projects list`). Migrations can still be applied from Larry's Mac CLI as a fallback. Recovery is just rotating; no data loss.

**Time required:** ~5 minutes wall-clock.

**Not to be confused with:** `SUPABASE_SERVICE_ROLE_KEY` (RLS-bypassing JWT for the data plane — see `rotate-supabase-keys.md`) or `SUPABASE_DB_PASSWORD` (Postgres password — see `rotate-supabase-db-password.md`). The access token is the personal Management API token that scopes the CLI to Larry's Supabase account.

---

## Steps

### 1. Regenerate the access token

1. Open https://supabase.com/dashboard/account/tokens in a browser logged in as `agent.beacon.ourliberty@gmail.com`.
2. Click **Generate new token**.
3. Name: `droplet-cli-YYYYMMDD` (today's date — the suffix prevents collision with the old token while both exist briefly).
4. Click **Generate token**.
5. **Copy the token immediately.** Supabase shows it exactly once. Starts with `sbp_` (NOT `eyJ` — that prefix is the data-plane JWT, a different credential).

### 2. Install on the droplet

From your laptop's Terminal, with the token on your clipboard:

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
token = sys.stdin.read().strip()
assert token and token.startswith(\"sbp_\") and len(token) >= 20 and \" \" not in token and \"\n\" not in token, \"access-token validation failed (expected sbp_ prefix)\"
content = open(p).read()
new, n = re.subn(r\"^SUPABASE_ACCESS_TOKEN=.*$\", f\"SUPABASE_ACCESS_TOKEN='\''{token}'\''\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f\"OK: SUPABASE_ACCESS_TOKEN updated ({len(token)} chars)\")"'
```

Then clear your clipboard:

```bash
pbcopy < /dev/null
```

The value is single-quoted on disk (codified item #57): `SUPABASE_ACCESS_TOKEN='sbp_...'`. This is the safe default for `.env` values consumed via `set -a && source <file> && set +a`.

### 3. Verify the new token works

```bash
ssh larry@134.209.44.80 'set -a && source /home/larry/credentials/.env.larry && set +a && \
  PATH=~/.local/share/supabase:$PATH supabase projects list'
```

Expected output: a table listing `ourliberty-pm-dashboard` (project ref `ezldtkbhexyrgujqmxpd`). If you see `Invalid access token` or similar, the install didn't take — repeat step 2 with a fresh copy from the Supabase dashboard (the Show-once value can't be retrieved).

### 4. Revoke the old token

Once the new token is verified, return to https://supabase.com/dashboard/account/tokens and **delete** the previous `droplet-cli-<old-date>` entry. Leaving the old one valid extends the blast radius of any earlier leak.

### 5. Update the registry

Edit `config/token-rotation-schedule.json`:

- `last_rotated_at` → today's date (`YYYY-MM-DD`)
- `next_rotation_due` → today + 365 days

Open a PR titled `chore(creds): rotate Supabase access token <YYYY-MM-DD>` — the discipline expects the registry stays in sync with reality.

Then DM Beacon (`@OLH_Beacon_bot`): *"Push the Supabase access-token rotation calendar event date forward by 1 year"* — she will move the event via her Google Calendar MCP. Or do it manually in your personal Calendar UI.

The next Pulse cycle (within 4 hours) will read the updated registry and confirm the upcoming-rotations DM count is now zero for `SUPABASE_ACCESS_TOKEN`.

---

## Rollback

If something goes wrong:

1. **You still have the old token** — it's not deleted from Supabase until step 4. Re-paste the old token from wherever you have it, or just leave the file alone and the old token continues working until step 4 happens. The system never stops working between step 1 and step 4.
2. **You completed step 4 with a broken new token** — regenerate at step 1 and re-run step 2. The old (now revoked) token cannot be recovered.
3. **You're locked out of the droplet CLI entirely** — Larry's Mac still has its own `SUPABASE_ACCESS_TOKEN` configured for the Supabase CLI. Apply any urgent migrations from the Mac while you re-rotate.

The window in step 4 is the only point of irreversibility. Verify step 3 lists the project *before* clicking Revoke in step 4.

---

## Related

- Registry entry: `config/token-rotation-schedule.json` (search for `"name": "SUPABASE_ACCESS_TOKEN"`)
- Convention: `shared/credentials-discipline.md`
- Sibling — DB password: `docs/runbooks/rotate-supabase-db-password.md`
- Sibling — data-plane keys: `docs/runbooks/rotate-supabase-keys.md`
- Canonical migration apply path: `docs/runbooks/apply-supabase-migrations-from-droplet.md`
- Original install narrative: `docs/operating-manual.md` Part II, "Supabase CLI moved to droplet (2026-05-26)"
