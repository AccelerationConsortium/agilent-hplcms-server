"""Tests for the /control/* endpoints.

Queue-ownership model (2026-06-23): the sidecar's MosesRunner is the sole queue
and Moses runs synchronously, so **process exit is authoritative** (rc==0 →
done, rc!=0 → failed). There is no OpenLab-queue "enqueued"/"acquiring" state and
no .sirslt finalization. OLSS is observed only to detect technician *servicing*.
Submission precedence (highest first): servicing 409 > workflow 423 > queue >
idle. Claims carry a roster-resolved role (hplcms | hte); workflow.start is
hte-only.
"""

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
from agilent_hplcms_server.control.roster import resolve_role
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
    status: str = "running",
    request_dict: dict | None = None,
) -> JobEntry:
    active = status == "running"
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
    """MosesRunner that never touches the filesystem or spawns processes.

    ``servicing`` / ``queue_full`` are forced via overrides so the router's
    precedence gates can be driven deterministically without real OLSS polling.
    """

    def __init__(
        self,
        *,
        busy: bool = False,
        run_id: str = "test-run-1",
        queue_full: bool = False,
        servicing: bool = False,
    ) -> None:
        super().__init__()
        if busy or queue_full:
            entry = _fake_job_entry(run_id, status="running")
            self._jobs[run_id] = entry
            self._active_id = run_id
        self.submitted: list[dict] = []
        self.aborted = False
        self._next_run_id = "queued-run-1"
        self._queue_full = queue_full
        self._servicing = servicing

    def is_queue_full(self, settings=None) -> bool:  # type: ignore[override]
        return self._queue_full

    def is_servicing(self, settings=None) -> bool:  # type: ignore[override]
        # Honour both the forced flag and the real persistent service-mode flag
        # (set via /control/service/start through the inherited set_service_mode).
        return self._servicing or self._service_mode

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
        if self._queue_full:
            raise OverflowError(f"Queue is full ({settings_obj.queue_max_depth} pending runs).")
        if len(self._pending_ids) >= settings_obj.queue_max_depth and self._active_id is not None:
            raise OverflowError(f"Queue is full ({settings_obj.queue_max_depth} pending runs).")

        run_id = self._next_run_id
        self.submitted.append({"script_name": script_name, "job": job, "run_id": run_id})

        # Launch immediately only when idle AND not being serviced.
        if self._active_id is None and not self._servicing:
            entry = _fake_job_entry(run_id, status="running", request_dict=request_dict)
            self._jobs[run_id] = entry
            self._active_id = run_id
            return run_id, 0
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

def _settings(**overrides) -> Settings:
    """Test settings whose default roster makes any owner an ``hte`` user (via
    the ``"*"`` wildcard), so the bulk of the suite can claim with arbitrary
    owner strings and still submit + run workflows. Role/service-enforcement
    tests pass explicit group lists."""
    base = dict(hplcms_users="", hte_users="*", hplcms_admins="")
    base.update(overrides)
    return Settings(**base)


def _client(
    signals: dict,
    runner: MosesRunner | None = None,
    settings: Settings | None = None,
) -> TestClient:
    def fake_reader(_: Settings) -> dict:
        return dict(signals)

    app = create_app(settings=settings or _settings(), reader=fake_reader, runner=runner)
    return TestClient(app)


def _authed_client(
    signals: dict,
    runner: MosesRunner | None = None,
    *,
    owner: str = "test-operator",
    session_id: str = "test-session",
    settings: Settings | None = None,
) -> TestClient:
    """A client that holds a valid claim, with ``X-Claim-Token`` pre-set as a
    default header on every request — mirrors how the aggregator drives a
    hard-enforcement (v1.1) device. Use for tests that hit mutating
    ``/control/*`` endpoints; read-only tests can use :func:`_client`.
    """
    client = _client(signals, runner=runner, settings=settings)
    r = client.post(
        "/control/claim",
        json={"owner": owner, "session_id": session_id, "ttl_s": 30.0},
    )
    assert r.status_code == 200, r.text
    client.headers["X-Claim-Token"] = r.json()["claim_token"]
    return client


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
        # Rear tray is the manual/open tray; front is reserved for the robot.
        {"sample_name": "cpd_01", "tray": "rear", "well": "A1", "injection_volume": 2.0}
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
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["run_id"]
    assert body["pid"] == 12345
    assert body["queue_position"] is None


