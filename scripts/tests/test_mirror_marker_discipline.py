#!/usr/bin/env python3
"""test_mirror_marker_discipline.py — forward-looking Mirror marker-shape scanner.

Forward-looking scanner. At PR merge: expect 4 historical violations from
2026-05-25 PR #101 + PR #104 r1/r2/r3 reviews — that is documentation of
the before-state, not failure. Hard-fail enforcement deferred until
historical non-canonical sessions roll out of the 7-day window.

What this catches:

  Canonical (what agents/mirror/CLAUDE.md mandates, produced by marker.py):
      === REVIEW_PASS ===
      { ... }
      === END_REVIEW_PASS ===

  Non-canonical shape #1 — wrapper (silently dropped by outbox-notifier parser):
      === REVIEW_RESULT ===
      {"verdict": "...", ...}
      === END_REVIEW_RESULT ===

  Non-canonical shape #2 — inline-prefix (also silently dropped):
      REVIEW_PASS:
      ```json
      { ... }
      ```

The scanner walks `~/.claude/projects/*/<session_id>.jsonl`, picks sessions
whose mtime is within `WITHIN_DAYS` (default 7), iterates every assistant
turn, and matches text against forbidden patterns. Findings are printed
(not asserted) — the test is a regression watcher, not an enforcement gate.

Why a watcher and not a gate: when this lands, the historical PR #101 +
PR #104 sessions are still inside the 7-day window. A hard-fail would
self-trigger on its own merge. Once those sessions age out, a follow-up
PR can flip the watcher to an enforcing assertion.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_mirror_marker_discipline

Run with the scanner only (to see live findings against your real sessions):
    python3 -m unittest \
      scripts.tests.test_mirror_marker_discipline.LiveScannerWatcher.test_print_findings_against_real_sessions
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))


CLAUDE_PROJECTS_ROOT = Path(
    os.environ.get('CLAUDE_PROJECTS_ROOT', str(Path.home() / '.claude' / 'projects'))
)
WITHIN_DAYS = 7

# Non-canonical shape #1: the "wrapper" form. Outbox-notifier parser does
# not recognize REVIEW_RESULT — only REVIEW_PASS / REVIEW_REVISION /
# REVIEW_ESCALATE / REVIEW_EMERGENCY_HALT.
_WRAPPER_RE = re.compile(
    r'===\s*REVIEW_RESULT\s*===\s*(\{.*?\})\s*===\s*END_REVIEW_RESULT\s*===',
    re.DOTALL,
)

# Non-canonical shape #2: the "inline-prefix" form. A verdict keyword
# followed by a colon and a code-fenced JSON block. Outbox-notifier parser
# misses this entirely because it has no `=== KEYWORD ===` delimiters.
# Multiline match: keyword on one line, then any whitespace, then a fence.
_INLINE_PREFIX_RE = re.compile(
    r'\b(REVIEW_PASS|REVIEW_REVISION|REVIEW_ESCALATE|REVIEW_EMERGENCY_HALT)'
    r':\s*\n\s*```(?:json)?\s*\n',
    re.MULTILINE,
)

# Chain discipline v3 GAP 4 (2026-05-26) — non-canonical shape #3: the
# "bare keyword" form. A verdict keyword in narrative text with no
# `=== ... ===` delimiters and no JSON body at all. PR #107 review
# emitted this; parser walks for canonical marker.py output and finds
# nothing.
#
# Detection MUST be conservative — assistant turns regularly discuss
# verdict keywords in prose ("I considered REVIEW_REVISION but…",
# "REVIEW_PASS when the diff is clean") and those are not violations.
# Only flag when the keyword sits in verdict-emission position:
#   - Alone on its own line (line content == keyword, ± trailing punct).
#   - Preceded by a "Verdict:" / "My verdict:" / "Marker:" / "Decision:"
#     prefix on the same line.
# A canonical `=== KEYWORD === ... === END_KEYWORD ===` block ANYWHERE
# in the same turn fully suppresses bare-keyword findings for that
# keyword — the marker was emitted correctly and the bare mention is
# narrative reference to it.
_BARE_VERDICT_LINE_RE = re.compile(
    r'^[ \t]*'
    r'(?:(?:Verdict|My verdict|Marker|Decision)\s*:\s*)?'
    r'(REVIEW_PASS|REVIEW_REVISION|REVIEW_ESCALATE|REVIEW_EMERGENCY_HALT)'
    r'[ \t]*[.,;!]?[ \t]*$',
    re.MULTILINE,
)
_CANONICAL_BLOCK_RE = re.compile(
    r'===\s*(REVIEW_PASS|REVIEW_REVISION|REVIEW_ESCALATE|REVIEW_EMERGENCY_HALT)\s*===',
)

# Chain discipline v3 GAP 4 (2026-05-26) — non-canonical shape #4: the
# "task_id mismatch" form. A canonical block whose JSON `task_id` field
# does not match the review-request envelope's task_id. PR #109 review
# emitted this shape. Detection here is HEURISTIC: we extract the
# task_id from the canonical block AND compare against any task_id
# observable in the session source (the envelope's task_id is not
# present in the JSONL; the assistant typically echoes it in the
# narrative preceding the marker). False-positive guard: if the
# narrative contains the marker's task_id verbatim AT LEAST ONCE before
# the marker, we treat the marker's task_id as confirmed (no mismatch).
# Otherwise, flag the marker for human review.
_CANONICAL_REVIEW_BLOCK_RE = re.compile(
    r'===\s*(REVIEW_PASS|REVIEW_REVISION|REVIEW_ESCALATE|REVIEW_EMERGENCY_HALT)\s*==='
    r'\s*(\{.*?\})\s*'
    r'===\s*END_(?:REVIEW_PASS|REVIEW_REVISION|REVIEW_ESCALATE|REVIEW_EMERGENCY_HALT)\s*===',
    re.DOTALL,
)


def _extract_assistant_text(line: str) -> str:
    """Return concatenated text from a single JSONL assistant turn, or ''."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return ''
    msg = entry.get('message')
    if not isinstance(msg, dict) or msg.get('role') != 'assistant':
        return ''
    content = msg.get('content')
    if not isinstance(content, list):
        return ''
    parts = [
        c.get('text', '') for c in content
        if isinstance(c, dict) and c.get('type') == 'text'
    ]
    return '\n'.join(p for p in parts if p)


