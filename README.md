# pfs_qa_dashboard

The PFS image QA in a browser: the **run report**, one stand-alone page giving every visit of a collection a
verdict. It is served live during a run from qadb (with the opdb's visits), or offline from a snapshot of a qadb
read, or written once as a file. It reads what drp_qa's QA tasks stored.

## The run report

One page, with its styles, fonts, charts and script inline, so the same file can be served by any web server or
opened from disk, and fetches nothing. It shows every visit by night, on a clock (HST; the night, 18:00 to 07:00, or the whole day) or in the order
taken, for the kinds of frame, arm and spectrograph ticked (science and full-slit calibrations to start with);
the visits counted by kind, what was measured on the science frames per detector, each night, the visits that do not pass,
and a filterable list of every visit. A visit takes the worst verdict among its detectors.

Science frames are judged here, from what `dmLineResiduals` measured, at limits given on the command line (each
WARN then FAIL; a value at a limit has crossed it):

| What | Measured as | Default limits |
|---|---|---|
| Offset from the calibration, x | `medianDeltaX` of the traces against the calibration detectorMap | `--offset-x 0.5 1` px |
| Offset from the calibration, y | `medianDeltaY` of the sky species furthest off | `--offset-y 0.5 1` px |
| Sky-line residual after the visit's adjustment | `medianDeltaY` of the held-back lines of the arm's best-measured species (OH on r, n and m; [O I] on b) against the visit's own detectorMap, in km/s with the dispersion at those lines (`data/kmsPerPixel.csv`) | `--kms-r`, `--kms-n`, `--kms-m 0.5 1`; `--kms-b 1.5 2.5` km/s |

Width and line scatter are not judged on science frames. Every other kind of frame keeps the verdict its QA tasks
stored (all-sky frames `dmLineResiduals`' alone).

On visits read with the m arm, the calibration (made with the r arm) sits a steady fraction of a pixel from the b
arm's traces and the n arm's lines. The report measures that per detector from the collection's own full-slit
quartz (b, x) and Neon arcs (n, y), as the median with m minus the median with r where there are at least three
visits in each mode, subtracts it before the pixel limits, and says so in an appendix. `--no-mode-correction` turns
that off.

```bash
# The page, with visits.csv and images.csv beside it, from qadb or from a snapshot
python bin/qaDashboard.py report --collection qaActor/reductions --first-visit <first visit of the run> \
    --opdb-host 127.0.0.1 --opdb-port 5432 --title "Run31 visit verdicts" --out <dir> [--kms-r 1 2 ...]
python bin/qaDashboard.py report --snapshot <dir> --out <dir>
```

`serve` takes the same limits and title, and answers `/` with the page, made again whenever qadb holds new
results.

**Visits with no result.** With the opdb, the report reads every visit that exposed the spectrograph from
`--first-visit` (the earliest visit judged, by default) to the latest visit judged. One taken as an object, arc or
flat frame that qadb holds no result for is shown as `NO RESULT`: an outline on the calendar at its own time, a
count beside each tally and night, and a section listing each with how its sequence ended. Live results arrive in
the order the visits were taken (on Run31, none of 859 live visits was judged after a later one), so a visit older
than the latest one judged was passed over; a newer one is not shown, as its result may be on its way. The page
does not say why a result is missing. Without the opdb, times are qadb's and no visit can be told to lack a
result.

Built on drp_qa (`pfs.drp.qa`) for reading qadb back, and served with Tornado.

## Running it

The dashboard needs drp_qa (`pfs.drp.qa`, with `pfs.drp.qa.qadb.reader`) and pfs_utils importable: set up in the
LSST/PFS stack, or their checkouts' `python/` on `PYTHONPATH`. Stack-free, [uv](https://docs.astral.sh/uv/) makes
`.venv` from `pyproject.toml` on its own Python, apart from the system's:

```bash
uv sync --python 3.12 --managed-python
PYTHONPATH=<drp_qa>/python:<pfs_utils>/python .venv/bin/python bin/qaDashboard.py {serve,snapshot,report} ...
```

or, without a venv, `uv run --no-project --python 3.12 --with numpy,pandas,pyarrow,pyyaml,scipy,sqlalchemy,tornado,'psycopg[binary]'`
with `python:` first on `PYTHONPATH`.

**Live, from qadb.** qaActor loads every visit it judges into qadb. `serve` reads a collection back
(`pfs.drp.qa.qadb.reader.readQadb`) and every `--refresh` seconds reads only the images loaded since
(`qa_gate.decided_at`), making the page again when there are any. An image judged in more than one RUN of the
collection shows the latest.

The databases are read on a thread of their own, and a browser is answered at once with the last page made, so
a database that fails or hangs does not hold the page up. The page served follows the server: every few seconds
it asks for `/status`, reloads itself when a newer page has been made (keeping the filters, buttons and place
as they were left), and says at the bottom right when the results were last read. That turns red, with the
error, when a read fails, or when none has worked for three intervals (90 s at least), as when a connection
hangs. A connection that is not answered is given up on after 10 s (`PGCONNECT_TIMEOUT`, when not already set);
a query that hangs on an open connection is not interrupted, and the server may then need restarting. A page
written by `report`, or served from a snapshot, has none of this and fetches nothing.

**With the opdb** (`--opdb-host`, read-only `public_user`), each visit has the time it was taken, its exposure
time, cameras and IIC sequence, and visits with no result are shown (above). Without it, times are qadb's
`pfs_visit.issued_at` (HST).

```bash
# qadb as pfs (--qadb-user), the opdb as read-only public_user; credentials in ~/.pgpass
python bin/qaDashboard.py serve --collection qaActor/reductions --first-visit <first visit of the run> \
    --qadb-host 127.0.0.1 --qadb-port 5436 --opdb-host 127.0.0.1 --opdb-port 5432 [--refresh 30]
```

**Offline, from a snapshot.** `snapshot --out <dir>` reads the collection once and writes what was read, one
parquet file per table (with what the run report shows of each visit, given `--opdb-host`), and
`provenance.json` (the databases, the time read and the drp_qa that read). `serve --snapshot <dir>` and
`report --snapshot <dir>` use it without a database. A run judged after the fact is loaded into a qadb first
(drp_qa's `bin.src/loadQadb.py`; a local copy will do).

```bash
python bin/qaDashboard.py snapshot --collection <collection> --out <dir> [--qadb-... as above]
python bin/qaDashboard.py serve --snapshot <dir>
```

The server listens on localhost:5006; add `--address 0.0.0.0` to serve the network.

## Tests and lint

```bash
PYTHONPATH=python:<drp_qa>/python:<pfs_utils>/python uv run --no-project --python 3.12 \
    --with pytest,numpy,pandas,pyarrow,pyyaml,scipy,sqlalchemy,tornado python -m pytest
uv run --no-project --with ruff==0.14.13 ruff check . && uv run --no-project --with ruff==0.14.13 ruff format --check .
```

CI runs both, against the drp_qa named by `DRP_QA_REF` in `.github/workflows/tests.yml`.

## Versions

Releases are `MAJOR.MINOR.PATCH` git tags ([Semantic Versioning](https://semver.org)), with `version` in
`pyproject.toml` to match; user-visible changes go under `## [Unreleased]` in `CHANGELOG.md` until then.
