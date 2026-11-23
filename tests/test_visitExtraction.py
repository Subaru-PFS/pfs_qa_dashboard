"""Tests for the per-visit extraction plots (``visitExtraction``): rendering, the cache, and the detail PDF."""

import numpy as np

from pfs.qa.dashboard.visitExtraction import ExtractionImages, renderExtraction


def _stats(n: int = 40) -> dict:
    """Return a small ``extQaImage_pickle`` for one detector."""
    rng = np.random.default_rng(0)
    fiberIds = np.sort(rng.choice(np.arange(1, 651), size=n, replace=False))
    return {
        "dataId": {"visit": 42, "arm": "r", "spectrograph": 1, "run": "test"},
        "fiberIds": fiberIds,
        "xa": np.linspace(50.0, 4050.0, n),
        "pfsArmAve": rng.uniform(100.0, 5000.0, n),
        "targetMask": {"SCIENCE": np.ones(n, dtype=bool)},
        "chiMed": rng.normal(0.0, 0.3, n),
        "chiStd": np.abs(rng.normal(1.0, 0.3, n)),
        "chiAtPeak": rng.normal(0.0, 0.3, n),
        "resFrac": rng.normal(0.0, 1.0, n),
    }


def testRenderExtractionMakesAPng():
    png = renderExtraction(_stats())
    assert isinstance(png, bytes) and png[:4] == b"\x89PNG"


def testImagesPlaceholderForMissingOrEmptyData():
    # A detector whose pickle is None gets a placeholder PNG, not an error.
    none = ExtractionImages(lambda v, a, s: None, lambda v, a, s: None, lambda v: {"r1": "science"})
    assert none.png(42, "r1")[:4] == b"\x89PNG"
    # An empty-fiber pickle (the task wrote a degenerate result) is also "no data".
    empty = ExtractionImages(
        lambda v, a, s: {"fiberIds": []}, lambda v, a, s: None, lambda v: {"r1": "science"}
    )
    assert empty.png(42, "r1")[:4] == b"\x89PNG"


def testImagesRenderCacheAndFourOhFour():
    reads = []

    def readStats(visit, arm, spectrograph):
        reads.append((visit, arm, spectrograph))
        return _stats() if arm == "r" else None  # n1 has no stored pickle

    context = lambda visit: {"r1": "science", "n1": "science"}  # noqa: E731
    images = ExtractionImages(readStats, lambda v, a, s: None, context)

    first = images.png(42, "r1")
    assert first[:4] == b"\x89PNG"
    # Cached: a second request for the same detector does not read again.
    assert images.png(42, "r1") is first
    assert reads == [(42, "r", 1)]
    # A detector the Butler has no pickle for gets a placeholder PNG, not an error or None.
    placeholder = images.png(42, "n1")
    assert placeholder[:4] == b"\x89PNG" and placeholder != first
    # A camera that is not one of the visit's detectors is a 404 (None), and never read.
    assert images.png(42, "m4") is None
    assert ("m", 4) not in [(arm, sp) for _, arm, sp in reads]


def testImagesCacheEvictsLeastRecentlyUsed():
    images = ExtractionImages(
        lambda v, a, s: _stats(),
        lambda v, a, s: None,
        lambda v: {"r1": "science", "r2": "science"},
        limit=1,
    )
    a = images.png(1, "r1")
    images.png(2, "r2")  # evicts (1, r1)
    # (1, r1) was dropped, so it is rendered afresh (a new bytes object).
    assert images.png(1, "r1") is not a


def testDetailPdfStreamedAndFourOhFour():
    pdfReads = []

    def readImagePdf(visit, arm, spectrograph):
        pdfReads.append((visit, arm, spectrograph))
        return b"%PDF-1.4 fake" if arm == "r" else None

    context = lambda visit: {"r1": "science", "n1": "science"}  # noqa: E731
    images = ExtractionImages(lambda v, a, s: None, readImagePdf, context)
    # A real detector's detail PDF is streamed as-is.
    assert images.pdf(42, "r1") == b"%PDF-1.4 fake"
    # A detector the Butler has no detail PDF for is a 404 (None).
    assert images.pdf(42, "n1") is None
    # A camera not among the visit's detectors is a 404, and is never read.
    assert images.pdf(42, "m4") is None
    assert ("m", 4) not in [(arm, sp) for _, arm, sp in pdfReads]
