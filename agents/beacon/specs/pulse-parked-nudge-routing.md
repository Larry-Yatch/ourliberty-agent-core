# Spec: Park-the-Nudge — recurring Pulse proposals land in the Missions Parked lane

**Status:** ready for build-sequence orchestration (agent-core steps); 1 dashboard follow-up.
**Author intent (Larry, 2026-06-10):** "Integrate these with different labels so they have somewhere to land. Once they are on there they can be silenced in the chat."
**Builds on:** Missions v2 Phase 1 (`missions-v2-phase1-durable-capture.md`), Pulse Check I (`scripts/pulse_check_i.py`).

## 1. Problem statement

A recurring Pulse Check I proposal (e.g. the `smoke-5a-pf-no-marker` templating
proposal, which surfaced unchanged for 3+ consecutive cycles) re-pitches in the
digest DM every cycle until Larry acts. He often does not act **because he is
busy, not because he disagrees** — so any "decay / auto-suppress after N
ignores" mechanism is wrong: it would silently drop work he actually wants.

The correct trigger for silencing a chat nudge is **durable capture**, not
inattention. The Missions v2 Parked lane already gives follow-ups "somewhere to
land," with a built-in process to move through them (GC aging clock → parked-
aging "promote / drop / snooze?" digest). This spec routes non-auto-dispatched
Pulse proposals into that lane with a distinguishing **label**, and silences the
DM line for a proposal once — and only once — it is safely parked.

## 2. Principle (locked)

> Non-action ≠ disagreement. Never decay/suppress a recurring nudge on
> N-ignores. Route it to the Parked lane with a label; silence-in-chat is
> EARNED by durable capture, never by being ignored.

## 3. Decisions locked

| # | Decision | Rationale |
|---|----------|-----------|
| A | **Capture carries a first-class `label`** (not a tag baked into the title). | Larry asked for "different labels"; a real field is filterable/groupable and survives title edits. |
| B | **`label` is allowlisted server-side**, mirroring the `source` posture. | A leaked ingest token must only be able to write *known* labels — same threat model as `CAPTURE_ALLOWED_SOURCES`. |
| C | **Emitter-side dedup; emit once.** Pulse keys on its existing `_proposal_dedup_key` and records the returned `capture_id` in a state file. | Server idempotency is only `CAPTURE_IDEMPOTENCY_WINDOW_SEC=600` (collapses retry-floods, not a 4×/week recurrence). The emitter is the durable deduper — same pattern as `pulse-check-i-dispatched.json`. |
| D | **Reuse the tolerated ingest write path** (`POST /api/ingest/capture`, `source='agent'`), never a second direct writer of `captures.json`. | `captures.json` is a machine-owned file with exactly ONE committer (the GC healer). Pulse must POST through the same writer the system already tolerates — adding a second committer is the #409→#413 dual-committer data-loss class. |
| E | **DM suppression is per-proposal and conditional on a recorded `capture_id`.** A parked proposal is shown once as "parked → dashboard", then dropped from subsequent digests. New proposals and the Ledger headline still DM. | This is the "silence earned by capture" behavior; it must never suppress a proposal that failed to park. |
| F | **The Parked-lane label chip is a separate dashboard-repo PR.** | The build-sequence orchestrator is single-repo in V1 (orchestrator spec §4). The agent-core steps deliver the first-class label *contract + data*; the chip render is a thin follow-up in `ourliberty-dashboard`. |

## 4. Contract A — capture `label` field (agent-core: `dashboard_api.py` + `captures.json`)

Extend the capture ingest + registry with an optional `label`:

- **`CAPTURE_ALLOWED_LABELS`** — new frozen allowlist beside `CAPTURE_ALLOWED_SOURCES`. V1 members: `pulse-check-i` (room to add `pulse-check-iii`, `carry-forward`, etc. later — new label = one tuple entry, no schema change).
- **`_handle_capture_ingest`** — accept an optional top-level `label` (or `origin.label`; pick top-level for symmetry with `title`/`note`). Validate: `None` is allowed (back-compat — desktop/telegram captures carry no label); a non-`None` value must be in `CAPTURE_ALLOWED_LABELS` else `HTTPException(400, detail=f'invalid label={label!r}')`. Store it on the appended capture dict as `"label": label`.
- **Idempotency unchanged.** `_find_recent_capture` continues to key on `(title, origin.session_id)` only — label does not enter the dedup key (a re-POST of the same titled proposal with the same label collapses correctly).
- **`captures.json` schema** — bump `schema_version`; every capture record gains `"label": <str|null>`. Existing records read back as label-absent (treat missing as `None`).
- **Readers** — `_reader_captures` / `_parked_from_captures` pass `label` through to the dashboard payload so the frontend can render/filter it.
- **Payload size** — `label` counts toward `MAX_CAPTURE_PAYLOAD_BYTES=4096` (ample).

