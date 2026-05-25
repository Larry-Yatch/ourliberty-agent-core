# Migrate Marvin Mission Control → Supabase (one-shot)

**When to run this:** once, after E4.1 (schema v1) has shipped and you have
the parallel-run week with Mission Control in front of you. This is the
procedure for filling the empty Supabase PM schema with your actual
Mission Control data so the new dashboard has real content.

**Time required:** ~10–15 minutes total (5 min copying files + 1 min
dry-run + 1 min apply + 5 min verifying in the dashboard).

**Prerequisites:**

- E4.1 schema applied to Supabase project `ezldtkbhexyrgujqmxpd` (verify
  with `SELECT count(*) FROM programs;` returning 6).
- Mission Control's JSON files at `/Users/marvinrogers/.openclaw/workspace/`
  on the Mac Mini, reachable via Tailscale or `scp`.
- `supabase-py` installed wherever you run the script (droplet has it; on
  the Mac: `pip3 install --user --break-system-packages supabase`).
- `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` available — either via
  `~/credentials/.env.larry` on the droplet, or pasted into `--supabase-url`
  / `--supabase-key` flags when running on the Mac.

---

## What this is

A one-shot migration. Like running a Google Sheets "import CSV" once and
never again — except the destination is your Supabase PM schema and the
source is the three JSON files Mission Control writes to disk. The script
is **idempotent**: re-running it is safe. It looks at the `external_id`
column on each Supabase row and skips anything already migrated. That
means if step 6 (`--apply`) ever fails halfway through, you re-run with
`--apply` and it picks up where it left off.

What it does NOT do: bi-directional sync. Once you've run `--apply`, the
convention is "edit only in the new dashboard." Mission Control edits
made after this won't propagate — that's the whole point of decommissioning
Mission Control (E4.5) shortly after.

---

## Step 1 — Copy the JSON files off Mission Control

SSH into Marvin OR open Finder via Tailscale, then copy three files:

- `programs.json`
- `projects.json`
- `assignees.json` (we don't actually read it, but copy it anyway — it's
  the "did I copy everything" reference)

…from `/Users/marvinrogers/.openclaw/workspace/` to either:

- **On the droplet:** `/tmp/mc-export/` (use `scp` from the Mac:
  `scp /Users/marvinrogers/.openclaw/workspace/{programs,projects,assignees}.json larry@134.209.44.80:/tmp/mc-export/`).
- **On your Mac:** `/tmp/mc-export/` (just cp through Finder or via
  Tailscale SMB).

**Expected end state:** `ls /tmp/mc-export/` shows the three files; each
is a non-empty JSON file.

### What to do if Step 1 fails

- **`scp: No such file or directory`** — verify the source path on Marvin.
  The folder may have moved; SSH in and `ls ~/.openclaw/workspace/` first.
- **Permission denied** — Mission Control runs as `marvinrogers`; you may
  need to `ssh marvinrogers@<mac-mini-tailscale-ip>` first, then `cp` to a
  world-readable temp dir before pulling.

---

## Step 2 — Set Supabase credentials

**On the droplet:**

```bash
set -a; source ~/credentials/.env.larry; set +a
```

Verify with `echo $SUPABASE_URL` — should print `https://ezldtkbhexyrgujqmxpd.supabase.co`.

**On your Mac (no .env file):** pass on the command line instead — see
Step 4. Copy the values from the Supabase dashboard at
`https://app.supabase.com/project/ezldtkbhexyrgujqmxpd/settings/api`
(SUPABASE_URL = the "Project URL"; SUPABASE_SERVICE_ROLE_KEY = the
"service_role secret" key — NOT the anon key).

### What to do if Step 2 fails

- **`echo $SUPABASE_URL` is empty after sourcing** — open
  `~/credentials/.env.larry` and confirm the SUPABASE_URL line exists and
  is uncommented. The bootstrap procedure for these slots lives in
  `docs/runbooks/setup-supabase-pm-project.md` step 3.

