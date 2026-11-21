# AG sky transparency — nightly-page prototype (INSTRM-3130)

Prototype for surfacing the per-exposure AG sky **transparency** (and FWHM/seeing)
on the QA dashboard's nightly page. Tracking ticket: INSTRM-3130; the shared
calculation is INSTRM-3115.

## What it shows

Transparency per AG exposure across the night, as a time series:

- **y**: transparency from `pfs.utils.agMetrics` (median over the brighter guide
  stars; uncalibrated — a clear sky reads ~0.8, not 1).
- **colour**: guide stars used / the field's guide-star total, so a sparse-catalogue
  field does not read as low signal.
- **vertical markers**: design changes (the guide-star budget changes with design).

## Data source

The number is recomputed through `pfs.utils.agMetrics` from opdb
(`agc_data.estimated_magnitude`, matched via `agc_match` to `pfs_design_agc`
catalogue magnitude + Gaia colour) — the same calc ics_agActor logs per exposure.
`pfs_visit.pfs_design_id` gives the design per visit. See
`DRP/drp_qa/.../guiders/queries.py::readAgcData` for the canonical opdb AG join.

This is the "recompute from opdb" path; alternatively the dashboard can read the
qadb `transparency_agc_exposure` / `seeing_agc_exposure` tables directly.

## Status / validation

agMetrics reproduces the `pfs_obsproc_qa_tools` (`obsCondition.py`) qadb values
in the older isophotal-magnitude era, and differs in the newer agcc 2.4.0
fixed-aperture ("12px") era — under evaluation in INSTRM-3115 (whether the 12px
method is the better one, in which case agActor writes to the DB in place of the tool).

## Production integration

The dashboard builds hand-written **inline SVG** (no matplotlib) into
`{{PLACEHOLDER}}` slots; the night page's `scienceSections`/`_strip`
(`runReport.py`) distribution bars and the night-clock timeline JS
(`data/nightReport.html`) are the patterns to mirror. The 30 s live refresh
(`ReportCache.watch`) needs no change once the data is part of the cache rebuild.

`prototype.py` here is a **standalone matplotlib reference** for the data path and
the intended view only — not production code. It takes a visit range and writes a
PNG; it reads no committed data.
