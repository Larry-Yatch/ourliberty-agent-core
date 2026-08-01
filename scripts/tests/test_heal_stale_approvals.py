"""Tests for scripts/heal_stale_approvals.py (auto-clear node N1).

unittest (repo convention; pytest isn't installed on the droplet).

Coverage:
- classify_approval: pending NEVER cleared; history cleared; unknown uncertain.
- classify_clarify: clarify_response clears; progressed-past clears; live kept;
  history-resolved-before-clarify-ts kept (uncertain).
- run_once end-to-end with a fake Supabase client: clears only resolved rows,
  never the still-pending approval, writes a backup, is idempotent, and
  --dry-run writes nothing.
- load_beacon_approvals: missing / malformed file is conservative (empty).

Task_ids use the reserved `zz-fixture-` namespace so they can never be confused
with live mock rows (real-* / prod-*) per spec § 7.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import freshness_probe as fp  # noqa: E402
import task_terminal_state as tts  # noqa: E402

NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)
OLD_TS = '2026-06-01T00:00:00+00:00'        # 36h before NOW (past grace)
FRESH_TS = '2026-06-02T11:30:00+00:00'      # 0.5h before NOW (within grace)


# -------------------- fake Supabase client --------------------

class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Mimics the PostgREST fluent builder for the subset this job uses."""

    def __init__(self, store, update_log):
        self._store = store
        self._update_log = update_log
        self._eq: dict = {}
        self._null: list = []
        self._in = None
        self._range = None
        self._update_patch = None

    def select(self, _cols):
        return self

    def is_(self, col, _val):
        self._null.append(col)
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def in_(self, col, vals):
        self._in = (col, list(vals))
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def update(self, patch):
        self._update_patch = patch
        return self

    def execute(self):
        if self._update_patch is not None:
            col, vals = self._in
            valset = set(vals)
            n = 0
            for row in self._store:
                if row.get(col) in valset:
                    row.update(self._update_patch)
                    n += 1
            self._update_log.append({'ids': list(vals), 'n': n})
            return _Resp([])
        rows = []
        for row in self._store:
            if any(row.get(c) is not None for c in self._null):
                continue
            if all(row.get(k) == v for k, v in self._eq.items()):
                rows.append(row)
        if self._range is not None:
            a, b = self._range
            rows = rows[a:b + 1]
        return _Resp([dict(r) for r in rows])


class _FakeClient:
    def __init__(self, store):
        self._store = store
        self.update_log: list = []

    def table(self, _name):
        return _FakeQuery(self._store, self.update_log)


def _row(event_id, event_type, task_id, ts, read_at=None):
    return {
        'event_id': event_id,
        'event_type': event_type,
        'agent': 'forge',
        'task_id': task_id,
        'ts': ts,
        'read_at': read_at,
    }


