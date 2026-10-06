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


def _git(cwd, *args, env=None):
    fixed = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env=dict(fixed, **(env or {})))


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


# --------------------------------------------------------------------------- #
# R2 direction-bearing-refusal — the REAL 0072 excerpt (#318 r1-F1)
# --------------------------------------------------------------------------- #
# `git -C ~/dev/RSDPM show origin/main:supabase/migrations/0072_record_links.sql | sed -n '520,575p'`, verbatim.
# RSDPM origin/main 1d3b89333a6a14f5629acf50c5b65562797bdd9d (the file last changed in 8a5a55543523d347277a1b770ebdc87558990b5c).
# Excerpt line k (0-based) is the full file's line 520 + k.
MIG_0072_EXCERPT = '''  EXECUTE format(v_fetch, public.rsdpm_table_for(v_b_type)) USING v_b_id INTO v_b_status;
  -- record_status is NOT NULL on every one of the seven tables, so NULL here
  -- means "no such row inside the wall" and nothing else.
  IF v_a_status IS NULL OR v_b_status IS NULL THEN
    RAISE EXCEPTION 'record_not_found' USING errcode = 'no_data_found';
  END IF;
  -- 7 — in the CALLER's from/to terms, whichever way round the locks went
  v_from_status := CASE WHEN v_swap THEN v_b_status ELSE v_a_status END;
  v_to_status   := CASE WHEN v_swap THEN v_a_status ELSE v_b_status END;
  IF v_from_status <> 'confirmed' THEN
    RAISE EXCEPTION 'cannot_link_%: from', v_from_status USING errcode = 'check_violation';
  END IF;
  IF v_to_status <> 'confirmed' THEN
    RAISE EXCEPTION 'cannot_link_%: to', v_to_status USING errcode = 'check_violation';
  END IF;

  -- 8 — `related` is stored ONCE, canonically. This runs BEFORE the duplicate
  -- door so the reverse call (B~A after A~B) finds the row that exists and
  -- refuses by name instead of meeting the CHECK or the UNIQUE.
  IF v_rel = 'related' AND v_swap THEN
    v_ft := v_a_type; v_fi := v_a_id; v_tt := v_b_type; v_ti := v_b_id;
  END IF;

  -- 9 — the duplicate door. Read as the definer (the owner is outside RLS), so
  -- the WALL is written out rather than inherited from the policy.
  PERFORM 1 FROM public.record_links rl
   WHERE rl.workspace_id = public.current_workspace()
     AND rl.from_type = v_ft AND rl.from_id = v_fi
     AND rl.to_type = v_tt AND rl.to_id = v_ti
     AND rl.relation = v_rel;
  IF FOUND THEN
    RAISE EXCEPTION 'link_exists' USING errcode = 'unique_violation';
  END IF;
  IF v_rel = 'blocks' THEN
    PERFORM 1 FROM public.record_links rl
     WHERE rl.workspace_id = public.current_workspace()
       AND rl.from_type = v_tt AND rl.from_id = v_ti
       AND rl.to_type = v_ft AND rl.to_id = v_fi
       AND rl.relation = 'blocks';
    IF FOUND THEN
      RAISE EXCEPTION 'link_reverse_exists' USING errcode = 'check_violation';
    END IF;
  END IF;

  -- 9b
  v_person := public.rsdpm_viewer_person(v_actor);
  IF v_person IS NULL THEN
    RAISE EXCEPTION 'no_roster_person_for_caller' USING errcode = 'insufficient_privilege';
  END IF;

  -- 10
  INSERT INTO public.record_links
    (workspace_id, from_type, from_id, to_type, to_id, relation, created_by)
  VALUES (public.current_workspace(), v_ft, v_fi, v_tt, v_ti, v_rel, v_person)
  RETURNING record_links.id INTO v_id;

'''
R2_TABLE_HEAD = ("# Brief\n\nBuild on `supabase/migrations/0072_record_links.sql`.\n\n"
                 "## THE PREDICATE TABLE\n\n| DB refusal / state | step 2 renders |\n|---|---|\n")
R2_RED_ROW = ('| `link_reverse_exists` | "<other> already blocks this record — unlink that first if it is the other way '
              'round." (the Unlink is in the panel) |')
