# Tunables — single source of truth for system dials

Every numeric or boolean knob in the agent system, in one place. The values listed are what's live at the time of the most recent entry in the change log at the bottom. When you change a dial, update both its row here AND the change log.

**Why this doc exists.** Dials live scattered across `config/`, `scripts/*.py`, systemd units, and IDENTITY/CLAUDE files. Without a single index it's hard to know what's tunable, what the current setting is, and whether anyone's revisited the choice. This doc collects them so quarterly reviews are cheap — read the table, decide what (if anything) to retune.

**How to use this doc.**

- Skim the **Periodic review cadence** section once a quarter and the table once a month.
- When a dial fires unexpectedly (alert flood, budget exhausted, cooldown swallowed a real alert), check the table row first to see the design intent + the retune trigger.
- When adding a new dial to the system, add the row here in the same commit. Drift between this doc and the code is the failure shape.

---

## Dispatch + clarification protocol

| Dial | Location | Current | Range | Retune trigger |
|---|---|---|---|---|
| `max_clarifications` (default) | `scripts/forge_preflight_handler.py` `DEFAULT_MAX_CLARIFICATIONS` | 3 | 0–10 | Forge consistently hits the cap and Beacon revises productively → raise. Cap rarely fires → leave; the budget isn't load-bearing. |
| `MAX_NOTIFY_DEPTH` | `scripts/outbox_notifier.py` | 1 | 1–3 | A new multi-hop notify shape needs ≥ 2 hops AND can't bypass via marker/clarification leg → raise carefully. |
| `MAX_MARKER_ERROR_RETRIES` | `scripts/outbox_notifier.py` | 3 | 1–5 | An agent's marker grammar fails repeatedly on real cases (not just CLAUDE.md drift) → raise. Pattern of stuck dispatches at retry 3 → lower or fix the marker design. |
| `MIN_PROMPT_LEN` | `scripts/dispatch_validator.py` | 100 | 50–200 | F24 empty-prompt failure mode resurfaces → keep or raise; raising can reject legitimate one-line dispatches. |
| `MAX_PROMPT_LEN` | `scripts/dispatch_validator.py` | 50000 | 10000–200000 | Real spec exceeds this → raise. Probably never. |
| `MIN_TIMEOUT` / `MAX_TIMEOUT` | `scripts/dispatch_validator.py` | 60 / 14400 sec | — | A class of long-running dispatch needs > 4h → raise MAX. Floor exists to catch `timeout: 0` misuse. |
| `POLL_INTERVAL_SECONDS` (notifier) | `scripts/outbox_notifier.py` | 5 sec | 1–30 | End-to-end latency (Forge outbox → Beacon notify) too slow → lower. Too noisy in journal → raise. |

---

## D3.5 review loop (5a forward-compat; 5b/5c/5d activate)

| Dial | Location | Current | Range | Retune trigger |
|---|---|---|---|---|
| `max_revisions` | `config/agent-models.json` `loop_bounds` | 3 | 1–5 | Forge↔Mirror revisions consistently converge in 1–2 → lower. Mirror keeps requesting revision past 3 → raise OR force ESCALATE earlier. |
| `max_replans` | `config/agent-models.json` `loop_bounds` | 2 | 1–4 | Beacon's replans usually land in 1 → lower to 1 (one revision is enough). 2 replans rarely enough to resolve → raise to 3 OR escalate to Larry sooner. |
| `cost_per_task_usd` | `config/agent-models.json` `loop_bounds` | 15.0 | 1.0–20.0 | Stuck loops burn $15 before pause → lower. Real long-running tasks legitimately exceed → raise. After 10+ live D3.5 runs, retune to actual p95 cost × 2. |
| `DEFAULT_MAX_REVISIONS` (handler default) | `scripts/mirror_review_handler.py` | 3 | 1–5 | Should match `loop_bounds.max_revisions`; drift between the two is a bug. |
| `mirror_marker_self_validate_retries` | `config/agent-models.json` `loop_bounds` | 2 | 0–4 | mirror-marker-self-validate-gate-001. Caps the SAME-PROCESS verdict-marker re-prompt loop in `inbox_watcher.process_task` for phase=review Mirror dispatches. Mirror keeps emitting malformed verdicts past 2 in-process corrections → raise. Gate never fires / cross-process marker-error notifies vanished → lower toward 0 (0 disables, notifier net is the outer backstop). |
| `forge_preflight_marker_self_validate_retries` | `config/agent-models.json` `loop_bounds` | 2 | 0–4 | forge-preflight-marker-self-validate-gate-001. Caps the SAME-PROCESS preflight-marker re-prompt loop in `inbox_watcher.process_task` for phase=preflight Forge dispatches (prose-verdict-no-block on dense specs). Forge still lands prose past 2 in-process corrections → raise. MalformedForgeMarker preflight notifies vanished → lower toward 0 (0 disables, outbox_notifier cascade is the outer backstop). |

