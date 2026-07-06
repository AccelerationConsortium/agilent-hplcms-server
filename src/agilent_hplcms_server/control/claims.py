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
    role: str | None = None
    workflow: bool = False


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
        self._role: str | None = None
        # True while the holder has taken the equipment-blocking workflow lock.
        # Lives on the claim so it inherits TTL/heartbeat/auto-expiry for free —
        # a crashed workflow holder loses the lock when its claim lapses.
        self._workflow: bool = False

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
        self._role = None
        self._workflow = False

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
            role=self._role,
            workflow=self._workflow,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def claim(
        self,
        owner: str,
        session_id: str,
        ttl_s: float | None = None,
        role: str | None = None,
    ) -> ClaimGrant:
        """Acquire (or re-acquire) the claim.

        - Free slot (or expired claim) → grant a fresh token; the workflow lock
          starts cleared.
        - Same ``session_id`` re-claiming → idempotent; rotate to a fresh token
          and extend the TTL (the SDK handles token rotation). An active workflow
          lock is *preserved* across the re-claim so a heartbeating workflow
          holder keeps its lock.
        - Held by a *different* live session → raise :class:`ClaimConflict`.

        ``role`` is the owner's resolved lab role (see ``control/roster.py``);
        it is attribution only, recorded so ``workflow.start`` can be gated.
        """
        ttl = self._clamp_ttl(self._default_ttl_s if ttl_s is None else ttl_s)
        now = self._now()
        with self._lock:
            same_session = self._is_active_locked(now) and self._session_id == session_id
            if self._is_active_locked(now) and self._session_id != session_id:
                raise ClaimConflict(self._claimed_by_locked())
            # Free, expired, or same-session: (re)issue.
            self._token = uuid.uuid4().hex
            self._owner = owner
            self._session_id = session_id
            self._role = role
            if not same_session:
                # Fresh claim (new or expired slot): workflow lock starts clear.
                self._workflow = False
            return self._grant_locked(now, ttl)

    def start_workflow(self, token: str) -> ClaimGrant:
        """Take the equipment-blocking workflow lock. Raises :class:`KeyError`
        if ``token`` is not the current live claim (caller must hold the claim).
        Idempotent: re-starting while already held just returns the grant."""
        now = self._now()
        with self._lock:
            if not self._is_active_locked(now) or token != self._token:
                if not self._is_active_locked(now):
                    self._clear_locked()
                raise KeyError(token)
            self._workflow = True
            return self._grant_locked(now, self._clamp_ttl(self._default_ttl_s))

    def end_workflow(self, token: str) -> None:
        """Release the workflow lock while keeping the claim. Idempotent: a no-op
        if the token is not the live holder or no workflow is active (never
        raises) — symmetry with :meth:`release`."""
        now = self._now()
        with self._lock:
            if self._is_active_locked(now) and token == self._token:
                self._workflow = False

    def is_workflow_active(self) -> bool:
        """True iff a live claim currently holds the workflow lock."""
        now = self._now()
        with self._lock:
            return self._is_active_locked(now) and self._workflow

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
            role=self._role,
            workflow=self._workflow,
        )


__all__ = ["ClaimGrant", "ClaimConflict", "ClaimHolder"]
