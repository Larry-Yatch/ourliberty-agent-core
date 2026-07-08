# Installing Ourliberty systemd units

These units replace the tmux-based bot launchers with proper systemd-managed services. Benefits:
- Auto-restart on crash
- Auto-start on droplet boot
- Centralized logs via `journalctl`
- Resource limits (memory, tasks)
- Filesystem hardening (`ProtectHome`, `ReadWritePaths`)

## Prerequisites

- All bots' tokens populated in `/home/larry/credentials/.env.larry`
- Anthropic API key set in `.env.larry` (for Pulse `/cycle`)
- `larry` user can `sudo` without password (already configured)

## Install all units

Run on the droplet as `larry`:

```bash
# Copy units into systemd's directory
sudo cp ~/agent-core/systemd/ourliberty-*.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-*.timer /etc/systemd/system/

# Reload systemd to see the new units
sudo systemctl daemon-reload

# (Recommended) verify each unit parses cleanly
for unit in ~/agent-core/systemd/ourliberty-*; do
  systemd-analyze verify "$unit" || echo "VERIFY FAILED: $unit"
done
```

## Enable and start the units

### Beacon (replaces the tmux session)

If you're currently running Beacon via tmux, stop it first to avoid two instances polling the same Telegram bot:

```bash
tmux kill-session -t beacon-bot 2>/dev/null
sudo systemctl enable --now ourliberty-beacon-bot.service
sudo systemctl status ourliberty-beacon-bot.service
journalctl -u ourliberty-beacon-bot.service -f   # tail the log
```

### Forge / Mirror / Pulse (Phase C / D activation)

After you've created the bot via BotFather and installed the token in `.env.larry`:

```bash
sudo systemctl enable --now ourliberty-forge-bot.service
sudo systemctl enable --now ourliberty-mirror-bot.service
sudo systemctl enable --now ourliberty-pulse-bot.service
```

### Inbox watcher (Phase D2)

The shared inbox watcher polls `~/agents/inboxes/{beacon,forge,mirror,pulse}/`
every 5s, validates each task, runs `claude --print` per-agent, and writes the
result to `~/agents/outboxes/<agent>/`. One process, four agent threads, max
one in-flight task per agent (lease primitive).

```bash
sudo systemctl enable --now ourliberty-inbox-watcher.service
journalctl -u ourliberty-inbox-watcher.service -f   # tail the log
```

To smoke-test end-to-end, drop a HANDSHAKE-conformant JSON into one of the
inbox dirs (see `runbooks/cycle-prompt.md` §8 for the format) and watch:
- `journalctl -u ourliberty-inbox-watcher.service -f` for the pickup line
- `~/agents/outboxes/<agent>/` for the result file
- `~/agents/blackboard/costs.jsonl` for the cost record
- `~/agents/inboxes/<agent>/.archive/` to confirm the task was consumed

### Periodic services (timers)

These are **timers**, not the underlying services. Enabling the timer is what schedules the work:

```bash
# /cycle runs every 30 min (Phase D)
sudo systemctl enable --now ourliberty-cycle.timer

# Sync runs every 1 hour
sudo systemctl enable --now ourliberty-sync.timer

# Health check runs every 30 min
sudo systemctl enable --now ourliberty-agent-core-health.timer

# Watchdog runs every 5 min (Phase D activation; depends on watchdog.py being wired)
sudo systemctl enable --now ourliberty-watchdog.timer

# Pulse Check XI — catalog accuracy meter runs daily (04:17 local).
# Deterministic + LLM-free, so it runs on a timer rather than inside the Pulse
# /cycle. Requires the ourliberty-graph checkout at /home/larry/ourliberty-graph
# (set OURLIBERTY_GRAPH_DIR in the .service to override). Watched for liveness
# by heal-pulse-check-staleness via its "xi" cadence entry.
sudo systemctl enable --now ourliberty-pulse-check-xi.timer

# Pulse Checks I, III, V, VI, VIII, IX, X — all deterministic analyzers, moved
# off agent-invoked /cycle scheduling 2026-07-07 (the agent chronically missed
# late runbook sections; timers never miss). Cadences mirror
# config/pulse-check-cadence.json; each is watched for liveness by
# heal-pulse-check-staleness. Check IV's timer predates these (same pattern).
sudo systemctl enable --now ourliberty-pulse-check-i.timer     # Mon/Wed/Fri/Sun, after Ledger's Monday run
sudo systemctl enable --now ourliberty-pulse-check-iii.timer   # Sundays; analyzer self-gates to the 14-day cadence
sudo systemctl enable --now ourliberty-pulse-check-v.timer     # first Monday of the month
sudo systemctl enable --now ourliberty-pulse-check-vi.timer    # first Monday of the month
sudo systemctl enable --now ourliberty-pulse-check-viii.timer  # Mondays
sudo systemctl enable --now ourliberty-pulse-check-ix.timer    # Mondays
sudo systemctl enable --now ourliberty-pulse-check-x.timer     # Mondays
sudo systemctl enable --now ourliberty-pulse-check-xiv.timer   # Mondays (05:49) — alert-precision meter
# One-time catch-up: if a check's scheduled day already passed when you enable
# (e.g. enabling after the first Monday leaves V/VI dark until NEXT month while
# the staleness watcher's cadence+grace expires), run the missed check once by
# hand — its internal sentinel/gate makes this idempotent:
#   sudo systemctl start ourliberty-pulse-check-v.service ourliberty-pulse-check-vi.service
```

### Self-healing healers (Phase D2.5 + E1.3 + E1.5.2 + E2.1 + E2.2)

Twelve scripts under `scripts/heal_*.py`, `scripts/sync_*.py`, and `scripts/deploy_notifier.py` watch for specific failure modes the audit identified. Each runs on its own systemd timer (2 min–12 h cadence) and is one-shot — fires, reports, exits. Enabling these closes audit Gap 8 and the credential-discipline + install-discipline + deploy-targets + deploy-notifier gaps surfaced in E1.5 / E2.1 / E2.2.

