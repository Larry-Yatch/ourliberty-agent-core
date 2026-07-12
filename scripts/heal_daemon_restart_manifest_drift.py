#!/usr/bin/env python3
"""heal_daemon_restart_manifest_drift.py — keep the daemon-restart manifest fresh.

manifest-self-heal-001 (2026-06-21). `config/daemon-restart-manifest.json`
maps each persistent `Type=simple` daemon to the repo paths whose change must
restart it — its transitive first-party import closure. TWO restart paths
consume it:

  - `sync_agent_core.sh` (deploy-restart): restart units whose `git diff
    OLD..NEW` intersects their `watch_paths`.
  - `heal_stale_daemon_code.py` (backstop): restart a unit whose entrypoint or
    any manifest-listed import is newer than the process's start time.

The closure is computed once (`daemon_restart_manifest.py regenerate`) and
committed — nothing keeps it fresh. When a PR adds a new first-party import to a
daemon's closure but does not regenerate the manifest, that dependency is
invisible to BOTH restart paths, so the daemon silently serves stale code after
the next deploy until someone restarts it by hand.

That is exactly what hid PR #617 (P6 brainstorm) and #621: `dashboard_api.py`
reaches `projects_brainstorm_author.py` transitively (via
`heal_projects_store.py`), but the committed manifest's dashboard-api entry was
missing it, so neither restart path knew dashboard-api depended on the changed
file.

This healer makes the manifest self-maintaining so it can never silently drift:

  Each tick:
    1. fresh = build_manifest()      — the truth derived from current code.
    2. committed = load_manifest()   — what is on disk.
    3. compute_drift over the `units` blocks (watch_paths + entrypoint; `_meta`
       is cosmetic and ignored).
    4. No drift  → no-op (the steady state; idempotent).
    5. Drift     → write the fresh manifest, commit + push ONLY that path to
       origin/main (single-committer, pathspec-limited, FF-or-rebase-retry,
       never force, refuse off-main), and emit ONE digest alert summarizing the
       per-unit added/removed paths. A commit/push failure or off-main refusal
       ESCALATES with the manual `regenerate` command.

This healer only keeps the manifest fresh; it never restarts a daemon. The
restart stays `heal_stale_daemon_code`'s job — on its next tick it reads the
now-fresh manifest and restarts any newly-tracked, currently-stale daemon
(decoupled, each independently testable).

Safe-by-construction:
  - Never raises: every git/IO op degrades to a logged no-op (a read-only or
    full FS, a wedged git, a missing larry_alerts all leave the system in
    today's state, never crash the one-shot).
  - Kill-switch aware: `~/agents/healers.disabled` short-circuits before any
    work.
  - Idempotent: once the drift is committed, the next tick sees no drift and
    does nothing — so a healthy state never re-pages (the failure path leans on
    larry_alerts' subject-keyed cooldown).
  - Single committer: pathspec-limited commit touches ONLY the manifest, never
    co-committing another machine-owned file (cf. machine-owned-file-single-committer).
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic_io import atomic_write_json  # noqa: E402
import daemon_restart_manifest as drm  # noqa: E402

SOURCE = 'heal-daemon-restart-manifest-drift'
MANIFEST_REL = 'config/daemon-restart-manifest.json'

# The escalate subjects this healer can emit (each == the failing status token).
# The positive-clear retraction (on the 'fresh' / no-drift branch) keys on the
# SAME source:subject so it retracts exactly its own stale reds. The
# 'regenerated' (committed) subject routes to the digest lane, not a red.
_ESCALATE_SUBJECTS = ('wrong-branch', 'commit-failed', 'push-failed',
                      'write-failed')

GIT_TIMEOUT_SEC = 60
PUSH_TIMEOUT_SEC = 180

# How many added/removed paths to spell out per unit in the alert body before
# collapsing the tail to a "(+N more)" count. Never a SILENT truncation — the
# exact total is always shown and the full set is in the committed diff.
ALERT_PATHS_PER_UNIT = 12


# -------------------- env-resolved paths (read at call time for tests) --------------------

def _agents_root() -> Path:
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))


def _kill_switch_path() -> Path:
    return _agents_root() / 'healers.disabled'


def _log_path() -> Path:
    """Honor OURLIBERTY_LOG_DIR (test sandbox) so a test run never writes into
    the live ~/agents/logs tree."""
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    base = Path(override) if override else (_agents_root() / 'logs')
    return base / 'heal-daemon-restart-manifest-drift.log'


def _heartbeat_path() -> Path:
    return _agents_root() / 'blackboard' / 'heal-daemon-restart-manifest-drift.heartbeat'


def log(msg: str, level: str = 'INFO') -> None:
    line = f'[{datetime.now(timezone.utc).isoformat()}] [{level}] {msg}'
    print(line, flush=True)
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        # Best-effort: a full/read-only log FS must never crash the healer.
        pass


def heartbeat() -> None:
    try:
        path = _heartbeat_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def kill_switch_active() -> bool:
    return _kill_switch_path().exists()


# -------------------- drift computation (pure) --------------------

@dataclass
class UnitDelta:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    entrypoint_changed: Optional[tuple[str, str]] = None  # (old, new)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.entrypoint_changed)


@dataclass
class DriftResult:
    # unit -> UnitDelta (only units whose watch_paths/entrypoint changed)
    units_changed: dict[str, UnitDelta] = field(default_factory=dict)
    units_added: list[str] = field(default_factory=list)    # in fresh, not committed
    units_removed: list[str] = field(default_factory=list)  # in committed, not fresh

    @property
    def has_drift(self) -> bool:
        return bool(self.units_changed or self.units_added or self.units_removed)

    def one_line(self) -> str:
        bits = []
        for unit in sorted(self.units_changed):
            d = self.units_changed[unit]
            piece = unit
            if d.added:
                piece += f' +{len(d.added)}'
            if d.removed:
                piece += f' -{len(d.removed)}'
            if d.entrypoint_changed:
                piece += ' entrypoint!'
            bits.append(piece)
        if self.units_added:
            bits.append('NEW units: ' + ', '.join(sorted(self.units_added)))
        if self.units_removed:
            bits.append('DROPPED units: ' + ', '.join(sorted(self.units_removed)))
        return '; '.join(bits) if bits else '(none)'

    def alert_body(self) -> str:
        lines: list[str] = []
        for unit in sorted(self.units_changed):
            d = self.units_changed[unit]
            lines.append(f'{unit}:')
            if d.entrypoint_changed:
                lines.append(f'  entrypoint: {d.entrypoint_changed[0]} -> '
                             f'{d.entrypoint_changed[1]}')
            if d.added:
                lines.append('  now tracked (+%d): %s' % (
                    len(d.added), _fmt_paths(d.added)))
            if d.removed:
                lines.append('  no longer tracked (-%d): %s' % (
                    len(d.removed), _fmt_paths(d.removed)))
        if self.units_added:
            lines.append('NEW daemons: ' + ', '.join(sorted(self.units_added)))
        if self.units_removed:
            lines.append('DROPPED daemons: ' + ', '.join(sorted(self.units_removed)))
        return '\n'.join(lines)


def _fmt_paths(paths: list[str]) -> str:
    paths = sorted(paths)
    if len(paths) <= ALERT_PATHS_PER_UNIT:
        return ', '.join(Path(p).name for p in paths)
    shown = ', '.join(Path(p).name for p in paths[:ALERT_PATHS_PER_UNIT])
    return f'{shown} (+{len(paths) - ALERT_PATHS_PER_UNIT} more)'


def _watch_set(spec: object) -> set[str]:
    if not isinstance(spec, dict):
        return set()
    watch = spec.get('watch_paths', [])
    return {p for p in watch if isinstance(p, str)} if isinstance(watch, list) else set()


def _entrypoint(spec: object) -> Optional[str]:
    if not isinstance(spec, dict):
        return None
    ep = spec.get('entrypoint')
    return ep if isinstance(ep, str) else None


def compute_drift(committed: dict, fresh: dict) -> DriftResult:
    """Diff the two manifests' `units` blocks. `_meta` is cosmetic and ignored.

    Drift = any unit gained/lost a watch_path, changed entrypoint, or any
    daemon was added/removed. Returns a DriftResult; `has_drift` is False iff
    the committed `units` already equal the freshly-derived closure."""
    cu = drm.manifest_units(committed)
    fu = drm.manifest_units(fresh)
    result = DriftResult()
    result.units_added = sorted(set(fu) - set(cu))
    result.units_removed = sorted(set(cu) - set(fu))
    for unit in sorted(set(cu) & set(fu)):
        c_paths, f_paths = _watch_set(cu[unit]), _watch_set(fu[unit])
        delta = UnitDelta(
            added=sorted(f_paths - c_paths),
            removed=sorted(c_paths - f_paths),
        )
        c_ep, f_ep = _entrypoint(cu[unit]), _entrypoint(fu[unit])
        if c_ep != f_ep:
            delta.entrypoint_changed = (c_ep or '', f_ep or '')
        if delta.changed:
            result.units_changed[unit] = delta
    # A brand-new or dropped unit also implies its paths; surface those in the
    # body via units_added/removed (the per-unit deltas above only cover the
    # intersection).
    return result


# -------------------- git helpers (never raise) --------------------

def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ['git', *args], cwd=str(repo),
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'git {" ".join(args)} failed in {repo}: {type(e).__name__}: {e}', 'WARN')
        return subprocess.CompletedProcess(args, returncode=255, stdout='', stderr=str(e))


def current_branch(repo: Path) -> str:
    head = _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    return head.stdout.strip() if head.returncode == 0 else ''


def commit_and_push_manifest(repo: Path, rel_path: str, audit_msg: str) -> str:
    """Commit + push the manifest delta to origin/main. Returns a status token:
       'nothing' / 'wrong-branch' / 'committed' / 'commit-failed' / 'push-failed'.

    Mirrors heal_missions_card_gc's single-committer push strategy (try push;
    on a non-FF refusal, `pull --rebase --autostash` and retry; abort the rebase
    on conflict; never force-push). The commit is pathspec-limited to `rel_path`
    so it can NEVER co-commit another machine-owned file that happens to be dirty
    in the droplet's working tree (single-committer invariant)."""
    if current_branch(repo) != 'main':
        return 'wrong-branch'

    clean = _git(repo, 'diff', '--quiet', '--', rel_path)
    clean_cached = _git(repo, 'diff', '--quiet', '--cached', '--', rel_path)
    if clean.returncode == 0 and clean_cached.returncode == 0:
        return 'nothing'

    # Pathspec-limited commit: stages + commits ONLY rel_path, ignoring anything
    # else already staged in the index.
    commit = _git(repo, 'commit', '-m',
                  'chore(deploy): manifest-drift healer — regenerate daemon-restart-manifest',
                  '-m', audit_msg, '--', rel_path)
    if commit.returncode != 0:
        log(f'{rel_path} commit failed in {repo}: '
            f'{(commit.stderr or commit.stdout).strip()[:200]}', 'WARN')
        return 'commit-failed'

    if _git(repo, 'push', '-q', 'origin', 'main', timeout=PUSH_TIMEOUT_SEC).returncode == 0:
        return 'committed'
    log(f'{rel_path} push refused (likely non-FF); pull --rebase --autostash + retry', 'WARN')
    rebase = _git(repo, 'pull', '--rebase', '--autostash', '-q', 'origin', 'main',
                  timeout=PUSH_TIMEOUT_SEC)
    if rebase.returncode == 0:
        if _git(repo, 'push', '-q', 'origin', 'main', timeout=PUSH_TIMEOUT_SEC).returncode == 0:
            return 'committed'
        # Rebased cleanly onto origin but the re-push failed (transient). HEAD is
        # now a linear DESCENDANT of origin/main, so sync's `merge --ff-only`
        # stays a no-op (not diverged); the next tick re-pushes. Safe to leave.
        return 'push-failed'
    # Rebase conflict: origin changed the manifest concurrently (e.g. a human
    # `regenerate` PR landed between our fetch and push). Abort, then DROP our
    # local commit (`reset --hard HEAD~1`) so the droplet's HEAD never stays
    # DIVERGED from origin — otherwise the next ourliberty-sync
    # `git merge --ff-only origin/main` fails and escalate-pages
    # 'sync-blocked:fast-forward-failed'. `--hard` (not `--mixed`) is correct
    # here precisely because the manifest is a deterministic derived artifact:
    # discarding the working copy loses nothing — the next healer tick simply
    # regenerates + re-commits against the now-current origin. After the reset
    # local is an ancestor of origin (behind, not diverged) so sync fast-forwards.
    log(f'{rel_path} rebase conflict; aborting + dropping local commit '
        f'(reset --hard HEAD~1) to avoid diverging origin', 'WARN')
    _git(repo, 'rebase', '--abort')
    _git(repo, 'reset', '--hard', 'HEAD~1')
    return 'push-failed'


