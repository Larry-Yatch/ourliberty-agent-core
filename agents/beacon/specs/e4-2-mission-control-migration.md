# Spec: E4.2 — Mission Control → Supabase Migration Script

**Status:** Draft (awaiting Larry approval — sub-spec of E4)
**Author:** Claude-as-Beacon (drafted 2026-05-24)
**Approver:** Larry (pending)
**Phase:** E4.2 of `docs/phase-e-plan.md` Phase E4
**Parent spec:** [agents/beacon/specs/e4-overview.md](e4-overview.md)
**Predecessors:** E4.0 + E4.1 shipped 2026-05-24
**Successor:** E4.3 (`pm_writer` library + Beacon CLAUDE.md updates) is parallel-eligible; E4.4 (UI rebuild) follows

---

## 1. Problem statement

E4.1 shipped an empty Supabase schema with 6 seed Programs. E4.2 fills it with Larry's actual personal PM data — currently in Marvin Mission Control, a Node.js + JSON-files app on the Mac Mini (~28 projects across 5 programs). After this ships, the new dashboard has real content even before E4.4's UI rebuild lands — Larry can start using Supabase as the source of truth for personal PM during the parallel-run week with Mission Control.

Ships ONE script: `scripts/migrate_mission_control.py` in `ourliberty-agent-core`. Dry-run by default; `--apply` is the explicit commit. Idempotent (re-runnable; uses `external_id` for dedup).

**Trigger:** E4.1 fully closed via PRs #3 + #4 + Larry's `supabase db push` of 0001 + 0002 (all done 2026-05-24 evening MDT).

---

## 2. Success criteria

