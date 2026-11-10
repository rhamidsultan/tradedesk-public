"""Read-only Kite connection that reuses the portfolio app's key and its morning login (like Algo Lab).

TradeDesk never logs in to Kite and never places orders in this build. It reads:
  TD_KITE_ENV    a .env file with KITE_API_KEY (and KITE_API_SECRET, unused here)
  TD_KITE_TOKEN  the portfolio app's saved login (JSON, .env-style or plain text holding an access token)
The token is never logged or sent to the phone.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
_cache: dict = {}


def _env(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _token(path: Path) -> str | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(text)
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                for k, v in x.items():
                    if isinstance(v, str) and "access_token" in k.lower() and v:
                        return v
                    stack.append(v)
            elif isinstance(x, list):
                stack.extend(x)
    except ValueError:
        pass
    m = re.search(r"access_token[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9]{16,})", text, re.I)
    return m.group(1) if m else None


def _creds() -> tuple[str | None, str | None, Path | None]:
    envf, tokf = os.environ.get("TD_KITE_ENV"), os.environ.get("TD_KITE_TOKEN")
    key = os.environ.get("KITE_API_KEY") or (_env(Path(envf)).get("KITE_API_KEY") if envf else None)
    tp = Path(tokf) if tokf else None
    return key, (_token(tp) if tp else None), tp


def _get(path: str, key: str, token: str, params=None) -> dict:
    url = "https://api.kite.trade" + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, headers={"X-Kite-Version": "3", "Authorization": f"token {key}:{token}"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def status() -> dict:
    """{configured, logged_in_today, user, checked, message}; cached 60 s."""
    if _cache.get("status") and time.time() - _cache["status"][0] < 60:
        return _cache["status"][1]
    key, token, tp = _creds()
    out = {"configured": bool(key and tp), "logged_in_today": False, "user": None,
           "checked": datetime.now(IST).strftime("%H:%M"), "message": ""}
    if not key or not tp:
        out["message"] = "Kite not configured on this machine (research only)."
    elif not token:
        out["message"] = "No Kite login found. Log in on the portfolio dashboard this morning."
    else:
        try:
            prof = _get("/user/profile", key, token)["data"]
            out.update(logged_in_today=True, user=prof.get("user_shortname") or prof.get("user_id"),
                       message="Kite login valid (shared with the portfolio dashboard).")
        except Exception as e:
            code = getattr(e, "code", None)
            out["message"] = ("Kite login expired — log in on the portfolio dashboard." if code in (401, 403)
                              else f"Kite not reachable ({type(e).__name__}).")
    _cache["status"] = (time.time(), out)
    return out


def market() -> dict:
    """NIFTY 50, BANKNIFTY, SENSEX and India VIX last prices; cached 5 s."""
    if _cache.get("market") and time.time() - _cache["market"][0] < 5:
        return _cache["market"][1]
    key, token, _ = _creds()
    if not (key and token):
        return {"ok": False}
    names = {"NSE:NIFTY 50": "NIFTY", "NSE:NIFTY BANK": "BANKNIFTY", "BSE:SENSEX": "SENSEX", "NSE:INDIA VIX": "INDIAVIX"}
    try:
        d = _get("/quote/ohlc", key, token, [("i", k) for k in names])["data"]
        out = {"ok": True, "at": datetime.now(IST).strftime("%H:%M:%S"),
               "quotes": {names[k]: {"ltp": v["last_price"], "prev_close": v["ohlc"]["close"]} for k, v in d.items() if k in names}}
    except Exception as e:
        out = {"ok": False, "error": type(e).__name__}
    _cache["market"] = (time.time(), out)
    return out
