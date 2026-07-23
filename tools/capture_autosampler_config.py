r"""Capture the UPLC autosampler's real plate/labware geometry from OpenLab.

OpenLab CDS writes a Sample Container snapshot (``Sampler_*_*.scml``) into every
result folder. Each snapshot embeds, as GZip+Base64 XML:

* the multisampler DEVICE layout (drawer tags D1F/D1B/..., ``ZDimension`` = the
  drawer/plate top height the arm must clear), and
* a CATALOG of Sample Container definitions, each with full geometry
  (``NumRows``/``NumCols``, ``WellHeight``/``WellDepth``, ``NumLocation``, ...).

This tool scans the newest ``.scml`` files under the results tree, decodes those
blobs, and prints the plate-type catalog with exact geometry. With ``--assign``
it emits a labware config JSON (control/labware.py schema, LABWARE_CONFIG_PATH)
so the sidecar validates submissions against the plate ACTUALLY loaded in each
drawer instead of the built-in 96/384 assumption.

Why you still assign drawers by hand: a single snapshot reliably yields the
plate CATALOG + geometry, but the authoritative drawer->plate binding is a
safety-critical decision. Once labware is standardized per drawer you declare it
once (``--assign D1F=<PlateName> D4B=<PlateName>``); the tool fills in the
precise geometry captured from OpenLab. Drawer codes are D1F/D1B/.../D4F/D4B
(D + drawer number 1-4 + F=front / B=back), matching the sample_position address
the sidecar validates.

Usage (PowerShell, on the instrument PC):

    # inspect what plate types exist + their geometry:
    uv run python tools/capture_autosampler_config.py

    # write a ready labware config for the sidecar:
    uv run python tools/capture_autosampler_config.py `
        --assign D4B="*54VialPlate*" D1F="*54VialPlate*" `
        --out C:/SDL_Tools/labware_config.json

Then point the sidecar at it:  setx LABWARE_CONFIG_PATH C:\SDL_Tools\labware_config.json

Stdlib only - runs with any Python on the PC.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import os

# Valid Agilent multisampler drawer codes: D<1-4><F|B>. These are the drawer
# halves of the sample_position "D#X-Y1" address the sidecar validates.
KNOWN_DRAWERS = {f"D{n}{s}" for n in range(1, 5) for s in ("F", "B")}

DEFAULT_RESULTS_ROOT = os.environ.get(
    "CDS_RESULTS_DIR", r"C:\CDSProjects\Installation\Results"
)

_NAME_BLOB_RE = re.compile(
    r'Name="([^"]*)"[^>]*>\s*<XmlContent>([^<]+)</XmlContent>'
)


def _decode_blob(b64: str) -> str:
    raw = base64.b64decode(b64)
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return raw.decode("utf-8", errors="replace")


def _find_scml_files(root: Path, limit: int) -> list[Path]:
    files = sorted(
        root.rglob("Sampler_*.scml"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[:limit]


def _parse_container(name: str, xml_text: str) -> dict | None:
    """Extract geometry from one decoded SampleContainer definition."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    if root.tag != "SampleContainer":
        return None
    units = root.find("./Geometry/CartesianContainer/Units")
    if units is None:
        return None

    def _txt(tag: str) -> str | None:
        el = units.find(tag)
        return el.text if el is not None else None

    def _int(tag: str) -> int | None:
        v = _txt(tag)
        return int(float(v)) if v not in (None, "") else None

    def _float(tag: str) -> float | None:
        v = _txt(tag)
        return float(v) if v not in (None, "") else None

    common = root.find("Common")
    guid = None
    num_loc = None
    if common is not None:
        gid = common.find("Identifier")
        guid = gid.text if gid is not None else None
        nl = common.find("NumLocation")
        num_loc = int(nl.text) if nl is not None and nl.text else None

    rows = _int("NumRows")
    cols = _int("NumCols")
    if rows is None or cols is None:
        return None
    return {
        "plate_type": name,
        "rows": rows,
        "cols": cols,
        "num_locations": num_loc if num_loc is not None else rows * cols,
        "well_height_mm": _float("WellHeight"),
        "well_depth_mm": _float("WellDepth"),
        "container_guid": guid,
    }


