#!/usr/bin/env python3
"""Tests for sync_agent_core.sh's Pulse-runtime auto-commit + push path.

Closes the 2026-05-28 iter-98 incident class: an interactive Pulse /cycle
leaves runbooks/cycle-journal.md, runbooks/cycle-actions.jsonl, or pulse
MEMORY files uncommitted, and sync refuses for hours. Sync now auto-commits
+ pushes when the dirty set is fully inside the auto-commit allowlist
(SYNC_AUTOCOMMIT_PATHS) defined in scripts/_lib_pulse_runtime.sh.

The 2026-06-10 sibling: agents/beacon/captures.json is machine-owned
missions-capture state, written by the ingest endpoint and committed every
~10min by heal_missions_card_gc.py. The hourly sync tick can land between the
write and that commit and refuse-and-page on a purely machine-owned file. It
is now part of SYNC_AUTOCOMMIT_PATHS, so sync absorbs that race the same way
it does Pulse runtime dirt (see CapturesJsonAutoCommitTest below).

The behavior under test:
  - allowlist-only dirt → auto-commit + push, sync proceeds.
  - mixed dirt (allowlist + something else)  → existing refuse-and-alert
    path unchanged, no auto-commit.
  - push fails (remote rejects)              → hard-reset the local commit so
    no local-only commit lingers on main; emit a sync-blocked envelope so
    Larry sees it.
  - fixture-pattern token in staged diff     → refuse auto-commit, alert.

Each subtest invokes the real sync_agent_core.sh in a tmpdir-rooted fake
agent-core repo with a local bare repo as origin. HOME / REPO_DIR / LIVE_ROOT
/ STAGING_ROOT / BACKUP_ROOT are all redirected via env vars; no writes to
the real ~/agents.

Run:
    python3 -m unittest scripts.tests.test_sync_agent_core_pulse_runtime
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SYNC_SCRIPT = _REPO_ROOT / 'scripts' / 'sync_agent_core.sh'
_LARRY_ALERTS = _REPO_ROOT / 'scripts' / 'larry_alerts.py'
_LIB_PULSE = _REPO_ROOT / 'scripts' / '_lib_pulse_runtime.sh'
# sync_agent_core.sh sources this for the push rebase-fallback (c0e238e); the
# fake scripts dir must carry it or every run dies at the `source` line.
_LIB_PUSH = _REPO_ROOT / 'scripts' / '_lib_push_with_rebase.sh'


class _SyncResilienceBase(unittest.TestCase):
    """Builds a fake agent-core repo wired to a local bare origin so the
    auto-commit + push path exercises real git operations."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.home = self.tmp_path / 'home'
        self.home.mkdir()
        self.repo_dir = self.tmp_path / 'agent-core'
        self.repo_dir.mkdir()
        self.scripts_dir = self.repo_dir / 'scripts'
        self.scripts_dir.mkdir()
        # Copy the scripts the test exercises.
        (self.scripts_dir / 'sync_agent_core.sh').write_bytes(
            _SYNC_SCRIPT.read_bytes(),
        )
        (self.scripts_dir / 'larry_alerts.py').write_bytes(
            _LARRY_ALERTS.read_bytes(),
        )
        (self.scripts_dir / '_lib_pulse_runtime.sh').write_bytes(
            _LIB_PULSE.read_bytes(),
        )
        (self.scripts_dir / '_lib_push_with_rebase.sh').write_bytes(
            _LIB_PUSH.read_bytes(),
        )
        os.chmod(self.scripts_dir / 'sync_agent_core.sh', 0o755)

        self._git('init', '-q', '--initial-branch=main')
        self._git('config', 'user.email', 'test@example.com')
        self._git('config', 'user.name', 'Test')

        # Seed the Pulse runtime files in tracked state so subsequent
        # modifications register as tracked-modified rather than untracked.
        runbooks = self.repo_dir / 'runbooks'
        runbooks.mkdir()
        (runbooks / 'cycle-journal.md').write_text('seed journal\n')
        (runbooks / 'cycle-actions.jsonl').write_text('')
        pulse_dir = self.repo_dir / 'agents' / 'pulse'
        pulse_dir.mkdir(parents=True)
        (pulse_dir / 'MEMORY.md').write_text('seed memory\n')
        (pulse_dir / 'memory').mkdir()
        (pulse_dir / 'memory' / '.gitkeep').write_text('')
        # Beacon captures.json: machine-owned missions-capture state, normally
        # committed by heal_missions_card_gc.py. Seed it tracked so a later
        # modification registers as tracked-modified (the sync-only extra in
        # SYNC_AUTOCOMMIT_PATHS).
        beacon_dir = self.repo_dir / 'agents' / 'beacon'
        beacon_dir.mkdir(parents=True)
        (beacon_dir / 'captures.json').write_text('{"captures": []}\n')
        (self.repo_dir / 'README').write_text('seed\n')
        self._git('add', '-A')
        self._git('commit', '-q', '-m', 'seed')

        # Set up a local bare origin and push the seed so fetch + push work.
        self.origin = self.tmp_path / 'origin.git'
        subprocess.run(
            ['git', 'init', '--bare', '-q', '--initial-branch=main',
             str(self.origin)],
            check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._git('remote', 'add', 'origin', f'file://{self.origin}')
        self._git('push', '-q', 'origin', 'main')
        # Track upstream so push without args works in case anything relies
        # on it (sync's push is explicit, but be safe).
        self._git('branch', '--set-upstream-to=origin/main', 'main')

        self.live_root = self.tmp_path / 'agents'
        self.staging_root = self.live_root / '.sync-staging'
        self.backup_root = self.live_root / '.sync-backup'
        self.live_root.mkdir()
        (self.live_root / 'blackboard').mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, *args):
        subprocess.run(
            ['git', *args],
            cwd=self.repo_dir, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _run_sync(self):
        env = os.environ.copy()
        env['HOME'] = str(self.home)
        env['REPO_DIR'] = str(self.repo_dir)
        env['LIVE_ROOT'] = str(self.live_root)
        env['STAGING_ROOT'] = str(self.staging_root)
        env['BACKUP_ROOT'] = str(self.backup_root)
        return subprocess.run(
            ['bash', str(self.scripts_dir / 'sync_agent_core.sh')],
            env=env, cwd=self.repo_dir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30,
        )

    def _read_alerts(self):
        alerts_file = (
            self.home / 'agents' / 'blackboard' / 'larry-alerts.jsonl'
        )
        if not alerts_file.exists():
            return []
        out = []
        for line in alerts_file.read_text().splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def _head(self) -> str:
        return subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=self.repo_dir, check=True, stdout=subprocess.PIPE,
        ).stdout.decode().strip()

    def _origin_head(self) -> str:
        return subprocess.run(
            ['git', '--git-dir', str(self.origin), 'rev-parse', 'main'],
            check=True, stdout=subprocess.PIPE,
        ).stdout.decode().strip()


