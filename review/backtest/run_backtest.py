#!/usr/bin/env python3
"""Backtest the Mirror bug-hunt gate against the 64-finding ground-truth set.

Method (faithful "would the gate catch this in a PR diff?"):
  For each finding, reconstruct the buggy code as a reviewable PR diff by REVERSING
  the remediation commit that fixed it (`git show -R <fix_commit> -- <file>`). Hand
  that diff to the bug-hunt reviewer (the lenses in review/mirror-bughunt-lenses.md),
  then judge whether the reviewer flagged the KNOWN bug (match on file + line
  proximity + category/description).

Catch-rate is reported by severity and category. This is the acceptance test for the
gate AND the mini-CI for every future corpus addition.

Reviewer backend is pluggable via --backend:
  - 'claude': shells `claude -p` locally (NOT the rate-limited droplet auth). Cost!
  - 'stub'  : prints the prompts it WOULD send; no API. Use to dry-run the harness.

Scope flags: --pilot N (first N, severity-sorted), --severity HIGH, --ids 1,11,9.
"""
import json, subprocess, argparse, pathlib, re, sys, tempfile, shutil

ROOT = pathlib.Path(__file__).resolve().parents[2]
LENSES = (ROOT / 'review' / 'mirror-bughunt-lenses.md').read_text()
SEV_ORDER = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
MAP_PATH = ROOT / 'review/backtest/mapping.json'

def git(*a):
    return subprocess.run(['git', *a], cwd=ROOT, capture_output=True, text=True).stdout

def buggy_diff(fix_commit, file):
    # reverse the fix => the change that (re)introduces the bug, scoped to the file
    return git('show', '-R', '--pretty=format:', fix_commit, '--', file)

REVIEW_PROMPT = """You are a code-review gate sub-agent. Review the following PR diff \
for correctness, reliability, data-loss, and security bugs, using these lenses:

{lenses}

PR DIFF (this is the change under review):
```diff
{diff}
```

You MAY assume the rest of the repo exists; reason about call sites and surrounding \
context where a lens calls for it. Return ONLY a JSON array of findings, each: \
{{"file": str, "line_hint": str, "lens": str, "severity": "HIGH|MEDIUM|LOW", \
"description": str}}. If you find no real bugs, return []."""

JUDGE_PROMPT = """A code-review gate was asked to find bugs in a diff. Here is the \
KNOWN planted bug (ground truth) and the reviewer's reported findings. Did the \
reviewer CATCH the known bug? Catch = a finding that identifies the same defect \
(same file, overlapping/adjacent location, same root cause), even if worded \
differently. Near-misses that name a different bug do NOT count.

KNOWN BUG (finding #{id}, {severity} {category}, {file}:{line}):
{title}

REVIEWER FINDINGS (JSON):
{reviewer}

Return ONLY JSON: {{"caught": true|false, "matched_index": int|null, "reason": str}}."""

def call_claude(prompt):
    # tool-free: the diff is inline in the prompt, so no file access / bypass needed.
    # This is a LOWER BOUND on the real gate (which also reads surrounding context).
    # Prompt via stdin (--disallowedTools is variadic and would eat a positional prompt).
    p = subprocess.run(['claude', '-p', '--disallowedTools', 'Bash', 'Edit', 'Write', 'Read'],
                       input=prompt, capture_output=True, text=True, timeout=600)
    return p.stdout.strip()

def extract_json(text, kind='array'):
    # robust: find the FIRST balanced [...] / {...} (string-aware), not a greedy
    # \[.*\] that spans stray brackets in surrounding prose and fails to parse.
    o, c = ('[', ']') if kind == 'array' else ('{', '}')
    start = text.find(o)
    if start < 0: return None
    depth = 0; in_str = False; esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc: esc = False
            elif ch == '\\': esc = True
            elif ch == '"': in_str = False
        elif ch == '"': in_str = True
        elif ch == o: depth += 1
        elif ch == c:
            depth -= 1
            if depth == 0:
                try: return json.loads(text[start:i+1])
                except Exception: return None
    return None

