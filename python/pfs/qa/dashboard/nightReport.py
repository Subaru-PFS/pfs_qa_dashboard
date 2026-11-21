"""The night page: one night of a collection at a glance, for "what is being observed now".

The whole-run view is `pfs.qa.dashboard.runReport`; this page shows a single
night (the most recent by default): the latest visit, the night on a clock, the
night's visits most-recent-first, the science frames' spread, and the visits
that do not pass or have no QA result. It reads the same tables
(`pfs.qa.dashboard.live.readCollection`) and judges nothing: every verdict is
the gate's, read back from qadb. The data functions and most render helpers are
shared with `runReport`.
"""

import html
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from pfs.qa.dashboard.live import HST
from pfs.qa.dashboard.runReport import (
    AWAITING,
    NO_RESULT,
    Report,
    _fontFaces,
    _tally,
    _theme,
    infoFromTables,
    measure,
    scienceSections,
    visitTable,
)

__all__ = ["DATA_COLUMNS", "activeNight", "buildNightReport", "renderNight"]

_DATA = Path(__file__).parent / "data"

#: Statuses of a visit the night page shows but qadb holds no verdict for: taken, not (yet) judged.
_PENDING = (NO_RESULT, AWAITING)

#: Each status's CSS class (chip, clock square, tile border).
_CLASS = {
    "PASS": "pass",
    "WARN": "warn",
    "FAIL": "fail",
    "UNKNOWN": "unknown",
    NO_RESULT: "none",
    AWAITING: "awaiting",
}

#: The per-visit row the night clock reads, in this order (as `runReport`'s page uses).
DATA_COLUMNS = [
    *("visit", "hst", "label", "slit", "exptime", "arms"),
    *("status", "flagged", "detectorsStatus", "night", "why"),
]


def activeNight(visits: pd.DataFrame) -> str:
    """Return the most recent night (HST evening date) with a visit, or ``""`` when none has a time."""
    nights = sorted(night for night in visits["night"].unique() if night)
    return nights[-1] if nights else ""


def _fmtExp(exptime: Any) -> str:
    return "" if pd.isna(exptime) else str(round(float(exptime)))


def _dataRows(visits: pd.DataFrame) -> list[list]:
    """Return the night clock's rows, newest-last is irrelevant: the clock places by time."""
    rows = []
    for row in visits.itertuples():
        rows.append(
            [
                int(row.visit),
                row.hst,
                row.label,
                row.slit,
                _fmtExp(row.exptime),
                row.arms,
                row.status,
                row.flagged,
                row.detectorsStatus,
                row.night,
                row.why,
            ]
        )
    return rows


def _chip(status: str) -> str:
    return f"<span class='chip {_CLASS.get(status, 'unknown')}'>{status}</span>"


def _latestTile(visits: pd.DataFrame, images: pd.DataFrame) -> str:
    """Return the lead tile: the most recent visit of the night, with its verdict and how far QA got."""
    timed = visits[visits["time"].notna()]
    if timed.empty:
        return "<div class='tile lead'><span class='lab'>Latest visit</span><span class='big'>–</span></div>"
    row = timed.sort_values("time").iloc[-1]
    ofVisit = images[images["visit"] == row["visit"]]
    detectors = len(ofVisit)
    judged = int((ofVisit["status"] != "UNKNOWN").sum()) if detectors else 0
    if row["status"] == AWAITING:
        standing = "awaiting QA"
    elif row["status"] == NO_RESULT:
        standing = "no QA result"
    elif detectors:
        standing = f"{judged} of {detectors} detectors judged"
    else:
        standing = ""
    cls = _CLASS.get(row["status"], "unknown")
    # A coloured border for a verdict or an exposure in progress, not for a passed-over visit.
    border = cls if row["status"] not in (NO_RESULT,) else ""
    detail = html.escape(row["why"]) if row["why"] else ""
    return (
        f"<div class='tile lead {border}'><span class='lab'>Latest visit</span>"
        f"<span class='big'>{row['visit']}</span>"
        f"<span class='row'><span class='chip verdict {cls}'>{row['status']}</span> "
        f"{html.escape(row['label'])}</span>"
        f"<span class='sub'>{html.escape(row['hst'])} HST · {_fmtExp(row['exptime'])} s · {html.escape(standing)}</span>"
        + (f"<span class='sub'>{detail}</span>" if detail else "")
        + "</div>"
    )


