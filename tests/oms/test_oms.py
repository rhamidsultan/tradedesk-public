"""Order-management incidents against FakeBroker (architecture §6.4): every path ends with a protected or flat book."""
from __future__ import annotations

import pytest

from tradedesk_oms import LegReq, Limits, OrderManager, Quote, Rejected, NetworkUnknown, RateLimited
from tradedesk_oms.broker import FakeBroker
from tradedesk_oms.oms import GuardBlocked

CE, PE = "NIFTY25O2726000CE", "NIFTY25O2724000PE"


class Clock:
    def __init__(self): self.t = 1_761_200_000.0      # a weekday morning
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


@pytest.fixture()
def env(tmp_path):
    b = FakeBroker()
    b.quotes = {CE: Quote(39.5, 40.0, 40.0), PE: Quote(39.5, 40.0, 40.0)}
    c = Clock()
    alerts = []
    om = OrderManager(b, tmp_path / "oms.db", Limits(max_lots=2, max_orders_day=20, daily_loss=8000), alert=lambda *a: alerts.append(a), clock=c, sleep=c.sleep)
    assert om.start()
    om.armed = True
    return b, om, c, alerts


def strangle(lots=1):
    return [LegReq(CE, "NFO", "SELL", lots, 65, 40.0, 60.0), LegReq(PE, "NFO", "SELL", lots, 65, 40.0, 60.0)]


def entry(b, om, sym):
    return b.by_tag(next(l["entry_tag"] for l in om.snapshot()["legs"] if l["symbol"] == sym))


def sl_of(b, om, sym):
    return b.by_tag(next(l["sl_tag"] for l in om.snapshot()["legs"] if l["symbol"] == sym))


def test_happy_path_sequential_legs_and_protective_sl(env):
    b, om, c, _ = env
    om.enter("s1", strangle())
    assert [x[0] for x in b.calls] == ["place"]                      # second leg waits for the first
    b.fill(entry(b, om, CE).order_id, price=40.0); om.poll()
    sl = sl_of(b, om, CE)
    assert (sl.side, sl.order_type, sl.trigger, sl.price, sl.qty) == ("BUY", "SL", 60.0, 66.0, 65)   # limit = trigger + 10%
    b.fill(entry(b, om, PE).order_id); om.poll()
    assert b.positions() == {CE: -65, PE: -65} and om.ready and om.locked() is None
    assert all(l["status"] == "open" for l in om.snapshot()["legs"])


def test_no_market_or_slm_orders_exist():
    from tradedesk_oms.broker import OrderReq, InputInvalid
    for t in ("MARKET", "SL-M"):
        with pytest.raises(InputInvalid):
            OrderReq("TD1", CE, "NFO", "BUY", 65, t, 10.0, 9.0)


def test_guards_block_opening_orders(env):
    b, om, c, _ = env
    om.armed = False
    with pytest.raises(GuardBlocked, match="not armed"):
        om.enter("s1", strangle())
    om.armed = True
    with pytest.raises(GuardBlocked, match="max 2"):
        om.enter("s1", strangle(lots=3))
    om.L.daily_loss = 5000
    with pytest.raises(GuardBlocked, match="worst case"):               # 2 legs × 2 lots × (66−40) × 65 = 6,760 > 5,000
        om.enter("s1", [LegReq(CE, "NFO", "SELL", 2, 65, 40.0, 60.0), LegReq(PE, "NFO", "SELL", 2, 65, 40.0, 60.0)])
    with pytest.raises(GuardBlocked, match="above a short"):
        om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 30.0)])
    assert b.calls == []


def test_reject_on_second_leg_squares_off_first_and_locks(env):
    b, om, c, alerts = env
    om.enter("s1", strangle())
    b.fill(entry(b, om, CE).order_id); om.poll()
    b.reject(entry(b, om, PE).order_id); om.poll()
    sl = sl_of(b, om, CE)
    assert sl.order_type == "LIMIT" and sl.price == 40.0                 # the SL order became the exit (no second order)
    assert sum(1 for x in b.calls if x[0] == "place") == 3              # CE entry, CE SL, PE entry — no extra exit order
    b.fill(sl.order_id); om.poll()
    assert b.positions() == {} and om.locked("s1")
    with pytest.raises(GuardBlocked, match="locked"):
        om.enter("s1", strangle())
    assert any(a[0] == "critical" for a in alerts)


