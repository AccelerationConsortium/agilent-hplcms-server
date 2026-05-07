"""Sidecar configuration sourced from environment variables.

All defaults match the verified read-only paths on this PC at the time this
repo was scaffolded. Override any of them via env var without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    openlab_log_dir: str = os.environ.get(
        "OPENLAB_LOG_DIR", r"C:\ProgramData\Agilent\LogFiles"
    )
    cds_results_dir: str = os.environ.get(
        "CDS_RESULTS_DIR", r"C:\CDSProjects\Installation\Results"
    )
    moses_env_glob: str = os.environ.get(
        "MOSES_ENV_GLOB", r"C:\Users\sdl2\anaconda3\envs\moses*"
    )
    busy_threshold_s: int = _env_int("BUSY_THRESHOLD_S", 90)
    error_window_s: int = _env_int("ERROR_WINDOW_S", 300)
    result_scan_root_limit: int = _env_int("RESULT_SCAN_ROOT_LIMIT", 12)
    result_scan_dir_limit: int = _env_int("RESULT_SCAN_DIR_LIMIT", 1500)
    instrument_label: str = os.environ.get("OPENLAB_INSTRUMENT_NAME", "SDL2_LC1290")
    dashboard_origin: str = os.environ.get("DASHBOARD_ORIGIN", "*")
    host: str = os.environ.get("HOST", "0.0.0.0")
    port: int = _env_int("PORT", 8010)


def load_settings() -> Settings:
    return Settings()