def test_run_queued_when_busy():
    runner = FakeRunner(busy=True, run_id="existing-run")
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["queue_position"] == 1
    assert body["pid"] is None


def test_run_409_when_requires_init():
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_requires_init.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "requires_init"
    assert "start_openlab" in detail["required_actions"]


def test_run_422_injection_volume_too_large():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    bad = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "s1", "tray": "rear", "well": "A1", "injection_volume": 999.0}
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
    # script_name is a free-form field (no Pydantic pattern); the allowlist
    # rejection is a runtime 422 raised inside enqueue, so a valid claim is
    # needed to get past hard enforcement and reach it.
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    bad = {**VALID_RUN_BODY, "script_name": "../../evil.py"}
    r = client.post("/control/run", json=bad)
    assert r.status_code == 422


def test_run_422_sample_name_with_spaces():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    bad = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "has spaces", "tray": "rear", "well": "A1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=bad)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /control/queue
# ---------------------------------------------------------------------------

def test_post_queue_accepted_when_idle():
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/queue", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queue_id"]
    assert body["status"] == "queued"
    assert body["position"] == 0


def test_post_queue_queued_when_busy():
    runner = FakeRunner(busy=True, run_id="active-123")
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/queue", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["position"] == 1


def test_post_queue_409_requires_init():
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_requires_init.json"), runner=runner)
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
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    client.post("/control/run", json=VALID_RUN_BODY)
    r = client.get("/control/queue")
    body = r.json()
    assert body["pending_count"] == 1
    # pid is None = genuinely queued (not yet launched).
    not_started = [j for j in body["queue"] if j["pid"] is None]
    assert len(not_started) == 1
    assert len(not_started[0]["request"]["samples"]) == 1


def test_queue_shows_active_job_as_running():
    """The active job (Moses subprocess alive) shows status 'running'."""
    runner = FakeRunner(busy=True, run_id="active-123")
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.get("/control/queue")
    body = r.json()
    active_jobs = [j for j in body["queue"] if j["queue_id"] == "active-123"]
    assert len(active_jobs) == 1
    assert active_jobs[0]["status"] == "running"


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
    client = _authed_client(_load("signals_ready.json"), runner=runner)
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
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.delete("/control/queue/active-123")
    assert r.status_code == 409


def test_delete_queue_404_not_found():
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.delete("/control/queue/nonexistent-id")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /control/abort
# ---------------------------------------------------------------------------

def test_abort_not_running():
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/abort")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "not_running"
    assert body["queue_cleared"] == 0


def test_abort_active_run():
    runner = FakeRunner(busy=True, run_id="run-to-abort")
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/abort")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "aborted"
    assert body["run_id"] == "run-to-abort"
    assert runner.aborted


def test_abort_clears_queue():
    runner = FakeRunner(busy=True, run_id="active-run")
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    # Queue two more runs
    client.post("/control/run", json=VALID_RUN_BODY)
    client.post("/control/run", json=VALID_RUN_BODY)
    r = client.post("/control/abort")
    body = r.json()
    assert body["queue_cleared"] == 2
    assert runner.queue_depth() == 0


# ---------------------------------------------------------------------------
# /control/standby
# ---------------------------------------------------------------------------

def test_standby_accepted_when_idle():
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/standby")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["run_id"]


def test_standby_queued_when_busy():
    runner = FakeRunner(busy=True, run_id="active-run")
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/standby")
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
    assert body["message"] == "OpenLab acquisition active (instrument state: Running)"
    assert body["details"]["olss_current_run"] == "Direct OpenLab sequence"


def test_status_paused_sequence_maps_to_busy():
    """v1.1: OLSS 'Paused' is reported as equipment_status 'busy' (paused is not
    a legal EquipmentState). The precise OLSS status is preserved in details and
    the hplc component state."""
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_olss_paused.json"), runner=runner)
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_status"] == "busy"
    assert body["required_actions"] == ["resume_paused_sequence"]
    assert "paused" in body["message"].lower()
    assert body["components"]["hplc"]["state"] == "paused"
    assert body["details"]["olss_software_status"] == "Paused"


def test_status_details_queue_length():
    runner = FakeRunner(busy=True, run_id="active-run")
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    # Queue one more
    client.post("/control/run", json=VALID_RUN_BODY)
    r = client.get("/status")
    body = r.json()
    assert body["details"]["queue_length"] == 1


