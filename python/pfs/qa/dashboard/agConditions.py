"""AG sky conditions on the night page: transparency and seeing.

The guide cameras measure the sky transparency and the seeing (FWHM) of every AG
exposure. The observatory's tool stores them per exposure in qadb's
``transparency_agc_exposure`` and ``seeing_agc_exposure`` (``pfs.utils.agMetrics``
is the shared calculation; INSTRM-3115 for transparency, INSTRM-3069 for the FWHM
definition). Transparency is uncalibrated: a clear sky reads about 0.8, not 1.
Seeing is an FWHM in arcsec, where lower is better.

`readTransparency` and `readSeeing` read one metric for a visit range from qadb;
`conditions` turns one night of them into the night page's **Conditions** tile — a
compact summary that leads with seeing and carries transparency, with a sparkline —
and the modal that tile opens, which holds a full inline-SVG time series per metric
(a median line with a 25th-to-75th-percentile band over a few-minute bins, on the
same 18:00-to-07:00 window as the verdict clock). The dashboard reads what is stored
and computes nothing of its own.
"""

import html
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

__all__ = ["conditions", "readSeeing", "readTransparency"]

#: The columns a reader returns, one row per AG exposure (``value`` is the metric's per-exposure median).
_COLUMNS = ["visit", "agcExposureId", "time", "value"]

#: The night-clock window, hours from midnight of the evening date: 18:00 to 07:00 next day.
_NIGHT_HOURS = (18, 31)
#: Minutes per time bin for the median and the band.
_BIN_MIN = 5
#: A gap longer than this (minutes) between bins breaks the line and band (between observing blocks).
_GAP_MIN = 16.0


@dataclass(frozen=True)
class _Metric:
    """One AG sky-condition metric: where it lives in qadb and how it is drawn."""

    key: str  #: dict key / identifier
    table: str  #: qadb table
    column: str  #: qadb median column
    positiveOnly: bool  #: drop non-positive values (seeing's bad rows)
    label: str  #: lower-case name for prose ("seeing", "transparency")
    title: str  #: the modal subsection heading ("Seeing", "Sky transparency")
    unit: str  #: appended to a value ("″" for arcsec, "" for transparency)
    yMax: float  #: chart y-axis maximum, values clipped to it
    yTicks: tuple[float, ...]  #: y gridline values
    sparkLo: float  #: sparkline fills this value band vertically ...
    sparkHi: float  #: ... so the night's movement is legible at tile size
    caption: str  #: the modal section's description sentence

    def fmt(self, value: float) -> str:
        """Format a value with the metric's unit, e.g. ``0.56″`` or ``0.77``."""
        return f"{value:.2f}{self.unit}"


SEEING = _Metric(
    key="seeing",
    table="seeing_agc_exposure",
    column="seeing_median",
    positiveOnly=True,
    label="seeing",
    title="Seeing",
    unit="″",
    yMax=2.5,
    yTicks=(0.0, 0.5, 1.0, 1.5, 2.0),
    sparkLo=0.3,
    sparkHi=1.8,
    caption=(
        "The seeing (FWHM) the guide cameras measured on each AG exposure, over the same hours as the night "
        "clock; lower is better. The line is the median over a few minutes; the band is its "
        "25th-to-75th-percentile spread."
    ),
)
TRANSPARENCY = _Metric(
    key="transparency",
    table="transparency_agc_exposure",
    column="transparency_median",
    positiveOnly=False,
    label="transparency",
    title="Sky transparency",
    unit="",
    yMax=1.2,
    yTicks=(0.0, 0.25, 0.5, 0.75, 1.0),
    sparkLo=0.4,
    sparkHi=1.0,
    caption=(
        "The sky transparency the guide cameras measured on each AG exposure, a median over the brighter guide "
        "stars, over the same hours as the night clock. Uncalibrated: a clear sky reads about 0.8, not 1. The "
        "line is the median over a few minutes; the band is its 25th-to-75th-percentile spread."
    ),
)

#: The id of the modal the Conditions tile opens.
_MODAL_ID = "conditions"


