"""Tests of the dashboard's metrics table and its DuckDB store."""

import pandas as pd
import pytest

from pfs.drp.qa.metrics.gate import STATUS_ORDER
from pfs.qa.dashboard import schema
from pfs.qa.dashboard.schema import NOTE_COLUMNS
from pfs.qa.dashboard.serving import VERDICT, buildMetricsTable, inferSequences, noteRows
from pfs.qa.dashboard.store import MetricsStore, visitTimeline

#: The thresholds the images cite: file 1 judges medFwhm of arm b, WARN at 3, FAIL at 4.
IQ_FILE = 1
#: The line-residual thresholds the images cite.
DM_FILE = 2


@pytest.fixture
def thresholds(makeThresholds):
    return makeThresholds((IQ_FILE, "medFwhm", 3.0, 4.0, {"arm": "b"}))


def _iq() -> pd.DataFrame:
    """Three b-arm arcs of one sequence, then a trace far away: FWHM 2.5, 3.5, 4.5, 2.0, all citing file 1."""
    return pd.DataFrame(
        {
            "visit": [100, 101, 102, 200],
            "arm": ["b"] * 4,
            "spectrograph": [1] * 4,
            "obsType": ["arc", "arc", "arc", "trace"],
            "seqName": ["Arc: Neon"] * 3 + ["Trace"],
            "medFwhm": [2.5, 3.5, 4.5, 2.0],
            "traceOnly": [False] * 4,
            # 101's stored verdict is stale: gated with older thresholds.
            "qaStatus": ["PASS", "PASS", "FAIL", "PASS"],
            "qaReason": ["", "", "medFWHM=4.50px >= fail threshold 4px", ""],
            "calexpRun": ["run/1"] * 4,
            "thresholdsId": pd.array([IQ_FILE] * 4, dtype="Int64"),
        }
    )


def _lines() -> tuple[pd.DataFrame, pd.DataFrame]:
    summary = pd.DataFrame(
        {
            "visit": [100, 101, 102, 200],
            "arm": ["b"] * 4,
            "spectrograph": [1] * 4,
            "qaStatus": ["WARN", "PASS", "PASS", "UNKNOWN"],
            "qaReason": ["calib NeI: |medianDeltaY|=1.2px >= warn threshold 1px", "", "", ""],
            "recommendedAction": ["APPLY_SHIFT", "NOMINAL", "NOMINAL", "INSUFFICIENT_DATA"],
            "thresholdsId": pd.array([DM_FILE] * 4, dtype="Int64"),
        }
    )
    metrics = pd.DataFrame(
        {
            "visit": [100, 100],
            "arm": ["b", "b"],
            "spectrograph": [1, 1],
            "cameraSet": ["brn", "brn"],
            "reference": ["calib", "visit"],
            "description": ["NeI", "NeI"],
            "statusType": ["BOTH", "RESERVED"],
            "metric": ["medianDeltaY", "robustRmsDeltaY"],
            "value": [1.2, 0.05],
            "status": ["WARN", ""],
            "thresholdsId": pd.array([DM_FILE] * 2, dtype="Int64"),
        }
    )
    return summary, metrics


def testStatusOrderMatchesGate():
    """The light schema module copies the gate's order rather than importing scipy with it."""
    assert schema.STATUS_ORDER == STATUS_ORDER


def testInferSequences():
    """A new seqName or a gap of more than three visits starts a sequence; opdb ids win."""
    images = pd.DataFrame({"visit": [1, 2, 3, 10, 11, 12], "seqName": ["A", "A", "B", "B", "B", "B"]})
    inferred = inferSequences(images).set_index("visit")
    keys = inferred["sequenceKey"]
    assert keys[1] == keys[2] != keys[3]
    assert keys[3] != keys[10] == keys[11] == keys[12]
    assert (keys < 0).all()
    assert inferred["timeExpStart"].isna().all()
    # Negative control: grouping by seqName alone would merge 3 and 10.
    bySeqName = images.join(inferred, on="visit").groupby("seqName")["sequenceKey"].nunique()
    assert bySeqName["B"] == 2

    listing = pd.DataFrame(
        {
            "pfs_visit_id": [1, 2],
            "time_exp_start": pd.to_datetime(["2026-03-01 18:00", "2026-03-01 18:05"]),
            "iic_sequence_id": [555, 555],
        }
    )
    withOpdb = inferSequences(images, listing).set_index("visit")
    assert withOpdb.loc[1, "sequenceKey"] == 555
    assert withOpdb.loc[1, "sequenceSource"] == "opdb"
    assert withOpdb.loc[3, "sequenceSource"] == "inferred"
    assert withOpdb.loc[2, "timeExpStart"] == pd.Timestamp("2026-03-01 18:05")


