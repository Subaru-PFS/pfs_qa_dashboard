"""Tests for the command line (``bin/qaDashboard.py``): its subcommands, with stand-ins for the databases."""

import importlib.util
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

from pfs.qa.dashboard import live
from pfs.qa.dashboard.live import QadbStore
from pfs.qa.dashboard.qadbSource import readSnapshot


def _cli():
    path = Path(__file__).parents[1] / "bin" / "qaDashboard.py"
    spec = importlib.util.spec_from_file_location("qaDashboard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(qadb, collection, first, last, since=None):
    """Return what `pfs.drp.qa.qadb.reader.readQadb` returns for one image."""
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
            "calexpRun": [""],
            "decidedAt": [pd.Timestamp("2026-11-01 19:00")],
        }
    )
    empty = pd.DataFrame(columns=["visit", "arm", "spectrograph", "decidedAt"])
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
        "visitTimes": times,
    }


def _gate(qadb, collection, first, last):
    """Return what `pfs.drp.qa.qadb.reader.readGate` returns for `_read`'s image."""
    return pd.DataFrame(
        {
            "visit": [1001],
            "arm": ["b"],
            "spectrograph": [1],
            "frameType": ["arc"],
            "qaStatus": ["WARN"],
            "qaDecidedBy": ["width"],
            "qaReason": ["line and trace width 4% above normal"],
            "mode": ["live"],
            "decidedAt": [pd.Timestamp("2026-11-01 19:00")],
            "run": ["r"],
            "processingId": [1],
        }
    )


def _qadb(monkeypatch):
    """Answer the CLI's qadb reads with `_read` and `_gate`, and return what each was asked."""
    asked = {"numbers": [], "gate": []}
    monkeypatch.setitem(
        sys.modules, "pfs.utils.database.qadb", types.SimpleNamespace(QaDB=lambda **kwargs: object())
    )
    monkeypatch.setattr(live, "readQadb", lambda *args: asked["numbers"].append(args[1:]) or _read(*args))
    monkeypatch.setattr(live, "readGate", lambda *args: asked["gate"].append(args[1:]) or _gate(*args))
    return asked


def testServeTakesOneSource():
    parser = _cli().makeParser()
    for argv in (["serve"], ["serve", "--collection", "c", "--snapshot", "d"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    # Negative control: one source is enough.
    assert parser.parse_args(["serve", "--snapshot", "d"]).snapshot == Path("d")


def testSnapshotWritesWhatServeReads(tmp_path, monkeypatch):
    """``snapshot`` reads qadb once into a directory that ``serve --snapshot`` serves."""
    asked = _qadb(monkeypatch)
    connected = []
    fakeQadb = types.SimpleNamespace(QaDB=lambda **kwargs: connected.append(kwargs) or object())
    monkeypatch.setitem(sys.modules, "pfs.utils.database.qadb", fakeQadb)

    _cli().main(["snapshot", "--collection", "c", "--out", str(tmp_path / "snap")])
    assert connected[0]["user"] == "pfs"
    tables, provenance = readSnapshot(tmp_path / "snap")
    assert provenance["collection"] == "c (qadb snapshot)" and "references" not in provenance
    store = QadbStore.fromTables(tables)
    assert store.raw["iqQaMetrics"]["visit"].tolist() == [1001] and store.times["visit"].tolist() == [1001]
    # The gate's verdicts travel with the snapshot, read over the same visits as the numbers.
    assert store.raw["qaGate"]["qaStatus"].tolist() == ["WARN"]
    assert asked["gate"] == [("c", 0, 2**31 - 1)] and asked["numbers"] == [("c", 0, 2**31 - 1, None)]
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

    # A snapshot from before the gate's verdicts were read holds none: it is written again, not served.
    _qadb(monkeypatch)
    cli.main(["snapshot", "--collection", "c", "--out", str(tmp_path / "snap")])
    (tmp_path / "snap" / "qaGate.parquet").rename(tmp_path / "qaGate.parquet")
    with pytest.raises(SystemExit):
        cli.main(["serve", "--snapshot", str(tmp_path / "snap")])
    assert "no qaGate.parquet" in capsys.readouterr().err and not served

    # Negative control: a directory the snapshot command wrote is served.
    (tmp_path / "qaGate.parquet").rename(tmp_path / "snap" / "qaGate.parquet")
    cli.main(["serve", "--snapshot", str(tmp_path / "snap")])
    assert served and served[0].snapshot == tmp_path / "snap"


def testOptionsOfTheMetricViewsAndTheReportsOwnLimitsAreGone(capsys):
    """The reference visits and websocket origins were the metric views', the limits the report's own judging."""
    parser = _cli().makeParser()
    for option in (
        ["--reference", "a=1-2"],
        ["--allow-origin", "host"],
        ["--kms-r", "1", "2"],
        ["--offset-x", "0.5", "1"],
        ["--no-mode-correction"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(["serve", "--collection", "c", *option])
        assert "unrecognized arguments" in capsys.readouterr().err
    # Negative control: the address to listen on is still an option.
    assert parser.parse_args(["serve", "--collection", "c", "--address", "0.0.0.0"]).address == "0.0.0.0"