R2_GREEN_ROW = ('| `link_reverse_exists` | DIRECTION-BEARING — two sentences, chosen by the SENT call\'s `selfSide`: when this '
                'record was the `from` ("<this> blocks <other>"): "<other> already blocks this record — unlink that first if '
                'it is the other way round."; when this record was the `to` ("<other> blocks <this>"): "This record already '
                'blocks <other> — unlink that first if it is the other way round." (the Unlink is in the panel). |')
R2_REMOVED_ROW = ('| `cannot_link_removed: from` / `: to` | "Not linked — <other> was removed. It\'s on the Removed bench." (the '
                  'name is the OTHER\'s — the end that is not this record; for "<other> blocks <this>" the removed `from` IS '
                  'the other) |')


class DirectionRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="brief-check-r2-"))
        cls.repo = cls.tmp / "repo"
        bare = cls.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(cls.repo)], check=True)
        mig = cls.repo / "supabase" / "migrations"
        mig.mkdir(parents=True)
        # the sha comment goes LAST so the excerpt's line numbers stay full-file minus 520
        (mig / "0072_record_links.sql").write_text(
            MIG_0072_EXCERPT + "-- excerpt of RSDPM 1d3b89333a6a14f5629acf50c5b65562797bdd9d:"
            "supabase/migrations/0072_record_links.sql lines 520-575\n")
        _git(cls.repo, "add", "-A")
        _git(cls.repo, "commit", "-q", "-m", "init")
        _git(cls.repo, "remote", "add", "origin", str(bare))
        _git(cls.repo, "push", "-q", "-u", "origin", "main")
        cls.r = bc.Repo(cls.repo, "origin/main")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def r2(self, text, repo="default", **kw):
        return rows_of("direction-bearing-refusal", bc.check_text(text, repo=self.r if repo == "default" else repo, **kw))

    def test_each_of_the_six_raises_is_classified_by_its_own_statement_group(self):
        ml = MIG_0072_EXCERPT.split("\n")
        expected = {524: ("record_not_found", False), 530: ("cannot_link_%: from", True), 533: ("cannot_link_%: to", True),
                    551: ("link_exists", False), 560: ("link_reverse_exists", True),
                    567: ("no_roster_person_for_caller", False)}
        for full_line, (lit, want) in expected.items():
            i = full_line - 520
            self.assertIn("'%s'" % lit, ml[i], full_line)
            self.assertEqual(bc.direction_bearing(ml, i), want, "%s at :%d" % (lit, full_line))

    def test_318_r1_F1_one_sentence_for_a_reversed_pair_is_refused(self):
        rows = self.r2(R2_TABLE_HEAD + R2_RED_ROW + "\n")
        f = [r for r in rows if r.status == "FAIL"]
        self.assertEqual(len(f), 1, [(r.status, r.note) for r in rows])
        self.assertEqual(f[0].line, 9)
        self.assertIn("has two truths", f[0].note)
        self.assertIn("link_reverse_exists", f[0].note)

    def test_318_r1_F1_two_quoted_sentences_pass(self):
        rows = self.r2(R2_TABLE_HEAD + R2_GREEN_ROW + "\n")
        self.assertEqual([r.status for r in rows], ["ok"], [r.note for r in rows])

    def test_both_ends_said_in_words_passes(self):
        row = '| `link_reverse_exists` | "These two are already linked the other way round." — one sentence, true at both ends |'
        self.assertEqual([r.status for r in self.r2(R2_TABLE_HEAD + row + "\n")], ["ok"])

    def test_a_split_cell_is_flagged_ONCE_per_row(self):
        rows = self.r2(R2_TABLE_HEAD + R2_REMOVED_ROW + "\n")
        self.assertEqual(len(rows), 1, [r.note for r in rows])
        self.assertEqual(rows[0].status, "ok")  # the original row carries two quoted sentences
        bare = '| `cannot_link_removed: from` / `: to` | "Not linked — <other> was removed. It\'s on the Removed bench." |'
        rows = self.r2(R2_TABLE_HEAD + bare + "\n")
        self.assertEqual([r.status for r in rows], ["FAIL"])

    def test_rows_that_are_states_or_straight_refusals_are_not_direction_bearing(self):
        rows = self.r2(R2_TABLE_HEAD + '| `link_exists` | "Already linked." |\n| `record_not_found` (link) | "Not linked — one of '
                       'these records is no longer here." |\n| PGRST202 on `link_records` | `LINK_FAILED_LINE` |\n'
                       '| `success (link)` | the sheet closes |\n')
        self.assertEqual([r.status for r in rows], ["ok"], [r.note for r in rows])
        self.assertIn("none direction-bearing", rows[0].note)

    def test_the_fallback_keeps_only_the_longest_fixed_prefix(self):
        mig = ("CREATE FUNCTION f() AS $$\nBEGIN\n  RAISE EXCEPTION 'cannot_link_kind: %', v_kind;\n\n"
               "  RAISE EXCEPTION 'cannot_link_%: from', v_s;\nEND $$;\n")
        sites = bc.raise_sites({"0099_x.sql": mig})
        self.assertEqual([s.literal for s in bc.match_raises("cannot_link_kind", sites)], ["cannot_link_kind: %"])
        self.assertEqual([s.literal for s in bc.match_raises("cannot_link_removed: from", sites)], ["cannot_link_%: from"])
        self.assertEqual(bc.match_raises(": to", sites), [])
        for skipped in ("cannot_link_draft|rejected: …", "cannot_link_kind: <kind>"):
            self.assertFalse(bc.r2_span_ok(skipped), skipped)

    def test_without_a_repo_or_the_migration_it_is_one_n_a_row(self):
        rows = self.r2(R2_TABLE_HEAD + R2_RED_ROW + "\n", repo=None)
        self.assertEqual([r.status for r in rows], ["n/a"])
        rows = self.r2(R2_TABLE_HEAD.replace("0072_record_links", "0073_not_here") + R2_RED_ROW + "\n")
        self.assertEqual([r.status for r in rows], ["n/a"])

    def test_decisions_mode_has_no_predicate_table_so_no_R2(self):
        self.assertEqual(self.r2(R2_TABLE_HEAD + R2_RED_ROW + "\n", decisions_only=True), [])


