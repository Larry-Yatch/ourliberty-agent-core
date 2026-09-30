# Dispatch wrapper (from the manager, 2026-09-30) — read this, then the brief below it.

You are running headless under account slot b, dispatched by the manager session that is watching this run.
- Your working directory IS the worktree: `~/dev/rsdpm-wt-S`, branch `feat/lifecycle-substrate`, already created
  from `origin/main` (`9ed2896`), with `npm ci` done, `.env.local` and `e2e/.auth/state.json` copied in.
  Do NOT create a worktree and do NOT touch `~/dev/RSDPM`. Skip the brief's "create a worktree" step only.
- You have no permission prompts. Three shared gates still fire on Bash. One of them DENIES `gh pr create`
  until the discipline checks for this branch have been run and FILED; its deny message names the exact
  commands (`discipline_inbox.py file …`). Follow them and retry. Never use `OL_EVIDENCE_WAIVE`.
- The proof scripts boot their own throwaway Postgres (postgresql@15 is on PATH). No Docker, no `supabase start`.
- The dev server, if you need it for `npm run lab:shoot`, must NOT use port 3131 or 3132 (both reserved). Kill it when done.
- Staging is real. You may READ it (lab shots, typecheck against types). Never write to staging and never delete a row.
- End your final message with ONE line: `COST: <the usage summary you can see, or "unknown">`. The manager records
  the real figure from the JSON envelope.

---

# Builder brief — PR S: the lifecycle substrate (migration 0063)

**You are a fresh, headless builder session.** This brief is self-contained; you do not have the
manager's context and must not guess it. The manager watches every step of this PR and decides at
every stop. Report to the PR body, never to a chat.

## Read first, in this order (all in `~/dev/ol-work/` unless stated)
1. `rsdpm-lifecycle-contract.md` — **v4, FROZEN.** §2 (the columns), §4 (the taxonomy paragraph and the
   refusal-name list), §5 (the stamp renders as a `people` join), §7 (the app side). You build the
   parts marked S. You do NOT build any transition, any per-kind rulebook, Remove, or any lane change.
2. `rsdpm-phased-plan-2026-09-22.md` — ONLY the `## BUILD ORDER v2` section at the end: the S row, the
   wave rule, and "What v2 does NOT change".
3. In the repo: `supabase/migrations/0054_task_status_lifecycle.sql` (the header style and proof style
   every migration copies), `0053_notes_on_work_records.sql` (the trigger-loop you re-run),
   `0058_task_threads.sql` (the NEWEST `event_taxonomy_v1` body — derive from THIS one),
   `components/ui/RowStatusControl.tsx` (the control you generalise — its header's three subtractions
   are binding), `lib/tasks/status.ts` (the vocabulary shape), `app/my-day/components/MyDayView.tsx`
   and `app/my-day/data.ts` (where `UndatedGroup` will be mounted by D′/E′ — you mount nothing).
4. `~/.claude/code-discipline.md` items 2, 3, 4, 8, 9, 10, 12 — the evidence this PR must carry.

## Where you work
- Repo `~/dev/RSDPM`. `git fetch origin` first; branch from `origin/main` into a NEW worktree
  `~/dev/rsdpm-wt-S` on branch **`feat/lifecycle-substrate`**. Never work in `~/dev/RSDPM` itself
  (shared checkout) and never in another worktree.
- **Open the PR on that `feat/*` branch with NO label.** Never `claude/*`, never `auto-review`, never
  draft. That is the hold: it cannot route until the manager clears it.
- Commit and push after EVERY green step (a builder that dies mid-way loses nothing; the next agent
  starts from the PR, never from your context).
- If the Write tool refuses with "read the file first", that is the tool's read-first rule — read the
  file and retry. It is NOT another agent editing your worktree. Never stop for it.
- Wall clock: if any single step passes 45 minutes, stop, commit what is green, and write the state
  into the PR body.

## Scope — exactly this, nothing more

