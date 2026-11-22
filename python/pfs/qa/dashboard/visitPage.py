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
    SPECIES_LABEL,
    _calibrationOffsets,
    _fontFaces,
    _theme,
    kmsPerPixel,
    measure,
    visitTable,
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
        # The title carries the visit's identifying facts, so let the header use the full content
        # width (the shared theme caps it at a 68ch reading column, which wraps the longer title).
        "  header { max-width: none; }\n"
        "  .verdict { font-size: 1.05rem; padding: 2px 12px; }\n"
        "  td.cam, th.cam { text-align: center; }\n"
        "  thead th.camhead { text-align: center; }\n"
        "  thead th.camhead .chip { display: block; margin-top: 3px; }\n"
        "  nav.visitnav { display: flex; justify-content: space-between; gap: 12px;"
        " margin: 0 0 14px; font-size: 0.95rem; }\n"
        "  nav.visitnav .nolink { color: var(--fg-3); }\n"
        # The residual plots: each a full-width figure (the drawing is wide, two rows by three columns),
        # the image scaling to the column so it reads on a phone too.
        "  .residuals { display: flex; flex-direction: column; gap: 20px; }\n"
        "  .residuals figure { margin: 0; scroll-margin-top: 12px; }\n"
        "  .residuals figcaption { font-weight: 600; margin: 0 0 4px; }\n"
        # A 15:8 box (the figure's own shape) reserves the image's height before it loads, so a lazily
        # fetched plot does not shift the page; a spinner sits in the box until the opaque PNG covers it.
        "  .residuals .imgwrap { position: relative; aspect-ratio: 15 / 8; overflow: hidden; cursor: zoom-in;"
        " border: 1px solid var(--rule); border-radius: 4px; background: var(--surface); }\n"
        "  .residuals .imgwrap::before { content: ''; position: absolute; top: 50%; left: 50%; width: 34px;"
        " height: 34px; margin: -17px 0 0 -17px; border: 3px solid var(--rule); border-top-color: var(--fg-3);"
        " border-radius: 50%; animation: residual-spin 0.8s linear infinite; }\n"
        "  .residuals .imgwrap img { display: block; width: 100%; height: 100%; object-fit: contain;"
        " position: relative; z-index: 1; }\n"
        "  @keyframes residual-spin { to { transform: rotate(360deg); } }\n"
        "  @media (prefers-reduced-motion: reduce) { .residuals .imgwrap::before { animation: none; } }\n"
        # Click a plot to enlarge it: a full-screen overlay, filled from the already-loaded image (no refetch).
        "  #lightbox { position: fixed; inset: 0; z-index: 50; display: flex; align-items: center;"
        " justify-content: center; padding: 20px; background: rgba(0, 0, 0, 0.85); cursor: zoom-out; }\n"
        "  #lightbox[hidden] { display: none; }\n"
        "  #lightbox img { max-width: 100%; max-height: 100%; object-fit: contain; }\n"
        "</style>\n</head>\n<body>\n<main>\n"
        f"{body}\n</main>\n</body>\n</html>\n"
    )


def _header(visit: int, vrow: pd.Series | None, info: pd.Series | None) -> str:
    """Return the page header: the visit and its identifying facts as the title, the sequence, and the verdict.

    The title carries the visit number and what identifies it — when it was taken, the exposure,
    the type and the arms — rather than relegating them to a byline under a bare "Visit N".
    """
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
    title = " · ".join([f"Visit {visit}", *bits])
    return (
        f"<header>\n<h1>{title}</h1>\n"
        + (f"<p class='meta'>{sequence}</p>\n" if sequence else "")
        + (f"<p>{verdict}</p>\n" if verdict else "")
        + "</header>"
    )


def _links(residuals: bool = False) -> str:
    notShown = "Pipeline PDFs are" if residuals else "Pipeline PDFs and the line-residual plots are"
    return (
        "<section class='text notes'><p class='meta'>Back to the "
        "<a href='/'>night page</a> or the <a href='/run'>run report</a>. "
        "Every verdict here is the gate's, read back from qadb; this page judges nothing. "
        f"{notShown} not yet shown.</p></section>"
    )