def testBuildMetricsTable(thresholds):
    summary, metrics = _lines()
    table = buildMetricsTable(_iq(), thresholds, summary, metrics)
    assert list(table.columns) == list(schema.METRICS_COLUMNS)
    fwhm = table[(table["source"] == "iqQaMetrics") & (table["metric"] == "medFwhm")].set_index("visit")
    assert fwhm["status"].to_dict() == {100: "PASS", 101: "WARN", 102: "FAIL", 200: "PASS"}
    assert (fwhm["warn"] == 3.0).all() and (fwhm["fail"] == 4.0).all()
    verdicts = table[table["metric"] == VERDICT]
    assert set(verdicts["source"]) == {"iqQaMetrics", "dmLineResidualSummary"}
    assert len(verdicts) == 8
    # The stored verdict is kept, not replaced by the re-judged one.
    stored = verdicts[verdicts["source"] == "iqQaMetrics"].set_index("visit")["status"]
    assert stored[101] == "PASS"
    assert table["obsType"].ne("").all()
    # Negative control: the ids are not rows of the table.
    assert "thresholdsId" not in table.columns


def testLimitsAreEachImagesOwnThresholds(makeThresholds):
    """Each image is judged with the file it cites; one citing none keeps its value and verdict without limits."""
    thresholds = makeThresholds(
        (IQ_FILE, "medFwhm", 3.0, 4.0, {"arm": "b"}), (3, "medFwhm", 4.6, 5.0, {"arm": "b"})
    )
    iq = _iq()
    iq["thresholdsId"] = pd.array([IQ_FILE, IQ_FILE, 3, pd.NA], dtype="Int64")
    table = buildMetricsTable(iq, thresholds)
    fwhm = table[(table["source"] == "iqQaMetrics") & (table["metric"] == "medFwhm")].set_index("visit")
    assert fwhm["status"].to_dict() == {100: "PASS", 101: "WARN", 102: "PASS", 200: ""}
    assert fwhm["warn"].to_dict() == pytest.approx(
        {100: 3.0, 101: 3.0, 102: 4.6, 200: float("nan")}, nan_ok=True
    )
    assert fwhm.loc[200, "value"] == 2.0
    # The stored verdict stands whatever the limits say.
    verdict = table[(table["source"] == "iqQaMetrics") & (table["metric"] == VERDICT)].set_index("visit")
    assert verdict.loc[102, "status"] == "FAIL"
    # Negative control: one file for all would call 102 FAIL.
    same = buildMetricsTable(iq.assign(thresholdsId=pd.array([IQ_FILE] * 4, dtype="Int64")), thresholds)
    sameFwhm = same[(same["source"] == "iqQaMetrics") & (same["metric"] == "medFwhm")].set_index("visit")
    assert sameFwhm.loc[102, "status"] == "FAIL"


def testWithoutThresholdsValuesHaveNoLimits():
    """A read without thresholds (a snapshot older than them, no thresholdsId) keeps every value and verdict."""
    iq = _iq().drop(columns="thresholdsId")
    table = buildMetricsTable(iq)
    fwhm = table[(table["source"] == "iqQaMetrics") & (table["metric"] == "medFwhm")].set_index("visit")
    assert fwhm["value"].to_dict() == {100: 2.5, 101: 3.5, 102: 4.5, 200: 2.0}
    assert fwhm["warn"].isna().all() and (fwhm["status"] == "").all()
    assert (table[table["metric"] == VERDICT]["status"] == _iq()["qaStatus"].to_numpy()).all()


