"""td-paper: records live 1-minute bars from the Kite WebSocket and runs the paper sessions.

Read-only: market data and GET requests only. Uses the portfolio app's daily Kite login; never logs in.
Files ($TD_HOME/paper): recorder.json (feed status), <session>/session.json (spec, status), <session>/state.json
(what the phone shows), <session>/base.pkl (engine state at the last close).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import websockets

from tradedesk_engine.data import N_MIN, minute_of
from tradedesk_marketcal import load_refs
from . import kite
from .bars import MinuteBars, instruments_from_store
from .paper import PaperSession
from . import live as livemod
import tradedesk_alerts as alerts

IST = timezone(timedelta(hours=5, minutes=30))
log = logging.getLogger("td-paper")


INDEX_NAMES = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "SENSEX": "BSE:SENSEX", "INDIAVIX": "NSE:INDIA VIX"}


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def home() -> Path:
    return Path(os.environ.get("TD_HOME", "~/Trading/tradedesk-home")).expanduser() / "paper"


def store_root() -> Path:
    return Path(os.environ.get("TD_STORE", "~/Trading/tradedesk-store")).expanduser()


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, default=str))
    tmp.replace(path)


def first_day_for(created: datetime, cal) -> str:
    """A session starts at once if created before today's close (entries only after its start minute), else next trading day."""
    d = created.date()
    if created.time() < time(15, 29) and cal.is_trading_day(d):
        return str(d)
    d += timedelta(days=1)
    while not cal.is_trading_day(d):
        d += timedelta(days=1)
    return str(d)


