"""The QA dashboard: a Panel app over the metrics table.

One page: the provenance strip (collection, thresholds, drp_qa version, time read) and
the verdicts so far, the images counted by status with the latest visit judged. The
views are built up on this one at a time; the prototype's Live, Run and Visit tabs are
at tag 0.1.0.

Everything shown comes from the metrics table (`live.QadbMetricsStore`, read from qadb
or a snapshot of it); the app computes no metric and holds no threshold of its own: the
limits are the thresholds qadb records each image was judged with, found for each value by
the gate's `judge`, and the provenance strip names those files.
"""

import html

import pandas as pd
import panel as pn
import param

from pfs.drp.qa.metrics.gate import isConfigLayer
from pfs.qa.dashboard.schema import IMAGE_STATUS_ORDER
from pfs.qa.dashboard.store import MetricsStore, visitTimeline

__all__ = ["Dashboard", "createApp", "thresholdsLines", "verdictCounts"]

#: Status colours (Okabe-Ito-like, distinguishable with the common colour-vision deficiencies).
STATUS_COLORS = {"PASS": "#3a9a5b", "WARN": "#e6a100", "FAIL": "#d1495b", "UNKNOWN": "#9aa0a6", "": "#c7ccd1"}

#: Status marks, so no status is told by colour alone.
STATUS_MARKS = {"PASS": "✓", "WARN": "!", "FAIL": "✕", "UNKNOWN": "?"}

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


class Dashboard(param.Parameterized):
    """The app's state: the data's version, bumped when a refresh brings new loads."""

    dataVersion = param.Integer(default=0)

    def __init__(self, store: MetricsStore, refreshSeconds: float = 30.0, **params):
        self.store = store
        self.refreshSeconds = refreshSeconds
        self._reload()
        super().__init__(**params)

    def _reload(self) -> None:
        """Read the visits and images from the store."""
        self._seenVersion = self.store.version
        self.visits = self.store.visits()
        self.images = self.store.images()
        self.timeline = visitTimeline(self.visits)
        self._visitTimes = self.timeline.set_index("visit")["time"]

    def refresh(self) -> None:
        """Take what the store has read since the last call."""
        self.store.refresh()
        if self.store.version == self._seenVersion:
            return
        self._reload()
        self.dataVersion += 1

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

    # ---- layout ----------------------------------------------------------------------------------

    def view(self) -> pn.template.FastListTemplate:
        if self.store.live:
            pn.state.add_periodic_callback(self.refresh, period=int(self.refreshSeconds * 1000))
        return pn.template.FastListTemplate(
            title="PFS QA dashboard",
            main=[pn.panel(self.provenanceBar), pn.panel(self.summary)],
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
