"""Build the dashboard's metrics table: one long-format table per collection.

The dashboard reads only this table (with DuckDB), never a Butler. It is built
from the QA tasks' per-detector tables as qadb holds them
(`pfs.drp.qa.qadb.reader.readQadb`), so every number in it was stored by the pipeline or
judged by `pfs.drp.qa.metrics.gate`: the dashboard derives no metric. Each value's
limits are the thresholds qadb records its image was judged with (the file ``qa_gate``
cites for the task, whose entries the reader returns beside the images), found for the
image by the gate's own `~pfs.drp.qa.metrics.gate.judge`. That call is the one judgement
here: the image verdicts and the line-residual rows' statuses are kept as stored, but
``imageQualityQa`` stores no status per metric, so an IQ value's row takes `judge`'s,
from the same entries. An image citing none keeps its values and stored verdicts, with
no limits. A metric a later layer judged (the IIS
file, the config's fields) is not cited either, so it shows no limits.

One row per (image, source, metric, population), in `METRICS_COLUMNS`. An image's
verdict from each source is a row too, with ``metric`` `VERDICT`.
"""

from collections.abc import Iterator, Mapping

import numpy as np
import pandas as pd

from pfs.drp.qa.metrics.gate import judge
from pfs.drp.qa.qadb.reader import THRESHOLDS_ID, thresholdsTables
from pfs.qa.dashboard.schema import (
    IMAGE_STATUS_ORDER,
    METRICS_COLUMNS,
    NOTE_COLUMNS,
    VERDICT,
)

__all__ = [
    "IMAGE_STATUS_ORDER",
    "METRICS_COLUMNS",
    "VERDICT",
    "buildMetricsTable",
    "inferSequences",
    "noteRows",
]

#: iqQaMetrics columns shown as values; one an image's thresholds name gets its status and limits too.
_IQ_VALUES = (
    "medFwhm",
    "medDxCenter",
    "dxCenterRms",
    "pctFlagged",
    "linesPerFiber",
    "nLines",
    "nLitFibers",
    "cosmicRayCount",
    "fitXRms",
    "fitYRms",
)

#: Visits further apart than this, or of another seqName, start a new inferred sequence.
_SEQUENCE_GAP = 3

_IMAGE = ["visit", "arm", "spectrograph"]


def inferSequences(images: pd.DataFrame, visitTimes: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return each visit's sequence and start time.

    Parameters
    ----------
    images : `pandas.DataFrame`
        Rows with ``visit`` and ``seqName``.
    visitTimes : `pandas.DataFrame`, optional
        opdb's visit listing (`pfs.drp.qa.comparison.queries.readVisitListing`
        columns ``pfs_visit_id``, ``time_exp_start``, ``iic_sequence_id``).

    Returns
    -------
    `pandas.DataFrame`
        One row per visit: ``visit``, ``sequenceKey``, ``sequenceSource``
        (``"opdb"``, or ``"inferred"`` when the opdb listing is absent or
        lacks the visit) and ``timeExpStart`` (NaT without the listing).
        An inferred sequence is a run of visits with one ``seqName``, none
        more than a few visit numbers after the last.
    """
    visits = images.groupby("visit", sort=True)["seqName"].first().reset_index()
    newSequence = (visits["seqName"] != visits["seqName"].shift()) | (visits["visit"].diff() > _SEQUENCE_GAP)
    # Negative, so an inferred key never collides with an iic_sequence_id.
    # As plain booleans: a pyarrow-backed seqName compares to bool[pyarrow], which has no cumsum, and the
    # first visit, against a missing one, to NA: it starts a sequence.
    visits["sequenceKey"] = -np.cumsum(newSequence.to_numpy(dtype=bool, na_value=True)).astype("int64")
    visits["sequenceSource"] = "inferred"
    visits["timeExpStart"] = pd.NaT
    if visitTimes is not None and not visitTimes.empty:
        listing = visitTimes.set_index("pfs_visit_id")
        known = visits["visit"].isin(listing.index)
        ids = listing["iic_sequence_id"].reindex(visits["visit"]).to_numpy()
        fromOpdb = known & pd.notna(ids)
        visits.loc[fromOpdb, "sequenceKey"] = ids[fromOpdb.to_numpy()].astype("int64")
        visits.loc[fromOpdb, "sequenceSource"] = "opdb"
        times = pd.to_datetime(listing["time_exp_start"]).reindex(visits["visit"]).to_numpy()
        visits["timeExpStart"] = times
    return visits.drop(columns="seqName")


def _byThresholds(
    rows: pd.DataFrame, tables: Mapping[int, pd.DataFrame]
) -> Iterator[tuple[pd.DataFrame, pd.DataFrame | None]]:
    """Yield the rows judged with each thresholds file and the file's table; `None` for rows citing none.

    Rows without a ``thresholdsId`` column (a read older than it) all cite none.
    """
    if THRESHOLDS_ID not in rows.columns:
        yield rows, None
        return
    for thresholdsId, group in rows.groupby(THRESHOLDS_ID, dropna=False, sort=False):
        yield group, None if pd.isna(thresholdsId) else tables.get(int(thresholdsId))


def _measured(table: pd.DataFrame, metrics: pd.DataFrame) -> pd.DataFrame:
    """Return the entries of ``table`` on metrics ``metrics`` has columns for.

    The gate refuses a table naming a metric the task did not measure; the dashboard shows what
    is stored, and an entry on a metric qadb does not hold simply has no value to judge.
    """
    return table[table["metric"].isin(metrics.columns)]


def _iqRows(iq: pd.DataFrame, tables: Mapping[int, pd.DataFrame]) -> pd.DataFrame:
    """Per-metric rows of ``iqQaMetrics``: each image's values, judged with the thresholds it cites, and its stored verdict."""
    parts = []
    for rows, table in _byThresholds(iq, tables):
        rows = rows.reset_index(drop=True)
        judgedNames: set[str] = set()
        entries = _measured(table, rows) if table is not None else None
        if entries is not None and not entries.empty:
            judged = judge(rows, entries)
            judged = judged.assign(
                source="iqQaMetrics",
                description="",
                reference="",
                statusType="",
                run=rows["calexpRun"].to_numpy()[judged["row"]] if "calexpRun" in rows else "",
            )
            judgedNames = set(judged["metric"])
            parts.append(judged.drop(columns=["row", "layer", "provenance"], errors="ignore"))
        plainNames = [name for name in _IQ_VALUES if name in rows.columns and name not in judgedNames]
        plain = rows[[*_IMAGE, *plainNames]].melt(id_vars=_IMAGE, var_name="metric", value_name="value")
        plain = plain.assign(source="iqQaMetrics", status="", population="", reason="")
        if "calexpRun" in rows:
            plain["run"] = np.tile(rows["calexpRun"].to_numpy(), len(plainNames))
        parts.append(plain)
    verdicts = iq[[*_IMAGE, "qaStatus", "qaReason"]].rename(
        columns={"qaStatus": "status", "qaReason": "reason"}
    )
    verdicts = verdicts.assign(source="iqQaMetrics", metric=VERDICT, value=np.nan)
    if "calexpRun" in iq:
        verdicts["run"] = iq["calexpRun"].to_numpy()
    return pd.concat([*parts, verdicts], ignore_index=True)


