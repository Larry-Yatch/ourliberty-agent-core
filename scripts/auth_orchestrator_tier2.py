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
these files. Secret-handling hardening (deep-research review follow-up +
2026-06-05 audit): every file this script creates is 0600 (umask) and created
**exclusively** (O_CREAT|O_EXCL|O_NOFOLLOW) so a pre-planted symlink OR a
pre-planted attacker-owned regular file at a fixed /tmp path can't capture the
write; if the path already exists we refuse unless it is a regular file we own.
The operator-supplied CODE_FILE is read with O_NOFOLLOW + regular-file +
same-owner checks. The predictable PATHS stay (human-facing convention); only
the file HANDLING is hardened. Import is side-effect-free (executable flow under
main()/__main__). See auth_orchestrator.py for the Tier 1 twin.
"""
import os, pty, subprocess, time, select, sys, re, stat, errno, atexit

URL_FILE = "/tmp/auth-url-tier2.txt"
CODE_FILE = "/tmp/auth-code-tier2.txt"
RESULT_FILE = "/tmp/auth-result-tier2.txt"
LOG_FILE = "/tmp/auth-debug-tier2.log"


def _write_private(path, text, append=False):
    """Write `text` to `path` at 0600, refusing to leak into a file we did not
    create or do not own. See auth_orchestrator.py for the full rationale.

    Hardening against the predictable-/tmp-path attacks:
      * O_CREAT|O_EXCL creates the file ourselves when it does not exist.
      * If it already exists we re-open WITHOUT O_CREAT and refuse unless it is a
        *regular* file, *owned by us*, with *link count 1*, then re-assert 0600 —
        rejecting a planted symlink (O_NOFOLLOW → ELOOP), a foreign-owned file
        (st_uid), and a same-uid *hardlink* swap (st_nlink != 1) that O_NOFOLLOW
        alone does NOT catch.
      * If the path vanishes between the two opens we retry the exclusive create.
    """
    base = os.O_WRONLY | os.O_NOFOLLOW
    if append:
        base |= os.O_APPEND
    last_exc = None
    for _ in range(8):
        try:
            fd = os.open(path, base | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as e:
            last_exc = e
            try:
                fd = os.open(path, base)  # no O_CREAT; O_NOFOLLOW bars a symlink
            except FileNotFoundError:
                continue  # vanished between the two opens; retry the exclusive create
            except OSError as oe:
                if oe.errno == errno.ELOOP:
                    raise PermissionError(f"refusing to write {path}: it is a symlink") from oe
                raise
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise PermissionError(f"refusing to write {path}: not a regular file")
                if st.st_uid != os.getuid():
                    raise PermissionError(
                        f"refusing to write {path}: owned by uid {st.st_uid}, not us"
                    )
                if st.st_nlink != 1:
                    raise PermissionError(
                        f"refusing to write {path}: unexpected hard link (nlink={st.st_nlink})"
                    )
                os.fchmod(fd, 0o600)
                if not append:
                    os.ftruncate(fd, 0)
            except BaseException:
                os.close(fd)
                raise
        try:
            f = os.fdopen(fd, "a" if append else "w")
        except BaseException:
            os.close(fd)
            raise
        with f:
            f.write(text)
        return
    raise PermissionError(f"refusing to write {path}: path kept changing under us") from last_exc


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


def main():
    os.umask(0o077)  # anything this script creates is private to the invoking user

    # LOG_FILE included so the _write_private below can't hit a stale/planted
    # log file before we've logged.
    for f in [URL_FILE, CODE_FILE, RESULT_FILE, LOG_FILE]:
        if os.path.lexists(f):
            os.unlink(f)
    _write_private(LOG_FILE, "")  # create the log fresh at 0600

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

    # The child is its own session leader (setsid), so it will NOT get SIGHUP
    # when we exit. Guarantee it is reaped on ANY exit — including an uncaught
    # PermissionError from _write_private mid-run — so we never orphan a live
    # `claude auth login` holding the pty.
    def _terminate_proc():
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass

    atexit.register(_terminate_proc)

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


if __name__ == "__main__":
    main()
