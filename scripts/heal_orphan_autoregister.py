#!/usr/bin/env python3
"""heal_orphan_autoregister.py — the Missions v2 orphan auto-registration healer
(Phase 3 § 6).

Sibling to heal_missions_card_gc (the GC pattern): a timer healer that keeps the
Orphans lane self-draining. Each tick scans NON-TERMINAL, NON-INFRASTRUCTURE
orphans and appends a `phase: "proposed"` entry to missions.json so the board can
render an accept/dismiss affordance (the dashboard step). The parked
`cap-bidirectional-missions-board` idea is retired here: auto-registration IS the
concrete realization of "agents read the board to self-prioritize".

Three properties the spec pins (§ 6), all enforced structurally:

  1. REUSE THE PHASE-2 DERIVE (no drift). Orphan classification — which task_ids
     are orphans, which are infrastructure, and whether one is terminal — is NOT
     reimplemented here. We import the SAME pure functions the dashboard
     `/api/missions/derived` route uses (dashboard_api.detect_orphans,
     is_infrastructure_task, _derive_orphan_readability, ...). One classification,
     one source of truth.

  2. IDEMPOTENT. A proposed entry carries the orphan's task_id in its `task_ids`.
     That registers the task_id, so the NEXT tick's detect_orphans (which excludes
     every registered task_id) no longer surfaces it — it cannot be re-proposed.
     Accept (phase proposed→drafting) and dismiss (mark acknowledged) both keep the
     task_id registered, so neither re-proposes either. There is no separate dedup
     store to drift; the registry IS the dedup key.

  3. FAIL-SAFE — every indeterminate signal errs toward NOT proposing (no noise):
       * chain_events unavailable / fetch error  → propose nothing this tick.
       * missions.json malformed                 → skip (never append onto a
                                                    corrupt registry).
       * an orphan carries a pr_url we could NOT resolve a live state for → skip it
         (it might actually be merged/terminal; we refuse to propose a dead thread).
       * a terminal orphan (merged or explicitly-closed PR)              → skip.
     A bad tick reports + skips; it never corrupts the registry or proposes noise.

  4. COMMIT + PUSH the missions.json delta to main, the durability half — exactly
     like the GC healer commits its captures.json delta. The healer ONLY APPENDS
     proposed entries (never edits an existing one), so a rebase conflict against a
     concurrent New-Mission PR merge is near-impossible.

stdlib only (+ dashboard_api for the derive and chain_event_emit / larry_alerts,
all imported lazily so a missing optional dep degrades the tick rather than
crashing it).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Repo scripts dir on sys.path so sibling imports (dashboard_api, chain_event_emit,
# larry_alerts) resolve when run by systemd.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_MODELS_CONFIG_PATH = _SCRIPTS_DIR.parent / 'config' / 'agent-models.json'

MISSIONS_REL = 'agents/beacon/missions.json'

# The phase enum value that marks an auto-proposed (pre-drafting) thread (§ 6).
PROPOSED_PHASE = 'proposed'
# Stable, deterministic entry id for a proposal so a second tick can detect the
# entry already exists even before detect_orphans' registered-exclusion kicks in.
PROPOSED_ID_PREFIX = 'proposed-'
PROPOSED_BY = 'heal_orphan_autoregister'

GIT_TIMEOUT_SEC = 60
PUSH_TIMEOUT_SEC = 180


# ---------- env-resolved paths (read at call time so tests can override) ----------


def _agents_root() -> Path:
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))


def _kill_switch_path() -> Path:
    return _agents_root() / 'healers.disabled'


def _log_path() -> Path:
    """Honor the test/CI OURLIBERTY_LOG_DIR override so a test run never writes
    into the live ~/agents/logs/ tree (see scripts/tests/conftest.py)."""
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    base = Path(override) if override else (_agents_root() / 'logs')
    return base / 'missions-autoregister.log'


def log(msg: str) -> None:
    line = f'[{datetime.now(timezone.utc).isoformat()}] {msg}'
    print(line, flush=True)
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        # Best-effort: a full/read-only log FS must never crash the healer.
        pass


# ---------- config ----------


def load_repo_paths() -> dict[str, Path]:
    """Repo name → Path from config/agent-models.json ``repo_paths`` (the same
    block the GC healer reads). Returns {} on a missing/unreadable block — the
    healer degrades (skips the tick) rather than crashing."""
    try:
        cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f'could not read {_MODELS_CONFIG_PATH}: {e}')
        return {}
    block = cfg.get('repo_paths') if isinstance(cfg, dict) else None
    if not isinstance(block, dict):
        return {}
    out: dict[str, Path] = {}
    for name, raw in block.items():
        if isinstance(raw, str) and raw:
            out[name] = Path(raw)
    return out


def missions_path(repo_paths: dict[str, Path]) -> Optional[Path]:
    """Path to agent-core's missions.json, or None if agent-core isn't
    configured."""
    core = repo_paths.get('ourliberty-agent-core')
    return (core / MISSIONS_REL) if core else None


# ---------- derive reuse (the SAME functions the dashboard route uses) ----------


def load_derive() -> Optional[Any]:
    """Import the dashboard_api module — the single source of the orphan derive.

    Lazy + fail-safe: if the import fails (e.g. fastapi absent under the runtime
    python), we log and the caller skips the tick rather than crashing. Reusing
    the module (instead of re-porting detect_orphans / is_infrastructure_task /
    _derive_orphan_readability) is what guarantees ZERO drift between what the
    board shows as an orphan and what this healer proposes."""
    try:
        import dashboard_api  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — fail-safe: a missing dep skips the tick
        log(f'dashboard_api (derive) import failed: {type(e).__name__}: {e}')
        return None
    return dashboard_api


# ---------- missions.json (read / write) — fail-safe ----------


def read_missions_registry(path: Path) -> Optional[dict[str, Any]]:
    """Load missions.json as a registry dict. Missing file → fresh empty registry.
    Malformed / wrong-shape → None (caller skips this tick; we never append onto a
    corrupt registry). Mirrors heal_missions_card_gc.read_captures_registry: a bad
    tick reports rather than crashes (§ 6 fail-safe)."""
    if not path.exists():
        return {'schema_version': 1, 'missions': []}
    try:
        raw = path.read_text()
        data = json.loads(raw) if raw.strip() else {'schema_version': 1, 'missions': []}
    except (OSError, json.JSONDecodeError) as e:
        log(f'missions.json malformed/unreadable ({path}): {e} — skipping this tick')
        return None
    if not isinstance(data, dict) or not isinstance(data.get('missions'), list):
        log(f'missions.json shape invalid ({path}) — skipping this tick')
        return None
    data.setdefault('schema_version', 1)
    return data


def registered_task_ids(registry: dict[str, Any]) -> set[str]:
    """Every task_id already registered to a mission entry (proposed, accepted,
    dismissed, or any other phase). detect_orphans excludes these, so this set is
    the idempotency key: once an orphan's task_id lands here it is never
    re-proposed."""
    out: set[str] = set()
    for entry in registry.get('missions', []):
        if not isinstance(entry, dict):
            continue
        tids = entry.get('task_ids')
        if isinstance(tids, list):
            out.update(t for t in tids if isinstance(t, str) and t)
    return out


def existing_entry_ids(registry: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for entry in registry.get('missions', []):
        if isinstance(entry, dict):
            eid = entry.get('id')
            if isinstance(eid, str) and eid:
                out.add(eid)
    return out


def atomic_write_missions(path: Path, registry: dict[str, Any]) -> None:
    """tmp-in-same-dir + os.replace. Mirrors heal_missions_card_gc.atomic_write_captures
    so a reader never sees a partial file."""
    import tempfile  # noqa: PLC0415
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(json.dumps(registry, indent=2) + '\n')
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------- proposal selection + construction (pure; unit-tested directly) ----------


def select_proposable_orphans(
    orphans: list[dict[str, Any]],
    pr_state_by_url: dict[str, str],
) -> list[dict[str, Any]]:
    """From derive-enriched orphans, keep only the ones safe to propose.

    Pure — the caller resolves all I/O (orphan derive + PR states) first. The two
    fail-safe guards (§ 6 'every indeterminate signal errs toward NOT proposing'):

      * TERMINAL → skip. A merged or explicitly-closed orphan is done; proposing it
        is noise. `terminal` is the derive's own field.
      * PR-STATE INDETERMINATE → skip. An orphan that carries a pr_url whose live
        state we could NOT resolve (no token, network error) is indeterminate — it
        might actually be merged. We refuse to propose it rather than risk a dead
        thread. (A PR-less orphan has no merge state to be unsure about, so it is
        proposable when non-terminal.)
    """
    out: list[dict[str, Any]] = []
    for o in orphans:
        if o.get('terminal'):
            continue
        pr_url = o.get('pr_url')
        if pr_url and pr_url not in pr_state_by_url:
            # Has a PR but its live state is indeterminate → err toward NOT proposing.
            continue
        out.append(o)
    return out


def build_proposed_entry(orphan: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Construct the `phase: "proposed"` missions.json entry for one orphan (§ 6).

    Carries the orphan's task_id (the idempotency anchor — registers it so the next
    tick's detect_orphans excludes it), its derived label, repo/branch, and last
    activity. Shaped like a normal registry entry (id/name/phase/brief/spec_docs/
    task_ids/repo/created/deferred_reason) plus additive provenance fields the
    Proposed affordance reads."""
    task_id = orphan['task_id']
    label = orphan.get('label') or task_id
    repo = orphan.get('repo')
    branch = orphan.get('branch')
    last_activity = orphan.get('last_event_ts')
    if repo and branch:
        loc = f' ({repo}/{branch})'
    elif repo:
        loc = f' ({repo})'
    else:
        loc = ''
    return {
        'id': f'{PROPOSED_ID_PREFIX}{task_id}',
        'name': label,
        'phase': PROPOSED_PHASE,
        'brief': (
            f'Auto-proposed from orphan task `{task_id}`{loc}. '
            f'Last activity {last_activity}. Accept to claim the task_id into a '
            f'drafting mission; dismiss to stop re-proposing.'
        ),
        'spec_docs': [],
        'task_ids': [task_id],
        'repo': repo,
        'created': now.date().isoformat(),
        'deferred_reason': None,
        # Additive provenance + orphan metadata (§ 6) — ignored by existing reads.
        'proposed_by': PROPOSED_BY,
        'proposed_at': now.isoformat(),
        'orphan_branch': branch,
        'orphan_last_activity_ts': last_activity,
    }


