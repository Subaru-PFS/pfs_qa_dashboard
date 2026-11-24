"""A collection's QA results in qadb, kept up to date while a run is observed.

`QadbStore` holds what `readCollection` reads of a collection: the numbers
`pfs.drp.qa.qadb.reader.readQadb` returns and each image's gate verdict
(`pfs.drp.qa.qadb.reader.readGate`, as ``qaGate``). Its `refresh` reads from
qadb only the images loaded or judged since the last read (the reader's
``since``, less `OVERLAP`) and replaces their rows, so a refresh costs what
the new visits cost. Offline, the same store holds a snapshot of a qadb read
(`qadbSource.readSnapshot`), and never changes.
"""

import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from pfs.drp.qa.qadb.reader import FIRST_VISIT, GATE_COLUMNS, LAST_VISIT, readGate, readQadb

__all__ = ["GATE", "HST", "OVERLAP", "QadbStore", "qadbProvenance", "readCollection"]

HST = ZoneInfo("Pacific/Honolulu")

_IMAGE = ["visit", "arm", "spectrograph"]

#: The table of a read holding each image's gate verdict: `pfs.drp.qa.qadb.reader.readGate`.
GATE = "qaGate"

#: The numbers of a qadb read the store keeps: one row per image, or per image and metric.
_NUMBERS = ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics")

#: Every table of a read the store keeps.
_RAW = (*_NUMBERS, GATE)

#: Those with one row per image: their visits are those a read brought.
_PER_IMAGE = ("iqQaMetrics", "dmLineResidualSummary")

#: How far before the latest load already read each refresh reads again. A load is stamped
#: (``qa_gate.decided_at``) just before its rows are written, so two loaders at once can commit
#: out of the order they stamped: a load stamped earlier than one already read, and committed
#: after it, is read on the next refresh as long as its write took less than this.
OVERLAP = timedelta(minutes=5)

_TIMES = ["pfs_visit_id", "time_exp_start"]


def readCollection(
    qadb: Any, collection: str, first: int = FIRST_VISIT, last: int = LAST_VISIT, since: Any = None
) -> dict[str, pd.DataFrame]:
    """Read a collection's numbers and verdicts from qadb (read-only).

    Returns
    -------
    `dict` [`str`, `pandas.DataFrame`]
        What `pfs.drp.qa.qadb.reader.readQadb` returns, with ``since``, and
        `GATE`: each image's latest gate row (`pfs.drp.qa.qadb.reader.readGate`).
        With ``since``, the gate rows of the visit range the numbers read
        covers, which holds every image they hold; none when they hold none.
    """
    tables = dict(readQadb(qadb, collection, first, last, since))
    if since is None:
        tables[GATE] = readGate(qadb, collection, first, last)
        return tables
    visits = [int(visit) for name in _PER_IMAGE for visit in tables[name]["visit"]]
    tables[GATE] = (
        readGate(qadb, collection, min(visits), max(visits))
        if visits
        else pd.DataFrame(columns=list(GATE_COLUMNS))
    )
    return tables


def qadbProvenance(collection: str, **extra: Any) -> dict:
    """Return the provenance of a collection read from qadb: the collection, the drp_qa reading it, and ``extra``."""
    return {"collection": collection, "drpQaVersion": _drpQaVersion(), **extra}


