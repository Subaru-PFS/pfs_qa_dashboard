"""Tests for the run's auto-guiding report (``pfs.qa.dashboard.agRun``): its store, its page, its routes and the CLI."""

import html
import json
import re

import numpy as np
import pandas as pd
import pytest
from test_runReport import _cli, _fetch, _science, _tables

from pfs.drp.qa.guiders.runPlotting import captions
from pfs.qa.dashboard import agRun
from pfs.qa.dashboard.agRun import (
    AgRunCache,
    AgRunStore,
    agRoutes,
    agTables,
    buildAgReport,
    nightStart,
    snapshotTables,
)
from pfs.qa.dashboard.qadbSource import readSnapshot, writeSnapshot
from pfs.qa.dashboard.runReport import buildReport

#: The first night of the synthetic run (its evening's date), and the noon that begins it.
NIGHT = pd.Timestamp("2026-11-03")
START = NIGHT + pd.Timedelta(hours=12)


def agRunTables(nights=2, visits=3, exposures=30, dead=(), seed=3):
    """Return the tables of drp_qa's ``readAgRun`` for a small run: science visits from 20:00 HST each night.

    Every camera but those of ``dead`` detects 8 spots, 6 of them good matches, in every AG exposure
    (10 s apart); the guide offsets are noise of 0.05″ (1″ in rotation).
    """
    rng = np.random.default_rng(seed)
    exposureRows, cameraRows, visitRows, magnitudeRows = [], [], [], []
    aid = 5000
    for night in range(nights):
        when = NIGHT + pd.Timedelta(days=night, hours=20)
        for index in range(visits):
            visit = 153000 + 10 * night + index
            visitRows.append((visit, 77, 0.0, 9000 + visit, "scienceObject"))
            for _ in range(exposures):
                aid += 1
                when += pd.Timedelta(seconds=10)
                focus = {f"guide_delta_z{n}": 0.01 * (-1) ** n + rng.normal(0, 1e-3) for n in range(1, 7)}
                exposureRows.append(
                    {
                        "agc_exposure_id": aid,
                        "pfs_visit_id": visit,
                        "taken_at": when,
                        "agc_exptime": 3.0,
                        "altitude": 60.0,
                        "azimuth": 10.0,
                        "mask": 0.0,
                        "guide_dec": 0.0,
                        "guide_delta_ra": rng.normal(0, 0.05),
                        "guide_delta_dec": rng.normal(0, 0.05),
                        "guide_delta_insrot": rng.normal(0, 1.0),
                        "guide_delta_z": float(np.median(list(focus.values()))),
                        **focus,
                    }
                )
                for camera in range(6):
                    if camera in dead:
                        continue
                    for half, sigma in ((0, 2.0), (1, 2.2)):
                        cameraRows.append((aid, camera, half, 4, 3, sigma, 100.0, 12.0, 0, 1, 0, 0, 0, 0))
            for camera in range(6):
                magnitudeRows.append(
                    (visit, camera, 15.0, 2 * exposures, 0 if camera in dead else 2 * exposures)
                )
    exposureFrame = pd.DataFrame(exposureRows)
    return {
        "cameras": pd.DataFrame(
            cameraRows,
            columns=[
                "agc_exposure_id",
                "agc_camera_id",
                "right_half",
                "n_spots",
                "n_good",
                "sigma_pix",
                "background",
                "residual_um",
                "n_disabled",
                "n_edge",
                "n_saturated",
                "n_bad_shape",
                "n_bad_ellip",
                "n_bad_size",
            ],
        ),
        "exposures": exposureFrame,
        "visits": pd.DataFrame(
            visitRows,
            columns=[
                "pfs_visit_id",
                "pfs_design_id",
                "dec_center_designed",
                "iic_sequence_id",
                "sequence_type",
            ],
        ),
        "focus": pd.DataFrame(
            {
                "pfs_visit_id": exposureFrame.pfs_visit_id,
                "created_at": exposureFrame.taken_at,
                "m2_off3": 3.25,
            }
        ),
        "designStars": pd.DataFrame(
            [(77, camera, 12, 10) for camera in range(6)],
            columns=["pfs_design_id", "agc_camera_id", "n_stars", "n_bright"],
        ),
        "magnitudes": pd.DataFrame(
            magnitudeRows, columns=["pfs_visit_id", "agc_camera_id", "magnitude", "n_expected", "n_good"]
        ),
    }


