# Set up the Supabase PM project (first-time setup)

**When to run this:** once, after E4.0a merges and before E4.1 (schema v1) dispatches. This is the procedure Larry follows to stand up the Supabase project that the rest of Phase E4 builds against.

**Time required:** ~30–40 minutes total, mostly Chrome MCP + Vercel UI clicks.

**Prerequisites:**
- Logged in to Chrome as `agent.beacon.ourliberty@gmail.com` (the agent OS's Google identity).
- SSH access to the droplet (`ssh larry@134.209.44.80`).
- Vercel UI access for the `ourliberty-dashboard` project under the `larry-yatch` account.

---

## What this is

Supabase is like a Google Sheet that the agents can read/write programmatically. Each Supabase **project** = one Sheet you own; each **table** is like a tab on that Sheet. Unlike a real Sheet, the agents authenticate using API keys (short JWTs) instead of OAuth — and one of those keys (the service-role key) bypasses all row-level security, so it's the high-blast-radius secret you'll see referenced as "the dangerous one" throughout this runbook.

We're standing up exactly one Supabase project for the PM dashboard. Future products (TruPath, AI Co, client work) will get their own separate Supabase projects in Phase F, following this same template.

---

## Step 1 — Create the Supabase project

1. Open https://app.supabase.com in a Chrome window logged in as `agent.beacon.ourliberty@gmail.com`. If you're prompted to sign in, use that Google account — not your personal one.
2. Click **New Project** in the top-right.
3. Fill in:
   - **Name:** `ourliberty-pm-dashboard`
   - **Organization:** the default org tied to the agent.beacon.ourliberty@gmail.com account (Supabase auto-creates an org on first sign-in).
   - **Database password:** click **Generate a password** → copy it somewhere durable (1Password, a sticky note in your keychain — anywhere NOT in `.env.larry`). We won't use this password directly because we authenticate via the JWT keys, but Supabase requires you to set one and you'll need it if you ever want raw `psql` access for emergency repair.
   - **Region:** `East US (North Virginia)` — `us-east-1`. This is the closest AWS region to the NYC3 droplet, minimizing round-trip latency.
   - **Pricing Plan:** `Free`. The PM workload (500MB DB, ~thousands of writes/month) fits comfortably under the free-tier limits. Upgrade to Pro ($25/mo) only if we hit the 60-concurrent-connections cap.
4. Click **Create new project**. Provisioning takes ~2 minutes; the dashboard shows a "Setting up project..." spinner.

**Expected end state:** the project's dashboard loads at `https://app.supabase.com/project/<project-id>` — you'll see the project ID in the URL (a random 20-char string). Note that ID; you'll use it whenever you need to deep-link into project settings.

### What to do if Step 1 fails

- **"You've reached the free-tier project limit."** Supabase Free allows max 2 projects per organization. Click **Account** → **Projects** to see what already exists; identify any unused project (typically a test/playground project from earlier exploration) and **Pause** or **Delete** it before retrying. Pause is reversible (free tier auto-pauses inactive projects anyway); delete is not.
- **Region not available.** Free tier doesn't expose every AWS region. If `us-east-1` is greyed out, fall back to `us-east-2` (Ohio) or `us-west-1`. Note the chosen region in the SUPABASE_URL registry entry's `notes` field via follow-up commit.
- **"Database password is too weak."** Use Supabase's **Generate a password** button instead of typing one in.

---

## Step 2 — Capture the three values

Once the project finishes provisioning:

1. In the left sidebar, click **Settings** (gear icon, near the bottom) → **API**.
2. The page shows three values you need — copy each to a temporary text buffer (a Notes scratch doc, a paste-and-clear loop, etc.):
   - **Project URL** — at the top, labelled "URL". Looks like `https://abcdefghijklm.supabase.co`. This is `SUPABASE_URL`.
   - **anon public** key — in the "Project API keys" section. Long JWT starting `eyJ...`, roughly 200 characters. This is `SUPABASE_ANON_KEY`. Click the eye icon to reveal, then the clipboard icon to copy.
   - **service_role secret** key — same section, marked **"secret"** with a warning banner. Also `eyJ...`, roughly 200 characters, but a DIFFERENT JWT than the anon key. This is `SUPABASE_SERVICE_ROLE_KEY`. Click eye → clipboard.

**What each looks like (example shape, not real values):**

```
SUPABASE_URL=https://abcdefghijklmnopqrst.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFiY2RlZmdoaWprbG1ub3BxcnN0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE2..._SHORT_SUFFIX_anon
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFiY2RlZmdoaWprbG1ub3BxcnN0Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTY..._SHORT_SUFFIX_service
```

The anon and service-role keys are easy to confuse — both are `eyJ...` JWTs. The differentiator is the `"role"` claim in the middle (base64-decoded: `"anon"` vs `"service_role"`). Always copy from the labelled row in the Supabase UI; don't try to identify them by eyeballing.

---

## Step 3 — Install on the droplet

We use the same `pbpaste | ssh ...` pattern documented in `docs/runbooks/rotate-vercel-token.md` step 2, adapted to handle three values. The pattern guarantees the secrets never land in shell history, never get written to a temp file outside your laptop's clipboard, and arrive at `.env.larry` with mode `0600`.

### 3.0 — Bootstrap the slots (one-time, idempotent)

The install commands in 3a/3b/3c below use regex-replace (`re.subn` with `^SUPABASE_*=`), which requires the slots to ALREADY exist in `.env.larry`. The forward-looking stub at `rotate-supabase-keys.md` referenced these slots but they were never actually created — verified during the first real activation on 2026-05-24. **Run this bootstrap command first; it's idempotent (only appends missing slots):**

```bash
ssh larry@134.209.44.80 'python3 -c "
import re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
content = open(p).read()
needed = [\"SUPABASE_URL\", \"SUPABASE_ANON_KEY\", \"SUPABASE_SERVICE_ROLE_KEY\"]
appended = []
for name in needed:
    if not re.search(rf\"^{name}=\", content, flags=re.M):
        content += (\"\" if content.endswith(\"\n\") else \"\n\") + f\"{name}=\n\"
        appended.append(name)
if appended:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
    os.write(fd, content.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
    print(\"appended slots:\", appended)
else:
    print(\"all slots already exist\")
"'
```

Expected first-run output: `appended slots: ['SUPABASE_URL', 'SUPABASE_ANON_KEY', 'SUPABASE_SERVICE_ROLE_KEY']`. Re-runs: `all slots already exist`.

Run each of the three install commands below separately. **For each one: first copy the value to your clipboard from your Notes scratch buffer (Step 2), then run the command.** The clipboard contents flow through `pbpaste` and over SSH into `.env.larry`.

### 3a. Install SUPABASE_URL

```bash
pbpaste | ssh larry@134.209.44.80 'python3 -c "
import sys, re, os, tempfile
p = \"/home/larry/credentials/.env.larry\"
val = sys.stdin.read().strip()
assert val and val.startswith(\"https://\") and \".supabase.co\" in val and \" \" not in val and \"\n\" not in val, \"SUPABASE_URL validation failed\"
content = open(p).read()
new, n = re.subn(r\"^SUPABASE_URL=.*$\", f\"SUPABASE_URL={val}\", content, flags=re.M)
assert n == 1, f\"expected 1 replacement, got {n}\"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
os.write(fd, new.encode()); os.close(fd); os.chmod(tmp, 0o600); os.rename(tmp, p)
print(f\"OK: SUPABASE_URL set to {val}\")"'
```

Expected output: `OK: SUPABASE_URL set to https://....supabase.co`.

### 3b. Install SUPABASE_ANON_KEY

Copy the anon key to your clipboard from Step 2, then:

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

Expected output: `OK: SUPABASE_ANON_KEY updated (~200 chars)`.

### 3c. Install SUPABASE_SERVICE_ROLE_KEY

Copy the service-role key to your clipboard, then:

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

Expected output: `OK: SUPABASE_SERVICE_ROLE_KEY updated (~200 chars)`.

### 3d. Clear the clipboard

```bash
pbcopy < /dev/null
```

Also clear the Notes scratch buffer you used in Step 2.

### What to do if Step 3 fails

- **`assert n == 1, "expected 1 replacement, got 0"`** — the `.env.larry` file doesn't have the matching empty slot. SSH in (`ssh larry@134.209.44.80`), open the file (`sudo -u larry vim /home/larry/credentials/.env.larry`), and check for a line beginning `SUPABASE_URL=` / `SUPABASE_ANON_KEY=` / `SUPABASE_SERVICE_ROLE_KEY=`. If it doesn't exist, add a stub line `SUPABASE_<NAME>=` somewhere in the file (existing convention groups related vars; the existing Vercel block is a fine reference point) and re-run the install command.
- **`validation failed`** — the value on your clipboard isn't shaped like the expected JWT or URL. For URL: should start with `https://` and contain `.supabase.co`. For keys: should start with `eyJ` and be at least 100 chars long. Re-copy from the Supabase UI; you may have grabbed surrounding whitespace or the wrong row.
- **Permission denied** — verify your SSH key is loaded (`ssh-add -l`) and that you can `ssh larry@134.209.44.80 'echo ok'` separately.

---

## Step 4 — Install on Vercel

The dashboard repo (E4.0b adds the supabase-js client wiring) reads these env vars at runtime. Set them in Vercel BEFORE the next dashboard deploy or the build will throw `MissingEnvError`.

1. Open https://vercel.com/larry-yatch/ourliberty-dashboard/settings/environment-variables in a logged-in browser.
2. Click **Add New**.
3. Add `SUPABASE_URL`:
   - **Key:** `SUPABASE_URL`
   - **Value:** paste the same URL you installed in step 3a.
   - **Environments:** check **Production** and **Preview**. Leave **Development** unchecked (local dev uses `.env.local`).
   - **Sensitive:** leave unchecked (the URL is not a secret).
   - Click **Save**.
4. Click **Add New** again. Add `SUPABASE_SERVICE_ROLE_KEY`:
   - **Key:** `SUPABASE_SERVICE_ROLE_KEY`
   - **Value:** paste the service-role JWT from step 3c.
   - **Environments:** Production + Preview (NOT Development — local dev shouldn't have prod admin keys).
   - **Sensitive:** **CHECK THIS BOX.** This is the RLS-bypassing key; Sensitive prevents Vercel from showing the value in build logs or the UI after save.
   - Click **Save**.
5. **Do NOT add `SUPABASE_ANON_KEY` to Vercel right now.** E4 has no client-side Supabase queries planned — everything goes through Next.js server routes with the service-role key. If we later add browser-side reads, we'll add anon-key to Vercel then.

**Expected end state:** the env-vars page shows `SUPABASE_URL` (Production, Preview) and `SUPABASE_SERVICE_ROLE_KEY` (Production, Preview, Sensitive). The next deploy of the dashboard repo will use these values.

---

## Step 5 — Ask Beacon to create the calendar rotation event

The service-role key rotates every 90 days. Beacon owns the rotation calendar (per the credential-discipline pattern); ask her to create the event so Pulse can DM you when the next rotation lands inside the 60-day warning window.

DM `@OLH_Beacon_bot`:

> Create a Google Calendar event for SUPABASE_SERVICE_ROLE_KEY rotation, 90 days from today, scope-audit only (no actual rotation work). The runbook is `docs/runbooks/rotate-supabase-keys.md`.

She'll create the event via her Google Calendar MCP and DM back the event URL.

---

## Step 6 — Paste the calendar URL into the registry

Once Beacon DMs you the calendar event URL, open a small follow-up commit to update the registry. The `SUPABASE_SERVICE_ROLE_KEY` entry in `config/token-rotation-schedule.json` ships with `calendar_event_url: null` per E4.0a; this commit fills it in.

Easiest path: ask Forge to handle it via a tiny dispatch.

> @OLH_Beacon_bot: Dispatch a Forge task to paste `<URL>` into the `calendar_event_url` field of the SUPABASE_SERVICE_ROLE_KEY entry in `config/token-rotation-schedule.json`. PR title: `chore(creds): set SUPABASE_SERVICE_ROLE_KEY calendar URL`.

Beacon will emit the APPROVAL_REQUEST marker; you approve; Forge ships a one-line PR; Mirror auto-merges.

---

## Step 7 — Smoke test

Confirm everything works end-to-end.

### 7a. Droplet smoke

```bash
ssh larry@134.209.44.80 'source /home/larry/credentials/.env.larry && \
  python3 -c "
import os
from supabase import create_client
url = os.environ[\"SUPABASE_URL\"]
key = os.environ[\"SUPABASE_SERVICE_ROLE_KEY\"]
c = create_client(url, key)
print(\"OK: client_created url=\" + url[:40] + \"...\")"'
```

Expected: `OK: client_created url=https://....supabase.co...`.

- If `ImportError: No module named 'supabase'` → the `pip3 install --user --break-system-packages supabase` step from `systemd/INSTALL.md` (Supabase Python client subsection) wasn't run. Run it.
- If `KeyError: 'SUPABASE_URL'` → step 3a's install didn't take. Re-check `.env.larry` for the line.

### 7b. REST-API smoke (both keys)

```bash
ssh larry@134.209.44.80 'source /home/larry/credentials/.env.larry && \
  echo "Service-role:" && \
  curl -sS -o /dev/null -w "  HTTP %{http_code}\n" \
    -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
    -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
    "$SUPABASE_URL/rest/v1/" && \
  echo "Anon:" && \
  curl -sS -o /dev/null -w "  HTTP %{http_code}\n" \
    -H "apikey: $SUPABASE_ANON_KEY" \
    -H "Authorization: Bearer $SUPABASE_ANON_KEY" \
    "$SUPABASE_URL/rest/v1/"'
```

Expected:
```
Service-role:
  HTTP 200
Anon:
  HTTP 200
```

Any 401 means the corresponding key didn't install correctly — re-run the install step for that key.

### 7c. Dashboard smoke (after E4.0b ships)

E4.0b's PR adds a temporary `/api/supabase-smoke` route to the dashboard repo. Once that PR has merged AND Vercel has rebuilt with the new env vars, open:

```
https://<your-current-preview-url>.vercel.app/api/supabase-smoke
```

Expected: `200 OK` with JSON `{"ok": true, ...}`. The E4.0b PR removes this route in the same commit set — once you've verified 200, the route is gone in production.

---

## Done

When all three smoke tests pass + the registry entry has a real `calendar_event_url`, E4.0 is complete. The PM dashboard schema (E4.1) can dispatch.

---

## Related

- Sibling runbook: `docs/runbooks/rotate-supabase-keys.md` — what to do when the keys need rotating (90-day cadence on service-role, or any time on suspected leak).
- Vercel-token install pattern (template): `docs/runbooks/rotate-vercel-token.md` step 2.
- Convention: `shared/credentials-discipline.md` — the 4-artifact rule this runbook implements.
- Parent spec: `agents/beacon/specs/e4-0-supabase-activation.md`.
- Registry entries: `config/token-rotation-schedule.json` (search for `"name": "SUPABASE_`).
