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
    serve.add_argument("--port", type=int, default=5006)
    serve.add_argument(
        "--address", default="localhost", help="interface to listen on; 0.0.0.0 for the local network"
    )
    serve.add_argument("--show", action="store_true", help="open a browser")

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
        help="write the run report as one stand-alone page (index.html), with its visits and images as CSV",
    )
    source = report.add_mutually_exclusive_group(required=True)
    source.add_argument("--collection", help="the collection loaded into qadb, e.g. qaActor/reductions")
    source.add_argument(
        "--snapshot", type=Path, metavar="DIR", help="a snapshot written by the snapshot command"
    )
    report.add_argument("--out", type=Path, required=True, metavar="DIR", help="the directory written to")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = makeParser()
    args = parser.parse_args(argv)
    if args.command == "serve" and args.refresh < 1:
        parser.error("--refresh: at least 1 second")
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


def snapshotSource(tables: dict, provenance: dict) -> str:
    """Return the report's note on where a snapshot's results come from."""
    from pfs.qa.dashboard.runReport import sourceText

    collection = str(provenance.get("collection", "")).removesuffix(" (qadb snapshot)")
    readOn = str(provenance.get("builtAt") or "")[:10] or None
    return sourceText(collection, "visitInfo" in tables, readOn)


def pages(args: argparse.Namespace, source: str) -> dict:
    """Return the pages a `ReportCache` builds: the night page (``/``) and the run report (``/run``)."""
    from pfs.qa.dashboard.nightReport import buildNightReport
    from pfs.qa.dashboard.runReport import buildReport

    return {
        "night": (buildNightReport, {"title": args.title, "source": source, "runUrl": "/run"}),
        "report": (buildReport, {"title": args.title, "source": source}),
    }


def serve(args: argparse.Namespace) -> None:
    """Serve the night page (``/``) and the run report (``/run``) of a collection or a snapshot."""
    # One line per request, with the client's address: shows whether a browser reached the server.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("tornado.access").setLevel(logging.INFO)

    from tornado.web import Application

    from pfs.qa.dashboard.live import QadbStore
    from pfs.qa.dashboard.nightReport import visitStub
    from pfs.qa.dashboard.qadbSource import readSnapshot
    from pfs.qa.dashboard.runReport import ReportCache, infoFromTables, readVisitInfo, routes, sourceText

    if args.collection:
        qadb, opdb = connect(args)
        store = QadbStore.fromQadb(qadb, args.collection, args.first_visit, args.last_visit)
        print(f"{len(store.times)} visits so far", flush=True)
        readInfo = None if opdb is None else (lambda first, last: readVisitInfo(opdb, first, last))
        cache = ReportCache(
            store,
            readInfo,
            firstVisit=args.first_visit or None,
            pages=pages(args, sourceText(args.collection, opdb is not None)),
        )
        cache.update()
        # The databases are read on a thread of the cache's own from here on: a browser gets the last
        # page made at once, whatever the databases are doing.
        cache.watch(args.refresh)
    else:
        tables, provenance = readSnapshot(args.snapshot)
        store = QadbStore.fromTables(tables)
        print(f"{len(store.times)} visits in {args.snapshot}", flush=True)
        cache = ReportCache(
            store, info=infoFromTables(tables), pages=pages(args, snapshotSource(tables, provenance))
        )
        cache.update()

    mounts = {"/": "night", "/run": "report"}
    print(
        f"Serving the night page at http://{args.address}:{args.port}/ and the run report at /run", flush=True
    )
    listen(Application(routes(cache, mounts, visit=visitStub)), args.address, args.port, args.show)


def listen(application, address: str, port: int, show: bool = False) -> None:
    """Serve a Tornado ``application`` on ``address:port`` until interrupted; ``show`` opens a browser on it."""

    async def run() -> None:
        application.listen(port, address=address)
        if show:
            import webbrowser

            webbrowser.open(f"http://localhost:{port}")
        await asyncio.Event().wait()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


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
    if args.collection:
        from pfs.qa.dashboard.live import readCollection

        qadb, opdb = connect(args)
        tables = readCollection(qadb, args.collection, args.first_visit, args.last_visit)
        if opdb is not None:
            visits = judgedVisits(tables)
            if visits:
                info = readVisitInfo(opdb, args.first_visit or visits[0], visits[-1])
        source = sourceText(args.collection, opdb is not None, f"{datetime.now(HST):%Y-%m-%d}")
    else:
        from pfs.qa.dashboard.qadbSource import readSnapshot

        tables, provenance = readSnapshot(args.snapshot)
        source = snapshotSource(tables, provenance)
    made = buildReport(tables, info=info, **reportOptions(args, source))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(made.html)
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
    print(f"Wrote {args.out / 'index.html'} in {time.monotonic() - start:.1f} s", flush=True)


if __name__ == "__main__":
    main()