def conditions(tables, seeing=0.7, first=0, last=2**31 - 1):
    """Return qadb's seeing and transparency for the run's AG exposures of some visits, as the readers do."""
    exposures = tables["exposures"]
    exposures = exposures[exposures.pfs_visit_id.between(first, last)].reset_index(drop=True)
    frame = pd.DataFrame(
        {
            "visit": exposures.pfs_visit_id,
            "agcExposureId": exposures.agc_exposure_id,
            "time": exposures.taken_at,
        }
    )
    return {"seeing": frame.assign(value=seeing), "transparency": frame.assign(value=0.8)}


class Opdb:
    """Answers ``readRun(start, end)`` from a run's tables, and records what was asked."""

    def __init__(self, tables):
        self.tables = tables
        self.asked = []

    def __call__(self, start, end):
        self.asked.append((pd.Timestamp(start), pd.Timestamp(end)))
        exposures = self.tables["exposures"]
        exposures = exposures[(exposures.taken_at >= start) & (exposures.taken_at < end)]
        visits = set(exposures.pfs_visit_id)
        focus = self.tables["focus"]
        return {
            "cameras": self.tables["cameras"][
                self.tables["cameras"].agc_exposure_id.isin(exposures.agc_exposure_id)
            ],
            "exposures": exposures,
            "visits": self.tables["visits"][self.tables["visits"].pfs_visit_id.isin(visits)],
            "focus": focus[(focus.created_at >= start) & (focus.created_at < end)],
            "designStars": self.tables["designStars"],
            "magnitudes": self.tables["magnitudes"][self.tables["magnitudes"].pfs_visit_id.isin(visits)],
        }


def testNightStartIsTheNoonBefore():
    assert nightStart("2026-11-04 02:00") == START
    assert nightStart("2026-11-03 20:00") == START
    assert nightStart("2026-11-03 12:00") == START
    # Negative control: the morning before noon belongs to the night before.
    assert nightStart("2026-11-03 11:59") == START - pd.Timedelta(days=1)


def testStoreReadsTheRunOnceThenOnlyTheNightUnderWay():
    """After the first read, a refresh reads the night under way; a night that has ended is read once more and kept."""
    tables = agRunTables(nights=3)
    opdb = Opdb(tables)
    clock = [NIGHT + pd.Timedelta(days=1, hours=23)]  # the second night, 23:00
    store = AgRunStore(opdb, START, START + pd.Timedelta(days=3), now=lambda: clock[0])
    second = START + pd.Timedelta(days=1)

    assert store.refresh() is True
    assert opdb.asked == [(START, second), (second, clock[0])]
    assert store.tables["exposures"].pfs_visit_id.nunique() == 6 and store.version == 1
    # Nothing new: only the night under way is read again, and it is no change.
    opdb.asked.clear()
    assert store.refresh() is False and store.version == 1
    assert opdb.asked == [(second, clock[0])]
    # The next afternoon: the night that ended is read to its end and kept; the new night has nothing yet.
    opdb.asked.clear()
    clock[0] = NIGHT + pd.Timedelta(days=2, hours=13)
    store.refresh()
    assert opdb.asked == [(second, second + pd.Timedelta(days=1)), (second + pd.Timedelta(days=1), clock[0])]
    # Once the run is over, only its end is waited for: nothing after it is read.
    opdb.asked.clear()
    clock[0] = NIGHT + pd.Timedelta(days=9)
    assert store.refresh() is True
    assert opdb.asked == [(second + pd.Timedelta(days=1), START + pd.Timedelta(days=3))]
    assert len(store.tables["exposures"]) == len(tables["exposures"])
    assert not store.tables["visits"].pfs_visit_id.duplicated().any()
    assert len(store.tables["designStars"]) == 6
    opdb.asked.clear()
    assert store.refresh() is False and opdb.asked == []


def testStoreSeesAGuideOffsetWrittenLate():
    """A value that changes in a row already read is a change, though no row was added."""
    tables = agRunTables(nights=1)
    opdb = Opdb(tables)
    store = AgRunStore(opdb, START, START + pd.Timedelta(days=1), now=lambda: NIGHT + pd.Timedelta(hours=23))
    last = tables["exposures"].index[-1]
    tables["exposures"].loc[last, "mask"] = np.nan
    store.refresh()

    tables["exposures"].loc[last, "mask"] = 0.0
    assert store.refresh() is True and store.version == 2
    # Negative control: read again with nothing changed, it is the same.
    assert store.refresh() is False


