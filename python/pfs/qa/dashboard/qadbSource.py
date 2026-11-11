"""Turn what drp_qa reads back from qadb into the dashboard's metrics table.

During a run qaActor loads every visit it judges into qadb;
`pfs.drp.qa.qadb.reader.readQadb` reads them back in the Butler tables' shapes,
and `metricsTableFromQadb` builds them into the metrics table
(`serving.buildMetricsTable`). A run judged after the fact is loaded into a qadb
the same way (drp_qa's ``bin.src/loadQadb.py``). `writeSnapshot` saves a read to
parquet, for the dashboard offline.

Without the opdb, visit times are qadb's ``pfs_visit.issued_at`` (HST) and
sequences are inferred from runs of one ``seqName`` (`serving.inferSequences`),
as qadb holds no IIC sequence.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from pfs.qa.dashboard.serving import buildMetricsTable, inferSequences

__all__ = [
    "REFERENCE_TABLES",
    "metricsTableFromQadb",
    "readSnapshot",
    "referenceRecords",
    "referenceSnapshotTables",
    "referencesFromSnapshot",
    "withSequences",
    "writeSnapshot",
]

#: The snapshot tables holding the reference visits' metrics, every reference together.
REFERENCE_TABLES = ("referenceIqQaMetrics", "referenceLineMetrics")

_REFERENCE_NAME = "referenceName"


def metricsTableFromQadb(tables: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Return the metrics table for what `pfs.drp.qa.qadb.reader.readQadb` read; empty when it read no image.

    Each value's limits are those of the thresholds its image cites, read with
    the images (``thresholds``). A read without them (a snapshot written before
    they were read back) has values and verdicts, and no limits.

    Parameters
    ----------
    tables : mapping
        From `pfs.drp.qa.qadb.reader.readQadb`.
    """
    iq = tables["iqQaMetrics"]
    if iq.empty:
        return pd.DataFrame()
    summary, metrics = tables["dmLineResidualSummary"], tables["dmLineResidualMetrics"]
    # A read can hold line verdicts and no line metrics (no species with enough lines): keep the verdicts.
    hasLines = not summary.empty
    return buildMetricsTable(
        iq.drop(columns="decidedAt"),
        tables.get("thresholds"),
        summary.drop(columns="decidedAt") if hasLines else None,
        metrics.drop(columns="decidedAt", errors="ignore") if hasLines else None,
        tables["visitTimes"],
    )


def withSequences(table: pd.DataFrame, visitTimes: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return a metrics table with its sequences and times inferred again over all its visits.

    A table put together from several reads has each read's sequences, numbered
    apart; this numbers them as one (`serving.inferSequences`).
    """
    images = table[["visit", "seqName"]].drop_duplicates("visit")
    sequences = inferSequences(images, visitTimes)
    return (
        table.drop(columns=["sequenceKey", "sequenceSource", "timeExpStart"])
        .merge(sequences, on="visit", how="left")
        .reindex(columns=table.columns)
    )


def referenceRecords(references: Iterable[Any], collection: str) -> list[dict]:
    """Return what the provenance says of each `pfs.drp.qa.qadb.compareRuns.Reference`: name, collection, visits."""
    return [
        {"name": r.name, "collection": r.collection or collection, "visits": [r.first, r.last]}
        for r in references
    ]


def referenceSnapshotTables(references: Mapping[str, Sequence[pd.DataFrame]]) -> dict[str, pd.DataFrame]:
    """Return `REFERENCE_TABLES` for a snapshot: each reference's two metrics tables, with its name.

    Parameters
    ----------
    references : mapping
        By name, the tables `pfs.drp.qa.qadb.compareRuns.metricTables` read. Nothing for none.
    """
    if not references:
        return {}
    return {
        table: pd.concat(
            [tables[index].assign(**{_REFERENCE_NAME: name}) for name, tables in references.items()],
            ignore_index=True,
        )
        for index, table in enumerate(REFERENCE_TABLES)
    }


def referencesFromSnapshot(
    tables: dict[str, pd.DataFrame], names: Iterable[str]
) -> dict[str, list[pd.DataFrame]]:
    """Take `REFERENCE_TABLES` out of a snapshot's ``tables``; return each named reference's two tables."""
    held = [tables.pop(table, None) for table in REFERENCE_TABLES]
    if any(frame is None for frame in held):
        return {}
    return {
        name: [frame[frame[_REFERENCE_NAME] == name].drop(columns=_REFERENCE_NAME) for frame in held]
        for name in names
    }


def writeSnapshot(tables: Mapping[str, pd.DataFrame], directory: Path | str, provenance: Mapping) -> Path:
    """Write what `pfs.drp.qa.qadb.reader.readQadb` read to ``directory``: ``<table>.parquet`` each, and ``provenance.json``."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # The directory is this read only: a table an earlier snapshot there held, and this one doesn't, goes.
    for stale in directory.glob("*.parquet"):
        if stale.stem not in tables:
            stale.unlink()
    for name, frame in tables.items():
        frame.to_parquet(directory / f"{name}.parquet")
    (directory / "provenance.json").write_text(json.dumps(dict(provenance), indent=2, default=str) + "\n")
    return directory


def readSnapshot(directory: Path | str) -> tuple[dict[str, pd.DataFrame], dict]:
    """Return the tables and provenance `writeSnapshot` wrote to ``directory``."""
    directory = Path(directory)
    tables = {path.stem: pd.read_parquet(path) for path in sorted(directory.glob("*.parquet"))}
    return tables, json.loads((directory / "provenance.json").read_text())