class _Base(unittest.TestCase):
    """Repoints AGENTS_ROOT at a temp dir + reloads the module so its
    module-level path constants pick up the env var."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'agents'
        (self.root / 'logs').mkdir(parents=True)
        (self.root / 'blackboard' / 'backups').mkdir(parents=True)
        (self.root / 'state').mkdir(parents=True)
        # Save/restore, never pop — popping would un-sandbox later tests
        # whose modules resolve the env at call time (test-jail audit H2/H3).
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        import heal_stale_approvals as mod
        self.mod = importlib.reload(mod)
        self.backup_dir = self.root / 'blackboard' / 'backups'

    def tearDown(self):
        self._tmp.cleanup()
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root


# -------------------- root resolution --------------------

class TestAgentsRootResolution(unittest.TestCase):
    """The approval-state root must resolve like the rest of the pending-approvals
    trio (handler, heal_unregistered): an EMPTY OURLIBERTY_AGENTS_ROOT falls back
    to ~/agents, NOT Path('') = cwd (audit L1 trio-consistency follow-up)."""

    def setUp(self):
        self._saved = os.environ.get('OURLIBERTY_AGENTS_ROOT')

    def tearDown(self):
        if self._saved is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._saved
        import heal_stale_approvals as mod
        importlib.reload(mod)  # restore default-env module state for later tests

    def _reload(self):
        import heal_stale_approvals as mod
        return importlib.reload(mod)

    def test_empty_override_falls_back_to_home(self):
        os.environ['OURLIBERTY_AGENTS_ROOT'] = ''
        mod = self._reload()
        expected = Path.home() / 'agents'
        self.assertEqual(mod.AGENTS_ROOT, expected)
        self.assertEqual(mod.PENDING_APPROVALS,
                         expected / 'state' / 'beacon-pending-approvals.json')

    def test_unset_override_falls_back_to_home(self):
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        mod = self._reload()
        self.assertEqual(mod.AGENTS_ROOT, Path.home() / 'agents')

    def test_set_override_is_honored(self):
        os.environ['OURLIBERTY_AGENTS_ROOT'] = '/tmp/zz-fixture-root'
        mod = self._reload()
        self.assertEqual(mod.AGENTS_ROOT, Path('/tmp/zz-fixture-root'))


# -------------------- classification --------------------

class TestClassifyApproval(_Base):
    def test_pending_never_cleared(self):
        clear, reason = self.mod.classify_approval(
            _row('e1', 'approval_request', 'zz-fixture-pending', '2026-06-01T00:00:00+00:00'),
            pending_ids={'zz-fixture-pending'}, history_ids=set())
        self.assertFalse(clear)
        self.assertIn('live', reason)

    def test_history_cleared(self):
        clear, _ = self.mod.classify_approval(
            _row('e2', 'approval_request', 'zz-fixture-done', '2026-06-01T00:00:00+00:00'),
            pending_ids=set(), history_ids={'zz-fixture-done'})
        self.assertTrue(clear)

    def test_unknown_uncertain_not_cleared(self):
        clear, reason = self.mod.classify_approval(
            _row('e3', 'approval_request', 'zz-fixture-ghost', '2026-06-01T00:00:00+00:00'),
            pending_ids=set(), history_ids=set())
        self.assertFalse(clear)
        self.assertIn('uncertain', reason)

    def test_pending_wins_even_if_also_in_history(self):
        # A task re-opened: present in both buckets. Pending guard must win.
        clear, reason = self.mod.classify_approval(
            _row('e4', 'approval_request', 'zz-fixture-reopened', '2026-06-01T00:00:00+00:00'),
            pending_ids={'zz-fixture-reopened'}, history_ids={'zz-fixture-reopened'})
        self.assertFalse(clear)
        self.assertIn('live', reason)


class TestClassifyClarify(_Base):
    TS = '2026-06-01T00:00:00+00:00'

    def test_clarify_response_after_clears(self):
        clear, _ = self.mod.classify_clarify(
            _row('c1', 'clarify_request', 'zz-fixture-c', self.TS),
            cresp_ts={'zz-fixture-c': ['2026-06-02T00:00:00+00:00']},
            hist_resolved_at={}, history_ids=set())
        self.assertTrue(clear)

    def test_progressed_past_clears_without_response(self):
        # No clarify_response, but the task's approval resolved at/after the
        # clarify ts -> progressed past. This is the under-clear case (b).
        clear, reason = self.mod.classify_clarify(
            _row('c2', 'clarify_request', 'zz-fixture-shipped', self.TS),
            cresp_ts={},
            hist_resolved_at={'zz-fixture-shipped': '2026-06-02T00:00:00+00:00'},
            history_ids={'zz-fixture-shipped'})
        self.assertTrue(clear)
        self.assertIn('progressed past', reason)

    def test_live_clarify_kept(self):
        clear, reason = self.mod.classify_clarify(
            _row('c3', 'clarify_request', 'zz-fixture-live', self.TS),
            cresp_ts={}, hist_resolved_at={}, history_ids=set())
        self.assertFalse(clear)
        self.assertIn('live', reason)

    def test_history_resolved_before_clarify_is_uncertain(self):
        # Task in history but resolved BEFORE the clarify ts: the clarify is a
        # fresh question posted after that resolution -> kept (uncertain).
        clear, reason = self.mod.classify_clarify(
            _row('c4', 'clarify_request', 'zz-fixture-fresh', self.TS),
            cresp_ts={},
            hist_resolved_at={'zz-fixture-fresh': '2026-05-30T00:00:00+00:00'},
            history_ids={'zz-fixture-fresh'})
        self.assertFalse(clear)
        self.assertIn('uncertain', reason)


# -------------------- load_beacon_approvals --------------------

class TestLoadBeaconApprovals(_Base):
    def test_missing_file_is_empty(self):
        pending, history, hist = self.mod.load_beacon_approvals(
            self.root / 'state' / 'nope.json')
        self.assertEqual((pending, history, hist), (set(), set(), {}))

    def test_malformed_file_is_empty(self):
        p = self.root / 'state' / 'bad.json'
        p.write_text('{not json')
        pending, history, hist = self.mod.load_beacon_approvals(p)
        self.assertEqual((pending, history, hist), (set(), set(), {}))

    def test_parses_pending_history_and_latest_resolved(self):
        p = self.root / 'state' / 'ba.json'
        p.write_text(json.dumps({
            'pending': [{'id': 'zz-fixture-p'}],
            'history': [
                {'task_id': 'zz-fixture-h', 'resolved_at': '2026-06-01T00:00:00+00:00'},
                {'task_id': 'zz-fixture-h', 'resolved_at': '2026-06-02T00:00:00+00:00'},
            ],
        }))
        pending, history, hist = self.mod.load_beacon_approvals(p)
        self.assertEqual(pending, {'zz-fixture-p'})
        self.assertEqual(history, {'zz-fixture-h'})
        self.assertEqual(hist['zz-fixture-h'], '2026-06-02T00:00:00+00:00')


# -------------------- run_once integration --------------------

class TestRunOnce(_Base):
    def _store(self):
        return [
            _row('a-pending', 'approval_request', 'zz-fixture-pending', '2026-06-01T00:00:00+00:00'),
            _row('a-done', 'approval_request', 'zz-fixture-done', '2026-06-01T00:00:00+00:00'),
            _row('c-live', 'clarify_request', 'zz-fixture-live', '2026-06-01T00:00:00+00:00'),
            _row('c-ship', 'clarify_request', 'zz-fixture-shipped', '2026-06-01T00:00:00+00:00'),
            # A clarify_response signal row (not a decision row).
            _row('resp-1', 'clarify_response', 'zz-fixture-live2', '2026-06-02T00:00:00+00:00'),
        ]

    def _beacon_state(self):
        return (
            {'zz-fixture-pending'},                                  # pending
            {'zz-fixture-done', 'zz-fixture-shipped'},               # history
            {'zz-fixture-shipped': '2026-06-02T00:00:00+00:00'},     # resolved_at
        )

    def test_clears_resolved_keeps_pending_and_live(self):
        store = self._store()
        client = _FakeClient(store)
        counts = self.mod.run_once(
            client, beacon_state=self._beacon_state(), now=NOW,
            backup_dir=self.backup_dir)

        by_id = {r['event_id']: r for r in store}
        # Resolved approval + progressed-past clarify cleared.
        self.assertIsNotNone(by_id['a-done']['read_at'])
        self.assertIsNotNone(by_id['c-ship']['read_at'])
        # Still-pending approval + live clarify untouched.
        self.assertIsNone(by_id['a-pending']['read_at'])
        self.assertIsNone(by_id['c-live']['read_at'])

        self.assertEqual(counts['clear_approval'], 1)
        self.assertEqual(counts['clear_clarify'], 1)
        self.assertEqual(counts['cleared'], 2)
        # a-pending (approval still pending) + c-live (clarify, no signal).
        self.assertEqual(counts['kept_live'], 2)
        self.assertEqual(counts['kept_uncertain'], 0)

        backups = list(self.backup_dir.glob('heal-stale-approvals-*.json'))
        self.assertEqual(len(backups), 1)
        backed = {r['event_id'] for r in json.loads(backups[0].read_text())}
        self.assertEqual(backed, {'a-done', 'c-ship'})

    def test_dry_run_writes_nothing(self):
        store = self._store()
        client = _FakeClient(store)
        counts = self.mod.run_once(
            client, beacon_state=self._beacon_state(), now=NOW,
            dry_run=True, backup_dir=self.backup_dir)
        self.assertTrue(all(r['read_at'] is None for r in store))
        self.assertEqual(client.update_log, [])
        self.assertEqual(list(self.backup_dir.glob('*.json')), [])
        self.assertEqual(counts['clear_approval'], 1)  # still classified
        self.assertEqual(counts['cleared'], 0)         # but not applied

    def test_idempotent_second_run_clears_nothing(self):
        store = self._store()
        client = _FakeClient(store)
        self.mod.run_once(client, beacon_state=self._beacon_state(), now=NOW,
                          backup_dir=self.backup_dir)
        counts2 = self.mod.run_once(client, beacon_state=self._beacon_state(),
                                    now=NOW, backup_dir=self.backup_dir)
        # Already-cleared rows have read_at set, so they aren't refetched.
        self.assertEqual(counts2['pending_approval'], 1)   # only a-pending
        self.assertEqual(counts2['clear_approval'], 0)
        self.assertEqual(counts2['clear_clarify'], 0)
        self.assertEqual(counts2['cleared'], 0)


# -------------------- terminal-state reconciliation (spec § 3.1) --------------------

def _entry(approval_id, created_at, task_id=None):
    """A pending-approval entry. task_id defaults to approval_id (add_pending
    sets id := dispatch_payload['task_id'], so they normally agree)."""
    payload = {} if task_id is None else {'task_id': task_id}
    return {
        'id': approval_id,
        'created_at': created_at,
        'chat_id': 1,
        'plan_summary': 'fixture plan',
        'target_agent': 'forge',
        'dispatch_payload': payload,
        'status': 'pending',
        'reminders_sent': [],
    }


class TestClassifyTerminalApproval(_Base):
    """The conservative guard (spec § 1, § 6): retire ONLY when the work is
    positively terminal AND past grace; OPEN / UNKNOWN / within-grace / no-ts /
    no-task-id all KEEP. An indeterminate probe can never falsely retire."""

    GRACE = 2.0

    def _probe(self, state):
        return lambda _tid: state

    def test_merged_past_grace_retires(self):
        retire, reason = self.mod.classify_terminal_approval(
            _entry('zz-fixture-merged', OLD_TS, task_id='zz-fixture-merged'),
            NOW, self.GRACE, self._probe(tts.MERGED))
        self.assertTrue(retire)
        self.assertIn('terminal', reason)

    def test_closed_past_grace_retires(self):
        retire, _ = self.mod.classify_terminal_approval(
            _entry('zz-fixture-closed', OLD_TS, task_id='zz-fixture-closed'),
            NOW, self.GRACE, self._probe(tts.CLOSED))
        self.assertTrue(retire)

    def test_open_kept(self):
        retire, reason = self.mod.classify_terminal_approval(
            _entry('zz-fixture-open', OLD_TS, task_id='zz-fixture-open'),
            NOW, self.GRACE, self._probe(tts.OPEN))
        self.assertFalse(retire)
        self.assertIn('not terminal', reason)

    def test_unknown_kept(self):
        retire, reason = self.mod.classify_terminal_approval(
            _entry('zz-fixture-unknown', OLD_TS, task_id='zz-fixture-unknown'),
            NOW, self.GRACE, self._probe(tts.UNKNOWN))
        self.assertFalse(retire)
        self.assertIn('not terminal', reason)

    def test_within_grace_kept_even_if_terminal(self):
        # A freshly-created approval whose work already merged is NOT retired —
        # the grace window protects against racing a just-landed happy path.
        retire, reason = self.mod.classify_terminal_approval(
            _entry('zz-fixture-fresh', FRESH_TS, task_id='zz-fixture-fresh'),
            NOW, self.GRACE, self._probe(tts.MERGED))
        self.assertFalse(retire)
        self.assertIn('within grace', reason)

    def test_missing_created_at_kept(self):
        retire, reason = self.mod.classify_terminal_approval(
            _entry('zz-fixture-nots', '', task_id='zz-fixture-nots'),
            NOW, self.GRACE, self._probe(tts.MERGED))
        self.assertFalse(retire)
        self.assertIn('created_at', reason)

    def test_unparseable_created_at_kept(self):
        retire, _ = self.mod.classify_terminal_approval(
            _entry('zz-fixture-bad', 'not-a-date', task_id='zz-fixture-bad'),
            NOW, self.GRACE, self._probe(tts.MERGED))
        self.assertFalse(retire)

    def test_no_task_id_kept(self):
        # No dispatch_payload.task_id AND no usable id to probe -> keep.
        entry = _entry('', OLD_TS, task_id=None)
        retire, reason = self.mod.classify_terminal_approval(
            entry, NOW, self.GRACE, self._probe(tts.MERGED))
        self.assertFalse(retire)
        self.assertIn('no task_id', reason)

    def test_falls_back_to_entry_id_when_payload_has_no_task_id(self):
        # dispatch_payload lacks task_id, but the entry id is a valid task_id.
        seen = []
        entry = _entry('zz-fixture-byid', OLD_TS, task_id=None)
        retire, _ = self.mod.classify_terminal_approval(
            entry, NOW, self.GRACE,
            lambda tid: (seen.append(tid) or tts.MERGED))
        self.assertTrue(retire)
        self.assertEqual(seen, ['zz-fixture-byid'])


class _TerminalBase(_Base):
    """_Base + a sandbox-repointed beacon_approval_handler so resolve() reads and
    writes the temp-dir beacon-pending-approvals.json instead of the real one."""

    def setUp(self):
        super().setUp()
        import beacon_approval_handler as approval
        # reload mutates the module IN PLACE, so heal_stale_approvals' bound
        # `approval` reference picks up the repointed PENDING_APPROVALS_PATH.
        self.approval = importlib.reload(approval)
        self.pending_path = self.approval.PENDING_APPROVALS_PATH

    def _write_state(self, pending):
        self.pending_path.write_text(json.dumps(
            {'version': 1, 'pending': pending, 'history': []}))

    def _load(self):
        return json.loads(self.pending_path.read_text())


class TestReconcileTerminalApprovals(_TerminalBase):
    def _probe(self, states):
        return lambda tid: states.get(tid, tts.UNKNOWN)

    def test_retires_terminal_keeps_open_and_within_grace(self):
        self._write_state([
            _entry('zz-fixture-merged', OLD_TS, task_id='zz-fixture-merged'),
            _entry('zz-fixture-open', OLD_TS, task_id='zz-fixture-open'),
            _entry('zz-fixture-fresh', FRESH_TS, task_id='zz-fixture-fresh'),
        ])
        probe = self._probe({
            'zz-fixture-merged': tts.MERGED,
            'zz-fixture-open': tts.OPEN,
            'zz-fixture-fresh': tts.MERGED,  # terminal but within grace -> keep
        })
        counts = self.mod.reconcile_terminal_approvals(now=NOW, probe=probe)

        self.assertEqual(counts['pending'], 3)
        self.assertEqual(counts['retired'], 1)
        self.assertEqual(counts['kept'], 2)

        state = self._load()
        pending_ids = {e['id'] for e in state['pending']}
        self.assertEqual(pending_ids, {'zz-fixture-open', 'zz-fixture-fresh'})
        history = {e['id']: e for e in state['history']}
        self.assertIn('zz-fixture-merged', history)
        self.assertEqual(history['zz-fixture-merged']['status'], 'expired')
        self.assertIn('terminal-state',
                      history['zz-fixture-merged'].get('resolution_note', ''))

    def test_dry_run_writes_nothing(self):
        self._write_state([
            _entry('zz-fixture-merged', OLD_TS, task_id='zz-fixture-merged'),
        ])
        counts = self.mod.reconcile_terminal_approvals(
            now=NOW, probe=self._probe({'zz-fixture-merged': tts.MERGED}),
            dry_run=True)
        self.assertEqual(counts['retired'], 1)  # classified
        state = self._load()
        self.assertEqual([e['id'] for e in state['pending']],
                         ['zz-fixture-merged'])      # but not applied
        self.assertEqual(state['history'], [])

    def test_unknown_probe_never_retires(self):
        # The five-rows-stuck case: every probe is UNKNOWN (e.g. tier-1
        # canonical_intervention_id that matches no PR). NONE may be retired.
        self._write_state([
            _entry('zz-fixture-a', OLD_TS, task_id='zz-fixture-a'),
            _entry('zz-fixture-b', OLD_TS, task_id='zz-fixture-b'),
        ])
        counts = self.mod.reconcile_terminal_approvals(
            now=NOW, probe=lambda _tid: tts.UNKNOWN)
        self.assertEqual(counts['retired'], 0)
        self.assertEqual(counts['kept'], 2)
        self.assertEqual(len(self._load()['pending']), 2)

    def test_idempotent_second_run(self):
        self._write_state([
            _entry('zz-fixture-merged', OLD_TS, task_id='zz-fixture-merged'),
            _entry('zz-fixture-open', OLD_TS, task_id='zz-fixture-open'),
        ])
        probe = self._probe({
            'zz-fixture-merged': tts.MERGED, 'zz-fixture-open': tts.OPEN})
        self.mod.reconcile_terminal_approvals(now=NOW, probe=probe)
        counts2 = self.mod.reconcile_terminal_approvals(now=NOW, probe=probe)
        self.assertEqual(counts2['pending'], 1)   # only the open one remains
        self.assertEqual(counts2['retired'], 0)
        self.assertEqual(counts2['kept'], 1)


# ---- stale-premise reconcile (approvals-freshness slice 2, Part B) ----

def _probe_entry(approval_id, created_at, probe, *, premise_stale=None):
    """A pending entry carrying a freshness_probe on its dispatch_payload (where
    the evaluator reads it). An optional pre-existing premise_stale marker
    exercises the idempotent-refresh path."""
    entry = _entry(approval_id, created_at, task_id=approval_id)
    entry['dispatch_payload']['freshness_probe'] = probe
    if premise_stale is not None:
        entry['premise_stale'] = premise_stale
    return entry


# A file_contains probe — the demonstrable end-to-end path this slice ships.
_FC_PROBE = {
    'kind': 'file_contains', 'repo': '/tmp/zz-fixture-repo',
    'path': 'x.py', 'substring': 'gone', 'ref': 'origin/main',
}


class _ProbeLegBase(_TerminalBase):
    """_TerminalBase + the probe leg's shared guards, so the demote-side and
    stamp-side suites can both use them without one inheriting (and re-running)
    the other's tests."""

    def _patch_approval(self, name, fn):
        """Monkeypatch an attribute on the SHARED beacon_approval_handler module
        and restore it at teardown. `_TerminalBase.setUp` reloads the module, so
        an unrestored stub happens to be cleaned up before the next test in this
        file — but only by accident of class ordering. Restoring explicitly keeps
        a raising stub from ever escaping into a suite-wide run."""
        original = getattr(self.approval, name)
        self.addCleanup(setattr, self.approval, name, original)
        setattr(self.approval, name, fn)

    def _guard_no_autoclear(self):
        """Make resolve() / _clear_dashboard_pending() fail the test if the probe
        leg ever reaches the auto-CLEAR path."""
        def _boom_resolve(*a, **k):
            raise AssertionError('probe leg must NEVER call resolve()')

        def _boom_clear(*a, **k):
            raise AssertionError('probe leg must NEVER call _clear_dashboard_pending()')
        self._patch_approval('resolve', _boom_resolve)
        self._patch_approval('_clear_dashboard_pending', _boom_clear)


