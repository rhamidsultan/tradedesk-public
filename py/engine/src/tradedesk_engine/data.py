"""Per-day market data for the engine, read from the TradeDesk store.

Minutes are indexed 0..374 from 09:15 (bar start times). Only bars that exist are present; the engine
never invents prices. Single-bar spikes that reverse (bad prints) are masked out of fills and triggers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import numpy as np

from tradedesk_marketdata import Store

N_MIN = 375
T0 = time(9, 15)


def minute_of(t: time | str) -> int:
    t = time.fromisoformat(t) if isinstance(t, str) else t
    return (t.hour * 60 + t.minute) - (T0.hour * 60 + T0.minute)


def ts_of(d: date, m: int) -> datetime:
    return datetime.combine(d, T0) + timedelta(minutes=m)


@dataclass
class Series:
    """One instrument for one day: arrays indexed by bar number, plus minute index of each bar."""
    m: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    good: np.ndarray        # False for masked bad prints
    _pos: np.ndarray = field(init=False, repr=False)

    def __post_init__(self):
        pos = np.full(N_MIN, -1, dtype=np.int32)
        pos[self.m] = np.arange(len(self.m), dtype=np.int32)
        self._pos = pos

    def at(self, minute: int) -> int:
        """Index of the bar at `minute`, or -1."""
        if 0 <= minute < N_MIN:
            return int(self._pos[minute])
        return -1

    def last_before(self, minute: int, max_age: int = 10_000) -> int:
        """Index of the last good bar strictly before `minute` (within max_age minutes), or -1."""
        i = int(np.searchsorted(self.m, minute, side="left")) - 1
        while i >= 0 and not self.good[i]:
            i -= 1
        if i >= 0 and minute - self.m[i] <= max_age:
            return i
        return -1

    def first_from(self, minute: int, within: int) -> int:
        """Index of the first good bar at or after `minute` and before minute+within, or -1."""
        i = int(np.searchsorted(self.m, minute, side="left"))
        while i < len(self.m) and self.m[i] < minute + within:
            if self.good[i]:
                return i
            i += 1
        return -1


def _mask_spikes(c: np.ndarray, ratio: float = 0.4) -> np.ndarray:
    """Backward-looking only (no lookahead): a bar whose close jumps more than `ratio` from the previous
    close is not used for ENTRY fills or strike selection. Stops still trigger on every bar."""
    good = np.ones(len(c), dtype=bool)
    if len(c) >= 2:
        prev, cur = c[:-1], c[1:]
        good[1:] = ~((prev >= 5) & (np.abs(cur / prev - 1) > ratio))
    return good


@dataclass
class Seconds:
    """One option's 1-second bars for one day (only seconds that traded). `t` = seconds since 09:15:00."""
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    bid: np.ndarray          # best bid / ask at the end of the second (NaN where not stored)
    ask: np.ndarray

    def in_minute(self, m: int) -> range:
        """Bar indexes inside minute m (09:15 + m), in time order."""
        lo = int(np.searchsorted(self.t, m * 60, side="left"))
        hi = int(np.searchsorted(self.t, (m + 1) * 60, side="left"))
        return range(lo, hi)


@dataclass
class Contract:
    symbol: str
    expiry: date
    strike: float
    opt_type: str
    s: Series


class DayData:
    def __init__(self, d: date, spot: Series | None, options: dict[str, Contract]):
        self.date = d
        self.spot = spot
        self.options = options
        self.by_key: dict[tuple[date, str], list[Contract]] = {}
        for c in options.values():
            self.by_key.setdefault((c.expiry, c.opt_type), []).append(c)
        for v in self.by_key.values():
            v.sort(key=lambda c: c.strike)
        self.expiries = sorted({c.expiry for c in options.values()})
        self.vix: Series | None = None
        self.sec_loader = None              # symbol -> Seconds | None, when 1-second bars are stored for the day
        self._sec: dict[str, Seconds | None] = {}

    @property
    def has_seconds(self) -> bool:
        return self.sec_loader is not None

    def seconds(self, symbol: str) -> Seconds | None:
        """The option's 1-second bars, read the first time they are needed (only contracts held)."""
        if self.sec_loader is None:
            return None
        if symbol not in self._sec:
            self._sec[symbol] = self.sec_loader(symbol)
        return self._sec[symbol]


