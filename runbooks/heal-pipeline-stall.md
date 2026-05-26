# Runbook: heal_pipeline_stall

**Purpose.** Surface pipeline stalls to Larry via Telegram DM so they're caught the moment work stops flowing, not when someone notices hours later.

**Cadence.** Every 15 min via `ourliberty-heal-pipeline-stall.timer`. Silent unless a stall is detected. Deduplicated 1 hour per unique stall key.

**Never acts.** Surface only. The healer never kills processes, never merges PRs, never re-dispatches anything. Matches the posture of Joe's `pipeline_watcher.py` (the upstream model) and our existing `dispatch_sentinel.py`.

**Adapted from.** `GrowthMastery-ai/gm-agent-core/scripts/pipeline_watcher.py` (2026-04-15, Joe's "you never have to discover a stall on your own" doctrine).

---

## Five checks (in order)

### 1. Forge built but no PR opened

**Detects:** outbox-notifier log has `[forge] done task=X success=True` >2h ago AND no PR with branch `forge/X` (or `larry/X`) exists on any tracked repo (open or recently merged).

**Cause:** Forge crashed after build, or `gh pr create` silently failed, or the worktree was cleaned up before the PR was opened.

**Action recipe in DM:** SSH and check the worktree state, inspect Forge's session log, manually run `gh pr create` from the worktree if the branch exists locally.

### 2. PR opened but no Mirror review-request dispatched

**Detects:** A `forge/<task_id>` PR exists, but no `review-request dispatched mirror` log line for that task_id. Age threshold: 30 min for doc PRs (`docs(`, `spec(`, `chore(` prefixes), 60 min for code PRs (`fix(`, `feat(`, `refactor(`, `perf(`, `build(`).

**Cause:** Forge's result notify never fired correctly, or the build-phase dispatch fell through silently. Sometimes the chain doesn't auto-dispatch Mirror for externally-authored `larry/` branches — those are skipped by this check (`larry/` branches require manual Mirror dispatch via Beacon).

**Action recipe in DM:** Grep outbox-notifier log for the task. If Forge notify never fired, dispatch Mirror manually via Beacon.

### 3. Mirror PASS but PR still OPEN

**Detects:** `marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-X.json)` log line >30 min ago, AND the PR for that task is still OPEN on a tracked repo.

**Cause:** AUTO_MERGE never fired (notifier crash between PASS-process and merge-call), or `gh pr merge` returned non-zero and the primary path didn't retry. The existing `heal_pr_auto_merge.py` retries the `outcome=failed` case; this check catches the case where AUTO_MERGE never logged an outcome at all.

**Action recipe in DM:** Check `AUTO_MERGE.*<task>` in outbox-notifier log. If no AUTO_MERGE line at all, manually merge: `gh pr merge <N> --repo <repo> --squash --delete-branch`.

### 4. Mirror reviewed but no marker classified (the marker-shape drift signal)

**Detects:** `notified beacon <- mirror (mirror-result, depth=1, file=notify-X.json)` log line >30 min ago, AND no subsequent `marker-notified beacon <- mirror` for the same task_id, AND no `AUTO_MERGE` line for the task (the AUTO_MERGE skip handles the case where a follow-up Mirror dispatch eventually classified the marker).

**Cause:** Mirror emitted a marker in a non-canonical shape that the parser doesn't recognize. Per PR #105's discipline mandate, Mirror should always use `scripts/marker.py render mirror <verdict>`; this alert means she didn't. Three drifted shapes observed historically (PR #101 + PR #104 reviews): `=== REVIEW_RESULT ===` wrapper with verdict in JSON, inline `REVIEW_PASS:` + fenced JSON, and hand-typed marker blocks with wrong delimiter strings.

**Action recipe in DM:** Read Mirror's session JSONL for the actual verdict (`grep -oE "=== REVIEW_(PASS|REVISION|ESCALATE|EMERGENCY_HALT) ===" <session>.jsonl`). Manually merge if PASS. If the discipline mandate from PR #105 was bypassed, flag the recurrence for a Forge follow-up dispatch tightening Mirror's CLAUDE.md.

### 5. Retry-cap exhausted in last 30 min

**Detects:** `All retries exhausted` log lines in the inbox-watcher systemd journal from the last 30 min.

**Cause:** An agent task hard-failed repeatedly and was dead-lettered. Could be a marker-error feedback loop, a Claude API outage, a corrupt envelope, or a structural bug in the task.

**Action recipe in DM:** Read the journal for the full retry trace, then check the dead-letter dir (`~/agents/inboxes/*/.invalid/`).

---

## State + observability

| Path | Purpose |
|---|---|
| `~/agents/logs/heal-pipeline-stall.log` | All run logs (INFO every run, WARN/ERROR on failures) |
| `~/agents/blackboard/heal-pipeline-stall-state.json` | Per-stall-key dedup timestamps |
| `~/agents/blackboard/heal-pipeline-stall.heartbeat` | Updated on every run; consumed by `heal_stale_daemon_code.py` (PR #105) |

## Kill switch

`touch ~/agents/healers.disabled` disables this healer (and all other ourliberty healers). The script exits 0 immediately on detecting the file. Standard pattern across the healer constellation.

## Disabling just this healer

`sudo systemctl stop ourliberty-heal-pipeline-stall.timer && sudo systemctl disable ourliberty-heal-pipeline-stall.timer`

## Manual run

`python3 /home/larry/agent-core/scripts/heal_pipeline_stall.py` — same code path as the systemd-driven invocation. Useful for debugging or pre-deployment validation.

## Tuning thresholds

The five threshold constants live near the top of `scripts/heal_pipeline_stall.py`:

| Constant | Default | What it gates |
|---|---|---|
| `FORGE_BUILT_NO_PR_MIN` | 120 | Check 1 — Forge done but no PR |
| `PR_NO_MIRROR_DISPATCH_MIN` | 30 | Check 2 — doc PRs awaiting Mirror dispatch |
| `PR_NO_MIRROR_DISPATCH_CODE_MIN` | 60 | Check 2 — code PRs (regression check legitimate) |
| `MIRROR_PASS_UNMERGED_MIN` | 30 | Check 3 — PASS classified but PR still OPEN |
| `MIRROR_MARKER_INVISIBLE_MIN` | 30 | Check 4 — Mirror notified but no marker classified |
| `RETRY_EXHAUST_WINDOW_MIN` | 30 | Check 5 — retry-cap exhausted recency window |
| `ALERT_DEDUP_HOURS` | 1 | Suppress same alert key within window |
| `LOG_LOOKBACK_HOURS` | 24 | How far back into outbox-notifier.log to read |

These should eventually migrate to `config/system_tab_thresholds.json` once E4.4d's threshold config layer ships (per the [self-optimizing-config-via-Pulse-Check pattern](https://github.com/Larry-Yatch/ourliberty-agent-core/blob/main/docs/operating-manual.md) backlog item #1).

## When to expect this healer to fire

- **Healthy chain:** never. The healer logs `no stalls detected` and exits 0.
- **A real stall hits one of the five checks:** ONE DM via Telegram within the next 15-min tick after the stall crosses its threshold. Each unique stall fires once per hour at most.
- **Multiple stalls simultaneously:** one DM per unique stall. Maximum surface per tick is bounded by the number of distinct task_ids in stalled state.

## When NOT to expect this healer to help

- **A task is taking longer than expected but no log line is suspicious.** This healer checks discrete stall conditions, not "is the task spending too long on its current phase." Per-session duration monitoring is the E4.4d System tab's job (dashboard-side, threshold-gated, see spec § 5.4-5.5).
- **A daemon is running stale code.** That's `heal_stale_daemon_code.py` (PR #105) — separate concern.
- **A PR has a Mirror REVIEW_REVISION and Forge needs to revise.** This is normal chain behavior, not a stall. Check 4 explicitly skips REVISION/ESCALATE intents.

## Operational history

- Shipped 2026-05-26 via PR #(TBD on merge).
- Codifies the manager-duty pattern Larry + Claude did manually throughout the 2026-05-25 session — every status answer that session came from this exact set of checks performed by hand via SSH.
