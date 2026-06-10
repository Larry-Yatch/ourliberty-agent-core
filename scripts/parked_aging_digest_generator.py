#!/usr/bin/env python3
"""parked_aging_digest_generator.py — Missions v2 Phase 2 parked-&-aging digest.

Spec: agents/beacon/specs/missions-v2-phase2-resurfacing-and-derive.md § 6.

A small, structured producer modeled on scripts/ceo_digest_generator.py (timer
cadence + atomic write + fail-safe + heartbeat), but with NO LLM voice: this
digest is a plain structured artifact the dashboard renders as a read-only
"catch me up" card answering "what's parked & aging — promote / drop / snooze?"

It reads captures.json, selects `state == "parked"` captures already flagged
`aging: true`, and writes a digest artifact (parked count, aging count, the
aging items with title + origin repo + age) to a blackboard JSON file the
dashboard reads. Daily cadence via a systemd timer; the same entrypoint regen-
erates the artifact on demand (the artifact is overwritten, never appended).

REUSE THE AGING CLOCK — DO NOT REINVENT IT (spec § 6 enforcement):
The GC healer (scripts/heal_missions_card_gc.py, AGING_BUSINESS_DAYS = 5
business days) is the SINGLE definition of "aging" across the system. This
generator selects on the persisted `cap["aging"]` flag ONLY; it contains no
business-day math and does not recompute the aging decision. The per-item
`age_days` field is a display-only CALENDAR-day count derived from
`last_touched` for the card to render "N days ago" — it never gates selection.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))

# captures.json lives in the agent-core repo (same checkout as this script). The
# GC healer keeps it current on main; we read whatever is on disk here. An env
# override keeps the path test-controllable.
CAPTURES_REL = 'agents/beacon/captures.json'
DIGEST_ARTIFACT = AGENTS_ROOT / 'blackboard' / 'parked-aging-digest.json'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'parked-aging-digest-generator.heartbeat'

SCHEMA_VERSION = 1
VALID_TRIGGERS = ('scheduled', 'on-demand')


def resolve_log_dir() -> Path:
    """Log dir, honoring OURLIBERTY_LOG_DIR (test isolation; see conftest)."""
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    return Path(override) if override else AGENTS_ROOT / 'logs'


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        log_dir = resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / 'parked-aging-digest-generator.log', 'a') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


# -------------------- captures.json (read) --------------------


def resolve_captures_path() -> Path:
    override = os.environ.get('OURLIBERTY_CAPTURES_FILE')
    return Path(override) if override else _SCRIPT_DIR.parent / CAPTURES_REL


def read_captures_registry(path: Path) -> Optional[dict[str, Any]]:
    """Load captures.json as a registry dict. Missing file → fresh empty
    registry (a valid "nothing parked" state). Malformed/wrong-shape → None so
    the caller reports + skips rather than writing a bogus artifact. Mirrors
    heal_missions_card_gc.read_captures_registry's fail-safe contract."""
    if not path.exists():
        return {'schema_version': 1, 'captures': []}
    try:
        raw = path.read_text()
        data = json.loads(raw) if raw.strip() else {'schema_version': 1, 'captures': []}
    except (OSError, json.JSONDecodeError) as e:
        log(f'captures.json malformed/unreadable ({path}): {e} — skipping this run', 'WARN')
        return None
    if not isinstance(data, dict) or not isinstance(data.get('captures'), list):
        log(f'captures.json shape invalid ({path}) — skipping this run', 'WARN')
        return None
    return data


# -------------------- selection (pure; aging-flag-only) --------------------


def select_aging_parked(registry: dict[str, Any]) -> list[dict[str, Any]]:
    """The parked-&-aging set: captures with `state == "parked"` AND the GC
    healer's persisted `aging` flag set true. PURE and flag-only — no date math,
    no business-day computation, no aging recompute (spec § 6 enforcement)."""
    out: list[dict[str, Any]] = []
    for cap in registry.get('captures', []):
        if not isinstance(cap, dict):
            continue
        if cap.get('state') == 'parked' and cap.get('aging') is True:
            out.append(cap)
    return out


def count_parked(registry: dict[str, Any]) -> int:
    """Total parked captures (aging or not) — the denominator on the card."""
    return sum(
        1 for cap in registry.get('captures', [])
        if isinstance(cap, dict) and cap.get('state') == 'parked'
    )


# -------------------- rendering --------------------


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _origin_repo(cap: dict[str, Any]) -> Optional[str]:
    origin = cap.get('origin')
    if isinstance(origin, dict):
        repo = origin.get('repo')
        if isinstance(repo, str) and repo:
            return repo
    return None


def _age_days(cap: dict[str, Any], now: datetime) -> Optional[int]:
    """Calendar days since `last_touched`, for display only ("N days ago"). NOT
    the aging criterion — that is the GC healer's business-day flag, already
    decided. Returns None when last_touched is missing/unparseable."""
    touched = _parse_iso_utc(cap.get('last_touched'))
    if touched is None:
        return None
    return max(0, (now.date() - touched.date()).days)


def build_item(cap: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        'capture_id': cap.get('id'),
        'title': cap.get('title'),
        'repo': _origin_repo(cap),
        'age_days': _age_days(cap, now),
        'last_touched': cap.get('last_touched'),
    }


def build_digest(registry: dict[str, Any], now: datetime, trigger: str) -> dict[str, Any]:
    """Assemble the structured artifact: counts + the aging items (§ 6)."""
    aging = select_aging_parked(registry)
    items = [build_item(cap, now) for cap in aging]
    return {
        'schema_version': SCHEMA_VERSION,
        'generated_at': now.isoformat(),
        'trigger': trigger,
        'parked_count': count_parked(registry),
        'aging_count': len(aging),
        'items': items,
    }


# -------------------- write (atomic) --------------------


def atomic_write_digest(path: Path, digest: dict[str, Any]) -> None:
    """tmp-in-same-dir + os.replace so the dashboard never reads a partial
    file. Mirrors heal_missions_card_gc.atomic_write_captures."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(json.dumps(digest, indent=2) + '\n')
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# -------------------- run --------------------


def run(now: Optional[datetime] = None, *, trigger: str = 'scheduled',
        captures_path: Optional[Path] = None,
        artifact_path: Optional[Path] = None) -> int:
    """Generate and persist one parked-&-aging digest. Returns a process exit
    code (0 ok; 1 captures.json unreadable → nothing written this run)."""
    if trigger not in VALID_TRIGGERS:
        log(f'invalid trigger {trigger!r}; expected one of {VALID_TRIGGERS}', 'ERROR')
        return 2
    heartbeat()
    now = now or datetime.now(timezone.utc)
    cap_path = captures_path or resolve_captures_path()
    out_path = artifact_path or DIGEST_ARTIFACT

    registry = read_captures_registry(cap_path)
    if registry is None:
        log('captures.json unreadable; leaving prior artifact in place', 'WARN')
        return 1

    digest = build_digest(registry, now, trigger)
    try:
        atomic_write_digest(out_path, digest)
    except OSError as e:
        log(f'digest write failed ({out_path}): {type(e).__name__}: {e}', 'ERROR')
        return 1
    log(f'{trigger} digest written: parked={digest["parked_count"]} '
        f'aging={digest["aging_count"]} → {out_path}')
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Generate the Missions v2 parked-&-aging dashboard digest.')
    parser.add_argument(
        '--trigger', choices=VALID_TRIGGERS, default='scheduled',
        help="how this run was invoked (scheduled timer vs on-demand refresh); "
             "recorded in the artifact. Default: scheduled.")
    args = parser.parse_args(argv)
    return run(trigger=args.trigger)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
