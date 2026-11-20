"""A collection's QA results in qadb, kept up to date while a run is observed.

`QadbStore` holds the tables `pfs.drp.qa.qadb.reader.readQadb` returns for a
collection. Its `refresh` reads from qadb only the images loaded since the last
read (the reader's ``since``) and replaces their rows, so a refresh costs what
the new visits cost; a refresh within ``minInterval`` of the last reads nothing.
Offline, the same store holds a snapshot of a qadb read
(`qadbSource.readSnapshot`), and never changes.
"""

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from pfs.drp.qa.qadb.reader import FIRST_VISIT, LAST_VISIT, readQadb

__all__ = ["HST", "QadbStore", "qadbProvenance"]

HST = ZoneInfo("Pacific/Honolulu")

_IMAGE = ["visit", "arm", "spectrograph"]

#: The tables of a qadb read the store keeps: one row per image, or per image and metric.
_RAW = ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics")

_TIMES = ["pfs_visit_id", "time_exp_start"]


def qadbProvenance(collection: str, **extra: Any) -> dict:
    """Return the provenance of a collection read from qadb: the collection, the drp_qa reading it, and ``extra``."""
    return {"collection": collection, "drpQaVersion": _drpQaVersion(), **extra}


class QadbStore:
    """The QA results of a collection in qadb, re-read as visits are loaded.

    Parameters
    ----------
    read : callable
        ``read(since)`` returns rows as `pfs.drp.qa.qadb.reader.readQadb` does:
        every image when ``since`` is None, else those loaded at or after it.
    minInterval : `float`
        Seconds within which a second `refresh` reads nothing.
    clock : callable
        Seconds, for ``minInterval``; `time.monotonic`.

    Attributes
    ----------
    raw : `dict` [`str`, `pandas.DataFrame`]
        ``iqQaMetrics``, ``dmLineResidualSummary`` and ``dmLineResidualMetrics``
        as qadb returned them, each image's latest load; empty before the
        first image is read.
    times : `pandas.DataFrame`
        qadb's own time for each visit read: ``visit`` and ``time`` (HST).
    version : `int`
        Counts the refreshes that brought a new load.
    since
        The latest load read (``qa_gate.decided_at``); None before the first.
    """

    def __init__(
        self,
        read: Callable[[Any], Mapping[str, pd.DataFrame]],
        minInterval: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._read = read
        self.minInterval = minInterval
        self._clock = clock
        self._lock = threading.Lock()
        self._lastRead: float | None = None
        self.since: Any = None
        self._seenAtSince: set[tuple] = set()
        self.version = 0
        self.raw: dict[str, pd.DataFrame] = {}
        self._times = pd.DataFrame(columns=_TIMES)
        self.refresh()

    @classmethod
    def fromQadb(
        cls,
        qadb: Any,
        collection: str,
        first: int = FIRST_VISIT,
        last: int = LAST_VISIT,
        minInterval: float = 15.0,
    ) -> "QadbStore":
        """Return the store of one collection's visits ``first``..``last`` in a qadb (read-only)."""
        return cls(lambda since: readQadb(qadb, collection, first, last, since), minInterval)

    @classmethod
    def fromTables(cls, tables: Mapping[str, pd.DataFrame]) -> "QadbStore":
        """Return the store of one qadb read (a snapshot's, `qadbSource.readSnapshot`); it never changes."""
        none = {name: frame.iloc[:0] for name, frame in tables.items()}
        return cls(lambda since: tables if since is None else none, 0)

    @property
    def times(self) -> pd.DataFrame:
        times = self._times.rename(columns={"pfs_visit_id": "visit", "time_exp_start": "time"})
        return times.assign(time=pd.to_datetime(times["time"]))

    def refresh(self) -> bool:
        """Read the images loaded since the last read and replace their rows.

        Returns
        -------
        `bool`
            Whether any load was new. False within ``minInterval`` of the last read.
        """
        with self._lock:
            now = self._clock()
            if self._lastRead is not None and now - self._lastRead < self.minInterval:
                return False
            self._lastRead = now
            changed = self._takeLoads(self._read(self.since))
            if changed:
                self.version += 1
            return changed

    def _takeLoads(self, tables: Mapping[str, pd.DataFrame]) -> bool:
        """Replace the rows of the images in ``tables`` (a qadb read); return whether any load was new."""
        iq = tables["iqQaMetrics"]
        if iq.empty:
            return False
        # ``since`` is inclusive, so the last read's newest loads come back every time: only
        # an image load not seen before (image and time) is new.
        loads = set(iq[[*_IMAGE, "decidedAt"]].itertuples(index=False, name=None))
        if loads <= self._seenAtSince:
            return False
        # Every image the read holds rows of, in whichever table: an image may have line
        # residuals and no image-quality row.
        judged = pd.MultiIndex.from_frame(
            pd.concat([tables[name][_IMAGE] for name in _RAW], ignore_index=True).drop_duplicates()
        )
        for name in _RAW:
            held = self.raw.get(name)
            if held is None or held.empty:
                self.raw[name] = tables[name]
                continue
            others = held[~pd.MultiIndex.from_frame(held[_IMAGE]).isin(judged)]
            parts = [part for part in (others, tables[name]) if len(part)]
            self.raw[name] = pd.concat(parts, ignore_index=True) if parts else held.iloc[:0]
        read = tables.get("visitTimes")
        if read is not None:
            times = pd.concat([self._times, read.reindex(columns=_TIMES)], ignore_index=True)
            self._times = times.drop_duplicates("pfs_visit_id", keep="last").sort_values(
                "pfs_visit_id", ignore_index=True
            )
        stamps = pd.concat([tables[name]["decidedAt"] for name in ("iqQaMetrics", "dmLineResidualSummary")])
        self.since = stamps.max()
        self._seenAtSince = {load for load in loads if load[-1] == self.since}
        return True


def _drpQaVersion() -> str:
    """Return the version of the drp_qa reading qadb, or ``?`` when it can't be read (no git checkout)."""
    try:
        from pfs.drp.qa.comparison.cli import drpQaVersion

        return drpQaVersion()
    except Exception:
        return "?"