class AllowlistOnlyDirtAutoCommitsTest(_SyncResilienceBase):
    """Allowlist-only dirt: sync auto-commits + pushes, then proceeds."""

    def test_journal_only_dirt_is_auto_committed_and_pushed(self):
        pre_head = self._head()
        # Dirty exactly cycle-journal.md (tracked-modified).
        (self.repo_dir / 'runbooks' / 'cycle-journal.md').write_text(
            'seed journal\nincremented by interactive cycle\n',
        )

        result = self._run_sync()
        self.assertEqual(
            result.returncode, 0,
            f'sync should succeed; stdout={result.stdout!r} '
            f'stderr={result.stderr!r}',
        )
        # Tree is clean.
        status = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=self.repo_dir, check=True, stdout=subprocess.PIPE,
        ).stdout.decode()
        self.assertEqual(status.strip(), '')
        # A new commit was made and pushed.
        new_head = self._head()
        self.assertNotEqual(new_head, pre_head)
        self.assertEqual(new_head, self._origin_head())
        # No sync-blocked alert envelope was emitted.
        alerts = self._read_alerts()
        sync_blocked = [
            a for a in alerts if a.get('subject', '').startswith('sync-blocked')
        ]
        self.assertEqual(sync_blocked, [], f'unexpected alerts: {alerts}')

    def test_all_four_allowlist_files_dirty_together_auto_committed(self):
        pre_head = self._head()
        (self.repo_dir / 'runbooks' / 'cycle-journal.md').write_text(
            'updated\n',
        )
        (self.repo_dir / 'runbooks' / 'cycle-actions.jsonl').write_text(
            '{"event": "cycle"}\n',
        )
        (self.repo_dir / 'agents' / 'pulse' / 'MEMORY.md').write_text(
            'updated memory\n',
        )
        (self.repo_dir / 'agents' / 'pulse' / 'memory' / 'note.md').write_text(
            'staged memory note\n',
        )
        # The new file under agents/pulse/memory/ is untracked; sync's
        # `git add -- <allowlist paths>` will stage it.
        self._git('add', 'agents/pulse/memory/note.md')

        result = self._run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        new_head = self._head()
        self.assertNotEqual(new_head, pre_head)
        self.assertEqual(new_head, self._origin_head())


