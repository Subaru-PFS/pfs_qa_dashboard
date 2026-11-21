"""AG sky transparency per exposure, recomputed via pfs.utils.agMetrics from opdb.

Standalone matplotlib reference for the nightly-page view (INSTRM-3130). Not
production code: the dashboard builds inline SVG (see this directory's README).
Reads no committed data; takes a visit range and writes a PNG.

  python prototype.py <first_visit> <last_visit> <out.png> \
      [--host H] [--port P] [--user U]

Needs opdb reachable (e.g. a local copy or a pfsa-db forward) and pfs_utils on
the path. Uses the read-only user by default.
"""
import argparse

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from pfs.utils import agMetrics
from pfs.utils.database.opdb import OpDB

# One row per matched guide-star spot of each AG exposure in the visit range.
_SPOTS_SQL = """
SELECT agc_exposure.agc_exposure_id,
       agc_exposure.pfs_visit_id,
       agc_exposure.taken_at,
       pfs_visit.pfs_design_id,
       agc_data.estimated_magnitude,
       pfs_design_agc.guide_star_magnitude,
       pfs_design_agc.guide_star_color
FROM agc_exposure
JOIN agc_data ON agc_data.agc_exposure_id = agc_exposure.agc_exposure_id
JOIN agc_match ON agc_match.agc_exposure_id = agc_data.agc_exposure_id
   AND agc_match.agc_camera_id = agc_data.agc_camera_id
   AND agc_match.spot_id = agc_data.spot_id
LEFT JOIN pfs_visit ON pfs_visit.pfs_visit_id = agc_exposure.pfs_visit_id
LEFT JOIN pfs_design_agc ON pfs_design_agc.pfs_design_id = pfs_visit.pfs_design_id
   AND pfs_design_agc.guide_star_id = agc_match.guide_star_id
WHERE agc_exposure.pfs_visit_id BETWEEN :lo AND :hi
"""
# The guide-star total per design, for the colour normalization.
_DESIGN_SQL = "SELECT pfs_design_id, count(*) AS n_total FROM pfs_design_agc GROUP BY pfs_design_id"


def perExposure(spots):
    """Transparency (uncalibrated median) and star count per AG exposure, via agMetrics."""
    def one(g):
        t = agMetrics.magnitudesToTransparency(
            g.estimated_magnitude.to_numpy(float),
            g.guide_star_magnitude.to_numpy(float),
            g.guide_star_color.to_numpy(float),
        )
        r = agMetrics.summarizeTransparency(t, g.estimated_magnitude.to_numpy(float))
        return pd.Series({"transparency": r.transparency, "n_used": r.nStars})

    keys = ["agc_exposure_id", "pfs_visit_id", "taken_at", "pfs_design_id"]
    return spots.groupby(keys, dropna=False).apply(one, include_groups=False).reset_index()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("lo", type=int)
    p.add_argument("hi", type=int)
    p.add_argument("out")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--user", default="public_user")
    p.add_argument("--dbname", default="opdb")
    a = p.parse_args()

    db = OpDB(host=a.host, port=a.port, user=a.user, dbname=a.dbname)
    spots = db.query_dataframe(_SPOTS_SQL, params={"lo": a.lo, "hi": a.hi})
    designs = db.query_dataframe(_DESIGN_SQL).set_index("pfs_design_id")["n_total"]

    df = perExposure(spots).sort_values("taken_at").reset_index(drop=True)
    df["taken_at"] = pd.to_datetime(df["taken_at"])  # HST
    df["frac"] = df["n_used"] / df["pfs_design_id"].map(designs)

    did = df["pfs_design_id"].fillna(-1)
    changes = df.loc[did.ne(did.shift()), ["taken_at", "pfs_design_id"]]

    fig, ax = plt.subplots(figsize=(13, 5.5))
    sc = ax.scatter(df.taken_at, df.transparency, c=df.frac, s=14, cmap="viridis", vmin=0, alpha=0.85)
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("time (HST)")
    ax.set_ylabel("AG transparency (uncalibrated; clear ≈ 0.8)")
    ax.set_title(f"AG sky transparency per exposure — visits {a.lo}–{a.hi}")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.grid(True, alpha=0.3)
    fig.colorbar(sc, ax=ax).set_label("guide stars used / field total")
    for t in changes.taken_at:
        ax.axvline(t, color="0.6", ls=":", lw=0.9, zorder=0)
    fig.tight_layout()
    fig.savefig(a.out, dpi=130)
    print(f"{len(df)} exposures, {df.pfs_design_id.nunique()} designs -> {a.out}")


if __name__ == "__main__":
    main()
