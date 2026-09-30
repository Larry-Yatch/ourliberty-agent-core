#!/usr/bin/env python3
"""Tests for scripts/rsdpm_runner.py — the RSDPM build runner.

Every rule the brief names has a test that fails with the rule removed (the PR
body lists the mutation per test). Nothing here dispatches a real `claude`:
every dispatch goes to a recording fake via --claude-cmd, `claude`/`gh`/`npm`
are fakes on a private PATH, and every git operation runs in a throwaway repo
with a local bare remote. HOME is redirected so ~/dev/RSDPM, the real ledger and
the real state dir are never reachable.

Run:
    cd <worktree-root> && python3 -m unittest tests.test_rsdpm_runner -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "rsdpm_runner"

import rsdpm_runner as r  # noqa: E402

FAKE_CLAUDE = """#!/bin/sh
# fake `claude`: only `auth status` is ever called by the runner
if [ "$FAKE_LOGGED_IN" = "no" ]; then
  printf '{"loggedIn": false, "configDirectory": "%s"}\\n' "$CLAUDE_CONFIG_DIR"
else
  printf '{"loggedIn": true, "configDirectory": "%s"}\\n' "$CLAUDE_CONFIG_DIR"
fi
"""
FAKE_GH = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_RECORD_DIR/gh.calls"
printf '%s' "${FAKE_GH_JSON:-[]}"
"""
FAKE_NPM = """#!/bin/sh
printf 'npm %s\\n' "$*" >> "$FAKE_RECORD_DIR/npm.calls"
exit 0
"""
# The dispatcher stand-in for --claude-cmd: records argv / cwd / env / stdin,
# optionally sleeps, then replays a stream-json fixture on stdout.
FAKE_DISPATCH = """#!/bin/sh
d="$FAKE_RECORD_DIR"
printf '%s\\n' "$@" > "$d/argv"
pwd > "$d/cwd"
cat > "$d/stdin"
if [ -n "$FAKE_SLEEP" ]; then sleep "$FAKE_SLEEP"; fi
if [ -n "$FAKE_STREAM" ]; then cat "$FAKE_STREAM"; fi
exit 0
"""


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), check=check, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)