class TestReconcileStalePremises(_ProbeLegBase):
    """The load-bearing semantics of the probe leg:
      FALSE -> demote in place (still pending, read_at untouched, no history);
      TRUE / INDETERMINATE -> untouched;
      no freshness_probe -> not probed;
      NEVER auto-clear on a probe verdict (resolve/_clear never called);
      idempotent second tick (no history, no clear, no escalation);
      a demote that finds the entry gone is NOT counted as a demotion;
      --dry-run writes nothing."""

    def test_false_demotes_in_place(self):
        self._guard_no_autoclear()
        self._write_state([_probe_entry('zz-fixture-dead', OLD_TS, _FC_PROBE)])
        counts = self.mod.reconcile_stale_premises(
            now=NOW, evaluate=lambda _p: fp.FALSE)
        self.assertEqual(counts, {
            'pending': 1, 'probed': 1, 'demoted': 1, 'kept': 0,
            # No client passed -> the stamp lane skips rather than fails.
            'verified': 0, 'stale': 0, 'skipped': 1, 'failed': 0})
        state = self._load()
        # STILL in pending[]; nothing moved to history.
        self.assertEqual([e['id'] for e in state['pending']], ['zz-fixture-dead'])
        self.assertEqual(state['history'], [])
        entry = state['pending'][0]
        marker = entry['premise_stale']
        self.assertEqual(marker['kind'], 'file_contains')
        self.assertIn('file_contains premise FALSE', marker['evidence'])
        self.assertIn('probed_at', marker)
        # read_at is NEVER touched by the probe leg.
        self.assertNotIn('read_at', entry)

    def test_true_and_indeterminate_untouched(self):
        self._guard_no_autoclear()
        self._write_state([
            _probe_entry('zz-fixture-true', OLD_TS, dict(_FC_PROBE)),
            _probe_entry('zz-fixture-indet', OLD_TS, dict(_FC_PROBE)),
        ])
        verdicts = {'zz-fixture-true': fp.TRUE, 'zz-fixture-indet': fp.INDETERMINATE}

        def _eval(probe):
            # key off the substring we vary per entry below
            return verdicts[probe['_id']]
        # tag each probe so the fake evaluate can tell them apart
        st = self._load()
        for e in st['pending']:
            e['dispatch_payload']['freshness_probe']['_id'] = e['id']
        self.pending_path.write_text(json.dumps(st))

        counts = self.mod.reconcile_stale_premises(now=NOW, evaluate=_eval)
        self.assertEqual(counts['probed'], 2)
        self.assertEqual(counts['demoted'], 0)
        self.assertEqual(counts['kept'], 2)
        for e in self._load()['pending']:
            self.assertNotIn('premise_stale', e)

    def test_no_freshness_probe_not_probed(self):
        self._guard_no_autoclear()
        # a plain approval with no probe — must be left entirely alone
        self._write_state([_entry('zz-fixture-noprobe', OLD_TS,
                                   task_id='zz-fixture-noprobe')])

        def _eval(_p):
            raise AssertionError('evaluate() must not run on a probe-less entry')
        counts = self.mod.reconcile_stale_premises(now=NOW, evaluate=_eval)
        self.assertEqual(counts, {
            'pending': 1, 'probed': 0, 'demoted': 0, 'kept': 0,
            'verified': 0, 'stale': 0, 'skipped': 0, 'failed': 0})
        self.assertNotIn('premise_stale', self._load()['pending'][0])

    def test_vanished_entry_is_not_counted_as_demoted(self):
        """The lost race: the entry is resolved between the probe read and the
        demote write, so demote() returns None. That is NOT a demotion — counting
        it would inflate `demoted` and emit a log line claiming a card was marked
        stale when nothing was written, which is the same false-clean shape this
        leg exists to prevent."""
        self._guard_no_autoclear()
        self._write_state([_probe_entry('zz-fixture-dead', OLD_TS, _FC_PROBE)])
        self._patch_approval('demote', lambda *a, **k: None)
        counts = self.mod.reconcile_stale_premises(
            now=NOW, evaluate=lambda _p: fp.FALSE)
        self.assertEqual(counts['probed'], 1)
        self.assertEqual(counts['demoted'], 0)   # NOT 1 — nothing was written
        self.assertNotIn('premise_stale', self._load()['pending'][0])

    def test_idempotent_second_tick(self):
        self._guard_no_autoclear()
        self._write_state([_probe_entry('zz-fixture-dead', OLD_TS, _FC_PROBE)])
        self.mod.reconcile_stale_premises(now=NOW, evaluate=lambda _p: fp.FALSE)
        first = self._load()['pending'][0]['premise_stale']
        # second tick over the already-demoted entry
        counts2 = self.mod.reconcile_stale_premises(
            now=NOW, evaluate=lambda _p: fp.FALSE)
        self.assertEqual(counts2['demoted'], 1)  # re-demoted (refresh), not escalated
        state = self._load()
        self.assertEqual(len(state['pending']), 1)     # no escalation to clear
        self.assertEqual(state['history'], [])         # nothing moved to history
        marker = state['pending'][0]['premise_stale']
        self.assertEqual(marker['kind'], first['kind'])  # single dict, not a list
        self.assertIsInstance(marker, dict)

    def test_dry_run_writes_nothing(self):
        self._guard_no_autoclear()
        self._write_state([_probe_entry('zz-fixture-dead', OLD_TS, _FC_PROBE)])
        counts = self.mod.reconcile_stale_premises(
            now=NOW, evaluate=lambda _p: fp.FALSE, dry_run=True)
        self.assertEqual(counts['demoted'], 1)  # classified
        self.assertNotIn('premise_stale', self._load()['pending'][0])  # not written

    def test_default_evaluate_keeps_inert_sql(self):
        # sql stays on the inert default executor -> INDETERMINATE -> keep, with
        # the REAL freshness_probe.evaluate (no injected evaluate).
        self._guard_no_autoclear()
        self._write_state([_probe_entry(
            'zz-fixture-sql', OLD_TS,
            {'kind': 'sql', 'query': 'select true'})])
        counts = self.mod.reconcile_stale_premises(now=NOW)
        self.assertEqual(counts['demoted'], 0)
        self.assertEqual(counts['kept'], 1)
        self.assertNotIn('premise_stale', self._load()['pending'][0])


