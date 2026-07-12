# Spec: Notifier auto-retraction — recurring detectors clear their own reds

**Status:** Slice 1 shipped (mechanism + 2 pilots); Slice 2 shipping (classification audit §7 + 5 single-subject adopters); Slice 3 planned
**Author:** Claude-as-Forge (dispatched by Beacon, `notifier-auto-retraction-slice1-001`)
**Related:** `heal_systemd_install_drift.py` (the retraction exemplar); `alert-pipeline-rework.md` (severity→route model this reuses)

---

## 1. Problem statement

The `larry-alerts.jsonl` queue is append-only: it has no retraction primitive.
When a recurring detector (a healer / sentinel) emits a 🔴 escalate line and the
underlying condition later resolves out-of-band, the original red stays in the
queue until 14-day retention prunes it. Today exactly **one** of ~40 recurring
detectors — `heal_systemd_install_drift` — retracts its own red when it observes
the condition clear. Every other resolved issue can leave a lingering 🔴 in the
feed for up to two weeks, eroding the signal value of the queue.

The retraction machinery already exists (`larry_alerts.resolve_alert`); what is
missing is (a) an *auditable* wrapper so a retraction of something Larry saw is
itself visible, and (b) adoption across the detectors whose clear-state is a
safe, positive observation.

## 2. The three-slice program

### Slice 1 — mechanism + 2 pilots (THIS PR)

- New helper `larry_alerts.retract_with_standdown(key, standdown_message, subject=None) -> int`
  (see §3). Makes retraction auditable and never silent.
- Pilot adoption on two heartbeat healers whose clear-state is a POSITIVE
  observation (a recent timestamp): `heal_chain_event_shipper_heartbeat.py` and
  `heal_build_sequence_advancer_heartbeat.py`. Each calls the helper on its
  positively-fresh branch (`reason == 'fresh'`), keyed on its own
  `source:subject`.
- This canonical spec doc (the reference for slices 2–3).

### Slice 2 — classification audit + expansion (separate dispatch)

Audit all ~40 recurring detectors and classify each as:

- **Retractable, single-subject shape** — one detector, one subject; clear-state
  is a single positive observation (like the two pilots). Expand to this batch.
- **Retractable, set-diff shape** — the clear-state is a *set difference* over a
  live inventory (the `heal_systemd_install_drift` `live_set` pattern: a unit
  left the drift set). Needs the per-member diff loop; deferred within slice 2.
- **One-shot / non-retractable** — fires once on an irreversible or
  point-in-time event with no recurring "now-clear" observation. Excluded.

Then adopt the helper across the structurally-safe single-subject detectors.

### Slice 3 — confidence → severity threading (separable, deferrable)

Thread each detector's detection confidence into its `severity` argument so a
low-confidence detection routes to the digest lane instead of escalate.
`append_alert(..., severity=, route=)` **already** implements the routing
(`severity == 'info'` defaults to digest; `critical` forces escalate) — so slice
3 is an ADOPTION task per detector, not a missing mechanism. Slice 1 does **not**
touch severity routing.

## 3. The helper (slice 1)

```python
retract_with_standdown(key, standdown_message, subject=None) -> int
```

Beside `resolve_alert` in `scripts/larry_alerts.py`. Behavior:

1. Calls `resolve_alert(key)` (removes pending escalate line(s) whose cooldown
   key == `key`, under the queue flock, keeping beacon+medic cursors consistent
   and clearing the shipped `chain_event` rows).
2. **Only when** `resolve_alert` removed ≥ 1 line — proof a real 🔴 had been
   delivered — appends exactly ONE closure stand-down line via
   `append_alert(severity='info', route='closure', ...)`.
3. On a 0-removal no-match, appends nothing and returns 0.
4. Never raises (fire-and-forget, matching `resolve_alert` / `append_alert`).

This generalizes the `heal_systemd_install_drift` exemplar
(`_resolve_install_alert` → retract, then a closure DM gated on `removed`) so any
positive-clear detector can adopt one call.

## 4. Guardrails (the review contract)

- **POSITIVE-CLEAR ONLY.** A detector retracts solely on a positively-observed
  resolved condition — a fresh timestamp, a member leaving a drift set — NEVER on
  absence-of-signal or a degraded / unreadable probe. This inverts
  `heal_pipeline_stall`'s "unreadable == non-terminal == still alertable"
  degrade-safe posture for the clear side. In the pilots this is enforced by
  gating the retract on `reason == 'fresh'`: `check_staleness()` returns
  `is_stale=True` on every error path, so a degraded read can never reach the
  retract call, and the positive sentinel makes the invariant local + testable.
- **AUDITABLE, NEVER SILENT.** Every retraction that actually removed a pending
  red emits a closure stand-down line, so a wrongful retraction surfaces as a
  disputable closure DM rather than a silently vanished red.

## 5. Acceptance criteria (slice 1)

- `retract_with_standdown`: returns 0 and appends nothing on no-match; on a
  seeded pending escalate line matching the key, removes it and appends exactly
  one closure line; never raises.
- Each pilot healer: (a) a positively-fresh tick retracts a seeded stale alert
  for its key; (b) a probe-error / degraded-read tick does NOT call retract
  (REQUIRED, not optional).
- Existing behavior unchanged: the stale-emit branches of both pilots are
  untouched; the full suite stays green.

## 6. Out of scope for slice 1

- Any detector beyond the two named pilots (slice 2, gated on the audit).
- Confidence → severity threading (slice 3).
- Any change to `resolve_alert`'s semantics, the cooldown/route model, or the
  digest pipeline.
