# The RSDPM build runner — operator page

`scripts/rsdpm_runner.py` does the MECHANICAL steps of running one headless builder per
RSDPM PR: make the worktree, install the push hook, prepend the standard wrapper to the
brief, dispatch `claude -p` detached with a dollar cap and a wall clock, watch it, and file
the cost row. The manager keeps every judgement: reading findings, deciding per finding,
the click-through, the merge. Spec and the measured runs it was built from:
`~/dev/ol-work/rsdpm-build-ledger.md` and `rsdpm-phased-plan-2026-09-22.md`
§ "PRE-BUILD REVIEW + RUNNER SAFETY".

Python 3.9, stdlib only. `--dry-run` (before the subcommand) prints exactly what a command
would do and writes nothing outside the state dir. Exit codes: `0` ok · `2` refused (the
message says why) · `3` the builder failed (cap hit, error, or no result line).

## Commands

| command | what it does | refuses when |
|---|---|---|
| `build --pr S --brief <file> --slot b\|c --cap <usd> --branch feat/<name> [--repo ~/dev/RSDPM] [--base origin/main]` | `git fetch` → `git worktree add ~/dev/rsdpm-wt-S -b <branch> <base>` → `npm ci` → copy `.env.local` + `e2e/.auth/state.json` → per-worktree post-commit push hook → wrapper + brief → one detached `claude-alt <slot> -p --output-format stream-json --max-budget-usd <cap>` under `perl alarm` (default 4 h, `--wall-seconds`) | `STOP` present · two builders alive · that slot already busy · slot not logged in · branch already on origin · worktree already exists · `.env.local` missing · `--cap` or `--wall-seconds` ≤ 0 (that would be no cap / no wall clock) · `--claude-cmd` not an executable |
| `fix --pr S --round <n> --findings <ReportFindings.json> --decisions <md> --slot b\|c --cap <usd>` | composes the fix brief (every finding VERBATIM + the manager's decision per finding + the evidence list + the standing rules) and dispatches a FRESH builder in the EXISTING worktree | the `build` guards · no run recorded for that PR · worktree missing or dirty · local HEAD ≠ origin (unpushed) · a finding without a decision, a decision for a finding that does not exist, an empty or duplicate decision |
| `watch --pr S [--once]` | every 30 s: pid alive? `LOCAL COMMIT` / `PUSH` / `UNPUSHED` lines from local HEAD vs `git ls-remote`, `PR #n` + `COMMENTS n` via `gh`; on exit parses the LAST `result` line, files the ledger row, frees the slot; exit 3 on `error_max_budget_usd`, `is_error`, or a missing result line (wall clock hit) | no run recorded |
| `ledger --pr S --step "<text>" --stream <file.jsonl>` | parses one stream-json file (per-message usage deduped by `message.id`; the result envelope is authoritative) and appends ONE row after the last row of the runs table, columns in the file's fixed order; reads slot + cap from the run's `.meta.json` when it sits beside the stream | a row for that session is already there (idempotent) · the ledger's header columns changed |
| `classify --findings <json>` | a table per finding with a SEVERITY column pre-filled from `category` only (`correctness`/`security` → `reachable?`, the rest → `latent?`, unknown → `?`) and the stop-rule sentence. You replace each `?` word with `reachable` / `latent` / `coverage` / `record` by hand. It decides nothing. | not a ReportFindings JSON |
| `status` | STOP present? · every run's pid alive? · the slot table · the last ledger row per PR | — |
| `daily [--repo ~/dev/RSDPM]` | one line: PRs merged today (`gh pr list --state merged --search "merged:>=today"`), spare-slot spend today (sum of today's `est_usd`), primary reviews (rows whose step says `review r<n>`), paused/stopped | — |
| `stop` / `resume` | writes / removes `STOP`; `build` and `fix` refuse while it exists | — |

**Decisions file shape** (for `fix`): one heading per finding, in the findings' order:

```
## F1
FIX AT THE ROOT — …
## F2
NO CODE — intended. Add one sentence to the PR body …
```

## The state dir — `~/dev/ol-work/runner/` (`--state-dir`)

```
runs/<PR>/<step>-<timestamp>.prompt.md      the exact prompt sent (wrapper + brief)
runs/<PR>/<step>-<timestamp>.stream.jsonl   the builder's stream-json (stdout)
runs/<PR>/<step>-<timestamp>.err            its stderr
runs/<PR>/<step>-<timestamp>.pid            the detached pid
runs/<PR>/<step>-<timestamp>.meta.json      pr, step, slot, cap, branch, worktree, repo, argv, started_at
slots.json                                  slot → {pid, pr, step, started_at}; dead pids are ignored
STOP                                        the kill switch
```

The ledger it appends to is `~/dev/ol-work/rsdpm-build-ledger.md` (`--ledger`). The only
file the runner ever deletes is its own `STOP`.

## The push hook

Every commit in a runner worktree is pushed by a `post-commit` hook. It is installed under
the worktree's OWN gitdir (`<repo>/.git/worktrees/rsdpm-wt-S/hooks/`) and wired with a
per-worktree `core.hooksPath`, which needs `extensions.worktreeConfig=true` on the repo —
the runner sets it. Measured on git 2.39: `git rev-parse --git-path hooks` inside a
worktree resolves to the SHARED `.git/hooks`, so a hook there would auto-push commits made
in `~/dev/RSDPM` itself; the per-worktree wiring is why a commit in the main checkout
never pushes (`tests/test_rsdpm_runner.py::test_hook_pushes_worktree_commits_and_never_the_main_checkout`).
The hook cannot fail a commit; if a push fails it says so on stderr and the wrapper tells
the builder to push by hand before the next step. `watch` prints `UNPUSHED` when local and
remote heads differ.

## What it will never do

- Run a review, verify a finding, decide a severity, click through, label a PR, or merge.
  There is no merge, label, or review command anywhere in the script; a test greps for them.
  The manager sends the review to the Opus session and answers `classify`'s `?` column.
- Dispatch a third builder, dispatch while `STOP` exists, dispatch on a slot whose
  `claude auth status` is not `loggedIn`, or start two builders on one slot.
- Write to staging or the droplet, or delete anything under `~/dev/RSDPM`. Its git writes are
  `fetch`, `worktree add`, and the per-worktree hook config; its only deletion is `STOP`.
- Rewrite a ledger row, reorder the ledger's columns, or file the same run twice.
- Retry a failed step, raise a cap, or decide what happens at a stop. `watch` exits 3 and
  prints why; the next step is the manager's.
- Run a `claude` from its tests: every test dispatches to a recording fake.

## A typical PR

```
rsdpm_runner.py --dry-run build --pr D --brief ~/dev/ol-work/rsdpm-pr-D-builder-brief.md --slot b --cap 60 --branch feat/waiting-on-lifecycle
rsdpm_runner.py build --pr D --brief … --slot b --cap 60 --branch feat/waiting-on-lifecycle
rsdpm_runner.py watch --pr D                 # blocks; files the ledger row on exit
#   … manager: /code-review high in the review session → findings JSON; decisions .md …
rsdpm_runner.py classify --findings ~/dev/ol-work/runner/D-r1.json
rsdpm_runner.py fix --pr D --round 1 --findings ~/dev/ol-work/runner/D-r1.json --decisions ~/dev/ol-work/runner/D-r1-decisions.md --slot b --cap 40
rsdpm_runner.py watch --pr D
rsdpm_runner.py daily
```
