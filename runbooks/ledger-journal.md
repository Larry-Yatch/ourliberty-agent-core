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

## Iteration 2 — 2026-05-18 16:10 UTC

**Week ending:** 2026-05-18
**Health:** ✅ Nominal
**Total:** $115.91
**Vs prior:** +$115.91 (+0.0%)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-05-18
**DM:** queued

## Iteration 3 — 2026-05-18 18:41 UTC

**Week ending:** 2026-05-18
**Health:** ✅ Nominal
**Total:** $115.91
**Vs prior:** +$115.91 (+0.0%)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-05-18
**DM:** queued

## Iteration 4 — 2026-05-25 07:00 UTC

**Week ending:** 2026-05-25
**Health:** 🟡 Drift
**Total:** $251.49
**Vs prior:** +$135.58 (+117.0%)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-05-25
**DM:** queued

## Iteration 5 — 2026-05-27 04:49 UTC

**Week ending:** 2026-05-25
**Health:** 🟡 Drift
**Total:** $251.49
**Vs prior:** +$135.58 (+117.0%)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-05-25
**DM:** queued

## Iteration 6 — 2026-05-28 18:57 UTC

**Week ending:** 2026-05-25
**Health:** 🟡 Drift
**Total:** $251.49
**Vs prior:** +$135.58 (+117.0%)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-05-25
**DM:** queued

## Iteration 7 — 2026-05-31 00:04 UTC

**Week ending:** 2026-05-25
**Health:** 🟡 Drift
**Total:** $251.49
**Vs prior:** +$135.58 (+117.0%)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-05-25
**DM:** queued

## Iteration 8 — 2026-06-01 00:08 UTC

**Week ending:** 2026-06-01
**Health:** 🟡 Drift
**Total:** $1611.38
**Vs prior:** +$1359.89 (+540.7%)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-01
**DM:** queued

## Iteration 9 — 2026-06-01 07:00 UTC

**Week ending:** 2026-06-01
**Health:** 🟡 Drift
**Total:** $1611.38
**Vs prior:** +$1359.89 (+540.7%)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-01
**DM:** queued

## Iteration 10 — 2026-06-03 00:22 UTC

**Week ending:** 2026-06-01
**Health:** 🟡 Drift
**Total:** $1611.38
**Vs prior:** +$1359.89 (+540.7%)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-01
**DM:** queued

## Iteration 11 — 2026-06-05 00:25 UTC

**Week ending:** 2026-06-01
**Health:** 🟡 Drift
**Total:** $1611.38
**Vs prior:** +$1359.89 (+540.7%)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-01
**DM:** queued

## Iteration 12 — 2026-06-07 00:26 UTC

**Week ending:** 2026-06-01
**Health:** 🟡 Drift
**Total:** $1611.38
**Vs prior week:** +$1359.89 (+540.7%)
**Anomalies:** 0 σ-flagged (ramp-up: σ-flagging suspended)
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-01
**DM:** queued

## Iteration 13 — 2026-06-08 07:01 UTC

**Week ending:** 2026-06-08
**Health:** 🟡 Anomalies
**Total:** $1041.64
**Vs prior week:** −$569.73 (−35.4%)
**Anomalies:** 179 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-08
**DM:** queued

## Iteration 14 — 2026-06-10 03:55 UTC

**Week ending:** 2026-06-08
**Health:** 🟡 Anomalies
**Total:** $1041.64
**Vs prior week:** −$569.73 (−35.4%)
**Anomalies:** 179 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-08
**DM:** queued

## Iteration 15 — 2026-06-12 02:31 UTC

**Week ending:** 2026-06-08
**Health:** 🟡 Anomalies
**Total:** $1041.64
**Vs prior week:** −$569.73 (−35.4%)
**Anomalies:** 179 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-08
**DM:** queued

## Iteration 16 — 2026-06-14 00:18 UTC

