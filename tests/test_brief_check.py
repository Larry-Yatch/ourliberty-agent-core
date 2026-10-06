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
import os
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
# R1 carried-no-reader — the ORIGINAL brief-J2-step2.md S3 paragraph (#318 r2-F2)
# --------------------------------------------------------------------------- #
# brief-J2-step2.md:105-124 as it read BEFORE the r2 ruling (ol-work runner/brief-gate-v2/brief-J2-step2-ORIGINAL.md),
# the WHOLE S3 paragraph, verbatim: the trigger sentence sits at :122, nine lines below the "renders, inside `Panel`"
# at :113 that a paragraph-wide reader scope accepted (round 0's corpus measurement).
R1_RED = (
    '**S3 — The links block is READ ON THE SERVER and rendered on BOTH surfaces as ONE client panel with Unlink.**\n'
    '`app/detail/links.ts` (`import "server-only"`, `verb-inputs.ts`\'s shape): `fetchRecordLinks(kind, id): Promise<RecordLinksState>`\n'
    '= `supabase.rpc("record_links_for", { record_type, id })` through the session client → `{ ok: true, rows: RecordLinkRow[] }`\n'
    "or `{ ok: false }` on ANY error (PGRST202 included). It is called in `DetailPage`'s existing `Promise.all`\n"
    '(`app/detail/DetailPage.tsx:107-108`) and in `peekRecordAction` (`app/actions/peek.ts:39-51`), and the plain value is\n'
    'handed down: `DetailView` (`app/detail/components/DetailView.tsx:250`) mounts `<RecordLinksPanel record links actions />`\n'
    'AFTER `detail-backlinks` (`:498-545`) and BEFORE `detail-earlier-decisions`; `RecordDrawerBody` (`components/ui/\n'
    'RecordDrawer.tsx:156`) mounts the same panel after `drawer-fields`. `RecordLinksPanel` (`components/records/\n'
    'RecordLinksPanel.tsx`, `"use client"`, every prop REQUIRED and undefaulted) renders, inside `Panel` (`components/ui/\n'
    'Panel.tsx:33`, label "Links", count = rows), three groups in this order: "Blocked by" (direction `in`), "Blocks" (`out`),\n'
    '"Related" — each line a `RecordLink` to the other record (`components/ui/RecordLink.tsx`; on the page it PEEKS like a\n'
    "backlink row, in the drawer it is under `RecordLinksNavigate` and navigates — the drawer's own rule, `RecordDrawer.tsx:\n"
    '96-98`) followed by an **Unlink** button (`aria-label="Unlink — <other name>"`) → `unlinkRecords(linkId, fromType, toType, telemetry)`;\n'
    "busy per line; success → `router.refresh()` / the provider's re-read; refusal → ONE quiet `Notice` line under the group\n"
    '(the classified sentence) and the line stays until the re-read drops it. **Empty state: the panel renders NOTHING** (a\n'
    'link is information, not a prompt — no "Add a link" nudge; the door is in the verb row). **Third state (`ok: false`):\n'
    'the panel renders with ONE line `LINKS_UNAVAILABLE_LINE` = "Couldn\'t load links" and no Unlink** (what the reader LOSES:\n'
    "the lines; a failed EXTRA read is a third state with one honest line, never a silent empty block). `created_by_name` / `created_at` are NOT rendered in step 2 (no reader asked; the row keeps them — one line in the panel's header comment).\n"
    'Census: the new `links` prop on both boundary edges is DATA (a call result) → `m26` `PINNED_UNKNOWNS` rows in the\n'
    '`DetailView → VerbControls` shape (`tests/contracts/__tests__/m26-rsc-boundary-census.contract.test.ts:231-241`).'
)
R1_GREEN = ("`created_by` / `created_by_name` / `created_at` are NOT carried into the app row at all — a field with no "
            "reader is not carried.")
