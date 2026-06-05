"""
Concurrency Guard — Global semaphore shared across all processes.
Uses a file-based lock + counter to limit total concurrent claude processes
across the Telegram bot AND orchestrator.

Safe limit: 6 concurrent claude processes
  6 x 400MB = 2.4 GB
  + services ~900MB
  + headroom for 1-2 Playwright browsers (~1 GB)
  = ~4.3 GB peak of 7.8 GB available (+ 4 GB swap safety)
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)

import os
import json
import time
import fcntl
from pathlib import Path

AGENTS_ROOT = Path.home() / 'agents'
GUARD_FILE = AGENTS_ROOT / 'config' / '.concurrency-guard.json'

# Safe limit defaults to 6 (see module docstring for the RAM math: 6×400MB +
# services + browsers ≈ 4.3 GB of 7.8 GB). It was previously hard-coded to 10 —
# inherited from the gm-agent-core bootstrap — which contradicted the documented
# ceiling and risked OOM on this VM. Override ONLY via the environment when the
# VM is resized; a value outside the safe band fails loudly rather than silently
# over-committing memory.
_MAX_CONCURRENT_CEILING = 6


def _resolve_max_concurrent():
    raw = os.environ.get('OURLIBERTY_MAX_CONCURRENT')
    if raw is None:
        return _MAX_CONCURRENT_CEILING
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f'OURLIBERTY_MAX_CONCURRENT={raw!r} is not an integer')
    if value < 1 or value > _MAX_CONCURRENT_CEILING:
        raise ValueError(
            f'Unsafe OURLIBERTY_MAX_CONCURRENT={value}; expected 1..'
            f'{_MAX_CONCURRENT_CEILING} for current VM sizing')
    return value


MAX_CONCURRENT = _resolve_max_concurrent()
STALE_TIMEOUT = 600  # Fallback reap window when process identity is unverifiable


def _proc_start_token(pid):
    """A stable per-process start token (Linux `/proc/<pid>/stat` starttime),
    or None where unavailable (e.g. macOS, or the process is gone).

    `os.kill(pid, 0)` alone can't tell a still-running slot-owner from an
    unrelated process that the OS later assigned the SAME recycled PID. Pairing
    the PID with its start time disambiguates: a recycled PID has a different
    start time, so a stale slot is dropped immediately instead of squatting a
    capacity slot until STALE_TIMEOUT — and a verified-same long-running process
    keeps its slot even past STALE_TIMEOUT (builds can run for >10 min)."""
    try:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    # comm (field 2) is parenthesized and may contain spaces/parens; split after
    # the last ')'. starttime is field 22 → index 19 of the post-comm fields
    # (which start at field 3).
    try:
        rest = data[data.rfind(b')') + 2:].split()
        return rest[19].decode()
    except (IndexError, ValueError):
        return None


class ConcurrencyGuard:
    def __init__(self):
        if not GUARD_FILE.exists():
            self._write({'slots': [], 'max': MAX_CONCURRENT})

    def _read(self):
        try:
            with open(GUARD_FILE) as f:
                return json.load(f)
        except:
            return {'slots': [], 'max': MAX_CONCURRENT}

    def _write(self, data):
        with open(GUARD_FILE, 'w') as f:
            json.dump(data, f)

    def _clean_stale(self, data):
        """Remove slots whose owning process is dead, whose PID was recycled by
        a different process, or (when identity is unverifiable) timed out."""
        now = time.time()
        alive = []
        for slot in data.get('slots', []):
            pid = slot.get('pid', 0)
            ts = slot.get('ts', 0)
            # Is *some* process alive at this PID?
            try:
                os.kill(pid, 0)
            except (OSError, ProcessLookupError):
                continue  # dead process — drop the slot

            stored = slot.get('start')
            current = _proc_start_token(pid)
            if stored is not None and current is not None:
                # Identity is verifiable: keep iff it's the SAME process,
                # regardless of age (a long build legitimately outlives
                # STALE_TIMEOUT). A mismatch means the PID was recycled.
                if stored == current:
                    alive.append(slot)
                continue
            # Identity unverifiable (no /proc, or legacy slot without 'start')
            # → fall back to the time-based reap.
            if now - ts < STALE_TIMEOUT:
                alive.append(slot)
        data['slots'] = alive
        return data

    def acquire(self, agent_id, task_id=''):
        """Try to acquire a slot. Returns True if acquired, False if at capacity."""
        lock_file = GUARD_FILE.with_suffix('.lock')
        with open(lock_file, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = self._read()
                data = self._clean_stale(data)

                if len(data['slots']) >= MAX_CONCURRENT:
                    return False

                data['slots'].append({
                    'pid': os.getpid(),
                    'agent': agent_id,
                    'task': task_id,
                    'ts': time.time(),
                    'start': _proc_start_token(os.getpid()),
                })
                self._write(data)
                return True
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def release(self, agent_id=''):
        """Release a slot for this process."""
        lock_file = GUARD_FILE.with_suffix('.lock')
        with open(lock_file, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = self._read()
                pid = os.getpid()
                data['slots'] = [s for s in data.get('slots', [])
                                if not (s.get('pid') == pid and
                                       (not agent_id or s.get('agent') == agent_id))]
                self._write(data)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def active_count(self):
        """How many slots are currently active."""
        data = self._read()
        data = self._clean_stale(data)
        return len(data.get('slots', []))

    def wait_for_slot(self, agent_id, task_id='', timeout=1800, poll_interval=2):
        """Block until a slot is available or timeout."""
        start = time.time()
        while time.time() - start < timeout:
            if self.acquire(agent_id, task_id):
                return True
            time.sleep(poll_interval)
        return False


# Singleton
_guard = None

def get_guard():
    global _guard
    if _guard is None:
        _guard = ConcurrencyGuard()
    return _guard
