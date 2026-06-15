"""Tests for the /control/* endpoints."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from subprocess import Popen
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from agilent_hplcms_server.api import create_app
from agilent_hplcms_server.config import Settings
from agilent_hplcms_server.control.runner import ActiveRun, MosesRunner


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fake runner helpers
# ---------------------------------------------------------------------------

def _fake_active_run(run_id: str = "test-run-1") -> ActiveRun:
    mock_proc = MagicMock(spec=Popen)
    mock_proc.pid = 12345
    mock_proc.poll.return_value = None  # still running
    return ActiveRun(
        run_id=run_id,
        pid=12345,
        process=mock_proc,
        started_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        job_path=Path("fake_job.json"),
        script_name="examples/agent_agilent.py",
    )


class FakeRunner(MosesRunner):
    """MosesRunner subclass that never touches the filesystem or spawns processes."""

    def __init__(self, *, busy: bool = False, run_id: str = "test-run-1") -> None:
        super().__init__()
        if busy:
            self._active = _fake_active_run(run_id)
        self._submitted: list[dict] = []
        self._aborted = False

    def submit(self, script_name: str, job: dict, settings=None) -> ActiveRun:  # type: ignore[override]
        allowed = ["examples/agent_agilent.py"]
        if script_name not in allowed:
            raise ValueError(f"Script '{script_name}' is not in MOSES_ALLOWED_SCRIPTS.")
        active = _fake_active_run()
        self._active = active
        self._submitted.append({"script_name": script_name, "job": job})
        return active

    def abort(self, timeout_s: int = 10) -> bool:  # type: ignore[override]
        if self._active is None:
            return False
        self._active = None
        self._aborted = True
        return True

    def poll(self) -> None:
        pass  # fake runner never auto-clears


# ---------------------------------------------------------------------------
# Client factories
# ---------------------------------------------------------------------------

def _settings() -> Settings:
    return Settings()


def _client(signals: dict, runner: MosesRunner | None = None) -> TestClient:
    def fake_reader(_: Settings) -> dict:
        return dict(signals)

    app = create_app(settings=_settings(), reader=fake_reader, runner=runner)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Valid job fixture
# ---------------------------------------------------------------------------

VALID_RUN_BODY = {
    "output_dir": "C:/CDSProjects/Installation/Results/TestBatch",
    "ms_mode": "positive_negative",
    "standby_after": True,
    "gradient": {
        "name": "standard_10min",
        "solvent_a": "H2O_0.1%FA",
        "solvent_b": "ACN_0.1%FA",
        "run_time": 10.0,
        "flow_rate": 0.6,
        "equilibration_time": 1.0,
        "gradient_table": [[0.0, 0.05], [1.0, 0.05], [7.0, 1.0], [9.8, 1.0], [9.9, 0.05]],
    },
    "samples": [
        {"sample_name": "cpd_01", "sample_position": "D4B-A1", "injection_volume": 2.0}
    ],
}


# ---------------------------------------------------------------------------
# /control/startup
# ---------------------------------------------------------------------------

def test_startup_ready():
    client = _client(_load("signals_ready.json"))
    r = client.post("/control/startup")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_startup_requires_init():
    client = _client(_load("signals_requires_init.json"))
    r = client.post("/control/startup")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "requires_init"
    assert "AcquisitionServer" in body["missing_processes"]
    assert "AcqInstrumentService" in body["missing_processes"]


# ---------------------------------------------------------------------------
# /control/run
# ---------------------------------------------------------------------------

def test_run_accepted():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["run_id"]
    assert body["pid"] == 12345


def test_run_409_when_runner_busy():
    runner = FakeRunner(busy=True, run_id="existing-run")
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "equipment_busy"
    assert detail["run_id"] == "existing-run"


def test_run_409_when_probe_busy():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_busy.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "equipment_busy"


def test_run_409_when_requires_init():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_requires_init.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "requires_init"
    assert "start_openlab" in detail["required_actions"]


def test_run_422_injection_volume_too_large():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    bad = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "s1", "sample_position": "D4B-A1", "injection_volume": 999.0}
    ]}
    r = client.post("/control/run", json=bad)
    assert r.status_code == 422


def test_run_422_run_time_too_long():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    bad_gradient = {**VALID_RUN_BODY["gradient"], "run_time": 999.0}
    bad = {**VALID_RUN_BODY, "gradient": bad_gradient}
    r = client.post("/control/run", json=bad)
    assert r.status_code == 422


def test_run_422_empty_samples():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    bad = {**VALID_RUN_BODY, "samples": []}
    r = client.post("/control/run", json=bad)
    assert r.status_code == 422


def test_run_422_invalid_script_name():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    bad = {**VALID_RUN_BODY, "script_name": "../../evil.py"}
    r = client.post("/control/run", json=bad)
    assert r.status_code == 422


def test_run_422_sample_name_with_spaces():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    bad = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "has spaces", "sample_position": "D4B-A1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=bad)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /control/abort
# ---------------------------------------------------------------------------

def test_abort_not_running():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/abort")
    assert r.status_code == 200
    assert r.json()["status"] == "not_running"


def test_abort_active_run():
    runner = FakeRunner(busy=True, run_id="run-to-abort")
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/abort")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "aborted"
    assert body["run_id"] == "run-to-abort"
    assert runner._aborted


# ---------------------------------------------------------------------------
# /control/shutdown
# ---------------------------------------------------------------------------

def test_shutdown_accepted_when_idle():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/shutdown")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["run_id"]


def test_shutdown_409_when_busy():
    runner = FakeRunner(busy=True, run_id="active-run")
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/shutdown")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "equipment_busy"


# ---------------------------------------------------------------------------
# /status reflects runner busy state
# ---------------------------------------------------------------------------

def test_status_busy_when_runner_active():
    """GET /status must return busy even when probe signals are 'ready'."""
    runner = FakeRunner(busy=True)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["equipment_status"] == "busy"


def test_status_ready_when_runner_idle():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["equipment_status"] == "ready"
