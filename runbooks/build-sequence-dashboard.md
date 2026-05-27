# Runbook — build-sequence dashboard API + ladder panel

**Component:** `GET /api/system/build-sequences` in `scripts/dashboard_api.py` (droplet) + ladder panel in `ourliberty-dashboard` (PR-S3b, separate repo)
**Spec:** `agents/beacon/specs/build-sequence-orchestrator.md` § 5.6 (ladder panel + API endpoint), § 5.8 (data sources)
**Shipped in:** PR-S3a (this PR — droplet endpoint + tests + this runbook + spec annotation). PR-S3b (follow-on, ourliberty-dashboard) ships the UI that consumes it.
**Related:** `runbooks/build-sequence-advancer.md` (the writer side — the advancer daemon produces the sequence files this endpoint reads).

## What it does (one paragraph)

The endpoint surfaces every build-sequence file the advancer manages, partitioned server-side into `active` and `archived` arrays. PR-S3b's ladder panel polls it every ~10 s (per spec § 5.6) and renders one row per sequence on the list page, with a per-sequence ladder view on the detail page. The endpoint is read-only, token-gated, uncached, and returns the raw sequence-file dicts verbatim — no field projection. Failures fall through gracefully (missing dir → empty arrays; corrupted file → omitted + surfaced in `parse_warnings`).

## Endpoint contract

```
GET /api/system/build-sequences
Headers:
  X-Dashboard-Token: <value of DASHBOARD_API_TOKEN env on the droplet>
```

| Outcome | Status | Body |
|---|---|---|
| Missing/wrong token | 401 | `{"detail": "missing X-Dashboard-Token"}` / `{"detail": "invalid X-Dashboard-Token"}` |
| Success | 200 | `{"active": [...], "archived": [...], "parse_warnings": [...], "as_of": "<iso>"}` |

`active[]` and `archived[]` each hold raw sequence-file dicts (the on-disk JSON, untouched) matching spec § 5.1's schema: `seq_id`, `label`, `spec_doc`, `created_at`, `status`, `current_steps`, `steps[]`, `audit_log[]`, etc. `parse_warnings[]` lists any sequence files that failed to load (path + reason) — the endpoint omits them rather than 500'ing so one bad file can't blind the dashboard.

### Active vs archived classification (locked 2026-05-27)

| Sequence is in… | When… |
|---|---|
| `active[]` | Main-dir file with `status ∈ {pending, active, paused}` or unknown/missing status |
| `archived[]` | Main-dir file with `status ∈ {complete, failed, archived}` **OR** any file under `~/agents/blackboard/build-sequences/.archive/YYYY-MM/*.json` |

