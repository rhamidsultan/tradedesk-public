"""Live bridge: the paper engine decides, the OrderManager trades (architecture §4, §6).

Per armed live session, every minute after the engine replay:
  - a new engine trade (entry in the last 3 minutes) → OrderManager.enter() with Kite symbols and Kite lot sizes
    (blocked if refs.yaml disagrees), a limit at the touch, the engine's premium stop as the broker SL trigger
    (long legs without one get a backstop at −40%), and the strategy's own limits;
  - any sent leg the engine no longer holds is exited after ENGINE_FLAT_GRACE seconds unless the broker already
    closed it (covers time exits, targets, spot stops, and engine stops that Kite never triggered);
  - if Kite's SL closes one leg of a multi-leg trade, siblings follow the strategy's lone-leg rule, and an alert goes out.
Independently of sessions, the desk keeps managing every open leg in oms.db (start, reconcile, chase, daily loss, kill).
Live is possible only when $TD_HOME/live/ENABLED exists and the session is armed. Phone commands arrive as files.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from pathlib import Path

from tradedesk_engine.data import minute_of
from tradedesk_oms import LegReq, Limits, OrderManager
from tradedesk_oms.oms import GuardBlocked

from . import kite

ENTRY_FRESH_MIN = 3
ENGINE_FLAT_GRACE = 45.0
BACKSTOP = 0.6
QUOTE_MAX_AGE = 15.0


def live_dir() -> Path:
    d = Path(os.environ.get("TD_HOME", "~/Trading/tradedesk-home")).expanduser() / "live"
    (d / "commands").mkdir(parents=True, exist_ok=True)
    return d


def enabled() -> bool:
    return (live_dir() / "ENABLED").exists()


def atomic_json(path: Path, obj) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


def account_limits() -> Limits | None:
    """Explicit account limits from $TD_HOME/live/limits.json; None (no live) if missing or invalid."""
    f = live_dir() / "limits.json"
    try:
        d = json.loads(f.read_text())
        return Limits(max_lots=int(d["max_lots"]), max_orders_day=int(d["max_orders_day"]), daily_loss=float(d["daily_loss"]),
                      entry_window=tuple(d.get("entry_window", ("09:16", "15:00"))))
    except Exception:
        return None


def has_open_legs(db: Path) -> bool:
    if not db.exists():
        return False
    try:
        c = sqlite3.connect(str(db))
        r = c.execute("SELECT 1 FROM legs WHERE status IN ('pending','entering','open','exiting') LIMIT 1").fetchone()
        c.close()
        return bool(r)
    except sqlite3.Error:
        return True                                      # unknown: manage it


def session_limits(spec: dict) -> dict:
    r, sch = spec["risk"], spec["schedule"]
    start = max("09:16", sch.get("start", "09:16"))
    return {"max_lots": int(r.get("max_lots", 1)), "daily_loss": float(r["max_loss_per_day"]),
            "entry_window": (start, sch.get("no_new_after", "15:00"))}


class LiveDesk:
    def __init__(self, broker, alert, log, limits: Limits, preflight=None, db: Path | None = None, clock=None, cal=None):
        self.dir = live_dir()
        self.om = OrderManager(broker, db or self.dir / "oms.db", limits, alert=alert, log=log, preflight=preflight, clock=clock)
        self.alert, self.log, self.cal = alert, log, cal
        self.sent_f = self.dir / "sent.json"
        self.sent = json.loads(self.sent_f.read_text()) if self.sent_f.exists() else {}
        self.started_day: str | None = None
        self.flat_since: dict[int, float] = {}
        self.cmd_fail: dict[str, int] = {}

    def _save(self):
        atomic_json(self.sent_f, self.sent)

    def new_day(self, d: date, broker=None):
        """Day roll / new login: fresh credentials, not ready until today's start() has reconciled."""
        if broker is not None:
            self.om.b = broker
            self.om.broker_ok = True
        if self.started_day != str(d):
            self.om.ready = False
            self.started_day = None

    def ensure_started(self, d: date) -> bool:
        if self.started_day == str(d) and self.om.ready:
            return True
        if self.om.now_ist().strftime("%H:%M:%S") < "09:15:20":       # Kite rejects SL orders before the open
            return False
        ok = self.om.start()
        if ok:
            self.started_day = str(d)
        return ok

    # ---------------------------------------------------------------- commands from the phone
    def commands(self, disarm_all):
        for f in sorted((self.dir / "commands").glob("*.json")):
            cid = f.stem
            try:
                c = json.loads(f.read_text())
                if c.get("cmd") == "kill":
                    disarm_all()
                    self.om.kill(cid, "kill switch from the app")
                elif c.get("cmd") == "exit_leg":
                    self.om.exit_leg(int(c["leg_id"]), "manual")
                elif c.get("cmd") == "move_stop":
                    if not self.om.move_stop(int(c["leg_id"]), float(c["trigger"])):
                        self.om.event("important", f"stop of leg {c['leg_id']} not moved (only tighter stops on an untriggered SL)")
                elif c.get("cmd") == "exit_session":
                    for lg in self.om._legs("session=? AND status IN ('pending','entering','open','exiting')", (str(c["session"]),)):
                        self.om.exit_leg(lg["id"], "session_stopped")
                f.rename(f.with_suffix(".done"))
                self.cmd_fail.pop(cid, None)
            except Exception as e:
                n = self.cmd_fail[cid] = self.cmd_fail.get(cid, 0) + 1
                self.log(f"command {cid} failed ({type(e).__name__}); retry {n}")
                if n == 3:
                    self.alert("critical", "Live command not done", f"{f.name}: {type(e).__name__} — still retrying. Check Kite.")

    def poll(self, mtm_net: float | None = None):
        if self.om.ready or self.has_open():
            self.om.poll()
        if mtm_net is not None:
            self.om.check_mtm(mtm_net)
        self.write_snapshot()

    def has_open(self) -> bool:
        return bool(self.om._legs("status IN ('pending','entering','open','exiting')"))

    def write_snapshot(self):
        snap = self.om.snapshot()
        snap["enabled"] = enabled()
        snap["started_day"] = self.started_day
        snap["mtm_today"] = getattr(self, "last_mtm", None)
        lt = getattr(self, "ltp", {}) or {}
        for lg in snap["legs"]:
            lg["ltp"] = lt.get(lg["symbol"])
        snap["limits"] = {"daily_loss": self.om.L.daily_loss, "max_lots": self.om.L.max_lots, "max_orders_day": self.om.L.max_orders_day}
        atomic_json(self.dir / "oms.json", snap)

    # ---------------------------------------------------------------- engine → orders
    def bridge(self, sid: str, ps, meta: dict, d: date, bars, now_min: int):
        if meta.get("mode") != "live" or ps.today is None or bars is None:
            return
        if self.started_day != str(d) or not self.om.ready:
            return
        armed = bool(meta.get("armed")) and enabled() and meta.get("status") != "stopped"
        bt = ps.today
        by_sym = {i.symbol: (tok, i) for tok, i in bars.inst.items()}
        for t in bt.trades.values():
            if not t.entry_ts.startswith(str(d)):
                continue
            key = f"{sid}|{d}|{t.trade_id}"
            if key in self.sent:
                rec = self.sent[key]
                if "legs" in rec and len(t.legs) > len(rec["engine_legs"]):
                    self._add_legs(sid, meta, t, rec, key, by_sym, bars, now_min, armed)
                continue
            if now_min - minute_of(t.entry_ts[11:16]) > ENTRY_FRESH_MIN:
                self.sent[key] = {"skipped": "stale (service restarted or late)"}
                self._save()
                continue
            if not armed:
                self.sent[key] = {"skipped": "not armed"}
                self._save()
                continue
            legs, problems = [], []
            for lg in t.legs:
                ti = by_sym.get(lg["symbol"])
                if ti is None:
                    problems.append(f"{lg['symbol']} not in the live instrument list")
                    continue
                tok, ins = ti
                if self.cal is not None:
                    ref = self.cal.lot_size(ins.underlying, ins.expiry)
                    if ref is None or ref.value != ins.lot_size:
                        problems.append(f"lot size: refs.yaml {ref.value if ref else '?'} vs Kite {ins.lot_size}")
                        continue
                if bars.age(tok) > QUOTE_MAX_AGE:
                    problems.append(f"{lg['symbol']}: no fresh quote")
                    continue
                q = bars.quote.get(tok) or (None, None)
                px = (q[0] if lg["side"] == "SELL" else q[1]) or bars.ltp.get(tok)
                hedge = bool(lg.get("hedge")) and lg["side"] == "BUY"
                trig = None if hedge else (lg.get("sl") or (round(lg["entry"] * BACKSTOP, 1) if lg["side"] == "BUY" else None))
                if (not trig and not hedge) or not px:
                    problems.append(f"{lg['symbol']}: no premium stop or price")
                    continue
                legs.append((LegReq(ins.kite_symbol, kite.OPT_EXCHANGE.get(ins.underlying, "NFO"), lg["side"], int(lg["lots"]),
                                    ins.lot_size, float(px), float(trig) if trig else None), lg["symbol"]))
            legs.sort(key=lambda x: x[0].side != "BUY")    # hedges first (keeps margin low), then the shorts
            engine_syms = [sym for _, sym in legs]
            legs = [l for l, _ in legs]
            if problems:
                self.sent[key] = {"blocked": "; ".join(problems)}
                self._save()
                self.om.event("critical", f"live entry not sent for {meta.get('name')}: {'; '.join(problems)}")
                continue
            try:
                self.om.armed = True
                ids = self.om.enter(sid, legs, command_id=key, session_limits=session_limits(meta["spec"]))
                self.sent[key] = {"legs": ids, "engine_legs": engine_syms}
                self.om.event("important", f"LIVE entry for {meta.get('name')}: " +
                              ", ".join(f"{l.side} {l.symbol} × {l.lots * l.lot_size} @ {l.price} SL {l.sl_trigger}" for l in legs))
            except GuardBlocked as e:
                self.sent[key] = {"blocked": str(e)}
            finally:
                self.om.armed = False
            self._save()
        self._follow_engine(sid, meta, bt, d, now_min)

    def _add_legs(self, sid, meta, t, rec, key, by_sym, bars, now_min, armed):
        """A python strategy added legs to an open trade (e.g. wings a minute later): send just those."""
        new = [lg for lg in t.legs[len(rec["engine_legs"]):]]
        akey = f"{key}|add{len(rec['engine_legs'])}"
        if akey in self.sent:
            return
        if not armed:
            self.sent[akey] = {"skipped": "not armed"}
            self._save()
            return
        legs, syms, problems = [], [], []
        for lg in new:
            ti = by_sym.get(lg["symbol"])
            if ti is None or bars.age(ti[0]) > QUOTE_MAX_AGE:
                problems.append(f"{lg['symbol']}: no live quote")
                continue
            tok, ins = ti
            q = bars.quote.get(tok) or (None, None)
            px = (q[0] if lg["side"] == "SELL" else q[1]) or bars.ltp.get(tok)
            hedge = bool(lg.get("hedge")) and lg["side"] == "BUY"
            trig = None if hedge else (lg.get("sl") or (round(lg["entry"] * BACKSTOP, 1) if lg["side"] == "BUY" else None))
            legs.append((LegReq(ins.kite_symbol, kite.OPT_EXCHANGE.get(ins.underlying, "NFO"), lg["side"], int(lg["lots"]),
                                ins.lot_size, float(px), float(trig) if trig else None), lg["symbol"]))
        if problems:
            self.sent[akey] = {"blocked": "; ".join(problems)}
            self._save()
            self.om.event("critical", f"{meta.get('name')}: added legs not sent ({'; '.join(problems)}) — closing the live position")
            for lid in rec["legs"]:
                self.om.exit_leg(lid, "added_leg_failed")
            return
        legs.sort(key=lambda x: x[0].side != "BUY")
        try:
            self.om.armed = True
            ids = self.om.enter(sid, [l for l, _ in legs], command_id=akey, session_limits=session_limits(meta["spec"]), add_to=True)
            rec["legs"] += ids
            rec["engine_legs"] += [sym for _, sym in legs]
            self.sent[akey] = {"legs": ids}
        except GuardBlocked as e:
            self.sent[akey] = {"blocked": str(e)}
            self.om.event("critical", f"{meta.get('name')}: added legs blocked ({e}) — closing the live position")
            for lid in rec["legs"]:
                self.om.exit_leg(lid, "added_leg_blocked")
        finally:
            self.om.armed = False
        self._save()

    def _follow_engine(self, sid, meta, bt, d, now_min):
        now = self.om.clock()
        engine_pos = {(p.trade_id, p.c.symbol): p for p in bt.positions}
        open_engine = set(engine_pos)
        last_reason = {}
        for f in bt.fills:
            if f.reason != "entry":
                last_reason[(f.trade_id, f.symbol)] = f.reason
        tr = (meta.get("spec") or {}).get("risk", {}).get("trailing") or {}
        trail_step = float(tr.get("step", 1.0)) if tr else 1e9
        spec = meta.get("spec", {})
        lone_rule = spec.get("risk", {}).get("close_lone_leg_eod") and spec.get("schedule", {}).get("hold_overnight")
        for key, rec in self.sent.items():
            if not key.startswith(f"{sid}|") or "legs" not in rec:
                continue
            tid = int(key.rsplit("|", 1)[1])
            pairs = [(self.om._legs("id=?", (lid,)), sym) for lid, sym in zip(rec["legs"], rec["engine_legs"])]
            pairs = [(l[0], sym) for l, sym in pairs if l]
            legs = [l for l, _ in pairs]
            live_open = [l for l in legs if l["status"] in ("open", "entering")]
            for lg, sym in pairs:
                if lg["status"] not in ("open", "entering"):
                    self.flat_since.pop(lg["id"], None)
                    continue
                if (tid, sym) in open_engine:
                    self.flat_since.pop(lg["id"], None)
                    ep = engine_pos[(tid, sym)]
                    if lg["status"] == "open" and ep.qty != self.om._open_qty(lg):
                        # the strategy resized this leg (partial close / add): not mirrored live yet — close it, alert
                        self.om.event("critical", f"{meta.get('name')}: {sym} resized by the strategy ({self.om._open_qty(lg)} → {ep.qty}); "
                                      "live resizing is not supported — closing the live leg")
                        self.om.exit_leg(lg["id"], "resize_not_supported")
                        continue
                    if ep.sl is not None and lg["status"] == "open" and lg["sl_trigger"] is not None and abs(ep.sl - lg["sl_trigger"]) >= trail_step:
                        try:
                            self.om.move_stop(lg["id"], float(ep.sl))       # trailing: the broker stop follows
                        except Exception as e:                                # one leg's broker error never stops the rest
                            self.om.event("important", f"trail of {lg['symbol']} not moved ({type(e).__name__}); retry next minute")
                    continue
                reason = last_reason.get((tid, sym))
                if reason and reason != "stop_loss":       # explicit engine exit (time, target, spot stop, position stop…)                   # engine decided to exit (time, target, position stop…): now
                    self.om.exit_leg(lg["id"], reason)
                    self.flat_since.pop(lg["id"], None)
                    continue
                first = self.flat_since.setdefault(lg["id"], now)   # engine stop: give Kite's SL time to trigger
                if now - first >= ENGINE_FLAT_GRACE:
                    self.om.exit_leg(lg["id"], "engine_exit")
                    self.flat_since.pop(lg["id"], None)
            # broker stop closed one leg of a multi-leg trade while the engine still holds both
            stopped = [l for l in legs if l["status"] == "closed" and l["reason"] == "stop_loss"]
            if stopped and live_open and len(legs) > 1:
                note = f"{sid}|lone|{tid}"
                if note not in self.sent:
                    self.sent[note] = {"alerted": True}
                    self._save()
                    self.om.event("critical", f"{meta.get('name')}: Kite stopped {stopped[0]['symbol']}; "
                                  f"{', '.join(l['symbol'] for l in live_open)} still open"
                                  + (" — will close at 15:25 (lone-leg rule)" if lone_rule else ""))
                if lone_rule and self.om.now_ist().strftime("%H:%M") >= "15:25":
                    for l in live_open:
                        self.om.exit_leg(l["id"], "lone_leg_eod")
