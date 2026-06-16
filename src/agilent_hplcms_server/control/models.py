"""Pydantic models for the /control/* endpoints (Layer 1 hardware limits)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


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
    sample_position: str = Field(
        min_length=1,
        max_length=16,
        description='Autosampler position, e.g. "D4B-A1" or "3"',
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


class RunResponse(BaseModel):
    """Response for POST /control/run (backward-compat quick-submit)."""

    run_id: str
    status: Literal["accepted", "queued"]
    message: str
    pid: int | None = None
    started_at: datetime | None = None
    queue_position: int | None = None


class QueuedRun(BaseModel):
    """Single job entry returned in GET /control/queue."""

    queue_id: str
    request: dict[str, Any]
    queued_at: datetime
    status: Literal["pending", "acquiring", "done", "failed"]
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


class ShutdownResponse(BaseModel):
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
    error: Literal["queue_full"] = "queue_full"
    message: str
    max_depth: int
    current_depth: int
