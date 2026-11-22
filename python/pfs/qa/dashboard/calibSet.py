"""The calibration set's go/no-go for the dashboard: which exposures to build the calibrations from.

A run's ``detectorMap_calib`` and fiber profiles are built per detector from its calibration set — a
few arc and quartz sequences taken within an hour — and a run can build the set more than once (the
run-start set, a detector rebuild, a later re-set). This module finds each of those **set events** and
judges its exposures per detector, reusing the stack-free judging of
`pfs.drp.qa.comparison.calibInputs` (the same code as ``drpQa.py calib inputs``, so the dashboard and
the command agree). Nothing is judged here; the inputs are read from qadb (the measured numbers and
the normal values) and the opdb (the visit listing, for which visits are a set).

The go/no-go of an exposure's detector is ``go``, ``no-go`` (leave it out of that exposure's inputs),
``missing`` (exposed but not measured — not ingested or not reduced) or ``""`` (not exposed). It rests
on three rules in `calibInputs`: the gate against the camera's normal values, consistency within the
sequence (FWHM and reserved-line RMS), and each species' line count against the sequence's best
exposure. The reserved-RMS rule needs ``reductionStats`` (qadb's ``detector_health_qa``); until the
actor loads it, that rule is inert and the rest stand.
"""

import html
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from pfs.drp.qa.comparison.calibInputs import (
    detectorTable,
    goNoGo,
    judge,
    setVisits,
    speciesTable,
    summary,
)
from pfs.drp.qa.comparison.classify import classifyVisits
from pfs.drp.qa.comparison.runs import loadPeriods

__all__ = ["CalibSet", "calibSetEvents", "renderCalibSet"]

#: Detector order across a row of the grid: b, r, n, then m (the medium-resolution red arm).
_ARMS = "brnm"

#: A new set event starts when the gap to the previous set exposure exceeds this: a set is taken within
#: an hour, so a few hours cleanly separates the run-start set from a later rebuild or re-set.
_EVENT_GAP = pd.Timedelta(hours=3)

#: ``reductionStats`` long-format columns, for an empty frame when qadb holds none yet.
_REDUCTION_COLUMNS = ["visit", "arm", "spectrograph", "amplifier", "metric", "value"]

#: Each go/no-go value's CSS class and glyph (the cell colour carries the verdict; no-go is marked).
_CELL = {"go": ("go", ""), "no-go": ("nogo", "✕"), "missing": ("miss", "·"), "": ("na", "")}


@dataclass(frozen=True)
class CalibSet:
    """One set-building event: its visits, the go/no-go grid (detectors × visits), and the notes."""

    label: str
    lamps: str
    visits: pd.DataFrame
    grid: pd.DataFrame
    notes: list[str]


#: Whether this drp_qa's ``detectorTable`` takes ``reductionStats`` (PIPE2D-2016, drp_qa 0.10.0). Before that
#: it read the reserved-line RMS from ``iqQaMetrics`` and took no such argument; pass it only when accepted.
_DETECTOR_TABLE_TAKES_REDUCTION = "reductionStats" in inspect.signature(detectorTable).parameters


def _judgeSet(
    visits: pd.DataFrame,
    iqMetrics: pd.DataFrame,
    lineMetrics: pd.DataFrame,
    reductionStats: pd.DataFrame,
    normalValues: pd.DataFrame | None,
) -> pd.DataFrame:
    """Judge the set's detectors, adapting to drp_qa's ``detectorTable`` signature (pre-/post-PIPE2D-2016)."""
    if _DETECTOR_TABLE_TAKES_REDUCTION:
        return judge(detectorTable(visits, iqMetrics, lineMetrics, reductionStats, normalValues))
    return judge(detectorTable(visits, iqMetrics, lineMetrics, normalValues))


