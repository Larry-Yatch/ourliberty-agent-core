#!/usr/bin/env python3
"""freshness_probe.py — the falsifiable-premise evaluator for approval cards.

SLICE 1 of 3 of the approvals-freshness arc (parent capture
cap-approvals-tab-let-a-card-carry-its-own-falsifier-8bb3). This slice defines
ONLY the schema + a pure evaluator. It is wired into NOTHING: no UI, no healer
change, no producer retrofit. `evaluate()` is called by no code in this slice —
slices 2/3 (the 10-min tick, the `premise_stale` demotion, the badge) consume it.

WHY THIS EXISTS
---------------
Approval cards go stale because nothing re-checks their premise. Today the only
clearing signals answer "did Beacon already resolve this?" or "did a PR for this
task_id merge/close?" (`scripts/task_terminal_state.py`). A card whose premise is
a LIVE-STATE fact — a DB row, a file's contents, a config key — probes UNKNOWN,
and UNKNOWN means keep forever. On 2026-07-29, 6 of 8 cards on the Approvals tab
did not need a human. A `freshness_probe` lets an author record, at filing time,
the exact FALSIFIABLE fact the ask depends on, so a later mechanism can notice
when it goes stale.

THE SCHEMA
----------
An optional `freshness_probe` object on an approval's `dispatch_payload`: a
deterministic, machine-checkable predicate whose FALSITY means the ask is moot.
Supported kinds (this slice):

  * `pr_state`      — name the work either by `task_id` (token-matched through
                      `task_terminal_state.task_terminal_state`) or DIRECTLY by a
                      `repo` + `pr_number` coordinate (resolved through
                      `task_terminal_state.pr_coordinate_state`); the premise
                      HOLDS while the work is OPEN/UNKNOWN and is FALSE once it is
                      terminal in the kind's declared `expect` direction.
  * `file_contains` — `git show <ref>:<path>` in a repo; premise holds while a
                      substring is PRESENT, false once it's gone.
  * `file_lacks`    — the inverse: premise holds while a substring is ABSENT.
  * `json_path`     — read a dotted key from a JSON config file; premise holds
                      while the value EQUALS an expected value.
  * `sql`           — a read-only query returning a boolean-shaped result; premise
                      holds while the query is truthy.

`shell` is explicitly OUT of this slice.

LOAD-BEARING SEMANTICS (the whole point)
----------------------------------------
`evaluate(probe)` returns exactly one of three explicit tri-state values:

  * TRUE          => premise still holds        => KEEP the card.
  * FALSE         => premise dead               => the ask is moot.
  * INDETERMINATE => couldn't decide            => every caller MUST treat as KEEP.

ERROR / timeout / unparseable / unknown kind / missing required field ALL collapse
to INDETERMINATE. Fail toward the human, never toward the convenient answer. This
mirrors the conservative posture `scripts/task_terminal_state.py` already takes
(any gh failure collapses to UNKNOWN=keep) and the Check 0 guard in PR #1058.

INJECTABLE EXECUTORS
--------------------
The three side-effecting kinds — git (`file_contains`/`file_lacks`), sql, and the
config read for `json_path` — take their I/O through injectable executors so this
module stays PURE and unit-testable with no live git/DB. Production defaults do
the real bounded subprocess / file read; every default fails toward INDETERMINATE.

Stdlib + `git`/`gh` (via `task_terminal_state`) only; bounded timeouts; no
Supabase dependency at import time.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import task_terminal_state as tts  # noqa: E402 — reused for the pr_state kind

# -------------------- tri-state constants (mirror task_terminal_state) --------------------
#
# The string values intentionally echo the MERGED/CLOSED/OPEN/UNKNOWN shape in
# task_terminal_state.py: explicit uppercase sentinels, compared by identity/value,
# never booleans (a bool can't carry the third "couldn't decide" state that is the
# entire safety property here).

TRUE = 'TRUE'
FALSE = 'FALSE'
INDETERMINATE = 'INDETERMINATE'

# The tri-state a caller must treat as KEEP is anything that is not FALSE — but the
# set is exported explicitly so a consumer can write `if verdict in KEEP_STATES`
# rather than re-deriving the conservative rule and getting it subtly wrong.
KEEP_STATES = frozenset({TRUE, INDETERMINATE})

# Bounded subprocess timeout for the git-backed kinds. Mirrors
# task_terminal_state.DEFAULT_GH_TIMEOUT_SEC: the CLI usually answers instantly;
# this gives headroom without letting a wedged subprocess stall a caller.
DEFAULT_GIT_TIMEOUT_SEC = 15

# The supported probe kinds THIS slice evaluates. An unknown/absent kind is
# INDETERMINATE (never guessed) — the same fail-toward-human posture as a missing
# required field. `shell` is deliberately absent (out of scope for slice 1).
SUPPORTED_KINDS = frozenset(
    {'pr_state', 'file_contains', 'file_lacks', 'json_path', 'sql'}
)

# For pr_state: the two directions a premise can point. `expect: "open"` (the
# default) means "the ask holds while the work is still live" — TRUE on OPEN,
# FALSE once MERGED/CLOSED. `expect: "terminal"` inverts it ("the ask holds until
# the work ships"). Either way UNKNOWN => INDETERMINATE (keep).
_PR_EXPECT_OPEN = 'open'
_PR_EXPECT_TERMINAL = 'terminal'

# The two coordinate fields that let a pr_state probe name a PR DIRECTLY instead
# of going through task_id's token match. Both must be present and valid, or
# neither — see _parse_pr_coordinate.
_PR_COORD_FIELDS = ('repo', 'pr_number')


# -------------------- default executors (real I/O; fail toward INDETERMINATE) --------------------

def _default_git_show(
    repo: str, ref: str, path: str, *, timeout: float = DEFAULT_GIT_TIMEOUT_SEC,
) -> Optional[str]:
    """Return the text of `path` at `ref` in `repo` via `git show <ref>:<path>`,
    or None on ANY failure — repo not a checkout, ref/path absent, git missing,
    timeout, non-zero exit, or undecodable bytes.

    None is the "couldn't read" signal the evaluator maps to INDETERMINATE; it is
    NEVER an assertion that the file is empty. Never raises. `repo` is the working
    directory the git command runs in (a local checkout); `ref` is any revision
    git accepts (e.g. `origin/main`)."""
    try:
        proc = subprocess.run(
            ['git', '-C', repo, 'show', f'{ref}:{path}'],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _default_pr_coordinate_probe(repo: str, number: int) -> str:
    """Terminal state of ONE PR named by (`repo`, `number`), as a bare
    task_terminal_state constant.

    Delegates to `task_terminal_state.pr_coordinate_state`, which already asks
    gh for exactly that PR and returns `(UNKNOWN, None)` on ANY failure — so the
    conservative posture is inherited, not re-implemented. That function returns
    a `(state, merge_sha)` tuple; only the state is a verdict input here, so the
    merge sha is dropped to match the bare-string contract the task_id executor
    already uses."""
    state, _merge_sha = tts.pr_coordinate_state(repo, number)
    return state


def _default_read_json(path: str) -> Optional[Any]:
    """Parse the JSON file at `path`, or None on ANY failure — missing file,
    unreadable, or unparseable. None maps to INDETERMINATE; never raises."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):
        return None


