# Mirror bug-hunt gate — soak assessment (the "looking" before Phase 2)

The bug-hunt gate shipped 2026-06-08 (PR #398, squash `c82e927`). It is **safety-first
blocking** in production, and its **false-positive rate on clean PRs was never measured**
before ship. This doc defines the assessment that decides whether the gate is working
well — and gates the move to Phase 2 (the Pulse audit→distill→corpus self-tuning loop).

## Trigger
Volume-based, **not** a date: fire once the gate has reviewed **N = 15** PRs since
go-live. A Pulse check polls the count; on crossing N it runs the assessment, DMs Larry
once, and writes a sentinel so it never re-fires. (N is tunable in the check.)

## What "working well" means — the metrics `assess_gate.py` computes

Source: mirror outbox archives (`~/agents/outboxes/mirror/.archive/*.json`, the
`result` text + `revision_count` + verdict marker) and/or `chain_events`, filtered to
`completed_at >= go-live`.

1. **False-positive proxy (the headline risk).** Of the bug-hunt-attributable
   REVIEW_REVISION findings (correctness/security/data-loss flavored — i.e. NOT the
   `Regression gate: ...` test-failure findings, which are the pre-existing gate),
   how many did Forge's revision genuinely act on vs. push back on / Mirror drop on
   re-review? A high "dropped/pushed-back" share = noisy gate.
2. **Loop-health.** First-pass-PASS rate since go-live vs. the **77%** pre-gate
   baseline. Revision rounds per task vs. the pre-gate distribution (was: 28×1, 8×2,
   0×3). A large drop in first-pass-rate or a spike in rounds = the gate is too eager.
3. **Catch signal.** Any bug the gate blocked (REVISION/HALT citing a bug-hunt lens)
   that would plausibly have merged before — the value side.
4. **Volume gate.** If `< N` reviews have flowed through, DO NOT decide — extend the
   soak. Deciding on < ~15 reviews is noise.

The assessment prints these + a few example findings (so Larry can eyeball quality)
and a recommendation.

## Decision (Larry's call — the DM presents it, does not auto-act)
- **Keep as-is** — FP proxy low, loop-health intact → the gate is working.
- **Dial back** — FP proxy high or first-pass-rate dropped materially → raise the
  blocking thresholds in `mirror-bughunt-lenses.md` (one-edit change), re-soak.
- **Greenlight Phase 2** — gate is working AND stable → build the Pulse
  audit→distill→corpus self-tuning loop. **This is development work done in a desktop
  Claude Code session, not by Beacon.** The DM says exactly that.

## The DM (must be unambiguous)
The Telegram DM states, in plain language: (a) the bug-hunt gate has now reviewed N
PRs; (b) the assessment numbers (FP proxy, first-pass-rate vs 77%, catches); (c) the
recommendation; (d) the explicit next step — *"To act: open Claude Code on your
desktop in ourliberty-agent-core and say 'proceed with the bug-hunt gate Phase 2'
(or 'dial back the gate thresholds'). Context: memory mirror-bughunt-gate-project."*
It does NOT start any work itself.