def _remote_sha(bare, branch):
    cp = _git(bare, "rev-parse", "--verify", "--quiet", "refs/heads/%s" % branch, check=False)
    return cp.stdout.strip() or None


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state"
        self.ledger = self.root / "ledger.md"
        shutil.copy(FIXTURES / "ledger-example.md", self.ledger)
        self.record = self.root / "record"
        self.record.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (("claude", FAKE_CLAUDE), ("gh", FAKE_GH), ("npm", FAKE_NPM)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.dispatch = self.root / "fake-dispatch"
        self.dispatch.write_text(FAKE_DISPATCH)
        self.dispatch.chmod(0o755)
        # the runner hands the detached builder to init on purpose; Popen's destructor warns about it
        warnings.filterwarnings("ignore", "subprocess .* is still running", ResourceWarning)
        self._env = dict(os.environ)
        os.environ["PATH"] = str(self.bin) + os.pathsep + os.environ["PATH"]
        os.environ["HOME"] = str(self.home)
        os.environ["FAKE_RECORD_DIR"] = str(self.record)
        for k in ("FAKE_LOGGED_IN", "FAKE_SLEEP", "FAKE_STREAM", "FAKE_GH_JSON"):
            os.environ.pop(k, None)
        # a throwaway "RSDPM": main checkout + local bare origin, main pushed
        self.bare = self.root / "dev" / "origin.git"
        self.bare.parent.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        self.repo = self.root / "dev" / "RSDPM"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "README.md").write_text("x\n")
        (self.repo / ".env.local").write_text("SECRET=1\n")
        (self.repo / "e2e" / ".auth").mkdir(parents=True)
        (self.repo / "e2e" / ".auth" / "state.json").write_text("{}\n")
        (self.repo / ".gitignore").write_text(".env.local\ne2e/.auth/\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        _git(self.repo, "remote", "add", "origin", str(self.bare))
        _git(self.repo, "push", "-q", "-u", "origin", "main")
        self.brief = self.root / "brief.md"
        self.brief.write_text("# Builder brief — PR S\n\nBuild the thing.\n")
        self._children = []

    def tearDown(self):
        for p in self._children:
            with contextlib.suppress(Exception):
                p.kill()
                p.wait(timeout=5)
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    # ---- helpers -------------------------------------------------------- #
    def run_cli(self, *args, dry_run=False):
        argv = ["--state-dir", str(self.state), "--ledger", str(self.ledger)]
        if dry_run:
            argv.append("--dry-run")
        argv += list(args)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = r.main(argv)
        return code, out.getvalue()

    def build_args(self, pr="S", slot="b", branch="feat/s", cap="5", **kw):
        a = ["build", "--pr", pr, "--brief", str(self.brief), "--slot", slot, "--cap", cap,
             "--branch", branch, "--repo", str(self.repo), "--base", "origin/main",
             "--claude-cmd", str(self.dispatch), "--wall-seconds", kw.pop("wall", "60")]
        return a

    def spawn_sleeper(self, secs=60):
        p = subprocess.Popen(["sleep", str(secs)])
        self._children.append(p)
        return p.pid

    def write_slots(self, slots):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "slots.json").write_text(json.dumps(slots))

    def wait_dead(self, pid, timeout=20):
        t0 = time.time()
        while r.pid_alive(pid) and time.time() - t0 < timeout:
            time.sleep(0.1)
        return not r.pid_alive(pid)

    def worktree(self, letter="S"):
        return self.repo.parent / ("rsdpm-wt-%s" % letter)

    def make_worktree_with_meta(self, letter="S", branch="feat/s", slot="b", push=True):
        wt = self.worktree(letter)
        _git(self.repo, "worktree", "add", "-q", str(wt), "-b", branch, "origin/main")
        _git(wt, "config", "user.email", "t@example.com")
        _git(wt, "config", "user.name", "t")
        if push:
            _git(wt, "push", "-q", "-u", "origin", branch)
        rd = self.state / "runs" / letter
        rd.mkdir(parents=True, exist_ok=True)
        meta = {"pr": letter, "step": "build", "slot": slot, "cap": 5.0, "branch": branch,
                "worktree": str(wt), "repo": str(self.repo), "pid": 0,
                "stream": str(rd / "build-x.stream.jsonl"), "err": str(rd / "build-x.err"),
                "started_at": "2026-09-30T10:00:00"}
        (rd / "build-x.meta.json").write_text(json.dumps(meta))
        return wt, meta

    # ---- the wrapper ------------------------------------------------------ #
    def test_wrapper_every_line_present(self):
        wt = Path("/x/rsdpm-wt-S")
        prompt = r.compose_prompt("# brief body\n", "b", wt, "feat/s")
        for line in r.wrapper_lines("b", wt, "feat/s"):
            self.assertIn(line, prompt.split("\n"), "wrapper line missing: %r" % line)
        self.assertTrue(prompt.endswith("# brief body\n"))
        self.assertLess(prompt.index("---"), prompt.index("# brief body"))

    def test_wrapper_carries_every_standing_rule(self):
        text = "\n".join(r.STANDARD_WRAPPER_LINES)
        for phrase in ("worktree", "already exists", "~/dev/RSDPM", "feat/*", "NO label", "NOT draft",
                       "Commit after EVERY green step", "hook pushes", "no permission prompts",
                       "OL_EVIDENCE_WAIVE", "own throwaway Postgres", "3131", "3132", "READ only",
                       "never delete", "45-minute", "read the file first", "git add -A", "commit -a",
                       "void_check.py", "git worktree add --detach", "git status --porcelain",
                       "2, 3, 4, 5, 7, 8, 9, 10, 12 and 20", "SHAPE grep", "COST:"):
            self.assertIn(phrase, text, "standing rule missing from wrapper: %r" % phrase)

    # ---- what the runner may never do ------------------------------------ #
    def test_source_never_reviews_labels_or_merges(self):
        src = (_SCRIPTS / "rsdpm_runner.py").read_text()
        for banned in ("gh pr merge", "--add-label", "auto-review", "code-review", "rmtree", "deep-review"):
            self.assertNotIn(banned, src, "runner source contains %r" % banned)
        # the `claude/*` branch prefix (an assertion that local review is done) — `~/.claude/` paths are not it
        self.assertIsNone(re.search(r"(?<![.\w])claude/", src), "runner source names a claude/* branch")
        # the only file it ever removes is its own STOP file
        self.assertEqual(src.count(".unlink()"), 1)
        self.assertIn("state.stop_file.unlink()", src)
        self.assertNotIn("os.remove", src)

    # ---- build guards ------------------------------------------------------ #
    def test_build_refuses_when_stop_present(self):
        self.run_cli("stop")
        code, out = self.run_cli(*self.build_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("STOP", out)
        self.assertFalse(self.worktree().exists())
        self.run_cli("resume")
        code, out = self.run_cli(*self.build_args(), dry_run=True)
        self.assertEqual(code, 0, out)

    def test_build_refuses_a_third_builder(self):
        p1, p2 = self.spawn_sleeper(), self.spawn_sleeper()
        self.write_slots({"b": {"pid": p1, "pr": "S", "step": "build"},
                          "c": {"pid": p2, "pr": "D", "step": "build"}})
        code, out = self.run_cli(*self.build_args(pr="E", slot="b", branch="feat/e"), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("two builders already alive", out)
        code, out = self.run_cli(*self.build_args(pr="E", slot="c", branch="feat/e"), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)

    def test_dead_pids_in_slots_do_not_count(self):
        p1 = self.spawn_sleeper(1)
        self.write_slots({"b": {"pid": p1, "pr": "S", "step": "build"},
                          "c": {"pid": 999999, "pr": "D", "step": "build"}})
        self.assertTrue(self.wait_dead(p1))
        code, out = self.run_cli(*self.build_args(pr="E", slot="b", branch="feat/e"), dry_run=True)
        self.assertEqual(code, 0, out)

    def test_build_refuses_busy_slot_but_allows_the_other(self):
        p1 = self.spawn_sleeper()
        self.write_slots({"b": {"pid": p1, "pr": "S", "step": "build"}})
        code, out = self.run_cli(*self.build_args(pr="E", slot="b", branch="feat/e"), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("slot b is busy", out)
        code, out = self.run_cli(*self.build_args(pr="E", slot="c", branch="feat/e"), dry_run=True)
        self.assertEqual(code, 0, out)

    def test_build_refuses_slot_not_logged_in(self):
        os.environ["FAKE_LOGGED_IN"] = "no"
        code, out = self.run_cli(*self.build_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("not logged in", out)

    def test_auth_check_uses_the_slots_config_dir(self):
        data = r.auth_status("c")
        self.assertEqual(data["configDirectory"], str(r.ACCOUNTS_ROOT / "c"))
        self.assertTrue(str(r.ACCOUNTS_ROOT).endswith("/.claude-accounts"))

    def test_auth_check_refuses_non_json(self):
        (self.bin / "claude").write_text("#!/bin/sh\necho not json\nexit 1\n")
        code, out = self.run_cli(*self.build_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("did not return JSON", out)

    def test_build_refuses_branch_already_on_origin(self):
        _git(self.repo, "push", "-q", "origin", "main:refs/heads/feat/taken")
        code, out = self.run_cli(*self.build_args(branch="feat/taken"), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("already exists on origin", out)

    def test_build_refuses_when_env_local_missing(self):
        (self.repo / ".env.local").unlink()
        code, out = self.run_cli(*self.build_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn(".env.local", out)

    def test_build_refuses_bad_slot_name(self):
        with self.assertRaises(SystemExit):
            self.run_cli(*self.build_args(slot="d"), dry_run=True)

    # ---- build dry run ------------------------------------------------------ #
    def test_build_dry_run_prints_the_plan_and_dispatches_nothing(self):
        code, out = self.run_cli(*self.build_args(cap="7.5"), dry_run=True)
        self.assertEqual(code, 0, out)
        self.assertIn("worktree add", out)
        self.assertIn("npm ci", out)
        self.assertIn("post-commit", out)
        self.assertIn("would launch", out)
        self.assertIn("--max-budget-usd 7.5", out)
        self.assertIn("nothing dispatched", out)
        self.assertFalse(self.worktree().exists())
        self.assertFalse((self.record / "argv").exists(), "the fake dispatcher ran")
        self.assertEqual(list(self.state.rglob("*.pid")), [])
        self.assertEqual(list(self.state.rglob("*.meta.json")), [])
        prompts = list(self.state.rglob("*.prompt.md"))
        self.assertEqual(len(prompts), 1)
        self.assertIn("Build the thing.", prompts[0].read_text())
        # nothing written outside the state dir + the throwaway repo's own files
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         sorted(["home", "state", "ledger.md", "record", "bin", "fake-dispatch", "dev", "brief.md"]))
        self.assertEqual(sorted(p.name for p in (self.root / "dev").iterdir()), ["RSDPM", "origin.git"])

    # ---- build for real (fake dispatcher) ----------------------------------- #
    def test_build_creates_worktree_hook_and_detached_builder(self):
        os.environ["FAKE_SLEEP"] = "3"
        code, out = self.run_cli(*self.build_args(cap="12"))
        self.assertEqual(code, 0, out)
        wt = self.worktree()
        self.assertTrue((wt / ".git").exists())
        self.assertEqual((wt / ".env.local").read_text(), "SECRET=1\n")
        self.assertTrue((wt / "e2e" / ".auth" / "state.json").is_file())
        self.assertIn("npm ci", (self.record / "npm.calls").read_text())
        # the builder is running, detached, with the right argv and stdin
        metas = list(self.state.rglob("*.meta.json"))
        self.assertEqual(len(metas), 1)
        meta = json.loads(metas[0].read_text())
        self.assertTrue(r.pid_alive(meta["pid"]))
        self.assertEqual(os.getpgid(meta["pid"]), meta["pid"], "not its own session leader")
        t0 = time.time()
        while not (self.record / "stdin").exists() and time.time() - t0 < 10:
            time.sleep(0.1)
        argv = (self.record / "argv").read_text().split("\n")
        self.assertEqual(argv[:2], ["b", "-p"])
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--verbose", argv)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "12.0")
        self.assertEqual(Path((self.record / "cwd").read_text().strip()).resolve(), wt.resolve())
        stdin = (self.record / "stdin").read_text()
        self.assertEqual(stdin, Path(meta["prompt"]).read_text())
        self.assertIn("Build the thing.", stdin)
        self.assertIn(str(wt), stdin)
        self.assertEqual(meta["argv"][:3], ["perl", "-e", "alarm shift; exec @ARGV"])
        self.assertEqual(meta["argv"][3], "60")
        slots = json.loads((self.state / "slots.json").read_text())
        self.assertEqual(slots["b"]["pid"], meta["pid"])
        # a second build on the same slot is refused while it runs
        code, out = self.run_cli(*self.build_args(pr="D", branch="feat/d"), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("slot b is busy", out)
        self.assertTrue(self.wait_dead(meta["pid"]))

    def test_build_refuses_when_worktree_already_exists(self):
        self.worktree().mkdir()
        code, out = self.run_cli(*self.build_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("worktree already exists", out)

    # ---- the post-commit push hook -------------------------------------- #
    def test_hook_pushes_worktree_commits_and_never_the_main_checkout(self):
        wt = self.worktree()
        _git(self.repo, "worktree", "add", "-q", str(wt), "-b", "feat/s", "origin/main")
        _git(wt, "config", "user.email", "t@example.com")
        _git(wt, "config", "user.name", "t")
        hook = r.install_post_commit_hook(wt)
        # the hook lives under the worktree's OWN gitdir, not the shared .git/hooks
        self.assertEqual(hook.resolve(), (self.repo / ".git" / "worktrees" / wt.name / "hooks" / "post-commit").resolve())
        self.assertFalse((self.repo / ".git" / "hooks" / "post-commit").exists())
        self.assertEqual(_git(wt, "config", "core.hooksPath").stdout.strip(), str(hook.parent))
        self.assertEqual(_git(self.repo, "config", "core.hooksPath", check=False).stdout.strip(), "")
        self.assertIsNone(_remote_sha(self.bare, "feat/s"))
        (wt / "a.txt").write_text("a\n")
        _git(wt, "add", "a.txt")
        _git(wt, "commit", "-q", "-m", "one")
        self.assertEqual(_remote_sha(self.bare, "feat/s"), _git(wt, "rev-parse", "HEAD").stdout.strip())
        (wt / "b.txt").write_text("b\n")
        _git(wt, "add", "b.txt")
        _git(wt, "commit", "-q", "-m", "two")
        self.assertEqual(_remote_sha(self.bare, "feat/s"), _git(wt, "rev-parse", "HEAD").stdout.strip())
        # negative control: a commit in the shared main checkout does NOT push
        before = _remote_sha(self.bare, "main")
        (self.repo / "c.txt").write_text("c\n")
        _git(self.repo, "add", "c.txt")
        _git(self.repo, "commit", "-q", "-m", "main-local")
        self.assertEqual(_remote_sha(self.bare, "main"), before)
        self.assertNotEqual(_git(self.repo, "rev-parse", "HEAD").stdout.strip(), before)

    def test_hook_refuses_to_install_on_the_main_checkout(self):
        with self.assertRaises(r.Refusal):
            r.install_post_commit_hook(self.repo)
        self.assertFalse((self.repo / ".git" / "hooks" / "post-commit").exists())

    # ---- the wall-clock breaker -------------------------------------------- #
    def test_wall_clock_kills_the_builder_and_watch_reports_it(self):
        os.environ["FAKE_SLEEP"] = "30"
        code, out = self.run_cli(*self.build_args(wall="1"))
        self.assertEqual(code, 0, out)
        meta = json.loads(next(self.state.rglob("*.meta.json")).read_text())
        self.assertTrue(self.wait_dead(meta["pid"], timeout=15), "perl alarm did not kill the run")
        code, out = self.run_cli("watch", "--pr", "S")
        self.assertEqual(code, r.EXIT_BUILDER_FAILED)
        self.assertIn("missing result line", out)
        self.assertIn("NO RESULT LINE", out)
        self.assertEqual(json.loads((self.state / "slots.json").read_text()), {})

    # ---- fix ------------------------------------------------------------- #
    def fix_args(self, pr="S", round_no="2", slot="b", cap="4", findings=None, decisions=None):
        findings = findings or FIXTURES / "findings-example.json"
        if decisions is None:
            decisions = self.root / "decisions.md"
            n = len(json.loads((FIXTURES / "findings-example.json").read_text())["findings"])
            decisions.write_text("".join("## F%d\nDECISION %d: fix it.\n\n" % (i, i) for i in range(1, n + 1)))
        return ["fix", "--pr", pr, "--round", round_no, "--findings", str(findings), "--decisions", str(decisions),
                "--slot", slot, "--cap", cap, "--claude-cmd", str(self.dispatch), "--wall-seconds", "60"]

    def test_fix_refuses_without_a_build_record(self):
        code, out = self.run_cli(*self.fix_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("no run recorded", out)

    def test_fix_refuses_missing_worktree(self):
        wt, _ = self.make_worktree_with_meta()
        _git(self.repo, "worktree", "remove", "--force", str(wt))
        code, out = self.run_cli(*self.fix_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("worktree missing", out)

    def test_fix_refuses_dirty_worktree(self):
        wt, _ = self.make_worktree_with_meta()
        (wt / "README.md").write_text("dirty\n")
        code, out = self.run_cli(*self.fix_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("dirty", out)

    def test_fix_refuses_unpushed_head(self):
        wt, _ = self.make_worktree_with_meta()
        (wt / "n.txt").write_text("n\n")
        _git(wt, "add", "n.txt")
        _git(wt, "commit", "-q", "-m", "local only")
        code, out = self.run_cli(*self.fix_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("!= origin/feat/s", out)

    def test_fix_refuses_when_branch_never_pushed(self):
        self.make_worktree_with_meta(push=False)
        code, out = self.run_cli(*self.fix_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("never pushed", out)

    def test_fix_refuses_a_finding_without_a_decision(self):
        self.make_worktree_with_meta()
        d = self.root / "d.md"
        d.write_text("## F1\nfix\n## F2\nfix\n")
        code, out = self.run_cli(*self.fix_args(decisions=d), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("no decision for finding(s) F3", out)

    def test_fix_refuses_a_decision_for_a_finding_that_does_not_exist(self):
        self.make_worktree_with_meta()
        d = self.root / "d.md"
        d.write_text("".join("## F%d\nfix\n" % i for i in range(1, 12)))
        code, out = self.run_cli(*self.fix_args(decisions=d), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("F11", out)

    def test_fix_refuses_an_empty_decision_and_a_duplicate(self):
        self.make_worktree_with_meta()
        d = self.root / "d.md"
        d.write_text("".join("## F%d\n%s\n" % (i, "" if i == 5 else "fix") for i in range(1, 11)))
        code, out = self.run_cli(*self.fix_args(decisions=d), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("F5", out)
        with self.assertRaises(r.Refusal):
            r.parse_decisions("## F1\na\n## F1\nb\n")

    def test_fix_refuses_a_non_findings_json(self):
        self.make_worktree_with_meta()
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"findings": [{"file": "x", "summary": "s"}]}))
        code, out = self.run_cli(*self.fix_args(findings=bad), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("failure_scenario", out)

    def test_fix_dry_run_composes_the_brief_verbatim_and_dispatches_nothing(self):
        wt, _ = self.make_worktree_with_meta()
        head = _git(wt, "rev-parse", "HEAD").stdout.strip()
        code, out = self.run_cli(*self.fix_args(round_no="2"), dry_run=True)
        self.assertEqual(code, 0, out)
        self.assertIn("nothing dispatched", out)
        self.assertFalse((self.record / "argv").exists())
        prompts = list(self.state.rglob("fix-round-2-*.prompt.md"))
        self.assertEqual(len(prompts), 1)
        text = prompts[0].read_text()
        findings = json.loads((FIXTURES / "findings-example.json").read_text())["findings"]
        for i, f in enumerate(findings, 1):
            self.assertIn("### r2-F%d — `%s:%s` [%s]" % (i, f["file"], f["line"], f["category"]), text)
            self.assertIn("**Finding:** " + f["summary"], text)
            self.assertIn("**Scenario:** " + f["failure_scenario"], text)
            self.assertIn("**DECISION:** DECISION %d: fix it." % i, text)
        for item in r.FIX_EVIDENCE_LIST:
            self.assertIn(item, text)
        self.assertIn("HEAD is `%s`" % head[:7], text)
        self.assertIn("FRESH headless builder on account slot b", text)
        self.assertIn("review round 2 → fix round 2", text)
        for line in r.wrapper_lines("b", wt, "feat/s"):
            self.assertIn(line, text.split("\n"))

    def test_fix_dispatches_in_the_existing_worktree(self):
        wt, _ = self.make_worktree_with_meta()
        os.environ["FAKE_SLEEP"] = "2"
        code, out = self.run_cli(*self.fix_args(round_no="1", cap="3"))
        self.assertEqual(code, 0, out)
        metas = sorted(self.state.rglob("*.meta.json"))
        meta = json.loads(metas[-1].read_text())
        self.assertEqual(meta["step"], "fix round 1")
        self.assertEqual(meta["round"], 1)
        t0 = time.time()
        while not (self.record / "cwd").exists() and time.time() - t0 < 10:
            time.sleep(0.1)
        self.assertEqual(Path((self.record / "cwd").read_text().strip()).resolve(), wt.resolve())
        self.assertTrue(self.wait_dead(meta["pid"]))

    def test_fix_guards_are_the_build_guards(self):
        self.make_worktree_with_meta()
        self.run_cli("stop")
        code, out = self.run_cli(*self.fix_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("STOP", out)
        self.run_cli("resume")
        os.environ["FAKE_LOGGED_IN"] = "no"
        code, out = self.run_cli(*self.fix_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("not logged in", out)
        os.environ.pop("FAKE_LOGGED_IN")
        p1, p2 = self.spawn_sleeper(), self.spawn_sleeper()
        self.write_slots({"b": {"pid": p1}, "c": {"pid": p2}})
        code, out = self.run_cli(*self.fix_args(), dry_run=True)
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("two builders", out)

    # ---- stream parsing + ledger --------------------------------------------- #
    def test_parse_stream_dedupes_per_message_usage_by_id(self):
        parsed = r.parse_stream(FIXTURES / "build-S.stream-sample.jsonl")
        self.assertEqual(parsed["messages"], 1, "three lines share one message.id")
        self.assertEqual(parsed["message_usage"]["cache_creation_input_tokens"], 64350)
        self.assertEqual(parsed["message_usage"]["output_tokens"], 2)
        self.assertAlmostEqual(parsed["result"]["total_cost_usd"], 28.651915, places=5)
        self.assertEqual(parsed["result"]["num_turns"], 179)
        self.assertEqual(parsed["bad_lines"], 0)

    def test_parse_stream_skips_bad_lines_and_takes_the_last_result(self):
        p = self.root / "s.jsonl"
        p.write_text('not json\n{"type":"result","subtype":"success","total_cost_usd":1}\n'
                     '{"type":"result","subtype":"error_max_budget_usd","total_cost_usd":2}\n')
        parsed = r.parse_stream(p)
        self.assertEqual(parsed["bad_lines"], 1)
        self.assertEqual(parsed["result"]["total_cost_usd"], 2)
        self.assertIn("error_max_budget_usd", r.result_failure(parsed))

    def test_ledger_row_reproduces_the_measured_rows(self):
        expect = {
            "build-S.stream-sample.jsonl": ("**$28.65**", "47m 35s", "44m 10s", "179", "2,852 / 547,379 / 29,986,860 / 203,582"),
            "fix1-S.stream-sample.jsonl": ("**$19.81**", "32m 33s", None, "104", "2,252 / 403,588 / 17,594,465 / 146,418"),
            "fix2-S.stream-sample.jsonl": ("**$13.36**", "21m 23s", None, "54", "1,666 / 293,357 / 10,394,706 / 97,554"),
            "fix3-S.stream-sample.jsonl": ("**$17.51**", "32m 58s", None, "83", "2,474 / 337,588 / 18,520,750 / 122,084"),
        }
        for name, (est, wall, api, turns, toks) in expect.items():
            cells = r.build_ledger_row(r.parse_stream(FIXTURES / name), "S", "step", "b", 80)
            self.assertEqual(len(cells), len(r.LEDGER_COLUMNS))
            self.assertEqual(cells[4], "claude-fable-5-1", name)
            self.assertEqual(cells[5], "$80.00")
            self.assertEqual(cells[6], est, name)
            self.assertEqual(cells[7], wall, name)
            if api:
                self.assertEqual(cells[8], api, name)
            self.assertEqual(cells[9], turns, name)
            self.assertEqual(cells[10], toks, name)
            self.assertIn("session ", cells[11])
        build = r.build_ledger_row(r.parse_stream(FIXTURES / "build-S.stream-sample.jsonl"), "S", "step")
        n_denials = len(json.loads([ln for ln in (FIXTURES / "build-S.stream-sample.jsonl").read_text().split("\n")
                                    if '"type":"result"' in ln][0])["permission_denials"])
        self.assertEqual(n_denials, 2)
        self.assertIn("2 denials", build[11], "the real build run had two permission denials")
        self.assertTrue(build[11].startswith("success"))

    def test_ledger_appends_one_row_in_place_and_refuses_a_second(self):
        before = self.ledger.read_text()
        code, out = self.run_cli("ledger", "--pr", "S", "--step", "fix round 9",
                                 "--stream", str(FIXTURES / "fix3-S.stream-sample.jsonl"))
        self.assertEqual(code, 0, out)
        after = self.ledger.read_text()
        b, a = before.split("\n"), after.split("\n")
        self.assertEqual(len(a), len(b) + 1)
        new = [ln for ln in a if ln not in b]
        self.assertEqual(len(new), 1)
        row = new[0]
        self.assertTrue(row.startswith("| 2026-09-30 | S | fix round 9 | ? | claude-fable-5-1 | ? | **$17.51** |"), row)
        # inserted directly after the last row of the runs table, before the notes
        self.assertEqual(a.index(row), b.index("") if False else a.index(row))
        idx = a.index(row)
        self.assertTrue(a[idx - 1].startswith("| 2026-09-30 | #283 (S, 0063) | review r3"))
        self.assertEqual(a[idx + 1], "")
        # every other line byte-identical, including the Daily table
        self.assertEqual([ln for ln in a if ln != row], b)
        # second filing of the same stream: refused, file untouched
        code, out = self.run_cli("ledger", "--pr", "S", "--step", "again",
                                 "--stream", str(FIXTURES / "fix3-S.stream-sample.jsonl"))
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("already in", out)
        self.assertEqual(self.ledger.read_text(), after)

    def test_ledger_dry_run_writes_nothing(self):
        before = self.ledger.read_text()
        code, out = self.run_cli("ledger", "--pr", "S", "--step", "x",
                                 "--stream", str(FIXTURES / "fix2-S.stream-sample.jsonl"), dry_run=True)
        self.assertEqual(code, 0, out)
        self.assertIn("**$13.36**", out)
        self.assertEqual(self.ledger.read_text(), before)

    def test_ledger_refuses_a_changed_column_order(self):
        txt = self.ledger.read_text().replace("| date | PR | step | slot |", "| date | PR | step | model |", 1)
        self.ledger.write_text(txt)
        code, out = self.run_cli("ledger", "--pr", "S", "--step", "x",
                                 "--stream", str(FIXTURES / "fix2-S.stream-sample.jsonl"))
        self.assertEqual(code, r.EXIT_REFUSED)
        self.assertIn("columns changed", out)
        self.assertEqual(self.ledger.read_text(), txt)

    def test_ledger_reads_slot_and_cap_from_the_run_meta(self):
        rd = self.state / "runs" / "S"
        rd.mkdir(parents=True)
        shutil.copy(FIXTURES / "fix1-S.stream-sample.jsonl", rd / "fix-round-1-x.stream.jsonl")
        (rd / "fix-round-1-x.meta.json").write_text(json.dumps({"slot": "c", "cap": 50}))
        code, out = self.run_cli("ledger", "--pr", "S", "--step", "fix round 1",
                                 "--stream", str(rd / "fix-round-1-x.stream.jsonl"))
        self.assertEqual(code, 0, out)
        self.assertIn("| c | claude-fable-5-1 | $50.00 | **$19.81** |", out)

    def test_ledger_row_for_a_missing_result_line(self):
        p = self.root / "cut.jsonl"
        lines = (FIXTURES / "build-S.stream-sample.jsonl").read_text().split("\n")
        p.write_text("\n".join(ln for ln in lines if '"type":"result"' not in ln))
        code, out = self.run_cli("ledger", "--pr", "S", "--step", "build", "--stream", str(p))
        self.assertEqual(code, 0, out)
        self.assertIn("NO RESULT LINE", out)
        self.assertIn("| — | — | — | — | 2 / 64,350 / 0 / 2 |", out)
        self.assertIn("NOTE: missing result line", out)

    # ---- classify ------------------------------------------------------------ #
    def test_classify_prefills_from_category_and_decides_nothing(self):
        code, out = self.run_cli("classify", "--findings", str(FIXTURES / "findings-example.json"))
        self.assertEqual(code, 0, out)
        rows = [ln for ln in out.split("\n") if ln.startswith("| F")]
        self.assertEqual(len(rows), 10)
        self.assertIn("| F1 | components/ui/StatusControl.tsx:320 | correctness | reachable? |", out)
        self.assertIn("| F4 | supabase/migrations/0063_lifecycle_substrate.sql:293 | altitude | latent? |", out)
        self.assertIn("| F5 | .github/workflows/test.yml:256 | stale-claim | latent? |", out)
        self.assertIn("| F8 | supabase/migrations/0063_lifecycle_substrate.sql:358 | data-flow | latent? |", out)
        for row in rows:
            sev = [c.strip() for c in row.strip("|").split("|")][3]
            self.assertTrue(sev.endswith("?"), "classify emitted a verdict: %r" % row)
        self.assertIn(r.STOP_RULE_SENTENCE, out)
        self.assertIn("zero REACHABLE wrong-behaviour findings = stop reviewing, manager check, merge; three rounds is the ceiling", out)
        table = r.classify_table([{"file": "a", "line": 1, "summary": "s", "failure_scenario": "f", "category": "weird"}])
        self.assertEqual(table[0][3], "?")
        self.assertEqual(set(r.CATEGORY_PREFILL), {"correctness", "security", "test-coverage", "efficiency",
                                                   "simplification", "conventions", "stale-claim", "reuse",
                                                   "altitude", "accessibility", "data-flow"})

    # ---- watch --------------------------------------------------------------- #
    def test_watch_once_reports_a_live_run_then_files_the_ledger_on_exit(self):
        os.environ["FAKE_SLEEP"] = "3"
        os.environ["FAKE_STREAM"] = str(FIXTURES / "fix2-S.stream-sample.jsonl")
        os.environ["FAKE_GH_JSON"] = json.dumps([{"number": 283, "state": "OPEN", "comments": [{}, {}], "url": "u"}])
        code, out = self.run_cli(*self.build_args(cap="40"))
        self.assertEqual(code, 0, out)
        meta = json.loads(next(self.state.rglob("*.meta.json")).read_text())
        code, out = self.run_cli("watch", "--pr", "S", "--once")
        self.assertEqual(code, 0, out)
        self.assertIn("alive", out)
        self.assertIn("LOCAL COMMIT", out)
        self.assertIn("PR #283 OPEN", out)
        self.assertIn("COMMENTS 2", out)
        self.assertIn("pr list --head feat/s", (self.record / "gh.calls").read_text())
        self.assertNotIn("| S | build |", self.ledger.read_text(), "--once must not file the row")
        self.assertTrue(self.wait_dead(meta["pid"]))
        before = self.ledger.read_text()
        code, out = self.run_cli("watch", "--pr", "S")
        self.assertEqual(code, 0, out)
        self.assertIn("EXITED", out)
        self.assertIn("LEDGER ROW:", out)
        self.assertIn("| 2026-09-30 | S | build | b | claude-fable-5-1 | $40.00 | **$13.36** | 21m 23s | 20m 21s | 54 |", out)
        self.assertIn("DONE: success", out)
        self.assertEqual(len(self.ledger.read_text().split("\n")), len(before.split("\n")) + 1)
        self.assertEqual(json.loads((self.state / "slots.json").read_text()), {})
        # a second watch after exit does not file a second row
        code, out = self.run_cli("watch", "--pr", "S")
        self.assertEqual(code, 0, out)
        self.assertIn("already filed", out)
        self.assertEqual(len(self.ledger.read_text().split("\n")), len(before.split("\n")) + 1)

    def test_watch_exits_non_zero_on_budget_stop_and_is_error(self):
        for subtype, is_error in (("error_max_budget_usd", False), ("success", True)):
            shutil.rmtree(self.state, ignore_errors=True)
            with contextlib.suppress(FileNotFoundError):
                (self.record / "argv").unlink()
            wt = self.worktree()
            if wt.exists():
                _git(self.repo, "worktree", "remove", "--force", str(wt))
                _git(self.repo, "branch", "-D", "feat/s")
            stream = self.root / ("%s.jsonl" % subtype)
            stream.write_text(json.dumps({"type": "system", "subtype": "init", "session_id": "abc12345xyz", "model": "m"}) + "\n"
                              + json.dumps({"type": "result", "subtype": subtype, "is_error": is_error, "total_cost_usd": 40.0,
                                            "duration_ms": 1000, "duration_api_ms": 900, "num_turns": 3, "usage": {},
                                            "session_id": "abc12345xyz" + subtype}) + "\n")
            os.environ["FAKE_STREAM"] = str(stream)
            code, out = self.run_cli(*self.build_args(cap="40"))
            self.assertEqual(code, 0, out)
            meta = json.loads(next(self.state.rglob("*.meta.json")).read_text())
            self.assertTrue(self.wait_dead(meta["pid"]))
            code, out = self.run_cli("watch", "--pr", "S")
            self.assertEqual(code, r.EXIT_BUILDER_FAILED, out)
            self.assertIn("FAILED", out)
            self.assertIn(subtype if not is_error else "is_error", out)
            self.assertIn("**$40.00**", self.ledger.read_text())

    def test_watch_refuses_unknown_pr(self):
        code, out = self.run_cli("watch", "--pr", "Z", "--once")
        self.assertEqual(code, r.EXIT_REFUSED)

    def test_poll_seconds_is_thirty(self):
        self.assertEqual(r.POLL_SECONDS, 30)
        src = (_SCRIPTS / "rsdpm_runner.py").read_text()
        self.assertEqual(src.count("time.sleep("), 1)
        self.assertIn("time.sleep(POLL_SECONDS)", src)

    # ---- stop / resume / status / daily ------------------------------------- #
    def test_stop_and_resume(self):
        code, out = self.run_cli("stop")
        self.assertEqual(code, 0)
        self.assertTrue((self.state / "STOP").exists())
        code, out = self.run_cli("status")
        self.assertIn("STOP: PRESENT", out)
        code, out = self.run_cli("daily", "--repo", str(self.repo))
        self.assertIn("paused/stopped: STOPPED", out)
        code, out = self.run_cli("resume")
        self.assertFalse((self.state / "STOP").exists())
        code, out = self.run_cli("resume")
        self.assertEqual(code, 0)

    def test_status_lists_runs_slots_and_last_rows(self):
        p1 = self.spawn_sleeper()
        self.write_slots({"b": {"pid": p1, "pr": "S", "step": "build"}})
        rd = self.state / "runs" / "S"
        rd.mkdir(parents=True)
        (rd / "build-x.meta.json").write_text(json.dumps({"pr": "S", "step": "build", "slot": "b", "pid": p1,
                                                          "started_at": "2026-09-30T10:00:00"}))
        code, out = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertIn("STOP: absent", out)
        self.assertRegex(out, r"PR S\s+build\s+slot b pid %d\s+alive" % p1)
        self.assertIn("b: pid %d alive — PR S build" % p1, out)
        self.assertIn("c: free", out)
        self.assertIn("#283 (S, 0063) | 2026-09-30 | review r3", out)

    def test_daily_line(self):
        os.environ["FAKE_GH_JSON"] = json.dumps([{"number": 283}, {"number": 284}])
        today = r._today()
        txt = self.ledger.read_text().replace("2026-09-30", today)
        self.ledger.write_text(txt)
        code, out = self.run_cli("daily", "--repo", str(self.repo))
        self.assertEqual(code, 0, out)
        self.assertIn("PRs merged: 2", out)
        self.assertIn("spare-slot spend: $79.33 (4 runs)", out)
        self.assertIn("primary reviews: 3", out)
        self.assertIn("paused/stopped: none", out)
        self.assertIn("--search merged:>=%s" % today, (self.record / "gh.calls").read_text())

    # ---- formatting ---------------------------------------------------------- #
    def test_formatters(self):
        self.assertEqual(r.fmt_dur(2854509), "47m 35s")
        self.assertEqual(r.fmt_dur(3725000), "1h 02m 05s")
        self.assertEqual(r.fmt_dur(None), "—")
        self.assertEqual(r.fmt_int(29986860), "29,986,860")
        self.assertEqual(r.fmt_usd(28.651915), "$28.65")


if __name__ == "__main__":
    unittest.main()
