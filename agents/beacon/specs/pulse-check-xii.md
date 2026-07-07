# Spec: Pulse Check XII — delivery-effectiveness meter

Status: BUILD-READY (2026-07-07). v2, revised after a 4-lens adversarial content review (substrate / metric-validity / ops-integration / purpose), reconciled against the now-live timer migration, then hardened by a build-readiness review whose 3 blockers + 5 clarifies are all resolved against **verified live facts** (missions.json canonical path, top-level task_id/pr_url join columns, append_alert signature, viii unit shape, 05:39 slot collision-free). Scope: **V1 = observe-only meter + artifact + heartbeat + cadence** (no firing rules, no digest wiring); **XII-b = CEO-digest integration** (separate follow-up PR); **V1.1 = calibrated firing rules** (after ~8 weeks of baseline). Timer checks I/III/V/VI/VIII/IX/X are live on their own timers; cycle-prompt §5 is triage-only.
Author: desktop session with Larry. Companion memory: `pulse-check-audit-2026-07-07`.

## 0. What the adversarial review changed (read first)

The v1 draft was a competent *pipeline-efficiency* meter wearing a *delivery-effectiveness* label, resting on three assumptions the droplet probe falsified. v2 makes two structural moves:

- **Observe-first, fire-later.** v1 asserted firing thresholds (>50% rework, >10% escapes, etc.) it had no data to justify; the review showed every one of them would either never fire or fire constantly at this fleet's volume. v2 ships **with zero firing rules for the first 8 weeks** — pure artifact + monthly digest — then a *separate* V1.1 turns on rules whose thresholds are set from the observed May–August variance (the calibration gate in §7). This is the prove→ship→automate pattern and it dissolves ~8 of the review's findings at once (they were all "this threshold is uncalibrated").
- **PR is the load-bearing unit; missions are a sparse overlay.** The droplet has only **15–18 missions ever marked shipped**, date-only precision, and `retired` means *garbage-collected proposals*, not delivered — so mission-based throughput/lead-time can't carry weight. Merged PRs (~84/week) have rich event substrate. v2 leads with PR metrics and demotes mission-level numbers to a whole-day, 4-week diagnostic that is honestly `insufficient_signal` most weeks.

Everything below reflects the verified substrate, not the v1 assumptions.

## 1. Purpose

Every existing Pulse check meters the machine (liveness, thresholds, markers, cost). Nothing meters the product. Check XII is the first outcome-level check. **Honest scope for V1:** it measures *delivery flow* — how much shipped, how fast, how much rework, at what cost, and split by whether the work was intentional (mission-linked) or self-maintenance (unlinked). It does **not** yet measure whether shipped software achieves its intent or gets used — that needs judgment and is a named V1 limitation (§8). It is the denominator layer Check XIII (Larry-leverage meter) will divide by.

Design constraints inherited from the 2026-07-07 pulse audit:

