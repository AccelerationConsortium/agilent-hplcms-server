"""Read-only agent guide shipped with the installed API package."""

from .models import PROTOCOL_VERSION


def agent_guide() -> dict:
    return {
        "title": "Agilent UPLC-MS agent integration guide",
        "protocol_version": PROTOCOL_VERSION,
        "reference": {"openapi": "/openapi.json", "swagger": "/docs", "status": "/status"},
        "execution_policy": (
            "Laboratory agents execute human-approved, validated plans through the lab-skills SDK. "
            "The HTTP contract below is for SDK integration; never bypass claims or interlocks. "
            "Use your own registered identity. Service-mode changes and fault acknowledgments are operator actions."
        ),
        "workflow": [
            "Read GET /status: equipment_status, activity, required_actions and allowed_actions. A 200 response does not mean ready.",
            "The SDK acquires POST /control/claim with owner, session_id and ttl_s; send X-Claim-Token for control calls. Heartbeat at the returned heartbeat_interval_s and release when finished.",
            "For an approved campaign, workflow.start requires the hte role and the same live claim throughout. End the workflow before releasing the claim.",
            "Submit via POST /control/queue for queue_id, or POST /control/run for run_id. Track jobs through GET /control/queue.",
            "Do not blindly resubmit after a network timeout: submission is not idempotent. Inspect the queue and resolve ambiguous acceptance first.",
        ],
        "sample_addressing": {
            "field": "samples[].sample_position",
            "format": "D[1-4][FB]-<well>", "example": "D4B-A1",
            "note": "Use one position string, not tray/well fields. Injection volume is in microlitres; gradient times in minutes, flow in mL/min, fraction_b in 0..1.",
            "labware": "Configured drawer geometry is authoritative. Unknown drawer, plate-name mismatch, off-plate well or plate height above known drawer clearance returns 422 plate_mismatch. Unknown height/clearance remains permissive. Do not change plate declarations or submitter identity to evade a refusal.",
        },
        "dispatch": {
            "sidecar": "Default FIFO: pending -> running -> done/failed. Technician acquisitions hold dispatch but accept queued submissions. A done job can carry error_message when samples completed but the standby park failed; inspect it before deciding to rerun.",
            "openlab": "Opt-in handoff: dispatching -> handed_off/failed. handed_off means submission only, NOT acquisition completion. OpenLab owns subsequent completion and cancellation. Omit script_name; the deployment chooses the submit script. One handoff subprocess at a time; 412 dispatch_in_progress includes Retry-After.",
            "methods": "OpenLab dispatch uses immutable method snapshots; exact matches reuse a method and near matches may coalesce within configured script tolerances. Substitutions are logged. Display names are not method identity.",
        },
        "refusals": {
            "shape": "HTTP errors use {detail: {error, detail, ...}}; request-validation 422 uses {detail: [...]}.",
            "403": "Unknown roster owner or role forbidden: resolve authorization; never impersonate a service account.",
            "409": "requires_init, instrument_servicing or subsystem_fault: report and resolve the cause. Explicit servicing refuses submissions; either servicing source refuses standby and workflow.start.",
            "412": "queue_full or dispatch_in_progress: observe Retry-After. reserved_for_robot: use the approved physical drawer and submitter identity.",
            "422": "Invalid request or plate_mismatch: inspect the detail and actual configured labware; escalate physical discrepancies.",
            "423": "Missing/expired claim or workflow_active: respect the holder and claim lifetime.",
        },
        "cancellation": "DELETE /control/queue/{queue_id} cancels a pending job. POST /control/abort aborts the active sidecar run AND clears pending jobs. Neither cancels an acquisition already handed off to OpenLab.",
        "capabilities": "OpenAPI is the installed HTTP contract. allowed_actions reflects current state, not roster authorization or a guarantee a later request will succeed. No HTTP labware upload endpoint is provided; container conversion is an offline deployment tool.",
    }
