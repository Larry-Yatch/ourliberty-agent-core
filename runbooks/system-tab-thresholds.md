# Runbook — System tab stuck-detection thresholds

The dashboard's Operations → System view flags an agent session as "stuck" when its duration crosses one of the thresholds defined in `config/system_tab_thresholds.json`. This runbook covers what each threshold means, when to tune it, and how to apply changes.

## File location

- Repo: `Larry-Yatch/ourliberty-agent-core`
- Path: `config/system_tab_thresholds.json`
- Read by: the dashboard's `/api/operations/stuck-sessions` Route Handler (PR-D); also surfaced by `scripts/pulse_check_iii.py` when proposing new values.

## What each value means

| Key | Default | Meaning |
|---|---|---|
| `session_duration_seconds_default` | 900 (15 min) | Global cap for any agent session. If `now - session_start > 900s`, the dashboard renders a red stuck indicator on that session card. Mirror sessions can override via `mirror_review_overrides_seconds`. |
| `no_journal_output_seconds` | 600 (10 min) | Suspicious-silence threshold. Active sessions stream Claude output continuously; if no new chain event has landed for this session in 600s, the dashboard renders a stuck indicator. |
| `envelope_not_picked_up_seconds` | 120 (2 min) | Inbox latency cap. Inbox-watcher polls every 5s; typical pickup is <10s. An envelope sitting unpicked for >120s indicates the whole chain is in trouble. |
| `mirror_review_overrides_seconds.doc-only` | 900 | Override for `task_type=doc-only` Mirror reviews. Doc reviews are fast; matches the global. |
| `mirror_review_overrides_seconds.bug-investigation` | 1500 (25 min) | Override for `task_type=bug-investigation` Mirror reviews. |
| `mirror_review_overrides_seconds.feature-development` | 2100 (35 min) | Override for `task_type=feature-development` Mirror reviews. PR #101 would have flagged at minute 35 (~36 min earlier than the manual catch). |
| `mirror_review_overrides_seconds._default` | 1500 (25 min) | Fallback when `payload.task_type` is missing or unknown on a Mirror session. |

The Mirror override applies only when `agent == "mirror"`. Other agents always use `session_duration_seconds_default`.

## When to tune

- **Too many false positives.** A threshold is firing on sessions that are actually fine. Loosen by ~25%; re-check after 7 days.
- **A real stuck session slipped past.** A session hung but never crossed the threshold. Tighten by ~25%; watch for false positives.
- **Pulse Check III proposes a change.** See § 5.10 of `agents/beacon/specs/e4-4d-system-tab.md`. Pulse runs every 14 days, computes p90/p99 per `(agent, task_type)` bucket against the last 30 days of `chain_events`, and DMs Larry a proposal artifact at `~/agents/blackboard/pulse-threshold-proposals.json`. Larry approves with `approve threshold-update-<date>` to Beacon, who routes a one-line Claude-as-Forge config PR through Mirror.

Manual edits and Pulse-driven edits both land via PR review — never edit on the droplet directly.

## How to apply a change

1. Edit `config/system_tab_thresholds.json` in a branch. Keep the `_meta.rationale` updated to reference the change.
2. Open a PR (`feat(config): adjust stuck thresholds — <reason>`). Mirror reviews; auto-merge fires on PASS.
3. The dashboard reads the file on every `/api/operations/stuck-sessions` request. **No service restart required** on the droplet — the droplet API is uncached and the dashboard Route Handler re-reads its bundled copy on each request (PR-D wires the delivery path: either CI-sync from agent-core into the dashboard repo, or a `/api/system/thresholds` droplet endpoint).
4. Confirm by reloading `dashboard.ourliberty.dev/operations/system` and observing that previously-stuck sessions now render green (or vice-versa).

If the dashboard caches its bundled copy at build time, the change ships with the next dashboard deploy. Check the PR-D README for the exact delivery contract.

## Guardrails

- **Bounded delta.** Changes >50% from current values should be flagged in the PR body as `regime-change-suspected`. Probably worth understanding the underlying cause before tightening or loosening that much.
- **No auto-apply.** Pulse Check III proposes; Larry approves. Manual edits also go through PR review.
- **Sample-size floor.** Pulse Check III skips any `(agent, task_type)` bucket with <10 data points in the 30-day window. Insufficient signal to tune from.

## Related

- Spec: `agents/beacon/specs/e4-4d-system-tab.md` § 5.4 (locked decision-D values) and § 5.10 (self-optimizing loop).
- Pulse Check III: `scripts/pulse_check_iii.py` (ships in PR-B).
- Operating-manual doctrine entry: `docs/operating-manual.md` Part II item 48 (self-optimizing-config-via-Pulse-Check pattern).
