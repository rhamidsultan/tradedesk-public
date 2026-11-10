"""One desk per user and mode (shadow | live): an OrderManager plus the bridge from engine decisions to orders.

Adapted from py/live LiveDesk (reviewed 23 Oct, PASS WITH CONDITIONS), with these sources instead of files:
  - engine decisions: a PaperEngine replay of each session on the store's recorded bars (like paper);
  - contracts: the day's Zerodha instruments list (trading symbol, exchange, lot size), and the lot size must
    match the dated reference data, else the entry is blocked;
  - prices: the broker's quote endpoint at the moment of sending (never a minute-old bar);
  - commands: rows in live_commands (the API only queues; this desk is the single writer).

Every minute, per session, after the engine replay:
  - a new engine trade (entry within ENTRY_FRESH_MIN minutes) -> OrderManager.enter() with a limit at the touch and the
    engine's premium stop as the broker SL trigger (long legs without one get a backstop at -40 %);
  - a sent leg the engine no longer holds is exited (explicit engine exits at once; an engine stop after
    ENGINE_FLAT_GRACE seconds, giving the broker SL time) unless the broker already closed it;
  - a trailing stop that moved by at least its step is moved at the broker (tighter only);
  - a broker stop on one leg of a multi-leg trade is alerted and siblings follow the lone-leg rule.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Callable

from tradedesk_oms import LegReq, Limits, OrderManager
from tradedesk_oms.oms import GuardBlocked

ENTRY_FRESH_MIN = 3
ENGINE_FLAT_GRACE = 45.0
BACKSTOP = 0.6              # long legs without a premium stop: broker SL at 60 % of the entry
QUOTE_MAX_SPREAD = 0.25     # refuse an entry when the spread is wider than 25 % of the mid
MAX_DRIFT = 0.20            # refuse an entry when the live price is more than 20 % away from the engine's entry


def atomic_json(path: Path, obj) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


def session_limits(spec: dict) -> dict:
    r, sch = spec["risk"], spec["schedule"]
    start = max("09:16", sch.get("start", "09:16"))
    return {"max_lots": int(r.get("max_lots", 1)), "daily_loss": float(r["max_loss_per_day"]),
            "entry_window": (start, sch.get("no_new_after", "15:00"))}


class Desk:
    def __init__(self, user_id: int, mode: str, broker, limits: Limits, folder: Path,
                 alert: Callable[[str, str, str], None], log: Callable[[str], None],
                 preflight: Callable[[list], list[str]] | None = None, clock=None, lot_check=None,
                 freeze_of=None, closing_only: bool = False, sleep=None, market_open=None):
        assert mode in ("shadow", "live")
        self.user_id, self.mode = user_id, mode
        self.dir = folder
        self.dir.mkdir(parents=True, exist_ok=True)
        self.om = OrderManager(broker, self.dir / "oms.db", limits, alert=alert, log=log, preflight=preflight, clock=clock,
                               sleep=sleep, market_open=market_open)
        self.lot_check = lot_check            # (underlying, expiry, kite lot size) -> problem text or None
        self.freeze_of = freeze_of            # (underlying, day) -> exchange freeze quantity or None
        self.closing_only = closing_only      # no limits or live switched off: manage and close legs, never open
        self.mtm_unknown_since: float | None = None
        self.sent_f = self.dir / "sent.json"
        self.sent: dict = json.loads(self.sent_f.read_text()) if self.sent_f.exists() else {}
        self.started_day: str | None = None
        self.flat_since: dict[int, float] = {}
        self.ltp: dict[str, float] = {}

    def _save(self):
        atomic_json(self.sent_f, self.sent)

    # ---------------------------------------------------------------- day and readiness
    def new_day(self, d: date, broker=None):
        """Day roll / new login: fresh credentials, not ready until today's start() has reconciled."""
        if broker is not None:
            self.om.b = broker
            self.om.broker_ok = True
        if self.started_day != str(d):
            self.om.ready = False
            self.started_day = None

    def set_limits(self, limits: Limits, preflight, closing_only: bool) -> None:
        """Account limits apply from the next check (a cut takes effect at once)."""
        self.om.L = limits
        self.om.preflight = preflight
        self.closing_only = closing_only

    def ensure_started(self, d: date) -> bool:
        if self.started_day == str(d) and self.om.ready:
            return True
        if self.om.now_ist().strftime("%H:%M:%S") < "09:15:20" or not self.om.market_open():
            return False                                   # Kite rejects stop orders outside market hours
        ok = self.om.start()
        if ok:
            self.started_day = str(d)
        return ok

    def has_open(self) -> bool:
        return bool(self.om._legs("status IN ('pending','entering','open','exiting')"))

    # ---------------------------------------------------------------- commands (single writer)
    def run_command(self, kind: str, payload: dict, cid: str, disarm_all: Callable[[], None]) -> str:
        if kind == "kill":
            self.om.kill(cid, "Stop all from the app")          # local first: lock and mark every leg exiting
            try:
                disarm_all()
            except Exception as e:  # noqa: BLE001 - the account is already locked for today
                self.om.event("important", f"disarm after Stop all failed ({type(e).__name__}); the account stays locked")
            if not self.om.market_open():
                return "Stop all: every leg is marked to exit at the next open; new entries are blocked."
            return "Stop all: every leg is exiting; new entries are blocked for today."
        if kind == "exit_leg":
            self.om.exit_leg(int(payload["leg_id"]), "manual")
            if not self.om.market_open():
                return "Market closed: the leg exits at the next open."
            return "Exit sent: the leg's stop order becomes the exit."
        if kind == "tighten_stop":
            ok = self.om.move_stop(int(payload["leg_id"]), float(payload["trigger"]))
            lg = self.om._leg(int(payload["leg_id"]))
            if lg is not None and lg["status"] == "exiting":
                return "The new stop was already past the price, so the leg is exiting now."
            if ok:
                return "Stop moved at the broker."
            return "Stop not moved: only a tighter stop on an untriggered stop order is allowed."
        if kind == "exit_session":
            n = 0
            for lg in self.om._legs("session=? AND status IN ('pending','entering','open','exiting')", (str(payload["session"]),)):
                self.om.exit_leg(lg["id"], "session_stopped")
                n += 1
            return f"Exiting {n} leg{'s' if n != 1 else ''} of the session."
        raise ValueError(f"unknown command {kind}")

    def poll(self, mtm_net: float | None = None):
        if self.om.ready or self.has_open():
            self.om.poll()
        if mtm_net is not None:
            self.om.check_mtm(mtm_net)

    def mtm_today(self, charges=None) -> float | None:
        """Realised today + open legs at the last price, net of estimated charges (charges(side, value) per order:
        entry and exit of closed legs, entry and an exit at the last price for open ones). None when any open leg
        has no price: never guess the loss."""
        tot = self.om.realised_today()
        ch = 0.0
        for lg in self.om._legs("day=? AND status='closed'", (self.om.day(),)):
            if charges and lg["entry_avg"] and lg["exit_avg"]:
                close = "SELL" if lg["side"] == "BUY" else "BUY"
                ch += charges(lg["side"], lg["entry_avg"] * lg["filled"]) + charges(close, lg["exit_avg"] * lg["filled"])
        for lg in self.om._legs("status IN ('open','exiting')"):
            px = self.ltp.get(lg["symbol"])
            if px is None or not lg["entry_avg"]:
                return None
            q = self.om._open_qty(lg)
            tot += (1 if lg["side"] == "BUY" else -1) * (px - lg["entry_avg"]) * q
            if charges:
                close = "SELL" if lg["side"] == "BUY" else "BUY"
                ch += charges(lg["side"], lg["entry_avg"] * q) + charges(close, px * q)
        return round(tot - ch, 2)

    def expiry_exits(self, today: date, hhmm: str, cutoff: str, expiry_of: Callable[[str], date | None]) -> int:
        """At the expiry-day cut-off every open leg in a contract expiring today exits (carried legs too)."""
        if hhmm < cutoff:
            return 0
        n = 0
        for lg in self.om._legs("status IN ('open','entering')"):
            if expiry_of(lg["symbol"]) == today:
                self.om.exit_leg(lg["id"], "expiry_cutoff")
                n += 1
        return n

    def refresh_prices(self, exchange_of: Callable[[str], str]) -> None:
        syms = sorted({lg["symbol"] for lg in self.om._legs("status IN ('open','exiting','entering')")})
        if not syms:
            return
        by_ex: dict[str, list[str]] = {}
        for s in syms:
            by_ex.setdefault(exchange_of(s), []).append(s)
        for ex, ss in by_ex.items():
            try:
                got = self.om._call(self.om.b.quote, ss, ex)
                for s in ss:
                    q = got.get(s)
                    if q is not None and q.ltp:
                        self.ltp[s] = q.ltp
                    else:
                        self.ltp.pop(s, None)                  # missing from the reply: unknown, never the old price
            except Exception:  # noqa: BLE001 - a missing price makes MTM unknown (never zero)
                for s in ss:
                    self.ltp.pop(s, None)

    # ---------------------------------------------------------------- engine -> orders
    def bridge(self, run, bt, d: date, now_min: int, contracts: dict, armed: bool):
        """run: LiveRun row (id, name, spec); bt: the engine copy replayed to now; contracts: store symbol ->
        (kite symbol, exchange, lot size, expiry, underlying)."""
        rev = {v[0]: k for k, v in contracts.items()}       # Zerodha symbol -> store symbol
        if bt is None or self.started_day != str(d) or not self.om.ready:
            return
        sid = str(run.id)
        for t in bt.trades.values():
            if not t.entry_ts.startswith(str(d)):
                continue
            key = f"{sid}|{d}|{t.trade_id}"
            if key not in self.sent:
                had = self.om._legs("command=?", (key,))
                if had and not all(l["symbol"] in rev for l in had):
                    note = f"{key}|unmatched"
                    if note not in self.sent:
                        self.sent[note] = {"alerted": True}
                        self._save()
                        self.om.event("critical", f"{run.name}: legs of trade {t.trade_id} are in the order book but not in "
                                      "today's contract list; they keep their stops but are not followed — check by hand")
                    continue
                if had:                                           # sent before a crash or a lost sent.json: re-attach
                    # pair each order-book leg with its own engine leg by symbol (never by position: entries are
                    # sent hedges first, so the order differs from the engine's)
                    self.sent[key] = {"legs": [l["id"] for l in had], "engine_legs": [rev[l["symbol"]] for l in had]}
                    self._save()
                    self.om.event("important", f"{run.name}: re-attached {len(had)} sent leg(s) of trade {t.trade_id}")
            if key in self.sent:
                rec = self.sent[key]
                if "legs" in rec and len(t.legs) > len(rec["engine_legs"]):
                    self._add_legs(run, t, rec, key, contracts, armed)
                continue
            hh, mm = map(int, t.entry_ts[11:16].split(":"))
            if now_min - ((hh * 60 + mm) - 555) > ENTRY_FRESH_MIN:
                self.sent[key] = {"skipped": "stale (service restarted or late)"}
                self._save()
                continue
            if not armed or self.closing_only:
                self.sent[key] = {"skipped": "not armed" if not armed else "closing only (limits or live switch)"}
                self._save()
                continue
            legs, problems = self._leg_reqs(t.legs, contracts, run.spec, d)
            if problems:
                self.sent[key] = {"blocked": "; ".join(problems)}
                self._save()
                self.om.event("critical", f"{self.mode} entry not sent for {run.name}: {'; '.join(problems)}")
                continue
            legs.sort(key=lambda x: x[0].side != "BUY")    # hedges first (keeps margin low), then the shorts
            try:
                self.om.armed = True
                ids = self.om.enter(sid, [l for l, _ in legs], command_id=key, session_limits=session_limits(run.spec))
                self.sent[key] = {"legs": ids, "engine_legs": [s for _, s in legs]}
                self.om.event("important", f"{self.mode.upper()} entry for {run.name}: " + ", ".join(
                    f"{l.side} {l.symbol} × {l.lots * l.lot_size} @ {l.price} SL {l.sl_trigger}" for l, _ in legs))
            except GuardBlocked as e:
                self.sent[key] = {"blocked": str(e)}
            finally:
                self.om.armed = False
            self._save()
        self._follow_engine(run, bt)

    @staticmethod
    def _stop_for(lg: dict, px: float, spec: dict | None) -> float | None:
        """The engine's premium stop, re-based on the price actually sent: a multiple or percent stop keeps its
        ratio to the entry, a points stop keeps its distance (so a better or worse live price never loosens it)."""
        sl, entry = lg.get("sl"), lg.get("entry")
        if not sl or not entry:
            return sl
        typ = (((spec or {}).get("risk") or {}).get("stop_loss") or {}).get("type")
        trig = px + (sl - entry) if typ == "points" else px * sl / entry
        # never looser than the engine's own stop: for a short the lower trigger, for a long the higher one
        trig = min(trig, sl) if lg.get("side") == "SELL" else max(trig, sl)
        return round(round(trig / 0.05) * 0.05, 2)

    def _leg_reqs(self, engine_legs: list[dict], contracts: dict, spec: dict | None = None,
                  d: date | None = None) -> tuple[list, list[str]]:
        legs, problems = [], []
        for lg in engine_legs:
            c = contracts.get(lg["symbol"])
            if c is None:
                problems.append(f"{lg['symbol']} not in today's Zerodha instruments list")
                continue
            ksym, ex, lot, expiry, und = c
            if self.lot_check:
                bad = self.lot_check(und, expiry, lot)
                if bad:
                    problems.append(f"{lg['symbol']}: {bad}")
                    continue
            try:
                q = self.om._call(self.om.b.quote, [ksym], ex).get(ksym)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{lg['symbol']}: no price from Zerodha ({type(e).__name__})")
                continue
            px = (q.bid if lg["side"] == "SELL" else q.ask) if q else None
            if not px or not q.bid or not q.ask:
                problems.append(f"{lg['symbol']}: no bid/ask now")
                continue
            mid = (q.bid + q.ask) / 2
            if mid and (q.ask - q.bid) / mid > QUOTE_MAX_SPREAD:
                problems.append(f"{lg['symbol']}: spread {q.bid}–{q.ask} too wide")
                continue
            if lg.get("entry") and abs(px - lg["entry"]) / lg["entry"] > MAX_DRIFT:
                problems.append(f"{lg['symbol']}: price {px} is too far from the strategy's entry {lg['entry']}")
                continue
            freeze = self.freeze_of(und, d or expiry) if self.freeze_of else None
            if self.freeze_of and freeze is None:
                problems.append(f"{lg['symbol']}: freeze quantity for {und} unknown (add it to the reference data)")
                continue
            hedge = bool(lg.get("hedge")) and lg["side"] == "BUY"
            trig = None if hedge else (self._stop_for(lg, px, spec) or (round(px * BACKSTOP, 1) if lg["side"] == "BUY" else None))
            if not trig and not hedge:
                problems.append(f"{lg['symbol']}: no premium stop")
                continue
            if trig and lg["side"] == "SELL" and trig <= px:
                problems.append(f"{lg['symbol']}: price {px} is already at or past the stop {trig}")
                continue
            if trig and lg["side"] == "BUY" and trig >= px:
                problems.append(f"{lg['symbol']}: price {px} is already at or below the stop {trig}")
                continue
            legs.append((LegReq(ksym, ex, lg["side"], int(lg["lots"]), int(lot), float(px), float(trig) if trig else None,
                                freeze_qty=freeze), lg["symbol"]))
        return legs, problems

    def _add_legs(self, run, t, rec, key, contracts, armed):
        """A python strategy added legs to an open trade (e.g. wings a minute later): send just those."""
        new = list(t.legs[len(rec["engine_legs"]):])
        akey = f"{key}|add{len(rec['engine_legs'])}"
        if akey in self.sent:
            return
        if not armed or self.closing_only:
            self.sent[akey] = {"skipped": "not armed"}
            self._save()
            self.om.event("critical", f"{run.name}: the strategy added legs but the session can't open orders — closing the position")
            for lid in rec["legs"]:
                self.om.exit_leg(lid, "added_leg_not_armed")
            return
        legs, problems = self._leg_reqs(new, contracts, run.spec, None)
        if problems:
            self.sent[akey] = {"blocked": "; ".join(problems)}
            self._save()
            self.om.event("critical", f"{run.name}: added legs not sent ({'; '.join(problems)}) — closing the position")
            for lid in rec["legs"]:
                self.om.exit_leg(lid, "added_leg_failed")
            return
        legs.sort(key=lambda x: x[0].side != "BUY")
        try:
            self.om.armed = True
            ids = self.om.enter(str(run.id), [l for l, _ in legs], command_id=akey, session_limits=session_limits(run.spec),
                                add_to=True)
            rec["legs"] += ids
            rec["engine_legs"] += [s for _, s in legs]
            self.sent[akey] = {"legs": ids}
        except GuardBlocked as e:
            self.sent[akey] = {"blocked": str(e)}
            self.om.event("critical", f"{run.name}: added legs blocked ({e}) — closing the position")
            for lid in rec["legs"]:
                self.om.exit_leg(lid, "added_leg_blocked")
        finally:
            self.om.armed = False
        self._save()

    def _follow_engine(self, run, bt):
        sid = str(run.id)
        now = self.om.clock()
        engine_pos = {(p.trade_id, p.c.symbol): p for p in bt.positions}
        last_reason = {}
        for f in bt.fills:
            if f.reason != "entry":
                last_reason[(f.trade_id, f.symbol)] = f.reason
        tr = (run.spec or {}).get("risk", {}).get("trailing") or {}
        trail_step = float(tr.get("step", 1.0)) if tr else 1e9
        spec = run.spec or {}
        lone_rule = spec.get("risk", {}).get("close_lone_leg_eod") and spec.get("schedule", {}).get("hold_overnight")
        for key, rec in list(self.sent.items()):
            if not key.startswith(f"{sid}|") or "legs" not in rec or "|add" in key or "|lone|" in key or "|mismatch" in key \
                    or "|unmatched" in key:
                continue
            tid = int(key.rsplit("|", 1)[1])
            if not any(r["status"] in ("open", "entering") for lid in rec["legs"] for r in self.om._legs("id=?", (lid,))):
                continue                                        # nothing left to follow
            et = bt.trades.get(tid)
            if et is None or not set(rec["engine_legs"]) <= {x["symbol"] for x in et.legs}:
                # the engine's trade numbers shifted (revised bars, engine rebuilt): never exit on a guess
                note = f"{key}|mismatch"
                if note not in self.sent:
                    self.sent[note] = {"alerted": True}
                    self._save()
                    self.om.event("critical", f"{run.name}: the strategy no longer shows trade {tid} with these legs; "
                                  "they keep their stops at Zerodha but are not followed — check and exit by hand")
                continue
            pairs = [(self.om._legs("id=?", (lid,)), sym) for lid, sym in zip(rec["legs"], rec["engine_legs"])]
            pairs = [(l[0], sym) for l, sym in pairs if l]
            legs = [l for l, _ in pairs]
            live_open = [l for l in legs if l["status"] in ("open", "entering")]
            for lg, sym in pairs:
                if lg["status"] not in ("open", "entering"):
                    self.flat_since.pop(lg["id"], None)
                    continue
                if (tid, sym) in engine_pos:
                    self.flat_since.pop(lg["id"], None)
                    ep = engine_pos[(tid, sym)]
                    if lg["status"] == "open" and ep.qty != self.om._open_qty(lg):
                        self.om.event("critical", f"{run.name}: {sym} resized by the strategy ({self.om._open_qty(lg)} → {ep.qty}); "
                                      "resizing is not supported — closing the leg")
                        self.om.exit_leg(lg["id"], "resize_not_supported")
                        continue
                    if ep.sl is not None and lg["status"] == "open" and lg["sl_trigger"] is not None \
                            and abs(ep.sl - lg["sl_trigger"]) >= trail_step:
                        try:
                            self.om.move_stop(lg["id"], float(ep.sl))       # tighter only (move_stop refuses looser)
                        except Exception as e:  # noqa: BLE001 - one leg's broker error never stops the rest
                            self.om.event("important", f"trail of {lg['symbol']} not moved ({type(e).__name__}); retry next minute")
                    continue
                reason = last_reason.get((tid, sym))
                if reason and reason != "stop_loss":         # explicit engine exit (time, target, spot stop, manual…)
                    self.om.exit_leg(lg["id"], reason)
                    self.flat_since.pop(lg["id"], None)
                    continue
                first = self.flat_since.setdefault(lg["id"], now)   # engine stop: give the broker SL time to trigger
                if now - first >= ENGINE_FLAT_GRACE:
                    self.om.exit_leg(lg["id"], "engine_exit")
                    self.flat_since.pop(lg["id"], None)
            stopped = [l for l in legs if l["status"] == "closed" and l["reason"] == "stop_loss"]
            if stopped and live_open and len(legs) > 1:
                note = f"{sid}|lone|{tid}"
                if note not in self.sent:
                    self.sent[note] = {"alerted": True}
                    self._save()
                    self.om.event("critical", f"{run.name}: the broker stopped {stopped[0]['symbol']}; "
                                  f"{', '.join(l['symbol'] for l in live_open)} still open"
                                  + (" — will close at 15:25 (lone-leg rule)" if lone_rule else ""))
                if lone_rule and self.om.now_ist().strftime("%H:%M") >= "15:25":
                    for l in live_open:
                        self.om.exit_leg(l["id"], "lone_leg_eod")

    def snapshot(self) -> dict:
        snap = self.om.snapshot()
        for lg in snap["legs"]:
            lg["ltp"] = self.ltp.get(lg["symbol"])
            lg["mode"] = self.mode
            live_leg = lg["status"] in ("open", "entering", "exiting")
            # a hedge only while a short of its session is still open; otherwise a long leg without a stop is unprotected
            lg["hedge"] = bool(live_leg and lg["sl_trigger"] is None and lg["side"] == "BUY"
                               and self.om._shorts_open(lg["session"]))
            lg["stop_working"] = bool(lg["sl_tag"] and self.om._working(lg["sl_tag"]))
        snap["mode"] = self.mode
        snap["started_day"] = self.started_day
        snap["limits"] = {"daily_loss": self.om.L.daily_loss, "max_lots": self.om.L.max_lots,
                          "max_orders_day": self.om.L.max_orders_day}
        return snap
