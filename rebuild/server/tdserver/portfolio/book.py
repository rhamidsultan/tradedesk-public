"""Portfolio arithmetic, ported from the Algo Lab dashboard (server/portfolio/app.py) without its globals.

build(raw)            Zerodha holdings -> rows and a summary (value, invested, P&L, day P&L, gold share)
parse_tradebook(...)  a Console tradebook (CSV or Excel) -> trade rows
replay(trades, acts)  every trade and corporate action in date order with first-in-first-out lots
stock_history(...)    invested, value and P&L of one stock over time, reconciled with Zerodha's own figures
"""
from __future__ import annotations

import csv
import io


def is_gold(symbol: str, gold: list[str] | None = None, non_gold: list[str] | None = None) -> bool:
    if symbol in (non_gold or []):
        return False
    return symbol in (gold or []) or "GOLD" in symbol


def build(raw: list[dict], gold: list[str] | None = None, non_gold: list[str] | None = None) -> tuple[list[dict], dict]:
    rows = []
    for h in raw:
        qty = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0) + (h.get("collateral_quantity") or 0)
        if qty <= 0:
            continue
        avg, last = h.get("average_price") or 0, h.get("last_price") or 0
        close = h.get("close_price") or last
        invested, value = avg * qty, last * qty
        rows.append({
            "symbol": h["tradingsymbol"], "exchange": h.get("exchange", ""),
            "instrument_token": h.get("instrument_token"),
            "asset": "Gold" if is_gold(h["tradingsymbol"], gold, non_gold) else "Equity",
            "qty": qty, "avg_price": avg, "last_price": last, "close_price": close,
            "invested": invested, "value": value, "pnl": value - invested,
            "pnl_pct": (value - invested) / invested * 100 if invested else 0,
            "day_pnl": (last - close) * qty,
            "day_pct": (last - close) / close * 100 if close else 0,
        })
    rows.sort(key=lambda r: -r["value"])
    value = sum(r["value"] for r in rows)
    invested = sum(r["invested"] for r in rows)
    day_pnl = sum(r["day_pnl"] for r in rows)
    gold_v = sum(r["value"] for r in rows if r["asset"] == "Gold")
    prev = value - day_pnl
    for r in rows:
        r["weight"] = r["value"] / value * 100 if value else 0
    summary = {
        "value": value, "invested": invested, "pnl": value - invested,
        "pnl_pct": (value - invested) / invested * 100 if invested else 0,
        "day_pnl": day_pnl, "day_pct": day_pnl / prev * 100 if prev else 0,
        "gold_value": gold_v, "gold_pct": gold_v / value * 100 if value else 0,
        "count": len(rows), "up": sum(1 for r in rows if r["day_pnl"] > 0), "down": sum(1 for r in rows if r["day_pnl"] < 0),
    }
    return rows, summary


def norm_id(x) -> str:
    return str(x if x is not None else "").strip().lstrip("0") or "0"


def parse_tradebook(name: str, data: bytes) -> list[dict]:
    """Read a Zerodha Console tradebook (Excel or CSV) into trade rows."""
    if name.lower().endswith(".xlsx"):
        try:
            import openpyxl
        except ImportError:
            raise ValueError("Excel files need the openpyxl package. Download the tradebook as CSV instead.") from None
        ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True).worksheets[0]
        table = [list(r) for r in ws.iter_rows(values_only=True)]
    else:
        table = list(csv.reader(io.StringIO(data.decode("utf-8-sig", "replace"))))
    key = lambda c: str(c if c is not None else "").strip().lower().replace(" ", "_")  # noqa: E731
    head = next((i for i, r in enumerate(table) if {"symbol", "trade_date", "trade_type"} <= {key(c) for c in r}), None)
    if head is None:
        raise ValueError("This does not look like a Zerodha tradebook (no Symbol / Trade Date / Trade Type header).")
    cols = [key(c) for c in table[head]]
    out = []
    for r in table[head + 1:]:
        d = dict(zip(cols, r))
        side = str(d.get("trade_type") or "").strip().lower()
        if not d.get("symbol") or side not in ("buy", "sell") or d.get("quantity") in (None, ""):
            continue
        day = str(d["trade_date"])[:10]
        out.append({
            "day": day, "ts": str(d.get("order_execution_time") or day).replace("T", " ")[:19],
            "symbol": str(d["symbol"]).strip(), "isin": str(d.get("isin") or "").strip(),
            "exchange": str(d.get("exchange") or "").strip(), "side": side,
            "qty": float(d["quantity"]), "price": float(d["price"]),
            "trade_id": norm_id(d.get("trade_id")), "order_id": str(d.get("order_id") or "").strip()})
    return out


def _empty_book() -> dict:
    return {"lots": [], "states": [], "realized": 0.0, "oversold": False, "monthly": {},
            "actions": [], "factors": [], "trades": []}


