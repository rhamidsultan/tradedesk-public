"""The supervised 1-lot order test: `python -m tdserver.cli live-ordertest` on the server, in market hours,
with the owner watching. It is the gate before live trading is switched on.

Step A (cannot fill): BUY LIMIT 1 lot of a far-OTM NIFTY call (an expiry after today, bid at least ₹1) at ₹0.05,
        modify to ₹0.10, cancel; it must end CANCELLED with our tag and nothing filled.
Step B (real, small): BUY 1 lot of a ₹3–15 call at the ask through the OrderManager -> protective SL at Zerodha
        (trigger -40 %), seen working with our tag -> the SL becomes the LIMIT exit -> the account is flat with no
        working orders. Cost: the spread plus charges (typically ₹50–150).

Safety: the account must be flat with no working F&O orders before each step; each step runs inside try/finally,
so a broker error or Ctrl-C cancels Step A's order and keeps exiting Step B's leg until flat (and says exactly what
to close by hand if it can't). While it runs it writes an "ordertest" heartbeat the live runner waits for.
It never switches live on by itself: on a pass it prints the root command that creates the switch file.
"""
from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from ..models import ServiceStatus, User

EXIT_DRIVE_S = 600          # how long the cleanup keeps exiting before handing over to the owner
MAX_SPREAD = 0.25


class Stop(Exception):
    """End the test with a message (the cleanup in finally still runs)."""


def run(ctx, adapter=None, ask: Callable[[str], bool] | None = None, out=print, sleep=time.sleep,
        user_id: int | None = None, drive_s: float = EXIT_DRIVE_S, monotonic=time.monotonic) -> int:
    """Signal handlers (an SSH drop or a stop becomes an interrupt the cleanup handles) and a background heartbeat
    for the whole run, both undone at the end. Every line also goes to a log file that survives a lost terminal."""
    import signal
    import threading

    base = Path(ctx.settings.runs_dir or (Path(ctx.settings.store_root or ".") / "runs")).expanduser() / "live" / "ordertest"
    base.mkdir(parents=True, exist_ok=True)
    logf = base / f"{ctx.clock.now_ist():%Y%m%d-%H%M%S}.log"

    def safe_out(line: str) -> None:
        try:
            with open(logf, "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        except OSError:
            pass
        try:
            out(line)
        except OSError:                          # the terminal is gone (SSH dropped): keep going, the log has it
            pass

    def as_interrupt(*_):
        raise KeyboardInterrupt
    old = {signal.SIGINT: signal.getsignal(signal.SIGINT)}    # the cleanup ignores Ctrl-C; restored at the end
    for sig in (signal.SIGHUP, signal.SIGTERM):
        try:
            old[sig] = signal.signal(sig, as_interrupt)
        except (ValueError, OSError):
            pass
    stop_beat = threading.Event()
    try:
        safe_out(f"(log: {logf})")
        return _run(ctx, adapter, ask, safe_out, sleep, user_id, drive_s, monotonic, stop_beat)
    finally:
        stop_beat.set()
        for sig, h in old.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError):
                pass


