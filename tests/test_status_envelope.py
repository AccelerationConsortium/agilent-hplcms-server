"""Tests for the STATUS_SPEC v1.1 endpoints exposed by the sidecar."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agilent_hplcms_server.api import create_app
from agilent_hplcms_server.config import Settings
from agilent_hplcms_server.models import PROTOCOL_VERSION


FIXTURES = Path(__file__).parent / "fixtures"


def _settings() -> Settings:
    return Settings()


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client_with_signals(signals: dict) -> tuple[TestClient, list[int]]:
    """Return a TestClient whose ``/status`` returns the given signals dict.

    Also returns a single-element list containing the call count so a test can
    assert side-effect-free polling.
    """
    call_count = [0]

    def fake_reader(_settings: Settings) -> dict:
        call_count[0] += 1
        return dict(signals)

    app = create_app(settings=_settings(), reader=fake_reader)
    return TestClient(app), call_count


def test_root_probe_response():
    client, _ = _client_with_signals(_load("signals_ready.json"))
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["equipment_id"] == "agilent_uplc_ms"
    assert body["equipment_name"] == "Agilent UPLC-MS"


def test_health_returns_healthy():
    client, _ = _client_with_signals(_load("signals_ready.json"))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy"}


def test_openapi_present():
    client, _ = _client_with_signals(_load("signals_ready.json"))
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert "/" in spec["paths"]
    assert "/health" in spec["paths"]
    assert "/status" in spec["paths"]


@pytest.mark.parametrize(
    "fixture_name,expected_status",
    [
        ("signals_ready.json", "ready"),
        ("signals_busy.json", "busy"),
        ("signals_olss_run.json", "busy"),
        ("signals_olss_paused.json", "busy"),
        ("signals_requires_init.json", "requires_init"),
        ("signals_error.json", "error"),
        ("signals_unknown.json", "unknown"),
    ],
)
def test_status_state_mapping(fixture_name: str, expected_status: str):
    client, _ = _client_with_signals(_load(fixture_name))
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["equipment_id"] == "agilent_uplc_ms"
    assert body["equipment_kind"] == "hplc"
    assert body["equipment_status"] == expected_status
    assert "device_time" in body
    assert "components" in body
    assert "openlab_acquisition" in body["components"]
    assert "openlab_instrument_service" in body["components"]
    assert "moses_controller" in body["components"]
    assert "hplc" in body["components"]
    assert "ms" in body["components"]


def test_requires_init_lists_missing_processes():
    client, _ = _client_with_signals(_load("signals_requires_init.json"))
    r = client.get("/status")
    body = r.json()
    assert body["equipment_status"] == "requires_init"
    assert body["required_actions"] == ["start_openlab"]
    assert "AcquisitionServer" in body["message"]


def test_error_passes_through_last_error():
    client, _ = _client_with_signals(_load("signals_error.json"))
    r = client.get("/status")
    body = r.json()
    assert body["equipment_status"] == "error"
    assert body["last_error"] is not None
    assert body["last_error"]["severity"] == "error"
    assert body["details"]["last_error_log_path"].endswith("InstrumentService.log")


def test_olss_run_marks_status_busy_without_filesystem_acquisition():
    client, _ = _client_with_signals(_load("signals_olss_run.json"))
    body = client.get("/status").json()

    assert body["equipment_status"] == "busy"
    assert body["message"] == "OpenLab acquisition active (instrument state: Running)"
    assert body["details"]["olss_current_run"] == "Direct OpenLab sequence"
    assert body["components"]["hplc"]["state"] == "busy"
    assert body["components"]["ms"]["state"] == "busy"


def test_olss_paused_maps_to_busy_with_required_action():
    """v1.1: a paused OpenLab sequence is reported as busy (paused is not a
    legal EquipmentState) but still surfaces the resume action; the precise
    OLSS status survives in details + the component state."""
    client, _ = _client_with_signals(_load("signals_olss_paused.json"))
    body = client.get("/status").json()

    assert body["equipment_status"] == "busy"
    assert body["required_actions"] == ["resume_paused_sequence"]
    assert body["details"]["olss_software_status"] == "Paused"
    assert body["components"]["hplc"]["state"] == "paused"
    assert body["components"]["ms"]["state"] == "paused"


def test_status_is_side_effect_free():
    """Calling /status N times must call the probe N times and never mutate state."""
    client, calls = _client_with_signals(_load("signals_ready.json"))
    for _ in range(3):
        r = client.get("/status")
        assert r.status_code == 200
    assert calls[0] == 3


def test_status_always_200_when_signals_have_probe_error():
    """A probe environmental failure becomes ``unknown`` in-band, not HTTP 5xx."""
    signals = _load("signals_unknown.json")
    client, _ = _client_with_signals(signals)
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["equipment_status"] == "unknown"


def test_details_carries_instrument_label_and_paths():
    client, _ = _client_with_signals(_load("signals_ready.json"))
    body = client.get("/status").json()
    assert body["details"]["instrument_label"] == "SDL2_LC1290"
    assert body["details"]["openlab_log_dir"]
    assert body["details"]["cds_results_dir"]
    assert body["details"]["probe_version"]


def test_metrics_are_wrapped_with_units_and_omit_unknowns():
    signals = _load("signals_ready.json")
    signals.update(
        {
            "olss_instrument_state": "Idle",
            "vacuum_level_mbar": 5.2e-6,
            "source_temperature_c": 120.0,
            "turbopump_ready": True,
        }
    )
    client, _ = _client_with_signals(signals)

    metrics = client.get("/status").json()["metrics"]

    assert metrics["vacuum_level_mbar"] == {"value": 5.2e-6, "unit": "mbar", "timestamp": None}
    assert metrics["source_temperature_c"]["unit"] == "\u00b0C"
    assert metrics["turbopump_ready"] == {"value": True, "unit": None, "timestamp": None}
    assert metrics["ms_communication_ok"]["value"] is True
    assert "solvent_a_volume_ml" not in metrics


def test_communication_metrics_are_unknown_without_olss_state():
    client, _ = _client_with_signals(_load("signals_ready.json"))

    metrics = client.get("/status").json()["metrics"]

    assert "ms_communication_ok" not in metrics
    assert "pump_communication_ok" not in metrics
    assert "autosampler_communication_ok" not in metrics
