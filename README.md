# pfs_qa_dashboard

A browser dashboard of the PFS image QA gate, live during a run from qadb (with the opdb's IIC sequences, visit
times and observers' notes), or offline from a snapshot of a qadb read. It reads what drp_qa's QA tasks stored and
judged; it computes no metric of its own.

It is one page for now: the provenance strip (collection, thresholds, drp_qa version, time read) and the verdicts
so far, the images counted by status with the latest visit judged, in HST. The views are built up on this one at a
time; the prototype's Live, Run and Visit tabs are at tag `0.1.0`.

Built on Panel and DuckDB, and on drp_qa (`pfs.drp.qa`) for reading qadb and for the thresholds the tasks judge
with.

## Running it

The dashboard needs drp_qa (`pfs.drp.qa`, with `pfs.drp.qa.qadb.reader`) and pfs_utils importable: set up in the
LSST/PFS stack, or their checkouts' `python/` on `PYTHONPATH`. For example, stack-free with uv:

```bash
PYTHONPATH=python:<drp_qa>/python:<pfs_utils>/python uv run --no-project --python 3.12 \
    --with numpy,pandas,pyarrow,pyyaml,scipy,sqlalchemy,duckdb,panel,'psycopg[binary]' \
    python bin/qaDashboard.py {serve,snapshot} ...
```

**Live, from qadb.** qaActor loads every visit it judges into qadb. The dashboard reads a collection
back (`pfs.drp.qa.qadb.reader.readQadb`) and every `--refresh` seconds reads only the images loaded since
(`qa_gate.decided_at`), so the page follows the run. One read serves every browser session. An image judged in more than one RUN of the collection shows the latest. Each
metric's limits are those of the thresholds files drp_qa ships, at the tasks' defaults (`serving.taskThresholds`,
the IIS layer included); the verdicts are the ones stored.

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
parquet file per table (with the opdb's times and notes, given `--opdb-host`), and `provenance.json` (the thresholds
file and drp_qa version, the databases and the time read). `serve --snapshot <dir>` serves it without a database. A run
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
