"""Unit tests for consumable acknowledgments (waste/solvent warning suppression)."""

from __future__ import annotations

import json

from agilent_hplcms_server.control.consumables import (
    ConsumableAcks,
    consumable_direction,
    is_suppressed,
    raw_volume_signal,
)


def test_direction_and_signal_names():
    assert consumable_direction("waste") == "high"
    assert consumable_direction("a1") == "low"
    assert raw_volume_signal("waste") == "waste_volume_ml"
    assert raw_volume_signal("b2") == "solvent_b2_volume_ml"


def test_no_ack_never_suppresses():
    assert is_suppressed(None, 1908.0, "high", 200.0) is False


def test_waste_suppressed_until_it_climbs_a_delta():
    ack = {"raw_at_ack": 1908.0, "acked_at": "t"}
    # Right after the empty (raw unchanged) → suppressed.
    assert is_suppressed(ack, 1908.0, "high", 200.0) is True
    # A little more waste, still under the delta → still suppressed.
    assert is_suppressed(ack, 2000.0, "high", 200.0) is True
    # Climbed past ack + delta → genuinely new waste → re-armed (not suppressed).
    assert is_suppressed(ack, 2200.0, "high", 200.0) is False


def test_waste_ack_ignored_after_openlab_reset_below_ack():
    ack = {"raw_at_ack": 1908.0, "acked_at": "t"}
    # OpenLab reset independently → raw dropped far below the ack → not suppressed
    # (normal logic sees a low reading and won't warn anyway).
    assert is_suppressed(ack, 50.0, "high", 200.0) is False


def test_solvent_suppressed_until_it_falls_a_delta():
    ack = {"raw_at_ack": 90.0, "acked_at": "t"}
    # Just refilled (raw unchanged, OpenLab estimate not updated) → suppressed.
    assert is_suppressed(ack, 90.0, "low", 200.0) is True
    # OpenLab reflected the refill (raw rose above the ack) → ack obsolete; the
    # high reading won't warn anyway.
    assert is_suppressed(ack, 800.0, "low", 200.0) is False
    # Consumed back down past ack - delta → re-armed.
    ack2 = {"raw_at_ack": 900.0, "acked_at": "t"}
    assert is_suppressed(ack2, 650.0, "low", 200.0) is False
    # Still within the band below the ack → suppressed.
    assert is_suppressed(ack2, 750.0, "low", 200.0) is True


def test_ack_without_reading_is_honored():
    ack = {"raw_at_ack": None, "acked_at": "t"}
    assert is_suppressed(ack, None, "high", 200.0) is True
    assert is_suppressed(ack, 1908.0, "high", 200.0) is True  # raw_at_ack None


def test_store_record_get_clear_in_memory():
    store = ConsumableAcks(path=None)
    assert store.get("waste") is None
    ack = store.record("waste", 1908.0)
    assert ack["raw_at_ack"] == 1908.0
    assert store.get("waste")["raw_at_ack"] == 1908.0
    store.clear("waste")
    assert store.get("waste") is None


def test_store_persists_across_instances(tmp_path):
    path = tmp_path / "acks.json"
    store = ConsumableAcks(path=path)
    store.record("b1", 75.0)
    # A fresh instance (e.g. after a service restart) reloads the ack.
    reloaded = ConsumableAcks(path=path)
    assert reloaded.get("b1")["raw_at_ack"] == 75.0
    # And it is a well-formed JSON file.
    assert json.loads(path.read_text(encoding="utf-8"))["b1"]["raw_at_ack"] == 75.0


def test_store_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "acks.json"
    path.write_text("not json{", encoding="utf-8")
    store = ConsumableAcks(path=path)  # must not raise
    assert store.snapshot() == {}
