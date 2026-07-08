# Spec: Completeness program PR-1 — turn on what's built

**Status:** ready for build
**Author:** desktop Claude (distilled from the completeness-architecture design v2, adversarially reviewed 3-lens)
**Approved:** Larry, 2026-07-07 (the 3-PR program; this is PR-1 of 3)
**Sequence:** step 1 of build sequence `completeness-program` (PR-2 = `agents/beacon/specs/completeness-pr2.md` depends on this step)

North star (memory `alert-system-default-deny-northstar`): **automation, not suppression** — everything absorbed is tracked to completion; nothing silently lost. PR-1 activates the already-built tracking spine: the decision-outcome ledger becomes real (timer + joinable rows), the track record Check V graduates from stops being falsified at decision time, and silenced alerts stop leaving zero trace.

## 1. Context (what is true today, verified)

- `scripts/decision_outcome_reconcile.py` (Operator Feed Loop slice 2, PR #833) joins GitHub build outcomes onto Larry's decisions — but it is manual `--once`, **not timer-wired and not concurrency-safe** (its own reconcile docstring: "if a timer is added later this should take a file lock").
- The join is **PR-coordinate-only** (`parse_pr_coord`, `_PR_COORD_RE = r'^pr-(.+)-(\d+)$'`): a bare-task_id decision key is `skipped_non_pr`. Today **100% of the live ledger rows are bare-task_id keys** — the ledger joins nothing.
- A CLOSED-unmerged PR is recorded as a **permanent** `build_outcome='closed_unmerged'`; `has_build_outcome()` then locks the wrong "abandoned" signal in forever even if the PR is later reopened + merged.
- `scripts/alert_triage_state.py` `triage_alert()` records Tier-1 template executions with **default `outcome='success'` and no execution signal** — and `action-template-executions.json` is *the* track-record feed `scripts/pulse_check_v.py` reads for autonomy graduation. The live `triage-alert` CLI subcommand exposes **no outcome flag at all**, so every Tier-1 execution records `'success'` unconditionally.
- `scripts/larry_alerts.py` `append_alert()` returns on `is_silenced(key)` **before any record is built** — a silenced alert leaves zero trace anywhere, and nothing audits the `~/agents/state/alert-silenced/*` files (7 live).

## 2. Scope — five items, one PR

### (a) Wire `decision_outcome_reconcile` to a systemd timer, WITH a file lock first

- Add a file lock (`fcntl.flock` on a lockfile under `~/agents/state/`, non-blocking; a held lock ⇒ log + exit 0, never queue) to the reconcile pass **before** any timer fires it. Two concurrent passes can double-append `build_outcome` rows today.
- Wire a systemd `.service` + `.timer` (30-min cadence; follow the shape of existing `systemd/ourliberty-*` one-shot timers) running `--once`.
- **COORDINATION (check first, skip + note if present):** [PR #841](https://github.com/Larry-Yatch/ourliberty-agent-core/pull/841) "Operator Feed Loop: wire slices 2 & 3 to timers" was OPEN as of 2026-07-08T02:00Z and may merge before this builds; the govern-loop-assessor workstream (Operator Feed Loop slice 4, PR #844, merged) is this ledger's first reader and may also touch the wiring. At build time: if a reconcile timer unit already exists on main, **do not double-wire** — verify the lock exists; if the timer landed lock-less, add ONLY the lock. State in the PR body which case you hit.

### (b) `closed_unmerged` becomes re-checkable (pull of parked card `cap-slice-2-reconciler-hardening-closed-unmerged-ter-8d9e`)

The fix (the card's three design options, resolved here as one behavior):
- A `closed_unmerged` row is **not terminal**: rows younger than a settle period (default 14d, tunable constant) are re-checked on each reconcile pass.
- A later `merged` outcome **supersedes** `closed_unmerged` — append the corrected `build_outcome` row; readers take the newest row per `decision_key`.
- `has_build_outcome()` (or its call-site guard) **must not block the correction**: the "already has an outcome" skip must not apply when the only recorded outcome is a re-checkable `closed_unmerged` still inside the settle window.

Bundle the four non-blocking cleanups from the same `/code-review high` on #833, verbatim from the card:
1. `decision_keys_without_outcome` docstring claims a bounded scan but `read_recent` lists the whole file then slices.
2. Its resolved-set is window-scoped now that `build_outcome` rows share the file — consider a whole-file scan.
3. `reconcile` calls full-file `has_build_outcome` per key = O(keys × rows); pass the computed resolved set down.
4. `classify()`'s `'pending'` is a magic string, not in a named non-recordable set.

**Provenance / dup-work guard:** a `delegate-cap-slice-2-…-8d9e` Beacon task for this card ran 2026-07-07T23:01Z and **failed** (inbox_watcher `success=False` 23:12Z) — this PR supersedes that failed delegation. The parked govern-loop-assessor initiative names this card as its gating prerequisite. At build time, check whether the hardening already landed on main (a `closed_unmerged` re-check in `decision_outcome_reconcile.py`); if it did, skip this item and note it in the PR body.

### (c) Extend the join to bare-task_id decision keys

- For a decision key that is not a `pr-<repo>-<n>` coordinate, resolve terminal state via the shared kernel `scripts/task_terminal_state.py` (9+ consumers; probes `gh pr list` and matches task_ids against PR titles/branches — PR-only by design). Record `merged` / `closed_unmerged` (subject to (b)'s re-check semantics) when the kernel returns identity-grade evidence.
- **UNKNOWN ⇒ KEEP** (invariant, `specs/terminal-state-reconciliation.md`): an UNKNOWN probe records nothing and the key stays pending — never fabricate a terminal outcome. Non-PR work stays un-joined until per-work-type verifiers exist (explicitly PR-3+/parked territory; out of scope here).
- Respect gh rate limits: reuse the reconcile pass's existing bounded/batched gh usage patterns; don't probe more than the pending-key worklist per tick.

### (d) Fix the default-success lie: `outcome='unverified'`

- Flip the recorded default: `triage_alert()` (and its Tier-1 recording path into `record_action_template_execution`) records `outcome='unverified'` unless an explicit outcome is passed. Add `'unverified'` to the valid outcomes set.
- The `triage-alert` CLI subcommand has **no `--outcome` flag — add it** (`choices: success|failure|unverified`, default `unverified`).
- **Only a verifier path may write `'success'`** — i.e. `success` is only ever an explicit argument from a caller that ran a real verification probe (is-active-style hard probe or absence-of-recurrence); no code path defaults to it. **Enforcement:** the default value in both function signatures is `'unverified'`; a unit test asserts the CLI without `--outcome` records `unverified`; a Mirror review-checklist line (norms section of the design) flags any new template that records success at decision time.
- `pulse_check_v.py` graduation semantics: `'unverified'` executions must **not count toward a success streak** (they are absence-of-signal, not success) and must **not auto-demote** (they are not failure). Verify Check V's streak logic treats them as neutral/excluded; adjust if it currently counts any non-failure as success.
- Safety: the Tier-1 auto-fix lane is 0%-active live (1472 triage rows = 1120 silence / 352 ask / 0 auto-fix), so the flip changes no live behavior — it stops future poisoning of the graduation feed.

### (e) G8 silence-file auditor — silenced alerts leave a trace

- At the `is_silenced(key)` early-return in `append_alert()`, increment a cheap per-key suppressed-counter (append-safe state file under `~/agents/state/`; must never raise and never slow the hot path — wrap in the same fail-quiet posture as the surrounding code).
- New standing check (small script + systemd timer, or fold into the weekly retrospective run — builder's call, but it must be **timer-driven, not agent-invoked**, per memory `pulse-check-audit-2026-07-07`): list every `~/agents/state/alert-silenced/*` file with its key, age, and would-have-matched volume (the suppressed-counter) over the window.
- Output is **opt-in info, not a page**: a state-file report + one weekly retrospective line. Standalone now; folds into Check XIV's report when XIV lands (`build-xiv-v1` is in-flight in Forge — do not depend on it).

## 3. Out of scope

- PR-2 items (sequence-step stall recovery, parked-capture write-back, verification_pending surfacing) — `agents/beacon/specs/completeness-pr2.md`.
- **PR-3, the terminal-event fan-out sentinel — explicitly excluded from this program's sequence**; it gets its own adversarially-reviewed spec later.
- The default-flip gate (design §3), the registry+observer spine (parked), the G4 proposed-missions pile, G7 board-file exemption (PR-3 rider).
- Per-work-type (non-PR) terminal verifiers — `task_terminal_state` stays PR-only.

## 4. Test rules (standard, non-negotiable)

- stdlib `unittest`, **not pytest** (runner: `python3 -m unittest scripts.tests.<module>`).
- Sentinel-armed via the standard `scripts/tests/conftest.py` machinery (`OURLIBERTY_TEST_RUN_SENTINEL`); tests run inside the test-jail.
- **Zero live-tree writes**: every state path via injected roots/tmp dirs (`OURLIBERTY_AGENTS_ROOT` override), never `~/agents`. No subprocess shell-outs to `larry_alerts` (memory `choke-guard-subprocess-harness-trap`).
- Run only the touched suites; ~13 pre-existing env failures elsewhere are known.

## 5. Success criteria

- Reconcile runs on a timer, lock-guarded; a second concurrent invocation exits cleanly without writing.
- A reopened+merged PR corrects a prior `closed_unmerged` row within one settle-window pass; test replays that shape.
- A bare-task_id decision key with a merged PR (identity-grade match) gains a `build_outcome` row; an UNKNOWN stays pending (KEEP).
- `triage-alert` CLI without `--outcome` records `unverified`; Check V graduation accrues nothing from `unverified` rows.
- A silenced alert increments its suppressed-counter; the auditor report lists all silence files with age + volume.

**Enforcement:** timer + lock are systemd-installed (install-drift healer covers presence); the outcome-default is the function signature + CLI default (not convention); the auditor is a standing timer check; tests above are the regression net. Mirror review checklist: no new rule here lacks a mechanism.

## 6. Anchor facts (line cites carried verbatim from the design doc §5, verified 2026-07-07)

`alert_triage_state.py` — classify L622, triage_alert L711, default-success L715+L762–771 (CLI has no --outcome flag ~L855), Tier-3 auto-resolve L780–782 · `pulse_check_v.py` L405–414 + `action-template-executions.json` = the poisoned graduation feed · `build_sequence_advancer.py` L123 + L1451–57 + L1483–1506 (G1 permanent suppression) · `specs/sequence-step-stall-recovery.md` (ready-for-build) · `decision_resolve.py` L104–117 (entry_id-exact approvals leg), L262–272 (ledger hook) · `decision_outcome_reconcile.py` L88–110 (PR-coord-only join), no timer · `larry_alerts.py` L409 (G8 zero-trace) · `heal_droplet_git_drift` + `config/healer-managed-runtime-paths.json` (G7 exemption) · `no_session_ledger.py` L150/195 + `heal_pipeline_stall.py` L2216 (the obligation pattern) · `medic_actions.py` L489–498 (act→verify template) · `task_terminal_state.py` L253–286 (PR-only) · live numbers: triage 1472 rows = 1120 silence / 352 ask / 0 auto-fix; 7 silence files; 239 proposed missions; Feed Loop 2 rows, both un-joinable.

Line numbers drift as main advances — **re-anchor by symbol at HEAD**: `def triage_alert(` / `outcome: str = 'success'` (found at L747/L751 @ `e146c1f5`), the `triage-alert` argparse subparser (~L897), `if is_silenced(key): return False` in `append_alert` (L409 @ `e146c1f5`), `parse_pr_coord` + `_PR_COORD_RE` in `decision_outcome_reconcile.py`, and the reconcile docstring "should take a file lock" (~L196 @ `e146c1f5`).