def _reductionStats(tables: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Return the read's ``reductionStats``, or an empty long-format frame when qadb holds none."""
    reduction = tables.get("reductionStats")
    if reduction is None or len(reduction) == 0:
        return pd.DataFrame(columns=_REDUCTION_COLUMNS)
    return reduction


def _hst(value: Any) -> str:
    """Return a timestamp as ``YYYY-MM-DD HH:MM`` (HST, as qadb/opdb store it), or ``?``."""
    return "?" if value is None or pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")


def _eventKey(times: pd.Series) -> pd.Series:
    """Return a 0, 1, 2 … id per row, stepping up where the gap to the previous exposure exceeds `_EVENT_GAP`.

    ``times`` must be in time order; `setVisits` sorts the set by visit id, which is chronological, so a
    rebuild set hours after the run-start set starts a new id and the two are reported as separate events.
    """
    times = pd.to_datetime(times)
    return (times - times.shift()).gt(_EVENT_GAP).fillna(False).cumsum()


def calibSetEvents(
    listing: pd.DataFrame,
    tables: Mapping[str, pd.DataFrame],
    normalValues: pd.DataFrame | None = None,
    periods: Mapping[str, Any] | None = None,
) -> list[CalibSet]:
    """Return the run's set-building events, each judged per detector.

    Parameters
    ----------
    listing : `pandas.DataFrame`
        The run's opdb visit listing (`pfs.drp.qa.comparison.queries.readVisitListing`).
    tables : mapping
        A qadb read (`pfs.qa.dashboard.live.readCollection`): ``iqQaMetrics``,
        ``dmLineResidualMetrics`` and, when present, ``reductionStats``.
    normalValues : `pandas.DataFrame`, optional
        The collection's normal values (`pfs.drp.qa.qadb.reader.readNormalValues`); without them the
        gate rule gives no verdict and the go/no-go rests on the sequence rules, as in `calibInputs`.
    periods : mapping, optional
        The observing runs (`pfs.drp.qa.comparison.runs.loadPeriods`); read from the package by default.
    """
    periods = loadPeriods() if periods is None else periods
    classified = classifyVisits(listing, list(periods.values()))
    cadence = classified.get("cadence", pd.Series("", index=classified.index))
    allSet = setVisits(listing, visits=classified.loc[cadence == "set", "pfs_visit_id"])
    if allSet.empty:
        return []
    iq = tables["iqQaMetrics"]
    lines = tables["dmLineResidualMetrics"]
    reduction = _reductionStats(tables)
    # Judge the whole set once: the sequence rules group by IIC sequence, which lies within one event,
    # so this matches `calib inputs` and avoids judging a degenerate per-event subset. Slice per event.
    species = speciesTable(allSet, lines)
    judged = _judgeSet(allSet, iq, lines, reduction, normalValues)
    event = _eventKey(allSet["time_exp_start"])
    events: list[CalibSet] = []
    for _, ofEvent in allSet.groupby(event, sort=True):
        ofEvent = ofEvent.sort_values("visit")
        visitSet = set(ofEvent["visit"].astype(int))
        grid = goNoGo(judged[judged["visit"].isin(visitSet)], ofEvent)
        # Keep only the detectors the event exposed (a row that is all "" is not exposed at all).
        grid = grid[(grid != "").any(axis=1)]
        notes = summary(
            ofEvent, judged[judged["visit"].isin(visitSet)], species[species["visit"].isin(visitSet)]
        )
        lamps = ", ".join(dict.fromkeys(ofEvent["lamp"].dropna().astype(str)))
        events.append(CalibSet(_hst(ofEvent["time_exp_start"].min()), lamps, ofEvent, grid, notes))
    return events


def _detectorOrder(grid: pd.DataFrame) -> list[str]:
    """Return the grid's detectors in b, r, n, m then spectrograph order."""
    return sorted(grid.index, key=lambda name: (_ARMS.find(name[0]) % 9, name))


def _rowLabel(row: Any) -> str:
    """Return a set visit's label for the grid: its lamp and place in its sequence."""
    lamp = html.escape(str(row.lamp)) if isinstance(row.lamp, str) and row.lamp else "?"
    return f"{lamp} {int(row.k)}/{int(row.n)}" if pd.notna(row.k) and pd.notna(row.n) else lamp


def _renderEvent(event: CalibSet) -> str:
    """Return one set event as a heading, a visits × detectors go/no-go grid and its notes."""
    grid = event.grid
    detectors = _detectorOrder(grid)
    visitRow = {int(r.visit): r for r in event.visits.itertuples()}
    headCells = "".join(f"<th scope='col'>{det}</th>" for det in detectors)
    rows = []
    for visit in grid.columns:
        row = visitRow.get(int(visit))
        cells = []
        for det in detectors:
            value = str(grid.at[det, visit]) if det in grid.index else ""
            klass, text = _CELL.get(value, _CELL[""])
            title = f"{det} {int(visit)}: {value}" if value else f"{det} {int(visit)}: not exposed"
            cells.append(f"<td class='setcell {klass}' title='{html.escape(title)}'>{text}</td>")
        label = _rowLabel(row) if row is not None else ""
        rows.append(
            f'<tr><th scope="row"><a href="/visit/{int(visit)}">{int(visit)}</a></th>'
            f"<td class='l'>{label}</td>{''.join(cells)}</tr>"
        )
    notes = "".join(f"<li>{html.escape(line)}</li>" for line in event.notes)
    flat = grid.to_numpy().astype(str)
    nogo = int((flat == "no-go").sum())
    measured = int((flat == "go").sum()) + nogo
    if nogo:
        state = f'<span class="chip fail">{nogo} no-go</span>'
    elif measured == 0:
        state = '<span class="setstate">not yet measured</span>'
    else:
        state = '<span class="setstate">all go</span>'
    # Open an event with something to act on (a no-go); collapse the rest so the section stays short.
    count = len(event.visits)
    heading = (
        f"Set — {html.escape(event.label)} HST · {count} visit{'' if count == 1 else 's'} · "
        f"{html.escape(event.lamps)} · {state}"
    )
    return (
        f'<details class="setevent"{" open" if nogo else ""}>\n'
        f"  <summary>{heading}</summary>\n"
        "  <div class='tablewrap tall'><table class='setgrid'><thead><tr>"
        "<th scope='col'>Visit</th><th scope='col' class='l'>Exposure</th>"
        f"{headCells}</tr></thead><tbody>\n"
        + "\n".join(rows)
        + "\n</tbody></table></div>"
        + (f"<ul class='setnotes'>{notes}</ul>" if notes else "")
        + "\n</details>"
    )


def renderCalibSet(events: list[CalibSet]) -> str:
    """Return the whole calibration-set section (heading and each event's go/no-go grid), or ``""`` when none."""
    if not events:
        return ""
    body = "\n".join(_renderEvent(event) for event in events)
    return (
        '  <section class="block">\n'
        "    <h2>Calibration set: go/no-go per detector</h2>\n"
        "    <p>Each of the run's set-building events, judged per detector image, reusing "
        "<code>calib inputs</code>. A green cell is go, a red cell a detector to leave out of that exposure's "
        "inputs, a dot exposed but not measured, blank not exposed. The gate rule needs the collection's normal "
        "values and the reserved-RMS rule needs <code>reductionStats</code>, so a sparse run shows mostly go "
        "and missing.</p>\n"
        f"{body}\n  </section>"
    )
