"""Tests for scripts/heal_wedged_review_sessions.py.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Coverage (per spec deliverables):
- Case 1 provably-done auto-reap (marker present + idle past grace)
- Case 2 silent session → alert-only (no kill) while not graduated
- false-positive demote (alerted session emits a marker / resumes → streak reset)
- true-positive promote (3 consecutive true positives → graduate to auto-reap)
- kill-switch honored
- no-double-kill vs heal_zombie_main_workers (/tmp/wt-main-* + (deleted) ignored)
- config load (defaults on missing/garbage; valid override)
- pure classify() verdicts
- tail-read streak / mode helpers
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Import-time sandbox (canonical Gap-A shape, see scripts/tests/conftest.py):
# the healer's transitive imports freeze AGENTS_ROOT-derived paths at import,
# so the env must be set BEFORE the import below. Guarded so the #436 gate
# env / tests/__init__.py (and any outer harness) win. Interim until the
# per-module bootstrap (docs/test-jail-spec.md Layer A) lands.
if not os.environ.get('OURLIBERTY_AGENTS_ROOT'):
    _SANDBOX_ROOT = tempfile.mkdtemp(prefix='ol-test-agents-root-')
    os.makedirs(os.path.join(_SANDBOX_ROOT, 'logs'), exist_ok=True)
    os.environ['OURLIBERTY_AGENTS_ROOT'] = _SANDBOX_ROOT
    os.environ.setdefault(
        'OURLIBERTY_WORKTREES_ROOT', os.path.join(_SANDBOX_ROOT, 'worktrees'))
    os.environ.setdefault(
        'OURLIBERTY_LOG_DIR', os.path.join(_SANDBOX_ROOT, 'logs'))

import heal_wedged_review_sessions as h  # noqa: E402


NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
CFG = {
    'enabled': True,
    'marker_grace_seconds': 300,
    'silent_grace_seconds': 900,
    'hard_silent_grace_seconds': 3600,
    'streak_to_promote': 3,
    'build_handoff_grace_seconds': 300,
}


def _cand(*, pid=1000, tier='mirror', name='build-x', idle=0.0,
          marker=False, has_jsonl=True, session_id='sess-1', cwd=None):
    cwd = cwd or f'/home/larry/agent-worktrees/wt-{tier}-{name}'
    jsonl = Path(f'/tmp/{session_id}.jsonl') if has_jsonl else None
    return h.Candidate(
        pid=pid, cwd=cwd, tier=tier, jsonl=jsonl,
        session_id=session_id if has_jsonl else None,
        idle_secs=idle, marker_present=marker,
    )


class _Recorder:
    """Captures reaper / notify calls without touching /proc, git, or network."""

    def __init__(self):
        self.killed: list[int] = []
        self.removed: list[str] = []
        self.closures: list[tuple[str, str]] = []
        self.escalations: list[tuple[str, str, str]] = []

    def reaper(self, pid: int) -> bool:
        self.killed.append(pid)
        return True

    def remover(self, cwd: str) -> bool:
        self.removed.append(cwd)
        return True

    def closure(self, message: str, subject: str) -> None:
        self.closures.append((message, subject))

    def escalate(self, message: str, subject: str, action: str) -> None:
        self.escalations.append((message, subject, action))


def _run(rec, candidates, state, cfg=CFG, verify_pid_fn=None):
    # By default the pre-kill identity recheck must SUCCEED for the synthetic
    # candidates (their pids aren't real, so the production proc_cwd would abort
    # every kill). Map each candidate's pid back to its cwd so the recheck
    # passes; individual tests override verify_pid_fn to exercise the abort.
    if verify_pid_fn is None:
        _cwd_by_pid = {c.pid: c.cwd for c in (candidates or [])}
        verify_pid_fn = lambda pid: _cwd_by_pid.get(pid)  # noqa: E731
    return h.run_cycle(
        candidates=candidates, state=state, cfg=cfg, now=NOW,
        reaper=rec.reaper, worktree_remover=rec.remover,
        closure_notify=rec.closure, escalate_notify=rec.escalate,
        event_emitter=lambda **kw: None,  # never touch the live chain_events table
        verify_pid_fn=verify_pid_fn,
        persist=False,
    )


# ---------------------- pure classify ----------------------

class TestClassify(unittest.TestCase):
    def test_marker_present_idle_past_grace_is_case1(self):
        self.assertEqual(
            h.classify(marker_present=True, idle_secs=600, cfg=CFG), h.REAP_CASE1)

    def test_marker_present_but_fresh_is_skip(self):
        self.assertEqual(
            h.classify(marker_present=True, idle_secs=120, cfg=CFG), h.SKIP)

    def test_no_marker_idle_past_silent_is_case2(self):
        self.assertEqual(
            h.classify(marker_present=False, idle_secs=1200, cfg=CFG), h.SILENT_CASE2)

    def test_no_marker_fresh_is_skip(self):
        self.assertEqual(
            h.classify(marker_present=False, idle_secs=300, cfg=CFG), h.SKIP)

    def test_no_marker_idle_past_hard_is_hard_reap(self):
        # 60+ min silent with no marker → deterministic backstop, not the
        # confidence-ladder alert path.
        self.assertEqual(
            h.classify(marker_present=False, idle_secs=4000, cfg=CFG),
            h.REAP_CASE2_HARD)

    def test_hard_gate_takes_precedence_over_silent(self):
        # Even if misconfigured so hard <= silent, the hard verdict wins.
        cfg = dict(CFG, silent_grace_seconds=5000, hard_silent_grace_seconds=1000)
        self.assertEqual(
            h.classify(marker_present=False, idle_secs=2000, cfg=cfg),
            h.REAP_CASE2_HARD)

    def test_marker_present_never_hard_reaps(self):
        # A provably-done session (marker) is Case 1 regardless of how long it
        # has been idle — the hard backstop only applies to NO-marker sessions.
        self.assertEqual(
            h.classify(marker_present=True, idle_secs=9999, cfg=CFG), h.REAP_CASE1)


# ---------------------- Case 1 ----------------------

class TestCase1AutoReap(unittest.TestCase):
    def test_provably_done_session_is_reaped_day_one(self):
        rec = _Recorder()
        state = h.ConfidenceState()  # fresh: alert-only, no graduation needed
        cand = _cand(pid=4242, marker=True, idle=600)
        summary = _run(rec, [cand], state)
        self.assertEqual(summary['case1_reaped'], 1)
        self.assertEqual(rec.killed, [4242])
        self.assertEqual(rec.removed, [cand.cwd])
        self.assertEqual(len(rec.closures), 1)  # CLOSURE notify on reap

    def test_fresh_marker_session_not_reaped(self):
        rec = _Recorder()
        cand = _cand(marker=True, idle=120)
        summary = _run(rec, [cand], h.ConfidenceState())
        self.assertEqual(summary['case1_reaped'], 0)
        self.assertEqual(rec.killed, [])


# ---------------------- Case 2 alert-only ----------------------

class TestCase2AlertOnly(unittest.TestCase):
    def test_silent_session_alerts_but_does_not_kill(self):
        rec = _Recorder()
        state = h.ConfidenceState()
        cand = _cand(pid=5000, marker=False, idle=1200, session_id='silent-1')
        summary = _run(rec, [cand], state)
        self.assertEqual(summary['case2_alerted'], 1)
        self.assertEqual(rec.killed, [])  # DO NOT kill in alert-only
        self.assertEqual(len(rec.escalations), 1)
        self.assertIn('silent-1', state.pending)

    def test_already_pending_session_not_re_alerted(self):
        rec = _Recorder()
        state = h.ConfidenceState(pending={'silent-1': {
            'first_alert_ts': NOW.isoformat(),
            'jsonl_path': '/tmp/silent-1.jsonl',
            'jsonl_mtime_at_alert': NOW.timestamp(),
            'cwd': '/home/larry/agent-worktrees/wt-mirror-build-x',
            'tier': 'mirror',
        }})
        # session still alive (in candidates) and unchanged → stays pending,
        # no new alert.
        cand = _cand(pid=5000, marker=False, idle=1200, session_id='silent-1',
                     name='build-x')
        summary = _run(rec, [cand], state)
        self.assertEqual(summary['case2_alerted'], 0)


# ---------------------- confidence ladder ----------------------

class TestTruePositivePromotion(unittest.TestCase):
    def _pending_entry(self, session_id, cwd):
        return {
            'first_alert_ts': NOW.isoformat(),
            'jsonl_path': f'/tmp/{session_id}.jsonl',  # does not exist → no marker
            'jsonl_mtime_at_alert': NOW.timestamp(),
            'cwd': cwd, 'tier': 'mirror',
        }

    def test_three_true_positives_graduate_to_auto_reap(self):
        rec = _Recorder()
        # Two prior true positives already recorded.
        state = h.ConfidenceState(
            mode=h.MODE_ALERT_ONLY,
            executions=[
                {'outcome': h.TRUE_POSITIVE, 'session_id': 'a'},
                {'outcome': h.TRUE_POSITIVE, 'session_id': 'b'},
            ],
            pending={'c': self._pending_entry('c', '/home/larry/agent-worktrees/wt-mirror-c')},
        )
        # 'c' is gone (not in candidates) and its jsonl path doesn't exist →
        # no marker, process dead → third TRUE positive → graduate.
        summary = _run(rec, [], state)
        self.assertEqual(summary['true_positives'], 1)
        self.assertEqual(state.streak, 3)
        self.assertEqual(state.mode, h.MODE_AUTO_REAP)
        self.assertEqual(summary['mode_after'], h.MODE_AUTO_REAP)
        # one-time graduation CLOSURE notify
        self.assertTrue(any('graduated' in m.lower() for m, _ in rec.closures))

    def test_graduated_mode_auto_reaps_silent_session(self):
        rec = _Recorder()
        state = h.ConfidenceState(
            mode=h.MODE_AUTO_REAP,
            executions=[{'outcome': h.TRUE_POSITIVE, 'session_id': s}
                        for s in ('a', 'b', 'c')],
        )
        cand = _cand(pid=7000, marker=False, idle=1200, session_id='silent-new',
                     has_jsonl=True)
        # jsonl path /tmp/silent-new.jsonl doesn't exist → _resumed_since_scan
        # returns False → reap proceeds.
        summary = _run(rec, [cand], state)
        self.assertEqual(summary['case2_auto_reaped'], 1)
        self.assertEqual(rec.killed, [7000])


class TestCase2HardBackstop(unittest.TestCase):
    """The deterministic hard-silent backstop — reaps a provably-wedged
    no-marker session on the FIRST occurrence, independent of the confidence
    ladder. This is the gap Pulse flagged on PR #334: the healer could warn at
    15 min but not act until the streak graduated."""

    def test_ungraduated_session_past_hard_grace_is_reaped_immediately(self):
        rec = _Recorder()
        # Fresh state: alert-only, NO prior true positives — the streak path
        # would only warn here. The hard backstop must still kill.
        state = h.ConfidenceState(mode=h.MODE_ALERT_ONLY, executions=[], pending={})
        cand = _cand(pid=8100, marker=False, idle=4000, session_id='wedged-hard',
                     has_jsonl=True)  # /tmp/wedged-hard.jsonl absent → no resume
        summary = _run(rec, [cand], state)
        self.assertEqual(summary['case2_hard_reaped'], 1)
        self.assertEqual(rec.killed, [8100])
        # It did NOT need graduation and did NOT silently graduate the mode.
        self.assertEqual(state.mode, h.MODE_ALERT_ONLY)
        self.assertEqual(summary['case2_auto_reaped'], 0)
        self.assertEqual(summary['case2_alerted'], 0)
        # closure notify uses the reaped subject family.
        self.assertTrue(any('reaped' in subj or 'wedged-review-reaped' in subj
                            for _, subj in rec.closures))

    def test_hard_reap_aborts_if_session_resumed_at_gate(self):
        # Idle classification says hard-reap, but the JSONL has advanced since
        # the scan (real fresh file) → live work → must NOT kill.
        rec = _Recorder()
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            jsonl = Path(td) / 'resumed.jsonl'
            jsonl.write_text('still working, no marker yet')  # fresh mtime ≈ now
            cand = h.Candidate(
                pid=8200, cwd='/home/larry/agent-worktrees/wt-mirror-resumed',
                tier='mirror', jsonl=jsonl, session_id='resumed',
                idle_secs=4000, marker_present=False,
            )
            state = h.ConfidenceState(mode=h.MODE_ALERT_ONLY, executions=[], pending={})
            summary = _run(rec, [cand], state)
        self.assertEqual(summary['case2_hard_reaped'], 0)
        self.assertEqual(rec.killed, [])

    def test_hard_reap_independent_of_graduated_mode(self):
        # Belt-and-suspenders: even in auto-reap mode the hard path is the one
        # that fires for a >hard-grace session (and only kills once).
        rec = _Recorder()
        state = h.ConfidenceState(
            mode=h.MODE_AUTO_REAP,
            executions=[{'outcome': h.TRUE_POSITIVE, 'session_id': s}
                        for s in ('a', 'b', 'c')])
        cand = _cand(pid=8300, marker=False, idle=5000, session_id='hard-grad',
                     has_jsonl=True)
        summary = _run(rec, [cand], state)
        self.assertEqual(summary['case2_hard_reaped'], 1)
        self.assertEqual(summary['case2_auto_reaped'], 0)
        self.assertEqual(rec.killed, [8300])


class TestFalsePositiveDemote(unittest.TestCase):
    def test_pending_session_that_emits_marker_is_false_positive(self):
        rec = _Recorder()
        # Write a JSONL that now contains a terminal marker.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            jsonl = Path(td) / 'fp.jsonl'
            jsonl.write_text(_assistant_line(
                '=== REVIEW_PASS ===\n{"ok":1}\n=== END_REVIEW_PASS ===') + '\n')
            state = h.ConfidenceState(
                mode=h.MODE_AUTO_REAP,
                executions=[{'outcome': h.TRUE_POSITIVE, 'session_id': s}
                            for s in ('a', 'b', 'c')],
                pending={'fp': {
                    'first_alert_ts': NOW.isoformat(),
                    'jsonl_path': str(jsonl),
                    'jsonl_mtime_at_alert': NOW.timestamp(),
                    'cwd': '/home/larry/agent-worktrees/wt-mirror-fp',
                    'tier': 'mirror',
                }},
            )
            summary = _run(rec, [], state)
        self.assertEqual(summary['false_positives'], 1)
        self.assertEqual(state.streak, 0)            # streak reset
        self.assertEqual(state.mode, h.MODE_ALERT_ONLY)  # demoted
        self.assertNotIn('fp', state.pending)

    def test_pending_session_that_resumes_is_false_positive(self):
        rec = _Recorder()
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            jsonl = Path(td) / 'resume.jsonl'
            jsonl.write_text('no marker here, just work')
            mtime = jsonl.stat().st_mtime
            state = h.ConfidenceState(
                mode=h.MODE_ALERT_ONLY,
                executions=[{'outcome': h.TRUE_POSITIVE, 'session_id': 'a'}],
                pending={'r': {
                    'first_alert_ts': NOW.isoformat(),
                    'jsonl_path': str(jsonl),
                    # baseline well in the past → current mtime counts as "resumed"
                    'jsonl_mtime_at_alert': mtime - 100,
                    'cwd': '/home/larry/agent-worktrees/wt-forge-r',
                    'tier': 'forge',
                }},
            )
            summary = _run(rec, [], state)
        self.assertEqual(summary['false_positives'], 1)
        self.assertEqual(state.streak, 0)


# ---------------------- kill switch ----------------------

class TestKillSwitch(unittest.TestCase):
    def test_kill_switch_blocks_main(self, ):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            ks = Path(td) / 'healers.disabled'
            ks.write_text('')
            orig = h.KILL_SWITCH
            h.KILL_SWITCH = ks
            try:
                self.assertTrue(h.kill_switch_active())
                # Belt-and-suspenders: LOG_FILE froze into the module-top
                # sandbox at import, so this mock is the second layer, not
                # the only barrier — keep both.
                with mock.patch.object(h, 'log'):
                    rc = h.main()
                self.assertEqual(rc, 0)
            finally:
                h.KILL_SWITCH = orig


# ---------------------- no double kill ----------------------

class TestNoDoubleKill(unittest.TestCase):
    def test_zombie_healer_domain_is_not_a_candidate(self):
        # /tmp/wt-main-* belongs to heal_zombie_main_workers — never ours.
        self.assertIsNone(h.agent_tier_for_cwd('/tmp/wt-main-pulse-42'))

    def test_deleted_cwd_outside_prefix_is_ignored(self):
        self.assertIsNone(h.agent_tier_for_cwd('/tmp/wt-main-x (deleted)'))

    def test_unrelated_cwd_ignored(self):
        self.assertIsNone(h.agent_tier_for_cwd('/home/larry/agent-core'))

    def test_review_worktrees_are_owned(self):
        self.assertEqual(
            h.agent_tier_for_cwd('/home/larry/agent-worktrees/wt-mirror-a'), 'mirror')
        self.assertEqual(
            h.agent_tier_for_cwd('/home/larry/agent-worktrees/wt-forge-b'), 'forge')


# ---------------------- config ----------------------

class TestConfig(unittest.TestCase):
    def test_missing_file_uses_defaults(self):
        cfg = h.load_config(Path('/nonexistent/review-reaper-rules.json'))
        self.assertEqual(cfg['marker_grace_seconds'], 300)
        self.assertEqual(cfg['silent_grace_seconds'], 900)
        self.assertEqual(cfg['hard_silent_grace_seconds'], 3600)
        self.assertEqual(cfg['streak_to_promote'], 3)

    def test_garbage_file_uses_defaults(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'c.json'
            p.write_text('{not json')
            cfg = h.load_config(p)
            self.assertEqual(cfg, h.DEFAULT_CONFIG)

    def test_valid_override(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'c.json'
            p.write_text(json.dumps({
                'enabled': False, 'marker_grace_seconds': 60,
                'silent_grace_seconds': 120, 'streak_to_promote': 5,
            }))
            cfg = h.load_config(p)
            self.assertFalse(cfg['enabled'])
            self.assertEqual(cfg['marker_grace_seconds'], 60)
            self.assertEqual(cfg['silent_grace_seconds'], 120)
            self.assertEqual(cfg['streak_to_promote'], 5)

    def test_negative_value_falls_back(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'c.json'
            p.write_text(json.dumps({'marker_grace_seconds': -5}))
            cfg = h.load_config(p)
            self.assertEqual(cfg['marker_grace_seconds'], 300)

    def test_hard_below_silent_disables_hard_backstop(self):
        # Safety invariant: a mistuned config (hard <= silent) must NOT make the
        # healer kill more aggressively — it disables the hard path instead, so
        # only the conservative alert ladder runs.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'c.json'
            p.write_text(json.dumps({
                'silent_grace_seconds': 900,
                'hard_silent_grace_seconds': 600,  # below silent — invalid
            }))
            cfg = h.load_config(p)
            self.assertEqual(cfg['hard_silent_grace_seconds'], h._HARD_BACKSTOP_DISABLED)
            # And a normally-hard-eligible idle now classifies as the soft path.
            self.assertEqual(
                h.classify(marker_present=False, idle_secs=5000, cfg=cfg),
                h.SILENT_CASE2)

    def test_hard_equal_silent_disables_hard_backstop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'c.json'
            p.write_text(json.dumps({
                'silent_grace_seconds': 900, 'hard_silent_grace_seconds': 900,
            }))
            cfg = h.load_config(p)
            self.assertEqual(cfg['hard_silent_grace_seconds'], h._HARD_BACKSTOP_DISABLED)


# ---------------------- streak / mode helpers ----------------------

class TestStreakHelpers(unittest.TestCase):
    def test_streak_counts_tail(self):
        execs = [
            {'outcome': h.FALSE_POSITIVE},
            {'outcome': h.TRUE_POSITIVE},
            {'outcome': h.TRUE_POSITIVE},
        ]
        self.assertEqual(h.consecutive_true_positive_streak(execs), 2)

    def test_false_positive_breaks_streak(self):
        execs = [
            {'outcome': h.TRUE_POSITIVE},
            {'outcome': h.TRUE_POSITIVE},
            {'outcome': h.FALSE_POSITIVE},
        ]
        self.assertEqual(h.consecutive_true_positive_streak(execs), 0)

    def test_empty_streak(self):
        self.assertEqual(h.consecutive_true_positive_streak([]), 0)
        self.assertEqual(h.consecutive_true_positive_streak(None), 0)

    def test_mode_for_streak(self):
        self.assertEqual(
            h.mode_for_streak(3, threshold=3, current_mode=h.MODE_ALERT_ONLY),
            h.MODE_AUTO_REAP)
        self.assertEqual(
            h.mode_for_streak(2, threshold=3, current_mode=h.MODE_AUTO_REAP),
            h.MODE_ALERT_ONLY)


# ---------------------- jsonl marker detection ----------------------

def _assistant_line(text: str) -> str:
    return json.dumps({
        'type': 'assistant',
        'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]},
    })


def _user_line(text: str) -> str:
    """A user/tool_result line — e.g. the agent reading its own CLAUDE.md."""
    return json.dumps({
        'type': 'user',
        'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]},
    })


class TestMarkerDetection(unittest.TestCase):
    def test_mirror_marker_detected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 's.jsonl'
            p.write_text(_assistant_line(
                'blah === REVIEW_ESCALATE === {"x":1} === END_REVIEW_ESCALATE ===') + '\n')
            self.assertTrue(h.jsonl_has_terminal_marker(p, 'mirror'))
            self.assertFalse(h.jsonl_has_terminal_marker(p, 'forge'))

    def test_forge_preamble_detected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 's.jsonl'
            p.write_text(_assistant_line(
                'PR opened: https://github.com/x/y/pull/9') + '\n')
            self.assertTrue(h.jsonl_has_terminal_marker(p, 'forge'))

    def test_no_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 's.jsonl'
            p.write_text(_assistant_line('just some working output, no terminal marker') + '\n')
            self.assertFalse(h.jsonl_has_terminal_marker(p, 'mirror'))

    def test_marker_in_user_line_is_not_a_verdict(self):
        # The contamination Mirror flagged: the marker delimiter appears in a
        # user/tool_result line because the agent read its own CLAUDE.md at
        # startup. That is NOT proof of completion and must not match.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 's.jsonl'
            p.write_text('\n'.join([
                _user_line('CLAUDE.md says emit `=== REVIEW_PASS ===` when you accept the PR.'),
                _assistant_line('Reviewing the diff now; running the regression gate.'),
            ]) + '\n')
            self.assertFalse(h.jsonl_has_terminal_marker(p, 'mirror'))

    def test_marker_in_assistant_after_user_contamination_still_detected(self):
        # Real session shape: startup CLAUDE.md read (user line, contaminated)
        # followed by the genuine emitted verdict (assistant line). Must match.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 's.jsonl'
            p.write_text('\n'.join([
                _user_line('manual example: === REVIEW_PASS ==='),
                _assistant_line('Looks good.\n=== REVIEW_PASS ===\n{"ok":1}\n=== END_REVIEW_PASS ==='),
            ]) + '\n')
            self.assertTrue(h.jsonl_has_terminal_marker(p, 'mirror'))

    def test_forge_preamble_in_user_line_is_not_a_verdict(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 's.jsonl'
            p.write_text(_user_line(
                "forge/CLAUDE.md: start your result with 'PR opened:'") + '\n')
            self.assertFalse(h.jsonl_has_terminal_marker(p, 'forge'))

    def test_forge_revision_prose_is_not_a_marker(self):
        # Regression: bare substring 'Revision ' matched ordinary prose from a
        # LIVE Forge agent ('the Revision phase'), triggering a destructive
        # Case-1 reap. The preamble must be line-anchored + the precise
        # 'Revision N applied:' form.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            for prose in (
                "I'll move to the Revision phase next.",
                'Revision protocol: first re-read the findings.',
                'Working on a revision pass for the failing test.',
                'Mid-sentence note about PR opened: discipline in the docs.',
            ):
                p = Path(td) / 's.jsonl'
                p.write_text(_assistant_line(prose) + '\n')
                self.assertFalse(
                    h.jsonl_has_terminal_marker(p, 'forge'),
                    f'prose should NOT be a terminal marker: {prose!r}')

    def test_forge_revision_applied_line_is_a_marker(self):
        # The genuine terminal signal: a line that STARTS with 'Revision N
        # applied:' still counts (even when preceded by other assistant text).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 's.jsonl'
            p.write_text(_assistant_line(
                'Done.\nRevision 2 applied: pushed fixes to the PR branch.') + '\n')
            self.assertTrue(h.jsonl_has_terminal_marker(p, 'forge'))

    def test_forge_canonical_pr_forms_are_markers(self):
        # The real shapes Forge writes (mirroring outbox_notifier._PR_URL_RE):
        # the '#<num>' token and a leading clause must still be recognized — the
        # earlier bare 'PR opened:' literal MISSED 'PR #303 opened:'.
        import tempfile
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/303'
        for line in (
            f'PR #303 opened: {url}',
            f'Done. PR opened: {url}',
            f'Result: PR #303 updated: {url}',
        ):
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / 's.jsonl'
                p.write_text(_assistant_line(line) + '\n')
                self.assertTrue(h.jsonl_has_terminal_marker(p, 'forge'),
                                f'should be a terminal marker: {line!r}')

    def test_forge_pr_mention_without_url_is_not_a_marker(self):
        # Prose discussing a PR but carrying no pull URL must NOT count
        # (the URL requirement is what rejects 'I considered PR opened: ...').
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 's.jsonl'
            p.write_text(_assistant_line(
                'I considered PR opened: discipline from last week but moved on.')
                + '\n')
            self.assertFalse(h.jsonl_has_terminal_marker(p, 'forge'))


class TestRemoveWorktreeDataLossGuard(unittest.TestCase):
    def test_skips_removal_when_worktree_dirty(self):
        # A dirty worktree (uncommitted changes) must NOT be force-removed —
        # --force would discard the work. remove_worktree returns False (skip)
        # and never invokes `git worktree remove`.
        from unittest import mock
        wt = '/home/larry/agent-worktrees/wt-mirror-dirty'
        with mock.patch.object(h, '_canonical_repo_for_worktree',
                               return_value=Path('/home/larry/agent-core')), \
             mock.patch.object(h, '_repo_on_main', return_value=True), \
             mock.patch.object(h, '_worktree_has_uncommitted_changes', return_value=True), \
             mock.patch.object(h.subprocess, 'run') as run:
            self.assertFalse(h.remove_worktree(wt))
            run.assert_not_called()  # no `git worktree remove` issued

    def test_unknown_dirty_state_also_skips(self):
        # If status can't be read (None), treat as 'do not force-destroy'.
        from unittest import mock
        wt = '/home/larry/agent-worktrees/wt-forge-unknown'
        with mock.patch.object(h, '_canonical_repo_for_worktree',
                               return_value=Path('/home/larry/agent-core')), \
             mock.patch.object(h, '_repo_on_main', return_value=True), \
             mock.patch.object(h, '_worktree_has_uncommitted_changes', return_value=None), \
             mock.patch.object(h.subprocess, 'run') as run:
            self.assertFalse(h.remove_worktree(wt))
            run.assert_not_called()


class TestReapPidIdentityRecheck(unittest.TestCase):
    def test_reap_aborts_when_pid_cwd_no_longer_matches(self):
        # PID-reuse / process-exited guard: if the pid's cwd no longer matches
        # the wedged worktree at kill time, the reap aborts (no kill) — never
        # SIGKILL a process that inherited the recycled PID.
        rec = _Recorder()
        state = h.ConfidenceState(mode=h.MODE_AUTO_REAP,
                                  executions=[{'outcome': h.TRUE_POSITIVE, 'session_id': s}
                                              for s in ('a', 'b', 'c')])
        cand = _cand(pid=9100, marker=False, idle=1200, session_id='reused')
        # The pid now reports a DIFFERENT cwd than the scanned worktree.
        summary = _run(rec, [cand], state,
                       verify_pid_fn=lambda pid: '/some/other/unrelated/cwd')
        self.assertEqual(rec.killed, [])           # nothing killed
        self.assertEqual(summary['case2_auto_reaped'], 0)

    def test_reap_aborts_when_pid_already_gone(self):
        rec = _Recorder()
        state = h.ConfidenceState(mode=h.MODE_ALERT_ONLY, executions=[], pending={})
        cand = _cand(pid=9200, marker=False, idle=4000, session_id='gone')  # hard-grace
        summary = _run(rec, [cand], state,
                       verify_pid_fn=lambda pid: None)  # pid no longer exists
        self.assertEqual(rec.killed, [])
        self.assertEqual(summary['case2_hard_reaped'], 0)


# ---------------------- Forge-PR reap guard ----------------------

class TestForgePrReapGuard(unittest.TestCase):
    """The guard added for the #412 incident: never destroy (or alert on) a Forge
    session whose branch has an OPEN PR with no Mirror review dispatched yet."""

    @staticmethod
    def _recent_iso():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _old_iso():
        from datetime import datetime, timezone, timedelta
        return (datetime.now(timezone.utc)
                - timedelta(hours=h.GUARD_MAX_PROTECT_HOURS + 1)).isoformat()

    def test_helper_protects_when_open_pr_and_no_review(self):
        from unittest import mock
        with mock.patch.object(h, '_open_pr_created_at',
                               return_value=self._recent_iso()), \
             mock.patch.object(h, '_review_already_dispatched_for_task',
                               return_value=False):
            self.assertTrue(h._forge_branch_has_open_unreviewed_pr(
                '/home/larry/agent-worktrees/wt-forge-build-x'))

    def test_helper_no_protection_when_no_open_pr(self):
        from unittest import mock
        with mock.patch.object(h, '_open_pr_created_at', return_value=None), \
             mock.patch.object(h, '_review_already_dispatched_for_task',
                               return_value=False):
            self.assertFalse(h._forge_branch_has_open_unreviewed_pr(
                '/home/larry/agent-worktrees/wt-forge-build-x'))

    def test_helper_no_protection_once_review_dispatched(self):
        from unittest import mock
        with mock.patch.object(h, '_open_pr_created_at',
                               return_value=self._recent_iso()), \
             mock.patch.object(h, '_review_already_dispatched_for_task',
                               return_value=True):
            self.assertFalse(h._forge_branch_has_open_unreviewed_pr(
                '/home/larry/agent-worktrees/wt-forge-build-x'))

    def test_helper_no_protection_when_pr_older_than_cap(self):
        # Age cap: a long-open PR with no review is no longer protected, so a
        # wedged session can't hold a fleet slot forever.
        from unittest import mock
        with mock.patch.object(h, '_open_pr_created_at',
                               return_value=self._old_iso()), \
             mock.patch.object(h, '_review_already_dispatched_for_task',
                               return_value=False) as m_rev:
            self.assertFalse(h._forge_branch_has_open_unreviewed_pr(
                '/home/larry/agent-worktrees/wt-forge-build-x'))
            m_rev.assert_not_called()  # age cap short-circuits before the review check

    def test_helper_no_protection_for_non_forge_cwd(self):
        # A mirror worktree (or any non wt-forge- cwd) is never PR-protected; the
        # gh check is short-circuited so no network call is made.
        from unittest import mock
        with mock.patch.object(h, '_open_pr_created_at') as m_pr:
            self.assertFalse(h._forge_branch_has_open_unreviewed_pr(
                '/home/larry/agent-worktrees/wt-mirror-build-x'))
            m_pr.assert_not_called()

    def test_case1_reap_skipped_when_forge_pr_protected(self):
        # idle past the build-handoff grace floor (marker_grace 300 + handoff 300
        # = 600) so the open-PR condition (b) — not the grace window — is what
        # spares the session; the queued-build probe is forced off to isolate (b).
        from unittest import mock
        rec = _Recorder()
        cand = _cand(pid=4242, tier='forge', marker=True, idle=700)
        with mock.patch.object(h, '_forge_branch_has_open_unreviewed_pr',
                               return_value=True), \
             mock.patch.object(h, '_forge_inbox_has_queued_build',
                               return_value=False):
            summary = _run(rec, [cand], h.ConfidenceState())
        self.assertEqual(rec.killed, [])
        self.assertEqual(rec.removed, [])
        self.assertEqual(summary['case1_reaped'], 0)
        self.assertEqual(summary['forge_spared'], 1)

    def test_hard_reap_skipped_when_forge_pr_protected(self):
        from unittest import mock
        rec = _Recorder()
        state = h.ConfidenceState(mode=h.MODE_ALERT_ONLY, executions=[], pending={})
        cand = _cand(pid=9200, tier='forge', marker=False, idle=4000)  # hard grace
        with mock.patch.object(h, '_forge_branch_has_open_unreviewed_pr',
                               return_value=True), \
             mock.patch.object(h, '_forge_inbox_has_queued_build',
                               return_value=False):
            summary = _run(rec, [cand], state)
        self.assertEqual(rec.killed, [])
        self.assertEqual(summary['case2_hard_reaped'], 0)
        self.assertEqual(summary['forge_spared'], 1)

    def test_forge_session_reaps_normally_when_not_protected(self):
        # idle past the grace floor (600) with NO queued build and NO open PR →
        # all spare-conditions fail → reaped.
        from unittest import mock
        rec = _Recorder()
        cand = _cand(pid=4242, tier='forge', marker=True, idle=700)
        with mock.patch.object(h, '_forge_branch_has_open_unreviewed_pr',
                               return_value=False), \
             mock.patch.object(h, '_forge_inbox_has_queued_build',
                               return_value=False):
            summary = _run(rec, [cand], h.ConfidenceState())
        self.assertEqual(rec.killed, [4242])
        self.assertEqual(summary['case1_reaped'], 1)
        self.assertEqual(summary['forge_spared'], 0)

    def test_mirror_session_never_spared(self):
        # The guard is forge-only: a mirror candidate is reaped normally and the
        # spare helper is never consulted for it.
        from unittest import mock
        rec = _Recorder()
        cand = _cand(pid=7001, tier='mirror', marker=True, idle=600)
        with mock.patch.object(h, '_forge_spare_reason') as m_g:
            summary = _run(rec, [cand], h.ConfidenceState())
        m_g.assert_not_called()
        self.assertEqual(rec.killed, [7001])
        self.assertEqual(summary['case1_reaped'], 1)


# ---------------------- build-sequence spare conditions ----------------------

class TestForgeBuildSequenceSpare(unittest.TestCase):
    """The fix for the 2026-06-10 ccd-s1 incident: the reaper must SPARE a Forge
    worktree whose build is still legitimately in flight, and still reap a
    genuinely-wedged one. Four spare-conditions, per the spec:
      (1) a queued next-phase build dispatch  → spared
      (2) an open PR awaiting Mirror review    → spared
      (3) within the advancer-cadence grace    → spared
      (4) none of the above + idle past grace  → still reaped
    """

    def test_session_with_queued_next_phase_dispatch_is_spared(self):
        # (1) preflight→build handoff: a queued build-<task>.json in Forge's inbox
        # means the build is about to --resume into this worktree. Idle is past
        # the grace floor so only condition (a) can spare it; PR check forced off.
        from unittest import mock
        rec = _Recorder()
        cand = _cand(pid=4300, tier='forge', name='ccd-s1', marker=True, idle=5632)
        with mock.patch.object(h, '_forge_inbox_has_queued_build',
                               return_value=True), \
             mock.patch.object(h, '_forge_branch_has_open_unreviewed_pr',
                               return_value=False):
            summary = _run(rec, [cand], h.ConfidenceState())
        self.assertEqual(rec.killed, [])
        self.assertEqual(rec.removed, [])
        self.assertEqual(summary['case1_reaped'], 0)
        self.assertEqual(summary['forge_spared'], 1)

    def test_session_with_open_pr_awaiting_review_is_spared(self):
        # (2) open PR, no review dispatched yet. Idle past the grace floor and no
        # queued build, so condition (b) is what spares it.
        from unittest import mock
        rec = _Recorder()
        cand = _cand(pid=4301, tier='forge', name='pr-x', marker=True, idle=5632)
        with mock.patch.object(h, '_forge_inbox_has_queued_build',
                               return_value=False), \
             mock.patch.object(h, '_forge_branch_has_open_unreviewed_pr',
                               return_value=True):
            summary = _run(rec, [cand], h.ConfidenceState())
        self.assertEqual(rec.killed, [])
        self.assertEqual(summary['case1_reaped'], 0)
        self.assertEqual(summary['forge_spared'], 1)

    def test_session_within_advancer_grace_window_is_spared(self):
        # (3) idle past marker_grace (300 → verdict is REAP_CASE1) but within the
        # handoff grace floor (300 + 300 = 600). The advancer hasn't had a full
        # cycle yet, so the session is spared without consulting any external seam.
        from unittest import mock
        rec = _Recorder()
        cand = _cand(pid=4302, tier='forge', name='handoff', marker=True, idle=450)
        # Grace is pure idle math and must short-circuit BEFORE the queue/PR
        # probes — assert those are never consulted.
        with mock.patch.object(h, '_forge_inbox_has_queued_build') as m_q, \
             mock.patch.object(h, '_forge_branch_has_open_unreviewed_pr') as m_pr:
            summary = _run(rec, [cand], h.ConfidenceState())
        m_q.assert_not_called()
        m_pr.assert_not_called()
        self.assertEqual(rec.killed, [])
        self.assertEqual(summary['case1_reaped'], 0)
        self.assertEqual(summary['forge_spared'], 1)

    def test_genuinely_wedged_session_is_still_reaped(self):
        # (4) terminal marker, idle far past the grace floor, NO queued dispatch,
        # NO open PR → fails all spare-conditions → reaped (the truly-wedged case
        # the healer still exists to handle).
        from unittest import mock
        rec = _Recorder()
        cand = _cand(pid=4303, tier='forge', name='wedged', marker=True, idle=5632)
        with mock.patch.object(h, '_forge_inbox_has_queued_build',
                               return_value=False), \
             mock.patch.object(h, '_forge_branch_has_open_unreviewed_pr',
                               return_value=False):
            summary = _run(rec, [cand], h.ConfidenceState())
        self.assertEqual(rec.killed, [4303])
        self.assertEqual(rec.removed, [cand.cwd])
        self.assertEqual(summary['case1_reaped'], 1)
        self.assertEqual(summary['forge_spared'], 0)

    # -------- _forge_spare_reason precedence / bounds --------

    def test_spare_reason_grace_takes_precedence(self):
        cand = _cand(tier='forge', marker=True, idle=400)  # <= 600 grace floor
        reason = h._forge_spare_reason(cand, CFG)
        self.assertIsNotNone(reason)
        self.assertIn('grace', reason)

    def test_spare_reason_none_when_all_conditions_fail(self):
        from unittest import mock
        cand = _cand(tier='forge', marker=True, idle=5632)
        with mock.patch.object(h, '_forge_inbox_has_queued_build',
                               return_value=False), \
             mock.patch.object(h, '_forge_branch_has_open_unreviewed_pr',
                               return_value=False):
            self.assertIsNone(h._forge_spare_reason(cand, CFG))

    def test_spare_reason_queued_dispatch_bounded_by_max_protect(self):
        # Past GUARD_MAX_PROTECT_HOURS the queued-dispatch probe is skipped (a
        # never-consumed dispatch can't hold the slot forever); with no open PR
        # the session is no longer spared.
        from unittest import mock
        idle = h.GUARD_MAX_PROTECT_HOURS * 3600 + 100
        cand = _cand(tier='forge', marker=True, idle=idle)
        with mock.patch.object(h, '_forge_inbox_has_queued_build',
                               return_value=True) as m_q, \
             mock.patch.object(h, '_forge_branch_has_open_unreviewed_pr',
                               return_value=False):
            self.assertIsNone(h._forge_spare_reason(cand, CFG))
            m_q.assert_not_called()  # bound short-circuits before the probe


class TestForgeInboxQueuedBuild(unittest.TestCase):
    """Direct tests for the queued-build probe against a temp INBOXES_ROOT."""

    def _with_inbox(self, fn):
        import tempfile
        import safe_write_inbox
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            forge_inbox = Path(td) / 'forge'
            forge_inbox.mkdir(parents=True)
            with mock.patch.object(safe_write_inbox, 'INBOXES_ROOT', Path(td)):
                return fn(forge_inbox)

    def test_live_build_file_present_is_queued(self):
        def check(forge_inbox):
            (forge_inbox / 'build-ccd-s1.json').write_text('{}')
            self.assertTrue(h._forge_inbox_has_queued_build('ccd-s1'))
        self._with_inbox(check)

    def test_replan_build_file_present_is_queued(self):
        def check(forge_inbox):
            (forge_inbox / 'build-ccd-s1-replan2.json').write_text('{}')
            self.assertTrue(h._forge_inbox_has_queued_build('ccd-s1'))
        self._with_inbox(check)

    def test_no_build_file_is_not_queued(self):
        def check(forge_inbox):
            self.assertFalse(h._forge_inbox_has_queued_build('ccd-s1'))
        self._with_inbox(check)

    def test_archived_build_does_not_count_as_queued(self):
        # An archived (already-consumed) build is NOT the handoff window — only a
        # LIVE inbox file means a dispatch is queued-and-pending.
        def check(forge_inbox):
            archive = forge_inbox / '.archive'
            archive.mkdir()
            (archive / 'build-ccd-s1.json').write_text('{}')
            self.assertFalse(h._forge_inbox_has_queued_build('ccd-s1'))
        self._with_inbox(check)


class TestForgeTaskIdForCwd(unittest.TestCase):
    def test_extracts_task_id(self):
        self.assertEqual(
            h._forge_task_id_for_cwd('/home/larry/agent-worktrees/wt-forge-ccd-s1'),
            'ccd-s1')

    def test_non_forge_cwd_is_none(self):
        self.assertIsNone(
            h._forge_task_id_for_cwd('/home/larry/agent-worktrees/wt-mirror-x'))


class TestBuildHandoffGraceConfig(unittest.TestCase):
    def test_default_present(self):
        cfg = h.load_config(Path('/nonexistent/review-reaper-rules.json'))
        self.assertEqual(cfg['build_handoff_grace_seconds'], 300)

    def test_valid_override(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'c.json'
            p.write_text(json.dumps({'build_handoff_grace_seconds': 120}))
            cfg = h.load_config(p)
            self.assertEqual(cfg['build_handoff_grace_seconds'], 120)

    def test_negative_falls_back(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'c.json'
            p.write_text(json.dumps({'build_handoff_grace_seconds': -1}))
            cfg = h.load_config(p)
            self.assertEqual(cfg['build_handoff_grace_seconds'], 300)


if __name__ == '__main__':
    unittest.main()
