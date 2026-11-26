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
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from pfs.qa.dashboard.agConditions import conditions
from pfs.qa.dashboard.live import HST
from pfs.qa.dashboard.runReport import (
    AWAITING,
    NO_QA,
    NO_RESULT,
    REDUCE_FAILED,
    Report,
    _calibrationOffsets,
    _clockControls,
    _clockScript,
    _fontFaces,
    _sequenceLabel,
    _tally,
    _theme,
    calibDriftSection,
    calibrationDrift,
    infoFromTables,
    measure,
    scienceSections,
    visitTable,
)

__all__ = ["DATA_COLUMNS", "activeNight", "buildNightReport", "nightsOf", "renderNight"]

_log = logging.getLogger(__name__)

_DATA = Path(__file__).parent / "data"

#: Statuses of a visit the night page shows but qadb holds no verdict for: taken, not (yet) judged, or no
#: QA method (a dark, bias, ...). All are kept out of the judged counts.
_PENDING = (NO_RESULT, AWAITING, NO_QA)

#: Each status's CSS class (chip, clock square, tile border).
_CLASS = {
    "PASS": "pass",
    "WARN": "warn",
    "FAIL": "fail",
    "UNKNOWN": "unknown",
    REDUCE_FAILED: "redfail",
    NO_RESULT: "none",
    AWAITING: "awaiting",
    NO_QA: "noqa",
}

#: The per-visit row the shared time map reads, in the order `runReport`'s page emits (so one clock
#: script serves both): ``[visit, hst, label, slit, exptime, arms, status, flagged, why, night,
#: detectorsStatus]``.
DATA_COLUMNS = [
    *("visit", "hst", "label", "slit", "exptime", "arms"),
    *("status", "flagged", "why", "night", "detectorsStatus"),
]


def activeNight(visits: pd.DataFrame) -> str:
    """Return the most recent night (HST evening date) with a visit, or ``""`` when none has a time."""
    return nightsOf(visits)[-1] if len(nightsOf(visits)) else ""


def nightsOf(visits: pd.DataFrame) -> list[str]:
    """Return the nights (HST evening dates) with a visit, oldest first."""
    return sorted(night for night in visits["night"].unique() if night)


def _nightHref(night: str, latest: str) -> str:
    """Return a night's URL: the live page (``/``) for the latest night, else its dated page."""
    return "/" if night and night == latest else f"/night/{night}"


def _nightNav(night: str, nights: list[str], runUrl: str = "/run") -> str:
    """Return the night navigation: a run-report link, then the night-to-night controls when there is a choice.

    The run-report link always shows, so the night page reaches the whole-run report even on a
    single-night run (the common case early in a run). When the run spans more than one night,
    earlier / later links and a dropdown follow; links point at ``/night/<date>``, and the latest
    night points at ``/`` so the live page stays canonical.
    """
    runLink = f"<a class='nav' href='{html.escape(runUrl)}'>Run report</a>"
    nights = [n for n in nights if n]
    if not night or len(nights) < 2:
        return f"<nav class='nightnav' aria-label='Night'>{runLink}</nav>"
    latest = nights[-1]
    here = nights.index(night) if night in nights else None
    earlier = nights[here - 1] if here not in (None, 0) else None
    later = nights[here + 1] if here is not None and here < len(nights) - 1 else None
    options = "".join(
        f"<option value='{_nightHref(n, latest)}'{' selected' if n == night else ''}>"
        f"{html.escape(n)}{' (latest)' if n == latest else ''}</option>"
        for n in reversed(nights)
    )
    earlierLink = (
        f"<a class='nav' href='{_nightHref(earlier, latest)}' rel='prev'>&lsaquo; {html.escape(earlier)}</a>"
        if earlier
        else "<span class='nav disabled'>&lsaquo; earlier</span>"
    )
    laterLink = (
        f"<a class='nav' href='{_nightHref(later, latest)}' rel='next'>{html.escape(later)} &rsaquo;</a>"
        if later
        else "<span class='nav disabled'>later &rsaquo;</span>"
    )
    return (
        "<nav class='nightnav' aria-label='Night'>"
        f"{runLink}"
        f"{earlierLink}"
        f"<select id='night-pick' aria-label='Jump to a night'>{options}</select>"
        f"{laterLink}</nav>"
    )


