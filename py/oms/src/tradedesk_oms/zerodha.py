"""ZerodhaAdapter: the only code that sends real orders. Constructed by live mode on the server only.

Kite Connect v3 REST. LIMIT and SL orders only (OrderReq refuses MARKET/SL-M). Every order carries our tag.
Errors map to the taxonomy in broker.py; a POST whose reply is lost raises NetworkUnknown (resolve by tag).
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from .broker import (BrokerDown, InputInvalid, MarginInsufficient, NetworkUnknown, OrderReq, OrderState, Quote,
                     RateLimited, Rejected, TokenExpired)

ROOT = "https://api.kite.trade"
_TERMINAL_OR_KNOWN = {"OPEN", "COMPLETE", "CANCELLED", "REJECTED", "TRIGGER PENDING"}


class ZerodhaAdapter:
    def __init__(self, api_key: str, access_token: str, timeout: float = 7.0):
        self._auth = f"token {api_key}:{access_token}"       # never logged
        self.timeout = timeout

    def __repr__(self):
        return "ZerodhaAdapter(<credentials hidden>)"

    def _req(self, method: str, path: str, params: dict | None = None):
        data = None
        url = ROOT + path
        if params is not None and method == "GET" and not isinstance(params, dict):
            url += "?" + urllib.parse.urlencode(params)
        elif params and method == "GET":
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        elif params:
            data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        r = urllib.request.Request(url, data=data, method=method,
                                   headers={"X-Kite-Version": "3", "Authorization": self._auth})
        try:
            with urllib.request.urlopen(r, timeout=self.timeout) as resp:
                return json.loads(resp.read())["data"]
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
            except Exception:
                body = {}
            et, msg = body.get("error_type", ""), body.get("message", str(e.code))
            if e.code == 429:
                raise RateLimited(msg)
            if e.code == 403 or et == "TokenException":
                raise TokenExpired(msg)
            if et == "MarginException":
                raise MarginInsufficient(msg)
            if et in ("OrderException",):
                raise Rejected(msg)
            if et == "InputException" or e.code == 400:
                raise InputInvalid(msg)
            if method != "GET" and e.code >= 500:
                raise NetworkUnknown(msg)
            raise BrokerDown(msg)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            if method != "GET":
                raise NetworkUnknown(type(e).__name__)
            raise BrokerDown(type(e).__name__)

    def place(self, req: OrderReq) -> str:
        d = self._req("POST", "/orders/regular", {
            "tradingsymbol": req.symbol, "exchange": req.exchange, "transaction_type": req.side,
            "order_type": req.order_type, "quantity": req.qty, "product": req.product, "validity": "DAY",
            "price": f"{req.price:.2f}", "trigger_price": f"{req.trigger:.2f}" if req.trigger else None, "tag": req.tag})
        return str(d["order_id"])

    def modify(self, order_id, *, price=None, trigger=None, order_type=None, qty=None):
        if order_type not in (None, "LIMIT", "SL"):
            raise InputInvalid("order type not allowed")
        self._req("PUT", f"/orders/regular/{order_id}", {
            "price": f"{price:.2f}" if price is not None else None, "trigger_price": f"{trigger:.2f}" if trigger else None,
            "order_type": order_type, "quantity": qty})

    def cancel(self, order_id):
        self._req("DELETE", f"/orders/regular/{order_id}")

    def orders(self) -> list[OrderState]:
        out = []
        for o in self._req("GET", "/orders"):
            st = o["status"]
            if st not in _TERMINAL_OR_KNOWN:          # transient states (VALIDATION PENDING, MODIFY PENDING …)
                st = "TRIGGER PENDING" if o["order_type"] == "SL" and not o.get("filled_quantity") else "PENDING"
            out.append(OrderState(str(o["order_id"]), o.get("tag") or "", o["tradingsymbol"], o["transaction_type"],
                                  int(o["quantity"]), o["order_type"], float(o.get("price") or 0),
                                  float(o["trigger_price"]) if o.get("trigger_price") else None, st,
                                  int(o.get("filled_quantity") or 0), float(o.get("average_price") or 0),
                                  (o.get("status_message") or "")[:200]))
        return out

    def positions(self) -> dict[str, int]:
        net = self._req("GET", "/portfolio/positions")["net"]
        out: dict[str, int] = {}
        for p in net:
            if p["exchange"] in ("NFO", "BFO") and int(p["quantity"]):
                out[p["tradingsymbol"]] = out.get(p["tradingsymbol"], 0) + int(p["quantity"])
        return out

    def quote(self, symbols: list[str], exchange: str = "NFO") -> dict[str, Quote]:
        keys = [s if ":" in s else f"{exchange}:{s}" for s in symbols]
        d = self._req("GET", "/quote", [("i", k) for k in keys])
        out = {}
        for k, v in d.items():
            dep = v.get("depth") or {}
            b = (dep.get("buy") or [{}])[0].get("price") or None
            a = (dep.get("sell") or [{}])[0].get("price") or None
            out[k.split(":", 1)[1]] = Quote(b, a, float(v.get("last_price") or 0))
        return out