def replay(trades: list[dict], actions: list[dict]) -> dict[str, dict]:
    """Replay trades and corporate actions in date order, keeping first-in-first-out lots per stock.

    A book holds the remaining lots [units, cost, day], the units and cost after each day something happened
    ("states"), booked profit, money in and out by month, the actions that touched the stock and its split factors.
    """
    trades = [dict(t) for t in sorted(trades, key=lambda t: (t["ts"], t["trade_id"]))]
    books: dict[str, dict] = {}

    def book(s):
        return books.setdefault(s, _empty_book())

    def mark(s, day):
        b = book(s)
        state = (day, sum(lot[0] for lot in b["lots"]), sum(lot[0] * lot[1] for lot in b["lots"]))
        if b["states"] and b["states"][-1][0] == day:
            b["states"][-1] = state
        else:
            b["states"].append(state)

    def cash(s, day, key, amount):
        m = book(s)["monthly"].setdefault(day[:7], {"month": day[:7], "bought": 0.0, "sold": 0.0})
        m[key] += amount

    def buy(s, day, q, p, traded=None):
        if q > 1e-9:
            book(s)["lots"].append([q, p, day])
        cash(s, day, "bought", (q if traded is None else traded) * p)

    def sell(s, day, q, p, traded=None):
        b = book(s)
        if p is not None:
            cash(s, day, "sold", (q if traded is None else traded) * p)
        while q > 1e-9 and b["lots"]:            # oldest units first, as Zerodha does
            lot = b["lots"][0]
            take = min(q, lot[0])
            if p is not None:
                b["realized"] += take * (p - lot[1])
            lot[0] -= take
            q -= take
            if lot[0] <= 1e-9:
                b["lots"].pop(0)
        if q > 1e-9:
            b["oversold"] = True                   # sold more than was ever recorded as bought

    def trim(lots):
        """Drop a fractional share from the newest lots (companies pay cash for fractions)."""
        total = sum(lot[0] for lot in lots)
        extra = total - int(total + 1e-9)
        for lot in reversed(lots):
            if extra <= 1e-9:
                break
            cut = min(extra, lot[0])
            lot[0] -= cut
            extra -= cut
        return [lot for lot in lots if lot[0] > 1e-9]

    def receive(s, lots):
        b = book(s)
        b["lots"] = sorted(b["lots"] + lots, key=lambda lot: lot[2])   # received shares keep their purchase dates

    def act(a):
        s, day, kind = a["symbol"], a["day"], a["type"]
        src = book(s)
        total = sum(lot[0] for lot in src["lots"])
        if kind == "split":
            for lot in src["lots"]:
                lot[0], lot[1] = lot[0] * a["mult"], lot[1] / a["mult"]
            src["factors"].append((day, a["mult"]))
        elif kind == "bonus":
            free = int(total * a["num"] / a["den"] + 1e-9)
            if free:
                src["lots"].append([free, 0.0, day])
            src["factors"].append((day, (a["num"] + a["den"]) / a["den"]))
        elif kind == "merge" and total > 0:      # also a plain rename (ratio 1)
            if a.get("units"):
                k = a["units"] / total
                moved = [[lot[0] * k, lot[1] / k, lot[2]] for lot in src["lots"]]
            else:
                moved = trim([[lot[0] * a["ratio"], lot[1] / a["ratio"], lot[2]] for lot in src["lots"]])
            src["lots"] = []
            receive(a["to"], moved)
        elif kind == "demerge" and total > 0:
            share = a["cost_pct"] / 100
            new = [[lot[0] * a["ratio"], lot[1] * share / a["ratio"], lot[2]] for lot in src["lots"]]
            for lot in src["lots"]:
                lot[1] *= 1 - share
            receive(a["to"], trim(new))
        elif kind == "acquire":
            buy(s, day, a["units"], a["price"])
        elif kind == "dispose":
            sell(s, day, a["units"], a.get("price"))
        for sym in (s, a.get("to")):
            if sym:
                book(sym)["actions"].append({"day": day, "text": a.get("text", kind), "basis": a.get("basis", "")})
                mark(sym, day)

    # buys and sells of one stock on one day cancel first (intraday), as Zerodha treats them
    same_day: dict[tuple, list] = {}
    for t in trades:
        t["left"] = t["qty"]
        same_day.setdefault((t["symbol"], t["day"]), []).append(t)
    for (sym, _), group in same_day.items():
        buys = [t for t in group if t["side"] == "buy"]
        for s in (t for t in group if t["side"] == "sell"):
            for bt in buys:
                k = min(s["left"], bt["left"])
                if k > 1e-9:
                    s["left"] -= k
                    bt["left"] -= k
                    book(sym)["realized"] += k * (s["price"] - bt["price"])

    events = [(a["day"], 0, "", a) for a in sorted(actions, key=lambda a: a["day"])] + \
             [(t["day"], 1, t["ts"], t) for t in trades]
    for day, kind, _, x in sorted(events, key=lambda e: e[:3]):
        if kind == 0:
            act(x)
            continue
        book(x["symbol"])["trades"].append(x)
        (buy if x["side"] == "buy" else sell)(x["symbol"], day, x["left"], x["price"], traded=x["qty"])
        mark(x["symbol"], day)
    return books


