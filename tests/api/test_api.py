"""API tests on a tiny synthetic store."""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location("eng_tests_api", Path(__file__).resolve().parents[1] / "engine" / "test_backtest.py")
eng = importlib.util.module_from_spec(spec); spec.loader.exec_module(eng)


def _client(tmp_path, monkeypatch):
    b = eng.Builder(tmp_path / "store")
    b.spot(eng.D1, eng.flat(25000))
    b.opt(eng.D1, eng.EXP2, 26000, "CE", lambda i: 40.0)
    b.opt(eng.D1, eng.EXP2, 24000, "PE", lambda i: 40.0)
    b.build()
    monkeypatch.setenv("TD_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TD_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("TD_STRATEGIES", str(tmp_path / "strategies"))
    from tradedesk_api.main import app
    return TestClient(app)


def test_templates_validate_save(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.get("/api/status").json()["store_ok"] is True
    t = {x["id"]: x for x in c.get("/api/templates").json()}
    spec = t["strangle_nifty"]["spec"]
    v = c.post("/api/strategies/validate", json=spec).json()
    assert v["ok"] and v["risk"]["lot_size"] == 65 and v["risk"]["legs"][0]["est_loss_at_stop"] > 0
    bad = dict(spec); bad["risk"] = {k: v for k, v in spec["risk"].items() if k != "stop_loss"}
    v = c.post("/api/strategies/validate", json=bad).json()
    assert not v["ok"] and any("stop_loss" in e["field"] for e in v["errors"])
    assert c.post("/api/strategies", json=bad).status_code == 422
    assert c.post("/api/strategies", json=spec).json()["ok"]
    assert len(c.get("/api/strategies").json()) == 1


def test_backtest_job_and_runs(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    spec = {x["id"]: x for x in c.get("/api/templates").json()}["strangle_nifty"]["spec"]
    jid = c.post("/api/backtests", json={"spec": spec, "start": str(eng.D1), "end": str(eng.D1)}).json()["id"]
    for _ in range(100):
        j = c.get(f"/api/backtests/{jid}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert j["status"] == "done", j
    assert j["result"]["metrics"]["net_pnl"] is not None and j["result"]["daily"]
    runs = c.get("/api/runs").json()
    assert runs and c.get(f"/api/runs/{runs[0]['run_id']}").status_code == 200
    assert c.get("/api/runs/..%2F..%2Fetc").status_code == 404
    assert c.get("/api/nope").status_code == 404
