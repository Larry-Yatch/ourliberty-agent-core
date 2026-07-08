# Spec: Flip-readiness gauge — the autonomy doorbell

**Status:** Draft — pending reconciliation against `completeness-architecture-design.md` §3 (SS3) before build
**Author:** Beacon (drafted 2026-07-08, from parked card `cap-build-flip-readiness-gauge-5-completeness-gate-m-a453`)
**Approver:** Larry (date TBD)
**Build gate:** dispatch the BUILD only after completeness-program **PR-1** ("turn on what's built") merges. XIV has already landed. See §8.

## 1. Problem statement

Larry locked the default-deny → autonomy flip behind **five measurable criteria** (design v2 §3). **Nothing computes those five today.** So the go-ahead depends on a human remembering to check each one — the exact silent-miss failure mode the completeness program exists to close. This gauge measures all five weekly and rings a single doorbell the moment they all go green.

## 2. Success criteria

- A weekly, self-firing job computes all five flip-readiness criteria from live substrate — no human runs it, no human tallies it.
- The moment all five transition from not-all-green → all-green, Larry receives **one** approval-shaped alert (the "flip-ready doorbell") and never a repeat while they stay green.
- Between runs Larry can read the current gauge state (per-criterion green/red + the number/gap) from a dated artifact without asking anyone.
- The gauge changes **no config and flips nothing itself** — it measures and announces; the flip stays Larry's explicit call.

## 3. Users / consumers

- **Primary:** Larry — the only actor who acts on the doorbell (decides whether to make the flip).
- **Consumer:** Larry-only. Not TruPath / Rocket Station / AI services — this is internal chain-autonomy governance.
- **Downstream:** a later phase may read the artifact for a dashboard tile; out of scope here.

## 4. The five criteria (transcribed from the capture note; **reconcile against design §3 at build**)

Each criterion is a pure boolean over live substrate. **Signature = green when the threshold holds.**

| # | Criterion | Threshold | Primary substrate |
|---|---|---|---|
| 1 | Escalation precision | ≥ 90% over trailing 30d | decision-outcome ledger (PR-1) |
| 2 | Backstop-caught misses | == 0 over trailing 4wk | backstop/healer catch ledgers |
| 3 | Verified auto-fix templates | ≥ 3 distinct templates, each ≥ 20 runs AND ≥ 95% verifier-confirmed success | `action-template-executions.json` / Check V graduation |
| 4 | Over-silence audit | green (no ~100%-silence high-volume signature flagged) | Check XIV over-silence surface + PR-1 G8 silence-file auditor |
| 5 | Projected post-flip approval volume | ≤ current ask-rate | Check XIV `ask_rate` + decision ledger dispatch projection |

