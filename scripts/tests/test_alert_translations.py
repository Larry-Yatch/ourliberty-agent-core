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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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
    # ---- Pre-existing wrapper-surfaced debt (2026-06 belt-and-suspenders) ----
    # The wrapper-aware scanner (see _discover_notify_wrappers) follows one
    # level of indirection through subject-forwarding helpers (_dm_larry,
    # _emit, dm_larry, ...). That newly made VISIBLE a set of alert subjects in
    # other healers that route their subject through such a wrapper and have no
    # translation yet — they were invisible to the old direct-call-only scan,
    # not absent. These sources have zero entries in alert-translations.json
    # today, so wildcarding them removes no existing coverage; it converts an
    # invisible blind spot into a tracked TODO. Translate per-source in future
    # PRs and drop the wildcard. (heal-pr-auto-merge / heal-chain-event-type-
    # audit / watchdog wrapper subjects are already covered by their wildcards
    # above.)
    ('build-sequence-advancer', None),
    ('heal-build-sequence-advancer-heartbeat', None),
    ('heal-phantom-dispatch-claim', None),
    ('heal-resume-paused-on-tier1', None),
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


def _normalize_subject(subject: object) -> Optional[tuple]:
    """Turn an extracted subject value into (subject_prefix, is_dynamic), or
    None if it can't be validated statically (no subject / fully dynamic with
    no literal prefix). Shared by the direct-call and wrapper-call scanners."""
    if subject is None:
        # No subject — alerts without subjects are not translatable under V1
        # (no key shape).
        return None
    if isinstance(subject, str):
        return (subject, False)
    if isinstance(subject, _Dynamic):
        prefix = subject.static_prefix
        if not prefix:
            # Fully dynamic with no extractable prefix — can't validate.
            return None
        # Strip trailing colon so the prefix is the static portion before the
        # dynamic suffix (e.g. 'pipeline-stall:forge-no-pr:' →
        # 'pipeline-stall:forge-no-pr').
        if prefix.endswith(':'):
            prefix = prefix[:-1]
        return (prefix, True)
    return None


def _discover_notify_wrappers(tree: ast.AST) -> dict:
    """Find subject-forwarding wrapper functions in a parsed module.

    A wrapper is a function whose body calls `append_alert(source=<literal>,
    subject=<param>)` where <param> is one of the function's own parameters.
    The literal subject then lives at the wrapper's *call sites*, not the
    `append_alert` call (whose `subject=<var>` is opaque to static analysis) —
    this is the indirection that let wedged-review-silent ship untranslated.

    Returns: {func_name: (source, subject_param_index)}.
    """
    wrappers: dict = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [a.arg for a in fn.args.args]
        for call in ast.walk(fn):
            if not (isinstance(call, ast.Call) and _is_append_alert_call(call)):
                continue
            source = _extract_kw_literal(call, 'source')
            if not isinstance(source, str):
                continue
            subj = next(
                (kw.value for kw in call.keywords if kw.arg == 'subject'), None)
            if isinstance(subj, ast.Name) and subj.id in params:
                wrappers[fn.name] = (source, params.index(subj.id))
    return wrappers


def _discover_wrapper_aliases(tree: ast.AST, wrappers: dict) -> dict:
    """Resolve one level of dependency-injection aliasing: a function param
    whose default value is a known wrapper (e.g. run_cycle's
    `escalate_notify: Callable = _escalate_notify`) is, at that wrapper's call
    sites, an alias for it. Returns {param_name: (source, subject_index)}."""
    aliases: dict = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Positional/positional-or-keyword params: defaults align to the tail.
        args = fn.args.args
        defaults = fn.args.defaults
        pairs = list(zip(args[len(args) - len(defaults):], defaults))
        # Keyword-only params (after `*`, as run_cycle uses for its injected
        # notify hooks): kw_defaults aligns 1:1 with kwonlyargs, None = no
        # default. Missing these is what hid wedged-review's DI indirection.
        pairs += [
            (a, d) for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults)
            if d is not None
        ]
        for arg, default in pairs:
            if isinstance(default, ast.Name) and default.id in wrappers:
                aliases[arg.arg] = wrappers[default.id]
    return aliases


def _wrapper_call_subject(call: ast.Call, subject_index: int) -> object:
    """Extract the subject argument from a wrapper call — positional at
    `subject_index`, or by `subject=` keyword. Returns the static-eval result
    (str / _Dynamic / None)."""
    if subject_index < len(call.args):
        return _evaluate_static(call.args[subject_index])
    for kw in call.keywords:
        if kw.arg == 'subject':
            return _evaluate_static(kw.value)
    return None