#: The inline script that enlarges a residual plot on click: a full-screen overlay filled from the
#: already-loaded image (its own ``src``), so no image is fetched twice. Closed by a click or Escape.
_LIGHTBOX_SCRIPT = (
    "<script>\n"
    "(function () {\n"
    "  var box = document.getElementById('lightbox'), big = box.querySelector('img');\n"
    "  document.querySelectorAll('.residuals .imgwrap img').forEach(function (img) {\n"
    "    img.addEventListener('click', function () {\n"
    "      big.src = img.currentSrc || img.src; big.alt = img.alt; box.hidden = false;\n"
    "    });\n"
    "  });\n"
    "  function close() { box.hidden = true; big.removeAttribute('src'); }\n"
    "  box.addEventListener('click', close);\n"
    "  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });\n"
    "})();\n"
    "</script>"
)


def _residualSection(perCam: pd.DataFrame, visit: int) -> str:
    """Return the per-detector detector-map residual plots, in detector order.

    One ``<img>`` per detector, served by the dashboard at ``/visit/<n>/residual/<camera>.png`` and
    drawn on demand from the stored per-line data: the page carries no plot data itself, so it stays
    small and the browser pulls the images lazily and in parallel. Each figure has an ``id`` the
    Detectors table links to, and a click enlarges it (`_LIGHTBOX_SCRIPT`). ``perCam`` is already in
    detector order (``_cameraKey``).
    """
    figures = []
    for row in perCam.itertuples():
        figures.append(
            f"<figure id='resid-{row.camera}'><figcaption>{row.camera} {_chip(row.status)}</figcaption>"
            f"<div class='imgwrap'><img loading='lazy' src='/visit/{int(visit)}/residual/{row.camera}.png' "
            f"alt='DetectorMap line residuals for {row.camera}, visit {int(visit)}'></div></figure>"
        )
    return (
        "<section class='block'><h2>DetectorMap line residuals</h2>"
        "<p>Per detector, the line residuals against the calibration (drift) and against the visit's own "
        "adjusted detectorMap (scatter), drawn from the stored per-line data. Δx is in pixels (left two "
        "columns, per fiber); Δy in km/s where the collection stored it (lamp and sky lines, per wavelength); "
        "the dashed bands are the gate's warn/fail limits in force now. Click a plot to enlarge it.</p>"
        f"<div class='residuals'>\n{chr(10).join(figures)}\n</div></section>"
        "\n<div id='lightbox' hidden><img alt=''></div>"
        f"\n{_LIGHTBOX_SCRIPT}"
    )


def _nav(visit: int) -> str:
    """Navigation row: the previous visit left, the night page centre, the next visit right.

    The previous/next links are deliberately dumb: the neighbours are ``visit - 1``
    and ``visit + 1`` with no check that either exists, so a gap in the numbering
    lands on a no-result page. The previous link is dropped below visit 1 so it never
    points at visit 0.
    """
    prev = (
        f"<a href='/visit/{visit - 1}' rel='prev'>&larr; Visit {visit - 1}</a>"
        if visit > 1
        else "<span class='nolink'>&larr; Visit</span>"
    )
    night = "<a href='/'>Night page</a>"
    nxt = f"<a href='/visit/{visit + 1}' rel='next'>Visit {visit + 1} &rarr;</a>"
    return f"<nav class='visitnav'>{prev}{night}{nxt}</nav>"


