"""The run's auto-guiding report: how the guide cameras and the guide loop did over the whole run.

The page (``/run/ag``) is drp_qa's end-of-run AG report (`pfs.drp.qa.guiders.runAnalysis`, as
``drpQa.py agReport`` writes it), drawn for the dashboard: the run's key numbers, what the analysis
flags, the camera states hour by hour, each science visit's guide error (its size and its jitter),
focus, acquisition, the night-by-night seeing and transparency, and a section per camera. The run
report (``/run``) carries a short block of the same (`AgReport.summary`) that links to it.

The dashboard measures nothing of its own here. The numbers, the flags and each figure's caption are
drp_qa's (`runSummary`, `findFlags`, `runPlotting.captions`); the run-wide charts are drawn inline as
SVG from the same analysis, and the figures of the cameras are drp_qa's own (`runPlotting.plotFocalPlane`, `plotCamera`),
rendered as a PNG when it is asked for. Seeing and transparency are what qadb holds
(`pfs.qa.dashboard.agConditions`).

The AG tables are read from the opdb (`pfs.drp.qa.guiders.queries.readAgRun`), not qadb. `AgRunStore`
reads the run once and then only the night under way, so a refresh costs a night, not the run;
`AgRunCache` keeps the page for the server, on a thread of its own, so a slow opdb never holds up the
other pages. A snapshot keeps the tables (`snapshotTables`), for serving or writing the page offline.
"""

import base64
import functools
import html
import io
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from pfs.drp.qa.comparison.runs import NIGHT_START
from pfs.qa.dashboard.live import HST
from pfs.qa.dashboard.runReport import _DATA, ReportCache, _fontFaces, _theme

_log = logging.getLogger(__name__)

__all__ = [
    "AG_REBUILD",
    "AG_REFRESH",
    "AgReport",
    "AgRunCache",
    "AgRunStore",
    "agRoutes",
    "agTables",
    "buildAgReport",
    "buildNightTiles",
    "nightStart",
    "nightTables",
    "snapshotTables",
]

#: Seconds between reads of the night under way: what the night page's AG tiles follow.
AG_REFRESH = 120.0

#: Seconds, at least, between makings of the AG page. It is about the whole run and takes seconds to make,
#: so it need not follow each read.
AG_REBUILD = 600.0

#: The tiles each page shows, in order (`_tiles`): the AG report's, the run report's block, and the night page's.
_PAGE_TILES = ("cameras", "error", "jitter", "matched", "residual", "tooFew", "notUsed", "seeing", "flags")
_RUN_TILES = ("cameras", "error", "jitter", "matched", "seeing", "flags")
_NIGHT_TILES = ("cameras", "error", "jitter", "matched", "notUsed")

#: What a snapshot's AG tables are called: ``agRun-<table>``.
_PREFIX = "agRun-"

#: The tables of qadb's sky conditions the page shows, beside those of `readAgRun`.
CONDITIONS = ("seeing", "transparency")

#: The size of an AG pixel: microns, and arcsec on the sky.
_PIXEL_UM, _PIXEL_ARCSEC = 13.0, 0.14

#: Dots per inch of drp_qa's figures (`plotCamera`, `plotFocalPlane`); `_figurePx` gives their size in pixels.
_DPI = 110

#: Geometry of the charts drawn against the run's nights (an SVG viewBox, scaled to the space it is given):
#: the width of a chart across the page and of one of a pair side by side, and the margins.
_WIDE, _HALF = 1100, 560
_LEFT, _RIGHT, _TOP, _BOTTOM = 54, 46, 16, 22


def nightStart(when: Any) -> pd.Timestamp:
    """Return the time (HST) the night ``when`` is in began: 14:00, drp_qa's ``NIGHT_START`` (00:00 UTC)."""
    start = pd.Timedelta(hours=NIGHT_START)
    return (pd.Timestamp(when) - start).normalize() + start


