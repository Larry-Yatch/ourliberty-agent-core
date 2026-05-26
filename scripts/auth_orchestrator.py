import os, pty, subprocess, time, select, sys, re

EMAIL = "agent.beacon.ourliberty@gmail.com"
URL_FILE = "/tmp/auth-url.txt"
CODE_FILE = "/tmp/auth-code.txt"
RESULT_FILE = "/tmp/auth-result.txt"
LOG_FILE = "/tmp/auth-debug.log"

def log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.time():.2f}] {msg}\n")

for f in [URL_FILE, CODE_FILE, RESULT_FILE]:
    if os.path.exists(f):
        os.unlink(f)
open(LOG_FILE, "w").close()

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
    open(URL_FILE, "w").write(m.group(0))
    log(f"URL captured ({len(m.group(0))} chars)")
else:
    log("FAIL: no URL found in output")
    log(f"output preview: {text[:500]!r}")
    open(URL_FILE, "w").write("ERROR_NO_URL_FOUND")
    proc.terminate()
    sys.exit(1)

log("waiting for /tmp/auth-code.txt")
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

final_text = (buf + buf2).decode("utf-8", errors="replace")
open(RESULT_FILE, "w").write(final_text)
log(f"final result written, rc={proc.poll()}")
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
sys.exit(proc.returncode or 0)
