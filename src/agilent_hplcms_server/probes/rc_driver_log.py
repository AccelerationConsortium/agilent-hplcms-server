"""Probe that reads solvent/waste bottle levels and per-module states from RCDriver.log.

The active RCDriver.log is written by AcquisitionClient.exe and contains:
  - Periodic DoRequestResponse messages from the pump (G7120A) that embed full
    device-settings XML with real-time solvent and waste bottle fill levels.
  - LDT SendInstruction entries for STAT?, LAMP:INFO?, ACT:PUMP?, LIST "HOTEL_STATE"
    and other per-module commands; these are written during prerun and operation.

Returned signals (bottle levels)
---------------------------------
Agilent UI label → XML tag → signal name:
  A1 → BottleFillingA → solvent_a1_volume_ml / solvent_a1_capacity_ml
  A2 → BottleFillingB → solvent_a2_volume_ml / solvent_a2_capacity_ml
  B1 → BottleFillingC → solvent_b1_volume_ml / solvent_b1_capacity_ml
  B2 → BottleFillingD → solvent_b2_volume_ml / solvent_b2_capacity_ml
Slots with max capacity == 0 (unconfigured) are omitted from the output.
waste_volume_ml, waste_capacity_ml   — waste bottle (mL)
waste_near_capacity                  — True when volume >= not-ready threshold
solvent_a1_low, solvent_a2_low,
solvent_b1_low, solvent_b2_low       — True when volume <= not-ready threshold
rc_driver_data_age_s                 — age of the last data point in seconds

Returned signals (per-module status — keyed by module role)
------------------------------------------------------------
module_binary_pump_state        — "ready" | "busy" | "error" | "not_ready" | "unknown"
module_binary_pump_stat_flags   — list[str] of STAT? flag tokens
module_binary_pump_stat_age_s   — seconds since last STAT? was seen for this module
module_binary_pump_stat_at      — ISO timestamp of that STAT? (naive local, the
                                  driver log's own clock domain)
module_binary_pump_on           — True when ACT:PUMP? replied 1 (pump running)
module_dad_detector_state       — same states as above (G7117B)
module_dad_detector_stat_flags  — STAT? flag list
module_dad_detector_stat_age_s  — age of STAT? data
module_dad_detector_stat_at     — ISO timestamp of that STAT? (naive local)
module_dad_lamp_on              — True when last LAMP command sent was LAMP 1
module_dad_lamp_rated_hours     — rated lamp lifetime in hours (from LAMP:INFO?)
module_dad_lamp_hours_used      — estimated accumulated on-time in hours
module_column_thermostat_state  — column compartment state (G7116B)
module_column_thermostat_stat_flags
module_column_thermostat_stat_age_s
module_column_thermostat_stat_at
module_column_thermostat_on     — True when last THRM command sent was THRM 1
module_multisampler_state       — multisampler state (G7167B)
module_multisampler_stat_flags
module_multisampler_stat_age_s
module_multisampler_stat_at
module_multisampler_drawers_occupied — hotel halves reporting a container (non-zero)
module_multisampler_drawers_total    — hotel halves reported (2 per drawer: D#F + D#B)

Returned signals (module hardware faults — see read_lc_faults)
--------------------------------------------------------------
lc_faults             — list[dict] of active faults, most actionable first
lc_fault_active       — True when at least one fault is active
lc_fault_severity     — "critical" | "error" of the top fault
lc_fault_message      — human text of the top fault ("G7167B multisampler: Leak detected")
lc_fault_module_roles — roles of the modules currently faulted
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_AGE_S: int = 7 * 24 * 3600  # 7 days — device settings only logged at session start; levels change slowly
_LOG_FILENAME: str = "RCDriver.log"

_RC_TIMESTAMP_RE = re.compile(
    r"Timestamp: (\d{2}-\d{2}-\d{4} \d{1,2}:\d{2}:\d{2}[.,]\d+)"
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


def _iter_lines(path: Path) -> Iterator[str]:
    """Stream a log file line by line.

    The fault scan only ever needs one line at a time, and these logs reach
    10 MB, so streaming keeps peak memory flat where ``_read_file`` would hold
    the whole decoded file. Measured on a 10 MB RCDriver.log: streaming the
    whole file costs ~18 ms, against ~10 MB resident for the slurped form.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            yield from f
    except OSError as exc:
        logger.debug("rc_driver_log: stream failed: %s", exc)