# ---------- the derive-backed scan (effectful edges seamed for tests) ----------


@dataclass
class ProposeResult:
    proposed: list[tuple[str, str]] = field(default_factory=list)  # (task_id, entry_id)
    scanned_orphans: int = 0
    skipped_terminal_or_indeterminate: int = 0
    events_unavailable: bool = False
    registry_unreadable: bool = False
    derive_unavailable: bool = False


def scan_and_propose(
    registry: dict[str, Any],
    rows: list[dict[str, Any]],
    derive: Any,
    now: datetime,
    *,
    pr_state_resolver: Optional[Callable[[list[str]], dict[str, str]]] = None,
) -> ProposeResult:
    """Reuse the derive to find orphans, then append a proposed entry for each
    proposable one. Mutates ``registry['missions']`` in place; returns what was
    proposed. Fail-safe: any unexpected error proposes nothing (the registry is
    left untouched for this orphan set)."""
    res = ProposeResult()
    registered = registered_task_ids(registry)
    existing_ids = existing_entry_ids(registry)

    orphans = derive.detect_orphans(rows, registered)
    res.scanned_orphans = len(orphans)
    if not orphans:
        return res

    # Group events per orphan (newest-first preserved) for readability/terminal —
    # exactly how _build_derived_response feeds _derive_orphan_readability.
    events_by_orphan: dict[str, list[dict[str, Any]]] = {}
    for ev in rows:
        tid = ev.get('task_id')
        if tid:
            events_by_orphan.setdefault(tid, []).append(ev)

    # Resolve live PR states (fail-safe). A {} result (no token / error) means
    # EVERY pr_url-bearing orphan is indeterminate → none of them are proposed.
    if pr_state_resolver is None:
        pr_state_resolver = derive._resolve_orphan_pr_states
    pr_state_by_url: dict[str, str] = {}
    orphan_pr_urls = [o['pr_url'] for o in orphans if o.get('pr_url')]
    if orphan_pr_urls:
        try:
            pr_state_by_url = pr_state_resolver(orphan_pr_urls) or {}
        except Exception as e:  # noqa: BLE001 — fail-safe: indeterminate → propose none
            log(f'PR-state resolve raised: {type(e).__name__}: {e} — treating as indeterminate')
            pr_state_by_url = {}

    for o in orphans:
        o.update(derive._derive_orphan_readability(
            o, events_by_orphan.get(o['task_id'], []), now,
            pr_state=pr_state_by_url.get(o.get('pr_url')),
        ))

    proposable = select_proposable_orphans(orphans, pr_state_by_url)
    res.skipped_terminal_or_indeterminate = len(orphans) - len(proposable)

    for o in proposable:
        entry = build_proposed_entry(o, now)
        # Defensive second idempotency layer: never duplicate an entry id (detect_orphans
        # already excludes registered task_ids, so this only guards a malformed prior entry).
        if entry['id'] in existing_ids:
            continue
        registry['missions'].append(entry)
        existing_ids.add(entry['id'])
        res.proposed.append((o['task_id'], entry['id']))
    return res


