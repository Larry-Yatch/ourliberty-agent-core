# Ledger — Weekly Run Journal

Append-only chronological log of every weekly run of `scripts/ledger_weekly.py`. One entry per run, written by the Python module via `append_journal()`. Auto-committed and pushed by `scripts/run_ledger.sh` after each run.

## Format

```markdown
## Iteration <N> — <YYYY-MM-DD HH:MM UTC>

**Week ending:** <YYYY-MM-DD>
**Health:** ✅ Nominal | 🟡 Anomalies | 🟡 Drift | 🔴 Failed
**Total:** $<N.NN>
**Vs prior:** ±$<N.NN> (±M.M%)  |  n/a (no prior week)
**Anomalies:** <N> σ-flagged  |  ramp-up note
**Skipped rows:** <N>
**Sentinel:** <path>
**DM:** queued | cooldown-suppressed | write failed | <error>
```

Iteration numbers are monotonic: the module reads the highest existing `## Iteration N` line and increments. Keep entries terse — anyone reading should be able to scan the journal and see weekly cost trends and any run-time failures.

---

_No runs yet — the timer enables on next deploy. First entry will be appended when the Monday cron fires._
