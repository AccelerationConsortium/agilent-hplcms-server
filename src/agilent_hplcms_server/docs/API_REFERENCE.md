# Agilent UPLC-MS — API reference

Base: the deployed service's `http://<host>:8010`. All timestamps UTC ISO-8601.
Exact request/response schemas: `/openapi.json`. Tags: `control`,
`documentation`.

Every mutating `/control/*` route takes a required `X-Claim-Token` header
("claim" in the *gate* column). The exceptions are called out: `POST
/control/startup`, `GET /control/queue` and the three claim-protocol routes.
Error bodies are `{"detail": {...}}` — the model sits under `detail`, which the
generated OpenAPI document reflects — except request validation, which is
`{"detail": [...]}`. Branch on `detail.error`, never on prose.

## Read (no claim, side-effect-free)

| method + path | returns |
|---|---|
| `GET /` | `ProbeResponse` — `{equipment_id: "agilent_uplc_ms", equipment_name: "Agilent UPLC-MS", protocol_version: "1.2"}` |
| `GET /health` | `{"status": "healthy"}` — service liveness only, says nothing about the instrument |
| `GET /status` | the full `EquipmentStatus` envelope (below) |
| `GET /docs/agent` | the JSON agent guide: `title`, `protocol_version`, `reference`, `execution_policy`, `workflow`, `sample_addressing`, `dispatch`, `refusals`, `cancellation`, `capabilities`. Static; no hardware access. |
| `GET /agent-docs` | this service's agent guide (`text/markdown`) |
| `GET /agent-docs/api-reference` | this document (`text/markdown`) |
| `GET /llms.txt` | discovery index (`text/plain`) |
| `GET /openapi.json`, `GET /docs` | OpenAPI document, Swagger UI |

### `GET /status` envelope

`protocol_version` (`"1.2"`), `equipment_id` (`"agilent_uplc_ms"`),
`equipment_name`, `equipment_kind` (`"hplc"`), `equipment_version`, `host`,
`equipment_status`, `message`, `required_actions`, `allowed_actions`,
`activity`, `activity_since`, `device_time`, `components`, `metrics`,
`last_error`, `details`.

`equipment_status` ∈ `unknown`, `requires_init`, `error`, `busy`, `degraded`,
`ready`. `activity` ∈ `running`, `idle`, `unknown`.

| `components` key | states |
|---|---|
| `openlab_acquisition`, `openlab_instrument_service`, `openlab_reverse_proxy` | `running` / `stopped` |
| `moses_controller` | `running` / `idle` |
| `hplc`, `ms` | OLSS state mapped: `ready`, `not_ready`, `busy`, `paused`, `error`, `stopped`; mirrors `equipment_status` when OLSS was not read |
| `binary_pump`, `dad_detector`, `column_thermostat`, `multisampler` | `ready`, `not_ready`, `busy`, `error`, `unknown` — **present only when the RC driver log has data for that module** |

`metrics` keys are included only when a value was read. Comms (`ms_`, `pump_`,
`autosampler_communication_ok`, booleans from OLSS); MS source and vacuum
(`turbopump_ready`, `vacuum_level_mbar`, `source_temperature_c`,
`source_temperature_setpoint_c`, `drying_gas_flow_lpm`,
`drying_gas_temperature_c`, `nebulizer_pressure_psig`, `hv_ready`); post-run
pressure QC from the archived `.dx` traces, describing the **last completed
run** (`run_pressure_max_bar`, `_min_bar`, `_mean_bar`, `_baseline_bar`,
`_delta_pct`); live LC (`system_pressure_bar`, `system_pressure_limit_bar`,
`column_temperature_c`, `column_temperature_setpoint_c`, `flow_rate_ml_min`,
`degasser_active`); consumables (`solvent_{a1,a2,b1,b2}_volume_ml`,
`_capacity_ml`, `_low`, `wash_solvent_volume_ml`, `waste_volume_ml`,
`waste_capacity_ml`, `waste_near_capacity`, `calibrant_ok`); and
`last_calibration_date`. There is **no run counter and no
`cycles_total`**.

