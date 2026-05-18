"""Infer a task_type label from a task_id when the dispatch envelope omits it.

Background
----------
Pre-week-1, dispatch envelopes routinely lacked `task_type` and Ledger's
`attribute_task_types` join (outbox archive lookup) found nothing either.
The result: 178 of ~235 rows in the 2026-05-18 sidecar landed in
`by_task_type["unknown"]` ($75/65% by count), distorting every cost
attribution downstream.

This helper recovers ~90%+ coverage by mapping known task_id prefixes to
task_type labels. Both `inbox_watcher.py` (cost-capture write) and
`ledger_weekly.py::attribute_task_types` (archive-join fallback) use it,
so the same label is produced regardless of which side fills it in.

Distinct from "unknown":
- `"unknown"` = the legacy default (the writer didn't even try to set it)
- `"unclassified"` = inference ran but the task_id has no known prefix.
  After this fix is live, "unknown" should not appear in new rows.
"""

from __future__ import annotations


def infer_task_type(task_id: str) -> str:
    """Map a task_id to a task_type label by prefix.

    Order matters: more specific patterns first.
    Returns "unclassified" if no rule matches (an empty / unrecognized id).
    """
    if not task_id:
        return "unclassified"

    # notify-* wraps every inter-agent workflow leg (forge-result -> Beacon,
    # mirror-result -> Beacon, beacon-result -> Pulse, etc.). Largest bucket.
    if task_id.startswith("notify-"):
        return "notification"

    # Retry shapes
    if task_id.startswith("marker-error-"):
        return "retry"
    if task_id.startswith("cycle-fix-"):
        return "retry"

    # Pulse autonomous /cycle commits (distinct from cycle-fix-* retries)
    if task_id.startswith("cycle-"):
        return "cycle"

    # Mirror review dispatches
    if task_id.startswith("review-"):
        return "review"

    # Forge build-phase dispatches
    if task_id.startswith("build-"):
        return "build"

    # Auto-merge / merge-recovery shapes
    if task_id.startswith("auto-merge-"):
        return "auto-merge"

    # Smoke tests
    if task_id.startswith("smoke-"):
        return "smoke-test"

    # Operating-manual updates (always doc-only by convention)
    if task_id.startswith("opmanual-"):
        return "doc-only"

    # Dead-letter cascade tasks
    if task_id.startswith("dead-letter-"):
        return "dead-letter"

    return "unclassified"
