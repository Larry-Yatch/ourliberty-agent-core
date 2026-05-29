# Pulse Cycle Upgrade — PR-γ Brief

**Purpose:** Forge reads this brief during PR-γ preflight + build. Dispatch text is short; canonical scope lives here. Sister doc to [docs/pulse-alpha1-brief.md](pulse-alpha1-brief.md), [docs/pulse-alpha2-brief.md](pulse-alpha2-brief.md), [docs/pulse-beta-brief.md](pulse-beta-brief.md), and [docs/pulse-cycle-upgrade-design-pass-2026-05-26.md](pulse-cycle-upgrade-design-pass-2026-05-26.md).

**Spec base:** [`agents/beacon/specs/pulse-cycle-upgrade.md`](../agents/beacon/specs/pulse-cycle-upgrade.md) § 6 PR-γ.

**Gates on:** PR-β merged.

**Claude-as-Forge eligible** per [[project_claude_as_forge_pattern]]: pure doc-only edit to a single persona file. Skip Forge preflight/build; Beacon dispatches Mirror review directly.

---

## Scope summary

γ is a small set of additions to `agents/pulse/CLAUDE.md` (currently 220 lines). Cross-references the new operational doctrine that lives in `runbooks/cycle-prompt.md` post-α₁/α₂. Does NOT duplicate that doctrine — cycle-prompt.md is the canonical operational spec; Pulse CLAUDE.md is the persona doc that points at it.

Net add: ~50-80 lines. Target file length post-γ: ~270-300 lines.

Deliverables:

1. **Cross-reference block** — short section near the existing "Session startup" list pointing at the new cycle-prompt.md structure (Tier state machine in § 2, MANDATORY 5 checks in § 3, PRIME DIRECTIVE accounting in § 6, Pipeline-driver in § 7, Phase 4 verification in § 8, WARN-vs-INFO heuristic in § 9). One line per section reference.
2. **Cycle-iter operating order section** — new section "When you wake up for a cycle iter, here's the order you operate in" naming the read order: continuity (cycle-journal + cycle-prime-ledger + MEMORY) → tier-state read → mandatory 5 checks → additive checks → conditional (weekday-gated) checks → PRIME DIRECTIVE accounting → journal/ledger writes → escalations → tier-state update → exit. Mirrors α₁'s § 1-§ 16 ordering but at persona-doc level, not operational detail level.
3. **cycle-prime-ledger.jsonl append discipline** — new top-of-mind rule reminding Pulse that intervention/systemic_fix/verification_pending rows go to `~/agents/blackboard/cycle-prime-ledger.jsonl` (NOT the existing auto-fix log at `runbooks/cycle-actions.jsonl`). Reinforces the OQ1 resolution at the persona-doc level so Pulse doesn't drift.
4. **WARN-vs-INFO calibration heuristic** — new top-of-mind rule in the same paragraph cluster as existing "What you don't do" / "Memory discipline" sections. Pulls the demote-to-INFO patterns + reserve-WARN patterns directly from α₁'s § 9 verbatim.

---

## Audit of current `agents/pulse/CLAUDE.md` — what γ extends

Current 220-line file (HEAD as of 2026-05-29). γ extends ONLY by appending. NO modifications to existing sections.

| Current section | γ touch | Detail |
|---|---|---|
| `# Pulse — Operating Manual` heading | None | Stays verbatim. |
| `## Session startup` list (steps 1-9) | None | Stays verbatim. cycle-prompt.md is already listed at step 8 — no need to add another reference. |
| `## Working directory` | None | Stays verbatim. |
| `## Tier rules (non-negotiable)` | None | Stays verbatim. |
| `## What you do — the Cycle Loop` | **Extend** | After step 8 of the existing loop, insert deliverable 2 ("When you wake up for a cycle iter, here's the order you operate in") as a NEW sub-section. Existing 8-step loop stays verbatim — the new sub-section provides ALTERNATE operating order that aligns with α₁/α₂'s cycle-prompt structure. Note in a one-line preface: "Use the cycle-prompt.md § 1-§ 16 order from α₁'s rewrite for autonomous cycles; this 8-step list remains the human-readable summary." |
| `## Fixture-pattern allowlist for /cycle` | None | Stays verbatim. Load-bearing per [[feedback_pulse_triages_operational_signals]] + PR #157 doctrine. |
| `## Commit discipline — Pulse is Observer, not Forge` | None | Stays verbatim. PR #157 doctrine is load-bearing. |
| `## /optimize` | None | Stays verbatim. |
| `## Check III — stuck-threshold review` | None | Stays verbatim. α₂ adds Check III to the 5-Check family overview at the cycle-prompt level, but this CLAUDE.md section stays — it's the persona-doc encoding of the same Check. |
| `## /dispatch <N>` | None | Stays verbatim. |
| `## What you don't do` | None | Stays verbatim. |
| `## Post-cycle exit discipline` | None | Stays verbatim. |
| `## Memory discipline` | None | Stays verbatim. |
| `## When something is genuinely broken` | None | Stays verbatim. |
| `## Your first move every cycle invocation` | None | Stays verbatim. |
| `## Your first move when chatted with directly` | None | Stays verbatim. |

**New sections appended (in this order, BEFORE `## When something is genuinely broken`):**

