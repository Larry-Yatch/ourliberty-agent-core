# Spec: Tiered Telegram Alert Taxonomy (NOW / SOON / FYI)

**Status:** Draft awaiting design pass
**Author:** Forge (stub from operator-UX backlog, 2026-05-28)
**Parent registry entry:** `agents/beacon/missions.json#operator-ux-alert-taxonomy`

---

## 1. Purpose

Add `tier` field to `alert-translations.json` so each subject maps to NOW (blocks chain), SOON (within hours), FYI (informational). DM rendering surfaces tier visually. Reduces operator fatigue from undifferentiated alerts.

Today every Telegram alert renders identically — a healer warning is visually indistinguishable from a chain-halting credential expiry. The operator triages by reading every alert end-to-end, which is the wrong shape once alert volume scales beyond ~5/day.

---

## 2. Sketch

- New required field `tier: "NOW" | "SOON" | "FYI"` on every entry in `config/alert-translations.json`. Migration backfills existing subjects per a one-pass classification (Forge proposes; Larry reviews diff).
- `larry_alerts.append_notification(...)` reads the tier from the translation registry and stamps it onto the ledger row alongside subject + body.
- DM rendering (`scripts/beacon_telegram_bot.py` notification formatter) prepends a tier glyph + bold tier label: `🔴 NOW · <subject>` / `🟡 SOON · <subject>` / `⚪ FYI · <subject>`.
- Downstream consumers (Action Queue 5.5 NOW-severity row, future Pulse Check IX signals) read the tier field as the canonical severity gradient.
- Unknown subjects (not yet in translations) default to `SOON` with a Pulse-surfaced warning so the gap gets registered.

---

## 3. Open questions

- Does the tier glyph belong before or after the existing healer/source prefix in the DM string?
- For multi-subject batch alerts (e.g. healer rollups), is the displayed tier the max of constituent tiers, or per-row?
- Should `FYI` alerts route to a separate Telegram thread / chat to keep the main thread NOW+SOON only? (Probably yes, but needs Larry confirmation.)
- Tier classification for currently-unfielded alerts: does Forge classify in the same PR, or does it land as a follow-up dispatch?

---

## 4. Acceptance (rough)

- Every entry in `alert-translations.json` carries a tier field; schema validator rejects entries missing it.
- A test alert dispatched at each tier renders the correct glyph + label in the Telegram DM.
- The Action Queue panel can filter NOW-severity rows from the ledger using only the tier field (no severity-string heuristics).

---

## 5. Estimated cost + sizing

Config-shape change plus DM formatter touch: ~$6–8. One PR. Mirror revisions expected 0–1. Sizing: small; the design work is in the classification pass, not the code.
