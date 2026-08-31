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
    canonical_plate_name,
    load_labware,
    plate_names_match,
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
    assert cfg.drawers == {}


def test_load_labware_missing_file_is_empty(tmp_path):
    load_labware.cache_clear()
    cfg = load_labware(str(tmp_path / "nope.json"))
    assert cfg.drawers == {}


def test_load_labware_nested_and_flat_forms(tmp_path):
    load_labware.cache_clear()
    nested = tmp_path / "nested.json"
    nested.write_text(json.dumps(
        {"drawers": {"D4B": {"plate_type": "54-vial", "rows": 6, "cols": 9}}}
    ), encoding="utf-8")
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps(
        {"D4B": {"plate_type": "54-vial", "rows": 6, "cols": 9}}
    ), encoding="utf-8")

    load_labware.cache_clear()
    a = load_labware(str(nested))
    load_labware.cache_clear()
    b = load_labware(str(flat))
    assert a.for_drawer("D4B").rows == 6
    assert b.for_drawer("D4B").cols == 9
    assert a.for_drawer("D1F") is None


def test_load_labware_legacy_trays_key(tmp_path):
    """Back-compat: a pre-existing config keyed by the old 'trays' top-level key
    still loads (values are now drawer codes)."""
    load_labware.cache_clear()
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(
        {"trays": {"D4B": {"plate_type": "54-vial", "rows": 6, "cols": 9}}}
    ), encoding="utf-8")
    cfg = load_labware(str(legacy))
    assert cfg.for_drawer("D4B").rows == 6


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

    config = mod._build_config(catalog, {"D4B": "*54VialPlate*"}, z_dim)
    d4b = config["drawers"]["D4B"]
    assert d4b["rows"] == 6 and d4b["cols"] == 9
    assert d4b["z_dimension_mm"] == 45.0

    # The emitted config must load cleanly into the sidecar's schema.
    cfg = LabwareConfig.model_validate(config)
    assert cfg.for_drawer("D4B").contains("F9")
    assert not cfg.for_drawer("D4B").contains("G1")


# ---------------------------------------------------------------------------
# Plate-name vocabulary (canonical names <-> OpenLab container names)
# ---------------------------------------------------------------------------

def test_legacy_name_matches_openlab_container_name():
    """A caller declaring "54-vial" means the same plate the config calls
    "*54VialPlate*"; comparing them verbatim would refuse every honest run."""
    assert plate_names_match("54-vial", "*54VialPlate*")
    assert plate_names_match("96-well", "*96Agilent*")
    assert plate_names_match("384-well", "*384Agilent*")


def test_plate_name_match_is_symmetric():
    """Either vocabulary may sit on either side (config or declaration)."""
    assert plate_names_match("*54VialPlate*", "54-vial")
    assert plate_names_match("*96Agilent*", "96-well")


def test_deep_well_plate_never_matches_shallow_96_well():
    """The needle-crash case: 96DeepAgilent45mm (44.0 mm) and *96Agilent*
    (14.3 mm) share 8x12 geometry, so only the name separates them. A caller
    declaring "96-well" must NOT satisfy a drawer holding the deep plate."""
    assert not plate_names_match("96-well", "96DeepAgilent45mm")
    assert not plate_names_match("*96Agilent*", "96DeepAgilent45mm")
    assert plate_names_match("96DeepAgilent45mm", "96DeepAgilent45mm")


def test_plate_name_match_ignores_case_and_punctuation():
    assert plate_names_match("54_VIAL", "*54vialplate*")
    assert plate_names_match("  96-Well  ", "*96AGILENT*")


def test_unknown_custom_plate_name_still_compares():
    """Custom labware isn't in the alias table; it must still match itself and
    not something else."""
    assert plate_names_match("MyCustomRack-A", "mycustomrack a")
    assert not plate_names_match("MyCustomRack-A", "MyCustomRack-B")


def test_canonical_plate_name_folds_both_vocabularies():
    assert canonical_plate_name("96-well") == canonical_plate_name("*96Agilent*")
    assert canonical_plate_name("54-vial") == "54vialplate"
