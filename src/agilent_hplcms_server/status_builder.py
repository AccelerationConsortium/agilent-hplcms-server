"""Map probe signals to a STATUS_SPEC v1.0 ``EquipmentStatus`` envelope."""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .config import Settings, load_settings
from .models import (
    ComponentStatus,
    EquipmentStatus,
    ErrorInfo,
)

# Avoid a hard import cycle: MosesRunner is only imported for type-checking.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .control.runner import MosesRunner


EQUIPMENT_ID = "agilent_uplc_ms"
EQUIPMENT_NAME = "Agilent UPLC-MS"
EQUIPMENT_KIND = "hplc"


def build_status(
    signals: dict[str, Any],
    settings: Settings | None = None,
    runner: "MosesRunner | None" = None,
) -> EquipmentStatus:
    """Build an ``EquipmentStatus`` from a probe ``read_signals()`` dict."""
    settings = settings or load_settings()

    # If the server-managed runner has an active process, treat the instrument
    # as busy regardless of what the probe says. This closes the race window
    # between run submission and the first *.sirslt directory appearing on disk.
    if runner is not None and runner.is_busy():
        signals = dict(signals)
        signals["acquisition_active"] = True

    core_up = (
        signals.get("openlab_acquisition_alive")
        and signals.get("openlab_instrument_service_alive")
        and signals.get("openlab_reverse_proxy_alive")
    )

    last_error_dict = signals.get("last_error")
    last_error: ErrorInfo | None = None
    if last_error_dict:
        last_error = ErrorInfo(
            code=last_error_dict.get("code"),
            message=last_error_dict.get("message", ""),
            severity=last_error_dict.get("severity", "error"),
            timestamp=_parse_iso(last_error_dict.get("timestamp"))
            or datetime.now(timezone.utc),
        )

    probe_error: str | None = signals.get("probe_error")

    if probe_error:
        equipment_state = "unknown"
        message: str | None = probe_error
        required_actions: list[str] = []
    elif not core_up:
        equipment_state = "requires_init"
        missing = []
        if not signals.get("openlab_acquisition_alive"):
            missing.append("AcquisitionServer")
        if not signals.get("openlab_instrument_service_alive"):
            missing.append("AcqInstrumentService")
        if not signals.get("openlab_reverse_proxy_alive"):
            missing.append("OpenLabReverseProxy")
        message = (
            "OpenLab core supervisor processes not detected: "
            + ", ".join(missing)
            if missing
            else "OpenLab core supervisor processes not detected"
        )
        required_actions = ["start_openlab"]
    elif last_error is not None:
        equipment_state = "error"
        message = "Recent OpenLab error event in log tail"
        required_actions = []
    elif signals.get("acquisition_active") or signals.get("moses_process_alive"):
        equipment_state = "busy"
        if signals.get("acquisition_active"):
            message = "Acquisition writing data"
        else:
            message = "moses controller script in flight"
        required_actions = []
    else:
        equipment_state = "ready"
        message = "OpenLab supervisor up; no active acquisition"
        required_actions = []

    components: dict[str, ComponentStatus] = {
        "openlab_acquisition": ComponentStatus(
            connected=bool(signals.get("openlab_acquisition_alive")),
            state="running" if signals.get("openlab_acquisition_alive") else "stopped",
        ),
        "openlab_instrument_service": ComponentStatus(
            connected=bool(signals.get("openlab_instrument_service_alive")),
            state=(
                "running"
                if signals.get("openlab_instrument_service_alive")
                else "stopped"
            ),
        ),
        "openlab_reverse_proxy": ComponentStatus(
            connected=bool(signals.get("openlab_reverse_proxy_alive")),
            state=(
                "running" if signals.get("openlab_reverse_proxy_alive") else "stopped"
            ),
        ),
        "moses_controller": ComponentStatus(
            connected=bool(signals.get("moses_process_alive")),
            state="running" if signals.get("moses_process_alive") else "idle",
        ),
        "hplc": ComponentStatus(
            connected=bool(signals.get("openlab_acquisition_alive")),
            state=_mirror_state(equipment_state),
        ),
        "ms": ComponentStatus(
            connected=bool(signals.get("openlab_acquisition_alive")),
            state=_mirror_state(equipment_state),
        ),
    }

    details: dict[str, Any] = {
        "instrument_label": settings.instrument_label,
        "openlab_log_dir": settings.openlab_log_dir,
        "cds_results_dir": settings.cds_results_dir,
        "probe_version": __version__,
        "probe_observed_at": signals.get("last_observation_at"),
        "busy_threshold_s": settings.busy_threshold_s,
        "error_window_s": settings.error_window_s,
    }
    if signals.get("last_run_dir"):
        details["last_run_dir"] = signals["last_run_dir"]
    if signals.get("last_run_mtime_iso8601"):
        details["last_run_mtime"] = signals["last_run_mtime_iso8601"]
    if signals.get("moses_process_pid") is not None:
        details["moses_process_pid"] = signals["moses_process_pid"]
    if signals.get("last_error_log_path"):
        details["last_error_log_path"] = signals["last_error_log_path"]

    return EquipmentStatus(
        equipment_id=EQUIPMENT_ID,
        equipment_name=EQUIPMENT_NAME,
        equipment_kind=EQUIPMENT_KIND,
        equipment_version=__version__,
        host=socket.gethostname(),
        equipment_status=equipment_state,
        message=message,
        required_actions=required_actions,
        device_time=datetime.now(timezone.utc),
        components=components,
        metrics={},
        last_error=last_error,
        details=details,
    )


def _mirror_state(equipment_state: str) -> str:
    if equipment_state in ("busy", "ready"):
        return equipment_state
    if equipment_state == "requires_init":
        return "stopped"
    if equipment_state == "error":
        return "error"
    return "unknown"


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None