# ---------------------------------------------------------------------------
# MosesRunner lifecycle — process exit is authoritative (no OLSS finalization)
# ---------------------------------------------------------------------------

def test_poll_marks_done_on_clean_exit():
    runner = MosesRunner()
    entry = _fake_job_entry("active-123", status="running")
    entry.process.poll.return_value = 0  # type: ignore[union-attr]
    runner._jobs[entry.queue_id] = entry
    runner._active_id = entry.queue_id

    runner.poll(settings=Settings())

    assert runner.get_active() is None
    assert entry.status == "done"
    assert entry.process is None
    assert entry.finished_at is not None


def test_poll_marks_failed_on_nonzero_exit():
    runner = MosesRunner()
    entry = _fake_job_entry("active-123", status="running")
    entry.process.poll.return_value = 3  # type: ignore[union-attr]
    runner._jobs[entry.queue_id] = entry
    runner._active_id = entry.queue_id

    runner.poll(settings=Settings())

    assert runner.get_active() is None
    assert entry.status == "failed"
    assert "Exit code 3" in (entry.error_msg or "")


def test_poll_leaves_running_while_process_alive():
    runner = MosesRunner()
    entry = _fake_job_entry("active-123", status="running")
    entry.process.poll.return_value = None  # type: ignore[union-attr]
    runner._jobs[entry.queue_id] = entry
    runner._active_id = entry.queue_id

    runner.poll(settings=Settings())

    assert runner.get_active() is not None
    assert entry.status == "running"


class _RecordingRunner(MosesRunner):
    """Records _launch_locked calls instead of spawning a real subprocess."""

    def __init__(self) -> None:
        super().__init__()
        self.launched: list[str] = []

    def _launch_locked(self, entry, settings) -> None:  # type: ignore[override]
        entry.status = "running"
        entry.started_at = datetime.now(timezone.utc)
        entry.pid = 999
        self._active_id = entry.queue_id
        self.launched.append(entry.queue_id)


def test_poll_launches_next_pending_after_done():
    runner = _RecordingRunner()
    active = _fake_job_entry("a", status="running")
    active.process.poll.return_value = 0  # type: ignore[union-attr]
    runner._jobs["a"] = active
    runner._active_id = "a"
    pending = _fake_job_entry("b", status="pending")
    runner._jobs["b"] = pending
    runner._pending_ids.append("b")

    runner.poll(settings=Settings())

    assert active.status == "done"
    assert runner._active_id == "b"
    assert "b" in runner.launched


def test_abort_active_run_real_runner():
    """MosesRunner.abort() marks the job failed inside the lock (no second lock race)."""
    runner = MosesRunner()
    entry = _fake_job_entry("active-123", status="running")
    runner._jobs[entry.queue_id] = entry
    runner._active_id = entry.queue_id

    was_active, n_cleared = runner.abort(settings=Settings())

    assert was_active is True
    assert n_cleared == 0
    assert entry.status == "failed"
    assert entry.error_msg == "Aborted by operator"
    assert runner.get_active() is None


def test_abort_active_run_shows_failed_in_queue():
    """After POST /control/abort the job appears as failed in GET /control/queue."""
    runner = FakeRunner(busy=True, run_id="run-to-abort")
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    client.post("/control/abort")
    body = client.get("/control/queue").json()
    jobs = {j["queue_id"]: j for j in body["queue"]}
    assert "run-to-abort" in jobs
    assert jobs["run-to-abort"]["status"] == "failed"
    assert body["active_run_id"] is None


# ---------------------------------------------------------------------------
# Servicing detection (precedence #1): OLSS busy AND no active job, debounced.
# ---------------------------------------------------------------------------

def test_is_servicing_requires_debounce():
    runner = MosesRunner()
    s = Settings(servicing_debounce_polls=2)
    # One observation of a real OLSS run while idle → below the debounce.
    runner.notify_olss_state("Busy", "OK", "Seq-Run-1")
    assert runner.is_servicing(s) is False
    # A second consecutive observation crosses it → servicing.
    runner.notify_olss_state("Busy", "OK", "Seq-Run-1")
    assert runner.is_servicing(s) is True
    # currentRun clearing (OLSS idle) resets the streak.
    runner.notify_olss_state("Idle", "OK", None)
    assert runner.is_servicing(s) is False


