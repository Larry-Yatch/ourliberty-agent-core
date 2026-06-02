#!/usr/bin/env python3
"""test_alert_translations.py — CI gate for healer-alert translation coverage.

Enforces the discipline added by the 2026-05-26 stopgap translation layer
(operating-manual.md Part II #68): every `larry_alerts.append_alert(...)`
call site in the repo must have a matching entry in
`config/alert-translations.json`, looked up under the longest-prefix rule
implemented by `larry_alerts.translate_alert`.

Fail-forward semantics:
  - Missing translation → test FAILS with the file:line of the call site
    and the (source, subject_prefix) that is missing.
  - Extra translation (entry exists in JSON but no producer emits it) →
    NOT a failure. Future producers can claim pre-existing keys, and
    Larry's CLARIFY response explicitly permits orphan-tolerance.

Mirror's PR #105 forward-looking-scanner shape is the template: scan code,
extract literal call-site facts, assert against current config.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_alert_translations
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import larry_alerts  # noqa: E402


REPO_ROOT = _REPO_SCRIPTS.parent
SCRIPTS_DIR = _REPO_SCRIPTS
TRANSLATIONS_FILE = REPO_ROOT / 'config' / 'alert-translations.json'

# Tier 2 fallback subjects are computed at runtime from a fixed enumeration
# (heal_pipeline_stall.py:866-876). Static analysis can extract the literal
# f-string template but not the {outcome}-{reason} combinations; bind the
# combinations the dispatch's V1 ten explicitly covers here. The producer
# can emit 6 combinations (3 outcomes × 2 reasons) but V1 covers only the
# two most-impactful: failed-rate_limit (both accounts limited) and
# unavailable-auth_401 (Tier 1 auth-expired + Tier 2 unprovisioned).
# The other 4 combinations fall back to the [no translation] footer until
# the table is extended.
_TIER2_FALLBACK_V1_ENUM = [
    ('failed', 'rate_limit'),
    ('unavailable', 'auth_401'),
]


# Subjects produced via runtime-only string-builder helpers that exist in
# the codebase but are NOT directly translatable under V1 — they're
# bookkeeping/dedup subjects, not operator-facing failure subjects, and
# the dispatch's stopgap-table scope is the ten operator-facing subjects.
# Listed here so the test can skip them with a documented reason rather
# than failing on unexpected coverage gaps.
_OUT_OF_V1_SCOPE_SUBJECTS = {
    # heal_pipeline_stall.py:474-475 — no-mirror-dispatch (covered by
    # mirror_marker_invisible from a different angle; flagged for future
    # entry but not in dispatch's V1 ten).
    ('heal-pipeline-stall', 'pipeline-stall:no-mirror-dispatch'),
    # heal_pipeline_stall.py:521-522 — mirror-pass-unmerged.
    ('heal-pipeline-stall', 'pipeline-stall:mirror-pass-unmerged'),
    # heal_pipeline_stall.py:791-792 — unrouted-pr.
    ('heal-pipeline-stall', 'pipeline-stall:unrouted-pr'),
    # heal_credential_registry_drift.py — MISSING_CREDENTIAL (different
    # drift-shape from MISSING_REGISTRY_ENTRY; not in V1).
    ('heal-credential-registry-drift', 'credential-drift:MISSING_CREDENTIAL'),
    # heal_credential_registry_drift.py:634 — activate-healer announcement.
    ('heal-credential-registry-drift',
     'credential-drift-healer: activate to receive drift alerts'),
    # heal_systemd_install_drift.py:220 — activate-healer announcement.
    ('heal-systemd-install-drift',
     'install-drift-healer: activate to receive missing-install alerts'),
    # heal_pr_auto_merge.py + heal_chain_event_type_audit.py +
    # heal_stale_daemon_code.py — out of dispatch's V1 ten. Each has its
    # own producer subjects that future PRs may translate; tracked here so
    # the test doesn't fail on them today.
    ('heal-pr-auto-merge', None),  # wildcard: any subject under this source
    ('heal-chain-event-type-audit', None),
    ('heal-stale-daemon-code', None),
    # outbox_notifier broadcast subjects beyond auto-merge-queue-corrupt
    # (priority broadcasts, beacon-replan fallback path, etc.). Tracked as
    # out of V1 scope; future PRs can add entries.
    ('outbox-notifier', None),
    # beacon-telegram-bot Tier 1/2 fallback failure DMs (pre-date the
    # translation table; V1 ten doesn't include the bot's own
    # quota/auth surface — the heal-pipeline-stall:tier2-fallback-* entries
    # cover the operator-facing version of the same condition).
    ('beacon-telegram-bot', None),
    # ledger weekly + pulse cycle outputs (operator-facing reports, not
    # failure alerts — out of stopgap's failure-translation scope).
    ('ledger', None),
    ('pulse', None),
    # watchdog (infra-monitoring: disk, memory, cgroup, bots). Bot-liveness
    # subjects (bots:<agent>:down, bots:pulse:tmux) DO have translations as
    # of the per-bot liveness-policy PR — see config/alert-translations.json
    # under "watchdog". The wildcard skip stays because the producer's bot
    # subject is dynamic (`bots:{short}:{mode}`) and would static-analyze to
    # prefix 'bots', which the longest-prefix lookup can't resolve without
    # exact-match enumeration. Runtime translation works via direct subject
    # match. Infra subjects (disk, memory, cgroup) still rely on the raw
    # emoji+source rendering.
    ('watchdog', None),
}


def _is_append_alert_call(node: ast.Call) -> bool:
    """True if `node` is a `larry_alerts.append_alert(...)` or
    `la.append_alert(...)` or bare `append_alert(...)` call."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == 'append_alert':
        return True
    if isinstance(func, ast.Name) and func.id == 'append_alert':
        return True
    return False


