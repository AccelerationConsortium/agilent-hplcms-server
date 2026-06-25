"""Roster-driven role resolution for claim ownership (no authentication).

Identity, not credentials. The sidecar is a device, not an identity provider:
the actual access boundary is the Tailscale ACL (see README) and, where a real
login is wanted, the dashboard/aggregator. This module only answers "given an
``owner`` string, which lab role is it?" so the device can attribute a claim and
gate role-restricted actions.

Roles are named by the capability they unlock (the same vocabulary the central
auth service projects). Three groups → roles, configured as comma-separated user
lists in :class:`Settings`:

* ``user``        (``HPLCMS_USERS``)  — submit single-sample runs into the queue.
* ``automation``  (``HTE_USERS``)     — submit **plus** start/end an
  equipment-blocking workflow (``workflow.start`` / ``workflow.end``); held by an
  automation/orchestration principal (e.g. the HTE platform).
* ``service``     (``HPLCMS_ADMINS``) — submit **plus** toggle service mode
  (``service.start`` / ``service.end``). Seeded today with the single
  ``Service-Account`` the dashboard claims under; generalizes to a service group
  later just by adding names.

Capabilities are gated per role (see :func:`can_*`); ``run.submit`` is open to
every recognized role. An owner is expected to be in exactly one group; if it
appears in several, the higher-privilege role wins (service > automation > user).

The roster is ALWAYS enforced. When every list is empty the built-in defaults
apply (so a fresh install always has a Service-Account and never bricks). A
literal ``"*"`` in a list matches any owner — an explicit open mode for dev, not
the same as an accidental empty config.
"""

from __future__ import annotations

from typing import Literal

from ..config import Settings

Role = Literal["user", "automation", "service"]

# Built-in fallback roster used only when every group list is empty/unset.
_DEFAULT_HPLCMS_USERS = {"hplcms-user"}
_DEFAULT_HTE_USERS = {"hte-user"}
_DEFAULT_HPLCMS_ADMINS = {"service-account"}


def _parse_users(raw: str) -> set[str]:
    """Comma-separated user list → lowercased, trimmed set (case-insensitive)."""
    return {u.strip().casefold() for u in raw.split(",") if u.strip()}


def _groups(settings: Settings) -> tuple[set[str], set[str], set[str]]:
    """Resolved (user, automation, service) member sets, applying the
    built-in defaults when nothing at all is configured."""
    users = _parse_users(settings.hplcms_users)
    hte = _parse_users(settings.hte_users)
    admins = _parse_users(settings.hplcms_admins)
    if not users and not hte and not admins:
        return set(_DEFAULT_HPLCMS_USERS), set(_DEFAULT_HTE_USERS), set(_DEFAULT_HPLCMS_ADMINS)
    return users, hte, admins


def _in(owner_key: str, members: set[str]) -> bool:
    return "*" in members or owner_key in members


def resolve_role(owner: str, settings: Settings) -> Role | None:
    """Map an ``owner`` to its lab role, or ``None`` if not on any list.

    Higher privilege wins when an owner is in multiple groups
    (service > automation > user).
    """
    users, automation, service = _groups(settings)
    key = owner.strip().casefold()
    if _in(key, service):
        return "service"
    if _in(key, automation):
        return "automation"
    if _in(key, users):
        return "user"
    return None


def can_submit(role: Role | None) -> bool:
    """Any recognized role may submit single-sample runs."""
    return role is not None


def can_workflow(role: Role | None) -> bool:
    """Only an automation principal may take the equipment-blocking workflow lock."""
    return role == "automation"


def can_service(role: Role | None) -> bool:
    """Only a service role may toggle service mode (today: the Service-Account)."""
    return role == "service"


__all__ = [
    "Role",
    "resolve_role",
    "can_submit",
    "can_workflow",
    "can_service",
]
