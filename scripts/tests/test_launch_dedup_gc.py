"""Tests for scripts/launch_dedup_gc.py (PR #609 follow-up — operational hygiene
for the board-Launch duplicate-work guard).

unittest (repo convention; pytest isn't installed on the droplet).

Coverage:
  - load_gc_config: missing / malformed -> conservative defaults; valid file
    parsed; individually-invalid fields fall back.
  - reap_deduped: a fresh hold is left alone; an expired hold with no fresh claim
    is re-surfaced (stamped + moved back to the queue); an expired hold that is
    STILL claimed is left held; a hold already re-surfaced once is archived; the
    archive roll-up alert fires via run_once; --dry-run writes nothing.
  - rotate_claims: archives + rewrites old lines, keeps the min-retained floor,
    is a no-op when nothing is eligible / the ledger is missing; archive-first.

All paths are redirected to a tmp tree (the #428 sandbox via _bootstrap plus a
per-test OURLIBERTY_AGENTS_ROOT override + module reload), and larry_alerts is
stubbed, so nothing touches the real ~/agents tree.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


class _GCHarness(unittest.TestCase):
    """Redirects OURLIBERTY_AGENTS_ROOT to a tmp tree, reloads the GC module so
    its frozen path constants re-resolve, and stubs larry_alerts.append_alert."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.agents_root = Path(self._tmp.name) / 'agents'
        self.queue_dir = self.agents_root / 'blackboard' / 'build-launch-queue'
        self.deduped_dir = self.queue_dir / '.deduped'
        self.deduped_dir.mkdir(parents=True)
        (self.agents_root / 'blackboard' / 'build-sequences').mkdir(parents=True)
        (self.agents_root / 'blackboard' / 'archive').mkdir(parents=True)
        (self.agents_root / 'logs').mkdir(parents=True)
        self.claims_file = self.agents_root / 'blackboard' / 'deliverable-claims.jsonl'
        self.archive_dir = self.agents_root / 'blackboard' / 'archive'

        self._prior_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.agents_root)
        for mod in ('launch_dedup_gc',):
            sys.modules.pop(mod, None)
        import launch_dedup_gc as gc  # noqa: E402
        self.gc = gc

        # Stub the roll-up alert so run_once never touches the real ledger and we
        # can assert on it.
        import larry_alerts  # noqa: E402
        self._prior_append = larry_alerts.append_alert
        self.alerts = []
        larry_alerts.append_alert = lambda **kw: self.alerts.append(kw) or True
        self._larry_alerts = larry_alerts

    def tearDown(self):
        self._larry_alerts.append_alert = self._prior_append
        if self._prior_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prior_root
        self._tmp.cleanup()

    # -------- helpers --------

    def _write_held(self, name, *, phase_id, age_hours, resurfaced=False, seq_id=None):
        """Write a held launch request into .deduped/ with an mtime `age_hours`
        before NOW."""
        entry = {'phase_id': phase_id, 'project_id': 'proj-x', 'repo': 'ourliberty-agent-core'}
        if seq_id:
            entry['seq_id'] = seq_id
        if resurfaced:
            entry[self.gc.RESURFACED_FIELD] = (NOW - timedelta(hours=age_hours)).isoformat()
        path = self.deduped_dir / name
        path.write_text(json.dumps(entry))
        ts = (NOW - timedelta(hours=age_hours)).timestamp()
        os.utime(path, (ts, ts))
        return path

    def _write_claim(self, claimed_task_id, *, hours_ago, envelope='sibling'):
        rec = {
            'ts': (NOW - timedelta(hours=hours_ago)).isoformat(),
            'claimed_task_id': claimed_task_id,
            'envelope_task_id': envelope,
        }
        with self.claims_file.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec) + '\n')

    def _reap(self, dry_run=False, window_h=24):
        return self.gc.reap_deduped(
            queue_dir=self.queue_dir, now=NOW,
            claim_window_sec=window_h * 3600, dry_run=dry_run,
        )


class TestConfig(_GCHarness):
    def test_defaults_when_missing(self):
        cfg = self.gc.load_gc_config(path=self.agents_root / 'nope.json')
        self.assertEqual(cfg, (24, 7, 500))

    def test_valid_and_invalid_fields(self):
        p = self.agents_root / 'cfg.json'
        p.write_text(json.dumps({
            'claim_window_hours': 48,
            'claims_retention_days': 'bad',  # invalid -> default 7
            'claims_min_retained_lines': 0,  # zero allowed
        }))
        self.assertEqual(self.gc.load_gc_config(path=p), (48, 7, 0))

    def test_malformed_file(self):
        p = self.agents_root / 'bad.json'
        p.write_text('{not json')
        self.assertEqual(self.gc.load_gc_config(path=p), (24, 7, 500))