def testAnEntryOnAMetricNotStoredIsSkipped(makeThresholds):
    """The gate refuses a table naming an unmeasured metric; the dashboard shows what qadb holds."""
    thresholds = makeThresholds(
        (IQ_FILE, "medFwhm", 3.0, 4.0, {"arm": "b"}), (IQ_FILE, "notStored", 1.0, 2.0, {"arm": "b"})
    )
    table = buildMetricsTable(_iq(), thresholds)
    fwhm = table[(table["source"] == "iqQaMetrics") & (table["metric"] == "medFwhm")]
    assert (fwhm["warn"] == 3.0).all()
    assert "notStored" not in set(table["metric"])


def testStore(thresholds):
    summary, metrics = _lines()
    store = MetricsStore(buildMetricsTable(_iq(), thresholds, summary, metrics), {"collection": "c"})
    assert store.provenance == {"collection": "c"}
    assert store.thresholdsFiles.empty
    images = store.images().set_index("visit")
    # Worst of the sources: 100 is PASS in iqQaMetrics but WARN in dmLineResidualSummary.
    assert images["status"].to_dict() == {100: "WARN", 101: "PASS", 102: "FAIL", 200: "PASS"}
    # Negative control: iqQaMetrics alone would call 100 PASS.
    assert images.loc[100, "iqStatus"] == "PASS"
    assert store.images(101, 102)["visit"].tolist() == [101, 102]
    assert store.visits()["visit"].tolist() == [100, 101, 102, 200]

    calib = store.trend("dmLineResidualMetrics", "medianDeltaY", reference="calib", statusType="BOTH")
    assert calib["value"].tolist() == [1.2]
    assert store.trend("dmLineResidualMetrics", "medianDeltaY", reference="visit").empty
    assert store.trend("iqQaMetrics", "medFwhm", 101, 200)["visit"].tolist() == [101, 102, 200]


def testLineThresholdsAreRecorded(makeThresholds):
    """The task stores line-residual verdicts without thresholds; the table records the cited file's."""
    thresholds = makeThresholds(
        (IQ_FILE, "medFwhm", 3.0, 4.0, {"arm": "b"}),
        (DM_FILE, "medianDeltaY", 1.0, 2.0, {"reference": "calib"}),
        (DM_FILE, "robustRmsDeltaY", 0.1, 0.2, {"reference": "visit", "statusType": "RESERVED"}),
    )
    summary, metrics = _lines()
    table = buildMetricsTable(_iq(), thresholds, summary, metrics)
    lines = table[(table["source"] == "dmLineResidualMetrics")].set_index("reference")
    assert lines.loc["calib", ["warn", "fail"]].tolist() == [1.0, 2.0]
    assert lines.loc["visit", ["warn", "fail"]].tolist() == [0.1, 0.2]
    # The stored verdict is kept as it is.
    assert lines.loc["calib", "status"] == "WARN" and lines.loc["visit", "status"] == ""
    # Negative control: a file the line rows do not cite gives them no thresholds.
    other = buildMetricsTable(
        _iq(), thresholds, summary, metrics.assign(thresholdsId=pd.array([9, 9], dtype="Int64"))
    )
    assert other.loc[other["source"] == "dmLineResidualMetrics", ["warn", "fail"]].isna().all().all()


def testLineThresholdsMatchTheSummarysCameraSetAndObsType(makeThresholds):
    """Qadb's metric rows lack cameraSet and obsType, which file entries name: they come from the summary."""
    summary, metrics = _lines()
    summary = summary.assign(cameraSet="brn", obsType="arc")
    metrics = metrics.drop(columns="cameraSet")
    thresholds = makeThresholds(
        (
            DM_FILE,
            "medianDeltaY",
            0.5,
            1.5,
            {"reference": "calib", "statusType": "BOTH", "cameraSet": "brn", "obsType": "arc"},
        ),
        absolute=True,
    )
    table = buildMetricsTable(_iq(), thresholds, summary, metrics)
    calib = table[(table["source"] == "dmLineResidualMetrics") & (table["reference"] == "calib")]
    assert calib[["warn", "fail"]].values.tolist() == [[0.5, 1.5]]
    # Negative control: without the summary's columns the entry matches nothing.
    bare = buildMetricsTable(_iq(), thresholds, summary.drop(columns=["cameraSet", "obsType"]), metrics)
    assert bare.loc[bare["source"] == "dmLineResidualMetrics", ["warn", "fail"]].isna().all().all()


