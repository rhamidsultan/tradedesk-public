"""td2-portfolio: keeps the owner's Zerodha holdings, history and alerts up to date. Read-only: it never places,
changes or cancels an order (it only calls holdings, trades and historical prices).

Talks to Zerodha only on trading days from 08:45 to 16:00 IST, using the owner's own login of the day, and not
at all while sync is paused by hand. Holdings every minute in market hours (every 5 minutes otherwise), a snapshot
every minute in market hours and once after the cash close, today's trades every 15 minutes, daily prices every
3 hours. Run with `python -m tdserver.portfolio.sync`.
"""
from __future__ import annotations

import logging
import signal
import time as _time
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select

from ..alerts.outbox import raise_alert
from ..context import AppContext, build_context, quiet_http_logs
from ..models import (PfAlertSent, PfExtreme, PfHoldingDay, PfPrice, PfSnapshot, PfTrade, ServiceStatus, Setting,
                      User)
from ..settings import get_settings
from .book import build, norm_id

log = logging.getLogger("tdserver.portfolio")

SYNC_FROM, SYNC_TO = time(8, 45), time(16, 0)
EXTREMES = (("day", "today", "day_pnl"), ("total", "ALL", "pnl"))
DEFAULTS = {"paused": False, "paused_since": "", "gold_symbols": [], "non_gold_symbols": [],
            "alerts": {"holding_day_move_pct": 3, "portfolio_day_move_pct": 2, "gold_allocation_band_pct": [5, 25],
                       "price_levels": []}}


def settings(db) -> dict:
    row = db.get(Setting, "portfolio")
    out = {**DEFAULTS, **((row.value if row else None) or {})}
    out["alerts"] = {**DEFAULTS["alerts"], **(out.get("alerts") or {})}
    return out


def save_settings(db, ctx: AppContext, value: dict, user_id: int | None) -> dict:
    row = db.get(Setting, "portfolio")
    if row is None:
        db.add(Setting(key="portfolio", value=value, updated_at=ctx.clock.now(), updated_by=user_id))
    else:
        row.value, row.updated_at, row.updated_by = value, ctx.clock.now(), user_id
    return value


def owner_id(db) -> int | None:
    return db.scalar(select(User.id).where(User.role == "owner", User.disabled_at.is_(None)).order_by(User.id))


def market_open(ctx: AppContext, t: datetime) -> bool:
    s = ctx.calendar.session(t.date())
    return ctx.calendar.is_trading_day(t.date()) and s.open <= t.time() <= s.cash_close


def sync_window(ctx: AppContext, t: datetime) -> bool:
    return ctx.calendar.is_trading_day(t.date()) and SYNC_FROM <= t.time() <= SYNC_TO


