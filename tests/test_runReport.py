"""Tests for the run report (``runReport``): the gate's verdicts by image and visit, the page, and its commands."""

import dataclasses
import importlib.util
import json
import re
import types
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pfs.drp.qa.metrics import registry
from pfs.qa.dashboard import live, runReport
from pfs.qa.dashboard.qadbSource import writeSnapshot
from pfs.qa.dashboard.runReport import ReportCache, buildReport, imageTable, measure

_OH_R1 = runReport.kmsPerPixel().set_index(["camera", "species"]).loc[("r1", "OH"), "kmsPerPixel"]


def _cli():
    path = Path(__file__).parents[1] / "bin" / "qaDashboard.py"
    spec = importlib.util.spec_from_file_location("qaDashboard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The gate's frame type of each ``obsType`` the fixtures use, lit through the full slit.
_FRAME = {"science": "science", "allsky": "allsky", "arc": "arc", "trace": "quartz", "twilight": "twilight"}


def _tables(*images):
    """Return a qadb read's tables for ``images``, as `live.readCollection` returns them.

    Each image is ``(visit, camera, obsType, measures)`` and options: ``measures`` maps
    ``(reference, species, metric)`` to a value; ``gate`` is the image's gate row as
    ``(status, reason)``, None for none; ``frameType`` the gate's (by default from
    ``obsType`` and the illumination); ``cameraSet`` and ``illumination`` as stored.
    """
    iq, dm, gate, metrics = [], [], [], []
    for visit, camera, obsType, measures, *rest in images:
        options = rest[0] if rest else {}
        key = {"visit": visit, "arm": camera[0], "spectrograph": int(camera[1])}
        seqName = {"arc": "Arc: Neon", "trace": "Trace"}.get(obsType, "")
        illumination = options.get("illumination", "full")
        iq.append({**key, "obsType": obsType, "seqName": seqName, "illumination": illumination})
        dm.append({**key, "obsType": obsType, "cameraSet": options.get("cameraSet", "brn")})
        verdict = options.get("gate", ("PASS", ""))
        if verdict is not None:
            frame = _FRAME.get(obsType)
            if frame in ("arc", "quartz") and illumination == "iis":
                frame = "iis" + frame.capitalize()
            status, reason = verdict
            gate.append(
                {
                    **key,
                    "frameType": options.get("frameType", frame),
                    "qaStatus": status,
                    "qaDecidedBy": "",
                    "qaReason": reason,
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
    gateColumns = ["visit", "arm", "spectrograph", "frameType", "qaStatus", "qaDecidedBy", "qaReason"]
    return {
        "iqQaMetrics": pd.DataFrame(iq),
        "dmLineResidualSummary": pd.DataFrame(dm),
        "dmLineResidualMetrics": pd.DataFrame(metrics, columns=columns),
        "qaGate": pd.DataFrame(gate, columns=gateColumns),
    }


def _science(x=0.0, y=0.0, heldBack=0.0, species="OH"):
    """Return the measures of a science image: its offsets from the calibration and its held-back residual (px)."""
    return {
        ("calib", "Trace", "medianDeltaX"): x,
        ("calib", species, "medianDeltaY"): y,
        ("visit", species, "medianDeltaY"): heldBack,
    }


def _status(tables):
    images = imageTable(tables)
    return dict(zip(zip(images["visit"], images["camera"], strict=True), images["status"], strict=True))


def testEveryVerdictIsTheGatesAndNoneIsMadeFromTheNumbers():
    tables = _tables(
        (1, "r1", "science", _science(x=2.0, y=-3.0), {"gate": ("PASS", "")}),
        (2, "r1", "science", _science(), {"gate": ("FAIL", "OH residual +1.20 km/s >= fail 1")}),
        (3, "r1", "arc", {}, {"gate": ("WARN", "line and trace width 4% above normal")}),
        (4, "r1", "science", _science(), {"gate": None}),
    )
    images = imageTable(tables).set_index("visit")
    assert images["status"].to_dict() == {1: "PASS", 2: "FAIL", 3: "WARN", 4: "UNKNOWN"}
    assert images.loc[2, "reason"] == "OH residual +1.20 km/s >= fail 1"
    assert images.loc[1, "reason"] == "" and images.loc[4, "reason"] == ""
    # Negative control: the measured offsets are far beyond the gate's limits, and the page still says PASS.
    assert abs(measure(tables).set_index("visit").loc[1, "xOffset"]) > registry.CHECKS["skyOffset"].fail
    # Negative control: verdict columns a task's table still carries (before qadb 0.6.0) are not read.
    tables["iqQaMetrics"]["qaStatus"] = "FAIL"
    assert _status(tables)[(1, "r1")] == "PASS"


def testTheKindOfFrameIsTheGatesFrameType():
    tables = _tables(
        (1, "r1", "arc", {}, {"frameType": "iisArc"}),
        (2, "r1", "science", {}, {"frameType": "dotRoach"}),
        (3, "r1", "trace", {}, {"frameType": None}),
    )
    tables["iqQaMetrics"] = tables["iqQaMetrics"].iloc[:0]
    images = imageTable(tables).set_index("visit")
    # Lit through the engineering fibers, by the frame type, with no image-quality row to say so.
    assert images.loc[1, ["kind", "illumination"]].tolist() == ["arc", "iis"]
    assert images.loc[2, "kind"] == "other"
    # Negative control: a gate row with no frame type (written before qadb 0.6.0) is typed by obsType.
    assert images.loc[3, ["kind", "illumination"]].tolist() == ["quartz", "full"]


def testTheYOffsetIsTheSpeciesFurthestOff():
    measures = {("calib", "OH", "medianDeltaY"): 0.1, ("calib", "O2", "medianDeltaY"): -0.7}
    image = measure(_tables((1, "r1", "science", measures))).iloc[0]
    assert image["yOffset"] == pytest.approx(-0.7)
    # Negative control: it is the largest in size, not the largest signed value.
    assert image["yOffset"] != pytest.approx(0.1)


def testTheResidualIsInKmPerSecondOnTheArmsBestSpecies():
    pixels = 0.6 / _OH_R1
    tables = _tables(
        (1, "r1", "science", _science(heldBack=pixels)),
        (2, "r1", "science", {**_science(), ("visit", "O2", "medianDeltaY"): 5.0}),
    )
    images = measure(tables).set_index("visit")
    assert images.loc[1, "kms"] == pytest.approx(0.6)
    # Negative control: the residual is the arm's best species', not another species' held-back lines.
    assert images.loc[2, "kms"] == pytest.approx(0.0)
    # Negative control: read as pixels, the same residual is a different number.
    assert pixels != pytest.approx(images.loc[1, "kms"])


def testAVisitTakesItsWorstDetectorAndTheEveningsDate():
    tables = _tables(
        (1, "b1", "science", _science(species="OI")),
        (1, "r1", "science", _science(), {"gate": ("FAIL", "x offset +1.50 px >= fail 1")}),
        (2, "r1", "allsky", {}),
        (3, "n1", "arc", {}, {"gate": ("WARN", "calib NeI: |medianDeltaY|=1.5px >= warn threshold 1px")}),
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
    visits = runReport.visitTable(measure(tables), info).set_index("visit")
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
    tables = _tables(
        (200, "b1", "science", _science(x=-0.55, species="OI"), {"gate": ("FAIL", "x <script> & y")}),
        (201, "r1", "science", _science()),
    )
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
    assert "made 2026-11-05" in page and "4 November to 4 November 2026 (HST)" in page
    data = json.loads(re.search(r"const DATA = (\[.*\]);\n", page).group(1))
    assert [row[0] for row in data] == made.visits["visit"].tolist()
    # qadb's text is escaped wherever it lands: in the table and in the script.
    assert "<script> & y" not in page and "x &lt;script&gt; &amp; y" in page
    # The report's own judging is gone: no limits table, no m-arm appendix.
    assert "sets of km/s limits" not in page and "appendix-m-arm" not in page


def testThePageListsTheGatesChecks(monkeypatch):
    """What the page says the gate applies, and the limits it draws, are drp_qa's."""
    tables = _tables((1, "b1", "science", _science(heldBack=0.1, species="OI")))
    page = buildReport(tables).html
    assert "<strong>Science:</strong> offset from the calibration: WARN at 0.5 px, FAIL at 1 px" in page
    assert (
        "[O I] residual after the visit&#x27;s adjustment on the b arm: WARN at 1.5 km/s, FAIL at 2.5 km/s"
        in page
    )
    assert "line and trace width: WARN at 3%, FAIL at 5% from the camera's normal value" in page
    assert "b arm · [O I] lines · WARN at 1.5 km/s, FAIL at 2.5 km/s" in page
    # Negative control: a change of a check in drp_qa is a change of the page.
    changed = dataclasses.replace(registry.CHECKS["skyLineResidualBlue"], warn=2.0, fail=3.0)
    monkeypatch.setitem(registry.CHECKS, "skyLineResidualBlue", changed)
    other = buildReport(tables).html
    assert "on the b arm: WARN at 2 km/s, FAIL at 3 km/s" in other and "WARN at 1.5 km/s" not in other


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
    made = []
    real = runReport.buildReport
    monkeypatch.setattr(runReport, "buildReport", lambda *a, **k: made.append(1) or real(*a, **k))
    store = _Store(
        _tables((200, "b1", "science", _science(species="OI")), (201, "r1", "science", _science()))
    )
    cache = ReportCache(store, title="Live")
    first = cache.html()
    assert cache.html() == first and len(made) == 1 and store.refreshed == 2 and cache.built == 1
    assert "2026-11-04 20:00" in first  # the store's own visit times, without the opdb
    store.raw = _tables((300, "r1", "science", _science(x=2.0)))
    store.version += 1
    assert "[300," in cache.html() and len(made) == 2 and cache.built == 2
    # Negative control: the page did change with the store.
    assert "[300," not in first


def testAStoreWithNoImageYetStillHasAPage():
    store = _Store({})
    store.times = store.times.iloc[:0]
    page = ReportCache(store, readInfo=lambda first, last: pytest.fail("no visit to read")).html()
    assert "const DATA = [];" in page and "0 visits" in page


def testThePageServedReadsNothing():
    """What the server answers with is the last page made: asking for it reads no database."""
    store = _Store(_tables((1, "r1", "science", _science())))
    cache = ReportCache(store)
    cache.update()
    assert store.refreshed == 1
    page, status = cache.page, cache.status()
    assert store.refreshed == 1 and "const DATA = [[1," in page
    assert status["built"] == 1 and not status["stale"] and status["error"] is None
    # Negative control: an update does read the store.
    cache.update()
    assert store.refreshed == 2


def testAReadThatFailsKeepsTheLastPageAndSaysSo():
    store = _Store(_tables((1, "r1", "science", _science())))
    cache = ReportCache(store)
    cache.update()
    page, readAt = cache.page, cache.readAt

    def down():
        raise ConnectionError("connection refused\nsecond line")

    working, store.refresh = store.refresh, down
    cache.poll()
    assert cache.page == page and cache.readAt == readAt
    assert cache.status()["error"] == "ConnectionError: connection refused"
    # Negative control: the next read that works clears it.
    store.refresh = working
    cache.poll()
    assert cache.status()["error"] is None and cache.readAt > readAt


def testAWatchedPageFollowsTheStatusAndAReportOnItsOwnDoesNot():
    """Served live, the page asks the server for its status; made once, it fetches nothing."""
    import time

    store = _Store(_tables((1, "r1", "science", _science())))
    cache = ReportCache(store)
    cache.update()
    alone = cache.page
    assert "fetch(" not in alone and alone == cache.report.html

    thread = cache.watch(0.01)
    assert thread.daemon
    watched = cache.page
    assert 'fetch("status"' in watched and "const built = 1," in watched and "{{" not in watched
    assert "keepView(); location.reload();" in watched  # a reload it makes keeps what the reader chose
    assert watched.rstrip().endswith("</body>\n</html>") and watched.count("</body>") == 1
    # The thread makes the next page when the store changes, and the page served carries its count.
    store.raw = _tables((300, "r1", "science", _science(x=2.0)))
    store.version += 1
    deadline = time.monotonic() + 10
    while cache.built < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache.built == 2 and "const built = 2," in cache.page and "[300," in cache.page


def testStatusSaysWhenNoReadHasWorkedForLong():
    """A read that hangs raises nothing: the time since the last one that worked tells."""
    from datetime import timedelta

    cache = ReportCache(_Store(_tables((1, "r1", "science", _science()))))
    cache.update()
    cache.interval = 30.0
    cache.readAt -= timedelta(seconds=89)
    assert not cache.status()["stale"]  # three intervals, and at least 90 s
    cache.readAt -= timedelta(seconds=2)
    assert cache.status()["stale"]
    # A read on an earlier day says which.
    cache.readAt -= timedelta(days=2)
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", cache.status()["readAt"])
    # Negative control: a cache nothing watches is never stale.
    cache.interval = None
    assert not cache.status()["stale"]


def testASlowReadIsToldFromAStoppedReaderAndLogged(caplog):
    """Stale while a read is under way is a slow read (amber on the page); stale with none is a stopped reader."""
    import threading
    from datetime import timedelta

    store = _Store(_tables((1, "r1", "science", _science())))
    cache = ReportCache(store)
    cache.update()
    cache.interval = 30.0
    release, started = threading.Event(), threading.Event()
    working = store.refresh
    store.refresh = lambda: started.set() or release.wait(10) or working()
    reader = threading.Thread(target=cache.poll)
    reader.start()
    assert started.wait(10)
    cache.readAt -= timedelta(seconds=120)
    with caplog.at_level("WARNING", logger="pfs.qa.dashboard.runReport"):
        status = cache.status()
        cache.status()
    assert status["stale"] and status["reading"] and status["error"] is None
    said = [r.getMessage() for r in caplog.records if "No read of the results" in r.getMessage()]
    assert len(said) == 1 and "under way since" in said[0]  # once per wait, not per page poll
    caplog.clear()
    with caplog.at_level("WARNING", logger="pfs.qa.dashboard.runReport"):
        cache.readingSince -= timedelta(seconds=120)  # the read began two minutes ago
        release.set()
        reader.join(10)
    assert cache.status()["reading"] == "" and not cache.status()["stale"]
    assert any("Reading the results took" in r.getMessage() for r in caplog.records)
    # Negative control: stale with no read under way is a reader that stopped, not a slow read.
    cache.readAt -= timedelta(seconds=120)
    assert cache.status()["stale"] and cache.status()["reading"] == ""
    page = cache.page
    assert (
        "the server has stopped reading" in page and "Still reading, since" in page and "#live.slow" in page
    )


def _snapshot(directory):
    tables = _tables(
        (1, "r1", "science", _science(heldBack=0.6 / _OH_R1), {"gate": ("WARN", "OH residual +0.60 km/s")}),
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


def testReportCommandWritesThePage(tmp_path, capsys):
    cli = _cli()
    snapshot = _snapshot(tmp_path / "snap")
    cli.main(["report", "--snapshot", str(snapshot), "--out", str(tmp_path / "out"), "--title", "Run99"])
    page = (tmp_path / "out" / "index.html").read_text()
    assert "<h1>Run99</h1>" in page and "<code>u/test/run</code>, read on 2026-11-05 (HST)" in page
    visits = pd.read_csv(tmp_path / "out" / "visits.csv").set_index("visit")
    assert visits["status"].to_dict() == {1: "WARN", 2: "PASS"}
    assert len(pd.read_csv(tmp_path / "out" / "images.csv")) == 2
    assert "science visits: 1 PASS, 1 WARN;" in capsys.readouterr().out
    # Negative control: the report's own limits are no longer arguments.
    with pytest.raises(SystemExit):
        cli.main(["report", "--snapshot", str(snapshot), "--out", str(tmp_path / "bad"), "--kms-r", "1", "2"])
    assert "unrecognized arguments: --kms-r" in capsys.readouterr().err


def _fetch(application, path):
    """Return the response of ``application`` to a GET of ``path``, served on a port of its own."""
    import asyncio

    from tornado.httpclient import AsyncHTTPClient
    from tornado.httpserver import HTTPServer
    from tornado.testing import bind_unused_port

    async def fetch():
        socket, port = bind_unused_port()
        server = HTTPServer(application)
        server.add_sockets([socket])
        try:
            return await AsyncHTTPClient().fetch(f"http://127.0.0.1:{port}{path}")
        finally:
            server.stop()

    return asyncio.run(fetch())


def testServeMountsTheNightPageAndTheRunReport(tmp_path, monkeypatch):
    """``serve`` gives the night page ``/``, the run report ``/run``, a ``/visit`` stub and ``/status``."""
    from tornado.httpclient import HTTPClientError

    cli = _cli()
    served = {}
    monkeypatch.setattr(
        cli, "listen", lambda application, address, port, show: served.update(locals()), raising=True
    )
    cli.main(["serve", "--snapshot", str(_snapshot(tmp_path / "snap")), "--title", "Run99", "--port", "5123"])
    assert (served["address"], served["port"], served["show"]) == ("localhost", 5123, False)

    night = _fetch(served["application"], "/").body.decode()
    assert "The night on the clock" in night and "Latest visits" in night and "<h1>Run99</h1>" in night
    # The night page links to the run report and to the per-visit route.
    assert 'href="/run"' in night and "visit/1" in night
    run = _fetch(served["application"], "/run").body.decode()
    assert "Every visit at a glance" in run and "OH residual +0.60 km/s" in run
    # Negative control: the whole-run markers are the run report's, not the night page's.
    assert "Every visit at a glance" not in night and "The night on the clock" not in run
    # A snapshot never changes: both pages are alone, with nothing to follow.
    assert "fetch(" not in night and "fetch(" not in run
    # The per-visit route builds a page from the cached store, with that visit's verdicts.
    one = _fetch(served["application"], "/visit/1").body.decode()
    assert "<h1>Visit 1" in one and "Detectors" in one and "OH residual +0.60 km/s" in one
    status = json.loads(_fetch(served["application"], "/status").body)
    assert status["built"] == 1 and status["error"] is None and not status["stale"]
    # Negative control: the metric views' address is gone.
    with pytest.raises(HTTPClientError, match="404"):
        _fetch(served["application"], "/dashboard")


def testServeFromQadbWatchesItAndThePageFollows(monkeypatch):
    """Live, ``serve`` makes the first page, then leaves the reading to the cache's thread."""
    import sys

    tables = {
        **_tables((1, "r1", "science", _science(x=0.7), {"gate": ("WARN", "x offset +0.70 px >= warn 0.5")})),
        "visitTimes": pd.DataFrame({"pfs_visit_id": [1], "time_exp_start": [pd.Timestamp("2026-11-04")]}),
    }
    for name in ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics", "qaGate"):
        tables[name]["decidedAt"] = pd.Timestamp("2026-11-04 21:00")
    gate = tables.pop("qaGate")
    reads = []
    monkeypatch.setattr(live, "readQadb", lambda *args, **kwargs: reads.append(args) or tables)
    monkeypatch.setattr(live, "readGate", lambda *args: gate)
    monkeypatch.setitem(sys.modules, "pfs.utils.database.qadb", types.SimpleNamespace(QaDB=lambda **k: None))
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    watched = []
    monkeypatch.setattr(runReport.ReportCache, "watch", lambda self, interval: watched.append(interval))
    cli = _cli()
    served = {}
    monkeypatch.setattr(cli, "listen", lambda application, address, port, show: served.update(locals()))
    cli.main(["serve", "--collection", "c", "--refresh", "20"])
    assert watched == [20.0] and len(reads) == 2  # the store's first read, and the first page's
    assert cli.os.environ["PGCONNECT_TIMEOUT"] == "10"

    body = _fetch(served["application"], "/").body.decode()
    assert "x offset +0.70 px &gt;= warn 0.5" in body
    # Asking for the page read nothing more.
    assert len(reads) == 2
    # Negative control: a refresh shorter than a second is refused.
    with pytest.raises(SystemExit):
        cli.main(["serve", "--collection", "c", "--refresh", "0"])


def testSnapshotKeepsWhatTheReportShowsOfEachVisit(tmp_path, monkeypatch):
    """With the opdb, ``snapshot`` keeps each visit's time, exposure time and sequence type for the report."""
    import sys

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
    for name in ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics", "qaGate"):
        tables[name]["decidedAt"] = pd.Timestamp("2026-11-04 21:00")
    gate = tables.pop("qaGate")
    monkeypatch.setattr(live, "readQadb", lambda *args, **kwargs: tables)
    monkeypatch.setattr(live, "readGate", lambda *args: gate)
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
    visits = runReport.visitTable(measure(tables), info).set_index("visit")
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
    plain = runReport.visitTable(measure(tables), info[["visit", "time"]])
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