def _default_sql_bool(query: str, *, dsn: Optional[str] = None) -> Optional[bool]:
    """Default `sql` executor. This slice ships NO live DB binding — evaluate() is
    called by nothing here — so the default is deliberately inert: it returns None
    (=> INDETERMINATE => keep), never a fabricated verdict. Slices 2/3 inject a
    real read-only executor. Keeping the default inert means a probe that reaches
    production before its executor is wired can only ever KEEP, never falsely
    retire on an un-run query."""
    return None


# -------------------- schema parsing (pure, total) --------------------

def _require_str(probe: dict[str, Any], key: str) -> Optional[str]:
    """The value at `key` if it is a non-empty string, else None. A missing/blank
    required field is how a malformed probe is routed to INDETERMINATE."""
    val = probe.get(key)
    return val if isinstance(val, str) and val else None


# -------------------- per-kind evaluators (each returns a tri-state) --------------------

# Sentinel distinguishing "no coordinate offered" (None -> use task_id) from
# "a coordinate was offered but is malformed" (-> INDETERMINATE, never a silent
# fallback to a different question).
_COORD_INVALID: tuple[str, int] = ('', -1)


def _parse_pr_coordinate(probe: dict[str, Any]) -> Optional[tuple[str, int]]:
    """(repo, pr_number) when the probe carries a VALID direct PR coordinate,
    else None.

    None means "no coordinate offered" ONLY when BOTH fields are absent — that
    is the case the caller routes to the historical `task_id` path. A PARTIAL or
    malformed coordinate (one field present, a blank repo, a non-positive or
    non-integer number) is a malformed probe, and the caller maps it to
    INDETERMINATE rather than silently falling back to `task_id`: falling back
    would answer a DIFFERENT question than the author asked, which is exactly the
    silent-no-op failure this coordinate support exists to remove.

    `pr_number` accepts an int or an all-digit string (JSON round-trips through
    both). `bool` is explicitly refused even though it is an int subclass in
    Python — `True` is not PR #1."""
    has_any = any(f in probe for f in _PR_COORD_FIELDS)
    if not has_any:
        return None
    repo = _require_str(probe, 'repo')
    if not repo or '/' not in repo:
        return _COORD_INVALID
    raw = probe.get('pr_number')
    if isinstance(raw, bool):
        return _COORD_INVALID
    if isinstance(raw, int):
        number = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        number = int(raw.strip())
    else:
        return _COORD_INVALID
    if number <= 0:
        return _COORD_INVALID
    return repo, number


