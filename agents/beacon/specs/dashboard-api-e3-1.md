# Spec: Dashboard API — E3.1 (read-only droplet status surface)

**Status:** Draft (awaiting Larry approval)
**Author:** Claude-as-Beacon (drafted 2026-05-20)
**Approver:** Larry (pending)
**Phase:** E3.1 of the Phase E plan (`docs/phase-e-plan.md`)
**Predecessor:** E2 (deploy layer) complete 2026-05-20 — PRs #51–#60
**Successors:** E3.2 (Next.js dashboard UI consumes this API), E3.3 (Nginx + HTTPS on `api.ourliberty.dev`)

## 1. Problem statement

Right now, the only way to see what the agent OS is doing is to SSH into the droplet and `cat` JSON files, or read Telegram. There is no consolidated visibility surface. E3 closes that gap with a read-only dashboard at `dashboard.ourliberty.dev`. The dashboard needs a backend it can call — that backend is the subject of this spec.

E3.1 builds the droplet-side HTTP API that exposes the agent OS state files (blackboard, state, logs, cycle journal, costs.jsonl) as 7 read-only JSON endpoints. The Next.js UI in E3.2 will fetch these every 30 seconds and render them. Nginx + Let's Encrypt in E3.3 will terminate TLS and proxy `https://api.ourliberty.dev/*` → `localhost:8000/*`.

## 2. Success criteria

- A FastAPI service `scripts/dashboard_api.py` runs under systemd on the droplet, bound to `localhost:8000` (loopback only — Nginx fronts it in E3.3).
- Seven GET endpoints return well-typed JSON describing the current state of the agent OS. Every endpoint that mutates anything → there are none; this is read-only.
- Auth is enforced via a static `X-Dashboard-Token` header compared in constant time against `DASHBOARD_API_TOKEN` from `/home/larry/credentials/.env.larry`. Missing or wrong header → 401.
- CORS allows only `Origin: https://dashboard.ourliberty.dev` (preview-deploy hostnames are handled in E3.2 via a Vercel env-var indirection, not by widening CORS here).
- `OURLIBERTY_AGENTS_ROOT` env override lets the test suite redirect filesystem reads into a tmpdir — same pattern as `scripts/deploy_notifier.py` (E2.2).
- `DASHBOARD_API_TOKEN` ships with the full E1.5 4-artifact discipline: token in `.env.larry`, registry entry in `config/token-rotation-schedule.json`, runbook at `docs/runbooks/rotate-dashboard-api-token.md`, Beacon-owned annual scope-audit calendar event.
- Smoke test: from Larry's laptop, `curl -H "X-Dashboard-Token: $TOKEN" http://localhost:8000/health` (via SSH tunnel) returns `200 OK` with a `{"status": "ok"}` body.

## 3. Users / consumers

- **Primary:** The E3.2 Next.js dashboard UI (`ourliberty-dashboard` repo). Polls every 30 s via SWR.
- **Secondary:** Larry, via direct curl during development / debugging.
- **Future (E4):** A mutation API will be added alongside this; for now the surface is strictly read-only.

Downstream consumer category: Larry-internal infrastructure (no client-facing surfaces in E3).

## 4. Scope (what's in)

### 4.1 New file: `scripts/dashboard_api.py`

A FastAPI application exposing 7 GET endpoints. Each endpoint reads from the droplet filesystem under `AGENTS_ROOT` (env-overridable, default `/home/larry/agents`) and returns JSON. All endpoints require the `X-Dashboard-Token` header.

#### Endpoint contracts

**`GET /health`**

```json
{
  "status": "ok",
  "version": "<git-sha-short or 'dev'>",
  "agents_root": "/home/larry/agents",
  "timestamp": "<iso-utc>"
}
```

200 always when the process is up. No filesystem reads — pure liveness. Auth still enforced (Nginx upstream check passes the header).

**`GET /agents/status`**

For each of the four agents (`beacon`, `forge`, `mirror`, `pulse`):

