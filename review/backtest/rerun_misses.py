#!/usr/bin/env python3
"""Re-test the HIGH-backtest misses with the REAL gate config (vs the tool-free floor):
  (1) inject the matching corpus class's detection_signature into the review prompt
  (2) run the reviewer inside a checkout of the BUGGY code state (fix_commit~1) so it
      can follow cross-file seams (what miss #10 needs).

For each target: worktree at <fix>~1 == the state the reverse-diff produces, present the
reverse-diff as the PR under review, let the reviewer Read/Grep the worktree, then judge.
"""
import json, subprocess, re, pathlib, tempfile, shutil, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
LENSES = (ROOT / 'review' / 'mirror-bughunt-lenses.md').read_text()
CORPUS = json.load(open(ROOT / 'review/known-bug-patterns.json'))['classes']
MAP = {m['id']: m for m in json.load(open(ROOT / 'review/backtest/mapping.json'))['mapped']}
TARGETS = [int(x) for x in (sys.argv[1].split(',') if len(sys.argv) > 1 else ['2', '10', '17', '18'])]

def git(*a, cwd=ROOT):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True).stdout

def sig_for(fid):
    for c in CORPUS:
        if fid in c['example_finding_ids']:
            return c['class_id'], c['detection_signature']
    return None, None

def claude(prompt, cwd):
    # read-only context tools allowed; runs inside the buggy worktree
    p = subprocess.run(
        ['claude', '-p', '--allowedTools', 'Read', 'Grep', 'Glob', 'Bash(grep:*)', 'Bash(sed:*)'],
        input=prompt, capture_output=True, text=True, timeout=900, cwd=cwd)
    return p.stdout.strip()

def jget(text, kind):
    # balanced, string-aware scan for the first [...] / {...} (not a greedy regex)
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

REVIEW = """You are a code-review gate sub-agent. Review the PR diff below for correctness, \
reliability, data-loss, and security bugs, using these lenses:

{lenses}

KNOWN BUG PATTERN to weight heavily this review (class `{cid}`):
{sig}

The full repository (at this PR's resulting state) is your working directory — Read/grep \
any file to check call sites and cross-file seams before deciding. PR diff under review:
```diff
{diff}
```
Return ONLY a JSON array: {{"file","line_hint","lens","severity","description"}}. [] if none."""

JUDGE = """Did the reviewer CATCH the known bug? Catch = same defect (same file, \
overlapping location, same root cause), even if worded differently.

KNOWN BUG (finding #{id}, {sev} {cat}, {file}:{line}): {title}
REVIEWER FINDINGS: {rev}
Return ONLY JSON: {{"caught": true|false, "reason": str}}."""

results = []
for fid in TARGETS:
    if fid not in MAP:
        print(f"#{fid}: not in mapping.json (unmapped finding) — skipping"); continue
    m = MAP[fid]
    cid, sig = sig_for(fid)
    if sig is None:
        print(f"#{fid}: no corpus class covers it — skipping (corpus-injection test is meaningless without a signature)"); continue
    wt = tempfile.mkdtemp(prefix=f'bt-{fid}-')
    git('worktree', 'add', '--detach', wt, m['fix_commit'] + '~1')
    try:
        diff = git('show', '-R', '--pretty=format:', m['fix_commit'], '--', m['file'])
        rprompt = REVIEW.format(lenses=LENSES, cid=cid, sig=sig, diff=diff[:24000])
        rfindings = jget(claude(rprompt, wt), 'array') or []
        jprompt = JUDGE.format(id=fid, sev=m['severity'], cat=m['category'], file=m['file'],
                               line=m['line'], title=m['title'], rev=json.dumps(rfindings)[:6000])
        verdict = jget(claude(jprompt, ROOT), 'object') or {'caught': None}
        results.append({'id': fid, 'class': cid, 'caught': verdict.get('caught'),
                        'reviewer_n': len(rfindings), 'reason': verdict.get('reason', '')})
        print(f"#{fid} [{cid}] caught={verdict.get('caught')} (reviewer_n={len(rfindings)})", flush=True)
    finally:
        git('worktree', 'remove', '--force', wt)
        shutil.rmtree(wt, ignore_errors=True)

pathlib.Path(ROOT / 'review/backtest/results_misses_retest.json').write_text(json.dumps(results, indent=2))
flipped = sum(1 for r in results if r['caught'])
print(f"\n=== {flipped}/{len(results)} previously-missed findings now CAUGHT with corpus+context ===")
