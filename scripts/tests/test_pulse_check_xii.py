#!/usr/bin/env python3
"""Tests for pulse_check_xii (Check XII delivery-effectiveness meter, V1).

Covers the pure analysis core over synthetic merges / chain_events / costs /
missions: the mission-linkage throughput split, lead-time n-gating, the §5
partial-data contract (dark source => exit 0 + insufficient_signal, never a
page), the §3 artifact schema (sources block + INERT rules block), sentinel
naming + atomic write, retention prune, the §6 DM discipline (monthly digest
first-Monday-only, source-dark 2-consecutive-only, silent otherwise), and the
no-LLM-import self-check.

Run::

    cd ~/agent-core && python3 -m unittest discover -s scripts/tests
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_xii as pxii  # noqa: E402


# NOW is the first Monday of June 2026 (monthly-digest gate = True).
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
# The second Monday (a Monday, but NOT the first => digest silent).
NOW_SILENT = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
# Windows.ending(NOW): trailing [2026-05-04, 2026-06-01), prior [2026-04-06,
# 2026-05-04).
TRAILING_TS = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
PRIOR_TS = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)


def _merge(pr, task_id, ts=TRAILING_TS, repo='ourliberty-agent-core', **kw):
    d = {
        'pr_url': pr, 'number': int(pr.rsplit('/', 1)[-1]) if '/' in pr else 1,
        'repo': repo, 'merged_at': ts.isoformat(), 'task_id': task_id,
    }
    d.update(kw)
    return d


def _session_start(task_id, ts, agent='forge'):
    return {'event_type': 'session_start', 'ts': ts.isoformat(),
            'task_id': task_id, 'agent': agent}


def _mission(mid, phase, task_ids, **kw):
    d = {'id': mid, 'phase': phase, 'task_ids': task_ids}
    d.update(kw)
    return d


def _substrate(raw, now=NOW):
    return pxii._substrate_from_fixture(raw, now)


class TestMissionLinkageSplit(unittest.TestCase):
    """§2.1 throughput split: a merge whose task_id is in a non-proposed
    mission's task_ids counts as mission-linked; otherwise unlinked."""

    def test_linked_vs_unlinked(self):
        raw = {
            'merges': [
                _merge('pr/1', 'm-1'),          # linked (shipped mission)
                _merge('pr/2', 'x-9'),          # unlinked (no mission)
                _merge('pr/3', 'm-2'),          # linked (building mission)
            ],
            'missions': [
                _mission('mission-a', 'shipped', ['m-1']),
                _mission('mission-b', 'building', ['m-2']),
                _mission('mission-c', 'proposed', ['x-9']),  # proposed excluded
            ],
        }
        art = pxii.build_artifact(_substrate(raw), NOW)
        tp = art['metrics']['throughput']
        self.assertEqual(tp['merges_total']['current'], 3)
        self.assertEqual(tp['merges_mission_linked']['current'], 2)
        self.assertEqual(tp['merges_unlinked']['current'], 1)

    def test_proposed_mission_task_ids_are_not_linkage(self):
        ids = pxii.mission_task_ids([
            pxii.Mission(id='p', phase='proposed', task_ids=['only-proposed']),
            pxii.Mission(id='s', phase='shipped', task_ids=['real']),
        ])
        self.assertEqual(ids, {'real'})


class TestLeadTime(unittest.TestCase):
    """§2.2: dispatch->merge p50 needs n>=5; p90 reports insufficient_n < 20."""

    def test_p50_computed_with_five_merges(self):
        merges, chain = [], []
        # 5 forge dispatches 10h before each merge => p50 == 10.0h.
        for i in range(5):
            tid = f't-{i}'
            start = TRAILING_TS.replace(hour=2)
            merge_ts = TRAILING_TS.replace(hour=12)
            merges.append(_merge(f'pr/{i}', tid, ts=merge_ts))
            chain.append(_session_start(tid, start))
        art = pxii.build_artifact(_substrate({'merges': merges,
                                              'chain_events': chain}), NOW)
        lt = art['metrics']['lead_time']
        self.assertEqual(lt['dispatch_to_merge_p50_hours']['current'], 10.0)
        # n<20 => p90 insufficient_n.
        self.assertEqual(lt['dispatch_to_merge_p90_hours']['current'],
                         'insufficient_n')

    def test_p50_insufficient_below_five(self):
        merges, chain = [], []
        for i in range(3):
            tid = f't-{i}'
            merges.append(_merge(f'pr/{i}', tid))
            chain.append(_session_start(tid, TRAILING_TS.replace(hour=2)))
        art = pxii.build_artifact(_substrate({'merges': merges,
                                              'chain_events': chain}), NOW)
        lt = art['metrics']['lead_time']
        self.assertEqual(lt['dispatch_to_merge_p50_hours']['current'],
                         pxii.INSUFFICIENT)


