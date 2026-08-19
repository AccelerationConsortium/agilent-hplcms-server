"""Post-run pump-pressure QC from the archived ``.dx`` traces.

Fixtures are synthesised in the real on-disk format (see ``probes/dx_trace``),
so these exercise the actual binary decoder rather than a stand-in. The layout
was reverse-engineered from this instrument's own run archives, and the scale
factor validated against independently known values (``LCMS1I`` = 325.0 °C,
``PMP1C`` = 0.50 mL/min, ``THM1A`` = 40.00 °C).
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

from agilent_hplcms_server.config import Settings
from agilent_hplcms_server.probes import dx_pressure
from agilent_hplcms_server.probes.dx_pressure import read_run_pressure
from agilent_hplcms_server.probes.dx_trace import list_signals, read_trace
from agilent_hplcms_server.status_builder import build_status

_OFF_SAMPLE_NAME = 0x035A
_OFF_METHOD = 0x0A0E
_OFF_DESCRIPTION = 0x1075
_OFF_SCALE = 0x127C
_OFF_DATA = 0x1800

# Real pressure traces carry a 0.005 scale factor, so the fixtures use raw
# counts the instrument would actually write.
_SCALE = 0.005


def _pstring(text: str) -> bytes:
    return bytes([len(text)]) + text.encode("utf-16-le")


def _make_trace(
    description: str,
    values_bar: list[float],
    *,
    sample_name: str = "sample-1",
    method: str = r"C:\Methods\gradient_a.amx",
    scale: float = _SCALE,
    step_ms: float = 25.0,
) -> bytes:
    buf = bytearray(_OFF_DATA)
    buf[0:4] = b"\x03179"

    def put(offset: int, blob: bytes) -> None:
        buf[offset : offset + len(blob)] = blob

    put(_OFF_SAMPLE_NAME, _pstring(sample_name))
    put(_OFF_METHOD, _pstring(method))
    put(_OFF_DESCRIPTION, _pstring(description))
    put(_OFF_SCALE, struct.pack(">d", scale))

    for i, bar in enumerate(values_bar):
        raw = bar / scale if scale else 0.0
        buf += struct.pack("<dd", (i + 1) * step_ms, raw)
    return bytes(buf)


def _make_dx(
    path: Path,
    values_bar: list[float],
    *,
    method: str = r"C:\Methods\gradient_a.amx",
    sample_name: str = "sample-1",
    extra: dict[str, bytes] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        # GUID-named members, exactly as OpenLab writes them.
        archive.writestr(
            "5fa90b72-bcf5-4acc-81bf-fad37b9ed16c.IT",
            _make_trace(
                "PMP1B,Pressure", values_bar, method=method, sample_name=sample_name
            ),
        )
        for name, blob in (extra or {}).items():
            archive.writestr(name, blob)
    return path


def _make_run(
    results_dir: Path,
    name: str,
    values_bar: list[float],
    *,
    method: str = r"C:\Methods\gradient_a.amx",
    mtime: float | None = None,
) -> Path:
    sirslt = results_dir / "project" / f"{name}.sirslt"
    dx = _make_dx(sirslt / f"{name}.dx", values_bar, method=method, sample_name=name)
    if mtime is not None:
        import os

        os.utime(dx, (mtime, mtime))
    return dx


def _clear_caches() -> None:
    dx_pressure._scan_cache.clear()
    dx_pressure._summary_cache.clear()


# --- binary decoding ----------------------------------------------------------


def test_decodes_a_trace_round_trip(tmp_path: Path):
    dx = _make_dx(tmp_path / "run.dx", [200.0, 300.0, 422.5])
    trace = read_trace(dx, "PMP1B")

    assert trace is not None
    assert trace.name == "PMP1B"
    assert trace.description == "PMP1B,Pressure"
    assert trace.sample_name == "sample-1"
    assert trace.method == r"C:\Methods\gradient_a.amx"
    assert [round(v, 1) for v in trace.values] == [200.0, 300.0, 422.5]
    # Times are stored in milliseconds and surfaced in seconds.
    assert [round(t, 3) for t in trace.times_s] == [0.025, 0.050, 0.075]


def test_unknown_signal_returns_none(tmp_path: Path):
    dx = _make_dx(tmp_path / "run.dx", [200.0])
    assert read_trace(dx, "PMP1C") is None


def test_signal_is_found_among_other_members(tmp_path: Path):
    """Members are GUID-named, so the description is the only way in."""
    dx = _make_dx(
        tmp_path / "run.dx",
        [410.0, 420.0],
        extra={
            "aaaaaaaa-0000-0000-0000-000000000001.IT": _make_trace(
                "PMP1C,Flow", [0.5, 0.5], scale=1e-06
            ),
            "bbbbbbbb-0000-0000-0000-000000000002.CH": _make_trace(
                "DAD1A,Sig=210.0", [1.0, 2.0], scale=1e-05
            ),
            "MSData/Contents.xml": b"<xml/>",
        },
    )
    assert sorted(list_signals(dx)) == [
        "DAD1A,Sig=210.0",
        "PMP1B,Pressure",
        "PMP1C,Flow",
    ]
    trace = read_trace(dx, "PMP1B")
    assert trace is not None and [round(v) for v in trace.values] == [410, 420]


def test_corrupt_archive_returns_none(tmp_path: Path):
    bad = tmp_path / "broken.dx"
    bad.write_bytes(b"not a zip file at all")
    assert read_trace(bad, "PMP1B") is None
    assert list_signals(bad) == []


def test_missing_file_returns_none(tmp_path: Path):
    assert read_trace(tmp_path / "absent.dx", "PMP1B") is None


def test_zero_scale_is_rejected(tmp_path: Path):
    """A zero scale would silently flatten the trace to a dead-flat signal."""
    path = tmp_path / "run.dx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "1111.IT", _make_trace("PMP1B,Pressure", [400.0], scale=0.0)
        )
    assert read_trace(path, "PMP1B") is None


# --- baseline comparison ------------------------------------------------------


def test_summarises_the_newest_run(tmp_path: Path):
    _clear_caches()
    _make_run(tmp_path, "run1", [200.0, 300.0, 400.0], mtime=1000)
    _make_run(tmp_path, "run2", [210.0, 310.0, 410.0], mtime=2000)

    out = read_run_pressure(tmp_path)

    assert out["run_pressure_run"] == "run2.sirslt"
    assert out["run_pressure_method"] == "gradient_a"
    assert out["run_pressure_max_bar"] == 410.0
    assert out["run_pressure_min_bar"] == 210.0
    assert out["run_pressure_mean_bar"] == 310.0


def test_stable_runs_report_no_drift(tmp_path: Path):
    _clear_caches()
    for i in range(5):
        _make_run(tmp_path, f"run{i}", [200.0, 420.0 + i], mtime=1000 + i)

    out = read_run_pressure(tmp_path)

    assert out["run_pressure_drift"] is False
    assert abs(out["run_pressure_delta_pct"]) < 5
    assert out["run_pressure_baseline_n"] == 4


def test_pressure_spike_is_flagged(tmp_path: Path):
    """The real failure this exists for: a blockage driving peak pressure up."""
    _clear_caches()
    for i in range(5):
        _make_run(tmp_path, f"run{i}", [200.0, 420.0], mtime=1000 + i)
    _make_run(tmp_path, "blocked", [400.0, 1260.0], mtime=9000)

    out = read_run_pressure(tmp_path)

    assert out["run_pressure_run"] == "blocked.sirslt"
    assert out["run_pressure_drift"] is True
    assert out["run_pressure_baseline_bar"] == 420.0
    assert out["run_pressure_delta_pct"] == 200.0


def test_pressure_collapse_is_flagged(tmp_path: Path):
    """A sudden drop — a leak or a failed seal — matters as much as a spike."""
    _clear_caches()
    for i in range(5):
        _make_run(tmp_path, f"run{i}", [200.0, 420.0], mtime=1000 + i)
    _make_run(tmp_path, "leaking", [40.0, 120.0], mtime=9000)

    out = read_run_pressure(tmp_path)

    assert out["run_pressure_drift"] is True
    assert out["run_pressure_delta_pct"] < -50


def test_baseline_is_scoped_to_the_same_method(tmp_path: Path):
    """A different method legitimately runs at a different pressure."""
    _clear_caches()
    for i in range(4):
        _make_run(
            tmp_path, f"low{i}", [5.0, 8.0], method=r"C:\M\low_flow.amx", mtime=1000 + i
        )
    _make_run(tmp_path, "high1", [200.0, 420.0], method=r"C:\M\fast.amx", mtime=5000)
    _make_run(tmp_path, "high2", [200.0, 425.0], method=r"C:\M\fast.amx", mtime=6000)

    out = read_run_pressure(tmp_path)

    assert out["run_pressure_method"] == "fast"
    assert out["run_pressure_baseline_n"] == 1
    assert out["run_pressure_baseline_bar"] == 420.0
    assert out["run_pressure_drift"] is False


def test_first_run_of_a_method_has_no_baseline(tmp_path: Path):
    _clear_caches()
    _make_run(tmp_path, "only", [200.0, 420.0], mtime=1000)

    out = read_run_pressure(tmp_path)

    assert out["run_pressure_baseline_n"] == 0
    assert "run_pressure_baseline_bar" not in out
    assert out["run_pressure_drift"] is False


def test_baseline_median_ignores_a_single_outlier(tmp_path: Path):
    """Median, not mean, so one bad run cannot mask the next one."""
    _clear_caches()
    for i in range(4):
        _make_run(tmp_path, f"ok{i}", [200.0, 420.0], mtime=1000 + i)
    _make_run(tmp_path, "outlier", [400.0, 1200.0], mtime=5000)
    _make_run(tmp_path, "normal", [200.0, 421.0], mtime=6000)

    out = read_run_pressure(tmp_path)

    assert out["run_pressure_baseline_bar"] == 420.0
    assert out["run_pressure_drift"] is False


def test_disabled_when_baseline_runs_is_zero(tmp_path: Path):
    _clear_caches()
    _make_run(tmp_path, "run1", [200.0, 420.0], mtime=1000)
    assert read_run_pressure(tmp_path, baseline_runs=0) == {}


def test_missing_results_dir_is_silent(tmp_path: Path):
    _clear_caches()
    assert read_run_pressure(tmp_path / "nope") == {}


def test_run_without_a_pressure_trace_is_skipped(tmp_path: Path):
    _clear_caches()
    _make_run(tmp_path, "good", [200.0, 420.0], mtime=1000)
    sirslt = tmp_path / "project" / "empty.sirslt"
    sirslt.mkdir(parents=True)
    with zipfile.ZipFile(sirslt / "empty.dx", "w") as archive:
        archive.writestr("only.IT", _make_trace("DAD1A,Sig=210.0", [1.0]))
    import os

    os.utime(sirslt / "empty.dx", (9000, 9000))

    out = read_run_pressure(tmp_path)
    assert out["run_pressure_run"] == "good.sirslt"


# --- /status integration ------------------------------------------------------


def _signals(**overrides) -> dict:
    base = {
        "openlab_acquisition_alive": True,
        "openlab_instrument_service_alive": True,
        "openlab_reverse_proxy_alive": True,
        "probe_error": None,
        "olss_instrument_state": "Idle",
        "run_pressure_run": "blocked.sirslt",
        "run_pressure_method": "gradient_a",
        "run_pressure_max_bar": 1260.0,
        "run_pressure_min_bar": 400.0,
        "run_pressure_mean_bar": 860.0,
        "run_pressure_duration_s": 600.0,
        "run_pressure_baseline_bar": 420.0,
        "run_pressure_baseline_n": 4,
        "run_pressure_delta_pct": 200.0,
        "run_pressure_drift": True,
    }
    base.update(overrides)
    return base


def test_status_surfaces_pressure_metrics_and_warning():
    status = build_status(_signals(), settings=Settings())

    assert status.metrics["run_pressure_max_bar"].value == 1260.0
    assert status.metrics["run_pressure_max_bar"].unit == "bar"
    assert status.metrics["run_pressure_delta_pct"].value == 200.0
    assert "check_lc_pressure" in status.required_actions
    assert status.details["run_pressure"]["drift"] is True
    assert status.details["run_pressure"]["run"] == "blocked.sirslt"


def test_pressure_drift_is_advisory_only():
    """It must not halt the lab: no fault, no state change, no blocked submission."""
    status = build_status(_signals(), settings=Settings())

    assert status.equipment_status == "ready"
    assert status.last_error is None
    assert "run.submit" in status.allowed_actions
    assert "subsystem_fault_modules" not in status.details


def test_no_warning_when_pressure_is_stable():
    status = build_status(
        _signals(run_pressure_drift=False, run_pressure_delta_pct=0.3), settings=Settings()
    )

    assert "check_lc_pressure" not in status.required_actions
    assert status.details["run_pressure"]["drift"] is False


def test_absent_pressure_data_omits_the_section():
    status = build_status(
        {
            "openlab_acquisition_alive": True,
            "openlab_instrument_service_alive": True,
            "openlab_reverse_proxy_alive": True,
            "probe_error": None,
            "olss_instrument_state": "Idle",
        },
        settings=Settings(),
    )

    assert "run_pressure" not in status.details
    assert "run_pressure_max_bar" not in status.metrics
    assert "check_lc_pressure" not in status.required_actions
