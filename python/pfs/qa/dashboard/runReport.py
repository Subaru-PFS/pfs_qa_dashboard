"""Judge a collection's visits and write the run report: one stand-alone HTML page.

The report gives every visit of a collection one verdict, the worst among its
detectors, and shows the run at a glance: a square per visit by night, counts by
kind of frame, what was measured on the science frames, and the visits that do
not pass.

Science frames are judged here, from what ``dmLineResiduals`` measured, against
`Limits`:

- the offset from the calibration in pixels: the traces' in x
  (``medianDeltaX`` of ``Trace``) and the sky lines' in y (``medianDeltaY`` of
  the species furthest off);
- the sky-line residual after the visit's detectorMap adjustment
  (``medianDeltaY`` of the held-back lines against the visit's own
  detectorMap), in km/s on the best-measured species of the arm
  (`BEST_SPECIES`), with the dispersion at that species' lines (`kmsPerPixel`).

A value at a limit has crossed it. Every other kind of frame keeps the verdict
its QA tasks stored: the worst of ``imageQualityQa`` and ``dmLineResiduals``,
and on all-sky frames ``dmLineResiduals`` alone.

On visits read with the m arm the calibration, made with the r arm, sits a
steady fraction of a pixel from the b arm's traces and the n arm's lines.
`modeOffsets` measures that from the collection's own quartz and Neon arcs
taken in both modes, and `measure` subtracts it before the pixel limits.

The input is what `pfs.drp.qa.qadb.reader.readQadb` returns (``iqQaMetrics``,
``dmLineResidualSummary``, ``dmLineResidualMetrics``): a live read, a
`qadbSource.readSnapshot`, or a `live.QadbMetricsStore`'s ``raw`` tables.
`buildReport` is the whole of it; `ReportCache` keeps the page of a store for
the server's index (`indexHandler`).
"""

import html
import json
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pfs.qa.dashboard.live import HST

__all__ = [
    "ALTERNATIVE_KMS",
    "BEST_SPECIES",
    "DEFAULT_KMS",
    "Limits",
    "Report",
    "ReportCache",
    "buildReport",
    "grade",
    "imageTable",
    "indexHandler",
    "infoFromTables",
    "judge",
    "kmsPerPixel",
    "measure",
    "modeOffsets",
    "readVisitInfo",
    "renderPage",
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

#: WARN and FAIL limits, in km/s, on each arm's sky-line residual.
DEFAULT_KMS = {"b": (1.5, 2.5), "r": (0.5, 1.0), "n": (0.5, 1.0), "m": (0.5, 1.0)}

#: Sets of km/s limits the report tabulates beside its own, loosest first: b, then r, n and m.
ALTERNATIVE_KMS = (((2.0, 3.0), (1.0, 2.0)), ((1.5, 2.5), (0.5, 1.0)), ((1.0, 2.0), (0.25, 0.5)))

#: The report's kind of frame for each ``obsType`` the QA tasks name; any other is unclassified.
KINDS = {"science": "science", "allsky": "all-sky", "arc": "arc", "trace": "quartz", "twilight": "twilight"}

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

#: The SpS exposure types the QA tasks judge: a visit of one of these should have a result.
JUDGED_EXP_TYPES = ("object", "arc", "flat")

#: The kind of frame an IIC sequence type takes, for a visit with no QA result to say it; any other is unclassified.
SEQUENCE_KINDS = {
    "scienceObject": "science",
    "scienceObject_windowed": "science",
    "scienceArc": "arc",
    "scienceTrace": "quartz",
}

#: Fewest visits in each red-arm mode for `modeOffsets` to give a detector a constant.
MIN_MODE_VISITS = 3

_IMAGE = ["visit", "arm", "spectrograph"]
_KEY = ["visit", "camera"]
_DATA = Path(__file__).parent / "data"
_INFO = ["visit", "time", "exptime", "sequenceType", "expType", "cameras", "iis", "sequenceEnd"]
_TABLES = ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics")
_COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five"}

# Every visit that exposed the spectrograph in a range: one row each (a visit is in one sequence at most).
_VISIT_INFO_SQL = """
SELECT v.pfs_visit_id AS visit, v.issued_at AS time, sv.exp_type,
    (SELECT max(e.exptime) FROM sps_exposure e WHERE e.pfs_visit_id = v.pfs_visit_id) AS exptime,
    (SELECT string_agg(c.arm || c.sps_module_id::text, ' ' ORDER BY c.arm, c.sps_module_id)
        FROM sps_exposure e JOIN sps_camera c ON c.sps_camera_id = e.sps_camera_id
        WHERE e.pfs_visit_id = v.pfs_visit_id) AS cameras,
    s.sequence_type, position('iis' IN lower(s.cmd_str)) > 0 AS iis, st.status_flag, st.cmd_output
FROM pfs_visit v
JOIN sps_visit sv ON sv.pfs_visit_id = v.pfs_visit_id
LEFT JOIN visit_set vs ON vs.pfs_visit_id = v.pfs_visit_id
LEFT JOIN iic_sequence s ON s.iic_sequence_id = vs.iic_sequence_id
LEFT JOIN iic_sequence_status st ON st.iic_sequence_id = s.iic_sequence_id
WHERE v.pfs_visit_id BETWEEN :first AND :last
"""


def _pair(values: Iterable[float]) -> tuple[float, float]:
    warn, fail = (float(value) for value in values)
    return warn, fail


@dataclass(frozen=True)
class Limits:
    """The limits science frames are judged with, each WARN then FAIL.

    Parameters
    ----------
    offsetX, offsetY : `tuple` [`float`, `float`]
        Pixels, on the size of the trace offset (x) and the sky-line offset (y)
        from the calibration.
    kms : mapping [`str`, `tuple` [`float`, `float`]]
        km/s by arm, on the size of the sky-line residual after the visit's
        adjustment.
    """

    offsetX: tuple[float, float] = (0.5, 1.0)
    offsetY: tuple[float, float] = (0.5, 1.0)
    kms: Mapping[str, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_KMS))

    def __post_init__(self) -> None:
        object.__setattr__(self, "offsetX", _pair(self.offsetX))
        object.__setattr__(self, "offsetY", _pair(self.offsetY))
        object.__setattr__(self, "kms", {arm: _pair(self.kms[arm]) for arm in ARMS})

    def withKms(self, b: Iterable[float], red: Iterable[float]) -> "Limits":
        """Return these limits with other km/s limits: ``b`` on the b arm, ``red`` on r, n and m."""
        return replace(self, kms={"b": b, "r": red, "n": red, "m": red})

    @property
    def sameRed(self) -> bool:
        """Whether the r, n and m arms share their km/s limits."""
        return len({self.kms[arm] for arm in "rnm"}) == 1


