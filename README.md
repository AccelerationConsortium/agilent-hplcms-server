# agilent-hplcms-server

Status and control sidecar for the Agilent UPLC-MS instrument (`SDL2_LC1290`) on this lab PC. Runs alongside the existing `moses` Python controller and the always-on Agilent OpenLab CDS supervisor.

This repo conforms to lab status spec v1.1: see [`docs/STATUS_SPEC.md`](https://github.com/AccelerationConsortium/ac-organic-lab/blob/main/docs/STATUS_SPEC.md). v1.1 adds cooperative claims (`/control/claim` · `/control/heartbeat` · `/control/release`), `allowed_actions` on `/status`, and `details.claimed_by`. **Claims are hard-enforced**: mutating `/control/*` calls require a valid `X-Claim-Token` and are rejected with HTTP 423 Locked otherwise (read-only `GET /control/queue` and `POST /control/startup` stay open).

## Install / run

```powershell
# Install dependencies
C:\SDL_Tools\uv.exe sync --extra dev

# Run tests
C:\SDL_Tools\uv.exe run pytest -q

# If Windows temp/cache permissions block pytest on this PC:
C:\SDL_Tools\uv.exe run pytest -q --basetemp .tmp_pytest -p no:cacheprovider

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
| `GET /status` | `EquipmentStatus` envelope per STATUS_SPEC v1.1 (incl. `allowed_actions`, `details.claimed_by`) |
| `GET /openapi.json` | Generated OpenAPI spec |

### Control

Mutating endpoints (marked 🔒) require a valid `X-Claim-Token` header — acquire one with `POST /control/claim` first, or get HTTP 423 Locked.

| Endpoint | Description |
|---|---|
| `POST /control/claim` | Acquire the single instrument claim. Body `{owner, session_id, ttl_s}` → `{claim_token, heartbeat_interval_s, expires_at, role}`. 403 if `owner` is not on the roster; 409 if held by another session. |
| `POST /control/heartbeat` | Refresh the claim TTL (header `X-Claim-Token`). 204 on success; 401 if the token is unknown/expired. |
| `POST /control/release` | Release the claim (header `X-Claim-Token`). Idempotent — always 204. |
| `POST /control/startup` | Read-only readiness check — reports whether OpenLab processes are running. Never starts OpenLab. |
| 🔒 `POST /control/run` | Submit a run. Starts immediately if idle; queues behind the active run if busy. Returns `status: "accepted"` or `"queued"`. 409 `instrument_servicing` when a technician holds the instrument; 412 `queue_full` (with `Retry-After`) when the queue is at depth; 412 `reserved_for_robot` when a manual run targets the robot-reserved tray; 423 `workflow_active` when a workflow holds the lock. |
| 🔒 `POST /control/queue` | Submit a run and get back a `queue_id` for tracking. Same semantics as `/control/run` with a richer response. |
| `GET /control/queue` | View all jobs (pending, running, recent done/failed) plus `instrument_online` and `accepting_jobs` signals. |
| 🔒 `DELETE /control/queue/{queue_id}` | Cancel a pending job. 409 if it is currently running (use abort instead), 404 if already done. |
| 🔒 `POST /control/abort` | Abort the active run and clear the entire queue. |
| 🔒 `POST /control/standby` | Submit a low-flow standby job to park the instrument. Queues behind any active run. 412 `queue_full` when the queue is at depth. **Not a full shutdown** — powering the instrument down is a deliberate manual procedure at the instrument, not an API action. |
| 🔒 `POST /control/workflow/start` | Take the equipment-blocking workflow lock for a robot/agent campaign. **HTE platform users only** (else 403 `role_forbidden`). |
| 🔒 `POST /control/workflow/end` | Release the workflow lock (the claim is retained). Idempotent. |
| 🔒 `POST /control/service/start` | Enable service mode — halt the queue and refuse submissions while a technician uses OpenLab CDS. Persistent until cleared. **Admin (service) account only** (else 403). |
| 🔒 `POST /control/service/end` | Clear service mode and resume the queue. Idempotent. Admin-only. |

`GET /status.allowed_actions` reports which of `run.submit` · `run.abort` · `queue.cancel` · `instrument.standby` · `workflow.start` · `workflow.end` the device will currently honour, mirroring the control-side *state* precondition refusals (enqueue verbs drop out when the queue is full, OpenLab is down, or a technician is servicing; `workflow.start`/`workflow.end` toggle on workflow state). `service.*` is an operator/dashboard control rather than an agent skill, so it is reported via `details.service_mode` instead of `allowed_actions`.

### Sample submission & trays

A run carries a `plate_format` (`96-well` / `384-well`) and a list of samples addressed by **tray + well**; the sidecar composes the Agilent autosampler position (`{drawer}-{well}`, e.g. `D4B-A1`) for Moses and rejects off-plate wells with `422`.

```jsonc
{
  "output_dir": "C:/CDSProjects/Installation/Results/Batch",
  "plate_format": "96-well",
  "submitter": "manual",            // or "robot"
  "gradient": { /* ... */ },
  "samples": [
    {"sample_name": "cpd_01", "tray": "rear", "well": "A1", "injection_volume": 2.0}
  ]
}
```

**Tray reservation.** The **front** tray is reserved for robotic sample submission (`RESERVED_ROBOT_TRAY`, default `front`); manual runs use the rear tray. A run with `submitter != "robot"` that targets the reserved tray is refused with **412 `reserved_for_robot`**; a `submitter: "robot"` run is allowed in. Set `RESERVED_ROBOT_TRAY=""` to disable the reservation.

> The logical tray → Agilent drawer-code mapping is config: `TRAY_FRONT_DRAWER` (default `D1F`, the confirmed robot tray) and `TRAY_REAR_DRAWER` (default `D4B`, matches the existing example — confirm against this instrument's multisampler before deploying).

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
- **Layer 2 — Device state machine:** HTTP 409 `requires_init` if any OpenLab core process is missing; HTTP 409 `instrument_servicing` if a technician holds the instrument; HTTP 412 `queue_full` (with `Retry-After`) if the queue is at max depth (default 20); HTTP 423 `workflow_active` if a workflow holds the lock.
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

### Queue ownership and submission precedence

This server's `MosesRunner` is the **sole** job queue. OpenLab's native sequence queue is not used for our jobs — OpenLab (OLSS) is reserved for technician servicing/maintenance. Because `moses.agilent` `start_run` runs **synchronously** (it blocks through Running → run → Idle before returning), job state is driven entirely by the subprocess: alive → `running`, exit `0` → `done`, non-zero → `failed`. No `.sirslt` polling or OpenLab-queue tracking is involved.

Submissions are gated by precedence (highest wins):

1. **Technician servicing** — the queue is *halted* (the next pending job waits, it is not dropped) and new submissions are refused `409 instrument_servicing` (no `Retry-After` — duration is unpredictable). Two sources:
   - **Explicit service mode (primary).** A technician about to use OpenLab CDS directly flips a persistent flag via the dashboard → `POST /control/service/start` (and `…/service/end` to clear). It is *not* tied to a claim, so a dropped dashboard/claim never silently un-blocks a maintenance window — it stays on until explicitly cleared. Admin-only (see roles).
   - **Auto-detect (fallback)** for when nobody flips the switch: OLSS shows a real acquisition (a `runQueue.currentRun`, i.e. `olss_current_run` is present) while this server holds no active job, sustained over `SERVICING_DEBOUNCE_POLLS` `/status` observations. Keyed on `currentRun`, **not** bare `state=="Busy"`, so data analysis / reprocessing does *not* halt the queue.
2. **Workflow** — a robot/agent campaign (a series of runs) holding the equipment-blocking lock via `POST /control/workflow/start`. Non-holders are refused `423 workflow_active` (with `Retry-After`); only the lock holder submits. The lock rides on the claim, so it inherits TTL/heartbeat/auto-expiry — a crashed holder loses it.
3. **Our queue job running** — normal FIFO queue; ETA is bounded by the gradient `run_time`.
4. **Idle** — a single sample is submitted into the queue.

`details.service_mode` reflects the explicit flag; `details.servicing` reflects either source. A paused OpenLab sequence (`olss_software_status: "Paused"`) is reported as `equipment_status: "busy"` with `required_actions: ["resume_paused_sequence"]` — `paused` is not a legal v1.1 `EquipmentState`.

### Claims, roles, and the lab roster

Mutating `/control/*` calls require a valid `X-Claim-Token` (hard enforcement, `423` otherwise). A claim records its `owner`, and the device resolves the owner to a lab **role** from a configured roster (identity attribution, *not* authentication — the network ACL / dashboard login is the real access boundary). Capabilities by role:

| group (env) | role | `run.submit` | `workflow.start/end` | `service.start/end` |
|---|---|:--:|:--:|:--:|
| `HPLCMS_USERS` | `user` | ✓ | | |
| `HTE_USERS` | `automation` | ✓ | ✓ | |
| `HPLCMS_ADMINS` | `service` | ✓ | | ✓ |

An unknown owner is refused `403 user_not_recognized`; an under-privileged owner calling a gated action gets `403 role_forbidden`. The roster is **always enforced**: when every list is empty the built-in defaults (`Hplcms-User` / `HTE-User` / `Service-Account`) apply, so a fresh install always has a service account and never bricks. A literal `"*"` in a list matches any owner — an explicit open mode for dev, distinct from an accidental empty config. `HPLCMS_ADMINS` is seeded with the single `Service-Account` the dashboard claims under to toggle service mode; broadening it later is just adding names.

**Central roster (optional).** Set `ROSTER_URL` to the central auth service's owner→role projection (ac-organic-lab `GET /equipment/{key}/roster`) and the sidecar polls it every `ROSTER_REFRESH_INTERVAL_S` (default 60 s) — the central roster is then **authoritative** for owner→role resolution, so per-user roles are managed centrally instead of via the `*_USERS` env lists. The static env roster above becomes the fallback used **only until the first successful pull** (the device never bricks if the auth service is unreachable at startup); once a roster is pulled, a later refresh failure keeps the last-good copy. A successfully-pulled *empty* roster is authoritative (nobody is allowed). Leave `ROSTER_URL` unset to run fully standalone on the env lists. The pull is stdlib-only (`urllib`) and Tailnet-only by deployment; set `ROSTER_API_KEY` only if the central service ever gates the device-plane endpoint.

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
6. **OpenLab Sharing Services (OLSS) REST API** — `GET /status` and `GET /control/queue` include `instrument_state` (e.g. `"Idle"`, `"Running"`, `"Busy"`, `"Prerun"`, `"PostRun"`, `"Error"`, `"NotReady"`, `"NotConnected"`) from the live OpenLab CDS instrument, plus `olss_software_status` and `olss_current_run` in `details`. OLSS active states are treated as busy even for runs submitted directly in OpenLab.
7. **Sensor daemon JSON file** — `GET /status` → `metrics` includes live MS hardware metrics populated by `tools/hplcms_sensor_daemon.py`.
8. **`RCDriver.log`** (`C:\ProgramData\Agilent\LogFiles\LC Drivers\`) — parsed for two signal classes:
   - **Bottle fill levels** (`DoRequestResponse` + `BottleSolvents` XML): solvent A1/A2/B1/B2 and waste volumes. Written whenever OpenLab polls pump device settings (prerun, opening Bottle Fillings dialog, etc.). All `*RCDriver*.log` files are searched newest-first; data up to 7 days old is accepted (levels change slowly).
   - **Per-module STAT?** (`LDT SendInstruction`): individual `ready`/`busy`/`error` state for each LC module (pump, DAD, column thermostat, multisampler), plus DAD lamp hours, pump-on flag, drawer occupancy.

## Live hardware metrics

`GET /status` → `metrics` returns these keys. Each value is `{"value": …, "unit": "…"}`.  Keys absent from all sources are omitted from the response.

**MS (G6160B)**

| Key | Unit | Source |
|---|---|---|
| `turbopump_ready` | bool | G6160B SWARM API |
| `vacuum_level_mbar` | mbar | G6160B SWARM API |
| `source_temperature_c` | °C | G6160B SWARM API |
| `source_temperature_setpoint_c` | °C | G6160B SWARM API |
| `drying_gas_flow_lpm` | L/min | G6160B SWARM API |
| `drying_gas_temperature_c` | °C | G6160B SWARM API |
| `nebulizer_pressure_psig` | psig | G6160B SWARM API |
| `hv_ready` | bool | G6160B SWARM API (capillary voltage > 1 kV) |

**LC communication (derived from OLSS — always present when OpenLab is connected)**

| Key | Unit | Source |
|---|---|---|
| `ms_communication_ok` | bool | OLSS REST state |
| `pump_communication_ok` | bool | OLSS REST state |
| `autosampler_communication_ok` | bool | OLSS REST state |

**LC consumables (from `RCDriver.log` — updated whenever OpenLab polls the pump device settings)**

Agilent UI slot labels: A1 → `a1`, A2 → `a2`, B1 → `b1`, B2 → `b2`.  Slots with max capacity 0 (unconfigured) are omitted.

| Key | Unit | Notes |
|---|---|---|
| `solvent_a1_volume_ml` / `solvent_a1_capacity_ml` | mL | Bottle A1 |
| `solvent_a2_volume_ml` / `solvent_a2_capacity_ml` | mL | Bottle A2 (omitted if unconfigured) |
| `solvent_b1_volume_ml` / `solvent_b1_capacity_ml` | mL | Bottle B1 |
| `solvent_b2_volume_ml` / `solvent_b2_capacity_ml` | mL | Bottle B2 (omitted if unconfigured) |
| `solvent_a1_low` / `solvent_a2_low` / `solvent_b1_low` / `solvent_b2_low` | bool | True when volume ≤ not-ready limit (default 100 mL) |
| `waste_volume_ml` / `waste_capacity_ml` | mL | Waste bottle |
| `waste_near_capacity` | bool | True when waste ≥ not-ready limit (default 1900 mL) |

Low-level bottles appear in `required_actions` as `refill_solvent_a1`, `refill_solvent_b1`, etc.

**Not available**

| Key | Reason |
|---|---|
| `system_pressure_bar`, `flow_rate_ml_min`, `column_temperature_c` | OpenLab SignalBuffer (port 9753) — WCF/SOAP endpoint, REST sub-paths return 404 |
| `calibrant_ok`, `last_calibration_date`, `leak_detected` | No accessible source on this setup |

## Per-module LC components

`GET /status` → `components` includes one entry per LC module, populated from `RCDriver.log` `LDT SendInstruction` entries.

| Component key | Module | Extra info in `message` |
|---|---|---|
| `binary_pump` | G7120A | `"pumping"` / `"pump off"` |
| `dad_detector` | G7117B | lamp state + `NNN/2000h lamp` |
| `column_thermostat` | G7116B | `"thermostat on"` / `"thermostat off"` |
| `multisampler` | G7167B | `"N/M drawers occupied"` |

Each component has `state`: `ready` / `busy` / `error` / `not_ready` / `unknown`.

- While OLSS reports an active run (`Running`, `Busy`, etc.), all module states are forced to `"busy"`.
- When OLSS is idle, each module uses its own STAT? readiness flags (`READY` / `NOT_READY` / `ERROR`), ignoring stale run-phase tokens.

## Sensor daemon

The live MS hardware metrics come from a companion daemon that runs in the `moses_v4_yoyo` conda env and polls the SQ instrument directly. The sidecar reads the JSON file it writes; the sidecar never imports .NET or pythonnet.

**Data sources used by the daemon:**
- **SQ G6160B SWARM TCD API** (`http://192.168.254.60:8080`) — React app served on the SQ itself; the daemon polls `/api/actual/FetchFullActualList` and `/api/actual/FetchTurboPumpState` every 30 seconds.
- **OpenLab InstrumentController** (Named Pipe) — used only for connection events and to know OpenLab is online. Does not supply any sensor readings.

**Data sources confirmed unavailable:**
- OpenLab SignalBufferService (`DESKTOP-V2PV40S:9753`) — WCF/SOAP POST-only endpoint; REST sub-paths return 404. Would give live pressure, flow rate, column temperature. Deferred.
- LC module hardware (`192.168.254.59`) — no HTTP API on that LAN card; telnet port 23 is LAN config only.

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
| `paused` | OLSS reports `olss_software_status: "Paused"` while OpenLab is connected. Response includes `required_actions: ["resume_paused_sequence"]`. |
| `busy` | Newest `*.sirslt` mtime within `BUSY_THRESHOLD_S`, a moses-env `python.exe` running, server-managed run active, or OLSS instrument state is `Run`, `Running`, `Busy`, `Prerun`, or `PostRun`. |
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
| `QUEUE_FULL_RETRY_AFTER_S` | `60` | Advisory `Retry-After` (s) returned with 412 `queue_full`. |
| `WORKFLOW_ACTIVE_RETRY_AFTER_S` | `60` | Advisory `Retry-After` (s) returned with 423 `workflow_active`. |
| `SERVICING_DEBOUNCE_POLLS` | `2` | Consecutive `/status` observations of a real OLSS run (no active job) before auto-detect declares servicing. |
| `HPLCMS_USERS` | `Hplcms-User` | Roster: owners with role `user` (submit samples). Comma-separated; `"*"` = any owner. |
| `HTE_USERS` | `HTE-User` | Roster: owners with role `automation` (submit + `workflow.*`). |
| `HPLCMS_ADMINS` | `Service-Account` | Roster: owners with role `service` (submit + `service.*`). |
| `ROSTER_URL` | _(empty)_ | Central roster projection URL (`…/equipment/{key}/roster`). Set → central is authoritative for owner→role; empty → static env roster only. |
| `ROSTER_REFRESH_INTERVAL_S` | `60` | How often to re-pull the central roster. |
| `ROSTER_HTTP_TIMEOUT_S` | `5` | Timeout for a single central-roster pull. |
| `ROSTER_API_KEY` | _(empty)_ | Optional `X-Api-Key` sent with the roster pull (endpoint is Tailnet-only by default). |

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
    "plate_format": "96-well",
    "submitter": "manual",
    "samples": [
        {"sample_name": "cpd_01", "tray": "rear", "well": "A1", "injection_volume": 2.0}
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

- [`STATUS_SPEC.md`](https://github.com/AccelerationConsortium/ac-organic-lab/blob/main/docs/STATUS_SPEC.md) — v1.1 contract this repo implements.
- [`INTERLOCKS.md`](https://github.com/AccelerationConsortium/ac-organic-lab/blob/main/docs/INTERLOCKS.md) — interlock layer design this server conforms to.
- [`DEVICE_PC_SETUP.md`](https://github.com/AccelerationConsortium/ac-organic-lab/blob/main/docs/DEVICE_PC_SETUP.md) — canonical Windows install recipe (uv at `C:\SDL_Tools\uv.exe`, NSSM, lab-user run, log paths).
