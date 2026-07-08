# Spec: Completeness program PR-2 — close the verified strand gaps as point fixes

**Status:** ready for build
**Author:** desktop Claude (distilled from the completeness-architecture design v2, adversarially reviewed 3-lens)
**Approved:** Larry, 2026-07-07 (the 3-PR program; this is PR-2 of 3)
**Sequence:** step 2 of build sequence `completeness-program`; depends_on PR-1 (`agents/beacon/specs/completeness-pr1.md`) — deliberately serialized so PR-1's outcome-recording is live before these changes flow through it, and Mirror review load stays sane.

Three point fixes, each with in-repo precedent. No new registry, no observer spine (parked) — the healers are never retired, at most demoted to backstops.

## 1. Scope — three items, one PR

### (a) G1+G2 as ONE fix: sequence-step stall recovery, both faces

**Face 1 (no-PR face) — build `agents/beacon/specs/sequence-step-stall-recovery.md` exactly as adopted** (in-repo, ready-for-build since 2026-06-16; read it in full — it is the contract):
- **Fix A:** new `check_stalled_active_step` in `scripts/heal_pipeline_stall.py` — scan `~/agents/blackboard/build-sequences/*.json` for `status=='active'` sequences with a step stuck `dispatched` past `STALLED_ACTIVE_STEP_MIN` (~30m default) showing no forward progress; reuse the existing alert + `ALERT_DEDUP_HOURS` cooldown; dedup key `stalled_active_step:<seq_id>:<step_id>:<dispatched_at>` so a fresh dispatch re-arms.
- **Fix B:** recover-or-route a dead-lettered step revision — auto re-dispatch a FRESH build task applying Mirror's findings to the EXISTING PR branch (the #532 manual recovery, mechanized); escalate to Larry only if recovery cannot fire or repeatedly fails. Recover-then-alert, never alert-only-and-sit. Whether it lives in the notifier dead-letter path or the Fix-A healer is the builder's call (pick the deterministic, idempotent seat).
- Out of scope per that spec §5: the revision-preamble gate strictness (`outbox_notifier.py` `Revision N applied:` check) stays untouched.