def _extract_kw_literal(node: ast.Call, name: str) -> object:
    """Return the literal value of keyword argument `name`, or sentinel
    `_DYNAMIC` if the argument is computed (variable, complex expression),
    or `None` if absent."""
    for kw in node.keywords:
        if kw.arg != name:
            continue
        return _evaluate_static(kw.value)
    return None


class _Dynamic:
    """Sentinel — the call-site argument is computed, not a literal we can
    extract via static analysis. Carries the static prefix when extractable
    from an f-string or `.format()` call."""

    def __init__(self, static_prefix: str = ''):
        self.static_prefix = static_prefix

    def __repr__(self) -> str:
        return f'_Dynamic(static_prefix={self.static_prefix!r})'


def _evaluate_static(node: ast.AST) -> object:
    """Best-effort static extraction. Returns:
      - str for a plain string literal.
      - _Dynamic(static_prefix=...) for an f-string or .format() call —
        the static prefix is the literal text before the first dynamic
        substitution.
      - _Dynamic() for any other computed expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string: walk values until we hit a FormattedValue (dynamic).
        prefix_parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                prefix_parts.append(v.value)
            else:
                break
        return _Dynamic(static_prefix=''.join(prefix_parts))
    if isinstance(node, ast.Call):
        # `'literal-{}'.format(x)` shape — extract the literal head.
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == 'format':
            if isinstance(func.value, ast.Constant) and isinstance(
                func.value.value, str,
            ):
                head = func.value.value
                idx = head.find('{')
                return _Dynamic(
                    static_prefix=head if idx < 0 else head[:idx],
                )
        return _Dynamic()
    return _Dynamic()


def _scan_file_for_call_sites(path: Path) -> list[dict]:
    """Return [{file, line, source, subject_literal_or_prefix, is_dynamic}]
    for every `append_alert(...)` call in `path` that includes a `source`
    keyword."""
    out: list[dict] = []
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except (OSError, SyntaxError):
        return out
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_append_alert_call(node)):
            continue
        source = _extract_kw_literal(node, 'source')
        if not isinstance(source, str):
            continue  # source must be a literal string — skip dynamic.
        subject = _extract_kw_literal(node, 'subject')
        if subject is None:
            # No subject keyword — alerts without subjects are not
            # translatable under V1 (no key shape). Skip.
            continue
        if isinstance(subject, str):
            subject_static = subject
            is_dynamic = False
        elif isinstance(subject, _Dynamic):
            subject_static = subject.static_prefix
            is_dynamic = True
            if not subject_static:
                # Fully dynamic with no extractable prefix — can't validate
                # statically. Skip with a warning surface.
                continue
            # Strip trailing colon so the prefix is the static portion
            # before the dynamic suffix (e.g. 'pipeline-stall:forge-no-pr:'
            # → 'pipeline-stall:forge-no-pr').
            if subject_static.endswith(':'):
                subject_static = subject_static[:-1]
        else:
            continue
        out.append({
            'file': str(path.relative_to(REPO_ROOT)),
            'line': node.lineno,
            'source': source,
            'subject_prefix': subject_static,
            'is_dynamic': is_dynamic,
        })
    return out


def _scan_all_producer_call_sites() -> list[dict]:
    """Walk scripts/ for every append_alert call site."""
    out: list[dict] = []
    for py in sorted(SCRIPTS_DIR.glob('*.py')):
        if py.name.startswith('test_'):
            continue
        out.extend(_scan_file_for_call_sites(py))
    return out


def _is_out_of_v1_scope(source: str, subject_prefix: str) -> bool:
    """True if (source, subject_prefix) is documented as out of V1 scope —
    either an exact (source, subject_prefix) entry or a source-wildcard."""
    if (source, None) in _OUT_OF_V1_SCOPE_SUBJECTS:
        return True
    if (source, subject_prefix) in _OUT_OF_V1_SCOPE_SUBJECTS:
        return True
    return False


class TranslationCoverageTest(unittest.TestCase):
    """Every append_alert call site in the in-V1-scope set must resolve to
    a translation entry under the longest-prefix lookup rule.

    Fail message names file:line and (source, subject_prefix) so adding the
    missing entry is mechanical."""

    def test_every_in_scope_call_site_has_translation(self):
        # Force a re-read of the translations file (in case a prior test in
        # this process polluted the module-level cache).
        larry_alerts._TRANSLATIONS_CACHE = None  # noqa: SLF001
        missing: list[str] = []
        for hit in _scan_all_producer_call_sites():
            source = hit['source']
            subject_prefix = hit['subject_prefix']
            if _is_out_of_v1_scope(source, subject_prefix):
                continue
            translation = larry_alerts.translate_alert(source, subject_prefix)
            if translation is None:
                missing.append(
                    f"{hit['file']}:{hit['line']} "
                    f"(source={source!r}, subject_prefix={subject_prefix!r})"
                )
        if missing:
            self.fail(
                'Missing translations in config/alert-translations.json '
                'for the following healer alert call sites — add an entry '
                'per docs/runbooks/add-new-healer-alert.md:\n  - '
                + '\n  - '.join(missing)
            )

    def test_tier2_fallback_enumeration_has_translations(self):
        """The Tier 2 fallback subjects are computed at runtime from a fixed
        enumeration in heal_pipeline_stall.py. Static analysis only sees the
        f-string template; this test enumerates the (outcome, reason) tuples
        explicitly and asserts each maps to a translation entry."""
        larry_alerts._TRANSLATIONS_CACHE = None  # noqa: SLF001
        missing: list[str] = []
        for outcome, reason in _TIER2_FALLBACK_V1_ENUM:
            subject = (
                f'pipeline-stall:tier2-fallback-{outcome}-{reason}:agent-x'
            )
            translation = larry_alerts.translate_alert(
                'heal-pipeline-stall', subject,
            )
            if translation is None:
                missing.append(f'(outcome={outcome}, reason={reason})')
        if missing:
            self.fail(
                'Missing Tier 2 fallback translations — entries needed for: '
                + ', '.join(missing)
                + ' (template: pipeline-stall:tier2-fallback-<outcome>-<reason>)'
            )


class TranslationFileShapeTest(unittest.TestCase):
    """Sanity checks on the JSON: every entry has the expected three fields
    with non-empty values, severity is from the allowed vocabulary."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(TRANSLATIONS_FILE.read_text(encoding='utf-8'))

    def test_schema_block_present(self):
        self.assertIn('_schema', self.data)

    def test_every_entry_has_required_fields(self):
        allowed_severities = {'URGENT', 'WARNING', 'INFO'}
        allowed_tiers = {'NOW', 'SOON', 'FYI'}
        for source, entries in self.data.items():
            if source.startswith('_'):
                continue
            self.assertIsInstance(entries, dict, f'source={source}')
            for subject, entry in entries.items():
                self.assertIsInstance(entry, dict,
                                      f'{source}:{subject}')
                for field in (
                    'severity', 'tier', 'plain_language_summary',
                    'recommended_action',
                ):
                    self.assertIn(field, entry, f'{source}:{subject}')
                    self.assertTrue(
                        entry[field].strip(),
                        f'{source}:{subject} {field} is empty',
                    )
                self.assertIn(
                    entry['severity'], allowed_severities,
                    f'{source}:{subject} severity={entry["severity"]!r} '
                    f'not in {allowed_severities}',
                )
                self.assertIn(
                    entry['tier'], allowed_tiers,
                    f'{source}:{subject} tier={entry["tier"]!r} '
                    f'not in {allowed_tiers}',
                )


