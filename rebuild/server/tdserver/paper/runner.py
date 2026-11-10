"""The paper runner: `python -m tdserver.paper` (its own systemd unit). It never places an order.

Every minute in market hours, about 20 seconds after the minute closes (the recorder writes every 15 seconds),
each active session replays today's closed bars from the store. After the F&O close (+5 minutes) each session
books the day. Missed days (the service was down) are booked from the store when it comes back. Alerts: each
new fill, a kill rule (the session is paused: no new entries), and the day's result.
"""
from __future__ import annotations

import logging
import signal
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import select

from ..alerts.outbox import raise_alert
from ..context import AppContext
from ..models import PaperRun
from .engine import PaperEngine
from .margins import EVERY_MIN
from .money import inr

log = logging.getLogger("tdserver.paper")

READ_AFTER_S = 20            # seconds after a minute closes before its bar is read
BOOK_AFTER = timedelta(minutes=5)   # after the F&O close
N_MIN = 375                  # minutes 09:15..15:29 (the engine's day)
COMPARE_DAYS = 60            # calendar days compared after each close


def minute_index(now: datetime) -> int:
    return (now.hour * 60 + now.minute) - (9 * 60 + 15)


def folder(ctx: AppContext, run_id: int) -> Path:
    base = Path(ctx.settings.runs_dir or (Path(ctx.settings.store_root or ".") / "runs")).expanduser()
    return base / "paper" / str(run_id)


def default_rules(spec: dict) -> dict:
    """Fixed when a session starts and never changed afterwards."""
    capital = float((spec.get("costs") or {}).get("capital") or 200000)
    day_limit = float((spec.get("risk") or {}).get("max_loss_per_day") or 0)
    return {"kill": {"cum_net_below": round(capital * 0.075), "day_worse_than": round(max(day_limit * 1.5, capital * 0.05)),
                     "median_slip_above": 4.0, "after_stops": 10},
            "gate": {"cycles": 20, "weeks": 8, "stop_events": 15}}


def day_actions(r: PaperRun, d: date) -> dict:
    """The actions the engine replays on day d: blocked minute ranges (pauses) and a square-off minute."""
    acts = r.actions or {}
    act = dict(acts.get(str(d), {}))
    blocked = [list(x) for x in act.get("blocked", [])]
    p = acts.get("_paused")
    if p and r.status in ("paused", "stopping"):
        pd = date.fromisoformat(p["day"])
        if d > pd:
            blocked.append([0, 10 ** 6])
        elif d == pd:
            blocked.append([p["minute"], 10 ** 6])
    act["blocked"] = blocked
    return act


def pause(r: PaperRun, d: date, minute: int) -> None:
    """No new entries from (d, minute). Open legs keep their stops and exits."""
    acts = dict(r.actions or {})
    acts.setdefault("_paused", {"day": str(d), "minute": int(minute)})
    r.actions, r.status = acts, "paused"


def resume(r: PaperRun, d: date, minute: int) -> None:
    """Entries again from (d, minute): the pause becomes a closed range on the day it ends."""
    acts = dict(r.actions or {})
    p = acts.pop("_paused", None)
    if p:
        pd = date.fromisoformat(p["day"])
        def block(day: date, a: int, b: int):
            x = dict(acts.get(str(day), {}))
            x["blocked"] = list(x.get("blocked", [])) + [[a, b]]
            acts[str(day)] = x
        if pd == d:
            block(d, p["minute"], minute)
        else:
            block(pd, p["minute"], 10 ** 6)          # days in between were booked while paused
            block(d, 0, minute)
    r.actions, r.status = acts, "running"


def square_off(r: PaperRun, d: date, minute: int) -> None:
    """Every square-off of the day is kept (a list), so a later one never rewrites an earlier exit."""
    acts = dict(r.actions or {})
    day = dict(acts.get(str(d), {}))
    prev = day.get("square_off")
    mins = list(prev) if isinstance(prev, list) else ([prev] if prev is not None else [])
    if int(minute) not in mins:
        mins.append(int(minute))
    day["square_off"] = sorted(mins)
    acts[str(d)] = day
    r.actions = acts


class Days:
    """Day data from the store, loaded once per underlying per pass."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._feeds: dict = {}
        self._store = None

    def feed(self, und: str):
        if und not in self._feeds:
            from tradedesk_engine.data import StoreFeed
            from tradedesk_marketdata import Store
            if self._store is None:
                self._store = Store(self.ctx.settings.store_root, memory_limit=self.ctx.settings.duckdb_memory)
            self._feeds[und] = StoreFeed(self._store, und)
        return self._feeds[und]

    def load(self, und: str, d: date):
        return self.feed(und).load(d)

    def quotes(self, und: str, d: date) -> dict:
        f = Path(self.ctx.settings.store_root) / "quotes_1m" / "opt" / und / f"date={d}" / "kite_live_0.parquet"
        if not f.exists():
            return {}
        out = {}
        for r in pq.read_table(f).to_pylist():
            m = minute_index(r["ts"])
            out[(r["symbol"], m)] = (r["bid"], r["ask"])
        return out


class PaperRunner:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.engines: dict[int, PaperEngine] = {}
        self.last_minute: tuple[date, int] | None = None

    def engine(self, r: PaperRun) -> PaperEngine:
        key = (r.id, r.first_day, r.start_minute)     # a reset changes the start: reload from disk
        e = self.engines.get(key)
        if e is None:
            for k in [k for k in self.engines if k[0] == r.id]:
                del self.engines[k]
            e = PaperEngine(folder(self.ctx, r.id), r.spec)
            self.engines[key] = e
        return e

    def forget(self, run_id: int) -> None:
        for k in [k for k in self.engines if k[0] == run_id]:
            del self.engines[k]

    # ------------------------------------------------------------------ one pass
    def tick(self, now: datetime | None = None, force: bool = False) -> str:
        now = now or self.ctx.clock.now_ist()
        d = now.date()
        cal = self.ctx.calendar
        trading = cal.is_trading_day(d)
        sess = cal.session(d) if trading else None
        until = minute_index(now)
        if trading and now.second < READ_AFTER_S and not force:
            return "waiting for the bar"
        if not force and self.last_minute == (d, until):
            return "done this minute"
        self.last_minute = (d, until)
        days = Days(self.ctx)
        booked_by = datetime.combine(d, sess.fo_close) + BOOK_AFTER if sess else None
        with self.ctx.sessions() as db:
            runs = db.scalars(select(PaperRun).where(PaperRun.status != "stopped")).all()
            for r in runs:
                try:
                    self._catch_up(db, r, d, days)
                    if not trading or d < r.first_day:
                        continue
                    if booked_by and now.replace(tzinfo=None) >= booked_by:
                        if r.last_day is None or r.last_day < d:
                            self._book(db, r, d, days)
                    elif 0 < until:
                        self._step(db, r, d, min(until, N_MIN), days)
                except Exception as e:  # noqa: BLE001 - one session never stops the others
                    log.error("paper session %s failed: %s: %s", r.id, type(e).__name__, e)
                    r.state = {**(r.state or {}), "error": f"{type(e).__name__}: {e}"[:300]}
                db.commit()
        return f"{len(runs)} sessions"

    def _catch_up(self, db, r: PaperRun, today: date, days: Days) -> None:
        """Book trading days the service missed (between the last booked day and yesterday)."""
        d = max(r.first_day, (r.last_day + timedelta(days=1)) if r.last_day else r.first_day)
        while d < today:
            if self.ctx.calendar.is_trading_day(d):
                self._book(db, r, d, days)
            d += timedelta(days=1)

    def _act(self, r: PaperRun, d: date) -> tuple[dict, int]:
        return day_actions(r, d), (r.start_minute if d == r.first_day else 0)

    def _step(self, db, r: PaperRun, d: date, until: int, days: Days) -> None:
        e = self.engine(r)
        if e.skip(d):
            return
        act, sm = self._act(r, d)
        day = days.load(r.underlying, d)
        e.step(day, until, act, sm)
        st = e.state(d, r.rules, until=until)
        st["spot"] = None
        if day.spot is not None and len(day.spot.m):
            j = day.spot.last_before(until)
            st["spot"] = float(day.spot.c[j]) if j >= 0 else None
        prev = (r.state or {}).get("margin")
        if not st["positions"]:
            st["margin"] = None
        elif prev is None or until % EVERY_MIN == 0 or prev.get("legs") != len(st["positions"]):
            from .margins import session_margin
            m = session_margin(self.ctx, db, r.user_id, r.underlying, d, st["positions"])
            st["margin"] = {**m, "legs": len(st["positions"])} if m else None
        else:
            st["margin"] = prev
        for k in ("vs_backtest", "kept_days"):                     # from the last close
            st[k] = (r.state or {}).get(k, [])
        self._publish(db, r, st, d)

    def _book(self, db, r: PaperRun, d: date, days: Days) -> None:
        e = self.engine(r)
        if not e.skip(d):
            act, sm = self._act(r, d)
            e.close_day(days.load(r.underlying, d), act, sm, days.quotes(r.underlying, d))
        r.last_day = d
        if r.status == "stopping":
            r.status, r.ended_at = "stopped", self.ctx.clock.now()
        st = e.state(d, r.rules)
        st["vs_backtest"] = self._compare(r, d, days, st)
        st["kept_days"] = e.kept_days()
        self._publish(db, r, st, d)
        if st.get("day_date") == str(d):
            self._alert(db, r, f"day|{d}", "info", f"Paper day · {r.name}",
                        f"{d.day} {d:%b %Y}: {inr(st['day_pnl'], signed=True)} net · total {inr(st['cum_net'], signed=True)}")
        nxt = self.ctx.calendar.next_trading_day(d)
        if nxt.isocalendar()[:2] != d.isocalendar()[:2]:             # last trading day of the week
            week = [x for x in st.get("daily", []) if date.fromisoformat(x[0]).isocalendar()[:2] == d.isocalendar()[:2]]
            g = st.get("gate", {})
            self._alert(db, r, f"week|{d.isocalendar()[0]}-{d.isocalendar()[1]}", "info", f"Paper week · {r.name}",
                        f"Week to {d.day} {d:%b}: {inr(sum(x[1] for x in week), signed=True)} net over {len(week)} days · total "
                        f"{inr(st['cum_net'], signed=True)} · gate: {g.get('cycles', 0)}/{g.get('cycles_needed')} cycles, "
                        f"{g.get('weeks', 0)}/{g.get('weeks_needed')} weeks, {g.get('stop_events', 0)}/"
                        f"{g.get('stop_events_needed')} stops")

    def _compare(self, r: PaperRun, d: date, days: Days, st: dict) -> list[dict]:
        """Paper vs a clean backtest of the same spec on the same stored bars (no trader actions, no mid-day start):
        differences come from pauses, square-offs, the start minute and bars that arrived late or not at all."""
        try:
            from tradedesk_engine import Backtest
            from tradedesk_spec import Strategy
            start = max(r.first_day, d - timedelta(days=COMPARE_DAYS))
            bt = Backtest(Strategy.model_validate(r.spec), days.feed(r.underlying))
            res = bt.run(start, d)
            clean = dict(res.daily)
        except Exception as e:  # noqa: BLE001
            log.warning("paper compare %s: %s", r.id, type(e).__name__)
            return (r.state or {}).get("vs_backtest", [])
        out = []
        for day_s, pnl in st.get("daily", []):
            if day_s in clean:
                out.append({"date": day_s, "paper": pnl, "backtest": clean[day_s], "diff": round(pnl - clean[day_s], 2)})
        return out[-COMPARE_DAYS:]

    # ------------------------------------------------------------------ results and alerts
    def _publish(self, db, r: PaperRun, st: dict, d: date) -> None:
        r.state, r.updated_at = st, self.ctx.clock.now()
        for f in st.get("fills_today", []):
            key = f"fill|{f['ts']}|{f['symbol']}|{f['side']}"
            what = {"stop_loss": "stop", "target": "target", "manual": "square-off"}.get(f.get("reason"), f.get("reason") or "")
            self._alert(db, r, key, "info", f"Paper fill · {r.name}",
                        f"{f['side']} {f['qty']} {f['symbol']} at {inr(f['price'], 2)} ({what}) · {f['ts'][11:16]}")
        if st.get("kill") and r.status == "running":
            # a kill rule pauses the session: no new entries; open legs keep their stops
            pause(r, d, max(0, min(minute_index(self.ctx.clock.now_ist()) + 1, N_MIN)))
            for k in st["kill"]:
                self._alert(db, r, f"kill|{k}", "critical", f"Paper kill rule · {r.name}",
                            k + ". The session is paused (no new entries). Return it to research.")

    def _alert(self, db, r: PaperRun, key: str, level: str, title: str, body: str) -> None:
        seen = list(r.alerted or [])
        if key in seen:
            return
        raise_alert(db, self.ctx, r.user_id, level, "paper", title, body, path=f"/trade/paper/{r.id}",
                    dedupe_key=f"paper{r.id}|{key}", once=True)
        r.alerted = (seen + [key])[-500:]


def main() -> None:
    from ..context import build_context, quiet_http_logs
    from ..settings import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    quiet_http_logs()
    ctx = build_context(get_settings())
    runner = PaperRunner(ctx)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    while not stop["now"]:
        try:
            runner.tick()
            # a reset or a deleted session: drop its engine so it is reloaded from disk
            with ctx.sessions() as db:
                live = set(db.scalars(select(PaperRun.id).where(PaperRun.status != "stopped")).all())
            for rid in {k[0] for k in runner.engines}:
                if rid not in live or not folder(ctx, rid).joinpath("base.pkl").exists():
                    runner.forget(rid)
        except Exception as e:  # noqa: BLE001
            log.error("paper pass failed: %s", type(e).__name__)
        _time.sleep(5)


if __name__ == "__main__":
    main()
