#!/usr/bin/env python3
"""Map each audit finding -> the remediation commit that fixed it, via git.
Ground-truth backtest uses `git show -R <fix_commit>` to reconstruct the buggy
code as a reviewable PR diff. Deterministic; no GitHub calls."""
import json, subprocess, re, pathlib, collections

def git(*a):
    return subprocess.run(['git', *a], cwd='.', capture_output=True, text=True).stdout

findings = json.load(open('review/backtest/findings.json'))

# remediation window: post-audit fix commits on origin/main
log = git('log', '--since=2026-06-05 00:00', '--pretty=%H\t%h\t%s', 'origin/main').splitlines()
remed = []
for ln in log:
    parts = ln.split('\t', 2)
    if len(parts) < 3: continue
    full, short, subj = parts
    if re.search(r'\b(fix|audit|PR-[A-Z]|SEC\d|finding|harden|seam|atomic)\b', subj, re.I):
        files = [x for x in git('show', '--name-only', '--pretty=format:', full).splitlines() if x.strip()]
        remed.append({'full': full, 'short': short, 'subj': subj, 'files': set(files)})

mapped, unmapped = [], []
for f in findings:
    cands = [c for c in remed if f['file'] in c['files']]
    # distinctive code tokens from the finding (backtick spans + identifier-shaped words)
    spans = re.findall(r'`([^`]+)`', f['body']) + [f['title'], f['body']]
    toks = set()
    for span in spans:
        for t in re.findall(r'[A-Za-z_][A-Za-z0-9_.]{3,}', span):
            if '_' in t or t.isupper() or '.' in t or len(t) >= 7:
                toks.add(t)
    toks -= {'scripts', 'audit', 'finding', 'should', 'because', 'return', 'function'}

    def score(c):
        # strongest: explicit "audit #N" citation of THIS finding
        if re.search(rf'audit\s*[#§]?\s*{f["id"]}\b', c['subj'], re.I):
            return 10_000, 0
        # principled: how many of the finding's distinctive tokens appear in the
        # reverse-diff of this candidate for this file (the fix that touched exactly
        # the code the finding describes). +small bonus for category-word in subject.
        rev = git('show', '-R', '--pretty=format:', c['full'], '--', f['file'])
        overlap = sum(1 for t in toks if t in rev)
        return overlap * 10 + (3 if f['category'] in c['subj'].lower() else 0), overlap

    if cands:
        ranked = sorted(cands, key=lambda c: score(c)[0], reverse=True)
        chosen = ranked[0]
        sc, ov = score(chosen)
        rev = git('show', '-R', '--pretty=format:', chosen['full'], '--', f['file'])
        mapped.append({**{k:f[k] for k in ('id','severity','confidence','category','file','line','title')},
                       'fix_commit': chosen['short'], 'fix_subj': chosen['subj'],
                       'buggy_diff_lines': rev.count('\n'), 'n_candidates': len(cands),
                       'map_score': sc, 'token_overlap': ov, 'n_tokens': len(toks)})
    else:
        unmapped.append({k:f[k] for k in ('id','severity','category','file','title')})

pathlib.Path('review/backtest/mapping.json').write_text(json.dumps({'mapped':mapped,'unmapped':unmapped}, indent=2))
print(f"remediation commits in window: {len(remed)}")
print(f"MAPPED: {len(mapped)}/{len(findings)}   UNMAPPED: {len(unmapped)}")
print("mapped by severity:", dict(collections.Counter(m['severity'] for m in mapped)))
print("unmapped by severity:", dict(collections.Counter(u['severity'] for u in unmapped)))
print("\n-- unmapped findings (no remediation commit touched the file) --")
for u in unmapped: print(f"  #{u['id']} [{u['severity']}·{u['category']}] {u['file']}")
