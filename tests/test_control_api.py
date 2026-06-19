"""Tests for the /control/* endpoints."""

from __future__ import annotations

import json
import tempfile
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
    active = status in ("dispatched", "enqueued", "acquiring")
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
        queue_full: bool = False,
    ) -> None:
        super().__init__()
        if busy or queue_full:
            entry = _fake_job_entry(run_id, status="dispatched")
            self._jobs[run_id] = entry
            self._active_id = run_id
        self.submitted: list[dict] = []
        self.aborted = False
        self._next_run_id = "queued-run-1"
        self._force_acquiring = force_acquiring
        self._queue_full = queue_full

    def check_acquiring(self, settings=None) -> bool:  # type: ignore[override]
        return self._force_acquiring

    def is_queue_full(self, settings=None) -> bool:  # type: ignore[override]
        return self._queue_full

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


def _authed_client(
    signals: dict,
    runner: MosesRunner | None = None,
    *,
    owner: str = "test-operator",
    session_id: str = "test-session",
) -> TestClient:
    """A client that holds a valid claim, with ``X-Claim-Token`` pre-set as a
    default header on every request — mirrors how the aggregator drives a
    hard-enforcement (v1.1) device. Use for tests that hit mutating
    ``/control/*`` endpoints; read-only tests can use :func:`_client`.
    """
    client = _client(signals, runner=runner)
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
        {"sample_name": "cpd_01", "tray": "front", "well": "A1", "injection_volume": 2.0}
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


def test_run_queued_when_olss_reports_external_acquisition():
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_olss_run.json"), runner=runner)
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
        {"sample_name": "s1", "tray": "front", "well": "A1", "injection_volume": 999.0}
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
        {"sample_name": "has spaces", "tray": "front", "well": "A1", "injection_volume": 2.0}
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


