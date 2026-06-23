# Spec: Auto-retire Done pipeline cards after a visibility window

**Status:** ready for build
**Author:** Larry + Claude (paired brainstorm)
**Date:** 2026-06-23
**Origin:** Follow-up to phase-aware "Complete & retire" (merged #629/#77). A Done project should self-clear off the "Actively working" board after it has had its moment as a visible win — so Larry never has to click **Complete & retire** by hand.

---

## 1. The surprising finding that reframes this task

Auto-retire is **already built and live**, and it fires **immediately** — there is no visibility window.

- `projects_store.retire_completed_projects()` (`scripts/projects_store.py:909`) flips every **active** project whose phases are all stored `done` → `retired`, stamps a `gc` audit block, and keeps the funnel source suppressed.
- `heal_projects_store` already calls it every tick (`scripts/heal_projects_store.py:376`), inside the single-committer atomic-write + commit. `retired` (not `archived`) is correct: a completed project keeps its promoted funnel source suppressed (`suppresses_funnel_source` is True for `active` and `retired`, False only for `archived` — `projects_store.py:53–61`).

There is **no time gate** anywhere in that path. The instant the last phase's done-stamp lands, the next healer tick retires the project and it disappears from "Actively working." The Done win never gets its moment on the board — which is precisely why Larry still reaches for the manual button (to retire *on his terms*, after seeing it).

**So the work is not "make it retire." It is "make it wait."** Add a visibility window to the existing immediate-retire so a finished card lingers as a Done win for ~48h, then the proven machinery clears it. This is an extension of a wired-in, tested path — not a new healer, not a new store writer.

---

## 2. The change (locked)

Add an **age gate** to `retire_completed_projects()`. It already receives `now`; give it a window and a per-project `done_at`, and retire only once the window has elapsed.

### 2.1 Decisions (locked in brainstorm 2026-06-23)

1. **The clock starts on *recorded* done, not *shown* done.** The trigger stays the stored rollup (`project_is_done` over stored `lifecycle_state`), exactly as today. We do **not** wire the read-time build-sequence rollup (`sequence_status_by_id`) into the single-committer healer. Recorded-done is the durable signal with a clean timestamp; the done-stamp writeback (`projects_status_writeback.stamp_done`) drags stored up to the displayed rollup, so a card that genuinely finished becomes recorded-done in the normal course. The "shows-done-but-never-recorded-done" class is a **pre-existing** gap the manual button shares — see §4.
2. **Window = 48h**, with an env override (§2.3).
3. **Extend the existing function**, no new tick.

### 2.2 `done_at` — when did the project reach Done?

```
done_at(project) = max(parse(phase['updated_at']) for phase in project.phases)
```

`project_is_done` already guarantees **every** phase is `done` before we get here, so the max over all phases is the most-recently-finished phase's stamp — the moment the whole project crossed into Done.

- `stamp_phase_done` sets `phase['updated_at'] = now` when it flips a phase to `done` (`projects_store.py:585`). For a phase that reads done purely via a linked-sequence rollup, the writeback stamps stored `done` and bumps the same field. Either way `updated_at` is the done moment (or slightly later — conservative, the window just starts a touch later).
- **Parse fail-safe:** parse with `datetime.fromisoformat`; on `ValueError`/`TypeError` (malformed or tz-naive legacy stamp that won't compare against the tz-aware `now`), that phase contributes no timestamp. If **no** phase yields a parseable `done_at`, the project is **kept on the board** (never retired on an unknown age). Over-eager retire is the dangerous direction; lingering is recoverable by the manual button.
- In production `heal_projects_store` runs `normalize_registry` **before** the retire pass (`heal_projects_store.py:357` then `:376`), and normalize guarantees every phase has an `updated_at` (`projects_store.py:158`, `setdefault`). So `done_at` is reliably present on the live path; the fail-safe covers only hand-built / un-normalized inputs.

### 2.3 The window — config, purity, signature

Keep `retire_completed_projects` **pure** (its docstring guarantees "no IO"; that purity is what lets both writers and readers share it). The env read lives in the **caller** (the healer), passed as a parameter:

```python
# projects_store.py
DONE_RETIRE_VISIBILITY_SEC = 48 * 3600   # default window a Done card stays visible

def retire_completed_projects(
    registry, *, now=None, min_done_age_sec=DONE_RETIRE_VISIBILITY_SEC,
) -> tuple[dict, list[str]]:
    ...
    # per project, after the existing active + project_is_done gates:
    done_at = _project_done_at(proj)               # max parseable phase.updated_at, or None
    if done_at is None:
        continue                                   # unknown age → keep on board
    if (now - done_at).total_seconds() < min_done_age_sec:
        continue                                   # still inside the visibility window
    # ... existing retire: stamp gc, flip state, bump updated_at ...
```

```python
# heal_projects_store.py — env read here, keeps the store function pure
DONE_RETIRE_WINDOW_SEC = int(
    os.environ.get('OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC',
                   projects_store.DONE_RETIRE_VISIBILITY_SEC))
...
_, retired = projects_store.retire_completed_projects(
    normalized, now=now, min_done_age_sec=DONE_RETIRE_WINDOW_SEC)
```

A window of `0` reproduces today's immediate-retire (useful for tests / an ops override).

### 2.4 Why the clock actually elapses (correctness argument)

The window only works if a settled Done project's `done_at` stops moving. It does — nothing re-bumps a done phase's `updated_at` on subsequent ticks:

- `normalize_registry` uses `setdefault` — never overwrites an existing `updated_at` (`projects_store.py:158`).
- `stamp_phase_done` on `done → done` is a no-op, returns False, no bump (`projects_store.py:582`).
- `attach_phase_closeout` re-attaching an identical closeout is a no-op, no bump (`projects_store.py:598`–~`615`).
- The build-sequence rollup is read-time / non-persisting (`build_pipeline` → `_phase_card`) and never touches stored `updated_at`.

So once a project settles into all-done, `done_at` is stable and `now - done_at` grows monotonically until the window elapses. (If the closeout author bumps `updated_at` once *after* done, the window simply starts from that slightly-later settle point — still correct.)

### 2.5 Audit + log (surface the "why")

- Extend the existing `gc` stamp on a retired project with `done_at` and `window_sec` alongside the current `prior_state` / `retired_at` / `retired_by`, so the wait is legible in the store: *"retired 48h after reaching Done at T."*
- Add `done_at` + computed age to the healer's existing retire log line (`heal_projects_store.py:377–379`), in both live and dry-run paths.

---

## 3. What does NOT change

- **Manual "Complete & retire" stays immediate.** `_handle_project_archive` (`dashboard_api.py:7659`) is an explicit human gesture — it should not wait. The window governs the **auto** path only. During the window the card is still `active` + Done, so the button remains live as a "clear it now" escape hatch.
- **The retire mechanics** — `retired` (not `archived`), funnel-source suppression, the single-committer invariant, idempotency, fail-safe-on-junk — are untouched. We add one guard before the existing flip.
- **No migration / backfill.** Anything already recorded-done is already retired by today's immediate path, so there's no backlog to clear. A project that finished long ago (`done_at` > window) simply retires on the next tick — correct, the win is old. Only newly-finished projects experience the visible window.

---

## 4. Out of scope (noted, not solved)

- **Shows-done-but-never-recorded-done.** A phase that reads Done only via its linked-sequence rollup, whose stored state never reaches `done`, neither auto-retires (today or after this change) nor flips the manual button to "retire." This is a pre-existing gap the manual path shares. If telemetry later shows cards lingering in that state, revisit by feeding `sequence_status_by_id` into the healer — a deliberately separate, heavier change.
- **A "recently retired" surface.** `retired` is terminal and funnel-suppressed; the `gc.retired_at` / `gc.done_at` audit makes a future "Done wins (last N days)" read buildable. Out of scope here, but the stamp is the hook.

---

## 5. Success criteria

- A project that reaches all-phases-done **stays visible** on "Actively working" for the window, then auto-retires on the first tick past it — Larry clicks nothing.
- Within the window, the card is unchanged (still `active`, still Done, manual button still works and still retires immediately).
- A project with any non-done phase, zero phases, or an unparseable `done_at` is **never** auto-retired (conservative posture holds).
- Idempotent: once retired, further ticks retire nothing new; a non-`active` project is skipped.
- The window is configurable via `OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC`; `0` ⇒ today's immediate behavior.
- Audit (`gc.done_at`, `gc.window_sec`) and the healer log explain *why/when* each retire happened.

---

## 6. Build plan (single small change-set)

1. **`projects_store.py`** — add `DONE_RETIRE_VISIBILITY_SEC`, a `_project_done_at(project) -> Optional[datetime]` helper (max parseable phase `updated_at`, fail-safe to None), and the `min_done_age_sec` param + age gate in `retire_completed_projects`; extend the `gc` stamp with `done_at` + `window_sec`. Keep the function pure.
2. **`heal_projects_store.py`** — read `OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC` (default the store constant), pass `min_done_age_sec`, and add `done_at`/age to the retire log (live + dry-run).
3. **Tests — `scripts/tests/test_projects_store.py`** (`RetireCompletedProjectsTest`):
   - Give the `_project` fixture an `updated_at` (default an old timestamp) so the existing "retires" cases still retire under the default window; update the `gc`-assertion cases for the new `done_at` / `window_sec` fields.
   - **New window cases:** done but `done_at` inside the window ⇒ **kept** active; done and past the window ⇒ retired; boundary (`age == window`) retires; `min_done_age_sec=0` ⇒ immediate (today's behavior); unparseable / missing `updated_at` ⇒ kept; the existing idempotency/junk/non-dict cases still pass.
4. **Test — `scripts/tests/test_heal_projects_store.py`** — assert the env override is read and threaded through, and the dry-run log mentions the window/age.

No new files, no new timer, no new store writer. One guard inside a function that is already wired, tested, and committing on its timer.
