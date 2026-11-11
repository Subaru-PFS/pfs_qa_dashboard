# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html): a release is a `MAJOR.MINOR.PATCH` git tag, with
`version` in `pyproject.toml` to match. Before 1.0.0 a minor release may change the command line or the Python API.

## [Unreleased]

## [0.5.0] - 2026-11-10

### Added

- **A night against the references** (PIPE2D-1923) — a comparison view: a strip of the chosen night's visits (HST
  evening date), each coloured by the worst of its images' verdicts and naming the camera, task and reason that
  decided it, and under it one grid per reference, a cell per metric, frame type and camera. A cell shows the
  difference of the night's median from the reference's (relative where the metric declares a tolerance, in the
  metric's own units where it does not) with drp_qa's `PASS`/`WARN`/`FAIL`, and how many of the night's visits are
  beyond the `FAIL` tolerance on their own, so one bad visit shows though the median passes. Choosing a cell lists
  its populations and each visit's value, opening on the one the cell shows. The grid's rows keep one order (by
  metric, then frame type) whatever their status, and visits without a time have a row of their own in the strip.
  References are named at start-up, `--reference NAME=[COLLECTION:]FIRST-LAST` (repeatable, each with its own
  name), read from qadb once and kept in a snapshot; nothing is stored in qadb. Needs drp_qa with `pfs.drp.qa.qadb.compareRuns.comparisonTables` and
  `pfs.drp.qa.metrics.runComparison.toleranceStatus` (0.3.0).

### Changed

- `--qadb-user` defaults to `pfs`, the only qadb user.

## [0.4.0] - 2026-11-10

### Changed

- **Limits from every thresholds layer** (PIPE2D-1991) — each value is judged with every layer qadb records its
  image's task judged with, in the order judged (`readQadb`'s `thresholdsIds`: the files, then the config's
  threshold fields as `<task>.config`), so a metric the first layer leaves out, the `medianDeltaX/Y` drift rows of
  `dmLineResiduals` above all, now shows the limits that judged it. `thresholdsId` is no longer read: a snapshot
  with it alone shows values without limits. The provenance strip names a config layer as such. Needs drp_qa with
  `pfs.drp.qa.qadb.reader.layeredThresholds` (0.2.0) and a qadb at spt_qa_database 0.5.0.

## [0.3.0] - 2026-11-06

### Changed

- **Limits come from qadb** (PIPE2D-1988) — each value's WARN/FAIL limits are the thresholds qadb records its image
  was judged with (the file `qa_gate` cites for the task, read back with the images by drp_qa's
  `pfs.drp.qa.qadb.reader`), found for the image by the gate's `judge`, which also gives each IQ value its own
  PASS/WARN/FAIL (the task stores none per metric); nothing comes from drp_qa's shipped files or task defaults any
  more, so a config override or a file changed since cannot make the limits disagree with the stored verdict. A metric a later layer judged (the IIS file, the config's fields) shows no limits. The provenance
  strip names the files cited, updating as new ones appear; snapshots hold them. Needs drp_qa 0.1.4 or later
  (`readQadb`'s `thresholds` tables).
- `serving.buildMetricsTable` takes the reader's `thresholds` table in place of threshold layers;
  `qadbSource.metricsTableFromQadb` and `live.QadbMetricsStore` take no layers.

### Removed

- `serving.taskThresholds`, `configDefaults`, `thresholdsProvenance` and `firstThresholdsFile`, and the
  `thresholds` and `thresholdLayers` keys of a snapshot's `provenance.json`.

## [0.2.0] - 2026-11-06

### Changed

- **One page** — the app is the provenance strip and the verdicts so far: the images counted by status, worst
  first, the visits' range and HST span, and the latest visit judged with its status and HST time. The views are
  built up on this one at a time, from what is wanted.

### Removed

- **The prototype's Live, Run and Visit tabs**, `serve --detail-dir` and `--start-visit`, and the HoloViews and
  Datashader dependencies. They stay at tag `0.1.0`.

## [0.1.0] - 2026-11-06

### Added

- **The QA dashboard** (PIPE2D-1924), moved from drp_qa (`pfs.drp.qa.dashboard` at drp_qa `46d7c8a`) as
  `pfs.qa.dashboard` and `bin/qaDashboard.py`. A Panel app with Live (the latest visit and its sequence), Run
  (verdict heat map and trends over a visit or HST range, each value with its thresholds) and Visit (a check ×
  camera verdict grid, each value's distance past FAIL, the pipeline's PDFs, line residuals rasterised with
  Datashader) tabs, over one long-format metrics table queried with DuckDB.
- **Live from qadb** (`qaDashboard.py serve --collection`): reads a collection through drp_qa's `pfs.drp.qa.qadb.reader` and every
  `--refresh` seconds only the images loaded since, one read for every session; the Live tab follows the latest
  visit.
- **The opdb** (`--opdb-host`): each visit's IIC sequence, SpS start time and observers' notes, the notes re-read for
  recent visits.
- **Snapshots** (`qaDashboard.py snapshot --out DIR`, then `serve --snapshot DIR`): one read saved as parquet with
  `provenance.json`, served without a database.
