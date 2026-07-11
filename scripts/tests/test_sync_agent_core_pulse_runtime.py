#!/usr/bin/env python3
"""Tests for sync_agent_core.sh's Pulse-runtime auto-commit + push path.

Closes the 2026-05-28 iter-98 incident class: an interactive Pulse /cycle
leaves runbooks/cycle-journal.md, runbooks/cycle-actions.jsonl, or pulse
MEMORY files uncommitted, and sync refuses for hours. Sync now auto-commits
+ pushes when the dirty set is fully inside the auto-commit allowlist
(SYNC_AUTOCOMMIT_PATHS) defined in scripts/_lib_pulse_runtime.sh.

The 2026-06-10 sibling, refined by the #409 follow-up: agents/beacon/captures.json
is machine-owned missions-capture state, written by the ingest endpoint and
committed every ~10min by heal_missions_card_gc.py — its SOLE committer. The
hourly sync tick can land between the write and that commit; sync must not
refuse-and-page, but it also must NOT become a second committer (#409 did, which
created a dual-committer race and a hard-reset data-loss window). Sync now
TOLERATES captures.json dirt: it neither commits nor resets it and proceeds to
the ff-pull, leaving the file to the healer (see CapturesJsonToleratedTest).

The behavior under test:
  - Pulse-runtime-only dirt → auto-commit + push, sync proceeds.
  - captures.json-only dirt → tolerated: NO commit, NO push, NO page; the dirt
    is left on disk for the GC healer; sync proceeds.
  - Pulse + captures.json dirt → commit the Pulse files ONLY (captures.json
    stays dirty), proceed.
  - mixed dirt (allowlist + something else)  → existing refuse-and-alert
    path unchanged, no auto-commit.
  - Pulse push fails (remote rejects)        → roll the Pulse paths back so no
    local-only commit lingers on main (captures.json, if dirty, is preserved);
    emit a sync-blocked envelope so Larry sees it.
  - fixture-pattern token in staged diff     → refuse auto-commit, alert.

Each subtest invokes the real sync_agent_core.sh in a tmpdir-rooted fake
agent-core repo with a local bare repo as origin. HOME / REPO_DIR / LIVE_ROOT
/ STAGING_ROOT / BACKUP_ROOT are all redirected via env vars; no writes to
the real ~/agents.

Run:
    python3 -m unittest scripts.tests.test_sync_agent_core_pulse_runtime
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

try:  # dotted/pytest path: relative import within the scripts.tests package
    from ._runtime_script_test_support import (
        copy_larry_alerts_cli,
        install_timeout_shim,
        scrub_run_sentinel,
        redirect_agents_root,
    )
except ImportError:  # discover loads this module top-level (no package parent)
    from _runtime_script_test_support import (
        copy_larry_alerts_cli,
        install_timeout_shim,
        scrub_run_sentinel,
        redirect_agents_root,
    )

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SYNC_SCRIPT = _REPO_ROOT / 'scripts' / 'sync_agent_core.sh'
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
        # larry_alerts.py + its sibling-module deps (single source of truth).
        copy_larry_alerts_cli(self.scripts_dir)
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
        # journal-archive/ is a PULSE_RUNTIME_PATHS member (rotate_cycle_journal.py
        # overflow). Seed its tracked .gitkeep so sync's atomic
        # `git add -- <allowlist paths>` never aborts on a missing pathspec —
        # same treatment as agents/pulse/memory/ above.
        (runbooks / 'journal-archive').mkdir()
        (runbooks / 'journal-archive' / '.gitkeep').write_text('')
        pulse_dir = self.repo_dir / 'agents' / 'pulse'
        pulse_dir.mkdir(parents=True)
        (pulse_dir / 'MEMORY.md').write_text('seed memory\n')
        (pulse_dir / 'memory').mkdir()
        (pulse_dir / 'memory' / '.gitkeep').write_text('')
        # Pulse's interactive Claude Code session config — a PULSE_RUNTIME_PATHS
        # member. Seed it tracked so sync's atomic `git add -- <allowlist paths>`
        # finds it; an absent path would abort the whole add and stage nothing.
        claude_dir = pulse_dir / '.claude'
        claude_dir.mkdir()
        (claude_dir / 'settings.json').write_text('{"permissions": {}}\n')
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

        # sync_agent_core.sh wraps its alert CLI calls in `timeout 10 …`; shim it
        # on PATH so the alert-asserting subtests run where GNU `timeout` is
        # absent (macOS). See _runtime_script_test_support.
        self.shim_bin = self.tmp_path / 'bin'
        self.shim_bin.mkdir()
        install_timeout_shim(self.shim_bin)

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
        env['PATH'] = f"{self.shim_bin}{os.pathsep}{env.get('PATH', '')}"
        # The fake tree is HOME-sandboxed, so let the script's larry_alerts
        # subprocess emit for real (subprocess equivalent of
        # test_isolation_guard.allow()); without this the inherited run sentinel
        # makes the choke guard raise and the alert is silently dropped.
        scrub_run_sentinel(env)
        redirect_agents_root(env)
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
    """Mixed dirt: refuse exactly as before. The uncommitted-changes alert now
    fires only when the dirt persists across two consecutive ticks (one-tick
    grace), so two runs are needed to observe it."""

    def test_journal_plus_unrelated_file_still_blocks(self):
        pre_head = self._head()
        (self.repo_dir / 'runbooks' / 'cycle-journal.md').write_text(
            'allowlist dirt\n',
        )
        # Non-allowlist tracked-modified.
        (self.repo_dir / 'README').write_text('non-allowlist dirt\n')

        # First tick: refuse (non-zero), but defer the alert (one-tick grace).
        first = self._run_sync()
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(self._head(), pre_head)
        self.assertEqual(
            self._read_alerts(), [],
            'first dirty tick must defer the alert (one-tick grace)',
        )

        # Second consecutive dirty tick: now the uncommitted-changes alert fires.
        second = self._run_sync()
        self.assertNotEqual(second.returncode, 0)
        # Still no commit landed.
        self.assertEqual(self._head(), pre_head)
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1, f'expected 1 alert, got {alerts}')
        self.assertEqual(
            alerts[0]['subject'], 'sync-blocked:uncommitted-changes',
        )


class PushFailureRollsBackTest(_SyncResilienceBase):
    """Push failure: roll the Pulse paths back to pre-commit HEAD (so no
    local-only commit lingers), emit the specific alert."""

    def test_push_failure_resets_and_alerts(self):
        # Install a pre-receive hook on the bare origin that rejects all
        # pushes. The Pulse auto-commit will succeed locally; the push will
        # fail; sync_agent_core.sh must roll back so no local-only commit
        # lingers (CapturesJsonToleratedTest covers the captures-preserving
        # half of the rollback).
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


class CapturesJsonToleratedTest(_SyncResilienceBase):
    """agents/beacon/captures.json is healer-owned: heal_missions_card_gc.py is
    its SOLE committer. Sync must absorb the write↔commit race by TOLERATING the
    dirt (not committing it) — it neither commits nor resets captures.json, so
    there is no dual-committer race on origin/main and no hard-reset data-loss
    window (#409 follow-up)."""

    CAPTURES_REL = 'agents/beacon/captures.json'

    def _captures_dirty(self) -> bool:
        rc = subprocess.run(
            ['git', 'diff', '--quiet', '--', self.CAPTURES_REL],
            cwd=self.repo_dir,
        ).returncode
        return rc != 0

    def test_captures_json_only_dirt_is_tolerated_not_committed(self):
        pre_head = self._head()
        pre_origin = self._origin_head()
        # Dirty exactly captures.json (tracked-modified), simulating the gap
        # between an ingest write and the GC healer's next commit tick.
        (self.repo_dir / 'agents' / 'beacon' / 'captures.json').write_text(
            '{"captures": [{"title": "note", "origin": "desktop"}]}\n',
        )

        result = self._run_sync()
        self.assertEqual(
            result.returncode, 0,
            f'sync should tolerate captures.json and proceed; '
            f'stdout={result.stdout!r} stderr={result.stderr!r}',
        )
        # Sync did NOT commit or push: HEAD and origin are unchanged.
        self.assertEqual(self._head(), pre_head)
        self.assertEqual(self._origin_head(), pre_origin)
        # The dirt is left on disk for the healer.
        self.assertTrue(
            self._captures_dirty(),
            'captures.json should still be dirty (left for the GC healer)',
        )
        # No page of any kind.
        alerts = self._read_alerts()
        sync_blocked = [
            a for a in alerts if a.get('subject', '').startswith('sync-blocked')
        ]
        self.assertEqual(sync_blocked, [], f'unexpected alerts: {alerts}')

    def test_captures_json_plus_pulse_dirt_commits_pulse_only(self):
        # captures.json + a Pulse runtime file: sync commits the Pulse file
        # (resilience) but NOT captures.json, which stays dirty for the healer.
        pre_head = self._head()
        (self.repo_dir / 'agents' / 'beacon' / 'captures.json').write_text(
            '{"captures": [{"title": "a"}]}\n',
        )
        (self.repo_dir / 'runbooks' / 'cycle-journal.md').write_text(
            'seed journal\ncycle line\n',
        )

        result = self._run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        # A Pulse commit landed and was pushed.
        new_head = self._head()
        self.assertNotEqual(new_head, pre_head)
        self.assertEqual(new_head, self._origin_head())
        # The commit touched the Pulse file but NOT captures.json.
        committed = subprocess.run(
            ['git', 'show', '--name-only', '--pretty=format:', 'HEAD'],
            cwd=self.repo_dir, check=True, stdout=subprocess.PIPE,
        ).stdout.decode().split()
        self.assertIn('runbooks/cycle-journal.md', committed)
        self.assertNotIn(self.CAPTURES_REL, committed)
        # captures.json is still dirty (left for the healer).
        self.assertTrue(self._captures_dirty())

    def test_captures_json_plus_non_allowlist_file_still_blocks(self):
        # captures.json (tolerated) + README (not) is still mixed dirt: the
        # refuse path is unchanged so genuine human dirt never gets silently
        # swept past the gate. The alert now fires on the SECOND consecutive
        # dirty tick (one-tick grace), so two runs are needed to observe it.
        pre_head = self._head()
        (self.repo_dir / 'agents' / 'beacon' / 'captures.json').write_text(
            '{"captures": [{"title": "b"}]}\n',
        )
        (self.repo_dir / 'README').write_text('human edit\n')

        first = self._run_sync()
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(self._head(), pre_head)
        self.assertEqual(
            self._read_alerts(), [],
            'first dirty tick must defer the alert (one-tick grace)',
        )

        second = self._run_sync()
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(self._head(), pre_head)
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1, f'expected 1 alert, got {alerts}')
        self.assertEqual(
            alerts[0]['subject'], 'sync-blocked:uncommitted-changes',
        )

    def test_pulse_push_failure_preserves_captures_json(self):
        # The key data-safety property: when a Pulse auto-commit's push fails and
        # sync rolls back, captures.json's live on-disk content is NOT reverted
        # (a plain `git reset --hard` would have clobbered it). Reject pushes via
        # a pre-receive hook, dirty both a Pulse file and captures.json, and
        # assert the rollback preserved captures.json.
        hooks_dir = self.origin / 'hooks'
        hooks_dir.mkdir(exist_ok=True)
        hook = hooks_dir / 'pre-receive'
        hook.write_text('#!/bin/sh\necho rejected by test hook >&2\nexit 1\n')
        os.chmod(hook, 0o755)

        pre_head = self._head()
        live_captures = '{"captures": [{"title": "live ingest"}]}\n'
        (self.repo_dir / 'agents' / 'beacon' / 'captures.json').write_text(
            live_captures,
        )
        (self.repo_dir / 'runbooks' / 'cycle-journal.md').write_text(
            'pulse dirt that triggers the auto-commit\n',
        )

        result = self._run_sync()
        self.assertNotEqual(result.returncode, 0)
        # Pulse auto-commit rolled back: no local-only commit lingers (HEAD back
        # to AUTO_PRE_HEAD) on either local or origin, so the next sync's
        # fast-forward isn't broken.
        self.assertEqual(self._head(), pre_head)
        self.assertEqual(self._origin_head(), pre_head)
        # The index is clean — the rollback left no staged residue that would
        # re-trip the refuse gate on the next tick.
        self.assertEqual(
            subprocess.run(
                ['git', 'diff', '--cached', '--quiet'], cwd=self.repo_dir,
            ).returncode,
            0,
            'rollback must leave a clean index (no staged Pulse residue)',
        )
        # captures.json's live content survived the rollback untouched.
        self.assertEqual(
            (self.repo_dir / 'agents' / 'beacon' / 'captures.json').read_text(),
            live_captures,
        )
        # The push-failure envelope is on the queue.
        alerts = self._read_alerts()
        subjects = [a['subject'] for a in alerts]
        self.assertIn('sync-blocked:auto-commit-push-failed', subjects)


