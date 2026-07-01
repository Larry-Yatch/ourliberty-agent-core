# Approval-Sync Phase 2.1 — Build Spec: fix the resolve-path over-resolution + incomplete Change C

**Status:** Spec for the team to build — 2026-07-01.
**Parent:** [approval-sync-north-star.md](approval-sync-north-star.md); follows Phase 2 ([approval-sync-phase2-spec.md](approval-sync-phase2-spec.md), merged #781).
**Born from:** a post-merge `/code-review high` on #781 (5 finder angles + adversarial verify). Phase 2's core (canonical key + one fan-out) is sound; this phase closes the correctness holes the review confirmed. Findings recorded in the review; the two load-bearing ones were code-verified.
**Risk tier:** HIGH — resolve path. This document is a docs-only spec and may merge normally; the deep-review hold applies to the **downstream build PR that implements this spec**, not to this spec PR. That build PR is the "deep-review" mission: it must NOT auto-merge ahead of a human `/code-review high` (see the companion [mirror-review-merge-gate-brief.md](mirror-review-merge-gate-brief.md)); until that gate exists, hold the build PR manually for review.

---

## 0. Why this phase
Phase 2 introduced `canonical_decision_key()` + `resolve_decision()`. The review found the key is **not injective** and the fan-out **acts on every key-match**, so resolution can hit decisions the operator never touched; and Change C (the escalations-bucket fix) **repointed a reader without closing the underlying dual-writer collision**, shifting the blind spot. This phase makes resolution act on exactly the acted-on decision, and makes the escalations feed a single coherent store.

## 1. Changes (ranked by severity)

### FIX 1 — P-leg must pop only the acted-on entry, not every key-match *(blocker)*
**Defect:** `decision_resolve._resolve_pending(key, ...)` resolves EVERY pending entry whose `canonical_decision_key` matches. Because `pr_url` takes precedence in the key, two distinct pending approvals for the same PR (a head-aware re-review after a new push — see #539; or a re-plan) collapse to one key. Approving one via Telegram (or `mark_done` on the dashboard) also moves the sibling to `history` as `approved` — but only the acted-on entry is dispatched, so the sibling's work never runs. This is the exact silent-swallow Phase 2 set out to kill, and a regression from the old `approval.resolve(entry['id'], ...)` (pop-exactly-one).

**Fix — decouple pop-target from cross-store key:**
- `resolve_decision` gains an explicit **`entry_id`** (the specific pending id the operator acted on), distinct from the cross-store `key`. The **P-leg resolves ONLY `entry_id`** (via `approval.resolve(entry_id, outcome)`); it never scans-and-resolves siblings. The **C/E/A legs continue to use `key`** for cross-surface clear.
- Both call sites already know the exact entry: Telegram passes the selected `entry['id']`; the dashboard `mark_done`/approve acts on a specific approval row — thread its id through. Derive the `key` from that same entry for the C/E/A legs.
- Guard: if `entry_id` is absent (a pure escalation/alert resolve with no pending approval), the P-leg is a **no-op** — never resolve a pending approval that wasn't the target. (Directly fixes the `mark_done`-on-escalation swallowing a key-colliding pending plan.)
- **`resolve` ≠ `dispatch`:** the P-leg only pops; it must never leave an entry `approved`-in-history that was not dispatched. With entry_id-scoping this holds by construction.

### FIX 2 — close the `for_larry_signal` ⇄ `for_larry_escalations` dual-writer collision *(blocker)*
**Defect:** both modules write the SAME file `~/agents/blackboard/for-larry-escalations.json` under incompatible top-level schemas — `for_larry_signal` writes `{'records': {...}}` (used by `promote_alerts` critical-unhandled + `outbox_notifier` CLARIFY-exhausted + two reconcile healers), `for_larry_escalations` writes `{'escalations': [...]}` (mirror-review). Each `_save` writes only its own key, clobbering the other. Phase 2 Change C repointed the State-Log reader to `for_larry_escalations.list_open()` — fixing the mirror-review undercount (#749/#751/#766) but going **blind to every `for_larry_signal` producer** and leaving live data-loss.

**Fix (converge the store — do NOT ship a reader-only patch again):**
- **One lock:** make `for_larry_escalations` acquire the **same** `file_lock.exclusive_lock(file_lock.sidecar_lock_path(path))` that `for_larry_signal` already uses, around every load→mutate→save envelope (`_save`/`upsert`/`clear`). Today it uses bare `atomic_write_json` with no flock, so even same-module writes race and cross-module writes clobber.
- **Schema-preserving writes:** each writer must PRESERVE the other's top-level key. Write `{'version', 'updated_at', 'records': {...}, 'escalations': [...]}` — a `for_larry_escalations._save` reads the current doc and rewrites the `escalations` list while carrying `records` through untouched, and vice-versa in `for_larry_signal`. No writer drops a key it doesn't own.
- **Union reader:** `system_state_log.load_for_larry_escalations` reads BOTH `for_larry_escalations.list_open()` AND `for_larry_signal.active_entries()`, dedup by `canonical_decision_key` (an item present in both surfaces counts once). No producer is blind.
- **Batch clear (fixes the E-leg O(N) race):** `decision_resolve._resolve_escalations` must collect all matching ids from ONE `list_open()` and clear them in ONE locked load-filter-save, not a per-row `clear()` loop (which released the lock between rewrites). Add/So use a `for_larry_escalations.clear_many(ids)` under the shared lock.
- Full unification into a single module/store remains Phase 3; FIX 2 makes the two-writer file safe in the meantime (no clobber, no blindness).

### FIX 3 — restore the Phase 1 backstop for PR-coordinate entries *(should)*
**Defect:** `heal_stale_approvals.fetch_larry_actions` derives the key from the dashboard `larry_action` row, which carries neither `pr_url` nor a stamped `decision_key`, so it normalizes to the bare `task_id` and never matches a pending entry keyed by its PR coordinate — the backstop can't reconcile exactly the PR-url class the fan-out most needs backstopping.
**Fix:** the dashboard resolve path (`dashboard_api` `_handle_larry_action`) already computes the `decision_key` to call `resolve_decision` — **stamp that `decision_key` into the `larry_action` chain_events payload.** Then `fetch_larry_actions` matches on the stamped key. (Belt-and-suspenders: also index the reconcile by the entry's own stamped key.)

### FIX 4 — Telegram approve needs an atomic claim before dispatch *(should)*
**Defect:** the Telegram path calls `dispatch_approved(entry)` then `resolve_decision` with no lock/claim across the pair, so a double-tap or a concurrent auto-approve can dispatch the same task to Forge twice (a paid duplicate build); the dashboard path already guards this with an atomic `read_at` claim.
**Fix:** claim the entry atomically before dispatch — a CAS on the pending entry (`status: pending → dispatching`) under `state_lock`; a second concurrent approve sees non-`pending` and no-ops. Mirror the dashboard's claim discipline.

### FIX 5 — C-leg task_id fallback: stop the mis-join, keep it decision-scoped *(should)*
**Defect:** `chain_event_emit.clear_decision`'s second UPDATE does `.eq('task_id', key)`; with a PR-coordinate key it can set `read_at` on unrelated `larry_alert`/`needs_attention` rows whose `task_id` legitimately equals `pr-<repo>-<num>` (mis-join — "worse than a missed join"), while legacy wrapped rows (`task_id='mirror-review-<stem>'`, key `'<stem>'`) match neither leg (under-clear).
**Fix:** restrict the `task_id` fallback leg to the **decision event types** (`approval_request`, `clarify_request`) so it can never retire an unrelated alert/attention row. Accept the legacy-wrapped under-clear as backstop-covered (safe direction), OR backfill `decision_key` on those rows. Collapse the two sequential UPDATEs into one `.or_(...)` round-trip while here.

### FIX 6 — leg ordering for crash-safety *(nice-to-have)*
Reorder `resolve_decision` legs so **P is resolved LAST** (C/E/A first). A hard crash mid-fan-out then leaves the pending P entry intact, and `heal_stale_approvals` (which keys reconcile off pending P entries) re-drives the whole fan-out on its next tick. Today P-first + crash strands C/E/A with nothing to reconcile against.

### FIX 7 — extract the shared identity helpers (kills the drift that caused this) *(should)*
`decision_identity` should own **one** `pr_coord_from_url()` (retire the 6th private copy of the GitHub-PR-URL regex) and **one** `key_for_record(record)` fail-safe wrapper (stamped-else-derive), imported by all ~6 sites that currently re-inline it (`decision_resolve`, `heal_stale_approvals`, `system_state_log`, `beacon_telegram_bot`, `dashboard_api`, `larry_alerts`, `for_larry_escalations`). Divergence between those copies is itself a cross-store mis/missed-join.

## 2. Success criteria
- Two pending approvals for the same PR: approving/rejecting one resolves+dispatches ONLY that entry; the other stays pending (test seeds two same-`pr_url` entries, asserts one resolved + one still pending after resolve).
- `mark_done` on an escalation that shares a key with a pending approval leaves the pending approval **untouched** (regression test).
- A `for_larry_signal`-written record surfaces in `waiting_on_larry`; a concurrent `for_larry_escalations` write does not drop it and vice-versa (test writes via both modules, asserts both survive and both surface).
- E-leg clears N matching escalation rows in ONE file rewrite under the shared lock (test asserts single write + no lost concurrent upsert).
- A dashboard-approved PR-coordinate entry is reconciled by `heal_stale_approvals` when the fan-out is skipped (backstop test).
- Double-tap Telegram approve dispatches to Forge exactly once (claim test).
- C-leg never sets `read_at` on a `larry_alert`/`needs_attention` row (mis-join test); all existing Phase 2 tests stay green.

## 3. Out of scope
- Full unification of `for_larry_signal`/`for_larry_escalations` into one module (Phase 3).
- The Approvals-tab UI unification / nav counter (Phase 3); Beacon scoped read (Phase 4).

## 4. Files (verify vs HEAD)
`scripts/decision_resolve.py` (FIX 1 entry_id-scoping, FIX 2 batch clear, FIX 6 ordering), `scripts/decision_identity.py` (FIX 7 helpers), `scripts/for_larry_escalations.py` (FIX 2 lock + schema-preserving + clear_many), `scripts/for_larry_signal.py` (FIX 2 schema-preserving), `scripts/system_state_log.py` (FIX 2 union reader), `scripts/chain_event_emit.py` (FIX 5), `scripts/dashboard_api.py` (FIX 1 thread entry_id, FIX 3 stamp decision_key), `scripts/beacon_telegram_bot.py` (FIX 1 entry_id, FIX 4 claim), `scripts/heal_stale_approvals.py` (FIX 3), tests under `scripts/tests/`.

## 5. Doctrine
Conservative resolve semantics: **act on exactly the entry the operator touched; use the shared key only to clear the OTHER surfaces.** A mis-join (clearing an unrelated decision) is worse than a missed join (the healer catches that). Every fix ships with a test that fails before and passes after.

## 6. Preflight
Read this spec + the #781 review findings + the named file ranges + live `for-larry-escalations.json` on the droplet (confirm both `records` and `escalations` keys can coexist). Confirm FIX 1's entry_id thread-through is clean at both call sites and FIX 2's schema-preserving writes don't break `active_entries()`/`list_open()` consumers. Emit PROCEED / CLARIFY_REQUEST / REJECT with one marker. Build is a separate dispatch.
