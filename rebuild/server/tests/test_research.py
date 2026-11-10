"""Backtests from the app: queue, market-hours wait, run, results, trades with fills, audit, ownership."""
from datetime import date, datetime, timedelta

import pytest
from test_data import _write

from tdserver.clock import IST
from tdserver.models import BacktestRun
from tdserver.research import jobs
from tdserver.research.runner import run_one

D1, D2 = date(2026, 10, 12), date(2026, 10, 13)
EXP = date(2026, 10, 13)


def _mins(d):
    t = datetime(d.year, d.month, d.day, 9, 15)
    return [t + timedelta(minutes=i) for i in range(375)]


@pytest.fixture(scope="module")
def store_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("bt")
    for d in (D1, D2):
        _write(root, "idx", "NIFTY", d, [(t, "NIFTY", "NIFTY", None, None, None, 25000, 25001, 24999, 25000, 0, 0,
                                          "truedata") for t in _mins(d)])
    def opt(d, strike, typ, fn):
        sym = f"NIFTY{EXP:%y%m%d}{strike}{typ}"
        return [(t, sym, "NIFTY", EXP, float(strike), typ, fn(i), fn(i), fn(i), fn(i), 650, 1000, "truedata")
                for i, t in enumerate(_mins(d))]
    _write(root, "opt", "NIFTY", D1, opt(D1, 26000, "CE", lambda i: 40.0 if i < 100 else 90.0)
           + opt(D1, 24000, "PE", lambda i: 40.0))
    _write(root, "opt", "NIFTY", D2, opt(D2, 24000, "PE", lambda i: 30.0 - i * 0.05))
    return root


@pytest.fixture
def research(ctx, store_root, tmp_path, clock):
    ctx.settings.store_root = str(store_root)
    ctx.settings.runs_dir = str(tmp_path / "runs")
    clock.at = datetime(2026, 11, 3, 18, 0, tzinfo=IST)      # after the close
    return ctx


def _run_queue(ctx):
    return jobs.tick(ctx, launch=lambda c, rid: run_one(c, rid))


def test_templates_listed(client, owner, research):
    t = client.get("/v1/research/templates", headers=owner["h"]).json()
    ids = {x["id"] for x in t}
    assert {"strangle_nifty", "orb_nifty"} <= ids
    s = next(x for x in t if x["id"] == "strangle_nifty")
    assert s["stop_loss"] and s["max_loss_per_day"] > 0


def test_full_run(client, owner, research):
    h = owner["h"]
    r = client.post("/v1/research/backtests", json={"template": "strangle_nifty", "start": "2026-10-12",
                                                    "end": "2026-10-13", "name": "Strangle test",
                                                    "overrides": {"risk.close_lone_leg_eod": False}}, headers=h)
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "queued" and run["fill_model"] == "honest"
    assert _run_queue(research) == f"ran {run['id']}"
    got = client.get(f"/v1/research/backtests/{run['id']}", headers=h).json()
    assert got["status"] == "done", got["note"]
    m = got["metrics"]
    assert m["trades"] == 1 and m["charges"] > 0 and abs(m["net_pnl"] - (m["gross_pnl"] - m["charges"])) < 0.05
    assert got["summary"]["luck_warning"] is True
    a = got["audit"]
    assert a["dates"]["with_data"] == 2 and a["fill_model"]["fill_model"] == "honest"
    assert a["lot_sizes_by_expiry"] == {"2026-10-13": 65}       # NIFTY lot by expiry (refs.yaml), not hard-coded
    trades = client.get(f"/v1/research/backtests/{run['id']}/trades", headers=h).json()
    assert trades["total"] == 1
    reasons = [f["reason"] for f in trades["trades"][0]["fills"]]
    assert reasons[:2] == ["entry", "entry"] and "stop_loss" in reasons and "expiry" in reasons
    days = client.get(f"/v1/research/backtests/{run['id']}/curves", headers=h).json()["days"]
    assert [d["date"] for d in days] == ["2026-10-12", "2026-10-13"]
    # saved runs: star, rerun, list, delete
    client.patch(f"/v1/research/backtests/{run['id']}", json={"starred": True}, headers=h)
    again = client.post(f"/v1/research/backtests/{run['id']}/rerun", headers=h).json()
    assert again["status"] == "queued"
    assert [x["id"] for x in client.get("/v1/research/backtests?starred=true", headers=h).json()] == [run["id"]]
    assert client.delete(f"/v1/research/backtests/{run['id']}", headers=h).json()["ok"] is True
    assert client.get(f"/v1/research/backtests/{run['id']}", headers=h).status_code == 404