### A. Migration `supabase/migrations/0063_lifecycle_substrate.sql`
1. On `tasks`, `decisions`, `waiting_ons`, `projects`: `ADD COLUMN IF NOT EXISTS status_changed_at
   timestamptz`, `status_changed_by uuid REFERENCES public.people(id)`, `status_reason text`, and a
   named `CHECK (status_reason IS NULL OR char_length(status_reason) <= 4000)` per table (DROP IF EXISTS
   + ADD, the 0054 idiom). No defaults, no backfill, no NOT NULL — contract §2 and §10.
2. Re-run 0053's `emit_record_event` trigger loop with `status_reason` APPENDED to each of the four
   tables' content CSVs (missions unchanged). Copy the loop body from 0053 verbatim and change only
   the four strings; say so in the header table.
3. Re-issue `event_taxonomy_v1()` from **0058's body** with these rows added and nothing else changed:
   `waiting_on.status_changed` (emitter `rsdpm_waiting_on_transition`), `decision.status_changed`
   (`rsdpm_decision_transition`), `project.status_changed` (`rsdpm_project_transition`),
   `record.removed` (`remove_record`); `record.restored`'s emitter becomes
   `restore_rejected_restore_removed`; the `queue.action` emitter string gains
   `_abandon_redate_receive_drop_redecide_defer_activate_park_block_complete_archive_unarchive_remove_restore`.
   These emitters do not exist yet — that is by design (contract §4, "re-issued ONCE"); check every
   taxonomy contract test (`tests/contracts/__tests__/*` grep `event_taxonomy`) still passes and say
   which ones read it.
4. Header in the repo's style: WHAT THIS IS (one paragraph citing the contract), the STARTED FROM WHICH
   BODY table (every re-issued function, its newest issuer, what changed), idempotency, no BEGIN/COMMIT,
   Standing Rule 1.
5. Update `lib/database.types.ts`, `supabase/verify/99_assertions.sql` (a block asserting the twelve
   columns, the four CHECKs, the taxonomy rows, and a `record.updated` payload that carries a
   `status_reason` fingerprint after an UPDATE of that column), `ops/verify-staging-applied.sql`,
   `ops/staging-contract-baseline.json` / `ops/verify-staging-contract.mts` as 0054's PR did — read
   that PR's footprint with `git show --stat d03f2bb`.
