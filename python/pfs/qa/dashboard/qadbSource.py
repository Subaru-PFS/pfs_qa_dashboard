"""Turn what drp_qa reads back from qadb into the dashboard's metrics table (PIPE2D-1924).

During a run qaActor loads every visit it judges into qadb (INSTRM-3040);
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
from collections.abc import Mapping, Sequence
from pathlib import Path

import pandas as pd

from pfs.qa.dashboard.serving import buildMetricsTable, inferSequences

__all__ = ["metricsTableFromQadb", "readSnapshot", "withSequences", "writeSnapshot"]


def metricsTableFromQadb(tables: Mapping[str, pd.DataFrame], layers: Mapping[str, Sequence]) -> pd.DataFrame:
    """Return the metrics table for what `pfs.drp.qa.qadb.reader.readQadb` read; empty when it read no image.

    Parameters
    ----------
    tables : mapping
        From `pfs.drp.qa.qadb.reader.readQadb`.
    layers : mapping
        The thresholds each task judges with, as `serving.taskThresholds`
        returns them, to record each metric's limits beside its stored value.
    """
    iq = tables["iqQaMetrics"]
    if iq.empty:
        return pd.DataFrame()
    summary, metrics = tables["dmLineResidualSummary"], tables["dmLineResidualMetrics"]
    # A read can hold line verdicts and no line metrics (no species with enough lines): keep the verdicts.
    hasLines = not summary.empty
    return buildMetricsTable(
        iq.drop(columns="decidedAt"),
        layers["iqQaMetrics"],
        summary.drop(columns="decidedAt") if hasLines else None,
        metrics.drop(columns="decidedAt", errors="ignore") if hasLines else None,
        tables["visitTimes"],
        layers["dmLineResidualMetrics"],
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
