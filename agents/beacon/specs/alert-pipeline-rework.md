# Alert Pipeline Rework — Build Spec (consolidated, all phases)

**Date:** 2026-06-28 · **Status:** APPROVED — direction + all decisions + sub-decision defaults locked by Larry. Phase 1 launching as a build-sequence at 10:15pm MT 2026-06-28; Phases 2 → 3 chained to auto-run after Phase 1 via `depends_on`. **Single sequence `alert-pipeline-rework`.**

**Author:** Claude (audit + design), grounded against the live code.

## One-sentence intent
Nothing texts Larry until it has been *judged*; Medic *fixes* the reversible problems and *logs* them instead of narrating; and once a week the system mines everything that did reach Larry to turn recurring interruptions into permanent fixes or new automation — on the board for his accept.

## Locked decisions
- **D1** Hybrid DM gate · **D2** retrospective pre-drafts the fix (accept-gated) · **D3** P1 now, P2 then P3 chained after · **D4** spec in repo, built via one build-sequence.
- **Phase-2 sub-decisions:** S1 build the 2b machinery now + migrate only `outbox-notifier` into the gate initially (let P3 name the rest); S2 persistence escalates after **N = 3 cycles**; S3 backstop hard cap **30 min**; S4 close the watchdog/Medic marker drift with a **shared marker-path module**.
- **Phase-3 sub-decisions:** R1 actionable at **≥3×/7d OR ≥2 weeks running**; R2 run **Monday early AM MT**; R3 **no card** per keep-elevating bucket (count in summary); R4 pre-draft = **change-spec + diff sketch on the card, build-on-accept** (no auto-PR); R5 **fully accept-gated** (no auto-apply yet).

---

