#!/usr/bin/env python3
"""mirror_review_conclusion.py — the SINGLE predicate for "has this Mirror review
already run to a delivered verdict?", shared by the two cleanup paths so they
cannot drift.

Two consumers reason about a stranded ``inboxes/mirror/.claimed/<slot>/
review-*.json`` claim and must agree on whether its review already concluded:

  * inbox_watcher.sweep_claimed_orphans (startup backstop) — must ARCHIVE a
    concluded orphan, NOT re-queue it (re-queuing pays for an Opus re-review of
    an already-reviewed PR — the 2026-07-10 PR #854 class).
  * heal_orphaned_mirror_claims.py (10-min timer healer) — archives concluded
    orphans so the slot un-blocks without waiting for a watcher restart.

If those two carried their own copies of the archive-name derivation, a change
to outbox_notifier's archive scheme (or the sanitizer) would have to be edited
in lockstep in both; miss one and the two disagree — exactly the paid-re-review
they exist to prevent. So the logic lives here once; both import it.

Signals (any one ⇒ a verdict was delivered), cheapest/most-durable first:

  1. a consumed verdict outbox archived at ``outboxes/mirror/.archive/`` under
     the name outbox_notifier wrote it with — ``sanitize_component(f"{task_id}.
     json")`` (inbox_watcher.process_task) — or its collision-suffixed
     ``<stem>.<n>.json`` form (outbox_notifier._archive_outbox). A ``.forfeit``
     sibling (a reaped never-delivered run) is NOT a verdict and is excluded.
  2. a review request with the SAME filename already archived under
     ``inboxes/mirror/.archive/`` — the re-dispatch-of-a-concluded-review case.
  3. outbox-notifier.log recorded a ``marker-notified ... <- mirror`` delivery
     for the task_id. Log-rotation-fragile, so this is the SUPPLEMENTARY signal
     behind the two durable filesystem checks.

All roots are passed in by the caller (dependency injection) so each consumer's
own — test-patchable — path constants are honored and there is no hidden global
coupling. Pure filesystem reads + one bounded log grep; no network, no LLM.
Every string comparison is glob-metachar-safe (no ``glob`` on a raw task_id) and
boundary-delimited (no substring-ID match — the anti-pattern the codebase fixed
via ``id_match``).
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import safe_write_inbox  # noqa: E402  (hashlib/json/os/re/sys/tempfile — no network)

# Bound the notifier-log grep so a huge log never stalls the caller.
NOTIFIER_LOG_MAX_BYTES = 8 * 1024 * 1024

_JSON_SUFFIX = ".json"


def archived_outbox_name(task_id: str) -> str:
    """The exact filename a delivered Mirror verdict outbox is archived under:
    ``sanitize_component(f"{task_id}.json")`` — the SAME derivation
    inbox_watcher.process_task uses to WRITE the outbox (so for an ordinary
    ``[A-Za-z0-9._-]`` task_id this is just ``<task_id>.json``, and for a
    task_id carrying a path-structural byte it is the sanitized form, never a
    raw-vs-sanitized mismatch)."""
    return safe_write_inbox.sanitize_component(f"{task_id}{_JSON_SUFFIX}")


def outbox_archive_has_verdict(outboxes_root: Path, agent: str, task_id: str) -> bool:
    """True iff a consumed verdict outbox for ``task_id`` sits in
    ``<outboxes_root>/<agent>/.archive/`` — the durable delivered-verdict proof.

    Matches the base name and the collision-suffixed ``<stem>.<n>.json`` using
    pure string ops (NOT ``glob``), so a task_id containing a glob metacharacter
    (``* ? [ ]``, preserved by the inbox sanitizer) can never break the match.
    A ``.forfeit.json`` sibling is excluded (its middle token isn't all-digits)."""
    base = archived_outbox_name(task_id)
    archive = outboxes_root / agent / ".archive"
    if (archive / base).exists():
        return True
    stem = base[: -len(_JSON_SUFFIX)] if base.endswith(_JSON_SUFFIX) else base
    prefix = stem + "."
    try:
        for p in archive.iterdir():
            n = p.name
            if n.startswith(prefix) and n.endswith(_JSON_SUFFIX):
                middle = n[len(prefix): -len(_JSON_SUFFIX)]
                if middle.isdigit():  # a collision counter, not '.forfeit'
                    return True
    except OSError:
        pass
    return False


def inbox_archive_has_same_review(inboxes_root: Path, agent: str, claim_name: str) -> bool:
    """True iff a review request with the SAME filename already sits in
    ``<inboxes_root>/<agent>/.archive/`` — i.e. this claim is a re-dispatch of a
    review that already ran to a terminal (archived) conclusion. The filename
    encodes the task_id, so a same-name match is a same-review match."""
    try:
        return (inboxes_root / agent / ".archive" / claim_name).exists()
    except OSError:
        return False


def notifier_log_marked_delivered(notifier_log: Path, task_id: str) -> bool:
    """True iff outbox-notifier.log recorded a Mirror verdict delivery for
    ``task_id``. SUPPLEMENTARY / rotation-fragile — behind the durable archive
    checks. Reads at most the trailing NOTIFIER_LOG_MAX_BYTES.

    The delivery line has the exact shape ``marker-notified <target> <- mirror
    (... file=notify-<task_id>.json)`` (outbox_notifier), so we match the FULL
    ``notify-<task_id>.json`` filename token rather than a bare ``task_id``
    substring. That closes the substring-ID collision that ``in`` (or even a
    hyphen-boundary token match) would leave open: ``task-alpha`` must NOT match
    a delivery line for ``task-alpha-2`` — and it doesn't, because
    ``notify-task-alpha.json`` is not a substring of
    ``notify-task-alpha-2.json``."""
    token = f"notify-{task_id}{_JSON_SUFFIX}"
    try:
        size = notifier_log.stat().st_size
        with open(notifier_log, "r", errors="replace") as f:
            if size > NOTIFIER_LOG_MAX_BYTES:
                f.seek(size - NOTIFIER_LOG_MAX_BYTES)
                f.readline()  # drop the partial first line
            for line in f:
                if ("marker-notified" in line and "<- mirror" in line
                        and token in line):
                    return True
    except OSError:
        return False
    return False


def verdict_delivered(*, outboxes_root: Path, inboxes_root: Path,
                      notifier_log: Path, agent: str, task_id: str,
                      claim_name: str) -> bool:
    """True iff a Mirror verdict for this task was already delivered — any of the
    two durable filesystem signals, falling back to the notifier log."""
    return (
        outbox_archive_has_verdict(outboxes_root, agent, task_id)
        or inbox_archive_has_same_review(inboxes_root, agent, claim_name)
        or notifier_log_marked_delivered(notifier_log, task_id)
    )
