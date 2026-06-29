# Tier-1 ↔ Tier-2 switch: capability audit

**Status:** audit complete 2026-06-29. Covers what an agent session (Beacon in
particular) inherits, loses, and restores when the active Claude account
rotates Tier-1 → Tier-2 → Tier-1.

**Why this exists:** #755 fixed *one* facet of the tier switch (app-code path
resolution). This audit checks the *whole* surface so a rotation to Tier-2
leaves agents fully online — not silently degraded. It was triggered by Beacon
reporting it could not read NORTH-STAR / memory / pending-approvals "blocked on
permission grants," and proposing to add those paths to a Read allowlist.

---

## TL;DR — the corrected mental model

1. **The proposed fix (add paths to a `Read` allowlist in
   `agents/<agent>/.claude/settings.json`) is a NO-OP.** `agent_runner.run_claude`
   launches every agent session with `--permission-mode bypassPermissions`
   (`scripts/agent_runner.py`). With permissions bypassed, the Claude Code
   allow/deny lists are not enforced, so editing them changes nothing. Do not
   ship allowlist edits to "unblock" an agent.

2. **Beacon's self-diagnosis is unreliable when it is boxed.** With permissions
   bypassed, "blocked on permission grants" is not a real mechanism. At least
   the pending-approvals file is *legitimately absent* when there are zero
   pending approvals (`beacon_approval_handler.py` returns early if the file
   doesn't exist). A read failure under Tier-2 is path/sandbox/existence — never
   a Claude Code permission grant.

3. **Most of the tier-switch surface is already covered.** Auth, the
   transcript-write sandbox, and path resolution are all handled (table below).
   The genuine residual gaps are **MCP parity** and, until now, **no monitoring**
   of either.

---

## How the switch works

- `scripts/active_tier.py` holds the active tier in
  `~/agents/blackboard/active-tier.json` (`tier1` | `tier2`), with cooldown /
  rotation logic. `TIER1_HOME = /home/larry`, `TIER2_HOME =
  /home/larry/.claude-larry-personal`.
- `agent_runner.run_claude` builds the child env and sets
  `env['HOME'] = active_tier.current_home()` so the Claude CLI finds *that
  tier's* OAuth under `$HOME/.claude/.credentials.json`. The swap is **per
  child-subprocess env only** — the long-running daemons stay pinned to
  `HOME=/home/larry` via their systemd units. Nothing persists HOME=Tier-2
  across the round-trip, so the return leg (Tier-2 → Tier-1) restores the full
  Tier-1 environment automatically for every new session.
- The switch is overloaded: `HOME` selects BOTH the CLI's OAuth AND where all
  `$HOME`-relative config (settings, MCP, projects, transcripts) is read. That
  overloading is the root of every gap below.

---

## Capability surface — what a Tier-2 session inherits vs. loses

| Surface | Tier-1 | Tier-2 | Status |
|---|---|---|---|
| OAuth credentials | `~/.claude/.credentials.json` | present (provisioned by `auth_orchestrator_tier2.py`; liveness-probed weekly) | ✅ covered |
| Transcript writes (sandbox) | unit `ReadWritePaths` carves `/home/larry/.claude` | all 5 session-running units ALSO carve `…/.claude-larry-personal` | ✅ covered (2026-06 fix landed) |
| App-code state paths (`~/agents/**`) | `Path.home()/agents` | `OURLIBERTY_AGENTS_ROOT` pinned to real home in the child env | ✅ covered (#755) |
| Permission allow/deny lists | in `~/.claude/settings.json` | absent — **but `bypassPermissions` makes them moot** | ✅ N/A |
| Beacon read-in paths | `../../shared/NORTH-STAR.md` (CWD-relative), `/home/larry/agents/memory/beacon/` (absolute) | identical — no `~` used | ✅ tier-proof |
| **MCP servers** (e.g. `workspace-mcp`/Google) | `~/.claude.json` → `['workspace-mcp']` | `~/.claude.json` → `[]` | ❌ **GAP** — Tier-2 session loses those tools |
| User `~/.claude/settings.json` (model, theme, plugins, env) | present | missing | ⚠️ mostly cosmetic under `bypassPermissions` |
| Registered/trusted projects (`~/.claude.json` `projects`) | 2 | 0 | ⚠️ may gate project-scoped MCP / prompt for trust |
| **Parity monitoring** | — | weekly probe checked OAuth only | ❌→✅ **closed by this PR** |

---

## Residual gaps and recommendations

### G1 — MCP parity (real, live)
`workspace-mcp` is defined only in Tier-1's `~/.claude.json`. A Tier-2 session
has zero MCP servers, so it loses those tools. Two durable options:
- **Project-scoped `.mcp.json`** in the repo (tier-proof, version-controlled).
  Caveat: the server *definition* travels, but MCP **auth** is account-bound, so
  Tier-2 may still need to authenticate the server once.
- **Provision Tier-2's `~/.claude.json`** to mirror Tier-1's `mcpServers`.
Decision deferred to Larry — whichever, the new parity monitor now *detects* the
gap so it can't sit silent.

### G2 — Strategic root fix (Larry's call)
The cleanest end-state is to **stop overloading `HOME`**: authenticate the CLI
to a tier via the OAuth token env (`active_tier.durable_claude_env` already sets
`CLAUDE_CODE_OAUTH_TOKEN`) and/or `CLAUDE_CONFIG_DIR`, while keeping
`HOME=/home/larry` so a Tier-2 session inherits Tier-1's fully-provisioned
config, MCP, projects, and settings. This collapses the entire gap class to
zero. It touches the core auth/rotation path, so it is a deliberate
architectural decision, not an auto-merge — flagged here for Larry.

### G3 — Return-leg correctness
Tier-2 → Tier-1 restore is sound at the mechanism level (per-subprocess HOME, no
persisted Tier-2 state). The one historical return-path bug — Beacon always
primary-ing Tier-1 and ignoring the active-tier pin — was fixed in #753.

---

## What this PR ships

1. **This document** — the surface map and corrected model (so nobody re-ships
   the no-op allowlist fix).
2. **A self-firing parity monitor** folded into the weekly Tier-2 probe
   (`scripts/heal_tier2_weekly_health_probe.py` → `check_provisioning_parity`).
   It asserts, read-only and isolated from the OAuth probe:
   - Tier-2 OAuth credentials exist;
   - every Tier-1 MCP server is also defined for Tier-2 (catches G1);
   - every session-running unit keeps `TIER2_HOME` in `ReadWritePaths` (guards
     the transcript-EROFS regression).
   On drift it emits `tier2_provisioning_drift` (severity WARNING, tier SOON)
   with a plain-language translation; silence means parity. Tests in
   `scripts/tests/test_tier2_provisioning_parity.py`.

## What this PR deliberately does NOT do
- No `Read`-allowlist edits (no-op under `bypassPermissions`).
- No core auth/rotation changes (G2 is Larry's decision).
- No systemd unit edits (current session units already carry Tier-2 in RWP;
  the monitor now guards against regression).
