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
    "calibDriftSection",
    "calibrationDrift",
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

#: A visit whose reduceExposure failed, so QA never ran (drp_qa's ``markReduceFailed`` wrote this verdict to
#: ``qa_gate`` for each exposed detector). A judged verdict, not an awaiting/no-result state, so such a visit
#: reads as a failure rather than as still on its way.
REDUCE_FAILED = "REDUCE FAILED"

#: Verdicts, worst last. REDUCE FAILED (reduceExposure crashed) outranks a QA FAIL: there is no data to judge.
STATUS_RANK = {"UNKNOWN": 0, "PASS": 1, "WARN": 2, "FAIL": 3, REDUCE_FAILED: 4}

#: The CSS chip class per verdict. The multi-word REDUCE FAILED cannot be the lower-cased status (it has a
#: space), so every chip of a verdict goes through this rather than ``status.lower()``.
STATUS_CLASS = {
    "PASS": "pass",
    "WARN": "warn",
    "FAIL": "fail",
    "UNKNOWN": "unknown",
    REDUCE_FAILED: "redfail",
}

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

#: A frame with no QA method (a dark, bias, master or test exposure): taken, and never judged.
NO_QA = "NO QA"

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
    "sequenceId",
    "expType",
    "cameras",
    "iis",
    "sequenceEnd",
    "shaka",
    "design",
]
_TABLES = ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics", GATE)

_log = logging.getLogger(__name__)

# Every visit that exposed the spectrograph in a range: one row each (a visit is in one sequence at most).
# The full design name is read and shown: the dashboard is internal (team access), so a science design's
# field or target name may appear on the pages. It is proprietary, so it must stay out of public records
# (commits, tickets, test fixtures). `pfs.drp.qa.comparison.classify.shakaOf` parses the one-fiber-group
# (shaka) names (a MOD<k> before a group<n>) for the shaka label.
_VISIT_INFO_SQL = """
SELECT v.pfs_visit_id AS visit, v.issued_at AS time, sv.exp_type,
    (SELECT max(e.exptime) FROM sps_exposure e WHERE e.pfs_visit_id = v.pfs_visit_id) AS exptime,
    (SELECT string_agg(c.arm || c.sps_module_id::text, ' ' ORDER BY c.arm, c.sps_module_id)
        FROM sps_exposure e JOIN sps_camera c ON c.sps_camera_id = e.sps_camera_id
        WHERE e.pfs_visit_id = v.pfs_visit_id) AS cameras,
    vs.iic_sequence_id, s.sequence_type, s.name AS sequence_name,
    position('iis' IN lower(s.cmd_str)) > 0 AS iis, st.status_flag, st.cmd_output,
    d.design_name AS design_name
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
    flagged = images["status"].isin(["WARN", "FAIL", REDUCE_FAILED])
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


def measure(
    tables: Mapping[str, pd.DataFrame],
    scale: pd.DataFrame | None = None,
    offsets: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return `imageTable` with what was measured on the science frames, as measured.

    Parameters
    ----------
    tables : mapping
        From `pfs.qa.dashboard.live.readCollection`.
    scale : `pandas.DataFrame`, optional
        `kmsPerPixel`, by default.
    offsets : `pandas.DataFrame`, optional
        `_calibrationOffsets` of ``tables``, computed here when None; pass it to share the one read
        with `calibrationDrift` (both filter the same table) rather than build it twice per report.

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
    offsets = (_calibrationOffsets(tables) if offsets is None else offsets).merge(science, on=_KEY)
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
        e.g. ``Arc: Neon``, both missing outside any sequence); ``sequenceId``
        (``visit_set.iic_sequence_id`` — the IIC sequence the visit belongs to,
        shared by every visit taken under the one command, missing outside any
        sequence); ``iis`` (whether the sequence's command lit the engineering fibers);
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
        "iic_sequence_id",
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
        columns={
            "sequence_type": "sequenceType",
            "sequence_name": "sequenceName",
            "iic_sequence_id": "sequenceId",
            "exp_type": "expType",
        }
    )
    info["sequenceEnd"] = pd.Series(ended, index=info.index, dtype=object)
    # A shaka exposure (one fiber group lit) is labelled as such; a non-shaka design gives "".
    info["shaka"] = [shakaLabel(*shakaOf(name)) for name in info["design_name"]]
    # The full design name, shown on the pages (the dashboard is internal); "" when the opdb has none.
    info["design"] = [name if isinstance(name, str) else "" for name in info["design_name"]]
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


def _sequenceLabel(sequenceId: Any) -> str:
    """Return a visit's IIC sequence id (``visit_set.iic_sequence_id``) as text, or ``""`` when it has none."""
    return "" if pd.isna(sequenceId) else str(int(sequenceId))


