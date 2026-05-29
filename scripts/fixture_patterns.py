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
    # 2026-05-28: structural sibling of task-001. Collision proof against
    # ~/agents/{inboxes,outboxes}/*/.archive/ (3968 distinct stems) found it
    # ONLY ever wrapped as `dead-letter-notify-notify-task-legacy*` cascade
    # artifacts — never a bare real task — so exact-match suppression cannot
    # swallow real work. Closes the doubled-notify dead-letter cascade.
    "task-legacy",
    "headless-001",
    "opmanual-d35-5b-shipped-note-001",
    "pf-ok",
    "bad-pf",
    "no-preamble",
    "no-chat",
    # 2026-05-29 incident: dead-letter GC/bad-task D3.5 fixtures. Collision-
    # proof — 202/104/96 archive hits, all `.N` loop artifacts, never real work.
    "dead-letter-bad",
    "dead-letter-gc",
    "dead-letter-bad-task",
    # 2026-05-29 incident: envelope-id retry-loop — the 54-burn half of the
    # cost leak documented in extend-fixture-gate-outbox-side. Archive search:
    # 218 hits across ~/agents/{outboxes,inboxes}/, ALL as `envelope-id.N`
    # cascade artifacts (one bare `envelope-id.json` from source
    # `outbox-notifier`) — never a bare real task. Closes the larger of the
    # two halves so the outbox-side gate quarantines marker-error responses
    # whose task_id is `envelope-id`. matched_fixture_envelope peels routing
    # wrappers (`marker-error-envelope-id-1` etc.) automatically.
    "envelope-id",
    # 2026-05-29 incident: 5 AUTO_MERGE failures (15:17–15:26 UTC) on fixture
    # task_ids producing bogus pr_urls (`x/y/pull/N` placeholder repo, or
    # `ourliberty/ourliberty-agent-core/pull/0`). The AUTO_MERGE pr-url-
    # allowlist gate caught them, but they still drove Forge→Mirror→notifier
    # cycles before being refused. The outbox-side fixture gate added in
    # PR #201 (15:27 UTC) reads through this allowlist — these entries make
    # it catch the family. Exact entries (not a `real-` prefix) because the
    # test suite uses `real-*` as the literal mock name for non-fixture work
    # (`real-task`, `real-heavy-task`, `real-build-123`, `real-0..real-9`);
    # a broad prefix would silently swallow those mocks and would conflict
    # with the discipline at L21-22 of this module ("prove no collision with
    # any real task_id"). Collision proof: 57 `real-*` archive hits, all
    # confirmed as fixture cascade artifacts (placeholder pr_urls, no real
    # specs, dated 2026-05-27/29).
    "real-mirror-bad-marker",
    "real-mirror-retry",
    "real-ok",
    "real-empty-findings",
    "real-loop",
    "real-built",
    "real-build-ok",
})


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


# Routing-wrapper prefixes the chain prepends to an envelope NAME as a task
# cascades (notify -> dead-letter -> marker-error). A fixture task_id buried
# under one or more of these — e.g. `marker-error-notify-t-pf-1`,
# `notify-dead-letter-notify-q-1.18` — evades a raw is_fixture_task_id on the
# envelope name, so the self-replicating cascade kept regenerating and the
# inbox-watcher kept dispatching it. See the 2026-05-28 cost-loop incident and
# docs/inbox-watcher-fixture-gate-brief.md.
ROUTING_WRAPPER_PREFIXES: tuple[str, ...] = (
    "dead-letter-marker-",
    "notify-",
    "dead-letter-",
    "marker-error-",
)


def _strip_seq_suffix(name: str) -> str:
    """Strip the trailing `.<int>` collision suffix(es) added by unique-dest.

    The watcher/notifier append `.<requeue-index>` or `.<epoch>` on name
    collision (e.g. `notify-q-1.18`), and these can stack. Peel every trailing
    dot-integer segment so the underlying id is what gets pattern-tested.
    """
    while True:
        head, dot, tail = name.rpartition(".")
        if dot and tail.isdigit():
            name = head
        else:
            return name


def matched_fixture_envelope(name: object) -> str | None:
    """Return the fixture pattern flagging an envelope NAME, or None.

    Unlike matched_fixture_pattern (which tests a bare task_id), this peels the
    routing-wrapper prefixes that accumulate as a fixture envelope
    self-replicates, tolerates the trailing `.<seq>` collision suffix, and
    re-tests the fixture allowlist at every peel layer. Pass the file STEM (no
    `.json`). Non-string / empty -> None. Reuses the single source of truth in
    matched_fixture_pattern — it introduces NO new patterns, so it can only flag
    a name whose some peel layer is already a documented fixture id.
    """
    if not isinstance(name, str) or not name:
        return None
    candidate = _strip_seq_suffix(name)
    seen: set[str] = set()
    for _ in range(16):  # cycle guard; real chains nest only a few wrappers
        if candidate in seen:
            break
        seen.add(candidate)
        hit = matched_fixture_pattern(candidate)
        if hit is not None:
            return hit
        peeled = candidate
        for wrapper in ROUTING_WRAPPER_PREFIXES:
            if peeled.startswith(wrapper):
                peeled = peeled[len(wrapper):]
                break
        if peeled == candidate:
            break  # no wrapper left to peel
        candidate = _strip_seq_suffix(peeled)
    return None


def is_fixture_envelope_name(name: object) -> bool:
    """True iff an inbox/outbox envelope NAME resolves to a fixture pattern.

    Wrapper-peeling companion to is_fixture_task_id; see matched_fixture_envelope.
    """
    return matched_fixture_envelope(name) is not None


# Shell-regex form, kept beside the Python list so run_cycle.sh and any
# other bash consumer source from the same module. The `^` anchor matches
# the start of the captured task_id literal; callers wrap as needed.
SHELL_FIXTURE_REGEX = (
    "^(t-|sess-abc-|notify-t-|notify-q-|marker-error-t-|"
    "marker-error-opmanual-|task-001$|task-legacy$|headless-001$|"
    "opmanual-d35-5b-shipped-note-001$|pf-ok$|bad-pf$|"
    "no-preamble$|no-chat$)"
)
