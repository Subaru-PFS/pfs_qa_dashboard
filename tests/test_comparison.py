"""Tests of the visit rollup and the comparison with reference visits, from the store to the page."""

import pandas as pd
import panel as pn

from pfs.qa.dashboard.app import Dashboard
from pfs.qa.dashboard.comparison import (
    ALL_VISITS,
    SMALL_POPULATION,
    gridCells,
    judgedCells,
    nightOptions,
    nightVisits,
    rowOrder,
    visitDeviations,
)
from pfs.qa.dashboard.figures import UNTIMED, cellDetail, comparisonGrid, differenceText, visitStrip
from pfs.qa.dashboard.live import QadbMetricsStore
from pfs.qa.dashboard.qadbSource import referencesFromSnapshot, referenceSnapshotTables

_LINES = ["visit", "arm", "spectrograph", "decidedAt"]


def _image(visit, medFwhm, status="PASS", reason="", arm="b", illumination="full", decidedAt="t1"):
    return {
        "visit": visit,
        "arm": arm,
        "spectrograph": 1,
        "obsType": "arc",
        "seqName": "arc",
        "illumination": illumination,
        "traceOnly": False,
        "medFwhm": medFwhm,
        **dict.fromkeys(
            ("medDxCenter", "dxCenterRms", "pctFlagged", "linesPerFiber", "nLines"), float("nan")
        ),
        "qaStatus": status,
        "qaReason": reason,
        "calexpRun": "",
        "decidedAt": decidedAt,
    }


def _tables(images):
    iq = pd.DataFrame(images)
    return [iq, pd.DataFrame(columns=[*_LINES, "obsType"])]


def _store(images, references=None, nights=None):
    """Return a store of ``images``; visits 11xx are on the night of 2026-11-01, 12xx on the next.

    Images added to the list afterwards, with a later ``decidedAt``, come with the next refresh.
    """

    def read(since):
        iq = pd.DataFrame([row for row in images if since is None or row["decidedAt"] >= since])
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
    # The panes hold a grid for the reference, the cell's chart with its populations, and the strip.
    assert sum(isinstance(pane, pn.pane.Bokeh) for pane in dashboard.grids()) == 1
    _, chooser, chart = dashboard.detail()
    assert chooser.options == ["full"] and isinstance(chart, pn.pane.Bokeh)
    assert isinstance(dashboard.strip(), pn.pane.Bokeh)


def testACellCanBeChosenOnceTheDetailIsShown():
    """The served page draws the detail before any click: choosing a cell then must not fail."""
    images = [*_NIGHT, *(_image(v, 3.3, arm="r") for v in (1101, 1102))]
    reference = {"firstNight": _tables([_image(1001, 3.0), _image(1001, 3.0, arm="r")])}
    dashboard = Dashboard(_store(images, reference))
    dashboard.detail()
    dashboard.selectCell(("firstNight", "medFwhm", "arc", "b1"))
    assert "medFwhm · arc · b1" in dashboard.detailHtml()
    dashboard.detail()
    dashboard.selectCell(("firstNight", "medFwhm", "arc", "r1"))
    assert dashboard.shownCell()[-1] == "r1" and dashboard.population is None


# Two populations of one cell, both beyond FAIL: Ar by 6.7 %, Ne by 20 %. Ar is the first read.
_LAMPS = [
    _image(1101, 3.2, illumination="Ar"),
    _image(1102, 3.2, illumination="Ar"),
    _image(1103, 3.6, illumination="Ne"),
    _image(1104, 3.6, illumination="Ne"),
]
_LAMP_REFERENCE = {
    "firstNight": _tables([_image(1001, 3.0, illumination="Ar"), _image(1002, 3.0, illumination="Ne")])
}


def testDetailOpensOnThePopulationTheCellShows():
    dashboard = Dashboard(_store(_LAMPS, _LAMP_REFERENCE))
    comparison, _, cells = dashboard.compared()
    assert cells["population"].tolist() == ["Ne"] and cells["nPopulations"].tolist() == [2]
    rows, population = dashboard._shownPopulation()
    assert population == "Ne" and list(rows["population"]) == ["Ne", "Ar"]
    assert "population Ne" in dashboard.detailHtml() and "+20.0%" in dashboard.detailHtml()
    # Negative control: both are FAIL, so the status alone would have left them as read, Ar first.
    assert comparison["status"].tolist() == ["FAIL", "FAIL"]
    assert comparison["illumination"].tolist() == ["Ar", "Ne"]


def testThePopulationChosenShowsUntilTheCellOrTheNightChanges():
    images = [*_LAMPS, _image(1201, 3.6, illumination="Ar"), _image(1202, 3.2, illumination="Ne")]
    dashboard = Dashboard(_store(images, _LAMP_REFERENCE))
    dashboard.night = "2026-11-01"
    chooser = dashboard.detail()[1]
    assert chooser.value == "Ne" and chooser.options == ["Ne", "Ar"]
    chooser.value = "Ar"
    assert dashboard.population == "Ar" and "population Ar" in dashboard.detailHtml()
    assert dashboard.detail()[1].value == "Ar"
    # Another night's cell opens on its own worst, which there is Ar; so does a cell chosen anew.
    dashboard.night = "2026-11-02"
    assert dashboard.population is None and dashboard.detail()[1].value == "Ar"
    dashboard.detail()[1].value = "Ne"
    dashboard.selectCell(dashboard.shownCell())
    assert dashboard.detail()[1].value == "Ar"
    # Negative control: within one night and cell the choice is kept, though it is not the worst.
    dashboard.detail()[1].value = "Ne"
    assert dashboard.detail()[1].value == "Ne"


def testARefreshKeepsWhatIsChosen():
    images = [*_NIGHT, _image(1201, 3.3)]
    dashboard = Dashboard(_store(images, _REFERENCE))
    for chosen in (ALL_VISITS, "2026-11-01"):
        dashboard.night = chosen
        version = dashboard.dataVersion
        images.append(_image(1300 + len(images), 3.3, decidedAt=f"t{len(images)}"))
        dashboard.refresh()
        assert dashboard.dataVersion == version + 1 and dashboard.night == chosen
    # The comparison is of the visits now held: every one, the night after the last refresh included.
    dashboard.night = ALL_VISITS
    assert dashboard.compared()[0]["nRun"].tolist() == [len(images)]
    assert "2026-11-03" in dashboard.param.night.objects
    # Negative control: a dashboard started now opens on the latest night, not on every visit.
    assert Dashboard(_store(images, _REFERENCE)).night == "2026-11-03"


def testAPopulationTheReferenceLacksIsNotGivenNumbers():
    images = [*_NIGHT, _image(1101, 3.3, arm="r")]
    dashboard = Dashboard(_store(images, _REFERENCE))
    dashboard.selectCell(("firstNight", "medFwhm", "arc", "r1"))
    text = dashboard.detailHtml()
    assert "medFwhm · arc · r1" in text and "the reference has no values" in text and "nan" not in text
    # Negative control: a cell the reference has says both medians.
    dashboard.selectCell(("firstNight", "medFwhm", "arc", "b1"))
    assert "vs 3 (n=3)" in dashboard.detailHtml()


def testACellOfFewImagesIsAnOutlineWithItsCount():
    def grid(nReference):
        reference = {"firstNight": _tables([_image(1001 + i, 3.0) for i in range(nReference)])}
        store = _store(_NIGHT, reference)
        cells = gridCells(store.comparison(), visitDeviations(store.metricValues(), store.comparison()))
        cell = cells[cells["metric"] == "medFwhm"].iloc[0]
        fill, outline = comparisonGrid(cells).renderers[:2]
        row = list(fill.data_source.data["metric"]).index("medFwhm")
        return cell, fill.data_source.data, row, list(outline.data_source.data["metric"])

    # Four of the night's images against two of the reference's: the smaller side counts.
    cell, data, row, outlined = grid(2)
    assert (cell["nRun"], cell["nReference"]) == (4, 2) and SMALL_POPULATION == 3
    assert (
        data["fillAlpha"][row] < 0.2 and data["visitsText"][row].startswith("n=2") and "medFwhm" in outlined
    )
    # The visit beyond the tolerance is still counted beside it.
    assert data["visitsText"][row].endswith("1/4")
    # Negative control: with four images on each side the cell is filled, with no count and no outline.
    cell, data, row, outlined = grid(4)
    assert (cell["nRun"], cell["nReference"]) == (4, 4)
    assert data["fillAlpha"][row] > 0.8 and "n=" not in data["visitsText"][row] and "medFwhm" not in outlined


