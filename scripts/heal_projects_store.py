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
import projects_brainstorm_author  # noqa: E402 — P6 brainstorm-autofill in-registry sweep

_MODELS_CONFIG_PATH = _SCRIPTS_DIR.parent / 'config' / 'agent-models.json'
PROJECTS_REL = 'agents/beacon/projects.json'
# READ-ONLY here: the kanban→pipeline migration mirrors ACTIVE missions as
# pipeline projects but NEVER writes missions.json (that store has its own
# committer). projects-v3 retire-missions-kanban.
MISSIONS_REL = 'agents/beacon/missions.json'

GIT_TIMEOUT_SEC = 60
PUSH_TIMEOUT_SEC = 180


# ---------- env-resolved paths (read at call time so tests can override) ----------
def _agents_root() -> Path:
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))


def _kill_switch_path() -> Path:
    return _agents_root() / 'healers.disabled'


def _retire_window_sec() -> int:
    """The Done-card visibility window (seconds) the GC waits before auto-retiring
    a finished project. Read at call time (so tests can override) from
    OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC, defaulting to the store constant. A
    non-int override falls back to the default rather than wedging the tick. The
    env read lives HERE (the caller), not in projects_store.retire_completed_
    projects, which stays pure (no IO) so both writers and readers can share it."""
    raw = os.environ.get('OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC')
    if raw is None:
        return projects_store.DONE_RETIRE_VISIBILITY_SEC
    try:
        return int(raw)
    except (ValueError, TypeError):
        log(f'OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC not an int ({raw!r}) — '
            f'using default {projects_store.DONE_RETIRE_VISIBILITY_SEC}s')
        return projects_store.DONE_RETIRE_VISIBILITY_SEC


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


def missions_path(repo_paths: dict[str, Path]) -> Optional[Path]:
    """Path to agent-core's missions.json — READ-ONLY (the migration mirrors
    active missions as projects, never writes missions.json). Honors
    OURLIBERTY_MISSIONS_JSON (test redirection)."""
    override = os.environ.get('OURLIBERTY_MISSIONS_JSON')
    if override:
        return Path(override)
    core = repo_paths.get('ourliberty-agent-core')
    return (core / MISSIONS_REL) if core else None


def read_missions(path: Optional[Path]) -> list[Any]:
    """The ``missions[]`` list from missions.json, or ``[]`` on any fault (path
    unresolved / missing / malformed / wrong shape). FAIL-SAFE + READ-ONLY: a bad
    missions file mirrors NOTHING this tick — it never blocks the projects
    normalize+commit, and this healer never writes missions.json."""
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f'missions.json unreadable ({path}): {e} — mirroring no missions this tick')
        return []
    missions = data.get('missions') if isinstance(data, dict) else None
    return missions if isinstance(missions, list) else []


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
    """Delegates to the shared guarded atomic_io writer. Byte-identical to the
    prior inline writer: pretty JSON (indent=2) + trailing newline."""
    import atomic_io
    atomic_io.atomic_write_json(path, data, trailing_newline=True)


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


