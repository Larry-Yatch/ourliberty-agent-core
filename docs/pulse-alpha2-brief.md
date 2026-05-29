# Pulse Cycle Upgrade — PR-α₂ Brief

**Purpose:** Forge reads this brief during PR-α₂ preflight + build. Dispatch text is short; canonical scope lives here. Sister doc to [docs/pulse-alpha1-brief.md](pulse-alpha1-brief.md) and [docs/pulse-cycle-upgrade-design-pass-2026-05-26.md](pulse-cycle-upgrade-design-pass-2026-05-26.md).

**Spec base:** [`agents/beacon/specs/pulse-cycle-upgrade.md`](../agents/beacon/specs/pulse-cycle-upgrade.md) §§ 12.1 (scope expansion), 12.2 (Decisions I-IV), 12.3 (5-Check family overview).

**Gates on:** PR-α₁ merged.

**What this brief is NOT:** scope for the state machine implementation (β), CLAUDE.md additions (γ), or the Check IV/V/VI/VII analyzer code (β).

---

## Scope summary

α₂ amends the cycle-prompt.md file α₁ ships, adding the healer-triage doctrine layer that turns Pulse from "observer + journaler" into "alert-triager + categorized auto-dispatcher with Larry-as-gate on guarded categories." Net add ~800 lines.

Deliverables in order:

