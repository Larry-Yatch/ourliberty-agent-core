"""Tests for no_session_ledger.py (S1 / M3) — the durable obligation ledger
backing the cold-start no-session revision self-heal.

Each test isolates the on-disk store by monkeypatching LEDGER_FILE to a
tmp_path; time is injected via the ``now=`` params so age-based logic is
deterministic.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import no_session_ledger as nsl  # noqa: E402

T0 = datetime(2026, 6, 23, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(nsl, 'LEDGER_FILE', tmp_path / 'no-session-ledger.json')


def _open(task='t1', **kw):
    defaults = dict(pr_url='https://gh/o/r/pull/1', branch='claude/x',
                    target_repo='ourliberty-agent-core', head_sha='abc123',
                    round_num=1, now=T0)
    defaults.update(kw)
    nsl.open_obligation(task, **defaults)


def test_open_creates_open_row():
    _open('t1')
    row = nsl.get_obligation('t1')
    assert row is not None
    assert row['status'] == nsl.OPEN
    assert row['pr_url'] == 'https://gh/o/r/pull/1'
    assert row['head_sha'] == 'abc123'
    assert row['round'] == 1
    assert row['opened_at'] == nsl._iso(T0)
    assert row['resolved_at'] is None


def test_open_idempotent_preserves_opened_at_bumps_round():
    _open('t1', round_num=1, head_sha='sha1', now=T0)
    later = T0 + timedelta(minutes=30)
    _open('t1', round_num=2, head_sha='sha2', now=later)
    row = nsl.get_obligation('t1')
    assert row['opened_at'] == nsl._iso(T0)          # preserved
    assert row['last_dispatch_at'] == nsl._iso(later)  # bumped
    assert row['round'] == 2
    assert row['head_sha'] == 'sha2'
    # Still exactly one row.
    assert list(nsl._load().keys()) == ['t1']


def test_resolve_clears_open_returns_true():
    _open('t1')
    assert nsl.resolve_obligation('t1', resolution='review-pass', now=T0) is True
    row = nsl.get_obligation('t1')
    assert row['status'] == nsl.RESOLVED
    assert row['resolution'] == 'review-pass'
    assert row['resolved_at'] is not None


def test_resolve_unknown_or_already_resolved_returns_false():
    assert nsl.resolve_obligation('nope', now=T0) is False
    _open('t1')
    assert nsl.resolve_obligation('t1', now=T0) is True
    assert nsl.resolve_obligation('t1', now=T0) is False  # second time: nothing open


def test_reopen_after_resolve_flips_back_to_open():
    _open('t1', now=T0)
    nsl.resolve_obligation('t1', now=T0)
    reopen = T0 + timedelta(hours=1)
    _open('t1', round_num=2, now=reopen)
    row = nsl.get_obligation('t1')
    assert row['status'] == nsl.OPEN
    assert row['resolved_at'] is None
    assert row['round'] == 2


def test_list_open_excludes_resolved():
    _open('open1')
    _open('done1')
    nsl.resolve_obligation('done1', now=T0)
    open_ids = {r['task_id'] for r in nsl.list_open()}
    assert open_ids == {'open1'}


def test_list_open_filters_by_age():
    _open('fresh', now=T0)
    old = T0 - timedelta(minutes=90)
    _open('stale', now=old)
    now = T0 + timedelta(minutes=1)
    stuck = nsl.list_open(now=now, older_than_minutes=60)
    assert {r['task_id'] for r in stuck} == {'stale'}


def test_prune_drops_aged_resolved_keeps_open():
    _open('keepopen', now=T0)
    _open('oldresolved', now=T0)
    nsl.resolve_obligation('oldresolved', now=T0)
    # A write far in the future triggers prune of the aged resolved row.
    future = T0 + timedelta(days=nsl._RESOLVED_RETENTION_DAYS + 1)
    _open('trigger', now=future)
    ids = set(nsl._load().keys())
    assert 'oldresolved' not in ids       # pruned
    assert {'keepopen', 'trigger'} <= ids  # open rows survive


def test_prune_never_evicts_open_rows(monkeypatch):
    # The cap must bound only RESOLVED-row growth; an OPEN obligation is never
    # evicted (losing one would blind the backstop). cap=3, with 5 open + 2
    # resolved → all 5 open survive, resolved dropped to honor the cap.
    monkeypatch.setattr(nsl, '_MAX_ROWS', 3)
    for i in range(5):
        nsl.open_obligation(f'open{i}', pr_url=f'p{i}',
                            now=T0 + timedelta(minutes=i))
    for i in range(2):
        nsl.open_obligation(f'res{i}', pr_url=f'r{i}', now=T0)
        nsl.resolve_obligation(f'res{i}', now=T0)
    # Any write triggers a prune.
    nsl.open_obligation('trigger', pr_url='t', now=T0 + timedelta(minutes=10))
    rows = nsl._load()
    open_ids = {k for k, v in rows.items() if v['status'] == nsl.OPEN}
    assert {'open0', 'open1', 'open2', 'open3', 'open4', 'trigger'} <= open_ids
    assert 'res0' not in rows and 'res1' not in rows  # resolved dropped


def test_corrupt_file_degrades_to_empty(tmp_path):
    nsl.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    nsl.LEDGER_FILE.write_text('{ not json')
    assert nsl._load() == {}
    assert nsl.get_obligation('anything') is None
    assert nsl.list_open() == []
    # And a write recovers cleanly.
    _open('t1')
    assert nsl.get_obligation('t1')['status'] == nsl.OPEN


def test_empty_task_id_is_noop():
    nsl.open_obligation('', pr_url='x', now=T0)
    assert nsl._load() == {}
    assert nsl.resolve_obligation('', now=T0) is False


def test_persisted_json_is_readable_dict():
    _open('t1')
    on_disk = json.loads(nsl.LEDGER_FILE.read_text())
    assert isinstance(on_disk, dict)
    assert on_disk['t1']['status'] == nsl.OPEN
