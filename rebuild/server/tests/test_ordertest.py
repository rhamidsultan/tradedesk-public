"""The supervised 1-lot order test, against a fake Zerodha that fills limit orders at once."""
from datetime import datetime

import pytest
from test_kite import _login
from test_live import KCE, LIMITS, live  # noqa: F401  (fixture)

from tdserver.clock import IST

pytest.importorskip("tradedesk_oms")
from tradedesk_oms import Quote  # noqa: E402
from tradedesk_oms.broker import FakeBroker  # noqa: E402


class AutoFill(FakeBroker):
    def orders(self):
        for o in list(self.book.values()):
            if o.order_type == "LIMIT" and o.status == "OPEN" and o.price > 0.2:
                self.fill(o.order_id)
        return super().orders()


def test_order_test_passes_and_prints_the_switch_command(client, owner, live, clock, net):  # noqa: F811
    from tdserver.live.ordertest import run
    _login(client, owner, net)
    clock.at = datetime(2026, 11, 3, 10, 0, tzinfo=IST)
    b = AutoFill()
    b.quotes = {"NIFTY 50": Quote(None, None, 25000.0), KCE: Quote(9.5, 10.0, 10.0),
                "NIFTY25N1024000PE": Quote(9.5, 10.0, 10.0)}
    lines = []
    rc = run(live, adapter=b, ask=lambda q: True, out=lines.append, sleep=lambda s: None)
    text = "\n".join(lines)
    assert rc == 0, text
    assert "Step A passed" in text and "PASSED" in text and "sudo" in text
    assert not b.positions()
    kinds = [c[0] for c in b.calls if c[0] != "quote"]
    assert kinds[:3] == ["place", "modify", "cancel"]                # step A: never fills


def test_order_test_refuses_while_the_runner_runs(client, owner, live, clock, net):  # noqa: F811
    from tdserver.live.ordertest import run
    from tdserver.models import ServiceStatus
    with live.sessions() as db:
        db.add(ServiceStatus(name="live-runner", updated_at=live.clock.now(), data={}))
        db.commit()
    lines = []
    assert run(live, adapter=FakeBroker(), ask=lambda q: True, out=lines.append, sleep=lambda s: None) == 2
    assert "td2-live" in lines[1]


def _ready(client, owner, live, clock, net):  # noqa: F811
    _login(client, owner, net)
    clock.at = datetime(2026, 11, 3, 10, 0, tzinfo=IST)


def test_step_a_fill_is_never_a_pass(client, owner, live, clock, net):  # noqa: F811
    from tdserver.live.ordertest import run
    _ready(client, owner, live, clock, net)

    class FillsEverything(AutoFill):
        def place(self, req):
            oid = super().place(req)
            if req.price <= 0.1:
                self.fill(oid, qty=1, price=req.price)              # a partial fill at ₹0.05
            return oid
    b = FillsEverything()
    b.quotes = {"NIFTY 50": Quote(None, None, 25000.0), KCE: Quote(9.5, 10.0, 10.0),
                "NIFTY25N1024000PE": Quote(9.5, 10.0, 10.0)}
    lines = []
    assert run(live, adapter=b, ask=lambda q: True, out=lines.append, sleep=lambda s: None) == 1
    text = "\n".join(lines)
    assert "did not end with the order modified" in text and "PASSED" not in text


def test_broker_error_in_step_b_still_closes_the_position(client, owner, live, clock, net):  # noqa: F811
    from tradedesk_oms import BrokerError
    from tdserver.live.ordertest import run
    _ready(client, owner, live, clock, net)

    class Flaky(AutoFill):
        armed = False
        def place(self, req):
            oid = super().place(req)
            if req.order_type == "LIMIT" and req.price > 0.2:
                Flaky.armed = True                                  # the real entry has gone out
            return oid
        def orders(self):
            if Flaky.armed:
                Flaky.armed = False
                from tradedesk_oms.broker import BrokerDown
                raise BrokerDown("timeout")                         # right after the entry
            return super().orders()
    b = Flaky()
    b.quotes = {"NIFTY 50": Quote(None, None, 25000.0), KCE: Quote(9.5, 10.0, 10.0),
                "NIFTY25N1024000PE": Quote(9.5, 10.0, 10.0)}
    lines = []
    run(live, adapter=b, ask=lambda q: True, out=lines.append, sleep=lambda s: None, drive_s=30)
    assert any(c[0] == "place" and c[5] > 0.2 for c in b.calls)      # the entry was really sent
    assert not b.positions()
    assert not [o for o in b.book.values() if o.status not in ("COMPLETE", "CANCELLED", "REJECTED")]


