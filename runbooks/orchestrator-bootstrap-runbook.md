# Runbook — Authoring + running multi-step build sequences

**Audience:** the operator (Larry) and Beacon, driving a multi-PR build through the build-sequence orchestrator end to end.
**Spec:** `agents/beacon/specs/build-sequence-orchestrator.md` — § 5.5 (authoring disciplines + the six shortcuts), § 5.4 (failure handling), § 5.6 (dashboard ladder), § 5.1 (sequence-file schema).
**Shipped across:** PR-S1 (spec adoption + Beacon CLAUDE.md authoring section), PR-S2 (advancer daemon + validator + state schema), PR-S3a/S3b (dashboard endpoint + ladder UI), PR-S4 + PR-S4-v1 rectification (the six shortcuts + executable helpers + Mirror DAG-verify wiring).
**Related runbooks:** `runbooks/build-sequence-advancer.md` (the daemon internals), `runbooks/build-sequence-dashboard.md` (the read API), `runbooks/build-sequence-shortcuts.md` (per-shortcut mutation reference).

## What this runbook is for

The three runbooks above each document one component. This one is the **operator's through-line**: how to take a build that spans several PRs from "Larry has an idea" to "the orchestrator ran it hands-free." It ties together the authoring disciplines Beacon follows, the runtime shortcuts Larry uses to steer, and the dashboard he watches. Read the component runbooks when you need the field-level detail; read this one to understand the workflow.

The orchestrator's value proposition: once a sequence is authored and kicked off, the `build_sequence_advancer` daemon dispatches each step as its dependencies merge, with no per-step approval from Larry. Larry approved the whole sequence once at author-time; the daemon does the rest, pausing and DMing only when a step fails or Larry intervenes.

## Part 1 — Authoring a sequence (the three § 5.5 disciplines)

These are Beacon's responsibility. They are reproduced from spec § 5.5; the spec is the canonical source if this doc and the spec ever disagree.

### Discipline 1 — Spec-doc-first authoring

When Larry says "build X across multiple PRs" or "implement the Y spec," the build detail does **not** go in the Telegram dispatch text. Instead:

1. Determine whether a canonical spec doc already exists at `agents/beacon/specs/<topic>.md`. If yes, amend it. If no, draft it.
2. The spec doc must be self-contained: someone who has not seen the Telegram conversation must be able to read it and understand what to build, why, and what success looks like.
3. The spec doc is committed to `main` **before** the sequence kicks off — typically as a doc-only PR that Mirror reviews quickly. The sequence file then references spec sections by anchor rather than inlining detail.

**Why:** the sequence file's per-step `dispatch_text` is capped at 500 characters (discipline 2). The design detail has to live somewhere durable that Forge can read at build time; that somewhere is the committed spec, not the chat history.

### Discipline 2 — Sequence-file synthesis

When Larry approves a multi-step build:

1. Write the sequence file to `~/agents/blackboard/build-sequences/<seq-id>.json` per the § 5.1 schema (`seq_id`, `label`, `spec_doc`, `created_at`, `status`, `current_steps`, `steps[]`, `audit_log[]`).
2. Each step's `dispatch_text` must be ≤500 characters and contain exactly three things: (a) a one-sentence statement of what to build, (b) a pointer to the spec section by anchor, (c) a brief Mirror-review-focus line. **No design detail inline** — that is what the spec doc is for.
3. Validate the DAG before kicking off:

   ```bash
   python3 scripts/build_sequence_validator.py validate <seq-id>
   ```

   The `validate <seq-id>` subcommand expands to `~/agents/blackboard/build-sequences/<seq-id>.json` automatically (PR-S4-v1 rectification H5; before that fix the CLI only accepted a raw path). It is the single source of truth for what is structurally valid — `VALID_SEQUENCE_STATUS`, `VALID_STEP_STATUS`, and the DAG-acyclicity / depends_on-resolution checks. Any sequence-file write that fails it is a regression.
