# gm-agent-core Upstream Audit

**Audit date:** 2026-05-11
**Upstream HEAD audited:** `55a8e17` (2026-05-11 06:04:25 -0500) — `feat(plan-firewall): FULL-ROLLOUT RULE — never <100 rollout in any plan (#246)`
**Larry's mirror last-synced commit:** `0ef7e9f` (2026-05-05) per memory — six post-2026-05-05 commits land between mirror and current upstream HEAD.
**Larry's fork HEAD:** `ec16055` (2026-05-11 16:16:43 -0600) — `docs/operating-manual: rewrite for Phase D2 + add Part II build narrative`

---

## Executive summary

Joe's upstream gm-agent-core is a **two-and-a-half-year-old, deeply battle-tested multi-agent orchestration system** with ~38,000 lines across 344 audited files in `scripts/`, `agents/`, `runbooks/`, `shared/`, `config/`, and `systemd/`. The architecture is Python-and-bash orchestrating `claude --print` subprocess pools, with file-based handshake (inbox JSON → outbox JSON), per-agent personas under `agents/<id>/`, and an elaborate self-healing constellation (15 healers, dispatch leases, dead-letter archives, restart-safe ledgers, dispatch sentinels). Almost every defensive primitive in upstream traces to a specific dated incident (e.g., the parent-CLAUDE.md poison guard exists because a stale `/tmp/CLAUDE.md` on 2026-04-15/16 silently re-identified every worker as Prism). The whole thing is run by a single 2,483-line `orchestrator.py` that calls into a 1,140-line `agent_runner.py` for the actual claude invocation.

Larry's fork is ~10,500 lines across 56 files (27% of upstream). Larry kept the **dispatch primitives** (HANDSHAKE-SCHEMA, dispatch_lease, dispatch_validator, dispatch_dedup_guard, concurrency_guard, await_quiescence), the **identity hardening machinery in `agent_runner.py`** (parent-CLAUDE.md guard, /tmp landmine scrubber, identity assertion preamble, worktree preamble idempotency), and seven healers. Larry **dropped** the GM customer-facing surface entirely (council protocol, sweep-ledgers, ship-tracking, plan-templates, video tooling, GitHub merge gates, telegram webhook business logic, multi-account token manager). Larry built **one new thing**: a 367-line `inbox_watcher.py` thread-per-agent watcher that replaces the orchestrator+agent_runner ensemble — and that is the design decision this audit must adjudicate.

**Top findings that change the build plan:**

