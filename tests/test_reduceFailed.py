"""A reduceExposure failure shows as a distinct REDUCE FAILED verdict, not AWAITING / NO RESULT."""

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