```json
{
  "agents": [
    {
      "name": "forge",
      "bot_active": true,
      "in_flight_count": 1,
      "in_flight_task_ids": ["task-28"],
      "last_activity_at": "<iso-utc>",
      "last_outbox_archive_at": "<iso-utc>"
    },
    ...
  ],
  "as_of": "<iso-utc>"
}
```

- `bot_active`: read by `systemctl is-active ourliberty-<agent>-bot.service` (only `beacon` and `forge` have bots today; `mirror`/`pulse` return `null` here — they're inbox-watcher-driven not bot-driven).
- `in_flight_count` + `in_flight_task_ids`: count of files in `~/agents/inboxes/<agent>/` matching `task-*.json` (excluding `.archive/`).
- `last_activity_at`: max mtime across `~/agents/outboxes/<agent>/.archive/*.json` + `~/agents/inboxes/<agent>/.archive/*.json`.
- `last_outbox_archive_at`: max mtime of `~/agents/outboxes/<agent>/.archive/*.json` only — sometimes a useful disambiguation.

**`GET /tasks/recent?limit=20`**

```json
{
  "tasks": [
    {
      "task_id": "task-28",
      "agent": "forge",
      "spec_summary": "<one-line>",
      "outcome": "review_pass" | "review_revision" | "review_escalate" | "review_emergency_halt" | "in_flight" | "unknown",
      "pr_url": "https://github.com/...",
      "started_at": "<iso-utc>",
      "completed_at": "<iso-utc>",
      "duration_seconds": 1234,
      "cost_usd": 1.23
    },
    ...
  ],
  "limit": 20,
  "returned": 17,
  "as_of": "<iso-utc>"
}
```

- Source: `~/agents/blackboard/costs.jsonl` joined with outbox archive metadata for outcome + duration.
- `limit` capped at 100 (validation: 422 if higher).
- Most-recent first.
- `outcome=in_flight` if the task is in an inbox + not yet archived.

**`GET /costs/today`**

```json
{
  "date_utc": "2026-05-20",
  "total_usd": 23.47,
  "by_agent": {
    "beacon": 4.12,
    "forge": 12.83,
    "mirror": 6.10,
    "pulse": 0.42
  },
  "task_count": 11,
  "as_of": "<iso-utc>"
}
```

Read from `~/agents/blackboard/costs.jsonl`, filtered to today (UTC).

**`GET /costs/week`**

```json
{
  "window_start_utc": "2026-05-14",
  "window_end_utc": "2026-05-20",
  "total_usd": 142.18,
  "by_day": [
    {"date_utc": "2026-05-14", "total_usd": 18.42, "task_count": 7},
    ...
  ],
  "by_agent": {
    "beacon": 28.11,
    "forge": 78.42,
    "mirror": 32.85,
    "pulse": 2.80
  },
  "task_count": 87,
  "as_of": "<iso-utc>"
}
```

Read from `~/agents/blackboard/costs.jsonl`, filtered to last 7 days (UTC). `window_end_utc` is today; `window_start_utc` is `today - 6 days` (inclusive on both ends → 7 calendar days).

**`GET /cycle-journal/recent?n=5`**

```json
{
  "entries": [
    {
      "started_at": "<iso-utc>",
      "headline": "<one-line summary>",
      "findings_count": 3,
      "body_markdown": "<full entry block, capped at 4 KB>"
    },
    ...
  ],
  "n": 5,
  "returned": 5,
  "as_of": "<iso-utc>"
}
```

- Source: `runbooks/cycle-journal.md` parsed by the existing convention (each entry begins with a `## YYYY-MM-DD HH:MM` header — Forge to verify against actual file shape at build time).
- `n` capped at 50.

**`GET /healers/status`**

```json
{
  "healers": [
    {
      "name": "heal_pr_auto_merge",
      "last_run_at": "<iso-utc>",
      "last_result": "ok" | "warn" | "error" | "stale",
      "last_summary": "<one-line>",
      "next_scheduled_at": "<iso-utc>",
      "kill_switch_active": false
    },
    ...
  ],
  "as_of": "<iso-utc>"
}
```

- One entry per `*.heartbeat` file under `~/agents/blackboard/`. Forge maps heartbeat-file → healer-name by stripping `.heartbeat`.
- `last_result` heuristic: tail of `~/agents/logs/<healer>.log` last 200 lines, classify by presence of `ERROR` / `WARN` / nothing-but-INFO. `stale` if the heartbeat is older than 2× the healer's expected cadence (Forge derives cadence from the systemd timer file if practical, else hard-codes the table).
- `next_scheduled_at`: `systemctl list-timers ourliberty-<healer>.timer --no-pager --no-legend` parse. Best-effort; `null` if parsing fails.
- `kill_switch_active`: `~/agents/healers.disabled` file existence.

### 4.2 Auth + CORS

- **Auth middleware:** FastAPI dependency. Reads `os.environ['DASHBOARD_API_TOKEN']` once at startup; compares incoming `X-Dashboard-Token` header with `secrets.compare_digest`. Missing header → 401 `{"detail": "missing X-Dashboard-Token"}`. Wrong header → 401 `{"detail": "invalid X-Dashboard-Token"}`. No logging of the actual token value (or any prefix of it).
- **CORS:** `fastapi.middleware.cors.CORSMiddleware` with `allow_origins=["https://dashboard.ourliberty.dev"]`, `allow_methods=["GET", "OPTIONS"]`, `allow_headers=["X-Dashboard-Token", "Content-Type"]`, `allow_credentials=False`, `max_age=600`.

### 4.3 Service binding

- Bind: `127.0.0.1:8000`. Loopback only. Nginx in E3.3 proxies.
- uvicorn entrypoint: `uvicorn scripts.dashboard_api:app --host 127.0.0.1 --port 8000 --log-level info`.

### 4.4 New file: `systemd/ourliberty-dashboard-api.service`

```
[Unit]
Description=Ourliberty Dashboard read-only JSON API (E3.1)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=larry
Group=larry
WorkingDirectory=/home/larry/agent-core
EnvironmentFile=/home/larry/credentials/.env.larry
ExecStart=/usr/bin/env python3 -m uvicorn scripts.dashboard_api:app \
  --host 127.0.0.1 --port 8000 --log-level info
Restart=on-failure
RestartSec=5s
StandardOutput=append:/home/larry/agents/logs/dashboard-api.log
StandardError=append:/home/larry/agents/logs/dashboard-api.log

[Install]
WantedBy=multi-user.target
```

No timer (long-running service, not oneshot). Install instructions go in `systemd/INSTALL.md` under a new "Dashboard API (E3.1)" subsection.

### 4.5 Tests: `scripts/tests/test_dashboard_api.py`

`unittest.TestCase` style (matches the rest of the repo per the E2.2 spec-lesson memory). Tests use FastAPI's `TestClient` + `setUpModule`/`tearDownModule` to redirect `OURLIBERTY_AGENTS_ROOT` to a per-test tmpdir before module import — mirrors the E2.2 isolation pattern. Coverage targets:

- **Auth:** missing header → 401; wrong header → 401; correct header → 200 (one test per endpoint, plus a parametrized sweep). Token comparison is constant-time (manual code review check — no automated way to verify timing, but spot-check the source uses `secrets.compare_digest`).
- **CORS:** `OPTIONS /agents/status` with allowed origin → 200 + correct headers; with disallowed origin → headers absent (FastAPI's CORSMiddleware behavior).
- **Health:** `/health` returns expected shape; no filesystem reads required.
- **Agents status:** mock inboxes + outboxes via tmpdir; verify counts, ids, mtimes. Include the case where an agent has zero files.
- **Tasks recent:** synthetic `costs.jsonl` + outbox archives; verify ordering, limit cap, in_flight detection. Edge: empty `costs.jsonl` → `{"tasks": [], "returned": 0}`.
- **Costs today / week:** synthetic `costs.jsonl` spanning multiple days; verify UTC boundary correctness (entry at 23:59 UTC counts for that day, not the next).
- **Cycle journal:** synthetic `cycle-journal.md`; verify parse + n cap + body truncation.
- **Healers status:** synthetic heartbeat files + log tails; verify stale detection (>2× cadence) + log classification.
- **Path isolation:** assert that running the test module does not write to `/home/larry/agents/logs/*.log`.
- **`/health` perf:** assert response < 50 ms in CI (cheap sanity).

Target: ≥ 30 unit tests. All deps mockable; no live droplet required.

### 4.6 Credential discipline (4 artifacts)

Per `shared/credentials-discipline.md`:

1. **Token install.** `DASHBOARD_API_TOKEN=<value>` appended to `/home/larry/credentials/.env.larry`. Mode 0600. Installed manually by Larry pre-merge (Claude generates the value; Larry pastes into droplet). Token: 43-char URL-safe base64 from `secrets.token_urlsafe(32)`. Same value also pasted into the Vercel project's `DASHBOARD_API_TOKEN` env var (Production + Preview + Development) in E3.2.

2. **Registry entry** in `config/token-rotation-schedule.json`:

```json
{
  "name": "DASHBOARD_API_TOKEN",
  "storage_location": "env_file:/home/larry/credentials/.env.larry",
  "credential_type": "shared_secret",
  "purpose": "Read-only dashboard API auth (E3.1); shared between droplet API and Vercel-hosted Next.js UI",
  "rotation_type": "scheduled",
  "cadence_days": 365,
  "created_at": "<PR-day YYYY-MM-DD>",
  "last_rotated_at": "<PR-day YYYY-MM-DD>",
  "next_rotation_due": "<PR-day + 365 days>",
  "calendar_event_url": "<Beacon-populated post-merge>",
  "runbook_path": "docs/runbooks/rotate-dashboard-api-token.md",
  "severity_if_lapsed": "high",
  "owner_role": "larry",
  "scopes": ["dashboard-read"],
  "notes": "Shared secret, not OAuth. Rotation requires updating both .env.larry on droplet AND Vercel project env vars. Both must change in the same window or the dashboard breaks."
}
```

3. **Runbook** at `docs/runbooks/rotate-dashboard-api-token.md`. Use `docs/runbooks/rotate-vercel-token.md` as the template. Sections: When to run / Severity if lapsed / Time required / Steps (1. Generate new token, 2. Install on droplet via SSH, 3. Update Vercel project env vars via Vercel dashboard, 4. Restart droplet service `sudo systemctl restart ourliberty-dashboard-api.service`, 5. Trigger Vercel redeploy to pick up new env, 6. Verify both ends with curl, 7. Revoke the old token from `.env.larry` once new is verified, 8. Update the registry, 9. Push the calendar event forward 1 year) / Rollback / Related.

4. **Calendar event.** Post-merge, Larry DMs Beacon: *"Create the DASHBOARD_API_TOKEN annual rotation calendar event for `<next_rotation_due>` minus 30 days, severity high, link to `docs/runbooks/rotate-dashboard-api-token.md`."* Beacon creates via her Google Calendar MCP, returns the URL, Larry pastes into the registry entry's `calendar_event_url` field via a small follow-up commit (or the next PR — same-PR is preferred but not strictly required since the registry validator doesn't require non-null calendar URLs).

## 5. Out of scope (what's deliberately not in)

- **Any mutation endpoints.** All seven endpoints are GET. E4 is the mutation layer.
- **Nginx config.** That's E3.3. This service binds to localhost; how it gets exposed publicly is the next phase's problem.
- **The Next.js UI.** That's E3.2.
- **Historical graphs / time series.** `/costs/week` returns daily aggregates; no minute-grain time series. E4+.
- **Multi-user auth.** Single shared secret. Per-user accounts are a Phase F problem.
- **WebSocket / SSE.** HTTP polling per the locked Q4 decision. E6 if the dashboard ever needs sub-second update latency.
- **Recursive endpoint enumeration / introspection beyond FastAPI's free `/docs` route.** `/docs` is gated by the same auth header (FastAPI's auto-docs route is included; auth applies).
- **Rate limiting.** Localhost-only + Nginx-fronted; rate limiting belongs in Nginx if we ever need it.
- **Per-route polling cadence.** All routes designed for 30 s polling; per-route tuning is E4.
- **Caching.** Each request re-reads the filesystem. The state files are small (KBs); premature caching is YAGNI.

