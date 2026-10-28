"""Per-user usage hours from the OpenLab results tree (usage_stats + GET /stats/usage).

Fixtures are synthesised in the real on-disk shapes: a ``.dx`` is a ZIP holding
``injection.acmd`` (with ``RunDateTime``) whose member timestamps mark when the
archive closed, and an ``.amx`` is a ZIP with ``MethodProperties``. Folder
creation times are never used, so the fixtures do not need to fake them.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agilent_hplcms_server import usage_stats
from agilent_hplcms_server.api import create_app
from agilent_hplcms_server.config import Settings
from agilent_hplcms_server.usage_stats import ESTIMATE_CAP_S, ResultsRootMissing, scan

EDT = timezone(timedelta(hours=-4))
T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=EDT)


@pytest.fixture(autouse=True)
def _fresh_caches():
    usage_stats._dx_cache.clear()
    usage_stats._method_cache.clear()
    usage_stats._report_cache.clear()
    yield


def _dos(moment: datetime) -> tuple[int, int, int, int, int, int]:
    return moment.timetuple()[:6]


def _dx(
    path: Path,
    start: datetime,
    duration_s: float | None,
    *,
    method: str = "",
    fraction: str = "0000000",
) -> Path:
    """Write a .dx whose archive closes ``duration_s`` after ``start``.

    ``duration_s=None`` leaves every member stamped at the start, the shape of
    an archive with no usable close time.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # .NET writes seven fractional digits; the reader must cope.
    run_dt = start.strftime(f"%Y-%m-%dT%H:%M:%S.{fraction}%z")
    run_dt = run_dt[:-2] + ":" + run_dt[-2:]
    acmd = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ACMD xmlns="urn:schemas-agilent-com:acmd20"><InjectionInfo>'
        "<RunOperator>SYSTEM (SYSTEM)</RunOperator>"
        f"<RunDateTime>{run_dt}</RunDateTime>"
        f"<AcquisitionMethod>{method}</AcquisitionMethod>"
        "</InjectionInfo></ACMD>"
    )
    end = start + timedelta(seconds=duration_s or 0)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("[Content_Types].xml", _dos(start)), "<Types/>")
        archive.writestr(zipfile.ZipInfo("injection.acmd", _dos(end)), acmd)
    return path


def _amx(path: Path, runtime_s: float, *, post_time_min: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pump = "<PostTimeMode>PostTime_Off</PostTimeMode>"
    if post_time_min is not None:
        pump = (f"<PostTimeMode>PostTime_Set</PostTimeMode>"
                f"<PostTimeValue>{post_time_min}</PostTimeValue>")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "MethodProperties/MethodProperties",
            f"<MethodProperties><MethodRuntimeInSeconds>{runtime_s}"
            "</MethodRuntimeInSeconds></MethodProperties>",
        )
        archive.writestr("DeviceMethodSettings/AgilentPumpDriver%25231", pump)
    return path


def _users(report) -> dict:
    return {u.user: u for u in report.users}


def test_groups_injections_by_owner_result_set_and_kind(tmp_path: Path):
    results = tmp_path / "Results"
    # One sequence result set holding two injections.
    _dx(results / "Alice/proj.rslt/a-01.dx", T0, 600)
    _dx(results / "Alice/proj.rslt/a-02.dx", T0 + timedelta(minutes=12), 600)
    # Single-injection results of one sequence, side by side in a plain folder.
    _dx(results / "Alice/batch/x-01.sirslt/x-01.dx", T0 + timedelta(hours=1), 300)
    _dx(results / "Alice/batch/x-02.sirslt/x-02.dx", T0 + timedelta(hours=2), 300)
    # A .sirslt directly in the user folder is its own set.
    _dx(results / "Alice/solo.sirslt/solo.dx", T0 + timedelta(hours=3), 1200)
    # Loose .sirslt at the results root: the automation sidecar's.
    _dx(results / "run_A1.sirslt/run_A1.dx", T0 + timedelta(hours=4), 900)
    _dx(results / "agent/temp/r.sirslt/r.dx", T0 + timedelta(hours=5), 900)
    _dx(results / "Shutdown/park.sirslt/park.dx", T0 + timedelta(hours=6), 1800)
    # Derived folders with no raw data are not injections.
    (results / "Alice/export.sirslt").mkdir(parents=True)
    (results / "Alice/export.sirslt/export_DAD1A.cdf").write_bytes(b"cdf")
    (results / "Alice/CSV/x-01.D").mkdir(parents=True)
    (results / "Alice/CSV/x-01.D/DAD1A.ch").write_bytes(b"ch")
    (results / "notes.txt").write_text("not a result")

    report = scan(results)
    users = _users(report)

    assert set(users) == {"Alice", "agent", "Shutdown"}
    alice = users["Alice"]
    assert (alice.kind, alice.injections, alice.result_sets) == ("human", 5, 3)
    assert alice.total_run_hours == round((600 + 600 + 300 + 300 + 1200) / 3600, 2)
    assert (alice.measured_injections, alice.estimated_injections) == (5, 0)
    assert alice.first_run == T0
    assert alice.last_run == T0 + timedelta(hours=3)
    assert (users["agent"].kind, users["agent"].injections) == ("agent", 2)
    assert (users["Shutdown"].kind, users["Shutdown"].injections) == ("system", 1)
    assert report.skipped == {"export_only_sirslt": 1, "chemstation_d": 1}
    assert report.unreadable == []
    assert report.grand_total_hours == round((3000 + 1800 + 1800) / 3600, 2)
    # Sorted by hours, most first.
    assert [u.user for u in report.users] == ["Alice", "agent", "Shutdown"]