class Runner:
    def __init__(self):
        self.cal = load_refs()
        self.dir = home()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, PaperSession] = {}
        self.bars: dict[str, MinuteBars] = {}
        self.status = {"connected": False, "ticks": 0, "last_tick": None, "subscribed": 0, "message": "starting", "day": None}
        self.ws_task = None
        self.desk = None
        self.index_ltp: dict[int, float] = {}
        self.index_prev: dict[int, float] = {}
        self.index_ltp_tokens = {kite.INDEX_TOKENS[k] for k in ("NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX")}
        self._last_margin = 0.0
        self._last_desk_poll = 0.0

    def _status(self, **kw):
        self.status.update(kw, updated=now_ist().isoformat(timespec="seconds"))
        write_json(self.dir / "recorder.json", self.status)

    # ---------------------------------------------------------------- sessions
    def load_sessions(self):
        for f in self.dir.glob("*/session.json"):
            sid = f.parent.name
            meta = json.loads(f.read_text())
            if meta.get("status") == "stopped":
                if sid in self.sessions:
                    self.sessions.pop(sid)
                st = f.parent / "state.json"
                if st.exists():
                    s = json.loads(st.read_text())
                    if s.get("status") != "stopped":
                        s["status"] = "stopped"
                        write_json(st, s)
                continue
            if sid not in self.sessions:
                try:
                    if "first_day" not in meta:
                        meta["first_day"] = first_day_for(datetime.fromisoformat(meta["created"]), self.cal)
                        write_json(f, meta)
                    c = datetime.fromisoformat(meta["created"])
                    sm = minute_of(c.time().replace(second=0, microsecond=0)) + 1 if str(c.date()) == meta["first_day"] else 0
                    ps = PaperSession(f.parent, meta["spec"], {"name": meta.get("name"), "first_day": meta["first_day"],
                                                               "start_minute": max(0, sm)})
                    self.sessions[sid] = ps
                except Exception as e:                      # a bad spec must not stop the other sessions
                    log.exception("session %s", sid)
                    write_json(f.parent / "state.json", {"id": sid, "name": meta.get("name"), "status": "error", "error": str(e)})

    def _active(self, d: date):
        for sid, ps in self.sessions.items():
            if str(d) >= ps.meta.get("first_day", "0000"):
                yield sid, ps

    def write_states(self, d: date, until: int | None, rerun: bool):
        for sid, ps in self.sessions.items():
            b = self.bars.get(ps.underlying)
            try:
                if str(d) < ps.meta.get("first_day", "0000"):
                    st = ps.state(d, None, final=True)
                    st["status"] = f"starts {ps.meta['first_day']}"
                elif rerun and b is not None:
                    st = ps.step(d, b, until)
                else:
                    st = ps.state(d, b, until=until)
                write_json(ps.dir / "state.json", st)
                self.notify(ps, st)
            except Exception as e:
                log.exception("step %s", sid)
                write_json(ps.dir / "state.json", {"id": sid, "name": ps.meta.get("name"), "status": "error", "error": str(e)})

    # ---------------------------------------------------------------- alerts
    def notify(self, ps: PaperSession, st: dict):
        """Alert once per new fill / kill rule / day close. Remembered in alerted.json so restarts don't repeat."""
        f = ps.dir / "alerted.json"
        seen = set(json.loads(f.read_text())) if f.exists() else set()
        new = []
        name = st.get("name") or ps.dir.name
        url = f"/#/paper/{ps.dir.name}"
        for x in st.get("fills_today") or []:
            k = f"fill|{x['ts']}|{x['symbol']}|{x['side']}|{x['reason']}"
            if k in seen:
                continue
            new.append(k)
            verb = "SOLD" if x["side"] == "SELL" else "BOUGHT"
            body = f"{verb} {x['symbol']} × {x['qty']} @ ₹{x['price']} ({x['reason'].replace('_', ' ')}) · day P&L ₹{st.get('day_pnl', 0):,.0f}"
            lvl = {"entry": "info", "stop_loss": "important", "daily_loss_limit": "critical"}.get(x["reason"], "info")
            alerts.send(lvl, f"Paper · {name}", body, url)
        for kill in (st.get("gate") or {}).get("kill") or []:
            k = f"kill|{kill}"
            if k not in seen:
                new.append(k)
                alerts.send("critical", f"Paper kill rule · {name}", kill + " — stop and return to research.", url)
        if st.get("as_of") == "close" and st.get("daily"):
            d, v = st["daily"][-1]
            k = f"close|{d}"
            if k not in seen:
                new.append(k)
                g = st.get("gate") or {}
                alerts.send("important", f"Paper day closed · {name}",
                            f"{d}: ₹{v:,.0f} net · total ₹{st.get('cum_net', 0):,.0f} · {len(st.get('positions') or [])} legs open overnight · "
                            f"cycles {g.get('cycles', 0)}/20", url)
        if new:
            write_json(f, sorted(seen | set(new))[-2000:])

    def watch_feed(self, d: date):
        """Critical alert if the market-data feed is down for 3+ minutes during the session; info when it recovers."""
        n = now_ist()
        if not (time(9, 20) <= n.time() <= time(15, 25)):
            return
        lt = self.status.get("last_tick")
        stale = (not self.status.get("connected")) or not lt or (n - datetime.fromisoformat(lt)).total_seconds() > 180
        if stale and not self.status.get("feed_alerted"):
            self.status["feed_alerted"] = True
            alerts.send("critical", "Paper feed down", f"No market data since {lt[11:] if lt else 'start'} — paper sessions are not updating. "
                        f"{self.status.get('message', '')}")
        elif not stale and self.status.get("feed_alerted"):
            self.status["feed_alerted"] = False
            alerts.send("info", "Paper feed back", f"Market data flowing again at {n:%H:%M}.")

    # ---------------------------------------------------------------- market strip and margins
    def write_market(self):
        out = {"at": now_ist().isoformat(timespec="seconds"), "quotes": {}}
        for k, tok in kite.INDEX_TOKENS.items():
            if tok in self.index_ltp:
                out["quotes"][k] = {"ltp": self.index_ltp[tok], "prev_close": self.index_prev.get(tok)}
        write_json(self.dir / "market.json", out)

    def update_margins(self, key, token):
        """Zerodha basket margin for each session's open legs (B11), every 30 min in market hours."""
        for sid, ps in self.sessions.items():
            bt = ps.today or ps.base
            b = self.bars.get(ps.underlying)
            if not bt.positions or b is None:
                if ps.meta.get("margin_now"):
                    ps.meta["margin_now"] = None
                continue
            by_sym = {i.symbol: i for i in b.inst.values()}
            orders = []
            for p in bt.positions:
                i = by_sym.get(p.c.symbol)
                if i:
                    orders.append({"exchange": kite.OPT_EXCHANGE.get(i.underlying, "NFO"), "tradingsymbol": i.kite_symbol,
                                   "transaction_type": p.side, "variety": "regular", "product": "NRML", "order_type": "LIMIT",
                                   "quantity": p.qty, "price": round(p.last or p.entry_price, 2)})
            try:
                m = kite.margins_basket(key, token, orders)
                ps.meta["margin_now"] = {"final": round(m["final"]["total"]), "at": now_ist().strftime("%H:%M")}
            except Exception as e:
                log.warning("basket margin %s: %s", sid, type(e).__name__)

    # ---------------------------------------------------------------- live
    def live_sessions(self) -> dict:
        out = {}
        for sid, ps in self.sessions.items():
            try:
                m = json.loads((ps.dir / "session.json").read_text())
            except (OSError, ValueError):
                continue                                     # being written; next minute
            if m.get("mode") == "live":
                out[sid] = (ps, m)
        return out

    def disarm_all(self):
        for f in self.dir.glob("*/session.json"):
            try:
                m = json.loads(f.read_text())
                if m.get("armed"):
                    m["armed"] = False
                    livemod.atomic_json(f, m)
            except (OSError, ValueError):
                pass

    def make_desk(self, d: date, key: str, token: str):
        """The order client is built only here: live ENABLED, or legs still open in oms.db. Fresh credentials
        every trading day and whenever the login changes."""
        from tradedesk_oms.zerodha import ZerodhaAdapter
        self.creds = (key, token)
        if self.desk is not None:
            if getattr(self, "_desk_token", None) != token or self.desk.started_day != str(d):
                self._desk_token = token
                self.desk.new_day(d, ZerodhaAdapter(key, token))
            return
        db = livemod.live_dir() / "oms.db"
        if not (livemod.enabled() or livemod.has_open_legs(db)):
            return
        lim = livemod.account_limits()
        if lim is None:
            alerts.send("critical", "Live not started", "live/limits.json missing or invalid on the server — no live orders today.")
            log.error("live limits missing")
            if not livemod.has_open_legs(db):
                return
            lim = livemod.Limits(max_lots=1, max_orders_day=0, daily_loss=1e12)       # manage open legs only: no entries, no auto square-off
        self._desk_token = token
        self.desk = livemod.LiveDesk(ZerodhaAdapter(key, token), alert=lambda lvl, t, b: alerts.send(lvl, t, b, "/#/trade"),
                                     log=log.info, limits=lim, preflight=self.preflight, db=db, cal=self.cal)
        self.desk.new_day(d)
        log.info("live desk ready")

    def expiry_of(self, kite_symbol: str):
        for b in self.bars.values():
            for i in b.inst.values():
                if i.kite_symbol == kite_symbol:
                    return i.expiry
        return None

    def expiry_cutoff(self) -> str:
        try:
            return json.loads((livemod.live_dir() / "limits.json").read_text()).get("expiry_cutoff", "15:15")
        except (OSError, ValueError):
            return "15:15"

    def expiry_sweep(self, d: date):
        """Expiry day: no live leg in an expiring contract is held past the cut-off (also NRML carried in)."""
        if not self.desk or now_ist().strftime("%H:%M") < self.expiry_cutoff():
            return
        for lg in self.desk.om._legs("status IN ('open','entering')"):
            if self.expiry_of(lg["symbol"]) == d:
                self.desk.om.exit_leg(lg["id"], "expiry_cutoff")

    def preflight(self, legs) -> list[str]:
        out = []
        today = now_ist().date()
        for l in legs:
            if self.expiry_of(l.symbol) == today and now_ist().strftime("%H:%M") >= self.expiry_cutoff():
                out.append(f"{l.symbol} expires today and it is past the {self.expiry_cutoff()} cut-off")
        key, token = getattr(self, "creds", (None, None))
        lt = self.status.get("last_tick")
        if not lt or (now_ist() - datetime.fromisoformat(lt)).total_seconds() > 15:
            out.append("market data feed not fresh")
        try:
            m = kite.get("/user/margins", key, token)["data"]["equity"]
            free = float(m.get("net") or 0)
            spot = max((b.ltp.get(b.spot_token) or 0) for b in self.bars.values()) if self.bars else 0
            need = sum(spot * l.lots * l.lot_size * 0.12 for l in legs if l.side == "SELL") + \
                sum(l.price * l.lots * l.lot_size for l in legs if l.side == "BUY")
            if need and free < need:
                out.append(f"free margin ₹{free:,.0f} < about ₹{need:,.0f} needed")
        except Exception as e:
            out.append(f"margin check failed ({type(e).__name__})")
        return out

    def live_mtm(self) -> float | None:
        """Day MTM net of charges; legs without a recorded price are valued from a broker quote (never skipped)."""
        if not self.desk:
            return None
        om = self.desk.om
        ltp = {i.kite_symbol: b.ltp.get(t) for b in self.bars.values() for t, i in b.inst.items()}
        total, d = om.realised_today(), om.day()
        missing = [lg for lg in om._legs("status IN ('open','exiting')") if not ltp.get(lg["symbol"])]
        if missing:
            try:
                for lg in missing:
                    q = om.b.quote([lg["symbol"]], exchange=lg["exchange"]).get(lg["symbol"])
                    if q and q.ltp:
                        ltp[lg["symbol"]] = q.ltp
            except Exception:
                pass
        for lg in om._legs("status IN ('open','exiting')"):
            q, px = om._open_qty(lg), ltp.get(lg["symbol"])
            if q > 0 and lg["entry_avg"]:
                if not px:
                    log.warning("no price for %s in MTM", lg["symbol"])
                    continue
                total += (1 if lg["side"] == "BUY" else -1) * (px - lg["entry_avg"]) * q
        for it in om.db.execute("SELECT side, filled, avg FROM intents WHERE day=? AND filled>0", (d,)).fetchall():
            total -= self.cal.option_charges(d, it["side"], it["filled"] * it["avg"])
        return round(total, 2)

    def desk_tick(self, d: date):
        if not self.desk:
            return
        try:
            self.desk.ltp = {i.kite_symbol: b.ltp.get(t) for b in self.bars.values() for t, i in b.inst.items() if t in b.ltp}
            self.desk.commands(self.disarm_all)
            started = self.desk.ensure_started(d)
            if started:
                self.expiry_sweep(d)
            fallback = self.desk.om.L.daily_loss >= 1e12
            mtm = self.live_mtm() if started else None
            self.desk.poll(mtm if not fallback else None)
            self.desk.last_mtm = mtm
        except Exception:
            log.exception("live desk")
            try:
                self.desk.write_snapshot()
            except Exception:
                pass

    def bridge_all(self, d: date, m_now: int):
        if not self.desk or self.desk.started_day != str(d) or not self.desk.om.ready:
            return
        try:
            sessions = self.live_sessions()
        except Exception:
            log.exception("live sessions")
            return
        for sid, (ps, meta) in sessions.items():
            try:
                self.desk.bridge(sid, ps, meta, d, self.bars.get(ps.underlying), m_now)
            except Exception:
                log.exception("bridge %s", sid)

    # ---------------------------------------------------------------- feed
    def build_universe(self, d: date, key: str, token: str):
        unders = sorted({ps.underlying for _, ps in self._active(d)}) or ["NIFTY"]
        names = {u: f"{'BSE' if u == 'SENSEX' else 'NSE'}:{ {'NIFTY': 'NIFTY 50', 'BANKNIFTY': 'NIFTY BANK', 'FINNIFTY': 'NIFTY FIN SERVICE', 'SENSEX': 'SENSEX'}[u] }" for u in unders}
        q = kite.get("/quote/ohlc", key, token, [("i", v) for v in names.values()])["data"]
        try:
            iq = kite.get("/quote/ohlc", key, token, [("i", v) for v in INDEX_NAMES.values()])["data"]
            for k, v in INDEX_NAMES.items():
                if v in iq:
                    self.index_prev[kite.INDEX_TOKENS[k]] = iq[v]["ohlc"]["close"]
                    self.index_ltp.setdefault(kite.INDEX_TOKENS[k], iq[v]["last_price"])
        except Exception:
            log.warning("index prev close not loaded")
        csv_cache: dict[str, bytes] = {}
        self.bars = {}
        for u in unders:
            ref = q[names[u]]["last_price"] or q[names[u]]["ohlc"]["close"]
            ex = kite.OPT_EXCHANGE[u]
            if ex not in csv_cache:
                csv_cache[ex] = kite.get(f"/instruments/{ex}", key, token, raw=True)
            inst = [i for i in kite.parse_instruments(csv_cache[ex], u) if i.expiry >= d]
            exps = sorted({i.expiry for i in inst})[:2]
            inst = [i for i in inst if i.expiry in exps and abs(i.strike - ref) <= ref * 0.07]
            lot = self.cal.lot_size(u, exps[0]) if exps else None
            kl = {i.lot_size for i in inst}
            if lot is not None and kl and kl != {lot.value}:
                log.warning("LOT SIZE MISMATCH %s: refs.yaml %s vs Kite %s", u, lot.value, kl)
                self._status(message=f"Lot size mismatch for {u}: refs.yaml {lot.value}, Kite {sorted(kl)} — paper uses refs.yaml; fix refs.yaml")
            sp = kite.INDEX_TOKENS[u]
            loaded = MinuteBars.load(store_root(), d, u, sp, inst, kite.INDEX_TOKENS["INDIAVIX"])
            self.bars[u] = loaded
        return self.bars

    async def ws_loop(self, key: str, token: str):
        def url():
            k2, t2 = kite.credentials()                      # a new login during the day is picked up on reconnect
            return f"wss://ws.kite.trade?api_key={k2 or key}&access_token={t2 or token}"
        idx = {kite.INDEX_TOKENS[k] for k in ("NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX")} | {b.spot_token for b in self.bars.values()}
        opts = {t for b in self.bars.values() for t in b.inst}
        backoff = 2
        while True:
            try:
                async with websockets.connect(url(), ping_interval=20, max_size=2 ** 22) as ws:
                    await ws.send(json.dumps({"a": "subscribe", "v": sorted(idx | opts)}))
                    await ws.send(json.dumps({"a": "mode", "v": ["quote", sorted(idx)]}))
                    await ws.send(json.dumps({"a": "mode", "v": ["full", sorted(opts)]}))
                    self._status(connected=True, subscribed=len(idx | opts), message="recording")
                    backoff = 2
                    async for msg in ws:
                        if isinstance(msg, str):
                            continue
                        now = now_ist()
                        ticks = kite.parse_binary(msg)
                        for t in ticks:
                            if t.token in self.index_ltp_tokens:
                                self.index_ltp[t.token] = t.ltp
                            for b in self.bars.values():
                                if t.token == b.spot_token or t.token in b.inst or t.token == b.vix_token:
                                    b.on_tick(t, now)
                        if ticks:
                            self.status["ticks"] += len(ticks)
                            self.status["last_tick"] = now.isoformat(timespec="seconds")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                code = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status_code", None)
                self._status(connected=False, message=f"feed disconnected ({type(e).__name__}{' ' + str(code) if code else ''}); retrying")
                log.warning("ws: %s", type(e).__name__)              # never log the URL (it carries the token)
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)

    # ---------------------------------------------------------------- day
    async def run_day(self, d: date):
        key = token = None
        while True:                                          # wait for the morning login
            key, token = kite.credentials()
            if key and token:
                try:
                    kite.get("/user/profile", key, token)
                    break
                except Exception:
                    pass
            self._status(connected=False, message="waiting for today's Kite login on the portfolio dashboard", day=str(d))
            if now_ist().time() >= time(9, 20) and self.status.get("login_alerted") != str(d) and any(True for _ in self._active(d)):
                self.status["login_alerted"] = str(d)
                await asyncio.to_thread(alerts.send, "critical", "Kite login needed",
                                        "Paper trading is waiting: log in to Kite on the portfolio dashboard.")
            if now_ist().time() > time(15, 30):
                self.record_day(d, "no Kite login today")
                return False
            await asyncio.sleep(60)
        self.load_sessions()
        await asyncio.to_thread(self.build_universe, d, key, token)
        self.status["day"] = str(d)
        self.ws_task = asyncio.create_task(self.ws_loop(key, token))
        self.make_desk(d, key, token)
        last_min = -1
        try:
            while True:
                now = now_ist()
                m_now = minute_of(now.time().replace(second=0, microsecond=0))
                if now.time() >= time(15, 30, 8):
                    break
                if m_now != last_min and now.second >= 2 and 0 < m_now <= N_MIN:
                    self.load_sessions()
                    for b in self.bars.values():
                        await asyncio.to_thread(b.flush, store_root())
                    await asyncio.to_thread(self.write_states, d, m_now, True)
                    await asyncio.to_thread(self.bridge_all, d, m_now)
                    await asyncio.to_thread(self.watch_feed, d)
                    last_min = m_now
                elif m_now > 0:
                    await asyncio.to_thread(self.write_states, d, m_now, False)   # live LTP between minutes (every second)
                    self.write_market()
                    if int(_time.time()) % 5 == 0:
                        self._status()
                if _time.time() - self._last_margin > 1800 and m_now > 0:
                    self._last_margin = _time.time()
                    await asyncio.to_thread(self.update_margins, key, token)
                if m_now % 2 == 0 and now.second < 2:
                    nk, nt = kite.credentials()                      # new login during the day → fresh adapter
                    if nt and nt != token:
                        key, token = nk, nt
                    self.make_desk(d, key, token)
                if self.desk and _time.time() - self._last_desk_poll >= 2:
                    self._last_desk_poll = _time.time()
                    await asyncio.to_thread(self.desk_tick, d)
                await asyncio.sleep(1)
            while self.desk and now_ist().time() < time(15, 36):   # let closing orders finish
                await asyncio.to_thread(self.desk_tick, d)
                await asyncio.sleep(2)
        finally:
            self.ws_task.cancel()
        for b in self.bars.values():
            b.flush(store_root())
        self.load_sessions()
        for sid, ps in list(self._active(d)):
            b = self.bars.get(ps.underlying)
            if b is not None:
                try:
                    st = ps.close_day(d, b); write_json(ps.dir / "state.json", st); self.notify(ps, st)
                except Exception as e:
                    log.exception("close %s", sid)
        self.record_day(d)
        self._status(connected=False, message=f"closed {d}; next session tomorrow")
        return True

    def catch_up(self, d: date):
        """If the service was down at the close, book the day from the bars recorded so far."""
        for sid, ps in list(self._active(d)):
            if (ps.meta.get("last_day") or "") >= str(d):
                continue
            inst = instruments_from_store(store_root(), d, ps.underlying)
            if not inst:
                continue
            b = MinuteBars.load(store_root(), d, ps.underlying, kite.INDEX_TOKENS[ps.underlying], inst, kite.INDEX_TOKENS["INDIAVIX"])
            try:
                st = ps.close_day(d, b); write_json(ps.dir / "state.json", st); self.notify(ps, st)
                log.info("caught up %s for %s", sid, d)
            except Exception:
                log.exception("catch-up %s", sid)

    def record_day(self, d: date, why: str | None = None):
        """G2: one line per trading day — was the day recorded? (feeds the health light)."""
        f = self.dir / "recordings.json"
        try:
            days = json.loads(f.read_text())
        except (OSError, ValueError):
            days = {}
        if why is None:
            n = max((len(b.bars.get(b.spot_token, {})) for b in self.bars.values()), default=0)
            ok = n >= 300
            why = f"{n} of 375 index minutes recorded" + ("" if ok else " (feed down part of the day)")
        else:
            ok = False
        days[str(d)] = {"ok": ok, "why": why}
        write_json(f, dict(sorted(days.items())[-60:]))
        if not ok:
            alerts.send("important", "Recording incomplete", f"{d}: {why}")

    async def main(self):
        self._status(message="idle")
        while True:
            n = now_ist()
            d = n.date()
            self.load_sessions()
            if self.cal.is_trading_day(d) and time(9, 8) <= n.time() < time(15, 30) and self.status.get("closed") != str(d):
                await self.run_day(d)
                self.status["closed"] = str(d)
            else:
                if self.cal.is_trading_day(d) and n.time() >= time(15, 31):
                    self.catch_up(d)
                self.write_states(d, None, False)
                self._status(message="market closed" if not self.cal.is_trading_day(d) or n.time() >= time(15, 30) else "waiting for 09:08")
            await asyncio.sleep(30)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(Runner().main())
