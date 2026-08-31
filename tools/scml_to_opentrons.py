r"""Convert OpenLab Sample Container geometry into Opentrons schema-2 labware.

Why
---
The autosampler's plate catalog lives only inside OpenLab's ``.scml`` snapshots,
in Agilent's own shape. The rest of the lab (see ac-organic-lab) describes
labware as Opentrons **schema-2** JSON, which carries the one dimension the
Agilent side never validates: height. Converting gives both sides one
vocabulary, and gives the HPLC-MS a definition that states plate height as a
first-class field instead of a comment.

What the .scml actually provides
--------------------------------
Every ``SampleContainer`` carries a complete Cartesian geometry, so nothing here
is invented:

    RowOffset / ColumnOffset      centre of A1, mm from the plate's upper-left
    RowDistance / ColumnDistance  well pitch, mm
    NumRows / NumCols             grid
    XWellDiameter/YWellDiameter   well opening, mm
    WellIsSquare                  circular vs rectangular
    WellHeight / WellDepth        plate height and well depth, mm
    WellVolume                    well capacity, uL
    XSize / YSize                 plate footprint, mm

Coordinate systems differ and the conversion is the interesting part:

* Agilent addresses from the plate's **upper-left**, y increasing **downward**
  (``Origin11 = LeftUpperEdge``).
* Opentrons measures x from the left and y from the **front**, y increasing
  toward the back, with well ``z`` the well **bottom** above the plate base.

So ``y_ot = YSize - y_agilent`` and ``z = WellHeight - WellDepth``. The latter
matches how ac-organic-lab's own builder derives it (``z = footprintZ -
wellDepth``), so definitions from here and from there agree.

Usage (on the instrument PC):

    # list what would be converted:
    uv run python tools/scml_to_opentrons.py

    # write definitions, one <loadName>.json per container:
    uv run python tools/scml_to_opentrons.py --out-dir C:/SDL_Tools/labware

Drop the results into ac-organic-lab's ``labware/`` directory to share them.

Stdlib only - runs with any Python on the PC.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_RESULTS_ROOT = os.environ.get(
    "CDS_RESULTS_DIR", r"C:\CDSProjects\Installation\Results"
)

_NAME_BLOB_RE = re.compile(r'Name="([^"]*)"[^>]*>\s*<XmlContent>([^<]+)</XmlContent>')

# Opentrons requires a lowercase load name; ac-organic-lab's store additionally
# requires at least one underscore (an OT-2 gateway parsing rule, not schema-2).
# Splitting Agilent's CamelCase satisfies both without inventing a prefix.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

SCHEMA_VERSION = 2
NAMESPACE = "agilent"


def _decode(blob: str) -> str:
    raw = base64.b64decode(blob)
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    for enc in ("utf-16", "utf-16-le", "utf-8"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "\x00" not in text:
            return text
    return raw.decode("utf-8", errors="replace")


def load_name_for(display_name: str) -> str:
    """Agilent display name -> Opentrons loadName.

    ``*54VialPlate*`` -> ``54_vial_plate``; ``96DeepAgilent45mm`` ->
    ``96_deep_agilent45mm``. Asterisks are OpenLab's own decoration and carry no
    meaning here.
    """
    name = display_name.strip().strip("*")
    name = _CAMEL_RE.sub("_", name)
    name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    name = re.sub(r"_+", "_", name)
    if "_" not in name:
        name = f"agilent_{name}"
    return name


def _row_letter(idx: int) -> str:
    return chr(ord("A") + idx)


def convert(container_xml: str) -> dict | None:
    """One decoded SampleContainer -> a schema-2 definition, or None."""
    try:
        root = ET.fromstring(container_xml)
    except ET.ParseError:
        return None
    if root.tag != "SampleContainer":
        return None
    units = root.find("./Geometry/CartesianContainer/Units")
    cart = root.find("./Geometry/CartesianContainer")
    common = root.find("Common")
    if units is None or cart is None or common is None:
        return None

    def num(parent: ET.Element, tag: str) -> float | None:
        el = parent.find(tag)
        if el is None or not (el.text or "").strip():
            return None
        try:
            return float(el.text)
        except ValueError:
            return None

    rows = num(units, "NumRows")
    cols = num(units, "NumCols")
    if rows is None or cols is None:
        return None
    rows, cols = int(rows), int(cols)

    x_size = num(cart, "XSize")
    y_size = num(cart, "YSize")
    row_off = num(units, "RowOffset")
    col_off = num(units, "ColumnOffset")
    row_dist = num(units, "RowDistance")
    col_dist = num(units, "ColumnDistance")
    well_h = num(units, "WellHeight")
    well_d = num(units, "WellDepth")
    well_v = num(units, "WellVolume")
    x_diam = num(units, "XWellDiameter")
    y_diam = num(units, "YWellDiameter")
    if None in (x_size, y_size, row_off, col_off, row_dist, col_dist, well_h, well_d):
        return None

    square_el = units.find("WellIsSquare")
    is_square = (square_el is not None and (square_el.text or "").strip().lower() == "true")
    plate_el = common.find("IsPlate")
    is_plate = (plate_el is not None and (plate_el.text or "").strip().lower() == "true")

    display = (common.findtext("DisplayName") or "").strip()
    guid = (common.findtext("Identifier") or "").strip()
    load_name = load_name_for(display)

    # Opentrons: well z is the well BOTTOM above the plate base. Agilent gives
    # overall height and how deep the well is cut into it.
    well_z = round(well_h - well_d, 3)

    wells: dict[str, dict] = {}
    ordering: list[list[str]] = []
    for c in range(cols):
        column: list[str] = []
        for r in range(rows):
            name = f"{_row_letter(r)}{c + 1}"
            column.append(name)
            x_ag = col_off + c * col_dist
            y_ag = row_off + r * row_dist
            well: dict[str, object] = {
                "depth": round(well_d, 3),
                "totalLiquidVolume": round(well_v, 3) if well_v is not None else 0,
                # Agilent y runs down from the upper edge; Opentrons y runs up
                # from the front. Flip through the footprint.
                "x": round(x_ag, 3),
                "y": round(y_size - y_ag, 3),
                "z": well_z,
            }
            if is_square:
                well["shape"] = "rectangular"
                well["xDimension"] = round(x_diam or 0.0, 3)
                well["yDimension"] = round(y_diam or x_diam or 0.0, 3)
            else:
                well["shape"] = "circular"
                well["diameter"] = round(x_diam or 0.0, 3)
            wells[name] = well
        ordering.append(column)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "version": 1,
        "namespace": NAMESPACE,
        "metadata": {
            "displayName": display,
            "displayCategory": "wellPlate" if is_plate else "tubeRack",
            "displayVolumeUnits": "\u00b5L",
        },
        "brand": {"brand": "Agilent", "brandId": [guid] if guid else []},
        "parameters": {
            "format": "irregular",
            "isTiprack": False,
            "isMagneticModuleCompatible": False,
            "loadName": load_name,
            "quirks": [],
        },
        "dimensions": {
            "xDimension": round(x_size, 3),
            "yDimension": round(y_size, 3),
            # The dimension the Agilent side never validates.
            "zDimension": round(well_h, 3),
        },
        "cornerOffsetFromSlot": {"x": 0, "y": 0, "z": 0},
        "wells": wells,
        "ordering": ordering,
        "groups": [{"wells": list(wells), "metadata": {}}],
    }


def collect(root: Path, limit: int) -> dict[str, dict]:
    """Scan the newest .scml snapshots; return {loadName: definition}."""
    files = sorted(
        root.rglob("Sampler_*.scml"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:limit]
    out: dict[str, dict] = {}
    for scml in files:
        try:
            text = scml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for _name, blob in _NAME_BLOB_RE.findall(text):
            defn = convert(_decode(blob))
            if defn is not None:
                # Newest snapshot wins; don't overwrite with an older one.
                out.setdefault(defn["parameters"]["loadName"], defn)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--results-root", default=DEFAULT_RESULTS_ROOT,
                    help=f"OpenLab results tree (default: {DEFAULT_RESULTS_ROOT}).")
    ap.add_argument("--limit", type=int, default=20,
                    help="Newest .scml files to scan (default: 20).")
    ap.add_argument("--out-dir", default=None,
                    help="Write <loadName>.json here (default: summarise only).")
    args = ap.parse_args()

    root = Path(args.results_root)
    if not root.is_dir():
        print(f"error: results root not found: {root}", file=sys.stderr)
        return 2

    defs = collect(root, args.limit)
    if not defs:
        print(f"error: no convertible containers found under {root}", file=sys.stderr)
        return 2

    print(f"# Converted {len(defs)} container(s) to Opentrons schema-2:", file=sys.stderr)
    for load_name, d in sorted(defs.items()):
        dim = d["dimensions"]
        print(
            f"#   {load_name:34} {len(d['ordering'])}x{len(d['ordering'][0])} "
            f"{dim['xDimension']}x{dim['yDimension']}x{dim['zDimension']} mm "
            f"({d['metadata']['displayName']})",
            file=sys.stderr,
        )

    if not args.out_dir:
        print("# (no --out-dir; printing definitions)", file=sys.stderr)
        print(json.dumps(defs, indent=2))
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for load_name, d in sorted(defs.items()):
        path = out_dir / f"{load_name}.json"
        path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        print(f"# wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