def _scan_text_for_bare_keyword(text: str) -> list[tuple[int, str]]:
    """Return [(start_offset, keyword)] for bare-keyword shapes — verdict
    keywords in verdict-emission position (alone on a line, or after a
    `Verdict:`/`My verdict:`/`Marker:`/`Decision:` prefix) with no
    `=== ... ===` delimiters. False positives suppressed when ANY
    canonical block for the same keyword appears in the same text.

    Chain discipline v3 GAP 4 shape #3 (bare keyword)."""
    canonical_keywords = {m.group(1) for m in _CANONICAL_BLOCK_RE.finditer(text)}
    hits: list[tuple[int, str]] = []
    for m in _BARE_VERDICT_LINE_RE.finditer(text):
        kw = m.group(1)
        if kw in canonical_keywords:
            continue
        hits.append((m.start(), kw))
    return hits


_TASK_ID_MIN_LEN_FOR_MISMATCH_CHECK = 8
_NARRATIVE_MIN_LEN_FOR_MISMATCH_CHECK = 150


def _scan_text_for_task_id_mismatch(text: str) -> list[dict]:
    """Return [{keyword, marker_task_id, snippet}] for canonical blocks
    whose JSON `task_id` is not echoed in the narrative preceding the
    block. Heuristic — the envelope's task_id is not in the JSONL, so we
    use the narrative as a proxy.

    Conservative gating to keep false-positives low against live session
    scans:
      - Skip when the marker's task_id is shorter than
        `_TASK_ID_MIN_LEN_FOR_MISMATCH_CHECK` (single-letter / synthetic
        test IDs aren't realistic; real task_ids are slug-shaped).
      - Skip when the narrative preceding the marker is shorter than
        `_NARRATIVE_MIN_LEN_FOR_MISMATCH_CHECK` (no room for the
        assistant to have echoed the envelope's task_id).

    Chain discipline v3 GAP 4 shape #4 (task_id mismatch)."""
    findings: list[dict] = []
    for m in _CANONICAL_REVIEW_BLOCK_RE.finditer(text):
        keyword = m.group(1)
        body = m.group(2)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        marker_task = payload.get('task_id')
        if not isinstance(marker_task, str) or not marker_task:
            continue
        if len(marker_task) < _TASK_ID_MIN_LEN_FOR_MISMATCH_CHECK:
            continue
        narrative_before = text[:m.start()]
        if len(narrative_before) < _NARRATIVE_MIN_LEN_FOR_MISMATCH_CHECK:
            continue
        if marker_task in narrative_before:
            continue
        findings.append({
            'keyword': keyword,
            'marker_task_id': marker_task,
            'snippet': text[m.start():m.start() + 200].replace('\n', ' '),
        })
    return findings


