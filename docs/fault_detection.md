# Fault detection: LC module faults and pump pressure

Plan for making the sidecar notice **hardware faults that need a human** — a leak,
a valve overcurrent, a comms loss, a sudden pump pressure drop — rather than only
the software-layer errors it surfaces today.

Everything below is grounded in what this instrument (`SDL2_LC1290`: G7120A binary
pump, G7117B DAD, G7116B column thermostat, G7167B multisampler, G6160B SQ) actually
writes to disk. Log evidence was sampled from
`C:\ProgramData\Agilent\LogFiles\LC Drivers\*RCDriver*.log` and
`C:\CDSProjects\Installation\Results\**\*.sirslt` in August 2026.

## Where we are

`/status` already reports *something* went wrong, through three state signals:

| Path | Code | Catches |
|---|---|---|
| OpenLab server log tail for `ERROR`/`CRITICAL`/`FATAL` | `probes/process.py` `_scan_recent_error` | Software-layer errors, within `error_window_s` |
| OLSS REST `state` / `softwareStatus` | `probes/openlab_rest.py` | `Error` / `NotReady` / `NotConnected`, whole-instrument granularity |
| Per-module `STAT?` flags | `probes/rc_driver_log.py` `read_module_states` | A module latched into `ERROR` |

Three gaps:

1. **No cause.** A latched `ERROR` flag says *that* the multisampler is unhappy, not
   that it detected a leak. `last_error` is whatever regex-matched the word "ERROR"
   in a text log.
2. **No pressure.** `system_pressure_bar` is declared in `sensor_file.py`,
   `status_builder._build_metrics`, and the daemon's `METRIC_VALUE_TYPES`, but
   `tools/hplcms_sensor_daemon.py::_fetch_lc_signal_metrics` is a stub returning
   `{}`. Nothing observes pressure, so no drop — sudden or gradual — is detectable.
3. **No push.** `/status` is poll-only. A fault at 02:00 sits in a JSON field until
   something polls and someone reads it.

## Phase 1 — LC module fault channel (`RCDriver.log`)

**The finding this phase rests on:** the file we already parse for bottle levels
also carries Agilent's module event stream, with an explicit severity per event.
Three correlated lines are written per fault
(`RCDriver.2026-08-05 13.03.51.log:2401-2409`):

```
LCEventData: ... EE 00064, 2026-07-30 3:38:13 PM, 0 "Leak detected"
Module: [G7167B:DEBAS04772] error description changed; Errors: G7167B:DEBAS04772 - Leak detected; ;
ControlIF log error: eLogAndAbortSequence, G7167B:DEBAS04772 - Leak detected [64 64, 0]
```

`EE` marks an **error** event (vs `EV` event, `ES` state), and the `eLog*` token is
the severity the driver itself assigns. Across all retained logs:

| `eLog*` token | Count | Meaning | Our severity |
|---|---|---|---|
| `eLogInformationMessage` | 523 | Routine chatter ("Valve is switched to bypass") | ignored |
| `eLogAndAbortCurrentRunOnly` | 4 | Kills the current run | `error` |
| `eLogAndAbortSequence` | 28 | Kills the whole sequence — **needs a human** | `critical` |

Faults actually recorded on this instrument: `Leak detected [64]`,
`Valve hardware overcurrent [22412]`, `Solvent counter limit exceeded [22055]`,
`Communication error`, `Shutdown [63]`. A pump pressure-limit trip surfaces through
the same mechanism with its own code — it just hasn't happened in the retained
window, so the exact code is unconfirmed. Nothing in the design depends on knowing
it: codes are carried through as opaque strings.

### Design

New `read_lc_faults(log_dir, window_s)` in `probes/rc_driver_log.py` — same module,
because it parses the same file and should share one read of it.

Parse `ControlIF log error: eLog<Severity>, <MODEL>:<SERIAL> - <text> [<code> <code>, <n>]`
as the primary line: it carries severity, module, human text, and code together.
`eLogInformationMessage` is dropped.

**Clearing is the hard part.** The log is append-only and, in every retained log,
*no* fault-cleared line is ever written — `Errors:` is non-empty in all 20
occurrences. So a fault cannot be considered latched-until-cleared, or a leak from
last month would pin the instrument into `error` forever. Two reconciliations:

