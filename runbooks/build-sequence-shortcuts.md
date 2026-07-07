# Runbook — Build sequence shortcuts

**Component:** Beacon's Telegram chat grammar + `scripts/outbox_notifier.py` kickoff handler + Mirror's DAG preflight discipline
**Spec:** `agents/beacon/specs/build-sequence-orchestrator.md` § 5.4 (failure handling + recovery shortcuts) and § 5.5 (the six shortcuts as designed)
**Shipped in:** PR-S4 — the orchestrator workstream finale
**Related runbooks:** `runbooks/build-sequence-advancer.md` (PR-S2 daemon); `runbooks/build-sequence-dashboard.md` (PR-S3a droplet endpoint)
**Ladder UI:** `dashboard.ourliberty.dev/operations/build-sequences` (PR-S3b in `ourliberty-dashboard`)

## What this runbook is for

Once a multi-step build sequence is in flight, Larry steers it via six Telegram chat shortcuts to Beacon. This runbook documents the canonical wording, the exact sequence-file mutation each shortcut produces, the idempotency contract, and the failure-recovery scenarios per spec § 5.4.

The shortcuts are CHAT-ONLY in V1. There is no dashboard button, no API endpoint, no slash-command parser — Beacon reads Larry's natural-language Telegram message, recognizes the canonical shortcut wording (case-insensitive on the verb), and applies the change to the sequence file at `~/agents/blackboard/build-sequences/<seq-id>.json` per the discipline in `agents/beacon/CLAUDE.md` § "Multi-step build sequence shortcuts".

## Locked schema invariants (zero drift from PR-S2)

Every shortcut mutates the existing PR-S2 schema fields. **No new fields are introduced.** The validator at `scripts/build_sequence_validator.py` is the single source of truth for what is structurally valid; any sequence-file write that fails `validate_dag` is a regression in this PR.

| Field family | Allowed values |
|---|---|
| `status` (sequence-level) | `pending`, `active`, `paused`, `complete`, `failed`, `archived` (`VALID_SEQUENCE_STATUS`) |
| `status` (step-level) | `pending`, `dispatchable`, `dispatched`, `building`, `reviewing`, `merged`, `failed` (`VALID_STEP_STATUS`) |
| `audit_log[]` | append-only list of `{ts, event, actor, ...}` dicts — events are `sequence-created`, `kickoff-acknowledged`, `paused`, `resumed`, `cancelled`, `step-retried`, `step-skipped`, plus events emitted by the advancer (`step-merged`, `step-failed`, `sequence-complete`, etc.) |

What is **not** in the schema (and must not be invented): `paused: bool`, `applied_kickoff`, `outcome`, `cancelled_at`, `skipped_at`, `skipped_reason`, `'skipped'` as a step status value. The PR-S4 dispatch brief mentioned these; the preflight CLARIFY round rejected them all to preserve PR-S2's locked schema.

## The six shortcuts

### 1. `approve sequence <seq-id>`

**Use when:** Larry has just received Beacon's plan + DAG-preflight-PASS DM and wants to kick the sequence off.

**Wording:** `approve sequence pulse-upgrade-001` (case-insensitive on the verb, exact match on the seq-id).

**What it writes:** Beacon emits an `APPROVAL_REQUEST` marker with `target_agent: build_sequence_advancer`, `task_id: kickoff-<seq-id>`, `prompt: kickoff <seq-id>`, which sets the sequence `status: pending → active` and appends a `kickoff-acknowledged` `audit_log` entry. The next advancer tick (≤5 min) discovers the dispatchable step(s) and dispatches them per spec § 5.2.

