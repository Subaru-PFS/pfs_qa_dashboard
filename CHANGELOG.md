# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html): a release is a `MAJOR.MINOR.PATCH` git tag, with
`version` in `pyproject.toml` to match. Before 1.0.0 a minor release may change the command line or the Python API.

## [Unreleased]

## [0.7.0] - 2026-11-20

### Added

- **The night page** (PIPE2D-2022), now the index (`/`): the current night at a glance — the latest visit, the
  night on a clock, counts for the night, the science offset and residual bars, the visits that do not pass, and
  the night's visits most-recent-first. The most recent night with a visit is shown.
- **Awaiting QA**: with the opdb, a visit taken after the latest one judged, with no result yet, is `AWAITING` (the
  night page lists it, on the clock, in the latest-visit tile and a panel); the active night follows the latest
  exposure, so the page moves to tonight on the first observation, before any reduction. An arc shows its lamp
  before QA runs, from the opdb's sequence name.
- **The per-visit page** (`/visit/<n>`, PIPE2D-2023): one visit over all its detectors — the header, each
  detector's verdict and the gate's reason, a metric × camera grid coloured by verdict, and the sky-line residual
  by species. A not-yet-reduced visit reads "No QA result". Built from the server's cached read, so it needs no
  database of its own. The run report and both clocks link each visit to its page.

### Changed

- **`serve` serves three pages** from one read: `/` (night), `/run` (run report) and `/visit/<n>`, sharing one
  `/status`. It now also reads the opdb for the visits taken each refresh, not only when qadb changes, so a new
  exposure makes the pages again and the browser reloads during a run that is not being reduced.
- The page's stylesheet (`data/theme.css`) and the science offset/residual bars (`scienceSections`) are shared by
  the run report and the night page; the run report's output is unchanged.

## [0.6.0] - 2026-11-20

### Added

- **The run report** — one stand-alone page giving every visit of a collection a verdict, the worst among its
  detectors: every visit by night, on a clock (HST; the night, 18:00 to 07:00, or the whole day) or in the order
  taken, for the kinds of frame, arm and spectrograph ticked; counts by kind; the
  science frames' measurements per detector, each night, the visits that do not pass and a filterable list of
  every visit. It judges nothing: each image's verdict, reason and kind of frame are its `qa_gate` row, read with
  drp_qa's `readGate`, for any collection in qadb (a redone run's too). The page lists the gate's checks and
  limits, from drp_qa's `CHECKS`, and draws the science checks' limits beside the offsets and sky-line residuals
  as measured. Needs drp_qa 0.5.0 or later and a qadb at spt_qa_database 0.6.0, which dropped the tasks' own
  verdict columns.
- **Visits with no QA result** — with the opdb, a visit that exposed the spectrograph as an object, arc or flat
  frame before the latest visit judged, and has no result in qadb, is shown as `NO RESULT`: an outline on the
  calendar, a count beside each tally, kind and night, and a section listing each with how its sequence ended.
  Visits after the latest one judged are left out, as their results may be on their way.
- The page embeds its two typefaces (Inter and Space Grotesk, SIL Open Font License), about 95 kB, so it looks
  the same wherever it is opened and fetches nothing.
- `report --collection <collection> | --snapshot <dir> --out <dir>` writes the page as `index.html`, with
  `visits.csv` and `images.csv`.
- `snapshot` keeps the gate's verdicts (`qaGate.parquet`); `serve` and `report` refuse a snapshot without them.
- `snapshot`, given the opdb, keeps every visit taken in its range, with its time, exposure, cameras, sequence
  and how that ended (`visitInfo`), for the report.

### Changed

- **`serve` serves the run report**, at `/`, from a plain Tornado server. It reads qadb every `--refresh`
  seconds (30 by default) on a thread of its own and makes the page again when there are new results, so a
  browser is answered at once with the last page made: a database that fails or hangs no longer holds the
  page up, or the server.
- **The page served follows the server.** Every few seconds it asks for `/status`, reloads itself when a newer
  page has been made, keeping the filters, buttons and place as they were left, and says when the results were
  last read: in red, with the error, when a read fails; in amber, "Still reading", when none has worked for three
  intervals (90 s at least) and one is under way; in red when none is. `serve` logs a failed read, the wait, and a
  slow read's duration.
  A page written by `report`, or served from a snapshot, is as before and fetches nothing.
- A database connection that is not answered is given up on after 10 s (`PGCONNECT_TIMEOUT`, when not set).

### Fixed

- A read taken while a load was being written could bring an image's line residuals and not its image-quality
  row; the image then lost that row (its lamp and illumination) until the server restarted. And a load stamped
  before one already read, but written after it (two loaders at once), was never read. Each table now replaces
  only the images it brought, a load is new by table, image and time, and each refresh reads from five minutes
  before the latest load read.
- A read from qadb that brought only images with line residuals and no image-quality row was not taken as new,
  so those images showed only once a later image was loaded. `snapshot` and `report` likewise left such
  visits out of the range they read from the opdb.

### Removed

- **The metric views** (the provenance strip, the verdicts so far and the comparison of a night against
  reference visits), with `/dashboard`, `--reference` and `--allow-origin`, and the modules only they used:
  `app`, `figures`, `comparison`, `store`, `serving` and `schema`. Panel, Bokeh and DuckDB are no longer
  needed; Tornado is. `live.QadbMetricsStore` is now `live.QadbStore`, holding the tables qadb returned and
  each visit's time; it reads no opdb and has no `minInterval`. `QadbStore.fromTables` replaces
  `fromSnapshot`.
- `snapshot` no longer writes the opdb's sequences and notes (`opdbVisitTimes`, `notes`) or the reference
  visits' metrics. A snapshot that has them is still read.

## [0.5.0] - 2026-11-10

### Added

- **A night against the references** (PIPE2D-1923) — a comparison view: a strip of the chosen night's visits (HST
  evening date), each coloured by the worst of its images' verdicts and naming the camera, task and reason that
  decided it, and under it one grid per reference, a cell per metric, frame type and camera. A cell shows the
  difference of the night's median from the reference's (relative where the metric declares a tolerance, in the
  metric's own units where it does not) with drp_qa's `PASS`/`WARN`/`FAIL`, and how many of the night's visits are
  beyond the `FAIL` tolerance on their own, so one bad visit shows though the median passes. A cell whose medians
  rest on three images or fewer, on either side, is an outline with the smaller count, not a filled square. Cells
  without a verdict are hidden until *Hide unjudged cells* is cleared, with a count under each grid. Choosing a cell lists
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
