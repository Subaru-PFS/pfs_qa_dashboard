"""Tests for the command line (``bin/qaDashboard.py``): its subcommands, with stand-ins for the databases."""

import importlib.util
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

import pfs.drp.qa.qadb.reader
from pfs.qa.dashboard.live import QadbMetricsStore


def _cli():
    path = Path(__file__).parents[1] / "bin" / "qaDashboard.py"
    spec = importlib.util.spec_from_file_location("qaDashboard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(qadb, collection, first, last, since=None):
    """Return what `readQadb` returns for one image."""
    nan = float("nan")
    iq = pd.DataFrame(
        {
            "visit": [1001],
            "arm": ["b"],
            "spectrograph": [1],
            "obsType": ["arc"],
            "seqName": ["Arc: Neon"],
            "illumination": ["full"],
            "traceOnly": [False],
            **{
                name: [nan]
                for name in ("medFwhm", "medDxCenter", "dxCenterRms", "pctFlagged", "linesPerFiber", "nLines")
            },
            "qaStatus": ["PASS"],
            "qaReason": [""],
            "calexpRun": [""],
            "decidedAt": [pd.Timestamp("2026-11-01 19:00")],
            "thresholdsIds": ["1"],
        }
    )
    empty = pd.DataFrame(columns=["visit", "arm", "spectrograph", "decidedAt"])
    thresholds = pd.DataFrame(
        {
            "thresholdsId": [1],
            "metric": ["medFwhm"],
            "population": [""],
            "higherIsWorse": [True],
            "absolute": [False],
            "method": ["percentile"],
            "warn": [3.0],
            "fail": [4.0],
            "provenance": ["test"],
        }
    )
    files = pd.DataFrame({"thresholdsId": [1], "name": ["iqQaThresholds-test.yaml"], "nEntries": [1]})
    times = pd.DataFrame(
        {
            "pfs_visit_id": [1001],
            "time_exp_start": [pd.Timestamp("2026-11-01 19:00")],
            "iic_sequence_id": pd.array([pd.NA], dtype="Int64"),
        }
    )
    return {
        "iqQaMetrics": iq,
        "dmLineResidualSummary": empty,
        "dmLineResidualMetrics": empty,
        "thresholds": thresholds,
        "thresholdsFiles": files,
        "visitTimes": times,
    }


def testServeTakesOneSource():
    parser = _cli().makeParser()
    for argv in (["serve"], ["serve", "--collection", "c", "--snapshot", "d"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    # Negative control: one source is enough.
    assert parser.parse_args(["serve", "--snapshot", "d"]).snapshot == Path("d")


def testSnapshotWritesWhatServeReads(tmp_path, monkeypatch):
    """``snapshot`` reads qadb once into a directory that ``serve --snapshot`` serves."""
    connected = []
    fakeQadb = types.SimpleNamespace(QaDB=lambda **kwargs: connected.append(kwargs) or object())
    monkeypatch.setitem(sys.modules, "pfs.utils.database.qadb", fakeQadb)
    monkeypatch.setattr(pfs.drp.qa.qadb.reader, "readQadb", _read)

    _cli().main(["snapshot", "--collection", "c", "--out", str(tmp_path / "snap")])
    assert connected[0]["user"] == "pfs"
    store = QadbMetricsStore.fromSnapshot(tmp_path / "snap")
    assert store.provenance["collection"] == "c (qadb snapshot)"
    assert store.images()["visit"].tolist() == [1001]
    # The thresholds cited travel with the snapshot.
    assert store.thresholdsFiles["name"].tolist() == ["iqQaThresholds-test.yaml"]
    assert store.trend("iqQaMetrics", "medFwhm").empty  # not measured: NaN values are left out
    # Negative control: a snapshot needs somewhere to go.
    with pytest.raises(SystemExit):
        _cli().makeParser().parse_args(["snapshot", "--collection", "c"])


def testServeRefusesADirectoryThatIsNotASnapshot(tmp_path, monkeypatch, capsys):
    """``serve --snapshot`` names the snapshot command when the directory holds no snapshot."""
    cli = _cli()
    served = []
    monkeypatch.setattr(cli, "serve", served.append)
    with pytest.raises(SystemExit):
        cli.main(["serve", "--snapshot", str(tmp_path / "missing")])
    error = capsys.readouterr().err
    assert "is not a snapshot" in error and "qaDashboard.py snapshot" in error
    assert not served

    # Negative control: a directory the snapshot command wrote is served.
    monkeypatch.setitem(
        sys.modules, "pfs.utils.database.qadb", types.SimpleNamespace(QaDB=lambda **kwargs: object())
    )
    monkeypatch.setattr(pfs.drp.qa.qadb.reader, "readQadb", _read)
    cli.main(["snapshot", "--collection", "c", "--out", str(tmp_path / "snap")])
    cli.main(["serve", "--snapshot", str(tmp_path / "snap")])
    assert served and served[0].snapshot == tmp_path / "snap"
