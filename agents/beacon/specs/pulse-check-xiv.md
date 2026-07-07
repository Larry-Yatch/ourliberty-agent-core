# Spec: Pulse Check XIV — alert-precision meter

Status: DRAFT v2 for build (2026-07-07). Independent parallel track to Check XII (no shared substrate) — keeps the idle Forge team fed while XII builds. Addresses hole #1 from the 2026-07-07 pulse audit: fleet-wide alert precision / toil. Numbering: XIII reserved for the Larry-leverage meter; this is **XIV**.
Author: desktop session with Larry. Companion memory: `pulse-check-audit-2026-07-07`, `alert-toil-principle`, `recurring-nudge-park-dont-decay`.

## 0. What v2 changed (two adversarial reviews — read first)

v1 had two independent fatal flaws; both are fixed by the same reframe.

- **The data model couldn't join.** v1 joined `larry-alerts.jsonl` to `alert-triage.json` on `alert_id`. Verified impossible: `alert_id` is an absolute line number the caller assigns, and `larry_alerts_retention.py` rotates + `repair_watermark()` renumbers the live log, so the stored triage keys index an *archived* generation (proof: triage keys 990–995 return null while live lines 990–995 are real recent alerts). And the triage row carries **no source, no subject, no alert-ts**, so the `(source,subject,ts)` fallback is also impossible. → **v2 reads the raw log ALONE and re-derives tier/decision by re-running the pure classifier** `alert_triage_state.classify()` over each line (verified: `classify(alert, *, registry, translations, route_fn=None) -> {tier, route, decision, rationale, template}`, "Pure § 6.6 classification — no side effects"). Clean keys, no join.
- **The silencing rule would blind Larry.** v1's rule 1 proposed auto-silencing any signature that recurred ≥3× "without conversion to a dispatch." But dispatches are structurally **0** fleet-wide, so "without conversion" filters nothing, and the proxy cannot tell *noise Larry ignores* from *a real unfixed problem that recurs because it's unfixed*. Auto-silencing the latter buries the signal — the catastrophic failure mode — and violates the `recurring-nudge-park-dont-decay` principle (never silence a recurring nudge just because it was ignored N times). → **v2 removes all auto-silence from V1.** V1 is a measurement + safety instrument that changes no config; the toil-reducing automation is sequenced behind the substrate that makes it safe (XIV-b/c below).

Producer-name correction: the triage layer is **`alert_triage_state.py`** (`classify`/`triage_alert`/`record_triage`), NOT `promote_alerts.py` (which explicitly does not consume the alert log) or `triage_decisions.py` (a chain_events subsystem).

## 1. Purpose + roadmap slices

Meter the precision of the fleet's alert layer and surface where it's blind or noisy — the measurement the audit asked for, delivered safely first. Honest V1 scope: it **reports and surfaces**; it does not yet auto-tune. The automation is deliberately downstream of the substrate that makes it non-dangerous:

- **XIV (this spec, V1):** measurement + over-silence safety surface + a *reported* (not auto-landed) list of recurring-novel signatures. Changes no config. Fully buildable on current substrate.
- **XIV-b:** close the tier-4 write-back loop — record whether Larry actioned/dismissed a surfaced alert (`resolution`/`resolved_at` on the triage row; all 347 tier-4 rows are null today). This is the substrate that turns the recurrence *proxy* into a real ignore/act signal.
- **XIV-c:** the self-tuning config loop — recurring-novel → propose a dispatch **template** (route the recurring signal to the healer that fixes it) or, once XIV-b exists, a time-boxed auto-silence — landed via a **new Beacon handler** (§6) into `alert-translations.json`, individually approvable through the Approvals queue (not a batch DM). Built after XIV-b makes silence safe.

This ordering is prove→ship→automate: measure first (safe), close the loop (substrate), then automate (guarded).

Design constraints (validated on Check XII):
- **Systemd timer from birth** — copy the whole `ourliberty-pulse-check-viii` unit; never enters cycle-prompt.md §5.
- Heartbeat every run via `pulse_check_heartbeat.run_check('xiv', main)`; `main()` returns 0 on every partial/skip path, non-zero ONLY on artifact-write failure (verified: `run_check` emits heartbeat on rc==0, `emit_failure` on rc!=0).
- `config/pulse-check-cadence.json` → `checks.xiv` = `{"cadence_hours": 168, "grace_hours": 36, "firing": "Weekly (systemd timer: ourliberty-pulse-check-xiv)", "label": "Alert-precision meter"}` (label + `checks.` nesting are required — every sibling has them).
- Deterministic stdlib Python (`scripts/pulse_check_xiv.py`), no LLM.

