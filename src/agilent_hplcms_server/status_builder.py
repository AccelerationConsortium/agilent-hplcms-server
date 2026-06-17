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
    MetricValue,
)

# Avoid a hard import cycle: MosesRunner is only imported for type-checking.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .control.runner import MosesRunner


EQUIPMENT_ID = "agilent_uplc_ms"
EQUIPMENT_NAME = "Agilent UPLC-MS"
EQUIPMENT_KIND = "hplc"

# OLSS instrument states that indicate an active acquisition. Must match
# _OLSS_ACTIVE_STATES in control/runner.py (duplicated to avoid import coupling).
_OLSS_ACTIVE_STATES: frozenset[str] = frozenset({"Run", "Running", "Busy", "Prerun", "PostRun"})


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

    waste_near_capacity: bool = bool(signals.get("waste_near_capacity"))
    solvent_a1_low: bool = bool(signals.get("solvent_a1_low"))
    solvent_a2_low: bool = bool(signals.get("solvent_a2_low"))
    solvent_b1_low: bool = bool(signals.get("solvent_b1_low"))
    solvent_b2_low: bool = bool(signals.get("solvent_b2_low"))

    olss_state: str | None = signals.get("olss_instrument_state")
    olss_sw_status: str | None = signals.get("olss_software_status")
    olss_connected = olss_state is not None and olss_state != "NotConnected"

    # softwareStatus "Paused" means the run queue has a paused sequence —
    # the hardware modules have returned to Idle but the sequence is waiting
    # for the operator to click Resume.  Only meaningful when OpenLab is
    # connected (not just "NotConnected" / None).
    sequence_paused = (
        olss_sw_status == "Paused"
        and olss_state not in (None, "NotConnected")
    )

    # True while OLSS hardware state is in an active-acquisition state.
    # This catches runs submitted directly in OpenLab (no Moses process, no
    # recent sirslt activity) as well as runs in progress via our queue.
    olss_acquiring = olss_state in _OLSS_ACTIVE_STATES

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
    elif sequence_paused:
        equipment_state = "paused"
        message = "OpenLab sequence paused — click Resume in OpenLab run queue to continue"
        required_actions = ["resume_paused_sequence"]
    elif signals.get("acquisition_active") or signals.get("moses_process_alive") or olss_acquiring:
        equipment_state = "busy"
        if signals.get("acquisition_active"):
            message = "Acquisition writing data"
        elif olss_acquiring:
            message = f"OpenLab acquisition active (instrument state: {olss_state})"
        else:
            message = "moses controller script in flight"
        required_actions = []
    else:
        equipment_state = "ready"
        message = "OpenLab supervisor up; no active acquisition"
        required_actions = []

    # Consumable warnings are appended regardless of instrument state so the
    # client can act on them even during a run.
    if waste_near_capacity:
        required_actions = list(required_actions) + ["empty_waste_bottle"]
    if solvent_a1_low:
        required_actions = list(required_actions) + ["refill_solvent_a1"]
    if solvent_a2_low:
        required_actions = list(required_actions) + ["refill_solvent_a2"]
    if solvent_b1_low:
        required_actions = list(required_actions) + ["refill_solvent_b1"]
    if solvent_b2_low:
        required_actions = list(required_actions) + ["refill_solvent_b2"]

    # Map OLSS instrument state to a component state string understood by clients.
    def _olss_to_component_state(s: str | None) -> str:
        if s is None:
            return _mirror_state(equipment_state)
        if sequence_paused:
            return "paused"
        return {
            "Idle": "ready",
            "NotConnected": "stopped",
            "NotReady": "not_ready",
            "Error": "error",
            "Busy": "busy",
            "Prerun": "busy",
            "Run": "busy",
            "Running": "busy",
            "PostRun": "busy",
        }.get(s, s.lower())

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
            connected=olss_connected or bool(signals.get("openlab_acquisition_alive")),
            state=_olss_to_component_state(olss_state),
            message=olss_sw_status if olss_sw_status and olss_sw_status != "OK" else None,
        ),
        "ms": ComponentStatus(
            connected=olss_connected or bool(signals.get("openlab_acquisition_alive")),
            state=_olss_to_component_state(olss_state),
            message=olss_sw_status if olss_sw_status and olss_sw_status != "OK" else None,
        ),
    }

    # Per-module LC components derived from RCDriver.log LDT entries.
    # Only added when signal data is present; absent = no component card shown.
    _lc_module_conn = olss_connected or bool(signals.get("openlab_acquisition_alive"))

    for role, comp in [
        ("binary_pump",       _build_pump_component(signals, _lc_module_conn, olss_state)),
        ("dad_detector",      _build_dad_component(signals, _lc_module_conn, olss_state)),
        ("column_thermostat", _build_column_component(signals, _lc_module_conn, olss_state)),
        ("multisampler",      _build_multisampler_component(signals, _lc_module_conn, olss_state)),
    ]:
        if comp is not None:
            components[role] = comp

    details: dict[str, Any] = {
        "instrument_label": settings.instrument_label,
        "openlab_log_dir": settings.openlab_log_dir,
        "cds_results_dir": settings.cds_results_dir,
        "probe_version": __version__,
        "probe_observed_at": signals.get("last_observation_at"),
        "busy_threshold_s": settings.busy_threshold_s,
        "error_window_s": settings.error_window_s,
    }
    if runner is not None:
        details["queue_length"] = runner.queue_depth()
    if signals.get("last_run_dir"):
        details["last_run_dir"] = signals["last_run_dir"]
    if signals.get("last_run_mtime_iso8601"):
        details["last_run_mtime"] = signals["last_run_mtime_iso8601"]
    if signals.get("moses_process_pid") is not None:
        details["moses_process_pid"] = signals["moses_process_pid"]
    if signals.get("last_error_log_path"):
        details["last_error_log_path"] = signals["last_error_log_path"]
    if signals.get("olss_instrument_state") is not None:
        details["olss_instrument_state"] = signals["olss_instrument_state"]
    if signals.get("olss_software_status") is not None:
        details["olss_software_status"] = signals["olss_software_status"]
    if signals.get("olss_current_run") is not None:
        details["olss_current_run"] = signals["olss_current_run"]
    if signals.get("olss_error") is not None:
        details["olss_error"] = signals["olss_error"]
    if signals.get("rc_driver_data_age_s") is not None:
        details["rc_driver_data_age_s"] = signals["rc_driver_data_age_s"]
    if waste_near_capacity:
        details["waste_near_capacity"] = True
    for _slot in ("a1", "a2", "b1", "b2"):
        if signals.get(f"solvent_{_slot}_low"):
            details[f"solvent_{_slot}_low"] = True

    # Keep the runner's OLSS-occupied flag current so poll() can gate job
    # completion and next-job dispatch correctly without importing any probe.
    if runner is not None:
        runner.notify_olss_state(olss_state, olss_sw_status)

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
        metrics=_build_metrics(signals),
        last_error=last_error,
        details=details,
    )