`details` (present-when-known): `instrument_label`, `openlab_log_dir`,
`cds_results_dir`, `probe_version`, `probe_observed_at`, `busy_threshold_s`,
`error_window_s`, `queue_length`, `last_run_dir`, `last_run_mtime`,
`moses_process_pid`, `last_error_log_path`, `olss_instrument_state`,
`olss_software_status`, `olss_current_run`, `olss_error`,
`rc_driver_data_age_s`, `lc_faults`, `fault_acks`, `run_pressure`,
`waste_near_capacity`, `solvent_<slot>_low`, `waste_reset_at`,
`solvent_<slot>_reset_at`, `claimed_by` (always present; `null` when
unclaimed), `service_mode`, `servicing`, `workflow_active`,
`subsystem_fault_modules`, `stale_active_run` (only when `true`: the active
job has shown no OpenLab acquisition for `STALE_RUN_GRACE_S`).

## Claim protocol (no `X-Claim-Token` gate — these establish it)

| method + path | body / header | responses |
|---|---|---|
| `POST /control/claim` | `{owner, session_id, ttl_s?}` | **200** `{claim_token, heartbeat_interval_s, expires_at, role}`. `ttl_s` defaults to 30 s, clamped 5–300; `heartbeat_interval_s` is half the granted TTL. Idempotent for the same `session_id`. **403** `user_not_recognized` `{detail, owner}` when the owner is off the roster. **409** `{detail, claimed_by, retry_after_s}` when another live session holds it. |
| `POST /control/heartbeat` | header `X-Claim-Token` | **204** No Content. **401** `{detail, claimed_by}` when the token is unknown, expired or from another session — the claim is lost. |
| `POST /control/release` | header `X-Claim-Token` | **204**, idempotent: an unknown or already-released token also returns 204. |

## Run submission

Both submission routes take the same `RunRequest` body, apply the same gates in
the same order, and answer **202**.

`RunRequest`:

| field | type | notes |
|---|---|---|
| `script_name` | str | default `examples/agent_agilent.py`; must be in `MOSES_ALLOWED_SCRIPTS` and exist under `MOSES_WORK_DIR`. **Refused (422) if set at all when `dispatch: "openlab"`.** |
| `instrument_config_path` | str | default `examples/hh_472_config.json` |
| `output_dir` | str | **required**; absolute path on the instrument PC |
| `ms_mode` | `positive` \| `negative` \| `positive_negative` | default `positive_negative` |
| `standby_after` | bool | default `true` |
| `standby_config` | object \| null | default `null` — the trailing low-flow park runs on the dispatch script's own defaults. Supply to shape it: `{flow_rate (mL/min, 0 < f ≤ 2, default 0.01), run_time (min, 0 < t ≤ 120, default 1.0), fraction_b (0.0–1.0, default 0.5), sample_position (default `"1"`; a bare vial, **not** a `D[1-4][FB]-<well>` address), ms_mode (default `positive_negative`)}`. **Refused (422) together with `standby_after: false`** — the park would never run. Lets one job be "analytical run, then park" instead of two separately-queued jobs. |
| `gradient` | object | `{name, solvent_a, solvent_b, run_time (min, 0 < t ≤ 120), flow_rate (mL/min, 0 < f ≤ 2), gradient_table: [[time_min, fraction_b]], equilibration_time (0–30 min, default 0)}` |
| `samples` | list | at least one `{sample_name (alphanumeric/`_`/`-`, ≤64), sample_position (`D[1-4][FB]-<well>`, e.g. `D1B-A1`), injection_volume (µL, 0 < v ≤ 20)}` |
| `plate_format` | str \| null | asserted against the drawer's configured labware. Canonical: `96-well`, `384-well`, `54-vial`. Null trusts the configured labware (and assumes `96-well` for the built-in check when none is configured). |
| `submitter` | `manual` \| `robot` | default `manual`; only `robot` may target `RESERVED_ROBOT_DRAWER` |
| `dispatch` | `sidecar` \| `openlab` | default `sidecar` |