def _eval_pr_state(
    probe: dict[str, Any],
    pr_state_probe: Callable[[str], str],
    pr_coordinate_probe: Optional[Callable[[str, int], str]] = None,
) -> str:
    """`pr_state`: reuse task_terminal_state rather than re-implementing a gh probe.

    TWO ways to name the work, checked in this order:

    1. A DIRECT coordinate — `repo` (owner/repo) + `pr_number` — resolved via
       `pr_coordinate_probe(repo, number)`. Use this when the premise is about a
       specific PR in a specific repo.
    2. `task_id` — the historical path, resolved via `pr_state_probe(task_id)`,
       which matches the id as a boundary TOKEN against recent PR branches and
       titles.

    WHY (1) EXISTS: the token match in (2) can only find a PR whose branch or
    title CONTAINS the id. A probe naming a PR by coordinate (`pr-<repo>-<num>`
    or any id the PR's branch does not carry) matches nothing, returns UNKNOWN ->
    INDETERMINATE -> keep, FOREVER — a probe that looks authored but can never
    fire. Producers that know the exact PR must be able to say so directly.

    Behaviour is UNCHANGED when neither coordinate field is present: `task_id`
    remains required and is resolved exactly as before.

    Optional in both modes: `expect` in {"open","terminal"} (default "open").

      expect=open     (premise: "work is still live")
        OPEN            -> TRUE  (still live, keep)
        MERGED/CLOSED   -> FALSE (shipped/abandoned, ask is moot)
      expect=terminal (premise: "work has not shipped yet")
        OPEN            -> TRUE  (not yet terminal, keep)
        MERGED/CLOSED   -> FALSE

    UNKNOWN (any gh failure / no match) -> INDETERMINATE in BOTH directions: a
    lookup failure never decides the premise. An unrecognized `expect` value, a
    malformed/partial coordinate, or a missing `task_id` with no coordinate are
    all malformed fields -> INDETERMINATE."""
    expect = probe.get('expect', _PR_EXPECT_OPEN)
    if expect not in (_PR_EXPECT_OPEN, _PR_EXPECT_TERMINAL):
        return INDETERMINATE
    coord = _parse_pr_coordinate(probe)
    if coord is _COORD_INVALID:
        return INDETERMINATE
    try:
        if coord is not None:
            repo, number = coord
            resolve = pr_coordinate_probe or _default_pr_coordinate_probe
            state = resolve(repo, number)
        else:
            task_id = _require_str(probe, 'task_id')
            if not task_id:
                return INDETERMINATE
            state = pr_state_probe(task_id)
    except Exception:  # noqa: BLE001 — a probe error is never a verdict
        return INDETERMINATE
    if not isinstance(state, str):
        # An executor that returned a non-state (e.g. the raw (state, sha) tuple)
        # is ambiguous, never a verdict.
        return INDETERMINATE
    if state == tts.UNKNOWN:
        return INDETERMINATE
    is_terminal = state in tts.TERMINAL_STATES
    if expect == _PR_EXPECT_OPEN:
        # Premise "still live" is FALSE once terminal, TRUE while open.
        return FALSE if is_terminal else TRUE
    # expect == terminal: premise "not yet shipped" is FALSE once terminal.
    return FALSE if is_terminal else TRUE


