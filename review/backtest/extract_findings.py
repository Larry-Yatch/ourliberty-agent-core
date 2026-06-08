#!/usr/bin/env python3
"""Parse AUDIT_main_20260605.md into structured findings (the labeled escaped-bug set)."""
import re, json, sys, pathlib
src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else 'AUDIT_main_20260605.md')
text = src.read_text()
HDR = re.compile(r'^## (\d+)\.\s*\[([A-Z]+)\s*·\s*(CONFIRMED|PLAUSIBLE)\s*·\s*([a-z-]+)\]\s*`?([^\s`]+):(\d+)`?', re.M)
hdrs = list(HDR.finditer(text))
findings = []
for i, m in enumerate(hdrs):
    start = m.end()
    end = hdrs[i+1].start() if i+1 < len(hdrs) else len(text)
    body = text[start:end].strip()
    # title = first non-empty line of body
    title = next((ln.strip() for ln in body.splitlines() if ln.strip()), '')
    findings.append({
        'id': int(m.group(1)),
        'severity': m.group(2),
        'confidence': m.group(3),
        'category': m.group(4),
        'file': m.group(5),
        'line': int(m.group(6)),
        'title': title[:300],
        'body': body,
    })
out = pathlib.Path('review/backtest/findings.json')
out.write_text(json.dumps(findings, indent=2))
print(f"extracted {len(findings)} findings -> {out}")
# loud sanity check: every `## N.` header must parse, else the ground-truth set is
# silently incomplete and the catch-rate denominator is wrong.
n_headers = len(re.findall(r'^## \d+\.', text, re.M))
if n_headers != len(findings):
    print(f"WARNING: {n_headers} '## N.' headers but only {len(findings)} parsed — "
          f"{n_headers - len(findings)} dropped by the HDR regex (format drift). Fix HDR before trusting results.")
from collections import Counter
print("severity:", dict(Counter(f['severity'] for f in findings)))
print("category:", dict(Counter(f['category'] for f in findings)))
print("files touched:", len(set(f['file'] for f in findings)))
