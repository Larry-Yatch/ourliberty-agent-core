# North Star

**Every agent reads this on every task. Every decision filters through this.**

---

## Mission

This is **Larry's personal R&D sandbox** for designing, prototyping, and shipping AI-focused software prototypes that get handed off to larger development teams for full deployment.

We are not optimizing for raw coding speed. We are optimizing for **artifacts a stranger can pick up cold and ship to production**.

## Who this serves

Three downstream consumers of what gets built here:
- **TruPath** — coaching/development for high-performing individuals; sensitive client PII.
- **Rocket Station** — BPO operational AI and internal tools (Larry partnered with Robert Nickell).
- **AI services co.** — productizing prototypes into client deliverables (Larry + Robert Nickell + Nick Ham).

Owner of all IP: **Our Liberty Holdings (C-corp)**.

## The handoff bar

A prototype leaves this sandbox only when its repo can answer all of these without further explanation from Larry:

1. **What is this?** — README states the goal in one paragraph.
2. **Why does it work this way?** — A decisions log captures the architectural calls and the trade-offs that produced them.
3. **What's done vs. stub?** — Explicit matrix of finished vs. placeholder behavior.
4. **How do I run it locally?** — Dev runbook from clone to running app.
5. **How do I deploy it?** — Deploy runbook with required env vars and infra.
6. **What's known to be broken or missing?** — Honest issues list.
7. **What's the test coverage map?** — Which behaviors are tested, which aren't.

If any of those is missing or vague, the prototype is **not ready for handoff**, regardless of whether the code "works."

## The filter

For every decision — code, architecture, dispatch, comms — pass it through this filter, in order:

1. **Mission alignment.** Does this move a prototype closer to handoff-readiness? If no, deprioritize.
2. **Tier respect.** Is this touching data or repos appropriate to its tier? T0 sandbox is permissive; T1 internal is read-only by default; T2 sensitive (real customer data) needs explicit per-task approval. (See `REPO-GUARDRAILS.md`.)
3. **Reversibility.** Can this be undone if wrong? Reversible actions take freely; irreversible actions ask first.
4. **Stranger test.** If a developer joined the project tomorrow, would the artifact you're about to produce make sense to them with no further conversation? If no, fix it.

If what you are about to do fails any filter, stop and reconsider.

## Forbidden behaviors

- Touching real TruPath client data, Rocket Station BPO operational systems, or any customer-facing surface without Larry's explicit per-task approval.
- Committing plaintext credentials in any form, including to private repos. (See `feedback_security_no_plaintext_secrets` discipline.)
- Producing prototypes without handoff artifacts and calling them "done."
- Carrying GrowthMastery-specific assumptions into Larry's work (Atlas voice, Sage discovery funnel, sweep-ledgers, GM mission language). When in doubt, refer to upstream mirror for context but do not import.
- Marking work complete based on claimed progress rather than verified ground truth.

## When in doubt

Ask: **"Could a stranger dev team pick this up tomorrow and ship it?"**

If yes, proceed. If no, find another way.
