#!/usr/bin/env python3
"""Tests for projects-v3 P6 step p6-brainstorm-autofill-author.

When a phase enters the Brainstorm lifecycle state, the brainstorm author
pre-fills — onto the phase card — a draft of the 8-section brainstorm template, a
capped "Your decisions" list (<=5), and the deterministic handoff-prompt payload
the dashboard assembles into the "Copy handoff for Claude" prompt. This covers
the three Mirror-review foci:

  * SINGLE-COMMITTER — the author is pure; the card write is the in-registry
    mutation via `projects_store.attach_phase_brainstorm`; the sweep mutates the
    registry the projects-store healer already holds — heal_projects_store stays
    the SOLE committer (no second writer).
  * DECISIONS CAPPED <=5 — `_cap_decisions` enforces the cap deterministically;
    overflow is flagged, never dropped silently.
  * DETERMINISTIC FALLBACK / NO RAW-METADATA LEAK — the raw fallback renders
    neutral prose referencing only the human-facing phase/project titles + the
    Desired End State, never task ids / branches / seq ids / raw spec or North
    Star text; the handoff payload is assembled deterministically (no model call).

Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_projects_brainstorm_author
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import projects_store as ps                   # noqa: E402
import projects_brainstorm_author as author   # noqa: E402
from test_isolation_guard import TestIsolationBreach  # noqa: E402


def _phase(pid, *, title=None, state='brainstorm', desired_end_state='the thing is done and live',
           closeout=None, order=0, brainstorm=None):
    ph = {
        'id': pid,
        'title': title if title is not None else pid,
        'desired_end_state': desired_end_state,
        'lifecycle_state': state,
        'order': order,
        'spec_ref': None,
        'sequence_ref': None,
        'created_at': '2026-06-21T00:00:00+00:00',
        'updated_at': '2026-06-21T00:00:00+00:00',
    }
    if closeout is not None:
        ph['closeout'] = closeout
    if brainstorm is not None:
        ph['brainstorm'] = brainstorm
    return ph


def _project(pid, phases, *, title=None, north_star_ref=None, repo=None):
    return {
        'id': pid, 'title': title if title is not None else pid,
        'north_star_ref': north_star_ref, 'repo': repo,
        'state': 'active', 'phases': phases, 'one_off': len(phases) == 1,
        'created_at': '2026-06-21T00:00:00+00:00',
        'updated_at': '2026-06-21T00:00:00+00:00',
    }


def _registry(*projects):
    return {'schema_version': ps.SCHEMA_VERSION, 'projects': list(projects)}


# --------------------------------------------------------------------------- #
# the pure author — schema correctness
# --------------------------------------------------------------------------- #
class AuthorSchemaTests(unittest.TestCase):
    def test_schema_shape_and_provenance(self):
        phase = _phase('p6-brainstorm-autofill-author', title='Brainstorm autofill')
        project = _project('proj', [phase])
        out = author.author_phase_brainstorm(phase, project, {}, use_llm=False)
        self.assertIn('brainstorm', out)
        self.assertIn('brainstorm_provenance', out)
        bs = out['brainstorm']
        self.assertEqual(
            set(bs),
            {'is_ai_draft', 'draft', 'decisions', 'decisions_overflow', 'handoff'})
        self.assertIs(bs['is_ai_draft'], True)
        # The draft carries exactly the 8 template sections, all non-empty strings.
        self.assertEqual(set(bs['draft']), set(author.BRAINSTORM_SECTION_KEYS))
        for k in author.BRAINSTORM_SECTION_KEYS:
            self.assertIsInstance(bs['draft'][k], str)
            self.assertTrue(bs['draft'][k].strip())
        self.assertIsInstance(bs['decisions'], list)
        self.assertLessEqual(len(bs['decisions']), author.MAX_DECISIONS)
        self.assertIsInstance(bs['decisions_overflow'], bool)
        self.assertIsInstance(bs['handoff'], dict)
        prov = out['brainstorm_provenance']
        self.assertEqual(prov['by'], author.BRAINSTORM_BY)
        self.assertEqual(prov['model'], 'raw')  # use_llm=False → deterministic
        self.assertEqual(prov['from_state'], 'brainstorm')

    def test_author_is_pure_does_not_mutate_inputs(self):
        phase = _phase('p6', title='Brainstorm')
        project = _project('proj', [phase])
        before = json.dumps(phase, sort_keys=True)
        author.author_phase_brainstorm(phase, project, {}, use_llm=False)
        self.assertEqual(json.dumps(phase, sort_keys=True), before)
        self.assertNotIn('brainstorm', phase)

    def test_use_llm_true_under_test_is_guarded(self):
        # The LLM voice is guarded by refuse_under_test('claude-spawn'); calling
        # the author with use_llm=True from a test process must surface the breach
        # rather than silently spawning claude.
        phase = _phase('p6')
        with self.assertRaises(TestIsolationBreach):
            author.author_phase_brainstorm(
                phase, _project('p', [phase]), {}, use_llm=True)


# --------------------------------------------------------------------------- #
# idempotency guard — which phases need a brainstorm draft
# --------------------------------------------------------------------------- #
class NeedsBrainstormTests(unittest.TestCase):
    def test_brainstorm_state_without_draft_needs_one(self):
        self.assertTrue(author.needs_brainstorm(_phase('p6', state='brainstorm')))

    def test_non_brainstorm_state_never_needs_one(self):
        for state in ('spec', 'building', 'done'):
            self.assertFalse(author.needs_brainstorm(_phase('p6', state=state)))

    def test_already_drafted_for_current_state_is_idempotent(self):
        phase = _phase('p6', state='brainstorm')
        fields = author.author_phase_brainstorm(
            phase, _project('p', [phase]), {}, use_llm=False)
        ps.attach_phase_brainstorm(phase, fields)
        self.assertFalse(author.needs_brainstorm(phase))

    def test_draft_stamped_from_other_state_needs_reauthor(self):
        phase = _phase('p6', state='brainstorm', brainstorm={'is_ai_draft': True})
        phase['brainstorm_provenance'] = {'from_state': 'spec'}
        self.assertTrue(author.needs_brainstorm(phase))

    def test_safe_on_junk(self):
        self.assertFalse(author.needs_brainstorm('nope'))
        self.assertFalse(author.needs_brainstorm({}))


# --------------------------------------------------------------------------- #
# deterministic fallback — NO raw-metadata leak + thin-context degrade
# --------------------------------------------------------------------------- #
class DeterministicFallbackTests(unittest.TestCase):
    def test_raw_brainstorm_leaks_no_metadata(self):
        phase = _phase(
            'p6-brainstorm-autofill-author', title='Brainstorm autofill',
            desired_end_state='every Brainstorm phase arrives pre-filled')
        phase['sequence_ref'] = 'launch-7f3a2b9c'
        phase['spec_ref'] = 'agents/beacon/specs/secret-internal.md'
        project = _project('proj-secret', [phase], title='Projects v3')
        context = {
            'north_star_excerpt': 'INTERNAL_NORTH_STAR_SENTINEL do not leak',
            'prior_closeout': {'summary': 'PRIOR_CLOSEOUT_SENTINEL'},
        }
        raw = author.render_raw_brainstorm(phase, project, context)
        blob = ' '.join(raw.values())
        # Human-facing labels are fine.
        self.assertIn('Brainstorm autofill', blob)
        self.assertIn('Projects v3', blob)
        # But NO raw metadata: branch / seq id / project id / spec path / excerpts.
        for leak in ('launch-7f3a2b9c', '7f3a2b9c', 'proj-secret',
                     'secret-internal.md', 'INTERNAL_NORTH_STAR_SENTINEL',
                     'PRIOR_CLOSEOUT_SENTINEL'):
            self.assertNotIn(leak, blob, f'raw brainstorm leaked {leak!r}')

    def test_raw_brainstorm_has_all_eight_sections(self):
        phase = _phase('p6', title='Phase')
        raw = author.render_raw_brainstorm(phase, _project('p', [phase]), {})
        self.assertEqual(set(raw), set(author.BRAINSTORM_SECTION_KEYS))
        for v in raw.values():
            self.assertIsInstance(v, str)
            self.assertTrue(v.strip())

    def test_thin_context_uses_desired_end_state_for_lead(self):
        # A phase with a DES but no other context still gets a usable §0 lead.
        phase = _phase('p6', title='Phase', desired_end_state='the board reflects reality')
        raw = author.render_raw_brainstorm(phase, _project('p', [phase]), {})
        self.assertEqual(raw['end_state'], 'the board reflects reality')

    def test_raw_decisions_capped_and_clean(self):
        phase = _phase('p6', title='Phase')
        decisions = author.render_raw_decisions(phase, _project('p', [phase]), {})
        self.assertLessEqual(len(decisions), author.MAX_DECISIONS)
        self.assertTrue(all(isinstance(d, str) and d.strip() for d in decisions))


# --------------------------------------------------------------------------- #
# decisions cap (<=5) — the spec § 4 guardrail
# --------------------------------------------------------------------------- #
class CapDecisionsTests(unittest.TestCase):
    def test_caps_at_max_and_flags_overflow(self):
        capped, overflow = author._cap_decisions(
            [f'decision {i}' for i in range(8)])
        self.assertEqual(len(capped), author.MAX_DECISIONS)
        self.assertTrue(overflow)

    def test_under_cap_no_overflow(self):
        capped, overflow = author._cap_decisions(['a', 'b', 'c'])
        self.assertEqual(capped, ['a', 'b', 'c'])
        self.assertFalse(overflow)

    def test_dedupes_order_preserving(self):
        capped, overflow = author._cap_decisions(['a', 'b', 'a', '  ', 'c', 'b'])
        self.assertEqual(capped, ['a', 'b', 'c'])
        self.assertFalse(overflow)


# --------------------------------------------------------------------------- #
# LLM parse — all-or-nothing draft, independent decisions
# --------------------------------------------------------------------------- #
class ParseLLMTests(unittest.TestCase):
    def _full_reply(self):
        reply = {k: f'value for {k}' for k in author.BRAINSTORM_SECTION_KEYS}
        reply['decisions'] = ['scope?', 'risk tolerance?']
        return reply

    def test_full_reply_parses_draft_and_decisions(self):
        draft, decisions = author._parse_llm_brainstorm(self._full_reply())
        self.assertEqual(set(draft), set(author.BRAINSTORM_SECTION_KEYS))
        self.assertEqual(decisions, ['scope?', 'risk tolerance?'])

    def test_missing_one_section_drops_whole_draft(self):
        reply = self._full_reply()
        del reply['done_gate']
        draft, decisions = author._parse_llm_brainstorm(reply)
        self.assertIsNone(draft)            # all-or-nothing on the 8 keys
        self.assertEqual(decisions, ['scope?', 'risk tolerance?'])  # independent

    def test_garbage_decisions_falls_back_to_none(self):
        reply = self._full_reply()
        reply['decisions'] = 'not a list'
        draft, decisions = author._parse_llm_brainstorm(reply)
        self.assertIsNotNone(draft)
        self.assertIsNone(decisions)

    def test_non_dict_reply_is_none_none(self):
        self.assertEqual((None, None), author._parse_llm_brainstorm(None))
        self.assertEqual((None, None), author._parse_llm_brainstorm('nope'))


# --------------------------------------------------------------------------- #
# prior-closeout feed-forward (P4 pre-load)
# --------------------------------------------------------------------------- #
class PriorCloseoutTests(unittest.TestCase):
    def test_picks_latest_earlier_phase_with_closeout(self):
        p0 = _phase('p0', state='done', order=0, closeout={'summary': 'first'})
        p1 = _phase('p1', state='done', order=1, closeout={'summary': 'second'})
        p2 = _phase('p2', state='brainstorm', order=2)
        project = _project('proj', [p0, p1, p2])
        co = author.prior_closeout_for_phase(project, p2)
        self.assertEqual(co, {'summary': 'second'})

    def test_first_phase_has_no_prior_closeout(self):
        p0 = _phase('p0', state='brainstorm', order=0)
        project = _project('proj', [p0])
        self.assertIsNone(author.prior_closeout_for_phase(project, p0))

    def test_no_earlier_closeout_returns_none(self):
        p0 = _phase('p0', state='done', order=0)  # done but no closeout
        p1 = _phase('p1', state='brainstorm', order=1)
        project = _project('proj', [p0, p1])
        self.assertIsNone(author.prior_closeout_for_phase(project, p1))


# --------------------------------------------------------------------------- #
# context gather — fail-safe North Star read
# --------------------------------------------------------------------------- #
class GatherContextTests(unittest.TestCase):
    def test_gather_reads_north_star_excerpt(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / 'north-star.md').write_text('NORTH STAR BODY', encoding='utf-8')
            phase = _phase('p6', title='Phase')
            project = _project('p', [phase], north_star_ref='north-star.md')
            ctx = author.gather_brainstorm_context(phase, project, repo=repo)
        self.assertEqual(ctx['north_star_excerpt'], 'NORTH STAR BODY')
        self.assertEqual(ctx['phase_title'], 'Phase')
        self.assertEqual(ctx['phase_desired_end_state'], 'the thing is done and live')

    def test_gather_missing_north_star_is_none(self):
        phase = _phase('p6')
        project = _project('p', [phase], north_star_ref='does-not-exist.md')
        ctx = author.gather_brainstorm_context(phase, project, repo=Path('/nonexistent'))
        self.assertIsNone(ctx['north_star_excerpt'])

    def test_gather_no_ref_is_none(self):
        phase = _phase('p6')
        ctx = author.gather_brainstorm_context(phase, _project('p', [phase]))
        self.assertIsNone(ctx['north_star_excerpt'])


# --------------------------------------------------------------------------- #
# handoff payload — deterministic, self-contained, no model call
# --------------------------------------------------------------------------- #
class HandoffPayloadTests(unittest.TestCase):
    def test_payload_shape_and_spec_target(self):
        phase = _phase('p6-card-ui', title='Card UI')
        project = _project('proj', [phase], north_star_ref='docs/ns.md', repo='ourliberty-dashboard')
        payload = author.build_handoff_payload(phase, project, {})
        self.assertEqual(
            set(payload),
            {'spec_target_path', 'target_repo', 'north_star_ref',
             'prior_closeout_summary', 'directive'})
        self.assertEqual(payload['spec_target_path'], 'agents/beacon/specs/p6-card-ui.md')
        self.assertEqual(payload['target_repo'], 'ourliberty-dashboard')
        self.assertEqual(payload['north_star_ref'], 'docs/ns.md')
        self.assertIn('p6-card-ui.md', payload['directive'])
        self.assertIn('ourliberty-dashboard', payload['directive'])

    def test_payload_default_repo_when_absent(self):
        phase = _phase('p6')
        payload = author.build_handoff_payload(phase, _project('p', [phase]), {})
        self.assertEqual(payload['target_repo'], author._DEFAULT_REPO)

    def test_payload_carries_prior_closeout_summary(self):
        phase = _phase('p6')
        ctx = {'prior_closeout': {'summary': 'shipped the prior phase'}}
        payload = author.build_handoff_payload(phase, _project('p', [phase]), ctx)
        self.assertEqual(payload['prior_closeout_summary'], 'shipped the prior phase')


# --------------------------------------------------------------------------- #
# pure store helper + card passthrough
# --------------------------------------------------------------------------- #
class StoreAttachTests(unittest.TestCase):
    def _fields(self):
        return {
            'brainstorm': {
                'is_ai_draft': True, 'draft': {'end_state': 'x'},
                'decisions': ['a'], 'decisions_overflow': False, 'handoff': {},
            },
            'brainstorm_provenance': {'by': 'beacon', 'model': 'raw'},
        }

    def test_attach_then_idempotent(self):
        ph = _phase('p6')
        self.assertTrue(ps.attach_phase_brainstorm(ph, self._fields()))
        self.assertIs(ph['brainstorm']['is_ai_draft'], True)
        self.assertEqual(ph['brainstorm_provenance']['by'], 'beacon')
        # Re-attaching the identical payload is a no-op (no spurious delta).
        self.assertFalse(ps.attach_phase_brainstorm(ph, self._fields()))

    def test_attach_safe_on_junk(self):
        self.assertFalse(ps.attach_phase_brainstorm('nope', self._fields()))
        self.assertFalse(ps.attach_phase_brainstorm({}, {'brainstorm': 'not-a-dict'}))
        self.assertFalse(ps.attach_phase_brainstorm({}, {}))

    def test_phase_card_projects_brainstorm_to_flat_card_shape(self):
        # The card consumes a FLAT shape (draft string + decisions objects +
        # spec_target_path), not the stored nested brainstorm dict — _phase_card
        # projects the stored fields into it (the #611↔#72 contract seam).
        bs = {
            'is_ai_draft': True,
            'draft': {'end_state': 'x', 'why_now': 'now'},
            'decisions': ['Pick a scope.'],
            'handoff': {'spec_target_path': 'agents/beacon/specs/p6.md'},
        }
        carded = ps._phase_card(_phase('p6', brainstorm=bs))
        self.assertEqual(carded['draft'], 'Desired end state\nx\n\nWhy now\nnow')
        self.assertEqual(carded['decisions'],
                         [{'id': 'dec-0', 'title': 'Pick a scope.', 'decision': ''}])
        self.assertEqual(carded['spec_target_path'], 'agents/beacon/specs/p6.md')
        self.assertNotIn('brainstorm', carded)  # flat is the card contract
        # Absent when the phase has no brainstorm draft.
        self.assertNotIn('draft', ps._phase_card(_phase('p6')))
        self.assertNotIn('decisions', ps._phase_card(_phase('p6')))


# --------------------------------------------------------------------------- #
# the in-registry sweep — mutates in place, bounded, idempotent
# --------------------------------------------------------------------------- #
class SweepTests(unittest.TestCase):
    def test_sweep_drafts_brainstorm_phases_in_place(self):
        reg = _registry(
            _project('p1', [_phase('a', state='brainstorm')]),
            _project('p2', [_phase('b', state='spec'),
                            _phase('c', state='brainstorm')]))
        authored, deferred = author.author_brainstorms_in_registry(
            reg, use_llm=False, repo=None)
        self.assertEqual(authored, 2)
        self.assertEqual(deferred, 0)
        # The brainstorm-state phases got a draft; the spec phase did not.
        a = reg['projects'][0]['phases'][0]
        b = reg['projects'][1]['phases'][0]
        c = reg['projects'][1]['phases'][1]
        self.assertIn('brainstorm', a)
        self.assertNotIn('brainstorm', b)
        self.assertIn('brainstorm', c)

    def test_sweep_is_idempotent(self):
        reg = _registry(_project('p1', [_phase('a', state='brainstorm')]))
        author.author_brainstorms_in_registry(reg, use_llm=False, repo=None)
        # Second sweep authors nothing — needs_brainstorm now returns False.
        authored, deferred = author.author_brainstorms_in_registry(
            reg, use_llm=False, repo=None)
        self.assertEqual(authored, 0)
        self.assertEqual(deferred, 0)

    def test_sweep_bounded_by_max_per_tick(self):
        reg = _registry(_project('p1', [
            _phase(f'ph{i}', state='brainstorm', order=i) for i in range(5)]))
        authored, deferred = author.author_brainstorms_in_registry(
            reg, use_llm=False, repo=None, max_per_tick=2)
        self.assertEqual(authored, 2)
        self.assertEqual(deferred, 3)

    def test_sweep_safe_on_empty_registry(self):
        self.assertEqual((0, 0), author.author_brainstorms_in_registry(
            {}, use_llm=False, repo=None))
        self.assertEqual((0, 0), author.author_brainstorms_in_registry(
            {'projects': 'nope'}, use_llm=False, repo=None))


if __name__ == '__main__':
    unittest.main()
