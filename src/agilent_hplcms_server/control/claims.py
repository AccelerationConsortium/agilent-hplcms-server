"""In-memory single-slot cooperative claim holder (STATUS_SPEC v1.1 §5).

A v1.1 device that hard-enforces claims requires a valid ``X-Claim-Token`` on
mutating ``/control/*`` calls. This module owns the one claim slot: who holds
it, until when, and the token that proves it. The instrument has a single
acquisition pipeline, so a single slot is the correct model — there is nothing
to claim per-channel.

Claims are cooperative and *not* authenticated: the token is an opaque random
string, not a credential. It exists so two clients cannot drive the instrument
at once, not to keep an attacker out (Tailscale ACLs do that — see README).

Thread-safety: every public method takes ``self._lock``. The holder is shared
across the FastAPI worker threads via ``app.state.claims``.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..models import ClaimedBy


# TTL clamping. The aggregator asks for 30 s (control.py:_CLAIM_TTL_SECONDS);
# we clamp to keep a wedged client from holding the instrument forever, and to
# reject absurdly short TTLs that would expire before the first heartbeat.
_MIN_TTL_S = 5.0
_MAX_TTL_S = 300.0
_DEFAULT_TTL_S = 30.0

# Callers must heartbeat strictly more often than the TTL. We advise half the
# granted TTL so a single dropped heartbeat does not lose the claim.
_HEARTBEAT_FRACTION = 0.5


@dataclass(frozen=True)
class ClaimGrant:
    """Returned on a successful claim/heartbeat."""

    claim_token: str
    heartbeat_interval_s: float
    expires_at: datetime
    session_id: str
    owner: str


class ClaimConflict(Exception):
    """Raised when a claim is requested but a *different* session holds it."""

    def __init__(self, held_by: ClaimedBy) -> None:
        super().__init__("Instrument is claimed by another session.")
        self.held_by = held_by


class ClaimHolder:
    """Owns the single claim slot with TTL + owner + session_id."""

    def __init__(
        self,
        *,
        default_ttl_s: float = _DEFAULT_TTL_S,
        min_ttl_s: float = _MIN_TTL_S,
        max_ttl_s: float = _MAX_TTL_S,
    ) -> None:
        self._lock = threading.Lock()
        self._default_ttl_s = default_ttl_s
        self._min_ttl_s = min_ttl_s
        self._max_ttl_s = max_ttl_s

        self._token: str | None = None
        self._owner: str | None = None
        self._session_id: str | None = None
        self._expires_at: datetime | None = None

    # ------------------------------------------------------------------
    # Internal helpers (call WITH the lock held)
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _is_active_locked(self, now: datetime) -> bool:
        return self._token is not None and self._expires_at is not None and now < self._expires_at

    def _clear_locked(self) -> None:
        self._token = None
        self._owner = None
        self._session_id = None
        self._expires_at = None

    def _clamp_ttl(self, ttl_s: float) -> float:
        return max(self._min_ttl_s, min(self._max_ttl_s, ttl_s))

    def _grant_locked(self, now: datetime, ttl_s: float) -> ClaimGrant:
        expires_at = now + timedelta(seconds=ttl_s)
        self._expires_at = expires_at
        assert self._token is not None and self._session_id is not None and self._owner is not None
        return ClaimGrant(
            claim_token=self._token,
            heartbeat_interval_s=ttl_s * _HEARTBEAT_FRACTION,
            expires_at=expires_at,
            session_id=self._session_id,
            owner=self._owner,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def claim(self, owner: str, session_id: str, ttl_s: float | None = None) -> ClaimGrant:
        """Acquire (or re-acquire) the claim.

        - Free slot (or expired claim) → grant a fresh token.
        - Same ``session_id`` re-claiming → idempotent; rotate to a fresh token
          and extend the TTL (the SDK handles token rotation).
        - Held by a *different* live session → raise :class:`ClaimConflict`.
        """
        ttl = self._clamp_ttl(self._default_ttl_s if ttl_s is None else ttl_s)
        now = self._now()
        with self._lock:
            if self._is_active_locked(now) and self._session_id != session_id:
                raise ClaimConflict(self._claimed_by_locked())
            # Free, expired, or same-session: (re)issue.
            self._token = uuid.uuid4().hex
            self._owner = owner
            self._session_id = session_id
            return self._grant_locked(now, ttl)

    def heartbeat(self, token: str) -> ClaimGrant:
        """Extend the claim TTL. Raises :class:`KeyError` if the token is not
        the current live claim (unknown / expired / different session)."""
        now = self._now()
        with self._lock:
            if not self._is_active_locked(now) or token != self._token:
                # Expired-but-not-cleared claims are tidied here too.
                if not self._is_active_locked(now):
                    self._clear_locked()
                raise KeyError(token)
            return self._grant_locked(now, self._clamp_ttl(self._default_ttl_s))

    def release(self, token: str | None) -> None:
        """Release the claim. Idempotent: releasing an unknown / already-released
        / expired token is a no-op (never raises) per §5."""
        with self._lock:
            if token is not None and token == self._token:
                self._clear_locked()

    def validate(self, token: str | None) -> bool:
        """Return True iff ``token`` is the current live claim. Used by the
        ``/control/*`` enforcement guard."""
        now = self._now()
        with self._lock:
            if not self._is_active_locked(now):
                self._clear_locked()
                return False
            return token is not None and token == self._token

    def current(self) -> ClaimedBy | None:
        """Snapshot of the active claim for ``details.claimed_by`` (``None``
        when unclaimed / expired)."""
        now = self._now()
        with self._lock:
            if not self._is_active_locked(now):
                return None
            return self._claimed_by_locked()

    def _claimed_by_locked(self) -> ClaimedBy:
        assert (
            self._session_id is not None
            and self._owner is not None
            and self._expires_at is not None
        )
        return ClaimedBy(
            session_id=self._session_id,
            owner=self._owner,
            expires_at=self._expires_at,
        )


__all__ = ["ClaimGrant", "ClaimConflict", "ClaimHolder"]
