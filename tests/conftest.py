"""The tests need drp_qa (``pfs.drp.qa``) and pfs_utils importable: the stack, or their ``python/`` on PYTHONPATH.

The fixtures here make the thresholds tables `pfs.drp.qa.qadb.reader.readQadb` returns
(``thresholds``, ``thresholdsFiles``), in the shapes the dashboard takes them.
"""

import pandas as pd
import pytest


@pytest.fixture
def makeThresholds():
    """Return a function making a reader's ``thresholds`` table from ``(thresholdsId, metric, warn, fail, population)`` entries.

    Every population column any entry names is a column; the stored ``population`` key is
    the entry's pairs, sorted, as `pfs.drp.qa.qadb.tables.populationKey` writes it.
    """

    def make(*entries, absolute=False, higherIsWorse=True):
        rows = []
        for thresholdsId, metric, warn, fail, population in entries:
            rows.append(
                {
                    "thresholdsId": thresholdsId,
                    "metric": metric,
                    "population": " ".join(f"{k}={v}" for k, v in sorted(population.items())),
                    **population,
                    "higherIsWorse": higherIsWorse,
                    "absolute": absolute,
                    "method": "percentile",
                    "warn": warn,
                    "fail": fail,
                    "provenance": f"test thresholds {thresholdsId}",
                }
            )
        frame = pd.DataFrame(rows)
        populations = sorted({key for *_, population in entries for key in population})
        order = ["thresholdsId", "metric", "population", *populations]
        order += ["higherIsWorse", "absolute", "method", "warn", "fail", "provenance"]
        return frame.reindex(columns=order).astype({"thresholdsId": "int64"})

    return make


@pytest.fixture
def makeThresholdsFiles():
    """Return a function making a reader's ``thresholdsFiles`` table from ``(thresholdsId, name)`` pairs."""

    def make(*files):
        return pd.DataFrame(
            [
                {
                    "thresholdsId": thresholdsId,
                    "sha256": f"{thresholdsId:064x}",
                    "name": name,
                    "derivedOn": pd.Timestamp("2026-10-19"),
                    "collection": "u/test/thresholds",
                    "referenceRuns": "25,29",
                    "drpQaVersion": "w.2026.35",
                    "datamodelVersion": "w.2026.40",
                    "obsPfsVersion": "w.2026.40",
                    "drpStellaVersion": "w.2026.41",
                    "nEntries": 1,
                    "registeredAt": pd.Timestamp("2026-10-26 12:00"),
                }
                for thresholdsId, name in files
            ]
        )

    return make
