"""Tier 1 headless Claude OAuth orchestrator.

ADMIN-ONLY, ONE-SHOT recovery tool. Run manually from chat when the Tier 1
account hits auth-401 (see the recovery strings in agent_runner.py /
beacon_telegram_bot.py). It spawns `claude auth login` under a pty, scrapes the
device-code URL for Larry, waits for him to drop the pasted code at CODE_FILE,
injects it, and records the result. It is NOT part of the runtime; no daemon
reads these files.

Secret-handling hardening (deep-research review follow-up): the four /tmp paths
below are predictable and world-guessable. Every file this script creates is
forced to 0600 via umask, written with O_NOFOLLOW so a pre-planted symlink at a
fixed /tmp path can't redirect our write (the classic predictable-path attack
that the old unlink()+open("w") was open to), and the operator-supplied
CODE_FILE is read with O_NOFOLLOW + a regular-file + same-owner check before we
trust it. The predictable PATHS are kept intentionally — they are the
human-facing convention (Larry writes the code to CODE_FILE, reads the URL from
URL_FILE); only the file HANDLING is hardened.
"""
import os, pty, subprocess, time, select, sys, re, stat, errno

os.umask(0o077)  # anything this script creates is private to the invoking user

EMAIL = "agent.beacon.ourliberty@gmail.com"
URL_FILE = "/tmp/auth-url.txt"
CODE_FILE = "/tmp/auth-code.txt"
RESULT_FILE = "/tmp/auth-result.txt"
LOG_FILE = "/tmp/auth-debug.log"


def _write_private(path, text, append=False):
    """Write `text` to `path` at 0600, refusing to follow a symlink at the
    final path component (defeats a pre-planted /tmp symlink). Replaces the
    TOCTOU-prone unlink()+open("w") pattern."""
    flags = os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW
    flags |= os.O_APPEND if append else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a" if append else "w") as f:
        f.write(text)


def log(msg):
    _write_private(LOG_FILE, f"[{time.time():.2f}] {msg}\n", append=True)


def _read_code_if_present():
    """Return the operator-supplied code, or None if not yet present. Refuses a
    symlink (O_NOFOLLOW), a non-regular file, or a file owned by another user —
    so a hostile local user can't feed us a code or trick us into reading an
    unintended file."""
    try:
        fd = os.open(CODE_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None  # not dropped yet
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


# Start clean. lexists() also catches a dangling/planted symlink; unlink removes
# the link itself (not its target), so this is safe.
for f in [URL_FILE, CODE_FILE, RESULT_FILE]:
    if os.path.lexists(f):
        os.unlink(f)
_write_private(LOG_FILE, "")  # truncate/create the log at 0600

import shutil
cred_file = os.path.expanduser("~/.claude/.credentials.json")
if os.path.exists(cred_file):
    shutil.move(cred_file, cred_file + ".pre-orchestrator-" + str(int(time.time())))
    log("moved existing credentials aside")

log("spawning claude auth login")
master, slave = pty.openpty()
proc = subprocess.Popen(
    ["claude", "auth", "login", "--claudeai", "--email", EMAIL],
    stdin=slave, stdout=slave, stderr=slave,
    preexec_fn=os.setsid,
)
os.close(slave)

buf = b""
deadline = time.time() + 30
while time.time() < deadline:
    r, _, _ = select.select([master], [], [], 1.0)
    if r:
        try:
            chunk = os.read(master, 4096)
            if not chunk: break
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

log("waiting for /tmp/auth-code.txt")
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
            if not chunk: break
            buf2 += chunk
        except OSError:
            break
    if proc.poll() is not None:
        try:
            while True:
                chunk = os.read(master, 4096)
                if not chunk: break
                buf2 += chunk
        except OSError:
            pass
        break

# NOTE: the result buffer is the raw post-auth terminal output and may contain
# token material; RESULT_FILE is written 0600 so it is not world-readable.
final_text = (buf + buf2).decode("utf-8", errors="replace")
_write_private(RESULT_FILE, final_text)
log(f"final result written, rc={proc.poll()}")
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
sys.exit(proc.returncode or 0)
