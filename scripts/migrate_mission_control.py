#!/usr/bin/env python3
"""migrate_mission_control.py — One-shot migration from Marvin Mission Control
JSON files to the Supabase PM schema (E4.1).

Dry-run by default; pass --apply to actually write. Idempotent: re-running
skips rows whose external_id is already present in Supabase.

Field mapping reference
=======================

MC programs.json shape: {"programs": [...], "lastUpdated": "..."}
MC programs → Supabase `programs`:
  id          → external_id
  name        → name
  description → description
  (no color)  → color stays NULL on migrated rows (Larry can edit via dashboard)

MC projects.json shape: {"projects": [...], "lastUpdated": "..."}
MC projects → Supabase `projects`:
  id              → external_id
  name            → name
  description     → description
  reportingBrief  → reporting_brief
  owner           → owner
  status          → status (translated; see below)
  priority        → priority (passthrough: high|medium|low; default 'medium')
  blocker         → blocker_type
  blockerNote     → blocker_note
  nextAction      → next_action
  whyItMatters    → why_it_matters
  links           → links (JSONB; preserved verbatim as array of {label,url})
  programId       → program_id (looked up via Programs name→UUID map)
  startedAt       → started_at (date YYYY-MM-DD → timestamptz at 00:00 UTC)
  lastUpdated     → last_updated (date YYYY-MM-DD → timestamptz at 00:00 UTC)
  (programName    → not migrated; FK resolved via programId)
  project_type    → 'personal' (default; Larry can recategorize via dashboard)

MC nested tasks (inside each project's `tasks` array) → Supabase `tasks`:
  id           → external_id
  name / title → name
  description  → description
  assignee     → assignee (free-text)
  dueDate / due_date → due_date (DATE; null if missing or unparseable)
  completed / status → status ('completed' if completed=true else 'pending')
  task_type    → 'human' (hardcoded; MC has no agent dispatches)
  agent        → NULL (hardcoded)

Status translation (projects):
  blocker non-null/non-empty  → 'blocked'
  archived=true               → 'dropped'
  else                        → MC value passthrough
                                ('notstarted'|'inprogress'|'done')

Fields NOT migrated:
  - MC createdAt timestamps on projects/tasks (Supabase NOW() at insert).
  - MC assignees.json (reference list of names; we use free-text assignee).
  - MC programName denormalization on projects (FK resolved via programId).

Decisions and Events tables: NOT populated. MC has no equivalent concept.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


REQUIRED_FILES = ("programs.json", "projects.json")


# ============================================================
# Argparse
# ============================================================

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="migrate_mission_control.py",
        description=(
            "One-shot migration from Marvin Mission Control JSON files to the "
            "Supabase PM schema (E4.1). Dry-run by default; pass --apply to write."
        ),
    )
    parser.add_argument(
        "--source-dir",
        required=True,
        help=(
            "Directory containing programs.json and projects.json (copied from "
            "Marvin Mission Control's /Users/marvinrogers/.openclaw/workspace/)."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to Supabase. Default: dry-run (no writes).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N projects (for incremental testing).",
    )
    parser.add_argument(
        "--target-program",
        default=None,
        help=(
            "Only migrate projects under this MC program name "
            "(useful for partial migrations during validation)."
        ),
    )
    parser.add_argument(
        "--supabase-url",
        default=None,
        help="Override SUPABASE_URL env var (useful when running on Larry's Mac).",
    )
    parser.add_argument(
        "--supabase-key",
        default=None,
        help="Override SUPABASE_SERVICE_ROLE_KEY env var.",
    )
    return parser.parse_args(argv)


# ============================================================
# Source loading
# ============================================================

def _load_source(source_dir: str) -> tuple[list[dict], list[dict]]:
    """Read programs.json + projects.json from source_dir. Return
    (programs_list, projects_list). Raises FileNotFoundError or ValueError
    with a clear message if the dir or a file is missing/malformed."""
    src = Path(source_dir)
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(
            f"--source-dir does not exist or is not a directory: {source_dir}"
        )

    for required in REQUIRED_FILES:
        if not (src / required).exists():
            raise FileNotFoundError(
                f"Required file missing in --source-dir: {required} "
                f"(expected at {src / required})"
            )

    try:
        programs_raw = json.loads((src / "programs.json").read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"programs.json is not valid JSON: {exc}") from exc

    try:
        projects_raw = json.loads((src / "projects.json").read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"projects.json is not valid JSON: {exc}") from exc

    # MC wraps both as {"programs": [...], "lastUpdated": "..."} and
    # {"projects": [...], "lastUpdated": "..."}.
    if not isinstance(programs_raw, dict) or "programs" not in programs_raw:
        raise ValueError(
            "programs.json must be a dict containing a 'programs' key "
            "(MC's dict-wrapped shape)."
        )
    if not isinstance(projects_raw, dict) or "projects" not in projects_raw:
        raise ValueError(
            "projects.json must be a dict containing a 'projects' key "
            "(MC's dict-wrapped shape)."
        )

    programs = programs_raw["programs"]
    projects = projects_raw["projects"]
    if not isinstance(programs, list) or not isinstance(projects, list):
        raise ValueError("Both 'programs' and 'projects' keys must be lists.")

    return programs, projects


# ============================================================
# Supabase client
# ============================================================

def _connect_supabase(url: Optional[str], key: Optional[str]):
    """Create a Supabase client. url/key arguments win over env vars."""
    final_url = url or os.environ.get("SUPABASE_URL")
    final_key = key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not final_url or not final_key:
        raise SystemExit(
            "ERROR: Supabase credentials missing. Either:\n"
            "  - export SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... before running, OR\n"
            "  - pass --supabase-url ... --supabase-key ... on the command line.\n"
            "On droplet: set -a; source ~/credentials/.env.larry; set +a"
        )

    try:
        from supabase import create_client
    except ImportError as exc:
        raise SystemExit(
            "ERROR: supabase-py is not installed. Run:\n"
            "  pip3 install --user --break-system-packages supabase"
        ) from exc

    return create_client(final_url, final_key)


# ============================================================
# Status / timestamp helpers
# ============================================================

def _translate_status(mc_project: dict) -> str:
    """Apply MC → Supabase status enum translation."""
    if mc_project.get("archived") is True:
        return "dropped"
    blocker = mc_project.get("blocker")
    if blocker is not None and blocker != "":
        return "blocked"
    mc_status = mc_project.get("status")
    if mc_status in ("notstarted", "inprogress", "done", "blocked", "dropped"):
        return mc_status
    return "notstarted"


def _date_to_timestamptz(value: Any) -> Optional[str]:
    """Convert a MC date string like '2026-04-13' to an ISO8601 timestamptz
    at 00:00:00 UTC. Returns None if value is missing or unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.isoformat()


