"""Sensor daemon — runs in the Moses conda env, writes live instrument parameters.

Run this in the ``moses_v4_yoyo`` conda env (which has pythonnet and the Agilent
OpenLab .NET SDK).  It should NOT be run inside the sidecar venv.

Usage
-----
    cd C:\\Users\\sdl2\\Documents\\Code\\yoyo\\pythofisher_hplcms
    C:\\Users\\sdl2\\anaconda3\\envs\\moses_v4_yoyo\\python.exe ^
        C:\\Users\\sdl2\\Projects\\agilent-hplcms-server\\tools\\hplcms_sensor_daemon.py

Or as an NSSM service ``hplc-ms-sensors``::

    nssm install hplc-ms-sensors ^
        C:\\Users\\sdl2\\anaconda3\\envs\\moses_v4_yoyo\\python.exe ^
        C:\\Users\\sdl2\\Projects\\agilent-hplcms-server\\tools\\hplcms_sensor_daemon.py

What it does
------------
1. Connects to the Agilent InstrumentController via the WCF Named Pipe to
   receive state-change events and know when OpenLab is online.
2. Polls the SQ G6160B SWARM HTTP API (http://192.168.254.60:8080) for MS
   vacuum, source, and gas parameters.
3. Polls the OpenLab SignalBufferService (localhost:9753) for LC pressure,
   flow, and column temperature.
4. Writes results to SENSOR_DATA_FILE every POLL_INTERVAL_S seconds (default 30).
5. Reconnects automatically if the connection drops.

Output JSON
-----------
Written to ``C:\\SDL_Tools\\hplcms_sensor_data.json`` (override via SENSOR_DATA_FILE
env var).  The sidecar reads this file and exposes the values via GET /status.

    {
      "turbopump_ready": true,
      "vacuum_level_mbar": 5.2e-06,
      "source_temperature_c": 120.1,
      "source_temperature_setpoint_c": 120.0,
      "drying_gas_flow_lpm": 12.0,
      "drying_gas_temperature_c": 350.0,
      "nebulizer_pressure_psig": 35.0,
      "hv_ready": true,
      "system_pressure_bar": 250.3,
      "flow_rate_ml_min": 0.6,
      "column_temperature_c": 35.0,
      "written_at": "2026-06-16T12:00:00+00:00"
    }

Data sources
------------
- MS metrics: SWARM TCD HTTP API on the SQ at http://192.168.254.60:8080
  Endpoints: /api/actual/FetchFullActualList, /api/actual/FetchTurboPumpState
- LC metrics: OpenLab SignalBufferService at localhost:9753
  Device 3 / Signal 401: Pump pressure (bar)
  Device 3 / Signal 402: Pump flow (mL/min)
  Device 1 / Signal 201: Column temperature (°C)
- solvent/waste volumes: SolventSensingSupported=False — not available from this
  instrument setup; omitted from output.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------

SENSOR_DATA_FILE: str = os.environ.get(
    "SENSOR_DATA_FILE", r"C:\SDL_Tools\hplcms_sensor_data.json"
)
LOG_FILE: str = os.environ.get(
    "SENSOR_DAEMON_LOG",
    r"C:\ProgramData\Agilent\LogFiles\hplcms_sensor_daemon.log",
)
POLL_INTERVAL_S: int = int(os.environ.get("SENSOR_POLL_INTERVAL_S", "30"))
RECONNECT_DELAY_S: int = int(os.environ.get("SENSOR_RECONNECT_DELAY_S", "60"))

INSTRUMENT_ID: str = os.environ.get("OPENLAB_INSTRUMENT_ID", "15")
PROJECT_ID: str = os.environ.get("OPENLAB_PROJECT_ID", "16")
CONN_STRING: str = os.environ.get(
    "OPENLAB_CONN_STRING", "net.pipe://localhost/Agilent/OpenLAB/"
)

# SQ G6160B SWARM TCD HTTP API
SQ_HTTP_BASE: str = os.environ.get("SQ_HTTP_BASE", "http://192.168.254.60:8080")

# OpenLab SignalBufferService (LC live signals)
LC_SIGNAL_BASE: str = os.environ.get(
    "LC_SIGNAL_BASE",
    "http://DESKTOP-V2PV40S:9753/openlab/acquisitionservices/15/signalbufferserivice",
)

# (device_id, signal_id) → metric_key
LC_SIGNALS: list[tuple[int, int, str]] = [
    (3, 401, "system_pressure_bar"),
    (3, 402, "flow_rate_ml_min"),
    (1, 201, "column_temperature_c"),
]

METRIC_VALUE_TYPES: dict[str, str] = {
    "turbopump_ready": "bool",
    "vacuum_level_mbar": "float",
    "source_temperature_c": "float",
    "source_temperature_setpoint_c": "float",
    "drying_gas_flow_lpm": "float",
    "drying_gas_temperature_c": "float",
    "nebulizer_pressure_psig": "float",
    "hv_ready": "bool",
    "system_pressure_bar": "float",
    "system_pressure_limit_bar": "float",
    "column_temperature_c": "float",
    "column_temperature_setpoint_c": "float",
    "flow_rate_ml_min": "float",
    "degasser_active": "bool",
    "solvent_a_volume_ml": "float",
    "solvent_b_volume_ml": "float",
    "wash_solvent_volume_ml": "float",
    "waste_volume_ml": "float",
    "waste_capacity_ml": "float",
    "calibrant_ok": "bool",
    "last_calibration_date": "str",
    "leak_detected": "bool",
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)


def _log_handlers() -> list[logging.Handler]:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError as exc:
        print(f"sensor_daemon: log file unavailable ({LOG_FILE}): {exc}", file=sys.stderr)
    return handlers


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][%(name)s] %(message)s",
    handlers=_log_handlers(),
)
log = logging.getLogger("sensor_daemon")


# ---------------------------------------------------------------------------
# SDK ticket (Moses Storage — same mechanism Moses uses)
# ---------------------------------------------------------------------------

def _ensure_moses_on_path() -> None:
    for candidate in [
        Path(r"C:\Users\sdl2\Documents\Code\yoyo\pythofisher_hplcms\src"),
        Path(".") / "src",
    ]:
        if candidate.exists() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
            return


def _get_sdk_ticket() -> str | None:
    _ensure_moses_on_path()
    try:
        from moses.agilent.data_items.storage import Storage  # type: ignore[import]
        storage = Storage(CONN_STRING)
        ticket = storage.get_ticket()
        log.info("SDK ticket obtained, length=%d", len(ticket))
        return ticket
    except Exception as exc:
        log.error("SDK ticket failed: %s", exc)
        return None


def _load_sdk() -> type:
    import clr  # type: ignore[import]
    _ensure_moses_on_path()
    from moses._config.utils import get_automation_instrument, get_automation_core  # type: ignore[import]
    clr.AddReference(str(get_automation_core() / "Agilent.OpenLAB.Acquisition.AutomationCore"))
    clr.AddReference(str(get_automation_instrument() / "Agilent.OpenLAB.Acquisition.AutomationInstrument"))
    from Agilent.OpenLAB.Acquisition.AutomationInstrument import InstrumentController  # type: ignore
    return InstrumentController


# ---------------------------------------------------------------------------
# Value coercion helpers
# ---------------------------------------------------------------------------

def _unwrap_value(value: Any, depth: int = 0) -> Any:
    if value is None or depth > 3:
        return value
    if isinstance(value, (bool, int, float, str, datetime, date)):
        return value
    for attr in ("Value", "ActualValue", "CurrentValue", "RawValue", "Result"):
        try:
            inner = getattr(value, attr)
        except Exception:
            continue
        if inner is not value and inner is not None:
            return _unwrap_value(inner, depth + 1)
    try:
        if hasattr(value, "ToString"):
            return str(value)
    except Exception:
        pass
    return value


def _coerce_bool(value: Any) -> bool | None:
    value = _unwrap_value(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on", "ok", "ready",
                    "active", "enabled", "good", "normal", "closed",
                    "detected", "leak", "leak detected"}:
            return True
        if text in {"0", "false", "no", "n", "off", "not ready", "notready",
                    "inactive", "disabled", "bad", "error", "open", "none",
                    "not detected", "no leak", "clear"}:
            return False
    return None


def _coerce_float(value: Any) -> float | None:
    value = _unwrap_value(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        import re
        text = value.strip().replace(",", "")
        if not text:
            return None
        match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text)
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


def _coerce_string(value: Any) -> str | None:
    value = _unwrap_value(value)
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _coerce_metric_value(metric_key: str, value: Any) -> object | None:
    expected = METRIC_VALUE_TYPES.get(metric_key)
    if expected == "bool":
        return _coerce_bool(value)
    if expected == "float":
        return _coerce_float(value)
    if expected == "str":
        return _coerce_string(value)
    return _unwrap_value(value)


# ---------------------------------------------------------------------------
# HTTP fetch helper
# ---------------------------------------------------------------------------

def _fetch_json(url: str, timeout: int = 5) -> dict[str, Any] | list | None:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(500_000).decode("utf-8", errors="replace")
        parsed = json.loads(body)
        return parsed if isinstance(parsed, (dict, list)) else None
    except Exception as exc:
        log.debug("HTTP fetch failed %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# SWARM API (SQ G6160B at 192.168.254.60:8080)
# ---------------------------------------------------------------------------

def _plain_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text", "")).strip()
    return str(value).strip()


def _rows_by_name(table_json: Any) -> dict[str, dict[str, str]]:
    """Parse a SWARM table response into {row_name: {device, setpoint, actual}}."""
    rows: dict[str, dict[str, str]] = {}
    if not isinstance(table_json, dict):
        return rows
    table = table_json.get("text", {})
    data = table.get("data") if isinstance(table, dict) else None
    if not isinstance(data, list):
        return rows

    last_device = ""
    for row in data:
        if not isinstance(row, list):
            continue
        cells = [_plain_text(cell) for cell in row]
        if len(cells) == 4:
            device, name, setpoint, actual = cells
            if device:
                last_device = device
            rows[name.lower()] = {"device": last_device, "setpoint": setpoint, "actual": actual}
        elif len(cells) == 2:
            name, actual = cells
            rows[name.lower()] = {"device": last_device, "setpoint": "", "actual": actual}
        elif len(cells) == 3:
            device, name, actual = cells
            if device:
                last_device = device
            rows[f"{last_device} {name}".strip().lower()] = {
                "device": last_device, "setpoint": "", "actual": actual
            }
    return rows


def _fetch_sq_http_metrics(base_url: str = SQ_HTTP_BASE) -> dict[str, object]:
    """Read live SQ actuals from the instrument's SWARM HTTP API."""
    base = base_url.rstrip("/")
    output: dict[str, object] = {}

    actuals = _fetch_json(f"{base}/api/actual/FetchFullActualList")
    if actuals is not None:
        # Log raw response once so we can verify the parser is correct
        if not getattr(_fetch_sq_http_metrics, "_actuals_logged", False):
            _fetch_sq_http_metrics._actuals_logged = True  # type: ignore[attr-defined]
            log.info("SWARM FetchFullActualList raw (first call):\n%s",
                     json.dumps(actuals, default=str)[:4000])

        rows = _rows_by_name(actuals)
        if not rows:
            if not getattr(_fetch_sq_http_metrics, "_actuals_empty_warned", False):
                _fetch_sq_http_metrics._actuals_empty_warned = True  # type: ignore[attr-defined]
                log.warning("FetchFullActualList returned data but _rows_by_name parsed 0 rows "
                            "— response format may differ from expected table structure")

        high_vac_torr = _coerce_float(rows.get("high vac (torr)", {}).get("actual"))
        if high_vac_torr is not None:
            output["vacuum_level_mbar"] = high_vac_torr * 1.333223684

        gas_temp = _coerce_float(rows.get("gas temp (c)", {}).get("actual"))
        if gas_temp is not None:
            output["source_temperature_c"] = gas_temp
            output["drying_gas_temperature_c"] = gas_temp
        gas_temp_sp = _coerce_float(rows.get("gas temp (c)", {}).get("setpoint"))
        if gas_temp_sp is not None:
            output["source_temperature_setpoint_c"] = gas_temp_sp

        gas_flow = _coerce_float(rows.get("gas flow (l/min)", {}).get("actual"))
        if gas_flow is not None:
            output["drying_gas_flow_lpm"] = gas_flow

        nebulizer = _coerce_float(rows.get("nebulizer (psi)", {}).get("actual"))
        if nebulizer is not None:
            output["nebulizer_pressure_psig"] = nebulizer

        capillary_v = _coerce_float(rows.get("capillary voltage (v)", {}).get("actual"))
        if capillary_v is not None:
            output["hv_ready"] = abs(capillary_v) > 1000.0

    turbo = _fetch_json(f"{base}/api/actual/FetchTurboPumpState")
    if turbo is not None:
        if not getattr(_fetch_sq_http_metrics, "_turbo_logged", False):
            _fetch_sq_http_metrics._turbo_logged = True  # type: ignore[attr-defined]
            log.info("SWARM FetchTurboPumpState raw (first call):\n%s",
                     json.dumps(turbo, default=str)[:2000])

        rows = _rows_by_name(turbo)
        turbo_state = rows.get("turbo pump state", {}).get("actual", "").lower()
        turbo_speed = _coerce_float(rows.get("turbo pump percent speed (%)", {}).get("actual"))
        if turbo_state or turbo_speed is not None:
            output["turbopump_ready"] = turbo_state == "on" and (
                turbo_speed is None or turbo_speed >= 95.0
            )

    return output


# ---------------------------------------------------------------------------
# SignalBufferService (LC live signals via OpenLab at localhost:9753)
# ---------------------------------------------------------------------------

def _fetch_lc_signal_metrics() -> dict[str, object]:
    """Read LC actuals from the OpenLab SignalBufferService.

    The SignalBufferService at port 9753 is a WCF SOAP service (POST-only).
    Reverse-engineering the WSDL operation signatures is deferred; LC metrics
    (system_pressure_bar, flow_rate_ml_min, column_temperature_c) are left as
    "—" in the dashboard until a working client is implemented.
    """
    return {}


# ---------------------------------------------------------------------------
# Output file
# ---------------------------------------------------------------------------

def _write_sensor_file(data: dict[str, object]) -> None:
    data["written_at"] = datetime.now(timezone.utc).isoformat()
    tmp = SENSOR_DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, default=str, indent=2)
    os.replace(tmp, SENSOR_DATA_FILE)
    log.info("Wrote %d sensor keys to %s", len(data) - 1, SENSOR_DATA_FILE)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once() -> None:
    """Connect to InstrumentController, poll SWARM + SignalBuffer until disconnect."""
    ticket = _get_sdk_ticket()
    if not ticket:
        raise RuntimeError("Could not obtain SDK ticket")

    InstrumentController = _load_sdk()
    controller = InstrumentController()
    app_initialized = False
    state_changed = False

    def _on_state_change(sender, e):  # noqa: ARG001
        nonlocal state_changed
        state_changed = True
        log.info("InstrumentStateChange: %s", getattr(e, "State", "?"))

    def _on_app_initialized(sender, args):  # noqa: ARG001
        nonlocal app_initialized
        app_initialized = True
        log.info("AppInitialized")

    controller.InstrumentStateChangeEvent += _on_state_change  # type: ignore
    controller.AppInitialized += _on_app_initialized  # type: ignore

    log.info("Connecting to InstrumentController (conn=%s, instr=%s, proj=%s)...",
             CONN_STRING, INSTRUMENT_ID, PROJECT_ID)
    controller.EstablishConnection(CONN_STRING, ticket, INSTRUMENT_ID, PROJECT_ID)

    for _ in range(90):
        if controller.IsConnected and app_initialized:  # type: ignore
            break
        time.sleep(1)
    else:
        controller.Disconnect()  # type: ignore
        raise RuntimeError("Timed out waiting for InstrumentController AppInitialized")

    modules = list(controller.Modules)  # type: ignore
    log.info("InstrumentController ready. Modules: %s",
             [getattr(m, "Name", str(m)) for m in modules])

    while controller.IsConnected:  # type: ignore
        output: dict[str, object] = {}
        output.update(_fetch_sq_http_metrics())
        output.update(_fetch_lc_signal_metrics())

        if output:
            _write_sensor_file(output)
        else:
            log.warning(
                "No sensor data from SWARM or SignalBuffer — "
                "check instrument network connections"
            )

        state_changed = False
        for _ in range(POLL_INTERVAL_S):
            if state_changed:
                break
            time.sleep(1)

    log.warning("InstrumentController disconnected — will reconnect")
    controller.Disconnect()  # type: ignore


def main() -> None:
    log.info("hplcms_sensor_daemon starting (output=%s)", SENSOR_DATA_FILE)
    Path(SENSOR_DATA_FILE).parent.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            run_once()
        except ImportError as exc:
            log.error(
                "pythonnet / Agilent SDK not available: %s\n"
                "Run this script from the moses_v4_yoyo conda env.",
                exc,
            )
            time.sleep(RECONNECT_DELAY_S)
        except Exception as exc:
            log.error("run_once() failed: %s — retrying in %ds", exc, RECONNECT_DELAY_S)
            time.sleep(RECONNECT_DELAY_S)


if __name__ == "__main__":
    main()