def _takenRow(row: Any, status: str, letter: str, why: str) -> dict:
    """Return a visit row for a visit the opdb says was taken but qadb holds no verdict for."""
    cameras = row.cameras.split() if isinstance(row.cameras, str) else []
    arms = "".join(
        sorted({camera[0] for camera in cameras}, key=lambda arm: ARMS.find(arm) % (len(ARMS) + 1))
    )
    if status == NO_QA and isinstance(row.expType, str) and row.expType:
        kind = row.expType  # a no-QA frame (dark, bias, ...) is shown by its exposure type
    else:
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


def _noResultRows(
    info: pd.DataFrame, judgedVisits: set[int], awaiting: bool = False, includeNoQa: bool = False
) -> list[dict]:
    """Return a row for each taken visit that was not judged: passed over, awaiting, or with no QA method.

    A visit of a judged kind (`JUDGED_EXP_TYPES`) that has no result is either older than the latest one
    judged, so passed over (`NO_RESULT`), or newer, so maybe still on its way (`AWAITING`, only with
    ``awaiting`` — the night page's "what is being observed now"). With ``includeNoQa``, a frame of any
    other kind (a dark, bias, master or test exposure) gets a `NO_QA` row: it was taken and has no QA
    method, so it is complete, whatever the latest visit judged.
    """
    if "expType" not in info.columns:
        return []
    notJudged = info[~info["visit"].isin(judgedVisits)]
    rows = []
    if includeNoQa:
        for row in notJudged[~notJudged["expType"].isin(JUDGED_EXP_TYPES)].itertuples():
            rows.append(_takenRow(row, NO_QA, "Q", "no QA method"))
    if judgedVisits:
        latest = max(judgedVisits)
        for row in notJudged[notJudged["expType"].isin(JUDGED_EXP_TYPES)].itertuples():
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
    images: pd.DataFrame,
    info: pd.DataFrame | None = None,
    awaiting: bool = False,
    includeNoQa: bool = False,
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
        ``night`` (the HST date of the evening), ``exptime``,
        ``sequenceType``, and ``sequenceId`` (the IIC sequence id, from
        ``info``). A `NO_RESULT` row has the cameras that exposed for its
        detectors (``b1N b2N ...``) and how its sequence ended for ``why``.
    """
    rows = []
    for visit, group in images.sort_values("camera").groupby("visit"):
        # REDUCE FAILED carries a reason (the returnCode) too, so it belongs with WARN/FAIL here: the
        # reason reaches the tables' "why" column and the flagged-detectors list, as on the visit page.
        bad = group[group["status"].isin(["WARN", "FAIL", REDUCE_FAILED])]
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
    rows += _noResultRows(info, {row["visit"] for row in rows}, awaiting=awaiting, includeNoQa=includeNoQa)
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
    visits["design"] = visits["design"].fillna("").astype(str) if "design" in visits else ""
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
def _clockControls() -> str:
    """Return the shared time-map controls (``data/clockControls.html``), for ``{{CLOCK_CONTROLS}}``.

    The run report and the night page draw the same clock; the night page feeds it one night's visits.
    Paired with `_clockScript`, which reads the page's ``DATA``, ``CLS`` and ``esc``.
    """
    return (_DATA / "clockControls.html").read_text()


@functools.cache
def _clockScript() -> str:
    """Return the shared time-map script (``data/clockScript.js``), for ``{{CLOCK_SCRIPT}}`` inside a page's script."""
    return (_DATA / "clockScript.js").read_text()


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


def _foldedCounts(visits: pd.DataFrame) -> pd.Series:
    """Status counts with REDUCE FAILED folded into FAIL.

    The fixed PASS/WARN/FAIL/UNKNOWN columns and the tile chips total only over those verdicts, so a
    reduceExposure failure counts as a failure in the aggregates; the per-visit rows still carry the
    distinct REDUCE FAILED verdict.
    """
    return visits["status"].replace(REDUCE_FAILED, "FAIL").value_counts()


def _counts(visits: pd.DataFrame) -> str:
    counts = _foldedCounts(visits)
    return "".join(f"<td>{int(counts.get(k, 0)) or '–'}</td>" for k in ("PASS", "WARN", "FAIL", "UNKNOWN"))


