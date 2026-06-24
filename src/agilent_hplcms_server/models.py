"""Lab Equipment Status Spec v1.1 Pydantic models.

Copied verbatim from ``ac-organic-lab/docs/STATUS_SPEC.md`` so the sidecar has
no runtime dependency on the dashboard repo. Field names are snake_case and
match the spec exactly; the dashboard's ``HttpStatusAdapter`` is strict about
shape.

v1.1 adds, additively over v1.0:
- ``EquipmentStatus.allowed_actions`` — skill names the device will honour right
  now (see ``status_builder.allowed_actions`` for the single source of truth).
- ``ClaimedBy`` — surfaced under ``details.claimed_by`` (``null`` when unclaimed).
- cooperative claims on ``/control/*`` (see ``control/claims.py``).
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


PROTOCOL_VERSION = "1.1"


EquipmentKind = Literal[
    "solid_doser",
    "liquid_handler",
    "press",
    "fume_hood",
    "robot_arm",
    "environmental_sensor",
    "hplc",
    "plate_reader",
    "plate_sealer",
    "plate_stacker",
    "other",
]

EquipmentState = Literal[
    "ready",
    "busy",
    "requires_init",
    "degraded",
    "dry_run",
    "error",
    "e_stop",
    "unknown",
]
# Note: OLSS "Paused" is not a legal EquipmentState (v1.1 dropped "paused").
# A paused OpenLab sequence is reported as "busy" with required_actions
# ["resume_paused_sequence"]; the precise OLSS status is preserved in
# details.olss_software_status and the hplc/ms component state. See status_builder.


class ComponentStatus(BaseModel):
    connected: bool
    state: str
    message: str | None = None
    last_event_at: datetime | None = None


class MetricValue(BaseModel):
    value: float | int | str | bool
    unit: str | None = None
    timestamp: datetime | None = None


class ErrorInfo(BaseModel):
    code: str | None = None
    message: str
    severity: Literal["info", "warning", "error", "critical"]
    timestamp: datetime


class ClaimedBy(BaseModel):
    """v1.1: identity of the current claim holder. Surfaced under
    ``details.claimed_by`` so every reader sees who controls the device without
    a side trip. ``null`` (absent) when no claim is active.

    ``role`` is the holder's lab role (``hplcms`` or ``hte``; see
    ``control/roster.py``) — identity attribution, not a credential. ``workflow``
    is True while the holder has taken the equipment-blocking workflow lock via
    ``POST /control/workflow/start``."""

    session_id: str
    owner: str
    expires_at: datetime
    role: str | None = None
    workflow: bool = False


class EquipmentStatus(BaseModel):
    protocol_version: str = PROTOCOL_VERSION

    equipment_id: str
    equipment_name: str
    equipment_kind: EquipmentKind
    equipment_version: str | None = None
    host: str | None = None

    equipment_status: EquipmentState
    message: str | None = None
    required_actions: list[str] = Field(default_factory=list)

    device_time: datetime
    uptime_seconds: float | None = None

    components: dict[str, ComponentStatus] = Field(default_factory=dict)
    metrics: dict[str, MetricValue] = Field(default_factory=dict)
    last_error: ErrorInfo | None = None

    # NEW in v1.1: skill names the device will currently honour. Mirrors
    # control-side precondition refusals (§6.2). `details.claimed_by` is a
    # ClaimedBy | None nested under details to keep the top-level shape stable.
    allowed_actions: list[str] = Field(default_factory=list)

    details: dict[str, Any] = Field(default_factory=dict)


class ProbeResponse(BaseModel):
    equipment_id: str
    equipment_name: str
    protocol_version: str


class HealthResponse(BaseModel):
    status: Literal["healthy"] = "healthy"
