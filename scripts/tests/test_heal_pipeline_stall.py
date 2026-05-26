"""Tests for scripts/heal_pipeline_stall.py.

Coverage:
- Each of the five checks against fixture log lines + mock gh output
- Kill switch
- Dedup state file
- Regex patterns against real-shape log lines from outbox_notifier
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture
def tmp_agents_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'agents'
    root.mkdir()
    (root / 'logs').mkdir()
    (root / 'blackboard').mkdir()
    monkeypatch.setenv('OURLIBERTY_AGENTS_ROOT', str(root))
    # Force re-import so module-level constants pick up the env var.
    if 'heal_pipeline_stall' in sys.modules:
        del sys.modules['heal_pipeline_stall']
    return root


@pytest.fixture
def hps(tmp_agents_root: Path):
    """Imports heal_pipeline_stall fresh against the temp root."""
    import heal_pipeline_stall  # noqa: E402
    return heal_pipeline_stall


def _ts(minutes_ago: int) -> str:
    """Produce a log-line timestamp `minutes_ago` minutes before now."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _write_log(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n')


# ---------- Regex pattern tests ----------

def test_forge_done_regex_matches_real_shape(hps):
    line = '[2026-05-25 13:08:39] [notifier] [INFO] [forge] done task=chain-discipline-v2-marker-shape-and-stale-daemon-001 success=True'
    m = hps._FORGE_DONE_RE.search(line)
    assert m is not None
    assert m.group('task') == 'chain-discipline-v2-marker-shape-and-stale-daemon-001'


def test_marker_notified_mirror_regex_matches_pass(hps):
    line = '[2026-05-25 13:16:54] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-chain-discipline-marker-parser-and-regression-check-001.json)'
    m = hps._MARKER_NOTIFIED_MIRROR_RE.search(line)
    assert m is not None
    assert m.group('intent') == 'review-pass'
    assert m.group('task') == 'chain-discipline-marker-parser-and-regression-check-001'


def test_notified_mirror_generic_regex_matches_depth1(hps):
    line = '[2026-05-25 19:08:53] [notifier] [INFO] notified beacon <- mirror (mirror-result, depth=1, file=notify-review-pr-104-e4-4d-system-tab-spec.json)'
    m = hps._NOTIFIED_MIRROR_GENERIC_RE.search(line)
    assert m is not None
    assert m.group('task') == 'review-pr-104-e4-4d-system-tab-spec'


def test_review_request_dispatched_regex(hps):
    line = '[2026-05-25 13:08:44] [notifier] [INFO] review-request dispatched mirror <- beacon (task=chain-discipline-marker-parser-and-regression-check-001, file=review-X.json, pr=https://github.com/Larry-Yatch/ourliberty-agent-core/pull/103)'
    m = hps._REVIEW_REQUEST_DISPATCHED_RE.search(line)
    assert m is not None
    assert m.group('task') == 'chain-discipline-marker-parser-and-regression-check-001'


def test_auto_merge_regex_matches_outcome_merged(hps):
    line = '[2026-05-25 13:16:57] [notifier] [INFO] AUTO_MERGE task=chain-discipline-marker-parser-and-regression-check-001 pr=https://github.com/Larry-Yatch/ourliberty-agent-core/pull/103 outcome=merged (--squash --delete-branch)'
    m = hps._AUTO_MERGE_MERGED_RE.search(line)
    assert m is not None
    assert m.group('task') == 'chain-discipline-marker-parser-and-regression-check-001'


# ---------- Kill switch ----------

def test_kill_switch_exits_immediately(tmp_agents_root: Path, hps):
    (tmp_agents_root / 'healers.disabled').touch()
    with patch.object(hps.larry_alerts, 'append_alert') as mock_alert:
        hps.run()
    mock_alert.assert_not_called()


# ---------- Heartbeat ----------