**Substrate mapping notes (verified 2026-07-08):**
- Criteria **1, 3, 5** depend on the decision-outcome ledger being *honest*: PR-1 makes the join real (bare-task_id keys join, `closed_unmerged` re-checkable) and flips the default-success lie to `outcome='unverified'`. Until PR-1 merges these three read a ledger that joins nothing and a graduation feed poisoned by default-`success`. **This is the build gate.**
- Criterion **3** must count only `success` executions verifier-confirmed post-PR-1 (`unverified` rows are neutral — excluded from the streak, per completeness-pr1.md §2(d)).
- Criterion **4** reads XIV V1's over-silence safety surface (`~/agents/blackboard/pulse-check-xiv/check-xiv-<date>.json`) plus PR-1's silence-file auditor. XIV V1 is sufficient — it does not need XIV-b/c.
- Criterion **5** reads XIV's per-source `ask_rate` and projects the approval volume the chain would still surface to Larry post-flip; green iff that projection ≤ today's ask-rate (the flip must not *increase* Larry's load).

## 5. Scope (what's in)

- `scripts/flip_readiness_gauge.py` — deterministic stdlib Python (no LLM). Reads the three substrates, computes the five booleans + their underlying numbers, writes the artifact, manages the doorbell state transition.
- **Weekly systemd timer + service from birth** — copy the `ourliberty-pulse-check-viii` unit shape (the XIV/`pulse-check` precedent: timer-driven, never enters `cycle-prompt.md §5`). Slot into the Monday cluster at a free `OnCalendar` minute + `RandomizedDelaySec`; honor `EMERGENCY_HALT` via `ConditionPathExists`.
- **Heartbeat every run** via the standard `pulse_check_heartbeat` machinery so `heal_pulse_check_staleness` covers it.
- **Artifact:** `~/agents/blackboard/flip-readiness/flip-readiness-<date>.json` — the five booleans, each criterion's computed number + threshold + gap, `all_green` bool, `as_of` (UTC), `window`s, and a per-substrate status block (which inputs were readable). 26-week self-pruned retention (XIV precedent).
- **DM-only-on-state-change** (order-fragile-gauge precedent): silent (artifact + heartbeat only) while state is unchanged. On the **not-all-green → all-green** transition, ring the doorbell exactly once. On an **all-green → red** regression (a criterion falls back), send one warning DM (a gate we thought was met slipped). Persist last-state in the artifact / a small state file so transitions are computed, not re-fired every week.
- **The doorbell = one approval-shaped alert to Larry**: "Flip-readiness: all 5 gates green as of <date>. Consider the default-deny → autonomy flip." Larry decides the flip; the gauge only rings.
- **Partial-data contract:** each substrate read is try/except'd; a dark/unreadable input marks that criterion `indeterminate` (NOT green), records `substrate.<x>='error'` in the artifact, and the run still writes + heartbeats + exits 0. Escalate only after 2 consecutive dark runs on the same input. `all_green` requires every criterion genuinely green — an indeterminate is never green.

## 6. Out of scope (deliberately not in)

- **Performing the flip.** The gauge never mutates the trust policy / default-deny config. It announces; Larry flips.
- **Auto-tuning the five thresholds.** They are Larry-locked (design §3). The gauge reads them from a named constant/config block; it does not propose changes.
- **Any XIV-b/c automation** (action-rate write-back, auto-silence). Criterion 4 uses XIV V1's existing over-silence surface only.
- **A dashboard widget.** Artifact only; a tile is a later followup.
- **Non-PR terminal verifiers** for the ledger — PR-1 keeps `task_terminal_state` PR-only; the gauge inherits that boundary and does not fabricate outcomes for un-joined work.

## 7. Acceptance criteria

- [ ] Timer + service = a `pulse-check-viii` unit copy; `systemctl list-timers` shows the next Monday firing; unit refreshed in `/etc/systemd/system` before daemon-reload.
- [ ] A run with all five substrates readable writes the artifact with five booleans + numbers + `all_green`, heartbeats, exits 0.
- [ ] First run where `all_green` flips false→true rings the doorbell exactly once; a subsequent still-green run rings nothing (transition, not level).
- [ ] An `all_green`→regression run sends exactly one warning DM and updates the artifact.
- [ ] A dark substrate marks its criterion `indeterminate` (not green), records `substrate.<x>='error'`, still exits 0; `all_green` is false; escalates only after 2 consecutive dark runs.
- [ ] Criterion 3 excludes `unverified` executions from the verified-template count (post-PR-1 honest-ledger semantics).
- [ ] `heal_pulse_check_staleness` (or the gauge's own healer coverage) shows the gauge fresh.
- [ ] Decommission order documented (stop/disable timer → remove any cadence entry → delete heartbeat) → zero alerts next healer run.
- [ ] Tests: stdlib `unittest` (not pytest), sentinel-armed, **zero live-tree writes** (injected roots/tmp; never `~/agents`). Transition logic (false→true rings once; true→true silent; true→false warns) unit-tested against fixture artifacts.

## 8. Open questions / risks

1. **Criteria transcribed from the capture note, not the primary design doc.** `completeness-architecture-design.md` §3 (SS3) is a desktop-only artifact not in the repo, so I could not quote it verbatim. The five thresholds here are from the parked-card note. **To resolve: Larry / build-time — reconcile §4 against design §3 before Forge builds; flag any delta.** (Beacon memory `spec_faithful_capture_of_locked_decisions`.)
2. **Build gate: PR-1 must merge first.** Criteria 1/3/5 read the honest ledger PR-1 establishes. Dispatching the build before PR-1 merges would code against a ledger that joins nothing. **To resolve: Beacon — hold the build dispatch until completeness-program step `completeness-pr1` merges; XIV is already satisfied.**
3. **Criterion 1 & 5 formulas are directional, not yet exact.** "Escalation precision" and "projected post-flip approval volume ≤ current ask-rate" need a precise numerator/denominator definition against the ledger's actual row shape. **To resolve: build-time — define against the live ledger schema PR-1 lands; Mirror verifies the formula matches design §3 intent.**
4. **Backstop-caught-miss source (criterion 2).** Multiple backstop ledgers exist (`no_session_ledger`, `heal_pipeline_stall` Check 6, sequence-step stall recovery). **To resolve: build-time — enumerate the authoritative backstop-catch events; a "miss" = automation-should-have-caught-but-a-backstop-did.**
5. **order-fragile-gauge precedent not located in-repo.** Cited as the pattern (weekly timer, DM-on-state-change). The XIV unit is a sufficient structural template regardless. **To resolve: Larry — confirm the precedent name, or accept the XIV shape.**

## 9. Handoff package requirements

Internal chain tooling (not a stranger-team prototype), but per NORTH-STAR the PR must ship: a module docstring stating the goal + the five criteria + build gate; the systemd units; the unittest suite (transition + partial-data + criterion-3 semantics); a one-paragraph runbook (how to read the artifact, how to decommission); and the artifact schema documented inline.

## 10. References

- Parked card `cap-build-flip-readiness-gauge-5-completeness-gate-m-a453` (the origin note with the five criteria).
- `completeness-architecture-design.md` §3 (desktop — the authoritative locked criteria; reconcile before build).
- `agents/beacon/specs/completeness-pr1.md` (the ledger PR-1 makes live — build gate).
- `agents/beacon/specs/pulse-check-xiv.md` (the landed over-silence + ask_rate substrate; structural precedent for the weekly-timer/DM-on-signal/heartbeat/partial-data shape).
- `scripts/pulse_check_v.py` + `action-template-executions.json` (criterion 3 graduation feed).

## Changelog

- 2026-07-08 — Draft authored by Beacon from the parked card; build gated on PR-1 merge; §4 criteria pending reconciliation against design §3.
