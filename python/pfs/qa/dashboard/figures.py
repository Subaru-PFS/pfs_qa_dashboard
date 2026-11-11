"""The dashboard's charts: tables in, Bokeh figures out.

Status is never told by colour alone: each has its own marker shape in the charts and its
mark in the grid's cells (`STATUS_MARKS`), and every chart has a hover saying it in words.
"""

from collections.abc import Callable

import numpy as np
import pandas as pd
from bokeh.models import (
    BoxAnnotation,
    ColumnDataSource,
    FixedTicker,
    HoverTool,
    Range1d,
    Span,
    TapTool,
)
from bokeh.plotting import figure

from pfs.drp.qa.metrics.registry import specFor
from pfs.qa.dashboard.comparison import CELL_KEYS, cameraOrder, metricLabel

__all__ = [
    "STATUS_COLORS",
    "STATUS_MARKERS",
    "STATUS_MARKS",
    "cellDetail",
    "comparisonGrid",
    "differenceText",
    "relativeText",
    "visitStrip",
]

#: Status colours (Okabe-Ito-like, distinguishable with the common colour-vision deficiencies).
STATUS_COLORS = {"PASS": "#3a9a5b", "WARN": "#e6a100", "FAIL": "#d1495b", "UNKNOWN": "#9aa0a6", "": "#c7ccd1"}

#: Status marks, so no status is told by colour alone.
STATUS_MARKS = {"PASS": "✓", "WARN": "!", "FAIL": "✕", "UNKNOWN": "?"}

#: Status marker shapes in the charts, for the same reason.
STATUS_MARKERS = {"PASS": "circle", "WARN": "triangle", "FAIL": "diamond", "UNKNOWN": "square"}

# Drawn in this order, so the visits to look at lie on top.
_DRAW_ORDER = ("UNKNOWN", "PASS", "WARN", "FAIL")
_SIZES = {"PASS": 9, "WARN": 13, "FAIL": 15, "UNKNOWN": 8}
_INK = "#1f2933"
_MUTED = "#7b8794"


def _clock(hours: float) -> str:
    """Return hours after HST noon as a clock time, ``01:00`` for 13."""
    return f"{int(hours + 12) % 24:02d}:00"


def relativeText(relative: float) -> str:
    """Return a relative difference as text, ``+7.2%``; ``n/a`` for none."""
    return "n/a" if pd.isna(relative) else f"{relative:+.1%}"


def differenceText(metric: str, relative: float, difference: float) -> str:
    """Return a median's difference from its reference's as a cell says it.

    Relative (``+7.2%``) for a metric with a tolerance, which is what the tolerance is of.
    Absolute, in the metric's units (``+0.012``), for one without: its reference median may
    be near zero, where a relative difference says nothing.
    """
    if specFor(metric).tolerance is not None:
        return relativeText(relative)
    return "n/a" if pd.isna(difference) else f"{difference:+.2g}"


def _quiet(plot: figure) -> None:
    """Make the axes and grid recede, and leave only the tools the chart needs."""
    plot.toolbar.logo = None
    plot.outline_line_color = None
    plot.grid.grid_line_alpha = 0.25
    plot.axis.axis_line_color = _MUTED
    plot.axis.major_tick_line_color = _MUTED
    plot.axis.minor_tick_line_color = None


