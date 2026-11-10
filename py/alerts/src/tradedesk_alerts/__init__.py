"""Alerts for TradeDesk. Levels: info (push), important (push + email), critical (push + WhatsApp + email).

Settings live on the server, never in the app:
  $TD_HOME/alerts.env   WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, WHATSAPP_TO (e.g. 9198xxxxxxxx), WHATSAPP_TEMPLATE
                        (an approved template with one body variable; default "tradedesk_alert"), WHATSAPP_LANG (en),
                        SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS (Gmail app password), EMAIL_TO
  $TD_HOME/vapid.pem    web-push key (created on first use); push-subs.json  subscribed devices
Every alert is appended to $TD_HOME/alerts.log.jsonl (without secrets). Sending never raises.
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
LEVELS = {"info": ("push",), "important": ("push", "email"), "critical": ("push", "whatsapp", "email")}


def home() -> Path:
    h = Path(os.environ.get("TD_HOME", "~/Trading/tradedesk-home")).expanduser()
    h.mkdir(parents=True, exist_ok=True)
    return h


def settings() -> dict:
    out = {}
    f = home() / "alerts.env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def channels() -> dict:
    s = settings()
    return {"push": len(_subs()), "whatsapp": all(s.get(k) for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID", "WHATSAPP_TO")),
            "email": all(s.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_TO"))}


# ------------------------------------------------------------------ web push
def _vapid():
    from py_vapid import Vapid01
    f = home() / "vapid.pem"
    if not f.exists():
        v = Vapid01()
        v.generate_keys()
        v.save_key(str(f))
        f.chmod(0o600)
    return Vapid01.from_file(str(f))


def public_key() -> str:
    import base64
    from cryptography.hazmat.primitives import serialization
    raw = _vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _subs() -> list:
    f = home() / "push-subs.json"
    return json.loads(f.read_text()) if f.exists() else []


def add_subscription(sub: dict, name: str = "device") -> None:
    subs = [s for s in _subs() if s["sub"].get("endpoint") != sub.get("endpoint")]
    subs.append({"name": name[:40], "sub": sub, "added": datetime.now(IST).strftime("%Y-%m-%d %H:%M")})
    f = home() / "push-subs.json"
    f.write_text(json.dumps(subs))
    f.chmod(0o600)


def _push(title: str, body: str, url: str) -> str:
    from pywebpush import WebPushException, webpush
    subs, keep, sent = _subs(), [], 0
    for s in subs:
        try:
            webpush(s["sub"], json.dumps({"title": title, "body": body, "url": url}),
                    vapid_private_key=str(home() / "vapid.pem"), vapid_claims={"sub": "mailto:" + (settings().get("EMAIL_TO") or "owner@tradedesk.local")},
                    ttl=3600)
            sent += 1
            keep.append(s)
        except WebPushException as e:
            if getattr(e.response, "status_code", None) not in (404, 410):     # gone devices are dropped
                keep.append(s)
    if len(keep) != len(subs):
        (home() / "push-subs.json").write_text(json.dumps(keep))
    return f"push {sent}/{len(subs)}"


# ------------------------------------------------------------------ WhatsApp (Meta Cloud API, approved template)
def _whatsapp(text: str) -> str:
    s = settings()
    body = {"messaging_product": "whatsapp", "to": s["WHATSAPP_TO"], "type": "template",
            "template": {"name": s.get("WHATSAPP_TEMPLATE", "tradedesk_alert"), "language": {"code": s.get("WHATSAPP_LANG", "en")},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": text[:900]}]}]}}
    req = urllib.request.Request(f"https://graph.facebook.com/v21.0/{s['WHATSAPP_PHONE_ID']}/messages",
                                 data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {s['WHATSAPP_TOKEN']}",
                                                                           "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return f"whatsapp {r.status}"


def _email(title: str, body: str) -> str:
    s = settings()
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = f"TradeDesk: {title}", s["SMTP_USER"], s["EMAIL_TO"]
    m.set_content(body)
    with smtplib.SMTP(s["SMTP_HOST"], int(s.get("SMTP_PORT", 587)), timeout=20) as c:
        c.starttls(context=ssl.create_default_context())
        c.login(s["SMTP_USER"], s["SMTP_PASS"])
        c.send_message(m)
    return "email ok"


def send(level: str, title: str, body: str, url: str = "/#/trade") -> dict:
    ch = channels()
    results = {}
    for c in LEVELS.get(level, ("push",)):
        if not ch.get(c):
            results[c] = "not set up"
            continue
        try:
            results[c] = {"push": lambda: _push(title, body, url), "whatsapp": lambda: _whatsapp(f"{title} — {body}"),
                          "email": lambda: _email(title, body)}[c]()
        except Exception as e:
            results[c] = f"failed: {type(e).__name__}"
    rec = {"at": datetime.now(IST).isoformat(timespec="seconds"), "level": level, "title": title, "body": body, "result": results}
    with open(home() / "alerts.log.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def recent(n: int = 30) -> list:
    f = home() / "alerts.log.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()[-n:]][::-1] if f.exists() else []