def testStoreJoinsANightWhoseOffsetsAreAllNull():
    """A night read before any guide offset was written (its columns all NULL, so objects) joins as numbers."""
    tables = agRunTables(nights=2)

    def read(start, end):
        part = Opdb(tables)(start, end)
        if start > START:  # the night under way: one AG exposure, its guide offset not yet written
            offsets = [name for name in part["exposures"] if name.startswith("guide_") or name == "mask"]
            part["exposures"] = part["exposures"].head(1).astype(dict.fromkeys(offsets, object))
            part["exposures"][offsets] = None
        return part

    store = AgRunStore(
        read, START, START + pd.Timedelta(days=2), now=lambda: NIGHT + pd.Timedelta(days=1, hours=23)
    )
    store.refresh()
    exposures = store.tables["exposures"]

    assert exposures.guide_dec.dtype == float and exposures["mask"].dtype == float
    assert exposures.guide_dec.isna().sum() == 1
    assert "Cameras in use" in buildAgReport(store.tables).html
    # Negative control: joined as they come, the columns would be objects, which the analysis cannot take.
    night = read(START + pd.Timedelta(days=1), START + pd.Timedelta(days=2))["exposures"]
    assert pd.concat([tables["exposures"], night]).guide_dec.dtype == object


def testStoreReadsTheConditionsOfEachPeriodsVisits():
    """The conditions qadb holds are read as the AG tables are: the run once, then the night under way's visits."""
    tables = agRunTables(nights=3)
    asked = []

    def readConditions(first, last):
        asked.append((first, last))
        return conditions(tables, first=first, last=last)

    clock = [NIGHT + pd.Timedelta(days=1, hours=23)]
    store = AgRunStore(
        Opdb(tables), START, START + pd.Timedelta(days=3), readConditions=readConditions, now=lambda: clock[0]
    )
    store.refresh()

    # The first night's visits (153000-153002), then the second's (153010-153012), under way.
    assert asked == [(153000, 153002), (153010, 153012)]
    assert len(store.tables["seeing"]) == 2 * 3 * 30 and set(store.tables) == {
        *tables,
        "seeing",
        "transparency",
    }
    asked.clear()
    store.refresh()
    assert asked == [(153010, 153012)]
    # Once the run is over, it is read to its end, and then qadb is not asked again.
    clock[0] = NIGHT + pd.Timedelta(days=9)
    store.refresh()
    assert len(store.tables["seeing"]) == len(tables["exposures"])
    assert not store.tables["seeing"].agcExposureId.duplicated().any()
    asked.clear()
    store.refresh()
    assert asked == []
    # Negative control: a period with no AG exposure has no visits to ask qadb for.
    quiet = []
    AgRunStore(
        Opdb(tables),
        START + pd.Timedelta(days=5),
        START + pd.Timedelta(days=6),
        readConditions=lambda first, last: quiet.append((first, last)) or {},
        now=lambda: NIGHT + pd.Timedelta(days=9),
    ).refresh()
    assert quiet == []


def testASnapshotKeepsTheAgTables(tmp_path):
    tables = {**agRunTables(nights=1), **conditions(agRunTables(nights=1))}
    writeSnapshot({"iqQaMetrics": pd.DataFrame({"visit": [1]}), **snapshotTables(tables)}, tmp_path, {})
    read, _ = readSnapshot(tmp_path)

    assert (tmp_path / "agRun-exposures.parquet").exists()
    assert set(agTables(read)) == set(tables)
    store = AgRunStore.fromTables(agTables(read))
    assert store.refresh() is False and len(store.tables["exposures"]) == len(tables["exposures"])
    # Negative control: a snapshot without them has no AG report.
    assert agTables({"iqQaMetrics": pd.DataFrame({"visit": [1]})}) is None


def testFlagHtmlLinksVisitsAndNotCounts():
    text = (
        "**AG1: no detections at all in 152000 consecutive AG exposures** from 2026-09-10 19:40 (visit 149951); "
        "112 of 160646 AG exposures have no guide offset, in 2 visits: 149486, 150815. "
        "Worst jitter, worst: 150815 (93×), 148259 (13×)."
    )
    out = agRun._flagHtml(text)

    assert out.startswith("<b>AG1: no detections at all in 152000 consecutive AG exposures</b>")
    assert re.findall(r"/visit/(\d+)'", out) == ["149951", "149486", "150815", "150815", "148259"]
    # Negative control: a count of AG exposures is not a visit, however long it is.
    assert "/visit/152000" not in out and "/visit/160646" not in out
    assert "&lt;" in agRun._flagHtml("a < b")


