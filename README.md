# ourliberty-agent-core

Larry's personal AI agent operating system — a sandbox for designing, prototyping, and shipping AI-focused software prototypes that get handed off to larger development teams for full deployment.

Derived from [`GrowthMastery-ai/gm-agent-core`](https://github.com/GrowthMastery-ai/gm-agent-core) (Joe McVeen's system at GrowthMastery.ai), scrubbed of GM-specific business content and adapted for the prototype-to-handoff loop.

## Architecture

A multi-agent system orchestrating Claude Code processes via file-based dispatch (JSON tasks dropped into per-agent inboxes), with a self-healing `/cycle` loop monitoring its own health.

| Agent | Role | Status |
|---|---|---|
| **Beacon** | Strategy / Architect — idea → spec | planned (Phase B) |
| **Forge** | Engineering / Builder — spec → code | planned (Phase C) |
| **Mirror** | Adversarial Review — critique gate before merge | planned (Phase C) |
| **Pulse** | Self-healing / `/cycle` Observer | planned (Phase D) |
| **Aide** | Executive Assistant — Google Workspace | planned (Phase E) |
| **Scout** | Researcher | planned (Phase 2) |
| **Compass** | Planner / Dispatcher | planned (Phase 2) |

## North Star

See [`shared/NORTH-STAR.md`](./shared/NORTH-STAR.md) — read on every task. Optimization target: **artifacts a stranger picks up cold**, not raw coding speed.

## Repository discipline

See [`shared/REPO-GUARDRAILS.md`](./shared/REPO-GUARDRAILS.md). Three-tier data model (T0 sandbox, T1 internal, T2 sensitive). This repo is **always on `main`, always clean** — same discipline rule as upstream.

## Inter-agent dispatch

Tasks are JSON files dropped into `inboxes/<agent>/`. Schema: [`shared/HANDSHAKE-SCHEMA.json`](./shared/HANDSHAKE-SCHEMA.json) (kept verbatim from upstream).

## Reference / upstream

- **Upstream mirror:** [`Larry-Yatch/gm-agent-core-upstream-mirror`](https://github.com/Larry-Yatch/gm-agent-core-upstream-mirror) — read-only snapshot of `GrowthMastery-ai/gm-agent-core` for studying Joe's improvements over time. Synced daily by cron.
- **Live runtime:** `~/agents/` on the deployment VM. Never overwritten by repo sync.

## Status

🚧 **Phase A bootstrap** — 2026-05-08. Repo initialized with foundational files. Beacon coming next (Phase B).