---

## Watchdog (infra health)

| Dial | Location | Current | Range | Retune trigger |
|---|---|---|---|---|
| Disk critical / warning | `scripts/watchdog.py` | 90% / 80% | 70–95% / 60–90% | Disk fills faster than expected → lower warning. False-alarm warnings at 85% → raise warning to 88%. |
| System memory critical / warning | `scripts/watchdog.py` | 90% / 80% | 70–95% / 60–90% | Memory pressure rising → lower warning. Steady ~85% normal → raise warning. |
| `inbox-watcher` RSS hard threshold | `scripts/watchdog.py` | 1.5 GB | 0.5–2.0 GB | Watcher legitimately needs more memory (more concurrent claude subprocesses) → raise toward MemoryMax (2 GB). Headroom shrinks → lower. |
| `inbox-watcher` cgroup % thresholds | `scripts/watchdog.py` | 95% / 80% of MemoryMax | 60–98% / 50–90% | cgroup hits 95% often → lower to alert sooner. Watchdog noise without real overrun → raise. |

---

## Larry-alerts cooldowns

| Dial | Location | Current | Range | Retune trigger |
|---|---|---|---|---|
| Critical-severity cooldown | `scripts/larry_alerts.py` | 10 min | 5–30 min | Critical alerts flood phone (same subject re-firing) → raise. Real critical events get swallowed by cooldown → lower. |
| Warning-severity cooldown | `scripts/larry_alerts.py` | 60 min | 15–240 min | Warning floods → raise. Important warnings get swallowed → lower. Dial 3 pick per Q8 D3.5-prep signoff. |

---

## Beacon-bot reminder + alert sweep

| Dial | Location | Current | Range | Retune trigger |
|---|---|---|---|---|
| `REMINDER_INTERVAL_SEC` | `scripts/beacon_telegram_bot.py` | 300 sec (5 min) | 60–900 sec | Alert-to-DM latency too high (worst case ~10 min) → lower. Bot noise / cost → raise. |

---

## Dispatch sentinel (stall detection)

| Dial | Location | Current | Range | Retune trigger |
|---|---|---|---|---|
| Sweep cadence | `systemd/ourliberty-dispatch-sentinel.timer` | every 10 min | 5–30 min | Stalls detected too late → lower. Sweep is heavy / noisy → raise. |
| Stale-lease age threshold | `scripts/dispatch_sentinel.py` | (check file) | — | Legitimate long-running dispatches get flagged as stalls → raise. Real stalls go undetected → lower. |
| Inbox-task unpicked threshold | `scripts/dispatch_sentinel.py` | (check file; smoke-tested at 4h) | 30 min – 24 h | Legitimate slow agents flagged → raise. Real stuck tasks linger → lower. |

---

## Pulse autonomous /cycle

| Dial | Location | Current | Range | Retune trigger |
|---|---|---|---|---|
| /cycle cadence | `systemd/ourliberty-cycle.timer` | every 4h | 1h – 24h | Cycles repeatedly find nothing new → raise (lower cost). Real signals missed because cycle cadence too slow → lower. |
| Pulse `inbox_model` | `config/agent-models.json` agents.pulse | `claude-sonnet-4-6` | sonnet / opus | Cycles need deeper analysis on recurring patterns → escalate to opus for those specific cycles (Pulse can self-escalate mid-task). |
| Open Forge-PR digest threshold | `scripts/run_cycle.sh` or agents/pulse prompt | 24h (will become 72h after 5d ships auto-merge) | 12h – 168h | Auto-merge keeps PRs flowing → raise. PRs sit too long without surfacing to Larry → lower. |

