#!/usr/bin/env python3
"""Tests for scripts/brief_check.py — the builder-brief gate.

Every rule has a test that goes RED with the rule removed (the PR body lists the
mutation per test). The claim fixtures are the ACTUAL sentences from the briefs
behind RSDPM #309 r1-F1/F2, #297 r1-F1, #301 r1-F1 and #294 r1-F2 — the check
must refuse each one as written and pass it once the evidence the rule asks for
is beside it. The structure rules run against a throwaway git repo with two
migrations on a local `origin/main`; nothing here reads ~/dev/RSDPM.

Run:
    cd <worktree-root> && python3 -m unittest tests.test_brief_check -v
"""
from __future__ import annotations

import contextlib
import io
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import brief_check as bc  # noqa: E402


def rows_of(rule: str, rows):
    return [r for r in rows if r.rule == rule]


def fails(rows, rule=None):
    return [r for r in bc.failures(rows) if rule is None or r.rule == rule]


# The sentences, verbatim from the briefs the reviews found. ----------------- #
J_F1 = (
    "## THE MANAGER'S RULINGS for the screens\n\n"
    "**S3 — The proposal is ONE bounded model call.** reads BOTH rows through the session client (the kind's\n"
    "`DISPLAY_PROJECTION`, `whitelist.ts:212-222`, PLUS `notes` for task/project since `EDIT_FIELDS` renders it and the\n"
    "DB door accepts it — Houston's mirror rule `field_not_allowed` does NOT apply here because this is not a Houston\n"
    "proposal and the write goes through `merge_records`, not `tap.ts`) → ONE call.\n"
)
J_F2 = (
    "## Build exactly this\n\n"
    "3. **The same-kind picker**: a server-side search over the kind (person display name / org name / task title /\n"
    "   project name — confirmed only, session client, `neq(\"id\", selfId)`, ordered by name, capped at 200 like `LIST_CAP`),\n"
    "   rendered as a list of rows.\n"
)
G2_F1 = (
    "## The fix\n\n"
    "3. **The resent why.** The DB's Redate arm KEEPS the stored reason and ignores the resent text. So: when the human\n"
    "   leaves the pre-filled why untouched, nothing changes (correct). When they EDIT it, the DB keeps the OLD why.\n"
)
W_F1 = (
    "## Build\n\n"
    "- `name` = ONE content-free token: for `P0001` (our `RAISE EXCEPTION 'token: detail'`) the message's leading `[a-z_]+`\n"
    "  before the first `:` or space; for `23514` / `23505` the `constraint \"([a-z_]+)\"` captured from `message`.\n"
)
W_PAYLOAD = (
    "## Build\n\n"
    "3. When a run row exists, call `finalize_extraction_run(run_id, {\"refusal\": {\"code\": c, \"name\": n}}, \"failed\")`\n"
    "   so the row that already says completed_at carries the refusal.\n"
)
F_LEAF = (
    "## A. Migration\n\n"
    "- the party rule (`task_waiting_party_required`) → locked read `… AND t.record_status = 'confirmed' FOR UPDATE` (tasks are LEAVES —\n"
    "  nothing takes FOR KEY SHARE on them, so the plain lock is right).\n"
)


