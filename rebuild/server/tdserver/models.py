"""Database tables for phase 1.

Times are stored as timezone-aware UTC. Secrets (broker keys, tokens, alert targets) are stored only as
envelope-encrypted bytes (see crypto.py). Every user-owned row carries user_id.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .db import Base


class UTCDateTime(TypeDecorator):
    """Stores aware datetimes as UTC and always returns them aware (SQLite drops the zone otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given to a UTC column")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(16), default="trader")  # owner | trader | viewer
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # access per area (see access.py); None = the role's default. Owner always has everything.
    access: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    hide_amounts: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    devices: Mapped[list[Device]] = relationship(back_populates="user")


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="trader")
    name: Mapped[str] = mapped_column(String(80))
    # set when the invite adds a device for an existing user instead of creating one
    for_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    access: Mapped[dict | None] = mapped_column(JSON, nullable=True)        # applied to the new user on enrolment
    hide_amounts: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    key_id: Mapped[str] = mapped_column(String(64), unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)  # P-256, X9.63 uncompressed (65 bytes)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # free Apple account: the app reports when its 7-day install expires, for the re-install reminder
    install_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user: Mapped[User] = relationship(back_populates="devices")


class Challenge(Base):
    __tablename__ = "challenges"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class AuthSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="app")  # app | console
    csrf: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ConsoleLink(Base):
    """One-time sign-in link for the web safety console, sent to the user's own alert channels."""

    __tablename__ = "console_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"
    __table_args__ = (UniqueConstraint("user_id", "broker"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    broker: Mapped[str] = mapped_column(String(16), default="zerodha")
    client_id_masked: Mapped[str] = mapped_column(String(16))
    # keyed hash of the full Zerodha client id: a login for any other Zerodha account is refused
    client_id_hash: Mapped[str] = mapped_column(String(64))
    data_key_enc: Mapped[bytes] = mapped_column(LargeBinary)
    api_key_enc: Mapped[bytes] = mapped_column(LargeBinary)
    api_secret_enc: Mapped[bytes] = mapped_column(LargeBinary)
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_day: Mapped[date | None] = mapped_column(Date, nullable=True)  # the Kite day the token is for
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class KiteLoginState(Base):
    __tablename__ = "kite_login_states"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    origin: Mapped[str] = mapped_column(String(16))  # app | console
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class LinkCode(Base):
    """One-time short codes, e.g. the Telegram /start code. Stored hashed, 15-minute life."""

    __tablename__ = "link_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(24))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class AlertChannel(Base):
    __tablename__ = "alert_channels"
    __table_args__ = (UniqueConstraint("user_id", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16))  # telegram | ntfy
    data_key_enc: Mapped[bytes] = mapped_column(LargeBinary)
    target_enc: Mapped[bytes] = mapped_column(LargeBinary)  # Telegram chat id or ntfy topic
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AlertPref(Base):
    __tablename__ = "alert_prefs"
    __table_args__ = (UniqueConstraint("user_id", "category"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    category: Mapped[str] = mapped_column(String(32))
    muted: Mapped[bool] = mapped_column(Boolean, default=False)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    level: Mapped[str] = mapped_column(String(16))  # info | important | critical
    category: Mapped[str] = mapped_column(String(32))  # market_data | paper | live | portfolio | eod | system
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text, default="")
    path: Mapped[str] = mapped_column(String(160), default="")  # screen to open in the app
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    next_send_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    sends: Mapped[int] = mapped_column(Integer, default=0)
    acked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    acked_via: Mapped[str | None] = mapped_column(String(16), nullable=True)


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    ok: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(String(200), default="")
    at: Mapped[datetime] = mapped_column(UTCDateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str] = mapped_column(String(120), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime)


class Setting(Base):
    """Small owner settings stored as JSON (e.g. what the recorder records)."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class ServiceStatus(Base):
    """Heartbeat of a background service (recorder, worker), read by Health and the status strip."""

    __tablename__ = "service_status"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class RecordingRun(Base):
    """One line per trading day: did the day's prices get recorded, and if not, why."""

    __tablename__ = "recording_runs"
    __table_args__ = (UniqueConstraint("day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))      # ok | warn | fail | pending
    reason: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime)


class BacktestRun(Base):
    """A backtest asked for from the app. The result (trades, fills, curves) is a file in TD2_RUNS_DIR."""

    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    template: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_id: Mapped[int | None] = mapped_column(ForeignKey("strategies.id"), nullable=True, index=True)
    strategy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="backtest", server_default="backtest")  # backtest | sweep
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)       # sweep: grid, split, folds
    underlying: Mapped[str] = mapped_column(String(16))
    spec: Mapped[dict] = mapped_column(JSON)
    start: Mapped[date] = mapped_column(Date)
    end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|failed|cancelled
    progress: Mapped[int] = mapped_column(Integer, default=0)      # percent
    note: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    starred: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class StrategyDoc(Base):
    """A user's strategy: a draft being edited (may be incomplete) and saved versions (always valid)."""

    __tablename__ = "strategies"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16), default="nocode")
    template: Mapped[str | None] = mapped_column(String(64), nullable=True)
    draft: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0)   # 0 = never saved
    share_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stage: Mapped[str] = mapped_column(String(16), default="research", server_default="research")  # research|paper|shadow|live
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("strategy_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict] = mapped_column(JSON)
    spec_hash: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(String(200), default="")
    accepted_risk: Mapped[float | None] = mapped_column(nullable=True)   # Rs per trade the owner accepted above 2%
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class StrategyPreset(Base):
    """Named settings for a strategy (Algo Studio presets): overrides applied on top of the saved version."""

    __tablename__ = "strategy_presets"
    __table_args__ = (UniqueConstraint("strategy_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class PaperRun(Base):
    """A paper session: a frozen spec run by the engine on today's recorded bars, minute by minute. No orders.

    Kill rules and the gate to the next stage are fixed when the session starts (`rules`) and never change.
    Trader actions (pause, square off) are minute-stamped per day (`actions`) so each replay repeats them."""

    __tablename__ = "paper_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    strategy_id: Mapped[int | None] = mapped_column(ForeignKey("strategies.id", name="fk_paper_strategy"), nullable=True, index=True)
    strategy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    underlying: Mapped[str] = mapped_column(String(16))
    spec: Mapped[dict] = mapped_column(JSON)
    spec_hash: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)   # running | paused | stopped
    first_day: Mapped[date] = mapped_column(Date)
    start_minute: Mapped[int] = mapped_column(Integer, default=0)     # no entries before this minute on the first day
    last_day: Mapped[date | None] = mapped_column(Date, nullable=True)  # last day booked after the close
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    actions: Mapped[dict] = mapped_column(JSON, default=dict)          # "YYYY-MM-DD" -> {no_entry_from, square_off}
    state: Mapped[dict] = mapped_column(JSON, default=dict)            # the latest state shown on the phone
    alerted: Mapped[list] = mapped_column(JSON, default=list)          # alert keys already sent (restart-safe)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class LiveRun(Base):
    """A shadow or live session: a frozen spec whose engine decisions become orders through the user's OrderManager.

    mode "shadow": real prices, simulated fills (never sends). mode "live": real orders, only when the server's
    ENABLED file exists, the user's account limits are set, and the session is armed for that day with a
    Face ID signature."""

    __tablename__ = "live_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    strategy_id: Mapped[int | None] = mapped_column(ForeignKey("strategies.id", name="fk_live_strategy"), nullable=True)
    strategy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paper_id: Mapped[int | None] = mapped_column(Integer, nullable=True)     # the paper session whose gate was met
    name: Mapped[str] = mapped_column(String(120))
    underlying: Mapped[str] = mapped_column(String(16))
    spec: Mapped[dict] = mapped_column(JSON)
    spec_hash: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(8))                  # shadow | live
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)   # running | paused | stopped
    armed_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    armed_device: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_day: Mapped[date] = mapped_column(Date)
    start_minute: Mapped[int] = mapped_column(Integer, default=0)
    last_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class LiveCommand(Base):
    """A command from the phone for the live runner (the single writer of orders). Ids come from the phone, so a
    repeated tap or a retried request is the same command."""

    __tablename__ = "live_commands"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    mode: Mapped[str] = mapped_column(String(8))                  # shadow | live
    kind: Mapped[str] = mapped_column(String(16))                 # kill | exit_leg | tighten_stop | exit_session
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="queued", index=True)   # queued | done | failed
    result: Mapped[str] = mapped_column(String(300), default="")
    tries: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class JournalEntry(Base):
    """One closed trade in the user's journal, imported from a paper, shadow or live session (or added by hand),
    with the trader's own setup tag, tags, notes and rating. Imported numbers are never edited here."""

    __tablename__ = "journal_entries"
    __table_args__ = (UniqueConstraint("user_id", "source", "source_key", name="uq_journal_source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(8))                # paper | shadow | live | manual
    source_key: Mapped[str] = mapped_column(String(80))           # session id + trade id (manual: a random key)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(120))                # strategy / session name
    underlying: Mapped[str] = mapped_column(String(16))
    entry_at: Mapped[str] = mapped_column(String(19))             # IST "YYYY-MM-DD HH:MM"
    exit_at: Mapped[str] = mapped_column(String(19))
    net: Mapped[float] = mapped_column(Float)
    gross: Mapped[float] = mapped_column(Float)
    charges: Mapped[float] = mapped_column(Float)
    exit_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    legs: Mapped[list] = mapped_column(JSON, default=list)
    setup: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)     # 1..5
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


# ---- portfolio (the owner's Zerodha holdings; see tdserver/portfolio) --------------------------------------------
class PfSnapshot(Base):
    """The whole portfolio once a minute in market hours (and once after the close)."""
    __tablename__ = "pf_snapshots"
    ts: Mapped[str] = mapped_column(String(19), primary_key=True)        # IST "YYYY-MM-DD HH:MM:SS"
    day: Mapped[str] = mapped_column(String(10), index=True)
    value: Mapped[float] = mapped_column(Float)
    invested: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float)
    day_pnl: Mapped[float] = mapped_column(Float)
    gold_value: Mapped[float] = mapped_column(Float, default=0)


