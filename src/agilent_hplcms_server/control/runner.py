"""Thread-safe Moses subprocess manager with status-tracked job queue.

The sidecar's ``MosesRunner`` is the **sole** job queue (queue-ownership pivot,
2026-06-23). OpenLab's native sequence queue is no longer used for our jobs —
OpenLab (OLSS) is reserved for technician servicing / maintenance.

This rests on a verified fact about Moses: ``moses.agilent`` ``start_run`` runs
fully **synchronously** — it blocks through Running → run-duration → Idle before
returning, and ``run_batch`` loops samples one at a time, exiting non-zero only
if a sample failed. So **process exit is authoritative**:

- subprocess alive  → batch running
- exit code ``0``   → batch done
- exit code ``!= 0``→ batch failed

That makes job completion a simple ``process.poll()`` — no ``.sirslt`` polling,
no OLSS-driven finalization, no "enqueued/waiting in OpenLab's queue" state. The
only thing the runner still learns from OLSS is whether a *technician* is driving
the instrument directly (servicing), so it can halt the queue.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..config import Settings, load_settings

logger = logging.getLogger(__name__)


@dataclass
class JobEntry:
    """Internal tracking entry for one queued/active/completed run.

    Status lifecycle (process-exit authoritative):

    - ``pending`` — queued, subprocess not yet launched.
    - ``running`` — Moses subprocess is alive (acquiring synchronously).
    - ``done``    — subprocess exited 0.
    - ``failed``  — subprocess exited non-zero, or the job was aborted/cancelled.
    """

    queue_id: str
    script_name: str
    job: dict[str, Any]          # Moses job spec written to disk
    request_dict: dict[str, Any]  # original RunRequest as dict (for API display)
    queued_at: datetime
    status: Literal["pending", "running", "done", "failed"] = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pid: int | None = None
    process: subprocess.Popen | None = field(default=None, repr=False)  # type: ignore[type-arg]
    error_msg: str | None = None
    job_path: Path = field(default_factory=lambda: Path(""))
    log_path: Path | None = None


@dataclass
class ActiveRun:
    """Backward-compat view of the currently running job."""

    run_id: str
    pid: int
    process: subprocess.Popen  # type: ignore[type-arg]
    started_at: datetime
    job_path: Path
    script_name: str


class MosesRunner:
    """Owns one active Moses subprocess slot plus a FIFO queue with full status tracking.

    Jobs progress: pending → running → done | failed.
    Completed jobs are retained up to _HISTORY_LIMIT entries so clients can query history.
    """

    _HISTORY_LIMIT = 50

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, JobEntry] = {}
        self._pending_ids: deque[str] = deque()
        self._active_id: str | None = None
        self._poller_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Servicing detection has two independent sources, both surfaced through
        # is_servicing():
        #   1. _service_mode — an explicit, persistent flag toggled by an admin
        #      (Service-Account) via /control/service/start|end. Primary signal;
        #      survives claim release / dashboard disconnect so a maintenance
        #      window is never silently un-blocked.
        #   2. auto-detect fallback — OLSS shows a real acquisition (a runQueue
        #      currentRun) while we hold no active job, sustained over a debounce.
        #      Catches "the technician forgot to flip the switch". Keyed on
        #      currentRun (not bare state=="Busy") so data analysis / reprocessing
        #      does NOT halt the queue. Updated by build_status() via
        #      notify_olss_state() on every /status poll. Neither source drives
        #      job completion — process exit does that.
        self._service_mode: bool = False
        self._olss_run_no_job_streak: int = 0

    # ------------------------------------------------------------------
    # Read-only queries
    # ------------------------------------------------------------------

    def is_busy(self) -> bool:
        with self._lock:
            return self._active_id is not None

    def get_active(self) -> ActiveRun | None:
        with self._lock:
            if self._active_id is None:
                return None
            e = self._jobs[self._active_id]
            return ActiveRun(
                run_id=e.queue_id,
                pid=e.pid or 0,
                process=e.process,  # type: ignore[arg-type]
                started_at=e.started_at or datetime.now(timezone.utc),
                job_path=e.job_path,
                script_name=e.script_name,
            )

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._pending_ids)

    def is_queue_full(self, settings: Settings | None = None) -> bool:
        """True iff a new enqueue would be refused with 412 queue_full.

        Mirrors :meth:`submit_to_queue`: the depth cap only bites while a run is
        active (an idle instrument launches the new job immediately). Read by
        ``status_builder`` so ``allowed_actions`` drops the enqueue verbs in
        exactly the states the enqueue endpoints would 412 (§6.2).
        """
        settings = settings or load_settings()
        with self._lock:
            return self._active_id is not None and len(self._pending_ids) >= settings.queue_max_depth

    def is_servicing(self, settings: Settings | None = None) -> bool:
        """True iff the queue must be halted for technician servicing.

        Two sources (see __init__): the explicit ``service_mode`` flag, OR the
        auto-detect fallback — OLSS shows a real acquisition while the sidecar
        holds no active job, sustained for ``servicing_debounce_polls``
        consecutive ``/status`` observations. The debounce avoids a false
        positive in the one-poll gap after our Moses process exits but before
        OLSS clears its currentRun. While True the queue is halted and
        submissions are refused 409 instrument_servicing (highest precedence).
        """
        settings = settings or load_settings()
        with self._lock:
            return self._is_servicing_locked(settings)

    def _is_servicing_locked(self, settings: Settings) -> bool:
        if self._service_mode:
            return True
        debounce = max(1, settings.servicing_debounce_polls)
        return self._active_id is None and self._olss_run_no_job_streak >= debounce

    def set_service_mode(self, on: bool) -> None:
        """Set/clear the explicit, persistent service-mode flag (admin toggle)."""
        with self._lock:
            self._service_mode = bool(on)
        logger.info("Service mode %s", "ENABLED" if on else "cleared")

    def service_mode(self) -> bool:
        """Current state of the explicit service-mode flag (for /status details)."""
        with self._lock:
            return self._service_mode

    def notify_olss_state(
        self,
        olss_state: str | None,
        olss_sw_status: str | None,
        olss_current_run: str | None = None,
    ) -> None:
        """Update the servicing auto-detect fallback from the latest OLSS poll.

        Called by ``build_status()`` on every ``/status`` poll so the runner
        stays current without importing any probe code. ``run_active`` keys on
        ``olss_current_run`` (a runQueue currentRun) — the signal that an actual
        acquisition sequence is underway — NOT bare ``state=="Busy"``, so data
        analysis / reprocessing does not halt the queue. Job completion is driven
        by process exit in :meth:`poll`, never by OLSS.
        """
        run_active = olss_current_run is not None
        with self._lock:
            # Streak counts observations of "a real OLSS run while we hold no
            # active job" — the signature of a technician acquiring directly.
            if run_active and self._active_id is None:
                self._olss_run_no_job_streak += 1
            else:
                self._olss_run_no_job_streak = 0

    def get_all_jobs(self) -> list[JobEntry]:
        """Snapshot of all tracked jobs sorted by queued_at (pending + active + history)."""
        with self._lock:
            return sorted(self._jobs.values(), key=lambda e: e.queued_at)

    def get_queue_snapshot(self) -> list[JobEntry]:
        """Pending-only snapshot (backward compat)."""
        with self._lock:
            return [self._jobs[qid] for qid in self._pending_ids if qid in self._jobs]

    # ------------------------------------------------------------------
    # Background poller
    # ------------------------------------------------------------------

    def start_poller(self, settings: Settings | None = None) -> None:
        settings = settings or load_settings()
        interval = settings.queue_poll_interval_s

        if self._poller_thread is not None and self._poller_thread.is_alive():
            return

        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.wait(timeout=interval):
                try:
                    self.poll(settings=settings)
                except Exception:
                    logger.exception("Queue poller error")

        self._poller_thread = threading.Thread(
            target=_loop, daemon=True, name="moses-queue-poller"
        )
        self._poller_thread.start()

    def stop_poller(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def poll(self, settings: Settings | None = None) -> None:
        """Finalize the active job if its process exited, then launch the next
        pending job — unless a technician is servicing the instrument.

        Process exit is authoritative: rc==0 → done, rc!=0 → failed. No
        filesystem or OLSS check is needed.
        """
        settings = settings or load_settings()
        next_entry: JobEntry | None = None

        with self._lock:
            if self._active_id is not None:
                entry = self._jobs[self._active_id]
                rc = entry.process.poll() if entry.process else 0
                if rc is None:
                    return  # Moses still running

                entry.finished_at = datetime.now(timezone.utc)
                entry.process = None
                if rc == 0:
                    entry.status = "done"
                    logger.info("Run %s finished (exit 0)", self._active_id)
                else:
                    entry.status = "failed"
                    entry.error_msg = f"Exit code {rc}"
                    logger.info("Run %s failed (exit %s)", self._active_id, rc)
                self._active_id = None
                self._evict_history()

            # Launch the next pending job, unless a technician holds the
            # instrument (servicing) — the queue is halted, not drained, so the
            # job simply waits and starts once servicing clears.
            if (
                self._active_id is None
                and self._pending_ids
                and not self._is_servicing_locked(settings)
            ):
                next_id = self._pending_ids.popleft()
                next_entry = self._jobs.get(next_id)

        if next_entry is not None:
            try:
                self._launch_entry(next_entry, settings)
            except Exception:
                logger.exception("Failed to auto-start queued run %s", next_entry.queue_id)
                with self._lock:
                    next_entry.status = "failed"
                    next_entry.finished_at = datetime.now(timezone.utc)
                    next_entry.error_msg = "Launch failed"

    def enqueue(
        self,
        script_name: str,
        job: dict[str, Any],
        settings: Settings | None = None,
    ) -> tuple[str, int]:
        """Backward-compat entry point. Returns (queue_id, position)."""
        return self.submit_to_queue(
            script_name=script_name,
            job=job,
            request_dict={"script_name": script_name, **job},
            settings=settings,
        )

    def submit_to_queue(
        self,
        script_name: str,
        job: dict[str, Any],
        request_dict: dict[str, Any],
        settings: Settings | None = None,
    ) -> tuple[str, int]:
        """Add a run to the queue. Returns (queue_id, position).

        Position 0 means started immediately (instrument was idle and not being
        serviced). Higher-precedence refusals (servicing 409, workflow 423) are
        enforced by the router before this is called; the servicing guard here is
        defense-in-depth so we never launch a job into a technician's session.

        Raises
        ------
        ValueError
            If *script_name* is not in the allowed-scripts list.
        FileNotFoundError
            If the resolved script path does not exist.
        OverflowError
            If the queue is already at max depth.
        """
        settings = settings or load_settings()

        allowed = [s.strip() for s in settings.moses_allowed_scripts.split(",") if s.strip()]
        if script_name not in allowed:
            raise ValueError(
                f"Script '{script_name}' is not in MOSES_ALLOWED_SCRIPTS. Allowed: {allowed}"
            )

        work_dir = Path(settings.moses_work_dir)
        script_path = work_dir / script_name
        if not script_path.exists():
            raise FileNotFoundError(f"Moses script not found: {script_path}")

        jobs_dir = Path(settings.run_jobs_dir)
        jobs_dir.mkdir(parents=True, exist_ok=True)

        queue_id = str(uuid.uuid4())
        job_path = jobs_dir / f"{queue_id}.json"
        job_path.write_text(json.dumps(job, indent=2), encoding="utf-8")

        entry = JobEntry(
            queue_id=queue_id,
            script_name=script_name,
            job=job,
            request_dict=request_dict,
            queued_at=datetime.now(timezone.utc),
            job_path=job_path,
        )

        with self._lock:
            self._jobs[queue_id] = entry

            # Launch immediately only if idle AND not being serviced; otherwise
            # queue it (respecting the depth cap).
            if self._active_id is None and not self._is_servicing_locked(settings):
                self._launch_locked(entry, settings)
                return queue_id, 0

            if len(self._pending_ids) >= settings.queue_max_depth:
                del self._jobs[queue_id]
                raise OverflowError(
                    f"Queue is full ({settings.queue_max_depth} pending runs)."
                )

            self._pending_ids.append(queue_id)
            return queue_id, len(self._pending_ids)

    def cancel_queued(self, queue_id: str) -> JobEntry:
        """Cancel a pending job.

        Raises
        ------
        KeyError
            Job not found.
        RuntimeError
            Job is currently running; use /control/abort instead.
        LookupError
            Job already completed (done or failed).
        """
        with self._lock:
            entry = self._jobs.get(queue_id)
            if entry is None:
                raise KeyError(queue_id)
            if entry.status == "running":
                raise RuntimeError("Job is currently running; use /control/abort to stop it.")
            if entry.status in ("done", "failed"):
                raise LookupError(f"Job already {entry.status}.")
            try:
                self._pending_ids.remove(queue_id)
            except ValueError:
                pass
            entry.status = "failed"
            entry.finished_at = datetime.now(timezone.utc)
            entry.error_msg = "Cancelled by operator"
        return entry

    def abort(self, settings: Settings | None = None) -> tuple[bool, int]:
        """Terminate the active process and clear the pending queue.

        Returns ``(was_active, n_queue_cleared)``.

        The active job is marked ``failed`` inside the lock before the lock is
        released, so the next ``GET /control/queue`` poll always sees the updated
        status even if the background poller fires concurrently.
        """
        settings = settings or load_settings()
        now = datetime.now(timezone.utc)
        proc_to_kill: subprocess.Popen | None = None  # type: ignore[type-arg]
        run_id = "none"

        with self._lock:
            n_cleared = len(self._pending_ids)
            for qid in list(self._pending_ids):
                e = self._jobs.get(qid)
                if e:
                    e.status = "failed"
                    e.finished_at = now
                    e.error_msg = "Aborted (queue cleared)"
            self._pending_ids.clear()

            if self._active_id is None:
                return False, n_cleared

            entry = self._jobs[self._active_id]
            proc_to_kill = entry.process
            run_id = self._active_id
            # Mark failed *inside* the lock so GET /control/queue immediately
            # reflects the aborted state regardless of concurrent poll() calls.
            entry.status = "failed"
            entry.finished_at = now
            entry.error_msg = "Aborted by operator"
            self._active_id = None
            self._evict_history()

        # Terminate outside the lock to avoid holding it during proc.wait().
        if proc_to_kill:
            proc_to_kill.terminate()
            try:
                proc_to_kill.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc_to_kill.kill()

        logger.info("Aborted run %s; cleared %d queued run(s)", run_id, n_cleared)
        return True, n_cleared

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _launch_entry(self, entry: JobEntry, settings: Settings) -> None:
        """Launch entry without holding the lock."""
        with self._lock:
            self._launch_locked(entry, settings)

    def _launch_locked(self, entry: JobEntry, settings: Settings) -> None:
        """Launch entry. Must be called WITH the lock held."""
        work_dir = Path(settings.moses_work_dir)
        script_path = work_dir / entry.script_name

        # Redirect Moses output to a log file beside the job JSON.
        # Using subprocess.PIPE without a reader causes Moses to block when the
        # 64 KB pipe buffer fills, preventing it from ever exiting.
        log_path = entry.job_path.with_suffix(".log") if entry.job_path.name else None
        log_fh = open(log_path, "w", encoding="utf-8", errors="replace") if log_path else None
        try:
            proc = subprocess.Popen(
                [settings.moses_python_exe, str(script_path), str(entry.job_path)],
                cwd=str(work_dir),
                stdout=log_fh or subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        finally:
            if log_fh:
                log_fh.close()  # subprocess holds its own handle; safe to close parent's copy

        entry.status = "running"
        entry.pid = proc.pid
        entry.process = proc
        entry.started_at = datetime.now(timezone.utc)
        entry.log_path = log_path
        self._active_id = entry.queue_id
        logger.info(
            "Started run %s (PID %d, script %s, log %s)",
            entry.queue_id, proc.pid, entry.script_name, log_path,
        )

    def _evict_history(self) -> None:
        """Remove oldest completed entries when over limit. Call WITH lock held."""
        completed = [
            (k, v) for k, v in self._jobs.items() if v.status in ("done", "failed")
        ]
        if len(completed) > self._HISTORY_LIMIT:
            completed.sort(key=lambda kv: kv[1].finished_at or kv[1].queued_at)
            for k, _ in completed[: len(completed) - self._HISTORY_LIMIT]:
                del self._jobs[k]