class LookupRuleTest(unittest.TestCase):
    """Pin the lookup semantics: exact match, longest-prefix fallback,
    unmatched returns None."""

    def setUp(self):
        larry_alerts._TRANSLATIONS_CACHE = None  # noqa: SLF001

    def test_exact_match_static_subject(self):
        t = larry_alerts.translate_alert(
            'heal-chain-event-shipper-heartbeat', 'chain-event-shipper-stale',
        )
        self.assertIsNotNone(t)
        self.assertEqual(t['severity'], 'URGENT')

    def test_longest_prefix_for_dynamic_suffix(self):
        t = larry_alerts.translate_alert(
            'heal-pipeline-stall',
            'pipeline-stall:forge-no-pr:wt-forge-some-task',
        )
        self.assertIsNotNone(t)
        self.assertEqual(t['severity'], 'URGENT')

    def test_install_drift_with_unit_suffix(self):
        t = larry_alerts.translate_alert(
            'heal-systemd-install-drift', 'install-drift:my-daemon.service',
        )
        self.assertIsNotNone(t)
        self.assertEqual(t['severity'], 'URGENT')

    def test_unmatched_returns_none(self):
        t = larry_alerts.translate_alert(
            'heal-pipeline-stall', 'nonexistent:subject:here',
        )
        self.assertIsNone(t)

    def test_unknown_source_returns_none(self):
        t = larry_alerts.translate_alert(
            'totally-fake-source', 'chain-event-shipper-stale',
        )
        self.assertIsNone(t)

    def test_subject_none_returns_none(self):
        self.assertIsNone(
            larry_alerts.translate_alert('heal-pipeline-stall', None)
        )