| method + path | gate | returns | refusals |
|---|---|---|---|
| `POST /control/run` | claim | **202** `RunResponse` `{run_id, status: accepted\|queued\|dispatching, message, pid?, started_at?, queue_position?}` — `accepted` started immediately, `queued` carries a 1-based `queue_position`, `dispatching` is the OpenLab handoff | 409 `requires_init` · 409 `instrument_servicing` (explicit service mode only) · 409 `subsystem_fault` · 412 `reserved_for_robot` · 412 `queue_full` (+`Retry-After`) · 412 `dispatch_in_progress` (+`Retry-After`, openlab only) · 422 `plate_mismatch` · 422 validation / script not allowlisted or missing · 423 claim · 423 `workflow_active` (+`Retry-After`) |
| `POST /control/queue` | claim | **202** `QueueResponse` `{queue_id, position, status: queued\|dispatching, message}` — `position` 0 means it started immediately, otherwise 1-based | identical to `POST /control/run` |

Gate order is: claim → `requires_init` → service mode → subsystem fault →
reserved drawer → labware → enqueue (queue full / script allowlist / handoff in
flight). Auto-detected servicing does **not** refuse here: the job is accepted
and parked until the instrument frees.

## Queue and lifecycle

| method + path | gate | returns | refusals |
|---|---|---|---|
| `POST /control/startup` | **none** | **200** `StartupResponse` `{status: ready\|requires_init, message, missing_processes}` | none — a read-only readiness check that never starts OpenLab and never refuses |
| `GET /control/queue` | **none** | **200** `QueueStatusResponse` `{queue: [QueuedRun], active_run_id, pending_count, max_depth, instrument_online, accepting_jobs, dispatch_held_reason: service_mode\|servicing\|null, instrument_state, stale_active_run}` | none |
| `DELETE /control/queue/{queue_id}` | claim | **200** `{cancelled_id, message}` | 404 (unknown or already finished; plain string `detail`) · 409 (the job is running — use `POST /control/abort`; plain string `detail`) · 423 claim |
| `POST /control/abort` | claim | **200** `AbortResponse` `{status: aborted\|not_running, message, run_id, queue_cleared}` — kills the active `moses` process **and** clears every pending job | 423 claim |
| `POST /control/standby` | claim | **202** `StandbyResponse` `{run_id, status: accepted\|queued, message, queue_position?}` — enqueues a low-flow park job; **not** an instrument shutdown | 409 `requires_init` · 409 `instrument_servicing` (**either** source, unlike a run) · 409 `subsystem_fault` · 412 `queue_full` (+`Retry-After`) · 423 claim · 423 `workflow_active` |

`QueuedRun`: `{queue_id, request, queued_at, status, dispatch, started_at,
finished_at, pid, error_message}`. `status` is `pending` → `running` → `done` |
`failed` for `dispatch: "sidecar"` (process-exit authoritative), and
`dispatching` → `handed_off` | `failed` for `dispatch: "openlab"`.
`error_message` is always set on `failed`, and also carries the standby-park
warning on a job finalized `done`.

## Workflow lock

| method + path | gate | returns | refusals |
|---|---|---|---|
| `POST /control/workflow/start` | claim + `automation` role | **200** `{status: "workflow_started", message, expires_at, heartbeat_interval_s}` | 403 `role_forbidden` (`required_role: "automation"`) · 409 `requires_init` · 409 `instrument_servicing` (either source) · 409 `subsystem_fault` · 423 claim |
| `POST /control/workflow/end` | claim | **200** `{status: "workflow_ended", message}` — idempotent | 423 claim |

While the lock is held, every other `/control/*` caller is refused **423**
`workflow_active` with `claimed_by` and an advisory `Retry-After`
(`WORKFLOW_ACTIVE_RETRY_AFTER_S`, default 60 s).

