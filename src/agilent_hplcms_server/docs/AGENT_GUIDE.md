# Agilent UPLC-MS — agent guide

This service is a sidecar in front of one Agilent UPLC-MS (LC stack + single-quad
MS) driven through OpenLab CDS and the lab's `moses` controller scripts. It
speaks the AC Organic lab's STATUS_SPEC **v1.2** (`equipment_id:
"agilent_uplc_ms"`, `equipment_kind: "hplc"`) and listens on port **8010**.

It does **not** talk to the instrument. Every reading comes from observing the
instrument PC — OpenLab supervisor processes, the OLSS REST API, the Agilent RC
driver log, archived `.dx` traces, the CDS results directory, and an optional
sensor-daemon JSON file. Every action it takes is launching a `moses` script as
a subprocess. That shapes everything below: the sidecar knows what it can see,
and says `unknown` rather than guessing when it cannot see.

Read this before driving it. The
[API reference](agent-docs/api-reference) lists every route with its gate, body
and refusal codes; `/openapi.json` carries the exact schemas; and
**`GET /docs/agent`** is the existing versioned JSON guide — execution policy,
the submission workflow in order, sample addressing, dispatch semantics, the
refusal-code table and cancellation rules. This guide does not repeat it; read
both.

## What "primary operation" means here

An **acquisition**: an injection and its gradient in flight. Not a queued job,
not a method edit, not data reprocessing.

`activity` is observed (§2.3), never derived from `equipment_status`. It is
`running` when any of these hold:

- `acquisition_active` — the CDS results directory shows a run directory
  written within `BUSY_THRESHOLD_S` (default 90 s);
- `moses_process_alive` — a `moses` controller subprocess is in flight;
- the OLSS instrument state is one of `Run`, `Running`, `Busy`, `Prerun`,
  `PostRun`;
- the OLSS software status is `Paused` while connected — a sequence waiting on
  the operator's **Resume** is an operation in progress, not a finished one.

Two overrides sit in front of that: when `MosesRunner` holds an active
subprocess, `acquisition_active` is forced true (it closes the race between
submitting a run and the first run directory appearing on disk); and the §2.3
invariant `requires_init ⇒ idle` wins over everything. With a `probe_error`,
`activity` is `unknown` — the sidecar cannot see the instrument at all.

`activity_since` is stamped the instant the value *changes*, so it is the start
of the current span, not the poll instant. It is process-local state: a restart
of this service restarts the span.

**There is no `cycles_total` metric on this device**, and no run counter of any
kind in `metrics`. For usage accounting use `GET /control/queue`, which keeps
the job history (queued/started/finished timestamps per job); sampled `activity`
will miss short runs entirely.

## Health vs. activity (STATUS_SPEC §2.2 / §2.3)

`equipment_status` answers "is it fit for a run"; `activity` answers "is it
running". They are computed independently, so a run in flight when an error
lands reports `error` **and** `activity: "running"`.

`equipment_status` is decided by this precedence, first match wins:

| `equipment_status` | when |
|---|---|
| `unknown` | `probe_error` — a probe itself failed (missing results dir, missing OpenLab log dir, an exception). The sidecar cannot reason about the instrument, so `allowed_actions` is **empty**. |
| `requires_init` | an OpenLab core supervisor process is missing — `AcquisitionServer`, `AcqInstrumentService` or `OpenLabReverseProxy`. `required_actions: ["start_openlab"]`. The service never starts OpenLab itself; that is a manual operator action at the PC. |
| `error` | an active LC module fault, or a recent OpenLab error event in the log tail (`ERROR_WINDOW_S`, default 300 s). A module fault outranks the log-tail error for `last_error`. |
| `busy` | OLSS `softwareStatus: "Paused"` (`required_actions: ["resume_paused_sequence"]`), or an acquisition is active by any of the signals above. |
| `degraded` | otherwise-`ready` **and** an LC module reports a hardware error. MS and comms are up but a run cannot safely start; `required_actions` carries `check_<role>` per module. |
| `ready` | OpenLab supervisor up, no active acquisition, no faults. |

There is no `dry_run` or `offline` state on this device.

Consumable and pressure actions are appended to `required_actions` regardless of
state, so they are actionable mid-run: `empty_waste_bottle`,
`refill_solvent_{a1,a2,b1,b2}`, `check_lc_pressure` (advisory post-run pressure
drift — it never changes `equipment_status` and never blocks a submission).

`GET /status` is side-effect-free and does no instrument I/O; it reads files and
process tables. It does update two pieces of in-process bookkeeping — the
activity span and the runner's servicing debounce — which is why polling it
regularly matters for the auto-detect described below.

### Components

`openlab_acquisition`, `openlab_instrument_service`, `openlab_reverse_proxy`
(`running` / `stopped` from the process probe), `moses_controller` (`running` /
`idle`), and `hplc` / `ms` (both mirroring the OLSS instrument state: `ready`,
`not_ready`, `busy`, `paused`, `error`, `stopped`, or `unknown`).

Four per-module LC cards — `binary_pump`, `dad_detector`, `column_thermostat`,
`multisampler` — appear only when the RC driver log has said something about
that module. **An absent card is not a healthy module**; it means no `STAT?`
reply has been seen. A logged hardware fault puts a module in `error` even if
its last `STAT?` said `busy`, and a module that has logged a fault but never a
`STAT?` still gates run submission (see *Subsystem faults*).

## Claims (STATUS_SPEC §5)

Every mutating `/control/*` call needs a valid `X-Claim-Token`. Claims are
always enforced — there is no disable switch.

- `POST /control/claim` with `{owner, session_id, ttl_s}`. `ttl_s` defaults to
  30 s and is clamped to **5–300 s**; the response's `heartbeat_interval_s` is
  **half** the granted TTL. Heartbeat at that interval, release when done.
- The response also carries `role`, the owner's roster-resolved lab role. Read
  it: it tells you up front whether you may start a workflow.
- An `owner` not on the configured roster → **403** `user_not_recognized`.
  Resolve authorization; never retry under another identity.
- Held by a different live session → **409** with `claimed_by`.
- Re-claiming with the same `session_id` is idempotent.
- No/stale token on a control call → **423** with `claimed_by`.
- `details.claimed_by` on `/status` shows the holder, plus this device's
  additive `role` and `workflow` fields.

`lab_skills.ClaimManager` does all of this for you.

### Roles

Roster membership is identity attribution, not authentication — the real access
boundary is the Tailscale ACL. Three roles, configured as comma-separated lists:

| role | env list | may |
|---|---|---|
| `user` | `HPLCMS_USERS` | submit runs, cancel, abort, standby, acknowledge consumables and LC module faults |
| `automation` | `HTE_USERS` | submit runs, cancel, abort, standby, acknowledge consumables, **plus** `workflow.start` / `workflow.end`. Not LC fault acknowledgments |
| `service` | `HPLCMS_ADMINS` | everything a `user` may, **plus** the service-mode toggle |

Higher privilege wins if an owner appears in several lists. When *every* list is
empty the built-in defaults apply (`hplcms-user`, `hte-user`,
`service-account`), so a fresh install is never bricked; a literal `"*"` in a
list is an explicit open mode for development.

### The workflow lock

An `automation` principal running a campaign takes the equipment-blocking lock
with `POST /control/workflow/start`. While it is held, **non-holders** are
refused **423** `workflow_active` with an advisory `Retry-After`. The lock rides
on the claim, so it inherits the TTL, heartbeat and auto-expiry — a crashed
holder loses it. End the workflow before releasing the claim.

`workflow.start` is refused **403** `role_forbidden` for any other role; the
body's `required_role` is `"automation"`. It additionally waits out
`requires_init`, servicing (from either source) and a subsystem fault with
**409** — unlike a single run, which merely queues, taking the lock claims the
instrument *now*.

## The submission model

Two routes submit the same `RunRequest` body and apply the same gates; they
differ only in the response shape: `POST /control/run` returns `run_id` +
`status` (`accepted` when it started immediately, `queued` with a 1-based
`queue_position`, or `dispatching`), `POST /control/queue` returns `queue_id` +
`position` (0 means it started immediately). Both answer **202**. Track
everything through `GET /control/queue`.

A request is one **sequence**: a shared method (`gradient`, `ms_mode`,
`output_dir`, `instrument_config_path`, `standby_after`) plus a list of
`samples`, at least one. The sequence may end in a low-flow park, and
`standby_config` shapes it (flow rate, duration, %B, vial) — so "run these
samples, then park the column" is one job, not a second one you have to
sequence by hand behind the first. Each sample is `{sample_name, sample_position,
injection_volume}`, and `sample_position` is the whole autosampler address —
`D[1-4][F|B]-<well>`, e.g. `"D4B-A1"` — forwarded to the instrument verbatim.
There are no separate tray/well fields to set. The sidecar parses the drawer and
well back out only for its own safety checks.

Hardware ranges are enforced at the model: `injection_volume` in µL, `> 0` and
`≤ 20`; `gradient.run_time` in minutes, `> 0` and `≤ 120`; `gradient.flow_rate`
in mL/min, `> 0` and `≤ 2`; `gradient.equilibration_time` 0–30 min;
`gradient_table` rows are `[time_min, fraction_b]` with `fraction_b` in 0..1.

**Submission is not idempotent.** A network timeout is not a refusal: inspect
`GET /control/queue` and resolve the ambiguity before resubmitting, or you will
run the sequence twice.

### Two queues

`dispatch: "sidecar"` (the default) uses this service's own FIFO. Completion is
**process-exit authoritative**: `pending` → `running` → `done` (exit 0) or
`failed` (non-zero, aborted, or cancelled). A `failed` entry always carries an
`error_message`; a `done` entry can also carry one, when the samples completed
but only the post-run standby park failed — inspect it before deciding to rerun.

`dispatch: "openlab"` is an opt-in fire-and-forget handoff into OpenLab's own
run queue via a submit-and-exit script: `dispatching` → `handed_off` or
`failed`. **`handed_off` means the submission succeeded, not that the
acquisition finished.** OpenLab then owns the run, including its cancellation;
the sidecar tracks nothing further. A handoff never occupies the FIFO
(`position` is 0) and lines up in OpenLab even behind a technician's
acquisition. Only one handoff subprocess runs at a time — a second arrival is
**412** `dispatch_in_progress` with a `Retry-After` (default 10 s). Do not set
`script_name` in this mode: the submit script is device configuration, and a
caller-set value is refused **422** rather than silently ignored.

`script_name` in sidecar mode must be in the `MOSES_ALLOWED_SCRIPTS` allowlist
and must exist under `MOSES_WORK_DIR`; neither is a JSON refusal body — both
come back as **422** with a plain string `detail`.

### Standby is a job, not a shutdown

`POST /control/standby` enqueues a low-flow park sequence with no samples. It
queues behind whatever is active, and it is **not** an instrument shutdown —
powering the UPLC-MS down is a deliberate manual procedure at the instrument.
There is no `/control/shutdown` on this device.

### A run that looks alive but is not acquiring

Completion is process-exit authoritative, so a Moses script that hangs without
exiting keeps its job `running` and `active_run_id` set — what pinned the
instrument on 2026-09-09. `GET /control/queue` (and `/status`
`details`) carry `stale_active_run: true` once the sidecar has held an active
job while OpenLab has shown no acquisition for it for `STALE_RUN_GRACE_S`
(default 300 s). It is a warning, not a state change: check the instrument,
and `POST /control/abort` is the exit. An unreadable OLSS never counts as idle,
and the flag never applies to a job other than the one it was observed on.

### Cancelling

`DELETE /control/queue/{queue_id}` removes a *pending* job (404 if unknown or
already finished, 409 if it is the running one). `POST /control/abort` kills the
active `moses` process **and clears every pending job** — it is not a
single-job cancel. Neither touches an acquisition already handed off to OpenLab;
that must be cancelled in OpenLab CDS.

## When another application holds the instrument

Someone running samples at the instrument PC — a technician working directly in
OpenLab CDS — is the normal case this sidecar is built around. Two distinct
signals express it, and they behave differently on purpose.

**Explicit service mode** (`POST /control/service/start`, `service` role only)
is a persistent flag, not claim-bound: the dashboard may claim only long enough
to set it and then release, and it stays set until explicitly cleared, so a
dropped claim never silently reopens a maintenance window. While it is on, the
queue is halted **and the door is closed**: an enqueue is refused **409**
`instrument_servicing`, and `run.submit`, `instrument.standby` and
`workflow.start` all leave `allowed_actions`.

**Auto-detected servicing** is the fallback when nobody flips the switch: OLSS
reports a real `runQueue.currentRun` while the sidecar holds no active job,
sustained across `SERVICING_DEBOUNCE_POLLS` (default 2) consecutive `/status`
observations. The debounce exists to avoid a false positive in the one-poll gap
after a `moses` process exits but before OLSS clears its current run. This keys
on an actual run, not a bare `state == "Busy"`, so data reprocessing does not
halt the queue.

Auto-detected servicing **does not refuse a submission**. The job is accepted
and parked, and starts by itself once the instrument frees. Refusing at the door
used to leave the queue sitting empty while it rejected work every time a
colleague ran a sample by hand. What it *does* refuse — **409**
`instrument_servicing` — are the two verbs that take the instrument now:
`POST /control/standby` and `POST /control/workflow/start`.

Read the difference off `GET /control/queue`: `accepting_jobs` mirrors the
enqueue door, and `dispatch_held_reason` is `"service_mode"`, `"servicing"` or
`null`. That is how you tell "queued and starting" from "queued, waiting for the
technician to finish". `/status` carries the same facts as
`details.service_mode` and `details.servicing`, with `message` reading
"Instrument in use via OpenLab CDS (technician servicing)".

Neither refusal carries a `Retry-After`: servicing duration is unpredictable, so
the spec prefers omitting the field to guessing it.

### When the instrument state itself is unreadable

The OLSS REST probe fails softly. A failed login, an HTTP error, a timeout or an
empty instrument list all return `olss_instrument_state: null` with the reason
in `olss_error`, surfaced as `details.olss_error` on `/status`. In that state:

- `details.olss_instrument_state` is **absent** — the field is only set when a
  value was actually read. Absent means "not observed", never "idle".
- The `hplc` and `ms` component cards fall back to mirroring the top-level
  `equipment_status` rather than reporting an instrument state nobody read.
- The `*_communication_ok` metrics are omitted entirely.
- `activity` and `equipment_status` fall back to the file- and process-based
  signals (`acquisition_active`, `moses_process_alive`), which see a run in
  progress but not which phase it is in.
- The servicing auto-detect sees no current run, so its streak resets and
  `dispatch_held_reason` will not report `"servicing"`. **A run someone else
  started can therefore be invisible to the auto-detect while OLSS is
  unreachable.** If you know a human owns the instrument, have them set explicit
  service mode; that flag does not depend on OLSS at all.

This service has no ChemStation-specific concept: everything it knows about
"another application is acquiring" comes from the OLSS view above and the
process/results-directory probes.

## Subsystem faults

An LC module reporting a hardware `error` — from a `STAT?` ERROR flag or an
active fault on the driver's own error channel — refuses every enqueue with
**409** `subsystem_fault` (`faulted_modules` names them) and drops `run.submit`
from `allowed_actions`, fail-closed, so a run never launches into faulted
hardware. The gate and the `/status` component cards are computed by the same
function, so they cannot disagree.

The awkward part: Agilent's driver never logs a *fault-cleared* line, and the
only observable recovery — the module's own `STAT?` going READY — is written
at prerun. A module fixed while the instrument sits idle has no way to say so,
and the fault would otherwise hold for the whole `LC_FAULT_WINDOW_S` (default
one hour) with submissions refused behind it. `POST /control/faults/{module}/ack`
is the exit: the operator holding the instrument (a `user` or `service`
account, never an `automation` one), having physically checked the module,
clears the evidence that exists *right now*. Anything the driver logs afterwards
re-arms the fault in full, so the acknowledgment needs no expiry and cannot mask
the next failure. An acknowledged module is reported `not_ready`, not `ready` —
it has not sent a READY since, and claiming otherwise would invent a reply the
module never made. `DELETE` the same path withdraws an ack taken in error.

Which modules are green only because someone vouched for them, and when, is on
`/status` as `details.fault_acks`.

## Consumables

OpenLab's bottle-fill numbers are read-only accumulating estimates, so
physically emptying the waste bottle or refilling a solvent cannot clear the
warning by itself. `POST /control/consumables/waste/reset` and
`POST /control/consumables/solvent/{slot}/reset` (`a1`/`a2`/`b1`/`b2`) record
the acknowledgment; the warning and its `required_action` stay suppressed until
the raw estimate moves `CONSUMABLE_REARM_DELTA_ML` (default 200 mL) past the
level at acknowledgment. Acknowledgments are persisted, so a service restart
does not resurrect the warning, and an active suppression is visible as
`details.waste_reset_at` / `details.solvent_<slot>_reset_at`. Any valid claim
holder may do this, an automation account included — unlike a fault ack,
which asserts that a person looked at the module.

## Preconditions and `allowed_actions` (STATUS_SPEC §6)

Read `allowed_actions` before acting. It is generated by the same helper the
`/control/*` router refuses from, so the advisory list and the gates cannot
drift. The verbs are the lab skill-catalog names:

| action | route | withheld when |
|---|---|---|
| `run.submit` | `POST /control/run`, `POST /control/queue` | `requires_init`, queue full, explicit service mode, subsystem fault |
| `run.abort` | `POST /control/abort` | never (while the service can see the instrument) |
| `queue.cancel` | `DELETE /control/queue/{id}` | never (while the service can see the instrument) |
| `instrument.standby` | `POST /control/standby` | the `run.submit` conditions **plus** servicing from either source |
| `workflow.start` | `POST /control/workflow/start` | the `instrument.standby` conditions, plus while a workflow is already active |
| `workflow.end` | `POST /control/workflow/end` | offered exactly while a workflow is active |

With a `probe_error` the list is empty: nothing can be reasoned about, so
nothing is offered.

`allowed_actions` is **identity-agnostic** by design (§6.2). It reflects device
state, not who is calling — `run.submit` is listed even though a tokenless
caller gets 423, and it is not dropped merely because someone else holds the
workflow lock. It is also not a promise: a later request can still fail on a
condition that changed in between.

Note that `POST /control/startup` has no verb here. On this device it is a
read-only readiness check (does the OpenLab supervisor stack exist?), takes no
claim, and starts nothing.

## Refusal codes

Control errors go on the wire as `{"detail": {...}}` — FastAPI wraps the model
under `detail`, and the OpenAPI document is post-processed to say so. Request
validation is the exception: `{"detail": [...]}`. Branch on
`detail.error`, never on prose.

| code | `detail.error` | means |
|---|---|---|
| **401** | — | `POST /control/heartbeat` with an unknown, expired or wrong-session token. Treat the claim as lost and re-claim. |
| **403** | `user_not_recognized` | the claim `owner` is not on the roster |
| **403** | `role_forbidden` | the holder's role lacks this action; body carries `required_role` |
| **404** | — | unknown `queue_id` (or already finished), unknown solvent slot, unknown LC module, or no acknowledgment to withdraw. Plain string `detail`. |
| **409** | — | `DELETE /control/queue/{id}` on the *running* job — use `POST /control/abort`. Plain string `detail`. |
| **409** | `requires_init` | an OpenLab core process is missing; `required_actions: ["start_openlab"]` |
| **409** | `instrument_servicing` | explicit service mode (enqueues) or servicing from either source (standby, workflow start). No `Retry-After`. |
| **409** | `subsystem_fault` | an LC module reports a hardware error; `faulted_modules` names them. No `Retry-After`. |
| **409** | — | `POST /control/claim` while another live session holds it; body is `{detail, claimed_by, retry_after_s}` |
| **412** | `queue_full` | the FIFO is at `QUEUE_MAX_DEPTH` (default 20) *with a run active*; `Retry-After` default 60 s |
| **412** | `reserved_for_robot` | a non-robot run targets `RESERVED_ROBOT_DRAWER` (default `D1F`); set `submitter: "robot"` only if that is physically true |
| **412** | `dispatch_in_progress` | an OpenLab handoff subprocess is still in flight; `Retry-After` default 10 s |
| **422** | `plate_mismatch` | the sample does not fit the labware actually configured in that drawer |
| **422** | — | request-validation failure, or a `script_name` outside the allowlist / missing on disk (plain string `detail`) |
| **423** | — | missing or stale `X-Claim-Token`; body is `{detail, claimed_by}` |
| **423** | `workflow_active` | a workflow holds the instrument and you are not the holder; advisory `Retry-After` |

412 and 409 refusals are *decisions*, not failures: per §6.3 they never populate
`last_error`.

Four things trigger `plate_mismatch`, all checked against the drawer's
**configured** labware (`LABWARE_CONFIG_PATH`), which is authoritative over what
you declare: the drawer has no configured labware, the configured plate stands
above the drawer's clearance, your `plate_format` disagrees with the loaded
plate type, or the `well` is off the configured plate's geometry. The last case
is why the check exists at all — non-canonical labware such as a 6×9 54-vial
plate cannot be expressed by the built-in 96/384 check. The needle descends to
the configured plate's geometry, so a height disagreement is a collision. Do not
edit the plate declaration or the submitter identity to get past a refusal;
report the physical discrepancy.

With no labware configured, the check is skipped entirely and only the built-in
`plate_format` well-range check applies (defaulting to `96-well` when
`plate_format` is null).

## `last_error`

`last_error` is whatever the sidecar last *observed*, not a record of your
request's outcome. Two sources, in precedence order: an active LC module fault
(the driver's own code, severity and module name) outranks a recent error event
in the OpenLab log tail, unconditionally, because it names hardware rather than
software. The displaced log-tail error stays reachable at
`details.last_error_log_path`, and the full fault list — most actionable first,
so the cascade is visible and not just the promoted fault — at
`details.lc_faults`.

## Discovery

`/llms.txt` (the index), `/agent-docs` (this guide),
`/agent-docs/api-reference`, `/docs/agent` (the JSON agent guide: execution
policy, workflow order, sample addressing, dispatch, refusals, cancellation),
`/openapi.json`, `/docs` (Swagger UI).
