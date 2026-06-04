# Phantom-dispatch reconciliation — Beacon claims a dispatch that never happened

Status: Larry-approved scope, 2026-06-03 ("dispatch it, then harden against it"). One Forge build.

## Why

On 2026-06-03 Beacon told Larry via Telegram: "Approved — `deploy-notifier-ready-logonly`
dispatches to Forge now." It never did. 15+ minutes later there was NO Forge inbox envelope, NO
in-flight worktree, NO outbox-notifier translation, and NO record in the approvals store — verified
across every surface. Beacon asserted an action in conversational prose without the canonical marker
that actually performs it, and nothing detected the discrepancy. The approved work silently stranded;
Larry only caught it by asking.

This is the same marker-vs-prose disease as the Mirror auto-merge drift
([[feedback_author_self_approval_merge_gap]]) and the Approvals-tab coverage gap — but at the
DISPATCH-CONFIRMATION step. The approval-tab reconciliation (separately dispatched,
`harden-approval-tab-direction-ask-coverage`) scans `larry-alerts.jsonl` for unregistered approval
ASKS; it does NOT catch a phantom dispatch CLAIM that left no alert at all (this one was pure chat).
This build is the missing net for that surface.

## Goal

Detect when Beacon tells Larry it dispatched (or approved-and-dispatched) a task to Forge but no real
Forge dispatch exists, and escalate — so an approved item can never again be silently lost between
"Beacon said so" and reality. DETECTION ONLY (re-constructing an envelope from chat is unsafe).

Read first: `scripts/heal_pulse_check_staleness.py` (healer pattern), `scripts/larry_alerts.py`
(append_alert), `scripts/beacon_telegram_bot.py` + `~/agents/logs/beacon_telegram_bot.log` (the
outbound `-> <chat>: '...'` message log — confirm the canonical log path + line format in preflight),
`scripts/outbox_notifier.py` / `~/agents/logs/outbox-notifier.log` (dispatch/translation lines).

## Locked decisions (Larry approved — do not re-open)

1. New deterministic healer `scripts/heal_phantom_dispatch_claim.py` + systemd `.service`/`.timer`,
   following the `heal_pulse_check_staleness.py` pattern: heartbeat, `healers.disabled` kill-switch,
   stdlib only, `EnvironmentFile=.env.larry`, OnCalendar timer. DETECTION ONLY — it escalates, it does
   not re-dispatch.

2. INPUT: Beacon's outbound message log (`~/agents/logs/beacon_telegram_bot.log`, the
   `-> <chat>: '...'` lines — verify canonical path/format in preflight). Scan a trailing window
   (default 30 min).

3. DETECT dispatch-claims conservatively, via a config-driven pattern list
   (`config/phantom-dispatch-claim-patterns.json`): an outbound Beacon message that BOTH claims a
   Forge dispatch (verbs/phrases like "dispatches to Forge", "dispatched to Forge", "goes to Forge
   now", "Approved —") AND yields a task_id (prefer a backtick-quoted token; else a kebab-case token
   adjacent to the claim). A claim with no extractable task_id is recorded as an ambiguous claim
   (decision 5). Keep patterns conservative + editable.

4. VERIFY each claimed task_id within a GRACE window (default 10 min after the claim ts — must exceed
   the outbox-notifier poll interval so normal lag never false-alarms). A dispatch is REAL if ANY of:
   a Forge inbox file `<task>.json` / `build-<task>.json` (live, `.archive`, or `.invalid`); a
   `state/in-flight/<task>.json`; an active `git worktree` `wt-forge-<task>`; or an
   `outbox-notifier.log` dispatch/translation line referencing the task. If ANY exists -> dispatched,
   no alert. If NONE after the grace window -> PHANTOM.

5. ON PHANTOM: emit a larry-alert (route=escalate, severity warning), subject
   `phantom-dispatch:<task_id>`, message naming the task + claim ts + that no Forge dispatch exists N
   min later, suggested_action = verify and re-dispatch (point at the chat). For ambiguous (no
   task_id) claims, alert ONLY if the message strongly indicates a dispatch AND no Forge dispatch
   activity at all followed within the window — favor a slightly noisy alert over a silent phantom,
   but keep false-positive risk low.

6. DEDUP: persist alerted claim keys (task_id + claim ts) in a state file; alert each phantom once;
   idempotent across ticks. Heartbeat each run; self-failure -> larry-alert.

## Acceptance

- Fixture replaying the exact 2026-06-03 message ("Approved — `deploy-notifier-ready-logonly`
  dispatches to Forge now") with NO matching Forge artifact -> exactly one
  `phantom-dispatch:deploy-notifier-ready-logonly` alert after the grace window.
- A claim WITH a matching artifact (test each path: live inbox, `.archive`, worktree,
  outbox-notifier line) -> no alert.
- A claim still inside the grace window -> no alert yet (lag-tolerance test).
- Run twice -> each phantom alerted exactly once (idempotent).
- A routine non-dispatch Beacon message -> no alert (conservative-pattern test).
- Standard healer shape (heartbeat, kill-switch, install-drift coverage); stdlib + existing deps;
  Forge flow preflight -> build -> Mirror -> PR; conventional commits.

## Out of scope

- Auto-re-dispatch (detection only this round).
- The STRONGER structural fix — have the dispatch path / notifier emit the authoritative "dispatched"
  DM (so the word "dispatched" can only appear when a real envelope was written), instead of trusting
  Beacon's prose. Note it in the PR body as the recommended follow-up; do NOT build it here.
- The approval-tab coverage reconciliation (separate, already dispatched).
- Any change to how Beacon emits markers (CLAUDE.md may gain a one-line note that a "dispatched"
  claim must be marker-backed and is now enforced by this healer).