# ---- verification stamps (approvals-freshness slice 2b) ----

class _StampRecorder:
    """Stand-in for chain_event_emit.stamp_approval_verification, capturing what
    the tick would project onto each card's Supabase row."""

    def __init__(self, ok=True):
        self.calls: list[tuple] = []
        self._ok = ok

    def __call__(self, task_id, verification, *, client=None):
        self.calls.append((task_id, dict(verification)))
        return self._ok

    @property
    def by_state(self):
        out: dict[str, list] = {}
        for task_id, v in self.calls:
            out.setdefault(v['state'], []).append((task_id, v))
        return out


# ECMA-262's Date Time String Format — what `Date.parse` accepts unambiguously.
_ES_DATETIME_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')


class TestVerificationStamps(_ProbeLegBase):
    """Slice 2b: the tick projects each verdict onto the card's chain_events row.

    The whole risk surface is SILENT failure — the dashboard reader renders a
    malformed or expired verdict as Unverified without erroring — so these tests
    pin the two invariants that keep the badge honest: every `verified` stamp
    carries a parseable probed_at, and it is re-stamped on EVERY tick rather than
    only on a change of verdict."""

    def _tick(self, verdict, *, stamp=None, client=None, **kw):
        stamp = stamp if stamp is not None else _StampRecorder()
        counts = self.mod.reconcile_stale_premises(
            evaluate=lambda _p: verdict,
            stamp=stamp,
            client=client if client is not None else object(),
            **kw)
        return stamp, counts

    def test_true_stamps_verified_with_probed_at(self):
        self._guard_no_autoclear()
        self._write_state([_probe_entry('zz-fixture-live', OLD_TS, _FC_PROBE)])
        stamp, counts = self._tick(fp.TRUE, now=NOW)
        self.assertEqual(counts['verified'], 1)
        self.assertEqual(counts['failed'], 0)
        self.assertEqual(len(stamp.calls), 1)
        task_id, verification = stamp.calls[0]
        self.assertEqual(task_id, 'zz-fixture-live')
        self.assertEqual(verification['state'], 'verified')
        self.assertIn('probed_at', verification)
        # A TRUE verdict must not demote or move the card.
        self.assertNotIn('premise_stale', self._load()['pending'][0])

    def test_every_verified_stamp_carries_a_date_parseable_probed_at(self):
        """Criterion 6. `probed_at` is OPTIONAL on the reader's type, but a
        `verified` verdict without a parseable one renders Unverified forever —
        the write succeeding and nothing raising is exactly what makes this
        invisible. The format must be the one ECMA-262 specifies, not merely one
        today's engines happen to tolerate."""
        self._guard_no_autoclear()
        self._write_state([
            _probe_entry('zz-fixture-a', OLD_TS, dict(_FC_PROBE)),
            _probe_entry('zz-fixture-b', OLD_TS, dict(_FC_PROBE)),
        ])
        stamp, _ = self._tick(fp.TRUE, now=NOW)
        self.assertEqual(len(stamp.calls), 2)
        for _task_id, verification in stamp.calls:
            probed_at = verification.get('probed_at')
            self.assertIsInstance(probed_at, str)
            self.assertRegex(probed_at, _ES_DATETIME_RE)
            # Round-trips back to the instant it claims (UTC, no offset drift).
            parsed = datetime.strptime(probed_at, '%Y-%m-%dT%H:%M:%S.%fZ')
            self.assertEqual(parsed.replace(tzinfo=timezone.utc), NOW)

    def test_second_consecutive_tick_restamps_with_newer_probed_at(self):
        """Criterion 5 — the guard that kills the transition-only optimization.
        The reader decays anything older than 30 min back to Unverified, so a
        verdict written once and re-written only 'when it changes' lights the
        badge for one window and then goes dark forever, silently. The verdict
        here does NOT change between ticks; the write must happen anyway, with a
        strictly newer probed_at."""
        self._guard_no_autoclear()
        self._write_state([_probe_entry('zz-fixture-live', OLD_TS, _FC_PROBE)])
        later = NOW + timedelta(minutes=10)

        stamp = _StampRecorder()
        self._tick(fp.TRUE, stamp=stamp, now=NOW)
        self._tick(fp.TRUE, stamp=stamp, now=later)

        self.assertEqual(len(stamp.calls), 2, 'second tick issued no write')
        first, second = stamp.calls[0][1], stamp.calls[1][1]
        self.assertEqual(second['state'], 'verified')
        self.assertGreater(second['probed_at'], first['probed_at'])
        self.assertNotEqual(second['probed_at'], first['probed_at'])

    def test_false_stamps_stale_with_evidence_after_demote(self):
        self._guard_no_autoclear()
        self._write_state([_probe_entry('zz-fixture-dead', OLD_TS, _FC_PROBE)])
        stamp, counts = self._tick(fp.FALSE, now=NOW)
        self.assertEqual(counts['stale'], 1)
        self.assertEqual(counts['demoted'], 1)
        task_id, verification = stamp.calls[0]
        self.assertEqual(task_id, 'zz-fixture-dead')
        self.assertEqual(verification['state'], 'stale')
        self.assertIn('file_contains premise FALSE', verification['evidence'])
        # Criterion 2: still pending, read_at untouched, nothing in history.
        state = self._load()
        self.assertEqual([e['id'] for e in state['pending']], ['zz-fixture-dead'])
        self.assertEqual(state['history'], [])
        self.assertNotIn('read_at', state['pending'][0])
        # A `stale` verdict does not decay, so it needs no probed_at.
        self.assertNotIn('probed_at', verification)

    def test_no_stale_stamp_when_the_demote_did_not_land(self):
        """The badge must never claim a demotion that didn't happen: if the entry
        vanished between probe and write, there is nothing to mark stale."""
        self._guard_no_autoclear()
        self._write_state([_probe_entry('zz-fixture-dead', OLD_TS, _FC_PROBE)])
        self._patch_approval('demote', lambda *a, **k: None)
        stamp, counts = self._tick(fp.FALSE, now=NOW)
        self.assertEqual(stamp.calls, [])
        self.assertEqual(counts['stale'], 0)
        self.assertEqual(counts['demoted'], 0)

    def test_indeterminate_stamps_nothing(self):
        """Criterion 3. Absence is the honest Unverified default — writing a
        verdict we could not determine is the false-clean shape this leg exists
        to prevent."""
        self._guard_no_autoclear()
        self._write_state([_probe_entry('zz-fixture-indet', OLD_TS, _FC_PROBE)])
        stamp, counts = self._tick(fp.INDETERMINATE, now=NOW)
        self.assertEqual(stamp.calls, [])
        self.assertEqual(counts['skipped'], 1)
        self.assertEqual(counts['verified'], 0)
        self.assertEqual(counts['stale'], 0)

    def test_probe_less_entry_stamps_nothing(self):
        self._guard_no_autoclear()
        self._write_state([_entry('zz-fixture-noprobe', OLD_TS,
                                  task_id='zz-fixture-noprobe')])
        stamp = _StampRecorder()
        counts = self.mod.reconcile_stale_premises(
            now=NOW, evaluate=lambda _p: fp.TRUE, stamp=stamp, client=object())
        self.assertEqual(stamp.calls, [])
        self.assertEqual(counts['probed'], 0)

    def test_dry_run_stamps_nothing(self):
        """Criterion 4."""
        self._guard_no_autoclear()
        self._write_state([
            _probe_entry('zz-fixture-dead', OLD_TS, dict(_FC_PROBE)),
        ])
        stamp, counts = self._tick(fp.FALSE, now=NOW, dry_run=True)
        self.assertEqual(stamp.calls, [])
        self.assertEqual(counts['stale'], 1)   # classified...
        self.assertFalse(self.mod.VERIFICATION_FAILURE_FILE.exists())

    def test_no_client_skips_stamps_but_still_demotes(self):
        """A Supabase outage must not stop the local demote: the card still gets
        marked, it just reads Unverified until a later tick stamps it."""
        self._guard_no_autoclear()
        self._write_state([_probe_entry('zz-fixture-dead', OLD_TS, _FC_PROBE)])
        stamp = _StampRecorder()
        counts = self.mod.reconcile_stale_premises(
            now=NOW, evaluate=lambda _p: fp.FALSE, stamp=stamp, client=None)
        self.assertEqual(stamp.calls, [])
        self.assertEqual(counts['skipped'], 1)
        self.assertEqual(counts['failed'], 0)
        self.assertEqual(counts['demoted'], 1)
        self.assertIn('premise_stale', self._load()['pending'][0])

    def test_uses_dispatch_payload_task_id_as_the_join_key(self):
        """The chain_events row is keyed by the dispatch task_id, which normally
        equals the entry id but is read through `_entry_task_id` so a divergent
        payload still stamps the right row."""
        self._guard_no_autoclear()
        entry = _probe_entry('zz-fixture-approval-id', OLD_TS, _FC_PROBE)
        entry['dispatch_payload']['task_id'] = 'zz-fixture-work-id'
        self._write_state([entry])
        stamp, _ = self._tick(fp.TRUE, now=NOW)
        self.assertEqual(stamp.calls[0][0], 'zz-fixture-work-id')


