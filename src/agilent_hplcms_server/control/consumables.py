"""Operator acknowledgments for consumable (waste / solvent) warnings.

The sidecar can only *read* OpenLab's bottle-fill numbers (parsed from
``RCDriver.log``); it cannot reset OpenLab's accumulating estimate. So the waste
``near_capacity`` warning — and the solvent ``low`` warnings — would stay latched
forever after a physical empty / refill, training operators to ignore them.

This module records an operator acknowledgment ("I emptied the waste bottle" /
"I refilled solvent A1") that **suppresses** that consumable's warning until the
raw estimate shows the condition is genuinely due again:

* **waste** is *high-bad* (warns when volume ≥ the not-ready limit). After an
  ack, the warning re-arms once the raw estimate climbs ``rearm_delta_ml`` above
  the acked level — real new waste since the empty.
* **solvents** are *low-bad* (warn when volume ≤ the not-ready limit). After an
  ack, the warning re-arms once the raw estimate falls ``rearm_delta_ml`` below
  the acked level — genuinely consumed again.

If OpenLab's estimate never moves (the common case — it is not reset on a
physical empty), the ack simply keeps the warning suppressed: the operator's
explicit "I handled it" is the best truth available, and ``details.*_reset_at``
keeps it transparent on the dashboard.

Suppression is a **pure function** of ``(ack, raw_value)`` so ``/status`` stays
side-effect-free; only the ``/control/consumables/*`` endpoints mutate state.
The state is persisted to a JSON file so a service restart never resurrects a
warning the operator already cleared.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Consumable keys and the direction of their "bad" side.
Direction = Literal["high", "low"]
WASTE_KEY = "waste"
SOLVENT_SLOTS = ("a1", "a2", "b1", "b2")

# Map a consumable key → (warn direction, raw-volume signal name).
def consumable_direction(key: str) -> Direction:
    return "high" if key == WASTE_KEY else "low"


def raw_volume_signal(key: str) -> str:
    return "waste_volume_ml" if key == WASTE_KEY else f"solvent_{key}_volume_ml"


def is_suppressed(
    ack: dict[str, Any] | None,
    raw_value: float | None,
    direction: Direction,
    rearm_delta_ml: float,
) -> bool:
    """True iff an active ack should suppress this consumable's warning.

    Pure — no I/O, no mutation. An ack with no comparable fresh reading is
    honored (suppress); otherwise the warning re-arms once the raw estimate has
    moved ``rearm_delta_ml`` back toward the warning side of where it was when
    acknowledged.
    """
    if not ack:
        return False
    raw_at_ack = ack.get("raw_at_ack")
    if raw_value is None or raw_at_ack is None:
        return True
    if direction == "high":
        # Waste only accumulates (up) between empties. Suppress within the band
        # above the acked level; re-arm when it climbs a delta above (new waste),
        # and treat a drop *below* the ack as an independent OpenLab reset that
        # makes the ack obsolete (either way, a low reading won't warn).
        return raw_at_ack <= raw_value < raw_at_ack + rearm_delta_ml
    # Solvents only deplete (down) between refills. Mirror image: suppress within
    # the band below the acked level; re-arm when it falls a delta below
    # (consumed again); a rise above the ack means OpenLab reflected the refill.
    return raw_at_ack - rearm_delta_ml < raw_value <= raw_at_ack


class ConsumableAcks:
    """Thread-safe, file-backed store of consumable acknowledgments.

    State shape on disk: ``{key: {"raw_at_ack": float | None, "acked_at": iso}}``
    for ``key`` in ``waste`` / ``a1`` / ``a2`` / ``b1`` / ``b2``.
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
            logger.warning("consumable acks: could not load %s: %s", self._path, exc)
            return
        if isinstance(data, dict):
            # Keep only well-formed entries.
            self._acks = {
                str(k): v for k, v in data.items() if isinstance(v, dict)
            }

    def _save_locked(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._acks, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("consumable acks: could not save %s: %s", self._path, exc)

    def record(self, key: str, raw_at_ack: float | None) -> dict[str, Any]:
        """Acknowledge a consumable at its current raw reading. Returns the ack."""
        ack = {
            "raw_at_ack": raw_at_ack,
            "acked_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._acks[key] = ack
            self._save_locked()
        logger.info("Consumable %s acknowledged at raw=%s", key, raw_at_ack)
        return dict(ack)

    def clear(self, key: str) -> None:
        with self._lock:
            if key in self._acks:
                del self._acks[key]
                self._save_locked()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            ack = self._acks.get(key)
            return dict(ack) if ack else None

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._acks.items()}