def test_heartbeat_writes_on_run(tmp_agents_root: Path, hps):
    with patch.object(hps, '_all_open_prs', return_value=[]), \
         patch.object(hps, '_all_merged_prs_recent', return_value=[]), \
         patch.object(hps, '_read_recent_log_lines', return_value=[]), \
         patch('subprocess.run') as mock_sub:
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stdout = ''
        mock_sub.return_value.stderr = ''
        hps.run()
    assert (tmp_agents_root / 'blackboard' / 'heal-pipeline-stall.heartbeat').exists()


# ---------- Check 1: forge built but no PR ----------

def test_check_forge_built_no_pr_fires_when_no_matching_pr(tmp_agents_root: Path, hps):
    lines = [f'[{_ts(180)}] [notifier] [INFO] [forge] done task=test-stalled-task-001 success=True']
    alerts = hps.check_forge_built_no_pr(lines, [], [], {})
    assert len(alerts) == 1
    assert 'test-stalled-task-001' in alerts[0]['message']
    assert alerts[0]['key'] == 'forge_built_no_pr:test-stalled-task-001'


def test_check_forge_built_no_pr_skips_when_pr_exists(tmp_agents_root: Path, hps):
    lines = [f'[{_ts(180)}] [notifier] [INFO] [forge] done task=test-task-002 success=True']
    open_prs = [{'headRefName': 'forge/test-task-002', 'number': 99, '_repo': 'x/y'}]
    alerts = hps.check_forge_built_no_pr(lines, open_prs, [], {})
    assert alerts == []


def test_check_forge_built_no_pr_respects_threshold(tmp_agents_root: Path, hps):
    lines = [f'[{_ts(60)}] [notifier] [INFO] [forge] done task=recent-task-001 success=True']
    alerts = hps.check_forge_built_no_pr(lines, [], [], {})
    assert alerts == []  # < FORGE_BUILT_NO_PR_MIN


def test_check_forge_built_no_pr_accepts_merged_pr(tmp_agents_root: Path, hps):
    """A merged PR for the task means Forge's work shipped — not a stall."""
    lines = [f'[{_ts(180)}] [notifier] [INFO] [forge] done task=merged-task-001 success=True']
    merged_prs = [{'headRefName': 'forge/merged-task-001', 'number': 50, '_repo': 'x/y'}]
    alerts = hps.check_forge_built_no_pr(lines, [], merged_prs, {})
    assert alerts == []


# ---------- Check 2: PR opened but no Mirror review-request dispatched ----------

def test_check_pr_no_mirror_dispatch_fires_for_doc_after_30min(hps):
    pr = {
        'number': 200, 'headRefName': 'forge/doc-task-001', 'title': 'docs(e4): something',
        'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
        '_repo': 'Larry-Yatch/ourliberty-agent-core',
    }
    alerts = hps.check_pr_no_mirror_dispatch([], [pr], {})
    assert len(alerts) == 1
    assert 'PR #200' in alerts[0]['message']


def test_check_pr_no_mirror_dispatch_waits_longer_for_code(hps):
    pr = {
        'number': 201, 'headRefName': 'forge/code-task-001', 'title': 'fix(chain): X',
        'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
        '_repo': 'Larry-Yatch/ourliberty-agent-core',
    }
    # 45 min < 60 min code threshold
    alerts = hps.check_pr_no_mirror_dispatch([], [pr], {})
    assert alerts == []


def test_check_pr_no_mirror_dispatch_skips_when_dispatched(hps):
    pr = {
        'number': 202, 'headRefName': 'forge/dispatched-task-001', 'title': 'docs: x',
        'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
        '_repo': 'Larry-Yatch/ourliberty-agent-core',
    }
    lines = [f'[{_ts(40)}] [notifier] [INFO] review-request dispatched mirror <- beacon (task=dispatched-task-001, file=review-x.json, pr=https://example/1)']
    alerts = hps.check_pr_no_mirror_dispatch(lines, [pr], {})
    assert alerts == []