## 6. Acceptance criteria

- [ ] `scripts/dashboard_api.py` exists; imports cleanly; `uvicorn scripts.dashboard_api:app` starts on localhost:8000.
- [ ] All 7 endpoints return well-typed JSON matching the contracts in § 4.1 when called with a valid `X-Dashboard-Token` header.
- [ ] Every endpoint returns 401 with no token header.
- [ ] Every endpoint returns 401 with a wrong token header.
- [ ] CORS preflight `OPTIONS` from `https://dashboard.ourliberty.dev` returns 200 with correct `Access-Control-Allow-*` headers; from any other origin returns no CORS headers.
- [ ] `OURLIBERTY_AGENTS_ROOT` override works — tests under `scripts/tests/test_dashboard_api.py` write nothing to `/home/larry/agents/`.
- [ ] `systemd/ourliberty-dashboard-api.service` passes `systemd-analyze verify`.
- [ ] `systemd/INSTALL.md` is updated with a new "Dashboard API (E3.1)" subsection covering install / start / verify-running / view-logs.
- [ ] `config/token-rotation-schedule.json` includes the `DASHBOARD_API_TOKEN` entry with all 14 required fields populated.
- [ ] `python3 scripts/validate_token_rotation_schedule.py` passes against the diff state.
- [ ] `docs/runbooks/rotate-dashboard-api-token.md` exists and follows the template; covers all 9 step categories above.
- [ ] Test suite (`python3 -m unittest scripts.tests.test_dashboard_api`) green; ≥ 30 tests.
- [ ] Mirror's dial-3 regression gate (`scripts/test_regression_check.py`) passes — no new test failures introduced.
- [ ] PR body has Summary + Test plan + a "Smoke test instructions" section Larry can run post-merge.
- [ ] No literal `DASHBOARD_API_TOKEN` value appears in any committed file (only the name).

