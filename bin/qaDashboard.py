#!/usr/bin/env python3
r"""The QA dashboard: serve a run's report live from qadb or from a snapshot, write it as a file, or save a snapshot.

Stack-free; needs tornado, and psycopg for qadb.

Examples::

    # A run as qaActor loads it, with the opdb's visits: both only read (credentials in ~/.pgpass)
    python bin/qaDashboard.py serve --collection qaActor/reductions --first-visit 151000 \
        --qadb-host 127.0.0.1 --qadb-port 5436 --opdb-host 127.0.0.1 --opdb-port 5432 \
        --title "Run31 visit verdicts"
    # Read it once into a snapshot (parquet and provenance.json), then serve that offline
    python bin/qaDashboard.py snapshot --collection qaActor/reductions --opdb-host 127.0.0.1 --out run31-qadb
    python bin/qaDashboard.py serve --snapshot run31-qadb
    # Write the run report as one stand-alone page, for any web server
    python bin/qaDashboard.py report --snapshot run31-qadb --out run31-report

then open http://localhost:5006. To serve the local network, add ``--address 0.0.0.0``.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path


def makeParser() -> argparse.ArgumentParser:
    """Return the command line's parser: ``serve``, ``snapshot`` and ``report``, sharing the database options."""
    databases = argparse.ArgumentParser(add_help=False)
    qadb = databases.add_argument_group("qadb")
    qadb.add_argument("--first-visit", type=int, default=0, help="first visit read")
    qadb.add_argument("--last-visit", type=int, default=2**31 - 1, help="last visit read")
    qadb.add_argument("--qadb-host", default="127.0.0.1")
    qadb.add_argument("--qadb-port", type=int, default=5436)
    qadb.add_argument("--qadb-dbname", default="qadb")
    qadb.add_argument("--qadb-user", default="pfs", help="the qadb user (default pfs); only read with")
    opdb = databases.add_argument_group(
        "the opdb's visits: their times, exposures and IIC sequences (default: none)"
    )
    opdb.add_argument("--opdb-host")
    opdb.add_argument("--opdb-port", type=int, default=5432)
    opdb.add_argument("--opdb-dbname", default="opdb")
    opdb.add_argument("--opdb-user", default="public_user", help="a read-only user (default public_user)")

    page = argparse.ArgumentParser(add_help=False)
    report = page.add_argument_group("the run report")
    report.add_argument(
        "--title", default="Visit verdicts", help="the report's title, e.g. 'Run31 visit verdicts'"
    )

    parser = argparse.ArgumentParser(description="The QA dashboard.")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser(
        "serve",
        parents=[databases, page],
        help="serve the run report, live from qadb or from a snapshot",
    )
    source = serve.add_mutually_exclusive_group(required=True)
    source.add_argument("--collection", help="the collection loaded into qadb, e.g. qaActor/reductions")
    source.add_argument(
        "--snapshot", type=Path, metavar="DIR", help="a snapshot written by the snapshot command"
    )
    serve.add_argument("--refresh", type=float, default=30.0, help="seconds between qadb reads (default 30)")
    serve.add_argument(
        "--butler",
        metavar="REPO",
        help=(
            "a Butler repository (e.g. /work/datastore) on this host, for the detector-map residual plots on"
            " the per-visit page: each is drawn on demand from the collection's dmLineResidualData. Needs the"
            " LSST stack; only with --collection (a snapshot has no per-line data). Omit for no plots."
        ),
    )
    serve.add_argument("--instrument", default="PFS", help="the Butler instrument for --butler (default PFS)")
    serve.add_argument("--port", type=int, default=5006)
    serve.add_argument(
        "--address", default="localhost", help="interface to listen on; 0.0.0.0 for the local network"
    )
    serve.add_argument("--show", action="store_true", help="open a browser")
    serve.add_argument(
        "--debug",
        action="store_true",
        help="development: restart the server when the dashboard's code or its data/ assets change",
    )

    snapshot = commands.add_parser(
        "snapshot",
        parents=[databases],
        help="read a collection from qadb (and the opdb) once into a directory",
    )
    snapshot.add_argument("--collection", required=True, help="the collection loaded into qadb")
    snapshot.add_argument("--out", type=Path, required=True, metavar="DIR", help="the snapshot's directory")

    report = commands.add_parser(
        "report",
        parents=[databases, page],
        help="write the run report as one stand-alone page (<name>.html), with its visits and images as CSV",
    )
    source = report.add_mutually_exclusive_group(required=True)
    source.add_argument("--collection", help="the collection loaded into qadb, e.g. qaActor/reductions")
    source.add_argument(
        "--snapshot", type=Path, metavar="DIR", help="a snapshot written by the snapshot command"
    )
    report.add_argument("--out", type=Path, required=True, metavar="DIR", help="the directory written to")
    report.add_argument(
        "--name",
        default="index",
        help="the page's file name, written <name>.html (default: index, e.g. run31)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = makeParser()
    args = parser.parse_args(argv)
    if args.command == "serve" and args.refresh < 1:
        parser.error("--refresh: at least 1 second")
    if args.command == "serve" and args.butler and not args.collection:
        parser.error("--butler needs --collection: a snapshot has no per-line residual data to plot")
    if args.command in ("serve", "report") and args.snapshot is not None:
        missing = [
            name
            for name in ("provenance.json", "iqQaMetrics.parquet", "qaGate.parquet")
            if not (args.snapshot / name).exists()
        ]
        if missing:
            parser.error(
                f"{args.snapshot} is not a snapshot with the gate's verdicts (no {', '.join(missing)}): write one with "
                f"`qaDashboard.py snapshot --collection <collection> --out {args.snapshot}`"
            )
    {"snapshot": snapshot, "report": report, "serve": serve}[args.command](args)


def connect(args: argparse.Namespace):
    """Return the qadb, and the opdb or None, that ``args`` name; both are only read."""
    from pfs.utils.database.qadb import QaDB

    # A database that does not answer is given up on, not waited for; the environment's own value wins.
    os.environ.setdefault("PGCONNECT_TIMEOUT", "10")
    qadb = QaDB(host=args.qadb_host, port=args.qadb_port, dbname=args.qadb_dbname, user=args.qadb_user)
    opdb = None
    if args.opdb_host:
        from pfs.utils.database.opdb import OpDB

        opdb = OpDB(host=args.opdb_host, port=args.opdb_port, dbname=args.opdb_dbname, user=args.opdb_user)
    print(
        f"Reading {args.collection} from qadb at {args.qadb_host}:{args.qadb_port}"
        + (f", with the opdb at {args.opdb_host}:{args.opdb_port}" if opdb else ", without the opdb")
        + " ...",
        flush=True,
    )
    return qadb, opdb


def judgedVisits(tables: dict) -> list[int]:
    """Return the visits a qadb read holds a result for, from either task."""
    names = ("iqQaMetrics", "dmLineResidualSummary")
    return sorted({int(visit) for name in names for visit in tables[name]["visit"]})


def reportOptions(args: argparse.Namespace, source: str) -> dict:
    """Return what `pfs.qa.dashboard.runReport.buildReport` takes from the command line."""
    return {"title": args.title, "source": source}


def runWindow(start, end):
    """Return the observing period(s) overlapping ``[start, end]``, else ``[start, end]`` padded a day.

    The calibration-set listing is read over this so `classifyVisits` sees the whole run (``daily`` needs a
    sequence to recur on several nights); a narrow visit range would otherwise misclassify the set.
    """
    import pandas as pd

    from pfs.drp.qa.comparison.runs import loadPeriods

    periods = [p for p in loadPeriods().values() if p.start <= end and p.end >= start]
    if periods:
        return min(p.start for p in periods), max(p.end for p in periods)
    return start - pd.Timedelta(days=1), end + pd.Timedelta(days=1)


def readRunListing(opdb, times):
    """Return the opdb visit listing over the run of ``times`` (HST), or None when there is no opdb or no time."""
    import pandas as pd

    if opdb is None or times is None or len(times) == 0:
        return None
    span = pd.to_datetime(pd.Series(list(times))).dropna()
    if span.empty:
        return None
    from pfs.drp.qa.comparison.queries import readVisitListing

    return readVisitListing(opdb, *runWindow(span.min(), span.max()))


def calibSetInputs(opdb, qadb, collection, times):
    """Return ``(listing, normalValues)`` for the run report's calibration-set section, or ``(None, None)``.

    The opdb visit listing covers the run (`runWindow`); the normal values come from qadb. No opdb, no
    collection or no times gives ``(None, None)``, so `buildReport` omits the section. One-shot (`report`);
    `serve` reads them per build instead, so the section stays current.
    """
    if opdb is None or not collection:
        return None, None
    listing = readRunListing(opdb, times)
    if listing is None:
        return None, None
    from pfs.drp.qa.qadb.reader import readNormalValues

    return listing, readNormalValues(qadb, collection)


def snapshotSource(tables: dict, provenance: dict) -> str:
    """Return the report's note on where a snapshot's results come from."""
    from pfs.qa.dashboard.runReport import sourceText

    collection = str(provenance.get("collection", "")).removesuffix(" (qadb snapshot)")
    readOn = str(provenance.get("builtAt") or "")[:10] or None
    return sourceText(collection, "visitInfo" in tables, readOn)


def pages(
    args: argparse.Namespace,
    source: str,
    readTransparency=None,
    readSeeing=None,
    readListing=None,
    readNormalValues=None,
) -> dict:
    """Return the pages a `ReportCache` builds: the night page (``/``) and the run report (``/run``).

    ``readTransparency(first, last)`` and ``readSeeing(first, last)`` read the night's AG transparency and
    seeing from qadb for the night page's Conditions tile; None (a snapshot, or no live qadb) omits that metric.
    ``readListing(start, end)`` and ``readNormalValues()`` feed the run report's calibration-set section, read
    each build so it stays current; None omits it.
    """
    from pfs.qa.dashboard.nightReport import buildNightReport
    from pfs.qa.dashboard.runReport import buildReport

    return {
        "night": (
            buildNightReport,
            {
                "title": args.title,
                "source": source,
                "runUrl": "/run",
                "readTransparency": readTransparency,
                "readSeeing": readSeeing,
            },
        ),
        "report": (
            buildReport,
            {
                "title": args.title,
                "source": source,
                "readListing": readListing,
                "readNormalValues": readNormalValues,
            },
        ),
    }


def butlerResiduals(args: argparse.Namespace, sliceStore):
    """Return a `ResidualImages` for the per-visit residual plots, or None without ``--butler``.

    The Butler read is the only stack-bound part: imported here, guarded by a lock in case the Butler
    is not thread-safe, and a missing dataset returns None rather than raising. The detector context
    (which cameras a visit has, and each one's frame type) comes from the cached store, so it needs no
    database.
    """
    if not args.butler:
        return None

    import threading

    from lsst.daf.butler import Butler

    from pfs.qa.dashboard.runReport import imageTable
    from pfs.qa.dashboard.visitResiduals import ResidualImages

    butler = Butler(args.butler, collections=[args.collection], writeable=False)
    lock = threading.Lock()

    def readLineData(visit: int, arm: str, spectrograph: int):
        dataId = {
            "instrument": args.instrument,
            "visit": int(visit),
            "arm": arm,
            "spectrograph": int(spectrograph),
        }
        with lock:
            try:
                return butler.get("dmLineResidualData", dataId)
            except (LookupError, FileNotFoundError):
                # The collection holds no dmLineResidualData for this detector — the task did not run or
                # wrote nothing (LookupError), or the dataset is registered but its file is gone
                # (FileNotFoundError): the plot shows a placeholder, not a 500.
                return None

    def readBBox(visit: int, arm: str, spectrograph: int):
        """Return the detector's pixel bounds ``(x0, x1, y0, y1)`` from its detectorMap, or None.

        `ResidualImages` holds the residual face maps to these bounds, and reads them once per camera, so
        a detectorMap is fetched only the first time a detector's plot is drawn.
        """
        dataId = {
            "instrument": args.instrument,
            "visit": int(visit),
            "arm": arm,
            "spectrograph": int(spectrograph),
        }
        with lock:
            try:
                detectorMap = butler.get("detectorMap", dataId)
            except (LookupError, FileNotFoundError):
                return None
        bbox = detectorMap.getBBox()
        return (bbox.getBeginX(), bbox.getEndX(), bbox.getBeginY(), bbox.getEndY())

    def context(visit: int) -> dict:
        images = imageTable(sliceStore(int(visit)))
        images = images[images["visit"] == int(visit)]
        return dict(zip(images["camera"], images["frameType"], strict=True))

    print(
        f"Serving detector-map residual plots from the Butler at {args.butler} ({args.collection})",
        flush=True,
    )
    return ResidualImages(readLineData, context, readBBox=readBBox)


def butlerExtraction(args: argparse.Namespace, sliceStore):
    """Return an `ExtractionImages` for the per-visit extraction plots, or None without ``--butler``.

    Mirrors `butlerResiduals`: the Butler reads are the only stack-bound part, guarded by a lock and
    returning None for a missing dataset. The summary PNG is drawn from ``extQaImage_pickle``; the
    "detail (PDF)" link streams the ``extQaImage`` file straight from the datastore.
    """
    if not args.butler:
        return None

    import threading

    from lsst.daf.butler import Butler

    from pfs.qa.dashboard.runReport import imageTable
    from pfs.qa.dashboard.visitExtraction import ExtractionImages

    butler = Butler(args.butler, collections=[args.collection], writeable=False)
    lock = threading.Lock()

    def dataId(visit: int, arm: str, spectrograph: int) -> dict:
        return {
            "instrument": args.instrument,
            "visit": int(visit),
            "arm": arm,
            "spectrograph": int(spectrograph),
        }

    def readStats(visit: int, arm: str, spectrograph: int):
        with lock:
            try:
                return butler.get("extQaImage_pickle", dataId(visit, arm, spectrograph))
            except (LookupError, FileNotFoundError):
                # No pickle for this detector: the task did not run or wrote nothing. Plot a placeholder.
                return None

    def readImagePdf(visit: int, arm: str, spectrograph: int):
        with lock:
            try:
                uri = butler.getURI("extQaImage", dataId(visit, arm, spectrograph))
            except (LookupError, FileNotFoundError, ValueError):
                # LookupError/FileNotFoundError: not stored; ValueError: a disassembled/multi-URI dataset.
                return None
        # The file read needs no butler, so it runs outside the lock; a missing file is a 404, not a 500.
        try:
            return uri.read()
        except (FileNotFoundError, OSError):
            return None

    def context(visit: int) -> dict:
        images = imageTable(sliceStore(int(visit)))
        images = images[images["visit"] == int(visit)]
        return dict(zip(images["camera"], images["frameType"], strict=True))

    print(
        f"Serving extraction-QA plots from the Butler at {args.butler} ({args.collection})",
        flush=True,
    )
    return ExtractionImages(readStats, readImagePdf, context)


def serve(args: argparse.Namespace) -> None:
    """Serve the night page (``/``) and the run report (``/run``) of a collection or a snapshot."""
    # One line per request, with the client's address: shows whether a browser reached the server.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("tornado.access").setLevel(logging.INFO)

    import pandas as pd
    from tornado.web import Application

    from pfs.qa.dashboard.live import QadbStore
    from pfs.qa.dashboard.nightReport import buildNightReport
    from pfs.qa.dashboard.qadbSource import readSnapshot
    from pfs.qa.dashboard.runReport import ReportCache, infoFromTables, readVisitInfo, routes, sourceText
    from pfs.qa.dashboard.visitPage import buildVisitPage

    if args.collection:
        from pfs.drp.qa.comparison.queries import readVisitListing
        from pfs.drp.qa.qadb.reader import readNormalValues as readNormalValuesDb
        from pfs.qa.dashboard.agConditions import readSeeing as readSeeingDb
        from pfs.qa.dashboard.agConditions import readTransparency as readTransparencyDb

        qadb, opdb = connect(args)
        store = QadbStore.fromQadb(qadb, args.collection, args.first_visit, args.last_visit)
        print(f"{len(store.times)} visits so far", flush=True)
        readInfo = None if opdb is None else (lambda first, last: readVisitInfo(opdb, first, last))
        readTransparency = lambda first, last: readTransparencyDb(qadb, first, last)  # noqa: E731
        readSeeing = lambda first, last: readSeeingDb(qadb, first, last)  # noqa: E731
        # Read the set's inputs each build (not frozen at serve start), so a later rebuild set or
        # newly-accepted normal values appear without a restart.
        readListing = (
            None if opdb is None else (lambda start, end: readVisitListing(opdb, *runWindow(start, end)))
        )
        # The set section needs the opdb listing, so without an opdb neither reader is used.
        readNormalValues = None if opdb is None else (lambda: readNormalValuesDb(qadb, args.collection))
        source = sourceText(args.collection, opdb is not None)
        cache = ReportCache(
            store,
            readInfo,
            firstVisit=args.first_visit or None,
            pages=pages(args, source, readTransparency, readSeeing, readListing, readNormalValues),
        )
        cache.update()
        # The databases are read on a thread of the cache's own from here on: a browser gets the last
        # page made at once, whatever the databases are doing.
        cache.watch(args.refresh)
    else:
        tables, provenance = readSnapshot(args.snapshot)
        store = QadbStore.fromTables(tables)
        print(f"{len(store.times)} visits in {args.snapshot}", flush=True)
        source = snapshotSource(tables, provenance)
        cache = ReportCache(store, info=infoFromTables(tables), pages=pages(args, source))
        cache.update()

    def sliceStore(number: int) -> dict:
        """Return the cached store's four tables, each scoped to ``number``'s rows."""
        # Fixed names via get(), not raw.items(): the refresh thread may add a table key concurrently.
        raw = cache.store.raw
        sliced = {}
        for name in ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics", "qaGate"):
            frame = raw.get(name)
            if frame is not None and "visit" in getattr(frame, "columns", []):
                sliced[name] = frame[frame["visit"] == number]
        return sliced

    residualImages = butlerResiduals(args, sliceStore)
    extractionImages = butlerExtraction(args, sliceStore)

    def visitPage(number: int) -> str:
        """Build the per-visit page from the cached store (no database read)."""
        return buildVisitPage(
            sliceStore(number),
            cache.info,
            number,
            residuals=residualImages is not None,
            extraction=extractionImages is not None,
        )

    def nightPage(date: str) -> str:
        """Build a past night's page (``/night/<date>``) from the cached store, with no new database read.

        The latest night is served pre-built at ``/``, with its AG conditions; a past night is built on the
        request from the tables already in memory, so it reads nothing (no qadb while the refresh thread may
        be reading it) and omits the conditions tile, which would need a read of its own.
        """
        raw = cache.store.raw
        # Fixed names via get(), as sliceStore does: the refresh thread may add a table key concurrently.
        tables = {
            name: raw.get(name, pd.DataFrame(columns=["visit", "arm", "spectrograph"]))
            for name in ("iqQaMetrics", "dmLineResidualSummary", "dmLineResidualMetrics", "qaGate")
        }
        return buildNightReport(
            tables, info=cache.info, title=args.title, source=source, night=date, runUrl="/run"
        ).html

    mounts = {"/": "night", "/run": "report"}
    print(
        f"Serving the night page at http://{args.address}:{args.port}/ (past nights at /night/<date>) "
        "and the run report at /run",
        flush=True,
    )
    listen(
        Application(
            routes(
                cache,
                mounts,
                visit=visitPage,
                residual=residualImages.png if residualImages is not None else None,
                night=nightPage,
                extraction=extractionImages.png if extractionImages is not None else None,
                extractionPdf=extractionImages.pdf if extractionImages is not None else None,
            )
        ),
        args.address,
        args.port,
        args.show,
        debug=args.debug,
    )


def listen(application, address: str, port: int, show: bool = False, debug: bool = False) -> None:
    """Serve a Tornado ``application`` on ``address:port`` until interrupted.

    ``show`` opens a browser on it. ``debug`` auto-reloads the server (Tornado's autoreload) when the
    dashboard's code or its ``data/`` assets change, by restarting the process; each restart re-reads the
    databases, so it pairs well with ``--snapshot``.
    """

    async def run() -> None:
        application.listen(port, address=address)
        if debug:
            # Started here, inside the running loop, so the watch binds to it, not to a loop that is
            # replaced by asyncio.run (which is why Application(debug=True) would not do).
            _autoreload()
        if show:
            import webbrowser

            webbrowser.open(f"http://localhost:{port}")
        await asyncio.Event().wait()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


def _autoreload() -> None:
    """Restart the process when a Python module or a ``data/`` asset changes (development only).

    Autoreload already watches the imported modules; the ``data/`` assets (the CSS, HTML and fonts baked
    into a page when it is built) are not modules, so they are added to the watch by hand.
    """
    from tornado import autoreload

    from pfs.qa.dashboard.runReport import _DATA

    for asset in sorted(path for path in _DATA.rglob("*") if path.is_file()):
        autoreload.watch(str(asset))
    autoreload.start()
    print(
        "Auto-reload on: a change to the dashboard's code or its data/ assets restarts the server", flush=True
    )


def snapshot(args: argparse.Namespace) -> None:
    """Read the collection from qadb (and the opdb) once and write it, with its provenance, to ``--out``."""
    from pfs.qa.dashboard.live import HST, qadbProvenance, readCollection
    from pfs.qa.dashboard.qadbSource import writeSnapshot
    from pfs.qa.dashboard.runReport import readVisitInfo

    qadb, opdb = connect(args)
    start = time.monotonic()
    tables = readCollection(qadb, args.collection, args.first_visit, args.last_visit)
    if opdb is not None:
        visits = judgedVisits(tables)
        # What the run report shows of each visit taken: its time, exposure, sequence and how that ended.
        if visits:
            # From the first visit asked for, so a visit taken before the first one judged shows as unjudged.
            tables["visitInfo"] = readVisitInfo(opdb, args.first_visit or visits[0], visits[-1])
    provenance = qadbProvenance(
        f"{args.collection} (qadb snapshot)",
        builtAt=datetime.now(HST).isoformat(timespec="seconds"),
        qadb=f"{args.qadb_host}:{args.qadb_port}/{args.qadb_dbname}",
        opdb=f"{args.opdb_host}:{args.opdb_port}/{args.opdb_dbname}" if opdb is not None else None,
        visits=[args.first_visit, args.last_visit],
    )
    out = writeSnapshot(tables, args.out, provenance)
    size = sum(path.stat().st_size for path in out.iterdir()) / 1e6
    rows = ", ".join(f"{name} {len(frame)}" for name, frame in tables.items())
    print(f"{rows} rows -> {out} ({size:.2f} MB) in {time.monotonic() - start:.1f} s", flush=True)


def report(args: argparse.Namespace) -> None:
    """Write the report of the collection (``--collection``) or a snapshot (``--snapshot``) to ``--out``."""
    from pfs.qa.dashboard.live import HST
    from pfs.qa.dashboard.runReport import buildReport, readVisitInfo, sourceText

    start = time.monotonic()
    info = None
    listing = normalValues = None
    if args.collection:
        from pfs.qa.dashboard.live import readCollection

        qadb, opdb = connect(args)
        tables = readCollection(qadb, args.collection, args.first_visit, args.last_visit)
        if opdb is not None:
            visits = judgedVisits(tables)
            if visits:
                info = readVisitInfo(opdb, args.first_visit or visits[0], visits[-1])
        listing, normalValues = calibSetInputs(
            opdb, qadb, args.collection, info["time"] if info is not None else None
        )
        source = sourceText(args.collection, opdb is not None, f"{datetime.now(HST):%Y-%m-%d}")
    else:
        from pfs.qa.dashboard.qadbSource import readSnapshot

        tables, provenance = readSnapshot(args.snapshot)
        source = snapshotSource(tables, provenance)
    made = buildReport(
        tables, info=info, listing=listing, normalValues=normalValues, **reportOptions(args, source)
    )
    args.out.mkdir(parents=True, exist_ok=True)
    page = args.out / f"{args.name}.html"
    page.write_text(made.html)
    made.visits.to_csv(args.out / "visits.csv", index=False)
    made.images.to_csv(args.out / "images.csv", index=False)

    def tally(visits) -> str:
        counts = visits["status"].value_counts()
        return (
            ", ".join(f"{counts[k]} {k}" for k in ("PASS", "WARN", "FAIL", "UNKNOWN") if k in counts)
            or "none"
        )

    science = made.visits[made.visits["kind"] == "science"]
    print(
        f"{len(made.visits)} visits, {len(made.images)} images; science visits: {tally(science)}; "
        f"all visits: {tally(made.visits)}",
        flush=True,
    )
    print(f"Wrote {page} in {time.monotonic() - start:.1f} s", flush=True)


if __name__ == "__main__":
    main()