def testPageShowsTheRunAsDrpQaReportsIt():
    tables = {**agRunTables(), **conditions(agRunTables())}
    report = buildAgReport(tables, title="Run31 visit verdicts", source="the opdb, read {date}.")
    page = report.html

    assert "<h1>Auto-guiding</h1>" in page and "&lsaquo; Run31 visit verdicts</a>" in page
    assert "{{" not in page
    # The scale is said where the reader sees it, and the offsets carry an axis in AG pixels.
    assert "1 AG pixel = 13 µm = 0.14″" in page and ">AG pixels</text>" in page
    # The captions are drp_qa's own, one for every figure.
    for caption in captions().values():
        assert html.escape(caption) in page
    assert "Cameras in use" in page and "6 <span class='unit'>of 6</span>" in page
    # Every science visit is a point in each guiding panel, linked to its page.
    assert page.count("<a href='/visit/153000'>") == 6 + 1  # size and jitter on three axes, and the focus
    for camera in range(1, 7):
        assert f"src='/run/ag/camera/AG{camera}.png'" in page
    assert "Seeing and transparency" in page and "median 0.70″" in page
    # The run report's block: the headline numbers and the way to the page.
    assert "<a href='/run/ag'>The AG report</a>" in report.summary
    assert "<span class='lab'>Guide error</span>" in report.summary and "id='ag'" in report.summary
    # The map of camera states is the page's, not the block's.
    assert "<svg class='agplot" in page and "<svg" not in report.summary
    # Negative control: without qadb's conditions there is no such section, and no seeing tile.
    plain = buildAgReport(agRunTables()).html
    assert "Seeing and transparency" not in plain and "<span class='lab'>Seeing</span>" not in plain


def testACameraNotInUseIsNamedAndNotDrawn():
    report = buildAgReport(agRunTables(dead=(1,)))

    assert "5 <span class='unit'>of 6</span>" in report.html and "AG2 not in use" in report.html
    assert "not in use: no detections in the run" in report.html
    assert "camera/AG2.png" not in report.html and "camera/AG4.png" in report.html
    assert report.cameraPng(2) is None
    # Negative control: a camera in use has a figure.
    assert report.cameraPng(4)[:8] == b"\x89PNG\r\n\x1a\n"
    assert report.cameraPng(7) is None


def testAPageWrittenAsAFileHoldsItsFigures():
    report = buildAgReport(agRunTables(nights=1), runUrl="run31.html", agUrl="run31-ag.html", embed=True)

    assert report.html.count("src='data:image/png;base64,") == 6
    assert 'href="run31.html"' in report.html
    assert "<a href='run31-ag.html'>The AG report</a>" in report.summary
    # Negative control: served, the page asks the server for them.
    assert "data:image/png" not in buildAgReport(agRunTables(nights=1)).html


def testARunWithNoAgExposureSaysSo():
    empty = {name: frame.iloc[:0] for name, frame in agRunTables(nights=1).items()}
    report = buildAgReport(empty, title="Run31 visit verdicts")

    assert "No AG exposure has been read for this run yet." in report.html
    assert report.summary == "" and report.cameraPng(1) is None
    # A night with too few AG exposures to report is said too.
    few = buildAgReport(agRunTables(nights=1, visits=1, exposures=5))
    assert "No night of this run has enough AG exposures to report yet." in few.html


def testTheRunReportCarriesTheAgBlock():
    tables = _tables((1, "r1", "science", _science()))
    block = buildAgReport(agRunTables(nights=1)).summary

    assert "<h2>Auto-guiding</h2>" in buildReport(tables, agSummary=block).html
    assert "<h2>Auto-guiding</h2>" in buildReport(tables, agSummary=lambda: block).html
    # Negative control: without one the run report has no such section, and no placeholder left.
    plain = buildReport(tables).html
    assert "Auto-guiding" not in plain and "{{AG_SUMMARY}}" not in plain


def _application(cache):
    from tornado.web import Application

    return Application(agRoutes(cache, "/run/ag"))


