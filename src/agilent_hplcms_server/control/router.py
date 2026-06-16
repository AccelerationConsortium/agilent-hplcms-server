"""FastAPI router for /control/* endpoints (Layer 2 device state machine)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..config import Settings, load_settings
from .models import (
    AbortResponse,
    CancelResponse,
    EquipmentBusyError,
    QueueFullError,
    QueueResponse,
    QueueStatusResponse,
    QueuedRun,
    RequiresInitError,
    RunRequest,
    RunResponse,
    ShutdownResponse,
    StartupResponse,
)
from .runner import JobEntry, MosesRunner

router = APIRouter(prefix="/control", tags=["control"])


def _get_runner(request: Request) -> MosesRunner:
    return request.app.state.runner  # type: ignore[no-any-return]


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _read_signals(request: Request) -> dict:
    settings = _get_settings(request)
    reader = request.app.state.reader
    return reader(settings)


def _notify_runner_from_signals(runner: MosesRunner, signals: dict) -> None:
    runner.notify_olss_state(
        signals.get("olss_instrument_state"),
        signals.get("olss_software_status"),
    )


def _missing_openlab_processes(signals: dict) -> list[str]:
    missing = []
    if not signals.get("openlab_acquisition_alive"):
        missing.append("AcquisitionServer")
    if not signals.get("openlab_instrument_service_alive"):
        missing.append("AcqInstrumentService")
    if not signals.get("openlab_reverse_proxy_alive"):
        missing.append("OpenLabReverseProxy")
    return missing


def _entry_to_queued_run(entry: JobEntry) -> QueuedRun:
    # "dispatched" is internal: script started but OpenLab not yet confirming.
    # Show as "pending" to the client until acquisition is confirmed.
    api_status = entry.status if entry.status != "dispatched" else "pending"
    return QueuedRun(
        queue_id=entry.queue_id,
        request=entry.request_dict,
        queued_at=entry.queued_at,
        status=api_status,  # type: ignore[arg-type]
        started_at=entry.started_at,
        finished_at=entry.finished_at,
        pid=entry.pid,
    )


def _check_requires_init(signals: dict) -> None:
    missing = _missing_openlab_processes(signals)
    if missing:
        raise HTTPException(
            status_code=409,
            detail=RequiresInitError(
                message="OpenLab core supervisor processes not detected: " + ", ".join(missing),
                required_actions=["start_openlab"],
            ).model_dump(),
        )


def _do_enqueue(
    script_name: str,
    job: dict,
    request_dict: dict,
    runner: MosesRunner,
    settings: Settings,
) -> tuple[str, int]:
    """Submit to the queue; translates runner exceptions to HTTP errors."""
    try:
        return runner.submit_to_queue(
            script_name=script_name,
            job=job,
            request_dict=request_dict,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(
            status_code=409,
            detail=QueueFullError(
                message=str(exc),
                max_depth=settings.queue_max_depth,
                current_depth=runner.queue_depth(),
            ).model_dump(),
        ) from exc


@router.post("/startup", response_model=StartupResponse, summary="Check instrument readiness")
def startup(request: Request) -> StartupResponse:
    """Read-only readiness check. Never starts OpenLab — that is a manual operator action."""
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
    summary="Quick-submit a run (starts immediately if idle, queues if busy)",
    responses={409: {"model": RequiresInitError | QueueFullError}},
)
def submit_run(body: RunRequest, request: Request) -> RunResponse:
    """Submit a batch run.

    - Idle instrument → starts immediately (``status: accepted``).
    - Busy instrument → FIFO queue (``status: queued``, ``queue_position`` is 1-based).
    - HTTP 409 ``requires_init`` if any OpenLab core process is missing.
    - HTTP 409 ``queue_full`` if the queue is at max depth.
    - HTTP 422 if any parameter is out of hardware range (Layer 1).
    """
    runner = _get_runner(request)
    settings = _get_settings(request)

    signals = _read_signals(request)
    _notify_runner_from_signals(runner, signals)
    runner.poll(settings=settings)
    _check_requires_init(signals)

    job = body.model_dump(exclude={"script_name"})
    run_id, position = _do_enqueue(
        script_name=body.script_name,
        job=job,
        request_dict=body.model_dump(),
        runner=runner,
        settings=settings,
    )

    active = runner.get_active()
    if position == 0:
        return RunResponse(
            run_id=run_id,
            status="accepted",
            message=f"Run started immediately. Moses PID {active.pid if active else '?'}.",
            pid=active.pid if active else None,
            started_at=active.started_at if active else None,
            queue_position=None,
        )
    return RunResponse(
        run_id=run_id,
        status="queued",
        message=f"Run queued at position {position}. Will start when current run finishes.",
        pid=None,
        started_at=None,
        queue_position=position,
    )


@router.post(
    "/queue",
    response_model=QueueResponse,
    status_code=202,
    summary="Submit a run to the job queue",
    responses={409: {"model": RequiresInitError | QueueFullError}},
)
def post_to_queue(body: RunRequest, request: Request) -> QueueResponse:
    """Submit a run to the queue and get back a ``queue_id`` to track it.

    Use ``GET /control/queue`` to check status and ``DELETE /control/queue/{queue_id}``
    to cancel before it starts. ``position`` is 0 if started immediately, 1-based otherwise.
    """
    runner = _get_runner(request)
    settings = _get_settings(request)

    signals = _read_signals(request)
    _notify_runner_from_signals(runner, signals)
    runner.poll(settings=settings)
    _check_requires_init(signals)

    job = body.model_dump(exclude={"script_name"})
    queue_id, position = _do_enqueue(
        script_name=body.script_name,
        job=job,
        request_dict=body.model_dump(),
        runner=runner,
        settings=settings,
    )

    if position == 0:
        msg = "Run started immediately — instrument was idle."
    else:
        msg = f"Queued at position {position}. Starts when preceding run(s) finish."

    return QueueResponse(queue_id=queue_id, position=position, status="queued", message=msg)


@router.get(
    "/queue",
    response_model=QueueStatusResponse,
    summary="View current queue status and job history",
)
def get_queue(request: Request) -> QueueStatusResponse:
    """Return all tracked jobs (pending, running, recent done/failed) plus instrument online status."""
    runner = _get_runner(request)
    settings = _get_settings(request)

    signals = _read_signals(request)
    _notify_runner_from_signals(runner, signals)
    runner.poll(settings=settings)

    # Promote dispatched → acquiring only if *this job's* output_dir shows sirslt activity
    # after the job started. Uses scoped check to avoid false positives from concurrent
    # OpenLab jobs writing to other result directories.
    runner.maybe_promote_to_acquiring(settings)

    missing = _missing_openlab_processes(signals)
    instrument_online = len(missing) == 0

    active = runner.get_active()
    all_jobs = runner.get_all_jobs()
    pending_count = runner.queue_depth()
    accepting_jobs = instrument_online and (
        active is None or pending_count < settings.queue_max_depth
    )

    return QueueStatusResponse(
        queue=[_entry_to_queued_run(e) for e in all_jobs],
        active_run_id=active.run_id if active else None,
        pending_count=pending_count,
        max_depth=settings.queue_max_depth,
        instrument_online=instrument_online,
        accepting_jobs=accepting_jobs,
        instrument_state=signals.get("olss_instrument_state"),
    )


@router.delete(
    "/queue/{queue_id}",
    response_model=CancelResponse,
    summary="Cancel a pending job",
    responses={404: {}, 409: {}},
)
def cancel_queue_entry(queue_id: str, request: Request) -> CancelResponse:
    """Remove a pending job from the queue.

    - HTTP 404 if the job is not found or already completed.
    - HTTP 409 if the job is currently running (use ``POST /control/abort`` instead).
    """
    runner = _get_runner(request)
    try:
        runner.cancel_queued(queue_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Job {queue_id!r} not found.")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return CancelResponse(cancelled_id=queue_id, message="Job cancelled and removed from queue.")


@router.post("/abort", response_model=AbortResponse, summary="Abort active run and clear queue")
def abort_run(request: Request) -> AbortResponse:
    """Abort the active Moses process and clear all pending queued runs."""
    runner = _get_runner(request)
    settings = _get_settings(request)

    signals = _read_signals(request)
    _notify_runner_from_signals(runner, signals)
    runner.poll(settings=settings)
    active = runner.get_active()
    run_id = active.run_id if active else None

    was_active, n_cleared = runner.abort(settings=settings)

    if not was_active and n_cleared == 0:
        return AbortResponse(
            status="not_running",
            message="No active run and queue was already empty.",
            run_id=None,
            queue_cleared=0,
        )
    msg_parts = []
    if was_active:
        msg_parts.append(f"Run {run_id} aborted.")
    if n_cleared:
        msg_parts.append(f"{n_cleared} queued run(s) cleared.")
    return AbortResponse(
        status="aborted" if was_active else "not_running",
        message=" ".join(msg_parts),
        run_id=run_id,
        queue_cleared=n_cleared,
    )


@router.post(
    "/shutdown",
    response_model=ShutdownResponse,
    status_code=202,
    summary="Park the instrument in low-flow standby",
    responses={409: {"model": QueueFullError | RequiresInitError}},
)
def shutdown(request: Request) -> ShutdownResponse:
    """Submit a standby-only Moses job. Queues behind any active run."""
    runner = _get_runner(request)
    settings = _get_settings(request)

    signals = _read_signals(request)
    _notify_runner_from_signals(runner, signals)
    runner.poll(settings=settings)
    _check_requires_init(signals)

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
    standby_request_dict = {
        "script_name": "examples/agent_agilent.py",
        **standby_job,
        "_type": "shutdown",
    }

    run_id, position = _do_enqueue(
        script_name="examples/agent_agilent.py",
        job=standby_job,
        request_dict=standby_request_dict,
        runner=runner,
        settings=settings,
    )

    active = runner.get_active()
    if position == 0:
        return ShutdownResponse(
            run_id=run_id,
            status="accepted",
            message=f"Standby job started. Moses PID {active.pid if active else '?'}.",
            queue_position=None,
        )
    return ShutdownResponse(
        run_id=run_id,
        status="queued",
        message=f"Standby job queued at position {position}. Will run after current jobs finish.",
        queue_position=position,
    )
