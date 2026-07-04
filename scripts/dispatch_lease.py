"""
Dispatch Lease — Wave 2 concurrency primitive.

Atomic file leases for in-flight tasks. Replaces the ephemeral in-memory
_running_tasks dict with a persistent, restart-safe primitive.

Nova's 4 amendments + Prism's 3 hardening additions:
  1. Atomic heartbeat writes (write-to-temp + os.rename)
  2. Flock-protected reclaim (fcntl.flock during acquire)
  3. Agent-scoped heartbeat (daemon thread per task)
  4. Kill-before-reclaim (SIGTERM → 10s grace → SIGKILL)
  5. Boot-ID PID-reuse guard (/proc/sys/kernel/random/boot_id)
  6. Nonce validation (UUID4 per lease)
  7. TTL ≥ 3× heartbeat (180s TTL with 60s heartbeat)

Modes via GM_DEDUP_USE_LEASES env var:
  off           — no lease operations (full fallback)
  shadow        — leases acquired + heartbeated, divergences vs old dedup
                  are logged, old dedup is authoritative  (Wave 2 default)
  authoritative — leases are source of truth, old dedup disabled
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)

import os
import json
import uuid
import time
import fcntl
import signal
import threading
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pid_identity  # noqa: E402  # PID-reuse guard before reclaim-kill
from test_isolation_guard import refuse_under_test  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or Path.home() / 'agents')
LEASES_DIR = AGENTS_ROOT / 'state' / 'dispatch-leases'
DIVERGENCE_LOG = AGENTS_ROOT / 'logs' / 'lease-divergences.log'
LEASE_LOG = AGENTS_ROOT / 'logs' / 'lease-operations.log'

HEARTBEAT_INTERVAL_SECONDS = 60
TTL_SECONDS = 180
RECLAIM_SIGTERM_GRACE_SECONDS = 10


def mode():
    return os.environ.get('GM_DEDUP_USE_LEASES', 'shadow').lower()


def _boot_id():
    try:
        with open('/proc/sys/kernel/random/boot_id') as f:
            return f.read().strip()
    except Exception:
        return 'unknown'


def _safe_identity(identity):
    # identities can contain slashes / weird chars; make them filename-safe
    return identity.replace('/', '_').replace('..', '_')[:200]


def _lease_path(identity):
    LEASES_DIR.mkdir(parents=True, exist_ok=True)
    return LEASES_DIR / (_safe_identity(identity) + '.lease')


def _lock_path(identity):
    LEASES_DIR.mkdir(parents=True, exist_ok=True)
    return LEASES_DIR / (_safe_identity(identity) + '.lock')


def _log(path, msg):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, 'a') as f:
            f.write(f'[{ts}] {msg}\n')
    except Exception:
        pass


def _log_op(msg):
    _log(LEASE_LOG, msg)


def log_divergence(msg):
    _log(DIVERGENCE_LOG, msg)


def _atomic_write(path, data):
    # Delegates to the shared guarded atomic_io writer. Byte-identical to the
    # prior compact json.dump (indent=None, no trailing newline).
    import atomic_io
    atomic_io.atomic_write_json(path, data, indent=None, trailing_newline=False)


def _read_lease(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _pid_alive_same_boot(pid, boot_id):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ProcessLookupError):
        return False
    return _boot_id() == boot_id


class _Lock:
    """Context manager for flock on the lease lock file."""
    def __init__(self, identity):
        self.lock_file = _lock_path(identity)
        self.fd = None

    def __enter__(self):
        self.fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def try_acquire(identity, holder_pid=None):
    """Try to acquire a lease.

    Returns dict: { acquired: bool, nonce: str|None, reason: str, existing_pid: int|None }
    In 'off' mode always returns acquired=True, nonce=None.

    If a stale lease is found (age > TTL or holder PID dead), performs
    kill-before-reclaim before acquiring.
    """
    if mode() == 'off':
        return {'acquired': True, 'nonce': None, 'reason': 'leases-off', 'existing_pid': None}

    holder_pid = holder_pid or os.getpid()
    boot_id = _boot_id()
    lease_path = _lease_path(identity)

    with _Lock(identity):
        if lease_path.exists():
            existing = _read_lease(lease_path)
            if existing:
                age = time.time() - existing.get('timestamp_renewed', 0)
                ex_pid = existing.get('holder_pid')
                ex_boot = existing.get('boot_id', 'unknown')

                if age < TTL_SECONDS and _pid_alive_same_boot(ex_pid, ex_boot):
                    return {
                        'acquired': False,
                        'nonce': None,
                        'reason': f'held-live-pid-{ex_pid}-age-{int(age)}s',
                        'existing_pid': ex_pid,
                    }

                # boot_id catches a PID reused across a reboot; the start-time
                # check catches a PID recycled to an unrelated process within the
                # SAME boot (None on a legacy lease -> degrades to liveness only,
                # never less safe). Only kill when it is provably still the holder.
                same_holder = _pid_alive_same_boot(ex_pid, ex_boot) and \
                    pid_identity.still_same_process(ex_pid, existing.get('holder_starttime'))
                if same_holder:
                    refuse_under_test('reclaim-kill')
                    _log_op(f'RECLAIM: stale lease for {identity} with live PID {ex_pid} '
                            f'(age={int(age)}s) — SIGTERM then SIGKILL')
                    try:
                        os.kill(int(ex_pid), signal.SIGTERM)
                        time.sleep(RECLAIM_SIGTERM_GRACE_SECONDS)
                        # Re-verify IDENTITY (not just liveness) after the grace:
                        # the holder may have exited in response to SIGTERM and
                        # the PID been recycled to an unrelated process during the
                        # window. still_same_process returns False if it's gone or
                        # recycled, so we never SIGKILL a bystander.
                        if pid_identity.still_same_process(ex_pid, existing.get('holder_starttime')):
                            os.kill(int(ex_pid), signal.SIGKILL)
                            _log_op(f'RECLAIM: SIGKILLed {ex_pid}')
                    except (OSError, ProcessLookupError):
                        pass
                else:
                    _log_op(f'RECLAIM: stale lease for {identity} '
                            f'(pid {ex_pid} dead, different boot, or PID reused)')

                lease_path.unlink(missing_ok=True)

        nonce = str(uuid.uuid4())
        now = time.time()
        _atomic_write(lease_path, {
            'identity': identity,
            'holder_pid': holder_pid,
            'holder_starttime': pid_identity.proc_starttime(holder_pid),
            'boot_id': boot_id,
            'nonce': nonce,
            'timestamp_created': now,
            'timestamp_renewed': now,
        })
        _log_op(f'ACQUIRE: {identity} pid={holder_pid} nonce={nonce[:8]}')
        return {'acquired': True, 'nonce': nonce, 'reason': 'acquired', 'existing_pid': None}


def renew(identity, nonce):
    """Renew a held lease. Returns (renewed: bool, reason: str).

    If nonce doesn't match, lease was already reclaimed by someone else — we
    do NOT overwrite it (that would steal it back from a legitimate new holder).
    """
    if mode() == 'off':
        return (True, 'leases-off')
    lease_path = _lease_path(identity)
    with _Lock(identity):
        existing = _read_lease(lease_path)
        if not existing:
            return (False, 'lease-missing')
        if existing.get('nonce') != nonce:
            return (False, 'nonce-mismatch')
        existing['timestamp_renewed'] = time.time()
        try:
            _atomic_write(lease_path, existing)
        except Exception as e:
            return (False, f'write-failed:{e}')
    return (True, 'renewed')


def release(identity, nonce=None):
    """Release a lease. If nonce provided, only release if nonce matches."""
    if mode() == 'off':
        return
    lease_path = _lease_path(identity)
    with _Lock(identity):
        if not lease_path.exists():
            return
        if nonce:
            existing = _read_lease(lease_path)
            if existing and existing.get('nonce') != nonce:
                _log_op(f'RELEASE: nonce-mismatch for {identity}, refusing')
                return
        lease_path.unlink(missing_ok=True)
        _log_op(f'RELEASE: {identity}')


def is_held(identity) -> bool:
    """Read-only predicate: is a LIVE lease currently held for `identity`?

    True iff a lease file exists, is within TTL, and its holder PID is alive on
    the current boot — the same "held" notion try_acquire enforces before it
    refuses (see the age < TTL_SECONDS and _pid_alive_same_boot gate there), so
    a peer process polling this sees exactly what the acquirer would.

    Unlike try_acquire this NEVER acquires, reclaims, signals, or creates
    anything: it reads the lease path directly (no LEASES_DIR mkdir) and never
    kills the holder. That makes it safe for an unrelated process (e.g. a healer)
    to ask "is the watcher mid-dispatch?" without disturbing the lease. In 'off'
    mode no lease files are written, so this is always False."""
    if mode() == 'off':
        return False
    # Read the path directly rather than via _lease_path(), which mkdirs the
    # leases dir as a side effect — a write we must not do on this read path.
    existing = _read_lease(LEASES_DIR / (_safe_identity(identity) + '.lease'))
    if not existing:
        return False
    age = time.time() - existing.get('timestamp_renewed', 0)
    if age >= TTL_SECONDS:
        return False
    return _pid_alive_same_boot(existing.get('holder_pid'),
                                existing.get('boot_id', 'unknown'))


def startup_sweep():
    """Clear any leases left over from a previous boot — PID reuse is possible."""
    if mode() == 'off':
        return 0
    if not LEASES_DIR.exists():
        return 0
    current_boot = _boot_id()
    cleared = 0
    for lease_file in LEASES_DIR.glob('*.lease'):
        data = _read_lease(lease_file)
        if not data or data.get('boot_id', 'unknown') != current_boot:
            lease_file.unlink(missing_ok=True)
            cleared += 1
    if cleared:
        _log_op(f'STARTUP_SWEEP: cleared {cleared} leases from previous boot')
    return cleared


class Heartbeat(threading.Thread):
    """Daemon thread that renews a lease on a fixed interval.

    stop() signals the thread to exit on its next wake-up. The thread is a
    daemon so it dies with the process — no cleanup needed on orchestrator
    SIGKILL.
    """
    def __init__(self, identity, nonce):
        super().__init__(daemon=True, name=f'lease-hb-{identity[:24]}')
        self.identity = identity
        self.nonce = nonce
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            ok, reason = renew(self.identity, self.nonce)
            if not ok and reason == 'nonce-mismatch':
                _log_op(f'HEARTBEAT: {self.identity} nonce-mismatch — duplicate worker, giving up')
                return
            if not ok:
                _log_op(f'HEARTBEAT: {self.identity} renew failed: {reason}')