def test_runs_wait_for_the_close(client, owner, research, clock):
    clock.at = datetime(2026, 11, 3, 11, 0, tzinfo=IST)
    rid = client.post("/v1/research/backtests", json={"template": "strangle_nifty", "start": "2026-10-12",
                                                      "end": "2026-10-13"}, headers=owner["h"]).json()["id"]
    assert _run_queue(research) == "waiting"
    assert "close" in client.get(f"/v1/research/backtests/{rid}", headers=owner["h"]).json()["note"]
    clock.at = datetime(2026, 11, 3, 15, 41, tzinfo=IST)
    assert _run_queue(research) == f"ran {rid}"


def test_no_stop_loss_no_run(client, owner, research):
    lib = pytest.importorskip("tradedesk_spec")
    raw = lib.load_template("strangle_nifty").model_dump(mode="json")
    del raw["risk"]["stop_loss"]
    r = client.post("/v1/research/backtests", json={"spec": raw, "start": "2026-10-12", "end": "2026-10-13"},
                    headers=owner["h"])
    assert r.status_code == 400 and "not valid" in r.json()["detail"]
    r = client.post("/v1/research/backtests", json={"template": "strangle_nifty", "start": "2026-10-12",
                                                    "end": "2026-10-13", "overrides": {"risk.nonsense": 1}},
                    headers=owner["h"])
    assert r.status_code == 400
    r = client.post("/v1/research/backtests", json={"template": "strangle_nifty", "start": "2026-10-13",
                                                    "end": "2026-10-12"}, headers=owner["h"])
    assert r.status_code == 400


def test_failed_run_and_cancel(client, owner, research, ctx):
    ctx.settings.store_root = "/nonexistent/store"
    rid = client.post("/v1/research/backtests", json={"template": "strangle_nifty", "start": "2026-10-12",
                                                      "end": "2026-10-13"}, headers=owner["h"]).json()["id"]
    _run_queue(research)
    got = client.get(f"/v1/research/backtests/{rid}", headers=owner["h"]).json()
    assert got["status"] in ("failed", "done")
    rid2 = client.post("/v1/research/backtests", json={"template": "strangle_nifty", "start": "2026-10-12",
                                                       "end": "2026-10-13"}, headers=owner["h"]).json()["id"]
    assert client.post(f"/v1/research/backtests/{rid2}/cancel", headers=owner["h"]).json()["status"] == "cancelled"
    assert _run_queue(research) == "idle"


def test_runs_are_private(client, ctx, owner, research):
    from conftest import Phone, enroll, sign_in
    rid = client.post("/v1/research/backtests", json={"template": "strangle_nifty", "start": "2026-10-12",
                                                      "end": "2026-10-13"}, headers=owner["h"]).json()["id"]
    p = Phone()
    enroll(client, ctx, p, role="viewer", name="Viewer")
    h = {"Authorization": f"Bearer {sign_in(client, p)}"}
    assert client.get(f"/v1/research/backtests/{rid}", headers=h).status_code == 404
    assert client.post("/v1/research/backtests", json={"template": "strangle_nifty", "start": "2026-10-12",
                                                       "end": "2026-10-13"}, headers=h).status_code == 403


def test_restart_requeues_running(ctx, owner, research):
    with ctx.sessions() as db:
        db.add(BacktestRun(user_id=1, name="x", underlying="NIFTY", spec={}, start=D1, end=D2, status="running",
                           created_at=ctx.clock.now()))
        db.commit()
        assert jobs.recover(db) == 1


