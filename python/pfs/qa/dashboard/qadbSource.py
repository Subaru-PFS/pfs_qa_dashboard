"""Snapshots of a qadb read, for the report offline.

During a run qaActor loads every visit it judges into qadb, and
`pfs.drp.qa.qadb.reader.readQadb` reads them back in the Butler tables' shapes.
A run judged after the fact is loaded into a qadb the same way (drp_qa's
``bin.src/loadQadb.py``). `writeSnapshot` saves a read to parquet and
`readSnapshot` returns it.
"""

import json
from collections.abc import Mapping
from pathlib import Path

import pandas as pd

__all__ = ["readSnapshot", "writeSnapshot"]


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
