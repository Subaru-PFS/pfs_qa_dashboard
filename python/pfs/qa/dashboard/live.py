"""The metrics table of a collection in qadb, kept up to date while a run is observed.

`QadbMetricsStore` is a `store.MetricsStore` whose `refresh` reads from qadb
only the images loaded since the last read (`pfs.drp.qa.qadb.reader.readQadb`'s ``since``)
and replaces their rows, so a refresh costs what the new visits cost. One store
serves every browser session; a refresh within ``minInterval`` of the last is
skipped, so many sessions polling it make one query. Offline, the same store
reads a snapshot of qadb (`qadbSource.writeSnapshot`) once.
"""

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from pfs.drp.qa.qadb.reader import FIRST_VISIT, LAST_VISIT, THRESHOLDS_ID, readQadb
from pfs.qa.dashboard.qadbSource import (
    metricsTableFromQadb,
    readSnapshot,
    referenceRecords,
    referencesFromSnapshot,
    withSequences,
)
from pfs.qa.dashboard.schema import NOTE_COLUMNS
from pfs.qa.dashboard.serving import noteRows
from pfs.qa.dashboard.store import MetricsStore

__all__ = ["HST", "OpdbTables", "QadbMetricsStore", "opdbReader", "qadbProvenance"]

HST = ZoneInfo("Pacific/Honolulu")

_log = logging.getLogger(__name__)

_IMAGE = ["visit", "arm", "spectrograph"]

#: The tables of a qadb read kept as read, for the comparison with the references.
_RAW = ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics")


def qadbProvenance(collection: str, **extra: Any) -> dict:
    """Return the provenance of a collection read from qadb: the collection, the drp_qa reading it, and ``extra``.

    The thresholds the images were judged with are not here: they are read with the images
    (`QadbMetricsStore.thresholdsFiles`), as qadb cites them.
    """
    return {"collection": collection, "drpQaVersion": _drpQaVersion(), **extra}


#: What `QadbMetricsStore`'s ``readOpdb`` returns: the visits' times and sequences, and the notes on them.
OpdbTables = tuple[pd.DataFrame, pd.DataFrame]

_TIMES = ["pfs_visit_id", "time_exp_start", "iic_sequence_id"]


def opdbReader(opdb: Any) -> Callable[[list[int]], OpdbTables]:
    """Return a ``readOpdb`` for `QadbMetricsStore` that reads ``opdb`` (read-only)."""
    from pfs.drp.qa.comparison.queries import readNotes, readVisitTimes

    def read(visits: list[int]) -> OpdbTables:
        times = readVisitTimes(opdb, visits)
        return times, readNotes(opdb, visits, times["iic_sequence_id"].dropna())

    return read


