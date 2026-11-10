"""The recorder service: `python -m tdserver.recorder` (its own systemd unit, so trading never waits on it).

Each trading day it waits for the pre-open, takes the owner's Kite session for today (read-only), picks the
instruments, follows the price stream and writes 1-minute and 1-second bars every 15 seconds until the F&O
close. It never places, changes or cancels an order. Problems become alerts: no Kite login by 09:10, the
stream down, or no prices for a minute during market hours.
"""
from __future__ import annotations

import logging
import signal
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from ..alerts.outbox import raise_alert, resolve
from ..clock import IST
from ..context import AppContext
from ..kite import access_token, secrets_for
from ..models import BrokerAccount, ServiceStatus, Setting, User
from .books import Book
from .stream import KiteStream
from .ticks import parse_frame
from .universe import Dumps, indexes, options_around

log = logging.getLogger("tdserver.recorder")

DEFAULTS = {"underlyings": ["NIFTY", "BANKNIFTY", "SENSEX"], "expiries": 2, "strikes_each_side": 30,
            "record_seconds": True, "keep_seconds_days": 5}
FLUSH_EVERY = 15          # seconds between writes
REBAND_EVERY = 60         # seconds between checks that the strike band still follows the index
STALE_AFTER = 60          # seconds without a price in market hours before an alert
START_BEFORE = timedelta(minutes=2)   # connect this long before the open


