# Rotate Desktop Ingest token

**When to run this:** when Pulse DMs you that `DESKTOP_INGEST_TOKEN` is within 60 days of `next_rotation_due` (registered as `2027-06-09`), or unscheduled if the token is suspected leaked.

**Severity if lapsed:** medium. The desktop session-ingest (write) path breaks — the desktop Claude Code hook's `desktop_session_*` POSTs to `scripts/dashboard_api.py` start returning `401` and no desktop-session cards appear on the dashboard. Nothing else degrades: the token's blast radius is limited to writing `desktop_session_*` events as `desktop-claude` (the handler pins the agent and rejects any other event type). No data loss; recovery is just rotating.

**Time required:** ~10 minutes wall-clock. Like `rotate-dashboard-api-token`, the token lives in two places that must change together — the droplet env AND the desktop client that holds a copy.

**Critical:** this is a **shared secret** — the same value lives in the droplet's `.env.larry` AND in the desktop client that sends the `X-Ingest-Token` header. Both must change in the same window or desktop ingest `401`s. Do not stop after step 2 — keep going to step 3 (update the desktop client) immediately.

---

## Steps

### 1. Generate a new token

On the droplet (or any machine with Python 3):

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Copy the 43-character output. Starts with arbitrary letters/digits/`-`/`_`; no prefix. (This is a self-minted shared secret — there's no provider to mint it for you; the droplet API just compares the header to whatever value is in env, constant-time.)

### 2. Install on the droplet

From your laptop's Terminal, with the new token on your clipboard:

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
token = sys.stdin.read().strip()
assert token and len(token) >= 32 and \" \" not in token and \"\n\" not in token, \"token validation failed\"
content = open(p).read()
new, n = re.subn(r\"^DESKTOP_INGEST_TOKEN=.*$\", f\"DESKTOP_INGEST_TOKEN={token}\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f\"OK: DESKTOP_INGEST_TOKEN updated ({len(token)} chars)\")"'
```

Then restart the droplet service so it picks up the new token from `EnvironmentFile=`:

```bash
ssh larry@134.209.44.80 'sudo systemctl restart ourliberty-dashboard-api.service && \
  sleep 2 && \
  sudo systemctl is-active ourliberty-dashboard-api.service'
```

Expected output: `active`. If `failed`, fall back to the Rollback section immediately.

> The ingest endpoint reads the token at request time (`_expected_ingest_token` in `scripts/dashboard_api.py`), so the restart is to be safe; even without it the next request would pick up the new env once systemd re-sources the file. Restart anyway for determinism.

### 3. Update the desktop client copy

The desktop Claude Code hook that POSTs `desktop_session_*` events sends the token in the `X-Ingest-Token` header. It holds its OWN copy of `DESKTOP_INGEST_TOKEN` — a token used by an external desktop client must change on **both** sides.

1. On the machine running the desktop Claude Code hook, find where the hook reads the ingest token (its env file / hook config — wherever `X-Ingest-Token` is sourced).
2. Replace the old value with the new token from step 1.
3. Make sure no stale copy lingers in a second config, shell profile, or paste buffer.

Do step 2 → step 3 in rapid succession so the inconsistency window (droplet has new token, desktop still sends old) is as short as possible.

### 4. Verify the ingest path works

From the desktop client (or by replaying a desktop_session POST with the new header), confirm an ingest succeeds end-to-end:

```bash
# Droplet side (service is loopback-only at this phase — tunnel in).
ssh -L 8001:127.0.0.1:8000 larry@134.209.44.80 -N -f
TOKEN='<paste-new-token-here>'
curl -sS -X POST \
  -H "X-Ingest-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"event_type":"desktop_session_active","payload":{"note":"rotation smoke test"}}' \
  http://127.0.0.1:8001/desktop/session/ingest
# expected: {"ok":true,"event_id":"..."}
```

If `401`: the install didn't take on the droplet (repeat step 2) OR you're sending the old token (check the header). If `503` or hang: the service crashed on restart — check `journalctl -u ourliberty-dashboard-api.service -n 50`. Once the curl returns `ok:true`, trigger one real desktop session from the client and confirm a `desktop_session_*` card appears on the dashboard.

> Confirm the exact route path and payload shape against `scripts/dashboard_api.py` before running — the handler is `_handle_desktop_session_ingest` and the allowed `event_type` set is `desktop_session_start` / `desktop_session_active` / `desktop_session_done`.

### 5. Revoke the old token

There's nothing external to revoke — the old token only had power because the droplet env held it and the desktop client sent it. Step 2 already overwrote the droplet value and step 3 overwrote the client copy. Confirm no stale copy remains:

```bash
ssh larry@134.209.44.80 'grep DESKTOP_INGEST_TOKEN /home/larry/credentials/.env.larry'
# Expected: a single line, with the new token.
```

Then clear paste buffers and any drafts:

```bash
pbcopy < /dev/null
# Also clear any 1Password drafts, Notes app jots, etc.
```

### 6. Update the registry

Edit `config/token-rotation-schedule.json`:

- `last_rotated_at` → today's date (`YYYY-MM-DD`)
- `next_rotation_due` → today + 365 days

Open a PR titled `chore(creds): rotate DESKTOP_INGEST_TOKEN <YYYY-MM-DD>`. Mirror's regression gate will run; merge once green.

### 7. Push the calendar event forward by 1 year

DM Beacon (`@OLH_Beacon_bot`): *"Push the DESKTOP_INGEST_TOKEN rotation calendar event date forward by 1 year"* — she'll move the event via her Google Calendar MCP. Or do it manually in your personal Calendar UI if you prefer.

The next Pulse cycle (within 4 hours) will read the updated registry and confirm the upcoming-rotations DM count is now zero for the ingest token.

---

## Rollback

The token lives in two places. The window of inconsistency is between step 2 (droplet has new token) and step 3 (desktop client updated). During that window desktop ingest `401`s. To minimize: do step 2 → step 3 in rapid succession.

If something goes wrong:

1. **You still have the old token** — it's not deleted from anywhere external. If the new token install was bad, re-paste the old value via step 2, restart the service, and start over from step 1 with a fresh token.
2. **Droplet updated but the desktop client still 401s** — the client is still sending the old token. Re-apply step 3 on the client side; no droplet change needed.
3. **Both sides updated but ingest still 401s** — bisect with the curl in step 4: if the curl with the new token succeeds, the desktop client config didn't take (repeat step 3). If the curl 401s, the droplet didn't pick up `.env.larry` — restart the service again.

There is no irreversible step. Both sides can be re-overwritten freely.

---

## Related

- Registry entry: `config/token-rotation-schedule.json` (search for `"name": "DESKTOP_INGEST_TOKEN"`)
- Convention: `shared/credentials-discipline.md`
- Calendar event: see registry's `calendar_event_url` field
- Sibling runbook (similar self-minted shared-secret, 2-location pattern): `docs/runbooks/rotate-dashboard-api-token.md`
- Service file: `systemd/ourliberty-dashboard-api.service`
- API source: `scripts/dashboard_api.py` (`_require_ingest_token`, `_handle_desktop_session_ingest`, `INGEST_TOKEN_ENV`)
- Spec: `agents/beacon/specs/missions-v2-phase0-desktop-session-feed.md`