class QadbStore:
    """The QA results of a collection in qadb, re-read as visits are loaded.

    Parameters
    ----------
    read : callable
        ``read(since)`` returns tables as `readCollection` does: every image
        when ``since`` is None, else those loaded or judged at or after it (and,
        in `GATE`, maybe others).

    Attributes
    ----------
    raw : `dict` [`str`, `pandas.DataFrame`]
        ``iqQaMetrics``, ``dmLineResidualSummary``, ``dmLineResidualMetrics``
        and `GATE` as qadb returned them, each image's latest load; empty
        before the first image is read.
    times : `pandas.DataFrame`
        qadb's own time for each visit read: ``visit`` and ``time`` (HST).
    version : `int`
        Counts the refreshes that brought a new load.
    since
        The latest load read (``qa_gate.decided_at``); None before the first.
        A refresh reads from `OVERLAP` before it, and takes again only what
        it has not seen.
    """

    def __init__(self, read: Callable[[Any], Mapping[str, pd.DataFrame]]):
        self._read = read
        self._lock = threading.Lock()
        self.since: Any = None
        # The loads read within OVERLAP of ``since``, by table, image and time: those a refresh reads again.
        self._seen: set[tuple] = set()
        self.version = 0
        self.raw: dict[str, pd.DataFrame] = {}
        self._times = pd.DataFrame(columns=_TIMES)
        self.refresh()

    @classmethod
    def fromQadb(
        cls, qadb: Any, collection: str, first: int = FIRST_VISIT, last: int = LAST_VISIT
    ) -> "QadbStore":
        """Return the store of one collection's visits ``first``..``last`` in a qadb (read-only)."""
        return cls(lambda since: readCollection(qadb, collection, first, last, since))

    @classmethod
    def fromTables(cls, tables: Mapping[str, pd.DataFrame]) -> "QadbStore":
        """Return the store of one qadb read (a snapshot's, `qadbSource.readSnapshot`); it never changes."""
        none = {name: frame.iloc[:0] for name, frame in tables.items()}
        return cls(lambda since: tables if since is None else none)

    @property
    def times(self) -> pd.DataFrame:
        times = self._times.rename(columns={"pfs_visit_id": "visit", "time_exp_start": "time"})
        return times.assign(time=pd.to_datetime(times["time"]))

    def refresh(self) -> bool:
        """Read the images loaded since the last read and replace their rows.

        Returns
        -------
        `bool`
            Whether any load was new.
        """
        with self._lock:
            changed = self._takeLoads(self._read(_before(self.since)))
            if changed:
                self.version += 1
            return changed

    def _takeLoads(self, tables: Mapping[str, pd.DataFrame]) -> bool:
        """Replace the rows of the images in ``tables`` (a qadb read); return whether any load was new.

        A load is new by table, image and time. A read whose queries straddle a load can bring an
        image's line residuals at the new time and its image-quality row at the old one (or none):
        each table replaces only the images it brought itself, and the next read, which brings the
        image-quality row at the new time, has a load not yet seen.
        """
        columns = [*_IMAGE, "decidedAt"]
        loads = {
            (name, *load)
            for name in _NUMBERS
            for load in tables[name]
            .reindex(columns=columns)
            .drop_duplicates()
            .itertuples(index=False, name=None)
        }
        if loads <= self._seen:
            return False
        for name in _RAW:
            read = tables.get(name)
            if read is None:
                continue
            held = self.raw.get(name)
            if held is None or held.empty:
                self.raw[name] = read
                continue
            # One key per held row (a table may hold several rows per image), against the images read.
            others = held[~pd.MultiIndex.from_frame(held[_IMAGE]).isin(_images(read[_IMAGE]))]
            parts = [part for part in (others, read) if len(part)]
            self.raw[name] = pd.concat(parts, ignore_index=True) if parts else held.iloc[:0]
        read = tables.get("visitTimes")
        if read is not None:
            # Exclude an empty part (the first read's empty ``self._times``) before the concat: pandas
            # drops empty/all-NA entries when resolving the result dtypes, which it warns is going away.
            parts = [part for part in (self._times, read.reindex(columns=_TIMES)) if len(part)]
            times = pd.concat(parts, ignore_index=True) if parts else self._times
            self._times = times.drop_duplicates("pfs_visit_id", keep="last").sort_values(
                "pfs_visit_id", ignore_index=True
            )
        latest = max(load[-1] for load in loads)
        self.since = latest if self.since is None else max(self.since, latest)
        cutoff = _before(self.since)
        self._seen = {load for load in self._seen | loads if load[-1] >= cutoff}
        return True


def _before(since: Any) -> Any:
    """Return where a refresh reads from: `OVERLAP` before ``since`` (a time), or ``since`` itself."""
    return since - OVERLAP if isinstance(since, datetime) else since


def _images(keys: pd.DataFrame) -> pd.MultiIndex:
    """Return the images of some rows' keys (visit, arm, spectrograph), once each."""
    return pd.MultiIndex.from_frame(keys.drop_duplicates())


def _drpQaVersion() -> str:
    """Return the version of the drp_qa reading qadb, or ``?`` when it can't be read (no git checkout)."""
    try:
        from pfs.drp.qa.comparison.cli import drpQaVersion

        return drpQaVersion()
    except Exception:
        return "?"