def testRoutesServeThePageItsStatusAndTheFigures():
    from tornado.httpclient import HTTPClientError

    cache = AgRunCache(AgRunStore.fromTables(agRunTables(nights=1, dead=(1,))), title="Run31 visit verdicts")
    cache.update()
    application = _application(cache)

    page = _fetch(application, "/run/ag")
    assert "text/html" in page.headers["Content-Type"] and "<h1>Auto-guiding</h1>" in page.body.decode()
    # The page's live script asks for ``status`` beside it.
    status = json.loads(_fetch(application, "/run/status").body)
    assert status["built"] == 1 and status["error"] is None
    figure = _fetch(application, "/run/ag/camera/AG4.png")
    assert figure.headers["Content-Type"] == "image/png" and figure.body[:4] == b"\x89PNG"
    # Negative control: a camera not in use, and one that does not exist, have none.
    for path in ("/run/ag/camera/AG2.png", "/run/ag/camera/AG7.png"):
        with pytest.raises(HTTPClientError) as error:
            _fetch(application, path)
        assert error.value.code == 404


def testAFailedFirstReadLeavesAPageThatSaysSo():
    """Until a read has worked the page says the tables have not been read, and why; then it is the report."""
    tables = agRunTables(nights=1)
    opdb = Opdb(tables)
    failing = [True]

    def read(start, end):
        if failing[0]:
            raise RuntimeError("connection refused")
        return opdb(start, end)

    store = AgRunStore(read, START, START + pd.Timedelta(days=1), now=lambda: NIGHT + pd.Timedelta(hours=23))
    cache = AgRunCache(store, title="Run31 visit verdicts")
    cache.poll()

    assert "The AG tables have not been read yet: RuntimeError: connection refused" in cache.pageFor()
    assert cache.summary == "" and cache.status()["error"] == "RuntimeError: connection refused"
    failing[0] = False
    cache.poll()
    assert "Cameras in use" in cache.pageFor() and cache.status()["error"] is None
    assert "<span class='lab'>Guide error</span>" in cache.summary


def testCacheMakesThePageAgainOnlyWhenAReadBringsSomething():
    tables = agRunTables(nights=1)
    opdb = Opdb(tables)
    clock = [NIGHT + pd.Timedelta(hours=20, minutes=6)]  # part-way through the second visit
    store = AgRunStore(opdb, START, START + pd.Timedelta(days=1), now=lambda: clock[0])
    cache = AgRunCache(store, rebuildEvery=0)

    assert cache.update() is True and cache.built == 1
    first = cache.pageFor()
    assert cache.update() is False and cache.built == 1 and cache.pageFor() == first
    # Later in the night more AG exposures have been taken: the page is made again.
    clock[0] = NIGHT + pd.Timedelta(hours=23)
    assert cache.update() is True and cache.built == 2
    assert cache.pageFor() != first


def testThePageIsNotMadeAgainOnEveryRead():
    """The store is read every time, for the night page's tiles; the run's page waits until it is due."""
    tables = agRunTables(nights=1)
    clock = [NIGHT + pd.Timedelta(hours=20, minutes=6)]
    store = AgRunStore(Opdb(tables), START, START + pd.Timedelta(days=1), now=lambda: clock[0])
    cache = AgRunCache(store, rebuildEvery=3600)
    cache.update()
    early = cache.nightTiles("2026-11-03")

    clock[0] = NIGHT + pd.Timedelta(hours=23)
    assert cache.update() is False and cache.built == 1
    # The read happened all the same: the night's tiles are of the newer tables.
    assert store.version == 2 and cache.nightTiles("2026-11-03") != early
    assert "from 90 AG exposures" in cache.nightTiles("2026-11-03")
    # Negative control: with nothing between makings, the same read makes the page again.
    assert agRun.AG_REFRESH < agRun.AG_REBUILD
    eager = AgRunCache(
        AgRunStore(Opdb(tables), START, START + pd.Timedelta(days=1), now=lambda: clock[0]), rebuildEvery=0
    )
    clock[0] = NIGHT + pd.Timedelta(hours=20, minutes=6)
    eager.update()
    clock[0] = NIGHT + pd.Timedelta(hours=23)
    assert eager.update() is True and eager.built == 2


