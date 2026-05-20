# Rotate Supabase keys

**STATUS: STUB.** The `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `SUPABASE_SERVICE_ROLE_KEY` slots in `.env.larry` are currently empty as of 2026-05-19. Supabase is planned for E6 (prototype data persistence layer) — when it lands, populate the slots AND fill in this runbook with real procedures.

**Three keys, three rotation modes:**

| Key | Rotation cadence | Notes |
|---|---|---|
| `SUPABASE_URL` | Never (it's the project URL, not a secret) | Don't rotate. Stored in `.env.larry` for consistency with the rest of the connection details. |
| `SUPABASE_ANON_KEY` | On suspected leak only (revocation_only) | Anon key has Row-Level-Security applied; relatively low blast radius. |
| `SUPABASE_SERVICE_ROLE_KEY` | 90d scheduled + on any suspected leak | **HIGH BLAST RADIUS — bypasses RLS.** This is the dangerous one. |

---

## Rotation steps (template — refine when wired)

### Rotating the service-role key

This is the critical path. The service-role key is RLS-bypassing — anyone holding it has full DB read/write. Rotate on schedule (90d) and any time it's suspected exposed.

1. Open https://app.supabase.com/project/<project-id>/settings/api.
2. **WARNING**: there is no zero-downtime rotation of the service-role key in the Supabase UI. Rotating it invalidates the old value immediately. Plan a brief outage of any cron / agent worker that uses this key, OR coordinate so the new key is installed in `.env.larry` and services restarted within seconds of regeneration.
3. Click **Reset service_role secret**. Copy the new value immediately.
4. Install per the standard pbpaste pattern (see `rotate-vercel-token.md` step 2; same shape with `SUPABASE_SERVICE_ROLE_KEY=` as the target var).
5. Restart any service that reads it.
6. Verify by exercising one RLS-bypassing query (e.g. an admin-only read from the service-role context).

### Rotating the anon key

Anon-key rotation is similar but lower-stakes. Procedure mirrors the service-role steps; the verification step is a normal anon-context query (e.g. a public-table read).

### Updating SUPABASE_URL

Don't rotate. If the URL itself changes (e.g. project migration), update `.env.larry` and the registry entry's `notes` field, but don't bump `last_rotated_at` — that field is reserved for credential rotations, not metadata changes.

---

## Registry shape (when wired)

Add three separate entries, one per key:
- `SUPABASE_URL` — `rotation_type: revocation_only`, severity: low (it's not a secret; runbook clarifies that)
- `SUPABASE_ANON_KEY` — `rotation_type: revocation_only`, severity: medium
- `SUPABASE_SERVICE_ROLE_KEY` — `rotation_type: scheduled`, `cadence_days: 90`, severity: critical, calendar event required

All three share this runbook (`runbook_path: docs/runbooks/rotate-supabase-keys.md`).

---

## When to remove this stub

If E6 lands without using Supabase (the spec changes to e.g. `sqlite3` on the droplet), remove the three empty slots from `.env.larry` and delete this file. Otherwise, fill in real procedures matching the project's actual `app.supabase.com` URL and the wired services.

---

## Related

- Convention: `shared/credentials-discipline.md`
- Supabase API key docs: https://supabase.com/docs/guides/api/api-keys
- RLS guide (why service-role rotation matters): https://supabase.com/docs/guides/database/postgres/row-level-security
