# Pulse uncommitted-local-main guard — detect-and-alert-loudly

**Status:** draft, awaiting Larry review
**Origin:** Pulse iter 99 G-rule cycle (2026-05-29). PR #183 covers the runtime-allowlist auto-commit path; this spec covers the residual class: non-runtime-allowlist files left uncommitted or unpushed on local `main` after an interactive Pulse session.
**Type:** safety guard; no behavior change to existing auto-commit logic.

## Problem

After PR #183 ships, sync.service will auto-commit + push files matching the runtime allowlist (whitelisted Pulse runtime artifacts). But an interactive Pulse session can still leave **non-allowlist** files dirty on local `main`:

- Manual edits Larry made mid-cycle that Pulse didn't intend to commit.
- Pulse-emitted artifacts that escaped the allowlist (a new file type, a typo'd path).
- Aborted partial commits where the cycle ended before staging completed.

These files are invisible to sync.service (correctly — they're not on the allowlist) and silently sit on local `main`, diverging from origin. The next cycle then trips the "diverged main" G-rule, which is the symptom we're trying to prevent at the source.

## Goal

When an interactive Pulse session ends (or run_cycle.sh wraps post-cycle), **detect** any non-runtime-allowlist uncommitted/unpushed files on local `main` and **alert loudly** — yellow severity, Telegram DM to Larry, surfaced in the next operator status check.

Critically: **do NOT auto-push**. Auto-pushing non-allowlist files would bypass Mirror review and is exactly the failure shape doctrine-of-doctrine warns against. The guard's only job is to make the residue visible.

## Out of scope

- Auto-commit / auto-push of non-allowlist files. That stays manual + Mirror-reviewed.
- Pre-cycle guards (those exist already via the wrong-branch + diverged-main checks in run_cycle.sh).
- Any change to PR #183's runtime-allowlist auto-commit path.

## Proposed shape

Two complementary detection surfaces, both read-only:

1. **`run_cycle.sh` post-cycle check.** After the cycle's commit/push phase completes, run `git status --porcelain` on local `main`. If non-empty AND no path matches the runtime allowlist, emit a yellow-severity heal entry with the dirty paths + a one-line remediation hint ("review + commit + push, or stash"). Same alert pipe as existing run_cycle.sh diagnostics — surfaces in the next operator status check and (via larry_alerts) Telegram DM.

2. **Interactive-session end-check.** When an interactive Pulse session terminates (either via clean exit or harness teardown), invoke the same detection. The hook lives in the Pulse session-wrapper (TBD whether `pulse/run_cycle_interactive.sh` already has a teardown surface or one needs to be added — flag for Forge to investigate at preflight).

Both paths share one helper (suggested location: `scripts/check_local_main_residue.sh` or inline in `scripts/run_cycle.sh` if small enough). Single source of truth for the allowlist match logic — reuse the existing pattern matcher PR #170 / commit `469eed2` established.

## Acceptance criteria

- A test fixture where Pulse leaves a non-allowlist file dirty on local `main` triggers a yellow heal entry naming the file path.
- A test fixture where Pulse leaves only allowlist files dirty (the PR #183 happy path) does NOT trigger the guard.
- A test fixture where local `main` is clean does NOT trigger the guard.
- No code path in the guard executes `git add`, `git commit`, `git push`, or any mutation. Read-only by construction.
- The yellow alert reaches Larry via the standard `larry_alerts` Telegram pipe within one operator-status-check cycle.

## Risks / tradeoffs

- **False positives** if a Larry-edited file is mid-flight and intentionally uncommitted. Mitigation: yellow severity (not red), and the alert is informational — Larry can dismiss / address at his pace.
- **Allowlist drift.** If the runtime allowlist gains a new entry, the guard's allowlist matcher must stay in sync. Mitigation: single shared helper (see "Proposed shape" point 1) so the allowlist lives in one place.

## Enforcement

The guard itself IS the enforcement mechanism — it's a detection rule. The doctrine-pairing for this rule: any future change to the runtime allowlist must update the shared matcher (single source of truth), enforced via Mirror review checklist on PRs touching `scripts/check_local_main_residue.sh` (or equivalent) — Mirror flags any direct re-implementation of allowlist-matching logic in a parallel location.

## Open questions for Larry

1. Yellow alert is the right severity, right? Not red (no production impact), not green (it IS a divergence-class precursor).
2. Should the guard ALSO check for **unpushed** commits on local `main` (clean working tree but ahead of origin by N commits)? That's the same divergence class but a different shape than uncommitted files. I lean yes — same code path, marginal cost.
3. Where does the interactive-session teardown hook live? Need Forge to investigate at preflight.
