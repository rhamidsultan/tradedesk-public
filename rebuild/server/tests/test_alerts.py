from datetime import datetime
from urllib.parse import urlparse

from tdserver.alerts.dispatcher import dispatch_due
from tdserver.alerts.outbox import raise_alert
from tdserver.alerts.reminders import run_reminders
from tdserver.clock import IST
from tdserver.models import Alert, AlertChannel
from tdserver.worker import tick


def _channels(client, owner):
    assert client.put("/v1/alerts/channels", json={"kind": "telegram", "target": "123456789"},
                      headers=owner["h"]).status_code == 200
    topic = client.post("/v1/alerts/channels/ntfy-topic", headers=owner["h"]).json()["topic"]
    assert client.put("/v1/alerts/channels", json={"kind": "ntfy", "target": topic},
                      headers=owner["h"]).status_code == 200
    return topic


def test_channel_targets_encrypted_and_masked(client, ctx, owner):
    _channels(client, owner)
    out = client.get("/v1/alerts/channels", headers=owner["h"]).json()
    assert out["telegram"]["target"].endswith("789") and "123456" not in out["telegram"]["target"]
    with ctx.sessions() as db:
        assert all(b"123456789" not in c.target_enc for c in db.query(AlertChannel))


def test_channel_validation(client, owner):
    assert client.put("/v1/alerts/channels", json={"kind": "ntfy", "target": "short"},
                      headers=owner["h"]).status_code == 400
    assert client.put("/v1/alerts/channels", json={"kind": "telegram", "target": "@me"},
                      headers=owner["h"]).status_code == 400


def test_levels_route_to_channels(client, ctx, owner, net):
    _channels(client, owner)
    for lvl in ("info", "important"):
        client.post(f"/v1/alerts/test?level={lvl}", headers=owner["h"])
    tick(ctx)
    tg, nt = net.bodies("api.telegram.org"), net.bodies("ntfy.example")
    assert len(tg) == 2 and len(nt) == 1                  # info goes to Telegram only, silently
    assert tg[0]["disable_notification"] is True
    assert nt[0]["priority"] == 4 and nt[0]["actions"][0]["method"] == "POST"


def test_critical_repeats_until_acknowledged(client, ctx, owner, net, clock):
    _channels(client, owner)
    a = client.post("/v1/alerts/test?level=critical", headers=owner["h"]).json()
    tick(ctx)
    assert len(net.bodies("ntfy.example")) == 1
    clock.advance(seconds=60)
    tick(ctx)
    assert len(net.bodies("ntfy.example")) == 1           # not yet 2 minutes
    clock.advance(seconds=61)
    tick(ctx)
    assert len(net.bodies("ntfy.example")) == 2
    assert "(repeat 1)" in net.bodies("ntfy.example")[1]["title"]
    client.post(f"/v1/alerts/{a['id']}/ack", headers=owner["h"])
    clock.advance(minutes=5)
    tick(ctx)
    assert len(net.bodies("ntfy.example")) == 2           # stopped


def test_critical_slows_to_15_minutes_after_an_hour(ctx, owner, client, clock):
    _channels(client, owner)
    a = client.post("/v1/alerts/test?level=critical", headers=owner["h"]).json()
    for _ in range(40):
        tick(ctx)
        clock.advance(minutes=2)
    with ctx.sessions() as db:
        al = db.get(Alert, a["id"])
        gap = al.next_send_at - clock.now()
        assert al.sends >= 30 and gap.total_seconds() > 10 * 60


def test_ack_link_from_message(client, ctx, owner, net):
    _channels(client, owner)
    client.post("/v1/alerts/test?level=critical", headers=owner["h"])
    tick(ctx)
    ack_url = net.bodies("ntfy.example")[0]["actions"][0]["url"]
    path = urlparse(ack_url)
    page = client.get(f"{path.path}?{path.query}")
    assert page.status_code == 200 and "Acknowledge" in page.text
    assert client.get("/v1/alerts?open_only=true", headers=owner["h"]).json() != []   # GET changed nothing
    done = client.post(f"{path.path}?{path.query}")
    assert done.status_code == 200 and "Repeats stopped" in done.text
    assert client.get("/v1/alerts?open_only=true", headers=owner["h"]).json() == []
    assert client.post(f"{path.path}?{path.query[:-3]}abc").status_code == 400      # tampered


def test_mute_stops_messages_but_never_critical(client, ctx, owner, net):
    _channels(client, owner)
    client.put("/v1/alerts/prefs", json={"category": "system", "muted": True}, headers=owner["h"])
    client.post("/v1/alerts/test?level=important", headers=owner["h"])
    client.post("/v1/alerts/test?level=critical", headers=owner["h"])
    tick(ctx)
    titles = [b["title"] for b in net.bodies("ntfy.example")]
    assert titles == ["Test alert (critical)"]
    assert len(client.get("/v1/alerts", headers=owner["h"]).json()) == 2   # both still listed in the app