class TestVerificationFailureEscalation(_ProbeLegBase):
    """A permanently broken stamp is invisible by construction — the card just
    reads Unverified, which is also the honest default. Only an unbroken run of
    totally-failed ticks (~the 30-min decay window) escalates; single failures
    stay silent per the actionable-only alert doctrine."""

    def setUp(self):
        super().setUp()
        self.alerts: list[tuple] = []
        self.mod._alert_stamps_failing = (
            lambda streak, attempted: self.alerts.append((streak, attempted)))

    def _failing_tick(self, ok=False):
        self._guard_no_autoclear()
        return self.mod.reconcile_stale_premises(
            now=NOW, evaluate=lambda _p: fp.TRUE,
            stamp=_StampRecorder(ok=ok), client=object())

    def test_single_failed_tick_is_silent(self):
        self._write_state([_probe_entry('zz-fixture-live', OLD_TS, _FC_PROBE)])
        counts = self._failing_tick()
        self.assertEqual(counts['failed'], 1)
        self.assertEqual(counts['verified'], 0)
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.mod._read_fail_streak(), 1)

    def test_escalates_after_three_consecutive_all_failed_ticks(self):
        self._write_state([_probe_entry('zz-fixture-live', OLD_TS, _FC_PROBE)])
        for _ in range(2):
            self._failing_tick()
        self.assertEqual(self.alerts, [])
        self._failing_tick()
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0], (3, 1))

    def test_a_successful_tick_resets_the_streak(self):
        self._write_state([_probe_entry('zz-fixture-live', OLD_TS, _FC_PROBE)])
        self._failing_tick()
        self._failing_tick()
        self.assertEqual(self.mod._read_fail_streak(), 2)
        self._failing_tick(ok=True)
        self.assertEqual(self.mod._read_fail_streak(), 0)
        self._failing_tick()
        self.assertEqual(self.alerts, [])

    def test_a_tick_with_nothing_to_stamp_leaves_the_streak_alone(self):
        """A quiet period is not evidence the writer recovered — otherwise an
        empty queue would silently clear a genuinely broken writer's streak."""
        self._write_state([_probe_entry('zz-fixture-live', OLD_TS, _FC_PROBE)])
        self._failing_tick()
        self._failing_tick()
        self._write_state([])  # no pending cards at all
        self._failing_tick()
        self.assertEqual(self.mod._read_fail_streak(), 2)

    def test_unreadable_streak_file_reads_as_zero(self):
        self.mod.VERIFICATION_FAILURE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.mod.VERIFICATION_FAILURE_FILE.write_text('{not json')
        self.assertEqual(self.mod._read_fail_streak(), 0)