def test_partial_fill_sizes_sl_then_squares_off(env):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 2, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id, qty=65); om.poll()
    c.t += 31; om.poll()                                                 # entry timeout: cancel rest, SL for 65, square off
    e = entry(b, om, CE)
    assert e.status == "CANCELLED"
    sl = sl_of(b, om, CE)
    assert sl.qty == 65 and sl.order_type == "LIMIT"
    b.fill(sl.order_id); om.poll()
    assert b.positions() == {} and om.locked("s1")


def test_sl_triggered_not_filled_is_chased(env):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    sl = sl_of(b, om, CE)
    b.quotes[CE] = Quote(70.0, 71.0, 70.5)
    b.trigger(sl.order_id); om.poll()                                    # first sight: start the clock
    c.t += 5; om.poll()
    assert b.book[sl.order_id].price == 71.0                             # raised to the ask
    for _ in range(6):
        c.t += 5; om.poll()
    assert b.book[sl.order_id].price == tick_up(71.0 * 1.02)             # after 6 tries: ask + 2%
    b.fill(sl.order_id); om.poll()
    leg = om.snapshot()["legs"][0]
    assert leg["status"] == "closed" and leg["reason"] == "stop_loss"


def tick_up(x):
    from tradedesk_oms.oms import tick
    return tick(x, True)


def test_stop_beyond_daily_cap_closes_everything(env):
    b, om, c, _ = env
    om.L.daily_loss = 2000                                               # worst case at SL limit 1,950 fits
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 50.0), LegReq(PE, "NFO", "SELL", 1, 65, 40.0, 50.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    b.fill(entry(b, om, PE).order_id); om.poll()
    b.fill(sl_of(b, om, CE).order_id, price=72.0); om.poll()           # filled beyond the limit after chasing: −2,080
    assert om.locked() and "daily loss" in om.locked()
    pe_sl = sl_of(b, om, PE)
    assert pe_sl.order_type == "LIMIT"
    b.fill(pe_sl.order_id); om.poll()
    assert b.positions() == {}


def test_kill_switch_is_idempotent(env):
    b, om, c, _ = env
    om.enter("s1", strangle())
    b.fill(entry(b, om, CE).order_id); om.poll()
    assert om.kill("cmd-1")
    n = len(b.calls)
    assert not om.kill("cmd-1") and len(b.calls) == n
    assert entry(b, om, PE).status == "CANCELLED" and sl_of(b, om, CE).order_type == "LIMIT"
    b.fill(sl_of(b, om, CE).order_id); om.poll()
    assert b.positions() == {} and not om.armed and om.locked()


def test_lost_reply_is_resolved_by_tag_not_resent(env):
    b, om, c, _ = env
    b.lost_next = 1
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    assert sum(1 for x in b.calls if x[0] == "place") == 1
    c.t += 2; om.poll()
    leg = om.snapshot()["legs"][0]
    it = om._intent(leg["entry_tag"])
    assert it["status"] == "SENT" and it["order_id"]                     # adopted from the order book
    assert sum(1 for x in b.calls if x[0] == "place") == 1


def test_restart_resolves_sending_and_reconciles(env, tmp_path):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    om2 = OrderManager(b, tmp_path / "oms.db", Limits(max_lots=2, max_orders_day=20, daily_loss=8000), clock=c, sleep=c.sleep)
    assert om2.start() and om2.expected_positions() == {CE: -65}


def test_manual_trade_mismatch_resizes_sl_and_locks(env):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    b.pos[CE] = 0                                                        # owner closed it by hand in Kite
    assert not om.reconcile()
    assert sl_of(b, om, CE).status == "CANCELLED"                        # SL resized to 0: cannot open a new long
    assert "mismatch" in om.locked() and not om.ready
    with pytest.raises(GuardBlocked):
        om.enter("s2", [LegReq(PE, "NFO", "SELL", 1, 65, 40.0, 60.0)])


def test_sl_rejected_exits_leg(env):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.fail_next = [Rejected("RMS")]
    b.fill(entry(b, om, CE).order_id); om.poll()
    exits = [x for x in b.calls if x[0] == "place" and x[4] == "LIMIT" and x[2] == "BUY"]
    assert exits and exits[-1][5] == 40.0                                # exit at the ask straight away


def test_rate_limit_retried_not_dropped(env):
    b, om, c, _ = env
    b.fail_next = [RateLimited("429"), RateLimited("429")]
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    assert len(b.book) == 1


def test_daily_loss_from_mtm(env):
    b, om, c, _ = env
    om.enter("s1", strangle())
    b.fill(entry(b, om, CE).order_id); om.poll()
    b.fill(entry(b, om, PE).order_id); om.poll()
    om.check_mtm(-8100)
    assert all(b.by_tag(l["sl_tag"]).order_type == "LIMIT" for l in om.snapshot()["legs"])
    assert "daily loss" in om.locked()


def test_zerodha_adapter_maps_requests_and_errors(monkeypatch):
    import io, json as _j, urllib.error
    from tradedesk_oms import zerodha as z
    from tradedesk_oms.broker import OrderReq, TokenExpired, MarginInsufficient
    seen = []
    def fake(req, timeout):
        seen.append((req.get_method(), req.full_url, req.data.decode() if req.data else None))
        if "fail403" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 403, "x", {}, io.BytesIO(b'{"error_type":"TokenException","message":"expired"}'))
        if req.get_method() == "POST" and "MARGIN" in (req.data or b"").decode():
            raise urllib.error.HTTPError(req.full_url, 400, "x", {}, io.BytesIO(b'{"error_type":"MarginException","message":"no margin"}'))
        if req.get_method() == "POST":
            raise TimeoutError()
        return io.BytesIO(_j.dumps({"data": []}).encode())
    monkeypatch.setattr(z.urllib.request, "urlopen", fake)
    a = z.ZerodhaAdapter("k", "secret-token")
    assert "secret" not in repr(a)
    with pytest.raises(NetworkUnknown):                                   # lost reply on POST = maybe placed
        a.place(OrderReq("TD1", "NIFTY25O2726000CE", "NFO", "BUY", 65, "SL", 66.0, 60.0))
    m, url, body = seen[-1]
    assert m == "POST" and url.endswith("/orders/regular") and "order_type=SL" in body and "tag=TD1" in body and "trigger_price=60.00" in body
    with pytest.raises(MarginInsufficient):
        a.place(OrderReq("TD2", "MARGIN", "NFO", "SELL", 65, "LIMIT", 40.0))
    a._req.__func__  # noqa
    monkeypatch.setattr(z, "ROOT", "https://api.kite.trade/fail403")
    with pytest.raises(TokenExpired):
        a.orders()


