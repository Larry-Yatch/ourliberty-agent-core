# Ledger tagging enforcement — closing the untagged/mislabeled-row gap

**Task:** `harden-ledger-intervention-tagging-002` · **Phase A completion** · 2026-06-03

## The gap

PR #279 added the `--template`/`--detail` CLI seam to `cycle_prime_ledger.py` and made a
NON-CONFORMING (non-kebab) template fail loud. But nothing **enforced** that an intervention
is recorded *with* a template at all. The cycle's LLM could (and did) omit the flag, and the
write layer happily appended `kind=intervention` rows with an empty `intervention_id`.

Live evidence (2026-06-03):
- Ledger iters 764-766 tagged correctly (`pulse-cycle-check:iter-N`).
- Iters 767-771 reverted to `intervention_id=""` — the LLM dropped `--template`.

Empty/untagged rows break Check V's per-template streak: `pulse_check_v._template_of()`
returns `''` for an empty id, so the row is silently skipped — the data point is **lost**,
not just unbucketed. The promotion ladder (track record B and C depend on it) can't see it.

## The two causes

We classified the empty-id rows against the cycle journal:

- **(b) — NO-OP / clean iter mislabeled as `kind=intervention` (DOMINANT, confirmed).**
  Every cycle iter records *itself* as `kind=intervention` with `pulse-cycle-check:iter-N`.
  Journal iter 777 is decisive: item 4 reads *"No always-allowed auto-fixes triggered"* (the
  iter is CLEAN) yet item 5 still appends a `kind=intervention` row. The "correctly tagged"
  iters and the empty-id iters are the *same* per-iter heartbeat — the empty ones just dropped
  the flag. The PRIME DIRECTIVE ratio (interventions=660, systemic_fixes=4, ratio≈165) is
  almost entirely these heartbeats inflating the denominator. Every clean iter looks like an
  untagged intervention.

- **(a) — REAL intervention recorded without `--template` (LATENT, not observed).**
  No real untagged intervention appears in the current evidence, but the same dropped-flag
  failure mode would silently lose a genuine intervention's data point. We defend against it
  rather than wait for it to happen.

## The fix

Enforcement lives at the **write layer** (`cycle_prime_ledger.py`), per the doctrine that
every rule earns an enforcement mechanism — prose in the cycle prompt is not enough.

1. **Mandatory template for `kind in (intervention, systemic_fix)`.** If a row of these kinds
   is recorded with an omitted/empty `intervention_id`, the write layer **normalizes** it to
   the reserved `uncategorized:<detail-or-iter>` template instead of writing an untaggable
   row. This preserves the data point (cause a never loses a real intervention), buckets every
   such row under ONE stable `uncategorized` template (no fragmentation), and visibly flags it
   as needing classification. A **malformed** (non-kebab) explicit template still fails loud —
   that behavior is unchanged.

   We chose normalize-to-`uncategorized` over hard-reject so an untagged *real* intervention is
   never dropped. The bucket is visible and countable, so a human can reclassify it later.

2. **`iter_clean` — a non-intervention kind for clean iters (cause b fix).** Clean/no-op iters
   stop being recorded as `kind=intervention`. They record under a new `iter_clean` kind, which
   `compute_ratio_30d` and `_template_of`/`compute_template_stats` already exclude (they only
   count `intervention`/`systemic_fix`). Check V's denominator therefore counts only genuine
   interventions. We keep recording clean iters (rather than omitting them) so the prime ledger
   retains a per-iter liveness marker — but one that is explicitly NOT an intervention.

3. **Reserved `uncategorized` registry record** in `config/auto-fix-patterns.json`:
   `permanent_guard: true`, `reversible: false`. It is the "classify me" bucket, never an
   auto-fix pattern, and permanently non-graduating.

4. **Never-graduate guard for `uncategorized` in `pulse_check_v.run_check`.** Belt-and-suspenders
   alongside the registry: even if `uncategorized` were added to a guard list with a long clean
   streak, Check V emits no `graduate` proposal for it.

5. **Cycle-prompt wording** tightened so the deterministic path is the path of least resistance:
   real interventions/systemic-fixes pass `--template`; clean iters use `--kind iter_clean`.

## Acceptance

- No new `kind=intervention` ledger row has an empty `intervention_id` — an untagged real
  intervention becomes `uncategorized:<detail>` (visible, countable, single bucket).
- Clean iters are recorded under `iter_clean`, excluded from Check V's denominator.
- Replaying the iters-767-771 condition produces tagged/correctly-kinded rows, not empty ones.
- `uncategorized` buckets to one template and never graduates.
- A malformed kebab template still fails loud.
