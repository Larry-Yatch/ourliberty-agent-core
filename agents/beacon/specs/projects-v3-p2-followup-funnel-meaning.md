# projects-v3-p2-followup — funnel meaning layer + reliable refresh

**Type:** follow-up patch to P2 (universal action card on the funnel).
**Why this exists:** a browser verification of the live board (2026-06-17) found two
gaps in what P2 shipped. The data is correct; the display and the refresh path are not.
This patch closes both. Small, low-risk, no data migration.

---

## 0. Desired End State (plain language)

When you open the Missions/Funnel board:

1. **Every funnel card reads like English** — including the *Proposed* lane. Today the
   49 Proposed cards show raw machine text ("Auto-proposed from orphan task
   `…`. Last activity 2026-06-12T…"). They should show the same plain-English
   meaning layer the Parked cards already show (what / why / suggested), with a risk
   badge — because that briefing **already exists in the data** for every live
   proposed card; it just isn't being displayed.
2. **The funnel keeps itself fresh without erroring.** Today the board's
   background data refresh intermittently fails (503), so the funnel only reliably
   updates on a full page reload. After this patch the live refresh succeeds every time.

Done means: open the board, the Proposed lane is plain English with risk badges, and
the funnel data refreshes in the background with no 503s in the network tab.

---

## 1. Why now

P2 is otherwise live and the completion engine proved itself. These two gaps were
invisible to a data-only check (the briefings exist in `missions.json` and in the
`/api/missions/derived` payload) and only showed up when the board was opened in a
browser. They defeat the core promise of P2 for the busiest lane, so we close them
while the context is fresh — before P3 builds the pipeline on top of this surface.

---

## 2. Scope

IN:
- Proposed lane (dashboard) renders the Narrator-authored meaning layer.
- Funnel client refresh routed through the standard same-origin proxy client.
- Short TTL cache on the `/api/missions/derived` computation (agent-core).

OUT:
- No change to how/where the Narrator authors briefings (data is already correct).
- No data migration (all 49 live proposed missions already carry a valid
  `briefing {what,why,suggest}` + `risk` + `risk_note` + `recommended_action`).
- No change to the Proposed lane's *actions* — Accept / Dismiss stay (that is the
  correct "admit into / stop re-proposing" intake gesture for orphan-derived cards;
  the universal Delegate/Snooze/Drop/Talk set is for the primary mission-backed lanes).
- Not adding uvicorn workers in this patch (a systemd-unit change with multi-process
  implications; the TTL cache is the lower-risk lever and is expected to be sufficient
  for a single-operator board — revisit only if 503s persist after the cache lands).

---

## 3. Constraints & reuse (consulted shelf + graph 2026-06-17)

- **Contract A reuse:** the meaning-layer render block already exists in-repo at
  `app/missions/components/FunnelCard.tsx` (and `ParkedCard.tsx`) — `item.briefing.{what,why,suggest}`,
  the `risk` badge, and `item.risk_note`. Reuse that rendering in the Proposed lane;
  do **not** author a new briefing component. Absence-renders-neutral is already the
  established pattern (no briefing → a quiet "still being written up" state, never raw text).
- **Contract B reuse:** the dashboard already has `dashboard-api-client` — typed SWR
  hooks that poll **same-origin `/api/proxy/*`** routes. The funnel refresh must use
  that same pattern. The observed bug: the client was calling `/api/missions/derived`
  (same-origin, 404 — missing the `/proxy/` segment) and falling back to the direct
  API host `api.ourliberty.dev` (503 / cross-origin), instead of the proxied route.
- **Derive cache:** net-new (shelf verdict NONE) — a small in-process TTL cache keyed
  on nothing (whole-payload) in `scripts/dashboard_api.py`. ~10s TTL. Single committer
  / no shared-state concerns (read-only materialization).
- **Single-source data contract** (do not break): `/api/missions/derived` already
  returns `parked[]` (briefing inline), `missions[]` (proposed carry `briefing` when
  live), and `funnel{primary,secondary}` (thin pointers). The Proposed lane should read
  the briefing from the proposed `missions[]` entries (where it lives), exactly as the
  Parked lane reads `parked[]`.

---

## 4. Risks

- **Low.** Frontend is additive (show briefing when present; raw `brief` stays as the
  fallback so nothing regresses for cards without a briefing).
- The TTL cache must invalidate within a few seconds so the funnel doesn't feel stale;
  10s is the ceiling. Cache must be safe under the existing single worker (it is —
  per-process dict + timestamp).
- **Activation:** the agent-core derive-cache change (`dashboard_api.py`) only goes
  live after `sudo systemctl restart ourliberty-dashboard-api.service` on the droplet
  (long-lived process). That restart is a separate, human-approved step after merge.

---

## 5. Done-gate (checkable form of §0)

- [ ] Open the live board: Proposed lane cards show what/why/suggest + a risk badge
      (not the raw "Auto-proposed from orphan task …" line) for the live proposed set.
- [ ] A proposed card with no briefing still renders neutrally (never raw machine text
      as the primary content) — absence-renders-neutral preserved.
- [ ] Accept / Dismiss still present and functional on Proposed cards.
- [ ] Network tab on board load + over a refresh cycle shows the funnel data fetch
      succeeding (200) via the same-origin proxy route — no 404 on `/api/missions/derived`,
      no 503 on the API host.
- [ ] `/api/missions/derived` served from the TTL cache on repeat calls within the
      window (verify latency drops to ~0 on a warm second call).

---

## 6. Breakdown (steps)

1. **p2fix-proposed-meaning** — *(dashboard repo)* Proposed lane (`ProposedLane.tsx`)
   renders `briefing.{what,why,suggest}` + risk badge + `risk_note` when present,
   reusing the `FunnelCard`/`ParkedCard` briefing block; raw `brief` becomes the
   neutral fallback. Keep Accept / Dismiss. Add/extend tests
   (`__tests__/ProposedLane.test.tsx`) for: briefing present → rendered; absent →
   neutral; actions intact. *(no deps)*

2. **p2fix-funnel-refresh** — *(dashboard repo)* Route the funnel/derived client-side
   fetch through the standard same-origin `dashboard-api-client` SWR proxy pattern
   (`/api/proxy/...`), eliminating the 404-then-direct-host-503 fallback. Verify no
   component fetches `api.ourliberty.dev` directly for funnel data. *(no deps)*

3. **p2fix-derive-cache** — *(agent-core repo)* Add a short (~10s) in-process TTL cache
   to the `/api/missions/derived` handler in `scripts/dashboard_api.py` so concurrent
   page-load calls reuse one computation instead of recomputing the ~1.5s payload per
   request (the single-worker pile-up that produced ~6s latencies under load). Unit
   test the cache hit/expiry. *(no deps)* — **activation: dashboard-api restart after merge.**

All three steps are independent (no serialization). Closeout note should confirm the
Done-gate was checked **in the browser**, not just in the data (the lesson from how
these gaps were missed the first time).