- `scripts/migrate_mission_control.py` exists; runs from Larry's Mac OR the droplet (both have supabase-py + `.env.larry`).
- Reads 3 JSON files from a `--source-dir` flag location (Larry copies them off Marvin's Mac via Tailscale or scp).
- `--dry-run` (default): outputs a structured summary of what WOULD be inserted (counts + a few sample rows) without writing.
- `--apply`: actually writes to Supabase using `SUPABASE_SERVICE_ROLE_KEY`. Idempotent (re-runnable).
- Programs: matches existing seed Programs by name (so MC "TruPath" doesn't create a duplicate of the 0001 seed "TruPath"); only inserts truly new programs. Sets `external_id` = MC program id on each.
- Projects: inserts under correct `program_id` (looked up by name match). Sets `external_id` = MC project id. Translates field-by-field per § 4.2 mapping.
- Tasks: inserts under correct `project_id` for each nested task. Sets `external_id` = MC task id, `task_type='human'`, `agent=NULL`. Translates status enum if needed.
- Decisions table NOT populated (Mission Control doesn't have a decisions concept; Larry creates these in the new dashboard going forward).
- Events table NOT populated (Mission Control has no event log; will be populated by E4.3 `pm_writer` for new agent dispatches).
- `--apply` end-of-run validation: count rows in each table; compare to expected; report SUCCESS or DRIFT with details.
- All field mappings documented in the script's module docstring.
- Larry can run `--dry-run` from his Mac in <10 sec; output is readable as a punch list ("I would insert X projects, Y tasks across N programs").

---

## 3. Users / consumers

- **Primary:** Larry, one-shot during the parallel-run week.
- **Indirect template:** Future migrations from other personal-PM tools (Linear, Notion, etc.) will follow this script's shape — read-source-files → translate → idempotent-insert.

---

## 4. Scope — what's in

Single Forge dispatch against `target_repo: ourliberty-agent-core`. One new Python file + tests.

### 4.1 New file: `scripts/migrate_mission_control.py` (~250-350 LOC)

CLI script (argparse-based) with the following surface:

```
usage: migrate_mission_control.py [-h] --source-dir SOURCE_DIR
                                  [--apply] [--limit LIMIT]
                                  [--target-program PROGRAM_NAME]

  --source-dir   Directory containing programs.json, projects.json,
                 assignees.json (copied from Marvin Mission Control)
  --apply        Actually write to Supabase. Default: dry-run.
  --limit N      Process only first N projects (for incremental testing).
  --target-program NAME    Only migrate projects under this MC program name
                           (useful for partial migrations during validation).
```

Module structure:

```python
"""
migrate_mission_control.py — One-shot migration from Marvin Mission Control
JSON files to the Supabase PM schema (E4.1).

Field mapping reference (MC → Supabase Programs):
  id → external_id
  name → name
  description → description
  color → color (default null if missing)

Field mapping (MC → Supabase Projects):
  id → external_id
  name → name
  description → description
  reportingBrief → reporting_brief
  owner → owner
  status → status (passthrough; MC values 'notstarted'|'inprogress'|'done'
                   match our schema; 'blocked' inferred if blocker field set)
  priority → priority (passthrough: high|medium|low; default 'medium')
  blocker → blocker_type
  blockerNote → blocker_note
  nextAction → next_action
  whyItMatters → why_it_matters (often null in source)
  links → links (JSONB; preserved as-is — array of {label, url} objects)
  programId → program_id (looked up via Programs name→UUID map)
  project_type → 'personal' (default; Larry recategorizes via dashboard later)

Field mapping (MC nested tasks → Supabase Tasks):
  id → external_id
  name/title → name
  description → description
  assignee → assignee (free-text; MC uses string assignee names)
  dueDate / due_date → due_date (date; null if missing or invalid)
  status → status (translated: completed=true → 'completed';
                   otherwise 'pending' unless MC has explicit status)
  task_type → 'human' (hardcoded; all MC tasks are human-driven)
  agent → NULL (hardcoded; MC has no agent concept)

Field mapping NOT done:
  - Mission Control's color picker hex → preserved verbatim
  - Mission Control's createdAt timestamps → ignored; we use Supabase NOW()
    on insert (no point preserving "when MC made this row" for migrated data)
  - Mission Control's archive field → projects with archived=true become
    status='dropped' in our schema (closest semantic match)
"""

import argparse
import json
import os
import sys
from pathlib import Path
from supabase import create_client

def main():
    args = _parse_args()
    programs, projects, assignees = _load_source(args.source_dir)
    client = _connect_supabase()
    
    # Phase 1: Programs (match-or-insert)
    name_to_uuid = _migrate_programs(client, programs, dry_run=not args.apply)
    
    # Phase 2: Projects
    project_id_map = _migrate_projects(client, projects, name_to_uuid,
                                         dry_run=not args.apply,
                                         limit=args.limit,
                                         target_program=args.target_program)
    
    # Phase 3: Tasks (nested in projects)
    task_count = _migrate_tasks(client, projects, project_id_map,
                                  dry_run=not args.apply)
    
    # Validation summary
    _report_summary(programs, projects, task_count, dry_run=not args.apply)

def _parse_args(): ...
def _load_source(source_dir): ...
def _connect_supabase(): ...
def _migrate_programs(client, mc_programs, dry_run): ...
def _migrate_projects(client, mc_projects, name_to_uuid, dry_run, limit, target_program): ...
def _migrate_tasks(client, mc_projects, project_id_map, dry_run): ...
def _report_summary(...): ...
```

Core behaviors:

- **Idempotency:** before each insert, query for existing row by `external_id`. If found, skip with INFO log. If not found, INSERT (or print preview in dry-run).
- **Program name matching:** for each MC program, do `client.table("programs").select("id,name").execute()` once, build `{name.lower(): uuid}` map, look up MC program's name. Match = reuse UUID + write `external_id` via UPDATE. Miss = INSERT with full fields. Both cases: cache the UUID for projects-phase lookup.
- **Project status translation:** MC's project status field is usually 'notstarted'|'inprogress'|'done'. Our schema accepts those plus 'blocked'|'dropped'. Translation: if `blocker` field non-null → status='blocked'. If `archived` field true → status='dropped'. Otherwise passthrough.
- **Dry-run output shape:** `print()` calls grouped by phase. Examples:
  ```
  PHASE 1: PROGRAMS
    Found 5 MC programs.
    [MATCH]   TruPath           → existing UUID 3a4b...
    [MATCH]   Personal          → existing UUID 8b9c...
    [INSERT]  Custom Holdings   (would be inserted)
    Summary: 4 matched, 1 would be inserted.
  
  PHASE 2: PROJECTS
    Found 28 MC projects.
    [INSERT] "Q3 financial review" → program=TruPath (would be inserted)
    [SKIP]   "Old experiment X" → external_id=mc-proj-007 already exists
    ...
    Summary: 27 would be inserted, 1 already exists.
  
  PHASE 3: TASKS
    Found 47 MC tasks across 28 projects.
    Summary: 47 would be inserted.
  
  --dry-run: NO writes made. Re-run with --apply to commit.
  ```
- **`--apply` output:** identical structure but INSERT/SKIP show actual results, end with `SUCCESS: X programs, Y projects, Z tasks inserted; A programs matched existing.`
- **Env var loading:** reads `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` from process env. Fails loudly with clear message if missing (script user runs `set -a; source ~/credentials/.env.larry; set +a` first OR Larry passes via `--supabase-url` + `--supabase-key` flags if running on Mac without the .env).
- **Tasks: nested or top-level?** Mission Control nests tasks INSIDE project JSON (per research). Script reads them from each project's `tasks` array, not from a separate tasks.json file.

### 4.2 New file: `scripts/tests/test_migrate_mission_control.py` (~150-200 LOC)

Unittest-style tests with mocked supabase client. >=8 tests:

1. `_load_source` reads 3 JSON files correctly.
2. `_load_source` raises clear error if a file is missing or malformed.
3. `_migrate_programs` dry-run: matches names case-insensitively (MC "TruPath" matches existing "TruPath" OR "trupath").
4. `_migrate_programs` dry-run: returns name→UUID map including BOTH matched and newly-inserted programs.
5. `_migrate_programs` --apply: calls `client.table("programs").insert()` only for new programs.
6. `_migrate_projects` --apply: passes correct `program_id` based on name→UUID map.
7. `_migrate_projects` --apply: idempotent — running twice doesn't insert duplicates (uses external_id lookup).
8. `_migrate_projects` status translation: project with `blocker="waiting"` → status='blocked'; project with `archived=true` → status='dropped'.
9. `_migrate_tasks` --apply: handles nested tasks; correct project_id; `task_type='human'`, `agent=null`.
10. `_report_summary` produces parseable counts in dry-run vs apply modes.

Mock pattern: `unittest.mock.MagicMock` for the supabase client + table chain. Don't hit real Supabase in tests.

### 4.3 Larry-actions checklist (post-merge)

Document inline at top of script's module docstring AND in a new `docs/runbooks/migrate-mission-control.md` file:

```
1. SSH into Marvin OR open Finder via Tailscale; navigate to
   /Users/marvinrogers/.openclaw/workspace/
2. Copy 3 files (programs.json, projects.json, assignees.json) to a tmpdir
   on Larry's Mac OR to the droplet (whichever is more convenient — script
   runs in both places).
3. Set env vars OR pass via flags:
   - set -a; source ~/credentials/.env.larry; set +a    (on droplet)
   - export SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=...    (on Mac)
4. Dry-run:
   python3 scripts/migrate_mission_control.py --source-dir /tmp/mc-export
5. Read the output. If any program needs renaming or any project should
   be skipped, edit the JSON files in /tmp/mc-export/ accordingly (or
   add CLI flags later if this comes up).
6. Apply:
   python3 scripts/migrate_mission_control.py --source-dir /tmp/mc-export --apply
7. Verify in dashboard:
   - app.supabase.com/project/ezldtkbhexyrgujqmxpd → Table Editor
   - Check: programs count = 5 (matched existing) or 5+N (new ones inserted)
   - Check: projects count = 28 (or whatever MC actually has)
   - Check: tasks count > 0
8. Keep the source JSON files for 1 week as fallback. After parallel-run
   ends + Mission Control decommissions (E4.5), delete the tmpdir.
```

Time required: ~10-15 min total (5 min copying files + 1 min dry-run + 1 min apply + 5 min verify).

---

## 5. Out of scope (explicit deferrals)

- **Bi-directional sync.** This is one-shot migration; if Larry edits in Mission Control during parallel-run, those edits don't propagate. The convention is "after migration, edit only in the new dashboard." Re-running `--apply` would idempotently skip existing rows (no overwrite); a real re-sync would need explicit `--update-existing` flag — deferred until requested.
- **Mission Control assignees.json content.** MC assignees are reference data (list of people who can be assigned to tasks). Our schema uses free-text `assignee` field; no Assignee table needed. Script reads but ignores assignees.json (just validates it exists).
- **Task ordering preservation.** MC may have an explicit ordering; we use `position` defaulting to 0 (or sequence within project). Re-order in dashboard UI after migration if it matters.
- **Project links → smart structured format.** Mission Control stores links as `{label, url}` arrays — we keep that shape in JSONB. No URL validation, no deduplication.
- **Decision-history reconstruction.** No MC decisions concept. New dashboard's Decisions table starts empty; Larry creates decisions there going forward.
- **Soft-delete vs hard-delete for archived projects.** MC `archived=true` projects become `status='dropped'`; we don't filter them out. Larry can manually hard-delete via dashboard if they're truly junk.
- **Color migration.** If MC programs have UI colors (hex), preserve them. Don't generate new ones for programs that don't.

---

## 6. Architecture decisions locked

| Decision | Value | Rationale |
|---|---|---|
| Script location | `scripts/migrate_mission_control.py` in `ourliberty-agent-core` | One-shot migration utility, fits the existing scripts/ pattern. Not part of the agent OS daemon. |
| Target schema | E4.1 schema-v1 in Supabase project `ezldtkbhexyrgujqmxpd` | Only Supabase project that exists. |
| Default mode | dry-run (must pass `--apply` to write) | Destructive op; explicit opt-in. |
| Idempotency | via `external_id` field lookup before insert | Re-runnable safely. |
| Program name matching | case-insensitive substring match → reuse UUID | Avoids duplicating "TruPath" if MC has it AND we seeded it. |
| Project translation | passthrough where possible; explicit field map in docstring | One canonical reference, no hidden translations. |
| Status enum translation | `blocker` non-null → 'blocked'; `archived=true` → 'dropped'; else passthrough | Closest semantic mapping; doesn't lose data. |
| Tasks parsing | nested inside projects.json | Matches MC's storage model. |
| Decisions / Events | NOT populated | MC has no equivalent concepts. Forward-only. |
| Test approach | unittest + MagicMock for supabase client | No integration test against real Supabase. |
| Output format | print() statements grouped by phase | Larry reads, decides. No formal log file. |
| Field for "where data came from" | `external_id` = MC's id (UUID or slug) | Forensic audit trail; enables future "find by MC source" queries. |

---

## 7. Dependencies

- **Hard prereq:** E4.1 schema v1 + 0002 GRANT migration applied. Verified by validation queries returning 6 programs.
- **Hard prereq:** `supabase` Python client installed on whichever machine runs the script. Droplet has it; Larry's Mac may need `pip3 install supabase` if running locally.
- **Hard prereq:** Larry has access to Marvin Mission Control's JSON files via Tailscale or SCP from `/Users/marvinrogers/.openclaw/workspace/`.
- **Soft prereq:** Larry has read the dry-run output and is satisfied no Project should be renamed/skipped/edited before --apply.

---

## 8. Risks + rollback

| Risk | Mitigation | Rollback |
|---|---|---|
| Script inserts data Larry doesn't want | Dry-run by default; Larry reviews before --apply | DELETE FROM tables (CASCADE handles cleanup); MC source files unchanged so re-run after edit. |
| Program name match is too loose (e.g., "Personal" matches "Personal Projects") | Case-insensitive EXACT match (not substring); script logs both names side-by-side for review | Edit MC JSON to rename, re-run dry-run. |
| Status enum translation surprises Larry | Field map documented; dry-run shows translated values | Update via dashboard post-apply OR re-import with edited JSON. |
| `external_id` collisions between MC and future imports | MC uses string IDs; future imports use different ID schemes; partial index on `external_id IS NOT NULL` doesn't enforce global uniqueness but collisions would be informational, not destructive | Manual cleanup if it happens. |
| Re-run after partial failure leaves orphaned partial state | Idempotency via `external_id` lookup — re-run is safe, will resume where it left off | Re-run --dry-run first to confirm partial state matches expectation. |
| MC has stale/dead projects Larry no longer cares about | Script imports everything; Larry curates post-import via dashboard (delete via UI) | Same as above. |
| MC JSON schema has changed since the research pass | `_load_source` validates required fields per programs/projects keys + raises clear error if missing | Update field-map docstring + script; re-run. |

---

## 9. Effort + cost

| Item | LLM cost | Wall clock | Larry-time |
|---|---|---|---|
| E4.2 spec PR + Mirror review | ~$0.50 | ~5 min | 2 min approval |
| E4.2 Forge build (script + tests + runbook) | ~$6 | ~25 min | 2 min approval of plan |
| Mirror review + auto-merge | ~$0.50 | ~3 min | None |
| Larry: copy files + run --dry-run + --apply + verify | $0 | ~15 min | ~15 min |
| **Total E4.2** | **~$7** | **~50 min wall clock** | **~20 min Larry-time** |

---

## 10. Validation (post-merge + post-Larry-apply)

Before declaring E4.2 done, all of these must be true:

- [ ] `python3 scripts/migrate_mission_control.py --source-dir /tmp/mc-export --dry-run` runs without error and outputs phase-by-phase summary.
- [ ] Larry reviews dry-run output and confirms it matches expectations (program names, project count, task count).
- [ ] `--apply` run succeeds; ends with `SUCCESS:` line and counts.
- [ ] In Supabase Table Editor:
  - `programs` row count = max(6, 6 + N_new) where N_new is any program Larry has in MC beyond the 5 seeded.
  - `projects` row count ≈ 28 (Larry's actual MC project count).
  - `tasks` row count > 0.
- [ ] From droplet: query for any one Project by `external_id` → returns the row with all expected fields populated.
- [ ] Re-running `--apply` immediately reports 0 new inserts (all already exist via external_id dedup).

---

## 11. Open questions (none expected)

If MC schema has fields the spec doesn't anticipate, Forge CLARIFY_REQUESTs with the specific field + proposed mapping. Otherwise this is execution.

---

*This sub-spec lives at `agents/beacon/specs/e4-2-mission-control-migration.md`. Parent: [e4-overview.md](e4-overview.md). Predecessors: [e4-0-supabase-activation.md](e4-0-supabase-activation.md), [e4-1-schema-v1.md](e4-1-schema-v1.md). Update parent doc's § 6 sequencing checklist when E4.2 ships.*