def testTilesSayOneThingEach():
    """One number a tile: the guide error and its jitter on the sky, and the share of guide stars matched."""
    report = buildAgReport(agRunTables(dead=(1,)))
    labels = re.findall(r"<span class='lab'>([^<]*)</span>", report.html)

    assert labels == [
        "Cameras in use",
        "Guide error",
        "Guide jitter",
        "Guide stars matched",
        "Star position, measured − expected",
        "Too few stars",
        "Guide offsets not used",
        "FWHM of matched stars",
        "Flags",
    ]
    # Every star designed on the five cameras in use is matched in every AG exposure.
    assert "<span class='big'>100<span class='unit'>%</span></span>" in report.html
    assert "of the designed guide stars on the cameras in use, median visit" in report.html
    # The guide error is one value in arcsec, said in AG pixels too.
    assert re.search(r"Guide error</span><span class='big'>0\.0\d<span class='unit'>″</span>", report.html)
    assert re.search(r"median visit · 0\.\d AG pixels · 95% of visits under 0\.\d\d″", report.html)
    # The run report's block has the few that summarise the run.
    assert re.findall(r"<span class='lab'>([^<]*)</span>", report.summary) == [
        "Cameras in use",
        "Guide error",
        "Guide jitter",
        "Guide stars matched",
        "Flags",
    ]
    # Negative control: a read without the designed guide stars has no such tile.
    older = agRunTables()
    del older["magnitudes"]
    assert "Guide stars matched" not in buildAgReport(older).html


def testNightTilesAreOneNightsOwn():
    """The night page's AG tiles summarise that night's AG exposures alone."""
    tables = agRunTables(nights=2)
    # On the second night AG3 detects nothing, and the pointing is ten times as far off.
    second = tables["exposures"].taken_at >= START + pd.Timedelta(days=1)
    tables["exposures"].loc[second, "guide_delta_dec"] *= 10
    ids = tables["exposures"].agc_exposure_id[second]
    cameras = tables["cameras"]
    tables["cameras"] = cameras[~(cameras.agc_exposure_id.isin(ids) & (cameras.agc_camera_id == 2))]
    first, later = agRun.buildNightTiles(tables, "2026-11-03"), agRun.buildNightTiles(tables, "2026-11-04")

    assert re.findall(r"<span class='lab'>([^<]*)</span>", first) == [
        "Cameras in use",
        "Guide error",
        "Guide jitter",
        "Guide stars matched",
        "Guide offsets not used",
    ]
    assert (
        "from 90 AG exposures and 3 science visits" in first
        and "<a href='/run/ag'>the run's AG report</a>" in first
    )
    assert "6 <span class='unit'>of 6</span>" in first and "5 <span class='unit'>of 6</span>" in later
    assert "AG3 not in use" in later and "AG exposures and 3 science visits" in later

    def error(tiles):
        return float(re.search(r"Guide error</span><span class='big'>([\d.]+)<", tiles).group(1))

    assert error(later) > 5 * error(first)
    # Only the night's rows are taken, and not the grid.
    night = agRun.nightTables(tables, "2026-11-03")
    assert len(night["exposures"]) == 90 and night["exposures"].taken_at.max() < START + pd.Timedelta(days=1)
    assert "grid" not in night and set(night["visits"].pfs_visit_id) == {153000, 153001, 153002}
    # Negative control: a night with no AG exposure, and no night at all, have no tiles.
    assert agRun.buildNightTiles(tables, "2026-11-09") == "" and agRun.buildNightTiles(tables, "") == ""


def testTheNightPageCarriesTheNightsAgTiles():
    from pfs.qa.dashboard.nightReport import buildNightReport

    tables = _tables((153000, "r1", "science", _science()))
    info = pd.DataFrame(
        {"visit": [153000], "time": [NIGHT + pd.Timedelta(hours=20)], "exptime": [900.0], "cameras": ["r1"]}
    )
    cache = AgRunCache(AgRunStore.fromTables(agRunTables(nights=1)))
    asked = []

    def agTiles(night):
        asked.append(night)
        return cache.nightTiles(night)

    page = buildNightReport(tables, info=info, agTiles=agTiles).html
    assert asked == ["2026-11-03"]
    assert "Auto-guiding this night, from 90 AG exposures" in page and "Guide stars matched" in page
    # They follow the page's own tiles.
    assert page.index("Auto-guiding this night") > page.index('<div class="tiles">')
    # Negative control: without them the page has no such row, and no placeholder left.
    plain = buildNightReport(tables, info=info).html
    assert "Auto-guiding this night" not in plain and "{{AG_TILES}}" not in plain


def testAFailureToMakeTheNightsTilesLeavesThemOut(monkeypatch):
    cache = AgRunCache(AgRunStore.fromTables(agRunTables(nights=1)))

    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(agRun, "buildNightTiles", fail)
    assert cache.nightTiles("2026-11-03") == ""
    # Negative control: made as usual, they are there.
    monkeypatch.undo()
    assert "Guide error" in AgRunCache(AgRunStore.fromTables(agRunTables(nights=1))).nightTiles("2026-11-03")


