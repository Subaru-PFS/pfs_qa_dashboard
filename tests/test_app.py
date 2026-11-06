"""Tests of the dashboard app (`pfs.qa.dashboard.app`) over a live store fed by a stand-in for qadb."""

import pandas as pd

from pfs.qa.dashboard.app import Dashboard, verdictCounts
from pfs.qa.dashboard.live import QadbMetricsStore
from pfs.qa.dashboard.serving import taskThresholds


def _image(visit, obsType, decidedAt, status="PASS", reason=""):
    return {
        "visit": visit,
        "arm": "b",
        "spectrograph": 1,
        "obsType": obsType,
        "seqName": obsType,
        "illumination": "full",
        "traceOnly": False,
        **dict.fromkeys(
            ("medFwhm", "medDxCenter", "dxCenterRms", "pctFlagged", "linesPerFiber", "nLines"), float("nan")
        ),
        "qaStatus": status,
        "qaReason": reason,
        "calexpRun": "",
        "decidedAt": decidedAt,
    }


def _liveStore(loads):
    """Return a store reading ``loads`` (lists of images), each image's latest load as qadb holds it."""

    def read(since):
        latest = {(row["visit"], row["arm"]): row for load in loads for row in load}
        iq = pd.DataFrame([row for row in latest.values() if since is None or row["decidedAt"] >= since])
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

    return QadbMetricsStore(read, taskThresholds()[0], {}, minInterval=0)


def testVerdictCountsEveryStatusWorstFirst():
    images = pd.DataFrame({"status": ["PASS", "PASS", "WARN", "FAIL", "PASS"]})
    counts = verdictCounts(images)
    assert list(counts.index) == ["FAIL", "WARN", "PASS", "UNKNOWN"]
    assert counts.to_dict() == {"FAIL": 1, "WARN": 1, "PASS": 3, "UNKNOWN": 0}
    # Negative control: an empty table counts nothing, but still names every status.
    assert verdictCounts(images.iloc[:0]).to_dict() == {"FAIL": 0, "WARN": 0, "PASS": 0, "UNKNOWN": 0}


def testSummaryFollowsLiveVisits():
    """The summary counts the visits loaded after it was built, and names the latest with its HST time."""
    loads = [[_image(1001, "bias", "t1")]]
    dashboard = Dashboard(_liveStore(loads))
    before = dashboard.summaryHtml()
    assert "1 PASS images" not in before  # the count is the tile's value, the label names the status
    assert (
        ">1<" in before and "PASS images" in before and "latest visit ✓ PASS, 2026-11-01 19:01 HST" in before
    )

    loads.append([_image(1002, "arc", "t2", "FAIL", "nLines<100")])
    dashboard.refresh()
    after = dashboard.summaryHtml()
    assert dashboard.dataVersion == 1
    assert "FAIL images" in after and "latest visit ✕ FAIL, 2026-11-01 19:02 HST" in after
    assert "visits, 1001–1002" in after
    # Negative control: the earlier summary did not know the second visit.
    assert "1002" not in before


def testRefreshWithoutNewLoadsChangesNothing():
    dashboard = Dashboard(_liveStore([[_image(1001, "bias", "t1")]]))
    dashboard.refresh()
    assert dashboard.dataVersion == 0


def testProvenanceAndSummaryAreEscaped():
    """The provenance and the summary show data from qadb and the thresholds file as text, not markup."""
    store = _liveStore([[_image(1001, "<i>arc</i>", "t1")]])
    store.provenance = {"collection": "<b>c</b>", "thresholdLayers": ["a & b"]}
    dashboard = Dashboard(store)
    bar = dashboard.provenanceBar().object
    assert "&lt;b&gt;c&lt;/b&gt;" in bar and "a &amp; b" in bar
    # Negative control: the raw markup is not there to be rendered.
    assert "<b>c</b>" not in bar


def testEmptyStoreSaysSo():
    dashboard = Dashboard(_liveStore([]))
    assert "No visit in qadb yet" in dashboard.summaryHtml()
