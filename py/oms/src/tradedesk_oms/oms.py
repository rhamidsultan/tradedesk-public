"""OrderManager: one per broker account, single writer for all its orders (architecture §6).

Rules enforced here:
- Opening orders pass guards (armed, reconciled, broker OK, not locked, session time, lots, freeze quantity,
  orders/day, worst case at the SL limit within the remaining daily loss, runner preflight: feed/margin/circuits).
  Closing orders are never blocked.
- LIMIT and SL only. Every entry fill (also partial) gets a protective SL at the broker sized to the filled quantity.
- Exits convert the existing SL order into a LIMIT exit (no second order, no flip). If the broker refuses that
  modify, the SL is cancelled, the cancel confirmed, then a LIMIT for the remaining quantity is placed.
- Quantity still open on a leg = entry fills − fills of every closing order of that leg (never forgotten), and is
  capped by the broker's real position before any closing order is sent.
- Chasers: every 5 s to the touch, after 6 tries (or 30 s for exits) touch ±2%; before 20 modifications the order
  is cancelled and replaced.
- Multi-leg entry is sequential; reject, timeout or partial fill squares off what filled and locks the session.
- Intent rows are written (SENDING, with tag) before the HTTP call; unresolved rows are matched by tag for the
  whole day, so a late order is adopted and protected. Reconciliation at start, every 60 s and after fills; a
  mismatch resizes broker SLs to the real quantity, locks the account and alerts.
- Kill switch and daily loss mark every leg 'exiting' first, then try each leg independently; poll() keeps
  driving them until flat. A repeated kill re-drives exits.
- Legs carried overnight get a fresh protective SL at start() before trading is allowed.
"""
from __future__ import annotations

import json
import sqlite3
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .broker import TERMINAL, Broker, BrokerError, NetworkUnknown, OrderReq, OrderState, RateLimited, Rejected, TokenExpired

TICK = 0.05
IST = timezone(timedelta(hours=5, minutes=30))
WORKING = ("OPEN", "TRIGGER PENDING", "PENDING")


def tick(x: float, up: bool) -> float:
    n = round(x / TICK, 6)
    n = int(n) + (1 if up and n != int(n) else 0)
    return round(max(TICK, n * TICK), 2)


class GuardBlocked(Exception):
    pass


@dataclass
class Limits:
    max_lots: int
    max_orders_day: int
    daily_loss: float
    sl_buffer_pct: float = 10.0
    entry_window: tuple = ("09:16", "15:00")      # opening orders only inside this IST window
    chase_every: float = 5.0
    chase_tries: int = 6
    entry_timeout: float = 30.0
    exit_chase_secs: float = 30.0
    resolve_after: float = 30.0
    reconcile_every: float = 60.0
    orders_per_sec: int = 8
    max_mods: int = 20


