"""Tests for the AG sky-conditions night-page tile and modal (``agConditions``)."""

from datetime import date

import numpy as np
import pandas as pd

from pfs.qa.dashboard.agConditions import (
    SEEING,
    TRANSPARENCY,
    _bins,
    _minutes,
    _runs,
    _x,
    _y,
    conditions,
    readSeeing,
    readTransparency,
)


class _FakeQadb:
    """A stand-in `QaDB`: records the query and returns a fixed frame from ``query_dataframe``."""

    def __init__(self, frame):
        self._frame = frame
        self.params = None

    def query_dataframe(self, sql, params=None):
        self.sql, self.params = sql, params
        return self._frame


def _rawRows(n, start="2026-11-04 20:00", visit=1000):
    """Return a read as the reader's SQL hands it back: the metric column is aliased to ``value``."""
    return pd.DataFrame(
        {
            "pfs_visit_id": visit,
            "agc_exposure_id": range(1, n + 1),
            "taken_at": pd.date_range(start, periods=n, freq="1min"),
            "value": np.linspace(0.70, 0.85, n),
        }
    )


def _exposures(times, values, visit=1000):
    """Return a reader-shaped frame (``visit``, ``agcExposureId``, ``time``, ``value``)."""
    times = pd.to_datetime(times)
    return pd.DataFrame(
        {
            "visit": visit,
            "agcExposureId": range(len(times)),
            "time": times,
            "value": np.asarray(values, float),
        }
    )


def testReadTransparencyRenamesAndBindsTheRange():
    qadb = _FakeQadb(_rawRows(3))
    out = readTransparency(qadb, 1000, 1009)
    assert list(out.columns) == ["visit", "agcExposureId", "time", "value"]
    assert len(out) == 3 and out["visit"].iloc[0] == 1000
    assert pd.api.types.is_datetime64_any_dtype(out["time"])
    # The visit range is bound, not formatted into the SQL.
    assert qadb.params == {"first": 1000, "last": 1009}
    assert "transparency_agc_exposure" in qadb.sql


def testReadSeeingFiltersNonPositive():
    qadb = _FakeQadb(_rawRows(3))
    out = readSeeing(qadb, 1, 2)
    assert list(out.columns) == ["visit", "agcExposureId", "time", "value"]
    # Seeing drops non-physical (<= 0) rows; transparency does not filter on sign.
    assert "seeing_agc_exposure" in qadb.sql and "seeing_median > 0" in qadb.sql


def testReadEmptyKeepsTheColumns():
    empty = pd.DataFrame(columns=["pfs_visit_id", "agc_exposure_id", "taken_at", "transparency_median"])
    out = readTransparency(_FakeQadb(empty), 1, 2)
    assert out.empty and list(out.columns) == ["visit", "agcExposureId", "time", "value"]


def testConditionsLeadsWithSeeingAndTheModalHasBothCharts():
    times = pd.date_range("2026-11-04 20:00", periods=120, freq="1min")
    seeing = _exposures(times, np.full(120, 0.60))
    transparency = _exposures(times, np.full(120, 0.78))
    tile, modal = conditions(transparency, seeing, "2026-11-04")
    # The tile is a button leading with seeing (median + unit), carrying transparency, with a sparkline.
    assert "<button" in tile and "aria-controls='conditions'" in tile
    assert "0.60″" in tile and "seeing" in tile and 'class="spark"' in tile
    assert "0.78 transparency" in tile  # the secondary line
    # The modal has an overall heading and a section (chart) for each metric.
    assert "id='conditions'" in modal and "AG sky conditions tonight" in modal
    assert "<h3>Seeing</h3>" in modal and "<h3>Sky transparency</h3>" in modal
    assert modal.count("<svg") == 2 and modal.count("<polyline") >= 2
    assert ">18:00<" in modal and ">06:00<" in modal


