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


def scan_jsonl_for_non_canonical_markers(path: Path) -> list[dict]:
    """Scan one session JSONL for forbidden marker shapes. Returns findings.

    Each finding is a dict with: session (path basename), shape ('wrapper'
    or 'inline-prefix'), keyword (the verdict name when known), snippet
    (first ~120 chars of the matched region for human review).
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