class PushFailPersistenceGateTest(_SyncResilienceBase):
    """The auto-commit-push failure is a benign self-healing race: the rollback
    restores a pushable tree and the next ~5min tick retries. Alerting Larry on
    every such tick was double-noise. Sync now counts CONSECUTIVE rolled-back
    push failures in agent-core-sync.json and DMs (via notify_larry.py) exactly
    once — on the tick the count crosses PUSH_FAIL_ALERT_THRESHOLD. Below
    threshold: digest only, zero DMs; a successful tick resets the counter."""

    def _reject_pushes(self) -> None:
        hooks_dir = self.origin / 'hooks'
        hooks_dir.mkdir(exist_ok=True)
        hook = hooks_dir / 'pre-receive'
        hook.write_text('#!/bin/sh\necho rejected by test hook >&2\nexit 1\n')
        os.chmod(hook, 0o755)

    def _accept_pushes(self) -> None:
        hook = self.origin / 'hooks' / 'pre-receive'
        if hook.exists():
            hook.unlink()

    def _install_notify_stub(self) -> Path:
        """Drop a notify_larry.py stub that records each invocation. The real
        alert_larry() no-ops when notify_larry.py is absent, so without this the
        threshold DM is unobservable."""
        record = self.tmp_path / 'notify_calls.log'
        stub = self.scripts_dir / 'notify_larry.py'
        stub.write_text(
            '#!/usr/bin/env python3\n'
            'import sys\n'
            f'open({str(record)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
        )
        os.chmod(stub, 0o755)
        return record

    def _notify_call_count(self, record: Path) -> int:
        if not record.exists():
            return 0
        return len([l for l in record.read_text().splitlines() if l.strip()])

    def _sync_status(self) -> dict:
        status_file = (
            self.live_root / 'blackboard' / 'agent-core-sync.json'
        )
        return json.loads(status_file.read_text())

    def _dirty_pulse(self, text: str) -> None:
        (self.repo_dir / 'runbooks' / 'cycle-journal.md').write_text(text)

    def test_single_failure_is_silent_digest_only_counter_one(self):
        record = self._install_notify_stub()
        self._reject_pushes()
        self._dirty_pulse('seed journal\ntick 1 dirt\n')

        result = self._run_sync()
        self.assertNotEqual(result.returncode, 0)
        # Counter incremented to 1, status errored.
        status = self._sync_status()
        self.assertEqual(status['status'], 'error')
        self.assertEqual(status['consecutive_push_failures'], 1)
        # The low-noise digest audit trail fired...
        alerts = self._read_alerts()
        subjects = [a['subject'] for a in alerts]
        self.assertIn('sync-blocked:auto-commit-push-failed', subjects)
        digest = [a for a in alerts
                  if a['subject'] == 'sync-blocked:auto-commit-push-failed']
        self.assertTrue(all(a.get('route') == 'digest' for a in digest))
        # ...but Larry got ZERO DMs (below threshold).
        self.assertEqual(self._notify_call_count(record), 0)

    def test_successful_tick_resets_counter(self):
        self._install_notify_stub()
        self._reject_pushes()
        self._dirty_pulse('seed journal\ntick 1 dirt\n')
        first = self._run_sync()
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(self._sync_status()['consecutive_push_failures'], 1)

        # Next tick: pushes accepted, tree clean → no-change/success sync resets.
        self._accept_pushes()
        self._dirty_pulse('seed journal\n')  # revert to committed content
        second = self._run_sync()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self._sync_status()['consecutive_push_failures'], 0)

    def test_threshold_reached_fires_exactly_one_dm(self):
        record = self._install_notify_stub()
        self._reject_pushes()
        # Dirty once; the --mixed rollback keeps the worktree dirt, so each
        # subsequent tick re-attempts the auto-commit + push and fails again.
        self._dirty_pulse('seed journal\npersistent dirt\n')

        # Ticks 1 and 2: below threshold (3) → silent.
        for expected in (1, 2):
            r = self._run_sync()
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(
                self._sync_status()['consecutive_push_failures'], expected,
            )
            self.assertEqual(self._notify_call_count(record), 0)

        # Tick 3: crosses the threshold → exactly ONE DM.
        r3 = self._run_sync()
        self.assertNotEqual(r3.returncode, 0)
        self.assertEqual(self._sync_status()['consecutive_push_failures'], 3)
        self.assertEqual(self._notify_call_count(record), 1)

        # Tick 4: still failing but past the edge → no further DM (exactly one).
        r4 = self._run_sync()
        self.assertNotEqual(r4.returncode, 0)
        self.assertEqual(self._sync_status()['consecutive_push_failures'], 4)
        self.assertEqual(self._notify_call_count(record), 1)


