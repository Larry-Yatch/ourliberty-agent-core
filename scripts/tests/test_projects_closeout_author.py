#!/usr/bin/env python3
"""Tests for projects-v3 P4 step p4-closeout-author.

When a phase reaches Done (SEQUENCE_COMPLETE + status=done), the closeout author
reads the merged PRs + spec + North Star and authors the closeout — a plain
summary plus the structured schema (shipped · changed-vs-spec · learnings · cost
· done-gate met?) — onto the phase card, and ticks the North Star status
tracker. This covers the three Mirror-review foci:

  * SINGLE-COMMITTER — the author is pure; the card write is the
    `projects_status_writeback.attach_closeout` NON-committer and the North Star
    tick is a NON-committer doc write; heal_projects_store stays sole committer.
  * DETERMINISTIC FALLBACK / NO RAW-METADATA LEAK — the raw fallback renders
    neutral prose referencing only PR numbers, never task ids / branches / seq
    ids / raw spec text; cost + done_gate are computed deterministically.
  * NORTH STAR TICKED — the §9 status-tracker row flips to done (status cell
    only), idempotently, and never ticks an unrelated row.

Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_projects_closeout_author
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import projects_store as ps                 # noqa: E402
import projects_status_writeback as psw     # noqa: E402
import projects_closeout_author as author   # noqa: E402
from test_isolation_guard import TestIsolationBreach  # noqa: E402


def _phase(pid, *, title=None, state='building', sequence_ref=None, closeout=None):
    ph = {
        'id': pid,
        'title': title if title is not None else pid,
        'desired_end_state': 'the thing is done and live',
        'lifecycle_state': state,
        'order': 0,
        'spec_ref': None,
        'sequence_ref': sequence_ref,
        'created_at': '2026-06-17T00:00:00+00:00',
        'updated_at': '2026-06-17T00:00:00+00:00',
    }
    if closeout is not None:
        ph['closeout'] = closeout
    return ph


def _project(pid, phases, *, north_star_ref=None):
    return {
        'id': pid, 'title': pid, 'north_star_ref': north_star_ref, 'repo': None,
        'state': 'active', 'phases': phases, 'one_off': len(phases) == 1,
        'created_at': '2026-06-17T00:00:00+00:00',
        'updated_at': '2026-06-17T00:00:00+00:00',
    }


def _registry(*projects):
    return {'schema_version': ps.SCHEMA_VERSION, 'projects': list(projects)}


_TRACKER_DOC = (
    '# North Star\n'
    '\n'
    '## §9 Status tracker\n'
    '\n'
    '| Phase | What | Status |\n'
    '| --- | --- | --- |\n'
    '| P4 | Closeout pass | 🚧 specced — author lands soon |\n'
    '| P5 | DAG view | ▫️ planned |\n'
    '\n'
    'Legend: ✅ done · 🚧 in flight · ▫️ planned\n'
)


# --------------------------------------------------------------------------- #
# the pure author — schema correctness
# --------------------------------------------------------------------------- #
class AuthorSchemaTests(unittest.TestCase):
    def test_schema_shape_and_provenance(self):
        phase = _phase('p4-closeout-author', title='Closeout pass')
        project = _project('proj', [phase])
        context = {
            'pr_numbers': ['512'], 'cost_usd': None, 'all_steps_merged': True,
        }
        out = author.author_phase_closeout(
            phase, project, context, use_llm=False)
        self.assertIn('closeout', out)
        self.assertIn('closeout_provenance', out)
        co = out['closeout']
        self.assertEqual(
            set(co),
            {'summary', 'shipped', 'changed_vs_spec', 'learnings', 'cost',
             'done_gate_met'})
        for k in ('summary', 'shipped', 'changed_vs_spec', 'learnings', 'cost'):
            self.assertIsInstance(co[k], str)
            self.assertTrue(co[k].strip())
        self.assertIsInstance(co['done_gate_met'], bool)
        prov = out['closeout_provenance']
        self.assertEqual(prov['by'], author.CLOSEOUT_BY)
        self.assertEqual(prov['model'], 'raw')  # use_llm=False → deterministic
        self.assertEqual(prov['from_state'], 'building')
        self.assertEqual(prov['pr_numbers'], ['512'])

    def test_author_is_pure_does_not_mutate_inputs(self):
        phase = _phase('p4', title='Closeout')
        project = _project('proj', [phase])
        before = json.dumps(phase, sort_keys=True)
        author.author_phase_closeout(phase, project, {}, use_llm=False)
        self.assertEqual(json.dumps(phase, sort_keys=True), before)
        self.assertNotIn('closeout', phase)

    def test_use_llm_true_under_test_is_guarded(self):
        # The LLM voice is guarded by refuse_under_test('claude-spawn'); calling
        # the author with use_llm=True from a test process must surface the
        # breach rather than silently spawning claude.
        phase = _phase('p4')
        with self.assertRaises(TestIsolationBreach):
            author.author_phase_closeout(
                phase, _project('p', [phase]), {}, use_llm=True)


# --------------------------------------------------------------------------- #
# deterministic fallback — NO raw-metadata leak
# --------------------------------------------------------------------------- #
class DeterministicFallbackTests(unittest.TestCase):
    def test_raw_closeout_references_pr_numbers_only(self):
        phase = _phase(
            'p4-closeout-author', title='Closeout pass',
            sequence_ref='launch-7f3a2b9c')
        project = _project('proj-secret', [phase])
        context = {
            'pr_numbers': ['512', '514'],
            'spec_excerpt': 'INTERNAL_SPEC_SENTINEL do not leak',
            'commit_subjects': ['chore: TASK-9001 wip'],
            'changed_files': ['scripts/secret_internal.py'],
        }
        raw = author.render_raw_phase_closeout(phase, project, context)
        blob = ' '.join(raw.values())
        # PR numbers ARE allowed (clickable identifiers).
        self.assertIn('#512', blob)
        self.assertIn('#514', blob)
        # The human-facing phase title is fine.
        self.assertIn('Closeout pass', blob)
        # But NO raw metadata: branch / seq id / task id / internal paths / spec.
        for leak in ('launch-7f3a2b9c', '7f3a2b9c', 'TASK-9001',
                     'secret_internal.py', 'INTERNAL_SPEC_SENTINEL',
                     'proj-secret'):
            self.assertNotIn(leak, blob, f'raw closeout leaked {leak!r}')

    def test_raw_closeout_with_no_prs_is_clean(self):
        phase = _phase('p4', title='Closeout')
        raw = author.render_raw_phase_closeout(phase, _project('p', [phase]), {})
        blob = ' '.join(raw.values())
        self.assertIn('Closeout', blob)
        self.assertNotIn('#', blob)  # no dangling "(PR #)" when no PRs

    def test_render_cost_is_deterministic(self):
        self.assertEqual(
            author.render_cost({'cost_usd': 12.5}), 'about $12.50 in build time.')
        self.assertEqual(
            author.render_cost({'pr_numbers': ['1']}), 'One PR of build work.')
        self.assertEqual(
            author.render_cost({'pr_numbers': ['1', '2', '3']}),
            '3 PRs of build work.')
        self.assertEqual(author.render_cost({}), 'Not separately tracked.')

    def test_render_done_gate_met_is_deterministic(self):
        self.assertTrue(author.render_done_gate_met({'all_steps_merged': True}))
        self.assertFalse(author.render_done_gate_met({'all_steps_merged': False}))
        # Unknown (no gathered step state) defaults True — phase reached this via
        # a verified SEQUENCE_COMPLETE.
        self.assertTrue(author.render_done_gate_met({}))


# --------------------------------------------------------------------------- #
# context gather — fail-safe + never spawns gh under test
# --------------------------------------------------------------------------- #
class GatherContextTests(unittest.TestCase):
    def test_gather_with_no_steps_does_not_spawn(self):
        # No PR urls → no gh round-trip → no guard breach; doc excerpts read.
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / 'spec.md').write_text('SPEC BODY', encoding='utf-8')
            phase = _phase('p4')
            phase['spec_ref'] = 'spec.md'
            ctx = author.gather_closeout_context(
                phase, _project('p', [phase]), {'steps': []}, repo=repo)
        self.assertEqual(ctx['pr_numbers'], [])
        self.assertEqual(ctx['spec_excerpt'], 'SPEC BODY')
        self.assertFalse(ctx['all_steps_merged'])

    def test_gather_with_prs_hits_the_under_test_guard(self):
        # A real gather over PR urls must reach refuse_under_test('gh-spawn') —
        # proving the IO is guarded and never hits the network from a test.
        phase = _phase('p4')
        seq = {'steps': [{'pr_url': 'https://github.com/o/r/pull/9', 'status': 'merged'}]}
        with self.assertRaises(TestIsolationBreach):
            author.gather_closeout_context(phase, _project('p', [phase]), seq)


# --------------------------------------------------------------------------- #
# pure store helper + card passthrough
# --------------------------------------------------------------------------- #
class StoreAttachTests(unittest.TestCase):
    def _fields(self):
        return {
            'closeout': {
                'summary': 's', 'shipped': 'sh', 'changed_vs_spec': 'c',
                'learnings': 'l', 'cost': 'One PR of build work.',
                'done_gate_met': True,
            },
            'closeout_provenance': {'by': 'beacon', 'model': 'raw'},
        }

    def test_attach_then_idempotent(self):
        ph = _phase('p4')
        self.assertTrue(ps.attach_phase_closeout(ph, self._fields()))
        self.assertEqual(ph['closeout']['summary'], 's')
        self.assertEqual(ph['closeout_provenance']['by'], 'beacon')
        # Re-attaching the identical payload is a no-op (no spurious delta).
        self.assertFalse(ps.attach_phase_closeout(ph, self._fields()))

    def test_attach_safe_on_junk(self):
        self.assertFalse(ps.attach_phase_closeout('nope', self._fields()))
        self.assertFalse(ps.attach_phase_closeout({}, {'closeout': 'not-a-dict'}))
        self.assertFalse(ps.attach_phase_closeout({}, {}))

    def test_phase_card_passes_closeout_through(self):
        co = {'summary': 's', 'done_gate_met': True}
        carded = ps._phase_card(_phase('p4', closeout=co))
        self.assertEqual(carded['closeout'], co)
        # Absent when the phase has no closeout (open phase).
        self.assertNotIn('closeout', ps._phase_card(_phase('p4')))


# --------------------------------------------------------------------------- #
# attach_closeout — NON-committer on-disk writer
# --------------------------------------------------------------------------- #
class WritebackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / 'projects.json'
        self._prev = os.environ.get('OURLIBERTY_PROJECTS_JSON')
        os.environ['OURLIBERTY_PROJECTS_JSON'] = str(self.path)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('OURLIBERTY_PROJECTS_JSON', None)
        else:
            os.environ['OURLIBERTY_PROJECTS_JSON'] = self._prev
        self._tmp.cleanup()

    def _write(self, registry):
        self.path.write_text(json.dumps(registry, indent=2) + '\n')

    def _fields(self):
        return author.author_phase_closeout(
            _phase('ph1', sequence_ref='launch-ph1'),
            _project('p1', []),
            {'pr_numbers': ['7'], 'all_steps_merged': True},
            use_llm=False,
        )

    def test_attach_writes_closeout_to_matching_phase(self):
        self._write(_registry(_project('p1', [
            _phase('ph1', state='done', sequence_ref='launch-ph1')])))
        self.assertTrue(
            psw.attach_closeout(seq_id='launch-ph1', closeout_fields=self._fields()))
        reg = json.loads(self.path.read_text())
        _, phase = ps.find_phase_by_sequence_ref(reg, 'launch-ph1')
        self.assertIn('closeout', phase)
        self.assertTrue(phase['closeout']['summary'].strip())
        self.assertEqual(phase['closeout_provenance']['model'], 'raw')

    def test_attach_idempotent_no_rewrite(self):
        self._write(_registry(_project('p1', [
            _phase('ph1', state='done', sequence_ref='launch-ph1')])))
        fields = self._fields()
        self.assertTrue(
            psw.attach_closeout(seq_id='launch-ph1', closeout_fields=fields))
        before = self.path.read_bytes()
        self.assertFalse(
            psw.attach_closeout(seq_id='launch-ph1', closeout_fields=fields))
        self.assertEqual(self.path.read_bytes(), before)

    def test_attach_non_launch_sequence_is_noop(self):
        self._write(_registry(_project('p1', [_phase('ph1', state='spec')])))
        before = self.path.read_bytes()
        self.assertFalse(
            psw.attach_closeout(seq_id='ordinary-seq-1', closeout_fields=self._fields()))
        self.assertEqual(self.path.read_bytes(), before)

    def test_attach_does_not_git_commit(self):
        self._write(_registry(_project('p1', [
            _phase('ph1', state='done', sequence_ref='launch-ph1')])))
        psw.attach_closeout(seq_id='launch-ph1', closeout_fields=self._fields())
        siblings = {p.name for p in self.path.parent.iterdir()}
        self.assertNotIn('.git', siblings)


# --------------------------------------------------------------------------- #
# North Star status-tracker tick
# --------------------------------------------------------------------------- #
class NorthStarTickTests(unittest.TestCase):
    def test_tick_flips_matching_row_status_only(self):
        phase = _phase('p4-closeout-author', title='Closeout pass')
        out = author.tick_north_star_tracker(_TRACKER_DOC, phase)
        self.assertIsNotNone(out)
        self.assertIn('| P4 | Closeout pass | ✅ done |', out)
        # Other rows + prose preserved (status-only edit, no prose rewrite).
        self.assertIn('| P5 | DAG view | ▫️ planned |', out)
        self.assertIn('## §9 Status tracker', out)
        self.assertIn('Legend: ✅ done', out)
        # The old status text for P4 is gone.
        self.assertNotIn('🚧 specced — author lands soon', out)

    def test_tick_is_idempotent(self):
        phase = _phase('p4-closeout-author', title='Closeout pass')
        once = author.tick_north_star_tracker(_TRACKER_DOC, phase)
        self.assertIsNone(author.tick_north_star_tracker(once, phase))

    def test_tick_no_matching_row_is_noop(self):
        phase = _phase('zz-unrelated', title='Something else entirely')
        self.assertIsNone(author.tick_north_star_tracker(_TRACKER_DOC, phase))

    def test_tick_never_ticks_unrelated_row(self):
        phase = _phase('p5-dag-view', title='DAG view')
        out = author.tick_north_star_tracker(_TRACKER_DOC, phase)
        self.assertIsNotNone(out)
        self.assertIn('| P5 | DAG view | ✅ done |', out)
        # P4 untouched.
        self.assertIn('🚧 specced', out)

    def test_tick_safe_on_empty(self):
        self.assertIsNone(author.tick_north_star_tracker('', _phase('p4')))
        self.assertIsNone(author.tick_north_star_tracker(None, _phase('p4')))


# --------------------------------------------------------------------------- #
# write_north_star_tick — NON-committer doc write
# --------------------------------------------------------------------------- #
class WriteNorthStarTickTests(unittest.TestCase):
    def test_write_ticks_absolute_ref_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            doc = Path(d) / 'north-star.md'
            doc.write_text(_TRACKER_DOC, encoding='utf-8')
            phase = _phase('p4-closeout-author', title='Closeout pass')
            project = _project('p', [phase], north_star_ref=str(doc))
            self.assertTrue(author.write_north_star_tick(None, project, phase))
            self.assertIn('| P4 | Closeout pass | ✅ done |',
                          doc.read_text(encoding='utf-8'))
            # Idempotent: already ticked → no write.
            self.assertFalse(author.write_north_star_tick(None, project, phase))
            # Non-committer: no git repo created beside the doc.
            self.assertNotIn('.git', {p.name for p in doc.parent.iterdir()})

    def test_write_no_ref_is_noop(self):
        phase = _phase('p4')
        self.assertFalse(
            author.write_north_star_tick(None, _project('p', [phase]), phase))

    def test_write_relative_ref_without_repo_is_noop(self):
        phase = _phase('p4-closeout-author', title='Closeout pass')
        project = _project('p', [phase], north_star_ref='docs/north-star.md')
        self.assertFalse(author.write_north_star_tick(None, project, phase))


# --------------------------------------------------------------------------- #
# phase resolution — sequence_ref primary, launch-drain audit fallback. The
# closeout must resolve its phase even when the building-stamp never persisted
# the sequence_ref (the 2026-06-19 EROFS failure).
# --------------------------------------------------------------------------- #
class NarratorFindPhaseTests(unittest.TestCase):
    def _seq(self, *, seq_id='launch-ph1', project_id='p1', phase_id='ph1',
             with_audit=True):
        audit = ([{'event': 'authored-by-launch-drain',
                   'project_id': project_id, 'phase_id': phase_id}]
                 if with_audit else [{'event': 'step-dispatched'}])
        return {'seq_id': seq_id, 'audit_log': audit}

    def test_resolves_by_sequence_ref(self):
        reg = _registry(_project('p1', [
            _phase('ph1', state='building', sequence_ref='launch-ph1')]))
        # No audit entry needed — the pinned ref wins.
        project, phase = author.narrator_find_phase(
            reg, self._seq(with_audit=False))
        self.assertEqual((project['id'], phase['id']), ('p1', 'ph1'))

    def test_audit_fallback_when_sequence_ref_absent(self):
        # sequence_ref never persisted (EROFS) → resolve by the launch-drain ids.
        reg = _registry(_project('p1', [
            _phase('ph1', state='done', sequence_ref=None)]))
        project, phase = author.narrator_find_phase(reg, self._seq())
        self.assertEqual((project['id'], phase['id']), ('p1', 'ph1'))

    def test_non_launch_sequence_returns_none(self):
        reg = _registry(_project('p1', [_phase('ph1', state='spec')]))
        self.assertEqual(
            (None, None),
            author.narrator_find_phase(
                reg, {'seq_id': 'feat-1', 'audit_log': [{'event': 'x'}]}))


if __name__ == '__main__':
    unittest.main()
