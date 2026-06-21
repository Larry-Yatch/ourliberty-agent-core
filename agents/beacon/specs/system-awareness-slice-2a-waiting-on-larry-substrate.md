# Build spec — System self-awareness, Slice 2a: itemized cross-source "waiting on Larry" (substrate)

**Mission:** System self-awareness (the "standing brain") — see `docs/system-awareness-north-star.md`. Slice 2 = "What needs Larry"; this is **2a, the substrate** (2b = the dashboard render, follows).
**Status:** Draft v1 for build — 2026-06-21.
**Repo:** **ourliberty-agent-core** (Python substrate).
**Author:** Claude Code (desktop). **Approver:** Larry.

> Consult done first (`build_check` 2026-06-21): no STRONG reuse miss; the reuse is the existing Approvals data + the State Log. Chosen shape (Larry): **unify into the standing brain** — extend the State Log, reuse the Approvals data, do NOT rebuild the Approvals system.

## 0. Goal

Extend the State Log's `waiting_on_larry` from a bare parked count (`{parked: N}`) into an **itemized, cross-source decision list** — aggregating **parked captures + pending approvals + for-Larry escalations** — so "everything blocked on Larry's decision" lives in one place (the standing brain), readable by the `/where-we-are` page (Slice 2b), Beacon, and later the Orchestrator. **Read-only aggregation**; reuse existing sources; never mutate them.

## 1. Why

Slice 1/1b deliver "where we are" + a parked count. Slice 2 = "what needs Larry" — the piece that most directly attacks the bottleneck. The Approvals tab and the Missions funnel already hold the pieces; this **unifies** them into the State Log, reusing their data — it does not duplicate the Approvals UI.

## 2. Reuse (read the SAME sources — do not rebuild)

- **Pending approvals:** read `~/agents/state/beacon-pending-approvals.json` → `pending[]` (the file owned by `scripts/beacon_approval_handler.py`; see its `load_state()` for the shape). Per-entry fields: `id`, `plan_summary`, `created_at`, `target_agent`. **Read-only** — do not import/alter the handler.
- **For-Larry escalations:** the Larry-visible escalations only (conservative — avoid false "needs you"). Prefer the canonical Larry-facing form (chain_events `event_type='escalation'` with an explicit for-Larry flag); `~/agents/blackboard/pulse-escalations.json` is ops-internal — only fold in entries explicitly flagged for Larry. If no clean for-Larry signal exists, include **none** rather than guess (note it in the PR).
- **Parked captures:** already read by `load_parked_count()` (captures.json `state == 'parked'`). Itemize the SAME read.
- Do **not** touch `lib/approval-queries.ts`, the Approvals tab, or the approval-handler logic.

## 3. Deliverables (all in `scripts/system_state_log.py` + its tests)

### D1 — New fail-open source readers (mirror `load_parked_count`'s pattern)
- `load_pending_approvals() -> list[dict]` — read beacon-pending-approvals.json `pending[]`, map each to a waiting-item. Fail-open: any read/parse error → `[]`.
- `load_for_larry_escalations() -> list[dict]` — read the for-Larry escalations (conservative). Fail-open → `[]`.
- Itemize parked: extend the captures read so we can produce parked **items** (id, title, why/briefing, age), not just the count.

### D2 — Extend `waiting_on_larry` in `build_snapshot` (~line 385)
Replace `'waiting_on_larry': {'parked': parked}` with:
```python
'waiting_on_larry': {
    'parked': <int>,             # KEEP — back-compat: the current /where-we-are panel reads this
    'pending_approvals': <int>,
    'escalations': <int>,
    'total': <int>,
    'items': [                   # the itemized cross-source list (bounded, ordered)
        {'source': 'approval' | 'escalation' | 'parked',
         'id': str, 'title': str, 'why': str,
         'age_seconds': int,
         'severity': 'critical' | 'warning' | 'info' | None,
         'action_hint': str},   # e.g. "approve/reject in Approvals", "promote/drop in Missions"
    ],
    'truncated': bool,           # true if items were capped
}
```
- **Keep `parked`** so the existing /where-we-are panel keeps working until 2b ships.
- **Bound** `items` (cap ~25), ordered most-urgent first: escalations → pending approvals → aged parked. Set `truncated`/`total` so the surface can say "+N more".

### D3 — Narrative
Extend `build_narrative` + the deterministic fallback (~line 435) so the "Waiting on you" line reflects the cross-source total, e.g. *"Waiting on you: N items — A approvals, E escalations, P parked."* Keep it fail-safe (fallback never crashes / never empty).

### D4 — Tests (`scripts/tests/test_system_state_log.py`)
- Fixtures for each source (a pending-approvals file, for-Larry escalations, parked captures) → `waiting_on_larry.items` aggregates all three with correct counts + ordering.
- **Independent fail-open:** a missing/malformed file for ONE source contributes `[]` while the others still aggregate (no crash).
- The narrative reflects the cross-source count.
- `parked` back-compat field still present.

## 4. Acceptance criteria

- [ ] `waiting_on_larry` has `parked` + `pending_approvals` + `escalations` + `total` counts **and** a bounded, ordered `items` list; `parked` retained for back-compat.
- [ ] Each source fails open independently (one bad file never breaks the snapshot).
- [ ] Narrative mentions the cross-source waiting total.
- [ ] **Read-only:** never writes/mutates beacon-pending-approvals.json, escalations, or captures.
- [ ] Tests green; existing State Log tests unregressed (`python3 -m unittest scripts.tests.test_system_state_log`).

## 5. Out of scope (→ Slice 2b / later)

- **The `/where-we-are` render** of `items` — that's Slice 2b (a dashboard build; it consumes this schema, links to `/approvals` + `/missions` for actions).
- **Any actions** (approve/reject/promote) — read-only here.
- **PRs-awaiting-merge** as a source — deferred (most auto-merge; add later if it proves needed).

## 6. Notes

- Reuse, don't rebuild: the State Log is a read-only summary layer over the Approvals + Missions data; read the same files/tables they do.
- Keep the `items` schema (D2) stable — Slice 2b renders it.
- (NOTE for the builder: an earlier Explore mis-read a stale local checkout — `waiting_on_larry` is in THIS file `system_state_log.py` at the `build_snapshot` assembly, NOT in `ceo_digest_generator.py`; and `/where-we-are` already exists. Build against current `origin/main`.)
