#!/usr/bin/env python3
"""validate_token_rotation_schedule.py — registry validator for
config/token-rotation-schedule.json (Phase E1.5.2).

The registry is the canonical record of every active credential. This
validator enforces the schema + semantic invariants so a malformed entry
can't ship: drift healer, Pulse cycle, and Mirror's PR review all assume
the registry is well-formed.

Library use:
    from validate_token_rotation_schedule import validate_registry
    ok, reasons = validate_registry(path_or_dict)

CLI use:
    python3 scripts/validate_token_rotation_schedule.py [PATH]
    # exit 0 = valid; exit 1 = invalid (reasons on stderr)

Stdlib only. The validator is read-only — it never mutates the registry.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Union

SCHEMA_VERSION = 1

ROTATION_TYPES = frozenset({
    'scheduled', 'scope_audit', 'revocation_only', 'auto_refresh',
})

SEVERITIES = frozenset({'critical', 'high', 'medium', 'low'})

REQUIRED_FIELDS = (
    'name', 'storage_location', 'credential_type', 'purpose',
    'rotation_type', 'cadence_days', 'created_at', 'last_rotated_at',
    'next_rotation_due', 'calendar_event_url', 'runbook_path',
    'severity_if_lapsed', 'owner_role', 'scopes', 'notes',
)

# Known storage_location prefixes — must match known_storage_locations keys.
KNOWN_PREFIXES = ('env_file:', 'gh_cli:', 'claude_cli:', 'workspace_mcp:')

CALENDAR_URL_PREFIX = 'https://www.google.com/calendar/'

# Leap-year tolerance: next_rotation_due may be ±1 day of last_rotated_at +
# cadence_days, since Feb-29 anniversaries shift to Mar-1 on non-leap years.
CADENCE_TOLERANCE_DAYS = 1


def _repo_root() -> Path:
    """Resolve repo root as the parent of scripts/. Defensive — works whether
    the validator is invoked from repo root or anywhere else."""
    return Path(__file__).resolve().parent.parent


def _parse_iso_date(s: Any) -> Optional[date]:
    if not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None


def _validate_entry(
    entry: Any,
    index: int,
    seen_names: set[str],
    known_locations: dict[str, Any],
    repo_root: Path,
) -> list[str]:
    """Return a list of reasons (empty = entry valid)."""
    reasons: list[str] = []
    if not isinstance(entry, dict):
        return [f'credentials[{index}]: must be an object, got {type(entry).__name__}']
    name = entry.get('name')
    name_tag = f'credentials[{index}] (name={name!r})' if name else f'credentials[{index}]'

    # Required fields presence (None values for nullable fields are OK; the
    # individual checks below validate type).
    for field in REQUIRED_FIELDS:
        if field not in entry:
            reasons.append(f'{name_tag}: missing required field {field!r}')
    if reasons:
        return reasons  # Stop early; further checks depend on field presence.

    if not isinstance(name, str) or not name:
        reasons.append(f'{name_tag}: name must be a non-empty string')
        return reasons
    if name in seen_names:
        reasons.append(f'{name_tag}: duplicate name (already seen)')
    else:
        seen_names.add(name)

    rotation_type = entry.get('rotation_type')
    if rotation_type not in ROTATION_TYPES:
        reasons.append(
            f'{name_tag}: rotation_type={rotation_type!r} not in '
            f'{sorted(ROTATION_TYPES)}'
        )

    severity = entry.get('severity_if_lapsed')
    if severity not in SEVERITIES:
        reasons.append(
            f'{name_tag}: severity_if_lapsed={severity!r} not in '
            f'{sorted(SEVERITIES)}'
        )

    storage_location = entry.get('storage_location')
    if not isinstance(storage_location, str) or not any(
        storage_location.startswith(p) for p in KNOWN_PREFIXES
    ):
        reasons.append(
            f'{name_tag}: storage_location={storage_location!r} must start '
            f'with one of {KNOWN_PREFIXES}'
        )
    elif known_locations:
        # storage_location may exactly match a known key, OR for
        # workspace_mcp's directory-based store, may share its prefix-dir.
        matched = (
            storage_location in known_locations
            or any(
                storage_location.startswith(k)
                for k in known_locations
                if k.startswith('workspace_mcp:')
            )
        )
        if not matched:
            reasons.append(
                f'{name_tag}: storage_location={storage_location!r} not in '
                f'known_storage_locations'
            )

    created = _parse_iso_date(entry.get('created_at'))
    if created is None:
        reasons.append(
            f'{name_tag}: created_at={entry.get("created_at")!r} '
            f'must be YYYY-MM-DD'
        )
    last_rot = _parse_iso_date(entry.get('last_rotated_at'))
    if last_rot is None:
        reasons.append(
            f'{name_tag}: last_rotated_at={entry.get("last_rotated_at")!r} '
            f'must be YYYY-MM-DD'
        )

    cadence_days = entry.get('cadence_days')
    next_due = entry.get('next_rotation_due')

    if rotation_type == 'revocation_only':
        if cadence_days is not None:
            reasons.append(
                f'{name_tag}: revocation_only requires cadence_days=null, '
                f'got {cadence_days!r}'
            )
        if next_due is not None:
            reasons.append(
                f'{name_tag}: revocation_only requires next_rotation_due=null, '
                f'got {next_due!r}'
            )
    elif rotation_type in {'scheduled', 'scope_audit', 'auto_refresh'}:
        if not isinstance(cadence_days, int) or isinstance(cadence_days, bool) or cadence_days <= 0:
            reasons.append(
                f'{name_tag}: rotation_type={rotation_type} requires '
                f'cadence_days as positive int, got {cadence_days!r}'
            )
        next_due_date = _parse_iso_date(next_due)
        if next_due_date is None:
            reasons.append(
                f'{name_tag}: rotation_type={rotation_type} requires '
                f'next_rotation_due as YYYY-MM-DD, got {next_due!r}'
            )
        elif (
            isinstance(cadence_days, int)
            and not isinstance(cadence_days, bool)
            and cadence_days > 0
            and last_rot is not None
        ):
            expected = last_rot + timedelta(days=cadence_days)
            delta = abs((next_due_date - expected).days)
            if delta > CADENCE_TOLERANCE_DAYS:
                reasons.append(
                    f'{name_tag}: next_rotation_due={next_due!r} not within '
                    f'±{CADENCE_TOLERANCE_DAYS}d of last_rotated_at + '
                    f'cadence_days (expected ~{expected.isoformat()})'
                )

    calendar_url = entry.get('calendar_event_url')
    if calendar_url is not None:
        if not isinstance(calendar_url, str) or not calendar_url.startswith(
            CALENDAR_URL_PREFIX
        ):
            reasons.append(
                f'{name_tag}: calendar_event_url must be null or start with '
                f'{CALENDAR_URL_PREFIX!r}, got {calendar_url!r}'
            )

    runbook = entry.get('runbook_path')
    if not isinstance(runbook, str) or not runbook:
        reasons.append(
            f'{name_tag}: runbook_path must be a non-empty string, '
            f'got {runbook!r}'
        )
    else:
        full = (repo_root / runbook).resolve()
        if not full.is_file():
            reasons.append(
                f'{name_tag}: runbook_path={runbook!r} does not resolve to '
                f'an existing file (looked for {full})'
            )

    scopes = entry.get('scopes')
    if (
        not isinstance(scopes, list)
        or len(scopes) == 0
        or not all(isinstance(s, str) and s for s in scopes)
    ):
        reasons.append(
            f'{name_tag}: scopes must be a non-empty list of strings, '
            f'got {scopes!r}'
        )

    return reasons


def validate_registry(
    source: Union[str, Path, dict[str, Any]],
    repo_root: Optional[Path] = None,
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

    creds = payload.get('credentials')
    if not isinstance(creds, list):
        reasons.append(
            f'credentials must be a list, got {type(creds).__name__}'
        )
        return False, reasons

    known_locations = payload.get('known_storage_locations')
    if not isinstance(known_locations, dict):
        reasons.append(
            f'known_storage_locations must be an object, '
            f'got {type(known_locations).__name__}'
        )
        known_locations = {}

    repo_root = repo_root or _repo_root()
    seen_names: set[str] = set()
    for i, entry in enumerate(creds):
        reasons.extend(
            _validate_entry(entry, i, seen_names, known_locations, repo_root)
        )

    return (not reasons), reasons


def _format_reasons(reasons: Iterable[str]) -> str:
    return '\n'.join(f'  - {r}' for r in reasons)


def main(argv: list[str]) -> int:
    default_path = _repo_root() / 'config' / 'token-rotation-schedule.json'
    path = Path(argv[1]) if len(argv) > 1 else default_path
    ok, reasons = validate_registry(path)
    if ok:
        print(f'OK: {path} validates against schema_version={SCHEMA_VERSION}')
        return 0
    sys.stderr.write(f'INVALID: {path}\n{_format_reasons(reasons)}\n')
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
