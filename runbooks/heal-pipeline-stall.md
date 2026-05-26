# Runbook: heal_pipeline_stall

**Purpose.** Surface pipeline stalls to Larry via Telegram DM so they're caught the moment work stops flowing, not when someone notices hours later.

**Cadence.** Every 15 min via `ourliberty-heal-pipeline-stall.timer`. Silent unless a stall is detected. Deduplicated 1 hour per unique stall key.

**Never acts.** Surface only. The healer never kills processes, never merges PRs, never re-dispatches anything. Matches the posture of Joe's `pipeline_watcher.py` (the upstream model) and our existing `dispatch_sentinel.py`.

**Adapted from.** `GrowthMastery-ai/gm-agent-core/scripts/pipeline_watcher.py` (2026-04-15, Joe's "you never have to discover a stall on your own" doctrine).

---

## Seven checks (in order)

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

### 6. REVIEW_REVISION dispatched with no Forge session (chain discipline v3 GAP 1)

**Detects:** `outbox-notifier.log` has a WARN line matching `REVIEW_REVISION on task <task_id> has no forge_build_session_id`.

**Cause:** Mirror requested a revision on a PR that wasn't built through the standard Forge dispatch chain. Most common cause: Claude-as-Forge PR (Larry asked Claude to author a quick fix), OR a manually-pushed branch. The auto-resume path requires Forge's build session to `--resume` against; without it, the revision dispatch can't run. The direct fix in `outbox_notifier.py` already broadcasts a `larry_alerts.append_alert` when the path fires; this healer check is defense in depth in case that alert was suppressed (per-subject cooldown), the queue file was unwriteable, or the WARN was investigated late.

**Action recipe in DM:** Open the PR, read Mirror's findings (her latest review comment OR `grep "no forge_build_session_id" ~/agents/logs/outbox-notifier.log | grep "<task_id>"`), then either:
  - Apply the revisions to the PR branch by hand (`git commit`/`git push` directly), OR
  - Re-dispatch the work via Beacon with a fresh task_id so a new Forge build session threads through the chain.

**False-positive silencing:** This check is fundamentally additive — if the direct fix already DMed Larry, the per-task 6h cooldown on the healer side will suppress duplicate DMs. If a single task generates repeated WARNs (e.g. a daemon log replay), the per-`subject` cooldown is `pipeline-stall:no-session-revision:<task_id>` — clear it manually via `rm ~/agents/state/alert-cooldown/warning/heal-pipeline-stall_pipeline-stall_no-session-revision_<task_id>` to re-allow.

### 7. Open PR with no review-request dispatch logged (chain discipline v3 GAP 3)

**Detects:** An OPEN PR on a tracked repo older than 60 min that has NO matching `beacon → mirror review-request` event in `~/agents/logs/routing-events.jsonl`. Match heuristic: the PR's `headRefName` either ends with the routing event's `task_id` (the `forge/<task>` / `larry/<task>` convention) or contains the `task_id` as a substring.

**Cause:** The notifier's auto-dispatch only fires for PRs opened via the build-phase pipeline (Forge emits `PR opened: <url>` in her build result; the notifier scrapes it). PRs authored outside that pipeline — Claude-as-Forge, manual pushes, GitHub UI commits — never auto-dispatch. Without a manual route, Mirror never reviews and the PR sits.

**Action recipe in DM:** Dispatch the review manually via Beacon chat: `dispatch mirror review pr=<pr_url>`. Verify the dispatch fired with `tail -50 ~/agents/logs/routing-events.jsonl | grep <branch>`. The DM body includes the full PR URL and a pre-formed dispatch command — paste-and-send.

**False-positive silencing:** If a PR is intentionally not getting auto-routed (e.g. a draft, an experimental branch Larry wants to keep manual), the per-PR cooldown lives at `~/agents/state/alert-cooldown/warning/heal-pipeline-stall_pipeline-stall_unrouted-pr_PR#<N>`. Touching the cooldown file extends suppression by another 60 min; the 1h cadence repeats the DM if the PR is still open + still unrouted by the next tick. The longer-term silencer is to close the PR or route it manually.

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

## Scan window

Every Check gates its stall-trigger event timestamp against `SCAN_WINDOW_SECONDS` (default `86400`, 24h). Events whose anchor timestamp is older than the window are treated as historical record, not stalls, and produce no alert. This retires already-resolved incidents instead of re-firing on log lines from yesterday.

**Default rationale.** 24h is long enough to cover overnight quiet periods (a real stall that begins at 11pm and goes uninvestigated until 9am is still fully in-window) and short enough that a stall older than a day is past the point where another DM helps — at that age the action is human investigation, not another notification. Three false positives on 2026-05-26 (Mirror PASS markers from PRs Larry had already merged manually the prior day; the cooldown had expired but `LOG_LOOKBACK_HOURS=24` was still serving the events) drove the explicit constant.

**When to tune.** Two operational shapes legitimately argue for a different default:
  - **Long-running deployments / weekend-only operation.** If the chain is intentionally idle for >24h (e.g., a long migration weekend), a real stall that begins on Friday and persists past Monday morning might fall outside the window before anyone notices. Bump to 48h or 72h for the window of concern.
  - **Holiday gaps.** A multi-day holiday where the chain is paused (kill-switch on) followed by a resumed scan: events from before the pause might trigger if they're inside the window. Either keep kill-switch on through the boundary, or shrink the window temporarily to retire pre-pause events faster.

**How to tune.** Edit `SCAN_WINDOW_SECONDS` near the top of `scripts/heal_pipeline_stall.py`, then `sudo systemctl restart ourliberty-heal-pipeline-stall.timer`. The change takes effect on the next tick. Per the doctrine in [Self-optimizing-config-via-Pulse-Check](https://github.com/Larry-Yatch/ourliberty-agent-core/blob/main/docs/operating-manual.md), this constant is a candidate for Pulse-driven auto-tuning once that pattern's threshold-config layer ships — for now, it's hand-tuned.

**Recovery for a real >SCAN_WINDOW_SECONDS stall.** If a real stall persists past the window (e.g., a PR that's been stuck Mirror-PASS-unmerged for 30h because the AUTO_MERGE queue jammed and no one noticed), the healer falls silent — the event is past-window. Manual recovery path:
  1. Identify the stalled task via `git log`, `gh pr list --state open`, or by direct grep of `~/agents/logs/outbox-notifier.log` (the log still has the event; the healer just doesn't alert on it).
  2. Apply the recovery action from the appropriate Check's section above (manually merge, manually dispatch Mirror, investigate the worktree, etc.).
  3. If the >24h-old class of stall is recurring, that's the signal to bump `SCAN_WINDOW_SECONDS` upward — the assumption "24h is enough to catch every real stall while it's still actionable" no longer holds.

The healer's heartbeat still updates even when every Check skips its events (the heartbeat fires on the run, not the alerts). `heal_stale_daemon_code` will not false-positive a window-skip-only run.

## Tuning thresholds

The threshold constants live near the top of `scripts/heal_pipeline_stall.py`:

| Constant | Default | What it gates |
|---|---|---|
| `FORGE_BUILT_NO_PR_MIN` | 120 | Check 1 — Forge done but no PR |
| `PR_NO_MIRROR_DISPATCH_MIN` | 30 | Check 2 — doc PRs awaiting Mirror dispatch |
| `PR_NO_MIRROR_DISPATCH_CODE_MIN` | 60 | Check 2 — code PRs (regression check legitimate) |
| `MIRROR_PASS_UNMERGED_MIN` | 30 | Check 3 — PASS classified but PR still OPEN |
| `MIRROR_MARKER_INVISIBLE_MIN` | 30 | Check 4 — Mirror notified but no marker classified |
| `RETRY_EXHAUST_WINDOW_MIN` | 30 | Check 5 — retry-cap exhausted recency window |
| `PR_UNROUTED_MIN_AGE_MIN` | 60 | Check 7 — minimum PR age before flagging as unrouted |
| `ROUTING_EVENTS_LOOKBACK_HOURS` | 168 | Check 7 — how far back into routing-events.jsonl to scan |
| `ALERT_DEDUP_HOURS` | 1 | Suppress same alert key within window |
| `LOG_LOOKBACK_HOURS` | 24 | How far back into outbox-notifier.log to read |
| `SCAN_WINDOW_SECONDS` | 86400 | Per-Check stall-trigger event age cap. See [Scan window](#scan-window) above. |

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