# ---------------------------------------------------------------- review 23 Oct 2026 (P0/P1 regressions)
def two_open(b, om):
    om.enter("s1", strangle())
    b.fill(entry(b, om, CE).order_id); om.poll()
    b.fill(entry(b, om, PE).order_id); om.poll()


def test_p0_1_kill_survives_broker_error_and_repeat_kill_redrives(env):
    from tradedesk_oms.broker import BrokerDown
    b, om, c, _ = env
    two_open(b, om)
    b.fail_next = [BrokerDown("down")]                                   # first modify fails
    om.kill("k1")
    legs = om.snapshot()["legs"]
    assert all(l["status"] == "exiting" for l in legs)                   # both marked, second still converted
    assert sl_of(b, om, PE).order_type == "LIMIT"
    om.kill("k1")                                                        # repeat re-drives the failed one
    assert sl_of(b, om, CE).order_type == "LIMIT"
    b.fill(sl_of(b, om, CE).order_id); b.fill(sl_of(b, om, PE).order_id); om.poll()
    assert b.positions() == {}


def test_p0_2_partial_exit_then_cancel_never_flips(env):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    om.exit_leg(om.snapshot()["legs"][0]["id"], "manual")
    ex = sl_of(b, om, CE)
    b.fill(ex.order_id, qty=40); b.book[ex.order_id].status = "CANCELLED"; om.poll()
    new = [x for x in b.calls if x[0] == "place"][-1]
    assert new[2] == "BUY" and new[3] == 25                              # only the 25 still open
    b.fill(b.by_tag(new[1]).order_id); om.poll()
    assert b.positions() == {} and om.snapshot()["legs"][0]["status"] == "closed"


