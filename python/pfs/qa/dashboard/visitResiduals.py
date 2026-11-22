"""Detector-map line-residual plots for the per-visit page, rendered on demand.

`pfs.drp.qa.plotting.dmResiduals.plotDetectorResiduals` draws one
detector's line residuals from ``dmLineResidualData``: the per-line rows the
reduction stores and the pipeline no longer renders. qadb keeps only the
summary and metrics, not those per-line rows, so the plot's data lives in the
Butler. When the dashboard is served on a host with the Butler
(``qaDashboard.py serve --butler``), `ResidualImages` reads a detector's rows
through an injected reader, draws the figure, and caches the PNG, so the visit
page's ``<img>`` tags are answered on demand — nothing is rendered until a page
is viewed, and nothing is stored.

The reader is the only Butler-bound part; it is passed in. Everything here —
the limits from the registry, the rendering, the cache — is stack-free and
tested with a stub reader.
"""

import io
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

import matplotlib
import pandas as pd

from pfs.drp.qa.metrics.registry import CHECKS, FIXED

# No display on the server; set before any figure is drawn. Imported only in the
# ``--butler`` path, so the rest of the dashboard needs no matplotlib.
matplotlib.use("Agg")

__all__ = ["ResidualImages", "renderResidual", "residualLimits"]

#: Dots per inch for the rendered PNG: a 15×8 in figure is ~1440×770 px, ~240 kB.
_DPI = 96

#: The km/s columns `plotDetectorResiduals` needs for a km/s Δy trend (else it falls back to pixels).
_KMS_COLUMNS = {"dyKmsCalib", "dyKmsVisit"}

#: Serialises matplotlib rendering across the server's threads: matplotlib is not thread-safe, so two
#: renders at once could corrupt a figure. The Butler read (its own lock) and the event loop stay parallel.
_RENDER_LOCK = threading.Lock()


def _fixedLimit(
    frameType: str | None, metrics: tuple[str, ...], arm: str | None = None
) -> tuple[float, float] | None:
    """Return the first FIXED check's (WARN, FAIL) for a frame type and arm, reading one of ``metrics``.

    The dashboard judges nothing; these come from the current registry, so a
    band shows the limit in force now, which the page notes may differ from the
    one a visit was judged with. Only FIXED checks give a band in the trend's
    own unit; a RELATIVE one (a per-cent of a normal value) is skipped.
    """
    wanted = set(metrics)
    for check in CHECKS.values():
        if check.kind != FIXED or frameType not in check.frames:
            continue
        if not wanted & set(check.metrics):
            continue
        if check.arms and arm is not None and arm not in check.arms:
            continue
        return check.warn, check.fail
    return None


def residualLimits(frameType: str | None, arm: str | None, *, dyKms: bool) -> dict[str, tuple[float, float]]:
    """Return the gate bands for `plotDetectorResiduals`, from the registry's FIXED checks.

    Parameters
    ----------
    frameType : `str` or None
        The detector's gate frame type (`pfs.drp.qa.metrics.registry.FRAME_TYPES`).
    arm : `str` or None
        The detector's arm, for the arm-specific sky-residual limit.
    dyKms : `bool`
        Whether the Δy trend is in km/s (the data carries the km/s columns); the
        km/s band is given only then, so its unit matches the trend.

    Returns
    -------
    `dict`
        ``{"dx": (warn, fail)}`` in pixels (the offset-from-calibration limit) and,
        when ``dyKms``, ``{"dy": (warn, fail)}`` in km/s (the sky-line residual
        limit). A trend with no applicable FIXED check gets no band.
    """
    limits: dict[str, tuple[float, float]] = {}
    dx = _fixedLimit(frameType, ("medDxCenter", "medianDeltaX"))
    if dx is not None:
        limits["dx"] = dx
    if dyKms:
        dy = _fixedLimit(frameType, ("medianDeltaYKms",), arm=arm)
        if dy is not None:
            limits["dy"] = dy
    return limits


