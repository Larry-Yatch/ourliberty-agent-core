# Pulse — Long-term Memory

*Distilled wisdom carried across cycles. The cycle-journal is the chronological record; this file is the curated essence — patterns I've internalized, calibration notes, things to keep in mind.*

*Keep under 15,000 characters. Above 18,000 = condense.*

---

## Check I firing days are Mon/Wed/Fri/Sun — call WITHOUT --force on firing days (learned 2026-06-15 iter ~1899, updated iter ~2612)

**Rule:** Check I fires on Mon/Wed/Fri/Sun per spec (UTC weekday ∈ {0,2,4,6}). Always invoke `python3 ~/agent-core/scripts/pulse_check_i.py` (no `--force`) on scheduled firing days — the weekday gate passes naturally and the dm_route journal-peek (PR #674) functions correctly, suppressing repeat same-week DMs. Use `--force` ONLY for `/optimize` (ad-hoc, any day). Using `--force` on a firing day bypasses dm_route and emits spurious route=escalate alerts (G-rule check-i-force-bypass-dm-route). Confirmed fixed at manual level iter ~2612; code-level dispatch to Beacon at 3/3.

---

## Dispatch routing rule (learned 2026-06-12 — routing rejection)

**Rule:** Pulse may ONLY dispatch to **Beacon**. The dispatch_validator enforces `allowed from pulse: ['beacon']`. Pulse → Forge dispatches are REJECTED and dead-lettered to `.invalid/`. The correct path for code fixes is always: Pulse direction-ask → Beacon → Forge build brief. When writing a dispatch envelope, set `target_agent: beacon` (not `forge`), and phrase the prompt as a direction-ask to Beacon asking it to spec + dispatch Forge.

---

## beacon-pending-approvals.json correct path (learned 2026-06-12 — 5 consecutive false positives)

**Rule:** `beacon-pending-approvals.json` lives at `~/agents/state/beacon-pending-approvals.json`. NOT `~/agents/blackboard/`. File is not referenced in cycle-prompt.md — check is informal (Pulse reads it as part of Check 4 / pending-directives scan). Always use `~/agents/state/beacon-pending-approvals.json`.

---

## Dispatch envelope schema (learned 2026-06-11, two failures)

**Rule:** Beacon inbox dispatch envelopes MUST use root field `task_id` (not `envelope_id`). Required fields: `task_id`, `source`, `dedup_identity`, `prompt`, `timeout`. `envelope_id` is silently ignored and fails the validator. `timeout` MUST be an integer (seconds), in range [60, 14400] — string durations like `"48h"` are rejected with `out of bounds` error. (learned 2026-06-14 from dead-letter on unreviewed-merge-missions-no-mirror-001)

---

## approval_request alerts in larry-alerts.jsonl (learned 2026-06-12)

**Rule:** `kind=approval_request` entries in larry-alerts.jsonl are DELIVERY CONFIRMATIONS from outbox-notifier, not new tasks for Pulse. Outbox-notifier already sent the Telegram DM. Pulse should claim + triage these (Tier-4 in absence of a registry template) but NOT send a second DM to Larry. Journal-note only. See iter ~1604.

---

## cycle_prime_ledger.py correct CLI (learned 2026-06-12)

**Rule:** Valid subcommands are `ratio`, `append`, `promote`. NOT `summary`. For appending: `--tier {1,2,3} --kind {intervention,systemic_fix,verification_pending,iter_clean} --template <kebab-case> --detail <free-text>`.

---

## systemctl --user false-negative (learned 2026-06-13 iter ~1676)

**Rule:** `systemctl is-active <service>` without `--user` returns "inactive" for user-scoped services when run from an interactive non-D-Bus session (e.g., `systemctl --user` fails with "No medium found"). Always verify daemon liveness via `ps -p <PID>` or `ps -p <PID1>,<PID2>,...` with comma-separated list. The comma-separated form is required; space-separated PIDs after `-p` produce exit-code 1 with no output.

---

## outbox_notifier url-shape-invalid gap (learned 2026-06-13 iter ~1674)

**Rule:** outbox_notifier's PR URL shape validator rejects repos not in its recognized-list. ourliberty-graph was not recognized despite being added to allowed_repos and systemd RWP. Symptom: `WARN MIRROR_REVIEW_STATUS … skipped reason=pr-url-shape-invalid (shape-mismatch)` followed by `WARN AUTO_MERGE … outcome=skipped reason=pr-url-shape-invalid`. Recovery: `gh pr merge <num> --repo Larry-Yatch/<repo> --squash --delete-branch` after verifying Mirror outbox archive shows REVIEW_PASS. Systemic fix: PR #493 merged 2026-06-13 21:12Z — allowlist now sourced from agent-models config. RESOLVED.

---

## alert_triage_state.py set-watermark correct syntax (learned 2026-06-14 iter ~1845)

**Rule:** `alert_triage_state.py set-watermark` requires `--line <N>` (named argument), NOT a positional argument. Usage: `python3 scripts/alert_triage_state.py set-watermark --line 931`. Positional form fails with "the following arguments are required: --line".

---

## Alert watermark persistence gap (learned 2026-06-14 iter ~1703)

**Rule:** In interactive `/cycle` sessions, `alert_triage_state.py set-watermark` is called by Pulse's journal narrative but NOT always committed before session end. On next iter, get-watermark returns the pre-session value (e.g., 982 instead of expected 984). Check the watermark at start of each iter and advance it if the lines in question have already been triaged (Tier-3/nominal). Do NOT re-triage — just confirm against prior journal and advance. This is structural: interactive sessions may not persist watermark if Pulse exits before the explicit set-watermark step.

---

## larry-alerts.jsonl correct path (learned 2026-06-14 iter ~1741)

**Rule:** `larry-alerts.jsonl` lives at `/home/larry/agents/blackboard/larry-alerts.jsonl`. NOT `/home/larry/agents/logs/`. Confirmed by `ls /home/larry/agents/blackboard/larry-alerts*`.

---

## heal-stale-daemon-code heartbeat correct path and format (corrected iter ~1768, format confirmed ~1829)

**Rule:** The heal-stale-daemon-code heartbeat lives at `/home/larry/agents/blackboard/heal-stale-daemon-code.heartbeat` (NOT `state/`). This is `HEARTBEAT_FILE` in `scripts/heal_stale_daemon_code.py`. Previous Check 5 invocations used `state/` path and would have gotten "no file" — always use `blackboard/` path. The file contains a **plain-text ISO 8601 UTC timestamp** (e.g. `2026-06-14T20:39:19.896028+00:00`), NOT JSON — read with `cat`, not `json.load`. Parse timestamp directly to compute age.

---

## Check 0 must call helper before manual classification (learned 2026-06-14 iter ~1812)

**Rule:** Before manually classifying an alert as Tier-4, Pulse MUST call `python3 scripts/alert_triage_state.py triage-alert --alert-id "<id>" --alert '<json>' --iter <N>` and act on the returned tier. If the helper returns Tier-3 (silence/known-pattern match), that result is authoritative — do NOT override it with in-prompt manual classification. The helper handles `kind`-only alerts (no `subject` field) via fallback logic in `_translation_match` that Pulse's in-prompt subject-keyed lookup misses. PR #491 (merged 2026-06-13) already added `outbox-notifier → approval_request` Tier-3 silence to `config/alert-translations.json`; multiple Tier-4 mis-classifications before and after that PR were Pulse bypassing the helper.

---

## beacon_telegram_bot.py get-messages MUST NEVER BE CALLED (learned iter ~1876, escalated iter ~1943)

**Rule:** NEVER call `beacon_telegram_bot.py get-messages` in ANY form — not with `run_in_background=true`, not in foreground, not with `| head -N` truncation. The Bash tool may auto-background blocking commands regardless of the run_in_background parameter, causing the same 409 conflict. The competing getUpdates loop causes HTTP 409 conflicts with the production bot, disrupting message receipt. For Telegram sweeps (Check 2), use ONLY: `tail -N /home/larry/agents/logs/beacon_telegram_bot.log` (note: NOT beacon-telegram-bot.log) + `ps -p <PID> -o stat` for the bot health check. This is the only safe Telegram check pattern. G-rule telegram-409-burst at **2/3** as of iter ~1943 — all three incidents were self-inflicted by calling get-messages.

---

## beacon-pending-approvals.json correct structure (corrected iter ~1878)

**Rule:** `beacon-pending-approvals.json` structure is `{"version": 1, "pending": [...], "history": [...]}` — NOT a dict keyed by approval ID. Check for pending items via `d["pending"]` list. Prior parsing (looking for `.items()` with a `status` field) was wrong and returned 0 pending incorrectly. Correct check: `len(d.get("pending", []))`.

---

## medic-diagnosis-tier4 G-rule COMPLETE ✅ (iter ~1955 dispatch, iter ~1969 verified)

**Rule:** `source=medic, intent=medic-diagnosis` alerts now classify Tier-3 (silenced, route=digest) per translation in `config/alert-translations.json`. PR #515 (`forge/medic-diagnosis-tier3-silence-001`) merged 2026-06-15T17:27:41Z. **G-rule COMPLETE.** No DM from Pulse warranted — medic already DMs directly via chat_id.

---

## Ledger/Check-I Tier-4 → COMPLETE ✅ (iter ~2316 dispatch, iter ~2347 PRIME verified)

**Rule:** `source=ledger` weekly reports (subject=weekly-YYYY-MM-DD) and `source=pulse` Check I digests (subject=check-i-YYYY-MM-DD) now classify Tier-3 (digest route) via trailing-ISO-date-strip step in `config/alert-translations.json`. PR #604 (`silence-routine-weekly-alerts-tier3-001`) merged 2026-06-20T14:21Z after Larry approved ('Go' at 08:04 MDT). G-rule COMPLETE. PRIME verified iter ~2347.

---

## auto-dispatch-APPROVAL_REQUEST-task-id-mismatch (observed iter ~1910)

**Rule:** When Pulse sends a Check I auto-dispatch envelope, outbox-notifier WARNs `beacon pulse-auto-dispatch APPROVAL_REQUEST task_id mismatch (envelope=pulse-auto-XXXX, marker='<proposal-task-id>'); falling through to default routing`. Dispatch STILL SUCCEEDS via fallback. 6 total occurrences since May 28 (firing at Check I dispatch cadence). Prior iters missed it because Check 1 used `tail -20` (too small). G-rule: **auto-dispatch-APPROVAL_REQUEST-task-id-mismatch-warn-vs-info 1/3**. Dispatch to Beacon at 3/3 for warn-vs-info fix.

---

## heal-droplet-git-drift Tier-4 → COMPLETE ✅ (iter ~2273 dispatch, iter ~2278 PR merged, iter ~2293 PRIME verified)

**Rule:** G-rule heal-droplet-git-drift-tier4 COMPLETE. PR #586 (`chore(config): silence Pulse re-triage of droplet-uncommitted:main drift alert`) merged 2026-06-19T13:26:32Z. Tier-3 translation confirmed working in production: L891 (`source=heal-droplet-git-drift, subject=droplet-uncommitted:main`) classified Tier-3 by triage helper at iter ~2293 (2026-06-19T19:30Z). Bot still DMs Larry (route=escalate preserved per PR #586 design). PRIME verified. **G-rule COMPLETE.**

---

## heal-pipeline-stall:unrouted-pr Tier-4 → COMPLETE ✅ (iter ~1930 dispatch, iter ~1969 verified)

**Rule:** `source=heal-pipeline-stall, subject=pipeline-stall:unrouted-pr:PR#N` alerts now classify Tier-3 (silenced per longest-prefix match) via translation in `config/alert-translations.json`. PR #516 (`forge/alert-translation-unrouted-pr-001`) merged 2026-06-15T17:27:36Z. First live verification: L1031+L1032 (PRs #513/#512) triaged Tier-3 by helper in iter ~1969. **G-rule COMPLETE.** Bot still DMs Larry for unrouted PRs — Pulse no longer double-DMs.

---

## catalog-accuracy-drift Tier-4 pattern (observed iter ~1926)

**Rule:** `source=pulse-check, subject=catalog-accuracy-drift` alerts classify as **Tier-3** (known-pattern match in alert-translations.json) in triage helper. Alert carries `route=digest` — bot delivers as digest, no DM. Do NOT send second DM from Pulse. Journal-note only. Current count: 7/60 shelf cards drifted (iter ~2340). **G-rule count: 2/3** — dispatch to Beacon at 3/3 for alert-translations.json Tier-3 template.

---

## §5.0 script paths — ground-truth (confirmed iter ~2183)

**Rule:** `audit_due_nudge.py` and `distill_detector.py` live in `scripts/`, NOT `review/distill/`. Only `audit_cadence_signal.py` is in `review/distill/`. Always invoke: `python3 scripts/audit_due_nudge.py`, `python3 scripts/distill_detector.py`, `python3 review/distill/audit_cadence_signal.py`.

---

## G-rule forge-preflight-no-marker → COMPLETE ✅ (iter ~2306 dispatch, iter ~2307 verified)

**Rule:** G-rule forge-preflight-no-marker COMPLETE. Mirror approved PR #600 `feat(agent_runner): deterministic preflight marker reminder for Forge dispatches` at 18:03:38 MDT 2026-06-19; auto-merged + branch deleted. Pattern was `MalformedForgeMarker: phase=preflight requires ONE marker block — none found` (4 incidents, all self-recovered). Fix: deterministic preflight marker reminder injected via --append-system-prompt on every phase=preflight Forge dispatch. PRIME verified iter ~2307. **G-rule COMPLETE.**

---

## G-rule projects-json-healer-path-unregistered → COMPLETE ✅ (iter ~2309 dispatch, iter ~2313 verified)

**Rule:** `agents/beacon/projects.json` is written and committed by a projects-store healer but was NOT listed in `config/healer-managed-runtime-paths.json`. Produced Check A dirty-tree finding (never-auto + tier-reset) on 3 occasions. G-rule 3/3 threshold hit at iter ~2309 → dispatch sent to Beacon (`projects-json-healer-path-register-001.json`). PR #603 `fix(config): register projects.json as healer-managed runtime path` merged 2026-06-20T01:21:55Z. Fix adds projects.json to `config/healer-managed-runtime-paths.json`, `scripts/_lib_pulse_runtime.sh`, and `scripts/heal_droplet_git_drift.py`. PRIME verified iter ~2313. **G-rule COMPLETE.**

---

## G-rule outbox-notifier-review-pass-tier4 → COMPLETE ✅ (iter ~2347)

**Rule:** `source=outbox-notifier, kind=notification, intent=review-pass` alerts now correctly classify Tier-3 (digest route) via the `review-pass` key in `config/alert-translations.json`. Confirmed working in production at iter ~2347 — triage helper returned tier=3, route=digest. G-rule count stopped at 1/3 because the translation was already present (confirmed in PR #604's scope or earlier). COMPLETE.

---

## seq-advancer-sequence-stranded false-positive mechanism → G-rule COMPLETE ✅ (iter ~2567 dispatch, iter ~2571 verified)

**Rule:** When the build-sequence-advancer fires a `sequence-stranded` alert (4h backstop), verify the build phase archive BEFORE treating it as a live issue. Check `outboxes/forge/.archive/<task-id>.1.json` for `exit_code` and `result` text. If exit_code=0 and result says "PR opened: …," the alert is a false positive — Forge built and opened a PR but the advancer's 4h backstop fired before detecting it. **G-rule COMPLETE:** PR #661 `fix(advancer): check for open PR before stranding a stalled sequence step` merged 2026-06-24T02:43:55Z. Fix: advancer now checks for existing open PR before marking step failed at the 4h backstop. **G-rule COMPLETE.**

---

## G-rule catalog-accuracy-drift → COMPLETE ✅ (iter ~2453 verified)

**Rule:** G-rule catalog-accuracy-drift COMPLETE. catalog-drift-sync-cadence-001 → ourliberty-graph PR #6 merged 2026-06-22T04:56:35Z (Mirror REVIEW_PASS, auto-merged). The fix syncs catalog drift on a cadence. Pattern was `source=pulse-check, subject=catalog-accuracy-drift` (7/60 shelf cards drifted). G-rule dispatch was iter ~2452 (3/3 threshold). **G-rule COMPLETE.**

---

## medic-diagnosis-translation-gap → CLOSED ✅ (iter ~2496 verified)

**Rule:** G-rule `medic-diagnosis-translation-gap` CLOSED. Triage helper returned Tier-3 for `source=medic, intent=medic-diagnosis` alert L947 at iter ~2496. alert-translations.json has working `medic-diagnosis` entry (tier=FYI, route=digest — medic already DMs Larry via chat_id, so Pulse silence is correct). Translation likely restored by PR #645 (changed alert-translations.json). G-rule condition (translation missing) no longer holds. **CLOSED.**

---

## doorbell-tier4-pattern → COMPLETE ✅ (iter ~2518 dispatch, iter ~2529 verified)

**Rule:** `source=doorbell, intent=doorbell` alerts now classify Tier-3 (silenced, route=digest) via entry in `config/alert-translations.json`. PR #648 (`fix(alerts): Tier-3-silence doorbell notifications (config-only)`) merged 2026-06-23T14:34:49Z. Live triage test confirmed tier=3 at iter ~2529 (15:21Z). PRIME verification_pending promoted to systemic_fix. **G-rule COMPLETE.** Doorbell service already DMs Larry directly; Pulse silence is correct.

---

## heal-stale-daemon-code-script-service-mismatch → COMPLETE ✅ (iter ~2518 dispatch, ~2520 approved, ~2522 verified)

**Rule:** `scripts/heal_stale_daemon_code.py` now correctly attributes per-service script paths in larry-alerts messages. PR #647 (`fix(heal-stale-daemon): attribute shared-lib-triggered restarts correctly in alerts`) merged 2026-06-23T13:41:25Z (commit b89f7615). Fix: `dm_larry_auto_restarted` gains optional `changed_lib_entrypoint` separating changed library from service's own entrypoint; `_check_watchlist_pair` resolves entrypoint via FragmentPath/parse_script_path helpers. 77 targeted tests pass. G-rule COMPLETE. PRIME systemic_fix logged iter ~2522.

---

## G-rule mirror-marker-parse-error → COMPLETE ✅ (iter ~2526 dispatch, iter ~2531 verified)

**Rule:** Mirror review dispatches now get a symmetric marker reminder matching the Forge preflight reminder. PR #650 `feat(agent-runner): symmetric review-phase marker reminder for Mirror` merged 2026-06-23T15:35:09Z. Fix adds `build_review_marker_reminder_system_prompt()` + `review_marker_reminder_args()` gated to `phase==review AND expected_agent==mirror`, naming all four REVIEW_* markers and the marker.py render mirror path. 19 tests pass. PRIME systemic_fix logged iter ~2531. **G-rule COMPLETE.**

---

## G-rule watchdog-watcher-log-stale-post-fix → DISPATCHED (iter ~2640, 3/3)

**Rule:** PR #649 was COMPLETE after 5 clean Check 1 scans (iter ~2531). Pattern re-emerged: iter ~2634 (idle gap), iter ~2638 (long Mirror session for PR #687, 7 WARNs/40 min), iter ~2640 (NEW session for PR #687 backstop review at 22:40Z). 3/3 threshold crossed iter ~2640. Dispatch: `watchdog-stale-post-pr649-regression-fix-001.json` to Beacon inbox. Fix needed: suppress watchdog stale-log WARNs when (a) inbox_watcher has live in-flight Mirror session PID + open worktree, OR (b) all inboxes empty. verification_pending.

---

## G-rule ourliberty-health-notify-script-missing — 2/3 (iter ~2634 first, iter ~2640 second)

**Rule:** `ourliberty-health` emits `WARN: notify script missing, alert dropped: 1 issue(s) need attention` intermittently. The service tries to notify via a script that doesn't exist; underlying "1 issue" is unknown. journalctl query by unit name requires elevated permissions (only visible via `ourliberty-*.service` wildcard). Instances: iter ~2634 (first), iter ~2640 (22:33Z second). Dispatch to Beacon at 3/3 to investigate missing script and the suppressed health issue.

---

## G-rule watchdog-watcher-log-stale → COMPLETE ✅ (iter ~2522 dispatch, iter ~2531 verified)

**Rule:** PR #649 `fix-watchdog-stale-log-inflight-aware-001` merged 2026-06-23T14:54Z. Fix makes the watchdog in-flight-aware (active build state suppresses stale-log WARNs). Verified via 5 consecutive clean Check 1 scans post-merge — no stale-log WARNs detected. PRIME systemic_fix logged iter ~2531. **G-rule COMPLETE.**

---

## triage-alert call discipline — pass ACTUAL alert JSON, never reconstruct (learned iter ~2503)

**Rule:** When calling `alert_triage_state.py triage-alert --alert '<json>'`, always pass the VERBATIM JSON from larry-alerts.jsonl. Never reconstruct the JSON with inferred fields. The `_translation_match` lookup uses `subject → intent → kind` precedence — if you add a non-null `subject` field that wasn't in the original alert, the subject key overrides the `intent` fallback and fails the translation lookup (returns Tier-4 instead of Tier-3). In iter ~2503, L955/L956 (medic-diagnosis) were initially Tier-4 because of a fake `subject` field added during reconstruction; re-triaged correctly as Tier-3 after passing the actual alert JSON (which has `intent=medic-diagnosis` and NO subject field).

---

## check-i-repeat-dm-fix-001 → COMPLETE ✅ (iter ~2610 verified)

**Rule:** Same-week repeat Check I DMs now route to `digest` instead of `escalate`. PR #674 (`fix(pulse): route repeat same-week Check I DMs to digest instead of escalate`) merged 2026-06-24T15:45Z. Fix verified iter ~2610: `pulse_check_i.py --force` ran `mode=digest`, `DM: cooldown-suppressed`, no new entry written to `larry-alerts.jsonl`. Journal-peek predicate (`week_ending in _CHECK_I_HEADER_RE.findall(journal_text)`) mirrors `append_journal` dedup; first weekly run escalates+writes the block, later same-week runs go digest. 114 tests pass. **G-rule COMPLETE.**

---

## G-rule check-i-force-bypass-dm-route — 1/3 (new, iter ~2611)

**Rule:** The cycle invokes `pulse_check_i.py --force` on scheduled firing days (Mon/Wed/Fri/Sun). `--force` bypasses both the weekday gate AND the `dm_route` journal-peek added by PR #674. On a scheduled firing day, the weekday gate passes naturally — `--force` is unnecessary and its side-effect undoes PR #674's routing fix: repeat same-week Check I DMs write to larry-alerts.jsonl with route=escalate instead of route=digest. Tier-3 triage silences these correctly, but the root cause is worth fixing. Fix candidate: drop `--force` from cycle's Check I invocation on firing days (Mon/Wed/Fri/Sun); keep `--force` only for /optimize (ad-hoc path). Dispatch to Beacon at 3/3.

---

## G-rule heal-daemon-restart-manifest-drift-regenerated-tier4 — 1/3 (new, iter ~2620)

**Rule:** `source=heal-daemon-restart-manifest-drift, subject=regenerated` alerts classify Tier-4 (novel) — no translation match. But these are routine healer auto-commit actions (route=digest in the alert itself; bot already silences as digest). Should add Tier-3 translation. Dispatch to Beacon at 3/3 to add `config/alert-translations.json` entry.

---

## G-rule heal-pipeline-stall-mirror-pass-unmerged-tier4 — 2/3 (iter ~2629, ~2636)

**Rule:** `source=heal-pipeline-stall, subject=pipeline-stall:mirror-pass-unmerged:PR#N` alerts classify Tier-4 (novel — no translation match). Bot delivers as escalate (route=escalate) and DMs Larry. Pulse does NOT send a second DM. Dispatch to Beacon at 3/3 for Tier-3 translation in alert-translations.json (bot already DMs Larry directly; Pulse silence is correct). Instances: iter ~2629 (PR#685 rev1), iter ~2636 (PR#685 L1039 at 22:07Z).

---

## Status snapshot — updated 2026-06-24 22:45Z UTC (Iter ~2640, Tier 1, consecutive_clean=0)

**Iter ~2640 summary:** ⚠️ Watch — PR #685 CONFLICTING (pipeline-stall cooldown active); PR #687 CONFLICTING (MalformedMirrorMarker; heal-undispatched-pr-review backstop dispatched at 22:35Z; inbox_watcher processing backstop review; 2 Mirror inbox items). G-rule watchdog-watcher-log-stale-post-fix **3/3 DISPATCHED** (watchdog-stale-post-pr649-regression-fix-001 → Beacon inbox). ourliberty-health-notify-script-missing **2/3** (22:33Z second occurrence). 0 new alerts (watermark=1041). 8 daemons alive. Check I: mode=digest, cooldown-suppressed. HEAD=f9e31ea9. PRIME: interventions≈1112, systemic_fixes=64, ratio≈17.4, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-24 22:39Z UTC (Iter ~2639, Tier 1, consecutive_clean=0)

**Iter ~2639 summary:** ⚠️ Watch — PR #685 CONFLICTING (pipeline-stall cooldown active); PR #687 CONFLICTING (Mirror session reaped at 22:30Z; MalformedMirrorMarker at 22:32Z; re-review pending via marker-error envelope in Mirror inbox). 1 new alert (L1041): Tier-3 (heal-wedged-review-sessions, wedged-review-reaped). Watermark 1040→1041. 8 daemons alive. G-rule watchdog-watcher-log-stale-post-fix: 2/3 carry (same session reaped, no new independent occurrence). Check I: mode=digest, cooldown-suppressed. HEAD=eb45518b. PRIME: interventions≈1111, systemic_fixes=63, ratio≈17.6, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-24 22:31Z UTC (Iter ~2638, Tier 1, consecutive_clean=0)

**Iter ~2638 summary:** ⚠️ Watch — PR #685 CONFLICTING (pipeline-stall cooldown active); PR #687 CONFLICTING (Mirror review active since 21:55Z, 0 reviews yet); G-rule watchdog-watcher-log-stale-post-fix **2/3** NEW (7 WARNs in 40-min window, in-flight suppression failing for long Mirror sessions). 0 new alerts (watermark=1040). 8 daemons alive. Check I: mode=digest, cooldown-suppressed. HEAD=e57fc076. PRIME: interventions≈1110, systemic_fixes=63, ratio≈17.6, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-24 22:20Z UTC (Iter ~2637, Tier 1, consecutive_clean=0)

**Iter ~2637 summary:** ⚠️ Watch — PR #685 CONFLICTING (pipeline-stall cooldown active); PR #687 CONFLICTING (Mirror review active, no reviews yet). 1 new alert (L1040): Tier-3 (medic-diagnosis). Watermark 1039→1040. 8 daemons alive. Check I: mode=digest, cooldown-suppressed. HEAD=b8019068. PRIME: interventions≈1109, systemic_fixes=63, ratio≈17.6, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-24 22:14Z UTC (Iter ~2636, Tier 1, consecutive_clean=0)

**Iter ~2636 summary:** ⚠️ Watch — PR #685 CONFLICTING (mirror-pass-unmerged L1039 DM'd Larry; pipeline-stall in cooldown); PR #687 CONFLICTING (Mirror review active, no reviews yet). 4 new alerts triaged (L1036-L1039): 3 Tier-3, 1 Tier-4 (L1039 mirror-pass-unmerged). Watermark 1035→1039. G-rule heal-pipeline-stall-mirror-pass-unmerged-tier4 **2/3** NEW. 8 daemons alive. Check I: mode=digest. HEAD=879158fc. PRIME: interventions≈1108, systemic_fixes=63, ratio≈17.6, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-24 22:07Z UTC (Iter ~2635, Tier 1, consecutive_clean=0)

**Iter ~2635 summary:** ⚠️ Watch — PR #685 (forge/escalation-feed) CONFLICTING (carry); PR #687 (forge/forge-post-open-mergeable-rebase-001) CONFLICTING, Mirror review active since 21:55:22Z. Pipeline stall cooldown expired for reconcile-hardening-mission-shipped-001 (dry-run: 1 alert would fire; re-dispatch -002 in BUILD). 0 new alerts (watermark 1035). 8 daemons alive. G-rule watchdog-watcher-log-stale-post-fix 1/3 VERIFIED no new instance (in-flight suppression working). Check I: mode=digest. HEAD=d5d6b53c. PRIME: interventions≈1107, systemic_fixes=63, ratio≈17.6, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-24 21:57Z UTC (Iter ~2634, Tier 1, consecutive_clean=0)

**Iter ~2634 summary:** ⚠️ Watch — PR #687 NEW CONFLICTING (21:44Z) — G-rule fix forge-post-open-mergeable-rebase-001 opened as PR but is itself CONFLICTING (meta-irony); PR #685 still CONFLICTING. Watchdog-watcher-log-stale WARNs above threshold (12/hr, post-PR #649 fix; G-rule watchdog-watcher-log-stale-post-fix 1/3 NEW). ourliberty-health notify-script-missing (G-rule 1/3 NEW). 0 new larry-alerts (watermark 1035). 8 daemons alive. Check I: mode=digest, cooldown-suppressed. PRIME: interventions≈1106, systemic_fixes=63, ratio≈17.5, trend=improving. Tier 1, consecutive_clean=0.