class MissionsJsonToleratedTest(_SyncResilienceBase):
    """agents/beacon/missions.json is healer-owned: heal_missions_card_gc.py is
    its SOLE committer (Contract D — it commits ANY pending missions.json delta
    on its tick). Sync must absorb the write↔commit race by TOLERATING the dirt
    the same way it does captures.json — neither committing nor resetting it — so
    a cleanup's delta no longer jams sync (the P1 incident)."""

    MISSIONS_REL = 'agents/beacon/missions.json'

    def _seed_missions(self) -> None:
        """missions.json isn't part of the base seed commit; track it so a later
        modification registers as tracked-modified."""
        (self.repo_dir / 'agents' / 'beacon' / 'missions.json').write_text(
            '{"schema_version": 1, "missions": []}\n',
        )
        self._git('add', self.MISSIONS_REL)
        self._git('commit', '-q', '-m', 'seed missions')
        self._git('push', '-q', 'origin', 'main')

    def _missions_dirty(self) -> bool:
        rc = subprocess.run(
            ['git', 'diff', '--quiet', '--', self.MISSIONS_REL],
            cwd=self.repo_dir,
        ).returncode
        return rc != 0

    def test_missions_json_only_dirt_is_tolerated_not_committed(self):
        self._seed_missions()
        pre_head = self._head()
        pre_origin = self._origin_head()
        # Dirty exactly missions.json (tracked-modified), simulating the gap
        # between a cleanup's write and the GC healer's next commit tick.
        (self.repo_dir / 'agents' / 'beacon' / 'missions.json').write_text(
            '{"schema_version": 1, "missions": '
            '[{"id": "m-x", "phase": "shipped"}]}\n',
        )

        result = self._run_sync()
        self.assertEqual(
            result.returncode, 0,
            f'sync should tolerate missions.json and proceed; '
            f'stdout={result.stdout!r} stderr={result.stderr!r}',
        )
        # Sync did NOT commit or push: HEAD and origin are unchanged.
        self.assertEqual(self._head(), pre_head)
        self.assertEqual(self._origin_head(), pre_origin)
        # The dirt is left on disk for the healer.
        self.assertTrue(
            self._missions_dirty(),
            'missions.json should still be dirty (left for the GC healer)',
        )
        # No page of any kind.
        alerts = self._read_alerts()
        sync_blocked = [
            a for a in alerts if a.get('subject', '').startswith('sync-blocked')
        ]
        self.assertEqual(sync_blocked, [], f'unexpected alerts: {alerts}')

    def test_missions_plus_captures_dirt_both_tolerated(self):
        # Both healer-owned extras dirty together: sync tolerates the pair,
        # commits neither, and proceeds (no Pulse file dirty → no auto-commit).
        self._seed_missions()
        pre_head = self._head()
        pre_origin = self._origin_head()
        (self.repo_dir / 'agents' / 'beacon' / 'missions.json').write_text(
            '{"schema_version": 1, "missions": [{"id": "m-y"}]}\n',
        )
        (self.repo_dir / 'agents' / 'beacon' / 'captures.json').write_text(
            '{"captures": [{"title": "note"}]}\n',
        )

        result = self._run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._head(), pre_head)
        self.assertEqual(self._origin_head(), pre_origin)
        self.assertTrue(self._missions_dirty())
        alerts = self._read_alerts()
        sync_blocked = [
            a for a in alerts if a.get('subject', '').startswith('sync-blocked')
        ]
        self.assertEqual(sync_blocked, [], f'unexpected alerts: {alerts}')

    def test_missions_plus_non_allowlist_file_still_blocks(self):
        # missions.json (tolerated) + README (not) is still mixed dirt: the
        # refuse-and-alert path is unchanged so genuine human dirt never gets
        # silently swept past the gate. (The alert-emission half of this path is
        # asserted by CapturesJsonToleratedTest; here we assert the block +
        # no-commit, which doesn't depend on the alert CLI.)
        self._seed_missions()
        pre_head = self._head()
        pre_origin = self._origin_head()
        (self.repo_dir / 'agents' / 'beacon' / 'missions.json').write_text(
            '{"schema_version": 1, "missions": [{"id": "m-z"}]}\n',
        )
        (self.repo_dir / 'README').write_text('human edit\n')

        result = self._run_sync()
        self.assertNotEqual(result.returncode, 0)
        # No commit landed locally or on origin; the mixed dirt was refused.
        self.assertEqual(self._head(), pre_head)
        self.assertEqual(self._origin_head(), pre_origin)


