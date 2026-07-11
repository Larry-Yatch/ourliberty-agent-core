#!/usr/bin/env python3
"""task_no_pr_legitimacy.py — the ONE canonical "did this task legitimately
conclude without a PR?" classifier.

THE PROBLEM this centralizes
----------------------------
Several healer daemons infer a stall/failure from the ABSENCE of a PR/commit/
review for a dispatched task, then retry / re-dispatch / escalate (DM Larry).
But several task types legitimately produce NO PR of their own — a Mirror
review emits a *verdict*, a preflight REJECT is an intentional non-build, a
`kickoff-*` is a state transition, a `dag-preflight-*` is a review. Before this
module each healer re-inferred "is a missing PR OK here?" ad-hoc, so coverage
drifted: `heal_pipeline_stall.check_forge_built_no_pr` grew ~9 stacked
per-incident suppress steps while `check_stalled_active_step` had ZERO guard and
false-escalated legit-no-PR steps.

This is the single source of truth those call-sites consult as their FIRST gate.

THE VERDICTS
------------
`expects_no_pr(task_id, *, outbox=None) -> (verdict, reason)`:

  * ``LEGIT_NO_PR`` — the task's CORRECT terminal outcome is no PR. A caller
    may suppress its retry/alert.
  * ``EXPECTS_PR``  — the task should have opened/updated a PR; a missing PR is
    a REAL failure. A caller must NOT suppress (a `forge/*` build that emitted
    ack-proceed then died; a `doc-only` task; a `rebase-*`/`resolve-*` whose
    TARGET PR is still OPEN, so a genuine missing update surfaces).
  * ``UNKNOWN``     — DEFAULT, conservative. Anything not positively classified.
    Callers MUST fall through to their EXISTING gh-existence checks on UNKNOWN —
    never silently suppress. A missed LEGIT_NO_PR shape therefore degrades to
    today's behavior (status quo), never a NEW false-suppression. Every UNKNOWN
    is logged (task_id + why) so a genuinely-new legit-no-PR shape self-surfaces
    and can be added as a rule — incompleteness is self-correcting, not a silent
    trap.

TARGET-PR-OPERATING TASKS
-------------------------
A whole family of tasks force-push an EXISTING PR #N and NEVER open a PR of
their own — a Mirror review (`mirror-review-pr-<repo>-<N>`), a rebase
(`rebase-pr-<N>`), a conflict resolve (`resolve-pr<N>`), each optionally
``-retry<k>``-suffixed. For these the "missing PR" question is really "is PR #N
finished?": once #N is MERGED/CLOSED a WIP-retried rebase legitimately produces
no PR (LEGIT_NO_PR — the live `rebase-pr-860-001` false-escalation this fixes);
while #N is still OPEN the task genuinely should have updated it, so a missing
update is a real stall (EXPECTS_PR); an unresolvable target state stays UNKNOWN.
``mirror-review-*`` remains a strong-shape special case (suppressed
unconditionally, no gh probe) — a review emits only a verdict, so it needs no
target-state check; ``rebase-*``/``resolve-*`` consult PR #N's terminal state.

CONSERVATISM CONTRACT
---------------------
Only a POSITIVE ``LEGIT_NO_PR`` may cause a caller to suppress. ``doc-only``
tasks open a PR, so they are encoded as ``EXPECTS_PR`` — a real stall on them is
never hidden. ``rebase-*``/``resolve-*`` are suppressed ONLY when their target
PR is provably TERMINAL; an OPEN target keeps them EXPECTS_PR.

PURITY
------
No network and no disk I/O at import or in ``expects_no_pr`` itself, WITH ONE
scoped exception: the target-PR rule for ``rebase-*``/``resolve-*`` does one
bounded ``gh pr view`` per tracked repo (via ``task_terminal_state``) to read
PR #N's terminal state. Every other verdict path stays pure. The outbox-truth
rules operate on an injected ``outbox`` dict (the archived Forge outbox OR a
dispatch envelope — both carry ``result`` / ``exit_code`` / ``phase`` /
``task_type``), so callers load it and pass it in; tests inject a dict directly.
The only other side effect is a best-effort append to the observability log on
an UNKNOWN verdict, which never raises.

Enumeration was confirmed against the real dispatch/intent producers (cited in
the PR body): `mirror-review-pr-*` (outbox_notifier / the mirror-review branch
shape), `review-sequence-dag-*` (heal_pipeline_stall / outbox_notifier),
`dag-preflight-*` (dispatch_validator.ALLOWED_INTENTS), `kickoff-*`
(build_sequence_advancer / build_sequence_kickoff), `notify-*` / `review-*`
(task_type_inference, heal_missions_card_gc.REVIEW_SHAPED_TASK_ID_PREFIXES),
`direction-ask` task_type (heal_unregistered_approval), and the Forge preflight
non-PROCEED / errored outbox shapes (heal_pipeline_stall).

stdlib only.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------- verdict constants ----------------

LEGIT_NO_PR = 'LEGIT_NO_PR'
EXPECTS_PR = 'EXPECTS_PR'
UNKNOWN = 'UNKNOWN'

# ---------------- task-id shape tables ----------------

# Shapes whose CORRECT terminal outcome is no PR of their own. Each is a
# review/decision/state-transition, never a build:
#   mirror-review-*     a Mirror review of an external PR / a session-less
#                       decision approval — emits a verdict, never a PR. The
#                       review-shaped special case of the target-PR-operating
#                       family (see _TARGET_PR_OP_PREFIXES): suppressed
#                       unconditionally here (a review has no PR to update, so no
#                       target-state probe is needed).
#   review-sequence-dag-*  a DAG-level review dispatch.
#   dag-preflight-*     a build-sequence preflight review (REJECT/REVISION).
#   review-            a Mirror review dispatch (task_type_inference: "review").
#   kickoff-           a build_sequence_advancer kickoff — a state transition.
#   notify-            an inter-agent notify leg (task_type_inference:
#                       "notification") — a message, never a PR.
_LEGIT_NO_PR_PREFIXES = (
    'mirror-review-',
    'review-sequence-dag-',
    'dag-preflight-',
    'review-',
    'kickoff-',
    'notify-',
)

# Shapes that DO produce/update a PR and must NEVER be suppressed:
#   build-       a Forge build-phase dispatch — opens a PR.
#   opmanual-    operating-manual edits (task_type_inference: "doc-only") —
#                open a PR.
# NOTE: `rebase-` is NOT here — it operates on an EXISTING target PR and its
# legitimacy is target-state-conditional, handled by the target-PR rule below.
_EXPECTS_PR_PREFIXES = (
    'build-',
    'opmanual-',
)

# Target-PR-operating shapes: they force-push an EXISTING PR #N (parsed from the
# task id) and never open a PR of their own, so legitimacy tracks PR #N's
# terminal state rather than being a fixed verdict. `mirror-review-` is the
# review-shaped member but keeps its unconditional strong-shape fast-path above
# (no PR to update → no probe); these two consult the target PR:
#   rebase-      rebase a branch and force-push its existing PR.
#   resolve-     resolve conflicts on / re-push an existing PR.
_TARGET_PR_OP_PREFIXES = (
    'rebase-',
    'resolve-',
)

# Extract the target PR number from a target-PR-operating task id. Matches a
# `pr-<N>` or `pr<N>` segment at a boundary (`rebase-pr-860-001`,
# `resolve-pr123`, `rebase-forge-pr-42`, any `...-retry<k>` suffix ignored). A
# letter after `pr-` (as in `mirror-review-pr-<repo>-<N>`) does not match here —
# those are handled by the strong-shape fast-path, not this rule.
_TARGET_PR_RE = re.compile(r'(?:^|-)pr-?(\d+)(?=-|$)')

# task_type values (from a dispatch envelope) that never back a PR — these are
# decisions/messages Beacon consumes, not builds.
_LEGIT_NO_PR_TASK_TYPES = frozenset({'direction-ask', 'notification', 'review'})

# task_type values that DO back a PR and must never be suppressed.
_EXPECTS_PR_TASK_TYPES = frozenset({'doc-only', 'build'})


# ---------------- observability ----------------

def _log_path() -> Path:
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    if override:
        return Path(override) / 'task_no_pr_legitimacy.log'
    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
    return root / 'logs' / 'task_no_pr_legitimacy.log'


def _observe_unknown(task_id: str, reason: str) -> None:
    """Best-effort append of an UNKNOWN verdict so a genuinely-new legit-no-PR
    shape self-surfaces in the logs (the incompleteness-is-self-correcting
    contract). Never raises — a full/read-only log FS must not perturb a
    caller's stall decision."""
    line = (f'[{datetime.now(timezone.utc).isoformat()}] [UNKNOWN] '
            f'task={task_id!r} reason={reason}')
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