1. **Window.** Only faults newer than `lc_fault_window_s` (default 3600 s) count.
2. **STAT? recovery.** `read_module_states` already parses each module's `STAT?`
   readiness flags and their age. If a module's `STAT?` is *newer* than its fault
   and reports `READY`, the module recovered — drop the fault. This is what makes a
   1-hour window safe: a genuinely unresolved fault keeps the module out of `READY`,
   so it survives; a transient one clears as soon as the module reports ready again.

   **Read readiness from the flags, not from `module_<role>_state`.** That composite
   state ranks the run-phase token above `READY` (a card wants to show "busy"), and
   *every* `STAT?` this instrument emits carries `PRERUN` — 173 of 173 across all
   retained logs — so the state is never `"ready"`. Comparing it to `"ready"` made
   the recovery branch unreachable: faults only ever aged out of the window. On
   2026-08-19 the multisampler threw transport faults at 16:19–16:21, reported
   `NO_ERROR, READY` at 16:25:52, ran a full successful injection, and `/status`
   still said `error` at 17:18 — 57 minutes of a false fault that also blocked
   `run.submit` through the `subsystem_fault` interlock. `_stat_readiness()` reads
   the `READY` / `NOT_READY` / `ERROR` flags directly and ignores the run phase.

   Note the remaining limit: `STAT?` is only written at prerun, so recovery is only
   *observable* when a run starts. Fix the hardware and start nothing, and the
   window is the only automatic exit. That gap bit on 2026-08-20: the multisampler
   drove its needle into a vessel top in D2F at 20:53:47 (`Pusher hit the vessel
   top [25225]` → `Needle command failed [25022]` → `Draw command aborted
   [25478]`), took the other three modules down with it via `Analysis aborted by
   another module`, and recovered minutes later — OpenLab was showing `Idle` /
   `OK` by 21:06 while `/status` still reported `error` and refused `run.submit`.
   No run started, so no `STAT?` was written, so nothing could observe the
   recovery. **Manual acknowledgment** (below) is the third exit added for exactly
   this case.

**Rotation.** `RCDriver.log` rotates at 10 MB, and a busy day rotates it in ~25
minutes (observed 2026-08-11: 9.5 MB at 10:35, rotated by 10:45). A fault inside the
window can therefore live in a rotated file, so scan the active log plus any
`*RCDriver*.log` whose mtime falls within the window.

**I/O.** `read_module_states` already slurps the whole (up to 10 MB) `RCDriver.log`
on every poll, so the obvious move was to cache the decoded text and share it with
the fault scan. Measurement killed that idea: on a real 10 MB log, decoding costs
**3 ms** but pins **10 MB** resident per cached file — a bad trade for a
long-running service. Streaming the same file line by line costs **18 ms** and holds
nothing, so `read_lc_faults` streams (`_iter_lines`). The pre-existing slurp in
`read_module_states` is left alone; it is a separate, older cost.

### Signals emitted

```
lc_faults              list[dict]   # module_code, role, serial, message, code, severity, timestamp, age_s
lc_fault_active        bool
lc_fault_severity      "critical" | "error" | None
lc_fault_message       str | None   # most severe, then most recent
lc_fault_module_roles  list[str]
```

### Integration into `/status`

- **`last_error`** ← the top fault, when it is newer than the log-tail error. A coded
  per-module fault is strictly more actionable than a regex hit on "ERROR".
- **`equipment_status`** → `error` follows for free: `build_status` already maps a
  non-null `last_error` to `error`, above `busy` (health-first, §2.2).
- **`errored_lc_modules()`** ← extended to union STAT?-derived and fault-derived
  modules. This is the real safety win: that function already gates the router's
  `subsystem_fault` interlock (`control/router.py::_check_subsystem_fault`), so a
  leaking autosampler starts refusing `run.submit` with 409 without touching the
  router at all.
- **`required_actions`** ← `check_<role>`, matching the existing convention.
- **`details.lc_faults`** ← the full list, for the dashboard.

### Manual acknowledgment (`control/fault_acks.py`)

`POST /control/faults/{module}/ack` records that an operator has physically
checked a faulted module; `DELETE` withdraws it. Service-role gated — it asserts
something about the hardware and releases the `subsystem_fault` interlock, which
is the service toggle's kind of authority rather than the consumable
acknowledgments' (refilling a bottle cannot crash a needle into a vial).

Shaped after `control/consumables.py`, for the same reason that module exists: a
warning the device cannot clear on its own, and can only *read* evidence about,
trains operators to ignore it. A file-backed store that only `/control/*` mutates,
plus a **pure** suppression function `build_status` applies, so `/status` stays
side-effect-free.

**Both fault channels are acknowledged together.** Filtering `lc_faults` alone
would leave the module red anyway: the multisampler's last `STAT?` before an
abort is `ERROR, NOT_READY`, and `status_builder._module_state_with_olss` reads
that ERROR flag straight into a component state of `error`. So an ack also drops
the ERROR token from a `STAT?` it covers — and only that token, leaving
`NOT_READY` to stand. The module has not reported ready since, and inventing a
`READY` it never sent would be a worse lie than showing a stale not-ready.
`not_ready` is enough, because both the status gate and the interlock test for
`error` exactly.

