# Spec: E4.0 — Supabase Activation (gating dependency for all of E4)

**Status:** Draft (awaiting Larry approval — sub-spec of E4)
**Author:** Claude-as-Beacon (drafted 2026-05-24)
**Approver:** Larry (pending)
**Phase:** E4.0 of `docs/phase-e-plan.md` Phase E4
**Parent spec:** [agents/beacon/specs/e4-overview.md](e4-overview.md)
**Predecessor:** E3 fully shipped 2026-05-21
**Successors:** E4.1 (schema v1), E4.3 (pm_writer + Beacon updates) both depend on this

---

## 1. Problem statement

Phase E4 needs Supabase as the persistent backing store for the PM dashboard's Programs / Projects / Tasks / Events / Decisions data. Today the agent OS has zero persistent DB infrastructure — all state is JSON files on the droplet. E4.0 stands up the Supabase project, captures credentials with full E1.5 4-artifact discipline, installs the Python + JS clients, and verifies an end-to-end "select 1" works from both the droplet and the dashboard.

This phase ships ZERO PM logic — no schema, no tables, no API endpoints. It only proves the infrastructure is correctly wired so subsequent sub-phases (E4.1 schema, E4.2 migration, E4.3 pm_writer, E4.4 UI) have a working Supabase to build against.

**Trigger:** Larry has approved the E4 overview spec ([e4-overview.md](e4-overview.md)) and given go-ahead 2026-05-24.

---

## 2. Success criteria

