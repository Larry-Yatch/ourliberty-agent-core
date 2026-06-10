#!/usr/bin/env python3
"""Phase-2 distiller (the HEAVY job — run on the DESKTOP, reuses local `claude -p`).

Pipeline for one full-codebase audit:
  1. read the audit's structured findings.json (extract_findings.py shape)
  2. classify each finding recurrence-vs-novel + cluster novels into new CLASSES
     (distill_findings.distill, LLM-backed; --stub for a deterministic dry run)
  3. mini-backtest every proposed corpus change (guardrail c):
       (c-2) novel-class proof-of-catch — each new class must catch its own finding ids
       (c-1) corpus-regression — any sharpened signature must not drop the existing
             ground-truth catch-rate below HIGH>=80% / MEDIUM>=60%
  4. write a proposal artifact (applied:false) for Larry's approve flow

It NEVER edits review/known-bug-patterns.json — the Beacon approve-handler is the sole
writer (guardrail b). Larry gates the corpus CHANGE; this script gates on backtest
RESULTS, not on individual detection signatures.

Usage:
  python3 review/distill/run_distill.py review/backtest/findings.json \
      --source-audit AUDIT_main_20260605.md            # real (LLM + backtests; costs!)
  python3 review/distill/run_distill.py <findings.json> --stub --no-backtest   # dry run
"""
import os, sys, json, argparse, pathlib, datetime, collections

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'review' / 'backtest'))
sys.path.insert(0, str(ROOT / 'review' / 'distill'))
import atomic_io                       # noqa: E402
import run_backtest as bt              # noqa: E402  (backtest_class)
import distill_findings as df          # noqa: E402

AGENTS_ROOT = pathlib.Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', os.path.expanduser('~/agents')))
PROPOSAL_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-distill-proposals'
# Acceptance bars (review/backtest/README.md): a corpus change must not regress these.
BARS = {'HIGH': 0.80, 'MEDIUM': 0.60}


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --- stub backends (deterministic; for --stub dry runs and unit tests) ---
def _stub_classify(finding, summaries):
    # naive: match if any existing class already lists this id (used only for dry runs).
    return {'class_id': None, 'confidence': 0, 'why': 'stub'}


def _stub_cluster(novel, summaries):
    if not novel:
        return []
    return [{'class_id': 'stub-novel-class', 'name': 'stub', 'description': 'stub',
             'detection_signature': 'stub', 'review_lens': 'control-flow-correctness',
             'severity_default': 'MEDIUM', 'example_finding_ids': [f['id'] for f in novel],
             'blocking': False}]


def backtest_new_class(new_class, findings_by_id, *, read_context=True):
    """(c-2) Does this proposed class catch its own example findings? Aggregate verdict."""
    results = []
    for fid in new_class.get('example_finding_ids', []):
        finding = findings_by_id.get(fid, {'id': fid})
        v = bt.backtest_class(finding, new_class['detection_signature'],
                              new_class['class_id'], read_context=read_context)
        results.append({'id': fid, **v})
    caught = sum(1 for r in results if r['caught'] is True)
    judged = [r for r in results if r['caught'] is not None]
    status = ('unbacktested' if not judged
              else 'caught' if caught == len(judged)
              else 'partial' if caught else 'missed')
    return {'status': status, 'caught': caught, 'judged': len(judged),
            'total': len(results), 'detail': results}


