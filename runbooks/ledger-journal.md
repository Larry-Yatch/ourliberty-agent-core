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

## Iteration 1 — 2026-05-15 23:51 UTC

**Week ending:** 2026-05-11
**Health:** ✅ Nominal
**Total:** $0.00
**Vs prior:** n/a (no prior week)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-05-11
**DM:** queued

_Smoke dispatch `smoke-and-merge-ledger-pr25-001`: invoked `python3 scripts/ledger_weekly.py` against the live droplet before merging PR #25. Production-path verifications: ✅ markdown report `~/agents/blackboard/ledger/weekly-2026-05-11.md` written + non-empty; ✅ JSON sidecar `~/agents/blackboard/ledger/weekly-2026-05-11.json` parses + conforms to spec § 7 schema (all 9 required fields, `schema_version=v1`); ✅ sentinel `ledger-ready-2026-05-11` touched; ✅ DM queued via `larry_alerts.append_alert` (`source=ledger, subject=weekly-2026-05-11`) with heartbeat shape — week of 2026-05-11 covers [2026-05-04, 2026-05-11) which had zero rows (Ledger wasn't live yet), so heartbeat path exercised correctly. Substantive computation paths separately validated against `--week-ending 2026-05-18 --output-dir /tmp/smoke-substantive --no-dm`: 196 real cost rows → total $88.68, by-agent rendered (beacon $20.11 / forge $33.73 / mirror $10.88 / pulse $23.95), by-task_type rendered (`unknown` bucket holds 148/196 rows for Pulse cycle + notify dispatches per PR body's preflight note), top-5 correctly placed `build-ledger-001` (this PR's own build) at $7.31, retry overhead 24.0%. Anomalies = ramp-up notice as expected (no prior sidecars). All four success criteria pass; PR #25 merged at commit 62cbcb0._
