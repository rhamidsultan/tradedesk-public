"""Incremental indicators on the strategy timeframe (closed bars only — no lookahead).

Operand names (see spec): open high low close, ema(n) sma(n) rsi(n) atr(n), supertrend(n,k),
supertrend_dir(n,k), opening_range_high(m) opening_range_low(m), day_high day_low,
prev_day_high prev_day_low prev_day_close. Numbers are constants.
"""
from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass

_rx = re.compile(r"^([a-z_]+)(?:\(([^)]*)\))?$")


@dataclass
class Bar:
    day: object
    end_minute: int      # minute index after which the bar is closed (exclusive end)
    o: float
    h: float
    l: float
    c: float


class _Ind:
    value: float | None = None

    def update(self, bar: Bar, ctx: "Context") -> None: ...


class _Price(_Ind):
    def __init__(self, f): self.f = f
    def update(self, bar, ctx): self.value = getattr(bar, self.f)


class _EMA(_Ind):
    def __init__(self, n): self.n, self.k, self.cnt, self.value = int(n), 2 / (int(n) + 1), 0, None
    def update(self, bar, ctx):
        self.cnt += 1
        self._v = bar.c if self.cnt == 1 else self.k * bar.c + (1 - self.k) * self._v
        self.value = self._v if self.cnt >= self.n else None


class _SMA(_Ind):
    def __init__(self, n): self.n, self.q, self.value = int(n), deque(), None
    def update(self, bar, ctx):
        self.q.append(bar.c)
        if len(self.q) > self.n: self.q.popleft()
        self.value = sum(self.q) / self.n if len(self.q) == self.n else None


class _RSI(_Ind):
    def __init__(self, n): self.n, self.prev, self.ag, self.al, self.cnt, self.value = int(n), None, 0.0, 0.0, 0, None
    def update(self, bar, ctx):
        if self.prev is not None:
            ch = bar.c - self.prev
            g, l = max(ch, 0), max(-ch, 0)
            self.cnt += 1
            if self.cnt <= self.n:
                self.ag += g / self.n; self.al += l / self.n
            else:
                self.ag = (self.ag * (self.n - 1) + g) / self.n; self.al = (self.al * (self.n - 1) + l) / self.n
            if self.cnt >= self.n:
                self.value = 100.0 if self.al == 0 else 100 - 100 / (1 + self.ag / self.al)
        self.prev = bar.c


class _ATR(_Ind):
    def __init__(self, n): self.n, self.prev, self.cnt, self.atr, self.value = int(n), None, 0, 0.0, None
    def tr(self, bar):
        return bar.h - bar.l if self.prev is None else max(bar.h - bar.l, abs(bar.h - self.prev), abs(bar.l - self.prev))
    def update(self, bar, ctx):
        tr = self.tr(bar); self.cnt += 1
        self.atr = tr if self.cnt == 1 else (self.atr * (self.n - 1) + tr) / self.n if self.cnt > self.n else (self.atr * (self.cnt - 1) + tr) / self.cnt
        self.prev = bar.c
        self.value = self.atr if self.cnt >= self.n else None


class _SuperTrend(_Ind):
    def __init__(self, n, k, direction=False):
        self.atr, self.k, self.dir_out = _ATR(n), float(k), direction
        self.ub = self.lb = None; self.dir = 1; self.prev_c = None; self.value = None
    def update(self, bar, ctx):
        self.atr.update(bar, ctx)
        a = self.atr.value
        if a is None:
            self.prev_c = bar.c; return
        mid = (bar.h + bar.l) / 2
        bub, blb = mid + self.k * a, mid - self.k * a
        ub = bub if self.ub is None or bub < self.ub or (self.prev_c is not None and self.prev_c > self.ub) else self.ub
        lb = blb if self.lb is None or blb > self.lb or (self.prev_c is not None and self.prev_c < self.lb) else self.lb
        if self.ub is not None:
            if self.dir == 1 and bar.c < lb: self.dir = -1
            elif self.dir == -1 and bar.c > ub: self.dir = 1
        self.ub, self.lb, self.prev_c = ub, lb, bar.c
        self.value = float(self.dir) if self.dir_out else (lb if self.dir == 1 else ub)