def test_no_servicing_during_data_analysis():
    """state=='Busy' with NO currentRun is data analysis/reprocessing, not an
    acquisition — it must NOT halt the queue (keyed on currentRun, not Busy)."""
    runner = MosesRunner()
    s = Settings(servicing_debounce_polls=1)
    runner.notify_olss_state("Busy", "OK", None)
    runner.notify_olss_state("Busy", "OK", None)
    assert runner.is_servicing(s) is False


def test_not_servicing_while_our_job_active():
    """A real OLSS run with an active sidecar job is OUR run, not a technician."""
    runner = MosesRunner()
    entry = _fake_job_entry("a", status="running")
    runner._jobs["a"] = entry
    runner._active_id = "a"
    runner.notify_olss_state("Busy", "OK", "Seq-Run-1")
    runner.notify_olss_state("Busy", "OK", "Seq-Run-1")
    assert runner.is_servicing(Settings()) is False


def test_service_mode_flag_forces_servicing():
    """The explicit persistent flag halts the queue regardless of OLSS state."""
    runner = MosesRunner()
    assert runner.is_servicing(Settings()) is False
    runner.set_service_mode(True)
    assert runner.service_mode() is True
    assert runner.is_servicing(Settings()) is True
    runner.set_service_mode(False)
    assert runner.service_mode() is False
    assert runner.is_servicing(Settings()) is False


def test_poll_halts_queue_during_servicing():
    """A pending job is NOT launched while a technician is servicing."""
    runner = _RecordingRunner()
    s = Settings(servicing_debounce_polls=1)
    runner.notify_olss_state("Busy", "OK", "Seq-Run-1")  # streak 1 ≥ 1 → servicing
    pending = _fake_job_entry("p1", status="pending")
    runner._jobs["p1"] = pending
    runner._pending_ids.append("p1")

    runner.poll(settings=s)

    assert runner.get_active() is None
    assert pending.status == "pending"
    assert runner.launched == []


def test_run_409_instrument_servicing():
    runner = FakeRunner(busy=False, servicing=True)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "instrument_servicing"
    assert r.headers.get("Retry-After") is None  # duration unpredictable
    assert runner.submitted == []  # never enqueued


def test_queue_not_accepting_during_servicing():
    runner = FakeRunner(busy=False, servicing=True)
    client = _client(_load("signals_ready.json"), runner=runner)
    body = client.get("/control/queue").json()
    assert body["accepting_jobs"] is False


# ---------------------------------------------------------------------------
# v1.1 claim protocol (STATUS_SPEC §5)
# ---------------------------------------------------------------------------

def _claim(client: TestClient, owner="agent:test", session_id="s-1", ttl_s=30.0):
    return client.post(
        "/control/claim",
        json={"owner": owner, "session_id": session_id, "ttl_s": ttl_s},
    )


