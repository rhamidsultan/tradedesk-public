"""The live runner: `python -m tdserver.live` (its own systemd unit). The only process that can send real orders.

Per user and mode (shadow | live) one Desk (OrderManager + bridge). Every 2 s, per desk and each in its own error
boundary: phone commands first (Stop all never waits for anything), then start-of-day reconciliation, prices of
open legs through the user's quote gate (one batched request a second at most), the daily-loss check net of
estimated charges, the expiry-day cut-off, and the order book poll. Every minute, 20 s after the bar, each
session's engine replays today's closed bars and its decisions go to the desk; engine exits are followed until
the F&O close. After the close the engines book the day.

A desk is built for every user with a session running, legs open in its order book, or a command queued, so a
restart never leaves legs or a Stop all unmanaged. Opening orders additionally need: the ENABLED file (outside the
services' writable folders), the user's account limits, the user's own Zerodha login today, and the session armed
today with a Face ID signature. Without limits or with live switched off a desk runs closing-only.
"""
from __future__ import annotations

import gzip
import logging
import signal
import sqlite3
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from ..alerts.outbox import raise_alert
from ..context import AppContext
from ..models import BrokerAccount, LiveCommand, LiveRun, ServiceStatus, Setting
from ..paper.engine import PaperEngine
from ..paper.runner import N_MIN, READ_AFTER_S, Days, minute_index
from ..recorder.universe import OPTION_EXCHANGE, parse_dump, store_symbol
from .desk import Desk
from .quotes import GatedBroker, QuoteGate

log = logging.getLogger("tdserver.live")

POLL_S = 2.0
PRICES_EVERY_S = 5.0
RECORDER_STALE_S = 90
MTM_UNKNOWN_ALERT_S = 30
BOOK_AFTER = timedelta(minutes=5)
COMMAND_TRIES = 3
MODES = ("live", "shadow")


def live_dir(ctx: AppContext) -> Path:
    base = Path(ctx.settings.data_dir or ctx.settings.store_root or ".").expanduser()
    return base / "live"


def enabled_file(ctx: AppContext) -> Path:
    """Set TD2_LIVE_ENABLED_FILE to a root-owned path outside the services' writable folders (e.g. /etc/td2/)."""
    f = getattr(ctx.settings, "live_enabled_file", "") or ""
    return Path(f).expanduser() if f else live_dir(ctx) / "ENABLED"


def enabled(ctx: AppContext) -> bool:
    """Real opening orders are possible only while the owner has created this file on the server. In production the
    file must be configured (TD2_LIVE_ENABLED_FILE, root-owned): the services' own data folder never counts."""
    return enabled_reason(ctx) is None


def enabled_reason(ctx: AppContext) -> str | None:
    """None when live is switched on, else why not (shown in the checklist)."""
    conf = getattr(ctx.settings, "live_enabled_file", "") or ""
    if not ctx.settings.is_dev and not conf:
        return "TD2_LIVE_ENABLED_FILE is not set on the server"
    f = enabled_file(ctx)
    if not f.exists():
        return f"no file at {f}"
    if not ctx.settings.is_dev:
        st = f.stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            return f"{f} must be owned by root and writable by root only"
    return None


UNDERLYINGS = ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "NIFTY")


def underlying_of(kite_symbol: str) -> str | None:
    for u in UNDERLYINGS:
        if kite_symbol.startswith(u):
            return u
    return None


def account_limits(db, user_id: int) -> dict | None:
    row = db.get(Setting, f"live_limits:{user_id}")
    v = (row.value if row else None) or None
    if not v:
        return None
    try:
        return {"max_lots": int(v["max_lots"]), "max_orders_day": int(v["max_orders_day"]),
                "daily_loss": float(v["daily_loss"]), "entry_window": tuple(v.get("entry_window", ("09:16", "15:00"))),
                "expiry_cutoff": str(v.get("expiry_cutoff", "15:15"))}
    except (KeyError, TypeError, ValueError):
        return None


def exchange_of(sym: str) -> str:
    return "BFO" if sym.startswith("SENSEX") else "NFO"