> **Two transports reach the same transition — mind which one your kickoff takes.** When the kickoff comes from a Beacon session dispatched with `source ∈ {larry, orchestrator}` (the file-outbox path), `outbox_notifier._handle_build_sequence_advancer_kickoff` performs the transition (actor `outbox-notifier`). When it comes from **`approve sequence` typed in Telegram** (the common case — this shortcut is chat-only), the bot dispatches the same marker in-process via `beacon_approval_handler.dispatch_approved`, which lands it in `~/agents/inboxes/build_sequence_advancer/`; the advancer's own `_drain_kickoff_inbox` consumes it and performs the transition (actor `advancer`). The notifier never sees the chat-path marker — so **for a Telegram kickoff, the actor in the audit log is `advancer`, not `outbox-notifier`**. Before the 2026-07-07 fix the chat path had no consumer at all and stalled the sequence at `pending` (see `runbooks/build-sequence-advancer.md` → "Kickoff-inbox drain").

**Idempotency:** if `status != "pending"` (sequence already in `{active, paused, complete, failed, archived}`), the kickoff handler logs WARN and exits without mutating status, without writing a second `kickoff-acknowledged` event, and without firing a DM. To keep the audit trail honest in the double-tap case, the handler DOES append a `{event: "kickoff-duplicate-suppressed", actor: "outbox-notifier", original_task_id, duplicate_task_id, status_at_suppression, ts}` entry — a different event type from `kickoff-acknowledged`, so the "no duplicate kickoff" invariant still holds. Re-running `approve sequence X` on an active sequence is safe.

**Failure modes:**
- Sequence file missing at the time of kickoff → handler DMs Larry: *"Sequence `X` kickoff failed: sequence file missing at `<path>`. Author the sequence file (Beacon discipline 2) before re-dispatching the kickoff."* No state change.
- Sequence file malformed JSON → DM Larry with the JSON error; no state change.
- Sequence file fails `validate_dag` (cycle, missing depends_on reference, etc.) → DM Larry with marker `task_id`, sequence file path, and the first 3 validator errors + a runnable `python3 scripts/build_sequence_validator.py validate <seq-id>` to see all errors. Also appends one JSON line to `~/agents/blackboard/build-sequences/.kickoff-failures.jsonl` for ops audit trail. No state change.
- Kickoff marker emitted with no parseable seq_id (e.g., wrong prompt verb) → handler DMs Larry with subject `kickoff-malformed-prompt:<task_id>` so the bad dispatch surfaces immediately. No state change.

### 2. `pause sequence <seq-id>`

**Use when:** Larry wants to freeze a sequence mid-flight without losing work. Common reasons: a non-emergency intervention is required upstream, Larry wants to inspect intermediate state before the next step dispatches, the dashboard ladder shows something unexpected and Larry wants the daemon to stop touching the file until he's looked.

**Wording:** `pause sequence pulse-upgrade-001`.

**What it writes:** Beacon sets `status: "paused"` and appends `{event: "paused", actor: "larry", ts: <utc>}` to `audit_log`. The advancer's next tick reads `status == "paused"` and skips the sequence entirely per spec § 5.2 (NO new step dispatch, NO gate checks on in-flight steps).

**Idempotency:** if `status == "paused"` already, Beacon WARNs to Larry (*"Sequence `X` is already paused; no-op."*) and writes nothing.

**Note on in-flight steps:** pause is sequence-level per decision I (spec § 2). Any step that was `dispatched` / `building` / `reviewing` at pause time continues — Forge and Mirror don't know about the pause. The pause prevents the advancer from observing their completion and dispatching the next step. If Larry wants to abort an in-flight step too, he should `cancel sequence X` instead.

### 3. `resume sequence <seq-id>`

**Use when:** the condition that motivated the pause is resolved.

**Wording:** `resume sequence pulse-upgrade-001`.

**What it writes:** Beacon sets `status: "active"` and appends `{event: "resumed", actor: "larry", ts: <utc>}` to `audit_log`. The advancer's next tick resumes normal processing — gate checks fire on in-flight steps; dispatchable steps dispatch.

**Idempotency:** if `status == "active"` already, WARN no-op.

### 4. `cancel sequence <seq-id>[: <reason>]`

**Use when:** the sequence cannot or should not complete. Examples: the spec underlying the sequence has been invalidated; the work has been done out-of-band; Larry has decided to pivot.