def test_refuses_an_account_that_is_not_flat(client, owner, live, clock, net):  # noqa: F811
    from tdserver.live.ordertest import run
    _ready(client, owner, live, clock, net)
    b = AutoFill()
    b.pos = {"NIFTY25N1026000CE": -65}
    lines = []
    assert run(live, adapter=b, ask=lambda q: True, out=lines.append, sleep=lambda s: None) == 1
    assert "must be flat" in lines[1] and not [c for c in b.calls if c[0] == "place"]


def _quotes(b):
    b.quotes = {"NIFTY 50": Quote(None, None, 25000.0), KCE: Quote(9.5, 10.0, 10.0),
                "NIFTY25N1024000PE": Quote(9.5, 10.0, 10.0)}
    return b


def test_ctrl_c_while_the_real_entry_is_in_flight_still_ends_flat(client, owner, live, clock, net):  # noqa: F811
    """Second review B1: interrupted during the entry request; the order fills; cleanup closes it."""
    from tdserver.live.ordertest import run
    _ready(client, owner, live, clock, net)

    class Interrupt(AutoFill):
        def place(self, req):
            oid = super().place(req)
            if req.order_type == "LIMIT" and req.price > 0.2 and not getattr(self, "done", False):
                self.done = True
                raise KeyboardInterrupt
            return oid
    b = _quotes(Interrupt())
    lines = []
    rc = run(live, adapter=b, ask=lambda q: True, out=lines.append, sleep=lambda s: None, drive_s=30)
    assert rc == 1 and not b.positions()
    assert not [o for o in b.book.values() if o.status not in ("COMPLETE", "CANCELLED", "REJECTED")]


def test_step_a_fails_when_modify_is_refused(client, owner, live, clock, net):  # noqa: F811
    from tradedesk_oms.broker import Rejected
    from tdserver.live.ordertest import run
    _ready(client, owner, live, clock, net)
    b = _quotes(AutoFill())
    b.fail_next = [None, Rejected("modify not allowed")]       # place ok, modify refused
    lines = []
    assert run(live, adapter=b, ask=lambda q: True, out=lines.append, sleep=lambda s: None) == 1
    assert "PASSED" not in "\n".join(lines) and not [c for c in b.calls if c[0] == "place" and c[5] > 0.2]


def test_cleanup_survives_a_second_interrupt_and_a_dead_terminal(client, owner, live, clock, net):  # noqa: F811
    """Third review B1: Ctrl-C mid-test, again during the cleanup's sleep, and the terminal gone: still ends flat."""
    from tdserver.live.ordertest import run
    _ready(client, owner, live, clock, net)
    b = _quotes(AutoFill())
    state = {"sleeps": 0, "dead": False}

    def sleep(_s):
        state["sleeps"] += 1
        if state["sleeps"] == 40:                     # during Step B, after the stop is working
            raise KeyboardInterrupt
        if state["sleeps"] == 42:                     # a second Ctrl-C inside the cleanup
            state["dead"] = True
            raise KeyboardInterrupt

    def out(line):
        if state["dead"]:
            raise OSError("EIO: terminal gone")
    run(live, adapter=b, ask=lambda q: True, out=out, sleep=sleep, drive_s=30)
    assert not b.positions()
    assert not [o for o in b.book.values() if o.status not in ("COMPLETE", "CANCELLED", "REJECTED")]


def test_ctrl_c_mid_entry_with_an_order_that_never_fills_is_cancelled(client, owner, live, clock, net):  # noqa: F811
    from tdserver.live.ordertest import run
    from tradedesk_oms.broker import FakeBroker as Plain
    _ready(client, owner, live, clock, net)

    class NoFill(Plain):
        def place(self, req):
            oid = super().place(req)
            if req.order_type == "LIMIT" and req.price > 0.2 and not getattr(self, "done", False):
                self.done = True
                raise KeyboardInterrupt
            return oid
    b = _quotes(NoFill())
    t = {"now": 0.0}
    clock_secs = lambda: t["now"]

    def sleep(s):
        t["now"] += s
        live.clock.advance(seconds=s)
    run(live, adapter=b, ask=lambda q: True, out=lambda l: None, sleep=sleep, drive_s=120, monotonic=clock_secs)
    assert not [o for o in b.book.values() if o.status not in ("COMPLETE", "CANCELLED", "REJECTED")]
    assert not b.positions()
