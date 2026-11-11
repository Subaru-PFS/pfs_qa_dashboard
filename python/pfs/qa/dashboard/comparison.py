"""What the comparison view shows, as tables: no Panel, no Bokeh.

The medians of a night's visits against each reference's come from drp_qa
(`pfs.drp.qa.metrics.runComparison.compareRuns`, through `store.MetricsStore.comparison`).
A median hides one bad visit among good ones, so this module also sets each visit's own
value against the reference median (`visitDeviations`), by the same tolerances
(`pfs.drp.qa.metrics.runComparison.toleranceStatus`), and folds both into the grid's cells
(`gridCells`): one per reference, metric, frame type and camera.
"""

from collections.abc import Sequence

import numpy as np
import pandas as pd

from pfs.drp.qa.metrics.registry import specFor
from pfs.drp.qa.metrics.runComparison import COMPARED_METRICS, POPULATION_COLUMNS, toleranceStatus

__all__ = [
    "ALL_VISITS",
    "CELL_COLUMNS",
    "CELL_KEYS",
    "SMALL_POPULATION",
    "cameraOrder",
    "gridCells",
    "judgedCells",
    "metricLabel",
    "nightOptions",
    "nightVisits",
    "populationLabel",
    "populationRows",
    "rowOrder",
    "visitDeviations",
    "worstFirst",
]

#: The night selector's option for every visit, whatever its night.
ALL_VISITS = "All visits"

#: What names a cell of the grid.
CELL_KEYS = ("referenceName", "metric", "obsType", "camera")

#: A cell whose medians rest on this many images or fewer, on either side, is drawn as weak evidence:
#: one odd exposure of three moves a median as far as it likes.
SMALL_POPULATION = 3

#: Columns of `gridCells`, in order.
CELL_COLUMNS = (
    *CELL_KEYS,
    "arm",
    "spectrograph",
    "row",
    "status",
    "relative",
    "difference",
    "population",
    "nPopulations",
    "nRun",
    "nReference",
    "nVisits",
    "nBeyond",
    "nWarned",
)

_POPULATION_KEYS = ("metric", "arm", "spectrograph", *POPULATION_COLUMNS)
_RANK = {"UNKNOWN": 0, "PASS": 1, "WARN": 2, "FAIL": 3}
_ARMS = {"b": 0, "r": 1, "m": 2, "n": 3}


def metricLabel(metric: str) -> str:
    """Return a metric's name with its units, ``medFwhm (pix)``."""
    units = specFor(metric).units
    return f"{metric} ({units})" if units else metric


def populationLabel(row) -> str:
    """Return a population as text: those of `POPULATION_COLUMNS` that say something, less the frame type."""
    parts = (row[column] for column in POPULATION_COLUMNS if column != "obsType")
    return " ".join(str(part) for part in parts if part) or "all"


def cameraOrder(cameras: pd.DataFrame) -> list[str]:
    """Return the ``camera`` names of rows with ``arm`` and ``spectrograph``: by spectrograph, then b, r, m, n."""
    unique = cameras.drop_duplicates("camera")
    keys = sorted(
        (int(row.spectrograph), _ARMS.get(row.arm, len(_ARMS)), row.camera)
        for row in unique.itertuples(index=False)
    )
    return [camera for _, _, camera in keys]


def nightOptions(timeline: pd.DataFrame) -> list[str]:
    """Return the nights of `store.visitTimeline`, latest first, then `ALL_VISITS`; only that without times."""
    nights = sorted({night for night in timeline["night"] if night}, reverse=True)
    return [*nights, ALL_VISITS]


def nightVisits(timeline: pd.DataFrame, night: str) -> list[int]:
    """Return the visits of one night of `store.visitTimeline` (HST evening date), or all for `ALL_VISITS`."""
    chosen = timeline if night == ALL_VISITS else timeline[timeline["night"] == night]
    return [int(visit) for visit in chosen["visit"]]


def visitDeviations(values: pd.DataFrame, comparison: pd.DataFrame) -> pd.DataFrame:
    """Set each image's value against the reference median of its camera and population.

    Parameters
    ----------
    values : `pandas.DataFrame`
        `store.MetricsStore.metricValues`: one row per image, population and metric.
    comparison : `pandas.DataFrame`
        `store.MetricsStore.comparison` of the same visits.

    Returns
    -------
    `pandas.DataFrame`
        ``values`` once per reference, with ``referenceName``, ``referenceMedian``,
        ``relative`` (the value less that median, over it; missing when the median is not
        positive), ``warn``, ``fail`` and ``status``: the value judged alone, as the
        comparison judges the median.
    """
    keys = list(_POPULATION_KEYS)
    medians = comparison[["referenceName", *keys, "referenceMedian"]]
    rows = values.merge(medians, on=keys, how="inner")
    positive = rows["referenceMedian"] > 0
    rows["relative"] = ((rows["value"] - rows["referenceMedian"]) / rows["referenceMedian"]).where(positive)
    judged = toleranceStatus(rows["metric"], rows["relative"])
    for column in judged:
        rows[column] = judged[column].to_numpy()
    return rows.astype({"relative": "float64"})


