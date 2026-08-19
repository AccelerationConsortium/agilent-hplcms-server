"""LC module hardware-fault detection from the RC driver logs.

Fixture lines are reproductions of real ``RCDriver.log`` output from this
instrument, including the parts that make parsing awkward: the trailing
``;Category:`` field, the optional ``[code code, n]`` bracket, and the
``Communication error.`` variant that ends in a period and carries no code.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from agilent_hplcms_server.config import Settings
from agilent_hplcms_server.probes.rc_driver_log import (
    read_lc_faults,
    read_module_states,
)
from agilent_hplcms_server.status_builder import build_status, errored_lc_modules

_TAIL = (
    ";Category: Agilent.LCDrivers.RapidControl.RCDriverBase, Debug;Priority: 3;"
    "Process Name: C:\\Program Files (x86)\\Agilent Technologies\\OpenLab "
    "Acquisition\\AcquisitionServer.exe;Extended Properties: ModuleShortname - "
    "Agilent.LCDrivers.RapidControl.RCDriverBase;"
)


def _stamp(when: datetime) -> str:
    return when.strftime("%d-%m-%Y %H:%M:%S.%f")[:-3]


def _fault_line(when: datetime, token: str, module: str, text: str) -> str:
    """One `ControlIF log error` line, in the driver's exact shape."""
    return (
        f"EventId: 558668619;Timestamp: {_stamp(when)};Thread Id: 36;"
        f"Message: ControlIF log error: {token}, {module} - {text}" + _TAIL
    )


def _stat_line(when: datetime, module: str, flags: str) -> str:
    return (
        f"EventId: 6481076;Timestamp: {_stamp(when)};Thread Id: 24;"
        f"Message: LDT SendInstruction: Module:[{module}]; Instruction:[STAT?]; "
        f'Reply:[[{module}:IN]: RA 00000 STAT "{flags}"]' + _TAIL
    )


def _write_log(log_dir: Path, lines: list[str], name: str = "RCDriver.log") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _ago(seconds: float) -> datetime:
    return datetime.now() - timedelta(seconds=seconds)


# --- parsing -----------------------------------------------------------------


def test_parses_critical_fault_with_code(tmp_path: Path):
    _write_log(
        tmp_path,
        [
            _fault_line(
                _ago(60),
                "eLogAndAbortSequence",
                "G7167B:DEBAS04772",
                "Leak detected [64 64, 0]",
            )
        ],
    )
    out = read_lc_faults(tmp_path)

    assert out["lc_fault_active"] is True
    assert out["lc_fault_severity"] == "critical"
    assert out["lc_fault_message"] == "G7167B multisampler: Leak detected"
    assert out["lc_fault_module_roles"] == ["multisampler"]

    (fault,) = out["lc_faults"]
    assert fault["message"] == "Leak detected"
    assert fault["code"] == "64"
    assert fault["role"] == "multisampler"
    assert fault["serial"] == "DEBAS04772"


def test_parses_fault_without_code_and_trailing_period(tmp_path: Path):
    """`Communication error.` carries no [code] bracket and ends in a period."""
    _write_log(
        tmp_path,
        [
            _fault_line(
                _ago(60),
                "eLogAndAbortSequence",
                "G7120A:DEBA201988",
                "Communication error.",
            )
        ],
    )
    (fault,) = read_lc_faults(tmp_path)["lc_faults"]

    assert fault["message"] == "Communication error"
    assert fault["code"] is None
    assert fault["role"] == "binary_pump"


def test_abort_current_run_only_is_error_not_critical(tmp_path: Path):
    _write_log(
        tmp_path,
        [
            _fault_line(
                _ago(60),
                "eLogAndAbortCurrentRunOnly",
                "G7120A:DEBA201988",
                "Pressure above upper limit [22001 22001, 0]",
            )
        ],
    )
    assert read_lc_faults(tmp_path)["lc_fault_severity"] == "error"