1. **`agent_runner.py` carries six pieces of defense-in-depth machinery that `inbox_watcher.py` does NOT have**: parent-CLAUDE.md poison quarantine, /tmp identity-landmine scrub on every spawn, identity assertion preamble (when `expected_agent` is set), graceful cancellation via blackboard markers, MAX_RETRIES=5 with exponential backoff (10→20→40→80→160s), and an in-flight registry (`state/in-flight/`) that survives orchestrator restarts (orphan adoption). The thin watcher has none of these. In a 4-agent topology these matter less *per task* but they matter MORE in aggregate because Larry has no human-in-the-loop noticing silently mis-routed work the way Joe does in real time.
2. **Larry's mirror is 8 commits behind current upstream HEAD** (2026-05-06 through 2026-05-11). Two of those are load-bearing for Larry: `heal_pr_auto_merge.py` (new healer, #240) and a *significantly* rewritten cycle-prompt with V2-customer-readiness program direction. The rest are GM-specific runbook directives Larry can skip.
3. **Upstream has a working **dispatch_lease** + **concurrency_guard** + **await_quiescence** ensemble** that Larry has pulled but is NOT actually using in his thin watcher (the watcher acquires leases per-agent but does not share with the runner). The full ensemble is what gives upstream the restart-safety + cross-process-correctness Larry's design needs for D3 (Beacon ↔ Pulse dialogue).
4. **The `-result` source convention is the upstream pattern for inter-agent result feedback** (e.g., `sage-result` writes back to Sage's inbox so she sees what Luma did). It is fully implemented in `orchestrator.process_outbox_notifications`. Larry's fork preserves the enum in `dispatch_validator.ALLOWED_SOURCES` but has no code that produces or consumes `*-result` files — meaning D3's Beacon→Forge feedback loop has the convention available but no plumbing.
5. **The `council-*` source values are NOT a generic approval-gate**; they're specific to GM's 5-phase `/plan` review choreography. Larry should **not** adapt them for the Beacon→Larry approval-DM gate — that needs a purpose-built `pending-approvals` state file (already specified in the 2026-05-09 handoff doc). However, upstream's `ship_completion_watcher.py` *milestone bypass* pattern (line ~1834 in `orchestrator.py`: detects "PR merged" / "shipped" in agent output and routes a synthesized milestone directly to Atlas for voicing) IS the right blueprint for the Beacon→Larry "done" DM in Phase D4.

The default recommendation throughout: **adapt and use** upstream rather than rebuild. Joe spent six months learning what breaks. The cost of unlearning that by greenfield is much higher than the cost of one careful adaptation pass per primitive.

---

## Section 1 — Upstream component inventory

Sorted by directory, then alphabetically. "Status" reflects Larry's fork as of audit date.

### scripts/

| Path | Lines | Purpose (1-2 sentences) | Larry's fork status | Recommendation |
|---|---|---|---|---|
| `add_repo_sync_override.sh` | 26 | One-shot override marker that suppresses a single `repo_sync.sh` divergence alert when Joe knowingly accepts drift. | Not pulled | Skip (GM-specific) |
| `agent_core_health_check.py` | 346 | Hourly systemd-driven repo-discipline enforcer for `gm-agent-core`: branch=main, clean tree, in-sync with origin, sync-script ran < 6h ago. Alerts via Prism on issues; auto fast-forwards behind-only case. | Adapted (paths joe→larry, prism→pulse-or-direct) | Keep as-is — already in fork at parity |
| `agent_health.py` | 105 | Reads each agent's `.log` file and reports success-rate per window. Returns JSON or human-readable line. | Adapted | Keep as-is |
| `agent_runner.py` | 1140 | **The orchestrator's claude-spawning core.** Concurrency guard, retry/backoff, parent-CLAUDE.md poison quarantine, /tmp identity-landmine scrubber, identity-assertion preamble, worktree creation+branch checkpoint+preamble idempotency, cancel-marker polling, in-flight registry for restart-safe orphan adoption, sweep-ledger lesson injection, JSON-fallback parsing. Called by orchestrator + telegram_bot. | Pulled with 3 known bug lines (501/694/936) and stub token_manager | Adapt and use — the D2 watcher should call into this rather than parallel it (see Section 5) |
| `apply_proposal.py` | 294 | Reads a Prism proposal JSON and mechanically applies the edits to live files; supports dry-run + diff preview. | Not pulled | Skip — Prism agent dropped |
| `atlas-outbound-call.sh` | 35 | VAPI dialer for Atlas voice channel. | Not pulled | Skip (GM-specific) |
| `atlas-post-call-dispatch.sh` | 142 | Post-VAPI-call ingestion + Sage dispatch with call transcript. | Not pulled | Skip (GM-specific) |
| `atlas-vapi-call.sh` / `atlas-vapi-refresh-prompt.sh` | 50/194 | VAPI session lifecycle. | Not pulled | Skip (GM-specific) |
| `atlas_activity_window.py` | 149 | Computes Atlas's allowed voice-call window from rules JSON. | Not pulled | Skip |
| `atlas_plan_tracer.py` | 294 | Traces a plan's lineage across Atlas/Sage/Luma artifacts. | Not pulled | Skip |
| `atlas_voice_handler.py` | 148 | VAPI webhook → Atlas inbox bridge. | Not pulled | Skip |
| `audit_founder_comms.py` | 223 | Daily summary of every Joe-facing Telegram message (founder-tone audit). | Not pulled | Adapt later (Phase D5+) — useful for "did Beacon DM Larry today" |
| `audit_logger.py` | 173 | Append-only audit log for agent actions, separate from operational logs. | Not pulled | Adapt and use — Phase D handoff packages need this |
| `auto_pr_opener.py` | 196 | Watches Luma's outbox for "push complete" outputs, opens PR if not opened. | Not pulled | Adapt later — D5 (when Forge starts opening PRs) |
| `await_quiescence.py` | 189 | Blocks until all inboxes empty + no claude procs + no concurrency-guard slots — pre-sync safety. | Adapted (paths joe→larry) | Keep as-is |
| `backlog_promoter.py` | 407 | Hourly cron promoting `state/backlog/*.json` items into Sage's inbox when capacity allows. | Not pulled | Adapt later — D5+ (Compass agent territory) |
| `bootstrap_heartbeat.sh` | 41 | Once-on-boot heartbeat write. | Not pulled | Pull from upstream (trivial, useful for watchdog) |
| `check_merge_dm_gap.py` | 227 | Detects merges that happened without a corresponding Joe DM and emits late ack. | Not pulled | Skip until Beacon-merges-DM-Larry pattern is live (D4) |
| `cleanup_stale_worktrees.py` | 123 | Removes `/tmp/wt-*` worktrees > 24h old. | Not pulled | Pull from upstream — needed once Forge starts using worktrees |
| `concurrency_guard.py` | 121 | File-locked global semaphore (max 10 concurrent claude processes), single-file JSON state + dead-PID cleanup. | Adapted (paths joe→larry) | Keep as-is |
| `cost_per_pr_dashboard.py` | 110 | Rolls up `state/cost-records.jsonl` by PR. | Not pulled | Adapt — Larry already has `costs.jsonl` (D2); pulling this gives the per-task aggregator for free |
| `council_overlap_detector.py` | 310 | Flags overlapping council reviews. | Not pulled | Skip (GM-specific) |
| `council_script_checks.py` | 255 | Static checks for council-protocol compliance. | Not pulled | Skip |
| `council_watchdog.py` | 206 | Watchdog for council-flow stalls. | Not pulled | Skip |
| `craft_plan_prompt.py` | 303 | Builds the long-form plan prompt for `/plan` invocations. | Not pulled | Skip — Beacon does this with her own spec template |
| `cron_runner.sh` | 240 | Generic cron wrapper: lockfile, log redirect, kill-switch check, exec script. | Not pulled | Pull from upstream — useful once Pulse spawns more crons |
| `daily_merge_digest.py` | 190 | Once-daily summary of all PR merges. | Not pulled | Skip until merge volume warrants it |
| `dead_letter_alarm.py` | 193 | Watches `blackboard/undelivered-replies.jsonl` and alarms Joe via Atlas if > N undelivered in window. | Not pulled | Adapt later — needed when Telegram delivery becomes async (D4+) |
| `deploy-component-b.sh` | 98 | One-shot deployer for a specific GM component. | Not pulled | Skip (GM-specific) |
| `dispatch_audit.py` | 201 | Adversarial post-merge audit — Sage compares PR diff against plan acceptance criteria, dispatches recovery if under-delivered. | Not pulled | Adapt later — D5+ (Mirror's role expansion) |
| `dispatch_dedup_guard.py` | 136 | Pre-write dedup: filename-chain depth limit + prompt-hash dedup against `dispatch_ledger.jsonl`. | Adapted (paths joe→larry) | Keep as-is — but verify the ledger writer side is wired (see gap §3) |
| `dispatch_lease.py` | 279 | Atomic file-leases (flock + nonce + boot-id + TTL + kill-before-reclaim). Heartbeat daemon thread. Three modes via `GM_DEDUP_USE_LEASES`: off / shadow / authoritative. | Adapted (paths joe→larry) | Keep as-is — this is the load-bearing concurrency primitive |
| `dispatch_manifest.py` | 289 | Manifest tracker for which sub-tasks of a multi-task plan have been dispatched. | Not pulled | Skip until multi-task plans are live (D5+) |
| `dispatch_sentinel.py` | 430 | Every 10 min cron: scans every inbox for tasks stuck > 3h, alerts founder once, tracks per-task state. Reads dispatch-leases (Wave 3). | Not pulled | **Adapt and use** — the thin watcher does NOT have a stall detector |
| `dispatch_validator.py` | 118 | Pre-dispatch validation: prompt length, source enum, reply_chat_id int, task_id present, timeout bounds. | Adapted (sources rewritten: gm agents → beacon/forge/mirror/pulse + aide/scout/compass) | Keep as-is |
| `dispatch_with_wireframes.py` | 190 | Wraps a Sage dispatch with Stitch wireframes attached. | Not pulled | Skip (GM-specific) |
| `dormant_issue_monitor.py` | 367 | Every 4h: GitHub issues with `luma-assigned` that haven't moved in N hours → alert. | Not pulled | Skip — Larry's pipeline doesn't use GitHub issues for routing |
| `exit_file_watchdog.py` | 199 | Detects `.exit-now` marker files and SIGTERMs the named process — surgical kill switch. | Not pulled | Pull from upstream (trivial, useful for /cycle on-demand stop) |
| `fathom-webhook-relay.py` | 238 | Fathom-call-recording webhook receiver. | Not pulled | Skip (GM-specific) |
| `generate_feature_scope.py` | 259 | Builds `shared/feature-scopes/<slug>.json` map for an existing feature. | Not pulled | Skip |
| `get-vercel-key.sh` / `set-vercel-env.sh` | 20/62 | Vercel CLI helpers. | Not pulled | Skip — only useful if Larry deploys to Vercel later |
| `github_mirror.sh` | 40 | Pushes a local mirror of repo state to a backup remote. | Not pulled | Optional — Phase F infra hygiene |
| `github_webhook_handler.py` | 418 | Receives GitHub webhooks (PR opened/merged/etc.) and writes to agent inboxes. | Not pulled | Adapt later — D5+ (when Forge actually opens PRs against real repos) |
| `gm_agent_core_branch_cleanup.py` | 99 | Cleans stale feature branches in `gm-agent-core` repo. | Not pulled | Adapt or skip — small enough to rewrite if needed |
| `guardian/` (10 files, 2765 lines) | — | Whole subsystem for tracking pinned-version drift of `agent-browser` and similar deps; release-check + rollback orchestration; dead-man's switch; aggregation/replay/shadow-run/dual-run-verify. | Not pulled | Skip for now — Guardian's role (pinned-external-deps tracking) doesn't exist in Larry's topology yet. Re-evaluate in Phase F when Mini Brains brings Vercel/Supabase pins |
| `heal_abandoned_inbox_tasks.py` | 213 | Every 10 min: tasks > 60 min old in inboxes/{main,sage} with no live lease + no live worker → rename with `-recovery-<ts>` suffix to bypass `submitted_tasks` cache. | Adapted (paths joe→larry) | Keep as-is |
| `heal_backlog_promoter_alive.py` | 100 | Detects if `backlog_promoter` cron is dead and re-enables. | Not pulled | Skip until backlog_promoter is in service |
| `heal_blocked_inbox_age.py` | 101 | Every 15 min: archives inbox `blocked/` files older than 48h. | Adapted (paths joe→larry) | Keep as-is |
| `heal_context_less_notify_result.py` | 182 | Detects notify-result files lacking the original task context and back-fills from ship-tracking. | Not pulled | Skip (GM-specific — needs ship-tracking) |
| `heal_core_branch_drift.py` | 109 | Detects `gm-agent-core` repo drifted off `main` and alerts. | Not pulled | Adapt or skip — `agent_core_health_check.py` already covers this for Larry |
| `heal_empty_inbox_files.py` | 177 | Every 15 min: archives 0-byte or empty-JSON inbox files. | Adapted | Keep as-is |
| `heal_frozen_features_inbox_firewall.py` | 139 | Inbox firewall around `frozen-features` flag. | Not pulled | Skip (GM-specific) |
| `heal_joe_inbox_stale.py` | 95 | Archives stale `inboxes/joe/` (pseudo-inbox for human msgs). | Not pulled | Skip (GM-specific) |
| `heal_manifest_reconcile.py` | 192 | Reconciles dispatch_manifest entries after a sweep. | Not pulled | Skip until manifest is in use |
| `heal_mirror_state.py` | 102 | Validates mirror-state consistency. | Not pulled | Skip |
| `heal_pr_auto_merge.py` | 197 | **NEW 2026-05-10 (#240).** Bridges disabled repo auto-merge by detecting MERGEABLE+green PRs with no auto-merge and enabling it. | Not pulled (post-mirror commit) | Pull from upstream when Forge starts opening PRs — load-bearing for the "Joe never merges; system handles 100%" pattern Larry wants |
| `heal_recovery_already_merged.py` | 130 | Every 5 min: archives `recover-*` tasks whose target PR already merged. | Adapted | Keep as-is |
| `heal_restart_dedup_obsolete.py` | 110 | Every 10 min: clears `_restart_safety_stems` entries that aged out. | Adapted | Keep as-is |
| `heal_silent_loop_death.py` | 149 | Detects self-scheduled re-queue loops that died silently. | Adapted | Keep as-is |
| `heal_zombie_main_workers.py` | 238 | Every 5 min: kills `claude` processes whose cwd is `(deleted)` (Pattern A) or whose worktree HEAD merged > 4h ago (Pattern B). | Adapted | Keep as-is — but Pattern B logic depends on a GH repo Forge isn't yet pushing to |
| `inbox_misroute_redirector.py` | 111 | Watches for misrouted inbox writes and redirects. | Not pulled | Skip — routing_validator covers this |
| `inbox_watcher.py` (upstream) | 126 | **NOT a task runner.** It's an inotify-based daemon that touches `blackboard/inbox-wake.flag` when a new file lands, so the orchestrator's poll loop can wake immediately instead of waiting POLL_INTERVAL. | Larry has a DIFFERENT 367-line `inbox_watcher.py` that IS the task runner (replaces upstream's orchestrator+agent_runner combination). | The two scripts share only a name. See Section 5. |
| `joe_block_lint.py` | 211 | Lints prompts/messages for "permission-asking" language that violates the agent's autonomy rules. | Not pulled | Adapt later — D4/D5 (Beacon's plan should not ask Larry for permission, just present approve/modify/reject) |
| `joe_response_sla_check.py` | 151 | Checks that Joe-DM SLAs are being met. | Not pulled | Skip until volume warrants |
| `joe_signal_curator.py` | 249 | Curates the daily signal-to-Joe stream. | Not pulled | Skip |
| `kill_duplicate_workers.py` | 121 | Detects parallel workers on the same task and kills the younger. | Not pulled | Adapt later — D5 (relevant once Forge runs in parallel worktrees) |
| `kill_switch.py` | 111 | EMERGENCY_HALT touch/remove + status. Orchestrator polls the flag. | Adapted | Keep as-is — but the watcher needs to be wired to check it (gap §3) |
| `ledger_sync.py` | 495 | Syncs `sweep-ledgers/` from per-feature JSON files. | Not pulled | Skip (sweep-ledgers GM-specific) |
| `luma_email.py` | 205 | Email sender for Luma agent. | Not pulled | Skip (GM-specific) |
| `merge_gates.py` | 1017 | The 5-gate PR auto-merge engine (code-reviewer verdict + mergeability + Vercel preview + CI + walkthrough label). | Not pulled | Adapt later — D5+ (Mirror gates + Forge auto-merge) |
| `merge_gates_daily_report.py` | 126 | Daily report on which PRs cleared which gates. | Not pulled | Skip until merge_gates is live |
| `merge_watcher.py` | 740 | Every-minute cron that runs `merge_gates`-style checks and squash-merges qualifying PRs. | Not pulled | Adapt later — D5+ |
| `meta_watcher.py` | 291 | Watches the system for higher-order anomalies (agents stuck on the same prompt across runs, etc.). | Not pulled | Adapt later — D5+ |
| `metrics_collector.py` | 296 | Periodic metrics gather → `blackboard/metrics.json`. | Not pulled | Adapt later — needed for Ledger (Phase F) |
| `monitor_dashboard.py` | 181 | Reads metrics + statuses, renders a TUI/HTML dashboard. | Not pulled | Adapt later — D5+ |
| `notion_client.py` | 234 | Notion API wrapper. | Not pulled | Skip (GM-specific) |
| `orchestrator.py` | 2483 | **The main daemon.** Parallel inbox processing with ThreadPoolExecutor (10 workers). Dedup, lease, routing-validator, schema-validation, dispatch-blocked enforcement, message filter (founder-tone), Telegram reply with bot-fallback, requeue + exponential backoff, dead-letter archival, ship-tracking propagation, notify-cascade depth limiter, milestone bypass, post-merge auto-iterate, content-similarity dedup, restart safety from ledger, memory backpressure, founder-vision priority bypass, zombie reaper, subreaper prctl, lease startup-sweep, orphan adoption, EMERGENCY_HALT, continuation registry. | **NOT pulled** — Larry replaced the entire thing with `inbox_watcher.py` (367 lines). | See Section 5 — recommendation is to migrate D3 work onto `agent_runner.py` (with the three known fixes) and treat orchestrator.py as a reference for which protective layers to copy in (memory backpressure, EMERGENCY_HALT poll, milestone bypass, requeue/dead-letter, lease + heartbeat). NOT to wholesale adopt 2483 lines. |
| `patch_repo_sync_silence.py` | 86 | Manages the override marker `add_repo_sync_override.sh` writes. | Not pulled | Skip |
| `patch_safely.py` | 128 | Wraps a code patch with backup + dry-run preview. | Not pulled | Adapt later — useful for Forge in worktrees |
| `pipeline-health.sh` | 92 | Pipeline-health snapshot. | Not pulled | Skip until pipeline (Sage's plan→ship flow) exists |
| `pipeline_sentinel.py` | 420 | Watches the plan→ship pipeline for stalls. | Not pulled | Skip |
| `pipeline_watcher.py` | 686 | Same pipeline domain. | Not pulled | Skip |
| `polish_sweep_trigger.py` | 611 | Triggers polish sweeps for V2 clusters. | Not pulled | Skip (GM-specific) |
| `post_incident_reconcile.py` | 172 | Post-incident reconciliation report generator. | Not pulled | Adapt later — Phase F (Ledger) |
| `post_merge_verifier.py` | 478 | Every 20 min: dispatches Playwright verification tasks to Luma for merged PRs against production. | Adapted (paths joe→larry, REPO + PRODUCTION_URL env-configurable, TODO for which-agent-verifies) | Keep as-is — already adapted; defer activation until Forge ships against a real product URL |
| `pr_routing.py` | 168 | Extracts PR-to-chat routing from agent output and stores it for future GitHub webhook events. | Not pulled | Adapt later — D5+ |
| `prism_notify_joe.py` | 137 | Tier-colored notification dispatcher (`--tier green/yellow/red/breakdown`) → writes to Atlas inbox + filesystem markers. | Not pulled | Adapt and use — Larry needs a notification tier system for Beacon→Larry and Pulse→Larry. Adapt Prism's tier semantics, route to Beacon/Pulse-via-Larry-direct |
| `prism_signal_aggregator.py` | 303 | Aggregates Prism signals for weekly read. | Not pulled | Skip |
| `prism_weekly_aggregator.py` | 193 | Weekly Prism aggregator. | Not pulled | Skip |
| `prop_attribution_tracker.py` | 216 | Attributes properties to specific agents. | Not pulled | Skip |
| `queue_parallel_dispatch.py` | 190 | Queues N parallel dispatches with backpressure. | Not pulled | Adapt later — D5+ |
| `reconcile_issues.py` | 284 | GitHub issue reconciliation. | Not pulled | Skip until issue-driven dispatch is live |
| `reconciliation_scan.py` | 370 | Daily 07:00 scan for plan-gist drift. | Not pulled | Skip (GM-specific) |
| `record_issue_origin.py` | 52 | Records originating chat_id for each new issue. | Not pulled | Skip |
| `repair_sweep_ledgers.py` | 135 | Sweep-ledger repair. | Not pulled | Skip |
| `repo_sync.sh` | 354 | Source-side: pushes a clean working tree of `gm-agent-core` back to origin. | Not pulled | Adapt later — D5+ (when Larry's agents start committing to the repo) |
| `resurrect_orphan_workers.py` | 135 | Pre-start hook for orchestrator: re-adopts workers from a prior boot. | Not pulled | Adapt and use — load-bearing for the agent_runner.py adoption path |
| `retire_wave2_3_autopilot.sh` | 29 | Wave-2/3 autopilot retirement script. | Not pulled | Skip |
| `routing_audit_summary.py` | 138 | Weekly summary of `agent-routing-audit.log`. | Not pulled | Adapt later — D5+ |
| `routing_validator.py` | 261 | Reads `## Routing Constraints` section of each agent's IDENTITY.md and enforces accepts_from_user / accepts_task_types / escalation_target at every inbox write. | Not pulled | **Adapt and use** — Larry's 4-agent set has clear role boundaries (Pulse never builds; Mirror never plans). Encoding those constraints in IDENTITY.md and enforcing at write-time would prevent the kind of role-bleed where someone DMs the wrong agent. |
| `safe-tmp-prune.sh` | 32 | Safe /tmp prune (excludes worktrees). | Not pulled | Pull from upstream — trivial, useful |
| `schema_strict_flip.py` | 158 | Flips HANDSHAKE schema validation from permissive→strict after a quiet period. | Not pulled | Adapt later — D5 (after dispatch_validator has run a few weeks clean) |
| `sentry_healer.py` | 94 | Sentry-driven healer dispatcher. | Not pulled | Skip until Sentry is wired |
| `session_manager.py` | 201 | Thread-safe, atomic, flock-protected `sessions.json` store: per-(agent_id, chat_id) Claude session id + per-task session_id. 24h staleness TTL. | Not pulled | Pull from upstream — Larry's `<agent>_telegram_sessions.json` is per-bot. Upstream's design unifies them with TTL + atomicity. Pull on D3 when sessions matter more |
| `ship_completion_watcher.py` | 987 | Every 30 min: enforces "100% built 100% of the time always" — per-sub-task PR reconciliation, redispatch on stale, escalate to Joe on N retries, terminal-state guard. Memory + concurrency backpressure. | Not pulled | Adapt later — D5+ (this is GM-flavored ship-tracking; Larry's prototype paths don't have ship-tracking yet) |
| `ship_dispatch.py` | 587 | The Sage-side dispatcher that builds the ship-tracking JSON + writes Luma's inbox task. | Not pulled | Skip (Sage role; Beacon handles this for Larry) |
| `ship_notifier.py` | 369 | Emits structured ship-event notifications. | Not pulled | Adapt later |
| `sweep_coverage_debt.py` | 190 | Tracks sweep coverage debt. | Not pulled | Skip |
| `sweep_ledger_semantic_dedup.py` | 291 | Semantic dedup of sweep-ledger entries. | Not pulled | Skip |
| `sweep_lessons_digest.py` | 295 | Weekly digest of sweep lessons. | Not pulled | Skip |
| `sweep_status.py` | 326 | Per-feature sweep status snapshot. | Not pulled | Skip |
| `sync_agent_core.sh` | 343 | Atomic-swap sync from `gm-agent-core` repo into live VM `gm-agents/`. Quiescence-aware, never touches inboxes/outboxes/blackboard/telegram/logs/memory. Status JSON + alert-Joe on failure. | Adapted (paths joe→larry, repo name gm-agent-core→ourliberty-agent-core) | Keep as-is |
| `telegram_bot.py` | 883 | Multi-bot polling Telegram bridge with thread-pool, debounced send (3s), per-bot rate-limit, chat-history per `bot_id:chat_id`. Each message spawns claude via `agent_runner.run_claude`. | Not pulled directly — Larry built `beacon_telegram_bot.py` + `agent_telegram_bot.py` instead | Keep Larry's design — it's simpler. But pull upstream's *debounce* (3s collect-before-process) idea once Larry sees rapid-message bursts |
| `telegram_webhook.py` | 3076 | Webhook receiver (the alternative to polling). Massive: routing classifier, sweep dispatch, council orchestration, Ember calendar, founder-vision detection, conversation memory, response sender. | Not pulled | Skip — webhook is not Larry's chosen path (he polls) |
| `test_plan_v4_acceptance.py` | 312 | Acceptance tests for plan v4 protocol. | Not pulled | Skip |
| `test_reconciliation_fixture.py` | 206 | Test fixture for reconciliation scans. | Not pulled | Skip |
| `tests/` (6 files) | ~600 | Test harnesses: identity-landmine scrub, parent-CLAUDE-md guard, prompt builder, repo_sync, routing hardening. | Not pulled | **Adapt and use** the identity-landmine and parent-CLAUDE-md tests — they protect machinery Larry already pulled. Skip the others. |
| `tmp_landmine_sweep.sh` | 51 | Daily 03:00 sweep of /tmp identity landmines. | Not pulled | Pull from upstream — pairs with `scrub_tmp_identity_landmines()` in agent_runner |
| `token_manager.py` | 359 | Dual-OAuth-token manager: rate-limit detection from claude output, automatic failover, per-account cooldowns. Distinguishes usage-cap (1h cooldown) from transient 429 (5min). | Not pulled — Larry replaced with a stub in agent_runner | Adapt later — when Larry adds a second OAuth account or wants smarter rate-limit detection than the watcher's "wait and retry" |
| `triage_dispatch.py` | 186 | Sage's `/triage` command implementation. | Not pulled | Skip (Sage role) |
| `typecheck-hang-killer.sh` | 19 | Kills `tsc` processes stuck > N minutes. | Not pulled | Skip until Forge runs TS builds |
| `validate_agent_core.py` | 284 | Pre-sync validation: HANDSHAKE-SCHEMA is valid JSON Schema, required agent files exist, no secret patterns, .gitignore has required entries. | Adapted (allowlist trimmed, secret patterns kept) | Keep as-is |
| `validate_contract.py` | 265 | Validates that a feature contract / interface is met. | Not pulled | Skip until contracts are formalized |
| `validate_plan_schema.py` | 101 | Validates plan templates conform to PLAN-TEMPLATE schema. | Not pulled | Skip |
| `video_notifier.py` | 293 | Video pipeline notifications. | Not pulled | Skip |
| `watchdog.py` | 615 | 8-check system health monitor every 2 min: orchestrator alive, telegram-webhook alive, disk < 90%, memory < 90%, inbox stale tasks, log growth, token-manager status, all systemd services active. Auto-restarts. | Adapted (services renamed) | Keep as-is — but the 8 checks reference orchestrator+webhook services Larry doesn't run. Need to update the SERVICE_LIST in fork once D3 stabilizes which units exist |

### agents/

| Path | Lines | Purpose | Larry's fork status | Recommendation |
|---|---|---|---|---|
| `agents/atlas/` (6 files) | ~600 | CEO persona: VAPI voice config, decision framework, SOUL/IDENTITY/CLAUDE/TOOLS. | Not pulled | Skip (GM-specific) |
| `agents/ember/` (9 files) | ~800 | Calendar/EA persona. | Not pulled | Read for reference when Aide is built (Phase E) |
| `agents/luma/` (13 files) | ~3500 | Engineer/CTO persona: includes DEV-MANAGER, AUTH-RULES, BOOTSTRAP, HEARTBEAT, ACTIVE-TASKS. | Not pulled | **Read for reference** — Forge's spec analog. The DEV-MANAGER doc and AUTH-RULES are the most transferable patterns |
| `agents/mula/` | ~12 files | Kelly's assistant (sensitive scoping; restricted access). | Not pulled | Skip |
| `agents/nova/` (~20 files including artifacts) | ~5000 | CRO persona + a lot of plan artifacts in-tree (presentations, marketing calendars). | Not pulled | Skip the artifacts; read NOVA-IDENTITY for the role-boundary pattern |
| `agents/prism/` (4 files) | ~200 | Meta-observer; proposal author. | Not pulled | Read for reference — Pulse is closest analog |
| `agents/sage/` (~25 files, ~10K lines) | ~10000 | COO / Mission Command. Heavy plan-archive contamination. | Not pulled | Read SHIP-ORCHESTRATION, TRIAGE-PIPELINE, MESSAGING for transferable patterns |

### runbooks/

| Path | Lines | Purpose | Larry's fork status | Recommendation |
|---|---|---|---|---|
| `runbooks/concierge-production-setup.md` | ~200 | Concierge setup runbook. | Not pulled | Skip (GM-specific) |
| `runbooks/cycle-actions.jsonl` | append-only | Append-only audit log of every cycle's auto-fix action. | Pulled (empty in fork) | Keep — same convention |
| `runbooks/cycle-journal.md` | append-only | Chronological journal of every cycle iteration. | Pulled (empty in fork) | Keep |
| `runbooks/cycle-prompt.md` | very large (~30KB) | The canonical `/cycle` spec. **Heavily rewritten 2026-05-06 to make V2 Customer-Readiness Program the top-level mission.** Includes mission, parallelization rules, defensive auto-merge scan, post-ship E2E protocol, full-rollout rule, user-reachability contract. | Adapted (Larry-flavored version for Pulse) | Keep Larry's version. Read upstream's iter-100+ rules (max-parallelization, defensive auto-merge scan) and pick the ones generalizable to Pulse's role |
| `runbooks/plan-output-template.md` | ~100 | Template for plan output. | Not pulled | Adapt — Beacon's spec template should reference this |
| `runbooks/triage-dispatch.md` | ~200 | Triage-pipeline runbook. | Not pulled | Skip (Sage role) |

### shared/

| Path | Lines | Purpose | Larry's fork status | Recommendation |
|---|---|---|---|---|
| `shared/AGENT-ROUTING-SCHEMA.md` | 122 | Schema for the `## Routing Constraints` block in IDENTITY.md. | Not pulled | **Pull and adapt** — Larry should encode beacon/forge/mirror/pulse role boundaries in their IDENTITY.md and let routing_validator enforce |
| `shared/AI-FIRST-TIMELINE-CONVENTION.md` | 115 | Date convention (the audit file uses `2026-` not `2024-` — this is GM's deliberate future-dating). | Not pulled | Read for context only |
| `shared/BOOTSTRAP-STATUS.md` | 48 | Bootstrap-completion tracking. | Not pulled | Skip |
| `shared/CHAT-OWNERSHIP.md` | 67 | Which Telegram chats are "owned" by which agent. | Not pulled | Pull and adapt — single Larry chat for now, but the pattern is right |
| `shared/COUNCIL-PROTOCOL.md` | 299 | The 5-phase `/plan` review choreography (Phase 0 reality → 1 contributions → 2 synthesis → 3 critique → 4 adversarial → 5 approval). | Not pulled | Skip for now — adversarial review is Mirror's role in Larry's topology, but the 5-phase council is overkill for Larry. Read only Phase 0 (reality grounding) as a pattern for Beacon's spec drafting |
| `shared/DESIGN-SYSTEM-BIBLE.md` | 413 | Visual/UX bible for GrowthMastery product. | Not pulled | Skip (GM-specific) |
| `shared/HANDSHAKE-SCHEMA.json` | 117 | The JSON-schema for inbox task envelopes. | Pulled (currently IDENTICAL except `$id` URL; the source enum has NOT been narrowed to beacon/forge/mirror/pulse) | **Update fork** — narrow `source` enum to beacon/forge/mirror/pulse + aide/scout/compass + system sources. The `dispatch_validator` is already narrowed but the JSON schema is not; this is a drift gap |
| `shared/IDEAL-CUSTOMER.md` | 96 | GrowthMastery's customer persona. | Not pulled | Skip |
| `shared/NORTH-STAR.md` | 65 | GM's mission. | Adapted (Larry's mission) | Keep |
| `shared/PIPELINE-ROLLBACK.md` | 369 | Multi-system rollback playbook. | Not pulled | Adapt later — D5+ |
| `shared/PIPELINE-RUNBOOK.md` | 121 | Pipeline observability runbook (dormant + reconciliation scans). | Not pulled | Skip |
| `shared/PLAN-*-TEMPLATE.md` (5 files) | ~400 total | Templates for plan artifacts. | Not pulled | Adapt — Beacon's spec template should look like a stripped PLAN-TEMPLATE |
| `shared/REPO-GUARDRAILS.md` | 94 | Authoritative table of which repos exist and who can write to which. | Adapted (Larry's repos + tier system) | Keep |
| `shared/SHARED-SOUL.md` | 91 | Common values across all agents. | Not pulled | Pull and adapt — Larry's agents currently have isolated SOUL.md files; a SHARED-SOUL provides cross-agent grounding |
| `shared/SHIP-PLAN-HEADER.md` | 48 | Standard ship-plan header. | Not pulled | Skip |
| `shared/STITCH-WIREFRAMES.md` | 110 | Wireframe tooling docs. | Not pulled | Skip |
| `shared/SWEEP-PROTOCOL.md` | 251 | Sweep-ledger protocol. | Not pulled | Skip (sweep-ledgers GM-specific) |
| `shared/VERIFICATION-REPORT.md` | 74 | Template for verification reports. | Not pulled | Adapt later — D5+ |
| `shared/brand-rules.md` | 198 | GrowthMastery brand rules. | Not pulled | Skip |
| `shared/triage-protocol.md` | 215 | Triage protocol for Sage. | Not pulled | Skip |
| `shared/video-storyboard-schema.md` | 305 | Video pipeline schema. | Not pulled | Skip |
| `shared/agent-requests/*.json` | small | Cross-agent request artifacts. | Not pulled | Skip |
| `shared/feature-scopes/_feature-paths.json` | small | Feature → file-paths map. | Not pulled | Skip until Larry has multiple in-flight features |
| `shared/sweep-ledgers/*.json` (~23 files) | ~mid | Per-feature sweep ledgers (lesson injection source for agent_runner). | Not pulled | Skip — adopting these would require building Larry's own feature taxonomy |

### config/

| Path | Lines | Purpose | Larry's fork status | Recommendation |
|---|---|---|---|---|
| `config/agent-models.json` | small | Per-agent model routing (telegram_model, inbox_model, fallback_model). | Adapted (beacon/forge/mirror/pulse + aide/scout/compass placeholders) | Keep — Larry's version is cleaner than upstream's (has $schema_version and _history) |
| `config/cron-jobs-reference.json` | medium | A snapshot of which cron jobs SHOULD exist (reference for an external scheduler). | Not pulled | Skip — Larry uses systemd timers, not external cron |
| `config/crontab.txt` | small | Joe's actual crontab. | Not pulled | Skip |
| `config/external-deps.json` | small | Pinned external versions for Guardian. | Not pulled | Skip until Guardian is in service |
| `config/telegram-config.json` | small | Per-bot Telegram tokens + chat allowlists. | Not pulled directly (Larry uses env vars) | Pull as a *secondary* read source — env vars OK for prod, JSON is easier to edit |

### systemd/

| Path | Purpose | Larry's fork status | Recommendation |
|---|---|---|---|
| `gm-orchestrator.service` | Runs `orchestrator.py` always-restart, with MemoryHigh=12G + OOMPolicy=continue + TasksMax=1024 + env vars baked in (with PLAINTEXT SECRETS — a violation flagged by Larry's profile rule) | Not pulled | **Don't copy directly.** Adapt for `inbox_watcher` or `agent_runner` (whichever D3 lands on). Memory limits and TasksMax are useful patterns. NEVER copy the inline `Environment=` secrets. |
| `gm-inbox-watcher.service` | Runs upstream's `inbox_watcher.py` (the inotify wake-toucher) — a tiny helper unit. | Larry has a unit by the same name but it runs Larry's *task-running* watcher. **Naming collision risk.** | Keep Larry's. Add a comment explaining the divergence from upstream |
| `gm-*.timer` (15+ timers) | Hourly/daily/2-min/5-min cron equivalents for healers, watchdog, sync, etc. | Larry has 4 timers (cycle, sync, health, watchdog) | Pull more timers as the underlying healers are activated — but only one at a time, and only after each healer has been observed running clean for 24h |
| `gm-heal-*.{service,timer}` (15 healer pairs) | Per-healer service + timer pair. | Larry has 7 healers pulled but no systemd units for them yet. | **Gap** — write systemd timer pairs for the 7 healers, modeled exactly on upstream's pair structure |

---

## Section 2 — Patterns and conventions

### Pattern A — The HANDSHAKE / dispatch ensemble

**Files (upstream):** `shared/HANDSHAKE-SCHEMA.json`, `scripts/dispatch_validator.py`, `scripts/dispatch_lease.py`, `scripts/dispatch_dedup_guard.py`, `scripts/dispatch_manifest.py`, `scripts/dispatch_sentinel.py`, `scripts/dispatch_audit.py`.

**Conceptual flow:**
1. **Pre-write** (`dispatch_validator.py`): the caller validates the task dict against the schema before writing. Checks prompt length [100, 50000] chars, source enum membership, task_id present, reply_chat_id is int-or-null, timeout in [60, 14400], priority in known enum. Returns `(ok, reason)`. Schema mode is configurable (`config/schema-validation-mode.json` → permissive/strict; orchestrator auto-flips to strict after 48h with zero warnings).
2. **Pre-write** (`dispatch_dedup_guard.py`): refuses two patterns: (a) filename-chain depth ≥ 3 (when Sage keeps re-prefixing dispatched filenames with timestamps each time she dedup-detects the prior one); (b) prompt-hash matching a dispatch in the last 5 minutes to the same agent (`logs/dispatch_ledger.jsonl`).
3. **Write**: `orchestrator.safe_write_inbox()` consults `routing_validator.validate_inbox_write()` — reads target agent's IDENTITY.md `## Routing Constraints` section, applies `accepts_from_user` / `accepts_task_types` / `escalation_target` rules. If task type doesn't match, reroutes to `escalation_target` (usually Sage). Always logs to `logs/agent-routing-audit.log`. Defense-in-depth — never blocks a write, just redirects.
4. **Acquire** (`dispatch_lease.py`): orchestrator acquires a lease on `<task_identity>` before spawning claude. Lease is a flocked-on-acquire file in `state/dispatch-leases/<identity>.lease`. Carries `{identity, holder_pid, boot_id, nonce, timestamp_created, timestamp_renewed}`. Heartbeat thread renews every 60s; TTL is 180s. If existing lease is stale (TTL expired or PID dead), SIGTERM-then-SIGKILL the holder before reclaiming (kill-before-reclaim). Three modes via `GM_DEDUP_USE_LEASES`: `off` (no-op), `shadow` (acquired + logged, but old in-memory `_running_tasks` dict is authoritative), `authoritative` (lease is the source of truth). Larry's fork is on default `shadow`.
5. **Run** (`agent_runner.run_claude`): concurrency_guard slot acquired (max 10), then `claude -p --output-format json --model <m> --permission-mode bypassPermissions --add-dir <root>` is spawned with stdin-piped prompt. start_new_session=True to detach from orchestrator. In-flight registry (`state/in-flight/<task_stem>.json`) records PID for restart-survival.
6. **Outbox**: result JSON written with `{task_id, source, success, output, timestamp, agent_id, worktree, reply_chat_id, wireframes_in_task}`. Source carries the original (so `-result` convention works downstream).
7. **Outbox processing** (`orchestrator.process_outbox_notifications`): for each result file, possibly auto-retry on fail (fix-pr-* class), possibly emit milestone-to-Atlas bypass, possibly write `notify-*` to the source agent's inbox if source is another agent (with depth limit 1 to prevent cascade), then send Telegram reply with bot-fallback (selects bot whose chat-health says it's reachable).
8. **Stall detection** (`dispatch_sentinel.py`): every 10 min, scans all inboxes for tasks > 3h old, alerts Joe once per task (state in `state/dispatch-sentinel.json` so we don't spam).

**Fork status:** Larry has 1, 2 (partial), 3 (NOT — routing_validator not pulled), 4 (pulled, configured `shadow` but the watcher only acquires `inbox:<agent>` per-agent — never the task-level lease the orchestrator uses), 5 (the watcher does this but without retry/cancel/in-flight registration), 6 (the watcher does this), 7 (NOT — the watcher does not produce notify-* cascades or auto-retry), 8 (NOT — no stall detector running).

**Recommendation:** D3 work should add — in order — `routing_validator` (cheap, high-leverage), `dispatch_sentinel` (cheap, high-leverage), then re-evaluate moving onto `agent_runner.py` for the run-claude step so that retry + in-flight + cancel + identity-assertion + worktree are all back in play.

### Pattern B — The `-result` source convention

**Files (upstream):** enum lives in `shared/HANDSHAKE-SCHEMA.json` (sources `atlas-result`, `sage-result`, `main-result`, etc.); convention is implemented in `orchestrator.process_outbox_notifications` (~line 1869).

**How it works:** When agent X processes a task whose source is *another* agent Y, the orchestrator constructs a `notify-` filename, writes it to agent Y's inbox with `source = X-result`, and embeds the original output as the new task's `prompt` (with depth tag `_notify_depth`). Depth-limited to 1 hop (`_notify_depth > 1` → drop with audit record). This is **how Sage learns what Luma did**: Luma's result lands in `outboxes/main/<task>-result.json` with `source: sage` (the original), orchestrator sees `source == sage` (another agent), writes a new task to `inboxes/sage/notify-<task>-result.json` with `source: main-result` and the output as the prompt.

**Fork status:** The source enum is preserved in `dispatch_validator.ALLOWED_SOURCES` (Larry renamed to beacon-result/forge-result/mirror-result/pulse-result + aide/scout/compass-result). The HANDSHAKE-SCHEMA enum in shared/ STILL has the gm-era enum (atlas-result/sage-result/etc.) and has NOT been narrowed for Larry's agents — drift gap. The plumbing (the bit that converts an outbox `-result.json` into a notify-* task in the *originating* agent's inbox) does NOT exist in `inbox_watcher.py`.

**Recommendation:** When D3 wires Beacon→Forge with feedback (Mirror's verdict needs to return to Beacon's session), the canonical pattern is *exactly this*. Adapt the upstream orchestrator's lines 1869–1947 (the notify-cascade writer with depth-limiter) into a small `outbox_notifier.py` or fold it directly into `inbox_watcher.py`'s post-task hook.

### Pattern C — `council-*` source values

**Files (upstream):** `shared/COUNCIL-PROTOCOL.md`, `scripts/orchestrator.py` (INTERNAL_SOURCES set), `scripts/dispatch_validator.py` (ALLOWED_SOURCES enum).

**How it works:** `/plan` is a 5-phase choreography where Sage dispatches contributions, syntheses, critiques, adversarial reviews, and approval phases to other agents in parallel. Each phase uses a specific source (`council-contribution`, `council-approval`, `council-adversarial`) and ALL of them are in `INTERNAL_SOURCES` so the founder never sees the choreography fire — only Sage's final synthesis DM.

**Fork status:** Enum preserved but no choreography in Larry's fork.

**Recommendation:** Don't reuse `council-*` for the Beacon→Larry approval gate. The semantic is "agents talking to each other silently"; Larry's approval gate is "agent asking a human." Use a new source value `larry-approval-pending` (or similar) and a new state file `agents/state/<agent>-pending-approvals.json` (already designed in 2026-05-09 handoff doc). Do read `COUNCIL-PROTOCOL.md` Phase 0 (reality grounding) as a pattern for Beacon's spec drafting — "ground in what's actually true today before proposing what to change" is transferable.

### Pattern D — Continuation / milestone source values

**Files (upstream):** `scripts/orchestrator.py` lines ~2304–2326 (CONTINUATION REGISTRY), lines ~1322–1363 (START_MILESTONE), lines ~1808–1867 (MILESTONE_DIRECT_TO_ATLAS bypass).

**How it works:** A task can write a "continuation" — a JSON file in `state/continuations/` with a `fire_at` ISO timestamp. The orchestrator's poll loop checks every 10 cycles (~30s) and dispatches mature continuations to their target agent. This is how a long-running plan resumes after a session crash. Milestone bypass: when an agent's output contains markers like "PR #N merged" / "shipped" / "customer-ready", orchestrator writes a `milestone-*` task to Atlas's inbox bypassing the normal depth-1 cascade limit (milestones reset depth to 0). Atlas voices it to Joe in her own voice.

**Fork status:** Neither pattern is in Larry's fork.

**Recommendation:** Continuation registry is the right blueprint for Beacon's "I asked Larry for approval; check back in 24h if no reply" pattern — adapt it. Milestone bypass is the right blueprint for Beacon→Larry "we just shipped" DM in D4 — adapt it. Both are small additions (~50 lines each) on top of the inbox watcher.

### Pattern E — `ship_completion_watcher` post-merge guarantee

**File:** `scripts/ship_completion_watcher.py` (987 lines).

**How it works:** Every 30 min, for every ship-tracking JSON in `shared/ship-tracking/`: enforce that every sub-task gets to merged-or-explicitly-dropped status; re-dispatch sub-tasks pending too long; escalate to Joe after N retries; only mark terminal-completed when every sub-task is accounted for. Memory + concurrency backpressure built in (won't re-dispatch if `MAX_CONCURRENT_LUMA=8` is exceeded or memory > 80%).

**Fork status:** Not pulled. Larry's prototypes don't have ship-tracking yet.

**Recommendation:** This is the *correct shape* for the "Beacon DMs Larry done" flow in D4 if it includes the "make sure the work actually shipped" guarantee. Skip the GM-specific sub-task taxonomy; adapt the *invariant* ("every dispatch eventually closes with done-or-escalated") into a much smaller `pending_dispatches.py` that watches a Larry-shaped state file. ~200 lines, not 987.

### Pattern F — Working-copy discipline enforcement

**Files:** `scripts/agent_core_health_check.py`, `scripts/sync_agent_core.sh`, `scripts/await_quiescence.py`, `scripts/validate_agent_core.py`.

**How it works:** Every hour, `agent_core_health_check.py` runs five checks on `/home/joe/gm-agent-core`: branch=main, working tree clean, no untracked outside .gitignore, in sync with origin, sync-script ran < 6h ago. Auto-fixes the safe cases (fast-forward when behind+clean); alerts via Prism on cases needing human judgment. Companion `sync_agent_core.sh` runs the actual `gm-agent-core → gm-agents/` atomic-swap, guarded by `await_quiescence.py` (no inbox tasks + no claude processes + no agent_runner processes). The sync explicitly REFUSES to overwrite inboxes/outboxes/blackboard/telegram/logs/memory.

**Fork status:** All four pulled and adapted, names updated. Working.

**Recommendation:** Keep as-is. This is the most-load-bearing piece of upstream wisdom Larry already has.

### Pattern G — Cost attribution and accounting

**Upstream:** `scripts/cost_per_pr_dashboard.py` reads from `state/cost-records.jsonl` (written by `agent_runner.run_claude` from the JSON output's `total_cost_usd` field, ~line 642). `audit_founder_comms.py` and `metrics_collector.py` consume it.

**Fork status:** Larry's D2 added a parallel `~/agents/blackboard/costs.jsonl` writer in `inbox_watcher.process_task()` (lines 271–285). The schema is similar but the field names differ slightly (`cost_usd` vs upstream's `total_cost_usd`; `cache_read` vs upstream's `cache_read_input_tokens`).

**Recommendation:** **Normalize on upstream's field names** for forward-compat. When Ledger lands in Phase F it will be easier to build on the upstream conventions than to bridge two schemas. Pull `cost_per_pr_dashboard.py` (110 lines, mostly a roll-up) once Forge starts opening PRs.

### Pattern H — Healer / watchdog taxonomy

**Files (upstream):** 15 healers totaling ~2,234 lines.

**Taxonomy:**
- **Abandonment healers** — heal_abandoned_inbox_tasks (workers exited silently)
- **Empty-state healers** — heal_empty_inbox_files (0-byte writes)
- **Stale-blocked healers** — heal_blocked_inbox_age (drains the `blocked/` dir)
- **Recovery healers** — heal_recovery_already_merged (kills recover tasks whose PR shipped)
- **Loop-liveness healers** — heal_silent_loop_death, heal_backlog_promoter_alive
- **Zombie killers** — heal_zombie_main_workers (deleted-worktree + completed-PR patterns)
- **Restart-state healers** — heal_restart_dedup_obsolete
- **Repo-drift healers** — heal_core_branch_drift, heal_mirror_state
- **Manifest healers** — heal_manifest_reconcile
- **Context-loss healers** — heal_context_less_notify_result
- **Frozen-firewall healers** — heal_frozen_features_inbox_firewall
- **External-flow healers** — heal_pr_auto_merge (NEW 2026-05-10), heal_joe_inbox_stale

Common pattern: all are read-only-by-default, kill-switch-aware (`healers.disabled` flag), HEARTBEAT log per run, HEALED log per intervention. Reversible (move-not-delete). 5–15 min cadence via systemd timer.

**Fork status:** Larry has 7 of 15 pulled — the most-load-bearing class (abandonment, empty-state, stale-blocked, recovery, loop-liveness, zombie, restart-state). None have systemd timer pairs yet.

**Recommendation:** Phase D2/D3 — write systemd `.service` + `.timer` pairs for the 7 healers Larry has. Phase D5 — pull `heal_pr_auto_merge` (new), and skip the rest until they're needed.

### Pattern I — Concurrency / lease / quiescence composition

**Files (upstream):** `scripts/concurrency_guard.py` + `scripts/dispatch_lease.py` + `scripts/await_quiescence.py`.

**How they compose:**
- `concurrency_guard` is the global semaphore (max 10 concurrent claude processes across the WHOLE system, file-locked). Token: get a slot before spawn, release on exit.
- `dispatch_lease` is the per-task ownership primitive: a specific `<task_identity>` can only have ONE worker at a time, even across orchestrator restarts (flock + nonce + boot-id + TTL + kill-before-reclaim).
- `await_quiescence` is the system-wide read-only check: "everything is idle right now" (no inbox files, no concurrency_guard slots, no claude procs). Used before `sync_agent_core.sh` to avoid atomic-swap mid-spawn.

In `orchestrator.py`: a task starts with a `concurrency_guard.wait_for_slot()` (blocks up to 30 min), then `dispatch_lease.try_acquire()`, then `subprocess.Popen()`. Heartbeat thread renews the lease every 60s. On exit, lease released + slot released. On orchestrator crash, the in-flight registry (`state/in-flight/`) plus boot-id-aware lease sweep let the next orchestrator adopt orphans and clear stale leases.

**Fork status:** All three pulled. Larry's `inbox_watcher` uses concurrency_guard implicitly (via agent_runner which is imported as a stub `get_manager`/`get_guard`) and explicitly takes `inbox:<agent>` leases per-agent. NOT the per-task lease pattern.

**Recommendation:** D3 should switch to per-task leases (the upstream pattern). This lets Beacon and Pulse start separate dispatches in parallel without blocking each other and gives correct restart-safety.

### Pattern J — Multi-model routing / fallback

**File:** `config/agent-models.json` (both fork and upstream). Upstream consumed by `agent_runner.get_agent_model()`.

**How it works:** Per-agent: `telegram_model` for chat (fast/cheap), `inbox_model` for work tasks (deep thinking), `fallback_model` for retry-on-failure. Each agent in upstream pins `pinned_at` + `pinned_reason`. The `--fallback-model` is passed to `claude` CLI as the actual model-failover target if the first one errors.

**Fork status:** Larry's `agent-models.json` is *better organized than upstream's* — adds `$schema_version`, `default` block, `_history`, `status: planned` flag for future agents. The semantics are identical.

**Recommendation:** Keep Larry's. **Don't** adopt upstream's per-task-type routing — upstream doesn't actually do this. The model is picked per-agent + context (telegram vs inbox), not per-task-type. No escalation chains in upstream either; just one fallback.

### Pattern K — Telegram-back routing (`reply_chat_id`)

**Files (upstream):** Every dispatch path threads `reply_chat_id` through: `dispatch_validator` requires it be int-or-null; `safe_write_inbox` propagates it; `agent_runner.process_inbox` reads `task.get('reply_chat_id')` and includes it in the result envelope; `orchestrator.process_outbox_notifications` reads it from the result, with three fallback sources (result → ship-tracking originating_chat_id → BRIEFING_CHAT_ID -5191724743). Loud-log fallback ("ROUTING_FALLBACK" / "ORIGIN_CHAT_UNKNOWN") whenever the default kicks in. The bot used to send is chosen via `_bot_for_chat()` which reads `blackboard/telegram-chat-health.json` to pick a bot that's actually a member of the target chat (no silent drops).

**Fork status:** Larry's HANDSHAKE schema preserves the field. `dispatch_validator` checks it. `inbox_watcher.process_task` *passes it through to the outbox* but does NOT do anything with it — no Telegram reply is sent from the watcher. The per-agent `beacon_telegram_bot.py` / `agent_telegram_bot.py` does its own session-keyed reply directly via Telegram API.

**Recommendation:** In Larry's topology with one human (Larry) and one chat per agent, the upstream's multi-chat bot-health gymnastics aren't needed. **Keep Larry's design.** But add `reply_chat_id` to the outbox result so D3's Beacon-replies-to-the-DM-that-asked path can find which chat to write to.

---

## Section 3 — Gaps in our adoption

### Gap 1 — `agent_runner.py` in fork has known bugs and isn't actually wired

**Upstream component:** `scripts/agent_runner.py`.
**Why it matters:** The file is sitting in fork's `scripts/` (1140 lines, adapted) but `inbox_watcher.py` doesn't import it. Three known bugs (lines 501, 694, 936 per task brief — these correspond to MULA WORKSPACE ISOLATION at 479–488 of upstream → simplified to single `--add-dir /home/larry/agents` at fork line 487; repo path `repo_dir = AGENTS_ROOT / 'agents' / agent_id / 'workspace' / 'repo'` at fork line 694 vs upstream's `growth-mastery` at line 694; sweep-ledgers references at lines 933–971 still present in fork). Result: the file is essentially dead code that drifts further from upstream every release.
**Effort:** Modest. Either delete the file and accept the thin-watcher path, or wire `inbox_watcher.process_task` to call `agent_runner.run_claude` (~50-line bridge). See Section 5.
**Priority:** D3 (the design decision must be made before D3 work).

### Gap 2 — No stall detection (dispatch_sentinel missing)

**Upstream component:** `scripts/dispatch_sentinel.py` (430 lines, cron every 10 min).
**Why it matters:** If a task sits in an inbox > 3h with no progress, in upstream Joe gets ONE Telegram alert and the task gets flagged. In Larry's fork, the task just sits there. With only Larry watching and a small task volume this won't bite for a while, but the first time a watcher crash leaves three tasks unprocessed overnight, this is the missing piece.
**Effort:** Modest — adapt the 430 lines (paths joe→larry, alert destination Atlas→Larry-DM-direct, agents list).
**Priority:** D3 — install with the watcher.

### Gap 3 — No routing_validator (role boundaries unenforced)

**Upstream component:** `scripts/routing_validator.py` + `shared/AGENT-ROUTING-SCHEMA.md`.
**Why it matters:** Larry's IDENTITY.md files have role descriptions but no MACHINE-READABLE constraints. Nothing prevents a misrouted task from landing in (e.g.) Pulse's inbox when it should be in Forge's. Today with Larry hand-driving Telegram this is fine; in D3 when Pulse can write directly to Forge/Beacon, this gap matters.
**Effort:** Modest — pull `routing_validator.py` (261 lines), add `## Routing Constraints` sections to beacon/forge/mirror/pulse IDENTITY.md files. Wire into `inbox_watcher`'s validate-before-process step.
**Priority:** D3.

### Gap 4 — No requeue / retry / dead-letter (failures vanish silently)

**Upstream component:** Lines ~1610–1665 of `orchestrator.py` + `_archive_dead_letter()` function.
**Why it matters:** A failed task in Larry's fork bumps `requeue_count` once (cap = 3) and writes back. Upstream does this WITH exponential backoff (5min, 15min, 45min + jitter) AND dead-letter archive with a sidecar reason AND a yellow-tier Prism notify. Larry's path is incomplete — on third failure the task moves to `.invalid` with no notification.
**Effort:** Modest — port the requeue logic (with `not_before` timestamps) and dead-letter archival into the watcher's failure path.
**Priority:** D3.

### Gap 5 — No EMERGENCY_HALT poll in the watcher

**Upstream component:** `scripts/kill_switch.py` writes `blackboard/EMERGENCY_HALT`; `orchestrator.main()` checks the file every poll cycle (~line 2258). Cron jobs check too.
**Fork status:** `kill_switch.py` is pulled but nothing polls the flag.
**Effort:** Trivial — add a one-line check at the top of `agent_loop()` in `inbox_watcher.py`.
**Priority:** D3.

### Gap 6 — No in-flight registry / orphan adoption

**Upstream component:** `agent_runner._register_in_flight()` + `orchestrator.main()` orphan-adoption block (~line 2209).
**Why it matters:** If the watcher crashes mid-claude-call, the next start has no idea a claude subprocess is still running. It may dispatch the same task again.
**Effort:** Modest — the code already exists in fork's `agent_runner.py`, just not exercised.
**Priority:** D3.

### Gap 7 — HANDSHAKE schema source enum drift

**Upstream component:** `shared/HANDSHAKE-SCHEMA.json` (the JSON Schema validated by orchestrator).
**Why it matters:** Fork's HANDSHAKE-SCHEMA.json *still has the gm-era enum* (atlas/sage/luma/etc.) — identical to upstream. But fork's `dispatch_validator.py` uses the new beacon/forge/mirror/pulse enum. If anything else reads the JSON Schema as the canonical truth, the two will disagree.
**Effort:** Trivial — sync the enum.
**Priority:** D3.

### Gap 8 — No systemd timers for the 7 pulled healers

**Upstream component:** `systemd/gm-heal-*.{service,timer}` pairs.
**Why it matters:** The 7 healers exist in fork's `scripts/` but nothing runs them.
**Effort:** Trivial — copy upstream's pair structure, rename gm-→ourliberty-, run `systemctl enable`.
**Priority:** D3.

### Gap 9 — No identity-test coverage

**Upstream component:** `scripts/tests/test_identity_landmine_scrub.py`, `scripts/tests/test_parent_claude_md_guard.py`.
**Why it matters:** The poison-guard machinery in `agent_runner.py` is high-stakes and easy to break with a refactor. Upstream has tests Larry didn't pull.
**Effort:** Trivial — pull the two test files, run them.
**Priority:** D3.

### Gap 10 — No `cleanup_stale_worktrees` cron

**Upstream component:** `scripts/cleanup_stale_worktrees.py`.
**Why it matters:** Once Forge starts using `agent_runner.create_worktree_for_task()`, `/tmp/wt-*` directories will accumulate until the disk fills. Upstream's hourly cleanup is the safety net.
**Effort:** Trivial — pull, adapt, add systemd timer.
**Priority:** D5 (when Forge actually creates worktrees).

### The 2-day delta (Larry's mirror 2026-05-05 → upstream HEAD 2026-05-11)

Eight commits Larry's mirror is missing. Full list:

| SHA | Date | Subject | Larry-relevance |
|---|---|---|---|
| `55a8e17` | 2026-05-11 | `feat(plan-firewall): FULL-ROLLOUT RULE — never <100 rollout in any plan (#246)` | **Skip** — GM-specific plan-firewall rule (V2 program direction) |
| `680f36a` | 2026-05-11 | `feat(plan-firewall): User-Reachability Contract — close shipped-but-invisible failure mode (#245)` | **Skip for now, read later** — the underlying principle ("don't ship features users can't reach") may inform Beacon's spec template |
| `a8d0f89` | 2026-05-11 | `fix(unified-verifier): use source=sage for fix-PR dispatch (Mission Command routing) (#244)` | Skip — Mission Command is GM-specific |
| `f908d3e` | 2026-05-11 | `chore(polish-sweep): tighten cron cadence 1h → 15min for serial Wave 3 drain (#243)` | Skip — polish-sweep GM-specific |
| `35b0687` | 2026-05-10 | `fix(polish-sweep-trigger): verifier-less fallback for polish/codification PRs (#242)` | Skip |
| `844d916` | 2026-05-10 | `fix(post-merge-verifier): include enhance/, ship/, hotfix/ branch prefixes (#241)` | **Pull** — Larry already pulled post_merge_verifier.py. This one-line fix expands the branch-name allowlist; trivial to forward-port |
| `2c0fc49` | 2026-05-10 | `feat(heal-pr-auto-merge): bridge disabled repo auto-merge with 3-min healer (#240)` | **Pull (D5+)** — when Forge starts opening PRs this is the auto-merge bridge Larry needs |
| `0e6eee6` | 2026-05-10 | `feat(post-ship-e2e): one Unified Verifier (Sage+Nova woven), plan-bounded, agent-browser-only (#239)` | Skip — Unified Verifier is woven into Sage's specific flow; the *idea* ("plan-bounded E2E verification") is transferable but not the code |

**Net:** of 8 commits, 2 are load-bearing for Larry's fork (#241 the post_merge_verifier fix, #240 the heal_pr_auto_merge healer). 6 are GM-specific runbook directives / Sage flow changes.

---

## Section 4 — Genuinely GM-specific code

These should stay un-pulled. Each gets a one-line defense.

| Component | Why it's GM-specific |
|---|---|
| `scripts/atlas-*.{sh,py}` (VAPI voice) | Atlas's VAPI voice channel is a customer-facing telephony surface Larry doesn't have. |
| `scripts/luma_email.py` | Luma's outbound email to customers — Larry's agents don't email anyone. |
| `scripts/fathom-webhook-relay.py` | Fathom call recordings → Sage ingestion. Larry has no Fathom integration. |
| `scripts/notion_client.py` | Notion API client for GM's content surface. |
| `scripts/council_*.py` + `shared/COUNCIL-PROTOCOL.md` | `/plan` 5-phase choreography is a GM workflow for customer-facing feature planning; Larry's Beacon does this single-threaded. |
| `scripts/sweep_*.py` + `shared/sweep-ledgers/*.json` + `shared/SWEEP-PROTOCOL.md` | Sweep-ledgers are GM's customer-facing audit trail (per-feature lesson accumulation) — Larry's sandbox produces handoff packages, not customer audits. The lesson-injection in `agent_runner.extract_lessons_for_prompt` (lines 933–1024) reads these files; safe to keep the function but it'll be a no-op without ledgers. |
| `scripts/ship_*.py` + `shared/ship-tracking/` | Ship-tracking is the multi-sub-task plan persistence layer for Sage's complex multi-PR plans. Larry's prototypes are single-PR for now. |
| `scripts/pipeline_*.py` + `scripts/dormant_issue_monitor.py` + `scripts/reconciliation_scan.py` | These watch GitHub issues tagged `luma-assigned` for plan-gist drift — Larry doesn't use GitHub issues for routing. |
| `scripts/dispatch_with_wireframes.py` + `shared/STITCH-WIREFRAMES.md` | Wireframes are a UI design artifact for GM's customer features. |
| `scripts/triage_dispatch.py` + `runbooks/triage-dispatch.md` + `scripts/joe_signal_curator.py` | Sage's `/triage` command — Larry has no equivalent triage surface yet. |
| `scripts/post_incident_reconcile.py` + `scripts/audit_founder_comms.py` | Founder-comms audit is for tracking which agent said what to Joe at scale; Larry's volume is tiny. |
| `scripts/test_plan_v4_acceptance.py` + `scripts/validate_plan_schema.py` | Plan v4 protocol acceptance tests — GM's plan template. |
| `scripts/video_notifier.py` + `tools/video/` | Whole video production pipeline for GM marketing — completely out of scope. |
| `scripts/merge_gates*.py` + `scripts/merge_watcher.py` + `scripts/ship_completion_watcher.py` (in current form) | 5-gate PR auto-merge engine pinned to GrowthMastery's `growth-mastery` repo + Vercel preview check + Sage walkthrough label. The *invariants* are transferable to Forge later (D5+); the specific gates aren't. |
| `scripts/guardian/*` | Pinned-external-version drift tracking — only matters when you have external deps to pin (agent-browser, etc.). Larry's prototypes don't yet. Re-evaluate in Phase F. |
| `scripts/prism_*.py` + `agents/prism/*` | Prism is the meta-observer agent; Pulse is Larry's analog. Read Prism's IDENTITY for transferable patterns; skip the code. |
| `scripts/auto_pr_opener.py` + `scripts/repo_sync.sh` | These touch GitHub directly — pull when Forge opens PRs for real (D5+), not before. |
| `scripts/ledger_sync.py` | Sweep-ledger sync. |
| Most of `agents/sage/*.md` (~25 files), `agents/luma/*.md` (~13 files), `agents/nova/*.md` (~20 files) | Persona files for agents Larry's fork dropped. Read for reference; never adopt as-is. |
| `shared/DESIGN-SYSTEM-BIBLE.md` + `shared/brand-rules.md` + `shared/IDEAL-CUSTOMER.md` + `shared/PLAN-TEMPLATE.md` + `shared/PLAN-MANIFEST-TEMPLATE.md` | All customer/product-facing artifacts for GrowthMastery's market. |

---

## Section 5 — The D2 thin-watcher question

**The question:** In Phase D2, Larry wrote a 367-line `inbox_watcher.py` (fork) instead of using upstream's 1140-line `agent_runner.py` + 2483-line `orchestrator.py`. Stated reasons: (a) three path bugs in fork's already-adapted `agent_runner.py`, (b) GM-specific orchestration not needed for Larry's 4-agent topology.

### 5.1 What `agent_runner.py` does that `inbox_watcher.py` doesn't

Reading both files end-to-end, the `agent_runner.run_claude()` function offers the following capabilities the thin watcher lacks:

| Capability | `agent_runner.run_claude` location | `inbox_watcher` equivalent |
|---|---|---|
| **MAX_RETRIES=5 with exponential backoff** (10→20→40→80→160s) | Lines 460–676 (the `for attempt in range(MAX_RETRIES)` loop) | None — single attempt, fail-as-final |
| **Rate-limit detection + per-account cooldown** (1h for usage caps, 5min for transient 429) | Lines 611–621 (via `token_manager.check_for_rate_limit` + `report_rate_limit`) | None — claude error is just an error |
| **Parent-CLAUDE.md poison quarantine** (called once per spawn) | Lines 122–242 + invocation at 510 | None |
| **/tmp identity-landmine scrubber** (called once per spawn) | Lines 260–314 + invocation at 525 | None |
| **Identity-assertion preamble** (opt-in via `expected_agent`) | Lines 326–348 + invocation in `process_inbox` at line 1054 | None |
| **Graceful cancellation** via `blackboard/cancel-task-<stem>.json` markers, polled every 5s | Lines 409–429 + polling block 562–579 | None |
| **In-flight registry** (`state/in-flight/<task_stem>.json`) for restart-survival + orphan adoption | Lines 361–406 + invocations 547–596 | None |
| **start_new_session=True** (workers detach from parent process group) | Line 545 | NOT — `subprocess.run` blocks parent until done; on watcher restart all in-flight tasks are lost |
| **Worktree creation per task** with branch-checkpoint pre-push | Lines 682–817 | None |
| **Worktree preamble idempotency** with HEADER_BLOAT escape hatch | Lines 826–905 | None |
| **Sweep-ledger lesson injection** (would no-op in Larry's fork; safe to keep) | Lines 934–1024 | None |
| **Cancel-marker → SIGTERM (10s grace) → SIGKILL** with cleanup of cancel file | Lines 567–579 + 422–429 | None |
| **JSONDecodeError fallback** — partial output preserved as plaintext, returncode-0 still treated success | Lines 651–658 | The watcher errors-out on non-JSON |
| **session_id resume** (`--resume <id>`) for multi-turn dev tasks | Lines 491 + 627–629 + 644 | None — watcher does not support session resume (the per-agent telegram bot does, but the inbox-dispatch path does not) |
| **CLAUDE_CODE_EFFORT_LEVEL** env var (low/medium/high/max) | Line 465 | None — always claude-default |
| **per-task model_override** | Lines 467–471 | Yes (the watcher reads `task['model']` ✓) |
| **fallback-model on first-failure** (CLI flag `--fallback-model`) | Lines 489–491 | None |
| **--permission-mode bypassPermissions** | Line 472 | None — claude uses default permissions |

### 5.2 Which of those capabilities meaningfully improve robustness in our 4-agent topology

In rough priority order:

1. **In-flight registry + start_new_session=True** — TODAY: a watcher SIGKILL or systemd-restart leaves any running claude subprocess orphaned, the watcher dispatches it again, and you get duplicate work. Severity: HIGH. Probability: any time the unit restarts.
2. **MAX_RETRIES with exponential backoff + rate-limit detection** — TODAY: any transient claude error (network blip, OAuth 401, 429) fails the task permanently. Severity: HIGH. Probability: weekly.
3. **Parent-CLAUDE.md poison quarantine + /tmp landmine scrubber** — TODAY: someone (a healer, a manual op, a copy/paste, a misconfigured cron) drops a `/tmp/CLAUDE.md` and every subsequent agent spawn loads it as identity. Severity: MEDIUM-HIGH. Probability: rare but catastrophic when it happens (the upstream 2026-04-16 incident wedged Joe for a full day).
4. **Identity-assertion preamble (expected_agent)** — TODAY: nothing prevents Pulse's task from being processed by Beacon if a misroute happens. Severity: MEDIUM. Probability: rises once D3 makes agents-write-to-other-agents-inboxes a real flow.
5. **bypassPermissions + EFFORT_LEVEL + add-dir** — TODAY: the agent might prompt for permission mid-task (and the watcher's stdin is closed, so it hangs). Severity: HIGH on the rare case it fires; resolved by the missing `--permission-mode bypassPermissions`.
6. **Graceful cancellation via cancel-markers** — TODAY: there's no way to cancel an in-flight task other than killing the watcher (which kills ALL in-flight tasks). Severity: MEDIUM. Probability: needed once /cycle can self-stop a runaway agent.
7. **Worktree creation + branch-checkpoint** — Only matters once Forge writes code to a real repo. Severity: HIGH at that point, NONE today.
8. **session_id resume** — Matters when tasks are long and OOM-restartable. Severity: LOW for now (Beacon/Forge tasks are minutes, not hours).
9. **Sweep-ledger lesson injection** — Safe no-op without ledgers; adopt later.

### 5.3 Path to use `agent_runner.py` directly

The three known fixes (paths joe→larry at lines 501, 694, 936 in upstream — corresponding to fork lines 487, 694, 936) are already applied in fork's copy. The remaining adaptations needed to make `agent_runner.py` runnable as-is:

| Issue | Effort to fix |
|---|---|
| `token_manager` stub (already done in fork lines 18–25; returns `(env_token, 0)` always) — works but has no rate-limit detection. Need to add a passthrough `check_for_rate_limit`, `detect_cap_in_output`, `report_rate_limit`, `report_success` methods returning False/no-ops. | ~15 lines |
| `concurrency_guard.get_guard()` — already pulled, works as-is. | 0 |
| `IN_FLIGHT_DIR = AGENTS_ROOT / 'state' / 'in-flight'` directory creation — works as-is once the dir is mkdir'd by systemd or first call. | 0 |
| `CANCEL_DIR = AGENTS_ROOT / 'blackboard'` — works once blackboard exists. | 0 |
| Sweep-ledger function returns empty string when no ledgers exist — already handles missing dir gracefully (line 994 `if not ledger_file.exists(): continue`). | 0 |
| `process_inbox()` was upstream's task loop — Larry's per-agent thread structure is better. **Don't use process_inbox; just call `run_claude` from `inbox_watcher.process_task`**. | ~30 lines (new bridge function in watcher) |
| Worktree creation (`if agent_id == 'main':` at line 1066) is `main`-specific in upstream — Larry needs to either make it a per-agent opt-in (read from agent-models.json) or restrict to Forge. Default: skip worktree creation for now; add when Forge needs it. | ~5 lines |
| The `model_override` arg in `run_claude` becomes the `task['model']` value — pass through. | 0 |

Total scope for "use agent_runner.run_claude from inbox_watcher": ~50 lines of new code, ~15 lines of stub token_manager. Less than a day's work.

### 5.4 Recommendation

**HYBRID: keep `inbox_watcher.py`'s thread-per-agent structure, but call `agent_runner.run_claude()` for the actual claude invocation. Phase out the watcher's current `run_claude` function in favor of the upstream one.**

Reasoning:
- The thread-per-agent topology is *Larry's improvement* over upstream's ThreadPoolExecutor-on-flat-queue. Keep it. It maps perfectly to Larry's 4-agent fan-out and gives implicit per-agent serialization which is what Larry wants for now.
- The 1140 lines of `agent_runner.py` contain ~600 lines of identity-hardening, retry, in-flight registry, cancel-marker, and rate-limit handling that Larry will eventually want and that took Joe 6 months of incidents to write. Rebuilding that from scratch is the wrong move.
- The thin watcher's value is in its *structure* (per-agent thread, per-agent lease, simple loop), not in its `run_claude` (which is duplicative and undercooked).
- Migration path is unblocked: the three known bugs are already fixed in fork's `agent_runner.py`. The remaining work is the ~50-line bridge.

**Concrete D3 work to migrate:**
1. Add a minimal token_manager stub class to fork's `agent_runner.py` with no-op rate-limit methods (don't import from a missing module; class it inline).
2. Delete the current `run_claude` function in `inbox_watcher.py` (lines 165–232).
3. In `process_task`, replace the `run_claude(agent, task, model)` call with `agent_runner.run_claude(agent_id=agent, prompt=task['prompt'], working_dir=str(AGENT_DIR), timeout=timeout, context='inbox', model_override=task.get('model'), task_stem=task_id, system_prompt_file=str(agent_claudemd) if Larry-uses-system-prompts)`.
4. Wire `expected_agent` field into the task envelope writer (Pulse/Beacon/etc. should set `task['expected_agent']` = the target agent).
5. Wire EMERGENCY_HALT check at top of `agent_loop()`.
6. Add the test files `test_identity_landmine_scrub.py` + `test_parent_claude_md_guard.py` from upstream so refactors don't break the poison-guards.
7. After migration runs clean for a week, replace `inbox_watcher.py`'s ad-hoc dedup (lease + per-agent thread) with `agent_runner.py`'s in-flight registry + per-task `dispatch_lease` (Pattern I in Section 2).

**What to NOT do:**
- Don't wholesale adopt `orchestrator.py`. It's 2483 lines, 60% of which is GM-specific (founder-tone message filter, council-source filter, sage-milestone bypass, briefing-chat fallback, ship-tracking propagation, schema strict-flip, content-similarity dedup, F38/F43/F55 incident-specific guards). The thread-per-agent watcher is the right structural successor.
- Don't reintroduce the upstream `inbox_watcher.py` (the inotify wake-flag toucher). The thread-per-agent poll watcher Larry has is simpler and faster on a 4-agent system.
- Don't pull token_manager.py. The dual-OAuth-account complexity isn't worth it for Larry yet; add it back if Larry adds a second account.

---

## Section 6 — Implications for D3 design

D3 wires three flows. For each: what upstream gives us, what's genuinely new.

### Flow 1 — Beacon ↔ Pulse dialogue

**Goal:** Pulse asks Beacon a question, Beacon answers, Pulse resumes with context.

**Upstream blueprint:**
- The `-result` source convention (Pattern B) — Pulse's task to Beacon carries `source: pulse`. Beacon's outbox `-result.json` carries the same. The outbox-processor sees `source != self`, writes `notify-<stem>.json` to Pulse's inbox with `source: beacon-result` and the response as the new prompt + `_notify_depth: 1`.
- Implementation lives in `orchestrator.process_outbox_notifications` lines 1869–1947.
- Depth limiter at line 1878 (`if next_depth > 1`) prevents infinite ping-pong.

**What's needed:** Port the ~80-line notify-cascade writer from upstream's `process_outbox_notifications` into a new file `scripts/outbox_notifier.py` (or fold into the inbox_watcher's post-process hook). Adapt the agent enum from upstream's 7 to Larry's 4. Remove GM-specific milestone-to-atlas bypass — Beacon will DM Larry directly via her own telegram bot, not via a fan-out notify task.

### Flow 2 — Larry-approval gate via Telegram

**Goal:** Beacon DMs Larry with a plan, waits for "approve/modify/reject", routes accordingly.

**Upstream blueprint:** Honestly, **partial** — upstream's `INTERNAL_ACK_PREFIXES` filter (orchestrator.py lines 309–388) and the `council-approval` source value give *some* shape, but the actual "agent asks human for approval, waits, resumes" flow is NEW in Larry's topology. GM's analog is the `/plan` council where Sage synthesizes and Joe never actively approves — the council just runs.

**What's genuinely new:**
- A `pending-approvals` state file (`agents/state/beacon-pending-approvals.json`) — already specified in the 2026-05-09 handoff doc (lines 100–106).
- Beacon's telegram bot needs to detect approval-pattern replies ("approve", "yes", "go", "ok", "modify: ...", "reject: ..."). Pattern detection logic is new.
- A timeout mechanism: if no reply within N hours, the pending request is auto-rejected with a journal entry.

**Upstream patterns to reuse:**
- The `continuation registry` (Pattern D, orchestrator lines 2304–2326) — same shape: persistent JSON file, `fire_at` timestamp, every N poll cycles check for matured continuations. Adapt for approval-pending with `expire_at` timestamp.
- The `INTERNAL_ACK_PREFIXES` filter (orchestrator lines 309–388) — protects against the "agent says 'Sent.' which then gets sent back to user" double-delivery pattern. Pull into Beacon's telegram bot.

### Flow 3 — Beacon → Forge dispatch

**Goal:** On Larry's approval, Beacon writes a spec task to `inboxes/forge/`.

**Upstream blueprint:** This IS the upstream pattern. Sage → Luma dispatch is exactly Beacon → Forge. The upstream Sage code in `scripts/ship_dispatch.py` is too coupled to ship-tracking/wireframes/feature-scopes, but the *core write* is `safe_write_inbox(target='forge', task_dict={...}, source='beacon', filename=...)`.

**What's needed:**
- Pull `safe_write_inbox` semantics (validation + routing_validator check + atomic write + audit log) into a small helper module, OR inline in Beacon's bot.
- Use `dispatch_dedup_guard.py` (already in fork) before the write — prevents Beacon from accidentally re-dispatching the same spec.
- Set `task['expected_agent'] = 'forge'` so when agent_runner.run_claude (post-D3 migration) picks it up, it asserts identity.

**Pull from upstream for this flow:** `routing_validator.py` (Gap 3), `dispatch_sentinel.py` (Gap 2), `outbox_notifier.py` (newly extracted from `orchestrator.process_outbox_notifications`, Flow 1 above).

---

## Section 7 — Maintenance plan for this document

### Cadence for re-audit
- **Per-phase:** Re-audit at the close of each major phase (D5, E, F, G). Each phase pulls in new upstream-derived components, and we want to know what we missed when we did.
- **Quarterly otherwise:** A lightweight diff-only re-audit every 90 days even if no phase boundary fires. Mark new commits since last audit, classify each.
- **NOT per-upstream-pull.** Pulling a single component (e.g. a healer) should not require a full re-audit; it should require updating the relevant Section 1 row.

### Re-audit triggers (force a re-audit out of cadence)
- A large upstream PR lands that touches `orchestrator.py`, `agent_runner.py`, or `dispatch_*.py` (the core dispatch ensemble).
- Upstream introduces a new agent role (e.g., they spin up an 8th agent in their C-suite).
- Upstream changes the HANDSHAKE-SCHEMA in a breaking way (anything other than additive properties or enum extension).
- Larry's fork experiences an incident that traces to a missing upstream defense (the kind of "Joe already solved this, we didn't pull it" moment).
- Six months pass with no re-audit, regardless of cadence — accumulating drift gets harder to address the longer it sits.

### Sections that may need follow-up audits
- **Section 5 (D2 thin-watcher question)** will go stale once the D3 migration to `agent_runner.py` is done. After D3 lands, retire Section 5 and replace with a "D3 migration retrospective" note pointing at the relevant fork PR.
- **Section 3 Gap 1 (agent_runner bugs)** should be checked against fork's current `agent_runner.py` every audit — when fork updates `agent_runner.py`, verify the three known fixes are still applied and no new joe→larry paths slipped in.
- **Section 4 (genuinely GM-specific)** should be re-examined when each new agent role lands (Aide, Scout, Compass, Ledger). Some of the GM-specific code (e.g., `audit_founder_comms.py`) may become applicable when the agent surface grows.
- **Section 2 Pattern G (cost attribution)** needs a deep re-read once Ledger ships (Phase F) — at that point we should normalize on whichever cost schema Larry's Ledger consumes.
- **Section 2 Pattern E (ship_completion_watcher)** is the right blueprint for the "Beacon DMs Larry done" flow but the GM-specific 987 lines aren't pullable. A separate "ship-tracking design for Larry's prototype topology" audit will be needed in D4/D5 — likely a 200-line `pending_dispatches.py` rather than a port.

### Authoritative state on re-audit
This audit doc itself should be the authoritative comparison target. On re-audit:
1. Update the upstream HEAD SHA at the top.
2. Add a row to Section 3's "2-day delta" table for each new commit since last audit, with a relevance classification.
3. For any new component in upstream's `scripts/` directory not in Section 1, add a row.
4. For any component whose Status changed (Adapted-and-use → Pulled → etc.), update the row.

Append a section at the end of the doc per re-audit (`## Audit log`) recording the audit date and what changed since last audit, so the doc tracks its own history.
