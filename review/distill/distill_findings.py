#!/usr/bin/env python3
"""Phase-2 distillation core: classify audit findings against the corpus, cluster the
novel ones into generalized bug CLASSES, and guard against accreting duplicate classes.

This is the *pure* half of the Phase-2 self-tuning loop (the heavy orchestration —
backtests, artifact write, DM — lives in run_distill.py). The two LLM steps (classify,
cluster) are injected as callables so this module is unit-testable with deterministic
stubs (see review/distill/test_distill_loop.py) and so the API-burning `claude -p`
backend is isolated.

Design invariants (the agreed guardrails):
  (a) GENERALIZE — `cluster_novels` proposes reusable CLASSES (corpus schema), never
      logs a finding verbatim.
  (b) ONE corpus — this module only READS review/known-bug-patterns.json; it never
      writes it. The approve-handler (Beacon) is the sole writer.
  The classifier reads CORPUS CLASSES ONLY (not the lens prose in
  mirror-bughunt-lenses.md) so the LOCO acceptance proof can't be leaked by the lenses
  reminding the model what a removed class looked like.
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORPUS_PATH = ROOT / 'review' / 'known-bug-patterns.json'
# The 8 lenses the gate fans out (mirror-bughunt-lenses.md A–H). A proposed class whose
# review_lens is outside this set is flagged as "needs a new lens" — a louder, separate
# decision for Larry, not a silent corpus append.
KNOWN_LENSES = {
    'concurrency-atomicity', 'input-path-safety', 'identifier-matching',
    'integration-seam', 'automation-honesty', 'state-persistence',
    'control-flow-correctness', 'claude-md-adherence',
}
_WORD = re.compile(r'[a-z0-9]+')


def load_corpus(path=CORPUS_PATH):
    return json.loads(pathlib.Path(path).read_text())


def class_summaries(corpus):
    """The matcher's view of the corpus: identity + the human/signature text, NOT the
    full entry. Keeps the classify prompt small and lens-free."""
    return [
        {
            'class_id': c['class_id'],
            'name': c['name'],
            'description': c['description'],
            'detection_signature': c['detection_signature'],
            'review_lens': c.get('review_lens'),
        }
        for c in corpus.get('classes', [])
    ]


def _tokens(text):
    # lowercase word-token bag, minus very common filler that would inflate overlap
    # between any two security/automation descriptions.
    stop = {
        'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'is', 'it', 'that', 'this',
        'with', 'without', 'on', 'for', 'as', 'by', 'at', 'be', 'are', 'not', 'no',
        'so', 'then', 'into', 'from', 'its', 'one', 'two', 'more', 'than', 'never',
    }
    return {t for t in _WORD.findall(text.lower()) if t not in stop and len(t) > 2}


def overlap_score(a_text, b_text):
    """Jaccard token overlap in [0,1]. Same shape as map_fixes.py's selector — a cheap,
    deterministic near-duplicate signal for the dedup-guard."""
    ta, tb = _tokens(a_text), _tokens(b_text)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def dedup_guard(proposed, existing_summaries, threshold=0.5):
    """Is `proposed` (a new-class dict) a near-duplicate of an existing class?

    Returns {is_dup, dup_of, score}. A proposed class that duplicates an existing one
    is merged into it (its findings become a recurrence update) rather than appended —
    otherwise the corpus accretes synonym classes and the gate fans out redundant
    lenses. Also the backstop that keeps recurrence-vs-novel honest: a too-strict
    classifier makes everything look novel; the dedup-guard catches the synonyms it
    lets through. Compared within the SAME review_lens only (cross-lens text overlap is
    not duplication)."""
    p_text = f"{proposed.get('name', '')} {proposed.get('description', '')} {proposed.get('detection_signature', '')}"
    best, best_id = 0.0, None
    for s in existing_summaries:
        if s.get('review_lens') != proposed.get('review_lens'):
            continue
        sc = overlap_score(p_text, f"{s['name']} {s['description']} {s['detection_signature']}")
        if sc > best:
            best, best_id = sc, s['class_id']
    return {'is_dup': best >= threshold, 'dup_of': best_id if best >= threshold else None,
            'score': round(best, 3)}


def distill(findings, corpus, *, classify_fn, cluster_fn, dedup_threshold=0.5):
    """Classify every finding (recurrence vs novel), cluster the novels, dedup-merge,
    and compute convergence. LLM work is delegated to:

      classify_fn(finding, summaries) -> {class_id|None, confidence, why}
      cluster_fn(novel_findings, summaries) -> [ {corpus-schema new class}, ... ]

    Returns a dict ready to fold into the proposal artifact:
      {recurrence_updates[], new_classes[], dedup_merges[], lens_gaps[], convergence{}}
    """
    summaries = class_summaries(corpus)
    known_ids = {s['class_id'] for s in summaries}
    existing_examples = {c['class_id']: set(c.get('example_finding_ids', []))
                         for c in corpus.get('classes', [])}

    recurrence = {}   # class_id -> set(new finding ids)
    novel = []
    for f in findings:
        verdict = classify_fn(f, summaries) or {}
        cid = verdict.get('class_id')
        if cid and cid in known_ids:
            recurrence.setdefault(cid, set()).add(f['id'])
        else:
            novel.append(f)

    # cluster novels into candidate new classes (corpus schema). Tolerate a backend that
    # returns None (API error / a custom cluster_fn) the same way classify is guarded
    # above — treat it as "no clusters proposed" rather than crashing the distill run.
    raw_new = (cluster_fn(novel, summaries) or []) if novel else []

    new_classes, dedup_merges, lens_gaps = [], [], []
    absorbed_ids = set()
    for nc in raw_new:
        guard = dedup_guard(nc, summaries, threshold=dedup_threshold)
        if guard['is_dup']:
            # merge into the existing class: its findings become a recurrence update.
            tgt = guard['dup_of']
            ids = set(nc.get('example_finding_ids', []))
            recurrence.setdefault(tgt, set()).update(ids)
            absorbed_ids |= ids
            dedup_merges.append({'proposed_class_id': nc.get('class_id'),
                                 'merged_into': tgt, 'score': guard['score'],
                                 'finding_ids': sorted(ids)})
            continue
        if nc.get('review_lens') not in KNOWN_LENSES:
            lens_gaps.append({'class_id': nc.get('class_id'),
                              'proposed_lens': nc.get('review_lens')})
        new_classes.append(nc)

    # recurrence_updates: only ids NOT already recorded on the class.
    recurrence_updates = []
    for cid, ids in sorted(recurrence.items()):
        fresh = sorted(ids - existing_examples.get(cid, set()))
        if fresh:
            recurrence_updates.append({'class_id': cid, 'add_example_finding_ids': fresh,
                                       'signature_edit': None})

    # convergence: a novel finding is one that survived as a genuinely new class
    # (classified novel AND not absorbed by a dedup-merge into an existing class).
    novel_ids = {f['id'] for f in novel} - absorbed_ids
    total = len(findings)
    novel_count = len(novel_ids)
    convergence = {
        'novel_count': novel_count,
        'recurrence_count': total - novel_count,
        'novel_share': round(novel_count / total, 3) if total else 0.0,
        'classes_touched': sorted(recurrence.keys()),
        'new_class_ids': [c.get('class_id') for c in new_classes],
    }
    return {
        'recurrence_updates': recurrence_updates,
        'new_classes': new_classes,
        'dedup_merges': dedup_merges,
        'lens_gaps': lens_gaps,
        'convergence': convergence,
    }


# --- live `claude -p` backends (isolated; tests inject stubs instead) ---

CLASSIFY_PROMPT = """You triage a confirmed code-audit finding against a corpus of \
known bug CLASSES. Decide whether this finding is an INSTANCE of an existing class, or \
NOVEL (no class fits its root-cause shape).

