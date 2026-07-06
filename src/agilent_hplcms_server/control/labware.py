"""Autosampler labware configuration (which plate/vial container is loaded in
each logical tray) so the sidecar can validate a submitted sample against the
*actual* plate geometry instead of a hardcoded 96-/384-well assumption.

Why this exists
---------------
A run addresses samples by ``{tray, well}``. The built-in geometry check in
``control/models.py`` only knows the canonical 96-/384-well formats, so a well
that is valid for a 96-well plate (e.g. ``G1``) is accepted even when the tray
physically holds a 54-vial plate (6 rows x 9 cols) — sending the needle to a
position that does not exist. This module lets the deployment declare the plate
type per tray; submissions are then validated against that real geometry and a
declared ``plate_format`` that disagrees with the loaded labware is refused.

Source of truth
---------------
A JSON file (``LABWARE_CONFIG_PATH``) mapping each tray to a plate type. It can
be generated from the instrument's real OpenLab Sample Container configuration
with ``tools/capture_autosampler_config.py``, which decodes the geometry OpenLab
writes into every result folder's ``.scml`` snapshot.

Empty / unset path -> no labware config -> the sidecar falls back to the
built-in ``plate_format`` geometry check (legacy behaviour, never bricks).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

_WELL_RE = re.compile(r"^([A-Za-z])(\d{1,2})$")


class PlateType(BaseModel):
    """Geometry of the container currently loaded in one autosampler tray.

    ``rows``/``cols`` are authoritative for the well-range check. The remaining
    fields are provenance/audit captured from the OpenLab ``.scml`` geometry
    (they document the physical plate but are not used for validation).
    """

    plate_type: str = Field(description="Human name, e.g. '96-well', '54-vial'.")
    rows: int = Field(gt=0, le=32, description="Number of lettered rows (A, B, ...).")
    cols: int = Field(gt=0, le=48, description="Number of numbered columns (1..cols).")
    num_locations: int | None = Field(
        default=None, description="Addressable positions (rows*cols for a full plate)."
    )
    well_height_mm: float | None = None
    well_depth_mm: float | None = None
    z_dimension_mm: float | None = Field(
        default=None, description="Drawer/plate top height reported by OpenLab (crash-clearance)."
    )
    container_guid: str | None = None
    source: str | None = Field(
        default=None, description="Where this was captured from (e.g. the .scml path)."
    )

    def contains(self, well: str) -> bool:
        """True if ``well`` (e.g. 'A1') is an addressable position on this plate."""
        m = _WELL_RE.match(well)
        if m is None:
            return False
        row_idx = ord(m.group(1).upper()) - ord("A")
        col = int(m.group(2))
        return 0 <= row_idx < self.rows and 1 <= col <= self.cols


class LabwareConfig(BaseModel):
    """Logical tray name ('front'/'rear') -> the plate type loaded in it."""

    trays: dict[str, PlateType] = Field(default_factory=dict)

    def for_tray(self, tray: str) -> PlateType | None:
        return self.trays.get(tray)


def _coerce(raw: dict) -> dict:
    """Accept either ``{"trays": {...}}`` or a flat ``{"front": {...}}`` file."""
    if "trays" in raw:
        return raw
    return {"trays": raw}


@lru_cache(maxsize=8)
def load_labware(path: str) -> LabwareConfig:
    """Load and cache the labware config from a JSON file.

    Empty path or missing file -> empty config (no labware enforcement). Cached
    by path; call ``load_labware.cache_clear()`` after editing the file in place.
    """
    if not path:
        return LabwareConfig()
    p = Path(path)
    if not p.is_file():
        return LabwareConfig()
    raw = json.loads(p.read_text(encoding="utf-8"))
    return LabwareConfig.model_validate(_coerce(raw))