def _read(qadb: Any, metric: _Metric, first: int, last: int) -> pd.DataFrame:
    """Read one metric's per-exposure values over a visit range from qadb (read-only)."""
    where = f"pfs_visit_id BETWEEN :first AND :last AND {metric.column} IS NOT NULL"
    if metric.positiveOnly:
        where += f" AND {metric.column} > 0"
    # Keyed by pfs_visit_id (the table's indexed primary key), so the read is a tight index scan.
    sql = f"SELECT pfs_visit_id, agc_exposure_id, taken_at, {metric.column} AS value FROM {metric.table} WHERE {where}"
    read = qadb.query_dataframe(sql, params={"first": int(first), "last": int(last)})
    if read is None or read.empty:
        return pd.DataFrame(columns=_COLUMNS)
    out = read.rename(
        columns={"pfs_visit_id": "visit", "agc_exposure_id": "agcExposureId", "taken_at": "time"}
    )[_COLUMNS]
    out["time"] = pd.to_datetime(out["time"])
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    return out.dropna(subset=["time", "value"]).reset_index(drop=True)


def readTransparency(qadb: Any, first: int, last: int) -> pd.DataFrame:
    """Read the per-exposure AG transparency of a visit range from qadb (`_COLUMNS`; ``value`` is the median)."""
    return _read(qadb, TRANSPARENCY, first, last)


def readSeeing(qadb: Any, first: int, last: int) -> pd.DataFrame:
    """Read the per-exposure AG seeing (FWHM, arcsec) of a visit range from qadb (`_COLUMNS`)."""
    return _read(qadb, SEEING, first, last)


def conditions(transparency: pd.DataFrame, seeing: pd.DataFrame, night: str) -> tuple[str, str]:
    """Return the night's AG ``(tile, modal)`` HTML; a no-data tile and ``""`` when neither metric has data.

    The tile leads with the night's median seeing and carries transparency, with a sparkline, and opens the
    modal; the modal holds a full time series for each metric that has data. A metric with no measured value
    is simply left out, so a night with only one still gets a tile and a one-chart modal. With neither metric,
    the tile shows its no-data state (and there is no modal) rather than being dropped, so the night page
    always carries a Conditions tile in that slot — it never falls back to another metric.

    Parameters
    ----------
    transparency, seeing : `pandas.DataFrame`
        `readTransparency` / `readSeeing` columns for the night's visits (others are ignored); either may be
        None or empty.
    night : `str`
        The night shown (HST evening date, ``YYYY-MM-DD``).
    """
    available = []
    for metric, frame in ((SEEING, seeing), (TRANSPARENCY, transparency)):
        agg = _aggregate(frame, night)
        if agg is not None:
            available.append((metric, agg))
    if not available:
        return _emptyTile(), ""
    return _tile(available), _modal(available)


def _emptyTile() -> str:
    """Return the Conditions tile's no-data state: the label and a muted note, with no modal to open.

    Shown when neither seeing nor transparency has a value for the night, so the tile holds its place
    rather than the page falling back to another metric. A plain ``div`` (not a button): there is nothing
    to open.
    """
    return (
        "<div class='tile cond empty'>"
        "<span class='lab'>Conditions</span>"
        "<span class='big'>&ndash;</span>"
        "<span class='sub'>no AG seeing or transparency for this night</span></div>"
    )


def _aggregate(frame: pd.DataFrame | None, night: str) -> dict | None:
    """Return a metric's binned values and summary numbers for the night, or None when there is nothing."""
    if frame is None or frame.empty or not night:
        return None
    try:
        nightDate = datetime.strptime(night, "%Y-%m-%d").date()
    except ValueError:
        return None
    minutes = _minutes(frame["time"], nightDate)
    values = frame["value"].to_numpy(float)
    bins = _bins(minutes, values)
    if not bins:
        return None
    inside = (minutes >= _NIGHT_HOURS[0] * 60) & (minutes < _NIGHT_HOURS[1] * 60)
    shown = values[inside]
    return {
        "bins": bins,
        "n": int(shown.size),
        "median": float(np.median(shown)),
        "low": float(np.quantile(shown, 0.05)),
        "high": float(np.quantile(shown, 0.95)),
        "outside": int((~inside).sum()),
    }


