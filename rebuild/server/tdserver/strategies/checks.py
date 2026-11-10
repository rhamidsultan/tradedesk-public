"""Plain-language checks for a strategy: what to fix before it can be saved or backtested.

The rules themselves live in the spec (py/spec: a stop loss and a daily loss limit are required, a seller needs a
premium stop, and so on). This module only turns the validator's messages into sentences a trader can act on,
and adds a few warnings that do not block saving."""
from __future__ import annotations

FIELD_NAMES = {
    "instrument": "Instrument", "underlying": "index", "timeframe": "bar length",
    "schedule": "Schedule", "start": "start time", "no_new_after": "no new entries after", "exit_by": "exit by",
    "expiry_exit_time": "expiry exit time", "days": "days",
    "entry": "Entry", "any_of": "rules", "all_of": "conditions", "at_time": "time", "action": "name",
    "legs": "Legs", "strike": "strike", "lots": "lots", "side": "buy or sell", "type": "call or put",
    "risk": "Risk", "stop_loss": "stop loss", "max_loss_per_day": "max loss a day", "target": "target",
    "trailing": "trailing stop", "risk_per_trade": "risk per trade", "max_lots": "max lots",
    "max_trades_per_day": "max trades a day", "value": "value", "costs": "Costs", "capital": "capital",
    "slippage_pct": "slippage", "name": "name", "id": "id",
}

SENTENCES = {
    "stop loss cannot be a risk:reward ratio": "Set the stop loss in points, % or a multiple of the premium.",
    "a trailing stop needs a premium stop loss": "A trailing stop works on the option premium: set the stop loss on premium.",
    "selling options needs a premium stop loss per leg": "Selling options needs a stop loss on each leg's premium, so it can sit at Zerodha.",
    "overnight positions need a premium stop loss (placed at the broker)": "Positions held overnight need a premium stop loss placed at Zerodha.",
    "a no-code strategy needs entry rules and legs": "Add at least one entry rule and one leg.",
    "lots=from_risk needs risk.risk_per_trade": "Lots from risk needs the ₹ you risk per trade.",
    "need start <= no_new_after <= exit_by": "Times must run in order: start, then no new entries after, then exit by.",
}


def _where(loc) -> str:
    parts = []
    for p in loc:
        if isinstance(p, int):
            parts.append(f"#{p + 1}")
        elif p in FIELD_NAMES:
            parts.append(FIELD_NAMES[p])
    return " › ".join(parts)


def _sentence(msg: str) -> str:
    m = msg.removeprefix("Value error, ")
    for k, v in SENTENCES.items():
        if k in m:
            return v
    if m.startswith("Field required"):
        return "This is required."
    if "greater than 0" in m:
        return "Must be more than 0."
    if "outside the 09:15–15:29 session" in m:
        return m.capitalize().replace("15:29 session", "15:29 session.")
    if "has no entry rule with that action" in m:
        return "This leg opens on an entry name that no rule uses: pick one of the entry rules."
    if "opens no legs" in m:
        return "An entry rule opens no legs: add a leg for it or remove the rule."
    if m.startswith("Input should be"):
        return m.replace("Input should be", "Choose").rstrip(".") + "."
    if m.startswith("Extra inputs are not permitted"):
        return "Not a setting this version knows."
    return m[:1].upper() + m[1:]


def validate(spec: dict) -> tuple[object | None, list[dict]]:
    """(Strategy or None, problems). Each problem: where, text (blocking)."""
    from tradedesk_spec import Strategy
    from pydantic import ValidationError
    try:
        return Strategy.model_validate(spec), []
    except ValidationError as e:
        out, seen = [], set()
        for err in e.errors():
            item = {"where": _where(err.get("loc", ())) or "Strategy", "text": _sentence(err.get("msg", ""))}
            key = (item["where"], item["text"])
            if key not in seen:
                seen.add(key)
                out.append(item)
        return None, out


def warnings(strat) -> list[dict]:
    """Non-blocking advice."""
    w = []
    s = strat
    if s.schedule.hold_overnight and not s.risk.close_lone_leg_eod and len(s.legs) > 1:
        w.append({"where": "Risk", "text": "A leg can be left alone overnight after its pair stops. Consider closing lone legs at 15:25."})
    if any(l.side == "SELL" for l in s.legs) and not any(l.side == "BUY" for l in s.legs):
        w.append({"where": "Legs", "text": "Short options with no bought wing: a gap can lose more than the stop loss."})
    if s.risk.max_loss_per_day > 0.04 * s.costs.capital:
        w.append({"where": "Risk › max loss a day",
                  "text": f"Above 4% of capital (₹{0.04 * s.costs.capital:,.0f})."})
    if s.costs.fill_model == "classic":
        w.append({"where": "Costs", "text": "Classic fills are optimistic; backtests from the app use honest fills."})
    return w
