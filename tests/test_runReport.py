"""Tests for the run report (``runReport``): how images and visits are judged, the page, and its commands."""

import importlib.util
import json
import re
import types
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pfs.qa.dashboard import runReport
from pfs.qa.dashboard.qadbSource import writeSnapshot
from pfs.qa.dashboard.runReport import Limits, ReportCache, buildReport, grade, judge, measure, modeOffsets

_OH_R1 = runReport.kmsPerPixel().set_index(["camera", "species"]).loc[("r1", "OH"), "kmsPerPixel"]


def _cli():
    path = Path(__file__).parents[1] / "bin" / "qaDashboard.py"
    spec = importlib.util.spec_from_file_location("qaDashboard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tables(*images):
    """Return a qadb read's three tables for ``images``.

    Each image is ``(visit, camera, obsType, measures)`` and options: ``measures`` maps
    ``(reference, species, metric)`` to a value; ``iq`` and ``dm`` are the tasks' stored
    verdicts as ``(status, reason)``; ``cameraSet`` and ``illumination`` as stored.
    """
    iq, dm, metrics = [], [], []
    for visit, camera, obsType, measures, *rest in images:
        options = rest[0] if rest else {}
        key = {"visit": visit, "arm": camera[0], "spectrograph": int(camera[1])}
        iqStatus, iqReason = options.get("iq", ("PASS", ""))
        dmStatus, dmReason = options.get("dm", ("PASS", ""))
        seqName = {"arc": "Arc: Neon", "trace": "Trace"}.get(obsType, "")
        iq.append(
            {
                **key,
                "obsType": obsType,
                "seqName": seqName,
                "illumination": options.get("illumination", "full"),
                "qaStatus": iqStatus,
                "qaReason": iqReason,
            }
        )
        dm.append(
            {
                **key,
                "obsType": obsType,
                "cameraSet": options.get("cameraSet", "brn"),
                "qaStatus": dmStatus,
                "qaReason": dmReason,
            }
        )
        for (reference, species, metric), value in measures.items():
            statusType = "RESERVED" if reference == "visit" else "BOTH"
            metrics.append(
                {
                    **key,
                    "reference": reference,
                    "description": species,
                    "statusType": statusType,
                    "metric": metric,
                    "value": value,
                }
            )
    columns = ["visit", "arm", "spectrograph", "reference", "description", "statusType", "metric", "value"]
    return {
        "iqQaMetrics": pd.DataFrame(iq),
        "dmLineResidualSummary": pd.DataFrame(dm),
        "dmLineResidualMetrics": pd.DataFrame(metrics, columns=columns),
    }


def _science(x=0.0, y=0.0, heldBack=0.0, species="OH"):
    """Return the measures of a science image: its offsets from the calibration and its held-back residual (px)."""
    return {
        ("calib", "Trace", "medianDeltaX"): x,
        ("calib", species, "medianDeltaY"): y,
        ("visit", species, "medianDeltaY"): heldBack,
    }


def _status(tables, limits=None, constants=None):
    judged = judge(measure(tables, constants), limits)
    return dict(zip(zip(judged["visit"], judged["camera"], strict=True), judged["status"], strict=True))


def testAValueAtALimitHasCrossedIt():
    values = pd.Series([0.2, 0.5, -0.5, 1.0, -3.0, np.nan])
    assert grade(values, 0.5, 1.0).tolist() == ["PASS", "WARN", "WARN", "FAIL", "FAIL", ""]
    # Negative control: just inside a limit has not crossed it.
    assert grade(pd.Series([0.4999, 0.9999]), 0.5, 1.0).tolist() == ["PASS", "WARN"]


def testScienceIsJudgedOnWhatWasMeasuredNotOnTheStoredVerdicts():
    stored = {"iq": ("FAIL", "medFWHM=3.6px >= fail threshold 3.5px")}
    tables = _tables(
        (1, "r1", "science", _science(x=0.2, y=-0.6), stored),
        (2, "r1", "science", _science(x=-1.0, y=0.1), stored),
        (3, "r1", "science", _science(x=0.1, y=0.1), stored),
        (4, "r1", "arc", {}, stored),
    )
    judged = judge(measure(tables)).set_index("visit")
    assert judged["status"].to_dict() == {1: "WARN", 2: "FAIL", 3: "PASS", 4: "FAIL"}
    assert judged.loc[1, "reason"] == "y offset -0.60 px, limit 0.5"
    assert judged.loc[2, "reason"] == "x offset -1.00 px, limit 1"
    # Negative control: the stored width verdict is what an arc keeps, and what a science frame does not.
    assert "medFWHM" in judged.loc[4, "reason"] and "medFWHM" not in judged.loc[2, "reason"]


def testTheYOffsetIsTheSpeciesFurthestOff():
    measures = {("calib", "OH", "medianDeltaY"): 0.1, ("calib", "O2", "medianDeltaY"): -0.7}
    image = measure(_tables((1, "r1", "science", measures))).iloc[0]
    assert image["yOffset"] == pytest.approx(-0.7)
    # Negative control: it is the largest in size, not the largest signed value.
    assert image["yOffset"] != pytest.approx(0.1)


def testTheResidualIsInKmPerSecondOnTheArmsBestSpecies():
    warnPixels = 0.5 / _OH_R1  # the r arm's WARN limit, in pixels at r1's OH lines
    tables = _tables(
        (1, "r1", "science", _science(heldBack=1.2 * warnPixels)),
        (2, "r1", "science", _science(heldBack=-1.2 * warnPixels)),
        (3, "b1", "science", _science(heldBack=1.2 * warnPixels, species="OI")),
        (4, "r1", "science", {**_science(), ("visit", "O2", "medianDeltaY"): 5.0}),
    )
    judged = judge(measure(tables)).set_index("visit")
    assert judged.loc[1, "kms"] == pytest.approx(0.6)
    assert judged["status"].to_dict() == {1: "WARN", 2: "WARN", 3: "PASS", 4: "PASS"}
    assert judged.loc[1, "reason"] == "OH residual +0.60 km/s, limit 0.5"
    # Negative control: the same residual read as pixels would be far inside the limit.
    assert abs(1.2 * warnPixels) < 0.5 <= abs(judged.loc[1, "kms"])
    # Negative control: the limits are arguments; looser ones pass the same image.
    looser = Limits(kms={**runReport.DEFAULT_KMS, "r": (1.0, 2.0)})
    assert _status(tables, looser)[(1, "r1")] == "PASS"


def _twoModes(nWithM=3):
    """Return quartz and Neon arcs in both red-arm modes, and science visits read with m and with r."""
    quartz = [
        (100 + i, "b1", "trace", {("calib", "Trace", "medianDeltaX"): 0.02}, {"cameraSet": "brn"})
        for i in range(3)
    ]
    quartz += [
        (110 + i, "b1", "trace", {("calib", "Trace", "medianDeltaX"): -0.48}, {"cameraSet": "bmn"})
        for i in range(nWithM)
    ]
    neon = [
        (120 + i, "n1", "arc", {("calib", "NeI", "medianDeltaY"): 0.0}, {"cameraSet": "brn"})
        for i in range(3)
    ]
    neon += [
        (130 + i, "n1", "arc", {("calib", "NeI", "medianDeltaY"): -0.12}, {"cameraSet": "bmn"})
        for i in range(nWithM)
    ]
    science = [
        (200, "b1", "science", _science(x=-0.55, species="OI"), {"cameraSet": "bmn"}),
        (200, "n1", "science", _science(y=-0.58), {"cameraSet": "bmn"}),
        (200, "m1", "science", _science(), {"cameraSet": "bmn"}),
        (201, "b1", "science", _science(x=-0.55, species="OI"), {"cameraSet": "brn"}),
    ]
    return _tables(*quartz, *neon, *science)


def testVisitsReadWithMAreMeasuredFromWhereTheDetectorsSitInThatMode():
    tables = _twoModes()
    constants = modeOffsets(tables).set_index(["camera", "axis"])
    assert constants["constant"].to_dict() == pytest.approx({("b1", "x"): -0.5, ("n1", "y"): -0.12})
    assert constants.loc[("b1", "x"), ["withM", "withR"]].tolist() == [3, 3]

    corrected = judge(measure(tables, modeOffsets(tables))).set_index(["visit", "camera"])
    assert corrected.loc[(200, "b1"), "xOffset"] == pytest.approx(-0.05)
    assert corrected.loc[(200, "n1"), "yOffset"] == pytest.approx(-0.46)
    assert corrected.loc[[(200, "b1"), (200, "n1")], "status"].tolist() == ["PASS", "PASS"]
    assert corrected.loc[(200, "b1"), "corrected"] and not corrected.loc[(201, "b1"), "corrected"]
    # The visit read with r is judged against the calibration as it is.
    assert corrected.loc[(201, "b1"), "status"] == "WARN"
    # Negative control: without the constants the same m-arm visit is flagged.
    asMeasured = _status(tables)
    assert asMeasured[(200, "b1")] == "WARN" and asMeasured[(200, "n1")] == "WARN"
    # Negative control: adding the constant instead of subtracting it moves the image further off.
    flipped = modeOffsets(tables).assign(constant=lambda frame: -frame["constant"])
    assert _status(tables, constants=flipped)[(200, "b1")] == "FAIL"


def testADetectorNeedsEnoughVisitsInBothModesForAConstant():
    assert modeOffsets(_twoModes(nWithM=2)).empty
    # Negative control: one more visit with m and there is a constant.
    assert len(modeOffsets(_twoModes(nWithM=3))) == 2


def testAVisitTakesItsWorstDetectorAndTheEveningsDate():
    tables = _tables(
        (1, "b1", "science", _science(species="OI")),
        (1, "r1", "science", _science(x=1.5)),
        (2, "r1", "allsky", {}, {"iq": ("FAIL", "width"), "dm": ("PASS", "")}),
        (3, "n1", "arc", {}, {"dm": ("WARN", "calib NeI: |medianDeltaY|=1.5px >= warn threshold 1px")}),
        (4, "r1", "unknown", {}, {"illumination": "iis"}),
    )
    info = pd.DataFrame(
        {
            "visit": [1, 2, 3, 4],
            "time": pd.to_datetime(["2026-11-04 02:00", "2026-11-04 13:00", "2026-11-04 19:00", pd.NaT]),
            "exptime": [900.0, 30.0, 5.0, np.nan],
            "sequenceType": ["scienceObject", None, "scienceArc", "dotRoach"],
        }
    )
    visits = runReport.visitTable(judge(measure(tables)), info).set_index("visit")
    assert visits["status"].to_dict() == {1: "FAIL", 2: "PASS", 3: "WARN", 4: "PASS"}
    assert visits.loc[1, ["flagged", "detectorsStatus", "arms"]].tolist() == ["r1 F", "b1P r1F", "br"]
    assert visits["label"].to_dict() == {
        1: "science",
        2: "all-sky",
        3: "arc, Neon",
        4: "unclassified (dot roach)",
    }
    assert visits.loc[3, "why"] == "n1: calib NeI: |offset Y|=1.5px >= warn limit 1px"
    # A night is the HST date of its evening: 02:00 belongs to the evening before.
    assert visits["night"].to_dict() == {1: "2026-11-03", 2: "2026-11-04", 3: "2026-11-04", 4: ""}
    # Negative control: the calendar date of the exposure is not the night.
    assert visits.loc[1, "hst"][:10] != visits.loc[1, "night"]


def testThePageStandsAlone():
    tables = _twoModes()
    tables["dmLineResidualSummary"].loc[0, ["qaStatus", "qaReason"]] = ["FAIL", "x <script> & y"]
    info = pd.DataFrame(
        {"visit": [200, 201], "time": pd.to_datetime(["2026-11-04 20:00", "2026-11-04 21:00"])}
    )
    made = buildReport(tables, info=info, title="Run99 <verdicts>", now=datetime(2026, 11, 5))
    page = made.html
    assert not re.findall(r"\{\{[A-Z_]+\}\}", page)
    assert page.startswith("<!doctype html>") and "<title>Run99 &lt;verdicts&gt;</title>" in page
    # The calendar can be laid out in the order taken or by time.
    assert 'data-group="layout"' in page and "const minutesOf" in page
    # The kinds of frame shown are boxes ticked on their own, with no control for all of them at once.
    assert page.count("data-show=") == 3 and 'data-group="show"' not in page
    # Engineering-fiber frames start unticked; science and the full-slit calibrations start ticked.
    assert (
        'data-show="eng">' in page
        and 'data-show="science" checked>' in page
        and 'data-show="cal" checked>' in page
    )
    # Its fonts travel inside it.
    assert page.count("@font-face") == 2 and "url(data:font/woff2;base64," in page
    # Nothing is fetched: no stylesheet, script or image comes from elsewhere.
    assert not re.findall(r"""(?:src|href)=["'](?:https?:)?//""", page) and "url(http" not in page
    assert "judged 2026-11-05" in page and "4 November to 4 November 2026 (HST)" in page
    data = json.loads(re.search(r"const DATA = (\[.*\]);\n", page).group(1))
    assert [row[0] for row in data] == made.visits["visit"].tolist()
    # qadb's text is escaped wherever it lands: in the table and in the script.
    assert "<script> & y" not in page and "x &lt;script&gt; &amp; y" in page
    # The appendix names what was subtracted, and the offsets section points to it.
    assert "id='appendix-m-arm'" in page and "href='#appendix-m-arm'" in page
    # Negative control: with nothing subtracted there is no appendix, and no pointer to one.
    plain = buildReport(tables, info=info, correctModes=False).html
    assert "appendix-m-arm" not in plain and plain.count("†") == page.count("†") - 1


def testTheLimitsTableMarksTheReportsOwnSet():
    tables = _twoModes()
    page = buildReport(tables).html
    assert "three sets of km/s limits" in page and page.count('<tr class="own">') == 1
    # A set that is none of the tabulated ones gets a row of its own.
    other = buildReport(tables, limits=Limits(kms={**runReport.DEFAULT_KMS, "b": (3.0, 4.0)})).html
    assert "four sets of km/s limits" in other and other.count('<tr class="own">') == 1


class _Store:
    """A stand-in for `live.QadbStore`: its raw tables and visit times, and a version `refresh` may raise."""

    def __init__(self, tables):
        self.raw = tables
        self.version = 0
        self.refreshed = 0
        self.times = pd.DataFrame({"visit": [200], "time": [pd.Timestamp("2026-11-04 20:00")]})

    def refresh(self):
        self.refreshed += 1
        return False


def testTheCachedPageIsMadeAgainOnlyWhenTheStoreChanges(monkeypatch):
    built = []
    real = runReport.buildReport
    monkeypatch.setattr(runReport, "buildReport", lambda *a, **k: built.append(1) or real(*a, **k))
    store = _Store(_twoModes())
    cache = ReportCache(store, title="Live")
    first = cache.html()
    assert cache.html() == first and len(built) == 1 and store.refreshed == 2
    assert "2026-11-04 20:00" in first  # the store's own visit times, without the opdb
    store.raw = _tables((300, "r1", "science", _science(x=2.0)))
    store.version += 1
    assert "[300," in cache.html() and len(built) == 2
    # Negative control: the page did change with the store.
    assert "[300," not in first


def testAStoreWithNoImageYetStillHasAPage():
    store = _Store({})
    store.times = store.times.iloc[:0]
    page = ReportCache(store, readInfo=lambda first, last: pytest.fail("no visit to read")).html()
    assert "const DATA = [];" in page and "0 visits" in page


def _snapshot(directory):
    tables = _tables(
        (1, "r1", "science", _science(heldBack=0.6 / _OH_R1)),
        (2, "r1", "science", _science()),
    )
    times = pd.DataFrame(
        {"pfs_visit_id": [1, 2], "time_exp_start": pd.to_datetime(["2026-11-04 20:00", "2026-11-04 21:00"])}
    )
    provenance = {"collection": "u/test/run (qadb snapshot)", "builtAt": "2026-11-05T08:00:00-10:00"}
    # As the reader returns them, each row with the time its image was loaded.
    tables = {
        name: frame.assign(decidedAt=pd.Timestamp("2026-11-04 22:00")) for name, frame in tables.items()
    }
    return writeSnapshot({**tables, "visitTimes": times}, directory, provenance)


def testReportCommandWritesThePageAndTakesTheLimits(tmp_path, capsys):
    cli = _cli()
    snapshot = _snapshot(tmp_path / "snap")
    cli.main(["report", "--snapshot", str(snapshot), "--out", str(tmp_path / "out"), "--title", "Run99"])
    page = (tmp_path / "out" / "index.html").read_text()
    assert "<h1>Run99</h1>" in page and "<code>u/test/run</code>, read on 2026-11-05 (HST)" in page
    visits = pd.read_csv(tmp_path / "out" / "visits.csv").set_index("visit")
    assert visits["status"].to_dict() == {1: "WARN", 2: "PASS"}
    assert len(pd.read_csv(tmp_path / "out" / "images.csv")) == 2
    assert "science visits: 1 PASS, 1 WARN;" in capsys.readouterr().out

    # The limits are arguments: looser on the r arm, the same visit passes.
    cli.main(["report", "--snapshot", str(snapshot), "--out", str(tmp_path / "loose"), "--kms-r", "1", "2"])
    assert pd.read_csv(tmp_path / "loose" / "visits.csv")["status"].tolist() == ["PASS", "PASS"]
    # Negative control: limits out of order are refused.
    with pytest.raises(SystemExit):
        cli.main(["report", "--snapshot", str(snapshot), "--out", str(tmp_path / "bad"), "--kms-r", "2", "1"])
    assert "--kms-r: WARN then FAIL" in capsys.readouterr().err


def testServeAnswersTheIndexWithTheReport(tmp_path, monkeypatch):
    """``serve`` gives the run report the server's root, and serves nothing else."""
    import asyncio

    from tornado.httpclient import AsyncHTTPClient, HTTPClientError
    from tornado.httpserver import HTTPServer
    from tornado.testing import bind_unused_port

    cli = _cli()
    served = {}
    monkeypatch.setattr(
        cli, "listen", lambda application, address, port, show: served.update(locals()), raising=True
    )
    cli.main(["serve", "--snapshot", str(_snapshot(tmp_path / "snap")), "--title", "Run99", "--port", "5123"])
    assert (served["address"], served["port"], served["show"]) == ("localhost", 5123, False)

    async def fetch(path):
        socket, port = bind_unused_port()
        server = HTTPServer(served["application"])
        server.add_sockets([socket])
        try:
            return await AsyncHTTPClient().fetch(f"http://127.0.0.1:{port}{path}")
        finally:
            server.stop()

    response = asyncio.run(fetch("/"))
    body = response.body.decode()
    assert response.headers["Content-Type"].startswith("text/html")
    assert "<h1>Run99</h1>" in body and "residual +0.60 km/s, limit 0.5" in body
    # Negative control: the metric views' address is gone.
    with pytest.raises(HTTPClientError, match="404"):
        asyncio.run(fetch("/dashboard"))


def testSnapshotKeepsWhatTheReportShowsOfEachVisit(tmp_path, monkeypatch):
    """With the opdb, ``snapshot`` keeps each visit's time, exposure time and sequence type for the report."""
    import sys

    import pfs.drp.qa.qadb.reader

    info = pd.DataFrame(
        {
            "visit": [1],
            "time": [pd.Timestamp("2026-11-04 20:00")],
            "exp_type": ["object"],
            "exptime": [450.0],
            "cameras": ["r1"],
            "sequence_type": ["scienceObject"],
            "iis": [False],
            "status_flag": [0],
            "cmd_output": ["complete"],
        }
    )
    asked = []

    class Opdb:
        def query_dataframe(self, sql, params):
            asked.append(params)
            return info

    tables = {
        **_tables((1, "r1", "science", _science())),
        "visitTimes": pd.DataFrame({"pfs_visit_id": [1], "time_exp_start": [pd.Timestamp("2026-11-04")]}),
    }
    for name in ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics"):
        tables[name]["decidedAt"] = pd.Timestamp("2026-11-04 21:00")
    monkeypatch.setattr(pfs.drp.qa.qadb.reader, "readQadb", lambda *args, **kwargs: tables)
    monkeypatch.setitem(sys.modules, "pfs.utils.database.qadb", types.SimpleNamespace(QaDB=lambda **k: None))
    monkeypatch.setitem(
        sys.modules, "pfs.utils.database.opdb", types.SimpleNamespace(OpDB=lambda **k: Opdb())
    )
    cli = _cli()
    cli.main(["snapshot", "--collection", "c", "--opdb-host", "127.0.0.1", "--out", str(tmp_path / "snap")])
    assert asked == [{"first": 1, "last": 1}]  # one query, with the visit range bound

    cli.main(["report", "--snapshot", str(tmp_path / "snap"), "--out", str(tmp_path / "out")])
    visit = pd.read_csv(tmp_path / "out" / "visits.csv").iloc[0]
    assert (visit["hst"], visit["exptime"]) == ("2026-11-04 20:00", 450.0)
    assert "visit times from the opdb" in (tmp_path / "out" / "index.html").read_text()


def _taken(*visits):
    """Return the opdb's listing of some visits taken, as `runReport.readVisitInfo` returns it.

    Each is ``(visit, expType, sequenceType, iis, sequenceEnd)``.
    """
    return pd.DataFrame(
        {
            "visit": [v[0] for v in visits],
            "time": [
                pd.Timestamp("2026-11-04 20:00") + pd.Timedelta(minutes=10 * i) for i in range(len(visits))
            ],
            "exptime": 300.0,
            "sequenceType": [v[2] for v in visits],
            "expType": [v[1] for v in visits],
            "cameras": "b1 r1",
            "iis": [v[3] for v in visits],
            "sequenceEnd": [v[4] for v in visits],
        }
    )


def testAVisitTakenAndNeverJudgedShowsAsNoResult():
    tables = _tables((2, "r1", "science", _science()), (5, "r1", "science", _science()))
    info = _taken(
        (1, "arc", "scienceArc", True, "completed"),
        (2, "object", "scienceObject", False, "completed"),
        (3, "object", "scienceObject", False, "abortRequested"),
        (4, "dark", "darks", False, "completed"),
        (5, "object", "scienceObject", False, "completed"),
        (6, "object", "scienceObject", False, "completed"),
    )
    visits = runReport.visitTable(judge(measure(tables)), info).set_index("visit")
    # Visits 1 and 3 were taken before the latest visit judged (5) and have no result.
    assert visits["status"].to_dict() == {1: "NO RESULT", 2: "PASS", 3: "NO RESULT", 5: "PASS"}
    assert visits.loc[1, ["kind", "slit", "arms", "detectorsStatus", "why"]].tolist() == [
        *("arc", "engineering fibers", "br", "b1N r1N"),
        "sequence completed",
    ]
    assert visits.loc[3, ["kind", "slit", "why"]].tolist() == [
        "science",
        "full slit",
        "sequence ended: abortRequested",
    ]
    # Negative control: a dark has no QA method, and a visit newer than the latest judged may still be on its way.
    assert 4 not in visits.index and 6 not in visits.index
    # Negative control: without the opdb's listing nothing is known to be missing.
    plain = runReport.visitTable(judge(measure(tables)), info[["visit", "time"]])
    assert plain["visit"].tolist() == [2, 5]

    page = buildReport(tables, info=info).html
    assert "2 visits judged" in page and ", 2 more with no QA result" in page
    assert "Visits with no QA result (2)" in page and "<b>1</b> NO RESULT" in page
    assert "sequence ended: abortRequested" in page
    data = json.loads(re.search(r"const DATA = (\[.*\]);\n", page).group(1))
    assert [(row[0], row[6]) for row in data] == [
        (1, "NO RESULT"),
        (2, "PASS"),
        (3, "NO RESULT"),
        (5, "PASS"),
    ]
    # Negative control: a page whose visits all have results has no such section or note.
    whole = buildReport(tables, info=info[info["visit"].isin([2, 5])]).html
    assert "Visits with no QA result" not in whole and "more with no QA result" not in whole
