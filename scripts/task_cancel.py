#!/usr/bin/env python3
"""task_cancel.py — single source of truth for the in-flight task CANCEL
marker: `<agents_root>/blackboard/cancel-task-<task_stem>.json`.

WHY THIS MODULE EXISTS
----------------------
`agent_runner.run_claude` polls for this marker every 5s while a worker runs
and, on finding one, SIGTERMs (then SIGKILLs) the worker and returns
`TASK_CANCELLED`. `dispatch_sentinel`'s docstring calls it "the explicit
human-in-loop kill switch".

It had no WRITER. Nothing in the tree ever created one — not the dashboard, not
the build-sequence cancel endpoint, not any script. The only way to stop a
running build was to hand-write the file over ssh. So `cancel` on a build
sequence marked the sequence failed and blocked its auto-merge (#606) while the
Claude session it dispatched kept running to its 4h ceiling, burning tokens and
still free to push commits to the branch.

This module owns the marker's path shape and payload so the reader
(`agent_runner`) and the writers can never drift — the same rationale as the
sibling `marker_paths.py` for watchdog/Medic restart markers. That module stays
separate on purpose: it covers `state/` restart-coordination markers, which are
a different lifecycle with no payload.

`agents_root` is a parameter rather than a module constant for the same reason
`marker_paths` takes one: `agent_runner` hardcodes its AGENTS_ROOT while
`sequence_shortcut_helpers` honours an `OURLIBERTY_AGENTS_ROOT` override that
its test isolation depends on. Each caller stays authoritative over its own
root and they share only the SHAPE.

Stdlib + the shared `atomic_io` writer only — no agent/dashboard imports, so
both `agent_runner` (dispatch hot path) and `dashboard_api` (via
`sequence_shortcut_helpers`) can import it without dragging the other in.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent))

from atomic_io import atomic_write_json  # noqa: E402

_BLACKBOARD_SUBDIR = 'blackboard'
_MARKER_PREFIX = 'cancel-task-'


def cancel_marker_path(agents_root: Path, task_stem: str) -> Path:
    """The marker path for `task_stem`. Pure — no I/O, never raises.

    `task_stem` is used verbatim: at both dispatch sites `inbox_watcher` passes
    `task_stem=task_id`, and `agent_runner._check_cancel` interpolates the same
    raw value. Sanitizing here would look up a different filename than the
    reader polls for, so the cancel would silently never fire. Callers that
    accept untrusted ids must reject path separators BEFORE calling — see
    `is_safe_task_stem`.
    """
    return Path(agents_root) / _BLACKBOARD_SUBDIR / f'{_MARKER_PREFIX}{task_stem}.json'


def is_safe_task_stem(task_stem: Any) -> bool:
    """True if `task_stem` is safe to interpolate into the marker filename.

    The reader uses the raw stem, so the writer cannot sanitize — it must
    REFUSE instead. Rejects non-strings, empties, anything containing a path
    separator or a parent-dir hop, and leading dots (a `..`-style escape or a
    hidden file). Without this a crafted step_id like `../../state/foo` would
    write outside the blackboard.
    """
    if not isinstance(task_stem, str) or not task_stem:
        return False
    if task_stem.startswith('.'):
        return False
    return not any(c in task_stem for c in ('/', '\\', os.sep, os.altsep or '/'))


def is_cancel_requested(agents_root: Path, task_stem: str) -> Optional[str]:
    """The cancel reason if a marker exists for `task_stem`, else None.

    Mirrors `agent_runner._check_cancel`'s tolerance exactly: a present but
    unreadable/malformed marker still counts as a cancel (fail-TOWARD-stopping
    — a marker on disk means a human asked for this to stop, and a JSON typo
    must not keep the worker alive).
    """
    path = cancel_marker_path(agents_root, task_stem)
    if not path.exists():
        return None
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        return data.get('reason') or 'cancelled by request'
    except (OSError, json.JSONDecodeError, AttributeError):
        return 'cancelled (marker found)'


def request_cancel(
    agents_root: Path,
    task_stem: str,
    *,
    reason: Optional[str] = None,
    actor: str = 'larry',
    now: Optional[datetime] = None,
) -> bool:
    """Ask the worker running `task_stem` to stop. True if a marker is on disk
    afterwards, False if we refused or could not write one.

    Atomic (unique tmp + `os.replace`) so `agent_runner`'s 5s poll can never
    read a half-written marker. Idempotent by construction: re-requesting
    overwrites with the newer reason rather than erroring.

    FAIL-QUIET on write errors. The caller — a sequence cancel — has already
    marked the sequence failed and blocked its auto-merge, which is the
    guarantee that matters ("once aborted, nothing from that build lands on
    main"). A marker that could not be written costs tokens, not correctness,
    and must not turn the whole cancel into a 500 that invites a re-apply.
    """
    if not is_safe_task_stem(task_stem):
        return False
    path = cancel_marker_path(agents_root, task_stem)
    payload: dict[str, Any] = {
        'reason': reason or 'cancelled by request',
        'actor': actor,
        'requested_at': (now or datetime.now(timezone.utc)).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, payload, indent=2, trailing_newline=True)
        return True
    except OSError:
        return False


def clear_cancel(agents_root: Path, task_stem: str) -> None:
    """Remove the marker. Never raises — matches `agent_runner._clear_cancel`,
    which swallows failures so a cleanup error can't mask the cancel itself."""
    try:
        cancel_marker_path(agents_root, task_stem).unlink(missing_ok=True)
    except OSError:
        pass
