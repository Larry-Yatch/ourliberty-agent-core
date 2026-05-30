# Rate-limit / rotation resilience — project brief

Umbrella spec for build sequence `rate-limit-resilience-001`. Three serialized PRs
(C -> A -> B). All three touch `scripts/agent_runner.py`, so they MUST run in series
(not parallel) to avoid auto-merge conflicts.

## Motivation

On 2026-05-29 the droplet hit repeated agent stalls that surfaced as "rate limits."
Root cause was NOT quota exhaustion (rolling-5h usage sat at ~3% of the 10M-token
ceiling). The actual failure was the account-rotation scheduler (spec 6.3, shipped
2026-05-29) routing traffic to Tier 2 when Tier 2's OAuth token had silently expired,
producing a recurring `auth_401 'Invalid authentication credentials'` storm with no
circuit-breaker — every dispatch in the active-tier window failed Tier 2, fell back to
Tier 1, and repeated every ~90s. Larry disabled rotation (`rotation.enabled=false`,
commit 8b0e69a) to stop it.

## Hard constraints (apply to ALL steps)

- **Do NOT enable rotation.** `config/agent-models.json:rotation.enabled` MUST remain
  `false` in every PR. Re-enabling is a separate human-approved decision after Larry
  validates these fixes. A PR that flips it to true must be rejected.
- Default-OFF / behavior-preserving at merge wherever a new mechanism is added.
- Follow repo conventions: no emoji in code/docs/commits; regression gate must pass
  (no new test failures vs main); pre-existing failures are out of scope.

## Steps

- **C** (`rate-limit-resilience-C-ledger-brief.md`) — ledger completeness. Foundation:
  makes the rate-limit observation ledger capture the signal A's circuit-breaker and
  Check VIII depend on. Root step.
- **A** (`rate-limit-resilience-A-rotation-fix-brief.md`) — rotation auth hardening +
  Tier 2 keep-alive. Depends on C.
- **B** (`rate-limit-resilience-B-resume-healer-brief.md`) — auto-resume paused-on-tier1
  tasks. Depends on A.