def test_p0_3_kill_during_partial_entry_closes_the_filled_part(env):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 2, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id, qty=30); om.poll()
    assert sl_of(b, om, CE).qty == 30                                    # P1-1: SL for the partial at once
    om.kill("k1")
    assert entry(b, om, CE).status == "CANCELLED" and sl_of(b, om, CE).order_type == "LIMIT"
    b.fill(sl_of(b, om, CE).order_id); om.poll()
    assert b.positions() == {}


def test_p0_4_modify_refused_falls_back_to_cancel_then_limit(env):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    b.fail_next = [Rejected("order type change not allowed")]
    om.exit_leg(om.snapshot()["legs"][0]["id"], "manual")
    assert sl_of(b, om, CE).status == "CANCELLED"
    om.poll()
    ex = [x for x in b.calls if x[0] == "place"][-1]
    assert ex[4] == "LIMIT" and ex[2] == "BUY" and ex[3] == 65
    b.fill(b.by_tag(ex[1]).order_id); om.poll()
    assert b.positions() == {}


def test_p0_5_overnight_leg_gets_fresh_sl_next_morning(env, tmp_path):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    old = sl_of(b, om, CE)
    del b.book[old.order_id]                                             # DAY order expired; new day's book is empty
    c.t += 22 * 3600
    om2 = OrderManager(b, tmp_path / "oms.db", Limits(max_lots=2, max_orders_day=20, daily_loss=8000), clock=c, sleep=c.sleep)
    assert om2.start()
    fresh = sl_of(b, om2, CE)
    assert fresh.order_id != old.order_id and fresh.status == "TRIGGER PENDING" and fresh.qty == 65


def test_p1_2_late_entry_is_adopted_protected_and_closed(env):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    leg = om.snapshot()["legs"][0]
    hidden = b.book.pop(entry(b, om, CE).order_id)                       # broker book lags: order not visible
    om.db.execute("UPDATE intents SET status='SENDING', order_id=NULL WHERE tag=?", (leg["entry_tag"],))
    c.t += 31; om.poll()
    assert om._intent(leg["entry_tag"])["status"] == "FAILED"
    b.book[hidden.order_id] = hidden; b.fill(hidden.order_id); om.poll()
    assert sl_of(b, om, CE).order_type == "LIMIT"                        # protected and being closed
    b.fill(sl_of(b, om, CE).order_id); om.poll()
    assert b.positions() == {}


def test_p1_3_daily_loss_works_when_already_locked(env):
    b, om, c, _ = env
    two_open(b, om)
    om.lock("account", "something else")
    om.check_mtm(-9000)
    assert all(l["status"] == "exiting" for l in om.snapshot()["legs"])


def test_p1_4_entry_window_and_explicit_limits(env, tmp_path):
    b, om, c, _ = env
    c.t += 5 * 3600                                                      # 16:43 IST
    with pytest.raises(GuardBlocked, match="entry window"):
        om.enter("s1", strangle())
    with pytest.raises(TypeError):
        Limits()


def test_p1_5_bfo_quotes_use_bfo(env):
    b, om, c, _ = env
    sx = "SENSEX25O3085000CE"
    b.quotes[sx] = Quote(99.0, 100.0, 100.0)
    om.enter("s1", [LegReq(sx, "BFO", "SELL", 1, 20, 100.0, 150.0)])
    b.fill(entry(b, om, sx).order_id); om.poll()
    om.exit_leg(om.snapshot()["legs"][0]["id"], "manual")
    assert ("quote", "BFO") in b.calls


def test_p1_6_chaser_cancels_and_replaces_before_modify_cap(env):
    b, om, c, _ = env
    om.L.max_mods = 3
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    sl = sl_of(b, om, CE)
    b.trigger(sl.order_id); om.poll()
    for i in range(6):
        b.quotes[CE] = Quote(70 + i, 71 + i, 70 + i)
        c.t += 5; om.poll()
    assert b.book[sl.order_id].status == "CANCELLED"
    leg = om.snapshot()["legs"][0]
    assert leg["status"] == "exiting" and leg["exit_tag"] and leg["exit_tag"] != leg["sl_tag"]
    b.fill(b.by_tag(leg["exit_tag"]).order_id); om.poll()
    assert b.positions() == {} and om.snapshot()["legs"][0]["reason"] == "stop_loss"


