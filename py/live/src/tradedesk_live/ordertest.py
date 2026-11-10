"""td-ordertest: the supervised 1-lot order test (run on the server, during market hours, owner watching).

Step A (no fill possible): BUY LIMIT 1 lot of a far-OTM NIFTY option at ₹0.05 → modify → cancel.
        Proves the static IP, the API key's order permission and our tag handling.
Step B (real, small): BUY 1 lot of a cheap OTM option at the ask → protective SL (SELL SL, trigger −40%) →
        convert the SL into a LIMIT exit at the bid (the architecture's 'SL becomes the exit') → confirm flat.
        Cost: spread + charges (typically ₹50–150).
On success it creates $TD_HOME/live/ENABLED, which allows arming live sessions.
"""
from __future__ import annotations

import sys
import time
from datetime import date

from tradedesk_oms import LegReq, Limits, OrderManager
from tradedesk_oms.broker import OrderReq
from tradedesk_oms.zerodha import ZerodhaAdapter

from . import kite
from .live import live_dir


def ask(q: str) -> bool:
    return input(q + " Type YES to continue: ").strip() == "YES"


def wait_closed(om, leg, secs=120):
    """Keep managing until the leg is closed/failed AND the broker book is flat (or time runs out)."""
    for _ in range(secs):
        om.poll(); time.sleep(1)
        if om._leg(leg)["status"] in ("closed", "failed") and not om.b.positions():
            return True
    return False


def main():
    import json as _json
    snap = live_dir() / "oms.json"
    try:
        if time.time() - float(_json.loads(snap.read_text()).get("at") or 0) < 30:
            sys.exit("The live runner is active (td-paper). Stop it first: sudo systemctl stop tradedesk-paper")
    except (OSError, ValueError):
        pass
    key, token = kite.credentials()
    if not (key and token):
        sys.exit("No Kite login found. Log in on the portfolio dashboard first.")
    b = ZerodhaAdapter(key, token)
    spot = kite.get("/quote/ltp", key, token, [("i", "NSE:NIFTY 50")])["data"]["NSE:NIFTY 50"]["last_price"]
    ins = [i for i in kite.parse_instruments(kite.get("/instruments/NFO", key, token, raw=True), "NIFTY") if i.expiry >= date.today()]
    exp = min(i.expiry for i in ins)
    far = min((i for i in ins if i.expiry == exp and i.opt_type == "CE" and i.strike >= spot * 1.04), key=lambda i: i.strike)
    print(f"NIFTY {spot}. Step A uses {far.kite_symbol} (lot {far.lot_size}) at ₹0.05 — it cannot fill.")
    if not ask("Step A: place, modify and cancel a non-filling order?"):
        return
    tag = f"TDTEST{int(time.time()) % 10**8}"
    oid = b.place(OrderReq(tag, far.kite_symbol, "NFO", "BUY", far.lot_size, "LIMIT", 0.05))
    print("placed", oid); time.sleep(1)
    b.modify(oid, price=0.10); print("modified"); time.sleep(1)
    b.cancel(oid); time.sleep(1)
    st = next(o for o in b.orders() if o.order_id == oid)
    print("final status:", st.status, "tag:", st.tag)
    if st.status != "CANCELLED" or st.tag != tag:
        sys.exit("Step A did not end CANCELLED with our tag — stop and send me this output.")
    print("Step A passed: orders from this server work.\n")
    cheap = [i for i in ins if i.expiry == exp and i.opt_type == "CE" and i.strike >= spot * 1.01]
    qs = b.quote([i.kite_symbol for i in cheap[:15]])
    pick = next((i for i in sorted(cheap[:15], key=lambda i: i.strike) if qs.get(i.kite_symbol) and qs[i.kite_symbol].ask and 3 <= qs[i.kite_symbol].ask <= 15), None)
    if not pick:
        sys.exit("No option between ₹3 and ₹15 found right now; run again later.")
    q = qs[pick.kite_symbol]
    print(f"Step B: BUY 1 lot {pick.kite_symbol} × {pick.lot_size} at about ₹{q.ask} (≈ ₹{q.ask * pick.lot_size:,.0f}), SL at Kite, then exit.")
    if not ask("Step B places REAL orders and costs the spread plus charges."):
        return
    om = OrderManager(b, live_dir() / "ordertest.db", Limits(max_lots=1, max_orders_day=2, daily_loss=2000, entry_window=("09:16", "15:20")),
                      log=print, alert=lambda *a: print("ALERT", a))
    if not om.start():
        sys.exit("Account not flat or orders pending — test needs a flat F&O book. Stopping.")
    om.armed = True
    leg = om.enter("ordertest", [LegReq(pick.kite_symbol, "NFO", "BUY", 1, pick.lot_size, q.ask, round(q.ask * 0.6, 1))])[0]
    for _ in range(30):
        om.poll(); time.sleep(1)
        l = om._leg(leg)
        if l["status"] == "open" and om._working(l["sl_tag"]):
            break
    l = om._leg(leg)
    print("leg:", l["status"], "filled", l["filled"], "@", l["entry_avg"], "SL order:", om._working(l["sl_tag"]))
    if l["status"] != "open":
        print("Entry did not fill with an SL in 30 s; cancelling / squaring off and waiting until flat…")
        om.exit_leg(leg, "ordertest_abort")
        flat = wait_closed(om, leg)
        sys.exit(f"Test stopped. Leg {om._leg(leg)['status']}, broker flat: {flat}. "
                 + ("" if flat else "CHECK KITE NOW and close manually. ") + "Send me this output.")
    time.sleep(3)
    om.exit_leg(leg, "ordertest")
    wait_closed(om, leg)
    l = om._leg(leg)
    conv = om._intent(l["exit_tag"])["kind"] == "sl" if l["exit_tag"] else False
    print("leg:", l["status"], "exit @", l["exit_avg"], "| SL converted into the exit:", "yes" if conv else "no (fallback cancel+limit used)")
    flat = not b.positions()
    print("broker F&O positions flat:", flat)
    if l["status"] == "closed" and flat:
        (live_dir() / "ENABLED").write_text(time.strftime("%Y-%m-%d %H:%M") + f" ordertest passed; SL→LIMIT modify {'works' if conv else 'refused, fallback used'}\n")
        print("\nPASSED. Live is now enabled on this server. Start the runner again: sudo systemctl start tradedesk-paper")
    else:
        sys.exit("Not flat — check Kite NOW and close manually if needed. Send me this output.")
