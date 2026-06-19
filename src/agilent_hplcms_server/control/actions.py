"""Single source of truth for ``allowed_actions`` ⇔ control-side refusals.

STATUS_SPEC §6.2: ``<verb> in allowed_actions`` iff a ``POST /control/<verb>``
would NOT refuse right now. This module owns that mapping so the ``/status``
builder and the ``/control/*`` router can never drift — both import from here.

Action names match ``Skill.name`` in the lab skill catalog
(``lab_skills/skill_catalog/hplc.py``):

* ``run.submit``         → ``POST /control/run`` and ``POST /control/queue``
* ``run.abort``          → ``POST /control/abort``
* ``queue.cancel``       → ``DELETE /control/queue/{id}``
* ``instrument.standby`` → ``POST /control/standby``

The two *enqueue* actions (``run.submit``, ``instrument.standby``) refuse when
the queue is full (412) or OpenLab is not up (409 requires_init). ``run.abort``
and ``queue.cancel`` carry no device precondition — they only ever shrink the
queue / kill the active process — so they are always offered while the service
itself is operational.
"""

from __future__ import annotations

ACTION_RUN_SUBMIT = "run.submit"
ACTION_RUN_ABORT = "run.abort"
ACTION_QUEUE_CANCEL = "queue.cancel"
ACTION_INSTRUMENT_STANDBY = "instrument.standby"

# Actions whose POST enqueues a Moses job; gated by requires_init (409) and a
# full queue (412).
ENQUEUE_ACTIONS = (ACTION_RUN_SUBMIT, ACTION_INSTRUMENT_STANDBY)


def allowed_actions(
    *,
    service_operational: bool,
    requires_init: bool,
    queue_full: bool,
) -> list[str]:
    """Return the skill names the device would currently honour.

    Parameters mirror exactly the conditions the router checks before it
    refuses a control call:

    - ``service_operational`` — the sidecar can determine instrument state
      (no ``probe_error``). When False we cannot reason about the device, so
      we offer nothing.
    - ``requires_init`` — OpenLab core processes are not all up; enqueue
      actions return 409.
    - ``queue_full`` — the FIFO queue is at ``queue_max_depth`` with an active
      run; enqueue actions return 412.
    """
    if not service_operational:
        return []

    can_enqueue = (not requires_init) and (not queue_full)

    out: list[str] = []
    if can_enqueue:
        out.append(ACTION_RUN_SUBMIT)
    out.append(ACTION_RUN_ABORT)
    out.append(ACTION_QUEUE_CANCEL)
    if can_enqueue:
        out.append(ACTION_INSTRUMENT_STANDBY)
    return out


__all__ = [
    "ACTION_RUN_SUBMIT",
    "ACTION_RUN_ABORT",
    "ACTION_QUEUE_CANCEL",
    "ACTION_INSTRUMENT_STANDBY",
    "ENQUEUE_ACTIONS",
    "allowed_actions",
]
