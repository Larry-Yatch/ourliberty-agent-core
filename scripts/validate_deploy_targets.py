#!/usr/bin/env python3
"""validate_deploy_targets.py — registry validator for
config/deploy_targets.json (Phase E2.1).

The registry is the canonical record of every GitHub repo connected to a
Vercel project. The deploy notifier (E2.2) reads it to decide which pushes
to forward to which Vercel project. This validator enforces the schema +
semantic invariants so a malformed entry can't ship — the notifier, the
sync drift-detector, and Mirror's PR review all assume the registry is
well-formed.

Library use:
    from validate_deploy_targets import validate_registry
    ok, reasons = validate_registry(path_or_dict)

CLI use:
    python3 scripts/validate_deploy_targets.py [PATH]
    # exit 0 = valid; exit 1 = invalid (reasons on stderr)

Stdlib only. The validator is read-only — it never mutates the registry.
Empty deploy_targets array IS a valid state (matches initial-ship intent
in E2.1; first real entry lands in E2.3 once Vercel IDs exist).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Union

SCHEMA_VERSION = 1

REQUIRED_FIELDS = (
    'name', 'github_repo', 'vercel_project_id', 'vercel_org_id',
    'framework', 'env_var_keys', 'branch_filter', 'preview_enabled',
    'production_enabled', 'created_at', 'notes',
)

_NAME_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
_GITHUB_REPO_RE = re.compile(r'^[^/\s]+/[^/\s]+$')
_VERCEL_PROJECT_ID_RE = re.compile(r'^prj_[A-Za-z0-9]+$')
_VERCEL_ORG_ID_RE = re.compile(r'^team_[A-Za-z0-9]+$')


def _repo_root() -> Path:
    """Resolve repo root as the parent of scripts/. Defensive — works whether
    the validator is invoked from repo root or anywhere else."""
    return Path(__file__).resolve().parent.parent


def _parse_iso_date(s: Any) -> Optional[datetime]:
    if not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d')
    except ValueError:
        return None


def _validate_entry(
    entry: Any,
    index: int,
    seen_names: set[str],
    known_frameworks: tuple[str, ...],
) -> list[str]:
    """Return a list of reasons (empty = entry valid)."""
    reasons: list[str] = []
    if not isinstance(entry, dict):
        return [f'deploy_targets[{index}]: must be an object, '
                f'got {type(entry).__name__}']
    name = entry.get('name')
    name_tag = (
        f'deploy_targets[{index}] (name={name!r})'
        if name else f'deploy_targets[{index}]'
    )

    for field in REQUIRED_FIELDS:
        if field not in entry:
            reasons.append(f'{name_tag}: missing required field {field!r}')
    if reasons:
        return reasons  # Stop early; further checks depend on field presence.

    if not isinstance(name, str) or not _NAME_RE.match(name):
        reasons.append(
            f'{name_tag}: name must be kebab-case ([a-z0-9-]+), got {name!r}'
        )
        return reasons
    if name in seen_names:
        reasons.append(f'{name_tag}: duplicate name (already seen)')
    else:
        seen_names.add(name)

    github_repo = entry.get('github_repo')
    if not isinstance(github_repo, str) or not _GITHUB_REPO_RE.match(github_repo):
        reasons.append(
            f'{name_tag}: github_repo={github_repo!r} must match '
            f'owner/repo format'
        )

    project_id = entry.get('vercel_project_id')
    if not isinstance(project_id, str) or not _VERCEL_PROJECT_ID_RE.match(project_id):
        reasons.append(
            f'{name_tag}: vercel_project_id={project_id!r} must match '
            f'^prj_[A-Za-z0-9]+$'
        )

    org_id = entry.get('vercel_org_id')
    if org_id is not None and (
        not isinstance(org_id, str) or not _VERCEL_ORG_ID_RE.match(org_id)
    ):
        reasons.append(
            f'{name_tag}: vercel_org_id={org_id!r} must be null or match '
            f'^team_[A-Za-z0-9]+$'
        )

    framework = entry.get('framework')
    if framework not in known_frameworks:
        reasons.append(
            f'{name_tag}: framework={framework!r} not in '
            f'{list(known_frameworks)}'
        )

    env_var_keys = entry.get('env_var_keys')
    if (
        not isinstance(env_var_keys, list)
        or not all(isinstance(k, str) and k for k in env_var_keys)
    ):
        reasons.append(
            f'{name_tag}: env_var_keys must be a list of non-empty strings, '
            f'got {env_var_keys!r}'
        )

    branch_filter = entry.get('branch_filter')
    if branch_filter is not None and (
        not isinstance(branch_filter, str) or not branch_filter
    ):
        reasons.append(
            f'{name_tag}: branch_filter must be null or a non-empty string, '
            f'got {branch_filter!r}'
        )

    for bool_field in ('preview_enabled', 'production_enabled'):
        value = entry.get(bool_field)
        if not isinstance(value, bool):
            reasons.append(
                f'{name_tag}: {bool_field} must be a bool, got {value!r}'
            )

    created_at = entry.get('created_at')
    if _parse_iso_date(created_at) is None:
        reasons.append(
            f'{name_tag}: created_at={created_at!r} must be YYYY-MM-DD'
        )

    notes = entry.get('notes')
    if not isinstance(notes, str):
        reasons.append(
            f'{name_tag}: notes must be a string (may be empty), '
            f'got {type(notes).__name__}'
        )

    return reasons


def validate_registry(
    source: Union[str, Path, dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Validate a registry payload.

    `source` is either a path-like (str or Path) or an already-parsed dict.
    Returns (ok, reasons) — `ok` is True iff `reasons` is empty.
    Never raises for normal failures; reasons describe what's wrong.

    A bad source path or unparseable JSON yields ok=False with one reason.
    """
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.is_file():
            return False, [f'registry path does not exist: {path}']
        try:
            raw = path.read_text(encoding='utf-8')
        except OSError as e:
            return False, [f'read error on {path}: {e}']
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            return False, [f'JSON parse error on {path}: {e}']
    else:
        payload = source

    if not isinstance(payload, dict):
        return False, [
            f'registry must be a JSON object at the top level, '
            f'got {type(payload).__name__}'
        ]

    reasons: list[str] = []

    schema_version = payload.get('$schema_version')
    if schema_version != SCHEMA_VERSION:
        reasons.append(
            f'$schema_version mismatch: expected {SCHEMA_VERSION}, '
            f'got {schema_version!r}'
        )

    targets = payload.get('deploy_targets')
    if not isinstance(targets, list):
        reasons.append(
            f'deploy_targets must be a list, got {type(targets).__name__}'
        )
        return False, reasons

    known_frameworks_raw = payload.get('known_frameworks')
    if (
        not isinstance(known_frameworks_raw, list)
        or len(known_frameworks_raw) == 0
        or not all(isinstance(f, str) and f for f in known_frameworks_raw)
    ):
        reasons.append(
            f'known_frameworks must be a non-empty list of strings, '
            f'got {known_frameworks_raw!r}'
        )
        known_frameworks: tuple[str, ...] = ()
    else:
        known_frameworks = tuple(known_frameworks_raw)

    seen_names: set[str] = set()
    for i, entry in enumerate(targets):
        reasons.extend(
            _validate_entry(entry, i, seen_names, known_frameworks)
        )

    return (not reasons), reasons


def _format_reasons(reasons: Iterable[str]) -> str:
    return '\n'.join(f'  - {r}' for r in reasons)


def main(argv: list[str]) -> int:
    default_path = _repo_root() / 'config' / 'deploy_targets.json'
    path = Path(argv[1]) if len(argv) > 1 else default_path
    ok, reasons = validate_registry(path)
    if ok:
        print(f'OK: {path} validates against schema_version={SCHEMA_VERSION}')
        return 0
    sys.stderr.write(f'INVALID: {path}\n{_format_reasons(reasons)}\n')
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
