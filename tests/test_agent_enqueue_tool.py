"""Tests for the method-store logic in tools/agent_agilent_enqueue.py.

The tool is a Moses work-dir script, so its instrument imports (agent_agilent,
moses.agilent) exist only on the instrument PC — the module keeps them lazy so
the pure method-store logic loads and tests anywhere. These tests pin:

- the canonical spec (what identifies a method) and the readable store key;
- tolerance coalescing: a request "barely different" from a stored method
  (e.g. a 5.0 vs 5.1 min run time) reuses the stored one instead of minting a
  near-duplicate, while anything chemically different (solvents, MS mode, a
  gradient shape change beyond tolerance) never coalesces.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

TOOL_PATH = Path(__file__).parent.parent / "tools" / "agent_agilent_enqueue.py"

spec = importlib.util.spec_from_file_location("agent_agilent_enqueue", TOOL_PATH)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


GRADIENT = {
    "name": "OXSCR-05_5-95B_7min",
    "solvent_a": "H2O_0.1%FA",
    "solvent_b": "ACN_0.1%FA",
    "run_time": 7.0,
    "flow_rate": 0.6,
    "gradient_table": [[0.0, 0.05], [0.7, 0.05], [5.0, 0.95], [6.5, 0.95], [6.6, 0.05]],
    "equilibration_time": 3.0,
}


def _spec(**overrides):
    g = {**GRADIENT, **{k: v for k, v in overrides.items() if k != "ms_mode"}}
    return tool._canonical_spec(g, overrides.get("ms_mode", "positive"))


# --- canonical spec + key ---------------------------------------------------


def test_canonical_spec_ignores_gradient_display_name():
    # The gradient's free-text name is presentation, not chemistry: two
    # requests differing only in name are the same method.
    a = _spec()
    b = tool._canonical_spec({**GRADIENT, "name": "renamed"}, "positive")
    assert a == b


def test_method_key_is_readable_and_deterministic():
    key = tool._method_key(_spec())
    assert key == tool._method_key(_spec())
    assert key.startswith("positive_7min_0p6mLmin_")
    # short content hash suffix keeps distinct methods from colliding
    assert len(key.rsplit("_", 1)[1]) == 8
    assert tool._method_key(_spec(run_time=10.0)) != key


# --- tolerance coalescing ---------------------------------------------------


def test_barely_different_run_time_coalesces():
    # The motivating example: 5.0 vs 5.1 min has no practical difference.
    stored = _spec(run_time=5.0)
    requested = _spec(run_time=5.1)
    assert tool._equivalent_spec(requested, stored)


def test_gradient_point_within_half_percent_b_coalesces():
    stored = _spec()
    table = [list(row) for row in GRADIENT["gradient_table"]]
    table[2][1] = 0.953  # 95.0 → 95.3 %B
    assert tool._equivalent_spec(_spec(gradient_table=table), stored)


def test_gradient_point_beyond_tolerance_is_a_new_method():
    stored = _spec()
    table = [list(row) for row in GRADIENT["gradient_table"]]
    table[2][1] = 0.90  # 95 → 90 %B is a real shape change
    assert not tool._equivalent_spec(_spec(gradient_table=table), stored)


def test_different_solvent_never_coalesces():
    stored = _spec()
    assert not tool._equivalent_spec(_spec(solvent_b="MeOH_0.1%FA"), stored)


def test_different_ms_mode_never_coalesces():
    stored = _spec()
    assert not tool._equivalent_spec(_spec(ms_mode="negative"), stored)


def test_different_row_count_never_coalesces():
    stored = _spec()
    table = GRADIENT["gradient_table"] + [[6.9, 0.05]]
    assert not tool._equivalent_spec(_spec(gradient_table=table), stored)


# --- store lookup over manifests --------------------------------------------


def test_find_stored_method_prefers_exact_then_oldest_equivalent(tmp_path):
    exact = _spec()
    near = _spec(run_time=7.05)
    other = _spec(run_time=12.0)

    tool._write_manifest(tmp_path, "m_old", near)
    tool._write_manifest(tmp_path, "m_exact", exact)
    tool._write_manifest(tmp_path, "m_other", other)

    # Exact spec match wins over an equivalent-within-tolerance one.
    assert tool._find_stored_method(tmp_path, exact) == "m_exact"
    # With no exact hit, the first (oldest by key order) equivalent is reused.
    (tmp_path / "m_exact.json").unlink()
    assert tool._find_stored_method(tmp_path, exact) == "m_old"
    # Nothing equivalent → miss.
    assert tool._find_stored_method(tmp_path, _spec(solvent_a="D2O")) is None


def test_module_imports_without_instrument_sdk():
    # The lazy-import contract this whole test file relies on: loading the
    # module must not require agent_agilent / moses.
    assert not hasattr(tool, "GradientConfig")
