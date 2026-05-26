# Add a new healer alert

When you add a new healer (or extend an existing one with a new alert subject), you MUST add a corresponding entry to `config/alert-translations.json` BEFORE merging. The CI gate (`scripts/tests/test_alert_translations.py`) will fail the PR if a new in-scope `larry_alerts.append_alert(...)` call site doesn't have a matching translation.

This runbook is part of the stopgap translation layer documented in `docs/operating-manual.md` Part II #68. The Pulse cycle upgrade (follow-up to PR #108) will eventually subsume this layer; until then, every new healer alert needs an entry.

## The 4-step discipline

1. **Emit the alert from your producer** with `larry_alerts.append_alert(source=<your-source>, subject=<your-subject>, severity=<warning|critical>, message=..., suggested_action=...)`. Use hyphenated, colon-separated subjects (e.g., `my-healer:specific-failure:dynamic_id`) — the lookup rule strips trailing `:`-segments, so the static prefix is what gets matched.

2. **Add a translation entry** to `config/alert-translations.json` under your source. The key is the static subject prefix (everything before the first dynamic suffix). Each entry has three fields:

   ```json
   {
     "your-source": {
       "your-subject-prefix": {
         "severity": "URGENT" | "WARNING" | "INFO",
         "plain_language_summary": "...",
         "recommended_action": "..."
       }
     }
   }
   ```

3. **Run the CI gate locally** to confirm:

   ```
   cd ~/agent-core && python3 -m unittest scripts.tests.test_alert_translations
   ```

4. **Open the PR.** If the test fails, the failure message names the file:line of the producer call site and the missing `(source, subject_prefix)` — fixing it is mechanical.

## Translation writing guidelines

The translation is what Larry reads on his phone, not in code-deep mode. Write for that context.

- **`severity`** (URGENT / WARNING / INFO):
  - **URGENT** — the chain is stuck OR the dashboard is blind. Larry needs to act now.
  - **WARNING** — degradation but the chain still moves. Larry should know but can drill at his pace.
  - **INFO** — nothing's broken, just notable. Reserved for future use; no V1 entries use INFO.
  - Display severity is DECOUPLED from producer queue severity. A `queue=warning` alert can render as URGENT if operational urgency warrants it (e.g., `chain-event-shipper-stale` is queue=warning but display=URGENT because mission control is now blind).

- **`plain_language_summary`** — 1-2 sentences in plain English. What happened, what's broken, what caused it when known. No file paths, no function names, no jargon. Larry doesn't need to know which Python function detected the failure; he needs to know what failed.
  - Good: "Forge finished a build but the PR never opened. The branch was pushed but `gh pr create` failed silently."
  - Bad: "`check_forge_built_no_pr` in `heal_pipeline_stall.py:643` detected a state mismatch between PUSH_OK and PR_OPENED."

- **`recommended_action`** — a concrete command or path, OR an explicit "no action — Pulse will triage when upgrade lands" when no operator action applies. Always include the runbook reference (`runbooks/<healer-name>.md`) when one exists.
  - Good: "Run `sudo systemctl restart chain-event-shipper.service` and check `journalctl -u chain-event-shipper.service -n 50`. Runbook: runbooks/heal-chain-event-shipper-heartbeat.md."
  - Bad: "Investigate the issue and restart as needed."

## Lookup mechanics (so you can pick the right key)

`larry_alerts.translate_alert(source, subject)`:

1. Exact match on `(source, subject)`.
2. On miss, strip the trailing `:`-segment from `subject` and retry. Repeat until match or no segments left.
3. Source must match exactly — no prefix logic on source.

Examples:

- Producer emits `subject='pipeline-stall:forge-no-pr:wt-forge-foo'`. Lookup tries:
  - `pipeline-stall:forge-no-pr:wt-forge-foo` → miss
  - `pipeline-stall:forge-no-pr` → MATCH (this is the JSON key)
- Producer emits `subject='install-drift:my-daemon.service'`. Lookup tries:
  - `install-drift:my-daemon.service` → miss
  - `install-drift` → MATCH
- Producer emits `subject='chain-event-shipper-stale'` (no dynamic suffix). Lookup tries:
  - `chain-event-shipper-stale` → MATCH (exact)

The JSON key should be the static prefix — the part that's stable across all instances of the same failure mode.

## What happens if you skip the entry

The CI gate fails the PR. Recovery: add the entry, push the fix, re-run.

If the test passes but the entry is missing (a fully-dynamic subject the AST scanner couldn't extract a prefix from), the runtime behavior is: Larry's DM still arrives, but it carries the original raw body plus the footer:

```
[no translation; needs entry in config/alert-translations.json or Pulse triage scope]
```

That footer is the load-bearing guarantee — silence on unmatched is structurally impossible. But landing without a translation is still a regression; the CI gate is the catch.

## Out-of-V1-scope producers

Several pre-existing producers are documented as out-of-V1-scope in `scripts/tests/test_alert_translations.py::_OUT_OF_V1_SCOPE_SUBJECTS`:

- `watchdog` (disk, memory, cgroup, bots) — infra-monitoring, pre-dates the healer-alert framing.
- `pulse`, `ledger` — operator-facing reports, not failure alerts.
- `beacon-telegram-bot` — bot's own Tier 1/2 quota/auth surface (covered by `heal-pipeline-stall:tier2-fallback-*` from a different angle).
- `heal-pr-auto-merge`, `heal-chain-event-type-audit`, `heal-stale-daemon-code` — healers whose subjects weren't in the dispatch's V1 ten.
- `heal-pipeline-stall` non-failure-mode subjects: `pipeline-stall:no-mirror-dispatch`, `pipeline-stall:mirror-pass-unmerged`, `pipeline-stall:unrouted-pr`.
- `heal-credential-registry-drift:credential-drift:MISSING_CREDENTIAL` (V1 covers `MISSING_REGISTRY_ENTRY` only).
- `heal-systemd-install-drift` activate-healer announcement subject.

Promoting an out-of-scope producer to V1: add the entry to `config/alert-translations.json` AND remove the corresponding entry from `_OUT_OF_V1_SCOPE_SUBJECTS`. The test will then enforce coverage going forward.

## When this runbook retires

When the Pulse cycle upgrade (follow-up to PR #108) ships healer-alert triage, the translation layer can be retired. The CI gate stays useful as an operational-ergonomics review trigger for new healers — when a new alert ships, somebody should think about how it surfaces to the operator. This runbook becomes the canonical place to update with the new Pulse-triage discipline.

See also: `docs/operating-manual.md` Part II #68 for the full design context.
