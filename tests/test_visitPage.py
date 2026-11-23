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
    # Negative control: read as pixels the same residual is a different number, so the units matter.
    assert f"{0.6 / _OH_R1:+.2f}" != "+0.60"
    # Negative control: an arc with no sky species has no such section.
    arc = buildVisitPage(_tables((4, "r1", "arc", {}, {"gate": ("PASS", "")})), None, 4)
    assert "Sky-line residual by species" not in arc


def testVisitPageHasPrevNextNav():
    tables = _tables((7, "b1", "science", _science(), {"gate": ("PASS", "")}))
    page = buildVisitPage(tables, None, 7)
    # Dumb nav: the neighbours are visit ± 1 with no existence check.
    assert "href='/visit/6' rel='prev'" in page and "href='/visit/8' rel='next'" in page
    # The night page sits between prev and next, in that order.
    nav = re.search(r"<nav class='visitnav'>(.*?)</nav>", page).group(1)
    order = [nav.index("rel='prev'"), nav.index("href='/'"), nav.index("rel='next'")]
    assert order == sorted(order) and "Night page" in nav
    # Below visit 1 there is no previous link (it never points at visit 0).
    first = buildVisitPage(_tables((1, "b1", "science", _science(), {"gate": ("PASS", "")})), None, 1)
    assert "rel='prev'" not in first and "href='/visit/2' rel='next'" in first
    # Negative control: the previous link is visit - 1, not the same visit or visit + 1.
    assert "href='/visit/7' rel='prev'" not in page
    # A no-result visit is still navigable away from.
    none = buildVisitPage({}, None, 5)
    assert "href='/visit/4' rel='prev'" in none and "href='/visit/6' rel='next'" in none


def testVisitPageShowsResidualPlotsWhenServed():
    tables = _tables(
        (7, "b1", "science", _science(x=0.2), {"gate": ("PASS", "")}),
        (7, "n1", "science", _science(x=0.3), {"gate": ("FAIL", "OH residual +1.20 km/s >= fail 1")}),
    )
    # Off by default: a snapshot or a server without the Butler shows no plot, only the note that it is absent.
    plain = buildVisitPage(tables, None, 7)
    assert "DetectorMap line residuals" not in plain
    assert "line-residual plots are not yet shown" in plain
    # Negative control: the lightbox overlay and its script never leak onto a page without the plots.
    assert "id='lightbox'" not in plain and "<script" not in plain

    page = buildVisitPage(tables, None, 7, residuals=True)
    assert "DetectorMap line residuals" in page
    # One lazily-loaded image per detector, pointing at the dashboard's on-demand residual route.
    assert page.count("src='/visit/7/residual/") == 2 and "loading='lazy'" in page
    assert "src='/visit/7/residual/b1.png'" in page and "src='/visit/7/residual/n1.png'" in page
    # Regular detector order (brnm): b1 before n1, regardless of verdict.
    assert page.index("residual/b1.png") < page.index("residual/n1.png")
    # The Detectors table links each detector to its plot below (the figure carries the anchor).
    assert "<a href='#resid-n1'>n1</a>" in page and "id='resid-n1'" in page
    # Click-to-enlarge: a lightbox overlay and the script that fills it from the clicked image.
    assert "id='lightbox'" in page and "imgwrap" in page and "addEventListener('click'" in page
    # The page itself still carries no plot data (the browser fetches the images); it stays small.
    assert "data:image" not in page
    # The "not yet shown" note drops the line-residual plots once they are shown.
    assert "line-residual plots are not yet shown" not in page


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
    assert "No QA result" in page
    # The identifying facts are the large title, not a byline under a bare "Visit N".
    heading = re.search(r"<h1>(.*?)</h1>", page).group(1)
    assert heading.startswith("Visit 9 · ")
    assert "2026-11-04 22:00 HST" in heading and "b1 r1" in heading
    # No detector table without QA data.
    assert "Measurements by detector" not in page


def testVisitPageShowsExtractionPlotsWhenServed():
    tables = _tables(
        (7, "b1", "science", _science(x=0.2), {"gate": ("PASS", "")}),
        (7, "n1", "science", _science(x=0.3), {"gate": ("FAIL", "OH residual +1.20 km/s >= fail 1")}),
    )
    # Off by default: no extraction section, and the footer lists the plots as not yet shown.
    plain = buildVisitPage(tables, None, 7)
    assert "<h2>Extraction QA</h2>" not in plain
    assert "extraction-QA plots" in plain and "not yet shown" in plain

    page = buildVisitPage(tables, None, 7, extraction=True)
    assert "<h2>Extraction QA</h2>" in page
    # One lazily-loaded summary image per detector, each pointing at the on-demand extraction route.
    assert page.count("src='/visit/7/extraction/") == 2 and "loading='lazy'" in page
    assert "src='/visit/7/extraction/b1.png'" in page and "src='/visit/7/extraction/n1.png'" in page
    # A "detail (PDF)" link per detector, pointing at the detail-PDF route.
    assert "href='/visit/7/extraction/b1.pdf'>detail (PDF)</a>" in page
    assert "href='/visit/7/extraction/n1.pdf'" in page
    # Regular detector order (brnm): b1 before n1, regardless of verdict.
    assert page.index("extraction/b1.png") < page.index("extraction/n1.png")
    # Extraction is not gated: the figure anchors are extract-<camera>, and no verdict chip rides the caption.
    assert "id='extract-b1'" in page and "id='extract-n1'" in page
    # Click-to-enlarge is wired, and the extraction images are covered by the lightbox script.
    assert "id='lightbox'" in page and ".extraction .imgwrap img" in page
    # The page carries no plot data (the browser fetches the images); it stays small.
    assert "data:image" not in page
    # Negative control: once shown, extraction is dropped from the "not yet shown" note entirely.
    assert "extraction-QA plots" not in page


def testVisitPageBothPlotSectionsShareOneLightbox():
    tables = _tables((7, "b1", "science", _science(x=0.2), {"gate": ("PASS", "")}))
    page = buildVisitPage(tables, None, 7, residuals=True, extraction=True)
    assert "DetectorMap line residuals" in page and "<h2>Extraction QA</h2>" in page
    # The lightbox overlay and its script appear exactly once, shared by both sections.
    assert page.count("id='lightbox'") == 1 and page.count("<script>") == 1
    # The one script binds both sections' images.
    assert "'.residuals .imgwrap img, .extraction .imgwrap img'" in page
    # With both shown, nothing is left "not yet shown".
    assert "not yet shown" not in page


def testVisitPageHandlesMissingTables():
    # Before the first read the store may hold nothing: the page still renders.
    page = buildVisitPage({}, None, 5)
    assert "<h1>Visit 5</h1>" in page and "No QA result" in page
