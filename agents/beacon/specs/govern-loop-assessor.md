# Spec: Govern-Loop Assessor (operator-layer ROI/rank brain)

**Status:** Draft
**Author:** Beacon (drafted 2026-07-07)
**Approver:** Larry (date TBD)

## 1. Problem statement
Larry approves/rejects a stream of needs-Larry decisions (dashboard, Telegram, auto-rules). decision_outcome_ledger.py records each click and decision_outcome_reconcile.py joins the downstream build result (merged / merged_regressed / closed_unmerged) onto it. Nothing yet READS that joined record to tell Larry WHERE his approvals actually pay off. Without that, autonomy-widening and attention-allocation are guesswork.

## 2. Success criteria
A read-only assessor exists that, over the decision-outcome ledger, produces a ranked, human-readable answer to 'which areas of Larry's approvals ship clean vs. fizzle,' written to its own state file, auto-acting on nothing. Larry (or a downstream operator surface) can inspect the ranking to decide where attention/autonomy is best spent.

## 3. Users / consumers
Direct consumer: Larry (inspection) and, later, the Operator surfaces (Approvals queue, autonomy-graduation proposals). Downstream: this is chain-internal operator tooling, not a TruPath/Rocket-Station/AI-services deliverable. Handoff bar still applies: a stranger dev must understand it cold from this spec + the module docstring.

## 4. Scope (what's in)
- Read decision rows + build_outcome rows from ~/agents/state/decision-outcome-ledger.jsonl, joined on decision_key.
- Per decision_key, take the LATEST build_outcome row (append-only supersede) as the effective outcome.
- Bucket decisions by area dimensions available on the rows: actor (dashboard / telegram / auto-rule / larry-email), and auto_approved-by-rule vs human (derivable from the decision row's notes). Repo/task_type where derivable from the decision_key.
- Per bucket, compute counts and a payoff score: merged = +1, merged_regressed = -1 (shipped-but-bad is worse than not shipping), closed_unmerged = -0.5 (soft-negative; abandoned, not catastrophic). Weights are shadow-mode starting values, declared in one named place and tunable.
- Emit a ranked list (best-payoff to worst) with a plain-language reason per bucket, to its own state file ~/agents/state/govern-loop-assessment.json (or .jsonl). READ-ONLY over the ledger; never writes the ledger, never writes missions.json, never dispatches.
- A --once CLI for ad-hoc inspection, mirroring mission_staleness.py / the ledger CLIs.

## 5. Out of scope (what's deliberately not in)
- Any autonomy-widening PROPOSAL or action. Shadow-first: the assessor only observes and ranks. Turning a ranking into a proposal is a later, separately-approved slice.
- Any write to missions.json, the decision-outcome ledger, or GitHub.
- Timer/systemd wiring (a later slice, once the rankings have soaked and Larry trusts them — same start-read-only-then-wire discipline as PR #841).
- Dashboard surfacing.
- Empty-key ('') decision rows: per the ledger's KNOWN LIMITATIONS, these count in tallies but can never be joined to a build outcome; the assessor treats '' as count-don't-join and excludes it from payoff scoring.

## 6. Acceptance criteria
- [ ] Given a ledger with decision+build_outcome rows, the assessor produces a ranked bucket list with counts and payoff scores to its state file, and exits without mutating the ledger.
- [ ] When a decision_key has multiple build_outcome rows (e.g. an earlier closed_unmerged then a later merged), the LATEST row is the effective outcome (reopen+merge supersedes abandonment).
- [ ] Empty-key decision rows are excluded from payoff scoring but do not crash the pass.
- [ ] A malformed / missing ledger yields an empty assessment, not a raise (mirrors the ledger module's never-raise discipline).
- [ ] The assessor writes ONLY its own state file; a filesystem check confirms no write to decision-outcome-ledger.jsonl or missions.json.
- [ ] --once prints the ranking as JSON for inspection.

## 7. Architecture sketch
A single stdlib-only script scripts/govern_loop_assessor.py, importing decision_outcome_ledger as the read API (read_recent / records_for_key / new read helpers as needed). It groups decision rows, resolves each key's effective (latest) build_outcome, scores, ranks, and atomically writes the assessment state file. No external calls (the ledger already carries GitHub-derived outcomes; the assessor does not re-query GitHub). Pattern-match scripts/mission_staleness.py for structure, logging, and the read-only-scorer discipline.

### 7.1 RESOLVED design coupling — reconciler CLOSED handling (gating prerequisite guidance)
The gating card asks how the reconciler should treat a CLOSED-unmerged PR, to be decided 'in the context of how the assessor weights outcomes.' DECISION (Beacon, architect call): adopt LATEST-ROW-WINS + a bounded re-check settle window.
- merged / merged_regressed rows are TRULY terminal for reconciler idempotency (never re-checked, never superseded).
- closed_unmerged is recorded, but a closed key stays RE-CHECKABLE for a settle window (recommend 14 days) so a reopen+merge inside the window appends a superseding merged row; after the window it is treated as terminal to bound gh re-check cost. This means has_build_outcome()/the idempotency guard must distinguish 'terminal' (merged*) from 'provisional' (closed_unmerged within settle window).
- The assessor independently reads latest-row-wins per key, so even a late merge that lands by any path supersedes an earlier closed_unmerged at read time.
Rationale: the assessor weights closed_unmerged as a soft-negative; a WRONGLY-permanent closed_unmerged makes the assessor under-credit an area that actually shipped. Latest-row-wins + bounded re-check removes that false-positive while preserving append-only discipline (no row mutation). The gating delegation implements this plus its bundled cleanups (bounded-scan docstring fix, whole-file resolved-set, pass the resolved-set into reconcile to avoid O(keys x rows), a named non-recordable set for the 'pending' magic string).

## 8. Open questions / risks
- Bucket/area dimension granularity: actor + auto-vs-human is the v1 cut; repo/task_type enrichment depends on decision_key shape coverage (task_id-keyed decisions are not yet joined by the reconciler). To resolve: revisit after the reconciler's task-keyed join lands. Owner: Beacon, at assessor-build spec review.
- Starting weights (+1 / -1 / -0.5) are judgment calls; shadow mode exists precisely to validate them against real history before any decision leans on them. To resolve: Larry reviews the first weeks of shadow rankings. Owner: Larry.
- Sample size: at current low approval volume the ledger has only a handful of rows; rankings are not trustworthy until volume accrues. Shadow-first is the mitigation. To resolve: soak. Owner: time.

## 9. Handoff package requirements
This is chain-internal; the spec + module docstring are the primary handoff artifacts. The assessor-build PR must ship: a module docstring stating what it reads, what it writes, and what it never does; the named weight constants; --once CLI; and unit tests (fixture ledger -> expected ranking, latest-row-wins supersede, empty-key exclusion, missing-file empty result).

## 10. References
- scripts/decision_outcome_ledger.py (slice 1, row schema + read API + KNOWN LIMITATIONS).
- scripts/decision_outcome_reconcile.py (slice 2, the build-outcome join + the closed_unmerged bug this spec resolves).
- scripts/mission_staleness.py (the read-only scorer pattern to mirror).
- PR #841 (wires reconciler + staleness timers; the read-only-then-wire discipline).
- Capture cap-slice-2-reconciler-hardening-closed-unmerged-ter-8d9e (the gating prerequisite this spec guides).
- Capture cap-govern-loop-assessor-operator-layer-roi-rank-bra-28b0 (this initiative).

### Changelog
- 2026-07-07: Initial draft (Beacon), from the board-drain delegation of the govern-loop-assessor capture.
