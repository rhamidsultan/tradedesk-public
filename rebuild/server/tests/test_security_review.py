"""Fixes from the independent security review (3 Nov 2026)."""
import base64
import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import Phone, enroll, sign_in
from test_kite import KEYS

from tdserver import devicekeys
from tdserver.models import Alert, AlertDelivery
from tdserver.ratelimit import client_key
from tdserver.routes_auth import new_invite
from tdserver.settings import Settings
from tdserver.worker import tick


def test_worker_logs_never_hold_the_bot_token(client, ctx, owner, caplog):
    caplog.set_level(logging.DEBUG)
    from tdserver.context import quiet_http_logs
    quiet_http_logs()
    caplog.set_level(logging.INFO)
    client.put("/v1/alerts/channels", json={"kind": "telegram", "target": "123456789"}, headers=owner["h"])
    client.post("/v1/alerts/test?level=important", headers=owner["h"])
    tick(ctx)
    assert "BOT:TOKEN" not in caplog.text
    assert logging.getLogger("httpx").level == logging.WARNING


def test_missing_env_means_production():
    with pytest.raises(RuntimeError):
        Settings(_env_file=None, database_url="sqlite://").master_key_bytes()
    with pytest.raises(RuntimeError):  # dev with Postgres still needs a real key
        Settings(_env_file=None, env="dev", database_url="postgresql://x").master_key_bytes()
    assert Settings(_env_file=None, env="dev", database_url="sqlite://").master_key_bytes() == b"\0" * 32


def _start_login(client, owner):
    url = client.post("/v1/broker/kite/login-url", headers=owner["h"]).json()["url"]
    return parse_qs(parse_qs(urlparse(url).query)["redirect_params"][0])["state"][0]


def test_kite_login_for_another_zerodha_account_is_refused(client, ctx, owner, net):
    client.put("/v1/broker/kite", json=dict(KEYS, client_id="ZZ9999"), headers=owner["h"])
    state = _start_login(client, owner)
    r = client.get("/kite/callback", params={"state": state, "request_token": "RT", "status": "success"})
    assert r.headers["location"].endswith("status=wrong_account")
    assert client.get("/v1/broker/kite", headers=owner["h"]).json()["connected_today"] is False


def test_secrets_are_bound_to_their_owner(client, ctx, owner):
    client.put("/v1/broker/kite", json=KEYS, headers=owner["h"])
    from tdserver.models import BrokerAccount
    with ctx.sessions() as db:
        acc = db.query(BrokerAccount).one()
        from cryptography.exceptions import InvalidTag
        with pytest.raises(InvalidTag):
            ctx.vault.open(acc.data_key_enc, acc.api_key_enc, f"kite:api_key:user:{acc.user_id + 1}")


def test_console_link_goes_to_telegram_only(client, owner, net):
    topic = client.post("/v1/alerts/channels/ntfy-topic", headers=owner["h"]).json()["topic"]
    client.put("/v1/alerts/channels", json={"kind": "ntfy", "target": topic}, headers=owner["h"])
    client.post("/console/login", data={"name": "Hamid"})
    assert net.bodies("ntfy.example") == []


def test_console_banner_only_shows_known_results(client, owner, net):
    from test_console import _console_login
    _console_login(client, owner, net)
    page = client.get("/console?kite=<b>Call+this+number</b>").text
    assert "Call this number" not in page and "Signed in as" in page


def test_viewer_cannot_connect_a_broker(client, ctx):
    p = Phone()
    enroll(client, ctx, p, role="viewer", name="Viewer")
    h = {"Authorization": f"Bearer {sign_in(client, p)}"}
    assert client.put("/v1/broker/kite", json=KEYS, headers=h).status_code == 403


def test_naive_install_date_is_taken_as_utc(client, owner):
    sign_in(client, owner["phone"], install_expires_at="2026-11-08T09:00:00")
    assert client.get("/v1/devices", headers=owner["h"]).json()[0]["install_days_left"] == 5
    r = client.patch("/v1/devices/this", json={"install_expires_at": "2026-11-09T09:00:00"}, headers=owner["h"])
    assert r.status_code == 200


def test_used_invite_cannot_be_claimed_again(client, ctx):
    with ctx.sessions() as db:
        code = new_invite(db, ctx, "A", "trader", None)
        db.commit()
    for i in range(2):
        p = Phone()
        r = client.post("/v1/auth/enroll", json={"invite_code": code, "device_name": "x",
                                                "public_key": base64.b64encode(p.pub).decode(),
                                                "signature": p.sign(devicekeys.enroll_message(code))})
        assert r.status_code == (200 if i == 0 else 400)


def test_one_bad_alert_does_not_resend_the_others(client, ctx, owner, net):
    client.put("/v1/alerts/channels", json={"kind": "telegram", "target": "123456789"}, headers=owner["h"])
    for _ in range(3):
        client.post("/v1/alerts/test?level=important", headers=owner["h"])
    with ctx.sessions() as db:   # break the middle alert's level so sending it raises
        db.query(Alert).filter(Alert.id == 2).update({"level": "bogus"})
        db.commit()
    tick(ctx)
    tick(ctx)
    assert len(net.bodies("sendMessage")) == 2      # alerts 1 and 3 once each, never repeated
    with ctx.sessions() as db:
        assert db.query(AlertDelivery).filter_by(alert_id=2, ok=False).count() == 1


def test_telegram_code_is_one_time(client, ctx, owner, net):
    url = client.post("/v1/alerts/channels/telegram-link", headers=owner["h"]).json()["url"]
    code = url.split("start=")[1]
    net.tg_updates = [{"update_id": 1, "message": {"text": f"/start {code}", "chat": {"id": 555000111, "type": "private"}}},
                      {"update_id": 2, "message": {"text": f"/start {code}", "chat": {"id": 666, "type": "private"}}}]
    assert client.post("/v1/alerts/channels/telegram-link/check", headers=owner["h"]).json()["ok"] is True
    assert client.get("/v1/alerts/channels", headers=owner["h"]).json()["telegram"]["target"].endswith("111")
    # a later replay of the same code (by anyone) changes nothing
    net.tg_updates = [{"update_id": 3, "message": {"text": f"/start {code}", "chat": {"id": 777, "type": "private"}}}]
    assert client.post("/v1/alerts/channels/telegram-link/check", headers=owner["h"]).json()["ok"] is False


def test_ipv6_grouped_by_64():
    assert client_key("2001:db8:1:2:aaaa::1") == client_key("2001:db8:1:2:bbbb::9")
    assert client_key("2001:db8:1:3::1") != client_key("2001:db8:1:2::1")
    assert client_key("10.0.0.1") == "10.0.0.1" == client_key("::ffff:10.0.0.1")


def test_hsts_in_production(ctx):
    from fastapi.testclient import TestClient

    from tdserver.app import create_app
    ctx.settings.env = "prod"
    r = TestClient(create_app(ctx), base_url="https://td.example").get("/v1/health")
    assert "max-age" in r.headers.get("strict-transport-security", "")
