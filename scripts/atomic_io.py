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
import tempfile
from pathlib import Path
from typing import Any, Union

__all__ = [
    'atomic_write_bytes',
    'atomic_write_text',
    'atomic_write_json',
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
