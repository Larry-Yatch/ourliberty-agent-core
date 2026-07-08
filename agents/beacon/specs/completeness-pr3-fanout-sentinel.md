# Spec: PR-3 — terminal-event fan-out sentinel (completeness program, step 3)

Status: **v2 — BUILD-READY** (2026-07-08). v1 survived a 2-lens adversarial review (blast-radius attack: 11 findings; substrate/build-readiness: 4 blockers + 7 clarifies, all verified live). v2 applies every surviving finding. Parent: `completeness-architecture-design.md` §2-PR-3. Launch AFTER the PR-1→PR-2 sequence completes (review-load; PR-1's ledger records this sentinel's escalation outcomes).

## 0. What v2 changed (read first — it reshaped the component)

- **The sequence-step leg is GONE from the fan-out.** v1's "the advancer's dual-gate consumes the closing event" was verified false — the advancer reads only `auto_merge` events; my closing event would be invisible, and an out-of-band merge would still trip its 30-min gate-mismatch page. The advancer already self-heals MERGED steps via its own gh reconcile pass, and PR-2's post-PR-open timeout covers wedged steps. The sentinel does not touch sequences. (Also: live data shows the v1 filter matched zero real steps — the status enum was wrong.)
- **The mission leg is enumeration-only, tightly scoped.** Missions carry no PR coordinates (0 of 268) and 239 are `proposed` — v1 would have burned the gh budget probing ~240 unresolvable items and then paged Larry to "declare dead" healthy work. v2: enumerate ONLY `drafting`/`in_flight`/`ready` phases (~9–11 live), "resolves to a PR" = exact task_id join onto chain_events rows carrying `pr_url`; unresolvable → **skipped, never ledgered**; sequence-owned missions (synthetic `seq-*` task_ids) excluded from no-match aging entirely (`cause=sequence-owned`, never escalates). Mission *closes* stay owned by `heal_missions_card_gc` (single-committer discipline) — the sentinel never writes mission state.
- **What remains is coherent and small:** ONE probe pass over the genuinely PR-backed surfaces (in-review lane + needs-Larry records + pr_url-stamped mission task_ids), ONE emit + fan-out on identity-grade evidence, the UNKNOWN/declare-dead ledger, the parity metric, report-only rollout, and two riders (G7 delta-age; card-gc CLOSED-semantics fix). Live identity-grade volume is tiny (~5 items today) — that's fine; out-of-band terminals are a burst phenomenon and the sentinel is cheap.

## 1. Component + placement

`scripts/pr_terminal_fanout.py`. Unit shape from `dispatch_sentinel`'s service (incl. `EnvironmentFile=/home/larry/credentials/.env.larry` — needed for the Supabase creds the derivation reuse requires); **timer idiom `OnCalendar`** every 15 min (NOT `OnUnitActiveSec` — the documented dies-when-stopped lesson; copy `ourliberty-heal-stale-in-review-reconcile.timer`). `healers.disabled` kill-switch; EMERGENCY_HALT ConditionPathExists; heartbeat `~/agents/blackboard/pr-terminal-fanout.heartbeat` every run; per-pass gh probe cap **15** (the in-review healer's precedent). Conservative posture: UNKNOWN/ambiguous ⇒ KEEP.

## 2. Enumeration (verified sources; `item_key` scheme is store-prefixed)

