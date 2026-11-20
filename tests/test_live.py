"""Tests for the qadb store (`pfs.qa.dashboard.live.QadbStore`), with a stand-in for qadb."""

import pandas as pd
import pytest

from pfs.qa.dashboard.live import QadbStore, qadbProvenance
from pfs.qa.dashboard.qadbSource import readSnapshot, writeSnapshot


def image(visit, arm, status, medFwhm, decidedAt):
    """Return an ``iqQaMetrics`` row as the reader gives it."""
    return {
        "visit": visit,
        "arm": arm,
        "spectrograph": 1,
        "obsType": "arc",
        "seqName": "Arc: Neon",
        "illumination": "full",
        "medFwhm": medFwhm,
        "qaStatus": status,
        "qaReason": "" if status == "PASS" else "medFwhm",
        "decidedAt": decidedAt,
    }


def tables(images):
    """Return what `pfs.drp.qa.qadb.reader.readQadb` returns for some images: no line residuals."""
    iq = pd.DataFrame(images)
    visits = sorted(set(iq["visit"])) if len(iq) else []
    return {
        "iqQaMetrics": iq,
        "dmLineResidualSummary": pd.DataFrame(columns=["visit", "arm", "spectrograph", "decidedAt"]),
        "dmLineResidualMetrics": pd.DataFrame(columns=["visit", "arm", "spectrograph", "decidedAt"]),
        "visitTimes": pd.DataFrame(
            {
                "pfs_visit_id": visits,
                "time_exp_start": [
                    pd.Timestamp("2026-11-01 19:00") + pd.Timedelta(minutes=v % 100) for v in visits
                ],
                "iic_sequence_id": pd.array([pd.NA] * len(visits), dtype="Int64"),
            }
        ),
    }


class FakeQadb:
    """Answers ``read(since)`` from loads, each a list of images at one time."""

    def __init__(self):
        self.loads = []
        self.calls = []

    def read(self, since):
        self.calls.append(since)
        # As qadb holds them: each image's latest load.
        latest = {(row["visit"], row["arm"], row["spectrograph"]): row for load in self.loads for row in load}
        return tables([row for row in latest.values() if since is None or row["decidedAt"] >= since])


@pytest.fixture
def clock():
    now = [0.0]
    tick = lambda: now[0]  # noqa: E731
    tick.advance = lambda seconds: now.__setitem__(0, now[0] + seconds)
    return tick


def _statuses(store):
    iq = store.raw["iqQaMetrics"]
    return dict(zip(zip(iq["visit"], iq["arm"], strict=True), iq["qaStatus"], strict=True))


def testRefreshReplacesRejudgedImagesAndAddsNewOnes(clock):
    qadb = FakeQadb()
    qadb.loads.append([image(1001, "b", "PASS", 2.5, "t1"), image(1001, "r", "PASS", 2.7, "t1")])
    store = QadbStore(qadb.read, minInterval=10, clock=clock)
    assert store.version == 1 and store.since == "t1"
    assert _statuses(store) == {(1001, "b"): "PASS", (1001, "r"): "PASS"}

    # Within minInterval nothing is read.
    qadb.loads.append([image(1002, "b", "PASS", 2.5, "t2")])
    assert not store.refresh() and len(qadb.calls) == 1

    clock.advance(10)
    qadb.loads.append([image(1001, "b", "FAIL", 9.0, "t3")])
    assert store.refresh()
    assert qadb.calls[-1] == "t1"
    # (1001, b) is replaced, not duplicated; (1001, r) is kept.
    assert _statuses(store) == {(1001, "b"): "FAIL", (1001, "r"): "PASS", (1002, "b"): "PASS"}
    assert len(store.raw["iqQaMetrics"]) == 3
    assert store.since == "t3" and store.version == 2


def testTimesAreQadbsForEveryVisitRead(clock):
    qadb = FakeQadb()
    qadb.loads.append([image(1001, "b", "PASS", 2.5, "t1")])
    store = QadbStore(qadb.read, minInterval=0, clock=clock)
    qadb.loads.append([image(1002, "b", "PASS", 2.5, "t2"), image(1002, "r", "PASS", 2.5, "t2")])
    store.refresh()
    times = store.times
    assert list(times.columns) == ["visit", "time"] and times["visit"].tolist() == [1001, 1002]
    assert times["time"].tolist() == [pd.Timestamp("2026-11-01 19:01"), pd.Timestamp("2026-11-01 19:02")]