**Wording:** `cancel sequence pulse-upgrade-001` or `cancel sequence pulse-upgrade-001: spec invalidated after the merge of #999`.

**What it writes (per spec § 5.4 verbatim):** Beacon sets `status: "failed"` and appends `{event: "cancelled", actor: "larry", reason: "<Larry's text after the colon if present, else omitted>", ts: <utc>}` to `audit_log`. **No synchronous move to `.archive/YYYY-MM/`** — the 30-day rotation handles archiving per spec § 5.1. **No `outcome` field**, no `cancelled_at` field — the audit_log event carries the intent.

**Idempotency:** if `status` is already in `{failed, complete, archived}`, WARN no-op.

**Note on in-flight PRs:** cancel does NOT close in-flight PRs. If you also want to close PR-α₁'s open PR, do that manually with `gh pr close <url>` after the cancel.

### 5. `retry sequence <seq-id> step <step-id>`

**Use when:** a step has `status: failed` and Larry wants to re-dispatch it after addressing the underlying cause. Common scenarios:
- Mirror REJECT-ed PR-X's first attempt; Forge addresses the findings, then `retry sequence X step PR-X` re-dispatches.
- A merge conflict broke auto-merge; Larry rebases manually, then `retry` re-dispatches.
- A transient infrastructure failure (e.g., GitHub API outage during PR-open) caused the step to fail; conditions have since recovered.

**Wording:** `retry sequence pulse-upgrade-001 step alpha-2`.

**What it writes:** Beacon resets the step's failure state — sets `step.status: "pending"`, `step.dispatched_at: null`, `step.pr_url: null`, `step.current_actor: null`, `step.failure_reason: null`, `step.merged_at: null`. Removes `<step-id>` from sequence-level `current_steps` if present. Appends `{event: "step-retried", step_id: "<step-id>", actor: "larry", ts: <utc>}` to `audit_log`.

The advancer's next tick sees the step is `pending` with its deps still resolved (they merged before) and dispatches it via the existing pending → dispatchable → dispatched path — no special-case logic required in the daemon.

**Idempotency:** if `step.status == "pending"` already, WARN no-op.

**Pitfall:** retry does NOT roll back changes the step's previous failed attempt may have made (e.g., partial commits Forge pushed before failing). Inspect `git log origin/main` and the failed PR (if it opened) before retrying, to confirm there's nothing to clean up first.

### 6. `skip sequence <seq-id> step <step-id>[, <reason>]`

**Use when:** the step's work has been completed out-of-band and the sequence should advance past it. Example: a hotfix PR landed before the orchestrator dispatched the planned step; the planned PR is now obsolete.

**Wording:** `skip sequence pulse-upgrade-001 step alpha-2` or `skip sequence pulse-upgrade-001 step alpha-2, hotfix #134 covered this scope`.

**What it writes (per spec § 5.4 verbatim — "mark a step as `merged` without an actual PR"):** Beacon sets `step.status: "merged"`, `step.merged_at: <utc>` (so the audit trail has a timestamp). Appends `{event: "step-skipped", step_id: "<step-id>", reason: "<Larry's text after the comma if present, else omitted>", actor: "larry", ts: <utc>}` to `audit_log`.

**Why `merged` and not `skipped`:** the validator's `VALID_STEP_STATUS` enum does not include `"skipped"`. The advancer's dependency resolution treats `"merged"` as the green-light for downstream steps, so resumption works without enum changes. The audit_log event (`step-skipped` vs `step-merged`) preserves the operational distinction for human readers.

**Idempotency:** if `step.status == "merged"` already, WARN no-op.

**Pitfall:** skip should be used sparingly. Skipping a step means downstream steps will dispatch as if it had been built — if the skipped work isn't actually in place, downstream steps will likely fail.

## Failure-recovery scenarios (spec § 5.4)

The four canonical failure modes and the shortcut sequence that resolves each:

