"""Operator acknowledgments for LC module hardware faults.

Agilent's driver announces a fault but **never writes a fault-cleared line**, so
``probes/rc_driver_log.read_lc_faults`` cannot see a recovery directly. It infers
one from the module's own ``STAT?`` readiness reply — but ``STAT?`` is only
written at prerun, so a module that is fixed while the instrument sits idle never
gets to say so. Until now the only remaining exit was ``LC_FAULT_WINDOW_S``
(default one hour), during which ``/status`` stayed ``error`` and the
``subsystem_fault`` interlock refused ``run.submit`` — even though OpenLab itself
had long since gone back to green.

That gap is what this module closes. An operator who has physically checked the
module records an acknowledgment here, and the evidence they acknowledged stops
counting. It is the same shape as ``control/consumables.py``: a file-backed store
that only the ``/control/*`` endpoints mutate, plus a **pure** suppression
function the status builder applies, so ``/status`` stays side-effect-free.

What an ack covers
------------------
An ack clears *the evidence that existed when it was taken*, and nothing else.
Both fault channels are acknowledged together, because both must go quiet or the
module stays red:

* ``lc_faults`` entries for that role — the driver's own error events.
* A stale ``STAT?`` still carrying the ``ERROR`` flag. Suppressing this matters:
  the multisampler's last reply before an abort is ``ERROR, NOT_READY``, and
  ``status_builder._module_state_with_olss`` reads those flags straight into a
  component state of ``error``. Clearing the fault list alone would leave the
  card red anyway.

Re-arming is therefore automatic and needs no expiry: any fault logged *after*
the ack, or any newer ``STAT?`` that still says ``ERROR``, is evidence the
operator has not seen, so it counts in full.

Why timestamps rather than a wall clock
---------------------------------------
Evidence is matched by its own event time, not by "is it older than the ack".
Fault dicts carry an explicit ``timestamp``; each ``STAT?`` carries
``module_<role>_stat_at``. Both come from the RC driver log's naive-local clock,
while an ack is stamped in UTC, and ``age_s`` is measured from whenever the probe
last polled — so comparing across those domains would invite off-by-one-hour and
one-poll-late races. Storing the acknowledged event times verbatim, in the
driver's own domain, makes the comparison exact: the same reply always maps to
the same instant no matter when it is read.

A module acked while nothing is wrong records ``null`` thresholds, which suppress
nothing — the ack is inert rather than a blanket amnesty for future faults.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..probes.rc_driver_log import summarize_faults

logger = logging.getLogger(__name__)

# The module roles that can carry a fault. Mirrors the values of _MODULE_ROLES
# in probes/rc_driver_log.py, which is the parse-side source.
FAULT_ROLES: tuple[str, ...] = (
    "binary_pump",
    "dad_detector",
    "column_thermostat",
    "multisampler",
)


def _parse(value: Any) -> datetime | None:
    """Parse an ISO timestamp from a signal or a stored ack, or ``None``."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _not_after(evidence: Any, threshold: Any) -> bool:
    """True iff ``evidence`` is an event at or before ``threshold``.

    Both sides come from the RC driver log, so they share a clock domain — but a
    fault timestamp carries the local offset while ``stat_at`` is naive, and
    Python refuses to compare the two. Drop to a naive comparison when either
    side is naive, which is correct here precisely because it is the same wall
    clock on the same machine either way.
    """
    ev, th = _parse(evidence), _parse(threshold)
    if ev is None or th is None:
        return False
    if (ev.tzinfo is None) != (th.tzinfo is None):
        ev, th = ev.replace(tzinfo=None), th.replace(tzinfo=None)
    return ev <= th


def evidence_at_ack(signals: dict[str, Any], role: str) -> dict[str, Any]:
    """The fault evidence currently visible for ``role``, as ack thresholds.

    Pure. Returns ``{"faults_through": iso|None, "stat_through": iso|None}`` —
    the newest fault event for that module and the ``STAT?`` reply in force,
    which is exactly what an ack taken right now should cover.
    """
    newest: str | None = None
    for fault in signals.get("lc_faults") or ():
        if fault.get("role") != role:
            continue
        ts = fault.get("timestamp")
        if isinstance(ts, str) and (newest is None or not _not_after(ts, newest)):
            newest = ts
    stat_at = signals.get(f"module_{role}_stat_at")
    return {
        "faults_through": newest,
        "stat_through": stat_at if isinstance(stat_at, str) else None,
    }


def _has_error_flag(flags: Any) -> bool:
    return isinstance(flags, list) and any(str(f).upper() == "ERROR" for f in flags)


def _flags_to_state(flags: list[str]) -> str:
    """Composite state for a ``STAT?`` reply whose ERROR token was acknowledged.

    Deliberately not the probe's private ``_stat_flags_to_state``: that function
    reports what the module actually said, and reusing it here would mean
    re-ranking a reply we have edited. This one only has to answer what is left
    once ERROR is gone — the readiness the operator has vouched for.
    """
    fs = {str(f).upper() for f in flags}
    if "NOT_READY" in fs or "NOTREADY" in fs:
        return "not_ready"
    if any(f in fs for f in ("RUN", "PRERUN", "POSTRUN")):
        return "busy"
    if "READY" in fs:
        return "ready"
    return "unknown"