def contracts(root: Path, day: date, underlying: str) -> dict:
    """store symbol -> (Zerodha trading symbol, exchange, lot size, expiry, underlying) from today's instruments list."""
    ex = OPTION_EXCHANGE.get(underlying, "NFO")
    f = Path(root) / "reference" / "instruments" / day.isoformat() / f"{ex}.csv.gz"
    if not f.exists():
        return {}
    out = {}
    for l in parse_dump(gzip.decompress(f.read_bytes()).decode()):
        if l.name == underlying and l.expiry and l.kind in ("CE", "PE"):
            out[store_symbol(underlying, l.expiry, l.strike, l.kind)] = (l.tradingsymbol, ex, l.lot_size, l.expiry, underlying)
    return out


def lot_check(und: str, expiry: date, kite_lot: int) -> str | None:
    from tradedesk_marketcal import load_refs
    ref = load_refs().lot_size(und, expiry)
    if ref is None or ref.value != kite_lot:
        return f"lot size: reference data {ref.value if ref else '?'} vs Zerodha {kite_lot}"
    return None


def freeze_of(und: str, d: date) -> int | None:
    from tradedesk_marketcal import load_refs
    ref = load_refs().freeze_qty(und, d)
    return ref.value if ref else None


def charges_fn(d: date):
    from tradedesk_marketcal import load_refs
    cal = load_refs()
    return lambda side, value: cal.option_charges(d, side, value)