class FormatDmTranslationTest(unittest.TestCase):
    """End-to-end: format_dm must produce the layered DM shape on match and
    the raw+footer shape on miss. Severity HEADER on matched alerts MUST
    be a plain word with no leading/trailing emoji."""

    def setUp(self):
        larry_alerts._TRANSLATIONS_CACHE = None  # noqa: SLF001

    def test_matched_alert_layered_shape(self):
        text = larry_alerts.format_dm({
            'source': 'heal-pipeline-stall',
            'subject': 'pipeline-stall:forge-no-pr:wt-forge-foo',
            'severity': 'warning',
            'message': 'raw producer message',
            'suggested_action': 'do thing',
        })
        lines = text.split('\n')
        # First line is the tier header (glyph + label + subject).
        self.assertTrue(
            lines[0].startswith('🔴 NOW · '),
            f'expected tier-NOW header on line 0, got {lines[0]!r}',
        )
        self.assertIn('pipeline-stall:forge-no-pr:wt-forge-foo', lines[0])
        # Second line is the severity word, no emoji.
        self.assertEqual(lines[1], 'URGENT')
        self.assertNotIn('🚨', lines[1])
        self.assertNotIn('⚠', lines[1])
        # Technical-detail footer is present and includes the raw body.
        self.assertIn('---technical detail---', text)
        self.assertIn('raw producer message', text)
        self.assertIn('Run: do thing', text)

    def test_tier_glyph_renders_for_every_translation(self):
        """Mirror-review focus: every translation entry must render a tier
        glyph + label as the first line. Confirms tier mapping is wired end
        to end and the severity word remains plain on line 1."""
        data = json.loads(TRANSLATIONS_FILE.read_text(encoding='utf-8'))
        allowed_severities = {'URGENT', 'WARNING', 'INFO'}
        tier_to_glyph = {'NOW': '🔴', 'SOON': '🟡', 'FYI': '⚪'}
        for source, entries in data.items():
            if source.startswith('_'):
                continue
            for subject, entry in entries.items():
                text = larry_alerts.format_dm({
                    'source': source,
                    'subject': subject,
                    'severity': 'warning',
                    'message': 'm',
                })
                lines = text.split('\n')
                expected_glyph = tier_to_glyph[entry['tier']]
                self.assertTrue(
                    lines[0].startswith(
                        f'{expected_glyph} {entry["tier"]}'),
                    f'first line of matched DM for {source}:{subject} '
                    f'was {lines[0]!r}, expected to start with '
                    f'{expected_glyph} {entry["tier"]}',
                )
                self.assertIn(
                    lines[1], allowed_severities,
                    f'second line of matched DM for {source}:{subject} '
                    f'was {lines[1]!r}, expected one of {allowed_severities}',
                )

    def test_unmatched_alert_has_no_translation_footer(self):
        text = larry_alerts.format_dm({
            'source': 'unknown-healer',
            'subject': 'unknown-subject',
            'severity': 'warning',
            'message': 'raw',
        })
        self.assertIn('[no translation', text)
        # The raw body is still present.
        self.assertIn('unknown-healer', text)
        self.assertIn('raw', text)

    def test_alert_without_subject_falls_back_gracefully(self):
        """Producers that omit `subject` (allowed by append_alert signature)
        get the [no translation] footer rather than crashing the renderer."""
        text = larry_alerts.format_dm({
            'source': 'watchdog',
            'severity': 'critical',
            'message': 'CRITICAL 95%',
        })
        self.assertIn('[no translation', text)
        self.assertIn('CRITICAL 95%', text)