4. Emit a single APPROVAL_REQUEST with `task_id: kickoff-<seq-id>`, `target_agent: build_sequence_advancer`, `prompt: kickoff <seq-id>`. The bot routes this to the advancer rather than Forge.

**`depends_on` discipline:** for each step, ask whether the dependency is real (the step genuinely cannot start until the dep merges) or an over-conservative "just in case" ordering. Over-conservative deps serialize work that could run in parallel — prefer an empty `depends_on` for steps that share no upstream state. Steps with the same `depends_on` set render side-by-side on the ladder and dispatch together.

### Discipline 3 — Mirror preflight DAG verification

Before the kickoff APPROVAL_REQUEST is emitted, Beacon dispatches a small Mirror review of the sequence file's DAG — a separate APPROVAL_REQUEST with `task_type: code-review`, `phase: routing-signal`, `prompt: review-sequence-dag <seq-id>`.

The `phase: routing-signal` field is **required**. It exempts the short canonical prompt from the dispatch validator's `MIN_PROMPT_LEN` check (PR-S4-v1 rectification H2). Without it the validator rejects the ~30-char prompt as "too short." Emit via the CLI with the explicit flag — never hand-craft the JSON:

```bash
echo '{
  "task_id":"dag-preflight-<seq-id>",
  "summary":"DAG preflight for sequence <seq-id>",
  "target_agent":"mirror",
  "target_repo":"ourliberty-agent-core",
  "task_type":"code-review",
  "prompt":"review-sequence-dag <seq-id>"
}' \
  | python3 ~/agent-core/scripts/marker.py render beacon approval_request \
      --phase routing-signal
```

Mirror checks four things: no cycles; all `depends_on` references resolve to valid step_ids; steps declared parallel don't touch overlapping files (static analysis of dispatch_texts + spec sections); all referenced spec sections exist in the spec doc. She returns **PASS** or **REVISION-with-reasons**.

- **On REVISION:** Beacon amends the sequence file and re-dispatches the review.
- **On PASS:** `_handle_mirror_dag_preflight_result` (`scripts/outbox_notifier.py`, PR-S4-v1 rectification H1) auto-transitions the sequence file `status: pending → active` and appends a `dag-preflight-pass-kickoff` audit_log entry. The advancer's next tick (≤5 min) dispatches the root step. **No second approval is required** — Larry approved at author-time. The `approve sequence <seq-id>` shortcut (Part 2) still exists for the legacy/manual case where DAG preflight was bypassed; it is idempotent and a no-op on a sequence already advanced.

## Part 2 — Steering a running sequence (the six PR-S4 shortcuts)

Once a sequence is in flight, Larry steers it via six Telegram chat shortcuts to Beacon. They are **chat-only** — no dashboard button, no API endpoint. Beacon recognizes the canonical wording (case-insensitive on the verb; exact match on `<seq-id>` / `<step-id>`) and applies the mutation to `~/agents/blackboard/build-sequences/<seq-id>.json`.

**Schema discipline:** every shortcut mutates only existing PR-S2 schema fields (`status`, `current_steps`, `steps[].status`, `audit_log`). No shortcut invents a new field. `build_sequence_validator.py` is the source of truth for what is valid.

**Idempotency contract:** every shortcut is safe to re-run. Re-running when the target state already matches is a WARN no-op — Beacon DMs *"Sequence `X` is already <state>; no-op."* and writes nothing (no duplicate audit_log entry, no duplicate DM, no file write). For the five non-kickoff shortcuts this is enforced by `scripts/sequence_shortcut_helpers.py` (PR-S4-v1 rectification M1); for kickoff it is enforced by the `status != 'pending'` early-return in `_handle_build_sequence_advancer_kickoff`. **Always invoke via the helpers — never hand-edit the sequence file.**