def _run(ctx, adapter, ask, out, sleep, user_id, drive_s, monotonic, stop_beat) -> int:
    from tradedesk_oms import LegReq, Limits, OrderManager
    from tradedesk_oms.broker import TERMINAL, BrokerError, OrderReq
    from tradedesk_oms.zerodha import ZerodhaAdapter

    from ..kite import access_token, account, secrets_for
    from .runner import contracts, enabled_file, lot_check

    ask = ask or (lambda q: input(q + " Type YES to continue: ").strip() == "YES")

    test_user = {"id": user_id}

    def heartbeat():
        """Tells the live runner to leave this user's account alone while the test runs. Never fatal."""
        try:
            with ctx.sessions() as db:
                row = db.get(ServiceStatus, "ordertest")
                data = {"user_id": test_user["id"]}
                if row is None:
                    db.add(ServiceStatus(name="ordertest", updated_at=ctx.clock.now(), data=data))
                else:
                    row.updated_at, row.data = ctx.clock.now(), data
                db.commit()
        except Exception as e:  # noqa: BLE001
            out(f"(heartbeat not written: {type(e).__name__})")

    def stop_all_asked() -> bool:
        """A Stop all from the app during the test ends it (cleanup still closes the position)."""
        try:
            from ..models import LiveCommand
            with ctx.sessions() as db:
                return db.scalar(select(LiveCommand.id).where(LiveCommand.user_id == test_user["id"],
                                                              LiveCommand.kind == "kill",
                                                              LiveCommand.status == "queued").limit(1)) is not None
        except Exception:  # noqa: BLE001
            return False

    def in_hours() -> bool:
        n = ctx.clock.now_ist()
        return ctx.calendar.is_trading_day(n.date()) and "09:20" <= n.strftime("%H:%M") < "15:00"

    now = ctx.clock.now_ist()
    with ctx.sessions() as db:
        hb = db.get(ServiceStatus, "live-runner")
        if hb and (ctx.clock.now() - hb.updated_at).total_seconds() < 30:
            out("The live runner is running. Stop it first: sudo systemctl stop td2-live")
            return 2
        uid = user_id or db.scalar(select(User.id).where(User.role == "owner").order_by(User.id))
        test_user["id"] = uid
    import threading

    def beat():                                  # also while waiting at the prompts: the runner keeps off this account
        while not stop_beat.wait(10):
            heartbeat()
    threading.Thread(target=beat, daemon=True).start()
    with ctx.sessions() as db:
        acc = account(db, uid) if uid else None
        tok = access_token(ctx, acc) if acc else None
        if not tok:
            out("No Zerodha login today for the owner. Log in from the app (More › Zerodha) first.")
            return 2
        api_key, _ = secrets_for(ctx, acc)
    if not in_hours():
        out("Run this on a trading day between 09:20 and 15:00 IST.")
        return 2
    b = adapter or ZerodhaAdapter(api_key, tok)

    def flat_and_idle() -> str | None:
        """None when the account has no F&O position and no working F&O order; else what is there."""
        pos = b.positions()                                   # F&O only (NFO/BFO) by the adapter
        working = [o for o in b.orders() if o.status not in TERMINAL and o.symbol and not o.symbol.endswith("-EQ")
                   and (o.symbol.endswith("CE") or o.symbol.endswith("PE") or o.symbol.endswith("FUT"))]
        if pos or working:
            return f"positions {pos or '{}'}, working orders {[(o.symbol, o.side, o.status) for o in working]}"
        return None

    heartbeat()
    busy = flat_and_idle()
    if busy:
        out(f"The account must be flat with no working F&O orders: {busy}. Stopping.")
        return 1
    spot = b.quote(["NIFTY 50"], "NSE").get("NIFTY 50")
    if spot is None or not spot.ltp:
        out("No NIFTY price from Zerodha right now. Try again in a minute.")
        return 2
    ins = contracts(Path(ctx.settings.store_root), now.date(), "NIFTY")
    if not ins:
        out("Today's instruments list is not in the store yet (the recorder downloads it before the open).")
        return 2

    def strike(sym: str) -> float:              # store symbol: NIFTY + yymmdd + strike + CE/PE (see store_symbol)
        try:
            return float(sym[len("NIFTY") + 6:-2])
        except ValueError:
            return 0.0

    calls = sorted(((ks, lot, exp, strike(sym)) for sym, (ks, ex, lot, exp, und) in ins.items()
                    if sym.endswith("CE") and exp > now.date()), key=lambda x: (x[2], x[3]))
    if not calls:
        out("No NIFTY calls expiring after today in today's list.")
        return 2
    exps = sorted({c[2] for c in calls})[:2]    # never today's expiry: far-OTM calls there trade at ₹0.05
    exp = exps[0]
    week = [c for c in calls if c[2] in exps]   # the next two expiries: a pick exists on any weekday
    bad_lot = lot_check("NIFTY", exp, week[0][1])
    if bad_lot:
        out(f"Reference data and Zerodha disagree: {bad_lot}. Fix the reference data first.")
        return 1

    # ------------------------------------------------------------------ Step A
    far_c = [c for c in week if c[3] >= spot.ltp * 1.03][:40]
    qa = b.quote([c[0] for c in far_c], "NFO") if far_c else {}
    far = next((c for c in far_c if qa.get(c[0]) and (qa[c[0]].bid or 0) >= 1.0), None)
    if far is None:
        out("No far-OTM call with a bid of at least ₹1 (so ₹0.05 cannot fill). Try later.")
        return 1
    out(f"NIFTY {spot.ltp}. Step A uses {far[0]} (lot {far[1]}, bid ₹{qa[far[0]].bid}) at ₹0.05: it cannot fill.")
    if not ask("Step A: place, modify and cancel a non-filling order?"):
        return 1
    tag = f"TDTEST{int(time.time()) % 10**8}"
    oid = None
    found = None
    modified = False
    try:
        oid = b.place(OrderReq(tag, far[0], "NFO", "BUY", far[1], "LIMIT", 0.05))
        out(f"placed {oid}"); sleep(1)
        b.modify(oid, price=0.10); out("modified"); sleep(1)
        modified = True
    except BrokerError as e:
        out(f"Step A broker error ({type(e).__name__}): cancelling whatever reached Zerodha")
    finally:
        for i in range(8):                      # always cancel (also after an error or Ctrl-C), then read the state
            try:
                found = next((o for o in b.orders() if (oid and o.order_id == oid) or o.tag == tag), None)
                if found is None and oid is None and i < 5:
                    sleep(1)                    # a lost reply: the order may show up in the book a moment later
                    continue
                if found and found.status not in TERMINAL:
                    b.cancel(found.order_id)
                    sleep(1)
                    continue
                break
            except BrokerError as e:
                out(f"cancel/read failed ({type(e).__name__}); retrying")
                sleep(2)
    st = found
    out(f"final status: {st.status if st else '?'} tag: {st.tag if st else '?'} filled: {st.filled if st else '?'}")
    if st is None or st.status != "CANCELLED" or st.tag != tag or st.filled or not modified:
        out("Step A did not end with the order modified, then CANCELLED with our tag and nothing filled. Stop here; "
            f"in Kite cancel any order tagged {tag} and close any {far[0]} position. Send this output.")
        return 1
    busy = flat_and_idle()
    if busy:
        out(f"After Step A the account is not flat: {busy}. Close it in Kite. Stopping.")
        return 1
    out("Step A passed: orders from this server work.\n")

    # ------------------------------------------------------------------ Step B
    cheap = [c for c in week if c[3] >= spot.ltp * 1.01][:40]
    if not ask("Step B places REAL orders (1 lot of a ₹3–15 call) and costs the spread plus charges."):
        return 1
    heartbeat()
    if not in_hours():
        out("Outside 09:20–15:00 now. Stopping.")
        return 2
    busy = flat_and_idle()
    if busy:
        out(f"The account must be flat with no working F&O orders: {busy}. Stopping.")
        return 1
    qs = b.quote([c[0] for c in cheap], "NFO") if cheap else {}      # fresh prices after the confirmation

    def ok_price(c) -> bool:
        q = qs.get(c[0])
        if not q or not q.ask or not q.bid or not 3 <= q.ask <= 15:
            return False
        return (q.ask - q.bid) / ((q.ask + q.bid) / 2) <= MAX_SPREAD

    pick = next((c for c in cheap if ok_price(c)), None)
    if pick is None:
        out("No call between ₹3 and ₹15 with a tight spread right now; run again later.")
        return 1
    q = qs[pick[0]]
    out(f"Step B: BUY 1 lot {pick[0]} × {pick[1]} at ₹{q.ask} (≈ ₹{q.ask * pick[1]:,.0f}), stop at Zerodha, then exit.")
    base = Path(ctx.settings.runs_dir or (Path(ctx.settings.store_root or ".") / "runs")).expanduser() / "live" / "ordertest"
    base.mkdir(parents=True, exist_ok=True)
    om = OrderManager(b, base / f"{ctx.clock.now_ist():%Y%m%d-%H%M%S}-{tag}.db",      # a fresh book for every run
                      Limits(max_lots=1, max_orders_day=2, daily_loss=2000, entry_window=("09:16", "15:20")),
                      log=out, alert=lambda *a: out(f"ALERT {a}"), sleep=sleep,
                      clock=lambda: ctx.clock.now().timestamp())
    try:
        started = om.start()
    except BrokerError as e:
        started = False
        out(f"Zerodha did not answer ({type(e).__name__}).")
    if not started:
        out("Could not reconcile with Zerodha before Step B (nothing was bought). Stopping.")
        return 1
    om.armed = True
    leg = None
    passed = False
    sl_seen = False
    from tradedesk_oms.oms import GuardBlocked
    try:
        # MIS: if everything else failed, Zerodha squares the test position off itself near the close
        leg = om.enter("ordertest", [LegReq(pick[0], "NFO", "BUY", 1, pick[1], q.ask, round(q.ask * 0.6, 1),
                                            product="MIS")])[0]
        for _ in range(30):
            heartbeat()
            if stop_all_asked():
                raise Stop("Stop all from the app: ending the test.")
            try:
                om.poll()
            except BrokerError as e:
                out(f"order book read failed ({type(e).__name__}); retrying")
            sleep(1)
            lg = om._leg(leg)
            o = om._working(lg["sl_tag"])
            if lg["status"] == "open" and o is not None and o.status == "TRIGGER PENDING" and o.tag == lg["sl_tag"]:
                sl_seen = True
                break
        lg = om._leg(leg)
        out(f"leg: {lg['status']} filled {lg['filled']} @ {lg['entry_avg']} | stop order working at Zerodha: {sl_seen}")
        if not sl_seen:
            raise Stop("Entry did not fill with a working stop at Zerodha within 30 s.")
        if stop_all_asked():
            raise Stop("Stop all from the app: ending the test.")
        sleep(3)
        om.poll()
        lg = om._leg(leg)
        o = om._working(lg["sl_tag"])
        it = om._intent(lg["sl_tag"])
        if o is None or o.status != "TRIGGER PENDING" or (it and it["bstatus"] == "REJECTED"):
            raise Stop("The stop order did not stay working at Zerodha (rejected or changed).")
        om.exit_leg(leg, "ordertest")
        passed = True
    except Stop as e:
        out(str(e))
    except GuardBlocked as e:
        out(f"The order guards refused the entry (nothing was sent): {e}")
    except BrokerError as e:
        out(f"Broker error during Step B ({type(e).__name__}): closing the test position…")
    except KeyboardInterrupt:
        out("Interrupted: closing the test position before stopping…")
    finally:
        # keep exiting until the leg is closed and the account flat with nothing working, or hand over to the owner
        import signal as _sig
        for sg in (_sig.SIGINT, _sig.SIGHUP, _sig.SIGTERM):  # nothing interrupts the cleanup (drive_s bounds it)
            try:
                _sig.signal(sg, _sig.SIG_IGN)
            except (ValueError, OSError):
                pass
        legs = om._legs("session='ordertest'")            # also a leg whose entry was interrupted mid-request
        leg = legs[0]["id"] if legs else leg
        if leg is not None:
            t0 = monotonic()
            while True:
                try:                                       # nothing here may end the cleanup early
                    heartbeat()
                    lg = om._leg(leg)
                    if lg["status"] not in ("closed", "failed", "exiting"):
                        om.exit_leg(leg, "ordertest_cleanup")
                    om.poll()
                    if om._leg(leg)["status"] in ("closed", "failed") and flat_and_idle() is None:
                        break
                    if monotonic() - t0 > drive_s:
                        tags = [r[0] for r in om.db.execute("SELECT tag FROM intents")]
                        out(f"NOT FLAT after {int(drive_s)} s: CLOSE {pick[0]} IN KITE NOW and cancel the orders "
                            f"tagged {', '.join(tags)}. Until it is closed, the live desk will lock on the mismatch. "
                            "Send this output.")
                        return 1
                    sleep(1)
                except BrokerError as e:
                    out(f"broker error while exiting ({type(e).__name__}); retrying")
                except KeyboardInterrupt:
                    out("Still closing the test position: wait until it says flat.")
                except OSError:
                    pass                                   # terminal gone: keep closing; the log file has the output
    if leg is None:
        out("Test stopped before anything was sent.")
        return 1
    lg = om._leg(leg)
    conv = bool(lg["exit_tag"]) and om._intent(lg["exit_tag"])["kind"] == "sl"
    out(f"leg: {lg['status']} exit @ {lg['exit_avg']} | stop order became the exit: {'yes' if conv else 'no (cancel + limit used)'}")
    if passed and lg["status"] == "closed" and flat_and_idle() is None:
        f = shlex.quote(str(enabled_file(ctx)))
        d = shlex.quote(str(enabled_file(ctx).parent))
        out("\nPASSED. To switch live on, run on the server (root):")
        out(f"  sudo mkdir -p {d} && sudo sh -c 'date > {f}' && sudo chmod 644 {f}")
        out("then start the runner again: sudo systemctl start td2-live")
        return 0
    out("Test not passed (the test position is closed). Send this output.")
    return 1