def _detectorTable(perCam: pd.DataFrame, residuals: bool = False) -> str:
    """Return the per-detector verdicts and the gate's reason.

    With ``residuals``, each detector name links to its residual plot below (``#resid-<camera>``), so the
    table at the top of the page is also the way to jump to a detector's plot.
    """
    rows = []
    for row in perCam.itertuples():
        reason = html.escape(row.reason) if isinstance(row.reason, str) and row.reason else ""
        name = f"<a href='#resid-{row.camera}'>{row.camera}</a>" if residuals else row.camera
        rows.append(
            f"<tr><th scope='row'>{name}</th><td class='l'>{_chip(row.status)}</td>"
            f"<td class='l'>{html.escape(str(row.frameType)) if isinstance(row.frameType, str) else '–'}</td>"
            f"<td class='l why'>{reason or '–'}</td></tr>"
        )
    jump = " Each detector links to its residual plot below." if residuals else ""
    return (
        "<section class='block'><h2>Detectors</h2>"
        f"<p>Each detector's verdict and the gate's reason, as stored in qadb.{jump}</p>"
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


#: How the per-visit page writes each species name.
_SPECIES = {**SPECIES_LABEL, "O2": "O₂", "NaI": "Na I", "NeI": "Ne I", "ArI": "Ar I", "KrI": "Kr I"}


def _speciesSection(tables: Any) -> str:
    """Return the held-back sky-line residual after the visit's adjustment, per detector and species (km/s)."""
    offsets = _calibrationOffsets(tables)
    if offsets.empty:
        return ""
    held = offsets[
        (offsets["reference"] == "visit")
        & (offsets["statusType"] == "RESERVED")
        & (offsets["metric"] == "medianDeltaY")
    ]
    scale = kmsPerPixel().rename(columns={"species": "description"})[["camera", "description", "kmsPerPixel"]]
    km = held.merge(scale, on=["camera", "description"])
    if km.empty:
        return ""
    km = km.assign(kms=km["value"] * km["kmsPerPixel"])
    cameras = sorted(km["camera"].unique(), key=_cameraKey)
    species = sorted(km["description"].unique())
    pivot = km.pivot_table(index="camera", columns="description", values="kms", aggfunc="first")
    head = "".join(f"<th scope='col' class='cam'>{_SPECIES.get(s, s)}</th>" for s in species)
    rows = []
    for camera in cameras:
        cells = []
        for name in species:
            value = pivot.loc[camera, name] if (camera in pivot.index and name in pivot.columns) else None
            cells.append(f"<td class='cam'>{f'{value:+.2f}' if pd.notna(value) else '–'}</td>")
        rows.append(f"<tr><th scope='row'>{camera}</th>{''.join(cells)}</tr>")
    return (
        "<section class='block'><h2>Sky-line residual by species (km/s)</h2>"
        "<p>The held-back sky lines' median residual after the visit's own detectorMap adjustment, per "
        "species, in km/s. The gate judges the arm's best-measured species (OH on r, n and m; [O I] on b); "
        "the others are shown for context.</p>"
        f"<div class='tablewrap'><table><thead><tr><th scope='col' class='l'>Detector</th>{head}</tr></thead>"
        "<tbody>\n" + "\n".join(rows) + "\n</tbody></table></div></section>"
    )


def buildVisitPage(
    tables: Any,
    info: pd.DataFrame | None,
    visit: int,
    now: datetime | None = None,
    residuals: bool = False,
) -> str:
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
    residuals : `bool`, optional
        Whether detector-map residual plots are served (`ResidualImages`, the
        live ``--butler`` mode): when so, the page shows an ``<img>`` per detector
        pointing at ``/visit/<n>/residual/<camera>.png``. Off by default, so a
        snapshot or a server without the Butler shows no plot.
    """
    now = datetime.now(HST) if now is None else now
    # Normalise to the tables `measure` needs, so a slice missing one (before the first read) still renders.
    tables = {
        name: tables.get(name, pd.DataFrame())
        for name in ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics", "qaGate")
    }
    # Scope to the visit, so the whole-collection tables (an offline render) give one visit's page.
    tables = {
        name: (frame[frame["visit"] == int(visit)] if "visit" in frame.columns else frame)
        for name, frame in tables.items()
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
        body = (
            _nav(int(visit))
            + "\n"
            + _header(int(visit), None, infoRow)
            + "\n"
            + note
            + "\n"
            + _links(residuals)
        )
        return _page(f"Visit {visit}", body)

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

    # vrow["status"] is already the worst of the visit's detectors (visitTable), so it is the overall verdict.
    body = "\n".join(
        [
            _nav(int(visit)),
            _header(int(visit), vrow, infoRow),
            _detectorTable(perCam, residuals),
            _grid(perCam),
            _speciesSection(tables),
            *([_residualSection(perCam, int(visit))] if residuals else []),
            _links(residuals),
        ]
    )
    title = f"Visit {visit}"
    if vrow is not None and vrow.get("label"):
        title = f"Visit {visit} — {vrow['label']}"
    return _page(title, body)