class TestTimerFreshnessCoupling(unittest.TestCase):
    """Criterion 7 — pin the coupling between the tick cadence and the reader's
    decay window. Nothing in either repo enforces this at runtime: relaxing the
    timer past ~1/3 of the window would leave `verified` stamps expiring between
    ticks, and the Checked badge would simply stop appearing. No error, no log
    line — so it is pinned here, where it fails loudly instead."""

    TIMER = (Path(__file__).resolve().parents[2] / 'systemd'
             / 'ourliberty-heal-stale-approvals.timer')

    def _interval_seconds(self):
        """The tick interval from `OnCalendar=*:0/N` (every N minutes)."""
        for line in self.TIMER.read_text().splitlines():
            line = line.strip()
            if not line.startswith('OnCalendar='):
                continue
            m = re.search(r'/(\d+)\s*$', line)
            self.assertIsNotNone(
                m, f'unrecognized OnCalendar form: {line!r} — if the schedule '
                   f'shape changed, this coupling check must be updated too')
            return int(m.group(1)) * 60
        self.fail(f'no OnCalendar= line in {self.TIMER}')

    def test_timer_fires_often_enough_to_keep_verified_stamps_fresh(self):
        import heal_stale_approvals as mod
        interval = self._interval_seconds()
        budget = mod.VERIFICATION_FRESH_SECONDS / 3
        self.assertLessEqual(
            interval, budget,
            f'the tick runs every {interval}s but a `verified` stamp decays '
            f'after {mod.VERIFICATION_FRESH_SECONDS}s; at least three stamps '
            f'per window (<= {budget:.0f}s apart) keeps the Checked badge lit '
            f'through a missed tick and the timer jitter')

    def test_freshness_window_matches_the_dashboard_reader(self):
        """VERIFICATION_FRESH_SECONDS mirrors VERIFICATION_FRESH_MS in the
        dashboard's PendingCard.tsx. It is duplicated rather than imported (other
        repo), so drift is caught here."""
        import heal_stale_approvals as mod
        self.assertEqual(mod.VERIFICATION_FRESH_SECONDS, 30 * 60)


