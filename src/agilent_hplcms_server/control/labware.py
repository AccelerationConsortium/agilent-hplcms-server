"""Autosampler labware configuration (which plate/vial container is loaded in
each drawer) so the sidecar can validate a submitted sample against the *actual*
plate geometry instead of a hardcoded 96-/384-well assumption.

Why this exists
---------------
A run addresses samples by a single ``sample_position`` "D#X-Y1" (drawer + well,
e.g. "D1B-A1"); the router parses the drawer and well back out. The built-in
geometry check in ``control/models.py`` only knows the canonical 96-/384-well
formats, so a well that is valid for a 96-well plate (e.g. ``G1``) is accepted
even when the drawer physically holds a 54-vial plate (6 rows x 9 cols) —
sending the needle to a position that does not exist. This module lets the
deployment declare the plate type per drawer; submissions are then validated
against that real geometry and a declared ``plate_format`` that disagrees with
the loaded labware is refused.

Source of truth
---------------
A JSON file (``LABWARE_CONFIG_PATH``) mapping each drawer code (D1F, D4B, ...) to
a plate type. It can be generated from the instrument's real OpenLab Sample
Container configuration with ``tools/capture_autosampler_config.py``, which
decodes the geometry OpenLab writes into every result folder's ``.scml``
snapshot.

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

# ── Plate-name vocabulary ────────────────────────────────────────────────────
#
# Two vocabularies name the same physical plate. Callers (and the built-in
# ``plate_format`` check in control/models.py) use canonical names — "96-well",
# "54-vial". OpenLab names its Sample Containers differently — "*96Agilent*",
# "*54VialPlate*" — and those are the names ``capture_autosampler_config.py``
# writes into the labware config, because they carry the captured geometry.
#
# Comparing the two verbatim makes every honest declaration fail, so a declared
# name is resolved through this table before comparison. The mapping is only
# for names that mean the *same* container: notably ``96DeepAgilent45mm`` has no
# canonical alias, because no canonical name distinguishes a 44 mm deep-well
# plate from a 14.3 mm one — and conflating them is exactly the needle crash
# this module exists to prevent.
_LEGACY_PLATE_ALIASES: dict[str, str] = {
    "96-well": "*96Agilent*",
    "384-well": "*384Agilent*",
    "54-vial": "*54VialPlate*",
}


def _fold(name: str) -> str:
    """Fold a plate name to a comparison key (case/punctuation insensitive).

    OpenLab wraps some container names in asterisks and the two vocabularies
    disagree on hyphens and case, none of which distinguishes one plate from
    another.
    """
    return re.sub(r"[^a-z0-9]", "", name.strip().lower())


# Aliases are matched on the folded key, so "54-vial", "54_vial" and "54 VIAL"
# all resolve. The table above stays written in the readable canonical form.
_FOLDED_ALIASES: dict[str, str] = {
    _fold(k): _fold(v) for k, v in _LEGACY_PLATE_ALIASES.items()
}


def canonical_plate_name(name: str) -> str:
    """Resolve a plate name to the key used for equality between vocabularies."""
    folded = _fold(name)
    return _FOLDED_ALIASES.get(folded, folded)


def plate_names_match(declared: str, configured: str) -> bool:
    """True if two plate names refer to the same container.

    Symmetric, so it holds whichever vocabulary each side is written in: a
    caller declaring "54-vial" matches a config naming "*54VialPlate*", and the
    reverse. An unrecognised name is still compared — folded — against the
    other side, so custom labware works without being listed here.
    """
    return canonical_plate_name(declared) == canonical_plate_name(configured)


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
    """Drawer code ('D1F'/'D4B'/...) -> the plate type loaded in that drawer."""

    drawers: dict[str, PlateType] = Field(default_factory=dict)

    def for_drawer(self, drawer: str) -> PlateType | None:
        return self.drawers.get(drawer)


def _coerce(raw: dict) -> dict:
    """Accept ``{"drawers": {...}}``, legacy ``{"trays": {...}}``, or a flat
    ``{"D1F": {...}}`` file."""
    if "drawers" in raw:
        return raw
    if "trays" in raw:
        return {"drawers": raw["trays"]}
    return {"drawers": raw}


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