def testTransparencyOnlyLeadsWithTransparencyAndHasOneChart():
    times = pd.date_range("2026-11-04 20:00", periods=60, freq="1min")
    tile, modal = conditions(_exposures(times, np.full(60, 0.77)), None, "2026-11-04")
    assert "0.77" in tile and "transparency" in tile and "″" not in tile  # no seeing unit
    assert "<h3>Sky transparency</h3>" in modal and "<h3>Seeing</h3>" not in modal
    assert modal.count("<svg") == 1


def testSeeingOnlyLeadsWithSeeing():
    times = pd.date_range("2026-11-04 20:00", periods=60, freq="1min")
    tile, modal = conditions(None, _exposures(times, np.full(60, 0.9)), "2026-11-04")
    assert "0.90″" in tile and "<h3>Seeing</h3>" in modal and "<h3>Sky transparency</h3>" not in modal


def testNeitherMetricGivesNothing():
    good = _exposures(["2026-11-04 20:00", "2026-11-04 20:05"], [0.8, 0.8])
    assert conditions(None, None, "2026-11-04") == ("", "")
    assert conditions(good.iloc[:0], good.iloc[:0], "2026-11-04") == ("", "")
    assert conditions(good, good, "") == ("", "")
    assert conditions(good, good, "not-a-date") == ("", "")


def testExposuresOutsideTheNightWindowAreCountedNotDrawn():
    # Two in the night (20:00), one in daytime (10:00, before 18:00) that the window excludes.
    df = _exposures(["2026-11-04 20:00", "2026-11-04 20:05", "2026-11-04 10:00"], [0.8, 0.8, 0.8])
    _, modal = conditions(df, None, "2026-11-04")
    assert "2 AG exposures" in modal  # only the two inside
    assert "1 outside 18:00" in modal


def testMinutesCountFromTheEveningMidnight():
    # 20:00 on the evening date is 1200 min; 02:00 the next morning is (24+2)*60 = 1560.
    times = pd.to_datetime(pd.Series(["2026-11-04 20:00", "2026-11-05 02:00"]))
    minutes = _minutes(times, date(2026, 11, 4))
    assert list(minutes) == [1200.0, 1560.0]


def testRunsBreakOnLongGaps():
    # Two blocks of bins, 30 min apart (> the 16-min break), give two runs.
    minutes = np.array([1100.0, 1105.0, 1110.0, 1140.0, 1145.0])
    runs = _runs(_bins(minutes, np.full(minutes.shape, 0.8)))
    assert len(runs) == 2
    # Negative control: within one block the bins stay a single run.
    close = np.array([1100.0, 1105.0, 1110.0])
    assert len(_runs(_bins(close, np.full(close.shape, 0.8)))) == 1


def testBinsSummariseEachInterval():
    minutes = np.array([1200.0, 1201.0, 1202.0, 1203.0])
    (b,) = _bins(minutes, np.array([0.6, 0.7, 0.8, 0.9]))
    assert b["q25"] < b["median"] < b["q75"]
    assert np.isclose(b["median"], 0.75)


def testTheAxisMapsLargerValuesHigherAndClipsPerMetric():
    lo, hi = TRANSPARENCY, SEEING
    # x grows left to right across 18:00 -> 07:00 (metric-independent).
    xlo, xhi = _x(18 * 60), _x(31 * 60)
    assert xlo < _x(24 * 60) < xhi
    # y: a larger value is drawn higher on the page (smaller y), for each metric's own axis.
    assert _y(0.9, lo) < _y(0.5, lo)
    assert _y(2.0, hi) < _y(0.5, hi)
    # Negative control: the orientation is not inverted — 0 is at the bottom (largest y).
    assert _y(0.0, lo) > _y(1.0, lo)
    # Values above a metric's axis maximum clip to the top, not off-plot.
    assert _y(lo.yMax + 5, lo) == _y(lo.yMax, lo)
    assert _y(hi.yMax + 5, hi) == _y(hi.yMax, hi)
