#!/usr/bin/env python3
"""brief_check.py — refuse a builder brief whose sentences are guesses.

WHY THIS EXISTS
---------------
Nine review nights running (RSDPM #288 → #309, 2026-09-30 → 2026-10-02) the
headline finding was a sentence in the MANAGER's brief, not the builder's code:
"Houston's mirror rule does NOT apply here" (#309 r1-F1, written over a filed
ruling never opened — irreversible notebook replace); "capped at 200 like
LIST_CAP" (#309 r1-F2, the 201st row never asked); "the Redate arm KEEPS the
stored reason and ignores the resent text" (#297 r1-F1, true of the UPDATE arm,
false of the pre-read rule — Change return date never worked in production);
the finalize payload `{"refusal": …}` written without reading the function's
SET clause (#301 r1-F1, every refused run lost its verdict counts); "tasks are
LEAVES" (#294 r1-F2, false since 0058). Each time the log recorded a
"mechanical rule" in prose — "a brief may say X ONLY beside a pasted grep" — and
the next night the class fired again. A rule the runner does not enforce is a
sentence. This file is the enforcement: `rsdpm_runner.py build` refuses a brief
this check fails, and there is no waiver flag. Fix the brief, never the check.

WHAT IT CHECKS — and what it cannot
-----------------------------------
Rules, each a row in the printed table (FAIL rows exit 2):

  predicate-table   A brief that names a migration file or speaks of refusals
                    (4+ times) carries a heading containing "predicate table"
                    followed by a markdown table with at least one data row.
                    (memory: a-brief-sentence-can-be-the-defect, #292.)
  refusal-rows      Every `RAISE EXCEPTION 'token…'` token in a migration the
                    brief names — when that file exists on --ref — appears in
                    the predicate-table section. A refusal with no door row is
                    a finding before the builder starts.
  two-clocks        A "STEP 2 of N" brief carries a heading containing "TWO
                    CLOCKS", and when the step-1 migration exists on --ref at
                    least one substantive line of that section is VERBATIM from
                    the migration's text (#294 r1-F1: the step-2 brief dropped
                    the window paragraph the step-1 header had written).
  claim-quote       A paragraph that says a rule "does not apply", or that
                    something is "by design" / "deliberately", carries a fenced
                    block (a pasted quote of that rule's own comment) in the
                    same section. A citation is not a quote: #309's paragraph
                    cited whitelist.ts:212-222 and the comment that mattered was
                    at :19-29. (#309 r1-F1.)
  claim-cite        A paragraph that asserts a FACT about code — "ignores",
                    "discards", "never throws/fires/reaches/reads/changes/runs/
                    refuses", "nothing references", "no reader", "no caller",
                    "is a leaf"/"are leaves", a regex, a payload, or a call of a
                    repo DB function with a literal `{` payload — carries a
                    `file.ext:NN` citation or a fenced block in that paragraph's
                    section. (#297, #301, #294 r1-F2.)
  claim-cap         A paragraph that names a cap ("capped at", "cap of",
                    LIST_CAP) says, in the same section, what the first EXCLUDED
                    item means for the human. (#309 r1-F2.)

Fenced blocks (``` … ```) are never scanned for claims: pasted output is
evidence, not assertion. Headings are not paragraphs.

It CANNOT check that a quote was read, that a citation points at the governing
lines, or that an instruction traces the operator's next step (#308 r1-F1 was
a conscious ruling, "merge, then edit", whose edit landed on a row with no
fields). It converts "did I look?" from a judgement into a check and stops
there; the external review remains the net for the rest. Measured on the 26
briefs written 2026-09-30 → 2026-10-02: it refuses 5 of the 12 brief-sentence
findings by construction (the table is in the PR body); the other 7 are
omissions, rulings and reader-question defects no text rule can see.

USAGE
-----
    brief_check.py BRIEF.md [--repo ~/dev/RSDPM] [--ref origin/main] [--decisions]

`--decisions` runs only the claim rules (a manager's per-finding decisions file
is code-shaped instruction too — #283 r2 F1/F10 traced to decision text).
Exit 0 passes, 2 refused, 1 usage error. Python 3.9, stdlib only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2

MIGRATION_RE = re.compile(r"supabase/migrations/(\d{4}_[A-Za-z0-9_]+\.sql)")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
CITATION_RE = re.compile(r"\b[\w./-]+\.(?:sql|ts|tsx|mts|mjs|js|py|sh|md|json|yml|yaml)\s*:~?\s*\d+")
RAISE_RE = re.compile(r"RAISE\s+EXCEPTION\s+'([a-z][a-z0-9_]*)", re.I)
FUNCTION_RE = re.compile(r"FUNCTION\s+(?:public\.)?([a-z_][a-z0-9_]*)\s*\(", re.I)
STEP2_RE = re.compile(r"\bstep\s*2\s+of\s+\d", re.I)

QUOTE_CLAIMS = {
    "does not apply": re.compile(r"\b(?:does\s+not|doesn['’]t|do\s+not|don['’]t|will\s+not|won['’]t)\s+apply\b|\bnot\s+apply\b", re.I),
    "by design": re.compile(r"\bby\s+design\b", re.I),
    "deliberately": re.compile(r"\bdeliberately\b", re.I),
}
CITE_CLAIMS = {
    "ignores/discards": re.compile(r"\bignor(?:es|ed|ing)\b|\bdiscard(?:s|ed|ing)?\b", re.I),
    "never X": re.compile(r"\bnever\s+(?:throws?|fires?|reach(?:es)?|reads?|changes?|runs?|refuses?)\b", re.I),
    "nothing references / no reader / no caller": re.compile(
        r"\bnothing\s+(?:references|reads)\b|\bno\s+reader\b|\bno\s+(?:other\s+|product\s+)?callers?\b", re.I),
    "leaf": re.compile(r"\bis\s+a\s+leaf\b|\bare\s+leaves\b", re.I),
    "regex": re.compile(r"\bregex(?:es)?\b|\[a-z_\]\+", re.I),
    "payload": re.compile(r"\bpayloads?\b", re.I),
}
CAP_CLAIM = re.compile(r"\bcapped\s+at\b|\bcap\s+of\b|\bLIST_CAP\b", re.I)
CAP_EXCLUDED = re.compile(
    r"\bexcluded\b|\b\d+(?:st|nd|rd|th)\s+(?:row|item|record|match)\b|\bN\s*\+\s*1\b|\b(?:beyond|past|over|outside|above)\s+the\s+cap\b"
    r"|\bthe\s+first\s+(?:row|item|record)\s+(?:not|left|that)\b|\bwhat\s+the\s+human\s+sees\s+(?:past|beyond|at)\b", re.I)


class Row:
    __slots__ = ("rule", "line", "status", "note")

    def __init__(self, rule: str, line: Optional[int], status: str, note: str):
        self.rule, self.line, self.status, self.note = rule, line, status, note


# --------------------------------------------------------------------------- #
# Text structure
# --------------------------------------------------------------------------- #
def fence_mask(lines: Sequence[str]) -> List[bool]:
    """True for every line inside (or delimiting) a fenced block."""
    mask, inside, marker = [], False, ""
    for ln in lines:
        m = FENCE_RE.match(ln)
        if m and not inside:
            inside, marker = True, m.group(1)
            mask.append(True)
        elif m and inside and ln.strip().startswith(marker):
            inside = False
            mask.append(True)
        else:
            mask.append(inside)
    return mask


def sections(lines: Sequence[str], mask: Sequence[bool]) -> List[Tuple[str, int, int]]:
    """(heading text, first line index, end index exclusive); the preamble is ('', 0, …)."""
    starts = [(i, HEADING_RE.match(ln).group(1).strip()) for i, ln in enumerate(lines)
              if not mask[i] and HEADING_RE.match(ln)]
    out: List[Tuple[str, int, int]] = []
    prev_i, prev_h = 0, ""
    for i, h in starts:
        if i > prev_i or prev_h:
            out.append((prev_h, prev_i, i))
        prev_i, prev_h = i, h
    out.append((prev_h, prev_i, len(lines)))
    return [s for s in out if s[2] > s[1]]


def paragraphs(lines: Sequence[str], mask: Sequence[bool], start: int, end: int) -> List[Tuple[int, str]]:
    """(first line number 1-based, joined text) for every prose paragraph in [start, end).

    A list item or table row starts a new paragraph; fenced lines and headings are skipped.
    """
    out: List[Tuple[int, str]] = []
    cur: List[str] = []
    cur_start = -1

    def flush() -> None:
        nonlocal cur, cur_start
        if cur:
            out.append((cur_start + 1, " ".join(s.strip() for s in cur)))
        cur, cur_start = [], -1

    for i in range(start, end):
        ln = lines[i]
        if mask[i] or HEADING_RE.match(ln) or not ln.strip():
            flush()
            continue
        if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", ln) or TABLE_ROW_RE.match(ln):
            flush()
        if not cur:
            cur_start = i
        cur.append(ln)
    flush()
    return out


def fenced_texts(lines: Sequence[str], mask: Sequence[bool], start: int, end: int) -> List[str]:
    out, cur, inside = [], [], False
    for i in range(start, end):
        if mask[i] and FENCE_RE.match(lines[i]) and not inside:
            inside, cur = True, []
        elif mask[i] and FENCE_RE.match(lines[i]) and inside:
            inside = False
            out.append("\n".join(cur))
        elif mask[i] and inside:
            cur.append(lines[i])
    return out


def _norm(s: str) -> str:
    s = re.sub(r"^\s*(?:--|\*|\||>|#+)\s*", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


# --------------------------------------------------------------------------- #
# Repo lookups (git, never the working tree — the laptop checkout is stale)
# --------------------------------------------------------------------------- #
class Repo:
    def __init__(self, path: Optional[Path], ref: str):
        self.path, self.ref = path, ref

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        # universal_newlines is text=True by its older name — spelled this way so operator_artifacts.py
        # (which reads `text=` as an emitted message) can see every string this file shows a human
        return subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, universal_newlines=True)

    def available(self) -> Tuple[bool, str]:
        if self.path is None:
            return False, "no --repo given"
        if not (self.path / ".git").exists():
            return False, "%s is not a git repo" % self.path
        cp = self._git("rev-parse", "--verify", "--quiet", self.ref + "^{commit}")
        if cp.returncode != 0:
            return False, "ref %s not found in %s — fetch first" % (self.ref, self.path)
        return True, ""

    def show(self, rel: str) -> Optional[str]:
        cp = self._git("show", "%s:%s" % (self.ref, rel))
        return cp.stdout if cp.returncode == 0 else None

    def raise_origins(self) -> Dict[str, str]:
        """token -> the LOWEST-numbered migration on the ref that raises it (a re-issued body is not a new refusal)."""
        cp = self._git("grep", "-i", "-o", "-E", r"RAISE[[:space:]]+EXCEPTION[[:space:]]+'[a-z][a-z0-9_]*",
                       self.ref, "--", "supabase/migrations")
        out: Dict[str, str] = {}
        if cp.returncode not in (0, 1):
            return out
        for ln in cp.stdout.split("\n"):
            # <ref>:supabase/migrations/NNNN_x.sql:RAISE EXCEPTION 'token
            m = re.match(r"[^:]*:supabase/migrations/([^:]+):(.*)$", ln)
            if not m:
                continue
            f, rest = m.group(1), m.group(2)
            t = RAISE_RE.search(rest + "'")
            if not t:
                continue
            tok = t.group(1).lower()
            if tok not in out or f < out[tok]:
                out[tok] = f
        return out

    def db_functions(self) -> Set[str]:
        cp = self._git("grep", "-h", "-i", "-o", "-E", r"FUNCTION[[:space:]]+(public\.)?[a-z_][a-z0-9_]*[[:space:]]*\(",
                       self.ref, "--", "supabase/migrations")
        if cp.returncode not in (0, 1):
            return set()
        return {m.group(1).lower() for m in FUNCTION_RE.finditer(cp.stdout)}


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #
def check_text(text: str, repo: Optional[Repo] = None, db_functions: Optional[Set[str]] = None,
               decisions_only: bool = False) -> List[Row]:
    lines = text.split("\n")
    mask = fence_mask(lines)
    secs = sections(lines, mask)
    rows: List[Row] = []
    title = next((ln.strip() for ln in lines if ln.strip()), "")

    repo_ok, repo_why = (repo.available() if repo is not None else (False, "no repo"))
    if db_functions is None:
        db_functions = repo.db_functions() if (repo is not None and repo_ok) else set()
    fn_call_re = (re.compile(r"`?\b(%s)\b`?\s*\([^)]*\{" % "|".join(sorted(map(re.escape, db_functions))), re.I)
                  if db_functions else None)

    # ---- structure rules (briefs only) ----------------------------------- #
    if not decisions_only:
        named = []
        for ln in lines:
            for m in MIGRATION_RE.finditer(ln):
                if m.group(1) not in named:
                    named.append(m.group(1))
        refus = sum(len(re.findall(r"refus", ln, re.I)) for i, ln in enumerate(lines) if not mask[i])
        needs_table = bool(named) or refus >= 4
        pt = [s for s in secs if "predicate table" in s[0].lower()]
        table_rows: List[str] = []
        pt_text = ""
        if pt:
            h, s, e = pt[0]
            body = [lines[i] for i in range(s + 1, e) if not mask[i]]
            data = [ln for ln in body if TABLE_ROW_RE.match(ln) and not TABLE_SEP_RE.match(ln)]
            table_rows = data[1:] if len(data) >= 1 else []
            pt_text = "\n".join(lines[s:e]).lower()
        if needs_table and not pt:
            rows.append(Row("predicate-table", None, "FAIL",
                            "names %d migration file(s) / says 'refus…' %d× and has no heading containing "
                            "'predicate table' — write it: one row per DB refusal → the UI door that prevents the "
                            "tap (or 'none, by design: <why>' with the quote)" % (len(named), refus)))
        elif needs_table and not table_rows:
            rows.append(Row("predicate-table", pt[0][1] + 1, "FAIL",
                            "the predicate-table heading has no table with a data row under it"))
        elif needs_table:
            rows.append(Row("predicate-table", pt[0][1] + 1, "ok", "%d row(s)" % len(table_rows)))
        else:
            rows.append(Row("predicate-table", None, "n/a", "no migration named and 'refus…' < 4"))

        # refusal-rows: the NEWEST migration the brief names is this PR's (the rest are reading
        # material); every token it raises that NO older migration raises owes a predicate row.
        mig_texts: Dict[str, str] = {}
        if named and repo is not None and not repo_ok:
            rows.append(Row("refusal-rows", None, "FAIL", "cannot read the repo: %s" % repo_why))
        elif named and repo is not None:
            newest = max(named)
            origins = repo.raise_origins()
            for f in named:
                t = repo.show("supabase/migrations/" + f)
                if t is not None:
                    mig_texts[f] = t
            t = mig_texts.get(newest)
            if t is None:
                rows.append(Row("refusal-rows", None, "n/a", "%s is not on %s (a step-1 brief's own file)" % (newest, repo.ref)))
            else:
                tokens = sorted({m.group(1).lower() for m in RAISE_RE.finditer(t)})
                new_tokens = [tok for tok in tokens if origins.get(tok, newest) >= newest]
                missing = [tok for tok in new_tokens if tok not in pt_text]
                if not tokens:
                    rows.append(Row("refusal-rows", None, "ok", "%s raises nothing" % newest))
                elif missing:
                    rows.append(Row("refusal-rows", None, "FAIL",
                                    "%s raises %d token(s), %d new in it; %d have NO predicate-table row: %s"
                                    % (newest, len(tokens), len(new_tokens), len(missing), ", ".join(missing))))
                else:
                    rows.append(Row("refusal-rows", None, "ok", "%s: all %d new refusal token(s) have a row (%d re-issued, not counted)"
                                    % (newest, len(new_tokens), len(tokens) - len(new_tokens))))
        elif named:
            rows.append(Row("refusal-rows", None, "n/a", "no repo to read the migration(s) from"))

        # two-clocks for a step-2 brief
        if STEP2_RE.search(title):
            tc = [s for s in secs if "two clocks" in s[0].lower()]
            if not tc:
                rows.append(Row("two-clocks", None, "FAIL",
                                "a STEP 2 brief has no heading containing 'TWO CLOCKS' — paste the step-1 "
                                "migration header's two-clocks paragraph VERBATIM, with a row per NEW READER of "
                                "pre-existing rows"))
            else:
                h, s, e = tc[0]
                body = [_norm(lines[i]) for i in range(s + 1, e)]
                body = [b for b in body if len(b) >= 40]
                if mig_texts:
                    hay = {f: " ".join(_norm(x) for x in t.split("\n") if _norm(x)) for f, t in mig_texts.items()}
                    hit = next((f for f, hv in hay.items() for b in body if b in hv), None)
                    if hit:
                        rows.append(Row("two-clocks", s + 1, "ok", "verbatim from %s" % hit))
                    else:
                        rows.append(Row("two-clocks", s + 1, "FAIL",
                                        "no line of the TWO CLOCKS section (%d substantive) appears verbatim in %s — "
                                        "paste the header's paragraph, do not paraphrase it" % (len(body), ", ".join(mig_texts))))
                elif not body:
                    rows.append(Row("two-clocks", s + 1, "FAIL", "the TWO CLOCKS section is empty"))
                else:
                    rows.append(Row("two-clocks", s + 1, "ok", "present (%d lines; no named migration on the ref to compare)" % len(body)))

    # ---- claim rules ------------------------------------------------------ #
    n_claims = n_ok = 0
    for h, s, e in secs:
        fences = fenced_texts(lines, mask, s, e)
        for ln_no, para in paragraphs(lines, mask, s, e):
            cited = bool(CITATION_RE.search(para))
            # quote claims
            qhits = [name for name, rx in QUOTE_CLAIMS.items() if rx.search(para)]
            if qhits:
                n_claims += 1
                if fences:
                    n_ok += 1
                    rows.append(Row("claim-quote", ln_no, "ok", "'%s' with a fenced quote in §'%s'" % (qhits[0], h[:40])))
                else:
                    rows.append(Row("claim-quote", ln_no, "FAIL",
                                    "says '%s' and the section '%s' has no fenced block — paste the rule's OWN comment "
                                    "(`sed -n` the lines) under this paragraph; a citation is not a quote" % (qhits[0], h[:40])))
            # cite claims
            chits = [name for name, rx in CITE_CLAIMS.items() if rx.search(para)]
            if fn_call_re is not None and fn_call_re.search(para):
                chits.append("DB function called with a literal payload")
            if chits:
                n_claims += 1
                if cited or fences:
                    n_ok += 1
                    rows.append(Row("claim-cite", ln_no, "ok", "'%s' with %s" % (chits[0], "a file:line citation" if cited else "a fenced block")))
                else:
                    ids = [t for t in re.findall(r"`([^`]{2,60})`", para)][:3]
                    rows.append(Row("claim-cite", ln_no, "FAIL",
                                    "asserts '%s' with no file:line citation and no fenced block in the paragraph's section — "
                                    "paste `grep -n` for %s and cite the line" % (chits[0], ", ".join("`%s`" % i for i in ids) or "the thing it is about")))
            # cap claims
            if CAP_CLAIM.search(para):
                n_claims += 1
                sec_text = " ".join(lines[i] for i in range(s, e) if not mask[i])
                if CAP_EXCLUDED.search(sec_text):
                    n_ok += 1
                    rows.append(Row("claim-cap", ln_no, "ok", "the section says what the excluded item means"))
                else:
                    rows.append(Row("claim-cap", ln_no, "FAIL",
                                    "names a cap and the section never says what the first EXCLUDED item means for the human "
                                    "(write the sentence: 'the 201st … is …')"))
    rows.append(Row("claims", None, "info", "%d claim paragraph(s), %d with evidence" % (n_claims, n_ok)))
    return rows


def check_file(path: Path, repo: Optional[Repo], decisions_only: bool = False) -> List[Row]:
    return check_text(path.read_text(), repo=repo, decisions_only=decisions_only)


def failures(rows: Sequence[Row]) -> List[Row]:
    return [r for r in rows if r.status == "FAIL"]


def format_table(rows: Sequence[Row], name: str) -> str:
    out = ["brief_check: %s" % name, "| rule | line | status | note |", "|---|---|---|---|"]
    for r in rows:
        out.append("| %s | %s | %s | %s |" % (r.rule, r.line if r.line is not None else "—", r.status,
                                               r.note.replace("|", "\\|")))
    n = len(failures(rows))
    out.append("%s: %d FAIL row(s)" % ("REFUSED" if n else "PASS", n))
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="brief_check.py — refuse a builder brief whose sentences are guesses. "
                                            "Exit 0 passes, 2 refused (fix the brief, never the check), 1 usage error.")
    p.add_argument("brief")
    p.add_argument("--repo", default=None, help="the RSDPM checkout whose `--ref` holds the migrations (default: none)")
    p.add_argument("--ref", default="origin/main")
    p.add_argument("--decisions", action="store_true", help="a decisions file: claim rules only")
    a = p.parse_args(argv)
    path = Path(a.brief).expanduser()
    if not path.is_file():
        print("brief_check: not a file: %s" % path, file=sys.stderr)
        return EXIT_USAGE
    repo = Repo(Path(a.repo).expanduser().resolve(), a.ref) if a.repo else None
    rows = check_file(path, repo, decisions_only=a.decisions)
    print(format_table(rows, str(path)))
    return EXIT_REFUSED if failures(rows) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
