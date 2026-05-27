# PR-S4 Rectification v1 — Beacon brief

**Purpose:** PR-S4 (PR #138, merged 2026-05-27 14:54 UTC) shipped the final orchestrator wiring but with 14 wiring gaps now identified empirically + via deep audit. This brief is the load-bearing reading for the rectification PR. The dispatch text is short; details live here. Forge: read this entire brief during preflight.

**Context — how the gaps were surfaced:**

- 3 gaps caught empirically running `orchestrator-bootstrap-001` test sequence (now `status: paused` pending this fix).
- 11 additional gaps caught by a deep audit cross-referencing PR #138's actual diff against spec § 5.4 + § 5.5 + § 6 PR-S4.

All 14 must be fixed in one focused PR before any further sequence work. After this merges, a tiny `orchestrator-bootstrap-002` (2 steps) will verify all gaps resolved.

**target_repo:** ourliberty-agent-core

---

## HIGH (5) — block hands-free orchestration

### H1. Missing Mirror DAG-result handler in outbox_notifier

**File:** `scripts/outbox_notifier.py:1943-1985` area + new function needed.
**Observed:** Mirror's DAG preflight session returns `result: PASS` or `result: REVISION <reasons>` (per `agents/mirror/CLAUDE.md:362-368`). `_classify_mirror_marker` only parses REVIEW_PASS/REVISION/ESCALATE/EMERGENCY_HALT markers — none of which Mirror emits for DAG sessions. The result falls through to a generic `mirror-result` notify back to Beacon, who has no handler for this shape.
**Fix:** Add `_handle_mirror_dag_preflight_result(data)` that detects envelope's `prompt` starts with `review-sequence-dag <seq-id>`, reads the `result` field (either marker block or top-level), and:
- On PASS: triggers the kickoff path (mutates sequence file from pending → active or dispatches via the build_sequence_advancer kickoff handler).
- On REVISION: DMs Larry with the verdict + reasons + the sequence file path so he can amend.
- On unrecognized result shape: DMs Larry with a `mirror-dag-malformed-result:<seq-id>` alert.

### H2. dispatch_validator.MIN_PROMPT_LEN blanket-rejects routing-signal prompts

**File:** `scripts/dispatch_validator.py:95,111-112`. `MIN_PROMPT_LEN = 100`.
**Observed:** Beacon's kickoff APPROVAL_REQUEST has `prompt: "kickoff <seq-id>"` (~25 chars). Validation rejects with `DispatchRejected: schema rejection: prompt too short (34 chars, min 100) — likely F24 empty-prompt bug.` Same applies to `prompt: review-sequence-dag <seq-id>` (~30 chars).
**Fix:** Exempt routing-signal prompts from MIN_PROMPT_LEN when EITHER:
- `target_agent in {'build_sequence_advancer'}`, OR
- `phase == 'routing-signal'` (new phase value for explicit signals)

Either condition skips the min-length check. Keep all other validation rules intact.

### H3. Headless-approval handler source-locked to 'larry'

**File:** `scripts/outbox_notifier.py:4879` (`if data.get('source') != 'larry': return None`).
**Observed:** Advancer dispatches step envelopes with `source: 'orchestrator'` (`scripts/build_sequence_advancer.py:468,490`). Beacon emits a correct APPROVAL_REQUEST in response. The headless handler checks source and bails. The marker becomes a dead-end notify; Forge never gets dispatched. Bug #3 from the empirical test.
**Fix:** Broaden the source gate to `{'larry', 'orchestrator'}`. Cleanest: factor a module-level constant `_BEACON_TRUSTED_DISPATCH_SOURCES = frozenset({'larry', 'orchestrator'})` and use it in both `_handle_beacon_headless_approval_request` and `_handle_build_sequence_advancer_kickoff` (line 5066 area).

### H4. routing_validator.FRESH_DISPATCH_ROUTES missing build_sequence_advancer

**File:** `scripts/routing_validator.py:53-74`. `FRESH_DISPATCH_ROUTES['beacon'] = {'pulse', 'forge', 'mirror'}`.
**Observed:** Chat-mode kickoff path (`beacon_telegram_bot.py:607 → approval.dispatch_approved → safe_write_inbox:143 → validate_task → reject`) fails routing topology because `build_sequence_advancer` is not a permitted target for Beacon. The headless handler in H3 never even runs because the inbox write fails first.
**Fix:** Either (a) add `'build_sequence_advancer'` to `FRESH_DISPATCH_ROUTES['beacon']` and register a minimal `agents/build_sequence_advancer/IDENTITY.md`, OR (b) special-case the kickoff marker shape in `beacon_telegram_bot.py` to route via direct sequence-file mutation (skipping `safe_write_inbox` for this specific marker shape). Forge: pick (a) — symmetric and matches the existing pattern. (b) creates a chat/headless asymmetry that will cause future bugs.

### H5. Validator CLI invocation wrong in Beacon CLAUDE.md and spec

**Files:** `agents/beacon/CLAUDE.md:310` and `agents/beacon/specs/build-sequence-orchestrator.md:187` both say `python3 scripts/build_sequence_validator.py validate <seq-id>`. The validator's actual CLI (`scripts/build_sequence_validator.py:422-426`) only accepts ONE positional `path` argument. The instruction produces `argparse error: unrecognized arguments` and tries to open the nonexistent file `"validate"`.
**Fix:** Add a `validate <seq-id>` subcommand to `build_sequence_validator.py` that expands to `~/agents/blackboard/build-sequences/<seq-id>.json` automatically. Keep existing positional path arg for direct file usage. The CLAUDE.md and spec instructions become correct.

---

## MEDIUM (5) — break edge cases

### M1. 5/6 shortcuts have no executable enforcement

**Observed:** Only `approve sequence <seq-id>` has a Python handler (`outbox_notifier.py:_handle_build_sequence_advancer_kickoff`). The other 5 shortcuts (`pause` / `resume` / `cancel` / `retry sequence <seq-id> step <step-id>` / `skip sequence <seq-id> step <step-id>`) are CLAUDE.md prose only. Beacon-as-Claude must correctly parse chat, read sequence file, apply mutation, write atomically — no central library, no idempotency lock, no schema-validation gate. The "tests" in `test_outbox_notifier_sequence_handlers.py` `ShortcutMutationShapes` class are shape-locks that don't actually execute mutations.

Spec § 5.5 discipline 2 + Mirror review focus per PR #138 description explicitly called for "shortcut idempotency" — only the kickoff path enforces it.

**Fix:** New file `scripts/sequence_shortcut_helpers.py` providing pure-Python library:
- `apply_pause(seq_id, actor) -> Result`
- `apply_resume(seq_id, actor) -> Result`
- `apply_cancel(seq_id, actor, reason) -> Result`
- `apply_retry(seq_id, step_id, actor) -> Result`
- `apply_skip(seq_id, step_id, actor, reason) -> Result`

Each function:
1. Reads `~/agents/blackboard/build-sequences/<seq-id>.json` via the validator's existing read helper.
2. Validates current state allows the requested mutation (e.g., `apply_pause` is no-op if already paused).
3. Mutates the dict — sets new status, appends audit_log entry with actor + ts + reason.
4. Atomic-writes back (`tmp + rename`).
5. Returns `Result(applied: bool, reason: str)`. `applied=False` for WARN no-op (idempotent re-apply).

Update Beacon CLAUDE.md so each shortcut section instructs her to invoke via `python3 -c "from sequence_shortcut_helpers import apply_pause; print(apply_pause('<seq-id>', 'larry'))"` rather than hand-edit JSON. Discipline becomes executable, idempotent, and testable.

### M2. Kickoff handler swallows DAG validation failure detail

**File:** `scripts/outbox_notifier.py:5158-5178` area.
**Observed:** On `validate_dag()` failure during kickoff, DMs Larry with a generic "sequence is invalid" message. No `task_id` from the rejected marker, no marker payload echo, only the first validator error.
**Fix:** Enrich the alert message with: marker `task_id`, first 3 validator errors (not just 1), `seq_id`, and the path to the rejected sequence file. Also append to a side-channel `~/agents/blackboard/build-sequences/.kickoff-failures.jsonl` for ops audit trail.

### M3. No dedup audit_log entry for duplicate kickoffs

**Observed:** If two kickoff markers for the same `<seq-id>` exist (e.g., Larry double-tapped approve and both outboxes survive a crash), the first wins via `status != 'pending'` gate. The second is silently archived with no record in the sequence file. Hard to debug if a duplicate ever appears.
**Fix:** On WARN no-op of duplicate kickoff, append an audit_log entry: `{event: 'kickoff-duplicate-suppressed', actor: 'outbox-notifier', original_task_id: '<first>', duplicate_task_id: '<second>', ts: ...}`. Keeps the trail.

### M4. `_classify_mirror_marker` could mis-route stray REVIEW_* markers from DAG sessions

**File:** `scripts/outbox_notifier.py:1943-1985`.
**Observed:** Mirror's DAG preflight CLAUDE.md instructions tell her to emit `result: PASS/REVISION` only — but if she accidentally also emits a stray REVIEW_PASS or REVIEW_REVISION marker (e.g., habit from regular review work), the classifier picks it up and routes through auto-merge or replan — explicitly the failure mode `agents/mirror/CLAUDE.md:368` warns about. No defensive gate.
**Fix:** Add a short-circuit at the top of `_classify_mirror_marker`: if the envelope's `prompt` starts with `review-sequence-dag`, return None (defer to H1's new handler). Also add to Mirror CLAUDE.md: explicit "DAG sessions emit `result:` only, no REVIEW_* markers — those are reserved for PR reviews."