## 7. Architecture sketch

```
┌────────────────────────────────┐
│ uvicorn (systemd, localhost)   │
│  ├─ scripts/dashboard_api.py   │
│  │   └─ FastAPI app            │
│  │       ├─ auth dep           │  ←  X-Dashboard-Token (constant-time)
│  │       ├─ CORS middleware    │  ←  origin allowlist
│  │       └─ 7 GET routes       │
│  │            │                │
│  │            ▼                │
│  │       AGENTS_ROOT readers   │
└────────────│───────────────────┘
             │ filesystem reads (no writes)
             ▼
   ~/agents/{blackboard,state,logs,inboxes,outboxes}/
   ~/agent-core/runbooks/cycle-journal.md
```

Module structure inside `scripts/dashboard_api.py`:

- `AGENTS_ROOT` resolution (env override; identical pattern to `deploy_notifier.py`).
- `_require_token` FastAPI dependency.
- One small `_reader_*` function per endpoint that takes `AGENTS_ROOT` + optional params, returns a typed dict. Pure functions — no FastAPI in their signatures — so tests can call them directly without TestClient.
- Endpoint handlers are thin: parse query → call reader → return JSON.
- Pydantic response models for each endpoint (free OpenAPI schema + free validation).
- `app = FastAPI(title="Ourliberty Dashboard API", version="0.1.0")`.