def _parse_due_date(value: Any) -> Optional[str]:
    """Return a YYYY-MM-DD date string for the tasks.due_date column."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d")


def _translate_task_status(mc_task: dict) -> str:
    if mc_task.get("completed") is True:
        return "completed"
    status = mc_task.get("status")
    if status in ("pending", "in_progress", "blocked", "completed", "cancelled"):
        return status
    return "pending"


# ============================================================
# Phase 1: Programs
# ============================================================

def _migrate_programs(
    client, mc_programs: list[dict], dry_run: bool
) -> tuple[dict[str, str], dict[str, int]]:
    """Match each MC program against existing Supabase programs by
    case-insensitive EXACT name match. Insert any that don't match.
    Returns ({mc_program_id: supabase_uuid}, {matched, inserted, skipped})."""
    print("PHASE 1: PROGRAMS")
    print(f"  Found {len(mc_programs)} MC programs.")

    existing = client.table("programs").select("id,name,external_id").execute()
    existing_rows = existing.data or []
    name_to_uuid: dict[str, str] = {
        (row["name"] or "").lower(): row["id"] for row in existing_rows
    }
    existing_external_ids: set[str] = {
        row["external_id"] for row in existing_rows if row.get("external_id")
    }

    mc_id_to_supabase_uuid: dict[str, str] = {}
    matched = 0
    inserted = 0
    skipped = 0

    for mc_program in mc_programs:
        mc_id = mc_program.get("id")
        mc_name = mc_program.get("name", "")
        key = (mc_name or "").lower()

        if mc_id and mc_id in existing_external_ids:
            uuid = next(
                row["id"] for row in existing_rows
                if row.get("external_id") == mc_id
            )
            print(f"  [SKIP]   {mc_name!r:30}  external_id={mc_id} already linked")
            mc_id_to_supabase_uuid[mc_id] = uuid
            skipped += 1
            continue

        if key in name_to_uuid:
            uuid = name_to_uuid[key]
            print(f"  [MATCH]  {mc_name!r:30}  → existing UUID {uuid[:8]}...")
            if mc_id:
                mc_id_to_supabase_uuid[mc_id] = uuid
                if not dry_run:
                    client.table("programs").update(
                        {"external_id": mc_id}
                    ).eq("id", uuid).execute()
            matched += 1
            continue

        # No name match → INSERT
        row = {
            "external_id": mc_id,
            "name": mc_name,
            "description": mc_program.get("description"),
        }
        if dry_run:
            print(f"  [INSERT] {mc_name!r:30}  (would be inserted)")
        else:
            res = client.table("programs").insert(row).execute()
            new_uuid = (res.data or [{}])[0].get("id")
            if new_uuid and mc_id:
                mc_id_to_supabase_uuid[mc_id] = new_uuid
                name_to_uuid[key] = new_uuid
            print(f"  [INSERT] {mc_name!r:30}  → new UUID {(new_uuid or '?')[:8]}...")
        inserted += 1

    print(
        f"  Summary: {matched} matched, {inserted} "
        f"{'would be inserted' if dry_run else 'inserted'}, {skipped} skipped."
    )
    return mc_id_to_supabase_uuid, {
        "matched": matched, "inserted": inserted, "skipped": skipped,
    }


# ============================================================
# Phase 2: Projects
# ============================================================

def _migrate_projects(
    client,
    mc_projects: list[dict],
    program_id_map: dict[str, str],
    dry_run: bool,
    limit: Optional[int],
    target_program: Optional[str],
) -> tuple[dict[str, str], dict[str, int]]:
    """Insert MC projects under their Supabase program FK. Returns
    ({mc_project_id: supabase_uuid}, {inserted, skipped, orphaned})."""
    print()
    print("PHASE 2: PROJECTS")

    filtered = mc_projects
    if target_program:
        filtered = [
            p for p in filtered
            if (p.get("programName") or "").lower() == target_program.lower()
        ]
    if limit is not None:
        filtered = filtered[:limit]

    print(f"  Found {len(filtered)} MC projects to consider "
          f"(of {len(mc_projects)} total).")

    existing = client.table("projects").select("id,external_id").execute()
    existing_external_ids: dict[str, str] = {
        row["external_id"]: row["id"]
        for row in (existing.data or [])
        if row.get("external_id")
    }

    mc_id_to_supabase_uuid: dict[str, str] = {}
    inserted = 0
    skipped = 0
    orphaned = 0

    for mc_project in filtered:
        mc_id = mc_project.get("id")
        name = mc_project.get("name", "")

        if mc_id and mc_id in existing_external_ids:
            uuid = existing_external_ids[mc_id]
            print(f"  [SKIP]   {name!r:40}  external_id={mc_id} already exists")
            mc_id_to_supabase_uuid[mc_id] = uuid
            skipped += 1
            continue

        program_mc_id = mc_project.get("programId")
        program_uuid = program_id_map.get(program_mc_id) if program_mc_id else None
        if not program_uuid:
            print(
                f"  [ORPHAN] {name!r:40}  programId={program_mc_id} "
                "has no Supabase program match — skipping"
            )
            orphaned += 1
            continue

        row = {
            "external_id": mc_id,
            "program_id": program_uuid,
            "name": name,
            "description": mc_project.get("description"),
            "reporting_brief": mc_project.get("reportingBrief"),
            "owner": mc_project.get("owner"),
            "status": _translate_status(mc_project),
            "priority": mc_project.get("priority") or "medium",
            "blocker_type": mc_project.get("blocker"),
            "blocker_note": mc_project.get("blockerNote"),
            "next_action": mc_project.get("nextAction"),
            "why_it_matters": mc_project.get("whyItMatters"),
            "links": mc_project.get("links") or [],
            "project_type": "personal",
        }
        started_at = _date_to_timestamptz(mc_project.get("startedAt"))
        if started_at:
            row["started_at"] = started_at
        last_updated = _date_to_timestamptz(mc_project.get("lastUpdated"))
        if last_updated:
            row["last_updated"] = last_updated

        program_name = mc_project.get("programName") or "?"
        if dry_run:
            print(
                f"  [INSERT] {name!r:40}  program={program_name} "
                f"status={row['status']} (would be inserted)"
            )
            # Use a placeholder UUID so the tasks phase can preview
            # nested tasks under projects that would be inserted.
            if mc_id:
                mc_id_to_supabase_uuid[mc_id] = f"(dry-run:{mc_id})"
        else:
            res = client.table("projects").insert(row).execute()
            new_uuid = (res.data or [{}])[0].get("id")
            if new_uuid and mc_id:
                mc_id_to_supabase_uuid[mc_id] = new_uuid
            print(
                f"  [INSERT] {name!r:40}  program={program_name} "
                f"status={row['status']} → {(new_uuid or '?')[:8]}..."
            )
        inserted += 1

    print(
        f"  Summary: {inserted} {'would be inserted' if dry_run else 'inserted'}, "
        f"{skipped} skipped, {orphaned} orphaned."
    )
    return mc_id_to_supabase_uuid, {
        "inserted": inserted, "skipped": skipped, "orphaned": orphaned,
    }


# ============================================================
# Phase 3: Tasks (nested inside MC projects)
# ============================================================

def _migrate_tasks(
    client,
    mc_projects: list[dict],
    project_id_map: dict[str, str],
    dry_run: bool,
) -> int:
    """Insert tasks nested in each MC project's `tasks` array."""
    print()
    print("PHASE 3: TASKS")

    all_tasks: list[tuple[dict, str]] = []
    for mc_project in mc_projects:
        mc_proj_id = mc_project.get("id")
        if mc_proj_id not in project_id_map:
            continue
        supabase_project_uuid = project_id_map[mc_proj_id]
        for mc_task in mc_project.get("tasks") or []:
            all_tasks.append((mc_task, supabase_project_uuid))

    print(
        f"  Found {len(all_tasks)} MC tasks across "
        f"{len(project_id_map)} migrated projects."
    )

    existing = client.table("tasks").select("id,external_id").execute()
    existing_external_ids: set[str] = {
        row["external_id"] for row in (existing.data or [])
        if row.get("external_id")
    }

    inserted = 0
    skipped = 0

    for mc_task, project_uuid in all_tasks:
        mc_id = mc_task.get("id")
        if mc_id and mc_id in existing_external_ids:
            skipped += 1
            continue

        name = mc_task.get("name") or mc_task.get("title") or "(untitled)"
        row = {
            "external_id": mc_id,
            "project_id": project_uuid,
            "name": name,
            "description": mc_task.get("description"),
            "task_type": "human",
            "agent": None,
            "status": _translate_task_status(mc_task),
            "assignee": mc_task.get("assignee"),
        }
        due = _parse_due_date(mc_task.get("dueDate") or mc_task.get("due_date"))
        if due:
            row["due_date"] = due

        if dry_run:
            print(f"  [INSERT] task {name!r}  status={row['status']} (would be inserted)")
        else:
            client.table("tasks").insert(row).execute()
            print(f"  [INSERT] task {name!r}  status={row['status']}")
        inserted += 1

    print(
        f"  Summary: {inserted} {'would be inserted' if dry_run else 'inserted'}, "
        f"{skipped} skipped."
    )
    return inserted


