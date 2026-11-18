#!/usr/bin/env python3
r"""The QA dashboard: serve a run's report live from qadb or from a snapshot, write it as a file, or save a snapshot.

Stack-free; needs panel and duckdb, and psycopg for qadb.

Examples::

    # A run as qaActor loads it, with the opdb's times and sequences: only read, every 30 s
    # (credentials in ~/.pgpass); each night is compared with the reference visits named
    python bin/qaDashboard.py serve --collection qaActor/reductions --first-visit 151000 \
        --qadb-host 127.0.0.1 --qadb-port 5436 --opdb-host 127.0.0.1 --opdb-port 5432 \
        --title "Run31 visit verdicts" --reference firstNight=151000-151200
    # Read it once into a snapshot (parquet and provenance.json), then serve that offline
    python bin/qaDashboard.py snapshot --collection qaActor/reductions --opdb-host 127.0.0.1 --out run31-qadb
    python bin/qaDashboard.py serve --snapshot run31-qadb
    # Write the run report as one stand-alone page, for any web server, with other limits
    python bin/qaDashboard.py report --snapshot run31-qadb --out run31-report --kms-r 1 2

then open http://localhost:5006: the run report is the index, and the metric views are at ``/dashboard``.
To serve the local network, add ``--address 0.0.0.0 --allow-origin <this host's name or IP>``.
"""

import argparse
import logging
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path


def _reference(text: str):
    """Return the `pfs.drp.qa.qadb.compareRuns.Reference` that ``NAME=[COLLECTION:]FIRST-LAST`` names."""
    from pfs.drp.qa.qadb.compareRuns import parseReference

    try:
        return parseReference(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


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
    qadb.add_argument(
        "--reference",
        type=_reference,
        action="append",
        default=[],
        metavar="NAME=[COLLECTION:]FIRST-LAST",
        help="reference visits each night is compared with, in the same collection unless one is given; "
        "repeat for more (default: none, and no comparison)",
    )
    opdb = databases.add_argument_group("the opdb's IIC sequences, visit times and notes (default: none)")
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
        help="serve the run report (the index) and the metric views (/dashboard), live from qadb or from a snapshot",
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
    serve.add_argument(
        "--allow-origin",
        nargs="*",
        default=[],
        help="extra host[:port] names browsers may use for this server ('*' for any), with --address 0.0.0.0",
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
    names = [reference.name for reference in args.reference]
    if len(set(names)) < len(names):
        parser.error("--reference: each needs its own name")
    for name in ("kms_b", "kms_r", "kms_n", "kms_m", "offset_x", "offset_y"):
        warn, fail = getattr(args, name, (1, 1))
        if not 0 < warn <= fail:
            parser.error(f"--{name.replace('_', '-')}: WARN then FAIL, both above zero")
    if args.command in ("serve", "report") and args.snapshot is not None:
        if args.reference:
            parser.error("--reference reads qadb: a snapshot serves the references it was written with")
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
    """Serve the run report and the metric views over qadb (``--collection``) or a snapshot (``--snapshot``)."""
    # One line per request, with the client's address: shows whether a browser reached the server.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("tornado.access").setLevel(logging.INFO)
    logging.getLogger("bokeh").setLevel(logging.INFO)

    from pfs.qa.dashboard.live import QadbMetricsStore
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
        # One store for every session: each refresh reads qadb (and the opdb) once, whoever asks.
        store = QadbMetricsStore.fromQadb(
            qadb,
            args.collection,
            args.first_visit,
            args.last_visit,
            minInterval=args.refresh / 2,
            opdb=opdb,
            references=args.reference,
        )
        print(f"{store.table['visit'].nunique()} visits so far", flush=True)
        readInfo = None if opdb is None else (lambda visits: readVisitInfo(opdb, visits))
        cache = ReportCache(
            store, readInfo, **reportOptions(args, sourceText(args.collection, opdb is not None))
        )
    else:
        store = QadbMetricsStore.fromSnapshot(args.snapshot)
        print(f"{store.table['visit'].nunique()} visits in {args.snapshot}", flush=True)
        tables, provenance = readSnapshot(args.snapshot)
        cache = ReportCache(
            store, info=infoFromTables(tables), **reportOptions(args, snapshotSource(tables, provenance))
        )

    import panel as pn

    from pfs.qa.dashboard.app import createApp

    origins = [f"localhost:{args.port}", f"127.0.0.1:{args.port}"]
    origins += [origin if ":" in origin else f"{origin}:{args.port}" for origin in args.allow_origin]
    print(f"Serving on {args.address}:{args.port}, for {', '.join(origins)}", flush=True)
    # The run report is the index; the metric views keep their own address.
    pn.serve(
        {"dashboard": lambda: createApp(store, args.refresh)},
        address=args.address,
        port=args.port,
        show=args.show,
        title="PFS QA dashboard",
        websocket_origin=origins,
        extra_patterns=[(r"/", indexHandler(cache))],
        redirect_root=False,
    )


def snapshot(args: argparse.Namespace) -> None:
    """Read the collection from qadb (and the opdb) once and write it, with its provenance, to ``--out``."""
    from pfs.drp.qa.qadb.compareRuns import metricTables
    from pfs.drp.qa.qadb.reader import readQadb
    from pfs.qa.dashboard.live import HST, opdbReader, qadbProvenance
    from pfs.qa.dashboard.qadbSource import referenceRecords, referenceSnapshotTables, writeSnapshot
    from pfs.qa.dashboard.runReport import readVisitInfo

    qadb, opdb = connect(args)
    start = time.monotonic()
    tables = readQadb(qadb, args.collection, args.first_visit, args.last_visit)
    if opdb is not None:
        visits = sorted({int(visit) for visit in tables["iqQaMetrics"]["visit"]})
        tables["opdbVisitTimes"], tables["notes"] = opdbReader(opdb)(visits)
        # What the run report shows of each visit: its time, exposure time and sequence type.
        tables["visitInfo"] = readVisitInfo(opdb, visits)
    tables.update(
        referenceSnapshotTables(
            {
                r.name: metricTables(qadb, r.collection or args.collection, r.first, r.last)
                for r in args.reference
            }
        )
    )
    provenance = qadbProvenance(
        f"{args.collection} (qadb snapshot)",
        references=referenceRecords(args.reference, args.collection),
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
            info = readVisitInfo(opdb, {int(visit) for visit in tables["iqQaMetrics"]["visit"]})
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
