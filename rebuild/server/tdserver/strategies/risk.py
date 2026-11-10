"""Risk summary for a strategy before it is saved: loss per trade at the stop, target, charges, lots the capital
can carry, and the strangle "app down" worst case, against the 2% per trade and 4% per day marks.

Premiums come from the strike rule (premium-based legs) or from the last stored option chain (ATM-offset legs).
Lot sizes and charges come from the dated reference data (py/marketcal), never from constants."""
from __future__ import annotations

from datetime import date

from ..context import AppContext

BUDGET_PCT = 2.0      # per trade, owner's default (22 Oct)
DAY_PCT = 4.0         # per day


def _lot(underlying: str, day: date) -> int | None:
    from tradedesk_marketcal import load_refs
    ref = load_refs().lot_size(underlying, day)
    return ref.value if ref else None


def _chain_premium(ctx: AppContext, underlying: str, opt_type: str, offset: int) -> tuple[float | None, float | None]:
    """(premium, spot) for an ATM-offset leg from the last stored day's nearest expiry chain."""
    data = ctx.data
    if data is None or not data.available:
        return None, None
    days = data.days("opt", underlying)
    if not days:
        return None, None
    last = days[-1]
    exps = [e for e in data.expiries(underlying) if e["expiry"] >= last.isoformat()]
    if not exps:
        return None, None
    c = data.chain(underlying, exps[0]["expiry"], last.isoformat(), "15:29")
    spot = c.get("spot")
    rows = [r for r in c["strikes"] if r.get(opt_type)]
    if not rows or spot is None:
        return None, spot
    rows.sort(key=lambda r: r["strike"])
    atm = min(range(len(rows)), key=lambda i: abs(rows[i]["strike"] - spot))
    j = atm + int(offset)
    if not 0 <= j < len(rows):
        return None, spot
    return rows[j][opt_type]["ltp"], spot


def _leg_loss(level, premium: float, qty: int, delta: float, short: bool) -> float | None:
    if level is None or premium is None:
        return None
    v = level.value
    if level.on == "underlying":
        return v * delta * qty if level.type == "points" else None
    if level.type == "multiple":
        return (v - 1) * premium * qty if short else (1 - v) * premium * qty if v < 1 else None
    if level.type == "percent":
        return premium * v / 100 * qty
    if level.type == "points":
        return v * qty
    return None


def summary(ctx: AppContext, strat, today: date | None = None) -> dict:
    from tradedesk_marketcal import load_refs
    cal = load_refs()
    today = today or ctx.clock.now_ist().date()
    r, cs = strat.risk, strat.costs
    u = strat.instrument.underlying
    lot = _lot(u, today)
    legs, spot = [], None
    for leg in strat.legs:
        if leg.strike.by == "premium":
            prem = leg.strike.value
        else:
            prem, sp = _chain_premium(ctx, u, leg.type, int(leg.strike.value))
            spot = spot or sp
        lots = leg.lots if isinstance(leg.lots, int) else 1
        qty = (lot or 0) * lots
        short = leg.side == "SELL"
        loss = _leg_loss(r.stop_loss, prem, qty, r.assumed_delta, short) if qty else None
        if r.target is not None and r.target.type == "rr":
            gain = loss * r.target.value if loss is not None else None
        else:
            gain = _leg_loss(r.target, prem, qty, r.assumed_delta, not short) if (r.target and qty) else None
        ch = None
        if prem is not None and qty:
            ch = round(cal.option_charges(today, "SELL" if short else "BUY", prem * qty)
                       + cal.option_charges(today, "BUY" if short else "SELL", prem * qty), 2)
        legs.append({"side": leg.side, "type": leg.type, "strike": leg.strike.model_dump(), "lots": lots, "qty": qty,
                     "premium": round(prem, 2) if prem is not None else None,
                     "loss_at_stop": round(loss, 0) if loss is not None else None,
                     "gain_at_target": round(gain, 0) if gain is not None else None, "charges": ch})
    known = [l for l in legs if l["loss_at_stop"] is not None]
    loss = sum(l["loss_at_stop"] for l in known) + sum(l["charges"] or 0 for l in legs)
    budget = cs.capital * BUDGET_PCT / 100
    day_mark = cs.capital * DAY_PCT / 100
    out = {
        "lot_size": lot, "capital": cs.capital, "legs": legs,
        "loss_per_trade": round(loss, 0) if known else None,
        "loss_pct_of_capital": round(loss / cs.capital * 100, 2) if known else None,
        "budget_2pct": round(budget, 0), "day_mark_4pct": round(day_mark, 0),
        "max_loss_per_day": r.max_loss_per_day,
        "charges_per_trade": round(sum(l["charges"] or 0 for l in legs), 2),
        "unknown_premiums": len(legs) - len(known),
    }
    out["above_budget"] = bool(known and loss > budget)
    # lots from free margin (estimate; Kite's basket margin replaces it in phase 4)
    shorts = [l for l in strat.legs if l.side == "SELL"]
    if shorts and lot and spot:
        mr = cal.raw.get("margin_estimate", {})
        strangle = {l.type for l in shorts} == {"CE", "PE"}
        pct = mr.get("short_strangle_pct" if strangle else "short_single_pct", 12)
        per_lot = spot * lot * pct / 100 * (1 if strangle else len(shorts))
        out["margin_per_lot"] = round(per_lot, 0)
        out["lots_capital_carries"] = int(cs.capital // per_lot) if per_lot else None
    # strangle: both legs stopped, with the SL-limit buffer (a spike fills beyond the trigger)
    if len(shorts) >= 2 and r.stop_loss.on == "premium" and r.stop_loss.type == "multiple":
        buf = cs.sl_limit_buffer_pct / 100
        worst = 0.0
        for l in legs:
            if l["side"] == "SELL" and l["premium"] is not None and l["qty"]:
                worst += (r.stop_loss.value * (1 + buf) - 1) * l["premium"] * l["qty"]
        worst += out["charges_per_trade"]
        out["app_down_worst"] = round(worst, 0)
        out["app_down_vs_2pct"] = round(worst / budget, 2) if budget else None
        out["app_down_vs_4pct"] = round(worst / day_mark, 2) if day_mark else None
    # Everything above the marks must be accepted, in rupees, before saving: the largest amount covers the rest.
    items = []
    if out["above_budget"]:
        items.append({"what": "One trade at its stop, above 2% of capital", "amount": round(loss, 0)})
    if out.get("app_down_worst") and out["app_down_worst"] > budget:
        items.append({"what": "Strangle worst case (both stops past the trigger), above 2%", "amount": out["app_down_worst"]})
    if r.max_loss_per_day > day_mark:
        items.append({"what": "Max loss a day, above 4% of capital", "amount": round(r.max_loss_per_day, 0)})
    out["accept_items"] = items
    out["accept_required"] = max((i["amount"] for i in items), default=None)
    return out