# ---------- commit + push the missions.json delta to main (§ 6) ----------


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    """Run git in ``repo``; a timeout/OS error becomes a synthetic non-zero result
    so callers branch on returncode uniformly."""
    try:
        return subprocess.run(
            ['git', *args], cwd=str(repo),
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'git {" ".join(args)} failed in {repo}: {type(e).__name__}: {e}')
        return subprocess.CompletedProcess(args, returncode=255, stdout='', stderr=str(e))


def commit_and_push_missions(repo: Path, audit_msg: str) -> str:
    """Commit + push any missions.json delta to origin/main. Returns a status token:
      'nothing'       — no delta to commit
      'wrong-branch'  — repo not on main; refuse to commit (would land on a feature
                        branch) — caller escalates
      'committed'     — committed and pushed
      'commit-failed' / 'push-failed' — git step failed; commit retained locally

    Mirrors heal_missions_card_gc.commit_and_push_captures: try push; on a non-FF
    refusal, pull --rebase --autostash and retry; abort the rebase on conflict.
    Never force-pushes. This healer only APPENDS proposed entries (never edits an
    existing one), so a rebase conflict against a concurrent New-Mission PR merge is
    near-impossible — the appended object can't collide with an unrelated edit."""
    head = _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    branch = head.stdout.strip() if head.returncode == 0 else ''
    if branch != 'main':
        return 'wrong-branch'

    clean = _git(repo, 'diff', '--quiet', '--', MISSIONS_REL)
    clean_cached = _git(repo, 'diff', '--quiet', '--cached', '--', MISSIONS_REL)
    if clean.returncode == 0 and clean_cached.returncode == 0:
        return 'nothing'

    if _git(repo, 'add', MISSIONS_REL).returncode != 0:
        return 'commit-failed'
    commit = _git(repo, 'commit', '-m',
                  'chore(missions): autoregister healer — propose orphan thread(s)',
                  '-m', audit_msg)
    if commit.returncode != 0:
        log(f'missions.json commit failed in {repo}: {(commit.stderr or commit.stdout).strip()[:200]}')
        return 'commit-failed'

    if _git(repo, 'push', '-q', 'origin', 'main', timeout=PUSH_TIMEOUT_SEC).returncode == 0:
        return 'committed'
    log('missions.json push refused (likely non-FF); attempting pull --rebase --autostash')
    rebase = _git(repo, 'pull', '--rebase', '--autostash', '-q', 'origin', 'main',
                  timeout=PUSH_TIMEOUT_SEC)
    if rebase.returncode == 0:
        if _git(repo, 'push', '-q', 'origin', 'main', timeout=PUSH_TIMEOUT_SEC).returncode == 0:
            return 'committed'
        return 'push-failed'
    log('missions.json rebase failed; aborting (commit retained locally)')
    _git(repo, 'rebase', '--abort')
    return 'push-failed'