---

## Step 3 — Dry-run (no writes)

This is the only step that lets you preview what the migration will do
without committing. ALWAYS run this first; ALWAYS read its output before
proceeding.

```bash
cd ~/agent-core
python3 scripts/migrate_mission_control.py --source-dir /tmp/mc-export
```

(On your Mac: `cd ~/Desktop/ourliberty-agent-core` instead, and append
`--supabase-url https://...supabase.co --supabase-key eyJ...`.)

**Expected output shape:**

```
=== DRY RUN (pass --apply to write) ===

PHASE 1: PROGRAMS
  Found 5 MC programs.
  [MATCH]  'Personal'                      → existing UUID 8b9c...
  [MATCH]  'TruPath'                       → existing UUID 3a4b...
  [INSERT] 'Custom Holdings'               (would be inserted)
  Summary: 4 matched, 1 would be inserted, 0 skipped.

PHASE 2: PROJECTS
  Found 28 MC projects to consider (of 28 total).
  [INSERT] 'Q3 financial review'           program=TruPath status=inprogress (would be inserted)
  ...
  Summary: 28 would be inserted, 0 skipped, 0 orphaned.

PHASE 3: TASKS
  Found 47 MC tasks across 28 migrated projects.
  ...
  Summary: 47 would be inserted, 0 skipped.

--dry-run: NO writes made. Re-run with --apply to commit.
Would insert 1 programs, 28 projects, 47 tasks (4 programs matched existing).
```

### What to do if Step 3 fails

- **`ERROR: --source-dir does not exist`** — verify the copy in Step 1.
- **`programs.json must be a dict containing a 'programs' key`** — the MC
  schema may have drifted. Inspect the file (`head -c 200 /tmp/mc-export/programs.json`);
  if Mission Control changed to a bare-list shape, the script needs an
  update (CLARIFY back to Beacon).
- **`ERROR: Supabase credentials missing`** — re-do Step 2.
- **`ImportError: No module named 'supabase'`** — `pip3 install --user --break-system-packages supabase`.
- **`[ORPHAN] '...'  programId=mc-prog-xxx has no Supabase program match`** —
  this MC project points at a program that doesn't exist in the Supabase
  `programs` table AND isn't in your MC `programs.json` either. Usually
  means the MC program was deleted but a project still references it.
  Edit `/tmp/mc-export/projects.json` to either fix the `programId` or
  remove the orphaned project, then re-run dry-run.

---

## Step 4 — Review the dry-run output

Read the output as a punch list. Answer these questions:

- **Program counts match what you expect?** (Should be roughly 5 MC
  programs, most matching existing Supabase ones.)
- **Project count match?** (Should be ~28 — your actual Mission Control
  project count.)
- **Any unexpected `[INSERT]` for a program you don't recognize?**
  That suggests an old/test program is about to land in your real
  dashboard. Edit `/tmp/mc-export/programs.json` to drop it, then re-run
  dry-run.
- **Any project with the wrong `status` after translation?**
  Status rule: `archived=true` → `dropped`; `blocker` non-null → `blocked`;
  else passthrough of `notstarted` / `inprogress` / `done`. If a project
  shows `blocked` but it shouldn't be, the source JSON has a non-null
  `blocker` field; clear it in the JSON if you want a different status.

**Do not proceed to Step 5 until the dry-run output matches your
expectations.** This is the one chance to catch surprises before live
data lands.

---

## Step 5 — Apply

```bash
cd ~/agent-core
python3 scripts/migrate_mission_control.py --source-dir /tmp/mc-export --apply
```

Same flags as dry-run plus `--apply`. Output looks identical to dry-run
but the bracketed tags reflect real outcomes (`[INSERT]` shows the actual
new UUID; `[SKIP]` means already in Supabase from a previous run).

**Expected last line:**

```
SUCCESS: 1 programs, 28 projects, 47 tasks inserted; 4 programs matched existing.
```

### What to do if Step 5 fails