# ---------------- outbox-truth helpers (pure; operate on an injected dict) ----------------

def _result_text(outbox: dict) -> str:
    r = outbox.get('result')
    return r if isinstance(r, str) else ''


def outbox_preflight_verdict(outbox: Optional[dict]) -> Optional[str]:
    """Classify an archived Forge outbox as a clean preflight non-PROCEED.
    Returns ``'CLARIFY_REQUEST'`` / ``'REJECT_REQUEST'`` / ``'PREFLIGHT_EXIT'``
    when the outbox proves Forge intentionally did not open a PR, else ``None``.

    This is the SINGLE encoding of the preflight-non-PROCEED rule; the
    ``heal_pipeline_stall._forge_preflight_non_proceed`` disk-reading helper
    delegates here so the rule lives in one place. Mirrors the historical
    labels exactly:
      * marker delimiter in ``result`` → the corresponding request label;
      * a clean preflight exit whose ``result`` is prose narration only
        (``phase=='preflight'`` AND ``exit_code==0`` AND ``attempts>=1``) →
        ``'PREFLIGHT_EXIT'``.
    """
    if not isinstance(outbox, dict):
        return None
    result = _result_text(outbox)
    if '=== CLARIFY_REQUEST ===' in result:
        return 'CLARIFY_REQUEST'
    if '=== REJECT_REQUEST ===' in result:
        return 'REJECT_REQUEST'
    if (
        outbox.get('phase') == 'preflight'
        and outbox.get('exit_code') == 0
        and isinstance(outbox.get('attempts'), int)
        and outbox.get('attempts') >= 1
    ):
        return 'PREFLIGHT_EXIT'
    return None