# ---------- alerting ----------


def _emit_summary(res: ProposeResult, commit_status: str, dry_run: bool) -> None:
    """One audit line (log) + a low-noise digest alert when something was proposed;
    escalate on a hard failure. Exact counts + the full id list go to the log."""
    proposed_ids = [eid for _, eid in res.proposed]
    verb = 'would propose' if dry_run else 'proposed'
    summary = (
        f'missions-autoregister: {verb} {len(res.proposed)} orphan thread(s) '
        f'{proposed_ids}; scanned {res.scanned_orphans} orphan(s); '
        f'skipped {res.skipped_terminal_or_indeterminate} terminal/indeterminate; '
        f'commit={commit_status}'
    )
    if res.events_unavailable:
        summary += '; chain_events unavailable (skipped)'
    if res.registry_unreadable:
        summary += '; missions.json unreadable (skipped)'
    if res.derive_unavailable:
        summary += '; derive unavailable (skipped)'
    log(summary)

    if dry_run:
        return
    try:
        import larry_alerts  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — alerting is best-effort
        log(f'larry_alerts unavailable: {e}')
        return

    hard_failure = commit_status in ('wrong-branch', 'commit-failed', 'push-failed')
    if hard_failure:
        larry_alerts.append_alert(
            source='missions-autoregister', severity='warning',
            message=summary, subject=f'failure:{commit_status}', route='escalate')
    elif res.proposed and commit_status == 'committed':
        larry_alerts.append_alert(
            source='missions-autoregister', severity='warning',
            message=summary, subject='summary', route='digest')


# ---------- main ----------