class QadbMetricsStore(MetricsStore):
    """The metrics table of a collection in qadb, re-read as visits are loaded.

    Parameters
    ----------
    read : callable
        ``read(since)`` returns new rows as `pfs.drp.qa.qadb.reader.readQadb` does: every
        image when ``since`` is None, else those loaded at or after it, with the
        thresholds they cite (``thresholds``, ``thresholdsFiles``).
    provenance : mapping
        What the app's provenance bar shows (``collection``, ``thresholds``, ...).
    minInterval : `float`
        Seconds within which a second `refresh` reads nothing.
    clock : callable
        Seconds, for ``minInterval``; `time.monotonic`.
    readOpdb : callable, optional
        ``readOpdb(visits)`` returns the visits' start times and IIC sequences
        (`comparison.queries.readVisitTimes`) and the notes on them and their
        sequences (`~comparison.queries.readNotes`); `opdbReader`. Its times
        and sequences replace qadb's (which has no sequence). Without it there
        are no notes, and sequences are inferred.
    recentVisits : `int`, optional
        After the first read, each refresh reads the opdb for the latest
        ``recentVisits`` visits, since observers write notes after a visit is
        judged; every visit if None.
    references : mapping, optional
        The reference visits' metrics tables by reference name
        (`pfs.drp.qa.qadb.compareRuns.metricTables`), which `comparison` compares a
        night's visits with. They are read once, when the dashboard starts.
    """

    live = True

    def __init__(
        self,
        read: Callable[[Any], Mapping[str, pd.DataFrame]],
        provenance: Mapping,
        minInterval: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        readOpdb: Callable[[list[int]], OpdbTables] | None = None,
        recentVisits: int | None = 300,
        references: Mapping[str, Sequence[pd.DataFrame]] | None = None,
    ):
        self._read = read
        self.minInterval = minInterval
        self._clock = clock
        self._readOpdb = readOpdb
        self.recentVisits = recentVisits
        self._lock = threading.Lock()
        self._lastRead: float | None = None
        self.since: Any = None
        self._seenAtSince: set[tuple] = set()
        self.updatedAt: datetime | None = None
        self.version = 0
        self._qadbTimes = pd.DataFrame(columns=_TIMES)
        self._opdbTimes = pd.DataFrame(columns=_TIMES)
        self._opdbRead = False
        super().__init__(provenance=provenance)
        self.references = {name: list(tables) for name, tables in (references or {}).items()}
        self.refresh()

    @classmethod
    def fromQadb(
        cls,
        qadb: Any,
        collection: str,
        first: int = FIRST_VISIT,
        last: int = LAST_VISIT,
        minInterval: float = 15.0,
        opdb: Any = None,
        references: Sequence[Any] = (),
    ) -> "QadbMetricsStore":
        """Return the store of one collection's visits ``first``..``last`` in a qadb (read-only).

        With an ``opdb`` (read-only), visits have its times, sequences and notes. Each of
        ``references`` (`pfs.drp.qa.qadb.compareRuns.Reference`) is read now, from its own
        collection or this one; its visits need not lie within ``first``..``last``.

        Raises
        ------
        ValueError
            If two references share a name.
        """
        from pfs.drp.qa.qadb.compareRuns import metricTables

        names = [reference.name for reference in references]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"reference names must differ: {', '.join(repeated)} given more than once")
        return cls(
            lambda since: readQadb(qadb, collection, first, last, since),
            qadbProvenance(
                f"{collection} (qadb, live)",
                builtAt="live",
                references=referenceRecords(references, collection),
            ),
            minInterval,
            readOpdb=None if opdb is None else opdbReader(opdb),
            references={
                r.name: metricTables(qadb, r.collection or collection, r.first, r.last) for r in references
            },
        )

    @classmethod
    def fromSnapshot(cls, directory: Path | str) -> "QadbMetricsStore":
        """Return the store of a qadb snapshot (`qadbSource.writeSnapshot`), read once; it never refreshes.

        The verdicts and the limits beside each value are the snapshot's: the thresholds its images
        cite. A snapshot written before those were read back shows no limits. One written with an
        opdb holds its ``opdbVisitTimes`` and ``notes`` too, and one written with references their
        metrics (`qadbSource.REFERENCE_TABLES`).
        """
        tables, provenance = readSnapshot(directory)
        references = referencesFromSnapshot(
            tables, [record["name"] for record in provenance.get("references") or []]
        )
        none = {name: frame.iloc[:0] for name, frame in tables.items()}
        readOpdb = None
        if "opdbVisitTimes" in tables:
            readOpdb = lambda visits: (tables["opdbVisitTimes"], tables["notes"])  # noqa: E731
        store = cls(
            lambda since: tables if since is None else none,
            provenance,
            0,
            readOpdb=readOpdb,
            recentVisits=None,
            references=references,
        )
        store.live = False
        return store

    @property
    def hasOpdb(self) -> bool:
        return self._readOpdb is not None

    def _times(self) -> pd.DataFrame:
        """Return each visit's time and sequence: the opdb's where it has the visit, else qadb's."""
        qadbOnly = self._qadbTimes[~self._qadbTimes["pfs_visit_id"].isin(self._opdbTimes["pfs_visit_id"])]
        return pd.concat([qadbOnly, self._opdbTimes], ignore_index=True)

    def refresh(self) -> bool:
        """Read the images loaded since the last read and replace their rows; re-read recent opdb notes.

        Returns
        -------
        `bool`
            Whether the table or the notes changed. False within ``minInterval`` of the last read.
        """
        with self._lock:
            now = self._clock()
            if self._lastRead is not None and now - self._lastRead < self.minInterval:
                return False
            self._lastRead = now
            changed = self._takeLoads(self._read(self.since))
            self.updatedAt = datetime.now(HST)
            if self._readOpdb is not None and len(self.table):
                try:
                    changed = self._takeOpdb() or changed
                except Exception:
                    # Show the images just read; their sequences, times and notes wait for the next refresh.
                    _log.exception("Reading the opdb failed; keeping its last answer")
            if changed:
                self.table = withSequences(self.table, self._times())
                self.version += 1
                self._register()
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
        new = metricsTableFromQadb(tables)
        files = tables.get("thresholdsFiles")
        if files is not None and len(files):
            files = pd.concat([self.thresholdsFiles, files], ignore_index=True)
            self.thresholdsFiles = files.drop_duplicates(THRESHOLDS_ID, keep="last").sort_values(
                THRESHOLDS_ID, ignore_index=True
            )
        # Every image the read holds rows of, in whichever table: an image may have line
        # residuals and no image-quality row.
        judged = pd.MultiIndex.from_frame(
            pd.concat([tables[name][_IMAGE] for name in _RAW], ignore_index=True).drop_duplicates()
        )
        kept = self.table[~pd.MultiIndex.from_frame(self.table[_IMAGE]).isin(judged)]
        for name in _RAW:
            held = self.raw.get(name)
            if held is None or held.empty:
                self.raw[name] = tables[name]
                continue
            others = held[~pd.MultiIndex.from_frame(held[_IMAGE]).isin(judged)]
            parts = [part for part in (others, tables[name]) if len(part)]
            self.raw[name] = pd.concat(parts, ignore_index=True) if parts else held.iloc[:0]
        times = pd.concat([self._qadbTimes, tables["visitTimes"].reindex(columns=_TIMES)], ignore_index=True)
        self._qadbTimes = times.drop_duplicates("pfs_visit_id", keep="last")
        table = pd.concat([kept, new], ignore_index=True)
        self.table = table.sort_values([*_IMAGE, "source"], kind="stable", ignore_index=True)
        stamps = pd.concat([tables[name]["decidedAt"] for name in ("iqQaMetrics", "dmLineResidualSummary")])
        self.since = stamps.max()
        self._seenAtSince = {load for load in loads if load[-1] == self.since}
        return True

    def _takeOpdb(self) -> bool:
        """Read the opdb for every visit (the first time) or the recent ones; return whether anything changed."""
        visits = sorted(int(visit) for visit in self.table["visit"].unique())
        if self._opdbRead and self.recentVisits is not None:
            visits = visits[-self.recentVisits :]
        times, notes = self._readOpdb(visits)
        times = times.reindex(columns=_TIMES)
        times = times[times["pfs_visit_id"].isin(visits)]
        oldTimes = self._opdbTimes[self._opdbTimes["pfs_visit_id"].isin(visits)]
        allTimes = pd.concat([self._opdbTimes[~self._opdbTimes["pfs_visit_id"].isin(visits)], times])
        rows = noteRows(notes, allTimes)
        rows = rows[rows["visit"].isin(visits)]
        oldRows = self.noteTable[self.noteTable["visit"].isin(visits)]
        changed = _rowSet(times) != _rowSet(oldTimes) or _rowSet(rows) != _rowSet(oldRows)
        self._opdbTimes = allTimes.sort_values("pfs_visit_id", ignore_index=True)
        self.noteTable = pd.concat(
            [self.noteTable[~self.noteTable["visit"].isin(visits)], rows], ignore_index=True
        ).sort_values(NOTE_COLUMNS, ignore_index=True)
        self._opdbRead = True
        return changed


def _rowSet(frame: pd.DataFrame) -> set[tuple]:
    """Return a frame's rows as a set, missing values alike, to tell whether a re-read changed anything."""
    return set(frame.astype(object).where(frame.notna(), None).itertuples(index=False, name=None))


def _drpQaVersion() -> str:
    """Return the version of the drp_qa judging with, or ``?`` when it can't be read (no git checkout)."""
    try:
        from pfs.drp.qa.comparison.cli import drpQaVersion

        return drpQaVersion()
    except Exception:
        return "?"
