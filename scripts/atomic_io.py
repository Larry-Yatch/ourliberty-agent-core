"""scripts/atomic_io.py — durable atomic file writes (shared helper).

PR-E (2026-06-05 full-codebase audit; findings #7, #54, #58, #62).

Several writers persisted shared state with a plain ``path.write_text(...)`` or a
*fixed*-name ``<path>.tmp`` + ``os.replace``. Both are unsafe:

  * a non-atomic ``write_text`` to a file that *also* serves as an idempotency
    sentinel leaves a truncated file on a mid-write crash (OOM/SIGTERM) that
    still satisfies ``path.exists()`` — permanently suppressing the producer
    until a manual ``--force`` (audit #7, ``pulse_check_viii``);
  * a *fixed* tmp name (``<path>.tmp``) shared by two concurrent writers lets one
    truncate the other's half-written tmp before its ``os.replace`` — yielding a
    corrupt file or a spurious ``FileNotFoundError`` surfaced as a failed
    mutation (audit #62, ``sequence_shortcut_helpers``).

``atomic_write_*`` writes to a *unique* temp file in the destination directory
(via :func:`tempfile.mkstemp`, the same convention ``build_sequence_advancer``
already uses), flushes + ``fsync``s it, then ``os.replace``s it into place
(atomic on the same filesystem). A unique tmp name means concurrent writers
never share a tmp; the ``fsync`` means the bytes are durable on disk before the
rename publishes them, so a crash leaves *either* the intact old file or the
intact new file — never a truncated one.

On any error the partial tmp is unlinked and the original exception re-raised, so
the destination is never left pointing at a half-written temp.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Union

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import file_lock  # noqa: E402  stdlib-only advisory-lock helper

__all__ = [
    'atomic_write_bytes',
    'atomic_write_text',
    'atomic_write_json',
    'locked_update',
]


def atomic_write_bytes(
    path: Union[str, Path], data: bytes, *, mode: int = 0o644,
) -> Path:
    """Atomically write ``data`` to ``path``.

    Creates the parent directory if missing, writes to a unique temp file in the
    same directory, fsyncs it, then ``os.replace``s it onto ``path``. Returns the
    final path. Re-raises on failure after removing the temp file.
    """
    # test-jail Layer B (state channel): fail LOUD if a test process is about
    # to write LIVE production state under the real agents tree because its
    # sandbox root-redirect failed open. Destination-aware, so a correctly
    # redirected sandbox write (and every production write) passes through.
    from test_isolation_guard import refuse_live_state_write
    path = Path(path)
    refuse_live_state_write(path, 'atomic-io-write')
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    # Unique tmp in the destination dir so os.replace is a same-filesystem rename
    # and concurrent writers never collide on a shared tmp name.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(directory),
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp creates 0o600; normalize to the caller's mode (default 0o644,
        # matching what open('w')/write_text produced under a 022 umask).
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def atomic_write_text(
    path: Union[str, Path], text: str, *,
    encoding: str = 'utf-8', mode: int = 0o644,
) -> Path:
    """Atomically write ``text`` to ``path`` (see :func:`atomic_write_bytes`)."""
    return atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_json(
    path: Union[str, Path], obj: Any, *,
    indent: int = 2, sort_keys: bool = False,
    trailing_newline: bool = False, mode: int = 0o644,
) -> Path:
    """Atomically write ``obj`` as JSON to ``path``.

    ``trailing_newline`` appends a final ``\\n`` to match writers that did so.
    """
    text = json.dumps(obj, indent=indent, sort_keys=sort_keys)
    if trailing_newline:
        text += '\n'
    return atomic_write_text(path, text, mode=mode)


# Default bound on how long a read-modify-write waits for the lock before it
# gives up and runs UNSERIALIZED. A ledger RMW (load a KB of JSON, mutate a
# dict, atomic-write) completes in sub-millisecond-to-low-ms, so genuine
# contention clears near-instantly; a wait this long only elapses if a peer is
# wedged holding the lock — exactly when degrading beats blocking a daemon.
_DEFAULT_LOCK_TIMEOUT = 5.0


@contextmanager
def locked_update(
    path: Union[str, Path], *, timeout: Union[float, None] = _DEFAULT_LOCK_TIMEOUT,
) -> Iterator[bool]:
    """Serialize a read-modify-write on ``path`` across processes.

    ``atomic_write_json`` makes the file *swap* atomic (a reader never sees a
    torn file), but it does NOT serialize the surrounding load→mutate→write: two
    processes can both read the old state, each mutate its own copy, and the
    second write clobbers the first — a lost update. Wrap the whole critical
    section in this context manager so only one process holds the file at a time::

        with atomic_io.locked_update(LEDGER_FILE):
            state = _load()
            state[key] = ...          # mutate
            _save(state)              # atomic_write_json

    The lock is file_lock's advisory sidecar lock (``<path>.lock``), the repo's
    established convention. It yields ``True`` when the lock was actually held
    and ``False`` when it degraded to unserialized (see below) — callers may
    ignore the value; it exists for tests and observability. (Note the inverted
    sense vs ``decision_outcome_reconcile._reconcile_lock``, whose ``False``
    means "another pass owns this tick, skip"; this helper's ``False`` means
    "ran anyway, unserialized" — do not conflate the two contracts.)

    Fail-open, NEVER raises and NEVER wedges — this is the key contract every
    ledger's "never raises" promise now depends on. The body still runs
    UNSERIALIZED (yielding ``False``), degrading to the exact pre-lock behavior,
    whenever the lock can't be taken:
      * advisory locking is unavailable on the platform (no ``fcntl``);
      * it can't be acquired within ``timeout`` (a peer is wedged); or
      * the lock dir/file is transiently unwritable — ``mkdir``/``os.open`` on
        the sidecar raises ``OSError`` (EROFS under ProtectHome, ENOSPC, EMFILE).
    That last case is why acquisition is guarded here rather than in the caller:
    the ``@_serialized`` decorator in suite_guardian takes this lock OUTSIDE each
    mutator's own ``try/except OSError``, so an un-swallowed acquisition error
    would escape and abort the whole guardian cycle. ``timeout=None`` blocks
    until acquired (use only where a stuck lock is impossible).

    Accepted tradeoff: on a genuinely wedged peer, a caller looping over N
    mutations pays up to ``timeout`` per call before degrading, and the degraded
    write can lost-update a peer that DID hold the lock. Both are bounded and
    rare — a flock is auto-released by the kernel when its holder dies, and the
    critical section is sub-millisecond, so a live holder stuck past ``timeout``
    is pathological. Liveness (never wedge a daemon) is deliberately favored over
    preventing a rare, self-healing lost update.

    CORRECTNESS CAVEAT (from file_lock): an advisory lock only coordinates
    processes that BOTH take it. The guarantee holds only if EVERY writer of
    ``path`` goes through this helper — so all writers of a given file must be
    migrated together.
    """
    lock_path = file_lock.sidecar_lock_path(path)
    # Same test-jail Layer-B guard atomic_write_bytes applies to the data write:
    # refuse (loudly) to create the .lock sidecar in the LIVE agents tree during
    # a test whose sandbox redirect failed open. No-op in production and for a
    # correctly-redirected sandbox path. Imported lazily to match atomic_write_*.
    from test_isolation_guard import refuse_live_state_write
    refuse_live_state_write(lock_path, 'locked-update-sidecar')

    # Acquire in two explicit phases so an acquisition failure (LockTimeout or a
    # transient OSError from the sidecar mkdir/open) degrades to an unserialized
    # run, while an exception from the BODY still propagates normally (it is not
    # an acquisition error, so it is never mistaken for one — and the body yields
    # exactly once regardless of which path we take).
    cm = None
    if file_lock.have_flock():
        try:
            cm = file_lock.exclusive_lock(lock_path, timeout=timeout)
            cm.__enter__()
        except (file_lock.LockTimeout, OSError):
            cm = None  # degrade: run the body unserialized (see docstring)
    try:
        yield cm is not None
    finally:
        if cm is not None:
            cm.__exit__(None, None, None)
