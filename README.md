# pfs_qa_dashboard

The PFS image QA in a browser: three stand-alone pages giving every visit of a collection a verdict — what is
being observed now, the whole run, and one visit in detail. Served live during a run from qadb (with the opdb's
visits), or offline from a snapshot, or written once as files. It reads what drp_qa's QA tasks stored and **judges
nothing**: every verdict and reason is an image's row of qadb's `qa_gate`, which drp_qa's gate wrote (read back
with `pfs.drp.qa.qadb.reader`). An image with no gate row is UNKNOWN.

Each page has its styles, fonts, charts and script inline, so it is self-contained and fetches nothing, and the
three share one stylesheet (`data/theme.css`). A visit takes the worst verdict among its detectors.

## The pages

- **The night page** (`/`, the index) — the current night, for "what is being observed now": the latest visit, the
  night on a clock (18:00 to 07:00 HST), counts for the night, the science frames' offset and residual bars, the
  visits that do not pass, and the night's visits most-recent-first. With the opdb it shows **awaiting QA**: a visit
  taken after the latest one judged, with no result yet — so the page moves to tonight on the first exposure, before
  any reduction. The most recent night with a visit is shown by default.

- **The run report** (`/run`) — every visit of the whole collection, by night on a clock or in the order taken, for
  the kinds of frame, arm and spectrograph ticked; the visits counted by kind, what was measured on the science
  frames per detector, each night, the visits that do not pass, and a filterable list of every visit. The page
  lists the gate's checks and their limits (`pfs.drp.qa.metrics.registry.CHECKS`) and draws the science checks'
  limits beside what was measured: the offsets from the calibration in x (traces) and y (the sky species furthest
  off), and the sky-line residual after the visit's adjustment, in km/s on the arm's best-measured species (OH on
  r, n and m; [O I] on b), with the dispersion at those lines (`data/kmsPerPixel.csv`).

- **The per-visit page** (`/visit/<n>`) — one visit over all its detectors: the header (time, exposure, kind and
  lamp, IIC sequence and how it ended, overall verdict), each detector's verdict and the gate's reason, a metric ×
  camera grid of the stored values (medFWHM, lines per fiber, pct flagged, trace centre, offsets, km/s residual)
  coloured by verdict, and the sky-line residual by species. A visit with no QA rows — engineering, or not yet
  reduced — reads "No QA result (not reduced)" from the opdb's sequence information. It shows values and leans on
  the stored reason for the limits, rather than re-deriving them from the current registry (a visit may have been
  judged with a different threshold set). The page is built from the server's cached read, so it needs no database
  of its own. With `serve --butler` (below) it also shows each detector's **detector-map line residual plot** —
  the drift and scatter views drawn from the stored per-line data, no-go detectors first.

Every visit on the run report and both clocks links to its `/visit/<n>` page (the every-visit table, the
not-passing and no-result lists, and each heatmap square).

**Awaiting QA and no result.** With the opdb, each visit has the time it was taken, its exposure, cameras and IIC
sequence. A visit that exposed the spectrograph as an object, arc or flat frame and has no result in qadb is shown
as `NO RESULT` when it is older than the latest visit judged (it was passed over) and, on the night page, as
`AWAITING` when it is newer (its result may be on its way). Without the opdb, times are qadb's
`pfs_visit.issued_at` (HST) and no visit can be told to lack a result.

Built on drp_qa (`pfs.drp.qa`) for reading qadb back, and served with Tornado.

## Serving it live

```bash
# qadb as pfs (--qadb-user), the opdb as read-only public_user; credentials in ~/.pgpass
python bin/qaDashboard.py serve --collection qaActor/reductions --first-visit <first visit of the run> \
    --qadb-host 127.0.0.1 --qadb-port 5436 --opdb-host 127.0.0.1 --opdb-port 5432 [--refresh 30]
```

`serve` answers `/` with the night page, `/run` with the run report, `/visit/<n>` with a visit's page, and
`/status` with what the live pages poll. It listens on `localhost:5006`; add `--address 0.0.0.0` to serve the
network.

Every `--refresh` seconds, on a thread of its own, it reads qadb for the images loaded or judged since the last
read (`qa_gate.decided_at`, less five minutes, so a load committed out of order is still taken) and, with the
opdb, the visits taken since — so a new exposure makes the pages again and the browser reloads even while the run
is not being reduced. The whole opdb range is re-read on a qadb change, so judged visits stay current. The
databases are read off the server's loop, and a browser is answered at once with the last page made, so a database
that fails or hangs does not hold the page up.