### Scenario A — Mirror REJECT-ed a step's PR (Forge needs to address findings)

1. Mirror posts REJECT on PR-X's review; the advancer detects via chain_events and sets `step.status: failed`, sequence `status: paused`, DMs Larry.
2. Forge addresses the findings — pushes new commits to the existing PR branch.
3. Once Mirror re-reviews and PASSes (or Larry decides to take a different approach), Larry runs `retry sequence X step PR-X`.
4. The advancer's next tick re-dispatches step PR-X. The cycle resumes.

### Scenario B — Step's PR fails to merge due to a conflict

1. Mirror PASSes the PR but auto-merge fails because main has moved.
2. Larry rebases the PR branch manually: `git fetch && git rebase origin/main && git push --force-with-lease`.
3. Larry runs `retry sequence X step <step-id>` — the advancer's next tick re-checks the gate and merges.

### Scenario C — A step's dependency was cancelled out-of-band (the dep was supposed to merge but won't)

1. Step Y was supposed to depend on step X, but X has been cancelled (PR closed without merging).
2. Two paths:
   - **Skip Y if the cancellation was intentional and Y's premise still holds:** `skip sequence Z step Y, X was cancelled but its scope was absorbed into the hotfix at #134`.
   - **Cancel the whole sequence if the cancellation invalidates the rest:** `cancel sequence Z: dep X cancellation invalidates downstream steps`.

### Scenario D — Advancer daemon is stale (heartbeat hasn't been touched in >15 min)

1. Larry receives a DM from `heal_build_sequence_advancer_heartbeat.py` (PR-S2 healer): *"Advancer heartbeat is N min stale."*
2. Pause everything immediately while diagnosing: `pause sequence X` for each active sequence.
3. Investigate the daemon: `systemctl status ourliberty-build-sequence-advancer.service && journalctl -u ourliberty-build-sequence-advancer.service -n 100 --no-pager`. Restart if appropriate: `sudo systemctl restart ourliberty-build-sequence-advancer.service`.
4. Once `systemctl is-active` returns `active` and the heartbeat file at `~/agents/blackboard/build-sequence-advancer.heartbeat` is fresh, `resume sequence X` for each paused sequence.

## Idempotency contract

Every shortcut is safe to re-run. The contract:

- Re-running a shortcut when the target state already matches the desired state is a WARN no-op. Beacon DMs Larry: *"Sequence `X` is already <state>; no-op."*
- WARN no-ops MUST NOT append a duplicate audit_log entry, MUST NOT fire a duplicate DM cascade, MUST NOT write the sequence file.
- The idempotency check uses the existing `status` (sequence-level) or `step.status` (step-level) field — never a separate flag like `applied_kickoff` (which does not exist in the schema).

This contract is enforced by:
- `outbox_notifier._handle_build_sequence_advancer_kickoff` for the `approve sequence X` path (via the `status != 'pending'` early-return).
- Beacon's CLAUDE.md discipline for the other five shortcuts (Beacon-as-Claude reads the current status before mutating).
- The test suite at `scripts/tests/test_outbox_notifier_sequence_handlers.py` regression-guards both layers.

## Cross-references

- Spec: `agents/beacon/specs/build-sequence-orchestrator.md` § 5.4 + § 5.5.
- Advancer runbook: `runbooks/build-sequence-advancer.md` (PR-S2).
- Dashboard runbook: `runbooks/build-sequence-dashboard.md` (PR-S3a).
- Ladder UI: `dashboard.ourliberty.dev/operations/build-sequences` (PR-S3b in `ourliberty-dashboard`).
- Validator (source of truth for the schema enums): `scripts/build_sequence_validator.py`.
- Beacon CLAUDE.md authoring discipline: `agents/beacon/CLAUDE.md` § "Multi-step build sequence shortcuts" and § "How you author multi-step build sequences".
- Mirror DAG-verify discipline: `agents/mirror/CLAUDE.md` § "DAG verification for build sequences".
