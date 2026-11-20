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
    # Write the run report as one stand-alone page, for any web server, with other limits
    python bin/qaDashboard.py report --snapshot run31-qadb --out run31-report --kms-r 1 2

then open http://localhost:5006. To serve the local network, add ``--address 0.0.0.0``.
"""

import argparse
import asyncio
import contextlib
import logging
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

    limits = argparse.ArgumentParser(add_help=False)
    report = limits.add_argument_group("the run report: its science frames' limits, each WARN then FAIL")
    for arm, (warn, fail) in (("b", (1.5, 2.5)), ("r", (0.5, 1.0)), ("n", (0.5, 1.0)), ("m", (0.5, 1.0))):
        report.add_argument(
            f"--kms-{arm}",
            type=float,
            nargs=2,
            default=(warn, fail),
            metavar=("WARN", "FAIL"),
            help=f"km/s, on the {arm} arm's sky-line residual after the visit's adjustment (default {warn:g} {fail:g})",
        )
    for axis, what in (("x", "trace"), ("y", "sky-line")):
        report.add_argument(
            f"--offset-{axis}",
            type=float,
            nargs=2,
            default=(0.5, 1.0),
            metavar=("WARN", "FAIL"),
            help=f"pixels, on the {what} offset from the calibration (default 0.5 1)",
        )
    report.add_argument(
        "--no-mode-correction",
        action="store_true",
        help="judge visits read with the m arm against the calibration as it is, subtracting nothing",
    )
    report.add_argument(
        "--title", default="Visit verdicts", help="the report's title, e.g. 'Run31 visit verdicts'"
    )

    parser = argparse.ArgumentParser(description="The QA dashboard.")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser(
        "serve",
        parents=[databases, limits],
        help="serve the run report, live from qadb or from a snapshot",
    )
    source = serve.add_mutually_exclusive_group(required=True)
    source.add_argument("--collection", help="the collection loaded into qadb, e.g. qaActor/reductions")
    source.add_argument(
        "--snapshot", type=Path, metavar="DIR", help="a snapshot written by the snapshot command"
    )
    serve.add_argument(
        "--refresh", type=float, default=15.0, help="least seconds between qadb reads (default 15)"
    )
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
        parents=[databases, limits],
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
    for name in ("kms_b", "kms_r", "kms_n", "kms_m", "offset_x", "offset_y"):
        warn, fail = getattr(args, name, (1, 1))
        if not 0 < warn <= fail:
            parser.error(f"--{name.replace('_', '-')}: WARN then FAIL, both above zero")
    if args.command in ("serve", "report") and args.snapshot is not None:
        missing = [
            name for name in ("provenance.json", "iqQaMetrics.parquet") if not (args.snapshot / name).exists()
        ]
        if missing:
            parser.error(
                f"{args.snapshot} is not a snapshot (no {', '.join(missing)}): write one with "
                f"`qaDashboard.py snapshot --collection <collection> --out {args.snapshot}`"
            )
    {"snapshot": snapshot, "report": report, "serve": serve}[args.command](args)


def connect(args: argparse.Namespace):
    """Return the qadb, and the opdb or None, that ``args`` name; both are only read."""
    from pfs.utils.database.qadb import QaDB

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


def reportOptions(args: argparse.Namespace, source: str) -> dict:
    """Return what `pfs.qa.dashboard.runReport.buildReport` takes from the command line."""
    from pfs.qa.dashboard.runReport import Limits

    limits = Limits(args.offset_x, args.offset_y, {arm: getattr(args, f"kms_{arm}") for arm in "brnm"})
    return {
        "limits": limits,
        "correctModes": not args.no_mode_correction,
        "title": args.title,
        "source": source,
    }


def snapshotSource(tables: dict, provenance: dict) -> str:
    """Return the report's note on where a snapshot's results come from."""
    from pfs.qa.dashboard.runReport import sourceText

    collection = str(provenance.get("collection", "")).removesuffix(" (qadb snapshot)")
    readOn = str(provenance.get("builtAt") or "")[:10] or None
    return sourceText(collection, "visitInfo" in tables, readOn)


def serve(args: argparse.Namespace) -> None:
    """Serve the run report of a collection in qadb (``--collection``) or of a snapshot (``--snapshot``)."""
    # One line per request, with the client's address: shows whether a browser reached the server.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("tornado.access").setLevel(logging.INFO)

    from tornado.web import Application

    from pfs.qa.dashboard.live import QadbStore
    from pfs.qa.dashboard.qadbSource import readSnapshot
    from pfs.qa.dashboard.runReport import (
        ReportCache,
        indexHandler,
        infoFromTables,
        readVisitInfo,
        sourceText,
    )

    if args.collection:
        qadb, opdb = connect(args)
        # One store for every browser: qadb is read once, whoever asks, and not again within --refresh.
        store = QadbStore.fromQadb(
            qadb, args.collection, args.first_visit, args.last_visit, minInterval=args.refresh
        )
        print(f"{len(store.times)} visits so far", flush=True)
        readInfo = None if opdb is None else (lambda first, last: readVisitInfo(opdb, first, last))
        cache = ReportCache(
            store,
            readInfo,
            firstVisit=args.first_visit or None,
            **reportOptions(args, sourceText(args.collection, opdb is not None)),
        )
    else:
        tables, provenance = readSnapshot(args.snapshot)
        store = QadbStore.fromTables(tables)
        print(f"{len(store.times)} visits in {args.snapshot}", flush=True)
        cache = ReportCache(
            store, info=infoFromTables(tables), **reportOptions(args, snapshotSource(tables, provenance))
        )

    print(f"Serving on http://{args.address}:{args.port}", flush=True)
    listen(Application([(r"/", indexHandler(cache))]), args.address, args.port, args.show)


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
    from pfs.drp.qa.qadb.reader import readQadb
    from pfs.qa.dashboard.live import HST, qadbProvenance
    from pfs.qa.dashboard.qadbSource import writeSnapshot
    from pfs.qa.dashboard.runReport import readVisitInfo

    qadb, opdb = connect(args)
    start = time.monotonic()
    tables = readQadb(qadb, args.collection, args.first_visit, args.last_visit)
    if opdb is not None:
        visits = sorted({int(visit) for visit in tables["iqQaMetrics"]["visit"]})
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
    """Judge the collection (``--collection``) or a snapshot (``--snapshot``) and write its report to ``--out``."""
    from pfs.qa.dashboard.live import HST
    from pfs.qa.dashboard.runReport import buildReport, readVisitInfo, sourceText

    start = time.monotonic()
    info = None
    if args.collection:
        from pfs.drp.qa.qadb.reader import readQadb

        qadb, opdb = connect(args)
        tables = readQadb(qadb, args.collection, args.first_visit, args.last_visit)
        if opdb is not None:
            visits = sorted({int(visit) for visit in tables["iqQaMetrics"]["visit"]})
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
    if len(made.constants):
        subtracted = ", ".join(f"{r.camera} {r.axis} {r.constant:+.2f}" for r in made.constants.itertuples())
        print(f"Subtracted on visits read with m (px): {subtracted}", flush=True)
    print(f"Wrote {args.out / 'index.html'} in {time.monotonic() - start:.1f} s", flush=True)


if __name__ == "__main__":
    main()
