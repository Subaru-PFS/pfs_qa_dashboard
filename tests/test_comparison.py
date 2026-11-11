"""Tests of the visit rollup and the comparison with reference visits, from the store to the page."""

import pandas as pd

from pfs.qa.dashboard.app import Dashboard
from pfs.qa.dashboard.comparison import ALL_VISITS, gridCells, nightOptions, nightVisits, visitDeviations
from pfs.qa.dashboard.figures import cellDetail, comparisonGrid, differenceText, visitStrip
from pfs.qa.dashboard.live import QadbMetricsStore
from pfs.qa.dashboard.qadbSource import referencesFromSnapshot, referenceSnapshotTables

_LINES = ["visit", "arm", "spectrograph", "decidedAt"]


def _image(visit, medFwhm, status="PASS", reason="", arm="b"):
    return {
        "visit": visit,
        "arm": arm,
        "spectrograph": 1,
        "obsType": "arc",
        "seqName": "arc",
        "illumination": "full",
        "traceOnly": False,
        "medFwhm": medFwhm,
        **dict.fromkeys(
            ("medDxCenter", "dxCenterRms", "pctFlagged", "linesPerFiber", "nLines"), float("nan")
        ),
        "qaStatus": status,
        "qaReason": reason,
        "calexpRun": "",
        "decidedAt": "t1",
    }


def _tables(images):
    iq = pd.DataFrame(images)
    return [iq, pd.DataFrame(columns=[*_LINES, "obsType"])]