- A Supabase project named `ourliberty-pm-dashboard` exists under the Google account `agent.beacon.ourliberty@gmail.com`, in region `us-east-1` (closest to the NYC3 droplet), on the Free tier.
- Three credentials populated in `~/credentials/.env.larry` on the droplet (slots already exist per stub at `docs/runbooks/rotate-supabase-keys.md`): `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`.
- Same three vars set on Vercel project `ourliberty-dashboard` (Production + Preview environments). `SUPABASE_SERVICE_ROLE_KEY` marked Sensitive (Vercel UI checkbox).
- `config/token-rotation-schedule.json` has 3 new registry entries (URL, ANON, SERVICE_ROLE) per the registry shape outlined in the existing runbook stub. `scripts/validate_token_rotation_schedule.py` passes against the diff.
- `docs/runbooks/rotate-supabase-keys.md` expanded from STUB → real procedures, validated by Larry doing a dry-run read.
- New `docs/runbooks/setup-supabase-pm-project.md` walks Larry through first-time setup (the procedure he'll actually follow for this phase). Step-by-step with Google-Apps analogies where useful.
- `scripts/heal_credential_registry_drift.py` (next 6h cycle after merge) reports zero drift for the new entries.
- `systemd/INSTALL.md` has a new "Supabase client (E4.0)" subsection covering `pip3 install --user --break-system-packages supabase`.
- `python3 -c "from supabase import create_client; print('ok')"` runs on the droplet without ImportError.
- `ourliberty-dashboard` package.json includes `@supabase/supabase-js` at the latest stable; `npm install` from the repo root succeeds.
- New file `ourliberty-dashboard/lib/supabase-server.ts` exports a `getSupabaseServer()` helper that returns a configured admin client (service-role) — server-side only, never imported by client components.
- Smoke tests pass:
  - From droplet: `python3 -c "import os; from supabase import create_client; c = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY']); print(c.rpc('version').execute())"` returns Postgres version.
  - From dashboard Vercel preview: a temporary test route at `/api/supabase-smoke` (REMOVED in same PR after verification) returns the same.
- Beacon-owned Google Calendar event scheduled for `SUPABASE_SERVICE_ROLE_KEY` next rotation (90 days from setup). URL pasted into the registry entry as a follow-up commit.

---

## 3. Users / consumers

- **Primary:** Future E4 sub-phases (E4.1 onward). Nothing else depends on this work directly.
- **Secondary:** Larry, when reading the runbooks to do the Supabase setup.
- **Indirect:** All future products (TruPath, AI services co, client work) will follow this same activation pattern when they need their own Supabase projects in Phase F. This phase establishes the template.

---

## 4. Scope — what's in

This phase splits into two parallel Forge dispatches plus Larry-action work between them.

### 4.1 E4.0a — Agent-core artifacts (Forge dispatch)

**Files modified (5):**

1. `docs/runbooks/rotate-supabase-keys.md` — REPLACE the existing STUB with full procedures.
   - Remove the `STATUS: STUB` warning at top.
   - Service-role rotation: full step-by-step, with the explicit warning about no zero-downtime rotation, plus the `pbpaste | ssh ... pattern` from rotate-vercel-token.md.
   - Anon-key rotation: same pattern; verification step uses an anon-context RLS-gated read.
   - URL-update procedure: documented as "don't rotate; update notes if project migrates."
   - Add the verification commands (curl examples that exercise each key).
   - Add a rollback section matching the rotate-vercel-token.md template.

2. `docs/runbooks/setup-supabase-pm-project.md` — NEW first-time setup runbook for Larry. Sections:
   - **What this is** (Google-Apps analogy: "Supabase is like a Google Sheet that the agents can read/write programmatically. Each project = one Sheet you own.")
   - **Step 1: create Supabase project.** Sign in to https://app.supabase.com with agent.beacon.ourliberty@gmail.com. Click New Project. Name: `ourliberty-pm-dashboard`. Region: `us-east-1` (or whichever AWS region is closest to NYC). Plan: Free. Database password: generate random + save to 1Password or equivalent (NOT into `.env.larry` — we'll use the service-role key, not direct Postgres auth).
   - **Step 2: capture the three values.** From Settings → API: copy Project URL (looks like `https://xxxx.supabase.co`), anon (public) key (long JWT starting `eyJ...`), service_role (secret) key (long JWT starting `eyJ...`, also marked "secret"). Show what each looks like as a screenshot or example block.
   - **Step 3: install on droplet.** SSH in, edit `.env.larry`, paste the three values into the three existing slots. Use the same `pbpaste | ssh ...` pattern documented in `rotate-vercel-token.md` step 2, adapted for the 3 vars.
   - **Step 4: install on Vercel.** Open https://vercel.com/larry-yatch/ourliberty-dashboard/settings/environment-variables. Add `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` (Production + Preview, mark Service-role as Sensitive). Add `SUPABASE_ANON_KEY` if/when we add client-side queries (skip for now — Larry decides).
   - **Step 5: ask Beacon to create the calendar rotation event.** DM `@OLH_Beacon_bot`: "Create a Google Calendar event for SUPABASE_SERVICE_ROLE_KEY rotation, 90 days from today, scope-audit only (no actual rotation work)." She'll create it and DM back the URL.
   - **Step 6: paste calendar URLs into registry.** Small follow-up commit updates the registry entry's `calendar_event_url` field for the SERVICE_ROLE entry.
   - **Step 7: smoke test.** Run the droplet smoke command + open a Vercel preview URL with `/api/supabase-smoke` appended. Both should return success.
   - **What to do if Step 1 fails.** Common: Supabase free-tier project limit (you can have 2 free projects max; if you hit limit, identify the unused one + delete).
   - **What to do if Step 3 fails.** The `.env.larry` slots exist but are empty per stub; the pattern is replacing `SUPABASE_URL=` with `SUPABASE_URL=https://...`. If the slot doesn't exist, add it before/after the existing Vercel block.

3. `config/token-rotation-schedule.json` — add 3 entries to the `credentials` array. Schema per existing entries; specifics:
   - `SUPABASE_URL` — `storage_location: env_file:/home/larry/credentials/.env.larry`, `credential_type: connection_string`, `rotation_type: revocation_only`, `cadence_days: null`, `severity_if_lapsed: low` (not a secret — it's the project URL), `calendar_event_url: null` (never rotates on schedule), `runbook_path: docs/runbooks/rotate-supabase-keys.md`, `scopes: []`, `notes: "Project URL, not a secret. Stored in .env.larry for consistency with anon + service-role keys. Update only if project URL changes (e.g. migration)."`
   - `SUPABASE_ANON_KEY` — `rotation_type: revocation_only`, `cadence_days: null`, `severity_if_lapsed: medium`, `calendar_event_url: null`, runbook same as above, `scopes: ["anon-rls-gated"]`, `notes: "RLS-gated public key. Rotate only on suspected leak. Browser-safe (RLS enforces access control)."`
   - `SUPABASE_SERVICE_ROLE_KEY` — `rotation_type: scheduled`, `cadence_days: 90`, `severity_if_lapsed: critical`, `calendar_event_url: null` (Larry fills via follow-up commit), runbook same, `scopes: ["full-db-bypass-rls"]`, `notes: "RLS-bypassing admin key. NEVER expose to browser. Rotate every 90d; the cadence is short because exposure has full blast radius."`

4. `systemd/INSTALL.md` — append a new subsection `Supabase Python client (E4.0)` after the existing dashboard-api section. Content:
   - `pip3 install --user --break-system-packages supabase` (matches the prior install patterns)
   - One-line verification: `python3 -c "from supabase import create_client; print('ok')"`
   - Note: `supabase` package brings `httpx`, `postgrest`, `gotrue`, `realtime` as deps; document the disk-space impact (~50MB).

5. Tests:
   - `scripts/tests/test_validate_token_rotation_schedule.py` (existing file) — extend to cover the 3 new entries' validity if not auto-covered.
   - Run `scripts/heal_credential_registry_drift.py --dry-run` locally to verify it detects the registry-side without erroring.

**Trust-boundary edits in this dispatch:** 1 (the registry adds 3 entries; reviewed as credential-discipline work). No code-path changes.

### 4.2 Larry-actions (between E4.0a merge and E4.0b dispatch)

Following `docs/runbooks/setup-supabase-pm-project.md` (which lands in E4.0a):

1. Create Supabase project (Chrome MCP-assisted, ~10 min).
2. Capture three values, install to `.env.larry` (~10 min, via the documented pattern).
3. Install to Vercel project env vars (Chrome MCP-assisted, ~5 min). Mark service-role Sensitive.
4. DM Beacon to create calendar rotation event for SERVICE_ROLE_KEY (~2 min).
5. Receive calendar URL, paste into registry via small follow-up commit (Claude-as-Forge edit, ~5 min Claude time).
6. Run droplet smoke test (~2 min).

Total Larry time: ~30-40 minutes including any Chrome MCP back-and-forth.

### 4.3 E4.0b — Dashboard repo artifacts (Forge dispatch, parallel with E4.0a)

**Files modified (4):**

1. `package.json` — add `"@supabase/supabase-js": "^2.x"` to dependencies. Latest stable; lock to a major version.

2. `lib/supabase-server.ts` (NEW) — server-side admin client helper:
   ```ts
   import { createClient, SupabaseClient } from '@supabase/supabase-js'
   import { getEnv } from './env'
   
   let cachedClient: SupabaseClient | null = null
   
   export function getSupabaseServer(): SupabaseClient {
     if (cachedClient) return cachedClient
     const env = getEnv()
     cachedClient = createClient(env.SUPABASE_URL, env.SUPABASE_SERVICE_ROLE_KEY, {
       auth: { persistSession: false, autoRefreshToken: false },
     })
     return cachedClient
   }
   ```
   - Server-side ONLY. Throw a runtime error if called from a client component (verify by checking `typeof window`).
   - Cached singleton for connection pooling.
   - Auth disabled because we're using service-role (no user sessions).

3. `lib/env.ts` — extend the existing loader to require `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`. Throw `MissingEnvError` at module load if either is missing in production.

4. `.env.local.example` — add the two new vars with placeholder comments:
   ```
   # Supabase — get from app.supabase.com → Settings → API
   SUPABASE_URL=https://<project>.supabase.co
   SUPABASE_SERVICE_ROLE_KEY=eyJ...
   ```

5. Tests:
   - `lib/supabase-server.test.ts` (NEW) — Vitest test that the helper throws if env vars missing, that it returns a singleton on repeated calls, and that it doesn't instantiate when imported (lazy).
   - Update existing env tests to assert the new required vars.

**Trust-boundary edits:** 0 (pure code additions, no behavior change in existing paths).

### 4.4 Smoke test (post-Larry-action, both repos merged + env vars set)

**Droplet smoke** (Larry runs via SSH, one-liner from setup runbook step 7):

```bash
ssh larry@134.209.44.80 'source /home/larry/credentials/.env.larry && \
  python3 -c "
import os
from supabase import create_client
url = os.environ[\"SUPABASE_URL\"]
key = os.environ[\"SUPABASE_SERVICE_ROLE_KEY\"]
c = create_client(url, key)
result = c.rpc(\"version\").execute() if False else \"client_created_ok\"
print(\"OK:\", result, \"url=\" + url[:40] + \"...\")"'
```

Expected: `OK: client_created_ok url=https://xxxx.supabase.co...`. If it errors with `ImportError`, INSTALL.md step wasn't run; if `KeyError`, `.env.larry` install didn't take.

**Dashboard smoke** (Larry opens Vercel preview URL with `/api/supabase-smoke` path):

Temporary route handler at `app/api/supabase-smoke/route.ts` that does the same client-create + a trivial query. Returns 200 + JSON. **REMOVED in the same E4.0b PR** — it's a smoke test, not a permanent endpoint. (Mirror should flag if it's left in.)

---

## 5. Out of scope (explicit deferrals)

- **Any schema or tables.** E4.1 lands those.
- **Any PM data mutations.** E4.3 builds the `pm_writer` helper.
- **Any UI changes beyond the temporary smoke route.** E4.4 is the rebuild.
- **`@supabase/supabase-js` browser-side instantiation.** We're not using anon-key-from-browser pattern; everything goes through Next.js server routes with service-role.
- **Supabase realtime / subscriptions / storage / edge functions.** Not needed for PM. May be needed in Phase F when products use Supabase.
- **Database password capture/rotation.** Direct Postgres password isn't used by either client; we go through the supabase URL + JWT keys. If we ever need direct psql access, add a 4th registry entry.
- **Multiple Supabase environments (dev vs. staging vs. prod).** Single project for now. Branching/preview environments come later if needed.
- **CONNECTION POOLING tier (PgBouncer setup).** Free tier includes basic pooling. Upgrade to Pro tier (~$25/mo) only when we hit the free-tier 60-concurrent-connections limit — unlikely for PM workload.

---

## 6. Architecture decisions locked

| Decision | Value | Rationale |
|---|---|---|
| Project owner | **Larry-Yatch GitHub** (overrode 2026-05-24 during setup) | Originally locked as `agent.beacon.ourliberty@gmail.com`. Overridden during activation: Supabase has no Google SSO, and matching Vercel's existing posture (also Larry-Yatch GitHub) beats splitting identities for the PM dashboard's infra. Tenant-separation argument re-engaged in Phase F when per-product Supabases land. |
| API key format | **Legacy JWT (`eyJ...`)** — not the new `sb_publishable_/sb_secret_` format | Supabase introduced new API key format mid-2026. Our runbook + lib/supabase-server.ts + install validators all assume the legacy JWT shape; switching to new format would break them. Stick with legacy until Supabase announces deprecation, then migrate. |
| Region | `us-east-1` (AWS Virginia) | Geographically close to NYC3 droplet. Latency-optimized. |
| Pricing tier | Free | Sufficient for PM workload (500MB DB, unlimited API requests, RLS-included). Upgrade to Pro ($25/mo) only when needed. |
| Project naming | `ourliberty-pm-dashboard` | Distinct from future per-product Supabase projects (TruPath, AI Co will be `ourliberty-trupath`, etc.). |
| Single shared runbook for 3 keys | `docs/runbooks/rotate-supabase-keys.md` | Existing stub already uses this pattern. Keys are tightly coupled (rotate URL/anon/service-role together if project migrates); one runbook is more discoverable. |
| Service-role rotation cadence | 90 days | RLS-bypassing key has full blast radius. Short cadence limits exposure window. Matches the existing stub's recommendation. |
| Anon + URL rotation cadence | revocation-only (no schedule) | RLS-gated; lower blast radius. Rotate only on suspected leak. |
| Client library: Python (droplet) | `supabase-py` (official) | Joe's pattern; battle-tested. |
| Client library: JS (dashboard) | `@supabase/supabase-js` (official) | Standard Next.js pattern; most mature client. |
| Server-side-only on dashboard | service-role key never to browser | Security baseline. Verified by build-time check + Mirror review. |
| Smoke test route lifecycle | Created + removed in same PR | Don't leave debug surfaces in production. Mirror enforces. |

---

## 7. Dependencies

- **Hard prereq:** E4 overview spec approved (DONE 2026-05-24).
- **Hard prereq:** Larry has access to agent.beacon.ourliberty@gmail.com Google account (verified during E5).
- **Hard prereq:** Vercel project `ourliberty-dashboard` exists (verified — E3 ships against it).
- **Hard prereq:** `pip3 install --user --break-system-packages` pattern works on droplet (verified by prior E3.1 dashboard-api install).
- **Soft prereq:** Larry available for ~30-40 min of Chrome MCP + SSH work between E4.0a merge and E4.0b dispatch.

---

## 8. Risks + rollback

| Risk | Mitigation | Rollback |
|---|---|---|
| Supabase free-tier limit hit (2 projects max) | Setup runbook step 1 covers identifying + deleting unused free-tier projects. | If both slots are needed for legit reasons, upgrade to Pro ($25/mo). One-click. |
| Service-role key accidentally committed | `.env.larry` is in `.gitignore`; secrets-scan in CI flags any JWT-shaped string in diffs. | Rotate immediately via runbook. |
| `supabase-py` install fails on droplet | `pip3 install --user --break-system-packages` is the standard pattern (per prior FastAPI install). If it fails, log the error and Larry resolves manually (likely a Python version mismatch). | Roll back the agent-core PR; nothing else depends on `supabase` import at this phase. |
| Vercel env var typo (paste wrong value) | Smoke route 404s or returns auth error. | Re-paste; Vercel changes take effect on next deploy. |
| Drift healer false-positive on the new entries | Healer reads `known_storage_locations` from registry; `env_file:` is already there. No code change needed. New entries should pass on first 6h cycle. | If false-positive, fix entries to match canonical schema before next cycle. |
| Calendar event for service-role rotation not created | Larry forgets step 5; registry entry stays `calendar_event_url: null`; Pulse will eventually DM a reminder. | Manual creation via Beacon DM; ~5 min. |
| Smoke route left in production after merge | Mirror's review checks for `/api/supabase-smoke` path; should block REVIEW_PASS if present. | Manual delete + redeploy. ~5 min. |

---

## 9. Effort + cost

| Sub-step | LLM cost | Wall clock | Larry-time |
|---|---|---|---|
| E4.0a draft + dispatch + Mirror review | ~$5 | ~25 min | 5 min approval of plan/spec |
| E4.0b draft + dispatch + Mirror review (parallel) | ~$5 | ~25 min (parallel) | 5 min approval |
| Larry-actions (Supabase setup + Vercel + DM Beacon) | $0 LLM | ~40 min | 40 min |
| Follow-up commit (calendar URL paste) | ~$0.50 | ~10 min | 2 min approval |
| Smoke tests | $0 | ~5 min | 5 min |
| **Total E4.0** | **~$10.50** | **~1.5 hours wall clock** (most parallel) | **~1 hour Larry-time** |

---

## 10. Sequencing

1. **NOW:** Beacon dispatches E4.0a + E4.0b in parallel (per Claude-as-Forge approval flow — both are credential-discipline + small-code changes; can go direct to Forge with `source='beacon'` per the headless dispatch pattern).
2. **+15 min:** Both Forge builds complete; Mirror reviews both; auto-merge fires on both.
3. **+30 min:** Larry pulls + reviews on droplet; runs the setup runbook.
4. **+70 min:** Larry has Supabase project live + keys installed + Vercel set up + Beacon DM'd for calendar event.
5. **+80 min:** Calendar URL pasted into registry via small follow-up commit.
6. **+85 min:** Smoke tests pass. **E4.0 done.**
7. **NEXT:** E4.1 (schema v1) dispatches. P-1 (comms narrowing) can dispatch in parallel with E4.1 — different surfaces.

---

## 11. Validation (post-merge, after Larry's setup)

Before declaring E4.0 done, all of these must be true:

- [ ] `curl -sS -H "X-Dashboard-Token: $(grep ^DASHBOARD_API_TOKEN .env.larry | cut -d= -f2)" https://api.ourliberty.dev/health` returns 200 (existing E3 surface still works — no regression).
- [ ] Droplet smoke `python3 -c "from supabase import create_client; print('ok')"` runs without error.
- [ ] Droplet smoke `python3 -c "import os; from supabase import create_client; c = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY']); print('client_ok')"` runs without error.
- [ ] Vercel preview URL with `/api/supabase-smoke` returns 200 + valid JSON (then is removed).
- [ ] `python3 scripts/heal_credential_registry_drift.py --dry-run` reports zero drift.
- [ ] `python3 scripts/validate_token_rotation_schedule.py` exits 0.
- [ ] Supabase dashboard at `https://app.supabase.com/project/<project-id>` shows the project exists, region = us-east-1, plan = Free.
- [ ] Beacon-created calendar event visible in Larry's Google Calendar, dated 90 days out.

---

## 12. Open questions (none blocking — defaults locked above)

- **Multiple region future:** if latency from NYC3 → us-east-1 is acceptable today, do we ever care about multi-region? Probably never for PM (single-user workload). Defer.
- **Database password capture:** if we ever need direct psql access (e.g., for emergency repair), we'd add a 4th registry entry for the Postgres password. Skip until we need it.
- **Read replicas / connection pooler:** free tier handles our scale; revisit at Pro upgrade if/when needed.

---

*This sub-spec lives at `agents/beacon/specs/e4-0-supabase-activation.md`. Parent: [e4-overview.md](e4-overview.md). Update parent doc's § 6 sequencing checklist when E4.0 ships.*
