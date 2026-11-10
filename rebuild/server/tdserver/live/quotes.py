"""One quote gate per user: every Zerodha /quote call for that user's live and shadow desks goes through it.

Kite allows about 1 quote request a second per app. The gate batches every symbol asked for (plus the open legs
it is told to watch) into one request, at most once every MIN_GAP seconds, and serves answers younger than
MAX_AGE from its cache. A price older than STALE is never returned (the caller then treats it as unknown).
"""
from __future__ import annotations

import time as _time
from typing import Callable

MAX_AGE = 2.0
MIN_GAP = 1.05
STALE = 10.0


class QuoteGate:
    def __init__(self, fetch: Callable, clock: Callable[[], float] = _time.time, sleep: Callable[[float], None] = _time.sleep):
        self.fetch = fetch                         # (symbols, exchange) -> {symbol: Quote}; read-only
        self.clock, self.sleep = clock, sleep
        self.cache: dict[tuple[str, str], tuple[float, object]] = {}
        self.watch: dict[str, set[str]] = {}
        self.last = 0.0

    def set_watch(self, symbols_by_exchange: dict[str, set[str]]) -> None:
        self.watch = {k: set(v) for k, v in symbols_by_exchange.items()}

    def quote(self, symbols: list[str], exchange: str = "NFO") -> dict:
        now = self.clock()
        missing = [s for s in symbols if (exchange, s) not in self.cache or now - self.cache[(exchange, s)][0] > MAX_AGE]
        if missing:
            wait = MIN_GAP - (now - self.last)
            if wait > 0:
                self.sleep(wait)
            want = sorted(set(missing) | self.watch.get(exchange, set()))[:400]
            self.last = self.clock()                  # counts even if the request fails: no immediate retry burst
            got = self.fetch(want, exchange)
            t = self.clock()
            for s, q in got.items():
                self.cache[(exchange, s)] = (t, q)
        now = self.clock()
        return {s: self.cache[(exchange, s)][1] for s in symbols
                if (exchange, s) in self.cache and now - self.cache[(exchange, s)][0] <= STALE}


class GatedBroker:
    """The real adapter for orders, with its quotes served by the user's gate."""

    def __init__(self, adapter, gate: QuoteGate):
        self._a, self._g = adapter, gate

    def __repr__(self):
        return "GatedBroker(<credentials hidden>)"

    def place(self, req):
        return self._a.place(req)

    def modify(self, order_id, **kw):
        return self._a.modify(order_id, **kw)

    def cancel(self, order_id):
        return self._a.cancel(order_id)

    def orders(self):
        return self._a.orders()

    def positions(self):
        return self._a.positions()

    def quote(self, symbols, exchange="NFO"):
        return self._g.quote(symbols, exchange)
