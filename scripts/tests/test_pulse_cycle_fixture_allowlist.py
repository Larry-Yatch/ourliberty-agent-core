#!/usr/bin/env python3
"""Tests for the Pulse /cycle fixture-pattern allowlist (2026-05-27).

Closes the /cycle hallucination class: Pulse dispatched cycle-fix envelopes
for fixture-pattern task_ids (test artifacts leaked into ~/agents/inboxes/
and chain_events). The systemic fix is an allowlist applied across several
surfaces; this test exercises two of them as a regression gate (the
run_cycle.sh commit-guard surface was retired in afe9d07):

  Family A — pattern-matching unit tests on `is_fixture_task_id`:
    every fixture prefix matches a sample task_id; every exact-match
    matches; representative real task_ids from recent merges do NOT
    match; degenerate inputs (empty, None, non-string) return False.

  Family B — data-substrate filter:
    Check I's gather_retry_repeats drops fixture-prefixed bases;
    Check I's σ-anomaly filter drops fixture task_ids; Check III's
    run_check drops fixture durations before bucketing.

Test isolation: all writes are tmp_path-rooted. No real ~/agents/ writes.

Run:
    python3 -m unittest scripts.tests.test_pulse_cycle_fixture_allowlist
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import fixture_patterns as fp  # noqa: E402
import pulse_check_i as pci  # noqa: E402
import pulse_check_iii as p3  # noqa: E402


# ---------------------------------------------------------------------------
# Family A — pattern-matching unit tests
# ---------------------------------------------------------------------------


class FixturePatternMatchingTest(unittest.TestCase):

    def test_every_prefix_matches_sample_task_id(self):
        samples = {
            "t-": "t-pf",
            "sess-abc-": "sess-abc-foo-001",
            "notify-t-": "notify-t-fail",
            "notify-q-": "notify-q-1",
            "marker-error-t-": "marker-error-t-zero-1",
            "marker-error-opmanual-": (
                "marker-error-opmanual-d35-5b-shipped-note-001-1"
            ),
        }
        for prefix, sample in samples.items():
            with self.subTest(prefix=prefix, sample=sample):
                self.assertTrue(
                    fp.is_fixture_task_id(sample),
                    f"prefix {prefix!r} should match {sample!r}",
                )
                self.assertEqual(fp.matched_fixture_pattern(sample), prefix)

    def test_exact_matches(self):
        for sample in fp.FIXTURE_PATTERN_EXACT:
            with self.subTest(sample=sample):
                self.assertTrue(fp.is_fixture_task_id(sample))
                self.assertEqual(fp.matched_fixture_pattern(sample), sample)

    def test_real_task_ids_do_not_match(self):
        # Pulled from recent main commits (git log) — the regression any
        # broadening of the allowlist must not break.
        real = [
            "pr-s4-rectification-v1",
            "heal-pipeline-stall-shared-resolution-reconciliation",
            "e4-4e-approvals-tab-polish-v1",
            "teach-heal-stale-daemon-code-to-parse-uvicorn-execstart",
            "pulse-cycle-fixture-leak-fix",
            "pulse-check-iii-forge-bucket-feature-development-1800",
            "tier2-fallback-rate-limit-fix",
            "task-34-e4-2-mission-control-migration",
            "notify-pulse-cycle-fixture-leak-fix",
            "build-pulse-cycle-fixture-leak-fix",
            "review-pr-145",
            "cycle-fix-allowlist-forge-redispatch",
        ]
        for tid in real:
            with self.subTest(task_id=tid):
                self.assertFalse(
                    fp.is_fixture_task_id(tid),
                    f"real task_id {tid!r} unexpectedly flagged as fixture",
                )

    def test_degenerate_inputs(self):
        for value in ("", None, 42, [], {}, object()):
            with self.subTest(value=value):
                self.assertFalse(fp.is_fixture_task_id(value))
                self.assertIsNone(fp.matched_fixture_pattern(value))

    def test_every_observed_in_the_wild_task_id_matches(self):
        # The 18 task_ids enumerated in the dispatch brief that Pulse
        # hallucinated on. Every one must match the allowlist or the
        # systemic fix has a gap.
        observed = [
            "notify-t-fail",
            "notify-t-rev",
            "notify-t-pf-answer",
            "notify-q-1",
            "marker-error-envelope-id-1",  # exception — see below
            "marker-error-notify-t-pf-1",  # exception — see below
            "marker-error-opmanual-d35-5b-shipped-note-001-1",
            "marker-error-opmanual-d35-5b-shipped-note-001-2",
            "marker-error-t-bad-pf-1",
            "marker-error-t-bad-rev-1",
            "marker-error-t-built-1",
            "marker-error-t-drift-1",
            "marker-error-t-inflate-1",
            "marker-error-t-no-chat-1",
            "marker-error-t-no-preamble-1",
            "marker-error-t-pf-1",
            "marker-error-t-zero-1",
            # 2026-05-29 incident: AUTO_MERGE-failure fixture family,
            # migrated to the zz-fixture- reserved namespace (2026-05-30).
            # Caught via FIXTURE_PATTERN_PREFIXES, not enumerated real-* exacts.
            "zz-fixture-mirror-bad-marker",
            "zz-fixture-mirror-retry",
            "zz-fixture-ok",
            "zz-fixture-empty-findings",
            "zz-fixture-loop",
        ]
        # marker-error-envelope-id-1 and marker-error-notify-t-pf-1 do not
        # start with a covered prefix (they are wrapped notify-t-* /
        # envelope-id artifacts). Both get caught by their unwrapped base
        # when gather_retry_repeats walks them — the test asserts the
        # unwrap path here. envelope-id was added to FIXTURE_PATTERN_EXACT
        # in extend-fixture-gate-outbox-side (2026-05-29) when the outbox-
        # side gate closed the 54-burn envelope-id retry-loop.
        unwrap_exceptions = {
            "marker-error-envelope-id-1": True,   # base envelope-id flagged
            "marker-error-notify-t-pf-1": True,   # base notify-t-pf flagged
        }
        for tid in observed:
            with self.subTest(task_id=tid):
                direct = fp.is_fixture_task_id(tid)
                if tid in unwrap_exceptions:
                    base = pci._unwrap_marker_error_base(tid)
                    self.assertIsNotNone(base, f"unwrap of {tid!r} returned None")
                    flagged = direct or (
                        base is not None and fp.is_fixture_task_id(base)
                    )
                    self.assertEqual(
                        flagged,
                        unwrap_exceptions[tid],
                        f"{tid!r} expected flagged={unwrap_exceptions[tid]} "
                        f"(direct={direct}, base={base})",
                    )
                else:
                    self.assertTrue(
                        direct,
                        f"observed-in-the-wild fixture id {tid!r} not flagged",
                    )

    def test_overlap_with_task_type_inference(self):
        # Sanity: fixture prefixes don't conflict with task_type_inference's
        # production prefixes ("smoke-", "cycle-", "build-", "review-", ...).
        from task_type_inference import infer_task_type
        for prefix in fp.FIXTURE_PATTERN_PREFIXES:
            # Build a representative id under each prefix and confirm
            # infer_task_type doesn't classify it as one of the known
            # production buckets (that would mean the prefix was a
            # production prefix accidentally promoted to fixture).
            sample = prefix + "smoketest"
            inferred = infer_task_type(sample)
            self.assertIn(
                inferred,
                {"notification", "retry", "unclassified"},
                f"fixture prefix {prefix!r} collides with production "
                f"task_type {inferred!r}",
            )


# ---------------------------------------------------------------------------
# Family B — data-substrate filters in pulse_check_i + pulse_check_iii
# ---------------------------------------------------------------------------


class CheckIRetryRepeatsFilterTest(unittest.TestCase):

    def _seed_archive(self, root: Path, agent: str, filenames: list[str]) -> None:
        archive = root / agent / ".archive"
        archive.mkdir(parents=True)
        for name in filenames:
            (archive / name).write_text("{}")

    def test_fixture_prefixed_marker_errors_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox_root = Path(tmp)
            # 3+ retries of a real task (should appear) AND 3+ retries of
            # a fixture task (should NOT appear after the filter).
            real_files = [
                f"marker-error-real-task-{i}.json" for i in range(1, 5)
            ]
            fixture_files = [
                f"marker-error-t-pf-{i}.json" for i in range(1, 5)
            ]
            self._seed_archive(outbox_root, "forge", real_files + fixture_files)

            repeats = pci.gather_retry_repeats(outbox_root)
            task_ids = {r["task_id"] for r in repeats}
            self.assertIn("real-task", task_ids)
            self.assertNotIn("t-pf", task_ids)
            for tid in task_ids:
                self.assertFalse(
                    fp.is_fixture_task_id(tid),
                    f"fixture {tid!r} leaked into retry_repeats output",
                )

    def test_exact_match_fixtures_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox_root = Path(tmp)
            files = [
                f"marker-error-opmanual-d35-5b-shipped-note-001-{i}.json"
                for i in range(1, 5)
            ]
            self._seed_archive(outbox_root, "forge", files)

            repeats = pci.gather_retry_repeats(outbox_root)
            self.assertEqual(
                repeats, [],
                f"exact-match fixture leaked into retry_repeats: {repeats}",
            )


class CheckISigmaAnomalyFilterTest(unittest.TestCase):

    def _sidecar(self, anomalies: list[dict]) -> dict:
        return {
            "schema_version": "v1",
            "week_ending": "2026-05-25",
            "total_usd": 1.00,
            "delta_vs_prior_week": None,
            "anomalies": anomalies,
            "retry_overhead": {
                "total_retry_cost_usd": 0.0,
                "percent_of_total": 0.0,
            },
        }

    def _anom(self, tid: str, sigma: float = 5.0) -> dict:
        return {
            "task_id": tid,
            "sigma_above": sigma,
            "cost_usd": 1.50,
            "baseline_usd": 0.30,
        }

    def test_fixture_anomaly_does_not_synthesize_proposal(self):
        sidecar = self._sidecar([self._anom("t-zero")])
        proposals = pci.synthesize_proposals(sidecar, repeats=[])
        for p in proposals:
            self.assertNotIn(
                "t-zero", p["title"],
                f"fixture σ-anomaly produced a proposal: {p}",
            )

    def test_fixture_anomaly_excluded_from_assemble_count(self):
        sidecar = self._sidecar([
            self._anom("real-heavy-task"),
            self._anom("notify-t-pf-answer"),  # fixture
            self._anom("sess-abc-x"),  # fixture
        ])
        fired = datetime(2026, 5, 25, 12, tzinfo=timezone.utc)
        result = pci.assemble_check_i(
            sidecar=sidecar, repeats=[], week_ending="2026-05-25",
            sidecar_filename="weekly-2026-05-25.json", fired_at=fired,
        )
        # The headline anomaly count reflects only real anomalies.
        self.assertEqual(result["ledger_headline"]["anomaly_count"], 1)
        sig_anoms = result["engineering_signals"]["sigma_anomalies"]
        sig_tids = [a["task_id"] for a in sig_anoms]
        self.assertEqual(sig_tids, ["real-heavy-task"])

    def test_only_fixture_anomalies_yields_no_signal(self):
        sidecar = self._sidecar([
            self._anom("t-pf"),
            self._anom("marker-error-t-built-1"),
        ])
        fired = datetime(2026, 5, 25, 12, tzinfo=timezone.utc)
        result = pci.assemble_check_i(
            sidecar=sidecar, repeats=[], week_ending="2026-05-25",
            sidecar_filename="weekly-2026-05-25.json", fired_at=fired,
        )
        self.assertFalse(
            result["has_signal"],
            "fixture-only anomalies should produce no_signal mode",
        )
        self.assertEqual(result["mode"], "no-signal")


class CheckIIIDurationFilterTest(unittest.TestCase):

    def _dur(self, task_id: str, secs: float, agent: str = "forge",
             task_type: str = "feature-development") -> p3.SessionDuration:
        return p3.SessionDuration(
            agent=agent, task_type=task_type,
            duration_sec=secs, ts="2026-05-20T00:00:00+00:00",
            task_id=task_id,
        )

    def test_fixture_durations_dropped_before_bucketing(self):
        # 10 real durations (~120s each) + 5 fixture durations (1s each) all
        # in the same bucket. Without the filter, the fixture noise would
        # pull the p90 down dramatically. With the filter, the bucket sees
        # only real data.
        durations = (
            [self._dur("real-{}".format(i), 120.0) for i in range(10)]
            + [self._dur("t-pf-{}".format(i), 1.0) for i in range(5)]
        )
        artifact = p3.run_check(durations=durations, config={})
        # Run again with fixtures stripped manually; results must match.
        clean = [d for d in durations if not d.task_id.startswith("t-")]
        expected = p3.run_check(durations=clean, config={})
        self.assertEqual(artifact["proposals"], expected["proposals"])

    def test_fixture_only_bucket_falls_below_sample_floor(self):
        # 11 fixture durations: enough to clear the sample floor if they
        # weren't filtered. Post-filter the bucket is empty and silent.
        durations = [
            self._dur("notify-t-pf-{}".format(i), 600.0) for i in range(11)
        ]
        artifact = p3.run_check(durations=durations, config={})
        self.assertEqual(artifact["proposals"], [])

    def test_empty_task_id_passes_through(self):
        # Back-compat: SessionDuration with task_id='' (legacy fixture
        # files) must NOT be filtered — empty string is not a fixture
        # match.
        durations = [
            p3.SessionDuration(
                agent="forge", task_type="feature-development",
                duration_sec=100.0 + i, ts="2026-05-20T00:00:00+00:00",
                task_id="",
            )
            for i in range(15)
        ]
        artifact = p3.run_check(durations=durations, config={})
        # 15 samples ≥ sample-size floor → produces a proposal (no prior
        # config, so it's an initial proposal).
        self.assertEqual(len(artifact["proposals"]), 1)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Drift gate — the two live mirror surfaces must reference every pattern
# ---------------------------------------------------------------------------


class AllowlistDriftTest(unittest.TestCase):
    """If a pattern is added/removed in fixture_patterns.py, every mirror
    surface must be updated. This test fails loudly when one drifts."""

    def _read(self, rel: str) -> str:
        return (_REPO_ROOT / rel).read_text()

    def test_cycle_prompt_lists_every_pattern(self):
        body = self._read("runbooks/cycle-prompt.md")
        for prefix in fp.FIXTURE_PATTERN_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertIn(f"`{prefix}`", body)
        for exact in fp.FIXTURE_PATTERN_EXACT:
            with self.subTest(exact=exact):
                self.assertIn(f"`{exact}`", body)

    def test_pulse_claude_md_lists_every_pattern(self):
        body = self._read("agents/pulse/CLAUDE.md")
        for prefix in fp.FIXTURE_PATTERN_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertIn(f"`{prefix}`", body)
        for exact in fp.FIXTURE_PATTERN_EXACT:
            with self.subTest(exact=exact):
                self.assertIn(f"`{exact}`", body)


if __name__ == "__main__":
    unittest.main()
