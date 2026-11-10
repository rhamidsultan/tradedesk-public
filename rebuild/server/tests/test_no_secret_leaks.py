"""Secrets never reach logs, responses or the audit log; there is no order code in phase 1."""
import logging
import pathlib
import re

from test_kite import KEYS, _login

from tdserver.models import AuditLog

PKG = pathlib.Path(__file__).parents[1] / "tdserver"


def test_secrets_not_in_responses_logs_or_audit(client, ctx, owner, net, caplog):
    caplog.set_level(logging.DEBUG)
    _login(client, owner, net)
    seen = caplog.text
    for path in ("/v1/status", "/v1/broker/kite", "/v1/devices", "/v1/me", "/v1/alerts"):
        seen += client.get(path, headers=owner["h"]).text
    with ctx.sessions() as db:
        seen += " ".join(f"{a.action}{a.target}{a.detail}" for a in db.query(AuditLog))
    for secret in (KEYS["api_key"], KEYS["api_secret"], "ACCESS-123", "BOT:TOKEN"):
        assert secret not in seen


def test_no_order_calls_in_the_server_package():
    """tdserver never calls a broker order endpoint itself: real orders go only through tradedesk_oms (its
    ZerodhaAdapter), which only tdserver.live imports."""
    src = "\n".join(p.read_text() for p in PKG.rglob("*.py"))
    for pattern in (r"/orders", r"place_order", r"modify_order", r"cancel_order", r"/gtt"):
        assert not re.search(pattern, src), pattern
    for p in PKG.rglob("*.py"):
        if "ZerodhaAdapter" in p.read_text():
            assert p.parent.name == "live", f"{p} constructs the real order adapter"


def test_server_error_hides_details(ctx):
    from fastapi.testclient import TestClient

    from tdserver.app import create_app
    app = create_app(ctx)

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret-detail")

    r = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert r.status_code == 500 and "secret-detail" not in r.text