class MixedDirtStillBlocksTest(_SyncResilienceBase):
    """Mixed dirt: refuse exactly as before, emit the unchanged alert."""

    def test_journal_plus_unrelated_file_still_blocks(self):
        pre_head = self._head()
        (self.repo_dir / 'runbooks' / 'cycle-journal.md').write_text(
            'allowlist dirt\n',
        )
        # Non-allowlist tracked-modified.
        (self.repo_dir / 'README').write_text('non-allowlist dirt\n')

        result = self._run_sync()
        self.assertNotEqual(result.returncode, 0)
        # No commit landed.
        self.assertEqual(self._head(), pre_head)
        # Existing uncommitted-changes alert still fires unchanged.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1, f'expected 1 alert, got {alerts}')
        self.assertEqual(
            alerts[0]['subject'], 'sync-blocked:uncommitted-changes',
        )


class PushFailureRollsBackTest(_SyncResilienceBase):
    """Push failure: hard-reset to pre-commit HEAD, emit specific alert."""

    def test_push_failure_resets_and_alerts(self):
        # Install a pre-receive hook on the bare origin that rejects all
        # pushes. The auto-commit will succeed locally; the push will fail;
        # sync_agent_core.sh must roll back so no local-only commit lingers.
        hooks_dir = self.origin / 'hooks'
        hooks_dir.mkdir(exist_ok=True)
        hook = hooks_dir / 'pre-receive'
        hook.write_text('#!/bin/sh\necho rejected by test hook >&2\nexit 1\n')
        os.chmod(hook, 0o755)

        pre_head = self._head()
        (self.repo_dir / 'runbooks' / 'cycle-journal.md').write_text(
            'dirt that will trigger auto-commit\n',
        )

        result = self._run_sync()
        self.assertNotEqual(result.returncode, 0)
        # Local HEAD rolled back to pre-auto-commit HEAD: no local-only
        # commit that would break future fast-forward.
        self.assertEqual(self._head(), pre_head)
        # Origin is unchanged.
        self.assertEqual(self._origin_head(), pre_head)
        # Specific alert envelope on the queue.
        alerts = self._read_alerts()
        subjects = [a['subject'] for a in alerts]
        self.assertIn('sync-blocked:auto-commit-push-failed', subjects)


