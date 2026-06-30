# Approval-Sync Phase 2 — Build Spec: converge the stores under one decision key

**Status:** Spec for the team to build — 2026-06-30.
**Parent:** [approval-sync-north-star.md](approval-sync-north-star.md) §6 Phase 2, §8 open questions.
**Depends on:** Phase 1 (merged, #771) — the `heal_stale_approvals` resolved-in-supabase reconciler and the `task_terminal_state` prefix-strip. Phase 2 generalizes Phase 1's point-fix into a shared identity + a single resolve fan-out.
**Owner / approver:** Larry. **Risk tier:** higher than Phase 1 — touches the live resolve path on both the Telegram and dashboard sides, not just a healer. Build behind the existing `/api/larry/action` → droplet seam; no new dashboard write surface.

---

## 0. Why this phase

Phase 1 stopped the bleeding: a dashboard-approve now deterministically pops Beacon's queue within one 10-min healer tick. But that is **one reconciler patching one of four stores after the fact.** The root cause named in the North Star §2 is still open: the same logical "needs-Larry" item is authored into **four stores with no shared identity**, and resolution in one store does not fan out to the others. Phase 2 makes resolution **synchronous and total** instead of healer-reconciled-after-the-fact:

- **One decision identity** every store keys off (closes §5 gap 4).
- **One `resolve_decision(key, outcome)`** that clears all four stores, called by both surfaces (generalizes Phase 1; removes reliance on the 10-min healer for the common case).
- **Fix the State Log escalations-bucket undercount** (closes §5 gap 5) — the live bug where `waiting_on_larry.escalations` read `0` while `for-larry-escalations.json` held 3 unresolved (#749/#751/#766).

The Phase 1 healer stays as the **backstop** (for Supabase-unreachable / offline-resolve windows); Phase 2 makes the happy path not need it.

---

## 1. Decisions locked (resolves North Star §8 open questions)

These were open; this spec closes them so the build is unambiguous. Preflight may challenge with evidence, but default to these:

1. **Canonical key = the `task_id`, normalized through one shared helper `canonical_decision_key()`.** Not a new `decision_id` column (too invasive across already-shipped Supabase rows and JSON history), not a raw PR-coordinate (session-less/healer-minted items have no PR yet). The helper normalizes every representation to one string:
   - strip the wrapper prefixes `mirror-review-`, `heal-`, `fix-`, `land-` (reuse the Phase 1 prefix list already in `task_terminal_state.expand_variants` — **import it, do not re-list**);
   - when `task_id` is absent but a `pr_url` is present, derive `pr-<repo>-<num>` from the URL (reuse `parse_already_merged_pr_ref` / the `_PR_TASK_ID_RE` at `heal_pipeline_stall.py:776` — do not re-author the regex);
   - otherwise the normalized `task_id` is the key verbatim.
   This is a **pure function** with no I/O — it is the single join key across P/C/E/A.

2. **`resolve_decision()` lives on the droplet, single owner.** The dashboard already proxies approve/reject to the droplet via `/api/larry/action` (`dashboard_api.py:8565` CAS + `:8661` audit). The droplet handler calls `resolve_decision()`. The Telegram approve/reject path (`beacon_approval_handler.resolve`, `:649-689`) calls the **same** function. No resolution logic is duplicated in the dashboard/TS layer — TS stays a thin proxy.

3. **`for-larry-escalations.json` stays a feed for now** — it participates in the canonical key + the resolve fan-out, but is **not** retired/folded in this phase. Folding it into the canonical ledger is Phase 3 cleanup (lower blast radius to keep it during the resolve-path change).

---

## 2. Scope — three changes

### Change A — `canonical_decision_key()` shared helper
New pure function (suggested home: `scripts/decision_identity.py`, or alongside `task_terminal_state` if cohesion is better — preflight decides).
- Signature: `canonical_decision_key(task_id: str | None, pr_url: str | None = None) -> str | None`.
- Behavior per §1.1. Returns `None` only when neither a task_id nor a derivable PR coordinate exists (caller then falls back to today's behavior — no key, no cross-store join).
- **Stamped at emit time** onto every needs-Larry record so the join key is present at rest, not recomputed on every read:
  - `beacon_approval_handler.add_pending` (`:541`) — stamp `decision_key` on the pending entry.
  - `chain_event_emit.emit_event` for the Larry-facing types (`approval_request`, `clarify_request`, `needs_attention`, `larry_alert`, `escalation`) — stamp `decision_key` into the `payload` JSONB (additive; no schema migration — `payload` is already `jsonb`).
  - `for_larry_signal.sync_active` (`:186`) — stamp `decision_key` on each escalation record.
  - `larry_alerts.append_approval_request` / `append_alert` for route=escalate — stamp `decision_key` on the line.
- **Backfill is read-time-tolerant:** existing rows lack `decision_key`; every consumer computes it on the fly from `task_id`/`pr_url` if the stamped field is absent, so the change is forward-compatible without a data migration.

### Change B — `resolve_decision(key, outcome, *, actor, note)` fan-out
New function (droplet-side; suggested home: `scripts/decision_resolve.py`). `outcome ∈ {approved, rejected, modified, expired}`. Idempotent; each store cleared best-effort with structured logging; one store failing does not abort the others (mirrors Phase 1's `contextlib.suppress` + log discipline). Fans out to:
1. **P** — `beacon-pending-approvals.json`: find the pending entry whose `canonical_decision_key(entry)` == `key`, call the existing `approval.resolve(entry['id'], outcome, note)` (`:649-689`). (Today's resolve is keyed on the raw `id`; the new lookup matches on the **normalized** key so wrapped ids resolve too.)
2. **C** — Supabase `chain_events`: set `read_at` on **all** unread rows whose `payload->>'decision_key'` == key OR whose normalized `task_id` == key, across the Larry-facing event types — not just the one `approval_request`. Use the existing atomic-update path (`chain_event_emit.clear_approval_request` generalized from by-`task_id` to by-`decision_key`, `:205-264`). This is what closes §5 gap 6 (a retracted/resolved item leaving a stale shipped `larry_alert`/`escalation` row).
3. **E** — `for-larry-escalations.json`: flip `resolved: true` (+ `resolved_at`) on any record whose key matches (`for_larry_signal.resolve_record`, `:161`).
4. **A** — `larry-alerts.jsonl`: retract the matching escalate line(s) via `larry_alerts.resolve_alert` (`:497`) so the alert feed and its already-shipped C row agree.

**Both call sites:**
- Telegram: `beacon_approval_handler.resolve` (and the auto-approve / replan-exhaust paths) call `resolve_decision()` **instead of** the local-only `resolve()` + best-effort `_clear_dashboard_pending` (`:692-700`) — so the fan-out is the single resolution primitive.
- Dashboard: the droplet `/api/larry/action` handler (`dashboard_api.py:8565/8661`) calls `resolve_decision()` after the CAS + audit-row write.

The Phase 1 `heal_stale_approvals.reconcile_resolved_in_supabase` reconciler **remains** as the offline/failed-fan-out backstop. Its keying should be updated to the same `canonical_decision_key()` so backstop and primary agree.

### Change C — State Log escalations-bucket undercount fix
Symptom (live 2026-06-30): `system-state-log.json` `waiting_on_larry.escalations == 0` while `for-larry-escalations.json` held **3** records with `resolved == false` (#749, #751, #766). The doorbell/Where-are-we therefore **undercount** what is waiting.
- Diagnose the derivation in `system_state_log.py` (the escalations branch ~`:502`, reading `for-larry-escalations.json` + `pulse-escalations.json`). Likely a too-narrow filter (e.g. requiring `for_larry == true` when those 3 records have it unset/false, or a since-window cutoff). Preflight must confirm the exact cause against the live file before changing the filter.
- Fix so that **every `resolved == false` escalation record surfaces in `waiting_on_larry.escalations`** with its `decision_key`, and is removed the instant `resolve_decision()` flips it (Change B step 3) — so this bucket can never again disagree with the file.
- Add a regression test pinning the live failing shape: 3 unresolved records → `escalations == 3`.

---

## 3. Success criteria
- A single `canonical_decision_key()` maps a wrapped id (`mirror-review-pr-ourliberty-agent-core-763`), a bare PR coordinate (`pr-ourliberty-agent-core-763`), and a `pr_url` for the same PR to **one** string. Unit-tested across the four shapes + the session-less/healer-minted no-PR case.
- Approving/rejecting **once on either surface** clears the item from P, C, E, and A **synchronously** (verified by a test that seeds all four stores under one key and asserts all four cleared after one `resolve_decision()` call) — without waiting for any healer tick.
- `waiting_on_larry.escalations` equals the count of `resolved == false` escalation records (regression test on the live 3-record shape).
- The Phase 1 healer still retires an item that was resolved while Supabase was unreachable (backstop unbroken) — existing Phase 1 tests stay green.
- No new dashboard mutation endpoint; TS layer unchanged except (if needed) passing `decision_key` through the existing action proxy.
- Idempotent: calling `resolve_decision()` twice on the same key is a no-op the second time (no exceptions, no double-audit-row).

## 4. Out of scope (later phases)
- Retiring/folding `for-larry-escalations.json` into the canonical ledger → Phase 3.
- The Approvals-tab UI unification, decisions/parked split, nav counter, pointing other pages at it via links → Phase 3.
- Beacon's scoped read-only query → Phase 4.
- Parked-capture completion write-back (`drain_board_to_beacon` stamps `spawned`, never flips on ship) — **adjacent**; list it in the Phase 3 brief unless preflight finds it trivially co-located with Change B (then fold it, tested).
- Collapsing the two doorbells / three promotion timers / duplicated `_primary_chat_id()` → Phase 3 redundancy cleanup.

## 5. Files (anchors, verify against HEAD — Phase 1 shifted some lines)
- NEW `scripts/decision_identity.py` (`canonical_decision_key`), `scripts/decision_resolve.py` (`resolve_decision`) — or cohesive existing homes if preflight prefers.
- `scripts/beacon_approval_handler.py` — `add_pending` (~:541) stamp; `resolve` (~:649-689) + `_clear_dashboard_pending` (~:692-700) call `resolve_decision()`.
- `scripts/chain_event_emit.py` — stamp `decision_key` in `emit_event` payload; generalize `clear_approval_request` (~:205-264) to by-`decision_key` across Larry-facing types.
- `scripts/for_larry_signal.py` — stamp at `sync_active` (~:186); `resolve_record` (~:161) reachable from the fan-out.
- `scripts/larry_alerts.py` — stamp at `append_alert`/`append_approval_request` (~:311/:811); `resolve_alert` (~:497) reachable from the fan-out.
- `scripts/system_state_log.py` — escalations derivation (~:502) Change C.
- `scripts/heal_stale_approvals.py` — re-key Phase 1 reconciler to `canonical_decision_key()`.
- `scripts/task_terminal_state.py` / `scripts/heal_pipeline_stall.py` — **import** the existing prefix list + `_PR_TASK_ID_RE`, do not duplicate.
- Dashboard: `ourliberty-dashboard` droplet `/api/larry/action` handler → call `resolve_decision()`; no TS resolution logic.
- Tests under `scripts/tests/` for each change + the four-store fan-out integration test.

## 6. Doctrine
This is recovery/identity plumbing with rule-shaped behavior. The tests are the enforcement mechanism — each change ships with a test that fails before it and passes after. Conservative direction throughout: when a key is underivable, **fall back to today's behavior** (no cross-store join) rather than guessing a wrong join that could clear an unrelated item. `resolve_decision()` must never clear a store entry whose key does not match — a mis-join is worse than a missed join (a missed join is caught by the Phase 1 healer; a mis-join silently drops a real decision).

## 7. Preflight (first dispatch — read, decide, emit one marker)
Read this spec + the North Star + the named file ranges + the live `for-larry-escalations.json` / `beacon-pending-approvals.json` on the droplet. Confirm: (a) the exact cause of the Change C escalations undercount; (b) the best home for the two new helpers; (c) that the prefix list + PR regex are importable (not copy-paste). Then emit PROCEED / CLARIFY_REQUEST / REJECT with one marker block. Build is a separate dispatch.