def test_claim_grants_token_and_expiry():
    client = _client(_load("signals_ready.json"), runner=FakeRunner())
    r = _claim(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claim_token"]
    assert body["heartbeat_interval_s"] > 0
    assert body["heartbeat_interval_s"] < 30.0  # strictly more often than TTL
    assert body["expires_at"]


def test_claim_idempotent_for_same_session_rotates_token():
    client = _client(_load("signals_ready.json"), runner=FakeRunner())
    t1 = _claim(client, session_id="same").json()["claim_token"]
    r2 = _claim(client, session_id="same")
    assert r2.status_code == 200
    # Re-claiming the same session always succeeds (token may rotate).
    assert r2.json()["claim_token"]


def test_claim_conflict_when_held_by_other_session():
    client = _client(_load("signals_ready.json"), runner=FakeRunner())
    assert _claim(client, owner="agent:a", session_id="aaa").status_code == 200
    r = _claim(client, owner="agent:b", session_id="bbb")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["claimed_by"]["session_id"] == "aaa"
    assert detail["claimed_by"]["owner"] == "agent:a"


def test_heartbeat_extends_and_returns_204():
    client = _client(_load("signals_ready.json"), runner=FakeRunner())
    token = _claim(client).json()["claim_token"]
    r = client.post("/control/heartbeat", headers={"X-Claim-Token": token})
    assert r.status_code == 204


def test_heartbeat_401_on_unknown_token():
    client = _client(_load("signals_ready.json"), runner=FakeRunner())
    _claim(client)
    r = client.post("/control/heartbeat", headers={"X-Claim-Token": "not-the-token"})
    assert r.status_code == 401


def test_release_is_idempotent():
    client = _client(_load("signals_ready.json"), runner=FakeRunner())
    token = _claim(client).json()["claim_token"]
    r1 = client.post("/control/release", headers={"X-Claim-Token": token})
    assert r1.status_code == 204
    # Releasing again (now-unknown token) still 204 — never blocks the client.
    r2 = client.post("/control/release", headers={"X-Claim-Token": token})
    assert r2.status_code == 204
    # Releasing with no token at all is also a 204 no-op.
    r3 = client.post("/control/release")
    assert r3.status_code == 204


def test_release_frees_slot_for_next_session():
    client = _client(_load("signals_ready.json"), runner=FakeRunner())
    token = _claim(client, session_id="first").json()["claim_token"]
    # A different session is blocked while the first holds the claim...
    assert _claim(client, session_id="second").status_code == 409
    # ...but can claim once the first releases.
    assert client.post("/control/release", headers={"X-Claim-Token": token}).status_code == 204
    assert _claim(client, session_id="second").status_code == 200


# ---------------------------------------------------------------------------
# v1.1 hard claim enforcement — 423 Locked on /control/* without a valid token
# ---------------------------------------------------------------------------

def test_run_423_without_token():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 423
    assert r.json()["detail"]["claimed_by"] is None
    assert runner.get_active() is None  # action never executed


def test_run_423_with_stale_token():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post(
        "/control/run", json=VALID_RUN_BODY, headers={"X-Claim-Token": "bogus"}
    )
    assert r.status_code == 423
    assert runner.get_active() is None


def test_mutations_423_when_claim_held_by_other_session():
    runner = FakeRunner(busy=True, run_id="active-1")
    client = _client(_load("signals_ready.json"), runner=runner)
    # Someone else holds the claim.
    _claim(client, owner="agent:other", session_id="other")
    # We POST with no token → 423, body names the current holder.
    for call in (
        lambda: client.post("/control/abort"),
        lambda: client.post("/control/standby"),
        lambda: client.delete("/control/queue/active-1"),
    ):
        r = call()
        assert r.status_code == 423, r.text
        assert r.json()["detail"]["claimed_by"]["owner"] == "agent:other"


def test_read_endpoints_open_without_token():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    assert client.get("/control/queue").status_code == 200
    assert client.post("/control/startup").status_code == 200
    assert client.get("/status").status_code == 200


def test_status_surfaces_claimed_by():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    # Unclaimed → details.claimed_by is null (present, per spec example).
    body = client.get("/status").json()
    assert body["details"]["claimed_by"] is None
    # After a claim, /status surfaces the holder.
    _claim(client, owner="agent:screening", session_id="sess-9")
    body = client.get("/status").json()
    cb = body["details"]["claimed_by"]
    assert cb["owner"] == "agent:screening"
    assert cb["session_id"] == "sess-9"
    assert cb["expires_at"]
    assert cb["workflow"] is False


# ---------------------------------------------------------------------------
# Roster-driven roles (identity, NOT authentication)
# ---------------------------------------------------------------------------

def test_resolve_role_unit():
    s = Settings(hplcms_users="alice, bob", hte_users="HTE-User", hplcms_admins="Service-Account")
    assert resolve_role("alice", s) == "hplcms_user"
    assert resolve_role("BOB", s) == "hplcms_user"          # case-insensitive
    assert resolve_role("hte-user", s) == "hte"
    assert resolve_role("service-account", s) == "hplcms_admin"
    assert resolve_role("stranger", s) is None
    # All lists empty → built-in defaults apply (roster always enforced).
    d = Settings(hplcms_users="", hte_users="", hplcms_admins="")
    assert resolve_role("Hplcms-User", d) == "hplcms_user"
    assert resolve_role("HTE-User", d) == "hte"
    assert resolve_role("Service-Account", d) == "hplcms_admin"
    assert resolve_role("stranger", d) is None
    # Explicit "*" wildcard = open (any owner), distinct from accidental empty.
    w = Settings(hplcms_users="*", hte_users="", hplcms_admins="")
    assert resolve_role("whoever", w) == "hplcms_user"


def test_role_precedence_admin_over_hte_over_user():
    both = Settings(hplcms_users="carol", hte_users="carol", hplcms_admins="carol")
    assert resolve_role("carol", both) == "hplcms_admin"
    hte_and_user = Settings(hplcms_users="dave", hte_users="dave", hplcms_admins="")
    assert resolve_role("dave", hte_and_user) == "hte"


def test_claim_403_for_unknown_user_when_roster_enabled():
    settings = _settings(hplcms_users="Hplcms-User", hte_users="HTE-User")
    client = _client(_load("signals_ready.json"), runner=FakeRunner(), settings=settings)
    r = client.post(
        "/control/claim", json={"owner": "stranger", "session_id": "s", "ttl_s": 30.0}
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "user_not_recognized"
    assert detail["owner"] == "stranger"


def test_claim_returns_resolved_role():
    settings = _settings(
        hplcms_users="Hplcms-User", hte_users="HTE-User", hplcms_admins="Service-Account"
    )
    cases = {"HTE-User": "hte", "Hplcms-User": "hplcms_user", "Service-Account": "hplcms_admin"}
    for i, (owner, role) in enumerate(cases.items()):
        c = _client(_load("signals_ready.json"), runner=FakeRunner(), settings=settings)
        got = c.post(
            "/control/claim", json={"owner": owner, "session_id": f"s{i}", "ttl_s": 30.0}
        ).json()["role"]
        assert got == role, f"{owner} → {got}, expected {role}"


# ---------------------------------------------------------------------------
# Workflow lock (precedence #2): equipment-blocking series, HTE-only.
# ---------------------------------------------------------------------------

def test_workflow_start_end_happy_path():
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner, owner="HTE-User")
    r = client.post("/control/workflow/start")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "workflow_started"

    body = client.get("/status").json()
    assert body["details"]["workflow_active"] is True
    assert body["details"]["claimed_by"]["workflow"] is True
    assert "workflow.end" in body["allowed_actions"]
    assert "workflow.start" not in body["allowed_actions"]

    r2 = client.post("/control/workflow/end")
    assert r2.status_code == 200
    body2 = client.get("/status").json()
    assert body2["details"].get("workflow_active") in (None, False)
    assert "workflow.start" in body2["allowed_actions"]


def test_workflow_start_403_for_hplcms_role():
    runner = FakeRunner(busy=False)
    settings = _settings(hplcms_users="alice", hte_users="HTE-User")
    client = _authed_client(
        _load("signals_ready.json"), runner=runner, owner="alice", settings=settings
    )
    r = client.post("/control/workflow/start")
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "role_forbidden"
    assert detail["required_role"] == "hte"
    assert detail["role"] == "hplcms_user"


def test_workflow_start_423_without_token():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/workflow/start")
    assert r.status_code == 423


def test_workflow_active_blocks_non_holder_submit():
    """While an HTE workflow holds the lock, a tokenless submit is refused with
    the specific workflow_active reason + Retry-After (precedence #2)."""
    runner = FakeRunner(busy=False)
    holder = _authed_client(_load("signals_ready.json"), runner=runner, owner="HTE-User")
    assert holder.post("/control/workflow/start").status_code == 200

    # A second client on the SAME app shares the claim holder but has no token.
    intruder = TestClient(holder.app)
    r = intruder.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 423
    detail = r.json()["detail"]
    assert detail["error"] == "workflow_active"
    assert r.headers.get("Retry-After") is not None
    assert detail["claimed_by"]["owner"] == "HTE-User"


def test_workflow_holder_can_still_submit():
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner, owner="HTE-User")
    assert client.post("/control/workflow/start").status_code == 200
    # The holder keeps its token header → submits normally.
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text


