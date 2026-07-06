"""Pydantic models for the /control/* endpoints (Layer 1 hardware limits)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..models import ClaimedBy


# Autosampler tray + plate geometry. Kept byte-for-byte in sync with the lab
# skill catalog (ac-organic-lab: lab_skills/skill_catalog/hplc.py) so the SDK
# and this device validate sample addresses identically. The device is the
# authoritative validator; the catalog mirrors these definitions.
TrayName = Literal["front", "rear"]
PlateFormat = Literal["96-well", "384-well"]
_PLATE_GEOMETRY: dict[str, tuple[int, int]] = {"96-well": (8, 12), "384-well": (16, 24)}
_WELL_RE = re.compile(r"^([A-Za-z])(\d{1,2})$")


class GradientConfig(BaseModel):
    name: str
    solvent_a: str
    solvent_b: str
    run_time: float = Field(gt=0, le=120.0, description="Total run time in minutes (max 2 h)")
    flow_rate: float = Field(gt=0, le=2.0, description="Flow rate in mL/min")
    gradient_table: list[list[float]] = Field(
        description="[[time_min, fraction_b], ...] where fraction_b is 0.0–1.0"
    )
    equilibration_time: float = Field(default=0.0, ge=0, le=30.0)


class SampleConfig(BaseModel):
    sample_name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_\-]+$",
        description="Alphanumeric identifier (no spaces)",
    )
    tray: TrayName = Field(description="Autosampler tray holding this sample (front or rear).")
    well: str = Field(
        min_length=2,
        max_length=4,
        description='Plate well, e.g. "A1" or "H12". Validated against the run\'s plate_format.',
    )
    injection_volume: float = Field(gt=0, le=20.0, description="Injection volume in µL (max 20)")


class RunRequest(BaseModel):
    script_name: str = Field(
        default="examples/agent_agilent.py",
        description=(
            "Relative path (from MOSES_WORK_DIR) of the Python script to run. "
            "Must be in the MOSES_ALLOWED_SCRIPTS allowlist."
        ),
    )
    instrument_config_path: str = Field(
        default="examples/hh_472_config.json",
        description="Path to instrument config JSON (absolute or relative to MOSES_WORK_DIR).",
    )
    output_dir: str = Field(description="Absolute path on the instrument PC for result files.")
    ms_mode: Literal["positive", "negative", "positive_negative"] = "positive_negative"
    standby_after: bool = True
    gradient: GradientConfig
    samples: list[SampleConfig] = Field(min_length=1, description="At least one sample required.")
    plate_format: PlateFormat = Field(
        default="96-well", description="Plate format for all samples in this run."
    )
    submitter: Literal["manual", "robot"] = Field(
        default="manual",
        description=(
            "Runs targeting a tray reserved for robotic submission (RESERVED_ROBOT_TRAY) "
            "are refused with HTTP 412 reserved_for_robot unless submitter='robot'."
        ),
    )

    @model_validator(mode="after")
    def _validate_wells(self) -> "RunRequest":
        """Reject wells that fall off the plate for the declared plate_format.

        Mirrors the lab skill catalog's geometry check so a request rejected by
        the SDK and one rejected by the device fail for the identical reason.
        """
        rows, cols = _PLATE_GEOMETRY[self.plate_format]
        for s in self.samples:
            m = _WELL_RE.match(s.well)
            if m is None:
                raise ValueError(f"Malformed well {s.well!r} (expected like 'A1', 'H12').")
            row_idx = ord(m.group(1).upper()) - ord("A")
            col = int(m.group(2))
            if not (0 <= row_idx < rows) or not (1 <= col <= cols):
                raise ValueError(
                    f"Well {s.well!r} is out of range for a {self.plate_format} plate."
                )
        return self


class RunResponse(BaseModel):
    """Response for POST /control/run (backward-compat quick-submit)."""

    run_id: str
    status: Literal["accepted", "queued"]
    message: str
    pid: int | None = None
    started_at: datetime | None = None
    queue_position: int | None = None


class QueuedRun(BaseModel):
    """Single job entry returned in GET /control/queue.

    Status is process-exit authoritative (queue-ownership pivot): ``pending``
    (queued), ``running`` (Moses subprocess alive), ``done`` (exit 0),
    ``failed`` (non-zero exit, aborted, or cancelled)."""

    queue_id: str
    request: dict[str, Any]
    queued_at: datetime
    status: Literal["pending", "running", "done", "failed"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pid: int | None = None


class QueueResponse(BaseModel):
    """Response for POST /control/queue."""

    queue_id: str
    position: int
    status: Literal["queued"]
    message: str


class QueueStatusResponse(BaseModel):
    """Response for GET /control/queue."""

    queue: list[QueuedRun]
    active_run_id: str | None
    pending_count: int
    max_depth: int
    instrument_online: bool
    accepting_jobs: bool
    instrument_state: str | None = None


class CancelResponse(BaseModel):
    """Response for DELETE /control/queue/{queue_id}."""

    cancelled_id: str
    message: str


class AbortResponse(BaseModel):
    status: Literal["aborted", "not_running"]
    message: str
    run_id: str | None = None
    queue_cleared: int = 0


class StartupResponse(BaseModel):
    status: Literal["ready", "requires_init"]
    message: str
    missing_processes: list[str] = Field(default_factory=list)


class StandbyResponse(BaseModel):
    """Response for POST /control/standby (parks the instrument in low-flow
    standby — this is NOT a full instrument shutdown, which is a manual,
    careful operator procedure)."""

    run_id: str
    status: Literal["accepted", "queued"]
    message: str
    queue_position: int | None = None


class EquipmentBusyError(BaseModel):
    error: Literal["equipment_busy"] = "equipment_busy"
    message: str
    run_id: str | None = None


class RequiresInitError(BaseModel):
    error: Literal["requires_init"] = "requires_init"
    message: str
    required_actions: list[str]


class QueueFullError(BaseModel):
    """Body for HTTP 412 from an enqueue action while the queue is full.

    412 (precondition) per §6.1: the request would be valid once the queue
    drains. ``retry_after_s`` is advisory and mirrored into the ``Retry-After``
    header; 412 refusals MUST NOT populate ``last_error`` (§6.3).
    """

    error: Literal["queue_full"] = "queue_full"
    detail: str
    max_depth: int
    current_depth: int
    retry_after_s: float | None = None


class ReservedForRobotError(BaseModel):
    """Body for HTTP 412 when a non-robot run targets the robot-reserved tray.

    412 (precondition): the request is well-formed but the tray reservation
    forbids it right now; it would succeed with ``submitter="robot"`` or against
    an unreserved tray. Like other 412 refusals it MUST NOT populate
    ``last_error`` (§6.3).
    """

    error: Literal["reserved_for_robot"] = "reserved_for_robot"
    detail: str
    reserved_tray: str


class InstrumentServicingError(BaseModel):
    """Body for HTTP 409 when a technician is running samples directly in
    OpenLab CDS (highest precedence). The sidecar queue is halted and new
    submissions are refused. No ``Retry-After`` — servicing duration is
    unpredictable. Like requires_init this is a 409 (current-state conflict),
    not a 412, and MUST NOT populate ``last_error``."""

    error: Literal["instrument_servicing"] = "instrument_servicing"
    detail: str
    olss_state: str | None = None


class WorkflowActiveError(BaseModel):
    """Body for HTTP 423 when a robot/agent workflow holds the equipment-blocking
    lock and a non-holder tries to submit. The single-slot claim already blocks
    non-holders; this refines the rejection with a clearer reason and an advisory
    ``Retry-After`` (mirrored into the header)."""

    error: Literal["workflow_active"] = "workflow_active"
    detail: str
    claimed_by: ClaimedBy | None = None
    retry_after_s: float | None = None


class UserNotRecognizedError(BaseModel):
    """Body for HTTP 403 when a claim ``owner`` is not on the configured lab
    roster (see control/roster.py). Identity attribution, not authentication —
    an unknown owner cannot claim the instrument while the roster is enabled."""

    error: Literal["user_not_recognized"] = "user_not_recognized"
    detail: str
    owner: str


class RoleForbiddenError(BaseModel):
    """Body for HTTP 403 when the claim holder's role lacks permission for an
    action (e.g. an ``hplcms`` user calling ``workflow.start``, which requires an
    ``hte`` platform user)."""

    error: Literal["role_forbidden"] = "role_forbidden"
    detail: str
    owner: str | None = None
    role: str | None = None
    required_role: str


# ---------------------------------------------------------------------------
# v1.1 claim protocol (STATUS_SPEC §5)
# ---------------------------------------------------------------------------


class ClaimRequest(BaseModel):
    owner: str = Field(min_length=1, description="Human/agent identifier; surfaced in details.claimed_by")
    session_id: str = Field(min_length=1, description="Opaque per-session id (UUID recommended)")
    ttl_s: float = Field(default=30.0, gt=0, description="Requested TTL; device clamps to its own min/max")


class ClaimResponse(BaseModel):
    claim_token: str
    heartbeat_interval_s: float
    expires_at: datetime
    # Resolved lab role of the owner (see control/roster.py). Lets the caller
    # know up front whether it may start an equipment-blocking workflow.
    role: str | None = None


class WorkflowStartResponse(BaseModel):
    """Response for POST /control/workflow/start (equipment-blocking lock taken)."""

    status: Literal["workflow_started"] = "workflow_started"
    message: str
    expires_at: datetime
    heartbeat_interval_s: float


class WorkflowEndResponse(BaseModel):
    """Response for POST /control/workflow/end (lock released; claim retained)."""

    status: Literal["workflow_ended"] = "workflow_ended"
    message: str


class ServiceModeResponse(BaseModel):
    """Response for POST /control/service/start and /control/service/end. The
    flag is persistent (not claim-bound) so a maintenance window is never
    silently un-blocked by a dropped claim — it stays set until explicitly
    cleared by an admin."""

    status: Literal["service_mode_on", "service_mode_off"]
    service_mode: bool
    message: str


class ClaimRejection(BaseModel):
    """Body for HTTP 409 (claim contended) and HTTP 423 (control call without a
    valid token). The SDK treats 409/423 identically."""

    detail: str
    claimed_by: ClaimedBy | None = None
    retry_after_s: float | None = None


class HeartbeatResponse(BaseModel):
    """Optional 200 body for POST /control/heartbeat (device returns 204 by
    default; this lets a client observe the extended TTL when it asks)."""

    expires_at: datetime