| # | Shortcut | Use when | Effect | Helper |
|---|---|---|---|---|
| 1 | `approve sequence <seq-id>` | Confirm kickoff (legacy/manual path when DAG-preflight auto-transition was skipped) | Emits the kickoff APPROVAL_REQUEST → `status: pending → active`, appends `kickoff-acknowledged` | `_handle_build_sequence_advancer_kickoff` (outbox notifier) |
| 2 | `pause sequence <seq-id>` | Freeze a sequence mid-flight without losing work | `status: → paused`, appends `paused`; advancer skips the sequence next tick | `apply_pause(seq_id, 'larry')` |
| 3 | `resume sequence <seq-id>` | The condition that motivated the pause is resolved | `status: → active`, appends `resumed`; advancer resumes next tick | `apply_resume(seq_id, 'larry')` |
| 4 | `cancel sequence <seq-id>[: <reason>]` | The sequence cannot or should not complete | `status: → failed`, appends `cancelled` (with optional reason) | `apply_cancel(seq_id, 'larry', reason=...)` |
| 5 | `retry sequence <seq-id> step <step-id>` | A failed step should be re-dispatched after the cause is addressed | Resets the step to `pending` (clears `pr_url`/`dispatched_at`/`merged_at`/`current_actor`/`failure_reason`), removes it from `current_steps`, appends `step-retried` | `apply_retry(seq_id, step_id, 'larry')` |
| 6 | `skip sequence <seq-id> step <step-id>[, <reason>]` | A step's work landed out-of-band; advance past it | Sets the step to `merged` (NOT `skipped` — that's not in the enum), sets `merged_at`, appends `step-skipped` | `apply_skip(seq_id, step_id, 'larry', reason=...)` |

Invoke a helper from a Python context, e.g.:

```bash
python3 -c "from sequence_shortcut_helpers import apply_pause; print(apply_pause('pulse-upgrade-001', 'larry'))"
```

### Notes that bite operators

- **Pause is sequence-level, not step-level.** A step that was already `dispatched` / `building` / `reviewing` at pause time **keeps going** — Forge and Mirror don't know about the pause. Pause only stops the advancer from observing completion and dispatching the *next* step. To abort an in-flight step, use `cancel sequence X` instead.
- **Cancel does not close in-flight PRs.** If you also want to close an open PR, do it manually with `gh pr close <url>` after the cancel. There is no synchronous archive move and no `outcome`/`cancelled_at` field — the audit_log `cancelled` event carries the intent; the 30-day rotation handles archiving (§ 5.1).
- **Retry does not roll back partial work** the failed attempt may have pushed. Inspect `git log origin/main` and the failed PR before retrying.
- **Skip is "mark merged without a PR."** It is `merged` precisely because the advancer's dependency resolution treats `merged` as the green light for downstream steps; the `step-skipped` audit event preserves the human-readable distinction. Use sparingly — downstream steps will dispatch as if the skipped work is actually in place.

### Failure-recovery scenarios (§ 5.4)

- **Mirror REJECT-ed a step's PR:** advancer sets `step.status: failed`, sequence `status: paused`, DMs Larry. Forge addresses findings on the same branch; once Mirror re-PASSes, `retry sequence X step <step-id>` re-dispatches.
- **Merge conflict broke auto-merge:** rebase manually (`git fetch && git rebase origin/main && git push --force-with-lease`), then `retry sequence X step <step-id>`.
- **A dependency was cancelled out-of-band:** either `skip sequence X step Y, <reason>` if Y's premise still holds, or `cancel sequence X: <reason>` if the cancellation invalidates the rest.
- **Advancer daemon stale (heartbeat >15 min):** `heal_build_sequence_advancer_heartbeat.py` DMs Larry. `pause sequence X` each active sequence, investigate the daemon (`systemctl status ourliberty-build-sequence-advancer.service` + `journalctl -u ... -n 100 --no-pager`), restart if appropriate, then `resume sequence X` once the heartbeat is fresh.

Full per-scenario detail lives in `runbooks/build-sequence-shortcuts.md` § "Failure-recovery scenarios."

## Part 3 — Observing a sequence on the dashboard