## Grounding facts (verified against current code)
- **chain_events ≠ phone DMs.** `chain_event_shipper.py:806-807` ships every `larry-alerts.jsonl` line as a `larry_alert` event regardless of route; `route` ∈ {escalate, closure, digest} is preserved in payload. Only non-`digest` lines DM via `beacon_telegram_bot.py:996`. The bot's offset cursor is forward-only/positional — once advanced past a line it never re-reads it; the bot reads *nothing* but `larry-alerts.jsonl`.
- **Medic engine exists, parked Stage-1.** `medic_actions.py _act_restart` + Stage-2 guard `_recent_peer_restart` (:243-310) are written + tested (11 tests). Brake = empty allowlist arrays in `config/medic-reversible-targets.json`.
- **Pulse Check 0** (`alert_triage_state.py`, `cycle-prompt.md` §3.0) decides routes but cannot DM and never writes back to the queue. The only way to turn a held alert into a DM is to APPEND a new escalate-routed line.
- **Pipeline-stall root cause already shipped** (#716/#719 `pipeline_live_state.py`, #725, #739). heal-pipeline-stall + medic move in lockstep — fixing the detector killed the echoes. Phase 4 = verify, not rebuild.

---

## PHASE 1 — Clean what you see (near-zero risk, ships standalone)
**1a. Segregate digest-route events out of the alerts feed.** `dashboard/lib/system-queries.ts` `getEscalationEvents()` (:81-90) and `getRecentPulseCycles()` (:296-318) — add `.or("payload->>route.is.null,payload->>route.neq.digest")` (PostgREST `payload->>` filter precedented at `app/api/operations/pr-pipeline/route.ts:144`). Keeps null-route escalation/sentinel rows; drops digest larry_alerts. No component change. **Correction:** the "permanently maxed unread" badge is NOT these alerts — `getPendingAttentionCount()` (:343-351) counts only `approval_request`+`clarify_request` with `read_at` null; that's a separate follow-up.
**1b. Add `info` severity below `warning`.** `larry_alerts.py:77` `VALID_SEVERITIES=('info','warning','critical')`; `info` cooldown branch (~:127, longest window); default `route='digest'` when `severity=='info'` in `append_alert` (~:325); info glyph (~:903). Already forward-compatible: `event_briefing.py:80-82` maps `info→safe`; dashboard `approval-queries.ts:36-45` `NormalizedSeverity` already includes `"info"`; shipper copies severity verbatim. Then reclassify routine emitters to `info`: dispatch-branch-cleanup "pruned N", heal-stale-daemon-code "auto-restarted:X" successes, **watchdog restart-success alerts**, forge-wip routine, missions-autoregister summaries, install-healed.
**1c. Cost-panel fix.** `getSpendDeltaVsYesterday()` counts `cost_usd` over 48h with no event_type filter → `ceo_digest` rollup ($1,155 once) counts as spend. Exclude `ceo_digest` (or stop writing cost_usd on digest events). (task_8b156836)
**Acceptance:** alerts panel shows zero digest rows; a test `info` alert never DMs + lands digest; spend panel matches `sum(session_done.cost_usd)`.

---

## PHASE 2 — Turn the fixer on + converge the lanes

### Part A — Activate Medic auto-restart (2a, low risk)
- **A1.** `config/medic-reversible-targets.json`: `restart_daemon_units=["ourliberty-inbox-watcher.service","ourliberty-outbox-notifier.service"]`, `retrigger_inbox_targets=["ourliberty-inbox-watcher.service"]` (mirrors `watchdog.py AUTO_RESTART_SERVICES` :77-80).
- **A2. Ownership (document, no code change):** systemd `Restart=on-failure` → watchdog (down/mem/flap, defers on flap) → Medic fills "systemd gave up, no peer active." Guard `_recent_peer_restart` refuses on any fresh shared marker (flap 30m / mem 15m / reconcile 30m); recurrence gate caps Medic at ~1 restart/fingerprint. Watchdog unchanged.
- **A3. Required hardening (S4):** extract the three marker-path builders into ONE shared module imported by both `watchdog.py` and `medic_actions.py` (today they're *replicated* at medic_actions.py:194-216 — silent drift risk). Add a test asserting parity.
- **A4. Scope/safety:** activates `sentinel:inbox-stall`→retrigger-watcher and `watchdog:*@critical`→restart-daemon only; heal-stale-daemon-code + heal-pipeline-stall stay judgment/escalate-only. `still-stale-after-restart:<unit>` keeps `route=escalate` (DMs) — real "didn't fix it" signal preserved. Success → `medic-handled-ledger.jsonl` (`outcome='acted'`), no DM/chain_event.
- **Acceptance:** simulated `watchdog:<svc>-flapping@critical` (no fresh peer marker) → Medic restarts+verifies+logs, zero DM; with a fresh flap marker → Medic skips (`outcome='skipped'`); still-stale still DMs.

### Part B — Hybrid DM gate (2b)
- **B1. Add non-DM `hold` route.** `larry_alerts.py:87` add `'hold'` to `VALID_ROUTES`; in `beacon_telegram_bot.py _check_pending_alerts` (~:996) add a skip-and-advance branch mirroring `digest`, guarded: skip-without-DM only when `route in ('digest','hold')` AND `severity != 'critical'`. Held lines still land on the dashboard.
- **B2. Critical always DMs.** Emit-time: `append_alert` forces `route='escalate'` when `severity=='critical'`. Read-time: the B1 branch re-checks `severity != 'critical'`.
- **B3. Promotion = APPEND (only feasible).** Pulse turns a held alert into a DM by APPENDING a fresh `route='escalate'` line (cursor is forward-only — rewrite is invisible). Use a distinct subject/promotion-marker so it isn't swallowed by the original cooldown bucket. No bot re-read logic.
- **B4. Incremental migration (S1).** Keep `DEFAULT_ROUTE='escalate'` for un-migrated sources. Migrate **only `outbox-notifier`** into `hold` initially; P3 names the rest. Graduation registry is the control surface.
- **B5. Persistence → escalate ONCE (S2, N=3).** Anchor on `promotion-probation.json` (`first_seen_ts` + promote-once, promote_alerts.py:259-282,400-404). A held fingerprint unresolved past **3 cycles** → append exactly ONE escalate line, never again.
- **B6. Pulse-independent backstop (S3, 30 min).** A Pulse-independent sweep (the bot, or a tiny always-on healer) escalates any `hold` line unresolved past **30 min** regardless of Pulse health — nothing held is ever lost.
- **Acceptance:** migrated-source `warning` does not DM next sweep (shows on dashboard), DMs only after Pulse appends or the 30-min backstop; `critical` DMs next sweep regardless of route; approval_request / Medic-unfixable DM instantly; held+unresolved past 3 cycles → exactly one DM; with Pulse stopped a held alert still DMs within 30 min.

---

## PHASE 3 — Weekly elevation retrospective (the keystone)

**Architecture (hybrid):** Stage A `pulse_check_retrospective.py` (new, pure Python) gather→normalize→bucket→resolution-join→probation accounting → `retrospective-candidates.json`. Stage B bounded LLM author (claude `--print`, modeled on `ceo_digest_generator.py`) classifies + writes briefings + pre-drafts automate-now fixes + posts proposed missions. Plumbing cloned from `pulse_check_ix.py`.

**Data + join.** Elevations = chain_events `escalation`, `larry_alert` where `payload.route=='escalate'`, `sentinel_alert`, `approval_request`, `needs_attention`. Resolutions = `larry_action`, `clarify_response`. **Join key:** `larry_action.payload.source_event_id → elevation.event_id` (yields per elevation: did Larry act, and how). Recurrence from `larry-alerts.jsonl` + the persistent retrospective ledger; live-unresolved cross-check `for-larry-escalations.json`; filter `is_fixture_task_id`.

**Bucketing (new logic).** Root signature = `source` + normalized subject (strip trailing `-YYYY-MM-DD`, `#<num>`/PR ids; collapse `:`-tail to family — reuse the `alert-translations.json` strip rules). Each bucket: count this period, weeks-recurring, resolution histogram (approve/reject/silence/ack/none), examples.

**Classification + confidence gate.**
- **AUTOMATE-NOW** — recurring (R1) AND benign (resolutions consistently same low-risk action, never reject-as-wrong) AND fix fits an **allowed template**. Allowed templates (bounds the LLM; each = an existing reversible surface): (1) route a benign source to `hold`; (2) add a subject to Medic `silenceable_subjects`; (3) add a unit to Medic restart/retrigger allowlist; (4) add/relax a Pulse Check 0 `auto-fix-patterns.json` entry; (5) reclassify an emitter to `severity='info'`; (6) a healer-side reconciliation. No template fit → FIX-PERMANENTLY.
- **FIX-PERMANENTLY** — recurring from a real defect / non-trivial fix → build mission (objective spec, no code).
- **KEEP-ELEVATING** — genuine per-instance decision → no card (R3), counted in summary.

**Pre-draft + board (R4).** Proposed mission brief carries the concrete change (template, file/key, value, diff sketch) — does NOT open a PR or edit config. Write via `POST /api/system/missions/new` → `new-mission-queue/` → `heal_orphan_autoregister` (single committer), **`phase:'proposed'`** (accept→drafting; dismiss→`acknowledged:true`). Dedup: registry-id-prefix skip + `promotion-probation`-style ledger keyed on root signature (dismissed stays quiet until it re-crosses threshold by a margin).

**Trend/closure (= Phase 4 verification).** Each run reports per-bucket trend vs last period (new/rising/falling/resolved). First run after Phase 1+4 confirms heal-pipeline-stall + medic echo volume actually dropped. One plain-language summary line, not per-bucket spam.

**Cadence/liveness/safety.** Weekly dedicated systemd timer (`OnCalendar=Mon …` MT, `Persistent=true`, `RandomizedDelaySec`), modeled on `ceo-digest-weekly.timer`. Self-gate with a *validated* per-ISO-week artifact sentinel (not bare `.exists()`). Wrap in `pulse_check_heartbeat.run_check('retrospective', …)`. Bound the LLM with `timeout` + cost capture. Read-only except queue/artifact/ledger writes; never commits.

**Acceptance:** seeded recurring+benign elevation → one `proposed` card, automate-now, concrete pre-drafted fix; declining prevents re-proposal; defect-class → fix-permanently mission; decision-class → no card (in summary); weekly trend line proves Phase-4 fixes; truncated artifact doesn't permanently suppress.

---

## PHASE 4 — Pipeline-stall root cause (mostly shipped; verify + finish)
Verify the Jun 26-27 fixes (#716/#719/#725/#739) drove heal-pipeline-stall + medic-echo DMs toward zero — **this is Phase 3's first weekly run**. Finish the one edge: confirm "persisted N cycles → single needs-you" (overlaps B5). Do NOT rebuild (`pipeline_live_state.py` is in).

---

## BUILD SEQUENCING — one DAG, auto-runs P1 → P2 → P3
Sequence `alert-pipeline-rework`, kicked at 10:15pm MT by the staged launcher (Mirror DAG preflight → advancer). Cross-repo parallel; agent-core file-overlap serialized; Mirror DAG preflight verifies the parallel declarations.
- `p1-dashboard` (dashboard) ← []  · 1a + 1c
- `p1-agent-core` (agent-core) ← []  · 1b + emitter reclassification
- `p2a-medic` (agent-core) ← [p1-agent-core]  · A1+A3 (allowlist + shared marker module)
- `p2b-machinery` (agent-core) ← [p1-agent-core]  · B1–B3,B5,B6 (parallel w/ p2a — disjoint files)
- `p2b-migrate` (agent-core) ← [p2b-machinery]  · B4 (outbox-notifier → hold)
- `p3a-retro-prep` (agent-core) ← [p2a-medic, p2b-machinery]  · Stage A
- `p3b-retro-author` (agent-core) ← [p3a-retro-prep]  · Stage B + weekly timer
Each step builds + Mirror-reviews + auto-merges; the advancer dispatches the next when deps reach `merged`, DMs Larry on kickoff/each-merge/completion, and **pauses + DMs on any failure**. The chain self-paces across capacity windows. P3b's first live run = Phase 4 verification.
