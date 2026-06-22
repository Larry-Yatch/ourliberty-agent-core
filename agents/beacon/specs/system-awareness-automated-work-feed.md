# Build spec — System self-awareness: the "Automated Work" feed

**Mission:** System self-awareness (the "standing brain") — see `docs/system-awareness-north-star.md` §6 item 2 ("Automated Work — the mirror; the autonomy dial-in surface"). The mirror of Slice 2's "what needs Larry": **what the team did on its own.**
**Status:** Draft v1 for build — 2026-06-21.
**Author:** Claude Code (desktop). **Approver:** Larry.
**Repos:** endpoint = **ourliberty-agent-core** (this PR); render = **ourliberty-dashboard** (follow-on PR, §5).

> Born from Larry's ask (2026-06-21, right after autonomy went live): *"a plain-language list of automated work from across the system + the audit log behind it, as the instrument to dial in autonomy."* The substrate — the `autonomy_decision` chain_event (agent-core #623) — is now flowing live (proven 2026-06-21: `fix-phantom-build-phase-terminal-guard-001` auto_approve row landed in Supabase). This spec surfaces it.

## 0. Goal

A read-only, plain-English feed of the trust-policy decisions the team made **without Larry's click** — primarily the **auto-fired dispatches** (`decision='auto_approve'`), each row showing what was dispatched, where, when, and the **trust rule that fired**, plus window counts of what it **asked** about (`force_ask`) and **auto-blocked** (`reject`). This is the autonomy **dial-in instrument**: Larry audits what the autonomy did and adjusts the trust policy with evidence, not guesses.

## 1. Why

Autonomy went live (Beacon→Forge agent-core auto-fires; trust-policy gate). Larry asked to *see* it. The `autonomy_decision` record is the durable audit substrate for BOTH halves of Slice 2 — `force_ask`→"what needs Larry" (Slice 2a/2b), `auto_approve`→**this feed**. Without the feed, the autonomy is invisible and undial-able.

## 2. Architecture decision (grounded 2026-06-21)

- `autonomy_decision` rows live **only in Supabase `chain_events`** (push-emit; no local mirror). 
- `scripts/system_state_log.py` is **deliberately local-files-only** ("No HTTP; no Supabase" invariant) — so the feed does **NOT** route through the State Log substrate (it would break that boundary).
- The feed is a **droplet API endpoint** that queries `chain_events`, mirroring the existing `dashboard_api._fetch_chain_events_for_agent` / `get_system_agent_queue` pattern (the dashboard_api already holds the Supabase client + bounded-window query idiom). The dashboard renders it via the same same-origin proxy + `useDashboardData` the `/where-we-are` page already uses.

## 3. Deliverable (this PR — agent-core)

A new token-gated endpoint **`GET /api/system/automated-work`** in `scripts/dashboard_api.py` + a fail-safe reader + a pydantic response model + tests.

### 3.1 Endpoint
```
GET /api/system/automated-work?window_days=14&limit=50
```
- `window_days` (default 14, 1–90): trailing window over `chain_events.ts`.
- `limit` (default 50, 1–200): cap on returned feed `items` (the auto_approve list).
- Token-gated via the existing `Depends(_require_token)`.