class ClaimRules(unittest.TestCase):
    def check(self, text, **kw):
        return bc.check_text(text, repo=None, db_functions=kw.pop("db_functions", set()), **kw)

    # ---- claim-quote: "does not apply" / "by design" / "deliberately" ---- #
    def test_309_F1_does_not_apply_without_a_quote_is_refused(self):
        rows = self.check(J_F1)
        f = fails(rows, "claim-quote")
        self.assertEqual(len(f), 1, [r.note for r in rows])
        self.assertIn("does not apply", f[0].note)
        self.assertIn("a citation is not a quote", f[0].note)  # the paragraph DOES cite whitelist.ts:212-222

    def test_309_F1_passes_once_the_rule_is_quoted_in_the_section(self):
        quoted = J_F1 + "\n```\n$ sed -n 19,29p lib/houston/whitelist.ts\n// notes is deliberately NOT on the mirror …\n```\n"
        self.assertEqual(fails(self.check(quoted), "claim-quote"), [])
        self.assertEqual(rows_of("claim-quote", self.check(quoted))[0].status, "ok")

    def test_a_quote_in_a_DIFFERENT_section_does_not_count(self):
        text = J_F1 + "\n## Elsewhere\n\n```\nthe quote\n```\n"
        self.assertEqual(len(fails(self.check(text), "claim-quote")), 1)

    def test_by_design_and_deliberately_are_quote_claims(self):
        for word in ("none by design: the control offers only the registry's targets", "notes is deliberately off the mirror"):
            rows = self.check("## S\n\nThe row: %s.\n" % word)
            self.assertEqual(len(fails(rows, "claim-quote")), 1, word)

    # ---- claim-cap ------------------------------------------------------- #
    def test_309_F2_a_cap_with_no_excluded_item_sentence_is_refused(self):
        f = fails(self.check(J_F2), "claim-cap")
        self.assertEqual(len(f), 1)
        self.assertIn("EXCLUDED", f[0].note)

    def test_309_F2_passes_when_the_section_says_what_the_201st_row_means(self):
        text = J_F2 + "   The 201st match is a twin the picker cannot show: the search is server-side, so the human types more.\n"
        self.assertEqual(fails(self.check(text), "claim-cap"), [])

    # ---- claim-cite ------------------------------------------------------ #
    def test_297_F1_keeps_and_ignores_without_a_citation_is_refused(self):
        f = fails(self.check(G2_F1), "claim-cite")
        self.assertEqual(len(f), 1)
        self.assertIn("ignores/discards", f[0].note)

    def test_297_F1_passes_with_a_file_line_citation_in_the_paragraph(self):
        text = G2_F1.replace("KEEPS the stored reason", "KEEPS the stored reason (`0067_task_lifecycle_full.sql:387-402`)")
        self.assertEqual(fails(self.check(text), "claim-cite"), [])

    def test_301_F1_a_regex_claim_without_a_citation_is_refused(self):
        f = fails(self.check(W_F1), "claim-cite")
        self.assertEqual(len(f), 1)

    def test_301_F1_a_repo_db_function_called_with_a_literal_payload_is_a_claim(self):
        self.assertEqual(len(fails(self.check(W_PAYLOAD, db_functions={"finalize_extraction_run"}), "claim-cite")), 1)
        # without the repo's function list the sentence is not recognised — the rule depends on the repo, stated
        self.assertEqual(rows_of("claim-cite", self.check(W_PAYLOAD)), [])

    def test_294_F2_tasks_are_leaves_without_a_citation_is_refused(self):
        f = fails(self.check(F_LEAF), "claim-cite")
        self.assertEqual(len(f), 1)
        self.assertIn("leaf", f[0].note)

    def test_a_fenced_block_in_the_section_is_evidence_for_a_cite_claim(self):
        text = F_LEAF + "\n```\n$ grep -n 'REFERENCES tasks' supabase/migrations/*.sql\n0058_task_threads.sql:40:  task_id uuid REFERENCES tasks(id)\n```\n"
        self.assertEqual(fails(self.check(text), "claim-cite"), [])

    def test_never_X_and_no_caller_are_cite_claims(self):
        for s in ("the route never throws into the sheet", "`classifyFoo` has no caller after this PR", "nothing references `x`"):
            self.assertEqual(len(fails(self.check("## S\n\n%s.\n" % s), "claim-cite")), 1, s)

    # ---- what is NOT a claim ----------------------------------------------- #
    def test_text_inside_a_fence_is_never_a_claim(self):
        text = "## S\n\n```\nthis rule does NOT apply here, by design; never throws; capped at 200\n```\n"
        self.assertEqual(bc.failures(self.check(text)), [])

    def test_a_heading_is_not_a_paragraph(self):
        self.assertEqual(bc.failures(self.check("## Why the mirror rule does not apply\n\nplain text.\n")), [])

    def test_the_standing_wrapper_lines_are_not_claims(self):
        text = "## Rules\n\n- Staging is real: READ only. Never write to staging and never delete a row.\n- Nothing else changes.\n"
        self.assertEqual(bc.failures(self.check(text)), [])

    def test_decisions_mode_runs_only_the_claim_rules(self):
        text = "# decisions\n\n## F1\nRe-point `supabase/migrations/0070_merge_records.sql`; the refusal refuses refused refusing.\n"
        rows = self.check(text, decisions_only=True)
        self.assertEqual(rows_of("predicate-table", rows), [])
        self.assertEqual(rows_of("two-clocks", rows), [])

    def test_the_info_row_counts_claim_paragraphs(self):
        rows = self.check(J_F1 + "\n" + G2_F1)
        info = rows_of("claims", rows)[0]
        self.assertIn("2 claim paragraph(s), 0 with evidence", info.note)