def _outbox_errored(outbox: dict) -> bool:
    """True iff the outbox shows a CONSUMED-BUT-ERRORED dispatch (no clean PR):
    a non-zero ``exit_code``, a non-empty ``error``, or a gh-pr-create auth_401
    cue. Such a run DIED before finishing its job — a real failure to recover,
    never a legit no-PR outcome (mirrors the errored signal
    ``heal_pipeline_stall._classify_outbox_error`` keys on)."""
    exit_code = outbox.get('exit_code')
    error = outbox.get('error')
    error_str = error.strip() if isinstance(error, str) else ''
    haystack = f'{error_str} {_result_text(outbox)}'.lower()
    auth_401 = 'auth_401' in haystack or '401' in haystack
    return (
        (isinstance(exit_code, int) and exit_code != 0)
        or bool(error_str)
        or auth_401
    )


def _outbox_no_buildable_delta(outbox: dict) -> bool:
    """True iff the outbox's ``result`` narrates a clean 'no buildable delta' /
    PREFLIGHT_EXIT outcome in prose (the fallback for a preflight exit whose
    marker went to the session log, not the archived ``result`` field)."""
    low = _result_text(outbox).lower()
    return 'no buildable delta' in low or 'preflight_exit' in low


# ---------------- task-id shape helpers ----------------

def is_preflight_or_clarify(task_id: str) -> bool:
    """True if ``task_id`` is a preflight/clarify pointer (contains
    ``-preflight`` or ``-clarify``). These never open their own PR — the build
    is a separate, fresh-timestamp dispatch. The single encoding
    ``heal_pipeline_stall._is_preflight_or_clarify_task`` delegates to."""
    return isinstance(task_id, str) and (
        '-preflight' in task_id or '-clarify' in task_id)


