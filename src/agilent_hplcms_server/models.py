"""Lab Equipment Status Spec types — from the shared ``sdl-lab-contract``
package.

This module used to be a verbatim vendored copy of the contract from
``ac-organic-lab/docs/STATUS_SPEC.md``; the promised shared package now
exists, so the types are imported and re-exported here to keep every
``from .models import ...`` / ``from ..models import ...`` in this repo
working unchanged. The dashboard's ``HttpStatusAdapter`` is strict about
shape — sharing one set of Pydantic models is what makes drift impossible.

Conformance: this sidecar conforms to **lab status spec v1.2**
(``activity`` / ``activity_since`` observed from the acquisition signals;
see ``status_builder.py``). ``PROTOCOL_VERSION`` below is the version THIS
device speaks and deliberately overrides the package's parse-time default
("1.0" — for devices that don't state a version).
"""

from sdl_lab_contract import (
    Activity,
    ClaimRejection,
    ClaimRequest,
    ClaimResponse,
    ComponentStatus,
    EquipmentKind,
    EquipmentState,
    EquipmentStatus,
    ErrorInfo,
    HealthResponse,
    MetricValue,
    ProbeResponse,
)
from sdl_lab_contract import ClaimedBy as _ContractClaimedBy

PROTOCOL_VERSION = "1.2"


class ClaimedBy(_ContractClaimedBy):
    """Contract ``ClaimedBy`` plus this sidecar's additive extensions.

    ``role`` is the holder's lab role (``user`` / ``automation`` /
    ``service``; see ``control/roster.py``) — identity attribution, not a
    credential. ``workflow`` is True while the holder has taken the
    equipment-blocking workflow lock via ``POST /control/workflow/start``.
    Additive fields under ``details.claimed_by`` are contract-legal
    (``details`` is free-form); readers on the plain contract type simply
    ignore them.
    """

    role: str | None = None
    workflow: bool = False

__all__ = [
    "PROTOCOL_VERSION",
    "Activity",
    "ClaimedBy",
    "ClaimRejection",
    "ClaimRequest",
    "ClaimResponse",
    "ComponentStatus",
    "EquipmentKind",
    "EquipmentState",
    "EquipmentStatus",
    "ErrorInfo",
    "HealthResponse",
    "MetricValue",
    "ProbeResponse",
]
