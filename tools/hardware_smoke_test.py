"""Hardware smoke test for the Agilent UPLC-MS sidecar (STATUS_SPEC v1.1).

Drives the live /control/* surface against the REAL instrument in safe, ordered
stages. Read-only stages run by default; the stage that actually injects a
sample and runs a gradient is GATED behind --run-hardware so you cannot trip it
by accident.

Run it against a *staging* instance first (the new build on port 8011, alongside
the existing NSSM service on 8010), with the instrument idle and someone
watching. Only promote the build to the NSSM service after this passes.

Owners must be on the device roster (static env lists or the central
ac-organic-lab roster). Validated owners for this instrument: an automation
owner (e.g. hte-orchestrator@lab.local) for --owner and a service owner
(e.g. yangcyril.cao@utoronto.ca) for --admin-owner.

Usage (PowerShell, on the instrument PC):

    # read-only + claim + refusal checks (no hardware motion):
    uv run python tools/hardware_smoke_test.py --base-url http://localhost:8011 `
        --owner hte-orchestrator@lab.local --admin-owner yangcyril.cao@utoronto.ca

    # include the single real run + standby (use an unreserved drawer):
    uv run python tools/hardware_smoke_test.py --base-url http://localhost:8011 `
        --owner hte-orchestrator@lab.local --admin-owner yangcyril.cao@utoronto.ca `
        --run-hardware --sample-position D4B-A1 --output-dir C:/CDSProjects/Installation/Results/SmokeTest

Exit code 0 = all attempted stages passed; non-zero = a check failed (the claim
is always released on the way out).

Gotcha: if Stage 4 aborts with an OpenLab "Hardware error" (pusher/needle hit
the vessel top at e.g. D4B-A1), the physical labware does not match the plate
type / container geometry configured for that drawer in OpenLab. Raise/seat the
plate correctly (or fix the drawer's container type in OpenLab) and rerun. This
is NOT a sidecar/roster/tray-mapping issue — plate geometry is not part of the
run request.

Stdlib only - no third-party deps, so it runs with any Python on the PC.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

# ----------------------------------------------------------------------------
# Tiny HTTP helper (stdlib only)
# ----------------------------------------------------------------------------

_TOKEN: str | None = None  # current X-Claim-Token, set after /control/claim


def _request(method: str, url: str, body: dict | None = None) -> tuple[int, dict | str]:
    """Return (status_code, parsed_json_or_text). Never raises on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if _TOKEN is not None:
        req.add_header("X-Claim-Token", _TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        return 0, f"connection failed: {exc.reason}"


# ----------------------------------------------------------------------------
# Pass/fail bookkeeping
# ----------------------------------------------------------------------------

_FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail:
        line += f"  - {detail}"
    print(line)
    if not ok:
        _FAILURES.append(label)
    return ok


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


# ----------------------------------------------------------------------------
# Stages
# ----------------------------------------------------------------------------

def stage_readonly(base: str) -> bool:
    """Read-only status checks. Returns True iff the instrument is idle (no active
    run and an empty queue) - the caller uses this to gate the abort stage."""
    _section("Stage 1 - read-only status (no hardware motion)")
    idle = False

    code, body = _request("GET", f"{base}/health")
    _check("GET /health is 200", code == 200, str(body))

    code, body = _request("GET", f"{base}/status")
    if not _check("GET /status is 200", code == 200, str(body)[:200]):
        return False
    assert isinstance(body, dict)
    print(f"      protocol_version : {body.get('protocol_version')}")
    print(f"      equipment_status : {body.get('equipment_status')}")
    print(f"      allowed_actions  : {body.get('allowed_actions')}")
    details = body.get("details") or {}
    print(f"      claimed_by       : {details.get('claimed_by')}")
    print(f"      olss_sw_status   : {details.get('olss_software_status')}")
    _check("protocol_version is 1.1", body.get("protocol_version") == "1.1")
    _check("allowed_actions present", isinstance(body.get("allowed_actions"), list))
    _check(
        "claimed_by is null before any claim",
        details.get("claimed_by") is None,
        "instrument may already be claimed by another session",
    )

    code, body = _request("GET", f"{base}/control/queue")
    if _check("GET /control/queue is 200", code == 200, str(body)[:200]):
        assert isinstance(body, dict)
        print(f"      instrument_online: {body.get('instrument_online')}")
        print(f"      accepting_jobs   : {body.get('accepting_jobs')}")
        print(f"      active_run_id    : {body.get('active_run_id')}")
        print(f"      pending_count    : {body.get('pending_count')}")
        idle = body.get("active_run_id") is None and not body.get("pending_count")
        _check(
            "no active run before we start (instrument idle)",
            idle,
            "something is running/queued - the abort stage will be skipped",
        )
    return idle


def stage_claim(base: str, owner: str, session_id: str) -> dict:
    """Acquire the claim and verify hard enforcement. Returns the claim grant."""
    global _TOKEN
    _section("Stage 2 - claim protocol + hard enforcement (423)")

    # Tokenless mutating call MUST be locked out first.
    code, _ = _request("POST", f"{base}/control/abort")
    _check("tokenless POST /control/abort -> 423 Locked", code == 423, f"got {code}")

    code, body = _request(
        "POST", f"{base}/control/claim",
        {"owner": owner, "session_id": session_id, "ttl_s": 30.0},
    )
    if not _check("POST /control/claim -> 200", code == 200, str(body)[:200]):
        return {}
    assert isinstance(body, dict)
    _TOKEN = body["claim_token"]
    print(f"      heartbeat_interval_s: {body.get('heartbeat_interval_s')}")
    print(f"      expires_at          : {body.get('expires_at')}")
    print(f"      role                : {body.get('role')}")

    code, _ = _request("POST", f"{base}/control/heartbeat")
    _check("POST /control/heartbeat (valid token) -> 204", code == 204, f"got {code}")
    return body


def stage_workflow(base: str) -> None:
    """Exercise the equipment-blocking workflow lock (no hardware motion).

    Requires the claim owner to be an HTE platform user (role 'automation'); a
    plain user owner would get 403 role_forbidden here.
    """
    _section("Stage 3b - workflow lock (precedence #2, no hardware)")

    code, body = _request("POST", f"{base}/control/workflow/start")
    started = code == 200 and isinstance(body, dict) and body.get("status") == "workflow_started"
    if not _check("POST /control/workflow/start -> 200 workflow_started", started, str(body)[:200]):
        # If the owner lacks the automation role, skip the rest cleanly.
        return

    code, st = _request("GET", f"{base}/status")
    details = st.get("details", {}) if isinstance(st, dict) else {}
    actions = st.get("allowed_actions", []) if isinstance(st, dict) else []
    _check("/status shows details.workflow_active = true", details.get("workflow_active") is True)
    _check("/status offers workflow.end, not workflow.start",
           "workflow.end" in actions and "workflow.start" not in actions, str(actions))

    code, _ = _request("POST", f"{base}/control/workflow/end")
    _check("POST /control/workflow/end -> 200", code == 200, f"got {code}")
    code, st = _request("GET", f"{base}/status")
    details = st.get("details", {}) if isinstance(st, dict) else {}
    _check("/status workflow_active cleared after end",
           not details.get("workflow_active"))


def _min_run_body() -> dict:
    """A minimal valid run body (unreserved drawer) for refusal checks."""
    return {
        "output_dir": "C:/CDSProjects/Installation/Results/SmokeTest",
        "submitter": "manual", "plate_format": "96-well",
        "gradient": {
            "name": "smoke_iso", "solvent_a": "H2O_0.1%FA", "solvent_b": "ACN_0.1%FA",
            "run_time": 2.0, "flow_rate": 0.3, "equilibration_time": 0.0,
            "gradient_table": [[0.0, 0.05], [2.0, 0.05]],
        },
        "samples": [{"sample_name": "x", "sample_position": "D4B-A1", "injection_volume": 1.0}],
    }


def stage_service(base: str, admin_owner: str, session_id: str) -> None:
    """Service mode (precedence #1, admin-only) — its own claim episode so it
    runs before the main run-owner claim. No hardware motion."""
    global _TOKEN
    _section("Stage 2b - service mode (admin-only, no hardware)")

    code, _ = _request("POST", f"{base}/control/service/start")
    _check("tokenless service/start -> 423 Locked", code == 423, f"got {code}")

    code, body = _request(
        "POST", f"{base}/control/claim",
        {"owner": admin_owner, "session_id": f"{session_id}-svc", "ttl_s": 30.0},
    )
    if not _check(f"claim as admin {admin_owner!r} -> 200", code == 200, str(body)[:200]):
        return
    assert isinstance(body, dict)
    _TOKEN = body["claim_token"]
    if not _check(f"admin claim resolved role 'service' (is {admin_owner!r} a service owner?)",
                  body.get("role") == "service", f"role={body.get('role')}"):
        _request("POST", f"{base}/control/release")
        _TOKEN = None
        return

    code, body = _request("POST", f"{base}/control/service/start")
    on = code == 200 and isinstance(body, dict) and body.get("service_mode") is True
    _check("service/start -> 200 service_mode on", on, str(body)[:200])

    code, st = _request("GET", f"{base}/status")
    details = st.get("details", {}) if isinstance(st, dict) else {}
    _check("/status details.service_mode = true", details.get("service_mode") is True)

    code, body = _request("POST", f"{base}/control/run", _min_run_body())
    err = (body.get("detail") or {}).get("error") if isinstance(body, dict) else None
    _check("submit during service mode -> 409 instrument_servicing",
           code == 409 and err == "instrument_servicing", f"got {code}/{err}")

    code, body = _request("POST", f"{base}/control/service/end")
    off = code == 200 and isinstance(body, dict) and body.get("service_mode") is False
    _check("service/end -> 200 service_mode off", off, str(body)[:200])

    _request("POST", f"{base}/control/release")
    _TOKEN = None


def stage_refusals(base: str, reserved_drawer: str) -> None:
    """Submit bodies that MUST be refused *before* any hardware action."""
    _section("Stage 3 - precondition refusals (refused before hardware)")

    base_grad = {
        "name": "smoke_iso", "solvent_a": "H2O_0.1%FA", "solvent_b": "ACN_0.1%FA",
        "run_time": 2.0, "flow_rate": 0.3, "equilibration_time": 0.0,
        "gradient_table": [[0.0, 0.05], [2.0, 0.05]],
    }

    # Off-plate well -> 422 (geometry). A13 is off a 96-well plate (12 cols).
    off_plate = {
        "output_dir": "C:/CDSProjects/Installation/Results/SmokeTest",
        "gradient": base_grad, "plate_format": "96-well",
        "samples": [{"sample_name": "x", "sample_position": "D4B-A13", "injection_volume": 1.0}],
    }
    code, _ = _request("POST", f"{base}/control/run", off_plate)
    _check("off-plate well A13 on 96-well -> 422", code == 422, f"got {code}")

    # Manual run targeting the robot-reserved drawer -> 412 reserved_for_robot.
    reserved = {
        "output_dir": "C:/CDSProjects/Installation/Results/SmokeTest",
        "gradient": base_grad, "submitter": "manual",
        "samples": [{"sample_name": "x", "sample_position": f"{reserved_drawer}-A1", "injection_volume": 1.0}],
    }
    code, body = _request("POST", f"{base}/control/run", reserved)
    detail = body.get("detail") if isinstance(body, dict) else {}
    err = detail.get("error") if isinstance(detail, dict) else None
    _check(
        f"manual run on reserved drawer {reserved_drawer!r} -> 412 reserved_for_robot",
        code == 412 and err == "reserved_for_robot",
        f"got {code} / {err}",
    )


def _heartbeat(base: str) -> None:
    code, _ = _request("POST", f"{base}/control/heartbeat")
    if code != 204:
        print(f"      WARNING: heartbeat returned {code} (claim may be lost)")


def stage_hardware_run(base: str, sample_position: str, output_dir: str, hb_interval: float) -> None:
    """GATED: submit ONE real run, watch it through, then standby."""
    _section("Stage 4 - ONE real run + standby (HARDWARE MOVES)")
    print(f"      sample_position={sample_position} output_dir={output_dir}")

    run_body = {
        "output_dir": output_dir,
        "ms_mode": "positive_negative",
        "standby_after": False,  # we exercise /control/standby explicitly below
        "plate_format": "96-well",
        "submitter": "manual",
        "gradient": {
            "name": "smoke_iso", "solvent_a": "H2O_0.1%FA", "solvent_b": "ACN_0.1%FA",
            "run_time": 2.0, "flow_rate": 0.3, "equilibration_time": 0.0,
            "gradient_table": [[0.0, 0.05], [2.0, 0.05]],
        },
        "samples": [{"sample_name": "smoke_blank", "sample_position": sample_position, "injection_volume": 1.0}],
    }
    code, body = _request("POST", f"{base}/control/queue", run_body)
    if not _check("POST /control/queue (real run) -> 202", code == 202, str(body)[:250]):
        return
    assert isinstance(body, dict)
    queue_id = body.get("queue_id")
    print(f"      queue_id = {queue_id}")

    # Watch the job, heartbeating so we never lose the claim mid-run.
    deadline = time.monotonic() + 600  # run_time 2 min + generous OpenLab slack
    last_hb = 0.0
    seen_active = False
    final = None
    while time.monotonic() < deadline:
        if time.monotonic() - last_hb >= max(5.0, hb_interval):
            _heartbeat(base)
            last_hb = time.monotonic()
        code, q = _request("GET", f"{base}/control/queue")
        if code == 200 and isinstance(q, dict):
            entry = next((e for e in q.get("queue", []) if e.get("queue_id") == queue_id), None)
            st = entry.get("status") if entry else "?"
            print(f"      [{time.strftime('%H:%M:%S')}] status={st} "
                  f"active={q.get('active_run_id')} state={q.get('instrument_state')}")
            if st == "running":
                seen_active = True
            if st in ("done", "failed"):
                final = st
                break
        time.sleep(5)

    _check("run reached running (instrument accepted it)", seen_active)
    _check("run finalized as done (not failed)", final == "done", f"final status={final}")

    # Park the instrument.
    code, body = _request("POST", f"{base}/control/standby")
    _check("POST /control/standby -> 202", code == 202, str(body)[:200])


def stage_abort(base: str) -> None:
    _section("Stage 5 - abort clears the queue")
    code, body = _request("POST", f"{base}/control/abort")
    _check("POST /control/abort -> 200", code == 200, str(body)[:200])


def stage_release(base: str) -> None:
    _section("Stage 6 - release claim (idempotent)")
    code, _ = _request("POST", f"{base}/control/release")
    _check("POST /control/release -> 204", code == 204, f"got {code}")
    code, _ = _request("POST", f"{base}/control/release")
    _check("second release also -> 204 (idempotent)", code == 204, f"got {code}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8011",
                   help="Sidecar base URL (use the STAGING port, e.g. :8011)")
    p.add_argument("--owner", default="HTE-User",
                   help="Run-owner claim; must be on the device roster (HPLCMS_USERS / "
                        "HTE_USERS). An 'hte' user is needed for the workflow stage.")
    p.add_argument("--admin-owner", default="Service-Account",
                   help="Admin account for the service-mode stage (must be in HPLCMS_ADMINS).")
    p.add_argument("--session-id", default=f"smoke-{uuid.uuid4().hex[:8]}")
    p.add_argument("--reserved-drawer", default="D1F",
                   help="Must match the device's RESERVED_ROBOT_DRAWER (default D1F = robot drawer)")
    p.add_argument("--run-hardware", action="store_true",
                   help="ENABLE the real run + standby stage (moves hardware)")
    p.add_argument("--sample-position", default="D4B-A1",
                   help="Slot for the real run, 'D#X-Y1' (use an UNreserved drawer), e.g. D4B-A1")
    p.add_argument("--output-dir", default="C:/CDSProjects/Installation/Results/SmokeTest")
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    print(f"Smoke test against {base}  (session_id={args.session_id})")
    if args.run_hardware:
        print("\n*** --run-hardware ENABLED: a real injection + 2-min run will execute. ***")
        print("*** D1F is the confirmed robot drawer; confirm the --sample-position drawer,***")
        print("*** the instrument is idle, and someone is watching it.                     ***")
        if input("Type 'yes' to proceed: ").strip().lower() != "yes":
            print("Aborted by user; running read-only stages only.")
            args.run_hardware = False

    try:
        idle = stage_readonly(base)
        stage_service(base, args.admin_owner, args.session_id)
        grant = stage_claim(base, args.owner, args.session_id)
        if not grant:
            print("\nClaim failed - cannot run mutating stages. Stopping.")
            return 1
        hb_interval = float(grant.get("heartbeat_interval_s", 15.0))
        stage_refusals(base, args.reserved_drawer)
        stage_workflow(base)
        if args.run_hardware:
            stage_hardware_run(base, args.sample_position, args.output_dir, hb_interval)
        else:
            print("\n(Skipping Stage 4 hardware run - pass --run-hardware to enable.)")
        # /control/abort is a no-op when idle but would kill an active run, so only
        # exercise it when we confirmed idle at start (or after our own hardware run).
        if idle or args.run_hardware:
            stage_abort(base)
        else:
            print("\n(Skipping abort stage - instrument was not idle at start.)")
    finally:
        stage_release(base)

    _section("Result")
    if _FAILURES:
        print(f"  {len(_FAILURES)} check(s) FAILED:")
        for f in _FAILURES:
            print(f"    - {f}")
        return 1
    print("  All attempted checks PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