def target_pr_number(task_id: str) -> Optional[int]:
    """Parse the target PR number a ``rebase-*``/``resolve-*`` task operates on
    (``rebase-pr-860-001`` → 860, ``resolve-pr123`` → 123, ``rebase-forge-pr-42``
    → 42; any ``-retry<k>`` suffix ignored). Returns ``None`` when no ``pr<N>``
    segment is present — the caller then leaves the verdict conservative."""
    if not isinstance(task_id, str):
        return None
    m = _TARGET_PR_RE.search(task_id)
    return int(m.group(1)) if m else None


def _target_pr_verdict(number: int) -> tuple[str, str]:
    """Read target PR ``number``'s terminal state across tracked repos and map it
    to a verdict: any repo still OPEN → EXPECTS_PR (a genuine missing update);
    else any repo TERMINAL (merged/closed) → LEGIT_NO_PR (the retry chased an
    already-finished PR); otherwise UNKNOWN (conservative — a gh failure or an
    untracked/never-created number is never a positive suppress). OPEN wins over
    TERMINAL so a same-number PR still open in ANY repo blocks suppression.

    This is the ONLY gh-touching path in the module; the import is lazy so the
    pure paths keep importing where the probe dependency chain is absent."""
    import task_terminal_state as tts  # noqa: E402 — gh probe only on this path
    states = [
        tts.pr_coordinate_state(tts._qualify_repo(repo), number)[0]
        for repo in tts.default_repos()
    ]
    if tts.OPEN in states:
        return EXPECTS_PR, f'target-pr#{number}-open'
    if any(s in tts.TERMINAL_STATES for s in states):
        return LEGIT_NO_PR, f'target-pr#{number}-terminal'
    return UNKNOWN, f'target-pr#{number}-unknown'


# ---------------- the classifier ----------------

