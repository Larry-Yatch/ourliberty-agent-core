# D3-Forge — commit 4 plan

**Status as this plan is written (end of 2026-05-11 session):** D3 commits 1–3 shipped + live + smoke-tested. Dispatch back-channel and Larry-approval gate work end-to-end. Commit 4 ships the Forge full flow — preflight protocol, worktree machinery, session-id resume, clarification routes, real `gh pr create` plumbing.

This is the biggest commit of D3 by design. Realistic pacing: **2–3 sessions of focused design + code + verification**, possibly split if the live test surfaces issues.

---

## Scope reminder

Per the D3 design (Option C, signed off pre-implementation): D3 ships the dispatch chain. D3.5 ships the review chain (Forge → Mirror → Beacon report). Commit 4 finishes D3's *dispatch* half.

**Wired in commit 4:**
- Forge's preflight protocol (read spec, decide ack-proceed / clarify / reject) before any code is written
- The clarification protocol end-to-end (Forge writes `forge-question` outbox → notifier already routes it → Beacon's relay or escalation logic → `beacon-clarification` outbox → watcher delivers with `--resume` to Forge's preflight session)
- Worktree creation per task (currently gated to `agent_id == 'main'` in `agent_runner.py`; lift the gate to a per-agent flag in `agent-models.json`)
- Session_id resume in the watcher's dispatch path (today only Beacon's bot uses `--resume`; the inbox-dispatch path doesn't)
- Forge's `gh pr create` flow (branch → commit → push → PR-open)
- Forge's `CLAUDE.md` extended with the preflight discipline section and clarification marker convention
- Beacon's `CLAUDE.md` extended with the clarification-vs-modification fork heuristic (when to answer in-scope vs escalate to Larry as a fresh approval)
- ReadWritePaths fix on `ourliberty-inbox-watcher.service` to include `~/agent-repos/` (blocker — Forge worktrees go there)
- Refined notify-prompt template (the Pulse over-run in commit 2's smoke showed naked notifies waste tokens)

**NOT in commit 4:**
- Mirror review (D3.5)
- Beacon → Larry completion-summary DM (D3.5)
- Sentinel systemd timer (commit 5)

---

## Component inventory

### New scripts

- **`scripts/forge_preflight_handler.py`** — pure-logic library, mirrors `beacon_approval_handler` shape. Owns: `parse_forge_marker` (extracts `=== CLARIFY_REQUEST === {json} === END_CLARIFY_REQUEST ===` or `=== PROCEED === === END_PROCEED ===` or `=== REJECT === {json} === END_REJECT ===` from Forge's claude output), state transitions for pending-clarifications (per-task counter increments, max-clarifications enforcement, dead-letter on exhausted), the relay-back-to-Beacon dispatch.

  Open design Q for the session: should the markers be parsed by the **outbox_notifier** (which sees Forge's outbox after watcher writes it) or by **agent_runner.run_claude** as it captures Forge's stdout? Leaning notifier — same shape as Beacon's marker handling, no new injection point. The notifier already has the routing logic; this just adds marker recognition before deciding source-suffix.

### Modified scripts

- **`scripts/agent_runner.py`** — three changes:
  1. **Worktree gate.** Today line ~1066 gates worktree creation on `if agent_id == 'main':`. Replace with `if models_config[agent_id].get('worktree_enabled'):` reading from `agent-models.json`. Forge gets `worktree_enabled: true`; others remain `false`.
  2. **Session_id resume in dispatch path.** Today only the per-agent telegram bots call `--resume`; `agent_runner.run_claude` accepts `session_id` param but it's not threaded through from the watcher. Wire `task.get('session_id')` → `agent_runner.run_claude(session_id=...)`. Verified upstream wiring at lines 491 + 627–629 + 644.
  3. **`task_stem` already in use post-D2.5** for the in-flight registry. No change there.

- **`scripts/inbox_watcher.py`** — pass `session_id` from task envelope to `agent_runner.run_claude`. ~3 lines.

- **`scripts/outbox_notifier.py`** — extend to parse Forge's preflight markers:
  - `=== CLARIFY_REQUEST ===` → rewrite outbox `source` to `forge-question`, notify routes to Beacon (existing logic handles `*-question` correctly).
  - `=== PROCEED ===` → archive the preflight outbox; the watcher will pick up a re-dispatch of the same task with `phase=build` (the worktree persists, session_id chains).
  - `=== REJECT ===` → dead-letter the dispatch back to Beacon with the rejection reason.

  Also: **refined notify-prompt framing.** Today the prompt is `"Task result from {agent}: SUCCESS\n\n{output}"`. Receiver agents (per Pulse's over-run in commit 2) interpret this as new work. Replace with:
  ```
  [Inter-agent notify | intent={intent} | from={sender}]
  
  This is an automatic delivery of an outbox result. Your job: read it, journal it if material, do NOT generate new work unless explicitly asked.
  
  Sender's output:
  ---
  {output}
  ---
  ```
  Plus specific framing for `clarification-response` (tells Forge to resume preflight with the new context) and `dead-letter` (tells Beacon a dispatch failed).

- **`agents/forge/CLAUDE.md`** — substantial. New sections:
  - **Preflight discipline.** When a task arrives in your inbox: (a) read the spec; (b) read referenced files in the worktree; (c) check that referenced files exist; (d) check that the goal is buildable as described. Then decide one of three: `PROCEED` (have enough info), `CLARIFY_REQUEST` (specific question for Beacon), `REJECT` (spec is fundamentally not buildable).
  - **Marker formats.** Mirror Beacon's marker convention but with Forge's intents. Required fields per marker type.
  - **Build phase.** After `PROCEED`, work in the worktree. Commit using conventional-commit style. Push to `forge/<task-id>` branch. Open PR with `task.pr_title` and `task.pr_body_template`. Body should include the systemctl / grep / diff output captured during the build.
  - **Clarification budget.** `max_clarifications` defaults to 3. If you exhaust without reaching PROCEED, the bot dead-letters the dispatch back to Beacon. Use clarifications surgically.
  - **Out of scope.** Don't touch repos outside `target_repo`. Don't modify files outside `changed_files` unless the work requires it (and if so, surface as a clarification first).

- **`agents/beacon/CLAUDE.md`** — extend the existing D3 section with:
  - **Handling Forge clarifications.** When a `notify-*` with `source=forge-question` lands in your inbox (or a clarification arrives via the watcher), decide: in-scope answer (you have enough to clarify without changing the plan) OR escalate to Larry (the question implies a plan modification — DM Larry with the situation + proposed revised plan as a new APPROVAL_REQUEST).
  - **Forge rejections.** When `intent=clarification-exhausted` or `source=forge-result` with `exit_code != 0`, journal the failure and DM Larry with the situation.

- **`config/agent-models.json`** — add `worktree_enabled: true` to Forge's entry; leave others `false`. Add `allowed_repos: ["ourliberty-agent-core"]` to constrain Forge.

### New systemd / config

- **`systemd/ourliberty-inbox-watcher.service`** — add `~/agent-repos` to `ReadWritePaths` (blocker for live deployment).

### Tests

- **`scripts/tests/test_forge_preflight_handler.py`** — marker extraction (PROCEED / CLARIFY_REQUEST / REJECT), counter increments, max-clarifications dead-letter, source-suffix derivation.
- **`scripts/tests/test_outbox_notifier.py`** extended — new fixture cases for Forge markers + refined notify-prompt template.
- **`scripts/tests/test_agent_runner.py`** extended (if exists; else new) — worktree gate per-agent, session_id resume threading.
- Integration test: dispatch a synthetic Forge task → preflight + clarify cycle → ack-proceed → build phase. Mocked `gh pr create`.

---

## Architectural calls that need verification before coding

These are mostly answered by the D3 design session, but tomorrow's session should re-confirm in light of what we learned in commits 2 + 3:

1. **Marker parsing location.** Notifier (post-watcher) vs agent_runner (in-process). **Default: notifier.** Same shape as Beacon's marker handling.
2. **Worktree config source.** Per-agent flag in `agent-models.json` vs per-task `worktree: true`. **Default: agent-models.json.** Forge always uses worktrees; future per-task override possible.
3. **Worktree path layout.** `~/agent-repos/<repo>/.worktrees/<task-id>/` matches upstream convention.
4. **Branch naming.** `forge/<task-id>` — matches in-flight registry stem.
5. **gh authentication.** Already authenticated on droplet as `Larry-Yatch` (Phase A). Forge's `gh pr create` should work without setup.
6. **PR base branch.** `main` for now. Future: per-repo configurable.
7. **Session_id propagation.** Watcher reads `task.get('session_id')` and passes to `agent_runner.run_claude`. agent_runner already supports `--resume` (audit Section 5.1).
8. **Resume on PROCEED.** When Forge writes `PROCEED` outbox + the watcher re-dispatches the same task with `phase=build`, should the watcher use `--resume` on Forge's preflight session? **Yes** — preserves her reading of the spec. Same session_id chains through preflight → build.
9. **Refined notify-prompt framing.** Confirmed needed; template above is the starting point. May iterate on first live test.

---

## Sequencing (commit 4 may itself become 2 commits)

If commit 4 gets too big to land cleanly, split at the natural boundary:

**Option A — single commit:** D3-forge ships everything together.
**Option B — split:** 4a = preflight + clarification routes (no worktree yet), 4b = worktree + gh pr create.

**Lean Option B if and only if** the first session's design conversation reveals the worktree + PR work is going to be substantial enough to deserve its own design pass. Commit 4a's value is real: it makes the clarification protocol work end-to-end (Forge can decide PROCEED/CLARIFY/REJECT in a no-op manner) without the worktree complexity.

---

## Verification plan

Same shape as D2/D2.5/D3 commits 1–3:

**Per-commit unit tests:**
- All existing 129 tests still pass.
- New tests for the preflight handler, refined notify framing, watcher session_id wiring.

**Live smoke (planned target: the same `watchdog-doc-fix-001` task Beacon already proposed during D3-approval smoke):**
1. Pre-deploy check: `ReadWritePaths` on inbox-watcher service includes `~/agent-repos/`. If not, the watcher restart with `git reset --hard` + `systemctl restart` would fail the first Forge dispatch.
2. Larry messages Beacon "*revisit the watchdog-doc-fix-001 plan — propose a fresh marker, this is the real run, not a smoke test*".
3. Beacon emits marker.
4. Bot DMs Larry the formatted approval.
5. Larry replies `approve`.
6. Bot dispatches to Forge's inbox via `safe_write_inbox` with `phase=preflight`.
7. Watcher picks up. Worktree created at `~/agent-repos/ourliberty-agent-core/.worktrees/watchdog-doc-fix-001/`.
8. Forge processes the preflight. Reads the spec, examines `docs/operating-manual.md`, runs `systemctl is-enabled ourliberty-watchdog.timer` (per her preflight spec). Decides PROCEED.
9. Outbox emitted with PROCEED marker. Notifier sees, archives, re-dispatches with `phase=build` and `--resume`.
10. Forge writes the diff in the worktree, commits, pushes `forge/watchdog-doc-fix-001`, runs `gh pr create`.
11. Forge's final outbox arrives. Notifier sees `source=beacon`, writes `notify-*` back to Beacon with the PR URL.
12. Beacon's watcher picks up the notify. Beacon journals.

**Deliberately ambiguous spec (per Call 6 + design Call 18 follow-up):** after the watchdog path completes, drop a second test task with a fuzzy `prompt` field. Verify Forge writes a `CLARIFY_REQUEST` outbox instead of plowing forward. Verify the clarification routes to Beacon, Beacon's response routes back, Forge resumes preflight with new context.

**Cost estimate:** $1–2 for the full live test (Forge invocations are Opus per `agent-models.json` defaults, more expensive than Sonnet).

---

## Pre-deploy checklist (do not skip)

Before `git push` + droplet sync + service restarts:

- [ ] `ReadWritePaths` on `ourliberty-inbox-watcher.service` includes `~/agent-repos`.
- [ ] `agent-models.json` updated with Forge's `worktree_enabled: true` AND `allowed_repos`.
- [ ] No in-flight tasks (`ls ~/agents/state/in-flight/`) — restart will mark any as forfeit.
- [ ] Trust policy still ships empty rules (default `force_ask`). Don't enable auto-approve carve-outs in the same commit as new behavior.
- [ ] gh authenticated on droplet (`gh auth status` shows `Larry-Yatch`).
- [ ] Worktree path `~/agent-repos/ourliberty-agent-core/` exists OR is creatable.
- [ ] Local test suite passes (`python3 -m unittest discover scripts/tests/`).
- [ ] Droplet test suite passes after `git reset --hard origin/main`.

---

## Estimated depth

**Commit 4 alone, Option A:** ~3 hours of focused work + 30 min for live test. Plus the design conversation upfront (probably 30–45 min of architecture discussion before coding).

**Commits 4 + 5 together in one session:** unrealistic. Recommend splitting commit 5 (sentinel timer) to a separate short session afterward.

---

## Risk flags

- **First time Forge actually writes code to a real repo.** Even with the spec being doc-only, this is the highest-risk single commit of D3.
- **First time the system creates worktrees.** Path collisions, gitignore behavior, branch checkout edge cases.
- **First time `gh pr create` fires from the agent OS.** Token scope, branch protection rules (none on this repo), PR template behavior.
- **Forge's CLAUDE.md changes are substantial** — she might interpret instructions in unexpected ways. Smoke test deliberately uses a clear spec to minimize surprise.
- **Cost-of-overrun risk.** Forge defaults to Opus; a misframed prompt could burn $1+ in a single run. Mitigated by `max_clarifications` budget + the refined notify prompt.

---

## What to read first when picking up commit 4

1. **`docs/operating-manual.md` Part II, the most recent two phase entries (D2.5 + D3 commits 1–3).** The D3 entry has the full state.
2. **`docs/upstream-audit.md` Section 6 — Flow 3 (Beacon → Forge dispatch)** — upstream code references for the worktree + dispatch patterns.
3. **`docs/upstream-audit.md` Section 5.1 + 5.3** — what `agent_runner.run_claude` already does vs. what we need to wire (session_id resume, worktree gate, etc.).
4. **`scripts/agent_runner.py` lines ~1064–1090** (the worktree branch in upstream's pattern).
5. **This plan doc.**
