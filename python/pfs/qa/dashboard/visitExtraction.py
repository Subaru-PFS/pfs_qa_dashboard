"""Extraction-QA plots for the per-visit page, rendered and served on demand.

`pfs.drp.qa.plotting.extraction.plotExtractionStats` draws one detector's
four-panel extraction summary (chiMed / chiStd / chiAtPeak / resFrac vs fiberId,
coloured by mean extracted flux) from the ``extQaImage_pickle`` a reduction
stores. qadb holds none of this — the extraction task is not loaded into qadb and
carries no verdict — so the plot's data lives in the Butler. When the dashboard
is served on a host with the Butler (``qaDashboard.py serve --butler``),
`ExtractionImages` reads a detector's pickle through an injected reader, draws the
figure, and caches the PNG, so the visit page's summary ``<img>`` tags are
answered on demand; it also streams the task's ``extQaImage`` detail PDF (the 2-D
image pages that cannot be redrawn from the pickle) behind each "detail (PDF)"
link.

The readers are the only Butler-bound parts; they are passed in. Everything else
here — the rendering (`plotExtractionStats` is stack-free) and the cache — is
stack-free and tested with stub readers.
"""

import io
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

import matplotlib

# No display on the server; set before any figure is drawn. Imported only in the
# ``--butler`` path, so the rest of the dashboard needs no matplotlib.
matplotlib.use("Agg")

__all__ = ["ExtractionImages", "renderExtraction"]

#: Dots per inch for the rendered PNG: a 12x8 in figure is ~1152x768 px.
_DPI = 96

#: Serialises matplotlib rendering across the server's threads (matplotlib is not thread-safe); the
#: Butler read (its own lock) and the event loop stay parallel. Shared shape with ``visitResiduals``.
_RENDER_LOCK = threading.Lock()


def renderExtraction(qaStats: Mapping[str, Any]) -> bytes:
    """Draw one detector's `plotExtractionStats` and return it as PNG bytes.

    Drawn with no suptitle (``title=""``), so the output is exactly the figure's
    12x8 inches: the page reserves a 3:2 box for it (no layout shift), and the
    detector and visit are named in the figure's caption on the page, not on the
    image.
    """
    # Imported here, not at module scope: it pulls matplotlib, needed only when rendering.
    from pfs.drp.qa.plotting.extraction import plotExtractionStats

    buffer = io.BytesIO()
    with _RENDER_LOCK:
        fig = plotExtractionStats(qaStats, title="")
        fig.savefig(buffer, format="png", dpi=_DPI)
    return buffer.getvalue()


def _placeholder(text: str) -> bytes:
    """Return a small 3:2 PNG carrying ``text``, for a detector the Butler has no pickle for."""
    from matplotlib.figure import Figure

    buffer = io.BytesIO()
    with _RENDER_LOCK:
        fig = Figure(figsize=(6, 4))
        ax = fig.add_subplot(111)
        ax.axis("off")
        ax.text(0.5, 0.5, text, ha="center", va="center", fontsize="medium", color="#555555")
        fig.savefig(buffer, format="png", dpi=_DPI)
    return buffer.getvalue()


class ExtractionImages:
    """Render and cache a visit's extraction-QA summary plots, and serve their detail PDFs, on demand.

    Parameters
    ----------
    readStats : callable
        ``readStats(visit, arm, spectrograph)`` returns the detector's
        ``extQaImage_pickle`` (a mapping of per-fiber arrays), or None when the
        Butler holds none. The only Butler-bound part of the PNG path; it guards
        its own read if the Butler is not thread-safe.
    readImagePdf : callable
        ``readImagePdf(visit, arm, spectrograph)`` returns the detector's
        ``extQaImage`` detail PDF as bytes, or None when the Butler holds none.
    context : callable
        ``context(visit)`` returns ``{camera: frameType}`` for the visit's
        detectors, from the cached store — which cameras are real (so a bogus one
        is a 404).
    limit : `int`, optional
        How many rendered PNGs to keep (an LRU over ``(visit, camera)``).

    Notes
    -----
    A rendered PNG is cached by ``(visit, camera)`` and not invalidated: a
    detector's stored pickle in a run's collection is stable. Detail PDFs are
    streamed uncached — they are large and fetched only when a "detail (PDF)"
    link is clicked.
    """

    def __init__(
        self,
        readStats: Callable[[int, str, int], Mapping[str, Any] | None],
        readImagePdf: Callable[[int, str, int], bytes | None],
        context: Callable[[int], Mapping[str, Any]],
        *,
        limit: int = 64,
    ):
        self._readStats = readStats
        self._readImagePdf = readImagePdf
        self._context = context
        self._limit = limit
        self._cache: OrderedDict[tuple[int, str], bytes] = OrderedDict()
        self._lock = threading.Lock()

    def png(self, visit: int, camera: str) -> bytes | None:
        """Return the detector's summary plot as PNG bytes, or None when ``camera`` is not the visit's.

        A detector the Butler has no pickle for gets a placeholder PNG, not None,
        so the page's ``<img>`` never shows a broken icon; None is only a camera
        that is not a detector of the visit (a 404).
        """
        key = (int(visit), str(camera))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        if camera not in self._context(int(visit)):
            return None
        png = self._render(int(visit), str(camera))
        with self._lock:
            self._cache[key] = png
            self._cache.move_to_end(key)
            while len(self._cache) > self._limit:
                self._cache.popitem(last=False)
        return png

    def pdf(self, visit: int, camera: str) -> bytes | None:
        """Return the detector's ``extQaImage`` detail PDF as bytes, or None (a 404).

        None when ``camera`` is not one of the visit's detectors, or when the
        Butler holds no detail PDF for it.
        """
        if camera not in self._context(int(visit)):
            return None
        arm, spectrograph = camera[0], int(camera[1:])
        return self._readImagePdf(int(visit), arm, spectrograph)

    def _render(self, visit: int, camera: str) -> bytes:
        """Read the detector's pickle and draw the plot, or a placeholder when there is none (no lock held)."""
        arm, spectrograph = camera[0], int(camera[1:])
        stats = self._readStats(visit, arm, spectrograph)
        if stats is None or len(stats.get("fiberIds", [])) == 0:
            return _placeholder(f"No extraction data for {camera}, visit {visit}")
        return renderExtraction(stats)
