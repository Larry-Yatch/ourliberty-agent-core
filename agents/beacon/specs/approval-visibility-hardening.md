# Spec: Approval Visibility — a decision needing Larry never goes silent

**Status:** ready for build
**Author:** Beacon
**Date:** 2026-06-11
**Origin:** the PR #457 post-mortem — a Mirror escalation that needed Larry's direction reached the Approvals tab silently (no DM) and was only found by chance.

## 1. Invariant (locked)

Any approval-class decision that needs Larry MUST reach him as an **actionable, DM-delivered** approval on the Approvals tab. It must never be:
- (A) an escalation that sits unactioned with no surfacing,
- (B) a bare `larry-alert` that never reaches the Approvals tab, or
- (C) a tab item with no delivery path (`chat_id: None`) so no DM ever fires.

"On the tab" is not enough — if it needs Larry, it must ping Larry.

## 2. The three gaps (worked example: PR #457, 2026-06-11)

- **Gap A — escalate→Beacon unactioned.** Mirror emitted `REVIEW_ESCALATE` on PR #457 at 08:46Z. The escalate notify landed in Beacon's inbox and sat **unhandled for hours** — no Beacon session actioned it (revise or surface to Larry).
- **Gap B — direction-ask emitted as a bare alert.** ~2h35m later a Pulse cycle flagged it as a plain `larry-alert` (`subject=pr-457-mirror-escalation-3h-flag`), **not** an `approval_request` marker. Per the standing doctrine ("Direction-asks are APPROVAL_REQUESTs, not larry-alerts"), a direction-ask raised as a bare alert never reaches the Approvals tab natively.
- **Gap C — silent promotion (the direct cause).** `heal_unregistered_approval` (the reconciliation backstop) promoted it onto the tab at 11:15Z — but with **`chat_id: None`**. The alert sweep had no thread to DM, so the item sat on the dashboard **silently**. Larry only saw it by looking.

## 3. Fixes

### F-C — promotion must always be DM-deliverable (primary, load-bearing)
`scripts/heal_unregistered_approval.py` (and any approval-promotion path) MUST attach a deliverable `chat_id` (default the operator chat, `7998341473`) when promoting an item to the Approvals tab. A promoted approval with `chat_id: None` must be **impossible** — default it; if the default is somehow unavailable, log loud and still surface, never silently drop. **Enforcement:** a regression test that a promoted approval always carries a non-null `chat_id`, and that the alert sweep delivers a DM for it (replays the #457 shape → a DM fires).

### F-B — direction-asks emit as `approval_request` markers, not bare alerts
The emitter that produced `pr-457-mirror-escalation-3h-flag` (the Pulse-cycle / escalation re-flagging path) MUST emit an `approval_request` marker — reaching the tab AND DMing natively — instead of a bare `larry-alert`. This makes the doctrine enforced at the emission site, so the backstop (F-C) is a true backstop, not the primary delivery. **Enforcement:** the emission site emits an approval_request for direction-class escalations; `heal_unregistered_approval` remains the reconciliation net (now DM-safe via F-C).

### F-A — an unactioned escalate surfaces (not stays invisible)
A Mirror `REVIEW_ESCALATE` that sits unactioned past a threshold MUST surface to Larry as an actionable approval through the F-C-fixed path, so an un-actioned escalate can never stay silent. (Auto-*actioning* the escalate in Beacon is the chain-context-durability recover-or-route direction and is out of scope here; this spec guarantees the **surfacing/DM** safety net.) **Enforcement:** the existing 3h-escalation flag routes through the approval_request path (F-B) and/or the DM-safe promotion (F-C).

## 4. Build plan

Single focused effort, `target_repo: ourliberty-agent-core`:
- **F-C** — `scripts/heal_unregistered_approval.py`: default/guarantee `chat_id` on promotion + test. (Load-bearing; do first.)
- **F-B** — the Pulse-cycle / escalation emitter that raised `pr-457-mirror-escalation-3h-flag`: emit an `approval_request` marker for direction-class escalations instead of a bare alert.
- F-A is satisfied by F-B + F-C (an unactioned escalate either emits as an approval natively or is promoted DM-safely).

May ship as one PR (F-C + F-B together) or a 2-step linear sequence (F-C then F-B). Author decides at sequence-synthesis time.

## 5. Success criteria

- A promoted approval always carries a non-null `chat_id` and DMs Larry (no silent tab items).
- A direction-class escalation is emitted as an `approval_request` (reaches the tab + DMs natively).
- Replaying the #457 escalation produces a Telegram DM to Larry, not a silent tab entry.
- The `heal_unregistered_approval` backstop still works, but is now DM-safe.

## 6. Out of scope
- Auto-actioning a Mirror escalate inside Beacon (chain-context-durability recover-or-route territory).
- Any change to the Approvals-tab UI; this is delivery/emission only.