- `## Cross-reference — operational doctrine lives in cycle-prompt.md` (Deliverable 1, ~12 lines)
- `## Cycle-iter operating order` (Deliverable 2, ~25 lines)
- `## cycle-prime-ledger.jsonl append discipline` (Deliverable 3, ~15 lines)
- `## WARN-vs-INFO calibration — top-of-mind` (Deliverable 4, ~20 lines)

---

## Concrete content references — what to encode verbatim

| γ section | Source | Encoding |
|---|---|---|
| Cross-reference block (Deliverable 1) | α₁'s target structure §§ 2, 3, 6, 7, 8, 9 | One line per section: "Tier state machine → cycle-prompt.md § 2." Six lines total. No duplication of the actual doctrine. |
| Cycle-iter operating order (Deliverable 2) | α₁'s § 1-§ 16 ordering | Render as a numbered list of phase names (NOT the detailed step list). Phases: read continuity, read tier state, run mandatory 5, run additive checks, run conditional/periodic checks, write journal + ledger rows, send escalations, update tier state, exit. ~9 numbered items. |
| cycle-prime-ledger append discipline (Deliverable 3) | α₁'s § 6.4 + β's `cycle_prime_ledger.py` API | Encode: "Intervention rows go to `~/agents/blackboard/cycle-prime-ledger.jsonl` via `scripts/cycle_prime_ledger.py:append_action(tier, kind, payload)`. NOT the existing `runbooks/cycle-actions.jsonl` (which is the auto-fix log per cycle-prompt.md § 11). Two files, distinct purposes, do not confuse." |
| WARN-vs-INFO heuristic (Deliverable 4) | α₁'s § 9 | Encode the demote-to-INFO patterns (optional config keys missing, successful enforcement events, routine retries within tolerance, idle-state observations) + the reserve-WARN-for patterns (actionable problems, threshold breaches, unexpected failures, recoverable conditions becoming unrecoverable). Verbatim from α₁'s § 9. |

---

## task_type + Mirror review focus

- **task_type:** `doc-only` (single CLAUDE.md file, additive only, Claude-as-Forge eligible per [[project_claude_as_forge_pattern]]). Skip Forge preflight/build; Beacon dispatches Mirror review directly. Saves ~$3 + ~12 min vs going through full Forge.
- **Cost ceiling:** ~$2 LLM per memory project-pulse-cycle-upgrade-pending.

**Mirror review focus (Dial 3 regression-only):**

1. **No modifications to existing sections** — every "None" row in the audit table is actually unchanged. Mirror should diff β's merge SHA against γ's output and verify only the 4 new sections appear; everything else byte-identical.
2. **No doctrine duplication** — γ cross-references cycle-prompt.md sections; does NOT copy the doctrine itself. If Mirror sees the actual PRIME DIRECTIVE text or the 5-Check family table copied into CLAUDE.md, flag as REVISION (single source of truth violation).
3. **OQ1 path consistency** — Deliverable 3 names `cycle-prime-ledger.jsonl` (NEW ledger from β) as distinct from `cycle-actions.jsonl` (existing auto-fix log). Both file names should appear; the rule should be explicit that they are DIFFERENT files with DIFFERENT purposes.
4. **Section ordering** — 4 new sections inserted BEFORE `## When something is genuinely broken`, AFTER `## Memory discipline`. Verify the insertion point doesn't break the existing flow (Pulse reads top-to-bottom; placing operational-doctrine cross-refs after the existential "what you don't do" + "memory discipline" sections gives them the right priority weight).
5. **Length target** — file ends up 270-300 lines. Outside this range = scope drift (over-spec into β/α₂ territory, or under-spec missing a deliverable). Mirror should `wc -l` and flag if outside range.
6. **No β scope leakage** — no Python code, no state-file write specifications, no analyzer references beyond name-only cross-references. γ documents BEHAVIOR; β implements MECHANISM (already merged).
7. **PR #157 doctrine preservation** — `## Commit discipline — Pulse is Observer, not Forge` section stays byte-identical. Mirror should grep for "do not run `git commit`" and confirm the deny-block reference is intact.
8. **No α₁/α₂ scope leakage** — γ does NOT modify or supplement cycle-prompt.md doctrine. If γ adds new rules ("Pulse must check X") rather than cross-referencing existing rules, flag as REVISION.

---

## Acceptance criteria

- [ ] All 4 deliverables present as new sections in correct order (Cross-reference → Operating order → Ledger discipline → WARN-vs-INFO)
- [ ] No existing CLAUDE.md sections modified
- [ ] File length 270-300 lines
- [ ] OQ1 path consistency: `cycle-prime-ledger.jsonl` and `cycle-actions.jsonl` both mentioned, distinguished, not confused
- [ ] No β scope leaked (no Python, no state-file write specs)
- [ ] No α₁/α₂ scope leaked (no new doctrine, only cross-references)
- [ ] PR #157 doctrine intact (commit discipline section byte-identical)
- [ ] Mirror PASS
- [ ] Post-merge: next /cycle reads γ-augmented CLAUDE.md without parse errors; Pulse's reasoning surfaces references to cycle-prompt.md § 2/3/6/7/8/9 in journal entries (validates the cross-reference is load-bearing, not decorative)

---

## Dependencies + sequencing

- **Blocks:** Nothing. γ is the terminal step.
- **Blocked by:** PR-β merged. γ cross-references β's `cycle_prime_ledger.py:append_action` API signature + the `cycle-prime-ledger.jsonl` file path that β creates.

---

End of brief.
