# Spec: Pulse `/cycle` Upgrade — Active Meta-Orchestrator (Joe pattern adoption)

**Status:** Draft (awaiting Larry approval — sub-phase of E4 follow-on / Phase F prep)
**Author:** Claude-as-Forge (written 2026-05-26, research surfaced through Joe's `gm-agent-core/runbooks/cycle-prompt.md` v12)
**Approver:** Larry (pending)
**Phase:** Phase E4 follow-on. Not a strict E4 sub-phase because it's agent-OS hardening, not unified-PM-dashboard work. Sequenced AFTER E4.4d ships so it can consume the `chain_events` data layer.
**Predecessor:** PR #105 (chain-discipline v2: marker.py mandate + stale-daemon healer), PR #107 (heal_pipeline_stall.py — zero-LLM watchdog constellation)
**Successor:** Phase F (per-product Supabase + customer products) becomes safe to enter once the agent OS is self-orchestrating.
**Companion docs:** `runbooks/cycle-prompt.md` (today's Pulse `/cycle` prompt; gets rewritten by this upgrade), `agents/pulse/CLAUDE.md` (Pulse persona; small additions).

---

## 1. Problem statement (why upgrade)

Today's Pulse `/cycle` is a passive observer. It runs on a fixed 4-hour cadence, performs A-H drift checks, writes a journal entry, and exits. When something stalls between cycles, the system silently sits with the stall until either (a) the next cycle catches it 4 hours later, (b) Larry asks, or (c) a healer happens to surface it.

The 2026-05-25 session made the cost concrete. Three multi-hour debugging windows landed because the chain went silent in shapes Pulse didn't catch:

- The 71-min Mirror hang on PR #101 — invisible to Pulse's drift checks because the watcher itself was healthy.
- The page-cache "leak" misdiagnosis — Pulse caught the memory pressure but framed it as anonymous RSS until the off-cycle investigation correctly characterized it.
- The PR #103 stale-daemon situation — the fix was on disk for 4 hours before manual restart; Pulse had no signal that the running notifier was running pre-fix code.

Each of these was discovered by Larry asking "status?" and Claude doing forensics via SSH. That's manager work — performed manually because Pulse can't do it on the cadence and depth it needs to.

**Joe's `gm-agent-core` runs the same observation agent (his `sage`-paired `/cycle`) in a fundamentally different mode.** His `cycle-prompt.md` is 2,145 lines of doctrine. Three structural differences that matter:

1. **Multi-tier cadence with self-escalation** (Tier 1 = 5 min / Tier 2 = 15 min / Tier 3 = 25 min). Any signal in any iter forces immediate Tier 1.
2. **Per-iter MANDATORY issue-hunt protocol** (cumulative log-noise scan + Telegram thread sweep + silent-loop liveness + pending-directive check). Runs EVERY iter, regardless of tier. The default cadence question stops being "have I been at Tier 3 long enough to stay?" and becomes "what did I find this iter that demands Tier 1 attention?"
3. **Active pipeline-driver behavior.** When the pipeline is quiet AND there's open leverage work, the cycle stages a new planning sweep / dispatches the next leverage point. Quiet-and-empty ≠ quiet-and-finished.

Plus a **PRIME DIRECTIVE**: every iter must reduce future cycle workload. Intervention + systemic-fix in the SAME iter. The intervention-to-systemic-fix ratio trends monotonically toward zero, with empirical-verification gating (a HEALED event from a new zero-LLM healer, or a noise pattern drops >50%, or an orphan condition self-recovers — TODOs don't count).

E4.4d ships the *data layer* (`chain_events` table, `agent_sessions` VIEW, threshold config-as-data) Pulse needs to do this efficiently. PR #107 ships the *zero-LLM watchdog floor* (`heal_pipeline_stall.py`) that catches deterministic stalls without LLM cost. This spec captures the *LLM-judgment layer on top* — Pulse becomes the holistic interpreter that complements the watchdogs.

---

## 2. Decisions locked (research + draft author judgment, ready for Larry override)

| # | Decision | Locked value | Rationale |
|---|---|---|---|
| A | Cadence tier values | **Tier 1 = 5 min, Tier 2 = 15 min, Tier 3 = 30 min** | Match Joe's Tier 1 + 2 (proven cadence shape over many of his iters); widen Tier 3 from his 25 min to 30 min because our pipeline is less customer-facing than his and 30 min idle is fine. Re-tune via Pulse Check III once 30+ days of cycle-action data exists. |
| B | Self-escalation trigger | **Any non-empty result from any per-iter check forces immediate Tier 1.** Stays at Tier 1 until 3 consecutive iters return clean across all checks. | Match Joe's pattern exactly. The "what did I find this iter" framing is load-bearing. |
| C | Per-iter MANDATORY checks (run every iter) | **Five checks** — (1) cumulative log-noise scan, (2) Telegram thread sweep, (3) chain_events stall scan, (4) pending-Larry-directive check, (5) stale-daemon-code check | Joe has 4 (his log + Telegram + silent-loop + pending-directive). We add chain_events stall scan as #3 because E4.4d gives us a queryable surface that Joe doesn't have (and #5 reflects the staleness signal that bit us on PR #103). |
| D | Pipeline-driver behavior | **Conservative V1: surface-only.** Pulse identifies "next leverage" work when pipeline is quiet AND there are open spec docs or recurring stalls; she WRITES the proposal to a new `~/agents/blackboard/pulse-leverage-proposals.json` and DMs Larry. Larry approves → Beacon dispatches. **No auto-dispatch in V1.** | Joe auto-dispatches because his pipeline is customer-facing and he has Atlas reviewing. Ours is greenfield — Larry-approval-before-dispatch is the correct posture until we have a customer-readiness queue to drive against. Can lift to auto-dispatch later if Larry says so. |
| E | PRIME DIRECTIVE adoption | **YES — every cycle iter must reduce future workload.** Intervention + systemic-fix in same iter. Empirical-verification gating (HEALED event fires, noise drops >50%, or orphan self-recovers — TODOs don't count). | The single most valuable Joe pattern. Adopting it makes Pulse's existence sustainably cheaper over time, not more expensive. Joe's exact ratio framing carries over verbatim. |
| F | Phase 4 verification window | **Adopt: prompt-edit verification waits for ≥1 post-merge agent-process spawn before declaring verified.** | We literally hit this bug today (PR #103 fix sat in stale daemon for 4 hours). Pulse explicitly tracks the "post-merge process generation" and verifies behavior against newly-spawned sessions, not pre-merge ones. |
| G | WARN-vs-INFO calibration | **Adopt: WARN reserved for actionable; informational signals demoted to INFO.** Pulse audits log-level usage every cycle. | Cheap noise reduction. Joe's iter 8/10 lessons capture the heuristic well: optional config missing → INFO, successful enforcement events → INFO, routine retries within tolerance → INFO. WARN is for threshold breaches and unexpected failures. |
| H | Plan-First Protocol adoption | **DEFER to a separate sub-phase.** Not in V1 of the Pulse cycle upgrade. | Joe's PLAN-FIRST mandates schema-of-truth + prior-PR archaeology + open-issues review + live-customer-impact survey + best-practice reuse for every Sage→Luma dispatch. We don't have customers yet; the live-customer-impact survey is N/A. Adopting the full discipline shape requires its own design round. Worth doing post-customer; not blocking. |

**Decisions A-G I have locked based on research + author judgment. Decision H is explicitly deferred. Larry can override any of these in the approval-review pass.**

---

## 3. Success criteria

The upgraded Pulse `/cycle` is working when ALL of the following are true:

- Pulse's median cycle cadence drops to ~15 min during active development, ~30 min during idle. No more 4-hour gaps between observations.
- Today's 2026-05-25 incident chain replayed against the upgraded cycle would have produced ≥3 DMs to Larry within the same time window where he had to ask: (a) Mirror hang at minute 20 (post-PR-105 heal_pipeline_stall.py catches this; Pulse confirms holistically), (b) outbox-notifier stale-code alert at minute 5-10 post-merge (Check #5), (c) page-cache investigation auto-proposed via "memory pressure rising but anon RSS healthy" judgment call (Check #1 log-noise + Pulse interpretation).
- The intervention-to-systemic-fix ratio across the first 30 cycle iters trends toward zero. By iter 30, the cycle should be intervening rarely because earlier iters shipped systemic fixes that prevented recurrence.
- The pipeline-driver layer surfaces at least one "next leverage" proposal per quiet day. Larry's approval rate on those proposals is ≥80% (signal that Pulse is finding real work, not noise).
- Cycle-prompt.md doubles in size from current to ~800-1200 lines (adopts most of Joe's lessons; stays smaller than his 2,145 because we skip Plan-First and the FBV2-customer-readiness specifics).

---

## 4. Out of V1 scope (explicit deferrals)

- **Plan-First Protocol** (decision H). Schema-of-truth + prior-PR archaeology + open-issues review + live-customer-impact + best-practice reuse for every Forge dispatch. Defer until we have a customer.
- **Auto-dispatch of leverage proposals** (decision D, second half). Pulse surfaces; Larry approves; Beacon dispatches. No fully-autonomous next-work dispatch in V1.
- **Customer-readiness ranking framework.** Joe's "highest leverage = FBV2 customer-readiness critical path" depends on having a product. Our equivalent ranking framework develops post-Phase-F.
- **Multi-product cycle scope.** Joe's cycle operates over one growth-mastery product. Ours operates over the agent OS itself. Phase F may introduce multiple products on the same droplet, at which point cycle scoping becomes a real question.
- **Agent-browser tool integration** (Joe iter 42 lesson). Vercel agent-browser CLI for walkthroughs at 5-10× token savings vs Playwright MCP. Worth adopting later for E4.4 UI walkthroughs. Not in this spec.
- **Retroactive cycle-action backfill.** When this ships, treat all pre-upgrade cycle journal entries as legacy. Don't try to retroactively classify them against the new framework.

---

## 5. Architecture

### 5.1 Multi-tier cadence

Three tiers, all driven by the same systemd timer + an internal-state file determining which prompt the cycle invokes:

| Tier | Cadence | Triggers entry | What runs |
|---|---|---|---|
| 1 | 5 min | Any signal in any check (from any tier's run) | Full deep iteration: all 5 mandatory checks + chain_events scan + pipeline-driver evaluation + PRIME DIRECTIVE accounting |
| 2 | 15 min | 3 consecutive Tier-1 iters return clean across all checks | All 5 mandatory checks (same as Tier 1) + lighter pipeline-driver evaluation (only if quiet) + PRIME DIRECTIVE accounting |
| 3 | 30 min | 3 consecutive Tier-2 iters return clean | All 5 mandatory checks + skip pipeline-driver evaluation + PRIME DIRECTIVE accounting |

Implementation: `~/agents/state/cycle-tier.json` records `{tier, consecutive_clean, last_signal_at}`. The systemd timer fires every 5 min; the cycle script reads tier state, sleeps until the next appropriate-tier window if needed (e.g., Tier 3 means skip 5 of every 6 fires), runs the iter, updates state.

**Cost calibration.** At Tier 1 (5 min) on Opus = ~$0.30/run × 12/hr = $3.60/hr active, ~$86/day if active 24/7. Realistic active+quiet mix: $25-40/day. Worth it if it eliminates today's 3-hour manual debugging sessions even once a week.

### 5.2 Per-iter MANDATORY issue-hunt protocol

These five checks run EVERY iter regardless of tier. Order matters — Pulse executes them in sequence, recording findings as she goes.

**Check 1 — Cumulative log-noise scan.**
Read `~/agents/logs/outbox-notifier.log`, `~/agents/logs/inbox-watcher.log`, and journalctl for each `ourliberty-*.service` over (last 30 min, last 1 h, last 24 h) windows. Count distinct WARN/ERROR signatures. Flag any pattern firing >5/hour OR >50/24h as a `systemic-fix-target`. Cross-reference with `WARN-vs-INFO calibration` (decision G) — informational-masquerading-as-WARN signals get demoted, real signals get a systemic-fix dispatch proposal.

**Check 2 — Telegram thread sweep.**
Read `~/agents/logs/<agent>_telegram_bot.log` for last 4 h. Flag (a) Larry's `<- 7998341473` messages that look like directives or questions (use simple keyword heuristics: `?`, `please`, `should we`, `do X`, `please fix`, `why is`), and (b) agent messages containing problem keywords (`error`, `failed`, `regression`, `stuck`, `blocked`, `rate limit`, `timeout`, `crash`). Cross-reference Larry's directives against open Forge/Mirror dispatches — anything orphaned (Larry asked >24h ago, no PR or open task tracks it) forces Tier 1 + drives a DM to Larry asking for clarification.

**Check 3 — chain_events stall scan.**
Query Supabase `chain_events` table + `agent_sessions` VIEW for: (a) running sessions exceeding their `(agent, task_type)` threshold per E4.4d D4, (b) Mirror-PASS markers with no corresponding AUTO_MERGE within 30 min, (c) Forge build-completes with no PR opened within 2 h, (d) Mirror generic-notifies (depth=1) with no `marker-notified` follow-up within 30 min (marker-shape drift signal). Cross-references with `heal_pipeline_stall.py` heartbeat — if that healer is fresh, Pulse trusts its findings and only adds holistic context; if stale, Pulse runs the checks herself.

**Check 4 — Pending-Larry-directive check.**
Scan Larry's last 24 h of Telegram messages for explicit directives (per Check 2's heuristics). Match each against PRs opened in the same window or specs landed. Anything orphaned (Larry said do-X, no chain artifact tracks it) forces this iter to address it OR DM Larry: "you said X — still want me to act on it?".

**Check 5 — Stale-daemon-code check.**
Read `~/agents/blackboard/heal-stale-daemon-code-state.json` (the PR #105 healer's state file). If any daemon's script-mtime > service-start-timestamp with delta > 5 min, surface in this iter. Don't wait for the healer's next 30-min cycle to DM Larry — Pulse's cycle catches it faster.

**Tier reset rule.** If ANY of the five checks returns non-empty results, this iter forces immediate Tier 1. Stays at Tier 1 until 3 consecutive iters return clean across all 5 checks.

### 5.3 PRIME DIRECTIVE — intervention + systemic-fix in same iter

Verbatim adoption of Joe's framing:

> **Every cycle iteration must reduce the cycle's future workload. If you intervene one-off, you ALSO ship the systemic fix that prevents that intervention from being needed again — in the SAME iter. The intervention-to-systemic-fix ratio must trend monotonically toward zero.**

**Empirical verification gating.** A "systemic fix" only counts toward the ratio if AT LEAST ONE of these is verified within 24 h:
- `HEALED:` event fires from a new/edited zero-LLM healer script
- Target noise pattern reduces by >50% measured over 6 h
- An orphan/stuck condition self-recovers without cycle intervention

**Clock anchor + source (added 2026-05-26 per Mirror PR #108 review).** The 24 h window is measured in **wall-clock UTC** from the **systemic-fix dispatch event** as recorded in `chain_events` (the row whose `event_type` is the dispatch and whose `task_id` corresponds to the fix). For prompt-edit / CLAUDE.md / cycle-prompt.md fixes specifically, use the § 5.5 fresh-process-spawn timestamp instead — those fixes don't take effect at dispatch time and the 24 h window must start when a fresh agent process can actually exhibit the new behavior. This dual anchor is deliberate; the cycle-action ledger records both the dispatch ts AND the verification-anchor ts so any future iter can reproduce the verification window from chain_events alone.

The 6 h noise-pattern-drop window (second bullet above) is measured in wall-clock UTC from the same dispatch event for code/healer fixes, or from the fresh-process-spawn timestamp for prompt-edit fixes.

TODO comments, "I'll watch this" deferrals, and untested patches DO NOT count.

**Healer first-execution accounting.** When a new healer's first run drains an existing backlog AND establishes future prevention, that single ship counts as ONE systemic fix — not "1 one-off + 1 systemic." The healer IS the systemic mechanism; its first execution is the empirical proof.

**Cycle-action ledger.** Every iter appends a row to `~/agents/blackboard/cycle-actions.jsonl` with `{ts, tier, interventions: [...], systemic_fixes: [...], ratio_this_iter, ratio_cumulative_30d, ratio_trend}`. Pulse reads this on each cycle to know if the ratio is improving.

### 5.4 Pipeline-driver behavior (conservative V1)

When all 5 mandatory checks return clean AND the pipeline is quiet (no open envelopes in any inbox, no running sessions, no recent dispatches in last 30 min), Pulse evaluates:

1. **Spec backlog.** Are there spec docs at `agents/beacon/specs/*.md` with sub-phases unimplemented? (E.g., E4.4b kanban + drag-drop, E4.4c CRUD + forms, E4.3 pm_writer, E4.4d PR-A/B/C/D after this spec ships.)
2. **Recurring-stall surface.** Any `pulse-escalations.json` entries with `needs_response=true` >24h?
3. **Self-optimization backlog.** Any project on the PM dashboard (`Operational Config Self-Optimization`) ready for the next sub-task?

If any of (1)-(3) yields a clear next-leverage candidate, Pulse writes a proposal to `~/agents/blackboard/pulse-leverage-proposals.json` with `{ts, candidate, source, suggested_dispatch_template, applied: false}` AND DMs Larry: "next-leverage candidate: dispatch <task>? [approve|reject|defer]".

**Larry approves** → Beacon dispatches via the standard `approve <leverage>-<date>` Telegram shortcut → Forge implements.
**Larry rejects** → Pulse records the reject reason for future-Pulse to learn the pattern.
**Larry defers** → 24 h cooldown before re-proposing.

**No auto-dispatch.** Larry approval is mandatory in V1.

### 5.5 Phase 4 verification window

When Pulse fires a systemic-fix dispatch (e.g., a new healer ships), she does NOT mark it `verified` until:

1. The fix's commit lands on `main` AND has been pulled into the deployed location.
2. The relevant daemon/agent has been restarted post-merge (Pulse checks `systemctl show <unit> --property=ActiveEnterTimestamp` against the merge timestamp).
3. ≥1 fresh post-merge process has been observed to behave per the new contract.

For prompt-edit fixes (CLAUDE.md changes), step 3 means: ≥1 NEW Claude session (spawned post-merge) has been observed to follow the new rule. Joe's iter 9 lesson: pre-merge sessions carry the old prompt until they exit and respawn.

**Verification gate prevents PRIME DIRECTIVE inflation.** A fix that's merged but hasn't reached a fresh process doesn't count yet. Forces cycle to track the gap explicitly.

### 5.6 WARN-vs-INFO calibration heuristic

Pulse audits log-level usage every cycle. Patterns to demote to INFO:
- Optional config keys missing (deliberate non-error state)
- Successful enforcement events (the rule worked as designed)
- Routine retries within tolerance (RETRY 1 of 3, not yet escalation-worthy)
- Idle-state observations ("0 queued tasks, idle")

WARN reserved for:
- Actionable problems requiring human or healer response
- Threshold breaches (per E4.4d D config)
- Unexpected failures
- Recoverable conditions that may become unrecoverable without action

When Pulse finds a high-volume noise pattern in Check 1, she asks first: WARN-correct (real signal) or WARN-miscalibrated (informational masquerading)? Demote-to-INFO is the right systemic fix for the latter; root-cause-fix for the former.

### 5.7 Data sources Pulse reads

After E4.4d ships, Pulse's input surface is:

| Source | What she reads | Frequency |
|---|---|---|
| `chain_events` table (Supabase) | All chain events for stall scan, retry analysis, throughput metrics | Every iter |
| `agent_sessions` VIEW (Supabase) | Currently-running sessions for liveness | Every iter |
| `~/agents/logs/outbox-notifier.log` | Recent notifier activity for log-noise scan | Every iter |
| `~/agents/logs/*_telegram_bot.log` | All 4 agent bots for thread sweep | Every iter |
| `journalctl -u ourliberty-*.service` | Systemd journal for daemon stats + retry-exhausted detection | Every iter |
| `~/agents/blackboard/heal-stale-daemon-code-state.json` | Stale-code findings (consume don't recompute) | Every iter |
| `~/agents/blackboard/heal-pipeline-stall-state.json` | Stall findings (consume don't recompute) | Every iter |
| `~/agents/blackboard/pulse-escalations.json` | Her own prior escalations for `needs_response` follow-up | Every iter |
| `~/agents/blackboard/cycle-actions.jsonl` | Her own action history for PRIME DIRECTIVE ratio | Every iter |
| `gh pr list` across both repos | PR pipeline state for leverage-proposal evaluation | Quiet iters only |
| `agents/beacon/specs/*.md` | Spec backlog for leverage-proposal evaluation | Quiet iters only |

---

## 6. Implementation staging (3 PRs)

### PR-α: cycle-prompt.md rewrite (~½ day, ~$5 LLM)

Rewrite `runbooks/cycle-prompt.md` to encode the Joe doctrine adapted to our system. Add sections for:

- Multi-tier cadence (§ 5.1)
- Per-iter MANDATORY issue-hunt protocol — all 5 checks (§ 5.2)
- PRIME DIRECTIVE + empirical-verification gating + cycle-action ledger (§ 5.3)
- Pipeline-driver behavior (§ 5.4)
- Phase 4 verification window (§ 5.5)
- WARN-vs-INFO calibration heuristic (§ 5.6)
- Data sources (§ 5.7)

Plus tier-state machine documentation: how `~/agents/state/cycle-tier.json` is read/written, how tier escalation/de-escalation works, what happens on cycle-prompt edits while a session is mid-execution.

Estimate: ~600-800 lines of new prompt content. Mirror reviews for: faithful capture of the 8 locked decisions A-H, no contradictions with `agents/pulse/CLAUDE.md`, the 5 mandatory checks are concretely executable (no "vibe-check" prose), the tier-state machine is unambiguous.

### PR-β: cycle-tier state machine + cycle-actions ledger (~½ day, ~$3 LLM)

Implementation of the support infrastructure cycle-prompt.md references:

- `scripts/cycle_tier_state.py` (NEW) — small helper library: `get_current_tier()`, `record_iter_result(checks_clean: bool)`, `advance_tier()`. Reads/writes `~/agents/state/cycle-tier.json`.
- `scripts/cycle_actions_ledger.py` (NEW) — small library: `append_action(tier, interventions, systemic_fixes)`, `compute_ratio_30d()`. Reads/writes `~/agents/blackboard/cycle-actions.jsonl`.
- `scripts/tests/test_cycle_tier_state.py` + `test_cycle_actions_ledger.py` — unittest coverage.
- `scripts/run_cycle.sh` (UPDATED) — read tier state at startup, sleep-until-next-tier-window if appropriate, invoke `/cycle` with the right prompt context.
- `systemd/ourliberty-cycle.timer` (UPDATED) — change `OnUnitActiveSec=4h` → `OnUnitActiveSec=5min` (Tier 1 cadence; the script self-throttles to Tier 2/3 windows).

### PR-γ: agents/pulse/CLAUDE.md additions (~½ day, ~$2 LLM)

Small additions to Pulse's persona doc:

- Reference the new cycle-prompt.md sections (don't duplicate; cycle-prompt is the canonical operational doctrine).
- Add an explicit "When you wake up for a cycle iter, here's the order you operate in" section.
- Add the `cycle-actions.jsonl` append discipline.
- Add the WARN-vs-INFO calibration heuristic as a top-of-mind rule.

---

## 7. Effort + cost estimate

| PR | LLM cost | Wall clock | Larry actions |
|---|---|---|---|
| PR-α cycle-prompt.md rewrite | ~$5 | ½ day | 30-min review of the new prompt; values check on cadence numbers |
| PR-β state machine + ledger | ~$3 | ½ day | 5 min apply systemd timer change + restart |
| PR-γ Pulse CLAUDE.md additions | ~$2 | ½ day | None |
| **Total** | **~$10** | **~1.5 days** | **~35 min** |

**Ongoing cost.** Cycle now fires every 5/15/30 min instead of 4h. Conservative LLM cost projection: $30-50/day during active development, $15-20/day during idle periods. ~$1000-1500/month at the upper bound — significant relative to today's spend but justified if the upgrade prevents even 1-2 multi-hour debugging sessions per week.

**Cost-reduction lever.** Tier 3 (30-min cadence during steady-state) on Sonnet 4.6 instead of Opus 4.7 drops the cost by ~5×. Pulse Check III could auto-tune the (tier, model) pairing once cost data accumulates. Backlog candidate.

---

## 8. Risks + rollback

| Risk | Mitigation | Rollback |
|---|---|---|
| Tier 1 cadence proves too expensive in practice | Pulse Check III analyzes 30-day cost data; auto-proposes lengthening Tier 1 to 7 or 10 min if cost > $50/day | Edit `cycle-prompt.md` tier constants; re-merge |
| 5 mandatory checks compete for context budget — Pulse runs out of tokens mid-iter | Each check has a hard time budget (15 sec for each scan); Pulse short-circuits and proceeds to the next check if budget exceeded | Tighten budgets in cycle-prompt.md |
| Pipeline-driver proposes wrong-direction leverage (Larry rejects often) | Reject rate >50% over 7 days forces Pulse Check III to recalibrate the leverage-ranking heuristic | Disable pipeline-driver block in cycle-prompt.md |
| PRIME DIRECTIVE causes Pulse to over-engineer healers (each iter ships a new script) | Empirical-verification gate prevents counting unverified fixes; healer-first-execution accounting prevents inflation | Cycle-prompt.md tightens the "what counts as a systemic fix" definition |
| Tier-state file gets corrupted (atomic write fails mid-write) | `cycle_tier_state.py` validates schema on read; on corruption, resets to Tier 1 with a journal entry | Manual reset: `echo '{"tier":1,"consecutive_clean":0,"last_signal_at":null}' > ~/agents/state/cycle-tier.json` |
| Cycle blocks itself in a tight loop (Tier 1 forces, can't escape) | 3-clean-iter de-escalation rule; explicit pause command Larry can issue (`/pulse-pause 1h`) | Manual: `touch ~/agents/healers.disabled` halts all Pulse cycles |
| Cycle catches false-positive Larry directives in the Telegram thread sweep | Pulse DMs Larry for clarification before acting on ambiguous directives ("did you mean for me to dispatch X?") | Tighten Check 2 keyword heuristics; could move to LLM-judgment-only on ambiguous matches |
| Cycle-action ledger grows unbounded | Daily rotation: `cycle-actions-YYYY-MM.jsonl`; older months archived to `.archive/` | Manual archive + start fresh |

---

## 9. Acceptance criteria (for the implementation phase)

Per PR: PR-α, PR-β, PR-γ each ship with own acceptance. The full V1 cycle upgrade is accepted when:

- [ ] All 3 PRs merged, Mirror PASS on each, AUTO_MERGE fires cleanly (validates that PR #105 + PR #107 + this spec compose well)
- [ ] First post-merge Pulse `/cycle` iter runs at Tier 1 (5 min) and exits cleanly with all 5 mandatory checks executed
- [ ] After 3 clean iters at Tier 1, Pulse self-de-escalates to Tier 2; after 3 clean iters at Tier 2, to Tier 3
- [ ] Any subsequent signal in any check escalates back to Tier 1 within one cycle
- [ ] Cycle-actions.jsonl accumulates entries with intervention + systemic_fix counts; the ratio metric is computable and trends toward zero across the first 30 iters
- [ ] At least one leverage proposal lands in `pulse-leverage-proposals.json` within the first 24 h of operation (sign that pipeline-driver is finding real work, not just noise)
- [ ] Phase 4 verification window catches at least one "merged but not yet effective" case — Pulse explicitly waits for a fresh process before marking a systemic fix `verified`
- [ ] Larry can spend a working day on something else and trust the upgraded cycle to surface anything that needs his attention

---

## 10. Source notes (where this design came from)

- `GrowthMastery-ai/gm-agent-core/runbooks/cycle-prompt.md` v12 (2026-05-02, "Airtight Plan v1 Joe approved") — the 2,145-line operational doctrine that this upgrade adapts. Read in full during 2026-05-26 morning research session. Key sections inlined to our system: PRIME DIRECTIVE, multi-tier cadence, per-iter MANDATORY checks, Phase 4 verification, WARN-vs-INFO calibration. Sections explicitly NOT adopted: PLAN-FIRST PROTOCOL (deferred), REUSE-NOT-BUILD BIAS (customer-product specific), no-phasing-in-plans (Sage-Luma specific).
- `GrowthMastery-ai/gm-agent-core/scripts/pipeline_watcher.py` (2026-04-15) — already adapted in PR #107 as `heal_pipeline_stall.py`. This spec layers Pulse's LLM-judgment on top of that zero-LLM floor.
- `agents/beacon/specs/e4-4d-system-tab.md` (PR #104) — the data layer (chain_events + agent_sessions VIEW + thresholds config) that this upgrade reads from. PR-A of E4.4d must ship before PR-α of this spec.
- 2026-05-25 session retrospective — three multi-hour debugging windows documented in `docs/operating-manual.md` Part II entries for PR #102, #103, #104, #105, #107. Each one is a case study in what the upgraded cycle would have caught faster.
- Larry's morning 2026-05-26 directive: *"I want to fully update pulse with everything Joe has that will be beneficial to us and anything else we can think of that would make it better."* This spec captures the beneficial subset of Joe's doctrine + our own innovations (self-optimizing thresholds via Pulse Check III, marker.py canonical-render mandate, stale-daemon healer, chain_events data layer) that he doesn't have.

---

## 11. Open questions Larry may want to override before PR-α dispatches

These are the values I locked in § 2 based on author judgment. Larry can override any in the approval pass:

1. **Cadence values (decision A).** Are 5/15/30 the right tier values? Joe uses 5/15/25. We deviated to 30 on Tier 3 for lighter-than-Joe pipeline. If you'd rather match Joe exactly, change Tier 3 to 25 min.
2. **Pipeline-driver scope (decision D).** I locked conservative V1 (proposals only, Larry approves before dispatch). If you'd rather start with auto-dispatch for low-risk categories (e.g., spec-doc dispatches that touch nothing customer-facing), the spec needs amendment.
3. **PRIME DIRECTIVE strictness (decision E).** I locked the empirical-verification gating verbatim from Joe. If you'd rather start with a softer "intent to ship systemic fix" criterion and tighten later, the spec needs amendment.
4. **Plan-First Protocol deferral (decision H).** I locked deferred. If you want Plan-First adopted in V1 (especially for spec-doc dispatches where schema-of-truth review makes sense), it needs its own sub-section in this spec.

If any of these need amendment, paste the change as a Beacon `modify:` reply to the approval card before dispatching PR-α.
