"""Option chain analytics from the store: OI and its change since the previous day, implied volatility, build-up,
PCR, max pain, ATM IV and the ATM straddle, as of any minute of a recorded day.

Build-up compares the option's price and OI with the previous trading day's close:
  price up + OI up = long build-up; price down + OI up = short build-up;
  price up + OI down = short covering; price down + OI down = long unwinding.
"""
from __future__ import annotations

from datetime import date, datetime, time

from . import greeks as G

EXPIRY_CLOSE = time(15, 30)


def years_to_expiry(expiry: date, day: date, at: time) -> float:
    secs = (datetime.combine(expiry, EXPIRY_CLOSE) - datetime.combine(day, at)).total_seconds()
    return max(0.0, secs) / (365.0 * 86400)


def buildup(dp: float | None, doi: float | None) -> str | None:
    if dp is None or doi is None or dp == 0 or doi == 0:
        return None
    if dp > 0:
        return "long build-up" if doi > 0 else "short covering"
    return "short build-up" if doi > 0 else "long unwinding"


def max_pain(rows: list[dict]) -> float | None:
    """The expiry level at which option buyers would be paid the least (by open interest)."""
    ks = [r["strike"] for r in rows]
    best, best_k = None, None
    for x in ks:
        pay = 0.0
        for r in rows:
            k = r["strike"]
            pay += ((r["CE"] or {}).get("oi") or 0) * max(0.0, x - k) + ((r["PE"] or {}).get("oi") or 0) * max(0.0, k - x)
        if best is None or pay < best:
            best, best_k = pay, x
    return best_k


def analyse(chain: dict, prev: dict | None, rate: float, band: int | None = None) -> dict:
    """chain / prev: DataService.chain() output for the day and for the previous trading day (same expiry)."""
    spot = chain.get("spot")
    day = date.fromisoformat(chain["date"])
    at = time.fromisoformat(chain["at"])
    expiry = date.fromisoformat(chain["expiry"])
    t = years_to_expiry(expiry, day, at)
    prev_by = {r["strike"]: r for r in (prev or {}).get("strikes", [])}
    rows = sorted(chain.get("strikes", []), key=lambda r: r["strike"])
    out_rows = []
    for r in rows:
        k = r["strike"]
        row = {"strike": k}
        for kind in ("CE", "PE"):
            c = r.get(kind)
            if not c:
                row[kind] = None
                continue
            pc = (prev_by.get(k) or {}).get(kind) or {}
            ltp, oi = c.get("ltp"), c.get("oi")
            d_oi = (oi - pc["oi"]) if (oi is not None and pc.get("oi") is not None) else None
            d_px = (ltp - pc["ltp"]) if (ltp is not None and pc.get("ltp") is not None) else None
            iv = G.implied_vol(kind, ltp, spot, k, t, rate) if (spot and ltp and t > 0) else None
            row[kind] = {"ltp": ltp, "volume": c.get("volume"), "oi": oi, "oi_change": d_oi,
                         "change": round(d_px, 2) if d_px is not None else None,
                         "iv": round(iv * 100, 2) if iv else None, "buildup": buildup(d_px, d_oi),
                         "as_of": c.get("as_of")}
        out_rows.append(row)
    ce_oi = sum((r["CE"] or {}).get("oi") or 0 for r in out_rows)
    pe_oi = sum((r["PE"] or {}).get("oi") or 0 for r in out_rows)
    atm = min(out_rows, key=lambda r: abs(r["strike"] - spot))["strike"] if (spot and out_rows) else None
    atm_row = next((r for r in out_rows if r["strike"] == atm), None)
    ivs = [x["iv"] for x in ((atm_row or {}).get("CE"), (atm_row or {}).get("PE")) if x and x.get("iv")]
    straddle = None
    if atm_row and atm_row.get("CE") and atm_row.get("PE") and atm_row["CE"]["ltp"] and atm_row["PE"]["ltp"]:
        straddle = round(atm_row["CE"]["ltp"] + atm_row["PE"]["ltp"], 2)
    if band and atm is not None:
        i = next(i for i, r in enumerate(out_rows) if r["strike"] == atm)
        out_rows = out_rows[max(0, i - band): i + band + 1]
    return {"underlying": chain["underlying"], "expiry": chain["expiry"], "date": chain["date"], "at": chain["at"],
            "spot": spot, "days_to_expiry": round(t * 365, 2), "atm": atm,
            "atm_iv": round(sum(ivs) / len(ivs), 2) if ivs else None, "straddle": straddle,
            "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None, "max_pain": max_pain(rows) if rows else None,
            "total_oi": {"CE": ce_oi, "PE": pe_oi}, "prev_day": (prev or {}).get("date"), "rows": out_rows}
