"""Tests for the /control/* endpoints.

Queue-ownership model (2026-06-23): the sidecar's MosesRunner is the sole queue
and Moses runs synchronously, so **process exit is authoritative** (rc==0 →
done, rc!=0 → failed). There is no OpenLab-queue "enqueued"/"acquiring" state and
no .sirslt finalization. OLSS is observed only to detect technician *servicing*.
Submission precedence (highest first): servicing 409 > workflow 423 > queue >
idle. Claims carry a roster-resolved role (user | automation | service); workflow.start is
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
from agilent_hplcms_server.control.roster_sync import RosterProvider
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
        handoff_in_flight: bool = False,
    ) -> None:
        super().__init__()
        if busy or queue_full:
            entry = _fake_job_entry(run_id, status="running")
            self._jobs[run_id] = entry
            self._active_id = run_id
        self.submitted: list[dict] = []
        self.handoffs: list[dict] = []
        self.aborted = False
        self._next_run_id = "queued-run-1"
        self._queue_full = queue_full
        self._servicing = servicing
        self._handoff_in_flight = handoff_in_flight

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

    def submit_openlab_handoff(  # type: ignore[override]
        self,
        script_name: str,
        job: dict,
        request_dict: dict,
        settings=None,
    ) -> str:
        from agilent_hplcms_server.control.runner import HandoffInProgress

        if self._handoff_in_flight:
            raise HandoffInProgress("An OpenLab handoff dispatch is already in flight.")
        handoff_id = "handoff-run-1"
        entry = JobEntry(
            queue_id=handoff_id,
            script_name=script_name,
            job=job,
            request_dict=request_dict,
            queued_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            status="dispatching",
            dispatch="openlab",
            pid=4242,
            process=_make_mock_proc(),
            job_path=Path("fake_handoff.json"),
        )
        self._jobs[handoff_id] = entry
        self.handoffs.append({"script_name": script_name, "job": job, "run_id": handoff_id})
        return handoff_id

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
    base = dict(
        hplcms_users="", hte_users="*", hplcms_admins="",
        consumable_ack_file="",  # in-memory ack store — never touch the real file
        lc_fault_ack_file="",   # ditto for module fault acknowledgments
    )
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
        # D4B is an unreserved manual drawer; D1F is reserved for the robot.
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
        {"sample_name": "has spaces", "sample_position": "D4B-A1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=bad)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Labware matching (config-driven plate geometry, control/labware.py)
# ---------------------------------------------------------------------------

def _labware_settings(tmp_path, drawers: dict, **overrides) -> Settings:
    """Write a labware config file and return settings pointing at it."""
    from agilent_hplcms_server.control.labware import load_labware

    path = tmp_path / "labware.json"
    path.write_text(json.dumps({"drawers": drawers}), encoding="utf-8")
    load_labware.cache_clear()  # avoid a stale cache from another test's file
    return _settings(labware_config_path=str(path), **overrides)


# The D4B drawer physically holds a 6x9 54-vial plate on this instrument.
_D4B_54VIAL = {"D4B": {"plate_type": "54-vial", "rows": 6, "cols": 9, "num_locations": 54}}


def test_run_accepts_well_on_configured_plate(tmp_path):
    runner = FakeRunner(busy=False)
    settings = _labware_settings(tmp_path, _D4B_54VIAL)
    client = _authed_client(_load("signals_ready.json"), runner=runner, settings=settings)
    # F9 is the last well of a 6x9 plate — valid here.
    body = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "s1", "sample_position": "D4B-F9", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text


def test_run_rejects_well_off_configured_54vial_plate(tmp_path):
    """G1 passes the built-in 96-well check but is off a 6x9 54-vial plate."""
    runner = FakeRunner(busy=False)
    settings = _labware_settings(tmp_path, _D4B_54VIAL)
    client = _authed_client(_load("signals_ready.json"), runner=runner, settings=settings)
    body = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "s1", "sample_position": "D4B-G1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "plate_mismatch"
    assert detail["configured"] == "54-vial"
    assert detail["drawer"] == "D4B"


def test_run_rejects_declared_plate_format_mismatch(tmp_path):
    runner = FakeRunner(busy=False)
    settings = _labware_settings(tmp_path, _D4B_54VIAL)
    client = _authed_client(_load("signals_ready.json"), runner=runner, settings=settings)
    body = {**VALID_RUN_BODY, "plate_format": "96-well", "samples": [
        {"sample_name": "s1", "sample_position": "D4B-A1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "plate_mismatch"
    assert detail["declared"] == "96-well"
    assert detail["configured"] == "54-vial"


def test_run_rejects_unconfigured_drawer(tmp_path):
    """D4B submission when only the D1F drawer has configured labware."""
    runner = FakeRunner(busy=False)
    drawers = {"D1F": {"plate_type": "96-well", "rows": 8, "cols": 12}}
    settings = _labware_settings(tmp_path, drawers)
    client = _authed_client(_load("signals_ready.json"), runner=runner, settings=settings)
    r = client.post("/control/run", json=VALID_RUN_BODY)  # targets D4B
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "plate_mismatch"


def test_run_matching_declared_plate_format_accepted(tmp_path):
    runner = FakeRunner(busy=False)
    settings = _labware_settings(tmp_path, _D4B_54VIAL)
    client = _authed_client(_load("signals_ready.json"), runner=runner, settings=settings)
    body = {**VALID_RUN_BODY, "plate_format": "54-vial", "samples": [
        {"sample_name": "s1", "sample_position": "D4B-A1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text


# The same drawer as _D4B_54VIAL, but named the way OpenLab (and the capture
# tool) names it — the form a generated labware config actually contains.
_D4B_OPENLAB = {"D4B": {"plate_type": "*54VialPlate*", "rows": 6, "cols": 9,
                        "num_locations": 54, "well_height_mm": 36.0}}
# Same 8x12 addressable geometry as *96Agilent*, 29.7 mm taller.
_D4B_DEEP96 = {"D4B": {"plate_type": "96DeepAgilent45mm", "rows": 8, "cols": 12,
                       "num_locations": 96, "well_height_mm": 44.0}}


def test_run_accepts_canonical_name_against_openlab_named_config(tmp_path):
    """A generated config names the plate "*54VialPlate*"; callers (and
    hardware_smoke_test.py) declare canonical names. Both name one plate, so
    the declaration is accepted."""
    runner = FakeRunner(busy=False)
    settings = _labware_settings(tmp_path, _D4B_OPENLAB)
    client = _authed_client(_load("signals_ready.json"), runner=runner, settings=settings)
    body = {**VALID_RUN_BODY, "plate_format": "54-vial", "samples": [
        {"sample_name": "s1", "sample_position": "D4B-A1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text


def test_run_accepts_openlab_name_against_openlab_named_config(tmp_path):
    runner = FakeRunner(busy=False)
    settings = _labware_settings(tmp_path, _D4B_OPENLAB)
    client = _authed_client(_load("signals_ready.json"), runner=runner, settings=settings)
    body = {**VALID_RUN_BODY, "plate_format": "*54VialPlate*", "samples": [
        {"sample_name": "s1", "sample_position": "D4B-A1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text


def test_run_rejects_96_well_declaration_against_deep_well_drawer(tmp_path):
    """The 29 Aug needle crash, as an HTTP refusal: the drawer holds a 44 mm
    deep-well plate and the caller declares a shallow 96-well. Identical rows
    and cols, so only the name catches it."""
    runner = FakeRunner(busy=False)
    settings = _labware_settings(tmp_path, _D4B_DEEP96)
    client = _authed_client(_load("signals_ready.json"), runner=runner, settings=settings)
    body = {**VALID_RUN_BODY, "plate_format": "96-well", "samples": [
        {"sample_name": "s1", "sample_position": "D4B-A1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "plate_mismatch"
    assert detail["declared"] == "96-well"
    assert detail["configured"] == "96DeepAgilent45mm"


def test_run_no_labware_config_uses_legacy_check():
    """With no labware config, the built-in 96-well check still applies."""
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)  # no labware
    # G1 is valid for the default 96-well built-in geometry -> accepted.
    body = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "s1", "sample_position": "D4B-G1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text


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


def _write_log(tmp_path: Path, text: str) -> Path:
    log_path = tmp_path / "job.log"
    log_path.write_text(text, encoding="utf-8")
    return log_path


# run_batch raises when only the post-run standby park fails, even though every
# sample acquisition completed and OpenLab recorded the data. The runner must
# reconcile that to `done` (data is valid) instead of a bare `failed`.
_STANDBY_ONLY_LOG = (
    "2026-07-23 12:07:26  INFO      agent — Starting run: cpd_01 | position: D4B-A1\n"
    "2026-07-23 12:09:50  INFO      agent — Saved result: .../cpd_01.npz\n"
    "2026-07-23 12:10:04  ERROR     agent — Standby run failed: pump comm timeout\n"
    "Traceback (most recent call last):\n"
    "RuntimeError: 1 run(s) failed during batch:\n"
    "Standby failed: pump comm timeout\n"
)

_SAMPLE_FAIL_LOG = (
    "2026-07-23 12:07:26  INFO      agent — Starting run: cpd_01 | position: D4B-A1\n"
    "2026-07-23 12:08:10  ERROR     agent — Run 1/2 failed (cpd_01): injection error\n"
    "Traceback (most recent call last):\n"
    "RuntimeError: 1 run(s) failed during batch:\n"
    "Run 1/2 failed (cpd_01): injection error\n"
)


def test_poll_standby_park_failure_finalizes_done_with_warning(tmp_path):
    """A run whose samples all completed but whose post-run standby park failed
    is finalized `done` (acquisition valid) with the standby problem surfaced in
    error_msg — not marked `failed` (which would wrongly trigger a re-run)."""
    runner = MosesRunner()
    entry = _fake_job_entry("run-standby", status="running")
    entry.job = {"samples": [{"sample_name": "cpd_01", "sample_position": "D4B-A1"}]}
    entry.log_path = _write_log(tmp_path, _STANDBY_ONLY_LOG)
    entry.process.poll.return_value = 1  # type: ignore[union-attr]
    runner._jobs[entry.queue_id] = entry
    runner._active_id = entry.queue_id

    runner.poll(settings=Settings())

    assert entry.status == "done"
    assert "standby park failed" in (entry.error_msg or "").lower()
    assert "pump comm timeout" in (entry.error_msg or "")


def test_poll_sample_failure_still_fails_with_reason(tmp_path):
    """A genuine sample-acquisition failure stays `failed`, and the reason from
    the Moses log is captured in error_msg (not a bare 'Exit code N')."""
    runner = MosesRunner()
    entry = _fake_job_entry("run-sample-fail", status="running")
    entry.job = {"samples": [{"sample_name": "cpd_01", "sample_position": "D4B-A1"}]}
    entry.log_path = _write_log(tmp_path, _SAMPLE_FAIL_LOG)
    entry.process.poll.return_value = 1  # type: ignore[union-attr]
    runner._jobs[entry.queue_id] = entry
    runner._active_id = entry.queue_id

    runner.poll(settings=Settings())

    assert entry.status == "failed"
    assert "Run 1/2 failed (cpd_01): injection error" in (entry.error_msg or "")


def test_poll_standby_only_job_failure_stays_failed(tmp_path):
    """A standby-only job (POST /control/standby, no samples) that fails has no
    acquisition to preserve, so it stays `failed`."""
    runner = MosesRunner()
    entry = _fake_job_entry("run-standby-only", status="running")
    entry.job = {"samples": []}
    entry.log_path = _write_log(tmp_path, _STANDBY_ONLY_LOG)
    entry.process.poll.return_value = 1  # type: ignore[union-attr]
    runner._jobs[entry.queue_id] = entry
    runner._active_id = entry.queue_id

    runner.poll(settings=Settings())

    assert entry.status == "failed"
    assert "pump comm timeout" in (entry.error_msg or "")


def test_queue_status_surfaces_error_message(tmp_path):
    """GET /control/queue exposes error_message so a failed job shows its reason
    instead of a bare 'failed'."""
    runner = MosesRunner()
    entry = _fake_job_entry("run-visible", status="running")
    entry.job = {"samples": [{"sample_name": "cpd_01", "sample_position": "D4B-A1"}]}
    entry.log_path = _write_log(tmp_path, _SAMPLE_FAIL_LOG)
    entry.process.poll.return_value = 1  # type: ignore[union-attr]
    runner._jobs[entry.queue_id] = entry
    runner._active_id = entry.queue_id

    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = client.get("/control/queue").json()
    job = {j["queue_id"]: j for j in body["queue"]}["run-visible"]
    assert job["status"] == "failed"
    assert "injection error" in (job["error_message"] or "")


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


def test_no_servicing_when_olss_reports_no_active_run_label():
    """Some OLSS responses label the empty currentRun as 'no active run'."""
    runner = MosesRunner()
    s = Settings(servicing_debounce_polls=1)
    runner.notify_olss_state("Sleep", "OK", "no active run")
    runner.notify_olss_state("Sleep", "OK", "no active run")
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


def test_run_is_queued_while_a_technician_run_is_detected():
    # Auto-detected servicing (OLSS is acquiring a run we did not queue) is
    # ordinary "busy": the job is accepted and held, and the runner starts it
    # once the instrument frees. Refusing at the door made the documented
    # "busy -> FIFO queue" branch unreachable for the commonest kind of busy,
    # and blocked every submission whenever a colleague ran one sample by hand.
    runner = FakeRunner(busy=False, servicing=True)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 202, r.text
    assert runner.submitted != []          # accepted
    assert runner.get_active() is None     # never launched into the session


def test_run_409_under_explicit_service_mode():
    # The explicit toggle is a human declaring they own the instrument, so a
    # job that would surface later without them asking is refused outright.
    runner = FakeRunner(busy=False)
    runner.set_service_mode(True)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "instrument_servicing"
    assert r.headers.get("Retry-After") is None  # duration unpredictable
    assert runner.submitted == []  # never enqueued


def test_queue_accepts_and_names_the_hold_during_a_technician_run():
    runner = FakeRunner(busy=False, servicing=True)
    client = _client(_load("signals_ready.json"), runner=runner)
    body = client.get("/control/queue").json()
    assert body["accepting_jobs"] is True
    assert body["dispatch_held_reason"] == "servicing"


def test_queue_not_accepting_under_explicit_service_mode():
    runner = FakeRunner(busy=False)
    runner.set_service_mode(True)
    client = _client(_load("signals_ready.json"), runner=runner)
    body = client.get("/control/queue").json()
    assert body["accepting_jobs"] is False
    assert body["dispatch_held_reason"] == "service_mode"


def test_standby_still_refused_during_a_technician_run():
    # Parking the instrument is actuation, not enqueueing: it would disturb the
    # technician's session now, so it stays refused under either source.
    runner = FakeRunner(busy=False, servicing=True)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/standby")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "instrument_servicing"


def test_workflow_start_refused_under_either_servicing_source():
    """Taking the equipment lock cannot wait in the queue, so unlike run.submit
    it is refused by the inferred source too."""
    inferred = FakeRunner(busy=False, servicing=True)
    client = _authed_client(_load("signals_ready.json"), runner=inferred)
    r = client.post("/control/workflow/start")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "instrument_servicing"

    explicit = FakeRunner(busy=False)
    explicit.set_service_mode(True)
    client2 = _authed_client(_load("signals_ready.json"), runner=explicit)
    assert client2.post("/control/workflow/start").status_code == 409


# ---------------------------------------------------------------------------
# dispatch="openlab" — fire-and-forget handoff into OpenLab's native run queue
# ---------------------------------------------------------------------------
# The queue-ownership pivot made the sidecar FIFO the only path to the
# instrument, which also made queued work invisible from OpenLab's own Run
# Queue — the surface a technician plans from. dispatch="openlab" restores the
# pre-pivot submission mode as an explicit per-request opt-in: a submit-and-exit
# Moses script enqueues the job in OpenLab and returns, and the sidecar tracks
# only the handoff (dispatching → handed_off | failed), never the acquisition.
# Once the run acquires, the sidecar sees it as an OpenLab run it did not queue
# — i.e. exactly like technician activity — so the FIFO holds behind it.

OPENLAB_RUN_BODY = {**VALID_RUN_BODY, "dispatch": "openlab"}


def test_openlab_dispatch_hands_off_during_a_technician_run():
    # The motivating case: a technician sequence is acquiring, the FIFO is
    # held — an openlab dispatch still goes out, lining up in OpenLab's queue.
    runner = FakeRunner(busy=False, servicing=True)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=OPENLAB_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "dispatching"
    assert body["run_id"] == "handoff-run-1"
    assert runner.handoffs != []           # handed to the submit script
    assert runner.submitted == []          # never entered the FIFO
    assert runner.get_active() is None     # never took the FIFO slot


def test_openlab_dispatch_still_refused_under_explicit_service_mode():
    # The explicit toggle means a human declared they own the instrument;
    # lining work up behind them in OpenLab is still work they did not ask for.
    runner = FakeRunner(busy=False)
    runner.set_service_mode(True)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=OPENLAB_RUN_BODY)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "instrument_servicing"
    assert runner.handoffs == []


def test_openlab_dispatch_rejects_an_explicit_script_name():
    # The mode determines the script (config-owned); a caller-chosen script
    # under dispatch="openlab" is a contradiction, refused rather than ignored.
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {**OPENLAB_RUN_BODY, "script_name": "examples/agent_agilent.py"}
    r = client.post("/control/run", json=body)
    assert r.status_code == 422
    assert "script_name" in r.text
    assert runner.handoffs == []


def test_openlab_dispatch_412_while_a_dispatch_is_in_flight():
    # One submit subprocess at a time: concurrent Moses invocations against
    # OpenLab are unexercised territory, so a second dispatch waits its turn.
    runner = FakeRunner(busy=False, handoff_in_flight=True)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=OPENLAB_RUN_BODY)
    assert r.status_code == 412
    detail = r.json()["detail"]
    assert detail["error"] == "dispatch_in_progress"
    assert r.headers.get("Retry-After") is not None
    assert runner.handoffs == []


def test_openlab_dispatch_still_validates_the_reserved_drawer():
    # Pre-enqueue validation is dispatch-agnostic: the robot-drawer reservation
    # and labware geometry guard OpenLab-bound jobs exactly like FIFO jobs.
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {
        **OPENLAB_RUN_BODY,
        "samples": [
            {"sample_name": "cpd_01", "sample_position": "D1F-A1", "injection_volume": 2.0}
        ],
    }
    r = client.post("/control/run", json=body)
    assert r.status_code == 412
    assert r.json()["detail"]["error"] == "reserved_for_robot"
    assert runner.handoffs == []


def test_post_queue_openlab_dispatch_returns_dispatching():
    runner = FakeRunner(busy=False, servicing=True)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/queue", json=OPENLAB_RUN_BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "dispatching"
    assert body["position"] == 0
    assert runner.submitted == []


def test_queue_view_shows_the_handoff_and_excludes_it_from_pending():
    runner = FakeRunner(busy=False, servicing=True)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    r = client.post("/control/run", json=OPENLAB_RUN_BODY)
    assert r.status_code == 202, r.text
    hid = r.json()["run_id"]
    runner._jobs[hid].status = "handed_off"  # the poller reaped a clean exit
    body = client.get("/control/queue").json()
    jobs = {j["queue_id"]: j for j in body["queue"]}
    assert jobs[hid]["dispatch"] == "openlab"
    assert jobs[hid]["status"] == "handed_off"
    assert body["pending_count"] == 0
    assert body["active_run_id"] is None


# --- MosesRunner handoff lifecycle (real runner, faked subprocess) ---------


def _handoff_settings(tmp_path: Path) -> "Settings":
    """Settings pointing every Moses path at tmp_path, with both the batch and
    the enqueue script present so either dispatch path can launch."""
    work = tmp_path / "work"
    (work / "examples").mkdir(parents=True)
    (work / "examples" / "agent_agilent.py").write_text("# stub", encoding="utf-8")
    (work / "examples" / "agent_agilent_enqueue.py").write_text("# stub", encoding="utf-8")
    return Settings(
        moses_work_dir=str(work),
        run_jobs_dir=str(tmp_path / "jobs"),
        moses_allowed_scripts="examples/agent_agilent.py",
        moses_openlab_submit_script="examples/agent_agilent_enqueue.py",
        hplcms_users="",
        hte_users="*",
        hplcms_admins="",
        consumable_ack_file="",
    )


def _dispatching_handoff_entry(runner: MosesRunner, rc: int | None) -> JobEntry:
    """Wire a dispatching handoff entry (mock submit subprocess) into a runner."""
    entry = JobEntry(
        queue_id="h-1",
        script_name="examples/agent_agilent_enqueue.py",
        job={"samples": [{"sample_name": "cpd_01"}]},
        request_dict={},
        queued_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        status="dispatching",
        dispatch="openlab",
        pid=999,
        process=_make_mock_proc(),
        job_path=Path("fake_handoff.json"),
    )
    entry.process.poll.return_value = rc  # type: ignore[union-attr]
    runner._jobs["h-1"] = entry
    runner._handoff_id = "h-1"
    return entry


def test_handoff_dispatches_outside_the_fifo_slot(tmp_path, monkeypatch):
    from agilent_hplcms_server.control import runner as runner_mod

    monkeypatch.setattr(runner_mod.subprocess, "Popen", lambda *a, **k: _make_mock_proc())
    runner = MosesRunner()
    s = _handoff_settings(tmp_path)

    hid = runner.submit_openlab_handoff(
        script_name=s.moses_openlab_submit_script,
        job={"samples": [{"sample_name": "cpd_01"}]},
        request_dict={},
        settings=s,
    )
    entry = runner._jobs[hid]
    assert entry.status == "dispatching"
    assert entry.dispatch == "openlab"
    assert runner.get_active() is None
    assert runner.queue_depth() == 0

    # The FIFO slot is untouched: a sidecar submission still launches at once.
    run_id, position = runner.submit_to_queue(
        script_name="examples/agent_agilent.py",
        job={"samples": [{"sample_name": "cpd_02"}]},
        request_dict={},
        settings=s,
    )
    assert position == 0
    assert runner.get_active() is not None
    assert run_id != hid


def test_second_handoff_while_dispatching_raises(tmp_path, monkeypatch):
    from agilent_hplcms_server.control import runner as runner_mod
    from agilent_hplcms_server.control.runner import HandoffInProgress

    monkeypatch.setattr(runner_mod.subprocess, "Popen", lambda *a, **k: _make_mock_proc())
    runner = MosesRunner()
    s = _handoff_settings(tmp_path)
    runner.submit_openlab_handoff(
        script_name=s.moses_openlab_submit_script,
        job={"samples": []},
        request_dict={},
        settings=s,
    )
    with pytest.raises(HandoffInProgress):
        runner.submit_openlab_handoff(
            script_name=s.moses_openlab_submit_script,
            job={"samples": []},
            request_dict={},
            settings=s,
        )


def test_poll_reaps_a_clean_handoff_as_handed_off():
    runner = MosesRunner()
    entry = _dispatching_handoff_entry(runner, rc=0)

    runner.poll(settings=Settings())

    assert entry.status == "handed_off"
    assert entry.process is None
    assert entry.finished_at is not None
    assert runner.get_active() is None


def test_poll_marks_a_failed_handoff_with_reason():
    runner = MosesRunner()
    entry = _dispatching_handoff_entry(runner, rc=2)

    runner.poll(settings=Settings())

    assert entry.status == "failed"
    assert "exit code 2" in (entry.error_msg or "").lower()


def test_poll_reaps_handoff_then_next_dispatch_is_accepted(tmp_path, monkeypatch):
    from agilent_hplcms_server.control import runner as runner_mod

    monkeypatch.setattr(runner_mod.subprocess, "Popen", lambda *a, **k: _make_mock_proc())
    runner = MosesRunner()
    s = _handoff_settings(tmp_path)
    entry = _dispatching_handoff_entry(runner, rc=0)
    runner.poll(settings=s)
    assert entry.status == "handed_off"

    # The slot is free again: a new dispatch goes out without a 412.
    hid = runner.submit_openlab_handoff(
        script_name=s.moses_openlab_submit_script,
        job={"samples": []},
        request_dict={},
        settings=s,
    )
    assert runner._jobs[hid].status == "dispatching"


def test_abort_kills_a_dispatching_handoff():
    runner = MosesRunner()
    entry = _dispatching_handoff_entry(runner, rc=None)
    proc = entry.process

    was_active, _ = runner.abort(settings=Settings())

    assert was_active is False  # the FIFO slot was empty
    assert entry.status == "failed"
    assert entry.error_msg == "Aborted by operator"
    proc.terminate.assert_called_once()  # type: ignore[union-attr]


def test_cancel_refuses_a_dispatching_handoff():
    runner = MosesRunner()
    _dispatching_handoff_entry(runner, rc=None)
    with pytest.raises(RuntimeError):
        runner.cancel_queued("h-1")


def test_cancel_refuses_a_handed_off_job():
    # Once handed off, the job lives in OpenLab's queue — cancel it there.
    runner = MosesRunner()
    entry = _dispatching_handoff_entry(runner, rc=None)
    entry.status = "handed_off"
    runner._handoff_id = None
    with pytest.raises(LookupError):
        runner.cancel_queued("h-1")


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
    assert resolve_role("alice", s) == "user"
    assert resolve_role("BOB", s) == "user"          # case-insensitive
    assert resolve_role("hte-user", s) == "automation"
    assert resolve_role("service-account", s) == "service"
    assert resolve_role("stranger", s) is None
    # All lists empty → built-in defaults apply (roster always enforced).
    d = Settings(hplcms_users="", hte_users="", hplcms_admins="")
    assert resolve_role("Hplcms-User", d) == "user"
    assert resolve_role("HTE-User", d) == "automation"
    assert resolve_role("Service-Account", d) == "service"
    assert resolve_role("stranger", d) is None
    # Explicit "*" wildcard = open (any owner), distinct from accidental empty.
    w = Settings(hplcms_users="*", hte_users="", hplcms_admins="")
    assert resolve_role("whoever", w) == "user"


def test_role_precedence_service_over_automation_over_user():
    both = Settings(hplcms_users="carol", hte_users="carol", hplcms_admins="carol")
    assert resolve_role("carol", both) == "service"
    hte_and_user = Settings(hplcms_users="dave", hte_users="dave", hplcms_admins="")
    assert resolve_role("dave", hte_and_user) == "automation"


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
    cases = {"HTE-User": "automation", "Hplcms-User": "user", "Service-Account": "service"}
    for i, (owner, role) in enumerate(cases.items()):
        c = _client(_load("signals_ready.json"), runner=FakeRunner(), settings=settings)
        got = c.post(
            "/control/claim", json={"owner": owner, "session_id": f"s{i}", "ttl_s": 30.0}
        ).json()["role"]
        assert got == role, f"{owner} → {got}, expected {role}"


def test_central_roster_is_authoritative_over_static_env():
    """When a central roster has been pulled, it decides owner→role — overriding
    the static env lists (here a permissive ``*``)."""
    payload = {
        "equipment_key": "agilent_uplc_ms",
        "entries": [{"owner": "alice@utoronto.ca", "role": "automation"}],
    }
    provider = RosterProvider(fetcher=lambda u, t, k: payload)
    # static would allow ANY owner (hte_users="*"); central must win once pulled.
    settings = _settings(roster_url="http://auth/equipment/agilent_uplc_ms/roster", hte_users="*")
    assert provider.refresh(settings) is True

    def fake_reader(_: Settings) -> dict:
        return dict(_load("signals_ready.json"))

    app = create_app(
        settings=settings, reader=fake_reader, runner=FakeRunner(), roster=provider
    )
    with TestClient(app) as client:
        # alice is on the central projection → claim ok, role resolved from central.
        ok = client.post(
            "/control/claim",
            json={"owner": "alice@utoronto.ca", "session_id": "s1", "ttl_s": 30.0},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["role"] == "automation"
        # stranger is NOT on central → 403, despite the permissive static "*".
        bad = client.post(
            "/control/claim",
            json={"owner": "stranger", "session_id": "s2", "ttl_s": 30.0},
        )
        assert bad.status_code == 403
        assert bad.json()["detail"]["error"] == "user_not_recognized"


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
    assert detail["required_role"] == "automation"
    assert detail["role"] == "user"


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
    base = dict(
        hplcms_users="", hte_users="", hplcms_admins="Service-Account",
        consumable_ack_file="", lc_fault_ack_file="",
    )
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
    # Default settings make the owner an 'automation' user, not a service role.
    client = _authed_client(_load("signals_ready.json"), runner=runner, owner="HTE-User")
    r = client.post("/control/service/start")
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "role_forbidden"
    assert detail["required_role"] == "service"
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


def test_allowed_actions_keeps_run_submit_during_a_technician_run():
    # §6.2: the list must mirror what the endpoints would honour. The enqueue
    # routes now accept during auto-detected servicing, so run.submit must be
    # advertised; the two verbs that take the instrument must not be.
    runner = FakeRunner(busy=False, servicing=True)
    client = _client(_load("signals_ready.json"), runner=runner)
    body = client.get("/status").json()
    actions = body["allowed_actions"]
    assert "run.submit" in actions
    assert "instrument.standby" not in actions
    assert "workflow.start" not in actions
    assert "run.abort" in actions and "queue.cancel" in actions
    assert body["details"]["servicing"] is True


def test_allowed_actions_service_mode_drops_enqueue_verbs():
    runner = FakeRunner(busy=False)
    runner.set_service_mode(True)
    client = _client(_load("signals_ready.json"), runner=runner)
    actions = client.get("/status").json()["allowed_actions"]
    assert "run.submit" not in actions
    assert "instrument.standby" not in actions
    assert "workflow.start" not in actions


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


# A fresh STAT? ERROR flag on an LC module, with OLSS not in an active state, so
# the module state reconciles to "error" (not forced "busy" by an acquisition).
_MODULE_FAULT_SIGNALS = {
    "module_column_thermostat_state": "error",
    "module_column_thermostat_stat_flags": ["ERROR"],
    "module_column_thermostat_stat_age_s": 12,
    "module_multisampler_state": "error",
    "module_multisampler_stat_flags": ["ERROR"],
    "module_multisampler_stat_age_s": 12,
}


def test_subsystem_fault_degrades_status_and_drops_enqueue_verbs():
    """§2.2: a device MUST NOT report `ready` while an LC module reports error.
    Two errored modules → top-level `degraded`, enqueue verbs dropped, abort /
    cancel retained."""
    runner = FakeRunner(busy=False)
    signals = {**_load("signals_ready.json"), **_MODULE_FAULT_SIGNALS}
    client = _client(signals, runner=runner)
    body = client.get("/status").json()

    assert body["equipment_status"] == "degraded"
    assert body["components"]["column_thermostat"]["state"] == "error"
    assert body["components"]["multisampler"]["state"] == "error"
    assert set(body["details"]["subsystem_fault_modules"]) == {
        "column_thermostat", "multisampler",
    }
    assert "check_column_thermostat" in body["required_actions"]
    assert "check_multisampler" in body["required_actions"]

    actions = body["allowed_actions"]
    assert "run.submit" not in actions
    assert "instrument.standby" not in actions
    assert "workflow.start" not in actions
    assert "run.abort" in actions and "queue.cancel" in actions


def test_subsystem_fault_refuses_submit_409():
    """§6.2 invariant, end-to-end: a module fault drops run.submit from
    allowed_actions AND POSTing it 409s subsystem_fault (fail-closed)."""
    signals = {**_load("signals_ready.json"), **_MODULE_FAULT_SIGNALS}
    client = _authed_client(signals)

    r = client.post("/control/run", json=VALID_RUN_BODY)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "subsystem_fault"
    assert "multisampler" in detail["faulted_modules"]

    # standby and the queue endpoint share the gate.
    assert client.post("/control/standby").status_code == 409
    assert client.post("/control/queue", json=VALID_RUN_BODY).status_code == 409


def test_subsystem_fault_does_not_override_busy():
    """A fault mid-acquisition leaves the top-level `busy` (not `degraded`), but
    still gates enqueue verbs so nothing new launches into faulted hardware."""
    runner = FakeRunner(busy=False)
    signals = {
        **_load("signals_ready.json"),
        **_MODULE_FAULT_SIGNALS,
        "acquisition_active": True,
    }
    client = _client(signals, runner=runner)
    body = client.get("/status").json()

    assert body["equipment_status"] == "busy"
    assert "run.submit" not in body["allowed_actions"]


def test_allowed_actions_helper_matches_refusal_property():
    """Unit-level §6.2 property: ``verb in allowed_actions`` iff the matching POST
    would NOT refuse, across every combination of the gating conditions.

    The two groups part company on servicing. An enqueue is accepted and held
    while a technician run is auto-detected, so ``run.submit`` stays offered;
    the verbs that take the instrument now do not."""
    from agilent_hplcms_server.control.actions import allowed_actions

    import itertools

    for (
        requires_init,
        queue_full,
        servicing,
        service_mode,
        workflow_active,
        subsystem_fault,
    ) in itertools.product((False, True), repeat=6):
        # The explicit toggle is one of the two sources of servicing, so
        # service_mode without servicing is not a state the runner can report.
        if service_mode and not servicing:
            continue
        actions = allowed_actions(
            service_operational=True,
            requires_init=requires_init,
            queue_full=queue_full,
            servicing=servicing,
            service_mode=service_mode,
            workflow_active=workflow_active,
            subsystem_fault=subsystem_fault,
        )
        can_enqueue = (
            (not requires_init)
            and (not queue_full)
            and (not service_mode)
            and (not subsystem_fault)
        )
        can_take_instrument = can_enqueue and (not servicing)
        assert ("run.submit" in actions) is can_enqueue
        assert ("instrument.standby" in actions) is can_take_instrument
        assert ("workflow.start" in actions) is (
            can_take_instrument and not workflow_active
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
# sample_position pass-through, plate geometry, robot reservation
# ---------------------------------------------------------------------------

def test_run_forwards_sample_position_verbatim():
    """The device forwards sample_position to Moses unchanged; the sidecar-only
    fields plate_format/submitter are not forwarded."""
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "cpd_01", "sample_position": "D1F-A1", "injection_volume": 2.0},
        {"sample_name": "cpd_02", "sample_position": "D4B-H12", "injection_volume": 1.0},
    ], "submitter": "robot"}  # robot so the D1F (reserved) sample is allowed
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text
    job = runner.submitted[0]["job"]
    assert job["samples"][0]["sample_position"] == "D1F-A1"
    assert job["samples"][1]["sample_position"] == "D4B-H12"
    assert set(job["samples"][0]) == {"sample_name", "sample_position", "injection_volume"}
    assert "plate_format" not in job
    assert "submitter" not in job


def test_run_422_malformed_sample_position():
    """A sample_position that isn't D#X-Y1 is rejected at model validation (422)."""
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    for pos in ("A1", "D5B-A1", "front-A1", "D1X-A1", "D1B_A1", ""):
        bad = {**VALID_RUN_BODY, "samples": [
            {"sample_name": "x", "sample_position": pos, "injection_volume": 2.0}
        ]}
        r = client.post("/control/run", json=bad)
        assert r.status_code == 422, f"{pos!r} should be rejected, got {r.status_code}"