### M5. Spec § 5.4 recovery-shortcut grammar mismatch

**File:** `agents/beacon/specs/build-sequence-orchestrator.md:168-171`.
**Observed:** Spec says `resume <seq-id>` (no "sequence" prefix); PR-S4 grammar requires `resume sequence <seq-id>`. Advancer DM at `scripts/build_sequence_advancer.py:813-815` uses new grammar correctly — but if Beacon re-reads § 5.4 she'll be misled.
**Fix:** Update spec § 5.4 lines 168-171 to include the `sequence` prefix on all 4 recovery shortcuts (`resume sequence`, `cancel sequence`, `retry sequence`, `skip sequence`). Trivial spec patch — keeps doc consistent.

---

## LOW (4) — quality / test coverage

### L1+L2. Zero test coverage for source=orchestrator end-to-end + DAG result handling

**Observed:** All 35 PR-S4 tests use `source='larry'`. The source=orchestrator → target_agent=forge translation flow (the gap from H3) + Mirror DAG result round-trip (H1) have ZERO coverage.
**Fix:** New file `scripts/tests/test_outbox_notifier_dag_preflight_handlers.py` covering:
- `source=orchestrator` envelope reaches Beacon → her response auto-translates to Forge (H3 regression test)
- Mirror DAG `result: PASS` triggers kickoff path (H1 regression test)
- Mirror DAG `result: REVISION` DMs Larry with verdict (H1 happy-and-sad-path)
- Mirror DAG with stray `REVIEW_PASS` marker NOT auto-routed (M4 defensive gate test)
- `source=orchestrator` envelope with malformed marker → larry_alerts fires (L5 regression)

