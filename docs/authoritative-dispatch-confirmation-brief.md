# Authoritative dispatch confirmation — make "dispatched" deterministic, not prose

Status: DESIGN brief, pre-drafted 2026-06-03 for an EASY KICKOFF TOMORROW. This is the stronger
structural follow-up to the phantom-dispatch detector (`heal-phantom-dispatch-claim`). Kick off AFTER
that detector lands, and use one observed firing to validate the completion-claim pattern set.

## Why

On 2026-06-03 Beacon DM'd Larry "Approved — `deploy-notifier-ready-logonly` dispatches to Forge now"
but nothing reached Forge (verified across every surface). The detector we shipped CATCHES such a
phantom after the fact (~10 min). This build PREVENTS it: the word "dispatched" should be physically
incapable of reaching Larry unless a real dispatch happened. Same marker-vs-prose disease as the
Mirror auto-merge drift ([[feedback_author_self_approval_merge_gap]]) — detection is the net,
this is the structural fix.

## The seam (audited 2026-06-03 — read before designing)

`scripts/beacon_telegram_bot.py` has TWO approval paths:

1. DETERMINISTIC (≈L588-620): on a strict approve of a PENDING entry, the bot calls
   `approval.dispatch_approved(entry)` → `safe_write_inbox` → `approval.resolve(...)` and confirms to
   Larry TIED to the real dispatch ("approved <id> -> dispatched to <dest>"), or reports
   `DispatchRejected` / `RoutingDenied`. This path is ALREADY authoritative.
2. AGENT-PROSE: when Beacon (the Opus agent) generates a conversational "Approved — X dispatches to
   Forge now", that confirmation is free text, NOT tied to any dispatch.

The deploy-notifier phantom took path 2: no pending entry was ever registered (no marker emitted when
Beacon said "Approve and it goes to Forge"), so there was nothing for path 1 to dispatch — yet the
agent prose-confirmed a dispatch that never happened. ROOT: the authoritative "dispatched" signal can
currently originate from unverified agent prose, and an "approval" can be prose-confirmed without ever
being a registered, dispatchable entry. (`outbox_notifier.py` already has a "queued completion DM"
mechanism on real dispatch — that is the kind of authoritative emitter we want as the single source.)

## Design goal

The word "dispatched" / "approved-and-dispatched" reaches Larry ONLY when a real `safe_write_inbox`
succeeded. Beacon's prose MAY express INTENT ("I'm dispatching X now"); it must never be the
AUTHORITATIVE confirmation, and must never assert a COMPLETED dispatch that didn't go through the
deterministic path.

## Locked principles

1. SINGLE SOURCE OF TRUTH: the deterministic dispatch path (the bot / `outbox_notifier` on a confirmed
   `safe_write_inbox`) is the ONLY emitter of an authoritative "dispatched: <task> is in Forge"
   confirmation. Reuse/extend path-1's confirmation (`beacon_telegram_bot` ≈L596-598) + the notifier's
   existing "queued completion DM". Map ALL dispatch sites so there is exactly ONE authoritative
   emitter, not several.
2. PROSE GUARD: an agent response asserting a COMPLETED dispatch/approval ("dispatched", "approved —
   goes to Forge now", "shipped to Forge") with NO backing pending-entry + `safe_write_inbox` in that
   turn must be intercepted by the bot (kick-back/correct, like the malformed-marker handling at
   `beacon_telegram_bot` ≈L738) — NOT forwarded to Larry as-is. Beacon is re-prompted to emit the
   proper marker.
3. KEEP BEACON CONVERSATIONAL: she MAY narrate intent; the deterministic confirmation is appended by
   the system. Gate COMPLETION claims, not intent — do not flatten her voice.
4. BELT-AND-SUSPENDERS: complements (does not replace) `heal-phantom-dispatch-claim`. With both, a
   phantom is prevented at emission AND caught if it slips.

## Open design questions (resolve in the design / preflight pass)

- Precise discrimination of a "completion claim" vs an "intent" statement in an agent response — the
  conservative pattern set and where it hooks in the bot's response-forward path (avoid muzzling
  normal talk).
- For a chat approval with NO pending entry (the exact phantom): should the bot kick back and require
  the marker first, or auto-register from the agent's intent? (Likely require the marker — never
  fabricate an entry.)
- Best home for the authoritative confirmation: the bot vs `outbox_notifier`'s completion-DM path —
  pick ONE so there is a single emitter across all dispatch sites.
- UX: exactly what Larry sees — Beacon's intent line + a separate system "dispatched: <task> (Forge)"
  line.

## Scope boundaries

- The detection-only phantom net (`heal-phantom-dispatch-claim`) is separate and already dispatched.
- Do NOT change marker schemas or the Forge pipeline — this is about the CONFIRMATION emitter + a
  prose guard.
- Likely files: `scripts/beacon_telegram_bot.py`, `scripts/outbox_notifier.py`,
  `scripts/beacon_approval_handler.py`, a claim-pattern config, tests. Confirm in preflight.

## Kickoff (tomorrow)

A Forge preflight envelope is pre-staged at
`~/agents/inboxes/forge/.staged/harden-authoritative-dispatch-confirmation.json` — kickoff is a single
`mv` out of `.staged/` into the forge inbox. Do it AFTER `heal-phantom-dispatch-claim` lands.