def _tile(available: list[tuple[_Metric, dict]]) -> str:
    """Return the Conditions tile: a button opening the modal, leading with the first metric and a sparkline."""
    (leadMetric, lead), *rest = available
    secondary = " · ".join(f"{m.fmt(a['median'])} {m.label}" for m, a in rest)
    sub = (f"{secondary} · " if secondary else "") + "open the night"
    return (
        f"<button type='button' class='tile cond' aria-haspopup='dialog' aria-controls='{_MODAL_ID}'>"
        "<span class='lab'>Conditions</span>"
        f"<span class='big'>{lead['median']:.2f}{leadMetric.unit}"
        f"<span class='unit'> {leadMetric.label}</span></span>"
        f"<span class='row spark'>{_sparkline(lead['bins'], leadMetric)}</span>"
        f"<span class='sub'>{html.escape(sub)}</span></button>"
    )


def _modal(available: list[tuple[_Metric, dict]]) -> str:
    """Return the hidden modal overlay with a full time-series section for each available metric."""
    sections = []
    for metric, agg in available:
        summary = (
            f"{agg['n']:,} AG exposures · median {metric.fmt(agg['median'])} · "
            f"5th–95th percentile {metric.fmt(agg['low'])}–{metric.fmt(agg['high'])}"
        )
        outside = f" · {agg['outside']:,} outside 18:00–07:00 not drawn" if agg["outside"] else ""
        sections.append(
            f"<section class='cond-metric'><h3>{html.escape(metric.title)}</h3>"
            f"<p>{metric.caption}</p>"
            f"<p class='meta'>{html.escape(summary + outside)}</p>"
            f"<div class='tchart'>{_svg(agg['bins'], metric)}</div></section>"
        )
    return (
        f"<div class='modal' id='{_MODAL_ID}' hidden role='dialog' aria-modal='true' "
        f"aria-labelledby='{_MODAL_ID}-h'><div class='sheet'>"
        "<button type='button' class='x' aria-label='Close'>&times;</button>"
        f"<h2 id='{_MODAL_ID}-h'>AG sky conditions tonight</h2>" + "".join(sections) + "</div></div>"
    )


def _minutes(times: pd.Series, night: date) -> np.ndarray:
    """Return each time's minutes from midnight of the evening date (so 07:00 next day is 31*60)."""
    ts = pd.to_datetime(times)
    days = (ts.dt.normalize() - pd.Timestamp(night)).dt.days.to_numpy()
    return (
        days * 24 * 60 + ts.dt.hour.to_numpy() * 60 + ts.dt.minute.to_numpy() + ts.dt.second.to_numpy() / 60
    )


