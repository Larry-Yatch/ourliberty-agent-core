# Open Questions

Decisions needed from Larry. Each entry: the question, context, options, and who owns it. Updated by Beacon (or any agent that needs a decision) as questions arise. When a question is answered, move it to the Archive section with the resolution + date.

---

## Settings.json: allow Beacon to write to `~/agents/memory/beacon/`

- **Context:** Beacon's persistent memory dir is outside the session's allowed paths in `.claude/settings.json`. Cross-session memory writes (Ledger roadmap entry, lessons learned, calibration notes) are currently impossible. Recovery is in-conversation only — lost on session end.
- **Options:**
  - (a) Loosen Beacon's `.claude/settings.json` to allow writes to `~/agents/memory/beacon/`. One-time fix, unblocks every future session.
  - (b) Use repo-resident roadmap files (this `docs/roadmap.md` + `docs/open-questions.md`) as the durable layer; accept that wisdom-shape memory is lost between sessions.
  - (c) Both — repo files for shared state visible to all agents + Larry; persistent memory for Beacon-private wisdom (calibration, lessons from her own mistakes).
- **Recommendation:** (c). Repo files handle shared state; persistent memory handles Beacon-private wisdom.
- **Decision owner:** Larry

## Ledger-Pulse pipeline shape

- **Context:** Larry's specialization model (2026-05-15): Ledger captures/reports/analyzes cost data, Pulse adds technical interpretation + proposed fixes. Two ways to wire the handoff between them.
- **Options:**
  - (a) Pipeline. Ledger emits findings to a shared location (Pulse's inbox or a blackboard file) Monday morning. Pulse reads them, layers engineering interpretation, emits the consolidated digest to Larry.
  - (b) Independent. Both read shared raw artifacts (cost-capture JSONL, journals, etc.) directly; Ledger publishes cost framing, Pulse publishes engineering framing; Larry sees both.
- **Recommendation:** (a) — matches Larry's "Pulse looks at the technical implications of what HE is finding" framing; cleaner handoff; failure modes easier to reason about (Ledger-up-Pulse-down is a distinct state from both-down).
- **Decision owner:** Larry

## Build order: Ledger first vs sketch both contracts together

- **Context:** Pulse Check I's spec depends on what Ledger emits. Two ways to sequence the design work.
- **Options:**
  - (a) Ledger end-to-end first — full spec, dispatched, shipped. Then Pulse Check I spec uses real Ledger output.
  - (b) Sketch both contracts together — design the Ledger→Pulse handoff from both sides simultaneously, then ship in dispatch order (Ledger first, Pulse second).
- **Recommendation:** (b) — designing the handoff from both sides simultaneously reduces risk that Ledger's output shape turns out wrong from Pulse's perspective. Smaller round trip than ship-and-rework.
- **Decision owner:** Larry

---

**Convention:** when a question is answered, move it below an Archive section (to be added when the first answer lands) with the decision + date. When new questions arise, add them to the top.
