#!/usr/bin/env python3
"""daemon_restart_manifest.py — single source of truth for which
long-running daemons must be restarted when a deploy changes the code
they run.

Background (deploy-restart-gap-001, 2026-06-03). `sync_agent_core.sh`
Step 7 historically restarted only `ourliberty-orchestrator.service` — a
unit that no longer exists on the droplet — so a sync that changed a
shared module imported by a long-running `Type=simple` daemon left that
daemon running stale in-memory code until the next manual or healer
restart. This module closes the gap by mapping each persistent daemon to
the set of repo paths whose change must trigger its restart.

Two consumers read the committed manifest (`config/daemon-restart-manifest.json`):
  - `sync_agent_core.sh` (deploy side): `units-for-changed` maps a
    `git diff --name-only OLD..NEW` list to the units to restart.
  - `heal_stale_daemon_code.py` (healer backstop): inverts the manifest
    into a path -> units watchlist so a daemon whose imported module
    changed (but whose entrypoint did not) is still caught.

Design choice (per brief): the manifest is an EXPLICIT, committed JSON —
single source of truth, handoff-readable, enforceable by test — rather
than an import graph auto-derived at runtime (clever but fragile). The
`regenerate` subcommand uses a transitive import scan to PRODUCE the
watch-path lists accurately, but the output is committed and reviewed,
not recomputed on every deploy.

Membership rule (enforced by test_daemon_restart_manifest.py): a unit
belongs in the manifest iff its checked-in `systemd/*.service` file is
`Type=simple` AND has no sibling `*.timer` (timer-driven units re-read
code on each fire, so they are not stale-in-memory daemons — e.g.
`ourliberty-cycle.service`).
"""
from __future__ import annotations

import ast
import json
import sys
import warnings
from pathlib import Path
from typing import Iterable, Optional

# Repo root: this file lives at <repo>/scripts/daemon_restart_manifest.py.
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / 'scripts'
SYSTEMD_DIR = REPO_ROOT / 'systemd'
MANIFEST_PATH = REPO_ROOT / 'config' / 'daemon-restart-manifest.json'

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from atomic_io import atomic_write_json  # noqa: E402


# -------------------- systemd unit introspection --------------------

def _exec_start_entrypoint(service_text: str) -> Optional[str]:
    """Extract the repo-relative script path from a .service file's
    ExecStart line. Returns a `scripts/<name>.py` string or None.

    Handles the two shapes used in this repo:
        ExecStart=/usr/bin/python3 /home/larry/agent-core/scripts/foo.py
        ExecStart=/usr/bin/env python3 -m uvicorn scripts.bar:app ...
    """
    for line in service_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith('ExecStart'):
            continue
        # `-m uvicorn scripts.<mod>:<attr>` module-mode shape.
        for tok in stripped.split():
            if tok.startswith('scripts.') and ':' in tok:
                module = tok.split(':', 1)[0]  # scripts.dashboard_api
                rel = Path(*module.split('.')).with_suffix('.py')
                return rel.as_posix()
        # Plain `<interpreter> .../scripts/<name>.py` shape: take the last
        # token that resolves under scripts/ and ends in .py.
        for tok in reversed(stripped.split()):
            if tok.endswith('.py') and '/scripts/' in tok:
                name = tok.rsplit('/scripts/', 1)[1]
                return f'scripts/{name}'
        return None
    return None


def persistent_simple_units(systemd_dir: Path = SYSTEMD_DIR) -> dict[str, str]:
    """Return {unit_name: entrypoint_relpath} for every persistent daemon.

    Persistent daemon = `Type=simple` .service file with NO sibling
    `.timer` file. This is the enforced membership rule for the manifest.
    """
    units: dict[str, str] = {}
    for svc in sorted(systemd_dir.glob('ourliberty-*.service')):
        text = svc.read_text()
        is_simple = any(
            line.strip().replace(' ', '').lower() == 'type=simple'
            for line in text.splitlines()
        )
        if not is_simple:
            continue
        if svc.with_suffix('.timer').exists():
            continue  # timer-driven: re-reads code each fire, not stale-prone
        entrypoint = _exec_start_entrypoint(text)
        if entrypoint is None:
            continue
        units[svc.name] = entrypoint
    return units


# -------------------- transitive import scan --------------------

def _first_party_modules(scripts_dir: Path = SCRIPTS_DIR) -> set[str]:
    """Module stems that correspond to a file in scripts/ (first-party)."""
    return {p.stem for p in scripts_dir.glob('*.py')}


