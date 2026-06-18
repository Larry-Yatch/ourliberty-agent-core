#!/usr/bin/env python3
"""heal_projects_store.py — the SOLE committer for the Projects-tab-v3 pipeline
store `agents/beacon/projects.json` (projects-v3 P3, step p3-project-store).

The single-committer invariant (North Star §5, spec § 5 "Store ownership"):
the projects store has exactly ONE writer to git — this healer. The dashboard
and Beacon are NON-committers: the promote/launch endpoints (P3 steps 2-3) write
projects.json atomically ON DISK (and Beacon may mutate the in-memory registry),
but neither opens a commit. This healer batches any on-disk delta into one
commit to main on its timer — exactly the captures.json / GC-healer shape
(`heal_missions_card_gc.py`), which exists because the #409→#413 dual-committer
class caused real data loss. Two writers to one file is the bug; one committer
draining everyone else's on-disk writes is the fix.

Each tick, all idempotent / atomic / fail-safe (a bad tick reports + skips,
never corrupts):

  1. NORMALIZE the registry (projects_store.normalize_registry): backfill
     defaults, coerce/validate lifecycle + project states, sort phases by
     `order`, drop only entries too malformed to keep (no id / not a dict).
     Missing file → seed the empty registry. Malformed JSON → skip this tick
     (never append onto a corrupt file).

  2. ATOMIC-WRITE the normalized registry iff it differs from disk (tmp +
     os.replace, so a concurrent reader never sees a partial file). A clean
     store produces no delta → no write → no commit.

  3. COMMIT + PUSH the projects.json delta to main (the durability half). Same
     push strategy as the GC healer: push; on a non-FF refusal, pull --rebase
     --autostash and retry; abort the rebase on conflict and retain the local
     commit. Never force-pushes. Refuses to commit off `main` (would land on a
     feature branch) — the caller escalates.

stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Repo scripts dir on sys.path so the sibling import (projects_store, larry_alerts)
# resolves when run by systemd.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import projects_store  # noqa: E402 — shared schema/normalization (single source)

_MODELS_CONFIG_PATH = _SCRIPTS_DIR.parent / 'config' / 'agent-models.json'
PROJECTS_REL = 'agents/beacon/projects.json'

GIT_TIMEOUT_SEC = 60
PUSH_TIMEOUT_SEC = 180


# ---------- env-resolved paths (read at call time so tests can override) ----------
def _agents_root() -> Path:
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))


def _kill_switch_path() -> Path:
    return _agents_root() / 'healers.disabled'


def _log_path() -> Path:
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    base = Path(override) if override else (_agents_root() / 'logs')
    return base / 'projects-store.log'


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


def load_repo_paths() -> dict[str, Path]:
    """Repo name → Path from config/agent-models.json ``repo_paths`` (the block
    heal_missions_card_gc.py reads). Returns {} on a missing/unreadable block."""
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


def projects_path(repo_paths: dict[str, Path]) -> Optional[Path]:
    """Path to agent-core's projects.json, or None if agent-core isn't
    configured. Honors OURLIBERTY_PROJECTS_JSON (test redirection)."""
    override = os.environ.get('OURLIBERTY_PROJECTS_JSON')
    if override:
        return Path(override)
    core = repo_paths.get('ourliberty-agent-core')
    return (core / PROJECTS_REL) if core else None


# ---------- read / write — fail-safe ----------
def read_registry(path: Path) -> Optional[dict[str, Any]]:
    """Load projects.json as a raw registry dict. Missing file → a fresh empty
    registry (so the first tick seeds it). Malformed JSON / non-dict → None: the
    caller skips the write+commit this tick rather than append onto a corrupt
    file (mirrors heal_missions_card_gc.read_captures_registry)."""
    if not path.exists():
        return projects_store.empty_registry()
    try:
        raw = path.read_text()
        data = json.loads(raw) if raw.strip() else projects_store.empty_registry()
    except (OSError, json.JSONDecodeError) as e:
        log(f'projects.json malformed/unreadable ({path}): {e} — skipping write+commit this tick')
        return None
    if not isinstance(data, dict):
        log(f'projects.json shape invalid ({path}) — skipping write+commit this tick')
        return None
    return data


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """tmp-in-same-dir + os.replace, so a concurrent reader (the derive) never
    sees a partial file. Mirrors heal_missions_card_gc._atomic_write_json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(json.dumps(data, indent=2) + '\n')
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------- git helpers (never raise) ----------
def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    """Run git in ``repo``; a timeout/OS error becomes a synthetic non-zero
    result so callers branch on returncode uniformly."""
    try:
        return subprocess.run(
            ['git', *args], cwd=str(repo),
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'git {" ".join(args)} failed in {repo}: {type(e).__name__}: {e}')
        return subprocess.CompletedProcess(args, returncode=255, stdout='', stderr=str(e))


