#!/usr/bin/env python3
"""atomic_io.py — shared atomic-write / no-clobber filesystem primitives.

Audit PR-E (2026-06-05) found the same write hazards open-coded across many
daemons:

  1. Non-atomic / collision-prone writes. Several modules write state with
     ``path.write_text(...)`` (no tmp+replace at all) or with a *fixed* tmp
     name (``path + '.tmp'``). The former leaves a truncated file on a crash —
     and where that file doubles as an idempotency sentinel, the truncated
     remnant permanently suppresses the next run. The latter lets two
     concurrent writers clobber each other's tmp mid-write, so the loser's
     ``os.replace`` moves a partial/absent file into place.

  2. ``shutil.move`` into an archive directory silently overwrites a same-named
     prior file, destroying the earlier archived copy and defeating the
     "move (not delete) preserves the audit trail" guarantee.

This module centralizes the safe forms:

  * ``atomic_write_text`` / ``atomic_write_bytes`` / ``atomic_write_json`` —
    write to a *unique* temp file in the destination directory (``mkstemp``,
    so concurrent writers never share a scratch file), ``flush`` + ``fsync``
    for durability, then ``os.replace`` onto the final path. ``os.replace`` is
    an atomic rename on the same filesystem, so a reader sees either the old
    file or the fully-written new one, never a partial. On any error the temp
    file is removed and the original is left untouched. The created file's mode
    defaults to ``0o644`` (matching the umask-default the replaced
    ``write_text`` calls produced) rather than ``mkstemp``'s ``0o600``, so the
    permission of state/artifact files is not silently narrowed.

  * ``noclobber_dest`` — pick an archive destination that does not overwrite an
    existing file, appending a ``.N`` suffix before the extension.

The advisory ``flock`` helper for read-modify-write serialization lands in the
follow-up PR-E2 alongside its first production consumer.

Dependency-free and import-safe (no top-level side effects).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Union

PathLike = Union[str, "os.PathLike[str]"]

# Match the umask-default mode the replaced `write_text` calls produced, rather
# than mkstemp's 0o600, so wiring these helpers in does not silently narrow the
# permissions of non-secret state/artifact files.
DEFAULT_FILE_MODE = 0o644


def atomic_write_bytes(
    path: PathLike,
    data: bytes,
    *,
    fsync: bool = True,
    mode: int = DEFAULT_FILE_MODE,
) -> None:
    """Atomically write ``data`` to ``path``.

    Writes to a unique temp file in the same directory (so the final
    ``os.replace`` is a same-filesystem atomic rename), sets its mode, optionally
    ``fsync``s it for durability, then renames it onto ``path``. On any error the
    temp file is removed and ``path`` is left untouched, so a crash or failure
    can never leave a truncated file where a complete one (or nothing) was
    expected. The unique temp name means two concurrent writers cannot clobber
    each other's scratch file.
    """
    dest = Path(path)
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".tmp", dir=str(parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            # mkstemp creates 0o600; widen to the requested mode before the
            # file becomes visible under its final name.
            os.fchmod(f.fileno(), mode)
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp_name, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_text(
    path: PathLike,
    text: str,
    *,
    encoding: str = "utf-8",
    fsync: bool = True,
    mode: int = DEFAULT_FILE_MODE,
) -> None:
    """Atomically write ``text`` (see :func:`atomic_write_bytes`)."""
    atomic_write_bytes(path, text.encode(encoding), fsync=fsync, mode=mode)


def atomic_write_json(
    path: PathLike,
    obj: Any,
    *,
    indent: int = 2,
    sort_keys: bool = False,
    trailing_newline: bool = True,
    fsync: bool = True,
    mode: int = DEFAULT_FILE_MODE,
) -> None:
    """Serialize ``obj`` to JSON and atomically write it to ``path``."""
    text = json.dumps(obj, indent=indent, sort_keys=sort_keys, ensure_ascii=False)
    if trailing_newline:
        text += "\n"
    atomic_write_text(path, text, fsync=fsync, mode=mode)


def noclobber_dest(target: PathLike) -> Path:
    """Return a destination path that does not overwrite an existing file.

    Audit #54: archivers ``shutil.move`` a file into an archive dir using its
    original name; ``shutil.move`` silently overwrites a same-named destination,
    destroying the earlier archived copy. If ``target`` is free, return it
    unchanged; otherwise append a ``.N`` suffix before the extension (``foo.json``
    -> ``foo.1.json`` -> ``foo.2.json`` …) until a free name is found.

    Note: this is a point-in-time check, not an atomic reservation. It closes
    the across-time collision (the same name re-appearing on a later tick), which
    is the documented hazard for the single-instance oneshot healers that use it.
    """
    dest = Path(target)
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while True:
        candidate = dest.with_name(f"{stem}.{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1
