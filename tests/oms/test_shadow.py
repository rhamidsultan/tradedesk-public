"""ShadowBroker: the full OrderManager on real-looking prices with simulated fills; nothing is ever sent."""
from __future__ import annotations

from tradedesk_oms import LegReq, Limits, OrderManager, Quote
from tradedesk_oms.shadow import ShadowBroker

from test_oms import CE, PE, Clock


class Prices:
    def __init__(self):
        self.q = {CE: Quote(39.5, 40.0, 40.0), PE: Quote(39.5, 40.0, 40.0)}
        self.calls = 0

    def __call__(self, syms, ex="NFO"):
        self.calls += 1
        return {s: self.q[s] for s in syms if s in self.q}


def _om(tmp_path, px, c):
    b = ShadowBroker(px, tmp_path / "shadow.json")
    om = OrderManager(b, tmp_path / "oms.db", Limits(max_lots=2, max_orders_day=20, daily_loss=8000), clock=c, sleep=c.sleep)
    assert om.start()
    om.armed = True
    return b, om


def test_short_strangle_fills_at_the_bid_and_stop_fills_when_crossed(tmp_path):
    px, c = Prices(), Clock()
    b, om = _om(tmp_path, px, c)
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 39.5, 60.0), LegReq(PE, "NFO", "SELL", 1, 65, 39.5, 60.0)])
    for _ in range(3):
        om.poll()
    assert b.positions() == {CE: -65, PE: -65}
    legs = {l["symbol"]: l for l in om.snapshot()["legs"]}
    assert legs[CE]["status"] == "open" and legs[CE]["entry_avg"] == 39.5
    # the call spikes through its stop: the SL triggers and fills at the ask (inside its limit 66)
    px.q[CE] = Quote(61.0, 61.5, 61.5)
    om.poll(); om.poll()
    legs = {l["symbol"]: l for l in om.snapshot()["legs"]}
    assert legs[CE]["status"] == "closed" and legs[CE]["reason"] == "stop_loss" and legs[CE]["exit_avg"] == 61.5
    assert b.positions() == {PE: -65} and om.ready


def test_book_survives_a_restart(tmp_path):
    px, c = Prices(), Clock()
    b, om = _om(tmp_path, px, c)
    om.enter("s1", [LegReq(CE, "NFO", "SELL", 1, 65, 39.5, 60.0)])
    om.poll(); om.poll()
    b2 = ShadowBroker(px, tmp_path / "shadow.json")
    om2 = OrderManager(b2, tmp_path / "oms.db", Limits(max_lots=2, max_orders_day=20, daily_loss=8000), clock=c, sleep=c.sleep)
    assert om2.start()                                           # reconciles against the saved shadow position
    assert b2.positions() == {CE: -65}
    assert om2.kill("k1")
    for _ in range(3):
        om2.poll()
    assert b2.positions() == {} and om2.locked()


def test_shadow_never_touches_a_real_order_api(tmp_path):
    import inspect
    from tradedesk_oms import shadow
    src = inspect.getsource(shadow)
    assert "urllib" not in src and "ZerodhaAdapter" not in src and "requests" not in src