# -------------------- alerting --------------------

def _import_larry_alerts():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import larry_alerts as la  # noqa: E402
    return la


def emit_alert(drift: DriftResult, status: str) -> bool:
    """One alert per drift event. A successful self-heal routes via classify_route
    (digest for this routine maintenance); any failure escalates with the manual
    recovery command so a wedged committer surfaces."""
    try:
        la = _import_larry_alerts()
    except Exception as e:  # larry_alerts import is best-effort
        log(f'larry_alerts import failed: {type(e).__name__}: {e}', 'WARN')
        return False

    manual = ('cd /home/larry/agent-core && '
              'python3 scripts/daemon_restart_manifest.py regenerate && '
              'git commit -m "chore(deploy): regenerate daemon-restart-manifest" '
              '-- config/daemon-restart-manifest.json && git push origin main')

    try:
        if status == 'committed':
            subject = 'regenerated'
            body = (
                'Daemon-restart manifest had drifted from the live import '
                'closure; regenerated + committed to main.\n\n'
                + drift.alert_body()
                + '\n\nThe deploy-restart and heal_stale_daemon_code backstop now '
                'track these paths; the backstop\'s next tick restarts any '
                'daemon left stale by the previously-untracked dependency.'
            )
            route = la.classify_route(SOURCE, subject, healed=True)
            return la.append_alert(source=SOURCE, severity='warning',
                                   subject=subject, message=body, route=route)

        # Any non-success: the manifest is (or may be) still drifted/un-pushed.
        # Escalate so it does not silently rot — this is the fail-loud path.
        subject = status  # 'wrong-branch' / 'commit-failed' / 'push-failed' / 'write-failed'
        verb = {
            'wrong-branch': 'refused (checkout not on main)',
            'commit-failed': 'commit FAILED',
            'push-failed': 'committed locally but PUSH FAILED',
            'write-failed': 'could not write the regenerated manifest',
        }.get(status, status)
        body = (
            f'Daemon-restart manifest drift {verb}. Daemons may keep serving '
            f'stale code until the manifest is fresh on disk + origin.\n\n'
            + drift.alert_body()
            + f'\n\nManual recovery:\n  {manual}'
        )
        return la.append_alert(source=SOURCE, severity='warning', subject=subject,
                               message=body, suggested_action=manual, route='escalate')
    except Exception as e:
        log(f'emit_alert failed: {type(e).__name__}: {e}', 'WARN')
        return False


