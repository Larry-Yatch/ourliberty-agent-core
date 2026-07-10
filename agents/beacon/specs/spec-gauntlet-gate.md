# Spec: Antagonistic Spec-Review Gate ("Spec Gauntlet") — v2

**Status:** APPROVED by Larry 2026-07-10 (desktop session) — cleared for team build
**Destination:** `agents/beacon/specs/spec-gauntlet-gate.md`
**Decisions made by Larry:** (1) ship BLOCKING from day one — no report-only soak; revision rounds must be visible to him. (2) The headless `source:"larry"` path is permanently excluded — desktop-session specs already run this exact gauntlet interactively before dispatch.
**v2 note:** v1 of this spec itself went through a 3-lens antagonistic review (15 blocking + 8 advisory findings); every section marked ⚠ changed materially as a result — the v1→v2 deltas are documented inline in each section.

## 1. Problem

Beacon's specs reach Larry with zero technical review. Larry approves at CEO altitude ("does this make sense for the business") and cannot review technical correctness. Mirror adversarially reviews Forge's *code* against the spec, but nothing ever reviews the *spec*: a flawed spec produces a faithfully-built flaw that passes Mirror. The interactive loop Larry runs (draft spec → antagonistic reviewer fan-out → revise) almost always produces material changes. This gate codifies that proven loop.

Proven absent today: `git grep -iE 'antagonist|adversarial|spec[_ -]review' origin/main` → no code path between Beacon's marker and the approval stamp.

## 2. Shape (one paragraph) ⚠

The stamp sites do NOT run the review inline — v1's synchronous shape would have frozen both single-threaded host daemons (bot chat + all outbox traffic, Mirror verdict harvest included) for up to 75 minutes and blown the notifier's 512M cgroup with claude children. Instead, three cheap file-based hops:

```
stamp site: intercept(payload) → durable spool (~/agents/state/spec-review/pending/) → immediate chat ack → return
spec-review-runner (own systemd unit): spool → [3 lens reviewers ∥ → Beacon revision → re-review] → concluded/<task_id>.json
host daemon poll tick: sees concluded file → trust_decision → add_pending + approval_request chain_event (+ challenge digest) → Larry
```

The gate reviews `payload['prompt']` — the spec body that actually reaches Forge. I/O ownership is unchanged: the bot still sends every DM, the notifier still emits its events; the runner only reads/writes its own state dir and spawns reviewers.

## 3. Components

### 3.1 Intercept — `scripts/spec_review_gate.py` ⚠
- `intercept(payload, site) -> 'spooled' | 'disabled'`: writes `~/agents/state/spec-review/pending/<sanitized task_id>.json` `{payload, payload_hash, site, created_at}`, then the host sends an immediate ack on the chat path ("spec entering antagonistic review — approval card follows") so Larry knows in real time a gauntlet started. Pure file write — microseconds, no subprocess, safe inside the daemons' loops.
- **Gated sites, derived by payload provenance (Beacon-authored spec destined for Forge), not by add_pending grep** — v1's site list was factually wrong (two of four line numbers pointed at different functions; one would have blocking-gauntleted Mirror's one-line session-less decisions):
  1. Bot chat path (`beacon_telegram_bot.py` `_send_beacon_response`, both stamp branches) — **blocking**
  2. Replan path (`outbox_notifier._route_beacon_replan_approval`, ~10865) — **blocking**
  3. **Pulse auto-dispatch path (`outbox_notifier._route_beacon_pulse_auto_dispatch_approval`, add_pending ~11157) — blocking.** Missed entirely in v1; these are the *least*-human-reviewed specs in the system (can auto_approve to Forge with zero human contact), so they need the gauntlet most.
  - **Explicitly NOT gated:** `_emit_no_session_decision_approval` (~5807 — Mirror-verdict one-liners, not specs), `_propose_gated_finish_step` (~7638 — post-merge restart one-taps), and — **v1 scope cut** — the headless `source:"larry"` path (~11244): it never creates an approval card to attach findings to (it dispatches Forge directly), and those specs are Larry-authored via the interactive gauntlet already — decided exclusion, see §6.1.