# --- Phase-2 reuse: signature-injected, context-reading backtest of a SINGLE finding ---
# This is the "real config" backtest (corpus signature injected + context-reading in the
# buggy worktree) that rerun_misses.py pioneered, packaged as an import-safe function so
# review/distill/run_distill.py can mini-CI a proposed/sharpened class (guardrail c).

REVIEW_PROMPT_SIG = """You are a code-review gate sub-agent. Review the PR diff below for \
correctness, reliability, data-loss, and security bugs, using these lenses:

{lenses}

KNOWN BUG PATTERN to weight heavily this review (class `{cid}`):
{sig}

{context_note} PR diff under review:
```diff
{diff}
```
Return ONLY a JSON array, each: {{"file","line_hint","lens","severity","description"}}. \
[] if none."""

CTX_NOTE_WORKTREE = ("The full repository (at this PR's resulting state) is your working "
                     "directory — Read/grep any file to check call sites and cross-file "
                     "seams before deciding.")
CTX_NOTE_TOOLFREE = ("You MAY assume the rest of the repo exists; reason about call sites "
                     "from the diff alone (no file access).")


def _call_claude_ctx(prompt, cwd):
    # context-reading variant: read-only tools allowed, runs inside the buggy worktree.
    p = subprocess.run(
        ['claude', '-p', '--allowedTools', 'Read', 'Grep', 'Glob', 'Bash(grep:*)', 'Bash(sed:*)'],
        input=prompt, capture_output=True, text=True, timeout=900, cwd=cwd)
    return p.stdout.strip()


_MAP_CACHE = None


def _load_map():
    # mapping.json is static ground truth; backtest_class is called once per finding in a
    # loop, so memoize rather than re-parse it on every call.
    global _MAP_CACHE
    if _MAP_CACHE is None:
        _MAP_CACHE = {m['id']: m for m in json.loads(MAP_PATH.read_text())['mapped']}
    return _MAP_CACHE