def scan_jsonl_for_non_canonical_markers(path: Path) -> list[dict]:
    """Scan one session JSONL for forbidden marker shapes. Returns findings.

    Each finding is a dict with: session (path basename), shape (one of
    'wrapper', 'inline-prefix', 'bare-keyword', 'task-id-mismatch'),
    keyword (the verdict name when known), snippet (first ~120 chars of
    the matched region for human review).
    """
    findings: list[dict] = []
    try:
        with path.open('r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                text = _extract_assistant_text(line)
                if not text:
                    continue
                for m in _WRAPPER_RE.finditer(text):
                    findings.append({
                        'session': path.name,
                        'shape': 'wrapper',
                        'keyword': 'REVIEW_RESULT',
                        'snippet': text[m.start():m.start() + 120].replace('\n', ' '),
                    })
                for m in _INLINE_PREFIX_RE.finditer(text):
                    findings.append({
                        'session': path.name,
                        'shape': 'inline-prefix',
                        'keyword': m.group(1),
                        'snippet': text[m.start():m.start() + 120].replace('\n', ' '),
                    })
                for offset, kw in _scan_text_for_bare_keyword(text):
                    findings.append({
                        'session': path.name,
                        'shape': 'bare-keyword',
                        'keyword': kw,
                        'snippet': text[offset:offset + 120].replace('\n', ' '),
                    })
                for hit in _scan_text_for_task_id_mismatch(text):
                    findings.append({
                        'session': path.name,
                        'shape': 'task-id-mismatch',
                        'keyword': hit['keyword'],
                        'marker_task_id': hit['marker_task_id'],
                        'snippet': hit['snippet'],
                    })
    except OSError:
        pass
    return findings


def find_recent_sessions(
    projects_root: Path, within_days: int, now: float | None = None,
) -> list[Path]:
    """Return session JSONL paths with mtime within `within_days` of now."""
    if not projects_root.is_dir():
        return []
    now = now if now is not None else time.time()
    cutoff = now - (within_days * 86400)
    recent: list[Path] = []
    for path in projects_root.glob('*/*.jsonl'):
        try:
            if path.stat().st_mtime >= cutoff:
                recent.append(path)
        except OSError:
            continue
    return recent


def scan_recent_sessions(
    projects_root: Path = CLAUDE_PROJECTS_ROOT,
    within_days: int = WITHIN_DAYS,
) -> list[dict]:
    """Aggregate findings across every recent session under projects_root."""
    all_findings: list[dict] = []
    for path in find_recent_sessions(projects_root, within_days):
        all_findings.extend(scan_jsonl_for_non_canonical_markers(path))
    return all_findings


# -------------------- synthetic-data unit tests --------------------

def _assistant_jsonl_line(text: str) -> str:
    """Build a single JSONL line shaped like a Claude assistant turn."""
    record = {
        'message': {
            'role': 'assistant',
            'content': [{'type': 'text', 'text': text}],
        },
    }
    return json.dumps(record) + '\n'


class ScannerUnitTests(unittest.TestCase):
    """Scanner correctness — synthetic JSONLs, no live filesystem reads."""

    def test_canonical_marker_produces_no_findings(self):
        canonical = (
            'Review verdict.\n\n'
            '=== REVIEW_PASS ===\n'
            '{"task_id": "x", "pr_url": "https://x/1", "summary": "ok"}\n'
            '=== END_REVIEW_PASS ===\n'
        )
        with tempfile.NamedTemporaryFile(
            'w', suffix='.jsonl', delete=False,
        ) as f:
            f.write(_assistant_jsonl_line(canonical))
            path = Path(f.name)
        try:
            self.assertEqual(scan_jsonl_for_non_canonical_markers(path), [])
        finally:
            path.unlink()

    def test_wrapper_shape_is_flagged(self):
        wrapper = (
            'Review verdict.\n\n'
            '=== REVIEW_RESULT ===\n'
            '{"verdict": "pass", "summary": "looks good"}\n'
            '=== END_REVIEW_RESULT ===\n'
        )
        with tempfile.NamedTemporaryFile(
            'w', suffix='.jsonl', delete=False,
        ) as f:
            f.write(_assistant_jsonl_line(wrapper))
            path = Path(f.name)
        try:
            findings = scan_jsonl_for_non_canonical_markers(path)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]['shape'], 'wrapper')
            self.assertEqual(findings[0]['keyword'], 'REVIEW_RESULT')
        finally:
            path.unlink()

    def test_inline_prefix_review_pass_is_flagged(self):
        inline = (
            'Review verdict.\n\n'
            'REVIEW_PASS:\n'
            '```json\n'
            '{"task_id": "x", "pr_url": "https://x/1", "summary": "ok"}\n'
            '```\n'
        )
        with tempfile.NamedTemporaryFile(
            'w', suffix='.jsonl', delete=False,
        ) as f:
            f.write(_assistant_jsonl_line(inline))
            path = Path(f.name)
        try:
            findings = scan_jsonl_for_non_canonical_markers(path)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]['shape'], 'inline-prefix')
            self.assertEqual(findings[0]['keyword'], 'REVIEW_PASS')
        finally:
            path.unlink()

    def test_inline_prefix_other_verdicts_flagged(self):
        for kw in ('REVIEW_REVISION', 'REVIEW_ESCALATE', 'REVIEW_EMERGENCY_HALT'):
            inline = (
                f'Review.\n\n{kw}:\n'
                '```json\n{"task_id": "x"}\n```\n'
            )
            with tempfile.NamedTemporaryFile(
                'w', suffix='.jsonl', delete=False,
            ) as f:
                f.write(_assistant_jsonl_line(inline))
                path = Path(f.name)
            try:
                findings = scan_jsonl_for_non_canonical_markers(path)
                self.assertEqual(len(findings), 1, f'kw={kw}')
                self.assertEqual(findings[0]['shape'], 'inline-prefix')
                self.assertEqual(findings[0]['keyword'], kw)
            finally:
                path.unlink()

    def test_prose_mentioning_keywords_is_not_flagged(self):
        prose = (
            'I considered REVIEW_REVISION but the diff is clean. '
            'I considered REVIEW_RESULT as a wrapper but that is not canonical.\n'
        )
        with tempfile.NamedTemporaryFile(
            'w', suffix='.jsonl', delete=False,
        ) as f:
            f.write(_assistant_jsonl_line(prose))
            path = Path(f.name)
        try:
            self.assertEqual(scan_jsonl_for_non_canonical_markers(path), [])
        finally:
            path.unlink()

    def test_user_turn_is_ignored(self):
        # Non-assistant turns must not produce findings.
        record = {
            'message': {
                'role': 'user',
                'content': [{'type': 'text', 'text':
                    '=== REVIEW_RESULT ===\n{"x": 1}\n=== END_REVIEW_RESULT ==='
                }],
            },
        }
        with tempfile.NamedTemporaryFile(
            'w', suffix='.jsonl', delete=False,
        ) as f:
            f.write(json.dumps(record) + '\n')
            path = Path(f.name)
        try:
            self.assertEqual(scan_jsonl_for_non_canonical_markers(path), [])
        finally:
            path.unlink()


