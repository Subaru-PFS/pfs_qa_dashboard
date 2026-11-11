"""The QA dashboard: a Panel app over the metrics table.

One page: the provenance strip (collection, thresholds, drp_qa version, time read), the
verdicts so far (the images counted by status with the latest visit judged), the visits
by night with each one's rolled-up status, and a night's visits compared with the
reference visits the dashboard was started with: a grid per reference (metric and frame
type by camera) and, for the cell chosen, its values visit by visit. The views are built
up on this one at a time; the prototype's Live, Run and Visit tabs are at tag 0.1.0.

Everything shown comes from the metrics table (`live.QadbMetricsStore`, read from qadb
or a snapshot of it); the app computes no metric and holds no threshold of its own: the
limits are the thresholds qadb records each image was judged with, found for each value by
the gate's `judge`, and the provenance strip names those files. The rollup and the
comparison are drp_qa's (`pfs.drp.qa.metrics.visitRollup`, `pfs.drp.qa.metrics.runComparison`),
made when the page asks, with the tolerances the metrics declare.
"""

import html

import pandas as pd
import panel as pn
import param

from pfs.drp.qa.metrics.gate import isConfigLayer
from pfs.qa.dashboard.comparison import (
    ALL_VISITS,
    CELL_KEYS,
    gridCells,
    nightOptions,
    nightVisits,
    populationRows,
    visitDeviations,
    worstFirst,
)
from pfs.qa.dashboard.figures import (
    STATUS_COLORS,
    STATUS_MARKS,
    cellDetail,
    comparisonGrid,
    relativeText,
    visitStrip,
)
from pfs.qa.dashboard.schema import IMAGE_STATUS_ORDER
from pfs.qa.dashboard.store import MetricsStore, visitTimeline

__all__ = ["Dashboard", "createApp", "thresholdsLines", "verdictCounts"]

_STATUS_RANK = {status: rank for rank, status in enumerate(IMAGE_STATUS_ORDER)}


def verdictCounts(images: pd.DataFrame) -> pd.Series:
    """Return how many images have each status, worst first and every status present, zero included."""
    counts = images["status"].value_counts() if len(images) else {}
    return pd.Series(
        {status: int(counts.get(status, 0)) for status in reversed(IMAGE_STATUS_ORDER)}, dtype=int
    )


def _esc(value) -> str:
    return html.escape(str(value))


def thresholdsLines(thresholdsFiles: pd.DataFrame) -> list[str]:
    """Return one line per thresholds layer the images cite, for the provenance strip.

    ``iqQaThresholds-run25.yaml, derived 2026-10-19 from runs 25, 29 with drp_stella
    w.2026.41-6-ge56938d2, obs_pfs w.2026.40, datamodel w.2026.40 (124 entries)``, and
    ``dmLineResiduals.config, the task config's threshold fields (4 entries)`` for a config
    layer (`pfs.drp.qa.metrics.gate.isConfigLayer`), from
    `pfs.drp.qa.qadb.reader.readQadb`'s ``thresholdsFiles``; nothing for none.
    """
    if thresholdsFiles is None or thresholdsFiles.empty:
        return []
    lines = []
    for row in thresholdsFiles.sort_values("thresholdsId").to_dict("records"):
        if isConfigLayer(str(row.get("name", ""))):
            line = f"{row['name']}, the task config's threshold fields"
            line += f" ({int(row['nEntries'])} entries)" if pd.notna(row.get("nEntries")) else ""
            lines.append(line)
            continue
        derived = row.get("derivedOn")
        runs = row.get("referenceRuns")
        pipeline = ", ".join(
            f"{product} {row[column]}"
            for product, column in (
                ("drp_stella", "drpStellaVersion"),
                ("obs_pfs", "obsPfsVersion"),
                ("datamodel", "datamodelVersion"),
            )
            if pd.notna(row.get(column))
        )
        line = str(row.get("name", "?"))
        line += f", derived {pd.Timestamp(derived):%Y-%m-%d}" if pd.notna(derived) else ", derived ?"
        line += f" from runs {str(runs).replace(',', ', ')}" if pd.notna(runs) else ""
        line += f" with {pipeline}" if pipeline else ""
        line += f" ({int(row['nEntries'])} entries)" if pd.notna(row.get("nEntries")) else ""
        lines.append(line)
    return lines


def _tile(label: str, value: str, color: str | None = None, mark: str = "") -> str:
    """Return a stat tile: the value in text ink, the status (if any) as a coloured mark and a label."""
    edge = f"border-top:5px solid {color};" if color else "border-top:5px solid transparent;"
    badge = (
        f'<span style="display:inline-block;width:1.4em;height:1.4em;line-height:1.4em;border-radius:50%;'
        f'background:{color};color:white;text-align:center;font-size:0.8em;margin-right:0.4em">{mark}</span>'
        if color
        else ""
    )
    return (
        f'<div style="min-width:120px;padding:8px 14px;{edge}background:rgba(127,127,127,0.07)">'
        f'<div style="font-size:1.9em;font-weight:600;line-height:1.2">{_esc(value)}</div>'
        f'<div style="font-size:0.9em;white-space:nowrap">{badge}{_esc(label)}</div></div>'
    )


