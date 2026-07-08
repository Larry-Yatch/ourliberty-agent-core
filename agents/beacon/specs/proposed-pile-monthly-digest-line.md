# Proposed-pile monthly digest line

**Status:** implemented (generator + tests). Dashboard render is a separate
`ourliberty-dashboard` PR that builds against the frozen shape below.

## Why

The parked-&-aging digest (`scripts/parked_aging_digest_generator.py`) already
gives Larry a daily read-only "catch me up" card for parked captures. The
**proposed-mission pile** is a separate backlog — `missions.json` cards with
`phase == "proposed"` and not archived — that has grown past ~230 cards. Larry
wants a *monthly* readout of how that backlog is growing (count, oldest, top-3
most relevant) **without ever being forced to prune it** (park-don't-decay).

Rather than stand up a new artifact/timer, the pile status is folded into the
existing digest artifact as an additive `proposed_pile` block that refreshes
once a calendar month while the parked/aging half keeps refreshing daily.

## Frozen artifact shape

The digest artifact (`~/agents/blackboard/parked-aging-digest.json`,
`SCHEMA_VERSION = 2`) gains one additive top-level key, `proposed_pile`. All
existing top-level fields are unchanged and byte-compatible.

```jsonc
"proposed_pile": {
  "as_of_month": "2026-07",          // UTC YYYY-MM this block was computed for
  "proposed_count": 231,             // proposed, non-archived cards (the pile size)
  "actionable_count": 19,            // proposed_count − staleness candidates;
                                     //   null when the staleness file is absent
  "oldest_age_days": 160,            // max calendar-day age over proposed cards
  "oldest_name": "Some old card",    //   (from created[:10] as an ISO date); null
                                     //   when no proposed card has a parseable created
  "relevance_basis": "portfolio_rank", // or "age_fallback"
  "top_relevant": [
    // portfolio_rank basis — from mission-rank.json ranked[:3]:
    {"name": "Best card", "rank_score": 90.0, "what": "one-line brief.what"}
    // age_fallback basis — 3 oldest proposed cards:
    // {"name": "Oldest card", "age_days": 160}
  ]
}
```

### Field rules

- **`proposed_count`** — `phase == "proposed"` AND not `archived`. Same filter
  as `mission_staleness` / `mission_rank`.
- **`actionable_count`** — `proposed_count` minus the number of candidates in
  `mission-staleness-candidates.json`, clamped at 0. `null` when that file is
  missing/unusable.
- **`oldest_age_days` / `oldest_name`** — the max calendar-day delta from each
  card's `created` (date-only: `created[:10]` parsed as an ISO date) to now.
  Cards with a missing/unparseable `created` are skipped; both fields are `null`
  when none qualify.
- **`top_relevant` / `relevance_basis`** —
  - `portfolio_rank`: up to 3 items from `mission-rank.json` `ranked[:3]`
    (pre-sorted best-first), each `{name, rank_score, what}` where `what` is the
    rank entry's `brief.what` one-liner. This is the canonical "most relevant"
    ordering.
  - `age_fallback`: when the rank file is missing/empty/unusable, the 3 oldest
    proposed cards as `{name, age_days}`.

## Monthly-cadence rule

On each run the generator reads the **prior artifact** first:

- If it already carries a `proposed_pile` block whose `as_of_month` equals the
  current UTC `YYYY-MM`, that block is **carried forward unchanged** (no
  recompute).
- Otherwise (prior artifact missing/unreadable, no prior `proposed_pile` block,
  or a stale month) the block is **recomputed and re-stamped** with the current
  month.

Net effect: the pile numbers refresh once per calendar month; the parked/aging
half keeps refreshing every run. On-demand refreshes (`--trigger on-demand`)
within the same month carry the block forward — they must not force a mid-month
recompute.

## Inputs (all READ-ONLY, all env-overridable for test isolation)

| Input | Default path | Env override |
|---|---|---|
| Missions board | `${OURLIBERTY_AGENTS_ROOT:-~/agents}/agents/beacon/workspace/missions.json` | `OURLIBERTY_MISSIONS_FILE` |
| Portfolio rank | `~/agents/state/mission-rank.json` | `OURLIBERTY_MISSION_RANK_FILE` |
| Staleness candidates | `~/agents/state/mission-staleness-candidates.json` | `OURLIBERTY_MISSION_STALENESS_FILE` |

## Discipline

- Stdlib only. The block is **fail-open**: a missing/malformed missions.json,
  rank file, or staleness file degrades the block (empty/`null`/`age_fallback`
  fields) but never raises and never fails the run or the parked/aging half.
- Purely additive: existing top-level fields stay byte-compatible; consumers
  gate on `SCHEMA_VERSION` (bumped `1 → 2`).
- Reuses the existing daily timer (`ourliberty-parked-aging-digest.timer`); **no
  new systemd unit.**

## Enforcement

The monthly-cadence rule is enforced by the cadence-invariant unit tests in
`scripts/tests/test_parked_aging_digest_generator.py`
(`MonthlyCadenceTest`): a second run in the same month carries the block forward
unchanged even when the underlying pile grows; a run in a new month recomputes
and re-stamps `as_of_month`. Fail-open, field-computation, and top-3/age_fallback
paths are covered by the sibling test classes.

## Out of scope

- The dashboard card render of the new line (separate `ourliberty-dashboard` PR
  — this doc is its contract).
- Any prune/write-back action on the pile.
- Any change to the aging clock or the parked/aging selection.
- Telegram delivery.