- **Systemd timer from birth** (`ourliberty-pulse-check-xii.timer`), same unit shape as `ourliberty-pulse-check-iv` (direct python exec). It never gets a cycle-prompt.md §5 section.
- Emits a heartbeat via `scripts/pulse_check_heartbeat.py` on **every** run including clean skips and partial-data runs; gets a `config/pulse-check-cadence.json` entry (`cadence_hours: 168, grace_hours: 36`) so `heal_pulse_check_staleness` covers it from day one.
- Deterministic stdlib Python (`scripts/pulse_check_xii.py`), no LLM. DM body is a filled template.
- All `chain_events` reads **paginated** (lesson #795 — unpaginated selects truncate at 1000 rows).

## 2. Metrics (trailing 4w window; trend vs prior-4w rolling baseline, defined once in §4)

### 2.1 Throughput — with a substance split (the v1 "motion metric" fix)
Source of truth for merges: `gh` merged-PR search (authed in the timer env via `HOME=/home/larry` + `~/.config/gh/hosts.yml`; no token in `.env.larry`). Repo list source: **`config/agent-models.json` → `repo_paths` keys**, owner hardcoded `Larry-Yatch` (no dedicated fleet-repo config exists; drift assumption noted — a repo onboarded review-only would need explicit add). `chain_events` `auto_merge` rows give a secondary signal but miss desktop merges, so gh is authoritative.

Repo keys in `agent-models.json` `repo_paths` are `ourliberty-agent-core` / `ourliberty-dashboard` / `ourliberty-graph` (full GH names, owner `Larry-Yatch`) — use them verbatim, not the local-path basenames.

Report per week, **split** (this is what separates software from churn):
- `merges_mission_linked` vs `merges_unlinked` — a PR whose task_id resolves to a mission is intentional delivery; unlinked is maintenance/healer churn. **Linkage (verified fields):** a merged PR's `task_id` is the **top-level `task_id` column** on its `chain_events` `auto_merge`/`review_request` row; test membership against the union of `missions[].task_ids` across all **non-proposed** missions (phases `drafting`/`building`/`shipped`/`deferred`). Missions file: see §2.2 path. **Verify at build** that the forge task_id namespace equals the mission `task_ids` namespace on one real merged PR — if they differ, linkage needs the dispatch-id map, not raw equality.
- Size distribution (files-changed, additions/deletions) — one extra field on the gh call already being made; distinguishes a real capability from a one-line config bump.
- `repo_class` split: factory-internal (the three `repo_paths` repos) vs product (RSDPM etc.) — **empty until the product repo exists**, but present so self-referentiality is visible, not invisible, the day it starts.
- `handsfree_merge_share` — merges via the forge/auto-merge pipeline vs desktop `merge_reviewed_pr.sh` (distinguishable from labels/branch prefix on the same PRs already fetched). Explicitly labeled a **Check XIII preview** field; costs nothing to compute now.

### 2.2 Lead time — PR clock is primary, mission clock is a demoted diagnostic
- **Primary — dispatch → merge** per PR: filter `agent='forge'` `session_start` rows (561/1123 session_starts are Beacon notification noise — must exclude), resolve task_id→pr_url, take earliest forge session_start → PR mergedAt from gh. **Verified join fields:** both `task_id` and `pr_url` are **top-level columns** on `chain_events`; session_start rows have `pr_url=None`, so map task_id→pr_url from the same task_id's `review_request`/`auto_merge` row via `row['pr_url']` (fall back to `(row.get('payload') or {}).get('pr_url')` — the shipper writes pr_url top-level and also mirrors it into payload). Report p50 (n≥5) and p90 (**n≥20**, else `p90: insufficient_n` — p90 from n=5 is just the sample max).
- **Diagnostic only — mission created → shipped**: missions file = **`<repo_root>/agents/beacon/missions.json`** resolved script-relative like siblings resolve paths (`Path(__file__).resolve().parents[1] / 'agents/beacon/missions.json'` = `/home/larry/agent-core/agents/beacon/missions.json`, the single-committer canonical copy the dashboard reads, 268 entries). **Do NOT read** the synced runtime copy `~/agents/agents/beacon/workspace/missions.json` — it lags the committer by several entries. Filter `phase=='shipped'` / `shipped_at` only, **excluding `retired`** (GC, not delivery — footnote: `retired` may not appear as a live phase value; the exclusion is then a safe no-op, do not block hunting for it). Whole-day precision, 4-week granularity, expected `insufficient_signal` most windows (n≈15–18 ever). **Subtract operator-gated dwell** (time in Parked / awaiting-approval / deep-review-hold states, from chain_events transitions) — otherwise this measures Larry's queue, not the factory. If dwell extraction slips V1, relabel "mission wall-clock incl. operator queue" and keep it out of any future firing rule. Not the "spec-throughput" number — see §2.6.

### 2.3 Rework — split so it doesn't punish good review
The v1 metric counted Mirror review rounds as "rework," which fires exactly when review *improves* and points a future auto-tuner at *weakening* the gate. Split:
- **Firing-eligible rework** (V1.1): Forge redispatch count = `agent='forge'` `session_start` rows per task_id minus 1 (`build_dispatched` events don't exist — never emitted), PLUS reaped/silent abandonment share (dispatches that never produced a merged PR).
- **Diagnostic-only**: Mirror review rounds = `review_revision` count per `pr_url` (dedicated events exist: 341 `review_pass` / 65 `review_revision` / 30 `review_escalate` in 30d; `review_request.payload.revision_count` also carries it directly). Reported *paired with* escape rate. Rounds-up + escapes-flat/down = "review tightened, healthy," never a fire.

### 2.4 Defect escape — survival framing + a de-noised proxy
- **Survival share** (report positively, free): % of window merges untouched for 14 days. The cheapest "did it stick" number, same join as the proxy below.
- **Hotfix proxy** (de-noised): the v1 "same files re-modified within 7d" would false-positive ~98% of the time in this hot-file codebase (a file touched by 5% of PRs expects ~4 unrelated re-modifiers/week). Tighten with a concrete, buildable denylist: exclude (a) a **hot-file denylist** = the **top-20 most-touched files** over the trailing 8-week baseline, recomputed each run; plus (b) **machine-owned/auto-committed files** = any file whose commit history in the window is >90% a single committer identity, seeded with the known auto-committed set (`missions.json`, `pulse-check-cadence.json` + other `config/*.json`, the daemon-restart manifest, `alert-translations.json`, `costs.jsonl`, `cycle-journal.md`). AND require the second PR to be fix-shaped (revert keyword, healer-origin, or fix-classified title). Report the base rate in the backtest before any rule keys on it (diagnostic-only in V1 regardless).
- Reverts: explicit revert commits/PRs against the window's merges.
- Incident→merge attribution: **out of scope V1** (needs judgment).

### 2.5 Cost — per-mission is the honest unit
`~/agents/blackboard/costs.jsonl` (`ts` + `cost_usd` + agent/task_id/model; windowable, total-able; early-May rows have naive tz — blurs backtest window edges by hours only).
- The PR is a **gameable** cost unit: the build-sequence orchestrator splits features into N PRs by choice, so cost-per-merge moves ~N× with zero delivery change. Make **cost-per-mission-shipped** the headline (firing-eligible in V1.1) and cost-per-merge diagnostic-only.
- **Split the numerator** by attribution: build/review spend vs ops-overhead (healers, Medic, pulse checks, chat). If cost tags don't support the split, report `overhead_share: unknown` rather than folding overhead into delivery cost — otherwise an 85%-false healer storm reads as delivery getting expensive.

### 2.6 Demand — the actual binding constraint (new; v1 omitted it)
Spec throughput (Larry feeding the factory) is the stated #1 constraint, and **every other metric in this check improves when Larry writes fewer specs** (less work → shorter queues, less rework, lower cost). A purpose-level meter that can't see starvation rewards it. Report:
- Missions registered / week (missions.json `created`, date-only; same repo-copy path as §2.2).
- Backlog depth (proposed/drafting missions not yet building) and forge dispatch-slot idle share (idle-while-backlog-empty vs idle-while-backlog-exists — the second is demand starvation).
Substrate: missions.json + the board-drain / dispatch-slot signals from the two-team-feasibility work. This is the number §2.2's mission clock was mislabeled as.

## 3. Artifact + heartbeat

- Artifact: `~/agents/blackboard/pulse-check-xii/check-xii-<date>.json` (**not** `-proposals/` — XII is a meter, not a proposal source; drop the `applied` field the family uses for tuning proposals). Staleness-safe: the healer globs both `pulse-check-xii/` and `-proposals/`.
- Schema per metric block: `{current, prior, trend_pct, n, sources_ok}`; top-level `sources: {github, chain_events, costs, missions}` status block (see §5 partial-data), `as_of` (**written in UTC** — the digest converts to Denver for display), `window`, and a `rules` state block present **from V1** (empty/inert while rules are off) so V1.1 needs no schema migration. `rules` shape: `{ "<rule_n>": {"last_fired_at": <iso|null>, "suppressed_until_below": <bool>, "last_value": <num|null>, "armed_since": <iso|null>} }`. Cross-run reads: glob `pulse-check-xii/check-xii-*.json`, sort by date, "the two most recent sufficient-signal artifacts" = the two newest whose relevant metric block is not `insufficient_signal`; bootstrap run (no prior) initializes empty.
- Retention: keep 26 weeks; the family has no reaper, so XII prunes its own dir.
- Heartbeat: `~/agents/blackboard/pulse-check-xii.heartbeat` on every run, including partial-data and clean-skip.

## 4. Baseline, trend, and firing rules (rules are V1.1, gated on §7 calibration)

**Baseline is defined once, everywhere:** `prior-4w rolling` = the 4-week window immediately preceding the trailing window (weeks 5–8 back). A metric's `trend_pct` is `insufficient_signal` until the prior window is **fully populated** (8 weeks of data). Because the backtest (§7) proves historical windows are computable, **the 8-week baseline is retro-computed on the first run** from gh/chain_events/costs — there is no data-accumulation wait, only a correctness wait.

**Ramp guard (record in V1, fire in V1.1):** also compare the current 4w against a **fixed anchor** (first stored 4w, or best-ever 4w) so slow monotonic rot — which a rolling baseline is structurally blind to — surfaces in the monthly digest even when no step-change rule trips.

**Firing rules ship OFF in V1.** V1.1 turns them on with thresholds set by the calibration gate (§7). When on, every rule obeys:
- **Absolute + relative floor**: fire only if relative-Δ > threshold AND absolute-Δ exceeds a floor (≥5 events or ≥2pp) — at low base rates (2% redispatch ≈ 7 events) a "+50%" is 1–2 events of noise.
- **Disjoint-window confirmation** (replaces v1's overlapping "two consecutive runs," which shared 75% of their data and confirmed nothing): fire when the most-recent week alone AND the trailing-4w both exceed threshold vs a baseline excluding the trailing window.
- **Episode dedup / hysteresis**: once fired, suppress re-fire until the metric drops below threshold for one run (one incident = one DM, not 3–4 as the window rolls). Rule state lives in the artifact `rules` block; "consecutive" = the two most recent *sufficient-signal* artifacts regardless of wall-clock gap; a catch-up run pair <24h apart counts as one observation.
- **Stratified, not pooled**: p50/rework trends evaluated within repo (or task_type) strata, firing on the worst *adequately-sized* (n≥10) stratum with a shrunk estimate (add-k toward fleet rate) to kill the winner's-curse of naming small unlucky buckets. Plus a **per-repo zero-floor**: any repo with baseline ≥3 merges/wk dropping to 0 for two windows fires regardless of fleet aggregate (catches a whole-repo CI-gate freeze the fleet p50 would mask).

Planned V1.1 rule set (calibrated thresholds TBD by backtest): (1) rework climbing — Forge redispatch/abandonment only, requiring escapes-not-down; (2) lead-time degrading — dispatch→merge, stage-split; (3) escape rate — de-noised proxy + reverts; (4) cost-per-mission up while throughput flat; (5) **throughput/output collapse** — merges or mission-linked merges down >X% sustained (the purpose instrument must speak when output *stops*, not only when it degrades); (6) demand starvation — idle-while-backlog-empty up.

## 5. Cadence, scheduling, partial-data contract

- `OnCalendar=Mon *-*-* 05:39:00` + `RandomizedDelaySec=300`, **bare (system-tz) to match the freshly-migrated siblings**. The whole family now uses bare OnCalendar on the Denver-tz droplet, so pinning `America/Denver` would make XII the *inconsistent* one — v1's pin rationale ("the family is inconsistent") is obsolete post-migration. The 05:39 slot sits after the last weekly check and before the CEO-digest deadline (06:00). Verified live sibling slots: xi 04:17 (daily), iv 04:25, iii 04:41 (Sun), v 04:49, vi 04:59 (first Mon), viii 05:09, ix 05:19, x 05:29. The Ledger-ordering clause from v1 is **dropped** — §2.5 reads costs.jsonl directly, not the Ledger sidecar.
- **No silent-until.** v1 silenced 4 weeks; the backtest proves history is computable, so the first run retro-computes its baseline and the monthly digest is meaningful immediately. (Firing rules are off in V1 regardless.)
- **Partial-data contract** (v1 had none — a 05:00 gh/Supabase blip would page Larry via `pulse-check-failed:xii` for a self-resolving condition, violating the alert-toil principle): each source (gh, chain_events, costs, missions) is independently try/except'd. The artifact is **always** written with the `sources` status block and per-metric `insufficient_signal` where a source was dark; a `warning` DM escalates only when a source is dark **2 consecutive weekly runs**.
- **Heartbeat/rc invariant (pin against the `run_check` wrapper):** wire as `run_check('xii', main)` exactly like siblings — `run_check` fires the heartbeat **only when `main()` returns 0** and calls `emit_failure` → `pulse-check-failed:xii` on any non-zero. Therefore `main()` **must return 0 on every partial-data and clean-skip path** (so the heartbeat always fires and the staleness watcher stays quiet), and reserve **non-zero exclusively for "could not write the artifact at all."** A dark source is a 0-exit with `sources.<name>='error'`, NOT a non-zero exit — the natural builder instinct (return non-zero on a failed source) would page Larry and must be avoided.

## 6. DM routing + CEO digest integration

- **Pin the routing tuples** (v1 left this to `append_alert`'s default, which sends `info` to the digest lane — so the monthly nominal report would never DM). Verified signature: `append_alert(source, severity, message, subject=, route=, ...)` — `source` and `message` are **required**; XII passes `source='pulse-check-xii'` and `message=<filled plain-language body>` on every call. Tuples: monthly nominal → `(severity='info', route='escalate', subject='pulse-check-xii-monthly-digest')`; V1.1 rule-trip → `(severity='warning', route='escalate', subject='pulse-check-xii-rule:<n>')`; source-dark → `(severity='warning', route='escalate', subject='pulse-check-xii-source-dark:<name>')`. Add `config/alert-translations.json` entries for each subject (plain-language-first rule) or the DMs arrive untranslated.
- **Delivery cadence:** monthly nominal digest (first Monday) + V1.1 rule-trips only. Silent otherwise (artifact + heartbeat) — the alert-toil principle applies to XII itself.
- **CEO digest integration is a SEPARATE follow-up PR (XII-b), NOT part of V1's definition of done.** The build-readiness review found `ceo_digest_generator.py` has no artifact-ingestion path and no per-check block mechanism — adding XII means real edits at three named sites, not a fold-in: (1) `gather_activity()` gains a step that reads the newest `pulse-check-xii/check-xii-*.json`; (2) `render_raw` gains a branch that emits the XII block; (3) `build_prompt` gains the XII field + a fixture. Constraints for XII-b: gate the block on `period == 'weekly'` only (the generator also runs daily — else it re-injects all week); freshness `< 48h` (v1's `< 8d` can never exclude last week's artifact for a weekly check); the digest is Denver-tz and `as_of` is UTC (§3) so convert before comparing; **tolerate artifact-absent** — the digest fires Mon 06:00 and XII fires Mon 05:39 +≤300s jitter, and §9 says XII runtime is pagination-dominated, so on a slow week the artifact may not exist yet: render "XII: not yet available this week" rather than erroring or showing stale numbers. V1 ships the artifact; XII-b wires the digest once V1 is observed stable.

## 7. Acceptance

1. Timer + service installed by **copying the whole `ourliberty-pulse-check-viii` unit file** (the canonical post-migration template) and changing only the script/identifier — copy it entire (`Type=oneshot`, `User=larry`/`Group=larry`, `ConditionPathExists=!/home/larry/agents/blackboard/EMERGENCY_HALT`, `EnvironmentFile=/home/larry/credentials/.env.larry`, `Environment="HOME=/home/larry"`, the `PATH=` Environment line, `Nice=10`, `TimeoutStartSec=900`, `StandardOutput=journal`/`StandardError=journal`, `SyslogIdentifier`), NOT the older bare iv/xi shape and NOT a hand-picked subset. The migration has **landed** (2026-07-07), so `pulse-check-cadence.json` is settled — add XII's entry (`{"cadence_hours": 168, "grace_hours": 36, "label": "Delivery-effectiveness meter", "firing": "Weekly (systemd timer: ourliberty-pulse-check-xii)"}`) and, while editing that file, fix the migration's leftover descriptive drift (the `firing` fields for i/iii/v/vi/viii/ix/x plus `_schema.purpose` "(I-X)" and `tuning_note` still describe /cycle agent-invocation, not the timers that now drive them). `systemctl list-timers` shows next Monday. Unit-file copy refreshed in `/etc/systemd/system` before daemon-reload (systemd gotcha).
2. Manual first run writes artifact + heartbeat, retro-computes the 8w baseline, emits **no DM** (rules off), exits 0.
2b. **Dark-source exit-0 test:** a run with one source forced dark (e.g. gh unreachable) still writes the artifact with `sources.github='error'`, fires the heartbeat, and **exits 0** — no `pulse-check-failed:xii`. (Guards the §5 heartbeat/rc invariant, the exact anti-toil contract.)
3. `heal_pulse_check_staleness` shows XII fresh (cadence entry present, no `pulse-check-no-cadence:xii`).
4. chain_events reads verified paginated against a >1000-row window (copy the pagination pattern from `pulse_check_x.py`); task_id→pr_url join verified on one real merged PR using the §2.2 fields (top-level `task_id` + top-level `pr_url`), confirming forge-task-id == mission-task-id namespace (§2.1); Beacon-notification session_starts (`agent='beacon'`) verified excluded.
5. **Calibration gate (replaces v1's eyeball backtest):** run over the May–August 2026 known-healthy window; the artifact reports each planned V1.1 rule's observed baseline, variance, and *would-have-fired count*, plus the hotfix-proxy base rate. V1.1 thresholds are set from this output; **any rule that would have fired >1× in the healthy window fails acceptance** and its threshold is widened before it ships on.
6. **DM path exercised before production:** a forced synthetic rule-trip produces a real DM through `append_alert` → Beacon triage → Telegram, translated via alert-translations. (v1's acceptance was all silent-path; the first real DM would otherwise happen unobserved in production.)
7. **Systemd-env auth:** one real timer fire (or `systemd-run --uid=larry` with the unit's exact `Environment`) confirms gh + Supabase auth under the unit context — XII is the first timer-run pulse check that needs gh, and a manual shell run doesn't prove the unit env.
8. **Decommission verified:** documented disable order — stop/disable timer → remove cadence entry → delete heartbeat file — produces zero alerts on the next healer run (leaving any one causes `pulse-check-stale` or `pulse-check-no-cadence` forever). **EMERGENCY_HALT: honored via `ConditionPathExists=!/home/larry/agents/blackboard/EMERGENCY_HALT`, matching every migrated sibling** (resolved — the migration standardized this gate across all pulse units; a tripped halt skips XII as condition-failed, not error).
9. Unit tests via unittest (not pytest), sentinel-armed, zero live-tree writes (the PR #823 review's blocker list is the checklist of what not to do).
10. **(XII-b, separate PR — not V1 DoD)** CEO digest run (weekly period) renders the XII block with `as_of`; daily period does not; artifact-absent renders "not yet available this week" without erroring.

## 8. Out of scope (V1) — and honest limitations

- **Firing rules** — off until V1.1 calibrates them (§4/§7).
- **Whether shipped software achieves mission intent or gets used** — needs judgment; §1 says so plainly. Survival share (§2.4) and unit-liveness (a `systemctl` query on units installed by window merges — stretch field) are the cheap partial proxies.
- Check XIII (Larry-leverage meter) — separate spec; `handsfree_merge_share` (§2.1) ships now as its leading indicator; full "Larry-touch" accounting deferred.
- Incident→merge attribution.
- Dashboard tab/widget — artifact + CEO digest only.
- Auto-tuning of XII's own thresholds — V1.1 hardcodes calibrated values; self-tuning is a later family upgrade.

## 9. Cost estimate

Deterministic Python; ~zero marginal LLM cost. gh reads within existing quota. Runtime dominated by chain_events pagination — bound the window server-side. Comparable to Check IX's footprint.

## 10. Followups

- V1.1: turn on the calibrated firing rules after ~8 weeks of live baseline + the §7.5 backtest.
- Wire Check XIII once XII has baseline (its denominators come from XII's artifact + `handsfree_merge_share`).
- Consider folding the order-fragile-test weekly gauge and the alert-precision meter into the same Monday reporting family.
- If/when the product repo (RSDPM) goes live, populate the `repo_class=product` split — the field exists from day one so the transition needs no schema change.
