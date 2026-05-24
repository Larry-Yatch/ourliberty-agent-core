# Rotate Supabase keys

**When to run this:** when Pulse DMs you that `SUPABASE_SERVICE_ROLE_KEY` is within 60 days of its `next_rotation_due` (90-day cadence), or unscheduled if any of the three keys is suspected leaked.

**Severity if lapsed:**
- `SUPABASE_SERVICE_ROLE_KEY`: **critical** — RLS-bypassing admin key. Any process holding it has full DB read/write. Lapsed key = the dashboard + any droplet-side Supabase write/read stops working.
- `SUPABASE_ANON_KEY`: medium — RLS-gated; lapsed key means anon-context reads fail but admin paths keep working.
- `SUPABASE_URL`: low — not a secret. "Lapsed" only applies if the project URL itself changes (e.g. project migration).

**Time required:** ~10 minutes wall-clock for the service-role rotation; ~5 minutes for the anon-key rotation.

**Three keys, three rotation modes:**

| Key | Rotation cadence | Notes |
|---|---|---|
| `SUPABASE_URL` | Never (it's the project URL, not a secret) | Don't rotate. Stored in `.env.larry` for consistency with the rest of the connection details. |
| `SUPABASE_ANON_KEY` | On suspected leak only (revocation_only) | Anon key has Row-Level-Security applied; relatively low blast radius. |
| `SUPABASE_SERVICE_ROLE_KEY` | 90d scheduled + on any suspected leak | **HIGH BLAST RADIUS — bypasses RLS.** This is the dangerous one. |

---

## Rotating the service-role key

This is the critical path. The service-role key is RLS-bypassing — anyone holding it has full DB read/write. Rotate on schedule (90d) and any time it's suspected exposed.

### 1. WARNING — there is no zero-downtime rotation

Supabase regenerates the service-role secret atomically: clicking **Reset service_role secret** in the UI invalidates the old value the moment the new value is shown. Plan for a brief outage of any cron / agent worker that uses this key, OR coordinate so the new key is installed in `.env.larry` and services restarted within seconds of regeneration.

Best practice: regenerate during a low-traffic window, have the install command pre-typed in your terminal so paste-and-go is one keystroke.

### 2. Generate a new service-role key

1. Open `https://app.supabase.com/project/<project-id>/settings/api` in a browser logged in as `agent.beacon.ourliberty@gmail.com`.
2. Scroll to the **Project API keys** section.
3. Find the `service_role` row and click **Reset service_role secret**.
4. Confirm the warning dialog. Supabase generates a new JWT (starts with `eyJ...`, ~200 chars).
5. **Copy the new value immediately.** Supabase shows it exactly once after reset; if you navigate away, you must reset again.

### 3. Install on the droplet

From your laptop's Terminal, with the new key on your clipboard:

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
token = sys.stdin.read().strip()
assert token and len(token) >= 100 and token.startswith(\"eyJ\") and \" \" not in token and \"\n\" not in token, \"service-role key validation failed\"
content = open(p).read()
new, n = re.subn(r\"^SUPABASE_SERVICE_ROLE_KEY=.*$\", f\"SUPABASE_SERVICE_ROLE_KEY={token}\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f\"OK: SUPABASE_SERVICE_ROLE_KEY updated ({len(token)} chars)\")"'
```

Then clear your clipboard:

```bash
pbcopy < /dev/null
```

### 4. Restart any service that reads it

Once Supabase has consumers (E4.3 `pm_writer` and later), restart them so they re-read `.env.larry`:

```bash
ssh larry@134.209.44.80 'sudo systemctl restart ourliberty-pm-writer.service'   # E4.3+
# Add other services as they come online.
```

Until E4.3 ships, no service currently holds a long-lived Supabase client connection — the new key is picked up on the next ad-hoc invocation.

### 5. Verify the new key works

```bash
ssh larry@134.209.44.80 'source /home/larry/credentials/.env.larry && \
  curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
    -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
    -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
    "$SUPABASE_URL/rest/v1/"'
```

Expected output: `HTTP 200`. If `401`, the install didn't take — repeat step 3 with a fresh copy (Supabase's show-once value can't be retrieved if you navigated away; you'd need to **Reset** again).

The `/rest/v1/` root endpoint always exists on a fresh Supabase project — it returns 200 with an empty body when the service-role key is valid, regardless of whether any tables exist yet.

### 6. Update the registry

Edit `config/token-rotation-schedule.json`:

- `last_rotated_at` → today's date (`YYYY-MM-DD`)
- `next_rotation_due` → today + 90 days

Open a PR titled `chore(creds): rotate Supabase service-role key <YYYY-MM-DD>` — the discipline expects the registry stays in sync with reality.

### 7. Push the calendar event out by 90 days

Either DM Beacon (`@OLH_Beacon_bot`): *"Push the Supabase service-role rotation calendar event date forward by 90 days"* — she will move the event via her Google Calendar MCP. Or do it manually in your personal Calendar UI.

The next Pulse cycle (within 4 hours) will read the updated registry and confirm the upcoming-rotations DM count is now zero for Supabase.

---

## Rotating the anon key

Anon-key rotation is lower-stakes — RLS gates every read, so the blast radius is the public read-policy surface area, not the full DB.

### 1. Generate a new anon key

1. Open `https://app.supabase.com/project/<project-id>/settings/api`.
2. Find the `anon public` row and click **Reset anon key**.
3. Confirm. Copy the new JWT (also `eyJ...`).

### 2. Install on the droplet

Same pbpaste-ssh pattern as the service-role key, with the var name swapped:

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
token = sys.stdin.read().strip()
assert token and len(token) >= 100 and token.startswith(\"eyJ\") and \" \" not in token and \"\n\" not in token, \"anon key validation failed\"
content = open(p).read()
new, n = re.subn(r\"^SUPABASE_ANON_KEY=.*$\", f\"SUPABASE_ANON_KEY={token}\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f\"OK: SUPABASE_ANON_KEY updated ({len(token)} chars)\")"'
```

Then `pbcopy < /dev/null`.

### 3. Verify

The verification exercises an anon-context request — same root endpoint, but using the anon key. RLS enforcement means a 200 response only confirms auth, not data access.

```bash
ssh larry@134.209.44.80 'source /home/larry/credentials/.env.larry && \
  curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
    -H "apikey: $SUPABASE_ANON_KEY" \
    -H "Authorization: Bearer $SUPABASE_ANON_KEY" \
    "$SUPABASE_URL/rest/v1/"'
```

Expected: `HTTP 200`.

### 4. Update the registry

The anon key is `rotation_type: revocation_only` (no scheduled cadence) — only update `last_rotated_at` and leave `next_rotation_due` null. There's no calendar event to push.

PR title: `chore(creds): rotate Supabase anon key <YYYY-MM-DD>`.

---

## Updating SUPABASE_URL

Don't rotate. If the URL itself changes (e.g. project migration to a different region, or a paid-tier upgrade that moves the project), update `.env.larry` and the registry entry's `notes` field, but don't bump `last_rotated_at` — that field is reserved for credential rotations, not metadata changes.

If you do need to update it (rare):

1. Run the pbpaste-ssh pattern with `SUPABASE_URL` as the target var. The validation should be relaxed: `assert token.startswith("https://") and ".supabase.co" in token`.
2. Update the registry entry's `notes` field to record the migration date + reason.
3. Restart any consumer services that cache the URL value.

---

## Rollback

If something goes wrong:

### Service-role rotation went bad

1. **Service-role rotation is the only one with NO recovery path** for the old key — Supabase's reset invalidates the prior secret immediately. If the new key is broken (typo on paste, file write failed, etc.):
   - Re-run **Reset service_role secret** in the Supabase UI to generate ANOTHER new key.
   - Run step 3 again with the freshly-generated key.
   - You'll likely have a few minutes of service downtime during this; this is why step 1's warning matters.
2. If the install succeeded but services can't read the new key, check `.env.larry`'s file mode (`stat /home/larry/credentials/.env.larry`) — should be `0600`. The `os.chmod(tmp, 0o600)` in the install one-liner guarantees this; if you edited manually with `vim`, the mode may have changed.

### Anon-key rotation went bad

Same recovery as service-role — re-reset and re-install. The anon key is lower-stakes so the impact of a botched rotation is smaller (only the anon-context reads fail; service-role paths keep working).

### URL update went bad

The URL change requires a coordinated change of all consumers. If the new URL is wrong:
- Revert `.env.larry` to the prior URL manually (the file is mode 0600; edit with `sudo -u larry vim`).
- Restart consumer services.

There is no Supabase-side action needed — the URL is read-only metadata.

---

## Related

- Convention: `shared/credentials-discipline.md`
- Registry entries: `config/token-rotation-schedule.json` (search for `"name": "SUPABASE_`)
- Setup runbook (first-time): `docs/runbooks/setup-supabase-pm-project.md`
- Sibling pattern (Vercel token rotation): `docs/runbooks/rotate-vercel-token.md`
- Supabase API key docs: https://supabase.com/docs/guides/api/api-keys
- RLS guide (why service-role rotation matters): https://supabase.com/docs/guides/database/postgres/row-level-security
