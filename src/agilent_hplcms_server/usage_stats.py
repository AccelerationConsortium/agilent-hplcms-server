"""Per-user instrument usage, reconstructed from the OpenLab CDS results tree.

OpenLab files every acquisition under ``Results\\<user>\\...``, so the tree is
already a complete usage log — it only has to be read back. Read-only: nothing
here writes, moves or touches anything under the results root.

What counts as one injection
----------------------------
One ``.dx`` archive. It is the raw acquisition (an OPC ZIP holding the signal
traces and ``injection.acmd``), written once per injection into either a
sequence result set (``*.rslt``) or a single-injection result (``*.sirslt``).
Everything else in the tree is derived from a ``.dx`` and is deliberately not
counted: a ``*.sirslt`` with no ``.dx`` is an export (CDF/CSV) or an
acquisition that never wrote data, and a ChemStation ``*.D`` folder is an
export of a run whose ``.dx`` sits elsewhere. Counting those would bill the
same instrument time twice. They are tallied in ``UsageReport.skipped``.

Timing
------
Folder CreationTime is unusable here: results trees have been copied between
projects (whole user folders carry one creation instant months after the runs),
so every time comes from inside the archive, where a copy preserves it:

* start — ``RunDateTime`` in ``injection.acmd`` (ISO 8601 with offset).
* measured end — the newest member timestamp in the ``.dx`` ZIP. The archive
  is closed when acquisition stops; on this instrument the next injection's
  ``RunDateTime`` lands within seconds of it.
* estimated end (when the measured one is missing or implausible) — the next
  injection's start on the instrument, capped at the method run time (the
  ``.amx`` ``MethodRuntimeInSeconds`` plus any pump post time), else at
  ``ESTIMATE_CAP_S``. "Next" is instrument-wide, not per result set: the
  instrument runs one injection at a time, so no run can outlast the next
  injection anywhere in the tree.

An injection copied into several folders shares its ``RunDateTime`` to the
sub-microsecond; such copies are counted once (``UsageReport.duplicates``),
attributed to the first owner in ``_owner_preference`` order. ``RunOperator``
cannot attribute a run: every injection on this instrument records
``SYSTEM (SYSTEM)``.

Owners
------
The top-level folder names the owner. Loose ``*.sirslt`` at the results root
come from this repo's automation sidecar, and ``Results\\agent`` is its own
folder; both are the ``agent`` user. A root ``*.sirslt`` is usually an export
whose ``.dx`` was filed under the requesting user's folder, so a run whose
``*.sirslt`` name matches a root one is the agent's wherever its ``.dx`` sits.
``SYSTEM_FOLDERS`` are maintenance (blanks, shutdown/standby parking, smoke
tests, installation checkout).
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import re
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Literal, TextIO

from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_RESULTS_ROOT = r"C:\CDSProjects\Installation\Results"

AGENT_USER = "agent"
SYSTEM_FOLDERS = frozenset(
    name.casefold()
    for name in ("Blanks", "Shutdown", "SmokeTest", "standby", "Installation Checkout")
)

# Fallback cap for an estimated duration when no method run time is known.
ESTIMATE_CAP_S: float = 2 * 3600.0
# A measured duration longer than this is a rewritten archive, not a run.
MEASURED_MAX_S: float = 12 * 3600.0

Kind = Literal["human", "agent", "system"]

_RUN_DATETIME = re.compile(rb"<RunDateTime>([^<]+)</RunDateTime>")
_ACQ_METHOD = re.compile(rb"<AcquisitionMethod>([^<]*)</AcquisitionMethod>")
_RUN_OPERATOR = re.compile(rb"<RunOperator>([^<]*)</RunOperator>")
_METHOD_RUNTIME = re.compile(rb"<MethodRuntimeInSeconds>([^<]+)</MethodRuntimeInSeconds>")
_POST_TIME_SET = re.compile(rb"<PostTimeMode>PostTime_Set</PostTimeMode>")
_POST_TIME_VALUE = re.compile(rb"<PostTimeValue>([^<]+)</PostTimeValue>")
# .NET writes 7 fractional digits; datetime.fromisoformat accepts at most 6.
_FRACTION = re.compile(r"(\.\d{6})\d+")


class ResultsRootMissing(FileNotFoundError):
    """The configured results root does not exist or is not a directory."""


@dataclass(frozen=True)
class Injection:
    user: str
    kind: Kind
    result_set: str      # relative to the owner's folder (the results root for an
                         # agent run filed under a person's folder)
    injection: str       # .dx path relative to the results root
    start: datetime      # timezone-aware
    duration_s: float
    measured: bool
    method: str
    operator: str


@dataclass(frozen=True)
class UserUsage:
    user: str
    kind: Kind
    injections: int
    result_sets: int
    total_run_hours: float
    first_run: datetime | None
    last_run: datetime | None
    measured_injections: int
    estimated_injections: int


@dataclass
class UsageReport:
    generated_at: datetime
    results_root: str
    users: list[UserUsage]
    injections: list[Injection]
    grand_total_hours: float
    # (dropped copy, counted copy), .dx paths relative to the results root
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)
    walk_s: float = 0.0


@dataclass(frozen=True)
class _DxInfo:
    start: datetime
    end: datetime | None
    method: str
    operator: str


# Parsed archives keyed on file identity, so a rewritten .dx re-parses. Only the
# few timestamps are kept; it is what makes a repeat scan cost a stat per file.
_dx_cache: dict[tuple[str, int, int], _DxInfo | None] = {}
_method_cache: dict[tuple[str, int, int], float | None] = {}


def _parse_run_datetime(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_FRACTION.sub(r"\1", raw.strip()))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _read_dx(path: Path) -> _DxInfo | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _dx_cache:
        return _dx_cache[key]

    info: _DxInfo | None = None
    try:
        with zipfile.ZipFile(path) as archive:
            acmd = archive.read("injection.acmd")
            members = archive.infolist()
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        logger.debug("unreadable .dx %s: %s", path, exc)
    else:
        match = _RUN_DATETIME.search(acmd)
        start = _parse_run_datetime(match.group(1).decode("ascii", "replace")) if match else None
        if start is not None:
            end: datetime | None = None
            if members:
                # ZIP member times are local wall-clock (DOS) times with no zone;
                # the run's own offset is the zone they were written in.
                end = datetime(*max(m.date_time for m in members), tzinfo=start.tzinfo)
            method = _ACQ_METHOD.search(acmd)
            operator = _RUN_OPERATOR.search(acmd)
            info = _DxInfo(
                start=start,
                end=end,
                method=method.group(1).decode("utf-8", "replace") if method else "",
                operator=operator.group(1).decode("utf-8", "replace") if operator else "",
            )
    _dx_cache[key] = info
    return info


def _read_method_runtime(path: Path) -> float | None:
    """Run time of an ``.amx`` in seconds, including pump post time, or None."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _method_cache:
        return _method_cache[key]

    runtime: float | None = None
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            props = archive.read("MethodProperties/MethodProperties")
            pump = next(
                (archive.read(n) for n in names
                 if "PumpDriver" in n and not n.endswith(".chk")),
                b"",
            )
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        logger.debug("unreadable .amx %s: %s", path, exc)
    else:
        match = _METHOD_RUNTIME.search(props)
        try:
            runtime = float(match.group(1)) if match else None
            if runtime is not None and _POST_TIME_SET.search(pump):
                post = _POST_TIME_VALUE.search(pump)
                if post:
                    runtime += float(post.group(1)) * 60.0
        except ValueError:
            runtime = None
        if runtime is not None and runtime <= 0:
            runtime = None
    _method_cache[key] = runtime
    return runtime