`item_key`: `inreview:<task_id>` · `decision:<canonical_decision_key>` (via `decision_identity`) · `mission:<id>` · `obligation:<task_id>`.
1. **In-review lane** — reuse the dashboard derivation exactly as `heal_stale_in_review_reconcile.py` does (sys.path insert + lazy `import dashboard_api`; verified working standalone): `_get_larry_action_supabase_client` → `_fetch_chain_events_for_agent` **twice** (`agent='forge'` AND `agent='mirror'` verdict rows) → `_derive_in_review`. **A `None` fetch skips the tick — never derive on a failed verdict fetch** (deriving without verdicts resurrects phantoms).
2. **Needs-Larry records carrying a PR coordinate/pr_url** — P `~/agents/state/beacon-pending-approvals.json` (rows carry `id` = entry_id), C chain_events (Larry-facing event types with `read_at IS NULL` AND `pr_url` set — write the exact query at build), E `~/agents/blackboard/for-larry-escalations.json`, A `~/agents/blackboard/larry-alerts.jsonl` (only lines carrying `decision_key`/pr_url).
3. **Missions** — repo copy `agent-core/agents/beacon/missions.json`, phases `drafting/in_flight/ready` only; task_id → pr_url via exact chain_events join; unresolvable skipped.
4. **Obligation ledgers** (`~/agents/state/no-session-revision-ledger.json`, the rebase ledger via `outbox_notifier.py:87`) — enumerate for the UNKNOWN ledger ONLY; their own resolvers own closure.

## 3. Terminal evidence — grades + the four false-terminal guards

Probe: `gh pr list --repo <R> --head <branch> --state all` over `task_terminal_state.DEFAULT_REPOS`, plus PR-coordinate lookups. Grades:
- **Identity-grade** (fan-out permitted): exact recorded PR coordinate `pr-<repo>-<n>`, **provenance-checked** — honored only if stamped by the dispatch/PR-open path, OR cross-checked `headRefName == forge/<task_id>` exactly (a coordinate written by earlier search-grade reconciliation must not launder into fan-out authority). Or exact-branch match. Healer-minted `-retryN`/truncated branch variants fail exact-match **conservatively** — acceptable coverage loss.
- **Search-grade** (fan-out FORBIDDEN): anything from task-id search/variant matching. May retire only the single originating record, as today's healers do.

