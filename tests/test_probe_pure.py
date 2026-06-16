"""Smoke tests that the probe is pure read-only and never imports moses / pythonnet."""

from __future__ import annotations

import sys
from pathlib import Path

from agilent_hplcms_server.config import Settings
from agilent_hplcms_server.probes import read_signals
from agilent_hplcms_server.probes.sensor_file import read_sensor_file


def test_probe_returns_expected_keys(tmp_path: Path):
    settings = Settings(
        openlab_log_dir=str(tmp_path / "no-such-logs"),
        cds_results_dir=str(tmp_path / "no-such-results"),
        rc_driver_log_dir=str(tmp_path / "no-such-lc-drivers"),
    )
    out = read_signals(settings)
    expected_keys = {
        "openlab_acquisition_alive",
        "openlab_instrument_service_alive",
        "openlab_reverse_proxy_alive",
        "moses_process_alive",
        "moses_process_pid",
        "last_run_dir",
        "last_run_mtime_iso8601",
        "last_run_mtime_epoch",
        "acquisition_active",
        "last_error",
        "last_error_log_path",
        "last_observation_at",
        "probe_error",
        # OLSS REST probe keys
        "olss_instrument_state",
        "olss_software_status",
        "olss_current_run",
        "olss_error",
    }
    assert expected_keys.issubset(set(out.keys()))
    assert isinstance(out["openlab_acquisition_alive"], bool)
    assert out["last_observation_at"]


def test_probe_does_not_import_moses_or_pythonnet():
    """Importing the probe module must not pull in any vendor dependency."""
    forbidden = {"moses", "pythonnet", "clr", "clr_loader"}
    leaked = forbidden & set(sys.modules.keys())
    assert not leaked, f"sidecar must not import {leaked}"


def test_probe_marks_unknown_when_dirs_missing(tmp_path: Path):
    settings = Settings(
        openlab_log_dir=str(tmp_path / "missing-logs"),
        cds_results_dir=str(tmp_path / "missing-results"),
        rc_driver_log_dir=str(tmp_path / "missing-lc-drivers"),
    )
    out = read_signals(settings)
    assert out["probe_error"] is not None
    assert out["last_run_dir"] is None
    assert out["last_error"] is None


def test_sensor_file_accepts_raw_and_wrapped_values(tmp_path: Path):
    sensor_file = tmp_path / "sensors.json"
    sensor_file.write_text(
        """
        {
          "turbopump_ready": {"value": true, "unit": null},
          "vacuum_level_mbar": 5.2e-6,
          "unknown_key": 123,
          "written_at": "2999-01-01T00:00:00+00:00"
        }
        """,
        encoding="utf-8",
    )

    values = read_sensor_file(str(sensor_file))

    assert values == {
        "turbopump_ready": True,
        "vacuum_level_mbar": 5.2e-6,
    }