def _direct_imports(py_file: Path, known: set[str]) -> set[str]:
    """Return the set of first-party module stems imported by `py_file`.

    Resolves both `import foo` / `from foo import x` and the package-
    qualified `import scripts.foo` / `from scripts.foo import x` shapes.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            tree = ast.parse(py_file.read_text())
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split('.')
                cand = head[1] if head[0] == 'scripts' and len(head) > 1 else head[0]
                if cand in known:
                    found.add(cand)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import; scripts/ is flat, skip
                continue
            if not node.module:
                continue
            head = node.module.split('.')
            cand = head[1] if head[0] == 'scripts' and len(head) > 1 else head[0]
            if cand in known:
                found.add(cand)
    return found


def transitive_watch_paths(
    entrypoint_rel: str, scripts_dir: Path = SCRIPTS_DIR,
) -> list[str]:
    """Compute the sorted set of repo-relative paths whose change should
    trigger a restart of the daemon launched from `entrypoint_rel`.

    The closure is the entrypoint plus every first-party module reachable
    through transitive imports. Returns repo-relative POSIX paths
    (`scripts/<name>.py`) so the set compares directly against
    `git diff --name-only` output.
    """
    known = _first_party_modules(scripts_dir)
    entry_stem = Path(entrypoint_rel).stem
    seen: set[str] = set()
    stack = [entry_stem]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        mod_file = scripts_dir / f'{mod}.py'
        if not mod_file.is_file():
            continue
        for dep in _direct_imports(mod_file, known):
            if dep not in seen:
                stack.append(dep)
    return sorted(f'scripts/{m}.py' for m in seen if (scripts_dir / f'{m}.py').is_file())


# -------------------- manifest load + mapping --------------------

def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    """Load the committed manifest. Returns {} on any read/parse failure
    (fail-open: callers fall back to today's behavior)."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def manifest_units(manifest: dict) -> dict[str, dict]:
    units = manifest.get('units', {})
    return units if isinstance(units, dict) else {}


def units_for_changed_paths(
    changed: Iterable[str], manifest: dict,
) -> list[str]:
    """Return the sorted set of units whose watch_paths intersect the
    changed-path set. Each unit appears at most once."""
    changed_set = {c.strip() for c in changed if c.strip()}
    if not changed_set:
        return []
    affected: set[str] = set()
    for unit, spec in manifest_units(manifest).items():
        if not isinstance(spec, dict):
            continue
        watch = spec.get('watch_paths', [])
        if not isinstance(watch, list):
            continue
        if changed_set.intersection(watch):
            affected.add(unit)
    return sorted(affected)


def path_to_units(manifest: dict) -> dict[str, set[str]]:
    """Invert the manifest into {repo_relative_path: {units}} for the
    healer's mtime watchlist."""
    inverse: dict[str, set[str]] = {}
    for unit, spec in manifest_units(manifest).items():
        if not isinstance(spec, dict):
            continue
        for p in spec.get('watch_paths', []):
            inverse.setdefault(p, set()).add(unit)
    return inverse


# -------------------- regeneration --------------------

def build_manifest() -> dict:
    """Produce a fresh manifest by scanning the checked-in unit files and
    each entrypoint's transitive import closure."""
    units: dict[str, dict] = {}
    for unit, entrypoint in persistent_simple_units().items():
        units[unit] = {
            'entrypoint': entrypoint,
            'watch_paths': transitive_watch_paths(entrypoint),
        }
    return {
        '_meta': {
            'description': (
                'Maps each persistent long-running daemon to the repo paths '
                'whose change requires restarting it on deploy. Consumed by '
                'sync_agent_core.sh (deploy restart) and '
                'heal_stale_daemon_code.py (healer backstop).'
            ),
            'membership_rule': (
                'Type=simple systemd unit in systemd/ with no sibling .timer.'
            ),
            'regenerate': 'python3 scripts/daemon_restart_manifest.py regenerate',
            'note': (
                'watch_paths are repo-relative POSIX paths derived from a '
                'transitive first-party import scan. Regenerate with the '
                'command above; heal_daemon_restart_manifest_drift.py keeps '
                'this file fresh (commits the regen) if a PR adds a new import '
                'without regenerating, so it never silently drifts.'
            ),
        },
        'units': dict(sorted(units.items())),
    }


# -------------------- CLI --------------------

def _cmd_regenerate() -> int:
    manifest = build_manifest()
    atomic_write_json(MANIFEST_PATH, manifest, indent=2, trailing_newline=True)
    print(f'wrote {MANIFEST_PATH} ({len(manifest_units(manifest))} units)')
    return 0


def _cmd_units_for_changed() -> int:
    changed = [line for line in sys.stdin.read().splitlines()]
    manifest = load_manifest()
    for unit in units_for_changed_paths(changed, manifest):
        print(unit)
    return 0


def _cmd_list_units() -> int:
    for unit in sorted(manifest_units(load_manifest())):
        print(unit)
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print('usage: daemon_restart_manifest.py '
              '{regenerate|units-for-changed|list-units}', file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == 'regenerate':
        return _cmd_regenerate()
    if cmd == 'units-for-changed':
        return _cmd_units_for_changed()
    if cmd == 'list-units':
        return _cmd_list_units()
    print(f'unknown subcommand: {cmd}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