def test_information_messages_are_not_faults(tmp_path: Path):
    """The bulk of this channel is routine chatter and must be ignored."""
    _write_log(
        tmp_path,
        [
            _fault_line(
                _ago(60),
                "eLogInformationMessage",
                "G7167B:DEBAS04772",
                "Valve is switched to bypass",
            ),
            _fault_line(
                _ago(60),
                "eLogInformationMessage",
                "G7117B:DEBAW05689",
                "Detector: Idle",
            ),
        ],
    )
    assert read_lc_faults(tmp_path) == {}


def test_controller_stop_request_is_not_a_hardware_fault(tmp_path: Path):
    """An abort we asked for is not a fault needing a human."""
    _write_log(
        tmp_path,
        [
            _fault_line(
                _ago(60),
                "eLogAndAbortCurrentRunOnly",
                "G7120A:DEBA201988",
                "Controller stop automation request",
            )
        ],
    )
    assert read_lc_faults(tmp_path) == {}


def test_timestamp_carries_local_offset(tmp_path: Path):
    """Driver timestamps are local; emitting them naive would read as UTC."""
    _write_log(
        tmp_path,
        [
            _fault_line(
                _ago(60), "eLogAndAbortSequence", "G7167B:DEBAS04772", "Leak detected"
            )
        ],
    )
    (fault,) = read_lc_faults(tmp_path)["lc_faults"]
    assert datetime.fromisoformat(fault["timestamp"]).tzinfo is not None


# --- ranking and de-duplication ----------------------------------------------


def test_causal_fault_outranks_the_shutdown_cascade(tmp_path: Path):
    """One module's leak shuts the rest down; the leak is what to report."""
    when = _ago(60)
    _write_log(
        tmp_path,
        [
            _fault_line(when, "eLogAndAbortSequence", "G7120A:DEBA201988", "Shutdown [63 63, 0]"),
            _fault_line(when, "eLogAndAbortSequence", "G7117B:DEBAW05689", "Shutdown [63 63, 0]"),
            _fault_line(when, "eLogAndAbortSequence", "G7167B:DEBAS04772", "Leak detected [64 64, 0]"),
        ],
    )
    out = read_lc_faults(tmp_path)

    assert out["lc_fault_message"] == "G7167B multisampler: Leak detected"
    assert len(out["lc_faults"]) == 3
    assert out["lc_fault_module_roles"] == ["binary_pump", "dad_detector", "multisampler"]


def test_repeated_fault_lines_are_deduplicated(tmp_path: Path):
    """The driver writes each fault more than once, and logs overlap."""
    when = _ago(60)
    line = _fault_line(when, "eLogAndAbortSequence", "G7167B:DEBAS04772", "Leak detected [64 64, 0]")
    _write_log(tmp_path, [line, line])
    _write_log(tmp_path, [line], name="abc-def-RCDriver.log")

    assert len(read_lc_faults(tmp_path)["lc_faults"]) == 1


def test_rotated_log_inside_the_window_is_scanned(tmp_path: Path):
    """RCDriver.log rotates at 10 MB — ~25 min on a busy day."""
    _write_log(
        tmp_path,
        [_fault_line(_ago(60), "eLogAndAbortSequence", "G7167B:DEBAS04772", "Leak detected")],
        name="RCDriver.2026-08-05 13.03.51.log",
    )
    _write_log(tmp_path, ["nothing interesting here"])

    assert read_lc_faults(tmp_path)["lc_fault_module_roles"] == ["multisampler"]


# --- clearing ----------------------------------------------------------------


def test_fault_older_than_the_window_is_dropped(tmp_path: Path):
    """The driver never logs a fault-cleared line, so the window is the floor."""
    _write_log(
        tmp_path,
        [
            _fault_line(
                _ago(7200), "eLogAndAbortSequence", "G7167B:DEBAS04772", "Leak detected"
            )
        ],
    )
    assert read_lc_faults(tmp_path, window_s=3600) == {}


