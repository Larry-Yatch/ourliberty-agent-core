#!/usr/bin/env python3
"""projects_brainstorm_author.py — the Projects-tab-v3 P6 brainstorm autofill
author (spec `agents/beacon/specs/projects-v3-p6-brainstorm-autofill.md`, step
p6-brainstorm-autofill-author).

End state: *every Brainstorm phase arrives pre-filled.* When a phase is in the
Brainstorm lifecycle state, this pass authors — onto the phase **card** — a draft
of the 8-section brainstorm template (docs/templates/project-brainstorm-template.md
§0–§7), a capped **"Your decisions" list (≤5)** of the genuine forks that are
Larry's to call, and the **handoff-prompt payload fields** the dashboard
(p6-brainstorm-card-ui) deterministically assembles into the "Copy handoff for
Claude" prompt. Inputs: the funnel/candidate briefing carried on the project +
phase (title + desired end state), the project North Star, and the prior phase's
P4 closeout (the feed-forward P4 already pre-loads).

This is the **Narrator extended to the brainstorm** (spec § 3 "Reuse"): exactly
as P4 extended it to the closeout. Same authoring engine (`missions_narrator`'s
single guarded `claude` round-trip + tolerant JSON parse), same store location +
write discipline (the projects GC committer `heal_projects_store` drains an
in-registry mutation — the SAME pattern `missions_narrator.author_captures_in_
registry` uses, so this is never a second committer of projects.json).

INVARIANTS (Mirror focus, spec § 3 / § 4):

  - SINGLE-COMMITTER. The author + the sweep are PURE w.r.t. git: the sweep
    mutates the in-memory registry the projects GC committer already holds, via
    the pure `projects_store.attach_phase_brainstorm`; the SOLE git committer
    stays `heal_projects_store.py`. There is no second writer to the store.

  - DECISIONS CAPPED ≤5 (spec § 4 guardrail). `_cap_decisions` enforces the cap
    DETERMINISTICALLY (the value is *fewer* decisions, not a new checklist); when
    the model surfaces more, the extras are dropped and `decisions_overflow` is
    set so the card can note "more will surface in the Claude session."

  - DETERMINISTIC FALLBACK / NO RAW-METADATA LEAK (spec § 4.3 thin-context). The
    LLM authors only prose (the 8 draft sections) + the decisions list; a
    malformed or absent reply falls through to `render_raw_brainstorm` +
    `render_raw_decisions`, which render neutral plain English referencing only
    the human-facing phase/project titles + desired end state — NEVER task ids,
    branches, seq ids, spec/North-Star text echoed raw, or any other metadata.
    The handoff payload is assembled DETERMINISTICALLY (never LLM-authored).

  - v1 BOUNDARY (spec § 2). The author stops at draft + decisions + handoff
    payload. No spec is authored; nothing launches. Larry (with Claude) still
    refines and marks Ready-to-spec.

stdlib + `missions_narrator` (the `claude` CLI is optional; deterministic
fallback when absent). No network of its own — the prior closeout + briefing come
from the registry already in hand; the North Star is a best-effort local read.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Sibling scripts/ on path so imports resolve under systemd and in tests.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import missions_narrator as narrator  # noqa: E402 — reuse the Narrator round-trip
import projects_store  # noqa: E402 — pure schema + the attach helper

# The brainstorm author shares the Narrator's voice + model (single-voice; the
# meaning layer is worth the better model — see missions_narrator / the closeout).
BRAINSTORM_BY = narrator.NARRATOR_BY
BRAINSTORM_MODEL = narrator.NARRATOR_MODEL

# The 8 sections of the brainstorm template (docs/templates/project-brainstorm-
# template.md §0–§7), in order. These are the ONLY LLM-authored prose keys.
BRAINSTORM_SECTION_KEYS: tuple[str, ...] = (
    'end_state',          # §0 Desired End State (the lead)
    'why_now',            # §1 Why now
    'scope',              # §2 Scope & non-goals
    'constraints_reuse',  # §3 Constraints & reuse
    'options_decision',   # §4 Options & the decision
    'risks_guardrails',   # §5 Risks & guardrails
    'done_gate',          # §6 Done-gate
    'breakdown',          # §7 Breakdown (the proposed phase/step breakdown)
)

# Cap the "Your decisions" list (spec § 4 guardrail): the value is FEWER
# decisions, not a new checklist. Enforced deterministically so a runaway model
# reply can never flood the card.
MAX_DECISIONS = 5

# Bound the LLM round-trips one sweep can spend (mirrors the Narrator's
# NARRATOR_MAX_PER_TICK; the remainder authors next tick — needs_brainstorm is
# idempotent so the sweep and any event path converge).
BRAINSTORM_MAX_PER_TICK = getattr(narrator, 'NARRATOR_MAX_PER_TICK', 8)

# The spec target the handoff directs Claude at (spec § 3 / § 5 done-gate).
_SPEC_DIR = 'agents/beacon/specs'
_DEFAULT_REPO = 'ourliberty-agent-core'

# Bound the North Star excerpt so it can't blow the prompt (mirrors the closeout).
_EXCERPT_CHARS = 4000


def _log(msg: str) -> None:
    """Reuse the Narrator/GC log sink so brainstorm lines land with the rest of
    the projects authoring trail."""
    narrator.log(f'brainstorm: {msg}')


# --------------------------------------------------------------------------- #
# idempotency guard — which phases need a brainstorm draft authored
# --------------------------------------------------------------------------- #
def needs_brainstorm(phase: dict[str, Any]) -> bool:
    """True iff ``phase`` needs a brainstorm draft authored — it is in the
    ``brainstorm`` lifecycle state and has no draft yet (or its provenance was
    stamped from a different state). Idempotent: a phase already drafted for its
    current state returns False, so the periodic sweep never re-authors an
    unchanged card (mirrors ``missions_narrator.needs_briefing``). A phase past
    brainstorm (spec/building/done) is never (re)drafted — its draft is frozen
    at the moment Larry moved it forward."""
    if not isinstance(phase, dict):
        return False
    if phase.get('lifecycle_state') != 'brainstorm':
        return False
    if not isinstance(phase.get('brainstorm'), dict):
        return True
    prov = phase.get('brainstorm_provenance')
    if not isinstance(prov, dict):
        return True
    return prov.get('from_state') != phase.get('lifecycle_state')


# --------------------------------------------------------------------------- #
# context gathering (the source stream the draft reads — all fail-safe)
# --------------------------------------------------------------------------- #
def _read_doc_excerpt(repo: Optional[Path], ref: Optional[str]) -> Optional[str]:
    """Best-effort read of a repo-relative doc (the project ``north_star_ref``),
    bounded to ``_EXCERPT_CHARS``. A ``#anchor`` suffix is stripped. Any failure
    (no repo, missing file, read error) yields None — the draft degrades to less
    context, never crashes. Mirrors ``projects_closeout_author._read_doc_excerpt``."""
    if not ref or not isinstance(ref, str):
        return None
    path_part = ref.split('#', 1)[0].strip()
    if not path_part:
        return None
    p = Path(path_part)
    if not p.is_absolute():
        if repo is None:
            return None
        p = repo / path_part
    try:
        text = p.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    return text[:_EXCERPT_CHARS]


def _phase_sort_key(phase: dict[str, Any], idx: int) -> tuple[int, int]:
    order = phase.get('order')
    return (order if isinstance(order, int) else idx, idx)


def prior_closeout_for_phase(
    project: dict[str, Any], phase: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """The authored ``closeout`` of the phase immediately BEFORE ``phase`` (by
    ``order``, ties by list position) that carries one — the P4 feed-forward
    input (spec § 1 / § 3). Returns that closeout dict, or None for the first
    phase / a project whose earlier phases have no closeout. Pure; safe on junk."""
    if not isinstance(project, dict) or not isinstance(phase, dict):
        return None
    phases = [p for p in (project.get('phases') or []) if isinstance(p, dict)]
    cur_id = phase.get('id')
    indexed = list(enumerate(phases))
    cur = next(((i, p) for i, p in indexed if p.get('id') == cur_id), None)
    if cur is None:
        return None
    cur_key = _phase_sort_key(cur[1], cur[0])
    # Earlier phases, latest-first, first one carrying a closeout wins.
    earlier = sorted(
        (ip for ip in indexed if _phase_sort_key(ip[1], ip[0]) < cur_key),
        key=lambda ip: _phase_sort_key(ip[1], ip[0]), reverse=True)
    for _, p in earlier:
        co = p.get('closeout')
        if isinstance(co, dict) and co:
            return co
    return None


def gather_brainstorm_context(
    phase: dict[str, Any],
    project: dict[str, Any],
    *,
    repo: Optional[Path] = None,
) -> dict[str, Any]:
    """Gather the source stream the draft reads: the funnel/candidate briefing
    carried on the project + phase (titles + desired end state), the project
    North Star excerpt (best-effort local read), and the prior phase's P4
    closeout. Pure over its inputs; the only IO is the fail-safe North Star read."""
    return {
        'project_title': project.get('title') if isinstance(project, dict) else None,
        'phase_title': phase.get('title') if isinstance(phase, dict) else None,
        'phase_desired_end_state': (
            phase.get('desired_end_state') if isinstance(phase, dict) else None),
        'north_star_excerpt': _read_doc_excerpt(
            repo, project.get('north_star_ref') if isinstance(project, dict) else None),
        'prior_closeout': prior_closeout_for_phase(project, phase),
    }


# --------------------------------------------------------------------------- #
# deterministic fallback draft + decisions (no raw-metadata leak)
# --------------------------------------------------------------------------- #
def render_raw_brainstorm(
    phase: dict[str, Any],
    project: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, str]:
    """Deterministic plain-English brainstorm draft — the fallback when the LLM
    voice is unavailable, and the graceful thin-context degrade (spec § 4.3). Uses
    ONLY operator language: the human-facing phase/project titles + the phase's
    Desired End State; NEVER task ids, branches, seq ids, or spec / North-Star
    text echoed raw (the no-raw-metadata guard). It never claims detail it does
    not have — it names the open work plainly so Larry + Claude finish it."""
    title = (phase.get('title') or phase.get('id') or 'this phase').strip()
    project_title = (project.get('title') or '').strip()
    des = (phase.get('desired_end_state') or '').strip()
    has_prior = isinstance(context.get('prior_closeout'), dict) and bool(
        context.get('prior_closeout'))
    has_north_star = bool(context.get('north_star_excerpt'))

    in_project = f' in "{project_title}"' if project_title else ''
    north_star_clause = (
        ' Pull the framing from the project North Star.' if has_north_star else '')
    prior_clause = (
        ' The previous phase\'s closeout is the starting point — build on what it '
        'shipped and learned.' if has_prior else '')

    end_state = des or (
        f'When "{title}" is done, its goal is met and the result is live and '
        'usable. Fill in the one-sentence destination in your own words.')
    return {
        'end_state': end_state,
        'why_now': (
            f'"{title}"{in_project} is the next step worth taking now.'
            f'{prior_clause} Confirm what is forcing it — the pain or the cost of '
            'waiting.'),
        'scope': (
            'Draft what is explicitly IN this phase and, just as important, what '
            'is OUT so the build does not sprawl. Keep it to this phase\'s end '
            'state above.'),
        'constraints_reuse': (
            'Check the shelf / graph first — this is assembly, not greenfield. '
            f'Note what already exists to reuse and what must not break.'
            f'{north_star_clause}'),
        'options_decision': (
            'Lay out the real forks for this phase, each with its one-line '
            'trade-off, and end with a recommendation — not a menu. This is where '
            'your input matters most (see "Your decisions").'),
        'risks_guardrails': (
            'Name what could go wrong and the guardrail that contains each — '
            'especially anything irreversible or outward-facing.'),
        'done_gate': (
            'The checkable form of the end state: how we will know "'
            f'{title}" is actually done, plus the tests that prove it.'),
        'breakdown': (
            'Proposed breakdown: the steps that become the spec + the DAG build '
            'sequence. A small phase collapses to a single step.'),
    }


def render_raw_decisions(
    phase: dict[str, Any],
    project: dict[str, Any],
    context: dict[str, Any],
) -> list[str]:
    """Deterministic "Your decisions" list — the thin-context fallback (spec
    § 4.3: a sparse phase still gets a usable decisions list). Generic, genuine
    forks only; ≤ MAX_DECISIONS; NO metadata. These are the questions a team
    cannot assume for Larry on any phase."""
    return [
        'Scope: anything important being left out, or in that should be cut?',
        'Approach: is the proposed direction right, or is there a fork worth taking?',
        'Risk tolerance: how much polish vs. speed for this phase?',
    ]


# --------------------------------------------------------------------------- #
# LLM authoring prompt + parse
# --------------------------------------------------------------------------- #
def build_brainstorm_prompt(
    phase: dict[str, Any],
    project: dict[str, Any],
    context: dict[str, Any],
) -> str:
    """Authoring prompt for the pre-filled brainstorm (mirrors the closeout
    author's prompt shape). Beacon voice, plain language, JSON-out for parse.
    Passes the briefing (titles + end state) + North Star excerpt + prior
    closeout so the model drafts what it reasonably can — and is told to mark it
    an AI draft and to say so plainly rather than invent detail when context is
    thin."""
    prior = context.get('prior_closeout')
    facts = {
        'project_title': context.get('project_title'),
        'phase_title': context.get('phase_title'),
        'phase_desired_end_state': context.get('phase_desired_end_state'),
        'north_star_excerpt': context.get('north_star_excerpt'),
        'prior_phase_closeout': prior if isinstance(prior, dict) else None,
    }
    return (
        "You are Beacon, the operator's manager. A new phase of a project just "
        "entered Brainstorm. Pre-fill the team's 8-section brainstorm template so "
        "the operator drops into a draft instead of a blank page — he then edits "
        "freely and answers only the handful of decisions that are genuinely his. "
        "This is an AI DRAFT to edit, never a finished doc.\n\n"
        "Draft everything you reasonably can FROM THE FACTS BELOW. Where the facts "
        "are thin, write a short, honest placeholder that names the open question "
        "— do NOT invent specifics, numbers, or commitments.\n\n"
        "Return ONLY a JSON object with exactly these keys:\n"
        '  "end_state":         §0 — plain-language destination: when this phase '
        "is done, what is true.\n"
        '  "why_now":           §1 — what is forcing this phase now.\n'
        '  "scope":             §2 — what is explicitly IN and OUT.\n'
        '  "constraints_reuse": §3 — what to reuse (assembly, not greenfield) and '
        "what must not break.\n"
        '  "options_decision":  §4 — the real forks, each with its trade-off, '
        "ending with a recommendation.\n"
        '  "risks_guardrails":  §5 — what could go wrong and the guardrail for '
        "each.\n"
        '  "done_gate":         §6 — the checkable form of §0 (how we know it is '
        "done) + tests.\n"
        '  "breakdown":         §7 — the proposed steps/phase breakdown that '
        "become the spec + DAG.\n"
        '  "decisions":         a list of the GENUINE choices that are the '
        "operator's to make (scope trade-offs, risk tolerance, priorities, key "
        f"approach forks). AT MOST {MAX_DECISIONS}, fewest that matter — this is "
        "the value: fewer decisions, not a new checklist. Each one short, plain "
        "English.\n\n"
        "Every value for the 8 section keys must be a non-empty plain-English "
        "string (no markdown headers). Ground every statement in the facts; never "
        "reference task ids, branches, commits, agents, sequence ids, or risk "
        "codes.\n\n"
        "Here are the facts (JSON):\n"
        f"{json.dumps(facts, indent=2, default=str)}\n\n"
        "Write the pre-filled brainstorm now. Output ONLY the JSON object."
    )


def _parse_llm_brainstorm(
    raw: Optional[dict[str, Any]],
) -> tuple[Optional[dict[str, str]], Optional[list[str]]]:
    """Coerce a raw (LLM or absent) reply into ``(draft, decisions)``. ``draft``
    is the 8-section dict ONLY when EVERY section key is a non-empty string
    (all-or-nothing, mirroring ``generate_briefing_voice``) — else None so the
    caller renders the deterministic raw draft. ``decisions`` is the cleaned list
    of non-empty strings, or None when absent/garbage (caller falls back).
    Independent: a good decisions list survives an incomplete draft and vice
    versa."""
    if not isinstance(raw, dict):
        return None, None
    draft: dict[str, str] = {}
    for k in BRAINSTORM_SECTION_KEYS:
        v = raw.get(k)
        if not (isinstance(v, str) and v.strip()):
            draft = {}
            break
        draft[k] = v.strip()
    parsed_draft = draft or None

    decisions_raw = raw.get('decisions')
    parsed_decisions: Optional[list[str]] = None
    if isinstance(decisions_raw, list):
        cleaned = [d.strip() for d in decisions_raw
                   if isinstance(d, str) and d.strip()]
        parsed_decisions = cleaned or None
    return parsed_draft, parsed_decisions


def _cap_decisions(decisions: list[str]) -> tuple[list[str], bool]:
    """Dedupe (order-preserving) + cap the decisions list at ``MAX_DECISIONS``
    DETERMINISTICALLY. Returns ``(capped, overflow)`` where ``overflow`` is True
    iff more genuine decisions existed than the cap — the card uses it to note
    that more will surface in the Claude session (spec § 4 guardrail)."""
    seen: set[str] = set()
    out: list[str] = []
    for d in decisions:
        if not (isinstance(d, str) and d.strip()):
            continue
        d = d.strip()
        if d in seen:
            continue
        seen.add(d)
        out.append(d)
    overflow = len(out) > MAX_DECISIONS
    return out[:MAX_DECISIONS], overflow


# --------------------------------------------------------------------------- #
# deterministic handoff payload (never LLM-authored — spec § 2 "no double-call")
# --------------------------------------------------------------------------- #
def build_handoff_payload(
    phase: dict[str, Any],
    project: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """The handoff-prompt payload FIELDS the dashboard (p6-brainstorm-card-ui)
    assembles deterministically into the "Copy handoff for Claude" prompt — no
    model call here or on the button. Carries the exact spec target
    (``agents/beacon/specs/<phase_id>.md`` + repo), the North Star ref, the prior
    closeout summary (so a fresh Claude session has the feed-forward context), and
    the standard directive. Self-contained per spec § 4 handoff-completeness."""
    phase_id = (phase.get('id') or '').strip() if isinstance(phase, dict) else ''
    repo = ''
    if isinstance(project, dict):
        repo = (project.get('repo') or '').strip()
    repo = repo or _DEFAULT_REPO
    spec_target_path = f'{_SPEC_DIR}/{phase_id}.md' if phase_id else None

    prior = context.get('prior_closeout')
    prior_summary = None
    if isinstance(prior, dict):
        s = prior.get('summary')
        if isinstance(s, str) and s.strip():
            prior_summary = s.strip()

    if spec_target_path:
        directive = (
            'Work with Larry to finalize this brainstorm and author the spec at '
            f'{spec_target_path} in {repo}; he will mark it Ready to spec.')
    else:
        directive = (
            'Work with Larry to finalize this brainstorm and author the spec; he '
            'will mark it Ready to spec.')

    return {
        'spec_target_path': spec_target_path,
        'target_repo': repo,
        'north_star_ref': (
            project.get('north_star_ref') if isinstance(project, dict) else None),
        'prior_closeout_summary': prior_summary,
        'directive': directive,
    }


# --------------------------------------------------------------------------- #
# the pure author
# --------------------------------------------------------------------------- #
def author_phase_brainstorm(
    phase: dict[str, Any],
    project: dict[str, Any],
    context: Optional[dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Author the full brainstorm for one Brainstorm-state phase and return the
    field dict (``brainstorm`` + ``brainstorm_provenance``) for the caller to
    merge onto the phase card. PURE — does not mutate inputs or touch disk; the
    caller owns the single write + commit (single-committer invariant).

    Schema (spec § 0 / § 2): ``brainstorm`` = ``is_ai_draft`` (always True — it is
    a draft to edit) + ``draft`` (the 8 template sections) + ``decisions`` (the
    capped ≤5 list) + ``decisions_overflow`` (bool) + ``handoff`` (the deterministic
    payload). ``use_llm=True`` (production) tries the claude voice and falls back
    to the deterministic raw draft + decisions on ANY failure, so the author never
    raises and a malformed reply renders a usable lighter draft rather than raw
    metadata (§4.3 thin-context gate). The handoff payload is ALWAYS deterministic."""
    context = context or {}
    now = now or datetime.now(timezone.utc)

    draft = None
    decisions = None
    if use_llm:
        raw = narrator.claude_json_roundtrip(
            build_brainstorm_prompt(phase, project, context),
            model=BRAINSTORM_MODEL)
        draft, decisions = _parse_llm_brainstorm(raw)
    authored_by_llm = draft is not None

    if draft is None:
        draft = render_raw_brainstorm(phase, project, context)
    if decisions is None:
        decisions = render_raw_decisions(phase, project, context)

    capped, overflow = _cap_decisions(decisions)
    brainstorm = {
        'is_ai_draft': True,
        'draft': draft,
        'decisions': capped,
        'decisions_overflow': overflow,
        'handoff': build_handoff_payload(phase, project, context),
    }
    return {
        'brainstorm': brainstorm,
        'brainstorm_provenance': {
            'by': BRAINSTORM_BY,
            'model': BRAINSTORM_MODEL if authored_by_llm else 'raw',
            'at': now.isoformat(),
            'from_state': phase.get('lifecycle_state'),
        },
    }


# --------------------------------------------------------------------------- #
# the in-registry sweep (drained by the projects GC committer)
# --------------------------------------------------------------------------- #
def _resolve_repo() -> Optional[Path]:
    """Best-effort agent-core repo path (for the North Star excerpt read). Any
    failure (config absent / import error) yields None — the draft degrades to
    no North Star context, never raises. Never the committer."""
    try:
        import heal_projects_store as _committer  # local import: optional dep
        return _committer.load_repo_paths().get('ourliberty-agent-core')
    except Exception as e:  # noqa: BLE001 — repo resolution is best-effort
        _log(f'repo resolution raised {type(e).__name__}: {e}; no North Star context')
        return None


def author_brainstorms_in_registry(
    registry: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    use_llm: bool = True,
    repo: Optional[Path] = None,
    max_per_tick: int = BRAINSTORM_MAX_PER_TICK,
) -> tuple[int, int]:
    """Author the brainstorm draft onto every Brainstorm-state phase in
    ``registry`` that needs one, bounded by ``max_per_tick``. Mutates the registry
    IN PLACE — the caller (the projects GC committer ``heal_projects_store``) owns
    the single atomic write + git commit, so this never becomes a second writer of
    projects.json (single-committer invariant; the SAME pattern as
    ``missions_narrator.author_captures_in_registry``).

    Returns ``(authored, deferred)``: how many phases were drafted this tick and
    how many still need one afterward (the remainder authors next tick;
    needs_brainstorm is idempotent). Fail-safe per phase: an author error on one
    phase is logged and skipped — never aborts the sweep, never corrupts the
    registry (the deterministic fallback already guarantees a usable draft)."""
    now = now or datetime.now(timezone.utc)
    projects = registry.get('projects') if isinstance(registry, dict) else None
    if not isinstance(projects, list):
        return (0, 0)
    if repo is None:
        repo = _resolve_repo()

    pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        for phase in (proj.get('phases') or []):
            if isinstance(phase, dict) and needs_brainstorm(phase):
                pending.append((proj, phase))

    attempted = 0
    authored = 0
    for proj, phase in pending:
        if attempted >= max_per_tick:
            break
        attempted += 1
        try:
            context = gather_brainstorm_context(phase, proj, repo=repo)
            fields = author_phase_brainstorm(
                phase, proj, context, now=now, use_llm=use_llm)
        except Exception as e:  # noqa: BLE001 — per-phase fail-safe
            _log(f'author failed for phase {phase.get("id")!r}: '
                 f'{type(e).__name__}: {e} — skipped (retries next tick)')
            continue
        if projects_store.attach_phase_brainstorm(phase, fields, now=now):
            authored += 1

    deferred = max(0, len(pending) - authored)
    if pending:
        _log(f'sweep: authored {authored} brainstorm draft(s); deferred {deferred} '
             f'(max {max_per_tick}/tick)')
    return (authored, deferred)
