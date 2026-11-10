"""Smoke test: the Lab dashboard renders and runs a backtest on a tiny store."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[2] / "apps" / "lab" / "app.py")
spec = importlib.util.spec_from_file_location("eng_tests", Path(__file__).resolve().parents[1] / "engine" / "test_backtest.py")
eng = importlib.util.module_from_spec(spec); spec.loader.exec_module(eng)


def test_lab_pages(tmp_path, monkeypatch):
    b = eng.Builder(tmp_path / "store")
    b.spot(eng.D1, eng.flat(25000))
    b.opt(eng.D1, eng.EXP2, 26000, "CE", lambda i: 40.0)
    b.opt(eng.D1, eng.EXP2, 24000, "PE", lambda i: 40.0)
    b.build()
    monkeypatch.setenv("TD_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TD_RUNS", str(tmp_path / "runs"))
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    at.selectbox[0].set_value("strangle_nifty")
    at.date_input[0].set_value(eng.D1); at.date_input[1].set_value(eng.D1)
    at.run()
    at.button[0].click().run()
    assert not at.exception
    assert any("Saved to" in s.value for s in at.success)
    at.sidebar.radio[0].set_value("Runs").run()
    assert not at.exception and at.dataframe
    at.sidebar.radio[0].set_value("Data").run()
    assert not at.exception
