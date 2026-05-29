# Pulse Cycle Upgrade — Design Pass 2026-05-26 (PR-0 Beacon brief)

**Purpose:** This document is the load-bearing brief for PR-0 of the Pulse cycle upgrade. PR-0's remaining deliverable is the amendment to the canonical spec at `agents/beacon/specs/pulse-cycle-upgrade.md` that captures the locked decisions from the 2026-05-26 conversational design pass.

Forge reads THIS document during preflight + build. Beacon's dispatch text is short; the details live here.

**Scope note — translation layer already shipped.** The original brief drafted on 2026-05-26 included a second deliverable: an interim healer-alert translation layer (`config/healer-alert-translations.json` + render helper). That work shipped in parallel as PR #121 ("feat(ops): plain-language translation layer for healer alerts") on 2026-05-26 15:40, with slightly different file naming: `config/alert-translations.json` + `translate_alert()` extension to `scripts/larry_alerts.py` + AST-walk CI gate in `scripts/tests/test_alert_translations.py`. The Deliverable 1 section of this brief is therefore obsolete and has been dropped. PR-0 reduces to the spec amendment only.

---

## Deliverable — Spec amendment

**Path to amend:** `agents/beacon/specs/pulse-cycle-upgrade.md`

**Action:** Append a new section `## 12. Post-2026-05-26 design pass — locked decisions` after the existing § 11. Do NOT modify §§ 1-11; the original locked decisions A-H stay verbatim.

**Literal content for § 12:**