def testAgWindowIsTheRunTheVisitsAreIn():
    """The AG report covers the observing run of the latest visit, from the first visit's night on."""
    cli = _cli()
    run31 = (pd.Timestamp("2026-10-30 12:00"), pd.Timestamp("2026-12-01 12:00"))

    assert cli.agWindow([pd.Timestamp("2026-10-30 21:00"), pd.Timestamp("2026-11-10 03:00")]) == run31
    # Visits from the run's pre-run period on: the pre-run is covered too, from the first visit's night.
    preRun = cli.agWindow([pd.Timestamp("2026-10-25 15:00"), pd.Timestamp("2026-11-10 03:00")])
    assert preRun == (pd.Timestamp("2026-10-25 12:00"), run31[1])
    # A collection that reaches back over earlier runs is still this run's report, not all of them.
    assert cli.agWindow([pd.Timestamp("2026-08-30 21:00"), pd.Timestamp("2026-11-10 03:00")])[
        0
    ] == pd.Timestamp("2026-10-21 12:00")
    # Outside every known period: from the first visit's night to the day after the last.
    assert cli.agWindow([pd.Timestamp("2031-03-02 21:00"), pd.Timestamp("2031-03-04 01:00")]) == (
        pd.Timestamp("2031-03-02 12:00"),
        pd.Timestamp("2031-03-04 12:00"),
    )
    # Negative control: with no visit there is no run to place.
    assert cli.agWindow([]) is None and cli.agWindow(None) is None


def testALiveServeHasAnAgPageBeforeTheRunsFirstResult(monkeypatch):
    """Started before any visit is judged, the server reads the run under way now, without qadb's conditions."""
    import argparse

    cli = _cli()
    run = agRunTables(nights=1)
    opdb = Opdb(run)
    asked = []
    monkeypatch.setattr(
        cli,
        "agReaders",
        lambda *args: (opdb, lambda first, last: asked.append((first, last)) or conditions(run)),
    )
    args = argparse.Namespace(first_visit=0, last_visit=2**31 - 1, title="Run31 visit verdicts")
    noVisits = pd.DataFrame({"visit": [], "time": []})

    cache = cli.liveAgCache(args, object(), object(), noVisits)
    assert cache is not None and opdb.asked and opdb.asked[0][0] == cache.store.start
    assert cache.store.start <= pd.Timestamp.now() + pd.Timedelta(hours=14)
    # With visits, the run is theirs, and qadb's conditions are read for the visits its AG exposures were in.
    asked.clear()
    visits = pd.DataFrame({"visit": [153000], "time": [NIGHT + pd.Timedelta(hours=20)]})
    cache = cli.liveAgCache(args, object(), object(), visits)
    assert cache.store.start == START and asked[0] == (153000, 153002)
    # Negative control: without the opdb there is no AG report.
    assert cli.liveAgCache(args, None, object(), visits) is None


def _snapshot(directory, ag=True):
    """Write a snapshot of one science image, with a run's AG tables unless ``ag`` is False."""
    tables = {
        **_tables((153000, "r1", "science", _science())),
        "visitTimes": pd.DataFrame(
            {"pfs_visit_id": [153000], "time_exp_start": [NIGHT + pd.Timedelta(hours=20)]}
        ),
    }
    if ag:
        run = agRunTables(nights=1)
        tables.update(snapshotTables({**run, **conditions(run)}))
    writeSnapshot(tables, directory, {"collection": "c (qadb snapshot)", "builtAt": "2026-11-05T09:00:00"})
    return directory


def testReportWritesTheAgPageBesideTheRunReport(tmp_path, capsys):
    cli = _cli()
    snapshot = _snapshot(tmp_path / "snap")
    cli.main(["report", "--snapshot", str(snapshot), "--out", str(tmp_path / "out"), "--name", "run31"])

    agPage = (tmp_path / "out" / "run31-ag.html").read_text()
    runPage = (tmp_path / "out" / "run31.html").read_text()
    assert "<h1>Auto-guiding</h1>" in agPage and 'href="run31.html"' in agPage
    assert "read 2026-11-05." in agPage and "data:image/png;base64," in agPage
    assert "<a href='run31-ag.html'>The AG report</a>" in runPage
    assert "run31.html and run31-ag.html" in capsys.readouterr().out
    # Negative control: a snapshot with no AG tables writes the run report alone.
    cli.main(
        [
            "report",
            "--snapshot",
            str(_snapshot(tmp_path / "plain", ag=False)),
            "--out",
            str(tmp_path / "out2"),
        ]
    )
    assert not (tmp_path / "out2" / "index-ag.html").exists()
    assert "Auto-guiding" not in (tmp_path / "out2" / "index.html").read_text()