R1_GREEN_READER = "… `created_at` is not rendered in the drawer; `RecordLinksPanel.tsx:140` renders it as the \"linked on\" line."


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

    def test_the_corrected_row_carries_nothing_and_raises_no_row(self):
        rows = self.check("## S3\n\n" + R1_GREEN + "\n")
        self.assertEqual(fails(rows, "carried-no-reader"), [])
        self.assertEqual(rows_of("carried-no-reader", rows), [])  # nothing is kept, so nothing is asked

    def test_a_named_reader_passes(self):
        rows = self.check("## S3\n\n" + R1_GREEN_READER + "\n")
        self.assertEqual([r.status for r in rows_of("carried-no-reader", rows)], ["ok"])

    def test_a_reader_in_the_NEXT_sentence_passes_even_across_a_paragraph_break(self):
        rows = self.check("## S3\n\n`created_at` is not rendered in the drawer.\n\n`RecordLinksPanel` renders it as the linked-on line.\n")
        self.assertEqual([r.status for r in rows_of("carried-no-reader", rows)], ["ok"])

    def test_a_negated_reader_verb_is_not_a_reader(self):
        for s in ("`x` is not shown; nothing reads it.", "`x` is not shown; `Panel.tsx:40` never renders it."):
            self.assertEqual(len(fails(self.check("## S\n\n%s\n" % s), "carried-no-reader")), 1, s)

    def test_the_carried_field_is_not_its_own_reader(self):
        f = fails(self.check("## S\n\n`created_at` is not displayed; `created_at` renders nothing new.\n"), "carried-no-reader")
        self.assertEqual(len(f), 1)

    def test_a_reader_verb_more_than_six_words_away_does_not_count(self):
        s = "`x` is not rendered; `Panel` is the component on the page that eventually, much later, renders things."
        self.assertEqual(len(fails(self.check("## S\n\n%s\n" % s), "carried-no-reader")), 1)

    def test_the_trigger_and_the_field_in_different_sentences_still_trigger(self):
        f = fails(self.check("## S\n\nThe app row gains `created_at`. It is not rendered.\n"), "carried-no-reader")
        self.assertEqual(len(f), 1)
        self.assertIn("`created_at`", f[0].note)

    def test_a_fenced_keep_is_never_scanned(self):
        self.assertEqual(rows_of("carried-no-reader", self.check("## S\n\n```\n`x` is not rendered\n```\n")), [])

    def test_keep_carries_and_kept_no_longer_trigger(self):
        # round 0's corpus: 176 refusals in 55 files, nearly all code DESCRIBED ("`x` carries the id") or an
        # instruction ("KEEP it", brief-J2-step2.md:57) — no field crossing a boundary unread
        for s in ("the newer text is #315's — KEEP it and add yours beside it: `fetchVerbInputs` / `needsRoster`.",
                  "`RecordLinkRow` carries the link id; the row is kept and retained.",
                  "| `link_reverse_exists` | two sentences, chosen by the SENT call's `selfSide` carried in `failed` |"):
            self.assertEqual(rows_of("carried-no-reader", self.check("## S\n\n%s\n" % s)), [], s)

    def test_a_quoted_trigger_is_cited_not_asserted(self):
        # round 1 F2: decisions-318-r2.md:16 DELETEs the panel header's quoted "CARRIED and not rendered" sentence;
        # brief-F-step2.md:89/:157 quote "NOT RENDERED YET — F′" to rewrite it. A quoted phrase is blanked first.
        rows = self.check('## F2\n\n`createdBy` leaves the row. DELETE the header\'s "CARRIED and not rendered" sentence.\n')
        self.assertEqual(rows_of("carried-no-reader", rows), [], [(r.status, r.note) for r in rows])

    def test_a_reader_two_sentences_away_does_not_count(self):
        # the reader scope is the trigger's sentence and the next one — never the paragraph (round 0: the whole S3
        # paragraph passed on "renders, inside `Panel`" nine lines above the trigger)
        s = "`Panel` renders the links. The row has an id. `created_at` is not rendered. The row has a kind."
        self.assertEqual(len(fails(self.check("## S\n\n%s\n" % s), "carried-no-reader")), 1)


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
# brief-J2-step2.md:80-103 as it read BEFORE the r2 ruling (ol-work runner/brief-gate-v2/brief-J2-step2-ORIGINAL.md),
# verbatim: RemoveVerb mirrored at :102 beside `router.refresh()` (16 chars), MergeVerb cited at :103 with no why
# within 12 words (the paragraph's only contrast word is the "NOT danger" at :98).
R3_S2_ORIGINAL = (
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
    'the classified sentence in `verb-error`, the Link button dead until the sheet is reopened (RemoveVerb\'s gate shape at `VerbControls.tsx:2057`\'s `canRemove`, cite the current line), `router.refresh()`. "Back" from step 2 returns to the\n'
    "kind chooser and re-reads the un-queried list (MergeVerb's repick rule, `:2363-2367`)."
)
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
# file at :2268 — inside RemoveVerb too; `disabled={busy}` only in MergeSheetBody (real :2801). SYNTHETIC, for the
# quote half's "at most twice in the file" clause: `aria-disabled={busy}` (20 chars) THREE times on origin/main — once
# in RemoveSheetBody (RemoveVerb's region), twice in MergeSheetBody — and once only on origin/once. origin/three adds
# LinkVerb (2026-10-06T13:55:00Z), the real newest door. A test file defines `const drawer` (the real one is
# components/ui/__tests__/record-drawer.test.tsx:149) — never a mirror target.
VERB_FILE = "app/detail/components/VerbControls.tsx"
VERB_V1 = ('"use client";\nimport { useRouter } from "next/navigation";\n\n'
           "function RemoveVerb({ record }: { record: Rec }) {\n  const router = useRouter();\n"
           "  async function onRemove() {\n    setBusy(true);\n    router.refresh();\n  }\n"
           "  return <RemoveSheetBody record={record} onRemove={onRemove} />;\n}\n\n"
           "export function RemoveSheetBody({ blockers, busy, error }: Props) {\n  " + CAN_REMOVE + "\n"
           "  return <Button disabled={!canRemove} aria-disabled={busy}>Remove</Button>;\n}\n")
# round 2 F1: a top-level definition that does NOT carry RemoveVerb's stem sits between RemoveVerb's helpers and
# MergeVerb (on RSDPM 3f267b1 `StatusVerbs` sat inside OwnerVerb's same-suffix region, :1041-1836) — X's region ends
# there. Appended after RemoveSheetBody, so no VERB_V1 line moves (`RemoveSheetBody` stays at 13).
FOREIGN_LINE = 'return <Chip tone="neutral" onPick={pickStatus}>Status</Chip>;'
VERB_V2 = VERB_V1 + ("\nfunction StatusChips({ busy }: Props) {\n  " + FOREIGN_LINE + "\n}\n"
                     "\nfunction MergeVerb({ record }: { record: Rec }) {\n  const router = useRouter();\n"
                     "  async function onMerge() {\n    router.refresh();\n  }\n"
                     "  return <MergeSheetBody record={record} onMerge={onMerge} />;\n}\n\n"
                     "export function MergeSheetBody({ busy }: Props) {\n"
                     "  const cancel = <Button aria-disabled={busy}>Cancel</Button>;\n"
                     "  return <Button data-testid=\"merge-confirm\" disabled={busy} aria-disabled={busy}>Merge</Button>;\n}\n")
VERB_ONCE = VERB_V2.replace("<Button aria-disabled={busy}>Cancel", "<Button>Cancel").replace(
    "disabled={busy} aria-disabled={busy}>Merge", "disabled={busy}>Merge")
