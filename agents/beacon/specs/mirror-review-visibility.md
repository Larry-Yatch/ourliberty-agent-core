# Spec: Mirror Review Visibility — surface findings + route human-needed escalations for session-less PRs

**Status:** Refined by Beacon 2026-06-24 — ready to sequence (build held until orchestrator-terminal-signal-hardening-001 lands; see §9).
**Author:** Claude Code (web session, 2026-06-24), drafted with Larry; refined by Beacon 2026-06-24.
**Approver:** Larry
**Parent:** chain-context-durability.md (the context-drop / human-terminal dead-end class; M1–M4 shipped)
**Predecessor / template:** forge-cold-start-revision (the mechanical no-session re-dispatch `_dispatch_revision_to_forge` + `no_session_ledger` obligation) + Chain Context Durability M4 (recover-then-alert). This spec extends that work; it does not re-do it.
**Related surfaces:** e4-4g-operator-action-queue.md (the dashboard action panel — still Draft; a soft dependency, see §9), the Approvals tab (fed by approval_request events), the Telegram doorbell (larry_alerts).

> **Beacon refinements (2026-06-24), folded in:**
> 1. Re-anchored to current code: the no-session self-heal is now the MECHANICAL `_dispatch_revision_to_forge` + `no_session_ledger` (forge-cold-start-revision). The earlier `_route_no_session_revision_to_beacon` function is REMOVED from outbox_notifier.py — references updated throughout (§1, §3, §5).
> 2. Locked the Contract B classifier signal: classify on `marker_type` + session/ledger state (signals the notifier already has), not on finding semantics (which it cannot read). See §5.
> 3. Added §9 sequencing note: hold kickoff until orchestrator-terminal-signal-hardening-001 lands (shared edits to outbox_notifier.py + heal_pipeline_stall.py).

---

## 1. Purpose

When Mirror reviews a PR and wants changes, its verdict is delivered as a bare `mirror-review` GitHub commit status — a red ❌ with a one-line description (e.g. REVIEW_REVISION) and no findings body. That's fine on the happy path: Mirror hands its findings to the live Forge build session, which fixes them automatically and nobody needs to look.

