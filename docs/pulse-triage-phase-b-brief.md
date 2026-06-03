# Brief: Pulse triage — Phase B (durable Check 0 triage layer)

## Where this sits (A → B → C)

This is **Phase B** of the pre-authorized A→B→C split of the Pulse triage layer
(parent brief: `docs/pulse-triage-layer-brief.md`).

- **A (merged):** `config/auto-fix-patterns.json` registry + the ledger-tagging
  seam (`canonical_intervention_id()` + the `--template`/`--detail` path on
  `cycle_prime_ledger.py`) + tagging enforcement (every intervention row carries a
  template; clean iters are `kind=iter_clean`). PRs #279 + #288.
- **B (THIS PR):** the durable, data-driven Check 0 backend. Extends
  `scripts/alert_triage_state.py` so that per-signal triage is classified from the
  registry + the translation table (not prompt judgment), persisted in
  `~/agents/state/alert-triage.json`, routed via `larry_alerts.classify_route`,
  and — for the Tier-1 auto-fix path — recorded as a tagged
  `cycle_prime_ledger` intervention so per-pattern track record accrues.
- **C (next, OUT OF SCOPE here):** the promotion loop — Check V threshold-unify,
  graduate/demote operating on the registry, and the plain-language
  graduation-approval UX. **No graduation, demotion, or approval UX is built in B.**

## The gap B closes

Before B, `scripts/alert_triage_state.py` (shipped in #197) was a bare lifecycle
ledger: a flat `{alert_id: row}` state file with `pending → triaged-tier-N →
action-dispatched → resolved` transitions, but **no knowledge of the registry, no
route, no tier-4, and no idempotency guard.** The actual tier *decision* still came
from prompt judgment each iter — not durable, not consistent, not data-driven.

B makes the decision durable + data-driven. It is an **extension** of the existing
helper, not a parallel module: the lifecycle primitives (`record_triage`,
`mark_dispatched`, `mark_resolved`, atomic read/write) are reused unchanged; B adds
a classification + orchestration layer on top.

## Design — the data-driven § 6.6 decision table

For each new `larry-alerts.jsonl` signal, Check 0 evaluates the gates **in order**;
the first match wins (this mirrors the spec § 3.0 / § 6.6 table verbatim):

1. **Tier 3 (silence → digest).** `(source, subject)` matches a known pattern in
   `config/alert-translations.json` (exact subject first, then strip trailing
   `:`-segments — the table's own longest-prefix lookup rule). Larry already
   approved silence on this pattern, so it resolves silently. `route = digest`.
2. **Tier 2 (ask → escalate).** The signal's action-template is in the registry and
   is `permanent_guard` **OR** `state != "graduated"` (i.e. probation). DM Larry
   (plain-language); do **not** auto-act. `route = escalate`.
3. **Tier 1 (auto-fix → closure/digest).** The signal's action-template is in the
   registry, `state == "graduated"`, **and not** `permanent_guard`. The remediation
   is acted this iter, a **tagged** `cycle_prime_ledger` intervention is recorded
   (`template = the pattern id`), and the row advances to `action-dispatched`.
   `route` is stamped per significance via `classify_route` (significant →
   `closure`, routine → `digest`).
4. **Tier 4 (novel → escalate).** No registry template and no translation match.
   DM Larry for triage guidance; the answer trains the allowlist (Check IV, C-era).
   `route = escalate`.

### How a signal "maps to a registry template"

The registry keys on a kebab-case **action-template** (the remediation class), not
on an alert subject — the registry has no subject field, and B does **not** invent a
subject→template matcher (that would be a hidden, drift-prone second source of
truth). Instead, the seam is explicit and honest: **the producing healer tags its
alert with the canonical `template`** it would be remediated under (the same
kebab-case id it would pass to `cycle_prime_ledger --template`). A signal carrying a
`template` that is present in the registry is classified by that record's state +
guard; a signal with no registry template (and no translation match) is Tier 4
(novel). This keeps classification **single-source** — the registry and the
translation table are the only inputs — and lines the alert tag up 1:1 with the
ledger tag, which is exactly what lets Check V (C) accrue per-pattern track record.

### The B→C link (track-record accrual)

The load-bearing seam to Phase C is the Tier-1 ledger write. When Check 0 classifies
a signal Tier 1, it calls `cycle_prime_ledger.append_action(kind="intervention",
intervention_id=canonical_intervention_id(template, detail))`. Because the
intervention is tagged with the pattern's template, Check V (C) can later compute a
per-template clean-streak and propose graduation. **Even though nothing is graduated
today** (see below), the code path exists and is tested with a fixture-graduated
pattern, so C has data to graduate on the moment the loop ships.

### Route stays the routing layer's job

Check 0 only **stamps** the `route` (via `larry_alerts.classify_route`, the single
significance-consulting decision point from #277). It does **not** send DMs, write
digests, or deliver anything — delivery is #277's job. Tier 3 is the one carve-out
where the route is set to `digest` directly rather than through `classify_route`: a
silenced known pattern is journal-only by definition and must never produce a
`closure` DM, even if its subject is on the significance list.

### Idempotency (no double-acting across iters)

The orchestration entrypoint reads the existing lifecycle row first. If the signal
is already `action-dispatched` or `resolved`, classification + acting are **skipped**
and the existing row is returned unchanged — re-running an iter on an
already-handled alert is a no-op. In particular, a Tier-1 signal records its tagged
ledger intervention **exactly once**, never once per iter.

## Current live effect (nothing auto-fires yet)

With C not shipped, **every registry pattern is `state=probation`** — none is
`graduated`. So Check 0 correctly classifies registry signals as **Tier 2 (ask)** and
auto-fixes nothing. B forces no pattern to `graduated`. B's value *now* is durable,
consistent, data-driven classification + correct routing + track-record accrual, so
that when C ships the graduation loop there is real per-pattern data to graduate on.
The `permanent_guard` floor is enforced unconditionally: a guarded template (credential
/ money / irreversible) never classifies Tier 1 even if it were graduated.

## Out of scope (Phase C)

- Graduation / demotion of registry patterns.
- The plain-language graduation-approval DM + Larry's approve path.
- Check V threshold-unify + registry wiring.
- The richer spec § 3.0 state-file features not required by B's acceptance:
  watermark-based line claiming, healer-flood reclassification, and the
  `action_templates[]` execution-history slice. B keeps the existing flat
  `{alert_id: row}` schema (extended with `route` + `template` fields) rather than
  reshaping it into the spec's two-key `{alerts, known_patterns}` form — reshaping
  would be a rewrite, and these features are not in B's deliverables.
