"""Write the run report of a collection's verdicts: one stand-alone HTML page.

The report gives every visit of a collection one verdict, the worst among its
detectors, and shows the run at a glance: a square per visit by night, counts by
kind of frame, what was measured on the science frames, and the visits that do
not pass.

It judges nothing: each image's verdict and reason are its row of qadb's
``qa_gate``, which drp_qa's gate (`pfs.drp.qa.metrics.gate`) wrote, on every
kind of frame. The checks the gate applies, and their limits, are drp_qa's
(`pfs.drp.qa.metrics.registry.CHECKS`); the page lists them, and draws the
science checks' limits beside what was measured:

- the offset from the calibration in pixels: the traces' in x
  (``medianDeltaX`` of ``Trace``) and the sky lines' in y (``medianDeltaY`` of
  the species furthest off), as measured;
- the sky-line residual after the visit's detectorMap adjustment
  (``medianDeltaY`` of the held-back lines against the visit's own
  detectorMap), in km/s on the best-measured species of the arm
  (`BEST_SPECIES`), with the dispersion at that species' lines (`kmsPerPixel`).

The input is what `pfs.qa.dashboard.live.readCollection` returns
(``iqQaMetrics``, ``dmLineResidualSummary``, ``dmLineResidualMetrics`` and
``qaGate``): a live read, a `qadbSource.readSnapshot`, or a `live.QadbStore`'s
``raw`` tables. `buildReport` is the whole of it; `ReportCache` keeps the page
of a store for the server (`routes`).
"""

import base64
import functools
import html
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pfs.drp.qa.comparison.classify import shakaLabel, shakaOf
from pfs.drp.qa.metrics.registry import CHECKS, FIXED, specFor
from pfs.qa.dashboard.live import GATE, HST, LAST_VISIT

__all__ = [
    "BEST_SPECIES",
    "FRAME_KINDS",
    "Report",
    "ReportCache",
    "buildReport",
    "checkNotes",
    "imageTable",
    "infoFromTables",
    "kmsPerPixel",
    "measure",
    "readVisitInfo",
    "renderPage",
    "routes",
    "scienceSections",
    "sourceText",
    "visitTable",
    "worst",
]

#: Verdicts, worst last.
STATUS_RANK = {"UNKNOWN": 0, "PASS": 1, "WARN": 2, "FAIL": 3}

#: The arms, in the order shown.
ARMS = "brnm"

#: The sky species whose held-back lines give each arm's residual: the best measured there.
BEST_SPECIES = {"b": "OI", "r": "OH", "n": "OH", "m": "OH"}

#: How the report writes a species.
SPECIES_LABEL = {"OI": "[O I]", "OH": "OH"}

#: The report's kind of frame, and what lit it, for each frame type the gate names
#: (`pfs.drp.qa.metrics.registry.FRAME_TYPES`); any other is unclassified.
FRAME_KINDS = {
    "science": ("science", "full"),
    "allsky": ("all-sky", "full"),
    "arc": ("arc", "full"),
    "quartz": ("quartz", "full"),
    "twilight": ("twilight", "full"),
    "iisArc": ("arc", "iis"),
    "iisQuartz": ("quartz", "iis"),
}

#: The report's kind of frame for each ``obsType`` the QA tasks name, for a gate row with no frame type.
KINDS = {"science": "science", "allsky": "all-sky", "arc": "arc", "trace": "quartz", "twilight": "twilight"}

#: How the page names each frame type the gate judges, in the order it lists them.
FRAME_LABELS = {
    "science": "Science",
    "allsky": "All-sky",
    "arc": "Arcs, full slit",
    "quartz": "Quartz, full slit",
    "twilight": "Twilights",
    "iisArc": "Arcs, engineering fibers",
    "iisQuartz": "Quartz, engineering fibers",
}

#: What an unclassified visit is called, by its IIC sequence type.
SEQUENCE_LABELS = {
    "dotRoach": "dot roach",
    "dotRoachInit": "dot roach",
    "fiberProfiles": "fiber profiles",
    "flats": "flat",
    "scienceArc": "arc",
    "scienceTrace": "quartz",
    "arcs": "arc",
    "darks": "darks sequence",
}

#: A visit's status when the opdb says it was taken and qadb holds no QA result for it.
NO_RESULT = "NO RESULT"

#: A visit taken after the latest one judged, with no QA result yet: its result may still be on its way.
AWAITING = "AWAITING"

#: The SpS exposure types the QA tasks judge: a visit of one of these should have a result.
JUDGED_EXP_TYPES = ("object", "arc", "flat")

#: The kind of frame an IIC sequence type takes, for a visit with no QA result to say it; any other is unclassified.
SEQUENCE_KINDS = {
    "scienceObject": "science",
    "scienceObject_windowed": "science",
    "scienceArc": "arc",
    "scienceTrace": "quartz",
}

_IMAGE = ["visit", "arm", "spectrograph"]
_KEY = ["visit", "camera"]
_DATA = Path(__file__).parent / "data"
_INFO = [
    "visit",
    "time",
    "exptime",
    "sequenceType",
    "sequenceName",
    "expType",
    "cameras",
    "iis",
    "sequenceEnd",
    "shaka",
]
_TABLES = ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics", GATE)

_log = logging.getLogger(__name__)

# Every visit that exposed the spectrograph in a range: one row each (a visit is in one sequence at most).
# The design name is read only for the one-fiber-group (shaka) designs (a MOD<k> before a group<n>); a
# science design's name can be a field or target name, which is proprietary, so the CASE returns NULL for it
# and the proprietary names never leave the opdb. `pfs.drp.qa.comparison.classify.shakaOf` parses the rest.
_VISIT_INFO_SQL = """
SELECT v.pfs_visit_id AS visit, v.issued_at AS time, sv.exp_type,
    (SELECT max(e.exptime) FROM sps_exposure e WHERE e.pfs_visit_id = v.pfs_visit_id) AS exptime,
    (SELECT string_agg(c.arm || c.sps_module_id::text, ' ' ORDER BY c.arm, c.sps_module_id)
        FROM sps_exposure e JOIN sps_camera c ON c.sps_camera_id = e.sps_camera_id
        WHERE e.pfs_visit_id = v.pfs_visit_id) AS cameras,
    s.sequence_type, s.name AS sequence_name,
    position('iis' IN lower(s.cmd_str)) > 0 AS iis, st.status_flag, st.cmd_output,
    CASE WHEN d.design_name ~* 'mod[0-9].*group[0-9]' THEN d.design_name END AS design_name
FROM pfs_visit v
JOIN sps_visit sv ON sv.pfs_visit_id = v.pfs_visit_id
LEFT JOIN visit_set vs ON vs.pfs_visit_id = v.pfs_visit_id
LEFT JOIN iic_sequence s ON s.iic_sequence_id = vs.iic_sequence_id
LEFT JOIN iic_sequence_status st ON st.iic_sequence_id = s.iic_sequence_id
LEFT JOIN pfs_design d ON d.pfs_design_id = v.pfs_design_id
WHERE v.pfs_visit_id BETWEEN :first AND :last
"""


def worst(statuses: Iterable[Any]) -> str:
    """Return the worst of some verdicts; UNKNOWN when none is a verdict."""
    known = [status for status in statuses if isinstance(status, str) and status in STATUS_RANK]
    return max(known, key=STATUS_RANK.get) if known else "UNKNOWN"


def _visitLink(visit: Any) -> str:
    """Return a link to a visit's page (``/visit/<n>``); absolute, so it works from ``/run`` too."""
    return f"<a href='/visit/{int(visit)}'>{int(visit)}</a>"