class TestArtifactSchema(unittest.TestCase):
    """§3 artifact shape: as_of / check / window / sources / metrics + the
    INERT rules block (present but never fired in V1)."""

    def test_top_level_keys_and_check_id(self):
        art = pxii.build_artifact(_substrate({}), NOW)
        for key in ('as_of', 'check', 'window', 'sources', 'metrics', 'rules'):
            self.assertIn(key, art)
        self.assertEqual(art['check'], 'XII')
        self.assertEqual(set(art['sources']), set(pxii.SOURCES))

    def test_metric_blocks_present(self):
        art = pxii.build_artifact(_substrate({}), NOW)
        for block in ('throughput', 'lead_time', 'rework', 'escape', 'cost',
                      'demand'):
            self.assertIn(block, art['metrics'])

    def test_rules_block_inert(self):
        art = pxii.build_artifact(_substrate({}), NOW)
        rules = art['rules']
        self.assertEqual(len(rules), 6)
        for state in rules.values():
            self.assertIsNone(state['last_fired_at'])
            self.assertIsNone(state['armed_since'])
            self.assertFalse(state['suppressed_until_below'])

    def test_per_metric_block_shape(self):
        art = pxii.build_artifact(_substrate({'merges': [_merge('pr/1', None)]}),
                                  NOW)
        blk = art['metrics']['throughput']['merges_total']
        for f in ('current', 'prior', 'trend_pct', 'n', 'sources_ok'):
            self.assertIn(f, blk)


