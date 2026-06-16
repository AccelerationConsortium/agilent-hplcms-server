"""Tests for the /control/* endpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from subprocess import Popen
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from agilent_hplcms_server.api import create_app
from agilent_hplcms_server.config import Settings
from agilent_hplcms_server.control.runner import JobEntry, MosesRunner

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fake runner helpers
# ---------------------------------------------------------------------------

def _make_mock_proc() -> MagicMock:
    mock_proc = MagicMock(spec=Popen)
    mock_proc.pid = 12345
    mock_proc.poll.return_value = None
    return mock_proc


def _fake_job_entry(
    run_id: str = "test-run-1",
    status: str = "dispatched",
    request_dict: dict | None = None,
) -> JobEntry:
    active = status in ("dispatched", "acquiring")
    return JobEntry(
        queue_id=run_id,
        script_name="examples/agent_agilent.py",
        job={},
        request_dict=request_dict or {"script_name": "examples/agent_agilent.py"},
        queued_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        status=status,  # type: ignore[arg-type]
        started_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc) if active else None,
        pid=12345 if active else None,
        process=_make_mock_proc() if active else None,
        job_path=Path("fake_job.json"),
    )


class FakeRunner(MosesRunner):
    """MosesRunner that never touches the filesystem or spawns processes."""

    def __init__(
        self,
        *,
        busy: bool = False,
        run_id: str = "test-run-1",
        force_acquiring: bool = False,
    ) -> None:
        super().__init__()
        if busy:
            entry = _fake_job_entry(run_id, status="dispatched")
            self._jobs[run_id] = entry
            self._active_id = run_id
        self.submitted: list[dict] = []
        self.aborted = False
        self._next_run_id = "queued-run-1"
        self._force_acquiring = force_acquiring

    def check_acquiring(self, settings=None) -> bool:  # type: ignore[override]
        return self._force_acquiring

    def submit_to_queue(  # type: ignore[override]
        self,
        script_name: str,
        job: dict,
        request_dict: dict,
        settings=None,
    ) -> tuple[str, int]:
        allowed = ["examples/agent_agilent.py"]
        if script_name not in allowed:
            raise ValueError(f"Script '{script_name}' is not in MOSES_ALLOWED_SCRIPTS.")
        settings_obj = settings or Settings()
        if len(self._pending_ids) >= settings_obj.queue_max_depth and self._active_id is not None:
            raise OverflowError(f"Queue is full ({settings_obj.queue_max_depth} pending runs).")

        run_id = self._next_run_id
        self.submitted.append({"script_name": script_name, "job": job, "run_id": run_id})

        if self._active_id is None and not self._olss_occupied:
            entry = _fake_job_entry(run_id, status="dispatched", request_dict=request_dict)
            self._jobs[run_id] = entry
            self._active_id = run_id
            return run_id, 0
        else:
            entry = _fake_job_entry(run_id, status="pending", request_dict=request_dict)
            self._jobs[run_id] = entry
            self._pending_ids.append(run_id)
            return run_id, len(self._pending_ids)

    def enqueue(self, script_name: str, job: dict, settings=None) -> tuple[str, int]:  # type: ignore[override]
        return self.submit_to_queue(
            script_name=script_name,
            job=job,
            request_dict={"script_name": script_name, **job},
            settings=settings,
        )

    def abort(self, settings=None) -> tuple[bool, int]:  # type: ignore[override]
        n_cleared = len(self._pending_ids)
        for qid in list(self._pending_ids):
            e = self._jobs.get(qid)
            if e:
                e.status = "failed"
        self._pending_ids.clear()
        if self._active_id is None:
            return False, n_cleared
        e = self._jobs.get(self._active_id)
        if e:
            e.status = "failed"
        self._active_id = None
        self.aborted = True
        return True, n_cleared

    def poll(self, settings=None) -> None:  # type: ignore[override]
        pass

    def start_poller(self, settings=None) -> None:  # type: ignore[override]
        pass


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
# /control/run — accepted immediately when idle
# ---------------------------------------------------------------------------

def test_run_accepted_when_idle():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["run_id"]
    assert body["pid"] == 12345
    assert body["queue_position"] is None


def test_run_queued_when_busy():
    runner = FakeRunner(busy=True, run_id="existing-run")
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["queue_position"] == 1
    assert body["pid"] is None


def test_run_queued_when_olss_reports_external_acquisition():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_olss_run.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["queue_position"] == 1
    assert body["pid"] is None
    assert runner.get_active() is None
    assert runner.queue_depth() == 1


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
# POST /control/queue
# ---------------------------------------------------------------------------

def test_post_queue_accepted_when_idle():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/queue", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queue_id"]
    assert body["status"] == "queued"
    assert body["position"] == 0


def test_post_queue_queued_when_busy():
    runner = FakeRunner(busy=True, run_id="active-123")
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/queue", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["position"] == 1


def test_post_queue_queued_when_olss_reports_external_acquisition():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_olss_run.json"), runner=runner)
    r = client.post("/control/queue", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["position"] == 1
    assert runner.get_active() is None


def test_post_queue_409_requires_init():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_requires_init.json"), runner=runner)
    r = client.post("/control/queue", json=VALID_RUN_BODY)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# GET /control/queue
# ---------------------------------------------------------------------------

def test_queue_empty_when_idle():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.get("/control/queue")
    assert r.status_code == 200
    body = r.json()
    assert body["pending_count"] == 0
    assert body["active_run_id"] is None
    assert body["queue"] == []
    assert body["instrument_online"] is True


def test_queue_shows_active_run():
    runner = FakeRunner(busy=True, run_id="active-123")
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.get("/control/queue")
    assert r.status_code == 200
    body = r.json()
    assert body["active_run_id"] == "active-123"
    assert body["pending_count"] == 0
    assert body["instrument_online"] is True
    assert body["accepting_jobs"] is True


def test_queue_shows_pending_after_submit():
    runner = FakeRunner(busy=True, run_id="active-123")
    client = _client(_load("signals_ready.json"), runner=runner)
    client.post("/control/run", json=VALID_RUN_BODY)
    r = client.get("/control/queue")
    body = r.json()
    assert body["pending_count"] == 1
    # pid is None = genuinely queued (not yet dispatched); distinguishes from dispatched-but-not-acquiring
    not_started = [j for j in body["queue"] if j["pid"] is None]
    assert len(not_started) == 1
    assert len(not_started[0]["request"]["samples"]) == 1


def test_queue_dispatched_shows_as_pending_before_acquisition():
    """Dispatched job (script started, OpenLab not yet acquiring) shows as pending."""
    runner = FakeRunner(busy=True, run_id="active-123")
    # signals_ready has acquisition_active=False
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.get("/control/queue")
    body = r.json()
    active_jobs = [j for j in body["queue"] if j["queue_id"] == "active-123"]
    assert len(active_jobs) == 1
    assert active_jobs[0]["status"] == "pending"


def test_queue_dispatched_becomes_acquiring_when_output_dir_active():
    """Dispatched job transitions to acquiring when the job's own output_dir has sirslt activity."""
    runner = FakeRunner(busy=True, run_id="active-123", force_acquiring=True)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.get("/control/queue")
    body = r.json()
    active_jobs = [j for j in body["queue"] if j["queue_id"] == "active-123"]
    assert len(active_jobs) == 1
    assert active_jobs[0]["status"] == "acquiring"


