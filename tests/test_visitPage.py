"""Tests for the per-visit page (``visitPage``): one visit over its detectors, built from the store."""

import re

import pandas as pd
from test_runReport import _OH_R1, _science, _tables

from pfs.qa.dashboard.visitPage import buildVisitPage


def testVisitPageShowsDetectorsAndGrid():
    tables = _tables(
        (7, "b1", "science", _science(x=0.2), {"gate": ("PASS", "")}),
        (
            7,
            "n1",
            "science",
            _science(x=0.3),
            {"gate": ("FAIL", "imageQualityQa: medFWHM=3.7px >= fail threshold 3.5px")},
        ),
    )
    page = buildVisitPage(tables, None, 7)
    assert page.startswith("<!doctype html>") and "<h1>Visit 7" in page
    assert "Detectors" in page and "Measurements by detector" in page
    # The overall verdict is the worst detector's.
    assert "<span class='chip verdict fail'>FAIL</span>" in page
    # Each detector's stored reason is shown.
    assert "medFWHM=3.7px" in page
    # Both detectors appear (detector table and grid columns).
    assert "b1</th>" in page and "n1</th>" in page
    # Stands alone: fonts inline, nothing fetched; links back to the other pages.
    assert page.count("@font-face") == 2 and not re.findall(r"""(?:src|href)=["']https?://""", page)
    assert "href='/'" in page and "href='/run'" in page


def testVisitPageShowsSkyResidualBySpecies():
    tables = _tables(
        (3, "r1", "science", _science(heldBack=0.6 / _OH_R1, species="OH"), {"gate": ("PASS", "")})
    )
    page = buildVisitPage(tables, None, 3)
    assert "Sky-line residual by species (km/s)" in page
    # The held-back OH residual, converted to km/s with the r1 OH dispersion.
    assert "OH" in page and "+0.60" in page
    # Negative control: an arc with no sky species has no such section.
    arc = buildVisitPage(_tables((4, "r1", "arc", {}, {"gate": ("PASS", "")})), None, 4)
    assert "Sky-line residual by species" not in arc


def testVisitPageWithNoQaResult():
    info = pd.DataFrame(
        {
            "visit": [9],
            "time": pd.to_datetime(["2026-11-04 22:00"]),
            "exptime": [600.0],
            "sequenceType": ["scienceObject"],
            "sequenceName": [None],
            "expType": ["object"],
            "cameras": ["b1 r1"],
            "iis": [False],
            "sequenceEnd": ["completed"],
        }
    )
    # Visit 9 is not in the QA tables (not reduced): the page says so, from the opdb info.
    page = buildVisitPage(_tables((1, "r1", "science", _science())), info, 9)
    assert "<h1>Visit 9</h1>" in page and "No QA result" in page
    assert "2026-11-04 22:00 HST" in page
    # No detector table without QA data.
    assert "Measurements by detector" not in page


def testVisitPageHandlesMissingTables():
    # Before the first read the store may hold nothing: the page still renders.
    page = buildVisitPage({}, None, 5)
    assert "<h1>Visit 5</h1>" in page and "No QA result" in page
