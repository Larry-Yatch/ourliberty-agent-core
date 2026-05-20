# Audit the gh CLI OAuth token

**When to run this:** when Pulse DMs you that `GITHUB_GH_OAUTH_TOKEN` is within 60 days of `next_rotation_due` (registered as `2027-05-08`), or unscheduled if a token leak is suspected.

This is a **scope audit**, not a rotation. The gh-OAuth token (`gho_...`) is non-expiring; the rotation cadence exists so we re-check the scope set once a year and drop anything we no longer use. Lower scope = smaller blast radius if the token leaks.

**Severity if lapsed:** critical. Forge can't push commits or open PRs. Mirror can't comment on PRs. The auto-merge healer can't merge.

**Time required:** ~10 minutes if no scope changes; ~20 minutes if dropping a scope (test that everything still works).

---

## Current scopes (as of last audit)

From `config/token-rotation-schedule.json`:
- `gist` — likely audit win; no `gh gist` usage observed in agent OS code.
- `read:org` — used for organization membership checks (low usage).
- `repo` — used by every PR-touching operation (Forge, Mirror, healer).
- `workflow` — used to trigger / rerun GitHub Actions (the CANCELLED-workflow rerun in `heal_pr_auto_merge.py`).

---

## Steps

### 1. Pull a usage report

On the droplet, run the scope-usage parser against the last 90 days of logs:

```bash
ssh larry@134.209.44.80 "cd ~/agent-core && python3 -c \"
from scripts.scope_usage_parser import analyze_scope_usage
import json
print(json.dumps(analyze_scope_usage('GITHUB_GH_OAUTH_TOKEN', days=90), indent=2))
\""
```

Expected shape: `{\"repo\": <count>, \"workflow\": <count>, \"gist\": <count>, \"read:org\": <count>}`.

A zero count for a scope means the parser saw no invocations using it in the window. **Zero is not proof of disuse** — log-grep is fragile, the call may have happened via a code path that doesn't log the subcommand. Verify before dropping.

### 2. Decide what to drop

For each scope with low/zero usage, decide:
- **Drop**: clearly no agent code uses it AND no plausible near-term need.
- **Keep**: usage is rare but legitimate (e.g. quarterly admin tasks), OR removing it would require re-doing the `gh auth login` flow on a different schedule.

For the current set, the likely action is **drop `gist`** — there's no `gh gist` use in the codebase as of E1.5.

### 3. Re-issue the token with the new scope set

The gh CLI re-auth flow re-runs the OAuth grant — you can't shrink scopes on an existing token. Revoke the old one and grant new with fewer scopes.

On the droplet, in an SSH session (NOT a non-interactive pipe — the device-code flow needs a browser):

```bash
ssh larry@134.209.44.80
gh auth login --hostname github.com --git-protocol https --scopes "repo,workflow,read:org"
# Drop `gist` from the scope list above if that was the audit decision.
```

`gh auth login` walks you through:
1. Open the URL in a browser; sign in to GitHub.
2. Paste the device code shown in the terminal.
3. Authorize the requested scopes (this is the moment to verify the scope list matches your decision).

### 4. Verify the new token works

```bash
ssh larry@134.209.44.80 "gh auth status"
```

Expected: `Logged in to github.com as Larry-Yatch (oauth_token)` with the `Token scopes:` line showing your new scope set.

Then exercise a representative operation:

```bash
ssh larry@134.209.44.80 "gh pr list --repo Larry-Yatch/ourliberty-agent-core --state open --limit 3"
```

Expected: a list of open PRs (or empty list, both fine). A `Bad credentials` / `Resource not accessible` error means a scope you actually needed was dropped — re-run step 3 with that scope added back.

### 5. Revoke the OLD token

After confirming the new token works, return to https://github.com/settings/tokens (the "Tokens (classic)" view does NOT show OAuth app tokens). For gh CLI OAuth tokens, go to https://github.com/settings/applications and find the entry for **GitHub CLI** — that's the OAuth app. Click **Revoke** on the previous grant.

The new grant created in step 3 stays.

### 6. Update the registry

Edit `config/token-rotation-schedule.json` for `GITHUB_GH_OAUTH_TOKEN`:
- `last_rotated_at` → today (`YYYY-MM-DD`)
- `next_rotation_due` → today + 365 days
- `scopes` → updated list (e.g. drop `gist`)
- `notes` → append `Audited <YYYY-MM-DD>; dropped <scope> (no observed usage in 90d log window).`

Open a PR titled `chore(creds): audit gh-OAuth scopes <YYYY-MM-DD>`. Push the matching calendar event (if there is one) out by 1 year — see Step 6 of `rotate-vercel-token.md` for the Beacon DM shape.

---

## Rollback

If you dropped a scope and a workflow breaks:

1. Re-run step 3 with the dropped scope re-added.
2. Verify per step 4.
3. Restore the dropped scope in `config/token-rotation-schedule.json` and note in `notes` that the audit decision was reverted.

The window between "old revoked" and "new with fewer scopes installed" is the only point of irreversibility. Verify step 4 returns 200 *before* clicking Revoke in step 5.

---

## Related

- Registry entry: `config/token-rotation-schedule.json` (search for `GITHUB_GH_OAUTH_TOKEN`)
- Convention: `shared/credentials-discipline.md`
- Scope-usage parser: `scripts/scope_usage_parser.py`
- gh-OAuth scope docs: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps
