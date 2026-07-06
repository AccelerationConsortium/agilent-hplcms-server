"""Probe that reads solvent/waste bottle levels and per-module states from RCDriver.log.

The active RCDriver.log is written by AcquisitionClient.exe and contains:
  - Periodic DoRequestResponse messages from the pump (G7120A) that embed full
    device-settings XML with real-time solvent and waste bottle fill levels.
  - LDT SendInstruction entries for STAT?, LAMP:INFO?, ACT:PUMP?, LIST "HOTEL_STATE"
    and other per-module commands; these are written during prerun and operation.

Returned signals (bottle levels)
---------------------------------
Agilent UI label → XML tag → signal name:
  A1 → BottleFillingA → solvent_a1_volume_ml / solvent_a1_capacity_ml
  A2 → BottleFillingB → solvent_a2_volume_ml / solvent_a2_capacity_ml
  B1 → BottleFillingC → solvent_b1_volume_ml / solvent_b1_capacity_ml
  B2 → BottleFillingD → solvent_b2_volume_ml / solvent_b2_capacity_ml
Slots with max capacity == 0 (unconfigured) are omitted from the output.
waste_volume_ml, waste_capacity_ml   — waste bottle (mL)
waste_near_capacity                  — True when volume >= not-ready threshold
solvent_a1_low, solvent_a2_low,
solvent_b1_low, solvent_b2_low       — True when volume <= not-ready threshold
rc_driver_data_age_s                 — age of the last data point in seconds

Returned signals (per-module status — keyed by module role)
------------------------------------------------------------
module_binary_pump_state        — "ready" | "busy" | "error" | "not_ready" | "unknown"
module_binary_pump_stat_flags   — list[str] of STAT? flag tokens
module_binary_pump_stat_age_s   — seconds since last STAT? was seen for this module
module_binary_pump_on           — True when ACT:PUMP? replied 1 (pump running)
module_dad_detector_state       — same states as above (G7117B)
module_dad_detector_stat_flags  — STAT? flag list
module_dad_detector_stat_age_s  — age of STAT? data
module_dad_lamp_on              — True when last LAMP command sent was LAMP 1
module_dad_lamp_rated_hours     — rated lamp lifetime in hours (from LAMP:INFO?)
module_dad_lamp_hours_used      — estimated accumulated on-time in hours
module_column_thermostat_state  — column compartment state (G7116B)
module_column_thermostat_stat_flags
module_column_thermostat_stat_age_s
module_column_thermostat_on     — True when last THRM command sent was THRM 1
module_multisampler_state       — multisampler state (G7167B)
module_multisampler_stat_flags
module_multisampler_stat_age_s
module_multisampler_drawers_occupied — count of hotel drawers with a plate/vial
module_multisampler_drawers_total    — total hotel drawer slots reported
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_AGE_S: int = 7 * 24 * 3600  # 7 days — device settings only logged at session start; levels change slowly
_LOG_FILENAME: str = "RCDriver.log"

_RC_TIMESTAMP_RE = re.compile(
    r"Timestamp: (\d{2}-\d{2}-\d{4} \d{1,2}:\d{2}:\d{2}[.,]\d+)"
)


def _parse_timestamp(line: str) -> datetime | None:
    m = _RC_TIMESTAMP_RE.search(line)
    if not m:
        return None
    try:
        ts_str = m.group(1).replace(",", ".")
        # Log timestamps are local (naive) time — keep naive for comparison.
        return datetime.strptime(ts_str, "%d-%m-%Y %H:%M:%S.%f")
    except ValueError:
        return None


def _re_float(text: str, tag: str) -> float | None:
    m = re.search(rf"&lt;{tag}&gt;([0-9.eE+\-]+)&lt;/{tag}&gt;", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _read_file(path: Path) -> str:
    try:
        with path.open("rb") as f:
            data = f.read()
    except OSError as exc:
        logger.debug("rc_driver_log: read failed: %s", exc)
        return ""
    return data.decode("utf-8", errors="replace")


def _find_target_line(log_dir: Path) -> str | None:
    """Return the most recent DoRequestResponse+BottleSolvents line across all RCDriver logs.

    Searches every *RCDriver*.log file in log_dir, newest-modified first, and
    returns the last matching line from the first file that contains one.
    Device-settings responses are only emitted at session start, so they often
    live in a rotated log rather than the currently-active file.
    """
    try:
        candidates = sorted(
            log_dir.glob("*RCDriver*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for log_path in candidates:
        text = _read_file(log_path)
        for line in reversed(text.splitlines()):
            if "DoRequestResponse" in line and "BottleSolvents" in line:
                return line
    return None


# Maps Agilent module model codes to the role keys used in signal names.
_MODULE_ROLES: dict[str, str] = {
    "G7120A": "binary_pump",
    "G7117B": "dad_detector",
    "G7116B": "column_thermostat",
    "G7167B": "multisampler",
}

# STAT? entries are written during prerun/run; keep up to 7 days to cover
# instruments that run only a few times per week.
_MODULE_STAT_MAX_AGE_S: int = 7 * 24 * 3600
# Non-STAT signals (lamp/pump commands) are keep-until-overridden.
_MODULE_CMD_MAX_AGE_S: int = 7 * 24 * 3600


def _stat_flags_to_state(flags: list[str]) -> str:
    """Map a list of STAT? tokens to a canonical component state string."""
    fs = {f.upper() for f in flags}
    if "ERROR" in fs:
        return "error"
    if "NOT_READY" in fs or "NOTREADY" in fs:
        return "not_ready"
    if any(f in fs for f in ("RUN", "PRERUN", "POSTRUN")):
        return "busy"
    if "READY" in fs:
        return "ready"
    return "unknown"


def read_module_states(log_dir: str | Path) -> dict[str, Any]:
    """Parse per-module status signals from LDT SendInstruction entries in RCDriver.log.

    Reads the file once and scans backwards, collecting the most recent entry of
    each type per module.  Returns an empty dict if the log is missing or unreadable.
    """
    log_path = Path(log_dir) / _LOG_FILENAME
    if not log_path.exists():
        return {}

    text = _read_file(log_path)
    if not text:
        return {}

    # Tracking state: keyed by module code or signal name
    stat_seen: dict[str, tuple[datetime, list[str]]] = {}  # module → (ts, flags)
    lamp_info_seen: tuple[datetime, str] | None = None     # G7117B LAMP:INFO?
    lamp_on_seen: bool | None = None                        # G7117B last LAMP N command
    pump_on_seen: bool | None = None                        # G7120A ACT:PUMP? reply
    hotel_seen: tuple[datetime, str] | None = None          # G7167B LIST "HOTEL_STATE"
    thrm_on_seen: bool | None = None                        # G7116B last THRM N command

    # Track whether each module has had all its interesting signals found
    all_found = False
    lines = text.splitlines()

    for line in reversed(lines):
        if "LDT SendInstruction" not in line:
            continue

        m_mod = re.search(r"Module:\[([^:]+):", line)
        if not m_mod:
            continue
        module_code = m_mod.group(1)
        role = _MODULE_ROLES.get(module_code)

        ts = _parse_timestamp(line)

        # ── STAT? for any known module ─────────────────────────────────────
        if module_code not in stat_seen and "Instruction:[STAT?]" in line:
            m = re.search(r'STAT "([^"]+)"', line)
            if m and ts:
                flags = [f.strip() for f in m.group(1).split(",")]
                stat_seen[module_code] = (ts, flags)

        # ── G7117B (DAD) specific ──────────────────────────────────────────
        elif module_code == "G7117B":
            if lamp_info_seen is None and "Instruction:[LAMP:INFO?]" in line:
                m = re.search(r'LAMP:INFO "([^"]+)"', line)
                if m and ts:
                    lamp_info_seen = (ts, m.group(1))
            elif lamp_on_seen is None:
                m = re.search(r"Instruction:\[LAMP (\d)\]", line)
                if m:
                    lamp_on_seen = m.group(1) == "1"

        # ── G7120A (Pump) specific ─────────────────────────────────────────
        elif module_code == "G7120A":
            if pump_on_seen is None and "Instruction:[ACT:PUMP?]" in line:
                m = re.search(r"RA 00000 ACT:PUMP (\d)", line)
                if m:
                    pump_on_seen = m.group(1) == "1"

        # ── G7167B (Multisampler) specific ────────────────────────────────
        elif module_code == "G7167B":
            if hotel_seen is None and 'Instruction:[LIST "HOTEL_STATE"]' in line:
                # Match explicit drawer entries so each [...] is fully captured.
                # The outer \]\] covers the HOTEL_STATE list closer and the Reply
                # field closer; they are NOT consumed by the inner group.
                m = re.search(
                    r"HOTEL_STATE: \[HOTEL_STATE: ((?:\[\d+(?:,\d+)+\],? *)+)\]\]",
                    line,
                )
                if m and ts:
                    hotel_seen = (ts, m.group(1))

        # ── G7116B (Column Comp) specific ─────────────────────────────────
        elif module_code == "G7116B":
            if thrm_on_seen is None:
                m = re.search(r"Instruction:\[THRM (\d)\]", line)
                if m:
                    thrm_on_seen = m.group(1) == "1"

        # Early exit once we've found everything we care about
        if (
            len(stat_seen) >= len(_MODULE_ROLES)
            and lamp_info_seen is not None
            and lamp_on_seen is not None
            and pump_on_seen is not None
            and hotel_seen is not None
            and thrm_on_seen is not None
        ):
            break

    out: dict[str, Any] = {}
    now = datetime.now()

    # ── Per-module STAT? signals ───────────────────────────────────────────
    for module_code, (ts, flags) in stat_seen.items():
        role = _MODULE_ROLES.get(module_code)
        if not role:
            continue
        age_s = (now - ts).total_seconds()
        if age_s > _MODULE_STAT_MAX_AGE_S:
            continue
        out[f"module_{role}_state"] = _stat_flags_to_state(flags)
        out[f"module_{role}_stat_flags"] = flags
        out[f"module_{role}_stat_age_s"] = round(age_s, 1)

    # ── DAD lamp signals ───────────────────────────────────────────────────
    if lamp_info_seen is not None:
        ts, info_str = lamp_info_seen
        if (now - ts).total_seconds() <= _MODULE_CMD_MAX_AGE_S:
            parts = [p.strip() for p in info_str.split(",")]
            # Field 4 (index 4) = rated lamp lifetime hours (typically 2000)
            if len(parts) >= 5:
                try:
                    out["module_dad_lamp_rated_hours"] = int(parts[4])
                except ValueError:
                    pass
            # Field 7 (index 7) = accumulated on-time in milliseconds
            if len(parts) >= 8:
                try:
                    burn_ms = int(parts[7])
                    burn_hours = burn_ms / 1_000 / 3_600
                    if 0 < burn_hours < 50_000:
                        out["module_dad_lamp_hours_used"] = round(burn_hours, 1)
                except ValueError:
                    pass

    if lamp_on_seen is not None:
        out["module_dad_lamp_on"] = lamp_on_seen

    # ── Pump signals ───────────────────────────────────────────────────────
    if pump_on_seen is not None:
        out["module_binary_pump_on"] = pump_on_seen

    # ── Column thermostat ──────────────────────────────────────────────────
    if thrm_on_seen is not None:
        out["module_column_thermostat_on"] = thrm_on_seen

    # ── Multisampler hotel state ───────────────────────────────────────────
    if hotel_seen is not None:
        ts, state_str = hotel_seen
        if (now - ts).total_seconds() <= _MODULE_CMD_MAX_AGE_S:
            drawers = re.findall(r"\[(\d+),(\d+),(\d+),(\d+),(\d+)\]", state_str)
            if drawers:
                # field index 3 = container_present (1 = present, 0 = empty)
                n_occupied = sum(1 for d in drawers if d[3] == "1")
                out["module_multisampler_drawers_occupied"] = n_occupied
                out["module_multisampler_drawers_total"] = len(drawers)

    return out


def read_rc_driver_log(log_dir: str | Path) -> dict[str, Any]:
    """Parse the latest pump device-settings from RCDriver.log.

    Returns an empty dict on any failure — missing file, stale data, or parse
    error.  The status builder treats missing keys as unknown / not available.
    """
    log_dir_path = Path(log_dir)
    if not log_dir_path.exists():
        return {}

    target_line = _find_target_line(log_dir_path)
    if target_line is None:
        return {}

    # Check data freshness.  Log timestamps are local/naive — compare with
    # naive local now() to avoid timezone-induced false staleness.
    ts = _parse_timestamp(target_line)
    age_s: float = 0.0
    if ts:
        age_s = (datetime.now() - ts).total_seconds()
        if age_s > _MAX_AGE_S:
            logger.debug(
                "rc_driver_log: data is %.0f s old (max %d s), discarding",
                age_s,
                _MAX_AGE_S,
            )
            return {}

    # Parse all four solvent channels.  Agilent UI labels vs XML tag suffix:
    #   A1 → A,  A2 → B,  B1 → C,  B2 → D
    # Values in the log are litres; we convert to mL.  Slots with max == 0
    # are unconfigured (no bottle expected) and are skipped.
    _SOLVENT_SLOTS = [
        ("a1", "A"),
        ("a2", "B"),
        ("b1", "C"),
        ("b2", "D"),
    ]

    # Waste bottle uses bare tag names (no A/B/C/D suffix).
    w_vol = _re_float(target_line, "BottleFillingHighRes")
    w_max = _re_float(target_line, "BottleMaxFillingHighRes")

    # The line contains two NotReadyLimitValue entries: solvent first, waste
    # second (inside WasteBottleNotReadyLimit).  Split on the waste block tag
    # to assign each threshold to the right channel.
    waste_block_tag = "&lt;WasteBottleNotReadyLimit&gt;"
    waste_idx = target_line.find(waste_block_tag)
    if waste_idx >= 0:
        sol_block = target_line[:waste_idx]
        w_block = target_line[waste_idx:]
        sol_limit = _re_float(sol_block, "NotReadyLimitValue")
        w_limit = _re_float(w_block, "NotReadyLimitValue")
    else:
        sol_limit = _re_float(target_line, "NotReadyLimitValue")
        w_limit = None

    out: dict[str, Any] = {}

    for slot, tag in _SOLVENT_SLOTS:
        vol = _re_float(target_line, f"BottleFilling{tag}HighRes")
        cap = _re_float(target_line, f"BottleMaxFilling{tag}HighRes")
        if vol is not None and cap is not None and cap > 0:
            out[f"solvent_{slot}_volume_ml"] = round(vol * 1000, 1)
            out[f"solvent_{slot}_capacity_ml"] = round(cap * 1000, 1)
            if sol_limit is not None:
                out[f"solvent_{slot}_low"] = vol <= sol_limit

    if w_vol is not None and w_max is not None and w_max > 0:
        out["waste_volume_ml"] = round(w_vol * 1000, 1)
        out["waste_capacity_ml"] = round(w_max * 1000, 1)
        if w_limit is not None:
            out["waste_near_capacity"] = w_vol >= w_limit
        else:
            out["waste_near_capacity"] = w_vol >= w_max * 0.90

    if age_s > 0:
        out["rc_driver_data_age_s"] = round(age_s, 1)

    return out
