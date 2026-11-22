"""AG sky transparency on the night page.

The guide cameras measure the sky transparency of every AG exposure: the flux of
the matched guide stars over what their catalogue magnitudes predict, a median
over the brighter stars (so a sparse catalogue does not read as low signal). The
observatory's tool stores it per exposure in qadb's ``transparency_agc_exposure``
(``pfs.utils.agMetrics`` is the shared calculation; INSTRM-3115). It is
uncalibrated: a clear sky reads about 0.8, not 1.

`readTransparency` reads it for a visit range from qadb; `conditions` turns one
night of it into the night page's **Conditions** tile — a median and a sparkline —
and the modal that tile opens, which holds the full inline-SVG time series (a median
line with a 25th-to-75th-percentile band over a few-minute bins, on the same
18:00-to-07:00 window as the verdict clock). The dashboard reads what is stored and
computes no transparency of its own.
"""

import html
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

__all__ = ["TRANSPARENCY_COLUMNS", "conditions", "readTransparency"]

#: The columns `readTransparency` returns, one row per AG exposure.
TRANSPARENCY_COLUMNS = ["visit", "agcExposureId", "time", "transparency"]

# One row per AG exposure whose transparency the tool measured, over a visit range. Keyed by
# pfs_visit_id (the table's indexed primary key), so the read is a tight index scan, not a time sweep.
_SQL = """
SELECT pfs_visit_id, agc_exposure_id, taken_at, transparency_median
FROM transparency_agc_exposure
WHERE pfs_visit_id BETWEEN :first AND :last AND transparency_median IS NOT NULL
"""

#: The night-clock window, hours from midnight of the evening date: 18:00 to 07:00 next day (as in nightReport.html).
_NIGHT_HOURS = (18, 31)
#: Transparency axis, clipped to this; a clear sky is ~0.8 (uncalibrated), noise reaches past 1.
_Y_MAX = 1.2
#: Minutes per time bin for the median and the band.
_BIN_MIN = 5
#: A gap longer than this (minutes) between bins breaks the line and band (between observing blocks).
_GAP_MIN = 16.0


def readTransparency(qadb: Any, first: int, last: int) -> pd.DataFrame:
    """Read the per-exposure AG transparency of a visit range from qadb (read-only).

    Parameters
    ----------
    qadb : `pfs.utils.database.qadb.QaDB`
        The quality-assurance database.
    first, last : `int`
        The ``pfs_visit_id`` range, inclusive.

    Returns
    -------
    `pandas.DataFrame`
        `TRANSPARENCY_COLUMNS`: ``visit``, ``agcExposureId``, ``time`` (HST, as
        ``transparency_agc_exposure.taken_at``) and ``transparency`` (the per-
        exposure median). Empty with the same columns when there is none.
    """
    read = qadb.query_dataframe(_SQL, params={"first": int(first), "last": int(last)})
    if read is None or read.empty:
        return pd.DataFrame(columns=TRANSPARENCY_COLUMNS)
    out = read.rename(
        columns={
            "pfs_visit_id": "visit",
            "agc_exposure_id": "agcExposureId",
            "taken_at": "time",
            "transparency_median": "transparency",
        }
    )[TRANSPARENCY_COLUMNS]
    out["time"] = pd.to_datetime(out["time"])
    out["transparency"] = pd.to_numeric(out["transparency"], errors="coerce")
    return out.dropna(subset=["time", "transparency"]).reset_index(drop=True)


def _minutes(times: pd.Series, night: date) -> np.ndarray:
    """Return each time's minutes from midnight of the evening date (so 07:00 next day is 31*60)."""
    ts = pd.to_datetime(times)
    days = (ts.dt.normalize() - pd.Timestamp(night)).dt.days.to_numpy()
    return (
        days * 24 * 60 + ts.dt.hour.to_numpy() * 60 + ts.dt.minute.to_numpy() + ts.dt.second.to_numpy() / 60
    )


