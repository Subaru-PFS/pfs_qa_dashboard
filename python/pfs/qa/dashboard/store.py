"""Query the metrics table with DuckDB: what the dashboard reads, and nothing else.

Each method is one SQL query over the table `serving.buildMetricsTable` built,
with bound parameters, returning a DataFrame. No Butler, no opdb.
"""

from collections.abc import Collection, Mapping, Sequence

import duckdb
import numpy as np
import pandas as pd

from pfs.drp.qa.metrics.runComparison import COMPARED_METRICS, COMPARISON_COLUMNS, compareRuns, metricRows
from pfs.drp.qa.metrics.visitRollup import rollupVisits
from pfs.drp.qa.qadb.compareRuns import comparisonTables
from pfs.drp.qa.qadb.tables import cameraName
from pfs.qa.dashboard.schema import IMAGE_STATUS_ORDER, METRICS_COLUMNS, NOTE_COLUMNS, VERDICT

__all__ = ["VERDICT_SOURCES", "MetricsStore", "emptyMetricsTable", "visitTimeline"]

#: Sources whose verdicts roll up into an image's status.
VERDICT_SOURCES = ("iqQaMetrics", "dmLineResidualSummary")

#: The task behind each of an image's verdict columns.
_TASKS = {"iqStatus": "imageQualityQa", "lineStatus": "dmLineResiduals"}

# Worst verdict of an image over VERDICT_SOURCES.
_RANK = "CASE status WHEN 'FAIL' THEN 3 WHEN 'WARN' THEN 2 WHEN 'PASS' THEN 1 ELSE 0 END"
_IMAGES_SQL = f"""
SELECT visit, arm, spectrograph,
    any_value(obsType) AS obsType, any_value(seqName) AS seqName,
    any_value(sequenceKey) AS sequenceKey, any_value(sequenceSource) AS sequenceSource,
    any_value(timeExpStart) AS timeExpStart,
    max({_RANK}) AS rank,
    max(CASE WHEN source = 'iqQaMetrics' THEN status END) AS iqStatus,
    max(CASE WHEN source = 'dmLineResidualSummary' THEN status END) AS lineStatus,
    string_agg(CASE WHEN reason <> '' THEN source || ': ' || reason END, ' | ') AS reasons
FROM metrics
WHERE metric = $verdict AND source IN ('iqQaMetrics', 'dmLineResidualSummary')
    AND visit BETWEEN $first AND $last
GROUP BY visit, arm, spectrograph
ORDER BY visit, arm, spectrograph
"""


def emptyMetricsTable() -> pd.DataFrame:
    """Return a metrics table with no rows and `METRICS_COLUMNS`' types."""
    return pd.DataFrame({column: pd.Series(dtype=dtype) for column, dtype in METRICS_COLUMNS.items()})


