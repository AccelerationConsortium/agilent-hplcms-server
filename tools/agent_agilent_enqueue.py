"""Submit-and-exit Moses script for the sidecar's ``dispatch="openlab"`` mode.

DEPLOY: copy to ``<MOSES_WORK_DIR>/examples/agent_agilent_enqueue.py`` on the
instrument PC, next to ``agent_agilent.py`` (which it imports from — lazily,
so this module also loads off-PC for tests). The sidecar launches it exactly
like the batch script — ``python <script> <job.json>`` with the same job
shape — but expects it to EXIT as soon as every injection has been accepted
by OpenLab.

Contract with the sidecar (``control/runner.py::submit_openlab_handoff``):

- exit 0    — every sample in the job was accepted into OpenLab's run queue.
  From that point the acquisition belongs to OpenLab; this script must NOT
  wait for it (waiting is the batch script's job).
- exit != 0 — a submission was refused. The sidecar surfaces the last log
  line as the job's failure reason. NOTE: samples logged as ``Enqueued i/n``
  before the failure are already in OpenLab's queue — a failed handoff is not
  necessarily an empty one.

How it submits: ``Agilent.start_run`` couples the SDK enqueue
(``InstController.submit_single_run`` — the call that lines the run up in
OpenLab's Run Queue) with status/sleep wait loops. This script performs the
same run-parameter construction, then stops after ``submit_single_run``.

**Method store.** Queued OpenLab runs reference their acquisition method by
PATH, so methods must be immutable once a run points at them. Every method
this script uses lives in a store directory (``HPLCMS_AGENT_METHODS_DIR``,
default ``agent_methods/`` beside the configured method templates) as an
``.amx``/``.smx`` snapshot plus a JSON manifest of the canonical spec:

- **Lookup before programming.** An exact spec match reuses the stored
  method outright. A spec merely *within tolerance* of a stored one (e.g. a
  5.0 vs 5.1 min run time — no practical difference) coalesces onto it
  instead of minting a near-duplicate; the substitution is logged. Solvents,
  MS mode, and the gradient's row count must match exactly — different
  chemistry is never "close". Tolerances are the ``_TOL_*`` constants below.
- **Miss → program once, snapshot, reuse forever.** Only on a miss does the
  script program the method via ``set_acquisition_method`` (which writes
  through the shared templates, as the batch script always has), then copies
  the exact files a run would reference into the store under a readable key
  (``<ms_mode>_<runtime>min_<flow>mLmin_<hash8>``).

``standby_after`` is accepted for job-shape compatibility and ignored:
parking the instrument means taking it now, which a fire-and-forget
submission never does. Use the sidecar's ``/control/standby`` once OpenLab's
queue drains.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent_enqueue")

# ---------------------------------------------------------------------------
# Method store — canonical spec, readable key, tolerance coalescing
# ---------------------------------------------------------------------------

STORE_DIR_ENV = "HPLCMS_AGENT_METHODS_DIR"

# A requested method coalesces onto a stored one when every numeric dimension
# is within these tolerances (and the exact-match dimensions agree). Chosen so
# that differences with no practical analytical effect reuse the stored
# method, while a real gradient-shape change stays a new method.
_TOL_TIME_ABS_MIN = 0.1    # run time + gradient time points: max(0.1 min, 2%)
_TOL_TIME_REL = 0.02
_TOL_FRACTION_B = 0.005    # 0.5 %B at any gradient point
_TOL_FLOW_ABS = 0.02       # flow: max(0.02 mL/min, 3%)
_TOL_FLOW_REL = 0.03
_TOL_EQUILIBRATION_MIN = 0.5


def _canonical_spec(gradient: Dict[str, Any], ms_mode: str) -> Dict[str, Any]:
    """The identity of a method: gradient chemistry + MS polarity.

    The gradient's free-text ``name`` is presentation, not chemistry, so it is
    deliberately excluded — two requests differing only in name are the same
    method."""
    return {
        "ms_mode": ms_mode,
        "solvent_a": gradient["solvent_a"],
        "solvent_b": gradient["solvent_b"],
        "run_time": float(gradient["run_time"]),
        "flow_rate": float(gradient["flow_rate"]),
        "equilibration_time": float(gradient.get("equilibration_time") or 0.0),
        "gradient_table": [[float(t), float(b)] for t, b in gradient["gradient_table"]],
    }


def _method_key(spec: Dict[str, Any]) -> str:
    """Readable store key: mode + headline numbers + short content hash."""
    def num(x: float) -> str:
        return f"{x:g}".replace(".", "p").replace("-", "m")

    digest = hashlib.sha1(
        json.dumps(spec, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    return (
        f"{spec['ms_mode']}_{num(spec['run_time'])}min_"
        f"{num(spec['flow_rate'])}mLmin_{digest}"
    )


def _within(a: float, b: float, abs_tol: float, rel_tol: float = 0.0) -> bool:
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))


def _equivalent_spec(requested: Dict[str, Any], stored: Dict[str, Any]) -> bool:
    """True when the requested method may reuse the stored one.

    Exact-match dimensions first: different chemistry is never "close", and a
    gradient with a different number of rows has a different shape by
    construction. Then every numeric dimension pointwise within tolerance."""
    if (
        requested["ms_mode"] != stored["ms_mode"]
        or requested["solvent_a"] != stored["solvent_a"]
        or requested["solvent_b"] != stored["solvent_b"]
        or len(requested["gradient_table"]) != len(stored["gradient_table"])
    ):
        return False
    if not _within(requested["run_time"], stored["run_time"], _TOL_TIME_ABS_MIN, _TOL_TIME_REL):
        return False
    if not _within(requested["flow_rate"], stored["flow_rate"], _TOL_FLOW_ABS, _TOL_FLOW_REL):
        return False
    if not _within(
        requested["equilibration_time"], stored["equilibration_time"], _TOL_EQUILIBRATION_MIN
    ):
        return False
    for (t_req, b_req), (t_st, b_st) in zip(
        requested["gradient_table"], stored["gradient_table"]
    ):
        if not _within(t_req, t_st, _TOL_TIME_ABS_MIN, _TOL_TIME_REL):
            return False
        if not _within(b_req, b_st, _TOL_FRACTION_B):
            return False
    return True


def _write_manifest(store_dir: Path, key: str, spec: Dict[str, Any]) -> Path:
    manifest = {
        "key": key,
        "spec": spec,
        "amx": f"{key}.amx",
        "smx": f"{key}.smx",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path = Path(store_dir) / f"{key}.json"
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return path


def _find_stored_method(store_dir: Path, spec: Dict[str, Any]) -> Optional[str]:
    """Return the store key to reuse for ``spec``, or None on a miss.

    An exact spec match always wins; otherwise the first (oldest by key
    order — deterministic) stored method equivalent within tolerance."""
    equivalent: Optional[str] = None
    for path in sorted(Path(store_dir).glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            stored = manifest["spec"]
            key = manifest["key"]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            # A corrupt manifest must not brick every submission, but it is an
            # operational problem — surface it on every lookup until fixed.
            logger.warning("Skipping unreadable method manifest %s: %s", path.name, exc)
            continue
        if stored == spec:
            return key
        if equivalent is None and _equivalent_spec(spec, stored):
            equivalent = key
    return equivalent


def _store_dir(controller: Any) -> Path:
    env = os.environ.get(STORE_DIR_ENV)
    if env:
        return Path(env)
    # Beside the configured templates: the one directory tree OpenLab's
    # acquisition service is already proven to read methods from.
    template = Path(controller._software_settings["default_methods"]["positive"])
    return template.parent / "agent_methods"


def _ensure_method(controller: Any, spec: Dict[str, Any], program) -> Tuple[Path, Path]:
    """Return immutable (amx, smx) paths for ``spec``, programming on a miss.

    ``program`` is the zero-arg closure that runs
    ``controller.set_acquisition_method(...)`` — called only when nothing in
    the store can be reused. It writes through the shared templates exactly as
    the batch script always has; the snapshot taken immediately after is what
    the queued run references, so later programming cannot mutate it."""
    store = _store_dir(controller)
    store.mkdir(parents=True, exist_ok=True)

    hit = _find_stored_method(store, spec)
    if hit is not None:
        manifest = json.loads((store / f"{hit}.json").read_text(encoding="utf-8"))
        if manifest["spec"] == spec:
            logger.info("Reusing stored method %s (exact spec match)", hit)
        else:
            logger.info(
                "Coalescing onto stored method %s — requested spec differs only "
                "within tolerance; the stored method is what will run. "
                "Requested: %s | Stored: %s",
                hit, json.dumps(spec, sort_keys=True),
                json.dumps(manifest["spec"], sort_keys=True),
            )
        return store / manifest["amx"], store / manifest["smx"]

    key = _method_key(spec)
    program()
    amx_src = Path(str(controller.acquisition_method))
    smx_src = Path(str(controller.system.smxfile))  # the file a run references
    shutil.copy2(amx_src, store / f"{key}.amx")
    shutil.copy2(smx_src, store / f"{key}.smx")
    _write_manifest(store, key, spec)
    logger.info("Stored new method %s (snapshot of %s + %s)", key, amx_src.name, smx_src.name)
    return store / f"{key}.amx", store / f"{key}.smx"


# ---------------------------------------------------------------------------
# Enqueue-into-OpenLab batch
# ---------------------------------------------------------------------------


def enqueue_batch(
    instrument_config_path: str,
    output_dir: str,
    gradient: Dict[str, Any],
    samples: List[Dict[str, Any]],
    ms_mode: str = "positive",
    standby_after: bool = True,  # ignored — see module docstring
) -> List[str]:
    """Enqueue every sample into OpenLab's run queue and return the run ids."""
    # Instrument imports stay lazy so the method-store logic above is testable
    # off-PC (tests/test_agent_enqueue_tool.py loads this module without moses).
    from agent_agilent import GradientConfig, SampleConfig, connect

    gradient_cfg = GradientConfig.from_dict(gradient)
    sample_cfgs = [SampleConfig.from_dict(s) for s in samples]

    controller = connect(instrument_config_path)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # One immutable method for the whole batch, reused across batches via the
    # store. Programming happens at most once, on a store miss.
    lc_gradient = gradient_cfg.to_lc_gradient()
    spec = _canonical_spec(gradient, ms_mode)
    amx_path, smx_path = _ensure_method(
        controller,
        spec,
        lambda: controller.set_acquisition_method(
            gradient=lc_gradient, ms_settings={"mode": ms_mode}
        ),
    )
    # Mirror AgilentController.set_acquisition_method's equilibration rule, and
    # point the SDK at the store's immutable smx snapshot instead of the shared
    # template it would otherwise reference.
    equilibration = lc_gradient.equilibration_time is not None
    system = controller.system  # moses.agilent.Agilent
    system.smxfile = str(smx_path)

    run_ids: List[str] = []
    for i, sample in enumerate(sample_cfgs, start=1):
        # Timestamped name for result-folder uniqueness, mirroring run_single.
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        unique_name = f"{sample.sample_name}_{timestamp}"

        params = system._make_single_run_params(
            sample_name=unique_name,
            sample_position=sample.sample_position,
            injection_volume=sample.injection_volume,
            result_path=str(out),
            method_path=str(amx_path),
            processing_path=system.processing_path,
            equilibration=equilibration,
        )
        # Any SDK refusal propagates: traceback in the log, non-zero exit.
        results = system.controller.submit_single_run(params.data)
        run_ids.append(str(results.run_id))
        logger.info(
            "Enqueued %d/%d in OpenLab: %s | position %s | RunID %s",
            i, len(sample_cfgs), unique_name, sample.sample_position, results.run_id,
        )

    return run_ids


def _cli() -> None:
    if len(sys.argv) != 2:
        print("Usage: python agent_agilent_enqueue.py <job.json>")
        sys.exit(1)
    job_path = Path(sys.argv[1])
    if not job_path.exists():
        print(f"Job file not found: {job_path}")
        sys.exit(1)

    from agent_agilent import _setup_logging

    _setup_logging()
    with open(job_path) as f:
        job = json.load(f)

    run_ids = enqueue_batch(**job)
    print(f"\nEnqueued {len(run_ids)} run(s) in OpenLab's queue:")
    for rid in run_ids:
        print(f"  {rid}")


if __name__ == "__main__":
    _cli()