class BareKeywordShapeTests(unittest.TestCase):
    """Chain discipline v3 GAP 4 shape #3 — bare keyword without delimiters."""

    def test_bare_review_pass_is_flagged(self):
        text = (
            'Reviewed the PR. Spec coverage clean.\n\n'
            'Verdict: REVIEW_PASS\n\n'
            'Larry should merge manually.'
        )
        hits = _scan_text_for_bare_keyword(text)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], 'REVIEW_PASS')

    def test_bare_keyword_in_assistant_turn_flagged_via_scanner(self):
        text = (
            'Review summary: clean diff, no findings.\n\n'
            'Verdict: REVIEW_REVISION\n'
        )
        with tempfile.NamedTemporaryFile(
            'w', suffix='.jsonl', delete=False,
        ) as f:
            f.write(_assistant_jsonl_line(text))
            path = Path(f.name)
        try:
            findings = scan_jsonl_for_non_canonical_markers(path)
            shapes = [f['shape'] for f in findings]
            self.assertIn('bare-keyword', shapes)
        finally:
            path.unlink()

    def test_prose_mention_not_flagged_as_bare_keyword(self):
        """Verdict keywords appearing in prose discussion (not in
        verdict-emission position) must not be flagged."""
        text = (
            'I considered REVIEW_REVISION but the diff is clean. Going with PASS.\n\n'
            '=== REVIEW_PASS ===\n'
            '{"task_id": "x", "pr_url": "https://x/1", "summary": "ok"}\n'
            '=== END_REVIEW_PASS ===\n'
        )
        hits = _scan_text_for_bare_keyword(text)
        # Prose mention of REVIEW_REVISION is NOT verdict-emission position;
        # canonical block suppresses REVIEW_PASS. Result: zero findings.
        self.assertEqual(hits, [])

    def test_verdict_prefix_line_is_flagged(self):
        """A `Verdict: REVIEW_PASS` line with no canonical block flags."""
        text = 'Review summary above.\n\nVerdict: REVIEW_PASS\n'
        hits = _scan_text_for_bare_keyword(text)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], 'REVIEW_PASS')

    def test_pr107_bare_keyword_fixture_is_flagged(self):
        """Fixture for the actual PR #107 shape — bare REVIEW_PASS keyword
        in narrative with no delimiters and no JSON body."""
        fixture = (
            Path(__file__).resolve().parent / 'fixtures'
            / 'mirror_discipline_violations' / 'bare_keyword_pr107.jsonl'
        )
        self.assertTrue(fixture.exists(), f'missing fixture: {fixture}')
        findings = scan_jsonl_for_non_canonical_markers(fixture)
        shapes = [f['shape'] for f in findings]
        self.assertIn('bare-keyword', shapes)


