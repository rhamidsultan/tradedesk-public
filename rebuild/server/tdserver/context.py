"""Everything a request or the worker needs, built once per process (tests build their own)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from .clock import Clock
from .crypto import LinkSigner, LocalKeyProvider, Vault
from .db import Base, make_engine, make_sessionmaker
from .datastore import DataService
from .marketcal import MarketCalendar
from .ratelimit import RateLimiter
from .settings import Settings


@dataclass
class AppContext:
    settings: Settings
    sessions: sessionmaker[Session]
    vault: Vault
    links: LinkSigner
    clock: Clock
    calendar: MarketCalendar
    http: httpx.Client
    limiter: RateLimiter = field(default_factory=RateLimiter)
    data: DataService | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def with_data(self) -> "AppContext":
        self.data = DataService(self.settings.store_root or None, self.calendar, self.clock, self.settings.duckdb_memory)
        return self


def quiet_http_logs() -> None:
    """httpx logs every request URL at INFO; Telegram URLs contain the bot token. Keep them out of logs."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def build_context(settings: Settings, clock: Clock | None = None, http: httpx.Client | None = None,
                  create_tables: bool = False) -> AppContext:
    quiet_http_logs()
    settings.check_production()
    engine = make_engine(settings.database_url)
    if create_tables:
        from . import models  # noqa: F401  (registers tables)
        Base.metadata.create_all(engine)
    master = settings.master_key_bytes()
    return AppContext(
        settings=settings,
        sessions=make_sessionmaker(engine),
        vault=Vault(LocalKeyProvider(master)),
        links=LinkSigner(master),
        clock=clock or Clock(),
        calendar=MarketCalendar(settings.market_file),
        http=http or httpx.Client(timeout=10.0),
        data=None,
    ).with_data()
