# Spec-critique lenses (antagonistic spec-review gate)

> Foundations slice of `agents/beacon/specs/spec-gauntlet-gate.md` (§3.3).
> Vendored and read by **absolute path** — same rule as Mirror's bug-hunt lenses
> (`review/mirror-bughunt-lenses.md`): the runner reads it at
> `/home/larry/agent-core/review/spec-critique-lenses.md`. This is the reviewer
> prompt each lens runs against a *spec body* (`payload['prompt']`), not against
> code. It is the spec-time analogue of the per-PR bug-hunt gate: catch a flawed
> spec before Forge faithfully builds the flaw.

## Why a spec gauntlet

Beacon's specs reach Larry with zero technical review. Larry approves at CEO
altitude ("does this make sense for the business") and cannot review technical
correctness. Mirror adversarially reviews Forge's *code* against the spec, but
nothing reviews the *spec* itself — a flawed spec produces a faithfully-built
flaw that passes Mirror. These three lenses codify the interactive
draft → antagonistic-review → revise loop Larry already runs by hand.

## The fan-out

Run these three lenses as parallel reviewers over the spec body. Each lens is
given: the full spec body (`payload['prompt']`), permission to fact-check
against a read-only `origin/main` checkout, and its own attack brief below.

Each reviewer returns findings in the established block-delimiter marker shape
(the same grammar Mirror uses), NOT bare JSON:

```
=== SPEC_FINDINGS ===
{"findings": [{"lens": "S-A", "severity": "blocking", "claim": "...", "spec_quote": "...", "suggested_change": "..."}]}
=== END_SPEC_FINDINGS ===
```

Finding fields: `{lens, severity: blocking|advisory, claim, spec_quote, suggested_change}`.

- **`spec_quote` is mandatory** — a finding with no verbatim quote from the spec
  is discarded. This forces every finding to point at real text and blocks
  hand-wavy "this feels underspecified" noise.
- **Malformed reviewer output = "lens did not conclude"** — fail-open, consistent
  everywhere. A lens that emits an unparseable block is treated as having
  produced no findings for that round (surfaced on the digest as a
  non-concluding lens), never as a silent pass.

### Lens S-A — Feasibility & blast radius

Will this spec collide with the running system? Look for:
- Collisions with **live daemons, healers, machine-owned files, systemd units** —
  a spec that writes a file another daemon owns, or restarts a unit mid-flight,
  or grabs a lock a healer holds.
- **Rollback reality** — if this ships and is wrong, can it be reverted cleanly,
  or does it leave orphaned state / half-migrated data / a wedged queue?
- **Resource envelope** — cgroup memory limits, concurrency caps, disk, the
  claude-process VM-wide cap. A spec that spawns N subprocesses inside a 512M
  cgroup is infeasible as written.
- Consult the **ourliberty-graph shelf librarian** for blast radius (advisory,
  fail-safe-skip when the checkout is absent — same contract as Mirror Lens I).

### Lens S-B — Completeness & failure modes

Is the spec complete enough to build correctly, including the unhappy paths?
Look for:
- **Unhappy paths** the spec is silent on — what happens on the error branch, the
  empty input, the concurrent second caller?
- **Crash-mid-flight / restart / replay** behavior — if the process dies halfway,
  is the work lost, duplicated, or resumable? Is every state transition
  idempotent and restart-safe?
- **Missing acceptance criteria** — is each claimed behavior testable, or is a
  success criterion asserted with no way to verify it?
- Timeouts, ceilings, and bounds — does every loop / retry / wait have a cap?

### Lens S-C — Reuse, simplicity & verifiability

Is this the simplest correct shape, and can we prove it works? Look for:
- **Reinvention** — does the spec rebuild a primitive that already exists
  (an atomic-write helper, a config-override resolver, a concurrency guard,
  an existing daemon)? Name the existing part and prefer extending it.
- **A simpler shape** — is there a materially smaller design that meets the same
  goal? Over-engineered armor (cargo-culted predicates with no named consumer,
  speculative config, unused abstraction) is scope creep worth flagging.
- **Testable ACs** — can the acceptance criteria be tested deterministically in
  the credential-less regression gate (stubs/fixtures), or do they require live
  LLMs / live services to pass? ACs that can't be tested without credentials are
  a completeness gap.
- **Scope creep** — is the spec doing more than the problem statement asks?

## Round policy (summary — full spec in §3.3)

- **R1:** all three lenses attack in parallel. No blocking findings → conclude
  `passed`; advisory findings ride the challenge digest.
- **Revision (max 1):** a gate-owned `claude --print` step revises the spec
  against the blocking findings. NOT an inbox/outbox Beacon session (that would
  be re-entrant). Output is a fenced revised-spec body + per-finding responses;
  it is never written to any inbox/outbox and never parsed for an
  APPROVAL_REQUEST marker.
- **R2 re-review:** the same three lenses check BOTH "prior blocking findings
  resolved" AND "no new blocking flaw introduced by the revision diff." No
  revised body ever ships without one full re-review pass over it.
- R2 still blocking → conclude `contested`, ship to Larry with those findings
  flagged. Never a third round.

## Severity → conclusion

- Any **blocking** finding in R1 triggers the single revision round.
- **Advisory** findings never block; they always ride the challenge digest on the
  approval card so Larry sees them without them gating the dispatch.
- Terminal states, one per card while the gate is enabled:
  `passed | contested | incomplete | errored`.
