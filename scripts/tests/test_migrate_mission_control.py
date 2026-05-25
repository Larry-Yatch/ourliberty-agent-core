#!/usr/bin/env python3
"""Tests for migrate_mission_control (E4.2).

Covers source loading (happy + error paths), program matching/insertion,
project idempotency + status translation, and task migration. Mocks the
supabase client end-to-end; does NOT hit real Supabase.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_migrate_mission_control
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import migrate_mission_control as mmc  # noqa: E402


def _write_source(dir_path: Path, programs=None, projects=None) -> Path:
    """Drop programs.json + projects.json into dir_path. Returns dir_path."""
    programs_payload = {
        "programs": programs if programs is not None else [
            {"id": "mc-prog-1", "name": "Personal", "description": "Personal projects"},
        ],
        "lastUpdated": "2026-05-24",
    }
    projects_payload = {
        "projects": projects if projects is not None else [],
        "lastUpdated": "2026-05-24",
    }
    (dir_path / "programs.json").write_text(json.dumps(programs_payload))
    (dir_path / "projects.json").write_text(json.dumps(projects_payload))
    return dir_path


def _mock_client(
    programs_existing=None,
    projects_existing=None,
    tasks_existing=None,
    insert_returns_uuid="uuid-new-row",
):
    """Build a MagicMock supabase client whose .table(name) chain returns
    pre-canned select.data, and whose insert returns insert_returns_uuid."""
    programs_existing = programs_existing or []
    projects_existing = projects_existing or []
    tasks_existing = tasks_existing or []

    table_mocks: dict[str, MagicMock] = {}

    def _make_table_mock(name: str) -> MagicMock:
        if name in table_mocks:
            return table_mocks[name]

        table_mock = MagicMock(name=f"table[{name}]")
        # select(...).execute() returns .data
        select_chain = MagicMock(name=f"select[{name}]")
        if name == "programs":
            select_chain.execute.return_value = MagicMock(data=programs_existing)
        elif name == "projects":
            select_chain.execute.return_value = MagicMock(data=projects_existing)
        elif name == "tasks":
            select_chain.execute.return_value = MagicMock(data=tasks_existing)
        else:
            select_chain.execute.return_value = MagicMock(data=[])
        table_mock.select.return_value = select_chain

        # insert(row).execute() returns data: [{id: ...}]
        insert_chain = MagicMock(name=f"insert[{name}]")
        insert_chain.execute.return_value = MagicMock(
            data=[{"id": insert_returns_uuid}]
        )
        table_mock.insert.return_value = insert_chain

        # update(row).eq(...).execute() returns success
        update_chain = MagicMock(name=f"update[{name}]")
        update_chain.eq.return_value.execute.return_value = MagicMock(data=[{}])
        table_mock.update.return_value = update_chain

        table_mocks[name] = table_mock
        return table_mock

    client = MagicMock(name="client")
    client.table.side_effect = _make_table_mock
    client._table_mocks = table_mocks
    return client


class LoadSourceTest(unittest.TestCase):
    def test_happy_path_returns_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_source(
                Path(tmp),
                programs=[{"id": "p1", "name": "Personal"}],
                projects=[{"id": "x1", "name": "Foo"}],
            )
            programs, projects = mmc._load_source(tmp)
            self.assertEqual(len(programs), 1)
            self.assertEqual(programs[0]["name"], "Personal")
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["name"], "Foo")

    def test_missing_source_dir_raises(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            mmc._load_source("/tmp/this-does-not-exist-12345")
        self.assertIn("does not exist", str(ctx.exception))

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            # write only programs.json; omit projects.json
            (Path(tmp) / "programs.json").write_text('{"programs": []}')
            with self.assertRaises(FileNotFoundError) as ctx:
                mmc._load_source(tmp)
            self.assertIn("projects.json", str(ctx.exception))

    def test_malformed_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "programs.json").write_text("{not valid json")
            (Path(tmp) / "projects.json").write_text('{"projects": []}')
            with self.assertRaises(ValueError) as ctx:
                mmc._load_source(tmp)
            self.assertIn("programs.json", str(ctx.exception))

    def test_wrong_top_level_shape_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            # bare list instead of dict-wrapped
            (Path(tmp) / "programs.json").write_text("[]")
            (Path(tmp) / "projects.json").write_text('{"projects": []}')
            with self.assertRaises(ValueError) as ctx:
                mmc._load_source(tmp)
            self.assertIn("'programs' key", str(ctx.exception))


class MigratePrograms_MatchTest(unittest.TestCase):
    def test_case_insensitive_exact_name_match(self):
        client = _mock_client(
            programs_existing=[
                {"id": "uuid-personal", "name": "Personal", "external_id": None},
            ]
        )
        mc = [{"id": "mc-1", "name": "personal", "description": "lowercase match"}]
        result, stats = mmc._migrate_programs(client, mc, dry_run=False)
        self.assertEqual(result, {"mc-1": "uuid-personal"})
        self.assertEqual(stats["matched"], 1)
        # No insert; one update to set external_id
        client._table_mocks["programs"].insert.assert_not_called()
        client._table_mocks["programs"].update.assert_called_once()

    def test_exact_match_not_substring(self):
        """MC 'Personal' must NOT match existing 'Personal Projects'."""
        client = _mock_client(
            programs_existing=[
                {"id": "uuid-pp", "name": "Personal Projects", "external_id": None},
            ]
        )
        mc = [{"id": "mc-1", "name": "Personal"}]
        result, _ = mmc._migrate_programs(client, mc, dry_run=True)
        # No match → no map entry from existing UUID; dry-run records the would-insert
        self.assertNotIn("mc-1", result)
        client._table_mocks["programs"].insert.assert_not_called()

    def test_returns_map_for_both_matched_and_inserted(self):
        client = _mock_client(
            programs_existing=[
                {"id": "uuid-personal", "name": "Personal", "external_id": None},
            ],
            insert_returns_uuid="uuid-newprog",
        )
        mc = [
            {"id": "mc-1", "name": "Personal"},
            {"id": "mc-2", "name": "BrandNew"},
        ]
        result, _ = mmc._migrate_programs(client, mc, dry_run=False)
        self.assertEqual(result["mc-1"], "uuid-personal")
        self.assertEqual(result["mc-2"], "uuid-newprog")

    def test_apply_inserts_only_unmatched(self):
        client = _mock_client(
            programs_existing=[
                {"id": "uuid-personal", "name": "Personal", "external_id": None},
            ]
        )
        mc = [
            {"id": "mc-1", "name": "Personal"},
            {"id": "mc-2", "name": "Brand New Program", "description": "x"},
        ]
        mmc._migrate_programs(client, mc, dry_run=False)
        # Exactly one insert for the new one
        self.assertEqual(client._table_mocks["programs"].insert.call_count, 1)
        inserted_row = client._table_mocks["programs"].insert.call_args[0][0]
        self.assertEqual(inserted_row["name"], "Brand New Program")
        self.assertEqual(inserted_row["external_id"], "mc-2")

    def test_idempotent_skip_when_external_id_already_present(self):
        client = _mock_client(
            programs_existing=[
                {"id": "uuid-personal", "name": "Personal", "external_id": "mc-1"},
            ]
        )
        mc = [{"id": "mc-1", "name": "Personal"}]
        mmc._migrate_programs(client, mc, dry_run=False)
        client._table_mocks["programs"].insert.assert_not_called()
        client._table_mocks["programs"].update.assert_not_called()


class MigrateProjects_Test(unittest.TestCase):
    def test_apply_passes_correct_program_id(self):
        client = _mock_client()
        mc_projects = [
            {"id": "proj-1", "name": "Foo", "programId": "mc-prog-1"},
        ]
        program_map = {"mc-prog-1": "uuid-program-1"}
        mmc._migrate_projects(client, mc_projects, program_map,
                              dry_run=False, limit=None, target_program=None)
        inserted = client._table_mocks["projects"].insert.call_args[0][0]
        self.assertEqual(inserted["program_id"], "uuid-program-1")
        self.assertEqual(inserted["external_id"], "proj-1")
        self.assertEqual(inserted["project_type"], "personal")

    def test_idempotent_skip_when_external_id_exists(self):
        client = _mock_client(
            projects_existing=[
                {"id": "uuid-foo", "external_id": "proj-1"},
            ]
        )
        mc_projects = [
            {"id": "proj-1", "name": "Foo", "programId": "mc-prog-1"},
        ]
        result, _ = mmc._migrate_projects(
            client, mc_projects, {"mc-prog-1": "uuid-program-1"},
            dry_run=False, limit=None, target_program=None,
        )
        self.assertEqual(result["proj-1"], "uuid-foo")
        client._table_mocks["projects"].insert.assert_not_called()

    def test_status_blocked_when_blocker_set(self):
        client = _mock_client()
        mc_projects = [
            {"id": "p1", "name": "Foo", "programId": "mc-prog-1",
             "blocker": "waiting-on-input"},
        ]
        mmc._migrate_projects(
            client, mc_projects, {"mc-prog-1": "uuid-1"},
            dry_run=False, limit=None, target_program=None,
        )
        row = client._table_mocks["projects"].insert.call_args[0][0]
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["blocker_type"], "waiting-on-input")

    def test_status_dropped_when_archived(self):
        client = _mock_client()
        mc_projects = [
            {"id": "p1", "name": "Foo", "programId": "mc-prog-1",
             "archived": True, "status": "inprogress"},
        ]
        mmc._migrate_projects(
            client, mc_projects, {"mc-prog-1": "uuid-1"},
            dry_run=False, limit=None, target_program=None,
        )
        row = client._table_mocks["projects"].insert.call_args[0][0]
        self.assertEqual(row["status"], "dropped")

    def test_status_passthrough_otherwise(self):
        client = _mock_client()
        mc_projects = [
            {"id": "p1", "name": "Foo", "programId": "mc-prog-1",
             "status": "inprogress"},
        ]
        mmc._migrate_projects(
            client, mc_projects, {"mc-prog-1": "uuid-1"},
            dry_run=False, limit=None, target_program=None,
        )
        row = client._table_mocks["projects"].insert.call_args[0][0]
        self.assertEqual(row["status"], "inprogress")

    def test_orphan_when_program_id_unmapped(self):
        client = _mock_client()
        mc_projects = [
            {"id": "p1", "name": "Foo", "programId": "mc-prog-unknown"},
        ]
        result, _ = mmc._migrate_projects(
            client, mc_projects, {},
            dry_run=False, limit=None, target_program=None,
        )
        self.assertEqual(result, {})
        client._table_mocks["projects"].insert.assert_not_called()

    def test_preserves_started_at_and_last_updated(self):
        client = _mock_client()
        mc_projects = [
            {"id": "p1", "name": "Foo", "programId": "mc-1",
             "startedAt": "2026-03-29", "lastUpdated": "2026-04-13"},
        ]
        mmc._migrate_projects(
            client, mc_projects, {"mc-1": "uuid-1"},
            dry_run=False, limit=None, target_program=None,
        )
        row = client._table_mocks["projects"].insert.call_args[0][0]
        self.assertEqual(row["started_at"], "2026-03-29T00:00:00+00:00")
        self.assertEqual(row["last_updated"], "2026-04-13T00:00:00+00:00")

    def test_limit_applies(self):
        client = _mock_client()
        mc_projects = [
            {"id": f"p{i}", "name": f"Foo{i}", "programId": "mc-1"}
            for i in range(5)
        ]
        mmc._migrate_projects(
            client, mc_projects, {"mc-1": "uuid-1"},
            dry_run=True, limit=2, target_program=None,
        )
        # Dry-run never inserts, but limit means we only iterate 2.
        # Easier signal: count select() calls? Just verify with target_program below.
        # Here we verify by re-running with apply and count inserts.
        client_apply = _mock_client()
        mmc._migrate_projects(
            client_apply, mc_projects, {"mc-1": "uuid-1"},
            dry_run=False, limit=2, target_program=None,
        )
        self.assertEqual(
            client_apply._table_mocks["projects"].insert.call_count, 2
        )


class MigrateTasksTest(unittest.TestCase):
    def test_nested_tasks_inserted_with_correct_project_id(self):
        client = _mock_client()
        mc_projects = [
            {"id": "p1", "name": "Foo", "programId": "mc-1",
             "tasks": [
                 {"id": "t1", "name": "Task A", "completed": False},
                 {"id": "t2", "title": "Task B", "completed": True},
             ]},
        ]
        project_id_map = {"p1": "uuid-foo"}
        count = mmc._migrate_tasks(client, mc_projects, project_id_map, dry_run=False)
        self.assertEqual(count, 2)
        self.assertEqual(client._table_mocks["tasks"].insert.call_count, 2)

        first_row = client._table_mocks["tasks"].insert.call_args_list[0][0][0]
        self.assertEqual(first_row["project_id"], "uuid-foo")
        self.assertEqual(first_row["task_type"], "human")
        self.assertIsNone(first_row["agent"])
        self.assertEqual(first_row["status"], "pending")
        self.assertEqual(first_row["name"], "Task A")

        second_row = client._table_mocks["tasks"].insert.call_args_list[1][0][0]
        self.assertEqual(second_row["status"], "completed")
        self.assertEqual(second_row["name"], "Task B")

    def test_idempotent_skip_when_external_id_exists(self):
        client = _mock_client(
            tasks_existing=[{"id": "uuid-t", "external_id": "t1"}]
        )
        mc_projects = [
            {"id": "p1", "tasks": [{"id": "t1", "name": "Done already"}]},
        ]
        count = mmc._migrate_tasks(
            client, mc_projects, {"p1": "uuid-foo"}, dry_run=False
        )
        self.assertEqual(count, 0)
        client._table_mocks["tasks"].insert.assert_not_called()

    def test_skip_tasks_for_unmigrated_project(self):
        client = _mock_client()
        mc_projects = [
            {"id": "p1", "tasks": [{"id": "t1", "name": "Foo"}]},
        ]
        # project_id_map empty → no tasks should be processed
        count = mmc._migrate_tasks(client, mc_projects, {}, dry_run=False)
        self.assertEqual(count, 0)
        client._table_mocks["tasks"].insert.assert_not_called()


class HelperTest(unittest.TestCase):
    def test_translate_status_archived_wins_over_blocker(self):
        self.assertEqual(
            mmc._translate_status({"archived": True, "blocker": "x"}),
            "dropped",
        )

    def test_translate_status_unknown_falls_back_to_notstarted(self):
        self.assertEqual(mmc._translate_status({"status": "wat"}), "notstarted")

    def test_date_to_timestamptz_handles_bad_input(self):
        self.assertIsNone(mmc._date_to_timestamptz(None))
        self.assertIsNone(mmc._date_to_timestamptz(""))
        self.assertIsNone(mmc._date_to_timestamptz("not-a-date"))

    def test_parse_due_date_returns_iso_date_string(self):
        self.assertEqual(mmc._parse_due_date("2026-06-01"), "2026-06-01")
        self.assertIsNone(mmc._parse_due_date("garbage"))


class ConnectSupabaseTest(unittest.TestCase):
    def test_missing_credentials_raises(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                mmc._connect_supabase(None, None)


class CLITest(unittest.TestCase):
    def test_help_runs_cleanly(self):
        with self.assertRaises(SystemExit) as ctx:
            mmc._parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_source_dir_is_required(self):
        with self.assertRaises(SystemExit):
            mmc._parse_args([])

    def test_main_missing_source_dir_returns_nonzero(self):
        with mock.patch.dict(
            "os.environ",
            {"SUPABASE_URL": "x", "SUPABASE_SERVICE_ROLE_KEY": "y"},
            clear=True,
        ):
            rc = mmc.main(["--source-dir", "/tmp/does-not-exist-9999"])
            self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