It breaks for session-less PRs — a PR with no live Forge session to receive the findings. Two on-ramps produce these:
- **Off-chain authoring:** a doc/spec PR written directly in a human or Claude session and marked ready in GitHub (e.g. #653) — it never entered the Beacon→Forge→Mirror chain, so there was never a Forge session.
- **Reaped session:** a chain PR whose forge_build_session_id was dropped by a recovery/healer path (the "PR #412 class").

The forge-cold-start-revision work already self-heals the *reaped-session* case for chain PRs: on a clean REVIEW_REVISION with no `forge_build_session_id`, `_dispatch_revision_to_forge` (outbox_notifier.py) mechanically re-dispatches a fresh Forge run onto the existing branch and opens a `no_session_ledger` obligation. But two gaps remain, and #653 hit both:

1. **Findings are invisible.** Even when self-heal runs, Mirror's actual "what to change" lives only in the red status + (sometimes) Beacon's inbox. Nothing durable is on the PR; an off-chain PR may produce no chain envelope at all, so the findings are recoverable only by Beacon manually digging in.
2. **Human-needed cases reach no one.** When the auto-fix can't resolve it (recovery failed, or the revision is a scope/values decision), there is no signal on the surfaces Larry actually uses. On #653, nothing reached Larry — he learned of it only because a Claude session happened to be watching. *(Larry does not look at PRs.)*

This spec closes both: findings become visible for the machines + audit; and the subset that genuinely needs Larry is routed to the surfaces he already watches — never to a PR comment he'll never read.

**Done-gate:** a session-less PR that Mirror wants revised (a) carries Mirror's findings on the PR itself, (b) self-heals silently when it mechanically can, and (c) when it genuinely needs Larry, appears on his Approvals tab and/or Operator Action Queue with a Telegram ping — with zero reliance on anyone watching the PR.

## 2. Decisions (locked with Larry, 2026-06-24 design pass)

| # | Decision | Value |
|---|---|---|
| 1 | Routine revisions stay silent. A mechanical, auto-fixable revision must NOT notify Larry — it self-heals via Forge. Surfacing every revision would bury him. | locked |
| 2 | Larry never has to look at a PR. Findings on the PR are for machines + audit; anything needing Larry routes to *his* surfaces (Telegram + Approvals tab + Operator Action Queue). | locked |
| 3 | Route by shape. Decision-shaped → Approvals tab (approval_request). Action-shaped (stuck, no clean binary) → Operator Action Queue (larry_alert NOW). Both also push to Telegram. | locked |
| 4 | Decisions are binary. The Approvals tab handles approve/reject only; >2 options get narrowed to binary or taken to chat. | locked |
| 5 | Actionable-only. Larry is pinged solely for (i) a scope/values decision or (ii) auto-recovery having failed — never for a revision that's still self-healing. | locked |

## 3. Reuse map (assembly, not greenfield)

| Capability | Reuses (existing) | Mode |
|---|---|---|
| Self-heal a no-session REVISION (mechanical re-dispatch to Forge + durable obligation) | `_dispatch_revision_to_forge` + `no_session_ledger` (forge-cold-start-revision; SUPERSEDES the removed `_route_no_session_revision_to_beacon`) | reuse |
| Recover-before-alert backstop | Chain Context Durability M4 (heal_pipeline_stall.py recover-then-alert) | extend |
| Push signal to Larry (quiet/loud) | larry_alerts ledger + alert_triage_state + the 5-min Telegram doorbell sweep | reuse |
| Decision surface | approval_request marker (marker.py render) → Approvals tab; heal_unregistered_approval.py net ensures approval-class escalations reach the tab | reuse |
| Action surface | Operator Action Queue panel (e4-4g, sourced from larry_alerts NOW-tier) | extend (soft dep, §9) |
| Mirror's review verdict + markers | agents/mirror/CLAUDE.md review flow; REVIEW_PASS/REVIEW_REVISION/REVIEW_ESCALATE/EMERGENCY_HALT | extend |

## 4. Contract A — Mirror findings are always visible on the PR

Whenever Mirror returns REVIEW_REVISION or REVIEW_ESCALATE, it posts its findings as a PR review/comment (in addition to the mirror-review commit status), regardless of whether a Forge session exists. This makes the findings durable, for-the-record, and consumable by Beacon/Forge without anyone digging into agent inboxes.

- **Idempotent:** on re-review, update the existing Mirror findings comment rather than appending a new one each round (no comment spam across revision rounds).
- This is for machines + audit, explicitly NOT Larry's notification path — Contract C decides what (if anything) reaches Larry.

**Enforcement:** agents/mirror/CLAUDE.md review-emission step requires a findings comment on every non-PASS verdict; a test asserts a REVIEW_REVISION/REVIEW_ESCALATE produces (or updates) exactly one Mirror findings comment on the PR.

## 5. Contract B — classify the session-less outcome

When a REVIEW_REVISION/REVIEW_ESCALATE has no `forge_build_session_id`, classify it into exactly one bucket before any notification. **Classify on signals already on the wire — `marker_type` + session/ledger state — NOT on the finding's prose semantics (the routing site cannot read those):**

1. **Self-healing** — a `REVIEW_REVISION` that `_dispatch_revision_to_forge` is handling (mechanical re-dispatch fired, obligation open). → No Larry signal. Findings still posted (Contract A).
2. **Action-needed** — a `REVIEW_REVISION` whose mechanical recovery cannot proceed (an off-chain PR with no chain envelope to re-dispatch, or a `no_session_ledger` obligation that is stuck / recovery already failed). Reduces to "go unstick this," not a yes/no.
3. **Decision-needed** — a `REVIEW_ESCALATE` (Mirror's existing "a human must decide" verdict — scope/values), or a revision that reduces cleanly to approve-the-fix vs reject.

**Enforcement:** the classifier lives at the review-result routing site in scripts/outbox_notifier.py (~L10300, where `_dispatch_revision_to_forge` is called and `no_session_ledger` obligations open/resolve); a test fixtures one input of each bucket (keyed on marker_type + session/ledger state) and asserts the bucket chosen.

## 6. Contract C — route the human-needed buckets to Larry's surfaces (the three-surface rule)

| Bucket (from §5) | Surface | Mechanism |
|---|---|---|
| Self-healing | none (silent) | mechanical re-dispatch only; Contract A still posts findings |
| Action-needed | Operator Action Queue + Telegram | larry_alert NOW-tier (deep-links to the PR + carries the copy-paste next step) |
| Decision-needed | Approvals tab + Telegram | binary approval_request (approve = option A, reject = option B), summary states both options in plain language |

- A decision MUST be emitted as an approval_request — never a plain larry_alert that only *says* "waiting on your direction" (that strands the decision off the Approvals tab; the 2026-06-03 deploy-notifier incident is the cautionary case).
- Both human-needed buckets also append to larry_alerts so the Telegram doorbell fires (loud for blocked-on-you).

**Enforcement:** routing code in outbox_notifier.py emits the bucket-appropriate artifact; tests assert (a) decision-bucket → an approval_request event reaches the Approvals-tab feed, (b) action-bucket → a NOW-tier larry_alert, (c) self-healing → no Larry-facing artifact. The existing heal_unregistered_approval.py net backstops a missed decision marker.

## 7. Contract D — actionable-only / no double-notify

Routine self-healing revisions never reach Larry (Decision 1/5). Compose with M4: attempt recovery first; escalate to a human surface (Contract C) only if recovery fails or the bucket is decision-needed. A single escalation produces exactly one decision-or-action artifact (no duplicate DMs, no Approvals + Action-Queue double-post for the same case).

**Enforcement:** idempotency keyed on the PR + head SHA in the routing site; a test asserts a re-reviewed-but-still-self-healing PR emits zero new Larry artifacts, and a recovered-then-failed case emits exactly one.

## 8. Contract E — backstop healer (catch the silent red status)

A heal_pipeline_stall.py check that detects a PR sitting with a red `mirror-review` status past a threshold with no self-heal in progress and no Larry-facing artifact, and promotes it to the correct surface per Contract C (recover-then-alert posture). This is the net for the exact #653 failure mode: a session-less PR going quiet with findings reachable only by manual digging.

**Enforcement:** new check_* in heal_pipeline_stall.py following the M4 recover-then-alert template; a test asserts a stalled session-less PR triggers a recovery/route attempt before any alert, and alerts exactly once if that fails.

## 9. Build plan

Single-repo (ourliberty-agent-core): Mirror behavior, the notifier routing, and the healer all live there, and the Larry-facing surfaces it routes to (larry_alerts/Telegram, approval_request/Approvals tab) already exist.

| Step | Repo | Scope | depends_on |
|---|---|---|---|
| 1 — findings visible | agent-core | Contract A (Mirror posts/updates a PR findings comment on every non-PASS) | — |
| 2 — classify + route | agent-core | Contracts B + C + D (bucket the no-session outcome; route decision→Approvals, action→Action-Queue/larry_alert, self-healing→silent) | 1 |
| 3 — backstop healer | agent-core | Contract E (heal_pipeline_stall.py check for silent red-status session-less PRs) | 2 |

**Sequencing note (Beacon 2026-06-24):** HOLD kickoff until `orchestrator-terminal-signal-hardening-001` lands. Step 2 here edits `outbox_notifier.py` and Step 3 edits `heal_pipeline_stall.py` — both files are touched by that in-flight hardening sequence. Synthesize + DAG-preflight + kick off THIS sequence only on the post-hardening base, to avoid the file-overlap merge conflicts (the #668 lesson).

**Soft dependency — Operator Action Queue (`e4-4g`).** The *action-needed* bucket's ideal dashboard home is the Operator Action Queue panel, still Draft. This spec does NOT block on it: the action bucket routes via larry_alert NOW-tier, which already surfaces in Telegram today and will appear in the Operator Action Queue automatically once e4-4g ships (same larry_alerts NOW feed). No rework when e4-4g lands.

## 10. Test / proof plan

- **A:** a REVIEW_REVISION on a session-less PR posts a findings comment; a second review round updates that comment (no duplicate).
- **B/C/D:** fixtures for each bucket — self-healing emits no Larry artifact; action-needed emits a NOW larry_alert (→ Telegram + Operator Action Queue); decision-needed emits a binary approval_request (→ Approvals tab + Telegram). Re-reviewed-still-self-healing emits nothing new.
- **E:** a synthetic session-less PR left with a red mirror-review status and no self-heal triggers the healer's recover-then-route before any alert.
- **End-to-end (the real gate):** replay the #653 shape — a doc PR authored off-chain that Mirror wants revised. Findings land on the PR; it self-heals if mechanical; if it needs Larry, it appears on his Approvals tab / Operator Action Queue with a Telegram ping — without anyone watching the PR.

## 11. Out of scope

- The mechanical route-to-Forge self-heal itself — shipped (forge-cold-start-revision / Chain Context Durability M2/M4); this spec rides on it.
- Changing Mirror's review judgment — what Mirror flags is unchanged; only how findings are surfaced + escalated.
- Building the Operator Action Queue panel — that's e4-4g; this spec is forward-compatible with it but routes via larry_alerts in the interim.
- Dashboard-wide notification rework — only the session-less Mirror-review path is in scope here.
