#!/usr/bin/env python3
"""spec_review_runner.py — the antagonistic spec-review gauntlet engine.

Runner-engine slice of agents/beacon/specs/spec-gauntlet-gate.md (§3.2, §3.3,
§3.4, §3.7). A small own-unit daemon (systemd/ourliberty-spec-review-runner.
service) that drains the spec-review spool and runs each spooled spec through
the fixed round machine, writing exactly one conclusion file per gauntlet for
the host daemons to stamp.

Why its own daemon (§3.2): the reviewers cannot run inside the host daemons —
the notifier's cgroup is MemoryMax=512M vs ~400MB/claude ×3, and the bot's
sandbox has agent-core read-only. This unit gets limits sized for 3 claude
children + ReadWritePaths for agent-core/worktrees, mirroring the notifier unit.

The round machine (§3.3), fixed for correctness (not just termination):

  R1        3 lenses (S-A/S-B/S-C) attack the spec body in parallel. No blocking
            finding  → conclude ``passed`` (advisory findings ride the digest).
  Revision  (max 1) a GATE-OWNED ``claude --print`` step under run_review_step.sh
            revises the spec against the blocking findings. This is NOT an
            inbox/outbox Beacon session: its output is never written to any
            inbox/outbox and NEVER parsed for an APPROVAL_REQUEST marker, so a
            revision can never re-enter the approval path (Mirror focus #1).
  R2        the same 3 lenses re-review the REVISED body, checking BOTH "prior
            blocking resolved" AND "no NEW blocking flaw introduced by the diff"
            (§3.3 — v1 checked only the former). No revised body ever ships
            without one full re-review pass. R2 still blocking → ``contested``.

Terminal states, one per gauntlet (§3.4/§3.5): ``passed | contested |
incomplete | errored``.

Restart-safety (§3.2, Mirror focus #2): every round's per-lens result — and the
revision result — is archived to ~/agents/state/spec-review/archive/ AS IT
COMPLETES. On start the runner resumes a pending gauntlet from the last archived
round; a completed round is NEVER re-run (the notifier-restart dup class). The
gauntlet start time is the spool entry's ``created_at``, so the wall-clock
ceiling survives a restart and a pending entry past the ceiling (dead runner) is
swept to ``incomplete`` — an interrupted gauntlet can delay an approval, never
lose one.

Fail-open everywhere (§3.3, Mirror focus #3): a reviewer/revision step that
times out, errors, or emits malformed output is treated as "lens did not
conclude" — no findings, surfaced on the digest as a non-concluding lens, never
a silent pass and never a crash. A gauntlet whose engine itself throws concludes
``errored`` (the card is still legible).

Testability (§3.7): reviewer/revision invocation, the concurrency guard, the
wall clock, the chain-event emitter, and the spec_doc committer are all
injectable. The state machine (blocking→revise, clean→pass, timeout→fail-open,
resume, contested) is tested deterministically against stub reviewers emitting
fixture findings; LLM finding *quality* is out of unit-test scope.
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402  (shared durable atomic write)
import spec_review_conclusion as conclusion  # noqa: E402
from concurrency_guard import get_guard  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or HOME / 'agents')
REPO_ROOT = Path(__file__).resolve().parent.parent

STATE_DIR = AGENTS_ROOT / 'state' / 'spec-review'
PENDING_DIR = STATE_DIR / 'pending'
CONCLUDED_DIR = STATE_DIR / 'concluded'
ARCHIVE_DIR = STATE_DIR / 'archive'
# Runner-maintained fresh origin/main checkout the reviewers fact-check against
# (read-only to reviewers; §3.2 — per-gauntlet worktrees avoided entirely to
# stay clear of the leaked-worktree/null-HEAD class).
CHECKOUT_DIR = STATE_DIR / 'origin-main'

LOG_FILE = AGENTS_ROOT / 'logs' / 'spec-review-runner.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'spec-review-runner.heartbeat'
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'

# The lenses doc is vendored and read by ABSOLUTE path (same rule as Mirror's
# bug-hunt lenses). Prefer the repo copy resolved from this file; fall back to
# the canonical install path the doc itself names.
LENSES_DOC = REPO_ROOT / 'review' / 'spec-critique-lenses.md'
_LENSES_DOC_FALLBACK = Path('/home/larry/agent-core/review/spec-critique-lenses.md')
RUN_REVIEW_STEP_SH = REPO_ROOT / 'scripts' / 'run_review_step.sh'

LENSES: tuple[str, ...] = ('S-A', 'S-B', 'S-C')
SLOT_AGENT = 'spec-review'
POLL_INTERVAL_S = 15
_VALID_SEVERITY = frozenset({'blocking', 'advisory'})

_LOGGER = logging.getLogger('spec_review_runner')


# --------------------------------------------------------------------------- #
# Finding grammar — the established block-delimiter marker shape (§3.3), parsed
# with the parse_mirror_marker-style validate-or-reject structure (NOT bare
# JSON). A malformed block = "lens did not conclude" (fail-open).
# --------------------------------------------------------------------------- #
_SPEC_FINDINGS_RE = re.compile(
    r'===\s*SPEC_FINDINGS\s*===\s*(\{.*?\})\s*===\s*END_SPEC_FINDINGS\s*===',
    re.DOTALL,
)
_REVISED_BODY_RE = re.compile(
    r'===\s*REVISED_SPEC\s*===\s*(.*?)\s*===\s*END_REVISED_SPEC\s*===',
    re.DOTALL,
)


class MalformedSpecFindings(ValueError):
    """A SPEC_FINDINGS block was present but unparseable / not an object /
    missing the ``findings`` list. Caller treats as 'lens did not conclude'."""


class MalformedRevision(ValueError):
    """The revision step produced no usable revised-spec body. Caller treats it
    as a failed revision round (counts toward the cap, §3.3)."""


@dataclass
class Finding:
    lens: str
    severity: str
    claim: str
    spec_quote: str
    suggested_change: str

    def as_dict(self) -> dict[str, str]:
        return {
            'lens': self.lens, 'severity': self.severity, 'claim': self.claim,
            'spec_quote': self.spec_quote, 'suggested_change': self.suggested_change,
        }


@dataclass
class StepResult:
    """The raw outcome of one reviewer/revision step. ``concluded`` is False iff
    the step timed out, the claude process errored, or produced empty output —
    the fail-open signal (§3.3)."""
    concluded: bool
    output: str


def parse_spec_findings(output: str, lens: str) -> list[Finding]:
    """Extract validated findings from one reviewer's output.

    Raises ``MalformedSpecFindings`` when a block is absent or the JSON is
    unparseable / not an object / has no ``findings`` list — the caller maps
    that to "lens did not conclude" (fail-open). Individual findings missing a
    non-empty ``spec_quote`` are DISCARDED (§3.3 — "no spec quote → finding
    discarded"), not raised on, so one hand-wavy finding doesn't void a lens
    that also produced real ones. An unknown/absent severity discards that
    finding too."""
    if not output:
        raise MalformedSpecFindings('empty reviewer output')
    m = _SPEC_FINDINGS_RE.search(output)
    if not m:
        raise MalformedSpecFindings('no SPEC_FINDINGS block')
    try:
        obj = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise MalformedSpecFindings(f'unparseable JSON: {e}') from e
    if not isinstance(obj, dict) or not isinstance(obj.get('findings'), list):
        raise MalformedSpecFindings('missing findings list')
    out: list[Finding] = []
    for raw in obj['findings']:
        if not isinstance(raw, dict):
            continue
        severity = raw.get('severity')
        spec_quote = raw.get('spec_quote')
        if severity not in _VALID_SEVERITY:
            continue
        # Mandatory verbatim quote — a finding with none is discarded (blocks
        # "this feels underspecified" noise).
        if not isinstance(spec_quote, str) or not spec_quote.strip():
            continue
        out.append(Finding(
            lens=str(raw.get('lens') or lens),
            severity=severity,
            claim=str(raw.get('claim') or ''),
            spec_quote=spec_quote,
            suggested_change=str(raw.get('suggested_change') or ''),
        ))
    return out


def parse_revision_output(output: str) -> str:
    """Extract the revised spec body from a gate-owned revision step.

    The revision's strict output contract (§3.3) is a fenced revised-spec body
    plus per-finding responses. This engine needs the revised BODY (what R2
    re-reviews and what the write-back commits); the per-finding accepted/
    rejected responses are advisory narrative and not required to proceed. We
    accept an explicit ``=== REVISED_SPEC === ... === END_REVISED_SPEC ===``
    block; absent that, a single triple-backtick fenced block. No body → the
    round failed (``MalformedRevision``).

    NOTE (Mirror focus #1): this parser deliberately has NO branch that looks
    for an APPROVAL_REQUEST marker. The revision output is never inspected for
    one and never written to any inbox/outbox — a revision cannot re-enter the
    approval path.
    """
    if not output:
        raise MalformedRevision('empty revision output')
    m = _REVISED_BODY_RE.search(output)
    if m and m.group(1).strip():
        return m.group(1).strip()
    fence = re.search(r'```[a-zA-Z0-9_-]*\n(.*?)```', output, re.DOTALL)
    if fence and fence.group(1).strip():
        return fence.group(1).strip()
    raise MalformedRevision('no revised-spec body found')


def _extract_claude_result(stdout: str) -> str:
    """Pull the assistant text out of a ``claude --print --output-format json``
    stdout, falling back to raw stdout for the plaintext case."""
    if not stdout:
        return ''
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(data, dict):
        if data.get('is_error') is True:
            return ''
        return str(data.get('result') or data.get('text') or '')
    return stdout


def _claude_bin() -> str:
    """The claude binary, via the injectable env seam (§3.7) — same shape as the
    bot's CLAUDE_BIN test seam. SPEC_REVIEW_CLAUDE_BIN wins so a test can point
    the gauntlet at a stub without disturbing other claude callers."""
    return (os.environ.get('SPEC_REVIEW_CLAUDE_BIN')
            or os.environ.get('CLAUDE_BIN') or 'claude')


def real_step_runner(role: str, lens: str, prompt: str, ceiling_s: int,
                     model: Optional[str] = None) -> StepResult:
    """Production step runner: run ONE claude step FOREGROUND under the existing
    run_review_step.sh ceiling primitive (§3.3 — foreground, process-group
    kill; exit 124 on timeout). Any non-zero exit (timeout or claude error) =
    did-not-conclude (fail-open)."""
    bin_ = _claude_bin()
    cmd = [
        str(RUN_REVIEW_STEP_SH),
        '--timeout', str(int(ceiling_s)),
        '--label', f'spec-review-{role}-{lens}',
        '--',
        bin_, '--print', '--output-format', 'json',
    ]
    if model:
        cmd += ['--model', model]
    cmd.append(prompt)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=ceiling_s + 60, start_new_session=True,
        )
    except (subprocess.TimeoutExpired, OSError):
        return StepResult(False, '')
    if proc.returncode != 0:  # 124 = ceiling breach; anything else = claude error
        return StepResult(False, proc.stdout or '')
    text = _extract_claude_result(proc.stdout or '')
    return StepResult(bool(text.strip()), text)


def real_emit(task_id: str, round_no: int, event: dict[str, Any]) -> None:
    """Best-effort ``spec_review_round`` chain-event (§3.5 — the type is
    registered in step 1 so emit_event cannot silently drop it). Never raises;
    a failed emit is event-loss, never a gauntlet failure. Kept thin here — the
    challenge digest / gauge (§3.5) are a later slice."""
    try:
        import chain_event_emit  # local: keeps the Supabase dep off the hot path
        chain_event_emit.emit_event(
            event_type='spec_review_round',
            agent='spec-review',
            task_id=task_id,
            payload=event,
            id_extra=f'{task_id}-r{round_no}',
        )
    except Exception:  # pragma: no cover — best-effort visibility only
        _LOGGER.debug('spec_review_round emit failed for %s r%s', task_id, round_no)


def real_committer(spec_doc: str, revised_body: str, task_id: str) -> bool:
    """Production spec_doc write-back committer (§3.4). Writes the revised body
    to ``spec_doc`` inside the runner-maintained origin/main checkout and commits
    it — the runner is the SOLE committer of post-gauntlet revisions (machine-
    owned single-committer rule). Best-effort: a git failure logs and returns
    False (the payload body still carries the revision; the digest says so),
    never crashes the gauntlet."""
    try:
        target = CHECKOUT_DIR / spec_doc
        if CHECKOUT_DIR not in target.resolve().parents and target.resolve() != CHECKOUT_DIR:
            # Refuse a spec_doc that escapes the checkout (path traversal guard).
            _LOGGER.warning('spec_doc %r escapes checkout — skipping write-back', spec_doc)
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_io.atomic_write_text(target, revised_body.rstrip('\n') + '\n')
        subprocess.run(['git', '-C', str(CHECKOUT_DIR), 'add', spec_doc],
                       check=True, capture_output=True, text=True, timeout=30)
        subprocess.run(
            ['git', '-C', str(CHECKOUT_DIR), 'commit', '-m',
             f'chore(spec-gauntlet): revision write-back for {task_id}'],
            check=True, capture_output=True, text=True, timeout=30)
        subprocess.run(['git', '-C', str(CHECKOUT_DIR), 'push', 'origin', 'HEAD:main'],
                       check=True, capture_output=True, text=True, timeout=60)
        return True
    except Exception as e:  # pragma: no cover — best-effort write-back
        _LOGGER.warning('spec_doc write-back failed for %s: %s', task_id, e)
        return False


def _epoch(value: Any) -> float:
    """Coerce a spool ``created_at`` (epoch number or ISO-8601 string) to epoch
    seconds. Anything unparseable → now (so a broken timestamp never makes a
    fresh gauntlet look already-expired)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            s = value.replace('Z', '+00:00')
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            pass
    return time.time()


class SpecReviewRunner:
    """The gauntlet engine. All external effects are injectable (§3.7)."""

    def __init__(
        self,
        *,
        step_runner: Optional[Callable[..., StepResult]] = None,
        guard: Any = None,
        now: Optional[Callable[[], float]] = None,
        emit: Optional[Callable[[str, int, dict], None]] = None,
        committer: Optional[Callable[[str, str, str], bool]] = None,
        per_step_ceiling_s: int = 900,
        wall_clock_ceiling_s: int = 4500,
        model: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.step_runner = step_runner or real_step_runner
        self._guard = guard  # lazily default to get_guard() (test-jail: avoid at import)
        self.now = now or time.time
        self.emit = emit or real_emit
        self.committer = committer or real_committer
        self.per_step_ceiling_s = per_step_ceiling_s
        self.wall_clock_ceiling_s = wall_clock_ceiling_s
        self.model = model
        self.log = logger or _LOGGER

    @property
    def guard(self) -> Any:
        if self._guard is None:
            self._guard = get_guard()
        return self._guard

    # ---- filesystem helpers ------------------------------------------------ #
    @staticmethod
    def _ensure_dirs() -> None:
        for d in (PENDING_DIR, CONCLUDED_DIR, ARCHIVE_DIR):
            d.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _archive_path(task_id: str, round_label: str, lens: Optional[str] = None) -> Path:
        name = f'{task_id}-{round_label}' + (f'-{lens}' if lens else '') + '.json'
        return ARCHIVE_DIR / name

    @staticmethod
    def _load_archive(path: Path) -> Optional[dict[str, Any]]:
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def _write_archive(self, path: Path, obj: dict[str, Any]) -> None:
        atomic_io.atomic_write_json(path, obj, indent=2)

    # ---- prompt assembly --------------------------------------------------- #
    def _lenses_doc(self) -> str:
        for p in (LENSES_DOC, _LENSES_DOC_FALLBACK):
            try:
                return p.read_text()
            except OSError:
                continue
        return ''  # fail-safe: reviewers still get the spec body + finding contract

    def _build_review_prompt(self, lens: str, spec_body: str, extra: str) -> str:
        checkout_note = ''
        if CHECKOUT_DIR.exists():
            checkout_note = (
                f'\nYou MAY fact-check claims against the read-only origin/main '
                f'checkout at {CHECKOUT_DIR} (advisory; skip if a path is absent).\n')
        return (
            f'{self._lenses_doc()}\n\n'
            f'You are running lens {lens}. Review ONLY through that lens.\n'
            f'{checkout_note}{extra}\n'
            f'Emit findings in EXACTLY the SPEC_FINDINGS block shape from the '
            f'lenses doc — nothing else. If you find nothing for this lens, emit '
            f'an empty findings list.\n\n'
            f'=== SPEC UNDER REVIEW ===\n{spec_body}\n=== END SPEC UNDER REVIEW ===\n'
        )

    def _build_revision_prompt(self, spec_body: str, blocking: list[dict]) -> str:
        findings_json = json.dumps({'blocking_findings': blocking}, indent=2)
        return (
            'You are the gate-owned revision step for an antagonistic spec '
            'review. Revise the spec body below to resolve EVERY blocking '
            'finding, changing as little else as possible.\n\n'
            'STRICT OUTPUT CONTRACT:\n'
            '  1. The full revised spec body inside a '
            '`=== REVISED_SPEC === ... === END_REVISED_SPEC ===` block.\n'
            '  2. Then, per finding, one line: `accepted+changed` or '
            '`rejected: <reason>`.\n'
            'Do NOT emit any approval/dispatch marker. Your output is read only '
            'for the revised body and is never dispatched anywhere.\n\n'
            f'=== BLOCKING FINDINGS ===\n{findings_json}\n=== END BLOCKING FINDINGS ===\n\n'
            f'=== SPEC TO REVISE ===\n{spec_body}\n=== END SPEC TO REVISE ===\n'
        )

    @staticmethod
    def _r2_instructions(prior_blocking: list[dict], diff: str) -> str:
        return (
            'This is a RE-REVIEW (R2) of a REVISED spec. Check BOTH:\n'
            '  (a) that each prior blocking finding below is now resolved, AND\n'
            '  (b) that the revision diff introduced NO new blocking flaw.\n'
            'Flag (b) as blocking too — a revision that fixes one flaw and adds '
            'another must NOT ship.\n\n'
            f'=== PRIOR BLOCKING FINDINGS ===\n{json.dumps(prior_blocking, indent=2)}\n'
            f'=== END PRIOR BLOCKING FINDINGS ===\n\n'
            f'=== REVISION DIFF ===\n{diff}\n=== END REVISION DIFF ===\n'
        )

    # ---- round execution --------------------------------------------------- #
    def _run_one_lens(self, task_id: str, round_label: str, lens: str,
                      spec_body: str, extra: str) -> dict[str, Any]:
        """Run a single lens under a concurrency slot. Returns a resumable
        archive dict: {lens, concluded, findings}. A slot that never frees
        within the step ceiling, a timed-out/errored step, or malformed output
        all map to concluded=False (fail-open — the lens did not conclude)."""
        prompt = self._build_review_prompt(lens, spec_body, extra)
        # wait_for_slot blocks until a slot frees, so when <3 slots are free the
        # lenses SERIALIZE on the shared guard rather than bypassing it or
        # failing (§3.2). When 3 are free they run genuinely in parallel.
        if not self.guard.wait_for_slot(SLOT_AGENT, task_id,
                                        timeout=self.per_step_ceiling_s):
            return {'lens': lens, 'concluded': False, 'findings': []}
        try:
            res = self.step_runner('review', lens, prompt, self.per_step_ceiling_s,
                                   self.model)
        finally:
            self.guard.release(SLOT_AGENT)
        if not res.concluded:
            return {'lens': lens, 'concluded': False, 'findings': []}
        try:
            findings = parse_spec_findings(res.output, lens)
        except MalformedSpecFindings:
            return {'lens': lens, 'concluded': False, 'findings': []}
        return {'lens': lens, 'concluded': True,
                'findings': [f.as_dict() for f in findings]}

    def _round_lenses(self, task_id: str, round_label: str, spec_body: str,
                      extra: str = '') -> dict[str, dict[str, Any]]:
        """Run (or RESUME) all three lenses for one round. A lens whose archive
        already exists is loaded, never re-run (restart-safety, Mirror focus
        #2). Fresh lenses run parallel/serial via the shared guard, each
        archived AS IT COMPLETES."""
        results: dict[str, dict[str, Any]] = {}
        to_run: list[str] = []
        for lens in LENSES:
            cached = self._load_archive(self._archive_path(task_id, round_label, lens))
            if cached is not None:
                results[lens] = cached
            else:
                to_run.append(lens)
        if to_run:
            workers = min(3, len(to_run))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                fresh = list(ex.map(
                    lambda l: self._run_one_lens(task_id, round_label, l, spec_body, extra),
                    to_run))
            for r in fresh:
                self._write_archive(
                    self._archive_path(task_id, round_label, r['lens']), r)
                results[r['lens']] = r
        return results

    def _revision(self, task_id: str, spec_body: str,
                  blocking: list[dict]) -> dict[str, Any]:
        """Run (or RESUME) the single gate-owned revision round. Archived even
        on failure so a restart never re-runs it and it correctly counts toward
        the 1-revision cap (§3.3). Malformed/timed-out revision → concluded=
        False (the blocking findings stay unresolved → contested downstream)."""
        ap = self._archive_path(task_id, 'revision')
        cached = self._load_archive(ap)
        if cached is not None:
            return cached
        prompt = self._build_revision_prompt(spec_body, blocking)
        if not self.guard.wait_for_slot(SLOT_AGENT, task_id,
                                        timeout=self.per_step_ceiling_s):
            result = {'concluded': False, 'revised_body': ''}
            self._write_archive(ap, result)
            return result
        try:
            res = self.step_runner('revision', 'revision', prompt,
                                   self.per_step_ceiling_s, self.model)
        finally:
            self.guard.release(SLOT_AGENT)
        if not res.concluded:
            result = {'concluded': False, 'revised_body': ''}
        else:
            try:
                revised_body = parse_revision_output(res.output)
                result = {'concluded': True, 'revised_body': revised_body}
            except MalformedRevision:
                result = {'concluded': False, 'revised_body': ''}
        self._write_archive(ap, result)
        return result

    # ---- finding aggregation ----------------------------------------------- #
    @staticmethod
    def _findings_by_severity(round_results: dict[str, dict], severity: str) -> list[dict]:
        out: list[dict] = []
        for lens in LENSES:
            r = round_results.get(lens) or {}
            if not r.get('concluded'):
                continue
            for f in r.get('findings', []):
                if f.get('severity') == severity:
                    out.append(f)
        return out

    @staticmethod
    def _lens_verdicts(round_results: dict[str, dict]) -> dict[str, bool]:
        return {lens: bool((round_results.get(lens) or {}).get('concluded'))
                for lens in LENSES}

    # ---- conclusion -------------------------------------------------------- #
    def _conclude(self, task_id: str, payload: Any, payload_hash: str,
                  terminal_state: str, *, rounds: int,
                  resolved_count: int = 0,
                  contested_findings: Optional[list[dict]] = None,
                  advisory_findings: Optional[list[dict]] = None,
                  lens_verdicts: Optional[dict[str, bool]] = None,
                  spec_doc_updated: bool = False,
                  revision_failed: bool = False,
                  error: Optional[str] = None,
                  reason: Optional[str] = None) -> dict[str, Any]:
        contested_findings = contested_findings or []
        advisory_findings = advisory_findings or []
        conc = {
            'task_id': task_id,
            'payload_hash': payload_hash,
            'terminal_state': terminal_state,
            'rounds': rounds,
            'final_payload': payload,
            'blocking_resolved_count': resolved_count,
            'contested_count': len(contested_findings),
            'contested_findings': contested_findings,
            'advisory_findings': advisory_findings,
            'lens_verdicts': lens_verdicts or {},
            'spec_doc_updated': spec_doc_updated,
            'revision_failed': revision_failed,
            'concluded_at': datetime.now(timezone.utc).isoformat(),
        }
        if error is not None:
            conc['error'] = error
        if reason is not None:
            conc['reason'] = reason
        CONCLUDED_DIR.mkdir(parents=True, exist_ok=True)
        atomic_io.atomic_write_json(conclusion.conclusion_path(task_id), conc, indent=2)
        self.log.info('gauntlet %s concluded: %s (%d round(s))',
                      task_id, terminal_state, rounds)
        return conc

    def _maybe_write_back(self, payload: Any, revised_body: str) -> bool:
        """§3.4 write-back: when the payload carries a ``spec_doc``, an accepted
        revision must follow into the committed spec (kickoff treats the
        repo-committed spec_doc as authoritative). No spec_doc → payload is the
        sole source of truth; nothing to commit."""
        if not isinstance(payload, dict):
            return False
        spec_doc = payload.get('spec_doc')
        if not spec_doc or not isinstance(spec_doc, str):
            return False
        return bool(self.committer(spec_doc, revised_body, payload.get('task_id', '')))

    # ---- the round machine ------------------------------------------------- #
    def run_gauntlet(self, task_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        """Run (or RESUME) one gauntlet to a terminal conclusion. Never raises —
        an engine crash concludes ``errored`` (§3.4/AC-8)."""
        payload = entry.get('payload')
        payload_hash = entry.get('payload_hash', '')
        spec_body = payload.get('prompt', '') if isinstance(payload, dict) else ''
        started = _epoch(entry.get('created_at'))

        def wall_exceeded() -> bool:
            return (self.now() - started) > self.wall_clock_ceiling_s

        try:
            # Stale-spool sweep (§3.4): a pending entry already past the wall
            # ceiling (e.g. inherited across a dead-runner restart) concludes
            # incomplete — an interrupted gauntlet can delay a stamp, never lose
            # one.
            if wall_exceeded():
                return self._conclude(
                    task_id, payload, payload_hash, 'incomplete', rounds=0,
                    reason='wall-clock ceiling exceeded (stale spool / dead runner)')

            # ---- R1 ----
            r1 = self._round_lenses(task_id, 'r1', spec_body)
            r1_blocking = self._findings_by_severity(r1, 'blocking')
            r1_advisory = self._findings_by_severity(r1, 'advisory')
            self.emit(task_id, 1, self._round_event(1, r1, resolved=0))

            if not r1_blocking:
                return self._conclude(
                    task_id, payload, payload_hash, 'passed', rounds=1,
                    advisory_findings=r1_advisory,
                    lens_verdicts=self._lens_verdicts(r1))

            if wall_exceeded():
                return self._conclude(
                    task_id, payload, payload_hash, 'incomplete', rounds=1,
                    contested_findings=r1_blocking, advisory_findings=r1_advisory,
                    lens_verdicts=self._lens_verdicts(r1),
                    reason='wall-clock ceiling exceeded after R1')

            # ---- Revision (max 1) ----
            rev = self._revision(task_id, spec_body, r1_blocking)
            if not rev.get('concluded'):
                # Failed/malformed revision: blocking findings remain unresolved
                # and there is no verified body to ship → contested (never a
                # silent pass; §3.3 malformed-revision = failed round).
                return self._conclude(
                    task_id, payload, payload_hash, 'contested', rounds=2,
                    contested_findings=r1_blocking, advisory_findings=r1_advisory,
                    lens_verdicts=self._lens_verdicts(r1), revision_failed=True,
                    reason='revision step did not conclude')

            revised_body = rev['revised_body']
            final_payload = payload
            if isinstance(payload, dict):
                final_payload = dict(payload)
                final_payload['prompt'] = revised_body

            if wall_exceeded():
                return self._conclude(
                    task_id, final_payload, payload_hash, 'incomplete', rounds=2,
                    contested_findings=r1_blocking, advisory_findings=r1_advisory,
                    lens_verdicts=self._lens_verdicts(r1),
                    reason='wall-clock ceiling exceeded after revision')

            # ---- R2 re-review (diff-aware) ----
            diff = self._diff(spec_body, revised_body)
            r2 = self._round_lenses(
                task_id, 'r2', revised_body,
                extra=self._r2_instructions(r1_blocking, diff))
            r2_blocking = self._findings_by_severity(r2, 'blocking')
            r2_advisory = self._findings_by_severity(r2, 'advisory')
            self.emit(task_id, 2, self._round_event(2, r2, resolved=len(r1_blocking)))

            # No revised body ever ships without one full R2 pass — done above.
            spec_doc_updated = self._maybe_write_back(payload, revised_body)

            if r2_blocking:
                return self._conclude(
                    task_id, final_payload, payload_hash, 'contested', rounds=2,
                    resolved_count=len(r1_blocking), contested_findings=r2_blocking,
                    advisory_findings=r1_advisory + r2_advisory,
                    lens_verdicts=self._lens_verdicts(r2),
                    spec_doc_updated=spec_doc_updated)

            return self._conclude(
                task_id, final_payload, payload_hash, 'passed', rounds=2,
                resolved_count=len(r1_blocking),
                advisory_findings=r1_advisory + r2_advisory,
                lens_verdicts=self._lens_verdicts(r2),
                spec_doc_updated=spec_doc_updated)
        except Exception as e:  # engine crash → errored, still legible (AC-8)
            self.log.exception('gauntlet %s crashed', task_id)
            return self._conclude(
                task_id, payload, payload_hash, 'errored', rounds=0,
                error=f'{type(e).__name__}: {e}')

    @staticmethod
    def _round_event(round_no: int, round_results: dict[str, dict],
                     resolved: int) -> dict[str, Any]:
        blocking = SpecReviewRunner._findings_by_severity(round_results, 'blocking')
        advisory = SpecReviewRunner._findings_by_severity(round_results, 'advisory')
        return {
            'round': round_no,
            'blocking_count': len(blocking),
            'advisory_count': len(advisory),
            'resolved_count': resolved,
            'lens_verdicts': SpecReviewRunner._lens_verdicts(round_results),
        }

    @staticmethod
    def _diff(before: str, after: str) -> str:
        return '\n'.join(difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile='spec.before', tofile='spec.after', lineterm=''))

    # ---- daemon loop ------------------------------------------------------- #
    def _pending_entries(self) -> list[Path]:
        if not PENDING_DIR.exists():
            return []
        files = [p for p in PENDING_DIR.iterdir()
                 if p.is_file() and p.suffix == '.json']
        # Oldest first (spool is a FIFO queue; one gauntlet in flight at a time).
        files.sort(key=lambda p: p.stat().st_mtime)
        return files

    def process_once(self) -> int:
        """Drain the spool once. Returns the number of gauntlets concluded this
        pass. One gauntlet in flight at a time (§3.2)."""
        self._ensure_dirs()
        concluded = 0
        for pf in self._pending_entries():
            task_id = pf.stem
            try:
                with open(pf) as f:
                    entry = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.log.warning('unreadable spool entry %s — removing', pf.name)
                self._unlink(pf)
                continue
            payload_hash = entry.get('payload_hash', '')
            # Resume-safe dedup: already concluded for this exact body → just
            # clear the pending entry (host stamps from the concluded file).
            if conclusion.is_concluded(task_id, payload_hash):
                self._unlink(pf)
                continue
            self.run_gauntlet(task_id, entry)
            self._unlink(pf)
            concluded += 1
            self._heartbeat()
        return concluded

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    @staticmethod
    def _heartbeat() -> None:
        try:
            HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_FILE.write_text(str(int(time.time())))
        except OSError:
            pass


_STOP = False


def _install_signal_handlers() -> None:
    def _handler(signum, frame):  # noqa: ARG001
        global _STOP
        _STOP = True
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _configure_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true',
                        help='single drain pass then exit (tests / debugging)')
    args = parser.parse_args(argv)

    _configure_logging()
    runner = SpecReviewRunner()

    if args.once:
        runner.process_once()
        return 0

    _install_signal_handlers()
    _LOGGER.info('spec-review runner starting (poll=%ss)', POLL_INTERVAL_S)
    while not _STOP:
        # Blanket healer kill-switch parity (same file the healers honor).
        if KILL_SWITCH.exists():
            time.sleep(POLL_INTERVAL_S)
            continue
        try:
            runner.process_once()
        except Exception:  # pragma: no cover — loop must survive one bad pass
            _LOGGER.exception('process_once pass failed; continuing')
        runner._heartbeat()
        # Interruptible sleep so SIGTERM stops us promptly.
        for _ in range(POLL_INTERVAL_S):
            if _STOP:
                break
            time.sleep(1)
    _LOGGER.info('spec-review runner stopped')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
