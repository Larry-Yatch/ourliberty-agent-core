# Rotate OL_DB_RO_URL

**Purpose:** `OL_DB_RO_URL` is a read-only OurLiberty database connection URL with an embedded RO-user credential. Rotate it whenever the underlying read-only DB user's credential is rotated or revoked (so the embedded secret in the URL is no longer valid), on suspected compromise of the URL, or as the outcome of a scope audit that re-provisions the RO role. Because the secret is embedded in the connection string, "rotating" this credential means obtaining a fresh URL and overwriting the env value — there is no separate token to re-mint.

**When to run this:**

- On suspected compromise/leak of the connection URL.
- When the read-only DB user's password is rotated or revoked at the DB provider — the old URL stops authenticating, so this URL must be updated in the same window.
- On a `credential-drift` or scope-audit trigger that re-provisions the RO role.

This is `rotation_type: revocation_only` — there is **no scheduled cadence and no calendar reminder** (consistent with the sibling `SUPABASE_URL` / `SUPABASE_DB_PASSWORD` entries). Rotation is event-driven, not date-driven.

**Severity if lapsed:** medium. A stale/invalid URL breaks the read-path (read-only queries fail to connect); there is no write or data-loss risk because the embedded credential is read-only by construction. As of registration (2026-06-05) no code consumer reads `OL_DB_RO_URL`, so a lapse currently has no live blast radius — re-confirm the consumer set (step 3) before assuming impact.

**Time required:** ~10 minutes wall-clock, dominated by provisioning the new connection string at the DB provider.

---

## Steps

### 1. Provision / obtain the new read-only DB connection URL

Get a fresh connection string for the read-only role from the DB provider. For the Supabase-backed project:

1. Open the Supabase dashboard → the relevant project → **Project Settings → Database**.
2. Under **Connection string**, select the pooled or direct connection appropriate to the consumer, scoped to the **read-only role** (not the service role / owner).
3. Copy the full connection URL, including the embedded RO-user credential.

Do not paste this value into any committed file, log, or chat. It goes only into `.env.larry` (step 2).

### 2. Install on the droplet

Update `OL_DB_RO_URL` in `/home/larry/credentials/.env.larry`. From your laptop's Terminal, with the new URL on your clipboard:

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
url = sys.stdin.read().strip()
assert url and \" \" not in url and \"\n\" not in url, \"url validation failed\"
content = open(p).read()
new, n = re.subn(r\"^OL_DB_RO_URL=.*$\", f\"OL_DB_RO_URL={url}\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(\"OK: OL_DB_RO_URL updated\")"'
```

The `re.subn` asserts exactly one replacement, so a missing or duplicated line fails loudly rather than silently corrupting the file.

### 3. Restart the services that read it

Identify the consumers first — this credential's consumer set may change over time:

```bash
ssh larry@134.209.44.80 'cd ~/agent-core && grep -rn OL_DB_RO_URL scripts/ systemd/ 2>/dev/null'
```

As of 2026-06-05 this grep returns **no consumers** — `OL_DB_RO_URL` is present in env but not yet wired into any script or systemd unit, so there is nothing to restart. If/when a read-path lands (e.g. a read-only query path in `scripts/dashboard_api.py` or an ingestion/shipper reader), restart that service so it picks up the new value from `EnvironmentFile=`:

```bash
# Example, once a consumer exists — substitute the real unit name:
ssh larry@134.209.44.80 'sudo systemctl restart ourliberty-dashboard-api.service && \
  sleep 2 && sudo systemctl is-active ourliberty-dashboard-api.service'
```

Expected output: `active`.

### 4. Verify the read-path works post-rotation

If a consumer exists, confirm a read-only query succeeds with the new URL. With no consumer wired yet, verify the value is installed and well-formed (key-only, no value printed):

```bash
ssh larry@134.209.44.80 'grep -c "^OL_DB_RO_URL=." /home/larry/credentials/.env.larry'
# Expected: 1  (single non-empty OL_DB_RO_URL line)
```

Once a consumer lands, replace this with an actual read-only smoke query (e.g. `SELECT 1` against the RO connection) so post-rotation verification exercises the live path.

### 5. Update the registry

Edit `config/token-rotation-schedule.json`, in the `OL_DB_RO_URL` entry:

- `last_rotated_at` → today's date (`YYYY-MM-DD`).
- Leave `next_rotation_due` and `calendar_event_url` as `null` (revocation_only — no scheduled date).

Open a PR titled `chore(creds): rotate OL_DB_RO_URL <YYYY-MM-DD>`. Mirror's regression gate runs; merge once green. The next Pulse cycle (within 4h) reads the updated registry.

> If Larry decides this credential should rotate on a fixed cadence instead of event-driven, change `rotation_type` to `scheduled`, set `cadence_days` + `next_rotation_due`, and dispatch Beacon to create the annual calendar event (paste the URL into `calendar_event_url`). That's a follow-up, not part of a revocation_only rotation.

---

## Rollback

There is no irreversible step. The only mutation is overwriting `OL_DB_RO_URL` in `.env.larry`. If the new URL is bad (wrong role, typo, fails to connect), re-run step 2 with a known-good URL and restart any consumer. The previous value is not retained anywhere by this procedure, so keep the prior URL available (e.g. in your paste buffer / 1Password) until step 4 verifies the new one.

---

## Related

- Registry entry: `config/token-rotation-schedule.json` (search for `"name": "OL_DB_RO_URL"`)
- Convention: `shared/credentials-discipline.md`
- Sibling runbooks (Supabase DB credentials): `docs/runbooks/rotate-supabase-db-password.md`, `docs/runbooks/rotate-supabase-keys.md`
- Drift healer: `scripts/heal_credential_registry_drift.py` (flags `MISSING_REGISTRY_ENTRY` / `MISSING_CREDENTIAL` every 6h)