def testEmptyUntilTheFirstLoad(clock):
    qadb = FakeQadb()
    store = QadbStore(qadb.read, minInterval=0, clock=clock)
    assert store.version == 0 and store.raw == {} and store.times.empty and store.since is None
    assert not store.refresh()
    qadb.loads.append([image(1001, "b", "WARN", 2.9, "t1")])
    assert store.refresh() and _statuses(store) == {(1001, "b"): "WARN"}


def testUnchangedRefreshIsNotNew(clock):
    """``since`` is inclusive, so the last load comes back on every read: that is not new data."""
    qadb = FakeQadb()
    qadb.loads.append([image(1001, "b", "PASS", 2.5, "t1")])
    store = QadbStore(qadb.read, minInterval=0, clock=clock)
    assert not store.refresh() and not store.refresh()
    assert store.version == 1 and qadb.calls[-1] == "t1"

    # Negative control: an image loaded in the same instant, read only now, is new.
    qadb.loads.append([image(1001, "r", "WARN", 2.9, "t1")])
    assert store.refresh() and store.version == 2
    assert sorted(store.raw["iqQaMetrics"]["arm"]) == ["b", "r"]
    assert not store.refresh()


def testASnapshotHoldsWhatQadbHeldAndNeverChanges(tmp_path, clock):
    qadb = FakeQadb()
    qadb.loads.append([image(1001, "b", "PASS", 2.5, "t1"), image(1002, "r", "FAIL", 9.0, "t1")])
    liveStore = QadbStore(qadb.read, minInterval=0, clock=clock)
    provenance = qadbProvenance("c (qadb snapshot)", builtAt="2026-10-28T10:00:00-10:00")
    read, readProvenance = readSnapshot(writeSnapshot(qadb.read(None), tmp_path / "snap", provenance))
    store = QadbStore.fromTables(read)

    assert readProvenance == provenance
    assert _statuses(store) == _statuses(liveStore) and len(store.times) == 2
    assert store.version == 1 and not store.refresh() and store.version == 1
    # Negative control: the store reading qadb takes a later load.
    qadb.loads.append([image(1003, "b", "PASS", 2.5, "t2")])
    assert liveStore.refresh() and liveStore.version == 2


def testSnapshotHoldsOnlyItsOwnRead(tmp_path):
    """Writing a snapshot over an older one leaves none of the older one's tables behind."""
    qadb = FakeQadb()
    qadb.loads.append([image(1001, "b", "PASS", 2.5, "t1")])
    older = dict(qadb.read(None))
    older["visitInfo"] = pd.DataFrame({"visit": [1001]})
    directory = writeSnapshot(older, tmp_path / "snap", {})
    assert (directory / "visitInfo.parquet").exists()

    writeSnapshot(qadb.read(None), directory, {})
    read, _ = readSnapshot(directory)
    assert "visitInfo" not in read
    # Negative control: the qadb tables of the new read are there.
    assert len(read["iqQaMetrics"]) == 1


def testAnImageWithOnlyLineResidualsIsReplacedOnReload(clock):
    """A reload of an image that has line residuals and no image-quality row replaces its rows."""

    def lines(visit, decidedAt, status):
        return pd.DataFrame(
            {
                "visit": [visit],
                "arm": ["b"],
                "spectrograph": [1],
                "obsType": ["arc"],
                "qaStatus": [status],
                "qaReason": [""],
                "recommendedAction": [""],
                "decidedAt": [decidedAt],
            }
        )

    def read(since):
        first = since is None
        held = tables([image(1001, "b", "PASS", 2.5, "t1") if first else image(1003, "b", "PASS", 2.5, "t2")])
        held["dmLineResidualSummary"] = lines(1002, "t1", "WARN") if first else lines(1002, "t2", "PASS")
        return held

    store = QadbStore(read, minInterval=0, clock=clock)
    assert store.raw["dmLineResidualSummary"]["qaStatus"].tolist() == ["WARN"]
    clock.advance(1)
    assert store.refresh()
    summary = store.raw["dmLineResidualSummary"]
    assert summary["visit"].tolist() == [1002] and summary["qaStatus"].tolist() == ["PASS"]
    # Negative control: the images of the first read that the second does not hold stay.
    assert store.raw["iqQaMetrics"]["visit"].tolist() == [1001, 1003]
