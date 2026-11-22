"""Tests for the AG sky-transparency night-page section (``agTransparency``)."""

from datetime import date

import numpy as np
import pandas as pd

from pfs.qa.dashboard.agTransparency import (
    _NIGHT_HOURS,
    _Y_MAX,
    _bins,
    _minutes,
    _runs,
    _x,
    _y,
    conditions,
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
    """Return a qadb-shaped transparency read: ``n`` exposures a minute apart, from ``start``."""
    times = pd.date_range(start, periods=n, freq="1min")
    return pd.DataFrame(
        {
            "pfs_visit_id": visit,
            "agc_exposure_id": range(1, n + 1),
            "taken_at": times,
            "transparency_median": np.linspace(0.70, 0.85, n),
        }
    )


def _exposures(times, transparency, visit=1000):
    """Return a `readTransparency`-shaped frame (``visit``, ``agcExposureId``, ``time``, ``transparency``)."""
    times = pd.to_datetime(times)
    return pd.DataFrame(
        {
            "visit": visit,
            "agcExposureId": range(len(times)),
            "time": times,
            "transparency": np.asarray(transparency, float),
        }
    )


def testReadTransparencyRenamesAndBindsTheRange():
    qadb = _FakeQadb(_rawRows(3))
    out = readTransparency(qadb, 1000, 1009)
    assert list(out.columns) == ["visit", "agcExposureId", "time", "transparency"]
    assert len(out) == 3 and out["visit"].iloc[0] == 1000
    assert pd.api.types.is_datetime64_any_dtype(out["time"])
    # The visit range is bound, not formatted into the SQL.
    assert qadb.params == {"first": 1000, "last": 1009}
    assert "transparency_agc_exposure" in qadb.sql


def testReadTransparencyEmptyKeepsTheColumns():
    empty = pd.DataFrame(columns=["pfs_visit_id", "agc_exposure_id", "taken_at", "transparency_median"])
    out = readTransparency(_FakeQadb(empty), 1, 2)
    assert out.empty and list(out.columns) == ["visit", "agcExposureId", "time", "transparency"]


def testConditionsGivesAClickableTileAndAModalWithTheChart():
    df = _exposures(pd.date_range("2026-11-04 20:00", periods=120, freq="1min"), np.linspace(0.72, 0.84, 120))
    tile, modal = conditions(df, "2026-11-04")
    # The tile is a button that opens the modal, labelled Conditions, with a sparkline and the median.
    assert "<button" in tile and "aria-controls='conditions'" in tile
    assert "Conditions" in tile and 'class="spark"' in tile and "0.78" in tile
    # The modal holds the full chart: median line, IQR band, the summary and the hour ticks.
    assert "id='conditions'" in modal and "AG sky transparency tonight" in modal
    assert "<polyline" in modal and "<polygon" in modal
    assert "120 AG exposures" in modal and ">18:00<" in modal and ">06:00<" in modal


def testEmptyOrBadNightGivesNothing():
    good = _exposures(["2026-11-04 20:00", "2026-11-04 20:05"], [0.8, 0.8])
    assert conditions(good.iloc[:0], "2026-11-04") == ("", "")
    assert conditions(None, "2026-11-04") == ("", "")
    assert conditions(good, "") == ("", "")
    assert conditions(good, "not-a-date") == ("", "")


def testExposuresOutsideTheNightWindowAreCountedNotDrawn():
    # Two in the night (20:00), one in daytime (10:00, before 18:00) that the window excludes.
    df = _exposures(["2026-11-04 20:00", "2026-11-04 20:05", "2026-11-04 10:00"], [0.8, 0.8, 0.8])
    _, modal = conditions(df, "2026-11-04")
    assert "2 AG exposures" in modal  # only the two inside
    assert "1 outside 18:00" in modal


def testAllOutsideTheWindowGivesNothing():
    df = _exposures(["2026-11-04 10:00", "2026-11-04 11:00"], [0.8, 0.8])  # daytime, before 18:00
    assert conditions(df, "2026-11-04") == ("", "")


def testMinutesCountFromTheEveningMidnight():
    # 20:00 on the evening date is 1200 min; 02:00 the next morning is (24+2)*60 = 1560.
    times = pd.to_datetime(pd.Series(["2026-11-04 20:00", "2026-11-05 02:00"]))
    minutes = _minutes(times, date(2026, 11, 4))
    assert list(minutes) == [1200.0, 1560.0]


def testRunsBreakOnLongGaps():
    # Two blocks of bins, 30 min apart (> the 16-min break), give two runs.
    minutes = np.array([1100.0, 1105.0, 1110.0, 1140.0, 1145.0])  # 18:20-18:30, then 19:00-19:05
    values = np.full(minutes.shape, 0.8)
    runs = _runs(_bins(minutes, values))
    assert len(runs) == 2
    # Negative control: within one block the bins stay a single run.
    close = np.array([1100.0, 1105.0, 1110.0])
    assert len(_runs(_bins(close, np.full(close.shape, 0.8)))) == 1


def testBinsSummariseEachInterval():
    # Several exposures in one 5-min bin: the bin holds their median and quartiles.
    minutes = np.array([1200.0, 1201.0, 1202.0, 1203.0])
    values = np.array([0.6, 0.7, 0.8, 0.9])
    (b,) = _bins(minutes, values)
    assert b["q25"] < b["median"] < b["q75"]
    assert np.isclose(b["median"], 0.75)


def testTheAxisMapsHighTransparencyHigherAndTheWindowLeftToRight():
    lo, hi = _NIGHT_HOURS[0] * 60, _NIGHT_HOURS[1] * 60
    # x grows left to right across 18:00 -> 07:00.
    assert _x(lo) < _x((lo + hi) / 2) < _x(hi)
    # y: a clearer sky (higher transparency) is drawn higher on the page (smaller y).
    assert _y(0.9) < _y(0.5)
    # Negative control: the orientation is not inverted — 0 is at the bottom (largest y).
    assert _y(0.0) > _y(1.0)
    # Values above the axis maximum are clipped to the top, not drawn off-plot.
    assert _y(_Y_MAX + 5) == _y(_Y_MAX)