### L3. ShortcutMutationShapes tests are aspirational, not behavior tests

**File:** `scripts/tests/test_outbox_notifier_sequence_handlers.py:1227+`.
**Observed:** Tests like `test_pause_idempotency_on_already_paused` set up a paused sequence dict, then assert it's unchanged. But the test never ACTUALLY calls a pause function — there isn't one (M1). They're shape locks pretending to be behavior tests.
**Fix:** With M1's helper library in place, replace ShortcutMutationShapes tests with real behavior tests in a new file `scripts/tests/test_sequence_shortcut_helpers.py`:
- Each helper: happy path (state X → state Y)
- Each helper: idempotent no-op (state Y already → no-op + Result(applied=False))
- Each helper: atomic-write correctness (interrupt mid-write does not corrupt file)
- Each helper: audit_log entry shape + content

### L4. Kickoff audit_log actor mislabeled

**File:** `scripts/outbox_notifier.py:5197`.
**Observed:** Kickoff audit_log entry sets `actor: 'advancer'`. The outbox-notifier wrote the entry, not the advancer daemon. Misleading for ops debugging.
**Fix:** Change to `actor: 'outbox-notifier'` or `actor: 'kickoff-handler'`. Same correction in `runbooks/build-sequence-shortcuts.md:40` reference.

### L5. No alert on unmatched kickoff target_agent prompt

**File:** `scripts/outbox_notifier.py:5067-5079`.
**Observed:** If Beacon misemits the kickoff prompt (e.g., `prompt: "approve <id>"` instead of `"kickoff <id>"`), the handler silently archives the marker on the no-seq-id branch. Larry never learns; sequence stays `pending` forever.
**Fix:** On the no-seq-id branch, `larry_alerts.append_alert` with subject `kickoff-malformed-prompt:<task_id>` + severity `warning`. Loud failure beats silent.

---

## Sequencing + caveats

- **All 14 fixes in one PR.** Doing them separately risks Mirror revision cycles competing across PRs.
- **Real code path required.** NOT Claude-as-Forge — multi-file changes including outbox_notifier (>200 lines touched), new shortcut helpers, new test files, spec amendment.
- **After merge:** `heal-stale-daemon-code` auto-restarts `ourliberty-outbox-notifier.service` to pick up new handler code (verified working earlier today). Beacon-bot + mirror-bot also need restarts for new CLAUDE.md content — heal-stale-daemon-code handles those too.
- **Verification:** dispatch a 2-step `orchestrator-bootstrap-002` sequence (1 root + 1 parallel child) AFTER this PR merges + bots restart. If hands-free run completes without intervention, all 16 gaps confirmed resolved. If anything stalls, surface and we patch.

## Mirror review focus

- Every HIGH bug has a regression test that would have caught it (verify in test file)
- M1 helper library: read-validate-mutate-atomic-write-with-idempotency correctness across all 5 helpers
- M4 defensive gate: negative test (Mirror DAG session with stray REVIEW_PASS marker doesn't trigger auto-merge)
- Spec § 5.4 grammar matches PR-S4 shortcut implementation exactly (`sequence` prefix everywhere)
- No new asymmetries introduced (source=orchestrator and source=larry treated symmetrically by both kickoff and headless-approval handlers)
- CLAUDE.md changes don't contradict spec; spec changes don't contradict CLAUDE.md

End of brief.