def noteRows(notes: pd.DataFrame, visitTimes: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return opdb's notes as the dashboard shows them, one row per visit (or camera) a note is on.

    A note on a visit applies to all its cameras (``arm`` empty, ``spectrograph`` 0); an
    ``sps_annotation`` to its camera; a sequence's note to each of its visits in ``visitTimes``.

    Parameters
    ----------
    notes : `pandas.DataFrame`
        `pfs.drp.qa.comparison.queries.readNotes`.
    visitTimes : `pandas.DataFrame`, optional
        opdb's visit listing, mapping sequences to visits.

    Returns
    -------
    `pandas.DataFrame`
        `NOTE_COLUMNS`: ``visit``, ``arm``, ``spectrograph``, ``source``
        (``opdb:`` and the note's source) and ``note``, sorted.
    """
    onVisits = notes[notes["pfs_visit_id"].notna()].rename(columns={"pfs_visit_id": "visit"})
    onSequences = notes[notes["pfs_visit_id"].isna() & notes["iic_sequence_id"].notna()]
    if visitTimes is not None and not onSequences.empty:
        members = visitTimes[["pfs_visit_id", "iic_sequence_id"]].dropna().astype("int64")
        expanded = onSequences.drop(columns="pfs_visit_id").merge(
            members.astype({"iic_sequence_id": "Int64"}), on="iic_sequence_id"
        )
        onVisits = pd.concat(
            [onVisits, expanded.rename(columns={"pfs_visit_id": "visit"})], ignore_index=True
        )
    camera = onVisits["camera"].fillna("").astype(str)
    return pd.DataFrame(
        {
            "visit": onVisits["visit"].astype("int64").to_numpy(),
            "arm": camera.str[:1].to_numpy(),
            "spectrograph": pd.to_numeric(camera.str[1:], errors="coerce")
            .fillna(0)
            .astype("int64")
            .to_numpy(),
            "source": ("opdb:" + onVisits["source"].astype(str)).to_numpy(),
            "note": onVisits["note"].astype(str).str.strip().to_numpy(),
        }
    ).sort_values(NOTE_COLUMNS, ignore_index=True)


def _lineLimits(metrics: pd.DataFrame, tables: Mapping[int, pd.DataFrame]) -> pd.DataFrame:
    """Return ``warn``, ``fail`` and ``population`` for each ``dmLineResidualMetrics`` row.

    The task stores each row's verdict but not the thresholds behind it; these are what
    `pfs.drp.qa.metrics.gate.judge` finds for the row in the file its image cites.
    """
    limits = pd.DataFrame({"warn": np.nan, "fail": np.nan, "population": ""}, index=metrics.index)
    for rows, table in _byThresholds(metrics, tables):
        if table is None:
            continue
        for name, named in rows.groupby("metric", sort=False):
            entries = table[table["metric"] == name]
            if entries.empty:
                continue
            wide = named.drop(columns=["metric", "value", "status", THRESHOLDS_ID], errors="ignore")
            wide = wide.assign(**{name: named["value"].to_numpy()})
            judged = judge(wide.reset_index(drop=True), entries)
            limits.loc[named.index, ["warn", "fail", "population"]] = judged[
                ["warn", "fail", "population"]
            ].to_numpy()
    return limits


def _lineRows(
    summary: pd.DataFrame, metrics: pd.DataFrame, tables: Mapping[int, pd.DataFrame]
) -> pd.DataFrame:
    """Rows of ``dmLineResidualMetrics`` as stored, with their thresholds, and ``dmLineResidualSummary``'s verdicts."""
    # The thresholds name each row's camera set and observation type, which only the summary holds in qadb.
    context = [column for column in ("cameraSet", "obsType") if column in summary and column not in metrics]
    if context:
        metrics = metrics.merge(summary[[*_IMAGE, *context]].drop_duplicates(_IMAGE), on=_IMAGE, how="left")
    rows = metrics.assign(source="dmLineResidualMetrics", population="", reason="")
    if tables and not metrics.empty:
        rows[["warn", "fail", "population"]] = _lineLimits(metrics, tables)
    verdicts = summary[[*_IMAGE, "qaStatus", "qaReason", "recommendedAction"]]
    verdicts = verdicts.rename(columns={"qaStatus": "status"})
    verdicts = verdicts.assign(
        source="dmLineResidualSummary",
        metric=VERDICT,
        value=np.nan,
        reason=[
            "; ".join(str(part) for part in parts if isinstance(part, str) and part)
            for parts in zip(verdicts["qaReason"], verdicts["recommendedAction"], strict=True)
        ],
    ).drop(columns=["qaReason", "recommendedAction"])
    return pd.concat(
        [rows.drop(columns=["cameraSet", THRESHOLDS_ID], errors="ignore"), verdicts], ignore_index=True
    )


def buildMetricsTable(
    iqQaMetrics: pd.DataFrame,
    thresholds: pd.DataFrame | None = None,
    dmLineResidualSummary: pd.DataFrame | None = None,
    dmLineResidualMetrics: pd.DataFrame | None = None,
    visitTimes: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return the metrics table for one collection.

    Parameters
    ----------
    iqQaMetrics : `pandas.DataFrame`
        The collection's ``iqQaMetrics`` rows, one per image, each with the
        ``thresholdsId`` it cites (`pfs.drp.qa.qadb.reader.readQadb`).
    thresholds : `pandas.DataFrame`, optional
        The cited files' entries, as `~pfs.drp.qa.qadb.reader.readQadb`
        returns them (``thresholds``): each image's metrics are judged with
        the file its ``thresholdsId`` names, and the entry's status and
        limits recorded beside the value. The stored verdict of each image
        is kept as it is. Without them, or for an image citing none, values
        have no limits.
    dmLineResidualSummary, dmLineResidualMetrics : `pandas.DataFrame`, optional
        The same collection's ``dmLineResiduals`` outputs, with their
        ``thresholdsId``: each stored line-residual verdict gets the limits
        of the file its image cites.
    visitTimes : `pandas.DataFrame`, optional
        opdb's visit listing, for times and sequences (`inferSequences`).

    Returns
    -------
    `pandas.DataFrame`
        `METRICS_COLUMNS`, sorted by visit, arm, spectrograph and source.
        Only visits in ``iqQaMetrics``: rows on any other visit are left out.
    """
    tables = thresholdsTables(thresholds) if thresholds is not None and len(thresholds) else {}
    parts = [_iqRows(iqQaMetrics, tables)]
    if dmLineResidualSummary is not None and dmLineResidualMetrics is not None:
        parts.append(_lineRows(dmLineResidualSummary, dmLineResidualMetrics, tables))
    table = pd.concat(parts, ignore_index=True)
    # A visit without an iqQaMetrics row has no sequence to file its rows under.
    table = table[table["visit"].isin(iqQaMetrics["visit"])]
    images = iqQaMetrics[[*_IMAGE, "obsType", "seqName"]].drop_duplicates(_IMAGE)
    table = table.drop(columns=["obsType", "seqName"], errors="ignore").merge(images, on=_IMAGE, how="left")
    table = table.merge(inferSequences(images, visitTimes), on="visit", how="left")
    for column, dtype in METRICS_COLUMNS.items():
        if column not in table:
            table[column] = np.nan if dtype in ("float64",) else ""
        if dtype == "string":
            table[column] = table[column].fillna("").astype(str)
    table = table[list(METRICS_COLUMNS)].astype(
        {k: v for k, v in METRICS_COLUMNS.items() if v not in ("string", "datetime64[ns]")}
    )
    return table.sort_values([*_IMAGE, "source"], kind="stable", ignore_index=True)
