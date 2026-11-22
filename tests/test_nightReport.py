"""Tests for the night page (``nightReport``): one night of a collection, scoped from the shared tables."""

import re
from datetime import datetime

import pandas as pd

# The run report's fixtures build a qadb read's tables and the opdb's listing of visits taken.
from test_runReport import _science, _tables, _taken

from pfs.qa.dashboard import runReport
from pfs.qa.dashboard.nightReport import activeNight, buildNightReport


def _info(visits, times, sequences=None):
    """Return a minimal visit-information table (as `runReport.readVisitInfo`) for ``visits`` at ``times``."""
    return pd.DataFrame(
        {
            "visit": visits,
            "time": pd.to_datetime(times),
            "exptime": [900.0] * len(visits),
            "sequenceType": sequences or ["scienceObject"] * len(visits),
        }
    )


def testActiveNightIsTheMostRecentAndScopesToIt():
    tables = _tables(
        (1, "r1", "science", _science(x=0.1)),
        (2, "r1", "science", _science(x=2.0), {"gate": ("FAIL", "x offset +2.00 px >= fail 1")}),
    )
    # Two nights: visit 1 on the evening of 2026-11-04, visit 2 on 2026-11-05.
    info = _info([1, 2], ["2026-11-04 23:00", "2026-11-05 23:00"])
    allVisits = runReport.visitTable(runReport.measure(tables), info)
    assert activeNight(allVisits) == "2026-11-05"

    made = buildNightReport(tables, info=info)
    assert made.visits["night"].unique().tolist() == ["2026-11-05"]
    assert made.visits["visit"].tolist() == [2]
    # The clock's rows are only the latest night's; the earlier night's visit is gone.
    assert 'const DATA = [[2,"2026-11-05 23:00"' in made.html and '[1,"' not in made.html
    # A chosen night overrides the default.
    other = buildNightReport(tables, info=info, night="2026-11-04")
    assert other.visits["visit"].tolist() == [1]


def testTheNightPageStandsAloneAndLinks():
    tables = _tables((10, "b1", "science", _science(heldBack=0.1, species="OI")))
    info = _info([10], ["2026-11-04 21:00"])
    made = buildNightReport(
        tables, info=info, title="Run99 <x>", source="from <code>c</code>", now=datetime(2026, 11, 5)
    )
    page = made.html
    assert not re.findall(r"\{\{[A-Z_]+\}\}", page)
    assert page.startswith("<!doctype html>") and "<title>Run99 &lt;x&gt;</title>" in page
    # Its fonts travel inside it and nothing is fetched.
    assert page.count("@font-face") == 2 and "url(data:font/woff2;base64," in page
    assert not re.findall(r"""(?:src|href)=["'](?:https?:)?//""", page) and "url(http" not in page
    # It links to the run report and to the reserved per-visit route, and the clock reads the visit rows.
    assert 'href="/run"' in page and "visit/10" in page
    assert "const DATA = [[10," in page and "The night on the clock" in page


def testTheNightShowsTheSharedScienceBars():
    tables = _tables(
        (1, "r1", "science", _science(x=0.2, heldBack=0.3)),
        (2, "r1", "science", _science(x=-0.1, heldBack=-0.2)),
    )
    info = _info([1, 2], ["2026-11-04 20:00", "2026-11-04 20:10"])
    page = buildNightReport(tables, info=info).html
    # The same SVG bars as the run report (drp_qa's limits), drawn from `runReport.scienceSections`.
    assert 'svg class="bar"' in page and "Science frames tonight: offsets" in page
    assert "r arm" in page and "OH lines" in page


def testTheLatestTileNamesTheMostRecentVisit():
    tables = _tables(
        (1, "r1", "science", _science(), {"gate": ("PASS", "")}),
        (2, "r1", "science", _science(), {"gate": ("FAIL", "x offset +2.00 px >= fail 1")}),
    )
    page = buildNightReport(tables, info=_info([1, 2], ["2026-11-04 20:00", "2026-11-04 21:00"])).html
    tile = page[page.index("Latest visit") : page.index("The night on the clock")]
    # The lead tile is the later visit (2) and its verdict, not the earlier one.
    assert "<span class='big'>2</span>" in tile and "FAIL" in tile
    assert "<span class='big'>1</span>" not in tile


def testVisitsWithNoResultWithinTheNightAreShown():
    tables = _tables((2, "r1", "science", _science()), (5, "r1", "science", _science()))
    info = _taken(
        (2, "object", "scienceObject", False, "completed"),
        (3, "object", "scienceObject", False, "abortRequested"),
        (5, "object", "scienceObject", False, "completed"),
    )
    made = buildNightReport(tables, info=info)
    assert "Visits with no QA result tonight (1)" in made.html
    assert (made.visits["status"] == "NO RESULT").sum() == 1
    assert "sequence ended: abortRequested" in made.html


def _taken_info(rows):
    """Return a visit-information table (as `runReport.readVisitInfo`) with explicit times and types."""
    return pd.DataFrame(
        {
            "visit": [r[0] for r in rows],
            "time": pd.to_datetime([r[1] for r in rows]),
            "exptime": 600.0,
            "sequenceType": "scienceObject",
            "expType": "object",
            "cameras": "b1 r1",
            "iis": False,
            "sequenceEnd": [r[2] for r in rows],
        }
    )


