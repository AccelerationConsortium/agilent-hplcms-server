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
* ``workflow.start``     → ``POST /control/workflow/start``
* ``workflow.end``       → ``POST /control/workflow/end``

The *enqueue* actions (``run.submit``, ``instrument.standby``, ``workflow.start``)
refuse when the queue is full (412), OpenLab is not up (409 requires_init), a
technician is driving OpenLab directly (409 instrument_servicing), or an LC
module reports a hardware fault (409 subsystem_fault). ``run.abort`` and
``queue.cancel`` carry no device precondition — they only ever shrink the queue /
kill the active process — so they are always offered while the service itself is
operational.

``allowed_actions`` is identity-agnostic by design (§6.2): it reflects the
device's *state* preconditions, not who is calling. Just as it lists
``run.submit`` even though a tokenless caller would get 423, it does not drop
``run.submit`` merely because a workflow holds the claim (that is a per-caller
423 the holder does not hit). ``workflow_active`` only toggles the two workflow
verbs: you cannot start a second workflow while one runs, and ``workflow.end`` is
offered exactly while one is active.
"""

from __future__ import annotations

ACTION_RUN_SUBMIT = "run.submit"
ACTION_RUN_ABORT = "run.abort"
ACTION_QUEUE_CANCEL = "queue.cancel"
ACTION_INSTRUMENT_STANDBY = "instrument.standby"
ACTION_WORKFLOW_START = "workflow.start"
ACTION_WORKFLOW_END = "workflow.end"

# Actions whose POST enqueues a Moses job (or, for workflow.start, takes the
# equipment-blocking lock); gated by requires_init (409), servicing (409), a
# subsystem fault (409), and a full queue (412).
ENQUEUE_ACTIONS = (ACTION_RUN_SUBMIT, ACTION_INSTRUMENT_STANDBY)


def allowed_actions(
    *,
    service_operational: bool,
    requires_init: bool,
    queue_full: bool,
    servicing: bool = False,
    workflow_active: bool = False,
    subsystem_fault: bool = False,
) -> list[str]:
    """Return the skill names the device would currently honour.

    Parameters mirror exactly the *state* conditions the router checks before it
    refuses a control call (identity/claim gating is intentionally excluded — see
    the module docstring):

    - ``service_operational`` — the sidecar can determine instrument state
      (no ``probe_error``). When False we cannot reason about the device, so
      we offer nothing.
    - ``requires_init`` — OpenLab core processes are not all up; enqueue
      actions return 409.
    - ``queue_full`` — the FIFO queue is at ``queue_max_depth`` with an active
      run; enqueue actions return 412.
    - ``servicing`` — a technician is running samples directly in OpenLab CDS;
      the queue is halted and enqueue actions return 409 instrument_servicing.
    - ``workflow_active`` — a workflow holds the equipment-blocking lock; toggles
      the ``workflow.start`` / ``workflow.end`` verbs (see module docstring).
    - ``subsystem_fault`` — an LC module (pump / DAD / column thermostat /
      multisampler) reports a hardware error; enqueue actions return 409
      subsystem_fault so we never launch a run into faulted hardware.
    """
    if not service_operational:
        return []

    can_enqueue = (
        (not requires_init)
        and (not queue_full)
        and (not servicing)
        and (not subsystem_fault)
    )

    out: list[str] = []
    if can_enqueue:
        out.append(ACTION_RUN_SUBMIT)
    out.append(ACTION_RUN_ABORT)
    out.append(ACTION_QUEUE_CANCEL)
    if can_enqueue:
        out.append(ACTION_INSTRUMENT_STANDBY)
    if can_enqueue and not workflow_active:
        out.append(ACTION_WORKFLOW_START)
    if workflow_active:
        out.append(ACTION_WORKFLOW_END)
    return out


__all__ = [
    "ACTION_RUN_SUBMIT",
    "ACTION_RUN_ABORT",
    "ACTION_QUEUE_CANCEL",
    "ACTION_INSTRUMENT_STANDBY",
    "ACTION_WORKFLOW_START",
    "ACTION_WORKFLOW_END",
    "ENQUEUE_ACTIONS",
    "allowed_actions",
]