**Face 2 (with-PR face) — the post-PR-open sequence timeout** (the gap the adopted spec does NOT cover):
- `scripts/build_sequence_advancer.py` stall guard (design cite L1451–57) requires `and not step.get('pr_url')` — so a step that opened a PR and then wedged in review has **no timeout ever**. Worse, the pre-check (design cite L1483–1506) *records* `pr_url` on detecting an open PR, permanently suppressing any future stall escalation for that step (the #532 dead-lettered-revision shape).
- Add a with-PR timeout: a step carrying `pr_url`, still not `merged`/`failed` after `REVIEW_STALL_TIMEOUT_SEC` (new tunable, default 6h — generous vs. the ~1h typical review round; distinct from the 4h no-PR `DISPATCH_STALL_TIMEOUT_SEC` at L123), triggers recover-or-route: probe the PR + chain_events for a live review; if the review loop demonstrably died (no Mirror activity, no pending revision dispatch), route recovery (re-dispatch review) and alert once if unrecoverable. Dedup so one wedged step alerts once per dispatch epoch, not per tick.
- **Two faces, one PR.** They share detection plumbing and the recover-or-route doctrine; splitting them re-creates the half-covered state this fix ends.
- Anti-noise constraint (memory `handsoff-pr-pipeline-stuck-class`, `heal-pipeline-stall` ~85% false historically): both faces detect from **durable state** (sequence files, ledger, chain_events, gh), never by re-deriving state from logs. A healthy in-flight step is left alone — tests assert the no-false-positive case.

### (b) G3: parked-capture completion write-back (terminal signal)

- Parked captures in `agents/beacon/captures.json` that spawn work (`spawned.kind`/`spawned.task_id` already stamped by the delegate/board-drain path) get **no completion signal ever** — the card outlives its work's terminal state, violating the reconciliation invariant (`specs/terminal-state-reconciliation.md`: no in-flight record outlives its work's terminal state; every store MUST have a reconciler).
- Fix: a reconcile pass (fold into the existing capture/board machinery's owning committer — `captures.json` is a machine-owned single-committer file per repo convention; find the ONE existing writer and route the write-back through it, never add a second committer) that probes each spawned-but-unresolved capture's `spawned.task_id` via `task_terminal_state` and, on identity-grade terminal evidence, stamps the capture (e.g. `spawned.outcome` + `state` transition) so the card reads done/failed instead of parked-forever.
- **UNKNOWN ⇒ KEEP**: an UNKNOWN probe changes nothing (a failed delegate like `delegate-cap-slice-2-…-8d9e`, 2026-07-07, is exactly the shape that must surface as still-open, not silently closed).
- Park-don't-decay still holds (memory `recurring-nudge-park-dont-decay`): this write-back records *completion*, it never auto-retires an unfinished card.

### (c) G5: `verification_pending` past-window rows surface in the weekly retrospective

- `scripts/cycle_prime_ledger.py` `verification_pending` rows (Decision II: promoted if verified within `PROMOTE_WINDOW_DAYS = 7`) currently **expire silently** past the window.
- Fix: rows older than the 7d window that were never promoted emit **one line each (or one grouped summary line) into the weekly retrospective** (`scripts/pulse_check_retrospective.py` Stage A — the deterministic half). Surfacing, not new machinery: no new alert channel, no page to Larry; the retrospective is the existing opt-in info surface.
- Idempotent: a row surfaces once (stamp it surfaced, or key on the retrospective's weekly family key), not weekly forever.

## 2. Out of scope

- **PR-3, the terminal-event fan-out sentinel — explicitly excluded from this program's sequence**; separate adversarially-reviewed spec later. Do not build any fan-out close, per-store retire logic, or UNKNOWN cause/aging machinery here.
- G4 (proposed-missions pile), G7 (board-file exemption — PR-3 rider), the default-flip gate, the registry+observer spine (parked).
- Loosening the revision-preamble gate (`outbox_notifier.py`) — explicitly out of scope per the adopted stall-recovery spec §5.
- Retiring any existing healer. Backstops stay; success = they find nothing, measured.

## 3. Test rules (standard, non-negotiable)

- stdlib `unittest`, **not pytest** (runner: `python3 -m unittest scripts.tests.<module>`).
- Sentinel-armed via the standard `scripts/tests/conftest.py` machinery (`OURLIBERTY_TEST_RUN_SENTINEL`); tests run inside the test-jail.
- **Zero live-tree writes**: every state path via injected roots/tmp dirs (`OURLIBERTY_AGENTS_ROOT` override), never `~/agents`. No subprocess shell-outs to `larry_alerts` (memory `choke-guard-subprocess-harness-trap`).
- Advancer tests: replay the #532 shape (step dispatched, revision dead-lettered → detected within one tick) AND the with-PR wedge (pr_url recorded, review dead → timeout fires) AND healthy-step-untouched.

## 4. Success criteria

- A stalled `dispatched` step (no PR) alerts/recovers within one healer tick; a wedged in-review step (with PR) escalates after `REVIEW_STALL_TIMEOUT_SEC`; a healthy step never alerts. The `pr_url`-recording pre-check no longer creates a permanently-unmonitored state.
- A recoverable dead-lettered step revision re-dispatches autonomously; an unrecoverable one fires exactly one actionable alert.
- A spawned capture whose task reached identity-grade terminal state is stamped complete by the owning committer; UNKNOWN keeps it open.
- A `verification_pending` row past 7d appears exactly once in the next weekly retrospective.

**Enforcement:** the stall checks are standing `heal_pipeline_stall` timer passes + an advancer tunable (the system's memory, not anyone's recall — adopted spec §6); the write-back is a reconciler registered for its store per the terminal-state invariant; the retrospective line is deterministic Stage A output; tests above are the regression net. A Mirror review-checklist note flags any future sequence-step state added without stall coverage (adopted spec §6).

## 5. Anchor facts (line cites carried verbatim from the design doc §5, verified 2026-07-07)

`alert_triage_state.py` — classify L622, triage_alert L711, default-success L715+L762–771 (CLI has no --outcome flag ~L855), Tier-3 auto-resolve L780–782 · `pulse_check_v.py` L405–414 + `action-template-executions.json` = the poisoned graduation feed · `build_sequence_advancer.py` L123 + L1451–57 + L1483–1506 (G1 permanent suppression) · `specs/sequence-step-stall-recovery.md` (ready-for-build) · `decision_resolve.py` L104–117 (entry_id-exact approvals leg), L262–272 (ledger hook) · `decision_outcome_reconcile.py` L88–110 (PR-coord-only join), no timer · `larry_alerts.py` L409 (G8 zero-trace) · `heal_droplet_git_drift` + `config/healer-managed-runtime-paths.json` (G7 exemption) · `no_session_ledger.py` L150/195 + `heal_pipeline_stall.py` L2216 (the obligation pattern) · `medic_actions.py` L489–498 (act→verify template) · `task_terminal_state.py` L253–286 (PR-only) · live numbers: triage 1472 rows = 1120 silence / 352 ask / 0 auto-fix; 7 silence files; 239 proposed missions; Feed Loop 2 rows, both un-joinable.

Line numbers drift as main advances — **re-anchor by symbol at HEAD**: the advancer stall guard is the `and not step.get('pr_url')` condition (~L1453 @ `e146c1f5`) and the pr_url-recording pre-check is the `step-pr-detected` audit branch (~L1481–1506 @ `e146c1f5`); `DISPATCH_STALL_TIMEOUT_SEC` at L123; `PROMOTE_WINDOW_DAYS = 7` in `cycle_prime_ledger.py` (~L87); `check_stalled_pending_sequence` in `heal_pipeline_stall.py` is the sibling check Fix A extends.