class TaskIdMismatchShapeTests(unittest.TestCase):
    """Chain discipline v3 GAP 4 shape #4 — canonical block whose JSON
    task_id is not echoed in the narrative preceding the marker."""

    def test_mismatch_when_marker_task_id_absent_from_narrative(self):
        # Narrative ≥150 chars + marker task_id ≥8 chars; the assistant
        # reviewed a different task than the marker claims.
        text = (
            'I reviewed the PR thoroughly. AC coverage is clean, no must-fix '
            'findings. The diff matches the spec exactly and the tests pass. '
            'Confidence is high. Emitting REVIEW_PASS now.\n\n'
            '=== REVIEW_PASS ===\n'
            '{"task_id": "stale-task-from-previous-session-001", '
            '"pr_url": "https://x/1", "summary": "ok"}\n'
            '=== END_REVIEW_PASS ===\n'
        )
        findings = _scan_text_for_task_id_mismatch(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0]['marker_task_id'],
            'stale-task-from-previous-session-001',
        )

    def test_no_mismatch_when_task_id_echoed_in_narrative(self):
        text = (
            'I reviewed task `build-feature-x-001` thoroughly. AC coverage '
            'is clean, no must-fix findings. The diff matches the spec '
            'exactly and the tests pass. Confidence is high. Emitting '
            'REVIEW_PASS now.\n\n'
            '=== REVIEW_PASS ===\n'
            '{"task_id": "build-feature-x-001", '
            '"pr_url": "https://x/1", "summary": "ok"}\n'
            '=== END_REVIEW_PASS ===\n'
        )
        findings = _scan_text_for_task_id_mismatch(text)
        self.assertEqual(findings, [])

    def test_short_narrative_not_flagged_to_avoid_false_positives(self):
        """When the narrative preceding the marker is short, the assistant
        had no room to echo the envelope's task_id even if it intended to.
        Skip these to keep false-positives low."""
        text = (
            'Review verdict.\n\n'
            '=== REVIEW_PASS ===\n'
            '{"task_id": "real-looking-task-id-001", '
            '"pr_url": "https://x/1", "summary": "ok"}\n'
            '=== END_REVIEW_PASS ===\n'
        )
        findings = _scan_text_for_task_id_mismatch(text)
        self.assertEqual(findings, [])

    def test_pr109_task_id_mismatch_fixture_is_flagged(self):
        fixture = (
            Path(__file__).resolve().parent / 'fixtures'
            / 'mirror_discipline_violations' / 'task_id_mismatch_pr109.jsonl'
        )
        self.assertTrue(fixture.exists(), f'missing fixture: {fixture}')
        findings = scan_jsonl_for_non_canonical_markers(fixture)
        shapes = [f['shape'] for f in findings]
        self.assertIn('task-id-mismatch', shapes)


