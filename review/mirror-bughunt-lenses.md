# Mirror bug-hunt lenses (Phase 1 per-PR gate)

> v1 (shipped 2026-06-08) — validated by backtest at 17/19 HIGH = 89% in real
> config (corpus injected + context), vs ~0% for unaided review. This is the
> reviewer prompt Mirror runs on a PR diff, vendored from the `/code-review`
> skill's fan-out shape but tuned for *this* codebase's escaped-bug taxonomy
> (the corpus in `known-bug-patterns.json`). It is **additive** to Mirror's
> existing spec/AC checklist and her `test_regression_check.py` gate — it does NOT
> replace them. The thresholds below are the shipped v1 defaults; the Phase-2
> Pulse loop calibrates them. The same prompt is reused by the backtest harness.

## Why this differs from the stock /code-review skill

The 64 bugs that escaped Mirror are 100% correctness / reliability / data-loss /
security — the exact classes the stock skill is tuned to *de-emphasize*. So three
of its defaults are reversed here:

1. **Not shallow / not diff-only.** The stock skill tells agents to "avoid reading
   extra context beyond the changes." Our seam, TOCTOU, and identifier-match bugs
   are invisible without reading call sites and the surrounding function. Lenses
   below explicitly read beyond the hunk where the lens calls for it.
2. **Security & reliability are first-class**, not excluded. The stock skill says
   to ignore "general security issues ... unless explicitly required in CLAUDE.md."
   Here they are primary lenses.
3. **Severity-weighted confidence thresholds**, not a flat >80. A
   security / concurrency / data-loss candidate surfaces at lower confidence than a
   style nit, because the cost of a false negative is asymmetric for a *gate*.

## The fan-out

Run these lenses as parallel sub-agents over the PR diff. Each lens agent is given:
the PR diff, permission to read surrounding context in the modified files, and the
entries from the corpus (read it by absolute path: `/home/larry/agent-core/review/known-bug-patterns.json`)
whose `review_lens` matches its lens.
Each returns a list of candidate findings: `{file, line_range, lens, severity,
description, why_real, suggested_fix}`.

Two are scope-gated rather than universal: **Lens I** runs only on diffs that add a
new part, and **Lens J** runs only on diffs that ship SQL a live database will run.
On a diff that does neither, both are skipped and say so in one line.

### Lens A — concurrency & atomicity
TOCTOU, lock scope, lost updates, non-atomic read-modify-write, crash-recovery
gaps, `os.open` without `O_EXCL` on predictable paths, check-then-act on shared
state. **Read the surrounding function and any lock/flock usage**, not just the
hunk. (Escaped examples: auth write-path TOCTOU, append-lock loss, beacon lock
lost-update.)

### Lens B — input & path safety
Path traversal (`..`, separators) in any caller-supplied component used to build a
filesystem path; missing sanitization on `task_id` / filenames / PR refs; injection
into shell / SQL / `gh` args. **Trace where the value originates** (is it
attacker-or-operator-influenced?). (Escaped example: `safe_write_inbox` task_id
traversal.)

### Lens C — identifier matching
Substring / `in` / `startswith` / unanchored comparison on identifiers (task_id,
branch, PR/issue `#<n>`, worktree stem) without boundary or length-floor checks;
matching a parenthetical `#5` mention as if it referenced PR #5. (Escaped examples:
substring-ID matching across healers, `gh_ref_resolved` false-positive.)

### Lens D — integration seam
Does the change honor the contracts of what it calls AND what calls it? Column
projections that are ignored, return-shape mismatches, an emitter and consumer that
disagree on a field, marker grammar drift. **Read the other side of the seam.**
(Escaped examples: chain-events column-projection mismatch, mirror-attribution join.)

### Lens E — automation honesty  *(highest-density area: healers)*
Does the code report success only when the effect actually happened? Healers /
auto-merge / reconcilers that log "fixed" / return success / emit a pass marker
without verifying the post-condition; fail-OPEN on a config typo (`enabled:
"false"` truthy string); unbounded re-dispatch / retry without a budget. (Escaped
examples: healer-reports-success-while-broken, fail-open kill switch,
unbounded-redispatch.)

### Lens F — state persistence & data-loss
Fail-open clobber on a corrupt/partial read (writing fresh state that discards other
rows); non-atomic writes that can truncate on crash/full-disk; retention/cleanup
that deletes more than intended. (Escaped examples: triage-state clobber,
alert-retention data-loss.)

