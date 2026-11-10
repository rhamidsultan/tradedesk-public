"""Long straddle grid that switches to the trend - from the straddle-grid backtests (Oct 2026).

  Neutral    While NIFTY's 5-minute RSI(14) is inside the band, buy `lots` of the ATM call and put of
             the nearest expiry (`strike_offset` strikes out of the money each side; 0 = straddle).
             Each leg runs its own grid on its premium, measured from its last trade:
               premium +grid_step  -> sell 1 lot        premium -grid_step -> buy 1 lot
             When a leg is sold out, a fresh pair is bought (RSI permitting).
  Trend      When one leg has averaged down `trend_after_buys` times in a row (calls falling = NIFTY
             trending down; puts falling = trending up), every neutral leg is closed and
             `trend_lots` of the ATM option in the trend direction are bought (puts in a downtrend).
             It adds 1 lot every +grid_step (up to `trend_max_lots`) and exits on a trailing stop
             `trend_trail_pct` % below its highest price. Then neutral trading resumes.
  Day risk   Everything is closed and the day ends when the day's P&L reaches -day_stop, or, once it
             has reached +trail_arm, falls trail_giveback from its best. Square-off at exit_time.

Decisions are taken once per `check_seconds` on the last traded price, as in the 1-minute
backtest. The RSI is built from the live index; on a fresh start it is seeded from the stored
5-minute NIFTY file (data/spot/NIFTY_5minute.csv, written by the nightly recording).
Paper trading only (live_ready is False): it never sends an order to Zerodha.
"""
from __future__ import annotations

from datetime import datetime

from ..strategy import Param, Strategy, parse_time

SEED_FILE = "NIFTY_5minute.csv"


