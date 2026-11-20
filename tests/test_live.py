"""Tests for the qadb store (`pfs.qa.dashboard.live.QadbStore`), with a stand-in for qadb."""

import pandas as pd

from pfs.drp.qa.qadb.reader import GATE_COLUMNS
from pfs.qa.dashboard import live
from pfs.qa.dashboard.live import GATE, QadbStore, qadbProvenance, readCollection
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


def _statuses(store):
    iq = store.raw["iqQaMetrics"]
    return dict(zip(zip(iq["visit"], iq["arm"], strict=True), iq["qaStatus"], strict=True))


def testRefreshReplacesRejudgedImagesAndAddsNewOnes():
    qadb = FakeQadb()
    qadb.loads.append([image(1001, "b", "PASS", 2.5, "t1"), image(1001, "r", "PASS", 2.7, "t1")])
    store = QadbStore(qadb.read)
    assert store.version == 1 and store.since == "t1"
    assert _statuses(store) == {(1001, "b"): "PASS", (1001, "r"): "PASS"}

    qadb.loads.append([image(1002, "b", "PASS", 2.5, "t2")])
    qadb.loads.append([image(1001, "b", "FAIL", 9.0, "t3")])
    assert store.refresh()
    assert qadb.calls[-1] == "t1"
    # (1001, b) is replaced, not duplicated; (1001, r) is kept.
    assert _statuses(store) == {(1001, "b"): "FAIL", (1001, "r"): "PASS", (1002, "b"): "PASS"}
    assert len(store.raw["iqQaMetrics"]) == 3
    assert store.since == "t3" and store.version == 2


def testTimesAreQadbsForEveryVisitRead():
    qadb = FakeQadb()
    qadb.loads.append([image(1001, "b", "PASS", 2.5, "t1")])
    store = QadbStore(qadb.read)
    qadb.loads.append([image(1002, "b", "PASS", 2.5, "t2"), image(1002, "r", "PASS", 2.5, "t2")])
    store.refresh()
    times = store.times
    assert list(times.columns) == ["visit", "time"] and times["visit"].tolist() == [1001, 1002]
    assert times["time"].tolist() == [pd.Timestamp("2026-11-01 19:01"), pd.Timestamp("2026-11-01 19:02")]


def testEmptyUntilTheFirstLoad():
    qadb = FakeQadb()
    store = QadbStore(qadb.read)
    assert store.version == 0 and store.raw == {} and store.times.empty and store.since is None
    assert not store.refresh()
    qadb.loads.append([image(1001, "b", "WARN", 2.9, "t1")])
    assert store.refresh() and _statuses(store) == {(1001, "b"): "WARN"}


def testUnchangedRefreshIsNotNew():
    """``since`` is inclusive, so the last load comes back on every read: that is not new data."""
    qadb = FakeQadb()
    qadb.loads.append([image(1001, "b", "PASS", 2.5, "t1")])
    store = QadbStore(qadb.read)
    assert not store.refresh() and not store.refresh()
    assert store.version == 1 and qadb.calls[-1] == "t1"

    # Negative control: an image loaded in the same instant, read only now, is new.
    qadb.loads.append([image(1001, "r", "WARN", 2.9, "t1")])
    assert store.refresh() and store.version == 2
    assert sorted(store.raw["iqQaMetrics"]["arm"]) == ["b", "r"]
    assert not store.refresh()


def testASnapshotHoldsWhatQadbHeldAndNeverChanges(tmp_path):
    qadb = FakeQadb()
    qadb.loads.append([image(1001, "b", "PASS", 2.5, "t1"), image(1002, "r", "FAIL", 9.0, "t1")])
    liveStore = QadbStore(qadb.read)
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


def testAnImageWithOnlyLineResidualsIsReplacedOnReload():
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

    store = QadbStore(read)
    assert store.raw["dmLineResidualSummary"]["qaStatus"].tolist() == ["WARN"]
    assert store.refresh()
    summary = store.raw["dmLineResidualSummary"]
    assert summary["visit"].tolist() == [1002] and summary["qaStatus"].tolist() == ["PASS"]
    # Negative control: the images of the first read that the second does not hold stay.
    assert store.raw["iqQaMetrics"]["visit"].tolist() == [1001, 1003]


