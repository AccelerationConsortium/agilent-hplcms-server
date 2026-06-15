"""Thread-safe Moses subprocess manager (Layer 2 device state machine)."""

from __future__ import annotations

import json
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings, load_settings


@dataclass
class ActiveRun:
    run_id: str
    pid: int
    process: subprocess.Popen  # type: ignore[type-arg]
    started_at: datetime
    job_path: Path
    script_name: str


class MosesRunner:
    """Owns exactly one Moses subprocess slot.

    Thread-safe: all mutations are protected by a lock so FastAPI worker
    threads cannot race on submit/abort/poll.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: ActiveRun | None = None

    # ------------------------------------------------------------------
    # Read-only queries
    # ------------------------------------------------------------------

    def is_busy(self) -> bool:
        with self._lock:
            return self._active is not None

    def get_active(self) -> ActiveRun | None:
        with self._lock:
            return self._active

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def poll(self) -> None:
        """Check whether the active process has finished; clear it if so."""
        with self._lock:
            if self._active is None:
                return
            if self._active.process.poll() is not None:
                self._active = None

    def submit(
        self,
        script_name: str,
        job: dict[str, Any],
        settings: Settings | None = None,
    ) -> ActiveRun:
        """Launch Moses as a subprocess.

        Raises
        ------
        ValueError
            If *script_name* is not in the allowed-scripts list, or if a run
            is already active (caller must check :meth:`is_busy` first under
            their own lock if they need atomicity).
        FileNotFoundError
            If the resolved script path does not exist on disk.
        """
        settings = settings or load_settings()

        allowed = [s.strip() for s in settings.moses_allowed_scripts.split(",") if s.strip()]
        if script_name not in allowed:
            raise ValueError(
                f"Script '{script_name}' is not in MOSES_ALLOWED_SCRIPTS. "
                f"Allowed: {allowed}"
            )

        work_dir = Path(settings.moses_work_dir)
        script_path = work_dir / script_name
        if not script_path.exists():
            raise FileNotFoundError(f"Moses script not found: {script_path}")

        jobs_dir = Path(settings.run_jobs_dir)
        jobs_dir.mkdir(parents=True, exist_ok=True)

        run_id = str(uuid.uuid4())
        job_path = jobs_dir / f"{run_id}.json"
        job_path.write_text(json.dumps(job, indent=2), encoding="utf-8")

        with self._lock:
            if self._active is not None:
                raise RuntimeError("A run is already active; abort it before submitting a new one.")

            proc = subprocess.Popen(
                [settings.moses_python_exe, str(script_path), str(job_path)],
                cwd=str(work_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            active = ActiveRun(
                run_id=run_id,
                pid=proc.pid,
                process=proc,
                started_at=datetime.now(timezone.utc),
                job_path=job_path,
                script_name=script_name,
            )
            self._active = active

        return active

    def abort(self, timeout_s: int = 10) -> bool:
        """Terminate the active Moses process.

        Returns True if a process was running and was terminated, False if
        nothing was active.
        """
        with self._lock:
            if self._active is None:
                return False
            proc = self._active.process

        proc.terminate()
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()

        with self._lock:
            self._active = None

        return True