def _rsi(closes: list, n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    avg_g = avg_l = None
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        if i <= n:
            gains, losses = gains + g, losses + l
            if i == n:
                avg_g, avg_l = gains / n, losses / n
        else:
            avg_g = (avg_g * (n - 1) + g) / n
            avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


class StraddleTrendSwitch(Strategy):
    name = "straddle_trend_switch"
    title = "Straddle grid with trend switch (long options)"
    description = ("Buys an ATM straddle while NIFTY is range-bound (5-min RSI 45-55) and trades a 20-point "
                   "grid on each leg; when one leg falls twice in a row it closes the straddle and buys "
                   "options in the trend direction with pyramiding and a trailing stop. Daily stop and a "
                   "day-level trailing profit lock. Intraday only.")
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"day_stop": 0.0, "trail_arm": 0.0}
    PARAMS = {
        "lots": Param(2, "Neutral: lots per leg at entry", min=1),
        "strike_offset": Param(0, "Neutral: strikes out of the money each side (0 = ATM straddle)", min=0),
        "grid_step": Param(20.0, "Grid step in option premium points", min=1.0),
        "rsi_low": Param(45.0, "Enter neutral only if 5-min RSI(14) is at least", min=0.0, max=100.0),
        "rsi_high": Param(55.0, "...and at most", min=0.0, max=100.0),
        "entry_start": Param("09:45", "First entry time (IST, HH:MM)"),
        "entry_end": Param("14:45", "Last time a new position may be opened"),
        "exit_time": Param("15:15", "Square-off time (IST, HH:MM)"),
        "min_dte": Param(0, "Trade the nearest expiry at least this many days away (0 = include expiry day)", min=0),
        "trend_after_buys": Param(2, "Switch to the trend after this many averaging buys in a row (0 = never)", min=0),
        "trend_lots": Param(4, "Trend: lots bought on the switch", min=1),
        "trend_pyramid": Param(True, "Trend: add 1 lot every +grid_step"),
        "trend_max_lots": Param(6, "Trend: most lots when pyramiding", min=1),
        "trend_trail_pct": Param(30.0, "Trend: exit when the option falls this % from its highest price", min=1.0, max=90.0),
        "day_stop": Param(10000.0, "Day: stop for the day at this loss in Rs (0 = off)", min=0.0),
        "trail_arm": Param(8000.0, "Day: profit lock arms at this profit in Rs (0 = off)", min=0.0),
        "trail_giveback": Param(3000.0, "Day: ...then exits if the day gives back this much from its best", min=0.0),
        "check_seconds": Param(60, "Act at most once every N seconds (60 = like the 1-minute backtest)", min=1),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.day = ""
        self.closes: list = []         # completed 5-minute NIFTY closes, oldest first
        self.bucket = None             # (date, 5-minute slot) of the bar being built
        self.bucket_close = None
        self.legs: dict = {}           # contract label -> {c, ref, row, kind, peak, pair}
        self.contracts: dict = {}      # label -> contract dict (to rebuild after a restart)
        self.want = True
        self.halted = ""
        self.trend_on = False
        self.day_start = 0.0           # ctx.net_realized when the day started
        self.best = 0.0
        self.pairs = 0
        self.trends = 0
        self.last_check = None
        self.seeded = False

    # ---- state ----------------------------------------------------------------------------
    def get_state(self) -> dict:
        legs = {k: {kk: vv for kk, vv in v.items() if kk != "c"} for k, v in self.legs.items()}
        return {"day": self.day, "closes": self.closes[-80:], "bucket": list(self.bucket) if self.bucket else None,
                "bucket_close": self.bucket_close, "legs": legs, "contracts": self.contracts, "want": self.want,
                "halted": self.halted, "trend_on": self.trend_on, "day_start": self.day_start, "best": self.best,
                "pairs": self.pairs, "trends": self.trends, "seeded": self.seeded}

    def set_state(self, s: dict) -> None:
        from ..models import Contract
        self.day = s.get("day", "")
        self.closes = s.get("closes", [])
        self.bucket = tuple(s["bucket"]) if s.get("bucket") else None
        self.bucket_close = s.get("bucket_close")
        self.contracts = s.get("contracts", {})
        self.legs = {}
        for k, v in (s.get("legs") or {}).items():
            if k in self.contracts:
                self.legs[k] = dict(v, c=Contract.from_dict(self.contracts[k]))
        self.want, self.halted = s.get("want", True), s.get("halted", "")
        self.trend_on = s.get("trend_on", False)
        self.day_start, self.best = s.get("day_start", 0.0), s.get("best", 0.0)
        self.pairs, self.trends = s.get("pairs", 0), s.get("trends", 0)
        self.seeded = s.get("seeded", False)

    # ---- helpers --------------------------------------------------------------------------
    def _seed(self, ctx) -> None:
        """Fill the RSI history from the stored 5-minute file (bars that closed before now)."""
        self.seeded = True
        if len(self.closes) >= 15:
            return
        try:
            import pandas as pd
            from ..config import SPOT_DIR
            df = pd.read_csv(SPOT_DIR / SEED_FILE, usecols=["datetime", "close"], parse_dates=["datetime"])
            df = df[df["datetime"] + pd.Timedelta(minutes=5) <= pd.Timestamp(ctx.now.replace(tzinfo=None))]
            seed = [float(x) for x in df["close"].tail(60)]
            if seed:
                self.closes = seed + self.closes
                ctx.log(f"RSI history seeded with {len(seed)} stored 5-minute closes")
        except Exception as e:  # noqa: BLE001 - without a seed the RSI just warms up live
            ctx.log(f"RSI seed not available ({e}); RSI starts after 15 live 5-minute bars")

    def _track_bars(self, ctx) -> None:
        now = ctx.now
        slot = (now.date().isoformat(), (now.hour * 60 + now.minute) // 5)
        if self.bucket is not None and slot != self.bucket and self.bucket_close is not None:
            self.closes.append(self.bucket_close)
            self.closes = self.closes[-200:]
        self.bucket, self.bucket_close = slot, float(ctx.spot)

    def rsi(self):
        return _rsi(self.closes[-120:], 14)

    def day_pnl(self, ctx) -> float:
        return ctx.net_realized - self.day_start + ctx.unrealized

    def _lots(self, ctx, leg) -> int:
        pos = ctx.positions.get(leg["c"])
        return abs(pos.qty) // ctx.lot_size_for(leg["c"].expiry) if pos else 0

    def _add_leg(self, c, price, kind, pair) -> None:
        self.contracts[c.label] = c.to_dict()
        self.legs[c.label] = {"c": c, "ref": price, "row": 0, "kind": kind, "peak": price, "pair": pair}

    def _close_legs(self, ctx, reason, kinds=("neutral", "trend")) -> None:
        for k, leg in list(self.legs.items()):
            if leg["kind"] in kinds:
                if ctx.positions.get(leg["c"]):
                    ctx.close(leg["c"], reason)
                self.legs.pop(k, None)
        if "trend" in kinds:
            self.trend_on = False

    def status(self, ctx) -> dict | None:
        r = self.rsi()
        note = (f"Stopped for the day: {self.halted}" if self.halted else
                "Trend trade on" if self.trend_on else
                "Neutral straddle on" if self.legs else "Waiting for RSI in band")
        lv = {"RSI(14) 5-min": round(r, 1) if r is not None else None}
        for leg in self.legs.values():
            lv[f"{leg['c'].strike} {leg['c'].right} ref"] = round(leg["ref"], 2)
        return {"pnl": self.day_pnl(ctx), "stop": -self.p.day_stop if self.p.day_stop else None,
                "levels": {k: v for k, v in lv.items() if v is not None}, "note": note}

    def _enter(self, ctx, expiry, reason: str = "Margin check", group: str = "") -> list:
        """The neutral entry legs (buy `lots` of the call and put). The margin check uses this."""
        atm, off = ctx.atm_strike(), self.p.strike_offset * ctx.strike_step
        out = []
        for right, k in (("CE", atm + off), ("PE", atm - off)):
            c = ctx.option(expiry, k, right)
            out.append((c, ctx.buy(c, self.p.lots, reason, group)))
        return out

    # ---- the bar ---------------------------------------------------------------------------
    def on_bar(self, ctx) -> None:
        p, now = self.p, ctx.now
        if not self.seeded:
            self._seed(ctx)
        self._track_bars(ctx)
        today = now.date().isoformat()
        if self.day != today:
            if ctx.positions:   # a position left from an earlier day (e.g. a restart): square it off
                ctx.close_all("Square-off (left from an earlier day)")
            self.day, self.legs, self.want, self.halted, self.trend_on = today, {}, True, "", False
            self.day_start, self.best, self.last_check = ctx.net_realized, 0.0, None
        # drop legs whose position is gone (settled, closed elsewhere)
        for k, leg in list(self.legs.items()):
            if not ctx.positions.get(leg["c"]):
                self.legs.pop(k)
        if self.trend_on and not any(l["kind"] == "trend" for l in self.legs.values()):
            self.trend_on = False

        t = now.time()
        if t >= parse_time(p.exit_time):
            if ctx.positions:
                ctx.close_all("Square-off")
                self.legs, self.trend_on = {}, False
            self.halted = self.halted or "after square-off time"
            return
        if self.halted:
            return
        stamp = now.replace(tzinfo=None) if isinstance(now, datetime) else now
        if self.last_check is not None and (stamp - self.last_check).total_seconds() < p.check_seconds - 0.5:
            return
        self.last_check = stamp

        # ---- day-level risk
        pnl = self.day_pnl(ctx)
        self.best = max(self.best, pnl)
        if p.day_stop and pnl <= -p.day_stop:
            ctx.close_all("Daily stop")
            self.legs, self.trend_on, self.halted = {}, False, f"daily stop (Rs {pnl:,.0f})"
            return
        if p.trail_arm and self.best >= p.trail_arm and pnl <= self.best - p.trail_giveback:
            ctx.close_all("Day profit lock")
            self.legs, self.trend_on, self.halted = {}, False, f"profit lock (best Rs {self.best:,.0f}, now Rs {pnl:,.0f})"
            return

        step = p.grid_step
        switch = None
        for k, leg in list(self.legs.items()):
            c = leg["c"]
            px = ctx.price(c)
            if not px or px != px:
                continue
            lots = self._lots(ctx, leg)
            if leg["kind"] == "trend":
                if p.trend_pyramid:
                    add = 0
                    while px >= leg["ref"] + step and lots + add < p.trend_max_lots:
                        add += 1
                        leg["ref"] += step
                    if add:
                        ctx.buy(c, add, "Trend add", leg["pair"])
                leg["peak"] = max(leg["peak"], px)
                if px <= leg["peak"] * (1 - p.trend_trail_pct / 100):
                    ctx.close(c, f"Trend trailing stop (peak {leg['peak']:.2f})")
                    self.legs.pop(k)
                    self.trend_on, self.want = False, True
                continue
            n_sell = 0
            while lots - n_sell > 0 and px >= leg["ref"] + step:
                n_sell += 1
                leg["ref"] += step
            if n_sell:
                leg["row"] = 0
                if n_sell >= lots:
                    ctx.close(c, "Grid sell (leg flat)")
                    self.legs.pop(k)
                    self.want = True
                else:
                    ctx.sell(c, n_sell, "Grid sell", leg["pair"])
                continue
            n_buy = 0
            while px <= leg["ref"] - step:
                n_buy += 1
                leg["ref"] -= step
            if n_buy:
                ctx.buy(c, n_buy, "Grid buy", leg["pair"])
                leg["row"] += n_buy
                if (p.trend_after_buys and not self.trend_on and switch is None
                        and leg["row"] >= p.trend_after_buys and t <= parse_time(p.entry_end)):
                    switch = ("PE" if c.right == "CE" else "CE", f"{c.strike} {c.right} fell {leg['row']} steps")

        if switch is not None:
            right, why = switch
            self._close_legs(ctx, f"Switch to trend ({why})", kinds=("neutral",))
            expiry = ctx.pick_expiry(p.min_dte)
            if expiry is not None:
                c = ctx.option(expiry, ctx.atm_strike(), right)
                self.trends += 1
                group = f"T{self.trends:03d} | {now:%d %b %y}"
                fill = ctx.buy(c, p.trend_lots, f"Trend entry {'down' if right == 'PE' else 'up'} ({why})", group)
                self._add_leg(c, fill.price, "trend", group)
                self.trend_on, self.want = True, False
            return

        # ---- neutral entry
        r = self.rsi()
        if (self.want and not self.trend_on and r is not None and p.rsi_low <= r <= p.rsi_high
                and parse_time(p.entry_start) <= t <= parse_time(p.entry_end)):
            expiry = ctx.pick_expiry(p.min_dte)
            if expiry is None:
                return
            atm = ctx.atm_strike()
            off = p.strike_offset * ctx.strike_step
            self.pairs += 1
            group = f"{self.pairs:03d} | {now:%d %b %y}"
            for right, k in (("CE", atm + off), ("PE", atm - off)):
                c = ctx.option(expiry, k, right)
                if c.label in self.legs:      # same strike still held: add to it and reset its grid
                    fill = ctx.buy(c, p.lots, f"Entry (RSI {r:.1f})", group)
                    self.legs[c.label].update(ref=fill.price, row=0)
                    continue
                fill = ctx.buy(c, p.lots, f"Entry (RSI {r:.1f})", group)
                self._add_leg(c, fill.price, "neutral", group)
            self.want = False