def _fmtExp(exptime: Any) -> str:
    return "" if pd.isna(exptime) else str(round(float(exptime)))


def _dataRows(visits: pd.DataFrame) -> list[list]:
    """Return the clock's rows in `DATA_COLUMNS` order; order is irrelevant, the clock places by time."""
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
                row.why,
                row.night,
                row.detectorsStatus,
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
    elif row["status"] == NO_QA:
        standing = "no QA method"
    elif row["status"] == REDUCE_FAILED:
        standing = "reduceExposure failed"
    elif detectors:
        standing = f"{judged} of {detectors} detectors judged"
    else:
        standing = ""
    cls = _CLASS.get(row["status"], "unknown")
    # A coloured border for a verdict or an exposure in progress, not for a passed-over or no-QA visit.
    border = cls if row["status"] not in (NO_RESULT, NO_QA) else ""
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
    # REDUCE FAILED folds into FAIL for the tile's counts (the per-visit feed carries the distinct verdict),
    # so the chips still add up to the big number.
    counts = visits["status"].replace(REDUCE_FAILED, "FAIL").value_counts()
    # Every status a visit can carry gets a chip, so the chips add up to the big number (all visits taken):
    # the gate's verdicts, UNKNOWN (judged but nothing decidable, or no stored verdict), then NO RESULT / AWAITING.
    chips = "".join(
        f"<span class='chip {k.lower()}'>{int(counts.get(k, 0))} {k}</span>"
        for k in ("PASS", "WARN", "FAIL", "UNKNOWN")
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
        if row.status == AWAITING:
            # Taken after the latest judged visit, so its QA may still be on the way.
            detectors = "<span class='awaiting'>awaiting QA</span>"
        else:
            # A judged visit shows its flagged detectors; a NO_RESULT one (passed over, no QA coming)
            # has none, so it reads "–" like a visit with nothing flagged.
            detectors = html.escape(_flaggedText(row.flagged)) or "–"
        out.append(
            f"<tr><th scope='row'><a href='/visit/{row.visit}'>{row.visit}</a></th>"
            f"<td class='l'>{html.escape(_sequenceLabel(row.sequenceId))}</td>"
            f"<td class='l'>{when}</td><td class='l'>{html.escape(row.label)}</td>"
            f"<td class='l'>{html.escape(str(getattr(row, 'design', '') or ''))}</td>"
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
        "<div class='tablewrap tall'><table><thead><tr><th scope='col' class='l'>Visit</th>"
        "<th scope='col' class='l'>HST</th><th scope='col' class='l'>Kind</th><th scope='col' class='l'>Lit through</th>"
        "<th scope='col'>Exp (s)</th><th scope='col' class='l'>Arms</th><th scope='col' class='l'>Sequence</th></tr></thead>"
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
    calibDriftSm: str = "",
    calibDriftArm: str = "",
    transparency: pd.DataFrame | None = None,
    seeing: pd.DataFrame | None = None,
    nights: list[str] | None = None,
    agTiles: str = "",
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
    transparency, seeing : `pandas.DataFrame`, optional
        The night's AG transparency and seeing (`agConditions.readTransparency` / `readSeeing`); the Conditions
        tile and its modal are built from whichever has data, and omitted when neither does.
    nights : `list` of `str`, optional
        Every night (HST evening date) of the run, for the earlier/later navigation and the night dropdown;
        the latest links to ``/`` and the rest to ``/night/<date>``. No navigation when there is one night or none.
    agTiles : `str`
        The night's auto-guiding tiles (`pfs.qa.dashboard.agRun.buildNightTiles`), as HTML; none when empty.
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
    noQa = visits[visits["status"] == NO_QA]
    if len(noQa):
        # Frames with no QA method (darks, biases, ...): a count only, since they carry no verdict.
        lead += (
            f"<li><span class='k'>No QA</span><span class='n'>{len(noQa):,} visits</span>"
            f"<span class='chip noqa'><b>{len(noQa):,}</b> NO QA</span></li>"
        )
    # The third tile is always the night's AG conditions (seeing and transparency, opening a modal); with no
    # conditions data it shows its own no-data state rather than falling back to another metric.
    condTile, condModal = conditions(transparency, seeing, night)
    tiles = (
        _latestTile(visits, images)
        + _summaryTile("Tonight", visits)
        + condTile  # always the Conditions tile (its own no-data state when there is nothing to show)
    )
    offsetsHtml, offsetAxis, residualsHtml = scienceSections(images[images["kind"] == "science"])
    nightText = html.escape(night) if night else "the latest night"
    # The page title (heading and browser tab) names the night shown, so one night reads apart from another.
    pageTitle = f"{title} · {night}" if night else title
    fields = {
        "{{THEME}}": _theme(),
        "{{TITLE}}": html.escape(pageTitle),
        "{{FONT_FACES}}": _fontFaces(),
        "{{META}}": f"Night of {nightText} (HST, evening date) · {len(judged):,} visits judged, "
        f"{len(images):,} detector images · the whole run is in the run report",
        "{{NIGHT}}": nightText,
        "{{NIGHT_NAV}}": _nightNav(night, nightsOf(visits) if nights is None else nights, runUrl),
        "{{CLOCK_CONTROLS}}": _clockControls(),
        "{{CLOCK_SCRIPT}}": _clockScript(),
        "{{TILES}}": tiles,
        "{{AG_TILES}}": agTiles,
        "{{LEAD}}": lead,
        "{{CONDITIONS_MODAL}}": condModal,
        "{{FEED}}": _feedRows(visits),
        "{{CALIB_DRIFT_SM}}": calibDriftSm,
        "{{CALIB_DRIFT_ARM}}": calibDriftArm,
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
    readTransparency: Callable[[int, int], pd.DataFrame] | None = None,
    readSeeing: Callable[[int, int], pd.DataFrame] | None = None,
    agTiles: Callable[[str], str] | None = None,
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
        The night (HST evening date) to show; the most recent with a visit when None. Any night of the
        run may be asked for (the past-night ``/night/<date>`` pages); the earlier/later navigation and
        the dropdown cover every night the tables hold.
    readTransparency, readSeeing : callable, optional
        ``read(first, last)`` returning a visit range's AG transparency / seeing (`agConditions.readTransparency`
        / `readSeeing` over qadb). Read for the night's visits to build the Conditions tile and modal; a read
        that fails is logged and that metric dropped, so it never blanks the page. None omits the metric.
    agTiles : callable, optional
        ``agTiles(night)`` returns the night's auto-guiding tiles as HTML
        (`pfs.qa.dashboard.agRun.AgRunCache.nightTiles`), shown under the page's own; it reads nothing.
        None, or an empty answer, leaves them out.
    """
    offsets = _calibrationOffsets(tables)  # shared by measure (science) and calibrationDrift (calibs)
    allImages = measure(tables, offsets=offsets)
    allVisits = visitTable(
        allImages, infoFromTables(tables) if info is None else info, awaiting=True, includeNoQa=True
    )
    nights = nightsOf(allVisits)
    night = (nights[-1] if nights else "") if night is None else night
    visits = allVisits[allVisits["night"] == night] if night else allVisits
    images = allImages[allImages["visit"].isin(set(visits["visit"]))]
    # The drift trend is the whole run's daily calibrations, with tonight's points marked; both
    # groupings are rendered and a toggle on the page chooses between them.
    drift = calibrationDrift(tables, allVisits, offsets=offsets)

    def read(reader: Callable[[int, int], pd.DataFrame] | None, what: str) -> pd.DataFrame | None:
        if reader is None or not len(visits):
            return None
        try:
            return reader(int(visits["visit"].min()), int(visits["visit"].max()))
        except Exception:
            _log.exception("Reading AG %s failed; dropping that metric", what)
            return None

    page = renderNight(
        visits,
        images,
        night,
        title=title,
        source=source,
        runUrl=runUrl,
        now=now,
        calibDriftSm=calibDriftSection(drift, activeNight=night, by="spectrograph"),
        calibDriftArm=calibDriftSection(drift, activeNight=night, by="arm"),
        transparency=read(readTransparency, "transparency"),
        seeing=read(readSeeing, "seeing"),
        nights=nights,
        agTiles=agTiles(night) if agTiles is not None and night else "",
    )
    return Report(images, visits, page)