---

## Worktree retention

| Dial | Location | Current | Range | Retune trigger |
|---|---|---|---|---|
| `cleanup-stale-worktrees` age threshold | `systemd/ourliberty-cleanup-stale-worktrees.service` (`+24h` arg) | 24 h | 6 h – 7 d | Worktrees sit through legitimate multi-day dispatches → raise. Disk pressure from orphaned worktrees → lower. |
| `cleanup-stale-worktrees` cadence | `systemd/ourliberty-cleanup-stale-worktrees.timer` | daily | 4 h – weekly | — |

---

## Healer cadences (D2.5 self-healing)

The 7 healers each run on their own timer. The cadence dial for each:

| Healer | Cadence | Range |
|---|---|---|
| `heal-abandoned-inbox-tasks` | every 10 min | 5–30 min |
| `heal-blocked-inbox-age` | every 15 min | 5–60 min |
| `heal-empty-inbox-files` | every 15 min | 5–60 min |
| `heal-recovery-already-merged` | every 5 min | 5–30 min |
| `heal-restart-dedup-obsolete` | every 5 min | 5–30 min |
| `heal-silent-loop-death` | every 10 min | 5–30 min |
| `heal-zombie-main-workers` | every 5 min | 5–30 min |

Retune trigger for all: healer fires repeatedly on the same artifact → root-cause the upstream bug; the healer is a symptom-cleaner, not a permanent fix. Healer never fires → consider lowering cadence (cheap) or deleting if the underlying failure mode is gone.

---

## Periodic review cadence

**Quarterly review.** Once a quarter, walk through the table top-to-bottom. For each row:

1. Has this dial fired in production over the past 90 days? Check the relevant log (`~/agents/blackboard/larry-alerts.jsonl`, `~/agents/logs/outbox-notifier.log`, agent activity journals).
2. If it fired and the action it triggered was correct → leave alone.
3. If it fired but the action was wrong (false positive / false negative) → retune one increment in the indicated direction. Don't retune by more than one increment at a time without a specific reason; small adjustments are reversible.
4. If it never fired and you can't think of when it would → consider whether the dial is still load-bearing or whether the underlying failure mode is gone. If the latter, document and consider deleting.

**Monthly skim.** Once a month, read the table — no action required, just orient yourself to what's tunable and where. The act of re-reading catches "I forgot we had a knob for that" failure mode.

**Eventual:** a small `tunables status` script that prints current values from the live config files + grep-scrapes constants from .py files. Phase F+ candidate (tracked). Goal: this doc becomes the spec; the script becomes the read-the-current-state surface.

First scheduled review: 2026-08-13.

---

## Change log

Append-only. When you retune a dial, add a row here with date, dial name, old → new, and 1-2 sentence rationale.

| Date | Dial | Change | Rationale |
|---|---|---|---|
| 2026-05-13 | `max_revisions`, `max_replans`, `cost_per_task_usd` | (new) → 3, 2, 5.0 | D3.5 commit 5a introduced `loop_bounds`. Initial values are the plan's recommendations; revisit after 10+ live D3.5 runs. |
| 2026-05-13 | `DEFAULT_MAX_REVISIONS` (handler) | (new) → 3 | Matches `loop_bounds.max_revisions`. Tracked separately because the handler has its own default for when an envelope omits the field. |
| 2026-05-13 | Doc created | — | First version of this index. Populated from the dials in active use as of 5a ship. |
| 2026-05-13 | `max_revisions` | active in 5b | D3.5 5b lit the revision loop. Enforced: REVIEW_REVISION at revision_count+1 > max_revisions downgrades to ESCALATE (no new vocabulary; reuses Beacon's escalate handler). Re-eval after 10+ live revision rounds — if Forge consistently converges in 1-2, lower to 2; if 3 rarely enough, raise OR sharpen the spec design. |
| 2026-05-15 | `cost_per_task_usd` | $5 → $15 | Ledger build dispatch (build-ledger-001) spans 12+ files + spec read; $5 cap risked mid-build trip. $15 gives headroom without removing gate. |