```markdown
## 12. Post-2026-05-26 design pass — locked decisions

The 2026-05-26 conversational design pass with Larry locked four additional values decisions, four new self-optimizing Check instances, and one major scope expansion that supersedes parts of § 4 (Out of V1 scope) and § 5 (Architecture).

### 12.1 Scope expansion — healer-alert triage as first-class PR-α scope

Per [memory `feedback_pulse_triages_operational_signals`](../../../../.claude/projects/-Users-Larry-Desktop-Rocket-Station-PResentation/memory/feedback_pulse_triages_operational_signals.md): healer/operational alerts route through Pulse for triage and action; Larry sees outcomes with plain-language context, not raw technical signals.

PR-α's primary purpose becomes the healer-triage loop, with the Joe-doctrine adoption (PRIME DIRECTIVE, multi-tier cadence, pipeline-driver) serving the triage loop rather than being separate from it. Concretely:

- A new **Check 0 — alert-triage scan** runs first on EVERY iter regardless of tier (additional to the original 5 mandatory checks in § 5.2 — now 6 total).
- Pulse owns `~/agents/blackboard/larry-alerts.jsonl` as a primary input substrate.
- A new state file `~/agents/state/alert-triage.json` tracks per-alert lifecycle (`pending` → `triaged-tier-N` → `action-dispatched` → `resolved`) plus the Tier-3 known-pattern allowlist (seeded from PR-0's `config/healer-alert-translations.json`).
- The existing `outbox_notifier` becomes a fallback path: it fires raw alerts only when Pulse hasn't claimed an alert within N minutes (configurable) AND the alert matches an urgency-keyword allowlist. The primary path is Pulse-rendered.

### 12.2 Locked values decisions

#### Decision I — Tier-1 alert handling autonomy

**Default-trust categorized auto-dispatch.** When Pulse triages a healer alert as Tier 1, she auto-dispatches the fix through the chain unless the action falls in a guarded category. Guarded categories require approval-gate via Beacon shortcut:

1. **Credential operations** — any change touching `.env`, OAuth tokens, secrets registry, key rotation.
2. **Production config changes** — `config/` files affecting live dashboards, public-facing surfaces, budget caps.
3. **Novel action templates** — first-time-doing-this-exact-class; Pulse tracks her own action-template execution history. Templates with fewer than 3 prior successful executions remain gated.
4. **High-cost dispatches** — anything Forge will likely cost more than $20. Pulse estimates via the `task_type` cost model in `config/agent-models.json`.

For non-guarded categories, Pulse dispatches autonomously and DMs Larry post-hoc only if the post-hoc threshold (see Decision IV) is crossed. **Rationale:** Larry's approval doesn't add judgment value on technical correctness (Mirror + chain gates cover that); it adds value only on intent / direction for the four guarded categories.

The guarded list **shrinks over time** via Check V (action-template trust review — see § 12.3). Patterns that execute correctly 10+ times in a row with zero Larry modifications graduate out of the guard list.

#### Decision II — PRIME DIRECTIVE starting posture

**Neutral.** When a systemic fix dispatches and the 24h verification window passes without clear signal either way, the fix is marked `verification_pending` and does NOT count as either an intervention or a systemic fix. If the verifying signal appears within 7 days, the entry auto-promotes to `systemic_fix`. If it never appears, the entry stays neutral indefinitely — neither rewarding Pulse for unverified work nor penalizing her for naturally-noisy verification surfaces.

**Rationale:** Generous posture would rot the scorecard (every ambiguous case becomes a free win). Strict posture would warp behavior (Pulse avoids harder fixes where verification is naturally noisier — exactly the fixes that probably matter most). Neutral keeps the scorecard honest without perverse incentives.

The Neutral starting posture is itself self-tuning via Check VI (see § 12.3).

#### Decision III — Cost ceiling

**Soft cap with escalation DMs.** No hard circuit-breaker. Pulse tracks cumulative daily LLM spend. At $50/day and $100/day, she DMs Larry with the trend and asks "throttle / keep going?". Larry's answer is logged for Check VII to learn from. Default behavior if Larry doesn't respond: keep going (don't auto-throttle on silence — a silent Larry might just be in a meeting, not approving throttle).

**Rationale:** Hard cap risks throttling Pulse exactly when active development needs her most (a busy day correlates with active spending). Unmonitored watch-it risks waking up to a $200 day if Pulse gets stuck in a Tier-1 hot loop. Soft cap with escalation puts Larry in the loop at the right moments and learns his patterns over time.

The $50/$100 thresholds are themselves self-tuning via Check VII (see § 12.3).

#### Decision IV — Post-hoc DM threshold logic

When Pulse acts on a Tier-1 alert via auto-dispatch, she does NOT immediately DM Larry by default. Instead:

- **Immediate DM** only when the action crossed at least one of these thresholds:
  - Forge cost exceeded $5
  - Wall-clock for the action exceeded 30 minutes
  - More than 2 PR cycles involved (e.g., a fix that required a follow-up fix)
- **Daily digest** at 8:00 AM MDT — a single DM listing all non-threshold-crossing Tier-1 actions from the previous 24h.
- **Guarded-category requests** (per Decision I) are ALWAYS immediate DMs because they need Larry's gate.

The DM template (for all Pulse-to-Larry messages, immediate or digest): `Pulse triaged: <plain language>. Acting: <what the system did or is doing>. Status: <dispatched | merged | verified | failed>. Detail: <expandable raw context>.`

**Rationale:** Pulse's existence is supposed to REDUCE Larry's DM volume from healers. Auto-DMing every Tier-1 action would just shift the DM source from healers to Pulse without cutting volume. Threshold-gated DMs reserve Larry's attention for actions whose scope warrants his awareness.

### 12.3 Self-optimizing Check family additions

Doctrine #48 (`feedback_self_optimizing_config_via_pulse_check_pattern`) says any hand-tuned constant earns a periodic Check that proposes adjustments based on observed data. The 2026-05-26 design pass identified four new instances on top of the existing Check III:

| Check | What it tunes | Cadence | Data substrate | Silent until |
|---|---|---|---|---|
| III | Stuck-detection thresholds | 14-day Sunday-anchored | `chain_events` (live now) | Live (Sun 2026-05-31 first run) |
| IV | Marker-drift enforcement strictness | Weekly | `chain_events` query for `mirror_marker_invisible:*` | Live immediately (has data now) |
| V | Tier-1 action-template trust list | Monthly | `cycle-actions.jsonl` | ~30d of cycle ledger data |
| VI | PRIME DIRECTIVE posture (Generous / Neutral / Strict) | Monthly | `cycle-actions.jsonl` `verification_pending` rates + auto-promote ratios + ratio-trend | ~30d of cycle ledger data |
| VII | Cost-ceiling escalation thresholds ($50/$100 bands) | After every escalation DM logged | Pulse's escalation-response log | ~20 logged escalations |

Each Check follows the same five-step pattern per doctrine #48:

1. Pulse periodic Check queries its data substrate.
2. Writes proposal artifact to `~/agents/blackboard/pulse-<check-name>-proposals.json` with current vs. proposed values, sample sizes, rationale, `applied: false`, `as_of` date.
3. DMs Larry a digest via `larry_alerts.append_alert`.
4. Beacon shortcut `approve <check-name>-update-<date>` reads the dated artifact, dispatches a small Claude-as-Forge config-only PR (task_type: doc-only).
5. Mirror auto-merges; on merge, Beacon flips `applied: true` in the archived artifact (idempotency gate).

**Proposal-firing rules** (each Check's specific threshold for emitting a proposal):

- **Check IV — marker-drift enforcement.** If `mirror_marker_invisible:*` event rate exceeds 2/week over the trailing 4 weeks, propose enforcement tightening (Mirror prompt re-emphasis OR hard validator gate on marker shape).
- **Check V — action-template trust.** For each action-template, if it has been dispatched 10+ times in trailing 90d with zero modifications by Larry, propose removing it from the guard list. Inverse: if a non-guarded template caused a Larry-correction within 30 days, propose moving it INTO the guard list.
- **Check VI — PRIME DIRECTIVE posture.** Three trigger shapes:
  1. `verification_pending` rate exceeds 40% AND auto-promote rate exceeds 80% → posture too lenient, propose tightening (move toward Strict OR shrink verification window).
  2. `verification_pending` rate below 5% AND intervention-to-systemic-fix ratio NOT trending toward zero → Neutral is masking failures, propose tightening.
  3. `verification_pending` stuck-forever rate exceeds 30% → discipline failing, propose stricter posture + re-examine which fix-categories are systemically unverifiable.
- **Check VII — cost-ceiling thresholds.** Three trigger shapes:
  1. Larry consistently approves "keep going" at the $50 escalation (≥10 consecutive) → propose raising first escalation to $75 or $100.
  2. Larry consistently says "throttle" at the $50 escalation (≥10 consecutive) → propose lowering threshold OR auto-throttling at that level.
  3. Larry's pattern is fully consistent over 20 escalations → propose removing the escalation entirely for that band (Pulse decides without DMing Larry).

### 12.4 What this changes about the cycle-prompt size estimate

The original § 6 PR-α estimate was ~600-800 lines of new prompt content. With the healer-triage scope expansion + 5-Check family overview + plain-language DM doctrine + tier-state-machine + Decisions I-IV, the realistic landing is closer to ~2000 lines total (vs. the spec's original ~800-1200 estimate, and Joe's 2,145 reference).

PR-α therefore splits into PR-α₁ (core Joe doctrine: cadence, 5 original checks, PRIME DIRECTIVE accounting, pipeline-driver, Phase 4 verification, WARN-vs-INFO, tier-state-machine — ~1200 lines) and PR-α₂ (healer-triage doctrine + Check 0 + 5-Check family overview + Decisions I-IV operationalization + plain-language DM template — ~800 lines). PR-α₂ gates on PR-α₁ merged.

### 12.5 Scope handed off to a separate workstream — multi-step build orchestrator

The 2026-05-26 design pass identified that the PR-α₁/α₂/β/γ sequence (and future multi-PR builds) deserves a dedicated multi-step orchestrator rather than manual dispatch-one-at-a-time. The orchestrator is its own spec at `agents/beacon/specs/build-sequence-orchestrator.md` (separate workstream from this Pulse upgrade) and ships before the Pulse PR-α/β/γ dispatches fire — those run through the orchestrator as its first real-use test.

PR-0 (this PR) ships independently of the orchestrator; the translation stopgap and this spec amendment do not require the orchestrator to exist.

### 12.6 Channel-heartbeat liveness Check — folded in 2026-05-29

Folded into this upgrade's scope on 2026-05-29 (Larry-chat decision): the **channel-heartbeat liveness Check** — the observation half of the desired-state bot reconciler (`project_desired_state_reconciler_dispatched`, PR #178). The reconciler closed process-down recovery for all four bots; this Check closes the complementary gap where a bot process is alive but its Telegram channel is wedged, which existence checks (`systemctl is-active`) cannot see — e.g. the 2026-05-20 HTTP 502 storm and the 2026-05-28 HTTP 409 double-poll.

- **Number:** Check X (VIII burn-rate + IX operator-friction already shipped since the 2026-05-26 pass).
- **Observes:** per-bot end-to-end Telegram liveness — `getMe` success + getUpdates not erroring (no sustained 409/502) + optional self-ping watermark (bot confirms it received a heartbeat token).
- **Cadence:** short-interval (align with the watchdog 4-min tick or a dedicated 5-min timer) — a wedged channel is a live outage, not a slow-tuning constant.
- **Action on failure:** DM Larry via `larry_alerts.append_alert` with a plain-language tier (per § 12.1 healer-triage doctrine). Recovery is a bot restart, which the reconciler already actuates — so the Check signals/lets the reconciler restart rather than restarting directly. Observation stays single-owner-separate from actuation.
- **Not a self-tuning Check:** unlike III-VII it tunes no constant, so it does NOT follow the § 12.3 propose-adjustment pattern; it fits the Check family by cadence + DM doctrine only.
- **Sequencing:** ships in PR-β or PR-γ of this upgrade (not α — α is core doctrine). Only dependency is the reconciler, already merged.

---

End of § 12.
```

---

## task_type, review focus, sequencing

- **task_type:** `doc-only` (pure spec amendment, no code). Claude-as-Forge eligible per `project_claude_as_forge_pattern`.
- **Mirror review focus:**
  - Faithful capture of all four locked values decisions (I-IV) in § 12.2.
  - Faithful capture of the five-Check family (III-VII) in § 12.3 including each Check's proposal-firing rules.
  - Faithful capture of the healer-triage scope expansion in § 12.1 + the cycle-prompt size revision in § 12.4 + the cross-reference to the orchestrator spec in § 12.5.
  - No modifications to §§ 1-11 of the spec.
  - § 12 appended cleanly after the existing § 11 (no merge artifacts, no duplicate numbering).
- **Dependencies:** None. PR-0 ships independently of the orchestrator workstream.

End of brief.