Approximate size: 400–600 lines source, 600–900 lines tests.

## 8. Open questions / risks

- **FastAPI is a new runtime dependency on the droplet.** The repo's stdlib-only convention (called out in `deploy_notifier.py`'s docstring) doesn't apply to long-running web services — but the install path needs to exist. **Resolution:** install `fastapi` + `uvicorn[standard]` via `pip3 install --user fastapi 'uvicorn[standard]'` on the droplet during E3.1 deploy (Larry runs the command per a section in `systemd/INSTALL.md`). If a future Mirror review wants a `requirements.txt`, that's a small follow-up.
- **Cycle-journal parse fragility.** The journal is markdown authored by Pulse; the API parses it. A schema change in Pulse could break the parser. **Resolution for v1:** be lenient — if a section doesn't match the expected header regex, skip it and continue; surface parse errors in the response as `{"parse_warnings": [...]}` rather than 500-ing. Forge picks the exact regex by reading the live `runbooks/cycle-journal.md` at build time.
- **`bot_active` ambiguity for mirror/pulse.** They don't have always-on bots — they're inbox-watcher-dispatched. Returning `null` is technically correct but might confuse the UI. **Resolution for v1:** return `null` + add a `bot_model` field (`"systemd-bot"` for beacon/forge, `"inbox-watcher"` for mirror/pulse) so the UI can render appropriately.
- **`systemctl is-active` exec from inside FastAPI.** subprocess calls in request handlers are fine for 30s-cadence reads but introduce process-spawn cost (~10–30 ms per call) on every `/agents/status` hit. **Resolution for v1:** acceptable. If perf becomes a concern, cache for 5 s with a small TTL dict.
- **`/health` perf assertion.** The 50 ms assert is a sanity gate, not a hard SLA. Adjust to 100 ms if CI is variable.
- **No request logging today.** uvicorn logs requests to the systemd journal + the log file. We're not adding structured per-request logging. **Resolution:** YAGNI; revisit if we need traffic shape data.

