"""Map probe signals to a STATUS_SPEC v1.2 ``EquipmentStatus`` envelope."""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .config import Settings, load_settings
from .control.actions import allowed_actions as _allowed_actions
from .control.consumables import (
    consumable_direction as _consumable_direction,
    is_suppressed as _consumable_suppressed,
    raw_volume_signal as _raw_volume_signal,
)
from .models import (
    PROTOCOL_VERSION,
    ComponentStatus,
    EquipmentStatus,
    ErrorInfo,
    MetricValue,
)

# Avoid a hard import cycle: MosesRunner / ClaimHolder are only imported for
# type-checking.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .control.claims import ClaimHolder
    from .control.consumables import ConsumableAcks
    from .control.runner import MosesRunner


EQUIPMENT_ID = "agilent_uplc_ms"
EQUIPMENT_NAME = "Agilent UPLC-MS"
EQUIPMENT_KIND = "hplc"

# OLSS instrument states that indicate an active acquisition. Must match
# _OLSS_ACTIVE_STATES in control/runner.py (duplicated to avoid import coupling).
_OLSS_ACTIVE_STATES: frozenset[str] = frozenset({"Run", "Running", "Busy", "Prerun", "PostRun"})

# LC module role → component builder. Single source for both the /status
# component cards and the subsystem-fault gate (errored_lc_modules) so the two
# can never disagree about a module's state.
def _lc_module_builders() -> dict[str, Any]:
    return {
        "binary_pump": _build_pump_component,
        "dad_detector": _build_dad_component,
        "column_thermostat": _build_column_component,
        "multisampler": _build_multisampler_component,
    }


def errored_lc_modules(signals: dict[str, Any]) -> list[str]:
    """Return the roles of LC modules currently reporting a hardware ``error``.

    Uses the exact component builders that populate ``/status`` (so the
    subsystem-fault interlock in the control router matches what the dashboard
    renders). A module whose state reconciles to ``busy`` during an active
    acquisition is not counted — only a live ``error`` state. ``connected`` is
    assumed True here.

    Two things put a module in ``error``: a ``STAT?`` reply carrying the ERROR
    flag, and an active hardware fault from the driver's own error channel
    (``lc_fault_module_roles``). The fault roles are unioned in explicitly as
    well as flowing through the builders, because a module that has logged a
    fault but never logged a ``STAT?`` produces no component card at all — and a
    fault must never fail to gate run submission just because we are missing its
    readiness reply.
    """
    olss_state = signals.get("olss_instrument_state")
    faulted: list[str] = []
    for role, builder in _lc_module_builders().items():
        comp = builder(signals, True, olss_state)
        if comp is not None and comp.state == "error":
            faulted.append(role)
    for role in signals.get("lc_fault_module_roles") or ():
        if role not in faulted:
            faulted.append(role)
    return faulted


# --- v1.2 activity span tracking (STATUS_SPEC §2.3) --------------------------
# The primary operation is an ACQUISITION (injection / gradient in progress —
# a run in flight, whether submitted through this sidecar's queue or directly
# in OpenLab). Observed from the acquisition signals, never derived from
# `equipment_status`. Module-level because build_status is otherwise
# stateless: `activity_since` must stamp the instant the value changes, not
# the enclosing probe read.
_activity: str = "unknown"
_activity_since: datetime | None = None


def _note_activity(activity: str) -> tuple[str, datetime | None]:
    global _activity, _activity_since
    if activity != _activity:
        _activity = activity
        _activity_since = datetime.now(timezone.utc)
    return _activity, _activity_since