# --------------------------------------------------------------------------- #
# R1 carried-no-reader — the ORIGINAL brief-J2-step2.md S3 sentence (#318 r2-F2)
# --------------------------------------------------------------------------- #
R1_RED = ("`created_by_name` / `created_at` are NOT rendered in step 2 (no reader asked; the row keeps them — one line in "
          "the panel's header comment).")
R1_GREEN = ("`created_by` / `created_by_name` / `created_at` are NOT carried into the app row at all — a field with no "
            "reader is not carried.")
R1_GREEN_READER = "… `created_at` is kept on the row; `RecordLinksPanel.tsx:140` renders it as the \"linked on\" line."


class CarriedNoReader(unittest.TestCase):
    def check(self, text, **kw):
        return bc.check_text(text, repo=None, db_functions=set(), **kw)

    def test_318_r2_F2_a_kept_field_with_no_reader_is_refused(self):
        f = fails(self.check("## S3\n\n" + R1_RED + "\n"), "carried-no-reader")
        self.assertEqual(len(f), 1, [r.note for r in self.check(R1_RED)])
        self.assertEqual(f[0].line, 3)
        self.assertIn("name the reader or do not carry it", f[0].note)
        self.assertIn("`created_by_name`", f[0].note)

    def test_the_same_sentence_is_refused_in_a_decisions_file(self):
        self.assertEqual(len(fails(self.check("## F2\n" + R1_RED + "\n", decisions_only=True), "carried-no-reader")), 1)

    def test_a_negated_keep_is_a_drop_and_passes(self):
        rows = self.check("## S3\n\n" + R1_GREEN + "\n")
        self.assertEqual(fails(rows, "carried-no-reader"), [])
        self.assertEqual(rows_of("carried-no-reader", rows), [])  # nothing is kept, so nothing is asked

    def test_a_named_reader_passes(self):
        rows = self.check("## S3\n\n" + R1_GREEN_READER + "\n")
        self.assertEqual([r.status for r in rows_of("carried-no-reader", rows)], ["ok"])

    def test_a_reader_in_the_NEXT_paragraph_passes(self):
        rows = self.check("## S3\n\n`created_at` is kept on the row.\n\n`RecordLinksPanel` renders it as the linked-on line.\n")
        self.assertEqual([r.status for r in rows_of("carried-no-reader", rows)], ["ok"])

    def test_a_negated_reader_verb_is_not_a_reader(self):
        for s in ("`x` is kept; nothing reads it.", "`x` is kept; `Panel.tsx:40` never renders it."):
            self.assertEqual(len(fails(self.check("## S\n\n%s\n" % s), "carried-no-reader")), 1, s)

    def test_the_carried_field_is_not_its_own_reader(self):
        f = fails(self.check("## S\n\n`created_at` is kept; `created_at` renders nothing new.\n"), "carried-no-reader")
        self.assertEqual(len(f), 1)

    def test_a_reader_verb_more_than_six_words_away_does_not_count(self):
        s = "`x` is kept; `Panel` is the component on the page that eventually, much later, renders things."
        self.assertEqual(len(fails(self.check("## S\n\n%s\n" % s), "carried-no-reader")), 1)

    def test_the_keep_word_and_the_field_in_different_sentences_still_trigger(self):
        f = fails(self.check("## S\n\nThe app row gains `created_at`. The row keeps it.\n"), "carried-no-reader")
        self.assertEqual(len(f), 1)
        self.assertIn("`created_at`", f[0].note)

    def test_a_fenced_keep_is_never_scanned(self):
        self.assertEqual(rows_of("carried-no-reader", self.check("## S\n\n```\n`x` is kept\n```\n")), [])


# --------------------------------------------------------------------------- #
# Structure rules against a throwaway repo
# --------------------------------------------------------------------------- #
MIG_10 = """-- 0010_first.sql
CREATE OR REPLACE FUNCTION public.first_verb(p uuid) RETURNS void AS $$
BEGIN
  IF p IS NULL THEN RAISE EXCEPTION 'a_refused: %', p; END IF;
  RAISE EXCEPTION 'b_refused';
END $$ LANGUAGE plpgsql;
"""
MIG_11 = """-- 0011_second.sql
-- TWO CLOCKS
-- (a) OLD app + NEW schema: nothing changes for the live app, no row carries the new token on day one.
-- (b) NEW app + OLD schema: the Board read tolerates the retired tokens until this file is applied,
--     or the merge waits for the applier — a reader of pre-existing rows is a row in this table.
CREATE OR REPLACE FUNCTION public.first_verb(p uuid) RETURNS void AS $$
BEGIN
  IF p IS NULL THEN RAISE EXCEPTION 'a_refused: %', p; END IF;   -- re-issued from 0010
  RAISE EXCEPTION 'c_refused';
END $$ LANGUAGE plpgsql;
"""


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


class StructureRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="brief-check-"))
        cls.repo = cls.tmp / "repo"
        bare = cls.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(cls.repo)], check=True)
        mig = cls.repo / "supabase" / "migrations"
        mig.mkdir(parents=True)
        (mig / "0010_first.sql").write_text(MIG_10)
        (mig / "0011_second.sql").write_text(MIG_11)
        _git(cls.repo, "add", "-A")
        _git(cls.repo, "commit", "-q", "-m", "init")
        _git(cls.repo, "remote", "add", "origin", str(bare))
        _git(cls.repo, "push", "-q", "-u", "origin", "main")
        cls.r = bc.Repo(cls.repo, "origin/main")
        # a repo with no origin/main at all
        cls.norepo = cls.tmp / "norepo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(cls.norepo)], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def check(self, text, repo=None):
        return bc.check_text(text, repo=repo if repo is not None else self.r)

    TABLE = "## THE PREDICATE TABLE\n\n| DB refusal | the door |\n|---|---|\n| `c_refused` | the sheet hides Save |\n"

    def test_a_brief_naming_a_migration_needs_a_predicate_table(self):
        rows = self.check("# Brief\n\nBuild `supabase/migrations/0011_second.sql`.\n")
        self.assertEqual(len(fails(rows, "predicate-table")), 1)

    def test_a_brief_speaking_of_refusals_four_times_needs_one(self):
        rows = self.check("# Brief\n\nrefused refuses refusal refusing.\n")
        self.assertEqual(len(fails(rows, "predicate-table")), 1)
        rows = self.check("# Brief\n\nrefused refuses refusal.\n")
        self.assertEqual(rows_of("predicate-table", rows)[0].status, "n/a")

    def test_the_heading_needs_a_table_with_a_data_row_under_it(self):
        rows = self.check("# Brief\n\n`supabase/migrations/0011_second.sql`\n\n## Predicate table\n\nprose only\n")
        self.assertIn("no table with a data row", fails(rows, "predicate-table")[0].note)
        rows = self.check("# Brief\n\n`supabase/migrations/0011_second.sql`\n\n" + self.TABLE)
        self.assertEqual(rows_of("predicate-table", rows)[0].status, "ok")

    def test_refusal_rows_count_only_tokens_NEW_in_the_newest_named_migration(self):
        text = "# Brief\n\nRead `supabase/migrations/0010_first.sql`; build on `supabase/migrations/0011_second.sql`.\n\n" + self.TABLE
        rows = self.check(text)
        rr = rows_of("refusal-rows", rows)
        self.assertEqual([r.status for r in rr], ["ok"], [r.note for r in rr])
        self.assertIn("1 re-issued, not counted", rr[0].note)  # a_refused is 0010's; b_refused is not in 0011 at all

    def test_a_new_token_with_no_row_is_named(self):
        text = "# Brief\n\n`supabase/migrations/0011_second.sql`\n\n## Predicate table\n\n| a | b |\n|---|---|\n| `a_refused` | door |\n"
        f = fails(self.check(text), "refusal-rows")
        self.assertEqual(len(f), 1)
        self.assertIn("c_refused", f[0].note)
        self.assertNotIn("a_refused", f[0].note)

    def test_a_step_one_brief_naming_its_own_future_migration_is_not_refused_for_rows(self):
        text = "# Brief\n\n`supabase/migrations/0012_mine.sql`\n\n" + self.TABLE
        rr = rows_of("refusal-rows", self.check(text))
        self.assertEqual(rr[0].status, "n/a")
        self.assertIn("not on origin/main", rr[0].note)

    def test_a_repo_without_the_ref_fails_CLOSED(self):
        text = "# Brief\n\n`supabase/migrations/0011_second.sql`\n\n" + self.TABLE
        f = fails(self.check(text, repo=bc.Repo(self.norepo, "origin/main")), "refusal-rows")
        self.assertEqual(len(f), 1)
        self.assertIn("fetch first", f[0].note)

    def test_a_step_2_brief_needs_a_two_clocks_heading(self):
        text = "# Builder brief — PR X, STEP 2 of 2: the screens\n\n`supabase/migrations/0011_second.sql`\n\n" + self.TABLE
        f = fails(self.check(text), "two-clocks")
        self.assertEqual(len(f), 1)
        self.assertIn("TWO CLOCKS", f[0].note)

    def test_a_paraphrased_two_clocks_section_is_refused(self):
        text = ("# Builder brief — PR X, STEP 2 of 2\n\n`supabase/migrations/0011_second.sql`\n\n## TWO CLOCKS\n\n"
                "In the window where the new app runs on the old schema the board should cope with old tokens somehow.\n\n" + self.TABLE)
        f = fails(self.check(text), "two-clocks")
        self.assertEqual(len(f), 1)
        self.assertIn("verbatim", f[0].note)

    def test_a_verbatim_two_clocks_paragraph_passes_despite_comment_markers_and_wrapping(self):
        text = ("# Builder brief — PR X, STEP 2 of 2\n\n`supabase/migrations/0011_second.sql`\n\n## TWO CLOCKS — 0011's header, VERBATIM\n\n"
                "> (b) NEW app + OLD schema: the Board read tolerates the retired tokens until this file is applied, or the merge\n"
                "> waits for the applier — a reader of pre-existing rows is a row in this table.\n\n" + self.TABLE)
        tc = rows_of("two-clocks", self.check(text))
        self.assertEqual(tc[0].status, "ok", tc[0].note)

    def test_a_step_1_brief_owes_no_two_clocks_section(self):
        text = "# Builder brief — PR X, STEP 1 of 2\n\n`supabase/migrations/0012_mine.sql`\n\n" + self.TABLE
        self.assertEqual(rows_of("two-clocks", self.check(text)), [])

    def test_a_git_failure_inside_a_lookup_is_a_FAIL_row_not_an_empty_list(self):
        class Broken(bc.Repo):
            def _git(self, *args):
                if args and args[0] == "grep":
                    return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
                return super()._git(*args)
        r = Broken(self.repo, "origin/main")
        self.assertIsNone(r.db_functions())
        self.assertIsNone(r.raise_origins())
        text = "# Brief\n\n`supabase/migrations/0011_second.sql`\n\n" + self.TABLE
        rows = bc.check_text(text, repo=r)
        notes = [x.note for x in bc.failures(rows)]
        self.assertTrue(any("git grep over supabase/migrations" in n for n in notes), notes)
        self.assertTrue(any("cannot tell a new refusal from a re-issued one" in n for n in notes), notes)

    def test_db_functions_come_from_the_ref(self):
        self.assertEqual(self.r.db_functions(), {"first_verb"})
        self.assertEqual(self.r.raise_origins(), {"a_refused": "0010_first.sql", "b_refused": "0010_first.sql", "c_refused": "0011_second.sql"})


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bc.main(list(args))
        return code, buf.getvalue()

    def test_exit_codes_and_table(self):
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / "good.md"
            good.write_text("# Brief\n\nBuild the thing.\n")
            bad = Path(d) / "bad.md"
            bad.write_text(J_F1)
            code, out = self.run_cli(str(good))
            self.assertEqual(code, bc.EXIT_OK)
            self.assertIn("PASS: 0 FAIL row(s)", out)
            code, out = self.run_cli(str(bad))
            self.assertEqual(code, bc.EXIT_REFUSED)
            self.assertIn("| claim-quote |", out)
            self.assertIn("REFUSED: 1 FAIL row(s)", out)
            code, _ = self.run_cli(str(Path(d) / "missing.md"))
            self.assertEqual(code, bc.EXIT_USAGE)

    def test_there_is_no_waiver_option(self):
        p = bc.main.__globals__["argparse"].ArgumentParser()
        # re-create the parser the way main() does and read its option strings
        src = (_SCRIPTS / "brief_check.py").read_text()
        opts = re.findall(r'add_argument\("(--[a-z-]+)"', src)
        self.assertEqual(sorted(opts), ["--decisions", "--ref", "--repo"])
        for o in opts:
            for word in ("waive", "force", "skip", "allow", "no-"):
                self.assertFalse(word in o, "an option that disables the check: %s" % o)
        self.assertFalse("OL_EVIDENCE_WAIVE" in src or "os.environ" in src, "the check reads no escape hatch from the environment")


if __name__ == "__main__":
    unittest.main()
