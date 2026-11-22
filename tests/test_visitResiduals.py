"""Tests for the per-visit residual plots (``visitResiduals``): limits, rendering and the on-demand cache."""

import numpy as np
import pandas as pd

from pfs.drp.qa.metrics.registry import ARC, CHECKS, SCIENCE, TWILIGHT
from pfs.qa.dashboard.visitResiduals import ResidualImages, renderResidual, residualLimits


def _lineData(kms=True):
    """Return a small ``dmLineResidualData`` for one detector: trace rows and a few OH lines."""
    rng = np.random.default_rng(0)
    nTrace = 240
    trace = pd.DataFrame(
        {
            "fiberId": rng.integers(1, 20, nTrace),
            "wavelength": np.nan,
            "x": rng.uniform(0, 4000, nTrace),
            "y": rng.uniform(0, 4000, nTrace),
            "description": "Trace",
            "statusType": rng.choice(["USED", "RESERVED"], nTrace),
            "dxCalib": rng.normal(0.1, 0.05, nTrace),
            "dyCalib": np.nan,
            "dxVisit": rng.normal(0.0, 0.03, nTrace),
            "dyVisit": np.nan,
        }
    )
    nLine = 40
    line = pd.DataFrame(
        {
            "fiberId": rng.integers(1, 20, nLine),
            "wavelength": rng.uniform(600, 950, nLine),
            "x": rng.uniform(0, 4000, nLine),
            "y": rng.uniform(0, 4000, nLine),
            "description": "OH",
            "statusType": rng.choice(["USED", "RESERVED"], nLine),
            "dxCalib": rng.normal(0.1, 0.05, nLine),
            "dyCalib": rng.normal(0.2, 0.1, nLine),
            "dxVisit": rng.normal(0.0, 0.03, nLine),
            "dyVisit": rng.normal(0.0, 0.08, nLine),
        }
    )
    data = pd.concat([trace, line], ignore_index=True)
    if kms:
        data["dyKmsCalib"] = data["dyCalib"] * 7.0
        data["dyKmsVisit"] = data["dyVisit"] * 7.0
    return data


def testLimitsAreTheRegistrysFixedChecks():
    # A science frame: the offset band in x, and the sky-line residual band in km/s for the arm.
    limits = residualLimits(SCIENCE, "r", dyKms=True)
    assert limits["dx"] == (CHECKS["skyOffset"].warn, CHECKS["skyOffset"].fail)
    assert limits["dy"] == (CHECKS["skyLineResidual"].warn, CHECKS["skyLineResidual"].fail)
    # The blue arm's sky-residual limit is its own (wider), not the r/n/m one.
    assert residualLimits(SCIENCE, "b", dyKms=True)["dy"] == (
        CHECKS["skyLineResidualBlue"].warn,
        CHECKS["skyLineResidualBlue"].fail,
    )
    assert CHECKS["skyLineResidualBlue"].warn != CHECKS["skyLineResidual"].warn


def testLimitsOmitTheKmsBandWithoutKmsData():
    # Without the km/s columns the Δy trend is in pixels, so no km/s band is drawn (units would not match).
    assert "dy" not in residualLimits(SCIENCE, "r", dyKms=False)
    assert "dx" in residualLimits(SCIENCE, "r", dyKms=False)


def testLimitsByFrameType():
    # A calibration frame takes the calibOffset band in x, and has no km/s sky-residual check.
    arc = residualLimits(ARC, "r", dyKms=True)
    assert arc["dx"] == (CHECKS["calibOffset"].warn, CHECKS["calibOffset"].fail)
    assert "dy" not in arc
    # Negative control: the calibration band is not the science one.
    assert arc["dx"] != residualLimits(SCIENCE, "r", dyKms=True)["dx"]
    # A frame type no residual check applies to gets no bands at all.
    assert residualLimits(TWILIGHT, "r", dyKms=True) == {}
    # An unknown frame type (no gate row) has no matching check either, so no bands.
    assert residualLimits(None, "r", dyKms=True) == {}


def testImagesPlaceholderForEmptyData():
    # An empty DataFrame (not just None) is also "no data" and gets the placeholder, not an exception.
    images = ResidualImages(lambda v, a, s: pd.DataFrame(), lambda v: {"r1": SCIENCE})
    assert images.png(42, "r1")[:4] == b"\x89PNG"


def testRenderResidualMakesAPng():
    png = renderResidual(_lineData(), title="r1 42", limits={"dx": (0.5, 1.0), "dy": (0.5, 1.0)})
    assert isinstance(png, bytes) and png[:4] == b"\x89PNG"
    # Renders without limits too (an older collection, or a frame with no applicable check).
    assert renderResidual(_lineData(kms=False), title="r1 42")[:4] == b"\x89PNG"


def testImagesRenderCacheAndPlaceholder():
    reads = []

    def readLineData(visit, arm, spectrograph):
        reads.append((visit, arm, spectrograph))
        return _lineData() if arm == "r" else None  # n1 has no stored data

    context = lambda visit: {"r1": SCIENCE, "n1": SCIENCE}  # noqa: E731
    images = ResidualImages(readLineData, context)

    first = images.png(42, "r1")
    assert first[:4] == b"\x89PNG"
    # Cached: a second request for the same detector does not read again.
    assert images.png(42, "r1") is first
    assert reads == [(42, "r", 1)]
    # A detector the Butler has no data for gets a placeholder PNG, not an error or None.
    placeholder = images.png(42, "n1")
    assert placeholder[:4] == b"\x89PNG" and placeholder != first
    # A camera that is not one of the visit's detectors is a 404 (None), and never read.
    assert images.png(42, "m4") is None
    assert ("m", 4) not in [(arm, sp) for _, arm, sp in reads]


def testImagesCacheEvictsLeastRecentlyUsed():
    def readLineData(visit, arm, spectrograph):
        return _lineData()

    context = lambda visit: {"r1": SCIENCE, "r2": SCIENCE}  # noqa: E731
    images = ResidualImages(readLineData, context, limit=1)
    a = images.png(1, "r1")
    images.png(2, "r2")  # evicts (1, r1)
    # (1, r1) was dropped, so it is rendered afresh (a new bytes object).
    assert images.png(1, "r1") is not a
