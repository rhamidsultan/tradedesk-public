"""Strategy spec v0 (see .claude/skills/strategy-spec). One JSON document per strategy.

Validation enforces the non-negotiables: a stop loss and a max loss per day are required, and
a strategy that sells options must cap its loss per leg. Only features the engine executes are
accepted: anything else is rejected rather than silently ignored.
"""
from __future__ import annotations

import json
from datetime import time
from importlib import resources
from pathlib import Path
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Timeframe = Literal["1m", "3m", "5m", "15m"]
Day = Literal["Mon", "Tue", "Wed", "Thu", "Fri"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Instrument(_M):
    underlying: Literal["NIFTY", "BANKNIFTY", "SENSEX"]
    segment: Literal["index_options"] = "index_options"
    timeframe: Timeframe = "5m"


class Schedule(_M):
    days: list[Day] = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    start: str = "09:20"
    no_new_after: str = "14:30"
    exit_by: str = "15:15"
    skip_expiry_day: bool = False
    hold_overnight: bool = False          # NRML positions carried to later days
    expiry_exit_time: str = "15:15"       # positional: close on expiry day at this time

    @field_validator("start", "no_new_after", "exit_by", "expiry_exit_time")
    @classmethod
    def _time(cls, v: str) -> str:
        t = time.fromisoformat(v)
        if not (time(9, 15) <= t <= time(15, 29)):
            raise ValueError(f"{v} is outside the 09:15–15:29 session")
        return v

    @model_validator(mode="after")
    def _order(self):
        if not (time.fromisoformat(self.start) <= time.fromisoformat(self.no_new_after) <= time.fromisoformat(self.exit_by)):
            raise ValueError("need start <= no_new_after <= exit_by")
        return self


Operand = Union[float, str]
# Named series the engine evaluates on the strategy timeframe (closed bars only):
#   open high low close, ema(n) sma(n) rsi(n) atr(n), supertrend(n,k) (value) / supertrend_dir(n,k) (+1 up / -1 down),
#   opening_range_high(m) opening_range_low(m), day_high day_low, prev_day_high prev_day_low prev_day_close


class Condition(_M):
    left: Operand
    op: Literal[">", "<", ">=", "<=", "==", "crosses_above", "crosses_below"]
    right: Operand


class EntryRule(_M):
    all_of: list[Condition] = Field(default_factory=list)
    at_time: str | None = None            # time-based entry (e.g. sell a strangle at 09:20)
    action: str                            # label that selects legs (`when`)

    @model_validator(mode="after")
    def _one(self):
        if not self.all_of and not self.at_time:
            raise ValueError("an entry rule needs conditions or at_time")
        return self


class Entry(_M):
    any_of: list[EntryRule]


class Strike(_M):
    by: Literal["atm_offset", "premium"]
    value: float                           # steps from ATM (+ = higher strike) or target premium in ₹


class Leg(_M):
    when: str
    side: Literal["BUY", "SELL"]
    type: Literal["CE", "PE"]
    expiry: Literal["nearest_weekly", "next_weekly", "nearest_monthly"] = "nearest_weekly"
    strike: Strike
    lots: Union[int, Literal["from_risk"]] = 1


class Level(_M):
    on: Literal["underlying", "premium"]
    type: Literal["points", "percent", "multiple", "rr"]
    value: float = Field(gt=0)


class Trail(_M):
    """Trailing stop on the option premium: the stop follows the best price at `value` points/percent behind,
    once the leg is `activate_after` points/percent in profit. Moves only in the leg's favour, in steps of
    at least `step` points (each move modifies the SL order at the broker)."""
    type: Literal["points", "percent"] = "points"
    value: float = Field(gt=0)
    activate_after: float = Field(default=0, ge=0)
    step: float = Field(default=1.0, gt=0)


class Risk(_M):
    stop_loss: Level
    trailing: Trail | None = None
    position_stop: float | None = Field(default=None, gt=0, description="₹: close the whole trade (all legs) at this net loss")
    position_target: float | None = Field(default=None, gt=0, description="₹: close the whole trade at this net profit")
    reentry_gap: float = Field(default=0, ge=0, description="points: re-enter the same option only this far beyond its stop-out price")
    backstop: bool = Field(default=True, description="python strategies: framework stop on every leg. False = research "
                                                     "backtests only (comparing with Algo Lab); paper and live refuse it")
    target: Level | None = None
    risk_per_trade: float | None = Field(default=None, gt=0, description="₹, used by lots=from_risk")
    max_lots: int = Field(default=10, ge=1)
    max_loss_per_day: float = Field(gt=0)
    max_trades_per_day: int = Field(default=2, ge=1)
    reentry_after_sl: int = Field(default=0, ge=0)
    assumed_delta: float = Field(default=0.8, gt=0, le=1, description="spot SL → option loss for from_risk sizing")
    close_lone_leg_eod: bool = Field(default=False,
                                     description="overnight strategies: close a leg left alone after its pair stopped, at 15:25")

    @model_validator(mode="after")
    def _checks(self):
        if self.stop_loss.type == "rr":
            raise ValueError("stop loss cannot be a risk:reward ratio")
        if self.trailing and self.stop_loss.on != "premium":
            raise ValueError("a trailing stop needs a premium stop loss")
        if self.target and self.target.type == "rr" and self.target.on != self.stop_loss.on:
            raise ValueError("an rr target must be measured on the same thing as the stop loss")
        return self


class Costs(_M):
    slippage_pct: float = Field(default=0.5, ge=0, description="% of premium lost per fill")
    capital: float = Field(default=200000, gt=0)
    stop_fill_frac: float = Field(default=0.5, ge=0, le=1,
                                  description="stop touched inside a bar fills this far from the stop toward the bar extreme")
    sl_order: Literal["assume_fill", "limit"] = Field(default="assume_fill",
                                                       description="limit = Zerodha SL-limit: may stay unfilled in a spike")
    sl_limit_buffer_pct: float = Field(default=10, gt=0, description="SL-limit price = trigger ± this %")
    skip_thin: bool = Field(default=False, description="skip entries larger than 10% of the option's last 5 minutes' volume")
    # Algo Lab's honest fills (PR #57), used by the rebuild server by default; "classic" keeps the v0 model
    fill_model: Literal["classic", "honest"] = Field(
        default="classic", description="honest = minimum spread on cheap options, no fill at a trade older than "
                                       "fill_max_age_min, decisions only on prices known at the time")
    min_half_spread: list[tuple[float, float]] = Field(
        default=[(20, 0.10), (100, 0.20)],
        description="honest: a fill pays at least this much per side (Rs) when the premium is below the first number")
    fill_max_age_min: int = Field(default=3, ge=1, le=30, description="honest: oldest trade a fill may use")
    cost_stress: float = Field(default=1.0, ge=0.5, le=5, description="honest: x the slippage and spread")


class Strategy(_M):
    id: str
    name: str
    version: int = 1
    kind: Literal["nocode", "python"] = "nocode"
    module: str | None = None              # python: strategy name in tradedesk_strategies (e.g. "opening_move_itm")
    instrument: Instrument
    schedule: Schedule = Schedule()
    entry: Entry = Field(default_factory=lambda: Entry(any_of=[]))
    legs: list[Leg] = Field(default_factory=list)
    risk: Risk
    costs: Costs = Costs()
    params: dict = Field(default_factory=dict)
    notes: str = ""

    @model_validator(mode="after")
    def _legs(self):
        if self.kind == "python":
            if not self.module:
                raise ValueError("a python strategy needs `module`")
            if self.risk.stop_loss.on != "premium":
                raise ValueError("python strategies need a premium stop loss per leg (the broker backstop)")
            return self
        if not self.entry.any_of or not self.legs:
            raise ValueError("a no-code strategy needs entry rules and legs")
        actions = {r.action for r in self.entry.any_of}
        for leg in self.legs:
            if leg.when not in actions:
                raise ValueError(f"leg for {leg.when!r} has no entry rule with that action")
            if leg.lots == "from_risk" and not self.risk.risk_per_trade:
                raise ValueError("lots=from_risk needs risk.risk_per_trade")
            if leg.side == "SELL" and self.risk.stop_loss.on != "premium":
                raise ValueError("selling options needs a premium stop loss per leg")
        for a in actions:
            if not any(l.when == a for l in self.legs):
                raise ValueError(f"entry action {a!r} opens no legs")
        if self.schedule.hold_overnight and any(l.side == "BUY" for l in self.legs) and self.risk.stop_loss.on == "underlying":
            raise ValueError("overnight positions need a premium stop loss (placed at the broker)")
        return self


def load_strategy(path_or_dict) -> Strategy:
    if isinstance(path_or_dict, (str, Path)):
        return Strategy.model_validate(json.loads(Path(path_or_dict).read_text()))
    return Strategy.model_validate(path_or_dict)


def templates() -> list[str]:
    return sorted(p.name.removesuffix(".json") for p in resources.files("tradedesk_spec").joinpath("templates").iterdir()
                  if p.name.endswith(".json"))


def load_template(name: str) -> Strategy:
    text = resources.files("tradedesk_spec").joinpath("templates", f"{name}.json").read_text()
    return Strategy.model_validate(json.loads(text))
