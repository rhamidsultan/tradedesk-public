"""Tick -> 1-minute bars for today, kept in memory, flushed to the store, and turned into engine DayData."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import time as _time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from tradedesk_engine.data import N_MIN, Contract, DayData, Series, _mask_spikes, minute_of
from .kite import Instrument, Tick

SOURCE = "kite_live"


class MinuteBars:
    def __init__(self, d: date, underlying: str, spot_token: int, instruments: list[Instrument], vix_token: int | None = None):
        self.d, self.u = d, underlying
        self.spot_token, self.vix_token = spot_token, vix_token
        self.inst = {i.token: i for i in instruments}
        self.bars: dict[int, dict[int, list]] = defaultdict(dict)   # token -> minute -> [o,h,l,c,vol,bid,ask]
        self.last_cum_vol: dict[int, int] = {}
        self.ltp: dict[int, float] = {}
        self.quote: dict[int, tuple] = {}
        self.seen: dict[int, float] = {}

    def on_tick(self, t: Tick, now: datetime):
        m = minute_of(now.time().replace(second=0, microsecond=0))
        if not 0 <= m < N_MIN or t.ltp <= 0:
            return
        self.ltp[t.token] = t.ltp
        self.seen[t.token] = _time.time()
        if t.bid or t.ask:
            self.quote[t.token] = (t.bid, t.ask)
        b = self.bars[t.token].get(m)
        dv = 0
        if t.volume is not None:
            prev = self.last_cum_vol.get(t.token)
            dv = max(0, t.volume - prev) if prev is not None else 0
            self.last_cum_vol[t.token] = t.volume
        if b is None:
            self.bars[t.token][m] = [t.ltp, t.ltp, t.ltp, t.ltp, dv, t.bid, t.ask]
        else:
            b[1] = max(b[1], t.ltp); b[2] = min(b[2], t.ltp); b[3] = t.ltp; b[4] += dv
            if t.bid: b[5] = t.bid
            if t.ask: b[6] = t.ask

    def _series(self, token: int, until: int) -> Series:
        mins = sorted(m for m in self.bars.get(token, {}) if m < until)
        arr = np.array([self.bars[token][m][:5] for m in mins], dtype=float) if mins else np.zeros((0, 5))
        c = arr[:, 3] if len(arr) else np.zeros(0)
        return Series(np.array(mins, dtype=np.int32), arr[:, 0], arr[:, 1], arr[:, 2], c, arr[:, 4], _mask_spikes(c))

    def day_data(self, until: int = N_MIN) -> DayData:
        """Closed bars only (minutes < until)."""
        spot = self._series(self.spot_token, until)
        opts = {}
        for tok, i in self.inst.items():
            s = self._series(tok, until)
            if len(s.m):
                opts[i.symbol] = Contract(i.symbol, i.expiry, i.strike, i.opt_type, s)
        dd = DayData(self.d, spot if len(spot.m) else None, opts)
        if self.vix_token:
            v = self._series(self.vix_token, until)
            dd.vix = v if len(v.m) else None
        return dd

    def age(self, token: int) -> float:
        """Seconds since the last tick of this instrument (inf if never)."""
        t = self.seen.get(token)
        return _time.time() - t if t else float("inf")

    def quote_at(self, symbol: str, m: int) -> tuple | None:
        for tok, i in self.inst.items():
            if i.symbol == symbol:
                b = self.bars.get(tok, {}).get(m)
                return (b[5], b[6]) if b else None
        return None

    def flush(self, store_root: Path):
        """Write today's bars (all minutes so far) to the store as source kite_live (replaces the file)."""
        t0 = datetime.combine(self.d, datetime.min.time()).replace(hour=9, minute=15)
        def rows(tok, sym, exp, k, typ):
            for m, b in sorted(self.bars.get(tok, {}).items()):
                yield (t0 + timedelta(minutes=m), sym, self.u, exp, k, typ, *b[:4], int(b[4]), 0, b[5], b[6], SOURCE)
        schema = pa.schema([("ts", pa.timestamp("us")), ("symbol", pa.string()), ("underlying", pa.string()), ("expiry", pa.date32()),
                            ("strike", pa.float64()), ("opt_type", pa.string()), ("open", pa.float64()), ("high", pa.float64()),
                            ("low", pa.float64()), ("close", pa.float64()), ("volume", pa.int64()), ("oi", pa.int64()),
                            ("bid", pa.float64()), ("ask", pa.float64()), ("source", pa.string())])
        cols = schema.names
        for seg, items in (("idx", [(self.spot_token, self.u, None, None, None)] +
                            ([(self.vix_token, "INDIAVIX", None, None, None)] if self.vix_token else [])),
                           ("opt", [(t, i.symbol, i.expiry, i.strike, i.opt_type) for t, i in self.inst.items()])):
            for name in ({x[1] for x in items} if seg == "idx" else {self.u}):
                data = [r for it in items if (seg == "opt" or it[1] == name) for r in rows(*it)]
                if not data:
                    continue
                tbl = pa.table({c: [r[j] for r in data] for j, c in enumerate(cols)}, schema=schema)
                # bars in the store's schema; bid/ask kept apart (quotes_1m) so store reads stay uniform
                for sub, t in (("bars_1m", tbl.drop_columns(["bid", "ask"])),
                               ("quotes_1m", tbl.select(["ts", "symbol", "bid", "ask"]))):
                    d = store_root / sub / seg / name / f"date={self.d}"
                    d.mkdir(parents=True, exist_ok=True)
                    tmp = d / f".{SOURCE}_0.parquet.tmp"
                    pq.write_table(t, tmp)
                    tmp.replace(d / f"{SOURCE}_0.parquet")

    @classmethod
    def load(cls, store_root: Path, d: date, underlying: str, spot_token: int, instruments: list[Instrument], vix_token=None):
        """Rebuild today's bars after a restart from the flushed file."""
        mb = cls(d, underlying, spot_token, instruments, vix_token)
        by_sym = {i.symbol: i.token for i in instruments}
        by_sym[underlying] = spot_token
        if vix_token:
            by_sym["INDIAVIX"] = vix_token
        for seg, name in (("idx", underlying), ("idx", "INDIAVIX"), ("opt", underlying)):
            f = store_root / "bars_1m" / seg / name / f"date={d}" / f"{SOURCE}_0.parquet"
            if not f.exists():
                continue
            q = store_root / "quotes_1m" / seg / name / f"date={d}" / f"{SOURCE}_0.parquet"
            qd = {(r["symbol"], r["ts"]): (r["bid"], r["ask"]) for r in pq.read_table(q).to_pylist()} if q.exists() else {}
            for r in pq.read_table(f).to_pylist():
                tok = by_sym.get(r["symbol"])
                if tok is None:
                    continue
                m = minute_of(r["ts"].time())
                mb.bars[tok][m] = [r["open"], r["high"], r["low"], r["close"], r["volume"], *qd.get((r["symbol"], r["ts"]), (None, None))]
        return mb


def instruments_from_store(store_root: Path, d: date, underlying: str) -> list[Instrument]:
    """Instruments recorded on day d (for closing a day after a restart, without Kite)."""
    f = store_root / "bars_1m" / "opt" / underlying / f"date={d}" / f"{SOURCE}_0.parquet"
    if not f.exists():
        return []
    t = pq.read_table(f, columns=["symbol", "expiry", "strike", "opt_type"]).to_pylist()
    seen, out = {}, []
    for r in t:
        if r["symbol"] not in seen:
            seen[r["symbol"]] = len(seen) + 1
            out.append(Instrument(-seen[r["symbol"]], r["symbol"], r["symbol"], underlying, r["expiry"], r["strike"], r["opt_type"], 0))
    return out
