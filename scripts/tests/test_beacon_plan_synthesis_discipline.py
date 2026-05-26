#!/usr/bin/env python3
"""test_beacon_plan_synthesis_discipline.py — forward-looking scanner for
Beacon plan-synthesis turns that assert current chain-state facts without
a same-turn refetch of ground truth.

Same architectural shape as `test_mirror_marker_discipline.py`: walks
Beacon's session JSONLs (default
`~/.claude/projects/-home-larry-agent-core-agents-beacon/*.jsonl`),
prints findings, and does NOT assert against the live tree. The three
2026-05-26 incidents (PR #112 stale 'pending approval', PR #113 stale
'preflight', PR #114 stale 'mid-build') are documented historical
violations, expected at PR-merge time and not treated as failure.

Detection rule per assistant turn:
  1. Does the assistant text contain a plan-synthesis assertion about
     current chain state? (Heuristic: concrete state phrases like
     'pending your approval', 'in flight', 'mid-build', 'mid-BUILD',
     'preflight', a status-table markdown bullet, or PR-number refs in
     a state-claim context.)
  2. If yes, does the SAME turn (or the small window leading up to it
     within the same JSONL) contain a refetch tool-use? (Heuristic:
     a Bash `tool_use` running `gh pr list` / `gh pr view` /
     `ls ~/agents/state/in-flight/` / `ls ~/agents/inboxes/` /
     `ps -ef` / `tail` of agent logs, OR a Glob/Read of state files
     under `~/agents/state/`.)
  3. If (1) yes and (2) no → flag as a violation.

The window is intentionally small (the assistant turn itself plus the
~5 immediately preceding messages in the same JSONL). This avoids
false-positives on long conversations where a refetch happened many
turns ago — that refetch is, by definition, stale.

False-positive control documented per dispatch guidance:
  - Bare acknowledgments ("Standing by.", "Acknowledged.") are NOT
    plan-synthesis turns — they lack concrete state assertions and do
    not flag.
  - Refetch via Glob/Read against state files counts equally with Bash
    refetch — the discipline is about freshness, not the tool name.
  - Two rapid-succession status requests where the second answer reuses
    the first's refetch may produce a false-positive; we accept this
    as the cost of catching the real failure mode (the genuine drift
    cases dominate). See PR commit message for the trade-off.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_beacon_plan_synthesis_discipline

Run the live watcher only (prints findings against real sessions):
    python3 -m unittest \
      scripts.tests.test_beacon_plan_synthesis_discipline.LiveScannerWatcher.test_print_findings_against_real_sessions
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


# Beacon's session JSONLs live under the slugified working-directory dir
# Claude Code derives from the systemd WorkingDirectory. The bot service
# (ourliberty-beacon-bot.service) sets WorkingDirectory=/home/larry/
# agent-core/agents/beacon, which slugifies to the dir name below.
BEACON_PROJECT_DIR = Path(
    os.environ.get(
        'BEACON_PROJECT_DIR',
        str(Path.home() / '.claude' / 'projects'
            / '-home-larry-agent-core-agents-beacon'),
    )
)
WITHIN_DAYS = 7
WINDOW_MESSAGES = 5

# Plan-synthesis indicators: concrete state assertions about chain
# position. We require copula-anchored state claims ("X is pending",
# "X mid-build", a status-table bullet) rather than bare vocabulary
# mentions — "Forge accepted the preflight" should not flag, but
# "PR-B is pending your approval" should. Bare conversational
# acknowledgments ("Standing by.") lack these and won't match.
_STATE_ASSERTION_PATTERNS = [
    # Copula-anchored state claims: "is/are/was/were/still/currently
    # <state>". Catches the canonical failure-mode shape.
    re.compile(
        r'\b(?:is|are|was|were|still|currently)\s+'
        r'(?:pending\s+(?:your\s+)?approval|in\s+flight|'
        r'mid[-\s]?build|in\s+preflight|queued\s+(?:behind|in))\b',
        re.IGNORECASE,
    ),
    # "X pending YOUR approval" — possessive flag captures the
    # Beacon-to-Larry pattern even without explicit copula.
    re.compile(r'\bpending\s+your\s+approval\b', re.IGNORECASE),
    # Status-table markdown: a list bullet whose label is a task or
    # PR id followed by a colon and a state word anywhere in the
    # bullet's value (limited to the line so we don't span paragraphs).
    re.compile(
        r'^[\s-]*-\s+`?(?:PR[-\s]?[A-Z0-9]+|[a-z][a-z0-9-]+)`?'
        r'(?:\s*\([^)]+\))?:[^\n]*?'
        r'\b(?:building|merged|in[- ]flight|pending|preflight|'
        r'mid[-\s]?build|queued|emitted)\b',
        re.IGNORECASE | re.MULTILINE,
    ),
]

# Refetch-evidence patterns: any tool_use whose input shape suggests a
# fresh fetch of the kind of state the assertion was about. We match on
# the Bash command string or the Read/Glob path.
_REFETCH_BASH_PATTERNS = [
    re.compile(r'\bgh\s+pr\s+(?:list|view)\b'),
    re.compile(r'\bls\s+(?:~/|/home/\w+/)?agents/state/in-flight\b'),
    re.compile(r'\bls\s+(?:~/|/home/\w+/)?agents/inboxes/\b'),
    re.compile(r'\bps\s+(?:-ef|aux)\b'),
    re.compile(r'\btail\s+.*agents/logs/\w+'),
    re.compile(r'\bcat\s+.*beacon-pending-approvals\.json'),
    re.compile(r'\bsystemctl\s+status\b'),
]
_REFETCH_PATH_RE = re.compile(
    r'agents/(?:state|inboxes)|beacon-pending-approvals\.json'
)


def _load_jsonl_entries(path: Path) -> list[dict]:
    """Read all entries from a JSONL file. Returns a list of dicts (one
    per parseable line). Silently skips malformed lines."""
    entries: list[dict] = []
    try:
        with path.open('r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def _entry_role(entry: dict) -> str:
    msg = entry.get('message')
    return msg.get('role', '') if isinstance(msg, dict) else ''


def _entry_text(entry: dict) -> str:
    """Concatenated text content from an assistant or user entry."""
    msg = entry.get('message')
    if not isinstance(msg, dict):
        return ''
    content = msg.get('content')
    if not isinstance(content, list):
        return ''
    parts = [
        c.get('text', '') for c in content
        if isinstance(c, dict) and c.get('type') == 'text'
    ]
    return '\n'.join(p for p in parts if p)


def _entry_tool_uses(entry: dict) -> list[dict]:
    """Tool-use blocks from an assistant entry. Each is the raw dict."""
    msg = entry.get('message')
    if not isinstance(msg, dict) or msg.get('role') != 'assistant':
        return []
    content = msg.get('content')
    if not isinstance(content, list):
        return []
    return [
        c for c in content
        if isinstance(c, dict) and c.get('type') == 'tool_use'
    ]


def _has_state_assertion(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _STATE_ASSERTION_PATTERNS)


def _tool_use_is_refetch(tu: dict) -> bool:
    """True if a tool_use block looks like a chain-state refetch."""
    name = tu.get('name', '')
    inp = tu.get('input', {}) or {}
    if name == 'Bash':
        cmd = inp.get('command', '') or ''
        return any(p.search(cmd) for p in _REFETCH_BASH_PATTERNS)
    if name in ('Read', 'Glob'):
        path = inp.get('file_path', '') or inp.get('pattern', '') or ''
        return bool(_REFETCH_PATH_RE.search(path))
    return False


def _window_has_refetch(
    entries: list[dict], assertion_idx: int, window: int = WINDOW_MESSAGES,
) -> bool:
    """Look at the assertion entry plus up to `window` preceding entries
    for any tool_use that matches the refetch heuristic."""
    start = max(0, assertion_idx - window)
    for i in range(start, assertion_idx + 1):
        for tu in _entry_tool_uses(entries[i]):
            if _tool_use_is_refetch(tu):
                return True
    return False


def scan_jsonl_for_stale_state_violations(path: Path) -> list[dict]:
    """Scan one session JSONL. Return one finding dict per assistant
    turn that asserts chain state without same-turn refetch evidence.

    Each finding has: session (basename), index (entry position),
    snippet (first ~140 chars of the assertion text, newlines stripped).
    """
    entries = _load_jsonl_entries(path)
    findings: list[dict] = []
    for idx, entry in enumerate(entries):
        if _entry_role(entry) != 'assistant':
            continue
        text = _entry_text(entry)
        if not _has_state_assertion(text):
            continue
        if _window_has_refetch(entries, idx):
            continue
        findings.append({
            'session': path.name,
            'index': idx,
            'snippet': text[:140].replace('\n', ' '),
        })
    return findings


def find_recent_sessions(
    project_dir: Path, within_days: int, now: float | None = None,
) -> list[Path]:
    """Return Beacon session JSONLs with mtime within `within_days`."""
    if not project_dir.is_dir():
        return []
    now = now if now is not None else time.time()
    cutoff = now - (within_days * 86400)
    recent: list[Path] = []
    for path in project_dir.glob('*.jsonl'):
        try:
            if path.stat().st_mtime >= cutoff:
                recent.append(path)
        except OSError:
            continue
    return recent


def scan_recent_sessions(
    project_dir: Path = BEACON_PROJECT_DIR,
    within_days: int = WITHIN_DAYS,
) -> list[dict]:
    """Aggregate findings across recent Beacon sessions."""
    all_findings: list[dict] = []
    for path in find_recent_sessions(project_dir, within_days):
        all_findings.extend(scan_jsonl_for_stale_state_violations(path))
    return all_findings


# -------------------- synthetic-data unit tests --------------------

def _user_line(text: str) -> str:
    return json.dumps({
        'message': {'role': 'user',
                    'content': [{'type': 'text', 'text': text}]},
    }) + '\n'


def _assistant_text_line(text: str) -> str:
    return json.dumps({
        'message': {'role': 'assistant',
                    'content': [{'type': 'text', 'text': text}]},
    }) + '\n'


def _assistant_with_tool_use_line(
    text: str, tool_name: str, tool_input: dict,
) -> str:
    return json.dumps({
        'message': {
            'role': 'assistant',
            'content': [
                {'type': 'tool_use', 'id': 'tu_x',
                 'name': tool_name, 'input': tool_input},
                {'type': 'text', 'text': text},
            ],
        },
    }) + '\n'


class ScannerUnitTests(unittest.TestCase):
    """Scanner correctness on synthetic fixtures."""

    def _write_session(self, lines: list[str]) -> Path:
        f = tempfile.NamedTemporaryFile(
            'w', suffix='.jsonl', delete=False, encoding='utf-8',
        )
        f.writelines(lines)
        f.close()
        return Path(f.name)

    def test_bare_acknowledgment_does_not_flag(self):
        path = self._write_session([
            _user_line('status?'),
            _assistant_text_line('Standing by.'),
        ])
        try:
            self.assertEqual(
                scan_jsonl_for_stale_state_violations(path), [],
            )
        finally:
            path.unlink()

    def test_state_assertion_without_refetch_flags(self):
        path = self._write_session([
            _user_line('is PR-B in flight?'),
            _assistant_text_line(
                'PR-B is pending YOUR approval, not in flight.',
            ),
        ])
        try:
            findings = scan_jsonl_for_stale_state_violations(path)
            self.assertEqual(len(findings), 1)
            self.assertIn('pending', findings[0]['snippet'].lower())
        finally:
            path.unlink()

    def test_state_assertion_with_same_turn_bash_refetch_passes(self):
        path = self._write_session([
            _user_line('is PR-B in flight?'),
            _assistant_with_tool_use_line(
                'PR-B is pending YOUR approval, not in flight.',
                'Bash',
                {'command':
                 'gh pr list --repo Larry-Yatch/ourliberty-agent-core '
                 '--state all --limit 5 --json number,state'},
            ),
        ])
        try:
            self.assertEqual(
                scan_jsonl_for_stale_state_violations(path), [],
            )
        finally:
            path.unlink()

    def test_state_assertion_with_preceding_ls_refetch_passes(self):
        # Refetch landed in an earlier assistant turn within window;
        # the next assertion should not flag.
        path = self._write_session([
            _user_line('status?'),
            _assistant_with_tool_use_line(
                'Looking now.',
                'Bash',
                {'command': 'ls ~/agents/state/in-flight/'},
            ),
            _user_line('and PR-D?'),
            _assistant_text_line(
                'PR-D is queued behind the serializer in flight.',
            ),
        ])
        try:
            self.assertEqual(
                scan_jsonl_for_stale_state_violations(path), [],
            )
        finally:
            path.unlink()

    def test_state_file_read_counts_as_refetch(self):
        # Discipline is about freshness, not tool name. A Read against
        # a state file must count equally with Bash.
        path = self._write_session([
            _user_line('any pending approvals?'),
            _assistant_with_tool_use_line(
                'Zero pending approvals.',
                'Read',
                {'file_path':
                 '/home/larry/agents/state/beacon-pending-approvals.json'},
            ),
        ])
        try:
            self.assertEqual(
                scan_jsonl_for_stale_state_violations(path), [],
            )
        finally:
            path.unlink()

    def test_refetch_outside_window_does_not_save(self):
        # If the refetch was many turns ago (older than window), the
        # later state assertion still flags. Freshness threshold.
        lines = [_user_line('hi'),
                 _assistant_with_tool_use_line(
                     'reading',
                     'Bash',
                     {'command': 'gh pr list --repo X --state all'},
                 )]
        # Push many neutral turns between refetch and assertion.
        for _ in range(WINDOW_MESSAGES + 2):
            lines.append(_user_line('ok'))
            lines.append(_assistant_text_line('Acknowledged.'))
        lines.append(_user_line('status?'))
        lines.append(_assistant_text_line(
            'PR-D is mid-build right now.',
        ))
        path = self._write_session(lines)
        try:
            findings = scan_jsonl_for_stale_state_violations(path)
            self.assertEqual(len(findings), 1)
            self.assertIn('mid-build', findings[0]['snippet'].lower())
        finally:
            path.unlink()

    def test_user_turn_text_is_ignored(self):
        # A state assertion in user content must not flag the assistant.
        path = self._write_session([
            _user_line('I think PR-B is in flight'),
            _assistant_text_line('Looking.'),
        ])
        try:
            self.assertEqual(
                scan_jsonl_for_stale_state_violations(path), [],
            )
        finally:
            path.unlink()

    def test_three_historical_incidents_all_flag(self):
        """The three 2026-05-26 fixture sessions must all flag — they
        are the canonical historical violations this discipline targets."""
        fixtures = (
            Path(__file__).resolve().parent
            / 'fixtures' / 'beacon_plan_synthesis_violations'
        )
        self.assertTrue(fixtures.is_dir(),
                        f'fixtures dir missing: {fixtures}')
        fixture_files = sorted(fixtures.glob('*.jsonl'))
        self.assertEqual(len(fixture_files), 3,
                         'expected exactly 3 historical-incident fixtures')
        for fx in fixture_files:
            findings = scan_jsonl_for_stale_state_violations(fx)
            self.assertGreaterEqual(
                len(findings), 1,
                f'fixture {fx.name} should flag at least once',
            )


class RecentSessionFilterTests(unittest.TestCase):
    """Verify the mtime window."""

    def test_recent_session_included(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sess = root / 'abc.jsonl'
            sess.write_text('')
            self.assertIn(sess, find_recent_sessions(root, within_days=7))

    def test_old_session_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sess = root / 'old.jsonl'
            sess.write_text('')
            old = time.time() - (10 * 86400)
            os.utime(sess, (old, old))
            self.assertEqual(find_recent_sessions(root, within_days=7), [])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(
            find_recent_sessions(Path('/nonexistent/path'), within_days=7),
            [],
        )


# -------------------- live watcher (prints, does not assert) --------------------

class LiveScannerWatcher(unittest.TestCase):
    """Forward-looking watcher over Beacon's real session tree. Prints
    findings; does not assert.

    At PR merge time, the three 2026-05-26 incidents (PR #112, #113,
    #114 stale-state misses) are documented historical violations and
    will appear in the live findings — that is documentation of the
    before-state, not test failure. Once those sessions roll out of the
    7-day window, a follow-up PR can convert this watcher to an
    enforcing assertion (count == 0 → pass).
    """

    def test_print_findings_against_real_sessions(self):
        findings = scan_recent_sessions(BEACON_PROJECT_DIR, WITHIN_DAYS)
        print(f'\n[beacon-plan-synthesis-discipline] scanned '
              f'{BEACON_PROJECT_DIR} within last {WITHIN_DAYS}d')
        print(f'[beacon-plan-synthesis-discipline] total findings: '
              f'{len(findings)}')
        for f in findings[:15]:
            print(f'  - session={f["session"]} idx={f["index"]} '
                  f'snippet={f["snippet"][:90]!r}')
        if len(findings) > 15:
            print(f'  ... and {len(findings) - 15} more')


if __name__ == '__main__':
    unittest.main()