def test_run_422_well_off_plate_for_format():
    """Wells are validated against plate_format: A13/I1 are off a 96-well plate."""
    runner = FakeRunner(busy=False)
    client = _client(_load("signals_ready.json"), runner=runner)
    for well in ("A13", "I1"):
        bad = {**VALID_RUN_BODY, "samples": [
            {"sample_name": "x", "sample_position": f"D1F-{well}", "injection_volume": 2.0}
        ]}
        r = client.post("/control/run", json=bad)
        assert r.status_code == 422, f"{well!r} should be off a 96-well plate, got {r.status_code}"


def test_run_384_well_plate_accepts_high_wells():
    """A 384-well plate accepts P24, which is off a 96-well plate."""
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {**VALID_RUN_BODY, "plate_format": "384-well", "submitter": "robot", "samples": [
        {"sample_name": "x", "sample_position": "D4B-P24", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text
    assert runner.submitted[0]["job"]["samples"][0]["sample_position"] == "D4B-P24"


def test_run_412_reserved_drawer_for_manual_submitter():
    """A manual run targeting the robot-reserved drawer (default 'D1F') is refused 412."""
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {**VALID_RUN_BODY, "samples": [
        {"sample_name": "x", "sample_position": "D1F-A1", "injection_volume": 2.0}
    ]}  # submitter defaults to "manual"
    r = client.post("/control/run", json=body)
    assert r.status_code == 412, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "reserved_for_robot"
    assert detail["reserved_drawer"] == "D1F"
    assert runner.submitted == []  # never enqueued


def test_run_robot_submitter_allowed_on_reserved_drawer():
    """submitter='robot' bypasses the reservation."""
    runner = FakeRunner(busy=False)
    client = _authed_client(_load("signals_ready.json"), runner=runner)
    body = {**VALID_RUN_BODY, "submitter": "robot", "samples": [
        {"sample_name": "x", "sample_position": "D1F-A1", "injection_volume": 2.0}
    ]}
    r = client.post("/control/run", json=body)
    assert r.status_code == 202, r.text


# ---------------------------------------------------------------------------
# Consumable acknowledgments (waste emptied / solvent refilled)
# ---------------------------------------------------------------------------


def test_waste_reset_suppresses_empty_waste_bottle():
    signals = {
        **_load("signals_ready.json"),
        "waste_volume_ml": 1908.0, "waste_capacity_ml": 2000.0,
        "waste_near_capacity": True,
    }
    client = _authed_client(signals)
    body = client.get("/status").json()
    assert "empty_waste_bottle" in body["required_actions"]
    assert body["details"].get("waste_near_capacity") is True

    r = client.post("/control/consumables/waste/reset")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["consumable"] == "waste"
    assert j["raw_at_ack_ml"] == 1908.0
    assert j["warning_suppressed"] is True

    body2 = client.get("/status").json()
    assert "empty_waste_bottle" not in body2["required_actions"]
    assert "waste_near_capacity" not in body2["details"]
    assert body2["details"]["waste_reset_at"]


def test_waste_warning_rearms_after_more_waste():
    signals = {
        **_load("signals_ready.json"),
        "waste_volume_ml": 1500.0, "waste_capacity_ml": 3000.0,
        "waste_near_capacity": True,
    }
    client = _authed_client(signals)
    client.post("/control/consumables/waste/reset")  # ack at 1500
    assert "empty_waste_bottle" not in client.get("/status").json()["required_actions"]

    # New waste accrues past ack + rearm delta (200) → warning re-arms.
    signals["waste_volume_ml"] = 1750.0
    assert "empty_waste_bottle" in client.get("/status").json()["required_actions"]


def test_solvent_reset_suppresses_low_warning():
    signals = {
        **_load("signals_ready.json"),
        "solvent_a1_volume_ml": 80.0, "solvent_a1_low": True,
    }
    client = _authed_client(signals)
    assert "refill_solvent_a1" in client.get("/status").json()["required_actions"]

    r = client.post("/control/consumables/solvent/a1/reset")
    assert r.status_code == 200, r.text
    assert r.json()["consumable"] == "a1"

    body = client.get("/status").json()
    assert "refill_solvent_a1" not in body["required_actions"]
    assert "solvent_a1_low" not in body["details"]
    assert body["details"]["solvent_a1_reset_at"]


def test_solvent_reset_unknown_slot_404():
    client = _authed_client(_load("signals_ready.json"))
    assert client.post("/control/consumables/solvent/z9/reset").status_code == 404


def test_consumable_reset_requires_claim():
    client = _client(_load("signals_ready.json"))  # no X-Claim-Token
    assert client.post("/control/consumables/waste/reset").status_code == 423
    assert client.post("/control/consumables/solvent/a1/reset").status_code == 423


# ---------------------------------------------------------------------------
# LC module fault acknowledgments (POST/DELETE /control/faults/{role}/ack).
#
# The exit the fault channel was missing. Agilent's driver never logs a
# fault-cleared line and a module's STAT? — the one recovery signal the probe can
# read — is only written at prerun, so a module fixed while the instrument sits
# idle stays faulted for the whole LC_FAULT_WINDOW_S with run.submit refused
# behind it. Service-role gated: acknowledging asserts something about the
# physical instrument and releases a safety interlock.
# ---------------------------------------------------------------------------

# The multisampler mid-abort: a critical fault plus the stale STAT? that latched
# ERROR with it. Both channels must go quiet or the module card stays red.
_ACK_FAULT_AT = "2026-08-20T20:53:47.142000-04:00"
_ACK_STAT_AT = "2026-08-20T20:53:47.584000"
_NEEDLE_FAULT_SIGNALS = {
    "lc_faults": [
        {
            "module_code": "G7167B",
            "role": "multisampler",
            "serial": "DEBAS04772",
            "message": "Needle command failed",
            "code": "25022",
            "severity": "critical",
            "timestamp": _ACK_FAULT_AT,
            "age_s": 900.0,
        },
        {
            "module_code": "G7120A",
            "role": "binary_pump",
            "serial": "DEBA201988",
            "message": "Analysis aborted by another module",
            "code": None,
            "severity": "error",
            "timestamp": _ACK_FAULT_AT,
            "age_s": 900.0,
        },
    ],
    "lc_fault_active": True,
    "lc_fault_severity": "critical",
    "lc_fault_message": "G7167B multisampler: Needle command failed",
    "lc_fault_module_roles": ["binary_pump", "multisampler"],
    "module_multisampler_state": "error",
    "module_multisampler_stat_flags": ["PRERUN", "ERROR", "NOT_READY"],
    "module_multisampler_stat_age_s": 900.0,
    "module_multisampler_stat_at": _ACK_STAT_AT,
    "module_binary_pump_state": "not_ready",
    "module_binary_pump_stat_flags": ["PRERUN", "NO_ERROR", "NOT_READY"],
    "module_binary_pump_stat_age_s": 900.0,
    "module_binary_pump_stat_at": _ACK_STAT_AT,
}


def _faulted_signals() -> dict:
    return {**_load("signals_ready.json"), **_NEEDLE_FAULT_SIGNALS}


def _tech_client(runner: MosesRunner | None = None) -> TestClient:
    """A client claiming as the service account, which may acknowledge faults."""
    return _authed_client(
        _faulted_signals(),
        runner=runner,
        owner="Service-Account",
        settings=_admin_settings(),
    )


def test_fault_ack_requires_a_claim():
    client = _client(_faulted_signals())  # no X-Claim-Token

    assert client.post("/control/faults/multisampler/ack").status_code == 423


def test_fault_ack_requires_the_service_role():
    """An hte account may submit runs and refill bottles, but not vouch for
    hardware: clearing this interlock is the service account's call."""
    client = _authed_client(_faulted_signals())  # default roster → automation

    r = client.post("/control/faults/multisampler/ack")
    assert r.status_code == 403
    assert r.json()["detail"]["required_role"] == "service"


def test_fault_ack_unknown_module_is_404():
    client = _tech_client()

    assert client.post("/control/faults/laser_cannon/ack").status_code == 404


def test_fault_ack_clears_the_module_and_reopens_submission():
    """End-to-end: the evening of 2026-08-20, but with a technician on site."""
    client = _tech_client(runner=FakeRunner(busy=False))

    before = client.get("/status").json()
    assert before["equipment_status"] == "error"
    assert "run.submit" not in before["allowed_actions"]
    assert client.post("/control/run", json=VALID_RUN_BODY).status_code == 409

    r = client.post(
        "/control/faults/multisampler/ack", params={"note": "needle checked, D2F reseated"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["module"] == "multisampler"
    assert body["fault_cleared"] is True
    assert body["faults_through"] is not None
    assert body["stat_through"] is not None

    # The pump's cascade fault is untouched, so the instrument is not yet clear.
    assert body["faulted_modules"] == ["binary_pump"]

    after = client.get("/status").json()
    assert after["components"]["multisampler"]["state"] == "not_ready"
    assert "multisampler" in after["details"]["fault_acks"]
    assert after["details"]["subsystem_fault_modules"] == ["binary_pump"]

    # Acking the cascade too clears the instrument and lets a run through.
    assert client.post("/control/faults/binary_pump/ack").json()["faulted_modules"] == []
    final = client.get("/status").json()
    assert final["equipment_status"] == "ready"
    assert "run.submit" in final["allowed_actions"]
    assert client.post("/control/run", json=VALID_RUN_BODY).status_code in (200, 202)


def test_withdrawing_the_ack_restores_the_refusal():
    client = _tech_client(runner=FakeRunner(busy=False))
    client.post("/control/faults/multisampler/ack")
    client.post("/control/faults/binary_pump/ack")
    assert client.get("/status").json()["equipment_status"] == "ready"

    r = client.delete("/control/faults/multisampler/ack")
    assert r.status_code == 200, r.text
    assert r.json()["fault_cleared"] is False
    assert r.json()["faulted_modules"] == ["multisampler"]

    assert client.get("/status").json()["equipment_status"] == "error"
    assert client.post("/control/run", json=VALID_RUN_BODY).status_code == 409

    # Withdrawing a second time has nothing left to withdraw.
    assert client.delete("/control/faults/multisampler/ack").status_code == 404