def snapshotTables(tables: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Return the AG tables under the names a snapshot keeps them by (``agRun-<table>``)."""
    return {f"{_PREFIX}{name}": frame for name, frame in tables.items()}


def agTables(tables: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame] | None:
    """Return a snapshot's AG tables by their own names, or None when it holds none."""
    found = {name.removeprefix(_PREFIX): frame for name, frame in tables.items() if name.startswith(_PREFIX)}
    return found if "exposures" in found else None


def _combine(parts: list[Mapping[str, pd.DataFrame]]) -> dict[str, pd.DataFrame]:
    """Join reads of consecutive periods into one set of tables, as one read of the whole would return."""
    parts = [part for part in parts if part]
    tables = {}
    for name in dict.fromkeys(name for part in parts for name in part):
        frames = [part[name] for part in parts if name in part]
        # A read with no row adds nothing; and one whose column is all NULL (a night's first AG exposure, before
        # its guide offset is written) comes back as objects, which must not turn the whole column into them.
        frame = pd.concat([f for f in frames if len(f)] or frames[:1], ignore_index=True).infer_objects()
        if name == "visits":
            frame = frame.drop_duplicates("pfs_visit_id", keep="last", ignore_index=True)
        elif name == "designStars":
            frame = frame.drop_duplicates(["pfs_design_id", "agc_camera_id"], keep="last", ignore_index=True)
        elif name == "magnitudes":
            keys = ["pfs_visit_id", "agc_camera_id", "magnitude"]
            frame = frame.groupby(keys, as_index=False)[["n_expected", "n_good"]].sum()
        tables[name] = frame
    return tables


class AgRunStore:
    """The AG tables of a run, read from the opdb a night at a time once the run so far has been read.

    The first `refresh` reads the run up to the start of the night under way in one go, and that night
    beside it. Each later one reads only that night again; a night that has ended is read a last
    time and kept. Once the run's end has passed nothing more is read. qadb's conditions are read
    with each period, for its visits, so a value qadb gains for a night after that night was kept
    is not seen until the store is made again.

    Parameters
    ----------
    readRun : callable
        ``readRun(start, end)`` returns the tables of
        `pfs.drp.qa.guiders.queries.readAgRun` for a period (HST).
    start, end : `datetime.datetime`
        The run (HST): ``start`` inclusive, ``end`` exclusive.
    readConditions : callable, optional
        ``readConditions(first, last)`` returns the ``seeing`` and ``transparency`` of the visits
        ``first`` to ``last`` (the frames of `pfs.qa.dashboard.agConditions.readSeeing` and
        ``readTransparency``). It is asked for the visits of each period as that period is read, so
        the conditions are read as the AG tables are: the run once, then the night under way.
    now : callable, optional
        Returns the current time (HST, naive); for the tests.

    Attributes
    ----------
    tables : `dict` [`str`, `pandas.DataFrame`]
        The run's tables as last read, with the conditions; empty before the first `refresh`.
    version : `int`
        Counts the reads that brought something new.
    """

    def __init__(
        self,
        readRun: Callable[[Any, Any], Mapping[str, pd.DataFrame]],
        start: Any,
        end: Any,
        readConditions: Callable[[int, int], Mapping[str, pd.DataFrame]] | None = None,
        now: Callable[[], Any] | None = None,
    ):
        self._readRun = readRun
        self._readConditions = readConditions
        self.start, self.end = pd.Timestamp(start), pd.Timestamp(end)
        self._now = now or (lambda: datetime.now(HST).replace(tzinfo=None))
        self._kept: dict[str, pd.DataFrame] = {}
        self._keptTo: pd.Timestamp | None = None
        self._signature: tuple | None = None
        self.tables: dict[str, pd.DataFrame] = {}
        self.version = 0

    @classmethod
    def fromTables(cls, tables: Mapping[str, pd.DataFrame]) -> "AgRunStore":
        """Return a store that holds ``tables`` (a snapshot's `agTables`) and reads nothing."""
        store = cls(lambda start, end: {}, pd.Timestamp.min, pd.Timestamp.min)
        store.tables = dict(tables)
        store.version = 1
        store.refresh = lambda: False  # type: ignore[method-assign]
        return store

    def _read(self, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, pd.DataFrame]:
        """Read a period's AG tables, and qadb's conditions for the visits its AG exposures were taken in."""
        tables = dict(self._readRun(start, end))
        exposures = tables.get("exposures")
        if self._readConditions is not None and exposures is not None and len(exposures):
            visits = exposures["pfs_visit_id"]
            tables.update(self._readConditions(int(visits.min()), int(visits.max())))
        return tables

    def refresh(self) -> bool:
        """Read what may have changed since the last read; return whether anything did."""
        now = min(pd.Timestamp(self._now()), self.end)
        if now <= self.start:
            return False
        keepTo = max(min(nightStart(now), self.end), self.start)
        if self._keptTo is None or keepTo > self._keptTo:
            ended = self._read(self._keptTo or self.start, keepTo) if keepTo > self.start else {}
            self._kept = _combine([self._kept, ended])
            self._keptTo = keepTo
        under = self._read(keepTo, now) if now > keepTo else {}
        tables = _combine([self._kept, under])
        # What is kept is settled; the night under way is compared by its values, since a guide offset can be
        # written after its exposure was first read.
        signature = (
            keepTo,
            tuple(
                (name, len(frame), int(pd.util.hash_pandas_object(frame, index=False).sum()))
                for name, frame in sorted(under.items())
            ),
        )
        changed = signature != self._signature
        if changed:
            self._signature = signature
            self.tables = tables
            self.version += 1
        return changed


# --- the page's pieces ------------------------------------------------------------------------------


def _number(value: float, digits: int = 2) -> str:
    """Return a number for a table: a dash when there is none."""
    return "–" if value is None or not np.isfinite(value) else f"{value:.{digits}f}"


def _flagHtml(text: str) -> str:
    """Return a flag's sentence (drp_qa's Markdown) as HTML, its visits linked to their pages."""

    def link(match: re.Match) -> str:
        return f"<a href='/visit/{match.group(0)}'>{match.group(0)}</a>"

    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    # A visit named alone, then the lists of them ("in 43 visits: ...", "worst: ...", "visit 1 (3), visit 2 (2)").
    out = re.sub(r"(?<=\bvisit )\d+", link, out)
    return re.sub(
        r"((?:visits|worst): )([^.]*)",
        lambda m: m.group(1) + re.sub(r"\b\d{5,}\b(?!×)", link, m.group(2)),
        out,
    )


def _table(frame: pd.DataFrame) -> str:
    """Return a summary of drp_qa's (a row per quantity, a column per camera or group) as an HTML table."""
    head = "".join(f"<th scope='col'>{html.escape(str(column))}</th>" for column in frame.columns)
    rows = []
    for label, row in frame.iterrows():
        whole = "exposures with" in str(label)
        cells = "".join(f"<td>{_number(value, 0 if whole else 2)}</td>" for value in row)
        rows.append(f"<tr><th scope='row'>{html.escape(str(label))}</th>{cells}</tr>")
    return (
        "<div class='tablewrap'><table><thead><tr><th scope='col' class='l'></th>"
        f"{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


@dataclass(frozen=True)
class _Strip:
    """The geometry of a chart against the run's nights: one slot a night, 18:00 to 06:00 HST across it."""

    nights: pd.DatetimeIndex
    height: int = 150
    width: int = _WIDE
    #: Whether the value axis is logarithmic: for values that span decades, such as the guide error.
    log: bool = False

    @property
    def right(self) -> float:
        return self.width - _RIGHT

    @property
    def plotWidth(self) -> float:
        return self.right - _LEFT

    @property
    def bottom(self) -> float:
        return self.height - _BOTTOM

    def x(self, position: float) -> float:
        """Map a position on the strip of nights (`runAnalysis.nightPosition`) to the chart's x."""
        return round(_LEFT + position / max(len(self.nights), 1) * self.plotWidth, 1)

    def y(self, value: float, low: float, high: float) -> float:
        """Map a value to the chart's y, clipped to ``[low, high]``."""
        value = min(max(value, low), high)
        if self.log:
            value, low, high = math.log10(value), math.log10(low), math.log10(high)
        fraction = (value - low) / (high - low) if high > low else 0.0
        return round(self.bottom - fraction * (self.bottom - _TOP), 1)

    def frame(self, low: float, high: float, label: str, right: Callable[[float], str] | None = None) -> str:
        """Return the chart's ground: its plot area, the nights, and the value axis with its ticks.

        ``right(value)`` labels the same ticks on the right, in another unit (AG pixels).
        """
        parts = [
            f"<rect class='plot' x='{_LEFT}' y='{_TOP}' width='{self.plotWidth}' height='{self.bottom - _TOP}'/>"
        ]
        count = len(self.nights)
        for index in range(1, count):
            parts.append(
                f"<line class='night' x1='{self.x(index)}' x2='{self.x(index)}' y1='{_TOP}' y2='{self.bottom}'/>"
            )
        # A label needs about 44 units: with many nights, every second or third is named.
        step = max(1, math.ceil(44 * count / self.plotWidth))
        for index, night in enumerate(self.nights):
            if index % step == 0:
                parts.append(
                    f"<text class='xlab' x='{self.x(index + 0.5)}' y='{self.bottom + 14}'>{night:%m-%d}</text>"
                )
        for tick in _logTicks(low, high) if self.log else _ticks(low, high):
            y = self.y(tick, low, high)
            parts.append(f"<line class='grid' x1='{_LEFT}' x2='{self.right}' y1='{y}' y2='{y}'/>")
            parts.append(f"<text class='ylab' x='{_LEFT - 6}' y='{y + 3.5}'>{tick:g}</text>")
            if right is not None:
                parts.append(f"<text class='rlab' x='{self.right + 6}' y='{y + 3.5}'>{right(tick)}</text>")
        parts.append(
            f"<text class='unit' transform='translate(12,{(self.bottom + _TOP) / 2}) rotate(-90)'>{html.escape(label)}</text>"
        )
        if right is not None:
            parts.append(
                f"<text class='unit' transform='translate({self.width - 6},{(self.bottom + _TOP) / 2}) rotate(-90)'>AG pixels</text>"
            )
        return "".join(parts)

    def svg(self, body: str, label: str) -> str:
        """Wrap a chart's elements as an inline SVG, in a box that scrolls sideways where the page is narrow."""
        return (
            f"<div class='agscroll'><svg class='agplot {'half' if self.width < _WIDE else 'wide'}' "
            f"viewBox='0 0 {self.width} {self.height}' role='img' aria-label='{html.escape(label)}'>"
            f"{body}</svg></div>"
        )


def _ticks(low: float, high: float, target: int = 4) -> list[float]:
    """Return about ``target`` round values across ``[low, high]``."""
    span = high - low
    if not np.isfinite(span) or span <= 0:
        return [low]
    raw = span / target
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)
    first = math.ceil(low / step - 1e-9) * step
    return [round(first + i * step, 10) for i in range(int((high - first) / step + 1e-9) + 1)]


def _logTicks(low: float, high: float) -> list[float]:
    """Return the values 1, 2 and 5 times a power of ten within ``[low, high]``."""
    ticks = [
        m * 10.0**power
        for power in range(math.floor(math.log10(low)), math.ceil(math.log10(high)) + 1)
        for m in (1, 2, 5)
    ]
    return [round(tick, 10) for tick in ticks if low <= tick <= high]


def _points(strip: _Strip, points: pd.DataFrame, low: float, high: float, unit: str) -> str:
    """Return a scatter's points, each linked to its visit; those beyond the top or bottom are marked at the edge.

    ``points`` has ``x`` (a position on the strip of nights), ``y``, ``visit``, ``when`` and ``cls``.
    """
    parts = []
    for row in points.dropna(subset=["x", "y"]).itertuples():
        title = f"<title>{int(row.visit)} · {row.when:%m-%d %H:%M} · {row.y:.3g}{unit}</title>"
        x = strip.x(row.x)
        if row.y > high:
            mark = f"<path class='pt off {row.cls}' d='M{x - 3.5},{_TOP + 7} h7 L{x},{_TOP + 1} Z'>{title}</path>"
        elif row.y < low:
            edge = strip.bottom
            mark = f"<path class='pt off {row.cls}' d='M{x - 3.5},{edge - 7} h7 L{x},{edge - 1} Z'>{title}</path>"
        else:
            mark = f"<circle class='pt {row.cls}' cx='{x}' cy='{strip.y(row.y, low, high)}' r='2.6'>{title}</circle>"
        parts.append(f"<a href='/visit/{int(row.visit)}'>{mark}</a>")
    return "".join(parts)


def _panel(title: str, svg: str) -> str:
    return f"<figure class='panel'><figcaption><b>{title}</b></figcaption>{svg}</figure>"


def _statesChart(run: Any, shares: pd.DataFrame, slots: int, rowHeight: int, label: str) -> str:
    """Return the map of camera states: a row a camera, a cell a slot of each night, shaded by what it gave."""
    from pfs.drp.qa.guiders.runPlotting import CAMERA_ORDER

    nights = run.plottedNights
    strip = _Strip(nights, height=_TOP + 6 * rowHeight + 8 + _BOTTOM)
    index = {night: i for i, night in enumerate(nights)}
    rowY = {cid: _TOP + row * rowHeight + (8 if row >= 3 else 0) for row, cid in enumerate(CAMERA_ORDER)}
    cellWidth = round(0.9 / slots / max(len(nights), 1) * strip.plotWidth, 2)
    parts = []
    for i in range(1, len(nights)):
        parts.append(
            f"<line class='night' x1='{strip.x(i)}' x2='{strip.x(i)}' y1='{_TOP}' y2='{strip.bottom}'/>"
        )
    step = max(1, math.ceil(44 * len(nights) / strip.plotWidth))
    for i, night in enumerate(nights):
        if i % step == 0:
            parts.append(
                f"<text class='xlab' x='{strip.x(i + 0.5)}' y='{strip.bottom + 14}'>{night:%m-%d}</text>"
            )
    names = (("dark", "no detections"), ("unmatched", "detections, no match"), ("disabled", "disabled"))
    for cid, y in rowY.items():
        parts.append(f"<text class='ylab' x='{_LEFT - 6}' y='{y + rowHeight / 2 + 3}'>AG{cid + 1}</text>")
        if cid not in run.inUse:
            parts.append(
                f"<rect class='unused' x='{_LEFT}' y='{y}' width='{strip.plotWidth}' height='{rowHeight - 2}'/>"
                f"<text class='note' x='{_LEFT + strip.plotWidth / 2}' y='{y + rowHeight / 2 + 3}'>"
                "not in use: no detections in the run</text>"
            )
            continue
        for cell in shares[shares.agc_camera_id == cid].itertuples():
            x = strip.x(index[cell.night] + 0.05 + 0.9 * cell.slot / slots)
            judged = cell.n - cell.few_stars
            when = f"AG{cid + 1} · {cell.start:%m-%d %H:%M}"
            box = f"x='{x}' y='{y}' width='{cellWidth}' height='{rowHeight - 2}'"
            if judged == 0:
                parts.append(
                    f"<rect class='st few' {box}><title>{when} · too few bright guide stars designed "
                    f"({int(cell.n)} AG exposures)</title></rect>"
                )
                continue
            missed = judged - cell.matched
            worst = max(names, key=lambda name: getattr(cell, name[0]))[0]
            detail = ", ".join(
                f"{int(getattr(cell, key))} {text}" for key, text in names if getattr(cell, key)
            )
            title = f"{when} · matched a star in {int(cell.matched)} of {int(judged)} AG exposures" + (
                f": {detail}" if detail else ""
            )
            parts.append(f"<rect class='st' {box}><title>{title}</title></rect>")
            if missed:
                parts.append(
                    f"<rect class='st {worst}' {box} fill-opacity='{missed / judged:.2f}'><title>{title}</title></rect>"
                )
    return strip.svg("".join(parts), label)


_STATE_LEGEND = (
    "<div class='legend'>"
    "<span><i class='key st'></i>matched a star</span>"
    "<span><i class='key dark'></i>no detections</span>"
    "<span><i class='key unmatched'></i>detections, no match</span>"
    "<span><i class='key disabled'></i>disabled</span>"
    "<span><i class='key few'></i>too few bright guide stars designed</span>"
    "</div>"
)


#: What the points of a per-visit chart are, for its legend.
_VISIT_KEYS = (
    "<span><i class='key dot every'></i>science visit, stars matched on every camera in use</span>"
    "<span><i class='key dot fewer'></i>on fewer cameras</span>"
)
_OFF_SCALE_KEY = "<span><i class='key off'></i>beyond the scale, marked at its edge</span>"


def _figurePx(inches: tuple[float, float]) -> tuple[int, int]:
    """Return the size (pixels) of one of drp_qa's figures as `_render` draws it."""
    return round(inches[0] * _DPI), round(inches[1] * _DPI)


def _guidingCharts(run: Any, jitter: pd.DataFrame) -> str:
    """Return each science visit's guide error: per axis, its size and its jitter against the nights."""
    from pfs.drp.qa.guiders.runAnalysis import nightPosition

    nights = run.plottedNights
    jitter = jitter[jitter.night.isin(nights)]
    strip = _Strip(nights, width=_HALF, log=True)
    base = pd.DataFrame(
        {
            "x": nightPosition(jitter.taken_at, nights),
            "visit": jitter.pfs_visit_id,
            "when": jitter.taken_at,
            "cls": np.where(jitter.full.astype(bool), "", "few"),
        }
    )
    panels = []
    for axis, name, pixels in (("ra", "ΔRA on the sky", True), ("dec", "ΔDec", True), ("inr", "ΔInR", False)):
        for column, what in ((f"{axis}_size", "size (RMS offset)"), (axis, "frame-to-frame jitter")):
            values = jitter[column].astype(float)
            finite = values[np.isfinite(values) & (values > 0)]
            median = float(finite.median()) if len(finite) else float("nan")
            # A visit's error spans two decades and more across a run, so the axis is logarithmic: the bulk and
            # the worst visits are both readable, and none is off the scale.
            low, high = (
                (float(finite.min()) / 1.25, float(finite.max()) * 1.25) if len(finite) else (0.1, 1.0)
            )
            body = strip.frame(
                low, high, "arcsec", (lambda v: f"{v / _PIXEL_ARCSEC:.2g}") if pixels else None
            )
            if np.isfinite(median):
                y = strip.y(median, low, high)
                body += f"<line class='median' x1='{_LEFT}' x2='{strip.right}' y1='{y}' y2='{y}'/>"
            body += _points(strip, base.assign(y=values.to_numpy()), low, high, "″")
            title = f"{name}, {what} <span class='sub'>median {_number(median, 3)}″</span>"
            panels.append(_panel(title, strip.svg(body, f"{name}, {what}, per science visit")))
    legend = (
        f"<div class='legend'>{_VISIT_KEYS}<span><i class='key medianline'></i>the run's median</span></div>"
    )
    return f"{legend}<div class='pair'>{''.join(panels)}</div>"


def _problemsChart(run: Any) -> str:
    """Return, per night, the AG exposures whose guide offset was rejected or never written."""
    from pfs.drp.qa.guiders.runAnalysis import isRejectedOffset

    nights = run.plottedNights
    frames = run.frames[run.frames.night.isin(nights)]
    perNight = frames.groupby("night")["mask"].agg(
        rejected=lambda mask: int(isRejectedOffset(mask).sum()), missing=lambda mask: int(mask.isna().sum())
    )
    strip = _Strip(nights, height=120)
    top = max(5.0, float(perNight.to_numpy().max(initial=0)) * 1.25)
    body = strip.frame(0, top, "AG exposures")
    width = round(0.3 / max(len(nights), 1) * strip.plotWidth, 1)
    for night, row in perNight.iterrows():
        position = list(nights).index(night)
        for offset, column, text in (
            (0.18, "rejected", "rejected"),
            (0.52, "missing", "with no guide offset"),
        ):
            value = int(row[column])
            if not value:
                continue
            x, y = strip.x(position + offset), strip.y(value, 0, top)
            body += (
                f"<rect class='bar {column}' x='{x}' y='{y}' width='{width}' height='{round(strip.bottom - y, 1)}'>"
                f"<title>{night:%m-%d}: {value} AG exposures {text}</title></rect>"
                f"<text class='count' x='{x + width / 2}' y='{y - 2}'>{value}</text>"
            )
    legend = (
        "<div class='legend'><span><i class='key rejected'></i>rejected (INVALID_OFFSET)</span>"
        "<span><i class='key missing'></i>no guide offset written</span></div>"
    )
    return _panel("Guide-loop problems per night", strip.svg(body, "Guide-loop problems per night")) + legend


def _focusCharts(run: Any) -> str:
    """Return the guider's focus error per science visit, and the telescope's focus offset through the run."""
    from pfs.drp.qa.guiders.runAnalysis import nightPosition

    nights = run.plottedNights
    frames = run.frames[run.frames.night.isin(nights)]
    visits = (
        frames[frames.population.eq("science")]
        .groupby("pfs_visit_id")
        .agg(taken_at=("taken_at", "median"), dz=("guide_delta_z", "median"), n_cams=("n_cams", "median"))
        .reset_index()
    )
    visits["dz"] *= 1e3
    strip = _Strip(nights)
    finite = visits.dz[np.isfinite(visits.dz)]
    low, high = (-10.0, 10.0)
    if len(finite):
        p2, p98 = np.percentile(finite, [2, 98])
        pad = 0.3 * max(p98 - p2, 1)
        low, high = float(p2 - pad), float(p98 + pad)
    body = strip.frame(low, high, "µm")
    if low < 0 < high:
        y = strip.y(0, low, high)
        body += f"<line class='zero' x1='{_LEFT}' x2='{strip.right}' y1='{y}' y2='{y}'/>"
    points = pd.DataFrame(
        {
            "x": nightPosition(visits.taken_at, nights),
            "y": visits.dz,
            "visit": visits.pfs_visit_id,
            "when": visits.taken_at,
            "cls": np.where(visits.n_cams >= len(run.inUse), "", "few"),
        }
    )
    body += _points(strip, points, low, high, " µm")
    out = (
        _panel(
            "Guider focus error dZ <span class='sub'>median of the cameras in use, per science visit</span>",
            strip.svg(body, "Guider focus error per science visit"),
        )
        + f"<div class='legend'>{_VISIT_KEYS}{_OFF_SCALE_KEY}</div>"
    )

    focus = run.focus[run.focus.night.isin(nights) & (run.focus.m2_off3 > 0)].sort_values("created_at")
    strip = _Strip(nights, height=130)
    if len(focus):
        low, high = float(focus.m2_off3.min()), float(focus.m2_off3.max())
        pad = 0.08 * max(high - low, 0.01)
        low, high = low - pad, high + pad
        body = strip.frame(low, high, "mm")
        # A step line per night, from the points at which the offset changed.
        for _, night in focus.groupby("night"):
            changed = night[night.m2_off3.diff().fillna(1).ne(0) | (night.index == night.index[-1])]
            xs = [strip.x(p) for p in nightPosition(changed.created_at, nights)]
            ys = [strip.y(v, low, high) for v in changed.m2_off3]
            path = f"M{xs[0]},{ys[0]}" + "".join(f" H{x} V{y}" for x, y in zip(xs[1:], ys[1:], strict=True))
            body += f"<path class='step' d='{path}'/>"
        out += _panel(
            "Telescope focus offset <span class='sub'>tel_status.m2_off3</span>",
            strip.svg(body, "Telescope focus offset through the run"),
        )
    return out


def _acquisitionChart(run: Any) -> str:
    """Return the first guide offset of each convergence visit, ΔRA on the sky and ΔDec."""
    from pfs.drp.qa.guiders.runAnalysis import nightPosition, onSkyRa

    nights = run.plottedNights
    frames = run.frames[
        run.frames.night.isin(nights) & run.frames.population.eq("convergence") & run.frames["mask"].notna()
    ]
    first = frames.groupby("pfs_visit_id").head(1)
    limit = float(run.config.maxCorrection)
    both = np.abs(np.r_[onSkyRa(first).to_numpy(float), first.guide_delta_dec.to_numpy(float)])
    both = both[np.isfinite(both)]
    high = max(1.2 * limit, float(np.percentile(both, 99)) * 1.1 if len(both) else 0.0)
    strip = _Strip(nights)
    body = strip.frame(-high, high, "arcsec", lambda v: f"{v / _PIXEL_ARCSEC:.0f}")
    for y in (strip.y(-limit, -high, high), strip.y(limit, -high, high)):
        body += f"<line class='limit' x1='{_LEFT}' x2='{strip.right}' y1='{y}' y2='{y}'/>"
    x = nightPosition(first.taken_at, nights)
    for values, cls in ((onSkyRa(first), "s1"), (first.guide_delta_dec, "s2")):
        points = pd.DataFrame(
            {
                "x": x,
                "y": values.astype(float),
                "visit": first.pfs_visit_id,
                "when": first.taken_at,
                "cls": cls,
            }
        )
        body += _points(strip, points, -high, high, "″")
    legend = (
        "<div class='legend'><span><i class='key s1 dot'></i>ΔRA on the sky</span>"
        "<span><i class='key s2 dot'></i>ΔDec</span>"
        f"<span><i class='key limit'></i>max_correction, ±{limit:g}″</span>{_OFF_SCALE_KEY}</div>"
    )
    return (
        _panel(
            "First guide offset of each convergence visit",
            strip.svg(body, "First guide offset of each convergence visit"),
        )
        + legend
    )


def _nightly(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Return a condition's median and quartiles per night, from one row per AG exposure (``time``, ``value``)."""
    columns = ["night", "q25", "median", "q75", "n"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    night = (pd.to_datetime(frame["time"]) - pd.Timedelta(hours=NIGHT_START)).dt.normalize()
    grouped = frame.groupby(night)["value"]
    out = grouped.quantile([0.25, 0.5, 0.75]).unstack()
    out.columns = ["q25", "median", "q75"]
    return out.assign(n=grouped.size()).rename_axis("night").reset_index()[columns]


def _conditionsCharts(run: Any, tables: Mapping[str, pd.DataFrame]) -> str:
    """Return the night-by-night seeing and transparency qadb holds, or ``""`` when it holds neither."""
    nights = list(run.plottedNights)
    out = ""
    for name, title, unit, high in (
        ("seeing", "Seeing <span class='sub'>AG FWHM</span>", "arcsec", 2.0),
        (
            "transparency",
            "Transparency <span class='sub'>uncalibrated: a clear sky reads about 0.8</span>",
            "",
            1.2,
        ),
    ):
        nightly = _nightly(tables.get(name))
        nightly = nightly[nightly.night.isin(nights)]
        if nightly.empty:
            continue
        strip = _Strip(run.plottedNights, height=130)
        body = strip.frame(0, high, unit or "fraction")
        for row in nightly.itertuples():
            x = strip.x(nights.index(row.night) + 0.5)
            tip = (
                f"<title>{row.night:%m-%d}: median {row.median:.2f}{'″' if unit else ''}, "
                f"{row.q25:.2f} to {row.q75:.2f}; {int(row.n)} AG exposures</title>"
            )
            body += (
                f"<line class='iqr' x1='{x}' x2='{x}' y1='{strip.y(row.q25, 0, high)}' y2='{strip.y(row.q75, 0, high)}'>{tip}</line>"
                f"<circle class='pt night' cx='{x}' cy='{strip.y(row.median, 0, high)}' r='3.4'>{tip}</circle>"
            )
        out += _panel(title, strip.svg(body, f"{name} per night"))
    if out:
        out += (
            "<div class='legend'><span><i class='key dot s1'></i>the night's median</span>"
            "<span><i class='key iqr'></i>the middle half of its AG exposures</span></div>"
        )
    return out


def _tiles(
    summary: dict,
    chosen: tuple[str, ...],
    flags: list | None = None,
    tables: Mapping[str, pd.DataFrame] | None = None,
) -> str:
    """Return key numbers as tiles: those of ``chosen`` that there is something to show for.

    A visit is a science visit measured with its shutters open (drp_qa's `visitGuideErrors`), of the run or of
    the night the summary is of.
    """
    error, stars, loop = summary["guideError"]["sky"], summary["matchedStars"], summary["guideLoop"]
    matched = summary["matchedFraction"]
    inUse = summary["camerasInUse"]
    unused = [f"AG{n}" for n in range(1, 7) if f"AG{n}" not in inUse]
    tables = tables or {}

    def tile(label: str, big: str, facts: list[str], what: str, cls: str = "", of: str = "") -> str:
        """Return a tile: one number, what it is the value of (``of``), and separate facts about it as a list.

        ``what`` says in full what the number is, on hover.
        """
        items = "".join(f"<li>{fact}</li>" for fact in facts)
        listed = f"<ul class='facts'>{items}</ul>" if items else ""
        under = f"<span class='sub'>{of}</span>" if of else ""
        return (
            f"<div class='tile {cls}' title='{html.escape(what)}'><span class='lab'>{label}</span>"
            f"<span class='big'>{big}</span>{under}{listed}</div>"
        )

    def value(number: str, unit: str) -> str:
        """Return a tile's number with its unit beside it."""
        gap = " " if unit.startswith(" ") else ""
        return f"{number}{gap}<span class='unit'>{unit.strip()}</span>"

    def medianOf(count: int) -> str:
        """Say what a tile's value is the median of: the visits, one value each, and how many there were."""
        return f"median of {count:,} visit{'' if count == 1 else 's'}"

    # A bullet is one fact on its own: the same value in another unit, or how far the visits spread.
    tiles = {
        "cameras": tile(
            "Cameras in use",
            value(str(len(inUse)), " of 6"),
            [f"<b>{name}</b> not in use" for name in unused],
            "The cameras that detected anything in these AG exposures.",
            "warn" if unused else "",
        ),
    }
    # With no visit measured (a read with no shutter times), there is no guide error to show.
    if summary["measuredVisits"]:
        tiles["error"] = tile(
            "Guide error",
            value(_number(error["size"], 2), "″"),
            [
                f"<b>{_number(error['size'] / _PIXEL_ARCSEC, 1)}</b> AG pixels",
                f"95% of visits under <b>{_number(error['size95'], 2)}″</b>",
            ],
            "The RMS of a science visit's guide offsets on the sky (ΔRA and ΔDec together) in the AG exposures "
            "taken with the shutters open, one value a visit: the median of those values, and the value 95% of "
            "visits are under.",
            of=medianOf(summary["measuredVisits"]),
        )
        tiles["jitter"] = tile(
            "Frame-to-frame jitter",
            value(_number(error["jitter"], 2), "″"),
            [
                f"<b>{_number(error['jitter'] / _PIXEL_ARCSEC, 1)}</b> AG pixels",
                f"95% of visits under <b>{_number(error['jitter95'], 2)}″</b>",
            ],
            "How much a science visit's guide offset on the sky moves from one AG exposure to the next, with "
            "the shutters open, one value a visit: the median of those values, and the value 95% of visits are "
            "under.",
            of=medianOf(summary["measuredVisits"]),
        )
    if np.isfinite(matched["median"]):
        tiles["matched"] = tile(
            "Designed guide stars matched",
            value(_number(100 * matched["median"], 0), "%"),
            [
                f"5% of visits under <b>{_number(100 * matched['p5'], 0)}%</b>",
                f"5% of visits over <b>{_number(100 * matched['p95'], 0)}%</b>",
            ],
            "The share of a science visit's designed guide stars, on the cameras in use, that were matched, one "
            "value a visit: the median of those values, and the shares 5% of visits are under and over.",
            of=medianOf(matched["visits"]),
        )
    tiles["residual"] = tile(
        "Star position, measured − expected",
        value(_number(summary["residualUm"]["median"], 1), " µm median"),
        [
            f"<b>{_number(summary['residualUm']['median'] / _PIXEL_UM, 2)}</b> AG pixels",
            f"95% of AG exposures under <b>{_number(summary['residualUm']['p95'], 1)} µm</b>",
        ],
        "The distance of a matched star from where the guider expected it: the median science AG exposure, and "
        "the value 95% of them are under.",
    )
    tiles["tooFew"] = tile(
        "AG exposures with under 3 stars",
        value(_number(stars["underMinimumPct"], 2), "%"),
        ["of the science AG exposures"],
        "The share of science AG exposures in which fewer than 3 guide stars were matched.",
    )
    tiles["notUsed"] = tile(
        "Guide offsets not used",
        value(str(loop["rejected"]), " rejected"),
        [f"<b>{loop['noOffset']}</b> never written", f"of <b>{summary['exposures']:,}</b> AG exposures"],
        "AG exposures whose guide offset the guider rejected (INVALID_OFFSET), or never wrote, of all the AG "
        "exposures.",
        "fail" if loop["rejected"] else "",
    )
    seeing, transparency = tables.get("seeing"), tables.get("transparency")
    if seeing is not None and len(seeing):
        facts = []
        if transparency is not None and len(transparency):
            facts.append(f"transparency <b>{transparency['value'].median():.2f}</b>")
        tiles["seeing"] = tile(
            "Seeing (AG FWHM)",
            value(f"{seeing['value'].median():.2f}", "″ median"),
            facts,
            "The median seeing (AG FWHM) and transparency of the run's AG exposures, as stored in qadb.",
        )
    elif "residual" in chosen:
        tiles["seeing"] = tile(
            "FWHM of matched stars",
            value(_number(summary["fwhm"], 2), "″ median"),
            [],
            "The median FWHM of the matched stars, over cameras and AG exposures.",
        )
    if flags is not None:
        severe = sum(1 for flag in flags if flag.severe)
        tiles["flags"] = tile(
            "Flags",
            value(str(severe), f" fault{'' if severe == 1 else 's'}"),
            [f"<b>{len(flags) - severe}</b> more to look at"],
            "What the analysis flags: faults, and things worth a look. They are listed under Flags.",
            "fail" if severe else "",
        )
    # Each tile is named in the markup, so it can be found whatever it says.
    named = [
        tiles[name].replace("<div class='tile", f"<div data-tile='{name}' class='tile", 1)
        for name in chosen
        if name in tiles
    ]
    return f"<div class='tiles'>{''.join(named)}</div>"


def nightTables(tables: Mapping[str, pd.DataFrame], night: str) -> dict[str, pd.DataFrame]:
    """Return the rows of a run's AG tables that belong to one night (its evening's date, ``YYYY-MM-DD``).

    The ``grid`` is left out: the night page's numbers do not use it.
    """
    from pfs.drp.qa.guiders.runAnalysis import nightOf

    when = pd.Timestamp(night)
    exposures = tables["exposures"]
    exposures = exposures[nightOf(exposures["taken_at"]) == when]
    visits = set(exposures["pfs_visit_id"])
    out = {"exposures": exposures, "designStars": tables["designStars"]}
    cameras, focus = tables["cameras"], tables["focus"]
    out["cameras"] = cameras[cameras["agc_exposure_id"].isin(exposures["agc_exposure_id"])]
    out["visits"] = tables["visits"][tables["visits"]["pfs_visit_id"].isin(visits)]
    out["focus"] = focus[nightOf(focus["created_at"]) == when]
    if "magnitudes" in tables:
        out["magnitudes"] = tables["magnitudes"][tables["magnitudes"]["pfs_visit_id"].isin(visits)]
    return out


def buildNightTiles(tables: Mapping[str, pd.DataFrame], night: str, agUrl: str = "/run/ag") -> str:
    """Return one night's auto-guiding as a row of tiles, for the night page; ``""`` when there is nothing to show.

    The same numbers as the AG report's tiles (`pfs.drp.qa.guiders.runAnalysis.runSummary`), over the
    night's AG exposures alone.
    """
    from pfs.drp.qa.guiders import runAnalysis

    exposures = tables.get("exposures")
    if exposures is None or exposures.empty or not night:
        return ""
    run = runAnalysis.prepareRun(nightTables(tables, night))
    if not len(run.plottedNights):
        return ""
    summary = runAnalysis.runSummary(run, runAnalysis.visitGuideErrors(run))
    return (
        "<section class='block' id='ag'>"
        f"<p class='meta'>Auto-guiding this night, from {summary['exposures']:,} AG exposures and "
        f"{summary['scienceVisits']} science visits · "
        f"<a href='{html.escape(agUrl)}'>the run's AG report</a></p>"
        + _tiles(summary, _NIGHT_TILES)
        + "</section>"
    )


@dataclass
class AgReport:
    """A run's AG report: the page, the block the run report carries, and what the figures are drawn from.

    Attributes
    ----------
    html : `str`
        The page.
    summary : `str`
        The block of it the run report shows, or ``""`` when there is nothing to show.
    run, clusters, modelErrors
        The prepared run and its rejection clusters and position-model errors
        (`pfs.drp.qa.guiders.runAnalysis`); None for a run with no AG exposure.
    """

    html: str
    summary: str = ""
    run: Any = None
    clusters: pd.DataFrame | None = None
    modelErrors: pd.DataFrame | None = None
    _images: dict[int, bytes] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def cameraPng(self, camera: int) -> bytes | None:
        """Return camera ``camera``'s figure (AG1 is 1) as PNG bytes, drawn once.

        None when there is none: no such camera, no run, or a camera not in use, which has nothing to draw.
        """
        from pfs.drp.qa.guiders.runPlotting import plotCamera

        if self.run is None or camera - 1 not in self.run.inUse:
            return None
        with self._lock:
            if camera not in self._images:
                self._images[camera] = _render(
                    plotCamera, self.run, camera - 1, self.clusters, self.modelErrors
                )
            return self._images[camera]

    def focalPlanePng(self) -> bytes | None:
        """Return the figure of the six detectors on the PFI as PNG bytes, drawn once; None when there is no run."""
        from pfs.drp.qa.guiders.runPlotting import plotFocalPlane

        if self.run is None:
            return None
        with self._lock:
            if 0 not in self._images:
                self._images[0] = _render(plotFocalPlane, self.run, self.modelErrors)
            return self._images[0]


def _render(plot: Callable[..., Any], *args: Any) -> bytes:
    """Draw one of drp_qa's figures (``plot(*args)``) and return it as PNG bytes."""
    # matplotlib isn't thread-safe: one lock for every figure the server draws.
    from pfs.qa.dashboard.visitResiduals import _RENDER_LOCK

    with _RENDER_LOCK:
        buffer = io.BytesIO()
        plot(*args).savefig(buffer, format="png", dpi=_DPI)
        return buffer.getvalue()


def _page(title: str, runUrl: str, meta: str, body: str) -> str:
    """Fill the page's template: ``title`` is the run report's, which the link back to it carries."""
    page = (_DATA / "agRun.html").read_text()
    fields = {
        # First, so the stylesheet it injects still carries ``{{FONT_FACES}}`` for the next substitution.
        "{{THEME}}": _theme(),
        "{{FONT_FACES}}": _fontFaces(),
        "{{RUN_TITLE}}": html.escape(title or "Run report"),
        "{{RUN_URL}}": html.escape(runUrl),
        "{{META}}": meta,
        "{{BODY}}": body,
    }
    for key, value in fields.items():
        page = page.replace(key, value)
    return page


def _empty(title: str, runUrl: str, why: str) -> str:
    """Return the page for a run with no AG data to show."""
    return _page(title, runUrl, html.escape(why), "")


def buildAgReport(
    tables: Mapping[str, pd.DataFrame],
    title: str = "",
    source: str = "",
    now: datetime | None = None,
    runUrl: str = "/run",
    agUrl: str = "/run/ag",
    embed: bool = False,
) -> AgReport:
    """Return the AG report of a run's tables: the page and the run report's block.

    Parameters
    ----------
    tables : mapping
        The tables of `pfs.drp.qa.guiders.queries.readAgRun` (an `AgRunStore`'s), with ``seeing``
        and ``transparency`` when qadb's conditions were read.
    title : `str`
        The run report's title, which names the run on the link back to it.
    source : `str`
        Where the tables were read from, as HTML, for the page's last note.
    now : `datetime.datetime`, optional
        When the page was made; the current time (HST) by default.
    runUrl, agUrl : `str`
        Where the run report and this page are served (or, written as files, what they are called).
    embed : `bool`
        Put the cameras' figures in the page itself, for a page written as a file; otherwise the
        page asks for ``<agUrl>/focalPlane.png`` and ``<agUrl>/camera/AG<n>.png`` (`agRoutes`).
    """
    from pfs.drp.qa.guiders import runAnalysis
    from pfs.drp.qa.guiders.runPlotting import CAMERA_ORDER, GROUPS_NOTE, captions

    exposures = tables.get("exposures")
    if exposures is None or exposures.empty:
        return AgReport(_empty(title, runUrl, "No AG exposure has been read for this run yet."))
    run = runAnalysis.prepareRun({name: frame for name, frame in tables.items() if name not in CONDITIONS})
    if not len(run.plottedNights):
        return AgReport(_empty(title, runUrl, "No night of this run has enough AG exposures to report yet."))
    config = run.config
    jitter = runAnalysis.visitGuideErrors(run)
    dropouts = runAnalysis.findDropouts(run)
    clusters = runAnalysis.findRejectionClusters(run)
    modelErrors = runAnalysis.fitModelErrors(run)
    flags = runAnalysis.findFlags(run, jitter, dropouts, clusters, modelErrors)
    summary = runAnalysis.runSummary(run, jitter)
    caption = captions(config)
    report = AgReport("", run=run, clusters=clusters, modelErrors=modelErrors)

    def flagList(chosen: list) -> str:
        if not chosen:
            return "<ul class='flags'><li class='none'>No flags.</li></ul>"
        return (
            "<ul class='flags'>"
            + "".join(f"<li class='{'severe' if f.severe else ''}'>{_flagHtml(f.text)}</li>" for f in chosen)
            + "</ul>"
        )

    def section(heading: str, body: str, note: str = "", anchor: str = "") -> str:
        ident = f" id='{anchor}'" if anchor else ""
        text = f"<p>{html.escape(note)}</p>" if note else ""
        return (
            f"<details class='block' open{ident}><summary><h2>{heading}</h2></summary>{text}{body}</details>"
        )

    from pfs.drp.qa.guiders.runPlotting import CAMERA_FIGURE_IN, FOCAL_PLANE_FIGURE_IN

    def figure(src: str, data: Callable[[], bytes | None], inches: tuple[float, float], alt: str) -> str:
        """Return a figure of drp_qa's as an image linked to itself: held in the page, or asked for by it."""
        if embed:
            src = "data:image/png;base64," + base64.b64encode(data() or b"").decode("ascii")
        width, height = _figurePx(inches)
        return (
            f"<a class='camera' href='{src}'><img loading='lazy' width='{width}' height='{height}' "
            f"alt='{alt}' src='{src}'></a>"
        )

    focalPlane = figure(
        f"{agUrl}/focalPlane.png",
        report.focalPlanePng,
        FOCAL_PLANE_FIGURE_IN,
        "The six AG detectors on the PFI",
    )
    cameras = []
    for cid in CAMERA_ORDER:
        own = [flag for flag in flags if flag.agc_camera_id == cid]
        name = f"AG{cid + 1}"
        if cid not in run.inUse:
            # Nothing to draw: the flag says it is not in use.
            cameras.append(f"<h3 id='{name}'>{name}</h3>{flagList(own)}")
            continue
        cameras.append(
            f"<h3 id='{name}'>{name}</h3>{flagList(own)}"
            + figure(
                f"{agUrl}/camera/{name}.png",
                functools.partial(report.cameraPng, cid + 1),
                CAMERA_FIGURE_IN,
                f"{name} over the run",
            )
        )
    populations = (
        run.frames.groupby("population")
        .agg(visits=("pfs_visit_id", "nunique"), exposures=("agc_exposure_id", "size"))
        .rename_axis(None)
    )
    populationRows = "".join(
        f"<tr><th scope='row'>{html.escape(str(name))}</th><td>{int(row.visits):,}</td>"
        f"<td>{int(row.exposures):,}</td></tr>"
        for name, row in populations.iterrows()
    )
    skipped = run.nights[run.nights < config.minFramesNight]
    conditions = _conditionsCharts(run, tables)
    sections = [
        section("Flags", flagList(flags), anchor="flags"),
        section(
            "Camera states",
            _statesChart(run, runAnalysis.stateShares(run), 12, 26, "Camera states per hour")
            + _STATE_LEGEND
            + f"<h3>AG1/3/5 against AG2/4/6</h3><p>{html.escape(GROUPS_NOTE)}</p>"
            + _table(runAnalysis.groupSummary(run)),
            caption["cameraPresence"],
            "states",
        ),
        section(
            "Guiding",
            _guidingCharts(run, jitter) + _problemsChart(run),
            caption["guiding"],
            "guiding",
        ),
        section("Focus", _focusCharts(run), caption["focus"], "focus"),
        section("Acquisition", _acquisitionChart(run), caption["acquisition"], "acquisition"),
    ]
    if conditions:
        sections.append(
            section(
                "Seeing and transparency",
                conditions,
                "Each night's median, with its 25th to 75th percentile, of the AG exposures' seeing and "
                "transparency as stored in qadb.",
                "conditions",
            )
        )
    sections.append(
        section(
            "Cameras",
            focalPlane
            + "<h3>Each camera</h3><p>"
            + html.escape(
                caption["camera"]
                + " A rejected match is a detection paired with a guide star but not counted as a good match. "
                "Directions are on the detector: 0° = +x, 90° = +y."
            )
            + "</p>"
            + _table(runAnalysis.cameraSummary(run, clusters, modelErrors))
            + "".join(cameras),
            caption["focalPlane"],
            "cameras",
        )
    )
    sections.append(
        "<details class='block'><summary><h2>AG exposures</h2></summary>"
        "<div class='tablewrap'><table><thead><tr><th scope='col' class='l'></th><th scope='col'>Visits</th>"
        f"<th scope='col'>AG exposures</th></tr></thead><tbody>{populationRows}</tbody></table></div>"
        + (
            f"<p class='notes'>Not drawn (fewer than {config.minFramesNight} AG exposures): "
            + ", ".join(f"{night:%m-%d} ({n})" for night, n in skipped.items())
            + ".</p>"
            if len(skipped)
            else ""
        )
        + "</details>"
    )
    now = datetime.now(HST) if now is None else now
    meta = (
        f"{runAnalysis.describePeriod(run)} · made {now:%Y-%m-%d %H:%M} HST<br>"
        "1 AG pixel = 13 µm = 0.14″. FWHM is 2.05 × the stars' sigma, the radius of their second moments. "
        "Offsets in arcsec, ΔRA on the sky (ΔRA cos Dec). Times HST."
    )
    notes = (
        "<section class='block notes'><ul>"
        "<li>The numbers, the flags and the captions are drp_qa's end-of-run AG analysis "
        "(<code>drpQa.py agReport</code>), on the AG tables of the opdb; seeing and transparency are qadb's.</li>"
        + (
            f"<li><strong>Source:</strong> {source.replace('{date}', f'{now:%Y-%m-%d}')}</li>"
            if source
            else ""
        )
        + "</ul></section>"
    )
    report.html = _page(
        title, runUrl, meta, _tiles(summary, _PAGE_TILES, flags, tables) + "".join(sections) + notes
    )

    severe = [flag for flag in flags if flag.severe]
    report.summary = (
        "<details class='block' open id='ag'><summary><h2>Auto-guiding</h2></summary>"
        f"<p>The guide cameras and the guide loop over the run, from {summary['exposures']:,} AG exposures on "
        f"{summary['nights']} nights. <a href='{html.escape(agUrl)}'>The AG report</a> has each camera, the "
        "guide error of every science visit, focus and acquisition.</p>"
        + _tiles(summary, _RUN_TILES, flags, tables)
        + (flagList(severe) if severe else "")
        + "</details>"
    )
    return report


class AgRunCache(ReportCache):
    """The AG page of an `AgRunStore`, made again when a read brings something new.

    A `ReportCache` over the AG tables: `watch` reads on a thread of its own, `pageFor` and `status`
    answer the page and what its live script asks for. Its one page is ``ag``.

    Parameters
    ----------
    store : `AgRunStore`
        The store.
    **options
        What `buildAgReport` takes besides the tables.
    """

    def __init__(self, store: AgRunStore, rebuildEvery: float = AG_REBUILD, **options: Any):
        super().__init__(store, pages={"ag": (buildAgReport, options)})
        self._options = options
        self._rebuildEvery = rebuildEvery
        self._madeAt: float | None = None
        self._nightTiles: dict[tuple[int, str], str] = {}

    def pageFor(self, name: str = "ag") -> str:
        """Return the page made last; before the first read has worked, a page that says so (it reads nothing)."""
        if name not in self.reports:
            why = "The AG tables have not been read yet" + (f": {self.error}" if self.error else ".")
            return _empty(self._options.get("title", ""), self._options.get("runUrl", "/run"), why)
        return super().pageFor(name)

    def update(self) -> bool:
        """Read the opdb for what is new, and make the page again if it is due; return whether it was made.

        The store is read every time (the night page's tiles follow it); the page, which is about the
        whole run, is made again when a read has brought something new and at least ``rebuildEvery``
        seconds have passed since it was last made.
        """
        with self._lock:
            self._timings = {}
            mark = time.monotonic()
            self.store.refresh()
            mark, self._timings["opdb"] = time.monotonic(), time.monotonic() - mark
            version = self.store.version
            due = self._madeAt is None or mark - self._madeAt >= self._rebuildEvery
            changed = not self.reports or (self._version != version and due)
            if changed:
                self.reports = {
                    name: builder(self.store.tables, **options)
                    for name, (builder, options) in self._pages.items()
                }
                self.built += 1
                self._version = version
                self._madeAt = mark
            self._timings["rebuild"] = time.monotonic() - mark
            self.readAt = datetime.now(HST)
            self.error = None
            return changed

    def nightTiles(self, night: str) -> str:
        """Return a night's AG tiles for the night page, from the tables last read; ``""`` when there are none.

        It reads nothing. A night's tiles are kept until the next read that brings something new; a
        failure to make them is logged and leaves the night page without them.
        """
        key = (self.store.version, night)
        if key not in self._nightTiles:
            try:
                tiles = buildNightTiles(self.store.tables, night, self._options.get("agUrl", "/run/ag"))
            except Exception:
                _log.exception("Making the AG tiles of %s failed; leaving them out", night)
                tiles = ""
            # Only this read's are worth keeping.
            self._nightTiles = {k: v for k, v in self._nightTiles.items() if k[0] == key[0]}
            self._nightTiles[key] = tiles
        return self._nightTiles[key]

    @property
    def summary(self) -> str:
        """The run report's AG block as last made; ``""`` before the first page."""
        report = self.report
        return report.summary if report is not None else ""


def agRoutes(cache: AgRunCache, mount: str = "/run/ag") -> list[tuple[str, type]]:
    """Return the Tornado routes of the AG page: the page, its live status, and each camera's figure.

    The page is served at ``mount``, and asks for ``status`` beside it (``/run/status`` for
    ``/run/ag``), for ``<mount>/focalPlane.png`` and for ``<mount>/camera/AG<n>.png``. A figure is drawn on the request, off the
    event loop, and kept until the page is made again.
    """
    from concurrent.futures import ThreadPoolExecutor

    from tornado.ioloop import IOLoop
    from tornado.web import RequestHandler

    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ag-camera")

    class PageHandler(RequestHandler):
        def get(self) -> None:
            self.set_header("Content-Type", "text/html; charset=utf-8")
            self.set_header("Cache-Control", "no-cache")
            self.write(cache.pageFor("ag"))

    class StatusHandler(RequestHandler):
        def get(self) -> None:
            self.set_header("Cache-Control", "no-store")
            self.write(cache.status())

    class CameraHandler(RequestHandler):
        async def get(self, camera: str | None = None) -> None:
            report = cache.report
            if report is None:
                data = None
            elif camera is None:
                data = await IOLoop.current().run_in_executor(pool, report.focalPlanePng)
            else:
                data = await IOLoop.current().run_in_executor(pool, report.cameraPng, int(camera))
            if data is None:
                self.set_status(404)
                return
            self.set_header("Content-Type", "image/png")
            self.set_header("Cache-Control", "no-cache")
            self.write(data)

    return [
        (re.escape(mount), PageHandler),
        (re.escape(mount.rsplit("/", 1)[0]) + "/status", StatusHandler),
        (re.escape(mount) + r"/camera/AG([1-6])\.png", CameraHandler),
        (re.escape(mount) + r"/focalPlane\.png", CameraHandler),
    ]