def _scan_file_for_call_sites(path: Path) -> list[dict]:
    """Return [{file, line, source, subject_prefix, is_dynamic}] for every
    translatable alert emission in `path` — both direct `append_alert(...)`
    calls and calls to subject-forwarding wrapper functions (one level of
    indirection, plus DI-default aliases)."""
    out: list[dict] = []
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except (OSError, SyntaxError):
        return out

    wrappers = _discover_notify_wrappers(tree)
    callable_wrappers = {**wrappers, **_discover_wrapper_aliases(tree, wrappers)}

    def _emit(source: str, subject_value: object, lineno: int) -> None:
        norm = _normalize_subject(subject_value)
        if norm is None:
            return
        out.append({
            'file': str(path.relative_to(REPO_ROOT)),
            'line': lineno,
            'source': source,
            'subject_prefix': norm[0],
            'is_dynamic': norm[1],
        })

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_append_alert_call(node):
            source = _extract_kw_literal(node, 'source')
            if not isinstance(source, str):
                continue  # source must be a literal string — skip dynamic.
            _emit(source, _extract_kw_literal(node, 'subject'), node.lineno)
            continue
        # Wrapper / alias call — recover the literal subject from the caller.
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None)
        if name in callable_wrappers:
            source, subject_index = callable_wrappers[name]
            _emit(source, _wrapper_call_subject(node, subject_index), node.lineno)
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

    def test_wedged_review_subjects_surface_through_wrappers(self):
        """Belt-and-suspenders regression guard. heal_wedged_review_sessions.py
        emits every subject through the closure_notify / escalate_notify
        indirection, so the literal subject lives at the wrapper call site —
        invisible to a direct-append_alert-only scan. That blind spot let
        wedged-review-silent ship with a hyphen instead of a colon, so it never
        matched the longest-prefix lookup ('[no translation]'). Assert the
        wrapper-aware scanner now surfaces each wedged-review subject AND that
        each resolves — reverting the colon fix re-prefixes 'wedged-review-
        silent-' and re-reds this gate."""
        larry_alerts._TRANSLATIONS_CACHE = None  # noqa: SLF001
        path = SCRIPTS_DIR / 'heal_wedged_review_sessions.py'
        surfaced = {
            h['subject_prefix']
            for h in _scan_file_for_call_sites(path)
            if h['source'] == 'heal-wedged-review-sessions'
        }
        for expected in (
            'wedged-review-reaped',
            'wedged-review-silent',
            'wedged-review-case2-graduated',
        ):
            self.assertIn(
                expected, surfaced,
                f'wrapper-aware scan failed to surface {expected!r} from '
                'heal_wedged_review_sessions.py — wrapper resolution regressed '
                '(or the producer subject changed shape).',
            )
            self.assertIsNotNone(
                larry_alerts.translate_alert(
                    'heal-wedged-review-sessions', expected),
                f'{expected!r} surfaced but has no translation entry.',
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

    def test_sentinel_inbox_stall_full_path_subject(self):
        t = larry_alerts.translate_alert(
            'sentinel',
            'inbox-stall:/tmp/foo/beacon/foo.json',
        )
        self.assertIsNotNone(t)
        self.assertEqual(t['severity'], 'INFO')
        self.assertEqual(t['tier'], 'FYI')

    def test_sentinel_in_flight_stall_full_path_subject(self):
        # dispatch_sentinel emits subject='in-flight-stall:<full path>' (the
        # path uses '/', so the only viable key is the stripped prefix
        # 'in-flight-stall'). The entry must be the silenceable Tier-3 shape:
        # INFO/FYI with NO never_silence, so Check 0 mutes it to the digest
        # (the reaper auto-recovers both wt-mirror-* and wt-forge-* stalls).
        t = larry_alerts.translate_alert(
            'sentinel',
            'in-flight-stall:/tmp/in-flight/build-xyz.json',
        )
        self.assertIsNotNone(t)
        self.assertEqual(t['severity'], 'INFO')
        self.assertEqual(t['tier'], 'FYI')
        self.assertFalse(
            t.get('never_silence'),
            'sentinel in-flight-stall must stay silenceable (Tier-3 to digest) '
            '— a never_silence flag would force-surface auto-remediated noise',
        )

    def test_pulse_check_stale_id_suffix_translates(self):
        # Decision 4: the watcher emits pulse-check-stale:<id>; the id-suffixed
        # subject must strip back to the 'pulse-check-stale' entry and render.
        t = larry_alerts.translate_alert(
            'heal-pulse-check-staleness', 'pulse-check-stale:iv',
        )
        self.assertIsNotNone(t)
        self.assertEqual(t['tier'], 'SOON')
        self.assertTrue(t.get('never_silence'),
                        'dark-check translation must carry never_silence')

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


class OutcomeLanguageTest(unittest.TestCase):
    """Fix-first/notify-on-outcome (2026-06-03): translation entries for
    SUCCESSFUL-heal subjects describe a completed outcome, so they must NOT
    carry an operator imperative ("go run X"). The heal already happened —
    a "sudo ..." line would tell Larry to redo work the team already did.

    These subjects are the closure/digest-routed ones whose translation is the
    fallback render (`_render_outcome_alert` prefers the producer's own message,
    but the table entry is the safety net). Scanning the table here keeps the
    no-imperative contract enforced at the config layer."""

    # (source, subject) pairs that describe a heal that already succeeded.
    _OUTCOME_SUBJECTS = [
        ('heal-systemd-install-drift', 'install-healed'),
        ('heal-systemd-install-drift', 'stuck-timer-healed'),
        ('heal-stale-daemon-code', 'auto-restarted'),
    ]

    # Tokens that signal an operator imperative — none belongs in an
    # already-healed outcome entry.
    _IMPERATIVE_TOKENS = ('sudo ', 'run `', 'systemctl restart', 'git ')

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(TRANSLATIONS_FILE.read_text(encoding='utf-8'))

    def test_outcome_entries_exist(self):
        for source, subject in self._OUTCOME_SUBJECTS:
            self.assertIn(source, self.data, f'missing source {source!r}')
            self.assertIn(
                subject, self.data[source],
                f'missing outcome entry {source}:{subject}')

    def test_outcome_entries_have_no_imperative(self):
        offenders: list[str] = []
        for source, subject in self._OUTCOME_SUBJECTS:
            entry = self.data.get(source, {}).get(subject, {})
            blob = (
                f"{entry.get('plain_language_summary', '')} "
                f"{entry.get('recommended_action', '')}"
            ).lower()
            for token in self._IMPERATIVE_TOKENS:
                if token in blob:
                    offenders.append(f'{source}:{subject} contains {token!r}')
        if offenders:
            self.fail(
                'Outcome (already-healed) translation entries must not carry '
                'an operator imperative:\n  - ' + '\n  - '.join(offenders))

    def test_outcome_entries_render_without_imperative_via_format_dm(self):
        """End-to-end: a closure/digest record renders the self-healed shape
        with NO 'Run:' line and NO 'sudo', even when the producer wrote a
        message (the message wins) AND when it didn't (table summary is the
        fallback)."""
        larry_alerts._TRANSLATIONS_CACHE = None  # noqa: SLF001
        # No producer message → falls back to the table summary.
        text = larry_alerts.format_dm({
            'source': 'heal-systemd-install-drift',
            'subject': 'install-healed:my-daemon.service',
            'severity': 'warning',
            'route': 'closure',
            'suggested_action': 'sudo cp systemd/x /etc/systemd/system/',
        })
        self.assertIn('Self-healed', text)
        self.assertIn('No action needed', text)
        self.assertNotIn('Run:', text)
        self.assertNotIn('sudo', text.lower())


class RoutineWeeklyReportSilenceTest(unittest.TestCase):
    """The routine weekly cost reports (ledger weekly-<date>, pulse
    check-i-<date>) are Tier-3-silenced at the Pulse Check-0 layer to stop the
    duplicate re-escalation. The producer already DMs Larry via route=escalate,
    so these entries MUST stay silenceable: an INFO/FYI shape with NO
    never_silence flag (a never_silence here would re-surface the duplicate the
    fix exists to mute). The date-rotating subject matches the stable key via
    the trailing-ISO-date-strip lookup rule (exercised against classify in
    test_alert_triage_state.py)."""

    _SILENCED_WEEKLY = [
        ('ledger', 'weekly'),
        ('pulse', 'check-i'),
    ]

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(TRANSLATIONS_FILE.read_text(encoding='utf-8'))

    def test_entries_exist_with_silenceable_shape(self):
        for source, subject in self._SILENCED_WEEKLY:
            self.assertIn(source, self.data, f'missing source {source!r}')
            entry = self.data[source].get(subject)
            self.assertIsInstance(
                entry, dict, f'missing entry {source}:{subject}')
            self.assertEqual(entry['severity'], 'INFO', f'{source}:{subject}')
            self.assertEqual(entry['tier'], 'FYI', f'{source}:{subject}')
            self.assertFalse(
                entry.get('never_silence'),
                f'{source}:{subject} must stay silenceable — a never_silence '
                'flag would re-surface the duplicate this entry exists to mute',
            )

    def test_pulse_beacon_erofs_entry_untouched(self):
        # The existing pulse/beacon-erofs escalation stays intact alongside the
        # new check-i entry (no collateral change to other pulse subjects).
        erofs = self.data.get('pulse', {}).get('beacon-erofs', {})
        self.assertEqual(erofs.get('severity'), 'URGENT')
        self.assertEqual(erofs.get('tier'), 'NOW')


if __name__ == '__main__':
    unittest.main()