# ---- mirror-review PR-coordinate reconcile (out-of-band merge phantoms) ----

MR_REPO = 'owner/zz-fixture-repo'
MR_ID = 'mirror-review-pr-zz-fixture-repo-845'
# The head8-suffixed shape outbox_notifier mints when a head SHA is present.
MR_ID_HEAD8 = 'mirror-review-pr-zz-fixture-repo-846-a1b2c3d4'


def _mr_entry(created_at=OLD_TS, *, approval_id=MR_ID, payload_task_id=None):
    """A session-less mirror-review PR approval as beacon registers it: add_pending
    sets entry id := dispatch_payload['task_id'], so by default the payload
    task_id equals the id (a genuine card). `payload_task_id` overrides only the
    payload to exercise the same-work guard."""
    tid = approval_id if payload_task_id is None else payload_task_id
    return _entry(approval_id, created_at, task_id=tid)


class _MirrorReviewBase(_Base):
    """_Base + the known-repo pin `parse_pr_coordinate` validates against, so
    the fixture coordinate resolves hermetically (no dependence on the real
    default repo list)."""

    def setUp(self):
        super().setUp()
        self._prev_repos = os.environ.get('OURLIBERTY_TERMINAL_STATE_REPOS')
        os.environ['OURLIBERTY_TERMINAL_STATE_REPOS'] = MR_REPO

    def tearDown(self):
        if self._prev_repos is None:
            os.environ.pop('OURLIBERTY_TERMINAL_STATE_REPOS', None)
        else:
            os.environ['OURLIBERTY_TERMINAL_STATE_REPOS'] = self._prev_repos
        super().tearDown()

    @staticmethod
    def _no_generic_probe(_tid):
        raise AssertionError('generic probe must not be consulted')

    @staticmethod
    def _no_coord_probe(_repo, _number):
        raise AssertionError('direct coordinate probe must not be consulted')


class TestClassifyMirrorReviewCoordinate(_MirrorReviewBase):
    """The 2026-07-08 gap: an approval id shaped mirror-review-pr-<repo>-<num>
    names its PR by NUMBER, which the generic token probe can never match — six
    cards kept DM-ing reminders for 4-11h after their PRs merged out-of-band.
    The direct path asks gh for that exact PR and is AUTHORITATIVE (no fall-through
    to the generic probe); the id-prefix + same-work guard keeps it scoped to
    Mirror's PR-decision cards. coord_probe signature is (repo, number)."""

    GRACE = 2.0

    def test_merged_pr_retires_with_state_and_sha_in_reason(self):
        retire, reason = self.mod.classify_terminal_approval(
            _mr_entry(), NOW, self.GRACE, self._no_generic_probe,
            coord_probe=lambda _r, _n: (tts.MERGED, 'abc123def456'))
        self.assertTrue(retire)
        self.assertIn('MERGED', reason)
        self.assertIn('abc123def456', reason)
        self.assertIn('#845', reason)

    def test_closed_pr_retires(self):
        retire, reason = self.mod.classify_terminal_approval(
            _mr_entry(), NOW, self.GRACE, self._no_generic_probe,
            coord_probe=lambda _r, _n: (tts.CLOSED, None))
        self.assertTrue(retire)
        self.assertIn('CLOSED', reason)

    def test_head8_suffixed_id_resolves_and_retires(self):
        # The hex-suffixed shape must take the direct path too (an all-digit
        # regex anchor would miss it → phantom recurs).
        got = []
        retire, reason = self.mod.classify_terminal_approval(
            _mr_entry(approval_id=MR_ID_HEAD8), NOW, self.GRACE,
            self._no_generic_probe,
            coord_probe=lambda r, n: (got.append((r, n)) or (tts.MERGED, 'sha9')))
        self.assertTrue(retire)
        self.assertEqual(got, [('owner/zz-fixture-repo', 846)])
        self.assertIn('#846', reason)

    def test_open_pr_kept(self):
        retire, reason = self.mod.classify_terminal_approval(
            _mr_entry(), NOW, self.GRACE, self._no_generic_probe,
            coord_probe=lambda _r, _n: (tts.OPEN, None))
        self.assertFalse(retire)
        self.assertIn('OPEN', reason)

    def test_gh_failure_keeps_without_consulting_generic_probe(self):
        # UNKNOWN (gh outage) keeps — and the coordinate probe is AUTHORITATIVE:
        # it must NOT fall through to a second, redundant generic gh probe.
        retire, reason = self.mod.classify_terminal_approval(
            _mr_entry(), NOW, self.GRACE, self._no_generic_probe,
            coord_probe=lambda _r, _n: (tts.UNKNOWN, None))
        self.assertFalse(retire)
        self.assertIn('UNKNOWN', reason)

    def test_within_grace_kept_even_if_pr_merged(self):
        retire, reason = self.mod.classify_terminal_approval(
            _mr_entry(FRESH_TS), NOW, self.GRACE, self._no_generic_probe,
            coord_probe=lambda _r, _n: (tts.MERGED, 'abc'))
        self.assertFalse(retire)
        self.assertIn('within grace', reason)

    def test_payload_targeting_other_work_skips_direct_probe(self):
        # GUARD: the id carries the wrapper but dispatch_payload.task_id names
        # different work (id != payload.task_id) -> never expire via the direct
        # path; falls through to the generic probe (here UNKNOWN -> keep).
        entry = _mr_entry(payload_task_id='zz-fixture-unrelated-task')
        retire, _ = self.mod.classify_terminal_approval(
            entry, NOW, self.GRACE, lambda _tid: tts.UNKNOWN,
            coord_probe=self._no_coord_probe)
        self.assertFalse(retire)

    def test_missing_payload_skips_direct_probe(self):
        entry = _mr_entry()
        entry['dispatch_payload'] = {}  # no task_id -> not a genuine card
        retire, _ = self.mod.classify_terminal_approval(
            entry, NOW, self.GRACE, lambda _tid: tts.UNKNOWN,
            coord_probe=self._no_coord_probe)
        self.assertFalse(retire)

    def test_bare_coordinate_id_without_wrapper_skips_direct_probe(self):
        # GUARD: a plain approval whose id merely looks coordinate-shaped
        # (`pr-<repo>-<num>`, no `mirror-review-` wrapper) is NOT a Mirror
        # PR-decision card and must never take the coordinate path.
        entry = _entry('pr-zz-fixture-repo-845', OLD_TS,
                       task_id='pr-zz-fixture-repo-845')
        retire, _ = self.mod.classify_terminal_approval(
            entry, NOW, self.GRACE, lambda _tid: tts.UNKNOWN,
            coord_probe=self._no_coord_probe)
        self.assertFalse(retire)

    def test_non_pr_approval_untouched_by_coord_path(self):
        # A plain approval (non-coordinate id) never consults the direct probe.
        retire, _ = self.mod.classify_terminal_approval(
            _entry('zz-fixture-plain', OLD_TS, task_id='zz-fixture-plain'),
            NOW, self.GRACE, lambda _tid: tts.UNKNOWN,
            coord_probe=self._no_coord_probe)
        self.assertFalse(retire)