def test_queue_instrument_offline():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_requires_init.json"), runner=runner)
    r = client.get("/control/queue")
    body = r.json()
    assert body["instrument_online"] is False
    assert body["accepting_jobs"] is False


# ---------------------------------------------------------------------------
# DELETE /control/queue/{id}
# ---------------------------------------------------------------------------

def test_delete_queue_cancels_pending():
    runner = FakeRunner(busy=True, run_id="active-123")
    client = _client(_load("signals_ready.json"), runner=runner)
    # Queue a pending job
    r = client.post("/control/queue", json=VALID_RUN_BODY)
    queue_id = r.json()["queue_id"]

    r2 = client.delete(f"/control/queue/{queue_id}")
    assert r2.status_code == 200
    assert r2.json()["cancelled_id"] == queue_id

    # Verify it no longer shows as pending
    body = client.get("/control/queue").json()
    assert body["pending_count"] == 0


def test_delete_queue_409_running():
    runner = FakeRunner(busy=True, run_id="active-123")
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.delete("/control/queue/active-123")
    assert r.status_code == 409


def test_delete_queue_404_not_found():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.delete("/control/queue/nonexistent-id")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /control/abort
# ---------------------------------------------------------------------------

def test_abort_not_running():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/abort")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "not_running"
    assert body["queue_cleared"] == 0


def test_abort_active_run():
    runner = FakeRunner(busy=True, run_id="run-to-abort")
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/abort")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "aborted"
    assert body["run_id"] == "run-to-abort"
    assert runner.aborted


def test_abort_clears_queue():
    runner = FakeRunner(busy=True, run_id="active-run")
    client = _client(_load("signals_ready.json"), runner=runner)
    # Queue two more runs
    client.post("/control/run", json=VALID_RUN_BODY)
    client.post("/control/run", json=VALID_RUN_BODY)
    r = client.post("/control/abort")
    body = r.json()
    assert body["queue_cleared"] == 2
    assert runner.queue_depth() == 0


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


def test_shutdown_queued_when_busy():
    runner = FakeRunner(busy=True, run_id="active-run")
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/shutdown")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["queue_position"] == 1


# ---------------------------------------------------------------------------
# /status reflects runner busy state and queue_length
# ---------------------------------------------------------------------------

def test_status_busy_when_runner_active():
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


def test_status_busy_when_olss_reports_external_acquisition():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_olss_run.json"), runner=runner)
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_status"] == "busy"
    assert body["message"] == "OpenLab acquisition active (instrument state: Run)"
    assert body["details"]["olss_current_run"] == "Direct OpenLab sequence"


def test_status_paused_when_olss_reports_paused_sequence():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_olss_paused.json"), runner=runner)
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_status"] == "paused"
    assert body["required_actions"] == ["resume_paused_sequence"]
    assert body["components"]["hplc"]["state"] == "paused"


def test_status_details_queue_length():
    runner = FakeRunner(busy=True, run_id="active-run")
    client = _client(_load("signals_ready.json"), runner=runner)
    # Queue one more
    client.post("/control/run", json=VALID_RUN_BODY)
    r = client.get("/status")
    body = r.json()
    assert body["details"]["queue_length"] == 1


# ---------------------------------------------------------------------------
# MosesRunner OLSS lifecycle handling
# ---------------------------------------------------------------------------

def test_runner_holds_active_job_while_olss_occupied_after_moses_exit():
    runner = MosesRunner()
    entry = _fake_job_entry("active-123", status="dispatched")
    entry.process.poll.return_value = 0  # type: ignore[union-attr]
    runner._jobs[entry.queue_id] = entry
    runner._active_id = entry.queue_id

    runner.notify_olss_state("Idle", "Paused")
    runner.poll(settings=Settings())

    assert runner.get_active() is not None
    assert entry.status == "acquiring"
    assert entry.process is None

    runner.notify_olss_state("Run", "OK")
    runner.poll(settings=Settings())

    assert runner.get_active() is not None
    assert entry.status == "acquiring"

    runner.notify_olss_state("Idle", "OK")
    runner.poll(settings=Settings())

    assert runner.get_active() is None
    assert entry.status == "done"