def test_window_of_zero_disables_detection(tmp_path: Path):
    _write_log(
        tmp_path,
        [_fault_line(_ago(60), "eLogAndAbortSequence", "G7167B:DEBAS04772", "Leak detected")],
    )
    assert read_lc_faults(tmp_path, window_s=0) == {}


# Every STAT? reply this instrument emits carries a run-phase token — 173 of 173
# across all retained logs are PRERUN — so recovery fixtures must include one.
# A READY without it is a reply the hardware never sends, and testing against
# that shape is what hid the recovery check being dead code: the composite state
# ranks PRERUN above READY, so it was never equal to "ready" in the field.
_READY = "PRERUN, NO_ANALYSIS, NO_ERROR, READY, NO_TEST"
_NOT_READY = "PRERUN, NO_ANALYSIS, NO_ERROR, NOT_READY, NO_TEST"
_ERRORED = "PRERUN, NO_ANALYSIS, ERROR, NOT_READY, NO_TEST"


def test_fault_clears_once_the_module_reports_ready_again(tmp_path: Path):
    _write_log(
        tmp_path,
        [
            _fault_line(_ago(600), "eLogAndAbortSequence", "G7167B:DEBAS04772", "Leak detected"),
            _stat_line(_ago(30), "G7167B:DEBAS04772", _READY),
        ],
    )
    module_states = read_module_states(tmp_path)
    # The composite state stays "busy" — the run-phase token outranks READY for
    # the component card. Recovery must be read from the readiness flags.
    assert module_states["module_multisampler_state"] == "busy"
    assert "READY" in module_states["module_multisampler_stat_flags"]

    assert read_lc_faults(tmp_path, module_states=module_states) == {}


def test_fault_survives_a_ready_that_predates_it(tmp_path: Path):
    """A READY from before the leak says nothing about whether it was fixed."""
    _write_log(
        tmp_path,
        [
            _stat_line(_ago(600), "G7167B:DEBAS04772", _READY),
            _fault_line(_ago(30), "eLogAndAbortSequence", "G7167B:DEBAS04772", "Leak detected"),
        ],
    )
    module_states = read_module_states(tmp_path)

    out = read_lc_faults(tmp_path, module_states=module_states)
    assert out["lc_fault_module_roles"] == ["multisampler"]


def test_fault_survives_while_the_module_is_still_not_ready(tmp_path: Path):
    """A newer STAT? only clears the fault if it says the module came back."""
    _write_log(
        tmp_path,
        [
            _fault_line(_ago(600), "eLogAndAbortSequence", "G7167B:DEBAS04772", "Leak detected"),
            _stat_line(_ago(30), "G7167B:DEBAS04772", _NOT_READY),
        ],
    )
    module_states = read_module_states(tmp_path)

    out = read_lc_faults(tmp_path, module_states=module_states)
    assert out["lc_fault_module_roles"] == ["multisampler"]


def test_fault_survives_a_latched_error_flag(tmp_path: Path):
    """A module still reporting the ERROR flag has not recovered."""
    _write_log(
        tmp_path,
        [
            _fault_line(_ago(600), "eLogAndAbortSequence", "G7167B:DEBAS04772", "Leak detected"),
            _stat_line(_ago(30), "G7167B:DEBAS04772", _ERRORED),
        ],
    )
    module_states = read_module_states(tmp_path)

    out = read_lc_faults(tmp_path, module_states=module_states)
    assert out["lc_fault_module_roles"] == ["multisampler"]


