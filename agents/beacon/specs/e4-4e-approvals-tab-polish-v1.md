# Spec: E4.4e — Approvals Tab Polish v1 (Usable Inbox)

**Status:** Draft (awaiting Larry approval — polish sub-spec of E4.4e)
**Author:** Claude-as-Forge (written 2026-05-27 morning, based on first-use feedback session)
**Approver:** Larry (pending)
**Phase:** E4.4e polish, post-launch
**Parent spec:** [agents/beacon/specs/e4-4e-approvals-tab.md](e4-4e-approvals-tab.md)
**Predecessor:** PR-D (dashboard #12) shipped 2026-05-27 ~04:35 UTC; tab live at `dashboard.ourliberty.dev/approvals`.

---

## 1. Problem statement (what triggered this)

PR-D shipped the Approvals tab interactive surface — Google login, three buckets, per-source affordances, SWR polling all worked end-to-end. Larry's first-use feedback after the env-var redeploy was unambiguous: *"It's pretty rough."*

Observed problems on the live surface:

- Each card dumps the raw `prompt` payload as literal text. The payloads are markdown-formatted Forge briefs (1000–2000 words with `**bold**`, `### headers`, fenced code blocks, bullet/numbered lists) rendered as one continuous paragraph. A single card consumes 3–4 screens of scrolling.
- The card "title" is the task_id slug (`claude-quota-fixes-v2-tier2-bugs-plus-alert-noise-plus-new-healers`) — hard to parse, doesn't summarize.
- No collapse affordance — every card is expanded by default. The pending list is unscannable.
- "PENDING (1000)" is the PostgREST default cap, not the actual count. Real total is higher and the UI loads all rows at once with no pagination.
- Both Beacon approval gates AND healer FYI alerts show the same `beacon`+`info` badges — visually indistinguishable. The most important "blocks the chain" items have no visual priority.
- No bucket separation visible above the Pending list — Acknowledged today and Cleared exist in spec § 8.2 but aren't surfaced.
- No filter / search — with 1000+ items, can't reduce to relevant subsets.
- Approve / Reject buttons sit at the bottom of each massive card; user scrolls past them to reach them, loses context of what they're acting on.

Net effect: the tab is a log dump, not an inbox.

---

## 2. Design intent — how Larry actually uses this tab

Five workflow states the polish must support:

| State | Question | Time budget | Polish needs |
|---|---|---|---|
| **Glance** | "What's blocking the chain right now?" | <5 sec | Top-of-page count + the pinned blocking items above everything else |
| **Triage** | "I have 10 min, what can I clear?" | minutes | Scannable collapsed list; FYI alerts visually separable from real decisions; filter chips reduce noise |
| **Decide** | "I'm actually deciding on this approval" | minutes | Markdown-rendered context; action affordances stay visible while reading; dual-envelope preview for approve/reject |
| **Catch-up** | "What happened while I was away?" | minutes | Acknowledged today bucket shows recent action history |
| **Audit** | "Did I approve X yesterday?" | seconds | Cleared bucket searchable |

The success metric for v1 polish: Larry can clear 80% of the current pending queue in one 15-minute sitting without feeling overloaded.

---

## 3. Four locked decisions

| # | Decision | Locked value | Rationale |
|---|---|---|---|
| A | Card collapse model | **Default collapsed; one-line summary row; click anywhere on row to expand inline** | Wall-of-text is the highest-leverage problem. Collapsed-by-default makes the list scannable; click-to-expand keeps deep context one click away. |
| B | Markdown rendering | **Render `react-markdown` (or equivalent) for prompt + message payloads; no raw HTML; dark-theme styling** | The payloads ARE markdown; rendering them as such is a 10x readability improvement for zero design cost. |
| C | Bucket model + sort | **Three buckets per parent spec § 8.2 (Pending / Acknowledged today / Cleared) with explicit headers + real counts; pinned-blocking items above the Pending bucket** | Bucket hierarchy + pinning together solve the "what's blocking right now" question without forcing the user to scroll. |
| D | Visual source distinction | **Color-coded accent stripe per source type; severity dot for alerts; explicit "Blocks chain" pill on approval_request + clarify_request** | The current uniform `info` treatment is the second-most-painful problem. Source-distinct color + an explicit pill makes the priority obvious without reading text. |

---

## 4. Success criteria

A working polish ships when ALL of the following are true:

- A 2000-word Forge brief in an approval_request card is readable without horizontal squinting — headers stand out, code blocks have monospace + bg, bullet lists nest correctly.
- The pending list is scannable: 50 items visible in one scroll without expanding any.
- Larry can identify which pending items block the chain (approval_request + clarify_request) within 2 seconds of page load, separate from FYI alerts.
- True total count visible (not capped at 1000); virtual scroll keeps the page responsive at 10k+ items.
- Filter chips reduce 1000+ items to a focused subset in one click.
- Acknowledged today bucket surfaces the action history of the current day.
- Cleared bucket is searchable across the last 30 days.

---

## 5. Detailed requirements

### 5.1 Markdown rendering

Install `react-markdown` + `remark-gfm` (GitHub-flavored markdown for tables / strikethrough / task lists). Configure with:

- `disallowedElements: ['script', 'iframe', 'style']` and `unwrapDisallowed: true` (XSS guard — payloads originate from Forge but the principle is no-raw-HTML at the renderer)
- Custom component renderers for code blocks (monospace, bg color, horizontal scroll on overflow)
- Inline code (`monospace + subtle bg`)
- Headings (h1/h2/h3 with reduced sizes matching the card scale — not page-scale)
- Links open in new tab with `rel="noopener noreferrer"`
- Lists with proper nesting + indent

### 5.2 Card collapse + headline

Collapsed card layout (target ≤72px height):

```
[accent-stripe] [source-badge] [headline (truncated)]        [age]  [▸]
                [agent · task_id (small mono)]                       [actions on hover]
```

Expanded card adds the full body below (markdown-rendered) and a sticky action bar at card bottom.

Headline extraction helper in `lib/approval-queries.ts` — try in order:

1. First markdown H1 (`# Headline`) — strip the `#`
2. First markdown bold line (`**Headline**`) — strip the `**`
3. First sentence of the prompt (split on `. `), trimmed to 100 chars + ellipsis if longer
4. Fallback: the task_id slug humanized (`hyphens → spaces`, title case)

### 5.3 Buckets + counts + sort

Three vertically-stacked sections, each with a sticky header showing the bucket name + real count + collapse triangle:

- **Pending (X)** — expanded by default. Sort per parent spec § 8.2:
  - Blocking items first: approval_request, then clarify_request
  - Then escalation/alert by severity (critical > warning > info)
  - Then recency within each tier
- **Acknowledged today (X)** — collapsed by default. Sort by `read_at DESC`. "Today" = browser-local midnight boundary.
- **Cleared (X)** — collapsed by default. Paginated 50/page. Includes a search box (search payload text + task_id + agent name).

Real counts via Supabase `count: 'exact'` parameter on each bucket query — not the PostgREST default cap.

### 5.4 Pinned blocking strip

Above the Pending bucket, a compact horizontal strip:

```
[N tasks blocking the chain]  [chip: approval_request × M]  [chip: clarify_request × K]
```

Clicking a chip filters Pending to that source type. Strip hides if N = 0.

### 5.5 Filter chips + search

Above Pending bucket (below the pinned strip): horizontal chip row:

```
[All (X)] [Approvals (M)] [Clarifications (K)] [Escalations (P)] [Alerts (Q)]   [search box →]
```

Counts on chips update with the filtered total. Active chip has solid bg; inactive chips are ghost-style.

Search box performs client-side filter on currently-loaded Pending items (no backend round-trip for typing). Triggers on input with debounce 200ms.

### 5.6 Source-specific styling + affordances

| Source | Accent color | Pill | Affordances |
|---|---|---|---|
| `approval_request` | amber (chain-blocking) | "Blocks chain" | Approve / Reject + Comment field; expanded body shows proposing_agent → target_agent + envelope summary preview |
| `clarify_request` | orange | "Question" | Reply text field (multiline) + Submit; no Approve/Reject — clarifies aren't binary |
| `escalation` (`needs_response=true`) | red dot, amber accent | "Needs response" | Mark done + Approve/Reject |
| `escalation` (other) | red dot, gray accent | none | Mark done |
| `larry_alert` critical | red dot, gray accent | none | Mark done |
| `larry_alert` warning | yellow dot, gray accent | none | Mark done |
| `larry_alert` info | gray dot, gray accent | none | Mark done |
| `sentinel_alert` | red dot, gray accent | none | Mark done |

Severity normalization (parent spec § 11 open question 2): map at render time only. `red → critical, yellow → warning, green → info`. Don't mutate database rows. Document mapping in PR description.

### 5.7 Task grouping (inside Pending only)

If 2+ events in the Pending bucket share a `task_id`, render as one card with a "+N more" badge in the headline area. Expanding the card shows all sub-events as a vertical list. Each sub-event keeps its own action affordances; the parent card also has a "Mark all done" affordance for FYI-only sub-event sets.

Grouping is presentation-only — the underlying chain_events rows are independent.

### 5.8 Virtual scroll

For Pending + Cleared buckets, use `react-window` (or `react-virtuoso`) for virtualized rendering. Acknowledged today is typically small (<100 items) and doesn't need virtualization.

Load the first 200 Pending rows on initial fetch; load more on scroll-to-bottom (infinite scroll within bucket).

### 5.9 Sticky action bar (expanded cards)

When a card is expanded and the user scrolls within the body, the action affordance row (Approve/Reject buttons + comment field, or reply box + Submit) sticks to the bottom of the card so the user doesn't lose action context while reading.

### 5.10 Empty / loading states

- Empty Pending: "All clear — no pending approvals" with a green check icon and a subtle "last refreshed N sec ago" timestamp.
- Loading first paint: skeleton rows (3–5 placeholder cards) instead of blank.
- Pending API error: error card at top of bucket with "Retry" affordance + the existing toast.

---

## 6. Out of scope (explicit — deferred to a v2 polish)

- Keyboard shortcuts (J/K to navigate cards, A/R/M for approve/reject/mark-done, `/` for search focus)
- Bulk select + mark-all-done for FYI types
- Mobile responsive layout (V1 polish targets desktop only)
- New-item slide-in animation
- Browser push notifications when new approval_request lands
- "Snooze for X hours" affordance
- Reply threading on comments

---

## 7. Files in scope (dashboard repo)

- `app/approvals/page.tsx` — rework to host the new bucket layout + filter strip
- `app/approvals/components/PendingCard.tsx` — collapse + headline + accent stripe
- `app/approvals/components/AcknowledgedCard.tsx` — match new collapsed style
- `app/approvals/components/ApprovalActionBar.tsx` — sticky behavior + envelope preview
- `app/approvals/components/ClarifyReplyBox.tsx` — orange accent + larger question font
- `app/approvals/components/FilterChips.tsx` — NEW
- `app/approvals/components/PinnedBlockingStrip.tsx` — NEW
- `app/approvals/components/BucketHeader.tsx` — NEW (collapse triangle + count + name)
- `app/approvals/components/MarkdownBody.tsx` — NEW (react-markdown wrapper with theme)
- `app/approvals/components/SkeletonCard.tsx` — NEW (loading state)
- `lib/approval-queries.ts` — add `extractHeadline()`, `getBucketCounts()` (with `count: 'exact'`), `getPendingPaged()`
- `lib/types.ts` — add `HeadlineExtractResult` if helpful
- `package.json` — add `react-markdown`, `remark-gfm`, `react-window` (or `react-virtuoso`)

No droplet changes; no Supabase migrations.

---

## 8. Test plan

Vitest extensions:

- `extractHeadline`: H1 case, bold-line case, sentence-split case, all-caps case, truncation at 100 chars, fallback to task_id slug humanization
- `MarkdownBody`: renders heading + code + list + link correctly; strips `<script>` + `<iframe>`; opens links in new tab
- `BucketHeader`: collapse toggle works; count updates on prop change
- `FilterChips`: clicking a chip emits the correct filter event; counts update
- `PinnedBlockingStrip`: hides when no blocking items; renders correct chips otherwise
- Task grouping helper: 2+ events same task_id collapse; events with unique task_ids don't
- Sort comparator preserves the parent spec § 8.2 order under all source-mix permutations
- Existing list / action Route Handler tests must continue to pass (regression guard)

Manual acceptance per § 4 success criteria.

---

## 9. Cost estimate

Polish PR best-guess Forge + Mirror chain spend: $8–12. Larger than today's average (~$3) because the surface adds 5+ new components + introduces react-markdown + virtualization, plus the headline extraction helper has real test surface. Mirror revision rounds expected: 0–1 (clear scope, no architectural ambiguity).

---

## End of spec