def _retract_standdown() -> int:
    """Positive-clear retraction: the committed manifest matches the live import
    closure again (no drift), so any earlier drift-escalation red this healer
    emitted is resolved. Retract each escalate subject, keyed on this healer's
    own `source:subject`, and emit one closure stand-down per subject actually
    removed (auditable, never silent). Best-effort — swallow any error so it can
    never wedge the tick."""
    try:
        la = _import_larry_alerts()
    except Exception as e:  # noqa: BLE001 — import is best-effort
        log(f'_retract_standdown import failed: {type(e).__name__}: {e}', 'WARN')
        return 0
    total = 0
    for subject in _ESCALATE_SUBJECTS:
        try:
            removed = la.retract_with_standdown(
                f'{SOURCE}:{subject}',
                standdown_message=(
                    'Daemon-restart manifest matches the live import closure '
                    f'again — the earlier drift alert ({subject}) has resolved. '
                    'Standing it down.'
                ),
            )
            if removed:
                log(f'retracted {removed} stale {subject} alert line(s) '
                    f'(manifest fresh again)')
            total += removed
        except Exception as e:  # noqa: BLE001
            log(f'_retract_standdown[{subject}] failed: '
                f'{type(e).__name__}: {e}', 'WARN')
    return total


# -------------------- orchestration --------------------

