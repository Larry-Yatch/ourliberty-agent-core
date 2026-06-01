# Brief: Medic — scheduled alert-operator (auto-remediation step B)

Audience: Forge. Target repo: `ourliberty-agent-core`. This brief is the
durable contract for the whole Medic workstream; it ships as **four staged
PRs**. This dispatch covers **PR1 (scaffold + escalate-only)** only. PRs 2-4
get their own dispatches later, each pointing back at this brief.

## Goal

Stand up **Medic**, a new scheduled Claude operator that consumes the
**non-allowlisted tail** of `~/agents/blackboard/larry-alerts.jsonl` — the
judgment-class alerts the auto-healers (step C) do not handle — and either
fixes the mechanical ones itself or escalates them to Larry as a written
**diagnosis + recommended command** instead of a raw alert. This closes the
remaining "Beacon DM -> Larry diagnoses by hand -> Larry pastes to Claude"
loop. Step C closed the trivially-auto-fixable class; Medic is step B.

## Why now

Step C (PR #223, merged + live) handles install-drift and stuck-timer. But the
actual top of Larry's pager is judgment-class. From the last 200 queued alerts:
inbox-stall (72), watchdog-critical / inbox-watcher down (23), pipeline/chain
-stall i.e. the headless-handoff gap (~31), plus auto-restart-FAILED, rotation
-auth-gate-blocked, droplet-behind, sync-push-failure. Those still reach Larry
raw and he is first-line diagnostician. Medic removes that.

## Locked decisions (Larry's calls — do NOT re-open or refine)

- **Separate agent**, named `medic`. Not folded into Pulse: different cadence
  (event-driven, not Pulse's build-cycle timer), different risk profile (it
  acts across many classes), its own rate-window budget, and its own
  kill-switch so Larry can silence Medic without silencing Pulse.
- **Autonomy = dial 4: gate by action *reversibility*, not by alert class.**
  - Reversible / idempotent action -> **act, then notify** (no per-class
    allowlist). Examples: restart a downed daemon, re-trigger a stalled inbox,
    re-dispatch a stalled chain leg, kick a stuck timer.
  - Privileged / irreversible action -> **propose, do not do**: write an
    approval-request whose payload is the exact command; Larry replies "go"
    and the existing approval gate runs it. Examples: anything touching
    credentials, git history, force-ops, deletes, config changes.
  - Cannot diagnose confidently -> **escalate diagnose-only**: a written
    "what I checked / best guess / recommended next step".
- **v1 scope = all three buckets**: inbox-stall, watchdog-critical, chain
  /pipeline-stall.
- **Ships active** (gating provides safety, not dry-run staging) — same
  posture as step C. The action-classification gate + per-fingerprint cap are
  the safety, not a dormant flag.
- Build via **Forge + Mirror** (privileged code), not Claude-as-Forge.

## Architecture (the whole Medic, for context)

- **Dispatcher (non-Claude, cheap):** `scripts/medic_dispatcher.py`, run by a
  frequent systemd timer. Reads the queue via Medic's **own offset**
  (`~/agents/state/medic-alerts-offset.txt`, never touches Beacon's offset),
  filters to owned classes, checks enable-flag + kill-switch + rate-window;
  if any alert qualifies, writes a batch file and invokes the Medic Claude
  operator once; then advances the offset and records outcomes in the ledger.
  If nothing qualifies it exits in milliseconds (no Claude spin-up, no idle
  burn).
- **Owned-classes config:** `config/medic-owned-classes.json` — which
  (source, subject-prefix) alerts Medic owns, each with an action-tier hint.
  Seeded with: `sentinel/inbox-stall`, `watchdog/<daemon>` critical,
  `heal-pipeline-stall/pipeline-stall:*`, `heal-stale-daemon-code/auto-restart
  -failed:*`. Fail safe: an alert not matched here is **left for Beacon**
  (Medic never swallows an alert it does not own).
- **Action-policy config:** `config/medic-action-policy.json` — maps action
  types to `reversible` vs `privileged`. This is the dial-4 classifier's data.
- **Claude operator:** working dir `agents/medic/` with a `CLAUDE.md` protocol
  (modeled on `agents/pulse/`). Invoked headless (`claude -p`) by a run script
  `scripts/run_medic.sh` (modeled on `scripts/run_cycle.sh`). Auth/env mirrors
  Pulse: `EnvironmentFile=/home/larry/credentials/.env.larry`, Tier1
  setup-token, `AGENT=medic`. The operator gets the alert batch as input,
  investigates with **read-only bash** (same allowlist posture as Beacon's
  read-only bash, PR #127), and for each alert emits a structured finding.
- **Handled-ledger:** `scripts/medic_ledger.py` ->
  `~/agents/state/medic-handled-ledger.jsonl`. Keyed by alert **fingerprint**
  (source + subject dedup key). Records: classification, action taken or
  escalation emitted, ts, and an attempt counter.
- **Escalation / notify uses the existing `scripts/larry_alerts.py` API:**
  `append_notification` for a diagnosed report or an action-taken closure;
  `append_approval_request` for a privileged proposal (payload = the command).
  Beacon already renders all three. Every escalation fits one Telegram bubble
  (<= 2000 chars).
- **Approval -> executor wiring** (PR3): reuse `beacon_approval_handler`'s
  existing "reply go -> resolve pending approval -> dispatch payload" gate,
  with a Medic executor target that runs the approved command.
- **Gates:** `OURLIBERTY_MEDIC_ENABLED` enable-flag, a Medic-specific
  kill-switch `~/agents/medic.disabled`, AND the shared `~/agents/
  healers.disabled` — all checked; any one absent/false stops action.
- **Loop-safety guards (non-negotiable):**
  - **One action per fingerprint.** If an alert recurs after Medic acted on
    it (ledger shows a prior action for that fingerprint), Medic does NOT act
    again — it escalates with "I already tried X, it recurred". No retry-loops.
  - **Rate-window check** before spinning the operator: if the rolling-5h
    window is saturated by Forge/Mirror/Pulse, defer non-urgent runs so Medic
    never starves the build pipeline.
  - **Idempotency:** offset + ledger ensure an alert is processed once.
  - **Notify-after every action**; **diagnose-only fallback** on low
    confidence.
- **Observability (PR4):** a Pulse Check watching Medic's act/escalate ratio
  and recurrence-after-action rate, feeding the self-tuning loop (do not
  hand-pick thresholds long-term).

## Staged PRs (this dispatch = PR1 only)

- **PR1 — scaffold + escalate-only (THIS DISPATCH).** Everything plumbed end
  to end, but Medic takes **no remediation action yet** — it classifies and
  writes a diagnosed escalation for every owned alert. Deliverables below.
- **PR2 — reversible-action handlers:** daemon restart + inbox re-trigger;
  flip the act-branch on for the `reversible` tier.
- **PR3 — chain-bridge handler + approval->executor wiring:** re-dispatch a
  stalled chain leg with the correct session_id / source / reply_chat_id (the
  manual bridge); wire the privileged-proposal approval executor.
- **PR4 — Pulse Check observability + self-tuning.**

## PR1 build scope

1. **`config/medic-owned-classes.json`** (new) — seeded owned classes (see
   Architecture). Include `$schema_version` and a `_doc` string.
2. **`config/medic-action-policy.json`** (new) — action-type -> tier map
   (`reversible` / `privileged`), with a `_doc` string. PR1 ships the data;
   the act-branch that consumes it is stubbed (escalate-only).
3. **`scripts/medic_ledger.py`** (new) — append/read the handled-ledger;
   fingerprint helper (source + subject dedup key); attempt-counter lookup.
   Stdlib only; never raise on IO/parse error (fail safe, WARN-log).
4. **`scripts/medic_dispatcher.py`** (new) — the cheap trigger: own-offset
   read of `larry-alerts.jsonl`, owned-class filter, enable-flag + both
   kill-switches + rate-window gate, batch-file write, invoke `run_medic.sh`
   when qualifying alerts exist (else fast exit), advance offset, record
   ledger outcomes. The rate-window check may reuse any existing helper that
   reads the rolling-5h state; if none is import-safe, stub it as
   `_rate_window_ok() -> True` with a TODO and a WARN log (PR2 wires it) —
   note this in the PR description.
5. **`scripts/run_medic.sh`** (new) — modeled on `scripts/run_cycle.sh`:
   invoke `claude -p` in `agents/medic/` with the batch as input and the
   operator protocol. Honor the same auth/env as Pulse.
6. **`agents/medic/CLAUDE.md`** (new) — the operator protocol. PR1 instructs
   the operator to, for each batched alert: investigate with read-only bash,
   classify per the action-policy, and emit a **diagnosis + recommended
   command** via `append_notification` (or `append_approval_request` if the
   recommended fix is privileged). **Take no remediation action in PR1.**
   No emoji; every escalation <= one Telegram bubble.
7. **`systemd/ourliberty-medic-dispatcher.service` + `.timer`** (new) — run
   the dispatcher on a frequent cheap cadence (propose every 2-3 min; the
   dispatcher self-gates so most ticks are no-ops). Use `OnCalendar`, not
   `OnUnitActiveSec` (perpetual-timer hardening). Add an `INSTALL.md` note or
   follow the existing `systemd/INSTALL.md` convention. Enable-flag defaults
   to **on** (ships active) via a service `Environment=` line.
8. **Read-only bash allowlist for the Medic operator** — mirror Beacon's
   read-only posture (PR #127); PR1 additionally permits invoking
   `scripts/larry_alerts.py` for the escalation writes. No mutating system
   commands in PR1.

## Tests (new `scripts/tests/test_medic_*.py`)

- **Dispatcher:** owned-class filter (matched -> batched; unmatched -> left
  for Beacon, offset still advances past it correctly); enable-flag off ->
  no-op; kill-switch present (either file) -> no-op; rate-window not-ok ->
  defer; empty/no-qualifying batch -> fast exit, no operator invocation
  (mock the `run_medic.sh` call). Offset never regresses; never touches
  Beacon's offset file.
- **Ledger:** fingerprint stability (same source+subject -> same key);
  attempt counter increments; missing/malformed ledger file -> fail safe.
- **Config loaders:** owned-classes + action-policy missing/malformed ->
  fail safe (WARN, treat as empty/unknown), never raise.
- **Escalate-only contract:** PR1 must NOT contain any mutating system call
  (no `systemctl restart/enable`, no `cp` to /etc, no re-dispatch write) in
  the operator path — assert the act-branch is stubbed. (Guards against PR2
  scope leaking into PR1.)
- All subprocess / Claude-invocation / file IO mocked; tests never spin a
  real Claude, never write the real queue, never touch real systemd.

## Constraints

- **Stdlib only** for the Python; match existing logging / state / offset /
  cooldown helper patterns in `scripts/`.
- **No emoji anywhere** (code, configs, DMs, comments) — use words.
- All three gates (enable-flag + Medic kill-switch + shared `healers.disabled`)
  must pass before any future action; PR1 has no action but must already wire
  and test the gates.
- **Medic never swallows an alert it does not own** — unowned alerts pass
  through to Beacon untouched.
- Keep all existing tests green (regression dial 3 — block only on
  PR-introduced failures).

## Out of scope (later PRs / separate workstreams)

- Reversible-action handlers (PR2), chain-bridge + approval executor (PR3),
  Pulse Check self-tuning (PR4).
- Any change to Beacon's existing alert consumption or offset.
- Widening owned classes beyond the seeded three buckets.
