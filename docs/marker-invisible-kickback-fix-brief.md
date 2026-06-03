# Marker-invisible auto-merge gap — kickback fix

**Task:** `fix-mirror-verdict-marker-gate-001`
**Date:** 2026-06-03

## The gap

When Mirror emits a review verdict as PROSE — e.g. `**Verdict: PASS.**` — instead of
the canonical `=== REVIEW_PASS ===` marker block, the outbox-notifier silently
classifies the outbox as a generic result, never fires auto-merge, and the PR sits
open indefinitely. The `heal_pr_auto_merge` backstop misses it too, because no merge
was ever *attempted* (the healer only retries `outcome=failed` merges, not merges that
never started).

PR #277 hit exactly this shape and stayed open until a manual merge.

## Root cause — Mirror silent-None vs Forge strict-raise

The two classifiers in `scripts/outbox_notifier.py` are asymmetric:

- **Forge** (`_classify_forge_marker`): when `parse_forge_marker` yields no
  `marker_type` AND the envelope is `phase=preflight`, it **raises**
  `MalformedForgeMarker`. `process_outbox` catches it, calls
  `_notify_forge_marker_error`, and the marker-error kickback cascade re-dispatches
  Forge to re-emit a clean marker (3 retries → dead-letter to Beacon + DM Larry).
  This is why Forge self-corrects.

- **Mirror** (`_classify_mirror_marker`): when `parse_mirror_marker` yields no
  `marker_type`, it **returns `None` silently**. That falls through to default routing.
  Auto-merge keys on the canonical `REVIEW_PASS` marker, so it never fires.

`parse_mirror_marker` already raises on two near-miss shapes — loose delimiters
(`=== REVIEW_PASS ===` wrapping prose instead of JSON) and a bare keyword
(`REVIEW_PASS` alone on a line). But #277's `**Verdict: PASS.**` has *neither* a `===`
delimiter nor a bare `REVIEW_PASS` token, so the parser correctly returns
`(None, None, ...)` — and the classifier then drops it silently.

### #277 evidence

`~/agents/outboxes/mirror/.archive/alert-fix-first-outcome-routing-001.json`:

- `phase: "review"`, prompt begins `Review phase. Forge has opened PR ...` (a normal
  review dispatch — NOT the `review-sequence-dag` DAG-preflight path).
- Result body contained `**Verdict: PASS.**` and the line
  `The REVIEW_PASS marker is emitted above.` — but no canonical marker block.
- No `AUTO_MERGE` line in `outbox-notifier.log` for the task → confirms the silent
  fallthrough.

## The fix — symmetric strict gate, reusing the existing kickback

Make Mirror's None-case **raise** the same way Forge's does, for normal review
dispatches only:

In `_classify_mirror_marker`, when `parse_mirror_marker` yields no `marker_type` AND
the envelope carries `phase == "review"`, raise `MalformedMirrorMarker` with a clear
message pointing at `marker.py`. `process_outbox` already catches
`MalformedMirrorMarker` → `_notify_mirror_marker_error` → 3-retry kickback →
`_dead_letter_marker_error_to_dispatcher` (Beacon) + `_maybe_dm_larry`. **No retry
machinery is duplicated** — only the None-case is made to raise.

### Why `phase == "review"` is the correct, safe signal

It is the exact analog of Forge's `phase == "preflight"`:

- Review-request envelopes (written by `_dispatch_mirror_review` and the
  revision re-review path) carry `'phase': 'review'`.
- The **DAG-preflight** path uses a `review-sequence-dag <seq-id>` prompt, emits
  `result: PASS|REVISION` (not a `REVIEW_*` marker) **by design**, and carries **no
  `phase` field**. It is also consumed and archived by
  `_handle_mirror_dag_preflight_result` *before* `_classify_mirror_marker` runs, and
  `_classify_mirror_marker` additionally short-circuits the `review-sequence-dag`
  prompt prefix to `return None`. Gating on `phase == "review"` keeps the DAG path
  returning None on all three layers.
- Mirror **chat-mode** outputs (Larry-driven, no review dispatch) carry no
  `phase == "review"` and continue to take the default routing path.

So the gate fires on exactly the failure shape (#277) and nothing else.

## Fix at source, not at the merger

The gate catches a marker-invisible PASS at *classification* time (kicked back to
Mirror to re-emit a canonical marker) **before** it could ever reach
`heal_pr_auto_merge`. No prose-parsing / merge-on-text logic is introduced anywhere —
we do not teach the merger to read prose. The contract stays: auto-merge fires only on
the canonical `=== REVIEW_PASS ===` marker.

## Loud on exhaustion

If Mirror fails to emit a canonical marker 3 times in a row, the dispatch
dead-letters to Beacon and DMs Larry (the genuine can't-emit case is loud, never
silently dropped).

## Sibling surface — Beacon approval markers

`beacon_telegram_bot._send_beacon_response` catches `MalformedApprovalMarker` from
`extract_approval_request` but previously only logged + forwarded the raw response with
a warning — no kickback. Unlike Forge/Mirror (file-based outbox cascade driven by the
notifier daemon), the telegram bot is a *synchronous* `call_beacon` request/response
loop, so the notifier's file-based dead-letter helper does not generalize to it.
A bounded in-loop kickback (re-call `call_beacon` with a correction prompt, capped at
`MAX_MARKER_ERROR_RETRIES`, then fall back to log+forward) gives Beacon the same
re-emit chance without refactoring the bot's input loop.