def test_post_queue_queued_when_olss_reports_external_acquisition():
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_olss_run.json"), runner=runner)
    r = client.post("/control/queue", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["position"] == 1
    assert runner.get_active() is None


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

    # Moses exited while OLSS was occupied → job submitted to OpenLab queue
    assert runner.get_active() is not None
    assert entry.status == "enqueued"
    assert entry.process is None

    # OLSS reports Running → instrument picked up the job
    runner.notify_olss_state("Run", "OK")
    runner.poll(settings=Settings())

    assert runner.get_active() is not None
    assert entry.status == "acquiring"

    # OLSS returns to idle. Entry has no output_dir → _has_sirslt returns True
    # (cannot verify, assume done). notify_olss_state finalises before poll().
    runner.notify_olss_state("Idle", "OK")
    runner.poll(settings=Settings())

    assert runner.get_active() is None
    assert entry.status == "done"


# ---------------------------------------------------------------------------
# Fix 1: abort() atomicity — job shows "failed" immediately after abort
# ---------------------------------------------------------------------------

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


def test_abort_active_run_real_runner():
    """MosesRunner.abort() marks the job failed inside the lock (no second lock race)."""
    runner = MosesRunner()
    entry = _fake_job_entry("active-123", status="dispatched")
    runner._jobs[entry.queue_id] = entry
    runner._active_id = entry.queue_id

    was_active, n_cleared = runner.abort(settings=Settings())

    assert was_active is True
    assert n_cleared == 0
    assert entry.status == "failed"
    assert entry.error_msg == "Aborted by operator"
    assert runner.get_active() is None


# ---------------------------------------------------------------------------
# Fix 2: OLSS→idle sync — correct lifecycle when preceding job finishes first
# ---------------------------------------------------------------------------

def test_runner_keeps_enqueued_when_olss_idle_no_results():
    """OLSS going idle with no sirslt → job demoted to enqueued (preceding job may have finished,
    ours is still waiting in OpenLab's queue)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        runner = MosesRunner()
        entry = _fake_job_entry(
            "active-123",
            status="acquiring",
            request_dict={
                "script_name": "examples/agent_agilent.py",
                "output_dir": tmp_dir,
            },
        )
        entry.process = None  # Moses already exited
        runner._jobs[entry.queue_id] = entry
        runner._active_id = entry.queue_id
        runner._olss_occupied = True  # instrument was Running

        # OLSS returns to idle; output_dir exists but no sirslt files yet
        runner.notify_olss_state("Idle", None)

        # Job should be "enqueued" — it may still be waiting in OpenLab's queue
        assert runner.get_active() is not None
        assert entry.status == "enqueued"


def test_runner_marks_acquiring_job_done_on_olss_idle_with_results():
    """OLSS going idle with sirslt present → job marked done (run completed normally)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        runner = MosesRunner()
        entry = _fake_job_entry(
            "active-123",
            status="acquiring",
            request_dict={
                "script_name": "examples/agent_agilent.py",
                "output_dir": tmp_dir,
            },
        )
        entry.process = None
        runner._jobs[entry.queue_id] = entry
        runner._active_id = entry.queue_id
        runner._olss_occupied = True

        # Simulate sirslt file written by OpenLab
        (tmp_path / "sample1.sirslt").touch()

        runner.notify_olss_state("Idle", None)

        assert runner.get_active() is None
        assert entry.status == "done"


def test_runner_no_false_failure_when_olss_never_occupied():
    """No transition detection fires when OLSS was already idle before the job."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        runner = MosesRunner()
        entry = _fake_job_entry(
            "active-123",
            status="acquiring",
            request_dict={
                "script_name": "examples/agent_agilent.py",
                "output_dir": tmp_dir,
            },
        )
        entry.process = None
        runner._jobs[entry.queue_id] = entry
        runner._active_id = entry.queue_id
        # _olss_occupied stays False (default) — no occupied→idle transition

        runner.notify_olss_state("Idle", None)

        # No transition (was_occupied=False) → job untouched
        assert runner.get_active() is not None
        assert entry.status == "acquiring"


def test_poll_keeps_enqueued_when_moses_exits_cleanly_no_sirslt():
    """Moses exits rc=0, OLSS idle, no sirslt yet → job set to enqueued (queued in OpenLab)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        runner = MosesRunner()
        entry = _fake_job_entry(
            "active-123",
            status="dispatched",
            request_dict={
                "script_name": "examples/agent_agilent.py",
                "output_dir": tmp_dir,
            },
        )
        entry.process.poll.return_value = 0  # Moses exited cleanly
        runner._jobs[entry.queue_id] = entry
        runner._active_id = entry.queue_id
        runner._olss_occupied = False  # OLSS idle at this moment

        runner.poll(settings=Settings())

        # No sirslt files → job may be queued in OpenLab; show as enqueued
        assert runner.get_active() is not None
        assert entry.status == "enqueued"
        assert entry.process is None  # process handle released


def test_poll_marks_done_when_moses_exits_cleanly_with_sirslt():
    """Moses exits rc=0, OLSS idle, sirslt present → job immediately marked done."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        runner = MosesRunner()
        entry = _fake_job_entry(
            "active-123",
            status="dispatched",
            request_dict={
                "script_name": "examples/agent_agilent.py",
                "output_dir": tmp_dir,
            },
        )
        entry.process.poll.return_value = 0  # Moses exited cleanly
        runner._jobs[entry.queue_id] = entry
        runner._active_id = entry.queue_id
        runner._olss_occupied = False

        # Sirslt file already present when Moses exits
        (tmp_path / "result.sirslt").touch()

        runner.poll(settings=Settings())

        assert runner.get_active() is None
        assert entry.status == "done"


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
    assert actions == ["run.submit", "run.abort", "queue.cancel", "instrument.standby"]


def test_allowed_actions_requires_init_drops_enqueue_verbs():
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_requires_init.json"), runner=runner)
    actions = client.get("/status").json()["allowed_actions"]
    assert "run.submit" not in actions
    assert "instrument.standby" not in actions
    # abort / cancel carry no enqueue precondition.
    assert "run.abort" in actions
    assert "queue.cancel" in actions


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
            actions = allowed_actions(
                service_operational=True,
                requires_init=requires_init,
                queue_full=queue_full,
            )
            can_enqueue = (not requires_init) and (not queue_full)
            assert ("run.submit" in actions) is can_enqueue
            assert ("instrument.standby" in actions) is can_enqueue
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
    ], "submitter": "robot"}  # robot so the rear (reserved) sample is allowed
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
    """A manual run targeting the robot-reserved tray (default 'rear') is refused 412."""
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "x", "tray": "rear", "well": "A1", "injection_volume": 2.0}
    ]}  # submitter defaults to "manual"
    r = client.post("/control/run", json=body)
    assert r.status_code == 412, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "reserved_for_robot"
    assert detail["reserved_tray"] == "rear"
    assert runner.submitted == []  # never enqueued


def test_run_robot_submitter_allowed_on_reserved_tray():
    """submitter='robot' bypasses the reservation."""
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {**VALID_RUN_BODY, "submitter": "robot", "samples": [
        {"sample_name": "x", "tray": "rear", "well": "A1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text