def test_root_export_marks_a_run_filed_elsewhere_as_the_agents(tmp_path: Path):
    results = tmp_path / "Results"
    _dx(results / "Han/HH299/HH299-01_A1.sirslt/HH299-01_A1.dx", T0, 600)
    _dx(results / "Han/HH299/HH299-01_B1.sirslt/HH299-01_B1.dx", T0 + timedelta(hours=1), 600)
    (results / "HH299-01_A1.sirslt").mkdir(parents=True)
    (results / "HH299-01_A1.sirslt/HH299-01_A1_DAD1A.cdf").write_bytes(b"cdf")

    report = scan(results)
    users = _users(report)

    assert users["agent"].injections == 1
    assert users["Han"].injections == 1
    agent_run = next(i for i in report.injections if i.user == "agent")
    assert agent_run.result_set == "Han/HH299"
    assert agent_run.injection == "Han/HH299/HH299-01_A1.sirslt/HH299-01_A1.dx"


def test_a_copied_injection_is_counted_once(tmp_path: Path):
    results = tmp_path / "Results"
    _dx(results / "Bob/copy.sirslt/run.dx", T0, 600)
    _dx(results / "Alice/run.sirslt/run.dx", T0, 600)
    _dx(results / "Blanks/run.sirslt/run.dx", T0, 600)

    report = scan(results)

    assert len(report.injections) == 1
    assert report.injections[0].user == "Alice"  # human before system, then path
    assert report.duplicates == [
        ("Bob/copy.sirslt/run.dx", "Alice/run.sirslt/run.dx"),
        ("Blanks/run.sirslt/run.dx", "Alice/run.sirslt/run.dx"),
    ]


def test_duration_falls_back_to_next_start_capped_by_method(tmp_path: Path):
    results = tmp_path / "Results"
    methods = tmp_path / "Methods"
    _amx(methods / "ten_min.amx", 600)
    # The copy beside the data wins over the shared methods folder.
    _amx(results / "Alice/seq.rslt/five_min.amx", 240, post_time_min=1)
    method_path = r"C:\CDSProjects\Installation\Methods\ten_min.amx"

    # No usable close time; the next injection starts 7 min later (< 10 min method).
    _dx(results / "Alice/seq.rslt/a.dx", T0, None, method=method_path)
    # Next injection 1 h later, so the 10-min method run time is the cap.
    _dx(results / "Alice/seq.rslt/b.dx", T0 + timedelta(minutes=7), None, method=method_path)
    # Run time 4 min + 1 min pump post time, method found beside the .dx.
    _dx(results / "Alice/seq.rslt/c.dx", T0 + timedelta(hours=1, minutes=7), None,
        method=r"D:\elsewhere\five_min.amx")
    # A close time implausibly late (a rewritten archive) is not a measurement.
    _dx(results / "Alice/seq.rslt/d.dx", T0 + timedelta(hours=2), 20 * 3600)
    # Last injection, unknown method: the fixed cap.
    _dx(results / "Bob/e.sirslt/e.dx", T0 + timedelta(hours=30), None, method="missing.amx")

    report = scan(results, methods)
    durations = {Path(i.injection).name: (i.duration_s, i.measured) for i in report.injections}

    assert durations["a.dx"] == (7 * 60, False)
    assert durations["b.dx"] == (600, False)
    assert durations["c.dx"] == (300, False)
    assert durations["d.dx"] == (ESTIMATE_CAP_S, False)  # gap 28 h, no method: 2 h cap
    assert durations["e.dx"] == (ESTIMATE_CAP_S, False)
    assert _users(report)["Alice"].estimated_injections == 4


