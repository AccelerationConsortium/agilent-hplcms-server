# STATUS_SPEC v1.1 Migration Plan

Migrate the Agilent UPLC-MS sidecar to **STATUS_SPEC v1.1** and register it as a
v1.1 control device in the lab. This spans **two repos**:

- **Sidecar (device):** this repo — `agilent-hplcms-server` (branch `feature-agent-control`)
- **Lab / aggregator:** `ac-organic-lab` (already at v1.1). Spec: `docs/STATUS_SPEC.md`
  — §5 = claim protocol, §6 = preconditions / HTTP 412. The aggregator only runs
  the per-request claim dance when `equipment.yaml` marks the device
  `protocol: "1.1"` (see `api/app/control.py`, the `needs_claim` gate).

## Approved decisions

1. **Map `paused` → `busy`.** OLSS `softwareStatus == "Paused"` (while connected) is
   **not** a legal `EquipmentState` in v1.0 or v1.1. Report
   `equipment_status: "busy"` with `message="OpenLab sequence paused…"` and
   `required_actions: ["resume_paused_sequence"]`; keep the precise `Paused` value in
   `details.olss_software_status` and the `hplc` / `ms` component `state`. (Queue
   ownership and submission gating were subsequently reworked — see the
   *Queue-ownership pivot* addendum below.)
2. **Hard claim enforcement.** Require a valid `X-Claim-Token` on the mutating
   `/control/*` endpoints; reject mismatch / missing with **HTTP 423 Locked** plus
   `claimed_by`. Read-only endpoints (`GET /control/queue`, `POST /control/startup`)
   stay open.

## Sidecar tasks (this repo)

- **`models.py`**: set `PROTOCOL_VERSION = "1.1"`; add `allowed_actions: list[str] = []`
  to `EquipmentStatus`; add a `ClaimedBy` model; surface it as `details.claimed_by`
  (null when unclaimed).
- **`status_builder.py`**: implement the paused→busy mapping; populate
  `allowed_actions` from a **single shared helper** that mirrors the control-side
  refusals. §6.2 invariant: `<verb> in allowed_actions` **iff** a POST to
  `/control/<verb>` would NOT refuse (412 / 423 / 409).
- **New claim endpoints** in `control/`:
  - `POST /control/claim` → `{claim_token, heartbeat_interval_s, expires_at}`
  - `POST /control/heartbeat` (`X-Claim-Token`, 204; optional 200 with new `expires_at`)
  - `POST /control/release` (`X-Claim-Token`, idempotent 204)
  - Back them with an in-memory single-slot claim holder (TTL + owner + session_id)
    in/beside `MosesRunner`. A repeat claim from the same `session_id` is idempotent.
- **Enforce token** on `/control/run`, `POST /control/queue`, `/control/abort`,
  `/control/standby`, `DELETE /control/queue/{id}` → 423 on missing/stale token.
  (The park endpoint is `/control/standby`, not `shutdown` — a real power-down is a
  manual operator procedure, never an API action.)
- **HTTP code reconciliation (§6.1):**
  - Keep `requires_init` → **409** (device-state conflict) and hardware-limit
    violations → **422** (invalid body).
  - Move `queue_full` → **412** with `retry_after_s` + a `Retry-After` header, and
    drop the submit verb from `allowed_actions` while the queue is full.
  - 412 refusals MUST NOT set `last_error`; clear `last_error` on the next successful
    operational control action (§6.3 / §6.4).
- **Tests** (`tests/test_control_api.py`): claim acquire/heartbeat/release lifecycle,
  423 on bad token, idempotent release, the `allowed_actions` ⇔ refusal property, and
  the paused→busy mapping.

## Lab tasks (`ac-organic-lab`)

