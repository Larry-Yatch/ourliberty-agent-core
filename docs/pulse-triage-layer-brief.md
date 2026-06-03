# Brief: Pulse triage layer + experience-driven auto-fix promotion ladder

## What Larry approved (2026-06-03)

- Autonomy posture: Pulse AUTO-FIXES known-safe, reversible, pre-vetted patterns
  and reports the outcome (fix-first / notify-on-outcome, gated by the
  significance bar from the alert-routing PR). Novel / risky / unrecognized
  signals ASK first. Credentials, money, and irreversible/destructive actions
  ALWAYS ask, regardless of track record.
- Growth: the auto-fix allowlist is DATA, and it GROWS from track record. A
  pattern earns auto-fix after ~3 clean executions with zero Larry-corrections,
  reversible only.
- Graduation gate: Pulse does NOT self-graduate. It proposes graduations to Larry
  in a CLEAR, PLAIN-LANGUAGE approval (per pattern: what it is, what Pulse would
  now do automatically, why it's safe, its track record). Larry approves the
  PATTERN once — never per instance.
- Demotion: any failure or Larry-override of a graduated pattern drops it back to
  ask-first immediately (trust lost faster than earned).

This LIFTS spec Decision D (the cautious V1 "surface-only, ask-first-on-
everything") to "earned autonomy," and operationalizes § 3.0 / § 6.6 / § 12.3.

## The three gaps this closes

1. **No durable triage.** `~/agents/state/alert-triage.json` + the helper module
   `scripts/alert_triage_state.py` (spec calls this "PR-β") do not exist. Pulse
   triages from prompt judgment each iter with no persistent per-alert lifecycle,
   so triage decisions aren't durable, auditable, or consistent across iters.
2. **Broken track record.** cycle-prime-ledger.jsonl records interventions with
   empty intervention_id, so Check V can never compute per-template history and
   nothing can graduate.
3. **Open promotion loop.** Check V writes proposal artifacts to a file that
   nobody acts on. There is no approval path that, on Larry's yes, actually moves
   a pattern between guarded and graduated in a registry Check 0 reads.

## Design

**Single registry (data).** `config/auto-fix-patterns.json` is the single source
of truth for what Pulse may auto-fix. One record per action-template:
  { template, state: "probation"|"graduated", reversible: bool,
    permanent_guard: bool, clean_streak: int, total_dispatches: int,
    last_larry_correction_at: iso|null, graduated_at: iso|null,
    plain_language: { what, action, why_safe } }
Reconcile with the existing config/action-template-guard-list.json: that guard
list becomes a DERIVED VIEW of this registry (guarded = not graduated OR
permanent_guard), or is superseded by it — Forge's call, documented. Seed
permanent_guard=true for credential / money / irreversible templates.

**Check 0 reads the registry (durable triage).** Ship alert_triage_state.py +
alert-triage.json per spec § 3.0. For each new larry-alerts.jsonl entry, Check 0
classifies via the § 6.6 decision table, now DATA-DRIVEN:
  - Tier 3 (silence→digest): matches config/alert-translations.json known-pattern.
  - Tier 2 (ask): matches a registry template that is permanent_guard OR still
    probation. DM Larry (plain-language), route stamped per the routing PR.
  - Tier 1 (auto-fix→closure/digest): matches a registry template with
    state=graduated. Fix dispatches this iter; outcome reported via the route
    field (significant→closure, routine→digest) from the alert-routing PR.
  - Tier 4 (novel): no match. Ask Larry; the answer trains the allowlist.
The triage record stamps the resulting `route` so the alert-routing PR's bot gate
and digest ingestion handle delivery — Check 0 decides, the routing layer delivers.

**Tag the ledger (fix the track record).** Every intervention Pulse records to
cycle-prime-ledger.jsonl MUST carry intervention_id = "<template>:<detail>".
Backfill is not required; forward-tagging is. This makes Check V functional.

**Close the loop (graduate / demote).**
  - Graduation: when a probation template reaches clean_streak >= GRADUATE_MIN
    (reconcile the threshold: Larry said ~3; spec § 6.6 says "3+ prior
    successful"; Check V currently says >=10 dispatches — UNIFY to 3 clean
    consecutive, reversible, zero corrections, and document the change to
    Check V's GRADUATE_MIN_DISPATCHES). Pulse emits a graduation APPROVAL_REQUEST
    — a plain-language Beacon DM (see UX below). On Larry's YES, the registry
    record flips state→graduated, graduated_at stamped. On no/no-response, stays
    probation.
  - Demotion: on any failed execution OR Larry-correction of a graduated
    template, immediately flip state→probation, reset clean_streak=0, stamp
    last_larry_correction_at. This is automatic, no approval needed (losing trust
    never needs a gate).

## Graduation-approval UX (Larry's hard requirement: clear + plain-language)

The approval DM names, for EACH pattern, in plain English with NO enum/jargon:
  - WHAT it is ("When the CEO-digest service drifts out of install...")
  - WHAT Pulse would now do automatically ("...Pulse re-installs and restarts it")
  - WHY it's safe ("It's a reversible re-install; it has done this 4 times, every
    time correctly, and you never had to step in.")
  - The TRACK RECORD ("4/4 clean over the last 11 days.")
Then a single clear approve action per pattern (or a batched "approve all"). Reuse
the existing approval-request machinery (scripts/approval*.py / beacon_approval_
handler.py) — do NOT invent a new approval channel. Render must fall through to a
safe raw form if a field is missing (per the render-layer-human-translation rule).

## Out of scope
- The broader periodic checks (I, III, IV, VI–X) keep their current behavior.
- No change to the cadence tiers, the 5 mandatory checks' detection logic, or the
  PRIME DIRECTIVE ratio accounting (beyond the intervention_id tagging fix).
- No auto-rotation of credentials, no money actions — permanent_guard floor.

## Implementation sequencing (A | B | C) — Forge note, 2026-06-03

Beacon split delivery of this brief into three dependency-ordered PRs (the data
foundation gates the rest):

- **A — data foundation (THIS PR, `pulse-triage-layer-check0-001`).**
  `config/auto-fix-patterns.json` registry + the ledger-tagging fix
  (`canonical_intervention_id()` + the `--template`/`--detail` CLI path on
  `cycle_prime_ledger.py` + the cycle-prompt contract that calls it) + this brief
  + the spec reconcile. After A, every new intervention is template-tagged so
  Check V can compute a real per-template streak.
- **B — Check 0 durable triage.** `scripts/alert_triage_state.py` +
  `~/agents/state/alert-triage.json` lifecycle + the data-driven § 6.6 decision
  table reading the registry A created. Dispatched separately against merged A.
- **C — promotion loop.** Check V threshold-unify (to 3 clean consecutive) +
  registry wiring (graduate/demote operate on `config/auto-fix-patterns.json`) +
  the plain-language graduation-approval path. Dispatched separately against
  merged A.

### Guard-list reconcile decision (A): the registry SUPERSEDES the standalone list

`config/action-template-guard-list.json` does not exist on disk; `pulse_check_v`'s
`load_guard_list()` already treats an absent file as "no guarded templates." Rather
than create a second list that can disagree with the registry, the registry is the
single source of truth and the **guarded set is a derived view**, computed at read
time as:

```
guarded = { r.template for r in registry.patterns
            if r.state != "graduated" or r.permanent_guard }
```

i.e. a template is guarded (ask-first) unless it has been graduated AND is not a
permanent_guard floor template. C wires Check V to derive the guarded set from the
registry via this rule; no standalone `action-template-guard-list.json` is created.