# --------------------------------------------------------------------------- #
# R3 mirror-names-the-newest-sibling — the ORIGINAL S2 sentence (#318 r2-F1)
# --------------------------------------------------------------------------- #
R3_RED = ("refusal → the classified sentence in `verb-error`, the Link button dead until the sheet is reopened "
          "(RemoveVerb's gate shape at `VerbControls.tsx:2057`'s `canRemove`, cite the current line)")
CAN_REMOVE = 'const canRemove = blockers.phase === "ready" && !busy && error === null;'
# brief-J2-step2.md:80-103 as it reads after the r2 ruling (ol-work runner/overnight-2026-10-05-wave7), verbatim
R3_S2_CORRECTED = (
    '**S2 — The Link sheet has THREE steps and the record you opened it from is THIS record.**\n'
    'Step 1 "Link to a…": the KIND chooser — one row per `LINKABLE_KINDS` entry in the human noun (`KIND_NOUN` in\n'
    '`lib/records/merge-refusal.ts:134-146` — `mission` reads "business area"; EXPORT a `kindNoun(kind, n)` beside\n'
    "`mergeKindPlural` rather than a second table), the record's own kind included (task → task is the ruling's own example).\n"
    'Step 2 "Which one?": the SAME `MergeCandidatePicker` (`components/records/MergeCandidatePicker.tsx`), fed by\n'
    '`mergeCandidatesAction(kind, selfId, query)` (`app/actions/merge.ts:146`) WIDENED from `MergeableKind` to `LinkableKind`:\n'
    'three more arms in its per-kind table — `missions` (`id, name`; no hint), `decisions` and `waiting_ons` (`id, name,\n'
    "project_id` → the project's name as the hint, the task arm's shape at `:196-203`). The widening is a type and three\n"
    'arms; the name, the cap (`MERGE_LIST_CAP`, `merge-refusal.ts:402`), the LIKE escaping and `neq("id", selfId)` are\n'
    "untouched (`selfId` on another kind's table excludes nothing and that is correct — the picker never offers\n"
    "`cannot_link_self` only when kind = this record's kind; say it in a comment). `MergeCandidatesState`/`MergeCandidateOption`\n"
    "are reused as they are. Picker third states are the picker's own (loading / failed / empty / searching / cap — the\n"
    'testids already exist and `/lab` already renders them).\n'
    'Step 3 "How are they linked?": THREE radio rows, in words, with the two names in them —\n'
    '"**<other>** blocks **<this>** — this record waits on it" (sends `from = other, to = this, \'blocks\'`),\n'
    '"**<this>** blocks **<other>**" (sends `from = this, to = other, \'blocks\'`),\n'
    '"**Related** — the same piece of work seen from two places" (sends `from = this, to = other, \'related\'`; the DB stores\n'
    'it canonically). **"Related" is PRE-SELECTED** (Houston drafts, humans adjust — a required choice never arrives blank;\n'
    'Related is the choice with no direction to get wrong). Then the **Link** button (`variant="primary"`, NOT danger: a link\n'
    'is undone by Unlink, class **W**) → `linkRecords(...)` with telemetry `{surface, via: \'manual\'}`; busy → "Linking…",\n'
    "Cancel dead; success → the sheet closes, `router.refresh()` (the page re-reads; in the drawer the provider's `withReread`\n"
    '(`components/ui/RecordDrawerProvider.tsx:82`) re-runs the peek — no navigation, this record IS the record); refusal →\n'
    'the classified sentence in `verb-error` — computed from the call that was SENT (the choice and names at tap time), never from the live radio, which stays changeable; the Link button is NEVER disabled by a refusal — only while busy — and ANY input change (relation, pick, kind) clears the refusal; tapping Link again on the same inputs re-sends and the DB refuses by name again (MergeVerb\'s rule — the newest door and the right sibling; RemoveVerb is destructive and its refusals terminal, the wrong sibling: RULED 2026-10-06 01:05 after #318 r2-F1, which found "try again" on screen with the only control disabled), `router.refresh()`. "Back" from step 2 returns to the\n'
    "kind chooser and re-reads the un-queried list (MergeVerb's repick rule, `:2363-2367`)."
)