class TestPartialDataContract(unittest.TestCase):
    """§5: a dark source is a 0-exit with sources.<name>='error' and
    insufficient_signal blocks — NEVER a page."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pxii.ARTIFACT_DIR
        pxii.ARTIFACT_DIR = self.tmp / 'blackboard' / 'pulse-check-xii'

    def tearDown(self):
        pxii.ARTIFACT_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_fixture(self, raw):
        fx = self.tmp / 'fixture.json'
        fx.write_text(json.dumps(raw))
        return pxii.main(['--from-json', str(fx)])

    def test_dark_github_exits_zero_and_marks_error(self):
        # NOW_SILENT (not a first Monday) => main() takes no DM path.
        raw = {'now': NOW_SILENT.isoformat(), 'sources': {'github': 'error'},
               'missions': [_mission('m', 'shipped', ['m-1'])]}
        rc = self._run_fixture(raw)
        self.assertEqual(rc, 0)
        art = json.loads(
            (pxii.ARTIFACT_DIR / 'check-xii-2026-06-08.json').read_text())
        self.assertEqual(art['sources']['github'], 'error')
        self.assertEqual(
            art['metrics']['throughput']['merges_total']['current'],
            pxii.INSUFFICIENT)

    def test_all_sources_dark_still_exits_zero(self):
        raw = {'now': NOW_SILENT.isoformat(),
               'sources': {s: 'error' for s in pxii.SOURCES}}
        self.assertEqual(self._run_fixture(raw), 0)

    def test_write_failure_is_the_only_nonzero_exit(self):
        # Point ARTIFACT_DIR at a path whose parent is a file => mkdir raises.
        blocker = self.tmp / 'blocker'
        blocker.write_text('x')
        pxii.ARTIFACT_DIR = blocker / 'nested' / 'pulse-check-xii'
        raw = {'now': NOW_SILENT.isoformat()}
        fx = self.tmp / 'fixture.json'
        fx.write_text(json.dumps(raw))
        self.assertEqual(pxii.main(['--from-json', str(fx)]), 1)


class TestSentinelAndAtomicWrite(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pxii.ARTIFACT_DIR
        pxii.ARTIFACT_DIR = self.tmp / 'blackboard' / 'pulse-check-xii'

    def tearDown(self):
        pxii.ARTIFACT_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_artifact_path_is_dated_sentinel(self):
        path = pxii.artifact_path_for_date(NOW)
        self.assertTrue(str(path).endswith('check-xii-2026-06-01.json'))

    def test_write_leaves_no_tmp(self):
        art = pxii.build_artifact(_substrate({}), NOW)
        path = pxii.write_artifact(art, NOW)
        self.assertTrue(path.exists())
        self.assertEqual(list(path.parent.glob('*.tmp')), [])


class TestRetentionPrune(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pxii.ARTIFACT_DIR
        pxii.ARTIFACT_DIR = self.tmp / 'blackboard' / 'pulse-check-xii'
        pxii.ARTIFACT_DIR.mkdir(parents=True)

    def tearDown(self):
        pxii.ARTIFACT_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_keeps_only_most_recent(self):
        for day in range(1, 11):  # 10 dated artifacts
            (pxii.ARTIFACT_DIR / f'check-xii-2026-06-{day:02d}.json').write_text(
                '{}')
        pxii.prune_old_artifacts(keep_weeks=5)
        remaining = sorted(p.name for p in pxii.ARTIFACT_DIR.glob('*.json'))
        self.assertEqual(len(remaining), 5)
        self.assertEqual(remaining[0], 'check-xii-2026-06-06.json')
        self.assertEqual(remaining[-1], 'check-xii-2026-06-10.json')


class _DmCapture(unittest.TestCase):
    """Base: stub larry_alerts so no live DM fires and calls are captured."""

    def setUp(self):
        self._orig = sys.modules.get('larry_alerts')
        self.calls: list[dict] = []
        fake = types.ModuleType('larry_alerts')

        def append_alert(*, source, severity, message, subject=None,
                         route=None, **_):
            self.calls.append({'source': source, 'severity': severity,
                               'subject': subject, 'route': route})
            return True

        fake.append_alert = append_alert
        sys.modules['larry_alerts'] = fake

    def tearDown(self):
        if self._orig is not None:
            sys.modules['larry_alerts'] = self._orig
        else:
            sys.modules.pop('larry_alerts', None)


class TestDmDiscipline(_DmCapture):
    """§6 DM routing: monthly digest first-Monday-only; source-dark only after
    2 consecutive dark runs; silent otherwise."""

    def test_monthly_digest_fires_on_first_monday(self):
        art = pxii.build_artifact(_substrate({}), NOW)
        dmed = pxii.maybe_dm(art, NOW, prior=None)
        self.assertIn('pulse-check-xii-monthly-digest', dmed)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['source'], 'pulse-check-xii')
        self.assertEqual(self.calls[0]['severity'], 'info')

    def test_silent_on_non_first_monday_all_ok(self):
        art = pxii.build_artifact(_substrate({}, now=NOW_SILENT), NOW_SILENT)
        dmed = pxii.maybe_dm(art, NOW_SILENT, prior=None)
        self.assertEqual(dmed, [])
        self.assertEqual(self.calls, [])

    def test_source_dark_needs_two_consecutive_runs(self):
        raw = {'sources': {'github': 'error'}}
        art = pxii.build_artifact(_substrate(raw, now=NOW_SILENT), NOW_SILENT)
        # First dark run (prior clean) => no source-dark DM.
        prior_clean = {'sources': {s: 'ok' for s in pxii.SOURCES}}
        self.assertEqual(pxii.maybe_dm(art, NOW_SILENT, prior_clean), [])
        self.assertEqual(self.calls, [])
        # Second consecutive dark run (prior also dark) => fires.
        prior_dark = {'sources': {'github': 'error'}}
        dmed = pxii.maybe_dm(art, NOW_SILENT, prior_dark)
        self.assertIn('pulse-check-xii-source-dark:github', dmed)
        self.assertEqual(self.calls[-1]['severity'], 'warning')


class TestDmTranslationsExist(unittest.TestCase):
    """The §6 subjects the meter emits must have translation entries so the
    beacon-bot renders them (the meter's _dm passes source as a variable, so
    the static coverage test can't enforce this — assert it here)."""

    def test_subjects_resolve(self):
        import larry_alerts as la
        la._TRANSLATIONS_CACHE = None  # noqa: SLF001
        for subject in ('pulse-check-xii-monthly-digest',
                        'pulse-check-xii-source-dark:github',
                        'pulse-check-xii-rule:1'):
            self.assertIsNotNone(
                la.translate_alert('pulse-check-xii', subject),
                f'no translation for {subject!r}')


class TestTrendAndPercentile(unittest.TestCase):

    def test_trend_insufficient_on_zero_prior(self):
        self.assertEqual(pxii._trend_pct(5, 0), pxii.INSUFFICIENT)
        self.assertEqual(pxii._trend_pct(5, None), pxii.INSUFFICIENT)
        self.assertEqual(pxii._trend_pct(15, 10), 50.0)

    def test_percentile_nearest_rank(self):
        self.assertIsNone(pxii._percentile([], 0.9))
        self.assertEqual(pxii._percentile([1, 2, 3, 4, 5], 0.0), 1)
        self.assertEqual(pxii._percentile([1, 2, 3, 4, 5], 1.0), 5)


class TestNoLLMCalls(unittest.TestCase):
    """Mirror review focus: the meter must not import an LLM client."""

    def test_module_does_not_import_llm_client(self):
        src = (_REPO_SCRIPTS / 'pulse_check_xii.py').read_text()
        for forbidden in ('import anthropic', 'from anthropic',
                          'import openai', 'from openai'):
            self.assertNotIn(forbidden, src)


if __name__ == '__main__':
    unittest.main()
