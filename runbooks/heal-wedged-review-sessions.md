# Runbook: heal_wedged_review_sessions

**Purpose.** Reap Mirror/Forge review `claude -p` sessions that wedge *after*
their work is done. Motivated by the 2026-06-03 incident: Mirror reviewed
PR #297, emitted `=== REVIEW_PASS ===` (the PR auto-merged), then her `claude -p`
process froze ~30 min in a harness background-Bash poll loop (the self-matching
`until ! kill -0 $(cat <vanished-file>; echo $$)` that never exits), holding a
fleet slot + a live Opus session long after the work was in hand.

`heal_zombie_main_workers.py` PATTERN B describes this exact failure shape but
is scoped to main/pulse workers (sysprompt-main filter, `/tmp/wt-main-*` cwd, 4h
etime floor), so it never sees review agents in `~/agent-worktrees/wt-{mirror,forge}-*`.
This healer closes the gap for the review tier.

**Cadence.** Every 5 min via `ourliberty-heal-wedged-review-sessions.timer`
(`OnBootSec=4min`, `OnUnitActiveSec=5min`). One-shot — fires, reports, exits.
Silent unless it reaps or alerts.

**Script.** `scripts/heal_wedged_review_sessions.py`. Zero-LLM: pure `/proc`
reads, JSONL greps, signals, and `git worktree`. It never spawns a `claude`
subprocess — a reaper that spun up a model to decide would burn the very slot it
is trying to free.

---

## Scope

`claude` processes whose cwd is `~/agent-worktrees/wt-mirror-*` or `wt-forge-*`.
For each, the activity log is the Claude Code session JSONL under
`~/.claude/projects/<slug>/<session>.jsonl` (slug = cwd with `/` → `-`). The
JSONL mtime is last-activity; only **assistant-authored** lines are matched for
the tier's terminal markers (reused from the handler modules so renames
propagate). Role filtering is essential: every session reads its own CLAUDE.md
at startup, and that manual's examples contain the literal marker delimiters,
which persist as user/tool_result lines — a whole-file grep would treat them as
a verdict from startup onward. Markers matched:

- **Mirror:** `REVIEW_PASS` / `REVIEW_REVISION` / `REVIEW_ESCALATE` /
  `REVIEW_EMERGENCY_HALT` (`mirror_review_handler.MARKER_KEYWORDS`).
- **Forge:** `PROCEED` / `CLARIFY_REQUEST` / `REJECT`
  (`forge_preflight_handler.MARKER_KEYWORDS`) PLUS the build/revision exit
  preambles `PR opened:` / `PR updated:` / `Revision ` (agents/forge/CLAUDE.md).

---

## The two cases

### Case 1 — provably-done (AUTO-REAP, day one)
Terminal marker present in the JSONL **AND** process alive **AND** JSONL idle >
`marker_grace_seconds` (default 300). The marker is proof the work is in hand, so
reaping is zero-false-positive: SIGTERM→(5s)→SIGKILL the process tree,
`git worktree remove --force` + prune the stale worktree, emit a `healer_fire`
chain event + a **closure** notify.

### Case 2 — silent (NO marker)
JSONL idle > `silent_grace_seconds` (default 900) **AND** process alive **AND**
no marker. Starts in **alert-only** mode — escalate to Beacon, do **not** kill.
A confidence ladder (the Pulse promotion-ladder shape — a tail-read
consecutive-success streak over `~/agents/state/review-reaper-confidence.json`)
tracks outcomes:

- The session later **emits a marker or resumes activity** → FALSE positive
  (it was live work) → reset streak + demote to alert-only.
- The session **stayed dead with no marker** → TRUE positive → increment streak.

After `streak_to_promote` (default 3) consecutive true positives, Case 2
graduates to auto-reap (one-time graduation closure notify). Any miss
auto-demotes back to alert-only — including a final pre-kill recheck
(`_resumed_since_scan`) that aborts the kill if the session resumed between scan
and gate.

> **Verification runs first each sweep.** `run_cycle` resolves outstanding Case-2
> alerts before classifying live candidates, so a graduation that crosses the
> threshold this sweep takes effect for the new candidates below it.

---

## Config (Pulse-Check-tunable — no hand-picked thresholds)

`config/review-reaper-rules.json`:

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Master switch (config-level disable). |
| `marker_grace_seconds` | `300` | Case 1 post-marker idle floor. |
| `silent_grace_seconds` | `900` | Case 2 no-marker idle floor. |
| `streak_to_promote` | `3` | Consecutive true positives → Case 2 auto-reap. |

Missing file / malformed JSON / bad value → conservative built-in defaults
(per-key validated, never raises). Pulse-Check adjusts the JSON; nothing is
hand-picked in the script.

---

## Coexistence with heal_zombie_main_workers.py (no double-kill)

Domains are disjoint by construction: the zombie healer acts only on
sysprompt-main procs whose cwd is `/tmp/wt-main-*` (or `(deleted)`); this healer
acts only on `~/agent-worktrees/wt-{mirror,forge}-*`. `agent_tier_for_cwd`
additionally hard-skips the `/tmp/wt-main-` prefix so a future convention drift
can't make both target the same PID.

---

## Safety gates

1. Kill-switch file `~/agents/healers.disabled` absent (shared across healers).
2. `enabled: true` in `config/review-reaper-rules.json`.
3. Worktree removal is guarded: resolves the canonical repo via
   `git rev-parse --git-common-dir`, refuses to remove the canonical repo
   itself, and only removes when the canonical repo is on `main`.

---

## Operate

**Disable everything immediately:** `touch ~/agents/healers.disabled`.

**Disable just this healer:** set `"enabled": false` in
`config/review-reaper-rules.json` (next fire reads it).

**Inspect state / streak / pending:**
```bash
cat ~/agents/state/review-reaper-confidence.json     # mode, streak, executions, pending
tail ~/agents/logs/heal-wedged-review-sessions.log   # per-sweep HEARTBEAT line
cat ~/agents/blackboard/heal-wedged-review-sessions.heartbeat
journalctl -u ourliberty-heal-wedged-review-sessions.service --since '-1h'
```

**Force a one-off run (dry of nothing — it acts):**
```bash
python3 ~/agent-core/scripts/heal_wedged_review_sessions.py
```

**Reset the confidence ladder** (e.g. after a string of bad alerts you don't
want counting): delete `~/agents/state/review-reaper-confidence.json`; it
rebuilds in alert-only mode with an empty streak.

---

## Notifications

- **Case 1 reap / Case 2 auto-reap / graduation** → `route='closure'` (FYI
  confirmation, not an action ask) — Notify-on-outcome, never at detection.
- **Case 2 alert-only candidate** → `route='escalate'` with a suggested-action
  line (inspect the worktree + session log; kill the pid if genuinely wedged).
- Every reap also writes a `healer_fire` chain event
  (agent `heal-wedged-review-sessions`).

---

## Install

Auto-installed by `heal_systemd_install_drift.py` (it globs `systemd/*.{service,timer}`).
Manual:
```bash
sudo cp ~/agent-core/systemd/ourliberty-heal-wedged-review-sessions.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ourliberty-heal-wedged-review-sessions.timer
```
