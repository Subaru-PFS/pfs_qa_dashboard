# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html): a release is a `MAJOR.MINOR.PATCH` git tag (the
package version is derived from it by setuptools-scm). Before 1.0.0 a minor release may change the command line
or the Python API.

## [Unreleased]

### Added

- **`REDUCE FAILED` verdict** (PIPE2D-2036): a visit whose reduceExposure crashed — written to `qa_gate` by
  drp_qa's `markReduceFailed` — now reads as a distinct red `REDUCE FAILED` chip across the dashboard (the
  run report's "Every visit" and "Visits that do not pass" tables and verdict filter, the night page's
  latest-visit tile and feed, and the per-visit page's verdict and detector chips), instead of sitting in
  AWAITING / NO RESULT as if its QA were still on the way. Ranked the worst verdict (reduceExposure left no
  data to judge); in the aggregate tallies and the "By night" / "Visits by kind" counts it folds into FAIL,
  so every total still reconciles while the per-visit rows carry the precise cause. The per-visit page adds a
  red "Reduction failed" banner (with the returnCode) in place of the empty metric grid and plot sections.

## [0.17.0] - 2026-11-23

### Changed

- **Run-report link always on the night page**: the night page's navigation now always carries a "Run
  report" link, ahead of the night-to-night controls. It previously appeared only as part of the
  night-to-night nav, which is hidden on a single-night run — so early in a run the night page had no link
  to `/run` except in the footer.
- **"Every visit" table sorted newest first**: the run report's "Every visit" table now lists the most
  recent visit first. The underlying time-map data keeps its taken order, so "Every visit at a glance" is
  unchanged.
- **Night page Conditions tile no longer falls back to the Science tile**: when a night has no AG seeing or
  transparency, the Conditions tile now shows its own no-data state in place, rather than reverting to the
  science verdict counts. (Missing conditions data is a separate, upstream matter — the writer must be
  populating qadb's `seeing_agc_exposure` / `transparency_agc_exposure`.)

## [0.16.0] - 2026-11-23

### Added

- **Every visit is listed, darks and biases included**: the run report's "Every visit" table now lists
  every SpS exposure in the run, not only the kinds with a QA method. A frame with no QA method (a dark,
  bias, master or test exposure) is shown by its exposure type with a muted "NO QA" verdict — distinct from
  "NO RESULT" (a judged kind passed over). These are tallied in a new "No QA" lead line, a "No QA" column
  and per-type rows in "Visits by kind", and a "No QA" column in "By night"; on the time map they are a
  "Darks & biases" toggle (off by default) and a faint square, and the verdict filter gains a "NO QA"
  option. The night page lists them too — in its "Latest visits" feed, the Tonight tile, the lead, and the
  clock toggle — kept out of the judged counts.
- **Design name surfaced across the dashboard**: the opdb's full `pfs_design.design_name` is now read and
  shown — a "Design" column in the run report's "Every visit" table (with a free-text filter: type `shaka`,
  `SSP`, `blackDots`, a field name, …) and the night page's "Latest visits" table, and a "Design: …" line on
  each visit page. The query previously nulled every design name except the one-fiber-group (shaka) ones;
  the dashboard is internal, so the full name is surfaced now (it still must be kept out of public records —
  commits, tickets, test fixtures).
- **Night dates on the run report's time map link to their night page**: in "Every visit at a glance" on
  `/run`, each y-axis night date is now a link — the latest night to `/`, the rest to `/night/<date>` — so
  you can jump from a night on the clock straight to that night's page. Shown only when more than one night
  is on the clock, so the single-night night page keeps a plain label.

### Changed

- **Detector residual maps share one frame**: on the visit page (`/visit/<n>`, with `serve --butler`), the
  four 2D x–y residual face maps are now held to the full detector bounds (`detectorMap.getBBox()`) instead
  of auto-scaling to each visit's line extent, so a detector's maps read the same from visit to visit and
  across detectors. A detector's bounds are read once (cached per camera), so the NIR detector keeps its
  own size; the 1D trend panels are unchanged.
- **Night-page title carries the night's date**: the night page's heading and browser-tab title now read
  `<title> · <YYYY-MM-DD>` (e.g. "Visit verdicts · 2026-11-04") rather than just the run title, so one
  night's page reads apart from another at a glance and in a tab. The run report's title is unchanged.
- **Visit-page nav links to the run report and the visit's own night**: the top/bottom navigation on each
  visit page (`/visit/<n>`) now links to the run report (`/run`), the current night (`/`), and — for a
  visit taken on a past night — that night's page (`/night/<date>`). It previously linked only to the
  current night, and the run report only from the body text.
- **Collapsible run-report sections**: every section of the run report (`/run`) is now a
  `<details>` with a click-to-toggle heading. All start collapsed except "Every visit at a glance" and
  "Every visit", and "Every visit" moves to the top (just below the at-a-glance clock) so the full,
  filterable visit table is the first thing in reach; the rest expand on demand.