class StoreFeed:
    """Loads DayData from the store, one day at a time."""

    def __init__(self, store: Store, underlying: str, seconds: bool = False):
        self.store = store
        self.u = underlying
        self.con = store.connect()
        self.seconds = seconds          # settle stops and targets on 1-second bars where they are stored

    def dates(self, start: date, end: date) -> list[date]:
        """Days with index bars (TrueData or the live recorder), from the folder names: reading every file's dates
        through DuckDB ran out of memory on the 2 GB server."""
        out = []
        for p in (self.store.root / "bars_1m" / "idx" / self.u).glob("date=*"):
            try:
                d = date.fromisoformat(p.name[5:])
            except ValueError:
                continue
            if start <= d <= end and (any(p.glob("truedata_*.parquet")) or any(p.glob("kite_live_*.parquet"))):
                out.append(d)
        return sorted(out)

    def _series(self, rows) -> Series:
        m = np.array([minute_of(r[0].time()) for r in rows], dtype=np.int32)
        arr = np.array([r[1:6] for r in rows], dtype=float) if rows else np.zeros((0, 5))
        c = arr[:, 3] if len(arr) else np.zeros(0)
        return Series(m, arr[:, 0], arr[:, 1], arr[:, 2], c, np.nan_to_num(arr[:, 4]), _mask_spikes(c))

    @staticmethod
    def _files(folder, pattern="truedata_*.parquet"):
        """TrueData history where present, else the live recorder's bars (kite_live) for that day."""
        f = list(folder.glob(pattern))
        return f or list(folder.glob("kite_live_*.parquet"))

    def load(self, d: date) -> DayData:
        base = self.store.root / "bars_1m"
        spot_files = self._files(base / "idx" / self.u / f"date={d}")
        spot = None
        if spot_files:
            rows = self.con.execute(
                f"SELECT ts, open, high, low, close, volume FROM read_parquet({[str(f) for f in spot_files]}) ORDER BY ts").fetchall()
            spot = self._series(rows)
        opts: dict[str, Contract] = {}
        opt_files = self._files(base / "opt" / self.u / f"date={d}")
        if opt_files:
            rows = self.con.execute(
                f"SELECT symbol, expiry, strike, opt_type, ts, open, high, low, close, volume "
                f"FROM read_parquet({[str(f) for f in opt_files]}) ORDER BY symbol, ts").fetchall()
            start = 0
            for i in range(1, len(rows) + 1):
                if i == len(rows) or rows[i][0] != rows[start][0]:
                    sym, exp, k, t = rows[start][:4]
                    opts[sym] = Contract(sym, exp, float(k), t, self._series([r[4:] for r in rows[start:i]]))
                    start = i
        dd = DayData(d, spot, opts)
        vix_files = self._files(base / "idx" / "INDIAVIX" / f"date={d}")
        if vix_files:
            rows = self.con.execute(
                f"SELECT ts, open, high, low, close, volume FROM read_parquet({[str(f) for f in vix_files]}) ORDER BY ts").fetchall()
            dd.vix = self._series(rows)
        sec_files = list((self.store.root / "bars_1s" / "opt" / self.u / f"date={d}").glob("*.parquet")) \
            if self.seconds else []
        if sec_files:
            files = [str(f) for f in sec_files]
            cols = {r[0] for r in self.con.execute(f"DESCRIBE SELECT * FROM read_parquet({files}, union_by_name=true)").fetchall()}
            q = ", ".join(x if x in cols else f"NULL AS {x}" for x in ("bid", "ask"))

            def load_sec(symbol: str) -> Seconds | None:
                rows = self.con.execute(
                    f"SELECT ts, open, high, low, close, {q} FROM read_parquet({files}, union_by_name=true) "
                    f"WHERE symbol = ? ORDER BY ts", [symbol]).fetchall()
                if not rows:
                    return None
                t0 = datetime.combine(d, T0)
                t = np.array([int((r[0] - t0).total_seconds()) for r in rows], dtype=np.int32)
                a = np.array([[np.nan if v is None else v for v in r[1:7]] for r in rows], dtype=float)
                return Seconds(t, a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5])
            dd.sec_loader = load_sec
        return dd
