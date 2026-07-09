#!/usr/bin/env python3
"""Tests for system_resource_health — the honest box-level signal readers +
green/warning/critical verdict.

Every reader is driven against a synthetic /proc tree so the logic is verified
without depending on the host's real memory state.

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_system_resource_health
"""
from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import system_resource_health as srh  # noqa: E402


def _make_proc(
    tmp: Path,
    *,
    pressure: str | None = None,
    meminfo: str | None = None,
    loadavg: str | None = None,
    procs: dict[int, tuple[str, int]] | None = None,  # pid -> (name, rss_kb)
) -> Path:
    proc = tmp / 'proc'
    proc.mkdir()
    if pressure is not None:
        (proc / 'pressure').mkdir()
        (proc / 'pressure' / 'memory').write_text(pressure)
    if meminfo is not None:
        (proc / 'meminfo').write_text(meminfo)
    if loadavg is not None:
        (proc / 'loadavg').write_text(loadavg)
    for pid, (name, rss_kb) in (procs or {}).items():
        d = proc / str(pid)
        d.mkdir()
        (d / 'status').write_text(f'Name:\t{name}\nVmRSS:\t{rss_kb} kB\n')
        (d / 'cmdline').write_text(f'{name}\x00--flag\x00')
    return proc


HEALTHY_MEM = (
    'MemTotal:        8000000 kB\n'
    'MemFree:          500000 kB\n'
    'MemAvailable:    6400000 kB\n'
    'SwapTotal:             0 kB\n'
    'SwapFree:              0 kB\n'
)
TIGHT_MEM = (
    'MemTotal:        8000000 kB\n'
    'MemAvailable:     500000 kB\n'   # 93.75% used -> critical
    'SwapTotal:             0 kB\n'
    'SwapFree:              0 kB\n'
)
WARN_MEM = (
    'MemTotal:        8000000 kB\n'
    'MemAvailable:    1000000 kB\n'   # 87.5% used -> warning
    'SwapTotal:             0 kB\n'
    'SwapFree:              0 kB\n'
)
QUIET_PSI = 'some avg10=0.00 avg60=0.00 avg300=0.00 total=100\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=50\n'
HOT_PSI = 'some avg10=30.0 avg60=25.0 avg300=10.0 total=999\nfull avg10=10.0 avg60=8.0 avg300=3.0 total=500\n'


class ReaderTests(unittest.TestCase):
    def test_pressure_parse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = _make_proc(Path(td), pressure=HOT_PSI)
            p = srh.read_memory_pressure(proc)
            self.assertIsNotNone(p)
            self.assertAlmostEqual(p['some_avg60'], 25.0)
            self.assertAlmostEqual(p['full_avg10'], 10.0)

    def test_pressure_missing_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = _make_proc(Path(td))  # no pressure file
            self.assertIsNone(srh.read_memory_pressure(proc))

    def test_meminfo_used_excludes_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = _make_proc(Path(td), meminfo=HEALTHY_MEM)
            m = srh.read_meminfo(proc)
            self.assertEqual(m['total_bytes'], 8000000 * 1024)
            # used = total - available = 1.6G, NOT total - free (7.5G)
            self.assertEqual(m['used_bytes'], (8000000 - 6400000) * 1024)
            self.assertEqual(m['swap_used_bytes'], 0)

    def test_loadavg_parse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = _make_proc(Path(td), loadavg='0.40 0.55 0.60 1/238 12345')
            lo = srh.read_loadavg(proc)
            self.assertAlmostEqual(lo['load1'], 0.40)
            self.assertGreaterEqual(lo['ncpu'], 1.0)

    def test_top_process_picks_largest_rss_and_names_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = _make_proc(Path(td), procs={
                101: ('python3', 24_000),
                202: ('git', 2_000_000),   # the hog
                303: ('bash', 5_000),
            })
            top = srh._read_top_process(proc)
            self.assertEqual(top['pid'], 202)
            self.assertEqual(top['name'], 'git')
            self.assertEqual(top['rss_bytes'], 2_000_000 * 1024)


class VerdictTests(unittest.TestCase):
    def _collect(self, **proc_kw) -> dict:
        with tempfile.TemporaryDirectory() as td:
            proc = _make_proc(Path(td), **proc_kw)
            return srh.collect(proc_root=proc)

    def test_all_healthy_is_green(self) -> None:
        out = self._collect(pressure=QUIET_PSI, meminfo=HEALTHY_MEM,
                            loadavg='0.40 0.55 0.60 1/238 1', procs={1: ('init', 3000)})
        self.assertEqual(out['verdict']['level'], 'green')

    def test_high_real_memory_is_critical(self) -> None:
        out = self._collect(pressure=QUIET_PSI, meminfo=TIGHT_MEM,
                            loadavg='0.40 0.55 0.60 1/238 1',
                            procs={202: ('git', 6_000_000)})
        self.assertEqual(out['verdict']['level'], 'critical')
        # the culprit is named in the reasons
        self.assertTrue(any('git' in r for r in out['verdict']['reasons']))

    def test_sustained_pressure_is_critical(self) -> None:
        out = self._collect(pressure=HOT_PSI, meminfo=HEALTHY_MEM,
                            loadavg='0.40 0.55 0.60 1/238 1', procs={1: ('init', 3000)})
        self.assertEqual(out['verdict']['level'], 'critical')

    def test_moderate_memory_is_warning(self) -> None:
        out = self._collect(pressure=QUIET_PSI, meminfo=WARN_MEM,
                            loadavg='0.40 0.55 0.60 1/238 1', procs={1: ('init', 3000)})
        self.assertEqual(out['verdict']['level'], 'warning')

    def test_missing_signals_never_fabricate_trouble(self) -> None:
        # No pressure, no meminfo, no loadavg — a probe gap must read green,
        # never invent a critical from absence.
        out = self._collect(procs={1: ('init', 3000)})
        self.assertEqual(out['verdict']['level'], 'green')


class ThresholdTests(unittest.TestCase):
    def test_override_merges_over_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / 'th.json'
            cfg.write_text('{"mem_used_frac_critical": 0.99, "bogus": 1}')
            th = srh.load_thresholds(cfg)
            self.assertEqual(th['mem_used_frac_critical'], 0.99)
            self.assertNotIn('bogus', th)  # unknown keys ignored
            # untouched keys keep defaults
            self.assertEqual(th['load_per_core_warning'],
                             srh.DEFAULT_THRESHOLDS['load_per_core_warning'])

    def test_malformed_config_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / 'th.json'
            cfg.write_text('{not json')
            self.assertEqual(srh.load_thresholds(cfg), srh.DEFAULT_THRESHOLDS)


if __name__ == '__main__':
    unittest.main()