def _skyLimits(arm: str) -> tuple[float, float]:
    """Return the gate's WARN and FAIL on an arm's sky-line residual after the visit's adjustment, in km/s."""
    check = next(
        check for check in CHECKS.values() if "medianDeltaYKms" in check.metrics and arm in check.arms
    )
    return check.warn, check.fail


def _unit(units: str) -> str:
    return {"pixels": " px", "percent": "%"}.get(units, f" {units}")


def checkNotes() -> list[str]:
    """Return, for each frame type the gate judges, the checks it applies and their limits, as HTML items.

    From drp_qa's `pfs.drp.qa.metrics.registry.CHECKS`, so the page says what the gate that wrote its
    verdicts applies.
    """
    items = []
    for frame, label in FRAME_LABELS.items():
        rules = []
        for check in CHECKS.values():
            if frame not in check.frames:
                continue
            arms = (
                f" on the {', '.join(check.arms)} arm{'s' if len(check.arms) > 1 else ''}"
                if check.arms
                else ""
            )
            if check.kind == FIXED:
                unit = _unit(specFor(check.metrics[0]).units)
                limits = f"WARN at {check.warn:g}{unit}, FAIL at {check.fail:g}{unit}"
            else:
                limits = (
                    f"WARN at {check.warn:g}%, FAIL at {check.fail:g}% from the {check.by[0]}'s normal value"
                )
            rules.append(f"{html.escape(check.title)}{arms}: {limits}")
        if rules:
            items.append(f"<li><strong>{label}:</strong> {'; '.join(rules)}.</li>")
    return items


def kmsPerPixel() -> pd.DataFrame:
    """Return km/s per pixel in y on each detector, at each sky species' lines and at the detector's centre.

    Returns
    -------
    `pandas.DataFrame`
        ``camera``, ``species`` (``centre`` for the detector's centre) and
        ``kmsPerPixel``: the median over science frames of
        ``DetectorMap.getDispersion`` at the held-back lines of the species,
        with the lines' median ``wavelength`` (nm) and the ``frames`` behind it.
    """
    return pd.read_csv(_DATA / "kmsPerPixel.csv")