## Service mode

| method + path | gate | returns | refusals |
|---|---|---|---|
| `POST /control/service/start` | claim + `service` role | **200** `{status: "service_mode_on", service_mode: true, message}` | 403 `role_forbidden` (`required_role: "service"`) · 423 claim |
| `POST /control/service/end` | claim + `service` role | **200** `{status: "service_mode_off", service_mode: false, message}` — idempotent | 403 `role_forbidden` · 423 claim |

The flag is persistent and **not** claim-bound: it survives the claim being
released and stays set until explicitly cleared.

## Consumable acknowledgments (any valid claim holder)

| method + path | gate | returns | refusals |
|---|---|---|---|
| `POST /control/consumables/waste/reset` | claim | **200** `ConsumableResetResponse` `{consumable, raw_at_ack_ml, acked_at, warning_suppressed, message}` | 423 claim |
| `POST /control/consumables/solvent/{slot}/reset` | claim | same, with `consumable` = the slot | 404 when `slot` is not `a1`/`a2`/`b1`/`b2` (checked **before** the claim; plain string `detail`) · 423 claim |

Suppression lasts until OpenLab's raw estimate moves
`CONSUMABLE_REARM_DELTA_ML` (default 200 mL) past the level recorded at
acknowledgment. `warning_suppressed: false` in the response means the estimate
already shows the condition is due again.

## LC module fault acknowledgments (`user` or `service` role)

`{role}` is one of `binary_pump`, `dad_detector`, `column_thermostat`,
`multisampler`.

| method + path | gate | returns | refusals |
|---|---|---|---|
| `POST /control/faults/{role}/ack` | claim + `user` or `service` role | **200** `FaultAckResponse` `{module, acked_at, faults_through, stat_through, fault_cleared, faulted_modules, message}`. Optional query parameter `note`. | 404 unknown module (checked **before** the claim; plain string `detail`) · 403 `role_forbidden` · 423 claim |
| `DELETE /control/faults/{role}/ack` | claim + `user` or `service` role | **200** `FaultAckResponse` — withdraws the acknowledgment; any fault still inside `LC_FAULT_WINDOW_S` applies again | 404 unknown module, or no acknowledgment recorded · 403 `role_forbidden` · 423 claim |

`fault_cleared: false` on a successful ack means a *newer* fault is already
outstanding — the module has not recovered, and `run.submit` stays refused.

## Refusal bodies

| `detail.error` | code | extra fields |
|---|---|---|
| `requires_init` | 409 | `message`, `required_actions` |
| `instrument_servicing` | 409 | `detail`, `olss_state` |
| `subsystem_fault` | 409 | `detail`, `faulted_modules` |
| `queue_full` | 412 | `detail`, `max_depth`, `current_depth`, `retry_after_s` |
| `reserved_for_robot` | 412 | `detail`, `reserved_drawer` |
| `dispatch_in_progress` | 412 | `detail`, `retry_after_s` |
| `plate_mismatch` | 422 | `detail`, `drawer`, `declared`, `configured` |
| `user_not_recognized` | 403 | `detail`, `owner` |
| `role_forbidden` | 403 | `detail`, `owner`, `role`, `required_role` |
| `workflow_active` | 423 | `detail`, `claimed_by`, `retry_after_s` |
| — (claim rejection) | 409 / 423 / 401 | `detail`, `claimed_by`, `retry_after_s` |

409 and 412 refusals never populate `last_error` (§6.3).

## `allowed_actions` verbs

`run.submit`, `run.abort`, `queue.cancel`, `instrument.standby`,
`workflow.start`, `workflow.end` — the same names the `lab-skills` catalog uses
for `kind: hplc`. They are state-based and identity-agnostic; see the agent
guide for which conditions withhold which verb. `POST /control/startup`, the
claim protocol, the service toggle, and the consumable and fault
acknowledgments have no verb: they are operator/protocol actions, not skills.
