"""Pure read-only probe (option a + b hybrid) for the moses + OpenLab stack.

This probe NEVER:
    * opens a session against the instrument,
    * imports `moses` or `pythonnet`,
    * connects to the OpenLab named pipe / SharedServices,
    * writes to any file.

It only reads:
    * OS process metadata via :mod:`psutil`,
    * directory listings under ``CDS_RESULTS_DIR``,
    * the trailing bytes of a couple of OpenLab log files.

Returned dict shape (see ``status_builder.build_status``)::

    openlab_acquisition_alive: bool
    openlab_instrument_service_alive: bool
    openlab_reverse_proxy_alive: bool
    moses_process_alive: bool
    moses_process_pid: int | None
    last_run_dir: str | None
    last_run_mtime_iso8601: str | None
    last_run_mtime_epoch: float | None
    acquisition_active: bool
    last_error: { code, message, severity, timestamp_iso8601 } | None
    last_error_log_path: str | None
    last_observation_at: str               # iso8601 UTC
    probe_error: str | None                # populated if the probe itself
                                           # failed for environmental reasons
                                           # (missing dirs, permissions, etc.)
"""

from __future__ import annotations

import glob
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from ..config import Settings, load_settings
from .openlab_rest import read_instrument_state
from .rc_driver_log import read_rc_driver_log
from .sensor_file import read_sensor_file


# Process names that indicate the OpenLab supervisor stack is alive. We match
# on the bare process name reported by psutil (case-insensitive). Any one match
# is sufficient for that role; multiple are fine.
_OPENLAB_ACQUISITION_NAMES = {
    "AcquisitionServer.exe",
    "Agilent.OpenLAB.Acquisition.AcquisitionAgente.exe",
    "Agilent.OpenLAB.AcquisitionClient.exe",
}
_OPENLAB_INSTSVC_NAMES = {
    "Agilent.OpenLAB.Acquisition.AcqInstrumentService.exe",
    "AcqInstCfgServer.exe",
}
_OPENLAB_REVERSEPROXY_NAMES = {
    "OpenLabReverseProxy.exe",
}

# Lines we'd consider an error worth surfacing. Many OpenLab log lines start
# with an ISO-ish timestamp followed by the level. We keep the matcher lenient
# so we work across slightly different OpenLab log formats.
_ERROR_LEVEL_PATTERN = re.compile(r"\b(?:ERROR|CRITICAL|FATAL)\b", re.IGNORECASE)

