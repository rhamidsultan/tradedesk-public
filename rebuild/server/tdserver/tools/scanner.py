"""Scanner: built-in setups checked on the latest recorded bars of every index and stock in the store.

Each setup looks only at closed bars (the bar must have ended by the scan minute). Indicators match the engine's
definitions (EMA seeded by the first close, Wilder RSI and ATR, supertrend bands), so a hit here is the same signal
a strategy built from the same blocks would see.

Setups
  orb          15-minute opening range broken today (close above its high / below its low)
  pdh_pdl      close above the previous day's high / below its low
  ema_cross    EMA 9 crossed EMA 21 on 5-minute bars within the last 3 bars
  rsi_extreme  RSI 14 on 5-minute bars above 70 or below 30
  st_flip      supertrend (10, 3) on 15-minute bars changed direction within the last 2 bars
  nr7          yesterday's range was the narrowest of the last 7 days (a squeeze before a move)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

SETUPS = {
    "orb": "Opening range breakout (15 min)",
    "pdh_pdl": "Previous day high / low break",
    "ema_cross": "EMA 9 / 21 cross (5 min)",
    "rsi_extreme": "RSI 14 above 70 or below 30 (5 min)",
    "st_flip": "Supertrend 10,3 flip (15 min)",
    "nr7": "NR7: narrowest day of the last 7",
}


@dataclass
class B:
    ts: datetime
    o: float
    h: float
    l: float
    c: float


def resample(bars: list[B], minutes: int) -> list[B]:
    """1-minute bars -> `minutes` bars aligned to 09:15, per day (the last bar of a day may be partial)."""
    out: list[B] = []
    key = None
    for b in bars:
        m = (b.ts.hour * 60 + b.ts.minute) - (9 * 60 + 15)
        k = (b.ts.date(), m // minutes)
        if k != key:
            out.append(B(b.ts, b.o, b.h, b.l, b.c))
            key = k
        else:
            x = out[-1]
            x.h, x.l, x.c = max(x.h, b.h), min(x.l, b.l), b.c
    return out


def ema(xs: list[float], n: int) -> list[float | None]:
    k, v, out = 2 / (n + 1), None, []
    for i, x in enumerate(xs):
        v = x if v is None else k * x + (1 - k) * v
        out.append(v if i + 1 >= n else None)
    return out


def rsi(xs: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None]
    ag = al = 0.0
    for i in range(1, len(xs)):
        ch = xs[i] - xs[i - 1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            ag += g / n
            al += l / n
        else:
            ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
        out.append((100.0 if al == 0 else 100 - 100 / (1 + ag / al)) if i >= n else None)
    return out


def supertrend_dir(bars: list[B], n: int = 10, k: float = 3.0) -> list[int | None]:
    out: list[int | None] = []
    prev = None
    atr = 0.0
    ub = lb = None
    d = 1
    for i, b in enumerate(bars):
        tr = b.h - b.l if prev is None else max(b.h - b.l, abs(b.h - prev), abs(b.l - prev))
        cnt = i + 1
        atr = tr if cnt == 1 else (atr * (n - 1) + tr) / n if cnt > n else (atr * (cnt - 1) + tr) / cnt
        if cnt < n:
            prev = b.c
            out.append(None)
            continue
        mid = (b.h + b.l) / 2
        bub, blb = mid + k * atr, mid - k * atr
        nub = bub if ub is None or bub < ub or prev > ub else ub
        nlb = blb if lb is None or blb > lb or prev < lb else lb
        if ub is not None:
            if d == 1 and b.c < nlb:
                d = -1
            elif d == -1 and b.c > nub:
                d = 1
        ub, lb, prev = nub, nlb, b.c
        out.append(d)
    return out


def _days(bars: list[B]) -> list[tuple[date, float, float, float, float]]:
    """(day, open, high, low, close) per day."""
    out: dict[date, list[float]] = {}
    for b in bars:
        x = out.get(b.ts.date())
        if x is None:
            out[b.ts.date()] = [b.o, b.h, b.l, b.c]
        else:
            x[1], x[2], x[3] = max(x[1], b.h), min(x[2], b.l), b.c
    return [(d, *v) for d, v in sorted(out.items())]


def scan_one(bars: list[B], setups: list[str] | None = None) -> list[dict]:
    """Hits for one symbol: [{setup, direction (long|short), detail}] on its latest day."""
    if not bars:
        return []
    want = set(setups or SETUPS)
    hits: list[dict] = []
    last = bars[-1]
    today = last.ts.date()
    tb = [b for b in bars if b.ts.date() == today]
    days = _days(bars)
    prev = days[-2] if len(days) >= 2 and days[-1][0] == today else None

    def hit(s, direction, detail):
        hits.append({"setup": s, "name": SETUPS[s], "direction": direction, "detail": detail})

    if "orb" in want and len(tb) > 15:
        orh, orl = max(b.h for b in tb[:15]), min(b.l for b in tb[:15])
        if last.c > orh:
            hit("orb", "long", f"Close {last.c:,.2f} above the opening range high {orh:,.2f}")
        elif last.c < orl:
            hit("orb", "short", f"Close {last.c:,.2f} below the opening range low {orl:,.2f}")
    if "pdh_pdl" in want and prev:
        if last.c > prev[2]:
            hit("pdh_pdl", "long", f"Close {last.c:,.2f} above yesterday's high {prev[2]:,.2f}")
        elif last.c < prev[3]:
            hit("pdh_pdl", "short", f"Close {last.c:,.2f} below yesterday's low {prev[3]:,.2f}")
    b5 = resample(bars, 5)
    c5 = [b.c for b in b5]
    if "ema_cross" in want and len(c5) > 25:
        f, s = ema(c5, 9), ema(c5, 21)
        for i in range(len(c5) - 1, max(len(c5) - 4, 1), -1):
            if None in (f[i], s[i], f[i - 1], s[i - 1]):
                break
            if f[i - 1] <= s[i - 1] and f[i] > s[i]:
                hit("ema_cross", "long", f"EMA 9 crossed above EMA 21 at {b5[i].ts:%H:%M}")
                break
            if f[i - 1] >= s[i - 1] and f[i] < s[i]:
                hit("ema_cross", "short", f"EMA 9 crossed below EMA 21 at {b5[i].ts:%H:%M}")
                break
    if "rsi_extreme" in want and len(c5) > 15:
        r = rsi(c5)[-1]
        if r is not None and r > 70:
            hit("rsi_extreme", "watch", f"RSI {r:.0f}: overbought")
        elif r is not None and r < 30:
            hit("rsi_extreme", "watch", f"RSI {r:.0f}: oversold")
    if "st_flip" in want:
        b15 = resample(bars, 15)
        d = supertrend_dir(b15)
        for i in range(len(d) - 1, max(len(d) - 3, 0), -1):
            if d[i] is None or d[i - 1] is None:
                break
            if d[i] != d[i - 1]:
                hit("st_flip", "long" if d[i] == 1 else "short",
                    f"Supertrend turned {'up' if d[i] == 1 else 'down'} at {b15[i].ts:%H:%M}")
                break
    if "nr7" in want:
        done = [x for x in days if x[0] < today]
        if len(done) >= 7:
            rng = [x[2] - x[3] for x in done[-7:]]
            if rng[-1] == min(rng):
                hit("nr7", "watch", f"Yesterday's range {rng[-1]:,.2f} was the narrowest of 7 days")
    return hits
