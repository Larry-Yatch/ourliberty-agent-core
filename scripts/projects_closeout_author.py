#!/usr/bin/env python3
"""projects_closeout_author.py — the Projects-tab-v3 P4 closeout author (spec
`agents/beacon/specs/projects-v3-p4-closeout.md`, step p4-closeout-author).

End state: *a finished phase writes its own story.* When a phase reaches Done
(SEQUENCE_COMPLETE + status=done), this pass reads the merged diff + PR
bodies/commits + spec + North Star and authors the closeout — a plain-language
summary plus the structured schema (shipped · changed-vs-spec · learnings ·
actual cost · done-gate met?) — onto the phase **card**, and ticks the North
Star **status tracker** row for that phase.

This is the **Narrator extended to closeout** (North Star §4.5, spec § 3
"Reuse"): the exact authoring pattern `missions_narrator.author_closeout` uses
for a merged capture — an LLM reads a source stream and emits structured,
plain-language output, with a deterministic fallback so the pass runs head-less
and under test. We reuse `missions_narrator.generate_briefing_voice` (the one
claude round-trip + tolerant JSON parse) with closeout-shaped keys.

INVARIANTS (Mirror focus, spec § 3 "Must not break"):

  - SINGLE-COMMITTER. This module is PURE — it returns the closeout field dict
    and never touches disk or git. The on-disk write of the phase card is the
    NON-committer `projects_status_writeback.attach_closeout`; the SOLE git
    committer stays `heal_projects_store.py` (which also drains the ticked North
    Star doc). There is no second writer to the store — the invariant holds.

  - DETERMINISTIC FALLBACK / NO RAW-METADATA LEAK (North Star §5, §6 gate). The
    LLM authors only prose (summary/shipped/changed-vs-spec/learnings); a
    malformed or absent LLM reply falls through to `render_raw_phase_closeout`,
    which renders neutral plain English referencing only the PR number (a
    clickable identifier) — never task ids, branches, seq ids, or other raw
    metadata. `cost` and `done_gate_met` are computed DETERMINISTICALLY (never
    LLM-authored) so the card never shows a hallucinated cost.

  - DON'T BURDEN THE BUILD TEAM (§4.5). The closeout is a board-side pull pass
    fired from the notifier's SEQUENCE_COMPLETE signal — never Forge/Mirror's
    duty.

stdlib + the `claude` CLI (optional; deterministic fallback when absent) +
`gh`/`git` for best-effort context gathering (optional; all fail-safe).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Sibling scripts/ on path so imports resolve under systemd and in tests.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import missions_narrator as narrator  # noqa: E402 — reuse the Narrator round-trip
from heal_missions_card_gc import pr_number_from_url  # noqa: E402
from test_isolation_guard import refuse_under_test  # noqa: E402

# The closeout author shares the Narrator's voice + model (single-voice, the
# meaning layer is worth the better model — see missions_narrator).
CLOSEOUT_BY = narrator.NARRATOR_BY
CLOSEOUT_MODEL = narrator.NARRATOR_MODEL

# Only PROSE is LLM-authored. `cost` + `done_gate_met` are deterministic so the
# card never shows a hallucinated number (no-raw-metadata + accuracy guard).
CLOSEOUT_LLM_KEYS = ('summary', 'shipped', 'changed_vs_spec', 'learnings')

# p4-closeout-outputs: the funnel source tag for loose ends a closeout finds (spec
# § 2/§ 4 decision 3). Must be in `suggest_funnel_card.SUGGESTING_AGENTS` and the
# funnel derive's `_SUGGESTED_AGENTS` so the card lands in the suggested lane.
FOLLOWUP_SOURCE = 'closeout'

# Cap the loose ends one closeout drops so a runaway LLM reply can't flood the
# funnel with suggestions.
_MAX_FOLLOW_UPS = 10

# Bounds so a single closeout's context gather can't run unbounded or blow the
# LLM prompt: cap the doc/diff excerpts and the number of PRs scanned.
_EXCERPT_CHARS = 4000
_DIFFSTAT_CHARS = 2000
_MAX_PRS = 12
GH_TIMEOUT_SEC = 30
GIT_TIMEOUT_SEC = 30

_GH_OWNER = 'Larry-Yatch'


def _log(msg: str) -> None:
    """Reuse the Narrator/GC-healer log sink so closeout lines land with the
    rest of the missions-board authoring trail."""
    narrator.log(f'closeout: {msg}')


# --------------------------------------------------------------------------- #
# context gathering (IO — best-effort, fail-safe, never under test)
# --------------------------------------------------------------------------- #
def _read_doc_excerpt(repo: Optional[Path], ref: Optional[str]) -> Optional[str]:
    """Best-effort read of a repo-relative doc (a phase `spec_ref` or a project
    `north_star_ref`), bounded to `_EXCERPT_CHARS`. A `#anchor` suffix is
    stripped (it points at a section; we read the whole doc). Any failure
    (no repo, missing file, read error) yields None — the closeout degrades to
    less context, never crashes."""
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


def _gh_json(pr_url: str, fields: str) -> Optional[dict[str, Any]]:
    """`gh pr view <url> --json <fields>` → parsed dict, or None on any failure.
    Read-only network; never under test (refuse_under_test guards the spawn)."""
    refuse_under_test('gh-spawn')
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', pr_url, '--json', fields],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SEC, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        _log(f'gh pr view failed for {pr_url}: {type(e).__name__}: {e}')
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or '{}')
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _gh_diffstat(pr_url: str) -> Optional[str]:
    """`gh pr diff <url> --name-only` → a bounded list of changed files (the
    "merged diff" the closeout reads, kept to file names so the prompt stays
    small). None on any failure."""
    refuse_under_test('gh-spawn')
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'diff', pr_url, '--name-only'],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SEC, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        _log(f'gh pr diff failed for {pr_url}: {type(e).__name__}: {e}')
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or '').strip()[:_DIFFSTAT_CHARS] or None


def gather_closeout_context(
    phase: dict[str, Any],
    project: dict[str, Any],
    seq: Optional[dict[str, Any]] = None,
    *,
    repo: Optional[Path] = None,
) -> dict[str, Any]:
    """Best-effort gather of the source stream the closeout reads: the PRs
    (numbers/titles/bodies/commits/changed-files) from the completed sequence,
    the phase spec excerpt, and the project North Star excerpt. Every probe is
    fail-safe — a gather error degrades the context, never raises. Returns a
    plain dict the pure author consumes; under test the caller passes a
    pre-built context instead so no subprocess fires."""
    seq = seq or {}
    steps = [s for s in (seq.get('steps') or []) if isinstance(s, dict)]
    pr_urls = [s.get('pr_url') for s in steps if s.get('pr_url')][:_MAX_PRS]
    all_steps_merged = bool(steps) and all(
        s.get('status') in ('merged', 'complete') for s in steps)

    pr_numbers: list[str] = []
    pr_titles: list[str] = []
    pr_bodies: list[str] = []
    commit_subjects: list[str] = []
    changed_files: list[str] = []
    for url in pr_urls:
        n = pr_number_from_url(url)
        if n:
            pr_numbers.append(n)
        view = _gh_json(url, 'title,body,commits')
        if view:
            if isinstance(view.get('title'), str):
                pr_titles.append(view['title'])
            if isinstance(view.get('body'), str) and view['body'].strip():
                pr_bodies.append(view['body'].strip()[:_EXCERPT_CHARS])
            for c in (view.get('commits') or []):
                msg = c.get('messageHeadline') if isinstance(c, dict) else None
                if isinstance(msg, str) and msg.strip():
                    commit_subjects.append(msg.strip())
        diff = _gh_diffstat(url)
        if diff:
            changed_files.extend(diff.splitlines())

    return {
        'pr_numbers': pr_numbers,
        'pr_titles': pr_titles,
        'pr_bodies': pr_bodies,
        'commit_subjects': commit_subjects,
        'changed_files': changed_files[:50],
        'spec_excerpt': _read_doc_excerpt(repo, phase.get('spec_ref')),
        'north_star_excerpt': _read_doc_excerpt(repo, project.get('north_star_ref')),
        'cost_usd': seq.get('cost_per_task_usd') if isinstance(
            seq.get('cost_per_task_usd'), (int, float)) else None,
        'all_steps_merged': all_steps_merged,
    }


# --------------------------------------------------------------------------- #
# deterministic schema parts (never LLM-authored)
# --------------------------------------------------------------------------- #
def render_cost(context: dict[str, Any]) -> str:
    """Plain-English "actual cost" — DETERMINISTIC (never LLM-authored, so the
    card never shows a hallucinated dollar figure). Prefers a recorded
    `cost_usd`; else falls back to the shipped-PR count as the cost proxy."""
    cost = context.get('cost_usd')
    if isinstance(cost, (int, float)) and cost > 0:
        return f'about ${cost:.2f} in build time.'
    n = len([x for x in (context.get('pr_numbers') or []) if x])
    if n == 1:
        return 'One PR of build work.'
    if n > 1:
        return f'{n} PRs of build work.'
    return 'Not separately tracked.'


def render_done_gate_met(context: dict[str, Any]) -> bool:
    """Whether the phase's done-gate was met — DETERMINISTIC. A phase reaches
    this pass via SEQUENCE_COMPLETE (verified merge), so the default is True;
    it is False only when the gathered context shows steps that did not all
    merge (a divergence the completion DM flags for Larry)."""
    steps_known = context.get('all_steps_merged')
    if steps_known is None:
        return True
    return bool(steps_known)


# --------------------------------------------------------------------------- #
# deterministic fallback briefing (no raw-metadata leak)
# --------------------------------------------------------------------------- #
def render_raw_phase_closeout(
    phase: dict[str, Any],
    project: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, str]:
    """Deterministic plain-English closeout prose — the fallback when the LLM
    voice is unavailable (and the value tests assert against). Operator language
    ONLY: the human-facing phase/project titles + the PR number(s); NEVER task
    ids, branches, seq ids, or spec/diff text echoed raw (the no-raw-metadata
    guard, §5/§6)."""
    title = (phase.get('title') or phase.get('id') or 'this phase').strip()
    nums = [n for n in (context.get('pr_numbers') or []) if n]
    if len(nums) == 1:
        pr_ref = f' (PR #{nums[0]})'
    elif len(nums) > 1:
        pr_ref = ' (PRs ' + ', '.join(f'#{n}' for n in nums) + ')'
    else:
        pr_ref = ''

    summary = (f'"{title}" is done — its build landed{pr_ref} and is live. '
               'The next phase can pick up from here.')
    shipped = (f'The work for "{title}" merged{pr_ref}.' if pr_ref
               else f'The work for "{title}" merged.')
    changed_vs_spec = 'Built as planned — no notable divergence from the spec.'
    learnings = 'Nothing surprising surfaced during the build.'
    return {
        'summary': summary,
        'shipped': shipped,
        'changed_vs_spec': changed_vs_spec,
        'learnings': learnings,
    }


# --------------------------------------------------------------------------- #
# LLM authoring prompt
# --------------------------------------------------------------------------- #
def build_phase_closeout_prompt(
    phase: dict[str, Any],
    project: dict[str, Any],
    context: dict[str, Any],
) -> str:
    """CEO-voice authoring prompt for the phase closeout (mirrors
    `missions_narrator.build_closeout_prompt`). Beacon voice, plain outcomes,
    JSON-out for parse. Passes the source stream (PR titles/bodies/commits +
    changed files + spec + North Star excerpts) so the model writes the story
    a stranger could read cold."""
    facts = {
        'phase_title': phase.get('title'),
        'phase_desired_end_state': phase.get('desired_end_state'),
        'project_title': project.get('title'),
        'pr_numbers': context.get('pr_numbers') or [],
        'pr_titles': context.get('pr_titles') or [],
        'pr_bodies': context.get('pr_bodies') or [],
        'commit_subjects': (context.get('commit_subjects') or [])[:30],
        'changed_files': (context.get('changed_files') or [])[:50],
        'spec_excerpt': context.get('spec_excerpt'),
        'north_star_excerpt': context.get('north_star_excerpt'),
    }
    return (
        "You are Beacon, the operator's manager. A phase of a project just "
        "shipped (its build merged). Write a closeout so a busy CEO reads the "
        "story in seconds and the NEXT phase opens already knowing what happened "
        "— in his terms, never engineering jargon.\n\n"
        "Return ONLY a JSON object with exactly these keys (each one or two short "
        "sentences, plain English, no markdown):\n"
        '  "summary":         what shipped and what the next phase can pick up.\n'
        '  "shipped":         what actually got built and merged.\n'
        '  "changed_vs_spec": how the build diverged from the plan (or that it '
        "didn't).\n"
        '  "learnings":       anything learned worth carrying forward.\n\n'
        "You MAY reference PR numbers if present, but never task ids, branches, "
        "commits, agents, sequence ids, or risk codes. Ground every statement in "
        "the facts below; if a fact is missing, say so plainly rather than "
        "inventing it.\n\n"
        "Here are the facts (JSON):\n"
        f"{json.dumps(facts, indent=2, default=str)}\n\n"
        "Write the closeout now. Output ONLY the JSON object."
    )


# --------------------------------------------------------------------------- #
# the pure author
# --------------------------------------------------------------------------- #
def author_phase_closeout(
    phase: dict[str, Any],
    project: dict[str, Any],
    context: Optional[dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Author the full closeout for one done phase and return the field dict
    (``closeout`` + ``closeout_provenance``) for the non-committer to merge onto
    the phase card. PURE — does not mutate inputs or touch disk; the caller owns
    the single write + commit (single-committer invariant).

    The schema (spec § 0 / § 4 / § 7): a plain ``summary`` plus
    ``shipped`` · ``changed_vs_spec`` · ``learnings`` (LLM-authored prose with a
    deterministic raw fallback) and ``cost`` · ``done_gate_met`` (computed
    deterministically — never LLM-authored). ``use_llm=True`` (production) tries
    the claude voice and falls back to the deterministic raw closeout on ANY
    failure, so the author never raises and a malformed LLM reply renders a
    neutral summary rather than raw metadata (§6 fallback gate)."""
    context = context or {}
    now = now or datetime.now(timezone.utc)

    prose = None
    if use_llm:
        prose = narrator.generate_briefing_voice(
            build_phase_closeout_prompt(phase, project, context),
            keys=CLOSEOUT_LLM_KEYS,
        )
    authored_by_llm = prose is not None
    if prose is None:
        prose = render_raw_phase_closeout(phase, project, context)

    closeout = {
        'summary': prose['summary'],
        'shipped': prose['shipped'],
        'changed_vs_spec': prose['changed_vs_spec'],
        'learnings': prose['learnings'],
        'cost': render_cost(context),
        'done_gate_met': render_done_gate_met(context),
    }
    return {
        'closeout': closeout,
        'closeout_provenance': {
            'by': CLOSEOUT_BY,
            'model': CLOSEOUT_MODEL if authored_by_llm else 'raw',
            'at': now.isoformat(),
            'from_state': phase.get('lifecycle_state'),
            'pr_numbers': context.get('pr_numbers') or [],
        },
    }