# Background housekeeping components that log errors continuously and are not
# indicative of instrument readiness.  Lines matching any of these are skipped
# by _scan_recent_error so they do not flip equipment_state to "error".
_NOISE_PATTERN = re.compile(
    r"\b(?:ResultSetUploader|UploadRecoveryRun)\b",
    re.IGNORECASE,
)
_TIMESTAMP_PATTERNS = [
    # 2026-05-07 07:42:35,123
    re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
    # 05/07/2026 07:42:35
    re.compile(r"(?P<ts>\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})"),
]
_TAIL_BYTES_DEFAULT = 64 * 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_processes() -> tuple[bool, bool, bool, bool, int | None]:
    """Return ``(acq_alive, instsvc_alive, proxy_alive, moses_alive, moses_pid)``."""
    acq = instsvc = proxy = moses = False
    moses_pid: int | None = None
    moses_glob = load_settings().moses_env_glob
    moses_glob_norm = moses_glob.lower()

    for proc in psutil.process_iter(attrs=["name", "exe", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").strip()
            exe = (proc.info.get("exe") or "").strip()
            cmd_parts = proc.info.get("cmdline") or []
            cmdline = " ".join(cmd_parts) if cmd_parts else ""
            if name in _OPENLAB_ACQUISITION_NAMES:
                acq = True
            if name in _OPENLAB_INSTSVC_NAMES:
                instsvc = True
            if name in _OPENLAB_REVERSEPROXY_NAMES:
                proxy = True
            if not moses and name.lower() == "python.exe":
                exe_norm = exe.lower()
                if (
                    _path_matches_glob(exe_norm, moses_glob_norm)
                    and "pythofisher_hplcms" in cmdline.lower()
                ):
                    moses = True
                    moses_pid = proc.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    return acq, instsvc, proxy, moses, moses_pid


def _path_matches_glob(path: str, pattern: str) -> bool:
    """Cheap path-vs-glob match without re-walking the filesystem."""
    if not path or not pattern:
        return False
    if "*" not in pattern and "?" not in pattern:
        return path.startswith(pattern.rstrip("\\/"))
    # Anchor the glob to its parent dir so we match any python.exe living
    # underneath the expanded env directory.
    parent = pattern.rstrip("\\/")
    if "*" in parent:
        head, _, tail = parent.partition("*")
        head = head.rstrip("\\/")
        if not head:
            return True
        return head in path
    return path.startswith(parent)


def _newest_sirslt(
    results_dir: Path,
    root_limit: int = 12,
    dir_limit: int = 1500,
) -> tuple[Path | None, float | None]:
    """Find the newest ``*.sirslt`` directory and its newest contained mtime.

    A ``*.sirslt`` directory is created when an injection starts and is
    populated as the run progresses. Looking at the directory mtime alone can
    be misleading if the OS doesn't refresh it as files are added; we also
    consider the newest file mtime found inside, capped at a small number of
    files for cost.
    """
    if not results_dir.exists():
        return None, None
    newest_dir: Path | None = None
    newest_mtime: float | None = None
    try:
        root_dirs = sorted(
            (p for p in results_dir.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[: max(root_limit, 1)]
    except OSError:
        return None, None

    visited = 0
    try:
        for root in root_dirs:
            for current_root, dirs, _files in os.walk(root):
                visited += 1
                if visited > max(dir_limit, 1):
                    return newest_dir, newest_mtime
                for dirname in list(dirs):
                    if not dirname.lower().endswith(".sirslt"):
                        continue
                    sirslt = Path(current_root) / dirname
                    # Do not descend into the .sirslt payload unless this is
                    # the selected candidate. We only need one-level content
                    # mtimes to infer "currently writing".
                    try:
                        dirs.remove(dirname)
                    except ValueError:
                        pass
                    try:
                        if not sirslt.is_dir():
                            continue
                        dir_mtime = sirslt.stat().st_mtime
                        file_mtime = dir_mtime
                        # Cheap glance at the last-modified content file.
                        with os.scandir(sirslt) as it:
                            for entry in it:
                                try:
                                    m = entry.stat().st_mtime
                                    if m > file_mtime:
                                        file_mtime = m
                                except OSError:
                                    continue
                        m = max(dir_mtime, file_mtime)
                        if newest_mtime is None or m > newest_mtime:
                            newest_mtime = m
                            newest_dir = sirslt
                    except OSError:
                        continue
    except OSError:
        return newest_dir, newest_mtime
    return newest_dir, newest_mtime


def _tail_bytes(path: Path, n_bytes: int = _TAIL_BYTES_DEFAULT) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > n_bytes:
                f.seek(size - n_bytes)
            data = f.read()
    except OSError:
        return ""
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _parse_log_timestamp(line: str) -> datetime | None:
    for pat in _TIMESTAMP_PATTERNS:
        m = pat.search(line)
        if not m:
            continue
        ts = m.group("ts").replace(",", ".").replace("T", " ")
        try:
            if "/" in ts:
                dt = datetime.strptime(ts, "%m/%d/%Y %H:%M:%S")
            elif "." in ts:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
            else:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _classify_severity(line: str) -> str | None:
    upper = line.upper()
    if "FATAL" in upper:
        return "critical"
    if "CRITICAL" in upper:
        return "critical"
    if "ERROR" in upper:
        return "error"
    return None


def _scan_recent_error(
    log_dir: Path, error_window_s: int
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the most recent error event within ``error_window_s`` and its source path."""
    if not log_dir.exists():
        return None, None
    candidates: list[Path] = []
    instrument_log = log_dir / "InstrumentService.log"
    if instrument_log.exists():
        candidates.append(instrument_log)
    rest_log = log_dir / "rest-server.log"
    if rest_log.exists():
        candidates.append(rest_log)
    try:
        acq_logs = sorted(
            (p for p in log_dir.glob("AcquisitionServer-*.log") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        acq_logs = []
    if acq_logs:
        candidates.append(acq_logs[0])

    cutoff_epoch = datetime.now(timezone.utc).timestamp() - error_window_s
    newest: tuple[float, dict[str, Any], str] | None = None

    for path in candidates:
        try:
            file_mtime = path.stat().st_mtime
        except OSError:
            continue
        if file_mtime < cutoff_epoch:
            continue
        text = _tail_bytes(path)
        if not text:
            continue
        for line in reversed(text.splitlines()):
            if not _ERROR_LEVEL_PATTERN.search(line):
                continue
            if _NOISE_PATTERN.search(line):
                continue
            severity = _classify_severity(line) or "error"
            ts = _parse_log_timestamp(line)
            ts_epoch = ts.timestamp() if ts else file_mtime
            if ts_epoch < cutoff_epoch:
                break
            ts_iso = (
                ts.isoformat()
                if ts
                else datetime.fromtimestamp(file_mtime, tz=timezone.utc).isoformat()
            )
            event: dict[str, Any] = {
                "code": None,
                "message": line.strip()[:500],
                "severity": severity,
                "timestamp": ts_iso,
            }
            if newest is None or ts_epoch > newest[0]:
                newest = (ts_epoch, event, str(path))
            break
    if newest is None:
        return None, None
    _, event, source = newest
    return event, source


def read_signals(settings: Settings | None = None) -> dict[str, Any]:
    """Collect all read-only signals into a flat dict consumed by the builder."""
    settings = settings or load_settings()
    now_iso = _now_iso()
    probe_error: str | None = None

    try:
        acq_alive, instsvc_alive, proxy_alive, moses_alive, moses_pid = _check_processes()
    except Exception as exc:  # pragma: no cover - defensive
        acq_alive = instsvc_alive = proxy_alive = moses_alive = False
        moses_pid = None
        probe_error = f"process probe failed: {exc!r}"

    results_dir = Path(settings.cds_results_dir)
    log_dir = Path(settings.openlab_log_dir)

    if not results_dir.exists():
        last_run_dir, last_run_mtime = None, None
        if probe_error is None:
            probe_error = f"results dir not found: {results_dir}"
    else:
        last_run_dir, last_run_mtime = _newest_sirslt(
            results_dir,
            root_limit=settings.result_scan_root_limit,
            dir_limit=settings.result_scan_dir_limit,
        )

    acquisition_active = False
    if last_run_mtime is not None:
        age_s = datetime.now(timezone.utc).timestamp() - last_run_mtime
        if age_s <= settings.busy_threshold_s:
            acquisition_active = True

    last_run_iso: str | None = None
    if last_run_mtime is not None:
        last_run_iso = datetime.fromtimestamp(
            last_run_mtime, tz=timezone.utc
        ).isoformat()

    if not log_dir.exists():
        last_error = None
        last_error_log = None
        if probe_error is None:
            probe_error = f"openlab log dir not found: {log_dir}"
    else:
        last_error, last_error_log = _scan_recent_error(log_dir, settings.error_window_s)

    olss = read_instrument_state(
        olss_url=settings.openlab_olss_url,
        username=settings.openlab_username,
        instrument_id=settings.openlab_instrument_id,
    )

    sensor = read_sensor_file(settings.sensor_data_file)
    rc_driver = read_rc_driver_log(settings.rc_driver_log_dir)

    return {
        "openlab_acquisition_alive": bool(acq_alive),
        "openlab_instrument_service_alive": bool(instsvc_alive),
        "openlab_reverse_proxy_alive": bool(proxy_alive),
        "moses_process_alive": bool(moses_alive),
        "moses_process_pid": moses_pid,
        "last_run_dir": str(last_run_dir) if last_run_dir else None,
        "last_run_mtime_iso8601": last_run_iso,
        "last_run_mtime_epoch": last_run_mtime,
        "acquisition_active": bool(acquisition_active),
        "last_error": last_error,
        "last_error_log_path": last_error_log,
        "last_observation_at": now_iso,
        "probe_error": probe_error,
        # OLSS REST API signals
        "olss_instrument_state": olss.get("olss_instrument_state"),
        "olss_software_status": olss.get("olss_software_status"),
        "olss_current_run": olss.get("olss_current_run"),
        "olss_error": olss.get("olss_error"),
        # Sensor daemon file signals (absent until sensor daemon is deployed)
        **sensor,
        # RC driver log signals (solvent/waste bottle levels from RCDriver.log)
        **rc_driver,
    }