def _default_events_fetcher() -> Optional[list[dict[str, Any]]]:
    """Fetch all chain_events in the orphan window, newest-first — mirrors
    dashboard_api._fetch_recent_chain_events but via chain_event_emit's client (the
    same path the GC healer uses). None on an unavailable client or read error so
    the caller skips the tick (fail-safe)."""
    import chain_event_emit  # noqa: PLC0415
    from datetime import timedelta  # noqa: PLC0415
    cli = chain_event_emit._get_client()
    if cli is None:
        return None
    derive = load_derive()
    window_days = getattr(derive, '_ORPHAN_WINDOW_DAYS', 30) if derive else 30
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    try:
        resp = (
            cli.table('chain_events')
            .select('event_type,task_id,agent,pr_url,ts,payload')
            .gte('ts', since)
            .execute()
        )
    except Exception as e:  # noqa: BLE001 — read must never crash the tick
        log(f'chain_events read failed: {type(e).__name__}: {e}')
        return None
    return list(getattr(resp, 'data', None) or [])


def run_once(*, dry_run: bool,
             events_fetcher: Optional[Callable[[], Optional[list[dict[str, Any]]]]] = None,
             derive: Optional[Any] = None,
             pr_state_resolver: Optional[Callable[[list[str]], dict[str, str]]] = None,
             now: Optional[datetime] = None) -> int:
    """One healer tick. The injectable seams (events_fetcher / derive /
    pr_state_resolver / now) keep the effectful edges test-controllable; production
    resolves them from chain_event_emit + dashboard_api's derive."""
    now = now or datetime.now(timezone.utc)
    repo_paths = load_repo_paths()

    mpath = missions_path(repo_paths)
    if mpath is None:
        log('missions.json path unresolved (agent-core not in repo_paths) — skipping')
        _emit_summary(ProposeResult(registry_unreadable=True), 'nothing', dry_run)
        return 0

    registry = read_missions_registry(mpath)
    if registry is None:
        _emit_summary(ProposeResult(registry_unreadable=True), 'nothing', dry_run)
        return 0

    if derive is None:
        derive = load_derive()
        if derive is None:
            _emit_summary(ProposeResult(derive_unavailable=True), 'nothing', dry_run)
            return 0

    if events_fetcher is None:
        events_fetcher = _default_events_fetcher
    try:
        rows = events_fetcher()
    except Exception as e:  # noqa: BLE001 — fail-safe
        log(f'event fetch raised: {type(e).__name__}: {e}')
        rows = None
    if rows is None:
        log('chain_events unavailable — proposing nothing this tick')
        _emit_summary(ProposeResult(events_unavailable=True), 'nothing', dry_run)
        return 0

    try:
        res = scan_and_propose(registry, rows, derive, now,
                               pr_state_resolver=pr_state_resolver)
    except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
        log(f'scan-and-propose raised: {type(e).__name__}: {e} — proposing nothing')
        _emit_summary(ProposeResult(), 'nothing', dry_run)
        return 0

    commit_status = 'nothing'
    if res.proposed and not dry_run:
        try:
            atomic_write_missions(mpath, registry)
        except Exception as e:  # noqa: BLE001 — fail-safe
            log(f'missions.json write raised: {type(e).__name__}: {e}')
            _emit_summary(res, 'commit-failed', dry_run)
            return 0
        core = repo_paths.get('ourliberty-agent-core')
        if core:
            try:
                commit_status = commit_and_push_missions(core, _commit_audit(res))
            except Exception as e:  # noqa: BLE001 — fail-safe
                log(f'commit+push raised: {type(e).__name__}: {e}')
                commit_status = 'push-failed'

    _emit_summary(res, commit_status, dry_run)
    return 0


def _commit_audit(res: ProposeResult) -> str:
    return (f'Auto-committed by {PROPOSED_BY}. '
            f'proposed={len(res.proposed)} scanned={res.scanned_orphans}.')


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='heal_orphan_autoregister.py',
        description='Missions v2 orphan auto-registration healer: scan non-terminal, '
                    'non-infrastructure orphans and propose phase=proposed missions.json '
                    'entries (idempotent + fail-safe).')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Report what WOULD be proposed; append nothing, write nothing, commit '
             'nothing.')
    args = parser.parse_args(argv)

    if _kill_switch_path().exists():
        log('KILLED_BY_SWITCH: healers.disabled present, exiting')
        return 0

    mode = 'DRY-RUN' if args.dry_run else 'LIVE'
    log(f'Starting missions orphan-autoregister ({mode})')
    rc = run_once(dry_run=args.dry_run)
    log('Done.')
    return rc


if __name__ == '__main__':
    sys.exit(main())