## 5. Contract B — Pulse emit + dedup + DM suppression (agent-core: `pulse_check_i.py` + emit helper)

### 5.1 Shared emit helper
Refactor the `POST /api/ingest/capture` body of `scripts/emit_capture_impl.py`
into an importable function, e.g. `emit_capture(*, title, note, source='agent',
label=None, session_id=None) -> str | None` (returns `capture_id` or `None` on
failure), so any agent script can park a card in-process. The existing CLI
wrapper (`emit_capture.sh` / `emit_capture_impl.main`) calls the same function —
no behavior change for the desktop gesture. On the droplet, Pulse reuses the
same ingest token + local API base the desktop emitter uses; failure is logged,
never raised (a failed park must NOT crash Check I).

### 5.2 Park non-auto-dispatched proposals
In `pulse_check_i.py`, after `synthesize_proposals`, for each proposal that is
**not** `_is_auto_dispatch_eligible` (the judgment/medium ones that today
re-pitch every cycle): compute `key = _proposal_dedup_key(p)`; if `key` is not
already in a new **parked-state file** (`~/agents/state/pulse-check-i-parked.json`,
mirroring `DEFAULT_DISPATCH_STATE_FILE`), call `emit_capture(title=p['title'],
note=f"{p['impact']}\n\n{p['rationale']}", source='agent', label='pulse-check-i')`.
On success, record `{key: {capture_id, parked_at}}`. Best-effort + atomic-write,
exactly like `auto_dispatch_proposals`. Wrapped in try/except — Check I never
crashes on a park failure.

### 5.3 DM suppression (decision E)
`render_dm` / `assemble_check_i` consult the parked-state file: a proposal whose
`key` has a recorded `capture_id` is rendered once as
`[parked] {title} — see dashboard Parked lane` and thereafter omitted from the
"Proposed optimizations" list. A proposal that is NOT yet parked (or whose park
failed → no `capture_id`) still DMs in full. The Ledger headline and any
auto-dispatched / new proposals are unaffected. `mode` selection stays the same
except that an all-parked digest with no fresh signal collapses toward
`heartbeat`/`no-signal` instead of re-pitching.

## 6. Contract C — dashboard Parked-lane label chip (separate repo: `ourliberty-dashboard`)

Render `capture.label` as a chip/badge on the Parked-lane card; optionally allow
filtering/grouping the lane by label. Depends only on Contract A's data being
live (not on Contract B). Tracked as a standalone PR — out of the agent-core
build-sequence per decision F.

## 7. Build plan — 2-step agent-core sequence + 1 dashboard follow-up

Linear DAG (S2 depends on S1); both `target_repo: ourliberty-agent-core`.

- **S1 — `capture-label-contract`** (depends_on: []): Contract A. Add
  `CAPTURE_ALLOWED_LABELS`, accept/validate/store `label` in
  `_handle_capture_ingest`, bump `captures.json` `schema_version`, pass `label`
  through the readers. Tests: valid label stored; unknown label → 400; absent
  label → back-compat `None`; idempotency unchanged. **Mirror focus:** label
  allowlist enforced on the write path; no second committer introduced to
  `captures.json`.
- **S2 — `pulse-park-and-silence`** (depends_on: [S1]): Contract B. Shared
  `emit_capture` helper; Pulse parks non-auto-dispatched proposals with
  emitter-side dedup state; DM suppression for parked proposals. Tests: a
  recurring medium proposal parks exactly once across N cycles; a parked
  proposal is dropped from the digest; a park failure leaves the proposal in the
  DM (never silently dropped); auto-dispatch path unaffected. **Mirror focus:**
  emit failure can never crash Check I; suppression strictly requires a recorded
  `capture_id`.
- **D1 — dashboard chip** (separate repo, post-S1): Contract C.

## 8. Test / proof plan

Per-PR unit tests as in §7. End-to-end proof after S2: run
`pulse_check_i.py --force` against a sidecar+archive fixture that yields one
medium proposal; assert (a) exactly one capture POSTed with `label=pulse-check-i`,
(b) a second `--force` run POSTs nothing (dedup), (c) the second run's DM omits
the parked proposal. The parked card then flows through the existing GC aging
clock → parked-aging digest with no new code (regression-checked, not re-built).

## 9. Out of scope (later)

- Routing other recurring nudges (Check III, standing `go:` carry-forwards) through `emit_capture` — trivial once S1/S2 land (new label + caller); deferred to keep V1 tight.
- Write-back / auto-promote of a parked Pulse card to a mission (Missions v2 Phase 3 territory).
- Cross-repo build-sequence orchestration (orchestrator V2) — would let D1 ride the same sequence.
- Automatic parallelism inference; this sequence is hand-declared linear.
