#!/usr/bin/env python3
"""
Tier 2 OAuth orchestrator: install Larry's personal Claude Max OAuth at
HOME=/home/larry/.claude-larry-personal (NOT the default ~/.claude).

Adapted from /tmp/auth_orchestrator.py (2026-05-18 Tier 1 version):
- HOME comes from caller's env (set via launch wrapper, not hardcoded)
- No --email pre-fill (Larry picks personal account in browser)
- Filenames suffixed -tier2 to avoid collision with Tier 1's files
- Does NOT move pre-existing credentials (Tier 2 path is fresh)
"""
import os, pty, subprocess, time, select, sys, re, shutil

URL_FILE = "/tmp/auth-url-tier2.txt"
CODE_FILE = "/tmp/auth-code-tier2.txt"
RESULT_FILE = "/tmp/auth-result-tier2.txt"
LOG_FILE = "/tmp/auth-debug-tier2.log"


def log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.time():.2f}] {msg}\n")


for f in [URL_FILE, CODE_FILE, RESULT_FILE]:
    if os.path.exists(f):
        os.unlink(f)
open(LOG_FILE, "w").close()

# Verify HOME points at the Tier 2 path (caller set it)
home = os.environ.get("HOME", "")
expected = "/home/larry/.claude-larry-personal"
if home != expected:
    log(f"FAIL: HOME={home!r}, expected {expected!r}")
    open(RESULT_FILE, "w").write(f"ERROR: HOME mismatch ({home!r})\n")
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
    open(URL_FILE, "w").write(m.group(0))
    log(f"URL captured ({len(m.group(0))} chars)")
else:
    log("FAIL: no URL found in output")
    log(f"output preview: {text[:500]!r}")
    open(URL_FILE, "w").write("ERROR_NO_URL_FOUND")
    proc.terminate()
    sys.exit(1)

log(f"waiting for {CODE_FILE}")
deadline = time.time() + 900
while time.time() < deadline:
    if os.path.exists(CODE_FILE):
        code = open(CODE_FILE).read().strip()
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

final_text = (buf + buf2).decode("utf-8", errors="replace")
open(RESULT_FILE, "w").write(final_text)
log(f"final result written, rc={proc.poll()}")
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
sys.exit(proc.returncode or 0)
