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

## Confidence + severity gating

For each candidate, a separate verifier scores confidence 0–100 (try to construct a
concrete failing input/scenario; if you can, that's high signal). Then gate by
class:

| Lens / category | Surface if confidence ≥ | Blocks merge if confidence ≥ |
|---|---|---|
| security, concurrency-atomicity, data-loss, input-path-safety | 50 | 70 |
| automation-honesty, integration-seam, control-flow | 60 | 80 |
| identifier-matching, state-persistence | 60 | 75 |
| CLAUDE.md adherence, style | 80 | (never blocks alone) |
| reuse-reinvention (advisory) | 70 | (never blocks) |

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
