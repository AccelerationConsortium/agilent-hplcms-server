"""Smoke tests that the probe is pure read-only and never imports moses / pythonnet."""

from __future__ import annotations

import sys
from pathlib import Path

from agilent_hplcms_server.config import Settings
from agilent_hplcms_server.probes import read_signals


def test_probe_returns_expected_keys(tmp_path: Path):
    settings = Settings(
        openlab_log_dir=str(tmp_path / "no-such-logs"),
        cds_results_dir=str(tmp_path / "no-such-results"),
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
    }
    assert expected_keys == set(out.keys())
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
    )
    out = read_signals(settings)
    assert out["probe_error"] is not None
    assert out["last_run_dir"] is None
    assert out["last_error"] is None
