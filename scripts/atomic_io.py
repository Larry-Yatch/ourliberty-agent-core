#!/usr/bin/env python3
"""atomic_io.py — shared atomic-write and advisory-lock primitives.

Audit PR-E (2026-06-05) found the same two hazards open-coded in many
daemons:

  1. Non-atomic / collision-prone writes. Several modules write state with
     ``path.write_text(...)`` (no tmp+replace at all) or with a *fixed* tmp
     name (``path + '.tmp'``). The former leaves a truncated file on a crash
     — and where that file doubles as an idempotency sentinel, the truncated
     remnant permanently suppresses the next run. The latter lets two
     concurrent writers clobber each other's tmp mid-write, so the loser's
     ``os.replace`` moves a partial/absent file into place.

  2. Lock-free read-modify-write on JSON shared between processes. Two
     daemons each do load()->mutate()->save() with no serialization, so the
     last writer silently drops the other's mutation (a lost update).

This module centralizes the safe forms:

  * ``atomic_write_text`` / ``atomic_write_json`` — write to a *unique* temp
    file in the destination directory, ``flush`` + ``fsync``, then
    ``os.replace`` onto the final path. The unique temp name (``mkstemp``)
    means concurrent writers never share a scratch file; ``os.replace`` is an
    atomic rename on the same filesystem, so a reader sees either the old
    file or the fully-written new one, never a partial. On any error the temp
    file is removed and the original is left untouched.

  * ``file_lock`` — an advisory ``flock`` context manager for guarding a
    read-modify-write section across processes. It is **best-effort by
    design**: if the lock cannot be acquired (unwritable lock dir, timeout,
    platform without ``flock``) it logs and proceeds *unlocked* rather than
    raising, so a lock-system failure can never block a safety-critical write
    (e.g. a healer appending a CRITICAL alert). It narrows the race window to
    near-zero under normal operation without ever introducing a new hard
    failure mode.

Both helpers are intentionally dependency-free and import-safe (no top-level
side effects).
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Optional, Union

try:  # POSIX advisory locks; absent on Windows.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

PathLike = Union[str, "os.PathLike[str]"]


def _log(message: str, level: str = "INFO") -> None:
    """Best-effort stderr log; never raises (logging must not break I/O)."""
    try:
        print(f"[{level}] atomic_io: {message}", file=sys.stderr, flush=True)
    except Exception:
        pass


def atomic_write_bytes(path: PathLike, data: bytes, *, fsync: bool = True) -> None:
    """Atomically write ``data`` to ``path``.

    Writes to a unique temp file in the same directory (so the final
    ``os.replace`` is a same-filesystem atomic rename), optionally ``fsync``s
    it for durability, then renames it onto ``path``. On any error the temp
    file is removed and ``path`` is left untouched, so a crash or failure can
    never leave a truncated file where a complete one (or nothing) was
    expected. The unique temp name means two concurrent writers cannot clobber
    each other's scratch file.
    """
    dest = Path(path)
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    # mkstemp gives a unique name + O_EXCL create in the destination dir.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".tmp", dir=str(parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp_name, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_text(
    path: PathLike, text: str, *, encoding: str = "utf-8", fsync: bool = True
) -> None:
    """Atomically write ``text`` (see :func:`atomic_write_bytes`)."""
    atomic_write_bytes(path, text.encode(encoding), fsync=fsync)


def atomic_write_json(
    path: PathLike,
    obj: Any,
    *,
    indent: int = 2,
    sort_keys: bool = False,
    trailing_newline: bool = True,
    fsync: bool = True,
) -> None:
    """Serialize ``obj`` to JSON and atomically write it to ``path``."""
    text = json.dumps(obj, indent=indent, sort_keys=sort_keys, ensure_ascii=False)
    if trailing_newline:
        text += "\n"
    atomic_write_text(path, text, fsync=fsync)


@contextlib.contextmanager
def file_lock(
    lock_path: PathLike,
    *,
    exclusive: bool = True,
    timeout: float = 10.0,
    poll: float = 0.05,
) -> Iterator[bool]:
    """Advisory ``flock`` guarding a read-modify-write section.

    Yields ``True`` if the lock was actually held, ``False`` if it could not
    be acquired (and the caller is proceeding unlocked). This is **best-effort
    by design**: a lock-subsystem failure must never block a
    safety-critical write, so every failure path degrades to "proceed
    unlocked" with a WARN rather than raising. Under normal operation the lock
    is held and the protected section is serialized across processes.

    Usage::

        with file_lock(state_path.with_suffix('.lock')):
            state = load(state_path)
            mutate(state)
            atomic_write_json(state_path, state)
    """
    if fcntl is None:
        _log(f"flock unavailable on this platform; {lock_path} unlocked", "WARN")
        yield False
        return

    lp = Path(lock_path)
    fd: Optional[int] = None
    held = False
    try:
        try:
            lp.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as e:
            _log(f"cannot open lock file {lp} ({e}); proceeding unlocked", "WARN")
            yield False
            return

        mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, mode)
                held = True
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    _log(f"flock error on {lp} ({e}); proceeding unlocked", "WARN")
                    break
                if time.monotonic() >= deadline:
                    _log(
                        f"flock on {lp} not acquired within {timeout}s; "
                        f"proceeding unlocked",
                        "WARN",
                    )
                    break
                time.sleep(poll)
        yield held
    finally:
        if fd is not None:
            if held:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)