- Set-diff-shaped detectors (the `live_set` diff pattern) — deferred to slice 2.

---

## 7. Slice 2 — classification audit (the durable deliverable)

Every recurring detector in `scripts/` (42 `heal_*.py` + the `dispatch_sentinel.py`
sentinel = **43 total**) is classified below into one of the three shapes from §2,
plus the two exclusion buckets. This audit is the reference slice 3 uses to know
which detectors carry a per-detection *confidence* signal worth threading into
`severity` — that set is exactly the **single-subject retractable** rows (adopted
+ deferred), because a positive-clear observation is also the natural place a
confidence score lives.

Classification was grounded factually: a detector that never calls
`larry_alerts.append_alert` cannot emit a red and is therefore non-retractable by
construction (verified by grep, 2026-07-11).

### 7.1 Already retracting (baseline — untouched by slice 2)

| Detector | Shape | Note |
|---|---|---|
| `heal_chain_event_shipper_heartbeat` | single-subject | Slice-1 pilot |
| `heal_build_sequence_advancer_heartbeat` | single-subject | Slice-1 pilot |
| `heal_systemd_install_drift` | set-diff | The `live_set` exemplar §4 generalizes |

### 7.2 Retractable, single-subject — ADOPTED in slice 2 (this PR)

Each retracts on a positive-clear branch gated by a sentinel unreachable by a
degraded read, keyed on its own `source:subject`, with a REQUIRED
degraded-does-not-retract test.

| Detector | Positive-clear observation | Alert subject(s) retracted |
|---|---|---|
| `heal_pr_terminal_fanout_heartbeat` | `reason == 'fresh'` (recent heartbeat mtime) | `pr-terminal-fanout-stale` |
| `heal_tier2_weekly_health_probe` | `ok is True` (PROBE_OK, exit 0) | `tier2_weekly_probe_failed` |
| `heal_dashboard_api_sha_drift` | PROBE_OK **and** running SHA == on-disk HEAD (`'fresh'`) | `dashboard-api-sha-drift-stuck`, `dashboard-api-sha-drift-restart-failed` |
| `heal_daemon_restart_manifest_drift` | `not drift.has_drift` (committed manifest == live closure) | `wrong-branch`, `commit-failed`, `push-failed`, `write-failed` |
| `heal_chain_event_type_audit` | successful connect+query+classify **and** zero unknown types | `chain-event-type-audit` |

Note two of these clear more than one subject from a single positive observation
(dashboard-api, daemon-restart) — a single-detector-multi-red shape, still
single-*observation* and so structurally safe, distinct from the set-diff shape
(which diffs a live inventory).

### 7.3 Retractable, single-subject — DEFERRED (structural hazard)

| Detector | Why deferred |
|---|---|
| `heal_claude_max_burn_rate` | **Degrade-to-zero hazard.** `rolling_5h_token_volume` returns `0` on a missing/unreadable `costs.jsonl`, so the "under threshold" clear branch (pct low) is *reachable by a degraded read* — it violates POSITIVE-CLEAR ONLY without an extra existence/success guard. Adopt only after adding that guard (a follow-up, not slice 2). |

### 7.4 Retractable, set-diff — DEFERRED to a later slice (per §2/§6)

The clear-state is a *set difference* over a live inventory; adoption needs the
per-member diff loop (the `heal_systemd_install_drift` pattern), out of scope here.

`heal_pipeline_stall`, `heal_pulse_check_staleness`, `heal_stale_daemon_code`,
`heal_undispatched_pr_review`, `heal_wedged_review_sessions`,
`heal_claude_json_bind_drift`, `heal_credential_registry_drift`,
`heal_droplet_git_drift`, `dispatch_sentinel`.

### 7.5 One-shot / non-retractable — EXCLUDED

Emit a red on an irreversible or point-in-time event; there is no recurring
"now-clear" observation to key a retraction on.

`heal_forge_wip_only_redispatch`, `heal_missions_card_gc`,
`heal_orphan_autoregister`, `heal_phantom_dispatch_claim`, `heal_pr_auto_merge`,
`heal_resume_paused_on_tier1`, `heal_unreviewed_merge_detector`.

### 7.6 Non-alerting / silent — EXCLUDED (cannot emit a red)

No `larry_alerts.append_alert` red to retract: reconcilers, GC, board-drains, and
digest/tab-registration-only detectors.

- **Zero `append_alert` (16):** `heal_abandoned_inbox_tasks`,
  `heal_blocked_inbox_age`, `heal_completed_sequence_mission_reconcile`,
  `heal_empty_inbox_files`, `heal_merged_pr_board_reconcile`,
  `heal_missions_board_drain`, `heal_orphaned_mirror_claims`,
  `heal_projects_store`, `heal_recovery_already_merged`,
  `heal_restart_dedup_obsolete`, `heal_silent_loop_death`,
  `heal_stale_alert_triage`, `heal_stale_approvals`,
  `heal_stale_in_review_reconcile`, `heal_stale_pr_escalations`,
  `heal_zombie_main_workers`.
- **Non-red alert lanes (2):** `heal_review_ceiling_fit` (digest-only),
  `heal_unregistered_approval` (registers approval-tab cards, not an escalate red).

### 7.7 Tally

3 already-retracting + 5 adopted + 1 single-subject-deferred + 9 set-diff-deferred
+ 7 one-shot-excluded + 18 non-alerting-excluded = **43**.