def testUnjudgedCellsAreHiddenUntilAskedFor():
    # linesPerFiber declares no tolerance, so its cell has no verdict; medFwhm's has.
    def counted(visit, medFwhm):
        return {**_image(visit, medFwhm), "linesPerFiber": 100.0}

    reference = {"firstNight": _tables([counted(1001, 3.0), counted(1002, 3.0)])}
    dashboard = Dashboard(_store([counted(1101, 3.0), counted(1102, 3.0)], reference))
    cells = dashboard.compared()[2]
    assert dict(zip(cells["metric"], cells["status"], strict=True)) == {
        "medFwhm": "PASS",
        "linesPerFiber": "UNKNOWN",
    }
    judged, hiddenRows = judgedCells(cells)
    assert judged["metric"].tolist() == ["medFwhm"] and hiddenRows == 1

    def drawn():
        grids = [pane.object for pane in dashboard.grids() if isinstance(pane, pn.pane.Bokeh)]
        notes = [pane.object for pane in dashboard.grids() if isinstance(pane, pn.pane.HTML)]
        return list(grids[0].y_range.factors), any("unjudged cells are hidden" in note for note in notes)

    assert dashboard.hideUnjudged and drawn() == (["medFwhm · arc"], True)
    # Negative control: cleared, the row without a verdict is drawn and nothing is said to be hidden.
    dashboard.hideUnjudged = False
    assert drawn() == (["linesPerFiber · arc", "medFwhm · arc"], False)


def testGridRowsKeepTheirOrderWhateverTheirStatus():
    def both(visit, medFwhm, offset, obsType="arc"):
        return {**_image(visit, medFwhm), "medDxCenter": offset, "obsType": obsType}

    reference = {"firstNight": _tables([both(1001, 3.0, 0.001), both(1002, 3.0, 0.001, "quartz")])}

    def rows(arcFwhm, quartzFwhm):
        store = _store([both(1101, arcFwhm, 0.5), both(1102, quartzFwhm, 0.5, "quartz")], reference)
        cells = gridCells(store.comparison(), visitDeviations(store.metricValues(), store.comparison()))
        grid = comparisonGrid(cells)
        return cells, rowOrder(cells), list(grid.y_range.factors)

    cells, order, drawn = rows(3.0, 3.6)
    expected = ["medFwhm · arc", "medFwhm · quartz", "medDxCenter · arc", "medDxCenter · quartz"]
    assert order == expected and drawn == expected[::-1]
    # Negative control: the cells themselves come worst first, the failing quartz row ahead of the arc.
    assert cells["row"].tolist()[0] == "medFwhm · quartz"
    assert rows(3.6, 3.0)[1] == expected and rows(3.6, 3.0)[0]["row"].tolist()[0] == "medFwhm · arc"


def testAVisitWithoutATimeHasARowOfItsOwn():
    dashboard = Dashboard(_store([*_NIGHT, _image(1201, 3.0)], _REFERENCE))
    timeline = dashboard.timeline.copy()
    timeline.loc[timeline["visit"] == 1102, ["time", "night"]] = [pd.NaT, ""]
    strip = visitStrip(dashboard.rollup, timeline, "2026-11-01")
    assert list(strip.y_range.factors) == [UNTIMED, "2026-11-01 ▸", "2026-11-02"]
    placed = {
        visit: row
        for renderer in strip.renderers
        for visit, row in zip(
            renderer.data_source.data["visit"], renderer.data_source.data["row"], strict=True
        )
    }
    assert placed[1102] == UNTIMED and placed[1101] == "2026-11-01 ▸" and len(placed) == 5
    # Negative control: with no times at all there are no nights to draw, only the visits in order.
    none = timeline.assign(time=pd.NaT, night="")
    assert list(visitStrip(dashboard.rollup, none).y_range.factors) == ["visits"]


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
    assert list(source.data["text"]) == ["✓ +0.0%"] and list(source.data["visitsText"]) == ["n=3  ✕ 1/4"]
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
