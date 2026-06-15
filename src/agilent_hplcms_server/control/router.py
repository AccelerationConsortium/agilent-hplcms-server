"""FastAPI router for /control/* endpoints (Layer 2 device state machine)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..config import Settings, load_settings
from .models import (
    AbortResponse,
    EquipmentBusyError,
    RequiresInitError,
    RunRequest,
    RunResponse,
    ShutdownResponse,
    StartupResponse,
)
from .runner import MosesRunner

router = APIRouter(prefix="/control", tags=["control"])


def _get_runner(request: Request) -> MosesRunner:
    return request.app.state.runner  # type: ignore[no-any-return]


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _read_signals(request: Request) -> dict:
    settings = _get_settings(request)
    reader = request.app.state.reader
    return reader(settings)


def _missing_openlab_processes(signals: dict) -> list[str]:
    missing = []
    if not signals.get("openlab_acquisition_alive"):
        missing.append("AcquisitionServer")
    if not signals.get("openlab_instrument_service_alive"):
        missing.append("AcqInstrumentService")
    if not signals.get("openlab_reverse_proxy_alive"):
        missing.append("OpenLabReverseProxy")
    return missing


@router.post("/startup", response_model=StartupResponse, summary="Check instrument readiness")
def startup(request: Request) -> StartupResponse:
    """Read-only readiness check.

    Returns ``ready`` when all required OpenLab CDS supervisor processes are
    detected. Returns ``requires_init`` otherwise. This endpoint never starts
    OpenLab — that is a manual operator action.
    """
    settings = _get_settings(request)
    signals = _read_signals(request)
    missing = _missing_openlab_processes(signals)
    if missing:
        return StartupResponse(
            status="requires_init",
            message="OpenLab core supervisor processes not detected: " + ", ".join(missing),
            missing_processes=missing,
        )
    return StartupResponse(status="ready", message="OpenLab supervisor processes running.")


@router.post(
    "/run",
    response_model=RunResponse,
    status_code=202,
    summary="Submit a new HPLC-MS run",
    responses={
        409: {"model": EquipmentBusyError | RequiresInitError},
    },
)
def submit_run(body: RunRequest, request: Request) -> RunResponse:
    """Submit a batch run to Moses.

    Safety checks (Layer 2):
    - HTTP 409 ``requires_init`` if any OpenLab core process is missing.
    - HTTP 409 ``equipment_busy`` if a Moses run is already active or the
      probe detects active acquisition / moses process.
    - HTTP 422 (Pydantic) if any parameter is out of hardware range (Layer 1).
    """
    runner = _get_runner(request)
    settings = _get_settings(request)

    runner.poll()

    signals = _read_signals(request)
    missing = _missing_openlab_processes(signals)
    if missing:
        raise HTTPException(
            status_code=409,
            detail=RequiresInitError(
                message="OpenLab core supervisor processes not detected: " + ", ".join(missing),
                required_actions=["start_openlab"],
            ).model_dump(),
        )

    if runner.is_busy() or signals.get("acquisition_active") or signals.get("moses_process_alive"):
        active = runner.get_active()
        raise HTTPException(
            status_code=409,
            detail=EquipmentBusyError(
                message="Instrument is busy. Abort the current run before submitting a new one.",
                run_id=active.run_id if active else None,
            ).model_dump(),
        )

    job = body.model_dump(exclude={"script_name"})

    try:
        active_run = runner.submit(
            script_name=body.script_name,
            job=job,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return RunResponse(
        run_id=active_run.run_id,
        status="accepted",
        message=f"Run accepted. Moses PID {active_run.pid}.",
        pid=active_run.pid,
        started_at=active_run.started_at,
    )


@router.post("/abort", response_model=AbortResponse, summary="Abort the active run")
def abort_run(request: Request) -> AbortResponse:
    """Abort the currently running Moses process.

    Returns ``not_running`` (HTTP 200) if no run is active — this is not an
    error; it is idempotent.
    """
    runner = _get_runner(request)
    runner.poll()

    active = runner.get_active()
    if active is None:
        return AbortResponse(status="not_running", message="No active run to abort.", run_id=None)

    run_id = active.run_id
    runner.abort()
    return AbortResponse(
        status="aborted",
        message=f"Run {run_id} aborted.",
        run_id=run_id,
    )


@router.post(
    "/shutdown",
    response_model=ShutdownResponse,
    status_code=202,
    summary="Park the instrument in low-flow standby",
    responses={409: {"model": EquipmentBusyError}},
)
def shutdown(request: Request) -> ShutdownResponse:
    """Submit a standby-only Moses job to park the instrument safely.

    Equivalent to calling ``agent_agilent.py`` with ``samples=[]`` and
    ``standby_after=True``. Returns 409 if a run is already active.
    """
    runner = _get_runner(request)
    settings = _get_settings(request)

    runner.poll()

    if runner.is_busy():
        active = runner.get_active()
        raise HTTPException(
            status_code=409,
            detail=EquipmentBusyError(
                message="Cannot shut down while a run is active. Abort first.",
                run_id=active.run_id if active else None,
            ).model_dump(),
        )

    standby_job = {
        "instrument_config_path": "examples/hh_472_config.json",
        "output_dir": str(settings.cds_results_dir) + "/standby",
        "ms_mode": "positive_negative",
        "standby_after": True,
        "gradient": {
            "name": "shutdown_standby",
            "solvent_a": "H2O_0.1%FA",
            "solvent_b": "ACN_0.1%FA",
            "run_time": 1.0,
            "flow_rate": 0.01,
            "gradient_table": [[0.0, 0.5], [1.0, 0.5]],
            "equilibration_time": 0.0,
        },
        "samples": [],
    }

    try:
        active_run = runner.submit(
            script_name="examples/agent_agilent.py",
            job=standby_job,
            settings=settings,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ShutdownResponse(
        run_id=active_run.run_id,
        status="accepted",
        message=f"Standby job submitted. Moses PID {active_run.pid}.",
    )
