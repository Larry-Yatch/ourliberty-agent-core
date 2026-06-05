"""scripts/file_lock.py — advisory cross-process file locking (shared helper).

PR-E2 (2026-06-05 full-codebase audit; findings #16, #48). Several shared-state
files are read-modify-rewritten by one process while ~20 other processes append
or mutate the same file with no coordination, so an interleaved write is silently
lost (a textbook lost-update). ``fcntl.flock`` is the existing convention in this
repo (``concurrency_guard.py``, ``dispatch_lease.py``); this module factors the
acquire/release dance into one reusable context manager so the producer side and
the consumer/rewriter side can take the *same* lock.

Design notes:

  * The lock is always taken on a DEDICATED sidecar file (``<target>.lock``),
    never on the data file itself. The retention rewrite ``os.replace``s the data
    file — which swaps its inode — so a lock held on the data file's old inode
    would not exclude a writer that opened the new inode. A stable sidecar that
    is never rewritten avoids that entirely.

  * ``exclusive_lock`` blocks indefinitely by default (``LOCK_EX``). Pass
    ``timeout`` to bound the wait: it polls with ``LOCK_EX | LOCK_NB`` and raises
    :class:`LockTimeout` if the lock can't be taken in time — for callers (e.g.
    fire-and-forget alert appenders) that must never hang a daemon thread.

  * Advisory locks only coordinate processes that BOTH take the lock. A writer
    that bypasses the lock is unaffected — which is why both sides of every
    coordinated RMW must be migrated together.
"""
from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

try:  # pragma: no cover - exercised implicitly on POSIX
    import fcntl
    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - non-POSIX (Windows); degrade to no-op
    fcntl = None  # type: ignore[assignment]
    _HAVE_FLOCK = False

__all__ = ['LockTimeout', 'sidecar_lock_path', 'exclusive_lock', 'have_flock']


class LockTimeout(TimeoutError):
    """Raised by :func:`exclusive_lock` when a bounded acquire times out."""


def have_flock() -> bool:
    """True if advisory flock is available on this platform."""
    return _HAVE_FLOCK


def sidecar_lock_path(target: Union[str, Path]) -> Path:
    """The canonical lock-file path for a data file: ``<dir>/<name>.lock``.

    Both the producer and the rewriter MUST derive their lock path from this so
    they contend on the same file. Suffix-appending (not ``with_suffix``) keeps
    the original extension visible (``larry-alerts.jsonl.lock``).
    """
    target = Path(target)
    return target.parent / (target.name + '.lock')


@contextmanager
def exclusive_lock(
    lock_path: Union[str, Path],
    *,
    timeout: Union[float, None] = None,
    poll_interval: float = 0.05,
) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``lock_path`` for the ``with`` body.

    ``timeout`` is None → block until acquired (``LOCK_EX``). ``timeout`` >= 0 →
    poll with ``LOCK_EX | LOCK_NB`` and raise :class:`LockTimeout` if it can't be
    taken within that many seconds. On a platform without ``fcntl`` the lock
    degrades to a no-op (the body still runs — there is no cross-process
    coordination, matching the pre-lock behaviour).

    The lock file is created if missing; it is never written to or removed (a
    leftover empty ``.lock`` file is harmless and avoids an unlink/recreate
    race).
    """
    if not _HAVE_FLOCK:
        yield
        return

    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockTimeout(
                            f'could not acquire {lock_path} within {timeout}s'
                        ) from e
                    time.sleep(poll_interval)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
