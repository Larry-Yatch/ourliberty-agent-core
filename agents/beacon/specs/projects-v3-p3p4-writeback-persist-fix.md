# projects-v3 — P3/P4 status-writeback persistence fix (p3f/p4 hotfix)

**Step id:** `p3p4-writeback-persist-fix`
**Repo:** `ourliberty-agent-core`
**Status:** spec → build (direct fix, /code-review high gated)

## Desired end state

When a phase's build launch dispatches, its card flips to **Building** on the
board; when that build sequence completes (merge), it flips to **Done**, a P4
closeout is authored onto the card, the completion DM fires, and the loose ends
land in the funnel's Suggested lane — **automatically, persisted, every time.**

The first live dogfood (2026-06-19, phase `pipeline-empty-state-hint`, seq
`launch-pipeline-empty-state-hint`, ourliberty-dashboard PR #66) proved the whole
chain silently no-op'd: the build shipped, but the phase stayed at `spec` and no
closeout was authored.

## Root cause (two layers)

1. **The building-stamp write failed with EROFS, and a premature log masked it.**
   `projects_status_writeback.stamp_building` runs *inside the
   `ourliberty-build-sequence-advancer` daemon*. That unit's systemd sandbox
   (`ProtectHome=read-only` + `ProtectSystem=strict`) listed only
   `agents/blackboard|logs|state|inboxes|credentials` in `ReadWritePaths` —
   **NOT `/home/larry/agent-core`**. So the atomic write into
   `agent-core/agents/beacon/projects.json` raised
   `OSError [Errno 30] Read-only file system` and the phase never flipped to
   `building` / never pinned its `sequence_ref`. The committer
   (`heal-projects-store`) and `outbox-notifier` units already carry the
   agent-core carve-out, so only the building-stamp was blocked.

   Compounding it: the `"stamped building"` success line was logged from *inside
   the mutator, before the atomic write*, so the log read as success even though
   the write then failed — which is why the failure hid in plain sight.

2. **The done-stamp + closeout depend on the (never-persisted) `sequence_ref`.**
   `outbox_notifier._stamp_phase_done_for_sequence` and
   `projects_closeout_author.run_closeout_for_sequence` both resolved the phase by
   `find_phase_by_sequence_ref(seq_id)`. With the building-stamp's ref never
   persisted, both no-op'd — even though the notifier unit *can* write.

This was **not** a path mismatch (all services run from `/home/larry/agent-core`)
and **not** a committer/sync clobber (the healer commits cleanly; sync refuses on
dirt rather than hard-resetting). Both were ruled out from droplet evidence.

## Changes

1. **systemd** — add `/home/larry/agent-core` to the advancer unit's
   `ReadWritePaths` (`systemd/ourliberty-build-sequence-advancer.service`). This
   is the actual root fix: the building-stamp write now persists.
   *Deploy:* needs the installed `/etc/systemd/system` copy refreshed
   (`sudo cp` + `daemon-reload`, or the install-drift healer) — a unit-file
   change is inert until reinstalled.

2. **`projects_status_writeback.py`** — honest logging: the
   `stamped building / done / attached closeout` lines now fire **only after the
   write actually persists** (gated on `_apply`'s return, not emitted inside the
   mutator). An `OSError` (EROFS / full FS) is logged loudly and distinctly so a
   recurrence is unmistakable, still swallowed (a stamp must never wedge its
   caller).

3. **One shared, `sequence_ref`-independent phase resolver.** New pure
   `projects_store.resolve_phase_for_sequence(registry, seq)` — `sequence_ref`
   first, then the `(project_id, phase_id)` in the sequence's
   `authored-by-launch-drain` audit entry (`launch_ids_from_sequence`). Used by
   BOTH `stamp_done` (takes the `seq` dict) and the closeout's
   `narrator_find_phase`, so the done-stamp and the closeout always pick the SAME
   phase. `stamp_done` then **pins `sequence_ref`** when the phase is missing it,
   via the pure `projects_store.pin_phase_sequence_ref` (which bumps `updated_at`
   and never overwrites an existing ref) — so the downstream closeout resolves
   even when the building-stamp never persisted the ref. The advancer's
   `_launch_sequence_project_id` now delegates to the shared helper too, so the
   audit-entry parsing lives in exactly one place.

4. **Honest write-failure logging** — an `OSError` is logged loudly; an `EROFS`
   specifically points the operator at the caller unit's `ReadWritePaths`.

## Invariants preserved

- **Single committer.** All writers stay NON-committers writing
  `projects.json` on disk; `heal_projects_store.py` remains the sole git
  committer.
- **Idempotent / event-driven / fail-safe.** Re-stamps are no-ops (no write, no
  spurious git delta); stamps fire only on dispatch / SEQUENCE_COMPLETE; a stamp
  never raises into the advancer or the notifier.

## Verification

- Touched unit suites green: `test_projects_status_writeback`,
  `test_projects_closeout_author`, `test_projects_closeout_outputs`,
  `test_projects_store`, `test_outbox_notifier_sequence_handlers`,
  `test_build_sequence_advancer`.
- Droplet end-to-end: after deploy + unit reinstall, (a) the advancer can write
  projects.json (no EROFS in `projects-store.log`), (b) the stranded
  `pipeline-empty-state-hint` phase reconciles to `done` with a closeout
  authored, the derive shows `done`, and the completion DM + funnel Suggested
  land.
