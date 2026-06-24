"""Central roster projection pull (optional) for owner→role resolution.

The device's authoritative source for owner→role is the central auth service
(ac-organic-lab ``GET /equipment/{key}/roster``). When :attr:`Settings.roster_url`
is configured, this module polls that endpoint on an interval and resolves claim
owners against the pulled projection — central is then **authoritative**,
including a deliberately empty roster (central saying "no one is allowed").

The static env roster (:mod:`control.roster`) is the fallback used **only until
the first successful pull**, so the device never bricks if central is unreachable
at startup. Once a roster has been pulled, a later refresh failure keeps the
last-good copy rather than falling back.

Design mirrors the rest of the sidecar: stdlib ``urllib`` (no new runtime deps,
like :mod:`probes.openlab_rest`), a daemon-thread poller (like
:class:`control.runner.MosesRunner`), and a thread-safe last-good cache. When
``roster_url`` is empty the provider is a thin pass-through to the static env
roster, so the device runs fully standalone with no auth service.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Callable, Optional

from ..config import Settings
from .roster import Role, resolve_role

logger = logging.getLogger(__name__)

_VALID_ROLES = frozenset({"hplcms_user", "hte", "hplcms_admin"})

# (url, timeout_s, api_key) -> parsed JSON payload. Injectable for tests.
RosterFetcher = Callable[[str, float, str], dict]


def fetch_roster(url: str, timeout_s: float = 5.0, api_key: str = "") -> dict:
    """GET the central roster projection and return the parsed JSON payload.

    Raises on any transport / HTTP / decode error so the caller can keep the
    last-good roster.
    """
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return json.loads(r.read())


def parse_entries(payload: dict) -> dict[str, Role]:
    """Map a roster payload to ``{owner_casefold: role}``.

    Entries missing an owner or carrying a role this device doesn't recognize
    are skipped (defensive against a newer/forward central schema), never raised.
    """
    out: dict[str, Role] = {}
    for entry in payload.get("entries", []) or []:
        owner = entry.get("owner")
        role = entry.get("role")
        if not owner or role not in _VALID_ROLES:
            continue
        out[owner.strip().casefold()] = role  # type: ignore[assignment]
    return out


class RosterProvider:
    """Owner→role resolver backed by the central roster projection, with a
    static-env fallback. Thread-safe; an optional daemon poller refreshes it."""

    def __init__(self, fetcher: Optional[RosterFetcher] = None) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, Role] | None = None
        self._last_ok_monotonic: float | None = None
        self._last_error: str | None = None
        self._fetch = fetcher  # None → real urllib fetch_roster
        self._poller_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ---- resolution -------------------------------------------------------

    def resolve(self, owner: str, settings: Settings) -> Role | None:
        """Resolve ``owner`` to a role. Uses the pulled central roster once one
        has been fetched; otherwise falls back to the static env roster."""
        with self._lock:
            entries = self._entries
        if entries is not None:
            return entries.get(owner.strip().casefold())
        return resolve_role(owner, settings)

    def has_central_roster(self) -> bool:
        """True once a roster has been successfully pulled (central authoritative)."""
        with self._lock:
            return self._entries is not None

    # ---- refresh ----------------------------------------------------------

    def refresh(self, settings: Settings) -> bool:
        """Pull the central roster once. Returns True on success; on failure
        keeps the last-good roster and returns False. No-op (False) when
        ``roster_url`` is unset."""
        url = settings.roster_url
        if not url:
            return False
        fetch = self._fetch or fetch_roster
        try:
            payload = fetch(url, float(settings.roster_http_timeout_s), settings.roster_api_key)
            entries = parse_entries(payload)
        except Exception as exc:  # noqa: BLE001 - keep last-good on ANY failure
            with self._lock:
                self._last_error = str(exc)
            logger.warning("roster refresh failed (keeping last-good roster): %s", exc)
            return False
        with self._lock:
            self._entries = entries
            self._last_ok_monotonic = time.monotonic()
            self._last_error = None
        logger.info("roster refreshed from %s (%d entries)", url, len(entries))
        return True

    # ---- background poller ------------------------------------------------

    def start_poller(self, settings: Settings) -> None:
        """Start the daemon refresh loop. No-op when ``roster_url`` is unset
        (the device then uses the static env roster only)."""
        if not settings.roster_url:
            logger.info("ROSTER_URL not set → static env roster only (no central pull)")
            return
        if self._poller_thread is not None and self._poller_thread.is_alive():
            return
        self._stop_event.clear()
        interval = max(1, int(settings.roster_refresh_interval_s))

        def _loop() -> None:
            # Pull once immediately so the central roster goes live ASAP, then
            # on the configured interval until stopped.
            self.refresh(settings)
            while not self._stop_event.wait(timeout=interval):
                try:
                    self.refresh(settings)
                except Exception:  # noqa: BLE001 - refresh already swallows; belt & braces
                    logger.exception("Roster poller error")

        self._poller_thread = threading.Thread(
            target=_loop, daemon=True, name="roster-poller"
        )
        self._poller_thread.start()

    def stop_poller(self) -> None:
        self._stop_event.set()


__all__ = ["RosterProvider", "fetch_roster", "parse_entries", "RosterFetcher"]