**Week ending:** 2026-06-08
**Health:** 🟡 Anomalies
**Total:** $1041.64
**Vs prior week:** −$569.73 (−35.4%)
**Anomalies:** 179 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-08
**DM:** queued

## Iteration 17 — 2026-06-15 07:02 UTC

**Week ending:** 2026-06-15
**Health:** 🟡 Anomalies
**Total:** $1135.74
**Vs prior week:** +$94.09 (+9.0%)
**Anomalies:** 360 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-15
**DM:** queued

## Iteration 18 — 2026-06-15 07:04 UTC

**Week ending:** 2026-06-15
**Health:** 🟡 Anomalies
**Total:** $1135.74
**Vs prior week:** +$94.09 (+9.0%)
**Anomalies:** 360 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-15
**DM:** cooldown-suppressed or write failed

## Iteration 19 — 2026-06-17 00:11 UTC

**Week ending:** 2026-06-15
**Health:** 🟡 Anomalies
**Total:** $1135.74
**Vs prior week:** +$94.09 (+9.0%)
**Anomalies:** 360 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-15
**DM:** queued

## Iteration 20 — 2026-06-19 00:10 UTC

**Week ending:** 2026-06-15
**Health:** 🟡 Anomalies
**Total:** $1135.74
**Vs prior week:** +$94.09 (+9.0%)
**Anomalies:** 360 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-15
**DM:** queued

## Iteration 21 — 2026-06-20 01:44 UTC

**Week ending:** 2026-06-15
**Health:** 🟡 Anomalies
**Total:** $1135.74
**Vs prior week:** +$94.09 (+9.0%)
**Anomalies:** 360 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-15
**DM:** queued

## Iteration 22 — 2026-06-22 02:13 UTC

**Week ending:** 2026-06-22
**Health:** 🟡 Anomalies
**Total:** $859.04
**Vs prior week:** −$276.70 (−24.4%)
**Anomalies:** 129 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-22
**DM:** queued

## Iteration 23 — 2026-06-22 07:03 UTC

**Week ending:** 2026-06-22
**Health:** 🟡 Anomalies
**Total:** $859.04
**Vs prior week:** −$276.70 (−24.4%)
**Anomalies:** 129 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-22
**DM:** queued

## Iteration 24 — 2026-06-24 00:41 UTC

**Week ending:** 2026-06-22
**Health:** 🟡 Anomalies
**Total:** $859.04
**Vs prior week:** −$276.70 (−24.4%)
**Anomalies:** 129 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-22
**DM:** queued

## Iteration 25 — 2026-06-26 00:14 UTC

**Week ending:** 2026-06-22
**Health:** 🟡 Anomalies
**Total:** $859.04
**Vs prior week:** −$276.70 (−24.4%)
**Anomalies:** 129 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-22
**DM:** queued

## Iteration 26 — 2026-06-28 01:01 UTC

**Week ending:** 2026-06-22
**Health:** 🟡 Anomalies
**Total:** $859.04
**Vs prior week:** −$276.70 (−24.4%)
**Anomalies:** 129 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-22
**DM:** queued

## Iteration 27 — 2026-06-29 00:23 UTC

**Week ending:** 2026-06-29
**Health:** 🟡 Anomalies
**Total:** $1184.79
**Vs prior week:** +$325.75 (+37.9%)
**Anomalies:** 285 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-29
**DM:** queued

## Iteration 28 — 2026-06-29 07:03 UTC

**Week ending:** 2026-06-29
**Health:** 🟡 Anomalies
**Total:** $1184.79
**Vs prior week:** +$325.75 (+37.9%)
**Anomalies:** 285 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-29
**DM:** queued

## Iteration 29 — 2026-07-01 00:55 UTC

**Week ending:** 2026-06-29
**Health:** 🟡 Anomalies
**Total:** $1184.79
**Vs prior week:** +$325.75 (+37.9%)
**Anomalies:** 285 σ-flagged
**Skipped rows:** 0
**Sentinel:** /home/larry/agents/blackboard/ledger/ledger-ready-2026-06-29
**DM:** queued