def test_workflow_end_idempotent_without_active_workflow():
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner, owner="HTE-User")
    # Ending when none active still returns 200.
    assert client.post("/control/workflow/end").status_code == 200


# ---------------------------------------------------------------------------
# Service mode (precedence #1): admin-only persistent toggle + auto-detect.
# ---------------------------------------------------------------------------

def _admin_settings(**overrides) -> Settings:
    base = dict(hplcms_users="", hte_users="", hplcms_admins="Service-Account")
    base.update(overrides)
    return Settings(**base)


def test_service_start_blocks_submissions_then_end_resumes():
    runner = FakeRunner(busy=False)
    client = _authed_client(
        _load("signals_ready.json"), runner=runner,
        owner="Service-Account", settings=_admin_settings(),
    )
    r = client.post("/control/service/start")
    assert r.status_code == 200, r.text
    assert r.json()["service_mode"] is True

    body = client.get("/status").json()
    assert body["details"]["service_mode"] is True
    assert body["details"]["servicing"] is True
    assert "run.submit" not in body["allowed_actions"]

    # Submissions are refused while service mode is on (409 instrument_servicing).
    r2 = client.post("/control/run", json=VALID_RUN_BODY)
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "instrument_servicing"

    r3 = client.post("/control/service/end")
    assert r3.status_code == 200
    assert r3.json()["service_mode"] is False
    assert client.get("/status").json()["details"]["service_mode"] is False


