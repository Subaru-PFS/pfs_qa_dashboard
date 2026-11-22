"""Tests for the calibration-set section (`calibSet`): the go/no-go grid render and the run-report wiring.

The judging itself is `pfs.drp.qa.comparison.calibInputs` (tested in drp_qa); here we cover this module's
own code — grouping/rendering — and that `buildReport` includes the section only with an opdb listing.
"""

import pandas as pd
from test_runReport import _science, _tables

from pfs.qa.dashboard import calibSet, runReport
from pfs.qa.dashboard.calibSet import CalibSet, renderCalibSet

_DETECTORS = [f"{arm}{sp}" for arm in "brn" for sp in (1, 2, 3, 4)] + [f"m{sp}" for sp in (1, 2, 3, 4)]


def _event(cells: dict, visits: list[tuple], notes=()) -> CalibSet:
    """Return a CalibSet whose grid is all ``go`` except ``cells`` ({(detector, visit): value}); m not exposed."""
    frame = pd.DataFrame(visits, columns=["visit", "lamp", "k", "n"])
    frame["time_exp_start"] = pd.Timestamp("2026-10-31 03:30")
    grid = pd.DataFrame("go", index=_DETECTORS, columns=[int(v[0]) for v in visits])
    grid.loc[["m1", "m2", "m3", "m4"]] = ""  # the m arm did not expose on this set
    for (det, visit), value in cells.items():
        grid.loc[det, int(visit)] = value
    return CalibSet("2026-10-31 03:30", "halogen, neon", frame, grid, list(notes))


def testRenderShowsGoNoGoAndMissing():
    event = _event(
        {("n4", 1001): "no-go", ("n3", 1001): "missing"},
        [(1001, "halogen", 1, 3), (1002, "halogen", 2, 3)],
        notes=["Leave out: n4 1001 (reserved RMS off)."],
    )
    html = renderCalibSet([event])
    assert "Calibration set: go/no-go per detector" in html
    assert "setcell nogo" in html and "✕" in html  # the no-go cell is marked
    assert "setcell miss" in html  # the missing cell
    assert "Leave out: n4 1001 (reserved RMS off)." in html  # the notes
    assert "1 no-go" in html  # the event's no-go chip
    assert '<a href="/visit/1001">1001</a>' in html  # the visit links to its page
    # Negative control: only the one cell is no-go, not the whole grid.
    assert html.count("setcell nogo") == 1


def testRenderEmptyIsBlank():
    assert renderCalibSet([]) == ""


def testBuildReportIncludesSetOnlyWithAListing(monkeypatch):
    event = _event({("b1", 1001): "no-go"}, [(1001, "halogen", 1, 3)])
    monkeypatch.setattr(calibSet, "calibSetEvents", lambda listing, tables, normalValues: [event])
    tables = _tables((1001, "b1", "trace", _science()))

    without = runReport.buildReport(tables)
    assert "Calibration set: go/no-go per detector" not in without.html

    withListing = runReport.buildReport(tables, listing=pd.DataFrame({"pfs_visit_id": [1001]}))
    assert "Calibration set: go/no-go per detector" in withListing.html


def testBuildReportSurvivesACalibSetFailure(monkeypatch):
    def boom(listing, tables, normalValues):
        raise ValueError("no data")

    monkeypatch.setattr(calibSet, "calibSetEvents", boom)
    tables = _tables((1001, "b1", "trace", _science()))
    report = runReport.buildReport(tables, listing=pd.DataFrame({"pfs_visit_id": [1001]}))
    # The section is dropped, not fatal: the rest of the page is still there.
    assert "Calibration set: go/no-go per detector" not in report.html
    assert "Every visit" in report.html