class TranslationCacheReloadTest(unittest.TestCase):
    """_load_translations must pick up config edits without a process restart.

    Regression: the Beacon bot runs for days, and a once-per-process cache
    left a newly added translation entry rendering `[no translation]` until
    the bot was restarted — exactly what happened to the stuck-timer entry
    (added 2026-05-31, still rendering raw on 2026-06-01 because Beacon had
    been up since 2026-05-30). Cache is now keyed on the file's mtime.
    """

    def setUp(self):
        self._orig_file = larry_alerts.TRANSLATIONS_FILE
        self._orig_cache = larry_alerts._TRANSLATIONS_CACHE  # noqa: SLF001
        self._orig_mtime = larry_alerts._TRANSLATIONS_MTIME  # noqa: SLF001

    def tearDown(self):
        larry_alerts.TRANSLATIONS_FILE = self._orig_file
        larry_alerts._TRANSLATIONS_CACHE = self._orig_cache  # noqa: SLF001
        larry_alerts._TRANSLATIONS_MTIME = self._orig_mtime  # noqa: SLF001

    def _point_at(self, path):
        larry_alerts.TRANSLATIONS_FILE = path
        larry_alerts._TRANSLATIONS_CACHE = None  # noqa: SLF001
        larry_alerts._TRANSLATIONS_MTIME = None  # noqa: SLF001

    def test_edit_is_picked_up_on_mtime_change(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'alert-translations.json'
            p.write_text(json.dumps({'src': {'subj': {'severity': 'INFO'}}}))
            os.utime(p, (1000, 1000))
            self._point_at(p)
            self.assertEqual(
                larry_alerts.translate_alert('src', 'subj'),
                {'severity': 'INFO'})
            # Edit + bump mtime -> the next lookup must reflect the edit.
            p.write_text(json.dumps({'src': {'subj': {'severity': 'URGENT'}}}))
            os.utime(p, (2000, 2000))
            self.assertEqual(
                larry_alerts.translate_alert('src', 'subj'),
                {'severity': 'URGENT'})

    def test_unchanged_mtime_serves_cache(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'alert-translations.json'
            p.write_text(json.dumps({'src': {'subj': {'severity': 'INFO'}}}))
            os.utime(p, (1000, 1000))
            self._point_at(p)
            larry_alerts.translate_alert('src', 'subj')
            # Rewrite content but KEEP the mtime -> cache must win (no re-read).
            p.write_text(json.dumps({'src': {'subj': {'severity': 'URGENT'}}}))
            os.utime(p, (1000, 1000))
            self.assertEqual(
                larry_alerts.translate_alert('src', 'subj'),
                {'severity': 'INFO'})


if __name__ == '__main__':
    unittest.main()