def _summaryTile(label: str, visits: pd.DataFrame) -> str:
    counts = visits["status"].value_counts()
    chips = "".join(
        f"<span class='chip {k.lower()}'>{int(counts.get(k, 0))} {k}</span>"
        for k in ("PASS", "WARN", "FAIL")
        if counts.get(k, 0)
    )
    for status in _PENDING:
        count = int((visits["status"] == status).sum())
        if count:
            chips += f"<span class='chip {_CLASS[status]}'>{count} {status}</span>"
    # The big number is every visit taken, so the tile reads sensibly early in a night when none is judged yet.
    return (
        f"<div class='tile'><span class='lab'>{html.escape(label)}</span>"
        f"<span class='big'>{len(visits)}</span>"
        f"<span class='row'>{chips or 'none'}</span></div>"
    )


def _feedRows(visits: pd.DataFrame) -> str:
    """Return the night's visits, most recent first, as table rows."""
    order = visits.sort_values(["time", "visit"], ascending=[False, False], na_position="last")
    out = []
    for row in order.itertuples():
        when = html.escape(row.hst) if row.hst else "<span class='awaiting'>—</span>"
        if row.status in _PENDING:
            detectors = "<span class='awaiting'>awaiting QA</span>"
        else:
            detectors = html.escape(_flaggedText(row.flagged)) or "–"
        out.append(
            f"<tr><th scope='row'><a href='visit/{row.visit}'>{row.visit}</a></th>"
            f"<td class='l'>{when}</td><td class='l'>{html.escape(row.label)}</td>"
            f"<td>{_fmtExp(row.exptime)}</td><td class='l'>{row.arms}</td>"
            f"<td class='l'>{_chip(row.status)}</td><td class='l'>{detectors}</td>"
            f"<td class='l why'>{html.escape(row.why)}</td></tr>"
        )
    return "\n".join(out)


def _flaggedText(flagged: str) -> str:
    names = {"W": "WARN", "F": "FAIL"}
    parts = flagged.split()
    return ", ".join(
        f"{cam} {names.get(letter, letter)}" for cam, letter in zip(parts[::2], parts[1::2], strict=True)
    )


def _notPassRows(visits: pd.DataFrame) -> str:
    notPass = visits[(visits["status"] != "PASS") & (visits["illumination"] != "iis")]
    out = []
    for row in notPass.itertuples():
        out.append(
            f"<tr><th scope='row'>{row.visit}</th><td class='l'>{html.escape(row.hst)}</td>"
            f"<td class='l'>{html.escape(row.label)}</td><td class='l'>{_chip(row.status)}</td>"
            f"<td class='l'>{html.escape(_flaggedText(row.flagged))}</td>"
            f"<td class='l why'>{html.escape(row.why)}</td></tr>"
        )
    return "\n".join(out), len(notPass)


def _noResultSection(visits: pd.DataFrame) -> str:
    noResult = visits[visits["status"] == NO_RESULT]
    if noResult.empty:
        return ""
    rows = "\n".join(
        f"<tr><th scope='row'>{row.visit}</th><td class='l'>{html.escape(row.hst)}</td>"
        f"<td class='l'>{html.escape(row.label)}</td><td class='l'>{row.slit}</td>"
        f"<td>{_fmtExp(row.exptime)}</td><td class='l'>{row.arms}</td>"
        f"<td class='l why'>{html.escape(row.why)}</td></tr>"
        for row in noResult.itertuples()
    )
    return (
        f"<section class='block' id='no-result'><h2>Visits with no QA result tonight ({len(noResult)})</h2>"
        "<p>These exposed the spectrograph and are older than the latest visit judged, yet have no QA result. "
        "The last column is how the instrument's sequence ended.</p>"
        "<div class='tablewrap'><table><thead><tr><th scope='col' class='l'>Visit</th>"
        "<th scope='col' class='l'>HST</th><th scope='col' class='l'>Kind</th><th scope='col' class='l'>Lit through</th>"
        "<th scope='col'>Exp (s)</th><th scope='col' class='l'>Arms</th><th scope='col' class='l'>Sequence</th></tr></thead>"
        f"<tbody>\n{rows}\n</tbody></table></div></section>"
    )


def _awaitingSection(visits: pd.DataFrame) -> str:
    awaiting = visits[visits["status"] == AWAITING]
    if awaiting.empty:
        return ""
    order = awaiting.sort_values(["time", "visit"], ascending=[False, False], na_position="last")
    rows = "\n".join(
        f"<tr><th scope='row'><a href='visit/{row.visit}'>{row.visit}</a></th>"
        f"<td class='l'>{html.escape(row.hst)}</td><td class='l'>{html.escape(row.label)}</td>"
        f"<td class='l'>{row.slit}</td><td>{_fmtExp(row.exptime)}</td><td class='l'>{row.arms}</td></tr>"
        for row in order.itertuples()
    )
    return (
        f"<section class='block' id='awaiting'><h2>Awaiting QA ({len(awaiting)})</h2>"
        "<p>Taken after the latest visit judged, with no QA result yet — most recent first. Their verdicts "
        "appear here as the pipeline catches up.</p>"
        "<div class='tablewrap'><table><thead><tr><th scope='col' class='l'>Visit</th>"
        "<th scope='col' class='l'>HST</th><th scope='col' class='l'>Kind</th><th scope='col' class='l'>Lit through</th>"
        "<th scope='col'>Exp (s)</th><th scope='col' class='l'>Arms</th></tr></thead>"
        f"<tbody>\n{rows}\n</tbody></table></div></section>"
    )