def test_check_pr_no_mirror_dispatch_ignores_non_forge_branches(hps):
    """A `larry/` branch is human-authored — chain doesn't auto-review."""
    pr = {
        'number': 203, 'headRefName': 'larry/manual-doc', 'title': 'docs: x',
        'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat(),
        '_repo': 'Larry-Yatch/ourliberty-agent-core',
    }
    alerts = hps.check_pr_no_mirror_dispatch([], [pr], {})
    assert alerts == []


# ---------- Check 3: Mirror PASS but PR still OPEN ----------

def test_check_mirror_pass_unmerged_fires(hps):
    lines = [f'[{_ts(45)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-pass-stuck-001.json)']
    pr = {
        'number': 300, 'headRefName': 'forge/pass-stuck-001', 'title': 'fix: x',
        '_repo': 'Larry-Yatch/ourliberty-agent-core',
    }
    alerts = hps.check_mirror_pass_unmerged(lines, [pr], {})
    assert len(alerts) == 1
    assert 'PR #300' in alerts[0]['message']
    assert 'AUTO_MERGE never fired' in alerts[0]['message']


def test_check_mirror_pass_unmerged_respects_threshold(hps):
    lines = [f'[{_ts(15)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-fresh-pass-001.json)']
    pr = {
        'number': 301, 'headRefName': 'forge/fresh-pass-001', 'title': 'fix: x',
        '_repo': 'Larry-Yatch/ourliberty-agent-core',
    }
    alerts = hps.check_mirror_pass_unmerged(lines, [pr], {})
    assert alerts == []


def test_check_mirror_pass_unmerged_ignores_other_intents(hps):
    """A REVIEW_REVISION marker shouldn't trigger this check."""
    lines = [f'[{_ts(45)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-revision, file=notify-rev-001.json)']
    pr = {
        'number': 302, 'headRefName': 'forge/rev-001', 'title': 'fix: x',
        '_repo': 'Larry-Yatch/ourliberty-agent-core',
    }
    alerts = hps.check_mirror_pass_unmerged(lines, [pr], {})
    assert alerts == []


# ---------- Check 4: Mirror marker invisible ----------

def test_check_mirror_marker_invisible_fires_when_no_classify(hps):
    lines = [f'[{_ts(45)}] [notifier] [INFO] notified beacon <- mirror (mirror-result, depth=1, file=notify-shape-drift-001.json)']
    alerts = hps.check_mirror_marker_invisible(lines, {})
    assert len(alerts) == 1
    assert 'shape-drift-001' in alerts[0]['message']
    assert 'marker-shape drift' in alerts[0]['message'] or 'parser did NOT classify' in alerts[0]['message']


def test_check_mirror_marker_invisible_skips_when_classified(hps):
    """A generic notify followed by a marker-notified for the same task is fine."""
    lines = [
        f'[{_ts(45)}] [notifier] [INFO] notified beacon <- mirror (mirror-result, depth=1, file=notify-good-001.json)',
        f'[{_ts(44)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-good-001.json)',
    ]
    alerts = hps.check_mirror_marker_invisible(lines, {})
    assert alerts == []


def test_check_mirror_marker_invisible_skips_when_auto_merged(hps):
    """If a PR merged after the generic notify, the marker was eventually classified
    (perhaps via a follow-up dispatch). Don't false-alert."""
    lines = [
        f'[{_ts(60)}] [notifier] [INFO] notified beacon <- mirror (mirror-result, depth=1, file=notify-merged-001.json)',
        f'[{_ts(30)}] [notifier] [INFO] AUTO_MERGE task=merged-001 pr=https://example/x outcome=merged (--squash --delete-branch)',
    ]
    alerts = hps.check_mirror_marker_invisible(lines, {})
    assert alerts == []


# ---------- Check 5: retry-cap exhausted ----------

