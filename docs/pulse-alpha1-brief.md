# Pulse Cycle Upgrade — PR-α₁ Brief

**Purpose:** Forge reads this brief during PR-α₁ preflight + build. The dispatch text is a short pointer; canonical scope lives here.

**Spec base:** [`agents/beacon/specs/pulse-cycle-upgrade.md`](../agents/beacon/specs/pulse-cycle-upgrade.md) §§ 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7 + design-pass-2026-05-26 § 12.4 (α₁/α₂ split).

**What this brief is NOT:** scope for healer-triage doctrine, Check 0 (alert-triage), Decisions I-IV operationalization, plain-language DM template, or post-hoc DM threshold logic. Those are PR-α₂, which gates on α₁ merged.

---

## Scope summary

α₁ rewrites `runbooks/cycle-prompt.md` to encode Joe's core doctrine adapted to our system:

1. **Multi-tier cadence** (5/15/30-min, signal-driven escalation/de-escalation) — NEW
2. **Per-iter MANDATORY 5-check structure** (Checks 1-5 per spec § 5.2) — REPLACES current Checks A-I structure (with explicit retention rules below)
3. **PRIME DIRECTIVE + empirical-verification gating + cycle-actions ledger** — NEW
4. **Pipeline-driver behavior** (quiet-iter leverage proposals) — NEW
5. **Phase 4 verification window** — NEW
6. **WARN-vs-INFO calibration heuristic** — NEW
7. **Data sources Pulse reads** (§ 5.7 table inline in prompt) — NEW
8. **Tier-state machine documentation** (`~/agents/state/cycle-tier.json` semantics) — NEW

Target size: ~1200 lines total (current 486 + ~700 net add).

---

## Audit of current state — what stays, what changes