@dataclass
class LegReq:
    symbol: str
    exchange: str
    side: str                # entry side
    lots: int
    lot_size: int            # from the broker's instruments dump for this contract
    price: float             # entry limit
    sl_trigger: float | None  # premium trigger of the protective stop (None only for a long hedge next to shorts)
    product: str = "NRML"
    freeze_qty: int | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS intents(tag TEXT PRIMARY KEY, day TEXT, session TEXT, leg INTEGER, kind TEXT, symbol TEXT,
  exchange TEXT, side TEXT, qty INTEGER, order_type TEXT, price REAL, trig REAL, status TEXT, order_id TEXT,
  filled INTEGER DEFAULT 0, avg REAL DEFAULT 0, bstatus TEXT, msg TEXT, mods INTEGER DEFAULT 0, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS legs(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, session TEXT, seq INTEGER, symbol TEXT,
  exchange TEXT, product TEXT, side TEXT, qty INTEGER, price REAL, sl_trigger REAL, status TEXT, filled INTEGER DEFAULT 0,
  entry_avg REAL, exit_avg REAL, entry_tag TEXT, sl_tag TEXT, exit_tag TEXT, reason TEXT,
  chase_n INTEGER DEFAULT 0, chase_at REAL, exit_started REAL, entry_sent REAL, command TEXT);
CREATE TABLE IF NOT EXISTS locks(day TEXT, scope TEXT, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, at REAL);
CREATE TABLE IF NOT EXISTS events(at REAL, level TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS seq(n INTEGER);
"""


class OrderManager:
    def __init__(self, broker: Broker, db: Path | str, limits: Limits,
                 alert: Callable[[str, str, str], None] | None = None, clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 preflight: Callable[[list], list[str]] | None = None, log: Callable[[str], None] | None = None):
        if limits is None or not isinstance(limits, Limits):
            raise ValueError("explicit Limits are required")
        self.b = broker
        self.L = limits
        self.alert_fn = alert or (lambda *a: None)
        self.log = log or (lambda s: None)
        self.preflight = preflight          # runner: feed fresh, margin, circuit limits → list of reasons to block
        self.clock = clock or _time.time
        self.sleep = sleep or _time.sleep
        self.db = sqlite3.connect(str(db), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if not self.db.execute("SELECT 1 FROM seq").fetchone():
            self.db.execute("INSERT INTO seq VALUES (0)")
        self.armed = False
        self.ready = False
        self.broker_ok = True
        self._last_reconcile = 0.0
        self._sent: list[float] = []
        self._book: dict[str, OrderState] = {}
        self._loss_done_day = None

    # ------------------------------------------------------------------ basics
    def now_ist(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), IST)

    def day(self) -> str:
        return self.now_ist().strftime("%Y-%m-%d")

    def event(self, level: str, text: str):
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (self.clock(), level, text))
        self.log(f"[{level}] {text}")
        if level in ("important", "critical"):
            try:
                self.alert_fn(level, "Live orders", text)
            except Exception as e:
                self.log(f"alert failed: {type(e).__name__}")

    def locked(self, session: str | None = None) -> str | None:
        r = self.db.execute("SELECT reason FROM locks WHERE day=? AND (scope='account' OR scope=?)",
                            (self.day(), session or "")).fetchone()
        return r[0] if r else None

    def lock(self, scope: str, reason: str):
        if not self.db.execute("SELECT 1 FROM locks WHERE day=? AND scope=?", (self.day(), scope)).fetchone():
            self.db.execute("INSERT INTO locks VALUES (?,?,?,?)", (self.day(), scope, reason, self.clock()))
            self.event("critical", f"Locked {scope} for today: {reason}")

    def _tag(self) -> str:
        n = self.db.execute("UPDATE seq SET n = n + 1 RETURNING n").fetchone()[0]
        return f"TD{self.now_ist():%y%m%d}{n:08d}"

    def _throttle(self):
        now = self.clock()
        self._sent = [t for t in self._sent if now - t < 1.0]
        if len(self._sent) >= self.L.orders_per_sec:
            self.sleep(1.0 - (now - self._sent[0]) + 0.01)
        self._sent.append(self.clock())

    def _call(self, fn, *a, **k):
        """Broker call with throttle; 429 retried with backoff (never dropped); token errors pause entries."""
        for i in range(5):
            self._throttle()
            try:
                r = fn(*a, **k)
                self.broker_ok = True
                return r
            except RateLimited:
                self.sleep(0.3 * (i + 1))
            except TokenExpired:
                if self.broker_ok:
                    self.broker_ok = False
                    self.event("critical", "Kite login expired or invalid: new entries paused; stops at Kite keep working. Log in again.")
                raise
        raise RateLimited("rate limited 5 times")

    def _send(self, kind: str, session: str, leg: int, fields: dict) -> str:
        """Write the intent (SENDING) before the HTTP call; then SENT / FAILED. Returns the tag."""
        tag = self._tag()
        req = OrderReq(tag=tag, **fields)
        now = self.clock()
        self.db.execute("INSERT INTO intents(tag, day, session, leg, kind, symbol, exchange, side, qty, order_type, price, trig, status, created, updated)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'SENDING',?,?)",
                        (tag, self.day(), session, leg, kind, req.symbol, req.exchange, req.side, req.qty, req.order_type,
                         req.price, req.trigger, now, now))
        try:
            oid = self._call(self.b.place, req)
        except NetworkUnknown:
            self.event("important", f"{kind} order {tag} sent but no reply; matching it by tag in the order book")
            return tag
        except BrokerError as e:
            self.db.execute("UPDATE intents SET status='FAILED', msg=?, updated=? WHERE tag=?", (str(e)[:200], self.clock(), tag))
            raise
        self.db.execute("UPDATE intents SET status='SENT', order_id=?, updated=? WHERE tag=?", (oid, self.clock(), tag))
        return tag

    def _intent(self, tag):
        return self.db.execute("SELECT * FROM intents WHERE tag=?", (tag,)).fetchone() if tag else None

    def _legs(self, where: str = "1", args=()):
        return self.db.execute(f"SELECT * FROM legs WHERE {where} ORDER BY id", args).fetchall()

    def _leg(self, lid):
        return self._legs("id=?", (lid,))[0]

    def _set_leg(self, lid: int, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE legs SET {cols} WHERE id=?", (*kw.values(), lid))

    @staticmethod
    def _close_side(side: str) -> str:
        return "BUY" if side == "SELL" else "SELL"

    def _sl_limit(self, close_side: str, trig: float) -> float:
        b = self.L.sl_buffer_pct / 100
        return tick(trig * (1 + b), True) if close_side == "BUY" else tick(trig * (1 - b), False)

    def _entry_filled(self, lg) -> int:
        it = self._intent(lg["entry_tag"])
        return max(lg["filled"], it["filled"] if it else 0)

    def _closed_qty(self, lid: int) -> int:
        """Fills of every closing order ever sent for this leg (SLs and exits, all attempts)."""
        r = self.db.execute("SELECT coalesce(sum(filled),0) FROM intents WHERE leg=? AND kind IN ('sl','exit')", (lid,)).fetchone()
        return int(r[0])

    def _open_qty(self, lg) -> int:
        return self._entry_filled(lg) - self._closed_qty(lg["id"])

    def _working(self, tag) -> OrderState | None:
        o = self._order(self._intent(tag))
        return o if o and o.status not in TERMINAL else None

    # ------------------------------------------------------------------ order book
    def _order(self, it) -> OrderState | None:
        if not it or not it["order_id"]:
            return None
        if it["order_id"] not in self._book:
            self._sync(self._call(self.b.orders))
        return self._book.get(it["order_id"])

    def _sync(self, book: list[OrderState]):
        """Match every not-final intent (any day) with the broker book; adopt late orders by tag."""
        self._book = {o.order_id: o for o in book}
        by_tag = {o.tag: o for o in book}
        rows = self.db.execute("SELECT * FROM intents WHERE status IN ('SENT','SENDING') OR "
                               "(status='FAILED' AND msg='not found at broker' AND day=?)", (self.day(),)).fetchall()
        for it in rows:
            o = self._book.get(it["order_id"]) or by_tag.get(it["tag"])
            if o:
                if it["status"] == "FAILED":
                    self.event("critical", f"{it['kind']} order {it['tag']} appeared late at the broker; adopting and protecting it")
                self.db.execute("UPDATE intents SET order_id=?, status='SENT', filled=?, avg=?, bstatus=?, msg=?, updated=? WHERE tag=?",
                                (o.order_id, o.filled, o.avg, o.status, o.message, self.clock(), it["tag"]))
            elif it["status"] == "SENT" and it["day"] != self.day():
                # an earlier day's order is gone from today's book: the broker expired it at end of day
                self.db.execute("UPDATE intents SET status='EXPIRED', bstatus='CANCELLED', updated=? WHERE tag=?", (self.clock(), it["tag"]))

    def _resolve_sending(self):
        for it in self.db.execute("SELECT * FROM intents WHERE status='SENDING'").fetchall():
            if self.clock() - it["created"] > self.L.resolve_after:
                self.db.execute("UPDATE intents SET status='FAILED', msg='not found at broker', updated=? WHERE tag=?", (self.clock(), it["tag"]))
                self.event("important", f"{it['kind']} order {it['tag']} not found at the broker after {self.L.resolve_after:.0f} s")

    # ------------------------------------------------------------------ start / reconcile
    def start(self) -> bool:
        """After a restart (or each morning): sync, resolve, re-protect carried legs, reconcile. Only then trade."""
        self.ready = False
        self._sync(self._call(self.b.orders))
        self._resolve_sending()
        if self.db.execute("SELECT 1 FROM intents WHERE status='SENDING'").fetchone():
            self.event("important", "waiting to match orders sent before the restart; trading stays off until then")
            return False
        for lg in self._legs("status='open'"):
            if self._open_qty(lg) > 0 and lg["sl_trigger"] is not None and not self._working(lg["sl_tag"]):
                self.event("important", f"{lg['symbol']} carried without a working SL; placing a fresh one")
                self._place_sl(lg, self._open_qty(lg))
        ok = self.reconcile()
        self.ready = ok
        return ok

    def expected_positions(self) -> dict[str, int]:
        exp: dict[str, int] = {}
        for lg in self._legs("status IN ('entering','open','exiting')"):
            q = self._open_qty(lg)
            exp[lg["symbol"]] = exp.get(lg["symbol"], 0) + (q if lg["side"] == "BUY" else -q)
        return {k: v for k, v in exp.items() if v}

    def reconcile(self) -> bool:
        self._last_reconcile = self.clock()
        for attempt in range(2):                 # a fill may land between the two reads: look twice
            self._sync(self._call(self.b.orders))
            actual = self._call(self.b.positions)
            exp = self.expected_positions()
            if actual == exp:
                return True
        diff = {s: (exp.get(s, 0), actual.get(s, 0)) for s in set(exp) | set(actual) if exp.get(s, 0) != actual.get(s, 0)}
        for lg in self._legs("status IN ('open','exiting')"):
            real = actual.get(lg["symbol"], 0)
            want = abs(real) if (real < 0) == (lg["side"] == "SELL") else 0
            for tag in {lg["sl_tag"], lg["exit_tag"]} - {None}:
                o = self._working(tag)
                if o and o.qty - o.filled > want:
                    try:
                        if want == 0:
                            self._call(self.b.cancel, o.order_id)
                        else:
                            self._call(self.b.modify, o.order_id, qty=o.filled + want)
                    except BrokerError as e:
                        self.event("critical", f"could not resize {tag}: {e}")
        self.lock("account", f"position mismatch {json.dumps(diff)} (expected, at broker)")
        self.ready = False
        return False

    # ------------------------------------------------------------------ entry
    def enter(self, session: str, legs: list[LegReq], command_id: str | None = None,
              session_limits: dict | None = None, add_to: bool = False) -> list[int]:
        """session_limits (from the strategy spec): max_lots, daily_loss, entry_window — the tighter of these and the
        account limits applies."""
        if command_id and self.db.execute("SELECT 1 FROM commands WHERE id=?", (command_id,)).fetchone():
            return [l["id"] for l in self._legs("command=?", (command_id,))]
        sl_ = session_limits or {}
        max_lots = min(self.L.max_lots, int(sl_.get("max_lots", self.L.max_lots)))
        loss_cap = min(self.L.daily_loss, float(sl_.get("daily_loss", self.L.daily_loss)))
        win = (max(self.L.entry_window[0], sl_.get("entry_window", ("00:00", "99:99"))[0]),
               min(self.L.entry_window[1], sl_.get("entry_window", ("00:00", "99:99"))[1]))
        reasons = []
        if not self.armed: reasons.append("live is not armed")
        if not self.ready: reasons.append("not reconciled")
        if not self.broker_ok: reasons.append("broker login not valid")
        lk = self.locked(session)
        if lk: reasons.append(f"locked today: {lk}")
        t = self.now_ist().strftime("%H:%M")
        if not (win[0] <= t < win[1]):
            reasons.append(f"outside entry window {win[0]}–{win[1]}")
        if not add_to and self._legs("session=? AND status IN ('entering','open','exiting','pending')", (session,)):
            reasons.append("session already has open legs")
        open_shorts = [r for r in self._legs("session=? AND side='SELL' AND status IN ('entering','open')", (session,))]
        busy = {r["symbol"] for r in self._legs("status IN ('entering','open','exiting','pending')")}
        for l in legs:
            if l.symbol in busy: reasons.append(f"{l.symbol} already held by another session")
            if l.lots < 1 or l.lots > max_lots: reasons.append(f"{l.symbol}: {l.lots} lots > max {max_lots}")
            if l.lot_size < 1: reasons.append(f"{l.symbol}: unknown lot size")
            if l.freeze_qty and l.lots * l.lot_size > l.freeze_qty: reasons.append(f"{l.symbol}: quantity above freeze limit")
            hedge = l.sl_trigger is None
            kind = l.symbol[-2:]
            short_qty = sum(x.lots * x.lot_size for x in legs if x.side == "SELL" and x.symbol[-2:] == kind) + \
                sum(r["qty"] for r in open_shorts if r["symbol"][-2:] == kind)
            if hedge and not (l.side == "BUY" and short_qty >= l.lots * l.lot_size):
                reasons.append(f"{l.symbol}: every leg needs a stop (only a long {kind} hedge covering a short {kind} may go without)")
                continue
            if l.side not in ("BUY", "SELL") or l.price <= 0 or (not hedge and l.sl_trigger <= 0): reasons.append(f"{l.symbol}: bad leg")
            if not hedge and l.side == "SELL" and l.sl_trigger <= l.price: reasons.append(f"{l.symbol}: SL trigger must be above a short entry")
            if not hedge and l.side == "BUY" and l.sl_trigger >= l.price: reasons.append(f"{l.symbol}: SL trigger must be below a long entry")
        sent_today = self.db.execute("SELECT count(*) FROM intents WHERE day=? AND kind='entry'", (self.day(),)).fetchone()[0]
        if sent_today + len(legs) > self.L.max_orders_day:
            reasons.append(f"max {self.L.max_orders_day} opening orders a day")
        worst = sum((abs(self._sl_limit(self._close_side(l.side), l.sl_trigger) - l.price) if l.sl_trigger is not None
                     else l.price) * l.lots * l.lot_size for l in legs)          # a hedge can lose its whole premium
        budget = loss_cap + min(0.0, self.realised_today())
        if worst > budget:
            reasons.append(f"worst case at SL limit ₹{worst:,.0f} > remaining daily loss ₹{budget:,.0f}")
        if self.preflight and not reasons:
            reasons += self.preflight(legs)
        if reasons:
            self.event("important", f"entry blocked for {session}: " + "; ".join(reasons))
            raise GuardBlocked("; ".join(reasons))
        if command_id:
            self._new_command(command_id)
        ids = []
        for i, l in enumerate(legs):
            cur = self.db.execute("INSERT INTO legs(day, session, seq, symbol, exchange, product, side, qty, price, sl_trigger, status, command)"
                                  " VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                                  (self.day(), session, i, l.symbol, l.exchange, l.product, l.side, l.lots * l.lot_size, l.price, l.sl_trigger, command_id))
            ids.append(cur.lastrowid)
        self._next_leg(session)
        return ids

    def _new_command(self, cid: str) -> bool:
        try:
            self.db.execute("INSERT INTO commands VALUES (?,?)", (cid, self.clock()))
            return True
        except sqlite3.IntegrityError:
            return False

    def _next_leg(self, session: str):
        if self.locked(session) or self._legs("session=? AND status='entering'", (session,)):
            return
        nxt = self._legs("session=? AND status='pending'", (session,))
        if not nxt:
            return
        lg = nxt[0]
        try:
            tag = self._send("entry", session, lg["id"], dict(symbol=lg["symbol"], exchange=lg["exchange"], side=lg["side"],
                                                             qty=lg["qty"], order_type="LIMIT", price=lg["price"], product=lg["product"]))
        except BrokerError as e:
            self._set_leg(lg["id"], status="failed", reason=f"entry rejected: {e}")
            self._abort(session, f"entry leg {lg['symbol']} rejected: {e}")
            return
        self._set_leg(lg["id"], status="entering", entry_tag=tag, entry_sent=self.clock())

    def _abort(self, session: str, why: str):
        """Reject / timeout / partial on entry: drop pending legs, square off what filled, lock the session."""
        self.lock(session, why)
        for lg in self._legs("session=? AND status='pending'", (session,)):
            self._set_leg(lg["id"], status="failed", reason="not sent: " + why)
        self._exit_all(f"session={session!r}", "entry_abort")

    # ------------------------------------------------------------------ protective stop and exits
    def _place_sl(self, lg, qty: int):
        if qty <= 0 or lg["sl_trigger"] is None:          # long hedge: no stop by design
            return
        cs = self._close_side(lg["side"])
        trig = lg["sl_trigger"]
        try:
            tag = self._send("sl", lg["session"], lg["id"], dict(symbol=lg["symbol"], exchange=lg["exchange"], side=cs, qty=qty,
                                                                 order_type="SL", price=self._sl_limit(cs, trig), trigger=trig,
                                                                 product=lg["product"]))
            self._set_leg(lg["id"], sl_tag=tag)
            self.event("info", f"protective SL at broker for {lg['symbol']} × {qty}: trigger {trig}")
        except BrokerError as e:
            self.event("critical", f"protective SL for {lg['symbol']} rejected ({e}); exiting the leg now")
            self._set_leg(lg["id"], status="exiting", reason="sl_rejected", exit_started=self.clock(), exit_tag=None)
            self._try_exit(lg["id"])

    def _protect_partial(self, lg, filled: int):
        """SL always sized to what has filled so far."""
        sl = self._working(lg["sl_tag"])
        if sl is None and not (self._intent(lg["sl_tag"]) and self._intent(lg["sl_tag"])["status"] == "SENDING"):
            self._place_sl(lg, filled)
        elif sl is not None and sl.qty < filled:
            try:
                self._call(self.b.modify, sl.order_id, qty=filled)
                self._bump_mods(lg["sl_tag"])
            except BrokerError as e:
                self.event("critical", f"could not resize SL of {lg['symbol']} to {filled}: {e}")

    def _touch(self, lg, close_side: str) -> float | None:
        try:
            q = self._call(self.b.quote, [lg["symbol"]], exchange=lg["exchange"]).get(lg["symbol"])
        except BrokerError:
            return None
        if not q:
            return None
        px = (q.ask if close_side == "BUY" else q.bid) or q.ltp
        return px or None

    def _bump_mods(self, tag):
        self.db.execute("UPDATE intents SET mods = mods + 1 WHERE tag=?", (tag,))

    def _is_hedge(self, lg) -> bool:
        return lg["side"] == "BUY" and lg["sl_trigger"] is None

    def _shorts_open(self, session: str) -> bool:
        """A short counts until it is closed or failed: pending and unfilled entering shorts are coming."""
        return bool(self._legs("session=? AND side='SELL' AND status IN ('pending','entering','open','exiting')", (session,)))

    def exit_leg(self, leg_id: int, reason: str):
        """Mark the leg 'exiting' (so poll keeps driving it), then try to close it now. A hedge is never sold
        while its shorts are open: closing a hedge closes the session, shorts first."""
        lg = self._leg(leg_id)
        if lg["status"] in ("closed", "failed"):
            return
        if self._is_hedge(lg) and self._shorts_open(lg["session"]) and reason != "_hedge_release":
            self.event("important", f"{lg['symbol']} is the hedge of open shorts: closing the whole position, shorts first")
            self._exit_all(f"session={lg['session']!r}", reason)
            return
        if lg["status"] == "pending":
            self._set_leg(leg_id, status="failed", reason=reason)
            return
        if lg["status"] != "exiting":
            self._set_leg(leg_id, status="exiting", reason=reason, exit_started=self.clock(), chase_n=0, chase_at=self.clock())
        self._try_exit(leg_id)

    def _try_exit(self, leg_id: int):
        """One attempt to get a closing LIMIT working for the open quantity. Safe to call repeatedly."""
        lg = self._leg(leg_id)
        if self._is_hedge(lg) and lg["status"] == "exiting" and self._shorts_open(lg["session"]):
            return                                               # wing waits until its shorts are bought back
        cs = self._close_side(lg["side"])
        ent = self._intent(lg["entry_tag"])
        eo = self._working(lg["entry_tag"])
        if eo:                                                       # cancel the rest of an unfilled entry first
            try:
                self._call(self.b.cancel, eo.order_id)
            except BrokerError:
                pass
            self._sync(self._call(self.b.orders))
            ent = self._intent(lg["entry_tag"])
        if ent and ent["filled"] > lg["filled"]:
            self._set_leg(leg_id, filled=ent["filled"], entry_avg=ent["avg"])
            lg = self._leg(leg_id)
        if self._open_qty(lg) <= 0:
            if lg["filled"] == 0 and not self._working(lg["entry_tag"]):
                self._set_leg(leg_id, status="failed")
            return
        if self._working(lg["exit_tag"]) and lg["exit_tag"]:
            return                                                   # an exit is already working; poll chases it
        for tag in (lg["sl_tag"], lg["exit_tag"]):
            it = self._intent(tag)
            if it and it["status"] == "SENDING":
                self.event("important", f"exit of {lg['symbol']} waits: an order is still being confirmed (no double order)")
                return
        px = self._touch(lg, cs)
        sl = self._working(lg["sl_tag"])
        if sl is not None:
            if sl.order_type == "LIMIT":                              # already converted
                self._set_leg(leg_id, exit_tag=lg["sl_tag"])
                return
            lim = tick(px or self._sl_limit(cs, lg["sl_trigger"]), cs == "BUY")
            try:
                self._call(self.b.modify, sl.order_id, order_type="LIMIT", price=lim)
                self._bump_mods(lg["sl_tag"])
                self._set_leg(leg_id, exit_tag=lg["sl_tag"])
            except NetworkUnknown:
                pass                                                 # re-read next poll; modify is idempotent
            except Rejected:
                # broker refuses the conversion: cancel the SL, confirm, then place a LIMIT next attempt
                try:
                    self._call(self.b.cancel, sl.order_id)
                except BrokerError as e:
                    self.event("critical", f"cannot convert or cancel SL of {lg['symbol']} ({e}); retrying")
            except BrokerError as e:
                self.event("critical", f"exit of {lg['symbol']} failed ({e}); retrying")
            return
        # no working closing order: place one for exactly what is still open (capped by the real position)
        self._sync(self._call(self.b.orders))
        open_qty = self._open_qty(self._leg(leg_id))
        try:
            real = self._call(self.b.positions).get(lg["symbol"], 0)
        except BrokerError:
            return
        held = abs(real) if (real < 0) == (lg["side"] == "SELL") else 0
        qty = min(open_qty, held)
        if qty <= 0:
            if open_qty > 0:
                self.event("critical", f"{lg['symbol']}: app thinks {open_qty} open but broker holds none; not sending")
            return
        if px is None:
            self.event("critical", f"no quote for {lg['symbol']}; exit waits for a price")
            return
        try:
            tag = self._send("exit", lg["session"], leg_id, dict(symbol=lg["symbol"], exchange=lg["exchange"], side=cs, qty=qty,
                                                                 order_type="LIMIT", price=tick(px, cs == "BUY"), product=lg["product"]))
            self._set_leg(leg_id, exit_tag=tag, chase_at=self.clock())
        except BrokerError as e:
            self.event("critical", f"exit order for {lg['symbol']} refused ({e}); retrying")

    def move_stop(self, leg_id: int, new_trigger: float) -> bool:
        """Trailing: tighten the protective SL at the broker (trigger and limit). Never loosens. Reads a fresh
        order book first. Near the modification cap the SL is cancelled (confirmed) and a fresh SL placed —
        unless it filled meanwhile, in which case the leg is treated as stopped. A trigger already through the
        market exits the leg instead."""
        lg = self._leg(leg_id)
        if lg["status"] != "open" or lg["sl_trigger"] is None:
            return False
        cs = self._close_side(lg["side"])
        new_trigger = tick(new_trigger, cs == "SELL")
        if (cs == "BUY" and new_trigger >= lg["sl_trigger"]) or (cs == "SELL" and new_trigger <= lg["sl_trigger"]):
            return False
        self._sync(self._call(self.b.orders))                 # fresh book: never act on a ~2 s old status
        sl = self._working(lg["sl_tag"])
        if sl is None or sl.status != "TRIGGER PENDING" or sl.filled:
            return False                                     # triggered / filling / missing: chaser or exit path owns it
        px = self._touch(lg, cs)
        if px is not None and ((cs == "BUY" and new_trigger <= px) or (cs == "SELL" and new_trigger >= px)):
            self.event("important", f"trailing stop {lg['symbol']} {new_trigger} is already through the market ({px}); exiting")
            self.exit_leg(leg_id, "trail_crossed")
            return True
        it = self._intent(lg["sl_tag"])
        if it["mods"] >= self.L.max_mods:
            try:
                self._call(self.b.cancel, sl.order_id)
            except BrokerError:
                return False
            self._sync(self._call(self.b.orders))
            if self._working(lg["sl_tag"]):
                return False                                 # cancel not confirmed: keep the old stop
            if self._intent(lg["sl_tag"])["filled"] > 0:      # it filled in between: that is a stop, not a trail
                self.event("important", f"{lg['symbol']}: SL filled while being replaced; closing the rest")
                self._set_leg(leg_id, status="exiting", reason="stop_loss", exit_started=self.clock(), exit_tag=None)
                self._try_exit(leg_id)
                return True
            self._set_leg(leg_id, sl_trigger=new_trigger)
            self._place_sl(self._leg(leg_id), self._open_qty(self._leg(leg_id)))
            return True
        try:
            self._call(self.b.modify, sl.order_id, trigger=new_trigger, price=self._sl_limit(cs, new_trigger))
            self._bump_mods(lg["sl_tag"])
            self._set_leg(leg_id, sl_trigger=new_trigger)
            self.event("info", f"trailing stop {lg['symbol']}: trigger {lg['sl_trigger']} → {new_trigger}")
            return True
        except NetworkUnknown:
            self._bump_mods(lg["sl_tag"])                    # may have been applied: count it against the cap
            return False
        except BrokerError as e:
            self.event("important", f"could not move SL of {lg['symbol']} ({e}); old stop stays")
            return False

    def _chase(self, lg, tag: str, aggressive: bool):
        o = self._working(tag)
        if not o:
            return
        it = self._intent(tag)
        if it["mods"] >= self.L.max_mods:                          # cancel and replace before Kite's 25-modify cap
            try:
                self._call(self.b.cancel, o.order_id)
            except BrokerError:
                pass
            self._sync(self._call(self.b.orders))
            if self._working(tag):                                   # cancel not confirmed: never send a second order
                self.event("important", f"cancel of {tag} not confirmed yet; retrying before any new order")
                return
            if lg["status"] == "open":
                self._set_leg(lg["id"], status="exiting", reason="stop_loss", exit_started=lg["chase_at"] or self.clock())
            self._set_leg(lg["id"], exit_tag=None)
            return
        cs = self._close_side(lg["side"])
        px = self._touch(lg, cs)
        if px is None:
            return
        if aggressive:
            px = px * 1.02 if cs == "BUY" else px * 0.98
        px = tick(px, cs == "BUY")
        if (cs == "BUY" and px > o.price) or (cs == "SELL" and px < o.price):
            try:
                self._call(self.b.modify, o.order_id, price=px)
                self._bump_mods(tag)
            except BrokerError:
                pass
        self._set_leg(lg["id"], chase_n=lg["chase_n"] + 1, chase_at=self.clock())

    # ------------------------------------------------------------------ the loop
    def poll(self):
        """Call every 1–2 s. Syncs the order book and moves every leg along its state machine."""
        self._sync(self._call(self.b.orders))
        self._resolve_sending()
        now = self.clock()
        changed = False
        # late-appearing entries on legs already given up: protect and close
        for lg in self._legs("status='failed' AND entry_tag IS NOT NULL"):
            ent = self._intent(lg["entry_tag"])
            if ent and ent["filled"] > self._closed_qty(lg["id"]):
                self._set_leg(lg["id"], status="open", filled=ent["filled"], entry_avg=ent["avg"])
                self._protect_partial(self._leg(lg["id"]), ent["filled"])
                self.exit_leg(lg["id"], "late_fill")
                changed = True
        self._orphan_scan()
        for lg in self._legs("status='open' AND side='BUY' AND sl_trigger IS NULL"):
            if not self._shorts_open(lg["session"]):          # every short closed: the wing has nothing left to hedge
                self.event("info", f"{lg['symbol']}: its shorts are closed; selling the hedge")
                self._set_leg(lg["id"], status="exiting", reason="hedge_unneeded", exit_started=now, chase_n=0, chase_at=now)
        for lg in sorted(self._legs("status IN ('entering','open','exiting')"), key=lambda r: (r["side"] != "SELL", r["id"])):
            try:
                changed |= self._step(lg, now)
            except BrokerError as e:
                self.event("critical", f"{lg['symbol']}: broker error while managing the leg ({e}); will retry")
        if changed or now - self._last_reconcile >= self.L.reconcile_every:
            if self.ready:
                self.ready = self.reconcile()

    def _orphan_scan(self):
        """Any working closing order that is not the current SL/exit of a live leg is cancelled (no double stops)."""
        current = set()
        for lg in self._legs("status IN ('entering','open','exiting')"):
            current.add(lg["exit_tag"])
            if not lg["exit_tag"] or lg["exit_tag"] == lg["sl_tag"]:
                current.add(lg["sl_tag"])              # an SL is only legitimate while no separate exit order exists
        for it in self.db.execute("SELECT * FROM intents WHERE kind IN ('sl','exit') AND status='SENT'").fetchall():
            if it["tag"] in current:
                continue
            o = self._book.get(it["order_id"])
            if o and o.status not in TERMINAL:
                self.event("critical", f"orphan {it['kind']} order {it['tag']} on {it['symbol']} found working; cancelling")
                try:
                    self._call(self.b.cancel, o.order_id)
                except BrokerError as e:
                    self.event("critical", f"could not cancel orphan {it['tag']}: {e}")
        known = {r[0] for r in self.db.execute("SELECT tag FROM intents")}
        for o in self._book.values():
            if o.status not in TERMINAL and o.tag not in known:
                self.event("critical", f"order {o.order_id} on {o.symbol} not placed by TradeDesk is working; left alone")

    def _step(self, lg, now) -> bool:
        if lg["status"] == "entering":
            ent = self._intent(lg["entry_tag"])
            if not ent:
                return False
            st, filled = ent["bstatus"], ent["filled"]
            if filled > 0:
                self._protect_partial(lg, filled)
            if st == "COMPLETE":
                self._set_leg(lg["id"], status="open", filled=filled, entry_avg=ent["avg"])
                self.event("info", f"entry filled {lg['side']} {lg['symbol']} × {filled} @ {ent['avg']}")
                self._next_leg(lg["session"])
                return True
            if st in ("REJECTED", "CANCELLED") or ent["status"] == "FAILED":
                self._set_leg(lg["id"], status="open" if filled else "failed", filled=filled, entry_avg=ent["avg"],
                              reason=None if filled else f"entry {(st or 'failed').lower()}: {ent['msg'] or ''}")
                self._abort(lg["session"], f"entry {lg['symbol']} {(st or 'failed').lower()} {ent['msg'] or ''}".strip())
                return True
            if now - (lg["entry_sent"] or now) > self.L.entry_timeout:
                why = (f"partial fill {filled}/{lg['qty']} on {lg['symbol']}" if filled else
                       f"entry {lg['symbol']} not filled in {self.L.entry_timeout:.0f} s")
                self._abort(lg["session"], why)
                return True
            return False
        open_qty = self._open_qty(lg)
        if lg["filled"] > 0 and open_qty <= 0:
            closers = self.db.execute("SELECT filled, avg FROM intents WHERE leg=? AND kind IN ('sl','exit') AND filled>0", (lg["id"],)).fetchall()
            q = sum(r["filled"] for r in closers)
            avg = round(sum(r["filled"] * r["avg"] for r in closers) / q, 2) if q else None
            reason = "stop_loss" if lg["status"] == "open" else (lg["reason"] or "exit")
            self._set_leg(lg["id"], status="closed", exit_avg=avg, reason=reason)
            self.event("important" if reason in ("stop_loss", "daily_loss_limit", "kill_switch") else "info",
                       f"{reason.replace('_', ' ')}: {lg['symbol']} × {lg['filled']} closed @ {avg}")
            if reason == "stop_loss":
                self._after_stop()
            return True
        if lg["status"] == "open":
            if lg["sl_trigger"] is None:
                return False                                     # long hedge: nothing to protect
            sl_it = self._intent(lg["sl_tag"])
            sl = self._working(lg["sl_tag"])
            if sl is None:
                if not sl_it or sl_it["status"] != "SENDING":
                    self.event("critical", f"{lg['symbol']} has no working SL at the broker; exiting")
                    self.exit_leg(lg["id"], "sl_missing")
                    return True
            elif sl.status == "OPEN":                                # triggered but not filled: chase
                if sl.qty - sl.filled < open_qty:
                    self._protect_partial(lg, open_qty)
                if lg["chase_at"] is None:
                    self._set_leg(lg["id"], chase_at=now, chase_n=0)
                elif now - lg["chase_at"] >= self.L.chase_every:
                    self._chase(lg, lg["sl_tag"], aggressive=lg["chase_n"] >= self.L.chase_tries)
            return False
        # exiting
        if self._is_hedge(lg) and self._shorts_open(lg["session"]):
            return False
        if not self._working(lg["exit_tag"]):
            self._try_exit(lg["id"])
        elif now - (lg["chase_at"] or now) >= self.L.chase_every:
            self._chase(lg, lg["exit_tag"], aggressive=now - (lg["exit_started"] or now) >= self.L.exit_chase_secs)
        return False

    def _after_stop(self):
        if self.realised_today() <= -self.L.daily_loss:
            self.daily_loss_hit(f"realised ₹{self.realised_today():,.0f} after a stop")

    # ------------------------------------------------------------------ P&L, daily loss, kill switch
    def realised_today(self) -> float:
        tot = 0.0
        for lg in self._legs("day=? AND status='closed'", (self.day(),)):
            sign = 1 if lg["side"] == "BUY" else -1
            tot += sign * ((lg["exit_avg"] or 0) - (lg["entry_avg"] or 0)) * lg["filled"]
        return round(tot, 2)

    def _exit_all(self, where: str, reason: str):
        """Mark first, then act on each leg independently; any failure is retried by poll()."""
        legs = sorted(self._legs(f"({where}) AND status IN ('entering','open','exiting','pending')"),
                      key=lambda r: (r["side"] != "SELL", r["id"]))        # buy back shorts before selling wings
        for lg in legs:
            if lg["status"] == "pending":
                self._set_leg(lg["id"], status="failed", reason=reason)
            elif lg["status"] != "exiting":
                self._set_leg(lg["id"], status="exiting", reason=reason, exit_started=self.clock(), chase_n=0, chase_at=self.clock())
        for lg in legs:
            if lg["status"] == "pending":
                continue
            try:
                self._try_exit(lg["id"])
            except Exception as e:                                   # never stop half-way through the list
                self.event("critical", f"exit of {lg['symbol']} failed ({type(e).__name__}); poll will retry")

    def daily_loss_hit(self, why: str):
        self._loss_done_day = self.day()
        self.lock("account", f"daily loss limit ₹{self.L.daily_loss:,.0f}: {why}")
        self._exit_all("1", "daily_loss_limit")

    def check_mtm(self, mtm_net: float):
        """Runner calls this on every tick with MTM net of charges for the day (works even if already locked)."""
        done = self._loss_done_day == self.day() or self.db.execute(
            "SELECT 1 FROM locks WHERE day=? AND scope='account' AND reason LIKE 'daily loss limit%'", (self.day(),)).fetchone()
        if mtm_net <= -self.L.daily_loss and not done:
            self.daily_loss_hit(f"MTM ₹{mtm_net:,.0f}")

    def kill(self, command_id: str, why: str = "kill switch") -> bool:
        """Exits every leg, cancels unfilled entries, locks the account for the day. A repeat re-drives exits."""
        first = self._new_command(command_id)
        self.armed = False
        self.lock("account", why)
        self._exit_all("1", "kill_switch")
        return first

    def snapshot(self) -> dict:
        return {"at": self.clock(), "armed": self.armed, "ready": self.ready, "broker_ok": self.broker_ok, "locked": self.locked(),
                "realised_today": self.realised_today(),
                "legs": [dict(r) | {"open_qty": self._open_qty(r)} for r in self._legs("day=? OR status IN ('open','exiting','entering')", (self.day(),))],
                "events": [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY at DESC LIMIT 30")]}
