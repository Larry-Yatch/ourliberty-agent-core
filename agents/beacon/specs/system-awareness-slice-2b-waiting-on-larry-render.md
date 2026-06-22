# Build spec — System self-awareness, Slice 2b: dashboard "Waiting on you" itemized render

**Mission:** System self-awareness (the "standing brain") — see `docs/system-awareness-north-star.md`. Slice 2b of N (the last piece of "What needs Larry").
**Status:** Draft v1 for build — 2026-06-22.
**Repo:** **ourliberty-dashboard** (dashboard-repo build; this spec lives in agent-core per convention — set the mission's repo to `ourliberty-dashboard`).
**Author:** Claude Code (desktop). **Approver:** Larry.

> Grounded against the live dashboard repo `origin/main` + the **live** `GET /api/proxy/api/system/state-log` (verified 2026-06-22). Reuse-first: extend the existing `/where-we-are` page; reuse its proxy fetch + shared components. **READ-ONLY view — no actions, no writes. Do NOT rebuild the Approvals UI — link out to it.**

## 0. Goal

Slice 2a made the State Log **itemize** what needs Larry: `structured_snapshot.waiting_on_larry` now carries a per-source-counted, bounded, ordered `items[]` list (escalations → approvals → parked). Today the `/where-we-are` page's **"Waiting on you"** panel only renders the bare `waiting_on_larry.parked` count in an amber box.

Slice 2b renders that itemized list: each item with a **source badge** (escalation / approval / parked), its **title + plain-English why + relative age**, ordered as the substrate provides, with each item **linking out** to where Larry acts on it (approvals/escalations → the Approvals tab; parked → the Missions funnel). This completes the DES loop: open `/where-we-are` → see where everything is **and** what needs you, in plain English, one click from acting.

## 1. Why

Slice 1/1b gave Larry the whole-system glance; Slice 2a gave the substrate the itemized "what needs you" data. But the page still shows only a count — Larry can see *that* 22 things wait on him, not *what* they are or where to act. This panel is the payoff of the whole "What needs Larry" arc.

## 2. Reuse (extend these — do not invent patterns)

- **Extend** `app/where-we-are/page.tsx` (Slice 1b) — the `WaitingOnYouPanel` function specifically. Keep the page's structure, `PanelShell`, loading/stale/empty handling, and the other panels untouched.
- **Fetch** is already wired — the page polls `useDashboardData<StateLogResponse>("/api/system/state-log", …)`; this slice only changes rendering + types. No new fetch.
- **Reuse components/helpers:** `PanelShell` (in-file), `next/link` `<Link>` (see `components/Nav.tsx` for the import pattern), Tailwind v4 tokens (zinc / amber / rose / emerald) matching the existing pages.
- **Auth:** automatic via `proxy.ts` middleware — no per-page gate code.
- **Boundary — REUSE, do not rebuild:** the Approvals tab (`app/approvals/`, `lib/approval-queries.ts`) is the home for approvals/escalations. This panel **links to `/approvals`** — it does NOT re-implement the approvals list, queries, or actions. Parked items **link to `/missions`** (the funnel). No write/action controls in this panel.

## 3. The live shape (verified — type to THIS exactly)

`GET /api/proxy/api/system/state-log` → `structured_snapshot.waiting_on_larry` (verified live 2026-06-22, and confirmed against `scripts/system_state_log.py` `_build_waiting_on_larry` on `origin/main`):

```jsonc
"waiting_on_larry": {
  "parked": 22,             // count of parked captures — KEPT for back-compat (1b read this)
  "pending_approvals": 0,   // count
  "escalations": 0,         // count
  "total": 22,              // parked + pending_approvals + escalations (may exceed items.length)
  "truncated": false,       // true when total items exceeded the cap (25)
  "items": [                // bounded (cap 25), ALREADY ORDERED: escalations → approvals → parked
    {
      "source": "escalation" | "approval" | "parked",
      "id": "string",
      "title": "string",            // plain-English headline
      "why": "string",              // see § 3.1 — parked `why` is a stringified dict TODAY
      "severity": "critical" | "warning" | "info" | null,
      "action_hint": "string",      // e.g. "approve/reject in Approvals" / "promote/drop in Missions" / "review escalation"
      "age_seconds": 12345          // age at snapshot time (int)
    }
  ]
}
```

Notes from the substrate (`scripts/system_state_log.py`):
- `items` is **already ordered** most-urgent-first (escalations by severity then age → approvals oldest-first → parked oldest-first) and **already capped** at 25. **Render in array order — do NOT re-sort.**
- In production right now `pending_approvals` and `escalations` are `0` (escalation reader is conservative — explicit `for_larry` flag only), so today the list is all `parked`. The view must still handle all three sources (they appear the day the upstream signals exist).
- `total` counts ALL waiting items; `items` is capped — so when `truncated` is true, `total > items.length`.

### 3.1 `why` normalization (REQUIRED — plain-language-first)

⚠ **Verified data quirk:** for **parked** items, `why` is currently a **stringified Python dict** (the team-authored briefing), e.g.:

```
{'what': "A cleanup of one of our work-tracking views that currently lumps 97 finished and unfinished items together…", 'why': "Right now the view is misleading…", 'suggest': "Worth doing, but start small…"}
```

This is single-quoted Python `repr` — **not** valid JSON, and rendering it raw violates the plain-language-first rule. The view MUST normalize it. Add a pure, tested helper:

- **`cleanWhy(why: string): string`**
  - If `why` (trimmed) is a briefing-dict repr (starts with `{` and contains a `'what'` key), extract and return the **`what`** value (the plain-English summary). Handle both single- and double-quoted values and backslash-escaped quotes (Python `repr` picks the quote that avoids the value's quotes; a both-quotes value escapes with `\`).
  - If `why` is a dict-repr but the `what` value can't be extracted, return `""` (render nothing for `why` — the title + hint carry the meaning; never show raw `{'what'…}`).
  - Otherwise return `why` trimmed unchanged (approvals/escalations already carry clean prose).
  - Reference extraction (test cases are the contract, not the regex): `/['"]?what['"]?\s*:\s*(['"])((?:\\.|(?!\1).)*)\1/s` → unescape `\1` and `\\`.

(This is belt-and-suspenders: a sibling agent-core follow-up should make the substrate emit a clean `why` for parked items — `load_parked_items` using `briefing['what']` instead of `str(briefing)` — after which `cleanWhy` becomes a pass-through. Do NOT block this dashboard build on that.)

## 4. Deliverables

### D1 — Types (`lib/types.ts`)
Replace the loose placeholder:
```ts
export interface StateLogWaitingOnLarry {
  parked?: number;
  [key: string]: unknown;
}
```
with the real shape (all fields optional so an older snapshot or a partial test mock still type-checks and the view degrades gracefully; drop the index signature so typos are caught):
```ts
export type WaitingItemSource = "escalation" | "approval" | "parked";
export type WaitingItemSeverity = "critical" | "warning" | "info" | null;

export interface StateLogWaitingItem {
  source: WaitingItemSource;
  id: string;
  title: string;
  why: string;
  severity: WaitingItemSeverity;
  action_hint: string;
  age_seconds: number;
}

export interface StateLogWaitingOnLarry {
  parked?: number;            // KEPT — back-compat count
  pending_approvals?: number;
  escalations?: number;
  total?: number;
  items?: StateLogWaitingItem[];
  truncated?: boolean;
}
```

### D2 — Page (`app/where-we-are/page.tsx`) — extend `WaitingOnYouPanel`
Keep the `PanelShell` wrapper + `data-testid="waiting-on-you-panel"`. Inside:

- **Summary (top):** a count headline reflecting **`total`** (fall back to `parked` when `total` is absent — back-compat). Keep the amber-highlight feel. When `pending_approvals` or `escalations` > 0, show a small breakdown (e.g. `N escalation · N approval · N parked`). When the total is `0` / no items: a calm empty state — "Nothing waiting — you're all caught up." (no amber alarm box).
- **Itemized list:** render `items` **in array order** (do NOT re-sort — the substrate already ordered them). Each item row:
  - **Source badge** — a small uppercase pill: `escalation` → rose, `approval` → amber, `parked` → zinc/slate. (Optional: bump an escalation badge to a stronger rose when `severity === "critical"`.) Include the source text so it's screen-reader legible.
  - **Title** — `item.title`, medium weight.
  - **Why** — `cleanWhy(item.why)` as muted text; render nothing if it normalizes to empty.
  - **Age** — `formatAge(item.age_seconds)` as muted text (e.g. "3d ago", "5h ago", "just now").
  - **Action link** — a `next/link` `<Link>` whose text is `item.action_hint` and whose `href` is `linkFor(item.source)`: `approval`/`escalation` → `/approvals`, `parked` → `/missions`. (Linking the row or a trailing affordance both fine; the link target is what matters.)
- **Truncation note:** when `truncated` is true, a muted footer — e.g. "Showing the first 25 — N more waiting" (N = `total - items.length` when computable) — with the Approvals / Missions links handy.
- **Back-compat:** when `items` is absent/empty but a count exists (an older snapshot), keep the prior count box behavior (don't crash, don't show an empty list).

Add two small **pure, module-scope, tested** helpers alongside the existing `Stat`/`PanelShell`:
- **`formatAge(seconds: number): string`** — bucketed relative age (mirror `formatRelative`'s buckets: <45s "just now"; minutes; hours; days; months; years), always past-tense ("… ago"). Guard non-finite/negative → "just now"/"—".
- **`cleanWhy(why: string): string`** — per § 3.1.
- **`linkFor(source: WaitingItemSource): string`** — `"parked"` → `/missions`, else `/approvals`.

### D3 — Tests (`app/where-we-are/__tests__/page.test.tsx`) — extend the existing suite
Keep all existing tests green. Update `mockResponse`'s `waiting_on_larry` to the new shape with representative items (≥1 escalation, ≥1 approval, ≥1 parked whose `why` is a **dict-repr string**), and add assertions:
- the itemized list renders each item's **title**;
- **source badges** appear (escalation / approval / parked);
- the parked **dict-repr `why` is normalized** — the plain `what` text appears and the raw `{'what'` substring does **NOT**;
- **links resolve** — an approval/escalation item links to `/approvals`, a parked item links to `/missions` (assert `href`);
- the **summary count** reflects `total`;
- **truncation note** shows when `truncated: true`;
- **empty state** ("Nothing waiting" / "caught up") when `total: 0, items: []`;
- **back-compat** — the legacy `{ parked: N }` shape (no `items`) still renders the count without crashing.
Also add focused unit tests for `cleanWhy` (dict-repr → `what`; clean prose → unchanged; unparseable dict-repr → `""`) and `formatAge` (a few buckets). Export the helpers (or test them via the rendered output) — prefer exporting the pure helpers for direct unit tests.

## 5. Acceptance criteria

- [ ] The "Waiting on you" panel renders the itemized `items[]` (source badge + title + plain-English why + relative age), in the substrate's order.
- [ ] Parked `why` dict-reprs are normalized to plain English — no raw `{'what'…}` ever shown.
- [ ] Approval/escalation items link to `/approvals`; parked items link to `/missions`. The Approvals UI is **not** rebuilt.
- [ ] A summary count (driven by `total`, back-compat to `parked`) is kept; empty/zero, loading, and stale states behave like the rest of the page (no crash; `PanelErrorBoundary` still wraps the panel).
- [ ] `StateLogWaitingOnLarry` in `lib/types.ts` matches the live shape (verified against the live endpoint).
- [ ] vitest suite green (`npm test`); existing where-we-are tests still pass.

## 6. Out of scope

- Rebuilding / changing the Approvals tab or Missions funnel (link out only).
- Per-item drill-through detail panes / actions from this view (read-only).
- Health synthesis (the `health` field) — Slice C (held).
- The upstream substrate `why`-cleanup (agent-core `load_parked_items`) — separate follow-up; this build defends with `cleanWhy`.

## 7. Notes

- This extends the Slice-1b page; do not touch the Missions/Projects board (boundary respected — link to it, don't modify it).
- The State Log refreshes ~every 10 min on the droplet; the page's existing 30s poll keeps it current. `age_seconds` is age at snapshot time — `formatAge` renders it as-is (drift vs wall-clock is bounded by the refresh cadence and immaterial against hour/day-scale ages).