- **Scrollbars on the run report's long tables**: the "Visits that do not pass" and "Visits with no QA
  result" tables, which can each run to hundreds of rows, now use the shared `.tall` wrapper — capped at
  640 px with a vertical scrollbar and a sticky header — as the "Every visit" table already does, so they
  no longer push the rest of the page far down.

### Fixed

- **`FutureWarning` from the visit-times concat** in `QadbStore`: the first read concatenated an empty
  `_times` frame, which pandas warns it will stop excluding when resolving concat dtypes. The empty part
  is now dropped before the concat, which also keeps `time_exp_start` as `datetime64` on pandas 3 (the old
  code let the empty object column make the result `object`).

## [0.15.0] - 2026-11-23

### Added

- **The night page's time map is the run report's** (PIPE2D-2032): "The night on the clock" is now the same
  component as the run report's "Every visit at a glance" — the By time / In order taken toggle, the
  night-only (18:00–07:00) switch, and the arm and spectrograph selectors — scoped to the single night. The
  clock's markup and script live in one shared partial (`data/clockControls.html`, `data/clockScript.js`) that
  both pages render, so the two cannot drift; the night page's `AWAITING` state is carried into it.
- **Past-night pages** (PIPE2D-2032): a `/night/<date>` page for any night of the run, built on demand from
  the cached tables with no new database read. The night page gains earlier/later navigation and a dropdown of
  the run's nights; `/` stays the latest night. The AG conditions tile, which needs a read of its own, is shown
  only on the live `/` (a past night falls back to the science verdict counts).
- **Sequence id on the visit tables** (PIPE2D-2032): the run report's "Every visit" table and the night page's
  "Latest visits" table show each visit's IIC sequence id (`visit_set.iic_sequence_id`, shared by every visit
  taken under the one command), read by `readVisitInfo` in its one opdb query; blank for a visit outside any
  sequence.

### Changed

- The night page drops its separate "Awaiting QA" section — the "Latest visits" table already marks those
  visits, so it was redundant. Section explanation paragraphs now span the full content width (were capped near
  half), which trims vertical space on both the night page and the run report.

### Fixed

- The night page's "Latest visits" table labelled a NO RESULT visit "awaiting QA" in its Detectors column, the
  same as a genuinely pending AWAITING visit. A NO RESULT visit was passed over (its QA never ran, e.g.
  reduceExposure errored) so none is coming; it now reads "–", and only an AWAITING visit reads "awaiting QA".
- The night clock's tooltip read the wrong column (0.13.0): the night page emitted its per-visit rows with
  `detectorsStatus` and `why` swapped against the indices the clock script read, so a no-result or passed-over
  visit's tooltip showed the detectors string in place of the reason. Unifying on the run report's column order
  fixes it.

## [0.14.0] - 2026-11-22

### Added

- **Extraction-QA plots on the visit page** (PIPE2D-2031): the per-visit page now shows each detector's
  extraction-QA summary — `pfs.drp.qa.plotting.extraction.plotExtractionStats` (chiMed / chiStd / chiAtPeak /
  resFrac per GOOD fiber against fiberId, coloured by mean extracted flux) — drawn on demand from the stored
  `extQaImage_pickle`, in detector order, each beside a "detail (PDF)" link to the task's full `extQaImage`
  image pages (the 2-D calexp, residual and chi images, which cannot be redrawn from the pickle). Extraction QA
  is not loaded into qadb and carries no gate verdict, so these are diagnostic only (no verdict chip). As with
  the residual plots the data lives in the Butler, so they are served only with `serve --butler <repo>`: the
  summary PNG is rendered when its `<img>` is fetched (`/visit/<n>/extraction/<camera>.png`), off the event
  loop and cached, and the detail PDF is streamed from the datastore (`/visit/<n>/extraction/<camera>.pdf`).
  Needs a `drp_qa` that provides `plotExtractionStats`.

## [0.13.0] - 2026-11-22

### Added

