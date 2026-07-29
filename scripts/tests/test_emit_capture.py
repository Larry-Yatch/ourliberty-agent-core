#!/usr/bin/env python3
"""Tests for the importable `emit_capture` helper (Contract B §5.1).

Validates:
  - emit_capture() returns the server capture_id on success
  - the POST body carries the allowlisted `label` and the title/note/origin
  - emit_capture() returns None (never raises) on every failure shape:
    missing title, missing/empty token, network error, malformed response
  - the CLI wrapper main() preserves its observable behavior (exit 2 on no
    title, exit 0 + "captured <id>" on success, non-zero on failure)
  - the emit_capture.sh shell wrapper REFUSES a flag-style call (`--title ...`)
    instead of silently filing a capture titled "--title"

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_emit_capture
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import emit_capture_impl as eci  # noqa: E402


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _token_env(d: str, token: str = "secret-token") -> dict[str, str]:
    tok = Path(d) / "ingest-token"
    tok.write_text(token, encoding="utf-8")
    return {
        "OL_INGEST_TOKEN_FILE": str(tok),
        "OL_DASHBOARD_API_URL": "https://example.test",
    }


class EmitCaptureSuccessTests(unittest.TestCase):
    def test_returns_capture_id_and_posts_label(self):
        seen: dict = {}

        def fake_urlopen(req, timeout=None):
            seen["body"] = req.data
            seen["url"] = req.full_url
            seen["token"] = req.get_header("X-ingest-token")
            return _FakeResp(b'{"capture_id": "cap-xyz"}')

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _token_env(d), clear=False), \
                 mock.patch.object(eci.urllib.request, "urlopen", fake_urlopen):
                cid = eci.emit_capture(
                    title="A recurring proposal",
                    note="impact\n\nrationale",
                    source="agent",
                    label="pulse-check-i",
                )

        self.assertEqual(cid, "cap-xyz")
        body = json.loads(seen["body"])
        self.assertEqual(body["title"], "A recurring proposal")
        self.assertEqual(body["note"], "impact\n\nrationale")
        self.assertEqual(body["label"], "pulse-check-i")
        self.assertEqual(body["origin"]["source"], "agent")
        self.assertEqual(seen["token"], "secret-token")
        self.assertTrue(seen["url"].endswith("/api/ingest/capture"))

    def test_label_defaults_to_none_in_body(self):
        seen: dict = {}

        def fake_urlopen(req, timeout=None):
            seen["body"] = req.data
            return _FakeResp(b'{"capture_id": "cap-1"}')

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _token_env(d), clear=False), \
                 mock.patch.object(eci.urllib.request, "urlopen", fake_urlopen):
                cid = eci.emit_capture(title="t")

        self.assertEqual(cid, "cap-1")
        self.assertIsNone(json.loads(seen["body"])["label"])


class EmitCaptureFailureTests(unittest.TestCase):
    def test_no_title_returns_none(self):
        self.assertIsNone(eci.emit_capture(title="   "))

    def test_missing_token_returns_none(self):
        env = {"OL_INGEST_TOKEN_FILE": "/nonexistent/dir/token"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertIsNone(eci.emit_capture(title="t", label="pulse-check-i"))

    def test_empty_token_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _token_env(d, token=""),
                                 clear=False):
                self.assertIsNone(eci.emit_capture(title="t"))

    def test_network_error_returns_none_not_raises(self):
        def boom(req, timeout=None):
            raise OSError("connection refused")

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _token_env(d), clear=False), \
                 mock.patch.object(eci.urllib.request, "urlopen", boom):
                self.assertIsNone(eci.emit_capture(title="t"))

    def test_malformed_response_returns_none(self):
        def fake_urlopen(req, timeout=None):
            return _FakeResp(b'{"no_capture_id": true}')

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _token_env(d), clear=False), \
                 mock.patch.object(eci.urllib.request, "urlopen", fake_urlopen):
                self.assertIsNone(eci.emit_capture(title="t"))

    def test_http_error_surfaces_response_body(self):
        # A 4xx must report the response BODY (the endpoint's actionable
        # detail), not just "HTTP Error 400: Bad Request" — the whole point of
        # this fix. e.g. the ingest endpoint allow-lists origin.source.
        import io as _io

        def raise_http_error(req, timeout=None):
            raise eci.urllib.error.HTTPError(
                url="http://x/api/ingest/capture", code=400, msg="Bad Request",
                hdrs=None,
                fp=_io.BytesIO(b'{"detail":"invalid origin.source=\'x\'"}'),
            )

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _token_env(d), clear=False), \
                 mock.patch.object(eci.urllib.request, "urlopen",
                                   raise_http_error), \
                 mock.patch.object(eci, "_err") as err:
                self.assertIsNone(eci.emit_capture(title="t"))
        msgs = " ".join(str(c.args[0]) for c in err.call_args_list if c.args)
        self.assertIn("invalid origin.source", msgs)  # body surfaced
        self.assertIn("400", msgs)                     # status still shown

    def test_http_error_body_read_failure_still_reports(self):
        # If the body can't be read, we still report the status (never crash).
        class _BadFp:
            def read(self):
                raise OSError("body unreadable")

        def raise_http_error(req, timeout=None):
            raise eci.urllib.error.HTTPError(
                url="http://x", code=500, msg="err", hdrs=None, fp=_BadFp())

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _token_env(d), clear=False), \
                 mock.patch.object(eci.urllib.request, "urlopen",
                                   raise_http_error), \
                 mock.patch.object(eci, "_err") as err:
                self.assertIsNone(eci.emit_capture(title="t"))
        msgs = " ".join(str(c.args[0]) for c in err.call_args_list if c.args)
        self.assertIn("500", msgs)


class ShellWrapperFlagGuardTests(unittest.TestCase):
    """emit_capture.sh takes POSITIONAL args; a flag-style call must REFUSE.

    `--title X --note Y --repo Z` used to exit 0 having filed a capture titled
    "--title", with the real title shifted into the note and --repo silently
    dropped (there is no such option — origin comes from the cwd's git
    context). Three such cards are in captures.json.

    The emitter is stubbed with a fake `python3` on PATH that records the env
    it was handed, so each case proves whether a POST would have happened at
    all — not merely what the exit code was.
    """

    SCRIPT = _REPO_SCRIPTS / "emit_capture.sh"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.marker = tmp / "emitter-ran.json"
        shim_dir = tmp / "bin"
        shim_dir.mkdir()
        shim = shim_dir / "python3"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s" "{\\"title\\": \\"$OL_CAPTURE_TITLE\\","'
            ' > "$OL_MARKER"\n'
            'printf "%s" "\\"note\\": \\"$OL_CAPTURE_NOTE\\","'
            ' >> "$OL_MARKER"\n'
            'printf "%s" "\\"argv\\": \\"$*\\"}" >> "$OL_MARKER"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        self.shim_dir = shim_dir

    def _run(self, *args, env_extra=None):
        env = dict(os.environ)
        env.pop("OL_CAPTURE_TITLE", None)
        env.pop("OL_CAPTURE_NOTE", None)
        env["PATH"] = f"{self.shim_dir}{os.pathsep}{env['PATH']}"
        env["OL_MARKER"] = str(self.marker)
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", str(self.SCRIPT), *args],
            capture_output=True, text=True, timeout=30, env=env,
        )

    def _emitted(self):
        """What the emitter was invoked with, or None if it never ran."""
        if not self.marker.exists():
            return None
        return json.loads(self.marker.read_text(encoding="utf-8"))

    # --- the guard fires -------------------------------------------------
    def test_flag_style_call_exits_nonzero_and_posts_nothing(self):
        out = self._run("--title", "Real title", "--note", "Body",
                        "--repo", "SOMEREPO")
        self.assertNotEqual(out.returncode, 0)
        self.assertIsNone(self._emitted(), "guard must fire BEFORE the emitter")
        self.assertEqual(out.stdout, "")
        # The usage names the real convention AND the other half of the
        # mistake: repo comes from the cwd, not from an argument.
        self.assertIn('usage: emit_capture.sh "<title>" ["<note>"]', out.stderr)
        self.assertIn("--repo", out.stderr)
        self.assertRegex(out.stderr, r"(?i)current directory")

    def test_lone_flag_is_also_refused(self):
        out = self._run("--help")
        self.assertNotEqual(out.returncode, 0)
        self.assertIsNone(self._emitted())

    # --- the guard stays out of the way ----------------------------------
    def test_positional_title_and_note_still_emit(self):
        out = self._run("A real title", "A real note")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(
            self._emitted(),
            {"title": "A real title", "note": "A real note",
             "argv": str(_REPO_SCRIPTS / "emit_capture_impl.py")},
        )

    def test_env_title_with_no_positional_still_emits(self):
        # The `"${1:-${OL_CAPTURE_TITLE:-}}"` fallback path — $1 is absent, so
        # the guard must not even look at it.
        out = self._run(env_extra={"OL_CAPTURE_TITLE": "From env"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((self._emitted() or {}).get("title"), "From env")

    def test_title_containing_dashes_is_not_mistaken_for_a_flag(self):
        # Only a LEADING `--` is a flag; an em-dash-ish title is legitimate.
        out = self._run("Fix the --verbose handling in run.sh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((self._emitted() or {}).get("title"),
                         "Fix the --verbose handling in run.sh")


class CliWrapperTests(unittest.TestCase):
    def test_main_no_title_returns_2(self):
        env = {k: v for k, v in os.environ.items()}
        env.pop("OL_CAPTURE_TITLE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(eci.main(), 2)

    def test_main_success_returns_0(self):
        with mock.patch.object(eci, "emit_capture", return_value="cap-1"), \
             mock.patch.dict(os.environ, {"OL_CAPTURE_TITLE": "t"},
                             clear=False):
            self.assertEqual(eci.main(), 0)

    def test_main_failure_returns_nonzero(self):
        with mock.patch.object(eci, "emit_capture", return_value=None), \
             mock.patch.dict(os.environ, {"OL_CAPTURE_TITLE": "t"},
                             clear=False):
            self.assertNotEqual(eci.main(), 0)


if __name__ == "__main__":
    unittest.main()