class FixturePatternGuardTest(_SyncResilienceBase):
    """Fixture-pattern tokens no longer block the Pulse auto-commit path.

    afe9d07 removed the fixture-token guard from sync_agent_core.sh's
    auto-commit path: cycle-journal.md / cycle-actions.jsonl are append-only
    observability that legitimately narrate fixture triage in prose, so the
    guard false-positived and blocked sync for 11+ consecutive cycles during
    the 2026-05-29 incident. Allowlist-only dirt now auto-commits even when a
    fixture-pattern token appears in the diff. Fixture-replay protection moved
    to the dispatch boundary (scripts/fixture_patterns.py, PR #170) and is
    regression-covered by tests/test_inbox_watcher_fixture_gate.py — see that
    suite for the live enforcement; it is intentionally NOT re-asserted here.
    """

    def test_fixture_token_in_journal_is_auto_committed(self):
        pre_head = self._head()
        # A fixture-pattern token in an allowlist file used to trip the
        # removed guard; sync now auto-commits it (guard moved to dispatch).
        (self.repo_dir / 'runbooks' / 'cycle-actions.jsonl').write_text(
            '{"task_id":"t-fail","event":"phantom"}\n',
        )

        result = self._run_sync()
        self.assertEqual(
            result.returncode, 0,
            f'sync should auto-commit allowlist dirt; stdout={result.stdout!r} '
            f'stderr={result.stderr!r}',
        )
        # A new commit landed and was pushed; no fixture-pattern block.
        new_head = self._head()
        self.assertNotEqual(new_head, pre_head)
        self.assertEqual(new_head, self._origin_head())
        alerts = self._read_alerts()
        subjects = [a['subject'] for a in alerts]
        self.assertNotIn('sync-blocked:fixture-pattern-detected', subjects)
        self.assertEqual(
            [s for s in subjects if s.startswith('sync-blocked')], [],
            f'unexpected sync-blocked alerts: {alerts}',
        )


class CapturesJsonAutoCommitTest(_SyncResilienceBase):
    """agents/beacon/captures.json is a sync-only auto-commit extra: machine-
    owned missions state that the GC healer commits on a 10min cadence. Sync
    must absorb the write↔commit race instead of refusing and paging Larry."""

    def test_captures_json_only_dirt_is_auto_committed_and_pushed(self):
        pre_head = self._head()
        # Dirty exactly captures.json (tracked-modified), simulating the gap
        # between an ingest write and the GC healer's next commit tick.
        (self.repo_dir / 'agents' / 'beacon' / 'captures.json').write_text(
            '{"captures": [{"title": "note", "origin": "desktop"}]}\n',
        )

        result = self._run_sync()
        self.assertEqual(
            result.returncode, 0,
            f'sync should auto-commit captures.json; stdout={result.stdout!r} '
            f'stderr={result.stderr!r}',
        )
        status = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=self.repo_dir, check=True, stdout=subprocess.PIPE,
        ).stdout.decode()
        self.assertEqual(status.strip(), '')
        new_head = self._head()
        self.assertNotEqual(new_head, pre_head)
        self.assertEqual(new_head, self._origin_head())
        alerts = self._read_alerts()
        sync_blocked = [
            a for a in alerts if a.get('subject', '').startswith('sync-blocked')
        ]
        self.assertEqual(sync_blocked, [], f'unexpected alerts: {alerts}')

    def test_captures_json_plus_pulse_dirt_auto_committed_together(self):
        # captures.json + a Pulse runtime file are both inside
        # SYNC_AUTOCOMMIT_PATHS, so the mixed-but-all-allowlisted set still
        # auto-commits in one shot.
        pre_head = self._head()
        (self.repo_dir / 'agents' / 'beacon' / 'captures.json').write_text(
            '{"captures": [{"title": "a"}]}\n',
        )
        (self.repo_dir / 'runbooks' / 'cycle-journal.md').write_text(
            'seed journal\ncycle line\n',
        )

        result = self._run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=self.repo_dir, check=True, stdout=subprocess.PIPE,
            ).stdout.decode().strip(),
            '',
        )
        new_head = self._head()
        self.assertNotEqual(new_head, pre_head)
        self.assertEqual(new_head, self._origin_head())

    def test_captures_json_plus_non_allowlist_file_still_blocks(self):
        # captures.json (allowlisted) + README (not) is still mixed dirt: the
        # refuse-and-alert path is unchanged so genuine human dirt never gets
        # silently swept into an auto-commit.
        pre_head = self._head()
        (self.repo_dir / 'agents' / 'beacon' / 'captures.json').write_text(
            '{"captures": [{"title": "b"}]}\n',
        )
        (self.repo_dir / 'README').write_text('human edit\n')

        result = self._run_sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._head(), pre_head)
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1, f'expected 1 alert, got {alerts}')
        self.assertEqual(
            alerts[0]['subject'], 'sync-blocked:uncommitted-changes',
        )


if __name__ == '__main__':
    unittest.main()