def stock_history(symbol: str, books: dict, held_rows: list[dict], prices: dict, snapshots: dict,
                  today: str, logged_in: bool) -> dict:
    """One stock over time. prices: day -> (close, high, low) from Zerodha's adjusted history; snapshots:
    day -> (last, high, low) seen by the sync; held_rows: today's Zerodha rows for the stock (authority now)."""
    b = books.get(symbol) or _empty_book()
    trades = b["trades"]
    px: dict[str, list] = {}                       # day -> [close, high, low, adjusted]; later sources win
    for t in trades:
        p = px.setdefault(t["day"], [t["price"], t["price"], t["price"], False])
        p[0], p[1], p[2] = t["price"], max(p[1], t["price"]), min(p[2], t["price"])
    for day, (c, h, lo) in snapshots.items():
        if c:
            px[day] = [c, max(h or c, c), min(lo or c, c), False]
    history = bool(prices)
    for day, (c, h, lo) in prices.items():
        px[day] = [c, h or c, lo or c, True]
    for h in held_rows:
        c = h["last_price"]
        p = px.get(today, [c, c, c, False])
        px[today] = [c, max(p[1], c), min(p[2], c), False]

    def unadjust(day):                            # turn an adjusted price back into what it was on the day
        f = 1.0
        for when, mult in b["factors"]:
            if when > day:
                f *= mult
        return f

    states, series, i, qty, invested, last = b["states"], [], 0, 0.0, 0.0, None
    first = states[0][0] if states else None
    for day in sorted(set(px) | {s[0] for s in states}):
        while i < len(states) and states[i][0] <= day:
            _, qty, invested = states[i]
            i += 1
        if first is None or day < first:
            continue
        if day in px:
            c, h, lo, adj = px[day]
            f = unadjust(day) if adj else 1.0
            last = c * f
            c, h, lo = c * f, h * f, lo * f
        elif last is not None:
            c = h = lo = last
        else:
            continue
        series.append({"day": day, "qty": qty, "invested": round(invested, 2), "value": round(qty * c, 2),
                       "value_high": round(qty * h, 2), "value_low": round(qty * lo, 2),
                       "pnl": round(qty * c - invested, 2), "pnl_high": round(qty * h - invested, 2),
                       "pnl_low": round(qty * lo - invested, 2)})

    book_qty = sum(lot[0] for lot in b["lots"])
    book_cost = sum(lot[0] * lot[1] for lot in b["lots"])
    summary: dict = {}
    if states:
        value = series[-1]["value"] if series else 0.0
        summary = {"qty": book_qty, "invested": round(book_cost, 2), "value": value, "pnl": round(value - book_cost, 2),
                   "pnl_pct": (value - book_cost) / book_cost * 100 if book_cost else 0,
                   "avg_cost": book_cost / book_qty if book_qty else 0, "realized": round(b["realized"], 2),
                   "first_day": first, "last_price": last}
    holding_qty = sum(h["qty"] for h in held_rows) if (held_rows or logged_in) else None
    if held_rows:                                 # Zerodha's figures are the authority for what is held now
        h = held_rows[0]
        summary.update(qty=h["qty"], invested=round(h["invested"], 2), value=round(h["value"], 2),
                       pnl=round(h["pnl"], 2), pnl_pct=h["pnl_pct"], avg_cost=h["avg_price"],
                       last_price=h["last_price"], realized=round(b["realized"], 2), first_day=first)
    adds_up = not b["oversold"] and (holding_qty is None or abs(holding_qty - book_qty) < 0.5)
    cost_gap = None
    if held_rows and adds_up and held_rows[0]["invested"]:
        cost_gap = round(book_cost - held_rows[0]["invested"], 2)
    if summary and not adds_up:
        summary["realized"] = None                # booked profit cannot be trusted when units do not add up
    return {
        "symbol": symbol, "summary": summary, "series": series,
        "monthly": [b["monthly"][k] for k in sorted(b["monthly"])],
        "recent": [{k: t[k] for k in ("day", "side", "qty", "price")} for t in trades[::-1][:200]],
        "trade_count": len(trades), "first_trade": trades[0]["day"] if trades else None,
        "last_trade": trades[-1]["day"] if trades else None,
        "actions": b["actions"][::-1],
        "recon": {"holding_qty": holding_qty, "trade_qty": book_qty, "ok": adds_up, "cost_gap": cost_gap},
        "price_source": ("High, low and close are Zerodha's daily prices, applied to the units held at the end of each day."
                         if history else
                         "High, low and close come from your own trades and the sync's tracking, so they are rough and "
                         "days without a trade are flat."),
    }