def test_service_toggle_403_for_non_admin():
    runner = FakeRunner(busy=False)
    # Default settings make the owner an 'hte' user, not an admin.
    client = _authed_client(_load("signals_ready.json"), runner=runner, owner="HTE-User")
    r = client.post("/control/service/start")
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "role_forbidden"
    assert detail["required_role"] == "hplcms_admin"
    # And service mode was not turned on.
    assert runner.service_mode() is False


def test_service_start_423_without_token():
    client = _client(_load("signals_ready.json"), runner=FakeRunner())
    assert client.post("/control/service/start").status_code == 423


def test_service_mode_persists_across_claim_release():
    """The flag is standalone: releasing the admin claim leaves service mode ON
    (a dropped dashboard must not silently un-block a maintenance window)."""
    runner = FakeRunner(busy=False)
    admin = _authed_client(
        _load("signals_ready.json"), runner=runner,
        owner="Service-Account", settings=_admin_settings(),
    )
    assert admin.post("/control/service/start").status_code == 200
    assert admin.post("/control/release").status_code == 204
    # Claim gone, but the flag — and the 409 — remain.
    assert runner.service_mode() is True
    assert admin.get("/status").json()["details"]["service_mode"] is True


# ---------------------------------------------------------------------------
# v1.1 §6 — queue_full → 412 (not 409) + Retry-After; allowed_actions mirror
# ---------------------------------------------------------------------------

def test_queue_full_returns_412_with_retry_after():
    runner = FakeRunner(queue_full=True, run_id="active-1")
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 412
    detail = r.json()["detail"]
    assert detail["error"] == "queue_full"
    assert detail["retry_after_s"] is not None
    assert r.headers.get("Retry-After") is not None


def test_standby_queue_full_returns_412():
    runner = FakeRunner(queue_full=True, run_id="active-1")
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/standby")
    assert r.status_code == 412
    assert r.json()["detail"]["error"] == "queue_full"


def test_allowed_actions_ready_lists_all_verbs():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    actions = client.get("/status").json()["allowed_actions"]
    assert actions == [
        "run.submit", "run.abort", "queue.cancel", "instrument.standby", "workflow.start",
    ]


def test_allowed_actions_requires_init_drops_enqueue_verbs():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_requires_init.json"), runner=runner)
    actions = client.get("/status").json()["allowed_actions"]
    assert "run.submit" not in actions
    assert "instrument.standby" not in actions
    assert "workflow.start" not in actions
    # abort / cancel carry no enqueue precondition.
    assert "run.abort" in actions
    assert "queue.cancel" in actions


def test_allowed_actions_servicing_drops_enqueue_verbs():
    runner = FakeRunner(busy=False, servicing=True)
    client = _client(_load("signals_ready.json"), runner=runner)
    body = client.get("/status").json()
    actions = body["allowed_actions"]
    assert "run.submit" not in actions
    assert "instrument.standby" not in actions
    assert "workflow.start" not in actions
    assert "run.abort" in actions and "queue.cancel" in actions
    assert body["details"]["servicing"] is True


def test_allowed_actions_unknown_state_is_empty():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_unknown.json"), runner=runner)
    body = client.get("/status").json()
    assert body["equipment_status"] == "unknown"
    assert body["allowed_actions"] == []