def apply_fault_acks(
    signals: dict[str, Any], acks: dict[str, dict[str, Any]] | None
) -> dict[str, Any]:
    """Return ``signals`` with acknowledged fault evidence removed.

    Pure — the input dict is never mutated and no I/O happens, so ``/status`` can
    call it on every poll. Returns the original object unchanged when there is
    nothing to suppress, keeping the common (no acks) path free.

    Applied once, at the top of ``build_status``, so that *every* downstream
    reader — the component cards, ``faulted_modules``, ``last_error``,
    ``details.lc_faults``, and ``errored_lc_modules()`` behind the router's
    ``subsystem_fault`` interlock — works from one consistent view. Suppressing
    at each of those sites individually is exactly what would let them drift
    apart.

    An acknowledged ``STAT?`` has only its ``ERROR`` token dropped, leaving
    ``NOT_READY`` to speak for itself: the module has not reported ready since,
    and inventing a ``READY`` it never sent would be a worse lie than showing a
    stale not-ready. ``not_ready`` is enough to clear the red, because the status
    gate and the interlock both test for ``error`` exactly.
    """
    if not acks:
        return signals

    faults: list[dict[str, Any]] = list(signals.get("lc_faults") or ())
    kept: list[dict[str, Any]] = []
    suppressing: dict[str, str] = {}

    for fault in faults:
        role = str(fault.get("role"))
        ack = acks.get(role)
        if ack and _not_after(fault.get("timestamp"), ack.get("faults_through")):
            suppressing[role] = str(ack.get("acked_at", ""))
            continue
        kept.append(dict(fault))

    stat_overrides: dict[str, Any] = {}
    for role in FAULT_ROLES:
        ack = acks.get(role)
        if not ack:
            continue
        flags = signals.get(f"module_{role}_stat_flags")
        if not _has_error_flag(flags):
            continue
        if not _not_after(
            signals.get(f"module_{role}_stat_at"), ack.get("stat_through")
        ):
            continue
        cleared = [f for f in flags if str(f).upper() != "ERROR"]
        stat_overrides[f"module_{role}_stat_flags"] = cleared
        stat_overrides[f"module_{role}_state"] = _flags_to_state(cleared)
        suppressing[role] = str(ack.get("acked_at", ""))

    if len(kept) == len(faults) and not stat_overrides:
        return signals

    out = dict(signals)
    out.update(stat_overrides)
    # Drop the whole lc_fault_* family before re-deriving it: summarize_faults
    # returns {} when nothing survives, and a stale lc_fault_active / _message
    # left behind would keep the instrument in `error` with no fault to name.
    for key in (
        "lc_faults",
        "lc_fault_active",
        "lc_fault_severity",
        "lc_fault_message",
        "lc_fault_module_roles",
    ):
        out.pop(key, None)
    out.update(summarize_faults(kept))
    if suppressing:
        out["fault_acks_active"] = suppressing
    return out


class FaultAcks:
    """Thread-safe, file-backed store of module fault acknowledgments.

    State shape on disk::

        {"multisampler": {"faults_through": iso | null,
                          "stat_through":   iso | null,
                          "acked_at":       iso,
                          "owner":          str | null,
                          "note":           str | null}}

    Persisted so a service restart cannot resurrect a fault an operator has
    already cleared — the same reasoning as ``ConsumableAcks``.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = Path(path) if path else None
        self._acks: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("fault acks: could not load %s: %s", self._path, exc)
            return
        if isinstance(data, dict):
            # Keep only well-formed entries for roles we still recognize.
            self._acks = {
                str(k): v
                for k, v in data.items()
                if isinstance(v, dict) and str(k) in FAULT_ROLES
            }

    def _save_locked(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._acks, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("fault acks: could not save %s: %s", self._path, exc)

    def record(
        self,
        role: str,
        signals: dict[str, Any],
        owner: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Acknowledge ``role`` against the evidence currently in ``signals``."""
        ack = {
            **evidence_at_ack(signals, role),
            "acked_at": datetime.now(timezone.utc).isoformat(),
            "owner": owner,
            "note": note,
        }
        with self._lock:
            self._acks[role] = ack
            self._save_locked()
        logger.info(
            "LC module %s fault acknowledged by %s (faults_through=%s, stat_through=%s)",
            role,
            owner,
            ack["faults_through"],
            ack["stat_through"],
        )
        return dict(ack)

    def clear(self, role: str) -> bool:
        """Withdraw an acknowledgment. True if one was present."""
        with self._lock:
            if role in self._acks:
                del self._acks[role]
                self._save_locked()
                logger.info("LC module %s fault acknowledgment withdrawn", role)
                return True
            return False

    def get(self, role: str) -> dict[str, Any] | None:
        with self._lock:
            ack = self._acks.get(role)
            return dict(ack) if ack else None

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._acks.items()}