class MetricsStore:
    """A metrics table in memory, queried with DuckDB.

    Parameters
    ----------
    table : `pandas.DataFrame`, optional
        From `pfs.qa.dashboard.serving.buildMetricsTable`; empty by default.
    provenance : mapping, optional
        What the app's provenance bar shows (``collection``, ``thresholds``, ...).
    notes : `pandas.DataFrame`, optional
        opdb's notes on the visits (`pfs.qa.dashboard.serving.noteRows`); none by default.
    thresholdsFiles : `pandas.DataFrame`, optional
        The thresholds files the images cite, as `pfs.drp.qa.qadb.reader.readQadb` returns
        them (``thresholdsFiles``), for the provenance strip; none by default.

    Attributes
    ----------
    raw : `dict` [`str`, `pandas.DataFrame`]
        The metrics tables as qadb returned them (``iqQaMetrics``, ``dmLineResidualSummary``,
        ``dmLineResidualMetrics``), which the comparison with the references is made from;
        empty for a store built from a metrics table alone.
    references : `dict` [`str`, `list` [`pandas.DataFrame`]]
        The reference visits' metrics tables by reference name, as
        `pfs.drp.qa.metrics.runComparison.compareRuns` takes them; none by default.
    """

    #: Whether `refresh` can bring new visits (`pfs.qa.dashboard.live.QadbMetricsStore` over qadb).
    live = False

    #: Counts the changes `refresh` made.
    version = 0

    #: Whether the visits' sequences, times and notes come from the opdb.
    hasOpdb = False

    def __init__(
        self,
        table: pd.DataFrame | None = None,
        provenance: Mapping | None = None,
        notes: pd.DataFrame | None = None,
        thresholdsFiles: pd.DataFrame | None = None,
    ):
        self.table = emptyMetricsTable() if table is None else table
        self.provenance = dict(provenance or {})
        self.noteTable = pd.DataFrame(columns=NOTE_COLUMNS) if notes is None else notes
        self.thresholdsFiles = pd.DataFrame() if thresholdsFiles is None else thresholdsFiles
        self.raw: dict[str, pd.DataFrame] = {}
        self.references: dict[str, list[pd.DataFrame]] = {}
        self._db = duckdb.connect()
        self._register()

    def _register(self) -> None:
        """Let the queries see ``table`` as ``metrics``; again after replacing it."""
        strings = [column for column, dtype in METRICS_COLUMNS.items() if dtype == "string"]
        self._db.register("metrics", self.table.astype(dict.fromkeys(strings, "string")))

    def refresh(self) -> bool:
        """Read what changed since the last call; a fixed table never changes. Return whether anything did."""
        return False

    def _query(self, sql: str, **params) -> pd.DataFrame:
        frame = self._db.execute(sql, params).df()
        # Plain object strings, which every pane and test handles alike; Arrow-backed strings trip some.
        strings = frame.select_dtypes(include=["string"]).columns
        return frame.astype(dict.fromkeys(strings, object))

    def visits(self) -> pd.DataFrame:
        """Return one row per visit: ``visit``, ``obsType``, ``seqName``, sequence and time."""
        return self._query(
            """
            SELECT visit, any_value(obsType) AS obsType, any_value(seqName) AS seqName,
                any_value(sequenceKey) AS sequenceKey, any_value(sequenceSource) AS sequenceSource,
                any_value(timeExpStart) AS timeExpStart
            FROM metrics GROUP BY visit ORDER BY visit
            """
        )

    def images(self, first: int = 0, last: int = 2**31) -> pd.DataFrame:
        """Return one row per image of visits ``first``..``last`` with its rolled-up ``status``.

        ``status`` is the worst of the stored verdicts of `VERDICT_SOURCES`,
        ``UNKNOWN`` when none judged the image.
        """
        images = self._query(_IMAGES_SQL, verdict=VERDICT, first=first, last=last)
        images["status"] = [IMAGE_STATUS_ORDER[int(rank)] for rank in images.pop("rank")]
        images["reasons"] = images["reasons"].fillna("")
        return images

    def rollup(self, first: int = 0, last: int = 2**31) -> pd.DataFrame:
        """Return one row per visit ``first``..``last``: the worst of its images' statuses and what decided it.

        `pfs.drp.qa.metrics.visitRollup.rollupVisits` over `images`:
        `~pfs.drp.qa.metrics.visitRollup.ROLLUP_COLUMNS`, where ``decidedByTask`` names the
        task or tasks whose verdict the deciding image's status is.
        """
        gate = self.images(first, last).rename(columns={"status": "qaStatus", "reasons": "qaReason"})
        gate["decidedByTask"] = [
            ", ".join(task for column, task in _TASKS.items() if row[column] == row["qaStatus"])
            for row in gate.to_dict("records")
        ]
        return rollupVisits(gate)

    def _runTables(self, visits: Collection[int] | None) -> list[pd.DataFrame]:
        """Return the tables `compareRuns` takes, of ``visits`` or of every visit."""
        if not self.raw:
            return []
        tables = comparisonTables(self.raw)
        if visits is None:
            return tables
        return [table[table["visit"].isin(list(visits))] for table in tables]

    def comparison(self, visits: Collection[int] | None = None) -> pd.DataFrame:
        """Return the medians of ``visits`` (every visit by default) against each reference's.

        `pfs.drp.qa.metrics.runComparison.compareRuns`, made when asked for:
        `~pfs.drp.qa.metrics.runComparison.COMPARISON_COLUMNS`, with no rows without references.
        """
        tables = [table for table in self._runTables(visits) if len(table)]
        if not tables or not self.references:
            return pd.DataFrame(columns=list(COMPARISON_COLUMNS))
        return compareRuns(tables, self.references)

    def metricValues(
        self, visits: Collection[int] | None = None, metrics: Sequence[str] = COMPARED_METRICS
    ) -> pd.DataFrame:
        """Return the values the comparison's medians are of: one row per image, population and metric.

        `pfs.drp.qa.metrics.runComparison.metricRows` of ``visits`` (every visit by default),
        with ``camera``.
        """
        parts = [metricRows(table, metrics) for table in self._runTables(visits) if len(table)]
        rows = pd.concat(parts, ignore_index=True) if parts else metricRows(pd.DataFrame(), metrics)
        rows["camera"] = [
            cameraName(arm, spec) for arm, spec in zip(rows["arm"], rows["spectrograph"], strict=True)
        ]
        return rows

    def notes(self, first: int = 0, last: int = 2**31) -> pd.DataFrame:
        """Return the opdb notes on visits ``first``..``last``: ``visit``, ``arm``, ``spectrograph``, ``source``, ``note``."""
        notes = self.noteTable[self.noteTable["visit"].between(first, last)]
        return notes.drop_duplicates().sort_values(NOTE_COLUMNS, ignore_index=True)

    def visitMetrics(self, visit: int) -> pd.DataFrame:
        """Return every row of one visit, verdicts first."""
        return self._query(
            """
            SELECT * FROM metrics WHERE visit = $visit
            ORDER BY arm, spectrograph, metric <> $verdict, source, metric, reference, statusType, description
            """,
            visit=visit,
            verdict=VERDICT,
        )

    def trend(
        self,
        source: str,
        metric: str,
        first: int = 0,
        last: int = 2**31,
        reference: str | None = None,
        statusType: str | None = None,
    ) -> pd.DataFrame:
        """Return one metric's values over visits ``first``..``last``.

        Parameters
        ----------
        source, metric : `str`
            The table and metric, e.g. ``"iqQaMetrics"``, ``"medFwhm"``.
        first, last : `int`
            The visit range, inclusive.
        reference, statusType : `str`, optional
            For ``dmLineResidualMetrics``: ``calib`` or ``visit``, and
            ``BOTH``, ``USED`` or ``RESERVED``.

        Returns
        -------
        `pandas.DataFrame`
            One row per image, population and species, sorted by visit.
        """
        return self._query(
            """
            SELECT visit, arm, spectrograph, obsType, seqName, description, value, status, warn, fail,
                population, timeExpStart
            FROM metrics
            WHERE source = $source AND metric = $metric AND visit BETWEEN $first AND $last
                AND ($reference IS NULL OR reference = $reference)
                AND ($statusType IS NULL OR statusType = $statusType)
                AND isfinite(value)
            ORDER BY visit, arm, spectrograph
            """,
            source=source,
            metric=metric,
            first=first,
            last=last,
            reference=reference,
            statusType=statusType,
        )


def visitTimeline(visits: pd.DataFrame) -> pd.DataFrame:
    """Return each visit's place in time: ``visit``, ``position``, ``time``, ``night``, ``clock``.

    ``position`` counts the visits in order from 0; ``time`` is the start of the visit's first SpS
    exposure (HST, from the opdb listing; NaT without it); ``night`` is the date of the evening the
    night began (``time`` less 12 hours), so a night's visits share it across midnight.
    """
    timeline = visits[["visit", "timeExpStart"]].sort_values("visit").reset_index(drop=True)
    timeline = timeline.rename(columns={"timeExpStart": "time"})
    timeline["time"] = pd.to_datetime(timeline["time"])
    timeline["position"] = np.arange(len(timeline))
    timeline["night"] = (timeline["time"] - pd.Timedelta(hours=12)).dt.strftime("%Y-%m-%d").fillna("")
    timeline["clock"] = timeline["time"].dt.strftime("%H:%M").fillna("")
    return timeline