def renderNight(
    visits: pd.DataFrame,
    images: pd.DataFrame,
    night: str,
    title: str = "Tonight",
    source: str = "",
    runUrl: str = "/run",
    now: datetime | None = None,
) -> str:
    """Return the night page as one stand-alone HTML document.

    Parameters
    ----------
    visits, images : `pandas.DataFrame`
        The night's visits (`runReport.visitTable`) and images (`runReport.measure`), already scoped.
    night : `str`
        The night shown (HST evening date).
    title, source, runUrl, now
        The page title; the source note (HTML); the link to the whole-run report; the time made (HST).
    """
    now = datetime.now(HST) if now is None else now
    judged = visits[~visits["status"].isin(_PENDING)]
    notPass, nNotPass = _notPassRows(judged)
    lead = (
        _tally("Science", judged[judged["kind"] == "science"], int((visits["status"] == NO_RESULT).sum()))
        + _tally(
            "Calibs", judged[judged["kind"].isin(["arc", "quartz"]) & (judged["illumination"] == "full")]
        )
        + _tally("Engineering", judged[judged["illumination"] == "iis"])
    )
    tiles = (
        _latestTile(visits, images)
        + _summaryTile("Tonight", visits)
        + _summaryTile("Science", visits[visits["kind"] == "science"])
    )
    offsetsHtml, offsetAxis, residualsHtml = scienceSections(images[images["kind"] == "science"])
    nightText = html.escape(night) if night else "the latest night"
    fields = {
        "{{THEME}}": _theme(),
        "{{TITLE}}": html.escape(title),
        "{{FONT_FACES}}": _fontFaces(),
        "{{META}}": f"Night of {nightText} (HST, evening date) · {len(judged):,} visits judged, "
        f"{len(images):,} detector images · the whole run is in the run report",
        "{{NIGHT}}": nightText,
        "{{TILES}}": tiles,
        "{{LEAD}}": lead,
        "{{AWAITING_SECTION}}": _awaitingSection(visits),
        "{{FEED}}": _feedRows(visits),
        "{{OFFSETS}}": offsetsHtml,
        "{{OAXIS}}": offsetAxis,
        "{{RESIDUALS}}": residualsHtml,
        "{{NOTPASS}}": notPass,
        "{{NNOTPASS}}": str(nNotPass),
        "{{NO_RESULT_SECTION}}": _noResultSection(visits),
        "{{RUN_URL}}": html.escape(runUrl),
        "{{TODAY}}": f"{now:%Y-%m-%d}",
        "{{SOURCE}}": source.replace("{date}", f"{now:%Y-%m-%d}"),
        "{{DATA}}": json.dumps(_dataRows(visits), separators=(",", ":")).replace("<", "\\u003c"),
    }
    page = (_DATA / "nightReport.html").read_text()
    for key, value in fields.items():
        page = page.replace(key, value)
    return page


def buildNightReport(
    tables: Mapping[str, pd.DataFrame],
    info: pd.DataFrame | None = None,
    title: str = "Tonight",
    source: str = "",
    night: str | None = None,
    runUrl: str = "/run",
    now: datetime | None = None,
) -> Report:
    """Return the night page of what a qadb read holds, scoped to one night (the latest by default).

    Parameters
    ----------
    tables : mapping
        From `pfs.qa.dashboard.live.readCollection`.
    info : `pandas.DataFrame`, optional
        The visits' times and sequences (`runReport.readVisitInfo`); `runReport.infoFromTables` when None.
    title, source, runUrl, now
        As `renderNight` takes them.
    night : `str`, optional
        The night (HST evening date) to show; the most recent with a visit when None.
    """
    allImages = measure(tables)
    allVisits = visitTable(allImages, infoFromTables(tables) if info is None else info, awaiting=True)
    night = activeNight(allVisits) if night is None else night
    visits = allVisits[allVisits["night"] == night] if night else allVisits
    images = allImages[allImages["visit"].isin(set(visits["visit"]))]
    page = renderNight(visits, images, night, title=title, source=source, runUrl=runUrl, now=now)
    return Report(images, visits, page)
