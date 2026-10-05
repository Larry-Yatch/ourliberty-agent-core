#!/usr/bin/env python3
"""rsdpm_runner.py — the RSDPM build runner: the MECHANICAL steps of dispatching
one headless builder per PR, watching it, and filing its cost.

The manager keeps every judgement (reading findings, deciding per finding, the
click-through, the merge). This script only:

  build     guards → brief_check (REFUSES a brief whose sentences are guesses — no
            waiver; see scripts/brief_check.py) → worktree → per-worktree push hook
            → wrapper+brief → detached `claude -p`
  fix       guards → brief_check on the manager's DECISIONS (claim rules) → compose
            a fix brief from the findings JSON + the decisions → a FRESH builder in
            the EXISTING worktree
  watch     poll a run (pid, local vs remote head, PR open, comment count); on exit
            parse the last `result` line and file the ledger row
  ledger    parse one stream-json file → ONE row in the build ledger (idempotent)
  classify  table of findings with a PRE-FILLED severity column the manager edits
  status    every run, the slot table, the last ledger row per PR, STOP present?
  daily     the one-line daily report from the ledger
  stop      write STOP (build/fix refuse while it exists);  resume  removes it

It never runs a review, never verifies a finding, never labels, never merges,
never writes to staging or the droplet, never deletes anything outside its own
state dir. `tests/test_rsdpm_runner.py` pins each of those where a test can.

Spec: ~/dev/ol-work/rsdpm-build-ledger.md (the four measured runs + notes) and
~/dev/ol-work/rsdpm-phased-plan-2026-09-22.md § PRE-BUILD REVIEW + RUNNER SAFETY.

Python 3.9, stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import brief_check  # beside this file in scripts/; the gate on every brief and decisions file

# --------------------------------------------------------------------------- #
# Defaults (every path is overridable so the tests never touch the real ones)
# --------------------------------------------------------------------------- #
HOME = Path(os.environ.get("HOME", str(Path.home())))
DEFAULT_STATE_DIR = HOME / "dev" / "ol-work" / "runner"
DEFAULT_LEDGER = HOME / "dev" / "ol-work" / "rsdpm-build-ledger.md"
DEFAULT_REPO = HOME / "dev" / "RSDPM"
DEFAULT_CLAUDE_CMD = HOME / ".local" / "bin" / "claude-alt"
ACCOUNTS_ROOT = HOME / ".claude-accounts"
DEFAULT_WALL_SECONDS = 4 * 3600
POLL_SECONDS = 30
MAX_BUILDERS = 2
SLOTS = ("b", "c")

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_BUILDER_FAILED = 3

LEDGER_COLUMNS = [
    "date", "PR", "step", "slot", "model", "cap", "est_usd", "wall",
    "api time", "turns", "in / cache-write / cache-read / out tokens", "outcome",
]

# Severity pre-fill from the review's category. The manager REPLACES the `?`
# word by hand with reachable / latent / coverage / record. The script decides nothing.
CATEGORY_PREFILL = {
    "correctness": "reachable?",
    "security": "reachable?",
    "test-coverage": "latent?",
    "efficiency": "latent?",
    "simplification": "latent?",
    "conventions": "latent?",
    "stale-claim": "latent?",
    "reuse": "latent?",
    "altitude": "latent?",
    "accessibility": "latent?",
    "data-flow": "latent?",
}
SEVERITY_WORDS = ("reachable", "latent", "coverage", "record")
STOP_RULE_SENTENCE = (
    "Stop rule: zero REACHABLE wrong-behaviour findings = stop reviewing, "
    "manager check, merge; three rounds is the ceiling."
)

# --------------------------------------------------------------------------- #
# The STANDARD WRAPPER — prepended to every brief. Templates; compose_prompt fills
# {slot} {worktree} {branch}. test_wrapper_every_line_present pins each line.
# --------------------------------------------------------------------------- #
STANDARD_WRAPPER_LINES: Tuple[str, ...] = (
    "# Dispatch wrapper (from the RSDPM build runner) — read this, then the brief below it.",
    "",
    "You are running headless under account slot {slot}, dispatched by the runner. "
    "Report to the PR, never to a chat.",
    "- Your working directory IS the worktree: `{worktree}`, branch `{branch}`. It already exists, "
    "with `npm ci` done and `.env.local` copied in. Do NOT create a worktree. "
    "Never touch `~/dev/RSDPM` (the shared checkout) or any other worktree.",
    "- The branch is `feat/*`: open the PR with NO label and NOT draft. Never rename the branch, "
    "never add a label. That is the hold.",
    "- Commit after EVERY green step. A post-commit hook pushes each commit for you; if a push "
    "fails, run `git push -u origin HEAD` yourself before the next step.",
    "- You have no permission prompts. The shared gates still fire on Bash: follow a deny message's "
    "instructions and retry. Never use `OL_EVIDENCE_WAIVE`.",
    "- Proof scripts boot their own throwaway Postgres (postgresql@15 on PATH). No Docker, "
    "no `supabase start`.",
    "- A dev server must NEVER use port 3131 or 3132. Kill it when done.",
    "- Staging is real: READ only. Never write to staging and never delete a row.",
    "- 45-minute breaker: if any single step passes 45 minutes, stop, commit what is green, "
    "and write the state into the PR.",
    "- If the Write tool refuses with \"read the file first\", read the file and retry. "
    "It is NOT another agent editing your worktree. Never stop for it.",
    "- Never `git add -A` / `git commit -a` while `void_check.py` or any in-place check runs. "
    "Run the discipline tools in a detached copy (`git worktree add --detach`) or stage by "
    "explicit path, and `git status --porcelain` must be empty before every commit.",
    "- Discipline items 2, 3, 4, 5, 7, 8, 9, 10, 12 and 20 of `~/.claude/code-discipline.md` are "
    "mandatory; paste their artifacts, never describe them.",
    "- Per finding in a fix round, paste a SHAPE grep that lists every sibling of the defect "
    "(item 20), not just the line the reviewer named.",
    "- End your final message with ONE line: `COST: <the usage summary you can see, or \"unknown\">`.",
    "",
    "---",
    "",
)

FIX_EVIDENCE_LIST: Tuple[str, ...] = (
    "Per finding: the reproduction at the PR head (red output pinned) and the closing commit — "
    "or, for a no-code decision, the PR-body sentence added or deleted.",
    "Per finding: the SHAPE grep (item 20) — every sibling of the defect, with its state.",
    "`void_check.py` on the SPEC re-anchored: rules removed and added by name; table pasted; "
    "filed via `discipline_inbox`.",
    "Item 4 over this round's delta; items 5 and 7 per changed function (garbage in; what the "
    "human sees on throw; the hundredth run).",
    "`operator_artifacts.py --repo-root . --base origin/main` and `claim_drift.py --repo-root .` "
    "pasted; `migration_reissue.py --repo-root . --fetch` if a migration changed.",
    "The runs: typecheck (`set -o pipefail`, exit printed), the WHOLE vitest suite, "
    "`verify:coverage-floor`, both CI jobs' step lists locally, `bash ops/verify-fresh-db.sh`; "
    "`npm run lab:shoot` + `npm run verify:contrast:live` if a screen changed.",
    "Item 11: EDIT THE PR BODY so every claim is true at the new head; re-read the body after editing it; "
    "grep the repo for survivors of every deleted claim.",
    "'What I could not verify'. Then ONE line: `COST: <what you can see>`.",
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
class Refusal(Exception):
    """A guard said no. Message is what the manager reads."""


def _now() -> _dt.datetime:
    return _dt.datetime.now()


def _stamp() -> str:
    return _now().strftime("%Y%m%dT%H%M%S")


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def run_cmd(argv: Sequence[str], cwd: Optional[Path] = None, env: Optional[dict] = None,
            check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess:
    """One choke point for every child command (tests stub it or put fakes on PATH)."""
    return subprocess.run(
        list(argv), cwd=str(cwd) if cwd else None, env=env, check=check, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )


def pid_alive(pid: int) -> bool:
    """True while the process still runs. A zombie (exited, not yet reaped) is
    NOT alive: `kill -0` succeeds on one, so reap it if it is ours and ask `ps`."""
    if pid <= 0:
        return False
    try:
        os.waitpid(pid, os.WNOHANG)  # reap if it is our own child
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        cp = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return True
    st = cp.stdout.strip()
    if not st:
        return False
    return not st.startswith("Z")


def fmt_int(n) -> str:
    try:
        return "{:,}".format(int(n))
    except (TypeError, ValueError):
        return "?"


def fmt_dur(ms) -> str:
    try:
        s = int(round(int(ms) / 1000.0))
    except (TypeError, ValueError):
        return "—"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return "%dh %02dm %02ds" % (h, m, sec)
    return "%dm %02ds" % (m, sec)


def fmt_usd(v) -> str:
    try:
        return "$%.2f" % float(v)
    except (TypeError, ValueError):
        return "?"


# --------------------------------------------------------------------------- #
# State dir
# --------------------------------------------------------------------------- #
class State:
    def __init__(self, state_dir: Path, ledger: Path):
        self.dir = Path(state_dir).expanduser()
        self.ledger = Path(ledger).expanduser()
        self.runs = self.dir / "runs"
        self.stop_file = self.dir / "STOP"
        self.slots_file = self.dir / "slots.json"

    def ensure(self) -> None:
        self.runs.mkdir(parents=True, exist_ok=True)

    def read_slots(self) -> Dict[str, dict]:
        if not self.slots_file.exists():
            return {}
        try:
            data = json.loads(self.slots_file.read_text())
        except ValueError:
            raise Refusal("slots.json is not valid JSON: %s — fix it by hand" % self.slots_file)
        return data if isinstance(data, dict) else {}

    def write_slots(self, slots: Dict[str, dict]) -> None:
        self.ensure()
        tmp = self.slots_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(slots, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.slots_file)

    def live_slots(self) -> Dict[str, dict]:
        return {s: v for s, v in self.read_slots().items() if pid_alive(int(v.get("pid", 0)))}

    def register_slot(self, slot: str, pid: int, pr: str, step: str) -> None:
        slots = self.read_slots()
        slots[slot] = {"pid": pid, "pr": pr, "step": step, "started_at": _now().isoformat(timespec="seconds")}
        self.write_slots(slots)

    def release_slot_for_pid(self, pid: int) -> None:
        slots = self.read_slots()
        changed = False
        for s in list(slots):
            if int(slots[s].get("pid", 0)) == pid:
                del slots[s]
                changed = True
        if changed:
            self.write_slots(slots)

    def run_dir(self, pr: str) -> Path:
        d = self.runs / pr
        d.mkdir(parents=True, exist_ok=True)
        return d

    def metas(self, pr: Optional[str] = None) -> List[dict]:
        out = []
        base = self.runs if pr is None else self.runs / pr
        if not base.exists():
            return out
        for p in sorted(base.rglob("*.meta.json")):
            try:
                m = json.loads(p.read_text())
            except ValueError:
                continue
            m["_meta_path"] = str(p)
            out.append(m)
        out.sort(key=lambda m: m.get("started_at", ""))
        return out

    def latest_meta(self, pr: str) -> Optional[dict]:
        ms = self.metas(pr)
        return ms[-1] if ms else None


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def guard_stop(state: State) -> None:
    if state.stop_file.exists():
        raise Refusal("STOP file present (%s) — `rsdpm_runner.py resume` removes it" % state.stop_file)


def guard_builders(state: State, slot: str) -> None:
    live = state.live_slots()
    if len(live) >= MAX_BUILDERS:
        raise Refusal("two builders already alive: %s — never a third" % ", ".join(
            "%s(pid %s, %s %s)" % (s, v.get("pid"), v.get("pr"), v.get("step")) for s, v in sorted(live.items())))
    if slot in live:
        v = live[slot]
        raise Refusal("slot %s is busy: pid %s on %s %s" % (slot, v.get("pid"), v.get("pr"), v.get("step")))


def auth_status(slot: str) -> dict:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(ACCOUNTS_ROOT / slot)
    try:
        cp = run_cmd(["claude", "auth", "status"], env=env, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("slot %s: `claude auth status` could not run: %s" % (slot, exc))
    try:
        data = json.loads(cp.stdout.strip() or "{}")
    except ValueError:
        raise Refusal("slot %s: `claude auth status` did not return JSON (exit %s): %s"
                      % (slot, cp.returncode, (cp.stdout or cp.stderr).strip()[:200]))
    return data if isinstance(data, dict) else {}


def guard_auth(slot: str) -> None:
    data = auth_status(slot)
    if data.get("loggedIn") is not True:
        raise Refusal("slot %s is not logged in (`claude auth status` → loggedIn=%r); "
                      "sign in with `claude-alt %s` first" % (slot, data.get("loggedIn"), slot))


def branch_on_origin(repo: Path, branch: str) -> Optional[str]:
    cp = run_cmd(["git", "-C", str(repo), "ls-remote", "--heads", "origin", branch], check=False, timeout=120)
    if cp.returncode != 0:
        raise Refusal("git ls-remote failed in %s: %s" % (repo, cp.stderr.strip()[:200]))
    line = cp.stdout.strip()
    return line.split()[0] if line else None


def guard_branch_absent(repo: Path, branch: str) -> None:
    sha = branch_on_origin(repo, branch)
    if sha:
        raise Refusal("branch %s already exists on origin (%s) — pick another name" % (branch, sha[:8]))


def guard_brief(path: Path, repo: Path, ref: str, decisions: bool = False) -> None:
    """Print brief_check's table; refuse on any FAIL row. There is no flag that skips this —
    the nine brief-sentence nights (scripts/brief_check.py header) are why."""
    rows = brief_check.check_file(path, brief_check.Repo(repo, ref), decisions_only=decisions)
    print(brief_check.format_table(rows, str(path)))
    bad = brief_check.failures(rows)
    if bad:
        raise Refusal("%s refused by brief_check: %d FAIL row(s) above — fix the %s, never the check"
                      % ("decisions" if decisions else "brief", len(bad), "decisions file" if decisions else "brief"))


def guard_slot_name(slot: str) -> None:
    if slot not in SLOTS:
        raise Refusal("slot must be one of %s, got %r" % ("/".join(SLOTS), slot))


def guard_dispatch_params(cap: float, wall_seconds: int, claude_cmd: str) -> None:
    """Refuse the values that would silently disable a safety: a cap of 0 is no
    cap, `alarm 0` is no wall clock, and a missing dispatcher would only fail
    AFTER the worktree exists."""
    if not cap > 0:
        raise Refusal("--cap must be > 0 (got %r); 0 is no cap" % cap)
    if not wall_seconds > 0:
        raise Refusal("--wall-seconds must be > 0 (got %r); `perl alarm 0` is no wall clock" % wall_seconds)
    cmd = Path(str(claude_cmd)).expanduser()
    if not (cmd.is_file() and os.access(str(cmd), os.X_OK)):
        raise Refusal("--claude-cmd %s is not an executable file" % cmd)


# --------------------------------------------------------------------------- #
# Worktree + hook
# --------------------------------------------------------------------------- #
def worktree_path(repo: Path, letter: str) -> Path:
    # ~/dev/RSDPM → ~/dev/rsdpm-wt-<letter>: the brief's layout, no extra flag.
    return repo.parent / ("rsdpm-wt-%s" % letter)


POST_COMMIT_HOOK = """#!/bin/sh
# Installed by rsdpm_runner.py — per-worktree (core.hooksPath in this worktree's
# config.worktree). Every commit is pushed so a builder that dies loses nothing.
git push -u origin HEAD || echo "post-commit: push failed — run: git push -u origin HEAD" >&2
"""


def install_post_commit_hook(worktree: Path) -> Path:
    """Install the push hook for THIS worktree only.

    Measured 2026-09-30 (git 2.39): inside a linked worktree `git rev-parse
    --git-path hooks` resolves to the COMMON `.git/hooks`, shared with the main
    checkout — a push hook there would auto-push commits made in ~/dev/RSDPM.
    So the hook lives under the worktree's own gitdir and is wired with a
    per-worktree `core.hooksPath` (needs extensions.worktreeConfig on the repo).
    """
    git_dir = Path(run_cmd(["git", "rev-parse", "--git-dir"], cwd=worktree).stdout.strip())
    common = Path(run_cmd(["git", "rev-parse", "--git-common-dir"], cwd=worktree).stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    if not common.is_absolute():
        common = (worktree / common).resolve()
    if git_dir.resolve() == common.resolve():
        raise Refusal("%s is not a linked worktree (git-dir == common dir); refusing to install a "
                      "push hook on the main checkout" % worktree)
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-commit"
    hook.write_text(POST_COMMIT_HOOK)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    run_cmd(["git", "config", "extensions.worktreeConfig", "true"], cwd=worktree)
    run_cmd(["git", "config", "--worktree", "core.hooksPath", str(hooks_dir)], cwd=worktree)
    return hook


# --------------------------------------------------------------------------- #
# Prompt composition
# --------------------------------------------------------------------------- #
def wrapper_lines(slot: str, worktree: Path, branch: str) -> List[str]:
    return [ln.format(slot=slot, worktree=worktree, branch=branch) for ln in STANDARD_WRAPPER_LINES]


def compose_prompt(brief_text: str, slot: str, worktree: Path, branch: str) -> str:
    return "\n".join(wrapper_lines(slot, worktree, branch)) + "\n" + brief_text.rstrip("\n") + "\n"


def load_findings(path: Path) -> List[dict]:
    try:
        data = json.loads(Path(path).read_text())
    except ValueError as exc:
        raise Refusal("findings file is not JSON: %s (%s)" % (path, exc))
    findings = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(findings, list) or not findings:
        raise Refusal("findings file has no `findings` list: %s" % path)
    for i, f in enumerate(findings, 1):
        for key in ("file", "summary", "failure_scenario"):
            if not f.get(key):
                raise Refusal("finding %d lacks `%s` — not a ReportFindings JSON" % (i, key))
    return findings


_DECISION_HEAD = re.compile(r"^#{1,6}\s*(?:r\d+-)?F(\d+)\b.*$", re.IGNORECASE)


def parse_decisions(text: str) -> Dict[int, str]:
    """`## F3` (or `### r2-F3 …`) starts the decision for finding 3; body runs to the next head."""
    out: Dict[int, str] = {}
    current: Optional[int] = None
    buf: List[str] = []

    def flush():
        if current is not None:
            body = "\n".join(buf).strip()
            if current in out:
                raise Refusal("decisions file names F%d twice" % current)
            out[current] = body

    for line in text.splitlines():
        m = _DECISION_HEAD.match(line)
        if m:
            flush()
            current = int(m.group(1))
            buf = []
        else:
            buf.append(line)
    flush()
    return out


def compose_fix_brief(letter: str, round_no: int, slot: str, worktree: Path, branch: str,
                      head_sha: str, findings: List[dict], decisions: Dict[int, str]) -> str:
    n = len(findings)
    missing = [i for i in range(1, n + 1) if not decisions.get(i)]
    if missing:
        raise Refusal("no decision for finding(s) %s — every finding needs the manager's decision"
                      % ", ".join("F%d" % i for i in missing))
    extra = sorted(i for i in decisions if i < 1 or i > n)
    if extra:
        raise Refusal("decisions name %s but the findings file has %d finding(s)"
                      % (", ".join("F%d" % i for i in extra), n))
    lines = [
        "# Fix brief — PR %s (branch `%s`), review round %d → fix round %d" % (letter, branch, round_no, round_no),
        "",
        "**You are a FRESH headless builder on account slot %s.** The builders before you are gone. "
        "Your input is this brief, the PR on branch `%s` (`gh pr list --head %s`) and the repo. "
        "Report to the PR as a COMMENT, never to a chat." % (slot, branch, branch),
        "",
        "## Where you work",
        "- Worktree `%s` (exists; branch `%s`; `npm ci` done). Your cwd IS that worktree. "
        "`git fetch origin && git status` first; HEAD is `%s` = the PR head." % (worktree, branch, head_sha[:7]),
        "",
        "## Read first",
        "1. The PR body and its LAST comments (earlier rounds' findings, decisions and reports). Do not re-derive.",
        "2. `~/.claude/code-discipline.md` items 5, 7, 9, 11, 12, 14 and 20.",
        "3. Every file named below, in FULL, before touching it.",
        "",
        "## The %d finding%s (verbatim from `ReportFindings`, review round %d) and the DECISION on each"
        % (n, "" if n == 1 else "s", round_no),
        "",
    ]
    for i, f in enumerate(findings, 1):
        loc = "%s:%s" % (f.get("file"), f.get("line", "?"))
        lines += [
            "### r%d-F%d — `%s` [%s]" % (round_no, i, loc, f.get("category", "?")),
            "**Finding:** %s" % f["summary"],
            "**Scenario:** %s" % f["failure_scenario"],
            "**DECISION:** %s" % decisions[i],
            "",
        ]
    lines += ["## Evidence the PR COMMENT must carry (paste, never describe)"]
    lines += ["%d. %s" % (i, item) for i, item in enumerate(FIX_EVIDENCE_LIST, 1)]
    lines += [
        "",
        "## Reporting",
        "The PR comment is the report. Then STOP. The manager runs the next review round.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Launch
# --------------------------------------------------------------------------- #
def launch_builder(state: State, pr: str, step: str, slot: str, cap: float, prompt_text: str,
                   worktree: Path, branch: str, repo: Path, claude_cmd: str, wall_seconds: int,
                   dry_run: bool, extra_meta: Optional[dict] = None) -> dict:
    rd = state.run_dir(pr)
    base = rd / ("%s-%s" % (step.replace(" ", "-"), _stamp()))
    prompt_file = Path(str(base) + ".prompt.md")
    stream_file = Path(str(base) + ".stream.jsonl")
    err_file = Path(str(base) + ".err")
    pid_file = Path(str(base) + ".pid")
    meta_file = Path(str(base) + ".meta.json")
    prompt_file.write_text(prompt_text)
    argv = [
        "perl", "-e", "alarm shift; exec @ARGV", str(int(wall_seconds)),
        str(claude_cmd), slot, "-p", "--output-format", "stream-json", "--verbose",
        "--max-budget-usd", str(cap), "--dangerously-skip-permissions",
    ]
    # The model override the manager patched into the dispatch copy on 2026-10-02 (wave 6): when the
    # Fable weekly limit sits near 93% on both spare accounts, builders run on Opus via
    # RSDPM_BUILDER_MODEL=opus — the CLI ALIAS; "claude-opus-5-5" is refused as unrecognized_model.
    if os.environ.get("RSDPM_BUILDER_MODEL"):
        argv += ["--model", os.environ["RSDPM_BUILDER_MODEL"]]
    meta = {
        "pr": pr, "step": step, "slot": slot, "cap": cap, "branch": branch,
        "worktree": str(worktree), "repo": str(repo), "argv": argv,
        "prompt": str(prompt_file), "stream": str(stream_file), "err": str(err_file),
        "pid_file": str(pid_file), "wall_seconds": int(wall_seconds),
        "started_at": _now().isoformat(timespec="seconds"),
    }
    if extra_meta:
        meta.update(extra_meta)
    if dry_run:
        print("DRY-RUN: would launch (cwd=%s, stdin=%s, stdout=%s, stderr=%s):" % (worktree, prompt_file, stream_file, err_file))
        print("DRY-RUN:   " + " ".join(_sh(a) for a in argv))
        print("DRY-RUN: prompt written for reading: %s" % prompt_file)
        return meta
    with open(prompt_file, "rb") as fin, open(stream_file, "wb") as fout, open(err_file, "wb") as ferr:
        proc = subprocess.Popen(argv, cwd=str(worktree), stdin=fin, stdout=fout, stderr=ferr,
                                start_new_session=True)
    meta["pid"] = proc.pid
    pid_file.write_text("%d\n" % proc.pid)
    meta_file.write_text(json.dumps(meta, indent=2) + "\n")
    state.register_slot(slot, proc.pid, pr, step)
    print("LAUNCHED pid %d slot %s PR %s step %r cap $%s wall %ds" % (proc.pid, slot, pr, step, cap, wall_seconds))
    print("  stream: %s" % stream_file)
    print("  watch:  rsdpm_runner.py watch --pr %s" % pr)
    return meta


def _sh(a: str) -> str:
    return a if re.match(r"^[A-Za-z0-9_./:=@%+,-]+$", a) else "'" + a.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# Stream parsing + ledger
# --------------------------------------------------------------------------- #
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def parse_stream(path: Path) -> dict:
    """Read a stream-json file. Per-message usage is deduped by message.id (the CLI
    repeats one message's usage on every content block); the LAST `result` line is
    the authoritative envelope."""
    per_msg: Dict[str, dict] = {}
    result = None
    init = None
    bad = 0
    first_ts = None
    with open(path, "r", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                bad += 1
                continue
            t = obj.get("type")
            if t == "system" and obj.get("subtype") == "init":
                init = obj
            elif t == "assistant":
                msg = obj.get("message") or {}
                mid = msg.get("id")
                if mid and isinstance(msg.get("usage"), dict):
                    per_msg[mid] = msg["usage"]
                if first_ts is None and obj.get("timestamp"):
                    first_ts = obj["timestamp"]
            elif t == "result":
                result = obj
    sums = {k: sum(int(u.get(k, 0) or 0) for u in per_msg.values()) for k in USAGE_KEYS}
    return {"result": result, "init": init, "message_usage": sums, "messages": len(per_msg),
            "bad_lines": bad, "first_timestamp": first_ts}


def result_failure(parsed: dict) -> Optional[str]:
    """Why the run counts as failed, or None."""
    r = parsed.get("result")
    if r is None:
        return "missing result line (killed by the wall clock, or the stream is truncated)"
    if r.get("subtype") == "error_max_budget_usd":
        return "error_max_budget_usd — the cap stopped the run"
    if r.get("is_error"):
        return "is_error=true (subtype %s)" % r.get("subtype")
    return None


def _model_of(parsed: dict) -> str:
    r = parsed.get("result") or {}
    mu = r.get("modelUsage")
    if isinstance(mu, dict) and mu:
        return "+".join(sorted(mu))
    init = parsed.get("init") or {}
    return str(init.get("model") or "?")


def _date_of(parsed: dict) -> str:
    ts = parsed.get("first_timestamp")
    if ts:
        try:
            d = _dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
            if ts.endswith("Z"):
                d = d.replace(tzinfo=_dt.timezone.utc).astimezone()
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return _today()


def session_key(parsed: dict) -> Optional[str]:
    r = parsed.get("result") or parsed.get("init") or {}
    sid = r.get("session_id")
    return sid[:8] if sid else None


def build_ledger_row(parsed: dict, pr: str, step: str, slot: str = "?", cap=None) -> List[str]:
    r = parsed.get("result")
    sid = session_key(parsed) or "?"
    if r is not None:
        u = r.get("usage") or {}
        est = "**%s**" % fmt_usd(r.get("total_cost_usd"))
        wall = fmt_dur(r.get("duration_ms"))
        api = fmt_dur(r.get("duration_api_ms"))
        turns = str(r.get("num_turns", "?"))
        toks = " / ".join(fmt_int(u.get(k)) for k in USAGE_KEYS)
        denials = r.get("permission_denials") or []
        outcome = str(r.get("subtype", "?"))
        if r.get("terminal_reason") not in (None, "completed"):
            outcome += " (%s)" % r.get("terminal_reason")
        if denials:
            outcome += "; %d denial%s" % (len(denials), "" if len(denials) == 1 else "s")
        outcome += "; session %s" % sid
    else:
        s = parsed["message_usage"]
        est, wall, api, turns = "—", "—", "—", "—"
        toks = " / ".join(fmt_int(s[k]) for k in USAGE_KEYS)
        outcome = "NO RESULT LINE (per-message sum over %d messages); session %s" % (parsed["messages"], sid)
    return [_date_of(parsed), pr, step, slot, _model_of(parsed),
            fmt_usd(cap) if cap is not None else "?", est, wall, api, turns, toks, outcome]


def _row_text(cells: List[str]) -> str:
    return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"


def _split_row(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def find_ledger_table(lines: List[str]) -> Tuple[int, int]:
    """(header index, index of the last row) of the runs table. Refuses if the
    header's columns are not exactly LEDGER_COLUMNS — the column order is fixed."""
    for i, line in enumerate(lines):
        if line.startswith("|") and _split_row(line)[:3] == ["date", "PR", "step"]:
            cols = _split_row(line)
            if cols != LEDGER_COLUMNS:
                raise Refusal("ledger header columns changed: %s" % cols)
            last = i + 1  # the |---| separator
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                last = j
                j += 1
            return i, last
    raise Refusal("ledger has no runs table with header %s" % LEDGER_COLUMNS[:3])


def append_ledger_row(ledger: Path, cells: List[str], key: str, dry_run: bool) -> bool:
    """Insert ONE row after the last row of the runs table. Returns False (and
    writes nothing) when a row for `key` is already there."""
    if len(cells) != len(LEDGER_COLUMNS):
        raise Refusal("ledger row has %d cells, table has %d columns" % (len(cells), len(LEDGER_COLUMNS)))
    text = ledger.read_text()
    lines = text.split("\n")
    _, last = find_ledger_table(lines)
    if key and any(key in ln for ln in lines):
        return False
    row = _row_text(cells)
    if dry_run:
        print("DRY-RUN: would append to %s after line %d:" % (ledger, last + 1))
        print(row)
        return True
    lines.insert(last + 1, row)
    tmp = ledger.with_suffix(ledger.suffix + ".tmp")
    tmp.write_text("\n".join(lines))
    os.replace(tmp, ledger)
    return True


def ledger_rows(ledger: Path) -> List[List[str]]:
    if not ledger.exists():
        return []
    lines = ledger.read_text().split("\n")
    try:
        head, last = find_ledger_table(lines)
    except Refusal:
        return []
    return [_split_row(ln) for ln in lines[head + 2:last + 1]]


def file_ledger(state: State, parsed: dict, pr: str, step: str, slot: str, cap, dry_run: bool) -> Tuple[bool, str]:
    cells = build_ledger_row(parsed, pr, step, slot, cap)
    sid = session_key(parsed)
    key = ("session %s" % sid) if sid else ""
    added = append_ledger_row(state.ledger, cells, key, dry_run)
    return added, _row_text(cells)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_build(a, state: State) -> int:
    guard_slot_name(a.slot)
    guard_dispatch_params(a.cap, a.wall_seconds, a.claude_cmd)
    guard_stop(state)
    guard_builders(state, a.slot)
    guard_auth(a.slot)
    repo = Path(a.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        raise Refusal("%s is not a git repo" % repo)
    guard_branch_absent(repo, a.branch)
    brief = Path(a.brief).expanduser()
    if not brief.is_file():
        raise Refusal("brief not found: %s" % brief)
    wt = worktree_path(repo, a.pr)
    if wt.exists():
        raise Refusal("worktree already exists: %s — a build starts from nothing" % wt)
    env_local = repo / ".env.local"
    if not env_local.is_file():
        raise Refusal("%s missing — the builder cannot typecheck against staging without it" % env_local)
    e2e_state = repo / "e2e" / ".auth" / "state.json"
    step = "build"
    fetch = ("git fetch", ["git", "-C", str(repo), "fetch", "origin"], repo)
    if not a.dry_run:
        print("RUN %s" % fetch[0])
        cp = run_cmd(fetch[1], cwd=fetch[2], check=False, timeout=1800)
        if cp.returncode != 0:
            raise Refusal("%s failed (exit %d): %s" % (fetch[0], cp.returncode, (cp.stderr or cp.stdout).strip()[-400:]))
    guard_brief(brief, repo, a.base)
    prompt = compose_prompt(brief.read_text(), a.slot, wt, a.branch)
    plan = [
        ("worktree add", ["git", "-C", str(repo), "worktree", "add", str(wt), "-b", a.branch, a.base], repo),
        ("npm ci", ["npm", "ci"], wt),
    ]
    if a.dry_run:
        for name, argv, cwd in [fetch] + plan:
            print("DRY-RUN: %s: %s  (cwd %s)" % (name, " ".join(_sh(x) for x in argv), cwd))
        print("DRY-RUN: copy %s -> %s" % (env_local, wt / ".env.local"))
        if e2e_state.is_file():
            print("DRY-RUN: copy %s -> %s" % (e2e_state, wt / "e2e" / ".auth" / "state.json"))
        else:
            print("DRY-RUN: WARNING %s missing — the builder gets no e2e session (capture it with `npm run e2e:auth`)" % e2e_state)
        print("DRY-RUN: install per-worktree post-commit push hook in %s" % wt)
        launch_builder(state, a.pr, step, a.slot, a.cap, prompt, wt, a.branch, repo, a.claude_cmd,
                       a.wall_seconds, dry_run=True)
        print("DRY-RUN: nothing dispatched.")
        return EXIT_OK
    for name, argv, cwd in plan:
        print("RUN %s" % name)
        cp = run_cmd(argv, cwd=cwd, check=False, timeout=1800)
        if cp.returncode != 0:
            raise Refusal("%s failed (exit %d): %s" % (name, cp.returncode, (cp.stderr or cp.stdout).strip()[-400:]))
    shutil.copy2(env_local, wt / ".env.local")
    if e2e_state.is_file():
        (wt / "e2e" / ".auth").mkdir(parents=True, exist_ok=True)
        shutil.copy2(e2e_state, wt / "e2e" / ".auth" / "state.json")
    else:
        print("WARNING: %s missing — the builder gets no e2e session (capture it with `npm run e2e:auth`)" % e2e_state)
    hook = install_post_commit_hook(wt)
    print("HOOK %s" % hook)
    launch_builder(state, a.pr, step, a.slot, a.cap, prompt, wt, a.branch, repo, a.claude_cmd,
                   a.wall_seconds, dry_run=False)
    return EXIT_OK


def cmd_fix(a, state: State) -> int:
    guard_slot_name(a.slot)
    guard_dispatch_params(a.cap, a.wall_seconds, a.claude_cmd)
    guard_stop(state)
    guard_builders(state, a.slot)
    guard_auth(a.slot)
    meta = state.latest_meta(a.pr)
    if meta is None:
        raise Refusal("no run recorded for PR %s under %s — `build` first" % (a.pr, state.runs))
    wt = Path(meta["worktree"])
    branch = meta["branch"]
    repo = Path(meta["repo"])
    if not wt.is_dir() or not (wt / ".git").exists():
        raise Refusal("worktree missing: %s" % wt)
    dirty = run_cmd(["git", "status", "--porcelain"], cwd=wt).stdout.strip()
    if dirty:
        raise Refusal("worktree %s is dirty — a fresh builder starts from the PR head, never from a dead "
                      "context. Uncommitted:\n%s" % (wt, dirty[:800]))
    head = run_cmd(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip()
    remote = branch_on_origin(repo, branch)
    if remote is None:
        raise Refusal("branch %s is not on origin — the previous builder never pushed" % branch)
    if remote != head:
        raise Refusal("local HEAD %s != origin/%s %s — push or reset first so the fix builder sees the PR head"
                      % (head[:8], branch, remote[:8]))
    findings = load_findings(Path(a.findings).expanduser())
    guard_brief(Path(a.decisions).expanduser(), repo, "origin/main", decisions=True)
    decisions = parse_decisions(Path(a.decisions).expanduser().read_text())
    body = compose_fix_brief(a.pr, a.round, a.slot, wt, branch, head, findings, decisions)
    prompt = compose_prompt(body, a.slot, wt, branch)
    step = "fix round %d" % a.round
    launch_builder(state, a.pr, step, a.slot, a.cap, prompt, wt, branch, repo, a.claude_cmd,
                   a.wall_seconds, dry_run=a.dry_run,
                   extra_meta={"round": a.round, "findings": str(a.findings), "decisions": str(a.decisions)})
    if a.dry_run:
        print("DRY-RUN: nothing dispatched.")
    return EXIT_OK


def _gh_pr_for_branch(repo: Path, branch: str) -> Optional[dict]:
    cp = run_cmd(["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number,state,comments,url"],
                 cwd=repo, check=False, timeout=60)
    if cp.returncode != 0:
        return None
    try:
        prs = json.loads(cp.stdout or "[]")
    except ValueError:
        return None
    return prs[0] if prs else None


def poll_once(meta: dict, prev: dict) -> dict:
    """One observation; prints the change lines; returns the new snapshot."""
    pid = int(meta.get("pid", 0))
    alive = pid_alive(pid)
    wt = Path(meta["worktree"])
    repo = Path(meta["repo"])
    branch = meta["branch"]
    snap = {"alive": alive}
    local = remote = None
    if wt.is_dir():
        cp = run_cmd(["git", "rev-parse", "HEAD"], cwd=wt, check=False)
        local = cp.stdout.strip() if cp.returncode == 0 else None
        try:
            remote = branch_on_origin(repo, branch)
        except Refusal as exc:
            print("WARN %s" % exc)
    snap["local"] = local
    snap["remote"] = remote
    if local and local != prev.get("local"):
        print("LOCAL COMMIT %s" % local[:8])
    if remote and remote != prev.get("remote"):
        print("PUSH %s" % remote[:8])
    if local and remote and local != remote:
        print("UNPUSHED local %s remote %s" % (local[:8], remote[:8]))
    pr = _gh_pr_for_branch(repo, branch)
    if pr:
        n = len(pr.get("comments") or [])
        snap["pr"] = pr.get("number")
        snap["comments"] = n
        if prev.get("pr") != pr.get("number"):
            print("PR #%s %s %s" % (pr.get("number"), pr.get("state"), pr.get("url", "")))
        if n != prev.get("comments"):
            print("COMMENTS %d" % n)
    else:
        snap["pr"] = None
        snap["comments"] = None
    print("%s pid %s %s" % (_now().strftime("%H:%M:%S"), pid, "alive" if alive else "EXITED"))
    return snap


def cmd_watch(a, state: State) -> int:
    meta = state.latest_meta(a.pr)
    if meta is None:
        raise Refusal("no run recorded for PR %s" % a.pr)
    print("WATCH PR %s step %r slot %s pid %s stream %s" % (a.pr, meta["step"], meta["slot"], meta.get("pid"), meta["stream"]))
    prev: dict = {}
    while True:
        snap = poll_once(meta, prev)
        prev = snap
        if not snap["alive"]:
            break
        if a.once:
            return EXIT_OK
        time.sleep(POLL_SECONDS)
    stream = Path(meta["stream"])
    if not stream.exists():
        print("FAILED: stream file missing: %s" % stream)
        state.release_slot_for_pid(int(meta.get("pid", 0)))
        return EXIT_BUILDER_FAILED
    parsed = parse_stream(stream)
    added, row = file_ledger(state, parsed, meta["pr"], meta["step"], meta["slot"], meta.get("cap"), a.dry_run)
    print(("LEDGER ROW" if added else "LEDGER ROW (already filed)") + ":")
    print(row)
    state.release_slot_for_pid(int(meta.get("pid", 0)))
    why = result_failure(parsed)
    if why:
        err = Path(meta.get("err", ""))
        tail = ""
        if err.is_file():
            tail = err.read_text(errors="replace")[-600:]
        print("FAILED: %s" % why)
        if tail.strip():
            print("stderr tail:\n%s" % tail)
        return EXIT_BUILDER_FAILED
    print("DONE: %s" % (parsed["result"].get("subtype")))
    return EXIT_OK


def cmd_ledger(a, state: State) -> int:
    stream = Path(a.stream).expanduser()
    if not stream.is_file():
        raise Refusal("stream file not found: %s" % stream)
    parsed = parse_stream(stream)
    meta_path = Path(str(stream).replace(".stream.jsonl", ".meta.json"))
    slot, cap = "?", None
    if meta_path.is_file():
        try:
            m = json.loads(meta_path.read_text())
            slot, cap = m.get("slot", "?"), m.get("cap")
        except ValueError:
            pass
    added, row = file_ledger(state, parsed, a.pr, a.step, slot, cap, a.dry_run)
    if not added:
        print("REFUSED: a row for this run (%s) is already in %s" % ("session %s" % session_key(parsed), state.ledger))
        print(row)
        return EXIT_REFUSED
    print(row)
    why = result_failure(parsed)
    if why:
        print("NOTE: %s" % why)
    return EXIT_OK


def classify_table(findings: List[dict]) -> List[List[str]]:
    rows = []
    for i, f in enumerate(findings, 1):
        cat = str(f.get("category") or "?")
        rows.append(["F%d" % i, "%s:%s" % (f.get("file"), f.get("line", "?")), cat,
                     CATEGORY_PREFILL.get(cat, "?"), str(f.get("short_summary") or f.get("summary", ""))[:80]])
    return rows


def cmd_classify(a, state: State) -> int:
    findings = load_findings(Path(a.findings).expanduser())
    rows = classify_table(findings)
    print(_row_text(["#", "file:line", "category", "SEVERITY (manager fills: %s)" % " / ".join(SEVERITY_WORDS), "summary"]))
    print("|---|---|---|---|---|")
    for r in rows:
        print(_row_text(r))
    print("")
    print("Pre-filled from category only; `?` is a question, not a verdict. Replace each with one of: %s."
          % ", ".join(SEVERITY_WORDS))
    print(STOP_RULE_SENTENCE)
    return EXIT_OK


def cmd_status(a, state: State) -> int:
    print("STOP: %s" % ("PRESENT — build/fix refuse" if state.stop_file.exists() else "absent"))
    print("")
    print("Runs:")
    metas = state.metas()
    if not metas:
        print("  (none under %s)" % state.runs)
    for m in metas:
        pid = int(m.get("pid", 0) or 0)
        print("  PR %-3s %-14s slot %s pid %-6s %-7s started %s" % (
            m.get("pr"), m.get("step"), m.get("slot"), pid or "-",
            "alive" if pid_alive(pid) else "exited", m.get("started_at")))
    print("")
    print("Slots:")
    slots = state.read_slots()
    for s in SLOTS:
        v = slots.get(s)
        if v:
            print("  %s: pid %s %s — PR %s %s" % (s, v.get("pid"), "alive" if pid_alive(int(v.get("pid", 0))) else "exited",
                                              v.get("pr"), v.get("step")))
        else:
            print("  %s: free" % s)
    print("")
    print("Last ledger row per PR (%s):" % state.ledger)
    last: Dict[str, List[str]] = {}
    for row in ledger_rows(state.ledger):
        if len(row) == len(LEDGER_COLUMNS):
            last[row[1]] = row
    if not last:
        print("  (no rows)")
    for pr, row in last.items():
        print("  %s | %s | %s | %s | %s" % (pr, row[0], row[2], row[6], row[11][:60]))
    return EXIT_OK


def _usd_from_cell(cell: str) -> float:
    m = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", cell)
    return float(m.group(1)) if m else 0.0


def cmd_daily(a, state: State) -> int:
    today = _today()
    rows = [r for r in ledger_rows(state.ledger) if len(r) == len(LEDGER_COLUMNS) and r[0] == today]
    spend = sum(_usd_from_cell(r[6]) for r in rows)
    reviews = sum(1 for r in rows if re.search(r"\breview r\d+", r[2]))
    repo = Path(a.repo).expanduser()
    merged = "gh unavailable"
    cp = run_cmd(["gh", "pr", "list", "--state", "merged", "--search", "merged:>=%s" % today, "--json", "number"],
                 cwd=repo if repo.is_dir() else None, check=False, timeout=60)
    if cp.returncode == 0:
        try:
            merged = str(len(json.loads(cp.stdout or "[]")))
        except ValueError:
            pass
    live = state.live_slots()
    paused = "STOPPED" if state.stop_file.exists() else ("%d builder(s) alive" % len(live) if live else "none")
    print("%s | PRs merged: %s | spare-slot spend: $%.2f (%d run%s) | primary reviews: %d | paused/stopped: %s"
          % (today, merged, spend, len(rows), "" if len(rows) == 1 else "s", reviews, paused))
    return EXIT_OK


def cmd_stop(a, state: State) -> int:
    state.ensure()
    state.stop_file.write_text("stopped %s\n" % _now().isoformat(timespec="seconds"))
    print("STOP written: %s — build/fix refuse until `resume`" % state.stop_file)
    return EXIT_OK


def cmd_resume(a, state: State) -> int:
    if state.stop_file.exists():
        state.stop_file.unlink()
        print("STOP removed")
    else:
        print("no STOP file")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rsdpm_runner.py", description="The RSDPM build runner: the MECHANICAL steps of dispatching one headless builder per PR, watching it, and filing its cost. The manager keeps every judgement.")
    p.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    p.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    p.add_argument("--dry-run", action="store_true", help="print what would be done; dispatch and write nothing outside the state dir")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    def dispatch_args(sp):
        sp.add_argument("--pr", required=True, help="the PR letter (S, D, E, ...)")
        sp.add_argument("--slot", required=True, choices=list(SLOTS))
        sp.add_argument("--cap", required=True, type=float, help="--max-budget-usd for this run")
        sp.add_argument("--claude-cmd", default=str(DEFAULT_CLAUDE_CMD))
        sp.add_argument("--wall-seconds", type=int, default=DEFAULT_WALL_SECONDS)

    b = sub.add_parser("build", help="worktree + hook + wrapper+brief → one detached builder")
    dispatch_args(b)
    b.add_argument("--brief", required=True)
    b.add_argument("--branch", required=True)
    b.add_argument("--repo", default=str(DEFAULT_REPO))
    b.add_argument("--base", default="origin/main")
    b.set_defaults(fn=cmd_build)

    f = sub.add_parser("fix", help="compose a fix brief and dispatch a FRESH builder in the existing worktree")
    dispatch_args(f)
    f.add_argument("--round", required=True, type=int)
    f.add_argument("--findings", required=True, help="ReportFindings JSON")
    f.add_argument("--decisions", required=True, help="markdown: `## F1` … one decision per finding")
    f.set_defaults(fn=cmd_fix)

    w = sub.add_parser("watch", help="poll a run every 30 s; file the ledger row on exit")
    w.add_argument("--pr", required=True)
    w.add_argument("--once", action="store_true")
    w.set_defaults(fn=cmd_watch)

    l = sub.add_parser("ledger", help="parse a stream-json file and append ONE ledger row")
    l.add_argument("--pr", required=True)
    l.add_argument("--step", required=True)
    l.add_argument("--stream", required=True)
    l.set_defaults(fn=cmd_ledger)

    c = sub.add_parser("classify", help="findings table with a pre-filled severity column")
    c.add_argument("--findings", required=True)
    c.set_defaults(fn=cmd_classify)

    s = sub.add_parser("status")
    s.set_defaults(fn=cmd_status)

    d = sub.add_parser("daily")
    d.add_argument("--repo", default=str(DEFAULT_REPO))
    d.set_defaults(fn=cmd_daily)

    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    state = State(Path(a.state_dir), Path(a.ledger))
    state.ensure()
    try:
        return int(a.fn(a, state))
    except Refusal as exc:
        print("REFUSED: %s" % exc)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