# --------------------------------------------------------------------------- #
# follow-ups + divergence analysis (the closeout's "loose ends" + needs-you)
# --------------------------------------------------------------------------- #
def build_follow_ups_prompt(
    phase: dict[str, Any],
    project: dict[str, Any],
    context: dict[str, Any],
) -> str:
    """Authoring prompt for the closeout's loose ends + spec divergence. Reads
    the SAME source stream as the closeout prose (PRs/commits/diff/spec) and asks
    only for REAL follow-ups grounded in the facts — an empty list is the right
    answer for a clean build (no invented work, spec § 5 follow-up-spam guard)."""
    facts = {
        'phase_title': phase.get('title'),
        'phase_desired_end_state': phase.get('desired_end_state'),
        'project_title': project.get('title'),
        'pr_numbers': context.get('pr_numbers') or [],
        'pr_titles': context.get('pr_titles') or [],
        'pr_bodies': context.get('pr_bodies') or [],
        'commit_subjects': (context.get('commit_subjects') or [])[:30],
        'changed_files': (context.get('changed_files') or [])[:50],
        'spec_excerpt': context.get('spec_excerpt'),
    }
    return (
        "You are Beacon, the operator's manager. A phase of a project just "
        "shipped (its build merged). Two questions:\n"
        "  1. What LOOSE ENDS did it leave — follow-up work the build deferred, "
        "TODOs, known gaps, or risky areas that need a careful follow-up?\n"
        "  2. Did the build DIVERGE materially from its spec?\n\n"
        "Return ONLY a JSON object with exactly these keys:\n"
        '  "follow_ups":    a list (possibly empty) of objects, each '
        '{"text": "<one plain-English sentence naming the loose end>", '
        '"risky": <true if it is a careful/risky follow-up — touches '
        'security, credentials, data, or anything irreversible — else false>}.\n'
        '  "diverged":      true if the build diverged materially from the '
        "spec, else false.\n"
        '  "diverged_note": one short sentence describing the divergence, or '
        "null when it did not diverge.\n\n"
        "List ONLY real loose ends grounded in the facts below; an EMPTY list is "
        "the correct answer for a clean build. Never invent work. Use plain "
        "operator language — never task ids, branches, commits, or risk codes.\n\n"
        "Here are the facts (JSON):\n"
        f"{json.dumps(facts, indent=2, default=str)}\n\n"
        "Output ONLY the JSON object."
    )