- Deferred-stamp pickup: each host daemon's existing poll tick scans `~/agents/state/spec-review/concluded/` (cheap dir listing) and runs the *unchanged* legacy stamp path — `trust_decision` → `add_pending` → `build_approval_request_chain_event` — on the concluded payload, digest appended to the summary. Trust evaluates the FINAL (post-revision) payload.

### 3.2 Runner — `scripts/spec_review_runner.py` + `systemd/ourliberty-spec-review-runner.service` ⚠
New small daemon (or 1-min timer), because the reviewers cannot run inside the host daemons: the notifier's cgroup is `MemoryMax=512M` vs ~400MB/claude ×3, and the bot's sandbox has agent-core read-only (`ProtectSystem=strict`, no agent-core in `ReadWritePaths`) so it can't even create review worktrees. The runner unit gets its own limits sized for 3 claude children + `ReadWritePaths` for agent-core/worktrees, mirroring the notifier unit. Unit ships via the standard manifest so install-drift/self-heal cover it.
- **Concurrency:** each reviewer acquires a slot from the existing global `concurrency_guard` (hard cap 6 claude processes VM-wide) before spawning; if <3 slots free, lenses run serially rather than bypassing the guard or failing. One gauntlet in flight at a time (spool is a queue).
- **Idempotent + restart-safe:** every round's reviewer outputs archive to `~/agents/state/spec-review/archive/<task_id>-r<N>-<lens>.json` *as they complete*. On start, the runner scans `pending/`, consults the conclusion predicate + archive, and resumes from the last archived round — a daemon restart mid-gauntlet never loses the approval (v1 lost it: the payload lived only in bot memory until add_pending) and never re-runs completed rounds (the known notifier-restart dup-dispatch class).
- **Worktree:** reviewers fact-check against a runner-maintained fresh `origin/main` checkout (fetch per gauntlet, read-only to reviewers); per-gauntlet worktrees are avoided entirely to stay clear of the leaked-worktree/null-HEAD class.