def testTheNightFlipsToTheLatestExposureAwaitingQa():
    tables = _tables((1, "r1", "science", _science(), {"gate": ("PASS", "")}))
    # Visit 1 is judged on 2026-11-04; visit 2 was taken on the next night and has no verdict yet.
    info = _taken_info([(1, "2026-11-04 23:00", "completed"), (2, "2026-11-05 20:00", None)])
    made = buildNightReport(tables, info=info)
    # The page flips to the newer night and shows the exposure awaiting QA.
    assert made.visits["night"].unique().tolist() == ["2026-11-05"]
    assert made.visits.set_index("visit").loc[2, "status"] == "AWAITING"
    assert "Awaiting QA (1)" in made.html and "awaiting QA" in made.html
    # The latest tile is that exposure.
    assert "<span class='big'>2</span>" in made.html[: made.html.index("The night on the clock")]
    # Negative control: the run report never shows a visit newer than the latest judged.
    runVisits = runReport.visitTable(runReport.measure(tables), info)
    assert 2 not in runVisits["visit"].tolist()
    # Negative control: without awaiting, the night would stay on the judged visit's night.
    plain = runReport.visitTable(runReport.measure(tables), info)
    assert activeNight(plain) == "2026-11-04"


def testAnAwaitingArcShowsItsLamp():
    tables = _tables((1, "r1", "science", _science(), {"gate": ("PASS", "")}))
    info = pd.DataFrame(
        {
            "visit": [1, 2],
            "time": pd.to_datetime(["2026-11-04 23:00", "2026-11-05 20:00"]),
            "exptime": 600.0,
            "sequenceType": ["scienceObject", "scienceArc"],
            "sequenceName": [None, "Arc: Neon"],
            "expType": ["object", "arc"],
            "cameras": "b1 r1",
            "iis": False,
            "sequenceEnd": ["completed", None],
        }
    )
    made = buildNightReport(tables, info=info)
    row = made.visits.set_index("visit").loc[2]
    # The arc's lamp comes from the opdb sequence name even before QA runs.
    assert row["status"] == "AWAITING" and row["label"] == "arc, Neon"
    assert "arc, Neon" in made.html


def _reader(exposures):
    """Return a ``read(first, last)`` giving the exposures whose visit is in the range."""

    def read(first, last):
        return exposures[(exposures["visit"] >= first) & (exposures["visit"] <= last)].reset_index(drop=True)

    return read


def _conditionFrame(start):
    """Return a reader-shaped frame (``visit``, ``agcExposureId``, ``time``, ``value``) for visit 1."""
    return pd.DataFrame(
        {
            "visit": 1,
            "agcExposureId": range(30),
            "time": pd.date_range("2026-11-04 20:00", periods=30, freq="1min"),
            "value": pd.Series(range(30)).mul(0.002).add(start),
        }
    )


def testTheNightShowsAgConditionsWhenReadersGiveThem():
    tables = _tables((1, "r1", "science", _science()))
    info = _info([1], ["2026-11-04 20:00"])
    made = buildNightReport(
        tables,
        info=info,
        readTransparency=_reader(_conditionFrame(0.76)),
        readSeeing=_reader(_conditionFrame(0.60)),
    )
    # The Conditions tile (opening the modal) replaces the Science tile; the modal has a chart per metric.
    assert "class='tile cond'" in made.html and "aria-controls='conditions'" in made.html
    assert "id='conditions'" in made.html and "AG sky conditions tonight" in made.html
    assert "<h3>Seeing</h3>" in made.html and "<h3>Sky transparency</h3>" in made.html
    assert "<span class='lab'>Science</span>" not in made.html
    # Negative control: without readers there is no Conditions tile or modal; the Science tile stays.
    plain = buildNightReport(tables, info=info).html
    assert "AG sky conditions tonight" not in plain and "id='conditions'" not in plain
    assert "class='tile cond'" not in plain and "<span class='lab'>Science</span>" in plain


def testAConditionsReadThatFailsDropsOnlyThatMetric():
    tables = _tables((1, "r1", "science", _science()))
    info = _info([1], ["2026-11-04 20:00"])

    def boom(first, last):
        raise RuntimeError("qadb unreachable")

    made = buildNightReport(
        tables, info=info, readTransparency=boom, readSeeing=_reader(_conditionFrame(0.6))
    )
    # The page is built; seeing shows and the failed transparency read is dropped, not blanking the page.
    assert made.html.startswith("<!doctype html>") and "The night on the clock" in made.html
    assert "AG sky conditions tonight" in made.html and "<h3>Seeing</h3>" in made.html
    assert "<h3>Sky transparency</h3>" not in made.html
    # Both failing: no Conditions tile or modal, the Science tile stays.
    both = buildNightReport(tables, info=info, readTransparency=boom, readSeeing=boom).html
    assert "id='conditions'" not in both and "<span class='lab'>Science</span>" in both


def testAwaitingAndNoResultAreToldApart():
    tables = _tables((2, "r1", "science", _science()), (5, "r1", "science", _science()))
    # Visit 3 was taken before the latest judged (5) and passed over; visit 7 after it, still awaiting.
    info = _taken_info(
        [
            (2, "2026-11-04 20:00", "completed"),
            (3, "2026-11-04 20:10", "abortRequested"),
            (5, "2026-11-04 20:20", "completed"),
            (7, "2026-11-04 20:40", None),
        ]
    )
    made = buildNightReport(tables, info=info)
    status = made.visits.set_index("visit")["status"]
    assert status.loc[3] == "NO RESULT" and status.loc[7] == "AWAITING"
    assert "Awaiting QA (1)" in made.html and "Visits with no QA result tonight (1)" in made.html
