from datetime import date, datetime

from tdserver.clock import IST
from tdserver.marketcal import MarketCalendar
from tdserver.settings import DATA_DIR

cal = MarketCalendar(DATA_DIR / "market.yaml")


def test_trading_days():
    assert cal.is_trading_day(date(2026, 11, 3))        # Tuesday
    assert not cal.is_trading_day(date(2026, 11, 10))   # holiday in the list
    assert not cal.is_trading_day(date(2026, 11, 7))    # Saturday
    assert cal.is_trading_day(date(2026, 2, 1))         # Budget Sunday session
    assert cal.next_trading_day(date(2026, 11, 9)) == date(2026, 11, 11)


def test_session_times_change_on_3_aug_2026():
    assert cal.session(date(2026, 7, 31)).fo_close.strftime("%H:%M") == "15:30"
    assert cal.session(date(2026, 8, 3)).fo_close.strftime("%H:%M") == "15:40"


def test_market_state():
    at = lambda h, m, d=3: datetime(2026, 11, d, h, m, tzinfo=IST)  # noqa: E731
    assert cal.market_state(at(8, 0))["state"] == "closed"
    assert cal.market_state(at(9, 5))["state"] == "pre_open"
    assert cal.market_state(at(10, 0))["state"] == "open"
    assert cal.market_state(at(15, 36))["state"] == "fo_only"
    s = cal.market_state(at(16, 0))
    assert s["state"] == "closed" and s["next_open"].startswith("2026-11-04T09:15")
    assert cal.market_state(at(10, 0, d=7))["reason"] == "weekend"


def test_kite_day_boundary_is_6am_ist():
    assert cal.kite_day(datetime(2026, 11, 3, 5, 59, tzinfo=IST)) == date(2026, 11, 2)
    assert cal.kite_day(datetime(2026, 11, 3, 6, 0, tzinfo=IST)) == date(2026, 11, 3)


def test_holidays_listed_through_2026():
    assert cal.holidays_known_for(2026) and not cal.holidays_known_for(2027)