class TestReapDeduped(_GCHarness):
    def test_fresh_hold_left_alone(self):
        self._write_held('a.json', phase_id='phase-a', age_hours=1)
        counts = self._reap()
        self.assertEqual(counts['too_fresh'], 1)
        self.assertEqual(counts['resurfaced'], 0)
        self.assertTrue((self.deduped_dir / 'a.json').exists())

    def test_expired_no_claim_resurfaced(self):
        self._write_held('b.json', phase_id='phase-b', age_hours=25)
        counts = self._reap()
        self.assertEqual(counts['resurfaced'], 1)
        # Moved back to the queue, no longer held.
        self.assertFalse((self.deduped_dir / 'b.json').exists())
        requeued = self.queue_dir / 'b.json'
        self.assertTrue(requeued.exists())
        # ...and stamped so a re-hold is detectable next pass.
        entry = json.loads(requeued.read_text())
        self.assertIn(self.gc.RESURFACED_FIELD, entry)

    def test_expired_still_claimed_left_held(self):
        self._write_held('c.json', phase_id='phase-c', age_hours=25)
        self._write_claim('phase-c', hours_ago=1)  # fresh claim still matches
        counts = self._reap()
        self.assertEqual(counts['still_claimed'], 1)
        self.assertEqual(counts['resurfaced'], 0)
        self.assertTrue((self.deduped_dir / 'c.json').exists())

    def test_stale_claim_does_not_block_resurface(self):
        # A claim OUTSIDE the window must not count as "still claimed".
        self._write_held('d.json', phase_id='phase-d', age_hours=25)
        self._write_claim('phase-d', hours_ago=48)  # stale -> ignored
        counts = self._reap()
        self.assertEqual(counts['resurfaced'], 1)
        self.assertEqual(counts['still_claimed'], 0)

    def test_already_resurfaced_is_archived(self):
        self._write_held('e.json', phase_id='phase-e', age_hours=25, resurfaced=True)
        counts = self._reap()
        self.assertEqual(counts['archived'], 1)
        self.assertEqual(counts['archived_files'], ['e.json'])
        self.assertFalse((self.deduped_dir / 'e.json').exists())
        self.assertTrue((self.deduped_dir / '.archive' / 'e.json').exists())

    def test_dry_run_writes_nothing(self):
        self._write_held('f.json', phase_id='phase-f', age_hours=25)
        counts = self._reap(dry_run=True)
        self.assertEqual(counts['resurfaced'], 1)  # what it WOULD do
        # Nothing actually moved.
        self.assertTrue((self.deduped_dir / 'f.json').exists())
        self.assertFalse((self.queue_dir / 'f.json').exists())

    def test_run_once_emits_archive_alert(self):
        self._write_held('g.json', phase_id='phase-g', age_hours=25, resurfaced=True)
        self.gc.run_once(
            config=(24, 7, 500), now=NOW,
            queue_dir=self.queue_dir, claims_file=self.claims_file,
            archive_dir=self.archive_dir,
        )
        self.assertEqual(len(self.alerts), 1)
        alert = self.alerts[0]
        self.assertEqual(alert['source'], 'launch-dedup-gc')
        self.assertIn('g.json', alert['message'])

    def test_run_once_no_alert_when_nothing_archived(self):
        self._write_held('h.json', phase_id='phase-h', age_hours=25)  # re-surfaced, not archived
        self.gc.run_once(
            config=(24, 7, 500), now=NOW,
            queue_dir=self.queue_dir, claims_file=self.claims_file,
            archive_dir=self.archive_dir,
        )
        self.assertEqual(self.alerts, [])


class TestRotateClaims(_GCHarness):
    def _write_lines(self, days_ago_list):
        with self.claims_file.open('w', encoding='utf-8') as fh:
            for i, d in enumerate(days_ago_list):
                ts = (NOW - timedelta(days=d)).isoformat()
                fh.write(json.dumps({
                    'ts': ts, 'claimed_task_id': f'p{i}', 'envelope_task_id': 'e',
                }) + '\n')

    def _rotate(self, days=7, min_lines=2):
        return self.gc.rotate_claims(
            claims_file=self.claims_file, archive_dir=self.archive_dir,
            retention_days=days, min_retained_lines=min_lines, now=NOW,
        )

    def test_archives_old_keeps_recent(self):
        # 3 old (10d) + 2 recent (1d); min_lines=2 -> archive the 3 oldest.
        self._write_lines([10, 10, 10, 1, 1])
        counts = self._rotate(days=7, min_lines=2)
        self.assertEqual(counts['cut'], 3)
        self.assertEqual(counts['archived'], 3)
        survivors = self.claims_file.read_text().strip().splitlines()
        self.assertEqual(len(survivors), 2)
        # Archive exists and holds the 3 cut lines.
        archives = list(self.archive_dir.glob('deliverable-claims-*.jsonl'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(len(archives[0].read_text().strip().splitlines()), 3)

    def test_min_retained_floor_caps_the_cut(self):
        # All 5 are old, but min_lines=4 keeps the last 4 -> cut only 1.
        self._write_lines([10, 10, 10, 10, 10])
        counts = self._rotate(days=7, min_lines=4)
        self.assertEqual(counts['cut'], 1)
        self.assertEqual(
            len(self.claims_file.read_text().strip().splitlines()), 4)

    def test_noop_when_all_recent(self):
        self._write_lines([1, 1, 1])
        counts = self._rotate(days=7, min_lines=2)
        self.assertEqual(counts['cut'], 0)
        self.assertEqual(counts['archived'], 0)
        self.assertEqual(
            len(self.claims_file.read_text().strip().splitlines()), 3)

    def test_undated_line_halts_days_cut(self):
        # An unparseable/undated line can't be proven old -> halts the days cut.
        with self.claims_file.open('w', encoding='utf-8') as fh:
            fh.write('not json at all\n')
            fh.write(json.dumps({
                'ts': (NOW - timedelta(days=10)).isoformat(),
                'claimed_task_id': 'p', 'envelope_task_id': 'e',
            }) + '\n')
        counts = self._rotate(days=7, min_lines=0)
        self.assertEqual(counts['cut'], 0)

    def test_missing_ledger_noop(self):
        counts = self._rotate()
        self.assertEqual(counts['total_lines'], 0)
        self.assertEqual(counts['cut'], 0)


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    unittest.main()
