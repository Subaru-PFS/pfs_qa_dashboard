# pfs_qa_dashboard

A browser dashboard of the PFS image QA gate, live during a run from qadb (with the opdb's IIC sequences, visit
times and observers' notes), or offline from a snapshot of a qadb read. It reads what drp_qa's QA tasks stored and
judged; it computes no metric of its own. The one thing it works out is each value's standing against its limits, with
the gate's own `judge` and the thresholds qadb records for the image.

It is one page for now: the provenance strip (collection, thresholds, drp_qa version, time read) and the verdicts
so far, the images counted by status with the latest visit judged, in HST. The views are built up on this one at a
time; the prototype's Live, Run and Visit tabs are at tag `0.1.0`.

Built on Panel and DuckDB, and on drp_qa (`pfs.drp.qa`) for reading qadb back, thresholds included, and for the
gate's matching of an image to its thresholds entry.

## Running it

The dashboard needs drp_qa (`pfs.drp.qa`, with `pfs.drp.qa.qadb.reader`) and pfs_utils importable: set up in the
LSST/PFS stack, or their checkouts' `python/` on `PYTHONPATH`. Stack-free, [uv](https://docs.astral.sh/uv/) makes
`.venv` from `pyproject.toml` on its own Python, apart from the system's:

```bash
uv sync --python 3.12 --managed-python
PYTHONPATH=<drp_qa>/python:<pfs_utils>/python .venv/bin/python bin/qaDashboard.py {serve,snapshot} ...
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
# Read-only users; credentials in ~/.pgpass
python bin/qaDashboard.py serve --collection qaActor/reductions --first-visit <first visit of the run> \
    --qadb-host 127.0.0.1 --qadb-port 5436 --opdb-host 127.0.0.1 --opdb-port 5432 [--refresh 30]
```

**Offline, from a snapshot.** `snapshot --out <dir>` reads the collection once and writes what was read, one
parquet file per table (the thresholds cited included; with the opdb's times and notes, given `--opdb-host`), and
`provenance.json` (the databases, the time read and the drp_qa that read). `serve --snapshot <dir>` serves it without a database. A run
judged after the fact is loaded into a qadb first (drp_qa's `bin.src/loadQadb.py`; a local copy will do).

```bash
python bin/qaDashboard.py snapshot --collection <collection> --out <dir> [--qadb-... as above]
python bin/qaDashboard.py serve --snapshot <dir>
```

The server listens on localhost:5006; add `--address 0.0.0.0 --allow-origin <host>` to serve the network.

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