class Kite:
    """The three read-only calls this service needs, with the owner's own key and today's token."""

    def __init__(self, ctx: AppContext, api_key: str, token: str):
        self.ctx, self.h = ctx, {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{token}"}

    def _get(self, path: str, params: dict | None = None):
        r = self.ctx.http.get(f"{self.ctx.settings.kite_api_base}{path}", params=params, headers=self.h, timeout=15)
        if r.status_code == 403:
            raise PermissionError("Zerodha login expired")
        r.raise_for_status()
        return r.json()["data"]

    def holdings(self) -> list[dict]:
        return self._get("/portfolio/holdings")

    def trades(self) -> list[dict]:
        return self._get("/trades")

    def daily(self, token: int, start: date, end: date) -> list[list]:
        d = self._get(f"/instruments/historical/{token}/day",
                      {"from": f"{start} 00:00:00", "to": f"{end} 23:59:59"})
        return d.get("candles", [])


def kite_for(ctx: AppContext, db, uid: int) -> Kite | None:
    from ..kite import access_token, account, secrets_for
    acc = account(db, uid)
    tok = access_token(ctx, acc) if acc else None
    if not tok:
        return None
    api_key, _ = secrets_for(ctx, acc)
    return Kite(ctx, api_key, tok)


class PortfolioSync:
    def __init__(self, ctx: AppContext, sleep=_time.sleep):
        self.ctx, self.sleep = ctx, sleep
        self.last_snapshot = float("-inf")
        self.last = {"holdings": float("-inf"), "trades": float("-inf"), "prices": float("-inf")}
        self.loaded = False                       # one fetch outside the window so the page is never blank

    # ---- one pass ---------------------------------------------------------------------------------------
    def tick(self, mono: float | None = None) -> dict:
        mono = _time.monotonic() if mono is None else mono
        now = self.ctx.clock.now_ist()
        with self.ctx.sessions() as db:
            st = settings(db)
            uid = owner_id(db)
            status = db.get(ServiceStatus, "portfolio")
            state = dict(status.data) if status else {}
            if st["paused"]:
                return self._put(db, state, sync="paused")
            if uid is None:
                return self._put(db, state, sync="no_owner")
            in_window = sync_window(self.ctx, now)
            if not in_window and self.loaded:
                return self._put(db, state, sync="outside_hours")
            every = 60 if market_open(self.ctx, now) else 300
            if mono - self.last["holdings"] < every and self.loaded:
                return {"sync": "waiting"}
            kite = kite_for(self.ctx, db, uid)
            if kite is None:
                return self._put(db, {**state, "logged_in": False}, sync="login_needed")
            self.last["holdings"] = mono
            try:
                raw = kite.holdings()
            except PermissionError:
                return self._put(db, {**state, "logged_in": False}, sync="login_needed")
            except Exception as e:  # noqa: BLE001  never log the response (it could carry a token)
                log.warning("holdings failed: %s", type(e).__name__)
                return self._put(db, {**state, "error": f"Zerodha did not answer ({type(e).__name__})."}, sync="error")
            self.loaded = True
            rows, summary = build(raw, st["gold_symbols"], st["non_gold_symbols"])
            state = {"holdings": rows, "summary": summary, "logged_in": True, "error": None,
                     "updated": now.strftime("%Y-%m-%d %H:%M:%S")}
            live = market_open(self.ctx, now)
            day = now.strftime("%Y-%m-%d")
            first_today = db.scalar(select(PfSnapshot.ts).where(PfSnapshot.day == day).limit(1)) is None
            if rows and ((live and mono - self.last_snapshot >= 60) or first_today or self._needs_close(db, now)):
                self.save_snapshot(db, rows, summary, now)
                self.last_snapshot = mono
            if live and rows:
                self.update_extremes(db, summary, now)
                self.check_alerts(db, uid, rows, summary, st, now)
            if in_window:
                self.extras(db, kite, rows, mono)
            return self._put(db, state, sync="ok")

    def _put(self, db, state: dict, sync: str) -> dict:
        state = {**state, "sync": sync}
        row = db.get(ServiceStatus, "portfolio")
        if row is None:
            db.add(ServiceStatus(name="portfolio", updated_at=self.ctx.clock.now(), data=state))
        else:
            row.updated_at, row.data = self.ctx.clock.now(), state
        db.commit()
        return {"sync": sync}

    def _needs_close(self, db, now: datetime) -> bool:
        """Once a trading day: after the cash close, if the last snapshot is from before it."""
        close = self.ctx.calendar.session(now.date()).cash_close
        after = (datetime.combine(now.date(), close) + timedelta(minutes=5)).time()
        if not (self.ctx.calendar.is_trading_day(now.date()) and after <= now.time() <= time(18, 0)):
            return False
        day = now.strftime("%Y-%m-%d")
        last = db.scalar(select(func.max(PfSnapshot.ts)).where(PfSnapshot.day == day))
        return bool(last) and last < f"{day} {after:%H:%M:%S}"

    # ---- storing ------------------------------------------------------------------------------------------
    @staticmethod
    def save_snapshot(db, rows, s, now: datetime) -> None:
        day, ts = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")
        db.merge(PfSnapshot(ts=ts, day=day, value=s["value"], invested=s["invested"], pnl=s["pnl"],
                            day_pnl=s["day_pnl"], gold_value=s["gold_value"]))
        for r in rows:
            h = db.get(PfHoldingDay, (day, r["symbol"]))
            if h is None:
                db.add(PfHoldingDay(day=day, symbol=r["symbol"], qty=r["qty"], avg_price=r["avg_price"],
                                    last_price=r["last_price"], value=r["value"], pnl=r["pnl"], day_pnl=r["day_pnl"],
                                    close_price=r["close_price"], day_high=r["last_price"], day_low=r["last_price"]))
            else:
                h.qty, h.avg_price, h.last_price, h.value, h.pnl = r["qty"], r["avg_price"], r["last_price"], r["value"], r["pnl"]
                h.day_pnl, h.close_price = r["day_pnl"], r["close_price"]
                h.day_high = max(h.day_high or r["last_price"], r["last_price"])
                h.day_low = min(h.day_low or r["last_price"], r["last_price"])
        db.flush()

    @staticmethod
    def update_extremes(db, s, now: datetime) -> None:
        ts, day = now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d")
        for _, scope, metric in EXTREMES:
            scope = day if scope == "today" else scope
            v = s[metric]
            row = db.get(PfExtreme, (scope, metric))
            if row is None:
                db.add(PfExtreme(scope=scope, metric=metric, high=v, high_ts=ts, low=v, low_ts=ts))
                continue
            if v > row.high:
                row.high, row.high_ts = v, ts
            if v < row.low:
                row.low, row.low_ts = v, ts
        db.flush()

    def check_alerts(self, db, uid: int, rows, s, st: dict, now: datetime) -> list[str]:
        from ..paper.money import inr
        a = st["alerts"]
        found = []
        move = a.get("holding_day_move_pct")
        if move:
            for r in rows:
                if abs(r["day_pct"]) >= move:
                    d = "up" if r["day_pct"] > 0 else "down"
                    found.append((f"move:{r['symbol']}:{d}", f"{r['symbol']} is {d} {abs(r['day_pct']):.1f}% today "
                                  f"(LTP {r['last_price']:.2f}, day P&L {inr(r['day_pnl'])})"))
        pmove = a.get("portfolio_day_move_pct")
        if pmove and abs(s["day_pct"]) >= pmove:
            d = "up" if s["day_pct"] > 0 else "down"
            found.append((f"portfolio:{d}", f"Portfolio is {d} {abs(s['day_pct']):.1f}% today ({inr(s['day_pnl'])}); "
                          f"value {inr(s['value'])}"))
        band = a.get("gold_allocation_band_pct")
        if band and s["value"]:
            lo, hi = band
            if s["gold_pct"] < lo or s["gold_pct"] > hi:
                side = "below" if s["gold_pct"] < lo else "above"
                found.append((f"gold:{side}", f"Gold is {s['gold_pct']:.1f}% of the portfolio, {side} your {lo}–{hi}% band"))
        by = {r["symbol"]: r for r in rows}
        for lvl in a.get("price_levels", []):
            r = by.get(lvl.get("symbol"))
            if not r:
                continue
            if lvl.get("above") and r["last_price"] >= lvl["above"]:
                found.append((f"level:{r['symbol']}:above:{lvl['above']}",
                              f"{r['symbol']} crossed above {lvl['above']} (LTP {r['last_price']:.2f})"))
            if lvl.get("below") and r["last_price"] <= lvl["below"]:
                found.append((f"level:{r['symbol']}:below:{lvl['below']}",
                              f"{r['symbol']} fell below {lvl['below']} (LTP {r['last_price']:.2f})"))
        day, ts = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")
        new = []
        for key, msg in found:                     # each fires at most once a day
            if db.get(PfAlertSent, (day, key[:80])) is None:
                db.add(PfAlertSent(day=day, key=key[:80], ts=ts, message=msg[:300]))
                new.append(msg)
        if new:
            raise_alert(db, self.ctx, uid, "important", "portfolio",
                        "Portfolio alert" if len(new) == 1 else f"{len(new)} portfolio alerts", "\n".join(new),
                        path="/portfolio")
        db.flush()
        return new

    # ---- trades and daily prices -----------------------------------------------------------------------------
    def extras(self, db, kite: Kite, rows, mono: float) -> None:
        if mono - self.last["trades"] >= 900:
            self.last["trades"] = mono
            try:
                store_trades(db, trades_from_kite(kite.trades(), self.ctx.clock.now_ist()), "api")
            except Exception as e:  # noqa: BLE001
                log.warning("trade capture failed: %s", type(e).__name__)
        if mono - self.last["prices"] >= 3 * 3600:
            self.last["prices"] = mono
            try:
                self.update_prices(db, kite, rows)
            except Exception as e:  # noqa: BLE001
                log.warning("daily prices failed: %s", type(e).__name__)
        db.flush()

    def update_prices(self, db, kite: Kite, rows) -> None:
        today = self.ctx.clock.now_ist().date()
        for r in rows:
            sym, token = r["symbol"], r.get("instrument_token")
            if not token:
                continue
            first = db.scalar(select(func.min(PfTrade.day)).where(PfTrade.symbol == sym))
            last = db.scalar(select(func.max(PfPrice.day)).where(PfPrice.symbol == sym))
            start = last or first
            if not start:
                continue
            start_d = max(date.fromisoformat(start), today - timedelta(days=1900))
            self.sleep(0.4)                        # well inside Zerodha's 3 requests a second
            for c in kite.daily(token, start_d, today):
                db.merge(PfPrice(symbol=sym, day=str(c[0])[:10], close=c[4], high=c[2], low=c[3]))


def trades_from_kite(raw: list[dict], now: datetime) -> list[dict]:
    rows = []
    for t in raw:
        if t.get("exchange") not in ("NSE", "BSE"):
            continue
        ts = str(t.get("fill_timestamp") or t.get("exchange_timestamp") or t.get("order_timestamp")
                 or now.strftime("%Y-%m-%d %H:%M:%S"))[:19]
        side = str(t.get("transaction_type", "")).lower()
        qty = float(t.get("quantity") or 0)
        if side in ("buy", "sell") and qty > 0:
            rows.append({"day": ts[:10], "ts": ts, "symbol": t.get("tradingsymbol"), "isin": "",
                         "exchange": t.get("exchange"), "side": side, "qty": qty,
                         "price": float(t.get("average_price") or 0), "trade_id": norm_id(t.get("trade_id")),
                         "order_id": str(t.get("order_id") or "")})
    return rows


def store_trades(db, rows: list[dict], source: str) -> int:
    """Save trades, skipping any already stored. Returns how many were new."""
    new = 0
    for r in rows:
        key = (r["day"], r["exchange"] or "", r["trade_id"])
        if db.get(PfTrade, key) is not None:
            continue
        db.add(PfTrade(day=r["day"], exchange=r["exchange"] or "", trade_id=r["trade_id"], ts=r["ts"],
                       symbol=r["symbol"], isin=r.get("isin") or "", side=r["side"], qty=r["qty"], price=r["price"],
                       order_id=r.get("order_id") or "", source=source))
        db.flush()
        new += 1
    return new


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    quiet_http_logs()
    ctx = build_context(get_settings())
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    s = PortfolioSync(ctx)
    log.info("portfolio sync started")
    while not stop["now"]:
        try:
            s.tick()
        except Exception as e:  # noqa: BLE001
            log.error("portfolio tick failed: %s", type(e).__name__)
        _time.sleep(10)


if __name__ == "__main__":
    main()
