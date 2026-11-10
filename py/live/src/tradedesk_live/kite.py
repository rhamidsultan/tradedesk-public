"""Read-only Kite access: credentials shared with the portfolio app, instrument list, WebSocket packet parsing.

Nothing here can place, modify or cancel an order (only GET requests and the market-data WebSocket).
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import struct
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

INDEX_TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105, "INDIAVIX": 264969, "SENSEX": 265, "FINNIFTY": 257801}
OPT_EXCHANGE = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO", "SENSEX": "BFO"}


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


def read_token(path: Path) -> str | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        stack = [json.loads(text)]
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


def credentials() -> tuple[str | None, str | None]:
    envf, tokf = os.environ.get("TD_KITE_ENV"), os.environ.get("TD_KITE_TOKEN")
    key = os.environ.get("KITE_API_KEY") or (_env(Path(envf)).get("KITE_API_KEY") if envf else None)
    return key, (read_token(Path(tokf)) if tokf else None)


def get(path: str, key: str, token: str, params=None, raw=False):
    url = "https://api.kite.trade" + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, headers={"X-Kite-Version": "3", "Authorization": f"token {key}:{token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    return body if raw else json.loads(body)


@dataclass
class Instrument:
    token: int
    symbol: str          # TradeDesk symbol, e.g. NIFTY26102725000CE
    kite_symbol: str
    underlying: str
    expiry: date | None
    strike: float | None
    opt_type: str | None
    lot_size: int


def td_symbol(u: str, expiry: date, strike: float, typ: str) -> str:
    return f"{u}{expiry:%y%m%d}{int(strike)}{typ}"


def parse_instruments(csv_bytes: bytes, underlying: str) -> list[Instrument]:
    out = []
    for r in csv.DictReader(io.StringIO(csv_bytes.decode())):
        if r["name"] != underlying or r["instrument_type"] not in ("CE", "PE"):
            continue
        exp = date.fromisoformat(r["expiry"])
        k = float(r["strike"])
        out.append(Instrument(int(r["instrument_token"]), td_symbol(underlying, exp, k, r["instrument_type"]),
                              r["tradingsymbol"], underlying, exp, k, r["instrument_type"], int(float(r["lot_size"]))))
    return out


@dataclass
class Tick:
    token: int
    ltp: float
    volume: int | None = None
    bid: float | None = None
    ask: float | None = None


def parse_binary(msg: bytes) -> list[Tick]:
    """Kite WebSocket binary frame -> ticks. Prices are in paise for NSE/BSE segments."""
    if len(msg) < 2:
        return []                                   # heartbeat
    n = struct.unpack(">H", msg[:2])[0]
    i, ticks = 2, []
    for _ in range(n):
        if i + 2 > len(msg):
            break
        ln = struct.unpack(">H", msg[i:i + 2])[0]
        p = msg[i + 2:i + 2 + ln]
        i += 2 + ln
        if len(p) < 8:
            continue
        tok, ltp = struct.unpack(">iI", p[:8])
        t = Tick(tok, ltp / 100.0)
        if (tok & 0xFF) == 9 and ln in (28, 32):    # index packet: no volume/depth
            pass
        elif ln >= 44:
            t.volume = struct.unpack(">I", p[16:20])[0]
            if ln >= 184:                           # full mode: 5 bids then 5 asks, 12 bytes each, from byte 64
                bq, bp = struct.unpack(">iI", p[64:72])
                aq, ap = struct.unpack(">iI", p[124:132])
                t.bid = bp / 100.0 if bq else None
                t.ask = ap / 100.0 if aq else None
        ticks.append(t)
    return ticks


def margins_basket(key: str, token: str, orders: list[dict]) -> dict:
    """Zerodha's margin calculator for a basket (read-only: computes margin, places nothing)."""
    req = urllib.request.Request("https://api.kite.trade/margins/basket?consider_positions=false&mode=compact",
                                 data=json.dumps(orders).encode(),
                                 headers={"X-Kite-Version": "3", "Authorization": f"token {key}:{token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["data"]
