"""Read-only probe for OpenLab CDS instrument state via OLSS REST API.

Calls the OpenLab Sharing Services (OLSS) REST API running at
http://localhost:6625/olss to get the live instrument state.

The OLSS API authenticates with a short-lived ticket (bearer token).
Tokens are cached for TOKEN_TTL_S seconds to avoid hammering the login
endpoint on every status poll.

Returned fields
---------------
olss_instrument_state : str | None
    OpenLab CDS instrument state as reported by the AcquisitionServer.
    Typical values: "Idle", "Error", "NotReady", "NotConnected", "Busy".
    None if the probe failed.
olss_software_status : str | None
    Software-layer health of the CDS instrument.  Typical values: "OK", "Error".
    None if the probe failed.
olss_current_run : str | None
    Display title of the currently active run, or "no active run".
    None if the probe failed.
olss_error : str | None
    Error message if the probe itself failed; None on success.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Token cached at module level; shared across probe calls from any thread.
_lock = threading.Lock()
_token: str | None = None
_token_expires_at: float = 0.0

# How long a token is considered fresh.  OLSS tickets expire after ~15 min;
# we refresh after 14 min to stay ahead of expiry.
TOKEN_TTL_S: int = 840

# How long to wait for any single OLSS HTTP call.
HTTP_TIMEOUT_S: int = 5


def _login(olss_url: str, username: str) -> str | None:
    """POST /v1.0/login and return the bearer token string, or None on failure."""
    body = json.dumps(
        {
            "username": username,
            "password": "",
            "userApplication": "hplcms-sidecar",
        }
    ).encode()
    try:
        req = urllib.request.Request(
            f"{olss_url}/v1.0/login",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return r.read().decode().strip().strip('"')
    except Exception as exc:
        logger.debug("OLSS login failed: %s", exc)
        return None


def _get_token(olss_url: str, username: str) -> str | None:
    """Return a valid (possibly cached) bearer token, refreshing if needed."""
    global _token, _token_expires_at
    with _lock:
        if _token and time.monotonic() < _token_expires_at:
            return _token
        tok = _login(olss_url, username)
        if tok:
            _token = tok
            _token_expires_at = time.monotonic() + TOKEN_TTL_S
        else:
            _token = None
        return _token


def _invalidate_token() -> None:
    global _token, _token_expires_at
    with _lock:
        _token = None
        _token_expires_at = 0.0


def read_instrument_state(
    olss_url: str,
    username: str,
    instrument_id: int | None = None,
) -> dict[str, Any]:
    """Return live instrument state from the OLSS REST API.

    Parameters
    ----------
    olss_url:
        Base URL of the OLSS service, e.g. ``http://localhost:6625/olss``.
    username:
        OpenLab username.  An empty password is used (matches the lab's
        no-authentication configuration).
    instrument_id:
        Optional numeric instrument ID.  When multiple instruments are
        registered, this selects the right one.  Defaults to the first.

    Returns
    -------
    dict with keys:
      olss_instrument_state, olss_software_status, olss_current_run, olss_error
    """
    token = _get_token(olss_url, username)
    if not token:
        return {
            "olss_instrument_state": None,
            "olss_software_status": None,
            "olss_current_run": None,
            "olss_error": "OLSS login failed",
        }

    try:
        req = urllib.request.Request(
            f"{olss_url}/v2.0/instruments?select=state,softwareStatus,runQueue",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            _invalidate_token()
        logger.debug("OLSS instruments request failed: HTTP %s", exc.code)
        return {
            "olss_instrument_state": None,
            "olss_software_status": None,
            "olss_current_run": None,
            "olss_error": f"OLSS HTTP {exc.code}",
        }
    except Exception as exc:
        logger.debug("OLSS instruments request failed: %s", exc)
        return {
            "olss_instrument_state": None,
            "olss_software_status": None,
            "olss_current_run": None,
            "olss_error": str(exc),
        }

    instruments: list[dict] = data.get("instruments", [])
    if not instruments:
        return {
            "olss_instrument_state": None,
            "olss_software_status": None,
            "olss_current_run": None,
            "olss_error": "No instruments returned by OLSS",
        }

    instr = instruments[0]
    if instrument_id is not None and len(instruments) > 1:
        instr = next(
            (i for i in instruments if i.get("id") == instrument_id), instruments[0]
        )

    run_queue: dict = instr.get("runQueue") or {}
    current_run: dict = run_queue.get("currentRun") or {}

    return {
        "olss_instrument_state": instr.get("state"),
        "olss_software_status": instr.get("softwareStatus"),
        "olss_current_run": current_run.get("title"),
        "olss_error": None,
    }