def _method_runtime_for(method: str, dx_path: Path, methods_root: Path | None) -> float | None:
    """Resolve the method an injection ran with to an ``.amx`` and read it.

    The archived path is the copy OpenLab placed beside the data at run time; a
    results tree that has since moved keeps that copy next to the ``.dx``, so
    the same file name is tried there, then in the shared methods folder.
    """
    if not method:
        return None
    name = PureWindowsPath(method).name
    candidates = [Path(method), dx_path.parent / name]
    if methods_root is not None:
        candidates.append(methods_root / name)
    for candidate in candidates:
        runtime = _read_method_runtime(candidate)
        if runtime is not None:
            return runtime
    return None


def _kind(user: str) -> Kind:
    if user == AGENT_USER:
        return "agent"
    if user.casefold() in SYSTEM_FOLDERS:
        return "system"
    return "human"


@dataclass(frozen=True)
class _Found:
    user: str
    user_root: Path
    dx_path: Path
    info: _DxInfo


def _owner_preference(item: _Found) -> tuple[int, str]:
    # A human's copy wins over the agent's, which wins over a maintenance copy.
    return ({"human": 0, "agent": 1, "system": 2}[_kind(item.user)], item.dx_path.as_posix())


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _result_set(dx_path: Path, user_root: Path) -> str:
    """The group an injection was acquired in, relative to its user folder.

    A ``*.rslt`` is a sequence result set. A ``*.sirslt`` is one injection, and
    single-injection runs of one sequence sit side by side in a plain folder, so
    that folder is their set — unless it is the user folder itself, where each
    ``*.sirslt`` stands alone.
    """
    container = dx_path.parent
    if container.suffix.lower() == ".sirslt" and container.parent != user_root:
        container = container.parent
    try:
        return container.relative_to(user_root).as_posix() or "."
    except ValueError:
        return container.name