### Lens G — control-flow correctness
Off-by-one, wrong early-return / drop path, inverted condition, mishandled tri-state
(`None` vs `False`), edge cases that compile and pass existing tests but are wrong.
(Escaped examples: advancer tier-3 title match, await_quiescence logic.)

### Lens H — CLAUDE.md adherence
The modified directories' CLAUDE.md guidance — but only rules that actually apply to
review (not authoring-time hints). Lowest priority; do not invent rules.

### Lens I — reuse / reinvention + catalog-on-build  *(ADVISORY — never blocks; Mission-A connect-on-build)*
Unlike A–H (escaped-bug lenses), this one is a forward-looking *reuse* nudge: does this diff build a
**new capability that already exists on the component shelf**, reimplementing a catalogued part
instead of reusing it? **Scope: only diffs that ADD a part** — a new module/file, a new substantial
top-level function/class, a new `pulse_check_*`/`heal_*`/endpoint/agent. Skip edits to an existing
part and trivial helpers. For such an addition:
1. Name, in plain words, the capability the diff adds (from the PR title/description + the new
   files/symbols).
2. **Query the shelf librarian** — run it by ABSOLUTE path (like the corpus), it lives in the
   sibling ourliberty-graph repo, not your worktree:
   `python3 /home/larry/ourliberty-graph/pipeline/librarian.py "<capability phrase>"`
   It surfaces the closest catalogued candidates — `<id> [profile] reuse=<mode>` + capability
   statement + `location:`. Retrieval ranks the candidates; it renders **no verdict**. You judge.
3. **Judge each candidate on substance** — the same judgment contract `build_check.py` now uses, so
   the build-time consult and this gate agree. For the top candidate(s), decide:
   - **REUSE** — the candidate describes the **same** capability the diff is building **and** the diff
     does not import/reference/extend that part. Surface a reuse note: name the shelf part, its
     `location` and `reuse_mode`, and what to reuse/extend instead of reinventing.
   - **ADAPT** — the candidate solves the same problem at a different altitude or shape (a near-variant
     — same job, different implementation, or a pattern that transfers but leaves work to do). Mention
     it only if the diff *clearly* reinvents that capability (worth a note because it seeds the
     portfolio layer); otherwise stay quiet.
   - **NONE** — no candidate is relevant. Don't surface a reuse note; go to step 4 (restock).
   Judge on substance, not shared vocabulary. Cross-altitude matches (e.g. a workflow card answering a
   schema need) are normal and frequently correct, and "none of these fit" is a legitimate, expected
   outcome — not a failure. No score threshold: retrieval ranks, the reader judges.
4. If NO candidate fits (you judged every candidate NONE / the capability is genuinely new),
   surface a **restock note** instead: name the new component and its file(s) and flag it to be
   catalogued after merge, so the next builder finds it rather than reinventing it. This is the
   *catalog-on-build* half of the loop — reuse what exists (steps 1–3), catalogue what's new (this
   step). Same advisory weight: narrative-only, never blocks. (A Forge PR that already carries a
   `## Restock` heading has self-declared this — just confirm it names the right new part.)