def testAReadOfOnlyLineResidualsIsNew():
    """An image with line residuals and no image-quality row is taken when it is all a read brings."""

    def lines(visit, decidedAt):
        return pd.DataFrame(
            {
                "visit": [visit],
                "arm": ["b"],
                "spectrograph": [1],
                "obsType": ["allsky"],
                "qaStatus": ["WARN"],
                "qaReason": [""],
                "decidedAt": [decidedAt],
            }
        )

    def read(since):
        if since is None:
            return tables([image(1001, "b", "PASS", 2.5, "t1")])
        held = tables([])
        held["iqQaMetrics"] = pd.DataFrame(columns=["visit", "arm", "spectrograph", "decidedAt"])
        held["dmLineResidualSummary"] = lines(1002, "t2")
        return held

    store = QadbStore(read)
    assert store.version == 1 and store.since == "t1"
    assert store.refresh() and store.version == 2 and store.since == "t2"
    assert store.raw["dmLineResidualSummary"]["visit"].tolist() == [1002]
    assert store.raw["iqQaMetrics"]["visit"].tolist() == [1001]
    # Negative control: the same load read again is not new.
    assert not store.refresh() and store.version == 2


def verdict(visit, arm, status, decidedAt):
    """Return a gate row as `pfs.drp.qa.qadb.reader.readGate` gives it."""
    return {"visit": visit, "arm": arm, "spectrograph": 1, "qaStatus": status, "decidedAt": decidedAt}


def testTheGateIsReadOverTheVisitsTheNumbersRead(monkeypatch):
    """A refresh reads the gate rows of the new images' visits only; the first read, the whole range."""
    asked = []

    def readGate(qadb, collection, first, last):
        asked.append((first, last))
        return pd.DataFrame([verdict(1001, "b", "PASS", "t1")])

    numbers = {"t1": tables([image(1002, "b", "PASS", 2.5, "t1"), image(1005, "r", "PASS", 2.5, "t1")])}
    numbers["t2"] = tables([])
    numbers["t2"]["iqQaMetrics"] = pd.DataFrame(columns=["visit", "arm", "spectrograph", "decidedAt"])
    monkeypatch.setattr(live, "readGate", readGate)
    monkeypatch.setattr(live, "readQadb", lambda qadb, collection, first, last, since: numbers[since or "t1"])

    assert readCollection(None, "c", 1000, 2000)[GATE]["visit"].tolist() == [1001]
    readCollection(None, "c", 1000, 2000, since="t1")
    assert asked == [(1000, 2000), (1002, 1005)]
    # A read that brings no image asks the gate nothing.
    empty = readCollection(None, "c", 1000, 2000, since="t2")[GATE]
    assert len(asked) == 2 and empty.empty and list(empty.columns) == list(GATE_COLUMNS)
    # Negative control: the numbers' own tables come back as the reader gave them.
    assert readCollection(None, "c", 1000, 2000)["iqQaMetrics"]["visit"].tolist() == [1002, 1005]


def testGateRowsReplaceTheirOwnImages():
    """A gate row read again replaces the image's earlier one, and leaves the other tables' rows alone."""

    def read(since):
        if since is None:
            held = tables([image(1001, "b", "PASS", 2.5, "t1"), image(1002, "b", "PASS", 2.5, "t1")])
            held[GATE] = pd.DataFrame([verdict(1001, "b", "PASS", "t1"), verdict(1002, "b", "PASS", "t1")])
            return held
        # Visit 1002 judged again; the gate read covers 1001 to 1002, and so brings 1001's row too.
        held = tables([image(1002, "b", "FAIL", 2.5, "t2")])
        held[GATE] = pd.DataFrame([verdict(1001, "b", "PASS", "t1"), verdict(1002, "b", "FAIL", "t2")])
        return held

    store = QadbStore(read)
    assert store.refresh()
    gate = store.raw[GATE].sort_values("visit")
    assert gate["visit"].tolist() == [1001, 1002] and gate["qaStatus"].tolist() == ["PASS", "FAIL"]
    # Negative control: 1001's numbers, which the second read did not hold, are kept.
    assert sorted(store.raw["iqQaMetrics"]["visit"]) == [1001, 1002]