1. **Check 0 — alert-triage scan** — new section in α₁'s § 3 (the mandatory checks block). Becomes the first check every iter; the other 5 (1-5) shift to follow.
2. **§ 6.6 Tier-1 alert handling autonomy** — appended to α₁'s § 6 PRIME DIRECTIVE block. Encodes Decision I (default-trust + 4 guarded categories) and the Check V trust-graduation rule.
3. **§ 6.7 PRIME DIRECTIVE starting posture** — encodes Decision II (Neutral) + verification_pending lifecycle + Check VI self-tuning hook.
4. **§ 6.8 Soft cost ceiling** — encodes Decision III ($50/$100 escalation DM) + Check VII self-tuning hook.
5. **§ 6.9 Post-hoc DM threshold logic** — encodes Decision IV ($5 cost / 30 min wall / >2 PR cycles thresholds + daily 8:00 MDT digest + guarded-always-immediate carve-out).
6. **§ 6.10 The plain-language DM template** — single canonical Pulse→Larry message format: `Pulse triaged: <plain language>. Acting: <what the system did or is doing>. Status: <dispatched | merged | verified | failed>. Detail: <expandable raw context>.`
7. **§ 5.4 Check family overview** — short subsection at end of α₁'s § 5 conditional-checks block, naming Checks III, IV, V, VI, VII with one-line summaries each + cross-reference to the analyzer scripts (which β ships). Existing Check III prose stays where it is in agents/pulse/CLAUDE.md — α₂ just acknowledges the family pattern at cycle-prompt level.
8. **§ 6.6 Known-pattern allowlist semantics** — Tier-3 known-pattern allowlist seeded from `config/alert-translations.json` (the PR-0 stopgap that became PR #121). Cross-reference, not duplication.
9. **§ 14 actions-log extension** — extend α₁'s § 14 to also record `triage_decisions` rows in the alert-triage state file at `~/agents/state/alert-triage.json` per lifecycle `pending → triaged-tier-N → action-dispatched → resolved`.

Target file length post-α₂: ~2000 lines (α₁'s ~1200 + α₂'s ~800).

---

## Audit of α₁ state — what α₂ extends

α₂ runs AFTER α₁ merges, so the audit baseline is "α₁'s output as merged." α₂ should not modify any α₁ section except to append/extend per the deliverables above. Specifically:

| α₁ section | α₂ touch | Detail |
|---|---|---|
| § 1 Mission filter | None | Stays verbatim. |
| § 2 Tier state | None | α₂ does not modify the tier state machine. Check 0 runs at the same level as Checks 1-5 — does NOT change tier-reset semantics. |
| § 3 MANDATORY 5 checks | **Extend** | Add new § 3.0 Check 0 — alert-triage scan AT THE TOP, before § 3.1 Check 1. Renumber NOTHING — the spec calls them Check 1-5 with Check 0 distinct. Tier-reset rule applies the same: any Check 0/1/2/3/4/5 non-empty finding forces Tier 1. |
| § 4 Additive checks | None | Stays verbatim. |
| § 5 Conditional/Periodic checks | **Extend** | Append new § 5.4 "Self-optimizing Check family overview" subsection. NO modification of existing § 5.1 Check I / § 5.2 Check VIII / § 5.3 Check IX prose. |
| § 6 PRIME DIRECTIVE | **Extend** | Append § 6.6, § 6.7, § 6.8, § 6.9, § 6.10 per deliverables 2-6 above. NO modification of α₁'s § 6.1-6.5. |
| § 7 Pipeline-driver | None | Stays verbatim. |
| § 8 Phase 4 verification window | None | Stays verbatim. |
| § 9 WARN-vs-INFO | None | Stays verbatim. |
| § 10 Data sources | **Extend** | Add `larry-alerts.jsonl` and `alert-triage.json` rows to the table. |
| § 11 Auto-fix allow-list | None | Stays verbatim. Alert-triage actions go in the alert-triage state file per § 14 extension, NOT in § 11's auto-fix log. |
| § 12 Fixture-pattern allowlist | None | Stays verbatim. |
| § 13 Write journal entry | **Extend** | Add `Triage:` line summarizing Check 0 output (e.g., "Triage: 3 alerts, 1 Tier-1 dispatched, 2 Tier-3 known-pattern silenced"). |
| § 14 Write actions log | **Extend** | Add `triage_decisions` rows to alert-triage state file per Deliverable 9 above. |
| § 15 Send escalations | None | Stays verbatim. |
| § 16 End the cycle | None | Stays verbatim. PR #157 doctrine intact. |
| § 17 Dispatch task format | None | Stays verbatim. |

**Net file shape:** identical to α₁'s ordering, with the 9 deliverables inserted at the specified sections.

---

## Concrete content references — what to encode verbatim

For each section, ground the content in the spec sections cited below. Do NOT paraphrase decisions; quote the spec where it's already locked.

| α₂ section | Spec source | Encoding |
|---|---|---|
| Check 0 alert-triage scan | § 12.1 (full text) | Encode trigger ("runs first on EVERY iter regardless of tier"), data substrate (`larry-alerts.jsonl`), state file (`alert-triage.json`), and the four lifecycle phases verbatim. Add an example: a stale-daemon-code alert + a credential-rotation alert + a known-pattern alert = how Pulse classifies each. |
| Decision I — alert handling autonomy | § 12.2 Decision I (full text) | Encode "default-trust + 4 guarded categories" + the rationale + Check V trust-graduation rule. Verbatim list of the 4 guarded categories (credential, prod config, novel template, high-cost dispatch). |
| Decision II — PRIME DIRECTIVE posture | § 12.2 Decision II (full text) | Encode "Neutral" + 24h window + auto-promote-to-systemic_fix logic + Check VI cross-reference. The lifecycle states `verification_pending → systemic_fix` if signal appears within 7d, else stays neutral indefinitely. |
| Decision III — Cost ceiling | § 12.2 Decision III (full text) | Encode soft-cap $50/$100, escalation DM behavior, "no auto-throttle on silence" rule, Check VII cross-reference. |
| Decision IV — Post-hoc DM threshold | § 12.2 Decision IV (full text) | Encode thresholds ($5, 30 min, >2 PR cycles), 8:00 AM MDT daily digest, guarded-always-immediate exception. |
| Plain-language DM template | § 12.2 Decision IV final paragraph | Verbatim template: `Pulse triaged: <plain language>. Acting: <what the system did or is doing>. Status: <dispatched | merged | verified | failed>. Detail: <expandable raw context>.` Include 2-3 example renderings. |
| 5-Check family overview | § 12.3 (full table + 5-step pattern + proposal-firing rules) | Encode the table; explicitly cross-reference each Check's analyzer script (which β ships: `scripts/pulse_check_iv.py`, `_v.py`, `_vi.py`, `_vii.py` — see β brief). |
| Known-pattern allowlist | § 12.1 last bullet | Cross-reference `config/alert-translations.json` (PR #121 shipped this). Encode the rule: "Tier-3 means Pulse silences + logs to journal only — never DMs. Allowlist entries are seeded from PR #121 and grow via Check IV." |

---

## task_type + Mirror review focus

- **task_type:** `feature-development` (substantive doctrine extension; not Claude-as-Forge eligible — touches load-bearing prompt that drives every cycle).
- **Cost ceiling:** ~$4 LLM per memory project-pulse-cycle-upgrade-pending.

**Mirror review focus (Dial 3 regression-only):**

1. **Verbatim quotation** of Decisions I-IV from spec § 12.2. Mirror should grep for spec phrases ("default-trust categorized auto-dispatch", "Neutral", "Soft cap with escalation DMs", "$50/day and $100/day", "8:00 AM MDT", "$5", "30 minutes", ">2 PR cycles") and verify each appears in α₂'s output once, in the correct § 6.6/6.7/6.8/6.9.
2. **No modification of α₁ sections** outside the explicit extend list. Mirror should diff α₁'s merge SHA against α₂'s output and verify only the named § 3, § 5, § 6, § 10, § 13, § 14 sections changed; everything else byte-identical.
3. **Check 0 ordering** — Check 0 runs FIRST per spec § 12.1 "runs first on EVERY iter." Verify the new § 3.0 sits before § 3.1, not after § 3.5.
4. **Tier-reset rule consistency** — Check 0 findings force Tier 1 same as Checks 1-5. Verify the rule statement covers Check 0 explicitly.
5. **DM template canonicalization** — the plain-language template appears in § 6.10 and is the ONLY DM format documented for Pulse→Larry messages. Any earlier α₁ DM example using a different format must be updated to match.
6. **5-Check family cross-references** — Each Check III/IV/V/VI/VII's analyzer script reference must match what β actually ships (read the β brief). If β brief and α₂ diverge on script names, surface as a Mirror CHANGES_REQUESTED.
7. **No β scope leakage** — analyzer implementation (the Python scripts), the cycle-tier.json schema, the cycle-prime-ledger.jsonl ledger writes, and the systemd timer change all belong to β. α₂ documents the BEHAVIOR (what Pulse does); β implements the MECHANISM (the scripts + state files). Verify no Python code in α₂.
8. **No γ scope leakage** — `agents/pulse/CLAUDE.md` should NOT be touched by α₂. γ owns that file. Mirror should grep for `agents/pulse/CLAUDE.md` mentions in α₂'s diff — they should only be in cross-references (NEVER as a modified path).

---

## Acceptance criteria

- [ ] All 9 deliverables present in correct sections
- [ ] No α₁ sections outside the named extend list touched
- [ ] Total file length 1900-2100 lines (target ~2000)
- [ ] Spec § 12.1, § 12.2, § 12.3 quoted verbatim where the spec is verbatim (Decisions I-IV definitions)
- [ ] Check 0 ordering: appears before Check 1, after § 2 Tier state intro
- [ ] No β scope leaked (no Python, no state-file writes)
- [ ] No γ scope leaked (no `agents/pulse/CLAUDE.md` modifications)
- [ ] Mirror PASS
- [ ] Post-merge: next `/cycle` reads α₂-augmented prompt without parse errors; journal entry shows new `Triage:` line (may say "0 alerts triaged" on first run)

---

## Dependencies + sequencing

- **Blocks:** PR-β (state machine + analyzers). β needs α₂'s § 6.6-6.10 + § 3.0 to know what state to track + which analyzers to wire.
- **Blocked by:** PR-α₁ merged. α₂ extends α₁'s file directly; cannot ship in parallel.
- **Cross-reference:** α₂'s 5-Check family overview references β's analyzer scripts by name. If β's brief diverges on naming, α₂ must update to match. Verify during the chain.

---

End of brief.
