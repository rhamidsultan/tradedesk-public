"""Ticks -> 1-minute and 1-second bars, written to the store in the py/marketdata layout.

    bars_1m/{idx|opt}/{UNDERLYING}/date=YYYY-MM-DD/kite_live_0.parquet    (same columns as every store source)
    quotes_1m/{idx|opt}/{UNDERLYING}/date=YYYY-MM-DD/kite_live_0.parquet  ts, symbol, bid, ask (last in the minute)
    bars_1s/{idx|opt}/{UNDERLYING}/date=YYYY-MM-DD/kite_live_0.parquet    + bid, ask, ticks, day_open, day_high

Bars are labelled by their START time in IST (naive timestamps), regular session only (09:15:00..15:29:59 for
the index; F&O to the session's close). Writes replace the day's file atomically, so a restart can reload it.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .ticks import Tick

SOURCE = "kite_live"
OPEN = time(9, 15)

BAR_SCHEMA = pa.schema([
    ("ts", pa.timestamp("us")), ("symbol", pa.string()), ("underlying", pa.string()), ("expiry", pa.date32()),
    ("strike", pa.float64()), ("opt_type", pa.string()), ("open", pa.float64()), ("high", pa.float64()),
    ("low", pa.float64()), ("close", pa.float64()), ("volume", pa.int64()), ("oi", pa.int64()), ("source", pa.string()),
])
QUOTE_SCHEMA = pa.schema([("ts", pa.timestamp("us")), ("symbol", pa.string()), ("bid", pa.float64()),
                          ("ask", pa.float64())])
SEC_SCHEMA = pa.schema(list(BAR_SCHEMA) + [("bid", pa.float64()), ("ask", pa.float64()), ("ticks", pa.int32()),
                                           ("day_open", pa.float64()), ("day_high", pa.float64())])


@dataclass(frozen=True)
class Instrument:
    token: int
    symbol: str                # store symbol, e.g. NIFTY26102025000CE, or NIFTY / INDIAVIX for an index
    underlying: str            # store folder: NIFTY, BANKNIFTY, SENSEX, INDIAVIX
    segment: str               # idx | opt
    expiry: date | None = None
    strike: float | None = None
    opt_type: str | None = None


class _Bar:
    __slots__ = ("o", "h", "l", "c", "vol", "oi", "bid", "ask", "n", "dopen", "dhigh")

    def __init__(self, px: float):
        self.o = self.h = self.l = self.c = px
        self.vol = 0
        self.oi = None
        self.bid = self.ask = None
        self.n = 0
        self.dopen = self.dhigh = None


class Book:
    """One day's bars at one bar length (60 s or 1 s) for a set of instruments."""

    def __init__(self, day: date, seconds: int, close: time, index_close: time = time(15, 30)):
        self.day, self.seconds, self.close = day, seconds, close
        self.start = datetime.combine(day, OPEN)
        self.end = datetime.combine(day, close)
        # the index stops with the cash market (and is frozen from 15:15 in the closing auction since Aug 2026);
        # options trade on to the F&O close
        self.index_end = datetime.combine(day, min(index_close, close))
        self.bars: dict[int, dict[int, _Bar]] = defaultdict(dict)   # token -> slot -> bar
        self.cum_vol: dict[int, int] = {}
        self.inst: dict[int, Instrument] = {}
        self.dirty = False

    def add_instruments(self, items) -> None:
        for i in items:
            self.inst[i.token] = i

    def slot(self, at: datetime) -> int | None:
        if not (self.start <= at < self.end):
            return None
        return int((at - self.start).total_seconds()) // self.seconds

    def on_tick(self, t: Tick, at: datetime) -> None:
        """`at` is the tick's IST time (exchange time when the packet has it)."""
        inst = self.inst.get(t.token)
        if inst is None or t.ltp <= 0:
            return
        if inst.segment == "idx" and at >= self.index_end:
            return
        s = self.slot(at)
        if s is None:
            return
        dv = 0
        if t.volume is not None:
            prev = self.cum_vol.get(t.token)
            dv = max(0, t.volume - prev) if prev is not None else 0
            self.cum_vol[t.token] = t.volume
        b = self.bars[t.token].get(s)
        if b is None:
            b = self.bars[t.token][s] = _Bar(t.ltp)
        else:
            b.h, b.l, b.c = max(b.h, t.ltp), min(b.l, t.ltp), t.ltp
        b.vol += dv
        b.n += 1
        if t.oi is not None:
            b.oi = t.oi
        if t.bid:
            b.bid = t.bid
        if t.ask:
            b.ask = t.ask
        if t.day_open:
            b.dopen = t.day_open
        if t.day_high:
            b.dhigh = t.day_high
        self.dirty = True

    def last_tick_slot(self) -> int | None:
        slots = [max(v) for v in self.bars.values() if v]
        return max(slots) if slots else None

    # ---- writing ---------------------------------------------------------------------------------------
    def write(self, root: Path) -> int:
        """Write every bar so far (closed or not) to the store; returns rows written."""
        groups: dict[tuple[str, str], list[tuple[Instrument, int, _Bar]]] = defaultdict(list)
        for tok, slots in self.bars.items():
            inst = self.inst[tok]
            for s, b in slots.items():
                groups[(inst.segment, inst.underlying)].append((inst, s, b))
        n = 0
        for (seg, und), rows in groups.items():
            rows.sort(key=lambda r: (r[0].symbol, r[1]))
            ts = [self.start + timedelta(seconds=s * self.seconds) for _, s, _ in rows]
            base = {
                "ts": ts, "symbol": [i.symbol for i, _, _ in rows], "underlying": [und] * len(rows),
                "expiry": [i.expiry for i, _, _ in rows], "strike": [i.strike for i, _, _ in rows],
                "opt_type": [i.opt_type for i, _, _ in rows], "open": [b.o for *_, b in rows],
                "high": [b.h for *_, b in rows], "low": [b.l for *_, b in rows], "close": [b.c for *_, b in rows],
                "volume": [b.vol for *_, b in rows], "oi": [b.oi or 0 for *_, b in rows],
                "source": [SOURCE] * len(rows),
            }
            if self.seconds == 1:
                extra = {"bid": [b.bid for *_, b in rows], "ask": [b.ask for *_, b in rows],
                         "ticks": [b.n for *_, b in rows], "day_open": [b.dopen for *_, b in rows],
                         "day_high": [b.dhigh for *_, b in rows]}
                _atomic(pa.table({**base, **extra}, schema=SEC_SCHEMA), root, "bars_1s", seg, und, self.day)
            else:
                _atomic(pa.table(base, schema=BAR_SCHEMA), root, "bars_1m", seg, und, self.day)
                q = {"ts": ts, "symbol": base["symbol"], "bid": [b.bid for *_, b in rows], "ask": [b.ask for *_, b in rows]}
                _atomic(pa.table(q, schema=QUOTE_SCHEMA), root, "quotes_1m", seg, und, self.day)
            n += len(rows)
        self.dirty = False
        return n

    @classmethod
    def load(cls, root: Path, day: date, seconds: int, close: time, instruments,
             index_close: time = time(15, 30)) -> "Book":
        """Rebuild the day's bars after a restart from what was already written."""
        b = cls(day, seconds, close, index_close)
        b.add_instruments(instruments)
        by_sym = {i.symbol: i for i in instruments}
        sub = "bars_1s" if seconds == 1 else "bars_1m"
        for f in root.glob(f"{sub}/*/*/date={day.isoformat()}/{SOURCE}_0.parquet"):
            for r in pq.read_table(f).to_pylist():
                inst = by_sym.get(r["symbol"])
                if inst is None:
                    continue
                s = b.slot(r["ts"])
                if s is None:
                    continue
                bar = _Bar(r["open"])
                bar.h, bar.l, bar.c, bar.vol, bar.oi = r["high"], r["low"], r["close"], r["volume"], r["oi"] or None
                if seconds == 1:
                    bar.bid, bar.ask, bar.n = r.get("bid"), r.get("ask"), r.get("ticks") or 0
                b.bars[inst.token][s] = bar
        return b


def _atomic(table: pa.Table, root: Path, sub: str, seg: str, und: str, day: date) -> None:
    d = root / sub / seg / und / f"date={day.isoformat()}"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{SOURCE}_0.parquet.tmp"
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(d / f"{SOURCE}_0.parquet")


def stored_second_days(root: Path) -> list[date]:
    days = set()
    for p in root.glob("bars_1s/*/*/date=*"):
        try:
            days.add(date.fromisoformat(p.name[5:]))
        except ValueError:
            pass
    return sorted(days)


def prune_seconds(root: Path, keep_trading_days: int, today: date, is_trading_day) -> list[date]:
    """Delete 1-second days older than the last `keep_trading_days` trading days (today counts)."""
    import shutil
    keep, d = [], today
    while len(keep) < keep_trading_days:
        if is_trading_day(d):
            keep.append(d)
        d -= timedelta(days=1)
    cutoff = min(keep)
    removed = []
    for p in root.glob("bars_1s/*/*/date=*"):
        try:
            day = date.fromisoformat(p.name[5:])
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(p)
            removed.append(day)
    return sorted(set(removed))