class RecentSessionFilterTests(unittest.TestCase):
    """Verify the mtime window filters out sessions older than WITHIN_DAYS."""

    def test_recent_session_is_included(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = root / 'proj1'
            proj.mkdir()
            sess = proj / 'abc.jsonl'
            sess.write_text('')
            # mtime is now by default — within window.
            paths = find_recent_sessions(root, within_days=7)
            self.assertIn(sess, paths)

    def test_old_session_is_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = root / 'proj1'
            proj.mkdir()
            sess = proj / 'old.jsonl'
            sess.write_text('')
            # Backdate mtime to 10 days ago.
            old = time.time() - (10 * 86400)
            os.utime(sess, (old, old))
            self.assertEqual(find_recent_sessions(root, within_days=7), [])

    def test_missing_root_returns_empty(self):
        self.assertEqual(
            find_recent_sessions(Path('/nonexistent/path'), within_days=7), [],
        )


# -------------------- live watcher (prints, does not assert) --------------------

class LiveScannerWatcher(unittest.TestCase):
    """Forward-looking watcher over the real `~/.claude/projects/` tree.

    Prints findings to stdout for visibility. Does NOT assert. At PR merge
    time, expect 4 historical violations from 2026-05-25 PR #101 + PR #104
    r1/r2/r3 reviews — those are documentation of the before-state, not
    failure. Once those sessions roll out of the 7-day window, a follow-up
    PR can convert the print loop into an assertion.
    """

    def test_print_findings_against_real_sessions(self):
        findings = scan_recent_sessions(CLAUDE_PROJECTS_ROOT, WITHIN_DAYS)
        # Group by shape for readable output.
        by_shape: dict[str, list[dict]] = {}
        for f in findings:
            by_shape.setdefault(f['shape'], []).append(f)
        print(f'\n[mirror-marker-discipline] scanned '
              f'{CLAUDE_PROJECTS_ROOT} within last {WITHIN_DAYS}d')
        print(f'[mirror-marker-discipline] total findings: {len(findings)}')
        for shape, items in sorted(by_shape.items()):
            print(f'  shape={shape}: {len(items)}')
            for f in items[:10]:
                print(f'    - session={f["session"]} '
                      f'keyword={f["keyword"]} snippet={f["snippet"][:80]!r}')
            if len(items) > 10:
                print(f'    ... and {len(items) - 10} more')


if __name__ == '__main__':
    unittest.main()