def scan(
    results_root: str | Path = DEFAULT_RESULTS_ROOT,
    methods_root: str | Path | None = None,
) -> UsageReport:
    """Walk the results tree and aggregate instrument time per owner.

    ``methods_root`` defaults to the ``Methods`` folder beside the results root.
    Raises ``ResultsRootMissing`` if the results root is not a directory.
    """
    t0 = time.monotonic()
    root = Path(results_root)
    if not root.is_dir():
        raise ResultsRootMissing(f"results root not found: {root}")
    methods = Path(methods_root) if methods_root is not None else root.parent / "Methods"

    found: list[_Found] = []
    agent_exports: set[str] = set()
    skipped = {"export_only_sirslt": 0, "chemstation_d": 0}
    unreadable: list[str] = []

    def on_error(exc: OSError) -> None:
        unreadable.append(str(exc.filename or exc))

    for top in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if not top.is_dir():
            continue
        is_root_sirslt = top.suffix.lower() == ".sirslt"
        user = AGENT_USER if is_root_sirslt else top.name
        user_root = root if is_root_sirslt else top
        if is_root_sirslt:
            agent_exports.add(top.name.casefold())
        for current, dirs, files in os.walk(top, onerror=on_error):
            current_path = Path(current)
            suffix = current_path.suffix.lower()
            if suffix == ".d":
                skipped["chemstation_d"] += 1
                dirs.clear()
                continue
            dx_files = [f for f in files if f.lower().endswith(".dx")]
            if suffix == ".sirslt" and not dx_files:
                skipped["export_only_sirslt"] += 1
            for name in dx_files:
                dx_path = current_path / name
                info = _read_dx(dx_path)
                if info is None:
                    unreadable.append(_rel(dx_path, root))
                    continue
                found.append(_Found(user, user_root, dx_path, info))
            if suffix == ".sirslt":
                dirs.clear()  # a result payload holds no further results

    # The sidecar leaves its export of a run at the results root while the run's
    # own .dx is filed under the requester's folder; the export marks the run
    # as the agent's. Its result set keeps the folder it was filed in.
    found = [
        replace(item, user=AGENT_USER, user_root=root)
        if item.dx_path.parent.suffix.lower() == ".sirslt"
        and item.dx_path.parent.name.casefold() in agent_exports
        else item
        for item in found
    ]

    # One acquisition, however many folders it was copied into.
    by_start: dict[datetime, _Found] = {}
    duplicates: list[tuple[str, str]] = []
    for item in sorted(found, key=_owner_preference):
        kept = by_start.get(item.info.start)
        if kept is not None:
            duplicates.append((_rel(item.dx_path, root), _rel(kept.dx_path, root)))
            continue
        by_start[item.info.start] = item

    ordered = sorted(by_start.values(), key=lambda item: item.info.start)
    injections: list[Injection] = []
    for index, item in enumerate(ordered):
        info = item.info
        next_start = ordered[index + 1].info.start if index + 1 < len(ordered) else None
        duration: float | None = None
        if info.end is not None:
            # Whole seconds: the close time is a DOS stamp (2 s resolution).
            measured = round((info.end - info.start).total_seconds())
            if 0 < measured <= MEASURED_MAX_S:
                duration = measured
        is_measured = duration is not None
        if duration is None:
            cap = _method_runtime_for(info.method, item.dx_path, methods) or ESTIMATE_CAP_S
            gap = (next_start - info.start).total_seconds() if next_start else cap
            duration = max(0.0, min(gap, cap))
        injections.append(
            Injection(
                user=item.user,
                kind=_kind(item.user),
                result_set=_result_set(item.dx_path, item.user_root),
                injection=_rel(item.dx_path, root),
                start=info.start,
                duration_s=duration,
                measured=is_measured,
                method=PureWindowsPath(info.method).name if info.method else "",
                operator=info.operator,
            )
        )

    users = _aggregate(injections)
    return UsageReport(
        generated_at=datetime.now(timezone.utc),
        results_root=str(root),
        users=users,
        injections=injections,
        grand_total_hours=round(sum(i.duration_s for i in injections) / 3600.0, 2),
        duplicates=duplicates,
        skipped=skipped,
        unreadable=unreadable,
        walk_s=round(time.monotonic() - t0, 3),
    )