def expects_no_pr(task_id: str, *, outbox: Optional[dict] = None
                  ) -> tuple[str, str]:
    """Classify whether ``task_id`` legitimately concludes without a PR.

    Returns ``(verdict, reason)`` where ``verdict`` is ``LEGIT_NO_PR`` /
    ``EXPECTS_PR`` / ``UNKNOWN``. ``reason`` is a short human-readable label for
    logs. ``outbox`` (optional) is an archived Forge outbox OR a dispatch
    envelope dict; when provided it unlocks the outbox-truth rules (preflight
    REJECT / 'no buildable delta' / PREFLIGHT_EXIT → LEGIT_NO_PR; errored or
    ack-proceed-then-died → EXPECTS_PR).

    Evaluation order is precedence-sensitive:
      1. strong review/decision/state-transition SHAPES (never builds) →
         LEGIT_NO_PR — outbox irrelevant.
      2. outbox-truth (when a dict is provided): REJECT/CLARIFY → LEGIT_NO_PR;
         PROCEED / errored → EXPECTS_PR; PREFLIGHT_EXIT / no-delta → LEGIT_NO_PR.
         Placed BEFORE the ``build-``/``opmanual-`` shape prefixes because a
         build-SEQUENCE preflight task is legitimately named
         ``build-sequence-...`` yet exits without a PR — its PREFLIGHT_EXIT /
         REJECT outbox is the authoritative signal and must win over the coarse
         ``build-`` prefix. When no outbox is supplied this whole block is
         skipped and the shape prefixes decide (the no-outbox callers).
      2b. target-PR-operating SHAPES (rebase-/resolve-): read the target PR #N's
         terminal state — MERGED/CLOSED → LEGIT_NO_PR, still OPEN → EXPECTS_PR,
         unresolvable → UNKNOWN. Placed AFTER outbox-truth so a rebase whose
         outbox proves it errored/proceeded is decided by that hard evidence
         before the softer target-state inference. This is the one path that
         touches gh (bounded, one probe per tracked repo).
      3. must-never-suppress SHAPES (build-/opmanual-, doc-only/build
         task_type) → EXPECTS_PR — the fallback when no outbox truth overrides.
      4. weak SHAPE: ``*-preflight`` / ``*-clarify`` pointer → LEGIT_NO_PR
         (after outbox-truth so an ERRORED preflight resolves to EXPECTS_PR).
      5. envelope task_type decisions (direction-ask/notification/review) →
         LEGIT_NO_PR.
      6. UNKNOWN (logged).
    """
    if not isinstance(task_id, str) or not task_id:
        _observe_unknown(str(task_id), 'empty-or-non-str-task-id')
        return UNKNOWN, 'empty-or-non-str-task-id'

    # 1. Strong LEGIT shapes — reviews / decisions / state transitions. These
    #    are never builds, so no outbox can turn them into a PR-bearing task.
    for prefix in _LEGIT_NO_PR_PREFIXES:
        if task_id.startswith(prefix):
            return LEGIT_NO_PR, f'shape:{prefix}*'

    # 2. Outbox-truth — authoritative when a dict is provided. Placed BEFORE the
    #    build-/rebase-/opmanual- prefixes so a `build-sequence-*` PREFLIGHT task
    #    (build- prefix, but a preflight-exit outbox) resolves by its outbox, not
    #    misclassified as a PR-bearing build.
    if isinstance(outbox, dict):
        verdict = outbox_preflight_verdict(outbox)
        if verdict in ('CLARIFY_REQUEST', 'REJECT_REQUEST'):
            return LEGIT_NO_PR, f'outbox:{verdict.lower()}'
        # A PROCEED marker means Forge committed to building; a missing PR after
        # that is the real stall (proceeded-then-died).
        if '=== PROCEED ===' in _result_text(outbox):
            return EXPECTS_PR, 'outbox:proceed-then-no-pr'
        if _outbox_errored(outbox):
            return EXPECTS_PR, 'outbox:consumed-but-errored'
        if verdict == 'PREFLIGHT_EXIT':
            return LEGIT_NO_PR, 'outbox:preflight_exit'
        if _outbox_no_buildable_delta(outbox):
            return LEGIT_NO_PR, 'outbox:no-buildable-delta'

    # 2b. Target-PR-operating shapes (rebase-/resolve-) — a force-push onto an
    #     EXISTING PR #N, never a PR of their own. Legitimacy tracks PR #N: a
    #     TERMINAL (merged/closed) target means a WIP-retried rebase legitimately
    #     produced no PR; a still-OPEN target means a real missing update. A
    #     parseable number triggers one bounded gh probe per tracked repo; no
    #     parseable number falls through to the conservative UNKNOWN default.
    if task_id.startswith(_TARGET_PR_OP_PREFIXES):
        number = target_pr_number(task_id)
        if number is not None:
            verdict, treason = _target_pr_verdict(number)
            if verdict == UNKNOWN:
                _observe_unknown(task_id, treason)
            return verdict, treason

    # 3. Must-never-suppress shapes — these DO produce/update a PR (fallback when
    #    no outbox truth decided above).
    for prefix in _EXPECTS_PR_PREFIXES:
        if task_id.startswith(prefix):
            return EXPECTS_PR, f'shape:{prefix}*'

    # Envelope task_type — an authoritative signal when present.
    task_type = outbox.get('task_type') if isinstance(outbox, dict) else None
    if isinstance(task_type, str):
        if task_type in _EXPECTS_PR_TASK_TYPES:
            return EXPECTS_PR, f'task_type:{task_type}'
        if task_type in _LEGIT_NO_PR_TASK_TYPES:
            return LEGIT_NO_PR, f'task_type:{task_type}'

    # 4. Weak preflight/clarify pointer shape (after outbox-truth so an errored
    #    preflight is EXPECTS_PR, not suppressed).
    if is_preflight_or_clarify(task_id):
        return LEGIT_NO_PR, 'shape:preflight-or-clarify-pointer'

    # 6. Default — conservative UNKNOWN, logged so a new shape self-surfaces.
    _observe_unknown(task_id, 'unclassified-shape')
    return UNKNOWN, 'unclassified-shape'