def worst(statuses: Iterable[Any]) -> str:
    """Return the worst of some verdicts; UNKNOWN when none is a verdict."""
    known = [status for status in statuses if isinstance(status, str) and status in STATUS_RANK]
    return max(known, key=STATUS_RANK.get) if known else "UNKNOWN"


def grade(value: pd.Series, warn: Any, fail: Any) -> np.ndarray:
    """Return PASS, WARN or FAIL for the size of each value, and an empty string where there is none.

    A value at a limit has crossed it.
    """
    size = value.abs()
    verdict = np.where(size >= fail, "FAIL", np.where(size >= warn, "WARN", "PASS"))
    return np.where(value.isna(), "", verdict)


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
    """Return one row per detector image with what the QA tasks stored of it.

    Parameters
    ----------
    tables : mapping
        From `pfs.drp.qa.qadb.reader.readQadb`.

    Returns
    -------
    `pandas.DataFrame`
        ``visit``, ``arm``, ``spectrograph``, ``camera``; ``obsType`` and the
        report's ``kind`` of frame; ``illumination`` (``full`` where not
        stored); ``lamp`` on arcs and quartz; each task's verdict and reason
        (``iqStatus``, ``iqReason``, ``dmStatus``, ``dmReason``) and the worse
        of the two (``storedStatus``, ``storedReason``); ``cameraSet``; and
        ``withM``, whether the visit was read with the m arm.
    """
    iq = _keyed(tables["iqQaMetrics"], ["obsType", "illumination", "seqName", "qaStatus", "qaReason"])
    iq = iq.rename(columns={"qaStatus": "iqStatus", "qaReason": "iqReason"}).drop_duplicates(
        _KEY, keep="last"
    )
    dm = _keyed(tables["dmLineResidualSummary"], ["obsType", "cameraSet", "qaStatus", "qaReason"])
    dm = dm.rename(columns={"obsType": "dmObsType", "qaStatus": "dmStatus", "qaReason": "dmReason"})
    dm = dm.drop_duplicates(_KEY, keep="last")
    images = iq.merge(dm, on=[*_IMAGE, "camera"], how="outer")
    images["obsType"] = images["obsType"].where(images["obsType"].notna(), images["dmObsType"])
    images["illumination"] = images["illumination"].where(images["illumination"].notna(), "full")
    images["kind"] = images["obsType"].map(KINDS).fillna("other")
    images["lamp"] = images["seqName"].where(images["obsType"].isin(["arc", "trace"]))
    images["storedStatus"] = [
        worst(pair) for pair in zip(images["iqStatus"], images["dmStatus"], strict=True)
    ]
    images["storedReason"] = [
        "; ".join(f"{task}: {reason}" for task, reason in tasks if isinstance(reason, str) and reason)
        for tasks in zip(
            (("imageQualityQa", reason) for reason in images["iqReason"]),
            (("dmLineResiduals", reason) for reason in images["dmReason"]),
            strict=True,
        )
    ]
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


