# Doctrine of doctrine — every rule earns an enforcement mechanism

**Single source of truth referenced by `agents/beacon/CLAUDE.md`, `agents/mirror/CLAUDE.md`, `runbooks/cycle-prompt.md`.**

---

## The principle

Any new rule added to an agent's `CLAUDE.md`, a spec under `agents/*/specs/`, or a runbook under `runbooks/` MUST be paired with a hard enforcement mechanism — OR carry an explicit waiver documenting acceptable risk.

Prose alone does not hold. As the instruction set grows, the probability that any individual rule is silently violated grows with it. The mechanism is what makes the rule load-bearing; the prose is what makes the rule readable.

Established 2026-05-28 after two same-day Pulse drift incidents (PR #157 + cycle-prompt § 278 violation). Both were prose-only rules that Pulse silently misapplied for weeks.

---

## How to apply when drafting a rule

Every rule-introducing paragraph carries an adjacent `**Enforcement:**` line that names the mechanism. Acceptable shapes:

| Mechanism | Example | When to use |
|---|---|---|
| `deny block at <file>` | `Bash(git commit:*)` in `agents/pulse/.claude/settings.json` | When the rule is "agent X must not do Y" and Y maps to a Bash/Read/Edit tool call. |
| `validator at <file:function>` | `dispatch_validator.validate_task` checks source ∈ ALLOWED_SOURCES | When the rule is "shape Z is required" and shape Z is checked at a known choke point. |
| `state-file path that is gitignored` | `~/agents/state/pulse-fixture-suppressions.jsonl` for fixture suppressions | When the rule is "don't put X in git" and X has a structural home outside git. |
| `allowlist at <surfaces>` | `scripts/fixture_patterns.py` consulted at 5 surfaces | When the rule must hold across multiple call sites — defense in depth. |
| `Mirror review checklist item` | "Review checks for unenforced rules" in `agents/mirror/CLAUDE.md` | When the rule is structural and Mirror can catch violations at PR time. |
| `routing rule that physically prevents` | `check_hard_topology()` in `scripts/routing_validator.py` (function at line 294) blocks Pulse → Forge | When the rule is "agent X cannot reach Y" and routing is centralized. |
| `idempotency flag in artifact` | `applied:true` on Check III proposal artifacts | When the rule is "don't re-apply X" and X produces a durable artifact. |

If none of these fits, document the waiver explicitly:

```
**Enforcement:** deferred — risk: <one-sentence justification>. Mitigation: <how we'll catch drift>.
```

The waiver path is not a free pass. It is a deliberate, reviewed choice. Mirror reviews flag undocumented waivers the same way they flag missing mechanisms.

---

## How Mirror catches violations

At PR review time, Mirror checks every diff that touches `**/CLAUDE.md`, `agents/*/specs/*.md`, or `runbooks/*.md` for new rule-shaped paragraphs (imperative MUST/SHALL/DO NOT/ALWAYS/NEVER constructs) without a paired `**Enforcement:**` line. Missing enforcement → REVIEW_REVISION with the specific paragraph cited.

The check is structural — Mirror does not adjudicate whether the chosen mechanism is *sufficient* (that's a design call), only whether one was named.

---

## How Pulse references this when proposing permanent fixes

When Pulse's § G pattern detection surfaces a permanent fix that adds a rule, the dispatch envelope to Beacon/Mirror MUST include the proposed enforcement mechanism alongside the rule prose. The downstream Beacon-side refuse-to-forward check (described in cycle-prompt § G) is not yet implemented as a hard gate — for now, Mirror's review checklist (this PR) and Pulse's own § G discipline catch unenforced rules at PR review time. The Beacon emit-time refuse mechanism is tracked as a follow-up enforcement layer.

**Enforcement:** Mirror's checklist item flags the missing mechanism on PR review. Pulse's § G dispatches a permanent fix after ≥3 incidents of dispatches missing mechanism names. The Beacon emit-time refuse check is the next planned enforcement layer (tracked as follow-up).

---

## Existing artifacts that demonstrate the pattern

- **Fixture-pattern allowlist** (`scripts/fixture_patterns.py`) consulted at: cycle-prompt § G teach, `run_cycle.sh` commit guard, `pulse_check_i.py` σ-anomaly filter, `pulse_check_iii.py` chain_events filter, Pulse `CLAUDE.md` mirror. Defense-in-depth allowlist.
- **`dispatch_validator.ALLOWED_SOURCES`** + **`routing_validator.HARD_TOPOLOGY`** — validator/routing enforcement of dispatch shape and topology.
- **5-step Pulse Check pattern** (spec § 12.3) — `applied:true` flag on artifacts + Beacon shortcut idempotency. Each step has a mechanism.
- **`Bash(git commit:*)` deny in `agents/pulse/.claude/settings.json`** (PR #157) — paired with cycle-prompt § 7 + Pulse CLAUDE.md "Commit discipline" prose. Canonical doc + mechanism pairing.

## Anti-pattern: rules that drifted because they were prose-only

- **`shared/REPO-GUARDRAILS.md`** "edits MUST be committed in the same session" — applied to Forge (Builder); Pulse (Observer) silently misapplied it for weeks. Closed by PR #157.
- **`cycle-prompt.md` § 278** "Do NOT touch git-tracked `cycle-journal.md` for fixture suppressions" — Pulse violated it during the 2026-05-28 10:48 cycle; wrote 4 fixture entries to the git-tracked file, contributed to the 10-min timeout. Enforcement still needed.

---

## Meta-rule on this meta-rule

This document itself is a rule. **Enforcement:** Mirror's checklist verifies every new rule-bearing PR carries this discipline. If Mirror itself drifts, Pulse's § G pattern detection surfaces the drift after ≥3 occurrences and dispatches a permanent fix — typically tightening Mirror's checklist or adding a structural check to `dispatch_validator`.