def _bins(minutes: np.ndarray, values: np.ndarray) -> list[dict]:
    """Return one entry per non-empty time bin: its centre minute and the median, q25, q75 within it."""
    lo, hi = _NIGHT_HOURS[0] * 60, _NIGHT_HOURS[1] * 60
    inside = (minutes >= lo) & (minutes < hi)
    minutes, values = minutes[inside], values[inside]
    if minutes.size == 0:
        return []
    index = ((minutes - lo) // _BIN_MIN).astype(int)
    out = []
    for b in np.unique(index):
        chunk = values[index == b]
        out.append(
            {
                "minute": lo + (b + 0.5) * _BIN_MIN,
                "median": float(np.median(chunk)),
                "q25": float(np.quantile(chunk, 0.25)),
                "q75": float(np.quantile(chunk, 0.75)),
            }
        )
    return out


def _runs(bins: list[dict]) -> list[list[dict]]:
    """Split bins (time-ordered) into runs, breaking where more than `_GAP_MIN` separates two bins."""
    runs: list[list[dict]] = []
    for b in bins:
        if runs and b["minute"] - runs[-1][-1]["minute"] <= _GAP_MIN:
            runs[-1].append(b)
        else:
            runs.append([b])
    return runs


# Inline-SVG geometry (a viewBox scaled to the content width; see the night page's CSS).
_W, _H = 1000, 230
_PAD = {"l": 42, "r": 12, "t": 10, "b": 24}


def _x(minute: float) -> float:
    """Map a minute-from-evening-midnight to the plot's x, on the 18:00-to-07:00 window."""
    lo, hi = _NIGHT_HOURS[0] * 60, _NIGHT_HOURS[1] * 60
    plot = _W - _PAD["l"] - _PAD["r"]
    return round(_PAD["l"] + (minute - lo) / (hi - lo) * plot, 2)


def _y(value: float, metric: _Metric) -> float:
    """Map a value to the plot's y (0 at the bottom, ``metric.yMax`` at the top), clipped to the axis."""
    plot = _H - _PAD["t"] - _PAD["b"]
    clipped = min(max(value, 0.0), metric.yMax)
    return round(_H - _PAD["b"] - clipped / metric.yMax * plot, 2)


def _svg(bins: list[dict], metric: _Metric) -> str:
    """Return the metric's time series as an inline SVG: y grid, hour ticks, the band, the median line."""
    parts = [
        f'<svg class="tplot" viewBox="0 0 {_W} {_H}" width="{_W}" height="{_H}" '
        f'role="img" aria-label="AG {metric.label} through the night">'
    ]
    for v in metric.yTicks:
        y = _y(v, metric)
        parts.append(f'<line class="grid" x1="{_PAD["l"]}" x2="{_W - _PAD["r"]}" y1="{y}" y2="{y}"/>')
        parts.append(f'<text class="ylab" x="{_PAD["l"] - 6}" y="{y + 3}" text-anchor="end">{v:g}</text>')
    for hour in range(_NIGHT_HOURS[0], _NIGHT_HOURS[1] + 1, 2):
        x = _x(hour * 60)
        parts.append(f'<line class="grid vx" x1="{x}" x2="{x}" y1="{_PAD["t"]}" y2="{_H - _PAD["b"]}"/>')
        parts.append(
            f'<text class="xlab" x="{x}" y="{_H - _PAD["b"] + 15}" text-anchor="middle">{hour % 24:02d}:00</text>'
        )
    for run in _runs(bins):
        top = " ".join(f"{_x(b['minute'])},{_y(b['q75'], metric)}" for b in run)
        bottom = " ".join(f"{_x(b['minute'])},{_y(b['q25'], metric)}" for b in reversed(run))
        parts.append(f'<polygon class="band" points="{top} {bottom}"/>')
        line = " ".join(f"{_x(b['minute'])},{_y(b['median'], metric)}" for b in run)
        parts.append(f'<polyline class="median" points="{line}"/>')
    parts.append("</svg>")
    return "".join(parts)


# Sparkline geometry (the tile's mini median line, no axes).
_SW, _SH = 150, 32


def _sparkline(bins: list[dict], metric: _Metric) -> str:
    """Return a tiny axis-free median line for the tile, one polyline per observing run."""
    lo, hi = _NIGHT_HOURS[0] * 60, _NIGHT_HOURS[1] * 60

    def sx(minute: float) -> float:
        return round((minute - lo) / (hi - lo) * _SW, 2)

    def sy(value: float) -> float:
        clipped = min(max(value, metric.sparkLo), metric.sparkHi)
        return round(_SH - 2 - (clipped - metric.sparkLo) / (metric.sparkHi - metric.sparkLo) * (_SH - 4), 2)

    parts = [
        f'<svg class="spark" viewBox="0 0 {_SW} {_SH}" width="{_SW}" height="{_SH}" '
        'preserveAspectRatio="none" aria-hidden="true">'
    ]
    for run in _runs(bins):
        points = " ".join(f"{sx(b['minute'])},{sy(b['median'])}" for b in run)
        parts.append(f'<polyline points="{points}"/>')
    parts.append("</svg>")
    return "".join(parts)