- **`equipment.yaml`**: set `agilent_uplc_ms` → `protocol: "1.1"` (this is what turns
  on the aggregator's per-request claim dance); revisit `do_not_call_connect`.
- **`docs/SKILLS_CATALOG.md`** + skill defs: add HPLC control verbs using the
  `<role>.<verb>` naming convention (`run.submit`, `run.abort`, `queue.cancel`,
  `instrument.standby`, and the workflow verbs `workflow.start` / `workflow.end`) with
  `requires_states` + arg schemas mapping to the `/control/*` endpoints — mirror the
  existing `dose` / `xArm` v1.1 migration commits. (`service.start` / `service.end` are
  operator/dashboard controls, not agent skills — keep them out of the catalog.)
- **`ROADMAP.md`**: move `agilent_uplc_ms` from "read-only sidecar" to v1.1 control.

## Addendum: sample-submission contract (tray + well)

Shipped alongside the v1.1 control surface so a run submitted via the lab skill
catalog validates identically on the device.

- **Logical addressing.** A `RunRequest` carries a `plate_format`
  (`96-well` / `384-well`) and samples addressed by `{tray, well}`
  (`tray ∈ {front, rear}`, `well` like `A1`/`H12`) — *not* a raw Agilent
  position string. `control/router.py:_compose_moses_job` composes the
  `{drawer}-{well}` address Moses consumes from the tray→drawer config
  (`TRAY_FRONT_DRAWER` / `TRAY_REAR_DRAWER`); `plate_format` / `submitter` are
  device-side only and not forwarded to Moses.
- **Geometry validation (422).** Wells are checked against `plate_format`
  geometry (`96 → 8×12`, `384 → 16×24`); off-plate wells are rejected with 422.
- **Robot-tray reservation (412).** The **front** tray (`RESERVED_ROBOT_TRAY`,
  default `front`) is reserved for robotic submission; manual runs use the rear
  tray. A run with `submitter != "robot"` targeting the reserved tray is refused
  with **412 `reserved_for_robot`** (a precondition refusal — no `last_error`).
  `submitter="robot"` bypasses it; `""` disables the reservation.
- **Cross-repo alignment.** The `SampleConfig` / `RunRequest` field set, plate
  geometry, and well regex are kept in sync with the lab skill catalog
  (`ac-organic-lab: lab_skills/skill_catalog/hplc.py`). The device is the
  authoritative validator; the catalog mirrors these definitions.
- Drawer codes: `TRAY_FRONT_DRAWER=D1F` is the confirmed robot tray;
  `TRAY_REAR_DRAWER=D4B` matches the existing example job — confirm it against
  this instrument's multisampler before deploying.

## Addendum: queue-ownership pivot, servicing, and roles

Decided and implemented 2026-06-23, after the v1.1 control surface above shipped.

- **The sidecar owns the queue.** `MosesRunner` is the sole job queue; OpenLab's
  native sequence queue is no longer used for our jobs (OpenLab is for technician
  servicing/maintenance). Verified that `moses.agilent` `start_run` runs
  **synchronously**, so **process exit is authoritative**: subprocess alive →
  `running`, exit `0` → `done`, non-zero → `failed`. This was a net deletion of the
  old `enqueued`/`acquiring`/`.sirslt`/`_olss_occupied` finalization logic. Job status
  is now `pending → running → done | failed`.

- **Submission precedence (highest wins):**
  1. **Servicing** → `409 instrument_servicing`; queue halted (pending jobs wait, not
     dropped). Two sources: an **explicit, persistent admin toggle**
     (`POST /control/service/start` | `…/service/end`, *not* claim-bound so a dropped
     dashboard never un-blocks a maintenance window), and an **auto-detect fallback**
     keyed on `olss_current_run` (a real acquisition) while no sidecar job is active,
     debounced over `SERVICING_DEBOUNCE_POLLS`. Keyed on `currentRun`, not bare
     `state=="Busy"`, because OLSS `state` cannot distinguish a run from data analysis.
  2. **Workflow** → non-holder gets `423 workflow_active` (+ `Retry-After`). The
     equipment-blocking lock is a `workflow` flag on the single-slot claim
     (`POST /control/workflow/start` | `…/end`); it inherits TTL/heartbeat/auto-expiry.
  3. **Our queue job running** → normal FIFO queue.
  4. **Idle** → single sample queued.

- **Roster-driven roles (identity, NOT authentication — no passwords in the device).**
  `control/roster.py` maps a claim `owner` to one of three roles. Capabilities:

  | group (env) | role | `run.submit` | `workflow.*` | `service.*` |
  |---|---|:--:|:--:|:--:|
  | `HPLCMS_USERS` | `hplcms_user` | ✓ | | |
  | `HTE_USERS` | `hte` | ✓ | ✓ | |
  | `HPLCMS_ADMINS` | `hplcms_admin` | ✓ | | ✓ |

  Unknown owner → `403 user_not_recognized`; under-privileged action → `403
  role_forbidden`. The roster is always enforced; all-empty falls back to built-in
  defaults (`Hplcms-User`/`HTE-User`/`Service-Account`) so a fresh install never bricks,
  and a literal `"*"` opens a list. `HPLCMS_ADMINS` is seeded with the single
  `Service-Account` the dashboard claims under to toggle service mode. A real login /
  password belongs at the dashboard layer (to be built separately), which then claims
  on the device passing the authenticated user as `owner`.

- **`allowed_actions`** stays identity-agnostic (state preconditions only, as before):
  `servicing` / `requires_init` / `queue_full` drop the enqueue verbs; `workflow.start`
  / `workflow.end` toggle on workflow state. `service.*` is deliberately excluded (it is
  an operator control, not an agent skill); `details.service_mode` and
  `details.servicing` are surfaced for the dashboard instead.

- **Still open:** mirror `workflow.start` / `workflow.end` in the lab skill catalog;
  the supervised rear→`D4B` hardware run before any real `run.submit` is permitted.

## Hard constraints (unchanged)

- Never import or share an environment with `moses` — subprocess invocation only.
- `GET /status` stays side-effect-free and always returns HTTP 200.
- No secrets in `/status`; snake_case field names; UTC ISO-8601 timestamps.
- Run tests with `C:\SDL_Tools\uv.exe run pytest -q` (add
  `--basetemp .tmp_pytest -p no:cacheprovider` if Windows temp permissions block pytest).
