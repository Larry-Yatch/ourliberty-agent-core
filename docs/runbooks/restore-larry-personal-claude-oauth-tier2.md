# Runbook: Restore Larry's personal Claude Max OAuth as Tier 2

**Purpose.** Provision (or re-provision) Larry's personal Claude Max OAuth credentials at `/home/larry/.claude-larry-personal/.claude/.credentials.json` so the agent OS can fall back to a separate account when the primary Tier 1 account (`agent.beacon.ourliberty@gmail.com`) hits rate-limit or auth-401.

**When to run.**

- After today's incident (2026-05-26): Tier 2 was never provisioned on this droplet. Run this once to close the gap.
- After an annual scope audit (Tier 2 calendar event `n2846s75hkl5s07uh3t7emdsns`, 2027-05-26): verify the credentials file still authenticates as the personal account, not a silently-rotated one.
- After any healer alert with subject `claude_tier1_failed_tier2_unavailable` or `credential-drift:MISSING_CREDENTIAL:LARRY_PERSONAL_CLAUDE_MAX_OAUTH_TIER2`.

**Tier model recap.**

| Tier | Account | OAuth location | Trigger for use |
|------|---------|----------------|-----------------|
| 1 | `agent.beacon.ourliberty@gmail.com` | `~/.claude/.credentials.json` | All normal agent work |
| 2 | Larry's personal Claude Max | `~/.claude-larry-personal/.claude/.credentials.json` | Tier 1 hit rate-limit OR auth-401 (resume-discipline: not used mid-`--resume` session) |
| 3 | None | — | Out of scope. If Tier 2 also fails, agent OS DMs Larry and waits for manual recovery |

Separate accounts = separate quota and auth buckets. One account being limited does not block the other.

---

## STEP 0 — Backup safety (verify Tier 1 first, ALWAYS)

Before any move, verify that the existing Tier 1 credentials are intact and functional. The runbook below installs Tier 2 in a parallel directory, but a panicked operator might accidentally `mv` the Tier 1 file. Don't be that operator.

```bash
# Sanity: Tier 1 credentials file exists and parses
ls -la ~/.claude/.credentials.json
jq '.claudeAiOauth.accessToken | length > 0' ~/.claude/.credentials.json   # → true

# Sanity: Tier 1 auth status reports the agent account
claude auth status
# Expected:
#   Logged in as agent.beacon.ourliberty@gmail.com
#   Subscription: Max
```

**If Tier 1 is already broken, FIX TIER 1 FIRST** (see `docs/runbooks/audit-claude-max-oauth.md`). Don't try to set up Tier 2 while Tier 1 is down — you'll just end up with two broken paths.

---

## STEP 1 — Create the Tier 2 HOME directory

```bash
mkdir -p /home/larry/.claude-larry-personal/.claude
chmod 700 /home/larry/.claude-larry-personal
chmod 700 /home/larry/.claude-larry-personal/.claude
```

The Claude CLI follows `$HOME` for its credentials path. By isolating HOME to `/home/larry/.claude-larry-personal`, the `claude login` flow writes to `/home/larry/.claude-larry-personal/.claude/.credentials.json` without touching the agent account's file.

---

## STEP 2 — Run the headless OAuth orchestrator

The headless OAuth pattern (recovered today via `/tmp/auth_orchestrator.py`) spawns `claude login` under the Tier 2 HOME, scrapes the device-code URL from stdout, presents it to Larry via Telegram DM, waits for browser-side completion, then verifies with `claude auth status`.

**Recreate the orchestrator script if it's no longer at `/tmp/auth_orchestrator.py`:**

```python
#!/usr/bin/env python3
"""auth_orchestrator.py — headless Claude OAuth via Telegram device-code relay.

Spawn `HOME=/home/larry/.claude-larry-personal claude login`, scrape the
'https://...' URL from stdout, DM it to Larry, then wait for him to
complete browser-side auth. Smoke-test with `claude auth status` against
the same HOME.

Stdlib only. Reads TELEGRAM_BOT_TOKEN_BEACON + TELEGRAM_CHAT_ID_LARRY
from /home/larry/credentials/.env.larry.
"""
# (paste body from prior 2026-05-26 recovery; key shape is documented
# in the `feedback_headless_oauth_orchestrator` memory)
```