def visitStrip(rollup: pd.DataFrame, timeline: pd.DataFrame, selected: str = "") -> figure:
    """Return the visits by night and time, each marked with its status.

    Parameters
    ----------
    rollup : `pandas.DataFrame`
        `store.MetricsStore.rollup`: a row per visit.
    timeline : `pandas.DataFrame`
        `store.visitTimeline` of the same visits.
    selected : `str`
        The night the comparison is of, whose label is marked; none by default.

    Returns
    -------
    `bokeh.plotting.figure`
        A row per night (the HST date of its evening), latest at the top, against the HST
        clock time; without times, one row against the visits in order.
    """
    visits = rollup.merge(timeline, on="visit", how="left")
    timed = bool(len(visits)) and visits["time"].notna().all()
    if timed:
        noon = pd.to_datetime(visits["night"]) + pd.Timedelta(hours=12)
        visits["x"] = (visits["time"] - noon) / pd.Timedelta(hours=1)
        visits["row"] = [f"{night} ▸" if night == selected else night for night in visits["night"]]
        visits["when"] = visits["time"].dt.strftime("%Y-%m-%d %H:%M HST")
    else:
        visits["x"] = visits["position"].astype(float)
        visits["row"] = "visits"
        visits["when"] = ""
    visits["decided"] = [
        " ".join(str(part) for part in (camera, task) if part and not pd.isna(part))
        for camera, task in zip(visits["decidedByCamera"], visits["decidedByTask"], strict=True)
    ]
    visits["reason"] = visits["qaReason"].fillna("")
    rows = sorted(visits["row"].unique())
    plot = figure(
        y_range=rows,
        height=170 + 34 * max(len(rows), 1),
        sizing_mode="stretch_width",
        tools="xpan,xwheel_zoom,reset",
        active_scroll="xwheel_zoom",
        toolbar_location="right",
        title="Visits by night: worst status of each visit's images",
    )
    renderers = []
    for status in _DRAW_ORDER:
        some = visits[visits["qaStatus"] == status]
        if some.empty:
            continue
        renderers.append(
            plot.scatter(
                "x",
                "row",
                source=ColumnDataSource(some),
                marker=STATUS_MARKERS[status],
                size=_SIZES[status],
                fill_color=STATUS_COLORS[status],
                line_color="white",
                line_width=1,
                legend_label=f"{STATUS_MARKS[status]} {status} ({len(some)})",
            )
        )
    plot.add_tools(
        HoverTool(
            renderers=renderers,
            tooltips=[
                ("visit", "@visit"),
                ("start", "@when"),
                ("status", "@qaStatus"),
                ("decided by", "@decided"),
                ("reason", "@reason"),
                ("images", "@nImages: @nFail FAIL, @nWarn WARN, @nPass PASS, @nUnknown UNKNOWN"),
            ],
        )
    )
    if timed:
        low, high = np.floor(visits["x"].min()), np.ceil(visits["x"].max())
        plot.x_range = Range1d(low - 0.25, high + 0.25)
        ticks = [int(tick) for tick in np.arange(low, high + 1)]
        plot.xaxis.ticker = FixedTicker(ticks=ticks)
        plot.xaxis.major_label_overrides = {tick: _clock(tick) for tick in ticks}
        plot.xaxis.axis_label = "start of the visit's first exposure (HST)"
    else:
        plot.xaxis.axis_label = "visits in order (no times)"
    if len(plot.legend):
        plot.legend.orientation = "horizontal"
        plot.legend.location = "top_right"
        plot.legend.background_fill_alpha = 0.0
        plot.legend.border_line_color = None
        plot.add_layout(plot.legend[0], "above")
    _quiet(plot)
    plot.ygrid.grid_line_color = None
    return plot