def _module_state_with_olss(
    module_state: str | None,
    olss_state: str | None,
    stat_age_s: float | None,
    stat_flags: list[str] | None = None,
) -> str:
    """Reconcile STAT? module state with the current OLSS overall state.

    OLSS is only used to detect active acquisition (Run/Prerun/Busy/PostRun →
    "busy").  For non-active OLSS states, each module keeps its own individual
    state derived from its STAT? readiness flags — the overall OLSS aggregate
    (e.g. "NotReady") may reflect a *different* module still warming up and
    should not be blindly applied to all modules.

    When STAT? is stale (> 5 min) the run-phase token (PRERUN/RUN/POSTRUN) is
    from the previous run and is ignored; only the READY / NOT_READY / ERROR
    flags are used.
    """
    if module_state is None:
        return "unknown"
    if olss_state in ("Run", "Busy", "Prerun", "PostRun"):
        return "busy"
    _stale = stat_age_s is None or stat_age_s > 300
    if _stale and stat_flags is not None:
        # Stale STAT?: strip run-phase tokens and read readiness only.
        fs = {f.upper() for f in stat_flags}
        if "ERROR" in fs:
            return "error"
        if "NOT_READY" in fs or "NOTREADY" in fs:
            return "not_ready"
        if "READY" in fs:
            return "ready"
    if _stale and olss_state == "Idle":
        # No flags, but OLSS confirms the whole system is idle → safe to say ready.
        return "ready"
    return module_state