def _tally(name: str, visits: pd.DataFrame, noResult: int = 0) -> str:
    counts = _foldedCounts(visits)
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
    warn = int((visits["status"] == "WARN").sum())
    fail = int(visits["status"].isin(("FAIL", REDUCE_FAILED)).sum())  # a reduce failure counts as a failure
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


#: The gate check the daily-calibration drift is judged against (its WARN/FAIL set the chart's limit lines).
_CALIB_CHECK = CHECKS["calibOffset"]

#: The daily calibrations the drift trend covers: full-slit arcs and quartz (traces). Engineering-fiber
#: frames (lit through the IIS) are a different check and are left out.
_CALIB_KINDS = ("arc", "quartz")

#: The arc species the nightly y offset is taken on when it is measured: the nightly arc is Neon.
_NEON = "NeI"

#: The drift chart's layout, in SVG viewBox units (each chart scales to fill its panel's width).
_DW, _GUT, _PADR = 460, 42, 10  # viewBox width, left gutter for the y labels, right padding
_TRACK, _GAP, _TOP, _AXIS = 56, 16, 10, 20  # track height, gap between the two tracks, top padding, x axis


def _arcYOffset(arcs: pd.DataFrame) -> pd.DataFrame:
    """Return the nightly arc's y offset per image: the Neon (``NeI``) lines only.

    The daily arc is ``Arc: Neon``; Neon is in every arm and is the consistent
    nightly signal. A calibration set's other lamps (taken on the set's nights)
    are judged for the set go/no-go, not shown as nightly drift, so an arc image
    with no Neon lines contributes no point here.
    """
    neon = arcs[arcs["description"] == _NEON]
    return neon.groupby(_KEY, as_index=False)["value"].first()[[*_KEY, "value"]]


def _driftStatus(values: pd.Series) -> pd.Series:
    """Return each offset's verdict against the ``calibOffset`` check's own limits (absolute, px)."""
    magnitude = values.abs()
    verdict = np.where(
        magnitude >= _CALIB_CHECK.fail, "FAIL", np.where(magnitude >= _CALIB_CHECK.warn, "WARN", "PASS")
    )
    return pd.Series(verdict, index=values.index).where(values.notna(), "UNKNOWN")