- **Detector-map residual plots on the visit page** (PIPE2D-2030): the per-visit page now shows
  `pfs.drp.qa.plotting.dmResiduals.plotDetectorResiduals` for each detector — the drift (vs the calibration)
  and scatter (vs the visit's own adjusted detectorMap) line residuals — drawn on demand from the stored
  per-line `dmLineResidualData`, in detector order, with the gate's warn/fail limits as dashed bands. The
  Detectors table links each detector to its plot, each plot shows a loading spinner until it arrives, and a
  click enlarges a plot full-screen. The plot's per-line data is in the Butler, not qadb, so it is served only
  in the live mode and only with `serve --butler <repo>` (needs the LSST stack and `--collection`): each plot
  is rendered when its `<img>` is fetched (`/visit/<n>/residual/<camera>.png`), off the event loop and cached,
  so the page stays small and nothing is rendered until it is viewed. Without `--butler`, the page shows no
  plot, as before. Rendering adds `matplotlib` and `seaborn` (drp_qa's plotting) as dependencies.

## [0.12.0] - 2026-11-21

### Added

- **Shaka exposures labelled** (PIPE2D-1955) — a visit taken on a one-fiber-group (shaka) design
  (`cobraHome-MOD<k>_group<n>`, which lights one group of every `k`-th cobra) is tagged `· shaka MOD4 group2`
  on its label, so it reads apart from a fully lit arc or trace on the night page, the run report and the
  visit page. `readVisitInfo` reads the design name (only for the one-fiber-group designs, so proprietary
  field names stay in the opdb) and derives the label with drp_qa's `comparison.classify.shakaOf`.

## [0.11.0] - 2026-11-21

### Added

- **Daily calibration drift** (PIPE2D-1986): a new section on the night page and the run report showing each
  daily calibration's offset from `detectorMap_calib` over the whole run — a quartz's trace offset in x and the
  nightly Neon arc's line offset in y — per detector, with the run's own 5th-to-95th-percentile band, the nightly
  trend, and the night-date axis. The most recent night's points are marked, so today's set can be read against
  the rest of the run. A point is coloured when its own offset reaches the `calibOffset` limit (WARN 1.0 px, FAIL
  2.0 px), independent of the image's overall gate verdict. The charts are stacked into **columns on a shared time
  axis** and a toggle groups them **by spectrograph** (a shift common to a whole unit, or one detector drifting
  alone, reads down the column) or **by arm** (the same detector across the four spectrographs). Each track is
  labelled with its units (both offsets are in pixels). Clicking a chart opens its **whole group** — a
  spectrograph's arms, or an arm's spectrographs — enlarged and time-aligned in the shared modal overlay
  (`.modal`/`.sheet`, shared with the Conditions modal), so a shift common to the group reads at a glance.
  All drift data is read from qadb (`dm_line_residual_qa_metric`, written by the actor's per-visit load); the
  page judges nothing.
- **Calibration-set go/no-go** (PIPE2D-1986): the run report adds a section judging each of a run's
  set-building events per detector — a go/no-go grid (the set's exposures down, the 16 detectors across: go,
  no-go, exposed-but-not-measured, not-exposed) with the leave-out and lamp-trend notes. It reuses
  `drpQa.py calib inputs`' stack-free judging (`pfs.drp.qa.comparison.calibInputs`), fed from the opdb visit
  listing and qadb (the measured numbers and the collection's normal values); the gate rule needs the normal
  values and the reserved-RMS rule needs `reductionStats`, so a sparse run shows mostly go and missing. The
  section is omitted for a snapshot or when there is no opdb.
- `serve --debug`: restart the server when the dashboard's code or its `data/` assets (CSS, HTML, fonts)
  change (Tornado's autoreload). Unlike `--refresh`, which reloads the data, this reloads the code; a
  restart re-reads the databases, so it pairs with `serve --snapshot` for the dev loop.

## [0.10.0] - 2026-11-21

### Added

- **AG seeing** on the night page's Conditions tile: the tile now leads with the night's median seeing (FWHM,
  arcsec) and keeps transparency as a secondary line, and the modal shows a seeing time series alongside the
  transparency one. Source is qadb `seeing_agc_exposure`, read for the night's visits like transparency; a
  failed read drops only that metric. The `agTransparency` module is renamed `agConditions`, now covering both
  (INSTRM-3131).

### Changed

- The package version is now derived from the latest git tag by setuptools-scm, instead of a hard-coded
  `version` in `pyproject.toml`; a release is a tag alone, with no file to edit.

## [0.9.0] - 2026-11-21

### Added

- **AG sky transparency** on the night page: a **Conditions** tile (in place of the Science verdict tile) with
  the night's median sky transparency and a sparkline, which opens a modal holding the full per-night time
  series — a median line with a 25th-to-75th-percentile band over the same 18:00–07:00 hours as the verdict
  clock. The numbers are qadb's `transparency_agc_exposure` (the guide cameras' per-exposure transparency, a
  median over the brighter guide stars; uncalibrated, a clear sky reads about 0.8), read for the night's visits
  when serving a live collection. A read that fails is logged and the tile falls back to the Science counts, so
  it never blanks the page; the tile and modal are absent for a snapshot or offline report (INSTRM-3130).

## [0.8.0] - 2026-11-21

### Added

- **Previous/next visit navigation** on the per-visit page: a nav row with the previous visit on the left, the
  night page in the centre and the next visit on the right. The neighbours are `visit ± 1` with no check that they
  exist, so a gap in the numbering lands on a no-result page; below visit 1 there is no previous link.
- **`report --name`** names the stand-alone page `<name>.html` (default `index`), so a redone run's report can be
  written as e.g. `run31.html` and served from a directory of per-run pages. `drp_qa`'s `drpQa.py redo` prints the
  matching command (PIPE2D-2016).

### Changed

- The per-visit page's heading now carries the visit's identifying facts (time, exposure, type, arms) as the
  title, rather than a bare "Visit N" with those facts in a byline; the header uses the full content width.

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
