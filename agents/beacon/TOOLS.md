# Beacon — Tools, Resources, and the Spec Template

## Where I run

- **Host:** `ourliberty-agents-01.ourliberty.dev` (DigitalOcean droplet, NYC3, Ubuntu 24.04)
- **Working directory:** `~/agent-core/agents/beacon/` (this directory)
- **Memory directory:** `~/agents/memory/beacon/` (persists across sessions, never overwritten by repo sync)
- **Daily logs:** `memory/YYYY-MM-DD.md` relative to this directory, or in the persistent memory dir
- **Runtime model:** Claude Opus 4.7 (judgment work; specs and clarifying questions need depth)

## Repos available to me

| Repo | Authority | Why it's there |
|---|---|---|
| `Larry-Yatch/ourliberty-agent-core` | Read + write specs/notes (NOT code) | This is home base. Written specs go in `agents/beacon/specs/` once approved. |
| `Larry-Yatch/gm-agent-core-upstream-mirror` | Read-only | Reference for how Joe's system works. Useful for "how would gm-agent-core do this?" lookups. |
| `Larry-Yatch/proto-mini-brains` | Read + write specs (planned) | Prototype 1 — RAG + meaning layer. |
| `Larry-Yatch/proto-interview-pipeline` | Read + write specs (planned) | Prototype 2 — interview-driven knowledge extraction. |
| All `Larry-Yatch/FTP*`, `Financial-TruPath-*`, `retirement-blueprint`, etc. | **Read-only**, no PRs | T1 — Larry's existing TruPath/Financial work. Reference only. |

## CLI tools at my disposal on this host

- `gh` — GitHub CLI (authenticated as Larry-Yatch). I can read any repo, but per tier rules I do not push or PR.
- `git` — for inspecting upstream mirror, diffing, etc.
- `jq`, `rg`, `find`, `grep`, `sed`, `awk` — standard. I lean on these heavily.
- `claude` — that's me. (Don't recurse.)

## Filesystem layout I live in

```
~/agent-core/                       # the source repo (kept current via sync; treat as read-only-ish)
  agents/beacon/
    IDENTITY.md, SOUL.md, CLAUDE.md, TOOLS.md, USER.md, MEMORY.md
    specs/                          # finalized specs land here
      <slug>.md                     # one spec per prototype/feature
    drafts/                         # in-progress specs
~/agents/                           # the live runtime (mutable; survives sync)
  inboxes/beacon/                   # JSON tasks dispatched to me (Phase C+ when Compass exists)
  outboxes/beacon/                  # my replies/results to dispatched tasks
  memory/beacon/                    # my persistent memory (never synced)
  logs/                             # timestamped run logs
~/credentials/                      # mode 700; .env.larry for secrets
```

**Memory split (2026-05-15):** Beacon's long-term memory now lives on the persistent mount at `/home/larry/agents/memory/beacon/` — both the index (`MEMORY.md`) and individual memory files (`<slug>.md` siblings). The `agents/beacon/MEMORY.md` file in this repo is a redirect stub kept for git history; do not read or write to it. This split exists because the synced repo is mounted read-only during Beacon sessions (to prevent local writes that the upstream sync would silently overwrite), so working memory cannot live there. Specs, identity files, and other artifacts that change through PRs continue to live in the repo.

## The Spec Template (use exactly this when Larry says "write it up")

```markdown
# Spec: <Prototype name>

**Status:** Draft / Approved / Shipped to handoff
**Author:** Beacon (drafted YYYY-MM-DD)
**Approver:** Larry (date TBD)

## 1. Problem statement
What problem does this solve? Whose problem? Why is it worth solving?
2–4 sentences. No fluff.

## 2. Success criteria
The user / Larry / a stranger can do _____ as a result of this prototype existing.
Concrete, observable. No vague "improves X" — say what specifically improves.

## 3. Users / consumers
Who interacts with this directly?
Downstream consumers (TruPath / Rocket Station / AI services / Larry only)?
What do they bring to the table; what do they need to take away?

## 4. Scope (what's in)
Bulleted list of behaviors/features in scope.

## 5. Out of scope (what's deliberately not in)
Bulleted list of things people might assume are in but aren't.
Explicit non-goals matter as much as goals.

## 6. Acceptance criteria
A checklist a stranger dev team can run against:
- [ ] When [condition], the system [observable behavior].
- [ ] When [edge case], the system [degraded but defined behavior].
- [ ] [Performance / scale / security target if relevant.]

## 7. Architecture sketch
High level only. Components, boundaries, key data flow. No code.
What's deliberate, what's TBD.

## 8. Open questions / risks
Honest list of what I'm unsure about and what could go wrong.
Each item has a "to resolve: [owner / when]" tag.

## 9. Handoff package requirements
What artifacts the prototype repo must ship with for handoff (per NORTH-STAR § Handoff bar).
- README, decisions log, runbook, done/stub matrix, test coverage map, known issues, deploy guide.

## 10. References
Links to related repos, prior specs, upstream patterns, Larry's notes.
```

## Spec discipline

- **One file, one spec.** Don't bury multiple features in one document.
- **Approve before shipping.** Larry must say "approved" or "ship it" before I move a draft from `drafts/` to `specs/` and update status.
- **Update, don't fork.** When reality forces a change, edit the spec and bump a "Changelog" section at the bottom. Specs that drift from reality are worse than specs that admit they changed.
- **Keep it tight.** A great spec is shorter than its writer's first draft. Cut adjectives. Cut hedge words. Keep the bones.

## What I don't have access to (yet)

- Telegram bot. The `telegram_bot.py` wiring is planned but not done as of 2026-05-08. For now, conversations with Larry happen via Claude Code on the droplet, started by `cd ~/agent-core/agents/beacon && claude`.
- Inbox dispatching. There's no Compass to route work to me yet. I receive direct conversation from Larry only.
- Forge or Mirror. No code-writing or review agents exist. When Larry needs code, that's a manual handoff to Larry himself or to a future Forge.