def _find_target_line(log_dir: Path) -> str | None:
    """Return the most recent DoRequestResponse+BottleSolvents line across all RCDriver logs.

    Searches every *RCDriver*.log file in log_dir, newest-modified first, and
    returns the last matching line from the first file that contains one.
    Device-settings responses are only emitted at session start, so they often
    live in a rotated log rather than the currently-active file.
    """
    try:
        candidates = sorted(
            log_dir.glob("*RCDriver*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for log_path in candidates:
        text = _read_file(log_path)
        for line in reversed(text.splitlines()):
            if "DoRequestResponse" in line and "BottleSolvents" in line:
                return line
    return None


# Maps Agilent module model codes to the role keys used in signal names.
_MODULE_ROLES: dict[str, str] = {
    "G7120A": "binary_pump",
    "G7117B": "dad_detector",
    "G7116B": "column_thermostat",
    "G7167B": "multisampler",
}

# STAT? entries are written during prerun/run; keep up to 7 days to cover
# instruments that run only a few times per week.
_MODULE_STAT_MAX_AGE_S: int = 7 * 24 * 3600
# Non-STAT signals (lamp/pump commands) are keep-until-overridden.
_MODULE_CMD_MAX_AGE_S: int = 7 * 24 * 3600


def _stat_flags_to_state(flags: list[str]) -> str:
    """Map a list of STAT? tokens to a canonical component state string."""
    fs = {f.upper() for f in flags}
    if "ERROR" in fs:
        return "error"
    if "NOT_READY" in fs or "NOTREADY" in fs:
        return "not_ready"
    if any(f in fs for f in ("RUN", "PRERUN", "POSTRUN")):
        return "busy"
    if "READY" in fs:
        return "ready"
    return "unknown"


def _stat_readiness(flags: list[str] | None) -> str | None:
    """Readiness carried by a STAT? reply, ignoring its run-phase token.

    ``_stat_flags_to_state`` ranks RUN/PRERUN/POSTRUN above READY because for a
    component card the run phase is the more useful thing to show. That ranking
    makes the composite state useless for recovery detection: this instrument
    stamps *every* STAT? reply with PRERUN (173 of 173 across all retained
    logs), so the state is never ``"ready"`` and a module could never be
    observed to recover. The readiness flags themselves are unambiguous —
    ``NO_ERROR, READY`` means the module is back — so read those directly.

    Returns ``None`` when the reply carries no readiness flag at all.
    """
    if not flags:
        return None
    fs = {f.upper() for f in flags}
    if "ERROR" in fs:
        return "error"
    if "NOT_READY" in fs or "NOTREADY" in fs:
        return "not_ready"
    if "READY" in fs:
        return "ready"
    return None


def read_module_states(log_dir: str | Path) -> dict[str, Any]:
    """Parse per-module status signals from LDT SendInstruction entries in RCDriver.log.

    Reads the file once and scans backwards, collecting the most recent entry of
    each type per module.  Returns an empty dict if the log is missing or unreadable.
    """
    log_path = Path(log_dir) / _LOG_FILENAME
    if not log_path.exists():
        return {}

    text = _read_file(log_path)
    if not text:
        return {}

    # Tracking state: keyed by module code or signal name
    stat_seen: dict[str, tuple[datetime, list[str]]] = {}  # module → (ts, flags)
    lamp_info_seen: tuple[datetime, str] | None = None     # G7117B LAMP:INFO?
    lamp_on_seen: bool | None = None                        # G7117B last LAMP N command
    pump_on_seen: bool | None = None                        # G7120A ACT:PUMP? reply
    hotel_seen: tuple[datetime, str] | None = None          # G7167B LIST "HOTEL_STATE"
    thrm_on_seen: bool | None = None                        # G7116B last THRM N command

    # Track whether each module has had all its interesting signals found
    all_found = False
    lines = text.splitlines()

    for line in reversed(lines):
        if "LDT SendInstruction" not in line:
            continue

        m_mod = re.search(r"Module:\[([^:]+):", line)
        if not m_mod:
            continue
        module_code = m_mod.group(1)
        role = _MODULE_ROLES.get(module_code)

        ts = _parse_timestamp(line)

        # ── STAT? for any known module ─────────────────────────────────────
        if module_code not in stat_seen and "Instruction:[STAT?]" in line:
            m = re.search(r'STAT "([^"]+)"', line)
            if m and ts:
                flags = [f.strip() for f in m.group(1).split(",")]
                stat_seen[module_code] = (ts, flags)

        # ── G7117B (DAD) specific ──────────────────────────────────────────
        elif module_code == "G7117B":
            if lamp_info_seen is None and "Instruction:[LAMP:INFO?]" in line:
                m = re.search(r'LAMP:INFO "([^"]+)"', line)
                if m and ts:
                    lamp_info_seen = (ts, m.group(1))
            elif lamp_on_seen is None:
                m = re.search(r"Instruction:\[LAMP (\d)\]", line)
                if m:
                    lamp_on_seen = m.group(1) == "1"

        # ── G7120A (Pump) specific ─────────────────────────────────────────
        elif module_code == "G7120A":
            if pump_on_seen is None and "Instruction:[ACT:PUMP?]" in line:
                m = re.search(r"RA 00000 ACT:PUMP (\d)", line)
                if m:
                    pump_on_seen = m.group(1) == "1"

        # ── G7167B (Multisampler) specific ────────────────────────────────
        elif module_code == "G7167B":
            if hotel_seen is None and 'Instruction:[LIST "HOTEL_STATE"]' in line:
                # Match explicit drawer entries so each [...] is fully captured.
                # The outer \]\] covers the HOTEL_STATE list closer and the Reply
                # field closer; they are NOT consumed by the inner group.
                m = re.search(
                    r"HOTEL_STATE: \[HOTEL_STATE: ((?:\[\d+(?:,\d+)+\],? *)+)\]\]",
                    line,
                )
                if m and ts:
                    hotel_seen = (ts, m.group(1))

        # ── G7116B (Column Comp) specific ─────────────────────────────────
        elif module_code == "G7116B":
            if thrm_on_seen is None:
                m = re.search(r"Instruction:\[THRM (\d)\]", line)
                if m:
                    thrm_on_seen = m.group(1) == "1"

        # Early exit once we've found everything we care about
        if (
            len(stat_seen) >= len(_MODULE_ROLES)
            and lamp_info_seen is not None
            and lamp_on_seen is not None
            and pump_on_seen is not None
            and hotel_seen is not None
            and thrm_on_seen is not None
        ):
            break

    out: dict[str, Any] = {}
    now = datetime.now()

    # ── Per-module STAT? signals ───────────────────────────────────────────
    for module_code, (ts, flags) in stat_seen.items():
        role = _MODULE_ROLES.get(module_code)
        if not role:
            continue
        age_s = (now - ts).total_seconds()
        if age_s > _MODULE_STAT_MAX_AGE_S:
            continue
        out[f"module_{role}_state"] = _stat_flags_to_state(flags)
        out[f"module_{role}_stat_flags"] = flags
        out[f"module_{role}_stat_age_s"] = round(age_s, 1)
        # Absolute time of this STAT? reply, in the driver log's own (naive
        # local) clock domain. `stat_age_s` is measured from the poll, so the
        # same reply reports a different age on every read; an operator fault
        # acknowledgment needs a stable identity for the evidence it clears
        # (see control/fault_acks.py).
        out[f"module_{role}_stat_at"] = ts.isoformat()

    # ── DAD lamp signals ───────────────────────────────────────────────────
    if lamp_info_seen is not None:
        ts, info_str = lamp_info_seen
        if (now - ts).total_seconds() <= _MODULE_CMD_MAX_AGE_S:
            parts = [p.strip() for p in info_str.split(",")]
            # Field 4 (index 4) = rated lamp lifetime hours (typically 2000)
            if len(parts) >= 5:
                try:
                    out["module_dad_lamp_rated_hours"] = int(parts[4])
                except ValueError:
                    pass
            # Field 7 (index 7) = accumulated on-time in milliseconds
            if len(parts) >= 8:
                try:
                    burn_ms = int(parts[7])
                    burn_hours = burn_ms / 1_000 / 3_600
                    if 0 < burn_hours < 50_000:
                        out["module_dad_lamp_hours_used"] = round(burn_hours, 1)
                except ValueError:
                    pass

    if lamp_on_seen is not None:
        out["module_dad_lamp_on"] = lamp_on_seen

    # ── Pump signals ───────────────────────────────────────────────────────
    if pump_on_seen is not None:
        out["module_binary_pump_on"] = pump_on_seen

    # ── Column thermostat ──────────────────────────────────────────────────
    if thrm_on_seen is not None:
        out["module_column_thermostat_on"] = thrm_on_seen

    # ── Multisampler hotel state ───────────────────────────────────────────
    if hotel_seen is not None:
        ts, state_str = hotel_seen
        if (now - ts).total_seconds() <= _MODULE_CMD_MAX_AGE_S:
            drawers = re.findall(r"\[(\d+),(\d+),(\d+),(\d+),(\d+)\]", state_str)
            if drawers:
                # HOTEL_STATE returns one entry per physical drawer (field 0 =
                # drawer number 1-4), and each drawer has two independently
                # loadable halves — the front/back split the D#F / D#B sample
                # addresses use. Fields 3 and 4 are those halves, so the hotel
                # holds 2 x len(drawers) containers, not len(drawers): the
                # multisampler's own device layout (OpenLab .scml) declares
                # eight user-addressable holders, D1F..D4B.
                #
                # Reading only field 3 reported four slots and counted the
                # other four as if they did not exist — D1 and D3 hold a plate
                # in the half this missed and were reported empty throughout.
                #
                # Fields 3/4 are tri-state (0/1/2). 0 is empty; 1 and 2 are
                # both non-empty, and which is which is NOT established — the
                # values change within seconds during acquisition, so they
                # likely distinguish "loaded" from "in transport / being
                # accessed" rather than describing what an operator loaded.
                # Counting non-zero is therefore the strongest claim the data
                # supports. Do not gate anything safety-critical on this until
                # the codes are confirmed at the instrument.
                halves = [h for d in drawers for h in (d[3], d[4])]
                out["module_multisampler_drawers_occupied"] = sum(
                    1 for h in halves if h != "0"
                )
                out["module_multisampler_drawers_total"] = len(halves)

    return out


# ── Module hardware faults ────────────────────────────────────────────────────
#
# The RC driver writes three correlated lines per fault; we parse the
# `ControlIF log error` one because it carries severity, module, text and code
# together:
#
#   ControlIF log error: eLogAndAbortSequence, G7167B:DEBAS04772 - Leak detected [64 64, 0];Category: ...
#
# The `eLog*` token is the severity the driver itself assigns to the event.
# `eLogInformationMessage` is routine operational chatter ("Valve is switched to
# bypass") and is dropped; only the two abort levels are faults.
_FAULT_SEVERITY: dict[str, str] = {
    "eLogAndAbortSequence": "critical",
    "eLogAndAbortCurrentRunOnly": "error",
}

_FAULT_RE = re.compile(
    r"ControlIF log error: (eLog\w+), "      # severity token
    r"([A-Za-z0-9]+):(\S+?) - "              # module model : serial
    r"(.*?)"                                 # human text (lazy)
    r"(?: \[(\d+) \d+, \d+\])?"              # optional [code code, n]
    r";Category:"                            # end-of-message anchor
)

# Abort-level events that are not hardware faults: the controller asking the
# module to stop is how a normal abort looks from the driver's point of view.
_FAULT_BENIGN_RE = re.compile(
    r"Controller stop automation request"
    # "Analysis aborted by another module" is not a fault of the module that
    # logs it. Two independent pieces of evidence say so. The driver files it as
    # `EV 00150` — an *event* — in all 21 occurrences across every retained log,
    # never once as the `EE` an error carries (the needle crash of 2026-08-20
    # 20:53:47 is `EE 25225`, logged 100 ms before the three cascade lines it
    # caused). And it is written just as readily for a *software* abort: three
    # runs aborted via `UnifiedControl.AbortRun` between 21:13 and 21:17 that
    # evening each stamped it on the multisampler, DAD and pump, latching all
    # three into `error` and refusing `run.submit` for an hour over what was
    # someone pressing stop.
    #
    # Whatever really aborted the run says so itself: an LC module logs its own
    # `EE` fault, which is what gates. The gap this leaves is an abort raised by
    # a module outside _MODULE_ROLES — the MS — which now reports nothing rather
    # than misattributing the fault to three innocent LC modules. Reporting the
    # wrong module is worse than reporting none: it sends a technician to the
    # pump for a mass-spec problem, and blocks submission on hardware that is
    # fine.
    r"|Analysis aborted by another module",
    re.IGNORECASE,
)

# "Shutdown" is what every other module reports once one of them faults, so it
# describes the blast radius rather than the cause. Kept (the system really did
# shut down) but ranked below a causal fault so `lc_fault_message` names the
# leak rather than the cascade.
_FAULT_CASCADE_RE = re.compile(r"^Shutdown$", re.IGNORECASE)

_SEVERITY_RANK: dict[str, int] = {"critical": 0, "error": 1}

# Faults older than this are ignored. The log is append-only and — in every
# retained log on this instrument — a fault-cleared line is never written, so
# without a window a leak from last month would pin the instrument into `error`
# forever. See also the STAT?-recovery reconciliation in _drop_recovered().
DEFAULT_FAULT_WINDOW_S: int = 3600


def _fault_log_candidates(log_dir: Path, window_s: int) -> list[Path]:
    """Return every RCDriver log that could hold a fault inside the window.

    RCDriver.log rotates at 10 MB, which on a busy day is ~25 minutes, so a
    fault within the window is often in a rotated file. A log whose mtime
    predates the window cannot contain an in-window fault, so mtime filtering is
    both correct and cheap.
    """
    cutoff = datetime.now().timestamp() - window_s
    try:
        return [
            p
            for p in log_dir.glob("*RCDriver*.log")
            if p.stat().st_mtime >= cutoff
        ]
    except OSError:
        return []


def _drop_recovered(
    faults: list[dict[str, Any]], module_states: dict[str, Any]
) -> list[dict[str, Any]]:
    """Drop faults whose module has since reported itself READY.

    The driver never logs a fault-cleared line, so recovery is inferred from the
    module's own STAT? readiness flags: a STAT? that is *newer* than the fault
    (smaller age) and reports READY means the module came back. This is what
    makes a one-hour window safe — a genuinely unresolved fault keeps the module
    out of READY, so it survives the filter.

    Readiness is read via :func:`_stat_readiness`, NOT from the composite
    ``module_<role>_state``: that state ranks the run-phase token above READY,
    and every STAT? this instrument emits carries PRERUN, so comparing it to
    ``"ready"`` never matched and no fault ever cleared on recovery — it only
    aged out of the window. The composite state is still the fallback for a
    module whose flags are missing.
    """
    out: list[dict[str, Any]] = []
    for fault in faults:
        role = fault.get("role")
        if role:
            state = _stat_readiness(
                module_states.get(f"module_{role}_stat_flags")
            ) or module_states.get(f"module_{role}_state")
            stat_age = module_states.get(f"module_{role}_stat_age_s")
            if (
                state == "ready"
                and stat_age is not None
                and stat_age < fault["age_s"]
            ):
                continue
        out.append(fault)
    return out


def read_lc_faults(
    log_dir: str | Path,
    window_s: int = DEFAULT_FAULT_WINDOW_S,
    module_states: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse active LC module hardware faults from the RC driver logs.

    Parameters
    ----------
    log_dir:
        Directory holding ``RCDriver.log`` and its rotated siblings.
    window_s:
        Only faults this recent count. ``0`` disables fault detection entirely.
    module_states:
        The dict from :func:`read_module_states`, used to clear faults whose
        module has since reported READY. Omitting it means no recovery
        reconciliation — faults then persist for the whole window.

    Returns an empty dict when there is nothing to report, so the status builder
    treats missing keys as "no known fault".
    """
    if window_s <= 0:
        return {}
    log_dir_path = Path(log_dir)
    if not log_dir_path.exists():
        return {}

    now = datetime.now()
    faults: list[dict[str, Any]] = []
    # A fault is written to several files (and repeated within one), so
    # de-duplicate on the identity of the event itself.
    seen: set[tuple[str, str, str]] = set()

    for path in _fault_log_candidates(log_dir_path, window_s):
        for line in _iter_lines(path):
            m = _FAULT_RE.search(line)
            if m is None:
                continue
            token, module_code, serial, message, code = m.groups()
            severity = _FAULT_SEVERITY.get(token)
            if severity is None:
                continue
            message = message.strip().rstrip(".")
            if _FAULT_BENIGN_RE.search(message):
                continue
            ts = _parse_timestamp(line)
            if ts is None:
                continue
            age_s = (now - ts).total_seconds()
            if age_s > window_s or age_s < 0:
                continue
            key = (module_code, message, ts.isoformat())
            if key in seen:
                continue
            seen.add(key)
            faults.append(
                {
                    "module_code": module_code,
                    "role": _MODULE_ROLES.get(module_code),
                    "serial": serial,
                    "message": message,
                    "code": code,
                    "severity": severity,
                    # RC driver timestamps are naive *local* time; astimezone()
                    # attaches the real local offset so downstream parsing does
                    # not silently read them as UTC.
                    "timestamp": ts.astimezone().isoformat(),
                    "age_s": round(age_s, 1),
                }
            )

    faults = _drop_recovered(faults, module_states or {})
    return summarize_faults(faults)


def summarize_faults(faults: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll a fault list up into the ``lc_fault_*`` signals.

    Split out from :func:`read_lc_faults` because it is applied twice: once here
    over everything parsed from the logs, and again in
    ``control/fault_acks.apply_fault_acks`` over what survives an operator
    acknowledgment. Both paths must agree on which fault is "top" and on the
    exact wording of ``lc_fault_message``, so the ranking lives in one place.

    Sorts in place (most actionable first) and returns ``{}`` for an empty list,
    so the status builder reads missing keys as "no known fault".
    """
    if not faults:
        return {}

    # Most actionable first: severity, then causal-before-cascade, then newest.
    faults.sort(
        key=lambda f: (
            _SEVERITY_RANK.get(f["severity"], 9),
            1 if _FAULT_CASCADE_RE.match(f["message"]) else 0,
            f["age_s"],
        )
    )
    top = faults[0]
    label = top["role"] or top["module_code"]
    return {
        "lc_faults": faults,
        "lc_fault_active": True,
        "lc_fault_severity": top["severity"],
        "lc_fault_message": f"{top['module_code']} {label}: {top['message']}",
        "lc_fault_module_roles": sorted(
            {f["role"] for f in faults if f["role"]}
        ),
    }


def read_rc_driver_log(log_dir: str | Path) -> dict[str, Any]:
    """Parse the latest pump device-settings from RCDriver.log.

    Returns an empty dict on any failure — missing file, stale data, or parse
    error.  The status builder treats missing keys as unknown / not available.
    """
    log_dir_path = Path(log_dir)
    if not log_dir_path.exists():
        return {}

    target_line = _find_target_line(log_dir_path)
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

    # Parse all four solvent channels.  Agilent UI labels vs XML tag suffix:
    #   A1 → A,  A2 → B,  B1 → C,  B2 → D
    # Values in the log are litres; we convert to mL.  Slots with max == 0
    # are unconfigured (no bottle expected) and are skipped.
    _SOLVENT_SLOTS = [
        ("a1", "A"),
        ("a2", "B"),
        ("b1", "C"),
        ("b2", "D"),
    ]

    # Waste bottle uses bare tag names (no A/B/C/D suffix).
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

    for slot, tag in _SOLVENT_SLOTS:
        vol = _re_float(target_line, f"BottleFilling{tag}HighRes")
        cap = _re_float(target_line, f"BottleMaxFilling{tag}HighRes")
        if vol is not None and cap is not None and cap > 0:
            out[f"solvent_{slot}_volume_ml"] = round(vol * 1000, 1)
            out[f"solvent_{slot}_capacity_ml"] = round(cap * 1000, 1)
            if sol_limit is not None:
                out[f"solvent_{slot}_low"] = vol <= sol_limit

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