def testNoteRows():
    """A visit's note, a camera's annotation, and a sequence's note on each of its visits."""
    notes = pd.DataFrame(
        {
            "pfs_visit_id": pd.array([100, 101, pd.NA], dtype="Int64"),
            "iic_sequence_id": pd.array([pd.NA, pd.NA, 7], dtype="Int64"),
            "camera": [None, "r2", None],
            "data_flag": pd.array([pd.NA, 1, pd.NA], dtype="Int64"),
            "note": ["Cloudy ", "bad amp", "SM1 lamp off"],
            "source": ["obslog", "sps_annotation", "obslog_sequence"],
        }
    )
    listing = pd.DataFrame(
        {"pfs_visit_id": [100, 101, 102], "iic_sequence_id": pd.array([7, 7, 8], dtype="Int64")}
    )
    rows = noteRows(notes, listing).sort_values(["visit", "note"]).reset_index(drop=True)
    assert rows[["visit", "arm", "spectrograph", "note"]].values.tolist() == [
        [100, "", 0, "Cloudy"],
        [100, "", 0, "SM1 lamp off"],
        [101, "", 0, "SM1 lamp off"],
        [101, "r", 2, "bad amp"],
    ]
    assert list(rows.columns) == NOTE_COLUMNS
    # Negative control: without the listing, a sequence's note reaches no visit.
    assert "SM1 lamp off" not in set(noteRows(notes)["note"])


def testStoreNotesKeepImagesClean(thresholds):
    notes = pd.DataFrame(
        {
            "pfs_visit_id": pd.array([100], dtype="Int64"),
            "iic_sequence_id": pd.array([pd.NA], dtype="Int64"),
            "camera": [None],
            "data_flag": pd.array([pd.NA], dtype="Int64"),
            "note": ["Cloudy"],
            "source": ["obslog"],
        }
    )
    summary, metrics = _lines()
    store = MetricsStore(buildMetricsTable(_iq(), thresholds, summary, metrics), notes=noteRows(notes))
    assert store.notes()[["visit", "note"]].values.tolist() == [[100, "Cloudy"]]
    # The note is not an image: the rollup still has one row per image.
    assert len(store.images()) == 4


def testVisitTimelineNights():
    """A visit after midnight belongs to the night that began the evening before."""
    visits = pd.DataFrame(
        {
            "visit": [10, 11, 12, 13],
            "timeExpStart": pd.to_datetime(
                ["2026-03-09 19:00", "2026-03-10 06:13", "2026-03-10 19:30", None]
            ),
        }
    )
    timeline = visitTimeline(visits).set_index("visit")
    assert timeline["night"].to_dict() == {10: "2026-03-09", 11: "2026-03-09", 12: "2026-03-10", 13: ""}
    assert timeline["position"].tolist() == [0, 1, 2, 3]
    assert timeline.loc[11, "clock"] == "06:13"
    # Negative control: the calendar date would split the first night at midnight.
    assert timeline.loc[11, "time"].strftime("%Y-%m-%d") != timeline.loc[11, "night"]


def testVisitsNotJudgedAreLeftOut(thresholds):
    """A line-residual row on a visit with no iqQaMetrics row has no sequence: it is dropped, not a crash."""
    summary, metrics = _lines()
    summary = pd.concat([summary, summary.iloc[[0]].assign(visit=999)], ignore_index=True)
    table = buildMetricsTable(_iq(), thresholds, summary, metrics)
    assert set(table["visit"]) == {100, 101, 102, 200}
    # Negative control: the judged visits' line-residual verdicts are all kept.
    assert (table["source"] == "dmLineResidualSummary").sum() == 4
