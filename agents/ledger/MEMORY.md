# Ledger — Long-term Memory

*Distilled wisdom carried across weekly runs. The journal at `runbooks/ledger-journal.md` is the chronological record; this file is the curated essence — patterns I've internalized, calibration notes, things to keep in mind.*

*Keep under 8,000 characters. Above 10,000 = condense.*

---

## Status snapshot — initialized 2026-05-15

v1 just shipped. No weekly runs yet. First scheduled run: next Monday after the systemd timer is enabled. Until 4 weekly windows accumulate, σ-based anomaly flagging is suspended (ramp-up posture). Week-over-week drift flagging (>20%) is active from week 2.

## Known calibration items (open, not yet validated)

- **σ threshold = 2.0.** v1 default. Spec § 7 lists "2σ above task_type baseline" as the flag. Tune after 4-week ramp-up if false-positive rate is too high or signal too quiet.
- **Drift threshold = 20% week-over-week.** v1 default. Spec § 7 lists this as the open question to tune after 2 weeks of data.
- **Baseline window = prior 4 weeks.** v1 default. Larger window (8 weeks?) would smooth more but slow response to legitimate cost-structure shifts.
- **task_type "unknown" handling.** When a `costs.jsonl` row doesn't match an outbox archive, it's bucketed as `task_type = "unknown"`. Likely sources: Pulse cycle runs (source=run_cycle.sh, no outbox archive), direct CLI invocations, dispatch archives lost or pruned. Monitor the unknown bucket size — if it's >20% of total, the join logic needs revisiting.

## Recurring patterns I've promoted to permanent fixes

*(empty — no history yet)*

## Recurring patterns I've decided NOT to promote (and why)

*(empty)*

## Calibration adjustments made

*(empty)*

## System-state assumptions that have proven wrong

*(empty)*

---

**Format reminder:** Each entry has a date, a one-line claim, and (where the claim is non-obvious) a "Why" line explaining the reasoning. Date stamps let me judge whether a memory is still current.