def _excess(metric: pd.Series, relative: pd.Series, difference: pd.Series) -> pd.Series:
    """Return how far each median is from its reference's, to pick a cell's population by.

    ``relative`` in the metric's bad direction; for a metric without a tolerance, which has
    no bad direction to judge by, the size of ``difference``. ``-inf`` where there is none.
    """
    outwards = np.array([1.0 if specFor(name).higherIsWorse else -1.0 for name in metric])
    judged = np.array([specFor(name).tolerance is not None for name in metric])
    directed = relative.astype("float64") * outwards
    return directed.where(judged, difference.astype("float64").abs()).fillna(-np.inf)


def worstFirst(comparison: pd.DataFrame) -> pd.DataFrame:
    """Return a comparison's rows worst first: by status, then by how far in the bad direction.

    The first row of a cell is the population the grid shows for it, and the one its
    detail opens on.
    """
    ranked = comparison.assign(
        rank=comparison["status"].map(_RANK).fillna(0),
        excess=_excess(comparison["metric"], comparison["relative"], comparison["difference"]),
    )
    ranked = ranked.sort_values(["rank", "excess"], ascending=False, kind="stable")
    return ranked.drop(columns=["rank", "excess"])


def rowOrder(cells: pd.DataFrame) -> list[str]:
    """Return the grid's ``row`` labels in a fixed order: by metric as drp_qa lists them, then frame type.

    The order does not follow the statuses, so a row stays where it is from night to night.
    """
    place = {metric: index for index, metric in enumerate(COMPARED_METRICS)}
    rows = cells.drop_duplicates("row")
    keys = sorted(
        (place.get(metric, len(place)), metric, obsType, row)
        for metric, obsType, row in zip(rows["metric"], rows["obsType"], rows["row"], strict=True)
    )
    return [row for *_, row in keys]


def gridCells(comparison: pd.DataFrame, deviations: pd.DataFrame) -> pd.DataFrame:
    """Return the grid's cells: one per reference, metric, frame type and camera.

    Parameters
    ----------
    comparison : `pandas.DataFrame`
        `store.MetricsStore.comparison`.
    deviations : `pandas.DataFrame`
        `visitDeviations` of the same visits.

    Returns
    -------
    `pandas.DataFrame`
        `CELL_COLUMNS`. ``status`` is the worst of the cell's populations' statuses (of
        their medians), ``population`` the one it is of (the furthest in the bad direction
        among the worst; for a metric without a tolerance, the furthest either way),
        ``relative`` and ``difference`` that one's differences from the reference median, ``nRun``
        and ``nReference`` the images its two medians are of. ``nVisits`` counts
        the visits with a value in the cell, ``nBeyond`` those with a value beyond the
        ``FAIL`` tolerance in any of its populations, and ``nWarned`` those beyond ``WARN``
        but not ``FAIL``. ``row`` labels the grid's row: the metric and the frame type.
    """
    if comparison.empty:
        return pd.DataFrame(columns=list(CELL_COLUMNS))
    ranked = worstFirst(comparison)
    ranked = ranked.assign(population=[populationLabel(row) for row in ranked.to_dict("records")])
    cells = ranked.groupby(list(CELL_KEYS), sort=False).agg(
        arm=("arm", "first"),
        spectrograph=("spectrograph", "first"),
        status=("status", "first"),
        relative=("relative", "first"),
        difference=("difference", "first"),
        population=("population", "first"),
        nPopulations=("population", "size"),
        nRun=("nRun", "first"),
        nReference=("nReference", "first"),
    )
    worst = (
        deviations.assign(rank=deviations["status"].map(_RANK).fillna(0))
        .groupby([*CELL_KEYS, "visit"])["rank"]
        .max()
        .reset_index()
    )
    counts = worst.groupby(list(CELL_KEYS)).agg(
        nVisits=("visit", "size"),
        nBeyond=("rank", lambda rank: int((rank == _RANK["FAIL"]).sum())),
        nWarned=("rank", lambda rank: int((rank == _RANK["WARN"]).sum())),
    )
    cells = cells.join(counts).reset_index()
    for column in ("nVisits", "nBeyond", "nWarned"):
        cells[column] = cells[column].fillna(0).astype("int64")
    cells["row"] = [
        " · ".join(part for part in (metric, obsType) if part)
        for metric, obsType in zip(cells["metric"], cells["obsType"], strict=True)
    ]
    return cells[list(CELL_COLUMNS)]


def judgedCells(cells: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Return the cells of `gridCells` with a verdict, and how many of the grid's rows have none at all.

    A cell is ``UNKNOWN`` when its metric declares no tolerance or the reference has nothing
    to compare with; a row of only those is left out of the grid whole.
    """
    judged = cells[cells["status"] != "UNKNOWN"]
    return judged, cells["row"].nunique() - judged["row"].nunique()


def populationRows(table: pd.DataFrame, cell: Sequence[str]) -> pd.DataFrame:
    """Return the rows of a comparison, or of `visitDeviations`, that one cell (`CELL_KEYS`) is made of.

    With ``population``, each row's `populationLabel`.
    """
    mask = np.ones(len(table), dtype=bool)
    for column, value in zip(CELL_KEYS, cell, strict=True):
        mask &= (table[column] == value).to_numpy()
    rows = table[mask].copy()
    rows["population"] = [populationLabel(row) for row in rows.to_dict("records")]
    return rows