# The REAL layout of app/detail/components/VerbControls.tsx, cut down: RemoveVerb is born first (RSDPM
# 2026-10-02T21:24:44Z), its helper RemoveSheetBody holds the REAL canRemove line; MergeVerb and MergeSheetBody are
# born in a later commit (2026-10-03T03:09:03Z), `router.refresh()` inside MergeVerb (real :2608) and — as in the real
# file at :2268 — inside RemoveVerb too; `disabled={busy}` only in MergeSheetBody (real :2801).
VERB_FILE = "app/detail/components/VerbControls.tsx"
VERB_V1 = ('"use client";\nimport { useRouter } from "next/navigation";\n\n'
           "function RemoveVerb({ record }: { record: Rec }) {\n  const router = useRouter();\n"
           "  async function onRemove() {\n    setBusy(true);\n    router.refresh();\n  }\n"
           "  return <RemoveSheetBody record={record} onRemove={onRemove} />;\n}\n\n"
           "export function RemoveSheetBody({ blockers, busy, error }: Props) {\n  " + CAN_REMOVE + "\n"
           "  return <Button disabled={!canRemove}>Remove</Button>;\n}\n")
VERB_V2 = VERB_V1 + ("\nfunction MergeVerb({ record }: { record: Rec }) {\n  const router = useRouter();\n"
                     "  async function onMerge() {\n    router.refresh();\n  }\n"
                     "  return <MergeSheetBody record={record} onMerge={onMerge} />;\n}\n\n"
                     "export function MergeSheetBody({ busy }: Props) {\n"
                     "  return <Button data-testid=\"merge-confirm\" disabled={busy}>Merge</Button>;\n}\n")


class MirrorRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="brief-check-r3-"))
        cls.repo = cls.tmp / "repo"
        bare = cls.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(cls.repo)], check=True)
        f = cls.repo / VERB_FILE
        f.parent.mkdir(parents=True)
        f.write_text(VERB_V1)
        d1 = {"GIT_AUTHOR_DATE": "2026-10-02T21:24:44Z", "GIT_COMMITTER_DATE": "2026-10-02T21:24:44Z"}
        _git(cls.repo, "add", VERB_FILE)
        _git(cls.repo, "commit", "-q", "-m", "RemoveVerb", env=d1)
        f.write_text(VERB_V2)
        d2 = {"GIT_AUTHOR_DATE": "2026-10-03T03:09:03Z", "GIT_COMMITTER_DATE": "2026-10-03T03:09:03Z"}
        _git(cls.repo, "add", VERB_FILE)
        _git(cls.repo, "commit", "-q", "-m", "MergeVerb", env=d2)
        _git(cls.repo, "remote", "add", "origin", str(bare))
        _git(cls.repo, "push", "-q", "-u", "origin", "main")
        cls.r = bc.Repo(cls.repo, "origin/main")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def r3(self, text, repo="default", **kw):
        return rows_of("mirror-names-the-newest-sibling",
                       bc.check_text(text, repo=self.r if repo == "default" else repo, db_functions=set(), **kw))

    def test_a_symbol_resolves_through_the_REAL_git_grep(self):
        # Apple Git 2.39's `grep -E` has no \b: a pattern using it exits 1 for EVERY symbol and R3 would never fire
        hits = self.r.find_definitions("RemoveVerb")
        self.assertEqual([(h[0], h[1]) for h in hits], [(VERB_FILE, 4)])
        self.assertEqual(self.r.find_definitions("Remove"), [])  # a prefix is not the symbol
        self.assertEqual([h[1] for h in self.r.find_definitions("RemoveSheetBody")], [13])

    def test_318_r2_F1_the_original_sentence_fails_BOTH_halves(self):
        rows = self.r3("## S2\n\n" + R3_RED + "\n")
        self.assertEqual([r.status for r in rows], ["FAIL", "FAIL"], [r.note for r in rows])
        quote, sib = rows
        self.assertIn("quote the lines you are mirroring", quote.note)
        self.assertIn("RemoveVerb", quote.note)
        self.assertIn("MergeVerb (2026-10-03)", sib.note)
        self.assertIn("#318 r2-F1", sib.note)

    def test_a_newer_sibling_mentioned_ELSEWHERE_is_info_not_FAIL(self):
        rows = self.r3("## S2\n\n" + R3_RED + "\n\n## S5\n\nThe merge door is MergeVerb.\n")
        self.assertEqual([r.status for r in rows], ["FAIL", "info"], [r.note for r in rows])
        self.assertIn("MergeVerb is newer and mentioned at line 7", rows[1].note)
        self.assertIn("why RemoveVerb's shape beats it", rows[1].note)

    def test_a_quote_and_the_newer_sibling_named_with_why_passes(self):
        text = ("## S2\n\n" + R3_RED + " MergeVerb (newer) is not the sibling here because its refusals are per (pair, "
                "relation) and this door's are terminal.\n```\n" + CAN_REMOVE + "\n```\n")
        rows = self.r3(text)
        self.assertEqual([r.status for r in rows], ["ok", "ok"], [r.note for r in rows])

    def test_the_corrected_S2_paragraph_as_it_really_reads_passes(self):
        rows = self.r3("## S2\n\n" + R3_S2_CORRECTED + "\n")
        self.assertEqual(bc.failures(rows), [], [(r.status, r.note) for r in rows])
        merge = [r for r in rows if "MergeVerb" in r.note]
        self.assertEqual([r.status for r in merge], ["ok", "ok"], [r.note for r in rows])
        self.assertIn("router.refresh()", merge[0].note)

    def test_negative_control_mergeverbs_rule_quoting_its_region_passes(self):
        rows = self.r3("## S\n\nTake MergeVerb's rule: `disabled={busy}` on the confirm, never a refusal gate.\n")
        self.assertEqual([r.status for r in rows], ["ok", "ok"], [r.note for r in rows])

    def test_mirror_X_and_a_backticked_name_trigger_too(self):
        for t in ("Mirror RemoveVerb for the gate.", "Use `RemoveVerb`'s gate shape here."):
            self.assertEqual([r.status for r in self.r3("## S\n\n%s\n" % t)], ["FAIL", "FAIL"], t)

    def test_a_name_that_is_not_a_symbol_or_no_repo_is_n_a(self):
        self.assertEqual([r.status for r in self.r3("## S\n\nHouston's rule and the header's rule hold.\n")], ["n/a", "n/a"])
        self.assertEqual([r.status for r in self.r3("## S2\n\n" + R3_RED + "\n", repo=None)], ["n/a"])

    def test_KNOWN_WEAKNESS_a_common_span_satisfies_the_quote_half(self):
        # the ORIGINAL S2 line also held `router.refresh()`, which is inside RemoveVerb's real region (:2268): the
        # quote half cannot judge relevance — the pre-dispatch review's question 1 is the net. The sibling half holds.
        rows = self.r3("## S2\n\n" + R3_RED + ", `router.refresh()`.\n")
        self.assertEqual([r.status for r in rows], ["ok", "FAIL"], [r.note for r in rows])

    def test_a_fenced_mirror_is_never_scanned(self):
        self.assertEqual(self.r3("## S\n\n```\nRemoveVerb's gate shape\n```\n"), [])

    def test_decisions_mode_runs_R3(self):
        self.assertEqual([r.status for r in self.r3("## F1\n" + R3_RED + "\n", decisions_only=True)], ["FAIL", "FAIL"])


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
