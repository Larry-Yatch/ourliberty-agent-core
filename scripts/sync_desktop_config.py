#!/usr/bin/env python3
"""sync_desktop_config.py — keep ~/.config/ourliberty in step with origin/main.

WHY: a handful of repo scripts are *executed from* ~/.config/ourliberty rather
than from the checkout — the Claude Code session hooks and the capture gesture
(`emit_capture.sh`). Those deployed copies were hand-placed with `cp` and
nothing ever refreshed them, so merging a fix to `scripts/` did not change what
actually ran. That is not hypothetical: when this script was written, the
deployed `emit_capture_impl.py` was 161 lines behind main (a pre-refactor
build with no importable `emit_capture()` and no env-var token fallback), and
the deployed `emit_capture.sh` had sat unchanged since June while its repo
source gained a usage guard.

Content comes from **origin/main**, not the working tree: "deployed" means
"what is merged", so a dirty checkout or a feature branch can never leak out.
A best-effort `git fetch` runs first; if it fails (offline), the last known
origin/main is still used, and that is reported.

Only paths listed in config/desktop-config-sync.json are ever written.
~/.config/ourliberty also holds hand-authored local-only tools and the ingest
token, and this must never clobber them.

Failures are LOUD. A silent no-op is the exact defect this exists to prevent,
so an unwritable destination or a missing source exits non-zero and says so.

Usage:
    sync_desktop_config.py              # apply: copy anything that drifted
    sync_desktop_config.py --check      # report only; exit 1 if drifted
    sync_desktop_config.py --verbose    # also name files already in sync

Env:
    OL_SYNC_DEST_DIR      override the destination (tests; default from config)
    OL_SYNC_SKIP_FETCH=1  skip the pre-sync `git fetch`
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _REPO_ROOT / 'config' / 'desktop-config-sync.json'
_REF = 'origin/main'
_FETCH_TIMEOUT = 8


def _err(msg: str) -> None:
    print(f'[sync-desktop-config] {msg}', file=sys.stderr)


def _out(msg: str) -> None:
    print(f'[sync-desktop-config] {msg}')


def load_manifest(config_path: Path = _CONFIG) -> tuple[Path, list[str]]:
    """(dest_dir, sources) from the canonical JSON. Raises on a broken file —
    a manifest we cannot read must not degrade into "nothing to sync"."""
    data = json.loads(config_path.read_text(encoding='utf-8'))
    dest = Path(os.environ.get('OL_SYNC_DEST_DIR')
                or os.path.expanduser(data['dest_dir']))
    sources = list(data['sources'])
    if not sources:
        raise ValueError(f'{config_path} lists no sources')
    return dest, sources


def _fetch() -> bool:
    """Best-effort refresh of the origin/main ref. False if it did not run."""
    if os.environ.get('OL_SYNC_SKIP_FETCH') == '1':
        return False
    try:
        r = subprocess.run(
            ['git', '-C', str(_REPO_ROOT), 'fetch', '--quiet', 'origin', 'main'],
            capture_output=True, text=True, timeout=_FETCH_TIMEOUT,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001 — offline/slow is survivable, not fatal
        return False


def _blob(source: str) -> tuple[bytes, bool]:
    """(content, is_executable) for `source` at origin/main. Raises if absent."""
    show = subprocess.run(
        ['git', '-C', str(_REPO_ROOT), 'show', f'{_REF}:{source}'],
        capture_output=True, timeout=30,
    )
    if show.returncode != 0:
        raise FileNotFoundError(
            f'{source} not found at {_REF}: '
            f'{show.stderr.decode("utf-8", "replace").strip()}')
    ls = subprocess.run(
        ['git', '-C', str(_REPO_ROOT), 'ls-tree', _REF, source],
        capture_output=True, text=True, timeout=30,
    )
    # "100755 blob <sha>\t<path>" — the mode is what makes a hook runnable.
    mode = ls.stdout.split(' ', 1)[0] if ls.stdout else ''
    return show.stdout, mode == '100755'


def sync(dest_dir: Path, sources: list[str], *, check: bool = False,
         verbose: bool = False) -> int:
    """Copy every drifted source into dest_dir. Returns a process exit code.

    check=True never writes; it exits 1 when anything is stale so a hook or
    test can assert "the deployed copies match main"."""
    changed: list[str] = []
    failed: list[str] = []

    for source in sources:
        name = Path(source).name
        target = dest_dir / name
        try:
            content, executable = _blob(source)
        except Exception as exc:  # noqa: BLE001 — report ALL, then fail once
            _err(f'{source}: {exc}')
            failed.append(source)
            continue

        try:
            current = target.read_bytes() if target.exists() else None
        except OSError as exc:
            _err(f'{target}: unreadable ({exc})')
            failed.append(source)
            continue

        if current == content:
            if verbose:
                _out(f'{name}: in sync')
            continue

        changed.append(name)
        if check:
            state = 'MISSING' if current is None else 'STALE'
            _out(f'{name}: {state} (deployed copy differs from {_REF})')
            continue

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            # Write via a temp file in the same dir, then rename: a hook firing
            # mid-sync sees either the old file or the new one, never a partial.
            tmp = target.with_name(f'.{name}.sync-tmp')
            tmp.write_bytes(content)
            os.chmod(tmp, 0o755 if executable else 0o644)
            os.replace(tmp, target)
        except OSError as exc:
            _err(f'{target}: write failed ({exc})')
            failed.append(source)
            continue
        _out(f'{name}: {"installed" if current is None else "updated"} '
             f'from {_REF}')

    if failed:
        _err(f'{len(failed)} file(s) could not be synced: {", ".join(failed)}')
        return 2
    if check and changed:
        _err(f'{len(changed)} deployed file(s) drifted from {_REF}: '
             f'{", ".join(changed)} — run scripts/sync_desktop_config.py')
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report drift without writing; exit 1 if drifted')
    ap.add_argument('--verbose', '-v', action='store_true',
                    help='also name files that are already in sync')
    args = ap.parse_args(argv)

    try:
        dest_dir, sources = load_manifest()
    except Exception as exc:  # noqa: BLE001 — a broken manifest is not "clean"
        _err(f'cannot read {_CONFIG}: {exc}')
        return 2

    if not _fetch() and args.verbose:
        _out(f'fetch skipped/failed — comparing against the last known {_REF}')

    return sync(dest_dir, sources, check=args.check, verbose=args.verbose)


if __name__ == '__main__':
    sys.exit(main())