def _eval_file_substring(
    probe: dict[str, Any], want_present: bool, git_show: Callable[..., Optional[str]],
) -> str:
    """Shared core of `file_contains` (want_present=True) and `file_lacks`
    (want_present=False).

    Required: `repo`, `path`, `substring`. Optional: `ref` (default `origin/main`).
    Reads the file text via `git_show(repo, ref, path)`; None (any git failure) ->
    INDETERMINATE.

      file_contains  premise "substring is present":
        present  -> TRUE
        absent   -> FALSE
      file_lacks     premise "substring is absent":
        absent   -> TRUE
        present  -> FALSE
    """
    repo = _require_str(probe, 'repo')
    path = _require_str(probe, 'path')
    substring = _require_str(probe, 'substring')
    if not (repo and path and substring):
        return INDETERMINATE
    ref = probe.get('ref') or 'origin/main'
    if not isinstance(ref, str):
        return INDETERMINATE
    try:
        text = git_show(repo, ref, path)
    except Exception:  # noqa: BLE001
        return INDETERMINATE
    if text is None:
        return INDETERMINATE
    present = substring in text
    holds = present if want_present else not present
    return TRUE if holds else FALSE


def _resolve_json_path(data: Any, dotted: str) -> tuple[bool, Any]:
    """Walk a dotted key path (`a.b.c`) through nested dicts. Returns
    (found, value). A missing segment or a non-dict mid-walk returns
    (False, None) — the caller maps a miss to INDETERMINATE."""
    cur = data
    for seg in dotted.split('.'):
        if not isinstance(cur, dict) or seg not in cur:
            return False, None
        cur = cur[seg]
    return True, cur


def _eval_json_path(
    probe: dict[str, Any], read_json: Callable[[str], Optional[Any]],
) -> str:
    """`json_path`: read a dotted key from a JSON config file, compare to expected.

    Required: `path`, `key` (dotted), and the `expected` key MUST be present (its
    value may legitimately be null/false/0, so presence — not truthiness — is the
    required-field check). Reads via `read_json(path)`; None (missing/unparseable)
    -> INDETERMINATE. A key absent from the parsed doc -> INDETERMINATE (can't
    decide), NOT FALSE — an absent key is ambiguous, not a proven-dead premise.

      value == expected -> TRUE  (premise holds)
      value != expected -> FALSE (config moved on, ask is moot)
    """
    path = _require_str(probe, 'path')
    key = _require_str(probe, 'key')
    if not (path and key) or 'expected' not in probe:
        return INDETERMINATE
    expected = probe.get('expected')
    try:
        data = read_json(path)
    except Exception:  # noqa: BLE001
        return INDETERMINATE
    if data is None:
        return INDETERMINATE
    found, value = _resolve_json_path(data, key)
    if not found:
        return INDETERMINATE
    return TRUE if value == expected else FALSE


