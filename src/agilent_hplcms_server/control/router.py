"""FastAPI router for /control/* endpoints (Layer 2 device state machine)."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, Response

from ..config import Settings, load_settings
from .claims import ClaimConflict, ClaimHolder
from .models import (
    AbortResponse,
    CancelResponse,
    ClaimRejection,
    ClaimRequest,
    ClaimResponse,
    EquipmentBusyError,
    HeartbeatResponse,
    QueueFullError,
    QueueResponse,
    QueueStatusResponse,
    ReservedForRobotError,
    QueuedRun,
    RequiresInitError,
    RunRequest,
    RunResponse,
    StandbyResponse,
    StartupResponse,
)
from .runner import JobEntry, MosesRunner

router = APIRouter(prefix="/control", tags=["control"])


def _get_runner(request: Request) -> MosesRunner:
    return request.app.state.runner  # type: ignore[no-any-return]


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _get_claims(request: Request) -> ClaimHolder:
    return request.app.state.claims  # type: ignore[no-any-return]


def _require_claim(request: Request) -> None:
    """Hard claim enforcement (§5): mutating /control/* requires a valid
    ``X-Claim-Token`` matching the live claim. Missing or stale → 423 Locked,
    body carries the current holder so the caller can back off / retry."""
    claims = _get_claims(request)
    token = request.headers.get("x-claim-token")
    if claims.validate(token):
        return
    held = claims.current()
    raise HTTPException(
        status_code=423,
        detail=ClaimRejection(
            detail=(
                "A valid X-Claim-Token is required for this action. Acquire one "
                "via POST /control/claim."
            ),
            claimed_by=held,
            retry_after_s=None,
        ).model_dump(mode="json"),
    )


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
    # "dispatched" is internal: script started but OpenLab not yet confirmed.
    # Show as "pending" to the client. All other statuses pass through as-is,
    # including "enqueued" (submitted to OpenLab queue, awaiting its turn).
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


def _compose_moses_job(body: RunRequest, settings: Settings) -> dict:
    """Build the Moses job dict from a RunRequest.

    Each sample's logical ``{tray, well}`` is composed into the Agilent
    multisampler ``{drawer}-{well}`` address Moses consumes, using the
    tray→drawer mapping in settings (e.g. tray "rear" + well "A1" → "D4B-A1").
    ``plate_format`` and ``submitter`` are sidecar-side concerns (well-geometry
    validation and the robot-tray reservation) and are not forwarded to Moses.
    """
    tray_to_drawer = {
        "front": settings.tray_front_drawer,
        "rear": settings.tray_rear_drawer,
    }
    job = body.model_dump(exclude={"script_name", "samples", "plate_format", "submitter"})
    job["samples"] = [
        {
            "sample_name": s.sample_name,
            "sample_position": f"{tray_to_drawer[s.tray]}-{s.well}",
            "injection_volume": s.injection_volume,
        }
        for s in body.samples
    ]
    return job


def _check_reserved_tray(body: RunRequest, settings: Settings) -> None:
    """Refuse a non-robot run that targets the robot-reserved tray (412).

    The reservation is a soft interlock so an agent and the robot don't fight
    over the same tray; ``submitter="robot"`` bypasses it, and an empty
    ``RESERVED_ROBOT_TRAY`` disables it. Raised before enqueue so the job never
    reaches Moses. Like queue_full this is a precondition refusal — 412, no
    ``last_error`` (§6.3).
    """
    reserved = settings.reserved_robot_tray
    if not reserved or body.submitter == "robot":
        return
    if any(s.tray == reserved for s in body.samples):
        raise HTTPException(
            status_code=412,
            detail=ReservedForRobotError(
                detail=(
                    f"Tray {reserved!r} is reserved for robotic submission; "
                    "set submitter='robot' to target it."
                ),
                reserved_tray=reserved,
            ).model_dump(mode="json"),
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
        # §6.1: queue-full is a *precondition* refusal (the request becomes
        # valid once the queue drains), so 412 — not 409 — with a Retry-After.
        # 412 refusals MUST NOT touch last_error (§6.3); we only raise here.
        retry_after_s = float(settings.queue_full_retry_after_s)
        raise HTTPException(
            status_code=412,
            detail=QueueFullError(
                detail=str(exc),
                max_depth=settings.queue_max_depth,
                current_depth=runner.queue_depth(),
                retry_after_s=retry_after_s,
            ).model_dump(mode="json"),
            headers={"Retry-After": str(int(retry_after_s))},
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
    responses={
        409: {"model": RequiresInitError},
        412: {"model": QueueFullError | ReservedForRobotError},
        423: {"model": ClaimRejection},
    },
)
def submit_run(body: RunRequest, request: Request) -> RunResponse:
    """Submit a batch run.

    - Idle instrument → starts immediately (``status: accepted``).
    - Busy instrument → FIFO queue (``status: queued``, ``queue_position`` is 1-based).
    - HTTP 409 ``requires_init`` if any OpenLab core process is missing.
    - HTTP 412 ``queue_full`` if the queue is at max depth (with ``Retry-After``).
    - HTTP 412 ``reserved_for_robot`` if a non-robot run targets the reserved tray.
    - HTTP 422 if any parameter is out of hardware range or a ``well`` is off-plate.
    - HTTP 423 if the ``X-Claim-Token`` is missing or stale.
    """
    _require_claim(request)
    runner = _get_runner(request)
    settings = _get_settings(request)

    signals = _read_signals(request)
    _notify_runner_from_signals(runner, signals)
    runner.poll(settings=settings)
    _check_requires_init(signals)
    _check_reserved_tray(body, settings)

    job = _compose_moses_job(body, settings)
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
    responses={
        409: {"model": RequiresInitError},
        412: {"model": QueueFullError | ReservedForRobotError},
        423: {"model": ClaimRejection},
    },
)
def post_to_queue(body: RunRequest, request: Request) -> QueueResponse:
    """Submit a run to the queue and get back a ``queue_id`` to track it.

    Use ``GET /control/queue`` to check status and ``DELETE /control/queue/{queue_id}``
    to cancel before it starts. ``position`` is 0 if started immediately, 1-based otherwise.
    """
    _require_claim(request)
    runner = _get_runner(request)
    settings = _get_settings(request)

    signals = _read_signals(request)
    _notify_runner_from_signals(runner, signals)
    runner.poll(settings=settings)
    _check_requires_init(signals)
    _check_reserved_tray(body, settings)

    job = _compose_moses_job(body, settings)
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
    responses={404: {}, 409: {}, 423: {"model": ClaimRejection}},
)
def cancel_queue_entry(queue_id: str, request: Request) -> CancelResponse:
    """Remove a pending job from the queue.

    - HTTP 404 if the job is not found or already completed.
    - HTTP 409 if the job is currently running (use ``POST /control/abort`` instead).
    - HTTP 423 if the ``X-Claim-Token`` is missing or stale.
    """
    _require_claim(request)
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


@router.post(
    "/abort",
    response_model=AbortResponse,
    summary="Abort active run and clear queue",
    responses={423: {"model": ClaimRejection}},
)
def abort_run(request: Request) -> AbortResponse:
    """Abort the active Moses process and clear all pending queued runs.

    HTTP 423 if the ``X-Claim-Token`` is missing or stale.
    """
    _require_claim(request)
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
    "/standby",
    response_model=StandbyResponse,
    status_code=202,
    summary="Park the instrument in low-flow standby",
    responses={
        409: {"model": RequiresInitError},
        412: {"model": QueueFullError},
        423: {"model": ClaimRejection},
    },
)
def standby(request: Request) -> StandbyResponse:
    """Submit a standby-only Moses job (low-flow park). Queues behind any active run.

    This is NOT a full instrument shutdown — powering the UPLC-MS down is a
    deliberate, manual operator procedure done at the instrument, not via the API.

    HTTP 423 if the ``X-Claim-Token`` is missing or stale.
    """
    _require_claim(request)
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
            "name": "standby_park",
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
        "_type": "standby",
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
        return StandbyResponse(
            run_id=run_id,
            status="accepted",
            message=f"Standby job started. Moses PID {active.pid if active else '?'}.",
            queue_position=None,
        )
    return StandbyResponse(
        run_id=run_id,
        status="queued",
        message=f"Standby job queued at position {position}. Will run after current jobs finish.",
        queue_position=position,
    )


# ---------------------------------------------------------------------------
# v1.1 claim protocol (STATUS_SPEC §5). These endpoints are NOT claim-enforced:
# /claim issues the token; /heartbeat and /release authenticate via the token
# in the X-Claim-Token header through their own logic.
# ---------------------------------------------------------------------------


@router.post(
    "/claim",
    response_model=ClaimResponse,
    summary="Acquire the instrument claim",
    responses={409: {"model": ClaimRejection}},
)
def claim(body: ClaimRequest, request: Request) -> ClaimResponse:
    """Acquire (or idempotently re-acquire) the single instrument claim.

    - Free / expired slot, or same ``session_id`` → HTTP 200 with a token.
    - Held by a different live session → HTTP 409 Conflict with ``claimed_by``.
    """
    claims = _get_claims(request)
    try:
        grant = claims.claim(owner=body.owner, session_id=body.session_id, ttl_s=body.ttl_s)
    except ClaimConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=ClaimRejection(
                detail="Instrument is already claimed by another session.",
                claimed_by=exc.held_by,
                retry_after_s=None,
            ).model_dump(mode="json"),
        ) from exc
    return ClaimResponse(
        claim_token=grant.claim_token,
        heartbeat_interval_s=grant.heartbeat_interval_s,
        expires_at=grant.expires_at,
    )


@router.post(
    "/heartbeat",
    summary="Refresh the claim TTL",
    responses={204: {}, 200: {"model": HeartbeatResponse}, 401: {"model": ClaimRejection}},
)
def heartbeat(
    request: Request,
    response: Response,
    x_claim_token: str | None = Header(default=None),
):
    """Extend the live claim's TTL.

    - Valid token → HTTP 204 No Content (TTL extended).
    - Unknown / expired / wrong-session token → HTTP 401; the client MUST treat
      its claim as lost.
    """
    claims = _get_claims(request)
    try:
        claims.heartbeat(x_claim_token or "")
    except KeyError as exc:
        raise HTTPException(
            status_code=401,
            detail=ClaimRejection(
                detail="Claim token is unknown, expired, or belongs to another session.",
                claimed_by=claims.current(),
                retry_after_s=None,
            ).model_dump(mode="json"),
        ) from exc
    response.status_code = 204
    return response


@router.post(
    "/release",
    status_code=204,
    summary="Release the claim (idempotent)",
)
def release(
    request: Request,
    response: Response,
    x_claim_token: str | None = Header(default=None),
):
    """Release the claim. Idempotent (§5): releasing an unknown / already-released
    token also returns HTTP 204 so a client can always move on."""
    claims = _get_claims(request)
    claims.release(x_claim_token)
    response.status_code = 204
    return response
