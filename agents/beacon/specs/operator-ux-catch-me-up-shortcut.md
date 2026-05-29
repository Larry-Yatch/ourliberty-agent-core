# Spec: Beacon catch-me-up Telegram shortcut

**Status:** Draft awaiting design pass
**Author:** Forge (stub from operator-UX backlog, 2026-05-28)
**Parent registry entry:** `agents/beacon/missions.json#operator-ux-catch-me-up-shortcut`

---

## 1. Purpose

Single Telegram command (e.g. `catch me up` / `status`) that returns structured since-last-checkpoint summary: merged PRs + open PRs + pending approvals + active alerts + sequence state. Eliminates 5+ refetch round-trips per check-in.

Today every operator check-in cycles through the same five questions ("any merges since I last looked?", "anything waiting on me?", "what alerts are live?", "where are the sequences?", "anything stuck?"). Beacon answers each via PLAN_SYNTHESIS_DISCIPLINE refetches — correct but slow and chat-clutter-heavy.

---

## 2. Sketch

- Recognize `catch me up`, `status`, `what's happening`, `where are we`, and similar variants (case-insensitive, near-match) in `scripts/beacon_telegram_bot.py`'s message handler.
- New module `scripts/catch_me_up.py` synthesizes the structured summary from: `gh pr list` (recent merges + open), `~/agents/state/beacon-pending-approvals.json`, `larry_alerts` ledger (last 24h, NOW+SOON tiers only), `~/agents/blackboard/build-sequences/*.json` (active + paused).
- Output is a single Telegram message with five short sections, each capped at 3 rows. Overflow links to the dashboard's relevant tab.
- "Last-checkpoint" defaults to the last `catch me up` invocation (per-operator, persisted to a tiny state file). First-time invocation defaults to the trailing 24h.
- Same shortcut is auto-invoked by Pulse Check IX (sibling spec) when an operator-friction signal fires, to baseline the operator before proposing the friction fix.

---

## 3. Open questions

- Per-section row cap (3 vs 5) is a guess — needs Larry usage to calibrate.
- Should the message include a *"since X ago"* timestamp header, or is the checkpoint implicit?
- Does the shortcut need a `since: <iso>` modifier for ad-hoc lookback windows, or is checkpoint-based good enough?
- Does invoking the shortcut update the checkpoint immediately, or only after Larry sends a follow-up acknowledgment?

---

## 4. Acceptance (rough)

- Typing `catch me up` returns the structured five-section summary in under 5 seconds end-to-end.
- The summary references only refetched state (PLAN_SYNTHESIS_DISCIPLINE-compliant); no cached snapshot fallback.
- The checkpoint advances on invocation so a follow-up `catch me up` 5 min later shows only the delta.

---

## 5. Estimated cost + sizing

Synthesizer module + bot handler + checkpoint state-file: ~$8–10. One PR. Mirror revisions expected 0–1. Sizing: small-to-medium; the design work is the section structure + checkpoint semantics.
