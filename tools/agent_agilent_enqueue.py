"""Submit-and-exit Moses script for the sidecar's ``dispatch="openlab"`` mode.

DEPLOY: copy to ``<MOSES_WORK_DIR>/examples/agent_agilent_enqueue.py`` on the
instrument PC, next to ``agent_agilent.py`` (which it imports from). The
sidecar launches it exactly like the batch script — ``python <script>
<job.json>`` with the same job shape — but expects it to EXIT as soon as every
injection has been accepted by OpenLab.

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
same method programming and run-parameter construction, then stops after
``submit_single_run``.

Known constraint — shared method files: a queued OpenLab run references the
instrument method saved by ``set_acquisition_method`` by PATH. Submitting
another openlab-dispatch job with a DIFFERENT gradient before earlier
handed-off runs have acquired may re-edit files those runs still reference.
Keep one gradient per outstanding handoff batch; the sidecar does not police
this.

``standby_after`` is accepted for job-shape compatibility and ignored: parking
the instrument means taking it now, which a fire-and-forget submission never
does. Use the sidecar's ``/control/standby`` once OpenLab's queue drains.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# Deployed next to agent_agilent.py; ``python examples/agent_agilent_enqueue.py``
# puts examples/ at sys.path[0], so the batch script's helpers import directly.
from agent_agilent import GradientConfig, SampleConfig, _setup_logging, connect

logger = logging.getLogger("agent_enqueue")


def enqueue_batch(
    instrument_config_path: str,
    output_dir: str,
    gradient: Dict[str, Any],
    samples: List[Dict[str, Any]],
    ms_mode: str = "positive",
    standby_after: bool = True,  # ignored — see module docstring
) -> List[str]:
    """Enqueue every sample into OpenLab's run queue and return the run ids."""
    gradient_cfg = GradientConfig.from_dict(gradient)
    sample_cfgs = [SampleConfig.from_dict(s) for s in samples]

    controller = connect(instrument_config_path)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Program LC gradient + MS polarity once for the whole batch — one method
    # shared by every sample, exactly like run_batch.
    controller.set_acquisition_method(
        gradient=gradient_cfg.to_lc_gradient(),
        ms_settings={"mode": ms_mode},
    )

    system = controller.system  # moses.agilent.Agilent
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
            method_path=str(controller.acquisition_method),
            processing_path=system.processing_path,
            equilibration=controller.equilibration,
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

    _setup_logging()
    with open(job_path) as f:
        job = json.load(f)

    run_ids = enqueue_batch(**job)
    print(f"\nEnqueued {len(run_ids)} run(s) in OpenLab's queue:")
    for rid in run_ids:
        print(f"  {rid}")


if __name__ == "__main__":
    _cli()