**Fail-safe — this lens can never block or fail the review.** If the librarian or the
ourliberty-graph checkout is absent or errors, write one line ("reuse-check skipped: librarian
unavailable") and move on. Retrieval ranks the candidates well (recall@3 is the tracked metric);
it does not judge — the reader judges each candidate REUSE/ADAPT/NONE on substance. Lens I stays **advisory only** by design
(reuse is a forward-looking judgment call, not a correctness gate): findings are sub-blocking
narrative notes (see Wiring), never `REVIEW_REVISION` / `REVIEW_ESCALATE` / `REVIEW_EMERGENCY_HALT`.
It surfaces a reuse opportunity; Forge/Larry decide.
(This is Mission A's connect-before-build behavior applied at the gate Mirror already runs —
PLAN §5-#3 / §9-Q4. Graduate it toward a real gate only once Pulse Check XI's accuracy meter earns
the trust.)

### Lens J — migration safety  *(scope: diffs that ship SQL a live database will run)*

**Fires when** the diff adds or edits a file under a migrations directory
(`supabase/migrations/*.sql` in RSDPM) — or otherwise ships DDL/DML destined for a
real database. Skip entirely otherwise; this lens has nothing to say about ordinary
code.

**Why it exists.** A migration is reviewed and merged like code, but merging it does
not change the database — historically a human pasted it into a SQL console
afterwards. In RSDPM, three times in two days (`0022`, `0029`, `0030`), the first
happened and the second did not, and each broke something live while every surface
reported green. The fix is to apply on merge. **That fix changes what a review
means: once it lands, merging a migration EXECUTES it.** The judgement that used to
sit (uselessly) in front of a paste step has to sit here instead, because here is
the last point where a human is asked anything.

It is worth saying plainly why "here" and not "at the paste step". From the repo
owner, verbatim: *"I don't have the technical expertise to know if I'm going to do
something that's going to break it anyway."* A gate whose operator cannot evaluate
what they approve is a delay with ceremony. So this lens does not ask him to
approve SQL. It hands him a decision he **can** make — about blast radius and
reversibility, in plain words.

#### The five questions. Grade every one PASS / FAIL / **UNKNOWN**.

**J1 — Reversible?** Is there a mechanical undo that restores structure *and* data?
`ADD COLUMN`, `CREATE INDEX`, a new table, `CREATE OR REPLACE FUNCTION` where the
prior body is in git → yes. `DROP COLUMN`, `DROP TABLE`, `TRUNCATE`, `DELETE`, and
`ALTER COLUMN ... TYPE` that narrows → **no**; the rows are gone and git does not
hold them.

**J2 — Structure or data?** Classify *every statement*, not the file. DATA-TOUCHING
includes the obvious (`INSERT`/`UPDATE`/`DELETE`/`TRUNCATE`) and the easily missed:
`DROP COLUMN`/`DROP TABLE` on a populated object, `ALTER COLUMN ... TYPE` (rewrites
existing rows), `SET NOT NULL` and new `CHECK`/`FOREIGN KEY` constraints (reject
existing rows, so the migration can simply fail mid-way on real data after passing
against an empty one). STRUCTURE-ONLY is a narrower category than it looks.

**J3 — How many rows?** For every data-touching statement, state a **number or a
bound**, and say how you got it. If you cannot count — and at review time you
usually cannot, because you are reading a diff and not connected to the database —
**say so, and treat the blast radius as unknown**. Unknown is a block. `"0 rows"`
is a claim that needs evidence; it is never the default.

**Look for a rehearsal comment before you conclude "unknown".** For RSDPM, a
droplet timer rehearses every open PR's migrations against the real database
inside a transaction that rolls back, and posts the measured counts as a PR
comment marked `<!-- rsdpm-migration-rehearsal -->`. When one is present, those
numbers ARE the answer to J3 — quote them and move on. They are measured, not
estimated, and they beat anything you could infer from the diff.

Three conditions on trusting it, and they matter more than the convenience:

1. **Check the author.** A PR comment is writable by anyone who can comment on
   the PR, so the marker alone proves nothing — a forged one would be a green
   light nobody checked. Accept it only from the repo-owner account that the
   droplet posts as (`Larry-Yatch`). Be honest in the verdict that this is weak
   authentication: it is the same account a human uses, and it distinguishes
   "our tooling or Larry" from "anyone else", not "a machine" from "a person".
2. **Check it matches the head you are reviewing.** A comment left against an
   earlier push describes migrations that may have changed since. If the diff
   has moved on, the numbers are stale — treat that as no comment at all.
3. **No comment is still UNKNOWN, and UNKNOWN is still FAIL.** The absence of a
   rehearsal is not evidence of safety. The timer may be off, the PR may be too
   new, the rehearsal may itself have failed. Do not read silence as "0 rows" —
   that inversion is the whole failure this lens exists to prevent.

A rehearsal that reports **WOULD FAIL** is a finding in its own right: the
migration errors against real data even though it may apply cleanly to an empty
one. Route it as `REVIEW_REVISION` — it is fixable, and better found here than
on the apply path at 2am.

**J4 — The rulebook.** Read the *target repo's* CLAUDE.md (Lens H's habit, applied
to schema). For RSDPM that is standing rules 2 and 3, and they are concrete:
every new table ships its RLS class in the same migration, deny-by-default;
`authenticated` gets no direct DML anywhere; every view is
`WITH (security_invoker = true)`; every function is `SECURITY DEFINER SET
search_path = ''`, owned by a dedicated non-superuser definer role, with `REVOKE
EXECUTE FROM PUBLIC, anon` and explicit `GRANT`s; module-owned DDL is a closed
list, so an object nobody's spec names is an amendment, not a drive-by.