Then run:

```bash
source ~/credentials/.env.larry
HOME=/home/larry/.claude-larry-personal python3 /tmp/auth_orchestrator.py
```

The orchestrator will DM Larry a one-time device-code URL.

---

## STEP 3 — Larry: STOP — read this before clicking the URL

> ⚠️ **Wrong-account-in-browser gotcha.**
>
> Google's default-account behavior remembers your most recently used Google account. If your browser is currently logged into `agent.beacon.ourliberty@gmail.com` (the agent account), clicking the device-code URL **will land the new OAuth credentials in the wrong account**, silently making Tier 2 a duplicate of Tier 1 — defeating the whole point of separate quota and auth buckets.
>
> **Before clicking:**
>
> 1. Open the URL in an **incognito/private window**, OR
> 2. In your normal browser, **explicitly switch the Google account picker** to your personal Claude Max account.
>
> Verify the account picker shows your personal email (not `agent.beacon.ourliberty@gmail.com`) before approving the device authorization.
>
> (Screenshot location for reference: `docs/runbooks/screenshots/claude-oauth-account-picker.png` — TODO if not already captured.)

---

## STEP 4 — Smoke test (REQUIRED — proves you didn't land in the wrong account)

```bash
# Probe: a trivial prompt under the Tier 2 HOME
HOME=/home/larry/.claude-larry-personal claude -p 'say PROBE_OK'
# Expected: output containing "PROBE_OK"

# Identity check: report the account the new credentials belong to
HOME=/home/larry/.claude-larry-personal claude auth status
# Expected:
#   Logged in as <larry's personal email>
#   Subscription: Max
#
# If the email matches agent.beacon.ourliberty@gmail.com, STOP — you fell
# into the wrong-account-in-browser trap. Delete the bad credentials
# (`rm ~/.claude-larry-personal/.claude/.credentials.json`) and re-run
# STEPS 2-4 with explicit account-picker switching.
```

---

## STEP 5 — Verify the credential-drift healer sees Tier 2

The credential-drift healer (`scripts/heal_credential_registry_drift.py`) was extended in this PR to scan `/home/larry/.claude-larry-personal/.claude/.credentials.json`. Confirm it now reports clean:

```bash
python3 ~/agent-core/scripts/heal_credential_registry_drift.py --source local
# Expected log: `tick: dry_run=False` with no drift on LARRY_PERSONAL_CLAUDE_MAX_OAUTH_TIER2
# (or --source origin if the healer-read-discipline PR has merged)
```

If the healer logs `MISSING_CREDENTIAL:LARRY_PERSONAL_CLAUDE_MAX_OAUTH_TIER2`, the credentials file isn't where the registry expects — re-check STEP 1.

---

## STEP 6 — Update the registry timestamps (annual audit)

When the calendar event for the annual scope audit fires (2027-05-26 09:00 MDT for the first cycle), update the registry entry's `last_rotated_at` and `next_rotation_due`:

```bash
# Edit config/token-rotation-schedule.json
# Set:
#   "last_rotated_at": "2027-05-26"
#   "next_rotation_due": "2028-05-26"
# Commit + PR via the standard discipline.
```

The annual audit verifies:

1. STEP 4 smoke test still reports Larry's **personal** account (not silently rotated).
2. The Max subscription is still active on that personal account.
3. Tier 2 still triggers correctly in a real fallback (mock a 401 via removing Tier 1 creds temporarily — be sure to restore them).

---

## Calendar pointer

- Tier 2 annual audit: Google Calendar event ID `n2846s75hkl5s07uh3t7emdsns` (2027-05-26 09:00 MDT, then yearly).
- Tier 1 annual audit: Google Calendar event ID `l0mj6gp5040itb3kk2au8t1f1s` (same cadence).

## Operational history

- 2026-05-26 — Tier 2 introduced via the `claude-quota-tier2-fallback-wrapper` PR. The 2026-05-26 11:05-11:09 MDT incident (Mirror's 5 review attempts hit auth-401 disguised as rate-limit) drove the design. RTO during that incident: ~15 min via headless OAuth re-auth (Tier 1 recovery, not Tier 2).