def _bins(minutes: np.ndarray, transparency: np.ndarray) -> list[dict]:
    """Return one entry per non-empty time bin: its centre minute and the median, q25, q75 within it."""
    lo, hi = _NIGHT_HOURS[0] * 60, _NIGHT_HOURS[1] * 60
    inside = (minutes >= lo) & (minutes < hi)
    minutes, transparency = minutes[inside], transparency[inside]
    if minutes.size == 0:
        return []
    index = ((minutes - lo) // _BIN_MIN).astype(int)
    out = []
    for b in np.unique(index):
        values = transparency[index == b]
        out.append(
            {
                "minute": lo + (b + 0.5) * _BIN_MIN,
                "median": float(np.median(values)),
                "q25": float(np.quantile(values, 0.25)),
                "q75": float(np.quantile(values, 0.75)),
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


def conditions(transparency: pd.DataFrame, night: str) -> tuple[str, str]:
    """Return the night's AG-transparency ``(tile, modal)`` HTML, or ``("", "")`` when there is none.

    The tile is a compact summary for the night page's tile row — a median and a sparkline — and a button
    that opens the modal; the modal holds the full time series (`_svg`). Both are empty when the night has
    no measured transparency, so the page keeps its other tile and adds no modal.

    Parameters
    ----------
    transparency : `pandas.DataFrame`
        `readTransparency`'s columns, for the night's visits (others are ignored).
    night : `str`
        The night shown (HST evening date, ``YYYY-MM-DD``).
    """
    agg = _aggregate(transparency, night)
    if agg is None:
        return "", ""
    return _tile(agg), _modal(agg)


def _aggregate(transparency: pd.DataFrame, night: str) -> dict | None:
    """Return the night's binned transparency and summary numbers, or None when there is nothing to show."""
    if transparency is None or transparency.empty or not night:
        return None
    try:
        nightDate = datetime.strptime(night, "%Y-%m-%d").date()
    except ValueError:
        return None
    minutes = _minutes(transparency["time"], nightDate)
    values = transparency["transparency"].to_numpy(float)
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


#: The id of the modal the Conditions tile opens.
_MODAL_ID = "conditions"


def _tile(agg: dict) -> str:
    """Return the Conditions tile: a button that opens the modal, showing the median and a sparkline."""
    return (
        f"<button type='button' class='tile cond' aria-haspopup='dialog' aria-controls='{_MODAL_ID}'>"
        "<span class='lab'>Conditions</span>"
        f"<span class='big'>{agg['median']:.2f}<span class='unit'> transp.</span></span>"
        f"<span class='row spark'>{_sparkline(agg['bins'])}</span>"
        "<span class='sub'>AG sky transparency · open the night</span></button>"
    )


def _modal(agg: dict) -> str:
    """Return the hidden modal overlay with the night's full transparency chart."""
    summary = (
        f"{agg['n']:,} AG exposures · median transparency {agg['median']:.2f} · "
        f"5th–95th percentile {agg['low']:.2f}–{agg['high']:.2f}"
    )
    outside = f" · {agg['outside']:,} outside 18:00–07:00 not drawn" if agg["outside"] else ""
    return (
        f"<div class='modal' id='{_MODAL_ID}' hidden role='dialog' aria-modal='true' "
        f"aria-labelledby='{_MODAL_ID}-h'><div class='sheet'>"
        "<button type='button' class='x' aria-label='Close'>&times;</button>"
        f"<h2 id='{_MODAL_ID}-h'>AG sky transparency tonight</h2>"
        "<p>The sky transparency the guide cameras measured on each AG exposure — a median over the brighter "
        "guide stars — over the same hours as the night clock. Uncalibrated: a clear sky reads about 0.8, not "
        "1. The line is the median over a few minutes; the band is its 25th-to-75th-percentile spread.</p>"
        f"<p class='meta'>{html.escape(summary + outside)}</p>"
        f"<div class='tchart'>{_svg(agg['bins'])}</div></div></div>"
    )


# Inline-SVG geometry (a viewBox scaled to the content width; see the night page's CSS).
_W, _H = 1000, 230
_PAD = {"l": 42, "r": 12, "t": 10, "b": 24}


def _x(minute: float) -> float:
    """Map a minute-from-evening-midnight to the plot's x, on the 18:00-to-07:00 window."""
    lo, hi = _NIGHT_HOURS[0] * 60, _NIGHT_HOURS[1] * 60
    plot = _W - _PAD["l"] - _PAD["r"]
    return round(_PAD["l"] + (minute - lo) / (hi - lo) * plot, 2)


def _y(value: float) -> float:
    """Map a transparency to the plot's y (0 at the bottom, `_Y_MAX` at the top), clipped to the axis."""
    plot = _H - _PAD["t"] - _PAD["b"]
    clipped = min(max(value, 0.0), _Y_MAX)
    return round(_H - _PAD["b"] - clipped / _Y_MAX * plot, 2)


def _svg(bins: list[dict]) -> str:
    """Return the transparency time series as an inline SVG: y grid, hour ticks, the band, the median line."""
    parts = [
        f'<svg class="tplot" viewBox="0 0 {_W} {_H}" width="{_W}" height="{_H}" '
        'role="img" aria-label="AG sky transparency through the night">'
    ]
    # y gridlines and labels.
    for v in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = _y(v)
        parts.append(f'<line class="grid" x1="{_PAD["l"]}" x2="{_W - _PAD["r"]}" y1="{y}" y2="{y}"/>')
        parts.append(f'<text class="ylab" x="{_PAD["l"] - 6}" y="{y + 3}" text-anchor="end">{v:g}</text>')
    # x hour ticks (HST), on the clock's window.
    for hour in range(_NIGHT_HOURS[0], _NIGHT_HOURS[1] + 1, 2):
        x = _x(hour * 60)
        parts.append(f'<line class="grid vx" x1="{x}" x2="{x}" y1="{_PAD["t"]}" y2="{_H - _PAD["b"]}"/>')
        parts.append(
            f'<text class="xlab" x="{x}" y="{_H - _PAD["b"] + 15}" text-anchor="middle">'
            f"{hour % 24:02d}:00</text>"
        )
    # The band (q25-q75) and the median line, one run each so observing gaps are not bridged.
    for run in _runs(bins):
        top = " ".join(f"{_x(b['minute'])},{_y(b['q75'])}" for b in run)
        bottom = " ".join(f"{_x(b['minute'])},{_y(b['q25'])}" for b in reversed(run))
        parts.append(f'<polygon class="band" points="{top} {bottom}"/>')
        line = " ".join(f"{_x(b['minute'])},{_y(b['median'])}" for b in run)
        parts.append(f'<polyline class="median" points="{line}"/>')
    parts.append("</svg>")
    return "".join(parts)


# Sparkline geometry (the tile's mini median line, no axes). It fills a narrower transparency band than
# the full chart so the night's movement is legible at tile size.
_SW, _SH = 150, 32
_SPARK_LO, _SPARK_HI = 0.4, 1.0


def _sparkline(bins: list[dict]) -> str:
    """Return a tiny axis-free median line for the tile, one polyline per observing run."""
    lo, hi = _NIGHT_HOURS[0] * 60, _NIGHT_HOURS[1] * 60

    def sx(minute: float) -> float:
        return round((minute - lo) / (hi - lo) * _SW, 2)

    def sy(value: float) -> float:
        clipped = min(max(value, _SPARK_LO), _SPARK_HI)
        return round(_SH - 2 - (clipped - _SPARK_LO) / (_SPARK_HI - _SPARK_LO) * (_SH - 4), 2)

    parts = [
        f'<svg class="spark" viewBox="0 0 {_SW} {_SH}" width="{_SW}" height="{_SH}" '
        'preserveAspectRatio="none" aria-hidden="true">'
    ]
    for run in _runs(bins):
        points = " ".join(f"{sx(b['minute'])},{sy(b['median'])}" for b in run)
        parts.append(f'<polyline points="{points}"/>')
    parts.append("</svg>")
    return "".join(parts)