def modeOffsets(tables: Mapping[str, pd.DataFrame], minVisits: float = MIN_MODE_VISITS) -> pd.DataFrame:
    """Return how far each b and n detector sits from the calibration when the red arm is m instead of r.

    The b arm in x, from the collection's quartz lit through the full slit
    (the traces' offset), and the n arm in y, from its Neon arcs (the Ne I
    lines' offset): each the median over the visits read with m minus the
    median over the visits read with r.

    Parameters
    ----------
    tables : mapping
        From `pfs.drp.qa.qadb.reader.readQadb`.
    minVisits : `float`
        A detector with fewer visits than this in either mode gets no constant
        (infinity: no detector gets one).

    Returns
    -------
    `pandas.DataFrame`
        ``camera``, ``axis`` (``x`` or ``y``), ``constant`` (pixels), ``source``
        (the frames measured), ``withM`` and ``withR`` (visits in each mode) and
        ``first``, ``last`` (their visit range); empty when the collection has
        no such frames in both modes.
    """
    images = imageTable(tables)
    full = images[images["illumination"] == "full"]
    offsets = _calibrationOffsets(tables)
    offsets = offsets[(offsets["reference"] == "calib") & (offsets["statusType"] == "BOTH")]
    offsets = offsets.merge(full[[*_KEY, "obsType", "withM"]], on=_KEY)
    quartz = offsets[
        (offsets["arm"] == "b")
        & (offsets["obsType"] == "trace")
        & (offsets["description"] == "Trace")
        & (offsets["metric"] == "medianDeltaX")
    ]
    neon = offsets[
        (offsets["arm"] == "n")
        & (offsets["obsType"] == "arc")
        & (offsets["description"] == "NeI")
        & (offsets["metric"] == "medianDeltaY")
    ]
    rows = []
    for frame, axis, source in ((quartz, "x", "quartz"), (neon, "y", "Neon arcs")):
        for camera, group in frame.groupby("camera"):
            withM, withR = group[group["withM"]], group[~group["withM"]]
            if min(withM["visit"].nunique(), withR["visit"].nunique()) < minVisits:
                continue
            rows.append(
                {
                    "camera": camera,
                    "axis": axis,
                    "constant": withM["value"].median() - withR["value"].median(),
                    "source": source,
                    "withM": withM["visit"].nunique(),
                    "withR": withR["visit"].nunique(),
                    "first": int(group["visit"].min()),
                    "last": int(group["visit"].max()),
                }
            )
    columns = ["camera", "axis", "constant", "source", "withM", "withR", "first", "last"]
    return pd.DataFrame(rows, columns=columns)


