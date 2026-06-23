# Orchestrator terminal-signal propagation + backstop integrity hardening

**Type:** Reliability hardening — build-sequence orchestrator (advancer) + the self-healing backstop layer.
**Author:** Beacon (2026-06-23), from a two-part code audit triggered by repeated silent stalls.
**Motivation:** A bug *class*: a terminal/decision signal from Forge or Mirror fails to propagate into orchestrator state, and the backstops meant to catch the resulting stall are themselves drifted, dead, or alert-suppressing. Confirmed live: PRs #645/#653 (Mirror no-session REVISION) and the `launch-system-self-awareness-slice-2b` sequence (Forge preflight REJECT never consumed; paused after 4h with the WRONG reason).

---

## 1. Root cause (verified in code 2026-06-23)

The advancer's failure detector keys on chain_event types the shipper **never emits**. `build_sequence_advancer.chain_event_says_failed` (build_sequence_advancer.py:372-387) matches `('mirror_revision_exhausted','mirror_emergency_halt','forge_reject')` and reads `payload['reason']`. Production actually emits `preflight_reject` (outbox_notifier.py:1701-1702), `review_escalate` (:1777-1783), `review_revision`, and `auto_merge` (chain_event_shipper.py:101); the names the advancer queries are not in `KNOWN_EVENT_TYPES` (chain_event_shipper.py:97-133) and are never produced. The advancer was coded against this orchestrator spec's *hypothetical* event names (build-sequence-orchestrator.md §5.4/§6), which diverged from the shipper.

**Consequence:** only the merge path is fully wired (`_signal_sequence_step_merged`, outbox_notifier.py:5629/5699). Every non-merge terminal outcome falls through to the 4h time-based stall backstop (`_escalate_stranded_dispatched_steps`, heal_pipeline_stall.py:1143, `DISPATCH_STALL_TIMEOUT_SEC=4h`), which never consults the existing terminal result and so misattributes "Forge may never have picked it up."

## 2. Scope

**In:**
- **A. Realign advancer terminal detection** with the emitted event types + payload shapes: `preflight_reject` (payload `marker_type`/`intent`, no `reason`), `review_escalate` (covers Mirror ESCALATE, EMERGENCY_HALT, and revision-budget exhaustion), and correct `review_revision` semantics (stays in-flight, not failed). Fix the payload-key read.
- **B. Push-signal non-merge terminals.** Add a `_signal_sequence_step_failed` sibling to `_signal_sequence_step_merged` so reject / marker-error / dead-letter / build-crash transition the step promptly instead of waiting on the pull poll + 4h timer.
- **C. Record `pr_url` + an in-flight substatus at PR-open**, not only at merge — restoring the dual-gate `gh` leg during review (build_sequence_advancer.py:704) and the never-PR-vs-open-PR-stuck distinguisher the stall heuristic relies on. Set `building`/`reviewing` per orchestrator §5.1.
- **D. Fix the stall-backstop attribution.** Before blaming "never picked up," consult the existing terminal forge/mirror result (`_latest_forge_build_result` already exists at heal_pipeline_stall.py:1293; `chain_event_says_failed` on the real types) and write the CORRECT reason (rejected / escalated / genuinely-long-build), routing each appropriately.
- **E. Backstop integrity sweep** (heal_pipeline_stall.py): fix Check 6 dead regex; fix-or-retire Check 8 (`TIER2_FALLBACK_*` has zero producer in code — either emit the signal at the real fallback site or retire the check); fix Check 9 alert-suppression; verify Checks 5/7 triggers against current production and repair drift. Cross-cutting: prefer durable structured triggers (state files / chain_events / routing-events JSON) over fragile log-string regexes, and make M4 recovery-success mean *verified-resolved* (re-read the stall condition post-recovery), not *inbox-write-succeeded*.
- **F. No-session REVISION mechanical recovery** (from the 2026-06-23 deep-dive): convert the fire-and-forget Beacon notify into a mechanical Forge re-dispatch (fresh task_id, findings + existing branch, phase=preflight so mechanical findings auto-fix and decision findings degrade to a Forge CLARIFY→Beacon), backed by a durable obligation record and a loud, non-suppressed Larry alert if it does not progress.

**Out:**
- The `launch_queue_drain` "re-launch of an already-shipped phase" bug (the slice-2b duplicate) — that is board phase-status reconciliation, a SEPARATE fix; noted here, specced separately.
- Any new agent capability, SSE/Realtime, or non-orchestrator surface.
- Renumbering/normalizing the orchestrator spec's event-name vocabulary beyond what A requires (the canonical fix is to match the shipper, and to add a regression test that locks advancer detection to `KNOWN_EVENT_TYPES`).

## 3. Gap inventory (evidence)

**Advancer (per terminal outcome):** PROCEED — no substatus (GAP, low). CLARIFY — no waiting substatus (GAP, low). **REJECT — GAP (root cause): `preflight_reject` not matched.** marker-error/dead-letter — GAP: no handler. build-crash/no-PR — GAP: no handler. PR-opened — GAP: `pr_url` only at merge. PASS→merge — CONSUMED (only wired path). REVISION(session) — OK by luck (not matched = stays waiting). **ESCALATE/HALT — GAP: `review_escalate` not matched.** auto-merge-FAIL-after-PASS — correct (no false merge) but strands to 4h if heal_pr_auto_merge never lands.