```bash
# Install (copy unit files into systemd's directory)
sudo cp ~/agent-core/systemd/ourliberty-heal-*.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-heal-*.timer /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-sync-deploy-targets.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-sync-deploy-targets.timer /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-deploy-notifier.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-deploy-notifier.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Enable + start all 12 timers at once
sudo systemctl enable --now ourliberty-heal-abandoned-inbox-tasks.timer
sudo systemctl enable --now ourliberty-heal-blocked-inbox-age.timer
sudo systemctl enable --now ourliberty-heal-empty-inbox-files.timer
sudo systemctl enable --now ourliberty-heal-recovery-already-merged.timer
sudo systemctl enable --now ourliberty-heal-restart-dedup-obsolete.timer
sudo systemctl enable --now ourliberty-heal-silent-loop-death.timer
sudo systemctl enable --now ourliberty-heal-zombie-main-workers.timer
sudo systemctl enable --now ourliberty-heal-wedged-review-sessions.timer  # reaps wedged Mirror/Forge review claude -p sessions (every 5 min; Case 1 auto-reap, Case 2 alert-only until promoted)
sudo systemctl enable --now ourliberty-heal-pr-auto-merge.timer  # E1.3 — DRY-RUN by default; see service file for activation
sudo systemctl enable --now ourliberty-heal-credential-registry-drift.timer  # E1.5.2 — DRY-RUN by default
sudo systemctl enable --now ourliberty-heal-systemd-install-drift.timer  # E1.5.2 — DRY-RUN by default
sudo systemctl enable --now ourliberty-sync-deploy-targets.timer  # E2.1 — DRY-RUN by default
sudo systemctl enable --now ourliberty-deploy-notifier.timer  # E2.2 — DRY-RUN by default
sudo systemctl enable --now ourliberty-heal-chain-event-shipper-heartbeat.timer  # E4.4d PR-B
sudo systemctl enable --now ourliberty-heal-chain-event-type-audit.timer  # E4.4d PR-B (weekly Sundays)
sudo systemctl enable --now ourliberty-heal-build-sequence-advancer-heartbeat.timer  # E-orchestrator PR-S2 (every 5 min)
sudo systemctl enable --now ourliberty-heal-claude-max-burn-rate.timer  # claude-quota-fixes-v2 (every 15 min)
sudo systemctl enable --now ourliberty-heal-tier2-weekly-health-probe.timer  # claude-quota-fixes-v2 (weekly Sun 06:00 MDT)
sudo systemctl enable --now ourliberty-heal-droplet-git-drift.timer  # droplet-drift-discipline-v2 (every 30 min; observe + alert, no mutation)
sudo systemctl enable --now ourliberty-heal-resume-paused-on-tier1.timer  # rate-limit-resilience step B — DRY-RUN by default; see service file for activation
# pulse-check liveness watcher — ENABLE ONLY AFTER the glob + monitoring-since
# hardening (PR "harden the pulse-check liveness watcher") has merged AND been
# synced to the droplet, and after running the one-time seed below; otherwise
# the pre-hardening watcher re-storms 8 first-run escalations.
#   python3 ~/agent-core/scripts/seed_pulse_check_heartbeats.py   # seed real heartbeats / baseline (no POST/DM/config edit)
sudo systemctl enable --now ourliberty-heal-pulse-check-staleness.timer  # liveness watcher (every 6h; OnCalendar)
# NOTE: heal-systemd-install-drift auto-installs missing units (cp + daemon-reload
# + enable --now for timers), so once the hardening PR is synced this timer will be
# installed AND enabled by that healer on its next run even without the manual line
# above. Run the seed first regardless.
sudo systemctl enable --now ourliberty-heal-unregistered-approval.timer  # direction-ask reconciliation net (every 15 min; OnCalendar) — needs EnvironmentFile=.env.larry for Supabase creds
sudo systemctl enable --now ourliberty-heal-missions-card-gc.timer  # missions-v2 Phase 1 § 6 (every 10 min) — retires stale desktop-session cards, ages parked captures, commits captures.json delta to main; needs EnvironmentFile=.env.larry for Supabase + gh + git push
sudo systemctl enable --now ourliberty-heal-merged-pr-board-reconcile.timer  # off-board merged-PR backstop (every 30 min, OnCalendar) — surfaces off-board missions whose work merged to the for-Larry needs-you lane (evidence only; NEVER mutates the board); needs EnvironmentFile=.env.larry for gh
sudo systemctl enable --now ourliberty-heal-orphan-autoregister.timer  # missions-v2 Phase 3 § 6 (every 15 min) — proposes phase=proposed missions.json threads for non-terminal orphans (idempotent + fail-safe); needs EnvironmentFile=.env.larry for Supabase + gh + git push. NOTE: enable only once the dashboard "Proposed" affordance (p3-dashboard-proposed-lane) has shipped, else proposed entries render in the kanban without their own lane.

# Long-running ingestion daemon (not a timer; default disabled at activation gate)
sudo systemctl enable ourliberty-chain-event-shipper.service  # E4.4d PR-B — service is OFF until OURLIBERTY_CHAIN_SHIPPER_ENABLED=true (see service file)

# Build-sequence advancer (timer-driven oneshot; default disabled at activation gate)
sudo systemctl enable --now ourliberty-build-sequence-advancer.timer  # E-orchestrator PR-S2 — enables the timer; the per-tick service is OFF until OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=true (see service file)

# Confirm
systemctl list-timers 'ourliberty-heal-*' 'ourliberty-sync-*' 'ourliberty-deploy-*' --all
```

What each one does:

