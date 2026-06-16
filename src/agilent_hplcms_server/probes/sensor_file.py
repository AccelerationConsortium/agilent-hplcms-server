"""Read live instrument sensor values from a JSON file written by the sensor daemon.

The sensor daemon (``tools/hplcms_sensor_daemon.py``) runs in the Moses conda
environment (which has the Agilent OpenLab .NET SDK), connects to the
InstrumentController once at startup, and writes this file on every parameter
change event.  The sidecar never imports .NET / pythonnet — it just reads the
file.

If the file is absent (daemon not yet running) or unreadable, all sensor keys
are omitted from the signals dict.  The dashboard displays "—" for any key
that is missing from the metrics response.

JSON file format
----------------
A flat object with metric-name keys and numeric/bool values::

    {
      "turbopump_ready": true,
      "vacuum_level_mbar": 5.2e-6,
      "source_temperature_c": 120.0,
      ...
      "written_at": "2026-06-16T12:00:00+00:00"
    }

The ``written_at`` field is used to detect a stale / hung daemon (values older
than SENSOR_FILE_MAX_AGE_S are dropped).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Sensor values older than this are considered stale and discarded.
SENSOR_FILE_MAX_AGE_S: int = 120

# All metric keys we expect from the sensor daemon.
SENSOR_METRIC_KEYS: frozenset[str] = frozenset(
    [
        # MS Vacuum & Source
        "turbopump_ready",
        "vacuum_level_mbar",
        "source_temperature_c",
        "source_temperature_setpoint_c",
        "drying_gas_flow_lpm",
        "drying_gas_temperature_c",
        "nebulizer_pressure_psig",
        "hv_ready",
        # LC System
        "system_pressure_bar",
        "system_pressure_limit_bar",
        "column_temperature_c",
        "column_temperature_setpoint_c",
        "flow_rate_ml_min",
        "degasser_active",
        # Consumables
        "solvent_a_volume_ml",
        "solvent_b_volume_ml",
        "wash_solvent_volume_ml",
        "waste_volume_ml",
        "waste_capacity_ml",
        "calibrant_ok",
        # Calibration
        "last_calibration_date",
        "leak_detected",
    ]
)


def read_sensor_file(sensor_data_file: str) -> dict[str, Any]:
    """Return the latest sensor values from the daemon-written JSON file.

    Returns an empty dict (rather than raising) on any failure — missing file,
    JSON parse error, or stale data.  Missing keys are silently omitted from
    the returned dict; the status builder treats them as "unknown".
    """
    path = Path(sensor_data_file)
    if not path.exists():
        return {}

    try:
        with path.open(encoding="utf-8") as f:
            data: dict = json.load(f)
    except Exception as exc:
        logger.debug("sensor_file: failed to read %s: %s", path, exc)
        return {}

    # Check freshness using the written_at field.
    written_at_str: str | None = data.get("written_at")
    if written_at_str:
        try:
            from datetime import datetime, timezone

            written_at = datetime.fromisoformat(written_at_str)
            if written_at.tzinfo is None:
                written_at = written_at.replace(tzinfo=timezone.utc)
            age_s = time.time() - written_at.timestamp()
            if age_s > SENSOR_FILE_MAX_AGE_S:
                logger.debug(
                    "sensor_file: data is %.0f s old (max %d s), discarding",
                    age_s,
                    SENSOR_FILE_MAX_AGE_S,
                )
                return {}
        except Exception:
            pass

    # Return only the recognised sensor metric keys. Accept either the daemon's
    # native flat shape or already-wrapped STATUS_SPEC-ish values so an
    # incremental daemon rollout cannot accidentally produce nested metrics.
    values: dict[str, Any] = {}
    for key in SENSOR_METRIC_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, dict) and "value" in value:
            value = value["value"]
        values[key] = value
    return values