def test_check_retry_exhausted_parses_journal(hps):
    fake_journal = 'May 26 08:01:00 host inbox-watcher: All retries exhausted for task=dead-task-001\n'
    with patch('subprocess.run') as mock_sub:
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stdout = fake_journal
        mock_sub.return_value.stderr = ''
        alerts = hps.check_retry_exhausted({})
    assert len(alerts) == 1
    assert 'dead-task-001' in alerts[0]['message']


def test_check_retry_exhausted_silent_when_no_lines(hps):
    with patch('subprocess.run') as mock_sub:
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stdout = 'May 26 08:01:00 host inbox-watcher: normal log line\n'
        mock_sub.return_value.stderr = ''
        alerts = hps.check_retry_exhausted({})
    assert alerts == []


# ---------- Dedup ----------

def test_dedup_state_suppresses_repeat_within_cooldown(tmp_agents_root: Path, hps):
    state = {}
    key = 'test:dedup-key'
    assert hps.should_alert(state, key) is True
    hps.record_alert(state, key)
    assert hps.should_alert(state, key) is False  # within 1h cooldown


def test_dedup_state_allows_after_cooldown(tmp_agents_root: Path, hps):
    state = {}
    key = 'test:dedup-key'
    # Record an old alert (2h ago)
    state[key] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert hps.should_alert(state, key) is True


def test_dedup_state_persists_to_disk(tmp_agents_root: Path, hps):
    state = {'key1': '2026-05-26T10:00:00+00:00'}
    hps.save_state(state)
    loaded = hps.load_state()
    assert loaded == state


# ---------- Branch-to-task extraction ----------

def test_task_id_from_branch_extracts_forge(hps):
    assert hps._task_id_from_branch('forge/my-task-001') == 'my-task-001'


def test_task_id_from_branch_extracts_larry(hps):
    assert hps._task_id_from_branch('larry/eod-docs-rollup') == 'eod-docs-rollup'


def test_task_id_from_branch_returns_none_for_unrecognized(hps):
    assert hps._task_id_from_branch('main') is None
    assert hps._task_id_from_branch('feature/x') is None


# ---------- End-to-end ----------

def test_end_to_end_no_stalls_silent(tmp_agents_root: Path, hps):
    """When everything is healthy, no alerts fire."""
    with patch.object(hps, '_all_open_prs', return_value=[]), \
         patch.object(hps, '_all_merged_prs_recent', return_value=[]), \
         patch.object(hps, '_read_recent_log_lines', return_value=[]), \
         patch('subprocess.run') as mock_sub, \
         patch.object(hps.larry_alerts, 'append_alert') as mock_alert:
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stdout = ''
        mock_sub.return_value.stderr = ''
        hps.run()
    mock_alert.assert_not_called()


def test_end_to_end_fires_alert_on_real_stall(tmp_agents_root: Path, hps):
    """A clear stall produces exactly one DM with severity warning."""
    pr = {
        'number': 999, 'headRefName': 'forge/end2end-stuck-001', 'title': 'fix: x',
        'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat(),
        '_repo': 'Larry-Yatch/ourliberty-agent-core',
    }
    pass_line = f'[{_ts(60)}] [notifier] [INFO] marker-notified beacon <- mirror (mirror-result, intent=review-pass, file=notify-end2end-stuck-001.json)'
    with patch.object(hps, '_all_open_prs', return_value=[pr]), \
         patch.object(hps, '_all_merged_prs_recent', return_value=[]), \
         patch.object(hps, '_read_recent_log_lines', return_value=[pass_line]), \
         patch('subprocess.run') as mock_sub, \
         patch.object(hps.larry_alerts, 'append_alert', return_value=True) as mock_alert:
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stdout = ''
        mock_sub.return_value.stderr = ''
        hps.run()
    assert mock_alert.call_count == 1
    call = mock_alert.call_args
    assert call.kwargs['source'] == 'heal-pipeline-stall'
    assert call.kwargs['severity'] == 'warning'
    assert 'PR #999' in call.kwargs['message']