class TestReconcileMirrorReviewEndToEnd(_MirrorReviewBase):
    """End-to-end against a tmp beacon-pending-approvals.json: the merged-PR
    card leaves pending[] as `expired` (no revision dispatched, card leaves the
    Approvals tab) with the PR state + merge sha journaled in the note; a live
    PR card and a non-PR approval stay pending."""

    def setUp(self):
        super().setUp()
        import beacon_approval_handler as approval
        self.approval = importlib.reload(approval)
        self.pending_path = self.approval.PENDING_APPROVALS_PATH

    def _write_state(self, pending):
        self.pending_path.write_text(json.dumps(
            {'version': 1, 'pending': pending, 'history': []}))

    def _load(self):
        return json.loads(self.pending_path.read_text())

    def test_out_of_band_merge_expires_only_that_card(self):
        open_id = 'mirror-review-pr-zz-fixture-repo-847'
        self._write_state([
            _mr_entry(),
            _mr_entry(approval_id=open_id),
            _entry('zz-fixture-plain', OLD_TS, task_id='zz-fixture-plain'),
        ])
        # coord_probe keyed by (repo, number) as classify calls it.
        coord_states = {
            ('owner/zz-fixture-repo', 845): (tts.MERGED, 'abc123def456'),
            ('owner/zz-fixture-repo', 847): (tts.OPEN, None),
        }
        counts = self.mod.reconcile_terminal_approvals(
            now=NOW, probe=lambda _tid: tts.UNKNOWN,
            coord_probe=lambda r, n: coord_states[(r, n)])

        self.assertEqual(counts['pending'], 3)
        self.assertEqual(counts['retired'], 1)
        self.assertEqual(counts['kept'], 2)

        state = self._load()
        self.assertEqual({e['id'] for e in state['pending']},
                         {open_id, 'zz-fixture-plain'})
        history = {e['id']: e for e in state['history']}
        self.assertEqual(history[MR_ID]['status'], 'expired')
        note = history[MR_ID].get('resolution_note', '')
        self.assertIn('MERGED', note)
        self.assertIn('abc123def456', note)

    def test_gh_outage_expires_nothing(self):
        self._write_state([_mr_entry()])
        counts = self.mod.reconcile_terminal_approvals(
            now=NOW, probe=lambda _tid: tts.UNKNOWN,
            coord_probe=lambda _r, _n: (tts.UNKNOWN, None))
        self.assertEqual(counts['retired'], 0)
        self.assertEqual([e['id'] for e in self._load()['pending']], [MR_ID])


# -------- resolved-in-supabase reconciliation (approval-sync §3.2) --------

def _action_row(task_id, action, ts=OLD_TS):
    """A `larry_action` audit row as the dashboard approve/reject handler writes
    it: top-level task_id = the source approval_request's task_id, payload.action
    the decision."""
    return {
        'event_id': f'la-{task_id}-{action}',
        'event_type': 'larry_action',
        'agent': 'dashboard',
        'task_id': task_id,
        'ts': ts,
        'read_at': None,
        'payload': {'action': action, 'source_event_id': f'ev-{task_id}'},
    }


class TestFetchLarryActions(_Base):
    def test_returns_decision_actions_only(self):
        store = [
            _action_row('A', 'approve'),
            _action_row('B', 'reject'),
            _action_row('C', 'comment'),                  # not a decision
            _row('x', 'approval_request', 'A', OLD_TS),   # wrong event_type
        ]
        got = self.mod.fetch_larry_actions(_FakeClient(store), {'A', 'B', 'C'})
        self.assertEqual(got, {'A': 'approve', 'B': 'reject'})

    def test_empty_taskids_short_circuits(self):
        self.assertEqual(self.mod.fetch_larry_actions(_FakeClient([]), set()), {})

    def test_latest_ts_wins(self):
        store = [
            _action_row('A', 'reject', ts=OLD_TS),
            _action_row('A', 'approve', ts=FRESH_TS),     # later -> wins
        ]
        self.assertEqual(
            self.mod.fetch_larry_actions(_FakeClient(store), {'A'}),
            {'A': 'approve'})


class TestReconcileResolvedInSupabase(_TerminalBase):
    def _pending(self):
        return [
            _entry('A', OLD_TS, task_id='A'),
            _entry('B', OLD_TS, task_id='B'),
            _entry('C', OLD_TS, task_id='C'),
        ]

    def _store(self):
        return [
            _action_row('A', 'approve'),
            _action_row('B', 'reject'),
            _action_row('C', 'comment'),    # non-decision -> C kept pending
            _action_row('ZZ', 'approve'),   # not pending -> ignored
        ]

    def test_pops_dashboard_resolved_keeps_undecided(self):
        self._write_state(self._pending())
        counts = self.mod.reconcile_resolved_in_supabase(
            _FakeClient(self._store()), now=NOW)
        self.assertEqual(counts, {'pending': 3, 'resolved': 2, 'kept': 1})
        state = self._load()
        self.assertEqual([e['id'] for e in state['pending']], ['C'])
        self.assertEqual(
            {e['id']: e['status'] for e in state['history']},
            {'A': 'approved', 'B': 'rejected'})

    def test_idempotent_second_run_pops_nothing(self):
        self._write_state(self._pending())
        client = _FakeClient(self._store())
        self.mod.reconcile_resolved_in_supabase(client, now=NOW)
        counts2 = self.mod.reconcile_resolved_in_supabase(client, now=NOW)
        self.assertEqual(counts2['resolved'], 0)
        self.assertEqual(counts2['kept'], 1)   # only C remains pending

    def test_dry_run_writes_nothing(self):
        self._write_state(self._pending())
        counts = self.mod.reconcile_resolved_in_supabase(
            _FakeClient(self._store()), now=NOW, dry_run=True)
        self.assertEqual(counts['resolved'], 2)
        state = self._load()
        self.assertEqual(len(state['pending']), 3)   # untouched
        self.assertEqual(state['history'], [])

    def test_no_pending_is_noop(self):
        self._write_state([])
        counts = self.mod.reconcile_resolved_in_supabase(
            _FakeClient(self._store()), now=NOW)
        self.assertEqual(counts, {'pending': 0, 'resolved': 0, 'kept': 0})


if __name__ == '__main__':
    unittest.main()