def corpus_regression(recurrence_updates, corpus, findings_by_id, *, read_context=True):
    """(c-1) Re-backtest the existing ground-truth findings of any class whose signature
    was SHARPENED, with the amended signature, and confirm the bar still holds. A pure
    append (no signature_edit) cannot regress existing catch-rate, so it's a no-op pass.
    """
    classes_by_id = {c['class_id']: c for c in corpus['classes']}
    edited = [u for u in recurrence_updates if u.get('signature_edit')]
    if not edited:
        return {'pass': True, 'note': 'append-only; no existing signature changed', 'by_severity': {}}
    by_sev_caught = collections.Counter()
    by_sev_total = collections.Counter()
    for u in edited:
        cls = classes_by_id[u['class_id']]
        sig = u['signature_edit']
        for fid in cls.get('example_finding_ids', []):
            finding = findings_by_id.get(fid, {'id': fid})
            v = bt.backtest_class(finding, sig, cls['class_id'], read_context=read_context)
            if v['caught'] is None:
                continue
            sev = finding.get('severity', '?')
            by_sev_total[sev] += 1
            if v['caught']:
                by_sev_caught[sev] += 1
    by_severity, ok = {}, True
    for sev, bar in BARS.items():
        tot = by_sev_total[sev]
        rate = (by_sev_caught[sev] / tot) if tot else 1.0
        by_severity[sev] = {'caught': by_sev_caught[sev], 'total': tot, 'rate': round(rate, 3)}
        if tot and rate < bar:
            ok = False
    return {'pass': ok, 'by_severity': by_severity,
            'note': 'sharpened signatures re-backtested against existing ground truth'}


def run(findings, corpus, *, source_audit, classify_fn, cluster_fn,
        do_backtest=True, read_context=True):
    result = df.distill(findings, corpus, classify_fn=classify_fn, cluster_fn=cluster_fn)
    findings_by_id = {f['id']: f for f in findings}

    if do_backtest:
        for nc in result['new_classes']:
            nc['backtest'] = backtest_new_class(nc, findings_by_id, read_context=read_context)
        reg = corpus_regression(result['recurrence_updates'], corpus, findings_by_id,
                                read_context=read_context)
    else:
        for nc in result['new_classes']:
            nc['backtest'] = {'status': 'skipped', 'note': '--no-backtest'}
        reg = {'pass': None, 'note': '--no-backtest'}

    return {
        'as_of': _now_iso(),
        'check': 'distill',
        'source_audit': source_audit,
        'audit_findings_total': len(findings),
        'convergence': result['convergence'],
        'recurrence_updates': result['recurrence_updates'],
        'new_classes': result['new_classes'],
        'dedup_merges': result['dedup_merges'],
        'lens_gaps': result['lens_gaps'],
        'corpus_regression_backtest': reg,
        'applied': False,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('findings', help='path to the audit findings.json (extract_findings shape)')
    ap.add_argument('--source-audit', default=None, help='audit doc name for provenance')
    ap.add_argument('--corpus', default=str(df.CORPUS_PATH))
    ap.add_argument('--stub', action='store_true', help='deterministic stub backends (no API)')
    ap.add_argument('--no-backtest', action='store_true', help='skip the backtests (structural dry run)')
    ap.add_argument('--no-context', action='store_true', help='tool-free backtest (no worktree read)')
    ap.add_argument('--out', default=None, help='artifact path (default: blackboard proposal dir)')
    args = ap.parse_args(argv)

    findings = json.loads(pathlib.Path(args.findings).read_text())
    corpus = df.load_corpus(args.corpus)
    source_audit = args.source_audit or pathlib.Path(args.findings).name
    classify_fn, cluster_fn = (_stub_classify, _stub_cluster) if args.stub else df._claude_backend()

    artifact = run(findings, corpus, source_audit=source_audit,
                   classify_fn=classify_fn, cluster_fn=cluster_fn,
                   do_backtest=not args.no_backtest, read_context=not args.no_context)

    date = (source_audit.replace('AUDIT_main_', '').replace('.md', '') or
            datetime.date.fromisoformat(artifact['as_of'][:10]).isoformat())
    out = pathlib.Path(args.out) if args.out else PROPOSAL_DIR / f'distill-{date}.json'
    atomic_io.atomic_write_json(out, artifact)

    c = artifact['convergence']
    print(f"[distill] {source_audit}: {artifact['audit_findings_total']} findings -> "
          f"novel {c['novel_count']} ({c['novel_share']:.0%}), recurrence {c['recurrence_count']}; "
          f"{len(artifact['new_classes'])} new classes, {len(artifact['dedup_merges'])} dedup-merged")
    print(f"[distill] corpus-regression pass={artifact['corpus_regression_backtest'].get('pass')}; "
          f"wrote {out}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