def _eval_sql(
    probe: dict[str, Any], sql_bool: Callable[..., Optional[bool]],
) -> str:
    """`sql`: a read-only query whose boolean-shaped result IS the premise.

    Required: `query`. Optional: `dsn` (connection hint passed through to the
    executor). `sql_bool(query, dsn=...)` returns True/False, or None on ANY
    failure (unbound executor, connection error, timeout, non-boolean shape) ->
    INDETERMINATE. A None result NEVER decides the premise.

      True  -> TRUE  (premise holds)
      False -> FALSE (premise dead)
    """
    query = _require_str(probe, 'query')
    if not query:
        return INDETERMINATE
    dsn = probe.get('dsn')
    if dsn is not None and not isinstance(dsn, str):
        return INDETERMINATE
    try:
        result = sql_bool(query, dsn=dsn)
    except Exception:  # noqa: BLE001
        return INDETERMINATE
    if result is None:
        return INDETERMINATE
    if not isinstance(result, bool):
        # A non-boolean-shaped result is ambiguous, not a verdict.
        return INDETERMINATE
    return TRUE if result else FALSE


# -------------------- the public evaluator --------------------

def evaluate(
    probe: Any,
    *,
    pr_state_probe: Optional[Callable[[str], str]] = None,
    pr_coordinate_probe: Optional[Callable[[str, int], str]] = None,
    git_show: Optional[Callable[..., Optional[str]]] = None,
    read_json: Optional[Callable[[str], Optional[Any]]] = None,
    sql_bool: Optional[Callable[..., Optional[bool]]] = None,
) -> str:
    """Evaluate a freshness_probe and return one of TRUE | FALSE | INDETERMINATE.

    TRUE  => premise still holds => KEEP the card.
    FALSE => premise dead        => the ask is moot.
    INDETERMINATE => couldn't decide => every caller MUST treat as KEEP.

    EVERY failure or ambiguity path returns INDETERMINATE: a non-dict probe, a
    missing/unknown `kind`, a missing required field, an unparseable value, or any
    executor error/timeout. This is a total function — it never raises.

    The side-effecting executors are injectable so this stays pure and unit
    testable; production defaults do the real bounded I/O and each fails toward
    INDETERMINATE:
      pr_state_probe: task_id -> task_terminal_state constant (default
                      task_terminal_state.task_terminal_state).
      pr_coordinate_probe: (repo, pr_number) -> task_terminal_state constant, for
                      a pr_state probe naming a PR DIRECTLY (default
                      task_terminal_state.pr_coordinate_state, state only).
      git_show:       (repo, ref, path) -> file text or None (default git show).
      read_json:      path -> parsed JSON or None (default file read).
      sql_bool:       (query, dsn=...) -> bool or None (default inert None; this
                      slice ships no live DB binding).
    """
    if not isinstance(probe, dict):
        return INDETERMINATE
    kind = probe.get('kind')
    if not isinstance(kind, str) or kind not in SUPPORTED_KINDS:
        return INDETERMINATE

    pr_state_probe = pr_state_probe or (lambda tid: tts.task_terminal_state(tid))
    git_show = git_show or _default_git_show
    read_json = read_json or _default_read_json
    sql_bool = sql_bool or _default_sql_bool

    try:
        if kind == 'pr_state':
            return _eval_pr_state(probe, pr_state_probe, pr_coordinate_probe)
        if kind == 'file_contains':
            return _eval_file_substring(probe, True, git_show)
        if kind == 'file_lacks':
            return _eval_file_substring(probe, False, git_show)
        if kind == 'json_path':
            return _eval_json_path(probe, read_json)
        if kind == 'sql':
            return _eval_sql(probe, sql_bool)
    except Exception:  # noqa: BLE001 — the outer net: no probe error is a verdict
        return INDETERMINATE
    return INDETERMINATE  # unreachable (kind is gated by SUPPORTED_KINDS)
