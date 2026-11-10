"""Broker protocol, error taxonomy and the scriptable FakeBroker."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from typing import Protocol


class BrokerError(Exception): ...
class Rejected(BrokerError): ...
class InputInvalid(Rejected): ...
class MarginInsufficient(Rejected): ...
class TokenExpired(BrokerError): ...
class RateLimited(BrokerError): ...
class BrokerDown(BrokerError): ...
class NetworkUnknown(BrokerError):
    """The request may or may not have reached the broker. Resolve by tag before any retry."""


TERMINAL = {"COMPLETE", "CANCELLED", "REJECTED"}


@dataclass
class OrderReq:
    tag: str                 # ≤ 20 alphanumerics, unique per day
    symbol: str              # broker trading symbol
    exchange: str            # NFO / BFO
    side: str                # BUY / SELL
    qty: int
    order_type: str          # LIMIT / SL   (never MARKET or SL-M on options)
    price: float
    trigger: float | None = None
    product: str = "NRML"

    def __post_init__(self):
        if self.order_type not in ("LIMIT", "SL"):
            raise InputInvalid(f"order type {self.order_type} not allowed")
        if self.order_type == "SL" and not self.trigger:
            raise InputInvalid("SL order needs a trigger")
        if not (1 <= len(self.tag) <= 20 and self.tag.isalnum()):
            raise InputInvalid("bad tag")
        if self.qty <= 0 or self.price <= 0:
            raise InputInvalid("qty and price must be positive")


@dataclass
class OrderState:
    order_id: str
    tag: str
    symbol: str
    side: str
    qty: int
    order_type: str
    price: float
    trigger: float | None
    status: str              # OPEN, TRIGGER PENDING, COMPLETE, CANCELLED, REJECTED
    filled: int = 0
    avg: float = 0.0
    message: str = ""


@dataclass
class Quote:
    bid: float | None
    ask: float | None
    ltp: float


class Broker(Protocol):
    def place(self, req: OrderReq) -> str: ...
    def modify(self, order_id: str, *, price: float | None = None, trigger: float | None = None,
               order_type: str | None = None, qty: int | None = None) -> None: ...
    def cancel(self, order_id: str) -> None: ...
    def orders(self) -> list[OrderState]: ...
    def positions(self) -> dict[str, int]: ...          # net qty per symbol (F&O, NRML+MIS)
    def quote(self, symbols: list[str], exchange: str = "NFO") -> dict[str, Quote]: ...


@dataclass
class FakeBroker:
    """In-memory broker for tests. Script it: set quotes, call fill()/trigger(), inject errors in `fail_next`."""
    book: dict = field(default_factory=dict)
    pos: dict = field(default_factory=dict)
    quotes: dict = field(default_factory=dict)
    fail_next: list = field(default_factory=list)      # exceptions raised by the next place/modify/cancel calls
    lost_next: int = 0                                 # place succeeds at the broker but the reply is lost
    calls: list = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1001))

    def _maybe_fail(self):
        if self.fail_next:
            e = self.fail_next.pop(0)
            if e is not None:
                raise e

    def place(self, req: OrderReq) -> str:
        self.calls.append(("place", req.tag, req.side, req.qty, req.order_type, req.price, req.trigger))
        self._maybe_fail()
        oid = str(next(self._ids))
        st = "TRIGGER PENDING" if req.order_type == "SL" else "OPEN"
        self.book[oid] = OrderState(oid, req.tag, req.symbol, req.side, req.qty, req.order_type, req.price, req.trigger, st)
        if self.lost_next:
            self.lost_next -= 1
            raise NetworkUnknown("timeout")
        return oid

    def modify(self, order_id, *, price=None, trigger=None, order_type=None, qty=None):
        self.calls.append(("modify", order_id, price, trigger, order_type, qty))
        self._maybe_fail()
        o = self.book[order_id]
        if o.status in TERMINAL:
            raise Rejected("order already " + o.status.lower())
        if price is not None: o.price = price
        if trigger is not None: o.trigger = trigger
        if qty is not None: o.qty = qty
        if order_type is not None:
            o.order_type = order_type
            if order_type == "LIMIT":
                o.trigger, o.status = None, "OPEN"

    def cancel(self, order_id):
        self.calls.append(("cancel", order_id))
        self._maybe_fail()
        o = self.book[order_id]
        if o.status in TERMINAL:
            raise Rejected("order already " + o.status.lower())
        o.status = "CANCELLED"

    def orders(self):
        return [replace(o) for o in self.book.values()]

    def positions(self):
        return {k: v for k, v in self.pos.items() if v}

    def quote(self, symbols, exchange="NFO"):
        self.calls.append(("quote", exchange))
        return {s: self.quotes.get(s, Quote(None, None, 0.0)) for s in symbols}

    # --- scripting helpers
    def fill(self, order_id: str, qty: int | None = None, price: float | None = None):
        o = self.book[order_id]
        q = o.qty - o.filled if qty is None else qty
        px = o.price if price is None else price
        o.avg = round((o.avg * o.filled + px * q) / (o.filled + q), 2)
        o.filled += q
        o.status = "COMPLETE" if o.filled >= o.qty else "OPEN"
        self.pos[o.symbol] = self.pos.get(o.symbol, 0) + (q if o.side == "BUY" else -q)

    def trigger(self, order_id: str):
        o = self.book[order_id]
        o.status = "OPEN"

    def reject(self, order_id: str, msg="RMS: margin exceeds"):
        o = self.book[order_id]
        o.status, o.message = "REJECTED", msg

    def by_tag(self, tag):
        return next(o for o in self.book.values() if o.tag == tag)