VERB_THREE = VERB_V2 + ("\nfunction LinkVerb({ record }: { record: Rec }) {\n"
                        "  return <LinkSheetBody record={record} />;\n}\n")
# round 2 F1, the NAME BOUNDARY: `move` sits inside `remove`, so a substring stem would run MoveVerb's region through
# RemoveVerb (brief-G-step2.md:69 quotes Remove's real button for "MoveVerb's shape"). Its own branch, origin/moveremove,
# so no shared-fixture line moves.
MOVE_LINE = "return <MoveSheetBody record={record} onMove={onMove} />;"
REMOVE_BUTTON = 'return <Button variant="danger" data-testid="detail-remove">Remove</Button>;'
VERB_MOVE_REMOVE = ('"use client";\n\nfunction MoveVerb({ record }: { record: Rec }) {\n  ' + MOVE_LINE + "\n}\n\n"
                    "function RemoveVerb({ record }: { record: Rec }) {\n  " + REMOVE_BUTTON + "\n}\n")
DRAWER_TEST = "components/ui/__tests__/record-drawer.test.tsx"
# round 1 F1: a non-test file defining the English word `task` (the real one is app/houston/fixtures.ts:37 on RSDPM
# 1d3b893 — brief-E.md:337's "add-a-task's shape" resolved to it and FAILed the quote half)
FIXTURES_FILE = "app/houston/fixtures.ts"
# round 2 F2: a SECOND non-test file defining the same `const task` (on RSDPM 3f267b1 `Panel` is defined in 7 non-test
# files) — a paragraph naming neither file is ambiguous and FAILs; naming one resolves to it
TASK_STATES = "app/lab/TaskStates.tsx"
# round 3 F2 (review r2-F2): `export default function` was not a definition to the region bound, so a symbol defined
# before a default-exported page ran to end of file (RSDPM app/layout.tsx: fetchQueueCount :52, RootLayout :65 — the
# region read 52-107). The default export does NOT carry QueueCount's stem `Queue` (a `QueuePage` would stay inside by
# the stem rule). Its own branch, origin/defaultpage, so no shared-fixture line moves.
PAGE_FILE = "app/queue/page.tsx"
QUEUE_LINE = "return countRows(await listQueue());"
LAYOUT_LINE = "const zone = await viewerTimezone();"
PAGE_SRC = ("function QueueCount() {\n  " + QUEUE_LINE + "\n}\n\n"
            "export default async function RootLayout() {\n  " + LAYOUT_LINE + "\n  return zone;\n}\n")


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
        t = cls.repo / DRAWER_TEST
        t.parent.mkdir(parents=True)
        t.write_text("const drawer = render(<RecordDrawer />);\n")
        fx = cls.repo / FIXTURES_FILE
        fx.parent.mkdir(parents=True)
        fx.write_text("const task = { ... }\n")
        ts = cls.repo / TASK_STATES
        ts.parent.mkdir(parents=True)
        ts.write_text("export function Chips() {}\n\nconst task = { title: \"Send the deck\" };\n")
        d1 = {"GIT_AUTHOR_DATE": "2026-10-02T21:24:44Z", "GIT_COMMITTER_DATE": "2026-10-02T21:24:44Z"}
        _git(cls.repo, "add", VERB_FILE, DRAWER_TEST, FIXTURES_FILE, TASK_STATES)
        _git(cls.repo, "commit", "-q", "-m", "RemoveVerb", env=d1)
        f.write_text(VERB_V2)
        d2 = {"GIT_AUTHOR_DATE": "2026-10-03T03:09:03Z", "GIT_COMMITTER_DATE": "2026-10-03T03:09:03Z"}
        _git(cls.repo, "add", VERB_FILE)
        _git(cls.repo, "commit", "-q", "-m", "MergeVerb", env=d2)
        _git(cls.repo, "remote", "add", "origin", str(bare))
        _git(cls.repo, "push", "-q", "-u", "origin", "main")
        for branch, body, date in (("once", VERB_ONCE, "2026-10-04T00:00:00Z"), ("three", VERB_THREE, "2026-10-06T13:55:00Z"),
                                   ("moveremove", VERB_MOVE_REMOVE, "2026-10-05T00:00:00Z")):
            _git(cls.repo, "checkout", "-q", "-b", branch, "main")
            f.write_text(body)
            _git(cls.repo, "add", VERB_FILE)
            _git(cls.repo, "commit", "-q", "-m", branch, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
            _git(cls.repo, "push", "-q", "origin", branch)
        _git(cls.repo, "checkout", "-q", "-b", "defaultpage", "main")
        pg = cls.repo / PAGE_FILE
        pg.parent.mkdir(parents=True)
        pg.write_text(PAGE_SRC)
        _git(cls.repo, "add", PAGE_FILE)
        _git(cls.repo, "commit", "-q", "-m", "defaultpage",
             env={"GIT_AUTHOR_DATE": "2026-10-05T12:00:00Z", "GIT_COMMITTER_DATE": "2026-10-05T12:00:00Z"})
        _git(cls.repo, "push", "-q", "origin", "defaultpage")
        _git(cls.repo, "checkout", "-q", "main")
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
        # round 0 F2(c): a newer sibling named with its why is an info row (listed with its date), never a FAIL
        self.assertEqual([r.status for r in rows], ["ok", "info"], [r.note for r in rows])
        self.assertIn("MergeVerb (2026-10-03) is newer and named here with why", rows[1].note)

    def test_the_corrected_S2_paragraph_owes_a_fenced_quote_of_merges_lines(self):
        # KNOWN CONSEQUENCE (round 0 F2): "MergeVerb's rule" with only `router.refresh()` (16 chars) and
        # `variant="primary"` (17) beside it quotes nothing; ONE row per symbol per paragraph (:102 and :103 both name it)
        rows = self.r3("## S2\n\n" + R3_S2_CORRECTED + "\n")
        merge = [r for r in rows if "MergeVerb" in r.note]
        self.assertEqual([r.status for r in merge], ["FAIL", "ok"], [r.note for r in rows])
        self.assertIn("quote the lines you are mirroring", merge[0].note)
        fence = "\n```\n  return <Button data-testid=\"merge-confirm\" disabled={busy} aria-disabled={busy}>Merge</Button>;\n```\n"
        rows = self.r3("## S2\n\n" + R3_S2_CORRECTED + fence)
        self.assertEqual([r.status for r in rows if "MergeVerb" in r.note], ["ok", "ok"], [r.note for r in rows])

    def test_negative_control_mergeverbs_rule_quoting_its_region_passes(self):
        rows = self.r3("## S\n\nTake MergeVerb's rule: `data-testid=\"merge-confirm\" disabled={busy}` on the confirm, never a refusal gate.\n")
        self.assertEqual([r.status for r in rows], ["ok", "ok"], [r.note for r in rows])

    def test_mirror_X_and_a_backticked_name_trigger_too(self):
        for t in ("Mirror RemoveVerb for the gate.", "Use `RemoveVerb`'s gate shape here."):
            self.assertEqual([r.status for r in self.r3("## S\n\n%s\n" % t)], ["FAIL", "FAIL"], t)

    def test_a_name_that_is_not_a_symbol_or_no_repo_is_n_a(self):
        self.assertEqual([r.status for r in self.r3("## S\n\n`Houston`'s rule and the `header`'s rule hold.\n")], ["n/a", "n/a"])
        # round 1 F1: unticked, one capital, no `_` — English, never resolved (KNOWN CONSEQUENCE: an unticked Panel too)
        self.assertEqual(self.r3("## S\n\nHouston's rule and the header's rule hold.\n"), [])
        self.assertEqual([r.status for r in self.r3("## S2\n\n" + R3_RED + "\n", repo=None)], ["n/a"])

    def test_a_common_short_span_no_longer_satisfies_the_quote_half(self):
        # the ORIGINAL S2 line also held `router.refresh()`, which is inside RemoveVerb's real region (:2268) and
        # occurs 14 times in the real file; it is 16 characters, under the 20 a quote now needs
        rows = self.r3("## S2\n\n" + R3_RED + ", `router.refresh()`.\n")
        self.assertEqual([r.status for r in rows], ["FAIL", "FAIL"], [r.note for r in rows])

    def test_a_span_that_occurs_more_than_twice_in_the_file_is_not_a_quote(self):
        # `aria-disabled={busy}`: 20 chars, has `=`, sits in RemoveVerb's region — and three times in the file
        text = "## S2\n\nRemoveVerb's gate shape: `aria-disabled={busy}` on the confirm.\n"
        self.assertEqual([r.status for r in self.r3(text)][0], "FAIL")
        once = bc.Repo(self.repo, "origin/once")
        self.assertEqual([r.status for r in self.r3(text, repo=once)][0], "ok")

    def test_a_fenced_common_short_line_no_longer_satisfies_the_quote_half(self):
        # round 3 F3 (review r2-F3): the fence path asked only 12 characters with code punctuation, so a fence of
        # `router.refresh();` (17 characters; in RemoveVerb's real region at :2268) passed where the same backticked
        # span FAILed (fixture-r3-F3.md:5 vs :12). Both paths ask one predicate now.
        rows = self.r3("## S2\n\n" + R3_RED + "\n```\nrouter.refresh();\n```\n")
        self.assertEqual([r.status for r in rows], ["FAIL", "FAIL"], [r.note for r in rows])
        self.assertIn("RemoveVerb is at %s:4-17 and this paragraph quotes none of it" % VERB_FILE, rows[0].note)

    def test_a_fenced_line_that_occurs_more_than_twice_in_the_file_is_not_a_quote(self):
        # round 3 F3: the fenced twin of the backtick test above — `aria-disabled={busy}` three times on origin/main
        text = "## S2\n\nRemoveVerb's gate shape:\n```\naria-disabled={busy}\n```\n"
        self.assertEqual([r.status for r in self.r3(text)][0], "FAIL", [r.note for r in self.r3(text)])
        once = bc.Repo(self.repo, "origin/once")
        self.assertEqual([r.status for r in self.r3(text, repo=once)][0], "ok", [r.note for r in self.r3(text, repo=once)])

    def test_a_name_that_resolves_only_into_a_test_file_is_n_a(self):
        # round 0: 12 of 23 refusals resolved into tests — "the drawer's own rule" → `const drawer` in a test file
        self.assertEqual(self.r.find_definitions("drawer")[0][0], DRAWER_TEST)
        # round 1 F1: the name is backticked so it still reaches resolution (an unticked "drawer" is dropped before it)
        text = ("## S3\n\nin the drawer it is under `RecordLinksNavigate` and navigates — the `drawer`'s own rule, "
                "`RecordDrawer.tsx:96-98`.\n")
        self.assertEqual([r.status for r in self.r3(text)], ["n/a"], [r.note for r in self.r3(text)])

    def test_an_english_word_is_not_resolved_as_a_mirror(self):
        # round 1 F1: brief-E.md:337 "add-a-task's shape" resolved `task` to a non-test `const` on RSDPM main and
        # FAILed; a capture is resolved only when backticked, holding 2+ capitals, or holding `_`
        self.assertEqual(self.r.find_definitions("task")[0][0], FIXTURES_FILE)
        rows = self.r3("## S\n\nbuild it in add-a-task's shape (`app/houston/fixtures.ts`)\n")
        self.assertEqual(rows, [], [(r.status, r.note) for r in rows])

    def test_a_backticked_lowercase_name_is_still_resolved(self):
        rows = self.r3("## S\n\nbuild it in `task`'s shape (`app/houston/fixtures.ts`)\n")
        self.assertEqual([r.status for r in rows], ["FAIL", "ok"], [r.note for r in rows])
        self.assertIn("task is at %s:1-2 and this paragraph quotes none of it" % FIXTURES_FILE, rows[0].note)
        self.assertIn("task is the newest of its siblings (0)", rows[1].note)

    def test_a_name_defined_in_two_non_test_files_fails_until_the_paragraph_names_one(self):
        # round 2 F2 (review r1-F5): the first `git grep` hit was taken silently; an unresolved mirror is never a pass
        rows = self.r3("## S\n\nbuild it in `task`'s shape.\n")
        self.assertEqual([r.status for r in rows], ["FAIL"], [r.note for r in rows])
        self.assertIn("task is defined in 2 files on origin/main (%s, %s) — name the file you mean" % (FIXTURES_FILE, TASK_STATES),
                      rows[0].note)
        rows = self.r3("## S\n\nbuild it in `task`'s shape (`%s`).\n" % TASK_STATES)
        self.assertEqual(rows[0].status, "FAIL", [r.note for r in rows])
        self.assertIn("task is at %s:3-4 and this paragraph quotes none of it" % TASK_STATES, rows[0].note)

    def test_a_file_name_inside_a_longer_file_name_does_not_name_that_file(self):
        # round 3 F1 (review r2-F1): the file name was a bare SUBSTRING of the paragraph, so `AddTaskPanel.tsx` named
        # `Panel.tsx` and resolved RSDPM's 7-way `Panel` ambiguity silently (fixture-r3-F1.md:5); `BigTaskStates.tsx`
        # holds `TaskStates.tsx` the same way. A file name names a file only at a name boundary.
        rows = self.r3("## S\n\nbuild it in `task`'s shape (`app/lab/BigTaskStates.tsx`).\n")
        self.assertEqual([r.status for r in rows], ["FAIL"], [r.note for r in rows])
        self.assertIn("task is defined in 2 files on origin/main (%s, %s) — name the file you mean" % (FIXTURES_FILE, TASK_STATES),
                      rows[0].note)
        # positive control: the full path, and the bare file name at a boundary, both name it
        for named in ("`%s`" % TASK_STATES, "`TaskStates.tsx`", "in TaskStates.tsx"):
            rows = self.r3("## S\n\nbuild it in `task`'s shape (%s).\n" % named)
            self.assertEqual(rows[0].status, "FAIL", (named, [r.note for r in rows]))
            self.assertIn("task is at %s:3-4 and this paragraph quotes none of it" % TASK_STATES, rows[0].note, named)

    def test_a_paragraph_naming_two_of_the_files_fails_until_one_full_path_wins(self):
        # round 3 F1: two named files is the same ambiguity — the first named hit was taken silently
        rows = self.r3("## S\n\nbuild it in `task`'s shape (fixtures.ts, or TaskStates.tsx).\n")
        self.assertEqual([r.status for r in rows], ["FAIL"], [r.note for r in rows])
        self.assertIn("task is defined in 2 files on origin/main; this paragraph names 2 of them (%s, %s) — name only the "
                      "one you mean, by its full path" % (FIXTURES_FILE, TASK_STATES), rows[0].note)
        rows = self.r3("## S\n\nbuild it in `task`'s shape (`%s`, not fixtures.ts).\n" % TASK_STATES)
        self.assertEqual(rows[0].status, "FAIL", [r.note for r in rows])
        self.assertIn("task is at %s:3-4 and this paragraph quotes none of it" % TASK_STATES, rows[0].note)

    def test_fixture_fake_stub_and_python_test_paths_are_test_paths(self):
        # round 2 F2(b): names resolved into tests/contracts/__fixtures__/ and workers/tests/*.py on RSDPM main;
        # tests/contracts/lib/ holds real definitions five corpus briefs cite and must stay resolvable
        for path in ("tests/contracts/__fixtures__/x/Chip.tsx", "tests/fakes/x.ts", "tests/stubs/x.ts", "workers/tests/t.py"):
            self.assertTrue(bc.TEST_PATH_RE.search(path), path)
        for path in ("tests/contracts/lib/rsc-census.ts", "app/detail/components/VerbControls.tsx"):
            self.assertFalse(bc.TEST_PATH_RE.search(path), path)

    def test_318_r2_F1_the_ORIGINAL_S2_paragraph_fails_three_times_on_the_real_sibling_order(self):
        # origin/three: RemoveVerb < MergeVerb < LinkVerb. RemoveVerb: no quote, and its newer sibling MergeVerb is cited
        # at :103 with no why within 12 words (the paragraph's "NOT danger" is far away); LinkVerb, named elsewhere,
        # is info. MergeVerb ("repick rule"): no quote; LinkVerb info.
        three = bc.Repo(self.repo, "origin/three")
        rows = [r for r in self.r3("## S2\n\n" + R3_S2_ORIGINAL + "\n\n## Build\n\n3. `LinkVerb` in VerbControls.tsx.\n",
                                   repo=three) if r.status != "n/a"]
        self.assertEqual([r.status for r in rows], ["FAIL", "FAIL", "info", "FAIL", "info"], [r.note for r in rows])
        remove_q, remove_sib, remove_info, merge_q, merge_info = rows
        self.assertIn("RemoveVerb is at", remove_q.note)
        self.assertIn("MergeVerb (2026-10-03) is named here with no why", remove_sib.note)
        self.assertIn("LinkVerb is newer and mentioned at line 30", remove_info.note)
        self.assertIn("MergeVerb is at", merge_q.note)
        self.assertIn("LinkVerb is newer and mentioned at line 30", merge_info.note)

    def test_a_foreign_definition_between_a_door_and_its_sibling_ends_the_region(self):
        # round 2 F1 (review r1-F6): `StatusChips` does not carry RemoveVerb's stem `Remove`, so RemoveVerb's region ends
        # at :17 — a quote of StatusChips' line is a quote of a different door, never RemoveVerb's lines
        text = "## S\n\nUse `RemoveVerb`'s gate shape here:\n```\n  " + FOREIGN_LINE + "\n```\n"
        rows = self.r3(text)
        self.assertEqual(rows[0].status, "FAIL", [r.note for r in rows])
        self.assertIn("RemoveVerb is at %s:4-17 and this paragraph quotes none of it" % VERB_FILE, rows[0].note)
        # positive control: RemoveVerb's own helper carries the stem and stays inside
        text = "## S\n\nUse `RemoveVerb`'s gate shape here:\n```\n  " + CAN_REMOVE + "\n```\n"
        self.assertEqual(self.r3(text)[0].status, "ok", [r.note for r in self.r3(text)])

    def test_a_default_exported_definition_ends_the_region(self):
        # round 3 F2 (review r2-F2): `RootLayout` is `export default async function` and does not carry `Queue`, so
        # QueueCount's region ends on the line before it — a quote of RootLayout's line is not QueueCount's shape
        dp = bc.Repo(self.repo, "origin/defaultpage")
        rows = self.r3("## S\n\nBuild it in `QueueCount`'s shape:\n```\n  " + LAYOUT_LINE + "\n```\n", repo=dp)
        self.assertEqual([r.status for r in rows], ["FAIL", "ok"], [r.note for r in rows])
        self.assertIn("QueueCount is at %s:1-4 and this paragraph quotes none of it" % PAGE_FILE, rows[0].note)
        # positive control: QueueCount's own line is in its region; RootLayout has no `Count` suffix, so no sibling
        rows = self.r3("## S\n\nBuild it in `QueueCount`'s shape:\n```\n  " + QUEUE_LINE + "\n```\n", repo=dp)
        self.assertEqual([r.status for r in rows], ["ok", "ok"], [r.note for r in rows])
        self.assertIn("from %s:1-4" % PAGE_FILE, rows[0].note)
        self.assertIn("QueueCount is the newest of its siblings (0)", rows[1].note)

    def test_the_stem_is_carried_only_at_a_name_boundary(self):
        # round 2 F1: `RemoveVerb` holds "move" but does not CARRY `Move` (the `m` after `Re` is lowercase), so
        # MoveVerb's region ends before it and a quote of Remove's button is not Move's shape
        mr = bc.Repo(self.repo, "origin/moveremove")
        rows = self.r3("## S\n\nBuild the door in `MoveVerb`'s shape:\n```\n  " + REMOVE_BUTTON + "\n```\n", repo=mr)
        self.assertEqual(rows[0].status, "FAIL", [r.note for r in rows])
        self.assertIn("MoveVerb is at %s:3-6" % VERB_FILE, rows[0].note)
        rows = self.r3("## S\n\nBuild the door in `MoveVerb`'s shape:\n```\n  " + MOVE_LINE + "\n```\n", repo=mr)
        self.assertEqual(rows[0].status, "ok", [r.note for r in rows])
        for name, stem, want in (("fetchMergeProposal", "Merge", True), ("sameOwner", "Owner", True),
                                 ("REMOVE_LINE", "Remove", True), ("removeBlockersLine", "Remove", True),
                                 ("RemoveVerb", "Move", False), ("StatusVerbs", "Owner", False),
                                 ("KIND_LABEL", "ProjectStatus", False), ("narrowCounts", "mergeRecords", False)):
            self.assertEqual(bc.carries_stem(name, stem), want, (name, stem))

    def test_a_quoted_mirror_is_cited_not_asserted(self):
        # round 1 F2: a decisions file quotes the sentence it corrects; the quoted mirror is not this document's mirror
        rows = self.r3('## F1\n\nthe old brief said "mirror RemoveVerb\'s gate" and was wrong\n', decisions_only=True)
        self.assertEqual(rows, [], [(r.status, r.note) for r in rows])

    def test_a_fenced_mirror_is_never_scanned(self):
        self.assertEqual(self.r3("## S\n\n```\nRemoveVerb's gate shape\n```\n"), [])

    def test_decisions_mode_runs_R3(self):
        self.assertEqual([r.status for r in self.r3("## F1\n" + R3_RED + "\n", decisions_only=True)], ["FAIL", "FAIL"])


# --------------------------------------------------------------------------- #
# R4 instruction-points-at-a-live-control — two ORIGINAL brief-J2-step2.md paragraphs (#318 r2-F1)
# --------------------------------------------------------------------------- #
R4_DEAD = R3_RED
R4_SENTENCES = '`LINK_FAILED_LINE` = "Not linked — try again.", `UNLINK_FAILED_LINE` = "Not unlinked — try again."'
R4_FIXED = ("the Link button is NEVER disabled by a refusal — only while busy — and ANY input change (relation, pick, kind) "
            "clears the refusal")


class DeadUntilRules(unittest.TestCase):
    def r4(self, text, **kw):
        return rows_of("instruction-points-at-a-live-control", bc.check_text(text, repo=None, db_functions=set(), **kw))

    def test_318_r2_F1_try_again_over_a_dead_until_control_is_refused(self):
        rows = self.r4("## S2\n\n" + R4_DEAD + "\n\n## S4\n\n" + R4_SENTENCES + "\n")
        f = [r for r in rows if r.status == "FAIL"]
        self.assertEqual(len(f), 1, [(r.status, r.note) for r in rows])
        self.assertEqual(f[0].line, 3)
        self.assertIn("an instruction the reader cannot obey is wrong copy", f[0].note)
        self.assertIn("line 3", f[0].note)
        self.assertIn("line 7", f[0].note)

    def test_the_corrected_sentence_passes(self):
        rows = self.r4("## S2\n\n" + R4_FIXED + "\n\n## S4\n\n" + R4_SENTENCES + "\n")
        self.assertEqual([r for r in rows if r.status == "FAIL"], [])

    def test_a_quoted_instruction_whose_paragraph_names_no_control_is_info(self):
        rows = self.r4("## S4\n\n" + R4_SENTENCES + "\n")
        self.assertEqual([r.status for r in rows], ["info", "info"], [r.note for r in rows])
        rows = self.r4('## S4\n\n"Not linked — try again." shows under the Link button.\n')
        self.assertEqual([r.status for r in rows], ["ok"], [r.note for r in rows])

    def test_stays_disabled_is_a_dead_until_phrase_and_a_fence_is_not_scanned(self):
        rows = self.r4('## S\n\nThe confirm stays disabled.\n\n"Pick another record and try again."\n')
        self.assertEqual([r.status for r in rows if r.status == "FAIL"], ["FAIL"])
        rows = self.r4('## S\n\n```\nThe confirm stays disabled.\n```\n\n"Pick another record and try again."\n')
        self.assertEqual([r.status for r in rows if r.status == "FAIL"], [])

    def test_no_quoted_instruction_means_no_row(self):
        self.assertEqual(self.r4("## S2\n\n" + R4_DEAD + "\n"), [])

    def test_decisions_mode_has_no_R4(self):
        # round 0 F3: a decisions file NARRATES the defect it removes — decisions-318-r2.md:6 quotes the reviewer's
        # "button stays disabled", :12 says DELETE every "Link dead until …" claim — so R4 is briefs only
        rows = self.r4("## F1\n" + R4_DEAD + "\n\n" + R4_SENTENCES + "\n", decisions_only=True)
        self.assertEqual(rows, [], [(r.status, r.note) for r in rows])

    def test_a_quoted_or_backticked_dead_until_phrase_is_cited_not_asserted(self):
        for s in ('the old caption read "Link dead until the sheet is reopened"',
                  "the old caption read \u201cLink dead until the sheet is reopened\u201d",
                  "grep for `stays disabled` and `dead until` in the diff"):
            rows = self.r4('## S\n\n%s\n\n"Not linked — try again." shows under the Link button.\n' % s)
            self.assertEqual([r for r in rows if r.status == "FAIL"], [], s)

    def test_backtick_spans_pair_left_to_right_so_a_dead_until_between_two_spans_still_fails(self):
        # round 1 F2: the shared blanking keeps CITED_SPAN_RE's own left-to-right scan and returns a whitespace-free
        # backtick span unchanged. A pattern REQUIRING whitespace inside the backticks would pair the closing backtick
        # of `link_exists` with the opening one of `verb-error`, then blank "Link dead until …" — losing one of the
        # two TRUE corpus catches (brief-J2-step2.md:169, the line below verbatim)
        text = ('## Predicate table\n\n'
                '| `link_exists` | "Already linked." in `verb-error`, Link dead until the sheet is reopened, `router.refresh()` |\n'
                'the refusal line reads "Not linked — try again."\n')
        f = [r for r in self.r4(text) if r.status == "FAIL"]
        self.assertEqual([r.line for r in f], [3], [(r.status, r.line, r.note) for r in self.r4(text)])
        self.assertIn("'dead until' at line 3", f[0].note)

    def test_blank_cited_keeps_a_name_and_blanks_quoted_spans_at_the_same_length(self):
        # round 3 F6: the direct test of the shared blanking (R1, R3 and R4 read its output) — a whitespace-free
        # backtick span is a NAME and survives verbatim; a double-quoted span and a backtick span holding whitespace
        # are blanked with `#` of the same length, so the text keeps its length and every later line number
        text = 'the `created_by_name` field, "the row keeps them", and `a b` here'
        out = bc.blank_cited(text)
        self.assertEqual(len(out), len(text))
        self.assertIn("`created_by_name`", out)
        self.assertEqual(out, text.replace('"the row keeps them"', "#" * 20).replace("`a b`", "#" * 5))

    def test_a_blanked_quote_keeps_every_later_line_number(self):
        # the stripped span is BLANKED with same-length filler, never deleted: line_of counts characters into the
        # joined paragraph, so a deleted span before the phrase would report it a line early
        text = ('## S\n\nThe caption "Link dead until the sheet is reopened" was deleted, and\n'
                'the confirm stays disabled while busy.\n\n"Pick another record and try again."\n')
        f = [r for r in self.r4(text) if r.status == "FAIL"]
        self.assertEqual([r.line for r in f], [4], [(r.line, r.note) for r in f])
        self.assertIn("'stays disabled' at line 4", f[0].note)


# --------------------------------------------------------------------------- #
# brief-review — the pre-dispatch review file check_file demands beside every brief and decisions file
# --------------------------------------------------------------------------- #
VALID_REVIEW = ("# Pre-dispatch review — brief.md\n\n## 1. Mirrors\n- none in this brief\n\n## 2. Predicate rows\n- none\n\n"
                "## 3. Human sentences\n- none\n\n## 4. Carried fields\n- NONE\n\nBRIEF DEFECTS: 0\n")
T_BRIEF, T_REVIEW = 1_000_000, 1_000_100  # explicit mtimes: a checkout gives fixtures arbitrary ones


def write_reviewed(d, name="brief.md", brief="# Brief\n\nBuild the thing.\n", review=VALID_REVIEW,
                   brief_mtime=T_BRIEF, review_mtime=T_REVIEW):
    b = Path(d) / name
    b.write_text(brief)
    os.utime(str(b), (brief_mtime, brief_mtime))
    if review is not None:
        rp = bc.review_path(b)
        rp.write_text(review)
        os.utime(str(rp), (review_mtime, review_mtime))
    return b


class BriefReview(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = self._d.name

    def tearDown(self):
        self._d.cleanup()

    def review_rows(self, **kw):
        return rows_of("brief-review", bc.check_file(write_reviewed(self.d, **kw), None))

    def assertRefused(self, rows, *needles):
        f = [r for r in rows if r.status == "FAIL"]
        self.assertEqual(len(f), 1, [(r.status, r.note) for r in rows])
        for n in needles:
            self.assertIn(n, f[0].note)
        self.assertIn("run the review: paste scripts/brief_review_prompt.md to an opus subagent (read-only)", f[0].note)

    def test_the_review_file_is_named_after_the_brief_stem(self):
        self.assertEqual(bc.review_path(Path("/x/brief-J2-step2.md")), Path("/x/brief-review-brief-J2-step2.md"))
        self.assertEqual(bc.review_path(Path("/x/decisions-318-r2.md")), Path("/x/brief-review-decisions-318-r2.md"))

    def test_a_missing_review_is_refused(self):
        self.assertRefused(self.review_rows(review=None), "brief-review-brief.md")

    def test_a_review_OLDER_than_the_brief_is_stale(self):
        self.assertRefused(self.review_rows(review_mtime=T_BRIEF - 1), "stale — the brief changed after the review; re-run the review")

    def test_an_equal_mtime_passes(self):
        self.assertEqual([r.status for r in self.review_rows(review_mtime=T_BRIEF)], ["ok"])

    def test_a_missing_heading_is_refused(self):
        self.assertRefused(self.review_rows(review=VALID_REVIEW.replace("## 3. Human sentences\n- none\n\n", "")),
                           "## 3. Human sentences")

    def test_an_empty_heading_is_refused(self):
        self.assertRefused(self.review_rows(review=VALID_REVIEW.replace("## 4. Carried fields\n- NONE\n", "## 4. Carried fields\n")),
                           "## 4. Carried fields")

    def test_headings_match_case_insensitively(self):
        rows = self.review_rows(review=VALID_REVIEW.replace("## 1. Mirrors", "### 1. MIRRORS"))
        self.assertEqual([r.status for r in rows], ["ok"])

    def test_no_defects_line_is_refused(self):
        self.assertRefused(self.review_rows(review=VALID_REVIEW.replace("BRIEF DEFECTS: 0\n", "")), "BRIEF DEFECTS")

    def test_open_defects_without_a_LEDGERED_line_are_refused(self):
        rows = self.review_rows(review=VALID_REVIEW.replace("BRIEF DEFECTS: 0\n", "BRIEF DEFECTS: 2\n- S2 mirrors the wrong door\n"
                                                            "- the row has one sentence\n"))
        self.assertRefused(rows, "BRIEF DEFECTS: 2")

    def test_open_defects_with_NO_line_under_them_are_refused(self):
        # "one line per defect": a count with nothing listed under it is not a ledger (fail-closed)
        self.assertRefused(self.review_rows(review=VALID_REVIEW.replace("BRIEF DEFECTS: 0", "BRIEF DEFECTS: 2")), "BRIEF DEFECTS: 2")

    def test_zero_defects_passes_and_ledgered_defects_pass(self):
        self.assertEqual([r.status for r in self.review_rows()], ["ok"])
        led = VALID_REVIEW.replace("BRIEF DEFECTS: 0\n", "BRIEF DEFECTS: 1\nLEDGERED D91 — the cap sentence waits on the second desk\n")
        self.assertEqual([r.status for r in self.review_rows(review=led)], ["ok"])

    def test_the_LAST_defects_line_counts(self):
        rerun = VALID_REVIEW.replace("BRIEF DEFECTS: 0\n", "BRIEF DEFECTS: 3\n- a\n- b\n- c\n\nre-run after fixing:\nBRIEF DEFECTS: 0\n")
        self.assertEqual([r.status for r in self.review_rows(review=rerun)], ["ok"])

    def test_check_text_stays_text_only(self):
        self.assertEqual(rows_of("brief-review", bc.check_text("# Brief\n\nBuild the thing.\n")), [])

    def test_the_prompt_file_carries_every_heading_the_check_demands(self):
        prompt = (_SCRIPTS / "brief_review_prompt.md").read_text()
        for h in bc.REVIEW_HEADINGS:
            self.assertIn(h, prompt)
        self.assertIn("BRIEF DEFECTS: N", prompt)
        self.assertIn("LEDGERED D", prompt)


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bc.main(list(args))
        return code, buf.getvalue()

    def test_exit_codes_and_table(self):
        with tempfile.TemporaryDirectory() as d:
            good = write_reviewed(d, "good.md")       # each brief now needs its review file beside it
            bad = write_reviewed(d, "bad.md", brief=J_F1)
            code, out = self.run_cli(str(good))
            self.assertEqual(code, bc.EXIT_OK)
            self.assertIn("PASS: 0 FAIL row(s)", out)
            code, out = self.run_cli(str(bad))
            self.assertEqual(code, bc.EXIT_REFUSED)
            self.assertIn("| claim-quote |", out)
            self.assertIn("REFUSED: 1 FAIL row(s)", out)
            code, _ = self.run_cli(str(Path(d) / "missing.md"))
            self.assertEqual(code, bc.EXIT_USAGE)
            # the CLI demands the review file too (check_file, no flag)
            bc.review_path(good).unlink()
            code, out = self.run_cli(str(good))
            self.assertEqual(code, bc.EXIT_REFUSED)
            self.assertIn("| brief-review |", out)

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