class PfHoldingDay(Base):
    """One row per holding per trading day (the day's last figures, high and low)."""
    __tablename__ = "pf_holding_days"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    qty: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float] = mapped_column(Float)
    last_price: Mapped[float] = mapped_column(Float)
    value: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float)
    day_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    day_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    day_low: Mapped[float | None] = mapped_column(Float, nullable=True)


class PfTrade(Base):
    """Equity trades from Zerodha (today's, captured during the day) and from Console tradebook imports."""
    __tablename__ = "pf_trades"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(8), primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    ts: Mapped[str] = mapped_column(String(19))
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    isin: Mapped[str] = mapped_column(String(16), default="")
    side: Mapped[str] = mapped_column(String(4))          # buy | sell
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    order_id: Mapped[str] = mapped_column(String(40), default="")
    source: Mapped[str] = mapped_column(String(12))       # api | console | import


class PfPrice(Base):
    """Daily close, high and low (Zerodha's history, adjusted for splits and bonuses)."""
    __tablename__ = "pf_prices"
    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    close: Mapped[float] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)


class PfExtreme(Base):
    """Highest and lowest day P&L of each day ("YYYY-MM-DD") and total P&L ever ("ALL")."""
    __tablename__ = "pf_extremes"
    scope: Mapped[str] = mapped_column(String(10), primary_key=True)
    metric: Mapped[str] = mapped_column(String(10), primary_key=True)
    high: Mapped[float] = mapped_column(Float)
    high_ts: Mapped[str] = mapped_column(String(19))
    low: Mapped[float] = mapped_column(Float)
    low_ts: Mapped[str] = mapped_column(String(19))


class PfAlertSent(Base):
    """Portfolio alerts already sent (each fires at most once a day)."""
    __tablename__ = "pf_alerts_sent"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    ts: Mapped[str] = mapped_column(String(19))
    message: Mapped[str] = mapped_column(String(300))


class PfAction(Base):
    """Corporate actions entered by hand (split, bonus, merge, demerge, acquire, dispose), used by stock history."""
    __tablename__ = "pf_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    data: Mapped[dict] = mapped_column(JSON)