def calibrationDrift(
    tables: Mapping[str, pd.DataFrame],
    visits: pd.DataFrame,
    offsets: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return each daily-calibration image's offset from ``detectorMap_calib``, over the run.

    One row per calibration image and direction the ``calibOffset`` check
    judges: a quartz's trace offset in x (``medianDeltaX`` of ``Trace``) and an
    arc's line offset in y (``medianDeltaY``, the lamp's own species — Neon by
    default, else the species furthest off). Full-slit arcs and quartz only
    (the daily calibrations); engineering-fiber frames are left out.

    Parameters
    ----------
    tables : mapping
        From `pfs.qa.dashboard.live.readCollection`.
    visits : `pandas.DataFrame`
        `visitTable`, for each visit's ``time`` and ``night`` (HST evening date).

    Returns
    -------
    `pandas.DataFrame`
        ``visit``, ``camera``, ``arm``, ``spectrograph``, ``kind``, ``status``
        (the image's gate verdict), ``which`` (``trace`` or ``arc``), ``value``
        (the offset, px, signed), ``time`` and ``night``. Empty when the read
        holds no daily calibration.
    """
    columns = [*_IMAGE, "camera", "kind", "status", "which", "value", "time", "night"]
    images = imageTable(tables)
    calib = images[images["kind"].isin(_CALIB_KINDS) & (images["illumination"] == "full")]
    if calib.empty:
        return pd.DataFrame(columns=columns)
    offsets = _calibrationOffsets(tables) if offsets is None else offsets
    offsets = offsets[(offsets["reference"] == "calib") & (offsets["statusType"] == "BOTH")]
    traces = offsets[(offsets["description"] == "Trace") & (offsets["metric"] == "medianDeltaX")]
    trace = traces.groupby(_KEY, as_index=False)["value"].first().assign(which="trace")
    arcs = offsets[(offsets["metric"] == "medianDeltaY") & (offsets["description"] != "Trace")]
    arc = _arcYOffset(arcs).assign(which="arc")
    drift = pd.concat([trace, arc], ignore_index=True)
    drift = drift.merge(calib[[*_IMAGE, "camera", "kind"]], on=_KEY, how="inner")
    drift = drift.merge(visits[["visit", "time", "night"]].drop_duplicates("visit"), on="visit", how="left")
    # The chart's verdict is the drift's own: this offset against the calibOffset limits, not the image's
    # overall gate verdict (which can fail on an unrelated check, e.g. lines per fiber).
    drift["status"] = _driftStatus(drift["value"])
    return drift.reindex(columns=columns).sort_values([*_KEY, "which"], ignore_index=True)


def _driftTrack(
    points: pd.DataFrame, cls: str, label: str, xt: Callable[[Any], float], y0: int, activeNight: str
) -> str:
    """Draw one detector-and-direction track into a chart: the run band, its median, the zero line, a dot per visit.

    ``points`` is one camera and direction of `calibrationDrift`. The y scale is
    the run's own spread (so drift is visible), centred on zero and labelled at
    its ends. Tonight's points (``night == activeNight``) are larger and ringed;
    WARN/FAIL points take the gate's colour.
    """
    x0, x1 = _GUT, _DW - _PADR
    values = points["value"].dropna()
    if values.empty:
        return (
            f'<text class="tlab {cls}" x="{x0 + 2}" y="{y0 + 11}">{label}</text>'
            f'<text class="ylab" x="{_GUT - 6}" y="{y0 + _TRACK / 2 + 3}">none</text>'
        )
    span = max(values.abs().quantile(0.95), 0.08) * 1.25
    low, high = -span, span

    def ys(value: float) -> float:
        return round(y0 + _TRACK - 3 - (_clip(value, low, high) - low) / (high - low) * (_TRACK - 6), 1)

    p5, p95, median = values.quantile(0.05), values.quantile(0.95), values.median()
    parts = [
        f'<rect class="plot" x="{x0}" y="{y0}" width="{x1 - x0}" height="{_TRACK}"/>',
        f'<rect class="band {cls}" x="{x0}" y="{ys(p95)}" width="{x1 - x0}" height="{max(ys(p5) - ys(p95), 1)}"/>',
        f'<line class="zero" x1="{x0}" x2="{x1}" y1="{ys(0)}" y2="{ys(0)}"/>',
        f'<line class="med {cls}" x1="{x0}" x2="{x1}" y1="{ys(median)}" y2="{ys(median)}"/>',
        f'<text class="tlab {cls}" x="{x0 + 3}" y="{y0 + 11}">{label}</text>',
        f'<text class="ylab" x="{_GUT - 6}" y="{ys(high) + 3}">{high:+.2f}</text>',
        f'<text class="ylab zero" x="{_GUT - 6}" y="{ys(0) + 3}">0</text>',
        f'<text class="ylab" x="{_GUT - 6}" y="{ys(low) + 3}">{low:+.2f}</text>',
    ]
    # The nightly median, drawn as a line through the nights, to show the trajectory.
    nightly = points.dropna(subset=["time"]).groupby("night", sort=True)
    trail = [(xt(group["time"].median()), ys(group["value"].median())) for night, group in nightly if night]
    if len(trail) > 1:
        parts.append(f'<polyline class="trail {cls}" points="{" ".join(f"{x},{y}" for x, y in trail)}"/>')
    order = points["status"].map({"WARN": 1, "FAIL": 2}).fillna(0)
    for row in points.iloc[np.argsort(order.to_numpy(), kind="stable")].itertuples():
        if pd.isna(row.value) or pd.isna(row.time):
            continue
        active = bool(activeNight) and row.night == activeNight
        status = row.status if row.status in ("WARN", "FAIL") else ""
        night = html.escape(str(row.night)) if row.night else "?"
        title = (
            f"Visit {row.visit} {row.camera}, {night}: {row.value:+.2f} px{f', {status}' if status else ''}"
        )
        klass = "dot" + (f" {status.lower()}" if status else "") + (" today" if active else "")
        radius = 4.2 if active else (3.6 if status else 2.4)
        parts.append(
            f'<circle class="{klass}" cx="{xt(row.time)}" cy="{ys(row.value)}" r="{radius}">'
            f"<title>{title}</title></circle>"
        )
    return "".join(parts)


def _dateAxis(t0: Any, t1: Any, y: float) -> str:
    """Draw the chart's x axis: a line and up to five night-date ticks (HST, ``MM-DD``)."""
    if pd.isna(t0) or pd.isna(t1):
        return ""
    x0, x1 = _GUT, _DW - _PADR
    out = [f'<line class="xaxis" x1="{x0}" x2="{x1}" y1="{y - 12}" y2="{y - 12}"/>']
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = round(x0 + frac * (x1 - x0), 1)
        when = t0 if t1 == t0 else t0 + (t1 - t0) * frac
        anchor = "start" if frac == 0 else "end" if frac == 1 else "middle"
        out.append(
            f'<text class="xlab" x="{x}" y="{y}" text-anchor="{anchor}">{pd.Timestamp(when):%m-%d}</text>'
        )
    return "".join(out)


def _driftPanel(
    camera: str, group: pd.DataFrame, t0: Any, t1: Any, activeNight: str, highlight: str = "tonight"
) -> str:
    """Return one detector's drift chart: a wide two-track timeline with its own y and night-date axes."""
    x0, x1 = _GUT, _DW - _PADR
    height = _TOP + 2 * _TRACK + _GAP + _AXIS

    def xt(time: Any) -> float:
        if pd.isna(time) or pd.isna(t0) or t1 == t0:
            return (x0 + x1) / 2
        return round(x0 + 2 + (time - t0) / (t1 - t0) * (x1 - x0 - 4), 1)

    trace = group[group["which"] == "trace"]
    arc = group[group["which"] == "arc"]
    svg = (
        f'<svg class="drift" viewBox="0 0 {_DW} {height}" preserveAspectRatio="xMidYMid meet" '
        f'role="img" aria-label="{camera}: offset from the calibration over the run">'
        + _driftTrack(trace, "rng", "trace x (px)", xt, _TOP, activeNight)
        + _driftTrack(arc, "rng2", "Neon y (px)", xt, _TOP + _TRACK + _GAP, activeNight)
        + _dateAxis(t0, t1, height - 6)
        + "</svg>"
    )
    marked = group[group["night"] == activeNight] if activeNight else group.iloc[:0]
    caption = f"<b>{camera}</b>{_highlightNote(marked, highlight)}"
    return f'<figure class="panel"><figcaption>{caption}</figcaption>{svg}</figure>'


def _highlightNote(marked: pd.DataFrame, highlight: str) -> str:
    """Return a detector panel's note for the highlighted night: its trace/arc medians, flagged if over limit."""
    if marked.empty:
        return f"<span class='sub'>no calibration {highlight}</span>"
    out = []
    for which, label in (("trace", "x"), ("arc", "y")):
        values = marked.loc[marked["which"] == which, "value"].dropna()
        if values.empty:
            continue
        status = worst(marked.loc[marked["which"] == which, "status"])
        chip = f" <span class='chip {status.lower()}'>{status}</span>" if status in ("WARN", "FAIL") else ""
        out.append(f"{label} {values.median():+.2f}{chip}")
    return f"<span class='sub'>{highlight} {' · '.join(out)}</span>" if out else ""


def calibDriftSection(
    drift: pd.DataFrame, activeNight: str = "", by: str = "spectrograph", highlight: str = "tonight"
) -> str:
    """Return the daily-calibration drift charts: columns of per-detector timelines, stacked in time.

    Shared by the run report and the night page. ``drift`` is `calibrationDrift`;
    each detector gets a wide two-track chart (trace offset in x, Neon-arc offset
    in y) of its offset from the calibration over the run, with the run's band and
    tonight's points marked, so today's set can be read against the rest of the run.

    Each group is a **column** of its detectors' charts, stacked on a shared time
    axis so the visits line up vertically and a time-based drift across the column
    is easy to read. ``by`` chooses the column: by ``spectrograph`` (the default —
    a column is one unit's arms, so a shift common to the whole spectrograph, or
    one detector drifting alone, stands out down the column), or by ``arm`` (a
    column is the same detector across the four spectrographs).
    """
    if drift.empty:
        return ""
    t0, t1 = drift["time"].min(), drift["time"].max()
    if by == "arm":
        columns = [(f"{arm} arm", drift[drift["arm"] == arm]) for arm in ARMS]
    else:
        columns = [
            (f"Spectrograph {sm}", drift[drift["spectrograph"] == sm])
            for sm in sorted(drift["spectrograph"].dropna().unique())
        ]
    out = []
    for heading, ofColumn in columns:
        if ofColumn.empty:
            continue
        cameras = sorted(ofColumn["camera"].unique(), key=lambda c: (ARMS.find(c[0]) % 9, int(c[1:])))
        panels = "".join(
            _driftPanel(camera, ofColumn[ofColumn["camera"] == camera], t0, t1, activeNight, highlight)
            for camera in cameras
        )
        out.append(f'<div class="driftcol"><h3 class="colhead">{heading}</h3>{panels}</div>')
    return f'<div class="driftcols">{"".join(out)}</div>' if out else ""


def renderPage(
    images: pd.DataFrame,
    visits: pd.DataFrame,
    title: str = "Visit verdicts",
    source: str = "",
    now: datetime | None = None,
    calibDriftSm: str = "",
    calibDriftArm: str = "",
    calibSet: str = "",
    agSummary: str = "",
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
    agSummary : `str`
        The run's auto-guiding block (`pfs.qa.dashboard.agRun.AgReport.summary`), as HTML; none when empty.
    """
    # The visits judged, those taken with no QA result yet, and those with no QA method (darks, biases, ...).
    everyVisit = visits
    noResult = everyVisit[everyVisit["status"] == NO_RESULT]
    noQa = everyVisit[everyVisit["status"] == NO_QA]
    visits = everyVisit[~everyVisit["status"].isin([NO_RESULT, NO_QA])]

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
        f"{unjudged(of, slit)}<td>–</td></tr>"
        for name, mask, of, slit in kinds
    ]
    # A row per kind of no-QA frame present (darks, biases, ...): shown by exposure type, with no verdict.
    noQaLabels = {"dark": "Darks", "bias": "Biases", "test": "Test frames", "master": "Masters"}
    for nqKind, nqGroup in noQa.groupby("kind"):
        label = noQaLabels.get(str(nqKind), str(nqKind).capitalize())
        summary.append(
            f"<tr><th scope='row'>{html.escape(label)}</th><td>{len(nqGroup)}</td>"
            f"{_counts(nqGroup)}<td>–</td><td>{len(nqGroup)}</td></tr>"
        )
    summary.append(
        f"<tr class='total'><th scope='row'>All</th><td>{len(visits) + len(noQa)}</td>{_counts(visits)}"
        f"<td>{len(noResult) or '–'}</td><td>{len(noQa) or '–'}</td></tr>"
    )

    offsetsHtml, offsetAxis, residualsHtml = scienceSections(images[images["kind"] == "science"])

    nights = [
        f"<tr><th scope='row'>{night}</th><td>{group['visit'].min()} to {group['visit'].max()}</td>"
        f"<td>{_nightCell(group[group['kind'] == 'science'])}</td>"
        f"<td>{_nightCell(group[group['kind'].isin(['twilight', 'all-sky'])])}</td>"
        f"<td>{_nightCell(group[group['kind'].isin(['arc', 'quartz']) & (group['illumination'] == 'full')])}</td>"
        f"<td>{_nightCell(group[group['illumination'] == 'iis'])}</td>"
        f"<td>{int((noResult['night'] == night).sum()) or '–'}</td>"
        f"<td>{int((noQa['night'] == night).sum()) or '–'}</td></tr>"
        for night, group in visits[visits["night"] != ""].groupby("night")
    ]

    def flaggedDetectors(flagged: str) -> str:
        names = {"W": "WARN", "F": "FAIL", "R": "REDUCE FAILED"}
        pairs = flagged.split()
        return ", ".join(
            f"{camera} {names[letter]}" for camera, letter in zip(pairs[::2], pairs[1::2], strict=True)
        )

    notPassing = visits[(visits["status"] != "PASS") & (visits["illumination"] != "iis")]
    flagged = "\n".join(
        f"<tr><th scope='row'>{_visitLink(r.visit)}</th><td class='l'>{r.hst}</td><td class='l'>{html.escape(r.label)}</td>"
        f"<td class='l'><span class='chip {STATUS_CLASS.get(r.status, 'unknown')}'>{r.status}</span></td>"
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
            f"<details class='block' id='no-result'><summary><h2>Visits with no QA result ({len(noResult)})</h2></summary>"
            "<p>These visits exposed the spectrograph and are older than the latest visit judged, yet have no QA "
            "result. The last column is how the instrument's sequence ended; why a completed visit has no result "
            "is not recorded here.</p>"
            "<div class='tablewrap tall'><table><thead><tr><th scope='col' class='l'>Visit</th>"
            "<th scope='col' class='l'>HST</th><th scope='col' class='l'>Kind</th>"
            "<th scope='col' class='l'>Lit through</th><th scope='col'>Exposure (s)</th>"
            "<th scope='col' class='l'>Arms</th><th scope='col' class='l'>Sequence</th></tr></thead>"
            f"<tbody>\n{missingRows}\n</tbody></table></div></details>"
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
            _sequenceLabel(r.sequenceId),
            r.design,
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
    if len(noQa):
        # Frames with no QA method (darks, biases, ...): a count only, since they carry no verdict.
        lead += (
            f"<li><span class='k'>No QA</span><span class='n'>{len(noQa):,} visits</span>"
            f"<span class='chip noqa'><b>{len(noQa):,}</b> NO QA</span></li>"
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
        "{{CLOCK_CONTROLS}}": _clockControls(),
        "{{CLOCK_SCRIPT}}": _clockScript(),
        "{{SUMMARY}}": "\n".join(summary),
        "{{CALIB_SET}}": calibSet,
        "{{AG_SUMMARY}}": agSummary,
        "{{CALIB_DRIFT_SM}}": calibDriftSm,
        "{{CALIB_DRIFT_ARM}}": calibDriftArm,
        "{{OFFSETS}}": offsetsHtml,
        "{{RESIDUALS}}": residualsHtml,
        "{{OAXIS}}": offsetAxis,
        "{{NIGHTS}}": "\n".join(nights),
        "{{FLAGGED}}": flagged,
        "{{NVISITS}}": f"{len(visits):,}",
        "{{NO_RESULT_META}}": "".join(
            f", {n:,} {label}"
            for n, label in ((len(noResult), "more with no QA result"), (len(noQa), "with no QA method"))
            if n
        ),
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
    listing: pd.DataFrame | None = None,
    normalValues: pd.DataFrame | None = None,
    readListing: Callable[[Any, Any], pd.DataFrame] | None = None,
    readNormalValues: Callable[[], pd.DataFrame] | None = None,
    agSummary: str | Callable[[], str] = "",
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
    listing : `pandas.DataFrame`, optional
        The run's opdb visit listing (`pfs.drp.qa.comparison.queries.readVisitListing`), for the
        calibration-set go/no-go section (`pfs.qa.dashboard.calibSet`); None omits that section, as
        for a snapshot or when there is no opdb. Ignored when ``readListing`` is given.
    normalValues : `pandas.DataFrame`, optional
        The collection's normal values (`pfs.drp.qa.qadb.reader.readNormalValues`), for the set's
        gate rule; without them the set go/no-go rests on the sequence rules alone. Ignored when
        ``readNormalValues`` is given.
    readListing, readNormalValues : callable, optional
        ``readListing(start, end)`` and ``readNormalValues()``, read **each build** so a live serve's
        set section stays current (a later rebuild set, newly-accepted normal values) without a
        restart; ``start``/``end`` span the run of ``info``'s visit times. When given they supersede
        the ``listing``/``normalValues`` frames, which stay for one-shot callers (a report, the tests).
    agSummary : `str` or callable, optional
        The run's auto-guiding block as HTML (`pfs.qa.dashboard.agRun.AgReport.summary`), or what
        returns it: a live serve passes its AG cache's, read each build. Empty omits the block.
    """
    offsets = _calibrationOffsets(tables)  # shared by measure (science) and calibrationDrift (calibs)
    images = measure(tables, offsets=offsets)
    # The run report lists every taken visit, no-QA frames (darks, biases, ...) included.
    visits = visitTable(images, infoFromTables(tables) if info is None else info, includeNoQa=True)
    # The daily-calibration drift over the whole run, the last night with a calibration marked; both
    # groupings, toggled.
    drift = calibrationDrift(tables, visits, offsets=offsets)
    latest = max((night for night in drift["night"].dropna().unique() if night), default="")
    # A live serve passes readers so the set section is read fresh each build, not frozen at serve start.
    if readListing is not None:
        listing = _runListing(readListing, info)
    if listing is not None and readNormalValues is not None:
        normalValues = readNormalValues()
    page = renderPage(
        images,
        visits,
        title=title,
        source=source,
        now=now,
        calibDriftSm=calibDriftSection(drift, activeNight=latest, by="spectrograph", highlight="latest"),
        calibDriftArm=calibDriftSection(drift, activeNight=latest, by="arm", highlight="latest"),
        calibSet=_calibSetSection(listing, tables, normalValues),
        agSummary=agSummary() if callable(agSummary) else agSummary,
    )
    return Report(images, visits, page)


def _calibSetSection(
    listing: pd.DataFrame | None, tables: Mapping[str, pd.DataFrame], normalValues: pd.DataFrame | None
) -> str:
    """Return the calibration-set go/no-go section, or ``""`` when there is no opdb listing or on failure.

    Imported lazily (it pulls in ``pfs.drp.qa.comparison``); a failure is logged and the section omitted,
    so it never blanks the page.
    """
    if listing is None:
        return ""
    try:
        from pfs.qa.dashboard.calibSet import calibSetEvents, renderCalibSet

        return renderCalibSet(calibSetEvents(listing, tables, normalValues))
    except Exception:
        _log.exception("Building the calibration-set section failed; omitting it")
        return ""


def _runListing(
    readListing: Callable[[Any, Any], pd.DataFrame], info: pd.DataFrame | None
) -> pd.DataFrame | None:
    """Return ``readListing(start, end)`` over the span of ``info``'s visit times, or None when there are none."""
    if info is None or "time" not in info.columns or not len(info):
        return None
    times = pd.to_datetime(info["time"]).dropna()
    return None if times.empty else readListing(times.min(), times.max())


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
    residual: Callable[[int, str], bytes | None] | None = None,
    night: Callable[[str], str] | None = None,
    extraction: Callable[[int, str], bytes | None] | None = None,
    extractionPdf: Callable[[int, str], bytes | None] | None = None,
) -> list[tuple[str, type]]:
    """Return the Tornado routes serving a `ReportCache`: its pages, and `status` at ``/status``.

    The pages read no database: each answers from what the cache last made. The residual route is the
    exception — it renders a detector's plot from the Butler on the request — so it is answered off the
    event loop, on a small thread pool, and the page already shown is never held up by it.

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
    night : callable, optional
        ``night(date)`` returns a past night's page HTML (``date`` an HST evening date, ``YYYY-MM-DD``);
        when given, it is served at ``/night/<date>`` (what the night page's dropdown and prev/next link to).
    residual : callable, optional
        ``residual(n, camera)`` returns a detector's residual plot as PNG bytes, or None when ``camera``
        is not one of the visit's detectors; when given, it is served at
        ``/visit/<n>/residual/<camera>.png`` (what the visit page's ``<img>`` tags fetch).
    extraction : callable, optional
        ``extraction(n, camera)`` returns a detector's extraction-QA summary as PNG bytes, or None; served
        at ``/visit/<n>/extraction/<camera>.png``.
    extractionPdf : callable, optional
        ``extractionPdf(n, camera)`` returns a detector's extraction-QA detail PDF as bytes, or None;
        served at ``/visit/<n>/extraction/<camera>.pdf`` (the "detail (PDF)" link beside each plot).
    """
    from concurrent.futures import ThreadPoolExecutor

    from tornado.ioloop import IOLoop
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
    if night is not None:

        class NightHandler(RequestHandler):
            def get(self, date: str) -> None:
                self.set_header("Content-Type", "text/html; charset=utf-8")
                self.set_header("Cache-Control", "no-cache")
                self.write(night(date))

        handlers.append((r"/night/(\d{4}-\d{2}-\d{2})", NightHandler))

    # The Butler-backed routes render or read a detector's dataset on the request, so each answers off the
    # event loop on its own small thread pool; the pages already shown are never held up.
    def bytesRoute(pattern: str, source: Callable[[int, str], bytes | None], contentType: str, pool) -> None:
        class ImageHandler(RequestHandler):
            async def get(self, number: str, camera: str) -> None:
                data = await IOLoop.current().run_in_executor(pool, source, int(number), camera)
                if data is None:
                    self.set_status(404)
                    return
                self.set_header("Content-Type", contentType)
                self.set_header("Cache-Control", "no-cache")
                self.write(data)

        handlers.append((pattern, ImageHandler))

    # A few workers each: a residual or extraction plot reads a detector's rows from the Butler and draws
    # it (~1 s), so several load in parallel, but not so many that the host runs out of memory.
    if residual is not None:
        bytesRoute(
            r"/visit/([0-9]+)/residual/([brnm][1-4])\.png",
            residual,
            "image/png",
            ThreadPoolExecutor(max_workers=3, thread_name_prefix="residual"),
        )
    if extraction is not None:
        bytesRoute(
            r"/visit/([0-9]+)/extraction/([brnm][1-4])\.png",
            extraction,
            "image/png",
            ThreadPoolExecutor(max_workers=3, thread_name_prefix="extraction"),
        )
    if extractionPdf is not None:
        bytesRoute(
            r"/visit/([0-9]+)/extraction/([brnm][1-4])\.pdf",
            extractionPdf,
            "application/pdf",
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="extraction-pdf"),
        )
    return handlers