**J5 — Order.** Does this migration redefine an object (function, view, policy,
trigger) that a **higher-numbered** migration in the same tree also redefines? If
so, applying it out of order silently reverts the later one. Check every `CREATE OR
REPLACE` / `ALTER` target against all higher-numbered files in the directory. This
exact check was done **by hand** for `0022` against `0027`; it must stop being a
hand check. Flag the mirror case too — a migration whose object a *lower*-numbered
**unapplied** migration also touches.

#### The verdict — written for a non-DBA, blast radius first

Emit this block in the review narrative for every migration file. It is the
deliverable of this lens; the graded J1–J5 are its working.

```
## Migration verdict — <filename>

**What it does to the database:** <one sentence, plain words, no SQL>
**Reversible:** yes / no — <why>
**Data at risk:** <N rows / N people / affects everyone who …>, or "none — structure only"
**How I know:** measured by the PR rehearsal / could not measure — see below
**Rulebook:** pass / fails <which rule>, at <file:line>
**Order:** safe / conflicts with <later migration>
**Recommendation:** apply / do not apply — <one clause>
**What I could not check:** <list, or "nothing">
**Independence:** <the standing caveat below>
```

How to write it, because the wording *is* the feature:

- **No SQL identifier is the subject of a sentence.** "the column that records
  whether a host wants the morning briefing", not `profiles.briefing_enabled`.
- **Name people and rows, not tables**, whenever data is at risk: *"this deletes a
  column holding data for 6 people and cannot be undone. I recommend no."*
- **Never write "approve this SQL?"** He has said he cannot evaluate it. A verdict
  that asks him to is a rubber stamp with extra steps.
- **State the limit of your own recommendation.** Standing caveat, include it every
  time: *"Written and reviewed by the same model — a careful second pass with
  adversarial framing, not an independent one."*
- **Say where the number came from.** "6 of 9 rows, measured against the real
  database" and "I could not measure this" are different claims and Larry should
  not have to guess which he is reading. A measured count is the strongest thing
  this lens can offer; an unmeasured one is a reason to stop, not a rounding.

#### Designing against the reviewer that always says yes

This lens's whole value is that it sometimes says no. Three guards:

1. **Grade against the written rules, item by item.** A Lens-J output without
   explicit PASS/FAIL/UNKNOWN on each of J1–J5 is not a run of this lens.
2. **UNKNOWN counts as FAIL.** Bias to block when unsure. The asymmetry is real —
   a wrongly-blocked migration costs a round trip; a wrongly-applied destructive one
   costs data.
3. **Track the approval rate.** Every verdict is one ledger line. **A migration lens
   that has never recommended "do not apply" is not reviewing** — it is the paste
   step again, wearing a report. (Meter and ledger land in the follow-up PR; until
   then, state the running count in the narrative.)

And the conflict, said out loud rather than papered over: **the same model writes
the migration and reviews it.** The partial mitigation is that this is a separate
pass with adversarial framing, run by a different agent with a different prompt —
genuinely how several real bugs were caught. It is a mitigation, not independence,
and the verdict says so in Larry's copy so he can price the recommendation.

#### Routing

- **J4 failure** (missing RLS, non-hardened function, view without
  `security_invoker`) → Forge can fix it inline → **`REVIEW_REVISION`**.
- **J5 overlap** → **`REVIEW_REVISION`** (reorder or fold the migrations), unless
  resolving it needs a spec decision → `REVIEW_ESCALATE`.
- **J1/J2/J3 — irreversible loss of existing data, or an UNKNOWN blast radius** →
  **`REVIEW_ESCALATE`**, carrying the verdict block verbatim. Not a revision:
  a deliberately destructive migration is not a bug Forge can fix, it is a question
  only a human can answer. Escalation is the path that asks him.
- **`REVIEW_EMERGENCY_HALT`** stays narrow: the migration targets a **production**
  database, or it deletes **client content** (transcripts, meeting notes, anything
  carrying real conversation) rather than factory-generated rows.