def test_recovery_needs_readiness_not_just_a_newer_stat(tmp_path: Path):
    """The real 2026-08-19 multisampler incident, end to end.

    Transport faults, then the module reports NO_ERROR/READY at the prerun of
    the next (successful) run. Before the readiness fix the fault rode out the
    full one-hour window and `/status` stayed `error` for ~57 min after the
    instrument had demonstrably recovered.
    """
    _write_log(
        tmp_path,
        [
            _fault_line(
                _ago(3490), "eLogAndAbortSequence", "G7167B:DEBAS04772",
                "Sampler transport initialization failed",
            ),
            _fault_line(
                _ago(3430), "eLogAndAbortSequence", "G7167B:DEBAS04772", "Connection lost"
            ),
            _stat_line(_ago(3180), "G7167B:DEBAS04772", _READY),
        ],
    )
    module_states = read_module_states(tmp_path)

    assert read_lc_faults(tmp_path, module_states=module_states) == {}


# --- /status integration ------------------------------------------------------


def _healthy_signals(**overrides) -> dict:
    signals = {
        "openlab_acquisition_alive": True,
        "openlab_instrument_service_alive": True,
        "openlab_reverse_proxy_alive": True,
        "moses_process_alive": False,
        "acquisition_active": False,
        "last_error": None,
        "probe_error": None,
        "olss_instrument_state": "Idle",
    }
    signals.update(overrides)
    return signals


_LEAK = {
    "lc_faults": [
        {
            "module_code": "G7167B",
            "role": "multisampler",
            "serial": "DEBAS04772",
            "message": "Leak detected",
            "code": "64",
            "severity": "critical",
            "timestamp": "2026-07-30T15:38:52.169000+00:00",
            "age_s": 60.0,
        }
    ],
    "lc_fault_active": True,
    "lc_fault_severity": "critical",
    "lc_fault_message": "G7167B multisampler: Leak detected",
    "lc_fault_module_roles": ["multisampler"],
}


def test_status_reports_error_with_the_fault_as_last_error():
    status = build_status(_healthy_signals(**_LEAK), settings=Settings())

    assert status.equipment_status == "error"
    assert status.last_error is not None
    assert status.last_error.message == "G7167B multisampler: Leak detected"
    assert status.last_error.code == "64"
    assert status.last_error.severity == "critical"
    assert "LC module fault" in (status.message or "")
    assert "check_multisampler" in status.required_actions
    assert status.details["lc_faults"] == _LEAK["lc_faults"]


def test_fault_outranks_the_log_tail_error():
    signals = _healthy_signals(
        last_error={
            "code": None,
            "message": "some ERROR line from a log tail",
            "severity": "error",
            "timestamp": "2026-08-11T10:00:00+00:00",
        },
        **_LEAK,
    )
    status = build_status(signals, settings=Settings())

    assert status.last_error is not None
    assert status.last_error.message == "G7167B multisampler: Leak detected"


def test_fault_blocks_run_submission():
    """The point of the whole exercise: no run launches into faulted hardware."""
    status = build_status(_healthy_signals(**_LEAK), settings=Settings())

    assert "run.submit" not in status.allowed_actions
    assert "run.abort" in status.allowed_actions
    assert status.details["subsystem_fault_modules"] == ["multisampler"]


def test_router_interlock_sees_the_fault():
    """errored_lc_modules backs the router's 409 subsystem_fault refusal."""
    assert errored_lc_modules(_healthy_signals(**_LEAK)) == ["multisampler"]


def test_faulted_module_component_shows_error_even_while_busy():
    signals = _healthy_signals(
        olss_instrument_state="Running",
        module_multisampler_state="busy",
        module_multisampler_stat_age_s=10.0,
        **_LEAK,
    )
    status = build_status(signals, settings=Settings())

    assert status.components["multisampler"].state == "error"
    # A run in flight is still reported as running (v1.2 §2.3, health-first).
    assert status.activity == "running"


def test_no_fault_leaves_status_untouched():
    status = build_status(_healthy_signals(), settings=Settings())

    assert status.equipment_status == "ready"
    assert status.last_error is None
    assert "lc_faults" not in status.details
    assert "run.submit" in status.allowed_actions
