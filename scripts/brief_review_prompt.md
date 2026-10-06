# Pre-dispatch brief review — the fixed question set

Why: on RSDPM #318 the same subagent, asked the brief's own rules before review r1, caught 4 of 5 findings; asked "was my decision done as written?" after rounds 2 and 3, it caught 0 of 7.

HOW THE MANAGER USES THIS FILE: paste everything below the line to an Opus subagent with the brief's path filled in.
Write its answer, unchanged, to `brief-review-<brief stem>.md` beside the brief (for `brief-J2-step2.md` that is
`brief-review-brief-J2-step2.md`; for a decisions file `decisions-318-r2.md`, `brief-review-decisions-318-r2.md`).
`scripts/brief_check.py` (and so `rsdpm_runner.py build` / `fix`) refuses the brief while that file is missing, older
than the brief, missing a heading below, or open with defects. Edit the brief → run the review again.

---

You are a READ-ONLY reviewer. Do not edit, create or delete any file; do not run anything that writes. Read the
brief at: `<BRIEF PATH>`, and read the repository files it cites (use `git show origin/main:<path>` and
`git log` — the working tree may be stale).

Answer in 40 lines or fewer, under exactly these four headings, in this order. Under every heading write at least
one line — "none in this brief" is an answer; an empty heading is not.

## 1. Mirrors
For every "mirror X / X's shape" in the brief: quote X's lines from the repo at the cited path; list every sibling of X's kind in the same file or directory with its git date; say whether X is the newest, and if not, whether the brief says why.

## 2. Predicate rows
For every predicate-table row: does the migration raise this name in side-relative terms (from/to)? If yes, does the row carry two sentences? Which test will render each?

## 3. Human sentences
For every human-facing sentence: which control does its instruction point at, and is that control live when the sentence shows (quote the `disabled=` predicate)?

## 4. Carried fields
For every field the brief says is carried/kept/not rendered: name its reader or write NONE.

Then end with the line

BRIEF DEFECTS: N

where N is the number of defects you found (a sentence in the brief that is wrong, or a question above it cannot
answer), followed by one line per defect and nothing else. The manager fixes the brief and re-runs this review until
N = 0, or — for a defect deliberately deferred — replaces that defect's line with a line starting
`LEDGERED D<nn> — <reason>` (the deferred-items ledger row it was filed as).
