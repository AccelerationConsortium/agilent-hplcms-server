# agilent-hplcms-server

Status and control sidecar for the Agilent UPLC-MS instrument (`SDL2_LC1290`) on this lab PC. Runs alongside the existing `moses` Python controller and the always-on Agilent OpenLab CDS supervisor.

Conforms to the AC Organic Self-Driving Lab status contract: see [`docs/STATUS_SPEC.md`](https://github.com/AccelerationConsortium/ac-organic-lab/blob/main/docs/STATUS_SPEC.md) (v1.0).

## Install / run

```powershell
# Install dependencies
C:\SDL_Tools\uv.exe sync --extra dev

# Run tests
C:\SDL_Tools\uv.exe run pytest -q

# Start the server (foreground)
C:\SDL_Tools\uv.exe run agilent-hplcms-server-serve --host 0.0.0.0 --port 8010
```

The server runs as the NSSM Windows service `hplc-ms-status` (Automatic startup). To restart after a code change:

```powershell
Start-Process powershell -Verb RunAs -ArgumentList "-Command C:\SDL_Tools\nssm.exe restart hplc-ms-status"
```

## Endpoints

### Status (read-only)

| Endpoint | Returns |
|---|---|
| `GET /` | `{equipment_id, equipment_name, protocol_version}` |
| `GET /health` | `{status: "healthy"}` |
| `GET /status` | `EquipmentStatus` envelope per STATUS_SPEC v1.0 |
| `GET /openapi.json` | Generated OpenAPI spec |

### Control

| Endpoint | Description |
|---|---|
| `POST /control/startup` | Read-only readiness check — reports whether OpenLab processes are running. Never starts OpenLab. |
| `POST /control/run` | Submit a run. Starts immediately if idle; queues behind the active run if busy. Returns `status: "accepted"` or `"queued"`. |
| `POST /control/queue` | Submit a run and get back a `queue_id` for tracking. Same semantics as `/control/run` with a richer response. |
| `GET /control/queue` | View all jobs (pending, running, recent done/failed) plus `instrument_online` and `accepting_jobs` signals. |
| `DELETE /control/queue/{queue_id}` | Cancel a pending job. 409 if it is currently running (use abort instead), 404 if already done. |
| `POST /control/abort` | Abort the active run and clear the entire queue. |
| `POST /control/shutdown` | Submit a low-flow standby job to park the instrument. Queues behind any active run. |

## Loopback verification

```powershell
curl http://127.0.0.1:8010/health
curl http://127.0.0.1:8010/status
curl -X POST http://127.0.0.1:8010/control/startup
curl http://127.0.0.1:8010/control/queue
```

Tailscale (from another tailnet device):

```powershell
curl http://sdl2-pc-06-uplc.tail6a1dd7.ts.net:8010/health
curl http://sdl2-pc-06-uplc.tail6a1dd7.ts.net:8010/status
```

## Safety model

Per [`INTERLOCKS.md`](https://github.com/AccelerationConsortium/ac-organic-lab/blob/main/docs/INTERLOCKS.md):

- **Layer 1 — Hardware limits:** Pydantic field validators on all numeric parameters (e.g. injection volume ≤ 20 µL, run time ≤ 120 min, flow rate ≤ 2 mL/min). Violations → HTTP 422.
- **Layer 2 — Device state machine:** HTTP 409 `requires_init` if any OpenLab core process is missing. HTTP 409 `queue_full` if the queue is at max depth (default 20).
- Moses is **never imported** — only called as a subprocess from the `moses_v4_yoyo` conda env. The sidecar stays in its own venv with no vendor dependencies.
- A **script allowlist** (`MOSES_ALLOWED_SCRIPTS`) prevents arbitrary script execution.

## How the queue works

```
POST /control/queue   → {"queue_id": "…", "position": 0, "status": "queued", "message": "…"}
GET  /control/queue   → {"queue": [...], "active_run_id": "…", "pending_count": 1,
                         "instrument_online": true, "accepting_jobs": true, ...}
DELETE /control/queue/{queue_id}  → cancel a pending job (409 if running, 404 if done)
```

- `position: 0` means the run started immediately (instrument was idle).
- `position: 1+` means the run is waiting; position is 1-based in the FIFO.
- `queue` contains all jobs in any status (pending, running, done, failed) up to the last 50 completed.
- `instrument_online` — all three OpenLab core processes are up.
- `accepting_jobs` — instrument is online and the queue is not full.
- `GET /status` → `details.queue_length` gives the current pending count.

`POST /control/run` is a convenience shorthand that returns `status: "accepted"` (started) or `"queued"`.

A background daemon thread polls every 5 seconds and automatically starts the next pending run when the active one finishes. `POST /control/abort` terminates the active process and marks all pending jobs as failed.

## What the server never does

- Does **not** import or share an environment with `moses`.
- Does **not** open any session against the Agilent OpenLab CDS .NET SDK, named pipe, instrument, serial port, or COM port.
- Does **not** modify any vendor configuration.
- `GET /status` is always side-effect-free and always returns HTTP 200 (`requires_init`, `error`, etc. are reported in-band).

## Status probe sources

1. OS process presence of OpenLab CDS supervisor processes (`psutil`).
2. Newest `*.sirslt` directory mtime under `C:\CDSProjects\Installation\Results\` — the strongest "writing data now" signal.
3. Any `python.exe` under `C:\Users\sdl2\anaconda3\envs\moses*\` currently running.
4. Trailing bytes of `C:\ProgramData\Agilent\LogFiles\InstrumentService.log` and the newest `AcquisitionServer-*.log` for recent `ERROR` / `CRITICAL` / `FATAL` events.
5. Server-managed runner state — if a run was just submitted, `busy` is forced immediately (before the `*.sirslt` directory appears on disk).
6. **OpenLab Sharing Services (OLSS) REST API** — `GET /status` and `GET /control/queue` include `instrument_state` (e.g. `"Idle"`, `"Error"`, `"NotReady"`, `"NotConnected"`) from the live OpenLab CDS instrument, plus `olss_software_status` and `olss_current_run` in `details`.
7. **Sensor daemon JSON file** — `GET /status` → `metrics` includes live hardware metrics populated by `tools/hplcms_sensor_daemon.py`. Keys absent from the file render as `"—"` in the dashboard.

## Live hardware metrics

`GET /status` → `metrics` returns these keys. Each value is `{"value": …, "unit": "…"}`.

| Key | Unit | Live? | Source |
|---|---|---|---|
| `turbopump_ready` | bool | ✅ | G6160B SWARM API |
| `vacuum_level_mbar` | mbar | ✅ | G6160B SWARM API |
| `source_temperature_c` | °C | ✅ | G6160B SWARM API (drying gas temp) |
| `source_temperature_setpoint_c` | °C | ✅ | G6160B SWARM API |
| `drying_gas_flow_lpm` | L/min | ✅ | G6160B SWARM API |
| `drying_gas_temperature_c` | °C | ✅ | G6160B SWARM API |
| `nebulizer_pressure_psig` | psig | ✅ | G6160B SWARM API |
| `hv_ready` | bool | ✅ | G6160B SWARM API (capillary voltage > 1 kV) |
| `ms_communication_ok` | bool | ✅ | OLSS REST state (no sensor daemon needed) |
| `pump_communication_ok` | bool | ✅ | OLSS REST state |
| `autosampler_communication_ok` | bool | ✅ | OLSS REST state |
| `system_pressure_bar` | bar | — | OpenLab SignalBuffer (WCF/SOAP, not yet wired) |
| `flow_rate_ml_min` | mL/min | — | OpenLab SignalBuffer (WCF/SOAP, not yet wired) |
| `column_temperature_c` | °C | — | OpenLab SignalBuffer (WCF/SOAP, not yet wired) |
| `column_temperature_setpoint_c` | °C | — | not available |
| `system_pressure_limit_bar` | bar | — | not available |
| `degasser_active` | bool | — | not available |
| `solvent_a_volume_ml` | mL | — | `SolventSensingSupported = False` on this setup |
| `solvent_b_volume_ml` | mL | — | `SolventSensingSupported = False` on this setup |
| `wash_solvent_volume_ml` | mL | — | `SolventSensingSupported = False` on this setup |
| `waste_volume_ml` | mL | — | `SolventSensingSupported = False` on this setup |
| `waste_capacity_ml` | mL | — | `SolventSensingSupported = False` on this setup |
| `calibrant_ok` | bool | — | not available |
| `last_calibration_date` | ISO 8601 | — | not available |
| `leak_detected` | bool | — | not available |

"—" keys are omitted from the response and displayed as "—" in the dashboard.

## Sensor daemon

The live MS hardware metrics come from a companion daemon that runs in the `moses_v4_yoyo` conda env and polls the SQ instrument directly. The sidecar reads the JSON file it writes; the sidecar never imports .NET or pythonnet.

**Data sources used by the daemon:**
- **SQ G6160B SWARM TCD API** (`http://192.168.254.60:8080`) — React app served on the SQ itself; the daemon polls `/api/actual/FetchFullActualList` and `/api/actual/FetchTurboPumpState` every 30 seconds.
- **OpenLab InstrumentController** (Named Pipe) — used only for connection events and to know OpenLab is online. Does not supply any sensor readings.

**Data sources confirmed unavailable:**
- OpenLab SignalBufferService (`DESKTOP-V2PV40S:9753`) — WCF/SOAP POST-only endpoint; REST sub-paths return 404. Deferred.
- LC module hardware (`192.168.254.59`) — no HTTP API on that LAN card; telnet port 23 is LAN config only.
- Solvent/waste volumes — `SolventSensingSupported = False` on this OpenLab configuration.

### Run the daemon

```powershell
cd C:\Users\sdl2\Documents\Code\yoyo\pythofisher_hplcms
C:\Users\sdl2\anaconda3\envs\moses_v4_yoyo\python.exe `
    C:\Users\sdl2\Projects\agilent-hplcms-server\tools\hplcms_sensor_daemon.py
```

The working directory matters — Moses path discovery looks for `src/` relative to it.

### Install as NSSM service

```powershell
C:\SDL_Tools\nssm.exe install hplc-ms-sensors `
    C:\Users\sdl2\anaconda3\envs\moses_v4_yoyo\python.exe `
    C:\Users\sdl2\Projects\agilent-hplcms-server\tools\hplcms_sensor_daemon.py
C:\SDL_Tools\nssm.exe set hplc-ms-sensors AppDirectory `
    C:\Users\sdl2\Documents\Code\yoyo\pythofisher_hplcms
C:\SDL_Tools\nssm.exe set hplc-ms-sensors Start SERVICE_AUTO_START
Start-Process powershell -Verb RunAs -ArgumentList `
    "-Command C:\SDL_Tools\nssm.exe start hplc-ms-sensors"
```

Writes to `C:\SDL_Tools\hplcms_sensor_data.json` every 30 seconds (override via `SENSOR_DATA_FILE` env var). Logs to `C:\ProgramData\Agilent\LogFiles\hplcms_sensor_daemon.log`.

## State mapping

| `equipment_status` | Trigger |
|---|---|
| `requires_init` | Any required OpenLab core process missing. |
| `error` | An `ERROR` / `CRITICAL` / `FATAL` event in the last `ERROR_WINDOW_S` of OpenLab logs. |
| `busy` | Newest `*.sirslt` mtime within `BUSY_THRESHOLD_S`, a moses-env `python.exe` running, or server-managed run active. |
| `ready` | OpenLab core processes up, no recent error, no recent acquisition activity, no active run. |
| `unknown` | Probe could not stat the OpenLab log dir or the CDS results dir. |

## Configuration (env vars)

### Status probe

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8010` | Bind port for uvicorn. |
| `HOST` | `0.0.0.0` | Bind host for uvicorn. |
| `DASHBOARD_ORIGIN` | `*` | CORS allow origin. Set to dashboard URL in production. |
| `OPENLAB_LOG_DIR` | `C:\ProgramData\Agilent\LogFiles` | Where OpenLab writes live logs. |
| `CDS_RESULTS_DIR` | `C:\CDSProjects\Installation\Results` | Acquisition output root (`*.sirslt` directories). |
| `MOSES_ENV_GLOB` | `C:\Users\sdl2\anaconda3\envs\moses*` | Glob matched against `python.exe` ExecutablePath. |
| `BUSY_THRESHOLD_S` | `90` | `*.sirslt` mtime within this many seconds → `busy`. |
| `ERROR_WINDOW_S` | `300` | Look-back window for tail-log error severity. |
| `OPENLAB_INSTRUMENT_NAME` | `SDL2_LC1290` | Surfaced in `details.instrument_label`. |
| `OPENLAB_OLSS_URL` | `http://localhost:6625/olss` | Base URL of the OpenLab Sharing Services REST API. |
| `OPENLAB_USERNAME` | `sdl2` | Username for OLSS login (empty password, no-auth mode). |
| `OPENLAB_INSTRUMENT_ID` | `15` | Numeric OLSS instrument ID for SDL2_LC1290. |
| `SENSOR_DATA_FILE` | `C:\SDL_Tools\hplcms_sensor_data.json` | JSON file written by the sensor daemon; absent → metrics show as `"—"`. |

### Control / queue

| Variable | Default | Purpose |
|---|---|---|
| `MOSES_WORK_DIR` | `C:\Users\sdl2\Documents\Code\yoyo\pythofisher_hplcms` | Working directory for Moses subprocess. |
| `MOSES_PYTHON_EXE` | `C:\Users\sdl2\anaconda3\envs\moses_v4_yoyo\python.exe` | Python interpreter for the Moses env. |
| `MOSES_ALLOWED_SCRIPTS` | `examples/agent_agilent.py` | Comma-separated allowlist of scripts (relative to `MOSES_WORK_DIR`) that can be submitted via `/control/run`. |
| `RUN_JOBS_DIR` | `C:\SDL_Tools\hplcms_jobs` | Persistent directory for job JSON files (kept for post-mortem). |
| `QUEUE_MAX_DEPTH` | `20` | Maximum number of runs that can be pending in the queue. |
| `QUEUE_POLL_INTERVAL_S` | `5` | How often the background thread checks if the active run finished. |

### Sensor daemon

| Variable | Default | Purpose |
|---|---|---|
| `SENSOR_DATA_FILE` | `C:\SDL_Tools\hplcms_sensor_data.json` | Output file path. |
| `SENSOR_DAEMON_LOG` | `C:\ProgramData\Agilent\LogFiles\hplcms_sensor_daemon.log` | Daemon log file. |
| `SENSOR_POLL_INTERVAL_S` | `30` | Poll interval in seconds. |
| `SENSOR_RECONNECT_DELAY_S` | `60` | Delay before reconnecting after a dropped InstrumentController connection. |
| `SQ_HTTP_BASE` | `http://192.168.254.60:8080` | SWARM TCD HTTP API base URL on the SQ. |
| `OPENLAB_INSTRUMENT_ID` | `15` | OLSS instrument ID (used for InstrumentController connection). |

## Client-side integration

```python
import httpx, time

BASE = "http://sdl2-pc-06-uplc.tail6a1dd7.ts.net:8010"

# 1. Check readiness
r = httpx.post(f"{BASE}/control/startup")
assert r.json()["status"] == "ready"

# 2. Submit run via the queue API (gets a queue_id for tracking)
job = {
    "output_dir": "C:/CDSProjects/Installation/Results/MyBatch",
    "ms_mode": "positive_negative",
    "standby_after": True,
    "gradient": {
        "name": "standard_10min",
        "solvent_a": "H2O_0.1%FA", "solvent_b": "ACN_0.1%FA",
        "run_time": 10.0, "flow_rate": 0.6, "equilibration_time": 1.0,
        "gradient_table": [[0.0,0.05],[1.0,0.05],[7.0,1.0],[9.8,1.0],[9.9,0.05]]
    },
    "samples": [
        {"sample_name": "cpd_01", "sample_position": "D4B-A1", "injection_volume": 2.0}
    ]
}
r = httpx.post(f"{BASE}/control/queue", json=job, timeout=10)
r.raise_for_status()   # 422 = bad params, 409 = requires_init or queue_full
queue_id = r.json()["queue_id"]
position  = r.json()["position"]   # 0 = started immediately, 1+ = waiting

# 3. Poll queue until our job is done
while True:
    q = httpx.get(f"{BASE}/control/queue").json()
    our_job = next((j for j in q["queue"] if j["queue_id"] == queue_id), None)
    if our_job and our_job["status"] in ("done", "failed"):
        break
    time.sleep(30)

# 4. Cancel if needed before it runs
# httpx.delete(f"{BASE}/control/queue/{queue_id}")
```

## See also

- [`STATUS_SPEC.md`](https://github.com/AccelerationConsortium/ac-organic-lab/blob/main/docs/STATUS_SPEC.md) — v1.0 contract this repo implements.
- [`INTERLOCKS.md`](https://github.com/AccelerationConsortium/ac-organic-lab/blob/main/docs/INTERLOCKS.md) — interlock layer design this server conforms to.
- [`DEVICE_PC_SETUP.md`](https://github.com/AccelerationConsortium/ac-organic-lab/blob/main/docs/DEVICE_PC_SETUP.md) — canonical Windows install recipe (uv at `C:\SDL_Tools\uv.exe`, NSSM, lab-user run, log paths).
