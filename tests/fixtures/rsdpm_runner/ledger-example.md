# RSDPM build ledger — measured cost per headless builder run

One row per `claude -p` run. `est_usd` is the CLI's client-side estimate from the JSON result envelope
(`total_cost_usd`, list-price basis) — the runs are on Max subscription slots, so this is a usage meter,
not a bill. The runner's caps derive from these rows (2× the measured cost per PR; a wave cap = the sum).

| date | PR | step | slot | model | cap | est_usd | wall | api time | turns | in / cache-write / cache-read / out tokens | outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-30 | #283 (S, 0063) | build → PR open | b | claude-fable-5-1 | $80 | **$28.65** | 47m 35s | 44m 10s | 179 | 2,852 / 547,379 / 29,986,860 / 203,582 | success — PR open on `feat/lifecycle-substrate` @ `6d65a89`, no label, +3233/−299 over 28 files |
| 2026-09-30 | #283 (S, 0063) | review r1 (10) → fix round 1 | b | claude-fable-5-1 | $50 | **$19.81** | 32m 33s | — | 104 | 2,252 / 403,588 / 17,594,465 / 146,418 | success — 3 commits, each pushed as it went green; head `b686578`; all 10 findings reproduced RED then closed; void check 37/37; PR body re-issued (item 11) |
| 2026-09-30 | #283 (S, 0063) | review r2 (10, count level → stop rule) → fix round 2 (subtraction) | b | claude-fable-5-1 | $40 | **$13.36** | 21m 23s | — | 54 | 1,666 / 293,357 / 10,394,706 / 97,554 | success — 5 commits, each pushed; head `602b538`; net −32 lines (+272/−304); reason sheet = one state machine with a state×door table; void check 45/45; one self-introduced flake caught by the full suite and fixed in its own commit |
| 2026-09-30 | #283 (S, 0063) | review r3 (10; severity 0 reachable → rule changed) → fix round 3 (7 small items) | b | claude-fable-5-1 | $30 | **$17.51** | 32m 58s | — | 83 | 2,474 / 337,588 / 18,520,750 / 122,084 | success — 6 commits, each pushed; head `069cc3c`; one self-inflicted sweep (a live void-check mutation committed via `git add -A`) caught and reverted by the builder; void check 54/54; CI green |

## Notes from the first run (the run that sets the runner's rules)

- **Pushed ONCE, at the end.** Five local commits, one push just before `gh pr create`. The brief said commit + push after every green step; the builder committed per step but did not push. The runner must enforce the push (a post-commit hook or a `git push` in the loop), not the brief.
- **Cache reads dominate.** ~30M cache-read tokens vs ~0.55M cache-write: the builder's context (contract + migrations + brief) is re-read every turn. A cap set on dollars tracks this fine; a cap set on turns would not.
- **The evidence gate self-served.** The builder ran void_check / operator_artifacts, filed them via `discipline_inbox`, and `gh pr create` passed with no waiver. Zero interruptions to Larry.
- **Cap primitive:** `--max-budget-usd` compares against the client-side estimate (docs: "client-side estimates, not authoritative billing"), and the run stayed under it, so whether it *enforces* on a subscription slot is still unmeasured. First cap for a PR of this size: 2 × $28.65 ≈ **$60**.

## Daily line

| date | PRs merged | spare-slot spend (day) | primary reviews | paused / stopped | note |
|---|---|---|---|---|---|
| 2026-09-30 | 1 (#283 S, `71851a7`; 0063 applied to staging 14:08 MDT, re-verified CLEAN) | $79.33 (4 runs: $28.65 · $19.81 · $13.36 · $17.51) | 3 | none | count-stop fired at r2 and r3; severity bar met at r3; rule changed by Larry to severity |
