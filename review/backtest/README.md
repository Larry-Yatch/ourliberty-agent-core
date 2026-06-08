# Bug-hunt gate backtest

Validates the Mirror bug-hunt gate (Phase 1) against ground truth: the 64 bugs from
`AUDIT_main_20260605.md` that all **passed Mirror's review** and shipped to main.
This is the acceptance test for the gate and the mini-CI for every future corpus
addition (`review/known-bug-patterns.yaml`).

## Pipeline

1. **`extract_findings.py`** → `findings.json` — parses the audit markdown into 64
   structured findings (id, severity, confidence, category, file, line, title, body).
2. **`map_fixes.py`** → `mapping.json` — maps each finding to the remediation commit
   that fixed it (63/64; only #33 unmapped). The reverse of that commit
   (`git show -R`) reconstructs the buggy code as a reviewable PR diff.
3. **`run_backtest.py`** → `results.json` — for each finding, hands the reconstructed
   buggy diff to the bug-hunt reviewer (`review/mirror-bughunt-lenses.md`), judges
   whether the known bug was caught, reports catch-rate by severity/category.

## Why reverse-diff (and not git-blame archaeology)

The audit doc is untracked, so its line numbers don't anchor to a commit. The
remediation commits, by contrast, are in git history and precisely scope each bug.
Reversing a fix commit yields the exact buggy code as a diff hunk — which is exactly
what Mirror reviews — making it the most faithful "would the gate catch this in a
PR?" test, and fully deterministic.

## Running

```bash
# dry-run the harness (no API): confirms every finding reconstructs a diff
python3 review/backtest/run_backtest.py --backend stub --severity HIGH

# 5-finding pilot, real reviewer (local `claude`, NOT the rate-limited droplet auth)
python3 review/backtest/run_backtest.py --backend claude --ids 1,11,9,4,12

# full HIGH set, then everything
python3 review/backtest/run_backtest.py --backend claude --severity HIGH
python3 review/backtest/run_backtest.py --backend claude
```

`--backend claude` shells `claude -p` per finding (review + judge = 2 calls each) —
this costs real tokens; start with `--pilot`/`--ids`. For the full 63-finding
fan-out, prefer a Workflow (needs explicit opt-in) over the serial script.

## Acceptance criteria (proposed)

- **HIGH findings: ≥ 80% caught.** A gate that misses high-severity correctness /
  security / data-loss bugs isn't doing its job.
- **MEDIUM: ≥ 60%.**
- Misses become the tuning worklist: each missed finding's class gets a sharper
  detection signature in the corpus, then re-backtest. This loop is the same one
  Phase 2 (Pulse) feeds in production.

## Known limitations (honest)

- **Multi-candidate mapping.** Findings in files touched by several remediation
  commits (e.g. the auth findings, 3 candidates each) are mapped to the *earliest*
  commit, which isn't always the precise fix — e.g. finding #1 (write-side TOCTOU)
  mapped to `2e12efc` (the broader /tmp-hardening PR) rather than the `468525f`
  SEC1 commit. Multi-candidate findings (`n_candidates > 1` in `mapping.json`)
  should be hand-verified before trusting their individual catch/miss verdict.
- **Synthetic introduction.** The reverse-diff presents the bug as a *removal of the
  fix*, not the original introducing PR. This is clean and scoped but slightly
  artificial; a reviewer sees "this PR removes a sanitizer" rather than "this PR
  adds an unsanitized writer." For most findings the signal is equivalent.
- **#33** (`agent_core_health_check.py`) has no in-window remediation commit — fixed
  outside the window or deferred; excluded from the set.
- The judge is itself an LLM; spot-check its catch/miss calls on the pilot before
  trusting aggregate numbers.
