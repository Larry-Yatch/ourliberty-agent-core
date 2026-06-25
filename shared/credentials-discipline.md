# Credentials discipline

**Status:** Active as of 2026-05-19 (Phase E1.5).
**Enforced by:** Mirror review (manual checklist) + `scripts/heal_credential_registry_drift.py` (every 6h timer).

This is the rule that keeps the agent OS from accumulating silent credential debt. It's load-bearing — if it's violated, rotation reminders silently lapse and the system breaks at the worst possible moment.

---

## The rule

**Every credential added to the system MUST ship with four artifacts in the same PR. Not split across PRs. Not "we'll do the registry entry next week."**

The four artifacts:

1. **The credential itself**, installed in its appropriate store (`.env.larry`, `~/.config/gh/hosts.yml`, MCP registration args, etc.)
2. **A registry entry** in `config/token-rotation-schedule.json` covering: `name`, `storage_location`, `credential_type`, `purpose`, `rotation_type`, `cadence_days`, `created_at`, `last_rotated_at`, `next_rotation_due`, `runbook_path`, `severity_if_lapsed`, `owner_role`, `scopes`, `notes`. See the schema in that file.
3. **A runbook** at `docs/runbooks/rotate-<credential-name>.md` covering: how to regenerate, how to install, how to verify, how to revoke the old one, how to update the registry. Use `docs/runbooks/rotate-vercel-token.md` as the canonical template.
4. **A Beacon-owned Google Calendar event** ~30 days before `next_rotation_due` if `rotation_type` is `scheduled` or `scope_audit`. The event URL goes into the registry entry's `calendar_event_url` field.

If `rotation_type` is `revocation_only`, item 4 is skipped (no scheduled date). If `rotation_type` is `auto_refresh`, item 4 covers the annual scope/billing audit.

---

## Why this rule exists

Established 2026-05-19 during the E2.0 Vercel token install + E1.5 design. The audit of `.env.larry` at that point revealed:

- The DigitalOcean token's template comment ("rotate every 90 days") had been silent for 11 days, with no system surface tracking it.
- 17 of 22 slots in `.env.larry` were empty placeholders accumulated since Phase A — credentials we'd intended to wire and forgotten about.
- Credentials lived in 4 different stores (`.env.larry`, gh CLI keychain, Claude CLI OAuth, workspace-mcp OAuth) with no unified registry.

The failure mode the rule prevents: someone (human or agent) installs a new credential — say, a Stripe API key in a future prototype — and forgets to set up the rotation reminder. A year later the key silently expires, mid-deploy, and the system breaks at the moment of highest user impact.

---

## Mirror's review checklist

When reviewing a PR that touches **any of these files**:
- `/home/larry/credentials/.env.larry` (referenced via deployment configs or scripts)
- `config/token-rotation-schedule.json`
- `docs/runbooks/rotate-*.md`
- `shared/google-workspace.md` (when wiring new OAuth scopes)
- Anything that adds an `EnvironmentFile=` directive or env var read

Mirror MUST confirm:

- [ ] All 4 artifacts from "The rule" above are present in the PR
- [ ] Registry entry's required fields are all populated (validator runs in CI; failure here is automatic)
- [ ] Runbook covers regenerate / install / verify / revoke / update-registry sections
- [ ] If `rotation_type=scheduled` or `scope_audit`: there's a corresponding Beacon calendar event creation (either already done in a prior commit + URL in the registry, or queued as a Beacon dispatch in the PR description)
- [ ] No actual credential values appear in the PR diff (the credential goes in `.env.larry` on the droplet, never in any committed file)

A PR that adds a credential but is missing any of these 4 artifacts fails review with `REVIEW_REVISION` marker. Forge must add them in the same PR before merge.

---

## The drift healer

`scripts/heal_credential_registry_drift.py` runs every 6h via systemd timer. It:

1. Scans each `storage_location` in `config/token-rotation-schedule.json#/known_storage_locations`
2. For each found credential, confirms a matching registry entry exists (matched by `name` field)
3. For each registry entry, confirms the underlying credential is still present in its store
4. DMs Larry on **any drift**:
   - Credential present in store but no registry entry → `MISSING_REGISTRY_ENTRY` alert
   - Registry entry exists but credential not in store → `MISSING_CREDENTIAL` alert (could mean the credential was rotated out of the file by mistake, or the credential never landed)

DMs continue every 6h until reconciled — fail-closed posture per Larry's Q2 decision in E1.5 design. Annoying by design; the alternative (silent drift) is worse.

**Feature flags are not credentials.** The `env_file` scanner auto-skips any key whose value is a boolean literal (`true/false/1/0/yes/no/on/off`) — these are on/off tunables (e.g. `OURLIBERTY_BOARD_DRAIN_ENABLED`) with nothing to rotate. Do **not** add a fabricated registry entry or an `ignored_keys` allowlist line for them; the skip handles them automatically. The `ignored_keys` allowlist remains only for genuinely non-boolean non-secrets (URLs, chat IDs). This is strictly safer than a name-suffix wildcard: a real secret is never the literal string `true`/`on`, so the skip can never swallow one.

The healer ships with a kill-switch: env var `OURLIBERTY_CREDENTIALS_HEALER_ENABLED=true` is required; default is dry-run (logs would-DM but doesn't actually send). Activation pattern matches `heal_pr_auto_merge` from E1.3.

---

## Adding a new credential — checklist for the operator (or for me / Claude)

1. [ ] Decide where the credential goes (`env_file`, `gh_cli`, `workspace_mcp`, or new store type)
2. [ ] If new store type: add to `known_storage_locations` in the registry + add a scanner in the drift healer
3. [ ] Install the credential
4. [ ] Add registry entry (validate locally with `python3 scripts/validate_token_rotation_schedule.py`)
5. [ ] Write the runbook (template from `rotate-vercel-token.md`)
6. [ ] If scheduled rotation: dispatch Beacon to create the calendar event; paste the URL into the registry's `calendar_event_url`
7. [ ] Open PR; Mirror reviews per the checklist above; auto-merge on PASS
8. [ ] Verify next Pulse cycle (within 4h) reads the new entry without complaint

---

## Related

- Memory: `feedback_credential_rotation_discipline` in Larry's auto-memory (the assistant-side version of this rule)
- Memory: `feedback_security_no_plaintext_secrets` (the broader "no plaintext credentials in repos" posture)
- Registry: `config/token-rotation-schedule.json`
- Healer: `scripts/heal_credential_registry_drift.py`
- Pulse integration: `runbooks/cycle-prompt.md` Section "Credential rotation check"
- Phase plan: `docs/phase-e-plan.md` E1.5 section
