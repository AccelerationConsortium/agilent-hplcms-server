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

    # Moses subprocess settings (control layer)
    moses_work_dir: str = os.environ.get(
        "MOSES_WORK_DIR",
        r"C:\Users\sdl2\Documents\Code\yoyo\pythofisher_hplcms",
    )
    moses_python_exe: str = os.environ.get(
        "MOSES_PYTHON_EXE",
        r"C:\Users\sdl2\anaconda3\envs\moses_v4_yoyo\python.exe",
    )
    # Comma-separated list of script paths (relative to moses_work_dir) that
    # are permitted to be executed via POST /control/run.
    moses_allowed_scripts: str = os.environ.get(
        "MOSES_ALLOWED_SCRIPTS",
        "examples/agent_agilent.py",
    )
    run_jobs_dir: str = os.environ.get(
        "RUN_JOBS_DIR",
        r"C:\SDL_Tools\hplcms_jobs",
    )
    queue_max_depth: int = _env_int("QUEUE_MAX_DEPTH", 20)
    queue_poll_interval_s: int = _env_int("QUEUE_POLL_INTERVAL_S", 5)
    # Advisory Retry-After (seconds) returned with HTTP 412 queue_full refusals.
    # A run is bounded by GradientConfig.run_time (<= 120 min); 60 s is a coarse
    # "check back soon" hint, not a guarantee.
    queue_full_retry_after_s: int = _env_int("QUEUE_FULL_RETRY_AFTER_S", 60)

    # Autosampler tray → Agilent multisampler drawer-code mapping. A run
    # addresses samples by logical {tray, well}; the control layer composes the
    # "{drawer}-{well}" position string Moses consumes (see control/router.py).
    # ⚠ TRAY_FRONT_DRAWER is a placeholder — confirm both codes against this
    # instrument's multisampler before deploying. TRAY_REAR_DRAWER matches the
    # code used in the existing example job.
    tray_front_drawer: str = os.environ.get("TRAY_FRONT_DRAWER", "D1F")
    tray_rear_drawer: str = os.environ.get("TRAY_REAR_DRAWER", "D4B")
    # Tray reserved for robotic sample submission. A run with submitter != "robot"
    # that targets this tray is refused with HTTP 412 reserved_for_robot. Set to
    # "" to disable the reservation entirely.
    reserved_robot_tray: str = os.environ.get("RESERVED_ROBOT_TRAY", "rear")

    # OpenLab Sharing Services REST API (instrument state probe)
    openlab_olss_url: str = os.environ.get(
        "OPENLAB_OLSS_URL",
        "http://localhost:6625/olss",
    )
    openlab_username: str = os.environ.get("OPENLAB_USERNAME", "sdl2")
    openlab_instrument_id: int = _env_int("OPENLAB_INSTRUMENT_ID", 15)

    # Sensor daemon JSON file (written by tools/hplcms_sensor_daemon.py running
    # in the Moses conda env).  Absent file → all sensor metrics show as "—".
    sensor_data_file: str = os.environ.get(
        "SENSOR_DATA_FILE",
        r"C:\SDL_Tools\hplcms_sensor_data.json",
    )

    # LC Drivers log directory — contains the active RCDriver.log written by
    # AcquisitionClient.exe with real-time solvent/waste bottle levels.
    rc_driver_log_dir: str = os.environ.get(
        "RC_DRIVER_LOG_DIR",
        r"C:\ProgramData\Agilent\LogFiles\LC Drivers",
    )


def load_settings() -> Settings:
    return Settings()