def _projects_git_dirty(repo: Path) -> bool:
    """True iff projects.json differs from git HEAD (working tree or index).
    Read-only — used only by the dry-run report. The live path relies on
    commit_and_push_projects, which performs the same check authoritatively."""
    unstaged = _git(repo, 'diff', '--quiet', '--', PROJECTS_REL).returncode != 0
    staged = _git(repo, 'diff', '--quiet', '--cached', '--', PROJECTS_REL).returncode != 0
    return unstaged or staged


def _north_star_doc_rels(repo: Path, registry: dict[str, Any]) -> list[str]:
    """Repo-relative paths of the North Star docs referenced by the registry's
    projects (each project's ``north_star_ref``, ``#anchor`` stripped). The
    closeout's status-tracker tick writes one of these docs to disk
    (non-committer); the healer — SOLE committer — drains that delta in the SAME
    commit as the projects.json card write, so the card and its North Star tick
    land atomically (single-committer invariant, spec § 2 / § 3). Only in-repo,
    on-disk paths are returned (a null / stale / traversal ref is skipped)."""
    rels: list[str] = []
    seen: set[str] = set()
    repo_resolved = repo.resolve()
    for proj in registry.get('projects', []) or []:
        if not isinstance(proj, dict):
            continue
        ref = proj.get('north_star_ref')
        if not isinstance(ref, str):
            continue
        rel = ref.split('#', 1)[0].strip()
        if not rel or rel in seen:
            continue
        p = repo / rel
        try:
            p.resolve().relative_to(repo_resolved)
        except (ValueError, OSError):
            continue  # absolute / traversal / unresolvable — never stage it
        if not p.exists():
            continue
        seen.add(rel)
        rels.append(rel)
    return rels


def commit_and_push_projects(
    repo: Path, audit_msg: str, *, extra_rels: tuple[str, ...] = (),
) -> str:
    """Commit + push any projects.json (and referenced North Star doc) delta to
    origin/main. Returns a status token:
      'nothing'       — no delta to commit
      'wrong-branch'  — repo not on main; refuse to commit — caller escalates
      'committed'     — committed and pushed
      'commit-failed' / 'push-failed' — git step failed; commit retained locally

    ``extra_rels`` are additional repo-relative paths (the North Star tracker
    docs) staged into the SAME commit so the closeout's two artifacts — the phase
    card and its North Star tick — are durable together under the one committer.

    Push uses heal_missions_card_gc's strategy: try push; on a non-FF refusal,
    pull --rebase --autostash and retry; abort the rebase on conflict. Never
    force-pushes. As the SOLE committer for this single-writer file, a rebase
    conflict is near-impossible (no second writer to conflict with)."""
    head = _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    branch = head.stdout.strip() if head.returncode == 0 else ''
    if branch != 'main':
        return 'wrong-branch'

    rels = [PROJECTS_REL, *extra_rels]
    # rc 0 == no diff; rc 1 == differs. Tree is clean iff EVERY rel is clean in
    # both the working tree and the index — else there is a delta to commit.
    any_delta = False
    for rel in rels:
        unstaged = _git(repo, 'diff', '--quiet', '--', rel).returncode != 0
        staged = _git(repo, 'diff', '--quiet', '--cached', '--', rel).returncode != 0
        if unstaged or staged:
            any_delta = True
    if not any_delta:
        return 'nothing'

    for rel in rels:
        if _git(repo, 'add', rel).returncode != 0:
            return 'commit-failed'
    commit = _git(
        repo, 'commit',
        '-m', 'chore(projects): projects-store healer — commit projects.json delta',
        '-m', audit_msg,
    )
    if commit.returncode != 0:
        log(f'{PROJECTS_REL} commit failed in {repo}: '
            f'{(commit.stderr or commit.stdout).strip()[:200]}')
        return 'commit-failed'

    if _git(repo, 'push', '-q', 'origin', 'main', timeout=PUSH_TIMEOUT_SEC).returncode == 0:
        return 'committed'
    log(f'{PROJECTS_REL} push refused (likely non-FF); attempting pull --rebase --autostash')
    rebase = _git(repo, 'pull', '--rebase', '--autostash', '-q', 'origin', 'main',
                  timeout=PUSH_TIMEOUT_SEC)
    if rebase.returncode == 0:
        if _git(repo, 'push', '-q', 'origin', 'main', timeout=PUSH_TIMEOUT_SEC).returncode == 0:
            return 'committed'
        return 'push-failed'
    log(f'{PROJECTS_REL} rebase failed; aborting (commit retained locally)')
    _git(repo, 'rebase', '--abort')
    return 'push-failed'


