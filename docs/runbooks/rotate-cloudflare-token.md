# Rotate Cloudflare API token

**STATUS: STUB.** The `CLOUDFLARE_API_TOKEN` slot in `.env.larry` is currently empty as of 2026-05-19 — no agent OS code reads it. When DNS automation is wired (likely as part of E2 deploy infrastructure work or later prototype builds), populate the slot AND fill in this runbook with real install/verify/revoke procedures.

**When to run this (future):** when Pulse DMs you that `CLOUDFLARE_API_TOKEN` is within 60 days of its `next_rotation_due`, or on suspected leak.

**Severity if lapsed (future):** depends on what's wired. If only DNS, medium (DNS edits stop until rotated; existing records continue to resolve). If wired into a deploy path, high.

---

## Steps (template — refine when wired)

### 1. Generate a new token

1. Open https://dash.cloudflare.com/profile/api-tokens in a browser logged in as the account that owns `ourliberty.dev`.
2. Click **Create Token**.
3. Use the **Edit zone DNS** template — or **Create Custom Token** if you need a narrower scope.
4. Scope: **Zone: DNS: Edit** restricted to **Specific zone: ourliberty.dev** (NEVER all zones — minimum-privilege).
5. TTL: pick the cadence that matches the registry entry (likely 365d).
6. Click **Create Token** and copy the value immediately. Starts with a long hex string.

### 2. Install on the droplet

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
token = sys.stdin.read().strip()
assert token and len(token) >= 20, \"token validation failed\"
content = open(p).read()
new, n = re.subn(r\"^CLOUDFLARE_API_TOKEN=.*$\", f\"CLOUDFLARE_API_TOKEN={token}\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f\"OK: CLOUDFLARE_API_TOKEN updated ({len(token)} chars)\")"'
pbcopy < /dev/null
```

### 3. Verify the new token

```bash
ssh larry@134.209.44.80 'source /home/larry/credentials/.env.larry && \
  curl -sS https://api.cloudflare.com/client/v4/user/tokens/verify \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | python3 -m json.tool'
```

Expected: `success: true` and `status: active` in the JSON response.

### 4. Revoke the old token

Return to https://dash.cloudflare.com/profile/api-tokens and **delete** the previous token (its row will be named with the previous date suffix per the naming convention from step 1).

### 5. Update the registry

When the credential is actually wired, add the registry entry first (per `shared/credentials-discipline.md` 4-artifact rule). Then on each rotation, push `last_rotated_at` + `next_rotation_due`.

---

## When to remove this stub

If by E2 (Vercel deploy + droplet ops work) the Cloudflare token is decisively not wired:
- Remove the empty `CLOUDFLARE_API_TOKEN=` line from `.env.larry`.
- Delete this stub file.
- Note in the operating manual that DNS is managed manually.

If/when it IS wired, replace the stub language above with verified install/verify steps and add the registry entry per the 4-artifact discipline.

---

## Related

- Convention: `shared/credentials-discipline.md`
- Cloudflare token docs: https://developers.cloudflare.com/fundamentals/api/get-started/create-token/
