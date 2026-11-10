"""Read-only view of the market-data store (the Parquet store built by py/marketdata) for the app's Data screens.

Fast paths first: day lists come from the `date=YYYY-MM-DD` folder names (no Parquet read), and every query
reads only the date partitions it needs. Results that scan the whole store (expiry list, disk use) are
cached for a few minutes. The store is never written from here.
"""
from __future__ import annotations

import calendar as _cal
import os
import re
import threading
import time as _time
from dataclasses import dataclass
from datetime import date, time, timedelta
from pathlib import Path

from .clock import Clock
from .marketcal import MarketCalendar

INDEXES = ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY")
VIX = "INDIAVIX"
SESSION_BARS = 375            # 09:15..15:29 one-minute bars
SHORT_DAY_BARS = SESSION_BARS - 5
_DAY = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")
_SAFE = re.compile(r"^[A-Z0-9&-]{1,40}$")
CACHE_SECONDS = 600


class StoreUnavailable(RuntimeError):
    pass


@dataclass
class _Cached:
    at: float
    value: object


class DataService:
    def __init__(self, root: str | Path | None, calendar: MarketCalendar, clock: Clock | None = None,
                 memory_limit: str = "1GB"):
        self.root = Path(root).expanduser() if root else None
        self.cal = calendar
        self.clock = clock or Clock()
        self.memory_limit = memory_limit
        self._cache: dict[str, _Cached] = {}
        self._lock = threading.Lock()

    # ---- basics ---------------------------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.root and (self.root / "bars_1m").is_dir())

    def _need(self) -> Path:
        if not self.available:
            raise StoreUnavailable("No market data on this server yet.")
        return self.root

    def _con(self):
        import duckdb  # imported lazily so the API starts even where duckdb is missing

        con = duckdb.connect()
        con.execute(f"SET memory_limit='{self.memory_limit}'")
        con.execute("SET threads=2")
        return con

    def _dir(self, segment: str, underlying: str) -> Path:
        if not _SAFE.match(underlying):
            raise ValueError("bad underlying")
        return self._need() / "bars_1m" / segment / underlying

    def days(self, segment: str, underlying: str) -> list[date]:
        d = self._dir(segment, underlying)
        if not d.is_dir():
            return []
        out = []
        for p in d.iterdir():
            m = _DAY.match(p.name)
            if m and any(p.glob("*.parquet")):
                out.append(date.fromisoformat(m.group(1)))
        return sorted(out)

    def _glob(self, segment: str, underlying: str, day_pattern: str = "*") -> str:
        return str(self._dir(segment, underlying) / f"date={day_pattern}" / "*.parquet")

    def _cached(self, key: str, fn):
        with self._lock:
            c = self._cache.get(key)
            if c and _time.monotonic() - c.at < CACHE_SECONDS:
                return c.value
        value = fn()
        with self._lock:
            self._cache[key] = _Cached(_time.monotonic(), value)
        return value

    # ---- summary: what is stored, per index -------------------------------------------------------------
    def summary(self) -> dict:
        if not self.available:
            return {"available": False, "indexes": [], "disk": []}
        indexes = []
        for u in INDEXES:
            spot, opts = self.days("idx", u), self.days("opt", u)
            if not spot and not opts:
                continue
            last_trading = self._last_trading_day()
            data_to = max(spot) if spot else None
            indexes.append({
                "underlying": u,
                "spot_from": spot[0].isoformat() if spot else None,
                "spot_to": data_to.isoformat() if data_to else None,
                "spot_days": len(spot),
                "options_from": opts[0].isoformat() if opts else None,
                "options_to": opts[-1].isoformat() if opts else None,
                "options_days": len(opts),
                "behind_days": self._trading_days_between(data_to, last_trading) if data_to else None,
            })
        vix = self.days("idx", VIX)
        return {"available": True, "indexes": indexes,
                "vix_to": vix[-1].isoformat() if vix else None,
                "disk": self._cached("disk", self._disk_use)}

    def _last_trading_day(self) -> date:
        d = self.clock.now_ist().date()
        while not self.cal.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def _trading_days_between(self, a: date, b: date) -> int:
        n, d = 0, a + timedelta(days=1)
        while d <= b:
            n += self.cal.is_trading_day(d)
            d += timedelta(days=1)
        return n

    def _disk_use(self) -> list[dict]:
        root = self._need()
        out = []
        for top in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
            parts = [top] if top.name not in ("bars_1m", "bars_5m") else \
                [s for s in top.iterdir() if s.is_dir()]
            for p in parts:
                size = 0
                for dirpath, _, files in os.walk(p):
                    for f in files:
                        try:
                            size += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            pass
                out.append({"folder": str(p.relative_to(root)), "bytes": size})
        return sorted(out, key=lambda r: -r["bytes"])

    # ---- month calendar with per-day checks --------------------------------------------------------------
    def month(self, underlying: str, month: str) -> dict:
        """Every day of a month: trading day or not, spot bars, VIX, option contracts and a data check."""
        if not re.match(r"^\d{4}-\d{2}$", month):
            raise ValueError("month must be YYYY-MM")
        self._need()
        y, m = map(int, month.split("-"))
        first, last = date(y, m, 1), date(y, m, _cal.monthrange(y, m)[1])
        spot = self._counts("idx", underlying, month, "count(*)")
        vix = self._counts("idx", VIX, month, "count(*)")
        opt = self._counts("opt", underlying, month, "count(DISTINCT symbol)", extra=", count(*)")
        late = self._late_spot_gaps(underlying, month) if spot else {}
        days = []
        d = first
        while d <= last:
            trading = self.cal.is_trading_day(d)
            n_spot = spot.get(d, (0,))[0]
            n_opt, n_opt_bars = opt.get(d, (0, 0))
            issues = []
            if trading:
                if not n_spot:
                    issues.append(("fail", "No index bars"))
                elif n_spot < SHORT_DAY_BARS:
                    issues.append(("warn", f"Index has {n_spot} of {SESSION_BARS} minutes"))
                if d in late:
                    issues.append(("warn", f"Index missing {late[d]} minutes after 15:15"))
                if n_spot and not vix.get(d):
                    issues.append(("warn", "No India VIX"))
                if n_spot and not n_opt and d >= self._first_option_day(underlying):
                    issues.append(("warn", "No option prices"))
            elif n_spot:
                issues.append(("info", "Special session"))
            status = "none" if not (trading or n_spot) else \
                "fail" if any(s == "fail" for s, _ in issues) else \
                "warn" if any(s == "warn" for s, _ in issues) else "ok"
            days.append({"date": d.isoformat(), "trading_day": trading, "spot_bars": n_spot,
                         "vix": bool(vix.get(d)), "option_contracts": n_opt, "option_bars": n_opt_bars,
                         "status": status, "issues": [{"level": s, "text": t} for s, t in issues]})
            d += timedelta(days=1)
        return {"underlying": underlying, "month": month, "days": days,
                "summary": {k: sum(1 for x in days if x["status"] == k) for k in ("ok", "warn", "fail")}}

    def _first_option_day(self, underlying: str) -> date:
        return self._cached(f"optfirst:{underlying}",
                            lambda: (self.days("opt", underlying) or [date.max])[0])

    def _counts(self, segment: str, underlying: str, month: str, agg: str, extra: str = "") -> dict:
        if not self._dir(segment, underlying).is_dir() or not any(
                self._dir(segment, underlying).glob(f"date={month}-*")):
            return {}
        con = self._con()
        try:
            rows = con.execute(
                f"SELECT date, {agg}{extra} FROM read_parquet(?, hive_partitioning=true) GROUP BY 1",
                [self._glob(segment, underlying, f"{month}-*")]).fetchall()
        finally:
            con.close()
        return {(r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0]))): tuple(r[1:]) for r in rows}

    def _late_spot_gaps(self, underlying: str, month: str) -> dict:
        """Days where the index has the session but is missing minutes between 15:16 and 15:28 (seen since Aug 2026)."""
        con = self._con()
        try:
            rows = con.execute(
                "SELECT date, count(*) FROM read_parquet(?, hive_partitioning=true) "
                "WHERE CAST(ts AS TIME) BETWEEN TIME '15:16' AND TIME '15:28' GROUP BY 1",
                [self._glob("idx", underlying, f"{month}-*")]).fetchall()
            days = {r[0] for r in con.execute("SELECT DISTINCT date FROM read_parquet(?, hive_partitioning=true)",
                                              [self._glob("idx", underlying, f"{month}-*")]).fetchall()}
        finally:
            con.close()
        have = {r[0]: r[1] for r in rows}
        return {d: 13 - have.get(d, 0) for d in days if have.get(d, 0) < 13}

    # ---- expiries, chain, a contract, the index -------------------------------------------------------------
    def expiries(self, underlying: str) -> list[dict]:
        def run():
            if not self._dir("opt", underlying).is_dir():
                return []
            con = self._con()
            try:
                rows = con.execute(
                    "SELECT expiry, count(DISTINCT symbol), min(date), max(date), count(DISTINCT date) "
                    "FROM read_parquet(?, hive_partitioning=true) GROUP BY 1 ORDER BY 1",
                    [self._glob("opt", underlying)]).fetchall()
            finally:
                con.close()
            return [{"expiry": str(e), "contracts": n, "from": str(a), "to": str(b), "days": d}
                    for e, n, a, b, d in rows if e is not None]
        return self._cached(f"expiries:{underlying}", run)

    def chain(self, underlying: str, expiry: str, day: str, at: str = "15:29") -> dict:
        """Option chain for one expiry as of a minute on a day: last close, day volume, OI per strike."""
        d, e, t = date.fromisoformat(day), date.fromisoformat(expiry), time.fromisoformat(at)
        con = self._con()
        try:
            rows = con.execute(
                """SELECT strike, opt_type, arg_max(close, ts) AS ltp, sum(volume) AS vol, arg_max(oi, ts) AS oi,
                          max(ts) AS last_ts
                   FROM read_parquet(?, hive_partitioning=true)
                   WHERE expiry = ? AND CAST(ts AS TIME) <= ? GROUP BY 1, 2 ORDER BY 1""",
                [self._glob("opt", underlying, d.isoformat()), e, t]).fetchall()
            spot = con.execute(
                "SELECT arg_max(close, ts) FROM read_parquet(?, hive_partitioning=true) WHERE CAST(ts AS TIME) <= ?",
                [self._glob("idx", underlying, d.isoformat()), t]).fetchone()[0] if \
                (self._dir("idx", underlying) / f"date={d.isoformat()}").is_dir() else None
        finally:
            con.close()
        strikes: dict[float, dict] = {}
        for k, typ, ltp, vol, oi, last_ts in rows:
            s = strikes.setdefault(k, {"strike": k, "CE": None, "PE": None})
            s[typ] = {"ltp": ltp, "volume": vol, "oi": oi, "as_of": str(last_ts)}
        return {"underlying": underlying, "expiry": expiry, "date": day, "at": at, "spot": spot,
                "strikes": list(strikes.values())}

    def contract(self, underlying: str, symbol: str, day: str) -> list[dict]:
        if not _SAFE.match(symbol):
            raise ValueError("bad symbol")
        d = date.fromisoformat(day)
        if not (self._dir("opt", underlying) / f"date={d.isoformat()}").is_dir():
            return []
        con = self._con()
        try:
            rows = con.execute(
                "SELECT ts, open, high, low, close, volume, oi FROM read_parquet(?, hive_partitioning=true) "
                "WHERE symbol = ? ORDER BY ts", [self._glob("opt", underlying, d.isoformat()), symbol]).fetchall()
        finally:
            con.close()
        return [{"ts": str(r[0]), "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5], "oi": r[6]} for r in rows]

    def index_bars(self, underlying: str, day: str) -> list[dict]:
        d = date.fromisoformat(day)
        if not (self._dir("idx", underlying) / f"date={d.isoformat()}").is_dir():
            return []
        con = self._con()
        try:
            rows = con.execute("SELECT ts, open, high, low, close FROM read_parquet(?, hive_partitioning=true) ORDER BY ts",
                               [self._glob("idx", underlying, d.isoformat())]).fetchall()
        finally:
            con.close()
        return [{"ts": str(r[0]), "o": r[1], "h": r[2], "l": r[3], "c": r[4]} for r in rows]

    # ---- scanner input -------------------------------------------------------------------------------------
    def symbols(self, segment: str) -> list[str]:
        d = self._need() / "bars_1m" / segment
        if segment not in ("idx", "eq") or not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir() if p.is_dir() and _SAFE.match(p.name) and p.name != VIX)

    def recent_bars(self, segment: str, symbols: list[str], days: int = 8, until: time | None = None) \
            -> dict[str, list[tuple]]:
        """symbol -> [(ts, o, h, l, c)] for its last `days` stored days (bars ending by `until` on the last day)."""
        files, last_day = [], {}
        for s in symbols:
            ds = self.days(segment, s)[-days:]
            if not ds:
                continue
            last_day[s] = ds[-1]
            for d in ds:
                files += [str(f) for f in (self._dir(segment, s) / f"date={d.isoformat()}").glob("*.parquet")]
        if not files:
            return {}
        con = self._con()
        try:
            rows = con.execute("SELECT filename, ts, open, high, low, close FROM read_parquet(?, filename=true, "
                               "hive_partitioning=false, union_by_name=true) ORDER BY filename, ts", [files]).fetchall()
        finally:
            con.close()
        out: dict[str, list[tuple]] = {}
        for fn, ts, o, h, l, c in rows:
            s = Path(fn).parts[-3]
            if until is not None and ts.date() == last_day.get(s) and \
                    (ts.hour * 60 + ts.minute + 1) > until.hour * 60 + until.minute:
                continue                      # bar not closed by the scan minute
            out.setdefault(s, []).append((ts, o, h, l, c))
        for v in out.values():
            v.sort(key=lambda r: r[0])
        return out
