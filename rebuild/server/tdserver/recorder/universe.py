"""Which instruments to record, from Zerodha's daily instruments list (never hard-coded tokens or lot sizes)."""
from __future__ import annotations

import csv
import gzip
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .books import Instrument

# Zerodha's names for the indexes in its NSE/BSE "INDICES" segment -> store folder
INDEX_NAMES = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY", "SENSEX": "SENSEX", "INDIA VIX": "INDIAVIX",
               "NIFTY FIN SERVICE": "FINNIFTY", "NIFTY MID SELECT": "MIDCPNIFTY"}
OPTION_EXCHANGE = {"SENSEX": "BFO"}           # everything else trades on NFO


@dataclass
class Listing:
    token: int
    tradingsymbol: str
    name: str
    expiry: date | None
    strike: float
    kind: str          # CE | PE | EQ (index rows have kind "EQ" and segment INDICES)
    segment: str
    lot_size: int


def parse_dump(text: str) -> list[Listing]:
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            exp = date.fromisoformat(r["expiry"]) if r.get("expiry") else None
            out.append(Listing(int(r["instrument_token"]), r["tradingsymbol"], r["name"].strip('"'), exp,
                               float(r.get("strike") or 0), r["instrument_type"], r["segment"],
                               int(float(r.get("lot_size") or 0))))
        except (KeyError, ValueError):
            continue
    return out


def store_symbol(underlying: str, expiry: date, strike: float, kind: str) -> str:
    k = int(strike) if strike == int(strike) else strike
    return f"{underlying}{expiry:%y%m%d}{k}{kind}"


def indexes(listings: list[Listing]) -> dict[str, Instrument]:
    out = {}
    for l in listings:
        if l.segment == "INDICES" and l.tradingsymbol in INDEX_NAMES:
            u = INDEX_NAMES[l.tradingsymbol]
            out[u] = Instrument(l.token, u, u, "idx")
    return out


def options_around(listings: list[Listing], underlying: str, spot: float, today: date, expiries: int,
                   strikes_each_side: int) -> list[Instrument]:
    """Calls and puts `strikes_each_side` strikes either side of the money on the nearest `expiries` expiries."""
    seg = f"{OPTION_EXCHANGE.get(underlying, 'NFO')}-OPT"
    rows = [l for l in listings if l.segment == seg and l.name == underlying and l.expiry and l.expiry >= today
            and l.kind in ("CE", "PE")]
    chosen: list[Instrument] = []
    for exp in sorted({l.expiry for l in rows})[:expiries]:
        strikes = sorted({l.strike for l in rows if l.expiry == exp})
        if not strikes:
            continue
        atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        band = set(strikes[max(0, atm - strikes_each_side): atm + strikes_each_side + 1])
        for l in rows:
            if l.expiry == exp and l.strike in band:
                chosen.append(Instrument(l.token, store_symbol(underlying, exp, l.strike, l.kind), underlying, "opt",
                                         exp, l.strike, l.kind))
    return chosen


class Dumps:
    """Today's instruments lists, downloaded once a day and kept beside the store (reference/instruments)."""

    def __init__(self, root: Path, fetch):
        self.root, self.fetch = root, fetch       # fetch(exchange) -> CSV text

    def load(self, day: date, exchanges=("NSE", "BSE", "NFO", "BFO")) -> list[Listing]:
        d = self.root / "reference" / "instruments" / day.isoformat()
        d.mkdir(parents=True, exist_ok=True)
        out = []
        for ex in exchanges:
            f = d / f"{ex}.csv.gz"
            if f.exists():
                text = gzip.decompress(f.read_bytes()).decode()
            else:
                text = self.fetch(ex)
                f.write_bytes(gzip.compress(text.encode()))
            out.extend(parse_dump(text))
        return out