def _build_pump_component(
    signals: dict[str, Any], connected: bool, olss_state: str | None
) -> ComponentStatus | None:
    state_raw = signals.get("module_binary_pump_state")
    if state_raw is None:
        return None
    age = signals.get("module_binary_pump_stat_age_s")
    flags: list[str] | None = signals.get("module_binary_pump_stat_flags")
    parts: list[str] = []
    pump_on = signals.get("module_binary_pump_on")
    if pump_on is not None:
        parts.append("pumping" if pump_on else "standby")
    if age is not None and age > 3600:
        parts.append(f"last seen {age / 3600:.1f}h ago")
    return ComponentStatus(
        connected=connected,
        state=_module_state_with_olss(state_raw, olss_state, age, flags),
        message=", ".join(parts) or None,
    )


def _build_dad_component(
    signals: dict[str, Any], connected: bool, olss_state: str | None
) -> ComponentStatus | None:
    state_raw = signals.get("module_dad_detector_state")
    if state_raw is None:
        return None
    age = signals.get("module_dad_detector_stat_age_s")
    flags: list[str] | None = signals.get("module_dad_detector_stat_flags")
    parts: list[str] = []
    lamp_on = signals.get("module_dad_lamp_on")
    if lamp_on is not None:
        parts.append("lamp on" if lamp_on else "lamp off")
    hours_used = signals.get("module_dad_lamp_hours_used")
    hours_rated = signals.get("module_dad_lamp_rated_hours")
    if hours_used is not None and hours_rated:
        parts.append(f"{hours_used:.0f}/{hours_rated}h lamp")
    if age is not None and age > 3600:
        parts.append(f"last seen {age / 3600:.1f}h ago")
    return ComponentStatus(
        connected=connected,
        state=_module_state_with_olss(state_raw, olss_state, age, flags),
        message=", ".join(parts) or None,
    )


def _build_column_component(
    signals: dict[str, Any], connected: bool, olss_state: str | None
) -> ComponentStatus | None:
    state_raw = signals.get("module_column_thermostat_state")
    if state_raw is None:
        return None
    age = signals.get("module_column_thermostat_stat_age_s")
    flags: list[str] | None = signals.get("module_column_thermostat_stat_flags")
    parts: list[str] = []
    thrm_on = signals.get("module_column_thermostat_on")
    if thrm_on is not None:
        parts.append("thermostat on" if thrm_on else "thermostat off")
    if age is not None and age > 3600:
        parts.append(f"last seen {age / 3600:.1f}h ago")
    return ComponentStatus(
        connected=connected,
        state=_module_state_with_olss(state_raw, olss_state, age, flags),
        message=", ".join(parts) or None,
    )


def _build_multisampler_component(
    signals: dict[str, Any], connected: bool, olss_state: str | None
) -> ComponentStatus | None:
    state_raw = signals.get("module_multisampler_state")
    if state_raw is None:
        return None
    age = signals.get("module_multisampler_stat_age_s")
    flags: list[str] | None = signals.get("module_multisampler_stat_flags")
    parts: list[str] = []
    occupied = signals.get("module_multisampler_drawers_occupied")
    total = signals.get("module_multisampler_drawers_total")
    if occupied is not None and total is not None:
        parts.append(f"{occupied}/{total} drawers occupied")
    if age is not None and age > 3600:
        parts.append(f"last seen {age / 3600:.1f}h ago")
    return ComponentStatus(
        connected=connected,
        state=_module_state_with_olss(state_raw, olss_state, age, flags),
        message=", ".join(parts) or None,
    )