| Healer | Cadence | What it watches for |
|---|---|---|
| `abandoned-inbox-tasks` | 10 min | Tasks stuck in an inbox because the worker exited silently |
| `blocked-inbox-age` | 15 min | Stale tasks in `inboxes/*/blocked/` past their TTL |
| `empty-inbox-files` | 15 min | Empty / trivially-malformed JSON files dropped into agent inboxes |
| `recovery-already-merged` | 5 min | Recovery tasks pointing at PRs that have since been merged |
| `restart-dedup-obsolete` | 5 min | Stale `RESTART_DEDUP` duplicate markers |
| `silent-loop-death` | 10 min | Self-scheduled re-queue loops that died without leaving a trace |
| `zombie-main-workers` | 5 min | `claude` agent processes still running in deleted worktree paths |
| `pr-auto-merge` (E1.3) | 5 min | Mirror-PASSed PRs whose auto-merge primary path missed |
| `credential-registry-drift` (E1.5.2) | 6 h | Credentials in store without registry entries; registry entries without credentials in store |
| `systemd-install-drift` (E1.5.2) | 12 h | systemd units shipped in repo but never installed under `/etc/systemd/system/` |
| `sync-deploy-targets` (E2.1) | 12 h | `config/deploy_targets.json` ↔ Vercel API drift (project missing on either side, name mismatch) |
| `deploy-notifier` (E2.2) | 2 min | Vercel preview-URL READY + build-ERROR events for configured deploy targets |
| `chain-event-shipper-heartbeat` (E4.4d PR-B) | 5 min | `chain_event_shipper.heartbeat` file mtime > 10 min stale (daemon hung or crashed) |
| `chain-event-type-audit` (E4.4d PR-B) | weekly Sun 06:00 | `chain_events` rows whose `event_type` is not in the application-side `KNOWN_EVENT_TYPES` allowlist |
| `build-sequence-advancer-heartbeat` (E-orchestrator PR-S2) | 5 min | `build-sequence-advancer.heartbeat` file mtime > 10 min stale (2 missed ticks at the 5-min advancer cadence — per spec § 5.4 failure mode 3) |
| `claude-max-burn-rate` (claude-quota-fixes-v2; re-based 2026-05-28) | 15 min | Rolling 5h Tier 1 quota-consuming token volume (`input_tokens + output_tokens + cache_creation` from `~/agents/blackboard/costs.jsonl`) ≥ 80% of `config/agent-models.json:tier1_quota.max_5h_token_threshold` (10M-token seed). Pure file read + arithmetic — makes ZERO LLM calls so it self-protects from Tier 1 quota. The prior dollar threshold (`max_5h_spend_threshold_usd`) was imputed-not-real and false-alarmed every ~15 min on 2026-05-27 while real Anthropic usage sat at 31% session / 59% weekly; the token-volume proxy fixes both. `scripts/pulse_check_viii.py` tunes the seed weekly via the precision/recall loop. |
| `tier2-weekly-health-probe` (claude-quota-fixes-v2) | weekly Sun 06:00 MDT | Cheap Haiku probe of Tier 2 OAuth (~$0.001/run); DMs if `claude -p 'say PROBE_OK'` against `HOME=/home/larry/.claude-larry-personal` fails (non-zero exit, `is_error: true`, or token missing from output). Catches silent credential rot BEFORE Tier 1 needs the fallback. |
| `droplet-git-drift` (droplet-drift-discipline-v2) | 30 min | Droplet working tree drift vs `origin`: ahead with oldest unpushed > 2h, behind by > 2 commits, or uncommitted files older than 6h. Observation-only; no auto-pull / auto-push / auto-commit. Each tripped condition fires a `droplet-{ahead,behind,uncommitted}:<branch>` larry_alert with the manual recovery command in the body. |
| `unregistered-approval` (approval-tab-coverage) | 15 min | Approval-class escalations in `larry-alerts.jsonl` (route=escalate + a decision signal per `config/unregistered-approval-heuristics.json`) that never got an `APPROVAL_REQUEST` marker. Promotes each unmatched one onto the Approvals tab via the bot's own `add_pending` + `emit_event` path (target_agent=beacon), reconstructing binary options from the alert's `suggested_action` or registering a needs-triage item. Dedups via `state/heal-unregistered-approval-promoted.json` (each alert promoted once) and skips asks Beacon already registered (subject collision guard). Needs `EnvironmentFile=.env.larry` for the Supabase write. |
| `missions-card-gc` (missions-v2 Phase 1 § 6) | 10 min | Three reconciliations of the Missions board: (1) retire stale desktop-session cards — an open `desktop_session_start` (no later `desktop_session_done`) whose branch is merged/deleted, whose repo dir is gone, or that's idle >24h → emit a synthetic `desktop_session_done` via `chain_event_emit.emit_event` (same write path the ingest endpoint uses); every indeterminate git/gh signal errs toward KEEP. (2) Age parked captures — flag `aging: true` (never delete, idempotent) on `state==parked` captures whose `last_touched` > 5 business days. (3) Commit + push any `agents/beacon/captures.json` delta to `main` (run_cycle.sh's pull --rebase --autostash push fallback; refuses to commit off-main). Idempotent / atomic / fail-safe; `--dry-run` reports without emitting/writing/committing. Needs `EnvironmentFile=.env.larry` for Supabase + gh + git push. |
| `orphan-autoregister` (missions-v2 Phase 3 § 6) | 15 min | Self-drains the Orphans lane: scans NON-TERMINAL, NON-INFRASTRUCTURE orphans (REUSING the dashboard's Phase-2 derive — `dashboard_api.detect_orphans` / `is_infrastructure_task` / `_derive_orphan_readability`, so there is one classification and no TS↔Python drift) and appends a `phase: "proposed"` entry to `agents/beacon/missions.json` for each, then commits + pushes the delta to `main` (refuses to commit off-main; pull --rebase --autostash fallback). IDEMPOTENT: a proposed entry registers the orphan's `task_id`, so the next tick's `detect_orphans` (which excludes registered task_ids) never re-proposes it — accept (phase→drafting) and dismiss (acknowledge) both keep it registered too. FAIL-SAFE: chain_events unavailable / missions.json malformed → propose nothing; a terminal orphan (merged/closed PR) or an orphan whose live PR-state can't be resolved (indeterminate) is skipped — every uncertain signal errs toward NOT proposing. `--dry-run` reports without appending/writing/committing. Needs `EnvironmentFile=.env.larry` for Supabase + gh + git push. |
| `resume-paused-on-tier1` (rate-limit-resilience B) | 10 min | In-flight tasks marked `paused_on_tier1` by `agent_runner._mark_paused_on_tier1` (Tier 1 quota/auth on a `--resume` session where Tier 2 fallback is structurally unavailable). Once the recorded tier's cooldown clears via `active_tier.cooldown_until(tier)`, re-dispatches the work as a FRESH task (`session_id`/`resume_session_id` stripped, `task_id` suffixed `-resume-<UTC ts>`, `source=auto-retry`) and deletes the in-flight marker. DRY-RUN by default; per-task budget = 3 attempts; per-tick cap = 5 re-dispatches. |

Each healer's logs land in `journalctl -u ourliberty-heal-<name>.service`. They `Nice=10` so they never starve real work.

#### Install-audit pattern (E1.5.2)

The `systemd-install-drift` healer above is itself an audit primitive: it catches every PR that ships a new `systemd/*.service` or `*.timer` but doesn't get installed on the droplet. Motivating example: PR #43 shipped `heal-pr-auto-merge.{service,timer}` to the repo, but they were never copied to `/etc/systemd/system/` — the gap stayed silent until E1.5 review caught it.

The pattern: any PR that adds a unit file is operator-completed when:

1. The file lands in `systemd/` in the repo (PR merge).
2. `sudo cp ~/agent-core/systemd/<unit> /etc/systemd/system/` + `sudo systemctl daemon-reload` + (for timers) `sudo systemctl enable --now <unit>` on the droplet.
3. The drift healer's next tick (within 12 h) finds no drift — confirms the install landed.

If step 2 is missed, the healer DMs Larry with the exact install commands; the gap closes within one tick.

#### Credential-discipline pattern (E1.5.2)

The `credential-registry-drift` healer enforces the 4-artifact rule from `shared/credentials-discipline.md` at runtime: every credential in `.env.larry` / `~/.config/gh/hosts.yml` / `~/.claude/.credentials.json` / `~/.google_workspace_mcp/credentials/` must have a matching entry in `config/token-rotation-schedule.json`, and vice versa. DMs every 6 h until reconciled (fail-closed per Larry's Q2 design decision). Activation env var: `OURLIBERTY_CREDENTIALS_HEALER_ENABLED=true` per the service file's commented activation snippet.

#### Deploy-targets drift pattern (E2.1)

The `sync-deploy-targets` script reconciles `config/deploy_targets.json` against the actual project list returned by the Vercel API (`GET /v9/projects`, personal Hobby account — no `teamId`). Three drift kinds: `MISSING_FROM_REGISTRY` (project exists on Vercel without a registry entry), `MISSING_FROM_VERCEL` (registry entry whose `vercel_project_id` returns 404), `NAME_MISMATCH` (both sides have the project but the human-readable names diverge). DMs every 24 h per persistent drift item (2 ticks at the 12 h timer cadence). Activation env var: `OURLIBERTY_DEPLOY_TARGETS_SYNC_ENABLED=true` per the service file's commented activation snippet. Vercel auth failures (401/403) emit a `critical`-severity `INFRASTRUCTURE_ALERT` and the unit exits non-zero so systemd surfaces it.

#### Deploy-notifier pattern (E2.2)

The `deploy-notifier` script polls Vercel's `GET /v6/deployments?state=READY,ERROR` every 2 min, filters by the GitHub repos in `config/deploy_targets.json`, and DMs Larry via the shared `larry_alerts` queue. READY → `warning`-severity DM with the preview URL. ERROR → `critical`-severity DM with the inspect link. BUILDING / QUEUED / INITIALIZING / CANCELED are skipped silently. Per-target `branch_filter` (null = match all branches; glob like `forge/*` for feature-branch-only) gates which deployments surface. PR number comes from `deployment.meta.githubPrId` first; falls back to `gh pr list --head <branch> --repo <repo>`; renders `PR #(unknown)` if both miss. Dedup is keyed by `<uid>:<state>` so a deployment that transitions READY → ERROR re-DMs; the same uid+state pair is never re-DMed. State file at `~/agents/state/deploy-notifier.json` capped at the 1000 most-recent entries (FIFO prune). Activation env var: `OURLIBERTY_DEPLOY_NOTIFIER_ENABLED=true` per the service file's commented activation snippet — default dry-run logs `would-DM` lines and fires a one-time activation prompt on first real event. Vercel auth failures (401/403) emit a `critical` `INFRASTRUCTURE_ALERT` throttled to one DM per 24 h; the unit exits non-zero so systemd surfaces transient errors via its retry path. Empty `deploy_targets` array → no API call, no DM, clean exit (`E2.3` lands the first real target).

### Medic dispatcher (auto-remediation step B, PR1)

`scripts/medic_dispatcher.py` is the cheap non-Claude trigger for the Medic alert-operator. It runs every 3 min (per the timer below), reads the non-allowlisted tail of `~/agents/blackboard/larry-alerts.jsonl` via Medic's own offset at `~/agents/state/medic-alerts-offset.txt` (never touches Beacon's offset), filters to owned classes per `config/medic-owned-classes.json`, and gates on `OURLIBERTY_MEDIC_ENABLED` + `~/agents/medic.disabled` + `~/agents/healers.disabled` + a stubbed rolling-5h rate-window check (real wiring lands in PR2 -- the stub logs a WARN every tick). If any owned alert qualifies, it writes a batch file under `~/agents/state/medic-batches/` and invokes `scripts/run_medic.sh`, which spins the Medic Claude operator in `agents/medic/`. Otherwise it advances the offset past unowned alerts and exits in milliseconds.

PR1 ships **escalate-only**: the operator classifies each owned alert (reversible / privileged / judgment per `config/medic-action-policy.json`) and writes one of:
- a diagnosis + recommended-command notification via `scripts/larry_alerts.py append_notification`,
- or an approval-request via `scripts/larry_alerts.py append_approval_request` when the recommended fix is privileged.

The act-branch is stubbed -- no `systemctl restart`, no re-dispatch writes, no file mutations. PR2 wires the reversible-action handlers (daemon restart + inbox re-trigger); PR3 wires the chain-bridge handler + approval-executor; PR4 wires Pulse Check observability + self-tuning.

```bash
# Install
sudo cp ~/agent-core/systemd/ourliberty-medic-dispatcher.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-medic-dispatcher.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Enable + start. Ships active by default via the service file's
# Environment=OURLIBERTY_MEDIC_ENABLED=1 line.
sudo systemctl enable --now ourliberty-medic-dispatcher.timer

# Confirm
systemctl list-timers ourliberty-medic-dispatcher.timer
journalctl -u ourliberty-medic-dispatcher.service -n 50 --no-pager

# Kill switches (in priority order):
# 1. Touch ~/agents/healers.disabled -- blanket switch for all healers + daemons.
# 2. Touch ~/agents/medic.disabled -- Medic-specific; lets Pulse + healers keep running.
# 3. sudo systemctl edit ourliberty-medic-dispatcher.service and set
#    OURLIBERTY_MEDIC_ENABLED=0; sudo systemctl daemon-reload.
```

Logs land at `~/agents/logs/medic-dispatcher.log` (dispatcher) and `~/agents/logs/medic.log` (operator wrapper). The handled-ledger at `~/agents/state/medic-handled-ledger.jsonl` is keyed by alert fingerprint (`source:subject`) with an attempt counter -- PR2 will enforce one-action-per-fingerprint based on this data.

### Dashboard API (E3.1)

`scripts/dashboard_api.py` is a FastAPI service that exposes the agent OS state as 7 read-only GET endpoints (`/health`, `/agents/status`, `/tasks/recent`, `/costs/today`, `/costs/week`, `/cycle-journal/recent`, `/healers/status`) for consumption by the upcoming E3.2 Next.js dashboard. The service binds to `127.0.0.1:8000` — Nginx + Let's Encrypt will front it in E3.3 on `https://api.ourliberty.dev/*`.

This is the **first non-stdlib runtime dependency on the droplet**, so the install path is two steps:

```bash
# 1. Install FastAPI + uvicorn (and httpx, used by the test client).
#    --break-system-packages is required on Debian 12+ per PEP 668; the
#    droplet doesn't use a venv for agent-core, so user-site is the right
#    landing zone.
pip3 install --user --break-system-packages fastapi 'uvicorn[standard]' httpx

# 2. Generate the auth token. 43-char URL-safe base64.
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
# Then append to ~/credentials/.env.larry (mode 0600):
#   DASHBOARD_API_TOKEN=<paste-here>
# The same token also goes into the Vercel project env vars (E3.2) —
# Production + Preview + Development — so the dashboard UI can reach
# the API through Nginx in E3.3.

# 3. Install the systemd unit.
sudo cp ~/agent-core/systemd/ourliberty-dashboard-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ourliberty-dashboard-api.service
sudo systemctl status ourliberty-dashboard-api.service

# 4. Smoke-test from the droplet itself (still localhost-only at this phase).
source /home/larry/credentials/.env.larry
curl -sS -H "X-Dashboard-Token: $DASHBOARD_API_TOKEN" \
  http://127.0.0.1:8000/health
# expected: {"status":"ok","version":"<sha>","agents_root":"/home/larry/agents",...}

# Auth check: no header → 401.
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8000/health
# expected: HTTP 401

# 5. Tail logs.
tail -F /home/larry/agents/logs/dashboard-api.log
```

The service has no timer — it's a `Type=simple` long-running daemon, restarted by systemd on failure (`Restart=on-failure`, `RestartSec=5s`). Auth is enforced via the `X-Dashboard-Token` header (constant-time compare via `secrets.compare_digest`). CORS allows exactly one origin: `https://dashboard.ourliberty.dev` — preview-deploy URLs are routed via a Vercel env-var indirection in E3.2 and do not widen CORS here. The FastAPI auto-docs route at `/docs` is gated by the same auth dependency.

**Credential discipline.** The `DASHBOARD_API_TOKEN` is registered in `config/token-rotation-schedule.json` with a 365 d rotation cadence. Rotation procedure lives at `docs/runbooks/rotate-dashboard-api-token.md` and covers BOTH the droplet `.env.larry` half AND the Vercel project env-var half — the token is shared by both sides and must rotate together to avoid breaking the dashboard.

### Supabase Python client (E4.0)

The Supabase Python client (`supabase-py`) is the droplet-side library for talking to the `ourliberty-pm-dashboard` Supabase project. E4.0a wires the credential discipline; E4.3+ adds the first consumer (`pm_writer`). The install is the same `pip3 --user --break-system-packages` pattern used for the Dashboard API:

```bash
# Install supabase-py (the official client). Brings httpx, postgrest,
# gotrue, realtime as transitive deps — ~50MB total on disk.
pip3 install --user --break-system-packages supabase

# Verify the import works.
python3 -c "from supabase import create_client; print('ok')"
# expected: ok
```

No systemd unit yet — `supabase-py` is a library, not a service. The first long-running consumer will be `pm_writer` (E4.3); its service file will land at that time.

**Credential discipline.** Three credentials are wired in `config/token-rotation-schedule.json` (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`), all sharing the rotation runbook at `docs/runbooks/rotate-supabase-keys.md`. The service-role key is on a 90-day scheduled cadence (RLS-bypassing → short cadence → bounded blast radius); the URL + anon key are revocation-only (rotate on suspected leak). First-time project setup is documented at `docs/runbooks/setup-supabase-pm-project.md`.

### Build-sequence advancer (E-orchestrator PR-S2)

`scripts/build_sequence_advancer.py` is a timer-driven oneshot (Type=oneshot) that polls `~/agents/blackboard/build-sequences/*.json` every 5 min and advances any active multi-step build sequence. Companion: the `heal-build-sequence-advancer-heartbeat` healer (above) DMs Larry when the per-tick heartbeat is >10 min stale (per spec § 5.4 failure mode 3).

The advancer ships **inactive by default** behind `OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=false` (mirrors the chain-event-shipper activation pattern). Enable only after PR-S3 (dashboard ladder UI) + PR-S4 (Beacon's 6 sequence shortcuts + Mirror's preflight DAG verification) have shipped and the kickoff round-trip is verified end-to-end. The blackboard directory is runtime-only — the daemon creates `~/agents/blackboard/build-sequences/` on its first tick; nothing in the repo tracks it.

```bash
# Install (service + timer for the advancer; service + timer for the healer).
sudo cp ~/agent-core/systemd/ourliberty-build-sequence-advancer.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-build-sequence-advancer.timer /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-heal-build-sequence-advancer-heartbeat.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-heal-build-sequence-advancer-heartbeat.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Enable both timers immediately. The advancer service itself is gated by
# the env var below; the healer fires on its own cadence regardless of
# the gate (it will DM Larry until activation, which is intentional — the
# DM is the operator's reminder that activation is still pending).
sudo systemctl enable --now ourliberty-build-sequence-advancer.timer
sudo systemctl enable --now ourliberty-heal-build-sequence-advancer-heartbeat.timer

# Activate (once Larry has verified PR-S3 + PR-S4 are in place):
sudo systemctl edit ourliberty-build-sequence-advancer.service
# In the editor, add:
#   [Service]
#   Environment="OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=true"
sudo systemctl daemon-reload

# Confirm.
systemctl list-timers ourliberty-build-sequence-advancer.timer ourliberty-heal-build-sequence-advancer-heartbeat.timer

# Smoke (after activation): trigger a tick by hand, watch the journal.
sudo systemctl start ourliberty-build-sequence-advancer.service
journalctl -u ourliberty-build-sequence-advancer.service -n 50 --no-pager
# Expect to see: `tick: files=N processed=M` near the bottom.

# Kill switches (in priority order):
# 1. Touch ~/agents/healers.disabled — blanket switch for all healers + daemons.
# 2. Per-healer: set OURLIBERTY_HEAL_BUILD_SEQUENCE_ADVANCER_DISABLE=true.
```

Full operating detail (ad-hoc pause/resume/cancel by direct file edit; corrupted-sequence handling; gate-mismatch diagnosis; spec-drift note on `auto_merge` event type) is at `runbooks/build-sequence-advancer.md`.

### Projects-store healer + launch-queue drain (projects-v3 P3 / P3 follow-up)

Two timer-driven oneshots complete the dashboard pipeline (`agents/beacon/specs/projects-v3-p3-pipeline.md` + `…-p3-followup-pipeline-flow.md`):

- `ourliberty-heal-projects-store.{service,timer}` — the projects-store **single committer** (~10 min). The dashboard write-endpoints (promote / advance / attach-spec / **archive** / launch) only land their deltas on `agents/beacon/projects.json` on disk; this healer normalizes the registry and commits the delta to `main`. It is the ONLY committer of that file (single-committer invariant) and carries `EnvironmentFile=.env.larry` for the git push.
- `ourliberty-launch-queue-drain.{service,timer}` — drains dashboard "Launch build" requests into Mirror-gated build sequences (5 min). Pure-filesystem non-committer (no git, no network, no credentials); see the service file header.

```bash
# Install (both service + timer pairs).
sudo cp ~/agent-core/systemd/ourliberty-heal-projects-store.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-heal-projects-store.timer /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-launch-queue-drain.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-launch-queue-drain.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Enable + start both timers.
sudo systemctl enable --now ourliberty-heal-projects-store.timer
sudo systemctl enable --now ourliberty-launch-queue-drain.timer

# Verify install landed AND the timers are active (the [[install-drift-timer-
# false-negative]] guard — "merged" never implies "installed"; confirm with
# is-active, not just list-timers).
systemctl is-active ourliberty-heal-projects-store.timer
systemctl is-active ourliberty-launch-queue-drain.timer
systemctl list-timers 'ourliberty-heal-projects-store.timer' 'ourliberty-launch-queue-drain.timer'
```

Both are auto-covered by the `systemd-install-drift` healer (it discovers every `systemd/*.{service,timer}` in the repo), so a missed `cp` DMs Larry the exact install commands within one 12 h tick.

| Healer | Cadence | What it watches for |
|---|---|---|
| `heal-projects-store` (projects-v3 P3) | 10 min | An on-disk delta on `agents/beacon/projects.json` (from a dashboard promote / advance / attach-spec / archive / launch write) → normalize + commit it to `main`. The single committer of the projects store; needs `EnvironmentFile=.env.larry` for git push. |

### Held-alert escalation (alert-pipeline-rework B5 + B6)

Two timer-driven oneshots that promote stale `hold` alerts into DMs. A `hold`
(alert-pipeline-rework B1) lands on the dashboard but is NOT DM'd; these jobs
decide a held line has sat unresolved long enough and APPEND a fresh `escalate`
line (B3) to surface it. Two deliberately-redundant paths:

- `ourliberty-held-alert-persistence.{service,timer}` — B5 persistence rule
  (every 10 min, aligns with the Pulse cycle). Promotes a fingerprint open ≥ 3
  consecutive cycles (~30 min), tracked in `~/agents/state/held-alert-probation.json`.
- `ourliberty-held-alert-backstop.{service,timer}` — B6 Pulse-independent
  backstop (every 15 min). Stateless: promotes any hold whose own `ts` is older
  than 30 min, so it fires even if the persistence timer or Pulse itself is dead.

Promote-once is queue-authoritative (a promotion line marks its fingerprint
resolved on the next scan), so the two paths never double-promote across cycles.

```bash
sudo cp ~/agent-core/systemd/ourliberty-held-alert-persistence.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-held-alert-persistence.timer /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-held-alert-backstop.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-held-alert-backstop.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ourliberty-held-alert-persistence.timer
sudo systemctl enable --now ourliberty-held-alert-backstop.timer

# Verify install landed AND the timers are active (merged != installed).
systemctl is-active ourliberty-held-alert-persistence.timer
systemctl is-active ourliberty-held-alert-backstop.timer
systemctl list-timers 'ourliberty-held-alert-*'
```

Both are auto-covered by the `systemd-install-drift` healer. Full ops detail
(the open-hold + promote-once model, tuning, kill switches) is at
`runbooks/held-alert-escalation.md`.

## Checking state

```bash
# What's running right now?
systemctl list-units 'ourliberty-*' --type=service

# What timers are scheduled?
systemctl list-timers 'ourliberty-*'

# Status of one unit
systemctl status ourliberty-beacon-bot.service

# Tail logs of one unit
journalctl -u ourliberty-beacon-bot.service -f

# Last 100 lines of all ourliberty logs combined
journalctl --since "1 hour ago" SYSLOG_IDENTIFIER=ourliberty-beacon SYSLOG_IDENTIFIER=ourliberty-cycle SYSLOG_IDENTIFIER=ourliberty-sync
```

## Stopping / disabling

```bash
# Stop a service (keeps it enabled — auto-starts on boot)
sudo systemctl stop ourliberty-beacon-bot.service

# Disable (won't auto-start on boot)
sudo systemctl disable ourliberty-beacon-bot.service

# Both
sudo systemctl disable --now ourliberty-beacon-bot.service
```

## Rolling back to tmux

If a systemd unit is misbehaving and you need to fall back to tmux while debugging:

```bash
sudo systemctl disable --now ourliberty-beacon-bot.service
bash ~/agent-core/scripts/beacon_telegram_bot.sh   # tmux launcher
```

## Updating after a code pull

After `git pull` in `~/agent-core/`, restart the affected services:

```bash
# If Python bot code changed
sudo systemctl restart ourliberty-beacon-bot.service
sudo systemctl restart ourliberty-forge-bot.service
# etc.

# If a unit file itself changed
sudo cp ~/agent-core/systemd/ourliberty-beacon-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ourliberty-beacon-bot.service

# Agent prompt files (.md) — no restart needed; next bot invocation picks them up
```

## Hardening notes

Each service runs with:
- `User=larry`, `Group=larry` — never root
- `ProtectHome=read-only` with explicit `ReadWritePaths` for the directories the bot legitimately needs
- `ProtectSystem=strict` — system dirs read-only
- `NoNewPrivileges=true` — can't escalate via setuid
- `PrivateTmp=true` — own /tmp, isolated from other services
- `MemoryMax=2G`, `TasksMax=64` — prevents runaway resource use

**`NoNewPrivileges` carve-out.** Two units intentionally OMIT `NoNewPrivileges`
because they must `sudo -n systemctl restart` other units:
`ourliberty-watchdog.service` (auto-recovers downed daemons) and
`ourliberty-sync.service` (deploy Step 7 — restarts daemons whose code a sync
changed). `NoNewPrivileges=true` blocks the kernel-level setuid path `sudo`
needs *regardless of sudoers config*, so under it every restart silently fails
(`restarted=0 failed=N`) and freshly-deployed daemons keep running stale
in-memory code. Both units preserve all their other hardening flags
(`ProtectSystem=strict`, `ProtectHome=read-only`, kernel protections,
`RestrictSUIDSGID`). A test in `scripts/tests/test_daemon_restart_manifest.py`
fails if `NoNewPrivileges=true` is ever re-added to the sync unit.

If a bot needs a path it doesn't currently have, edit the `ReadWritePaths=` line in the appropriate service file rather than relaxing other hardening.

## Phase activation checklist

| Service | When to enable | Larry-actions required first |
|---|---|---|
| `ourliberty-beacon-bot.service` | Now (replaces tmux) | Already done in Phase B |
| `ourliberty-sync.timer` | Now (low risk) | None — just enable |
| `ourliberty-agent-core-health.timer` | Now | None |
| `ourliberty-forge-bot.service` | Phase C activation | Create Forge bot via BotFather; install token |
| `ourliberty-mirror-bot.service` | Phase C activation | Create Mirror bot; install token |
| `ourliberty-pulse-bot.service` | Phase D activation | Create Pulse bot; install token |
| `ourliberty-cycle.timer` | Phase D activation | Anthropic API key in .env; first dry-run with Larry watching |
| `ourliberty-inbox-watcher.service` | Phase D2 activation | None — just enable; relies on existing `scripts/dispatch_lease.py` + `dispatch_validator.py` |
| `ourliberty-watchdog.timer` | Phase D activation | After cycle has been observed for ≥ 1 day |
| `ourliberty-ledger.timer` | After build-ledger-001 lands | None — `scripts/ledger_weekly.py` is pure-Python, no extra credentials. First Monday after enable triggers the inaugural run. |

### Ledger (weekly cost report)

After this PR lands:

```bash
sudo cp ~/agent-core/systemd/ourliberty-ledger.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-ledger.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ourliberty-ledger.timer
systemctl list-timers ourliberty-ledger.timer

# Manual smoke (writes a real report for the current Monday):
sudo systemctl start ourliberty-ledger.service
journalctl -u ourliberty-ledger.service -n 50
ls -la ~/agents/blackboard/ledger/
```

See `docs/operating-manual.md` §10.1 for full ops detail (recovery from missed run, manual `--week-ending` invocation, sentinel contract with Pulse Check I).

### CEO digest (N6 — daily + weekly CEO-voice summary)

`scripts/ceo_digest_generator.py` reads the period's chain activity (shipped PRs, auto-cleared decisions, decisions still waiting, spend, attention items) and writes a CEO-to-CEO plain-business summary that the dashboard's Approvals page reads off the `ceo_digest` chain_event. It calls `claude --print` for the voice pass and falls through to a deterministic jargon-free render if the LLM is unavailable. Two units: a **daily** timer (Mon–Sun 06:00 Larry-local, covers the prior day) and a **weekly** timer (Monday 06:00 Larry-local, covers the prior week).

> **⚠️ Timezone not yet Larry-DM-confirmed.** Both timers use `America/Denver` (the repo-documented Larry-local zone — system tz + ledger timer + Pulse MDT digests). The spec instructed confirming the tz by DM before wiring 6am; that confirmation has not happened. The generator reads `OURLIBERTY_LARRY_TZ` (default `America/Denver`), so the change point if wrong is a single env line in both service files plus the `OnCalendar=` zone. Verify before relying on the 6am boundary.

```bash
sudo cp ~/agent-core/systemd/ourliberty-ceo-digest-daily.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-ceo-digest-daily.timer /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-ceo-digest-weekly.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-ceo-digest-weekly.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ourliberty-ceo-digest-daily.timer
sudo systemctl enable --now ourliberty-ceo-digest-weekly.timer
systemctl list-timers 'ourliberty-ceo-digest-*'

# Manual smoke (writes a real digest for the prior period):
sudo systemctl start ourliberty-ceo-digest-daily.service
journalctl -u ourliberty-ceo-digest-daily.service -n 50
```

The `run_ceo_digest.sh` wrapper handles the concurrency lock (`.ceo-digest-{daily,weekly}.lock`), the `EMERGENCY_HALT` gate, and logging — same pattern as `run_ledger.sh`. Each run push-emits exactly one `ceo_digest` chain_event row.

### Weekly elevation retrospective (alert-pipeline-rework Phase 3)

The two-stage weekly retrospective. **Stage A** (`scripts/pulse_check_retrospective.py`, deterministic/LLM-free) mines the week's elevations + resolutions, buckets them by root signature with a resolution histogram + recurrence + probation dedup, and writes `~/agents/blackboard/retrospective-candidates.json` + the ledger. **Stage B** (`scripts/pulse_check_retrospective_author.py`, bounded `claude --print`) classifies each fresh bucket (automate-now / fix-permanently / keep-elevating), pre-drafts automate-now fixes from 6 allowed templates, and posts `phase:'proposed'` missions via `POST /api/system/missions/new`. The `run_retrospective.sh` wrapper runs Stage A then Stage B in order (Stage B reads Stage A's artifact + ledger) under the same lock + `EMERGENCY_HALT` gate pattern. Both stages emit a liveness heartbeat (`pulse-check-{retrospective,retrospective-author}.heartbeat`); the cadence is registered in `config/pulse-check-cadence.json`. One **weekly** timer (Monday 07:00 Larry-local — one hour after the CEO digest). The first live run doubles as Phase-4 verification (heal-pipeline-stall + medic-echo volume should have dropped).

> **⚠️ Timezone not yet Larry-DM-confirmed.** Same `America/Denver` caveat as the CEO digest timers above — verify the Monday-morning boundary before relying on it.

```bash
sudo cp ~/agent-core/systemd/ourliberty-retrospective-weekly.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-retrospective-weekly.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ourliberty-retrospective-weekly.timer
systemctl list-timers 'ourliberty-retrospective-*'

# Manual smoke (Stage A always runs; Stage B needs DASHBOARD_API_TOKEN + a
# reachable dashboard-api to post — without them it classifies + logs only):
sudo systemctl start ourliberty-retrospective-weekly.service
journalctl -u ourliberty-retrospective-weekly.service -n 80
```

### Parked-&-aging digest (Missions v2 Phase 2 — dashboard catch-me-up card)

`scripts/parked_aging_digest_generator.py` reads `agents/beacon/captures.json`, selects the `state == "parked"` captures the GC healer already flagged `aging: true` (it does **not** recompute aging — one definition lives in `heal_missions_card_gc.py`, `AGING_BUSINESS_DAYS = 5`), and writes a structured artifact to `~/agents/blackboard/parked-aging-digest.json` (parked count, aging count, the aging items with title + origin repo + calendar age). Stdlib-only, no LLM. The dashboard renders it as a read-only "what's parked & aging?" card (Phase 2 §6; promote/drop/snooze actions are Phase 3). One **daily** timer (06:15 Larry-local) regenerates it; the same wrapper run on demand (`run_parked_aging_digest.sh on-demand`) refreshes it without waiting for the cycle.

```bash
sudo cp ~/agent-core/systemd/ourliberty-parked-aging-digest.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-parked-aging-digest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ourliberty-parked-aging-digest.timer
systemctl list-timers 'ourliberty-parked-aging-digest.*'

# Manual smoke (writes/refreshes the artifact now):
sudo systemctl start ourliberty-parked-aging-digest.service
journalctl -u ourliberty-parked-aging-digest.service -n 50
cat ~/agents/blackboard/parked-aging-digest.json
```

The `run_parked_aging_digest.sh` wrapper handles the concurrency lock (`.parked-aging-digest.lock`), the `EMERGENCY_HALT` gate, and logging — same pattern as `run_ceo_digest.sh`. Each run overwrites the single artifact atomically (no append), so the dashboard always reads the latest.