def test_p1a_cap_cancel_failure_sends_no_second_order(env):
    from tradedesk_oms.broker import BrokerDown
    b, om, c, _ = env
    om.L.max_mods = 1
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    sl = sl_of(b, om, CE)
    b.trigger(sl.order_id); om.poll()
    b.quotes[CE] = Quote(70, 71, 70); c.t += 5; om.poll()                # one modify -> cap reached
    places = sum(1 for x in b.calls if x[0] == "place")
    b.fail_next = [BrokerDown("x")]                                      # the cancel fails
    b.quotes[CE] = Quote(72, 73, 72); c.t += 5; om.poll()
    assert b.book[sl.order_id].status == "OPEN"
    assert sum(1 for x in b.calls if x[0] == "place") == places          # no second closing order while it works
    c.t += 5; om.poll()                                                  # retry cancels, then replaces
    assert b.book[sl.order_id].status == "CANCELLED"


def test_p1b_late_old_sl_is_cancelled_as_orphan(env):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.lost_next = 0
    b.fill(entry(b, om, CE).order_id); om.poll()
    leg = om.snapshot()["legs"][0]
    first = sl_of(b, om, CE)
    hidden = b.book.pop(first.order_id)                                  # SL reply lost and not yet visible
    om.db.execute("UPDATE intents SET status='SENDING', order_id=NULL WHERE tag=?", (leg["sl_tag"],))
    c.t += 31; om.poll(); om.poll()                                      # marked failed -> leg exits / re-protects
    b.book[hidden.order_id] = hidden; om.poll()                          # the old SL shows up late
    assert b.book[hidden.order_id].status == "CANCELLED"


def test_trailing_moves_broker_stop_only_tighter_and_replaces_at_cap(env):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "BUY", 1, 65, 40.0, 30.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    sl = sl_of(b, om, CE)
    assert (sl.side, sl.trigger) == ("SELL", 30.0)
    assert om.move_stop(om._legs()[0]["id"], 35.0) and b.book[sl.order_id].trigger == 35.0 and b.book[sl.order_id].price == 31.5
    assert not om.move_stop(om._legs()[0]["id"], 33.0)                   # never loosens
    om.L.max_mods = 1
    assert om.move_stop(om._legs()[0]["id"], 38.0)                       # at the cap: cancel, then a fresh SL
    assert b.book[sl.order_id].status == "CANCELLED" and sl_of(b, om, CE).trigger == 38.0
    assert b.positions() == {CE: 65} and om.reconcile()


def test_trailing_short_leg_rounding_crossed_and_partial_fill_race(env):
    b, om, c, _ = env
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    lid = om._legs()[0]["id"]
    b.quotes[CE] = Quote(29.5, 30.0, 30.0)
    assert om.move_stop(lid, 45.03) and sl_of(b, om, CE).trigger == 45.0       # buy-SL rounds down (tighter)
    b.quotes[CE] = Quote(44.0, 46.0, 45.0)
    assert om.move_stop(lid, 44.0)                                              # through the ask: exit instead
    assert om._legs()[0]["reason"] == "trail_crossed" and sl_of(b, om, CE).order_type == "LIMIT"


def test_trailing_cap_replace_when_sl_partly_filled_closes_leg(env):
    b, om, c, _ = env
    om.L.max_mods = 0
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 60.0)])
    b.fill(entry(b, om, CE).order_id); om.poll()
    sl = sl_of(b, om, CE)
    b.quotes[CE] = Quote(29.5, 30.0, 30.0)
    real_cancel = b.cancel
    def cancel_after_partial(oid):
        b.fill(oid, qty=20); real_cancel(oid)                                    # fills 20 just before the cancel lands
    b.cancel = cancel_after_partial
    assert om.move_stop(om._legs()[0]["id"], 50.0)
    leg = om._legs()[0]
    assert leg["status"] == "exiting" and leg["reason"] == "stop_loss"
    ex = b.by_tag(leg["exit_tag"])
    assert ex.qty == 45 and ex.side == "BUY"                                      # only the rest, never a fresh SL


