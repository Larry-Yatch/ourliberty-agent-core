# Audit the Google OAuth refresh token (workspace-mcp)

**When to run this:** when Pulse DMs you that `GOOGLE_OAUTH_REFRESH_TOKEN` is within 60 days of `next_rotation_due` (registered as `2027-05-19`), or unscheduled if a leak is suspected.

This is a **scope audit**, not a rotation. The refresh token auto-grants new access tokens; the rotation cadence exists so we re-check the scope set once a year and decide whether the workspace-mcp's permissions are still the right set.

**Severity if lapsed:** medium. Beacon loses access to Google Docs + Drive — the E5 spec-drafting workflow breaks. No agent OS infrastructure depends on this (it's a UX win, not infrastructure).

**Time required:** ~15 minutes.

---

## Current scopes

From `config/token-rotation-schedule.json`:
- `docs` — Google Docs read/write (spec drafting flow).
- `drive` — Google Drive metadata + read/write inside `Shared with Larry/`.

Scope set is managed in the `beacon-agent` Google Cloud project; OAuth client is in **Testing** mode.

---

## Steps

### 1. Pull usage data

On the droplet, run the scope-usage parser:

```bash
ssh larry@134.209.44.80 "cd ~/agent-core && python3 -c \"
from scripts.scope_usage_parser import analyze_scope_usage
import json
print(json.dumps(analyze_scope_usage('GOOGLE_OAUTH_REFRESH_TOKEN', days=90), indent=2))
\""
```

Expected: `{\"docs\": <count>, \"drive\": <count>}` (counts may be 0 for `gmail` / `calendar` since those scopes aren't currently granted; their presence in output means a new tool was added).

### 2. Decide scope changes

For the current set, the audit decisions split into three questions:

1. **Has Beacon's workflow expanded?** If we're now wiring Gmail (e.g. for E-something automated email handling) or Calendar at the agent layer (rather than Beacon's MCP layer), add the corresponding scope.
2. **Are existing scopes still used?** If `docs` count is zero AND no near-term spec-drafting work is planned, consider whether the credential should be retired entirely (rotation_type → revocation_only).
3. **Is `Testing` mode still appropriate?** If the integration is matured, consider switching the OAuth client to `Production` mode — that removes the 7-day refresh-token expiry that `Testing` mode imposes. (As of E5: still in `Testing` mode; we've been refreshing manually as needed.)

### 3. Scope changes — re-grant flow

If you're adding/removing scopes, the OAuth grant must be re-issued (you can't shrink a granted scope set; you can only re-auth with the new set).

In Google Cloud Console for project `beacon-agent`:

1. Navigate to **APIs & Services → OAuth consent screen**.
2. Edit the app; update the **Scopes** list (add/remove the OAuth scope URIs).
3. Save.

Then re-grant via workspace-mcp's flow:

1. On the droplet, delete the existing credential file:
   ```bash
   ssh larry@134.209.44.80 "rm /home/larry/.google_workspace_mcp/credentials/agent.beacon.ourliberty@gmail.com.json"
   ```
2. The next agent invocation that hits workspace-mcp will fail with an auth-required message and print a URL.
3. Open the URL in a browser logged in as `agent.beacon.ourliberty@gmail.com`; consent to the new scope set; the credential JSON is written back.

### 4. Verify the new credential works

```bash
ssh larry@134.209.44.80 "ls -la /home/larry/.google_workspace_mcp/credentials/agent.beacon.ourliberty@gmail.com.json"
```

Expected: file exists, `0600` perms, mtime is fresh.

Exercise a representative operation by asking Beacon (in chat) to list recent files in `Shared with Larry/`. She should respond with file IDs and titles. Failure means the scopes don't cover the workflow — re-check step 2.

### 5. Revoke the OLD grant

After confirming the new grant works, return to https://myaccount.google.com/permissions in a browser logged in as `agent.beacon.ourliberty@gmail.com`. Find the `beacon-agent` (or `workspace-mcp`) app and **revoke**. This kills any old refresh token Anthropic still has cached.

The new grant (step 3) is unaffected.

### 6. Update the registry

Edit `config/token-rotation-schedule.json` for `GOOGLE_OAUTH_REFRESH_TOKEN`:
- `last_rotated_at` → today (`YYYY-MM-DD`)
- `next_rotation_due` → today + 365 days
- `scopes` → updated list (if changed)
- `notes` → append `Audited <YYYY-MM-DD>; <decision rationale>.`

Push the matching calendar event (if any) out by 1 year. Open the PR.

---

## Rollback

If a scope drop breaks a workflow:
1. Repeat steps 2-3 with the dropped scope re-added.
2. Verify per step 4.

The grant-revoke window (step 5) is the only point of irreversibility — verify step 4 fully *before* revoking.

---

## Related

- Registry entry: `config/token-rotation-schedule.json` (search for `GOOGLE_OAUTH_REFRESH_TOKEN`)
- Convention: `shared/credentials-discipline.md`
- Workspace conventions: `shared/google-workspace.md`
- Google account permissions: https://myaccount.google.com/permissions
- Google Cloud Console (beacon-agent project): https://console.cloud.google.com
