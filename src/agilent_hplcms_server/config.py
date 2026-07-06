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
    # Advisory Retry-After (seconds) returned with HTTP 423 workflow_active
    # refusals (a robot/agent campaign holds the equipment-blocking lock).
    workflow_active_retry_after_s: int = _env_int("WORKFLOW_ACTIVE_RETRY_AFTER_S", 60)
    # Servicing detection debounce: number of consecutive /status observations
    # of "OLSS busy AND no active sidecar job" required before the sidecar
    # declares a technician is driving OpenLab directly and halts the queue /
    # rejects submissions with 409 instrument_servicing. >=2 avoids a false
    # positive in the one-poll gap after our Moses process exits but before OLSS
    # returns to Idle. Fails safe: a transient over-count only briefly halts the
    # queue and self-clears when OLSS goes Idle.
    servicing_debounce_polls: int = _env_int("SERVICING_DEBOUNCE_POLLS", 2)

    # Lab user roster → claim role (identity only, NOT authentication; the
    # network ACL / dashboard login is the real access boundary). Comma-separated
    # owner names per group. Capabilities by role:
    #   user        (HPLCMS_USERS)  → submit samples into the queue
    #   automation  (HTE_USERS)     → submit + start equipment-blocking workflows
    #   service     (HPLCMS_ADMINS) → submit + toggle service mode
    # The roster is ALWAYS enforced: when every list is empty the built-in
    # defaults below apply (so a fresh install always has a Service-Account and
    # never bricks). A literal "*" in a list matches any owner (explicit open
    # mode for dev). See control/roster.py.
    hplcms_users: str = os.environ.get("HPLCMS_USERS", "Hplcms-User")
    hte_users: str = os.environ.get("HTE_USERS", "HTE-User")
    hplcms_admins: str = os.environ.get("HPLCMS_ADMINS", "Service-Account")

    # Central roster projection (optional). When ROSTER_URL is set, the sidecar
    # polls the central auth service's owner→role projection
    # (ac-organic-lab `GET /equipment/{key}/roster`) and resolves claim owners
    # against it — central is then authoritative. The static *_USERS lists above
    # are the fallback used only until the first successful pull (so the device
    # never bricks if central is unreachable at startup). Empty ROSTER_URL →
    # static env roster only (fully standalone). See control/roster_sync.py.
    roster_url: str = os.environ.get("ROSTER_URL", "")
    roster_refresh_interval_s: int = _env_int("ROSTER_REFRESH_INTERVAL_S", 60)
    roster_http_timeout_s: int = _env_int("ROSTER_HTTP_TIMEOUT_S", 5)
    # Optional X-Api-Key sent with the roster pull. The roster endpoint is
    # Tailnet-only by deployment, so this is normally unset; set it if the
    # central service ever gates the device-plane endpoints.
    roster_api_key: str = os.environ.get("ROSTER_API_KEY", "")

    # Autosampler tray → Agilent multisampler drawer-code mapping. A run
    # addresses samples by logical {tray, well}; the control layer composes the
    # "{drawer}-{well}" position string Moses consumes (see control/router.py).
    # The front drawer (D1F) is the robot's reserved tray — confirmed against the
    # instrument. The rear drawer (D4B) matches the code in the existing example
    # job; confirm it against this instrument's multisampler before deploying.
    tray_front_drawer: str = os.environ.get("TRAY_FRONT_DRAWER", "D1F")
    tray_rear_drawer: str = os.environ.get("TRAY_REAR_DRAWER", "D4B")
    # Tray reserved for robotic sample submission (the front tray, D1F). A run
    # with submitter != "robot" that targets it is refused with HTTP 412
    # reserved_for_robot. Set to "" to disable the reservation entirely.
    reserved_robot_tray: str = os.environ.get("RESERVED_ROBOT_TRAY", "front")

    # Autosampler labware config: a JSON file mapping each logical tray to the
    # plate/vial container actually loaded in it, so submissions are validated
    # against the REAL plate geometry (not the built-in 96/384 assumption) and a
    # declared plate_format that disagrees with the loaded labware is refused
    # (HTTP 422 plate_mismatch). Generate/refresh it from the instrument's real
    # OpenLab Sample Container config with tools/capture_autosampler_config.py.
    # Empty -> no labware enforcement (falls back to the plate_format geometry
    # check in control/models.py). See control/labware.py.
    labware_config_path: str = os.environ.get("LABWARE_CONFIG_PATH", "")

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