### 3.3 Lenses & rounds — `review/spec-critique-lenses.md` ⚠
Vendored, read by absolute path (same rule as Mirror's bug-hunt lenses). Three distinct attack lenses:
- **S-A Feasibility & blast radius** — collisions with live daemons/healers/machine-owned files/systemd; rollback reality; consults the ourliberty-graph shelf librarian (advisory, fail-safe-skip when absent, same contract as Mirror Lens I).
- **S-B Completeness & failure modes** — unhappy paths, crash-mid-flight, restart/replay behavior, missing acceptance criteria.
- **S-C Reuse, simplicity & verifiability** — reinvention vs existing primitives, simpler shape, testable ACs, scope creep.

**Finding grammar reuses the established block-delimiter marker shape** (`=== SPEC_FINDINGS === {json} === END_SPEC_FINDINGS ===`), parsed with the `parse_mirror_marker`-style validate-or-reject structure — not v1's bare JSON. Findings: `{lens, severity: blocking|advisory, claim, spec_quote, suggested_change}`; no spec quote → finding discarded. Malformed reviewer output = "lens did not conclude" (fail-open, consistent everywhere).

**Round policy (fixed for correctness, not just termination):**
- **R1:** 3 lenses attack in parallel. No blocking findings → conclude `passed`, advisory findings ride the digest.
- **Revision (max 1):** gate-owned `claude --print` under `run_review_step.sh` — NOT an inbox/outbox Beacon session. v1's "Beacon headless session" was re-entrant: a revision emitting an APPROVAL_REQUEST marker through an outbox would re-enter the approval path and recurse into the gauntlet. Strict output contract: fenced revised-spec-body block + per-finding JSON responses (`accepted+changed` / `rejected: <reason>`); output is never written to any inbox/outbox, never parsed for APPROVAL_REQUEST, and gauntlet rounds are budget-neutral w.r.t. `replan_count`/`max_replans`. Malformed revision output = failed round, counts toward the cap.
- **R2 re-review:** same 3 lenses check BOTH "prior blocking findings resolved" AND "no new blocking flaw introduced by the revision diff" (v1 checked only the former — a revision could introduce a fresh fatal flaw and ship under an all-resolved digest, manufacturing false confidence). **No revised body ever ships without one full re-review pass over it.**
- R2 still has blocking findings → conclude `contested`, ship to Larry with those findings flagged. Never a third round.
- **Ceilings:** 900s per reviewer/revision step (`run_review_step.sh`, foreground, process-group kill); whole-gauntlet 75 min wall clock → conclude `incomplete`. Approval latency is real but off-thread: the bot ack is instant and the card lands when the gauntlet concludes.

### 3.4 Conclusion, spool hygiene, artifact write-back ⚠
- `scripts/spec_review_conclusion.py`: single "did this gauntlet conclude for (task_id, payload_hash)?" predicate. **Named consumers (v1 had none — it was cargo-culted armor):** the runner's resume-on-start scan, and a stale-spool sweep (folded into the runner: pending entries past the wall-clock ceiling with a dead runner get concluded `incomplete` and handed to the host for stamping — an interrupted gauntlet can delay an approval, never lose one).
- **Artifact write-back:** an accepted revision changes what Forge builds, so the committed spec must follow — `build_sequence_validator`/kickoff treat the repo-committed `spec_doc` as authoritative, and Larry's chat narrative pre-dates the revision. When the mission/payload carries a `spec_doc`, the runner writes the revised body back to that `agents/beacon/specs/*.md` file and commits it (runner is the sole committer of post-gauntlet revisions, per machine-owned single-committer rules); the digest Larry sees states "spec file updated by gauntlet revision." No `spec_doc` → payload is sole source of truth, digest says so.

### 3.5 Visibility (Larry's explicit requirement) ⚠
- Immediate chat ack when a gauntlet starts (§3.1).
- Per round: `spec_review_round` chain_event `{task_id, round, blocking_count, advisory_count, resolved_count, lens_verdicts, duration_s}`. **In-scope: register `spec_review_round` in `chain_event_shipper.KNOWN_EVENT_TYPES`** with per-round `id_extra` dedup — `chain_event_emit` silently drops unregistered types, which would have silently voided this entire section in v1.
- **Challenge digest on the approval card** (appended to summary): `Gauntlet: passed|contested|incomplete|errored · N rounds · X blocking → Y resolved, Z contested · W advisory`, one line per contested/advisory finding. **Every card carries exactly one terminal state while the gate is enabled** — `passed | contested | incomplete | errored` — so a missing digest is impossible and "errored" is legible, not silent. Kill switch off → card says `gauntlet: disabled` (one label; otherwise byte-identical legacy behavior).
- **Silent-failure gauge:** because errored/incomplete fail OPEN and emit no alerts, a persistent gate breakage (e.g. the HOME-swap absolute-path class) would otherwise become an invisible permanent no-op. A trailing gauge (existing measurer idiom) surfaces "N consecutive gauntlets ended errored/incomplete" to the needs-you feed. Info surface only — no DMs, per the alert default-deny north star.
- **v1 scope cut:** no new `dashboard_api.py` endpoint — the digest is on the card Larry already reads, rounds are queryable in chain_events, and the endpoint dragged unscoped frontend work in a second repo. Dashboard panel = separate slice if wanted.

### 3.6 Config & kill switch ⚠
Repo defaults in `config/spec-review.json` `{enabled, max_rounds, per_step_ceiling_s, wall_clock_ceiling_s, gated_sites}`; **live gate in `~/agents/spec-review.override.json`**, mirroring exactly the trust-policy override resolution — v1's "commit-free edit to a tracked config file" was the known sync-wedge + stale-config-cache double trap. The enabled flag is read FRESH on every `intercept()` call (no module-level cache); flipping the override takes effect on the next spec with zero daemon restarts (AC below).

### 3.7 Testability ⚠
Reviewer/revision invocation is injectable (`SPEC_REVIEW_CLAUDE_BIN`-style seam + injectable ceilings, same pattern as the bot's existing `CLAUDE_BIN` test seam). All routing ACs run against stub reviewers emitting fixture findings — the gate's state machine (blocking→revise, clean→pass, timeout→fail-open, resume, dedup) is what's tested deterministically; LLM finding *quality* is explicitly out of unit-test scope (v1's ACs required live LLMs to deterministically find seeded flaws — untestable in the credential-less regression gate).

## 4. Non-goals
- Headless `source:"larry"` path — permanently excluded (§3.1, §6.1).
- No changes to `trust_decision`, auto-approve semantics, Approvals-tab resolve fan-out, Forge build, or Mirror review.
- No dashboard frontend work.
- No Google-Doc review; the gate reviews `payload['prompt']`.

## 5. Acceptance criteria (all against stub reviewers unless noted) ⚠
1. Chat-path spec, stub emits a blocking finding → no `add_pending` until revision + re-review ran; card carries digest with the finding resolved.
2. Clean spec → concluded `passed`, 1 round, advisory findings on digest.
3. Kill switch: override `enabled:false` → next spec stamps immediately via legacy path, card labeled `gauntlet: disabled`, zero daemon restarts needed.
4. One stub reviewer sleeps past its ceiling → killed at 900s, gauntlet concludes, digest shows the non-concluding lens, stamp proceeds.
5. `kill -9` the runner mid-gauntlet → on restart, gauntlet resumes from last archived round (no round re-runs), approval reaches `add_pending`; total loss of the runner → stale-spool sweep concludes `incomplete` and the approval still lands.
6. Host daemon (bot/notifier) restart mid-gauntlet → no duplicate spool entry (payload_hash dedup), no lost approval.
7. Stub revision introduces a NEW blocking flaw → R2 flags it, card ships `contested` with the finding visible.
8. Gate crash (spool write ok, runner throws) → card ships `errored`; gauge increments.
9. Wall-clock breach → card ships `incomplete`.
10. Pulse-auto-dispatch site: spec routed through gauntlet before its `add_pending` (~11157).
11. `spec_review_round` rows land in chain_events (type registered, one row per round, dedup-stable).
12. Revision with `spec_doc` present → committed spec file matches the shipped payload body (write-back AC).
13. Mirror/Forge dispatch latency unaffected while a gauntlet runs (runner is out-of-band; notifier loop timing test).

## 6. Decision record
1. Headless `source:"larry"` path exclusion — **DECIDED by Larry 2026-07-10: excluded permanently, not just v1.** Specs pushed from desktop sessions have already been through the interactive gauntlet with Claude; re-running it would duplicate the step. No advisory variant planned.

## 7. Risks
- **New daemon to operate** → smallest possible surface (poll dir, spawn bounded children); ships via standard manifest + install-drift/staleness healers; restart-safe by construction (§3.2).
- **Noise churn** → quote-the-spec requirement, blocking/advisory split, 1-revision cap, R2 scoped but diff-aware.
- **Silent fail-open** → terminal-state-on-every-card + consecutive-failure gauge (§3.5).
- **Token cost** → 3–7 bounded headless runs per spec; still the cheapest place in the pipeline to catch a flaw (spec-time catch = 1 revision; Mirror-time catch = Forge build + review round + revision dispatch).
- **Latency** → card appears up to ~75 min after Beacon emits the marker; Larry gets an instant "entering review" ack; approvals are async so wall-clock cost is real but idle-time-shaped.
