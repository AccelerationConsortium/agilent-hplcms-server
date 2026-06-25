"""Unit tests for the central roster projection pull (control/roster_sync.py)."""

from __future__ import annotations

from agilent_hplcms_server.config import Settings
from agilent_hplcms_server.control.roster_sync import RosterProvider, parse_entries


def _settings(**kw) -> Settings:
    return Settings(**kw)


# ---- parse_entries --------------------------------------------------------


def test_parse_entries_maps_and_skips_unknown():
    payload = {
        "equipment_key": "agilent_uplc_ms",
        "entries": [
            {"owner": "Alice@x.com", "role": "automation"},        # casefolded
            {"owner": "bob@x.com", "role": "user"},
            {"owner": "ghost", "role": "wizard"},           # unknown role → skipped
            {"role": "automation"},                                # missing owner → skipped
        ],
    }
    assert parse_entries(payload) == {"alice@x.com": "automation", "bob@x.com": "user"}


def test_parse_entries_handles_empty_and_missing():
    assert parse_entries({}) == {}
    assert parse_entries({"entries": None}) == {}


# ---- resolution + fallback ------------------------------------------------


def test_static_fallback_when_no_central():
    # No roster_url and never pulled → resolve uses the static env roster.
    p = RosterProvider()
    s = _settings(hplcms_users="alice", hte_users="", hplcms_admins="")
    assert p.resolve("alice", s) == "user"
    assert p.resolve("ALICE", s) == "user"  # case-insensitive (static path)
    assert p.resolve("stranger", s) is None
    assert p.has_central_roster() is False


def test_central_overrides_static_after_refresh():
    payload = {"entries": [{"owner": "alice@x.com", "role": "service"}]}
    p = RosterProvider(fetcher=lambda u, t, k: payload)
    s = _settings(roster_url="http://auth/roster", hte_users="*")  # static would allow anyone
    assert p.refresh(s) is True
    assert p.has_central_roster() is True
    assert p.resolve("alice@x.com", s) == "service"
    assert p.resolve("Alice@x.com", s) == "service"  # case-insensitive
    assert p.resolve("stranger", s) is None  # central authoritative, not on the list


def test_empty_central_roster_is_authoritative():
    # A successful pull returning nobody means nobody is allowed — even though the
    # static list ("*") would otherwise allow everyone.
    p = RosterProvider(fetcher=lambda u, t, k: {"entries": []})
    s = _settings(roster_url="http://auth/roster", hte_users="*")
    assert p.refresh(s) is True
    assert p.resolve("anyone", s) is None


def test_refresh_failure_keeps_last_good():
    calls = {"n": 0}

    def flaky(url, timeout, api_key):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"entries": [{"owner": "alice@x.com", "role": "automation"}]}
        raise OSError("central down")

    p = RosterProvider(fetcher=flaky)
    s = _settings(roster_url="http://auth/roster")
    assert p.refresh(s) is True
    assert p.resolve("alice@x.com", s) == "automation"
    assert p.refresh(s) is False  # second pull fails...
    assert p.resolve("alice@x.com", s) == "automation"  # ...last-good retained
    assert p.has_central_roster() is True


def test_refresh_noop_without_url():
    p = RosterProvider(fetcher=lambda u, t, k: {"entries": []})
    assert p.refresh(_settings()) is False  # roster_url empty → no-op, no fetch
    assert p.has_central_roster() is False


def test_start_poller_noop_without_url():
    # No URL → poller does not start a thread (device runs standalone).
    p = RosterProvider(fetcher=lambda u, t, k: {"entries": []})
    p.start_poller(_settings())
    assert p._poller_thread is None
    p.stop_poller()