class _ORH(_Ind):
    def __init__(self, m, hi=True): self.m, self.hi = int(m), hi
    def update(self, bar, ctx): self.value = ctx.opening_range(self.m, self.hi)


class _Day(_Ind):
    def __init__(self, f): self.f = f
    def update(self, bar, ctx): self.value = ctx.day_stat(self.f)


_FACT = {
    "open": lambda: _Price("o"), "high": lambda: _Price("h"), "low": lambda: _Price("l"), "close": lambda: _Price("c"),
    "ema": _EMA, "sma": _SMA, "rsi": _RSI, "atr": _ATR,
    "supertrend": lambda n, k: _SuperTrend(n, k), "supertrend_dir": lambda n, k: _SuperTrend(n, k, True),
    "opening_range_high": lambda m: _ORH(m, True), "opening_range_low": lambda m: _ORH(m, False),
    "day_high": lambda: _Day("day_high"), "day_low": lambda: _Day("day_low"),
    "prev_day_high": lambda: _Day("prev_day_high"), "prev_day_low": lambda: _Day("prev_day_low"),
    "prev_day_close": lambda: _Day("prev_day_close"),
}


def make(name: str) -> _Ind:
    m = _rx.match(name.replace(" ", ""))
    if not m or m.group(1) not in _FACT:
        raise ValueError(f"unknown operand {name!r}")
    args = [a for a in (m.group(2) or "").split(",") if a]
    return _FACT[m.group(1)](*args)


class Context:
    """Day-level facts for indicators: opening range from 1-min spot, day and previous-day stats."""

    def __init__(self):
        self.spot = None
        self.minute = 0
        self.today_h = -math.inf
        self.today_l = math.inf
        self.prev = {}

    def new_day(self, spot):
        if self.spot is not None and self.today_h > -math.inf:
            self.prev = {"prev_day_high": self.today_h, "prev_day_low": self.today_l,
                         "prev_day_close": float(self.spot.c[-1]) if len(self.spot.c) else None}
        self.spot, self.today_h, self.today_l = spot, -math.inf, math.inf

    def see_bar(self, bar: Bar):
        self.minute = bar.end_minute
        self.today_h, self.today_l = max(self.today_h, bar.h), min(self.today_l, bar.l)

    def opening_range(self, m: int, hi: bool):
        if self.minute < m or self.spot is None:
            return None
        sel = self.spot.m < m
        if not sel.any():
            return None
        return float(self.spot.h[sel].max() if hi else self.spot.l[sel].min())

    def day_stat(self, f: str):
        if f == "day_high": return self.today_h if self.today_h > -math.inf else None
        if f == "day_low": return self.today_l if self.today_l < math.inf else None
        return self.prev.get(f)


class Signals:
    """Evaluates a strategy's entry rules on each closed timeframe bar."""

    def __init__(self, rules):
        self.rules = rules
        self.ctx = Context()
        self.inds: dict[str, _Ind] = {}
        for r in rules:
            for c in r.all_of:
                for side in (c.left, c.right):
                    if isinstance(side, str) and side not in self.inds:
                        self.inds[side] = make(side)
        self.cur: dict[str, float | None] = {}
        self.prev: dict[str, float | None] = {}

    def on_bar(self, bar: Bar) -> list[str]:
        """Feed a closed bar; returns actions of rules (with conditions) that are true now."""
        self.ctx.see_bar(bar)
        self.prev = dict(self.cur)
        for k, ind in self.inds.items():
            ind.update(bar, self.ctx)
            self.cur[k] = ind.value
        out = []
        for r in self.rules:
            if r.all_of and all(self._cond(c) for c in r.all_of):
                out.append(r.action)
        return out

    def _val(self, x, which):
        if not isinstance(x, str):
            return float(x)
        return (self.cur if which == "cur" else self.prev).get(x)

    def _cond(self, c) -> bool:
        a, b = self._val(c.left, "cur"), self._val(c.right, "cur")
        if a is None or b is None:
            return False
        if c.op in ("crosses_above", "crosses_below"):
            pa, pb = self._val(c.left, "prev"), self._val(c.right, "prev")
            if pa is None or pb is None:
                return False
            return (pa <= pb and a > b) if c.op == "crosses_above" else (pa >= pb and a < b)
        return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b, "==": a == b}[c.op]
