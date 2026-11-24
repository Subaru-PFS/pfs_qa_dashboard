"""A reduceExposure failure shows as a distinct REDUCE FAILED verdict, not AWAITING / NO RESULT."""

import json
import re

from test_nightReport import _info
from test_runReport import _science, _tables

from pfs.qa.dashboard.nightReport import buildNightReport
from pfs.qa.dashboard.runReport import (
    REDUCE_FAILED,
    STATUS_RANK,
    _foldedCounts,
    buildReport,
    imageTable,
    visitTable,
    worst,
)
from pfs.qa.dashboard.visitPage import buildVisitPage

_FAILED = (REDUCE_FAILED, "reduceExposure failed (returnCode 1: FAILED)")


def testOutranksAFailAndIsKeptAsAVerdict():
    # Ranked worst, so a visit with any REDUCE FAILED detector reads REDUCE FAILED.
    assert STATUS_RANK[REDUCE_FAILED] > STATUS_RANK["FAIL"]
    assert worst(["PASS", "FAIL", REDUCE_FAILED]) == REDUCE_FAILED
    # The status is kept, not coerced to UNKNOWN the way an unknown string is.
    tables = _tables((1, "r1", "science", _science(), {"gate": _FAILED}))
    assert imageTable(tables).set_index("visit").loc[1, "status"] == REDUCE_FAILED
    assert visitTable(imageTable(tables)).set_index("visit").loc[1, "status"] == REDUCE_FAILED


def testFoldedIntoFailForAggregateCounts():
    tables = _tables(
        (1, "r1", "science", _science(), {"gate": _FAILED}),
        (2, "r1", "science", _science(), {"gate": ("FAIL", "OH residual high")}),
    )
    # The per-visit verdicts stay distinct, but the aggregate counts a reduce failure as a failure.
    counts = _foldedCounts(visitTable(imageTable(tables)))
    assert counts.get("FAIL") == 2 and REDUCE_FAILED not in counts


def testRunReportShowsADistinctRedfailChipAndNotAwaiting():
    tables = _tables((1, "r1", "science", _science(), {"gate": _FAILED}))
    page = buildReport(tables, info=_info([1], ["2026-11-04 22:00"])).html
    # The "Visits that do not pass" table carries a distinct redfail chip (not the pale FAIL chip).
    assert "<span class='chip redfail'>REDUCE FAILED</span>" in page
    # Negative control: redfail is its own class, and no visit is rendered as awaiting (the labels in the
    # clock legend are always there; a visit classed awaiting/no-result would carry an 'awaiting'/'none' chip).
    assert "chip redfail" != "chip fail"
    assert "chip awaiting" not in page
    # The Every-visit table data carries the verdict (JS colours it via the CLS map).
    data = re.search(r"const DATA = (\[.*?\]);\n", page).group(1)
    assert "REDUCE FAILED" in data


def testNightPageShowsReduceFailedInTheFeed():
    tables = _tables((1, "r1", "science", _science(), {"gate": _FAILED}))
    page = buildNightReport(tables, info=_info([1], ["2026-11-04 22:00"])).html
    assert "redfail" in page and "REDUCE FAILED" in page
    # The latest-visit tile reads the failure plainly.
    assert "reduceExposure failed" in page


def testVisitPageShowsTheReduceFailedVerdict():
    tables = _tables(
        (7, "r1", "science", _science(), {"gate": _FAILED}),
        (7, "b1", "science", _science(), {"gate": _FAILED}),
    )
    page = buildVisitPage(tables, _info([7], ["2026-11-04 22:00"]), 7)
    # The visit verdict and each detector read REDUCE FAILED with the redfail class.
    assert "chip verdict redfail" in page and page.count("chip redfail") >= 1
    assert "REDUCE FAILED" in page


def testVisitPageShowsAnErrorBannerAndNoEmptyPlots():
    tables = _tables(
        (7, "r1", "science", _science(), {"gate": _FAILED}),
        (7, "b1", "science", _science(), {"gate": _FAILED}),
    )
    # Even with the Butler serving plots, a failed reduction shows the error, not empty plot placeholders.
    page = buildVisitPage(tables, _info([7], ["2026-11-04 22:00"]), 7, residuals=True, extraction=True)
    assert "<section class='block error'>" in page and "Reduction failed" in page
    assert "reduceExposure failed (returnCode 1: FAILED)" in page
    # Negative control: the (empty) plot sections and metric grid are suppressed for a failed reduction.
    assert "class='residuals'" not in page and "class='extraction'" not in page

    # A visit that reduced normally still shows its plot sections (the banner is only for a failure).
    ok = _tables((8, "r1", "science", _science(), {"gate": ("PASS", "")}))
    okPage = buildVisitPage(ok, _info([8], ["2026-11-04 22:00"]), 8, residuals=True, extraction=True)
    assert "block error" not in okPage and "class='residuals'" in okPage


def testTheReasonReachesThePerVisitTables():
    # Regression (review finding): visitTable's bad filter must include REDUCE FAILED, or the returnCode
    # reason is blank in every table row (it only survived on the visit page before).
    tables = _tables((1, "r1", "science", _science(), {"gate": _FAILED}))
    vrow = visitTable(imageTable(tables)).set_index("visit").loc[1]
    assert "reduceExposure failed (returnCode 1: FAILED)" in vrow["why"]
    assert vrow["flagged"] == "r1 R"
    # It reaches the run report's "Visits that do not pass" table, not just the visit page.
    page = buildReport(tables, info=_info([1], ["2026-11-04 22:00"])).html
    assert "reduceExposure failed (returnCode 1: FAILED)" in page
    assert "r1 REDUCE FAILED" in page  # flaggedDetectors expands the R letter


def testTheClockKnowsTheReduceFailedLetter():
    # Regression (review finding): the clock derives each visit's verdict from the per-detector letters;
    # a REDUCE FAILED detector is the letter R. Without R in the clock's maps / CSS the square rendered
    # "undefined", uncoloured.
    tables = _tables((1, "r1", "science", _science(), {"gate": _FAILED}))
    page = buildReport(tables, info=_info([1], ["2026-11-04 22:00"])).html
    data = json.loads(re.search(r"const DATA = (\[.*?\]);\n", page).group(1))
    # detectorsStatus (DATA idx 10) encodes the REDUCE FAILED detector as the letter R.
    assert data[0][10] == "r1R"
    # The inlined clock script resolves R to a verdict and a class; the CSS colours the square.
    assert 'R: "REDUCE FAILED"' in page and "R: 4" in page
    assert ".sq.redfail" in page
    # Negative control: R outranks F in the clock too, so a mixed visit does not read as a plain FAIL.
    assert re.search(r"RANK = \{[^}]*F: 3, R: 4", page)