def testServeMountsTheAgPageOfASnapshot(tmp_path, monkeypatch):
    from tornado.httpclient import HTTPClientError

    cli = _cli()
    served = {}
    monkeypatch.setattr(
        cli, "listen", lambda application, address, port, show, debug=False: served.update(locals())
    )
    cli.main(["serve", "--snapshot", str(_snapshot(tmp_path / "snap")), "--title", "Run31 visit verdicts"])

    assert "<h1>Auto-guiding</h1>" in _fetch(served["application"], "/run/ag").body.decode()
    assert "<a href='/run/ag'>The AG report</a>" in _fetch(served["application"], "/run").body.decode()
    assert json.loads(_fetch(served["application"], "/run/status").body)["built"] == 1
    assert _fetch(served["application"], "/run/ag/camera/AG1.png").body[:4] == b"\x89PNG"
    # Negative control: a snapshot with no AG tables has no AG page.
    cli.main(["serve", "--snapshot", str(_snapshot(tmp_path / "plain", ag=False))])
    with pytest.raises(HTTPClientError) as error:
        _fetch(served["application"], "/run/ag")
    assert error.value.code == 404
    assert "Auto-guiding" not in _fetch(served["application"], "/run").body.decode()


def testSnapshotKeepsTheAgTablesReadFromTheOpdb(tmp_path, monkeypatch):
    """With the opdb, ``snapshot`` reads the run's AG tables once and keeps them; a failed read keeps the rest."""
    import sys
    import types

    from pfs.qa.dashboard import live

    tables = {
        **_tables((153000, "r1", "science", _science())),
        "visitTimes": pd.DataFrame(
            {"pfs_visit_id": [153000], "time_exp_start": [NIGHT + pd.Timedelta(hours=20)]}
        ),
    }
    for name in ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics", "qaGate"):
        tables[name]["decidedAt"] = pd.Timestamp("2026-11-04 21:00")
    gate = tables.pop("qaGate")
    monkeypatch.setattr(live, "readQadb", lambda *args, **kwargs: tables)
    monkeypatch.setattr(live, "readGate", lambda *args: gate)
    monkeypatch.setitem(sys.modules, "pfs.utils.database.qadb", types.SimpleNamespace(QaDB=lambda **k: None))
    monkeypatch.setitem(
        sys.modules, "pfs.utils.database.opdb", types.SimpleNamespace(OpDB=lambda **k: object())
    )
    cli = _cli()
    info = pd.DataFrame({"visit": [153000], "time": [NIGHT + pd.Timedelta(hours=20)]})
    monkeypatch.setattr("pfs.qa.dashboard.runReport.readVisitInfo", lambda opdb, first, last: info)
    run = agRunTables(nights=1)
    opdb = Opdb(run)
    monkeypatch.setattr(
        cli, "agReaders", lambda *args: (opdb, lambda first, last: conditions(run, first=first, last=last))
    )
    arguments = ["snapshot", "--collection", "c", "--opdb-host", "127.0.0.1", "--out"]

    cli.main([*arguments, str(tmp_path / "snap")])
    read, _ = readSnapshot(tmp_path / "snap")
    assert len(agTables(read)["exposures"]) == len(run["exposures"]) and "seeing" in agTables(read)
    # The run is read from the first visit's night, without a gap, and not beyond its end (Run 31's).
    assert opdb.asked[0][0] == pd.Timestamp("2026-11-03 12:00")
    assert all(later[0] == earlier[1] for earlier, later in zip(opdb.asked, opdb.asked[1:], strict=False))
    assert opdb.asked[-1][1] <= pd.Timestamp("2026-12-01 12:00")

    # Negative control: when the AG read fails, the snapshot is written without it.
    def fail(start, end):
        raise RuntimeError("no such table")

    monkeypatch.setattr(cli, "agReaders", lambda *args: (fail, lambda first, last: {}))
    cli.main([*arguments, str(tmp_path / "plain")])
    read, _ = readSnapshot(tmp_path / "plain")
    assert agTables(read) is None and "visitInfo" in read
