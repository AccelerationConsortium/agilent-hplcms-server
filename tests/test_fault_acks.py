"""Operator acknowledgment of LC module hardware faults.

The scenario throughout is the real one from 2026-08-20 20:53:47, which is what
motivated the feature: the multisampler drove its needle into the top of a vessel
in drawer D2F, threw three critical faults in 100 ms, and took the sequence down
with it — the pump, DAD and column thermostat each logging "Analysis aborted by
another module". OpenLab went back to green within minutes, but the sidecar held
``error`` (and refused ``run.submit``) for the full hour: the driver never logs a
fault-cleared line, and the module's own ``STAT?`` — the one recovery signal the
probe can read — is only written at prerun, so an instrument sitting idle can
never be observed to recover.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from agilent_hplcms_server.config import Settings
from agilent_hplcms_server.control.fault_acks import (
    FaultAcks,
    apply_fault_acks,
    evidence_at_ack,
)
from agilent_hplcms_server.status_builder import build_status, errored_lc_modules

# The driver stamps its logs in naive local time; the probe carries fault
# timestamps back out with the local offset attached and STAT? times naive. The
# two clock domains meeting is the whole reason ack thresholds are stored as
# event times rather than as a UTC wall clock, so the fixtures keep them apart.
_FAULT_AT = datetime(2026, 8, 20, 20, 53, 47, 142000).astimezone()
_STAT_AT = datetime(2026, 8, 20, 20, 53, 47, 584000)


def _fault(
    role: str,
    module_code: str,
    message: str,
    *,
    severity: str = "critical",
    code: str | None = None,
    at: datetime | None = None,
    age_s: float = 900.0,
) -> dict[str, Any]:
    return {
        "module_code": module_code,
        "role": role,
        "serial": "DEBAS04772",
        "message": message,
        "code": code,
        "severity": severity,
        "timestamp": (at or _FAULT_AT).isoformat(),
        "age_s": age_s,
    }


_NEEDLE_CRASH = [
    _fault("multisampler", "G7167B", "Needle command failed", code="25022"),
    _fault("multisampler", "G7167B", "Pusher hit the vessel top", code="25225"),
]
_CASCADE = [
    _fault("binary_pump", "G7120A", "Analysis aborted by another module", severity="error"),
    _fault("dad_detector", "G7117B", "Analysis aborted by another module", severity="error"),
    _fault(
        "column_thermostat", "G7116B", "Analysis aborted by another module", severity="error"
    ),
]


def _signals(*, faults: list[dict] | None = None, **overrides: Any) -> dict[str, Any]:
    """A healthy instrument carrying the given faults and a stale STAT? per module.

    The STAT? flags mirror what the modules actually replied as the sequence
    aborted: the multisampler latched ERROR, the rest went NOT_READY. Every reply
    also carries PRERUN — this instrument stamps all 173 of them that way — which
    is why readiness must be read from the flags and not the composite state.
    """
    faults = _NEEDLE_CRASH + _CASCADE if faults is None else faults
    signals: dict[str, Any] = {
        "openlab_acquisition_alive": True,
        "openlab_instrument_service_alive": True,
        "openlab_reverse_proxy_alive": True,
        "moses_process_alive": False,
        "acquisition_active": False,
        "last_error": None,
        "probe_error": None,
        "olss_instrument_state": "Idle",
        "olss_software_status": "OK",
    }
    for role, flags in (
        ("multisampler", ["PRERUN", "NO_ANALYSIS", "ERROR", "NOT_READY", "NO_TEST"]),
        ("binary_pump", ["PRERUN", "NO_ANALYSIS", "NO_ERROR", "NOT_READY", "NO_TEST"]),
        ("dad_detector", ["PRERUN", "NO_ANALYSIS", "NO_ERROR", "NOT_READY", "NO_TEST"]),
        ("column_thermostat", ["PRERUN", "NO_ANALYSIS", "NO_ERROR", "NOT_READY", "NO_TEST"]),
    ):
        signals[f"module_{role}_stat_flags"] = flags
        signals[f"module_{role}_state"] = "error" if "ERROR" in flags else "not_ready"
        signals[f"module_{role}_stat_age_s"] = 900.0
        signals[f"module_{role}_stat_at"] = _STAT_AT.isoformat()
    if faults:
        signals["lc_faults"] = faults
        signals["lc_fault_active"] = True
        signals["lc_fault_severity"] = faults[0]["severity"]
        signals["lc_fault_module_roles"] = sorted({f["role"] for f in faults})
        top = faults[0]
        signals["lc_fault_message"] = (
            f"{top['module_code']} {top['role']}: {top['message']}"
        )
    signals.update(overrides)
    return signals


def _acked(*roles: str, signals: dict[str, Any] | None = None) -> FaultAcks:
    """A path-less (in-memory) store holding an ack for each role."""
    store = FaultAcks(None)
    for role in roles:
        store.record(role, signals if signals is not None else _signals(), owner="tech")
    return store


# --- the incident, before any acknowledgment ---------------------------------


def test_the_incident_reports_error_and_blocks_submission():
    """Baseline: without an ack this is exactly what /status showed all evening."""
    status = build_status(_signals(), settings=Settings())

    assert status.equipment_status == "error"
    assert status.last_error is not None
    assert status.last_error.message == "G7167B multisampler: Needle command failed"
    assert sorted(status.details["subsystem_fault_modules"]) == [
        "binary_pump",
        "column_thermostat",
        "dad_detector",
        "multisampler",
    ]
    assert "run.submit" not in status.allowed_actions


# --- acknowledging -----------------------------------------------------------


def test_ack_clears_the_module_and_reopens_submission():
    signals = _signals(faults=_NEEDLE_CRASH)
    status = build_status(
        signals, settings=Settings(), fault_acks=_acked("multisampler", signals=signals)
    )

    assert status.equipment_status == "ready"
    assert status.last_error is None
    assert "subsystem_fault_modules" not in status.details
    assert "run.submit" in status.allowed_actions
    assert "check_multisampler" not in status.required_actions


def test_ack_must_clear_the_stale_stat_error_too_or_the_card_stays_red():
    """The fault list alone is not enough.

    ``_module_state_with_olss`` reads a stale STAT?'s ERROR flag straight into a
    component state of ``error``, so an ack that only filtered ``lc_faults``
    would leave the multisampler red — and ``faulted_modules`` with it.
    """
    signals = _signals(faults=_NEEDLE_CRASH)
    acks = _acked("multisampler", signals=signals)

    assert signals["module_multisampler_stat_flags"].count("ERROR") == 1
    applied = apply_fault_acks(signals, acks.snapshot())
    assert "ERROR" not in applied["module_multisampler_stat_flags"]

    status = build_status(signals, settings=Settings(), fault_acks=acks)
    assert status.components["multisampler"].state == "not_ready"


def test_ack_does_not_invent_a_ready_the_module_never_sent():
    """not_ready, never ready: STAT? is prerun-only, so nothing has said READY."""
    signals = _signals(faults=_NEEDLE_CRASH)
    status = build_status(
        signals, settings=Settings(), fault_acks=_acked("multisampler", signals=signals)
    )

    assert status.components["multisampler"].state == "not_ready"
    assert status.components["multisampler"].state != "ready"


def test_ack_surfaces_who_and_when_on_the_dashboard():
    signals = _signals(faults=_NEEDLE_CRASH)
    status = build_status(
        signals, settings=Settings(), fault_acks=_acked("multisampler", signals=signals)
    )

    assert "multisampler" in status.details["fault_acks"]


def test_router_interlock_honours_the_ack():
    """The /status view and the 409 subsystem_fault refusal must not diverge."""
    signals = _signals(faults=_NEEDLE_CRASH)
    acks = _acked("multisampler", signals=signals)

    assert errored_lc_modules(signals) == ["multisampler"]
    assert errored_lc_modules(signals, acks) == []


# --- scope: an ack covers one module, and only the evidence it saw -----------


def test_ack_is_per_module_and_leaves_the_cascade_alone():
    """Acking the module that crashed does not silence the three it took down."""
    signals = _signals()
    status = build_status(
        signals, settings=Settings(), fault_acks=_acked("multisampler", signals=signals)
    )

    assert status.equipment_status == "error"
    assert sorted(status.details["subsystem_fault_modules"]) == [
        "binary_pump",
        "column_thermostat",
        "dad_detector",
    ]
    assert "multisampler" not in status.details["subsystem_fault_modules"]


def test_acking_every_module_clears_the_whole_cascade():
    signals = _signals()
    acks = _acked(
        "multisampler", "binary_pump", "dad_detector", "column_thermostat", signals=signals
    )
    status = build_status(signals, settings=Settings(), fault_acks=acks)

    assert status.equipment_status == "ready"
    assert errored_lc_modules(signals, acks) == []


def test_a_newer_fault_rearms_the_module():
    """The ack covers what the operator saw; the next failure is not theirs."""
    signals = _signals(faults=_NEEDLE_CRASH)
    acks = _acked("multisampler", signals=signals)

    later = _signals(
        faults=[
            _fault(
                "multisampler",
                "G7167B",
                "Leak detected",
                code="64",
                at=_FAULT_AT + timedelta(minutes=5),
                age_s=60.0,
            )
        ]
    )
    status = build_status(later, settings=Settings(), fault_acks=acks)

    assert status.equipment_status == "error"
    assert status.last_error is not None
    assert status.last_error.message == "G7167B multisampler: Leak detected"
    assert errored_lc_modules(later, acks) == ["multisampler"]


def test_a_newer_stat_still_in_error_rearms_the_module():
    """A fresh prerun reply that still says ERROR is evidence nobody has cleared."""
    signals = _signals(faults=_NEEDLE_CRASH)
    acks = _acked("multisampler", signals=signals)

    later = _signals(faults=[])
    later["module_multisampler_stat_at"] = (_STAT_AT + timedelta(minutes=5)).isoformat()

    assert errored_lc_modules(later, acks) == ["multisampler"]
    assert build_status(later, settings=Settings(), fault_acks=acks).equipment_status == "degraded"


def test_ack_on_a_clean_module_is_inert_and_grants_no_future_amnesty():
    """Acking nothing records null thresholds, which suppress nothing."""
    clean = _signals(faults=[])
    clean["module_multisampler_stat_flags"] = ["PRERUN", "NO_ANALYSIS", "NO_ERROR", "READY"]
    clean["module_multisampler_state"] = "busy"
    acks = _acked("multisampler", signals=clean)

    ack = acks.get("multisampler")
    assert ack is not None and ack["faults_through"] is None

    # A fault that lands afterwards is fully in force.
    later = _signals(faults=_NEEDLE_CRASH)
    assert errored_lc_modules(later, acks) == ["multisampler"]


def test_withdrawing_an_ack_restores_the_fault():
    signals = _signals(faults=_NEEDLE_CRASH)
    acks = _acked("multisampler", signals=signals)
    assert errored_lc_modules(signals, acks) == []

    assert acks.clear("multisampler") is True
    assert errored_lc_modules(signals, acks) == ["multisampler"]
    assert acks.clear("multisampler") is False


# --- purity and persistence ---------------------------------------------------


def test_apply_is_pure_and_leaves_the_caller_dict_untouched():
    """/status must stay side-effect-free, so the signals dict is never mutated."""
    signals = _signals(faults=_NEEDLE_CRASH)
    before = {
        "faults": list(signals["lc_faults"]),
        "flags": list(signals["module_multisampler_stat_flags"]),
        "state": signals["module_multisampler_state"],
    }

    apply_fault_acks(signals, _acked("multisampler", signals=signals).snapshot())

    assert signals["lc_faults"] == before["faults"]
    assert signals["module_multisampler_stat_flags"] == before["flags"]
    assert signals["module_multisampler_state"] == before["state"]


def test_apply_with_no_acks_returns_the_same_object():
    signals = _signals()
    assert apply_fault_acks(signals, None) is signals
    assert apply_fault_acks(signals, {}) is signals


def test_evidence_at_ack_takes_the_newest_fault_for_that_module():
    signals = _signals(
        faults=[
            _fault("multisampler", "G7167B", "Pusher hit the vessel top"),
            _fault(
                "multisampler",
                "G7167B",
                "Needle command failed",
                at=_FAULT_AT + timedelta(seconds=30),
            ),
            # A different module's newer fault must not raise the threshold.
            _fault(
                "binary_pump",
                "G7120A",
                "Analysis aborted by another module",
                severity="error",
                at=_FAULT_AT + timedelta(minutes=10),
            ),
        ]
    )
    evidence = evidence_at_ack(signals, "multisampler")

    assert evidence["faults_through"] == (_FAULT_AT + timedelta(seconds=30)).isoformat()
    assert evidence["stat_through"] == _STAT_AT.isoformat()


def test_acks_survive_a_restart(tmp_path):
    """A restart must not resurrect a fault the operator already cleared."""
    path = tmp_path / "hplcms_fault_acks.json"
    signals = _signals(faults=_NEEDLE_CRASH)
    FaultAcks(path).record("multisampler", signals, owner="tech", note="needle checked")

    reloaded = FaultAcks(path)
    assert reloaded.get("multisampler") is not None
    assert reloaded.get("multisampler")["note"] == "needle checked"
    assert errored_lc_modules(signals, reloaded) == []


def test_unknown_roles_on_disk_are_ignored(tmp_path):
    path = tmp_path / "acks.json"
    path.write_text('{"laser_cannon": {"acked_at": "2026-08-20T00:00:00+00:00"}}', "utf-8")

    assert FaultAcks(path).snapshot() == {}


def test_a_corrupt_ack_file_does_not_take_the_service_down(tmp_path):
    path = tmp_path / "acks.json"
    path.write_text("{not json", "utf-8")

    assert FaultAcks(path).snapshot() == {}