**Re-arming is automatic, so an ack needs no expiry.** Evidence is matched by its
own event time, not by "older than the ack": faults carry `timestamp`, and each
`STAT?` now carries `module_<role>_stat_at` (added for this — `stat_age_s` is
measured from the poll, so the same reply reports a different age every read).
Both come from the driver log's naive-local clock while an ack is stamped in UTC,
so storing the acknowledged event times verbatim, in the driver's own domain,
keeps the comparison exact. Anything the driver logs afterwards is evidence the
operator has not seen and counts in full — the ack cannot mask the next failure,
and acking a module with nothing wrong records null thresholds that suppress
nothing.

`errored_lc_modules()` takes the acks too, so the dashboard view and the router's
409 refusal cannot drift apart — the same single-source discipline the
component-builder sharing already enforces.

### Configuration

`LC_FAULT_WINDOW_S` (default 3600). Setting it to 0 disables the probe.
`LC_FAULT_ACK_FILE` (default `C:\SDL_Tools\hplcms_fault_acks.json`) persists
acknowledgments, so a service restart cannot resurrect a cleared fault.

## Phase 2 — post-run pressure QC from the `.dx` archive

Every completed run already archives a full pump pressure trace. The `.dx` is a ZIP
(OPC) holding one member per signal, GUID-named, each a ChemStation-derived binary
trace. `probes/dx_trace.py` reads them in pure Python — no .NET, no vendor SDK, so
this lives in the sidecar rather than the daemon.

### File format (version `179`)

Fixed offsets; strings are length-prefixed UTF-16LE (one length byte in characters,
immediately before the text):

| Offset | Contents |
|---|---|
| `0x0000` | `b"\x03179"` format marker |
| `0x035A` | sample name |
| `0x0957` | acquisition date |
| `0x0A0E` | acquisition method path |
| `0x1075` | signal description, e.g. `PMP1B,Pressure` |
| `0x127C` | scale factor — **one big-endian float64** |
| `0x1800` | data: records of **two little-endian float64**, `(time_ms, raw_value)` |

Physical value is `raw_value × scale_factor`. The scale factor is per-signal and is
the only thing that makes the counts meaningful — the container carries no unit
string anywhere. It was pinned down against values known independently for this
instrument, which is what makes it trustworthy rather than inferred:

| Trace | scale | decodes to | independently known |
|---|---|---|---|
| `LCMS1I` Gas Temperature | 1 | 325.0 °C | sensor daemon reads 325.0 |
| `PMP1C` Flow | 1e-06 | 0.50 mL/min | method flow is 0.5 |
| `PMP1D/E` Solvent Ratio | 0.001 | 5.0 → 95.0 % | gradient is 5→95 |
| `THM1A/B` Column Temp | 0.001 | 40.00 °C | thermostat setpoint |
| **`PMP1B` Pressure** | **0.005** | **201–422 bar** | — |

Signal names follow ChemStation convention: `PMP1B` is pump 1 channel B. A typical
run yields 24030 pressure points at 25 ms spacing over exactly 10.0 minutes.

### The check

`probes/dx_pressure.py` summarises the newest completed run (peak / min / mean) and
compares its **peak** against the **median peak of the recent runs of the same
method**. Median rather than mean so one bad run cannot drag the reference toward
itself and mask the next one.

Baselining is per-method and short-horizon deliberately: the same method run six
days apart on this instrument sat at 197 bar and 422 bar peak — a column change, not
a fault — so a long baseline would flag routine work.

**Cost.** The directory walk dominates (~0.44 s across a results tree of ~3700 runs)
while decoding a trace costs ~6 ms, so the walk is TTL-cached for 60 s and run
summaries are cached on file identity. Steady-state cost to `/status` is ~0.8 ms.
Nothing is written: `/status` stays side-effect-free, so there is no history file —
the baseline is recomputed from the runs on disk.

### What it catches, measured

Replaying this instrument's real run history through the probe, treating each run in
turn as the newest:

| Run | peak | baseline | delta | |
|---|---|---|---|---|
| Blank 09:53 | 1261.9 bar | 386.9 | **+226%** | flagged |
| SG-02-097A-2 | 508.6 bar | 386.9 | **+31.5%** | flagged |
| SG-02-097B-2 | 421.6 bar | 352.9 | **+19.5%** | flagged |
| SG-02-097B-3 | 424.0 bar | 422.9 | +0.3% | quiet |

The morning of 2026-08-11 began with a blank at **1262 bar against a 1300 bar pump
limit** — a serious blockage that nothing in `/status` reported at the time — then
settled over three runs to a stable ~422 bar. The check flags all three runs of that
transient and goes quiet once pressure stabilises. That is the intended behaviour
rather than noise: the pressure genuinely moved 1262 → 508 → 422 across the morning.
It does mean a column change will flag for a run or two afterwards.

### Advisory, on purpose