class UncommittedChangesOneTickGraceTest(_SyncResilienceBase):
    """One-tick grace on the sync-blocked:uncommitted-changes path.

    The first dirty tick refuses (sync never pulls onto a dirty tree) but DEFERS
    the alert, because the dirt almost always clears by the next ~5min tick when
    Pulse's auto-commit or the GC healer lands. The alert fires only when the
    dirt is still present on a second consecutive tick. (2026-06-18 07:51→07:56
    false alarm: sync refused at 07:51 then synced cleanly one tick later at
    07:56, yet a 🟡 board warning had already fired and — larry_alerts being
    append-only with no resolve — never retracted.)"""

    def _grace_marker(self) -> Path:
        return (
            self.live_root / 'blackboard' / 'agent-core-sync-uncommitted-grace'
        )

    def test_first_dirty_tick_defers_alert_and_sets_marker(self):
        pre_head = self._head()
        # Non-allowlisted dirt — the auto-commit block won't touch it.
        (self.repo_dir / 'README').write_text('human edit\n')

        result = self._run_sync()
        # Safety refusal is unconditional: still non-zero, still no commit.
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._head(), pre_head)
        # No alert on the first occurrence.
        self.assertEqual(
            self._read_alerts(), [],
            'first dirty tick must defer the alert (one-tick grace)',
        )
        # The grace marker is persisted so the next tick can escalate.
        self.assertTrue(
            self._grace_marker().exists(),
            'first dirty tick must persist the grace marker',
        )

    def test_single_dirty_tick_then_clean_emits_no_alert(self):
        # The real-world false-alarm shape: dirty for one tick, then the dirt
        # clears before the next tick. No alert should ever fire.
        (self.repo_dir / 'README').write_text('human edit\n')

        first = self._run_sync()
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(self._read_alerts(), [])
        self.assertTrue(self._grace_marker().exists())

        # The dirt clears before the next tick (revert to committed content).
        (self.repo_dir / 'README').write_text('seed\n')

        second = self._run_sync()
        # Clean tree, HEAD == origin → no-change sync, exit 0.
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            self._read_alerts(), [],
            'a single dirty tick followed by a clean tick must emit no alert',
        )
        # The clean path cleared the marker so the next first-occurrence is fresh.
        self.assertFalse(
            self._grace_marker().exists(),
            'clean tick must clear the one-tick grace marker',
        )

    def test_captures_json_tolerated_tick_clears_grace_marker(self):
        # The tolerate branch (captures.json-only dirt) is also a "proceed" path
        # and must clear the marker too, so a prior dirty tick doesn't carry over
        # into a spurious second-tick alert once the human dirt is gone.
        (self.repo_dir / 'README').write_text('human edit\n')
        first = self._run_sync()
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue(self._grace_marker().exists())

        # Next tick: clear the human dirt; leave only tolerated captures.json.
        (self.repo_dir / 'README').write_text('seed\n')
        (self.repo_dir / 'agents' / 'beacon' / 'captures.json').write_text(
            '{"captures": [{"title": "x"}]}\n',
        )
        second = self._run_sync()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self._read_alerts(), [])
        self.assertFalse(
            self._grace_marker().exists(),
            'tolerated (captures.json) tick must clear the grace marker',
        )


if __name__ == '__main__':
    unittest.main()