6. A proof script `ops/verify-lifecycle-substrate.sh` in `ops/verify-task-status.sh`'s shape, wired
   into BOTH CI jobs in `.github/workflows/test.yml`: RED with `SUBSTRATE_PROOF_BASE=1` (migration
   withheld), GREEN with it applied; rows: each column exists; each CHECK refuses 4001 chars and admits
   4000; the fingerprint row; the taxonomy rows by name; and the §11-8 positive control — an UPDATE
   stamping `status_changed_by` to a seeded person resolves to the same `full_name` through (a) an
   authenticated RLS read, (b) `section_actions_due_today(viewer)` as the viewer, (c) the same call
   under `service_role` (the briefing's context). Rows (b)/(c) need the section to RETURN the name —
   see B.4. Committed mutation modes `SUBSTRATE_PROOF_MUTATE=` (at least: drop one column; drop one
   CHECK; forget one CSV; forget one taxonomy row; wrong emitter string), each caught by its named row.

### B. App side
1. `lib/lifecycle/kinds.ts`: the kind-generic TYPES and a registry in `lib/tasks/status.ts`'s shape
   (`STATUSES`, `statusControls`, `CONTROL_TARGET`, `CONTROL_LABEL`, `statusLabel`, `statusWord`,
   `classifyStatusRefusal` widened to `<kind>_status_transition_not_allowed` and the §4 refusal names).
   **Only `task` is populated** (re-exported from `lib/tasks/status.ts`, which stays the source);
   `waiting_on` / `decision` / `project` entries are added by their own PRs. A contract test pins that
   the task entry IS `lib/tasks/status.ts`'s tables (same object identity or deep-equal).
2. `components/ui/StatusControl.tsx`: the kind-generic descendant of `RowStatusControl` — props
   `(kind, recordId, title, state, controls, actions)`; the same Sheet; the three subtractions from
   `RowStatusControl`'s header kept and RESTATED in this file's header; a second sheet for a REASON
   when the chosen control's target requires one (from the registry: `reasonRequired(kind, target)`),
   blank-after-trim refused, no minimum length; the party / outcome / revisit-date sheets are NOT built
   here (their PRs add them). `RowStatusControl` becomes a thin task-only wrapper over it — its one
   caller (`app/detail/components/DetailView.tsx`) and its tests keep passing unchanged. The stamp line
   "<State> · <date> · by <name>" renders from `status_changed_at` + a `status_changed_by_name` prop
   (NULL → "by a teammate"); nothing supplies it yet except the lab fixture.
3. `components/ui/UndatedGroup.tsx`: `(rows, dateOf, cap, renderRow, label = "No date agreed")` —
   dated rows first in the caller's order, then a folded group with a count. Not mounted anywhere in
   product code; a lab state in `app/lab/` shows it with 0, 3 and 40 undated rows.
4. `section_actions_due_today` (newest issuer **0054**) gains ONE output column
   `status_changed_by_name text` via `LEFT JOIN public.people sb ON sb.id = t.status_changed_by` —
   re-derived from 0054's body with `migration_reissue.py`, everything else byte-identical. This is
   the only lane S touches, and only so the §11-8 positive control has a definer reader to test.
   `app/my-day/data.ts` types gain the column; `MyDayView` does not render it yet (F′ does).
5. `lib/houston/thread-turn.ts`: render `r.status` through `statusWord(kind, token)` for the kinds
   the registry knows; unknown kinds fall back to today's raw string. One test.
6. `/lab`: one state per new component (the control in every task state with and without a stamp;
   the reason sheet; the undated group ×3). `npm run lab:shoot`; read the shots yourself; the manager
   reads them too.

### NOT in scope (do not touch): any `rsdpm_*_transition` or `set_*_status`; whitelists; RLS
policies; `rsdpm_purge_note_set`; `project_health`; any other `section_*`; `create-task` /
`create-project` (the shared create core moved to E′); `workers/`; `prompts/`; `eval/`.

## Evidence the PR body must carry (paste, never describe)
1. **Item 2 — whole path:** for every function re-issued, one line: what it receives, what its callers
   do with the result. `migration_reissue.py --repo-root . --fetch` output pasted.
2. **Item 3 — void check:** a `SPEC.json` pairing every rule this PR adds (each CHECK, each taxonomy
   row, the fingerprint, the name join, the reason-sheet refusal, the fold cap) with the ONE test
   responsible for it, and `void_check.py`'s table pasted.
3. **Item 4 — branches:** the `git diff origin/main...HEAD | grep -n '^+' | grep -E 'continue|return|
   filter|if \(!|if not |\? \('` output with one line per hit saying which test takes it; the DOOR TABLE
   for the reason sheet (submit, Enter, Cancel, backdrop, Escape × the predicate); the empty-state
   question answered for `UndatedGroup` (zero rows; all rows undated).
4. **Item 8 — the human:** `operator_artifacts.py --repo-root . --base origin/main` pasted; every
   user-facing string the PR adds, in a table, and where it renders.
5. **Item 10 — mutations:** the `SUBSTRATE_PROOF_MUTATE` modes and the row that catches each.
6. **Item 12:** `claim_drift.py --repo-root .` pasted.
7. `npm run typecheck` with `set -o pipefail` and the exit code printed (never `| tail`); the WHOLE
   vitest suite (a red in the in-flight/transition family may be the known flake — re-run once, say so);
   `verify:coverage-floor`; both CI jobs' step lists from `test.yml` run locally.
8. The lab shots attached.
9. A **"What I could not verify"** section. An empty one is a claim; write the sentence.

## Reporting
The PR body is the report: Scope · What changed (by file) · Evidence (the eight items above, pasted) ·
What I could not verify · Cost (the `claude -p` JSON usage line). Then stop. The manager runs
`/code-review high`; findings come back as a PR comment; a FRESH builder fixes them (not you).
