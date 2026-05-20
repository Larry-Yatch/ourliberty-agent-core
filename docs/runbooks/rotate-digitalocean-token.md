# Rotate DigitalOcean API token

**STATUS: STUB.** The `DIGITALOCEAN_TOKEN` slot in `.env.larry` is currently empty as of 2026-05-19. The original Phase A template comment said "rotate every 90 days" but nothing in the agent OS code reads this token — when droplet automation is wired (planned scope: snapshot/restore ops, future scaling automation in E-late or F), populate the slot AND fill in this runbook with real procedures.

**When to run this (future):** when Pulse DMs you that `DIGITALOCEAN_TOKEN` is within 60 days of its `next_rotation_due` (cadence likely 90d per DO's recommended posture for full-account-scoped tokens), or on suspected leak.

**Severity if lapsed (future):** high. Droplet snapshot / restore / snapshot-from-image flows stop. No data loss unless the droplet itself fails during the lapse.

---

## Steps (template — refine when wired)

### 1. Generate a new token

1. Open https://cloud.digitalocean.com/account/api/tokens.
2. Click **Generate New Token**.
3. Name: `ourliberty-droplet-ops-<YYYY-MM>`.
4. Scope: prefer **Custom Scopes** narrowed to specific actions (e.g. `droplet:read`, `droplet:write` only). Full-account is broad — minimum-privilege is the discipline.
5. Expiration: 90 days (per DO's recommended cadence for read+write tokens).
6. Click **Generate** and copy the value immediately. Starts with `dop_v1_`.

### 2. Install on the droplet

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
token = sys.stdin.read().strip()
assert token and token.startswith(\"dop_v1_\"), \"token validation failed\"
content = open(p).read()
new, n = re.subn(r\"^DIGITALOCEAN_TOKEN=.*$\", f\"DIGITALOCEAN_TOKEN={token}\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f\"OK: DIGITALOCEAN_TOKEN updated ({len(token)} chars)\")"'
pbcopy < /dev/null
```

### 3. Verify the new token

```bash
ssh larry@134.209.44.80 'source /home/larry/credentials/.env.larry && \
  curl -sS https://api.digitalocean.com/v2/account \
    -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" | python3 -m json.tool'
```

Expected: `account.email` matches the DO account; `account.status` is `active`.

### 4. Revoke the old token

Return to https://cloud.digitalocean.com/account/api/tokens and **delete** the previous `ourliberty-droplet-ops-<old-date>` entry.

### 5. Update the registry

Push `last_rotated_at` + `next_rotation_due` (cadence: 90 days). Push the matching calendar event out by 90 days.

---

## When to remove this stub

If by F-phase planning the DigitalOcean token is decisively not wired:
- Remove the empty `DIGITALOCEAN_TOKEN=` line from `.env.larry`.
- Delete this stub file.
- Note in the operating manual that droplet ops are manual via the DO web console.

If/when it IS wired, replace the stub language above with verified install/verify steps and add the registry entry per the 4-artifact discipline.

---

## Related

- Convention: `shared/credentials-discipline.md`
- DigitalOcean token docs: https://docs.digitalocean.com/reference/api/create-personal-access-token/