def _retire_log_detail(
    registry: dict[str, Any], retired_ids: list[str], now: Optional[datetime],
) -> str:
    """Render ``id(done_at=T, age=Nh)`` for each retired project so the log
    explains WHEN it reached Done and HOW LONG it waited. Reads the ``gc.done_at``
    stamp ``retire_completed_projects`` just wrote (present in both live and
    dry-run, since the retire pass stamps ``normalized`` before either commits).
    Fail-safe: a missing/unparseable stamp degrades to the bare id."""
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    by_id = {p.get('id'): p for p in registry.get('projects', []) or []
             if isinstance(p, dict)}
    parts: list[str] = []
    for pid in retired_ids:
        proj = by_id.get(pid)
        gc = proj.get('gc') if isinstance(proj, dict) else None
        done_at_raw = gc.get('done_at') if isinstance(gc, dict) else None
        try:
            done_at = datetime.fromisoformat(done_at_raw)
            if done_at.tzinfo is None:
                done_at = done_at.replace(tzinfo=timezone.utc)
            age_h = (now_dt - done_at).total_seconds() / 3600
            parts.append(f'{pid}(done_at={done_at_raw}, age={age_h:.1f}h)')
        except (ValueError, TypeError):
            parts.append(str(pid))
    return ', '.join(parts)


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

    # Mirror ACTIVE (non-shipped) missions into the pipeline as single-phase
    # projects so the legacy Missions kanban can be retired (projects-v3
    # retire-missions-kanban). Mutates `raw` in place (adds projects only);
    # idempotent + reversible; READS missions.json, never writes it. Fail-safe: a
    # migration fault mirrors nothing and never blocks the normalize+commit below.
    minted: list[str] = []
    try:
        minted = projects_store.mirror_missions_as_projects(
            raw, read_missions(missions_path(repo_paths)), now=now)
        if minted:
            verb = 'would mirror' if dry_run else 'mirrored'
            log(f'{verb} {len(minted)} active mission(s) into the pipeline: {minted}')
    except Exception as e:  # noqa: BLE001 — fail-safe: never block the committer
        log(f'mirror-missions raised: {type(e).__name__}: {e} — mirroring nothing this tick')

    # Author the P6 brainstorm autofill onto every phase that just entered the
    # Brainstorm lifecycle state (projects-v3 P6: pre-filled 8-section draft +
    # capped "Your decisions" + handoff payload). Mutates `raw` in place — the
    # SAME in-registry mutation pattern missions_narrator.author_captures_in_
    # registry uses, so this healer stays the SOLE committer of projects.json (no
    # second writer). Fail-safe: an authoring fault drafts nothing and never blocks
    # the normalize+commit below. Gated on `not dry_run`: a dry tick is read-only
    # (it must not spawn claude OR mutate `raw`, so its git/normalization-delta
    # report stays faithful) — the next live tick authors the pending phases.
    authored = 0
    if not dry_run:
        try:
            authored, _deferred = projects_brainstorm_author.author_brainstorms_in_registry(
                raw, now=now, use_llm=True)
            if authored:
                log(f'authored {authored} brainstorm draft(s) for newly-Brainstorm phase(s)')
        except Exception as e:  # noqa: BLE001 — fail-safe: never block the committer
            log(f'brainstorm-autofill raised: {type(e).__name__}: {e} — drafting nothing this tick')

    try:
        normalized, dropped = projects_store.normalize_registry(raw, now=now)
    except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
        log(f'normalize raised: {type(e).__name__}: {e} — skipping write+commit this tick')
        return 1

    if dropped:
        log(f'dropped {len(dropped)} malformed project/phase entr(y/ies): {dropped}')

    # Terminal-mission reconcile: Done-stamp any ACTIVE project mirrored from a
    # mission that has SINCE shipped/retired, so the GC retire below can sweep it
    # off "Actively working". Closes the mirror↔ship race — a project born at
    # `building` AFTER its build already merged has no event to advance it (see
    # projects_store.reconcile_terminal_mission_projects). Operates on `normalized`
    # and runs BEFORE the retire pass, so a freshly-Done project already past its
    # visibility window retires in the SAME tick. Fail-safe: a fault reconciles
    # nothing and never blocks the write+commit below.
    reconciled: list[str] = []
    try:
        reconciled = projects_store.reconcile_terminal_mission_projects(
            normalized, read_missions(missions_path(repo_paths)), now=now)
        if reconciled:
            verb = 'would reconcile' if dry_run else 'reconciled'
            log(f'{verb} {len(reconciled)} project(s) whose source mission is '
                f'terminal (shipped/retired) → Done: {reconciled}')
    except Exception as e:  # noqa: BLE001 — fail-safe: never block the committer
        log(f'terminal-mission reconcile raised: {type(e).__name__}: {e} '
            f'— reconciling nothing this tick')

    # Project-level GC reconciliation: retire any ACTIVE project whose phases are
    # ALL done off the "Actively working" board (projects-stale-gc-archive-
    # completed). The project analogue of the missions GC terminal reconciliation —
    # it runs HERE, after normalize and BEFORE the atomic-write+commit, so the SOLE
    # committer drains the retire delta in its own commit (single-committer
    # invariant). Operates on `normalized` (valid states/ids). Fail-safe: a GC fault
    # retires nothing and never blocks the write+commit below. `retired` (not
    # `archived`) keeps the promoted funnel source suppressed — see
    # projects_store.retire_completed_projects.
    retired: list[str] = []
    window_sec = _retire_window_sec()
    try:
        _, retired = projects_store.retire_completed_projects(
            normalized, now=now, min_done_age_sec=window_sec)
        if retired:
            verb = 'would retire' if dry_run else 'retired'
            detail = _retire_log_detail(normalized, retired, now)
            log(f'{verb} {len(retired)} all-phases-done project(s) past the '
                f'{window_sec}s visibility window off the board: {detail}')
    except Exception as e:  # noqa: BLE001 — fail-safe: never block the committer
        log(f'project-GC raised: {type(e).__name__}: {e} — retiring nothing this tick')

    # `bool(minted)` forces the write+commit: the freshly-minted projects come out
    # of new_single_phase_project already normalized, so `normalized != raw` is
    # False (raw was mutated in place) even though disk lacks them — without this
    # the mirror delta would never reach disk. `bool(authored)` does the same for
    # the brainstorm sweep: it mutates `raw` before normalize, so the draft is in
    # both `raw` and `normalized` and the `!=` check alone would miss the delta.
    # `bool(retired)` forces the commit for the GC retire delta (it mutates
    # `normalized` after the `!=` baseline was captured against `raw`).
    # `bool(reconciled)` joins the gate for the same reason as `bool(retired)`:
    # the terminal-mission reconcile Done-stamps phases on `normalized` AFTER the
    # `!=` baseline was captured against `raw`, so the `!=` check alone would miss it.
    changed = ((normalized != raw) or bool(minted) or bool(authored)
               or bool(reconciled) or bool(retired) or not path.exists())
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
