"""The per-visit page: one visit over all its detectors (``/visit/<n>``).

Built from the dashboard's cached store, so it reads no database of its own: the
store already holds every visit's rows. It judges nothing — each detector's
verdict and reason are its ``qa_gate`` row, and the metric values are as the QA
tasks stored them. The grid shows the stored values coloured by the gate's
verdict and the gate's reason beside each flagged detector, rather than
re-deriving limits from the current registry (which may differ from the one a
visit was judged with).

The first version is the header, the per-detector verdicts, and a metric grid;
line residuals by species and pipeline artefacts come later (PIPE2D-2023).
"""

import html
from datetime import datetime
from typing import Any

import pandas as pd

from pfs.qa.dashboard.live import HST
from pfs.qa.dashboard.runReport import (
    ARMS,
    NO_RESULT,
    _fontFaces,
    _theme,
    measure,
    visitTable,
    worst,
)

__all__ = ["buildVisitPage"]

#: Each status's CSS class, as on the night page.
_CLASS = {
    "PASS": "pass",
    "WARN": "warn",
    "FAIL": "fail",
    "UNKNOWN": "unknown",
    NO_RESULT: "none",
    "AWAITING": "awaiting",
}

#: The metric grid's rows: label, the column in the per-camera table, and how to format a value.
_GRID = [
    ("medFWHM (px)", "medFwhm", "{:.2f}"),
    ("lines / fiber", "linesPerFiber", "{:.1f}"),
    ("flagged (%)", "pctFlagged", "{:.1f}"),
    ("trace centre Δx (px)", "medDxCenter", "{:+.3f}"),
    ("x offset from calib (px)", "xOffset", "{:+.3f}"),
    ("y offset from calib (px)", "yOffset", "{:+.3f}"),
    ("sky residual (km/s)", "kms", "{:+.2f}"),
]


def _chip(status: str, extra: str = "") -> str:
    return f"<span class='chip {extra}{_CLASS.get(status, 'unknown')}'>{status}</span>"


def _cameraKey(camera: str) -> tuple[int, int]:
    """Sort key for a camera name (``r3``): by arm in ``brnm`` order, then spectrograph."""
    arm = camera[0]
    return (ARMS.find(arm) if arm in ARMS else len(ARMS), int(camera[1:] or 0))


def _page(title: str, body: str) -> str:
    """Wrap a body in the stand-alone document skeleton with the shared theme (light)."""
    theme = _theme().replace("{{FONT_FACES}}", _fontFaces())
    return (
        '<!doctype html>\n<html lang="en" data-theme="light">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n<style>\n{theme}"
        "  .verdict { font-size: 1.05rem; padding: 2px 12px; }\n"
        "  td.cam, th.cam { text-align: center; }\n"
        "  thead th.camhead { text-align: center; }\n"
        "  thead th.camhead .chip { display: block; margin-top: 3px; }\n"
        "</style>\n</head>\n<body>\n<main>\n"
        f"{body}\n</main>\n</body>\n</html>\n"
    )


def _header(visit: int, vrow: pd.Series | None, info: pd.Series | None) -> str:
    """Return the page header: the visit, when it was taken, what it is, and the overall verdict."""
    bits = []
    if vrow is not None:
        if vrow.get("hst"):
            bits.append(f"{html.escape(str(vrow['hst']))} HST")
        if not pd.isna(vrow.get("exptime")):
            bits.append(f"{round(float(vrow['exptime']))} s")
        if vrow.get("label"):
            bits.append(html.escape(str(vrow["label"])))
        if vrow.get("arms"):
            bits.append(f"arms {vrow['arms']}")
    elif info is not None:
        if not pd.isna(info.get("time")):
            bits.append(f"{pd.to_datetime(info['time']):%Y-%m-%d %H:%M} HST")
        if isinstance(info.get("cameras"), str):
            bits.append(html.escape(info["cameras"]))
    sequence = ""
    if info is not None:
        name = info.get("sequenceName") or info.get("sequenceType")
        ended = info.get("sequenceEnd")
        if isinstance(name, str) and name:
            sequence = f"Sequence: {html.escape(name)}"
            if isinstance(ended, str) and ended:
                sequence += f" ({html.escape(ended)})"
    verdict = ""
    if vrow is not None and vrow.get("status"):
        verdict = (
            f"<span class='chip verdict {_CLASS.get(vrow['status'], 'unknown')}'>{vrow['status']}</span>"
        )
    meta = " · ".join(bits)
    return (
        f"<header>\n<h1>Visit {visit}</h1>\n"
        + (f"<p class='meta'>{meta}</p>\n" if meta else "")
        + (f"<p class='meta'>{sequence}</p>\n" if sequence else "")
        + (f"<p>{verdict}</p>\n" if verdict else "")
        + "</header>"
    )


def _links() -> str:
    return (
        "<section class='text notes'><p class='meta'>Back to the "
        "<a href='/'>night page</a> or the <a href='/run'>run report</a>. "
        "Every verdict here is the gate's, read back from qadb; this page judges nothing. "
        "Pipeline PDFs and the line-residual image are not yet shown.</p></section>"
    )