def build_status(
    signals: dict[str, Any],
    settings: Settings | None = None,
    runner: "MosesRunner | None" = None,
    claims: "ClaimHolder | None" = None,
    consumables: "ConsumableAcks | None" = None,
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

    # An active LC module fault outranks the log-tail error unconditionally: it
    # names a module, carries the driver's own severity and an Agilent event
    # code, and describes hardware rather than software. The displaced log-tail
    # error stays reachable via details.last_error_log_path.
    lc_faults: list[dict[str, Any]] = signals.get("lc_faults") or []
    if lc_faults:
        top_fault = lc_faults[0]
        last_error = ErrorInfo(
            code=top_fault.get("code"),
            message=signals.get("lc_fault_message") or top_fault.get("message", ""),
            severity=top_fault.get("severity", "error"),
            timestamp=_parse_iso(top_fault.get("timestamp"))
            or datetime.now(timezone.utc),
        )

    probe_error: str | None = signals.get("probe_error")

    waste_near_capacity: bool = bool(signals.get("waste_near_capacity"))
    solvent_a1_low: bool = bool(signals.get("solvent_a1_low"))
    solvent_a2_low: bool = bool(signals.get("solvent_a2_low"))
    solvent_b1_low: bool = bool(signals.get("solvent_b1_low"))
    solvent_b2_low: bool = bool(signals.get("solvent_b2_low"))

    # Suppress a consumable warning the operator has acknowledged (emptied waste
    # / refilled a solvent) until the raw OpenLab estimate shows it is genuinely
    # due again. Pure read of the ack store (keeps /status side-effect-free); the
    # timestamps of any *active* suppression are surfaced in details below.
    suppressed_consumables: dict[str, str] = {}
    if consumables is not None:
        delta = float(settings.consumable_rearm_delta_ml)

        def _suppress(key: str, warn: bool) -> bool:
            if not warn:
                return warn
            ack = consumables.get(key)
            raw = signals.get(_raw_volume_signal(key))
            if _consumable_suppressed(ack, raw, _consumable_direction(key), delta):
                suppressed_consumables[key] = ack.get("acked_at", "")  # type: ignore[union-attr]
                return False
            return warn

        waste_near_capacity = _suppress("waste", waste_near_capacity)
        solvent_a1_low = _suppress("a1", solvent_a1_low)
        solvent_a2_low = _suppress("a2", solvent_a2_low)
        solvent_b1_low = _suppress("b1", solvent_b1_low)
        solvent_b2_low = _suppress("b2", solvent_b2_low)

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
        if lc_faults:
            message = f"LC module fault — {last_error.message}"
            # Surfaced here as well as in the `faulted_modules` block below,
            # which only fires from `ready`; a fault puts us straight in `error`.
            required_actions = [
                f"check_{role}" for role in signals.get("lc_fault_module_roles") or ()
            ]
        else:
            message = "Recent OpenLab error event in log tail"
            required_actions = []
    elif sequence_paused:
        # OLSS "Paused" is not a legal EquipmentState (v1.1 dropped "paused").
        # Report "busy" — the instrument is mid-sequence and unavailable — and
        # surface the resume action. The precise OLSS status is preserved in
        # details.olss_software_status and the hplc/ms component state below.
        equipment_state = "busy"
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

    # v1.2 activity (§2.3): computed INDEPENDENTLY of the health precedence
    # above, from the same observed acquisition signals — so a run that is in
    # flight when an error lands still reports `error` + activity "running"
    # (health-first, §2.2, without losing the run). A paused sequence is an
    # operation in progress, not a finished one → "running" (see README).
    acquiring = bool(
        sequence_paused
        or signals.get("acquisition_active")
        or signals.get("moses_process_alive")
        or olss_acquiring
    )
    if probe_error:
        observed_activity = "unknown"  # we cannot observe the instrument
    elif not core_up:
        observed_activity = "idle"  # §2.3 invariant: requires_init ⇒ idle
    else:
        observed_activity = "running" if acquiring else "idle"
    activity, activity_since = _note_activity(observed_activity)

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

    # Post-run pressure drift is ADVISORY: it appends an action but never changes
    # equipment_status and never joins faulted_modules, so it cannot halt the lab
    # on a heuristic that has no tuning data behind it yet. Promoting it to a
    # blocking condition is a deliberate follow-up once a baseline exists —
    # see docs/fault_detection.md.
    if signals.get("run_pressure_drift"):
        required_actions = list(required_actions) + ["check_lc_pressure"]

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

    for role, builder in _lc_module_builders().items():
        comp = builder(signals, _lc_module_conn, olss_state)
        if comp is not None:
            components[role] = comp

    # §2.2: an LC module reporting a hardware `error` is an active subsystem
    # fault. The top-level MUST NOT stay `ready` while it knows of one. If we are
    # otherwise ready, downgrade to `degraded` (MS/comms are up, but a run can't
    # safely start) and surface a per-module check action. `busy`/`error`/
    # `requires_init` are left as-is: a fault mid-run is caught by OLSS/log-tail
    # `error`, and requires_init is the more fundamental condition. This same
    # module-error set gates enqueue actions (below and in the control router).
    # Must stay identical to errored_lc_modules(), which the control router uses
    # for the same gate — hence the same union of component-error and
    # fault-derived roles (see that function for why the union is needed).
    faulted_modules = [
        role
        for role in _lc_module_builders()
        if role in components and components[role].state == "error"
    ]
    for role in signals.get("lc_fault_module_roles") or ():
        if role not in faulted_modules:
            faulted_modules.append(role)
    if faulted_modules and equipment_state == "ready":
        equipment_state = "degraded"
        message = (
            "Subsystem fault — LC module(s) reporting error: "
            + ", ".join(faulted_modules)
        )
        required_actions = list(required_actions) + [
            f"check_{role}" for role in faulted_modules
        ]

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
    # Full fault list (most actionable first) so the dashboard can show the
    # cascade — one module's leak shuts the other three down — not just the
    # single fault promoted to last_error.
    if lc_faults:
        details["lc_faults"] = lc_faults
    if signals.get("run_pressure_run"):
        details["run_pressure"] = {
            "run": signals.get("run_pressure_run"),
            "method": signals.get("run_pressure_method"),
            "max_bar": signals.get("run_pressure_max_bar"),
            "min_bar": signals.get("run_pressure_min_bar"),
            "mean_bar": signals.get("run_pressure_mean_bar"),
            "duration_s": signals.get("run_pressure_duration_s"),
            "baseline_bar": signals.get("run_pressure_baseline_bar"),
            "baseline_n": signals.get("run_pressure_baseline_n"),
            "delta_pct": signals.get("run_pressure_delta_pct"),
            "drift": bool(signals.get("run_pressure_drift")),
        }
    # Consumable flags reflect the *effective* (post-acknowledgment) state so
    # details never disagree with required_actions.
    _solvent_low = {
        "a1": solvent_a1_low, "a2": solvent_a2_low,
        "b1": solvent_b1_low, "b2": solvent_b2_low,
    }
    if waste_near_capacity:
        details["waste_near_capacity"] = True
    for _slot, _low in _solvent_low.items():
        if _low:
            details[f"solvent_{_slot}_low"] = True
    # Surface the acknowledgment timestamp of each *currently suppressing* ack so
    # the dashboard can show "emptied/refilled at …" instead of the warning.
    for _key, _acked_at in suppressed_consumables.items():
        _label = "waste" if _key == "waste" else f"solvent_{_key}"
        details[f"{_label}_reset_at"] = _acked_at

    # v1.1: surface the current claim holder (null when unclaimed/expired).
    claimed_by = claims.current() if claims is not None else None
    details["claimed_by"] = claimed_by.model_dump(mode="json") if claimed_by is not None else None

    # Keep the runner's servicing auto-detect current (keyed on a real OLSS run,
    # i.e. olss_current_run) without importing any probe code.
    if runner is not None:
        runner.notify_olss_state(olss_state, olss_sw_status, signals.get("olss_current_run"))

    # v1.1 allowed_actions (§6.2): single source of truth shared with the
    # /control/* router so the advisory list can never disagree with what the
    # endpoints would actually honour. probe_error → we cannot reason about the
    # instrument, so offer nothing. ``notify_olss_state`` above has already
    # refreshed the runner's servicing debounce from this observation.
    queue_full = runner.is_queue_full(settings) if runner is not None else False
    servicing = runner.is_servicing(settings) if runner is not None else False
    service_mode = runner.service_mode() if runner is not None else False
    workflow_active = claims.is_workflow_active() if claims is not None else False
    allowed = _allowed_actions(
        service_operational=(probe_error is None),
        requires_init=(equipment_state == "requires_init"),
        queue_full=queue_full,
        servicing=servicing,
        service_mode=service_mode,
        workflow_active=workflow_active,
        subsystem_fault=bool(faulted_modules),
    )

    # Surface the precedence state so the dashboard can explain *why* the
    # instrument is busy / not accepting. equipment_status stays "busy" (set
    # above from the OLSS/acquisition signals); these refine the human message.
    if runner is not None:
        # Persistent admin toggle, distinct from the (possibly auto-detected)
        # servicing state above. The dashboard reads this to render its toggle,
        # and it is the source that refuses an enqueue outright.
        details["service_mode"] = service_mode
    if servicing:
        details["servicing"] = True
        if equipment_state == "busy":
            message = "Instrument in use via OpenLab CDS (technician servicing)"
    if workflow_active:
        details["workflow_active"] = True
        if claimed_by is not None and equipment_state == "busy":
            message = f"Autonomous workflow running (held by {claimed_by.owner!r})"
    if faulted_modules:
        details["subsystem_fault_modules"] = faulted_modules

    return EquipmentStatus(
        # Explicit: the shared contract model's field default is "1.0" (a
        # device that doesn't state its version is a pre-spec device).
        protocol_version=PROTOCOL_VERSION,
        equipment_id=EQUIPMENT_ID,
        equipment_name=EQUIPMENT_NAME,
        equipment_kind=EQUIPMENT_KIND,
        equipment_version=__version__,
        host=socket.gethostname(),
        equipment_status=equipment_state,
        message=message,
        required_actions=required_actions,
        activity=activity,  # type: ignore[arg-type]
        activity_since=activity_since,
        device_time=datetime.now(timezone.utc),
        components=components,
        metrics=_build_metrics(signals),
        last_error=last_error,
        allowed_actions=allowed,
        details=details,
    )


def _module_state(
    signals: dict[str, Any],
    role: str,
    module_state: str | None,
    olss_state: str | None,
    stat_age_s: float | None,
    stat_flags: list[str] | None = None,
) -> str:
    """Component state for one LC module, faults taking precedence.

    An active hardware fault (see ``probes/rc_driver_log.read_lc_faults``) beats
    whatever ``STAT?`` last said: a module can be mid-run — and so reporting
    ``busy`` — while the driver has already logged a leak against it. Routing
    faults through here rather than a parallel list is what keeps
    ``errored_lc_modules`` (and so the control-side ``subsystem_fault``
    interlock) in agreement with the component cards.
    """
    if role in (signals.get("lc_fault_module_roles") or ()):
        return "error"
    return _module_state_with_olss(module_state, olss_state, stat_age_s, stat_flags)


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
        state=_module_state(signals, "binary_pump", state_raw, olss_state, age, flags),
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
        state=_module_state(signals, "dad_detector", state_raw, olss_state, age, flags),
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
        state=_module_state(signals, "column_thermostat", state_raw, olss_state, age, flags),
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
        state=_module_state(signals, "multisampler", state_raw, olss_state, age, flags),
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

    # --- Post-run pump pressure QC (from the archived .dx traces) ---
    # Distinct from system_pressure_bar below: these describe the last COMPLETED
    # run, not the live instrument, which has no pressure feed on this setup.
    _put("run_pressure_max_bar",          signals.get("run_pressure_max_bar"),         "bar")
    _put("run_pressure_min_bar",          signals.get("run_pressure_min_bar"),         "bar")
    _put("run_pressure_mean_bar",         signals.get("run_pressure_mean_bar"),        "bar")
    _put("run_pressure_baseline_bar",     signals.get("run_pressure_baseline_bar"),    "bar")
    _put("run_pressure_delta_pct",        signals.get("run_pressure_delta_pct"),       "%")

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