def _detail(text: str) -> str:
    return f'<div style="font-size:0.95em;margin-top:8px">{text}</div>'


class Dashboard(param.Parameterized):
    """The app's state: the data's version, bumped when a refresh brings new loads, and what is chosen."""

    dataVersion = param.Integer(default=0)

    #: The night compared with the references: the HST date of its evening, or every visit.
    night = param.Selector(default=ALL_VISITS, objects=[ALL_VISITS], label="Night (HST evening date)")

    #: The grid cell shown in detail (`comparison.CELL_KEYS`); the worst when None.
    cell = param.Parameter(default=None)

    #: The population of that cell shown (`comparison.populationLabel`); its worst when None.
    population = param.Parameter(default=None)

    def __init__(self, store: MetricsStore, refreshSeconds: float = 30.0, **params):
        self.store = store
        self.refreshSeconds = refreshSeconds
        self._reload()
        super().__init__(**params)
        self._takeNights()

    def _reload(self) -> None:
        """Read the visits and images from the store."""
        self._seenVersion = self.store.version
        self.visits = self.store.visits()
        self.images = self.store.images()
        self.timeline = visitTimeline(self.visits)
        self._visitTimes = self.timeline.set_index("visit")["time"]
        self.rollup = self.store.rollup()
        self._compared = None

    def _takeNights(self) -> None:
        """Offer the nights the visits span; keep the one chosen, every visit included.

        The latest night is taken when there was none to choose from before (at the start,
        and when the first times arrive).
        """
        options = nightOptions(self.timeline)
        hadNights = len(self.param.night.objects) > 1
        chosen = self.night if hadNights and self.night in options else options[0]
        self.param.night.objects = options
        self.night = chosen

    def refresh(self) -> None:
        """Take what the store has read since the last call."""
        self.store.refresh()
        if self.store.version == self._seenVersion:
            return
        self._reload()
        self._takeNights()
        self.dataVersion += 1

    # ---- the comparison ----------------------------------------------------------------------------

    def compared(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Return the chosen night's comparison, its visits' own deviations, and the grid's cells."""
        key = (self._seenVersion, self.night)
        if self._compared is None or self._compared[0] != key:
            visits = nightVisits(self.timeline, self.night)
            comparison = self.store.comparison(visits)
            deviations = visitDeviations(self.store.metricValues(visits), comparison)
            self._compared = (key, (comparison, deviations, gridCells(comparison, deviations)))
        return self._compared[1]

    def selectCell(self, cell: tuple | None) -> None:
        """Show a grid cell (`comparison.CELL_KEYS`) in detail, at its worst population."""
        with param.parameterized.batch_call_watchers(self):
            self.population = None
            self.cell = None if cell is None else tuple(cell)

    @param.depends("night", watch=True)
    def _forgetPopulation(self) -> None:
        """Another night's cell opens on its own worst population."""
        self.population = None

    def _choosePopulation(self, event) -> None:
        self.population = event.new

    def shownCell(self) -> tuple | None:
        """Return the cell in detail: the one chosen if the night has it, else the night's worst."""
        cells = self.compared()[2]
        if cells.empty:
            return None
        keys = [tuple(key) for key in cells[list(CELL_KEYS)].itertuples(index=False)]
        if self.cell in keys:
            return self.cell
        ranked = cells.assign(
            rank=cells["status"].map(_STATUS_RANK), beyond=cells["nBeyond"] / cells["nVisits"].clip(lower=1)
        ).sort_values(["rank", "beyond"], ascending=False, kind="stable")
        return tuple(ranked.iloc[0][list(CELL_KEYS)])

    def _when(self, visit: int) -> str:
        """Return when a visit began, ``2026-03-09 19:42 HST``; empty without a time."""
        time = self._visitTimes.get(visit)
        return "" if time is None or pd.isna(time) else f"{pd.Timestamp(time):%Y-%m-%d %H:%M} HST"

    def _visitStatus(self, visit: int) -> str:
        statuses = self.images.loc[self.images["visit"] == visit, "status"]
        return max(statuses, key=_STATUS_RANK.get) if len(statuses) else "UNKNOWN"

    # ---- panes -----------------------------------------------------------------------------------

    @param.depends("dataVersion")
    def provenanceBar(self) -> pn.pane.HTML:
        """Return the collection, the thresholds files qadb says its images were judged with, and the read."""
        prov = self.store.provenance
        files = thresholdsLines(self.store.thresholdsFiles)
        parts = [
            f"<b>Collection</b> {_esc(prov.get('collection', '?'))}",
            "<b>Judged with</b> "
            + ("; ".join(_esc(line) for line in files) if files else "no thresholds cited"),
            f"<b>Read with drp_qa</b> {_esc(prov.get('drpQaVersion', '?'))}",
            f"<b>Built</b> {_esc(prov.get('builtAt', '?'))}",
        ]
        warnings = []
        if not files and len(self.visits):
            warnings.append(
                "qadb cites no thresholds for these images (loaded before they were recorded, or from files "
                "this drp_qa no longer ships): the stored verdicts show, their limits do not."
            )
        if not self.store.hasOpdb:
            warnings.append(
                "No opdb: sequences are inferred from runs of one seqName (qadb holds no IIC sequence), times "
                "are qadb's pfs_visit.issued_at (HST)"
                + ("" if self.visits["timeExpStart"].notna().any() else ", and there are none here")
                + ", and there are no observers' notes."
            )
        markup = '<div style="font-size:0.85em;line-height:1.5">' + " · ".join(parts) + "</div>"
        for warning in warnings:
            markup += (
                '<div style="font-size:0.85em;margin-top:4px;padding:4px 8px;border-left:4px solid '
                f'{STATUS_COLORS["WARN"]};background:rgba(230,161,0,0.08)">⚠ {warning}</div>'
            )
        return pn.pane.HTML(markup, sizing_mode="stretch_width")

    def summaryHtml(self) -> str:
        """Return the verdicts so far: images by status, worst first, then the visits and the latest one."""
        if not len(self.visits):
            return "<i>No visit in qadb yet: waiting for qaActor to load the first.</i>"
        counts = verdictCounts(self.images)
        tiles = [
            _tile(f"{status} images", f"{n:,}", STATUS_COLORS[status], STATUS_MARKS[status])
            for status, n in counts.items()
        ]
        visitIds = self.visits["visit"].astype(int)
        first, last = int(visitIds.min()), int(visitIds.max())
        span = " – ".join(when for when in (self._when(first), self._when(last)) if when)
        tiles.append(_tile(f"visits, {first}–{last}" + (f", {span}" if span else ""), f"{len(visitIds):,}"))
        status = self._visitStatus(last)
        when = self._when(last)
        tiles.append(
            _tile(
                f"latest visit {STATUS_MARKS[status]} {status}" + (f", {when}" if when else ""),
                str(last),
                STATUS_COLORS[status],
                STATUS_MARKS[status],
            )
        )
        return (
            '<div style="display:flex;flex-wrap:wrap;gap:12px;margin-top:12px">' + "".join(tiles) + "</div>"
        )

    @param.depends("dataVersion")
    def summary(self) -> pn.pane.HTML:
        return pn.pane.HTML(self.summaryHtml(), sizing_mode="stretch_width")

    @param.depends("dataVersion", "night")
    def strip(self):
        """Return the visits by night and time, each with its rolled-up status."""
        if not len(self.visits):
            return pn.pane.HTML("")
        return pn.pane.Bokeh(visitStrip(self.rollup, self.timeline, self.night), sizing_mode="stretch_width")

    def comparisonHeading(self) -> str:
        """Return what is compared with what: the night's visits, and each reference's."""
        visits = nightVisits(self.timeline, self.night)
        if not visits:
            return ""
        what = "every visit" if self.night == ALL_VISITS else f"the night of {self.night}"
        span = f"{len(visits)} visits, {min(visits)}–{max(visits)}"
        records = {r["name"]: r for r in self.store.provenance.get("references") or []}
        names = []
        for name in self.store.references:
            visitRange = records.get(name, {}).get("visits")
            names.append(_esc(name) + (f" (visits {visitRange[0]}–{visitRange[1]})" if visitRange else ""))
        return (
            f"<b>Compared with the reference visits</b>: {what} ({span}) against "
            + "; ".join(names)
            + ". A cell is filled by the status of the night's median against the reference's median for that "
            "camera, by the metric's tolerance (the worst of its populations; ? where the metric declares no "
            "tolerance or the reference has no such frames), and says the relative difference, or for a "
            "metric without a tolerance the difference in its own units; "
            f"“{STATUS_MARKS['FAIL']} n/m” counts the night's visits that are themselves beyond the "
            "FAIL tolerance. Click a cell for its visits."
        )

    @param.depends("dataVersion", "night", "cell")
    def grids(self):
        """Return a grid per reference: metric and frame type by camera."""
        if not len(self.visits):
            return pn.pane.HTML("")
        if not self.store.references:
            return pn.pane.HTML(
                '<div style="font-size:0.9em">No reference visits to compare with: start the dashboard with '
                "<code>--reference NAME=[COLLECTION:]FIRST-LAST</code>.</div>"
            )
        cells = self.compared()[2]
        parts = [pn.pane.HTML(f'<div style="font-size:0.9em">{self.comparisonHeading()}</div>')]
        shown = self.shownCell()
        for name in self.store.references:
            ours = cells[cells["referenceName"] == name]
            parts.append(pn.pane.HTML(f"<h3 style='margin:8px 0 0'>Against {_esc(name)}</h3>"))
            if ours.empty:
                parts.append(pn.pane.HTML("<i>These visits have no values of the compared metrics.</i>"))
                continue
            grid = comparisonGrid(ours, onSelect=self.selectCell, selected=shown)
            parts.append(pn.pane.Bokeh(grid, sizing_mode="stretch_width"))
        return pn.Column(*parts, sizing_mode="stretch_width")

    def _shownPopulation(self) -> tuple[pd.DataFrame, str]:
        """Return the shown cell's comparison rows, worst first, and the population in detail.

        The first is the one the grid's cell is of (`comparison.worstFirst`).
        """
        rows = worstFirst(populationRows(self.compared()[0], self.shownCell()))
        options = list(rows["population"])
        return rows, self.population if self.population in options else options[0]

    def detailHtml(self) -> str:
        """Return the cell in detail in words: what it is, the two medians and the visits beyond tolerance."""
        cell = self.shownCell()
        if cell is None:
            return ""
        rows, population = self._shownPopulation()
        row = rows[rows["population"] == population].iloc[0]
        deviations = populationRows(self.compared()[1], cell)
        deviations = deviations[deviations["population"] == population]
        beyond = sorted(int(v) for v in deviations.loc[deviations["status"] == "FAIL", "visit"].unique())
        referenceName, metric, obsType, camera = cell
        text = (
            f"<b>{_esc(metric)} · {_esc(obsType)} · {_esc(camera)}</b> against {_esc(referenceName)}, "
            f"population {_esc(population)}: {STATUS_MARKS[row['status']]} {row['status']}, "
            f"median {row['runMedian']:.4g} (n={int(row['nRun'])})"
        )
        if pd.isna(row["referenceMedian"]):
            return _detail(text + "; the reference has no values of this population, so nothing is judged.")
        text += (
            f" vs {row['referenceMedian']:.4g} (n={int(row['nReference'])}), difference "
            f"{row['difference']:+.4g} ({relativeText(row['relative'])})."
        )
        if pd.notna(row["fail"]):
            text += f" Visits beyond the FAIL tolerance: {len(beyond)} of {deviations['visit'].nunique()}"
            text += (": " + ", ".join(str(v) for v in beyond) if beyond else "") + "."
        else:
            text += " The metric declares no tolerance, so nothing is judged."
        return _detail(text)

    @param.depends("dataVersion", "night", "cell", "population")
    def detail(self):
        """Return the chosen cell's values visit by visit, against the reference median and tolerances."""
        cell = self.shownCell()
        if cell is None:
            return pn.pane.HTML("")
        rows, population = self._shownPopulation()
        row = rows[rows["population"] == population].iloc[0]
        deviations = populationRows(self.compared()[1], cell)
        deviations = deviations[deviations["population"] == population]
        deviations = deviations.assign(time=deviations["visit"].map(self._visitTimes))
        chooser = pn.widgets.Select(
            name="Population", options=list(rows["population"]), value=population, width=360
        )
        chooser.param.watch(self._choosePopulation, "value")
        return pn.Column(
            pn.pane.HTML(self.detailHtml()),
            chooser,
            pn.pane.Bokeh(cellDetail(deviations, row), sizing_mode="stretch_width"),
            sizing_mode="stretch_width",
        )

    # ---- layout ----------------------------------------------------------------------------------

    def view(self) -> pn.template.FastListTemplate:
        if self.store.live:
            pn.state.add_periodic_callback(self.refresh, period=int(self.refreshSeconds * 1000))
        return pn.template.FastListTemplate(
            title="PFS QA dashboard",
            main=[
                pn.panel(self.provenanceBar),
                pn.panel(self.summary),
                pn.panel(self.strip),
                pn.widgets.Select.from_param(self.param.night, width=260),
                pn.panel(self.grids),
                pn.panel(self.detail),
            ],
            accent="#3b6ea8",
            theme_toggle=True,
            main_max_width="1600px",
        )


def createApp(store: MetricsStore, refreshSeconds: float = 30.0) -> pn.template.FastListTemplate:
    """Return the dashboard over a store.

    A store that is `~MetricsStore.live` (`live.QadbMetricsStore` over qadb) is polled every
    ``refreshSeconds``, and one store may serve every session.
    """
    pn.extension(sizing_mode="stretch_width")
    return Dashboard(store, refreshSeconds).view()
