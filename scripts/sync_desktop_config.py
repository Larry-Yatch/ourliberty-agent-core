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
A `git fetch` runs first. If it FAILS, everything below is compared against a
possibly-stale ref, so the run says so on stderr and exits non-zero even when
nothing looked stale — reporting "clean" against a ref we could not refresh is
the same silent false-clean this script exists to prevent.

Only paths listed in config/desktop-config-sync.json are ever written.
~/.config/ourliberty also holds hand-authored local-only tools and the ingest
token, and this must never clobber them.

Failures are LOUD. A silent no-op is the exact defect this exists to prevent,
so an unwritable destination or a missing source exits non-zero and says so.

Usage:
    sync_desktop_config.py              # apply: copy anything that drifted
    sync_desktop_config.py --check      # report only; exit 1 if drifted
    sync_desktop_config.py --verbose    # also name files already in sync

Exit codes:
    0  in sync (and verified against a freshly fetched origin/main)
    1  --check only: something drifted
    2  a file could not be synced (missing source, unwritable destination,
       unreadable manifest)
    3  the fetch failed, so the comparison basis is unverified

Env:
    OL_SYNC_DEST_DIR      override the destination (tests; default from config)
    OL_SYNC_SKIP_FETCH=1  skip the pre-sync `git fetch` (an explicit opt-out;
                          unlike a FAILED fetch this does not force exit 3)
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
    # Destinations are keyed on the basename, so two sources sharing one would
    # silently collide: apply would rewrite the file on every run (last writer
    # wins) and still exit 0, while --check reported drift forever. Refuse the
    # manifest instead of deploying whichever entry happens to be last.
    seen: dict[str, str] = {}
    for src in sources:
        name = Path(src).name
        if name in seen:
            raise ValueError(
                f'{config_path}: {src} and {seen[name]} share the destination '
                f'basename {name!r} — they would overwrite each other')
        seen[name] = src
    return dest, sources


def _fetch() -> tuple[str, str]:
    """Refresh the origin/main ref. Returns (status, detail) where status is
    'ok', 'skipped' (caller opted out) or 'failed'.

    'failed' must NOT be conflated with 'skipped'. A failed fetch means every
    comparison below is against a possibly-stale ref, so the run cannot honestly
    claim the deployed copies match main — see main(). The likely trigger here
    is not being offline: the SessionStart hook runs against a checkout that
    other automation fetches and commits into concurrently, so a transient
    'cannot lock ref refs/remotes/origin/main' is expected."""
    if os.environ.get('OL_SYNC_SKIP_FETCH') == '1':
        return 'skipped', 'OL_SYNC_SKIP_FETCH=1'
    try:
        r = subprocess.run(
            ['git', '-C', str(_REPO_ROOT), 'fetch', '--quiet', 'origin', 'main'],
            capture_output=True, text=True, timeout=_FETCH_TIMEOUT,
        )
        if r.returncode == 0:
            return 'ok', ''
        # git is chatty on failure; this line lands in a SessionStart hook, so
        # collapse it to one readable line rather than a five-line wall.
        detail = ' '.join((r.stderr or r.stdout or '').split())
        return 'failed', (detail[:200] or f'exit {r.returncode}')
    except subprocess.TimeoutExpired:
        return 'failed', f'timed out after {_FETCH_TIMEOUT}s'
    except Exception as exc:  # noqa: BLE001 — report it, never swallow it
        return 'failed', str(exc)


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
    return show.stdout, is_executable_mode(source, ls.returncode, ls.stdout)


def is_executable_mode(source: str, returncode: int, stdout: str) -> bool:
    """Parse `git ls-tree` output into "should this land executable?".

    Raises rather than guessing. The deployed files are invoked as bare
    executables by the Claude Code hooks (settings.json runs the .sh directly,
    not `bash <file>`), so landing one 0644 breaks it with permission-denied on
    every session start. Silently defaulting to non-executable whenever this
    output is unexpected would make that the failure mode of every surprise."""
    if returncode != 0:
        raise ValueError(f'{source}: git ls-tree {_REF} failed (exit {returncode})')
    if not stdout.strip():
        raise ValueError(
            f'{source}: git ls-tree {_REF} returned no entry — `git show` and '
            '`git ls-tree` disagree about this path')
    # "100755 blob <sha>\t<path>"
    mode = stdout.split(' ', 1)[0]
    if mode not in ('100644', '100755'):
        # 120000 symlink, 160000 gitlink, 040000 tree — `git show` yields the
        # link target / nothing useful, so copying it as a regular file would
        # deploy something quietly wrong.
        raise ValueError(
            f'{source}: unsupported git mode {mode} at {_REF} — only regular '
            'files (100644/100755) can be deployed')
    return mode == '100755'


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

    status, detail = _fetch()
    if status == 'skipped' and args.verbose:
        _out(f'fetch skipped ({detail}) — comparing against the local {_REF}')

    rc = sync(dest_dir, sources, check=args.check, verbose=args.verbose)

    if status == 'failed':
        # Everything above compared against a ref we could not refresh, so a
        # clean result here means "matches a possibly-stale origin/main", not
        # "matches main". Reporting 0 in that state is the silent false-clean
        # this whole script exists to prevent — say so and exit non-zero even
        # when nothing looked stale.
        _err(f'git fetch failed ({detail}) — compared against a possibly-stale '
             f'{_REF}; the deployed copies are NOT verified against main')
        return rc or 3
    return rc


if __name__ == '__main__':
    sys.exit(main())
