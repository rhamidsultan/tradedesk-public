"""Test fixtures: the whole app on an in-memory database, a movable clock, and fake Zerodha/Telegram/ntfy."""
from __future__ import annotations

import base64
import json
from datetime import datetime

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from tdserver import devicekeys
from tdserver.app import create_app
from tdserver.clock import IST, FixedClock
from tdserver.context import build_context
from tdserver.db import Base, make_sessionmaker
from tdserver.routes_auth import new_invite
from tdserver.settings import Settings


class FakeNet:
    """Records outgoing HTTP calls and answers like Zerodha, Telegram and ntfy."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.kite_ok = True
        self.down = False
        self.tg_updates: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("network down", request=req)
        self.calls.append(req)
        url = str(req.url)
        if url.endswith("/session/token"):
            if not self.kite_ok:
                return httpx.Response(403, json={"status": "error", "message": "Token is invalid or has expired."})
            return httpx.Response(200, json={"status": "success", "data": {"access_token": "ACCESS-123",
                                                                           "user_id": "AB1234"}})
        if url.endswith("/user/profile"):
            return httpx.Response(200, json={"data": {"user_shortname": "Hamid"}})
        if url.split("?")[0].endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "TradeDeskBot"}})
        if url.split("?")[0].endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": self.tg_updates})
        if "api.telegram.org" in url or "ntfy" in url:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    def bodies(self, host_part: str) -> list[dict]:
        out = []
        for r in self.calls:
            if host_part in str(r.url):
                try:
                    out.append(json.loads(r.content or b"{}"))
                except ValueError:
                    out.append({"_raw": r.content.decode()})
        return out


class Phone:
    """An enrolled iPhone: holds a P-256 private key like the Secure Enclave would."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pub = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                      serialization.PublicFormat.UncompressedPoint)
        self.key_id = devicekeys.key_id(self.pub)

    def sign(self, msg: bytes) -> str:
        return base64.b64encode(self.key.sign(msg, ec.ECDSA(hashes.SHA256()))).decode()


@pytest.fixture
def clock():
    # Tuesday 3 Nov 2026, 10:00 IST: a trading day, market open
    return FixedClock(datetime(2026, 11, 3, 10, 0, tzinfo=IST))


@pytest.fixture
def net():
    return FakeNet()


@pytest.fixture
def ctx(clock, net, monkeypatch):
    settings = Settings(env="dev", database_url="sqlite://", public_base_url="https://td.example",
                        telegram_bot_token="BOT:TOKEN", ntfy_server="https://ntfy.example",
                        master_key=base64.b64encode(b"k" * 32).decode(), health_alerts=False)
    c = build_context(settings, clock=clock, http=httpx.Client(transport=httpx.MockTransport(net.handler)))
    # one shared in-memory database for every connection
    from sqlalchemy import create_engine
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    import tdserver.models  # noqa: F401
    Base.metadata.create_all(engine)
    c.sessions = make_sessionmaker(engine)
    return c


@pytest.fixture
def client(ctx):
    return TestClient(create_app(ctx), base_url="https://td.example", follow_redirects=False)


def enroll(client, ctx, phone: Phone, role="owner", name="Hamid", for_user_id=None, device_name="iPhone"):
    with ctx.sessions() as db:
        code = new_invite(db, ctx, name, role, None, for_user_id=for_user_id)
        db.commit()
    r = client.post("/v1/auth/enroll", json={
        "invite_code": code, "device_name": device_name,
        "public_key": base64.b64encode(phone.pub).decode(),
        "signature": phone.sign(devicekeys.enroll_message(code))})
    assert r.status_code == 200, r.text
    return r.json()


def sign_in(client, phone: Phone, **extra) -> str:
    ch = client.post("/v1/auth/challenge", json={"key_id": phone.key_id}).json()
    msg = devicekeys.login_message(ch["challenge_id"], base64.b64decode(ch["nonce"]))
    r = client.post("/v1/auth/login", json={"challenge_id": ch["challenge_id"], "signature": phone.sign(msg), **extra})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture
def owner(client, ctx):
    phone = Phone()
    enroll(client, ctx, phone)
    token = sign_in(client, phone)
    return {"phone": phone, "token": token, "h": {"Authorization": f"Bearer {token}"}}