def test_dedupe_open_alerts(ctx, owner):
    with ctx.sessions() as db:
        a1 = raise_alert(db, ctx, 1, "important", "market_data", "Feed stale", dedupe_key="feed:NIFTY")
        a2 = raise_alert(db, ctx, 1, "important", "market_data", "Feed stale", dedupe_key="feed:NIFTY")
        assert a1.id == a2.id


def test_kite_reminders_on_trading_days_only(client, ctx, owner, clock, net):
    _channels(client, owner)
    client.put("/v1/broker/kite", json={"client_id": "AB1234", "api_key": "kiteapikey1",
                                        "api_secret": "kitesecret99"}, headers=owner["h"])
    clock.at = datetime(2026, 11, 3, 8, 20, tzinfo=IST)
    with ctx.sessions() as db:
        assert run_reminders(db, ctx) == []
    clock.at = datetime(2026, 11, 3, 8, 31, tzinfo=IST)
    with ctx.sessions() as db:
        assert len(run_reminders(db, ctx)) == 1
        db.commit()
    clock.at = datetime(2026, 11, 3, 8, 46, tzinfo=IST)
    tick(ctx)
    tick(ctx)
    alerts = client.get("/v1/alerts", headers=owner["h"]).json()
    assert [a["level"] for a in alerts] == ["important", "info"]    # one each, no duplicates
    clock.at = datetime(2026, 11, 10, 8, 50, tzinfo=IST)            # holiday
    with ctx.sessions() as db:
        assert run_reminders(db, ctx) == []


def test_kite_login_closes_reminders(client, ctx, owner, clock, net):
    from test_kite import _login
    clock.at = datetime(2026, 11, 3, 8, 50, tzinfo=IST)
    client.put("/v1/broker/kite", json={"client_id": "AB1234", "api_key": "kiteapikey1",
                                        "api_secret": "kitesecret99"}, headers=owner["h"])
    tick(ctx)
    assert len(client.get("/v1/alerts?open_only=true", headers=owner["h"]).json()) == 2
    _login(client, owner, net)
    assert client.get("/v1/alerts?open_only=true", headers=owner["h"]).json() == []


def test_reinstall_reminder_on_saturday(client, ctx, owner, clock):
    from conftest import sign_in
    sign_in(client, owner["phone"], install_expires_at="2026-11-09T05:00:00+00:00")
    clock.at = datetime(2026, 11, 7, 10, 5, tzinfo=IST)   # Saturday
    tick(ctx)
    titles = [a["title"] for a in client.get("/v1/alerts", headers=owner["h"]).json()]
    assert "Re-install TradeDesk on iPhone" in titles


def test_missing_holidays_alert_in_december(client, ctx, owner, clock):
    clock.at = datetime(2026, 12, 1, 10, 0, tzinfo=IST)
    tick(ctx)
    titles = [a["title"] for a in client.get("/v1/alerts", headers=owner["h"]).json()]
    assert "Add the 2027 market holidays" in titles


def test_open_link_redirects_to_app(client):
    r = client.get("/o/broker/kite")
    assert r.headers["location"] == "tradedesk://broker/kite"
    assert client.get("/o/x<script>").headers["location"] == "tradedesk://xscript"


def test_failed_delivery_is_recorded_not_raised(client, ctx, owner, net):
    from tdserver.models import AlertDelivery
    _channels(client, owner)
    net.down = True
    client.post("/v1/alerts/test?level=important", headers=owner["h"])
    tick(ctx)
    with ctx.sessions() as db:
        rows = db.query(AlertDelivery).all()
        assert rows and all(not r.ok and r.detail == "network: ConnectError" for r in rows)


def test_telegram_one_tap_linking(client, ctx, owner, net):
    url = client.post("/v1/alerts/channels/telegram-link", headers=owner["h"]).json()["url"]
    assert url.startswith("https://t.me/TradeDeskBot?start=")
    code = url.split("start=")[1]
    assert client.post("/v1/alerts/channels/telegram-link/check", headers=owner["h"]).json()["ok"] is False
    net.tg_updates = [
        {"message": {"text": "/start WRONGCODE", "chat": {"id": 111, "type": "private"}}},
        {"message": {"text": f"/start {code}", "chat": {"id": 987654321, "type": "private"}}},
    ]
    assert client.post("/v1/alerts/channels/telegram-link/check", headers=owner["h"]).json()["ok"] is True
    ch = client.get("/v1/alerts/channels", headers=owner["h"]).json()["telegram"]
    assert ch["target"].endswith("321")
    assert net.bodies("sendMessage")[-1]["chat_id"] == "987654321"