def comparisonGrid(
    cells: pd.DataFrame, onSelect: Callable[[tuple], None] | None = None, selected: tuple | None = None
) -> figure:
    """Return one reference's comparison: a cell per metric and frame type (rows) and camera (columns).

    Parameters
    ----------
    cells : `pandas.DataFrame`
        `comparison.gridCells` of one reference.
    onSelect : callable, optional
        Called with a cell's `comparison.CELL_KEYS` when it is tapped.
    selected : `tuple`, optional
        The cell shown in detail, which is outlined.

    Returns
    -------
    `bokeh.plotting.figure`
        Each cell is filled by the status of the night's median against the reference's
        (the worst of its populations) and says the difference (`differenceText`); below it, how
        many of the night's visits are themselves beyond the ``FAIL`` tolerance.
    """
    cells = cells.reset_index(drop=True).copy()
    cameras = cameraOrder(cells)
    rows = list(dict.fromkeys(cells["row"]))[::-1]
    cells["color"] = cells["status"].map(STATUS_COLORS)
    cells["text"] = [
        f"{STATUS_MARKS[status]} {differenceText(metric, relative, difference)}"
        for status, metric, relative, difference in zip(
            cells["status"], cells["metric"], cells["relative"], cells["difference"], strict=True
        )
    ]
    cells["differenceText"] = [
        "n/a" if pd.isna(difference) else f"{difference:+.3g} {specFor(metric).units or ''}".strip()
        for metric, difference in zip(cells["metric"], cells["difference"], strict=True)
    ]
    cells["visitsText"] = [
        f"{STATUS_MARKS['FAIL']} {beyond}/{n}" if beyond else ""
        for beyond, n in zip(cells["nBeyond"], cells["nVisits"], strict=True)
    ]
    cells["relativeText"] = [relativeText(relative) for relative in cells["relative"]]
    cells["edge"] = [
        _INK if selected is not None and tuple(key) == tuple(selected) else "white"
        for key in cells[list(CELL_KEYS)].itertuples(index=False)
    ]
    cells["edgeWidth"] = [3 if edge == _INK else 2 for edge in cells["edge"]]
    source = ColumnDataSource(cells)
    plot = figure(
        x_range=cameras,
        y_range=rows,
        height=70 + 52 * len(rows),
        sizing_mode="stretch_width",
        x_axis_location="above",
        tools="",
        toolbar_location=None,
    )
    rect = plot.rect(
        "camera",
        "row",
        width=1,
        height=1,
        source=source,
        fill_color="color",
        fill_alpha=0.85,
        line_color="edge",
        line_width="edgeWidth",
        nonselection_fill_alpha=0.85,
        nonselection_line_color="edge",
        selection_line_color=_INK,
        selection_line_width=3,
    )
    text = {"source": source, "text_align": "center", "text_color": _INK, "text_font_size": "10pt"}
    plot.text("camera", "row", text="text", y_offset=-7, text_baseline="middle", **text)
    plot.text(
        "camera",
        "row",
        text="visitsText",
        y_offset=10,
        text_baseline="middle",
        text_font_style="bold",
        **text,
    )
    plot.add_tools(
        HoverTool(
            renderers=[rect],
            tooltips=[
                ("", "@row, @camera"),
                ("night's median vs reference", "@status, @differenceText (@relativeText)"),
                ("worst population", "@population (of @nPopulations)"),
                ("visits beyond FAIL tolerance", "@nBeyond of @nVisits"),
                ("visits beyond WARN only", "@nWarned of @nVisits"),
            ],
        ),
        TapTool(renderers=[rect]),
    )
    if onSelect is not None:
        keys = [tuple(key) for key in cells[list(CELL_KEYS)].itertuples(index=False)]

        def tapped(attr, old, new):
            if new:
                onSelect(keys[new[0]])

        source.selected.on_change("indices", tapped)
    _quiet(plot)
    plot.grid.grid_line_color = None
    plot.axis.axis_line_color = None
    plot.axis.major_tick_line_color = None
    plot.xaxis.major_label_text_font_size = "10pt"
    plot.yaxis.major_label_text_font_size = "10pt"
    return plot