- **Partial failure (e.g., HTTP 503 from Supabase mid-run)** — just re-run
  the same `--apply` command. The script's `external_id` lookup means
  already-inserted rows are skipped on retry; only the remainder gets
  inserted.
- **`SUCCESS: ... 0 programs matched existing`** when you expected 4 —
  the case-insensitive name match didn't fire. Check whether your MC
  program names are spelled the same as the 6 seeds in the schema
  migration (`Agent OS Development`, `TruPath`, `The Thing`, `AI Company`,
  `Marvin System`, `Personal`). Edit `/tmp/mc-export/programs.json` to
  rename, then re-run.

---

## Step 6 — Verify in Supabase

Open `https://app.supabase.com/project/ezldtkbhexyrgujqmxpd/editor` (the
Table Editor). Click each of the three tables:

- **programs** — expect 6 rows minimum (the 5 seeded + any MC programs
  that didn't match by name and got inserted fresh). Click any one and
  confirm `external_id` is populated for the rows your migration touched.
- **projects** — expect ~28 rows. Spot-check one: open it, confirm
  `program_id` matches the parent program's UUID, `status` looks right,
  `links` JSONB array contains the original Mission Control links if
  they existed, and `started_at` / `last_updated` reflect Mission
  Control's history (not today's date).
- **tasks** — expect > 0 rows. Spot-check one: `task_type` = `human`,
  `agent` is null, `project_id` points at a real project.

### CLI verification (alternative to Table Editor)

From the droplet:

```bash
source ~/credentials/.env.larry && \
  python3 -c "
import os
from supabase import create_client
c = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
for tbl in ('programs', 'projects', 'tasks'):
    n = c.table(tbl).select('*', count='exact').execute().count
    print(f'{tbl:10} {n}')
print()
# Sample a migrated project by external_id
sample = c.table('projects').select('name, status, program_id').limit(1).execute()
print('Sample project:', sample.data)
"
```

Expected output:

```
programs   6
projects   28
tasks      47

Sample project: [{'name': '...', 'status': '...', 'program_id': '...'}]
```

### Idempotency check

Re-run `--apply` immediately:

```bash
python3 scripts/migrate_mission_control.py --source-dir /tmp/mc-export --apply
```

Expected: every row reports `[SKIP]` because the `external_id` lookup
finds them all. Last line:

```
SUCCESS: 0 programs, 0 projects, 0 tasks inserted; ...
```

This confirms idempotency — safe to re-run without duplicating data.

---

## Step 7 — Hold onto the source JSON files

Keep `/tmp/mc-export/` for at least one week as a fallback in case
something looks wrong in the dashboard and you want to inspect the
original MC values. After the parallel-run week ends and Mission Control
decommissions (E4.5), delete the tmpdir:

```bash
rm -rf /tmp/mc-export
```

---

## Step 8 — Update the new dashboard

(Once E4.4's UI rebuild ships.) Open the dashboard, click through the
programs / projects / tasks, and confirm everything looks right. If any
program needs a color, set it via the dashboard UI (E4.2 deliberately
leaves `color` NULL on migrated programs — Larry picks them).

---

## Done

When all 6 program rows + ~28 project rows + > 0 task rows are visible
in Supabase AND the idempotency re-run reports 0 inserts, E4.2 is
complete. The new dashboard has real PM content to render; E4.4 (UI
rebuild) and E4.5 (Mission Control decommission) can dispatch.

---

## Related

- Sibling runbook: `docs/runbooks/setup-supabase-pm-project.md` — first-time
  Supabase project setup (E4.0).
- Spec: `agents/beacon/specs/e4-2-mission-control-migration.md`.
- Predecessor spec: `agents/beacon/specs/e4-1-schema-v1.md` — the schema
  the migration writes into.
- Source script: `scripts/migrate_mission_control.py` — the script being
  driven by this runbook.
- Convention: `shared/credentials-discipline.md` — SUPABASE_SERVICE_ROLE_KEY
  handling.