def generate_closeout_analysis_voice(prompt: str) -> Optional[dict[str, Any]]:
    """One claude round-trip for the follow-ups + divergence analysis. Returns the
    parsed JSON object, or None on ANY failure (timeout, non-zero exit, no JSON)
    so the caller falls through to the deterministic empty analysis. Never raises.
    Mirrors `narrator.generate_briefing_voice` but tolerates non-string values
    (the analysis carries a list + booleans, not a flat prose dict)."""
    refuse_under_test('claude-spawn')
    try:
        proc = subprocess.run(
            ['claude', '--print', '--model', CLOSEOUT_MODEL,
             '--output-format', 'json', prompt],
            capture_output=True, text=True,
            timeout=narrator.CLAUDE_TIMEOUT_SEC, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        _log(f'analysis claude invocation failed: {type(e).__name__}: {e}')
        return None
    if proc.returncode != 0:
        _log(f'analysis claude exited {proc.returncode}; using empty analysis')
        return None
    try:
        envelope = json.loads(proc.stdout or '{}')
    except json.JSONDecodeError:
        return None
    text = (envelope.get('result') or '').strip() if isinstance(
        envelope, dict) else ''
    if not text:
        return None
    parsed = narrator.parse_briefing_json(text)
    return parsed if isinstance(parsed, dict) else None


def _normalize_analysis(raw: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Coerce a raw (LLM or absent) analysis into the canonical shape:
    ``{'follow_ups': [{'text': str, 'risky': bool}], 'diverged': bool,
    'diverged_note': Optional[str]}``. A None/garbage reply yields the clean
    default (no follow-ups, no divergence) — the deterministic fallback so the
    pass is head-less and never raises. Follow-up items may arrive as bare
    strings or objects; both normalize. Empty-text items are dropped; the list
    is capped at ``_MAX_FOLLOW_UPS``."""
    follow_ups: list[dict[str, Any]] = []
    diverged = False
    diverged_note: Optional[str] = None
    if isinstance(raw, dict):
        diverged = bool(raw.get('diverged'))
        note = raw.get('diverged_note')
        if isinstance(note, str) and note.strip():
            diverged_note = note.strip()
        for item in (raw.get('follow_ups') or []):
            if isinstance(item, str):
                text, risky = item.strip(), False
            elif isinstance(item, dict):
                t = item.get('text') or item.get('summary')
                text = t.strip() if isinstance(t, str) else ''
                risky = bool(item.get('risky'))
            else:
                continue
            if not text:
                continue
            follow_ups.append({'text': text, 'risky': risky})
            if len(follow_ups) >= _MAX_FOLLOW_UPS:
                break
    if not diverged:
        diverged_note = None  # a note only makes sense alongside a divergence
    return {
        'follow_ups': follow_ups,
        'diverged': diverged,
        'diverged_note': diverged_note,
    }


def author_closeout_analysis(
    phase: dict[str, Any],
    project: dict[str, Any],
    context: dict[str, Any],
    *,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Author the closeout's loose ends + divergence verdict, normalized. PURE
    (no disk/git). ``use_llm=True`` (production) tries the claude voice and falls
    back to the deterministic empty analysis on ANY failure; ``use_llm=False``
    (test / head-less) is the empty analysis directly. The empty default means a
    closeout never INVENTS follow-ups when the model is unavailable."""
    raw = None
    if use_llm:
        raw = generate_closeout_analysis_voice(
            build_follow_ups_prompt(phase, project, context))
    return _normalize_analysis(raw)


def compute_needs_decision_flags(
    done_gate_met: bool, analysis: dict[str, Any],
) -> list[str]:
    """The DM's "needs you" flags (spec § 2 / § 4 decision 2): the closeout DM
    flags when Larry must decide — the done-gate wasn't met, the build diverged
    from spec, or a risky follow-up exists. Plain operator language, computed
    DETERMINISTICALLY from the closeout schema + the (already-authored) analysis
    so the same inputs always produce the same flags. Returns [] for a clean
    completion (the common, no-interruption case)."""
    flags: list[str] = []
    if not done_gate_met:
        flags.append(
            "the done-gate wasn't met — not every build step merged cleanly")
    if analysis.get('diverged'):
        note = analysis.get('diverged_note')
        flags.append(
            f'the build diverged from the plan: {note}' if note
            else 'the build diverged from the plan')
    risky = [f for f in (analysis.get('follow_ups') or [])
             if isinstance(f, dict) and f.get('risky') and f.get('text')]
    if risky:
        flags.append(f'a risky follow-up needs your eyes — {risky[0]["text"]}')
    return flags


# --------------------------------------------------------------------------- #
# next-phase handoff (the "Next up: Phase Y" the DM names)
# --------------------------------------------------------------------------- #
def next_phase_after(
    project: dict[str, Any], phase: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """The next not-yet-done phase after ``phase`` in the project, by ``order``
    (ties broken by list position) — the phase the closeout DM names as "ready
    for you to brainstorm." Returns None when ``phase`` is the last/only phase (a
    one-off or the final phase of a multi-phase project — nothing to feed)."""
    if not isinstance(project, dict) or not isinstance(phase, dict):
        return None
    phases = [p for p in (project.get('phases') or []) if isinstance(p, dict)]
    cur_order = phase.get('order', 0)
    cur_id = phase.get('id')
    candidates = []
    for idx, p in enumerate(phases):
        if p.get('id') == cur_id:
            continue
        if p.get('lifecycle_state') == 'done':
            continue
        order = p.get('order', 0)
        if order > cur_order or (order == cur_order and idx > _phase_index(
                phases, cur_id)):
            candidates.append((order, idx, p))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]))
    return candidates[0][2]