## 9. Handoff package requirements

- `scripts/dashboard_api.py` — the FastAPI app (§ 4.1, § 4.2, § 4.3).
- `scripts/tests/test_dashboard_api.py` — the unit tests (§ 4.5).
- `systemd/ourliberty-dashboard-api.service` — the systemd unit (§ 4.4).
- `systemd/INSTALL.md` — updated with a new subsection covering: pip install of fastapi + uvicorn, copy/symlink the unit, enable + start, verify via `systemctl status` + `curl -H "X-Dashboard-Token: …" http://localhost:8000/health`.
- `config/token-rotation-schedule.json` — new entry for `DASHBOARD_API_TOKEN` (§ 4.6 item 2).
- `docs/runbooks/rotate-dashboard-api-token.md` — the rotation runbook (§ 4.6 item 3).
- `docs/phase-e-plan.md` — Current Status block updated to mark E3.1 done at PR merge time.
- `agents/mirror/CLAUDE.md` — additive note under "Deploy-targets discipline" subsection (or its own new subsection): when a PR touches `scripts/dashboard_api.py` or `systemd/ourliberty-dashboard-api.service`, confirm the 4-artifact discipline applies to `DASHBOARD_API_TOKEN`. (Optional in this PR; can be a follow-up if scope creeps.)
- PR title: `feat(dashboard-api): E3.1 — read-only droplet status surface`.
- PR body: Summary (3 bullets max) + Acceptance-criteria checklist mirrored from § 6 + Test plan + Smoke test instructions.

## 10. References

- Phase plan: `docs/phase-e-plan.md` § Phase E3 (Architecture decisions locked 2026-05-20).
- E2.2 path-isolation pattern: `scripts/deploy_notifier.py` — same `OURLIBERTY_AGENTS_ROOT` env-override shape applies here.
- E1.5 credential discipline: `shared/credentials-discipline.md` and `docs/runbooks/rotate-vercel-token.md` (template).
- Mirror dial-3 gate: `scripts/test_regression_check.py` runs automatically per Mirror's CLAUDE.md.
- Memory: `project_phase_e2_2_complete`, `project_mirror_gate_posture`, `feedback_headless_mode_chain_gaps`.
