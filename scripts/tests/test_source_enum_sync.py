#!/usr/bin/env python3
"""Drift guard: shared/HANDSHAKE-SCHEMA.json `source` enum MUST match
dispatch_validator.ALLOWED_SOURCES exactly.

Background: the two lists are hand-maintained in different files with an
"keep these in sync" comment — the classic drift trap the deep-research review
flagged. It has already bitten in production: the `dashboard` source was added
to the validator but the schema (and, earlier, the allowlist itself) lagged,
and dashboard-sourced envelopes were silently dropped to beacon/.invalid for
~12h on 2026-05-27/28, breaking the UI as a control surface.

The validator is the source of truth (it is the actual dispatch gate); the
schema enum mirrors it for documentation + JSON-Schema validation. This test
fails loudly the moment they diverge, so a new source can't be added to one
without the other.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_source_enum_sync
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dispatch_validator as dv  # noqa: E402

_SCHEMA_PATH = _REPO_SCRIPTS.parent / 'shared' / 'HANDSHAKE-SCHEMA.json'


def _schema_source_enum():
    schema = json.loads(_SCHEMA_PATH.read_text())
    return schema['properties']['source']['enum']


class SourceEnumSyncTest(unittest.TestCase):
    def test_schema_enum_matches_validator_allowlist(self):
        schema_enum = set(_schema_source_enum())
        allowed = set(dv.ALLOWED_SOURCES)

        missing_from_schema = allowed - schema_enum
        extra_in_schema = schema_enum - allowed

        self.assertEqual(
            missing_from_schema, set(),
            'Sources in dispatch_validator.ALLOWED_SOURCES but missing from '
            'HANDSHAKE-SCHEMA.json source enum (add them to the schema): '
            f'{sorted(missing_from_schema)}')
        self.assertEqual(
            extra_in_schema, set(),
            'Sources in HANDSHAKE-SCHEMA.json source enum but not in '
            'dispatch_validator.ALLOWED_SOURCES (these would be REJECTED at '
            f'dispatch): {sorted(extra_in_schema)}')

    def test_schema_enum_has_no_duplicates(self):
        enum = _schema_source_enum()
        dupes = sorted({s for s in enum if enum.count(s) > 1})
        self.assertEqual(enum, list(dict.fromkeys(enum)),
                         f'Duplicate entries in schema source enum: {dupes}')


if __name__ == '__main__':
    unittest.main()