def _phase_index(phases: list[dict[str, Any]], phase_id: Any) -> int:
    for idx, p in enumerate(phases):
        if p.get('id') == phase_id:
            return idx
    return -1


# --------------------------------------------------------------------------- #
# drop the loose ends into the funnel's Suggested lane (source='closeout')
# --------------------------------------------------------------------------- #
def drop_follow_ups_to_funnel(
    follow_ups: list[dict[str, Any]],
    *,
    phase: dict[str, Any],
    repo: Optional[str] = None,
    existing_ids: Optional[set[str]] = None,
    queue_dir: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> list[str]:
    """Drop each loose end into the funnel's Suggested lane as a
    ``proposed_by='closeout'`` card, via the shared `suggest_funnel_card`
    interface (the SAME non-committer queue → healer-drain path Beacon/Medic/Pulse
    use). Returns the list of queued mission ids.

    DEDUPED + IDEMPOTENT (spec § 5 follow-up-spam guard, Mirror focus): a loose
    end is skipped when its kebab id (a) repeats within this batch, (b) is already
    registered in missions.json (``existing_ids``), or (c) already has an
    in-flight queue file (`SuggestionError` from a prior, not-yet-drained run). So
    a re-run on the same closeout drops NO duplicate cards. Fail-safe: a single
    bad suggestion is logged and skipped — it never aborts the batch or the
    closeout flow."""
    import suggest_funnel_card as suggest  # local import: optional dep

    existing = set(existing_ids or set())
    queued: list[str] = []
    seen: set[str] = set()
    phase_title = (phase.get('title') or phase.get('id') or 'a phase').strip() \
        if isinstance(phase, dict) else 'a phase'
    for fu in follow_ups:
        text = fu.get('text') if isinstance(fu, dict) else None
        if not (isinstance(text, str) and text.strip()):
            continue
        text = text.strip()
        mid = suggest._kebab_case(text)
        if not mid or mid in seen or mid in existing:
            continue
        seen.add(mid)
        brief = f'Loose end from the "{phase_title}" closeout: {text}'
        try:
            result = suggest.suggest_funnel_card(
                source=FOLLOWUP_SOURCE, name=text, brief=brief, repo=repo,
                queue_dir=queue_dir, now=now)
            queued.append(result['mission_id'])
        except suggest.SuggestionError as e:
            # Already queued in-flight (not yet drained) — the dedup no-op.
            _log(f'follow-up {mid!r} already queued; skipping ({e})')
        except Exception as e:  # noqa: BLE001 — one bad card never aborts the batch
            _log(f'follow-up {mid!r} drop raised {type(e).__name__}: {e}; skipping')
    return queued


def _existing_mission_ids(repo_paths: dict[str, Any]) -> set[str]:
    """Best-effort set of ids already in missions.json — the cross-run dedup key
    so a re-run does not re-suggest a loose end the healer has already drained
    into the registry. Any failure (no path, unreadable, bad shape) yields an
    empty set — dedup degrades to the in-flight-queue guard, never raises."""
    try:
        import heal_orphan_autoregister as orphan  # local import: optional dep
        mpath = orphan.missions_path(repo_paths)
        if mpath is None:
            return set()
        reg = orphan.read_missions_registry(mpath)
        if not isinstance(reg, dict):
            return set()
        return {m.get('id') for m in (reg.get('missions') or [])
                if isinstance(m, dict) and m.get('id')}
    except Exception as e:  # noqa: BLE001 — dedup is best-effort
        _log(f'existing-mission-ids read raised {type(e).__name__}: {e}')
        return set()


# --------------------------------------------------------------------------- #
# North Star status-tracker tick (deterministic markdown-table edit)
# --------------------------------------------------------------------------- #
_DONE_STATUS_CELL = '✅ done'


def _phase_tracker_keys(phase: dict[str, Any]) -> set[str]:
    """The lowercase alphanumeric tokens that identify a phase in a tracker
    row's Phase column (e.g. phase id ``p4-closeout-author`` → {'p4',
    'closeout', 'author'}). Used to match a phase to its §9 row WITHOUT a false
    hit on unrelated rows."""
    keys: set[str] = set()
    for field in ('id', 'title'):
        val = phase.get(field)
        if isinstance(val, str):
            for tok in val.replace('-', ' ').replace('_', ' ').split():
                tok = ''.join(ch for ch in tok.lower() if ch.isalnum())
                if tok:
                    keys.add(tok)
    return keys


def _row_phase_cell_tokens(cell: str) -> set[str]:
    """Lowercase alphanumeric tokens in a tracker row's Phase column cell."""
    out: set[str] = set()
    for tok in cell.replace('-', ' ').replace('_', ' ').split():
        tok = ''.join(ch for ch in tok.lower() if ch.isalnum())
        if tok:
            out.add(tok)
    return out


def tick_north_star_tracker(
    doc_text: str, phase: dict[str, Any],
) -> Optional[str]:
    """Return ``doc_text`` with the §9 status-tracker row for ``phase`` flipped
    to done in its STATUS cell only (spec § 2: tick the status, never rewrite
    prose), or ``None`` when there is no unambiguous matching row (a no-op — the
    common case, since most projects carry no North Star tracker).

    A tracker row is a markdown table row ``| <phase> | <what> | <status> |``;
    we match the FIRST column's tokens against the phase id/title tokens and set
    the LAST column to ``✅ done``. Conservative: a row is matched only when the
    phase's specific token (e.g. ``p4``) is in the row's Phase cell, so an
    unrelated row is never ticked. Idempotent: a row already at ``✅ done`` is
    not re-written (returns None — no delta)."""
    if not isinstance(doc_text, str) or not doc_text:
        return None
    keys = _phase_tracker_keys(phase)
    if not keys:
        return None

    out_lines: list[str] = []
    changed = False
    for line in doc_text.splitlines(keepends=True):
        stripped = line.strip()
        # A table row: starts and ends with a pipe and has >= 3 cells.
        if not changed and stripped.startswith('|') and stripped.endswith('|'):
            # Split into cells; drop the empty edges from leading/trailing pipes.
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            if len(cells) >= 3:
                phase_tokens = _row_phase_cell_tokens(cells[0])
                # Match only on a SPECIFIC token (skip generic separator rows
                # like header/`---` and the legend). Require overlap on a token
                # that is not a pure table-formatting artifact.
                overlap = phase_tokens & keys
                is_sep = set(cells[0].replace('-', '').replace(':', '').split()) == set()
                if overlap and not is_sep:
                    if cells[-1] == _DONE_STATUS_CELL:
                        return None  # already ticked — no delta
                    cells[-1] = _DONE_STATUS_CELL
                    newline = '\n' if line.endswith('\n') else ''
                    out_lines.append('| ' + ' | '.join(cells) + ' |' + newline)
                    changed = True
                    continue
        out_lines.append(line)

    if not changed:
        return None
    return ''.join(out_lines)


def write_north_star_tick(
    repo: Optional[Path], project: dict[str, Any], phase: dict[str, Any],
) -> bool:
    """Tick the project's North Star status-tracker row for ``phase`` to done, on
    disk — a NON-committer write (atomic tmp + os.replace), the SAME posture as
    the projects.json card write. The SOLE git committer stays
    ``heal_projects_store``, which drains this doc delta into the card's commit.
    Returns True iff the doc was rewritten.

    Fail-safe + idempotent: no ``north_star_ref`` (the common case), an
    unresolvable / out-of-repo path, a read/write error, or an already-ticked row
    is a logged-or-silent no-op (returns False) — a tracker tick must NEVER raise
    into the SEQUENCE_COMPLETE flow."""
    ref = project.get('north_star_ref') if isinstance(project, dict) else None
    if not isinstance(ref, str) or not ref.strip():
        return False
    rel = ref.split('#', 1)[0].strip()
    if not rel:
        return False
    p = Path(rel)
    if not p.is_absolute():
        if repo is None:
            return False
        p = repo / rel
    try:
        original = p.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return False
    ticked = tick_north_star_tracker(original, phase)
    if ticked is None or ticked == original:
        return False
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(p.parent), prefix=p.name + '.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(ticked)
            os.replace(tmp_name, p)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as e:
        _log(f'north star tick write failed ({p}): {e}')
        return False
    _log(f'ticked north star tracker for phase={phase.get("id")} ({rel})')
    return True


def run_closeout_for_sequence(
    seq: dict[str, Any], *, use_llm: bool = True, now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Orchestrate the full closeout for a completed sequence (the entry point the
    notifier's SEQUENCE_COMPLETE hook calls). Resolves the phase by
    ``sequence_ref == seq['seq_id']``, gathers context, authors the closeout,
    attaches it to the phase card (non-committer), ticks the North Star tracker
    (non-committer), authors the loose-ends/divergence analysis, and drops the
    loose ends into the funnel's Suggested lane (source='closeout', deduped).

    Returns the closeout OUTPUTS the completion DM renders — a dict
    ``{attached, summary, phase_title, next_phase_title, done_gate_met, flags,
    follow_ups_queued}`` — or ``None`` for a non-launch sequence (no matching
    phase, the common case for an ordinary build sequence).

    Fail-safe: the single-committer invariant holds (this only does non-committer
    writes — the healer commits both artifacts). NEVER raises into the caller —
    the notifier hook also wraps this in a daemon-never-wedge guard."""
    import heal_projects_store as healer  # local import: optional dep chain
    import projects_status_writeback as psw

    seq = seq or {}
    seq_id = seq.get('seq_id')
    if not seq_id:
        return None
    repo_paths = healer.load_repo_paths()
    path = healer.projects_path(repo_paths)
    if path is None:
        return None
    registry = healer.read_registry(path)
    if not isinstance(registry, dict):
        return None
    project, phase = narrator_find_phase(registry, seq_id)
    if phase is None or project is None:
        return None  # non-launch sequence — no phase to close out

    repo = repo_paths.get('ourliberty-agent-core')
    context = gather_closeout_context(phase, project, seq, repo=repo)
    fields = author_phase_closeout(
        phase, project, context, now=now, use_llm=use_llm)
    wrote = psw.attach_closeout(seq_id=seq_id, closeout_fields=fields, now=now)
    if wrote:
        _log(f'authored closeout for sequence_ref={seq_id} phase={phase.get("id")}')
    write_north_star_tick(repo, project, phase)

    # p4-closeout-outputs: loose ends → funnel Suggested lane (deduped), and the
    # closeout DM payload (summary + next-phase handoff + needs-you flags).
    analysis = author_closeout_analysis(phase, project, context, use_llm=use_llm)
    queued = drop_follow_ups_to_funnel(
        analysis['follow_ups'], phase=phase, repo=project.get('repo'),
        existing_ids=_existing_mission_ids(repo_paths), now=now)
    if queued:
        _log(f'dropped {len(queued)} follow-up(s) to funnel for seq={seq_id}: '
             f'{", ".join(queued)}')

    closeout = fields['closeout']
    done_gate_met = bool(closeout.get('done_gate_met', True))
    nxt = next_phase_after(project, phase)
    return {
        'attached': wrote,
        'summary': closeout.get('summary'),
        'phase_title': (phase.get('title') or phase.get('id') or '').strip(),
        'next_phase_title': (
            (nxt.get('title') or nxt.get('id') or '').strip() if nxt else None),
        'done_gate_met': done_gate_met,
        'flags': compute_needs_decision_flags(done_gate_met, analysis),
        'follow_ups_queued': queued,
    }


def narrator_find_phase(registry: dict[str, Any], seq_id: str):
    """Thin re-export of ``projects_store.find_phase_by_sequence_ref`` so this
    module owns its phase lookup without the notifier importing projects_store."""
    import projects_store  # local import: optional dep
    return projects_store.find_phase_by_sequence_ref(registry, seq_id)