def _aggregate(injections: list[Injection]) -> list[UserUsage]:
    groups: dict[str, list[Injection]] = {}
    for injection in injections:
        groups.setdefault(injection.user, []).append(injection)
    users = [
        UserUsage(
            user=user,
            kind=items[0].kind,
            injections=len(items),
            result_sets=len({i.result_set for i in items}),
            total_run_hours=round(sum(i.duration_s for i in items) / 3600.0, 2),
            first_run=min(i.start for i in items),
            last_run=max(i.start for i in items),
            measured_injections=sum(i.measured for i in items),
            estimated_injections=sum(not i.measured for i in items),
        )
        for user, items in groups.items()
    ]
    users.sort(key=lambda u: (-u.total_run_hours, u.user.casefold()))
    return users


# A warm scan still costs a walk plus a stat per .dx (~2 s on the real tree),
# so GET /stats/usage serves one report for REPORT_TTL_S unless asked to refresh.
# The lock keeps concurrent cold requests from walking the tree twice.
REPORT_TTL_S: float = 60.0
_report_cache: dict[tuple[str, str | None], tuple[float, UsageReport]] = {}
_report_lock = threading.Lock()


def cached_scan(
    results_root: str | Path = DEFAULT_RESULTS_ROOT,
    methods_root: str | Path | None = None,
    *,
    refresh: bool = False,
) -> UsageReport:
    key = (str(results_root), None if methods_root is None else str(methods_root))
    with _report_lock:
        cached = _report_cache.get(key)
        if not refresh and cached is not None and time.monotonic() - cached[0] < REPORT_TTL_S:
            return cached[1]
        report = scan(results_root, methods_root)
        _report_cache[key] = (time.monotonic(), report)
        return report


class UserUsageModel(BaseModel):
    user: str
    kind: Kind
    injections: int
    result_sets: int
    total_run_hours: float
    first_run: datetime | None
    last_run: datetime | None
    measured_injections: int
    estimated_injections: int


class UsageStatsResponse(BaseModel):
    generated_at: datetime
    results_root: str
    users: list[UserUsageModel]
    grand_total_hours: float
    note: str


class ResultsRootMissingError(BaseModel):
    """Body for HTTP 503 when the OpenLab results root is not on disk — a
    misconfigured ``CDS_RESULTS_DIR`` or an unmounted drive, never an empty
    tree (which is a valid all-zero report)."""

    error: Literal["results_root_missing"] = "results_root_missing"
    detail: str
    results_root: str