**Guards — ALL required before any fan-out (each defeats a verified false-terminal path):**
1. **OPEN-beats-terminal over the full PR set:** enumerate ALL PRs on the coordinate/branch and reduce with `task_terminal_state._combine` precedence (OPEN > MERGED > CLOSED). Defeats branch-reuse-across-redispatch (the redispatch healer deliberately re-queues on the SAME branch, and its own candidate rule lets a CLOSED PR sit on a live re-attempt's branch) and the one-task-two-PRs case.
2. **Temporal anchor:** terminal ts strictly AFTER the item's last dispatch/activity ts (`since`/`dispatched_at`) — the template healer's load-bearing guard v1 omitted.
3. **Liveness:** skip any item with a live in-flight claim (`state/in-flight/<task>.json`, signalable pid).
4. **MERGED ancestry:** `git merge-base --is-ancestor <headSha> origin/main` (or mergeCommit reachable from default branch). Failure → UNKNOWN `cause=ambiguous` + one escalation, never fan-out. Defeats the documented stacked-PR-orphan (MERGED while content never landed).
5. **CLOSED settle:** `closedAt` older than one full sentinel period AND observed CLOSED on two consecutive passes (close→reopen gap). MERGED (ancestry-verified) needs no settle. **Re-confirm state immediately before acting** (TOCTOU).

## 4. The fan-out close (identity-grade only) — with outcome semantics

The closing action carries `terminal ∈ {MERGED, CLOSED}` + `ancestry_verified`; surfaces map outcomes, never bulldoze them:
1. **Emit `review_obsolete`** (verified reusable: `agent='forge'`, deterministic event_id, payload `{pr_state, reason, source:'pr-terminal-fanout'}`; `_derive_in_review` only builds cards from `review_request` rows, so a stray emit for a never-reviewed task is ignored; already registered in `_QUEUE_TERMINAL_EVENT_TYPES` + shipper `KNOWN_EVENT_TYPES`, deliberately absent from done_today). Note: `review_obsolete` rows aren't in `chain-events-retention.json` bookkeeping types — pre-existing latent growth, note only, don't fix here.
2. **Clear needs-Larry surfaces via `decision_resolve`** with the acted-on `entry_id` where the record carries one (P-leg is entry_id-exact by design; without it, that surface stays with its healer backstop — say so in the artifact). **Outcome mapping:** MERGED → `approved` (out-of-band); CLOSED → `expired`. Never `approved` for abandoned work — the decision ledger's analytics depend on the distinction.
3. **Mission surface: no direct action** (single-committer; GC owns it — see rider R2).
**Ordering + partial-failure contract:** emit event → resolve legs → verify per-surface consumption → **cache LAST**. Re-runs re-execute all legs (each individually idempotent — that's what the deterministic event_id and decision_resolve's guarded clears are for). Acceptance kills the process mid-fan-out and asserts the next run completes it.

## 5. UNKNOWN ledger + caches

`~/agents/state/pr-fanout-unknown-ledger.json`: `{item_key, cause ∈ no-match|probe-error|ambiguous|sequence-owned, first_seen, consecutive_count, last_probe}` — bounded (drop rows whose item left enumeration).
- `no-match` ≥14d AND ≥3 consecutive clean probes → ONE approval: "can't find this work anywhere — declare dead / keep?" `sequence-owned` NEVER escalates.
- `probe-error` → per-item backoff + ONE aggregated probe-health signal when >20% of a pass errors.
- Terminal cache `~/agents/state/pr-fanout-terminal-cache.json`: **ancestry-verified MERGED cached permanently**; CLOSED entries keyed to `(item_key, closedAt, item.since)` and re-verified once after 24h (reopen recovery). Bounded: prune entries older than 26 weeks.

## 6. Self-watching

Per-pass artifact `~/agents/blackboard/pr-terminal-fanout/last-pass.json`: enumerated/probed/closed by grade+terminal-type, UNKNOWN by cause, parity records (§7). Per-store try/except (one malformed store never unwatches the rest). Heartbeat-watcher: copy `heal_chain_event_shipper_heartbeat.py` (the plain shape — NOT the advancer watcher, whose default-OFF probe logic doesn't apply), >45 min stale → DM. **Death-alarm hardcoded:** `route='escalate'` at emit + a `never_silence: true` entry in `alert-translations.json` under the exact emitting `source` string — note this is the config's FIRST never_silence entry (mechanism verified in classify() Gate 1: falls through to tier-4 escalate with translation intact).

## 7. Parity metric (the future-demote gate — defined so it isn't noise)

A backstop "miss" counts ONLY when: `gh_terminal_ts + one full sentinel period + 5min slack < healer_close_ts` AND the item appeared in a completed sentinel pass's enumeration after `gh_terminal_ts`. (Backstops at 10-min cadence will often win the race legitimately — a lost race is NOT a miss.) Per-backstop join sources, named: in-review healer → chain_events rows with `payload.source='heal-stale-in-review-reconcile'`; approvals → beacon history `resolved_at`; escalations (E) → timestamp-inference only (rows lack resolved_by and are pruned — accepted limitation, noted in artifact); board reconcile → mission audit_log. No log-scraping.

## 8. Rollout: report-only first (non-negotiable)

First **48h / minimum 4 passes**: full would-close artifact (item, evidence grade, terminal type, ancestry, guard results) — **zero writes** — diffed against what the backstops actually close over the same window. (The template healer ships dry-run-by-default with `--apply`; the sentinel copies that posture.)

**Window-complete arming approval (self-firing — arming MUST NOT rely on a human remembering to flip a flag):** on the first pass where the report-only window is complete (>=48h elapsed since the first recorded pass AND >=4 passes recorded), the sentinel ITSELF emits exactly ONE arming approval to Larry — `"PR-terminal fan-out sentinel finished its 48h report-only window: N would-closes, of which M diverged from what the backstops actually closed. Arm fan-out? (approve -> switch to live fan-out; reject -> stay report-only)."` The M (would-have-been-wrong) count is the sec-8 arming signal, surfaced so the decision is evidence-backed: if M > 0 the body recommends AGAINST arming, but Larry still decides. Idempotency: the emit is gated by a durable `arming_requested` flag in the sentinel's state so re-runs after the window do NOT re-emit; the single approval sits durably in the Approvals queue until acted on (the queue IS the reminder — no silent dependence on memory, no 15-min spam). Approve flips a durable `armed` flag (persisted in sentinel state / the arm config the passes read); reject records the reason and the sentinel stays report-only (a later re-arm is a deliberate operator action, not an automatic re-ask). Until `armed` is true, every pass stays artifact-only. **Enforcement:** the `arming_requested` durable flag (one-shot emit) plus the `armed` durable flag gating every fan-out write; acceptance sec-10.10.

**Post-arming first pass:** resulting approvals/digests batched into ONE grouped first-run digest.

## 9. Riders (both small, both "closes mean the right thing")

- **R1 — G7 delta-age:** schema extension to `config/healer-managed-runtime-paths.json` (`{path, committer, cadence_min}` entries: missions/captures → heal_missions_card_gc @10min; projects.json → heal_projects_store @10min); code seam = `evaluate_uncommitted` in `heal_droplet_git_drift.py`. Age = time since **first-observed-dirty** (small state file — NOT newest-mtime, which a busy writer keeps fresh exactly when the committer is dead); threshold `max(3× cadence_min, 45min)`; suppressed during the deploy grace window. One escalation naming the wedged committer.
- **R2 — card-gc CLOSED semantics:** `heal_missions_card_gc` currently flips a mission to **`shipped` when every task_id is terminal including CLOSED-unmerged** — abandoned work recorded as shipped (verified in its docstring; pre-existing bug surfaced by this review). Fix: all-terminal-but-any-CLOSED-unmerged → `retired` + one needs-attention surface, never `shipped`. (Phase-aware Drop already distinguishes Done→retired downstream.)

## 10. Acceptance

1. Unit/timer installed (dispatch_sentinel service shape + OnCalendar timer); heartbeat + watcher live; kill-switch + EMERGENCY_HALT honored; probe cap enforced.
2. Guard tests: each of §3's five guards has a test proving its false-terminal path cannot fan-out (branch-reuse w/ live re-attempt; stale coordinate w/ newer OPEN PR; pre-activity terminal ts; orphaned MERGED; fresh CLOSED). Search-grade fan-out proven unreachable.
3. Partial-failure: process killed between emit and resolve legs → next run completes; cache written last; re-run produces no duplicate events/approvals.
4. decision_resolve legs: real record WITH entry_id clears all four surfaces with correct outcome mapping (MERGED→approved, CLOSED→expired); record WITHOUT entry_id leaves P untouched + artifact notes delegation.
5. Single-writer: sentinel's write set provably excludes missions.json/projects.json/captures.json/build-sequences (test greps writes).
6. UNKNOWN ledger: no-match aging → exactly one approval; sequence-owned never escalates; probe-error storm (mocked gh) → backoff + one health signal, heartbeat fires, exit 0; caches bounded.
7. Parity join: seeded true-miss detected; seeded lost-race NOT counted as miss.
8. R1: simulated wedged committer under an active writer fires exactly one escalation (first-observed-dirty, not mtime); nominal slow tick (15–20min) stays silent.
9. R2: mission with one CLOSED-unmerged task_id → `retired` + surface, not `shipped`; all-MERGED → `shipped` unchanged.
10. Report-only mode + self-firing arming verified: (a) 48h/>=4-pass artifact-only run writes zero live-tree state; (b) on the first post-window pass the sentinel emits EXACTLY ONE arming approval carrying the N would-close / M would-have-been-wrong counts, gated by a durable `arming_requested` flag so re-runs do not re-emit; (c) approve flips the durable `armed` flag and subsequent passes fan-out for real; reject leaves it report-only; (d) no pass performs a fan-out write while `armed` is false. Arming never depends on a human remembering to flip a flag.
11. unittest (not pytest), sentinel-armed, zero live-tree writes from tests.

## 11. Out of scope

Sequence-step closes (advancer + PR-2 own that surface) · retiring/demoting any healer (metric-gated future decision, §7 feeds it) · non-PR work-types (parked observer) · the router default-flip · the 352-ask sort-once cleanup · fixing review_obsolete retention (noted only).