def backtest_class(finding, detection_signature, class_id, *,
                   synthetic_diff=None, read_context=True):
    """Would the gate, armed with `detection_signature` for `class_id`, catch this
    finding? Returns {'caught': bool|None, 'reviewer_n': int, 'reason': str}.

    Diff source:
      - finding id in mapping.json (and no synthetic_diff) -> reverse its fix commit in a
        worktree at fix~1 (the buggy state), reviewer reads context there. Deterministic;
        used by the LOCO acceptance proof.
      - else -> requires `synthetic_diff` (the finding's own cited buggy hunk at HEAD),
        reviewed tool-free. The path for genuinely NOVEL audit findings with no fix yet.
    """
    fid = finding['id']
    mp = _load_map()
    use_worktree = synthetic_diff is None and fid in mp and read_context
    wt = None
    try:
        if synthetic_diff is not None:
            diff, cwd, note, caller = synthetic_diff, ROOT, CTX_NOTE_TOOLFREE, call_claude
        elif fid in mp:
            m = mp[fid]
            diff = buggy_diff(m['fix_commit'], m['file'])
            if not diff.strip():
                return {'caught': None, 'reviewer_n': 0, 'reason': 'empty reverse diff'}
            if use_worktree:
                wt = tempfile.mkdtemp(prefix=f'distill-bt-{fid}-')
                git('worktree', 'add', '--detach', wt, m['fix_commit'] + '~1')
                cwd, note, caller = wt, CTX_NOTE_WORKTREE, _call_claude_ctx
            else:
                cwd, note, caller = ROOT, CTX_NOTE_TOOLFREE, call_claude
        else:
            return {'caught': None, 'reviewer_n': 0,
                    'reason': 'no mapping.json row and no synthetic_diff — cannot reconstruct a buggy diff'}

        rprompt = REVIEW_PROMPT_SIG.format(lenses=LENSES, cid=class_id,
                                           sig=detection_signature, context_note=note,
                                           diff=diff[:24000])
        reviewer = caller(rprompt, cwd) if caller is _call_claude_ctx else caller(rprompt)
        rfindings = extract_json(reviewer, 'array') or []
        jprompt = JUDGE_PROMPT.format(
            id=fid, severity=finding.get('severity', '?'), category=finding.get('category', '?'),
            file=finding.get('file', '?'), line=finding.get('line', '?'),
            title=finding.get('title', ''), reviewer=json.dumps(rfindings)[:6000])
        verdict = extract_json(call_claude(jprompt), 'object') or {'caught': None}
        return {'caught': verdict.get('caught'), 'reviewer_n': len(rfindings),
                'reason': verdict.get('reason', '')}
    finally:
        if wt:
            git('worktree', 'remove', '--force', wt)
            shutil.rmtree(wt, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backend', choices=['claude', 'stub'], default='stub')
    ap.add_argument('--pilot', type=int, default=0, help='only first N (severity-sorted)')
    ap.add_argument('--severity', default=None)
    ap.add_argument('--ids', default=None, help='comma-separated finding ids')
    ap.add_argument('--out', default='review/backtest/results.json')
    ap.add_argument('--resume', action='store_true', help='skip findings already judged in --out')
    args = ap.parse_args()

    mapping = json.load(open(ROOT / 'review/backtest/mapping.json'))['mapped']
    mapping.sort(key=lambda m: SEV_ORDER.get(m['severity'], 9))
    if args.severity: mapping = [m for m in mapping if m['severity'] == args.severity]
    if args.ids:
        want = {int(x) for x in args.ids.split(',')}
        mapping = [m for m in mapping if m['id'] in want]
    if args.pilot: mapping = mapping[:args.pilot]

    outpath = pathlib.Path(ROOT / args.out)
    # resume: keep already-judged findings from a prior (killed) run
    scope_ids = {m['id'] for m in mapping}
    results, done = [], set()
    if args.resume and outpath.exists():
        # keep ONLY prior results that are in THIS run's scope, else a resume with a
        # different --severity/--ids would re-save stale entries and skew the summary.
        results = [r for r in json.loads(outpath.read_text()) if r['id'] in scope_ids]
        done = {r['id'] for r in results if r.get('caught') is not None}
        print(f"resuming: {len(done)} in-scope findings already judged, skipping them")

    for m in mapping:
        if m['id'] in done: continue
        diff = buggy_diff(m['fix_commit'], m['file'])
        if not diff.strip():
            results.append({**m, 'caught': None, 'note': 'empty reverse diff'}); continue
        rprompt = REVIEW_PROMPT.format(lenses=LENSES, diff=diff[:24000])
        if args.backend == 'stub':
            print(f"[stub] #{m['id']} {m['severity']} {m['file']} via {m['fix_commit']} "
                  f"({len(diff)} diff bytes) — would review + judge")
            results.append({**m, 'caught': None, 'note': 'stub'}); continue
        reviewer = call_claude(rprompt)
        rfindings = extract_json(reviewer, 'array') or []
        jprompt = JUDGE_PROMPT.format(id=m['id'], severity=m['severity'], category=m['category'],
                                      file=m['file'], line=m['line'], title=m['title'],
                                      reviewer=json.dumps(rfindings)[:6000])
        verdict = extract_json(call_claude(jprompt), 'object') or {'caught': None}
        results.append({**m, 'caught': verdict.get('caught'),
                        'reviewer_n': len(rfindings), 'judge_reason': verdict.get('reason', '')})
        c = verdict.get('caught')
        print(f"#{m['id']:>2} {m['severity']:<6} {m['file']:<40} caught={c}", flush=True)
        outpath.write_text(json.dumps(results, indent=2))  # save after EACH finding (kill-resilient)

    outpath.write_text(json.dumps(results, indent=2))
    judged = [r for r in results if r.get('caught') is not None]
    if judged and args.backend != 'stub':
        import collections
        by_sev = collections.Counter(r['severity'] for r in judged if r['caught'])
        tot_sev = collections.Counter(r['severity'] for r in judged)
        print("\n=== CATCH RATE ===")
        for s in ('HIGH', 'MEDIUM', 'LOW'):
            if tot_sev[s]: print(f"  {s}: {by_sev[s]}/{tot_sev[s]} ({100*by_sev[s]//tot_sev[s]}%)")
        caught = sum(1 for r in judged if r['caught'])
        print(f"  OVERALL: {caught}/{len(judged)} ({100*caught//len(judged)}%)")
    print(f"\nwrote {args.out} ({len(results)} findings, backend={args.backend})")

if __name__ == '__main__':
    main()