### 3.2 Reader — `_reader_automated_work(client, *, window_days, limit) -> dict`
- **Fail-safe (never 500):** `client is None` (test env / no creds) → `{'present': False, …, 'items': [], 'counts': {…0}, 'truncated': False}`. Any query exception → same degraded shape. (Mirror `_fetch_chain_events_for_agent`'s `None`-on-error discipline.)
- Query: `chain_events` where `event_type='autonomy_decision'` and `ts >= cutoff`, `order('ts', desc=True)`, `.limit(_AUTOMATED_WORK_QUERY_CAP)` (a safety cap ~500 so counts stay bounded). Select `task_id, ts, pr_url, payload`.
- From the rows: **counts** over the window by `payload.decision` — `auto_approved` / `asked` (force_ask) / `rejected`. **items** = the `auto_approve` rows (most-recent first), capped at `limit`; `truncated=True` when more auto_approve rows existed than `limit`.
- Each item maps from the `autonomy_decision` payload:
  - `task_id`, `ts`, `age_seconds` (int, vs now), `pr_url` (if any),
  - `decision`, `dispatched`, `source`, `target_agent`, `target_repo`, `task_type`, `summary`,
  - `rule_label`: a concise plain-language label from `payload.matched_rule` — `"{source} → {target} · {repos joined}"` (e.g. `"beacon → forge · ourliberty-agent-core"`); `matched_rule is None` → `"default policy"`. Do **NOT** surface the rule's verbose `_note`.
  - `rule_action`: `matched_rule.action` (or `None`).

### 3.3 Response shape (the contract the dashboard renders — keep stable)
```jsonc
{
  "present": true,                 // false when Supabase unavailable — render degrades, never errors
  "window_days": 14,
  "counts": { "auto_approved": 3, "asked": 1, "rejected": 0 },
  "items": [                       // auto_approve only, most-recent first, capped at `limit`
    {
      "task_id": "fix-phantom-build-phase-terminal-guard-001",
      "ts": "2026-06-22T05:34:25Z", "age_seconds": 1234, "pr_url": null,
      "decision": "auto_approve", "dispatched": true,
      "source": "beacon", "target_agent": "forge",
      "target_repo": "ourliberty-agent-core", "task_type": "feature-development",
      "summary": "Add a terminal guard to the phantom-build-phase retry path…",
      "rule_label": "beacon → forge · ourliberty-agent-core", "rule_action": "auto_approve"
    }
  ],
  "truncated": false
}
```

### 3.4 Tests (`scripts/tests/test_dashboard_api_automated_work.py`)
Use the established recording-stub pattern (monkeypatch `_get_larry_action_supabase_client`, like the other chain_events-reading endpoint tests):
- mixed window of `autonomy_decision` rows (auto_approve ×N, force_ask, reject) → `items` is auto_approve-only, most-recent first; `counts` reflects all three; `rule_label` derived; `age_seconds` present.
- `client=None` → `present=False`, empty items, zero counts, **200 not 500**.
- query raises → degraded shape (no 500).
- `limit` smaller than the auto_approve count → `truncated=True` and `len(items)==limit`.
- `matched_rule=None` row → `rule_label="default policy"`.

## 4. Acceptance criteria
- [ ] `GET /api/system/automated-work` returns the §3.3 shape; token-gated.
- [ ] Reader fails safe (None client / query error → `present:false`, 200, empty) — never 500s the dashboard.
- [ ] `items` are auto_approve-only, most-recent first, bounded by `limit` with `truncated`; `counts` cover auto_approved/asked/rejected over the window.
- [ ] `rule_label` is concise plain-language; the verbose `_note` is never surfaced.
- [ ] Tests green; no State Log / `system_state_log.py` change (boundary respected).

## 5. Out of scope (→ the dashboard render PR + later)
- **The dashboard render** — an "Automated work" feed panel on `/where-we-are` (reuse `PanelShell` + `useDashboardData("/api/system/automated-work")` + `PanelErrorBoundary`), rows showing the plain-English summary + repo + relative time + `rule_label`, each drilling to its `chain_events` trail (reuse the timeline humanizer where wired; a `pr_url` link otherwise). Separate PR (ourliberty-dashboard).
- **Per-row chain_events drill-through pane** beyond a link — reuse `ChainTimelineDrawer` if cheap, else follow-up.
- **Narrator voice enrichment** — the payload `summary` is already Beacon-authored plain English; deterministic templating in v1, optional `missions_narrator` enrichment later.
- **Doorbell DM** (force_ask accrual nudge) — tracked separately under Slice 2.

## 6. Notes
- Reuse, don't rebuild: this is a thin read over `chain_events`, mirroring the agent-queue lane reader. No new Supabase wiring — reuse `_get_larry_action_supabase_client`.
- Keep the §3.3 shape stable — the dashboard render PR types against it.