Watch a running sequence at **`dashboard.ourliberty.dev/operations/build-sequences`** (the ladder panel, PR-S3b in `ourliberty-dashboard`). It polls the droplet endpoint `GET /api/system/build-sequences` (`scripts/dashboard_api.py`, PR-S3a) every ~10 s.

**List page:** active + recently-completed sequences, one clickable row each. Click a sequence to open its ladder.

**Ladder detail page:** each step is a node showing its label, status badge, current actor, elapsed time, and expected cost. Roots (`depends_on: []`) render at the top; dependents hang below with a vertical connector; steps sharing a `depends_on` set render side-by-side as parallel branches. Click a node for its `dispatch_text`, PR link, and chain-events history.

**Node colors:**

| Color | Step status |
|---|---|
| green | `merged` |
| blue | `dispatched` / `building` / `reviewing` |
| yellow | `pending` (waiting on deps) **or** the sequence is paused at this step |
| red | `failed` |

**Reading the underlying data (operator CLI sanity-check):** the endpoint returns raw sequence-file dicts in `active[]` / `archived[]` plus a `parse_warnings[]` array.

```bash
# Token lives in ~/credentials/.env.larry on the droplet.
TOKEN=$(grep DASHBOARD_API_TOKEN /home/larry/credentials/.env.larry | cut -d= -f2)

# Active sequences, summarized.
curl -s -H "X-Dashboard-Token: $TOKEN" \
  http://127.0.0.1:8000/api/system/build-sequences \
  | jq '.active[] | {seq_id, status, current_steps}'

# Non-empty parse_warnings means a sequence file is corrupted and needs triage.
curl -s -H "X-Dashboard-Token: $TOKEN" \
  http://127.0.0.1:8000/api/system/build-sequences | jq '.parse_warnings'
```

Interpreting what you see:

- **`current_steps[]` empty on an active sequence** just means the advancer hasn't dispatched the next batch yet — the next tick (≤5 min) should populate it.
- **`status: "paused"`** means the advancer halted on a failure or operator action; the latest `audit_log[]` entry explains why.
- **`audit_log[]` is append-only** — the most recent entry is the authoritative latest transition.

A non-empty `parse_warnings[]` is the one thing worth acting on immediately: a corrupted file is omitted from the dashboard rather than crashing the endpoint, so it can otherwise go unnoticed. The diagnosis recipe is in `runbooks/build-sequence-dashboard.md` § "Diagnosing an empty-state response."

## End-to-end checklist

1. **Spec on `main`** — draft/amend `agents/beacon/specs/<topic>.md`, PR it, merge (Discipline 1).
2. **Synthesize the sequence file** at `~/agents/blackboard/build-sequences/<seq-id>.json`, dispatch_texts ≤500 chars referencing spec anchors (Discipline 2).
3. **Validate** — `python3 scripts/build_sequence_validator.py validate <seq-id>` returns clean (Discipline 2).
4. **Mirror DAG preflight** — emit `review-sequence-dag <seq-id>` with `--phase routing-signal`; PASS auto-transitions to `active` (Discipline 3).
5. **Watch the ladder** at `/operations/build-sequences`; steps dispatch as deps merge.
6. **Steer as needed** via the six shortcuts; recover failures per § 5.4.

## Cross-references

- Canonical spec: `agents/beacon/specs/build-sequence-orchestrator.md` (§ 5.5 authoring, § 5.4 failure handling, § 5.6 dashboard, § 5.1 schema).
- Advancer daemon: `runbooks/build-sequence-advancer.md` (PR-S2).
- Dashboard API: `runbooks/build-sequence-dashboard.md` (PR-S3a).
- Per-shortcut mutation reference: `runbooks/build-sequence-shortcuts.md` (PR-S4).
- Validator (schema source of truth): `scripts/build_sequence_validator.py`.
- Shortcut helpers: `scripts/sequence_shortcut_helpers.py`.
- Beacon authoring + shortcut discipline: `agents/beacon/CLAUDE.md` § "How you author multi-step build sequences" and § "Multi-step build sequence shortcuts".
