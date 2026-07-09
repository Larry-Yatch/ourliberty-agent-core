#!/usr/bin/env python3
"""system_resource_health.py — honest box-level resource-health signals.

Why this exists
---------------
The System Health gauge used to read ONE cgroup's (`ourliberty-inbox-watcher`)
`memory.current` against its 3 GiB `memory.high` soft cap, plus the *cumulative*
`memory.events` counters. That is misleading:

  - `memory.current` is cache-INCLUSIVE. A long-lived daemon accrues gigabytes
    of RECLAIMABLE page cache (git/repo reads) charged to its cgroup by
    first-touch accounting. The gauge went orange from cache, not from the
    process — the process itself was ~24 MB.
  - `memory.events high` only ever counts UP until the service restarts, so the
    bar reads scary on long uptime and resets to green on any restart. It was
    effectively showing "time since restart", not health.

This module reads the signals that actually mean trouble on the box:

  - Memory PRESSURE (PSI, `/proc/pressure/memory`) — the honest "is memory
    actually stalling work" signal. Was flat-zero every time the false-orange
    gauge screamed.
  - REAL memory used (`/proc/meminfo` MemTotal - MemAvailable) — excludes
    reclaimable cache.
  - Swap usage.
  - Load average vs core count.
  - The single biggest memory consumer, NAMED — this is what fingers a runaway
    (e.g. the GitHub/git process that ate the droplet on 2026-07-07) instantly.

It then computes a green / warning / critical VERDICT with plain-English
reasons + suggested actions. The verdict lives here (not split across the py
API and the TS dashboard) so the gauge and the watcher can never disagree.

Pure + injectable: every reader takes a root Path so tests drive it against a
synthetic `/proc`. No side effects, never raises to the caller for a missing
file (returns None / partial) — a health probe must not itself crash.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

# ---- thresholds -----------------------------------------------------------
# Overridable via config/system_health_thresholds.json (edit + commit; both the
# API and the watcher read it on next invocation — no reload). Defaults are
# deliberately conservative: warning = "getting tight", critical = "real
# trouble". Tuned to stay quiet on the reclaimable-cache false class.
DEFAULT_THRESHOLDS: dict[str, float] = {
    # PSI "some" avg over 60s: fraction of time SOME task stalled on memory.
    'psi_some_avg60_warning': 5.0,
    'psi_some_avg60_critical': 20.0,
    # real memory used / total.
    'mem_used_frac_warning': 0.85,
    'mem_used_frac_critical': 0.92,
    # swap used / swap total (0 swap on this box → never trips).
    'swap_used_frac_warning': 0.25,
    'swap_used_frac_critical': 0.50,
    # 1-minute load average / online CPU count.
    'load_per_core_warning': 2.0,
    'load_per_core_critical': 4.0,
}

_LEVELS = ('green', 'warning', 'critical')


def _worse(a: str, b: str) -> str:
    """Return the more severe of two verdict levels."""
    return a if _LEVELS.index(a) >= _LEVELS.index(b) else b


def load_thresholds(config_path: Optional[Path] = None) -> dict[str, float]:
    """Merge on-disk overrides over the defaults. Malformed / missing file →
    defaults (a health probe never dies on its own config)."""
    merged = dict(DEFAULT_THRESHOLDS)
    if config_path is None:
        return merged
    try:
        raw = json.loads(config_path.read_text())
    except (OSError, ValueError):
        return merged
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in DEFAULT_THRESHOLDS and isinstance(v, (int, float)):
                merged[k] = float(v)
    return merged


# ---- raw signal readers ---------------------------------------------------

def read_memory_pressure(proc_root: Path = Path('/proc')) -> Optional[dict[str, float]]:
    """Parse /proc/pressure/memory. Returns some/full avg10/avg60/avg300, or
    None if PSI is unavailable (older kernel / not mounted)."""
    try:
        text = (proc_root / 'pressure' / 'memory').read_text()
    except (OSError, ValueError):
        return None
    out: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ('some', 'full'):
            continue
        kind = parts[0]
        for tok in parts[1:]:
            if '=' not in tok:
                continue
            k, _, v = tok.partition('=')
            if k.startswith('avg'):
                try:
                    out[f'{kind}_{k}'] = float(v)
                except ValueError:
                    continue
    return out or None


def read_meminfo(proc_root: Path = Path('/proc')) -> Optional[dict[str, int]]:
    """Parse /proc/meminfo (kB) into bytes. `used` excludes reclaimable cache
    (MemTotal - MemAvailable). Returns None if unreadable."""
    try:
        text = (proc_root / 'meminfo').read_text()
    except (OSError, ValueError):
        return None
    kb: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(':'):
            try:
                kb[parts[0][:-1]] = int(parts[1])
            except ValueError:
                continue
    if 'MemTotal' not in kb:
        return None
    total = kb['MemTotal'] * 1024
    # MemAvailable is the honest "how much can a new workload get" number
    # (free + reclaimable). Fall back to MemFree on ancient kernels.
    avail = kb.get('MemAvailable', kb.get('MemFree', 0)) * 1024
    swap_total = kb.get('SwapTotal', 0) * 1024
    swap_free = kb.get('SwapFree', 0) * 1024
    return {
        'total_bytes': total,
        'available_bytes': avail,
        'used_bytes': max(0, total - avail),
        'swap_total_bytes': swap_total,
        'swap_used_bytes': max(0, swap_total - swap_free),
    }


def read_loadavg(proc_root: Path = Path('/proc')) -> Optional[dict[str, float]]:
    """Parse /proc/loadavg + online CPU count."""
    try:
        parts = (proc_root / 'loadavg').read_text().split()
    except (OSError, ValueError):
        return None
    if len(parts) < 3:
        return None
    try:
        load1, load5, load15 = (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return None
    ncpu = os.cpu_count() or 1
    return {'load1': load1, 'load5': load5, 'load15': load15, 'ncpu': float(ncpu)}


def _read_top_process(proc_root: Path = Path('/proc')) -> Optional[dict[str, Any]]:
    """Return the single process with the largest resident set (VmRSS), named.

    RSS is a robust single-shot signal (unlike instantaneous %CPU, which needs
    a delta). A runaway that "uses all the resources" shows here as the top
    consumer — this is the field that names the culprit in the DM.
    """
    best: Optional[dict[str, Any]] = None
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        status_path = entry / 'status'
        try:
            status_text = status_path.read_text()
        except (OSError, ValueError, ProcessLookupError):
            continue  # process died mid-scan; skip
        rss_kb: Optional[int] = None
        comm = '?'
        for line in status_text.splitlines():
            if line.startswith('VmRSS:'):
                bits = line.split()
                if len(bits) >= 2:
                    try:
                        rss_kb = int(bits[1])
                    except ValueError:
                        rss_kb = None
            elif line.startswith('Name:'):
                comm = line.split('\t', 1)[-1].strip() or '?'
        if rss_kb is None:
            continue
        if best is None or rss_kb > best['rss_bytes'] // 1024:
            cmdline = _read_cmdline(entry)
            best = {
                'pid': int(name),
                'name': comm,
                'rss_bytes': rss_kb * 1024,
                'cmdline': cmdline,
            }
    return best


def _read_cmdline(proc_entry: Path) -> str:
    """/proc/<pid>/cmdline (NUL-separated) → space-joined, truncated."""
    try:
        raw = (proc_entry / 'cmdline').read_bytes()
    except (OSError, ValueError, ProcessLookupError):
        return ''
    cmd = raw.replace(b'\x00', b' ').decode('utf-8', 'replace').strip()
    return cmd[:200]


# ---- verdict --------------------------------------------------------------

def compute_verdict(
    signals: dict[str, Any],
    thresholds: dict[str, float],
) -> dict[str, Any]:
    """Combine raw signals into a green/warning/critical verdict with
    plain-English reasons + suggested actions. Signals that are None (probe
    unavailable) simply don't contribute — never fabricate a problem from a
    missing reading."""
    level = 'green'
    reasons: list[str] = []
    actions: list[str] = []

    pressure = signals.get('memory_pressure')
    if pressure and 'some_avg60' in pressure:
        p60 = pressure['some_avg60']
        if p60 >= thresholds['psi_some_avg60_critical']:
            level = _worse(level, 'critical')
            reasons.append(f'Memory pressure sustained ({p60:.0f}% of the last minute spent waiting on memory).')
            actions.append('Find the memory hog and restart or kill it (see top consumer below).')
        elif p60 >= thresholds['psi_some_avg60_warning']:
            level = _worse(level, 'warning')
            reasons.append(f'Memory pressure climbing ({p60:.0f}% of the last minute waiting on memory).')
            actions.append('Watch the top memory consumer; be ready to restart it if it keeps growing.')

    mem = signals.get('meminfo')
    if mem and mem['total_bytes'] > 0:
        frac = mem['used_bytes'] / mem['total_bytes']
        if frac >= thresholds['mem_used_frac_critical']:
            level = _worse(level, 'critical')
            reasons.append(f'Real memory nearly full ({frac*100:.0f}% used, excluding reclaimable cache).')
            actions.append('Free memory now — restart the biggest consumer or add swap.')
        elif frac >= thresholds['mem_used_frac_warning']:
            level = _worse(level, 'warning')
            reasons.append(f'Real memory getting tight ({frac*100:.0f}% used).')
        swap_total = mem['swap_total_bytes']
        if swap_total > 0:
            sfrac = mem['swap_used_bytes'] / swap_total
            if sfrac >= thresholds['swap_used_frac_critical']:
                level = _worse(level, 'critical')
                reasons.append(f'Swap heavily used ({sfrac*100:.0f}%) — the box is thrashing.')
            elif sfrac >= thresholds['swap_used_frac_warning']:
                level = _worse(level, 'warning')
                reasons.append(f'Swap in use ({sfrac*100:.0f}%).')

    load = signals.get('loadavg')
    if load and load['ncpu'] > 0:
        per_core = load['load1'] / load['ncpu']
        if per_core >= thresholds['load_per_core_critical']:
            level = _worse(level, 'critical')
            reasons.append(f'CPU overloaded (load {load["load1"]:.1f} on {int(load["ncpu"])} cores).')
            actions.append('Check what is pinning the CPU (top consumer below / `top`).')
        elif per_core >= thresholds['load_per_core_warning']:
            level = _worse(level, 'warning')
            reasons.append(f'CPU busy (load {load["load1"]:.1f} on {int(load["ncpu"])} cores).')

    top = signals.get('top_process')
    if level != 'green' and top:
        reasons.append(
            f'Biggest consumer: {top["name"]} (pid {top["pid"]}, '
            f'{top["rss_bytes"] / (1024**3):.2f} GiB).'
        )

    if level == 'green':
        headline = 'All resource signals healthy.'
    elif level == 'warning':
        headline = 'Resources getting tight — worth a look, not urgent.'
    else:
        headline = 'Real resource trouble — needs attention now.'

    return {
        'level': level,
        'headline': headline,
        'reasons': reasons,
        'suggested_actions': actions,
    }


def collect(
    proc_root: Path = Path('/proc'),
    config_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Read every signal + compute the verdict. This is the single snapshot
    both the API endpoint and the watcher consume."""
    thresholds = load_thresholds(config_path)
    signals: dict[str, Any] = {
        'memory_pressure': read_memory_pressure(proc_root),
        'meminfo': read_meminfo(proc_root),
        'loadavg': read_loadavg(proc_root),
        'top_process': _read_top_process(proc_root),
    }
    verdict = compute_verdict(signals, thresholds)
    return {'signals': signals, 'verdict': verdict}