**Backstops (heal_pipeline_stall, runs via `ourliberty-heal-pipeline-stall.timer`, ~5 min cadence — confirmed scheduled):** Check 6 no-session — DEAD (regex drift). Check 8 tier2_fallback — DEAD (no producer ever). Check 9 stalled-pending-sequence — SUPPRESSES-ALERT (re-deposits same notify, then silences human on write-success). Checks 5 (retry_exhausted) / 7 (unrouted_open_prs) — SUSPECT, triggers unverified vs production. Checks 1-4, heal_pr_auto_merge, and the state/process-based healers — OK.

## 4. Fix design

For A/D/E the fix is to read the SAME durable structured signals the system already produces (chain_events with their real types; `_latest_forge_build_result`) rather than guessed names or fragile log strings. For B/C add the missing push transitions + open-time `pr_url`/substatus. For F see the dedicated 2026-06-23 no-session deep-dive (mechanical re-dispatch + durable obligation + loud alert). Every backstop trigger that survives this work must key on a structured signal or carry a regression test pinning it to the exact production string it matches.

## 5. Build plan (single-repo: ourliberty-agent-core)

- **Step 1 — advancer-failure-detection** (root cause): realign `chain_event_says_failed` to `preflight_reject`/`review_escalate` + correct payload keys + a regression test pinning detection to `KNOWN_EVENT_TYPES` so this can't silently drift again (A) AND fix the stall-backstop attribution to consult the existing terminal result (D). Smallest, highest-value; fixes the confirmed slice-2b class. *(no deps)*
- **Step 2 — push-signal-and-substatus**: add `_signal_sequence_step_failed`; record `pr_url` + `building`/`reviewing` substatus at PR-open (B + C). *(depends_on step 1 — shares advancer/notifier surface)*
- **Step 3 — backstop-integrity**: Check 6 regex/structured-trigger; fix-or-retire Check 8; Check 9 verified-resolved + un-suppress; verify/repair Checks 5/7; M4 recovery-success = re-read-condition (E). *(no deps — heal_pipeline_stall surface; parallel to step 1)*
- **Step 4 — no-session-mechanical-recovery**: mechanical Forge re-dispatch + durable obligation + loud alert (F). *(depends_on step 3 — builds on the M4/Check-6 changes)*

DAG: {1 -> 2} and {3 -> 4}; {1} || {3}. Each PR Mirror-reviewed + tested; the merge path (the only currently-wired transition) must stay green untouched.

## 6. Test / proof plan

- Step 1: a seeded `preflight_reject` event fails its step within one tick with reason from `marker_type`/`intent`; a `review_escalate` fails its step; a `review_revision` does NOT fail (stays in-flight); a healthy PROCEED->PASS->merge is NOT falsely failed; the stall backstop, given a step with a prior terminal reject, writes "rejected" not "never picked up". Regression test asserts every name in `chain_event_says_failed` is in `KNOWN_EVENT_TYPES`.
- Step 2: a forge-reject push-signals the step failed without waiting for the poll; `pr_url` + substatus are set at PR-open; the dual-gate gh leg is live during review.
- Step 3: Check 6 fires on the real `NO_SESSION_REVISION` line (or its structured signal); Check 8 either fires on a real producer or is removed with rationale; Check 9 re-reads the stall condition and only suppresses the alert when the stall is genuinely gone; a unit test pins each surviving log-string trigger to the exact production string.
- Step 4: a no-session REVISION mechanically re-dispatches Forge to the existing branch (no LLM turn); the obligation is recorded and cleared on progress; a non-progressing obligation raises ONE loud, non-suppressed Larry alert past threshold.

## 7. Risks & guardrails

- **Touching the LIVE advancer while Phase 4b is in flight.** All changes are additive to *failure* detection; the merge path (the only wired, working transition) is not modified, and tests assert a healthy step is never falsely failed. Land step 1 only after confirming no false-fail in test; Phase 4b's in-flight steps will likely have merged before step 1 lands.
- **Don't over-correct into double-handling.** Several healers overlap (Check 2/7, Check 3/heal_pr_auto_merge). The sweep must not introduce duplicate alerts; de-dupe on a shared cooldown key.
- **A retired backstop is a removed safety net.** If Check 8 is retired rather than wired, the spec must state what now covers tier-2 fallback failures (or accept the gap explicitly with a waiver).

## 8. Enforcement

- **Rule: a backstop trigger MUST key on a durable structured signal, or carry a regression test pinning it to the exact production string it matches.** Enforcement: a test in the heal_pipeline_stall suite that, for each log-string trigger, asserts the live producer still emits a matching line (fixture cross-checked against the producer module).
- **Rule: advancer terminal-signal detection MUST reference only event types in `KNOWN_EVENT_TYPES`.** Enforcement: a regression test asserting `chain_event_says_failed`'s queried set is a subset of `chain_event_shipper.KNOWN_EVENT_TYPES`.
- **Rule: a stall backstop MUST treat recovery as resolved only after re-reading the stall condition** (not on a successful inbox write), and MUST emit a loud, non-suppressed operator alert when recovery does not clear the condition. Enforcement: the M4 recover-then-alert loop re-checks the predicate post-recovery; test covers the no-op-recovery case still alerting.
