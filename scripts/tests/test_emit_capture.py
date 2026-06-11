#!/usr/bin/env python3
"""Tests for the importable `emit_capture` helper (Contract B §5.1).

Validates:
  - emit_capture() returns the server capture_id on success
  - the POST body carries the allowlisted `label` and the title/note/origin
  - emit_capture() returns None (never raises) on every failure shape:
    missing title, missing/empty token, network error, malformed response
  - the CLI wrapper main() preserves its observable behavior (exit 2 on no
    title, exit 0 + "captured <id>" on success, non-zero on failure)

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_emit_capture
"""

from __future__ import annotations

import json
import os
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
