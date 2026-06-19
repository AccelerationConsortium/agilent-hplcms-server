"""Thread-safe Moses subprocess manager with status-tracked job queue."""

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

# OLSS instrument states that mean the instrument is actively acquiring.
# When the runner is notified of these states it holds open any tracked job
# (even if Moses has exited) and refuses to start the next pending job.
_OLSS_ACTIVE_STATES: frozenset[str] = frozenset({"Run", "Running", "Busy", "Prerun", "PostRun"})


@dataclass
class JobEntry:
    """Internal tracking entry for one queued/active/completed run."""

    queue_id: str
    script_name: str
    job: dict[str, Any]          # Moses job spec written to disk
    request_dict: dict[str, Any]  # original RunRequest as dict (for API display)
    queued_at: datetime
    # "dispatched" = Moses script started but OpenLab not yet confirmed.
    # "enqueued"   = Moses exited cleanly; job submitted to OpenLab's queue.
    # "acquiring"  = OLSS confirms instrument is actively Running this job.
    # API maps "dispatched" → "pending"; "enqueued" passes through as-is.
    status: Literal["pending", "dispatched", "enqueued", "acquiring", "done", "failed"] = "pending"
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
        # True while OLSS reports the instrument as running or paused.
        # Updated by build_status() on each /status call; used to hold jobs
        # open when Moses exits before OpenLab finishes, and to queue (not
        # start) new submissions while the instrument is occupied externally.
        self._olss_occupied: bool = False

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

    def notify_olss_state(
        self, olss_state: str | None, olss_sw_status: str | None
    ) -> None:
        """Tell the runner whether OLSS reports the instrument as occupied.

        Called by ``build_status()`` on every ``/status`` poll so the runner
        stays current without importing any probe code.  ``_olss_occupied`` is
        True while the instrument is actively running *or* in a paused sequence.
        Used to hold a Moses job open after Moses exits early (the job stays
        "acquiring" until OLSS confirms the run is done).  It does NOT gate new
        job submissions — Moses submits directly to OpenLab's native queue and
        the instrument handles ordering.

        Also drives the ``"enqueued"`` ↔ ``"acquiring"`` transitions:

        - idle → Running: job promoted from ``"enqueued"`` to ``"acquiring"``
          (instrument confirmed started after we submitted to its queue).
        - Running → idle: job finalized if result files confirm it ran
          (``done``), otherwise demoted back to ``"enqueued"`` — a preceding
          OpenLab job may have just finished while ours is still waiting.
          Use ``/control/abort`` to manually clear a stuck job.
        """
        occupied = olss_state in _OLSS_ACTIVE_STATES or (
            olss_sw_status == "Paused"
            and olss_state not in (None, "NotConnected")
        )
        entry_to_finalize: JobEntry | None = None
        with self._lock:
            was_occupied = self._olss_occupied
            self._olss_occupied = occupied

            if self._active_id is not None:
                entry = self._jobs.get(self._active_id)
                if entry is not None:
                    if occupied and entry.status == "enqueued":
                        # Instrument now reports Running; our submitted job is
                        # being (or is about to be) acquired.
                        entry.status = "acquiring"
                    elif was_occupied and not occupied:
                        # Running → idle: need sirslt check outside the lock.
                        if entry.status in ("acquiring", "enqueued"):
                            entry_to_finalize = entry

        if entry_to_finalize is None:
            return

        # Filesystem check outside the lock — may be slow on network paths.
        has_results = self._has_sirslt(entry_to_finalize)
        now = datetime.now(timezone.utc)
        with self._lock:
            # Re-verify: the background poller may have finalised the job
            # in the gap between the two lock sections (rare but possible).
            if (
                self._active_id == entry_to_finalize.queue_id
                and entry_to_finalize.status in ("acquiring", "enqueued")
            ):
                if has_results:
                    entry_to_finalize.status = "done"
                    entry_to_finalize.finished_at = now
                    self._active_id = None
                    self._evict_history()
                    logger.info(
                        "Run %s finalised via OLSS idle transition: done (has_sirslt=True)",
                        entry_to_finalize.queue_id,
                    )
                else:
                    # No result files yet — a preceding job may have just
                    # finished and ours is still waiting in OpenLab's queue.
                    # Demote to "enqueued"; the next idle→Running→idle cycle
                    # (when our run actually executes) will finalize it.
                    # Use /control/abort to manually clear a stuck job.
                    entry_to_finalize.status = "enqueued"
                    logger.debug(
                        "Run %s: OLSS went idle but no sirslt yet; "
                        "demoted to enqueued (job may be waiting in OpenLab queue)",
                        entry_to_finalize.queue_id,
                    )

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
        """Check if the active process finished; if so, start the next pending run."""
        settings = settings or load_settings()
        next_entry: JobEntry | None = None
        # Set when rc==0 and OLSS is idle — needs sirslt check outside the lock.
        check_id: str | None = None
        check_entry: JobEntry | None = None

        with self._lock:
            if self._active_id is not None:
                entry = self._jobs[self._active_id]
                rc = entry.process.poll() if entry.process else 0
                if rc is None:
                    return  # Moses still running

                # Moses has exited (or process handle was already cleared).
                # If OLSS says the instrument is still occupied, the job is
                # submitted to OpenLab's native queue (or actively running).
                # Only transition from "dispatched" → "enqueued" here; if the
                # entry is already "enqueued" or "acquiring", leave it to
                # notify_olss_state which drives those transitions.
                if self._olss_occupied and entry.status not in ("done", "failed"):
                    if rc != 0:
                        logger.warning(
                            "Run %s: Moses exited rc=%d while OLSS still occupied; "
                            "holding job open (tracked via OLSS)",
                            self._active_id, rc,
                        )
                    if entry.status == "dispatched":
                        entry.status = "enqueued"
                    entry.process = None  # release handle; completion via OLSS
                    return

                # Moses exited and OLSS is not occupied.
                if rc != 0:
                    # Definite failure — no filesystem check needed.
                    entry.finished_at = datetime.now(timezone.utc)
                    entry.status = "failed"
                    entry.error_msg = f"Exit code {rc}"
                    logger.info("Run %s finished (exit %s)", self._active_id, rc)
                    self._active_id = None
                    self._evict_history()
                else:
                    # rc == 0: Moses submitted the job cleanly.  We cannot
                    # distinguish "run completed" from "job queued in OpenLab
                    # behind other runs" without checking the result directory.
                    # Release the process handle and defer the decision — the
                    # filesystem check must happen outside the lock.
                    entry.process = None
                    check_id = self._active_id
                    check_entry = entry

            if check_entry is None:
                if self._active_id is not None or not self._pending_ids:
                    return
                next_id = self._pending_ids.popleft()
                next_entry = self._jobs.get(next_id)

        if check_entry is not None:
            has_sirslt = self._has_sirslt(check_entry)
            now = datetime.now(timezone.utc)
            with self._lock:
                if (
                    self._active_id == check_id
                    and check_entry.status not in ("done", "failed")
                ):
                    if has_sirslt:
                        check_entry.status = "done"
                        check_entry.finished_at = now
                        self._active_id = None
                        self._evict_history()
                        logger.info("Run %s finished (exit 0, sirslt present)", check_id)
                    else:
                        # No result files yet — job may be waiting in OpenLab's
                        # queue behind other runs.  Keep as "enqueued"; OLSS
                        # idle→Running promotes it to "acquiring", and the next
                        # Running→idle cycle finalises it when results appear.
                        check_entry.status = "enqueued"
                        logger.debug(
                            "Run %s: Moses exited cleanly but no sirslt yet; "
                            "holding as enqueued (job may be queued in OpenLab)",
                            check_id,
                        )

            if has_sirslt:
                with self._lock:
                    if self._active_id is not None or not self._pending_ids:
                        return
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

        Position 0 means started immediately (instrument was idle).

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

            if self._active_id is None:
                self._launch_locked(entry, settings)
                return queue_id, 0

            if len(self._pending_ids) >= settings.queue_max_depth:
                del self._jobs[queue_id]
                raise OverflowError(
                    f"Queue is full ({settings.queue_max_depth} pending runs)."
                )

            self._pending_ids.append(queue_id)
            return queue_id, len(self._pending_ids)

    def check_acquiring(self, settings: Settings | None = None) -> bool:
        """Return True if the active job's own output_dir has *.sirslt activity after job start.

        Scoped to the specific job's output_dir so that pre-existing or concurrent
        OpenLab jobs writing to other result directories cannot trigger a false positive.
        """
        with self._lock:
            if self._active_id is None:
                return False
            entry = self._jobs.get(self._active_id)
            if entry is None or entry.status != "dispatched":
                return False
            output_dir_str = entry.request_dict.get("output_dir", "")
            started_at = entry.started_at

        if not output_dir_str or not started_at:
            return False

        output_path = Path(output_dir_str)
        if not output_path.exists():
            return False

        started_epoch = started_at.timestamp()
        for sirslt in output_path.glob("**/*.sirslt"):
            try:
                if sirslt.stat().st_mtime >= started_epoch:
                    return True
            except OSError:
                pass
        return False

    def maybe_promote_to_acquiring(self, settings: Settings | None = None) -> None:
        """Promote dispatched → acquiring if the job's output_dir shows sirslt activity."""
        if self.check_acquiring(settings):
            self._promote_to_acquiring()

    def _promote_to_acquiring(self) -> None:
        with self._lock:
            if self._active_id is None:
                return
            entry = self._jobs.get(self._active_id)
            if entry and entry.status == "dispatched":
                entry.status = "acquiring"

    def update_active_from_signals(self, acquisition_active: bool) -> None:
        """Promote the active job from dispatched → acquiring once OpenLab confirms."""
        if acquisition_active:
            self._promote_to_acquiring()

    def cancel_queued(self, queue_id: str) -> JobEntry:
        """Cancel a pending job.

        Raises
        ------
        KeyError
            Job not found.
        RuntimeError
            Job is dispatched or acquiring; use /control/abort instead.
        LookupError
            Job already completed (done or failed).
        """
        with self._lock:
            entry = self._jobs.get(queue_id)
            if entry is None:
                raise KeyError(queue_id)
            if entry.status in ("dispatched", "enqueued", "acquiring"):
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

        entry.status = "dispatched"
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

    def _has_sirslt(self, entry: JobEntry) -> bool:
        """Return True if the job's output_dir has *.sirslt files written after job start.

        Called outside the lock.  Returns True (assume success) when output_dir is
        not configured or the path does not exist — we cannot verify without a
        concrete results directory, so we default to done rather than failed.
        """
        output_dir_str = entry.request_dict.get("output_dir", "")
        started_at = entry.started_at
        if not output_dir_str or not started_at:
            return True
        output_path = Path(output_dir_str)
        if not output_path.exists():
            return False
        started_epoch = started_at.timestamp()
        for sirslt in output_path.glob("**/*.sirslt"):
            try:
                if sirslt.stat().st_mtime >= started_epoch:
                    return True
            except OSError:
                pass
        return False
