# Bug-hunt gate Phase 2 — self-tuning distillation loop

Phase 1 (`review/known-bug-patterns.json` + `review/mirror-bughunt-lenses.md`) is the
per-PR gate. **Phase 2 teaches it**: full-codebase audit findings → distil into generalized
bug CLASSES → mini-backtest → propose → Larry approves → corpus PR. Without it the corpus is
frozen at its 14-class seed; with it the gate improves as new audits surface new classes.

## Pipeline

1. **`extract_findings.py`** (in `../backtest/`) parses an audit `AUDIT_main_<date>.md` into
   structured `findings.json` (id/severity/category/file/line/title/body).
2. **`distill_findings.py`** — the pure core. Classifies each finding *recurrence-of-known*
   (matches an existing corpus class) vs *novel*, clusters novels into new generalized
   classes, and a **dedup-guard** (token-overlap, same lens) prevents accreting synonym
   classes. The two LLM steps (classify, cluster) are injected callables → unit-testable
   with stubs. The classifier reads corpus classes ONLY (not the lens prose) so the LOCO
   proof isn't leaked.
3. **`run_distill.py`** — the heavy desktop job. Runs the classify/cluster (local
   `claude -p`, NOT the droplet auth) then the two backtests (guardrail c):
   - **(c-1) corpus-regression** — any *sharpened* signature must not drop the existing
     ground-truth catch-rate below HIGH≥80% / MEDIUM≥60% (`../backtest/run_backtest.py`).
   - **(c-2) novel-class proof-of-catch** — each new class must catch its own example
     findings (`run_backtest.backtest_class`, signature-injected + context-reading).
   Writes a proposal artifact (`~/agents/blackboard/pulse-distill-proposals/distill-<date>.json`,
   `applied:false`). It NEVER edits the corpus — the Beacon `approve distill-update-<date>`
   handler is the sole writer (guardrail b: one canonical corpus).

## Running

```bash
# extract + distil a real audit (LLM + backtests — costs API; local claude, not droplet)
python3 review/backtest/extract_findings.py AUDIT_main_<date>.md
python3 review/distill/run_distill.py review/backtest/findings.json --source-audit AUDIT_main_<date>.md

# deterministic structural dry run (no API)
python3 review/distill/run_distill.py review/backtest/findings.json --stub --no-backtest --out /tmp/x.json
```

## Acceptance / proof (no live audit needed)

`test_distill_loop.py` — **leave-one-class-out (LOCO) sweep**: for each seed class, remove
it, feed its labeled findings back, assert the loop marks them novel, the re-derived class
is NOT falsely dedup-merged into a surviving sibling, and convergence reports the right
novel-share. Plus a **saturation negative control** (re-distil the full seed → 0 novel).
Both run without API. A `OURLIBERTY_DISTILL_LIVE=1` tier backtests one held-out class for
real. Run: `python3 -m unittest review.distill.test_distill_loop`.

## Self-firing operations (cycle-prompt § 5.0b)

- **`../../scripts/distill_detector.py`** — notices an un-distilled audit landed, DMs Larry
  to run the distiller. Per-audit nag sentinel; never re-nags.
- **`audit_cadence_signal.py`** — the durable capture for the deferred *audit-cadence*
  decision (schedule recurring audits vs on-demand). Fires once after the first post-seed
  audit with the convergence reading + recommendation + a Missions Parked-lane card.

The convergence metric (novel-share per audit) is the maturity signal: high across real
audits ⇒ corpus still learning ⇒ scheduling audits pays; low ⇒ on-demand suffices.