def _build_metrics(signals: dict[str, Any]) -> dict[str, MetricValue]:
    """Build the metrics dict from probe signals.

    Only keys that have a value are included.  Missing keys render as "—" in
    the dashboard.  Units follow SI where possible; pressure in bar, temperature
    in °C, flow in mL/min, vacuum in mbar, volume in mL.
    """
    m: dict[str, MetricValue] = {}

    def _put(key: str, value: Any, unit: str | None = None) -> None:
        if value is not None:
            m[key] = MetricValue(value=value, unit=unit)

    # --- Communication OK: derived from OLSS state (no sensor daemon needed) ---
    olss_state: str | None = signals.get("olss_instrument_state")
    if olss_state is not None:
        comm_ok = olss_state not in ("NotConnected",)
        _put("ms_communication_ok", comm_ok)
        _put("pump_communication_ok", comm_ok)
        _put("autosampler_communication_ok", comm_ok)

    # --- MS Vacuum & Source (from sensor daemon JSON file) ---
    _put("turbopump_ready",               signals.get("turbopump_ready"))
    _put("vacuum_level_mbar",             signals.get("vacuum_level_mbar"),            "mbar")
    _put("source_temperature_c",          signals.get("source_temperature_c"),         "\u00b0C")
    _put("source_temperature_setpoint_c", signals.get("source_temperature_setpoint_c"), "\u00b0C")
    _put("drying_gas_flow_lpm",           signals.get("drying_gas_flow_lpm"),          "L/min")
    _put("drying_gas_temperature_c",      signals.get("drying_gas_temperature_c"),     "\u00b0C")
    _put("nebulizer_pressure_psig",       signals.get("nebulizer_pressure_psig"),      "psig")
    _put("hv_ready",                      signals.get("hv_ready"))

    # --- LC System (from sensor daemon JSON file) ---
    _put("system_pressure_bar",           signals.get("system_pressure_bar"),          "bar")
    _put("system_pressure_limit_bar",     signals.get("system_pressure_limit_bar"),    "bar")
    _put("column_temperature_c",          signals.get("column_temperature_c"),         "\u00b0C")
    _put("column_temperature_setpoint_c", signals.get("column_temperature_setpoint_c"), "\u00b0C")
    _put("flow_rate_ml_min",              signals.get("flow_rate_ml_min"),             "mL/min")
    _put("degasser_active",               signals.get("degasser_active"))

    # --- Consumables (from RC driver log + sensor daemon JSON file) ---
    for _slot in ("a1", "a2", "b1", "b2"):
        _put(f"solvent_{_slot}_volume_ml",   signals.get(f"solvent_{_slot}_volume_ml"),  "mL")
        _put(f"solvent_{_slot}_capacity_ml", signals.get(f"solvent_{_slot}_capacity_ml"), "mL")
        _put(f"solvent_{_slot}_low",         signals.get(f"solvent_{_slot}_low"))
    _put("wash_solvent_volume_ml",        signals.get("wash_solvent_volume_ml"),       "mL")
    _put("waste_volume_ml",               signals.get("waste_volume_ml"),              "mL")
    _put("waste_capacity_ml",             signals.get("waste_capacity_ml"),            "mL")
    _put("waste_near_capacity",           signals.get("waste_near_capacity"))
    _put("calibrant_ok",                  signals.get("calibrant_ok"))

    # --- Calibration & Comms (from sensor daemon JSON file) ---
    _put("last_calibration_date",         signals.get("last_calibration_date"))
    _put("leak_detected",                 signals.get("leak_detected"))

    return m


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