## 2. Substrate (verified live; no join)

- **Only source:** `~/agents/blackboard/larry-alerts.jsonl` — one JSON object per line: `{ts, source, severity, message, route, subject, [suggested_action], [template], [intent], [kind]}`. It has everything `classify()` reads.
- **Classifier:** import `alert_triage_state.classify` and call it per line with the **live** `registry` + `translations`, loaded via the same loaders `alert_triage_state`'s own caller (the Pulse Check 0 `triage-alert` CLI) uses — name those loader helpers at build; do not hand-roll config reads. `classify` returns `{tier, route, decision, rationale, template}` per alert with no side effects. This *reconstructs* the triage decision from clean keys instead of joining to the stale stored decisions.
- **Config the meter reports candidates for (XIV-c writes it, V1 only reads):** `config/alert-translations.json` — nested `{source: {subject: {plain_language_summary, recommended_action, severity∈{URGENT,WARNING,INFO}, tier∈{NOW,SOON,FYI}, [never_silence: bool]}}}`. Lookup: exact `(source,subject)`, then progressive `:`-suffix strip, then `-YYYY-MM-DD` strip, then source-level `'*'`. Presence = tier-3 auto-silence unless `never_silence:true`.
- **Window:** trailing 14d default. **Re-measure the baseline at build** — as of 2026-07-07 the triage file held ~1457 decisions over ~34d, tier-3 ≈ 76%, tier-4 ≈ 24%, **0 tier-1/2 (0 auto-dispatch)**, all tier-4 `resolution=null`. The §0 shape (silence-heavy, punts-to-Larry, auto-fixes-nothing) holds directionally; the exact numbers must be recomputed from the live log via `classify()` at build, not copied from here.

## 3. Metrics — per source and per (source, signature), all from re-classified raw lines

