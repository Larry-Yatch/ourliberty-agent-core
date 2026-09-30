# Fix brief — PR #283 (PR S), review round 2 → fix round 2 — a SUBTRACTION round

**You are a FRESH headless builder on account slot b.** Two builders came before you (build; fix round 1); both are gone. Your input is this brief, the PR (#283) and the repo. Report to the PR as a COMMENT, never to a chat.

**Why this round is shaped the way it is.** Review round 1 found 10; fix round 1 closed all 10; review round 2 found 10 AGAIN, so the convergence rule fired and the manager decided the shape of this round instead of patching: fix the ONE primitive that produced four findings across two rounds (the reason sheet's open/sending/closed/error lifecycle), DELETE what has no caller or is a copy, and correct the contract-level defects once. **Every change in this round must remove or restructure; nothing new is added except the tests that pin the restructure.** The next review must come back with fewer than 10 findings and ZERO wrong-behaviour findings, or the PR is held.

## Where you work
- Worktree `~/dev/rsdpm-wt-S` (exists; branch `feat/lifecycle-substrate`; `npm ci` done; `.env.local` + `e2e/.auth/state.json` present). Your cwd IS that worktree. `git fetch origin && git status` first; HEAD is `b686578` = the PR head. Never touch `~/dev/RSDPM`.
- `feat/*`, NO label, NOT draft. Never `claude/*`, never `auto-review`.
- **Commit AND PUSH after every green step.**
- No permission prompts; the shared gates still fire — follow a deny's message; never `OL_EVIDENCE_WAIVE`.
- Proof scripts boot their own Postgres (postgresql@15 on PATH). Dev server for `npm run lab:shoot`: never port 3131/3132; kill it after.
- Staging is real: read only. 45-minute breaker per step. Write-tool 'read the file first' = read and retry.

## Read first
1. `gh pr view 283 --json body -q .body` and the LAST TWO comments on #283 (round-1 findings + decisions; fix round 1's report). Do not re-derive.
2. `~/dev/ol-work/rsdpm-lifecycle-contract.md` — §4 (AMENDED today: the emitter string; the classifier rule), §5, §7, and the newest-issuer table (AMENDED: the lane → 0063).
3. `~/.claude/code-discipline.md` items 5, 7, 9, 11, 12, 14, **20** ('a defect in one column is a defect in every column of its shape' — round 2 F3 is a repeat of round 1 F9 four lines apart).
4. `app/queue/components/OccurredAt.tsx` (F10) and `components/ui/StatusControl.tsx` in FULL before touching it (F1/F2 — the whole component, every state, every door).

## The ten findings (verbatim from `/code-review high` round 2; all ten CONFIRMED at source by the manager) and the DECISION on each

### r2-F1 — `components/ui/StatusControl.tsx:320` [correctness]
**Finding:** The reason sheet's Save is never disabled by the sent-pair guard. Once a write succeeds, the sheet closes only when the `state` prop changes. In the gap before the refresh lands (or forever, if the refresh is dropped), the sheet stays open with a live Save that `send()` silently swallows at the "belt". So the comment's claim that "a click never reaches here" is false for the Save door.
**Scenario:** A kind with a reason-required target (D′/E′/F′): the user types a reason and taps Save, and the write succeeds. `pending` goes false before router.refresh() lands, so the sheet shows its text and an enabled Save button. Tapping Save does nothing: no write, no message. On a phone that drops the refetch, the sheet sits open forever looking unsaved, although the move DID land. The user then cancels and re-picks the verb, and finds the option disabled with no explanation.
**DECISION:** FIX AT THE ROOT — the reason sheet gets ONE explicit state machine, no dependence on the refresh: `closed` → `open` (typing) → `sending` (Save/Enter/box/Cancel all dead, 'Saving…') → on RESOLVE WITHOUT THROW: `closed` (the sheet closes the moment the write returns; the row re-renders when the refresh lands) → on THROW: `open` with `error` INSIDE the sheet and the text kept. The sent-pair belt is never what closes or gates the sheet. Tests: one per transition, plus 'a dropped refresh after success leaves NO sheet and NO live Save' and 'a failed save keeps the text'. The round-1 'closes when the server state changes' rule is DELETED; the reset-on-prop-change block still clears `reasonFor` (F7 r1) for the stale-sheet case, and that test stays.

### r2-F2 — `components/ui/StatusControl.tsx:343` [correctness]
**Finding:** `pick()` opens the reason sheet without clearing `error`. A "Not saved." left by an earlier failed menu move therefore renders inside a brand-new reason sheet before anything has been sent from it.
**Scenario:** A menu move (say, Start) fails, so "Not saved." shows under the chip. The user opens the chip and picks a control whose target requires a reason. The fresh sheet opens with "Not saved." already at the top, above an empty box. It reads as if this reason had failed to save.
**DECISION:** FIX: opening the reason sheet (`pick()`) clears `error`; the error state is SCOPED — while a sheet is open, `error` belongs to the sheet; when no sheet is open, under the chip. One test: a failed menu move, then pick a reason-required control → the fresh sheet shows no 'Not saved.'.

### r2-F3 — `ops/verify-lifecycle-substrate.sh:174` [correctness]
**Finding:** This repeats round-1 F9 on the sibling branch. `mutation_spec`'s unknown-mode arm echoes its message to STDOUT and exits 2. It runs inside `f="$(mutated_copy …)"`, so the message is captured into `$f` and never shown. The F9 fix moved only the Python anchor-not-found message to stderr.
**Scenario:** `SUBSTRATE_PROOF_MUTATE=plain-fk` (hyphen typo) or any stale mode name: the command substitution captures "unknown SUBSTRATE_PROOF_MUTATE=…", `set -e` exits 2, and the operator sees no explanation. That is exactly the symptom F9 was filed for.
**DECISION:** FIX: `mutation_spec`'s `*)` arm echoes to STDERR (`>&2`). Then grep the proof for EVERY `echo`/`print` that can run inside a `$(…)` and list them in the comment with 'stdout/stderr' — the shape, not the one arm (item 20). Paste the F9-style before/after for a mistyped mode name.

### r2-F4 — `supabase/migrations/0063_lifecycle_substrate.sql:293` [altitude]
**Finding:** S re-issues the whole 70-line section_actions_due_today body (DROP + CREATE, both overloads) to carry `status_changed_by_name`, which nothing renders. The header says the only reason is so the §11-8 positive control "has a definer reader to test". That adds one more re-issued body that later migrations must re-derive from at merge time. It is the hazard class the re-issue checker and the 'a re-issued body disarms BASE modes' memory exist for, and here it is taken on for a test's convenience rather than a product reader.
**Scenario:** D′/F′ (or any lane change before F′ renders the stamp) must now start from 0063's copy, not 0054's. A PR branched before S merges and edits 0054's lane silently drops the stamp column, or reverts S's join, and fresh-db asserts only the newest issuer. The §11-8 control could be proven with an RLS read plus a scratch SECURITY DEFINER probe in the proof script, with no production body re-issued.
**DECISION:** KEEP — no code. The re-issue is not only for the proof: F′ renders the stamp on My Day and needs this column, so S carries it once. The contract's issuer table now says 0063 and the build order's F′ row says 're-issued from 0063'. State this in the PR body's Scope (one sentence) and DELETE the sentence that says the only reason is the proof.

### r2-F5 — `.github/workflows/test.yml:256` [stale-claim]
**Finding:** The new CI step's comment is stale after fix round 1. It says the BASE run is RED at "38 rows" and names "eight mutation modes". The script now has nine modes (`plain_fk` was added) and four more BASE-red rows (F5–F8).
**Scenario:** A reviewer or future builder checks the BASE run against "38 rows" and reads a mismatch as a regression, or trusts that 8 modes cover the proof. This is the 'prose goes stale within minutes' class: a claim sitting beside code that no longer does what it says.
**DECISION:** FIX: DELETE the numbers from the CI step comment (no '38 rows', no 'eight modes') — describe the shape ('RED with 0063 withheld, GREEN after; every committed mutation mode caught by a named row'). Do not correct a number that will drift again.

### r2-F6 — `lib/lifecycle/kinds.ts:216` [simplification]
**Finding:** `classifyStatusRefusal` (a 13-variant union plus ~15 regexes) has no product caller in S. It classifies RAISE tokens that no migration emits yet, and on the task tokens it is a second copy of lib/tasks/status.ts's classifier, kept in agreement only by a test.
**Scenario:** About 95 lines of speculative code ship and must be kept in step with a contract the D′/E′/F′ migrations have not written. The two task classifiers can drift, for example when a new task malformed token is added to one file only. Delete it and let the first PR that raises these tokens add the classifier next to its caller.
**DECISION:** DELETE: `classifyStatusRefusal`, the `LifecycleRefusal` union, `RECORD_STATUSES`/`isRecordStatus` if nothing else reads them, the regex constants, and their tests in `lib/lifecycle/__tests__/kinds.test.ts`. The task classifier stays where it is (`lib/tasks/status.ts`) with its callers. Contract §4 now says the first PR that RAISES a token adds its classifier next to its caller. Re-run `void_check` — its SPEC rows for the deleted rules are removed, and say so.

### r2-F7 — `components/ui/UndatedGroup.tsx:89` [correctness]
**Finding:** `Math.max(1, Math.floor(cap))` does not clamp NaN: `Math.max(1, NaN)` is NaN. So `slice(0, NaN)` renders no undated rows and `hidden > 0` is false. The prop doc's "Clamped to ≥ 1" is untrue for that input.
**Scenario:** A caller passes a computed cap that comes out NaN (e.g. `Math.floor(height / rowHeight)` with rowHeight 0/undefined). The fold summary says "No date agreed · 12", but opening it shows an empty list with no "and N more" line. The undated rows are silently dropped, which is the one thing the component promises never happens.
**DECISION:** FIX: `const shown = Number.isFinite(cap) ? Math.max(1, Math.floor(cap)) : 1;` — one test with `cap={NaN}` renders the rows. Fix the prop doc to match.

### r2-F8 — `supabase/migrations/0063_lifecycle_substrate.sql:358` [data-flow]
**Finding:** The lane's new `status_changed_by_name` is described as "carried, not rendered". But Houston's `get_my_day` tool passes `section_actions_due_today` rows to the model unprojected (lib/houston/tools.ts `asRows(actions)`), and the briefing worker reads the same lane. So the stamp's name reaches model context and the briefing's row dicts as soon as a transition writes it.
**Scenario:** After 0064 writes the first stamp, Houston's context carries "status_changed_by_name": "<teammate>" on every due-today row. The model may start saying "Chris parked this" before F′ has designed how the stamp is worded or checked that surface. No test pins Houston's projection of this lane.
**DECISION:** NO CODE — intended. Houston answers FACTS (contract §7) and §5 says every reader renders the same name; a due-today row carrying who last moved it is a fact Houston may state. Add ONE sentence to the PR body's 'Two clocks' or Scope naming that `get_my_day` and the briefing carry the column from the first stamp on, so the flow is deliberate. No test.

### r2-F9 — `supabase/migrations/0063_lifecycle_substrate.sql:265` [simplification]
**Finding:** The re-issued `queue.action` emitter string lists `park`, `complete` and `restore` twice each (the old run plus the new verbs appended). It is now pinned byte-exact in five places: the migration, the proof's T6, 99_assertions, verify-staging-applied and the mutation anchor.
**Scenario:** Any reader that enumerates the queue verbs by splitting the emitter string double-counts three verbs. The contract says no later PR may re-issue the taxonomy, so the duplicates are frozen in for good, and five byte-exact pins make fixing them expensive.
**DECISION:** FIX: the `queue.action` emitter string in 0063 becomes 0058's string + `_abandon_redate_receive_drop_redecide_defer_activate_block_archive_unarchive_remove` (park, complete, restore are already in it). Re-issue all five pins (migration, the proof's T6 + the mutation anchor `wrong_emitter_queue`, `99_assertions.sql`, `verify-staging-applied.sql`, the contract test). Contract §4 is amended already — cite it.

### r2-F10 — `components/ui/StatusControl.tsx:168` [reuse]
**Finding:** `StampDate` re-implements app/queue/components/OccurredAt.tsx line for line: the same no-op `subscribe`, the same `useSyncExternalStore` with an empty server snapshot, the same `<time dateTime>`. Only the formatter differs.
**Scenario:** Two copies of the hydration-safe client-only-time idiom exist, so a fix to one (e.g. the #418 hydration lesson, or a prefix/empty-text rule) will not reach the other. Give OccurredAt a `format` prop (or move it to components/ui) and use it here.
**DECISION:** SUBTRACT the copy: ONE client-only-time component in `components/ui/` (`ClientOnlyTime`, or move `OccurredAt` there and give it a `format` prop — builder's call, state it) carrying the hydration note ONCE; `OccurredAt` becomes a thin wrapper with its current API (its tests unchanged); `StampDate` is deleted and the stamp renders through the shared one. The `useSyncExternalStore` + empty-snapshot idiom exists in exactly ONE file afterwards — paste the grep.

## Evidence the PR COMMENT must carry (paste, never describe)
1. Per finding: the reproduction at `b686578` (red output pinned) and the closing commit — or, for the two no-code decisions (F4, F8), the PR-body sentence added.
2. **The reason sheet's STATE TABLE**: every state × every door (Save, Enter, Cancel, backdrop, Escape, chip, menu option, prop `state` change, resolve-ok, resolve-throw) → next state, with the test that pins each cell. Every cell has a test or says why it is unreachable AND a test proves it unreachable.
3. `void_check.py` on the SPEC re-anchored: rules for the deleted classifier REMOVED (named), rules for the new state table ADDED; table pasted; filed via `discipline_inbox`.
4. Item 20 for F3: the grep over the proof for every stdout write inside `$(…)`, with stdout/stderr per hit.
5. Item 4 over this round's delta; items 5 and 7 per changed function (garbage in; what the human sees on throw — and this time: what the human sees after SUCCESS before the refresh, and if the refresh never lands).
6. `migration_reissue.py --repo-root . --fetch` (0063 changed again), `operator_artifacts.py --repo-root . --base origin/main`, `claim_drift.py --repo-root .` pasted; the `useSyncExternalStore` grep (exactly one file).
7. Mutation modes: all nine still caught; `wrong_emitter_queue` re-anchored to the deduped string; the F3 mistyped-mode stderr line.
8. The runs: typecheck (`set -o pipefail`, exit printed), the WHOLE vitest suite, `verify:coverage-floor`, both CI jobs' step lists locally, `bash ops/verify-fresh-db.sh`, `npm run lab:shoot` (read the shots), `npm run verify:contrast:live`.
9. Item 11: EDIT THE PR BODY so every claim is true at the new head (the classifier bullet gone; the emitter string; the lane's reason; the Houston sentence; the sheet's rule; the void-check count; the file table; 'What I could not verify'). Re-read after `gh pr edit`. Grep the repo for survivors of every deleted claim.
10. 'What I could not verify'. Then ONE line: `COST: <what you can see>`.

## Reporting
The PR comment is the report. Then STOP. The manager runs review round 3.
