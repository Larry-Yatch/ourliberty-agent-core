#!/usr/bin/env python3
"""rotate_cycle_journal.py — bound the live /cycle journal, archive the rest.

`runbooks/cycle-journal.md` is rewritten and committed every cycle by
`run_cycle.sh`. Git stores a full blob for each version of a changed file, so a
30 MB journal committed ~90x/day produced ~800 MB/day of loose-object churn and
eventually choked `git gc` (2026-07-07 droplet overload: load 9, Mirror review
timeouts).

The cycle prompt only ever reads the last 5-10 entries (section 1), so the full
history in the live file is dead weight in every commit. This script keeps the
most-recent KEEP_RECENT entries in the live journal and moves older entries into
size-capped chunk files under `runbooks/journal-archive/`. Chunks are appended
to (never rewritten) and freeze once full, so git deltifies them trivially and
the per-cycle journal blob shrinks ~100x.

Deterministic. Idempotent on a clean re-run: a second run over an
already-trimmed journal is a no-op. Loss-safe under a crash: entries are made
durable in the archive (fsync) *before* the journal is atomically rewritten, so
the live journal stays authoritative until the archive write lands. The only
crash artifact is duplication — a kill in the narrow window between archiving a
batch and rewriting the journal leaves that batch in both places, so the next
run re-archives it (reference-only duplication, bounded to that one batch, never
loss). Wired into `run_cycle.sh` immediately before the journal auto-commit, so
the trimmed journal and any new archive chunk land in the same Pulse-owned
commit (preserving the single-committer invariant). No-op when the journal
already holds <= KEEP_RECENT entries.

Env overrides (default in parens):
  OL_JOURNAL_KEEP_RECENT   entries to retain in the live journal (40)
  OL_JOURNAL_CHUNK_CAP     soft byte cap per archive chunk before rolling (8 MiB)
  OL_JOURNAL_PATH          override the journal path (tests)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# An entry begins at a top-level "## Iteration ~<N> ..." heading and runs up to
# the next such heading (or EOF). Everything before the first heading is
# preamble. Requiring the iteration number after the word avoids false-splitting
# on a prose line that merely starts with "## Iteration" inside an entry body.
# (A line beginning "## Iteration <digits>" quoted inside a fenced code block
# would still split — an accepted limit; the archive is reference-only and the
# round-trip stays lossless either way.)
ENTRY_RE = re.compile(r"^## Iteration\s+~?\d", re.MULTILINE)

DEFAULT_KEEP_RECENT = 40
DEFAULT_CHUNK_CAP = 8 * 1024 * 1024  # 8 MiB
ARCHIVE_DIRNAME = "journal-archive"
CHUNK_GLOB = "cycle-journal-archive-*.md"
CHUNK_FMT = "cycle-journal-archive-{n:03d}.md"
CHUNK_NUM_RE = re.compile(r"cycle-journal-archive-(\d+)\.md$")


def _repo_root() -> Path:
    # scripts/rotate_cycle_journal.py -> repo root is one level up from scripts/.
    return Path(__file__).resolve().parents[1]


def _journal_path() -> Path:
    override = os.environ.get("OL_JOURNAL_PATH")
    if override:
        return Path(override)
    return _repo_root() / "runbooks" / "cycle-journal.md"


def _keep_recent() -> int:
    raw = os.environ.get("OL_JOURNAL_KEEP_RECENT")
    if raw and raw.strip().isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_KEEP_RECENT


def _chunk_cap() -> int:
    raw = os.environ.get("OL_JOURNAL_CHUNK_CAP")
    if raw and raw.strip().isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_CHUNK_CAP


def split_journal(text: str) -> tuple[str, list[str]]:
    """Return (preamble, entries). Concatenating preamble + ''.join(entries)
    reproduces `text` exactly (lossless round-trip)."""
    starts = [m.start() for m in ENTRY_RE.finditer(text)]
    if not starts:
        return text, []
    preamble = text[: starts[0]]
    bounds = starts + [len(text)]
    entries = [text[bounds[i] : bounds[i + 1]] for i in range(len(starts))]
    return preamble, entries


def _chunk_header(n: int) -> str:
    return (
        f"# /cycle Journal — archive chunk {n:03d}\n\n"
        "<!-- Immutable append-only overflow from runbooks/cycle-journal.md. "
        "Older Pulse iterations evicted from the live journal to keep its "
        "per-commit git blob small. Newest entries live in cycle-journal.md; "
        "this file is reference-only and is never rewritten once full. -->\n\n"
    )


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _current_chunk(archive_dir: Path) -> tuple[Path, int]:
    """Highest-numbered existing chunk, or (path-for-chunk-001, 1) if none."""
    highest_num = 0
    for p in archive_dir.glob(CHUNK_GLOB):
        m = CHUNK_NUM_RE.search(p.name)
        if m:
            highest_num = max(highest_num, int(m.group(1)))
    if highest_num == 0:
        return archive_dir / CHUNK_FMT.format(n=1), 1
    return archive_dir / CHUNK_FMT.format(n=highest_num), highest_num


def _append_batch(path: Path, batch: list[str]) -> None:
    """Append a batch of entries to a chunk in a single write+fsync."""
    if not batch:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(batch))
        fh.flush()
        os.fsync(fh.fileno())


def archive_entries(evicted: list[str], archive_dir: Path, cap: int) -> list[Path]:
    """Append evicted entries (oldest journal tail) to size-capped chunks.

    Fills the current chunk until adding an entry would exceed `cap`, then rolls
    to a fresh chunk. Each chunk's entries are written in one append+fsync (not
    per-entry), which cuts fsyncs to one-per-chunk and shrinks the torn-write
    window to a single append. Append-only, so git sees just the new bytes.
    Returns the chunk paths touched, current chunk last."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    cur_path, cur_num = _current_chunk(archive_dir)
    if not cur_path.exists():
        _atomic_write(cur_path, _chunk_header(cur_num))
    touched: list[Path] = [cur_path]
    # Track size in-memory to avoid a stat() per entry; seed from the chunk's
    # current on-disk size (it may already carry a header and prior entries).
    size = cur_path.stat().st_size
    header_len = len(_chunk_header(cur_num).encode("utf-8"))
    batch: list[str] = []
    for entry in evicted:
        entry_bytes = len(entry.encode("utf-8"))
        # Roll only when the chunk already carries content beyond its header,
        # so a single oversized entry still lands somewhere rather than looping.
        if size + entry_bytes > cap and size > header_len:
            _append_batch(cur_path, batch)
            batch = []
            cur_num += 1
            cur_path = archive_dir / CHUNK_FMT.format(n=cur_num)
            _atomic_write(cur_path, _chunk_header(cur_num))
            touched.append(cur_path)
            size = cur_path.stat().st_size
            header_len = len(_chunk_header(cur_num).encode("utf-8"))
        batch.append(entry)
        size += entry_bytes
    _append_batch(cur_path, batch)
    return touched