**Signature** = normalized alert identity. Apply in THIS order (order is load-bearing — SHA/UUID before digits, else the digit pass mangles the hex so the SHA regex can't match): (1) UUID→`@`, (2) SHA `\b[0-9a-f]{7,40}\b`→`@`, (3) `\d+`→`#`, (4) lowercase, (5) collapse whitespace. Key = `source` + normalized `subject`. Verify at build that this collapses the real top-sources sensibly (eyeball 5).

Per source and per signature, over the window (tier/decision from the `classify()` re-run):
- `volume`.
- `silence_rate` = tier-3 / total (fleet ≈ 76%).
- `ask_rate` = tier-4 / total (the Larry-facing load).
- `dispatch_rate` = tier-1/2 / total (fleet ≈ 0% — reported as a *finding*: the fleet auto-fixes nothing).
- `recurrence` = raw count ÷ distinct signatures (how repetitive).
- `novelty` = share of the source's tier-4 alerts whose `rationale` is the novel-fallthrough (no template, no translation) — i.e. a missing template/allowlist, by definition.

**Precision proxy + its stated limit:** V1 reports `noise_candidate_share` = tier-3-silenced + recurring-novel-tier-4 (recur ≥3× identically), and **states inline that this cannot distinguish ignored-noise from unfixed-real** (no action-rate substrate until XIV-b). It is a *reporting* figure, never an auto-action trigger.

## 4. What V1 emits (no config changes, no auto-silence)

1. **Precision report** (artifact + monthly-or-on-signal digest): per-source precision table, the `dispatch_rate≈0` finding, and the top recurring-novel signatures as **candidates** (source, normalized signature, count, sample messages) that Larry — or later XIV-c — can act on. V1 does NOT land these; it names them.
2. **Over-silence safety surface** (the one genuinely Larry-worthy signal): a source/signature at ~100% silence AND high volume, OR any translation entry whose matched alerts spiked in volume, → surface for Larry to confirm the blanket silence is still right. This is the guard against an over-suppressed *real* signal (park-don't-decay applied to the existing allowlist). Warning DM, not a proposal.
3. **Nothing else auto-fires.** No allowlist writes, no route demotions — those are XIV-c, gated behind XIV-b.

Cap: report at most the top **10** recurring-novel candidates (ranked by volume × novelty); remainder to the artifact only, so the digest never storms.

## 5. Artifact, DM, cadence, contracts

- Artifact: `~/agents/blackboard/pulse-check-xiv/check-xiv-<date>.json` — per-source + per-signature metric blocks, the recurring-novel candidate list, the over-silence findings, `as_of` (UTC), `window`, `sources` status block. 26-week retention (self-pruned).
- Heartbeat every run (§1 invariant).
- DM: `append_alert(source='pulse-check-xiv', severity=…, message=…, subject=…, route='escalate')` (`route='escalate'` required — `info` defaults to the digest lane). Precision digest → `(severity='info', route='escalate', subject='pulse-check-xiv-digest')`, fires **first Monday of month or when the over-silence surface trips**, else artifact+heartbeat only (alert-toil applies to XIV itself). Over-silence → `(severity='warning', route='escalate', subject='pulse-check-xiv-oversilence:<source>')`. Add `alert-translations.json` entries for each new subject (plain-language-first) — **and tag them `never_silence` is NOT needed; but note XIV must not silence its own alerts**.
- Cadence: `OnCalendar=Mon *-*-* 05:49:00` + `RandomizedDelaySec=300`, bare system-tz (matches siblings). Slot verified free (Monday cluster: iv 04:25 / v 04:49 / vi 04:59 / viii 05:09 / ix 05:19 / x 05:29 / XII 05:39; XIV 05:49; CEO digest 06:00).
- **Partial-data contract:** the log read is try/except'd; artifact always written with `sources` status; a dark/unreadable log is a 0-exit with `sources.log='error'`, escalating only after 2 consecutive dark runs. Only artifact-write failure is non-zero.

## 6. XIV-c note (NOT V1) — the config-landing loop needs a new Beacon handler

Recorded so it isn't mis-scoped later: the self-tuning `approve <check>-update-<date>` flow is **not** generic — each check has a bespoke hand-authored block in `agents/beacon/CLAUDE.md` naming its target file + patch shape (III→system_tab_thresholds, VIII→agent-models tier1_quota, distill→known-bug-patterns). There is **no** handler for `alert-translations.json`. XIV-c must therefore ship a new Beacon CLAUDE.md handler block for `approve check-xiv-update-<date>` that names `config/alert-translations.json`, specifies the exact nested leaf shape (§2), and the "merge under existing `<source>` key, don't clobber siblings" rule — an **agent-behavior change reviewed as such, NOT a config-only PR**. Silence proposals route to the Approvals queue (individually approvable, time-boxed auto-expiry so a wrong silence self-heals) per the approval-sync north star, never a batch DM.

## 7. Acceptance (V1)

1. Timer + service = whole `pulse-check-viii` unit copy; `checks.xiv` cadence entry added with `label` (§1); coordinate the cadence.json edit with any concurrent XII build (both touch that file). `systemctl list-timers` shows next Monday; unit copy refreshed in `/etc/systemd/system` before daemon-reload.
2. First run reads the raw log, re-runs `classify()` per line with live registry+translations, writes artifact + heartbeat, exits 0, DMs only if the over-silence surface trips or it's the monthly digest.
2b. Dark-source exit-0 test (log unreadable → `sources.log='error'`, heartbeat fires, exit 0, no `pulse-check-failed:xiv`).
3. `heal_pulse_check_staleness` shows XIV fresh.
4. `classify()` re-run verified to reproduce the live tier distribution within tolerance on a known slice (sanity that the re-classification matches what the triage layer actually did); signature-normalization verified to collapse the real top-sources into sensible groups (eyeball 5); regex order (UUID/SHA before `\d+`) unit-tested.
5. Baseline re-measured from live data (not copied from §0/§2); over-silence threshold set from the observed per-source silence/volume distribution.
6. **No config-landing path in V1** — assert the run writes nothing to `alert-translations.json` and emits no `approve` shortcut (that's XIV-c). This is a V1 safety property, tested.
7. Unit tests via unittest (not pytest), sentinel-armed, zero live-tree writes.
8. Decommission order documented (stop/disable timer → remove cadence entry → delete heartbeat) → zero alerts next healer run. EMERGENCY_HALT honored via ConditionPathExists.

## 8. Out of scope (V1) / honest limitations

- **True Larry-action-rate** — unmeasurable until **XIV-b** closes the tier-4 write-back loop; V1's `noise_candidate_share` is an explicitly-labeled proxy, never an auto-trigger.
- **All auto-silence and route-demotion** — **XIV-c**, gated behind XIV-b; V1 changes no config.
- **The `alert-translations.json` landing handler** — XIV-c (§6), an agent-behavior change.
- Dashboard widget; root-causing why a source is noisy (V1 names noisy sources, doesn't fix them).

## 9. Cost + followups

Deterministic Python over one local file + an in-process `classify()` re-run; ~zero LLM cost; trivial runtime (no chain_events, no gh). Followups in order: **XIV-b** (write-back loop → real action-rate + the false-silence/silence-miss metric that gives the automation a self-correcting error signal), then **XIV-c** (guarded auto-tune via the new Beacon handler + Approvals-queue landing). Consider an alert-precision line in the CEO digest once XII-b establishes the digest-block pattern.
