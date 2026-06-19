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
   `details.olss_software_status` and the `hplc` / `ms` component `state`. Queue-gating
   is unchanged — it keys off the OLSS "occupied" flag in `control/runner.py`, not the
   envelope string.
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
  `/control/shutdown`, `DELETE /control/queue/{id}` → 423 on missing/stale token.
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
  `instrument.shutdown`) with `requires_states` + arg schemas mapping to the
  `/control/*` endpoints — mirror the existing `dose` / `xArm` v1.1 migration commits.
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
- **Robot-tray reservation (412).** `RESERVED_ROBOT_TRAY` (default `rear`) is
  reserved for robotic submission: a run with `submitter != "robot"` targeting it
  is refused with **412 `reserved_for_robot`** (a precondition refusal — no
  `last_error`). `submitter="robot"` bypasses it; `""` disables the reservation.
- **Cross-repo alignment.** The `SampleConfig` / `RunRequest` field set, plate
  geometry, and well regex are kept in sync with the lab skill catalog
  (`ac-organic-lab: lab_skills/skill_catalog/hplc.py`). The device is the
  authoritative validator; the catalog mirrors these definitions.
- ⚠ `TRAY_FRONT_DRAWER` default `D1F` is a **placeholder** — confirm both drawer
  codes against this instrument's multisampler before deploying.

## Hard constraints (unchanged)

- Never import or share an environment with `moses` — subprocess invocation only.
- `GET /status` stays side-effect-free and always returns HTTP 200.
- No secrets in `/status`; snake_case field names; UTC ISO-8601 timestamps.
- Run tests with `C:\SDL_Tools\uv.exe run pytest -q` (add
  `--basetemp .tmp_pytest -p no:cacheprovider` if Windows temp permissions block pytest).
