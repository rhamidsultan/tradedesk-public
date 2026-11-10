"""ShadowBroker: real Zerodha prices, simulated orders. It implements the Broker protocol but never sends an order.

Shadow mode runs the full OrderManager (guards, protective SLs, chasers, reconciliation, kill switch) against this
book, so the order logic is exercised on live prices before any real order. Fills are simulated against the touch:
  - a LIMIT BUY fills at the ask when ask <= limit; a LIMIT SELL at the bid when bid >= limit;
  - an SL order waits until the last price crosses its trigger, then works as a LIMIT at its limit price.
The book is saved to a JSON file after every change, so a restart reconciles against the same shadow positions.
"""
from __future__ import annotations

import itertools
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable

from .broker import TERMINAL, OrderReq, OrderState, Quote, Rejected


class ShadowBroker:
    def __init__(self, quotes: Callable[[list[str], str], dict[str, Quote]], path: Path | str,
                 exchange_of: Callable[[str], str] | None = None):
        self._quotes = quotes                       # read-only price source (Kite /quote with the user's login)
        self.path = Path(path)
        self.exchange_of = exchange_of or (lambda s: "BFO" if s.startswith("SENSEX") else "NFO")
        self.book: dict[str, OrderState] = {}
        self.pos: dict[str, int] = {}
        start = 1
        if self.path.exists():
            d = json.loads(self.path.read_text())
            self.book = {k: OrderState(**v) for k, v in d.get("book", {}).items()}
            self.pos = {k: int(v) for k, v in d.get("pos", {}).items()}
            start = int(d.get("next", 1))
        self._ids = itertools.count(start)

    def __repr__(self):
        return "ShadowBroker(simulated, never sends orders)"

    def _save(self):
        nxt = next(self._ids)
        self._ids = itertools.count(nxt)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"book": {k: asdict(v) for k, v in self.book.items()}, "pos": self.pos, "next": nxt}))
        tmp.replace(self.path)

    # ------------------------------------------------------------------ Broker protocol
    def place(self, req: OrderReq) -> str:
        oid = f"SH{next(self._ids)}"
        st = "TRIGGER PENDING" if req.order_type == "SL" else "OPEN"
        self.book[oid] = OrderState(oid, req.tag, req.symbol, req.side, req.qty, req.order_type, req.price, req.trigger, st)
        self._save()
        return oid

    def modify(self, order_id, *, price=None, trigger=None, order_type=None, qty=None):
        o = self.book.get(order_id)
        if o is None or o.status in TERMINAL:
            raise Rejected("order already " + (o.status.lower() if o else "gone"))
        if price is not None: o.price = price
        if trigger is not None: o.trigger = trigger
        if qty is not None: o.qty = qty
        if order_type is not None:
            o.order_type = order_type
            if order_type == "LIMIT":
                o.trigger, o.status = None, "OPEN"
        self._save()

    def cancel(self, order_id):
        o = self.book.get(order_id)
        if o is None or o.status in TERMINAL:
            raise Rejected("order already " + (o.status.lower() if o else "gone"))
        o.status = "CANCELLED"
        self._save()

    def orders(self) -> list[OrderState]:
        self._match()
        return [replace(o) for o in self.book.values()]

    def positions(self) -> dict[str, int]:
        return {k: v for k, v in self.pos.items() if v}

    def quote(self, symbols: list[str], exchange: str = "NFO") -> dict[str, Quote]:
        return self._quotes(symbols, exchange)

    # ------------------------------------------------------------------ simulation
    def _match(self):
        working = [o for o in self.book.values() if o.status not in TERMINAL]
        if not working:
            return
        by_ex: dict[str, list[str]] = {}
        for o in working:
            by_ex.setdefault(self.exchange_of(o.symbol), []).append(o.symbol)
        q: dict[str, Quote] = {}
        for ex, syms in by_ex.items():
            try:
                q.update(self._quotes(sorted(set(syms)), ex))
            except Exception:                        # no price: nothing fills this round
                continue
        changed = False
        for o in working:
            x = q.get(o.symbol)
            if x is None:
                continue
            if o.status == "TRIGGER PENDING":
                crossed = x.ltp and ((o.side == "BUY" and x.ltp >= o.trigger) or (o.side == "SELL" and x.ltp <= o.trigger))
                if not crossed:
                    continue
                o.status = "OPEN"
                changed = True
            touch = x.ask if o.side == "BUY" else x.bid
            if touch and ((o.side == "BUY" and touch <= o.price) or (o.side == "SELL" and touch >= o.price)):
                q_ = o.qty - o.filled
                o.avg = round((o.avg * o.filled + touch * q_) / o.qty, 2)
                o.filled = o.qty
                o.status = "COMPLETE"
                self.pos[o.symbol] = self.pos.get(o.symbol, 0) + (q_ if o.side == "BUY" else -q_)
                changed = True
        if changed:
            self._save()