def _parse_device_zdim(xml_text: str) -> float | None:
    """Largest ZDimension in a decoded SampleContainerDevice (drawer top height)."""
    zs = [float(m) for m in re.findall(r"<ZDimension>([\d.]+)</ZDimension>", xml_text)]
    return max(zs) if zs else None


def collect(root: Path, limit: int) -> tuple[dict[str, dict], float | None, list[str]]:
    """Return (plate_types_by_name, device_z_dimension, scanned_files)."""
    catalog: dict[str, dict] = {}
    z_dim: float | None = None
    scanned: list[str] = []
    for scml in _find_scml_files(root, limit):
        try:
            text = scml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned.append(str(scml))
        for name, blob in _NAME_BLOB_RE.findall(text):
            decoded = _decode_blob(blob)
            if "<SampleContainerDevice" in decoded and z_dim is None:
                z_dim = _parse_device_zdim(decoded)
            info = _parse_container(name, decoded)
            if info is not None:
                info["source"] = str(scml)
                # First (newest) wins; don't overwrite with older snapshots.
                catalog.setdefault(info["plate_type"], info)
    return catalog, z_dim, scanned


def _build_config(catalog: dict[str, dict], assign: dict[str, str], z_dim: float | None) -> dict:
    drawers: dict[str, dict] = {}
    for drawer, plate_name in assign.items():
        if plate_name not in catalog:
            raise SystemExit(
                f"error: plate {plate_name!r} not found in captured catalog "
                f"({', '.join(sorted(catalog)) or 'none'})."
            )
        entry = dict(catalog[plate_name])
        if z_dim is not None:
            entry["z_dimension_mm"] = z_dim
        drawers[drawer] = entry
    return {"drawers": drawers}


def _parse_assign(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"error: --assign expects DRAWER=PlateName, got {pair!r}.")
        drawer, plate = pair.split("=", 1)
        drawer = drawer.strip().upper()
        if drawer not in KNOWN_DRAWERS:
            raise SystemExit(
                f"error: unknown drawer {drawer!r} (known: {', '.join(sorted(KNOWN_DRAWERS))})."
            )
        out[drawer] = plate.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-root", default=DEFAULT_RESULTS_ROOT,
                    help=f"Root of the OpenLab results tree (default: {DEFAULT_RESULTS_ROOT}).")
    ap.add_argument("--limit", type=int, default=40,
                    help="Number of newest .scml files to scan (default: 40).")
    ap.add_argument("--assign", nargs="*", default=[], metavar="DRAWER=PLATE",
                    help="Assign a captured plate type to a drawer, e.g. D4B='*54VialPlate*'.")
    ap.add_argument("--out", default=None,
                    help="Write the labware config JSON here (default: stdout).")
    args = ap.parse_args()

    root = Path(args.results_root)
    if not root.is_dir():
        print(f"error: results root not found: {root}", file=sys.stderr)
        return 2

    catalog, z_dim, scanned = collect(root, args.limit)
    if not scanned:
        print(f"error: no Sampler_*.scml files under {root}", file=sys.stderr)
        return 2

    print(f"# Scanned {len(scanned)} .scml snapshot(s); device ZDimension (top height): "
          f"{z_dim if z_dim is not None else 'unknown'} mm", file=sys.stderr)
    print(f"# Plate types found ({len(catalog)}):", file=sys.stderr)
    for name, info in sorted(catalog.items()):
        print(
            f"#   {name!r}: {info['rows']}x{info['cols']} "
            f"({info['num_locations']} pos), WellHeight={info['well_height_mm']} "
            f"WellDepth={info['well_depth_mm']}",
            file=sys.stderr,
        )
    print(f"# Known drawer codes: {', '.join(sorted(KNOWN_DRAWERS))}", file=sys.stderr)

    if not args.assign:
        print("# (no --assign given; printing captured catalog only)", file=sys.stderr)
        print(json.dumps({"available_plate_types": catalog}, indent=2))
        return 0

    config = _build_config(catalog, _parse_assign(args.assign), z_dim)
    payload = json.dumps(config, indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"# Wrote labware config -> {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
