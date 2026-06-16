"""Probe that reads solvent/waste bottle levels from the LC Drivers RCDriver.log.

The active RCDriver.log is written by AcquisitionClient.exe and contains periodic
DoRequestResponse messages from the pump (G7120A) that embed full device-settings
XML including real-time solvent and waste bottle fill levels.  This is the only
read-only source for these values — the pump's SDK telemetry reports
SolventSensingSupported=False so they do not appear in the SignalBuffer.

The DeviceSettings XML inside each log line is HTML-encoded, so field values are
extracted with targeted regexes rather than an XML parser.

Returned signals
----------------
solvent_a_volume_ml, solvent_a_capacity_ml  — pump port A solvent (mL)
solvent_c_volume_ml, solvent_c_capacity_ml  — pump port C solvent (mL)
waste_volume_ml, waste_capacity_ml          — waste bottle (mL)
waste_near_capacity                         — True when volume >= not-ready threshold
solvent_a_low, solvent_c_low               — True when volume <= not-ready threshold
rc_driver_data_age_s                        — age of the last data point in seconds
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_AGE_S: int = 24 * 3600  # 24 hours — bottle levels change slowly
_LOG_FILENAME: str = "RCDriver.log"

_RC_TIMESTAMP_RE = re.compile(
    r"Timestamp: (\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}[.,]\d+)"
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


def _find_target_line(log_path: Path) -> str | None:
    """Return the most recent DoRequestResponse+BottleSolvents line.

    Reads the whole file because these log lines are ~3.5 KB each and can
    span a tail-read boundary, making partial reads unreliable.  The file is
    typically 2-5 MB and reads in milliseconds.
    """
    text = _read_file(log_path)
    for line in reversed(text.splitlines()):
        if "DoRequestResponse" in line and "BottleSolvents" in line:
            return line
    return None


def read_rc_driver_log(log_dir: str | Path) -> dict[str, Any]:
    """Parse the latest pump device-settings from RCDriver.log.

    Returns an empty dict on any failure — missing file, stale data, or parse
    error.  The status builder treats missing keys as unknown / not available.
    """
    log_path = Path(log_dir) / _LOG_FILENAME
    if not log_path.exists():
        return {}

    target_line = _find_target_line(log_path)
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

    # Parse solvent bottle fill levels (values are in litres).
    sa_vol = _re_float(target_line, "BottleFillingAHighRes")
    sa_max = _re_float(target_line, "BottleMaxFillingAHighRes")
    sc_vol = _re_float(target_line, "BottleFillingCHighRes")
    sc_max = _re_float(target_line, "BottleMaxFillingCHighRes")

    # Waste bottle uses the bare tag names (no A/B/C/D suffix).
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

    if sa_vol is not None and sa_max is not None and sa_max > 0:
        out["solvent_a_volume_ml"] = round(sa_vol * 1000, 1)
        out["solvent_a_capacity_ml"] = round(sa_max * 1000, 1)
        if sol_limit is not None:
            out["solvent_a_low"] = sa_vol <= sol_limit

    if sc_vol is not None and sc_max is not None and sc_max > 0:
        out["solvent_c_volume_ml"] = round(sc_vol * 1000, 1)
        out["solvent_c_capacity_ml"] = round(sc_max * 1000, 1)
        if sol_limit is not None:
            out["solvent_c_low"] = sc_vol <= sol_limit

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