def measure(
    tables: Mapping[str, pd.DataFrame],
    constants: pd.DataFrame | None = None,
    scale: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return `imageTable` with what the science frames are judged on.

    Parameters
    ----------
    tables : mapping
        From `pfs.drp.qa.qadb.reader.readQadb`.
    constants : `pandas.DataFrame`, optional
        `modeOffsets`: subtracted from the offsets of the visits read with the
        m arm. None, or empty, subtracts nothing.
    scale : `pandas.DataFrame`, optional
        `kmsPerPixel`, by default.

    Returns
    -------
    `pandas.DataFrame`
        `imageTable` and, on science frames: ``xOffset`` (the traces' median
        offset from the calibration, pixels), ``yOffset`` (that of the sky
        species furthest off, pixels, signed), ``yOffsetKms`` (the largest
        species' offset in km/s, each with the dispersion at its own lines),
        ``kms`` (the residual of the arm's `BEST_SPECIES` after the visit's
        adjustment, km/s) and ``corrected`` (whether a constant was
        subtracted).
    """
    scale = kmsPerPixel() if scale is None else scale
    scale = scale.rename(columns={"species": "description"})[["camera", "description", "kmsPerPixel"]]
    images = imageTable(tables)
    science = images.loc[images["kind"] == "science", [*_KEY, "withM"]]
    offsets = _calibrationOffsets(tables).merge(science, on=_KEY)
    calib = offsets[(offsets["reference"] == "calib") & offsets["statusType"].isin(["BOTH", "UNFITTED"])]
    calib = calib.copy()
    images["corrected"] = False
    if constants is not None and len(constants):
        for axis, metric in (("x", "medianDeltaX"), ("y", "medianDeltaY")):
            ofAxis = constants[constants["axis"] == axis]
            shift = calib["camera"].map(dict(zip(ofAxis["camera"], ofAxis["constant"], strict=True)))
            shift = pd.to_numeric(shift, errors="coerce").astype("float64")
            use = calib["withM"] & (calib["metric"] == metric) & shift.notna()
            calib.loc[use, "value"] = calib.loc[use, "value"] - shift[use]
        corrected = images["withM"] & images["camera"].isin(constants["camera"])
        images["corrected"] = corrected & (images["kind"] == "science")
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


def judge(images: pd.DataFrame, limits: Limits | None = None) -> pd.DataFrame:
    """Give each image its verdict and the reason for a WARN or FAIL.

    Parameters
    ----------
    images : `pandas.DataFrame`
        From `measure`.
    limits : `Limits`, optional
        The science frames' limits; the defaults when None.

    Returns
    -------
    `pandas.DataFrame`
        ``images`` with ``xStatus``, ``yStatus`` and ``kmsStatus`` (empty off
        science frames and where nothing was measured), ``status`` and
        ``reason``. A science frame takes the worst of the three; one with no
        offset measured takes the stored ``dmLineResiduals`` verdict with them.
        An all-sky frame takes the stored ``dmLineResiduals`` verdict, any
        other frame the worse of the two tasks'.
    """
    limits = Limits() if limits is None else limits
    judged = images.copy()
    science = (judged["kind"] == "science").to_numpy()
    arm = judged["arm"]
    kmsWarn = arm.map({a: limits.kms[a][0] for a in ARMS})
    kmsFail = arm.map({a: limits.kms[a][1] for a in ARMS})
    judged["xStatus"] = np.where(science, grade(judged["xOffset"], *limits.offsetX), "")
    judged["yStatus"] = np.where(science, grade(judged["yOffset"], *limits.offsetY), "")
    judged["kmsStatus"] = np.where(science, grade(judged["kms"], kmsWarn, kmsFail), "")

    def rank(column: str) -> pd.Series:
        return judged[column].map(STATUS_RANK).fillna(-1)

    measured = pd.concat([rank("xStatus"), rank("yStatus"), rank("kmsStatus")], axis=1).max(axis=1)
    noOffsets = (judged["xStatus"] == "") & (judged["yStatus"] == "")
    scienceRank = measured.where(~noOffsets, np.maximum(measured, rank("dmStatus")))
    names = {value: name for name, value in STATUS_RANK.items()} | {-1: "UNKNOWN"}
    dmStatus = judged["dmStatus"].where(judged["dmStatus"].isin(list(STATUS_RANK)), "UNKNOWN")
    stored = judged["storedStatus"].where(judged["kind"] != "all-sky", dmStatus)
    judged["status"] = np.where(science, scienceRank.map(names), stored)

    flagged = judged["status"].isin(["WARN", "FAIL"])
    storedReason = judged["storedReason"].where(judged["kind"] != "all-sky", judged["dmReason"])
    reason = storedReason.where(flagged & storedReason.notna(), "").astype(str).to_numpy(dtype=object)
    # A science frame that passes has no reason to give.
    for index in np.flatnonzero(science & (judged["status"] != "PASS").to_numpy()):
        row = judged.iloc[index]
        if row["status"] == "UNKNOWN":
            reason[index] = "nothing measured"
            continue
        bits = []
        if row["kmsStatus"] in ("WARN", "FAIL"):
            species = SPECIES_LABEL.get(BEST_SPECIES.get(row["arm"]), "sky-line")
            limit = limits.kms[row["arm"]][row["kmsStatus"] == "FAIL"]
            bits.append(f"{species} residual {row['kms']:+.2f} km/s, limit {limit:g}")
        for axis, pair in (("x", limits.offsetX), ("y", limits.offsetY)):
            status = row[f"{axis}Status"]
            if status in ("WARN", "FAIL"):
                bits.append(f"{axis} offset {row[f'{axis}Offset']:+.2f} px, limit {pair[status == 'FAIL']:g}")
        if not bits and flagged.iloc[index] and isinstance(row["dmReason"], str) and row["dmReason"]:
            bits.append(f"dmLineResiduals: {row['dmReason']}")
        reason[index] = "; ".join(bits)
    judged["reason"] = reason
    return judged


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
        ``sequenceType`` (missing outside any IIC sequence); ``iis`` (whether
        the sequence's command lit the engineering fibers); and
        ``sequenceEnd``, how the sequence ended (``completed``, or what it
        reported when it did not).
    """
    read = opdb.query_dataframe(_VISIT_INFO_SQL, params={"first": int(first), "last": int(last)})
    columns = [
        "visit",
        "time",
        "exp_type",
        "exptime",
        "cameras",
        "sequence_type",
        "iis",
        "status_flag",
        "cmd_output",
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
    info = read.rename(columns={"sequence_type": "sequenceType", "exp_type": "expType"})
    info["sequenceEnd"] = pd.Series(ended, index=info.index, dtype=object)
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


def _noResultRows(info: pd.DataFrame, judgedVisits: set[int]) -> list[dict]:
    """Return a row for each visit the opdb says was taken, and should have been judged, that was not.

    Live results arrive in visit order, so a visit older than the latest one
    judged was passed over; a newer one may still be on its way and is left out.
    """
    if not judgedVisits or "expType" not in info.columns:
        return []
    missing = info[
        info["expType"].isin(JUDGED_EXP_TYPES)
        & ~info["visit"].isin(judgedVisits)
        & (info["visit"] < max(judgedVisits))
    ]
    rows = []
    for row in missing.itertuples():
        cameras = row.cameras.split() if isinstance(row.cameras, str) else []
        arms = "".join(
            sorted({camera[0] for camera in cameras}, key=lambda arm: ARMS.find(arm) % (len(ARMS) + 1))
        )
        ended = row.sequenceEnd if isinstance(row.sequenceEnd, str) and row.sequenceEnd else None
        rows.append(
            {
                "visit": int(row.visit),
                "kind": SEQUENCE_KINDS.get(row.sequenceType, "other"),
                "lamp": "",
                "illumination": {True: "iis", False: "full"}.get(row.iis, ""),
                "arms": arms,
                "detectors": len(cameras),
                "status": NO_RESULT,
                "flagged": "",
                "detectorsStatus": " ".join(f"{camera}N" for camera in cameras),
                "why": {None: "not in a sequence", "completed": "sequence completed"}.get(
                    ended, f"sequence ended: {ended}"
                ),
            }
        )
    return rows


def visitTable(judged: pd.DataFrame, info: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return one row per visit: its worst verdict, the detectors behind it, and when it was taken.

    With the opdb's listing of the visits taken (``info`` from
    `readVisitInfo`), a visit that should have been judged and has no result
    gets a row too, with the status `NO_RESULT`.

    Parameters
    ----------
    judged : `pandas.DataFrame`
        From `judge`.
    info : `pandas.DataFrame`, optional
        `readVisitInfo` or `infoFromTables`: ``visit`` and any of ``time``
        (HST), ``exptime`` and ``sequenceType``.

    Returns
    -------
    `pandas.DataFrame`
        Sorted by ``visit``: ``kind``, ``label`` (the kind as shown), ``lamp``,
        ``illumination``, ``slit``, ``arms`` (with † where a constant was
        subtracted), ``detectors``, ``status``, ``flagged`` (``b1 W n3 F``),
        ``detectorsStatus`` (``b1P b2W ...``), ``why``, ``time``, ``hst``,
        ``night`` (the HST date of the evening), ``exptime`` and
        ``sequenceType``. A `NO_RESULT` row has the cameras that exposed for its
        detectors (``b1N b2N ...``) and how its sequence ended for ``why``.
    """
    rows = []
    for visit, group in judged.sort_values("camera").groupby("visit"):
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
                "arms": arms + (" †" if group["corrected"].any() else ""),
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
    rows += _noResultRows(info, {row["visit"] for row in rows})
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
            labels.append("arc, " + lamp.replace("Arc: ", ""))
        elif kind != "other":
            labels.append(kind)
        elif not hasSequences:
            labels.append("unclassified")
        elif isinstance(sequence, str) and sequence:
            labels.append(f"unclassified ({SEQUENCE_LABELS.get(sequence, sequence)})")
        else:
            labels.append("unclassified (no sequence)")
    visits["label"] = labels
    visits["slit"] = visits["illumination"].map({"full": "full slit", "iis": "engineering fibers"}).fillna("")
    return visits.sort_values("visit", ignore_index=True)


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


def renderPage(
    judged: pd.DataFrame,
    visits: pd.DataFrame,
    limits: Limits | None = None,
    constants: pd.DataFrame | None = None,
    title: str = "Visit verdicts",
    source: str = "",
    now: datetime | None = None,
) -> str:
    """Return the report as one HTML document, with its styles, charts and script inline.

    Parameters
    ----------
    judged : `pandas.DataFrame`
        From `judge`, with ``limits``.
    visits : `pandas.DataFrame`
        `visitTable` of ``judged``.
    limits : `Limits`, optional
        What ``judged`` was judged with; the defaults when None.
    constants : `pandas.DataFrame`, optional
        The `modeOffsets` that `measure` subtracted, for the appendix; none by
        default.
    title : `str`
        The page's title and heading.
    source : `str`
        Where the results were read from, as HTML, for the page's last note
        (`sourceText`); ``{date}`` in it becomes the date of ``now``.
    now : `datetime.datetime`, optional
        When the page was made; the current time (HST) by default.
    """
    limits = Limits() if limits is None else limits
    kms, offsetX, offsetY = limits.kms, limits.offsetX, limits.offsetY
    scales = kmsPerPixel()
    centre = scales[scales["species"] == "centre"]
    centre = centre.groupby(centre["camera"].str[0])["kmsPerPixel"].median()

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

    science = judged[judged["kind"] == "science"]
    width, height = 260, 28
    span = 1.3 * max(offsetX[0], offsetY[0])

    def xo(value: float) -> float:
        return round((_clip(value, -span, span) + span) / (2 * span) * width, 1)

    def offsetBar(camera: str, group: pd.DataFrame) -> str:
        tip = (
            f"{camera}: x offset {_range(group['xOffset'])} px, y offset {_range(group['yOffset'])} px "
            "(5th to 95th percentile)"
        )
        refs = "".join(
            f'<line class="ref" x1="{xo(v)}" x2="{xo(v)}" y1="1" y2="{height / 2 - 1}"/>'
            for v in (-offsetX[0], offsetX[0])
        )
        refs += "".join(
            f'<line class="ref" x1="{xo(v)}" x2="{xo(v)}" y1="{height / 2 + 1}" y2="{height - 1}"/>'
            for v in (-offsetY[0], offsetY[0])
        )
        return (
            f'<svg class="bar" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{tip}">'
            f'<line class="zero" x1="{xo(0)}" x2="{xo(0)}" y1="0" y2="{height}"/>'
            + refs
            + _strip(group, "xOffset", "xStatus", "px in x", xo, 4, "rng", 6)
            + _strip(group, "yOffset", "yStatus", "px in y", xo, 18, "rng2", 6)
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
            + _strip(group, "kms", "kmsStatus", "km/s", xr, 7, "rng", 8)
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
            f'<tr class="arm"><th colspan="8" scope="colgroup">{arm} arm · {species} lines · '
            f"WARN at {kms[arm][0]:g} km/s, FAIL at {kms[arm][1]:g} km/s</th></tr>"
        )
        for camera, group in ofArm.groupby("camera"):
            flags = (group["xStatus"].isin(["WARN", "FAIL"]) | group["yStatus"].isin(["WARN", "FAIL"])).sum()
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
                    f"<td class='viz'>{residualBar(camera, group, *kms[arm])}</td>"
                    f"<td>{int((group['kmsStatus'] == 'WARN').sum())}</td>"
                    f"<td>{int((group['kmsStatus'] == 'FAIL').sum())}</td></tr>"
                )
    tickAt = max(offsetX[0], offsetY[0])
    ticks = "".join(
        f'<text x="{xo(v)}" y="12" text-anchor="middle">{f"{v:+g}" if v else "0"}</text>'
        for v in (-tickAt, -tickAt / 2, 0, tickAt / 2, tickAt)
    )
    offsetAxis = f'<svg class="bar axis" viewBox="0 0 {width} 16" width="{width}" height="16" aria-hidden="true">{ticks}</svg>'

    sets = [limits.withKms(b, red) for b, red in ALTERNATIVE_KMS]
    if limits not in sets:
        sets.append(limits)
    limitRows = []
    for other in sets:
        own = other == limits
        rejudged = judged if own else judge(judged, other)
        ofScience = rejudged[rejudged["kind"] == "science"]
        perVisit = ofScience.groupby("visit")["status"].agg(worst)
        cells = "".join(
            f"<td>{int(((ofScience['kmsStatus'] == 'WARN') & (ofScience['arm'] == a)).sum())} / "
            f"{int(((ofScience['kmsStatus'] == 'FAIL') & (ofScience['arm'] == a)).sum())}</td>"
            for a in ARMS
        )
        red = (
            f"{other.kms['r'][0]:g} and {other.kms['r'][1]:g}"
            if other.sameRed
            else "; ".join(f"{a} {other.kms[a][0]:g} and {other.kms[a][1]:g}" for a in "rnm")
        )
        limitRows.append(
            f"<tr{' class="own"' if own else ''}><th scope='row'>{'This report' if own else ''}</th><td>{red}</td>"
            f"<td>{other.kms['b'][0]:g} and {other.kms['b'][1]:g}</td>{cells}"
            + "".join(f"<td>{int((perVisit == k).sum())}</td>" for k in ("PASS", "WARN", "FAIL"))
            + "</tr>"
        )

    nights = [
        f"<tr><th scope='row'>{night}</th><td>{group['visit'].min()} to {group['visit'].max()}</td>"
        f"<td>{_nightCell(group[group['kind'] == 'science'])}</td>"
        f"<td>{_nightCell(group[group['kind'].isin(['twilight', 'all-sky'])])}</td>"
        f"<td>{_nightCell(group[group['kind'].isin(['arc', 'quartz']) & (group['illumination'] == 'full')])}</td>"
        f"<td>{_nightCell(group[group['illumination'] == 'iis'])}</td>"
        f"<td>{int((noResult['night'] == night).sum()) or '–'}</td></tr>"
        for night, group in visits[visits["night"] != ""].groupby("night")
    ]

    modeNote = modePointer = ""
    nCorrected = int(visits.loc[visits["kind"] == "science", "arms"].str.contains("†").sum())
    if constants is not None and len(constants) and nCorrected:
        modeRows = "\n".join(
            f"<tr><th scope='row'>{r.camera}</th><td>{r.axis}</td><td class='key'>{r.constant:+.2f}</td>"
            f"<td class='l'>{r.source}, {r.withM} visits with m and {r.withR} with r</td>"
            f"<td>{r.first} to {r.last}</td></tr>"
            for r in constants.itertuples()
        )
        modePointer = (
            " On visits read with the m arm, a standing offset has been subtracted from the b arm in x and "
            "the n arm in y; see the <a href='#appendix-m-arm'>appendix</a>."
        )
        modeNote = (
            "<section class='block' id='appendix-m-arm'><h2>Appendix: visits read with the m arm</h2>"
            f"<p>On the {nCorrected} science visits read with the m arm, the b arm's x offset and the n arm's y "
            "offset are measured from where those detectors sit in that mode. The calibration was made with the "
            "r arm, and with m the b-arm traces and the n-arm lines land a steady fraction of a pixel away. The "
            "amount subtracted on each detector is the difference between the run's own calibration frames taken "
            "in the two modes. Those visits are marked † in the list of every visit.</p>"
            "<div class='tablewrap'><table><thead><tr><th scope='col' class='l'>Detector</th>"
            "<th scope='col'>Direction</th><th scope='col'>Subtracted on visits read with m (px)</th>"
            "<th scope='col' class='l'>Measured from</th><th scope='col'>Visits</th></tr></thead>"
            f"<tbody>\n{modeRows}\n</tbody></table></div></section>"
        )

    def flaggedDetectors(flagged: str) -> str:
        names = {"W": "WARN", "F": "FAIL"}
        pairs = flagged.split()
        return ", ".join(
            f"{camera} {names[letter]}" for camera, letter in zip(pairs[::2], pairs[1::2], strict=True)
        )

    notPassing = visits[(visits["status"] != "PASS") & (visits["illumination"] != "iis")]
    flagged = "\n".join(
        f"<tr><th scope='row'>{r.visit}</th><td class='l'>{r.hst}</td><td class='l'>{html.escape(r.label)}</td>"
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
            f"<tr><th scope='row'>{r.visit}</th><td class='l'>{r.hst}</td><td class='l'>{html.escape(r.label)}</td>"
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
    if limits.sameRed:
        kmsRule = (
            f"WARN at {kms['r'][0]:g} km/s and FAIL at {kms['r'][1]:g} km/s on the r, n and m arms (OH lines)"
        )
    else:
        kmsRule = "; ".join(
            f"WARN at {kms[a][0]:g} and FAIL at {kms[a][1]:g} km/s on the {a} arm" for a in "rnm"
        )
        kmsRule += " (OH lines)"
    kmsRule += f", WARN at {kms['b'][0]:g} km/s and FAIL at {kms['b'][1]:g} km/s on the b arm ([O I])"
    if offsetX == offsetY:
        offsetRule = f"WARN at {offsetX[0]:g} px and FAIL at {offsetX[1]:g} px"
    else:
        offsetRule = (
            f"WARN at {offsetX[0]:g} px and FAIL at {offsetX[1]:g} px in x, "
            f"WARN at {offsetY[0]:g} px and FAIL at {offsetY[1]:g} px in y"
        )
    when = everyVisit["time"].dropna()
    dates = ""
    if len(when):
        first, last = when.min(), when.max()
        dates = f", {first.day} {first:%B} to {last.day} {last:%B %Y} (HST)"
    now = datetime.now(HST) if now is None else now
    source = source.replace("{date}", f"{now:%Y-%m-%d}")
    fields = {
        "{{TITLE}}": html.escape(title),
        "{{SUMMARY}}": "\n".join(summary),
        "{{OFFSETS}}": "\n".join(offsetRows),
        "{{RESIDUALS}}": "\n".join(residualRows),
        "{{OAXIS}}": offsetAxis,
        "{{LIMITS}}": "\n".join(limitRows),
        "{{NSETS}}": _COUNT_WORDS.get(len(sets), str(len(sets))),
        "{{NIGHTS}}": "\n".join(nights),
        "{{FLAGGED}}": flagged,
        "{{NVISITS}}": f"{len(visits):,}",
        "{{NO_RESULT_META}}": f", {len(noResult):,} more with no QA result" if len(noResult) else "",
        "{{NO_RESULT_SECTION}}": missingSection,
        "{{NO_RESULT_NOTE}}": noResultNote,
        "{{NIMAGES}}": f"{len(judged):,}",
        "{{FIRST}}": str(everyVisit["visit"].min()) if len(everyVisit) else "–",
        "{{LAST}}": str(everyVisit["visit"].max()) if len(everyVisit) else "–",
        "{{NFLAGGED}}": str(len(notPassing)),
        "{{LEAD}}": lead,
        "{{DATES}}": dates,
        "{{TODAY}}": f"{now:%Y-%m-%d}",
        "{{SOURCE}}": source,
        "{{MODE_NOTE}}": modeNote,
        "{{MODE_POINTER}}": modePointer,
        "{{KMS_RULE}}": kmsRule,
        "{{OFFSET_RULE}}": offsetRule,
        "{{NSCIENCE}}": str(int((kind == "science").sum())),
        # Last, and with "<" escaped: the visits' reasons are qadb's text, inside a script element.
        "{{DATA}}": json.dumps(data, separators=(",", ":")).replace("<", "\\u003c"),
    }
    page = (_DATA / "runReport.html").read_text()
    for key, value in fields.items():
        page = page.replace(key, value)
    return page


@dataclass(frozen=True)
class Report:
    """A collection's report: the judged images and visits, what was subtracted, and the page."""

    images: pd.DataFrame
    visits: pd.DataFrame
    constants: pd.DataFrame
    html: str


def buildReport(
    tables: Mapping[str, pd.DataFrame],
    limits: Limits | None = None,
    info: pd.DataFrame | None = None,
    correctModes: bool = True,
    title: str = "Visit verdicts",
    source: str = "",
    now: datetime | None = None,
    constants: pd.DataFrame | None = None,
) -> Report:
    """Judge what a qadb read holds and return its report.

    Parameters
    ----------
    tables : mapping
        From `pfs.drp.qa.qadb.reader.readQadb`.
    limits : `Limits`, optional
        The science frames' limits; the defaults when None.
    info : `pandas.DataFrame`, optional
        The visits' times, exposure times and sequence types (`readVisitInfo`);
        `infoFromTables` when None.
    correctModes : `bool`
        Whether to subtract the `modeOffsets` on visits read with the m arm.
    title, source, now
        As `renderPage` takes them.
    constants : `pandas.DataFrame`, optional
        The `modeOffsets` to subtract, when they were measured elsewhere than
        on ``tables``; measured on ``tables`` when None.
    """
    limits = Limits() if limits is None else limits
    if constants is None or not correctModes:
        constants = modeOffsets(tables, minVisits=MIN_MODE_VISITS if correctModes else np.inf)
    judged = judge(measure(tables, constants), limits)
    visits = visitTable(judged, infoFromTables(tables) if info is None else info)
    page = renderPage(judged, visits, limits, constants, title=title, source=source, now=now)
    return Report(judged, visits, constants, page)


class ReportCache:
    """The report of a store's collection, made again when the store's tables change.

    Parameters
    ----------
    store : `pfs.qa.dashboard.store.MetricsStore`
        The store: its ``raw`` tables are judged, and its ``refresh`` is called
        before each page (a live store limits how often that reads qadb).
    readInfo : callable, optional
        ``readInfo(first, last)`` returns every visit taken in the range
        (`readVisitInfo` over the opdb), read from ``firstVisit`` to the latest
        visit judged. Without it, the store's own visit times are used, and
        no visit can be told to lack a result.
    info : `pandas.DataFrame`, optional
        The visits' information, fixed, in place of ``readInfo`` (a snapshot's).
    firstVisit : `int`, optional
        The first visit of the run; the earliest visit judged when None.
    **options
        ``limits``, ``correctModes``, ``title`` and ``source`` for `buildReport`.
    """

    def __init__(
        self,
        store: Any,
        readInfo: Callable[[int, int], pd.DataFrame] | None = None,
        info: pd.DataFrame | None = None,
        firstVisit: int | None = None,
        **options: Any,
    ):
        self.store = store
        self._readInfo = readInfo
        self._info = info
        self._firstVisit = firstVisit
        self._options = options
        self._lock = threading.Lock()
        self._version: int | None = None
        self.report: Report | None = None

    def take(self) -> tuple[int, dict[str, pd.DataFrame], pd.DataFrame]:
        """Read the store's source and return what a page is made from: its version, tables and visit times.

        Call it on the thread the store is used from (the server's loop, as the metric views do): a
        refresh rebuilds the store's tables, which its queries read.
        """
        self.store.refresh()
        # A store that has read no image yet holds no table: the page then says so, with no visit.
        tables = {name: self.store.raw.get(name, pd.DataFrame(columns=_IMAGE)) for name in _TABLES}
        times = self.store.table[["visit", "timeExpStart"]].drop_duplicates("visit")
        return self.store.version, tables, times.rename(columns={"timeExpStart": "time"})

    def build(self, taken: tuple[int, dict[str, pd.DataFrame], pd.DataFrame]) -> str:
        """Return the page for what `take` returned, made only if the store has changed since the last one."""
        version, tables, times = taken
        with self._lock:
            if self.report is None or self._version != version:
                info = self._info
                visits = sorted({int(visit) for name in tables for visit in tables[name]["visit"]})
                if info is None and self._readInfo is not None and visits:
                    info = self._readInfo(self._firstVisit or visits[0], visits[-1])
                elif info is None:
                    info = times
                self.report = buildReport(tables, info=info, **self._options)
                self._version = version
            return self.report.html

    def html(self) -> str:
        """Return the page for the store as it is now, reading the store's source first."""
        return self.build(self.take())


def indexHandler(cache: ReportCache) -> type:
    """Return the Tornado handler serving a `ReportCache`'s page, for the server's index."""
    from tornado.ioloop import IOLoop
    from tornado.web import RequestHandler

    class RunReportHandler(RequestHandler):
        async def get(self) -> None:
            # The store is read here, on the server's loop, as the metric views read it. Judging every
            # image and reading the opdb are off it, so the views stay live meanwhile.
            taken = cache.take()
            page = await IOLoop.current().run_in_executor(None, cache.build, taken)
            self.set_header("Content-Type", "text/html; charset=utf-8")
            self.set_header("Cache-Control", "no-cache")
            self.write(page)

    return RunReportHandler
