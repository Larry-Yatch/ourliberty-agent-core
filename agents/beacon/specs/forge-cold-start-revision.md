# Forge Cold-Start Revision — making session-less PR revisions mechanical, briefed, and enforced

**Status:** draft for build
**Author:** Claude Code (laptop session), at Larry's direction
**Date:** 2026-06-23

## 1. Purpose

PRs authored on the laptop by **Claude Code** (branch prefix `claude/`, pushed under Larry's GitHub identity) have already passed the local code-review gate, and are then re-checked by **Mirror** on the droplet. Two gaps in that handoff page Larry multiple times a day and leave clean PRs stalled:

- **Gap 1 — unrouted (`pipeline-stall:unrouted-pr`, e.g. PR #653):** the auto-dispatch to Mirror only fires for `forge/` branches, so a `claude/` PR never gets a review dispatched at all. It sits unrouted until Larry runs `dispatch mirror review` by hand. (#653 stalled ~1.5h on a doc-only spec, then passed cleanly once routed.)
- **Gap 2 — no-session revision (`pipeline-stall:no-session-revision`, the #645 class):** once a `claude/` PR *is* reviewed and Mirror requests changes, the revision has **no Forge build session to `--resume`** — Forge never built it. `_dispatch_revision_to_forge` hits its `if not forge_session:` guard and dead-ends.

Gap 2 is the substantive one and the centerpiece of this spec. Gap 1 is its precondition: closing Gap 1 funnels *more* `claude/` PRs into Gap 2, so Gap 2's reliability must land first.

The chain-context-durability spec (M2) already tried to close Gap 2 by routing the no-session REVISION to **Beacon's inbox** (`_route_no_session_revision_to_beacon`, intent `code-review-revision-no-session`) for an LLM-mediated re-dispatch. Verified against the live code, that mechanism does not hold:

1. The notify is written to Beacon's inbox and the notifier stops.
2. `inbox_watcher.process_task` spawns one Beacon LLM turn for the file and then **archives it unconditionally** (`inbox_watcher.py:713`) — whether or not the model emitted the re-dispatch. The entire recovery rides on a single LLM turn following an English instruction (CLAUDE.md "Shape 10"); if it no-ops, the file is gone and the chain silently ends.
3. The code backstop is dead: `heal_pipeline_stall.py` Check 6's trigger regex `_NO_FORGE_SESSION_RE` (`:234`) requires the log phrase `…REVIEW_REVISION on task X has no forge_build_session_id`, but the live path logs `NO_SESSION_REVISION task=X; routed…` (`outbox_notifier.py:4460`) — the regex never matches. And even if it fired, its "recovery" just re-writes the same LLM-dependent notify **and suppresses the Larry alert**.
4. Meanwhile Mirror's REVISION posts a `state=failure` on the **required** `mirror-review` commit status (`outbox_notifier.py:5089-5094`), pinned to the head SHA — so the PR is merge-blocked until a later PASS posts `state=success`. With the revision dead-ended, that failure sticks forever.

Net for a `claude/` PR Mirror wants revised: findings posted on the PR, a permanent failing required check, no mechanical re-dispatch, and a dead backstop = a silent stall until Larry digs in. That is #645 and #653.

## 2. Root cause

**The recovery depends on an LLM turn, with no enforcement and no working backstop.** The happy path (a `forge/` PR) carries `forge_build_session_id` and fires the whole Forge↔Mirror revision loop automatically; the session-less path drops to an English instruction that an LLM may or may not execute, behind a regex that no longer matches and an alert that is suppressed. There is nothing mechanical between "Mirror requested changes" and "the change got applied."

A second, smaller root cause sits underneath: **`claude/` PRs are never auto-routed to Mirror at all** (Gap 1), because the dispatcher gates on the `forge/` branch prefix.

## 3. Key insight — the loop already exists; only the cold-start is missing

The Forge↔Mirror back-and-forth is **not new work**. `_dispatch_revision_to_forge` (`outbox_notifier.py:4587`) already, for every `forge/` PR:

- serializes Mirror's findings into a brief (`:4570`),
- enforces a bounded `revision_count` budget (`:4589`),
- instructs commit-to-same-branch and re-review via `_dispatch_mirror_review_rerun`, threading `previous_findings` so round 2 stays coherent (`:4619`),
- threads `forge_build_session_id` forward (CARRY, `:4650`) so each round resumes,
- escalates on budget-exhaust.

All of it is built and trusted. **The one thing that breaks for a `claude/` PR is the cold-start:** round 1 has no `forge_build_session_id` to `--resume` (`:4616`), so the `if not forge_session:` guard (`:4515`) bails instead of simply starting Forge **fresh** on the branch. The session is only the *conversation* history; the git state comes from `branch` + `target_repo`, which are on the envelope regardless — a fresh Forge can check out and edit the branch fine.

So the fix is to **bootstrap the existing loop**, not build a new one: on round 1, dispatch Forge fresh (no `--resume`) with a brief rich enough to replace the missing build-conversation context; capture the fresh session id and thread it forward; from round 2 on it is the identical, already-trusted machinery.

## 4. Scope

**In scope:** the no-session branch of `_dispatch_revision_to_forge` and the round-1 brief construction in `scripts/outbox_notifier.py`; the auto-dispatch branch-prefix gate in `scripts/heal_undispatched_pr_review.py` (and the unrouted check in `scripts/heal_pipeline_stall.py`); Check 6's backstop in `scripts/heal_pipeline_stall.py`; a durable obligation ledger; the relevant agent `CLAUDE.md` handling shapes + doctrine.

**Out of scope:** Mirror's review logic and verdict criteria; what Forge builds; the trust-policy / approval gate. This spec changes *how a session-less revision is applied and how `claude/` PRs are routed* — never what work is performed or what gets approved.

## 5. The mechanisms

### M1 — Mechanical cold-start re-dispatch (replaces the Beacon route)

In the `if not forge_session:` branch of `_dispatch_revision_to_forge`, instead of `_route_no_session_revision_to_beacon`, the notifier **directly** dispatches a fresh Forge revision via `build_chain_envelope` (M1 of chain-context-durability — the sanctioned builder). The envelope is the normal revision envelope with `session_id = None` (no `--resume`) and the round-1 brief from M2. The fresh session id Forge returns is captured into its revision outbox and threaded forward so rounds 2+ resume normally — only round 1 is special.

**Decision-finding escape valve.** Mechanical findings (e.g. a missing translation row) → Forge applies and proceeds. A judgment/values finding (e.g. an id-vs-timestamp contradiction) is not Forge's to decide: the cold-start dispatch uses a phase that supports a CLARIFY/question exit (Beacon's `phase=preflight` shape) so Forge routes the decision to Beacon → Larry rather than guessing. The human is pinged only for (i) a genuine scope/values decision, or (ii) the dispatch itself failing (M4).

**Enforcement:** routing code in `outbox_notifier.py`; a test asserting the no-session REVISION path writes a **Forge** revision inbox file (fresh `session_id`, same branch) and does **not** route to Beacon's inbox.

### M2 — The round-1 cold-start brief (the centerpiece)

A resumed Forge carries its whole build conversation implicitly; a blind Forge carries nothing. The round-1 prompt must hand-deliver everything that conversation would have held, or Forge will "fix" a nit in a way that breaks the PR's purpose. It must contain:

- **(a) Provenance + framing.** "This is a Claude Code–authored PR. It is **not** your build — you have no prior context on it. Read the diff and the PR intent before editing." This replaces the existing prompt's `"Mirror has reviewed **your** build"` wording (`:4588`), which would otherwise lead a blind Forge to edit from false memory.
- **(b) Intent — the full PR body, verbatim.** The `## Why / ## What / ## Scope` Claude Code writes is the substitute for "the task Forge was originally given," and carries the governing spec references. The PR body is **not on the review/heal envelope today** (only `pr_title` is), so the cold-start path must fetch it (`gh pr view <n> --json body`) and inject it; the envelope already supports a `pr_body` field (`:4636`).
- **(c) Current state.** Explicit instruction to read the branch diff and `git log` before editing — the worktree is already a checkout of the branch, so this is available; Forge must be told to look rather than rely on memory.
- **(d) Mirror's findings, self-contained.** The structured `[severity] file line — description` block already serialized at `:4570`; ensure they stand alone (no "as discussed").
- **(e) Constraints.** Same branch, no new PR, address **only** the findings (no scope creep), preserve the stated intent.

Only round 1 pays this heavy brief; rounds 2+ inherit context via the resumed session.

**Enforcement:** a test asserting the round-1 cold-start prompt contains the provenance framing, the fetched PR body, the diff/log read instruction, the findings block, and the constraints; and a test that the `"your build"` phrasing is absent on the cold-start path.

### M3 — Durable obligation ledger

Record each no-session revision when dispatched in a state file (opened on dispatch with task_id + pr_url + head SHA; cleared on Mirror PASS / merge). This is the enforcement the English rule lacked: it lets the backstop verify the obligation actually resolved instead of scraping logs, and survives a notifier restart.

**Enforcement:** ledger writes in `outbox_notifier.py`; tests for open-on-dispatch and clear-on-PASS.

### M4 — Revive + harden the backstop

Repair Check 6 in `heal_pipeline_stall.py`: key it off the M3 ledger (a stuck open obligation past a threshold) rather than the dead `_NO_FORGE_SESSION_RE` log-scrape, and fix the regex regardless as a latent-bug cleanup. Change recovery from "silently re-deposit notify + suppress alert" to: verify the re-dispatch produced a new commit / re-review; if it has not resolved past a threshold, fire a **loud, non-suppressed** Larry alert carrying the findings + PR link.

**Enforcement:** healer tests that a stuck obligation triggers exactly one non-suppressed alert past threshold, and that a resolved one fires none.

### M5 — Auto-route `claude/` PRs to Mirror (Gap 1)

Extend the auto-dispatch to cover `claude/` head branches, not just `forge/`: in `heal_undispatched_pr_review.py` (the GitHub-truth backstop) and the unrouted check in `heal_pipeline_stall.py`. Keep the existing head-SHA-aware dedup (`headRefOid`) so a PR updated after review re-reviews on the new head — which is also what lets a fixed PR clear its sticky `mirror-review=failure` status. Optional hardening: have Claude Code **stamp** its PRs (a `claude-code` label or body trailer) so classification is positive rather than inferred from the branch prefix.

**Sequencing guard:** M5 opens the floodgate, so it must land **after** M3 + M4 make the session-less path reliable.

**Enforcement:** selection tests that a `claude/` PR past grace with no review is selected for dispatch, and that a re-pushed head re-dispatches.

## 6. Build sequence

- **S0 — dead-regex backstop fix (part of M4), standalone first commit.** Fix `_NO_FORGE_SESSION_RE` to match the current log line. Isolated latent bug; lands first regardless of the rest. `depends_on: []`.
- **S1 — durable obligation ledger (M3).** `depends_on: []`.
- **S2 — mechanical cold-start re-dispatch + round-1 brief (M1 + M2).** Replaces `_route_no_session_revision_to_beacon`. `depends_on: [S1]`.
- **S3 — harden backstop to verify-then-loud-alert, keyed off the ledger (M4).** `depends_on: [S1, S2]`.
- **S4 — auto-route `claude/` branches + head-SHA re-review (M5).** `depends_on: [S2, S3]` (reliability before floodgate).
- **S5 — agent `CLAUDE.md` shapes + doctrine.** Forge revision-phase cold-start shape; Mirror unchanged; deprecate Beacon "Shape 10"; doctrine note that a recovery must be mechanical + ledger-enforced, never an unenforced LLM turn. `depends_on: [S2, S4]`.

**File-overlap note:** S0, S2, S3 all touch `outbox_notifier.py` / `heal_pipeline_stall.py` — serialize them (linear S0→S1→S2→S3→S4→S5) rather than declaring parallel, to avoid a parallel-file-overlap REVISION.

## 7. Success criteria

- A no-session REVISION on a `claude/` PR **mechanically** dispatches a fresh, fully-briefed Forge revision on the same branch — no LLM-turn dependency, no Beacon-inbox indirection.
- The round-1 brief carries provenance framing + fetched PR body + diff/log read instruction + self-contained findings + constraints; the `"your build"` phrasing is gone from the cold-start path.
- Round 2+ resume the captured fresh session; the existing budget / re-review / escalation loop runs unchanged to a Mirror PASS (which clears the `mirror-review` status) or a loud escalation on budget-exhaust / decision-finding.
- The backstop fires on a real stuck obligation and escalates non-suppressed past threshold; the dead regex is fixed.
- `claude/` PRs auto-route to Mirror and re-review on each new head SHA.
- Replaying the #645 and #653 classes resolves end-to-end with zero silent dead-ends and zero manual `dispatch mirror review`.

## 8. Relationship to in-flight work

- **Supersedes chain-context-durability M2** (`_route_no_session_revision_to_beacon` / Beacon "Shape 10"): the LLM-mediated route is replaced by M1's mechanical dispatch. The M1/M3/M4 builder, ledger, and backstop discipline are retained and extended.
- **S0 (dead-regex fix)** is a latent bug independent of the rest and can ship immediately.
- Builds directly on chain-context-durability's `build_chain_envelope` (its M1) as the sole envelope constructor.