def renderResidual(
    data: pd.DataFrame, *, title: str, limits: Mapping[str, tuple[float, float]] | None = None
) -> bytes:
    """Draw one detector's `plotDetectorResiduals` and return it as PNG bytes."""
    # Imported here, not at module scope: it pulls matplotlib and seaborn, needed only when rendering.
    from pfs.drp.qa.plotting.dmResiduals import plotDetectorResiduals

    buffer = io.BytesIO()
    # Exactly the figure's 15×8 inches (no tight bbox), so the page can reserve the box with a 15/8
    # aspect-ratio and show a loader without the layout shifting when the image arrives. The figure is a
    # bare matplotlib.figure.Figure (no pyplot state), so it is freed when it goes out of scope.
    with _RENDER_LOCK:
        fig = plotDetectorResiduals(data, title=title, limits=dict(limits) if limits else None)
        fig.savefig(buffer, format="png", dpi=_DPI)
    return buffer.getvalue()


def _placeholder(text: str) -> bytes:
    """Return a small PNG carrying ``text``, for a detector the Butler has no residual data for."""
    from matplotlib.figure import Figure

    # The plot's 15:8 shape, so it fills the page's aspect-ratio box and covers the loading spinner.
    buffer = io.BytesIO()
    with _RENDER_LOCK:
        fig = Figure(figsize=(7.5, 4))
        ax = fig.add_subplot(111)
        ax.axis("off")
        ax.text(0.5, 0.5, text, ha="center", va="center", fontsize="medium", color="#555555")
        fig.savefig(buffer, format="png", dpi=_DPI)
    return buffer.getvalue()


class ResidualImages:
    """Render and cache a visit's detector residual plots, built on demand from a Butler read.

    Parameters
    ----------
    readLineData : callable
        ``readLineData(visit, arm, spectrograph)`` returns the detector's
        ``dmLineResidualData`` rows (`pandas.DataFrame`), or None when the Butler
        holds none. The only Butler-bound part; it guards its own read if the
        Butler is not thread-safe.
    context : callable
        ``context(visit)`` returns ``{camera: frameType}`` for the visit's
        detectors, from the cached store — which cameras are real (so a bogus one
        is a 404) and each one's frame type (for the limits).
    limit : `int`, optional
        How many rendered PNGs to keep (an LRU over ``(visit, camera)``); a full
        run of science visits renders at most this many detectors' plots at once.

    Notes
    -----
    A rendered plot is cached by ``(visit, camera)`` and not invalidated: a
    detector's rows in a run's collection are stable. A visit reprocessed into a
    new Butler RUN while the server runs would keep the old plot until a restart,
    as the page already warns a verdict may have been made with a different
    threshold set.
    """

    def __init__(
        self,
        readLineData: Callable[[int, str, int], pd.DataFrame | None],
        context: Callable[[int], Mapping[str, Any]],
        *,
        limit: int = 64,
    ):
        self._read = readLineData
        self._context = context
        self._limit = limit
        self._cache: OrderedDict[tuple[int, str], bytes] = OrderedDict()
        self._lock = threading.Lock()

    def png(self, visit: int, camera: str) -> bytes | None:
        """Return the detector's residual plot as PNG bytes, or None when ``camera`` is not one of the visit's.

        A detector the Butler has no residual data for gets a placeholder PNG,
        not None, so the page's ``<img>`` never shows a broken icon; None is only
        a camera that is not a detector of the visit (a 404).
        """
        key = (int(visit), str(camera))
        # Serve a cached plot without re-reading the store: a cached entry was a real detector when stored.
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        # Read the context once: whether ``camera`` is a detector of the visit (else a 404) and its frame type.
        context = self._context(int(visit))
        if camera not in context:
            return None
        png = self._render(int(visit), str(camera), context.get(camera))
        with self._lock:
            self._cache[key] = png
            self._cache.move_to_end(key)
            while len(self._cache) > self._limit:
                self._cache.popitem(last=False)
        return png

    def _render(self, visit: int, camera: str, frameType: Any) -> bytes:
        """Read the detector's rows and draw the plot, or a placeholder when there are none (no lock held)."""
        arm, spectrograph = camera[0], int(camera[1:])
        data = self._read(visit, arm, spectrograph)
        if data is None or len(data) == 0:
            return _placeholder(f"No residual data for {camera}, visit {visit}")
        dyKms = set(data.columns) >= _KMS_COLUMNS
        limits = residualLimits(frameType, arm, dyKms=dyKms)
        return renderResidual(data, title=f"{camera} {visit}", limits=limits)