To show the detector-map residual plots on the per-visit page, add `--butler <repo>` (e.g. `/work/datastore`),
pointing at a Butler on the same host that holds the collection's `dmLineResidualData` — the per-line data the
plot needs, which lives in the Butler, not qadb. This needs the LSST stack and `--collection` (a snapshot has no
per-line data). Each plot is rendered when its `<img>` (`/visit/<n>/residual/<camera>.png`) is fetched, on a small
thread pool off the server's loop, and cached — so the page is answered at once and nothing is drawn until a visit
is viewed. Without `--butler` the page shows no plot.

The live pages follow the server: every few seconds each asks for `/status`, reloads when a newer page has been
made (keeping the filters, buttons and place), and says at the bottom right when the results were last read — red
with the error when a read fails, amber ("Still reading") when none has worked for three intervals (90 s at least)
and one is under way, and red when none is. A connection that is not answered is given up on after 10 s
(`PGCONNECT_TIMEOUT`, when not already set); a query that hangs on an open connection is not interrupted, and the
server may then need restarting.

## Offline: a file, or a snapshot

```bash
# Write the run report as one stand-alone page (<name>.html, default index.html), with visits.csv and images.csv
python bin/qaDashboard.py report --collection qaActor/reductions --first-visit <first visit> \
    --opdb-host 127.0.0.1 --opdb-port 5432 --title "Run31 visit verdicts" --name run31 --out <dir>

# Read a collection once into a snapshot (parquet per table + provenance.json), then serve or write it offline
python bin/qaDashboard.py snapshot --collection qaActor/reductions --opdb-host 127.0.0.1 --out <dir>
python bin/qaDashboard.py serve  --snapshot <dir>
python bin/qaDashboard.py report --snapshot <dir> --out <dir>
```

`report` writes the run report alone and fetches nothing (no live script). A snapshot keeps the gate's verdicts
(`qaGate.parquet`); one written before the dashboard read the gate is refused — write it again. A run judged after
the fact is loaded into a qadb first (drp_qa's `bin.src/loadQadb.py`; a local copy will do). The report is made
for any collection in qadb: the live one (`qaActor/reductions`) or a redone run's.

## Developing the pages

`serve --debug` restarts the server whenever the dashboard's code or its `data/` assets (`theme.css`, the
HTML, the fonts) change — Tornado's autoreload. `--refresh` (above) only reloads the *data*; `--debug`
reloads the *code*. A restart re-reads the databases, so develop against a snapshot, which re-reads in no
time and never needs the tunnel:

```bash
python bin/qaDashboard.py snapshot --collection qaActor/reductions --opdb-host 127.0.0.1 --out <dir>
python bin/qaDashboard.py serve --snapshot <dir> --debug
```

## Running it

The dashboard needs drp_qa 0.5.0 or later (`pfs.drp.qa`, with `pfs.drp.qa.qadb.reader.readGate` and the gate's
`metrics.registry`), for a qadb at spt_qa_database 0.6.0, and pfs_utils importable: set up in the LSST/PFS stack,
or their checkouts' `python/` on `PYTHONPATH`. Stack-free, [uv](https://docs.astral.sh/uv/) makes `.venv` from
`pyproject.toml` on its own Python, apart from the system's:

```bash
uv sync --python 3.12 --managed-python
PYTHONPATH=<drp_qa>/python:<pfs_utils>/python .venv/bin/python bin/qaDashboard.py {serve,snapshot,report} ...
```

or, without a venv, `uv run --no-project --python 3.12 --with numpy,pandas,pyarrow,pyyaml,scipy,sqlalchemy,tornado,'psycopg[binary]'`
with `python:` first on `PYTHONPATH`.

## Tests and lint

```bash
PYTHONPATH=python:<drp_qa>/python:<pfs_utils>/python uv run --no-project --python 3.12 \
    --with pytest,numpy,pandas,pyarrow,pyyaml,scipy,sqlalchemy,tornado python -m pytest
uv run --no-project --with ruff==0.14.13 ruff check . && uv run --no-project --with ruff==0.14.13 ruff format --check .
```

CI runs both, against the drp_qa named by `DRP_QA_REF` in `.github/workflows/tests.yml`.

## Versions

Releases are `MAJOR.MINOR.PATCH` git tags ([Semantic Versioning](https://semver.org)); the package version is
derived from the latest tag by [setuptools-scm](https://setuptools-scm.readthedocs.io/), so cutting a release is
a tag alone — there is no version to edit in `pyproject.toml`. User-visible changes go under `## [Unreleased]` in
`CHANGELOG.md` until then.
