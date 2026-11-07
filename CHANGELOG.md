# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html): a release is a `MAJOR.MINOR.PATCH` git tag, with
`version` in `pyproject.toml` to match. Before 1.0.0 a minor release may change the command line or the Python API.

## [Unreleased]

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