def test_allowed_actions_mirror_412_when_queue_full():
    """§6.2 invariant, end-to-end: when the queue is full, /status drops the
    enqueue verbs AND POSTing them 412s; the non-enqueue verbs stay listed."""
    runner = FakeRunner(queue_full=True, run_id="active-1")
    client = _authed_client(_load("signals_ready.json"), runner=runner)

    actions = client.get("/status").json()["allowed_actions"]
    assert "run.submit" not in actions
    assert "instrument.standby" not in actions
    assert "run.abort" in actions and "queue.cancel" in actions

    # The dropped verbs really do 412 (allowed_actions never lies).
    assert client.post("/control/run", json=VALID_RUN_BODY).status_code == 412
    assert client.post("/control/standby").status_code == 412


def test_allowed_actions_helper_matches_refusal_property():
    """Unit-level §6.2 property: ``verb in allowed_actions`` iff an enqueue POST
    would NOT refuse, across every combination of the gating conditions."""
    from agilent_hplcms_server.control.actions import allowed_actions

    for requires_init in (False, True):
        for queue_full in (False, True):
            for servicing in (False, True):
                for workflow_active in (False, True):
                    actions = allowed_actions(
                        service_operational=True,
                        requires_init=requires_init,
                        queue_full=queue_full,
                        servicing=servicing,
                        workflow_active=workflow_active,
                    )
                    can_enqueue = (
                        (not requires_init) and (not queue_full) and (not servicing)
                    )
                    assert ("run.submit" in actions) is can_enqueue
                    assert ("instrument.standby" in actions) is can_enqueue
                    assert ("workflow.start" in actions) is (
                        can_enqueue and not workflow_active
                    )
                    assert ("workflow.end" in actions) is workflow_active
                    # Non-enqueue verbs are always offered while operational.
                    assert "run.abort" in actions
                    assert "queue.cancel" in actions

    # Not operational (probe_error) → nothing offered.
    assert allowed_actions(
        service_operational=False, requires_init=False, queue_full=False
    ) == []


# ---------------------------------------------------------------------------
# tray + well → sample_position composition, plate geometry, robot reservation
# ---------------------------------------------------------------------------

def test_run_composes_sample_position_from_tray_well():
    """The device composes {drawer}-{well} from logical {tray, well}; the
    sidecar-only fields plate_format/submitter are not forwarded to Moses."""
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "cpd_01", "tray": "front", "well": "A1", "injection_volume": 2.0},
        {"sample_name": "cpd_02", "tray": "rear", "well": "H12", "injection_volume": 1.0},
    ], "submitter": "robot"}  # robot so the front (reserved) sample is allowed
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text
    job = runner.submitted[0]["job"]
    # Defaults: front → D1F, rear → D4B (config.Settings).
    assert job["samples"][0]["sample_position"] == "D1F-A1"
    assert job["samples"][1]["sample_position"] == "D4B-H12"
    assert "tray" not in job["samples"][0]
    assert "well" not in job["samples"][0]
    assert "plate_format" not in job
    assert "submitter" not in job


def test_run_422_well_off_plate_for_format():
    """Wells are validated against plate_format: A13/I1 are off a 96-well plate."""
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    for well in ("A13", "I1"):
        bad = {**VALID_RUN_BODY, "samples": [
            {"sample_name": "x", "tray": "front", "well": well, "injection_volume": 2.0}
        ]}
        r = client.post("/control/run", json=bad)
        assert r.status_code == 422, f"{well!r} should be off a 96-well plate, got {r.status_code}"


def test_run_384_well_plate_accepts_high_wells():
    """A 384-well plate accepts P24, which is off a 96-well plate."""
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {**VALID_RUN_BODY, "plate_format": "384-well", "submitter": "robot", "samples": [
        {"sample_name": "x", "tray": "rear", "well": "P24", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text
    assert runner.submitted[0]["job"]["samples"][0]["sample_position"] == "D4B-P24"


def test_run_412_reserved_tray_for_manual_submitter():
    """A manual run targeting the robot-reserved tray (default 'front') is refused 412."""
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "x", "tray": "front", "well": "A1", "injection_volume": 2.0}
    ]}  # submitter defaults to "manual"
    r = client.post("/control/run", json=body)
    assert r.status_code == 412, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "reserved_for_robot"
    assert detail["reserved_tray"] == "front"
    assert runner.submitted == []  # never enqueued


def test_run_robot_submitter_allowed_on_reserved_tray():
    """submitter='robot' bypasses the reservation."""
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {**VALID_RUN_BODY, "submitter": "robot", "samples": [
        {"sample_name": "x", "tray": "front", "well": "A1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text
