"""Health lights (amber after one failure, red after two, alert on red) and the Home index strip."""
from datetime import datetime, timedelta

from test_data import _write

from tdserver.clock import IST
from tdserver.health import tick as health_tick
from tdserver.models import Alert, ServiceStatus
from tdserver.recorder.service import put_status


def _lights(client, owner):
    return {c["name"]: c for c in client.get("/v1/health/detail", headers=owner["h"]).json()["checks"]}


def test_lights_go_amber_then_red_then_alert(client, ctx, owner, clock):
    ctx.settings.health_alerts = True
    clock.at = datetime(2026, 11, 3, 10, 0, tzinfo=IST)              # market hours, recorder never ran
    with ctx.sessions() as db:
        health_tick(db, ctx); db.commit()
    assert _lights(client, owner)["Recorder"]["light"] == "amber"
    with ctx.sessions() as db:
        assert health_tick(db, ctx) is None                           # not again within the minute
    clock.advance(seconds=61)
    with ctx.sessions() as db:
        health_tick(db, ctx); db.commit()
    lights = _lights(client, owner)
    assert lights["Recorder"]["light"] == "red" and lights["Kite login"]["light"] == "red"
    with ctx.sessions() as db:
        titles = {a.title for a in db.query(Alert).filter_by(acked_at=None)}
    assert "Server: Recorder needs attention" in titles
    assert not any("Kite login" in t for t in titles)                 # the Kite reminders already cover it
    # the recorder starts: green again, alert closes
    with ctx.sessions() as db:
        put_status(db, ctx, "recorder", state="recording", stale=False)
        put_status(db, ctx, "worker", state="running")
        db.commit()
    clock.advance(seconds=61)
    with ctx.sessions() as db:
        put_status(db, ctx, "recorder", state="recording", stale=False)
        put_status(db, ctx, "worker", state="running")
        health_tick(db, ctx); db.commit()
    assert _lights(client, owner)["Recorder"]["light"] == "green"
    with ctx.sessions() as db:
        assert db.query(Alert).filter_by(title="Server: Recorder needs attention", acked_at=None).count() == 0
    assert {"Disk", "Memory", "CPU", "HTTPS", "Alerts worker"} <= set(lights)


def _mins(d, n=375):
    t = datetime(d.year, d.month, d.day, 9, 15)
    return [t + timedelta(minutes=i) for i in range(n)]


def test_index_strip_live_and_closed(client, ctx, owner, clock, tmp_path):
    from datetime import date
    for d, px in ((date(2026, 10, 30), 25000.0), (date(2026, 11, 2), 25250.0)):
        _write(tmp_path, "idx", "NIFTY", d, [(t, "NIFTY", "NIFTY", None, None, None, px, px, px, px, 0, 0, "truedata")
                                             for t in _mins(d)])
    ctx.settings.store_root = str(tmp_path)
    ctx.with_data()
    clock.at = datetime(2026, 11, 3, 8, 0, tzinfo=IST)                # before the open: last close
    s = client.get("/v1/market/indexes", headers=owner["h"]).json()
    n = s["indexes"][0]
    assert n["underlying"] == "NIFTY" and n["live"] is False and n["price"] == 25250.0 and n["change_pct"] == 1.0
    clock.at = datetime(2026, 11, 3, 10, 0, tzinfo=IST)               # recorder live
    with ctx.sessions() as db:
        put_status(db, ctx, "recorder", state="recording", day="2026-11-03",
                   indexes={"NIFTY": {"ltp": 25502.5, "open": 25300, "high": 25510, "low": 25290,
                                      "at": "2026-11-03T10:00:00+05:30"}})
        db.commit()
    n = client.get("/v1/market/indexes", headers=owner["h"]).json()["indexes"][0]
    assert n["live"] is True and n["price"] == 25502.5 and n["change_pct"] == 1.0
    clock.advance(minutes=5)                                            # recorder silent: back to the last close
    assert client.get("/v1/market/indexes", headers=owner["h"]).json()["indexes"][0]["live"] is False


def test_update_now(client, ctx, owner, tmp_path):
    import json
    ctx.settings.data_dir = str(tmp_path)
    (tmp_path / "update-available.json").write_text(json.dumps({"current": "abc1234", "available": "def5678",
                                                                "behind": 2, "subject": "Merge #2"}))
    v = client.get("/v1/system/version", headers=owner["h"]).json()
    assert v["available"]["behind"] == 2 and v["requested"] is False
    assert client.post("/v1/system/update", headers=owner["h"]).json()["ok"] is True
    assert (tmp_path / "update-request").exists()
    assert client.post("/v1/system/update", headers=owner["h"]).status_code == 409
    from conftest import Phone, enroll, sign_in
    p = Phone()
    enroll(client, ctx, p, role="trader", name="Brother")
    assert client.post("/v1/system/update", headers={"Authorization": f"Bearer {sign_in(client, p)}"}).status_code == 403