def test_hedge_leg_without_stop_only_next_to_shorts(env):
    b, om, c, _ = env
    WING = "NIFTY25O2726500CE"
    b.quotes[WING] = Quote(9.5, 10.0, 10.0)
    with pytest.raises(GuardBlocked, match="every leg needs a stop"):
        om.enter("s0", [LegReq(WING, "NFO", "BUY", 1, 65, 10.0, None)])
    om.enter("s1", [LegReq(WING, "NFO", "BUY", 1, 65, 10.0, None), LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 80.0)])
    b.fill(entry(b, om, WING).order_id); om.poll()
    b.fill(entry(b, om, CE).order_id); om.poll()
    legs = om._legs()
    assert legs[0]["sl_tag"] is None and legs[0]["status"] == "open"     # no SL on the wing, not exited as 'missing'
    assert sl_of(b, om, CE).trigger == 80.0 and om.reconcile()


def _fly(b, om):
    WING = "NIFTY25O2726500CE"
    b.quotes[WING] = Quote(9.5, 10.0, 10.0)
    om.enter("s1", [LegReq(WING, "NFO", "BUY", 1, 65, 10.0, None), LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 80.0)])
    b.fill(entry(b, om, WING).order_id); om.poll()
    b.fill(entry(b, om, CE).order_id); om.poll()
    return WING


def test_kill_buys_back_shorts_before_selling_wing(env):
    b, om, c, _ = env
    WING = _fly(b, om)
    n = len(b.calls)
    om.kill("k1")
    acts = [x for x in b.calls[n:] if x[0] in ("place", "modify")]
    assert acts and acts[0][0] == "modify" and acts[0][4] == "LIMIT"          # the short's SL becomes its exit first
    assert not [x for x in b.calls[n:] if x[0] == "place" and x[2] == "SELL"]  # wing not sold while the short is open
    b.fill(sl_of(b, om, CE).order_id); om.poll(); om.poll()
    wing_exit = [x for x in b.calls if x[0] == "place" and x[2] == "SELL"]
    assert wing_exit                                                          # released after the short closed
    b.fill(b.by_tag(wing_exit[-1][1]).order_id); om.poll()
    assert b.positions() == {}


def test_wing_exit_alone_closes_whole_position_and_hedge_released_after_stop(env):
    b, om, c, _ = env
    WING = _fly(b, om)
    wing_leg = om._legs()[0]["id"]
    om.exit_leg(wing_leg, "manual")
    assert sl_of(b, om, CE).order_type == "LIMIT"                            # shorts first
    assert om._legs()[0]["status"] == "exiting" and not om._legs()[0]["exit_tag"]


def test_wing_sold_when_its_short_is_stopped(env):
    b, om, c, _ = env
    WING = _fly(b, om)
    b.fill(sl_of(b, om, CE).order_id); om.poll(); om.poll()
    assert om._legs()[0]["reason"] == "hedge_unneeded"


def test_hedge_must_match_short_type_and_size(env):
    b, om, c, _ = env
    PEW = "NIFTY25O2723500PE"
    b.quotes[PEW] = Quote(9.5, 10.0, 10.0)
    with pytest.raises(GuardBlocked, match="hedge"):
        om.enter("s1", [LegReq(PEW, "NFO", "BUY", 1, 65, 10.0, None), LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 80.0)])


def test_wing_not_sold_while_short_still_entering(env):
    b, om, c, _ = env
    WING = "NIFTY25O2726500CE"
    b.quotes[WING] = Quote(9.5, 10.0, 10.0)
    om.enter("s1", [LegReq(WING, "NFO", "BUY", 1, 65, 10.0, None), LegReq(CE, "NFO", "SELL", 1, 65, 40.0, 80.0)])
    b.fill(entry(b, om, WING).order_id); om.poll(); om.poll(); om.poll()      # short sent, not filled yet
    assert om._legs()[0]["status"] == "open"
    assert not [x for x in b.calls if x[0] == "place" and x[2] == "SELL" and x[1] != entry(b, om, CE).tag]
