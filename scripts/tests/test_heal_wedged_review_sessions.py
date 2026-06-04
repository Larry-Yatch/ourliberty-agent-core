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

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import heal_wedged_review_sessions as h  # noqa: E402


NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
CFG = {
    'enabled': True,
    'marker_grace_seconds': 300,
    'silent_grace_seconds': 900,
    'streak_to_promote': 3,
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


def _run(rec, candidates, state, cfg=CFG):
    return h.run_cycle(
        candidates=candidates, state=state, cfg=cfg, now=NOW,
        reaper=rec.reaper, worktree_remover=rec.remover,
        closure_notify=rec.closure, escalate_notify=rec.escalate,
        event_emitter=lambda **kw: None,  # never touch the live chain_events table
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


class TestFalsePositiveDemote(unittest.TestCase):
    def test_pending_session_that_emits_marker_is_false_positive(self):
        rec = _Recorder()
        # Write a JSONL that now contains a terminal marker.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            jsonl = Path(td) / 'fp.jsonl'
            jsonl.write_text('... === REVIEW_PASS === {"ok":1} === END_REVIEW_PASS === ...')
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
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ks = Path(td) / 'healers.disabled'
            ks.write_text('')
            orig = h.KILL_SWITCH
            h.KILL_SWITCH = ks
            try:
                self.assertTrue(h.kill_switch_active())
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

class TestMarkerDetection(unittest.TestCase):
    def test_mirror_marker_detected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 's.jsonl'
            p.write_text('blah === REVIEW_ESCALATE === {"x":1} === END_REVIEW_ESCALATE ===')
            self.assertTrue(h.jsonl_has_terminal_marker(p, 'mirror'))
            self.assertFalse(h.jsonl_has_terminal_marker(p, 'forge'))

    def test_forge_preamble_detected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 's.jsonl'
            p.write_text('... PR opened: https://github.com/x/y/pull/9 ...')
            self.assertTrue(h.jsonl_has_terminal_marker(p, 'forge'))

    def test_no_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 's.jsonl'
            p.write_text('just some working output, no terminal marker')
            self.assertFalse(h.jsonl_has_terminal_marker(p, 'mirror'))


if __name__ == '__main__':
    unittest.main()
