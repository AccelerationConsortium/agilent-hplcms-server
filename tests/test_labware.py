"""Unit tests for autosampler labware config + the capture tool.

Covers control/labware.py (geometry check + config loading) and
tools/capture_autosampler_config.py (decoding OpenLab .scml geometry blobs).
"""

from __future__ import annotations

import base64
import gzip
import importlib.util
import json
from pathlib import Path

from agilent_hplcms_server.control.labware import (
    LabwareConfig,
    PlateType,
    load_labware,
)

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load_capture_module():
    spec = importlib.util.spec_from_file_location(
        "capture_autosampler_config", TOOLS / "capture_autosampler_config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# PlateType.contains
# ---------------------------------------------------------------------------

def test_plate_contains_54vial_boundaries():
    plate = PlateType(plate_type="54-vial", rows=6, cols=9)
    assert plate.contains("A1")
    assert plate.contains("F9")  # last well
    assert not plate.contains("G1")  # row past F
    assert not plate.contains("A10")  # col past 9
    assert not plate.contains("bogus")


def test_plate_contains_is_case_insensitive():
    plate = PlateType(plate_type="96-well", rows=8, cols=12)
    assert plate.contains("h12")
    assert not plate.contains("i1")


# ---------------------------------------------------------------------------
# load_labware
# ---------------------------------------------------------------------------

def test_load_labware_empty_path_is_empty():
    load_labware.cache_clear()
    cfg = load_labware("")
    assert isinstance(cfg, LabwareConfig)
    assert cfg.trays == {}


def test_load_labware_missing_file_is_empty(tmp_path):
    load_labware.cache_clear()
    cfg = load_labware(str(tmp_path / "nope.json"))
    assert cfg.trays == {}


def test_load_labware_nested_and_flat_forms(tmp_path):
    load_labware.cache_clear()
    nested = tmp_path / "nested.json"
    nested.write_text(json.dumps(
        {"trays": {"rear": {"plate_type": "54-vial", "rows": 6, "cols": 9}}}
    ), encoding="utf-8")
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps(
        {"rear": {"plate_type": "54-vial", "rows": 6, "cols": 9}}
    ), encoding="utf-8")

    load_labware.cache_clear()
    a = load_labware(str(nested))
    load_labware.cache_clear()
    b = load_labware(str(flat))
    assert a.for_tray("rear").rows == 6
    assert b.for_tray("rear").cols == 9
    assert a.for_tray("front") is None


# ---------------------------------------------------------------------------
# capture tool: decode + parse OpenLab .scml geometry
# ---------------------------------------------------------------------------

_SAMPLE_CONTAINER_XML = """<?xml version="1.0"?>
<SampleContainer Version="1.0.0.0">
  <Common>
    <Identifier>{TEST-GUID-0001}</Identifier>
    <DisplayName>*54VialPlate*</DisplayName>
    <NumLocation>54</NumLocation>
    <IsPlate>true</IsPlate>
  </Common>
  <Geometry>
    <CartesianContainer>
      <Units>
        <NumRows>6</NumRows>
        <NumCols>9</NumCols>
        <WellHeight>36</WellHeight>
        <WellDepth>29</WellDepth>
        <WellVolume>1500</WellVolume>
      </Units>
    </CartesianContainer>
  </Geometry>
</SampleContainer>"""


def _make_scml(path: Path, container_xml: str, z_dim: int | None = 45) -> None:
    blob = base64.b64encode(gzip.compress(container_xml.encode("utf-8"))).decode("ascii")
    device = ""
    if z_dim is not None:
        device_xml = f"<SampleContainerDevice><ZDimension>{z_dim}</ZDimension></SampleContainerDevice>"
        dblob = base64.b64encode(gzip.compress(device_xml.encode("utf-8"))).decode("ascii")
        device = f'<SampleContainerDevice Name="Multisampler"><XmlContent>{dblob}</XmlContent></SampleContainerDevice>'
    scml = (
        '<SampleContainerInfo>'
        f'{device}'
        '<SampleContainerCatalog>'
        f'<SampleContainer Name="*54VialPlate*"><XmlContent>{blob}</XmlContent></SampleContainer>'
        '</SampleContainerCatalog>'
        '</SampleContainerInfo>'
    )
    path.write_text(scml, encoding="utf-8")


def test_capture_decode_blob_roundtrip():
    mod = _load_capture_module()
    blob = base64.b64encode(gzip.compress(b"<hi/>")).decode("ascii")
    assert mod._decode_blob(blob) == "<hi/>"


def test_capture_parse_container_geometry():
    mod = _load_capture_module()
    info = mod._parse_container("*54VialPlate*", _SAMPLE_CONTAINER_XML)
    assert info is not None
    assert info["rows"] == 6
    assert info["cols"] == 9
    assert info["num_locations"] == 54
    assert info["well_height_mm"] == 36.0
    assert info["well_depth_mm"] == 29.0
    assert info["container_guid"] == "{TEST-GUID-0001}"


def test_capture_collect_and_build_config(tmp_path):
    mod = _load_capture_module()
    _make_scml(tmp_path / "Sampler_1_TEST_1.scml", _SAMPLE_CONTAINER_XML, z_dim=45)

    catalog, z_dim, scanned = mod.collect(tmp_path, limit=10)
    assert "*54VialPlate*" in catalog
    assert z_dim == 45.0
    assert len(scanned) == 1

    config = mod._build_config(catalog, {"rear": "*54VialPlate*"}, z_dim)
    rear = config["trays"]["rear"]
    assert rear["rows"] == 6 and rear["cols"] == 9
    assert rear["z_dimension_mm"] == 45.0

    # The emitted config must load cleanly into the sidecar's schema.
    cfg = LabwareConfig.model_validate(config)
    assert cfg.for_tray("rear").contains("F9")
    assert not cfg.for_tray("rear").contains("G1")
