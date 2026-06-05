"""Tier 1 headless Claude OAuth orchestrator.

ADMIN-ONLY, ONE-SHOT recovery tool. Run manually from chat when the Tier 1
account hits auth-401 (see the recovery strings in agent_runner.py /
beacon_telegram_bot.py). It spawns `claude auth login` under a pty, scrapes the
device-code URL for Larry, waits for him to drop the pasted code at CODE_FILE,
injects it, and records the result. It is NOT part of the runtime; no daemon
reads these files.

Secret-handling hardening (deep-research review follow-up + 2026-06-05 audit):
the four /tmp paths below are predictable and world-guessable. Every file this
script creates is forced to 0600 via umask and created **exclusively**
(O_CREAT|O_EXCL|O_NOFOLLOW) so a pre-planted symlink OR a pre-planted
attacker-owned regular file at a fixed /tmp path can't capture our write; if the
path already exists we refuse unless it is a regular file we own (mirroring the
read side). The operator-supplied CODE_FILE is likewise read with O_NOFOLLOW +
regular-file + same-owner checks before we trust it. The predictable PATHS are
kept intentionally — they are the human-facing convention (Larry writes the code
to CODE_FILE, reads the URL from URL_FILE); only the file HANDLING is hardened.

Import is side-effect-free: the executable flow lives under main()/__main__, so
the helpers can be unit-tested without spawning `claude auth login`.
"""
import os, pty, subprocess, time, select, sys, re, stat, errno, shutil, atexit

EMAIL = "agent.beacon.ourliberty@gmail.com"
URL_FILE = "/tmp/auth-url.txt"
CODE_FILE = "/tmp/auth-code.txt"
RESULT_FILE = "/tmp/auth-result.txt"
LOG_FILE = "/tmp/auth-debug.log"


def _write_private(path, text, append=False):
    """Write `text` to `path` at 0600, refusing to leak into a file we did not
    create or do not own.

    Hardening against the predictable-/tmp-path attacks this tool is exposed to:
      * O_CREAT|O_EXCL creates the file ourselves when it does not exist (the
        normal case after the startup unlink).
      * If it already exists (a stale file from a prior run, or something planted
        in the window between our startup unlink and now) we re-open WITHOUT
        O_CREAT and refuse unless it is a *regular* file, *owned by us*, with
        *link count 1*, then re-assert 0600. That rejects, in order: a planted
        symlink (O_NOFOLLOW → ELOOP), a foreign-owned file (st_uid), and a
        same-uid *hardlink* swap of one of our own files (st_nlink != 1) — the
        last of which O_NOFOLLOW alone does NOT catch.
      * If the path vanishes between the two opens we retry the exclusive create
        rather than crash, preserving the old create-or-open guarantee.

    This is best-effort hardening of an admin-only one-shot tool whose paths are
    a human-facing convention (so they can't move to a private dir); it closes
    the symlink, foreign-file, and hardlink redirect vectors but is not a
    substitute for an unguessable path.
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
    # The path kept vanishing under us across every retry — treat active
    # interference as a refusal rather than spin or write blindly.
    raise PermissionError(f"refusing to write {path}: path kept changing under us") from last_exc


def log(msg):
    _write_private(LOG_FILE, f"[{time.time():.2f}] {msg}\n", append=True)


def _maybe_restore_credentials(cred_backup, cred_file, log_fn=log):
    """Restore the moved-aside credentials iff we made a backup and no fresh
    credentials file exists — i.e. the run aborted (URL scrape failed, code
    timed out, crash) before `claude auth login` wrote a new file. Without this,
    a failed run leaves the Tier 1 account with NO credentials. A successful run
    leaves a fresh cred_file in place, so we keep our hands off it. Returns True
    iff a restore actually happened."""
    if cred_backup and not os.path.exists(cred_file):
        try:
            shutil.move(cred_backup, cred_file)
            log_fn("restored pre-orchestrator credentials (run did not complete a fresh login)")
            return True
        except OSError as e:
            log_fn(f"WARN: could not restore credentials backup {cred_backup!r}: {e}")
    return False


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


def main():
    os.umask(0o077)  # anything this script creates is private to the invoking user

    # Start clean. lexists() also catches a dangling/planted symlink; unlink
    # removes the link itself (not its target), so this is safe. LOG_FILE is
    # included so the _write_private below can't hit a stale/planted log file
    # before we've logged anything.
    for f in [URL_FILE, CODE_FILE, RESULT_FILE, LOG_FILE]:
        if os.path.lexists(f):
            os.unlink(f)
    _write_private(LOG_FILE, "")  # create the log fresh at 0600

    cred_file = os.path.expanduser("~/.claude/.credentials.json")
    cred_backup = None
    if os.path.exists(cred_file):
        cred_backup = cred_file + ".pre-orchestrator-" + str(int(time.time()))
        shutil.move(cred_file, cred_backup)
        log("moved existing credentials aside")

    # Restore on any exit (incl. sys.exit / unhandled exception) that didn't
    # produce a fresh credentials file.
    atexit.register(_maybe_restore_credentials, cred_backup, cred_file)

    log("spawning claude auth login")
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["claude", "auth", "login", "--claudeai", "--email", EMAIL],
        stdin=slave, stdout=slave, stderr=slave,
        preexec_fn=os.setsid,
    )
    os.close(slave)

    # The child is its own session leader (setsid), so it will NOT get SIGHUP
    # when we exit. Guarantee it is reaped on ANY exit — including an uncaught
    # PermissionError from _write_private mid-run — so we never orphan a live
    # `claude auth login` holding the pty. Registered after the credential
    # restore so (LIFO) the child is killed before credentials are touched.
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

    # NOTE: the result buffer is the raw post-auth terminal output and may
    # contain token material; RESULT_FILE is written 0600 so it is not
    # world-readable.
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