Drift appends `check_lc_pressure` to `required_actions` and populates
`details.run_pressure`, but sets **no fault**, changes **no** `equipment_status`, and
blocks **no** submission. The 15 % threshold has no tuning data behind it yet, and a
heuristic that can halt the lab needs a baseline first. Promoting it to blocking is a
deliberate follow-up, not something to back into.

## Phase 3 — real-time pressure (deferred)

Two candidate live sources, both problematic:

**`MonitorTrace` lines in `RCDriver.log`** — already on disk. The pump emits four
traces carrying the same raw driver counts as the archived `.IT` traces, so the
Phase-2 scale factors apply directly:

```
MonitorTrace: 1 → pressure ×0.005 bar   2 → flow ×1e-6 mL/min   3 → %A ×0.001   4 → %B ×0.001
```

(`78062` = 390.3 bar at 0.5 mL/min, falling to `1654` = 8.3 bar when flow drops to
0.01 mL/min. An earlier reading of trace 1 as `raw/1000` — 78 bar — was wrong; the
`.IT` scale factors in Phase 2, validated against independently known values, settle
it.)

Sampled every ~1.5 s with flow and %B alongside — enough to separate a real drop from
a gradient viscosity change. **Coverage kills it:** the line is only written when
OpenLab's online plot polls the buffer. 2026-08-11 shows one pump pressure line per
run (10:19:12, 10:31:47, 10:44:42), ~81 points ≈ 2 min of a 12.5-min run; two of the
three retained 10 MB rotated logs contain **zero**. Opportunistic per-run sanity
check at best, not a monitor.

**SignalBufferService on port 9753** — the only true real-time path. Not REST (GET
returns 405 on every path). Reflecting
`Agilent.OpenLAB.Acquisition.SignalBufferContracts.dll`:

```
ISBSConnectRealtimePlot: ConnectToSignalBuffer(), SignalActivated(sDeviceName, nModuleNumber, nSignalID), ...
InformClient / SignalDataAccessor / SignalAccessor  — with Publisher + Subscription properties
```

A duplex publish/subscribe contract over Agilent's own `Agilent.OpenLab.Communication`
bus. Unreachable from plain Python; reachable via pythonnet from the sensor daemon,
which already runs in the Moses env and holds a live `InstrumentController`. Signal
IDs are already guessed in the daemon (`3/401` pressure, `3/402` flow, `1/201` column
temp).

Deferred deliberately: Phase 1 catches the faults that latch the instrument and
Phase 2 catches slow degradation, which leaves this worth doing only if a run must be
aborted *mid-flight*. The effort is open-ended — an undocumented duplex contract,
with no guarantee a second subscriber is permitted while OpenLab holds its own.

## Phase 4 — getting it to a human (not yet scoped)

Detection only helps if someone hears it. `/status` is poll-only and there is no
webhook anywhere in `src/`. Whether the sidecar pushes (to the lab service, to a
dashboard alert) or the aggregator polls harder is a lab-wide decision, not a
device-local one, so it is called out here rather than designed.

## Order of work

| Phase | What | Status |
|---|---|---|
| 1 | LC module fault channel from `RCDriver.log` | **done** |
| 2 | Post-run pressure QC from the `.dx` archive | **done** |
| 4 | Push path so a fault reaches a human | open — needs a lab-wide decision |
| 3 | Real-time pressure via SignalBuffer | open — only if mid-run abort is required |

Phases 1 and 2 are complementary: Phase 1 catches the faults that latch the
instrument, Phase 2 catches the degradation that never trips a limit. Phase 4 is what
turns either into something a person actually sees, and is worth doing before Phase 3
— detection nobody is notified of is the weaker half of the problem.

### Follow-ups left open

- **Promote pressure drift to blocking.** Advisory today; revisit once there is enough
  baseline data to trust a threshold.
- **Tune the drift threshold** (`PRESSURE_DRIFT_PCT`, 15 %) against real history.
- **Unattended recovery.** The acknowledgment is a human in the loop, which does
  nothing for a fault at 02:00 with no run scheduled. Treating OLSS `Idle` +
  `softwareStatus OK`, observed after the fault, as recovery evidence would clear
  it unattended — but whether OLSS returns to `Idle` *immediately* after an abort
  is unverified, and if it does, keying on it would defeat the interlock
  entirely. Needs OLSS state sampled across a real fault before it can be
  trusted; Phase 4's push path is the better answer to the same 02:00 problem.
- **Confirm the pump's pressure-limit event code.** A limit trip surfaces through the
  Phase-1 channel like any other fault, but has not occurred in the retained logs, so
  the code is unconfirmed. Nothing depends on knowing it — codes pass through as
  opaque strings.
- **`read_module_states` slurps the whole `RCDriver.log`** (up to 10 MB) per poll.
  Pre-existing, and now the only remaining slurp in that module.