def test_measured_duration_comes_from_the_archive_close(tmp_path: Path):
    results = tmp_path / "Results"
    _dx(results / "Alice/run.sirslt/run.dx", T0, 452, fraction="1234567")
    (only,) = scan(results).injections
    assert only.measured
    assert only.duration_s == 452  # whole seconds, despite the sub-second start
    assert only.start == T0.replace(microsecond=123456)


def test_unreadable_archive_is_reported_not_counted(tmp_path: Path):
    results = tmp_path / "Results"
    bad = results / "Alice/run.sirslt/run.dx"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a zip")
    report = scan(results)
    assert report.injections == []
    assert report.unreadable == ["Alice/run.sirslt/run.dx"]


def test_empty_tree_is_an_all_zero_report(tmp_path: Path):
    results = tmp_path / "Results"
    results.mkdir()
    report = scan(results)
    assert report.users == []
    assert report.grand_total_hours == 0


def test_missing_root_fails_fast(tmp_path: Path):
    with pytest.raises(ResultsRootMissing):
        scan(tmp_path / "nope")
    assert usage_stats.main(["--results-root", str(tmp_path / "nope")]) == 2


def test_cli_prints_table_and_writes_csv(tmp_path: Path, capsys):
    results = tmp_path / "Results"
    _dx(results / "Alice/run.sirslt/run.dx", T0, 600)
    _dx(results / "Bob/run.sirslt/run.dx", T0 + timedelta(hours=1), None)
    out_csv = tmp_path / "usage.csv"

    assert usage_stats.main(["--results-root", str(results), "--csv", str(out_csv)]) == 0

    printed = capsys.readouterr().out
    assert "Alice" in printed and "Grand total" in printed
    rows = out_csv.read_text(encoding="utf-8").splitlines()
    assert rows[0] == ("user,kind,result_set,injection,start,duration_s,"
                       "duration_source,method,operator")
    assert rows[1].startswith("Alice,human,run.sirslt,Alice/run.sirslt/run.dx,")
    assert ",600,measured," in rows[1]
    assert ",estimated," in rows[2]


def _client(results_root: Path) -> TestClient:
    settings = Settings(
        cds_results_dir=str(results_root),
        consumable_ack_file="",  # in-memory ack stores — never touch the real files
        lc_fault_ack_file="",
    )
    return TestClient(create_app(settings=settings, reader=lambda _s: {}))


def test_stats_usage_endpoint(tmp_path: Path):
    results = tmp_path / "Results"
    _dx(results / "Alice/run.sirslt/run.dx", T0, 3600)
    client = _client(results)

    response = client.get("/stats/usage")  # no X-Claim-Token: read-only

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"generated_at", "results_root", "users", "grand_total_hours", "note"}
    assert body["results_root"] == str(results)
    assert body["grand_total_hours"] == 1.0
    (alice,) = body["users"]
    assert alice == {
        "user": "Alice", "kind": "human", "injections": 1, "result_sets": 1,
        "total_run_hours": 1.0,
        "first_run": "2026-10-01T13:00:00Z",
        "last_run": "2026-10-01T13:00:00Z",
        "measured_injections": 1, "estimated_injections": 0,
    }

    # Served from the short cache until a refresh is asked for.
    _dx(results / "Bob/run.sirslt/run.dx", T0 + timedelta(hours=2), 600)
    assert len(client.get("/stats/usage").json()["users"]) == 1
    assert len(client.get("/stats/usage?refresh=1").json()["users"]) == 2


def test_stats_usage_missing_root_is_a_503_envelope(tmp_path: Path):
    client = _client(tmp_path / "missing")

    response = client.get("/stats/usage")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error"] == "results_root_missing"
    assert detail["results_root"] == str(tmp_path / "missing")
    schema = client.get("/openapi.json").json()
    documented = schema["paths"]["/stats/usage"]["get"]["responses"]["503"]
    assert "detail" in documented["content"]["application/json"]["schema"]["properties"]
