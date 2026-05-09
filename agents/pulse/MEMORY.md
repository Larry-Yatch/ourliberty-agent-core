# Pulse — Long-term Memory

*Distilled wisdom carried across cycles. The cycle-journal is the chronological record; this file is the curated essence — patterns I've internalized, calibration notes, things to keep in mind.*

*Keep under 15,000 characters. Above 18,000 = condense.*

---

## Status snapshot — updated 2026-05-09

First real cycle ran (Iteration 1). System in early Phase C/D activation. Bots wired; sync not yet run; working tree has minor hygiene issue.

## Known calibration issues

- **Beacon log-silence false positive (2026-05-09).** Check C threshold (>30m log silence → ask-then-do) fires on idle Telegram polling periods. The beacon bot logs nothing when no user messages arrive. Need a "messages received" signal or a longer threshold for bots in idle state before escalating. Do not DM Larry for routine bot idleness unless there's also a process/health indicator of trouble.

## System-state assumptions that have proven wrong

- **2026-05-09 — Unattended run_cycle.sh cannot write journal.** The `claude --print --output-format json` invocation in run_cycle.sh is non-interactive. Write/Edit tool calls require interactive user approval. Until agents/pulse/.claude/settings.json has an allowlist for the cycle-specific write paths (cycle-journal.md, cycle-actions.jsonl, pulse-escalations.json, MEMORY.md), every unattended cycle will run checks and exit 0 but leave no journal trace. **Fix needed:** Forge task to add the allowlist. (See pulse-escalations.json iter=1.)

## Recurring patterns I've promoted to permanent fixes

*(empty — this is the most important section over time. Each entry: what was the pattern, what permanent fix landed, when, by whom)*

## Recurring patterns I've decided NOT to promote (and why)

*(empty — sometimes the systemic fix is worse than the manual intervention. Document those calls so I don't relitigate.)*

## Auto-fix allow-list expansions

*(empty — when an "ask-then-do" check has been "Larry says yes" for 10+ consecutive iterations, I propose moving it to "always-allowed". Track those decisions here.)*

## Escalations Larry overrode (calibration data)

*(empty — when I escalated and Larry said "no action needed" or "you should have just fixed that," recalibrate. Keeps me from over-paging or under-acting.)*

## System-state assumptions that have proven wrong

*(empty — when a check assumed something about the system that turned out not to be true; document so the check gets updated.)*

---

**Format reminder:** Each entry has a date, a one-line claim, and (where the claim is non-obvious) a "Why" line explaining the reasoning. Date stamps let me judge whether a memory is still current.
