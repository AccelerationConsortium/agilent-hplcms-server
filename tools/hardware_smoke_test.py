"""Hardware smoke test for the Agilent UPLC-MS sidecar (STATUS_SPEC v1.1).

Drives the live /control/* surface against the REAL instrument in safe, ordered
stages. Read-only stages run by default; the stage that actually injects a
sample and runs a gradient is GATED behind --run-hardware so you cannot trip it
by accident.

Run it against a *staging* instance first (the new build on port 8011, alongside
the existing NSSM service on 8010), with the instrument idle and someone
watching. Only promote the build to the NSSM service after this passes.

Usage (PowerShell, on the instrument PC):

    # read-only + claim + refusal checks (no hardware motion):
    uv run python tools/hardware_smoke_test.py --base-url http://localhost:8011

    # include the single real run + standby (use the manual/rear tray):
    uv run python tools/hardware_smoke_test.py --base-url http://localhost:8011 \
        --run-hardware --tray rear --well A1 --output-dir C:/CDSProjects/Installation/Results/SmokeTest

Exit code 0 = all attempted stages passed; non-zero = a check failed (the claim
is always released on the way out).

Stdlib only — no third-party deps, so it runs with any Python on the PC.
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
        line += f"  — {detail}"
    print(line)
    if not ok:
        _FAILURES.append(label)
    return ok


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


# ----------------------------------------------------------------------------
# Stages
# ----------------------------------------------------------------------------

def stage_readonly(base: str) -> None:
    _section("Stage 1 — read-only status (no hardware motion)")

    code, body = _request("GET", f"{base}/health")
    _check("GET /health is 200", code == 200, str(body))

    code, body = _request("GET", f"{base}/status")
    if not _check("GET /status is 200", code == 200, str(body)[:200]):
        return
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
        _check(
            "no active run before we start (instrument idle)",
            body.get("active_run_id") is None,
            "ABORT the test if something else is running",
        )


def stage_claim(base: str, owner: str, session_id: str) -> dict:
    """Acquire the claim and verify hard enforcement. Returns the claim grant."""
    global _TOKEN
    _section("Stage 2 — claim protocol + hard enforcement (423)")

    # Tokenless mutating call MUST be locked out first.
    code, _ = _request("POST", f"{base}/control/abort")
    _check("tokenless POST /control/abort → 423 Locked", code == 423, f"got {code}")

    code, body = _request(
        "POST", f"{base}/control/claim",
        {"owner": owner, "session_id": session_id, "ttl_s": 30.0},
    )
    if not _check("POST /control/claim → 200", code == 200, str(body)[:200]):
        return {}
    assert isinstance(body, dict)
    _TOKEN = body["claim_token"]
    print(f"      heartbeat_interval_s: {body.get('heartbeat_interval_s')}")
    print(f"      expires_at          : {body.get('expires_at')}")

    code, _ = _request("POST", f"{base}/control/heartbeat")
    _check("POST /control/heartbeat (valid token) → 204", code == 204, f"got {code}")
    return body


def stage_refusals(base: str, reserved_tray: str) -> None:
    """Submit bodies that MUST be refused *before* any hardware action."""
    _section("Stage 3 — precondition refusals (refused before hardware)")

    base_grad = {
        "name": "smoke_iso", "solvent_a": "H2O_0.1%FA", "solvent_b": "ACN_0.1%FA",
        "run_time": 2.0, "flow_rate": 0.3, "equilibration_time": 0.0,
        "gradient_table": [[0.0, 0.05], [2.0, 0.05]],
    }

    # Off-plate well → 422 (geometry).
    off_plate = {
        "output_dir": "C:/CDSProjects/Installation/Results/SmokeTest",
        "gradient": base_grad, "plate_format": "96-well",
        "samples": [{"sample_name": "x", "tray": "front", "well": "A13", "injection_volume": 1.0}],
    }
    code, _ = _request("POST", f"{base}/control/run", off_plate)
    _check("off-plate well A13 on 96-well → 422", code == 422, f"got {code}")

    # Manual run targeting the robot-reserved tray → 412 reserved_for_robot.
    reserved = {
        "output_dir": "C:/CDSProjects/Installation/Results/SmokeTest",
        "gradient": base_grad, "submitter": "manual",
        "samples": [{"sample_name": "x", "tray": reserved_tray, "well": "A1", "injection_volume": 1.0}],
    }
    code, body = _request("POST", f"{base}/control/run", reserved)
    detail = body.get("detail") if isinstance(body, dict) else {}
    err = detail.get("error") if isinstance(detail, dict) else None
    _check(
        f"manual run on reserved tray {reserved_tray!r} → 412 reserved_for_robot",
        code == 412 and err == "reserved_for_robot",
        f"got {code} / {err}",
    )


def _heartbeat(base: str) -> None:
    code, _ = _request("POST", f"{base}/control/heartbeat")
    if code != 204:
        print(f"      WARNING: heartbeat returned {code} (claim may be lost)")


def stage_hardware_run(base: str, tray: str, well: str, output_dir: str, hb_interval: float) -> None:
    """GATED: submit ONE real run, watch it through, then standby."""
    _section("Stage 4 — ONE real run + standby (HARDWARE MOVES)")
    print(f"      tray={tray} well={well} output_dir={output_dir}")

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
        "samples": [{"sample_name": "smoke_blank", "tray": tray, "well": well, "injection_volume": 1.0}],
    }
    code, body = _request("POST", f"{base}/control/queue", run_body)
    if not _check("POST /control/queue (real run) → 202", code == 202, str(body)[:250]):
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
            if st in ("enqueued", "acquiring"):
                seen_active = True
            if st in ("done", "failed"):
                final = st
                break
        time.sleep(5)

    _check("run reached enqueued/acquiring (instrument accepted it)", seen_active)
    _check("run finalized as done (not failed)", final == "done", f"final status={final}")

    # Park the instrument.
    code, body = _request("POST", f"{base}/control/standby")
    _check("POST /control/standby → 202", code == 202, str(body)[:200])


def stage_abort(base: str) -> None:
    _section("Stage 5 — abort clears the queue")
    code, body = _request("POST", f"{base}/control/abort")
    _check("POST /control/abort → 200", code == 200, str(body)[:200])


def stage_release(base: str) -> None:
    _section("Stage 6 — release claim (idempotent)")
    code, _ = _request("POST", f"{base}/control/release")
    _check("POST /control/release → 204", code == 204, f"got {code}")
    code, _ = _request("POST", f"{base}/control/release")
    _check("second release also → 204 (idempotent)", code == 204, f"got {code}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8011",
                   help="Sidecar base URL (use the STAGING port, e.g. :8011)")
    p.add_argument("--owner", default="smoke-test-student")
    p.add_argument("--session-id", default=f"smoke-{uuid.uuid4().hex[:8]}")
    p.add_argument("--reserved-tray", default="front",
                   help="Must match the device's RESERVED_ROBOT_TRAY (default front = robot tray)")
    p.add_argument("--run-hardware", action="store_true",
                   help="ENABLE the real run + standby stage (moves hardware)")
    p.add_argument("--tray", default="rear", help="Tray for the real run (use the UNreserved manual tray)")
    p.add_argument("--well", default="A1", help="Well for the real run, e.g. A1")
    p.add_argument("--output-dir", default="C:/CDSProjects/Installation/Results/SmokeTest")
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    print(f"Smoke test against {base}  (session_id={args.session_id})")
    if args.run_hardware:
        print("\n*** --run-hardware ENABLED: a real injection + 2-min run will execute. ***")
        print("*** Front=D1F is the confirmed robot tray; confirm TRAY_REAR_DRAWER (D4B),  ***")
        print("*** the instrument is idle, and someone is watching it.                     ***")
        if input("Type 'yes' to proceed: ").strip().lower() != "yes":
            print("Aborted by user; running read-only stages only.")
            args.run_hardware = False

    try:
        stage_readonly(base)
        grant = stage_claim(base, args.owner, args.session_id)
        if not grant:
            print("\nClaim failed — cannot run mutating stages. Stopping.")
            return 1
        hb_interval = float(grant.get("heartbeat_interval_s", 15.0))
        stage_refusals(base, args.reserved_tray)
        if args.run_hardware:
            stage_hardware_run(base, args.tray, args.well, args.output_dir, hb_interval)
        else:
            print("\n(Skipping Stage 4 hardware run — pass --run-hardware to enable.)")
        stage_abort(base)
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