EXISTING CLASSES (match on root-cause/mechanism, not on file or wording):
{classes}

FINDING #{id} ({severity} {category}, {file}:{line}):
{title}
{body}

Return ONLY JSON: {{"class_id": "<existing class_id or null>", "confidence": 0-100, \
"why": "<one sentence>"}}. Use null when no class captures the SAME defect mechanism — \
do not force-fit a loosely-related class."""

CLUSTER_PROMPT = """These audit findings did NOT match any existing bug class. Cluster \
them into a SMALL number of GENERALIZED, reusable bug classes (not one-per-finding — \
generalize the shared mechanism). Each class must be reusable to catch FUTURE unseen \
bugs of the same shape.

EXISTING CLASSES (do not duplicate these; your new classes must be genuinely distinct):
{classes}

NOVEL FINDINGS:
{findings}

Return ONLY a JSON array; each element is a corpus class entry:
{{"class_id": "<kebab-case-slug>", "name": "<short>", "description": "<the general \
mechanism>", "detection_signature": "<concrete code/AST tells a reviewer greps for>", \
"review_lens": "<one of: {lenses}>", "severity_default": "HIGH|MEDIUM|LOW", \
"example_finding_ids": [<the ids in this cluster>], "blocking": true|false}}."""


def _claude_backend():
    """Return (classify_fn, cluster_fn) backed by local `claude -p`, reusing the
    backtest harness's call_claude + extract_json (local auth, NOT the droplet)."""
    import sys
    sys.path.insert(0, str(ROOT / 'review' / 'backtest'))
    import run_backtest as bt  # noqa: E402  (call_claude, extract_json live here)

    def classify_fn(finding, summaries):
        prompt = CLASSIFY_PROMPT.format(
            classes=json.dumps(summaries, indent=1)[:12000],
            id=finding['id'], severity=finding['severity'], category=finding['category'],
            file=finding['file'], line=finding['line'],
            title=finding['title'], body=finding.get('body', '')[:4000])
        return bt.extract_json(bt.call_claude(prompt), 'object') or {'class_id': None}

    def cluster_fn(novel, summaries):
        compact = [{'id': f['id'], 'severity': f['severity'], 'category': f['category'],
                    'file': f['file'], 'title': f['title'], 'body': f.get('body', '')[:1500]}
                   for f in novel]
        prompt = CLUSTER_PROMPT.format(
            classes=json.dumps([{k: s[k] for k in ('class_id', 'name', 'review_lens')}
                                for s in summaries], indent=1)[:8000],
            findings=json.dumps(compact, indent=1)[:18000],
            lenses=', '.join(sorted(KNOWN_LENSES)))
        return bt.extract_json(bt.call_claude(prompt), 'array') or []

    return classify_fn, cluster_fn
