"""Plain data records shared by every part of the platform."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Contract:
    underlying: str
    expiry: date
    strike: int
    right: str  # "CE" or "PE"

    @property
    def label(self) -> str:
        return f"{self.underlying} {self.expiry.strftime('%d %b %y')} {self.strike} {self.right}"

    def intrinsic(self, spot: float) -> float:
        return max(spot - self.strike, 0.0) if self.right == "CE" else max(self.strike - spot, 0.0)

    def to_dict(self) -> dict:
        return {"underlying": self.underlying, "expiry": self.expiry.isoformat(),
                "strike": self.strike, "right": self.right}

    @staticmethod
    def from_dict(d: dict) -> "Contract":
        return Contract(d["underlying"], date.fromisoformat(d["expiry"]), int(d["strike"]), d["right"])


@dataclass
class Quote:
    last: float
    bid: float | None = None
    ask: float | None = None
    source: str = "model"  # "model" (Black-Scholes) or "market" (recorded / live price)
    day_open: float | None = None   # the contract's open and high so far today (live feeds: Zerodha ohlc)
    day_high: float | None = None


@dataclass
class Position:
    contract: Contract
    qty: int  # signed units: negative = short
    avg_price: float
    opened_at: datetime
    group: str = ""
    entry_costs: float = 0.0


@dataclass
class Fill:
    time: datetime
    contract: str
    side: str  # BUY / SELL
    qty: int
    price: float
    costs: float
    reason: str
    group: str
    source: str
    note: str = ""  # why, in numbers: the market and the strategy's levels when the order was sent


@dataclass
class Trade:
    """A closed round trip in one contract."""
    contract: str
    side: str  # SHORT / LONG
    qty: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    reason: str
    group: str