Read [`runbooks/cycle-prompt.md`](../runbooks/cycle-prompt.md) in full before drafting. Current 523-line structure (as of HEAD 2026-05-29, post-#179 Check IX add):

| Current section | Lines | α₁ disposition |
|---|---|---|
| Mission filter | 9-15 | **Keep verbatim.** No change. |
| § 1 Read continuity | 19-23 | **Keep + extend.** Add reads for `cycle-tier.json` and `cycle-actions.jsonl` ratio context. |
| § 2 Health Check Suite intro + classify-finding rubric | 25-36 | **Keep + extend.** Same nominal/always-fix/ask-then-do/never-auto/route taxonomy. Add `tier-reset` as a side-effect any finding may emit (§ 5.2 last paragraph). |
| Check A — Source repo discipline | 37-53 | **Keep.** Re-home under new "§ 4 Additive checks (every iter)" — not part of the new mandatory 5, but still load-bearing. |
| Check B — Sync health | 54-69 | **Keep** under § 4 additive checks. |
| Check C — Agent process liveness | 70-87 | **Keep** under § 4 additive checks. |
| Check D — Inbox / dispatch state | 88-106 | **Fold into new Check 3** (chain_events stall scan). The Check 3 scan + heal_pipeline_stall.py cross-reference subsume what Check D was doing manually. Preserve the duplicate-archive + malformed-JSON always-fix actions in § 11 auto-fix allow-list. |
| Check E — PR / merge state | 107-121 | **Keep** under § 4 additive checks. Re-frame to acknowledge auto-merge healer is now in front of this. |
| Check F — Cost / quota signals | 122-134 | **Replace.** Subsumed by Tier-1 escalation + § 9 WARN-vs-INFO + Decision III soft-cap (note: Decision III ships in α₂; α₁ leaves a stub `(see α₂ post-merge for Decision III soft-cap)`). |
| Check H — Forge activity digest | 135-154 | **Keep** under § 4 additive checks. |
| Credential rotation check (E1.5.2) | 155-184 | **Keep** under § 4 additive checks. Long-form unchanged. |
| Check I — Optimization mode (Mon/Wed/Fri/Sun) | 185-227 | **Keep** as a conditional check. Re-home under new "§ 5 Conditional/Periodic checks" subsection. Trigger logic unchanged. |
| Check VIII — Burn-rate signal validity (Mondays) | 228-263 | **Keep** under § 5 conditional checks. Trigger logic unchanged. |
| Check IX — Operator-friction signal (Mondays) | 264-299 | **Keep** under § 5 conditional checks alongside Check VIII. Trigger logic unchanged. PR #179 added 2026-05-28, just before this brief was drafted; missions-API integration + sentinel-cum-artifact shape mirror Check VIII. |
| Check G — Pattern detection | 300-324 | **Replace by PRIME DIRECTIVE accounting** (§ 5.3). The pattern-detection routing rules (Forge/Beacon/Mirror/Pulse cycle-prompt edit + the `cycle-fix-<slug>.json` envelope shape) survive in the new "permanent-fix dispatch protocol" subsection. The "no direct-commit path" doctrine + the doctrine-of-doctrine mechanism rule stay verbatim. |
| Fixture-pattern allowlist | 325-360 | **Keep verbatim.** No change. Referenced by Checks 1-5 the same way it's referenced by current Checks A-IX. |
| § 3 Auto-fix allow-list | 361-399 | **Keep verbatim.** No change in scope. Add tier-reset side-effect note. |
| § 4 Write the journal entry | 400-419 | **Extend.** Add `Tier:` field (current tier + consecutive_clean count), `PRIME DIRECTIVE ratio:` field, `Leverage proposals:` field (if quiet-iter). |
| § 5 Write the actions log | 420-427 | **Extend** — see OQ1 below; same-name collision with the PRIME DIRECTIVE ledger needs resolution. |
| § 6 Send escalations | 428-455 | **Keep verbatim.** No change. |
| § 7 End the cycle (with no-direct-commit doctrine from PR #157) | 456-465 | **Keep verbatim.** The PR #157 doctrine is load-bearing and must not be diluted. |
| § 8 Dispatch task format | 466-504 | **Keep verbatim.** Reference table for the `cycle-fix-<slug>.json` envelope shape. |
| When the cycle should NOT run | 505-516 | **Keep + extend.** Add tier-state corruption case (§ 5.3 risks table row). |
| When you genuinely don't know | 517-523 | **Keep verbatim.** |

**Disposition count:** 20 Keep / 1 Fold / 2 Replace. Total 23 rows in the audit table.

---

## New section structure (target order)

After applying the audit above, the α₁ file should read in this order:

1. Mission filter
2. § 1 Read continuity (extended)
3. § 2 Tier state — read at start (NEW, ~80 lines)
   - 2.1 Multi-tier cadence (§ 5.1 verbatim table)
   - 2.2 Tier-state machine — `~/agents/state/cycle-tier.json` schema, read/write semantics, how cycle-prompt edits interact with mid-execution sessions (§ 6 PR-β cross-ref)
   - 2.3 Tier-reset rule (any Check-1-5 non-empty finding forces Tier 1; 3 consecutive clean iters at a tier promotes to the next)
4. § 3 The MANDATORY 5 checks (every iter, in order) (NEW, ~250 lines)
   - 3.1 Check 1 — Cumulative log-noise scan
   - 3.2 Check 2 — Telegram thread sweep
   - 3.3 Check 3 — chain_events stall scan (includes folded Check D inbox/dispatch logic)
   - 3.4 Check 4 — Pending-Larry-directive check
   - 3.5 Check 5 — Stale-daemon-code check
   - Each subsection: trigger, data substrate, finding classification, examples of nominal vs signal output, time budget (15 sec scan hard cap per spec § 8 risks table)
5. § 4 Additive checks (every iter, after the 5 mandatory) (~120 lines)
   - 4.1 Check A — Source repo discipline
   - 4.2 Check B — Sync health
   - 4.3 Check C — Agent process liveness
   - 4.4 Check E — PR / merge state (with auto-merge healer cross-ref)
   - 4.5 Check H — Forge activity digest
   - 4.6 Credential rotation check (E1.5.2 long-form)
6. § 5 Conditional/Periodic checks (~100 lines, mostly preserved)
   - 5.1 Check I — Optimization mode (Mon/Wed/Fri/Sun)
   - 5.2 Check VIII — Burn-rate signal validity (Mondays)
   - 5.3 Check IX — Operator-friction signal (Mondays)
7. § 6 PRIME DIRECTIVE — intervention + systemic-fix accounting (NEW, ~150 lines)
   - 6.1 The directive verbatim from § 5.3
   - 6.2 Empirical-verification gating + dual-clock-anchor rule (§ 5.3 Mirror PR #108 amendment)
   - 6.3 Healer first-execution accounting
   - 6.4 The cycle-prime ledger — `~/agents/blackboard/cycle-prime-ledger.jsonl` row shape, how to append, how to compute ratio over trailing 30d
   - 6.5 Permanent-fix dispatch protocol (preserved from current Check G: routing rules + cycle-fix-<slug>.json shape + doctrine-of-doctrine enforcement-mechanism mandate + the no-direct-commit doctrine)
8. § 7 Pipeline-driver — quiet-iter leverage proposals (NEW, ~80 lines from § 5.4)
   - 7.1 When it fires (all 5 mandatory clean + all additive clean + pipeline quiet)
   - 7.2 What it evaluates (spec backlog / recurring stalls / self-optimization backlog)
   - 7.3 Proposal artifact + Larry approval gate (NO auto-dispatch in V1)
9. § 8 Phase 4 verification window (NEW, ~60 lines from § 5.5)
   - 8.1 Three gating conditions
   - 8.2 Fresh-process-spawn anchor for prompt-edit fixes
   - 8.3 Why this prevents PRIME DIRECTIVE inflation
10. § 9 WARN-vs-INFO calibration heuristic (NEW, ~50 lines from § 5.6)
    - 9.1 Demote-to-INFO patterns
    - 9.2 Reserve-WARN-for patterns
    - 9.3 How Check 1 uses this heuristic
11. § 10 Data sources Pulse reads (NEW, ~30 lines, § 5.7 table inline)
12. § 11 Auto-fix allow-list (current § 3, preserved verbatim + tier-reset side-effect note)
13. § 12 Fixture-pattern allowlist (current § 2 fixture section, preserved verbatim, repositioned)
14. § 13 Write the journal entry (current § 4, extended with Tier/Ratio/Leverage fields)
15. § 14 Write the actions log (current § 5, extended for cycle-actions ledger)
16. § 15 Send escalations (current § 6, preserved verbatim)
17. § 16 End the cycle (current § 7, preserved verbatim — no-direct-commit doctrine is load-bearing)
18. § 17 Dispatch task format (current § 8, preserved verbatim)
19. When the cycle should NOT run (preserved + tier-state corruption case)
20. When you genuinely don't know (preserved verbatim)

---

## Open questions (Forge: flag in preflight if not pre-answered)

**OQ1 — `cycle-actions.jsonl` naming collision.** The current cycle-prompt.md § 5 already writes auto-fix action rows to `runbooks/cycle-actions.jsonl`. The spec § 5.3 calls for a PRIME DIRECTIVE ratio ledger ALSO at `~/agents/blackboard/cycle-actions.jsonl`. These are TWO DIFFERENT files with the same name. Either:
- **Option A** — rename one. Suggested: PRIME DIRECTIVE ledger becomes `~/agents/blackboard/cycle-prime-ledger.jsonl`. Update spec § 5.3 + § 5.7 data-sources table accordingly.
- **Option B** — merge into one. Single `~/agents/blackboard/cycle-actions.jsonl` carries both auto-fix actions AND PRIME-DIRECTIVE intervention/systemic-fix rows; rows carry `kind: 'auto-fix' | 'intervention' | 'systemic-fix'` discriminator.

α₁ author should NOT silently pick one. Surface as a CLARIFY at preflight; default to Option A if the CLARIFY round budget is exhausted.

**OQ2 — Mandatory-vs-additive boundary on Check D fold.** Current Check D (inbox/dispatch) does duplicate detection + malformed-JSON archival as `always-fix` auto-fix actions. New Check 3 (chain_events stall scan) is more LLM-judgment-shaped. The auto-fix actions from Check D must survive somewhere — either:
- **Option A** — move auto-fix actions to § 11 (auto-fix allow-list) standalone, with Check 3 only doing the stall scan.
- **Option B** — Check 3 keeps the auto-fix actions inline (then it's not purely chain_events).

Default Option A unless preflight CLARIFY surfaces an issue.

**OQ3 — Existing § 7 "end the cycle" placement vs new § 16.** The current § 7's no-direct-commit doctrine + the auto-commit handoff to run_cycle.sh is load-bearing post-PR #157. Verify during build that the new § 16 keeps the doctrine VERBATIM including the deny-block reference. Mirror review focus item.

**OQ4 — Tier-state schema in cycle-prompt vs PR-β.** PR-β ships `scripts/cycle_tier_state.py` + writes the actual schema. α₁ documents the schema in § 2.2. Verify that α₁'s documented schema matches what PR-β will implement. Spec § 5.1 implementation paragraph names the keys (`tier`, `consecutive_clean`, `last_signal_at`). Use those names verbatim in α₁ § 2.2. If PR-β author proposes different keys, that's a revision of α₁ — block PR-β until α₁ updated.

---

## task_type + Mirror review focus

- **task_type:** `feature-development` (substantive doctrine encoding — not doc-only, not Claude-as-Forge eligible per [[project_claude_as_forge_pattern]]: real code rewrite of a load-bearing operational prompt).
- **Cost ceiling:** ~$6 LLM per memory project-pulse-cycle-upgrade-pending.

**Mirror review focus (Dial 3 regression-only):**

1. **Faithful capture** of spec §§ 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7 (verbatim text where the spec is verbatim — e.g., the PRIME DIRECTIVE block from § 5.3 is a quote, the 5.1 cadence table is a table, the 5.2 Check definitions are concretely executable not vibe-prose).
2. **No contradictions** with `agents/pulse/CLAUDE.md` (currently mentions the 5 mandatory checks indirectly + the fixture allowlist + the commit discipline). α₁ should NOT touch CLAUDE.md (that's α₂ scope) — but the new cycle-prompt must compose without conflicting.
3. **Audit dispositions preserved.** Every "Keep" row in the audit table above is actually preserved; every "Replace" row is actually replaced; every "Fold" is folded with cross-reference. Mirror should grep for: "Cumulative log-noise" (must appear once, in new § 3.1), "no direct-commit path" (must appear once, in new § 6.5 OR § 16 — verify Forge didn't drop it), "fixture-pattern allowlist" (preserved verbatim).
4. **The 5 mandatory checks are concretely executable** — each has: trigger condition, data substrate (file path / query), output classification, hard time budget. No "vibe-check" prose allowed (spec § 5.2 explicit requirement).
5. **Tier-state machine is unambiguous** — § 2.2 must answer: what does Pulse do if `cycle-tier.json` is missing on startup? Corrupted? Last-signal timestamp is from the future? § 2.2 should reference § 5.3 risks-table rollback row verbatim.
6. **Phase 4 verification window is encoded** — must include the dual-clock-anchor rule (chain_events dispatch timestamp for code fixes, fresh-process-spawn timestamp for prompt-edit fixes) per § 5.5.
7. **No premature α₂ scope leakage** — Check 0 (alert-triage), Decisions I-IV operationalization, plain-language DM template, post-hoc DM threshold logic must NOT appear in α₁. If Forge accidentally drafts them, Mirror REVISION.
8. **PR #157 doctrine survival** — the "no `git commit` / `git push` inside /cycle" guard must remain in the file, in any section. Mirror should grep for it.

---

## Acceptance criteria

- [ ] All audit-table dispositions reflected (20 keep + 1 fold + 2 replace per current count above)
- [ ] All new sections (§§ 2, 3, 6, 7, 8, 9, 10 in target order) present with the spec-cited content
- [ ] Total file length 1100-1300 lines (target ~1200)
- [ ] No α₂ scope leaked
- [ ] Mirror PASS
- [ ] Post-merge: next `/cycle` invocation reads the new prompt without parse errors and produces a journal entry with the new `Tier:` + `PRIME DIRECTIVE ratio:` + `Leverage proposals:` fields (even if all are "tier=1 / ratio=N/A / no proposals")
- [ ] No regression: existing fixture-pattern allowlist behavior unchanged; existing § 7 (now § 16) no-direct-commit doctrine intact

---

## Dependencies + sequencing

- **Blocks:** PR-α₂ (healer-triage doctrine + Check 0 + Decisions I-IV ops + DM template) — α₂ amends α₁'s file, cannot ship in parallel.
- **Blocked by:** None. cycle.timer fix (PR #165 merged 2026-05-28) is a prerequisite for any α₁ verification cycle to complete inside its window; without it, post-merge verification would fail at the 600s cap. Confirm `/etc/systemd/system/ourliberty-cycle.service` shows `TimeoutStartSec=1200` before dispatching α₁.
- **Independent:** PR-β (state machine implementation) — α₁ documents the schema; PR-β implements it. They can theoretically build in parallel but α₁'s § 2.2 schema doc becomes load-bearing for PR-β author, so serialize: α₁ → PR-β → PR-γ.

---

End of brief.
