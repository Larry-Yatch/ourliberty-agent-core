"""Canonical fixture-pattern allowlist for Pulse /cycle hallucination class.

Single source of truth. Mirrored in human-readable form into
`runbooks/cycle-prompt.md`, `agents/pulse/CLAUDE.md`, and
`scripts/run_cycle.sh` (regex form for shell). If those lists ever
diverge from this module, this module wins — the others are docs.

Background: 2026-05-27 — Pulse's /cycle scans inbox + log state for real
failures, but test artifacts (fixture envelopes from cascading marker
errors or from in-flight test execution) look structurally identical to
real failures. Pulse hallucinated cycle-fix dispatches for ~18 fixture
envelopes in one window, burning real Opus cost. The systemic fix per
PRIME DIRECTIVE is a fixture allowlist applied across five surfaces:
the cycle-prompt teach, the Pulse CLAUDE.md teach, the run_cycle.sh
commit guard, the Check I + Check III data substrates, and a regression
test gate.

The pattern list deliberately stays small — broad globs would risk
false-positives on real task_ids (e.g., a prefix of `t-` would silently
swallow `tier2-*` if we weren't careful — confirmed below by the
restriction to anchored prefixes only). When adding a pattern, prove it
does not collide with any real task_id in `~/agents/outboxes/*/.archive/`.

Stdlib only; importable from every script that touches the cycle path.
"""

from __future__ import annotations


FIXTURE_PATTERN_PREFIXES: tuple[str, ...] = (
    "t-",
    "sess-abc-",
    "notify-t-",
    "notify-q-",
    "marker-error-t-",
    "marker-error-opmanual-",
)

FIXTURE_PATTERN_EXACT: frozenset[str] = frozenset({
    "task-001",
    "task-legacy",
    "headless-001",
    "opmanual-d35-5b-shipped-note-001",
    "pf-ok",
    "bad-pf",
    "no-preamble",
    "no-chat",
})


# Wrapper prefixes the routing layer prepends to envelope filenames as a
# task moves through the notify/dead-letter cascade. They stack (e.g.,
# `notify-dead-letter-notify-q-1.18.json` is a fixture base buried under
# three wrappers). `is_fixture_envelope_name` peels these iteratively to
# match wrapped fixtures the bare `is_fixture_task_id` would miss.
_ENVELOPE_WRAPPER_PREFIXES: tuple[str, ...] = (
    "notify-",
    "dead-letter-",
    "marker-error-",
)

# Cycle-guard cap for the peel loop. Real wrappers stack at most ~3 deep
# (notify→dead-letter→notify on a doubled-prefix bug); 8 is generous and
# bounds pathological inputs without risk of false negatives on real shapes.
_ENVELOPE_PEEL_CAP: int = 8


def is_fixture_task_id(task_id: object) -> bool:
    """True iff task_id matches a documented fixture-pattern.

    Accepts any object — non-string inputs return False (defensive: cycle
    code paths sometimes hand us payload dicts where the field is missing
    or None). Empty string returns False.
    """
    if not isinstance(task_id, str) or not task_id:
        return False
    if task_id in FIXTURE_PATTERN_EXACT:
        return True
    for prefix in FIXTURE_PATTERN_PREFIXES:
        if task_id.startswith(prefix):
            return True
    return False


def matched_fixture_pattern(task_id: object) -> str | None:
    """Return the prefix or exact-match string that flagged task_id, or None.

    Used by callers that need to log which pattern fired (cycle-actions
    audit trail). Returns the longest matching prefix when multiple match
    (none currently overlap, but the discipline keeps future additions
    safe).
    """
    if not isinstance(task_id, str) or not task_id:
        return None
    if task_id in FIXTURE_PATTERN_EXACT:
        return task_id
    best: str | None = None
    for prefix in FIXTURE_PATTERN_PREFIXES:
        if task_id.startswith(prefix):
            if best is None or len(prefix) > len(best):
                best = prefix
    return best


def is_fixture_envelope_name(name: object) -> bool:
    """True iff envelope filename resolves to a fixture under wrapper-peeling.

    Self-replicating cascades bury the fixture task_id behind one or more
    routing wrappers (`notify-`, `dead-letter-`, `marker-error-`) and a
    trailing `.<seq>` suffix from filename collision-rename. A bare
    `is_fixture_task_id` on `envelope_path.name` misses these. This helper
    strips the `.json` extension and trailing numeric suffix once, then
    iteratively peels known wrappers from the front and retests
    `is_fixture_task_id` after each peel. Returns True at the first match.

    Cycle-guarded: aborts after `_ENVELOPE_PEEL_CAP` iterations and returns
    False (conservative — never false-positive a real task on pathological
    input).

    Accepts any object; non-string / empty returns False.
    """
    if not isinstance(name, str) or not name:
        return False

    # Strip .json envelope extension. Inbox filenames carry it; task_ids
    # in FIXTURE_PATTERN_EXACT do not.
    if name.endswith(".json"):
        name = name[: -len(".json")]

    # Strip trailing `.<seq>` collision-rename suffix (one layer only —
    # `_unique_dest` only appends a single numeric tail).
    dot_idx = name.rfind(".")
    if dot_idx > 0 and name[dot_idx + 1:].isdigit():
        name = name[:dot_idx]

    # Test the bare form first — bypasses the loop for the common case.
    if is_fixture_task_id(name):
        return True

    # Iteratively peel known wrappers and retest.
    for _ in range(_ENVELOPE_PEEL_CAP):
        peeled = False
        for prefix in _ENVELOPE_WRAPPER_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix):]
                peeled = True
                break
        if not peeled:
            return False
        if is_fixture_task_id(name):
            return True
    # Cycle guard tripped — conservative False (don't false-positive).
    return False


# Shell-regex form, kept beside the Python list so run_cycle.sh and any
# other bash consumer source from the same module. The `^` anchor matches
# the start of the captured task_id literal; callers wrap as needed.
SHELL_FIXTURE_REGEX = (
    "^(t-|sess-abc-|notify-t-|notify-q-|marker-error-t-|"
    "marker-error-opmanual-|task-001$|task-legacy$|headless-001$|"
    "opmanual-d35-5b-shipped-note-001$|pf-ok$|bad-pf$|"
    "no-preamble$|no-chat$)"
)