def cellDetail(deviations: pd.DataFrame, row: pd.Series) -> figure:
    """Return one population's values, visit by visit, against the reference median and its tolerances.

    Parameters
    ----------
    deviations : `pandas.DataFrame`
        `comparison.visitDeviations` rows of one reference, camera, metric and population,
        with ``time`` (NaT without).
    row : `pandas.Series`
        That population's row of the comparison.

    Returns
    -------
    `bokeh.plotting.figure`
        The values against the HST time of the visit (against the visit number without
        times), marked by each value's own status; the reference median as a line, the
        night's as a dashed one, and the ``WARN`` and ``FAIL`` tolerances as bands on the
        metric's bad side.
    """
    metric = row["metric"]
    spec = specFor(metric)
    points = deviations.sort_values("visit").copy()
    timed = bool(len(points)) and points["time"].notna().all()
    points["x"] = points["time"] if timed else points["visit"].astype(float)
    points["when"] = points["time"].dt.strftime("%Y-%m-%d %H:%M HST").fillna("") if timed else ""
    points["relativeText"] = [relativeText(relative) for relative in points["relative"]]
    plot = figure(
        height=340,
        sizing_mode="stretch_width",
        x_axis_type="datetime" if timed else "linear",
        tools="xpan,xwheel_zoom,box_zoom,reset",
        toolbar_location="right",
    )
    reference = row["referenceMedian"]
    if pd.notna(reference):
        plot.add_layout(Span(location=float(reference), dimension="width", line_color=_INK, line_width=2))
        plot.line([], [], line_color=_INK, line_width=2, legend_label=f"reference median {reference:.4g}")
        if pd.notna(row["warn"]) and reference > 0:
            sign = 1.0 if spec.higherIsWorse else -1.0
            warn, fail = (float(reference) * (1 + sign * row[name]) for name in ("warn", "fail"))
            edges = {"bottom": min(warn, fail), "top": max(warn, fail)}
            plot.add_layout(BoxAnnotation(fill_color=STATUS_COLORS["WARN"], fill_alpha=0.12, **edges))
            beyond = {"bottom": fail} if spec.higherIsWorse else {"top": fail}
            plot.add_layout(BoxAnnotation(fill_color=STATUS_COLORS["FAIL"], fill_alpha=0.12, **beyond))
            for name, level in (("WARN", warn), ("FAIL", fail)):
                plot.add_layout(
                    Span(location=level, dimension="width", line_color=STATUS_COLORS[name], line_width=1)
                )
                plot.line(
                    [],
                    [],
                    line_color=STATUS_COLORS[name],
                    legend_label=f"{name} beyond {row[name.lower()]:.0%} ({level:.4g})",
                )
    if pd.notna(row["runMedian"]):
        plot.add_layout(
            Span(location=float(row["runMedian"]), dimension="width", line_color=_INK, line_dash="dashed")
        )
        plot.line(
            [],
            [],
            line_color=_INK,
            line_dash="dashed",
            legend_label=f"these visits' median {row['runMedian']:.4g}",
        )
    renderers = []
    for status in _DRAW_ORDER:
        some = points[points["status"] == status]
        if some.empty:
            continue
        renderers.append(
            plot.scatter(
                "x",
                "value",
                source=ColumnDataSource(some),
                marker=STATUS_MARKERS[status],
                size=_SIZES[status],
                fill_color=STATUS_COLORS[status],
                line_color="white",
                line_width=1,
                legend_label=f"{STATUS_MARKS[status]} {status} visits ({len(some)})",
            )
        )
    plot.add_tools(
        HoverTool(
            renderers=renderers,
            tooltips=[
                ("visit", "@visit"),
                ("start", "@when"),
                (metric, "@value{0.0000}"),
                ("vs reference median", "@relativeText, @status"),
                ("image's own verdict", "@verdict"),
            ],
        )
    )
    plot.xaxis.axis_label = "start of the visit's first exposure (HST)" if timed else "visit"
    plot.yaxis.axis_label = metricLabel(metric) + (", absolute value" if spec.absolute else "")
    plot.legend.location = "top_left"
    plot.legend.background_fill_alpha = 0.6
    plot.legend.border_line_color = None
    plot.legend.label_text_font_size = "9pt"
    plot.add_layout(plot.legend[0], "right")
    _quiet(plot)
    return plot
