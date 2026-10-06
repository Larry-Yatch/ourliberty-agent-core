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
  carried-no-reader A sentence that says a backticked field is "not rendered",
                    "not shown", "not displayed" or "no reader asked" names its
                    READER in that sentence or the next: another identifier or
                    a file:line within six words of an un-negated
                    renders/reads/consumes/displays/shows. Never the whole
                    paragraph (the original S3 passed on a "renders" nine lines
                    above its trigger), and never "carries/keep/kept": those
                    describe code (176 false refusals in 55 briefs). A
                    QUOTED phrase — double quotes, or backticks holding
                    whitespace — is cited, not asserted: it is blanked before
                    the trigger, the reader verb and the sentence ends are
                    looked for, so a reader verb inside a quotation does not
                    count; a blanked span is ONE token in the six-word window
                    and no word for the negation look-back.
                    (#318 r2-F2: "the row keeps them" shipped a roster name to
                    the browser past a SECURITY DEFINER join.)
  direction-bearing-refusal
                    Every predicate-table row whose refusal the named migration
                    raises in the caller's from/to terms — `: from`/`: to` in the
                    literal or a side in its arguments, a SWAPPED side comparison
                    in its statement group, or direction words in the comment
                    block above it — carries two quoted sentences or says "both
                    ends". (#318 r1-F1: "X already blocks this record" was
                    inverted under "blocked by".) Briefs only; n/a with no repo.
                    `cannot_link_kind: %` and `cannot_confirm: %` are direction-
                    bearing by their side format arguments (v_ft / v_tt,
                    0072_record_links.sql:493-503): a row naming either as a
                    whole span owes "both ends" or two sentences (a placeholder
                    span such as `cannot_link_kind: <kind>` is skipped).
  mirror-names-the-newest-sibling
                    "X's shape/gate/rule/precedent" or "mirror X", X a symbol on
                    --ref outside test files (`__tests__/`, `__fixtures__/`,
                    `.test.`, `.spec.`, `e2e/`, `tests/fakes/`, `tests/stubs/`,
                    `tests/**.py`; `tests/contracts/lib/` stays resolvable) and
                    written like one — backticked, or with two or
                    more capitals, or with `_` ("add-a-task's shape" and "PR B's
                    rule" are English), and not inside a quoted phrase —
                    QUOTES X's region. X defined in two or more such files is
                    one FAIL naming them unless the paragraph names exactly one
                    — by its full path, or by its file name at a name boundary
                    (review r1-F5: `Panel` is in 7, and the first grep hit was
                    taken silently; review r2-F1: `AddTaskPanel.tsx` named
                    `Panel.tsx`; `page.tsx` alone names every page). The quote half: a backticked span of 20+
                    characters with = ( ) { } that the whole file holds at most
                    twice (`router.refresh()` is 16 characters, in 14 places, one
                    inside RemoveVerb's own region), or a fenced block within 2
                    lines after whose line is in the region. X's REGION runs
                    from X to the first following top-level definition whose
                    name does not carry X's stem (X minus its CamelCase suffix)
                    at a name boundary — starts with it, `_` + it, or it
                    capitalised after a lowercase letter or digit — so
                    RemoveSheetBody and MergeConfirmStep stay in their door and
                    StatusVerbs ends OwnerVerb's region (review r1-F6: a quote
                    of StatusVerbs passed as Owner's lines). Every NEWER sibling
                    of X's kind cited in the same paragraph has a contrast word
                    (not / over / instead of / wrong or right sibling) within 12
                    words of its name, and the newest is named somewhere; the
                    rest are one info row. (#318 r2-F1: "RemoveVerb's gate
                    shape" was the wrong sibling; Merge, the newest door, keeps
                    its confirm live.) KNOWN CONSEQUENCE: a correct "MergeVerb's
                    rule" owes a fenced quote of Merge's lines; a door helper
                    that does not carry the door's name (`useConfirmState`,
                    merge.ts's `narrowCounts`) is outside the region — quote the
                    door's own lines. KNOWN WEAKNESS:
                    a rare 20+ span from the region that is not the rule being
                    mirrored still passes — a word rule cannot judge relevance;
                    the review's question 1 is the net for that.
  instruction-points-at-a-live-control
                    A quoted sentence carrying an imperative ("try again",
                    "unlink", "pick", "reopen" …) and a "dead/disabled until" or
                    "stays dead/disabled" clause anywhere in the same document
                    is one FAIL naming both lines; a quoted instruction whose
                    paragraph names no control is an info row. Document-wide on
                    purpose: #318 r2-F1's two sentences sat in S2 and S5. A
                    dead-until phrase inside double quotes or backticks is
                    cited, not asserted, and is ignored. Briefs only; a
                    decisions file describes the defect it removes.
  brief-review      The pre-dispatch review: `brief-review-<stem>.md` beside the
                    brief (or decisions file) exists, is no older than it,
                    answers under the four REVIEW_HEADINGS, and ends `BRIEF
                    DEFECTS: 0` or ledgers every defect (`LEDGERED D<nn>`). The
                    questions are scripts/brief_review_prompt.md. Computed in
                    check_file (it needs the path), so the CLI and both runner
                    calls demand it. (#318: the same subagent asked the brief's
                    rules before review r1 caught 4 of 5 findings.)

Fenced blocks (``` … ```) are never scanned for claims: pasted output is
evidence, not assertion. Headings are not paragraphs.

It CANNOT check that a quote was read, that a citation points at the governing
lines, or that an instruction traces the operator's next step (#308 r1-F1 was
a conscious ruling, "merge, then edit", whose edit landed on a row with no
fields). It cannot tell that a direction-bearing row's second quoted string is
the OTHER side's sentence: any quoted span of 3+ words counts, so the original
`cannot_link_removed` row passed on its parenthetical "<other> blocks <this>".
Nor that a "direction" in the comment above a raise means an END and not a
state transition (0066's `project_status_transition_not_allowed` is flagged on
"the closed direction"). A mirror name with one capital and no backticks
(Panel, Button) is not resolved — write it in backticks and it is. A
decisions file that narrates the wrong mirror it corrects in its OWN words, or
names it in a single-token backtick, reads as a mirror and owes a quote — put
the brief's sentence in double quotes and the row disappears. A paragraph
saying a CONTROL is not rendered reads as a carried field. R4 cannot tell that
the quoted instruction points at a DIFFERENT, live control (brief-F-step2.md:
"Save dead until a reason" beside a line telling the human to type it), nor
that a quoted line is a description, not an instruction.
Six more, each counted at 0 where it was measured (review r1, 2026-10-06), with
the repair to build when one occurs: (a) an unbalanced straight `"` in a
paragraph re-pairs every later quoted span, so a trigger after it can be
blanked (0 of 1,591 corpus paragraphs; repair: an odd quote count skips
blanking for that paragraph with an info row naming the line); (b) a shallow
clone dates every symbol born before its horizon to the horizon commit, so no
sibling is ever newer and the sibling half passes (0 of 15 RSDPM checkouts
shallow; repair: `git rev-parse --is-shallow-repository` in Repo.available →
every mirror row n/a with that reason); (c) the sibling half reads the raw
text, so a newer sibling named ONLY inside a quotation counts as named (0 of
17 R3 FAIL rows; repair: read it from the blanked text); (d) review freshness
is the file mtime, which a copy without `-p` reorders either way (0 of 3
review files stale; repair: the review records the brief's sha256); (e)
`git log -S"const X"` counts substrings, so an earlier `const XProps` dates X
too early (0 of 8 const pairs on main; repair: anchor the needle with the
character after the name); (f) `export default function Chip` is never matched
by find_definitions, so a brief mirroring one gets n/a (21 such files on main:
18 page, 2 layout, 1 error — none a brief mirrors). It
converts "did I look?" from a judgement into a check and stops there. Counted over the brief-sentence findings of RSDPM #288 → #318 there are
16 classes; the word rules catch 9 of 16 by construction. The 7 they still
cannot: (1) a citation pointing at the wrong lines (#301's finalize JSON, #297
Redate); (2) a gate predicate that needs a per-reader question row (#306 F1);
(3) "no row → no rows" fail-open (#306 F2); (4) a ruling's operator trace
(#308); (5) a header "unchanged" claim (#293); (6) an omission (#288, #290);
(7) a screen-state implication against a standing principle (#292 F5). The
pre-dispatch review (brief-review) is the net for those, not the word rules;
the external review remains the net behind it.

USAGE
-----
    brief_check.py BRIEF.md [--repo ~/dev/RSDPM] [--ref origin/main] [--decisions]

`--decisions` runs the claim rules, carried-no-reader and the mirror rule — no
predicate-table rules and no instruction-points-at-a-live-control, because a
decisions file describes the defect it removes (a manager's per-finding
decisions file is code-shaped instruction too — #283 r2 F1/F10 traced to
decision text). Every file also needs its review file,
`brief-review-<stem>.md`, beside it.
Exit 0 passes, 2 refused, 1 usage error. Python 3.9, stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
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


def paragraph_ranges(lines: Sequence[str], mask: Sequence[bool], start: int, end: int) -> List[Tuple[int, int, str]]:
    """(first line index, last line index, joined text) for every prose paragraph in [start, end).

    A list item or table row starts a new paragraph; fenced lines and headings are skipped.
    """
    out: List[Tuple[int, int, str]] = []
    cur: List[str] = []
    cur_start = -1

    def flush() -> None:
        nonlocal cur, cur_start
        if cur:
            out.append((cur_start, cur_start + len(cur) - 1, " ".join(s.strip() for s in cur)))
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


def paragraphs(lines: Sequence[str], mask: Sequence[bool], start: int, end: int) -> List[Tuple[int, str]]:
    """(first line number 1-based, joined text) for every prose paragraph in [start, end)."""
    return [(a + 1, t) for a, _b, t in paragraph_ranges(lines, mask, start, end)]


def line_of(lines: Sequence[str], first: int, offset: int) -> int:
    """1-based line number of character `offset` in a paragraph joined by paragraph_ranges from line index `first`."""
    i, pos = first, 0
    while i < len(lines):
        n = len(lines[i].strip())
        if offset <= pos + n:
            return i + 1
        pos += n + 1
        i += 1
    return first + 1


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
# Sentence rules (v2) — each earned by a #318 finding whose cause was a brief sentence
# --------------------------------------------------------------------------- #
Para = Tuple[int, int, str]  # (first line index, last line index, joined text)

NEGATORS = {"not", "never", "no", "nor", "nothing", "none", "nobody", "without", "dropped",
            "isn't", "aren't", "doesn't", "don't", "won't", "wasn't", "weren't"}
WORD_RE = re.compile(r"[A-Za-z]+(?:['’][a-z]+)?")


def _negated(text: str, start: int, n: int) -> bool:
    """True when one of the n words before `start` is a negator ('not carried', 'nothing reads')."""
    words = [w.lower().replace("’", "'") for w in WORD_RE.findall(text[:start])][-n:]
    return any(w in NEGATORS for w in words)


def _word_index(tokens: Sequence[Tuple[int, int]], offset: int) -> int:
    k = 0
    for j, (a, _b) in enumerate(tokens):
        if a <= offset:
            k = j
        else:
            break
    return k


CITED_SPAN_RE = re.compile(r"\"[^\"]*\"|“[^”]*”|`[^`]*`")


def blank_cited(text: str) -> str:
    """`text` with every QUOTED phrase blanked by same-length `#` filler: double-quoted spans (straight or curly) and
    backticked spans holding whitespace are cited, not asserted (R1, R3 and R4 all read the blanked text). A
    whitespace-free backtick span is a NAME — R1's field, R3's mirror — and is returned unchanged. One left-to-right scan
    pairs the spans: a pattern that REQUIRED whitespace inside the backticks would pair one span's closing backtick with
    the next one's opening backtick and blank the prose between them (brief-J2-step2.md:169's true dead-until). Blank,
    never delete: line_of counts characters into the joined paragraph, so a shorter text shifts every later line."""
    return CITED_SPAN_RE.sub(lambda m: m.group(0) if m.group(0)[0] == "`" and not re.search(r"\s", m.group(0))
                             else "#" * len(m.group(0)), text)


# R1 carried-no-reader
IDENT_SPAN_RE = re.compile(r"`([A-Za-z_$][\w$./-]*)`")
KEEP_RE = re.compile(r"\bno\s+reader\s+asked\b|\bnot\s+(?:rendered|shown|displayed)\b", re.I)
READER_VERB_RE = re.compile(r"\brendered\s+by\b|\bread\s+by\b|\b(?:renders|reads|consumes|displays|shows)\b", re.I)
SENTENCE_SPLIT_RE = re.compile(r"(?<=\.\))\s*|\.\s+|;\s+|\.$")
R1_NOTE = ("name the reader or do not carry it — a field with no reader crosses a boundary for nothing "
           "(#318 r2-F2: a roster name shipped to the browser past a SECURITY DEFINER join)")


def _reader_in(text: str, carried: Set[str]) -> Optional[str]:
    """The first reader named in `text`: a backticked identifier that is not a carried field, or a file:line
    citation, within six words of an un-negated reader verb. None when there is none."""
    tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    verbs = [_word_index(tokens, m.start()) for m in READER_VERB_RE.finditer(text) if not _negated(text, m.start(), 2)]
    if not verbs:
        return None
    cands = [(m.start(), m.group(0)) for m in CITATION_RE.finditer(text)]
    cands += [(m.start(), "`%s`" % m.group(1)) for m in IDENT_SPAN_RE.finditer(text) if m.group(1) not in carried]
    for off, name in sorted(cands):
        wi = _word_index(tokens, off)
        if any(abs(wi - v) <= 6 for v in verbs):
            return name
    return None


def _sentences(text: str) -> List[str]:
    return [x for x in SENTENCE_SPLIT_RE.split(text) if x and x.strip()]


def rule_carried_no_reader(lines: Sequence[str], paras: Sequence[Para]) -> List[Row]:
    """The reader must sit in the trigger's OWN sentence or the next one (the next paragraph's first sentence when the
    trigger ends its paragraph) — never anywhere in the paragraph: round 0 measured the original S3 paragraph passing
    on a "renders, inside `Panel`" nine lines above its trigger."""
    rows: List[Row] = []
    for k, (a, _b, raw) in enumerate(paras):
        text = blank_cited(raw)  # a quoted trigger, reader verb or sentence end is cited, not this document's own
        if not IDENT_SPAN_RE.search(text):
            continue
        sents = _sentences(text)
        n_here = len(sents)
        if k + 1 < len(paras):
            sents += _sentences(blank_cited(paras[k + 1][2]))
        carried: List[str] = []
        readers: List[str] = []
        unread = False
        for i in range(n_here):
            if not [m for m in KEEP_RE.finditer(sents[i]) if not _negated(sents[i], m.start(), 3)]:
                continue
            fields = [m.group(1) for m in IDENT_SPAN_RE.finditer(sents[i])]
            if not fields:  # the trigger and the field sit in different sentences of one paragraph
                fields = [m.group(1) for m in IDENT_SPAN_RE.finditer(text)]
            carried += [f for f in fields if f not in carried]
            reader = _reader_in(" ".join(sents[i:i + 2]), set(fields))
            if reader:
                readers.append(reader)
            else:
                unread = True
        if not carried:
            continue
        fields = ", ".join("`%s`" % c for c in carried)
        if unread:
            rows.append(Row("carried-no-reader", a + 1, "FAIL", "%s (kept here: %s)" % (R1_NOTE, fields)))
        else:
            rows.append(Row("carried-no-reader", a + 1, "ok", "%s kept; reader %s" % (fields, ", ".join(readers))))
    return rows


# R2 direction-bearing-refusal
RAISE_LIT_RE = re.compile(r"RAISE\s+EXCEPTION\s+'((?:[^']|'')*)'(.*)$", re.I)
DIR_LITERAL_RE = re.compile(r":\s(?:from|to)\b", re.I)
DIR_ARGS_RE = re.compile(r"v_ft|v_tt|from_type|to_type|v_from_|v_to_", re.I)
DIR_SWAP_RE = re.compile(r"from_type\s*=\s*v_tt|to_type\s*=\s*v_ft|from_id\s*=\s*v_ti|to_id\s*=\s*v_fi", re.I)
DIR_COMMENT_RE = re.compile(r"direction|reverse|opposite|either\s+way\s+round|caller['’]s\s+from|from/to", re.I)
BOTH_ENDS_RE = re.compile(r"both\s+directions|either\s+direction|both\s+ends|either\s+end|whichever\s+end", re.I)
QUOTED_RE = re.compile(r"\"([^\"]+)\"|“([^”]+)”")
R2_NOTE = ("a refusal raised in the caller's from/to terms has two truths — one sentence is wrong in one of them "
           "(#318 r1-F1: 'X already blocks this record' was inverted under 'blocked by') — write one quoted sentence "
           "per side, or say 'both ends' and how the one sentence covers both")


class RaiseSite:
    __slots__ = ("file", "index", "literal")

    def __init__(self, file: str, index: int, literal: str):
        self.file, self.index, self.literal = file, index, literal


def raise_sites(mig_texts: Dict[str, str]) -> List[RaiseSite]:
    out: List[RaiseSite] = []
    for f, t in sorted(mig_texts.items()):
        for i, ln in enumerate(t.split("\n")):
            m = RAISE_LIT_RE.search(ln)
            if m:
                out.append(RaiseSite(f, i, m.group(1).replace("''", "'")))
    return out


def _lit_rx(literal: str) -> str:
    return "".join("[a-z0-9_]+" if part == "%" else re.escape(part) for part in re.split(r"(%)", literal))


def match_raises(span: str, sites: Sequence[RaiseSite]) -> List[RaiseSite]:
    """The raises a predicate-table span names: a FULL match of the literal (`%` = a token), else the span's prefix
    before its first `:` against each literal's prefix — keeping only the LONGEST fixed text before the first `%`."""
    full = [s for s in sites if re.fullmatch(_lit_rx(s.literal), span, re.I)]
    if full:
        return full
    prefix = span.split(":", 1)[0].strip()
    if not prefix:
        return []
    cands = [s for s in sites if re.fullmatch(_lit_rx(s.literal.split(":", 1)[0].strip()), prefix, re.I)]
    if not cands:
        return []
    best = max(len(s.literal.split("%", 1)[0]) for s in cands)
    return [s for s in cands if len(s.literal.split("%", 1)[0]) == best]


def r2_span_ok(span: str) -> bool:
    """A placeholder span (`<kind>`, `…`, `a|b`) names no single raise."""
    return not any(c in span for c in "|<…")


def direction_bearing(mig_lines: Sequence[str], raise_index: int) -> bool:
    """Is the RAISE at mig_lines[raise_index] phrased in the caller's from/to terms? (a) its literal says `: from` /
    `: to` or its format arguments name a side; (b) its STATEMENT GROUP (the contiguous non-blank, non-`--` lines
    ending at it) compares sides SWAPPED; (c) the `--` comment block directly above that group speaks of direction."""
    m = RAISE_LIT_RE.search(mig_lines[raise_index])
    if not m:
        return False
    args = re.split(r"\bUSING\b", m.group(2), maxsplit=1, flags=re.I)[0]
    if DIR_LITERAL_RE.search(m.group(1)) or DIR_ARGS_RE.search(args):
        return True
    j = raise_index
    while j > 0 and mig_lines[j - 1].strip() and not mig_lines[j - 1].strip().startswith("--"):
        j -= 1
    if any(DIR_SWAP_RE.search(mig_lines[k]) for k in range(j, raise_index + 1)):
        return True
    k = j - 1
    comment: List[str] = []
    while k >= 0 and mig_lines[k].strip().startswith("--"):
        comment.append(mig_lines[k])
        k -= 1
    return any(DIR_COMMENT_RE.search(c) for c in comment)


def table_cells(row: str) -> List[str]:
    """Split a markdown table row on `|` OUTSIDE backticks (`cannot_link_draft|rejected` is one span)."""
    cells, cur, tick = [], [], False
    for ch in row.strip():
        if ch == "`":
            tick = not tick
        if ch == "|" and not tick:
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    cells.append("".join(cur))
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return [c.strip() for c in cells]


def _is_sentence(q: str) -> bool:
    """A quoted span of three or more words ("Already linked." is two and is a label, not a sentence)."""
    return len([w for w in re.findall(r"\S+", q) if re.search(r"\w", w)]) >= 3


def quoted_sentences(text: str) -> List[str]:
    qs = [m.group(1) if m.group(1) is not None else m.group(2) for m in QUOTED_RE.finditer(text)]
    return [q for q in qs if _is_sentence(q)]


def rule_direction_bearing(rows_in: Sequence[Tuple[int, str]], mig_texts: Dict[str, str]) -> List[Row]:
    sites = raise_sites(mig_texts)
    lines_of = {f: t.split("\n") for f, t in mig_texts.items()}
    out: List[Row] = []
    for idx, row in rows_in:
        cells = table_cells(row)
        if len(cells) < 2:
            continue
        hits: List[RaiseSite] = []
        for span in re.findall(r"`([^`]+)`", cells[0]):
            if r2_span_ok(span):
                hits += [s for s in match_raises(span.strip(), sites) if s not in hits]
        bearing = [s for s in hits if direction_bearing(lines_of[s.file], s.index)]
        if not bearing:
            continue
        where = ", ".join("'%s' (%s:%d)" % (s.literal, s.file, s.index + 1) for s in bearing)
        rest = " ".join(cells[1:])
        if len(quoted_sentences(rest)) >= 2 or BOTH_ENDS_RE.search(rest):
            out.append(Row("direction-bearing-refusal", idx + 1, "ok", "direction-bearing %s — the row says both sides" % where))
        else:
            out.append(Row("direction-bearing-refusal", idx + 1, "FAIL", "%s — raised as %s" % (R2_NOTE, where)))
    if not out:
        out.append(Row("direction-bearing-refusal", None, "ok", "%d table row(s), none direction-bearing" % len(rows_in)))
    return out


# R3 mirror-names-the-newest-sibling
MIRROR_TRIGGERS = (
    re.compile(r"\b([A-Za-z_]\w*)`?['’]s\s+(?:\w+\s+){0,2}(?:shape|gate|rule|precedent)\b"),
    re.compile(r"\b[Mm]irror(?:s|ing|ed)?\s+(?:the\s+)?`?([A-Z_]\w*)"),
    re.compile(r"\bin\s+`?([A-Za-z_]\w*)`?['’]s\s+shape\b"),
)
DEF_LINE_RE = re.compile(r"^(export )?(async )?(function|const|class) ([A-Za-z_][A-Za-z0-9_]*)")
CAMEL_SUFFIX_RE = re.compile(r"(?<=[a-z0-9])([A-Z][a-z0-9]+)$")
FILE_SUFFIX_RE = re.compile(r"^.+-([a-z0-9]+)\.tsx?$")
CONTRAST_RE = re.compile(r"\b(?:not|over|instead\s+of|wrong\s+sibling|right\s+sibling)\b", re.I)
GUTTER_RE = re.compile(r"^\s*\d+(?::|\t|\s{2,})")
TEST_PATH_RE = re.compile(r"__tests__/|__fixtures__/|\.test\.|\.spec\.|^e2e/|(^|/)tests/(fakes|stubs)/|(^|/)tests/.*\.py$")
R3_NOTE = ("quote the lines you are mirroring and say why the older sibling beats the newer one (#318 r2-F1: "
           "'RemoveVerb's gate shape' was the WRONG sibling — Merge, the newest door, keeps its confirm live; it cost a "
           "review round and a fix round)")


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _quotable(s: str) -> bool:
    """A quote that can be checked: ≥12 characters with code punctuation in it (`canRemove` alone is a name, not a quote)."""
    return len(s) >= 12 and any(c in s for c in "=(){}")


def _day(ct: int) -> str:
    return _dt.datetime.utcfromtimestamp(ct).strftime("%Y-%m-%d")


def carries_stem(name: str, stem: str) -> bool:
    """`name` carries `stem` only at a NAME BOUNDARY: it starts with the stem, or holds `_` + stem (both case-
    insensitive), or holds the stem with its first letter upper-cased right after a lowercase letter or digit
    (`fetchMergeProposal` carries `Merge`, `sameOwner` carries `Owner`). Never a bare substring: `move` sits inside
    `remove`, and a substring rule hands the Move door the whole Remove door (brief-G-step2.md:69 would pass)."""
    low, st = name.lower(), stem.lower()
    if low.startswith(st) or ("_" + st) in low:
        return True
    return re.search(r"(?<=[a-z0-9])" + re.escape(stem[:1].upper() + stem[1:]), name) is not None


def mirror_names(text: str) -> List[str]:
    """Every capture that looks like a symbol: backticked, or holding two or more capitals, or holding `_`. Any other
    capture is English or a one-letter label (round 1: "add-a-task's shape", "a row's shape" and "PR B's rule"
    resolved to non-test consts on RSDPM main) and is dropped before resolution."""
    out: List[str] = []
    for rx in MIRROR_TRIGGERS:
        for m in rx.finditer(text):
            name = m.group(1)
            ticked = m.start(1) > 0 and text[m.start(1) - 1] == "`"
            if not (ticked or sum(c.isupper() for c in name) >= 2 or "_" in name):
                continue
            if name not in out:
                out.append(name)
    return out


def rule_mirror(lines: Sequence[str], mask: Sequence[bool], paras: Sequence[Para], repo: Optional["Repo"],
                repo_ok: bool, repo_why: str) -> List[Row]:
    rule = "mirror-names-the-newest-sibling"
    rows: List[Row] = []
    cache: Dict[str, object] = {}

    def cached(key: str, fn):
        if key not in cache:
            cache[key] = fn()
        return cache[key]

    for a, b, text in paras:
        for name in mirror_names(blank_cited(text)):  # a quoted mirror is the sentence being corrected, not one made
            if repo is None or not repo_ok:
                rows.append(Row(rule, a + 1, "n/a", "%s: %s" % (name, repo_why)))
                continue
            hits = cached("def:" + name, lambda: repo.find_definitions(name))
            if hits is not None:  # a test file's local `const drawer` is never what a brief mirrors
                hits = [h for h in hits if not TEST_PATH_RE.search(h[0])]
            if hits is None:
                rows.append(Row(rule, a + 1, "FAIL", "git grep for the definition of %s on %s failed — fix the repo, "
                                "then re-run (an unresolved mirror is never a pass)" % (name, repo.ref)))
                continue
            if not hits:
                rows.append(Row(rule, a + 1, "n/a", "%s is not a function/const/class outside test files on %s" % (name, repo.ref)))
                continue
            files = list(dict.fromkeys(h[0] for h in hits))
            # a hit is NAMED by its full path in the paragraph, or else by its file name at a name boundary (review
            # r2-F1: a bare substring let `AddTaskPanel.tsx` name `Panel.tsx`); a full path wins over a bare name
            named_hits = [h for h in hits if h[0] in text] or [
                h for h in hits if re.search(r"(?<![\w.-])%s(?![\w-])" % re.escape(Path(h[0]).name), text)]
            named_files = list(dict.fromkeys(h[0] for h in named_hits))
            if len(named_files) > 1:  # `page.tsx` alone names every page: still a guess
                rows.append(Row(rule, a + 1, "FAIL", "%s is defined in %d files on %s; this paragraph names %d of them "
                                "(%s) — name only the one you mean, by its full path"
                                % (name, len(files), repo.ref, len(named_files), ", ".join(named_files))))
                continue
            if not named_files and len(files) > 1:  # review r1-F5: the first grep hit is a guess, and a guess never passes
                rows.append(Row(rule, a + 1, "FAIL", "%s is defined in %d files on %s (%s) — name the file you mean in "
                                "this paragraph" % (name, len(files), repo.ref, ", ".join(files))))
                continue
            path, def_line, def_text = (named_hits or hits)[0]
            src = cached("show:" + path, lambda: repo.show(path)) or ""
            src_lines = src.split("\n")
            defs = [(i + 1, m.group(3), m.group(4)) for i, ln in enumerate(src_lines) for m in [DEF_LINE_RE.match(ln)] if m]
            suf = CAMEL_SUFFIX_RE.search(name)
            suffix = suf.group(1) if suf else None
            sibs = [(ln, kw, n) for ln, kw, n in defs
                    if suffix and kw in ("function", "const") and n != name and n.endswith(suffix) and len(n) > len(suffix)]
            # X's region ends at the first following top-level definition that does not carry X's STEM (X minus its
            # CamelCase suffix): a door's own helpers (RemoveSheetBody, MergeConfirmStep) stay in, another door's
            # code (StatusVerbs inside OwnerVerb's old region, review r1-F6) does not
            stem = name[:suf.start()] if suf else name
            nxt = next((ln for ln, _k, n in defs if ln > def_line and not carries_stem(n, stem)), len(src_lines) + 1)
            region = _ws("\n".join(src_lines[def_line - 1:nxt - 1]))
            where = "%s:%d-%d" % (path, def_line, nxt - 1)

            # ---- quote half: a backticked span of 20+ chars from X's region that the file holds at most twice
            # (`router.refresh()` is 16 chars and in 14 places), or a fence right after the paragraph ----
            whole = _ws(src)
            quote = next((q for q in re.findall(r"`([^`]+)`", text)
                          if len(q) >= 20 and _quotable(q) and _ws(q) in region and whole.count(_ws(q)) <= 2), None)
            if quote is None:
                for i in (b + 1, b + 2):
                    if i < len(lines) and mask[i] and FENCE_RE.match(lines[i]):
                        j = i + 1
                        while j < len(lines) and not FENCE_RE.match(lines[j]):
                            body = GUTTER_RE.sub("", lines[j]).strip()
                            if _quotable(body) and _ws(body) in region:
                                quote = body
                                break
                            j += 1
                        break
            if quote is not None:
                rows.append(Row(rule, a + 1, "ok", "%s: quotes `%s` from %s" % (name, quote[:60], where)))
            else:
                rows.append(Row(rule, a + 1, "FAIL", "%s — %s is at %s and this paragraph quotes none of it" % (R3_NOTE, name, where)))

            # ---- sibling half ----
            kw = def_text.split(name, 1)[0].split()[-1] if name in def_text else "function"
            needle = "function %s(" % name if kw == "function" else "%s %s" % (kw, name)
            born = cached("born:" + needle + path, lambda: repo.birth(needle, path))
            if suffix:
                cands = [(n, "function %s(" % n if k == "function" else "%s %s" % (k, n), path) for _l, k, n in sibs]
            else:
                fm = FILE_SUFFIX_RE.match(Path(path).name)
                listing = (cached("ls:" + str(Path(path).parent), lambda: repo.ls_files(str(Path(path).parent))) or []) if fm else []
                cands = [(Path(p).name, None, p) for p in listing
                         if p != path and fm and re.match(r"^.+-%s\.tsx?$" % re.escape(fm.group(1)), Path(p).name)]
            if born is None:
                rows.append(Row(rule, a + 1, "FAIL", "could not date %s — git log -S found no commit adding \"%s\" to %s "
                                "on %s (a differently spelled signature such as function %s<T>(, or a git error) — check "
                                "the definition line and re-run" % (name, needle, path, repo.ref, name)))
                continue
            newer: List[Tuple[int, str]] = []
            for n, nd, p in cands:
                t = cached("born:%s%s" % (nd, p), (lambda nd=nd, p=p: repo.birth(nd, p) if nd else repo.file_birth(p)))
                if t is not None and t > born:
                    newer.append((t, n))
            newer.sort(reverse=True)
            if not newer:
                rows.append(Row(rule, a + 1, "ok", "%s is the newest of its siblings (%d)" % (name, len(cands))))
                continue
            # EVERY newer sibling: cited in this paragraph with no contrast word within 12 words of it → FAIL; the
            # newest named nowhere → FAIL; the rest (named here with why, or only elsewhere) → one info row
            listed = ", ".join("%s (%s)" % (n, _day(t)) for t, n in newer)
            tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
            contrast = [_word_index(tokens, m.start()) for m in CONTRAST_RE.finditer(text)]
            bad: List[str] = []
            info: List[str] = []
            for t, n in newer:
                here = [_word_index(tokens, m.start()) for m in re.finditer(r"\b%s\b" % re.escape(n), text)]
                if here and not any(abs(h - c) <= 12 for h in here for c in contrast):
                    bad.append("%s (%s) is named here with no why" % (n, _day(t)))
                elif here:
                    info.append("%s (%s) is newer and named here with why" % (n, _day(t)))
                else:
                    at = next((i + 1 for i, ln in enumerate(lines)
                               if not mask[i] and re.search(r"\b%s\b" % re.escape(n), ln)), None)
                    if at is not None:
                        info.append("%s is newer and mentioned at line %d — say in THIS sentence why %s's shape beats it"
                                    % (n, at, name))
                    elif n == newer[0][1]:
                        bad.append("the newest, %s, is named nowhere" % n)
            if bad:
                rows.append(Row(rule, a + 1, "FAIL", "%s — %s; newer than %s: %s" % (R3_NOTE, "; ".join(bad), name, listed)))
            if info:
                rows.append(Row(rule, a + 1, "info", "%s (newer: %s)" % ("; ".join(info), listed)))
    return rows


# R4 instruction-points-at-a-live-control
IMPERATIVE_RE = re.compile(r"\b(?:try\s+again|unlink|remove|restore|pick|choose|reopen|set|clear|go\s+to|open)\b", re.I)
DEAD_UNTIL_RE = re.compile(r"\b(?:dead|disabled|not\s+live|never\s+live)\s+until\b|\bstays\s+(?:dead|disabled)\b", re.I)
CONTROL_RE = re.compile(r"\b(?:button|link|panel|sheet|row|line|chip|radio|field)s?\b"
                        r"|`[^`]*(?:-confirm|-button|testid)[^`]*`", re.I)
R4_NOTE = ("an instruction the reader cannot obey is wrong copy (#318 r2-F1: 'Not linked — try again.' over a disabled "
           "Link) — delete the dead-until clause or change the sentence")


def rule_dead_until(lines: Sequence[str], paras: Sequence[Para]) -> List[Row]:
    rule = "instruction-points-at-a-live-control"
    instructions: List[Tuple[int, str, bool]] = []  # (line, sentence, its paragraph names a control)
    dead: List[Tuple[int, str]] = []
    for a, _b, text in paras:
        for m in QUOTED_RE.finditer(text):
            q = m.group(1) if m.group(1) is not None else m.group(2)
            if _is_sentence(q) and IMPERATIVE_RE.search(q):
                instructions.append((line_of(lines, a, m.start()), q, bool(CONTROL_RE.search(text))))
        bare = blank_cited(text)  # a dead-until phrase holds whitespace, so a name-only backtick never holds one
        dead += [(line_of(lines, a, m.start()), m.group(0)) for m in DEAD_UNTIL_RE.finditer(bare)]
    rows: List[Row] = []
    if instructions:
        q_lines = ", ".join(str(n) for n in sorted({n for n, _q, _c in instructions}))
        for d_line, phrase in dead:
            rows.append(Row(rule, d_line, "FAIL", "%s — '%s' at line %d; quoted instruction(s) at line %s"
                            % (R4_NOTE, phrase, d_line, q_lines)))
    for n, q, has_control in instructions:
        if has_control:
            rows.append(Row(rule, n, "ok", "\"%s\" — its paragraph names the control" % q[:60]))
        else:
            rows.append(Row(rule, n, "info", "\"%s\" — its paragraph names no control: say which button/link it "
                            "points at and that it is live when this shows" % q[:60]))
    return rows


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

    def raise_origins(self) -> Optional[Dict[str, str]]:
        """token -> the LOWEST-numbered migration on the ref that raises it (a re-issued body is not a new
        refusal). None when git itself failed — the caller prints a FAIL row, never an empty set (item 7)."""
        cp = self._git("grep", "-i", "-o", "-E", r"RAISE[[:space:]]+EXCEPTION[[:space:]]+'[a-z][a-z0-9_]*",
                       self.ref, "--", "supabase/migrations")
        out: Dict[str, str] = {}
        if cp.returncode not in (0, 1):
            return None
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

    def find_definitions(self, name: str) -> Optional[List[Tuple[str, int, str]]]:
        """(path, line, text) of every top-level `function|const|class <name>` on the ref; None when git failed.
        NEVER `\\b` in this pattern: Apple Git 2.39's `grep -E` does not support it and exits 1 for every symbol."""
        cp = self._git("grep", "-n", "-E", "^(export )?(async )?(function|const|class) %s([^A-Za-z0-9_]|$)" % name, self.ref)
        if cp.returncode not in (0, 1):
            return None
        out: List[Tuple[str, int, str]] = []
        for ln in cp.stdout.split("\n"):
            m = re.match(r"%s:([^:]+):(\d+):(.*)$" % re.escape(self.ref), ln)
            if m:
                out.append((m.group(1), int(m.group(2)), m.group(3)))
        return out

    def birth(self, needle: str, path: str) -> Optional[int]:
        """Commit time of the first commit on the ref that added `needle` to `path` (`git log -S`)."""
        cp = self._git("log", "--reverse", "--format=%ct", "-S" + needle, self.ref, "--", path)
        first = cp.stdout.split("\n")[0].strip() if cp.returncode == 0 else ""
        return int(first) if first.isdigit() else None

    def file_birth(self, path: str) -> Optional[int]:
        cp = self._git("log", "--reverse", "--format=%ct", self.ref, "--", path)
        first = cp.stdout.split("\n")[0].strip() if cp.returncode == 0 else ""
        return int(first) if first.isdigit() else None

    def ls_files(self, directory: str) -> Optional[List[str]]:
        cp = self._git("ls-tree", "--name-only", self.ref, directory.rstrip("/") + "/")
        return [x for x in cp.stdout.split("\n") if x] if cp.returncode == 0 else None

    def db_functions(self) -> Optional[Set[str]]:
        """Every function name a migration on the ref declares; None when git itself failed (see raise_origins)."""
        cp = self._git("grep", "-h", "-i", "-o", "-E", r"FUNCTION[[:space:]]+(public\.)?[a-z_][a-z0-9_]*[[:space:]]*\(",
                       self.ref, "--", "supabase/migrations")
        if cp.returncode not in (0, 1):
            return None
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
        if repo is not None and repo_ok:
            found = repo.db_functions()
            if found is None:
                rows.append(Row("repo", None, "FAIL", "git grep over supabase/migrations on %s failed — the DB-function "
                                "claim rule cannot run; fix the repo, do not proceed on an empty list" % repo.ref))
                found = set()
            db_functions = found
        else:
            db_functions = set()
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
        table_rows: List[Tuple[int, str]] = []  # (line index, row) for every DATA row (the header row dropped)
        pt_text = ""
        if pt:
            h, s, e = pt[0]
            data = [(i, lines[i]) for i in range(s + 1, e)
                    if not mask[i] and TABLE_ROW_RE.match(lines[i]) and not TABLE_SEP_RE.match(lines[i])]
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
            if origins is None:
                rows.append(Row("refusal-rows", None, "FAIL", "git grep for RAISE EXCEPTION on %s failed — cannot tell a new "
                                "refusal from a re-issued one; fix the repo first" % repo.ref))
                origins = {}
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

        # direction-bearing-refusal: reuses mig_texts (read above); never FAILs for want of a repo or a file
        if not table_rows:
            rows.append(Row("direction-bearing-refusal", None, "n/a", "no predicate-table data row"))
        elif not mig_texts:
            rows.append(Row("direction-bearing-refusal", None, "n/a",
                            "no named migration readable on the ref (%s)" % ("no repo" if repo is None else repo.ref)))
        else:
            rows += rule_direction_bearing(table_rows, mig_texts)

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

    # ---- sentence rules (v2): R1 and R3 in both modes, R4 in briefs only --- #
    paras = [p for _h, s, e in secs for p in paragraph_ranges(lines, mask, s, e)]
    rows += rule_carried_no_reader(lines, paras)
    rows += rule_mirror(lines, mask, paras, repo, repo_ok, repo_why)
    if not decisions_only:  # a decisions file describes the defect it removes ("stays disabled" is the reproduction)
        rows += rule_dead_until(lines, paras)

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


def review_path(brief: Path) -> Path:
    """The pre-dispatch review file beside a brief or decisions file: brief-J2-step2.md -> brief-review-brief-J2-step2.md."""
    return brief.with_name("brief-review-%s.md" % brief.stem)


REVIEW_HEADINGS = ("## 1. Mirrors", "## 2. Predicate rows", "## 3. Human sentences", "## 4. Carried fields")
REVIEW_PROMPT = "scripts/brief_review_prompt.md"
DEFECTS_RE = re.compile(r"^BRIEF DEFECTS:\s*(\d+)")
LEDGERED_RE = re.compile(r"^LEDGERED D\d+")


def check_review(brief: Path) -> List[Row]:
    """The pre-dispatch review — the step no word rule can do — must exist beside the brief, be NEWER than it, answer
    the four questions under their headings, and end `BRIEF DEFECTS: 0` (or ledger every defect it found)."""
    rp = review_path(brief)
    todo = ("run the review: paste %s to an opus subagent (read-only) with the brief path; write its verdict to %s"
            % (REVIEW_PROMPT, rp))
    if not rp.is_file():
        return [Row("brief-review", None, "FAIL", "no review file — " + todo)]
    rows: List[Row] = []
    if rp.stat().st_mtime < brief.stat().st_mtime:
        rows.append(Row("brief-review", None, "FAIL", "stale — the brief changed after the review; re-run the review — " + todo))
    lines = rp.read_text().split("\n")
    mask = fence_mask(lines)
    heads = [(i, HEADING_RE.match(ln).group(1).strip().lower()) for i, ln in enumerate(lines)
             if not mask[i] and HEADING_RE.match(ln)]
    defects = [i for i, ln in enumerate(lines) if not mask[i] and DEFECTS_RE.match(ln)]
    for want in REVIEW_HEADINGS:
        key = HEADING_RE.match(want).group(1).strip().lower()
        k = next((k for k, (_i, h) in enumerate(heads) if h.startswith(key)), None)
        if k is None:
            rows.append(Row("brief-review", None, "FAIL", "the review has no '%s' heading — %s" % (want, todo)))
            continue
        start = heads[k][0] + 1
        end = min([i for i, _h in heads if i >= start] + [i for i in defects if i >= start] + [len(lines)])
        if not any(lines[i].strip() for i in range(start, end)):
            rows.append(Row("brief-review", heads[k][0] + 1, "FAIL", "'%s' has nothing under it — %s" % (want, todo)))
    if not defects:
        rows.append(Row("brief-review", None, "FAIL", "the review has no 'BRIEF DEFECTS: N' line — " + todo))
    else:
        d = defects[-1]
        n = int(DEFECTS_RE.match(lines[d]).group(1))
        after = [ln.strip() for ln in lines[d + 1:] if ln.strip()]
        ledgered = [ln for ln in after if LEDGERED_RE.match(ln)]
        if n > 0 and (len(ledgered) != len(after) or len(ledgered) < n):
            rows.append(Row("brief-review", d + 1, "FAIL",
                            "BRIEF DEFECTS: %d with %d of %d line(s) under it as 'LEDGERED D<nn> — <reason>' — fix the brief "
                            "and re-run the review until N = 0, or ledger each defect; %s" % (n, len(ledgered), max(n, len(after)), todo)))
        if not rows:
            rows.append(Row("brief-review", None, "ok", "%s: four answers, BRIEF DEFECTS: %d (%d ledgered)" % (rp.name, n, len(ledgered))))
    return rows


def check_file(path: Path, repo: Optional[Repo], decisions_only: bool = False) -> List[Row]:
    """check_text over the file, plus the brief-review row (it needs the path; check_text stays text-only)."""
    return check_text(path.read_text(), repo=repo, decisions_only=decisions_only) + check_review(path)


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
                                            "The pre-dispatch review file brief-review-<stem>.md (the questions are "
                                            "scripts/brief_review_prompt.md) must sit beside the brief, newer than it. "
                                            "Exit 0 passes, 2 refused (fix the brief, never the check), 1 usage error.")
    p.add_argument("brief")
    p.add_argument("--repo", default=None, help="the RSDPM checkout whose `--ref` holds the migrations (default: none)")
    p.add_argument("--ref", default="origin/main")
    p.add_argument("--decisions", action="store_true",
                   help="a decisions file: no predicate-table rules and no instruction-points-at-a-live-control "
                        "(a decisions file describes the defect it removes)")
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