def oms_has_open(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    try:
        c = sqlite3.connect(str(db_path))
        r = c.execute("SELECT 1 FROM legs WHERE status IN ('pending','entering','open','exiting') LIMIT 1").fetchone()
        c.close()
        return bool(r)
    except sqlite3.Error:
        return True                                      # unknown: manage it


class LiveRunner:
    sleep = staticmethod(_time.sleep)        # the order throttle's wait (tests replace it)

    def __init__(self, ctx: AppContext, broker_factory=None, shadow_factory=None, quote_factory=None):
        self.ctx = ctx
        self.desks: dict[tuple[int, str], tuple[Desk, str | None]] = {}   # (user, mode) -> (desk, token)
        self.gates: dict[int, tuple[QuoteGate, str]] = {}                 # user -> (quote gate, token)
        self.engines: dict[int, PaperEngine] = {}
        self.broker_factory = broker_factory or self._zerodha             # (api key, token) -> order adapter
        self.shadow_factory = shadow_factory or self._shadow              # (user, quote fn) -> ShadowBroker
        self.quote_factory = quote_factory or self._reader                # (api key, token) -> (symbols, ex) -> quotes
        self.last_minute: tuple[date, int] | None = None
        self.last_prices: dict[tuple[int, str], float] = {}
        self.contracts: dict[tuple[str, date], dict] = {}
        self.alerted: set[str] = set()

    # ------------------------------------------------------------------ brokers and quotes
    @staticmethod
    def _zerodha(api_key: str, token: str):
        from tradedesk_oms.zerodha import ZerodhaAdapter
        return ZerodhaAdapter(api_key, token)

    @staticmethod
    def _reader(api_key: str, token: str):
        from tradedesk_oms.zerodha import ZerodhaAdapter
        reader = ZerodhaAdapter(api_key, token)          # only its quote() is used: a read-only GET
        return lambda syms, ex="NFO": reader.quote(syms, ex)

    def _shadow(self, user_id: int, quote):
        from tradedesk_oms.shadow import ShadowBroker
        return ShadowBroker(quote, self._dir(user_id, "shadow") / "book.json", exchange_of=exchange_of)

    def _dir(self, user_id: int, mode: str) -> Path:
        base = Path(self.ctx.settings.runs_dir or (Path(self.ctx.settings.store_root or ".") / "runs")).expanduser()
        d = base / "live" / f"user{user_id}" / mode
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _login(self, db, user_id: int) -> tuple[str, str] | None:
        from ..kite import access_token, account, secrets_for
        acc = account(db, user_id)
        tok = access_token(self.ctx, acc) if acc else None
        if not tok:
            return None
        api_key, _ = secrets_for(self.ctx, acc)
        return api_key, tok

    def _gate(self, user_id: int, login: tuple[str, str]) -> QuoteGate:
        have = self.gates.get(user_id)
        if have and have[1] == login[1]:
            return have[0]
        g = QuoteGate(self.quote_factory(*login))
        self.gates[user_id] = (g, login[1])
        return g

    # ------------------------------------------------------------------ desks
    def _limits(self, lim: dict | None):
        from tradedesk_oms import Limits
        if lim is None:                                   # closing-only: nothing can open
            return Limits(max_lots=1, max_orders_day=0, daily_loss=1e12)
        return Limits(max_lots=lim["max_lots"], max_orders_day=lim["max_orders_day"], daily_loss=lim["daily_loss"],
                      entry_window=lim["entry_window"])

    def desk(self, db, user_id: int, mode: str, needed: bool) -> tuple[Desk | None, str]:
        """The user's desk for this mode with today's login and current limits. needed: legs open or a command
        queued, so the desk must run even closing-only. (None, why) when it cannot run at all."""
        lim = account_limits(db, user_id)
        login = self._login(db, user_id)
        on = enabled(self.ctx) if mode == "live" else True
        closing_only = lim is None or not on
        have = self.desks.get((user_id, mode))
        why = ("Set your live account limits: closing only." if lim is None else
               "Live is switched off at the server: closing only." if not on else "")
        if have is None and closing_only and not needed:
            return None, why or "Not needed."
        saved = self._dir(user_id, mode) / "last_limits.json"
        if lim is not None:
            try:
                import json as _json
                saved.write_text(_json.dumps({**lim, "entry_window": list(lim["entry_window"])}))
            except OSError:
                pass
            limits_obj = self._limits(lim)
        elif have is not None:
            limits_obj = have[0].om.L                      # a missing/corrupt row never drops the day-loss check
        else:
            limits_obj = self._limits(None)
            try:                                           # after a restart: the last good limits of this desk
                import json as _json
                old = _json.loads(saved.read_text())
                old["entry_window"] = tuple(old["entry_window"])
                limits_obj = self._limits(old)
            except (OSError, ValueError, KeyError):
                pass
        if login is None:
            if have:                                       # keep managing with the old login until it fails
                have[0].set_limits(limits_obj, self._preflight_fn(user_id, mode, lim), closing_only)
                return have[0], "No Zerodha login today: new entries paused; stops at Zerodha keep working."
            return None, "No Zerodha login today: open legs are protected only by their stops at Zerodha."
        gate = self._gate(user_id, login)
        token = login[1]
        now = self.ctx.clock.now_ist()
        if have and have[1] == token:
            have[0].set_limits(limits_obj, self._preflight_fn(user_id, mode, lim), closing_only)
            return have[0], why
        broker = GatedBroker(self.broker_factory(*login), gate) if mode == "live" else self.shadow_factory(user_id, gate.quote)
        if have:
            have[0].new_day(now.date(), broker=broker)
            have[0].set_limits(limits_obj, self._preflight_fn(user_id, mode, lim), closing_only)
            self.desks[(user_id, mode)] = (have[0], token)
            return have[0], why
        desk = Desk(user_id, mode, broker, self._limits(lim), self._dir(user_id, mode),
                    alert=self._alert_fn(user_id, mode), log=lambda s: log.info("u%s %s %s", user_id, mode, s),
                    preflight=self._preflight_fn(user_id, mode, lim), lot_check=lot_check,
                    freeze_of=freeze_of if mode == "live" else None, closing_only=closing_only,
                    market_open=self._market_open,
                    clock=lambda: self.ctx.clock.now().timestamp(), sleep=self.sleep)
        desk.new_day(now.date())
        self.desks[(user_id, mode)] = (desk, token)
        return desk, why

    def _market_open(self) -> bool:
        now = self.ctx.clock.now_ist()
        cal = self.ctx.calendar
        if not cal.is_trading_day(now.date()):
            return False
        s = cal.session(now.date())
        return s.open <= now.time().replace(tzinfo=None) < s.fo_close

    def _alert_fn(self, user_id: int, mode: str):
        def alert(level: str, title: str, text: str):
            with self.ctx.sessions() as db:
                raise_alert(db, self.ctx, user_id, level, "live", f"{'LIVE' if mode == 'live' else 'Shadow'} · {title}",
                            text[:900], path="/trade/live")
                db.commit()
        return alert

    def _alert_once(self, db, user_id: int, key: str, level: str, title: str, body: str) -> None:
        k = f"{self.ctx.clock.now_ist().date()}|{user_id}|{key}"
        if k in self.alerted:
            return
        self.alerted.add(k)
        raise_alert(db, self.ctx, user_id, level, "live", title, body[:900], path="/trade/live", dedupe_key=k)

    def _preflight_fn(self, user_id: int, mode: str, lim: dict | None):
        """Checks before any opening order (the OrderManager adds its own guards)."""
        cut = (lim or {}).get("expiry_cutoff", "15:15")

        def preflight(legs) -> list[str]:
            reasons = []
            with self.ctx.sessions() as db:
                st = db.get(ServiceStatus, "recorder")
                age = (self.ctx.clock.now() - st.updated_at).total_seconds() if st else None
                if age is None or age > RECORDER_STALE_S:
                    reasons.append("price recording is not running (the engine would decide on old bars)")
            now = self.ctx.clock.now_ist()
            for l in legs:
                exp = self._expiry_of(l.symbol)
                if exp is None:
                    reasons.append(f"{l.symbol}: expiry unknown")
                elif exp == now.date() and now.strftime("%H:%M") >= cut:
                    reasons.append(f"{l.symbol} expires today: no entries after {cut}")
            return reasons
        return preflight

    def _expiry_of(self, kite_symbol: str) -> date | None:
        for m in self.contracts.values():
            for v in m.values():
                if v[0] == kite_symbol:
                    return v[3]
        return None

    def _contracts(self, und: str, d: date) -> dict:
        k = (und, d)
        if k not in self.contracts:
            m = contracts(Path(self.ctx.settings.store_root), d, und)
            if not m:
                return {}                                   # not downloaded yet: try again next minute
            self.contracts = {kk: v for kk, v in self.contracts.items() if kk[1] == d}
            self.contracts[k] = m
        return self.contracts[k]

    def disarm_all(self, user_id: int, mode: str) -> None:
        with self.ctx.sessions() as db:
            for r in db.scalars(select(LiveRun).where(LiveRun.user_id == user_id, LiveRun.mode == mode)):
                r.armed_day = None
            db.commit()

    def _needed(self, db) -> set[tuple[int, str]]:
        """(user, mode) pairs that must have a desk: open legs in an order book or a queued command."""
        out = set()
        base = Path(self.ctx.settings.runs_dir or (Path(self.ctx.settings.store_root or ".") / "runs")).expanduser() / "live"
        for p in base.glob("user*/*/oms.db"):
            try:
                uid, mode = int(p.parent.parent.name[4:]), p.parent.name
            except ValueError:
                continue
            if mode in MODES and oms_has_open(p):
                out.add((uid, mode))
        for c in db.scalars(select(LiveCommand).where(LiveCommand.status == "queued")):
            out.add((c.user_id, c.mode))
        return out

    # ------------------------------------------------------------------ one pass
    def tick(self, now: datetime | None = None, force_minute: bool = False) -> None:
        now = now or self.ctx.clock.now_ist()
        d = now.date()
        with self.ctx.sessions() as db:
            ot = db.get(ServiceStatus, "ordertest")
            # the 1-lot order test is running on this user's account: their live desk waits (one writer per account);
            # everyone else, shadow, and expiring old commands carry on
            paused_uid = (ot.data or {}).get("user_id") if ot and (self.ctx.clock.now() - ot.updated_at).total_seconds() < 30 else None
            if ot and paused_uid is None and (self.ctx.clock.now() - ot.updated_at).total_seconds() < 30:
                paused_uid = -1                            # older test without a user: pause every live desk
            self.paused_uid = paused_uid
            self._expire_commands(db, d)
            runs = db.scalars(select(LiveRun).where(LiveRun.status != "stopped")).all()
            needed = self._needed(db)
            keys = {(r.user_id, r.mode) for r in runs} | set(self.desks) | needed
            ready = {}
            for uid, mode in sorted(keys):                 # 1) every user's commands first, committed on their own
                if mode == "live" and paused_uid in (uid, -1):
                    self._status(db, uid, mode, None, "Paused: the 1-lot order test is running on this account.")
                    continue
                try:
                    desk = self._prepare(db, uid, mode, (uid, mode) in needed)
                    if desk is not None:
                        ready[(uid, mode)] = desk
                except Exception as e:  # noqa: BLE001
                    log.error("prepare u%s %s: %s", uid, mode, type(e).__name__)
                    db.rollback()
            db.commit()
            for (uid, mode), desk in ready.items():       # 2) management, one savepoint per user and mode
                sp = db.begin_nested()                     # one user's database error never poisons the others
                seen = set(self.alerted)
                try:
                    self._manage(db, desk, uid, mode, d, now)
                    sp.commit()
                except Exception as e:  # noqa: BLE001 - one user's failure never stops another user's desk
                    sp.rollback()
                    self.alerted = seen                    # rolled-back alerts may be raised again
                    log.error("desk u%s %s: %s", uid, mode, type(e).__name__)
                    try:
                        self._alert_once(db, uid, f"desk-error-{mode}", "critical", "Live desk problem",
                                         f"The {mode} desk hit an error ({type(e).__name__}) and keeps retrying. "
                                         "Stops at Zerodha keep working; check the Orders tab.")
                    except Exception:  # noqa: BLE001
                        log.error("alert failed for u%s", uid)
            minute = minute_index(now)
            if (force_minute or now.second >= READ_AFTER_S) and self.last_minute != (d, minute):
                self.last_minute = (d, minute)
                self._minute(db, runs, d, now, minute)
            for (uid, mode), (desk, _) in list(self.desks.items()):
                if mode == "live" and paused_uid in (uid, -1):
                    continue
                try:
                    self._snapshot(db, uid, mode, desk, runs)
                except Exception as e:  # noqa: BLE001
                    log.error("snapshot u%s %s: %s", uid, mode, type(e).__name__)
            db.commit()

    def _prepare(self, db, uid: int, mode: str, needed: bool) -> Desk | None:
        desk, why = self.desk(db, uid, mode, needed)
        self._status(db, uid, mode, desk, why)
        if desk is None:
            if needed and "login" in why:
                self._alert_once(db, uid, f"no-login-{mode}", "critical", "Open positions without a Zerodha login",
                                 f"{mode.upper()} legs are open but there is no Zerodha login today: they are not managed "
                                 "and carried legs have no stop until you log in. Log in now (More › Zerodha).")
            if db.scalar(select(LiveCommand.id).where(LiveCommand.user_id == uid, LiveCommand.mode == mode,
                                                      LiveCommand.status == "queued").limit(1)):
                self._alert_once(db, uid, f"no-desk-{mode}", "critical", "Command can't be carried out",
                                 f"{why} Close positions in Kite if needed; stops at Zerodha keep working.")
            return None
        try:                                               # commands first: Stop all never waits for reconciliation
            self._commands(db, desk, uid, mode)
        except Exception as e:  # noqa: BLE001
            log.error("commands u%s %s: %s", uid, mode, type(e).__name__)
        return desk

    def _manage(self, db, desk: Desk, uid: int, mode: str, d: date, now: datetime) -> None:
        try:
            if self.ctx.calendar.is_trading_day(d):
                desk.ensure_started(d)
        except Exception as e:  # noqa: BLE001
            self._alert_once(db, uid, f"start-{mode}", "critical", "Live desk not reconciled",
                             f"Start of day failed ({type(e).__name__}); no new entries. Retrying.")
        open_now = self._market_open()
        ts = self.ctx.clock.now().timestamp()
        if open_now and ts - self.last_prices.get((uid, mode), 0) >= PRICES_EVERY_S:
            self.last_prices[(uid, mode)] = ts
            g = self.gates.get(uid)
            if g:
                watch: dict[str, set[str]] = {}
                for (u, _m), (dk, _) in self.desks.items():
                    if u == uid:
                        for lg in dk.om._legs("status IN ('open','exiting','entering')"):
                            watch.setdefault(exchange_of(lg["symbol"]), set()).add(lg["symbol"])
                g[0].set_watch(watch)
            try:
                desk.refresh_prices(exchange_of)
            except Exception as e:  # noqa: BLE001
                log.error("prices u%s %s: %s", uid, mode, type(e).__name__)
        mtm = desk.mtm_today(charges_fn(d))
        if mtm is None and desk.has_open() and open_now:
            desk.mtm_unknown_since = desk.mtm_unknown_since or ts
            if ts - desk.mtm_unknown_since >= MTM_UNKNOWN_ALERT_S:
                self._alert_once(db, uid, f"mtm-unknown-{mode}", "critical", "Daily-loss check paused",
                                 "No price from Zerodha for an open leg for 30 s: the day-loss limit can't be checked. "
                                 "Stops at Zerodha keep working.")
        else:
            desk.mtm_unknown_since = None
        lim = account_limits(db, uid) or {}
        for sym in {lg["symbol"] for lg in desk.om._legs("status IN ('open','entering','exiting')")}:
            und = underlying_of(sym)
            if und:
                self._contracts(und, d)                     # expiries of every open leg, even with no session running
            if open_now and now.strftime("%H:%M") >= "09:30" and self._expiry_of(sym) is None:
                self._alert_once(db, uid, f"expiry-unknown-{sym}", "critical", "Expiry of an open leg unknown",
                                 f"{sym}: today's Zerodha instruments list is missing, so the expiry-day exit can't run "
                                 "for it. Check the recorder; exit by hand on its expiry day if needed.")
        if open_now:
            desk.expiry_exits(d, now.strftime("%H:%M"), lim.get("expiry_cutoff", "15:15"), self._expiry_of)
            self._intraday_exits(db, desk, uid, mode, d, now)
        try:
            desk.poll(mtm)
        except Exception as e:  # noqa: BLE001 - e.g. no login outside market hours: alert only while open
            log.error("poll u%s %s: %s", uid, mode, type(e).__name__)
            if open_now:
                self._alert_once(db, uid, f"poll-{mode}", "critical", "Live desk can't reach Zerodha",
                                 f"{type(e).__name__} while managing {mode} orders; retrying every 2 s. "
                                 "Stops at Zerodha keep working.")

    def _intraday_exits(self, db, desk: Desk, uid: int, mode: str, d: date, now: datetime) -> None:
        """Sessions that don't hold overnight: every leg exits by the strategy's exit time (15:20 at the latest),
        whatever the engine says (an engine error or an unfilled exit must never carry a leg past the close, when
        Zerodha expires the DAY stop). Anything still open 10 minutes before the F&O close raises a critical alert."""
        from datetime import time as dtime
        t_now = now.time().replace(tzinfo=None, microsecond=0)
        close = self.ctx.calendar.session(d).fo_close
        cap = (datetime.combine(d, close) - timedelta(minutes=20)).time()       # 15:20 with a 15:40 F&O close
        late = (datetime.combine(d, close) - timedelta(minutes=10)).time()
        specs: dict[str, dict] = {}
        for lg in desk.om._legs("status IN ('open','entering','exiting')"):
            try:
                sid = lg["session"]
                if sid not in specs:
                    r = db.get(LiveRun, int(sid)) if str(sid).isdigit() else None
                    specs[sid] = (r.spec if r else {}) or {}
                sch = specs[sid].get("schedule") or {}
                if mode == "shadow" and sch.get("hold_overnight"):
                    continue                               # live never holds overnight (refused everywhere)
                try:
                    exit_by = min(dtime.fromisoformat(str(sch.get("exit_by", "15:20")).lstrip("T")), cap)
                except ValueError:
                    exit_by = cap
                if t_now >= exit_by and lg["status"] in ("open", "entering"):
                    desk.om.exit_leg(lg["id"], "exit_by")
                if late <= t_now < close:
                    where = ("Its stop at Zerodha expires at the close: close it in Kite now." if mode == "live"
                             else "Shadow only (nothing at Zerodha): the simulated leg is being closed.")
                    self._alert_once(db, uid, f"carry-{mode}-{lg['id']}", "critical" if mode == "live" else "important",
                                     "Intraday leg still open near the close",
                                     f"{lg['symbol']} ({mode}) should have closed by {exit_by:%H:%M} and is still "
                                     f"{lg['status']}. {where}")
            except Exception as e:  # noqa: BLE001 - one leg never stops the others or the poll
                log.error("intraday exit u%s %s leg %s: %s", uid, mode, lg["id"], type(e).__name__)

    def _expire_commands(self, db, d: date) -> None:
        """Commands from an earlier day are not carried out today (a kill is alerted instead)."""
        start = datetime.combine(d, datetime.min.time()).replace(tzinfo=self.ctx.clock.now_ist().tzinfo)
        for c in db.scalars(select(LiveCommand).where(LiveCommand.status == "queued", LiveCommand.created_at < start)):
            c.status, c.result, c.done_at = "failed", "expired: not carried out on its day", self.ctx.clock.now()
            if c.kind == "kill":
                raise_alert(db, self.ctx, c.user_id, "critical", "live", "Stop all from an earlier day was not carried out",
                            "Check your positions in Kite now.", path="/trade/live")

    def _minute(self, db, runs, d: date, now: datetime, minute: int) -> None:
        cal = self.ctx.calendar
        if not cal.is_trading_day(d):
            return
        sess = cal.session(d)
        close_min = minute_index(datetime.combine(d, sess.fo_close))
        booked_by = datetime.combine(d, sess.fo_close) + BOOK_AFTER
        days = Days(self.ctx)
        for r in runs:
            if d < r.first_day:
                continue
            try:
                e = self._engine(r)
                desk = self.desks.get((r.user_id, r.mode), (None, None))[0]
                if r.mode == "live" and getattr(self, "paused_uid", None) in (r.user_id, -1):
                    desk = None                             # the order test owns this account right now: no orders
                if now.replace(tzinfo=None) >= booked_by:
                    if r.last_day is None or r.last_day < d:
                        if not e.skip(d):
                            e.close_day(days.load(r.underlying, d), {}, r.start_minute if d == r.first_day else 0)
                        r.last_day = d
                        # it counts only when the desk ran that day from (about) the strategy's own start
                        ran = desk is not None and desk.started_day == str(d) and (d > r.first_day or r.start_minute <= 15)
                        if ran:                            # a day counts only when the desk really ran it
                            r.state = {**(r.state or {}), "days": int((r.state or {}).get("days", 0)) + 1}
                    continue
                if minute <= 0 or e.skip(d):
                    continue
                if minute > N_MIN:
                    if desk is not None and e.today is not None and minute <= close_min:
                        desk._follow_engine(r, e.today)     # engine exits in the last minute are still followed
                    continue
                e.step(days.load(r.underlying, d), minute, {}, r.start_minute if d == r.first_day else 0)
                db.refresh(r)                               # a disarm or Stop all in this tick wins over an entry
                overnight = bool((r.spec.get("schedule") or {}).get("hold_overnight"))
                armed = r.status == "running" and (
                    r.mode == "shadow" or (r.armed_day == d and enabled(self.ctx) and not overnight))
                if desk is not None:
                    desk.bridge(r, e.today, d, minute, self._contracts(r.underlying, d), armed)
                st = e.state(d, {}, until=minute)
                new = {**(r.state or {}), "engine": {k: st[k] for k in ("day_pnl", "positions", "as_of", "cum_net")}}
                new.pop("error", None)
                r.state, r.updated_at = new, self.ctx.clock.now()
            except Exception as ex:  # noqa: BLE001
                log.error("live session %s: %s: %s", r.id, type(ex).__name__, ex)
                r.state = {**(r.state or {}), "error": f"{type(ex).__name__}: {ex}"[:300]}
                desk = self.desks.get((r.user_id, r.mode), (None, None))[0]
                if desk is not None and desk.om._legs("session=? AND status IN ('open','exiting','entering')", (str(r.id),)):
                    self._alert_once(db, r.user_id, f"engine-{r.id}", "critical", f"{r.name}: strategy stopped deciding",
                                     f"Its engine hit an error ({type(ex).__name__}) with legs open. They keep their stops "
                                     "at Zerodha but no strategy exit will follow: exit by hand if needed.")

    def _engine(self, r: LiveRun) -> PaperEngine:
        e = self.engines.get(r.id)
        if e is None:
            base = Path(self.ctx.settings.runs_dir or (Path(self.ctx.settings.store_root or ".") / "runs")).expanduser()
            e = PaperEngine(base / "live" / "engines" / str(r.id), r.spec)
            self.engines[r.id] = e
        return e

    def _commands(self, db, desk: Desk, uid: int, mode: str) -> None:
        for c in db.scalars(select(LiveCommand).where(LiveCommand.user_id == uid, LiveCommand.mode == mode,
                                                      LiveCommand.status == "queued").order_by(LiveCommand.created_at)):
            try:
                c.result = desk.run_command(c.kind, c.payload or {}, c.id, lambda: self.disarm_all(uid, mode))[:300]
                c.status, c.done_at = "done", self.ctx.clock.now()
            except Exception as e:  # noqa: BLE001
                c.tries += 1
                c.result = f"{type(e).__name__}"[:300]
                if c.tries >= COMMAND_TRIES:
                    c.status = "failed"
                    raise_alert(db, self.ctx, uid, "critical", "live", "Command from the app not done",
                                f"{c.kind}: {type(e).__name__}. Check Zerodha and the Orders tab.", path="/trade/live")
            db.flush()

    def _status(self, db, uid: int, mode: str, desk: Desk | None, why: str) -> None:
        name = f"live:{uid}:{mode}"
        data = {"why": why, "desk": desk is not None, "ready": bool(desk and desk.om.ready),
                "broker_ok": bool(desk and desk.om.broker_ok), "locked": desk.om.locked() if desk else None,
                "closing_only": bool(desk and desk.closing_only),
                "enabled": enabled(self.ctx) if mode == "live" else None}
        row = db.get(ServiceStatus, name)
        if row is None:
            db.add(ServiceStatus(name=name, updated_at=self.ctx.clock.now(), data=data))
        else:
            row.updated_at, row.data = self.ctx.clock.now(), {**(row.data or {}), **data}

    def _snapshot(self, db, uid: int, mode: str, desk: Desk, runs) -> None:
        snap = desk.snapshot()
        row = db.get(ServiceStatus, f"live:{uid}:{mode}")
        if row is not None:
            row.data = {**(row.data or {}), "snapshot": snap, "mtm": desk.mtm_today(charges_fn(self.ctx.clock.now_ist().date()))}
            row.updated_at = self.ctx.clock.now()
        for r in runs:
            if r.user_id == uid and r.mode == mode:
                legs = [l for l in snap["legs"] if l["session"] == str(r.id)]
                r.state = {**(r.state or {}), "legs": legs}


def main() -> None:
    from ..context import build_context, quiet_http_logs
    from ..settings import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    quiet_http_logs()
    ctx = build_context(get_settings())
    runner = LiveRunner(ctx)
    with ctx.sessions() as db:
        raise_alert_once_on_boot(ctx, db)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    while not stop["now"]:
        t0 = _time.time()
        try:
            runner.tick()
            with ctx.sessions() as db:
                row = db.get(ServiceStatus, "live-runner")
                if row is None:
                    db.add(ServiceStatus(name="live-runner", updated_at=ctx.clock.now(), data={}))
                else:
                    row.updated_at = ctx.clock.now()
                db.commit()
        except Exception as e:  # noqa: BLE001
            log.error("live pass failed: %s", type(e).__name__)
        _time.sleep(max(0.2, POLL_S - (_time.time() - t0)))


def raise_alert_once_on_boot(ctx: AppContext, db) -> None:
    """A start in market hours means the runner restarted while positions may be open: say so."""
    now = ctx.clock.now_ist()
    if ctx.calendar.is_trading_day(now.date()) and "09:15" <= now.strftime("%H:%M") < "15:40":
        for uid in {a.user_id for a in db.scalars(select(BrokerAccount))}:
            raise_alert(db, ctx, uid, "important", "live", "Live runner restarted in market hours",
                        "It reconciles with Zerodha before any new order. Stops at Zerodha kept working.", path="/trade/live")
        db.commit()