def _keyed(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Return a Butler-shaped table's image keys and ``columns`` (missing ones empty), with ``camera``."""
    out = frame.reindex(columns=[*_IMAGE, *columns]).copy()
    out["visit"] = pd.to_numeric(out["visit"]).astype("int64")
    out["spectrograph"] = pd.to_numeric(out["spectrograph"]).astype("int64")
    # Plain objects, not pandas' own strings: two empty tables keyed by those fail to merge (pandas 3.1).
    out["arm"] = out["arm"].astype(str).astype(object)
    out["camera"] = (out["arm"] + out["spectrograph"].astype(str)).astype(object)
    return out


def imageTable(tables: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Return one row per detector image: what it is and its gate verdict.

    Parameters
    ----------
    tables : mapping
        From `pfs.qa.dashboard.live.readCollection`.

    Returns
    -------
    `pandas.DataFrame`
        ``visit``, ``arm``, ``spectrograph``, ``camera``; ``obsType``, the
        gate's ``frameType`` and the report's ``kind`` of frame (from the frame
        type, else from ``obsType``); ``illumination`` (``full`` where not
        stored); ``lamp`` on arcs and quartz; the gate's ``status`` (UNKNOWN
        for an image with no gate row), ``decidedBy`` and ``reason`` (empty
        unless WARN or FAIL); ``cameraSet``; and ``withM``, whether the visit
        was read with the m arm.
    """
    iq = _keyed(tables["iqQaMetrics"], ["obsType", "illumination", "seqName"]).drop_duplicates(
        _KEY, keep="last"
    )
    dm = _keyed(tables["dmLineResidualSummary"], ["obsType", "cameraSet"])
    dm = dm.rename(columns={"obsType": "dmObsType"}).drop_duplicates(_KEY, keep="last")
    gate = _keyed(tables.get(GATE, pd.DataFrame()), ["frameType", "qaStatus", "qaDecidedBy", "qaReason"])
    gate = gate.rename(columns={"qaStatus": "status", "qaDecidedBy": "decidedBy", "qaReason": "reason"})
    gate = gate.drop_duplicates(_KEY, keep="last")
    on = [*_IMAGE, "camera"]
    images = iq.merge(dm, on=on, how="outer").merge(gate, on=on, how="outer")
    images["obsType"] = images["obsType"].where(images["obsType"].notna(), images["dmObsType"])
    framed = images["frameType"].map(FRAME_KINDS)
    byFrame = framed.map(lambda pair: pair[0], na_action="ignore")
    images["kind"] = byFrame.where(images["frameType"].notna(), images["obsType"].map(KINDS)).fillna("other")
    lit = framed.map(lambda pair: pair[1], na_action="ignore")
    images["illumination"] = images["illumination"].where(images["illumination"].notna(), lit).fillna("full")
    images["lamp"] = images["seqName"].where(images["kind"].isin(["arc", "quartz"]))
    images["status"] = images["status"].where(images["status"].isin(list(STATUS_RANK)), "UNKNOWN")
    flagged = images["status"].isin(["WARN", "FAIL"])
    images["reason"] = images["reason"].where(flagged & images["reason"].notna(), "").astype(str)
    images["decidedBy"] = images["decidedBy"].fillna("").astype(str)
    withM = set(images.loc[images["arm"] == "m", "visit"])
    cameraSet = images["cameraSet"].where(images["cameraSet"].notna(), "").astype(str)
    images["withM"] = images["visit"].isin(withM) | cameraSet.str.contains("m")
    return images.drop(columns="dmObsType").sort_values(_KEY, ignore_index=True)


def _calibrationOffsets(tables: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Return the median offsets from the calibration, one row per image, species and direction."""
    metrics = _keyed(
        tables["dmLineResidualMetrics"], ["reference", "description", "statusType", "metric", "value"]
    )
    metrics["value"] = pd.to_numeric(metrics["value"], errors="coerce")
    return metrics[metrics["metric"].isin(["medianDeltaX", "medianDeltaY"])].dropna(subset=["value"])


def measure(tables: Mapping[str, pd.DataFrame], scale: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return `imageTable` with what was measured on the science frames, as measured.

    Parameters
    ----------
    tables : mapping
        From `pfs.qa.dashboard.live.readCollection`.
    scale : `pandas.DataFrame`, optional
        `kmsPerPixel`, by default.

    Returns
    -------
    `pandas.DataFrame`
        `imageTable` and, on science frames: ``xOffset`` (the traces' median
        offset from the calibration, pixels), ``yOffset`` (that of the sky
        species furthest off, pixels, signed), ``yOffsetKms`` (the largest
        species' offset in km/s, each with the dispersion at its own lines)
        and ``kms`` (the residual of the arm's `BEST_SPECIES` after the visit's
        adjustment, km/s).
    """
    scale = kmsPerPixel() if scale is None else scale
    scale = scale.rename(columns={"species": "description"})[["camera", "description", "kmsPerPixel"]]
    images = imageTable(tables)
    science = images.loc[images["kind"] == "science", _KEY]
    offsets = _calibrationOffsets(tables).merge(science, on=_KEY)
    calib = offsets[(offsets["reference"] == "calib") & offsets["statusType"].isin(["BOTH", "UNFITTED"])]
    isTrace = calib["description"] == "Trace"
    x = calib[isTrace & (calib["metric"] == "medianDeltaX")].groupby(_KEY)["value"].first()
    lines = calib[~isTrace & (calib["metric"] == "medianDeltaY")]
    lines = lines.assign(size=lines["value"].abs()).sort_values("size", kind="stable")
    y = lines.groupby(_KEY)["value"].last()
    inKms = lines.merge(scale, on=["camera", "description"])
    yKms = (inKms["size"] * inKms["kmsPerPixel"]).groupby([inKms["visit"], inKms["camera"]]).max()
    held = offsets[
        (offsets["reference"] == "visit")
        & (offsets["statusType"] == "RESERVED")
        & (offsets["metric"] == "medianDeltaY")
        & (offsets["description"] == offsets["arm"].map(BEST_SPECIES))
    ].merge(scale, on=["camera", "description"])
    kms = (held["value"] * held["kmsPerPixel"]).groupby([held["visit"], held["camera"]]).first()
    for name, values in (("xOffset", x), ("yOffset", y), ("yOffsetKms", yKms), ("kms", kms)):
        images = images.join(values.rename(name).astype("float64"), on=_KEY)
    return images


def readVisitInfo(opdb: Any, first: int, last: int) -> pd.DataFrame:
    """Read every visit that exposed the spectrograph in a range from the opdb, in one query.

    Parameters
    ----------
    opdb : `pfs.utils.database.opdb.OpDB`
        The opdb, read-only.
    first, last : `int`
        The visit range, inclusive.

    Returns
    -------
    `pandas.DataFrame`
        One row per visit: ``visit``; ``time`` (``pfs_visit.issued_at``, HST);
        ``exptime`` (the longest SpS exposure, seconds); ``expType`` (the SpS
        exposure type); ``cameras`` (those that exposed, ``b1 n1 r1``);
        ``sequenceType`` and ``sequenceName`` (the IIC sequence's type and name,
        e.g. ``Arc: Neon``, both missing outside any sequence); ``iis`` (whether
        the sequence's command lit the engineering fibers);
        ``sequenceEnd``, how the sequence ended (``completed``, or what it
        reported when it did not); and ``shaka``, its shaka label
        (``shaka MOD4 group2``) when it was taken on a one-fiber-group design,
        else ``""`` (`pfs.drp.qa.comparison.classify.shakaOf`).
    """
    read = opdb.query_dataframe(_VISIT_INFO_SQL, params={"first": int(first), "last": int(last)})
    columns = [
        "visit",
        "time",
        "exp_type",
        "exptime",
        "cameras",
        "sequence_type",
        "sequence_name",
        "iis",
        "status_flag",
        "cmd_output",
        "design_name",
    ]
    read = pd.DataFrame(columns=columns) if read is None else read.reindex(columns=columns)
    ended = []
    for flag, output in zip(read["status_flag"], read["cmd_output"], strict=True):
        if pd.isna(flag):
            ended.append(None)
        elif int(flag) == 0:
            ended.append("completed")
        else:
            ended.append(str(output) if isinstance(output, str) and output else "did not complete")
    info = read.rename(
        columns={"sequence_type": "sequenceType", "sequence_name": "sequenceName", "exp_type": "expType"}
    )
    info["sequenceEnd"] = pd.Series(ended, index=info.index, dtype=object)
    # A shaka exposure (one fiber group lit) is labelled as such; a non-shaka design gives "".
    info["shaka"] = [shakaLabel(*shakaOf(name)) for name in info["design_name"]]
    info = info.reindex(columns=_INFO).drop_duplicates("visit")
    info["visit"] = info["visit"].astype("int64")
    info["time"] = pd.to_datetime(info["time"])
    info["exptime"] = pd.to_numeric(info["exptime"], errors="coerce")
    return info.sort_values("visit", ignore_index=True)


def infoFromTables(tables: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Return the visits' times as a qadb read holds them, for a report made without the opdb.

    A snapshot's ``visitInfo`` (`readVisitInfo`, kept by the snapshot command)
    when it has one; else qadb's own visit times, with no exposure time and no
    ``sequenceType`` column, which cannot tell a visit qadb holds no result for.
    """
    if "visitInfo" in tables:
        return tables["visitInfo"].reindex(columns=_INFO)
    times = tables.get("visitTimes")
    if times is None:
        return pd.DataFrame(columns=["visit", "time"])
    info = times.rename(columns={"pfs_visit_id": "visit", "time_exp_start": "time"})[["visit", "time"]]
    return info.assign(time=pd.to_datetime(info["time"]))


def sourceText(collection: str, opdb: bool, readOn: str | None = None) -> str:
    """Return the report's note on where its results come from, as HTML.

    Parameters
    ----------
    collection : `str`
        The collection read from qadb.
    opdb : `bool`
        Whether the visits' times come from the opdb (else qadb's).
    readOn : `str`, optional
        The date qadb was read (HST); the date the page is made when None.
    """
    return (
        f"qadb's latest result for each image in <code>{html.escape(collection)}</code>, read on "
        f"{html.escape(readOn) if readOn else '{date}'} (HST), and visit times from {'the opdb' if opdb else 'qadb'}."
    )


def _shorten(reason: str) -> str:
    """Return the stored reasons in the report's words, cut to a readable length."""
    reason = re.sub(r"(imageQualityQa|dmLineResiduals|twilightQa): ", "", reason)
    words = (
        (" threshold ", " limit "),
        ("robustRmsDelta", "scatter "),
        ("medianDelta", "offset "),
        ("medFWHM", "width"),
        ("linesPerFiber", "lines per fiber"),
    )
    for old, new in words:
        reason = reason.replace(old, new)
    return reason if len(reason) <= 170 else reason[:167] + "…"


def _first(values: pd.Series) -> str:
    known = values.dropna()
    return str(known.iloc[0]) if len(known) else ""


def _takenRow(row: Any, status: str, letter: str, why: str) -> dict:
    """Return a visit row for a visit the opdb says was taken but qadb holds no verdict for."""
    cameras = row.cameras.split() if isinstance(row.cameras, str) else []
    arms = "".join(
        sorted({camera[0] for camera in cameras}, key=lambda arm: ARMS.find(arm) % (len(ARMS) + 1))
    )
    kind = SEQUENCE_KINDS.get(row.sequenceType, "other")
    # The arc lamp (and quartz) from the opdb's sequence name, so an arc shows its lamp before QA runs.
    sequenceName = getattr(row, "sequenceName", None)
    lamp = sequenceName if kind in ("arc", "quartz") and isinstance(sequenceName, str) else ""
    return {
        "visit": int(row.visit),
        "kind": kind,
        "lamp": lamp,
        "illumination": {True: "iis", False: "full"}.get(row.iis, ""),
        "arms": arms,
        "detectors": len(cameras),
        "status": status,
        "flagged": "",
        "detectorsStatus": " ".join(f"{camera}{letter}" for camera in cameras),
        "why": why,
    }


def _noResultRows(info: pd.DataFrame, judgedVisits: set[int], awaiting: bool = False) -> list[dict]:
    """Return a row for each visit the opdb says was taken, and should have been judged, that was not.

    Live results arrive in visit order, so a visit older than the latest one judged was passed over
    (`NO_RESULT`); a newer one may still be on its way. With ``awaiting``, each newer one gets a row too
    (`AWAITING`), for the night page's "what is being observed now"; without it they are left out, as on
    the run report.
    """
    if not judgedVisits or "expType" not in info.columns:
        return []
    latest = max(judgedVisits)
    taken = info[info["expType"].isin(JUDGED_EXP_TYPES) & ~info["visit"].isin(judgedVisits)]
    rows = []
    for row in taken.itertuples():
        if row.visit < latest:
            ended = row.sequenceEnd if isinstance(row.sequenceEnd, str) and row.sequenceEnd else None
            why = {None: "not in a sequence", "completed": "sequence completed"}.get(
                ended, f"sequence ended: {ended}"
            )
            rows.append(_takenRow(row, NO_RESULT, "N", why))
        elif awaiting and row.visit > latest:
            rows.append(_takenRow(row, AWAITING, "A", "taken; QA not in yet"))
    return rows


def visitTable(
    images: pd.DataFrame, info: pd.DataFrame | None = None, awaiting: bool = False
) -> pd.DataFrame:
    """Return one row per visit: its worst verdict, the detectors behind it, and when it was taken.

    With the opdb's listing of the visits taken (``info`` from
    `readVisitInfo`), a visit that should have been judged and has no result
    gets a row too, with the status `NO_RESULT`. With ``awaiting``, a visit
    taken after the latest one judged, and not judged, gets an `AWAITING` row
    (the night page shows these; the run report does not).

    Parameters
    ----------
    images : `pandas.DataFrame`
        From `imageTable` or `measure`.
    info : `pandas.DataFrame`, optional
        `readVisitInfo` or `infoFromTables`: ``visit`` and any of ``time``
        (HST), ``exptime`` and ``sequenceType``.

    Returns
    -------
    `pandas.DataFrame`
        Sorted by ``visit``: ``kind``, ``label`` (the kind as shown, with a
        ``· shaka MOD4 group2`` tag on a one-fiber-group exposure), ``lamp``,
        ``illumination``, ``slit``, ``arms``, ``detectors``, ``status``, ``flagged`` (``b1 W n3 F``),
        ``detectorsStatus`` (``b1P b2W ...``), ``why``, ``time``, ``hst``,
        ``night`` (the HST date of the evening), ``exptime`` and
        ``sequenceType``. A `NO_RESULT` row has the cameras that exposed for its
        detectors (``b1N b2N ...``) and how its sequence ended for ``why``.
    """
    rows = []
    for visit, group in images.sort_values("camera").groupby("visit"):
        bad = group[group["status"].isin(["WARN", "FAIL"])]
        reasons = (
            f"{camera}: {reason}"
            for camera, reason in zip(bad["camera"], bad["reason"], strict=True)
            if reason
        )
        arms = "".join(sorted(set(group["arm"]), key=lambda arm: ARMS.find(arm) % (len(ARMS) + 1)))
        rows.append(
            {
                "visit": int(visit),
                "kind": group["kind"].mode().iloc[0],
                "lamp": _first(group["lamp"]),
                "illumination": _first(group["illumination"]),
                "arms": arms,
                "detectors": len(group),
                "status": worst(group["status"]),
                "flagged": " ".join(f"{c} {s[0]}" for c, s in zip(bad["camera"], bad["status"], strict=True)),
                "detectorsStatus": " ".join(
                    f"{c}{s[0]}" for c, s in zip(group["camera"], group["status"], strict=True)
                ),
                "why": _shorten("; ".join(dict.fromkeys(reasons))),
            }
        )
    columns = [
        *("visit", "kind", "lamp", "illumination", "arms", "detectors"),
        *("status", "flagged", "detectorsStatus", "why"),
    ]
    hasSequences = info is not None and "sequenceType" in info.columns
    info = pd.DataFrame(columns=["visit"]) if info is None else info
    rows += _noResultRows(info, {row["visit"] for row in rows}, awaiting=awaiting)
    visits = pd.DataFrame(rows, columns=columns).astype({"visit": "int64"})
    info = info.reindex(columns=_INFO).drop_duplicates("visit").astype({"visit": "int64"})
    visits = visits.merge(info.drop(columns=["expType", "cameras", "iis"]), on="visit", how="left")
    when = pd.to_datetime(visits["time"])
    visits["time"] = when
    visits["hst"] = when.dt.strftime("%Y-%m-%d %H:%M").fillna("")
    visits["night"] = (when - pd.Timedelta(hours=12)).dt.strftime("%Y-%m-%d").fillna("")
    labels = []
    for kind, lamp, sequence in zip(visits["kind"], visits["lamp"], visits["sequenceType"], strict=True):
        if kind == "arc" and lamp:
            labels.append("arc, " + lamp.replace("Arc: ", "").strip())
        elif kind != "other":
            labels.append(kind)
        elif not hasSequences:
            labels.append("unclassified")
        elif isinstance(sequence, str) and sequence:
            labels.append(f"unclassified ({SEQUENCE_LABELS.get(sequence, sequence)})")
        else:
            labels.append("unclassified (no sequence)")
    # A shaka exposure (one fiber group lit) is labelled apart from a fully lit one of the same kind.
    shaka = visits["shaka"].fillna("") if "shaka" in visits else pd.Series("", index=visits.index)
    visits["label"] = [f"{label} · {tag}" if tag else label for label, tag in zip(labels, shaka, strict=True)]
    visits["slit"] = visits["illumination"].map({"full": "full slit", "iis": "engineering fibers"}).fillna("")
    return visits.sort_values("visit", ignore_index=True)


#: The page's typefaces, embedded in it: family, file under ``data/fonts`` and the weights the file covers.
_FONTS = (("Inter", "Inter-latin.woff2", "100 900"), ("Space Grotesk", "SpaceGrotesk-latin.woff2", "300 700"))


@functools.cache
def _theme() -> str:
    """Return the page's shared styles (``data/theme.css``), with ``{{FONT_FACES}}`` still to substitute.

    The run report and the night page share one stylesheet, inlined into each so the page fetches nothing.
    """
    return (_DATA / "theme.css").read_text()


@functools.cache
def _fontFaces() -> str:
    """Return the ``@font-face`` rules carrying the page's fonts in the page, so it fetches nothing."""
    rules = []
    for family, name, weights in _FONTS:
        encoded = base64.b64encode((_DATA / "fonts" / name).read_bytes()).decode("ascii")
        rules.append(
            f'  @font-face {{ font-family: "{family}"; font-style: normal; font-weight: {weights}; '
            f'font-display: swap; src: url(data:font/woff2;base64,{encoded}) format("woff2"); }}'
        )
    return "\n".join(rules)


def _clip(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def _range(values: pd.Series) -> str:
    return f"{values.quantile(0.05):+.2f} to {values.quantile(0.95):+.2f}"


def _strip(group, column, statusColumn, unit, scale, y, cls, tall) -> str:
    """Draw one distribution: its range, the 5th to 95th percentile, the median, and a dot per image outside."""
    values = group[column].dropna()
    if values.empty:
        return ""
    low, high, median = values.quantile(0.05), values.quantile(0.95), values.median()
    middle = y + tall / 2
    parts = [
        f'<line class="whisker {cls}" x1="{scale(values.min())}" x2="{scale(values.max())}" y1="{middle}" y2="{middle}"/>',
        f'<rect class="{cls}" x="{scale(low)}" y="{y}" width="{max(round(scale(high) - scale(low), 1), 3)}" height="{tall}" rx="{tall / 2}"/>',
        f'<rect class="med" x="{scale(median) - 1}" y="{y - 2}" width="2" height="{tall + 4}"/>',
    ]
    flagged = group[statusColumn].isin(["WARN", "FAIL"])
    outside = group[group[column].notna() & ((group[column] < low) | (group[column] > high) | flagged)]
    order = outside[statusColumn].map({"WARN": 1, "FAIL": 2}).fillna(0)
    for row in outside.iloc[np.argsort(order.to_numpy(), kind="stable")].itertuples():
        status, value = getattr(row, statusColumn), getattr(row, column)
        title = f"Visit {row.visit} {row.camera}: {value:+.2f} {unit}"
        if status in ("WARN", "FAIL"):
            parts.append(
                f'<circle class="dot {status.lower()}" cx="{scale(value)}" cy="{middle}" r="3.2">'
                f"<title>{title}, {status}</title></circle>"
            )
        else:
            parts.append(
                f'<circle class="dot" cx="{scale(value)}" cy="{middle}" r="1.5"><title>{title}</title></circle>'
            )
    return "".join(parts)


def _counts(visits: pd.DataFrame) -> str:
    counts = visits["status"].value_counts()
    return "".join(f"<td>{int(counts.get(k, 0)) or '–'}</td>" for k in ("PASS", "WARN", "FAIL", "UNKNOWN"))


def _tally(name: str, visits: pd.DataFrame, noResult: int = 0) -> str:
    counts = visits["status"].value_counts()
    chips = "".join(
        f"<span class='chip {k.lower()}{'' if counts.get(k, 0) else ' zero'}'><b>{int(counts.get(k, 0)):,}</b> {k}</span>"
        for k in ("PASS", "WARN", "FAIL")
    )
    if counts.get("UNKNOWN", 0):
        chips += f"<span class='chip unknown'><b>{int(counts['UNKNOWN'])}</b> UNKNOWN</span>"
    if noResult:
        chips += f"<span class='chip none'><b>{noResult}</b> {NO_RESULT}</span>"
    return f"<li><span class='k'>{name}</span><span class='n'>{len(visits):,} visits</span>{chips}</li>"


def _nightCell(visits: pd.DataFrame) -> str:
    if not (visits["status"] != "PASS").any():
        return f"{len(visits)}"
    warn, fail = int((visits["status"] == "WARN").sum()), int((visits["status"] == "FAIL").sum())
    return f"{len(visits)} <span class='sub'>({warn} WARN, {fail} FAIL)</span>"


def scienceSections(science: pd.DataFrame) -> tuple[str, str, str]:
    """Return the science-frame tables' bodies and the offset axis, drawn against the gate's limits.

    Shared by the run report and the night page. ``science`` is the science images
    of `measure` (``xOffset``, ``yOffset``, ``yOffsetKms``, ``kms`` and ``status``).

    Returns
    -------
    `tuple` [`str`, `str`, `str`]
        The offsets table's rows (one row group per arm, a row per detector with
        `offsetBar`), the offset axis SVG, and the residuals table's rows (with
        `residualBar`), each as HTML; the gate's limits (`CHECKS`) set the scales.
    """
    offsetWarn = CHECKS["skyOffset"].warn
    kms = {arm: _skyLimits(arm) for arm in ARMS}
    scales = kmsPerPixel()
    centre = scales[scales["species"] == "centre"]
    centre = centre.groupby(centre["camera"].str[0])["kmsPerPixel"].median()
    width, height = 260, 28
    span = 1.3 * offsetWarn

    def xo(value: float) -> float:
        return round((_clip(value, -span, span) + span) / (2 * span) * width, 1)

    def offsetBar(camera: str, group: pd.DataFrame) -> str:
        tip = (
            f"{camera}: x offset {_range(group['xOffset'])} px, y offset {_range(group['yOffset'])} px "
            "(5th to 95th percentile)"
        )
        refs = "".join(
            f'<line class="ref" x1="{xo(v)}" x2="{xo(v)}" y1="1" y2="{height / 2 - 1}"/>'
            for v in (-offsetWarn, offsetWarn)
        )
        refs += "".join(
            f'<line class="ref" x1="{xo(v)}" x2="{xo(v)}" y1="{height / 2 + 1}" y2="{height - 1}"/>'
            for v in (-offsetWarn, offsetWarn)
        )
        return (
            f'<svg class="bar" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{tip}">'
            f'<line class="zero" x1="{xo(0)}" x2="{xo(0)}" y1="0" y2="{height}"/>'
            + refs
            + _strip(group, "xOffset", "status", "px in x", xo, 4, "rng", 6)
            + _strip(group, "yOffset", "status", "px in y", xo, 18, "rng2", 6)
            + "</svg>"
        )

    def residualBar(camera: str, group: pd.DataFrame, warn: float, fail: float) -> str:
        def xr(value: float) -> float:
            return round((_clip(value, -1.15 * fail, 1.15 * fail) + 1.15 * fail) / (2.3 * fail) * width, 1)

        values = group["kms"].dropna()
        tip = (
            f"{camera}: residual {_range(values)} km/s (5th to 95th percentile), "
            f"from {values.min():+.2f} to {values.max():+.2f}"
        )
        return (
            f'<svg class="bar" viewBox="0 0 {width} 22" width="{width}" height="22" role="img" aria-label="{tip}">'
            f'<line class="zero" x1="{xr(0)}" x2="{xr(0)}" y1="0" y2="22"/>'
            + "".join(f'<line class="ref" x1="{xr(v)}" x2="{xr(v)}" y1="1" y2="21"/>' for v in (-warn, warn))
            + "".join(f'<line class="lim" x1="{xr(v)}" x2="{xr(v)}" y1="1" y2="21"/>' for v in (-fail, fail))
            + _strip(group, "kms", "status", "km/s", xr, 7, "rng", 8)
            + "</svg>"
        )

    offsetRows, residualRows = [], []
    for arm in ARMS:
        ofArm = science[science["arm"] == arm]
        if ofArm.empty:
            continue
        species = SPECIES_LABEL[BEST_SPECIES[arm]]
        perPixel = f" · 1 px in y is about {centre[arm]:.1f} km/s at the centre of the detector"
        offsetRows.append(
            f'<tr class="arm"><th colspan="9" scope="colgroup">{arm} arm{perPixel if arm in centre else ""}</th></tr>'
        )
        residualRows.append(
            f'<tr class="arm"><th colspan="6" scope="colgroup">{arm} arm · {species} lines · '
            f"WARN at {kms[arm][0]:g} km/s, FAIL at {kms[arm][1]:g} km/s</th></tr>"
        )
        for camera, group in ofArm.groupby("camera"):
            flags = group["status"].isin(["WARN", "FAIL"]).sum()
            if group[["xOffset", "yOffset"]].notna().any(axis=None):
                offsetRows.append(
                    f"<tr><th scope='row'>{camera}</th><td>{len(group):,}</td>"
                    f"<td class='key'>{_range(group['xOffset'])}</td><td>{group['xOffset'].abs().max():.2f}</td>"
                    f"<td class='key'>{_range(group['yOffset'])}</td><td>{group['yOffset'].abs().max():.2f}</td>"
                    f"<td>{group['yOffsetKms'].max():.1f}</td>"
                    f"<td class='viz'>{offsetBar(camera, group)}</td><td>{int(flags)}</td></tr>"
                )
            values = group["kms"].dropna()
            if len(values):
                residualRows.append(
                    f"<tr><th scope='row'>{camera}</th><td>{len(values):,}</td><td class='key'>{values.median():+.2f}</td>"
                    f"<td>{_range(values)}</td><td>{values.abs().max():.2f}</td>"
                    f"<td class='viz'>{residualBar(camera, group, *kms[arm])}</td></tr>"
                )
    ticks = "".join(
        f'<text x="{xo(v)}" y="12" text-anchor="middle">{f"{v:+g}" if v else "0"}</text>'
        for v in (-offsetWarn, -offsetWarn / 2, 0, offsetWarn / 2, offsetWarn)
    )
    offsetAxis = f'<svg class="bar axis" viewBox="0 0 {width} 16" width="{width}" height="16" aria-hidden="true">{ticks}</svg>'
    return "\n".join(offsetRows), offsetAxis, "\n".join(residualRows)


def renderPage(
    images: pd.DataFrame,
    visits: pd.DataFrame,
    title: str = "Visit verdicts",
    source: str = "",
    now: datetime | None = None,
) -> str:
    """Return the report as one HTML document, with its styles, charts and script inline.

    Parameters
    ----------
    images : `pandas.DataFrame`
        From `measure`.
    visits : `pandas.DataFrame`
        `visitTable` of ``images``.
    title : `str`
        The page's title and heading.
    source : `str`
        Where the results were read from, as HTML, for the page's last note
        (`sourceText`); ``{date}`` in it becomes the date of ``now``.
    now : `datetime.datetime`, optional
        When the page was made; the current time (HST) by default.
    """
    # The visits judged, and those the opdb says were taken that have no QA result.
    everyVisit = visits
    noResult = everyVisit[everyVisit["status"] == NO_RESULT]
    visits = everyVisit[everyVisit["status"] != NO_RESULT]

    def unjudged(frame: pd.DataFrame, slit: str | None = None) -> str:
        """Return how many of ``frame``'s kinds of visit have no result, lit through ``slit``; a dash for none."""
        of = noResult[noResult["kind"].isin(frame)]
        of = of if slit is None else of[of["illumination"] == slit]
        return f"<td>{len(of) or '–'}</td>"

    full, engineering = visits["illumination"] == "full", visits["illumination"] == "iis"
    kind = visits["kind"]
    kinds = [
        ("Science", kind == "science", ["science"], None),
        ("All-sky", kind == "all-sky", ["all-sky"], None),
        ("Twilight", kind == "twilight", ["twilight"], None),
        ("Arc, full slit", (kind == "arc") & full, ["arc"], "full"),
        ("Quartz, full slit", (kind == "quartz") & full, ["quartz"], "full"),
        ("Unclassified, full slit", (kind == "other") & full, ["other"], "full"),
        ("Arc, engineering fibers", (kind == "arc") & engineering, ["arc"], "iis"),
        ("Quartz, engineering fibers", (kind == "quartz") & engineering, ["quartz"], "iis"),
        ("Unclassified, engineering fibers", (kind == "other") & engineering, ["other"], "iis"),
    ]
    summary = [
        f"<tr><th scope='row'>{name}</th><td>{int(mask.sum())}</td>{_counts(visits[mask])}"
        f"{unjudged(of, slit)}</tr>"
        for name, mask, of, slit in kinds
    ]
    summary.append(
        f"<tr class='total'><th scope='row'>All</th><td>{len(visits)}</td>{_counts(visits)}"
        f"<td>{len(noResult) or '–'}</td></tr>"
    )

    offsetsHtml, offsetAxis, residualsHtml = scienceSections(images[images["kind"] == "science"])

    nights = [
        f"<tr><th scope='row'>{night}</th><td>{group['visit'].min()} to {group['visit'].max()}</td>"
        f"<td>{_nightCell(group[group['kind'] == 'science'])}</td>"
        f"<td>{_nightCell(group[group['kind'].isin(['twilight', 'all-sky'])])}</td>"
        f"<td>{_nightCell(group[group['kind'].isin(['arc', 'quartz']) & (group['illumination'] == 'full')])}</td>"
        f"<td>{_nightCell(group[group['illumination'] == 'iis'])}</td>"
        f"<td>{int((noResult['night'] == night).sum()) or '–'}</td></tr>"
        for night, group in visits[visits["night"] != ""].groupby("night")
    ]

    def flaggedDetectors(flagged: str) -> str:
        names = {"W": "WARN", "F": "FAIL"}
        pairs = flagged.split()
        return ", ".join(
            f"{camera} {names[letter]}" for camera, letter in zip(pairs[::2], pairs[1::2], strict=True)
        )

    notPassing = visits[(visits["status"] != "PASS") & (visits["illumination"] != "iis")]
    flagged = "\n".join(
        f"<tr><th scope='row'>{_visitLink(r.visit)}</th><td class='l'>{r.hst}</td><td class='l'>{html.escape(r.label)}</td>"
        f"<td class='l'><span class='chip {r.status.lower()}'>{r.status}</span></td>"
        f"<td class='l'>{flaggedDetectors(r.flagged)}</td><td class='l why'>{html.escape(r.why)}</td></tr>"
        for r in notPassing.itertuples()
    )
    missingSection = noResultNote = ""
    if everyVisit["sequenceEnd"].notna().any():
        noResultNote = (
            f"<li><strong>{NO_RESULT}:</strong> a visit that exposed the spectrograph as an object, arc or flat "
            "frame, by the opdb, and is older than the latest visit judged, with no QA result. Results arrive in "
            "the order the visits were taken, so such a visit was passed over.</li>"
        )
    if len(noResult):
        missingRows = "\n".join(
            f"<tr><th scope='row'>{_visitLink(r.visit)}</th><td class='l'>{r.hst}</td><td class='l'>{html.escape(r.label)}</td>"
            f"<td class='l'>{r.slit}</td><td>{'' if pd.isna(r.exptime) else round(float(r.exptime))}</td>"
            f"<td class='l'>{r.arms}</td><td class='l why'>{html.escape(r.why)}</td></tr>"
            for r in noResult.itertuples()
        )
        missingSection = (
            f"<section class='block' id='no-result'><h2>Visits with no QA result ({len(noResult)})</h2>"
            "<p>These visits exposed the spectrograph and are older than the latest visit judged, yet have no QA "
            "result. The last column is how the instrument's sequence ended; why a completed visit has no result "
            "is not recorded here.</p>"
            "<div class='tablewrap'><table><thead><tr><th scope='col' class='l'>Visit</th>"
            "<th scope='col' class='l'>HST</th><th scope='col' class='l'>Kind</th>"
            "<th scope='col' class='l'>Lit through</th><th scope='col'>Exposure (s)</th>"
            "<th scope='col' class='l'>Arms</th><th scope='col' class='l'>Sequence</th></tr></thead>"
            f"<tbody>\n{missingRows}\n</tbody></table></div></section>"
        )
    data = [
        [
            int(r.visit),
            r.hst,
            r.label,
            r.slit,
            "" if pd.isna(r.exptime) else round(float(r.exptime)),
            r.arms,
            r.status,
            r.flagged,
            r.why,
            r.night,
            r.detectorsStatus,
        ]
        for r in everyVisit.itertuples()
    ]

    calibs = visits[kind.isin(["arc", "quartz"]) & full]
    unjudgedCalibs = noResult["kind"].isin(["arc", "quartz"]) & (noResult["illumination"] == "full")
    lead = (
        _tally("Science", visits[kind == "science"], int((noResult["kind"] == "science").sum()))
        + _tally("Calibs", calibs, int(unjudgedCalibs.sum()))
        + _tally("Engineering", visits[engineering], int((noResult["illumination"] == "iis").sum()))
    )
    when = everyVisit["time"].dropna()
    dates = ""
    if len(when):
        first, last = when.min(), when.max()
        dates = f", {first.day} {first:%B} to {last.day} {last:%B %Y} (HST)"
    now = datetime.now(HST) if now is None else now
    source = source.replace("{date}", f"{now:%Y-%m-%d}")
    fields = {
        # First, so the stylesheet it injects still carries ``{{FONT_FACES}}`` for the next substitution.
        "{{THEME}}": _theme(),
        "{{TITLE}}": html.escape(title),
        "{{FONT_FACES}}": _fontFaces(),
        "{{SUMMARY}}": "\n".join(summary),
        "{{OFFSETS}}": offsetsHtml,
        "{{RESIDUALS}}": residualsHtml,
        "{{OAXIS}}": offsetAxis,
        "{{NIGHTS}}": "\n".join(nights),
        "{{FLAGGED}}": flagged,
        "{{NVISITS}}": f"{len(visits):,}",
        "{{NO_RESULT_META}}": f", {len(noResult):,} more with no QA result" if len(noResult) else "",
        "{{NO_RESULT_SECTION}}": missingSection,
        "{{NO_RESULT_NOTE}}": noResultNote,
        "{{NIMAGES}}": f"{len(images):,}",
        "{{FIRST}}": str(everyVisit["visit"].min()) if len(everyVisit) else "–",
        "{{LAST}}": str(everyVisit["visit"].max()) if len(everyVisit) else "–",
        "{{NFLAGGED}}": str(len(notPassing)),
        "{{LEAD}}": lead,
        "{{DATES}}": dates,
        "{{TODAY}}": f"{now:%Y-%m-%d}",
        "{{SOURCE}}": source,
        "{{CHECKS}}": "\n      ".join(checkNotes()),
        # Last, and with "<" escaped: the visits' reasons are qadb's text, inside a script element.
        "{{DATA}}": json.dumps(data, separators=(",", ":")).replace("<", "\\u003c"),
    }
    page = (_DATA / "runReport.html").read_text()
    for key, value in fields.items():
        page = page.replace(key, value)
    return page


@dataclass(frozen=True)
class Report:
    """A collection's report: its images and visits with their verdicts, and the page."""

    images: pd.DataFrame
    visits: pd.DataFrame
    html: str


def buildReport(
    tables: Mapping[str, pd.DataFrame],
    info: pd.DataFrame | None = None,
    title: str = "Visit verdicts",
    source: str = "",
    now: datetime | None = None,
) -> Report:
    """Return the report of what a qadb read holds: its gate verdicts, by image and visit, and the page.

    Parameters
    ----------
    tables : mapping
        From `pfs.qa.dashboard.live.readCollection`.
    info : `pandas.DataFrame`, optional
        The visits' times, exposure times and sequence types (`readVisitInfo`);
        `infoFromTables` when None.
    title, source, now
        As `renderPage` takes them.
    """
    images = measure(tables)
    visits = visitTable(images, infoFromTables(tables) if info is None else info)
    return Report(images, visits, renderPage(images, visits, title=title, source=source, now=now))


def _defaultBuilder(tables: Mapping[str, pd.DataFrame], info: pd.DataFrame | None = None, **options: Any):
    """Build the run report; a `ReportCache`'s default page. Calls `buildReport` by name, so tests may patch it."""
    return buildReport(tables, info=info, **options)


def _sameInfo(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    """Whether two visit-information frames hold the same rows (by the columns the pages read)."""
    cols = [name for name in _INFO if name in a.columns and name in b.columns]
    left = a[cols].sort_values("visit").astype(str).reset_index(drop=True)
    right = b[cols].sort_values("visit").astype(str).reset_index(drop=True)
    return left.equals(right)


class ReportCache:
    """The pages of a store's collection, made again when the store's tables change.

    `update` reads the store's source and makes the pages again if anything
    changed. `watch` has a thread of its own do that every so often, so `page`,
    what the server answers with, is the last page made and never waits on a
    database: a read that fails or hangs leaves the pages as they were, and
    `status` says since when.

    One cache can hold several pages built from the same read (the night page at
    ``/`` and the run report at ``/run``, say): they share one qadb read, one
    visit-information read, and one `built` count, so a browser following any of
    them reloads when the next read brings something new.

    Parameters
    ----------
    store : `pfs.qa.dashboard.live.QadbStore`
        The store: its ``refresh`` is called by each `update`, and its ``raw``
        tables are reported.
    readInfo : callable, optional
        ``readInfo(first, last)`` returns every visit taken in the range
        (`readVisitInfo` over the opdb), read from ``firstVisit`` to the latest
        visit judged. Without it, the store's own visit times are used, and
        no visit can be told to lack a result.
    info : `pandas.DataFrame`, optional
        The visits' information, fixed, in place of ``readInfo`` (a snapshot's).
    firstVisit : `int`, optional
        The first visit of the run; the earliest visit judged when None.
    pages : mapping, optional
        ``{name: (builder, options)}``, the pages to build; each ``builder(tables,
        info=info, **options)`` returns a `Report`. The default is one page,
        ``report``, the run report. The first page is `primary`, what `page` and
        `html` serve.
    **options
        The default page's ``title`` and ``source`` (ignored when ``pages`` is given).

    Attributes
    ----------
    reports : `dict` [`str`, `Report`]
        The last pages made, by name; empty before the first `update`.
    primary : `str`
        The name of the default page (the first of ``pages``).
    built : `int`
        Counts the rounds of pages made.
    readAt : `datetime.datetime`
        When the last `update` finished (HST); None before the first.
    error : `str`
        What the last `poll` failed with; None when it worked.
    readingSince : `datetime.datetime`
        When the `poll` under way began (HST); None between polls.
    interval : `float`
        Seconds between the watching thread's updates; None when not watched.
    """

    def __init__(
        self,
        store: Any,
        readInfo: Callable[[int, int], pd.DataFrame] | None = None,
        info: pd.DataFrame | None = None,
        firstVisit: int | None = None,
        pages: Mapping[str, tuple[Callable, Mapping[str, Any]]] | None = None,
        **options: Any,
    ):
        self.store = store
        self._readInfo = readInfo
        self._info = info
        self._firstVisit = firstVisit
        self._pages = dict(pages) if pages is not None else {"report": (_defaultBuilder, options)}
        self.primary = next(iter(self._pages))
        self._lock = threading.Lock()
        self._version: int | None = None
        self.reports: dict[str, Report] = {}
        #: The visit information last used to build the pages (for the per-visit page); None before the first read.
        self.info: pd.DataFrame | None = info
        self.built = 0
        self.readAt: datetime | None = None
        self.error: str | None = None
        self.readingSince: datetime | None = None
        self.interval: float | None = None
        self._staleSaid = False
        #: Seconds the last `update` spent in each step, in order; a step that raised is absent, so the
        #: missing step is the one a failed read was in. Logged by `poll` when a read is slow.
        self._timings: dict[str, float] = {}

    @property
    def report(self) -> Report | None:
        """The last `primary` page made (the run report, by default); None before the first `update`."""
        return self.reports.get(self.primary)

    def update(self) -> bool:
        """Read the sources and make the pages again if anything changed; return whether it did.

        This reads qadb, and the opdb for the visits taken: call it where waiting is no harm (before
        serving, and then on `watch`'s thread). A new exposure, even before it is judged, makes the
        pages again, so the night page stays live during a run that is not being reduced.
        """
        with self._lock:
            # Time each step so a slow read says which source it was (qadb, the opdb, or the rebuild);
            # each is recorded as it ends, so a read that raised leaves the step it died in absent.
            self._timings = {}
            mark = time.monotonic()
            self.store.refresh()
            mark, self._timings["qadb"] = time.monotonic(), time.monotonic() - mark
            version = self.store.version
            qadbChanged = not self.reports or self._version != version
            info, infoChanged = self._refreshInfo()
            mark, self._timings["opdb"] = time.monotonic(), time.monotonic() - mark
            changed = qadbChanged or infoChanged
            if changed:
                # A store that has read no image yet holds no table: the page then says so, with no visit.
                tables = {name: self.store.raw.get(name, pd.DataFrame(columns=_IMAGE)) for name in _TABLES}
                # Together, so a page is never served under another's count.
                self.reports = {
                    name: builder(tables, info=info, **options)
                    for name, (builder, options) in self._pages.items()
                }
                self.info = info
                self.built += 1
                self._version = version
            self._timings["rebuild"] = time.monotonic() - mark
            self.readAt = datetime.now(HST)
            self.error = None
            return changed

    def _refreshInfo(self) -> tuple[pd.DataFrame | None, bool]:
        """Return the visit information to build with, and whether it changed since the last read.

        A fixed ``info`` never changes. Without the opdb, qadb's own visit times are used. Otherwise the
        first read covers the whole range; later reads re-read only from the earliest visit whose info can
        still change — a new exposure, or a visit in a sequence that has not ended — to the latest visit
        taken, so settled visits are never read again. (A sequence that never records an end keeps its
        visits in the re-read window; the window is at worst that one sequence, not the whole run.)
        """
        if self._info is not None:
            return self._info, False
        raw = self.store.raw
        visits = sorted(
            {int(v) for name in _TABLES for v in raw.get(name, pd.DataFrame(columns=_IMAGE))["visit"]}
        )
        if self._readInfo is None or not visits:
            return self.store.times, False
        first = self._firstVisit or visits[0]
        if self.info is None:
            return self._readInfo(first, LAST_VISIT), False
        lastMax = int(self.info["visit"].max()) if len(self.info) else first - 1
        inFlight = (
            self.info.loc[self.info["sequenceType"].notna() & self.info["sequenceEnd"].isna(), "visit"]
            if {"sequenceType", "sequenceEnd"} <= set(self.info.columns)
            else []
        )
        start = min([lastMax + 1, *(int(v) for v in inFlight)])
        fresh = self._readInfo(start, LAST_VISIT)
        old = self.info[self.info["visit"] >= start]
        if (fresh is None or not len(fresh)) and not len(old):
            return self.info, False
        fresh = self.info.iloc[:0] if fresh is None else fresh
        kept = self.info[self.info["visit"] < start]
        merged = pd.concat([kept, fresh], ignore_index=True).sort_values("visit", ignore_index=True)
        return merged, not _sameInfo(fresh, old)

    def poll(self) -> None:
        """`update`, keeping the last page and noting the ``error`` when it fails.

        A read that took longer than `status` waits before calling the results stale is logged
        when it ends, with how long it took.
        """
        self.readingSince = datetime.now(HST)
        try:
            self.update()
        except Exception as error:
            _log.exception("Reading the results failed; keeping the last page")
            lines = f"{type(error).__name__}: {error}".splitlines()
            self.error = lines[0][:200] if lines else type(error).__name__
        finally:
            start, self.readingSince = self.readingSince, None
            took = (datetime.now(HST) - start).total_seconds()
            if took > self._staleAfter():
                steps = ", ".join(f"{name} {secs:.0f} s" for name, secs in self._timings.items())
                _log.warning(
                    "Reading the results took %.0f s (from %s HST)%s",
                    took,
                    f"{start:%H:%M:%S}",
                    f": {steps}" if steps else "",
                )
            self._staleSaid = False

    def _staleAfter(self) -> float:
        """Seconds without a read that worked after which a watched cache's results are stale."""
        return max(3 * (self.interval or 0.0), 90.0)

    def watch(self, interval: float) -> threading.Thread:
        """Start a thread that calls `poll` every ``interval`` seconds, and return it."""
        self.interval = float(interval)

        def loop() -> None:
            while True:
                time.sleep(interval)
                self.poll()

        thread = threading.Thread(target=loop, name="runReport", daemon=True)
        thread.start()
        return thread

    def html(self, name: str | None = None) -> str:
        """Return a page for the store as it is now, reading the store's source first (``primary`` by default)."""
        self.update()
        return self.reports[name or self.primary].html

    @property
    def page(self) -> str:
        """The last `primary` page made, served (see `pageFor`)."""
        return self.pageFor(self.primary)

    def pageFor(self, name: str) -> str:
        """Return the named page made last, which reads nothing; when watched, with the `status` script.

        That script reloads the page when a newer one has been made, and says when the results
        were last read. A report written to a file has none of it. All the cache's pages share one
        `status` and one `built`, so a browser on any of them reloads together.
        """
        if name not in self.reports:
            self.update()
        # One read of both: the watching thread may make the next page meanwhile.
        report, built = self.reports[name], self.built
        if self.interval is None:
            return report.html
        pollMs = int(1000 * min(15.0, max(2.0, self.interval / 2)))
        live = (_DATA / "liveStatus.html").read_text()
        live = live.replace("{{BUILT}}", str(built)).replace("{{POLL_MS}}", str(pollMs))
        head, end, tail = report.html.rpartition("</body>")
        return f"{head}{live}{end}{tail}"

    def status(self) -> dict:
        """Return what the served page asks for: ``built``, ``readAt``, ``stale``, ``reading`` and ``error``.

        ``readAt`` is the time of the last update that worked (HST, with its date when that is
        not today); ``stale`` is whether a watched cache has gone three intervals, and at least
        90 s, without one; ``reading`` is when the read under way began (HST), empty between
        reads: a stale cache that is reading is waiting on a slow or hung read, one that is not
        has stopped reading. The first stale answer of a wait is logged.
        """
        now = datetime.now(HST)
        readAt, readingSince = self.readAt, self.readingSince

        def when(moment: datetime | None) -> str:
            if moment is None:
                return ""
            return f"{moment:%H:%M:%S}" if moment.date() == now.date() else f"{moment:%Y-%m-%d %H:%M}"

        stale = (
            readAt is not None
            and self.interval is not None
            and (now - readAt).total_seconds() > self._staleAfter()
        )
        if stale and not self._staleSaid:
            self._staleSaid = True
            _log.warning(
                "No read of the results has worked since %s HST; %s",
                when(readAt),
                f"one has been under way since {when(readingSince)} HST"
                if readingSince
                else "none is under way",
            )
        return {
            "built": self.built,
            "readAt": when(readAt),
            "stale": stale,
            "reading": when(readingSince),
            "error": self.error,
        }


def routes(
    cache: ReportCache,
    mounts: Mapping[str, str] | None = None,
    visit: Callable[[int], str] | None = None,
) -> list[tuple[str, type]]:
    """Return the Tornado routes serving a `ReportCache`: its pages, and `status` at ``/status``.

    None of them reads a database: each answers from what the cache last made.

    Parameters
    ----------
    cache : `ReportCache`
        The cache whose pages are served.
    mounts : mapping, optional
        ``{path: pageName}``, where to serve each page; ``{"/": cache.primary}`` by default.
        The pages share ``/status`` (the injected script fetches it relative to the page).
    visit : callable, optional
        ``visit(n)`` returns a per-visit page's HTML; when given, it is served at
        ``/visit/<n>`` (a reserved route the night page links to).
    """
    from tornado.web import RequestHandler

    mounts = mounts or {"/": cache.primary}

    def pageHandler(name: str) -> type:
        class PageHandler(RequestHandler):
            def get(self) -> None:
                self.set_header("Content-Type", "text/html; charset=utf-8")
                self.set_header("Cache-Control", "no-cache")
                self.write(cache.pageFor(name))

        return PageHandler

    class StatusHandler(RequestHandler):
        def get(self) -> None:
            self.set_header("Cache-Control", "no-store")
            self.write(cache.status())

    handlers = [(path, pageHandler(name)) for path, name in mounts.items()]
    handlers.append((r"/status", StatusHandler))
    if visit is not None:

        class VisitHandler(RequestHandler):
            def get(self, number: str) -> None:
                self.set_header("Content-Type", "text/html; charset=utf-8")
                self.write(visit(int(number)))

        handlers.append((r"/visit/([0-9]+)", VisitHandler))
    return handlers
