# pfs_qa_dashboard

The PFS image QA in a browser, live during a run from qadb (with the opdb's visit times, IIC sequences and
observers' notes), or offline from a snapshot of a qadb read. It reads what drp_qa's QA tasks stored; it measures
nothing itself.

The server's index is the **run report**: one stand-alone page giving every visit of the collection a verdict
(below). The **metric views** at `/dashboard` show the stored values against the limits their images were judged
with (the gate's own `judge` and the thresholds qadb records for the image): the provenance strip, the verdicts so
far, and a night against reference visits.

## The run report

One page, with its styles, charts and script inline, so the same file can be served by any web server or opened
from disk. It shows every visit by night, on a clock (HST; the night, 18:00 to 07:00, or the whole day) or in the order
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

`serve` takes the same limits and title, and makes the page again whenever qadb holds new results.

**Visits with no result.** With the opdb, the report reads every visit that exposed the spectrograph from
`--first-visit` (the earliest visit judged, by default) to the latest visit judged. One taken as an object, arc or
flat frame that qadb holds no result for is shown as `NO RESULT`: an outline on the calendar at its own time, a
count beside each tally and night, and a section listing each with how its sequence ended. Live results arrive in
the order the visits were taken (on Run31, none of 859 live visits was judged after a later one), so a visit older
than the latest one judged was passed over; a newer one is not shown, as its result may be on its way. The page
does not say why a result is missing. Without the opdb, times are qadb's and no visit can be told to lack a
result.

Built on Panel and DuckDB, and on drp_qa (`pfs.drp.qa`) for reading qadb back, thresholds included, and for the
gate's matching of an image to its thresholds entry.

## Running it

The dashboard needs drp_qa (`pfs.drp.qa`, with `pfs.drp.qa.qadb.reader`) and pfs_utils importable: set up in the
LSST/PFS stack, or their checkouts' `python/` on `PYTHONPATH`. Stack-free, [uv](https://docs.astral.sh/uv/) makes
`.venv` from `pyproject.toml` on its own Python, apart from the system's:

```bash
uv sync --python 3.12 --managed-python
PYTHONPATH=<drp_qa>/python:<pfs_utils>/python .venv/bin/python bin/qaDashboard.py {serve,snapshot,report} ...
```

or, without a venv, `uv run --no-project --python 3.12 --with numpy,pandas,pyarrow,pyyaml,scipy,sqlalchemy,duckdb,panel,'psycopg[binary]'`
with `python:` first on `PYTHONPATH`.

**Live, from qadb.** qaActor loads every visit it judges into qadb. The dashboard reads a collection
back (`pfs.drp.qa.qadb.reader.readQadb`) and every `--refresh` seconds reads only the images loaded since
(`qa_gate.decided_at`), so the page follows the run. One read serves every browser session. An image judged in more
than one RUN of the collection shows the latest. Each value's limits are the thresholds qadb records its image was
judged with: every layer the task judged with, in order (`qa_processing_thresholds`, once per Butler RUN and task:
its thresholds files, then its config's threshold fields as `<task>.config`), whose entries the reader returns beside
the images. The gate's
`judge` finds each value's entry in the first layer that has one, as the task did, so the limits shown are the ones
the image was judged with, and a config override or a thresholds file changed since cannot make them disagree with
the stored verdict. The image verdicts and the line-residual rows' statuses are shown as stored; `imageQualityQa`
stores no per-metric status, so an IQ value's own PASS/WARN/FAIL is `judge`'s, from those same entries. An image
the loader could cite nothing for shows its verdict alone. The provenance strip names the layers cited.

**With the opdb** (`--opdb-host`, read-only `public_user`), each visit has its IIC sequence and the start of its
first SpS exposure, and the observers' notes on it, its cameras and its sequence. Notes are often written after a
visit is judged, so each refresh re-reads them for the latest 300 visits. Without it, times are qadb's
`pfs_visit.issued_at` (HST), sequences are inferred from runs of one `seqName`, and there are no notes. Only the
times show yet; the sequences and notes are read for the views to come.

```bash
# qadb as pfs (--qadb-user), the opdb as read-only public_user; credentials in ~/.pgpass
python bin/qaDashboard.py serve --collection qaActor/reductions --first-visit <first visit of the run> \
    --qadb-host 127.0.0.1 --qadb-port 5436 --opdb-host 127.0.0.1 --opdb-port 5432 [--refresh 30] \
    [--reference firstNight=<first>-<last> --reference lastRun=<collection>:<first>-<last>]
```

**Against reference visits.** Each `--reference NAME=[COLLECTION:]FIRST-LAST` names visits to compare with (the
run's own first calibration night, the previous run's set; in the served collection unless one is given). They are
read from qadb once at start-up, and a snapshot keeps them. The comparison view then shows a night at a time (HST
evening date): a strip of its visits, each coloured by the worst of its images' verdicts, and one grid per
reference with a cell per metric, frame type and camera. A cell is the night's median against the reference's,
by drp_qa's `pfs.drp.qa.metrics.runComparison.compareRuns`, made when the page asks and stored nowhere: the
relative difference and `PASS`/`WARN`/`FAIL` for a metric with a tolerance, the difference in the metric's own
units for one without. A median hides one bad visit among good ones, so a cell also counts the visits whose own
value is beyond the `FAIL` tolerance. A cell whose medians rest on three images or fewer, on either side, is an
outline, not filled, and says the smaller count (`n=2`): one odd exposure decides it. Cells without a verdict
(the metric declares no tolerance, or the reference has no values) are left out unless *Hide unjudged cells* is
cleared; a line under each grid says how many. A cell made of several populations (illumination, lamp, species, line set)
shows the worst; choosing it lists them all, and each visit's value. Without `--reference` the view says how to
name one.

**Offline, from a snapshot.** `snapshot --out <dir>` reads the collection once and writes what was read, one
parquet file per table (the thresholds cited included; with the opdb's times, notes and what the run report shows
of each visit, given `--opdb-host`), and
`provenance.json` (the databases, the time read and the drp_qa that read). `serve --snapshot <dir>` serves it without a database. A run
judged after the fact is loaded into a qadb first (drp_qa's `bin.src/loadQadb.py`; a local copy will do).

```bash
python bin/qaDashboard.py snapshot --collection <collection> --out <dir> [--qadb-... as above]
python bin/qaDashboard.py serve --snapshot <dir>
```

The server listens on localhost:5006, with the run report at `/` and the metric views at `/dashboard`; add
`--address 0.0.0.0 --allow-origin <host>` to serve the network.

## Tests and lint

```bash
PYTHONPATH=python:<drp_qa>/python:<pfs_utils>/python uv run --no-project --python 3.12 \
    --with pytest,numpy,pandas,pyarrow,pyyaml,scipy,sqlalchemy,duckdb,panel python -m pytest
uv run --no-project --with ruff==0.14.13 ruff check . && uv run --no-project --with ruff==0.14.13 ruff format --check .
```

CI runs both, against the drp_qa named by `DRP_QA_REF` in `.github/workflows/tests.yml`.

## Versions

Releases are `MAJOR.MINOR.PATCH` git tags ([Semantic Versioning](https://semver.org)), with `version` in
`pyproject.toml` to match; user-visible changes go under `## [Unreleased]` in `CHANGELOG.md` until then.