def run_once(*, dry_run: bool = False) -> str:
    """Detect + (if drifted) self-heal the manifest. Returns an outcome token:
       'fresh' / 'committed' / 'push-failed' / 'commit-failed' / 'wrong-branch'
       / 'write-failed' / 'drift-dry-run' / 'error'."""
    try:
        committed = drm.load_manifest()
        fresh = drm.build_manifest()
    except Exception as e:
        # build_manifest scans the tree; a parse/IO error there must not crash
        # the one-shot. Degrade to a logged no-op.
        log(f'could not build/load manifest: {type(e).__name__}: {e}', 'WARN')
        return 'error'

    drift = compute_drift(committed, fresh)
    if not drift.has_drift:
        log('no drift: committed manifest matches the live import closure')
        # POSITIVE-CLEAR ONLY: retract our own drift reds solely on a positively
        # observed no-drift state (committed manifest == freshly-built closure).
        # A build/load failure returns 'error' above BEFORE this branch, so a
        # degraded read can never reach the retract.
        _retract_standdown()
        return 'fresh'

    log('DRIFT: ' + drift.one_line(), 'WARN')
    if dry_run:
        return 'drift-dry-run'

    repo = drm.MANIFEST_PATH.parent.parent
    rel = MANIFEST_REL

    # Refuse to dirty a non-main checkout (the droplet is normally on main; a
    # feature-branch checkout means something is off — surface it, don't write).
    branch = current_branch(repo)
    if branch != 'main':
        log(f'refusing to write/commit: checkout is on {branch!r}, not main', 'WARN')
        emit_alert(drift, 'wrong-branch')
        return 'wrong-branch'

    try:
        atomic_write_json(drm.MANIFEST_PATH, fresh, indent=2, trailing_newline=True)
    except OSError as e:
        log(f'could not write {drm.MANIFEST_PATH}: {e}', 'WARN')
        emit_alert(drift, 'write-failed')
        return 'write-failed'

    status = commit_and_push_manifest(repo, rel, drift.one_line())
    emit_alert(drift, status)
    log(f'self-heal outcome: {status}')
    return status


def main() -> int:
    if kill_switch_active():
        log(f'KILL_SWITCH active at {_kill_switch_path()}; exiting cleanly')
        return 0
    heartbeat()
    run_once()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        sys.exit(1)
