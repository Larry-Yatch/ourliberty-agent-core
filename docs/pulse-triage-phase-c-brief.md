# Brief: Pulse triage — Phase C (the experience-driven promotion loop)

## Where this sits (A → B → C)

This is **Phase C**, the final phase, of the pre-authorized A→B→C split of the
Pulse triage layer (parent brief: `docs/pulse-triage-layer-brief.md`).

- **A (merged, #279 + #288):** `config/auto-fix-patterns.json` registry
  (`state` / `clean_streak` / `permanent_guard` / `plain_language` /
  `graduated_at` / `last_larry_correction_at`) + the ledger-tagging seam
  (`cycle_prime_ledger.canonical_intervention_id()` + the mandatory
  `--template`/`--detail` path) + tagging enforcement.
- **B (merged, #292):** the durable, data-driven Check 0 backend
  (`scripts/alert_triage_state.py` → `triage_alert()`): classify a signal from the
  registry + translation table, persist the lifecycle row, route it, and — for the
  Tier-1 auto-fix path — record a tagged `cycle_prime_ledger` intervention.
- **C (THIS PR):** close the cycle. The auto-fix allowlist GROWS from track record:
  a probation pattern that proves itself earns a one-time, plain-language
  graduation approval from Larry; on approval it becomes auto-fix (graduated); any
  later failure or Larry-correction auto-demotes it immediately (no approval).

## Larry's contract (honored exactly)

- **Pulse does NOT self-graduate.** She proposes; Larry approves the PATTERN once
  (never per instance). The approval reads in plain English: WHAT it is, WHAT Pulse
  would now do automatically, WHY it's safe, and the TRACK RECORD. No enum/jargon.
- **Threshold:** ~3 clean consecutive interventions (reversible, zero
  Larry-corrections). Credentials / money / irreversible (`permanent_guard`) NEVER
  graduate regardless of streak.
- **Demotion:** any failure OR Larry-correction of a graduated pattern → immediately
  back to probation. Automatic, no approval (losing trust is never gated).

## The gap C closes — the missing streak INPUT

Phase B records a tagged ledger intervention **only** for Tier-1 / already-graduated
patterns. A Tier-2 / probation "ask → approve → succeed" recorded **nothing**, so a
probation pattern accrued zero track record and could never reach `clean_streak>=3`.
The cycle-prime-ledger rows also carry no success/failure outcome and no
Larry-correction marker (the vestigial `pulse_check_v._is_larry_modification` reads
`payload.larry_modified`, a field `append_action` never writes). The streak input the
promotion loop needs **did not exist in the merged code.**

Phase C builds that input itself (self-contained — it captures the track record AND
graduates on it):

- **Authoritative track-record source** = `alert_triage_state` action-template
  executions, persisted at `~/agents/state/action-template-executions.json` as
  `{action_templates: {<template>: {executions: [{outcome, larry_correction_signal,
  ts}]}}}`. Built by `record_action_template_execution()` and invoked from Check 0
  (`triage_alert`) for BOTH Tier-1 auto-fixes AND Tier-2 approved-probation fixes.
- A **"clean" execution** = `outcome == "success"` AND `larry_correction_signal ==
  false`. The per-template streak = the count of **consecutive clean executions**
  from the tail of the list (a failure or a Larry-correction breaks it).
- `larry_correction_signal = true` when a Tier-2 proposal was approved-with-
  modification, or a fix (Tier-1 or Tier-2) was corrected/reverted after the fact. A
  plain clean-approve + successful exec = clean.

**Enforcement:** `record_action_template_execution()` is the single writer of the
executions store; `pulse_check_v.consecutive_clean_streak()` is the single reader.
The streak is never read from the vestigial `payload.larry_modified` ledger fields —
`test_pulse_check_v_promotion.py` asserts a Larry-correction breaks the streak from
the executions source.

## Design — the promotion loop (Check V, rewired)

### 1. Streak computation (authoritative = executions)

Check V computes, per template, the count of consecutive clean executions and writes
it back to the registry's `clean_streak` as a derived cache. The registry is the
state-of-record for `state` / `permanent_guard` / `reversible`; the executions store
is the state-of-record for the track record. Check V reconciles the two.

### 2. Graduation proposal

For a `state == "probation"`, NOT `permanent_guard`, `reversible == true` pattern
with `clean_streak >= GRADUATE_MIN_CLEAN_STREAK` (= 3) → emit a graduation
APPROVAL_REQUEST through the **existing** approval machinery:
`beacon_approval_handler.add_pending()` creates the pending entry (kind=`graduation`,
carrying the template + the plain-language fields), and
`larry_alerts.append_approval_request()` DMs Larry the plain-language render. Batch
multiple ready patterns into one ask.

**Threshold unification.** The legacy `GRADUATE_MIN_DISPATCHES = 10` dispatch-count
rule (remove-from-guard-list on 10 trailing-90d dispatches with zero Larry-mods) is
SUPERSEDED by the registry promotion loop at `clean_streak >= 3` consecutive clean
reversible executions, per Larry's decision + spec § 6.6 (`graduate_min_clean_streak:
3` in the registry `_schema`). The legacy pure functions (`run_check`,
`compute_template_stats`) are retained unchanged so their unit tests keep passing,
but the **live** graduation path is the promotion loop.

### 3. Plain-language approval render (the Larry-facing deliverable)

Per pattern: WHAT it is, WHAT Pulse will now do automatically, WHY it's safe
(reversible + the record), TRACK RECORD (e.g. "4/4 clean over 11 days"). The fields
come from the registry's `plain_language` block. If a field is missing, the render
falls through to a safe raw form (the render-layer-human-translation rule) rather
than crashing or leaking an enum.

**Enforcement:** `test_pulse_check_v_promotion.py` greps the rendered text for
enum/jargon leakage and asserts all four sections render + the raw fallback works
when a field is absent.

### 4. On approve / reject

The graduation flips a version-controlled config field
(`config/auto-fix-patterns.json`), so — exactly like Check III / Check VIII config
approvals — Larry's approval dispatches a Claude-as-Forge config-only PR that runs
the new `pulse_check_v.py apply-graduation <template>` CLI, which flips the registry
record `probation → graduated` and stamps `graduated_at`. The registry-mutation logic
lives in `pulse_check_v` (single source), NOT in the bot. On reject the pattern stays
probation.

**Dedicated grammar (a Larry-facing safety property).** A graduation grants new
autonomy, so it must be resolved ONLY by its explicit named approval. The bot adds an
`approve graduation <template>` route; a bare `approve` / `most_recent_pending` must
NOT resolve a graduation entry. `most_recent_pending` excludes `kind == "graduation"`,
and the dedicated route resolves a graduation only by exact template match.

**Enforcement:** `test_beacon_approval_handler.py` asserts `most_recent_pending`
skips graduation entries and the `approve graduation <template>` grammar parses to a
template-targeted action.

### 5. Auto-demotion (no approval)

On any FAILED execution OR Larry-correction of a `graduated` pattern → immediately
flip `graduated → probation`, reset `clean_streak = 0`, stamp
`last_larry_correction_at`. This is triggered from `record_action_template_execution()`
itself: when an adverse execution (failure or correction) is recorded against a
currently-graduated template, the recorder calls `pulse_check_v.demote()`
synchronously. No approval, no Check-V-cycle latency — immediate.

**Enforcement:** `test_pulse_check_v_promotion.py` asserts a graduated pattern with a
subsequent failure/correction is demoted to probation with `clean_streak = 0` and no
approval emitted.

### 6. The permanent_guard floor

A `permanent_guard` template (credentials / money / irreversible) NEVER graduates,
even at `clean_streak >= 3`. Enforced in `find_graduation_candidates()` (code, not
just docs), mirroring Check 0's Tier-2 floor and the `NEVER_GRADUATE_TEMPLATES`
sentinel for `uncategorized`.

**Enforcement:** `test_pulse_check_v_promotion.py` asserts a `permanent_guard`
template at `clean_streak >= 3` produces no graduation proposal.

## What Phase C does NOT change

- The legacy `run_check` / `compute_template_stats` pure functions + their tests
  (back-compat shim only; the live path is the promotion loop).
- Check 0's classification gates (`classify`) — unchanged. Phase C only adds the
  execution-recording side effect to the Tier-1 / Tier-2-approved act paths.
- The cycle-prime-ledger schema — unchanged. The outcome + correction signal lives in
  the new executions store, not in retrofitted ledger fields.

## A → B → C: complete

With Phase C merged, the ladder is whole: Check 0 classifies and records, executions
accrue a per-pattern track record, Check V proposes graduation in plain language,
Larry approves the pattern once, and any miss demotes it automatically. The auto-fix
allowlist grows from earned trust and shrinks the instant trust is lost.
