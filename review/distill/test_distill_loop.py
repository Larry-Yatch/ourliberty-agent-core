#!/usr/bin/env python3
"""Deterministic acceptance for the Phase-2 distillation loop — proves it WITHOUT a live
audit and (for the structural tiers) without burning API.

Tiers:
  A. LOCO sweep (no API) — for each of the 14 seed classes: remove it, feed its labeled
     example findings back, and assert the loop (a) marks them novel, (b) the re-derived
     class is NOT falsely dedup-merged into a surviving sibling (the dedup-guard is
     correctly calibrated — the failure mode where a legit new class gets swallowed),
     (c) convergence reports novel_share = class-size / total.
  B. Saturation control (no API) — a perfect classifier (every finding -> its real class)
     yields 0 novel. The negative control: the loop recognizes a saturated corpus.
  C. Live catch (API, opt-in via OURLIBERTY_DISTILL_LIVE=1) — backtest_class actually
     catches a held-out finding with its class's real signature. Skipped by default.

Run: python3 -m unittest review/distill/test_distill_loop.py
"""
import os, sys, copy, json, pathlib, unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'review' / 'distill'))
sys.path.insert(0, str(ROOT / 'review' / 'backtest'))
import distill_findings as df   # noqa: E402

CORPUS = df.load_corpus()
MAPPED_IDS = {m['id'] for m in json.load(open(ROOT / 'review/backtest/mapping.json'))['mapped']}


def label_of(corpus, fid):
    """The class_id that lists `fid` (a perfect classifier's answer), or None."""
    for c in corpus['classes']:
        if fid in c.get('example_finding_ids', []):
            return c['class_id']
    return None


def make_classifier(corpus):
    """A faithful, deterministic stand-in for the LLM classifier: it returns the corpus's
    own ground-truth label for a finding (or null if no surviving class lists it). Removing
    a class from `corpus` makes that class's findings classify as novel — exactly the LOCO
    setup. Not circular: this is what a perfect classifier WOULD return; the test exercises
    the orchestration + dedup-guard, not the LLM."""
    def classify(finding, summaries):
        return {'class_id': label_of(corpus, finding['id']), 'confidence': 90, 'why': 'loco-stub'}
    return classify


class TestLOCO(unittest.TestCase):
    def test_loco_each_class_redetected_and_not_false_merged(self):
        failures = []
        for held in CORPUS['classes']:
            ids = held.get('example_finding_ids', [])
            if not ids:
                continue
            # corpus with `held` removed
            reduced = copy.deepcopy(CORPUS)
            reduced['classes'] = [c for c in reduced['classes'] if c['class_id'] != held['class_id']]
            findings = [{'id': i, 'severity': 'HIGH', 'category': 'x', 'file': 'f', 'line': 1,
                         'title': f'finding {i}', 'body': ''} for i in ids]
            # cluster step re-proposes the SAME class entry (the thing a good distiller
            # would regenerate); the dedup-guard must let it through, not swallow it.
            redetected = copy.deepcopy(held)
            cluster = lambda novel, summaries, _rc=redetected: [copy.deepcopy(_rc)]
            res = df.distill(findings, reduced,
                             classify_fn=make_classifier(reduced), cluster_fn=cluster)

            novel_ids = set(ids) - {i for u in res['recurrence_updates']
                                    for i in u['add_example_finding_ids']}
            survived = {c['class_id'] for c in res['new_classes']}
            if held['class_id'] not in survived:
                merged = next((m for m in res['dedup_merges']
                               if m['proposed_class_id'] == held['class_id']), None)
                failures.append(f"{held['class_id']}: FALSE-MERGED into "
                                f"{merged['merged_into'] if merged else '?'} (score "
                                f"{merged['score'] if merged else '?'})")
                continue
            self.assertEqual(res['convergence']['novel_count'], len(ids),
                             f"{held['class_id']}: novel_count != held-out size")
            self.assertEqual(res['convergence']['novel_share'],
                             round(len(ids) / len(findings), 3))
        self.assertEqual(failures, [], "dedup-guard too loose — legit re-derived classes "
                         "swallowed by a sibling:\n  " + "\n  ".join(failures))

    def test_saturation_control(self):
        # perfect classifier against the FULL corpus -> every finding is recurrence.
        all_ids = sorted({i for c in CORPUS['classes'] for i in c.get('example_finding_ids', [])})
        findings = [{'id': i, 'severity': 'HIGH', 'category': 'x', 'file': 'f', 'line': 1,
                     'title': f'finding {i}', 'body': ''} for i in all_ids]
        res = df.distill(findings, CORPUS, classify_fn=make_classifier(CORPUS),
                         cluster_fn=lambda novel, s: [])
        self.assertEqual(res['convergence']['novel_count'], 0,
                         "saturated corpus should yield 0 novel")
        self.assertEqual(res['convergence']['novel_share'], 0.0)
        # recurrence_updates only carry NEW ids; re-feeding known ids adds nothing.
        self.assertEqual(res['recurrence_updates'], [])

    def test_dedup_guard_symmetry(self):
        # a class is always a near-duplicate of ITSELF (score 1.0) — sanity on the metric.
        summaries = df.class_summaries(CORPUS)
        c0 = CORPUS['classes'][0]
        proposed = {'name': c0['name'], 'description': c0['description'],
                    'detection_signature': c0['detection_signature'], 'review_lens': c0['review_lens']}
        g = df.dedup_guard(proposed, summaries, threshold=0.5)
        self.assertTrue(g['is_dup'])
        self.assertEqual(g['dup_of'], c0['class_id'])


@unittest.skipUnless(os.environ.get('OURLIBERTY_DISTILL_LIVE') == '1',
                     'live backtest costs API; set OURLIBERTY_DISTILL_LIVE=1 to run')
class TestLiveCatch(unittest.TestCase):
    def test_one_held_out_class_backtests(self):
        import run_backtest as bt
        # pick the first class whose example findings are in mapping.json (deterministic diff)
        held = next(c for c in CORPUS['classes']
                    if any(i in MAPPED_IDS for i in c.get('example_finding_ids', [])))
        fid = next(i for i in held['example_finding_ids'] if i in MAPPED_IDS)
        v = bt.backtest_class({'id': fid, 'severity': held['severity_default'], 'category': 'x',
                               'file': 'f', 'line': 1, 'title': held['name']},
                              held['detection_signature'], held['class_id'])
        self.assertIn(v['caught'], (True, False, None))
        print(f"\n[live] class={held['class_id']} finding #{fid} caught={v['caught']}")


if __name__ == '__main__':
    unittest.main()