# ============================================================
# Summary
# ============================================================

def _report_summary(
    program_stats: dict[str, int],
    project_stats: dict[str, int],
    task_count: int,
    dry_run: bool,
) -> None:
    print()
    if dry_run:
        print("--dry-run: NO writes made. Re-run with --apply to commit.")
        print(
            f"Would insert {program_stats['inserted']} programs, "
            f"{project_stats['inserted']} projects, {task_count} tasks "
            f"({program_stats['matched']} programs matched existing)."
        )
    else:
        print(
            f"SUCCESS: {program_stats['inserted']} programs, "
            f"{project_stats['inserted']} projects, {task_count} tasks inserted; "
            f"{program_stats['matched']} programs matched existing."
        )


# ============================================================
# Entry point
# ============================================================

def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    try:
        mc_programs, mc_projects = _load_source(args.source_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    client = _connect_supabase(args.supabase_url, args.supabase_key)
    dry_run = not args.apply

    if dry_run:
        print("=== DRY RUN (pass --apply to write) ===")
    else:
        print("=== APPLYING TO SUPABASE ===")
    print()

    program_id_map, program_stats = _migrate_programs(
        client, mc_programs, dry_run=dry_run
    )
    project_id_map, project_stats = _migrate_projects(
        client,
        mc_projects,
        program_id_map,
        dry_run=dry_run,
        limit=args.limit,
        target_program=args.target_program,
    )
    task_count = _migrate_tasks(client, mc_projects, project_id_map, dry_run=dry_run)
    _report_summary(program_stats, project_stats, task_count, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
