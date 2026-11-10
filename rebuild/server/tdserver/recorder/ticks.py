"""Kite WebSocket binary frames -> ticks (see Kite Connect docs, "WebSocket streaming").

Tradable packet: ltp 4, quote 44, full 184 bytes. Index packet: ltp 8, quote 28, full 32 bytes.
Prices are integers in paise for NSE/BSE (divide by 100). Exchange timestamps are Unix seconds.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass
class Tick:
    token: int
    ltp: float
    volume: int | None = None      # day's traded quantity so far
    oi: int | None = None
    bid: float | None = None
    ask: float | None = None
    exchange_ts: int | None = None # Unix seconds from the exchange, when the packet has it
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None


def is_index(token: int) -> bool:
    return (token & 0xFF) == 9      # segment byte 9 = indices


def parse_frame(msg: bytes) -> list[Tick]:
    if len(msg) < 4:                # 1-byte heartbeats
        return []
    n = struct.unpack(">H", msg[:2])[0]
    i, out = 2, []
    for _ in range(n):
        if i + 2 > len(msg):
            break
        ln = struct.unpack(">H", msg[i:i + 2])[0]
        p = msg[i + 2:i + 2 + ln]
        i += 2 + ln
        if len(p) != ln or ln < 8:
            continue
        tok, ltp = struct.unpack(">iI", p[:8])
        t = Tick(tok, ltp / 100.0)
        if is_index(tok):
            if ln >= 28:
                hi, lo, op = struct.unpack(">III", p[8:20])
                t.day_high, t.day_low, t.day_open = hi / 100.0, lo / 100.0, op / 100.0
            if ln >= 32:
                t.exchange_ts = struct.unpack(">I", p[28:32])[0] or None
        elif ln >= 44:
            t.volume = struct.unpack(">I", p[16:20])[0]
            op, hi, lo = struct.unpack(">III", p[28:40])
            t.day_open, t.day_high, t.day_low = op / 100.0, hi / 100.0, lo / 100.0
            if ln >= 184:
                t.oi = struct.unpack(">I", p[48:52])[0]
                t.exchange_ts = struct.unpack(">I", p[60:64])[0] or None
                bq, bp = struct.unpack(">iI", p[64:72])
                aq, ap = struct.unpack(">iI", p[124:132])
                t.bid = bp / 100.0 if bq > 0 and bp else None
                t.ask = ap / 100.0 if aq > 0 and ap else None
        out.append(t)
    return out


# ---- packet builders, for tests and the replay tool ----------------------------------------------------

def index_packet(token: int, ltp: float, ts: int = 0, hi: float = 0, lo: float = 0, op: float = 0) -> bytes:
    return struct.pack(">iIIIIIiI", token, round(ltp * 100), round(hi * 100), round(lo * 100), round(op * 100),
                       0, 0, ts)


def option_packet(token: int, ltp: float, volume: int, bid: float | None, ask: float | None, ts: int = 0,
                  oi: int = 0) -> bytes:
    body = struct.pack(">iIIIIIIIIIIIIIII", token, round(ltp * 100), 0, 0, volume, 0, 0,
                       round(ltp * 100), round(ltp * 100), round(ltp * 100), 0, 0, oi, 0, 0, ts)
    depth = b""
    for side in (bid, ask):
        for k in range(5):
            q, px = (75, round(side * 100)) if (side and k == 0) else (0, 0)
            depth += struct.pack(">iIhh", q, px, 1 if q else 0, 0)
    return body + depth


def frame(*packets: bytes) -> bytes:
    return struct.pack(">H", len(packets)) + b"".join(struct.pack(">H", len(p)) + p for p in packets)