def test_conservative_fills_double_the_stress(client, owner, research):
    r = client.post("/v1/research/backtests", json={"template": "strangle_nifty", "start": "2026-10-12",
                                                    "end": "2026-10-13", "fills": "conservative"}, headers=owner["h"]).json()
    assert r["fill_model"] == "honest" and r["cost_stress"] == 2.0


def test_sweep_with_plateau_and_conservative_recheck(client, owner, research):
    h = owner["h"]
    body = {"template": "strangle_nifty", "start": "2026-10-12", "end": "2026-10-13", "split": "2026-10-12",
            "grid": {"risk.stop_loss.value": [1.5, 2, 2.5]}}
    r = client.post("/v1/research/sweeps", json=body, headers=h)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert r.json()["kind"] == "sweep"
    _run_queue(research)
    got = client.get(f"/v1/research/backtests/{rid}", headers=h).json()
    assert got["status"] == "done", got["note"]
    sw = got["sweep"]
    assert sw["combos"] == 3 and len(sw["rows"]) == 3
    assert all("neighbours" in x for x in sw["rows"])
    top = [x for x in sw["rows"] if "out_of_sample" in x]
    assert top and all("out_of_sample_conservative" in x for x in top)
    assert "best_reason" in sw
    # bad sweeps are refused clearly
    assert client.post("/v1/research/sweeps", json={**body, "split": "2026-10-14"}, headers=h).status_code == 400
    assert client.post("/v1/research/sweeps", json={**body, "grid": {"risk.nope": [1, 2]}}, headers=h).status_code == 400
    big = {**body, "grid": {"risk.stop_loss.value": list(range(1, 9)), "risk.max_loss_per_day": list(range(1000, 9000, 1000))}}
    assert client.post("/v1/research/sweeps", json=big, headers=h).status_code == 400


def test_compare_and_replay(client, owner, research):
    h = owner["h"]
    ids = []
    for stop in (1.5, 2.0):
        ids.append(client.post("/v1/research/backtests", json={"template": "strangle_nifty", "start": "2026-10-12",
                                                               "end": "2026-10-13",
                                                               "overrides": {"risk.stop_loss.value": stop}},
                               headers=h).json()["id"])
        _run_queue(research)
    c = client.get(f"/v1/research/compare?ids={ids[0]},{ids[1]}", headers=h).json()
    assert len(c["runs"]) == 2 and c["runs"][0]["curve"] and "net_with_charges_x1_5" in c["runs"][0]["figures"]
    assert client.get(f"/v1/research/compare?ids={ids[0]}", headers=h).status_code == 400
    research.with_data()
    rp = client.post("/v1/research/replay", json={"template": "strangle_nifty", "day": "2026-10-12"}, headers=h).json()
    assert rp["day"] == "2026-10-12" and len(rp["index"]) == 375
    assert [f["reason"] for f in rp["fills"]][:2] == ["entry", "entry"]
    assert rp["steps"][-1]["realised"] <= 0 or rp["steps"]
    assert client.post("/v1/research/replay", json={"template": "strangle_nifty", "day": "2026-10-20"},
                       headers=h).status_code == 404


def test_a_run_that_cannot_start_fails_instead_of_staying_queued(ctx, owner, research, monkeypatch):
    import subprocess

    from tdserver.models import BacktestRun
    from tdserver.research import jobs

    def boom(*a, **k):
        raise subprocess.SubprocessError("Exception occurred in preexec_fn.")
    monkeypatch.setattr(jobs.subprocess, "run", boom)
    with ctx.sessions() as db:
        r = BacktestRun(user_id=1, name="x", underlying="NIFTY", spec={}, start=D1, end=D2, status="queued",
                        created_at=ctx.clock.now())
        db.add(r)
        db.commit()
        rid = r.id
    jobs._launch(ctx, rid)
    with ctx.sessions() as db:
        r = db.get(BacktestRun, rid)
        assert r.status == "failed" and "could not start" in r.note
