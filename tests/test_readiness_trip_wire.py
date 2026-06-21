#!/usr/bin/env python3
"""Tests for scripts/readiness_trip_wire.py — the second-build-team readiness
trip-wire.

Covers (per the dispatch acceptance list):
  1. does NOT fire when only some signals are present (partial signal);
  2. does NOT fire before warm-up / K ticks of history;
  3. FIRES when all signals are sustained K ticks (append_alert called once
     with the right subject);
  4. the multi-day cooldown suppresses a second fire;
  5. --dry-run never emits and never writes last_fired_at.

Isolation: a tmp OURLIBERTY_AGENTS_ROOT is set BEFORE importing the module so
every path resolver redirects into the sandbox, and larry_alerts.append_alert is
stubbed (the module never touches the real alert queue, so the test-isolation
choke guard is never reached). The three signal files are fabricated on disk.

Run:
    cd <worktree-root> && python3 -m unittest tests.test_readiness_trip_wire -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import readiness_trip_wire as w  # noqa: E402


HOUR = 3600.0


class _StubAlerts:
    """Stand-in for the larry_alerts module — records append_alert calls."""

    def __init__(self, return_value: bool = True):
        self.calls: list[dict] = []
        self.return_value = return_value

    def append_alert(self, **kwargs):
        self.calls.append(kwargs)
        return self.return_value


class ReadinessTripWireTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # Redirect every path resolver into the sandbox.
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)

        # Make the test deterministic and fast: small K + small thresholds +
        # short required span, overriding the module constants in place.
        self._saved = {
            name: getattr(w, name)
            for name in (
                'K_TICKS', 'MIN_SUSTAINED_SPAN_SEC', 'T_DEPTH', 'T_AGE',
                'RATE_WINDOW_HOURS', 'T_RATE_MIN', 'FIRE_COOLDOWN_SEC',
            )
        }
        w.K_TICKS = 8
        w.MIN_SUSTAINED_SPAN_SEC = int(2 * HOUR)
        w.T_DEPTH = 3
        w.T_AGE = int(1 * HOUR)
        w.RATE_WINDOW_HOURS = 3
        w.T_RATE_MIN = 1
        w.FIRE_COOLDOWN_SEC = int(3 * 24 * HOUR)

        # Stub the alert queue so no real DM is ever appended (and the
        # test-isolation choke guard is never reached).
        self.alerts = _StubAlerts()
        sys.modules['larry_alerts'] = self.alerts

        # Pre-create the dirs the resolvers expect.
        (self.root / 'inboxes' / 'forge').mkdir(parents=True, exist_ok=True)
        (self.root / 'config').mkdir(parents=True, exist_ok=True)
        (self.root / 'blackboard').mkdir(parents=True, exist_ok=True)
        (self.root / 'state').mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for name, val in self._saved.items():
            setattr(w, name, val)
        sys.modules.pop('larry_alerts', None)
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        self._tmp.cleanup()

    # ---------- signal fabrication ----------

    def _set_backlog(self, depth: int, oldest_age_sec: float):
        """Fabricate `depth` forge task envelopes; the oldest has the given age."""
        inbox = self.root / 'inboxes' / 'forge'
        for p in inbox.glob('*.json'):
            p.unlink()
        now = datetime.now(timezone.utc).timestamp()
        for i in range(depth):
            f = inbox / f'task-{i:03d}.json'
            f.write_text('{}')
            # The first file is the oldest; the rest are recent.
            age = oldest_age_sec if i == 0 else 1.0
            os.utime(f, (now - age, now - age))

    def _set_concurrency(self, active: int, max_: int = 6):
        slots = [{'pid': 1000 + i, 'agent': 'forge'} for i in range(active)]
        (self.root / 'config' / '.concurrency-guard.json').write_text(
            json.dumps({'slots': slots, 'max': max_})
        )

    def _set_rate_limits(self, count: int, within_hours: float = 1.0):
        path = self.root / 'blackboard' / 'anthropic-quota-events.jsonl'
        now = datetime.now(timezone.utc)
        lines = []
        for i in range(count):
            ts = (now - timedelta(hours=within_hours * (i + 1) / max(count, 1))).isoformat()
            lines.append(json.dumps({
                'ts': ts, 'agent': 'forge', 'task_id': f't{i}',
                'failure_class': 'rate_limit',
            }))
        path.write_text('\n'.join(lines) + ('\n' if lines else ''))

    def _all_signals_hot(self):
        self._set_backlog(depth=5, oldest_age_sec=2 * HOUR)
        self._set_concurrency(active=6, max_=6)
        self._set_rate_limits(count=2)

    def _seed_history(self, n_hot: int, n_cold: int = 0, span_hours: float = 3.0,
                      end: datetime | None = None) -> list[dict]:
        """Build a history list of n_cold cold samples followed by n_hot hot
        samples, evenly spaced over span_hours ending at `end`."""
        end = end or datetime.now(timezone.utc)
        total = n_cold + n_hot
        history = []
        for i in range(total):
            ts = end - timedelta(hours=span_hours * (total - 1 - i) / max(total - 1, 1))
            is_hot = i >= n_cold
            history.append({
                'ts': ts.isoformat(),
                'backlog_depth': 5 if is_hot else 0,
                'oldest_wait_sec': 2 * HOUR if is_hot else 0.0,
                'concurrency_active': 6 if is_hot else 1,
                'cap': 6,
                'rate_limit_count': 2 if is_hot else 0,
            })
        return history

    def _write_state(self, state: dict):
        (self.root / 'state' / 'readiness-trip-wire.json').write_text(
            json.dumps(state)
        )

    def _read_state(self) -> dict:
        p = self.root / 'state' / 'readiness-trip-wire.json'
        return json.loads(p.read_text()) if p.exists() else {}

    # ---------- (1) partial signals must NOT fire ----------

    def test_does_not_fire_when_only_some_signals_present(self):
        # Warm history (K-1 prior ticks) so warm-up isn't the reason it holds.
        now = datetime.now(timezone.utc)
        history = self._seed_history(n_hot=w.K_TICKS - 1, span_hours=2.5, end=now)
        self._write_state({
            'monitoring_since': (now - timedelta(hours=3)).isoformat(),
            'history': history,
        })
        # Backlog + concurrency saturated, but NO rate-limit pressure.
        self._set_backlog(depth=5, oldest_age_sec=2 * HOUR)
        self._set_concurrency(active=6, max_=6)
        self._set_rate_limits(count=0)

        result = w.run_once(now=now)
        self.assertFalse(result['should_fire'], result['reason'])
        self.assertFalse(result['fired'])
        self.assertEqual(self.alerts.calls, [])

    def test_does_not_fire_with_backlog_but_idle_concurrency(self):
        now = datetime.now(timezone.utc)
        history = self._seed_history(n_hot=w.K_TICKS - 1, span_hours=2.5, end=now)
        self._write_state({
            'monitoring_since': (now - timedelta(hours=3)).isoformat(),
            'history': history,
        })
        self._set_backlog(depth=5, oldest_age_sec=2 * HOUR)
        self._set_concurrency(active=1, max_=6)   # NOT saturated
        self._set_rate_limits(count=2)

        result = w.run_once(now=now)
        self.assertFalse(result['should_fire'], result['reason'])
        self.assertEqual(self.alerts.calls, [])

    # ---------- (2) warm-up: must NOT fire before K ticks ----------

    def test_does_not_fire_before_warmup(self):
        now = datetime.now(timezone.utc)
        # Only a couple of prior hot ticks — fewer than K even after this tick.
        history = self._seed_history(n_hot=2, span_hours=2.5, end=now)
        self._write_state({
            'monitoring_since': (now - timedelta(hours=1)).isoformat(),
            'history': history,
        })
        self._all_signals_hot()

        result = w.run_once(now=now)
        self.assertFalse(result['should_fire'], result['reason'])
        self.assertIn('warm-up', result['reason'])
        self.assertEqual(self.alerts.calls, [])

    def test_cold_start_empty_droplet_does_not_fire(self):
        # No state, no signal files at all — first ever tick.
        result = w.run_once()
        self.assertFalse(result['should_fire'])
        self.assertEqual(self.alerts.calls, [])
        # Baseline recorded + one history sample persisted.
        st = self._read_state()
        self.assertIn('monitoring_since', st)
        self.assertEqual(len(st['history']), 1)

    # ---------- (3) all signals sustained K ticks -> FIRES once ----------

    def test_fires_when_all_signals_sustained_k_ticks(self):
        now = datetime.now(timezone.utc)
        # K-1 prior hot ticks spanning > MIN_SUSTAINED_SPAN_SEC; this tick is hot
        # too, giving K consecutive hot ticks.
        history = self._seed_history(n_hot=w.K_TICKS - 1, span_hours=2.5, end=now)
        self._write_state({
            'monitoring_since': (now - timedelta(hours=3)).isoformat(),
            'history': history,
        })
        self._all_signals_hot()

        result = w.run_once(now=now)
        self.assertTrue(result['should_fire'], result['reason'])
        self.assertTrue(result['fired'])
        # append_alert called EXACTLY once, with the right subject + source.
        self.assertEqual(len(self.alerts.calls), 1)
        call = self.alerts.calls[0]
        self.assertEqual(call['subject'], w.ALERT_SUBJECT)
        self.assertEqual(call['source'], w.ALERT_SOURCE)
        self.assertEqual(call['severity'], 'warning')
        # last_fired_at persisted.
        self.assertIn('last_fired_at', self._read_state())

    def test_does_not_fire_when_streak_broken_by_one_cold_tick(self):
        now = datetime.now(timezone.utc)
        # One cold tick in the middle of the recent window breaks the streak.
        history = self._seed_history(n_hot=w.K_TICKS - 2, n_cold=1,
                                     span_hours=2.5, end=now)
        # Put the cold one INSIDE the last-K window (seed already orders
        # cold-first; with K-2 hot + 1 cold = K-1 entries, this tick makes K,
        # and the cold sits within the window).
        self._write_state({
            'monitoring_since': (now - timedelta(hours=3)).isoformat(),
            'history': history,
        })
        self._all_signals_hot()

        result = w.run_once(now=now)
        self.assertFalse(result['should_fire'], result['reason'])
        self.assertIn('not sustained', result['reason'])
        self.assertEqual(self.alerts.calls, [])

    # ---------- (4) multi-day cooldown suppresses a second fire ----------

    def test_cooldown_suppresses_second_fire(self):
        now = datetime.now(timezone.utc)
        history = self._seed_history(n_hot=w.K_TICKS - 1, span_hours=2.5, end=now)
        # Already fired 1 hour ago — well inside the 3-day cooldown.
        self._write_state({
            'monitoring_since': (now - timedelta(hours=3)).isoformat(),
            'history': history,
            'last_fired_at': (now - timedelta(hours=1)).isoformat(),
        })
        self._all_signals_hot()

        result = w.run_once(now=now)
        self.assertFalse(result['should_fire'], result['reason'])
        self.assertIn('cooldown', result['reason'])
        self.assertEqual(self.alerts.calls, [])

    def test_fires_again_after_cooldown_elapsed(self):
        now = datetime.now(timezone.utc)
        history = self._seed_history(n_hot=w.K_TICKS - 1, span_hours=2.5, end=now)
        # Fired 4 days ago — past the 3-day cooldown.
        self._write_state({
            'monitoring_since': (now - timedelta(days=5)).isoformat(),
            'history': history,
            'last_fired_at': (now - timedelta(days=4)).isoformat(),
        })
        self._all_signals_hot()

        result = w.run_once(now=now)
        self.assertTrue(result['should_fire'], result['reason'])
        self.assertEqual(len(self.alerts.calls), 1)

    # ---------- (5) --dry-run never emits / never writes last_fired_at ----------

    def test_dry_run_never_emits_or_records_fire(self):
        now = datetime.now(timezone.utc)
        history = self._seed_history(n_hot=w.K_TICKS - 1, span_hours=2.5, end=now)
        self._write_state({
            'monitoring_since': (now - timedelta(hours=3)).isoformat(),
            'history': history,
        })
        self._all_signals_hot()

        result = w.run_once(now=now, dry_run=True)
        # Would fire, but dry-run suppresses the emit + the last_fired_at write.
        self.assertTrue(result['should_fire'], result['reason'])
        self.assertFalse(result['fired'])
        self.assertEqual(self.alerts.calls, [])
        st = self._read_state()
        self.assertNotIn('last_fired_at', st)
        # History IS still appended (a dry-run is a real observation).
        self.assertEqual(len(st['history']), w.K_TICKS)

    def test_dry_run_cli_path_suppresses_emit(self):
        now = datetime.now(timezone.utc)
        history = self._seed_history(n_hot=w.K_TICKS - 1, span_hours=2.5, end=now)
        self._write_state({
            'monitoring_since': (now - timedelta(hours=3)).isoformat(),
            'history': history,
        })
        self._all_signals_hot()

        rc = w.main(['--dry-run'])
        self.assertEqual(rc, 0)
        self.assertEqual(self.alerts.calls, [])
        self.assertNotIn('last_fired_at', self._read_state())

    # ---------- extra: main() is fail-open, status is read-only ----------

    def test_status_is_read_only(self):
        before = self._read_state()
        rc = w.main(['--status'])
        self.assertEqual(rc, 0)
        # --status must not create or mutate the state file.
        self.assertEqual(self._read_state(), before)
        self.assertEqual(self.alerts.calls, [])

    def test_main_fail_open_on_unexpected_error(self):
        # Force run_once to blow up; main() must still return 0.
        orig = w.run_once
        w.run_once = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom'))
        try:
            self.assertEqual(w.main([]), 0)
        finally:
            w.run_once = orig


if __name__ == '__main__':
    unittest.main()
