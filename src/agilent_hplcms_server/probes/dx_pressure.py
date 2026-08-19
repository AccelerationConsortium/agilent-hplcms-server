"""Post-run pump-pressure QC from the archived ``.dx`` traces.

There is no live pressure feed on this instrument (see ``docs/fault_detection.md``),
but every completed run archives its full pump pressure trace. Comparing each
finished run against the recent runs of the *same method* catches the failures a
latched module fault never reports: a partially blocked column, a failing seal, a
degrading check valve, a leak that has not yet tripped a limit.

Baselining is per-method and short-horizon on purpose. The same method run six
days apart on this instrument sat at 197 bar and 422 bar peak — a column change,
not a fault — so a long baseline would flag routine work. Recent same-method runs
are the only comparison that means anything.

The check is advisory: it sets no fault and blocks no submission (see
``status_builder``), because the threshold has no tuning data behind it yet.

Returned signals
----------------
run_pressure_run           — name of the run directory measured
run_pressure_method        — method stem the run used
run_pressure_max_bar       — peak pressure of the run
run_pressure_min_bar       — minimum pressure of the run
run_pressure_mean_bar      — mean pressure of the run
run_pressure_duration_s    — trace duration
run_pressure_baseline_bar  — median peak of the prior same-method runs
run_pressure_baseline_n    — how many runs that median came from
run_pressure_delta_pct     — signed deviation of this run's peak from the baseline
run_pressure_drift         — True when |delta| exceeds the configured threshold
"""

from __future__ import annotations

import logging
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dx_trace import read_trace

logger = logging.getLogger(__name__)

# ChemStation signal name for pump 1 pressure.
PRESSURE_SIGNAL = "PMP1B"

DEFAULT_DRIFT_PCT: float = 15.0
DEFAULT_BASELINE_RUNS: int = 8
DEFAULT_SCAN_RUNS: int = 24


@dataclass(frozen=True)
class RunPressure:
    run: str
    method: str
    max_bar: float
    min_bar: float
    mean_bar: float
    duration_s: float


# Decoded summaries, keyed on file identity so a rewritten archive re-decodes.
# Only the summary is retained, never the trace, so the cache stays small; it is
# what keeps /status from re-reading every recent .dx on every poll.
_summary_cache: dict[tuple[str, int, int], RunPressure | None] = {}
_CACHE_MAX = 64


def _summarize(dx_path: Path) -> RunPressure | None:
    try:
        st = dx_path.stat()
    except OSError:
        return None
    key = (str(dx_path), st.st_mtime_ns, st.st_size)
    if key in _summary_cache:
        return _summary_cache[key]

    trace = read_trace(dx_path, PRESSURE_SIGNAL)
    summary: RunPressure | None = None
    if trace is not None and trace.values:
        summary = RunPressure(
            run=dx_path.parent.name,
            method=Path(trace.method).stem or "unknown",
            max_bar=round(max(trace.values), 1),
            min_bar=round(min(trace.values), 1),
            mean_bar=round(statistics.mean(trace.values), 1),
            duration_s=round(trace.duration_s, 1),
        )

    if len(_summary_cache) >= _CACHE_MAX:
        _summary_cache.clear()
    _summary_cache[key] = summary
    return summary


# The directory walk, not the decoding, dominates this probe: ~0.44 s across the
# real results tree, against ~6 ms to decode a trace. A completed run appears at
# most every few minutes, so the walk is cached for SCAN_TTL_S — that is what
# keeps this off the /status hot path. Read-only: nothing here touches disk state.
SCAN_TTL_S: float = 60.0
_scan_cache: dict[tuple[str, int, int, int], tuple[float, list[Path]]] = {}


def _recent_dx_files(
    results_dir: Path,
    scan_runs: int,
    root_limit: int,
    dir_limit: int,
) -> list[Path]:
    """Return the newest ``.dx`` archives, newest first.

    Deliberately bounded the same way ``process._newest_sirslt`` is: this results
    tree holds thousands of runs across years of projects, so it is walked from
    the most recently touched project directories only, and abandoned once
    ``dir_limit`` directories have been visited.
    """
    if not results_dir.exists():
        return []

    cache_key = (str(results_dir), scan_runs, root_limit, dir_limit)
    cached = _scan_cache.get(cache_key)
    if cached is not None and (time.monotonic() - cached[0]) < SCAN_TTL_S:
        return cached[1]
    try:
        roots = sorted(
            (p for p in results_dir.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[: max(root_limit, 1)]
    except OSError:
        return []

    found: list[tuple[float, Path]] = []
    visited = 0
    for root in roots:
        for current_root, dirs, _files in os.walk(root):
            visited += 1
            if visited > max(dir_limit, 1):
                break
            for dirname in list(dirs):
                if not dirname.lower().endswith(".sirslt"):
                    continue
                # Never descend into a result payload; the .dx sits at its top.
                dirs.remove(dirname)
                sirslt = Path(current_root) / dirname
                try:
                    for entry in os.scandir(sirslt):
                        if entry.name.lower().endswith(".dx"):
                            found.append((entry.stat().st_mtime, Path(entry.path)))
                            break
                except OSError:
                    continue

    found.sort(key=lambda pair: pair[0], reverse=True)
    newest = [path for _, path in found[: max(scan_runs, 1)]]
    _scan_cache[cache_key] = (time.monotonic(), newest)
    return newest


def read_run_pressure(
    results_dir: str | Path,
    *,
    drift_pct: float = DEFAULT_DRIFT_PCT,
    baseline_runs: int = DEFAULT_BASELINE_RUNS,
    scan_runs: int = DEFAULT_SCAN_RUNS,
    root_limit: int = 12,
    dir_limit: int = 1500,
) -> dict[str, Any]:
    """Summarise the newest completed run's pressure and compare it to its peers.

    ``baseline_runs`` of 0 disables the check. Returns an empty dict when there
    is no readable completed run, so the status builder treats the whole feature
    as simply absent.
    """
    if baseline_runs <= 0:
        return {}

    candidates = _recent_dx_files(
        Path(results_dir), scan_runs, root_limit, dir_limit
    )
    summaries = [s for s in (_summarize(p) for p in candidates) if s is not None]
    if not summaries:
        return {}

    latest = summaries[0]
    out: dict[str, Any] = {
        "run_pressure_run": latest.run,
        "run_pressure_method": latest.method,
        "run_pressure_max_bar": latest.max_bar,
        "run_pressure_min_bar": latest.min_bar,
        "run_pressure_mean_bar": latest.mean_bar,
        "run_pressure_duration_s": latest.duration_s,
        "run_pressure_drift": False,
    }

    # Baseline: the median peak of the most recent *prior* runs of this method.
    # Median rather than mean so one bad run does not drag the reference toward
    # itself and mask the next one.
    peers = [
        s.max_bar
        for s in summaries[1:]
        if s.method == latest.method
    ][:baseline_runs]
    out["run_pressure_baseline_n"] = len(peers)
    if not peers:
        return out

    baseline = statistics.median(peers)
    out["run_pressure_baseline_bar"] = round(baseline, 1)
    if baseline <= 0:
        return out

    delta_pct = (latest.max_bar - baseline) / baseline * 100.0
    out["run_pressure_delta_pct"] = round(delta_pct, 1)
    out["run_pressure_drift"] = abs(delta_pct) > drift_pct
    return out