def to_response(report: UsageReport) -> UsageStatsResponse:
    def utc(value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value is not None else None

    measured = sum(i.measured for i in report.injections)
    skipped = report.skipped
    note = (
        f"{len(report.injections)} injections, one per .dx raw-data archive. "
        f"Duration measured (RunDateTime to archive close) for {measured}, "
        f"estimated for {len(report.injections) - measured} (gap to the next "
        f"injection, capped at the method run time or "
        f"{ESTIMATE_CAP_S / 3600:g} h). {len(report.duplicates)} copies of the same "
        f"injection counted once; {skipped.get('export_only_sirslt', 0)} .sirslt "
        f"exports and {skipped.get('chemstation_d', 0)} ChemStation .D folders "
        f"without raw data not counted; {len(report.unreadable)} unreadable. "
        f"Root-level .sirslt runs are attributed to the agent."
    )
    return UsageStatsResponse(
        generated_at=report.generated_at,
        results_root=report.results_root,
        users=[
            UserUsageModel(
                user=u.user,
                kind=u.kind,
                injections=u.injections,
                result_sets=u.result_sets,
                total_run_hours=u.total_run_hours,
                first_run=utc(u.first_run),
                last_run=utc(u.last_run),
                measured_injections=u.measured_injections,
                estimated_injections=u.estimated_injections,
            )
            for u in report.users
        ],
        grand_total_hours=report.grand_total_hours,
        note=note,
    )


def write_csv(report: UsageReport, out: TextIO) -> None:
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        ["user", "kind", "result_set", "injection", "start", "duration_s",
         "duration_source", "method", "operator"]
    )
    for i in report.injections:
        writer.writerow(
            [i.user, i.kind, i.result_set, i.injection, i.start.isoformat(),
             f"{i.duration_s:.0f}", "measured" if i.measured else "estimated",
             i.method, i.operator]
        )


def format_table(report: UsageReport) -> str:
    out = io.StringIO()
    header = (f"{'user':<22} {'kind':<6} {'inj':>5} {'sets':>5} {'hours':>8} "
              f"{'first run':<10} {'last run':<10} {'meas':>5} {'est':>4}")

    def rows(kind_filter: set[str]) -> None:
        print(header, file=out)
        print("-" * len(header), file=out)
        for u in report.users:
            if u.kind not in kind_filter:
                continue
            first = u.first_run.date().isoformat() if u.first_run else ""
            last = u.last_run.date().isoformat() if u.last_run else ""
            print(f"{u.user:<22} {u.kind:<6} {u.injections:>5} {u.result_sets:>5} "
                  f"{u.total_run_hours:>8.2f} {first:<10} {last:<10} "
                  f"{u.measured_injections:>5} {u.estimated_injections:>4}", file=out)

    rows({"human", "agent"})
    print("\nMaintenance / system folders", file=out)
    rows({"system"})
    print(f"\nGrand total: {report.grand_total_hours:.2f} h over "
          f"{len(report.injections)} injections "
          f"({len(report.duplicates)} duplicate copies counted once)", file=out)
    skipped = ", ".join(f"{k}={v}" for k, v in report.skipped.items())
    print(f"Not counted (no .dx): {skipped}; unreadable: {len(report.unreadable)}", file=out)
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    """Console entry point: print the per-user table and optionally a CSV."""
    from .config import load_settings

    parser = argparse.ArgumentParser(
        prog="usage-stats",
        description="Per-user UPLC-MS usage hours from the OpenLab CDS results tree (read-only).",
    )
    parser.add_argument("--results-root", default=load_settings().cds_results_dir)
    parser.add_argument("--methods-root", default=None,
                        help="default: the Methods folder beside the results root")
    parser.add_argument("--csv", metavar="PATH",
                        help="write the per-injection CSV here ('-' for stdout)")
    args = parser.parse_args(argv)

    try:
        report = scan(args.results_root, args.methods_root)
    except ResultsRootMissing as exc:
        print(f"usage-stats: {exc}", file=sys.stderr)
        return 2
    print(format_table(report))
    if report.unreadable:
        print("Unreadable:", *report.unreadable[:20], sep="\n  ")
    if report.duplicates:
        print("Copies counted once (dropped <- counted):",
              *(f"{d} <- {k}" for d, k in report.duplicates), sep="\n  ")
    if args.csv == "-":
        write_csv(report, sys.stdout)
    elif args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as handle:
            write_csv(report, handle)
        print(f"CSV: {Path(args.csv).resolve()}")
    print(f"scan took {report.walk_s:.2f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