**A note on the halt rule above (step 4b, "a destructive/irreversible operation the
diff performs").** Once apply-on-merge exists, that rule starts reaching migrations,
because merging one performs it. Do not let it swallow this lens: halt stops the
pipeline and tells nobody anything, and the destructive-migration case specifically
needs a human to *decide*. Escalate by default; halt only on the two conditions
named above.

## Confidence + severity gating

For each candidate, a separate verifier scores confidence 0–100 (try to construct a
concrete failing input/scenario; if you can, that's high signal). Then gate by
class:

| Lens / category | Surface if confidence ≥ | Blocks merge if confidence ≥ |
|---|---|---|
| security, concurrency-atomicity, data-loss, input-path-safety | 50 | 70 |
| automation-honesty, integration-seam, control-flow | 60 | 80 |
| identifier-matching, state-persistence | 60 | 75 |
| migration-safety | 40 | 60 |
| CLAUDE.md adherence, style | 80 | (never blocks alone) |
| reuse-reinvention (advisory) | 70 | (never blocks) |

`migration-safety` sits lowest on purpose: with apply-on-merge, merging the diff
executes it, so a false negative is not a latent bug — it is a database change that
already happened. Its UNKNOWN-counts-as-FAIL rule is a floor under the table, not a
score: an unquantified blast radius blocks regardless of confidence, because the
thing you are uncertain about is exactly the thing being measured.

A finding marked `blocking: true` in the corpus inherits the lower (blocking)
threshold for its class. Lens I (reuse-reinvention) is `blocking: false` and keyed
to the reader's judgment, not a score band — the librarian surfaces the top
candidates and renders no verdict; Mirror judges each REUSE (same job — flag it),
ADAPT (near-variant — mention only on a clear reinvention), or NONE (not relevant —
don't flag). This is the same judgment contract `build_check.py` uses, so the
build-time consult and this gate agree. Judge on substance, not shared vocabulary;
no score threshold.

## Wiring into Mirror's verdict

- Any **blocking** finding (per table) → Mirror emits `REVIEW_REVISION` with the
  finding(s) in her standard `{file, line_range, severity, description}` shape, so
  Forge's existing revision loop handles them. Keep findings tightly scoped
  (file/line) to preserve the cheap revision loop.
- `REVIEW_EMERGENCY_HALT` is reserved for a dangerous/irreversible operation the
  diff *performs* (secret exposure, `rm -rf`, force-push, unguarded prod-data
  delete) — NOT for a latent data-loss *bug* in code logic (lock-free RMW,
  non-atomic write, cursor-skip). Those data-loss bugs are inline-fixable and
  route as `REVIEW_REVISION`.
- Sub-blocking findings → noted in the review narrative above the marker (where
  Beacon reads them), not gated on. (Do not open a separate PR comment — Mirror's
  flow is marker-based.)
- **Lens I (reuse/reinvention + restock) never blocks** — its notes (both reuse and
  catalog-on-build restock) always go in the narrative above the marker, regardless of
  confidence, and never enter `findings[]`. It is an advisory connect-on-build nudge, not a
  correctness gate; a reuse or restock note must not, on its own, turn a `REVIEW_PASS` into a revision.
- **Lens J (migration safety)** routes per its own section above — `REVIEW_REVISION`
  for rulebook and ordering faults, `REVIEW_ESCALATE` for irreversible data loss or
  an unknown blast radius, `REVIEW_EMERGENCY_HALT` only for prod or client content.
  Its **verdict block always goes in the narrative**, on a `REVIEW_PASS` as much as
  on a block — a clean migration Larry can read is the point, not just a blocked
  one.
- This runs **in addition to** the `test_regression_check.py` gate and the spec/AC
  checklist. Order: spec/AC → bug-hunt → test-regression gate. All three must pass
  for `REVIEW_PASS`.

## Tuning knobs (Phase-2 Pulse loop calibrates these)
- Exact thresholds in the gating table — calibrate to hold the ~89% catch-rate on the
  ground-truth set without blowing up false positives on recent clean PRs. **The
  false-positive rate on clean PRs is not yet measured; watch the first live reviews.**
- Whether the full bug-hunt fan-out (the eight A–H lenses; Lens I is advisory and can be
  skipped on re-reviews) runs on every review or only first-review (revision re-reviews could
  run a lighter targeted pass — revisions are scoped edits).