def _store(images, references=None, nights=None):
    """Return a store of ``images``; visits 11xx are on the night of 2026-11-01, 12xx on the next."""

    def read(since):
        iq = pd.DataFrame(images if since is None else [])
        visits = sorted(set(iq["visit"])) if len(iq) else []
        return {
            "iqQaMetrics": iq,
            "dmLineResidualSummary": pd.DataFrame(columns=_LINES),
            "dmLineResidualMetrics": pd.DataFrame(columns=_LINES),
            "visitTimes": pd.DataFrame(
                {
                    "pfs_visit_id": visits,
                    "time_exp_start": [
                        pd.Timestamp("2026-11-01 23:30")
                        + pd.Timedelta(days=(v // 100) - 11, minutes=20 * (v % 100))
                        for v in visits
                    ],
                    "iic_sequence_id": pd.array([pd.NA] * len(visits), dtype="Int64"),
                }
            ),
        }

    return QadbMetricsStore(read, {}, minInterval=0, references=references)


# One visit of four is 10 % wider than the reference; the night's median is not.
_NIGHT = [_image(1101, 3.0), _image(1102, 3.0), _image(1103, 3.0), _image(1104, 3.3, "FAIL", "medFwhm>3.2")]
_REFERENCE = {"firstNight": _tables([_image(1001, 3.0), _image(1002, 3.0), _image(1003, 3.0)])}


def testRollupNamesWhatDecidedEachVisit():
    rollup = _store(_NIGHT).rollup().set_index("visit")
    assert rollup.loc[1104, "qaStatus"] == "FAIL" and rollup.loc[1104, "decidedByCamera"] == "b1"
    assert rollup.loc[1104, "decidedByTask"] == "imageQualityQa" and "medFwhm" in rollup.loc[1104, "qaReason"]
    # Negative control: a visit whose images pass is not decided by anything.
    assert rollup.loc[1101, "qaStatus"] == "PASS" and not rollup.loc[1101, "qaReason"]


def testOneVisitBeyondToleranceShowsThoughTheMedianPasses():
    store = _store(_NIGHT, _REFERENCE)
    comparison = store.comparison()
    deviations = visitDeviations(store.metricValues(), comparison)
    cells = gridCells(comparison, deviations)
    cell = cells[cells["metric"] == "medFwhm"].iloc[0]
    assert cell["status"] == "PASS" and cell["camera"] == "b1" and cell["row"] == "medFwhm · arc"
    assert (cell["nBeyond"], cell["nVisits"]) == (1, 4)
    beyond = deviations[deviations["status"] == "FAIL"]
    assert list(beyond["visit"]) == [1104] and abs(beyond["relative"].iloc[0] - 0.1) < 1e-9
    # Negative control: narrower than the reference by as much is not beyond a higher-is-worse tolerance.
    narrower = _store([*_NIGHT[:3], _image(1104, 2.7)], _REFERENCE)
    cells = gridCells(narrower.comparison(), visitDeviations(narrower.metricValues(), narrower.comparison()))
    assert cells[cells["metric"] == "medFwhm"].iloc[0]["nBeyond"] == 0


def testComparisonIsOfTheVisitsAskedFor():
    store = _store([*_NIGHT, _image(1201, 3.3), _image(1202, 3.3)], _REFERENCE)
    later = store.comparison([1201, 1202])
    assert later["status"].tolist() == ["FAIL"] and later["nRun"].tolist() == [2]
    # Negative control: the first night's own median passes, and without references nothing is compared.
    assert store.comparison([1101, 1102, 1103, 1104])["status"].tolist() == ["PASS"]
    assert _store(_NIGHT).comparison().empty


def testNightsAreHstEvenings():
    store = _store([*_NIGHT, _image(1201, 3.3)], _REFERENCE)
    dashboard = Dashboard(store)
    assert nightOptions(dashboard.timeline) == ["2026-11-02", "2026-11-01", ALL_VISITS]
    # 23:50 and 00:10 the next morning are one night; the latest night is the one shown.
    assert nightVisits(dashboard.timeline, "2026-11-01") == [1101, 1102, 1103, 1104]
    assert dashboard.night == "2026-11-02"
    # Negative control: the calendar date would have split the first night.
    first = dashboard.timeline[dashboard.timeline["night"] == "2026-11-01"]
    assert first["time"].dt.strftime("%Y-%m-%d").nunique() == 2


def testDashboardShowsTheWorstCellAndTheOneChosen():
    images = [*_NIGHT, *(_image(v, 3.3, arm="r") for v in (1101, 1102))]
    reference = {"firstNight": _tables([_image(1001, 3.0), _image(1001, 3.0, arm="r")])}
    dashboard = Dashboard(_store(images, reference))
    assert dashboard.shownCell() == ("firstNight", "medFwhm", "arc", "r1")
    assert "✕ FAIL" in dashboard.detailHtml() and "2 of 2: 1101, 1102" in dashboard.detailHtml()
    dashboard.selectCell(("firstNight", "medFwhm", "arc", "b1"))
    text = dashboard.detailHtml()
    assert "medFwhm · arc · b1" in text and "✓ PASS" in text and "1 of 4: 1104" in text
    # Negative control: a cell the night does not have falls back to the worst.
    dashboard.selectCell(("firstNight", "medFwhm", "arc", "n1"))
    assert dashboard.shownCell()[-1] == "r1"
    # The panes build.
    assert dashboard.grids() is not None and dashboard.detail() is not None and dashboard.strip() is not None


def testWithoutReferencesThePageSaysHowToNameThem():
    dashboard = Dashboard(_store(_NIGHT))
    assert "--reference" in dashboard.grids().object and dashboard.shownCell() is None
    # Negative control: with references it is a grid.
    assert not hasattr(Dashboard(_store(_NIGHT, _REFERENCE)).grids(), "object")


def testFiguresDrawWhatTheTablesHold():
    store = _store(_NIGHT, _REFERENCE)
    dashboard = Dashboard(store)
    strip = visitStrip(dashboard.rollup, dashboard.timeline, dashboard.night)
    drawn = {r.glyph.marker: len(r.data_source.data["visit"]) for r in strip.renderers}
    assert drawn == {"circle": 3, "diamond": 1}
    comparison, deviations, cells = dashboard.compared()
    chosen = []
    grid = comparisonGrid(cells, onSelect=chosen.append)
    source = grid.renderers[0].data_source
    assert list(source.data["text"]) == ["✓ +0.0%"] and list(source.data["visitsText"]) == ["✕ 1/4"]
    source.selected.indices = [0]
    assert chosen == [("firstNight", "medFwhm", "arc", "b1")]
    detail = cellDetail(deviations.assign(time=pd.NaT), comparison.iloc[0])
    points = {
        r.glyph.marker: list(r.data_source.data["visit"])
        for r in detail.renderers
        if len(r.data_source.data["x"])
    }
    # Negative control: the visit beyond tolerance is drawn apart from those within.
    assert points == {"circle": [1101, 1102, 1103], "diamond": [1104]}


def testMetricWithoutToleranceSaysTheAbsoluteDifference():
    """A reference median near zero makes a relative difference meaningless; the cell says the units'."""

    def offset(visit, value):
        return {**_image(visit, 3.0), "medDxCenter": value}

    store = _store([offset(1101, 0.021), offset(1102, 0.021)], {"firstNight": _tables([offset(1001, 0.001)])})
    comparison = store.comparison()
    cells = gridCells(comparison, visitDeviations(store.metricValues(), comparison))
    cell = cells[cells["metric"] == "medDxCenter"].iloc[0]
    assert cell["status"] == "UNKNOWN" and abs(cell["difference"] - 0.02) < 1e-12
    assert differenceText("medDxCenter", cell["relative"], cell["difference"]) == "+0.02"
    # Negative control: its relative difference is 2000 %, and a metric with a tolerance still says that.
    assert abs(cell["relative"] - 20.0) < 1e-9 and differenceText("medFwhm", 0.1, 0.3) == "+10.0%"


def testReferencesSurviveASnapshot():
    tables = referenceSnapshotTables({"a": _tables([_image(1001, 3.0)]), "b": _tables([_image(2001, 4.0)])})
    assert set(tables) == {"referenceIqQaMetrics", "referenceLineMetrics"}
    back = referencesFromSnapshot(tables, ["a", "b"])
    assert back["b"][0]["visit"].tolist() == [2001] and "referenceName" not in back["b"][0]
    # Negative control: the tables are taken out of the snapshot's, and one without them has no references.
    assert tables == {} and referencesFromSnapshot({}, ["a"]) == {}