def rotate(journal_path: Path, keep_recent: int, chunk_cap: int) -> dict:
    """Trim journal to the newest `keep_recent` entries; archive the rest.

    Returns a summary dict. No-op (evicted=0) when the journal holds
    <= keep_recent entries or has no parseable entries."""
    if not journal_path.exists():
        return {"status": "missing", "evicted": 0, "kept": 0, "chunks": []}

    text = journal_path.read_text(encoding="utf-8")
    preamble, entries = split_journal(text)
    if len(entries) <= keep_recent:
        return {"status": "noop", "evicted": 0, "kept": len(entries), "chunks": []}

    # Entries are newest-first (Pulse prepends; section 13). Keep the top slice;
    # the tail is the oldest and gets archived.
    kept = entries[:keep_recent]
    evicted = entries[keep_recent:]

    archive_dir = journal_path.parent / ARCHIVE_DIRNAME
    chunks = archive_entries(evicted, archive_dir, chunk_cap)

    _atomic_write(journal_path, preamble + "".join(kept))
    return {
        "status": "rotated",
        "evicted": len(evicted),
        "kept": len(kept),
        "chunks": [str(c) for c in chunks],
    }


def main(argv: list[str]) -> int:
    summary = rotate(_journal_path(), _keep_recent(), _chunk_cap())
    if summary["status"] == "rotated":
        print(
            f"rotate_cycle_journal: kept {summary['kept']} entries, "
            f"archived {summary['evicted']} into {len(summary['chunks'])} chunk(s)"
        )
    elif summary["status"] == "noop":
        print(
            f"rotate_cycle_journal: no-op ({summary['kept']} entries "
            f"<= keep threshold)"
        )
    elif summary["status"] == "missing":
        print("rotate_cycle_journal: journal not found; nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
