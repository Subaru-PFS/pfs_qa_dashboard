"""Tests for the night page (``nightReport``): one night of a collection, scoped from the shared tables."""

import re
from datetime import datetime

import pandas as pd

# The run report's fixtures build a qadb read's tables and the opdb's listing of visits taken.
from test_runReport import _science, _tables, _taken

from pfs.qa.dashboard import runReport
from pfs.qa.dashboard.nightReport import activeNight, buildNightReport, visitStub


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


def testTheVisitStubStandsAlone():
    page = visitStub(12345)
    assert page.startswith("<!doctype html>") and "<title>Visit 12345</title>" in page
    assert "<h1>Visit 12345</h1>" in page and 'href="/"' in page
    assert "@font-face" in page and not re.findall(r"""(?:src|href)=["']https?://""", page)