def _detectorTable(perCam: pd.DataFrame) -> str:
    """Return the per-detector verdicts and the gate's reason."""
    rows = []
    for row in perCam.itertuples():
        reason = html.escape(row.reason) if isinstance(row.reason, str) and row.reason else ""
        rows.append(
            f"<tr><th scope='row'>{row.camera}</th><td class='l'>{_chip(row.status)}</td>"
            f"<td class='l'>{html.escape(str(row.frameType)) if isinstance(row.frameType, str) else '–'}</td>"
            f"<td class='l why'>{reason or '–'}</td></tr>"
        )
    return (
        "<section class='block'><h2>Detectors</h2>"
        "<p>Each detector's verdict and the gate's reason, as stored in qadb.</p>"
        "<div class='tablewrap'><table><thead><tr><th scope='col' class='l'>Detector</th>"
        "<th scope='col' class='l'>Verdict</th><th scope='col' class='l'>Frame type</th>"
        "<th scope='col' class='l'>Reason</th></tr></thead><tbody>\n"
        + "\n".join(rows)
        + "\n</tbody></table></div></section>"
    )


def _grid(perCam: pd.DataFrame) -> str:
    """Return the metric × camera grid: stored values per detector, columns coloured by verdict."""
    cameras = sorted(perCam["camera"], key=_cameraKey)
    byCam = perCam.set_index("camera")
    head = "".join(
        f"<th scope='col' class='camhead'>{camera}{_chip(byCam.loc[camera, 'status'])}</th>"
        for camera in cameras
    )
    body = []
    for label, column, fmt in _GRID:
        if column not in perCam.columns or perCam[column].isna().all():
            continue
        cells = []
        for camera in cameras:
            value = byCam.loc[camera, column]
            cells.append(f"<td class='cam'>{fmt.format(value) if pd.notna(value) else '–'}</td>")
        body.append(f"<tr><th scope='row' class='l'>{label}</th>{''.join(cells)}</tr>")
    if not body:
        return ""
    return (
        "<section class='block'><h2>Measurements by detector</h2>"
        "<p>The values the QA stored for this visit, as measured; each column is coloured by the detector's "
        "verdict. The limits that decided a flag are in the detector's reason above (a visit may have been "
        "judged with a different threshold set than the one in force now).</p>"
        f"<div class='tablewrap'><table><thead><tr><th scope='col' class='l'>Metric</th>{head}</tr></thead>"
        "<tbody>\n" + "\n".join(body) + "\n</tbody></table></div></section>"
    )


def buildVisitPage(tables: Any, info: pd.DataFrame | None, visit: int, now: datetime | None = None) -> str:
    """Return the per-visit page for ``visit`` as one stand-alone HTML document.

    Parameters
    ----------
    tables : mapping
        The store's tables (``iqQaMetrics``, ``dmLineResidualSummary``,
        ``dmLineResidualMetrics``, ``qaGate``), the whole collection or a slice;
        only ``visit``'s rows are used.
    info : `pandas.DataFrame`, optional
        The opdb's visit information (`runReport.readVisitInfo`), for the time,
        exposure, sequence and cameras; None without the opdb.
    visit : `int`
        The visit to show.
    now : `datetime.datetime`, optional
        The time the page was made (HST); unused for now, kept for symmetry.
    """
    now = datetime.now(HST) if now is None else now
    # Normalise to the tables `measure` needs, so a slice missing one (before the first read) still renders.
    tables = {
        name: tables.get(name, pd.DataFrame())
        for name in ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics", "qaGate")
    }
    images = measure(tables)
    images = images[images["visit"] == int(visit)]
    infoRow = None
    if info is not None and "visit" in info.columns:
        match = info[info["visit"] == int(visit)]
        infoRow = match.iloc[0] if len(match) else None

    if images.empty:
        # No QA rows: an engineering or not-yet-reduced visit. Show what the opdb knows.
        note = (
            "<section class='block'><p>No QA result for this visit: it has not been reduced, or the "
            "reduction stored nothing. The night page shows it as awaiting QA.</p></section>"
        )
        return _page(f"Visit {visit}", _header(int(visit), None, infoRow) + "\n" + note + "\n" + _links())

    infoForTable = info[info["visit"] == int(visit)] if infoRow is not None else None
    vrow = visitTable(images, infoForTable).iloc[0]

    iq = tables["iqQaMetrics"].copy()
    iq = iq[iq["visit"] == int(visit)]
    iq["camera"] = iq["arm"].astype(str) + iq["spectrograph"].astype(str)
    keep = [c for c in ("medFwhm", "linesPerFiber", "pctFlagged", "medDxCenter") if c in iq.columns]
    metrics = iq[["camera", *keep]].drop_duplicates("camera")
    perCam = (
        images[["camera", "status", "frameType", "reason", "xOffset", "yOffset", "kms"]]
        .merge(metrics, on="camera", how="left")
        .sort_values("camera", key=lambda s: s.map(_cameraKey))
        .reset_index(drop=True)
    )

    overall = worst(perCam["status"])
    if vrow is not None:
        vrow = vrow.copy()
        vrow["status"] = overall
    body = "\n".join(
        [
            _header(int(visit), vrow, infoRow),
            _detectorTable(perCam),
            _grid(perCam),
            _links(),
        ]
    )
    title = f"Visit {visit}"
    if vrow is not None and vrow.get("label"):
        title = f"Visit {visit} — {vrow['label']}"
    return _page(title, body)
