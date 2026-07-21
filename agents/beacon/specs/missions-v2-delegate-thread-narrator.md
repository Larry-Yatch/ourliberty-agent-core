# Spec: Missions v2 — Delegate Thread Narrator

**Status:** Built
**Parent:** [Phase 4.1 — Schedule the Narrator](missions-v2-phase4.1-narrator-schedule.md) (the folded-sweep + single-committer precedent this rides on)
**Build path:** single-repo (agent-core only), folded into the GC healer tick

---

## 1. Purpose

A delegated Missions card already recolors its status **chip** as the work moves
(handed_off → waiting-on-approval → building → in_review → review_passed →
merged), but its team **thread** stays empty. The chip tells you the state; the
thread should tell you the *story*. This narrator gives every delegated card a
running narrative: exactly **one `team_to_larry` card_message per phase
transition**, in the team's single voice, describing WHAT HAPPENED — so opening
a card reads as a narrative instead of a blank thread under a colored dot.

**Done-gate:** a card advancing handed_off → building → in_review →
review_passed → merged accrues one post per phase (oldest-first in the thread);
re-running the sweep with no change emits zero posts; a no-PR delegation never
gets a merged post; a closed-without-merge card gets a "needs your eyes" post.

---

## 2. Design decision — single writer, folded into the GC healer

`captures.json` is the machine-owned file with the dual-committer data-loss
hazard (the #409→#413 class); `heal_missions_card_gc.py` is its sole
writer/committer. Per the Phase 4.1 precedent, the narrator is **folded into the
existing GC tick** rather than run as a second process:

- The sweep **reads** the in-memory captures registry + the delegation signals
  the dashboard reads, and **emits** `card_message` chain_events. It **NEVER
  writes captures.json** — the healer's single write+commit is untouched, so the
  single-committer invariant holds with no new writer and no new systemd timer.
- It rides the healer's ~10-min cadence (same as the meaning-layer narrator).

**Enforcement:** the sweep (`author_delegate_thread_narration`) takes the
in-memory `registry` and a Supabase client only; it has no captures.json write
path. A test asserts the sweep performs zero captures.json writes.

## 3. Contract A — one post per phase, deterministic + idempotent

The idempotency key is a **deterministic event_id derived from
(capture_id + phase)** — the phase string is the `compute_event_id` seed in the
`ts` position (NOT the wall-clock time), with `extra='delegate-thread-narrator'`
as the namespace disambiguator. The row's real `ts` (for oldest-first thread
ordering) is decoupled from the id. Emitting is an upsert with
`on_conflict='event_id', ignore_duplicates=True`, so:

- The first time a card reaches a phase, exactly one post lands.
- Every later tick re-plans the SAME event_id and the upsert no-ops it.
- A skipped phase (e.g. handed_off → in_review with no observed `building`)
  simply gets no post for the skipped phase — no phantom backfill.

**Enforcement:** `_narration_event_id(capture_id, phase)`; the upsert's
`ignore_duplicates`. Tests cover per-phase single-post idempotency (re-run emits
zero) and skipped-phase-no-phantom.

## 4. Contract B — one shared phase resolver

The sweep and the dashboard read the SAME phase from the SAME code:
`dashboard_api.resolve_delegation_narrative_phase(cap, build_events_by_origin,
has_open_approval, native_build_events)` → `{narrative_phase, narrative_pr_url}`
over {handed_off, waiting_approval, building, in_review, review_passed, merged,
closed_failed, None}. It overlays the GC healer's already-persisted terminal
stamps (`spawned.outcome`, `shipped_note`/`shipped_pr_url`, `failure_signaled`)
and `has_open_approval` onto the read-side `_delegation_trail_field` phase — no
second trail deriver, no GitHub re-probe (`pr_state_by_url=None`; merge-truth
comes from the healer stamps). This is NOT a second resolver: PR #973's
auto-resolver and the trail-field derive are reused verbatim underneath.

## 5. Contract C — honest merge signal

A `merged` post fires ONLY on a real merge stamp — `spawned.outcome == 'merged'`
(reconcile_terminal_captures) OR a `shipped_note`/`shipped_pr_url` present
(reconcile_completed_cards S3, which does NOT set `spawned.outcome`). It is never
inferred from a bare `review_passed` trail phase. A delegation with no PR that
still merged linked work gets the merged post with the **PR-less phrasing**
(`_narration_text` degrades when the merge stamp carries no URL). A
closed-without-merge card (`spawned.outcome == 'closed'` + `failure_signaled`)
gets the `closed_failed` line — "the linked PR was closed without merging —
needs your eyes."

**Enforcement:** `_capture_merge_stamped`; tests cover no-PR-never-merged and
the closed_failed line.

## 6. Contract D — FYI only, never the doorbell

Posts are FYI: `needs_reply` False. The blocked-on-you doorbell
(`missions_doorbell`) keys on `needs_reply` True, so these narration posts are a
thread echo of the chip, never a Telegram ping — they never duplicate the chip's
loudness or ring Larry.

## 7. Bound + fail-safe

`_NARRATION_MAX_PER_TICK` (40) bounds emits per tick; the remainder narrates next
tick (idempotent). A per-card resolve error, an input-assembly error, or a
per-post emit error is logged and skipped — never aborts the healer tick.

## 8. Out of scope

- No captures.json write, no dashboard chip-rendering change, no new resolver
  (the PR #973 auto-resolver / trail-field derive are reused).
- No new systemd unit (rides the existing GC healer timer).
- No "concluded, no PR" post for pure investigations (a delegation that never
  produced a build stays at handed_off / None).
- No backfill of history — the first sweep narrates each existing card's CURRENT
  phase once (deterministic id makes that the natural behavior).