def settings(db, ctx: AppContext) -> dict:
    row = db.get(Setting, "recorder")
    s = {**DEFAULTS, **((row.value if row else None) or {})}
    s["expiries"] = max(1, min(4, int(s["expiries"])))
    s["strikes_each_side"] = max(0, min(60, int(s["strikes_each_side"])))
    s["keep_seconds_days"] = max(1, min(60, int(s["keep_seconds_days"])))
    s["underlyings"] = [u for u in s["underlyings"] if u in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY")]
    s["record_seconds"] = bool(s["record_seconds"])
    return s


def owner_session(db, ctx: AppContext) -> tuple[int, str, str] | None:
    """(user id, api key, today's access token) of the first owner logged in to Kite today."""
    for acc in db.scalars(select(BrokerAccount).join(User, User.id == BrokerAccount.user_id)
                          .where(User.role == "owner", User.disabled_at.is_(None))):
        tok = access_token(ctx, acc)
        if tok:
            api_key, _ = secrets_for(ctx, acc)
            return acc.user_id, api_key, tok
    return None


def owners(db) -> list[int]:
    return [u.id for u in db.scalars(select(User).where(User.role == "owner", User.disabled_at.is_(None)))]


def put_status(db, ctx: AppContext, name: str, **data) -> None:
    row = db.get(ServiceStatus, name)
    if row is None:
        db.add(ServiceStatus(name=name, updated_at=ctx.clock.now(), data=data))
    else:
        row.updated_at, row.data = ctx.clock.now(), data


@dataclass
class DayState:
    day: date
    m1: Book
    s1: Book | None
    tokens: dict[int, str] = field(default_factory=dict)
    spot_token: dict[str, int] = field(default_factory=dict)
    last_tick_epoch: float = 0.0
    ticks: int = 0
    gaps: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class Recorder:
    def __init__(self, ctx: AppContext, fetch_dump=None, stream_factory=None):
        self.ctx = ctx
        self.fetch_dump = fetch_dump or self._kite_dump
        self.stream_factory = stream_factory or (lambda key, tok, on_frame, on_state:
                                                 KiteStream(key, tok, on_frame, on_state))
        self.state: DayState | None = None
        self.stream = None
        self._last_flush = 0.0       # clock epochs (the injectable clock, so tests can move time)
        self._last_band = 0.0
        self._listings = None
        self._settings = DEFAULTS
        self._auth: tuple[str, str] | None = None

    # ---- Zerodha instruments list ----------------------------------------------------------------------
    def _kite_dump(self, exchange: str) -> str:
        api_key, token = self._auth
        r = self.ctx.http.get(f"{self.ctx.settings.kite_api_base}/instruments/{exchange}",
                              headers={"X-Kite-Version": "3", "Authorization": f"token {api_key}:{token}"}, timeout=60)
        r.raise_for_status()
        return r.text

    # ---- one tick of the service loop (called every second) ----------------------------------------------
    def step(self) -> str:
        """Returns what it is doing, for the status row and tests."""
        ctx = self.ctx
        now = ctx.clock.now_ist()
        today = now.date()
        cal = ctx.calendar
        if not cal.is_trading_day(today):
            return self._idle("closed today")
        sess = cal.session(today)
        start = datetime.combine(today, sess.open, IST) - START_BEFORE
        end = datetime.combine(today, sess.fo_close, IST) + timedelta(seconds=30)
        if now < start:
            self._check_login_reminder(now, sess)
            return self._idle("waiting for the open")
        if now >= end:
            if self.state is not None:
                self._finish("closed")
            return self._idle("done for today")
        if self.state is None or self.state.day != today:
            return self._begin(today, sess)
        self._tick_service(now, sess)
        return "recording"

    def _idle(self, why: str) -> str:
        with self.ctx.sessions() as db:
            put_status(db, self.ctx, "recorder", state="idle", note=why)
            db.commit()
        return why

    def _check_login_reminder(self, now: datetime, sess) -> None:
        # the worker sends the 08:30/08:45 reminders; at 09:10 the recorder adds that it cannot start
        if now.time() >= time(9, 10):
            with self.ctx.sessions() as db:
                if owner_session(db, self.ctx) is None:
                    for uid in owners(db):
                        raise_alert(db, self.ctx, uid, "critical", "market_data",
                                    "Prices will not be recorded today", "No Kite login yet. Log in to Zerodha now.",
                                    "broker/kite", dedupe_key=f"recorder_nologin:{now.date()}", once=True)
                    db.commit()

    def _begin(self, today: date, sess) -> str:
        ctx = self.ctx
        with ctx.sessions() as db:
            self._settings = settings(db, ctx)
            auth = owner_session(db, ctx)
            if auth is None:
                put_status(db, ctx, "recorder", state="waiting", note="No Kite login for today")
                self._check_login_reminder(ctx.clock.now_ist(), sess)
                db.commit()
                return "waiting for Kite login"
            _, api_key, token = auth
            self._auth = (api_key, token)
            resolve(db, ctx, auth[0], "recorder_nologin")
            db.commit()
        root = self._root()
        try:
            self._listings = Dumps(root, self.fetch_dump).load(today)
        except Exception as e:  # noqa: BLE001
            return self._idle(f"instruments list failed ({type(e).__name__}); retrying")
        idx = indexes(self._listings)
        wanted = [idx[u] for u in self._settings["underlyings"] + ["INDIAVIX"] if u in idx]
        m1 = Book.load(root, today, 60, sess.fo_close, wanted)
        s1 = Book.load(root, today, 1, sess.fo_close, wanted) if self._settings["record_seconds"] else None
        st = DayState(today, m1, s1)
        st.spot_token = {i.underlying: i.token for i in wanted}
        for i in wanted:
            st.tokens[i.token] = i.symbol
        self.state = st
        self.stream = self.stream_factory(api_key, token, self.on_frame, self.on_state)
        self.stream.subscribe(list(st.tokens))
        self.stream.start()
        self._last_band = 0.0
        log.info("recording %s: %d instruments", today, len(st.tokens))
        return "started"

    def _root(self):
        from pathlib import Path
        if not self.ctx.settings.store_root:
            raise RuntimeError("TD2_STORE_ROOT is not set")
        return Path(self.ctx.settings.store_root).expanduser()

    # ---- prices -----------------------------------------------------------------------------------------
    def on_frame(self, msg: bytes, received: float) -> None:
        st = self.state
        if st is None:
            return
        for t in parse_frame(msg):
            at = datetime.fromtimestamp(t.exchange_ts or received, IST).replace(tzinfo=None)
            with st.lock:
                st.m1.on_tick(t, at)
                if st.s1 is not None:
                    st.s1.on_tick(t, at)
                st.ticks += 1
                st.last_tick_epoch = received

    def on_state(self, what: str, detail: str) -> None:
        st = self.state
        if st is not None and what == "disconnected":
            st.gaps.append(f"{self.ctx.clock.now_ist():%H:%M:%S} stream lost ({detail or 'closed'})")

    # ---- every second while recording ------------------------------------------------------------------
    def _tick_service(self, now: datetime, sess) -> None:
        st = self.state
        t = self.ctx.clock.now().timestamp()
        if t - self._last_band >= REBAND_EVERY:
            self._last_band = t
            self._reband(st)
        if t - self._last_flush >= FLUSH_EVERY:
            self._last_flush = t
            self.flush()
            self._watch(now, sess)

    def _reband(self, st: DayState) -> None:
        """Keep the option band centred on each index as it moves (new strikes are added, none dropped)."""
        new = []
        with st.lock:
            for u, tok in st.spot_token.items():
                if u == "INDIAVIX":
                    continue
                bars = st.m1.bars.get(tok)
                if not bars:
                    continue
                spot = bars[max(bars)].c
                for inst in options_around(self._listings, u, spot, st.day, self._settings["expiries"],
                                           self._settings["strikes_each_side"]):
                    if inst.token not in st.tokens:
                        st.tokens[inst.token] = inst.symbol
                        new.append(inst)
            st.m1.add_instruments(new)
            if st.s1 is not None:
                st.s1.add_instruments(new)
        if new and self.stream is not None:
            self.stream.subscribe([i.token for i in new])

    def flush(self) -> int:
        st = self.state
        if st is None:
            return 0
        root = self._root()
        n = 0
        with st.lock:
            try:
                if st.m1.dirty:
                    n += st.m1.write(root)
                if st.s1 is not None and st.s1.dirty:
                    n += st.s1.write(root)
            except Exception as e:  # noqa: BLE001
                st.gaps.append(f"{self.ctx.clock.now_ist():%H:%M:%S} write failed ({type(e).__name__})")
        return n

    def _watch(self, now: datetime, sess) -> None:
        st = self.state
        age = self.ctx.clock.now().timestamp() - st.last_tick_epoch if st.last_tick_epoch else None
        in_market = datetime.combine(st.day, sess.open, IST) + timedelta(seconds=30) <= now < \
            datetime.combine(st.day, sess.fo_close, IST)
        stale = in_market and (age is None or age > STALE_AFTER)
        with self.ctx.sessions() as db:
            put_status(db, self.ctx, "recorder", state="recording", day=st.day.isoformat(),
                       indexes=self._index_quotes(st),
                       instruments=len(st.tokens), ticks=st.ticks,
                       last_tick=datetime.fromtimestamp(st.last_tick_epoch, IST).isoformat() if st.last_tick_epoch else None,
                       connected=bool(getattr(self.stream, "connected", False)),
                       reconnects=getattr(self.stream, "reconnects", 0), gaps=st.gaps[-20:], stale=stale,
                       seconds=st.s1 is not None)
            for uid in owners(db):
                if stale:
                    raise_alert(db, self.ctx, uid, "important", "market_data", "No live prices",
                                f"No price from Zerodha for {int(age) if age else 60}+ seconds. Recording is paused.",
                                "data", dedupe_key="feed_stale")
                else:
                    resolve(db, self.ctx, uid, "feed_stale")
            db.commit()

    @staticmethod
    def _index_quotes(st: DayState) -> dict:
        out = {}
        for u, tok in st.spot_token.items():
            bars = st.m1.bars.get(tok)
            if bars:
                last = bars[max(bars)]
                first = bars[min(bars)]
                out[u] = {"ltp": last.c, "open": first.o, "high": max(b.h for b in bars.values()),
                          "low": min(b.l for b in bars.values()),
                          "at": (st.m1.start + timedelta(seconds=max(bars) * 60)).replace(tzinfo=IST).isoformat()}
        return out

    def _finish(self, why: str) -> None:
        self.flush()
        if self.stream is not None:
            self.stream.stop()
        st = self.state
        with self.ctx.sessions() as db:
            put_status(db, self.ctx, "recorder", state="finished", day=st.day.isoformat(), ticks=st.ticks,
                       instruments=len(st.tokens), gaps=st.gaps[-20:], note=why)
            db.commit()
        self.state, self.stream = None, None


def main() -> None:
    from ..context import build_context, quiet_http_logs
    from ..settings import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    quiet_http_logs()
    ctx = build_context(get_settings())
    rec = Recorder(ctx)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    while not stop["now"]:
        try:
            rec.step()
        except Exception as e:  # noqa: BLE001
            log.error("recorder step failed: %s", type(e).__name__)
        _time.sleep(1)
    if rec.state is not None:
        rec._finish("service stopped")


if __name__ == "__main__":
    main()