Today every recently-completed sequence sits in the main dir with `status=complete` or `status=failed` (the 30-day spec § 5.1 archiver doesn't exist yet — `.archive/YYYY-MM/` is forward-compat scaffolding). Once the archiver lands, completed sequences move there and the endpoint will continue partitioning correctly without code changes.

### Pagination + time filtering

Neither is implemented in V1 — the endpoint returns every sequence file it finds. `TODO(PR-S3c): pagination` lives next to the archived-list construction in `_reader_build_sequences`; add `?limit=`, `?offset=`, and optionally `?archived_since=` when archived volume grows past what the dashboard can render in one poll cycle.

## Curl recipes

```bash
# Token is in ~/credentials/.env.larry on the droplet.
TOKEN=$(grep DASHBOARD_API_TOKEN /home/larry/credentials/.env.larry | cut -d= -f2)

# Just the keys (sanity check the shape).
curl -s -H "X-Dashboard-Token: $TOKEN" \
  http://127.0.0.1:8000/api/system/build-sequences | jq 'keys'

# Active sequences only, summarized.
curl -s -H "X-Dashboard-Token: $TOKEN" \
  http://127.0.0.1:8000/api/system/build-sequences \
  | jq '.active[] | {seq_id, status, current_steps}'

# Archived count by status.
curl -s -H "X-Dashboard-Token: $TOKEN" \
  http://127.0.0.1:8000/api/system/build-sequences \
  | jq '.archived | group_by(.status) | map({status: .[0].status, count: length})'

# Any parse warnings — non-empty array means one or more sequence files
# are corrupted and need operator triage.
curl -s -H "X-Dashboard-Token: $TOKEN" \
  http://127.0.0.1:8000/api/system/build-sequences \
  | jq '.parse_warnings'
```

## Interpreting the response

- **`current_steps[]` on an active sequence** lists step IDs that are in-flight right now (PR open, building, or under Mirror review). Empty array on an active sequence means the advancer hasn't dispatched the next batch yet — the next tick (≤5 min) should populate it.
- **`status: "paused"`** means the advancer halted the sequence on a failure or operator action. The latest `audit_log[]` entry explains why (`event: "sequence-paused-on-step-failure"`, `event: "sequence-paused-by-operator"`, etc.). Recovery: per `runbooks/build-sequence-advancer.md` "Pause / resume / cancel," edit the file to `status: "active"` until PR-S4 ships the Beacon shortcuts.
- **Each step's `status`** uses spec § 5.1's step-level enum (`pending` / `dispatchable` / `dispatched` / `building` / `reviewing` / `merged` / `failed`) — distinct from the sequence-level enum.
- **`audit_log[]` is append-only** (per spec § 5.1); the most recent entry is the latest authoritative transition.

## Diagnosing an empty-state response when sequences should be visible

```bash
# 1. Does the blackboard dir exist on the droplet?
ls -la /home/larry/agents/blackboard/build-sequences/

# 2. Are there any *.json files in it?
ls /home/larry/agents/blackboard/build-sequences/*.json 2>/dev/null

# 3. Is the dashboard_api process pointed at /home/larry/agents/?
systemctl show ourliberty-dashboard-api.service \
  | grep -E '^(Environment|EnvironmentFile)='
# Look for OURLIBERTY_AGENTS_ROOT — if it's overridden to a tmpdir, the
# endpoint sees an empty blackboard dir and returns the empty-state body.

# 4. Are sequence files readable by the dashboard_api user?
sudo -u larry stat /home/larry/agents/blackboard/build-sequences/*.json
```

If the endpoint returns 200 with empty `active` + `archived` arrays AND the dir contains valid JSON files: check the `parse_warnings` array — a file with the wrong top-level shape (e.g. an array instead of an object) is omitted with a warning rather than crashing the endpoint.

## Test surface

```bash
cd ~/agent-core && python3 -m unittest \
    scripts.tests.test_dashboard_api_build_sequences -v
```

27 tests cover: auth (401 on missing/bad token, 200 on correct), empty states (missing dir, empty dir, archive-only), per-status partitioning, multi-sequence layout, verbatim pass-through (including unknown fields), uncached file-mutation between requests, corrupted JSON omission with warning, archive-layout discipline (non-YYYY-MM subdirs ignored, hidden files ignored), symlink skipping (path-traversal guard), env-var leak guard, and top-level response key stability.

## Out of scope (V2 / follow-on PRs)

- The ladder panel UI itself — that's PR-S3b in `ourliberty-dashboard`. This endpoint is the data layer; the UI is a separate single-repo PR per the cross-repo split discipline in spec § 4.
- Pagination + `?archived_since=` — V1 returns everything. See the `TODO(PR-S3c)` breadcrumb in `_reader_build_sequences`.
- Write operations (pause / resume / cancel) — those land in PR-S4 via Beacon shortcuts, not on this endpoint. The endpoint is read-only.
- A 30-day → `.archive/YYYY-MM/` archiver. Spec § 5.1 describes it but no code exists yet; recently-completed sequences sit in the main dir with `status: complete` until that ships.

## Cross-references

- Orchestrator spec: `agents/beacon/specs/build-sequence-orchestrator.md` (canonical contract).
- Advancer runbook: `runbooks/build-sequence-advancer.md` (the writer side — produces the sequence files this endpoint reads).
- E4.4d PR-C conventions: `scripts/dashboard_api.py` `/api/system/active-sessions` / `/cgroup-stats` / `/worktrees` (this endpoint mirrors their token-gating + uncached discipline).
