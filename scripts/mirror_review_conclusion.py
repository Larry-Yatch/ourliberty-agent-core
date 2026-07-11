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

import glob
import json
import re
import sys
from pathlib import Path
from typing import Optional

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
    two durable filesystem signals, falling back to the notifier log.

    ROUND-BLIND: every signal is keyed on the ``task_id`` (or the exact
    ``claim_name``), so ANY round's delivered verdict satisfies it. That is the
    correct contract for inbox_watcher.sweep_claimed_orphans (the startup 4h
    backstop, which only needs "was this PR ever reviewed") but is WRONG for a
    re-review whose OWN round must be judged — see ``round_verdict_delivered``,
    which the 10-min heal_orphaned_mirror_claims healer uses so a prior round's
    verdict can never mask a not-concluded current round."""
    return (
        outbox_archive_has_verdict(outboxes_root, agent, task_id)
        or inbox_archive_has_same_review(inboxes_root, agent, claim_name)
        or notifier_log_marked_delivered(notifier_log, task_id)
    )


# ==================== round-aware conclusion (head-pinned) ====================
#
# The task_id-keyed signals above cannot tell a rev-N round's verdict apart from
# round-0's: the verdict OUTBOX never records head_sha (inbox_watcher._build_outbox
# does not propagate it), so a prior round's archived verdict at
# outboxes/mirror/.archive/<task_id>.json reads as "concluded" for a DIFFERENT
# round's claim — the gg-s4-silent-failure-gauge drop (PR #923, 2026-07-11).
#
# Round-awareness therefore keys off the archived review-REQUEST records, which
# DO carry head_sha and whose FILENAME carries the round suffix
# (review-<task>-rev<N>.json). A round is concluded iff its own request envelope
# was archived to a successful conclusion at THIS round's head.


# Round-record name grammar — kept behaviourally identical to
# outbox_notifier.review_record_name_re (a cross-module sync test pins the two so
# they can't drift; a third divergent copy is the exact fault the PR #865
# triple-dispatch taught). Matches every on-disk review-record filename for a
# review-task stem ``review-<task_id>``: the base ``<stem>.json``, ``move_to()``
# collisions ``<stem>.<i>.json``, revision rounds ``<stem>-rev<N>.json`` (+
# ``.<i>``), replan rounds ``<stem>-replan<P>.json`` (+ ``.<i>``), and
# replan+revision combos. ``stem`` is ``re.escape``d so a task_id carrying
# regex/glob metacharacters matches only its own records.
def review_record_name_re(stem: str) -> 're.Pattern[str]':
    return re.compile(
        re.escape(stem)
        + r'(?:-replan\d+)?(?:-rev\d+)?(?:\.\d+)?\.json$'
    )


# Strip a trailing round suffix (``-replan<P>`` / ``-rev<N>`` / ``.<i>``) from a
# review-record STEM so a round-N claim stem collapses to the task's round-0
# stem — the anchor ``review_record_name_re`` expands back into every sibling
# round. The on-disk suffix order is ``-replan<P>`` then ``-rev<N>`` then
# ``.<i>``; anchored at end so only a genuine trailing suffix is removed.
_ROUND_SUFFIX_RE = re.compile(r'(?:-replan\d+)?(?:-rev\d+)?(?:\.\d+)?$')


def base_review_stem(stem: str) -> str:
    return _ROUND_SUFFIX_RE.sub('', stem, count=1)


def recorded_head_sha(path: Path) -> Optional[str]:
    """The ``head_sha`` a review-request records (top-level, then nested under
    ``context``), or None — mirrors outbox_notifier._recorded_review_head_sha and
    inbox_watcher._task_head_sha. A record written before head_sha was captured
    returns None, which the caller treats as "does not cover this head"."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    v = data.get('head_sha')
    if isinstance(v, str) and v:
        return v
    ctx = data.get('context')
    if isinstance(ctx, dict):
        v = ctx.get('head_sha')
        if isinstance(v, str) and v:
            return v
    return None


def round_verdict_delivered(*, inboxes_root: Path, agent: str,
                            claim_name: str, head_sha: Optional[str]) -> bool:
    """True iff a verdict for THIS review ROUND was already delivered — judged by
    the round's own archived review-request, NOT by any task_id-keyed signal.

    Two round-aware proofs, neither of which a PRIOR round can satisfy:

      * the EXACT round-suffixed ``claim_name`` already sits in the inbox
        ``.archive/`` — round-aware by filename, and head-agnostic so it still
        recognises a legacy record that carries no ``head_sha``;
      * an archived review-request for this task (ANY round shape, via
        ``review_record_name_re`` over the round-0 base stem, incl. ``move_to()``
        ``.<i>`` uniquified names) whose recorded ``head_sha`` == this claim's
        ``head_sha`` — head-pinned, so round-0's verdict at an OLDER head never
        counts as a rev-N round's conclusion.

    Deliberately consults NEITHER the task_id-keyed verdict outbox nor the
    notifier log: both are round-blind (the outbox never records head_sha), so a
    prior round's delivered verdict would mask a not-concluded current round —
    the exact drop this predicate exists to prevent. When ``head_sha`` is None
    (ambiguous / legacy claim) only the exact-name proof can fire; the caller
    fails safe toward RE-INJECT on an open PR rather than archive-drop."""
    archive = inboxes_root / agent / ".archive"
    try:
        if (archive / claim_name).exists():
            return True
    except OSError:
        pass
    if not head_sha:
        return False
    stem = (
        claim_name[: -len(_JSON_SUFFIX)]
        if claim_name.endswith(_JSON_SUFFIX) else claim_name
    )
    base_stem = base_review_stem(stem)
    name_re = review_record_name_re(base_stem)
    try:
        for p in archive.glob(f"{glob.escape(base_stem)}*.json"):
            if name_re.fullmatch(p.name) and recorded_head_sha(p) == head_sha:
                return True
    except OSError:
        pass
    return False
