#!/usr/bin/env python3
"""
Tier 2 OAuth orchestrator: install Larry's personal Claude Max OAuth at
HOME=/home/larry/.claude-larry-personal (NOT the default ~/.claude).

Adapted from auth_orchestrator.py (2026-05-18 Tier 1 version):
- HOME comes from caller's env (set via launch wrapper, not hardcoded)
- No --email pre-fill (Larry picks personal account in browser)
- Filenames suffixed -tier2 to avoid collision with Tier 1's files
- Does NOT move pre-existing credentials (Tier 2 path is fresh)

ADMIN-ONLY, ONE-SHOT recovery tool — not part of the runtime; no daemon reads
these files. Secret-handling hardening (deep-research review follow-up): every
file this script creates is 0600 (umask) and written with O_NOFOLLOW so a
pre-planted symlink at a fixed /tmp path can't redirect the write; the
operator-supplied CODE_FILE is read with O_NOFOLLOW + regular-file + same-owner
checks. The predictable PATHS stay (human-facing convention); only the file
HANDLING is hardened. See auth_orchestrator.py for the Tier 1 twin.
"""
import os, pty, subprocess, time, select, sys, re, shutil, stat, errno

os.umask(0o077)  # anything this script creates is private to the invoking user

URL_FILE = "/tmp/auth-url-tier2.txt"
CODE_FILE = "/tmp/auth-code-tier2.txt"
RESULT_FILE = "/tmp/auth-result-tier2.txt"
LOG_FILE = "/tmp/auth-debug-tier2.log"


def _write_private(path, text, append=False):
    """Write `text` to `path` at 0600, refusing to follow a symlink at the
    final path component (defeats a pre-planted /tmp symlink)."""
    flags = os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW
    flags |= os.O_APPEND if append else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a" if append else "w") as f:
        f.write(text)


def log(msg):
    _write_private(LOG_FILE, f"[{time.time():.2f}] {msg}\n", append=True)


def _read_code_if_present():
    """Return the operator-supplied code, or None if not yet present. Refuses a
    symlink, a non-regular file, or a file owned by another user."""
    try:
        fd = os.open(CODE_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None
        if e.errno in (errno.ELOOP, errno.EMLINK):
            log("SECURITY: CODE_FILE is a symlink; refusing to read")
            return None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            log("SECURITY: CODE_FILE is not a regular file; refusing")
            return None
        if st.st_uid != os.getuid():
            log(f"SECURITY: CODE_FILE owned by uid {st.st_uid}, not us; refusing")
            return None
        return os.read(fd, 65536).decode("utf-8", errors="replace").strip()
    finally:
        os.close(fd)


# LOG_FILE included so the _write_private below (O_NOFOLLOW) can't hit a
# stale/planted log symlink and raise an uncaught OSError before we've logged.
for f in [URL_FILE, CODE_FILE, RESULT_FILE, LOG_FILE]:
    if os.path.lexists(f):
        os.unlink(f)
_write_private(LOG_FILE, "")  # truncate/create the log at 0600

# Verify HOME points at the Tier 2 path (caller set it)
home = os.environ.get("HOME", "")
expected = "/home/larry/.claude-larry-personal"
if home != expected:
    log(f"FAIL: HOME={home!r}, expected {expected!r}")
    _write_private(RESULT_FILE, f"ERROR: HOME mismatch ({home!r})\n")
    sys.exit(1)

# Ensure target dir exists; do NOT touch any pre-existing credentials there
target_dir = os.path.expanduser("~/.claude")
os.makedirs(target_dir, exist_ok=True)
log(f"target_dir={target_dir} ready")

log("spawning claude auth login (no --email pre-fill)")
master, slave = pty.openpty()
proc = subprocess.Popen(
    ["claude", "auth", "login", "--claudeai"],
    stdin=slave, stdout=slave, stderr=slave,
    preexec_fn=os.setsid,
    env=os.environ.copy(),  # inherit HOME from caller
)
os.close(slave)

buf = b""
deadline = time.time() + 30
while time.time() < deadline:
    r, _, _ = select.select([master], [], [], 1.0)
    if r:
        try:
            chunk = os.read(master, 4096)
            if not chunk:
                break
            buf += chunk
            if b"Paste code here" in buf:
                break
        except OSError as e:
            log(f"read err: {e}")
            break

text = buf.decode("utf-8", errors="replace")
log(f"phase1 buf len={len(buf)}")
m = re.search(r"https://claude\.com/cai/oauth/authorize\?\S+", text)
if m:
    _write_private(URL_FILE, m.group(0))
    log(f"URL captured ({len(m.group(0))} chars)")
else:
    log("FAIL: no URL found in output")
    log(f"output preview: {text[:500]!r}")
    _write_private(URL_FILE, "ERROR_NO_URL_FOUND")
    proc.terminate()
    sys.exit(1)

log(f"waiting for {CODE_FILE}")
deadline = time.time() + 900
while time.time() < deadline:
    code = _read_code_if_present()
    if code is not None:
        os.unlink(CODE_FILE)
        log(f"got code, len={len(code)}, injecting")
        os.write(master, (code + "\n").encode())
        break
    time.sleep(1)
else:
    log("FAIL: timed out waiting for code")
    proc.terminate()
    sys.exit(2)

buf2 = b""
deadline = time.time() + 60
while time.time() < deadline:
    r, _, _ = select.select([master], [], [], 1.0)
    if r:
        try:
            chunk = os.read(master, 4096)
            if not chunk:
                break
            buf2 += chunk
        except OSError:
            break
    if proc.poll() is not None:
        try:
            while True:
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                buf2 += chunk
        except OSError:
            pass
        break

# NOTE: result buffer may contain token material; RESULT_FILE is written 0600.
final_text = (buf + buf2).decode("utf-8", errors="replace")
_write_private(RESULT_FILE, final_text)
log(f"final result written, rc={proc.poll()}")
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
sys.exit(proc.returncode or 0)