# ---------- one tick ----------
def run_once(*, dry_run: bool, now: Optional[datetime] = None) -> int:
    """One healer tick. Resolves the store path, normalizes, atomic-writes any
    delta, then commits + pushes it. Fail-safe: any unexpected error is logged
    and the tick returns non-zero rather than corrupting the file."""
    now = now or datetime.now(timezone.utc)
    repo_paths = load_repo_paths()
    path = projects_path(repo_paths)
    if path is None:
        log('projects.json path unresolved (agent-core not in repo_paths) — skipping')
        return 0

    raw = read_registry(path)
    if raw is None:
        # malformed-on-disk: read_registry already logged; skip this tick.
        return 1

    try:
        normalized, dropped = projects_store.normalize_registry(raw, now=now)
    except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
        log(f'normalize raised: {type(e).__name__}: {e} — skipping write+commit this tick')
        return 1

    if dropped:
        log(f'dropped {len(dropped)} malformed project/phase entr(y/ies): {dropped}')

    changed = (normalized != raw) or not path.exists()
    n_projects = len(normalized.get('projects', []))
    core = repo_paths.get('ourliberty-agent-core')
    # North Star docs the closeout may have ticked (non-committer wrote them to
    # disk); the healer drains them into the same commit as the card.
    extra_rels = tuple(_north_star_doc_rels(core, normalized)) if core else ()

    if dry_run:
        # Report the *git* delta too, not just the normalization delta: the
        # dashboard/Beacon write a well-formed, already-normalized projects.json
        # to disk, so `changed` is False on the common path even though there is
        # a real git delta the live tick must commit. Read-only here.
        git_dirty = core is not None and (
            _projects_git_dirty(core)
            or any(_git(core, 'diff', '--quiet', '--', r).returncode != 0
                   or _git(core, 'diff', '--quiet', '--cached', '--', r).returncode != 0
                   for r in extra_rels))
        if changed:
            disp = 'would write + commit (normalization delta)'
        elif git_dirty:
            disp = 'would commit (git delta; no normalization change)'
        else:
            disp = 'no delta'
        log(f'DRY-RUN: {n_projects} project(s); {disp}; dropped={len(dropped)}')
        return 0

    if changed:
        try:
            _atomic_write_json(path, normalized)
        except OSError as e:
            log(f'atomic write failed ({path}): {e}')
            return 1

    # ALWAYS attempt the commit — NOT only when normalization changed something.
    # The dashboard/Beacon are non-committers: they write projects.json atomically
    # ON DISK (well-formed + already-normalized → `changed` is False), and rely on
    # this healer to drain the git delta. Gating the commit on `changed` left
    # those writes uncommitted forever, blocking ourliberty-sync. Match the
    # captures/missions GC healer: gate the WRITE on our own normalization, but
    # always attempt the commit — commit_and_push_projects checks the git delta
    # and returns 'nothing' on a clean tree, so an idle tick stays quiet.
    commit_status = 'nothing'
    if core is not None:
        audit = f'projects={n_projects} dropped={len(dropped)} normalized@{now.isoformat()}'
        commit_status = commit_and_push_projects(core, audit, extra_rels=extra_rels)
        log(f'commit status: {commit_status}')

    log(f'Done. projects={n_projects} changed={changed} commit={commit_status} dropped={len(dropped)}')
    # A wrong-branch / commit-failed / push-failed is a soft failure (the write
    # landed on disk; only the durability commit didn't) → non-zero so the timer
    # surfaces it, but never a crash.
    return 0 if commit_status in ('nothing', 'committed') else 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='heal_projects_store.py',
        description='Projects-store healer (single committer): normalize + '
                    'commit the projects.json delta to main.')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Report what WOULD be normalized/committed; write nothing, commit nothing.')
    args = parser.parse_args(argv)

    if _kill_switch_path().exists():
        log('KILLED_BY_SWITCH: healers.disabled present, exiting')
        return 0

    mode = 'DRY-RUN' if args.dry_run else 'LIVE'
    log(f'Starting projects-store healer ({mode})')
    return run_once(dry_run=args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
